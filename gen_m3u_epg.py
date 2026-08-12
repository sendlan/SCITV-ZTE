#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 由 /www/iptv_channels.json 生成可播 m3u (多套格式)
# ------------------------------------------------------------
# 输出 (路径见 /etc/iptv.conf 的 CITY_ID):
#   /www/{城市}.m3u        -> TVBox/DIYP 用, 长格式 {(b)yyyyMMddHHmmss}+08, 走:5140 口
#   /www/player.m3u        -> 内置 rtp2httpd 播放器用, 短格式 {(b)YmdHMS}, 走 80 口
#   /www/{城市}_vst.m3u    -> VST/kookong 标准 ${} 回看格式
# 参数从 /etc/iptv.conf 读取。
# ------------------------------------------------------------
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

LAN_IP = CFG.get("LAN_IP", "192.168.1.50")
HTTP_PORT = CFG.get("HTTP_PORT", "5140")
TS_SERVER = CFG.get("TS_SERVER", "")
TS_VENDOR = CFG.get("TS_VENDOR", "001")
FCC_SERVER = CFG.get("FCC_SERVER", "")
CITY_NAME = CFG.get("CITY_NAME", "")
CITY_ID = CFG.get("CITY_ID", "")

CHANNEL_FILE = "/www/iptv_channels.json"
EPG_URL = f"http://{LAN_IP}/epg.xml"

TVBOX_FORMAT = "yyyyMMddHHmmss"
PLAYER_FORMAT = "YmdHMS"
VST_FORMAT = "yyyyMMddHHmmss"

# 排除: PIP小窗 + 九宫格频道组合页（非真实单频道，不可独立播放）
BLACKLIST = ["PIP", "高清直播室", "天翼高清", "央视频道", "卫视频道", "综合卫视",
             "本地频道", "热门卫视", "地方卫视", "付费频道"]


def norm(s):
    return re.sub(r"[\-\s\u4e00-\u9fff()]+", "", s or "")


def is_blacklisted(name):
    for kw in BLACKLIST:
        if kw in name:
            return True
    return False


def build(channels, time_format, use_proxy=True, use_dollar=False):
    """use_proxy=True  -> 城市.m3u: 走 HTTP_PORT, 时间戳 +08
       use_proxy=False -> player.m3u: 走 80 口, 无 +08
       use_dollar=True -> 城市_vst.m3u: VST 标准 ${} 前缀"""
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
        chid = ch.get("channelid") or ch.get("tvid", "")
        catchup = ""
        if chid:
            dollar = "$" if use_dollar else ""
            csrc = (f"http://{LAN_IP}{port}/rtsp/{TS_SERVER}/live/{chid}.mpg"
                    f"?vcdnid={TS_VENDOR}"
                    f"&programbegin={dollar}{{(b){time_format}}}{tz}"
                    f"&programend={dollar}{{(e){time_format}}}{tz}")
            catchup = f" catchup=\"default\" catchup-source=\"{csrc}\""
        base = norm(name)
        base = re.sub(r"(高清|超清|4K|标清|HD|SD)$", "", base)
        base = re.sub(r"[\（[^）]*\）|\([^)]*\)", "", base)
        logo = f"https://gcore.jsdelivr.net/gh/taksssss/tv/icon/{base}.png"
        group = f"{CITY_NAME}组播" if CITY_NAME else "IPTV"
        out.append(f"#EXTINF:-1 tvg-id=\"{chid}\" tvg-logo=\"{logo}\" group-title=\"{group}\"{catchup},{name}")
        fcc = f"?fcc={FCC_SERVER}&fcc-type=telecom" if FCC_SERVER else ""
        out.append(f"http://{LAN_IP}{port}/rtp/{igmp}{fcc}")
    return "\n".join(out) + "\n", skipped


def main():
    chans = json.load(open(CHANNEL_FILE))
    m3us = [
        (f"/www/{CITY_ID}.m3u",     TVBOX_FORMAT,  True,  False),  # TVBox/DIYP  请按需求选择
        ("/www/player.m3u",         PLAYER_FORMAT, False, False),  # rtp2httpd内置  请按需求选择
        (f"/www/{CITY_ID}_vst.m3u", VST_FORMAT,    True,  True),   # VST/kookong  请按需求选择
    ]
    for path, fmt, proxy, dollar in m3us:
        content, skipped = build(chans, fmt, use_proxy=proxy, use_dollar=dollar)
        open(path, "w").write(content)
        n = content.count("#EXTINF")
        print(f"m3u -> {path}, {n} channels (skipped {skipped}), format={fmt}, proxy={proxy}")


if __name__ == "__main__":
    main()