#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# IPTV 认证签名 (AUTHENTICATOR) 生成器, 仅用标准库 + openssl
# 流程: 取 token -> 拼接明文 -> DES 加密 -> auth.jsp 验证 -> 写回 iptv.conf
#   iptv_token.py           生成并验证后写回
#   iptv_token.py --dry-run 只算不写
#   iptv_token.py --show    解出现有签名 (自检)
import os
import re
import sys
import time
import random
import shutil
import subprocess
import urllib.request
import urllib.parse

CONF = os.environ.get("IPTV_CONF", "/etc/iptv.conf")
LOG = os.environ.get("IPTV_TOKEN_LOG", "/tmp/iptv_token.log")
UA = ("Mozilla/5.0 (X11; Linux x86_64; SkyworthBrowser) AppleWebKit/534.24 "
      "(KHTML, like Gecko) Safari/534.24 SkWebKit-JS-CTC")


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 配置

def load_conf(path):
    cfg = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                m = re.match(r'^([A-Za-z0-9_]+)="?(.*?)"?$', s)
                if m:
                    cfg[m.group(1)] = m.group(2).strip()
    except Exception:
        pass
    return cfg


def ifaddr(dev):
    """取指定网卡的 IPv4 与小写冒号 MAC（内网 IP / MAC 都要绑定到签名里）"""
    ip = mac = ""
    try:
        out = subprocess.run(["ip", "-4", "addr", "show", "dev", dev],
                             capture_output=True, text=True).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        ip = m.group(1) if m else ""
    except Exception:
        pass
    try:
        out = subprocess.run(["ip", "link", "show", "dev", dev],
                             capture_output=True, text=True).stdout
        m = re.search(r"link/ether ([0-9a-f:]{17})", out)
        mac = m.group(1).lower() if m else ""
    except Exception:
        pass
    return ip, mac


# ---------------------------------------------------------------- 算法

