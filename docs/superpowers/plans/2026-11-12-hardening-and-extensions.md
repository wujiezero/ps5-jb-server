# 加固与拓展 Implementation Plan

> **给执行者：** 本计划按「里程碑 → 任务」组织，任务之间有依赖，请按顺序做。每个任务独立提交，步骤用 `- [ ]` 勾选。
> 行号以提交 `08d1280`（v1.9.0）为准，动手前先重新定位。
> 本计划只改仓库和测试：**不打 tag、不 push、不部署到真机**。标了「需真机验证」的条目，代码和单测做完后在任务末尾留下验证清单，由用户在自己的 PS5 / LXC 上确认。

**Goal:** 修掉 4 个已确认的问题，完成 5 项工程改进，新增 7 项功能，分 5 个版本交付。

**Tech Stack:** Python 3.11 标准库 + dnspython + cryptography（服务端，`unittest`），原生 HTML/CSS/JS（前端，`node --test`）。不新增必需的系统软件包。

---

## 范围对照

| 用户编号 | 内容 | 任务 | 版本 |
| --- | --- | --- | --- |
| 问题 1 | 防火墙网卡名写死 `eth0` | A1 | v1.9.1 |
| 问题 2 | DNS 查询被策略锁串行化 | A2 | v1.9.1 |
| 问题 3 | 每次解析整表重建 nftables | A3 | v1.9.1 |
| 问题 4 | 测试只在打 tag 时跑、README 数字过期 | A4 | v1.9.1 |
| 改进 4（前半） | 升级器放行新的前端文件（桥接） | A5 | v1.9.1 |
| 改进 5 | 路由样板收成路由表 | B1 | v1.10.0 |
| 改进 1 | 管理页长连接、ETag、gzip | B2 | v1.10.0 |
| 改进 2 | 传输队列与已发现主机落盘 | B3 | v1.10.0 |
| 改进 3 | 传输自动重试与断点续传 | B4 | v1.10.0 |
| 拓展 7 | 配置备份与恢复 | C1 | v1.11.0 |
| 拓展 10 | 页面自检 | C2 | v1.11.0 |
| 拓展 5 | 完成通知 | C3 | v1.11.0 |
| 拓展 8 | 活动历史与统计 | C4 | v1.11.0 |
| 拓展 1 | PKG 安装与金手指导入 | D1 | v1.12.0 |
| 拓展 2 | 游戏库视图 | D2 | v1.12.0 |
| 拓展 9 | PS5 → NAS 反向备份 | E1 | v1.13.0 |
| 改进 4（后半） | 前端拆分文件 | E2 | v1.13.0 |

不在范围内：策略预设与临时放行、按设备区分策略、定时检查更新、SSE 推送、ES module 化、多语言。

## 全局约束（每个任务都适用）

1. **网页升级器的文件白名单。** 已安装的旧版 `update_worker.py` 会拒绝归档里白名单之外的文件（[server/update_worker.py:38](../../../server/update_worker.py)、`_stage()` 的 `unexpected runtime member`）。
   - 新增 `server/*.py` 模块没问题（按文件名模式放行，安装器和打包脚本都用 glob）。
   - 新增 `web/` 下的文件在 A5 发布之前**不允许**；A5 之后只允许平铺在 `web/` 下、名字匹配白名单正则的文件。
   - `web/index.html`、`web/app.js`、`web/style.css` 三个文件名永远保留（`REQUIRED_FILES`）。
   - 因此 C、D 阶段的新界面先写进 `web/app.js` 现有结构，E2 再统一拆分。
2. **网页升级不运行 apt、不改 systemd 单元、不做配置迁移。** 新功能缺可选依赖时要降级并在页面说明，不能让服务起不来。新配置项必须有默认值，旧 `config.toml` 不改也能跑。
3. **接口向后兼容。** 只加字段不改语义；新前端拿不到新字段时静默降级（参考 v1.8.0 `local_domains` 的做法）。
4. **前端规则。** 无内联脚本和 `style` 属性（CSP `style-src 'self'`），服务端字符串一律 `textContent`，新表单受 8 秒轮询保护，新卡片标题配「?」功能说明。
5. **安全基线。** 新的写接口走 `require_session(write=True)`；新落盘文件 0600 且原子写；密钥类字段用 `nas.SecretBox` 加密，接口只返回 `has_secret`；路径一律过现有白名单函数。
6. **测试。** 每个任务先写失败的测试再实现。跑法：
   ```sh
   python3 -m unittest discover -s tests
   node --check web/app.js
   node --test tests/ui-interactions.js
   ./scripts/check-secrets.sh
   ```
7. **提交。** 每个任务一个提交，中文提交信息，沿用现有前缀风格（如 `网关：…`、`页面：…`、`文档：…`）。
8. **每个里程碑收尾。** 更新 `server/version.py`、`CHANGELOG.md`、`docs/release-notes/vX.Y.Z.md`、`docs/releases.md` 里的「当前版本」、README 的功能表与配置表、`docs/architecture.md` 对应章节、`examples/config.toml`，单独一个 `发布 vX.Y.Z：…` 提交。到此为止，不打 tag。

---

## 里程碑 A：v1.9.1 修复

### Task A1：防火墙网卡名可配置并自动探测

**现状：** [server/server.py:197](../../../server/server.py) 的 `create_engine()` 创建 `Firewall` 时没传 `interface`，落到 [server/policy.py:103](../../../server/policy.py) 的默认值 `eth0`。安装器的 `interface_name()`（[installer/__main__.py:709](../../../installer/__main__.py)）只用于基线规则，服务启动后被覆盖。网卡不叫 `eth0` 的机器上所有转发规则都不匹配。

