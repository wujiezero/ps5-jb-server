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
- 自动加载：读改写 `AUTOLOAD_ENABLED` / `AUTOLOAD_LIST`（文件名为 pldmgr 内的文件名）。
- “运行中”只统计与已装插件同名的进程；`/processes_list` 里的其它系统进程单独计数。

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
