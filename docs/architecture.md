# 架构与实现要点

## 进程与端口

| 单元 | 端口 | 职责 |
| --- | --- | --- |
| `ps5-jb-server` | 53 udp/tcp、8443 tcp | 策略 DNS、管理页与 API、nftables 策略、网络活动采集 |
| `ps5-jb-host` | 443、80 | 用户手册站点（自签证书，CN/SAN 为 `manuals.playstation.net` 等） |
| `ps5-jb-repo` | 8080 | 只读插件仓库 `/repo.json` 与 `/payloads/<file>` |

三者都以 root 运行；单元带 `NoNewPrivileges` / `ProtectSystem=strict` / 最小 `ReadWritePaths`。
`ps5-jb-server` 的 `ExecStopPost` 会强制 `ip_forward=0`，避免 nftables 停止时意外放行。

## 策略执行顺序

1. 管理页保存配置 → 校验 → 渲染 nftables → `nft -c` 语法检查 → 原子应用（先建新表再切换），
   失败则恢复上一版并保留诊断信息。
2. 规则只匹配一块网卡（进出同一接口，单网卡网关）。网卡名取 `[network] interface`，
   留空时每次启动探测：先找持有 `host_ip` 的网卡，再退到默认路由的出口网卡，最后退到 `eth0`。
   安装器的基线规则与 `doctor` 用同一个探测函数（`settings.detect_interface`）。
3. 转发仅在所有规则就绪后开启（`ip_forward=1`）；启动失败时保持关闭并让管理页可用。
4. 放行判定以**当前**白名单为准：活动记录里的“允许/未放行”是当时的判定，
   另附 `allowed_now` 表示现在是否在白名单内。

## DNS 策略

- 只服务配置网段内的客户端；A/AAAA 查询按租约应答，租约到期自动回收。
- 命中白名单的域名解析为本机地址（用户手册入口），其余按配置返回 NXDOMAIN 或转发上游。
- 应答前重新校验当前授权，避免缓存窗口内越权。
- 上游查询不持策略锁：内置域名、缓存命中和拒绝路径不必等慢上游。持锁时取策略快照与代号
  （`generation`，每次策略变更 +1），放锁问上游，再持锁授权；代号变了就按新策略重判，
  已被移出白名单的域名返回 NXDOMAIN，不授权也不写缓存。同名并发查询只发一次上游请求，
  后到者最多等 4 秒后改走缓存或自己查。
- 动态地址增量写入：每次应答只把本次涉及的地址用一条 `nft -f -` 事务加进 `dynamic_v4`；
  已在内核里、需要更长超时的地址在同一事务里「加 1 秒 → 删除 → 按新超时加回」
  （Debian 12 的 nft 1.0.6 不能直接改元素超时），中间没有放行空窗。增量失败（表被清掉等）
  退回整表替换并记 WARNING，整表也失败才返回 SERVFAIL。
- 策略变更（保存、回退、放行）仍整表替换，规则计数器会归零，这是可接受的代价。
- 记录“解析出的 IP → 域名”，网络活动据此反查域名。

## 网络活动采集

- 网关连接来自 `conntrack -L`（软件包 `conntrack`）。读取失败只影响网关连接，DNS 查询照常采集，错误显示在活动页并在恢复后清除。

容器内没有 `/dev/kmsg`，nftables `log` 不可读，因此采集方式为**每 2 秒轮询 `conntrack -L`**：

- 只保留源在本网段、目的在网段外的新连接，按五元组去重（默认 300 秒窗口）；
- 上限 500 条，重启清空；
- 标签（命名/分类）与 `allowed_now` 在**读取时**套用，因此改名对历史记录立即生效。

## PS5 控制通道

| 组件 | 端口 | 协议 |
| --- | --- | --- |
| Payload Manager `pldmgr` | 8084 | HTTP；变更请求需 `X-PLDMGR-Request: 1`（值必须恰为 `1`，无密钥） |
| `elfldr` | 9021 | TCP 原始 ELF，或 `GET /?uri=<url>` 让主机自行下载运行 |

