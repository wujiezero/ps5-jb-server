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

## 分卷压缩包解压安装（RAR5）

`server/archive.py`，由 `TransferManager` 作为 `kind: extract` 的任务执行，与普通 NAS 传输共用队列、进度与取消。

PS5 上的 ShadowMount+ 只认两种形态：单文件镜像（`PPSAxxxxx.ffpfsc`）和解开的游戏文件夹（含 `eboot.bin`、
`sce_sys/param.json`），扫描目录由 PS5 上 `/data/shadowmount/config.ini` 的 `[scan] path` 决定（真机为
`/mnt/ext0/games`、`/data/homebrew`）。分卷 `*.partNN.rar` 原样传上去不会被识别，所以在服务器上解开：

1. `GET /api/nas/archive?id=&path=`：从所在目录的列表里按名字找齐同一组分卷（按编号排序、检查缺号，
   忽略 macOS 的 `._` 元数据文件），然后**预扫描全部包头**：NAS 客户端的 `read_at`（WebDAV `Range`、
   SMB `seek`、FTP `REST`，FTP 每次读用新连接）按包头里的长度跳过数据区，每次读 64 KiB。返回分卷数、
   压缩包大小、解压后大小、文件数、标题 ID 猜测，`checked: true`。25 卷 100 GB 的包约 375 次请求、1–2 秒。
   服务器不支持按偏移读时退回只读第一卷开头 256 KiB（`checked: false`，解压后大小未知）。
2. `POST /api/nas/extract {id, path|paths, dest, mode, auto_register, host, title}`：`mode` 为 `install`（在 `dest` 下新建游戏文件夹）或
   `merge`（把内容覆盖进 `dest` 这个已有游戏文件夹）。服务会先只读规划选中的源，补齐同组分卷；`install` 时按归档内
   `eboot.bin + sce_sys/param.json|param.sfo` 的真实元数据定位游戏根，无论外面有几层 wrapper，最终固定为
   `dest/<title ID>/`。`auto_register` 默认开启，解压成功后调用本机 ps5upload engine 的 `/api/ps5/app/register`
   添加主屏幕图标；注册失败不会撤销已完成的文件安装，而是以 `done_with_warnings` 返回。
3. 任务里各分卷依次 `client.open()`，拼成一条字节流交给 libarchive（`stream_reader`）；每解出一个文件就直接
   `FileProxy.upload` 到 PS5 上的 `<父目录>/.extract-<任务 ID>/…`，不落服务器磁盘。进度按已读取的压缩包字节计。
4. 全部写完后：按真实游戏根目录执行一次同盘改名到 `dest/<title ID>`；旁边的 `.pkg`/金手指 ZIP 作为独立 sidecar 保留，README 等无关 wrapper 文件丢弃；合并模式逐项覆盖。
   同盘改名是瞬间完成的。任何失败或取消都删除整个 `.extract-*` 目录。PKG-only 归档不会被伪装成可运行 dump，应走 ps5upload 的
   `/api/pkg/install` 并等待 `phase=done, verdict=installed`；金手指 ZIP 只会被安全解析并给出 etaHEN 文件布局计划，SHN/MC4 不会被错误当 JSON 导入。

**为什么要有自己的包头解析器（`Rar5Scanner`）**：Debian 12 的 libarchive 3.6.2 读固实（solid）多卷包时会
静默少掉最后几个条目且不报错（2026-11-10 用 `archive_read_open_filenames` 复现，与拼接方式无关）。解析器与
libarchive 读同一批字节，只解析块头、按长度跳过数据区，独立记下包里每个文件；结束时两边对不上就判失败。它也负责：
分卷签名与卷号顺序、最后一卷的结束标记（缺后面的卷时报「可能缺少后面的分卷」）、非最后一卷末尾的 0 字节填充。