**Files:**
- Modify: `server/settings.py`、`server/server.py`、`installer/__main__.py`、`examples/config.toml`
- Test: `tests/test_server.py`、`tests/test_policy.py`、`tests/test_installer.py`

- [x] `Settings` 新增 `interface: str = ''`，来源 `[network] interface` / `PS5JB_INTERFACE`。
- [x] `settings.py` 新增 `detect_interface(host_ip, runner=subprocess.run)`：先在 `ip -4 -o addr show` 里找持有 `host_ip` 的网卡，再退到 `ip -4 route show default` 的出口网卡，最后退到 `eth0`。结果必须匹配 `Firewall` 已有的正则 `[a-zA-Z0-9_.:-]{1,15}`。配置留空时在 `Settings.from_mapping` 之后由 `main()` 调用（保持 `from_mapping` 无副作用，方便测试）。
- [x] `create_engine(store, host_ip, lan, firewall=None, interface=None)` 把网卡名传给 `Firewall`；`main()` 传入解析后的值并打一行 INFO 日志。
- [x] 安装器的 `interface_name()` 改为调用同一个函数，避免两套探测逻辑。`doctor` 打印网卡名。
- [x] `Application.state()` 的 `status` 加 `interface`；`diagnostic_report()` 加一行。前端概览的服务信息里显示（拿不到字段就不显示）。
- [x] 文档写明：仍然假设单网卡（进出同一接口）。

**测试：** 探测函数三条回退路径（注入假 runner）；配置值优先于探测；`create_engine(..., interface='ens18')` 渲染出的规则含 `iifname "ens18"`；非法网卡名被拒。

**验收：** 在网卡为 `ens18` 的假环境下，`Firewall.render()` 不再出现 `eth0`。**需真机验证：** 在一台 VM（非 LXC）上确认白名单域名可通。

---

### Task A2：DNS 上游查询移出策略锁

**现状：** `DNSPolicy.answer()` 在 `with self.store.lock:` 内调用上游（[server/dns_service.py:196-267](../../../server/dns_service.py)，超时 3 秒），所有查询串行。

**Files:**
- Modify: `server/dns_service.py`
- Test: `tests/test_dns.py`

- [x] `DNSPolicy` 增加 `self.generation = 0`，`change_config()` 成功后自增。
- [x] `answer()` 拆成三段：
  1. **持锁**：取配置快照和 `generation`，做白名单判断、查询类型判断、内置域名应答、缓存命中。这几条路径直接返回。
  2. **不持锁**：向上游发查询。
  3. **重新持锁**：若 `generation` 变了，用新配置重新判断该域名；不再允许则返回 NXDOMAIN 且不授权、不写缓存。通过后再写租约、缓存、`ip_domains`、日志。
- [x] 同名并发查询去重：用 `{name: threading.Event}` 记录进行中的查询，后到者等待先到者写入缓存后走缓存路径；等待上限 4 秒，超时自己查。
- [x] 保持现有语义：TTL 从收到上游应答开始算；租约上限 2048；出错返回 SERVFAIL 并记 `last_error`。

**测试：**
- 上游卡住时（resolver 阻塞在 `Event` 上），另一线程查询内置域名和已缓存域名立即返回。
- 上游等待期间白名单移除该域名 → 返回 NXDOMAIN，`firewall` 没有收到新租约。
- 两个线程同时查同一个新域名 → resolver 只被调用一次。
- 现有 `tests/test_dns.py` 全部保持通过。

---

### Task A3：动态地址增量写入 nftables

**现状：** 每个未命中缓存的应答都调 `Firewall.apply()`：3 次 `nft` 子进程，且脚本是「删表再建」（[server/policy.py:182-192](../../../server/policy.py)），规则计数器每次归零。

**Files:**
- Modify: `server/policy.py`、`server/dns_service.py`
- Test: `tests/test_policy.py`、`tests/test_dns.py`

- [x] 从 `render()` 抽出租约校验（公网单播 IPv4、整数 TTL、上限 2147483 秒）为 `_lease_elements(leases)`，`render()` 复用。
- [x] 新增 `Firewall.add_leases(leases, refresh=())`，用一次 `nft -f -` 提交一个原子脚本：
  ```
  delete element inet ps5guard dynamic_v4 { <refresh 中的地址> }   # 仅当 refresh 非空
  add element inet ps5guard dynamic_v4 { 1.2.3.4 timeout 30s, ... }
  ```
  `refresh` 是「内核里应当还在、需要延长超时」的地址。Debian 12 的 nft 1.0.6 不能直接更新已有元素的超时，所以先删再加；同一事务内没有放行空窗。
- [x] `DNSPolicy.answer()` 第三段只提交**本次应答涉及的地址**：新地址进 `add`，已有未过期租约且新过期时间更晚的进 `refresh`。
- [x] 增量提交失败（表被清空、元素已被内核回收导致 `delete` 报错等）时，回退到现有的整表 `apply(cfg, 全部租约)`，并记一条 WARNING。回退也失败才走现有的 SERVFAIL 路径。
- [x] `change_config()` 仍然整表替换（策略变更时计数器归零是可接受的，写进 `docs/architecture.md`）。

**测试：** 假 runner 记录 argv 与 stdin：首次应答只出现 `add element`、不出现 `delete table`；延长已有租约时出现 `delete element`；增量失败时退回整表脚本；非法租约地址被拒。

**验收：** 连续解析 10 个新域名，`nft` 调用次数为 10（原来 30）。**需真机验证：** Debian 12 LXC 上 `nft list set inet ps5guard dynamic_v4` 能看到地址和递减的超时，概览页计数器持续累加。

---

### Task A4：日常 CI 与 README

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `README.md`

