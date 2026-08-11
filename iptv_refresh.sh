#!/bin/sh
# 高频刷新任务: EPG 节目单 + 播放列表 (默认每 6 小时)
# ------------------------------------------------------------
# 重要: 本脚本【不】重新抓取频道列表。
# 频道列表由 /etc/iptv_auth.sh 每 14 天抓取一次。
# 这里只复用缓存的 /www/iptv_channels.json 刷新节目单和 m3u,
# 避免频繁认证把 IPTV 网关风控。
#
# 若频道列表还不存在(全新部署), 会自动先抓取一次兜底。

LOG=/tmp/iptv_epg.log

if [ ! -f /www/iptv_channels.json ]; then
    echo "[iptv_refresh] 频道列表缺失, 先抓取一次" >> "$LOG" 2>&1
    /etc/iptv_auth.sh --force >> "$LOG" 2>&1
fi

python3 /etc/iptv_epg.py epg >> "$LOG" 2>&1
python3 /etc/gen_m3u_epg.py >> "$LOG" 2>&1