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

## 游戏名称（标题 ID → 游戏名）

「PS5 文件」和 NAS 列表里，名称中带 PS4/PS5 标题 ID 的条目（`CUSA36464`、`PPSA03671`、
`PPSA01576.ffpfsc`、`PPSA03671-app`、`UP9000-PPSA03671_00-....pkg` 等）会在原文件名下方显示游戏名，
原文件名不变；中文名优先，悬停可看英文名；筛选框也能按游戏名搜索。识别规则为
`(CUSA|PPSA|PLAS|PCAS|PLJS|…)\d{5}`，前后不能紧挨字母或数字（`server/titles.py` 的 `PREFIXES`，
前端 `web/app.js` 的 `TITLE_ID_PATTERN`，两处保持一致）。查不到名称的条目只显示原名，不报错。

名称来源分两层，按标题 ID 合并（内置库优先，PS5 只补缺）：

1. **内置离线库** `titledb/titles.json`：由 [andshrew/PlayStation-Titles](https://github.com/andshrew/PlayStation-Titles)
   （MIT，Copyright (c) 2022 andshrew，数据采自 PlayStation Store）编译，约 7.1 万个 PS4/PS5 标题 ID。
   上游每行是某一地区的标题 ID，中文名只出现在亚洲区（HP/HB）的 ID 上；生成脚本按商店 `conceptId`
   把中文名扩散到同一游戏其他地区的 ID（例如欧版 `PPSA21567` 也得到「宇宙机器人」），
   日文汉字名不会被当成中文。格式 `{"PPSA21567": ["ASTRO BOT", "宇宙机器人"]}`，去掉 ™ ®。
   上游许可证原文随附在 `titledb/UPSTREAM_LICENSE`。服务端在第一次查询时才载入（约 20 MB 内存）。
2. **PS5 上的游戏自身信息**：列表里是 PS5 文件夹（如 `/mnt/ext0/games/PPSA03671`）且内置库缺名称或中文名时，
   服务端经文件代理读一次 `sce_sys/param.json`（PS5，`localizedParameters.<语言>.titleName`，
   中文取 `zh-Hans` → `zh-Hant`）或 `sce_sys/param.sfo`（PS4，`TITLE_11`/`TITLE_10`/`TITLE`）。
   读到的结果写入 `<data>/titles-cache.json`，之后列目录不再读 PS5；读不到的文件夹 10 分钟内不重试；
   每次请求最多读 5 秒，PS5 不在线时立即停止，未读到的在 `pending` 里返回，下次渲染再查。
   `.ffpfsc` 镜像不读（需要挂载），只靠内置库。

接口：`GET /api/titles?ids=CUSA36464,PPSA01576.ffpfsc&folder=/mnt/ext0/games/PPSA03671`（需登录；
`ids` 可直接传文件名，最多 500 个；`folder` 可重复，最多 64 个，路径受文件代理同样的白名单约束）。
返回 `{titles: {ID: {name, en, zh, source}}, pending: [ID…]}`，`source` 为 `builtin` / `console` /
`builtin+console`。前端渲染行时先显示文件名，再把当前页面所有 ID 合成一次请求，结果在内存里缓存（含未命中）。

**更新内置库**（上游基本每天更新）：

```sh
python3 scripts/build_titles.py                 # 下载上游 main，记录所用 commit
python3 scripts/build_titles.py --ref <commit>  # 固定某个上游版本
```

下载走 `HTTPS_PROXY`；若只能用 SOCKS 代理，先用 `curl --socks5-hostname <代理> -O` 下载上游的
`All_Titles.tsv` 与 `LICENSE` 到一个目录，再 `python3 scripts/build_titles.py --source <目录>`。
生成后跑 `python3 -m unittest tests.test_titles` 并提交 `titledb/`。部署时把 `titledb/titles.json`
放到 `[paths] titles`（默认 `/opt/ps5-jb-server/titledb/titles.json`）；未配置时也会找
`server/` 同级的 `titledb/` 或程序同目录的 `titles.json`（旧的平铺 `/opt/ps5-manager` 布局）。

## 依赖

对端需要有 zftpd（FTP）或 `tom-ps5-usbnet.elf`（或任何实现上述 `/fs/*` 的服务）在跑，
并在管理页「PS5 文件」里选择它的地址（默认从已发现的 PS5 主机里选）。