- [x] `ci.yml`：`push`（main）和 `pull_request` 触发；Python 3.11（与 Debian 12 一致）、Node 22；步骤与 `release.yml` 的测试一致，另加 `python -m compileall -q server installer scripts`、`node --check web/*.js`、`./scripts/check-secrets.sh`、`python scripts/build_release.py --output /tmp/dist`。
- [x] 加 `ruff check --select E9,F63,F7,F82,F401,F811,F841 server installer scripts tests`（pyflakes 级别）。先在本地跑，修掉真问题；确属有意的用 `# noqa: Fxxx` 并写原因。不引入格式化工具。
- [x] README「开发」一节去掉写死的「227 项单测」，改为不带数字的说明，并补上 `ruff` 命令。

**验收：** 本地按 `ci.yml` 的命令逐条能跑通。

---

### Task A5：升级器放行新的前端文件（桥接）

**目的：** 为 E2 的前端拆分铺路。这一版只放宽白名单，仍然只发 3 个前端文件。越早发布，后面能直接网页升级的用户越多。

**Files:**
- Modify: `server/update_worker.py`、`scripts/build_release.py`、`installer/__main__.py`、`server/server.py`、`docs/releases.md`
- Test: `tests/test_update_worker.py`、`tests/test_releases.py`、`tests/test_installer.py`、`tests/test_server.py`

- [x] `update_worker._stage()`：除 `RUNTIME_FILES` 外，接受 `web/<name>`，其中 `<name>` 匹配 `[a-z0-9][a-z0-9._-]{0,63}\.(?:js|css|html)`，仅限平铺一层。`REQUIRED_FILES` 不变。
- [x] `build_release.py` 和安装器的 `install_files()` 改为按同一正则收集 `web/` 下的文件（把正则放进一个两边都能导入的位置，例如 `server/version.py` 旁的常量，安装器已经会 `sys.path.insert` 导入 `server/`）。
- [x] `AdminHandler.do_GET` 的静态文件表改为：路径去掉前导 `/` 后匹配同一正则且文件存在于 `static_dir` 才提供，`/` 仍映射 `index.html`。正则不含 `/`，无路径穿越。
- [x] `docs/releases.md`：说明白名单规则，以及「≤1.9.0 无法直接网页升级到拆分后的版本，需先 SSH 升级一次」。

**测试：** 接受 `web/extra.js`；拒绝 `web/sub/x.js`、`web/x.php`、`web/.hidden.js`、`web/X.JS`；发布包内容在只有 3 个文件时与原来逐字节一致；静态服务对不存在或不匹配的名字返回 404。

**里程碑 A 收尾：** 版本 `1.9.1`，按全局约束第 8 条提交。

---

## 里程碑 B：v1.10.0 基础改进

### Task B1：路由表（纯重构）

**目的：** 后面要加十几个接口，先把 `do_POST` 的长 if/elif 和 5 处重复的 `begin_operation`/`end_operation` 收拢。

**Files:**
- Modify: `server/server.py`、`server/api_console.py`
- Test: 现有 `tests/test_server.py`、`test_update_api.py`、`test_admin_extras.py`、`test_review_fixes.py`、`test_console_api.py`

- [x] 在 `server.py` 定义路由表：`{(method, path): Route(func, write=bool, tracked=bool, body='json'|'raw'|None)}`。`tracked=True` 的路由由分发器统一包 `begin_operation`/`end_operation`，并在升级进行中返回现有的 409 文案。
- [x] 把 `do_GET` / `do_POST` 里的分支逐个迁成小函数；异常到状态码的映射（`PermissionError`→403、`FileNotFoundError`→404、`FileExistsError`→409、`ValueError`→400）集中在分发器。
- [x] `ConsoleAPI.handle_get/handle_post` 同样改成字典分发，`_disabled()` 检查保留。
- [x] **行为必须完全不变**：状态码、错误文案、Cookie、`/cancel` 结尾的路由不计入 tracked 等细节逐条对照。本任务不改任何测试断言；若必须改测试才能通过，说明重构改了行为，回头修代码。

**验收：** 全部现有测试不改一行通过；`server.py` 里 `begin_operation` 只出现在分发器中。

---

### Task B2：管理页长连接、缓存与压缩

**现状：** `protocol_version = 'HTTP/1.0'`（[server/server.py:700](../../../server/server.py)），每次轮询重新握手；静态文件 `no-store`、不压缩。

**Files:**
- Modify: `server/server.py`、`server/api_console.py`
- Test: `tests/test_server.py`、`tests/test_tls.py`

- [x] 切到 `HTTP/1.1`。逐条保证长连接安全：
  - 所有响应都带 `Content-Length`；`_stream()` 在长度未知时设 `self.close_connection = True`。
  - 请求体没读完就应答的路径（401、413、400、升级中 409 等）设 `close_connection = True`。实现方式：在 handler 上记录「本次请求体是否已读完」，`send_body()` 里统一判断。
  - 空闲超时 15 秒；`AdminServer` 的并发槽从 32 提到 64（浏览器每个标签最多 6 条连接，空闲连接会占槽）。DNS 服务的槽数不动。
- [x] 静态文件：`ETag` 为内容 SHA-256 前 16 位（按 mtime+size 缓存），`Cache-Control: no-cache`，命中 `If-None-Match` 返回 304。API 响应仍是 `no-store`。
- [x] gzip：静态文件预压缩后缓存在内存；JSON 响应大于 1 KiB 且请求带 `Accept-Encoding: gzip` 时压缩（压缩级别 5）。下载与流式响应不压缩。加 `Vary: Accept-Encoding`。
- [x] `log_request` 把 304 也归入 DEBUG。

**测试：** 一条连接上连续两个请求都成功；未读完请求体的错误响应带 `Connection: close`；304；gzip 解压后内容一致；不带 `Accept-Encoding` 时不压缩；文件上传与下载在 1.1 下行为不变。

**验收：** `app.js` 第二次加载返回 304；首次传输体积下降到原来的三分之一以下。

