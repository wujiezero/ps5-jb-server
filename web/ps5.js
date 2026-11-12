'use strict';

/* ── PS5 主机 ───────────────────────────────────────────────────────── */

const ps5HostByIp = (ip) => ps5.hosts.find((host) => host.ip === ip) || null;
const ps5SelectedHost = () => ps5HostByIp(ps5SelectedIp);

const ps5VersionLabel = (value) => {
  const text = str(value);
  if (!text) return '';
  return /^v/i.test(text) ? text : `v${text}`;
};

const ps5Clock = (value) => {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return asText(value, '');
  return parsed.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
};

const ps5InSubnet = (ip, cidr) => {
  const parts = String(cidr || '').split('/');
  if (!ipv4(ip) || !ipv4(parts[0])) return null;
  const bits = Number(parts[1]);
  if (!Number.isInteger(bits) || bits < 0 || bits > 32) return null;
  const toNumber = (value) => value.split('.').reduce((total, part) => total * 256 + Number(part), 0);
  const mask = bits === 0 ? 0 : (0xFFFFFFFF << (32 - bits)) >>> 0;
  return ((toNumber(ip) & mask) >>> 0) === ((toNumber(parts[0]) & mask) >>> 0);
};

function normalizePs5Host(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const ip = str(entry.ip);
  if (!ipv4(ip)) return null;
  const running = [];
  for (const item of Array.isArray(entry.running) ? entry.running : []) {
    if (!item || typeof item !== 'object') continue;
    const pid = Number(item.pid);
    const name = str(item.name) || (Number.isInteger(pid) && pid > 0 ? `PID ${pid}` : '未知进程');
    running.push({ pid: Number.isInteger(pid) && pid > 0 ? pid : null, name });
  }
  const autoloadRaw = entry.autoload && typeof entry.autoload === 'object' ? entry.autoload : {};
  const statusRaw = autoloadRaw.status && typeof autoloadRaw.status === 'object' ? autoloadRaw.status : {};
  const count = (value) => { const number = Number(value); return Number.isFinite(number) && number >= 0 ? Math.floor(number) : null; };
  const repoPayloads = Number(entry.repo_payloads);
  const payloads = [];
  for (const item of Array.isArray(entry.payloads) ? entry.payloads : []) {
    if (!item || typeof item !== 'object' || !str(item.slot)) continue;
    payloads.push({ slot: str(item.slot), filename: str(item.filename) || str(item.slot) });
  }
  const items = [];
  for (const item of Array.isArray(autoloadRaw.items) ? autoloadRaw.items : []) {
    if (!item || typeof item !== 'object' || !str(item.key)) continue;
    items.push({
      key: str(item.key),
      filename: str(item.filename) || str(item.key),
      installed: item.installed === true,
      delayMs: count(item.delay_ms) || 0,
      outcome: ['success', 'failed', 'skipped'].includes(str(item.outcome)) ? str(item.outcome) : ''
    });
  }
  if (!items.length && Array.isArray(autoloadRaw.list)) {
    for (const key of autoloadRaw.list.map((name) => str(name)).filter((name) => name && !name.startsWith('!'))) {
      items.push({ key, filename: key, installed: false, delayMs: 0, outcome: '' });
    }
  }
  const pluginUpdates = {};
  const updatesRaw = entry.updates && typeof entry.updates === 'object' ? entry.updates : {};
  for (const [filename, info] of Object.entries(updatesRaw)) {
    if (!info || typeof info !== 'object' || !['current', 'outdated', 'newer', 'maybe'].includes(str(info.state))) continue;
    const runningPid = Number(info.running);
    pluginUpdates[filename] = {
      state: str(info.state), confident: info.confident === true, slot: str(info.slot),
      installed: str(info.installed), installedVersion: str(info.installed_version), repoVersion: str(info.repo_version),
      running: Number.isInteger(runningPid) && runningPid > 0 ? runningPid : null
    };
  }
  const powerRaw = entry.power && typeof entry.power === 'object' ? entry.power : {};
  const power = {
    state: ['on', 'standby', 'off'].includes(str(powerRaw.state)) ? str(powerRaw.state) : entry.online === true ? 'on' : 'off',
    name: str(powerRaw.name),
    firmware: str(powerRaw.firmware),
    app: str(powerRaw.running_app),
    paired: powerRaw.paired === true,
    engine: powerRaw.engine === true,
    job: powerRaw.job && typeof powerRaw.job === 'object' ? {
      state: str(powerRaw.job.state), message: str(powerRaw.job.message), error: str(powerRaw.job.error)
    } : null
  };
  const lastSeen = Number(entry.last_seen);
  return {
    ip,
    power,
    // Listed from hosts.json until this start's scan reaches it (older servers omit it).
    remembered: entry.remembered === true,
    lastSeen: Number.isFinite(lastSeen) && lastSeen > 0 ? lastSeen * 1000 : null,
    rememberedName: str(entry.name),
    rememberedFirmware: str(entry.firmware),
    online: entry.online === true,
    elfldr: entry.elfldr === true,
    pldmgr: entry.pldmgr === true,
    pldmgrPort: count(entry.pldmgr_port) || 8084,
    version: str(entry.version),
    sourceAttached: entry.source_attached === true,
    sourceUrl: str(entry.source_url),
    repoPayloads: Number.isFinite(repoPayloads) && repoPayloads >= 0 ? Math.floor(repoPayloads) : null,
    installed: Array.isArray(entry.installed) ? [...new Set(entry.installed.map((name) => str(name)).filter(Boolean))] : [],
    payloads,
    running,
    pluginUpdates,
    autoload: {
      enabled: autoloadRaw.enabled === true,
      startDelay: count(autoloadRaw.start_delay) === null ? 5 : count(autoloadRaw.start_delay),
      leadMs: count(autoloadRaw.lead_ms) || 0,
      items,
      list: items.map((item) => item.key),
      status: {
        state: str(statusRaw.state),
        remaining: count(statusRaw.remaining),
        done: count(statusRaw.done),
        total: count(statusRaw.total),
        failed: count(statusRaw.failed),
        current: str(statusRaw.current)
      }
    },
    lastError: str(entry.last_error)
  };
}

function normalizePs5(data) {
  const discoveryRaw = data && typeof data.discovery === 'object' && data.discovery !== null ? data.discovery : {};
  const hosts = [];
  for (const entry of Array.isArray(data && data.hosts) ? data.hosts : []) {
    const host = normalizePs5Host(entry);
    if (host) hosts.push(host);
  }
  const scanned = Number(discoveryRaw.scanned);
  const duration = Number(discoveryRaw.duration_ms);
  return {
    discovery: {
      subnet: str(discoveryRaw.subnet),
      scanned: Number.isFinite(scanned) && scanned >= 0 ? Math.floor(scanned) : 0,
      lastScan: str(discoveryRaw.last_scan),
      scanning: discoveryRaw.scanning === true,
      durationMs: Number.isFinite(duration) && duration >= 0 ? Math.floor(duration) : 0,
      enabled: discoveryRaw.enabled !== false
    },
    hosts
  };
}

function applyPs5(data) {
  // A successful read supersedes any earlier scan/host error banner.
  setPs5Error('');
  const next = normalizePs5(data);
  if (dialogsOpen() || ps5Busy) {
    ps5Pending = next;
    return next;
  }
  assignPs5(next);
  return next;
}

function assignPs5(next) {
  for (const host of next.hosts) {
    if (host.power.job && ['waking', 'signing_in'].includes(host.power.job.state)) watchPs5Power(host.ip);
  }
  ps5.loaded = true;
  ps5.discovery = next.discovery;
  ps5.hosts = next.hosts;
  if (ps5SelectedIp && !ps5HostByIp(ps5SelectedIp)) {
    ps5SelectedIp = '';
    ps5Draft = null;
  }
  if (!ps5SelectedIp && ps5.hosts.length) ps5SelectedIp = ps5.hosts[0].ip;
  renderPs5();
}

function flushPs5Pending() {
  if (!ps5Pending || !authenticated || dialogsOpen() || ps5Busy) return;
  const next = ps5Pending;
  ps5Pending = null;
  assignPs5(next);
}

function updatePs5Host(raw) {
  const host = normalizePs5Host(raw);
  if (!host) return;
  const index = ps5.hosts.findIndex((entry) => entry.ip === host.ip);
  if (index >= 0) ps5.hosts[index] = host;
  else ps5.hosts.push(host);
  if (!ps5SelectedIp) ps5SelectedIp = host.ip;
}

async function refreshPs5() {
  if (!authenticated || ps5Loading || ps5Busy || dialogsOpen()) return false;
  ps5Loading = true;
  const requestEpoch = epoch;
  updatePs5Controls();
  try {
    const data = await api('/api/ps5');
    if (requestEpoch !== epoch) return false;
    ps5FetchedAt = Date.now();
    setPs5Error('');
    applyPs5(data);
    return true;
  } catch (error) {
    if (requestEpoch !== epoch) return false;
    if (error.status === 401) {
      showLogin(error.message);
      return false;
    }
    setPs5Error(`主机状态读取失败：${error.message}`);
    return false;
  } finally {
    ps5Loading = false;
    renderPs5();
  }
}

function setPs5Error(message) {
  const node = $('ps5-error');
  node.textContent = message || '';
  node.hidden = !message;
}

function setPs5AddError(message) {
  const node = $('ps5-add-error');
  node.textContent = message || '';
  node.hidden = !message;
  const input = $('ps5-add-ip');
  if (message) input.setAttribute('aria-invalid', 'true');
  else input.removeAttribute('aria-invalid');
}

function ps5Begin(kind, ip, extra = {}) {
  stopPolling();
  ps5Busy = true;
  ps5Action = { kind, ip, ...extra };
  updateControls();
}

function ps5End() {
  ps5Busy = false;
  ps5Action = null;
  updateControls();
  renderPs5();
  flushPs5Pending();
  schedulePoll();
}

function renderPs5() {
  if (!$('ps5-scan-info')) return;
  renderQuickLinks();
  renderPs5ScanInfo();
  renderPs5HostCount();
  const disabled = $('ps5-disabled-note');
  if (ps5.loaded && !ps5.discovery.enabled) {
    disabled.textContent = '服务器配置已关闭 PS5 主机控制（ps5_control = false），扫描与操作可能不可用。';
    disabled.hidden = false;
  } else {
    disabled.textContent = '';
    disabled.hidden = true;
  }
  renderPs5Hosts();
  renderPs5Detail();
  updatePs5Controls();
}

