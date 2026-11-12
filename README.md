# PS5 管家（ps5-jb-server）

<img src="docs/ui/logo.svg" width="72" alt="PS5 管家">

在**局域网里用一台 Linux 主机（LXC 容器或虚拟机）**接管 PS5 的越狱相关流量：接住「用户手册」入口、
按白名单放行域名和 IP、托管插件仓库，并在 PS5 越狱后直接管理它上面的插件和文件。

一套服务，解决四件事：

1. **不再需要 U 盘**：插件仓库由本机提供，PS5 的 Payload Manager 加一次源即可安装插件。
2. **不再靠改 hosts**：「用户手册」域名由本机 DNS 劫持到本机 HTTPS 站点，PS5 打开用户手册即进入越狱入口。
3. **越狱后还能管**：发现局域网里的 PS5，一键接入仓库、安装/运行/停止插件、编排开机自动加载。
4. **文件随手传**：浏览和管理 PS5 存储，从浏览器或家里的 NAS（SMB / WebDAV / FTP）直接把文件传到 PS5。

> 只适用于你自己的主机和家庭网络。越狱会违反 PSN 服务条款，风险自负。

---

## 功能

| 模块 | 能力 |
| --- | --- |
| 用户手册宿主 | 自签 HTTPS 站点，DNS 劫持 `manuals.playstation.net` / `ena.net.playstation.net` / `www.msftconnecttest.com` |
| 访问策略 | 域名白名单 + IPv4/CIDR 白名单 + 上游 DNS；默认拒绝，保存即生效，可回退上一版；网关网卡自动探测，也可在配置里指定 |
| 网络活动 | 记录网关连接、DNS 查询与本机服务事件；按结果与来源筛选、命名、分类、多选一键放行 |
| 插件仓库 | 只读 HTTP 仓库；管理页可上传、替换、编辑、删除，一键复制 Payload Manager 源地址；对照 Payload Manager 官方源检查新版本，一键（或全部）下载到仓库，旧版本自动保留，可随时回退 |
| PS5 主机 | 发现局域网 PS5（pldmgr 8084 / elfldr 9021，以及 DDP 广播，待机或未运行载荷也能发现），显示开机/待机/离线、主机名与固件；配对后可从待机唤醒并登录、一键进入待机（可选组件）；接入仓库、安装并运行、停止进程、退出 pldmgr；自动加载可编辑顺序、每个插件加载后的等待时间与启动倒计时 |
| PS5 文件 | 优先使用 tom-ps5-usbnet 的 `/fs/*`，不可用时自动改走 PS5 上的 FTP（如 zftpd）；浏览卷与容量、上传、下载、整目录打包、新建、重命名、复制/移动、删除 |
| NAS 存储 | 添加 SMB / WebDAV / FTP 的 NAS，浏览后把文件或文件夹直接传到 PS5 指定目录；服务器流式中转，可排队、取消、跳过或覆盖同名文件；账号加密保存 |
| 解压安装 | NAS 上的 `.rar` / `.partNN.rar` 游戏包一键解压装到 PS5：开始前扫描全部分卷包头（缺卷、加密、空间、字典大小），边解压边写进 PS5、不占本机磁盘，先解到隐藏目录、核对后才改名到位；可合并补丁 / DLC 到已有游戏；固实包与 4 GB 字典的包走 unrar 引擎（需 FUSE 与足够内存） |
| 传输任务 | 上传、复制/移动、NAS 传输统一显示进度条、百分比、已传/剩余大小、实时速度、已用时间与预计剩余 |

管理界面以中文为主，提供明暗主题和响应式布局：桌面按功能分组的侧边栏、平板图标栏，手机底部可直接进入
概览、主机、文件和活动，其余页面从「更多」进入。

## 架构

