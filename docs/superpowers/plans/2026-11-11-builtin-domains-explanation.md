# 内置域名说明 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 用户已批准设计（见 `docs/superpowers/specs/2026-11-11-builtin-domains-explanation-design.md`），本轮只改仓库 + 测试，不部署到真机。

**Goal:** 在页面上说明 `LOCAL_DOMAINS` 这 3 个内置域名，使「网络活动」里的「允许」不再被误读成白名单放行。

**Architecture:** 后端 `policy.LOCAL_DOMAINS` 仍是唯一数据源，`/api/state` 暴露 `local_domains`，活动 DNS 事件带 `local` 标记；前端用这两个字段渲染说明块与「本机劫持」标注，字段缺失时静默降级。

**Tech Stack:** Python 3 标准库 + dnspython（服务端，`unittest` 测试），原生 HTML/CSS/JS（前端，`node --test` 测试）。

---

### Task 1: 后端 — 活动 DNS 事件带 `local` 标记

**Files:**
- Modify: `server/activity.py`（`_collect_dns()` 的事件字典，约 321-337 行）
- Test: `tests/test_activity.py`（`ObserverTests` 类内新增）

- [ ] **Step 1: Write the failing test**

在 `tests/test_activity.py` 的 `ObserverTests` 里 `test_dns_events_are_included` 之后插入：

```python
    def test_dns_events_mark_builtin_local_domains(self):
        # dns_service logs action='local' only for policy.LOCAL_DOMAINS answers.
        rows = [{'seq': 2, 'time': '2026-01-01T00:00:01+08:00', 'client': CLIENT,
                 'name': 'manuals.playstation.net', 'qtype': 'A', 'action': 'local',
                 'detail': HOST_IP},
                {'seq': 1, 'time': '2026-01-01T00:00:00+08:00', 'client': CLIENT,
                 'name': 'ps5.playstation.net', 'qtype': 'A', 'action': 'allowed',
                 'detail': ''}]
        observer = FlowObserver(
            conntrack=lambda: '', classifier=self.classifier, labels=self.store,
            dns_lookup=lambda ip: (None, None), is_allowed=lambda ip: False,
            lan=LAN, host_ip=HOST_IP, clock=lambda: self.now, dns_events=lambda: rows)
        observer.poll_once()
        events = {event['domain']: event
                  for event in observer.snapshot()['events'] if event['kind'] == 'dns'}
        self.assertTrue(events['manuals.playstation.net']['local'])
        self.assertEqual(events['manuals.playstation.net']['verdict'], 'allowed')
        self.assertFalse(events['ps5.playstation.net']['local'])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_activity -v`（在仓库根目录；若 `tests` 不是包则用 `cd tests && ../.venv/bin/python -m unittest test_activity -v`）
Expected: FAIL — `KeyError: 'local'`

- [ ] **Step 3: Write minimal implementation**

`server/activity.py` 的 `_collect_dns()` 里，事件字典中 `'kind': 'dns',` 之前加一行：

```python
                'local': action == 'local',
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_activity -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/activity.py tests/test_activity.py
git commit -m "活动日志：内置域名的 DNS 记录带 local 标记"
```

---

### Task 2: 后端 — `/api/state` 暴露 `local_domains`

**Files:**
- Modify: `server/server.py:32`（`from policy import ...`）与 `Application.state()` 返回值（约 668 行）
- Test: `tests/test_review_fixes.py`（新增类）

- [ ] **Step 1: Write the failing test**

`tests/test_review_fixes.py` 顶部 import 行改为引入 `Application`：

```python
from server import Application, AuthManager, RepoManager, change_password, create_engine  # noqa: E402
```

文件末尾新增：

```python
class BuiltinDomainExposureTests(unittest.TestCase):
    """The UI explains LOCAL_DOMAINS from the API, not from a duplicated list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConfigStore(Path(self.tmp.name) / 'config.json',
                                 {'domains': [], 'networks': [], 'upstream_dns': '192.0.2.1'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_exposes_builtin_domains(self):
        engine = create_engine(self.store, '192.0.2.20', '192.0.2.0/24', firewall=NullFirewall())
        app = Application(engine)
        app.service_info = lambda: {'units': {'host': {'name': 'ps5-jb-host', 'state': 'unknown'}},
                                    'ports': {}, 'paths': {}}
        state = app.state()
        self.assertEqual(state['local_domains'],
                         ['manuals.playstation.net', 'ena.net.playstation.net',
                          'www.msftconnecttest.com'])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_review_fixes -v`
