# ps5-jb-server

在**局域网里用一台 Linux 主机（LXC 容器或虚拟机）**接管 PS5 的越狱相关流量：接住「用户手册」入口、
按白名单放行域名和 IP、托管插件仓库，并在 PS5 越狱后直接管理它上面的插件和文件。

一套服务，解决三件事：

1. **不再需要 U 盘**：插件仓库由本机提供，PS5 的 Payload Manager 加一次源即可安装插件。
2. **不再靠改 hosts**：「用户手册」域名由本机 DNS 劫持到本机 HTTPS 站点，PS5 打开用户手册即进入越狱入口。
3. **越狱后还能管**：发现局域网里的 PS5，一键接入仓库、安装/运行/停止插件、设置开机自动加载，以及浏览和上传主机文件。

> 只适用于你自己的主机和家庭网络。越狱会违反 PSN 服务条款，风险自负。

---

## 功能

| 模块 | 能力 |
| --- | --- |
| 用户手册宿主 | 自签 HTTPS 站点，DNS 劫持 `manuals.playstation.net` / `ena.net.playstation.net` / `www.msftconnecttest.com` |
| 访问策略 | 域名白名单 + IPv4/CIDR 白名单 + 上游 DNS；默认拒绝，保存即生效，可回退上一版 |
| 网络活动 | 记录经过本机的 IPv4 连接（域名/IP/端口/是否放行），支持命名、分类、多选一键放行 |
| 插件仓库 | 只读 HTTP 仓库，内置插件清单；管理页可上传/编辑/删除，PS5 端用 Payload Manager 直接安装 |
| PS5 主机 | 发现局域网 PS5（pldmgr 8084 / elfldr 9021），一键接入仓库、安装并运行、停止进程、配置自动加载、关机 |
| PS5 文件 | 代理 PS5 文件服务（`/fs/*`），浏览卷、上传、下载、建目录、重命名、复制/移动、删除、传输任务 |

管理界面以中文为主，提供明暗主题和响应式布局：桌面按功能分组的侧边栏、平板图标栏，手机底部可直接进入概览、主机、文件和活动，其余页面从「更多」进入。网络活动、插件仓库和主机插件目录均支持即时搜索；活动搜索可与允许/未放行筛选组合。

## 架构

```
                    ┌──────────────────────────── 家庭局域网 ────────────────────────────┐
                    │                                                                  │
   PS5 ── DNS 53 ──►│  ps5-jb-server (本机)                                            │
       ── 网关 ─────►│   ├─ DNS 策略：白名单域名 → 本机 / 其余 NXDOMAIN 或转发           │
                    │   ├─ nftables：默认拒绝，只放行白名单目标（IPv4）                 │
       ◄─ HTTPS 443 ─┤   ├─ 用户手册站点（自签证书）                                    │
                    │   ├─ 插件仓库 HTTP 8080                                          │
       ◄─ 管理 8443 ─┤   └─ 管理页 + 控制台 API                                       │
                    │                                                                  │
       ◄─ 8084/9021 ─┤  PS5 主机页：pldmgr（Payload Manager）/ elfldr                  │
       ◄─ 80 ────────┤  PS5 文件页：文件服务代理（tom-ps5-usbnet payload）             │
                    └──────────────────────────────────────────────────────────────────┘
```

浏览器不直连 PS5：管理页受 CSP 限制，且 PS5 端不发 CORS 头，所以文件与插件操作都经本机代理，
上传/下载是流式的，不落盘。

## 要求

- Debian 12 / Ubuntu 22.04 及以上的 LXC 容器或虚拟机，root，systemd。
- 主机与 PS5 在同一 IPv4 局域网。
- 需要的软件包：`python3`、`python3-dnspython`、`nftables`、`conntrack`、`openssl`
  （`install.sh` 会提示缺失项，不会静默安装）。
- PS5 端需要能触发一次内核越狱（WebKit/BD-J 等），本服务负责“用户手册入口 + 策略 + 仓库 + 遥控”。

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
sudo ./install.sh doctor              # 体检：目录、配置、端口、服务状态
sudo ./install.sh reset-password      # 重设管理密码（旧会话立即失效）
sudo ./install.sh uninstall [--purge] # 停服务并卸载；--purge 一并删除数据
```

## PS5 端设置（一次）

1. 设置 → 网络 → 设定互联网连接 → 有线/无线 → 高级设置：
   - **默认网关** 与 **主 DNS** 都填本机地址（例如 `192.168.1.10`），次 DNS 留空。
   - IP 地址保持自动获取，**不要**把 PS5 自己的 IP 设成本机地址。
2. 打开 PS5 的「用户手册」（设置 → 指南与提示 → 用户手册），会命中本机站点。
3. 越狱后想让本机接管插件：在 PS5 上运行 `pldmgr.elf`（Payload Manager）或 `elfldr`，
   然后在管理页 `#/ps5` 点「重新扫描」→「接入本服务仓库」。
