#!/bin/sh
# 抓取 IPTV 频道列表 (由 crontab 调度, 示例: 每月 1 号 04:05)
# ------------------------------------------------------------
# 流程: 抓频道列表 -> 探测频道健康(排除无信号) -> 重建 m3u
# 用法:
#   /etc/iptv_auth.sh   完整跑一遍
#   /etc/iptv_auth.sh --probe    只重新探测+重建 m3u (跳过抓列表)
# 依赖: python3 (/etc/iptv_epg.py /etc/iptv_probe.py /etc/gen_m3u_epg.py), /etc/iptv.conf

[ -r /etc/iptv.conf ] && . /etc/iptv.conf

if [ "$1" != "--probe" ]; then
    echo "[iptv_auth] 开始抓取频道列表..."
    python3 /etc/iptv_epg.py channels || { echo "[iptv_auth] 抓取失败" >&2; exit 1; }
fi

# 探测组播是否有视频反馈, 给频道打 healthy 标记 (排除中国体育/华西证券等无信号台)
echo "[iptv_auth] 探测频道健康..."
python3 /etc/iptv_probe.py || { echo "[iptv_auth] 探测失败" >&2; exit 1; }

# 用 healthy 标记重建播放列表 (无信号且非白名单的频道不会进 m3u)
python3 /etc/gen_m3u_epg.py >/dev/null 2>&1

echo "[iptv_auth] 完成 -> /www/iptv_channels.json, m3u 已按健康标记生成"