# IPTV 方案 (四川电信/各地电信内网EPG) 使用指南

一套跑在 OpenWrt 路由器上的 IPTV 方案:
认证机顶盒账号 → 抓频道列表 → **自动探测频道健康(排除无信号台)** → 抓节目单(XMLTV) → 生成可回看的 m3u。
局域网内用 TVBox / DIYP / 其他播放器看直播 + 回看。

> 本模板为可复用版本, 关键参数集中在 `iptv.conf`, 改完即可部署。
> 环境: ImmortalWRT / OpenWrt (qualcommax 等), Python3, rtp2httpd, nginx。

---

## 一、整体结构(四个角色)

| 任务 | 脚本 | 调度 | 说明 |
|------|------|------|------|
| 抓取**频道列表** | `/etc/iptv_auth.sh` | 每月 1 号 04:05 | 认证 + 抓最新频道列表 → `iptv_channels.json` |
| **探测频道健康** | `/etc/iptv_probe.py` | 随 auth(每月一次) | 逐个频道拉组播流, 无信号的打 `healthy=false`, 自动进黑名单 |
| 刷新**节目单 EPG** | `/etc/iptv_refresh.sh` | 每 6 小时 | 刷 EPG + 按 healthy 重建 m3u |
| 转发/路由 | `25-iptv-route` + `rtp2httpd` | 开机/接口up | 组播转 HTTP、EPG 网段路由 |

**设计要点**:
- 频道列表 + 健康探测都是"慢数据", 每月一次。
- 节目单是"快数据", 每 6 小时刷一次, 且不重复抓列表/探测, 减少对运营商网关的访问频率。
- **无信号自动排除**: 探测结果记为 `healthy`, m3u 生成时自动跳过 `healthy=false` 的频道
  (如 中国体育、华西证券、天翼高清临时频道 等), 但 **CCTV1-17 和含"卫视"的频道不受影响**,
  即使探测失败也保留, 不会误删主流频道。

---

## 二、文件清单

| 文件 | 部署位置 | 作用 |
|------|----------|------|
| `iptv.conf` | `/etc/iptv.conf` | **唯一需要你改的文件** (账号/服务器参数) |
| `iptv_auth.sh` | `/etc/iptv_auth.sh` | 抓频道列表 + 探测健康 + 重建 m3u (每月) |
| `iptv_probe.py` | `/etc/iptv_probe.py` | 探测每个频道组播是否有视频反馈 |
| `iptv_refresh.sh` | `/etc/iptv_refresh.sh` | 刷 EPG + 重建 m3u (每6h) |
| `iptv_epg.py` | `/etc/iptv_epg.py` | 认证/抓频道/抓节目单的核心逻辑 |
| `gen_m3u_epg.py` | `/etc/gen_m3u_epg.py` | 从频道列表生成多套 m3u, 排除无信号频道 |
| `25-iptv-route` | `/etc/hotplug.d/iface/25-iptv-route` | IPTV 接口up时加路由 |
| `crontabs_root` | 参考 `/etc/crontabs/root` | 定时任务样例 |

产出文件(在 `/www`):
- `/www/iptv_channels.json` 频道列表 (含 healthy 标记)
- `/www/iptv_bad.txt` 无信号频道清单 (排查用)
- `/www/epg.xml` 节目单 (XMLTV)
- `/www/{城市}.m3u` TVBox/DIYP 用 (长格式时间戳, 走 5140 口, 可回看)
- `/www/player.m3u` 内置 rtp2httpd 播放器用 (短格式, 走 80 口)

---

## 三、部署步骤

### 1. 前置: 路由器网络接口

把运营商的 IPTV 走独立 `iptv` 接口 (DHCP), 机顶盒信息要配进去:

```
uci set network.iptv=interface
uci set network.iptv.proto='dhcp'
uci set network.iptv.device='iptv'
uci set network.iptv.clientid='你的机顶盒序列号'
uci set network.iptv.hostname='你的机顶盒序列号'
uci set network.iptv.vendorid='SCITV'           # 运营商标识, 各地不同
uci set network.iptv.metric='20'
uci commit network
/etc/init.d/network reload
```

> 四川电信的机顶盒信息通过 DHCP 的 clientid/option60 等校验, 从光猫 IPTV 口抓包可拿到。

### 2. 安装依赖

```
opkg update
opkg install python3 rtp2httpd nginx curl
```

> 所有 python 脚本只用标准库, 无需 pip 包。

### 3. 部署脚本

把本目录文件传到路由器对应位置并赋权:

```
scp -O iptv.conf iptv_auth.sh iptv_refresh.sh iptv_epg.py iptv_probe.py gen_m3u_epg.py 25-iptv-route root@192.168.1.1:/etc/
ssh root@192.168.1.1 "chmod +x /etc/iptv_auth.sh /etc/iptv_refresh.sh /etc/iptv_epg.py /etc/iptv_probe.py /etc/gen_m3u_epg.py /etc/hotplug.d/iface/25-iptv-route; chmod 640 /etc/iptv.conf"
```

### 4. 配置(核心步骤)

编辑 `/etc/iptv.conf` (只改这一个文件):

