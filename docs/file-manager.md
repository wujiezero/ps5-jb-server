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
- 上传大小受 `settings.max_upload` 限制；请求必须带会话与 `X-CSRF-Token`。
- 文件名只从 `X-Filename` 头取，且不允许 `/` 与 `..`。

## 依赖

对端需要有 payload 在跑（`tom-ps5-usbnet.elf` 或任何实现上述 `/fs/*` 的服务），
并在管理页「PS5 文件」里选择它的地址（默认从已发现的 PS5 主机里选，端口默认 80）。
