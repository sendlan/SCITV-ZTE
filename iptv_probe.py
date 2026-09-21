#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 频道健康探测 (增强版, 防误杀 + 黑名单按月清零)
# ------------------------------------------------------------
# 判定逻辑
# --------
# 每个频道用【纯组播】经本机 rtp2httpd (127.0.0.1:HTTP_PORT) 拉流,
# 每轮最多尝试 ATTEMPTS 次, 每次最多观察 PROBE_TIME 秒:
#
#   ok      : 单次拿到 >= GOOD_BYTES 字节   -> 健康
#   partial : 拿到 >0 但不足 GOOD_BYTES     -> 健康 (慢启动/低码率, 不算故障)
#   dead    : 本轮 ATTEMPTS 次全部 0 字节   -> 本轮判失败
#
# 黑名单机制 (重要)
# ----------------
#   fail_streak = 本"月度周期"内的连续失败轮数。
#   * 月度任务用 --reset-streak --passes 2 调用:
#       先把所有频道的 fail_streak 清零  ->  每月的黑名单从零重算
#       第 1 轮判 dead 的频道, 间隔 PASS_GAP 秒后再复核 1 轮 (第 2 轮)
#       两轮都 dead 才让 fail_streak 达到 EXCLUDE_AFTER(=2) 而被排除
#     => 效果: 月内双轮确认(抗抖动) + 跨月归零(运营商调整后能自己回来)
#   * --only-excluded 用于每 6 小时的快速回捞:
#       只复测当前已被排除的频道, 且【只做转正, 不做降级】
#       -> 被误判的频道不必等一个月, 一旦恢复信号立刻回到列表
#
# 相比最初版本的关键改动
# ----------------------
#  1) 单次探测 -> 多次重试, 任一次拿到数据即判有信号
#  2) 判定分三态, "有数据但不足" 不再算故障
#  3) 引入 fail_streak + EXCLUDE_AFTER, 单次失败不再直接拉黑
#  4) 新增 --reset-streak / --passes, 实现"月度黑名单清零"
#  5) 新增 --only-excluded, 实现被排除频道的快速回捞
#  6) 白名单收敛: 只保留 CCTV1-17 + "卫视" + KEEP_KEYWORDS(默认"卫视")。
#     CHC 等付费电影频道**不做特例保留** —— 它们起播慢但确实有流,
#     靠"多次重试 + 三态判定(partial 算健康)"就能自己通过, 无需塞进白名单。
#  7) 白名单关键词改为 /etc/iptv.conf 的 KEEP_KEYWORDS 可配
#  8) 新增 --dry-run / --sample / --names-file, 便于小样本复测而不动 channels.json
#
# 用法:
#   python3 /etc/iptv_probe.py --reset-streak --passes 2   # 月度(黑名单清零)
#   python3 /etc/iptv_probe.py --only-excluded             # 6 小时快速回捞
#   python3 /etc/iptv_probe.py                             # 单轮全量, 写回
#   python3 /etc/iptv_probe.py --names-file /tmp/n --dry-run   # 只复测名单, 不写回
# ------------------------------------------------------------
import argparse, json, os, random, re, socket, sys, time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CFG = {}
try:
    with open("/etc/iptv.conf", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^([A-Za-z0-9_]+)=\"?(.*?)\"?$', line)
            if m:
                CFG[m.group(1)] = m.group(2).strip()
except Exception:
    pass

CHANNEL_FILE = CFG.get("CHANNEL_FILE", "/www/iptv_channels.json")
BAD_FILE = CFG.get("BAD_FILE", "/www/iptv_bad.txt")
HTTP_PORT = CFG.get("HTTP_PORT", "5140")

PROBE_TIME = 5                      # 单次观察上限(秒)
ATTEMPTS = 3                        # 每轮最多尝试次数
RETRY_GAP = 1.5                     # 重试间隔(秒)
PASS_GAP = 30                       # 复核轮次之间的间隔(秒)
GOOD_BYTES = 128 * 1024             # 单次拿到这么多就算 ok
CONCURRENCY = 4                     # 并发路数 (实测 >4 开始丢判定)
KEEP_KEYWORDS = [k for k in CFG.get("KEEP_KEYWORDS", "卫视").split(",") if k]
EXCLUDE_AFTER = int(CFG.get("EXCLUDE_AFTER", "2") or 2)   # 月内连续失败几轮才排除


def is_keep(name):
    """白名单: CCTV1-17 / KEEP_KEYWORDS 命中 -> 失败也不排除"""
    name = name or ""
    m = re.search(r'CCTV[- ]?([0-9]{1,3})', name)
    if m:
        try:
            if 1 <= int(m.group(1)) <= 17:
                return True
        except ValueError:
            pass
    for kw in KEEP_KEYWORDS:
        if kw and kw in name:
            return True
    return False


def base_url(igmp):
    """纯组播拉流, 不加 fcc (带 fcc 并发会因 FCC 会话冲突误判)"""
    return "http://127.0.0.1:%s/rtp/%s" % (HTTP_PORT, igmp.replace("igmp://", ""))


def streak(ch):
    return int(ch.get("fail_streak") or 0)


def is_excluded(ch):
    """当前是否处于"被排除"状态"""
    return (ch.get("healthy") is False
            and streak(ch) >= EXCLUDE_AFTER
            and not is_keep(ch.get("name", "")))


def probe_once(url):
    """返回本次尝试收到的字节数 (0 = 完全无数据)"""
    got = 0
    deadline = time.time() + PROBE_TIME
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIME) as f:
            while time.time() < deadline:
                try:
                    b = f.read(65536)
                except socket.timeout:
                    break
                except Exception:
                    break
                if not b:
                    break
                got += len(b)
                if got >= GOOD_BYTES:
                    break
    except Exception:
        pass
    return got