function renderPs5ScanInfo() {
  const node = $('ps5-scan-info');
  const scanning = Boolean(ps5Action && ps5Action.kind === 'scan');
  const subnet = ps5.discovery.subnet ? `网段 ${ps5.discovery.subnet}` : '网段未知';
  if (scanning) {
    node.textContent = `扫描中… · ${subnet}`;
    return;
  }
  if (!ps5.loaded) {
    node.textContent = ps5Loading ? '正在发现局域网内的主机…' : '登录后自动扫描局域网内的主机。';
    return;
  }
  const discovery = ps5.discovery;
  if (discovery.scanning) {
    node.textContent = `扫描中… · ${subnet}`;
    return;
  }
  if (!discovery.lastScan) {
    node.textContent = `尚未扫描 · ${subnet}`;
    return;
  }
  const scanned = discovery.scanned > 0 ? ` · 已扫描 ${discovery.scanned} 个地址` : '';
  const duration = discovery.durationMs > 0 ? ` · 耗时 ${discovery.durationMs} ms` : '';
  node.textContent = `扫描于 ${ps5Clock(discovery.lastScan)} · ${subnet}${scanned}${duration}`;
}

function renderPs5HostCount() {
  const tag = $('ps5-host-count');
  if (!ps5.loaded) {
    tag.textContent = '扫描中…';
    return;
  }
  const online = ps5.hosts.filter((host) => host.online).length;
  tag.textContent = ps5.hosts.length ? `${ps5.hosts.length} 台 · ${online} 在线` : '0 台';
}

function ps5LoadingNode() {
  const wrap = makeElement('div', 'ps5-loading');
  const spinner = makeElement('span', 'spinner');
  spinner.setAttribute('aria-hidden', 'true');
  wrap.append(spinner, makeElement('p', '', '正在扫描局域网内的 PS5 主机…'));
  return wrap;
}

function ps5EmptyNode() {
  const wrap = makeElement('div', 'ps5-empty');
  wrap.append(makeIcon('i-host'));
  wrap.append(makeElement('p', 'files-empty-title', '还没有发现可管理的主机。'));
  wrap.append(makeElement('p', 'files-empty-text', '确认 PS5 与本机在同一网段，并在 PS5 上运行 pldmgr.elf 或 elfldr 后重新扫描。'));
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'button button-secondary button-small';
  button.dataset.ps5Action = 'scan';
  button.setAttribute('aria-label', '重新扫描局域网内的 PS5 主机');
  const spinner = makeElement('span', 'spinner');
  spinner.setAttribute('aria-hidden', 'true');
  spinner.hidden = true;
  button.append(spinner, makeElement('span', 'button-label', '重新扫描'));
  wrap.append(button);
  return wrap;
}

function createPs5HostCard(host) {
  const card = makeElement('article', 'ps5-host');
  card.dataset.ip = host.ip;

  const head = makeElement('div', 'ps5-host-head');
  const idBox = makeElement('div', 'ps5-host-id');
  const ipLine = makeElement('p', 'ps5-host-ip mono');
  ipLine.dataset.field = 'ip';
  const meta = makeElement('p', 'ps5-host-meta');
  meta.dataset.field = 'meta';
  idBox.append(ipLine, meta);

  const badges = makeElement('div', 'ps5-host-badges');
  for (const [field, text] of [['online', '在线'], ['elfldr', 'elfldr 可用'], ['pldmgr', 'pldmgr 8084'], ['source', '仓库未接入']]) {
    const badge = makeElement('span', 'result-badge', text);
    badge.dataset.field = field;
    badge.dataset.tone = 'neutral';
    badges.append(badge);
  }
  head.append(idBox, badges);

  const counts = makeElement('p', 'ps5-host-counts');
  counts.dataset.field = 'counts';
  const hint = makeElement('p', 'ps5-host-hint', '需要先在主机上运行 pldmgr.elf 或 elfldr。');
  hint.dataset.field = 'hint';
  hint.hidden = true;
  const lastError = makeElement('p', 'ps5-host-last-error');
  lastError.dataset.field = 'last-error';
  lastError.hidden = true;
  const error = makeElement('p', 'field-error');
  error.dataset.field = 'error';
  error.hidden = true;

  const actions = makeElement('div', 'ps5-host-actions');
  const specs = [
    ['select', '详情', '查看主机详情', false],
    ['attach', '接入本服务仓库', '接入本服务仓库', false],
    ['catalog', '安装并运行', '打开插件库', false],
    ['files', '文件', '打开主机文件', false],
    ['autoload', '自动加载', '配置自动加载', false],
    ['running', '停止进程', '打开运行中的进程', false],
    ['power', '唤醒', '唤醒或让主机待机', false],
    ['pair', '配对唤醒', '配对遥控游玩以便唤醒', false],
    ['shutdown', '退出 pldmgr', '退出主机上的 Payload Manager', true]
  ];
  for (const [action, label, ariaSuffix, danger] of specs) {
    const button = makeElement('button', 'button button-secondary button-small');
    button.type = 'button';
    button.dataset.ps5Action = action;
    button.dataset.ip = host.ip;
    button.dataset.label = label;
    button.setAttribute('aria-label', `${ariaSuffix}（${host.ip}）`);
    if (danger) button.dataset.tone = 'danger';
    const spinner = makeElement('span', 'spinner');
    spinner.setAttribute('aria-hidden', 'true');
    spinner.hidden = true;
    button.append(spinner, makeElement('span', 'button-label', label));
    // Phones show the everyday actions inline; the rest go to an action sheet.
    if (!['select', 'files', 'power'].includes(action)) button.dataset.tier = 'secondary';
    actions.append(button);
  }
  const more = makeElement('button', 'button button-secondary button-small ps5-host-more');
  more.type = 'button';
  more.dataset.ps5More = host.ip;
  more.setAttribute('aria-label', `更多操作（${host.ip}）`);
  more.setAttribute('aria-haspopup', 'dialog');
  more.append(makeIcon('i-menu'), makeElement('span', 'button-label', '更多'));
  actions.append(more);

  const busy = makeElement('span', 'ps5-host-busy');
  busy.dataset.field = 'busy';
  busy.hidden = true;
  const busySpinner = makeElement('span', 'spinner');
  busySpinner.setAttribute('aria-hidden', 'true');
  const busyLabel = makeElement('span');
  busyLabel.dataset.field = 'busy-label';
  busyLabel.textContent = '处理中…';
  busy.append(busySpinner, busyLabel);

  card.append(head, counts, hint, lastError, error, actions, busy);
  return card;
}

function syncPs5HostButton(button, host) {
  const action = button.dataset.ps5Action;
  const busyThis = Boolean(ps5Action && ps5Action.ip === host.ip && ps5Action.kind === action);
  let disabled = !authenticated || ps5Busy || !host.online;
  let reason = !host.online ? (host.power.state === 'standby' ? '主机待机中，唤醒后可用' : host.power.state === 'on' ? '主机未运行 pldmgr / elfldr' : '主机当前离线') : '';
  if (action === 'power' || action === 'pair') {
    const label = button.querySelector('.button-label');
    const asleep = host.power.state === 'standby';
    const waking = Boolean(host.power.job && ['waking', 'signing_in'].includes(host.power.job.state));
    if (action === 'power') {
      if (label) label.textContent = waking ? '唤醒中…' : asleep ? '唤醒' : '进入待机';
      button.hidden = !(asleep || host.power.state === 'on');
      disabled = !authenticated || ps5Busy || waking || !host.power.engine || (asleep && !host.power.paired);
      reason = !host.power.engine ? '未部署电源控制引擎（ps5upload-engine）'
        : asleep && !host.power.paired ? '需要先配对遥控游玩才能唤醒' : waking ? '正在唤醒' : '';
    } else {
      button.hidden = host.power.paired || !host.power.engine;
      if (label) label.textContent = '配对唤醒';
      disabled = !authenticated || ps5Busy || host.power.state !== 'on';
      reason = host.power.state !== 'on' ? '配对需要主机处于开机状态' : '';
    }
  } else if (action === 'select') {
    disabled = false;
  } else if (action === 'attach') {
    const label = button.querySelector('.button-label');
    if (label) label.textContent = host.sourceAttached ? '仓库已接入' : '接入本服务仓库';
    if (!host.pldmgr) {
      disabled = true;
      reason = '需要主机运行 pldmgr（8084）';
    } else if (host.sourceAttached) {
      disabled = true;
      reason = '本服务仓库已在 Payload Manager 的源列表中';
    }
  } else if (action === 'catalog') {
    if (!host.pldmgr && !host.elfldr) {
      disabled = true;
      reason = '需要 pldmgr 或 elfldr 才能安装';
    }
  } else if (action === 'autoload' || action === 'running' || action === 'shutdown') {
    if (!host.pldmgr) {
      disabled = true;
      reason = '需要主机运行 pldmgr（8084）';
    }
  }
  if (busyThis) disabled = true;
  button.disabled = disabled;
  button.setAttribute('aria-busy', String(busyThis));
  if (reason) button.title = reason;
  else button.removeAttribute('title');
  const spinner = button.querySelector('.spinner');
  if (spinner) spinner.hidden = !busyThis;
}

