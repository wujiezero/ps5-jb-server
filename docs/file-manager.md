# PS5 文件管理：接口与实现

管理页里的「PS5 文件」不是自己造的文件服务，而是**代理**运行在 PS5 上的 payload 的文件接口。
来源是同一作者的项目 `tom-ps5-usbnet`（该项目的网卡驱动部分没有被引入，只取文件管理与界面风格）。

## 为什么需要代理

浏览器里的管理页受 CSP `default-src 'self'` 约束，不能直接访问 `http://<PS5>/fs/*`；
PS5 端的 payload 也不发 CORS 头。因此所有请求都经过本服务的 `/api/files/*`，
由服务端转发到 PS5，上传/下载都是流式的，不在服务端落盘。

## 对端接口（payload 侧，已按其源码核对）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/fs/volumes` | 卷列表：`{volumes:[{path,kind,format,total,free}]}` |
| GET | `/fs/list?path=` | 目录：`{path,entries:[{name,kind,size,mtime}],truncated}` |
| GET | `/fs/get?path=` | 下载单个文件（流） |
| GET | `/fs/tar?path=` | 整目录打包下载（流） |
| GET | `/fs/size?path=` | 目录占用 |
| GET | `/fs/job` / `/fs/transfers` | 复制/移动任务、传输状态 |
| POST | `/fs/put?path=&overwrite=&client=` | 上传（body 为文件字节） |
| POST | `/fs/mkdir?path=` / `/fs/rename?from=&to=` / `/fs/delete?path=` | 目录与重命名、删除 |
| POST | `/fs/copy?from=&to=` / `/fs/move?from=&to=` / `/fs/cancel` | 复制、移动、取消 |
| POST | `/fs/bench?path=` | 测速 |

## 本服务的接口（管理页调用）

`/api/files/info`、`/api/files/configure`、`/api/files/volumes`、`/api/files/list`、
`/api/files/size`、`/api/files/job`、`/api/files/transfers`、`/api/files/download`、
`/api/files/tar`、`/api/files/upload`、`/api/files/{mkdir,rename,delete,copy,move,cancel,bench}`。

## 安全边界

- 只代理配置网段内的地址（`settings.subnet`），不允许跨网段。
- 路径白名单：`/data`、`/mnt/ext<数字>`、`/mnt/usb<数字>`；拒绝 `.`、`..`、相对路径和超长路径。
- 上传为流式转发，默认不限大小（`[limits] max_file_upload`，0 = 不限制）；请求必须带会话与 `X-CSRF-Token`。
- 文件名只从 `X-Filename` 头取，且不允许 `/` 与 `..`。

## FTP 后备（zftpd）

tom-ps5-usbnet 本质是 USB 网卡驱动：它自带网络栈，只有在 USB 网卡**有网线链路**时才运行，
文件服务挂在 USB 网卡自己通过 DHCP 拿到的地址上，而不是 PS5 主网卡地址。日志在
PS5 的 `/data/tom-ps5-usbnet/log.txt`，没有链路时会写 `no Ethernet link` 后退出。

因此 `FileProxy` 先试 HTTP `/fs/volumes`（3 秒），失败再试 FTP（`[ps5] ftp_port`，默认 2120，
匿名登录）。选中的后端缓存 60 秒，连接失败时重新探测；`/api/files/info` 返回 `backend`。

| 操作 | FTP 实现 |
| --- | --- |
| 卷 | `/data` + `/mnt/ext*`、`/mnt/usb*` 中非空的挂载点；容量未知 |
| 列表 | `MLSD`；zftpd 对不存在的路径和文件返回空列表，空结果时回父目录核实 |
| 下载 / 打包 | `RETR` 流式；整目录在线程里边下载边写 tar 流 |
| 上传 | 先查重名（409），`STOR` 流式 |
| 复制 / 移动 | 同盘移动用 `RNFR/RNTO` 瞬间完成；其余为后台任务，两条连接 `RETR`→`STOR`，`/api/files/job` 报进度，可取消 |
| 删除 | 自底向上 `DELE` / `RMD` |

## NAS 存储（SMB / WebDAV / FTP）

`server/nas.py`：连接配置存于 `<data>/nas.json`（0600，原子写）。用户名和密码不以明文落盘：
`SecretBox` 用 AES-256-GCM 加密（12 字节随机 nonce，AAD 为 `ps5jb-nas:<连接 ID>`），存为
`credentials: "v1:<base64>"`；密钥 `nas.key`（32 字节，0600）默认在配置目录，与数据目录分开，
服务在 `ProtectSystem=strict` 下无法写 `/etc` 时才退回数据目录生成。旧版明文条目启动时自动加密迁移；
密钥不匹配时该连接标记 `credentials_ok: false`，页面提示重新填写。接口永不返回密码，编辑时留空即保留原密码。

| 协议 | 实现 | 说明 |
| --- | --- | --- |
| SMB | Debian `python3-smbc`（libsmbclient） | 共享名留空时根目录列出全部共享（隐藏 `$` 结尾的管理共享） |
| WebDAV | 标准库 `urllib`，`PROPFIND` Depth 0/1 + `GET` | Basic / Digest；HTTPS 默认不校验证书（家用 NAS 多为自签），可勾选校验 |
| FTP | `ftplib`，可选 FTPS | 以父目录列表判断文件/目录（不少服务器对目录执行 `SIZE` 也返回 0） |

接口：`GET /api/nas`、`GET /api/nas/list?id=&path=`、`POST /api/nas/{save,delete,test,transfer}`、
`GET /api/transfers`、`POST /api/transfers/{cancel,clear}`。NAS 侧鉴权失败一律返回 502，
避免被页面当成管理员会话过期（401）。

传输任务（`TransferManager`）：单线程顺序执行、其余排队；先统计（`scanning`）再传输（`running`）；
每个文件从 NAS 读 256 KiB 块直接喂给 `FileProxy.upload`（FTP `STOR` 或 HTTP `/fs/put`），
进度字段 `bytes_done/bytes_total/percent/speed`（5 秒滑动窗口）`/eta/elapsed/files_*`；
同名文件按目标目录列表一次判断（跳过或覆盖）；取消或单文件失败时删除半截文件，其余继续。
已结束的任务最多保留 30 条，服务重启后清空。

2026-11-10 在 Debian 12 容器里对 Samba 4.23、rclone WebDAV、rclone FTP 做过端到端联调：
中文与空格文件名、40 MB 文件字节一致、同名跳过、取消、错误密码提示均通过。

## 依赖

对端需要有 zftpd（FTP）或 `tom-ps5-usbnet.elf`（或任何实现上述 `/fs/*` 的服务）在跑，
并在管理页「PS5 文件」里选择它的地址（默认从已发现的 PS5 主机里选）。
