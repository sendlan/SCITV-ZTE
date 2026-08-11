#!/bin/sh
# 抓取 IPTV 频道列表 (由 crontab 调度, 示例: 每月 1 号 04:05)
# ------------------------------------------------------------
# 用法:
#   /etc/iptv_auth.sh   抓取频道列表并重新生成播放列表
# 依赖: python3 (/etc/iptv_epg.py /etc/gen_m3u_epg.py), /etc/iptv.conf

[ -r /etc/iptv.conf ] && . /etc/iptv.conf

echo "[iptv_auth] 开始抓取频道列表..."
python3 /etc/iptv_epg.py channels || { echo "[iptv_auth] 抓取失败" >&2; exit 1; }

# 拿到最新列表后立即重新生成播放列表
python3 /etc/gen_m3u_epg.py >/dev/null 2>&1

echo "[iptv_auth] 完成 -> /www/iptv_channels.json"