function syncPs5HostCard(card, host) {
  card.dataset.ip = host.ip;
  card.dataset.selected = String(host.ip === ps5SelectedIp);
  card.dataset.online = String(host.online);
  card.dataset.busy = String(Boolean(ps5Action && ps5Action.ip === host.ip));
  const ipNode = card.querySelector('[data-field="ip"]');
  if (ipNode) ipNode.textContent = host.ip;
  const meta = card.querySelector('[data-field="meta"]');
  if (meta) {
    const parts = [];
    const name = host.power.name || host.rememberedName;
    const firmware = host.power.firmware || host.rememberedFirmware;
    if (name) parts.push(name);
    if (firmware) parts.push(`固件 ${firmware}`);
    if (host.remembered && !host.online) parts.push(host.lastSeen ? `上次发现于 ${new Date(host.lastSeen).toLocaleString()}` : '上次发现');
    parts.push(host.sourceUrl ? `仓库源 ${host.sourceUrl}` : '仓库源地址未知');
    meta.textContent = parts.join(' · ');
  }

  const online = card.querySelector('[data-field="online"]');
  const powerLabel = { on: host.online ? '在线' : '已开机 · 未运行载荷', standby: '待机中', off: '离线' }[host.power.state] || '离线';
  online.textContent = host.remembered && !host.online && host.power.state === 'off' ? '上次发现 · 扫描中' : powerLabel;
  online.dataset.tone = host.power.state === 'standby' ? 'warning' : host.online ? 'good' : host.power.state === 'on' ? 'info' : 'neutral';
  online.title = host.power.state === 'standby' ? '主机处于待机（休息模式）：载荷通常仍在内存中，唤醒后即可继续使用。' : '';
  const elfldr = card.querySelector('[data-field="elfldr"]');
  elfldr.textContent = host.elfldr ? 'elfldr 可用' : 'elfldr 不可用';
  elfldr.dataset.tone = host.elfldr ? 'good' : 'neutral';
  const version = ps5VersionLabel(host.version);
  const pldmgr = card.querySelector('[data-field="pldmgr"]');
  pldmgr.textContent = host.pldmgr ? `pldmgr ${host.pldmgrPort}${version ? ` · ${version}` : ''}` : 'pldmgr 不可用';
  pldmgr.dataset.tone = host.pldmgr ? 'good' : 'neutral';
  const source = card.querySelector('[data-field="source"]');
  source.textContent = host.sourceAttached ? '仓库已接入' : '仓库未接入';
  source.dataset.tone = host.sourceAttached ? 'good' : 'neutral';

  const catalogItems = repo.loaded ? repo.items : [];
  const installable = catalogItems.length
    ? catalogItems.filter((item) => !host.installed.includes(item.filename) && !host.pluginUpdates[item.filename]).length
    : host.repoPayloads === null ? 0 : host.repoPayloads;
  const counts = card.querySelector('[data-field="counts"]');
  const upgradable = ps5UpgradeCandidates(host).length;
  counts.textContent = `可安装 ${installable} 个插件 · 运行中 ${host.running.length}${upgradable ? ` · ${upgradable} 个可更新` : ''}`;
  counts.dataset.tone = upgradable ? 'update' : '';

  const hint = card.querySelector('[data-field="hint"]');
  hint.hidden = host.pldmgr;

  const lastError = card.querySelector('[data-field="last-error"]');
  if (host.lastError) {
    lastError.textContent = `最近错误：${host.lastError}`;
    lastError.hidden = false;
  } else {
    lastError.textContent = '';
    lastError.hidden = true;
  }

  const errorText = ps5HostErrors.get(host.ip) || '';
  const error = card.querySelector('[data-field="error"]');
  error.textContent = errorText;
  error.hidden = !errorText;

  const busyNode = card.querySelector('[data-field="busy"]');
  const waking = Boolean(host.power.job && ['waking', 'signing_in'].includes(host.power.job.state));
  const cardBusy = Boolean(ps5Action && ps5Action.ip === host.ip) || waking;
  busyNode.hidden = !cardBusy;
  const busyLabel = busyNode.querySelector('[data-field="busy-label"]');
  if (busyLabel) busyLabel.textContent = waking ? host.power.job.message || '正在唤醒…' : (ps5Action && ps5Action.label) || '处理中…';
  for (const button of $$('[data-ps5-action]', card)) syncPs5HostButton(button, host);
}

function renderPs5Hosts() {
  const container = $('ps5-hosts');
  if (!ps5.loaded) {
    if (container.dataset.mode !== 'loading') {
      container.dataset.mode = 'loading';
      container.replaceChildren(ps5LoadingNode());
    }
    return;
  }
  if (!ps5.hosts.length) {
    if (container.dataset.mode !== 'empty') {
      container.dataset.mode = 'empty';
      container.replaceChildren(ps5EmptyNode());
    }
    return;
  }
  if (container.dataset.mode !== 'list') {
    container.dataset.mode = 'list';
    container.replaceChildren();
  }
  const existing = new Map();
  for (const child of Array.from(container.children)) {
    if (child.classList && child.classList.contains('ps5-host')) existing.set(child.dataset.ip, child);
  }
  const ordered = [];
  for (const host of ps5.hosts) {
    let card = existing.get(host.ip);
    if (card) existing.delete(host.ip);
    else card = createPs5HostCard(host);
    syncPs5HostCard(card, host);
    ordered.push(card);
  }
  for (const stale of existing.values()) stale.remove();
  const current = Array.from(container.children);
  const same = current.length === ordered.length && current.every((node, index) => node === ordered[index]);
  if (!same) container.replaceChildren(...ordered);
}

function syncPs5Tabs() {
  for (const name of PS5_TABS) {
    const button = $(`ps5-tab-${name}`);
    const panel = $(`ps5-panel-${name}`);
    const active = name === ps5Tab;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    if (panel) panel.hidden = !active;
  }
}

function renderPs5Detail(options = {}) {
  const host = ps5SelectedHost();
  const detail = $('ps5-detail');
  if (!host) {
    detail.hidden = true;
    return;
  }
  detail.hidden = false;
  $('ps5-detail-ip').textContent = host.ip;
  const version = ps5VersionLabel(host.version);
  const parts = [
    host.online ? '在线' : '离线',
    host.elfldr ? 'elfldr 可用' : 'elfldr 不可用',
    host.pldmgr ? `pldmgr 8084${version ? ` · ${version}` : ''}` : '未运行 pldmgr'
  ];
  $('ps5-detail-sub').textContent = parts.join(' · ');
  const state = $('ps5-detail-state');
  state.textContent = host.online ? (host.pldmgr ? '在线 · 可管理' : '在线 · 仅 elfldr') : '离线';
  state.dataset.tone = host.online ? 'good' : 'error';

  const note = $('ps5-detail-note');
  let noteText = '';
  if (!host.online) noteText = '主机当前离线，所有操作已禁用。请确认主机已开机并与本机处于同一网段。';
  else if (!host.pldmgr) noteText = '该主机未运行 pldmgr（HTTP 8084）。仍可尝试通过 elfldr 安装并运行载荷，但无法管理进程或自动加载。';
  else if (!host.sourceAttached) noteText = '本服务仓库尚未接入该主机。点击主机卡片上的「接入本服务仓库」后即可从插件库安装。';
  note.textContent = noteText;
  note.hidden = !noteText;

  syncPs5Tabs();
  if (ps5Tab === 'catalog') renderPs5Catalog(host, options);
  else if (ps5Tab === 'running') renderPs5Running(host, options);
  else renderPs5Autoload(host, options);
  updatePs5Controls();
}

function setPs5Tab(tab) {
  if (!PS5_TABS.includes(tab) || ps5Tab === tab) return;
  ps5Tab = tab;
  if (tab === 'autoload') ps5AutoloadKey = '';
  syncPs5Tabs();
  renderPs5Detail({ force: true });
}

function selectPs5Host(ip) {
  if (!ps5HostByIp(ip)) return;
  if (ps5SelectedIp !== ip) {
    ps5SelectedIp = ip;
    ps5Draft = null;
    ps5InlineError = null;
    ps5CatalogKey = '';
    ps5RunningKey = '';
    ps5AutoloadKey = '';
  }
  renderPs5Hosts();
  renderPs5Detail({ force: true });
}

function openPs5Detail(ip, tab) {
  selectPs5Host(ip);
  if (tab) setPs5Tab(tab);
  const detail = $('ps5-detail');
  if (detail.hidden) return;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  detail.scrollIntoView({ block: 'start', behavior: reduceMotion ? 'auto' : 'smooth' });
  const tabButton = $(`ps5-tab-${ps5Tab}`);
  if (tabButton) tabButton.focus({ preventScroll: true });
}

function renderPs5Catalog(host, options = {}) {
  const note = $('ps5-catalog-note');
  const body = $('ps5-catalog-body');
  const query = searchQuery('ps5-catalog-search');
  const items = repo.items.filter((item) => payloadMatchesSearch(item, query));
  $('ps5-catalog-search-summary').textContent = repo.loaded ? `显示 ${items.length} / ${repo.items.length} 个插件`
    : repoReadError ? '插件目录读取失败，请刷新重试。' : '正在读取插件目录…';
  if (!repo.loaded) {
    note.textContent = repoReadError ? '本服务的插件目录读取失败。' : '正在读取本服务的插件目录…';
    const mode = repoReadError ? 'error' : 'loading';
    if (body.dataset.mode !== mode) {
      body.dataset.mode = mode;
      body.replaceChildren(tableEmptyRow(6, repoReadError ? '插件目录读取失败，请刷新重试。' : '正在读取插件目录…'));
    }
    return;
  }
  if (!repo.items.length) {
    note.textContent = '本服务的插件仓库为空。';
    if (body.dataset.mode !== 'empty') {
      body.dataset.mode = 'empty';
      body.replaceChildren(tableEmptyRow(6, '插件目录为空，请先在「插件仓库」上传载荷。'));
    }
    return;
  }
  const rowError = ps5InlineError && ps5InlineError.scope === 'install' && ps5InlineError.ip === host.ip ? ps5InlineError : null;
  const upgradable = ps5UpgradeCandidates(host);
  note.textContent = `本服务仓库共 ${repo.items.length} 个插件 · 主机已安装 ${host.installed.length} 个${upgradable.length ? ` · ${upgradable.length} 个比仓库旧，可一键更新` : ''}${host.sourceAttached ? '' : ' · 仓库未接入，安装将尝试通过 elfldr 执行'}`;
  note.dataset.tone = upgradable.length ? 'update' : '';
  syncPs5UpgradeAll(host);
  if (!items.length) {
    body.dataset.mode = 'no-match';
    ps5CatalogKey = '';
    body.replaceChildren(tableEmptyRow(6, '没有匹配的插件，请调整搜索条件。'));
    return;
  }
  const key = JSON.stringify([
    query,
    host.ip,
    host.installed,
    host.running.map((item) => [item.pid, item.name]),
    host.pluginUpdates,
    items.map((item) => [item.filename, item.name, item.version, item.category, item.size]),
    rowError ? [rowError.key, rowError.message] : null
  ]);
  if (!options.force && key === ps5CatalogKey && body.dataset.mode === 'list') {
    syncPs5CatalogRows(host);
    return;
  }
  ps5CatalogKey = key;
  body.dataset.mode = 'list';
  const fragment = document.createDocumentFragment();
  for (const item of items) fragment.append(ps5CatalogRow(host, item));
  body.replaceChildren(fragment);
  syncPs5CatalogRows(host);
}