- 发现：对本网段并发 TCP 探测三个端口（每次探测一个地址的三个端口，限制线程数）。
- 接入：`POST /sources_add?url=<本机仓库>` + `repository_refresh`，幂等。
- 安装：先用只读 `GET /manage:check?filename=<文件名>` 取得 pldmgr 的本地槽位。槽位存在时使用
  `POST /manage:upload?filename=<文件名>&mode=replace`，不存在时用 `mode=new`；不先删除旧文件。
  pldmgr 会把版本/平台后缀归一，例如 `zftpd.elf` 与 `zftpd-ps5-v1.7.0.elf` 都落到 `local/zftpd`。
  上传后从 `/list_payloads` 按本次目标槽位及精确新文件名核实路径，避免选中仓库槽位中的同名旧副本；需要运行则 `loadpayload:<path>`。
  检查接口仅在明确返回 404 时按旧协议尝试新建；其它检查错误直接停止，不盲目覆盖。
  不依赖 pldmgr 自己去拉取自定义仓库（实测它的仓库缓存只包含默认源）。
- pldmgr 不可用且用户明确选择运行时可回退 elfldr 的 `?uri=` 投递；明确 HTTP 拒绝或上传后无法验证时不悄悄再次投递。
- 替换只更新 pldmgr 保存的文件，不终止已运行的旧进程，也不重写自动加载配置。文件服务等常驻插件请先等传输结束，
  使用「安装」保存新版后，再按插件要求停止旧进程/重启 PS5，避免同时运行两个版本。
- 本地仓库上传表单会从文件名识别名称与版本；精确匹配仓库文件时可复用已知元数据，手动编辑优先，无法可靠识别的作者/分类留空。
- `/list_payloads` 的真实响应是 `{self, items:[{slot,path,filename,kind,origin}]}`；
  同时兼容旧版 `{payloads:[路径]}` 结构。
- 自动加载：`AUTOLOAD_LIST` 是有序序列，`local/<槽位>`（取自 `/list_payloads` 的 `slot`）表示加载插件，
  `!<毫秒>`（0–3600000）表示等待后再加载下一项；`AUTOLOAD_DELAY`（0–3600 秒）是开始前的倒计时。
  管理页把每段等待归到前一个插件上编辑，保存时原样还原为 pldmgr 格式；`/autoload_status` 的 `outcomes`
  按插件顺序给出上次运行结果（s/f/k），仅在其 `list` 与当前配置一致时显示。
- “运行中”只统计与已装插件同名的进程；`/processes_list` 里的其它系统进程单独计数。
- 版本比对：仓库插件的槽位名问 `/manage:check`（按主机缓存，单次轮询最多 3 秒，旧版 pldmgr 用文件名猜测），对上主机
  `local/<槽位>` 里的文件后比较文件名中的版本号（忽略 ps4/ps5 平台字样）；本服务装过的以 `<data>/ps5-installs.json`
  里记录的 SHA-256 为准。`POST /api/ps5/upgrade`：旧版在运行就先 `process_kill`，再按上面的槽位替换上传并启动新版；
  未运行只替换文件。安装 / 更新的阶段与上传字节由 `GET /api/ps5/progress?ip=` 提供。

## 电源（唤醒 / 待机）

- 状态：DDP（UDP 9302）`SRCH * HTTP/1.1` + `device-discovery-protocol-version:00030010`，回复首行
  `HTTP/1.1 200 Ok`（开机）或 `620 Server Standby`（待机）；无回复视为关机或未开启遥控游玩。
  扫描时对子网广播，`host-name` / `system-version` 用于显示主机名与固件。
- 唤醒必须是 wake-login：DDP WAKEUP 后主机处于“等待遥控游玩客户端”状态，5 分钟没有会话就以
  `RemotePlayConnectTimeout` 退回待机。会话与登录由 PS5Upload 引擎实现（独立进程，GPL-3.0），
  本服务 `server/power.py` 只在 127.0.0.1 调用它的 `/api/ps5/power/{pair,wake-login,control}`，
  唤醒在后台线程执行，期间用 DDP 报告“正在启动 / 正在登录”，页面轮询 `/api/ps5/power`。
- 进入待机：引擎经 PS5 上 ps5upload 插件（AVA1 9120）调用 `sceSystemStateMgrEnterStandby`。
- 配对：主机开机且插件在跑时由插件提供 PSN 账号并完成 PIN 握手（免 PIN）；否则用链接装置 PIN + 账号 ID。
  密钥由引擎封存在 `/var/lib/private/ps5upload-engine`，本服务只读取“哪些主机已配对”。
