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
| 抓取**频道列表** | `/etc/iptv_auth.sh --no-probe` | 每月 1 号 04:05 | 认证 + 抓最新频道列表 → `iptv_channels.json` |
| **探测频道健康** | `/etc/iptv_auth.sh --probe` | 每月 1 号 20:17 | 跑 `iptv_probe.py --reset-streak --passes 2`, 打 `healthy`/`fail_streak` 并按结果重建 m3u |
| 刷新**节目单 EPG** | `/etc/iptv_refresh.sh` | 每 6 小时 | 刷 EPG + 快速回捞被排除频道 + 重建 m3u |
| 转发/路由 | `25-iptv-route` + `rtp2httpd` | 开机/接口up | 组播转 HTTP、EPG 网段路由 |

**设计要点**:
- 频道列表 + 健康探测都是"慢数据", 每月一次, 且**分成两次调度**:
  04:05 只抓列表(避开高峰), 20:17 再探测(黄金时段, 全频道都在播)。
- **探测为什么要挑 20:17 而不是跟着 04:05 跑**:
  "乐游 / 金色学堂 / 来钓鱼吧专区 / 雅克音乐季" 这类**分时段·事件型**频道在凌晨是真的没有流,
  凌晨探测会把它们**每个周期都确定性地误判**为无信号。挑黄金时段是治这个病的。
- 节目单是"快数据", 每 6 小时刷一次, 且不重复抓列表/探测, 减少对运营商网关的访问频率。
- **无信号自动排除**: 探测结果记为 `healthy` + `fail_streak`, m3u 生成时自动跳过
  `healthy=false` **且** `fail_streak >= EXCLUDE_AFTER` 的频道
  (如 中国体育、华西证券、天翼高清临时频道 等)。
  白名单里的频道(CCTV1-17 / 含"卫视" / `KEEP_KEYWORDS` 命中的)即使探测失败也保留, 不会误删主流频道。
- **抗误杀三板斧**(详见 §六): 单轮多次重试、月内双轮复核、每 6 小时快速回捞。

---

## 二、文件清单

| 文件 | 部署位置 | 作用 |
|------|----------|------|
| `iptv.conf` | `/etc/iptv.conf` | **唯一需要你改的文件** (账号/服务器参数, 另含可选的白名单/阈值开关) |
| `iptv_auth.sh` | `/etc/iptv_auth.sh` | 月度任务。`--no-probe` 只抓列表; `--probe` 探测+重建 m3u |
| `iptv_probe.py` | `/etc/iptv_probe.py` | 探测频道组播健康度, 维护 `healthy` / `fail_streak` 与黑名单 |
| `iptv_refresh.sh` | `/etc/iptv_refresh.sh` | 刷 EPG + 快速回捞被排除频道 + 重建 m3u (每6h) |
| `iptv_epg.py` | `/etc/iptv_epg.py` | 认证/抓频道/抓节目单的核心逻辑 |
| `gen_m3u_epg.py` | `/etc/gen_m3u_epg.py` | 从频道列表生成多套 m3u, 按健康标记排除无信号频道 |
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
| `KEEP_KEYWORDS` | (可选)白名单关键词, 逗号分隔, 默认 `卫视` | 见 §六 |
| `EXCLUDE_AFTER` | (可选)连续失败几轮才排除, 默认 `2` | 见 §六 |

> **怎么拿 AUTHENTICATOR** (没法直接要): 电脑/路由器在机顶盒和光猫之间抓包,
> 抓机顶盒开机向 `EPG_HOST` 发的 `auth.jsp` POST 里的 `Authenticator` 参数。
> 它和 STBID 绑定, 长期不变。

### 5. 定时任务

写入 `/etc/crontabs/root` 后重启 cron:

```
# 频道列表: 每月 1 号 04:05 抓取一次 (只抓列表, 不探测)
5 4 1 * * /etc/iptv_auth.sh --no-probe >/dev/null 2>&1
# 频道健康探测: 每月 1 号 20:17 单独跑 (黄金时段, 全频道都在播, 避免分时段频道误判)
17 20 1 * * /etc/iptv_auth.sh --probe >/dev/null 2>&1
# 节目单 + 播放列表: 每 6 小时刷新 (复用缓存的频道列表, 不重复抓取)
17 */6 * * * /etc/iptv_refresh.sh >/dev/null 2>&1
```

```
/etc/init.d/cron restart
```

> **那两行月度任务不要合并成一行。** `--no-probe` 必须留在凌晨(避开运营商网关高峰),
> 探测必须挪到晚上(见 §一 的设计要点)。合并会重新引入"分时段频道被确定性误判"的老问题。
>
> 想每半个月: 把日期 `1` 改成 `1,15`。

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
# 1. 完整跑一遍: 抓列表 + 探测(黑名单清零+双轮) + 重建 m3u
/etc/iptv_auth.sh

# 2. 只抓列表(不探测, 相当于凌晨那趟)
/etc/iptv_auth.sh --no-probe

# 3. 只探测+重建(不重抓列表, 相当于晚上那趟)
/etc/iptv_auth.sh --probe

# 4. 只看探测结果
cat /www/iptv_bad.txt            # 当前被排除的无信号频道
# 详细判定见脚本输出: ok / partial(有数据但慢) / dead 三态统计