function ps5CatalogRow(host, item) {
  const row = document.createElement('tr');
  row.dataset.filename = item.filename;
  const nameCell = document.createElement('td');
  nameCell.append(makeElement('span', 'ps5-payload-name', item.name || item.filename), makeElement('span', 'ps5-payload-file mono', item.filename));
  const versionCell = makeElement('td', '', asText(item.version));
  const categoryCell = makeElement('td', '', asText(item.category));
  const sizeCell = makeElement('td', 'mono ps5-payload-size', item.size === null ? '—' : formatBytes(item.size));
  const statusCell = makeElement('td', 'ps5-row-status');
  statusCell.dataset.field = 'status';
  const actionsCell = document.createElement('td');
  const actions = makeElement('div', 'ps5-row-actions');
  for (const [action, label] of [['upgrade', '更新并应用'], ['install', '安装'], ['install-run', '安装并运行']]) {
    const button = makeElement('button', `button ${action === 'upgrade' ? 'button-primary' : 'button-secondary'} button-small`);
    button.type = 'button';
    button.dataset.ps5Action = action;
    button.dataset.ip = host.ip;
    button.dataset.filename = item.filename;
    button.dataset.label = label;
    button.setAttribute('aria-label', `${label} ${item.name || item.filename}（${host.ip}）`);
    const spinner = makeElement('span', 'spinner');
    spinner.setAttribute('aria-hidden', 'true');
    spinner.hidden = true;
    button.append(spinner, makeElement('span', 'button-label', label));
    actions.append(button);
  }
  actionsCell.append(actions);
  row.append(nameCell, versionCell, categoryCell, sizeCell, statusCell, actionsCell);
  return row;
}

function syncPs5CatalogRows(host) {
  for (const row of $$('#ps5-catalog-body tr[data-filename]')) {
    const filename = row.dataset.filename;
    const busy = Boolean(ps5Action && ps5Action.kind === 'install' && ps5Action.ip === host.ip && ps5Action.filename === filename);
    const rowError = ps5InlineError && ps5InlineError.scope === 'install' && ps5InlineError.ip === host.ip && ps5InlineError.key === filename ? ps5InlineError.message : '';
    const content = makeElement('div', 'ps5-row-status-inner');
    if (busy) {
      const spinner = makeElement('span', 'spinner');
      spinner.setAttribute('aria-hidden', 'true');
      content.append(spinner);
    }
    const info = host.pluginUpdates[filename] || null;
    const installed = host.installed.includes(filename) || Boolean(info);
    const running = host.running.some((item) => item.name === filename) || Boolean(info && info.running);
    const offer = Boolean(info && (info.state === 'outdated' || info.state === 'maybe'));
    const badge = makeElement('span', 'result-badge');
    let detail = '';
    if (busy) {
      badge.dataset.tone = 'info';
      const progress = ps5Progress && ps5Progress.filename === filename ? ps5Progress : null;
      badge.textContent = progress ? ps5StageText(progress) : '处理中…';
    } else if (info && info.state === 'outdated') {
      badge.dataset.tone = 'warning';
      badge.textContent = running ? '可更新 · 运行中' : '可更新';
      detail = `主机 ${info.installedVersion || info.installed} → 仓库 ${info.repoVersion || filename}`;
    } else if (info && info.state === 'maybe') {
      badge.dataset.tone = 'warning';
      badge.textContent = '可能有更新';
      detail = `主机文件 ${info.installed}，与仓库文件名不同`;
    } else if (info && info.state === 'newer') {
      badge.dataset.tone = 'neutral';
      badge.textContent = running ? '运行中 · 主机较新' : '主机版本较新';
      detail = `主机 ${info.installedVersion || info.installed}`;
    } else if (running) {
      badge.dataset.tone = 'good';
      badge.textContent = '运行中';
    } else if (installed) {
      badge.dataset.tone = 'good';
      badge.textContent = '已安装';
    } else {
      badge.dataset.tone = 'neutral';
      badge.textContent = '未安装';
    }
    content.append(badge);
    if (busy && ps5Progress && ps5Progress.filename === filename) {
      const meter = makeElement('span', 'row-meter');
      const fill = makeElement('span');
      fill.style.width = `${Math.round(ps5StageFraction(ps5Progress) * 100)}%`;
      meter.append(fill);
      content.append(meter);
    }
    if (detail && !busy) content.append(makeElement('span', 'ps5-row-detail mono', detail));
    if (rowError) content.append(makeElement('span', 'ps5-row-error', rowError));
    const statusCell = row.querySelector('[data-field="status"]');
    if (statusCell) statusCell.replaceChildren(content);
    row.dataset.busy = String(busy);
    row.dataset.update = offer ? 'true' : 'false';
    for (const button of $$('[data-ps5-action]', row)) {
      const buttonBusy = busy;
      const isUpgrade = button.dataset.ps5Action === 'upgrade';
      button.hidden = isUpgrade ? !offer : false;
      if (button.dataset.ps5Action === 'install') {
        const label = button.querySelector('.button-label');
        if (label) label.textContent = installed ? '重新安装' : '安装';
      }
      const allowed = isUpgrade ? host.online && host.pldmgr : host.online && (host.pldmgr || host.elfldr);
      button.disabled = !authenticated || ps5Busy || !allowed;
      button.setAttribute('aria-busy', String(buttonBusy));
      const spinner = button.querySelector('.spinner');
      if (spinner) spinner.hidden = !buttonBusy;
      if (!allowed) button.title = !host.online ? '主机当前离线' : isUpgrade ? '更新需要 pldmgr' : '需要 pldmgr 或 elfldr';
      else if (isUpgrade) button.title = running ? '停止旧进程，替换为仓库版本并重新启动' : '替换为仓库版本，下次加载时生效';
      else button.removeAttribute('title');
    }
  }
}

function renderPs5Running(host, options = {}) {
  const note = $('ps5-running-note');
  note.textContent = host.pldmgr
    ? `运行中 ${host.running.length} 个进程${host.running.length ? '；停止进程需要确认。' : '。'}`
    : '该主机未运行 pldmgr（8084），无法读取或停止进程。';
  const key = JSON.stringify([host.ip, host.running, host.pldmgr]);
  const body = $('ps5-running-body');
  if (!options.force && key === ps5RunningKey && body.dataset.mode === 'list') {
    syncPs5RunningRows(host);
    return;
  }
  ps5RunningKey = key;
  body.dataset.mode = 'list';
  if (!host.running.length) {
    body.replaceChildren(tableEmptyRow(3, '当前没有运行中的进程。'));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of host.running) {
    const row = document.createElement('tr');
    row.dataset.name = item.name;
    row.dataset.pid = item.pid === null ? '' : String(item.pid);
    const pidCell = makeElement('td', 'mono', item.pid === null ? '—' : String(item.pid));
    const nameCell = makeElement('td', 'ps5-process-name', item.name);
    const actionsCell = document.createElement('td');
    const button = makeElement('button', 'button button-secondary button-small');
    button.type = 'button';
    button.dataset.ps5Action = 'kill';
    button.dataset.ip = host.ip;
    button.dataset.name = item.name;
    if (item.pid !== null) button.dataset.pid = String(item.pid);
    button.dataset.label = '停止';
    button.setAttribute('aria-label', `停止进程 ${item.name}${item.pid === null ? '' : `（PID ${item.pid}）`}`);
    const spinner = makeElement('span', 'spinner');
    spinner.setAttribute('aria-hidden', 'true');
    spinner.hidden = true;
    button.append(spinner, makeElement('span', 'button-label', '停止'));
    actionsCell.append(button);
    row.append(pidCell, nameCell, actionsCell);
    fragment.append(row);
  }
  body.replaceChildren(fragment);
  syncPs5RunningRows(host);
}

function syncPs5RunningRows(host) {
  for (const row of $$('#ps5-running-body tr[data-name]')) {
    const pid = Number(row.dataset.pid);
    const killBusy = Boolean(ps5Action && ps5Action.kind === 'kill' && ps5Action.ip === host.ip && ps5Action.pid === pid);
    for (const button of $$('[data-ps5-action="kill"]', row)) {
      const valid = Number.isInteger(pid) && pid > 0;
      button.disabled = !authenticated || ps5Busy || !host.pldmgr || !host.online || !valid;
      button.setAttribute('aria-busy', String(killBusy));
      const spinner = button.querySelector('.spinner');
      if (spinner) spinner.hidden = !killBusy;
      if (!valid) button.title = '该进程没有可用的 PID，无法停止';
      else if (!host.pldmgr) button.title = '需要主机运行 pldmgr（8084）';
      else button.removeAttribute('title');
    }
  }
}

const ps5SecondsLabel = (ms) => {
  const seconds = ms / 1000;
  return Number.isInteger(seconds) ? String(seconds) : seconds.toFixed(1);
};

function ps5HostDraftOf(host) {
  return {
    ip: host.ip,
    enabled: host.autoload.enabled,
    startDelay: host.autoload.startDelay,
    leadMs: host.autoload.leadMs,
    items: host.autoload.items.map((item) => ({ key: item.key, delayMs: item.delayMs }))
  };
}

function ps5AutoloadValues(host) {
  return ps5Draft && ps5Draft.ip === host.ip ? ps5Draft : ps5HostDraftOf(host);
}

function ps5EnsureDraft(host) {
  if (!ps5Draft || ps5Draft.ip !== host.ip) ps5Draft = ps5HostDraftOf(host);
  return ps5Draft;
}

const ps5DraftSignature = (draft) => JSON.stringify([draft.enabled, draft.startDelay, draft.leadMs,
  draft.items.map((item) => [item.key, item.delayMs])]);

function ps5DraftDirty(host) {
  if (!ps5Draft || ps5Draft.ip !== host.ip) return false;
  return ps5DraftSignature(ps5Draft) !== ps5DraftSignature(ps5HostDraftOf(host));
}

function ps5AutoloadMeta(host, key) {
  const known = host.autoload.items.find((item) => item.key === key);
  const slot = host.payloads.find((item) => item.slot === key);
  return {
    filename: (slot && slot.filename) || (known && known.filename) || key.split('/').pop(),
    installed: Boolean(slot) || Boolean(known && known.installed),
    outcome: known ? known.outcome : ''
  };
}

function syncPs5AutoloadMeta(host) {
  const chip = $('ps5-autoload-state');
  const dirty = ps5DraftDirty(host);
  chip.textContent = dirty ? '有未保存的修改' : '已同步';
  chip.dataset.dirty = String(dirty);
}

