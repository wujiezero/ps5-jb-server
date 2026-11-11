# 版本、GitHub 发版与网页升级

## 版本源

`server/version.py` 的 `VERSION` 是唯一应用版本源，采用 `X.Y.Z` 正式版本（当前 `1.4.0`）。管理服务、插件仓库 HTTP 标识、`GET /api/version` 和页面均使用这个版本。页面侧栏和「设置 → 服务更新」显示当前版本，不需要连接 GitHub。

「检查更新」由服务器读取 `wujiezero/ps5-jb-server` 的 GitHub Releases latest API，只接受正式发布，跳过草稿和预发布。不在启动时自动访问 GitHub。未检查、暂无正式发布、检查失败和发现新版会分别显示；检查失败时不能点击更新。

## 发布新版本

1. 修改 `server/version.py`，更新 `CHANGELOG.md`，完成测试后提交准备发布的代码。
2. 创建与代码版本一致的标签并推送，例如：

```sh
git tag v1.4.0
git push origin v1.4.0
```

3. GitHub Actions 的 `Release` workflow 会执行 Python 和界面测试，核对标签与版本，生成：

```text
ps5-jb-server-1.4.0.tar.gz
ps5-jb-server-1.4.0.tar.gz.sha256
```

4. workflow 使用仓库内置 `GITHUB_TOKEN` 创建正式 Release，上传两个附件并生成发布说明。无需在服务上保存 GitHub 写入 token。仓库需要允许 GitHub Actions 使用 `contents: write`。手动运行 workflow 只验证和上传 Actions artifact，不会从普通分支创建 Release。

也可本地验证发布包：

```sh
python3 scripts/build_release.py --tag v1.4.0 --output /tmp/ps5-release
```

发布包只包含运行模块、前端、规则集和离线标题库，包含根 `VERSION`。不包含配置、密码、密钥、证书、数据、测试文件、第三方 payload 或源码仓库。打包结果可重复，GitHub 自动生成的 Source code 附件不作为网页升级源。

## 网页一键升级

标准安装器会在程序目录生成 root 所有的 `update-install.json`，记录真实配置、数据、前端路径、管理/仓库 unit 和本机 HTTPS 健康地址。在源码目录运行时可以检查版本，但不支持网页替换源码。

在「设置 → 服务更新」点击「检查更新」，阅读版本和说明后点击「更新到 vX.Y.Z」。仅已登录管理员可使用，操作需要 CSRF 校验；浏览器不能提供下载 URL、文件路径或 shell 命令。

升级过程：

1. 检查没有传输/解压/复制任务或正在处理的主机写入请求；升级期间拒绝新主机写入。
2. 只从本仓库正式 Release 下载更新包和 SHA-256 附件，验证大小和校验值。
3. 启动独立 systemd 临时单元 `ps5-service-update-<任务编号>`，管理服务重启不会终止执行器。
4. 执行器验证归档路径、类型、数量/大小限制、Python 语法及两个版本源一致；不运行归档中的安装脚本。
5. 备份会替换的程序文件至 `<data>/updates/backups/`，停止管理/仓库服务，逐文件原子替换，再启动服务。
6. 请求部署配置中的 `/api/version`，确认新版本和网关初始化就绪，并核对管理/仓库 unit 与仓库 HTTP 响应。失败则恢复原文件、启动旧服务并再次验证旧版本就绪；恢复失败会显示单独的回滚错误。

执行器持有跨进程部署锁，并在停止服务前持久保存恢复日志。断电或执行器被终止时，安装器登记的 `*-update-recovery.service` 会在下次管理/仓库服务启动前恢复旧文件；恢复未完成时不能发起另一轮升级。启动执行器的请求超时也不会立即解除升级占用。

配置、管理密码、会话、NAS 密钥、证书、白名单、标签、插件仓库及传输数据保持原样；第三方宿主 `host.py`、PS5Upload 引擎和 payload 不随本服务升级。网页升级不会运行 apt、更新 systemd 单元或做配置迁移；需要新系统依赖的版本应在发布说明注明并先手工安装。

页面每 2 秒读取本地升级状态；重启期间断连会自动重连，超过 5 分钟提供手动重连和重载。下载失败不会修改服务。备份会保留供排查，长期使用后可由管理员根据空间情况手动归档。

## 没有更新入口的旧版本：首次升级

旧版本本身没有更新 API 和升级执行器，不能通过旧页面升级自己。需要通过 SSH 手动升级一次，装好版本号、更新模块和恢复服务；之后才可以在页面检查正式 Release 并一键更新。GitHub `main` 尚未包含本功能时，不能只运行 `git pull main`。

首次升级前，等 NAS 传输、解压和文件复制全部完成。保留原配置、管理员密码、NAS 密钥、证书和数据；不要卸载、使用 `--purge` 或重新初始化 NAS key。

