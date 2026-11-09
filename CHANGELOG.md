# 变更记录

## 1.0.0 — 2026-11-09

首个公开版本。

**服务端**
- DNS 策略：域名/A 记录应答、TTL 租约、CNAME 链校验、UDP/TCP 53、双栈畸形查询处理。
- 访问策略：域名 + IPv4/CIDR 白名单、上游 DNS、原子应用与回退、默认拒绝的 nftables 表 `inet ps5guard`。
- 网络活动：基于 `conntrack` 轮询的连接记录，DNS 反查域名，规则集自动分类，命名与多选放行。
- 插件仓库：只读 HTTP 服务，目录变更自动重载，管理页可上传/编辑/删除。
- PS5 主机：网段发现（pldmgr 8084 / elfldr 9021），一键接入仓库、安装/运行/停止、自动加载、关机。
- PS5 文件：`/fs/*` 流式代理，路径白名单，上传进度，下载/打包、复制/移动/删除。
- 配置外置：`config.toml` + `PS5JB_*` 环境变量，所有路径可改。

**界面**
- 响应式应用外壳：桌面侧边栏、平板图标栏、手机底部 5 标签，hash 路由 7 个分区。
- 青绿设计语言、暗色模式、`prefers-reduced-motion` / `-transparency` / `contrast` 支持。
- 表格在手机上转为卡片流，弹窗转为底部 sheet，44px 触控目标。

**安装与运维**
- `install.sh`：分步安装、`--dry-run`、`--prefix` 演练、升级保留配置与数据、`uninstall --purge`。
- `bootstrap.sh`：`curl | sudo sh` 一键部署。
- `install.sh doctor`：目录/配置/端口/服务状态体检；`reset-password` 重设密码。
- 三个 systemd 单元（策略/手册/仓库），`ExecStopPost` 保证停服务时关闭 IPv4 转发。
