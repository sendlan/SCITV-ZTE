# IPTV 方案 (四川电信中兴平台内网EPG) 使用指南

一套跑在 OpenWrt 路由器上的 IPTV 方案:
认证机顶盒账号 → 抓频道列表 → 抓节目单(XMLTV) → 生成可回看的 m3u。
局域网内用 TVBox / DIYP / 其他播放器看直播 + 7 天回看。

> 本模板已去除真实账号等关键信息, 按下面步骤改完就能用。
> 参考环境: ImmortalWRT/OpenWrt, Python3, rtp2httpd, nginx。

---

## 一、整体结构(三个角色, 分工明确)

| 任务 | 脚本 | 调度 | 说明 |
|------|------|------|------|
| 抓取**频道列表** | `/etc/iptv_auth.sh` | 每月 1 号 04:05 | 认证 + 抓最新频道列表 -> `/www/iptv_channels.json` |
| 刷新**节目单 EPG** | `/etc/iptv_refresh.sh` | 每 6 小时 | 用缓存频道列表刷节目单 `epg.xml` + 重新生成 m3u |
| 转发/路由 | `25-iptv-route` + `rtp2httpd` | 开机/接口up | 组播转 HTTP、EPG 网段路由 |

**设计要点**: 频道列表是"慢数据"(频道增删很少), 一个月抓一次就够了;
节目单是"快数据"(每天更新), 所以每 6 小时刷一次, 且不重复抓频道列表,
减少对运营商 EPG 网关的认证频率。

---

## 二、文件清单

| 文件 | 部署位置 | 作用 |
|------|----------|------|
| `iptv.conf` | `/etc/iptv.conf` | **唯一需要你改的文件** (所有账号/服务器参数) |
| `iptv_auth.sh` | `/etc/iptv_auth.sh` | 抓频道列表 (每月) |
| `iptv_refresh.sh` | `/etc/iptv_refresh.sh` | 刷 EPG + 重建 m3u (每6h) |
| `iptv_epg.py` | `/etc/iptv_epg.py` | 认证/抓频道/抓节目单的核心逻辑 |
| `gen_m3u_epg.py` | `/etc/gen_m3u_epg.py` | 从频道列表生成多套 m3u |
| `25-iptv-route` | `/etc/hotplug.d/iface/25-iptv-route` | IPTV 接口up时加路由 |
| `crontabs_root` | 参考 `/etc/crontabs/root` | 定时任务样例 |

产出文件(在 `/www`, nginx 直接对外):
- `/www/iptv_channels.json` 频道列表
- `/www/epg.xml` 节目单 (XMLTV, 播放器可订阅)
- `/www/{城市}.m3u` TVBox/DIYP 用播放列表
- `/www/player.m3u` 内置 rtp2httpd 的播放器用播放列表
- `/www/{城市}_vst.m3u` VST/kookong 用播放列表

---

## 三、部署步骤

### 1. 前置: 路由器网络接口

把运营商的 IPTV 走一条独立的 `iptv` 接口 (DHCP), 机顶盒信息要配进去:

```
uci set network.iptv=interface
uci set network.iptv.proto='dhcp'
uci set network.iptv.device='iptv'              # 对应物理网口 VLAN 四川电信这里wan.43
uci set network.iptv.clientid='你的机顶盒序列号'
uci set network.iptv.hostname='你的机顶盒序列号'
uci set network.iptv.vendorid='SCITV'           # 运营商标识, 各地不同
uci set network.iptv.metric='20'
uci commit network
/etc/init.d/network reload
```

> 四川电信的机顶盒信息会通过 DHCP 的 clientid/option60 等校验,
> 从光猫的 IPTV 口 / 机顶盒抓包可拿到。

### 2. 安装依赖

```
opkg update
opkg install python3 rtp2httpd nginx curl
```

> `iptv_epg.py` 只用 Python 标准库, 无需额外 pip 包。

### 3. 部署脚本

把本目录所有文件传上路由器对应位置, 赋权:
192.168.x.1需改为为你OpenWrt的实际地址
```
scp -O iptv.conf iptv_auth.sh iptv_refresh.sh iptv_epg.py gen_m3u_epg.py 25-iptv-route root@192.168.x.1:/etc/
scp -O 25-iptv-route root@192.168.x.1:/etc/hotplug.d/iface/25-iptv-route
ssh root@192.168.x.1 "chmod +x /etc/iptv_auth.sh /etc/iptv_refresh.sh /etc/iptv_epg.py /etc/gen_m3u_epg.py /etc/hotplug.d/iface/25-iptv-route; chmod 640 /etc/iptv.conf"
```

### 4. 配置(核心步骤)

编辑 `/etc/iptv.conf`, 对照样例填你的值:

