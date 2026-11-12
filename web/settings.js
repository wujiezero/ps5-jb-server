'use strict';

/* ── 服务版本与显式更新 ─────────────────────────────────────────────── */

function updateActive() {
  return updates.awaiting || UPDATE_ACTIVE_STATES.has(updates.data?.job?.state);
}

function renderUpdates() {
  const data = updates.data;
  const job = data?.job;
  const active = updateActive();
  const version = str(data?.current) || updates.current;
  const current = version ? `v${version.replace(/^v/, '')}` : '版本读取中';
  $('update-current').textContent = current;
  $('sidebar-version').textContent = current;
  const latest = data?.latest ? `v${String(data.latest).replace(/^v/, '')}` : '';
  const latestText = data?.check_error ? `检查失败${latest ? ` · 上次结果 ${latest}` : ' · 无法确认最新版本'}`
    : data?.available && latest ? `有更新 · ${latest}`
      : latest ? `${latest} · 已是最新` : data?.checked_at ? '暂无发布' : '未检查';
  $('update-latest').textContent = latestText;
  $('update-checked').textContent = data?.checked_at ? formatTime(data.checked_at, true) : '尚未检查';
  const proxy = data?.proxy;
  const proxyCell = $('update-proxy');
  proxyCell.textContent = !proxy ? '未知'
    : proxy.error ? str(proxy.error)
      : proxy.configured ? `${str(proxy.url)} · 来自 ${str(proxy.variable)}`
        : '直连（未设置代理环境变量）';
  proxyCell.dataset.tone = proxy?.error ? 'error' : proxy?.configured ? 'good' : '';
  proxyCell.title = proxy?.configured
    ? 'GitHub 更新与插件上游经由该代理；PS5、NAS 与本机插件仓库始终直连。'
    : '未设置代理时直连外网。可通过 systemd drop-in 设置 PS5JB_PROXY 或 HTTPS_PROXY，或安装时用 install.sh --proxy。';
  const chip = $('update-state');
  chip.textContent = active ? (updates.timedOut ? '等待确认' : '更新中') : latestText;
  chip.dataset.tone = data?.check_error || job?.state === 'failed' || job?.state === 'rolled_back' ? 'error'
    : job?.state === 'done' ? 'good' : 'neutral';
  $('update-check').textContent = updates.action === 'check' ? '正在检查…' : '检查更新';
  $('update-check').disabled = !authenticated || busy || updates.loading || active;
  $('update-check').setAttribute('aria-busy', String(updates.action === 'check'));
  $('update-apply').textContent = updates.action === 'apply' ? '正在提交…' : latest ? `更新到 ${latest}` : '更新服务';
  $('update-apply').hidden = !data?.available;
  $('update-apply').disabled = !authenticated || busy || updates.loading || active || !data?.supported || !data?.available || !latest;
  $('update-apply').setAttribute('aria-busy', String(updates.action === 'apply'));
  $('update-unsupported').textContent = data?.supported === false ? str(data.unsupported_reason) || '当前安装方式不支持在线更新。' : '';
  $('update-unsupported').hidden = data?.supported !== false;
  $('update-notes').textContent = str(data?.notes);
  $('update-notes-wrap').hidden = !str(data?.notes);
  const release = $('update-release');
  const releaseUrl = str(data?.release_url);
  // Only navigate to the project's HTTPS GitHub release pages.
  const safeRelease = /^https:\/\/github\.com\/wujiezero\/ps5-jb-server\/releases(?:[/?#]|$)/.test(releaseUrl);
  release.hidden = !safeRelease;
  if (safeRelease) release.setAttribute('href', releaseUrl);
  else release.removeAttribute('href');
  const error = updates.error || str(data?.check_error);
  $('update-error').textContent = error;
  $('update-error').hidden = !error;
  const progress = $('update-progress');
  let progressText = job ? UPDATE_STATE_LABELS[job.state] || '更新状态未知，请重新读取状态。' : '';
  if (job?.version && progressText) progressText += ` · v${String(job.version).replace(/^v/, '')}`;
  if (job?.error) progressText += `：${job.error}`;
  if (job?.rollback_error) progressText += `；回滚失败：${job.rollback_error}`;
  if (updates.reconnect) progressText = '服务连接已中断，正在重新连接以确认更新结果…';
  if (updates.timedOut) progressText = '已等待 5 分钟，尚未确认更新结果。请重试连接或重新加载页面查看状态。';
  progress.textContent = progressText;
  progress.hidden = !progressText;
  progress.dataset.tone = updates.timedOut ? 'error' : chip.dataset.tone;
  renderUpdateStepper(job, active);
  // 步骤条已经说明进行到哪一步；文字横幅只在出错或超时时补充。
  if (!$('update-stepper').hidden && !job?.error && !updates.timedOut) progress.hidden = true;
  $('update-retry').hidden = !updates.timedOut && !updates.error;
  $('update-retry').disabled = updates.loading || !authenticated;
  $('update-reload').hidden = !updates.timedOut;
}

async function loadVersion() {
  try {
    const data = await api('/api/version');
    if (str(data.version)) updates.current = str(data.version);
  } catch {
    // Authenticated status is a second source for the installed version.
  }
  renderUpdates();
}

function stopUpdatePolling() {
  clearTimeout(updates.timer);
  updates.timer = null;
}

function scheduleUpdatePoll() {
  stopUpdatePolling();
  if (!authenticated || !updateActive()) {
    updates.deadline = 0;
    return;
  }
  if (!updates.deadline) updates.deadline = Date.now() + UPDATE_TIMEOUT_MS;
  const remaining = updates.deadline - Date.now();
  if (remaining <= 0 || updates.timedOut) {
    updates.timedOut = true;
    renderUpdates();
    return;
  }
  updates.timer = setTimeout(() => {
    if (Date.now() >= updates.deadline) {
      updates.timedOut = true;
      stopUpdatePolling();
      renderUpdates();
    } else refreshUpdates();
  }, Math.min(UPDATE_POLL_MS, remaining));
}

async function requestUpdates(action = '') {
  if (!authenticated || updates.loading) return;
  if (action && (busy || updateActive())) return;
  if (action === 'apply' && (!updates.data?.supported || !updates.data.available || !updates.data.latest)) return;
  stopUpdatePolling();
  updates.loading = true;
  updates.action = action;
  updates.error = '';
  const generation = updates.generation;
  renderUpdates();
  try {
    const path = action ? `/api/updates/${action}` : '/api/updates';
    const body = action === 'apply' ? { version: updates.data.latest } : action === 'check' ? {} : undefined;
    const data = await api(path, body);
    if (generation !== updates.generation) return;
    updates.data = data;
    if (str(data.current)) updates.current = str(data.current);
    updates.awaiting = false;
    updates.reconnect = false;
    if (!updateActive()) {
      updates.deadline = 0;
      updates.timedOut = false;
    }
  } catch (error) {
    if (generation !== updates.generation) return;
    if (error.status === 401) {
      showLogin(error.message);
      return;
    }
    // An interrupted POST may already have queued an update. Read status;
    // never repeat the install request automatically.
    if (!error.status && (action === 'apply' || updateActive())) {
      updates.awaiting = true;
      updates.reconnect = true;
    } else updates.error = error.message;
  } finally {
    if (generation === updates.generation) {
      updates.loading = false;
      updates.action = '';
      renderUpdates();
      scheduleUpdatePoll();
    }
  }
}

function refreshUpdates() { return requestUpdates(); }
function checkUpdates() { return requestUpdates('check'); }
function applyUpdate() { return requestUpdates('apply'); }

function retryUpdateConnection() {
  if (updates.loading || !authenticated) return;
  updates.timedOut = false;
  updates.deadline = 0;
  if (!updates.current) loadVersion();
  refreshUpdates();
}

/* ── 修改密码 ───────────────────────────────────────────────────────── */

function clearPasswordErrors() {
  for (const [field, error] of [['current-password', 'current-password-error'], ['new-password', 'new-password-error'], ['confirm-password', 'confirm-password-error']]) {
    $(field).removeAttribute('aria-invalid');
    $(error).textContent = '';
    $(error).hidden = true;
  }
  $('password-form-error').textContent = '';
  $('password-form-error').hidden = true;
}

function resetPasswordPanel() {
  clearPasswordErrors();
  for (const id of ['current-password', 'new-password', 'confirm-password']) $(id).value = '';
  $('password-success').textContent = '';
  $('password-success').hidden = true;
}

function validatePasswordForm() {
  clearPasswordErrors();
  const current = $('current-password').value;
  const next = $('new-password').value;
  const confirm = $('confirm-password').value;
  let first = null;
  const invalid = (field, errorId, message) => {
    $(field).setAttribute('aria-invalid', 'true');
    $(errorId).textContent = message;
    $(errorId).hidden = false;
    if (!first) first = $(field);
  };
  if (!current) invalid('current-password', 'current-password-error', '请输入当前管理密码。');
  if (!next) invalid('new-password', 'new-password-error', '请输入新密码。');
  else if (next.length < PASSWORD_MIN) invalid('new-password', 'new-password-error', `新密码至少需要 ${PASSWORD_MIN} 个字符，当前 ${next.length} 个。`);
  if (!confirm) invalid('confirm-password', 'confirm-password-error', '请再次输入新密码。');
  else if (next && confirm !== next) invalid('confirm-password', 'confirm-password-error', '两次输入的新密码不一致。');
  if (first) first.focus();
  return !first;
}

async function submitPassword(event) {
  event.preventDefault();
  if (passwordBusy || busy || uploading || !authenticated) return;
  if (!validatePasswordForm()) return;
  passwordBusy = true;
  stopPolling();
  const requestEpoch = epoch;
  $('password-success').textContent = '';
  $('password-success').hidden = true;
  actBusy('password', true, '正在更新…');
  updateControls();
  let succeeded = false;
  try {
    const result = await api('/api/password', {
      current_password: $('current-password').value,
      new_password: $('new-password').value
    });
    if (requestEpoch !== epoch) return;
    if (result.ok !== true) throw new ApiError('服务未确认密码已更新，请重新登录后再试。');
    succeeded = true;
  } catch (error) {
    if (requestEpoch !== epoch) return;
    // 4xx 响应携带后端消息；会话是否失效由随后的状态探测决定。
    $('password-form-error').textContent = error.message;
    $('password-form-error').hidden = false;
  } finally {
    passwordBusy = false;
    actBusy('password', false);
    updateControls();
  }
  if (!succeeded) {
    schedulePoll();
    return;
  }
  resetPasswordPanel();
  $('password-success').textContent = PASSWORD_SUCCESS;
  $('password-success').hidden = false;
  const probeEpoch = epoch;
  try {
    const state = await api('/api/state');
    if (probeEpoch !== epoch) return;
    renderState(state, false);
    schedulePoll();
  } catch (error) {
    if (probeEpoch !== epoch) return;
    if (error.status === 401) {
      showLogin(PASSWORD_SUCCESS);
      return;
    }
    schedulePoll();
  }
}

/* ── 运行日志：查看、筛选与导出 ─────────────────────────────────────── */

const LOGS_MAX = 600;
const LOGS_POLL_MS = 5000;
const LOG_LEVELS = { DEBUG: '调试', INFO: '信息', WARNING: '警告', ERROR: '错误', CRITICAL: '严重' };
const logs = { entries: [], latest: 0, timer: null, loading: false, queued: false, active: false, loaded: false, searchTimer: null };

function noteClient(kind, message) {
  clientLog.push({ time: new Date(), kind, message: String(message).slice(0, 800) });
  if (clientLog.length > 200) clientLog.splice(0, clientLog.length - 200);
}

function startLogs() {
  logs.active = true;
  if (!logs.loaded) loadLogs(true);
  else scheduleLogs();
}

function stopLogs() {
  logs.active = false;
  clearTimeout(logs.timer);
  logs.timer = null;
}

function scheduleLogs() {
  clearTimeout(logs.timer);
  logs.timer = null;
  if (logs.active && authenticated && $('logs-follow').checked) logs.timer = setTimeout(() => loadLogs(false), LOGS_POLL_MS);
}

async function loadLogs(reset) {
  if (!authenticated) return;
  if (logs.loading) { if (reset) logs.queued = true; return; }
  clearTimeout(logs.timer);
  logs.loading = true;
  const requestEpoch = epoch;
  const params = new URLSearchParams({ level: $('logs-level').value, limit: '500' });
  const query = $('logs-search').value.trim();
  if (query) params.set('q', query.slice(0, 200));
  if (!reset && logs.latest) params.set('after', String(logs.latest));
  $('logs-refresh').setAttribute('aria-busy', 'true');
  try {
    const data = await api(`/api/logs?${params}`);
    if (requestEpoch !== epoch) return;
    const fresh = (Array.isArray(data.entries) ? data.entries : []).filter((entry) => entry && Number.isFinite(Number(entry.seq))).map((entry) => ({
      seq: Number(entry.seq), time: Number(entry.time), level: str(entry.level).toUpperCase(), source: str(entry.source), message: String(entry.message ?? '')
    }));
    const view = $('logs-view');
    const nearBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 32;
    if (reset) logs.entries = fresh;
    else logs.entries.push(...fresh);
    const overflow = logs.entries.length - LOGS_MAX;
    if (overflow > 0) logs.entries.splice(0, overflow);
    logs.latest = Math.max(reset ? 0 : logs.latest, Number(data.latest) || 0);
    logs.loaded = true;
    renderLogs(reset || overflow > 0 ? null : fresh, reset || nearBottom);
    const state = $('logs-state');
    state.textContent = `${logs.entries.length} 条 · ${formatTime(Date.now() / 1000)}`;
    state.dataset.tone = '';
    state.title = data.file ? `日志文件：${data.file}` : '日志文件不可用，仅保留内存中的记录';
  } catch (error) {
    if (requestEpoch !== epoch) return;
    if (error.status === 401) { showLogin(error.message); return; }
    const state = $('logs-state');
    state.textContent = '读取失败';
    state.dataset.tone = 'error';
    state.title = error.message;
  } finally {
    logs.loading = false;
    $('logs-refresh').setAttribute('aria-busy', 'false');
    if (logs.queued) { logs.queued = false; loadLogs(true); } else scheduleLogs();
  }
}

function logLine(entry) {
  const line = makeElement('div', 'svc-log');
  line.dataset.level = entry.level;
  const when = Number.isFinite(entry.time) ? formatTime(entry.time, true) : '—';
  line.append(
    makeElement('span', 'svc-log-time mono', when),
    makeElement('span', 'svc-log-level', LOG_LEVELS[entry.level] || entry.level),
    makeElement('span', 'svc-log-source mono', entry.source),
    makeElement('span', 'svc-log-msg mono', entry.message)
  );
  return line;
}

function renderLogs(appended, stick) {
  const view = $('logs-view');
  if (appended) {
    if (appended.length) view.append(...appended.map(logLine));
  } else if (!logs.entries.length) {
    const filtered = $('logs-search').value.trim() || $('logs-level').value !== 'INFO';
    view.replaceChildren(makeElement('p', 'logs-empty', filtered ? '没有符合条件的日志。' : '暂无日志记录。'));
  } else {
    view.replaceChildren(...logs.entries.map(logLine));
  }
  const empty = view.querySelector('.logs-empty');
  if (empty && logs.entries.length) empty.remove();
  if (stick) view.scrollTop = view.scrollHeight;
}

function clientLogText() {
  const lines = clientLog.map((entry) => `${entry.time.toLocaleString('zh-CN', { hour12: false })} ${entry.kind} ${entry.message}`);
  return ['', '==== 浏览器端记录 ====', `页面：${location.origin}${location.pathname}`, `浏览器：${navigator.userAgent}`,
    `导出时间：${new Date().toLocaleString('zh-CN', { hour12: false })}`, ...(lines.length ? lines : ['（本次打开页面后没有错误提示）']), ''].join('\n');
}

function saveTextFile(text, filename) {
  saveBlob(new Blob([text], { type: 'text/plain;charset=utf-8' }), filename);
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function exportLogs() {
  if (!authenticated) return;
  const button = $('logs-export');
  if (button.getAttribute('aria-busy') === 'true') return;
  buttonBusy(button, true, '正在导出…');
  try {
    const data = await api('/api/logs/export', undefined, 60000);
    const name = /^[\w.-]{1,120}$/.test(str(data.filename)) ? str(data.filename) : 'ps5-manager-logs.txt';
    saveTextFile(String(data.text ?? '') + clientLogText(), name);
    toast(`已导出 ${name}，反馈问题时附上这个文件即可。`, 'success');
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(`导出日志失败：${error.message}`, 'error');
  } finally {
    buttonBusy(button, false, '导出日志');
  }
}

/* ── 自检 ───────────────────────────────────────────────────────────── */

const SELFCHECK_LABEL = { ok: '正常', warn: '注意', fail: '异常', skip: '跳过' };
const selfcheck = { loaded: false, last: null };

function selfcheckItem(item) {
  const status = SELFCHECK_LABEL[item.status] ? item.status : 'fail';
  const node = makeElement('li', 'selfcheck-item');
  node.dataset.status = status;
  node.append(makeElement('span', 'selfcheck-badge', SELFCHECK_LABEL[status]),
    makeElement('span', 'selfcheck-title', str(item.title)),
    makeElement('p', 'selfcheck-detail', str(item.detail)));
  if (item.fix && status !== 'ok') node.append(makeElement('p', 'selfcheck-fix', `建议：${str(item.fix)}`));
  return node;
}

function renderSelfcheck(last) {
  selfcheck.last = last && typeof last === 'object' && Array.isArray(last.results) ? last : null;
  const list = $('selfcheck-list');
  if (!selfcheck.last) {
    list.hidden = true;
    list.replaceChildren();
    $('selfcheck-time').textContent = '本次启动后还没有运行过。';
    return;
  }
  const results = selfcheck.last.results;
  list.replaceChildren(...results.map(selfcheckItem));
  list.hidden = !results.length;
  const failed = results.filter((item) => item.status === 'fail').length;
  const warned = results.filter((item) => item.status === 'warn').length;
  const when = new Date(str(selfcheck.last.time));
  const stamp = Number.isNaN(when.getTime()) ? str(selfcheck.last.time) : when.toLocaleString('zh-CN', { hour12: false });
  $('selfcheck-time').textContent = `${stamp} · ${failed ? `${failed} 项异常` : '没有异常'}${warned ? `，${warned} 项需要注意` : ''}`;
}

async function loadSelfcheck() {
  if (!authenticated) return;
  try {
    const data = await api('/api/selfcheck');
    selfcheck.loaded = true;
    if (!selfcheck.last) renderSelfcheck(data.last);
  } catch (error) {
    if (error.status === 401) showLogin(error.message);
  }
}

async function runSelfcheck() {
  const button = $('selfcheck-run');
  if (!authenticated || button.getAttribute('aria-busy') === 'true') return;
  buttonBusy(button, true, '检查中…');
  try {
    const data = await api('/api/selfcheck', {}, 30000);
    selfcheck.loaded = true;
    renderSelfcheck(data.last);
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(`自检失败：${error.message}`, 'error');
  } finally {
    buttonBusy(button, false, '开始自检');
  }
}

/* ── 通知 ───────────────────────────────────────────────────────────── */

const NOTIFY_TYPES = { webhook: '通用 Webhook', bark: 'Bark', ntfy: 'ntfy', telegram: 'Telegram' };
// Element id -> request field, per channel type. Secret fields stay blank when editing.
const NOTIFY_FIELDS = {
  webhook: { 'notify-url': 'url' },
  bark: { 'notify-bark-server': 'server', 'notify-bark-key': 'device_key' },
  ntfy: { 'notify-ntfy-server': 'server', 'notify-ntfy-topic': 'topic', 'notify-ntfy-token': 'token' },
  telegram: { 'notify-tg-token': 'token', 'notify-tg-chat': 'chat_id' }
};
const NOTIFY_PUBLIC = new Set(['server', 'chat_id']);
const NOTIFY_DEFAULT_EVENTS = ['transfer.finished', 'transfer.failed', 'extract.finished', 'extract.failed', 'update.finished', 'update.failed', 'gateway.error'];
const notifyState = { loaded: false, channels: [], events: {}, editing: null, busy: false };

function normalizeNotify(data) {
  const channels = Array.isArray(data && data.channels) ? data.channels.filter((item) => item && typeof item.id === 'string') : [];
  const events = data && data.events && typeof data.events === 'object' ? data.events : notifyState.events;
  return { channels, events };
}

function notifyItem(channel) {
  const node = makeElement('li', 'notify-item');
  const main = makeElement('div', 'notify-item-main');
  main.append(makeElement('span', 'notify-item-name', str(channel.name) || NOTIFY_TYPES[channel.type] || '渠道'));
  const events = Array.isArray(channel.events) ? channel.events.length : 0;
  const parts = [NOTIFY_TYPES[channel.type] || str(channel.type), `${events} 个事件`];
  if (channel.enabled === false) parts.push('已停用');
  const meta = makeElement('span', 'notify-item-meta', parts.join(' · '));
  if (!channel.has_secret || channel.secret_ok === false) {
    meta.textContent += ' · 需要重新填写密钥';
    meta.dataset.tone = 'warn';
  }
  main.append(meta);
  const actions = makeElement('div', 'notify-item-actions');
  const button = (label, handler) => {
    const element = makeElement('button', 'button button-secondary button-small', label);
    element.type = 'button';
    element.addEventListener('click', handler);
    return element;
  };
  actions.append(button('测试', () => testNotify(channel.id)), button('编辑', () => openNotifyDialog(channel)),
    button('删除', () => deleteNotify(channel)));
  node.append(main, actions);
  return node;
}

function renderNotify() {
  const list = $('notify-list');
  list.replaceChildren(...notifyState.channels.map(notifyItem));
  list.hidden = !notifyState.channels.length;
  $('notify-empty').hidden = notifyState.channels.length > 0;
}

async function loadNotify() {
  if (!authenticated) return;
  try {
    Object.assign(notifyState, normalizeNotify(await api('/api/notify')), { loaded: true });
    renderNotify();
  } catch (error) {
    if (error.status === 401) showLogin(error.message);
  }
}

function showNotifyType(type) {
  for (const group of $$('.notify-type-group')) group.hidden = group.dataset.notifyType !== type;
}

function openNotifyDialog(channel = null) {
  if (!authenticated) return;
  notifyState.editing = channel;
  $('notify-dialog-title').textContent = channel ? '编辑通知渠道' : '添加通知渠道';
  $('notify-name').value = channel ? str(channel.name) : '';
  const type = channel && NOTIFY_FIELDS[channel.type] ? channel.type : 'bark';
  $('notify-type').value = type;
  $('notify-type').disabled = Boolean(channel);
  for (const fields of Object.values(NOTIFY_FIELDS)) {
    for (const [id, key] of Object.entries(fields)) $(id).value = channel && NOTIFY_PUBLIC.has(key) ? str(channel[key]) : '';
  }
  const chosen = new Set(channel ? channel.events || [] : NOTIFY_DEFAULT_EVENTS);
  $('notify-events').replaceChildren(...Object.entries(notifyState.events).map(([event, label]) => {
    const wrapper = makeElement('label', 'nas-check');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = event;
    input.checked = chosen.has(event);
    wrapper.append(input, makeElement('span', '', str(label)));
    return wrapper;
  }));
  $('notify-enabled').checked = !channel || channel.enabled !== false;
  $('notify-proxy').checked = !channel || channel.use_proxy !== false;
  $('notify-secret-note').hidden = !channel;
  showFieldMessage('notify-form-error', '');
  showNotifyType(type);
  $('notify-dialog').showModal();
  $('notify-name').focus({ preventScroll: true });
}

function notifyFormPayload() {
  const type = $('notify-type').value;
  const channel = { type, name: $('notify-name').value.trim(), enabled: $('notify-enabled').checked,
    use_proxy: $('notify-proxy').checked,
    events: $$('#notify-events input').filter((input) => input.checked).map((input) => input.value) };
  if (notifyState.editing) channel.id = notifyState.editing.id;
  for (const [id, key] of Object.entries(NOTIFY_FIELDS[type] || {})) channel[key] = $(id).value.trim();
  return channel;
}

async function saveNotify(event) {
  event.preventDefault();
  if (notifyState.busy) return;
  notifyState.busy = true;
  buttonBusy('notify-save', true);
  showFieldMessage('notify-form-error', '');
  try {
    const data = await api('/api/notify/save', { channel: notifyFormPayload() });
    Object.assign(notifyState, normalizeNotify(data));
    renderNotify();
    $('notify-dialog').close();
    toast('通知渠道已保存，可以点「测试」确认。', 'success');
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    showFieldMessage('notify-form-error', error.message);
  } finally {
    notifyState.busy = false;
    buttonBusy('notify-save', false);
  }
}

async function testNotify(id) {
  try {
    await api('/api/notify/test', { id }, 20000);
    toast('测试消息已发出，请到手机上查看。', 'success');
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(error.message, 'error');
  }
}

async function deleteNotify(channel) {
  if (!window.confirm(`删除通知渠道「${str(channel.name)}」？`)) return;
  try {
    Object.assign(notifyState, normalizeNotify(await api('/api/notify/delete', { id: channel.id })));
    renderNotify();
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(error.message, 'error');
  }
}

/* ── 备份与恢复 ─────────────────────────────────────────────────────── */

const BACKUP_MAGIC = 'PS5JBBK1';
const RESTORE_STATE = { restored: '已恢复', skipped: '已跳过', failed: '失败' };
const backupState = { encrypted: false };

function showFieldMessage(id, message) {
  const node = $(id);
  node.textContent = message || '';
  node.hidden = !message;
}

function backupFilename(header, fallback) {
  const match = /filename="([\w.-]{1,120})"/.exec(str(header));
  return match ? match[1] : fallback;
}

async function downloadBackup() {
  if (!authenticated) return;
  const button = $('backup-download');
  if (button.getAttribute('aria-busy') === 'true') return;
  const payloads = $('backup-payloads').checked;
  const includeKey = $('backup-key').checked;
  const passphrase = $('backup-passphrase').value;
  if (includeKey && passphrase.length < 10) {
    showFieldMessage('backup-error', '包含密钥时需要设置至少 10 个字符的备份口令。');
    $('backup-passphrase').focus();
    return;
  }
  showFieldMessage('backup-error', '');
  buttonBusy(button, true, '正在打包…');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 600000);
  try {
    const options = { credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/octet-stream' } };
    let url = `/api/backup?payloads=${payloads ? 1 : 0}`;
    if (includeKey) {
      url = '/api/backup';
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-CSRF-Token'] = csrf;
      options.body = JSON.stringify({ payloads, include_key: true, passphrase });
    }
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      throw new ApiError(error.name === 'AbortError' ? '打包超时，请稍后重试。' : '无法连接管理服务，请检查网络后重试。');
    }
    if (!response.ok) {
      let data = null;
      try { data = await response.json(); } catch { data = null; }
      throw new ApiError(data && typeof data.error === 'string' ? data.error : '备份失败，请查看运行日志。', response.status);
    }
    const blob = await response.blob();
    const name = backupFilename(response.headers.get('Content-Disposition'), includeKey ? 'ps5-jb-backup.ps5bak' : 'ps5-jb-backup.tar.gz');
    saveBlob(blob, name);
    $('backup-passphrase').value = '';
    toast(`已下载 ${name}（${formatBytes(blob.size)}）。`, 'success');
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    showFieldMessage('backup-error', error.message);
  } finally {
    clearTimeout(timeout);
    buttonBusy(button, false, '下载备份');
  }
}

async function inspectRestoreFile() {
  const file = $('restore-file').files?.[0] || null;
  showFieldMessage('restore-error', '');
  $('restore-result').hidden = true;
  backupState.encrypted = false;
  if (file) {
    try {
      const head = new Uint8Array(await file.slice(0, BACKUP_MAGIC.length).arrayBuffer());
      backupState.encrypted = String.fromCharCode(...head) === BACKUP_MAGIC;
    } catch {
      backupState.encrypted = false;
    }
  }
  $('restore-passphrase-group').hidden = !backupState.encrypted;
  $('restore-start').disabled = !file;
}

function openRestoreDialog() {
  const file = $('restore-file').files?.[0];
  if (!authenticated || !file) return;
  if (backupState.encrypted && !$('restore-passphrase').value) {
    showFieldMessage('restore-error', '这个备份包含密钥，请输入备份口令。');
    $('restore-passphrase').focus();
    return;
  }
  $('restore-file-name').textContent = `${file.name} · ${formatBytes(file.size)}`;
  $('restore-dialog').showModal();
  $('restore-cancel').focus({ preventScroll: true });
}

function renderRestoreResult(data) {
  const list = $('restore-result');
  const items = Array.isArray(data && data.items) ? data.items : [];
  list.replaceChildren(...items.map((item) => {
    const state = RESTORE_STATE[item.state] ? item.state : 'failed';
    const node = makeElement('li', '', `${str(item.title)}：${RESTORE_STATE[state]}${item.detail ? ` · ${str(item.detail)}` : ''}`);
    node.dataset.state = state;
    return node;
  }));
  list.hidden = !items.length;
  return items;
}

async function confirmRestore() {
  $('restore-dialog').close();
  const file = $('restore-file').files?.[0];
  const button = $('restore-start');
  if (!authenticated || !file || button.getAttribute('aria-busy') === 'true') return;
  if (!csrf) { showLogin('登录验证信息缺失，请重新登录。'); return; }
  showFieldMessage('restore-error', '');
  buttonBusy(button, true, '正在恢复…');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 600000);
  try {
    const headers = { Accept: 'application/json', 'Content-Type': 'application/octet-stream', 'X-CSRF-Token': csrf };
    if (backupState.encrypted) headers['X-Backup-Passphrase'] = encodeURIComponent($('restore-passphrase').value);
    let response;
    try {
      response = await fetch('/api/backup/restore', { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers, body: file, signal: controller.signal });
    } catch (error) {
      throw new ApiError(error.name === 'AbortError' ? '恢复超时，请查看运行日志。' : '无法连接管理服务，请检查网络后重试。');
    }
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (!response.ok) throw new ApiError(data && typeof data.error === 'string' ? data.error : '恢复失败，请查看运行日志。', response.status);
    const items = renderRestoreResult(data);
    $('restore-passphrase').value = '';
    const failed = items.filter((item) => item.state === 'failed').length;
    toast(failed ? `恢复完成，其中 ${failed} 项失败，详见卡片。` : '已从备份恢复。', failed ? 'warning' : 'success');
    dirty = false;
    refreshState({ forceConfig: true });
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    showFieldMessage('restore-error', error.message);
  } finally {
    clearTimeout(timeout);
    buttonBusy(button, false, '恢复…');
  }
}

function bindBackupEvents() {
  bindHistoryEvents();
  $('selfcheck-run').addEventListener('click', runSelfcheck);
  $('backup-nas-form').addEventListener('submit', startBackup);
  $('backup-nas-cancel').addEventListener('click', () => $('backup-nas-dialog').close());
  $('library-refresh').addEventListener('click', () => loadLibrary(true));
  $('library-search').addEventListener('input', renderLibrary);
  $('library-volume').addEventListener('change', renderLibrary);
  $('library-sort').addEventListener('change', renderLibrary);
  $('notify-add').addEventListener('click', () => openNotifyDialog(null));
  $('notify-type').addEventListener('change', () => showNotifyType($('notify-type').value));
  $('notify-form').addEventListener('submit', saveNotify);
  $('notify-cancel').addEventListener('click', () => $('notify-dialog').close());
  $('backup-key').addEventListener('change', () => {
    $('backup-passphrase-group').hidden = !$('backup-key').checked;
    showFieldMessage('backup-error', '');
  });
  $('backup-download').addEventListener('click', downloadBackup);
  $('restore-file').addEventListener('change', inspectRestoreFile);
  $('restore-start').addEventListener('click', openRestoreDialog);
  $('restore-cancel').addEventListener('click', () => $('restore-dialog').close());
  $('restore-confirm').addEventListener('click', confirmRestore);
}