def _des_ecb(key8, data, decrypt=False):
    a = "-d" if decrypt else "-e"
    p = subprocess.run(["openssl", "enc", a, "-des-ecb", "-K", key8.hex(), "-nopad"],
                       input=data, capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("openssl DES 失败: " + p.stderr.decode("utf-8", "replace")[:200])
    return p.stdout


def encrypt_3des(data, password):
    """Security.Encrypt3DES 的精确复刻（含手工填充与输出截断）"""
    key = (password.encode("utf-8") + b"0" * 16)[:24]
    nBQ = 8 - (len(data) % 8)
    padded = data + bytes([nBQ]) * nBQ
    k1, k2, k3 = key[0:8], key[8:16], key[16:24]
    if k2 == b"0" * 8 and k3 == b"0" * 8:
        # 退化：DESede(K2=K3=0) == 单 DES(K1)
        out = _des_ecb(k1, padded)
    else:                                   # 完整 3DES: E_K3 → D_K2 → E_K1
        out = _des_ecb(k3, padded)
        out = _des_ecb(k2, out, decrypt=True)
        out = _des_ecb(k1, out)
    return out[:len(padded)].hex().upper()  # ★ 输出截断，不是 out.hex()


def decrypt_3des(hexstr, password):
    """反向（仅供 --show 自检用）。返回填充后的明文字节。"""
    key = (password.encode("utf-8") + b"0" * 16)[:24]
    ct = bytes.fromhex(hexstr)
    ct = ct[:len(ct) // 8 * 8]          # ECB 要求整块
    return _des_ecb(key[0:8], ct, decrypt=True)


def token_of(plain):
    """从明文里取第 2 段（token），供日志显示"""
    p = plain.split("$")
    return p[1] if len(p) > 1 else ""


# ---------------------------------------------------------------- 网络

def fetch(url, data=None, timeout=15):
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return urllib.request.urlopen(req, timeout=timeout).read()


def fetch_token(host, userid, timeout=15):
    """服务端下发的 token（@@ + 30 位数字，含签发时间戳）"""
    url = ("http://%s/iptvepg/platform/getencrypttoken.jsp"
           "?UserID=%s&Action=Login&TerminalFlag=1"
           % (host, urllib.parse.quote(userid)))
    try:
        txt = fetch(url, timeout=timeout).decode("gbk", "replace")
    except Exception as e:
        log("取 token 失败 %s: %s" % (host, e))
        return None
    m = re.search(r"@@\d{10,40}", txt)
    if not m:
        log("响应里没找到 token（%d 字节）" % len(txt))
        return None
    return m.group(0)


def verify(host, userid, stbip, authenticator, timeout=15):
    """拿签名去 auth.jsp 换 UserToken —— 只有这一步成功才允许落盘"""
    data = urllib.parse.urlencode({
        "UserID": userid, "Authenticator": authenticator,
        "StbIP": stbip, "LastTermno": "0"}).encode()
    try:
        txt = fetch("http://%s/iptvepg/platform/auth.jsp" % host,
                    data=data, timeout=timeout).decode("utf-8", "replace")
    except Exception as e:
        return None, "HTTP 异常: %s" % e
    m = re.search(r"UserToken', *'([^']+)'", txt)
    if m:
        return m.group(1), ""
    return None, " ".join(txt.split())[:300]


def eds_discover(eds_host, userid, stbid, password, stbip, mac, timeout=15):
    """向 EDS 调度服务询问当前 token 服务器地址 (与机顶盒开机流程一致).

    GET  {eds}/api/authentication  -> AccountAuth (32hex)
    用 AccountAuth 作为 token 位生成引导签名, POST 到 {eds}/api/authurl
    从返回页面的 document.location 中取出 "IP:端口".

    返回 "IP:端口", 失败返回 None.
    """
    base = "http://%s/esv2UICloudAuthClient/UICloudAuthClient" % eds_host
    try:
        page = fetch("%s/api/authentication?UserID=%s&Action=Login"
                     % (base, urllib.parse.quote(userid)),
                     timeout=timeout).decode("gbk", "replace")
        m = re.search(r"CTCGetAuthInfo\('([0-9a-f]{32})'\)", page)
        if not m:
            log("EDS 认证页里没找到 AccountAuth")
            return None
        str_auth = "%08d" % random.randrange(0, 10000000)
        plain = "%s$%s$%s$%s$%s$%s$$CTC" % (str_auth, m.group(1),
                                            userid, stbid, stbip, mac)
        auth_hex = encrypt_3des(plain.encode("utf-8"), password)
        data = urllib.parse.urlencode({"UserID": userid,
                                       "authenticator": auth_hex}).encode()
        resp = fetch("%s/api/authurl" % base, data=data,
                     timeout=timeout).decode("gbk", "replace")
        m = re.search(r"http://([0-9.]+:\d+)/iptvepg/platform/index\.jsp", resp)
        if not m:
            log("EDS 响应里没找到 EPG 地址 (可能返回了认证失败页)")
            return None
        return m.group(1)
    except Exception as e:
        log("EDS 发现失败 %s: %s" % (eds_host, e))
        return None


# ---------------------------------------------------------------- 落盘

KEEP_BAK = 5        # 自动备份只保留最近 5 份（轮转，避免长期累积）


def write_conf(path, key, value):
    """原地替换 KEY=".."，其它行原样保留；先备份并轮转旧备份"""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = "%s.bak-%s" % (path, stamp)
    shutil.copy2(path, bak)
    try:                                # 只转自己的自动备份（*.bak-YYYYmmdd-HHMMSS）
        d = os.path.dirname(path) or "."
        pat = re.compile(r"^%s\.bak-\d{8}-\d{6}$" % re.escape(os.path.basename(path)))
        olds = sorted(f for f in os.listdir(d) if pat.match(f))
        for f in olds[:-KEEP_BAK]:
            os.remove(os.path.join(d, f))
    except Exception:
        pass
    pat = re.compile(r'^\s*%s\s*=' % re.escape(key))
    hit = False
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = '%s="%s"\n' % (key, value)
            hit = True
            break
    if not hit:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append('%s="%s"\n' % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, path)
    return bak, hit


# ---------------------------------------------------------------- 主流程

def main():
    dry = "--dry-run" in sys.argv
    cfg = load_conf(CONF)
    env = os.environ.get

    userid = env("USERID") or cfg.get("USERID", "")
    stbid = env("STBID") or cfg.get("STBID", "")
    password = env("PASSWORD") or cfg.get("PASSWORD", "")
    epg_host = env("EPG_HOST") or cfg.get("EPG_HOST", "")
    token_host_cfg = env("TOKEN_HOST") or cfg.get("TOKEN_HOST", "")
    eds_host = env("EDS_HOST") or cfg.get("EDS_HOST", "")
    stbip = env("STBIP") or cfg.get("STBIP", "")
    mac = env("MAC") or cfg.get("MAC", "")
    if not stbip or not mac:
        i, m = ifaddr(cfg.get("IPTV_IF", "iptv"))
        stbip = stbip or i
        mac = mac or m

    # --show: 解开现有签名，用于核对算法与参数是否仍然对得上
    if "--show" in sys.argv:
        cur = cfg.get("AUTHENTICATOR", "")
        if not cur or not password:
            log("--show 需要 iptv.conf 里同时有 AUTHENTICATOR 和 PASSWORD")
            return 1
        try:
            pt = decrypt_3des(cur, password)
        except Exception as e:
            log("解密失败: %s" % e)
            return 1
        pad = pt[-1] if pt else 0
        body = pt[:-pad] if 0 < pad <= 8 else pt
        seg = body.split(b"$")
        log("现有 AUTHENTICATOR 明文:")
        for i, s in enumerate(seg):
            log("  段%d = %s" % (i, s.decode("utf-8", "replace")))
        return 0

    missing = [n for n, v in [("USERID", userid), ("STBID", stbid),
                              ("PASSWORD", password), ("EPG_HOST", epg_host),
                              ("内网IP", stbip), ("MAC", mac)] if not v]
    if missing:
        log("缺少参数: %s（请在 %s 里补齐）" % (", ".join(missing), CONF))
        return 1

    # 先走 EDS 发现, 失败再按 TOKEN_HOST/EPG_HOST 兜底
    cands = []
    if token_host_cfg and str(token_host_cfg).strip():
        cands.append(str(token_host_cfg).strip())
    if epg_host and str(epg_host).strip() and str(epg_host).strip() not in cands:
        cands.append(str(epg_host).strip())
    token = None
    used = None
    if eds_host:
        log("EDS 调度发现 (%s)..." % eds_host)
        disc = eds_discover(eds_host, userid, stbid, password, stbip, mac)
        if disc:
            log("EDS 下发 token 服务器: %s" % disc)
            token = fetch_token(disc, userid)
            if token:
                used = "%s (EDS发现)" % disc
    if not token:
        for th in cands:
            log("尝试从 %s 获取 token..." % th)
            token = fetch_token(th, userid)
            if token:
                used = th
                break
    if not token:
        log("★ EDS 与所有候选均失败: %s，保持原有 AUTHENTICATOR 不变" % ", ".join(cands))
        return 1
    log("token = %s  (来源 %s)" % (token, used))

    str_auth = "%08d" % random.randrange(0, 10000000)
    plain = "%s$%s$%s$%s$%s$%s$$CTC" % (str_auth, token, userid, stbid, stbip, mac)
    try:
        auth = encrypt_3des(plain.encode("utf-8"), password)
    except Exception as e:
        log("签名失败: %s" % e)
        return 1
    log("明文  = %s" % plain)
    log("签名  = %s..%s (%d hex)" % (auth[:32], auth[-16:], len(auth)))

    ut, why = verify(epg_host, userid, stbip, auth)
    if not ut:
        log("★ 验证未通过，保持原有值不变。原因: %s" % why)
        return 1
    log("验证通过: auth.jsp 返回 UserToken = %s" % ut)

    if dry:
        log("--dry-run：未写入 %s" % CONF)
        return 0

    old = cfg.get("AUTHENTICATOR", "")
    if old == auth:
        log("签名与现值相同，无需写入")
        return 0
    bak, hit = write_conf(CONF, "AUTHENTICATOR", auth)
    log("已更新 %s 的 AUTHENTICATOR（%s，备份 %s）"
        % (CONF, "替换原行" if hit else "新增行", bak))
    return 0


if __name__ == "__main__":
    sys.exit(main())
