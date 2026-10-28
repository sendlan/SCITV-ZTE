#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 由 /www/iptv_channels.json 生成 m3u
#   /www/{CITY_ID}.m3u  TVBox/DIYP, 长格式时间, 走 HTTP_PORT
#   /www/player.m3u     rtp2httpd 内置播放器, 短格式时间, 走 80
import json, re

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

LAN_IP = CFG.get("LAN_IP", "192.168.1.1")
HTTP_PORT = CFG.get("HTTP_PORT", "5140")
TS_SERVER = CFG.get("TS_SERVER", "")
TS_VENDOR = CFG.get("TS_VENDOR", "001")
FCC_SERVER = CFG.get("FCC_SERVER", "")
CITY_NAME = CFG.get("CITY_NAME", "")
CITY_ID = CFG.get("CITY_ID", "")
KEEP_KEYWORDS = [k for k in CFG.get("KEEP_KEYWORDS", "卫视").split(",") if k]
EXCLUDE_AFTER = int(CFG.get("EXCLUDE_AFTER", "2") or 2)

CHANNEL_FILE = "/www/iptv_channels.json"
EPG_URL = f"http://{LAN_IP}/epg.xml"

TVBOX_FORMAT = "yyyyMMddHHmmss"
PLAYER_FORMAT = "YmdHMS"

# 排除: PIP小窗 + 九宫格频道组合页（非真实单频道，不可独立播放）
BLACKLIST = ["PIP", "高清直播室", "天翼高清", "央视频道", "卫视频道", "综合卫视",
             "本地频道", "热门卫视", "地方卫视", "付费频道"]


def is_keep(name):
    """白名单: CCTV1-17 与名称含"卫视"的频道, 即使无信号检测也保留"""
    m = re.search(r'CCTV[- ]?([0-9]{1,3})', name or "")
    if m:
        try:
            if 1 <= int(m.group(1)) <= 17:
                return True
        except ValueError:
            pass
    if "卫视" in (name or ""):
        return True
    for _kw in KEEP_KEYWORDS:
        if _kw and _kw in (name or ""):
            return True
    return False


def norm(s):
    return re.sub(r"[\-\s\u4e00-\u9fff()]+", "", s or "")


def is_blacklisted(name):
    for kw in BLACKLIST:
        if kw in name:
            return True
    return False


def build(channels, time_format, use_proxy=True):
    """use_proxy=True  -> 城市.m3u: 走 HTTP_PORT, 时间戳 +08
       use_proxy=False -> player.m3u: 走 80 口, 无 +08"""
    out = ["#EXTM3U x-tvg-url=\"%s\"" % EPG_URL]
    skipped = 0
    tz = "+08" if use_proxy else ""
    port = ":%s" % HTTP_PORT if use_proxy else ""
    for ch in channels:
        igmp = (ch.get("igmp") or "").replace("igmp://", "")
        name = ch.get("name", "")
        if is_blacklisted(name):
            skipped += 1
            continue
        if not igmp:
            skipped += 1
            continue
        # 无信号频道排除(白名单除外)
        if (ch.get("healthy") is False
                and int(ch.get("fail_streak") or 0) >= EXCLUDE_AFTER
                and not is_keep(name)):
            skipped += 1
            continue
        chid = ch.get("channelid") or ch.get("tvid", "")
        catchup = ""
        # 回看: 优先用抓取时下发的完整 TimeShiftURL(含签名)
        if chid and ch.get("timeshift") == "1":
            tsurl = ch.get("timeshiftURL") or ""
            if tsurl:
                csrc = tsurl.replace(".sdp", ".mpg").replace("rtsp://", "")
                csrc = csrc.replace("+08", "%2B08")
                csrc = f"http://{LAN_IP}{port}/rtsp/{csrc}"
                csrc = f"{csrc}&programbegin={{(b){time_format}}}{tz}&programend={{(e){time_format}}}{tz}"
            else:
                csrc = (f"http://{LAN_IP}{port}/rtsp/{TS_SERVER}/live/{chid}.mpg"
                        f"?vcdnid={TS_VENDOR}"
                        f"&programbegin={{(b){time_format}}}{tz}"
                        f"&programend={{(e){time_format}}}{tz}")
            catchup = f" catchup=\"default\" catchup-source=\"{csrc}\""
        base = norm(name)
        base = re.sub(r"(高清|超清|4K|标清|HD|SD)$", "", base)
        base = re.sub(r"[\（[^）]*\）|\([^)]*\)", "", base)
        logo = f"https://gcore.jsdelivr.net/gh/taksssss/tv/icon/{base}.png"
        group = f"{CITY_NAME}电信组播" if CITY_NAME else "IPTV"
        out.append(f"#EXTINF:-1 tvg-id=\"{chid}\" tvg-logo=\"{logo}\" group-title=\"{group}\"{catchup},{name}")
        fcc = f"?fcc={FCC_SERVER}&fcc-type=telecom" if FCC_SERVER else ""
        out.append(f"http://{LAN_IP}{port}/rtp/{igmp}{fcc}")
    return "\n".join(out) + "\n", skipped


def main():
    chans = json.load(open(CHANNEL_FILE))
    m3us = [
        (f"/www/{CITY_ID}.m3u",     TVBOX_FORMAT,  True),  # TVBox/DIYP
        ("/www/player.m3u",         PLAYER_FORMAT, False),  # 内置 rtp2httpd
    ]
    for path, fmt, proxy in m3us:
        content, skipped = build(chans, fmt, use_proxy=proxy)
        open(path, "w").write(content)
        n = content.count("#EXTINF")
        print(f"m3u -> {path}, {n} channels (skipped {skipped}), format={fmt}, proxy={proxy}")


if __name__ == "__main__":
    main()