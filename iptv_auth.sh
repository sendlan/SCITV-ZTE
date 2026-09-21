#!/bin/sh
# 抓取 IPTV 频道列表 (由 crontab 调度, 示例: 每月 1 号 04:05)
# ------------------------------------------------------------
# 流程: 抓频道列表 -> 探测频道健康(排除无信号) -> 重建 m3u
# 用法:
#   /etc/iptv_auth.sh   完整跑一遍
#   /etc/iptv_auth.sh --probe    只重新探测+重建 m3u (跳过抓列表)
#   /etc/iptv_auth.sh --no-probe 只抓频道列表 (不做探测, 供凌晨任务使用)
# 依赖: python3 (/etc/iptv_epg.py /etc/iptv_probe.py /etc/gen_m3u_epg.py), /etc/iptv.conf

[ -r /etc/iptv.conf ] && . /etc/iptv.conf

if [ "$1" != "--probe" ]; then
    echo "[iptv_auth] 开始抓取频道列表..."
    python3 /etc/iptv_epg.py channels || { echo "[iptv_auth] 抓取失败" >&2; exit 1; }
fi

# 探测组播是否有视频反馈, 给频道打 healthy 标记
# --reset-streak : 先把 fail_streak 全部清零 -> 每月黑名单从零重算(运营商调整后能自己回来)
# --passes 2     : 月内双轮复核, 只有两轮都无信号才排除 (抗抖动)
# 注意: 探测必须放在"全频道都在播"的时段(默认 20:17)。
#       凌晨 4 点跑会把 乐游/金色学堂/来钓鱼吧专区/雅克音乐季直播 这类
#       分时段频道误判为无信号。
if [ "$1" != "--no-probe" ]; then
    echo "[iptv_auth] 探测频道健康..."
    python3 /etc/iptv_probe.py --reset-streak --passes 2 || { echo "[iptv_auth] 探测失败" >&2; exit 1; }

    # 用健康标记重建播放列表 (连续失败达阈值的频道不会进 m3u)
    python3 /etc/gen_m3u_epg.py >/dev/null 2>&1
    echo "[iptv_auth] 完成 -> /www/iptv_channels.json, m3u 已按健康标记生成"
else
    # 只刷新 m3u (复用现有健康标记), 不重新探测
    python3 /etc/gen_m3u_epg.py >/dev/null 2>&1
    echo "[iptv_auth] 完成 -> 仅抓取频道列表, 未探测 (--no-probe)"
fi