- 隔离：引擎绑定 0.0.0.0 并自行拒绝非回环请求；`ps5upload-engine.service` 以 DynamicUser 运行，
  启动前加载 nft 表 `inet ps5upload_guard` 丢弃非 `lo` 的 19113 连接。
- pldmgr 的 `/shutdown` 只退出 pldmgr 自身，不是关机。

## 插件仓库的上游更新与历史版本

- 上游索引：`[repo] upstream`（默认 `https://itsplk.github.io/ps5-payloads-mirror/payloads_v2.json`，即 Payload Manager
  内置的 Official Repository）。只在管理员点「检查更新」时抓取，结果缓存到 `<data>/plugin-upstream.json`；失败保留上次结果。
  只接受 https 地址、文件名合法且带 64 位 SHA-256 的条目。
- 匹配（`plugin_updates.match_upstream`）：条目里记过的 `upstream` 名 → 文件名或 SHA-256 完全相同 → 文件名形如
  `<上游名>[_- ]<版本或 ps5>`（最长名字优先，`ftpsrv-drakmor` 不会被认成 `ftpsrv`）→ 名称或 id 相同。
  状态：校验和相同为 `current`；本地版本号的数字序列更大为 `local_newer`（不提示）；其余为 `available`。
- 更新：下载上限同上传上限（64 MiB），SHA-256 必须与索引一致才交给 `RepoManager.upgrade`；同一时间只允许一个更新。
- 历史：`RepoManager` 在发布新字节（上游更新、手动「替换」、回退）前把当前文件复制到 `versions/<sha256>`，记录在
  `versions/index.json`（按条目 `id` 分组，最近 3 个）。仓库服务只提供 `repo.json` 与 `payloads/` 里登记的文件，
  `versions/` 不可达。回退前重新校验 SHA-256；不再被引用的旧文件随即删除。
- 安装器重跑（`scripts/fetch_payloads.py`）合并已有 `repo.json`，不会撤销这些操作。

## 文件代理

浏览器受 CSP `default-src 'self'` 限制不能直连 PS5，文件接口也不发 CORS 头，因此走 `/api/files/*` 代理：

- 上传/下载/整目录 tar 全部 64 KiB 分块转发，服务端不落盘；
- 路径白名单：`/data`、`/mnt/ext<数字>`、`/mnt/usb<数字>`，拒绝 `.`/`..`/相对路径；
- 只代理配置网段内地址；写操作要会话 + `X-CSRF-Token`；
- 选择的文件主机持久化到 `console.json`（0600，原子写）。
- NAS 传输与解压任务列表写在 `transfers.json`，已发现的 PS5 写在 `hosts.json`（都是 0600，原子写），服务重启后读回；
  单文件网络错误自动重试，PS5 走 FTP 且支持 `REST STREAM` 时断点续传，细节见 [文件管理](file-manager.md)。

## 会话与登录

- 登录后写入 `ps5_session` Cookie：`HttpOnly; Secure; SameSite=Lax; Max-Age=30 天`。
  用 Lax 是为了让「从书签/其它站点进入或刷新」也能保持登录（写操作仍需 `X-CSRF-Token`，不依赖 Cookie 的 SameSite 防护）。
- 会话在服务端**滑动续期**：每次带 Cookie 的请求把有效期推到「现在 + 30 天」，单会话上限 90 天。
- 会话令牌以 **SHA-256 哈希**形式保存在 `sessions.json`（0600，原子写），
  因此 `systemctl restart` 后仍然保持登录；登出或过期会即时清除。

## 管理页 HTTP

- 管理接口按路由表分发（`server.py` 的 `GET_ROUTES` / `POST_ROUTES` 加前缀表）：会话、CSRF、JSON 请求体、
  升级期间拒绝写操作（`tracked`）和异常到状态码的映射都在分发器里，新接口只需登记一行和一个小函数。
- HTTP/1.1 长连接，空闲 15 秒断开；管理服务最多 64 个并发连接（空闲连接也占一个）。每个响应都带 `Content-Length`；
  请求体没读完就应答（401、413、升级中 409 等）、或下载长度未知时，响应带 `Connection: close`。
