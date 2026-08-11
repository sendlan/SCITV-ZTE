#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# IPTV 内网EPG: 认证 + 频道列表 + 节目单 -> XMLTV
# ------------------------------------------------------------
# 用法:
#   /etc/iptv_epg.py channels   仅抓取频道列表 -> /www/iptv_channels.json
#   /etc/iptv_epg.py epg        用已有频道列表刷新节目单 -> /www/epg.xml
#   /etc/iptv_epg.py all        先抓频道列表再刷节目单
#
# 所有密钥/参数从 /etc/iptv.conf 读取, 见该文件。
# ------------------------------------------------------------
import json, re, sys, time, html, datetime
import urllib.request, urllib.parse
import http.cookiejar
import subprocess


def load_conf(path="/etc/iptv.conf"):
    cfg = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r'^([A-Za-z0-9_]+)=\"?(.*?)\"?$', line)
                if m:
                    cfg[m.group(1)] = m.group(2).strip()
    except Exception:
        pass
    return cfg


CFG = load_conf()
EPG_HOST = CFG.get("EPG_HOST", "")
USERID = CFG.get("USERID", "")
STBID = CFG.get("STBID", "")
AUTHENTICATOR = CFG.get("AUTHENTICATOR", "")
STB_TYPE = CFG.get("STB_TYPE", "MT5001")
USER_GROUP = CFG.get("USER_GROUP", "42")

# 动态获取 iptv 接口 IP（DHCP 分配，可能变化）
_out = subprocess.getoutput("ip -4 addr show dev iptv 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1")
STBIP = _out.strip().splitlines()[0].strip() if _out.strip() else ""

UA = "Mozilla/5.0 (X11; Linux x86_64; SkyworthBrowser) AppleWebKit/534.24 (KHTML, like Gecko) Safari/534.24 SkWebKit-JS-CTC"

CJ_FILE = "/tmp/iptv_cj.txt"
CHANNEL_FILE = "/www/iptv_channels.json"
EPG_FILE = "/www/epg.xml"
LOG = "/tmp/iptv_epg.log"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


if not STBIP:
    log("启动失败: 无法获取 iptv 接口 IP，请检查 iptv 接口是否已获取 DHCP")
    sys.exit(1)


def http_get(url, data=None, timeout=15):
    cj = http.cookiejar.MozillaCookieJar()
    try:
        cj.load(CJ_FILE, ignore_discard=True, ignore_expires=True)
    except Exception:
        pass
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        resp = opener.open(req, timeout=timeout)
        body = resp.read()
        cj.save(CJ_FILE, ignore_discard=True, ignore_expires=True)
        return body
    except Exception as e:
        log(f"HTTP错误 {url[:80]}: {e}")
        return b""


def auth():
    """认证, 返回 UserToken"""
    body = http_get(f"http://{EPG_HOST}/iptvepg/platform/auth.jsp",
                    data=urllib.parse.urlencode({
                        "UserID": USERID, "Authenticator": AUTHENTICATOR,
                        "StbIP": STBIP, "LastTermno": "0"}).encode())
    txt = body.decode("utf-8", errors="replace")
    m = re.search(r"UserToken', *'([^']+)'", txt)
    if m:
        return m.group(1)
    log("认证失败: " + txt[:200])
    return None


def portal(ut):
    """建立门户会话"""
    http_get(f"http://{EPG_HOST}/iptvepg/function/index.jsp?UserGroupNMB={USER_GROUP}&EPGGroupNMB=&UserToken={urllib.parse.quote(ut)}&UserID={urllib.parse.quote(USERID)}&STBID={STBID}&LastTermno=0")
    http_get(f"http://{EPG_HOST}/iptvepg/function/funcportalauth.jsp",
             data=urllib.parse.urlencode({
                 "UserToken": ut, "UserID": USERID, "STBID": STBID,
                 "stbinfo": "", "prmid": "", "stbtype": STB_TYPE}).encode())
    http_get(f"http://{EPG_HOST}/iptvepg/function/frame.jsp")


def norm_name(s):
    """规范化频道名: 去掉横杠和空格用于关联"""
    return re.sub(r"[\-\s（）()]+", "", s)