function syncPs5AutoloadControls(host, values) {
  const enabled = $('ps5-autoload-enabled');
  enabled.checked = values.enabled;
  const allowed = Boolean(host && host.online && host.pldmgr);
  const locked = !authenticated || ps5Busy || !allowed;
  enabled.disabled = locked;
  const start = $('ps5-autoload-start-delay');
  if (document.activeElement !== start) start.value = String(values.startDelay);
  start.disabled = locked;
  for (const control of $$('#ps5-autoload-items input, #ps5-autoload-items button')) {
    control.disabled = locked || (control.dataset.move === 'up' && control.dataset.index === '0')
      || (control.dataset.move === 'down' && Number(control.dataset.index) === values.items.length - 1);
  }
  const addSelect = $('ps5-autoload-add-select');
  addSelect.disabled = locked || addSelect.options.length === 0;
  $('ps5-autoload-add').disabled = locked || addSelect.options.length === 0;
  const save = $('ps5-autoload-save');
  const saveBusy = Boolean(ps5Action && ps5Action.kind === 'autoload' && ps5Action.ip === host.ip);
  save.disabled = locked || saveBusy || !ps5DraftDirty(host);
  save.setAttribute('aria-busy', String(saveBusy));
  const spinner = save.querySelector('.spinner');
  if (spinner) spinner.hidden = !saveBusy;
  const errorNode = $('ps5-autoload-error');
  const error = ps5InlineError && ps5InlineError.scope === 'autoload' && ps5InlineError.ip === host.ip ? ps5InlineError.message : '';
  errorNode.textContent = error;
  errorNode.hidden = !error;
}

function ps5OutcomeBadge(outcome) {
  const [label, tone] = { success: ['上次启动：成功', 'good'], failed: ['上次启动：失败', 'error'], skipped: ['上次启动：跳过', 'neutral'] }[outcome] || [];
  if (!label) return null;
  const badge = makeElement('span', 'result-badge', label);
  badge.dataset.tone = tone;
  return badge;
}

function renderPs5Autoload(host, options = {}) {
  const values = ps5AutoloadValues(host);
  syncPs5AutoloadMeta(host);
  const note = $('ps5-autoload-note');
  if (!host.pldmgr) {
    note.textContent = `自动加载需要 pldmgr（${host.pldmgrPort}）。该主机当前未运行 pldmgr，设置已禁用。`;
    note.hidden = false;
  } else if (!host.online) {
    note.textContent = '主机当前离线，自动加载设置已禁用。';
    note.hidden = false;
  } else {
    note.textContent = '';
    note.hidden = true;
  }
  const progress = $('ps5-autoload-progress');
  const status = host.autoload.status;
  const stateLabels = { done: '已完成', running: '加载中', countdown: '倒计时中', aborted: '已中止', disabled: '未启用', idle: '未运行' };
  const parts = [];
  if (status.state) parts.push(stateLabels[status.state] || status.state);
  if (status.total !== null && status.done !== null) parts.push(`${status.done} / ${status.total} 项`);
  if (status.failed) parts.push(`失败 ${status.failed}`);
  if (status.current && status.current !== 'DONE') parts.push(`当前 ${status.current}`);
  progress.textContent = parts.length ? `本次开机的自动加载：${parts.join(' · ')}` : '';
  progress.hidden = !parts.length;
  const lead = $('ps5-autoload-lead');
  lead.textContent = values.leadMs ? `倒计时结束后另有 ${ps5SecondsLabel(values.leadMs)} 秒等待（列表开头的 !${values.leadMs}，保存时保留）。` : '';
  lead.hidden = !values.leadMs;

  const key = JSON.stringify([host.ip, values.items, host.payloads, host.autoload.items, host.pldmgr, host.online]);
  if (!options.force && key === ps5AutoloadKey) {
    syncPs5AutoloadControls(host, values);
    return;
  }
  ps5AutoloadKey = key;
  const fragment = document.createDocumentFragment();
  values.items.forEach((item, index) => {
    const meta = ps5AutoloadMeta(host, item.key);
    const row = makeElement('li', 'ps5-seq-row');
    row.append(makeElement('span', 'ps5-seq-index', String(index + 1)));
    const main = makeElement('div', 'ps5-seq-main');
    main.append(makeElement('span', 'ps5-seq-name', meta.filename), makeElement('span', 'ps5-seq-key', item.key));
    const tags = makeElement('span', 'ps5-seq-tags');
    const outcome = ps5OutcomeBadge(meta.outcome);
    if (outcome) tags.append(outcome);
    if (!meta.installed) {
      const missing = makeElement('span', 'result-badge', '主机上未找到');
      missing.dataset.tone = 'warning';
      tags.append(missing);
    }
    if (tags.childNodes.length) main.append(tags);
    const wait = makeElement('label', 'ps5-delay-input ps5-seq-wait');
    const input = document.createElement('input');
    input.type = 'number';
    input.min = '0';
    input.max = '3600';
    input.step = '0.5';
    input.inputMode = 'decimal';
    input.value = ps5SecondsLabel(item.delayMs);
    input.dataset.index = String(index);
    input.dataset.field = 'delay';
    input.setAttribute('aria-label', `${meta.filename} 加载后等待的秒数`);
    wait.append(makeElement('span', '', '加载后等待'), input, makeElement('span', '', '秒'));
    const tools = makeElement('span', 'ps5-seq-tools');
    for (const [move, text, label] of [['up', '↑', '上移'], ['down', '↓', '下移'], ['remove', '移除', '移除']]) {
      const button = makeElement('button', 'button button-secondary button-small', text);
      button.type = 'button';
      button.dataset.move = move;
      button.dataset.index = String(index);
      button.setAttribute('aria-label', `${label} ${meta.filename}`);
      if (move === 'remove') button.dataset.tone = 'danger';
      tools.append(button);
    }
    row.append(main, wait, tools);
    fragment.append(row);
  });
  $('ps5-autoload-items').replaceChildren(fragment);
  $('ps5-autoload-empty').hidden = values.items.length > 0;

  const used = new Set(values.items.map((item) => item.key));
  const select = $('ps5-autoload-add-select');
  const choices = host.payloads.filter((item) => !used.has(item.slot))
    .sort((left, right) => left.filename.localeCompare(right.filename, 'zh', { numeric: true }));
  select.replaceChildren(...choices.map((item) => {
    const option = document.createElement('option');
    option.value = item.slot;
    option.textContent = `${item.filename}（${item.slot}）`;
    return option;
  }));
  if (!choices.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = host.payloads.length ? '已安装的插件都在列表中' : '主机上还没有已安装的插件';
    select.replaceChildren(option);
    select.options.length = 1;
  }
  syncPs5AutoloadControls(host, values);
}

function ps5EditAutoload(change) {
  const host = ps5SelectedHost();
  if (!host || ps5Busy || !authenticated) return;
  const draft = ps5EnsureDraft(host);
  change(draft, host);
  ps5AutoloadKey = '';
  renderPs5Autoload(host);
  updatePs5Controls();
}

function onPs5AutoloadToggle(event) {
  ps5EditAutoload((draft) => { draft.enabled = event.target.checked; });
}

function onPs5AutoloadStartDelay(event) {
  const value = Math.round(Number(event.target.value));
  if (!Number.isFinite(value) || value < 0 || value > 3600) {
    event.target.setAttribute('aria-invalid', 'true');
    return;
  }
  event.target.removeAttribute('aria-invalid');
  ps5EditAutoload((draft) => { draft.startDelay = value; });
}

function onPs5AutoloadFile(event) {
  const input = event.target.closest('input[data-field="delay"]');
  if (!input) return;
  const seconds = Number(input.value);
  if (!Number.isFinite(seconds) || seconds < 0 || seconds > 3600) {
    input.setAttribute('aria-invalid', 'true');
    return;
  }
  input.removeAttribute('aria-invalid');
  const index = Number(input.dataset.index);
  ps5EditAutoload((draft) => { if (draft.items[index]) draft.items[index].delayMs = Math.round(seconds * 1000); });
}

function onPs5AutoloadClick(event) {
  const button = event.target.closest('button[data-move]');
  if (!button) return;
  const index = Number(button.dataset.index);
  const move = button.dataset.move;
  ps5EditAutoload((draft) => {
    const items = draft.items;
    if (move === 'remove') items.splice(index, 1);
    else if (move === 'up' && index > 0) [items[index - 1], items[index]] = [items[index], items[index - 1]];
    else if (move === 'down' && index < items.length - 1) [items[index + 1], items[index]] = [items[index], items[index + 1]];
  });
  const focusTarget = $$(`#ps5-autoload-items button[data-move="${move}"]`)[Math.max(0, move === 'up' ? index - 1 : move === 'down' ? index + 1 : index)];
  if (focusTarget && !focusTarget.disabled) focusTarget.focus({ preventScroll: true });
}

function onPs5AutoloadAdd() {
  const key = $('ps5-autoload-add-select').value;
  if (!key) return;
  ps5EditAutoload((draft) => {
    if (!draft.items.some((item) => item.key === key)) {
      // Match the common 3-second spacing used by the existing list.
      const last = draft.items[draft.items.length - 1];
      if (last && !last.delayMs) last.delayMs = 3000;
      draft.items.push({ key, delayMs: 0 });
    }
  });
}

function ps5RememberFocus() {
  ps5DialogReturn = document.activeElement instanceof HTMLElement ? document.activeElement : null;
}

function ps5RestoreFocus() {
  const node = ps5DialogReturn;
  ps5DialogReturn = null;
  if (node && node.isConnected && typeof node.focus === 'function') node.focus({ preventScroll: true });
}

function requestPs5AutoloadSave() {
  const host = ps5SelectedHost();
  if (!host || ps5Busy || !authenticated || !ps5DraftDirty(host)) return;
  const values = ps5AutoloadValues(host);
  const nextKeys = values.items.map((item) => item.key);
  const removed = host.autoload.items.filter((item) => !nextKeys.includes(item.key));
  const target = { ip: host.ip, values: JSON.parse(JSON.stringify(values)) };
  if (removed.length) {
    ps5AutoloadTarget = target;
    $('ps5-autoload-dialog-host').textContent = host.ip;
    $('ps5-autoload-dialog-current').textContent = String(host.autoload.items.length);
    $('ps5-autoload-dialog-next').textContent = String(values.items.length);
    $('ps5-autoload-dialog-note').textContent = `将从自动加载中移除：${removed.map((item) => ps5AutoloadMeta(host, item.key).filename).join('、')}`;
    $('ps5-autoload-dialog-error').textContent = '';
    $('ps5-autoload-dialog-error').hidden = true;
    $('ps5-autoload-dialog-cancel').disabled = false;
    $('ps5-autoload-dialog-confirm').disabled = false;
    ps5RememberFocus();
    $('ps5-autoload-dialog').showModal();
    $('ps5-autoload-dialog-cancel').focus({ preventScroll: true });
    return;
  }
  submitPs5Autoload(target);
}