- 前端文件带 `ETag`（内容 SHA-256 前 16 位）与 `Cache-Control: no-cache`，再次加载命中则返回 304；
  浏览器支持 gzip 时发送内存里预压缩好的副本。接口响应仍是 `no-store`，超过 1 KiB 的 JSON 按需 gzip，下载不压缩。

## 自检

`server/selfcheck.py`。`POST /api/selfcheck`（会话 + CSRF，因为会主动发探测）跑一遍，`GET /api/selfcheck` 取最近一次结果
（只在内存里，重启后为空）；导出日志的诊断摘要附上结果摘要。每项在线程池里并发，单项超时 3 秒，整体不超过 10 秒；
单项抛异常或超时只把自己标为「异常」。同一时间只跑一次，第二个请求得到 409。

| 项 | 怎么判断 |
| --- | --- |
| DNS 劫持 | 向本机 DNS 查询每个内置域名，应答必须是本机地址 |
| 证书与用户手册站点 | 以 SNI `manuals.playstation.net` 连本机 443，读证书 SAN 与到期时间；读 `admin.crt` 到期时间；30 天内到期为「注意」 |
| 插件仓库 | 不经代理 `GET http://<本机>:<repo>/repo.json`，并列出缺文件的条目 |
| 网关 | `ip_forward`、`ps5guard` 表、网关错误、网卡存在且持有本机地址、IPv6 转发关闭 |
| 上游 DNS | 向当前策略的上游查询 `example.com` |
| conntrack | `conntrack -C` 能否执行 |
| PS5 网络指向 | 每台已知 PS5：最近的 DNS 记录里有没有它（主 DNS 指向本机）、连接表里有没有它的出网连接（默认网关指向本机） |
| IPv6 绕过风险 | `ip -6 route show default` 非空时提示「可能」绕过（启发式） |
| 可选组件 | 解压引擎、unrar 引擎、SMB、电源引擎是否可用（未装为「跳过」并给出安装参数），代理配置是否有效 |
| 资源与服务 | 数据目录剩余空间（低于 200 MB 提示）、日志目录可写、三个 systemd 服务状态 |

结果里只有状态、说明和建议，代理地址按 `netproxy.masked()` 隐去账号。`install.sh doctor` 复用其中不依赖服务运行的四项
（conntrack、IPv6、可选组件、资源），在原有输出后追加几行。

## 通知

`server/notify.py`。渠道存在 `<data>/notify.json`（0600，原子写），最多 10 个，类型为通用 Webhook（POST JSON）、Bark
（`POST <server>/push`）、ntfy（`POST <server>/` JSON 发布，可带 Bearer 令牌）、Telegram（`sendMessage`）。
Webhook 地址、Bark 设备密钥、ntfy 主题与令牌、Telegram 机器人令牌用 NAS 的 `SecretBox` 加密，附加数据为
`ps5jb-nas:notify:<渠道 ID>`，与 NAS 账号共用 `nas.key`（所以备份时一起处理）；接口只返回 `has_secret`。

- 事件：`transfer.finished/failed`、`extract.finished/failed`（`TransferManager` 每个任务结束时通知监听者一次，取消不通知）、
  `update.finished/failed`、`gateway.error`；`package.*`、`backup.*` 预留给 PKG 安装与 PS5 备份。每个渠道勾选订阅。
- 发送：有界队列（100 条，满了丢最旧的）+ 一个后台线程，绝不阻塞传输线程。每次 5 秒超时，网络错误重试 2 次，
  4xx（除 408/429）不重试；同一事件键 60 秒内只发一次。只允许 http/https，拒绝改变协议的重定向。
- 出口：目标解析到私有地址时直连，否则走 `netproxy.opener()`；渠道可关掉代理。日志只写渠道名、类型和失败类型
  （`HTTP 401`、连接失败、超时），不写 URL（Telegram 的令牌就在 URL 里）。
- 升级结果：后台每 15 秒看一次升级状态，任务到达 `done` / `failed` / `rolled_back` 时通知一次，已通知的任务号记在
  `<data>/notify-state.json`；一小时前就结束的结果只记下、不补发。网关错误在 `gateway_error` 由空变为非空时通知。