def get_channels(ut):
    """获取频道列表, 返回 [{channelid,tvid,name,igmp,timeshift,timeshiftURL,shift}]"""
    portal(ut)
    body = http_get(f"http://{EPG_HOST}/iptvepg/function/frameset_builder.jsp",
                    data=b"MAIN_WIN_SRC=%2Fiptvepg%2Fframe6%2Fportal.jsp&NEED_UPDATE_STB=1&BUILD_ACTION=FRAMESET_BUILDER",
                    timeout=40)
    txt = body.decode("gbk", errors="replace")
    # 1) jsSetChannelInfo: mixno, tvid(ch0000...), name -> EPG节目单ID
    js_pat = re.compile(r"jsSetChannelInfo\('(\d+)','\d+','(\d+)','\d+','([^']+)','([^']+)'[^;]*;")
    tvs = []
    for mt in js_pat.finditer(txt):
        nm = mt.group(4)
        try:
            nm = json.loads('"%s"' % nm)
        except Exception:
            pass
        tvs.append({"mixno": mt.group(1), "tvid": mt.group(3), "name": nm})
    # 2) addChannel: ChannelID(live), ChannelName, igmp, TimeShiftURL
    blocks = re.findall(r'addChannel\([^;]+;', txt)
    adds = []
    for blk in blocks:
        def g(field):
            m2 = re.search(field + '="([^"]*)"', blk)
            return m2.group(1) if m2 else ""
        adds.append({
            "uid": g("UserChannelID"), "cid": g("ChannelID"),
            "name": g("ChannelName"), "igmp": g("ChannelURL"),
            "ts": g("TimeShift"), "tsurl": g("TimeShiftURL"),
        })
    # 3) 用规范名关联
    by_name = {}
    for a in adds:
        by_name.setdefault(norm_name(a["name"]), a)
    channels = []
    for tv in tvs:
        a = by_name.get(norm_name(tv["name"]))
        channels.append({
            "channelid": a["cid"] if a else "",
            "tvid": tv["tvid"],
            "mixno": tv["mixno"],
            "name": tv["name"],
            "igmp": a["igmp"] if a else "",
            "timeshift": a["ts"] if a else "",
            "timeshiftURL": a["tsurl"] if a else "",
        })
    return channels


def get_tvodlist(ut, channelid, date):
    """按频道+日期拉节目单"""
    url = (f"http://{EPG_HOST}/iptvepg/frame299/action/getTvodlist.jsp"
           f"?channelcode={urllib.parse.quote(channelid)}&timedata={date}")
    body = http_get(url, timeout=20)
    txt = body.decode("gbk", errors="replace")
    m = re.search(r'\{.*\}', txt, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        return []
    return data.get("items", [])


def to_xmltv_date(s):
    """2026.08.02 00:00:00 -> 20260802000000 +0800"""
    s = s.strip()
    try:
        t = time.strptime(s, "%Y.%m.%d %H:%M:%S")
        return time.strftime("%Y%m%d%H%M%S", t) + " +0800"
    except Exception:
        return None


def build_xmltv(channels, days=2):
    """生成 XMLTV 节目单"""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="nanchong-iptv-epg">']
    for ch in channels:
        chid = ch.get("channelid") or ch.get("tvid") or ch.get("mixno", "")
        out.append(f'  <channel id="{html.escape(chid)}">')
        out.append(f'    <display-name>{html.escape(ch["name"])}</display-name>')
        out.append('  </channel>')
    ut = auth()
    if not ut:
        return None
    portal(ut)
    total = 0
    for ch in channels:
        if not ch.get("tvid"):
            continue
        for d in range(days):
            day = (datetime.date.today() - datetime.timedelta(days=d)).strftime("%Y.%m.%d")
            items = get_tvodlist(ut, ch["tvid"], day)
            for it in items:
                s = to_xmltv_date(it.get("begintime", ""))
                e = to_xmltv_date(it.get("endtime", ""))
                if not s or not e:
                    continue
                title = it.get("prevuename", "")
                out.append(f'  <programme start="{s}" stop="{e}" channel="{html.escape(ch["channelid"] or ch["tvid"])}">')
                out.append(f'    <title lang="zh">{html.escape(title)}</title>')
                out.append('  </programme>')
                total += 1
        log(f"  已处理 {ch['name']}")
    log(f"节目总数: {total}")
    out.append('</tv>')
    return "\n".join(out)


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "all"
    if mode in ("all", "channels"):
        ut = auth()
        if not ut:
            sys.exit(1)
        chans = get_channels(ut)
        json.dump(chans, open(CHANNEL_FILE, "w"), ensure_ascii=False)
        log(f"频道列表: {len(chans)}个 -> {CHANNEL_FILE}")
        if mode == "channels":
            return
    # EPG
    chans = json.load(open(CHANNEL_FILE)) if mode != "channels" else chans
    xml = build_xmltv(chans)
    if xml:
        open(EPG_FILE, "w").write(xml)
        log(f"XMLTV -> {EPG_FILE} ({len(xml)}B)")
    log("完成")


if __name__ == "__main__":
    main()