def probe_one(args):
    idx, ch = args
    name = ch.get("name", "")
    igmp = ch.get("igmp", "")
    if not igmp:
        return idx, "dead", 0, 0, name

    url = base_url(igmp)
    best = 0
    for attempt in range(ATTEMPTS):
        # 抖动, 避免多路同时撞在同一秒
        time.sleep(random.uniform(0, 0.4) + (RETRY_GAP if attempt else 0))
        got = probe_once(url)
        if got > best:
            best = got
        if got >= GOOD_BYTES:
            return idx, "ok", best, attempt + 1, name

    if best > 0:
        return idx, "partial", best, ATTEMPTS, name
    return idx, "dead", 0, ATTEMPTS, name


def run_pass(items, label):
    print("  [%s] 探测 %d 个频道 ..." % (label, len(items)))
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        results = list(ex.map(probe_one, items))
    n_ok = sum(1 for r in results if r[1] != "dead")
    print("  [%s] 有信号 %d / 本轮全0 %d" % (label, n_ok, len(results) - n_ok))
    return results


def print_table(results):
    print()
    print("%-4s %-24s %-12s %10s %5s" % ("#", "频道", "判定", "最大字节", "尝试"))
    print("-" * 64)
    for idx, state, best, tries, name in results:
        flag = {"ok": "OK  ", "partial": "慢  ", "dead": "无信号"}[state]
        print("%-4d %-24s %-12s %10d %5d" % (idx, name, flag, best, tries))


