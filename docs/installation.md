# 安装、升级与卸载

## 三种安装方式

| 方式 | 命令 | 适用 |
| --- | --- | --- |
| 一键脚本 | `curl -fsSL .../bootstrap.sh \| sudo sh` | 全新机器，源码放在 `/opt/ps5-jb-server-src` |
| 源码安装 | `git clone … && cd ps5-jb-server && sudo ./install.sh` | 想控制源码位置/版本 |
| 演练 | `sudo ./install.sh --dry-run` | 先看会改哪些文件，不动系统 |

`bootstrap.sh` 支持环境变量：`PS5JB_REF`（默认 `main`）、`PS5JB_DIR`（默认 `/opt/ps5-jb-server-src`）。

## 安装做了什么

1. 预检：root、`python3`/`openssl`/`nft`/`systemctl` 是否存在、`python3-dnspython` 是否可导入、
   端口是否被占用（被占用会指明是谁）。
2. 建立目录：`/opt/ps5-jb-server`（程序）、`/etc/ps5-jb-server`（配置与证书）、
   `/var/lib/ps5-jb-server`（密码哈希、策略、标签）、`/srv/ps5-jb-server`（插件仓库）。
3. 写 `/etc/ps5-jb-server/config.toml`（已存在则保留）与两套自签证书
   （管理页 `admin.*`、用户手册 `manuals.*`，含 `manuals.playstation.net` 等 SAN）。
4. 生成 12 位管理密码 → `credentials.json`（PBKDF2-SHA256、310k 轮）与
   `/root/ps5-jb-server-access.txt`（0600）。
5. 下载第三方组件：用户手册宿主（GPL-3.0，校验 SHA-256）、插件（`payloads.json` 清单，逐个校验）。
6. 写 nftables 基线（默认拒绝）、`/etc/sysctl.d`（`ip_forward=0`），并 `nft -c` 校验语法。
7. 安装三个 unit 并 `enable --now`：`ps5-jb-server`（策略/DNS/管理）、`ps5-jb-host`（用户手册 443/80）、
   `ps5-jb-repo`（插件仓库 8080）。
8. 健康检查：管理页、仓库、用户手册三个 URL；最后打印地址与密码位置。

可选步骤（显式加参数才执行）：

- `--with-power-engine`：下载并校验 PS5Upload 引擎，装为 `ps5upload-engine.service`（仅 x86_64）。
- `--with-extract`：`apt-get install python3-libarchive-c libunrar5 rclone fuse3`，用于从 NAS 解压安装游戏压缩包。
  软件源里没有 `libunrar5`（Debian 需要 non-free 组件）时跳过它并提示；装完检查 `/dev/fuse`（Proxmox LXC 要在
  「选项 → 功能」勾选 FUSE 后重启容器）和内存（4 GB 字典的包需要约 5 GB，建议给容器 6 GB）。
  unrar 解压在 `systemd-run` 临时起的 `ps5-extract-<任务>` 单元里运行，不改动三个常驻服务的沙箱设置。
  只在 Proxmox 上的 Debian 12 非特权 LXC 里实测过。

## 参数

```sh
--prefix DIR          全部路径放到 DIR 下（测试用，不改系统位置）
--host-ip IP          本机在局域网的地址（默认自动探测）
--subnet CIDR         允许使用本服务的网段（默认 <host-ip>/24）
--upstream-dns IP     白名单域名的转发目标（默认读 /etc/resolv.conf）
--dns-port/--http-port/--https-port/--admin-port/--repo-port
--no-firewall         不写 nftables/sysctl（只有 DNS 与管理页）
--no-services         不安装 systemd 单元（只铺文件）
--no-payloads         不下载插件，只建空仓库
--no-manuals-host     不下载用户手册宿主
--with-power-engine   同时安装 PS5Upload 引擎（唤醒 / 待机，可选）
--with-extract        同时安装游戏压缩包解压引擎（可选）
--dry-run             只打印动作
```

## 升级

已启用网页升级的部署可在「设置 → 服务更新」查看当前/最新版本、检查 GitHub 正式发布并一键升级。升级前校验 SHA-256，保留配置与 NAS 密钥，重启失败自动恢复旧程序。首次启用旧布局和发布新版本的方法见 [版本与发版](releases.md)。

没有网页升级入口的旧版本需要先通过 SSH 手动升级一次。标准安装先备份程序、配置、密钥和数据，再获取包含新功能的源码、停止原服务、运行升级、重新启动；详见 [旧版首次升级](releases.md#没有更新入口的旧版本首次升级)。当前功能尚在 `auto-game-install` 分支时，直接拉取 `main` 不会得到新入口。

标准布局的升级动作如下（先完成备份和源码获取）：

```sh
sudo systemctl stop ps5-jb-server.service ps5-jb-host.service ps5-jb-repo.service
sudo ./install.sh upgrade --no-payloads --no-manuals-host --no-firewall
sudo systemctl restart ps5-jb-server.service ps5-jb-host.service ps5-jb-repo.service
```

先停服务是因为安装器会检查端口占用；如果中途失败，应先处理报错或恢复备份，再启动原服务。旧的 `/opt/ps5-manager` 手工布局请按专门步骤升级，不要直接套用标准路径。

- 覆盖 `/opt/ps5-jb-server` 下的程序与前端；
- **保留** `config.toml`、证书、管理员密码、白名单、标签、插件仓库；
- 重启三个服务后做健康检查。

## 卸载

```sh
sudo ./install.sh uninstall            # 停服务、删 unit、去掉 nft 包含、恢复 ip_forward=0
sudo ./install.sh uninstall --purge    # 额外删除 /opt、/etc、/var/lib、/srv 下本服务目录
```

自定义 `--prefix` 时，`--purge` 会顺带删除变空的上层目录；非本服务创建的文件不会删除。

## 回滚到安装前

安装器不会备份旧部署。若机器上原本有其他同类服务，安装前请自行备份；本仓库测试安装时使用的
`--prefix` 演练即用于此目的：

```sh
sudo ./install.sh --dry-run --prefix /tmp/ps5jb-dry   # 先看清单
```

## doctor

```sh
./install.sh doctor
```

输出：源码位置、安装目录、配置路径、主机/网段、五个端口占用情况、数据与仓库是否存在、
解压引擎（libarchive / unrar 缺哪些组件）与内存、三个 unit 的 enabled/active 状态。