---

### Task B3：传输队列与已发现主机落盘

**现状：** `TransferManager.jobs`（[server/nas.py:751](../../../server/nas.py)）和 `Discovery.cached`（[server/ps5.py:104](../../../server/ps5.py)）只在内存里。

**Files:**
- Modify: `server/nas.py`、`server/ps5.py`、`server/api_console.py`、`web/app.js`
- Test: `tests/test_nas.py`、`tests/test_ps5.py`、`tests/test_console_api.py`、`tests/ui-interactions.js`

- [x] `TransferManager` 增加 `state_path=<data>/transfers.json`：入队、状态变化、结束时原子写；进度更新最多每 5 秒写一次。只持久化对外可见字段，不含任何 NAS 凭据。
- [x] 启动时加载：非终态任务标为 `failed`，`error='服务重启时中断'`，新增字段 `interrupted: true`。解压任务记录暂存目录 `staging`，页面提示可手动清理。
- [x] unrar 任务跑在独立的 systemd 临时单元里，服务重启时它可能还活着。先读 `_execute_unrar()`（[server/nas.py:1104](../../../server/nas.py)）的状态文件机制：单元仍存活就重新接管监控，否则按中断处理。
- [x] 新接口 `POST /api/transfers/retry {id}`：用原参数重新入队，NAS 传输强制 `overwrite='skip'`，等效于文件级续传。只对 `failed` / `canceled` 的 NAS 传输与备份任务开放；解压任务不支持（需重新走预检）。
- [x] `Discovery` 增加 `<data>/hosts.json`：记录 `ip`、主机名、固件、`last_seen`。启动扫描时优先探测这些地址；扫描完成前页面把它们显示为「上次发现」。
- [x] 前端：任务卡片显示「已中断」和「重试」按钮。
- [x] `update_busy()` 把中断任务视为终态。

**测试：** 写入后重建 `TransferManager` 能读回；运行中的任务重启后标为中断；重试生成新任务且为 skip 模式；状态文件损坏时从空开始而不崩溃；主机记录读写与过期（30 天未见则丢弃）。

---

### Task B4：传输自动重试与断点续传

**现状：** `_copy_file()`（[server/nas.py:1185](../../../server/nas.py)）失败即删半截文件并计入失败，不重试。

**Files:**
- Modify: `server/nas.py`、`server/ftp_files.py`、`server/files.py`、`server/settings.py`
- Test: `tests/test_nas.py`、`tests/test_files.py`

**第一层：自动重试（必须做）**
- [x] 对连接类错误（`OSError`、`NasError`、`status is None` 的 `FileServiceError`）单文件最多重试 3 次，间隔 2 / 5 / 15 秒，等待期间响应取消。重试前回滚该文件已计入的 `bytes_done`。`ValueError`、404、409 不重试。
- [x] 任务新增 `retries` 计数，卡片上显示。次数可配 `[limits] transfer_retries`，默认 3，0 关闭。

**第二层：续传（按探测结果启用）**
- [ ] 第 0 步调研：确认 PS5 端 FTP 插件（zftpd）是否在 `FEAT` 里声明 `REST STREAM`。结论写进 `docs/file-manager.md`。
- [x] 源端：三个 NAS 客户端加 `open_at(path, offset)`（WebDAV `Range: bytes=N-` 且必须返回 206，否则视为不支持；FTP `REST`；SMB `lseek`）。
- [x] 目的端：`FtpFiles.supports_resume()` 读 `FEAT` 并缓存；`upload(..., offset=N)` 用 `storbinary(..., rest=N)`。HTTP 后端（`/fs/put`）不支持续传，只走第一层。
- [x] 续传流程：重试时 `stat` PS5 上半截文件得到 `N`（`0 < N < size`），源和目的都从 `N` 开始。传完后再 `stat` 一次：大小不等于期望值就删除并从头重传一次（防止服务器忽略 `REST`）。
- [x] 不做内容校验（两端都没有便宜的校验手段），在文档里写明。

**测试：** 中途断一次后重试成功且计数正确；不可重试错误直接失败；续传路径带上正确偏移；大小不符时退回全量重传；取消发生在退避等待期间能立即生效。

**需真机验证：** 传一个 10 GB 以上文件，中途重启 NAS 的服务或拔网线 10 秒，任务最终成功且 PS5 上文件大小正确。

**里程碑 B 收尾：** 版本 `1.10.0`。

---

## 里程碑 C：v1.11.0 运维类功能

### Task C1：配置备份与恢复

**Files:**
- Create: `server/backup.py`、`tests/test_backup.py`
- Modify: `server/server.py`、`web/index.html`、`web/app.js`、`web/style.css`

**备份内容**

| 内容 | 默认 | 说明 |
| --- | --- | --- |
| 访问策略（`config.json` 的 active） | 含 | |
| 活动标签 `labels.json` | 含 | |
| 文件主机选择 `console.json`、已知主机 `hosts.json` | 含 | |
| 通知配置 `notify.json`（C3 之后） | 含 | 密钥字段见下 |
| 插件仓库 `repo.json` + `versions/index.json` | 含 | |
| 插件二进制 `payloads/`、`versions/` | 可选 | 勾选「包含插件文件」 |
| NAS 连接 `nas.json` | 含 | 密文 |
| `nas.key` | 可选 | 勾选「包含密钥」时必须设置备份口令 |
| 管理密码、会话、TLS 证书、活动历史、PS5Upload 配对密钥 | 不含 | 写进界面说明 |