async function submitPs5Autoload(target) {
  if (ps5Busy || !authenticated || !target) return;
  const { ip, values } = target;
  const dialog = $('ps5-autoload-dialog');
  ps5Begin('autoload', ip, { label: '正在保存自动加载…' });
  ps5InlineError = null;
  const saveButton = $('ps5-autoload-save');
  if (dialog.open) {
    $('ps5-autoload-dialog-cancel').disabled = true;
    $('ps5-autoload-dialog-confirm').disabled = true;
    buttonBusy('ps5-autoload-dialog-confirm', true, '保存中…');
  }
  buttonBusy(saveButton, true, '保存中…');
  updatePs5Controls();
  renderPs5Detail();
  const requestEpoch = epoch;
  let saved = false;
  try {
    const result = await api('/api/ps5/autoload', {
      ip,
      enabled: Boolean(values.enabled),
      start_delay: values.startDelay,
      lead_ms: values.leadMs,
      items: values.items.map((item) => ({ key: item.key, delay_ms: item.delayMs }))
    });
    if (requestEpoch !== epoch) return;
    if (result.ok !== true) throw new ApiError('服务未确认自动加载设置，请刷新后检查。');
    if (result.host) updatePs5Host(result.host);
    ps5Draft = null;
    saved = true;
    if (dialog.open) dialog.close();
    toast('自动加载设置已保存，下次开机按新顺序加载。', 'success');
  } catch (error) {
    if (requestEpoch !== epoch) return;
    if (error.status === 401) {
      if (dialog.open) dialog.close();
      showLogin(error.message);
      return;
    }
    if (dialog.open) {
      $('ps5-autoload-dialog-error').textContent = error.message;
      $('ps5-autoload-dialog-error').hidden = false;
    } else {
      ps5InlineError = { ip, scope: 'autoload', message: error.message };
      toast(error.message, 'error');
    }
  } finally {
    if (dialog.open) {
      buttonBusy('ps5-autoload-dialog-confirm', false, '覆盖并保存');
      $('ps5-autoload-dialog-cancel').disabled = false;
    }
    buttonBusy(saveButton, false, '保存');
    ps5End();
  }
  if (saved) {
    ps5AutoloadTarget = null;
    ps5AutoloadKey = '';
    refreshPs5();
  }
}

async function scanPs5(options = {}) {
  if (!authenticated || ps5Busy) return false;
  const pickIp = str(options.pickIp);
  ps5Begin('scan', '', { label: '正在扫描局域网…' });
  setPs5Error('');
  const info = $('ps5-scan-info');
  const previousInfo = info.textContent;
  info.textContent = `扫描中…${ps5.discovery.subnet ? ` · 网段 ${ps5.discovery.subnet}` : ''}`;
  const requestEpoch = epoch;
  let result = null;
  try {
    const data = await api('/api/ps5/scan', {});
    if (requestEpoch !== epoch) return false;
    result = applyPs5(data);
    if (options.manual) toast(`扫描完成：发现 ${result.hosts.length} 台主机。`, 'success');
  } catch (error) {
    if (requestEpoch !== epoch) return false;
    if (error.status === 401) {
      showLogin(error.message);
      return false;
    }
    setPs5Error(`扫描失败：${error.message}`);
    toast(error.message, 'error');
    info.textContent = previousInfo;
  } finally {
    ps5End();
  }
  if (result && pickIp) {
    const host = ps5HostByIp(pickIp);
    if (host) {
      selectPs5Host(pickIp);
      setPs5Tab('catalog');
      $('ps5-add-ip').value = '';
      setPs5AddError('');
      toast(`已连接 ${pickIp}。`, 'success');
    } else {
      setPs5AddError(`未在 ${pickIp} 发现可管理的 PS5 主机。请确认 pldmgr 或 elfldr 正在运行，稍后重试。`);
    }
  }
  return Boolean(result);
}

function submitPs5Add(event) {
  event.preventDefault();
  if (!authenticated || ps5Busy) return;
  const input = $('ps5-add-ip');
  const value = input.value.trim();
  setPs5AddError('');
  if (!ipv4(value)) {
    setPs5AddError('请输入有效的 IPv4 地址，例如 192.168.1.142。');
    input.focus();
    return;
  }
  const inSubnet = ps5InSubnet(value, ps5.discovery.subnet);
  if (inSubnet === false) {
    setPs5AddError(`只能添加当前网段（${ps5.discovery.subnet}）内的地址。`);
    input.focus();
    return;
  }
  const existing = ps5HostByIp(value);
  if (existing) {
    openPs5Detail(value, 'catalog');
    input.value = '';
    toast(`${value} 已在主机列表中，已选中。`, 'info');
    return;
  }
  scanPs5({ pickIp: value });
}

async function attachPs5Host(ip) {
  if (!authenticated || ps5Busy) return;
  const host = ps5HostByIp(ip);
  if (!host) return;
  if (!host.online) {
    ps5HostErrors.set(ip, '主机当前离线，无法接入仓库。');
    renderPs5Hosts();
    return;
  }
  if (!host.pldmgr) {
    ps5HostErrors.set(ip, '该主机未运行 pldmgr（8084），无法接入仓库。');
    renderPs5Hosts();
    return;
  }
  ps5HostErrors.delete(ip);
  ps5Begin('attach', ip, { label: '正在接入仓库…' });
  const requestEpoch = epoch;
  let attached = false;
  try {
    const result = await api('/api/ps5/attach', { ip });
    if (requestEpoch !== epoch) return;
    if (result.ok !== true) throw new ApiError('服务未确认接入结果，请刷新后检查。');
    if (result.host) updatePs5Host(result.host);
    attached = true;
    const count = Number(result.host && result.host.repo_payloads);
    toast(Number.isFinite(count) && count > 0 ? `已接入本服务仓库（${count} 个载荷）。` : '已接入本服务仓库。', 'success');
  } catch (error) {
    if (requestEpoch !== epoch) return;
    if (error.status === 401) {
      showLogin(error.message);
      return;
    }
    ps5HostErrors.set(ip, error.message);
    toast(error.message, 'error');
  } finally {
    ps5End();
    renderPs5Hosts();
  }
  if (attached) refreshPs5();
}

async function installPs5Payload(ip, filename, run) {
  if (!authenticated || ps5Busy || !filename) return;
  const host = ps5HostByIp(ip);
  if (!host) return;
  if (!host.online) {
    ps5InlineError = { ip, scope: 'install', key: filename, message: '主机当前离线，无法安装。' };
    renderPs5Detail({ force: true });
    return;
  }
  if (!host.pldmgr && !host.elfldr) {
    ps5InlineError = { ip, scope: 'install', key: filename, message: '需要主机运行 pldmgr 或 elfldr 才能安装。' };
    renderPs5Detail({ force: true });
    return;
  }
  ps5InlineError = null;
  ps5Begin('install', ip, { label: run ? '正在安装并运行插件…' : '正在安装插件…', filename });
  renderPs5Detail({ force: true });
  const requestEpoch = epoch;
  let installed = false;
  ps5Progress = { filename, stage: 'reading' };
  const stopProgress = watchPs5Progress(ip, filename);
  try {
    const result = await api('/api/ps5/install', { ip, filename, run: Boolean(run) }, 180000);
    if (requestEpoch !== epoch) return;
    if (result.ok !== true) throw new ApiError('服务未确认安装结果，请刷新后检查。');
    if (result.host) updatePs5Host(result.host);
    installed = true;
    const mode = str(result.mode).toLowerCase() === 'elfldr' ? 'elfldr' : 'pldmgr';
    const ran = result.ran === true;
    toast(ran ? `已通过 ${mode} 安装并运行 ${filename}。` : `已通过 ${mode} 安装 ${filename}。`, 'success');
  } catch (error) {
    if (requestEpoch !== epoch) return;
    if (error.status === 401) {
      showLogin(error.message);
      return;
    }
    ps5InlineError = { ip, scope: 'install', key: filename, message: error.message };
    toast(error.message, 'error');
  } finally {
    stopProgress();
    ps5Progress = null;
    ps5End();
  }
  if (installed) refreshPs5();
}

function openPs5KillDialog(ip, pid, name) {
  if (!authenticated || ps5Busy) return;
  if (!Number.isInteger(pid) || pid <= 0) return;
  ps5KillTarget = { ip, pid, name: str(name) || `PID ${pid}` };
  $('ps5-kill-host').textContent = ip;
  $('ps5-kill-target').textContent = `${ps5KillTarget.name}（PID ${pid}）`;
  $('ps5-kill-error').textContent = '';
  $('ps5-kill-error').hidden = true;
  $('ps5-kill-cancel').disabled = false;
  $('ps5-kill-confirm').disabled = false;
  ps5RememberFocus();
  $('ps5-kill-dialog').showModal();
  $('ps5-kill-cancel').focus({ preventScroll: true });
}

async function confirmPs5Kill() {
  const target = ps5KillTarget;
  const dialog = $('ps5-kill-dialog');
  if (!target || ps5Busy || !authenticated) return;
  ps5Begin('kill', target.ip, { label: '正在停止进程…', pid: target.pid });
  $('ps5-kill-error').textContent = '';
  $('ps5-kill-error').hidden = true;
  $('ps5-kill-cancel').disabled = true;
  $('ps5-kill-confirm').disabled = true;
  buttonBusy('ps5-kill-confirm', true, '停止中…');
  const requestEpoch = epoch;
  let killed = false;
  try {
    const result = await api('/api/ps5/kill', { ip: target.ip, pid: target.pid });
    if (requestEpoch !== epoch) return;
    if (result.ok !== true) throw new ApiError('服务未确认停止结果，请刷新后检查。');
    if (result.host) updatePs5Host(result.host);
    killed = true;
    dialog.close();
    toast(`已停止 ${target.name}。`, 'success');
  } catch (error) {
    if (requestEpoch !== epoch) return;
    if (error.status === 401) {
      dialog.close();
      showLogin(error.message);
      return;
    }
    $('ps5-kill-error').textContent = error.message;
    $('ps5-kill-error').hidden = false;
  } finally {
    buttonBusy('ps5-kill-confirm', false, '确认停止');
    $('ps5-kill-cancel').disabled = false;
    ps5End();
  }
  if (killed) refreshPs5();
}

function openPs5Shutdown(ip) {
  if (!authenticated || ps5Busy || !ps5HostByIp(ip)) return;
  ps5ShutdownIp = ip;
  $('ps5-shutdown-ip').textContent = ip;
  $('ps5-shutdown-error').textContent = '';
  $('ps5-shutdown-error').hidden = true;
  $('ps5-shutdown-cancel').disabled = false;
  $('ps5-shutdown-confirm').disabled = false;
  ps5RememberFocus();
  $('ps5-shutdown-dialog').showModal();
  $('ps5-shutdown-cancel').focus({ preventScroll: true });
}