4. 想用文件管理：在 PS5 上运行 `tom-ps5-usbnet.elf`（或任何实现了 `/fs/*` 文件接口的服务），
   再到管理页 `#/files` 里选择主机。

## 管理页面

`https://<本机地址>:8443/`，只输密码、没有用户名，自签证书。

| 页面 | 作用 |
| --- | --- |
| 概览 | 服务状态、运行时间、最近 DNS 请求、PS5 网络设置指引、网关计数器 |
| 访问策略 | 域名 / IPv4 白名单 / 上游 DNS，保存并应用、回退 |
| 网络活动 | 实际流量的连接记录，命名与分类，多选放行 |
| PS5 主机 | 发现主机、接入仓库、安装/运行/停止插件、自动加载 |
| PS5 文件 | 卷列表、目录浏览、上传（带进度）、下载、建目录/重命名/复制/移动/删除 |
| 插件仓库 | 仓库内插件管理（上传/编辑/删除） |
| 设置 | 改密码、服务信息、停用回滚说明 |

## 配置

`/etc/ps5-jb-server/config.toml`，示例见 [`examples/config.toml`](examples/config.toml)。
所有键都能用 `PS5JB_*` 环境变量覆盖（例如 `PS5JB_HOST_IP`、`PS5JB_PORT_ADMIN`、`PS5JB_MAX_UPLOAD`）。

| 段 | 键 | 说明 |
| --- | --- | --- |
| `[network]` | `host_ip` / `subnet` / `upstream_dns` | 留空则自动探测本机地址、推导 /24、读 `/etc/resolv.conf` |
| `[ports]` | `dns` `http` `https` `admin` `repo` | 默认 53 / 80 / 443 / 8443 / 8080 |
| `[ps5]` | `pldmgr_port` `elf_port` `file_port` | 对应 PS5 端 8084 / 9021 / 80 |
| `[ps5]` | `control` / `file_manager` | 关掉可禁用对应页面 |
| `[limits]` | `max_upload` | 上传上限，默认 64 MiB |
| `[paths]` | `data` `repo` `static` `certs` `credentials` `rulesets` | 全部可改 |

修改后 `systemctl restart ps5-jb-server` 生效。

## 插件与第三方组件

本仓库**不打包**任何插件二进制，也不打包上游的用户手册宿主脚本：

- 插件：`payloads.json` 只记录清单与 SHA-256，安装/首次运行时由 `scripts/fetch_payloads.py` 从上游下载。
  刷新仓库：`python3 scripts/fetch_payloads.py --repo-dir /srv/ps5-jb-server --base-url http://<本机>:8080/payloads/`
- 用户手册宿主：上游 [itsPLK/ps5-webkit-autoloader](https://github.com/itsPLK/ps5-webkit-autoloader)
  v0.7.1（**GPL-3.0**）的 `host.py`，安装时按 `components.json` 下载并校验 SHA-256；
  也可用 `--no-manuals-host` 跳过。
- 域名分类规则集：由 [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community)（MIT）
  编译而来，构建脚本见 `scripts/build_rulesets.py`。

## 安全与边界（务必读）

- 管理页只允许本机与配置网段的请求，登录后写操作需要 CSRF token；密码用 PBKDF2 存储，无默认密码。
- 登录会话 30 天滑动续期：Cookie 为 `HttpOnly; Secure; SameSite=Lax`，服务端只存令牌哈希
  （`/var/lib/ps5-jb-server/sessions.json`，0600），重启服务不会掉登录；登出立即失效。
- 策略只管**经过本机的 IPv4**：
  - 同网段直连、不经本机网关的流量不受控；
  - PS5 若改回原网关或使用 IPv6，策略失效（本服务默认关闭 IPv6 转发）；
  - 放行是按域名/IP，不区分端口与路径；共享 CDN 的 IP 意味着“放行一个域名”可能连带放行同 IP 的其它域名。
- PS5 端 pldmgr/elfldr **没有鉴权**，能访问那个端口的人都能操作你的主机；只在可信局域网使用，不要映射到公网。
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
- **PS5 主机页扫不到**：确认 PS5 已越狱且运行了 `pldmgr.elf` 或 `elfldr`，并在同一网段。

## 开发

```sh
python3 -m unittest discover -s tests -v      # 109 项单测
node --check web/app.js
./scripts/check-secrets.sh                    # 提交前隐私扫描
sudo ./install.sh --dry-run --prefix /tmp/x   # 安装器演练
```

目录：`server/`（服务端）、`web/`（前端）、`installer/`（安装器）、`tests/`、`systemd/`、
`rulesets/`、`scripts/`、`docs/`。

## 许可

自有代码 [MIT](LICENSE)。上游组件遵循各自许可（用户手册宿主 GPL-3.0、规则集 MIT、各插件见其项目）。