- [x] 格式：`tar.gz`，根目录 `manifest.json`（格式版本、服务版本、时间、内容清单、各文件 SHA-256）。
- [x] 勾选「包含密钥」时，整个归档用口令加密：scrypt 派生密钥 + AES-256-GCM（`cryptography` 已是必需依赖），文件头写魔数、盐和参数。口令至少 10 位，不落盘。
- [x] `GET /api/backup?payloads=0|1`（无密钥）与 `POST /api/backup`（带口令，JSON 请求体，返回二进制）。归档先写到 `<data>/backup-tmp/` 再带 `Content-Length` 发送，发送后删除；启动时清理该目录。
- [x] `POST /api/backup/restore`（原始请求体，上限 = 仓库上传上限 × 8，可带 `X-Backup-Passphrase`）：
  - 校验清单、每个成员的名字白名单和 SHA-256；格式版本更高则拒绝。
  - 策略走 `engine.change_config()`，这样会经过校验和 nft 应用，原策略成为「上一版」可回退。
  - 标签整体替换；NAS：带密钥则写回密钥与密文并重载 `NasStore`，不带密钥则只导入连接信息，账号留空待重填。
  - 仓库：按 `id` 合并，只新增缺失条目并校验文件 SHA-256，不删除现有条目。
  - 返回逐项结果（已恢复 / 已跳过 / 失败原因）。有传输或升级在进行时拒绝（`tracked=True`）。
- [x] 界面：设置页新卡片「备份与恢复」，恢复前弹确认框列出将被覆盖的项目。

**测试：** 往返一致；口令错误、归档被篡改、未知成员、路径穿越成员均被拒；不含密钥的恢复不产生可解密的 NAS 账号；恢复策略失败时原策略不变。

---

### Task C2：页面自检

**Files:**
- Create: `server/selfcheck.py`、`tests/test_selfcheck.py`
- Modify: `server/server.py`、`installer/__main__.py`、`web/index.html`、`web/app.js`

- [x] `selfcheck.run(app) -> [{id, title, status, detail, fix}]`，`status` 为 `ok` / `warn` / `fail` / `skip`。每项独立超时（2–3 秒），线程池并发，总时长不超过 10 秒，任何一项抛异常只影响自己。
- [x] 检查项：
  1. **DNS 劫持**：向本机 DNS 查询每个内置域名，应答应为本机地址。
  2. **用户手册站点**：以 SNI `manuals.playstation.net` 连本机 443，读证书 SAN 与到期时间；管理页证书同样检查到期（30 天内 `warn`）。
  3. **插件仓库**：`GET http://<本机>:<repo>/repo.json`，统计条目与缺失文件。
  4. **网关**：`ip_forward`、`ps5guard` 表存在、`gateway_error` 为空、A1 的网卡存在且持有本机地址、IPv6 转发关闭。
  5. **上游 DNS**：向上游查询 `example.com`。
  6. **conntrack** 可用。
  7. **每台已知 PS5 的指向**：最近的 DNS 日志里有没有它的查询（主 DNS 是否指向本机）；conntrack 里有没有它的出网连接（默认网关是否指向本机）。只有 DNS 没有网关时 `warn` 并给出设置指引。
  8. **IPv6 绕过风险**：本机能看到 IPv6 默认路由（`ip -6 route show default` 非空）时 `warn`：局域网有 IPv6 路由，PS5 可能绕过策略。这是启发式判断，文案要写成「可能」。
  9. **可选组件**：解压引擎、unrar 引擎、电源引擎、SMB、代理状态；未安装为 `skip` 并给出安装参数。
  10. **资源**：数据目录剩余空间（低于 200 MB `warn`）、日志目录可写、三个 systemd 单元状态。
- [x] `POST /api/selfcheck`（需会话与 CSRF，因为会主动发探测）。最近一次结果缓存在内存，`diagnostic_report()` 附上摘要，这样导出日志里也有。
- [x] 安装器 `doctor` 复用其中不依赖运行中服务的检查项，输出格式不变、只增不减。
- [x] 界面：设置页「自检」卡片，逐项显示状态、说明和修复建议。

**测试：** 每个检查项注入假依赖，覆盖 ok / fail / 超时；单项异常不影响其它项；结果里不含任何密钥或口令。

---

### Task C3：完成通知

**Files:**
- Create: `server/notify.py`、`tests/test_notify.py`
- Modify: `server/nas.py`、`server/server.py`、`server/updates.py`、`web/index.html`、`web/app.js`

- [x] 渠道类型：通用 `webhook`（POST JSON）、`bark`、`ntfy`、`telegram`。配置存 `<data>/notify.json`（0600）；令牌、带密钥的 URL 用 `SecretBox` 加密，绑定渠道 ID，接口只返回 `has_secret`。
- [x] 发送：独立后台线程 + 有界队列（100 条，满了丢最旧的），绝不阻塞传输线程。超时 5 秒，失败重试 2 次。同一事件键 60 秒内只发一次。只允许 `http` / `https`，不跟随跨协议重定向。
- [x] 出口：公网目标走 `netproxy.opener()`（Telegram 等需要代理），目标是私有地址时直连。每个渠道可单独关掉代理。
- [x] 事件（每个渠道可勾选订阅）：

  | 事件 | 触发点 |
  | --- | --- |
  | `transfer.finished` / `transfer.failed` | `TransferManager._run()` 的 `finally`，含 `done_with_warnings` |
  | `extract.finished` / `extract.failed` | 同上，`kind == 'extract'` |
  | `package.finished` / `package.failed` | D1 之后 |
  | `backup.finished` / `backup.failed` | E1 之后 |
  | `update.finished` / `update.failed` | 服务启动后读到升级状态从进行中变为终态 |
  | `gateway.error` | `gateway_error` 从空变为非空 |

- [x] 消息内容：标题 + 正文，含任务名、大小、用时、平均速度或错误原因。界面注明「文件名会随通知发到外部服务」。
- [x] 接口：`GET /api/notify`、`POST /api/notify/save`、`/delete`、`/test`。
- [x] 界面：设置页「通知」卡片，添加 / 编辑 / 测试 / 删除渠道。