```
                     ┌──────────────────────────── 家庭局域网 ─────────────────────────────┐
                     │                                                                     │
  PS5 ── DNS 53 ────►│  ps5-jb-server（本机）                                              │
      ── 默认网关 ──►│   ├─ DNS 策略：白名单域名 → 放行 / 其余 NXDOMAIN                    │
                     │   ├─ nftables：默认拒绝，只放行白名单目标（IPv4）                   │
      ◄─ HTTPS 443 ──┤   ├─ 用户手册站点（自签证书）                                      │
      ◄─ HTTP 8080 ──┤   ├─ 插件仓库                                                      │
                     │   └─ 管理页 + 控制台 API（HTTPS 8443）◄── 浏览器                   │
                     │                                                                     │
      ◄─ 8084 / 9021 ┤  PS5 主机：pldmgr（Payload Manager）/ elfldr                        │
      ◄─ 2120 / 80 ──┤  PS5 文件：FTP（zftpd）或 tom-ps5-usbnet 的 /fs/* 代理              │
                     │                                                                     │
  NAS ◄──────────────┤  NAS 存储：SMB 445 / WebDAV / FTP，流式转发到 PS5                   │
                     └─────────────────────────────────────────────────────────────────────┘
```

浏览器不直连 PS5 或 NAS：管理页受 CSP 限制，PS5 端也不发 CORS 头，所以文件与插件操作都经本机代理，
上传、下载和 NAS 传输都是流式的，不在本机落盘。

## 要求

### 已验证的环境

| 项目 | 实测环境（2026-11-10） |
| --- | --- |
| 宿主 | Proxmox VE，Intel Celeron N5105（x86_64） |
| 容器 | **Debian 12 非特权 LXC**，2 核，systemd，root |
| 网络 | 千兆有线；PS5、NAS 与本机在同一个 IPv4 网段 |

只有上面这套环境实际跑过。**Ubuntu、Debian 13、其他发行版、虚拟机、特权容器、ARM 主机都没有测试过**：
程序是纯 Python 标准库 + Debian 软件包，理论上 Ubuntu 22.04 及以上也能装（软件包名基本相同，`libunrar5` 在 Ubuntu 的
multiverse，`python3-libarchive-c` 在 universe），但请先用 `--dry-run` 看清每一步，装完跑 `doctor` 自查。
唤醒 / 待机用到的 PS5Upload 引擎只有 x86_64 版本。

### 硬件与容器配置

| 用途 | CPU | 内存 | 磁盘 | LXC 功能 |
| --- | --- | --- | --- | --- |
| 基础功能：DNS 白名单、网关、管理页、插件仓库、PS5 文件 / NAS 传输 | 1 核即可 | 512 MB（服务自身约 70–130 MB） | 4 GB | 默认即可 |
| 解压安装：普通压缩包（libarchive，字典 ≤64 MB） | 解压时占满 1 核 | 同上（在服务进程里，约 100 MB） | 不额外占用（流式写进 PS5） | 默认即可 |
| 解压安装：固实包或大字典包（unrar，常见的 4 GB 字典） | 解压时约 1.5 核 | **≥ 字典大小 + 1 GB，4 GB 字典的包按 6 GB 配** | 不额外占用 | **开启 FUSE** |

- **FUSE**：Proxmox 里选中容器 →「选项」→「功能」→ 勾选 **FUSE**，然后重启容器（重启期间 PS5 的 DNS 会断开）。
  只有 unrar 引擎需要它：unrar 只能读文件形式的分卷，所以解压时用 rclone 把 NAS 目录只读挂载进来。
- 实测部署里只额外开了 FUSE，其余功能项保持 Proxmox 新建非特权容器时的默认值；网关用的 nftables 与 IP 转发
  在非特权容器里可以正常工作。
- 速度参考（N5105，千兆网，NAS 走 HTTPS WebDAV）：普通压缩包约 64 MB/s，4 GB 字典的包约 78 MB/s（峰值 98 MB/s）。
  解压时数据会经过本机两次（NAS → 本机 → PS5），千兆网是上限。

### 软件包