- 接口：`GET /api/notify`、`POST /api/notify/{save,delete,test}`。测试直接发送一次（不排队、不重试），失败返回 502 和失败类型。

## 备份与恢复

`server/backup.py`。备份是一个 `tar.gz`，成员只能是固定的几种名字：

| 成员 | 内容 |
| --- | --- |
| `policy.json` | 当前生效的访问策略 |
| `labels.json` / `console.json` / `hosts.json` | 活动标签、选中的文件主机、已知 PS5 |
| `notify.json` | 通知渠道（令牌仍是密文） |
| `nas.json` | NAS 连接（账号仍是密文）；`nas.key` 只在勾选「包含密钥」时加入 |
| `repo/repo.json`、`repo/versions/index.json` | 插件仓库目录与历史版本记录 |
| `repo/payloads/*`、`repo/versions/<sha256>` | 勾选「包含插件文件」时加入 |
| `manifest.json` | 格式版本、服务版本、时间、每个成员的大小和 SHA-256 |

管理密码、会话、TLS 证书、活动历史、PS5Upload 配对密钥都不进备份。

- 含密钥的备份整体加密成 `.ps5bak`：文件头 `PS5JBBK1` + 版本 + scrypt 参数（N=2^15、r=8、p=1）+ 16 字节盐 + 12 字节 nonce，
  后面是 AES-256-GCM 密文和 16 字节标签，文件头作为附加认证数据。读取时只接受 N 在 2^14–2^17、r=8、p=1，
  防止构造的文件让 scrypt 吃光内存。口令至少 10 位，不落盘，恢复时用 URL 编码放在 `X-Backup-Passphrase` 头里。
- `GET /api/backup?payloads=0|1` 下载不含密钥的备份，`POST /api/backup {payloads, include_key, passphrase}` 下载含密钥的。
  归档先写到 `<data>/backup-tmp/`（0700，文件 0600），带 `Content-Length` 发出后删除；服务启动时清掉残留。
- `POST /api/backup/restore`：原始请求体，上限是仓库上传上限的 8 倍；有传输、解压或复制任务时 409，升级时 409。
  先整体校验：只认白名单里的普通文件名（拒绝目录、链接、`..`）、每个成员与清单的大小和 SHA-256 一致、清单与实际成员一一对应、
  格式版本不高于本服务、解开后总大小不超过 4 GB。校验不过就什么都不改。
- 校验通过后逐项恢复，每项独立成败并返回「已恢复 / 已跳过 / 失败」：
  - 策略走 `engine.change_config()`（校验、写 nftables、原策略成为「上一版」），失败时原策略不变；
  - 标签、文件主机、已知主机整体替换；
  - NAS 连接整体替换。带密钥时连同密钥写回（替换 `nas.key`），不带密钥时备份里的密文不导入，
    本机已有的同 ID 连接保留现有账号，其余连接的账号留空待重填；
  - 插件仓库按条目 `id` 合并，只新增当前没有、文件名也不冲突、且备份里有插件文件的条目，文件按清单和条目的 SHA-256 校验，
    地址改写成本机仓库地址；不删除、不覆盖现有条目。

## 滚动与长列表

2026-11-11 起按用户要求改回「组件内滚动」：内容超高的组件不再把整页撑高。为避开旧版固定 `max-height: 420–480px`
的两个问题（框下方内容被裁到视口外、滚轮到框底误以为页面到底），高度改为跟随视口
（桌面表格 `max(320px, 100dvh - 300px)`，折叠卡片 / 自动加载列表 `min(46dvh, …)`），并且不设
`overscroll-behavior: contain`，滚到框底后滚轮继续带动页面；表头在框内 `position: sticky`。
手机与平板竖屏（≤1023px，文件表 ≤767px）的表格仍转成随页面滚动的列表。只有日志查看框固定高度并截断滚动链。

## 前端

- 单页、hash 路由（`#/overview` `#/policy` `#/activity` `#/ps5` `#/files` `#/repository` `#/settings`）。
- 无内联脚本/样式，无外部资源；服务端字符串一律 `textContent`。
- 8 秒轮询受保护：不覆盖未保存的表单、打开的弹窗、进行中的上传与已选中的行。