**不支持（开始前就拒绝并说明）**：带密码的包（文件加密或文件名加密）、RAR4（包括 `.rar + .r00` 旧式分卷）、
RAR 7 的 v7 算法（字典 >4 GB）。没有 unrar 引擎时，固实包和字典超过 64 MiB 的包也会被拒（libarchive 3.6.2 的上限，
2026-11-10 实测 128 MiB 报 “Declared dictionary size is not supported”）；字典所需内存超过服务器内存时同样拒绝。这些请先在电脑或 NAS 上解压，再用「传到 PS5」
传文件夹。符号链接 / 硬链接条目会跳过。

**性能（2026-11-10 在 .35 实测）**：解压在 nas-transfer 线程里进行（该线程 nice 10），libarchive 的解码在 C 里
释放 GIL。CUSA36464.rar（1.47 GB，32 MB 字典，压缩方法 1）从 WebDAV(HTTPS) NAS 解到 PS5 zftpd：23.4 s，
约 64 MB/s，服务进程占满一个核（WebDAV TLS + 解码 + FTP 发送），内存约 95 MB。

**两个解压引擎**：检查阶段扫完全部包头后选择引擎（`archive.choose_engine`）：

| 引擎 | 何时使用 | 怎么读分卷 | 进度计量 |
| --- | --- | --- | --- |
| libarchive（`python3-libarchive-c`） | 非 solid 且字典 ≤64 MiB | 服务进程内，NAS 客户端直接给字节流 | 已读取的压缩包字节 |
| unrar（`libunrar5`） | solid，或字典 >64 MiB（实测真实发布包常见 4 GB 字典） | 独立工作单元里用 rclone 只读挂载 NAS 目录 | 解压出的字节 |

unrar 引擎为什么要单独的进程：`ps5-manager` 单元有 `CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE`、
`NoNewPrivileges`、`MemoryMax=256M`，既不能挂 FUSE，也放不下几 GB 的解压窗口；C 库出问题也不能拖垮 DNS。
所以服务用 `systemd-run --unit=ps5-extract-<任务> --collect -p MemoryMax=<字典+768 MiB> -p Nice=10 ...` 启动
`server/extract_worker.py`，任务说明写在 `<data>/extract/<任务>.json`（不含账号，工作进程自己用 `nas.key` 解密），
进度写在同目录的 `<任务>.status.json`，服务每 0.5 秒读一次。取消就是 `systemctl stop`（工作进程收到 SIGTERM 后清理暂存目录）。
工作进程意外退出（例如内存不足被杀）且没写出最终状态时，服务判失败并删除暂存目录，日志见 `journalctl -u ps5-extract-<任务>`。

工作进程：`rclone mount NAS:<NAS 路径>/<分卷所在目录> /run/ps5-extract/<任务> --read-only --vfs-cache-mode off`
（rclone 配置只通过环境变量 `RCLONE_CONFIG_NAS_*` 传入，密码经 `rclone obscure -` 从标准输入编码，不出现在命令行），
再用 `archive.prescan` 读一遍包头作为核对清单，然后 libunrar 以 `RAR_TEST` 模式解压、`UCM_PROCESSDATA` 回调把数据交给
生产者线程 → 有界队列 → `FileProxy.upload`。libunrar 只认可随机读的真实文件（命名管道在打开时就失败，2026-11-10 实测），
所以必须挂载。需要：`libunrar5`、`rclone`、`fuse3`、`/dev/fuse`（PVE 里给 LXC 勾选 FUSE 功能）。

**实测（2026-11-10，.35，LXC 6 GB 内存）**：rclone 挂载 WebDAV(HTTPS) 顺序读 112 MB/s；
CUSA36464.rar（32 MB 字典，libarchive）64 MB/s；CUSA20499（4 GB 字典，unrar）见 PROJECT.md 的记录。

**测试**：`tests/test_archive.py` 用 `tests/fixtures/rar/` 里的真实 RAR5 小样本（`make.sh` 生成，需要 `rar`），
无 libarchive 时解压类用例跳过。2026-11-10 在 Debian 12 容器（libarchive 3.6.2、python3-libarchive-c 2.9）里
用 rclone WebDAV 作 NAS、rclone FTP 作 PS5 做过端到端：7 卷 32 MB 包解出 204 个文件，与源目录逐字节一致（含中文名、空目录）。

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