**测试：** 假 opener 验证四种渠道的请求格式；去重窗口；队列满时不阻塞；密钥不出现在接口响应和日志里；发送失败不影响任务状态。

---

### Task C4：活动历史与统计

**Files:**
- Create: `server/history.py`、`tests/test_history.py`
- Modify: `server/activity.py`、`server/server.py`、`server/settings.py`、`web/index.html`、`web/app.js`、`web/style.css`

- [x] 存储：标准库 `sqlite3`，`<data>/activity.db`，WAL 模式，单连接加锁。表 `events(id, ts, kind, client, dst, port, proto, domain, verdict, local)`，索引 `ts`、`(verdict, ts)`、`domain`。名称、分类、`allowed_now` 仍在读取时套用，不入库。
- [x] 写入：`FlowObserver.poll_once()` 把本轮新事件（连接 + DNS）批量 `executemany`。写库失败只记日志，不影响内存环和实时页面。
- [x] 保留：`[activity] retention_days` 默认 30，行数上限 50 万，每小时清理一次。数据库损坏时改名留存并重建，服务不退出。
- [x] 启动时用最近 500 行预热内存环（顺带解决「重启清空」）。
- [x] 接口：
  - `GET /api/activity/history?from=&to=&verdict=&kind=&client=&q=&before_id=&limit=`，按 `id` 游标翻页，`limit` 上限 500。
  - `GET /api/activity/stats?days=7|30`：按天的允许 / 未放行数量、被拦截最多的目标 Top 20、允许最多的 Top 20、按客户端汇总。
  - `GET /api/activity/export?from=&to=`：CSV，最多 5 万行。
  - `POST /api/activity/history/clear`。
- [x] 界面：网络活动页加「实时 / 历史 / 统计」切换。统计页的 Top 拦截列表每行带「放行」按钮（复用 `/api/allow`）；按天柱状图用 `createElementNS` 画 SVG，尺寸用属性设置，不用内联 `style`。
- [x] 设置页加「清空活动历史」。C1 的备份默认不含此数据库。

**测试：** 写入与游标翻页；各筛选条件；统计聚合；保留期清理；损坏数据库的恢复；预热后内存环顺序正确。

**里程碑 C 收尾：** 版本 `1.11.0`。

---

## 里程碑 D：v1.12.0 游戏相关

### Task D1：PKG 安装与金手指导入

**现状：** [server/game_services.py](../../../server/game_services.py) 里 `package_install()`、`cheat_import()`、`parse_cheat_zip()`、`plan_cheat_import()` 已实现并有单测，但没有任何接口和界面调用；[server/api_console.py:320](../../../server/api_console.py) 的报错提到了不存在的「安装包流程」。`game_sources.plan_sources()` 已能识别 `package` 与同目录金手指 ZIP。

**关键未知（第 0 步，必须先做）**
- [x] 查清 PS5Upload 引擎（版本见 `components.json`）`/api/pkg/install` 的 `source` 支持哪些形式。现有代码只用了 `{'host_file': 本机绝对路径}`，而本服务的原则是不落盘，PKG 动辄几十 GB。查上游仓库对应 tag 的源码与文档，有运行中的引擎就实际探测。结论写进 `docs/file-manager.md`。
- [x] 按结论选方案，优先级从高到低：
  - **方案 A：引擎支持 URL 源。** 本服务新开一个只读端点，把 NAS 上的 PKG 以支持 `Range` 的 HTTP 流提供出去。地址带一次性随机令牌，只在任务期间有效，只接受引擎（回环）或目标 PS5 的来源地址。不占本机磁盘。
  - **方案 B：引擎支持 PS5 上的路径。** 先用现有 NAS 传输把 PKG 放到 `<存储>/.pkg-staging/`，引擎从主机路径安装，完成后删除。PS5 上需要约两倍空间，预检时算进去。
  - **方案 C：只支持 `host_file`。** 需要把 NAS 以 FUSE 只读挂载给以 DynamicUser 运行的引擎，涉及挂载命名空间和单元改动，而网页升级不能改单元。**遇到这种情况停下来，把调研结论报告给用户，不要自行实现。**

**Files:**
- Modify: `server/nas.py`、`server/api_console.py`、`server/game_services.py`、`web/index.html`、`web/app.js`
- Test: `tests/test_game_services.py`、`tests/test_game_sources.py`、`tests/test_nas.py`、`tests/test_console_api.py`、`tests/ui-interactions.js`

**PKG 安装**
- [x] `TransferManager` 新任务类型 `package`，阶段「准备 → 传输（方案 B）→ 安装 → 确认」。引擎返回的进度写进任务；`package_install()` 的 300 秒轮询上限改为可传入，按 PKG 大小估算（下限 10 分钟）。支持取消。
- [x] `may_not_launch` 映射为 `done_with_warnings`，冲突（引擎忙）映射为可重试的失败。
- [x] 新接口 `GET /api/nas/plan?id=&paths=`：返回 `plan_sources()` 的结果（类型、标题 ID 提示、同目录金手指），供界面预览。
- [x] 新接口 `POST /api/nas/install`：按类型分发——`archive` 走现有解压，`folder` 走传输 + 注册，`package` 走新任务。`/api/nas/extract` 保留不变；把它那条误导性的报错改为指向新入口。
- [x] 预检：`game_services.preflight(host)`（需要电源引擎和 PS5 上的 ps5upload 插件）；不可用时界面说明缺什么。

