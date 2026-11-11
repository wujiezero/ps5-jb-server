# 内置域名说明（设计）

日期：2026-11-11　状态：已获用户确认

## 问题

`server/policy.py` 的 `LOCAL_DOMAINS` 硬编码 3 个域名，`dns_service.py` 对它们直接回答本机地址
（日志 `action='local'`），不需要白名单。但页面上没有任何说明：

- 「网络活动」把这类记录显示为「允许」，看起来像是白名单放行，用户在自己的白名单里却找不到它；
- 「功能说明」里 `dns` 写的「只解析白名单里的域名，其余查询一律拒绝」、`manual` 只提
  `manuals.playstation.net`，都与实际行为不符。

## 方案

后端是唯一数据源，前端不重复抄写域名列表。

### 后端

| 文件 | 改动 |
| --- | --- |
| `server/server.py` | `state()` 增加 `local_domains`，值为 `list(LOCAL_DOMAINS)`（导入自 `policy`） |
| `server/activity.py` | `_collect_dns()` 生成的 DNS 事件增加 `local: action == 'local'`；DNS 引擎日志的 `action` 是既有事实，无需再导入 `LOCAL_DOMAINS` |

旧版后端返回的 payload 没有这两个字段时，前端自动隐藏说明块、不加标注，不报错。

### 前端

| 文件 | 改动 |
| --- | --- |
| `web/index.html` | 白名单卡片内「允许的域名」字段帮助文字之后、`fieldset` 内加 `#builtin-domains` 说明块（复用 `.policy-note` 样式；实测放在 fieldset 之后会被推到卡片底部，不够显眼） |
| `web/app.js` | ① `renderState()` 调 `renderBuiltinDomains(data.local_domains, status.host_ip)`；② `normalizeActivity()` 保留 `local`；③ `activityRow()` 对 `kind === 'dns' && local` 的行，在**域名列**的域名后面加「本机劫持」标签（结果列保持只有「允许」，用户确认不要挤在结果列）；④ 修正 `HELP.dns` / `HELP.manual` / `HELP.whitelist` / `HELP.activity` 文案 |
| `web/style.css` | `#builtin-domains` 内的域名 chip 列表；`.domain-note` 标签（跟域名后面的「本机劫持」） |

### 文案（用户确认版）

说明块标题「内置域名 · 始终由本机应答」，域名 chip 由接口渲染，正文：

> 这 3 个域名固定解析到本机（当前 `192.168.5.35`），用来接住 PS5 的「用户手册」入口与连网检测；
> 不查询上游 DNS，也不会产生出网流量。它们不在上面的白名单里，但始终生效——加进白名单也不会改变行为，所以不用填。

不逐个断言域名用途：`ena.net.playstation.net` 的用途没有权威出处，只按事实说明「固定指向本机」。

## 测试

- `tests/test_activity.py`：`action='local'` 的 DNS 事件 `local=True`，`action='allowed'` 的为 `False`（且仍 `verdict='allowed'`）。
- `tests/test_review_fixes.py`：`Application.state()['local_domains']` 等于 3 个内置域名。
- `tests/ui-interactions.js`：`normalizeActivity` 保留 `local`；`activityRow` 对 `local` 行在域名列渲染「本机劫持」、结果列不含该标记、普通行不渲染；`renderBuiltinDomains` 渲染 chip 与主机 IP、空列表时隐藏。
- 回归：`.venv/bin/python -m unittest discover -s tests`、`node --test tests/ui-interactions.js`。

## 不做

- 不新增静态文件（服务更新器的 `RUNTIME_FILES` 是固定白名单）。
- 不改白名单校验、DNS 判定、nftables 规则、筛选与批量放行逻辑：内置域名本来就不可勾选（`allowed_now === true`）。
- 不动活动接口的分页/计数；`local` 只是附加字段。
