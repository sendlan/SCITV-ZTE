#!/bin/sh
# 每 6 小时刷新节目单与 m3u (复用频道列表, 不重复认证)

LOG=/tmp/iptv_epg.log

[ -f /www/iptv_channels.json ] || /etc/iptv_auth.sh >> "$LOG" 2>&1

python3 /etc/iptv_epg.py epg >> "$LOG" 2>&1
python3 /etc/iptv_probe.py --only-excluded >> "$LOG" 2>&1
python3 /etc/gen_m3u_epg.py >> "$LOG" 2>&1