**金手指导入（不依赖引擎）**
- [x] 新接口 `POST /api/nas/cheats {id, path, title?, version?, host}`：从 NAS 读 ZIP 到内存（`parse_cheat_zip` 已限 16 MiB），得到文件布局后经 `FileProxy` 写到 `/data/etaHEN/cheats/<格式>/<文件名>`，逐级建目录。返回写入的文件和警告。
- [x] 新接口 `POST /api/files/cheats`：浏览器直接上传 ZIP（原始请求体，同样 16 MiB 上限），后续流程相同。
- [x] 解压安装对话框：`plan` 里有金手指 ZIP 时显示「同时导入金手指」勾选项，作为解压完成后的附加步骤；失败只把任务标为 `done_with_warnings`。
- [x] 完成提示写明：需要在 etaHEN 里重新加载金手指。

**界面**
- [x] NAS 列表：`.pkg` 行出现「安装 PKG」；名字含「金手指 / cheat / trainer」的 `.zip` 行出现「导入金手指」。
- [x] 任务卡片支持 `package` 类型的阶段显示。

**测试：** 假引擎覆盖成功、忙、超时、`may_not_launch`；`/api/nas/install` 三种类型的分发；金手指文件落到正确路径、路径穿越成员被忽略、SHN / MC4 不会被当成 JSON；方案 A 的令牌过期与来源地址限制（若采用）。

**需真机验证：** 装一个真实 PKG 并能启动；导入一份金手指后 etaHEN 能识别。

---

### Task D2：游戏库视图

**Files:**
- Create: `server/library.py`、`tests/test_library.py`
- Modify: `server/api_console.py`、`server/settings.py`、`web/index.html`、`web/app.js`、`web/style.css`、`tests/browser/console-smoke.js`

- [x] 扫描范围：对 `files.volumes()` 返回的每个卷，查看 `[ps5] game_dirs`（默认 `["games", "homebrew", "etaHEN/games"]`）下的子目录，以及卷根下名字是标题 ID 的目录。只列一层，不递归。
- [x] 判定为游戏：目录名含标题 ID，或存在 `sce_sys/param.json` / `param.sfo`。条目字段：标题 ID、名称（`TitleCatalog.lookup`，带 `folder` 参数补读 param）、路径、所在卷、平台（按前缀区分 PS4 / PS5）、修改时间、`size: null`。
- [x] 大小按需计算：`GET /api/library/size?path=` 调 `files.folder_size()`，结果按「路径 + 修改时间」缓存到 `<data>/library-cache.json`。FTP 下遍历很慢，一次只算一个，界面上点击才算。
- [x] 封面：`GET /api/library/icon?path=` 代理 `sce_sys/icon0.png`，路径过白名单，上限 2 MiB，校验 PNG 文件头，缓存到 `<data>/library-icons/`，带 ETag。
- [x] 接口：`GET /api/library?host=&refresh=`（结果缓存 60 秒）、`POST /api/library/register`（调 `game_services.register`，沿用它对路径的校验）。删除和跨卷移动复用现有 `/api/files/delete`、`/api/files/move` 与任务进度。
- [x] 界面：新增 `#/library`「游戏库」。需要同步修改 `SECTIONS`、`SECTION_META`、`views`、桌面侧栏、平板图标栏、手机「更多」菜单。卡片网格显示封面、名称、标题 ID、所在卷、大小；支持搜索、按卷筛选、按名称或大小排序。每张卡片的操作：打开所在文件夹、移动到其它存储、重新注册图标、删除（确认框显示大小）。
- [x] 界面注明：通过 PKG 安装到系统分区（`/user/app`）的游戏不在路径白名单内，这里看不到。

**测试：** 假 `FileProxy` 覆盖多卷、无 param 的目录、名字不规范的目录；大小缓存命中与失效；图标的路径校验与非 PNG 拒绝；文件服务不可用时返回可读错误而不是 500。

**里程碑 D 收尾：** 版本 `1.12.0`。

---

## 里程碑 E：v1.13.0 反向备份与前端拆分

### Task E1：PS5 → NAS 反向备份

**现状：** NAS 客户端只有 `list` / `stat` / `open` / `read_at`，没有写入。PS5 文件路径白名单只含 `/data`、`/mnt/ext*`、`/mnt/usb*`（[server/files.py:315](../../../server/files.py)）。

**Files:**
- Modify: `server/nas.py`、`server/files.py`、`server/ftp_files.py`、`server/api_console.py`、`server/settings.py`、`web/index.html`、`web/app.js`
- Test: `tests/test_nas.py`、`tests/test_files.py`、`tests/test_console_api.py`

**NAS 写入**
- [x] 三个客户端加 `mkdir(path)`、`write(path, chunks, size)`、`rename(src, dst)`、`delete(path)`：WebDAV 用 `MKCOL` / 流式 `PUT` / `MOVE` / `DELETE`；SMB 用 smbc 对应调用；FTP 用 `MKD` / `STOR` / `RNFR`+`RNTO` / `DELE`。
- [x] 写入先落到 `<名字>.part`，完成并核对大小后改名。
- [x] NAS 连接新增 `writable` 开关，默认关闭；关闭时所有写方法直接报错。对话框的「测试连接」在开启时追加一次建目录 / 写入 / 删除的往返测试。

**备份任务**
- [x] `TransferManager` 新任务类型 `backup`：遍历 PS5 目录 → 逐文件 `FileProxy.stream()` → NAS `write()`。进度、取消（删除 `.part`）、B4 的重试都沿用。
- [x] 增量：NAS 上已有同名且大小相同的文件则跳过。两种目标布局可选：镜像（固定目录，增量）或快照（`<目标>/<主机名或 IP>/<YYYYMMDD-HHMMSS>/`）。默认镜像。
- [x] 接口 `POST /api/nas/backup {id, dest, paths, mode, host}`；`/api/transfers/retry` 支持此类型。
- [x] 发 `backup.finished` / `backup.failed` 通知（C3）。

