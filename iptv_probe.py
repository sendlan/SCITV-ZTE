#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 探测频道组播是否有视频反馈, 给频道打 healthy 标记
# ------------------------------------------------------------
# 原理: 每个频道经本机 rtp2httpd (127.0.0.1:5140) 用【纯组播】拉流,
#   累计收到 >=MIN_BYTES 视频数据即刻判健康并断开;
#   到 PROBE_TIME 还没攒够则判无信号。
#   注意: 探测必须用纯组播(不加 fcc)。带 fcc 并发拉流会因 FCC 会话
#   冲突导致误判; 而"频道有没有画面"本质就是组播是否持续有数据。
# 输出:
#   /www/iptv_channels.json   每个频道追加字段 healthy (true/false)
#   /www/iptv_bad.txt         无信号且非白名单的频道清单(会被 m3u 排除)
# 白名单(探测失败也保留): CCTV1-17 、名称含"卫视"
# 用法:
#   python3 /etc/iptv_probe.py
# ------------------------------------------------------------
import json, re, time, threading, socket
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

CHANNEL_FILE = "/www/iptv_channels.json"
BAD_FILE = "/www/iptv_bad.txt"
HTTP_PORT = CFG.get("HTTP_PORT", "5140")
PROBE_TIME = 4           # 最多拉流秒数
MIN_BYTES = 300 * 1024   # 收到>=此字节数判健康
CONCURRENCY = 4          # 并发探测路数 (已实测 4 并发纯组播不误判, 大于4会开始丢判定)


def base_url(igmp):
    # 纯组播拉流, 不加 fcc (详见文件头说明)
    return f"http://127.0.0.1:{HTTP_PORT}/rtp/{igmp.replace('igmp://', '')}"


def is_keep(name):
    """白名单: CCTV1-17 / 卫视 不参与排除"""
    m = re.search(r'CCTV[- ]?([0-9]{1,3})', name or "")
    if m:
        try:
            if 1 <= int(m.group(1)) <= 17:
                return True
        except ValueError:
            pass
    if "卫视" in (name or ""):
        return True
    return False


def probe_one(ch):
    name = ch.get("name", "")
    igmp = ch.get("igmp", "")
    if not igmp:
        return name, False
    url = base_url(igmp)
    got = 0
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIME) as f:
            while True:
                try:
                    b = f.read(65536)
                except Exception:
                    break
                if not b:
                    break
                got += len(b)
                if got >= MIN_BYTES:
                    break
    except Exception:
        pass
    return name, got >= MIN_BYTES


def main():
    chans = json.load(open(CHANNEL_FILE))
    print(f"探测 {len(chans)} 个频道, 并发 {CONCURRENCY}, 每路最多 {PROBE_TIME}s, 下限 {MIN_BYTES} 字节...")
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        results = list(ex.map(probe_one, chans))
    bad = []
    for ch, (name, ok) in zip(chans, results):
        ch["healthy"] = ok
        if not ok and not is_keep(name):
            bad.append(name)
    json.dump(chans, open(CHANNEL_FILE, "w"), ensure_ascii=False, indent=1)
    n_ok = sum(1 for _, ok in results if ok)
    n_keep = sum(1 for ch, ok in zip(chans, results) if not ok and is_keep(ch.get("name", "")))
    print(f"健康 {n_ok} / 无信号 {len(results) - n_ok}")
    print(f"白名单保留(探测失败) {n_keep}")
    with open(BAD_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(bad) + "\n")
    print(f"将被排除 {len(bad)} 个 -> {BAD_FILE}")
    for b in bad[:60]:
        print("  exclude:", b)
    if len(bad) > 60:
        print(f"  ... 共 {len(bad)} 个")


if __name__ == "__main__":
    main()