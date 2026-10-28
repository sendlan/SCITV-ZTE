#!/bin/sh
# IPTV 任务入口
#   (无参数)   刷新签名 -> 抓频道列表 -> 探测健康 -> 重建 m3u
#   --token    只刷新签名
#   --probe    只探测 + 重建 m3u
#   --no-probe 刷新签名 -> 抓频道列表 -> 重建 m3u

[ -r /etc/iptv.conf ] && . /etc/iptv.conf

echo "[iptv_auth] 刷新认证签名..."
python3 /etc/iptv_token.py || echo "[iptv_auth] 警告: 签名刷新失败, 沿用现有值" >&2

if [ "$1" = "--token" ]; then
    echo "[iptv_auth] 完成 -> 仅刷新认证签名"
    exit 0
fi

if [ "$1" != "--probe" ]; then
    echo "[iptv_auth] 开始抓取频道列表..."
    python3 /etc/iptv_epg.py channels || { echo "[iptv_auth] 抓取失败" >&2; exit 1; }
fi

if [ "$1" != "--no-probe" ]; then
    echo "[iptv_auth] 探测频道健康..."
    python3 /etc/iptv_probe.py --reset-streak --passes 2 || { echo "[iptv_auth] 探测失败" >&2; exit 1; }
    python3 /etc/gen_m3u_epg.py >/dev/null 2>&1
    echo "[iptv_auth] 完成 -> /www/iptv_channels.json, m3u 已按健康标记生成"
else
    python3 /etc/gen_m3u_epg.py >/dev/null 2>&1
    echo "[iptv_auth] 完成 -> 仅抓取频道列表, 未探测 (--no-probe)"
fi