- 必需：`python3`、`python3-dnspython`、`python3-cryptography`、`nftables`、`conntrack`、`openssl`。
- 可选：SMB 类型的 NAS 需要 `python3-smbc`（WebDAV / FTP 不需要）。
- 可选：从 NAS 解压安装 `.rar` / `.partNN.rar` 游戏包，用 `--with-extract` 安装 `python3-libarchive-c`、`libunrar5`、
  `rclone`、`fuse3`。`libunrar5` 在 Debian 的 **non-free** 组件里，软件源要先加上 non-free。
- 可选：唤醒 / 待机，用 `--with-power-engine` 安装 PS5Upload 引擎，PS5 端运行 ps5upload 插件。

必需软件包缺失时，`bootstrap.sh` / `install.sh` 会自动安装（apt；dnf、yum、apk、pacman、zypper 尽力而为），
包括 `git`、`python3`、CA 证书，以及上面的必需项和 `python3-smbc`；每条安装命令都会打印出来。
不想自动安装就加 `--no-deps`，此时只报告缺什么。解压引擎与电源引擎仍需显式加参数。

### PS5 端

需要能触发一次内核越狱（WebKit/BD-J 等），本服务负责“用户手册入口 + 策略 + 仓库 + 遥控 + 文件”。

## 快速开始

```sh
# 一条命令（推荐）
curl -fsSL https://raw.githubusercontent.com/wujiezero/ps5-jb-server/main/bootstrap.sh | sudo sh

# 或者
git clone https://github.com/wujiezero/ps5-jb-server
cd ps5-jb-server
sudo ./install.sh
```

安装完成后会打印管理地址与初始密码（同时写在 `/root/ps5-jb-server-access.txt`，权限 0600）。

常用参数：

```sh
sudo ./install.sh --dry-run           # 只打印将要做的每一步，不改系统
sudo ./install.sh --host-ip 192.168.1.10 --subnet 192.168.1.0/24
sudo ./install.sh --no-firewall       # 只用 DNS + 管理页，不做网关策略
sudo ./install.sh upgrade             # 升级程序，保留配置、密码、密钥与数据
sudo ./install.sh --with-power-engine # 同时安装唤醒/待机所需的 PS5Upload 引擎（可选）
sudo ./install.sh --with-extract      # 同时安装游戏压缩包解压引擎（可选，见「要求」）
sudo ./install.sh doctor              # 体检：目录、配置、端口、服务状态
sudo ./install.sh reset-password      # 重设管理密码（旧会话立即失效）
sudo ./install.sh uninstall [--purge] # 停服务并卸载；--purge 一并删除数据
```

## 版本与更新

侧栏与「设置 → 服务更新」显示当前版本；点击「检查更新」读取 GitHub 最新正式 Release，显示新版本和发布说明，再点击「更新到 vX.Y.Z」完成服务升级。更新包经过 SHA-256 校验，保留配置、NAS 密钥、密码及数据，启动失败自动恢复旧程序。源码运行只支持检查更新；旧部署首次启用和 `vX.Y.Z` 自动发版步骤见 [版本与发版](docs/releases.md)。

本机无法直连 GitHub 时，给服务设置 `HTTPS_PROXY`（或安装时加 `--proxy URL`）就能让更新和插件上游走代理；
PS5、NAS 与本机仓库始终直连。当前生效情况在「设置 → 服务更新」的「网络代理」一行，详见
[安装文档](docs/installation.md) 的「代理」一节。

## PS5 端设置（一次）

1. 设置 → 网络 → 设定互联网连接 → 有线/无线 → 高级设置：
   - **默认网关** 与 **主 DNS** 都填本机地址（例如 `192.168.1.10`），次 DNS 留空。
   - IP 地址保持自动获取，**不要**把 PS5 自己的 IP 设成本机地址。
2. 打开 PS5 的「用户手册」（设置 → 指南与提示 → 用户手册），会命中本机站点。
3. 越狱后想让本机接管插件：在 PS5 上运行 `pldmgr.elf`（Payload Manager）或 `elfldr`，
   然后在管理页 `#/ps5` 点「重新扫描」→「接入本服务仓库」。服务启动时也会自动扫描一次。