| 参数 | 含义 | 哪里拿 |
|------|------|--------|
| `EPG_HOST` | 内网 EPG 服务器 `IP:端口` | 机顶盒抓包 |
| `USERID` | 宽带 IPTV 账号 | 运营商给的拨号账号 |
| `STBID` | 机顶盒序列号 | 机顶盒背面 / 认证抓包 |
| `AUTHENTICATOR` | 认证密钥(与STBID绑定) | 认证抓包 |
| `FCC_SERVER` | FCC快速换台服务器 `IP:端口` | 频道列表 TimeShift/FCC 字段 |
| `TS_SERVER` | 回看RTSP服务器 `IP:554` | 频道列表 TimeShiftURL 字段 |
| `LAN_IP` / `HTTP_PORT` | 对外地址 / rtp2httpd 端口 | 你自己 |
| `IGMP_NET`/`IGMP_GW` | IPTV 网段/网关 | `ip route` 观察 |
| `CITY_NAME`/`CITY_ID` | 城市名(显示/文件名) | 自己 |

> **怎么拿 AUTHENTICATOR** (没法直接要): 电脑/路由器在机顶盒和光猫之间抓包,
> 抓机顶盒开机向 `EPG_HOST` 发的 `auth.jsp` POST 里的 `Authenticator` 参数。
> 它和 STBID 绑定, 长期不变。

### 5. 定时任务

写入 `/etc/crontabs/root` 后重启 cron:

```
# 频道列表+健康探测: 每月 1 号 04:05
5 4 1 * * /etc/iptv_auth.sh >/dev/null 2>&1
# 节目单+播放列表: 每 6 小时刷新
17 */6 * * * /etc/iptv_refresh.sh >/dev/null 2>&1
```

```
/etc/init.d/cron restart
```

> 想每半个月: 第一行改成 `5 4 1,15 * * /etc/iptv_auth.sh`。

### 6. 组播转 HTTP (rtp2httpd)

```
opkg install rtp2httpd
cat > /etc/config/rtp2httpd <<'EOF'
config rtp2httpd 'main'
    option enabled '1'
    option listen_port '5140'
    option upstream_interface 'iptv'
    option loglevel '4'
EOF
/etc/init.d/rtp2httpd enable
/etc/init.d/rtp2httpd start
```

---

## 四、手动测试

```
# 1. 每月任务: 抓列表 + 探测健康 + 重建 m3u
/etc/iptv_auth.sh
#    只看探测+重建(不重抓列表):
/etc/iptv_auth.sh --probe

# 2. 探测结果
cat /www/iptv_bad.txt            # 被排除的无信号频道
# 健康统计见脚本输出: "健康 X / 无信号 Y"

# 3. 刷节目单(每6小时任务)
python3 /etc/iptv_epg.py epg

# 4. 验证路由
ip route | grep 182
```

浏览器访问: `http://路由器IP/{城市}.m3u`、`http://路由器IP/epg.xml`

---

## 五、播放器接入

- **TVBox / DIYP**: 地址 `http://路由器IP/{城市}.m3u`, EPG `http://路由器IP/epg.xml`
- **盒子内置(rtp2httpd)**: 用 `player.m3u`
- **web 内置播放器**: `http://路由器IP:5140/player` (验证 FCC/换台速度用)

---

## 六、探测与排除逻辑(FCC/无信号专题)

**为什么用纯组播探测?**
- rtp2httpd 带 FCC 参数并发拉流会因 FCC 会话冲突误判;
  纯组播是持续广播, 并发无冲突, 且"有没有画面"本质取决于组播有无数据。
- 实测: 4 并发纯组播探测 316 频道约 4 分钟, 判定可靠。

**判定标准**: 4 秒内收到 ≥300KB 视频数据 → healthy; 否则无信号。

**白名单**: 名称匹配 `CCTV 1-17` 或含"卫视"的频道, 即使探测失败也保留。
(防止个别时段某卫视暂时无信号被误删。)

**探测出的无信号典型**: 中国体育1-3、华西证券、天翼高清临时频道、
雅克音乐季(非直播时段)、付费频道、国产SA频道残留等。

建议新台出现时手动跑一次 `/etc/iptv_auth.sh --probe` 更新黑名单。

---

## 七、常见问题

| 现象 | 原因/排查 |
|------|-----------|
| `认证失败: 未获取到UserToken` | 账号/密钥/STBID 错误; `iptv` 口未获取IP; 网段路由不对 |
| 无频道 | 抓包对比 frameset_builder 参数; 门户组号 `USER_GROUP` 不对 |
| 直播花屏/卡顿 | 组播网段路由缺失: 检查 `ip route` 里 182.146.x 走 iptv 口 |
| 回看不出来 | `TS_SERVER`/`TS_VENDOR` 不对; 播放器不支持 catchup 格式 |
| 某频道一直消失 | 是被健康探测排除了: 看 `/www/iptv_bad.txt` |
| 换台还是慢 | FCC 是否在 m3u URL 上(看 `?fcc=`); 播放器缓冲策略也影响起播 |
| 节目单空白 | EPG 网段路由断; 认证频率过高被限流 |

排查日志: `tail -f /tmp/iptv_epg.log`, `logread | grep rtp2httpd`

---

## 八、安全提醒

- `AUTHENTICATOR` / `STBID` 相当于机顶盒身份, **不要外传**。
- `chmod 600 /etc/iptv.conf`。
- 播放器接入仅限局域网, 勿把路由器直接暴露公网。