async function confirmPs5Shutdown() {
  const ip = ps5ShutdownIp;
  const dialog = $('ps5-shutdown-dialog');
  if (!ip || ps5Busy || !authenticated) return;
  ps5Begin('shutdown', ip, { label: '正在让 pldmgr 退出…' });
  $('ps5-shutdown-error').textContent = '';
  $('ps5-shutdown-error').hidden = true;
  $('ps5-shutdown-cancel').disabled = true;
  $('ps5-shutdown-confirm').disabled = true;
  buttonBusy('ps5-shutdown-confirm', true, '退出中…');
  const requestEpoch = epoch;
  let requested = false;
  try {
    const result = await api('/api/ps5/shutdown', { ip });
    if (requestEpoch !== epoch) return;
    if (result.ok !== true) throw new ApiError('服务未确认 pldmgr 已退出，请刷新后检查。');
    requested = true;
    dialog.close();
    toast(`已让 ${ip} 上的 pldmgr 退出。`, 'success');
  } catch (error) {
    if (requestEpoch !== epoch) return;
    if (error.status === 401) {
      dialog.close();
      showLogin(error.message);
      return;
    }
    $('ps5-shutdown-error').textContent = error.message;
    $('ps5-shutdown-error').hidden = false;
  } finally {
    buttonBusy('ps5-shutdown-confirm', false, '退出 pldmgr');
    $('ps5-shutdown-cancel').disabled = false;
    ps5End();
  }
  if (requested) refreshPs5();
}

function handlePs5Action(element) {
  const action = element.dataset.ps5Action;
  const ip = element.dataset.ip || ps5SelectedIp;
  switch (action) {
    case 'scan':
      scanPs5({ manual: true });
      break;
    case 'select':
      selectPs5Host(ip);
      break;
    case 'catalog':
      openPs5Detail(ip, 'catalog');
      break;
    case 'running':
      openPs5Detail(ip, 'running');
      break;
    case 'autoload':
      openPs5Detail(ip, 'autoload');
      break;
    case 'attach':
      attachPs5Host(ip);
      break;
    case 'files':
      openFilesForHost(ip);
      break;
    case 'shutdown':
      openPs5Shutdown(ip);
      break;
    case 'power': {
      const host = ps5HostByIp(ip);
      if (host && host.power.state === 'standby') wakePs5(ip);
      else openPs5Standby(ip);
      break;
    }
    case 'pair':
      openPs5Pair(ip);
      break;
    case 'upgrade':
      openPs5Upgrade(ip, [element.dataset.filename]);
      break;
    case 'install':
      installPs5Payload(ip, element.dataset.filename, false);
      break;
    case 'install-run':
      installPs5Payload(ip, element.dataset.filename, true);
      break;
    case 'kill':
      openPs5KillDialog(ip, Number(element.dataset.pid), element.dataset.name);
      break;
    default:
      break;
  }
}

function openPs5HostSheet(trigger) {
  const card = trigger.closest('.ps5-host');
  if (!card) return;
  const items = $$('[data-ps5-action][data-tier="secondary"]', card).filter((button) => !button.hidden).map((button) => ({
    label: (button.querySelector('.button-label') || button).textContent.trim(),
    hint: button.disabled ? button.title : '',
    disabled: button.disabled,
    danger: button.dataset.tone === 'danger',
    onSelect: () => handlePs5Action(button)
  }));
  const ip = trigger.dataset.ps5More;
  const meta = card.querySelector('[data-field="meta"]');
  openActionSheet({ title: `主机 ${ip}`, sub: meta ? meta.textContent : '', items, returnFocus: trigger });
}

function onPs5ViewClick(event) {
  const more = event.target.closest ? event.target.closest('[data-ps5-more]') : null;
  if (more) {
    event.preventDefault();
    openPs5HostSheet(more);
    return;
  }
  const action = event.target.closest ? event.target.closest('[data-ps5-action]') : null;
  if (action) {
    event.preventDefault();
    handlePs5Action(action);
    return;
  }
  const tab = event.target.closest ? event.target.closest('[data-ps5-tab]') : null;
  if (tab) setPs5Tab(tab.dataset.ps5Tab);
}

function onPs5TabsKeydown(event) {
  const keys = ['ArrowRight', 'ArrowLeft', 'Home', 'End'];
  if (!keys.includes(event.key)) return;
  event.preventDefault();
  const index = PS5_TABS.indexOf(ps5Tab);
  let next;
  if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = PS5_TABS.length - 1;
  else next = (index + (event.key === 'ArrowRight' ? 1 : -1) + PS5_TABS.length) % PS5_TABS.length;
  const name = PS5_TABS[next];
  setPs5Tab(name);
  const button = $(`ps5-tab-${name}`);
  if (button) button.focus();
}

function updatePs5Controls() {
  if (!$('ps5-scan')) return;
  const scan = $('ps5-scan');
  const scanBusy = Boolean(ps5Action && ps5Action.kind === 'scan');
  scan.disabled = !authenticated || ps5Busy || ps5Loading;
  scan.setAttribute('aria-busy', String(scanBusy));
  const scanSpinner = scan.querySelector('.spinner');
  if (scanSpinner) scanSpinner.hidden = !scanBusy;
  const scanLabel = scan.querySelector('.button-label');
  if (scanLabel) scanLabel.textContent = scanBusy ? '正在扫描…' : '重新扫描';

  const addInput = $('ps5-add-ip');
  addInput.disabled = !authenticated || ps5Busy;
  const addButton = $('ps5-add-button');
  addButton.disabled = !authenticated || ps5Busy || ps5Loading;
  addButton.setAttribute('aria-busy', String(Boolean(ps5Action && ps5Action.kind === 'scan')));
  const addSpinner = addButton.querySelector('.spinner');
  if (addSpinner) addSpinner.hidden = !(ps5Action && ps5Action.kind === 'scan');

  renderPs5Hosts();

  const host = ps5SelectedHost();
  if (host && !$('ps5-detail').hidden) {
    if (ps5Tab === 'catalog') syncPs5CatalogRows(host);
    else if (ps5Tab === 'running') syncPs5RunningRows(host);
    else syncPs5AutoloadControls(host, ps5AutoloadValues(host));
  } else {
    const enabled = $('ps5-autoload-enabled');
    const save = $('ps5-autoload-save');
    const locked = !authenticated || ps5Busy;
    enabled.disabled = locked;
    save.disabled = locked;
    for (const box of $$('#ps5-autoload-items input[type="checkbox"]')) box.disabled = locked;
  }
}

function resetPs5Panel() {
  $('ps5-catalog-search').value = '';
  ps5.loaded = false;
  ps5.discovery = { subnet: '', scanned: 0, lastScan: '', scanning: false, durationMs: 0, enabled: true };
  ps5.hosts = [];
  ps5Busy = false;
  ps5Loading = false;
  ps5Pending = null;
  ps5Action = null;
  ps5Draft = null;
  ps5SelectedIp = '';
  ps5Tab = 'catalog';
  ps5InlineError = null;
  ps5ShutdownIp = '';
  ps5KillTarget = null;
  ps5AutoloadTarget = null;
  ps5DialogReturn = null;
  ps5CatalogKey = '';
  ps5RunningKey = '';
  ps5AutoloadKey = '';
  ps5HostErrors.clear();
  setPs5Error('');
  setPs5AddError('');
  $('ps5-add-ip').value = '';
  $('ps5-detail').hidden = true;
  $('ps5-autoload-error').textContent = '';
  $('ps5-autoload-error').hidden = true;
  $('ps5-autoload-progress').textContent = '';
  $('ps5-autoload-progress').hidden = true;
  $('ps5-autoload-note').textContent = '';
  $('ps5-autoload-note').hidden = true;
  $('ps5-autoload-empty').hidden = true;
  $('ps5-autoload-items').replaceChildren();
  $('ps5-autoload-state').textContent = '已同步';
  $('ps5-autoload-state').dataset.dirty = 'false';
  $('ps5-catalog-body').replaceChildren(tableEmptyRow(6, '正在读取插件目录…'));
  $('ps5-running-body').replaceChildren();
  renderPs5();
  updatePs5Controls();
}

function ps5CatalogRefreshHook() {
  if (!ps5.loaded) return;
  renderPs5Hosts();
  const host = ps5SelectedHost();
  if (host && ps5Tab === 'catalog') renderPs5Catalog(host);
}

/* ── PS5 电源：唤醒 / 待机 / 配对 ─────────────────────────────────── */

const ps5PowerPolls = new Map();

async function wakePs5(ip) {
  if (!authenticated) return;
  try {
    await api('/api/ps5/power/wake', { ip });
    toast(`正在唤醒 ${ip}，通常需要 20–60 秒。`, 'info');
    watchPs5Power(ip);
    refreshPs5();
  } catch (error) {
    if (error.status === 401) {
      showLogin(error.message);
      return;
    }
    toast(error.message, 'error');
  }
}

function watchPs5Power(ip) {
  if (ps5PowerPolls.has(ip)) return;
  const started = Date.now();
  const tick = async () => {
    if (!authenticated || Date.now() - started > 180000) {
      ps5PowerPolls.delete(ip);
      return;
    }
    try {
      const status = await api(`/api/ps5/power?ip=${encodeURIComponent(ip)}`);
      const job = status.job || {};
      const host = ps5HostByIp(ip);
      if (host) {
        host.power.state = status.state || host.power.state;
        host.power.job = status.job ? { state: str(job.state), message: str(job.message), error: str(job.error) } : null;
        renderPs5Hosts();
      }
      if (job.state === 'done' || job.state === 'failed') {
        ps5PowerPolls.delete(ip);
        toast(job.state === 'done' ? `${ip}：${job.message || '已唤醒'}` : `${ip} 唤醒失败：${job.error || job.message}`, job.state === 'done' ? 'success' : 'error');
        setTimeout(() => refreshPs5(), 1500);
        return;
      }
    } catch (error) {
      if (error.status === 401) {
        ps5PowerPolls.delete(ip);
        return;
      }
    }
    ps5PowerPolls.set(ip, setTimeout(tick, 2500));
  };
  ps5PowerPolls.set(ip, setTimeout(tick, 1500));
}

function openPs5Standby(ip) {
  if (!authenticated || ps5Busy) return;
  ps5StandbyIp = ip;
  $('ps5-standby-ip').textContent = ip;
  $('ps5-standby-error').hidden = true;
  $('ps5-standby-confirm').disabled = false;
  ps5RememberFocus();
  $('ps5-standby-dialog').showModal();
  $('ps5-standby-cancel').focus({ preventScroll: true });
}