4. 想用文件管理：在 PS5 上运行一个 FTP 插件（如 zftpd，端口 2120）即可；同时运行 web-file-mgr（8888）
   时可显示各存储的容量。也支持 `tom-ps5-usbnet.elf` 的 `/fs/*` 接口，但它本质是 USB 网卡驱动，
   只在 USB 网卡接了网线时运行，且文件服务在 USB 网卡自己的地址上。

## 唤醒与待机（可选）

PS5 的待机即休息模式（黄灯）：插件留在内存里，唤醒后不必重新越狱。本服务用 DDP 判断状态
（不需要任何组件），唤醒与待机则交给 [PS5Upload](https://github.com/phantomptr/ps5upload) 引擎完成：

1. 安装引擎：`sudo ./install.sh --with-power-engine`（从固定版本下载并校验 SHA-256，装为
   `ps5upload-engine` 服务，只监听本机 19113，外部连接由 nft 表 `inet ps5upload_guard` 拦截）。
2. PS5 上开启「设置 › 系统 › 遥控游玩 › 启用遥控游玩」，在管理页插件库安装并运行 ps5upload 插件。
3. 主机卡片点「配对唤醒」：主机开机、插件在跑时无需 PIN；否则用「链接装置」的 8 位 PIN 加 PSN 账号 ID。
4. 之后主机待机时卡片显示「唤醒」：唤醒后自动建立遥控游玩会话并登录用户（只发 DDP 唤醒的主机
   5 分钟后会因 `RemotePlayConnectTimeout` 退回待机）；开机时显示「进入待机」。

配对密钥由引擎加密保存在它自己的数据目录（`/var/lib/private/ps5upload-engine`），本服务和页面都不保存、
不转发。主机恢复/重置、增删用户、IP 变化后需要重新配对。PS5 不响应 WoL 魔术包，请勿尝试。

## 管理页面

`https://<本机地址>:8443/`，只输密码、没有用户名，自签证书。

| 页面 | 作用 |
| --- | --- |
| 概览 | 服务状态、快捷入口实时摘要、最近 DNS 请求、PS5 网络设置指引、网关计数器 |
| 访问策略 | 域名 / IPv4 白名单 / 上游 DNS，保存并应用、回退上一版 |
| 网络活动 | 连接与 DNS 记录，按结果和来源筛选，命名与分类，多选放行 |
| PS5 主机 | 发现主机、接入仓库、安装/运行/停止插件、编排自动加载 |
| PS5 文件 | PS5 存储与 NAS 存储并列；浏览、上传、下载、整理文件，NAS 文件直传 PS5，传输任务面板 |
| 插件仓库 | 上传、替换、编辑、删除插件，复制仓库源地址；检查上游更新、一键更新、历史版本回退 |
| 设置 | 改密码、服务信息（端口、路径、服务名）、停用回滚说明 |

## 配置

`/etc/ps5-jb-server/config.toml`，示例见 [`examples/config.toml`](examples/config.toml)。
所有键都能用 `PS5JB_*` 环境变量覆盖（例如 `PS5JB_HOST_IP`、`PS5JB_PORT_ADMIN`、`PS5JB_MAX_UPLOAD`）。

| 段 | 键 | 说明 |
| --- | --- | --- |
| `[network]` | `host_ip` / `subnet` / `upstream_dns` | 留空则自动探测本机地址、推导 /24、读 `/etc/resolv.conf` |
| `[network]` | `interface` | 网关规则所用网卡，留空则取持有 `host_ip` 的网卡（再退到默认路由出口）；只支持单网卡 |
| `[ports]` | `dns` `http` `https` `admin` `repo` | 默认 53 / 80 / 443 / 8443 / 8080 |
| `[ps5]` | `pldmgr_port` `elf_port` `file_port` | 对应 PS5 端 8084 / 9021 / 80 |
| `[ps5]` | `ftp_port` / `space_port` | FTP 后备端口（默认 2120，0 关闭）/ 读取容量的 web-file-mgr 端口（默认 8888，0 关闭） |
| `[ps5]` | `control` / `file_manager` | 关掉可禁用对应页面 |
| `[limits]` | `max_upload` | 插件仓库上传上限，默认 64 MiB |
| `[limits]` | `max_file_upload` | PS5 文件上传上限（流式转发不落盘），默认 0 = 不限制 |
| `[limits]` | `transfer_retries` | NAS → PS5 传输中单个文件遇到网络错误时的自动重试次数（支持时断点续传），默认 3，0 关闭 |
| `[paths]` | `data` `repo` `static` `certs` `credentials` `rulesets` `titles` | 全部可改（`titles` 为游戏名称库） |
| `[paths]` | `secret_key` | NAS 账号加密密钥，默认与配置文件同目录的 `nas.key` |
| `[ps5]` | `engine_url` | 唤醒/待机所用 PS5Upload 引擎地址，默认 `http://127.0.0.1:19113` |

修改后 `systemctl restart ps5-jb-server` 生效。

## 插件与第三方组件

本仓库**不打包**任何插件二进制，也不打包上游的用户手册宿主脚本：

- 插件：`payloads.json` 只记录清单、镜像地址与 SHA-256，安装时由 `scripts/fetch_payloads.py` 自动下载。
  个别插件下载失败（网络、HTTP、校验和不符）只记录原因、不中断安装，结束时汇总成功与失败数量；
  仓库为空时会告警，重跑同一条命令即可补齐：
  `python3 scripts/fetch_payloads.py --repo-dir /srv/ps5-jb-server --base-url http://<本机>:8080/payloads/`
  （清单若使用相对地址，可加 `--download-base <镜像前缀>`）。
- 用户手册宿主：上游 [itsPLK/ps5-webkit-autoloader](https://github.com/itsPLK/ps5-webkit-autoloader)
  v0.7.1（**GPL-3.0**）的 `host.py`，安装时按 `components.json` 下载并校验 SHA-256；
  也可用 `--no-manuals-host` 跳过。
- 域名分类规则集：由 [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community)（MIT）
  编译而来，构建脚本见 `scripts/build_rulesets.py`。
- 游戏名称库 `titledb/titles.json`：由 [andshrew/PlayStation-Titles](https://github.com/andshrew/PlayStation-Titles)（MIT）
  编译而来，文件页与 NAS 列表据此把 `CUSA…`/`PPSA…` 显示为游戏名；构建脚本 `scripts/build_titles.py`，
  数据来源与更新方法见 [`docs/file-manager.md`](docs/file-manager.md#游戏名称标题-id--游戏名)。
- 唤醒 / 待机（可选）：[PS5Upload](https://github.com/phantomptr/ps5upload) 引擎 v12.4.2（**GPL-3.0**），
  作为独立进程运行、由本服务通过本机 HTTP 调用；安装器按 `components.json` 下载并校验 SHA-256，不随仓库分发。
- 自动加载格式与 [itsPLK/ps5-payload-manager](https://github.com/itsPLK/ps5-payload-manager) 的
  `AUTOLOAD_LIST`（`local/<槽位>`、`!<毫秒>`）保持一致。

## 安全与边界（务必读）

- 管理页只允许本机与配置网段的请求，登录后写操作需要 CSRF token；管理密码用 PBKDF2 存储，无默认密码。
- 登录会话 30 天滑动续期：Cookie 为 `HttpOnly; Secure; SameSite=Lax`，服务端只存令牌哈希
  （`/var/lib/ps5-jb-server/sessions.json`，0600），重启服务不会掉登录；登出或改密码立即失效。
- **NAS 账号加密保存**：用户名和密码以 AES-256-GCM 加密后写入数据目录的 `nas.json`，
  每条密文绑定所属连接；密钥是配置目录下单独的 `nas.key`（0600），与数据目录分开存放，
  单独拿到数据目录或其备份无法解出账号。接口从不返回 NAS 密码。
  能以 root 登录本机的人仍可同时读取密钥与密文——这是需要无人值守登录 NAS 的服务固有的限制，请保护好主机本身。
  更换或丢失 `nas.key` 后，已保存的 NAS 账号需要在管理页重新填写。
- 策略只管**经过本机的 IPv4**：
  - 同网段直连、不经本机网关的流量不受控；
  - PS5 若改回原网关或使用 IPv6，策略失效（本服务默认关闭 IPv6 转发）；
  - 放行是按域名/IP，不区分端口与路径；共享 CDN 的 IP 意味着“放行一个域名”可能连带放行同 IP 的其它域名。
- PS5 端 pldmgr/elfldr 与常见 FTP 插件**没有鉴权**，能访问那个端口的人都能操作你的主机；
  只在可信局域网使用，不要映射到公网。
- 本服务不是沙箱：它以 root 运行、改写 nftables/sysctl，请只在自己的机器上使用。

## 故障排查

```sh
systemctl status ps5-jb-server ps5-jb-host ps5-jb-repo
journalctl -u ps5-jb-server -n 100
sudo ./install.sh doctor
nft list table inet ps5guard         # 策略是否在
dig @<本机> manuals.playstation.net  # 应返回本机地址
```

- **PS5 打开用户手册仍是原页面**：确认 PS5 主 DNS 是本机地址，且 `ps5-jb-host` 在跑（443 端口）。
- **策略不生效／PS5 完全连不上网**：确认 PS5 默认网关是本机地址；`nft list table inet ps5guard` 有规则；
  日志里是否有“网关初始化失败”（失败时服务会关闭转发并保留管理页用于排查）。
- **网络活动为空**：只改 DNS 不会产生连接记录，必须同时把网关指向本机。
- **PS5 主机页扫不到**：确认 PS5 已越狱且运行了 `pldmgr.elf` 或 `elfldr`，并在同一网段；
  开启遥控游玩后，待机中的主机也能被 DDP 发现。
- **唤醒后几分钟又睡回去**：说明只做了 DDP 唤醒、没有登录；确认已配对且 `ps5upload-engine` 服务在运行。
- **无法进入待机**：需要 PS5 上运行 ps5upload 插件（端口 9120）。
- **文件页无法连接**：确认 PS5 上运行着 FTP 插件（默认端口 2120）；使用 tom-ps5-usbnet 时，
  它的日志在 PS5 的 `/data/tom-ps5-usbnet/log.txt`，`no Ethernet link` 表示 USB 网卡没接网线。
- **容量显示“未知”**：FTP 协议不提供容量，需要 PS5 上同时运行 web-file-mgr（8888）。
- **NAS 连接失败**：在 NAS 对话框点「测试连接」查看具体原因；SMB 需要本机安装 `python3-smbc`。
- **网页更新下载超时**：网络无法直连 GitHub 的 release 附件；设置 `HTTPS_PROXY` 或 `./install.sh --proxy` 后重试。

## 开发

```sh
python3 -m unittest discover -s tests          # 服务端单测（需要 python3-dnspython 与 python3-cryptography）
node --check web/app.js
node --test tests/ui-interactions.js           # 前端交互测试
ruff check --select E9,F63,F7,F82,F401,F811,F841 server installer scripts tests   # 只查 pyflakes 级别问题
./scripts/check-secrets.sh                     # 提交前隐私扫描
sudo ./install.sh --dry-run --prefix /tmp/x    # 安装器演练
```

每次推送到 `main` 和每个 PR 都会由 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 跑上面这些检查并试打一次发布包；
打 tag 时 `release.yml` 再跑一遍测试后发布。

目录：`server/`（服务端）、`web/`（前端）、`installer/`（安装器）、`tests/`、`systemd/`、
`rulesets/`、`titledb/`、`scripts/`、`docs/`。设计与接口细节见 [`docs/architecture.md`](docs/architecture.md)
与 [`docs/file-manager.md`](docs/file-manager.md)，变更记录见 [`CHANGELOG.md`](CHANGELOG.md)。

## 许可

自有代码 [MIT](LICENSE)。上游组件遵循各自许可（用户手册宿主 GPL-3.0、规则集 MIT、游戏名称库 MIT、各插件见其项目）。