Expected: FAIL — `KeyError: 'local_domains'`

- [ ] **Step 3: Write minimal implementation**

`server/server.py` 第 32 行：

```python
from policy import ConfigStore, Firewall, LOCAL_DOMAINS
```

`Application.state()` 的返回字典里，`'config': config,` 之后加：

```python
            'local_domains': list(LOCAL_DOMAINS),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_review_fixes -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/server.py tests/test_review_fixes.py
git commit -m "状态接口：暴露内置域名列表"
```

---

### Task 3: 前端 — 说明块、活动行标注、功能说明文案

**Files:**
- Modify: `web/index.html`（约 294 行，`#domains-error` 之后）
- Modify: `web/app.js`（`normalizeActivity` 2162 行附近、`activityRow` 2297 之后、`renderState` 815 行附近、`HELP` 7692/7696/7707/7711 行）
- Modify: `web/style.css`（`.activity-kind` 规则后加 `.domain-note`、`.policy-note` 规则后加 chip 列表）
- Test: `tests/ui-interactions.js`（`fixture()` 暴露的函数列表 + 文件末尾新增 2 个 test）

- [ ] **Step 1: Write the failing tests**

`tests/ui-interactions.js` 的 `globalThis.subject = { ... }` 里、`activityVisibleEvents,` 之后加三个名字：

```js
      normalizeActivity, activityRow, renderBuiltinDomains,
```

文件末尾新增：

```js
test('builtin DNS rows mark 本机劫持 after the domain, not in the verdict cell', () => {
  const ui = fixture();
  const normalized = ui.normalizeActivity({ events: [
    { kind: 'dns', domain: 'manuals.playstation.net', verdict: 'allowed', local: true, port: 53 },
    { kind: 'dns', domain: 'ps5.playstation.net', verdict: 'allowed', port: 53 }
  ] });
  assert.equal(normalized.events[0].local, true);
  assert.equal(normalized.events[1].local, false);
  const localRow = ui.activityRow(normalized.events[0]);
  const otherRow = ui.activityRow(normalized.events[1]);
  const domainCell = (row) => row.children[3];
  const verdictCell = (row) => row.children[6];
  assert.match(textOf(domainCell(localRow)), /manuals\.playstation\.net 本机劫持/);
  assert.doesNotMatch(textOf(verdictCell(localRow)), /本机劫持/);
  assert.match(textOf(verdictCell(localRow)), /允许/);
  assert.doesNotMatch(textOf(otherRow), /本机劫持/);
});

test('builtin domain block lists server domains and hides when absent', () => {
  const ui = fixture();
  ui.renderBuiltinDomains(['manuals.playstation.net', 'ena.net.playstation.net'], '192.0.2.10');
  assert.equal(ui.node('builtin-domains').hidden, false);
  assert.equal(ui.node('builtin-domain-list').children.length, 2);
  assert.equal(ui.node('builtin-host-ip').textContent, '192.0.2.10');
  ui.renderBuiltinDomains([], '192.0.2.10');
  assert.equal(ui.node('builtin-domains').hidden, true);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test tests/ui-interactions.js`
Expected: FAIL — `ui.normalizeActivity is not a function`（未暴露）

- [ ] **Step 3: Write minimal implementation**

`web/index.html`，白名单卡片内 `<p id="domains-error" ...></p>` 之后（即「允许的域名」字段帮助文字下方、`fieldset` 内）：

```html
              <div class="policy-note" id="builtin-domains" hidden>
                <strong>内置域名 · 始终由本机应答</strong>
                <p class="builtin-domain-list" id="builtin-domain-list"></p>
                <p>这 3 个域名固定解析到本机（当前 <span class="mono" id="builtin-host-ip">—</span>），用来接住 PS5 的「用户手册」入口与连网检测；不查询上游 DNS，也不会产生出网流量。它们不在上面的白名单里，但始终生效——加进白名单也不会改变行为，所以不用填。</p>
              </div>
```

`web/app.js`：

1) `normalizeActivity` 的 `events.push({` 里、`allowedNow:` 之前加：

```js
        local: entry.local === true,
```

2) `activityRow` 的 `else if (event.verdict === 'allowed' && event.allowedNow === false)` 之后加：