# 5. 单跑探测器(可选参数见下)
python3 /etc/iptv_probe.py --help
python3 /etc/iptv_probe.py --only-excluded        # 只复测被排除频道, 只转正不降级
python3 /etc/iptv_probe.py --dry-run --sample 20  # 随机抽 20 个试跑, 不写回文件

# 6. 刷节目单(每6小时任务)
python3 /etc/iptv_epg.py epg

# 7. 验证路由
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

### 为什么用纯组播探测?

- rtp2httpd 带 FCC 参数并发拉流会因 FCC 会话冲突误判;
  纯组播是持续广播, 并发无冲突, 且"有没有画面"本质取决于组播有无数据。
- 实测: 4 并发纯组播探测 316 频道约 4 分钟, 判定可靠。

### 判定标准(三态)

每个频道每轮最多尝试 `ATTEMPTS` 次(默认 3), 每次最多观察 `PROBE_TIME` 秒(默认 5):

| 判定 | 条件 | 处理 |
|---|---|---|
| `ok` | 任一次拿到 ≥ `GOOD_BYTES`(128KB) | 健康 |
| `partial` | 拿到 >0 但不足 128KB | **算健康**(慢启动/低码率, 不是故障) |
| `dead` | 本轮所有尝试都是 0 字节 | 本轮失败 |

> 静态图/彩条频道码率极低, 按老的"必须 300KB"标准会被误杀, 所以引入了 `partial`。

### 黑名单机制(抗误杀的核心)

```
fail_streak = 本"月度周期"内的连续失败轮数
排除条件     = healthy=False 且 fail_streak >= EXCLUDE_AFTER(默认2) 且不在白名单
```

月度任务 `iptv_auth.sh --probe` 内部调用的是 `--reset-streak --passes 2`:

1. **跨月归零** —— 先把所有频道 `fail_streak` 清零, 每月的黑名单从零重算。
   运营商本月新开通/修复的频道, 下个周期一定能自己回来。
2. **月内双轮** —— 第 1 轮全量; 间隔 30 秒后, 第 2 轮**只复核第 1 轮判 dead 的频道**。
   两轮都 dead 才让 `fail_streak` 达到 2 而被排除 → **单轮抖动不会造成误杀**。

### 每 6 小时的快速回捞

`iptv_refresh.sh` 里有一行:

```
python3 /etc/iptv_probe.py --only-excluded
```

它**只复测当前已被排除的频道, 且只做转正、不做降级**。
所以它不可能造成新的误杀, 而被误判的频道也不必干等一个月 —— 一旦恢复信号立刻回到列表。

### 白名单

名称匹配 `CCTV 1-17` 或命中 `KEEP_KEYWORDS`(默认 `卫视`)的频道,
即使探测失败也保留, 不参与排除。

> **CHC 等付费电影频道不进白名单。** 它们确实起播慢, 但那是"慢"不是"死" ——
> 靠上面的**多次重试 + `partial` 三态判定**就能自己通过, 不需要靠白名单兜。
> 真正长期没信号的付费频道, 就应该和别的频道一样被排除掉。
>
> 要给自己的频道开豁免, 改 `iptv.conf` 的 `KEEP_KEYWORDS`(逗号分隔)即可。

### 探测出的无信号典型

中国体育1-3、华西证券、天翼高清1-12、付费频道、各类 PIP(画中画)频道、
陕西农林PIP、CCTV-5+(平台未下发组播地址) 等。这些是**真没有信号**, 不是误判。

### 手工复测某个可疑频道

```sh
printf 'CCTV-1高清\nCHC家庭影院\n你的可疑频道名\n' > /tmp/names.txt
python3 -u /etc/iptv_probe.py --names-file /tmp/names.txt --dry-run
```

**务必夹带 2-3 个已知正常频道做对照**, 否则无法判断是探测方法坏了还是频道真死。

---

## 七、常见问题

| 现象 | 原因/排查 |
|------|-----------|
| `认证失败: 未获取到UserToken` | 账号/密钥/STBID 错误; `iptv` 口未获取IP; 网段路由不对 |
| 无频道 | 抓包对比 frameset_builder 参数; 门户组号 `USER_GROUP` 不对 |
| 直播花屏/卡顿 | 组播网段路由缺失: 检查 `ip route` 里 182.146.x 走 iptv 口 |
| 回看不出来 | `TS_SERVER`/`TS_VENDOR` 不对; 播放器不支持 catchup 格式 |
| 某频道一直消失 | 被健康探测排除了: 看 `/www/iptv_bad.txt`; 若确认它其实有信号, 直接跑 `python3 /etc/iptv_probe.py --only-excluded` 复测, 有流就立刻转正(不必等下次月度任务) |
| 某频道被误杀想做长期豁免 | 把关键词加进 `iptv.conf` 的 `KEEP_KEYWORDS`, 再跑一次 `/etc/iptv_auth.sh --probe` |
| 换台还是慢 | FCC 是否在 m3u URL 上(看 `?fcc=`); 播放器缓冲策略也影响起播 |
| 节目单空白 | EPG 网段路由断; 认证频率过高被限流 |

排查日志: `tail -f /tmp/iptv_epg.log`, `logread | grep rtp2httpd`

---

## 八、安全提醒

- `AUTHENTICATOR` / `STBID` 相当于机顶盒身份, **不要外传**。
- `chmod 600 /etc/iptv.conf`。
- 播放器接入仅限局域网, 勿把路由器直接暴露公网。