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
2. 转发仅在所有规则就绪后开启（`ip_forward=1`）；启动失败时保持关闭并让管理页可用。
3. 放行判定以**当前**白名单为准：活动记录里的“允许/未放行”是当时的判定，
   另附 `allowed_now` 表示现在是否在白名单内。

## DNS 策略

- 只服务配置网段内的客户端；A/AAAA 查询按租约应答，租约到期自动回收。
- 命中白名单的域名解析为本机地址（用户手册入口），其余按配置返回 NXDOMAIN 或转发上游。
- 应答前重新校验当前授权，避免缓存窗口内越权。
- 记录“解析出的 IP → 域名”，网络活动据此反查域名。

## 网络活动采集

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
- 安装：直接用 `POST /manage:upload?filename=<文件名>` 把 ELF 上传进 pldmgr（落到
  `local/<slug>/`），再从 `/list_payloads` 取回真实路径；需要运行则 `loadpayload:<path>`。
  不依赖 pldmgr 自己去拉取自定义仓库（实测它的仓库缓存只包含默认源）。
- pldmgr 不可用时回退 elfldr 的 `?uri=` 投递。
- `/list_payloads` 的真实响应是 `{self, items:[{slot,path,filename,kind,origin}]}`；
  同时兼容旧版 `{payloads:[路径]}` 结构。
- 自动加载：`AUTOLOAD_LIST` 是有序序列，`local/<槽位>`（取自 `/list_payloads` 的 `slot`）表示加载插件，
  `!<毫秒>`（0–3600000）表示等待后再加载下一项；`AUTOLOAD_DELAY`（0–3600 秒）是开始前的倒计时。
  管理页把每段等待归到前一个插件上编辑，保存时原样还原为 pldmgr 格式；`/autoload_status` 的 `outcomes`
  按插件顺序给出上次运行结果（s/f/k），仅在其 `list` 与当前配置一致时显示。
- “运行中”只统计与已装插件同名的进程；`/processes_list` 里的其它系统进程单独计数。

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

## 文件代理

浏览器受 CSP `default-src 'self'` 限制不能直连 PS5，文件接口也不发 CORS 头，因此走 `/api/files/*` 代理：

- 上传/下载/整目录 tar 全部 64 KiB 分块转发，服务端不落盘；
- 路径白名单：`/data`、`/mnt/ext<数字>`、`/mnt/usb<数字>`，拒绝 `.`/`..`/相对路径；
- 只代理配置网段内地址；写操作要会话 + `X-CSRF-Token`；
- 选择的文件主机持久化到 `console.json`（0600，原子写）。

## 会话与登录

- 登录后写入 `ps5_session` Cookie：`HttpOnly; Secure; SameSite=Lax; Max-Age=30 天`。
  用 Lax 是为了让「从书签/其它站点进入或刷新」也能保持登录（写操作仍需 `X-CSRF-Token`，不依赖 Cookie 的 SameSite 防护）。
- 会话在服务端**滑动续期**：每次带 Cookie 的请求把有效期推到「现在 + 30 天」，单会话上限 90 天。
- 会话令牌以 **SHA-256 哈希**形式保存在 `sessions.json`（0600，原子写），
  因此 `systemctl restart` 后仍然保持登录；登出或过期会即时清除。

## 滚动与长列表

表格不再用固定高度的内部滚动框（旧版 `max-height: 420–480px`）：滚轮在框内到底后容易误以为页面到底，
且框下方内容会被裁在视口之外。现在长表格随页面滚动，表头 `position: sticky`，页面底部保留 64px 留白。

## 前端

- 单页、hash 路由（`#/overview` `#/policy` `#/activity` `#/ps5` `#/files` `#/repository` `#/settings`）。
- 无内联脚本/样式，无外部资源；服务端字符串一律 `textContent`。
- 8 秒轮询受保护：不覆盖未保存的表单、打开的弹窗、进行中的上传与已选中的行。