```js
    // 内置域名由本机 DNS 直接应答，不是白名单放行；单独标出来，避免误读。
    if (event.kind === 'dns' && event.local) {
      const note = document.createElement('span');
      note.className = 'domain-note';
      note.textContent = '本机劫持';
      domainCell.append(note);   // 域名列：domainCell 在 activityRow 里已创建
    }
```

3) `renderState` 里 `$$('[data-host-ip]').forEach(...)` 之后加：

```js
    renderBuiltinDomains(data.local_domains, status.host_ip);
```

4) `renderState` 之前（例如 `renderServiceInfo` 定义前）新增函数：

```js
  function renderBuiltinDomains(localDomains, hostIp) {
    const block = $('builtin-domains');
    const names = Array.isArray(localDomains) ? localDomains.map((value) => str(value)).filter(Boolean) : [];
    block.hidden = names.length === 0;
    const list = $('builtin-domain-list');
    list.replaceChildren();
    for (const name of names) {
      const chip = document.createElement('code');
      chip.className = 'builtin-domain';
      chip.textContent = name;
      list.append(chip);
    }
    $('builtin-host-ip').textContent = asText(hostIp, location.hostname || '本机');
  }
```

5) `HELP` 文案改为：

```js
    dns: { title: 'DNS 服务', body: '本机在 UDP / TCP 53 端口为 PS5 解析域名，只解析「访问策略」白名单里的域名，其余查询一律拒绝，PS5 因此连不上系统更新、遥测等服务。另有 3 个内置域名（用户手册入口等）固定由本机应答，见「访问策略」里的说明。',
      steps: ['在 PS5 的网络设置里把主 DNS 指向本机（见概览「PS5 网络设置」）。', '每次查询及结果都记录在「网络活动」。'] },
```

```js
    manual: { title: '用户手册入口', body: 'PS5 的「用户手册」会访问 manuals.playstation.net 等 3 个内置域名。本机把这 3 个域名解析到自己（不需要、也不应该加入白名单），并在 HTTPS 443 端口托管越狱页面，在 PS5 上打开用户手册即可进入。',
      steps: ['PS5：设置 → 用户指南、健康与安全以及其他信息 → 用户手册。', '状态异常时看设置 → 服务信息里的用户手册服务（ps5-jb-host）。'] },
```

`whitelist` 的 `steps` 数组加一项（放在最后）：

```js
'用户手册、连网检测用的内置域名始终由本机应答，不用写在这里。'
```

`activity` 的 `steps` 数组加一项（放在最后）：

```js
'标「本机劫持」的 DNS 记录来自内置域名，由本机直接应答，不是白名单放行。'
```

`web/style.css`，`.activity-kind` 规则之后加：

```css
/* 内置域名（本机劫持）标记：跟在域名后面，和结果列的白名单判定分开。 */
.domain-note {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 8px;
  border-radius: 99px;
  background: var(--bar);
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.6;
  white-space: nowrap;
  vertical-align: middle;
}
```

`.policy-note p { ... }` 之后加：

```css
.policy-note .builtin-domain-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.policy-note .builtin-domain-list .builtin-domain { font-size: 0.75rem; font-weight: 540; }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests/ui-interactions.js`
Expected: PASS（全部用例，含新增 2 个）

Run: `node --check web/app.js`
Expected: exit 0

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/app.js web/style.css tests/ui-interactions.js
git commit -m "页面：说明内置域名，活动记录的放行结果标注本机劫持"
```

---

### Task 4: 回归与验收

**Files:** 无源码改动（必要时修正 Task 1-3 的疏漏）

- [ ] **Step 1: 全量 Python 测试**

Run: `.venv/bin/python -m unittest discover -s tests`
Expected: OK（不少于改动前的 435 项）

- [ ] **Step 2: 前端语法与 UI 测试**

Run: `node --check web/app.js && node --test tests/ui-interactions.js`
Expected: exit 0、全部 PASS

- [ ] **Step 3: 差异检查**

Run: `git diff --check && git status --short`
Expected: 无空白错误；工作区只剩预期文件

- [ ] **Step 4: 浏览器实测（可选，建议）**

用会话临时目录的 mock 页面（`.claude/launch.json` 的 mock 脚本不在仓库里，需按 `tests/browser/console-smoke.js` 的夹具重建）确认：白名单页说明块、活动域名列「本机劫持」标签、320/390 宽度不溢出。

- [ ] **Step 5: 记录**

在 `.claude/PROJECT.md` 末尾「工作记录」加一条：本次改动内容、测试结果、**未部署到 .35**。