async function confirmPs5Standby() {
  const ip = ps5StandbyIp;
  if (!ip || !authenticated) return;
  buttonBusy('ps5-standby-confirm', true, '发送中…');
  try {
    await api('/api/ps5/power/standby', { ip });
    $('ps5-standby-dialog').close();
    toast(`已让 ${ip} 进入待机。`, 'success');
    setTimeout(() => refreshPs5(), 6000);
  } catch (error) {
    if (error.status === 401) {
      $('ps5-standby-dialog').close();
      showLogin(error.message);
      return;
    }
    $('ps5-standby-error').textContent = error.message;
    $('ps5-standby-error').hidden = false;
  } finally {
    buttonBusy('ps5-standby-confirm', false, '进入待机');
  }
}

function syncPs5PairMode() {
  const pin = $('ps5-pair-form').querySelector('input[name="ps5-pair-mode"]:checked').value === 'pin';
  $('ps5-pair-pin-fields').hidden = !pin;
}

function openPs5Pair(ip) {
  if (!authenticated || ps5Busy) return;
  ps5PairIp = ip;
  $('ps5-pair-form').reset();
  $('ps5-pair-title').textContent = `配对遥控游玩 · ${ip}`;
  $('ps5-pair-status').textContent = '';
  $('ps5-pair-error').hidden = true;
  syncPs5PairMode();
  ps5RememberFocus();
  $('ps5-pair-dialog').showModal();
}

async function submitPs5Pair(event) {
  event.preventDefault();
  const ip = ps5PairIp;
  if (!ip || !authenticated) return;
  const pinMode = $('ps5-pair-form').querySelector('input[name="ps5-pair-mode"]:checked').value === 'pin';
  const body = { ip };
  if (pinMode) {
    body.pin = $('ps5-pair-pin').value.trim();
    body.account_id = $('ps5-pair-account').value.trim();
  }
  $('ps5-pair-error').hidden = true;
  $('ps5-pair-status').textContent = '正在与主机配对，最多需要一分钟…';
  buttonBusy('ps5-pair-submit', true, '配对中…');
  try {
    const result = await api('/api/ps5/power/pair', body);
    $('ps5-pair-dialog').close();
    toast(`已与 ${ip} 完成配对${result.nickname ? `（${result.nickname}）` : ''}，之后可以从待机唤醒。`, 'success');
    refreshPs5();
  } catch (error) {
    if (error.status === 401) {
      $('ps5-pair-dialog').close();
      showLogin(error.message);
      return;
    }
    $('ps5-pair-status').textContent = '';
    $('ps5-pair-error').textContent = error.message;
    $('ps5-pair-error').hidden = false;
  } finally {
    buttonBusy('ps5-pair-submit', false, '开始配对');
  }
}

function bindPs5PowerEvents() {
  $('ps5-standby-cancel').addEventListener('click', () => $('ps5-standby-dialog').close());
  $('ps5-standby-confirm').addEventListener('click', confirmPs5Standby);
  $('ps5-pair-cancel').addEventListener('click', () => $('ps5-pair-dialog').close());
  $('ps5-pair-form').addEventListener('submit', submitPs5Pair);
  for (const radio of $$('input[name="ps5-pair-mode"]')) radio.addEventListener('change', syncPs5PairMode);
  for (const id of ['ps5-standby-dialog', 'ps5-pair-dialog']) {
    $(id).addEventListener('close', () => {
      flushPs5Pending();
      ps5RestoreFocus();
    });
  }
}


function collectVisibleTargets() {
  const targets = [];
  for (const box of $$('#activity-body input[type="checkbox"]')) {
    if (box.dataset.target) targets.push(box.dataset.target);
  }
  return targets;
}

function applySelectAll(checked) {
  for (const target of collectVisibleTargets()) {
    if (checked) activitySelected.add(target);
    else activitySelected.delete(target);
  }
  updateActivityControls();
  if (!activitySelected.size) flushActivityPending();
}

async function submitLogin(event) {
  event.preventDefault();
  if (busy) return;
  busy = true;
  $('login-error').hidden = true;
  $('login-error').textContent = '';
  $('password').removeAttribute('aria-invalid');
  actBusy('login', true, '正在登录…');
  const loginEpoch = ++epoch;
  try {
    const data = await api('/api/login', { password: $('password').value });
    if (loginEpoch !== epoch) return;
    if (typeof data.csrf !== 'string' || !data.csrf) throw new ApiError('登录响应缺少验证信息，请重试。');
    csrf = data.csrf;
    $('password').value = '';
    const state = await api('/api/state');
    if (loginEpoch !== epoch) return;
    renderState(state);
    // 登录后固定从概览开始，避免沿用退出前的历史位置。
    try { history.replaceState(null, '', '#/overview'); } catch { /* ignore */ }
    showSection('overview', { moveFocus: true });
    refreshRepo();
    refreshActivity();
    if (dirty) toast('已重新登录，未保存的编辑已保留。', 'info');
  } catch (error) {
    if (loginEpoch !== epoch) return;
    $('login-error').textContent = error.message;
    $('login-error').hidden = false;
    $('password').setAttribute('aria-invalid', 'true');
    $('password').focus();
  } finally {
    busy = false;
    actBusy('login', false);
    updateControls();
    schedulePoll();
  }
}

/* ── PS5 插件：与仓库版本比对、一键更新并应用 ───────────────────────── */

function ps5UpgradeCandidates(host) {
  if (!host || !repo.loaded || !host.pluginUpdates) return [];
  return repo.items
    .filter((item) => {
      const info = host.pluginUpdates[item.filename];
      return info && (info.state === 'outdated' || info.state === 'maybe');
    })
    .map((item) => ({ item, info: host.pluginUpdates[item.filename] }));
}

function syncPs5UpgradeAll(host) {
  const button = $('ps5-upgrade-all');
  if (!button) return;
  const candidates = ps5UpgradeCandidates(host);
  const sure = candidates.filter(({ info }) => info.state === 'outdated');
  button.hidden = sure.length < 2;
  const busyNow = Boolean(ps5Batch);
  button.disabled = !authenticated || ps5Busy || !host.online || !host.pldmgr;
  button.setAttribute('aria-busy', String(busyNow));
  const spinner = button.querySelector('.spinner');
  if (spinner) spinner.hidden = !busyNow;
  const label = button.querySelector('.button-label');
  if (label) label.textContent = busyNow ? '更新中…' : `全部更新并应用（${sure.length}）`;
}

function openPs5Upgrade(ip, filenames) {
  const host = ps5HostByIp(ip);
  if (!authenticated || ps5Busy || !host) return;
  const list = ps5UpgradeCandidates(host).filter(({ item, info }) => (filenames ? filenames.includes(item.filename) : info.state === 'outdated'));
  if (!list.length) return;
  ps5UpgradeTarget = { ip, filenames: list.map(({ item }) => item.filename) };
  $('ps5-upgrade-host').textContent = ip;
  $('ps5-upgrade-title').textContent = list.length > 1 ? `更新 ${list.length} 个插件？` : `更新 ${list[0].item.name}？`;
  const fragment = document.createDocumentFragment();
  for (const { item, info } of list) {
    const row = makeElement('li', 'upgrade-item');
    const text = makeElement('div', 'upgrade-text');
    text.append(makeElement('strong', '', item.name || item.filename));
    text.append(makeElement('span', 'upgrade-versions mono', `${info.installedVersion || info.installed} → ${item.version || item.filename}`));
    const tag = makeElement('span', 'result-badge');
    tag.dataset.tone = info.state === 'maybe' ? 'warning' : info.running ? 'info' : 'neutral';
    tag.textContent = info.state === 'maybe' ? '可能有更新' : info.running ? '运行中 · 将重启' : '未运行 · 仅替换';
    row.append(text, tag);
    fragment.append(row);
  }
  $('ps5-upgrade-list').replaceChildren(fragment);
  $('ps5-upgrade-error').hidden = true;
  ps5RememberFocus();
  $('ps5-upgrade-dialog').showModal();
  $('ps5-upgrade-confirm').focus({ preventScroll: true });
}

async function confirmPs5Upgrade() {
  const target = ps5UpgradeTarget;
  if (!target) return;
  $('ps5-upgrade-dialog').close();
  await runPs5Upgrades(target.ip, target.filenames);
}

async function runPs5Upgrades(ip, filenames) {
  if (!authenticated || ps5Busy || !filenames.length) return;
  const requestEpoch = epoch;
  ps5InlineError = null;
  ps5Begin('install', ip, { label: '正在更新插件…', filename: filenames[0] });
  const done = [];
  let failure = null;
  try {
    for (const [index, filename] of filenames.entries()) {
      const item = repo.items.find((entry) => entry.filename === filename);
      const name = (item && item.name) || filename;
      ps5Action = { kind: 'install', ip, filename, label: `正在更新 ${name}…` };
      ps5Batch = { index, total: filenames.length, name };
      ps5Progress = { filename, stage: 'checking' };
      renderPs5OpProgress();
      renderPs5Detail({ force: true });
      const stop = watchPs5Progress(ip, filename);
      try {
        const result = await api('/api/ps5/upgrade', { ip, filename }, 180000);
        if (requestEpoch !== epoch) return;
        if (result.host) updatePs5Host(result.host);
        done.push(result.ran ? `${name}（已重启）` : name);
      } catch (error) {
        if (requestEpoch !== epoch) return;
        if (error.status === 401) { showLogin(error.message); return; }
        ps5InlineError = { ip, scope: 'install', key: filename, message: error.message };
        failure = `${name}：${error.message}`;
        break;
      } finally {
        stop();
      }
    }
  } finally {
    ps5Batch = null;
    ps5Progress = null;
    renderPs5OpProgress();
    ps5End();
  }
  if (done.length) toast(`已更新 ${done.join('、')}。`, 'success');
  if (failure) toast(`更新失败 · ${failure}`, 'error');
  refreshPs5();
}

// 仓库页：上游更新后，提醒哪些主机上还是旧版本。
function renderRepoPs5Hint() {
  const node = $('repo-ps5-hint');
  if (!node) return;
  const hosts = ps5.loaded ? ps5.hosts.map((host) => ({ host, count: ps5UpgradeCandidates(host).length })).filter((entry) => entry.count) : [];
  node.hidden = !hosts.length;
  if (!hosts.length) return;
  $('repo-ps5-hint-text').textContent = `PS5 主机 ${hosts.map(({ host, count }) => `${host.ip}（${count} 个）`).join('、')} 上的插件比仓库旧，可以一键更新并应用。`;
  $('repo-ps5-hint-go').dataset.ip = hosts[0].host.ip;
}

function goPs5Updates(ip) {
  if (location.hash !== '#/ps5') location.hash = '#/ps5';
  setTimeout(() => { if (ps5HostByIp(ip)) openPs5Detail(ip, 'catalog'); }, 60);
}