def save(chans):
    json.dump(chans, open(CHANNEL_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    exc = [c.get("name") for c in chans if is_excluded(c)]
    with open(BAD_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(exc) + ("\n" if exc else ""))
    return exc


def main():
    global ATTEMPTS, PROBE_TIME

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印, 不写回 channels.json")
    ap.add_argument("--sample", type=int, default=0, help="随机抽 N 个频道探测")
    ap.add_argument("--names-file", default="", help="只探测该文件里列出的频道名(每行一个)")
    ap.add_argument("--attempts", type=int, default=ATTEMPTS)
    ap.add_argument("--timeout", type=int, default=PROBE_TIME)
    ap.add_argument("--reset-streak", action="store_true",
                    help="先把所有 fail_streak 清零 (月度任务用: 黑名单按月清零)")
    ap.add_argument("--passes", type=int, default=1,
                    help="复核轮数; 第 2 轮起只复测上一轮判 dead 的频道")
    ap.add_argument("--pass-gap", type=int, default=PASS_GAP, help="轮次之间等待秒数")
    ap.add_argument("--only-excluded", action="store_true",
                    help="只复测当前被排除的频道, 且只做转正不做降级 (快速回捞)")
    a = ap.parse_args()

    ATTEMPTS, PROBE_TIME = a.attempts, a.timeout
    passes = max(1, a.passes)

    # 月度清零必须配全量探测, 否则未参与本次探测的频道会被"免费放行"
    if a.reset_streak and (a.names_file or a.sample or a.only_excluded):
        print("错误: --reset-streak 只能与全量探测一起用"
              "(不能搭配 --names-file / --sample / --only-excluded)")
        sys.exit(2)

    chans = json.load(open(CHANNEL_FILE, encoding="utf-8"))

    # ---- 选片 ----
    if a.only_excluded:
        picked = [c for c in chans if c.get("healthy") is False
                  and streak(c) >= EXCLUDE_AFTER]
        mode = "快速回捞(只复测被排除频道, 只转正不降级)"
    elif a.names_file:
        want = {l.strip() for l in open(a.names_file, encoding="utf-8") if l.strip()}
        picked = [c for c in chans if c.get("name") in want]
        mode = "指定名单"
    elif a.sample:
        picked = random.sample(chans, min(a.sample, len(chans)))
        mode = "随机抽样"
    else:
        picked = chans
        mode = "全量"

    print("模式: %s" % mode)
    print("目标 %d 个频道 | 轮数 %d | 每轮 %ds x %d 次尝试 | 并发 %d | ok阈值 %dKB | EXCLUDE_AFTER=%d"
          % (len(picked), passes, PROBE_TIME, ATTEMPTS, CONCURRENCY,
             GOOD_BYTES // 1024, EXCLUDE_AFTER))

    if not picked:
        print("没有需要探测的频道, 退出。")
        return

    # ---- 月度清零 ----
    if a.reset_streak:
        n = 0
        for c in chans:
            if streak(c):
                c["fail_streak"] = 0
                n += 1
        print("\n[月度清零] 已把 %d 个频道的 fail_streak 归零, 黑名单从零重算" % n)

    # ---- 第 1 轮 ----
    print()
    results = run_pass(list(enumerate(picked)), "轮1")
    # 记录每个频道"挂了几轮"; fail_streak 按失败轮数累加,
    # 这样 --passes 2 时"两轮都挂"才够到 EXCLUDE_AFTER=2
    dead_passes = {}
    for idx, state, _, _, _ in results:
        if state == "dead":
            dead_passes[idx] = dead_passes.get(idx, 0) + 1

    # ---- 复核轮 (只对轮1判 dead 的频道) ----
    dead_idx = [idx for idx, state, _, _, _ in results if state == "dead"]
    for p in range(2, passes + 1):
        if not dead_idx:
            print("\n  轮1 无 dead 频道, 无需复核。")
            break
        print("\n  等 %ds 后复核轮1判 dead 的 %d 个频道 ..." % (a.pass_gap, len(dead_idx)))
        time.sleep(a.pass_gap)
        sub = [(idx, picked[idx]) for idx in dead_idx]
        r2 = run_pass(sub, "轮%d" % p)
        # 记录本轮回捞到的频道 (只探一轮就会把它们误杀)
        saved = [r[4] for r in r2 if r[1] != "dead"]
        if saved:
            print("  第%d轮复核救回 %d 个(只探一轮会被误杀): %s" % (p, len(saved), saved))
        # 用复核结果覆盖对应的条目
        by_idx = {r[0]: r for r in r2}
        results = [by_idx.get(r[0], r) for r in results]
        for r in r2:
            if r[1] == "dead":
                dead_passes[r[0]] = dead_passes.get(r[0], 0) + 1
        dead_idx = [idx for idx, state, _, _, _ in results if state == "dead"]
        print("  复核后仍判 dead: %d 个" % len(dead_idx))

    print_table(results)

    stat = {"ok": 0, "partial": 0, "dead": 0}
    for _, state, _, _, _ in results:
        stat[state] += 1
    print()
    print("汇总: ok=%d  partial(有数据但慢)=%d  dead(全0)=%d"
          % (stat["ok"], stat["partial"], stat["dead"]))

    if a.dry_run:
        print("\n[dry-run] 未写回任何文件")
        return

    # ---- 写回 ----
    by_idx = {r[0]: (r[1], r[2], r[3]) for r in results}
    recovered = []

    for i, ch in enumerate(picked):
        state, best, tries = by_idx[i]
        name = ch.get("name", "")
        if a.only_excluded:
            # 只转正, 不降级
            if state != "dead":
                if ch.get("healthy") is False or streak(ch):
                    recovered.append(name)
                ch["healthy"] = True
                ch["fail_streak"] = 0
            ch["last_probe"] = int(time.time())
            ch["last_probe_bytes"] = best
            continue

        if state == "dead":
            ch["healthy"] = False
            # 按"挂掉的轮数"累加, 而不是固定 +1
            ch["fail_streak"] = streak(ch) + dead_passes.get(i, 1)
        else:
            ch["healthy"] = True
            ch["fail_streak"] = 0
        ch["last_probe"] = int(time.time())
        ch["last_probe_bytes"] = best

    exc = save(chans)
    print("\n已写回 %s" % CHANNEL_FILE)
    if a.only_excluded:
        print("快速回捞: 转正 %d 个 %s" % (len(recovered), recovered if recovered else ""))
    print("当前被排除(fail_streak>=%d): %d 个 -> %s" % (EXCLUDE_AFTER, len(exc), BAD_FILE))
    for n in exc:
        print("  exclude:", n)

    # 参考: 本次 dead 但还没被排除的(下个月若继续 dead 才会被排除)
    pending = [c.get("name") for c in chans
               if c.get("healthy") is False and 0 < streak(c) < EXCLUDE_AFTER]
    if pending:
        print("\n本次无信号但暂不排除(待下月复核) %d 个: %s" % (len(pending), pending))


if __name__ == "__main__":
    main()