| 参数 | 含义 | 哪里拿 |
|------|------|--------|
| `EPG_HOST` | 内网 EPG 服务器 `IP:端口` | 机顶盒抓包 / 同地区已破解方案 |
| `USERID` | 宽带 IPTV 账号 | 运营商给的拨号账号 |
| `STBID` | 机顶盒序列号 | 机顶盒背面 / 认证抓包 |
| `AUTHENTICATOR` | 认证密钥(与STBID绑定) | 认证抓包(见下文) |
| `LAN_IP` | 路由器局域网IP | 你自己 |
| `HTTP_PORT` | rtp2httpd 端口(默认5140) | 你配置的 |
| `TS_SERVER` | 回看RTSP服务器 `IP:554` | 频道列表 TimeShiftURL 里可提取 |
| `IGMP_NET/GW` | IPTV 网段/网关 | `ip route` 观察 |
| `CITY_NAME/CITY_ID` | 你的城市 | 自己 |

> **怎么拿到 AUTHENTICATOR?**
> 最稳的办法: 用光猫自带的端口镜像功能抓取机顶盒数据 (或路由器上抓包),
> 抓机顶盒开机认证时对 `EPG_HOST` 发的 `auth.jsp` POST 请求,
> 里面的 `Authenticator` 参数就是。它和 STBID 绑定, 长期不变。

### 5. 定时任务

把 `crontabs_root` 内容写入 `/etc/crontabs/root` 后重启 cron:

```
# 频道列表: 每月 1 号 04:05 抓取一次
5 4 1 * * /etc/iptv_auth.sh >/dev/null 2>&1
# 节目单+播放列表: 每 6 小时刷新
17 */6 * * * /etc/iptv_refresh.sh >/dev/null 2>&1
```

```
/etc/init.d/cron restart
```

> 想每半个月: 把第一行改成 `5 4 1,15 * * /etc/iptv_auth.sh`。

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

> nginx 默认把 `/www` 作为站点根, `epg.xml`、`*.m3u` 直接能访问。
> 若 `player.m3u`(80口) 也要能播, 需在 nginx 加 `/rtp/` 反向代理到 5140。

---

## 四、手动测试

```
# 1. 抓频道列表(相当于每月任务手动跑一次)
/etc/iptv_auth.sh
#   -> 看到 "频道列表: XXX个" 即成功

# 2. 刷节目单(相当于每6小时任务)
python3 /etc/iptv_epg.py epg
#   -> 生成 /www/epg.xml, 日志在 /tmp/iptv_epg.log

# 3. 重建 m3u
python3 /etc/gen_m3u_epg.py

# 4. 验证路由(EPG/组播网段走 iptv 口)
ip route | grep 182.146
```

浏览器访问验证:
- `http://路由器IP/nanchong.m3u`   (播放列表)
- `http://路由器IP/epg.xml`        (节目单)

---

## 五、播放器接入

- **TVBox / DIYP**: 地址填 `http://路由器IP/nanchong.m3u`, EPG 填 `http://路由器IP/epg.xml`
- **PotPlayer / 其他支持catchup的播放器**: 同上
- **VST/kookong**: 用 `nanchong_vst.m3u`
- **网页内置播放(rtp2httpd)**: 用 `player.m3u`

---

## 六、常见问题

| 现象 | 原因/排查 |
|------|-----------|
| `认证失败: 未获取到UserToken` | 账号/密钥/STBID 错误; `iptv` 口未获取到IP; 网段路由不对 |
| `无频道` | 抓包对比 frameset_builder 参数; 门户组号 `USER_GROUP` 不对 |
| 直播花屏/卡顿 | 组播网段路由缺失: 检查 `ip route` 里 182.146.x 走 iptv 口 |
| 回看不出来 | `TS_SERVER`/`TS_VENDOR` 不对; 播放器不支持 catchup 格式 |
| 节目单空白 | EPG 网段路由断了; 或认证频率过高被运营商限流(降低频率) |
| `STBIP` 获取失败 | `iptv` 接口名不对或未获取DHCP |

排查日志: `tail -f /tmp/iptv_epg.log`

---

## 七、安全提醒

- `AUTHENTICATOR` / `STBID` 相当于机顶盒身份, **不要外传/发到公开论坛**。
- 建议 `chmod 600 /etc/iptv.conf`。
- 不要把路由器直接暴露到公网; 播放器接入仅限局域网。

---

## 八、原理简述(想折腾再看)

1. **认证**: 机顶盒用账号+密钥向 `EPG_HOST/iptvepg/platform/auth.jsp` 换取 `UserToken`,
   并建立门户会话(cookie)。这是所有后续请求的前提。
2. **抓频道**: 请求 `frameset_builder.jsp`, 返回 JS 里有 `jsSetChannelInfo`
   (节目单ID/名称) 和 `addChannel`(直播ChannelID/组播地址/回看URL), 按名称关联成一份 JSON。
3. **抓节目单**: 对每个频道请求 `getTvodlist.jsp` 拿最近几天的节目, 拼成 XMLTV。
4. **回看地址**: 形如
   `http://LAN_IP:5140/rtsp/回看服务器/live/{channelid}.mpg?vcdnid=001&programbegin={(b)yyyyMMddHHmmss}+08&...`,
   播放器用 `{(b)..}` / `{(e)..}` 占位符自动填当前播放时间。