### 先获取包含更新功能的代码

当前可从 `auto-game-install` 分支取得已验证的运行代码和首次升级修复。请在一个新的工作目录执行，避免覆盖原有源码修改：

```sh
git clone --branch auto-game-install https://github.com/wujiezero/ps5-jb-server.git ps5-jb-server-upgrade
cd ps5-jb-server-upgrade
git switch --detach origin/auto-game-install
```

正式版本发布后，推荐改用对应 `vX.Y.Z` 标签。源码提交或分支推送不等于 GitHub Release；没有正式 Release 时，新页面显示“暂无发布”是正常的。

### 标准安装器部署

适用于程序在 `/opt/ps5-jb-server`、服务名为 `ps5-jb-server/ps5-jb-host/ps5-jb-repo` 的默认安装。先在服务器上备份：

```sh
sudo install -d -m 700 /root/ps5-upgrade
sudo sh -c 'umask 077; tar -czf /root/ps5-upgrade/before-web-updates-$(date +%Y%m%d-%H%M%S).tar.gz -C / opt/ps5-jb-server etc/ps5-jb-server var/lib/ps5-jb-server'
```

然后在刚获取的源码目录中执行：

```sh
sudo systemctl stop ps5-jb-server.service ps5-jb-host.service ps5-jb-repo.service
sudo ./install.sh upgrade --no-payloads --no-manuals-host --no-firewall
sudo systemctl restart ps5-jb-server.service ps5-jb-host.service ps5-jb-repo.service
```

必须先停止旧服务：当前安装器的预检会拒绝被占用的端口，直接在运行中的旧服务上执行 `install.sh upgrade` 会报端口占用。这一步会短暂停止管理、DNS 和用户手册服务。升级命令失败时，先处理报错或用备份恢复，再重新启动原服务；不要继续忽略错误运行。

上述选项避免首次升级时重新下载第三方组件或重写防火墙基线；不会删除已经安装的 payload、宿主和引擎。`config.toml`、凭据和 NAS key 已存在时会保留。自定义安装路径或端口请按实际布局操作，不要套用默认路径的命令。

### 旧的手工部署布局

例如 `/opt/ps5-manager` 平铺程序、`static` 前端、`ps5-manager/ps5-repo` 服务。先备份实际程序、配置、证书、数据目录和 unit。以下示例不创建全新的标准部署，不改变原服务名，也不替换第三方 `host.py`。

在获取的源码目录中执行（配置或数据路径不同请使用实际路径）：

```sh
sudo systemctl stop ps5-manager.service ps5-repo.service
sudo install -m 644 server/*.py /opt/ps5-manager/
sudo install -m 644 web/index.html web/app.js web/style.css /opt/ps5-manager/static/
sudo install -d /opt/ps5-manager/rulesets /opt/ps5-manager/titledb
sudo install -m 644 rulesets/compiled.json /opt/ps5-manager/rulesets/
sudo install -m 644 titledb/titles.json titledb/UPSTREAM_LICENSE /opt/ps5-manager/titledb/
```

安装本版本的所有运行模块和前端后，登记更新和恢复服务：

```sh
sudo python3 scripts/enable_web_updates.py \
  --program-dir /opt/ps5-manager \
  --config /etc/ps5-jb-server/config.toml \
  --admin-unit ps5-manager.service \
  --repo-unit ps5-repo.service \
  --host-unit ps5-host.service
sudo systemctl daemon-reload
sudo systemctl restart ps5-manager.service ps5-repo.service ps5-host.service
```

登记命令写部署描述，并安装升级恢复 unit 与管理/仓库服务的恢复依赖 drop-in；`--host-unit` 让用户手册服务随管理服务重新启动，避免它因 `Requires=管理服务` 被停止后没有恢复。标准安装器会自动登记对应的 `ps5-jb-host.service`。登记不会帮你部署尚未安装的新模块，也不会改配置、密钥或数据。可先加 `--dry-run` 查看路径。前端目录必须是程序目录下的真实目录（旧布局通常为 `static`），数据与配置必须在程序目录之外。所有部署目录应由 root 所有且不能被普通用户写入。

## 故障排查

```sh
journalctl -u ps5-service-update-<任务编号>
systemctl status ps5-manager.service   # 或标准 ps5-jb-server.service
```

持久状态位于 `<data>/updates/status.json`。GitHub 没有发布、API 限流、网络错误、缺少附件或校验不匹配都不会被当成可升级版本。若主机重启或执行器在修改前拒绝了部署，服务会根据任务 unit 状态解除过期的升级占用并显示错误。正式生产主机的真实升级应在首次发版后验证；自动测试使用隔离临时目录及模拟服务控制，不会重启当前服务器。