**只读附加根目录（存档与截图）**
- [ ] 第 0 步调研（需真机）：经 FTP 确认 PS5 上截图 / 录像与存档的实际路径（预期为 `/user/av_contents` 和 `/user/home/<用户>/savedata*`）以及 FTP 插件是否可读。结论写进 `docs/file-manager.md`。**未确认前不要把预设路径写成默认值。**
- [x] `files._check_path()` 增加一个**只读**白名单参数，来源 `[ps5] backup_roots`（默认空列表），只对 `list` 和 `stream` 生效；上传、建目录、改名、移动、删除一律不认这个白名单。为此加一组专门的测试。
- [x] 界面：文件页任意文件夹的操作菜单加「备份到 NAS」；配置了 `backup_roots` 时，备份对话框里出现对应的预设来源。
- [x] 界面与文档注明：存档是加密镜像，绑定主机与账号，备份只适合在同一台主机上恢复；**恢复不在本计划范围内**（向系统分区写入风险高）。

**测试：** 假客户端覆盖三种协议的写入与 `.part` 改名；`writable=False` 时拒绝；增量跳过；取消后无残留；只读白名单对每一个写操作都无效。

**需真机验证：** 把一个游戏目录备份到三种协议的 NAS 各一次，对比文件数与总大小。

---

### Task E2：前端拆分文件

**前提：** A5 已随 v1.9.1 发布，且至少经过一个版本。从 ≤1.9.0 直接网页升级到本版会被旧升级器在校验阶段拒绝（不改动系统），发布说明里要写清楚需先 SSH 升级。

**方式：** 拆成多个经典脚本（`<script defer>`），不改成 ES module。经典脚本顶层的 `const` / `let` / `function` 共享同一个全局词法作用域，所以拆分是机械操作：去掉最外层 IIFE，按现有的 29 个 `/* ── 标题 ── */` 分节切开，按原顺序加载。

**Files:**
- Modify: `web/index.html`、`web/app.js`、`web/style.css`、`tests/ui-interactions.js`、`tests/browser/console-smoke.js`、`docs/releases.md`
- Create: `web/*.js`、`web/*.css`（名字须匹配 A5 的正则）

- [x] 动手前排查全局名冲突：去掉 IIFE 后，顶层 `const` / `let` 不能与 `window` 上不可配置的属性同名（`top`、`location`、`document`、`window` 等会直接 `SyntaxError`），也要避免遮蔽常用全局（`name`、`status`、`history`、`event` 等）。列出 `app.js` 所有顶层声明逐个核对，冲突的改名。
- [x] 建议的文件划分（保持原有相对顺序作为加载顺序）：

  | 文件 | 来源分节 |
  | --- | --- |
  | `core.js` | 基础工具、网络请求、轻提示、按钮状态 |
  | `shell.js` | 路由与外壳、全局控件状态 |
  | `overview.js` | 概览渲染、轮询 |
  | `policy.js` | 策略保存 / 回退 / 退出 |
  | `settings.js` | 服务版本与更新、修改密码、运行日志，以及 C1–C3 的卡片 |
  | `repository.js` | 插件仓库、上游更新与历史版本、插件分类 |
  | `activity.js` | 网络活动（含 C4 的历史与统计） |
  | `ui.js` | 通用 DOM 小工具、手机底部面板、功能说明、进度展示 |
  | `ps5.js` | PS5 主机、电源、插件版本比对 |
  | `files.js` | PS5 文件、游戏名称 |
  | `nas.js` | NAS 存储（含 D1、E1 的入口） |
  | `library.js` | 游戏库（D2） |
  | `app.js` | 事件绑定、启动（**文件名保留，最后加载**） |

- [x] 注意顶层语句的执行时机：函数体里引用后面文件的声明没问题，但加载时就执行的顶层语句不能引用尚未加载的 `const`（暂时性死区）。「事件绑定」目前在文件中段，移到最后的 `app.js` 时确认没有顶层语句依赖它先执行。
- [x] 每个文件开头保留 `'use strict';`。`index.html` 按顺序列出全部 `<script defer>`。
- [x] `style.css` 同样按用途拆成若干文件（基础变量与排版、外壳布局、通用组件、各页面），`style.css` 文件名保留作为第一个加载的基础文件。CSS 按 `<link>` 顺序加载，层叠顺序不变即可。
- [x] 测试夹具：`tests/ui-interactions.js` 改为从 `index.html` 读出脚本列表、按序拼接后再做原来的「在启动段之前截断并暴露内部符号」处理。测试断言不改。
- [x] `node --check` 覆盖 `web/*.js`（A4 的 CI 已经是通配）。
- [ ] 跑 `tests/browser/console-smoke.js`，逐页点一遍确认控制台无报错。

**验收：** 拆分前后界面测试全部通过且断言未改；发布包内 `web/` 下文件都匹配 A5 的正则，且仍包含 `index.html`、`app.js`、`style.css`；任一文件不超过 2000 行。

**里程碑 E 收尾：** 版本 `1.13.0`，发布说明置顶写明升级路径要求。

---

## 需要用户确认或真机验证的事项汇总

| 任务 | 事项 |
| --- | --- |
| A1 | 在非 LXC 的 VM 上确认白名单域名可通 |
| A3 | Debian 12 LXC 上确认动态集合有元素、计数器累加 |
| B4 | zftpd 是否支持 `REST`；大文件断网后续传结果正确 |
| D1 | PS5Upload 引擎 `/api/pkg/install` 支持的源类型；只支持 `host_file` 时停下来报告 |
| D1 | 真实 PKG 安装后可启动；金手指被 etaHEN 识别 |
| E1 | 存档与截图的实际路径、FTP 插件是否可读 |
| E1 | 三种协议各备份一次并核对 |
| E2 | 发布说明注明 ≤1.9.0 需先 SSH 升级 |
