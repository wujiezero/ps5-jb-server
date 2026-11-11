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

## 旧布局首次启用

原有 `/opt/ps5-manager` 平铺部署需要先按既有部署方式备份、安装本版本的所有 `server/*.py` 和前端文件（保留配置、NAS key 和数据），再执行一次：

```sh
sudo python3 scripts/enable_web_updates.py \
  --program-dir /opt/ps5-manager \
  --config /etc/ps5-jb-server/config.toml \
  --admin-unit ps5-manager.service \
  --repo-unit ps5-repo.service
sudo systemctl daemon-reload
sudo systemctl restart ps5-manager.service
```

登记命令写部署描述，并安装升级恢复 unit 与管理/仓库服务的恢复依赖 drop-in；不会帮你部署尚未安装的新模块，也不会改配置、密钥或数据。可先加 `--dry-run` 查看路径。前端目录必须是程序目录下的真实目录（旧布局通常为 `static`），数据与配置必须在程序目录之外。所有部署目录应由 root 所有且不能被普通用户写入。

## 故障排查

```sh
journalctl -u ps5-service-update-<任务编号>
systemctl status ps5-manager.service   # 或标准 ps5-jb-server.service
```

持久状态位于 `<data>/updates/status.json`。GitHub 没有发布、API 限流、网络错误、缺少附件或校验不匹配都不会被当成可升级版本。若主机重启或执行器在修改前拒绝了部署，服务会根据任务 unit 状态解除过期的升级占用并显示错误。正式生产主机的真实升级应在首次发版后验证；自动测试使用隔离临时目录及模拟服务控制，不会重启当前服务器。
