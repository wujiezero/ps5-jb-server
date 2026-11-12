'use strict';

/* ── 网络活动 ───────────────────────────────────────────────────────── */

function normalizeActivity(data) {
  const events = [];
  const list = Array.isArray(data && data.events) ? data.events : [];
  for (const entry of list) {
    if (!entry || typeof entry !== 'object') continue;
    const domain = str(entry.domain);
    const dst = str(entry.dst);
    const target = str(entry.target) || (domain ? `domain:${domain}` : dst ? `ip:${dst}` : '');
    const port = Number(entry.port);
    const kind = ['flow', 'service', 'dns'].includes(str(entry.kind)) ? str(entry.kind) : 'flow';
    events.push({
      target,
      kind,
      // Service rows are terminal observations, not whitelist targets; rows
      // already covered by the current whitelist need no second approval.
      selectable: kind !== 'service' && Boolean(target) && entry.allowed_now !== true,
      time: str(entry.time),
      client: str(entry.client),
      dst,
      port: Number.isInteger(port) && port > 0 && port <= 65535 ? port : null,
      domain,
      name: str(entry.name),
      category: str(entry.category),
      verdict: str(entry.verdict).toLowerCase() === 'blocked' ? 'blocked' : 'allowed',
      // local 来自 DNS 引擎的 action='local'：内置域名由本机直接应答，不是白名单放行。
      local: entry.local === true,
      allowedNow: typeof entry.allowed_now === 'boolean' ? entry.allowed_now : null
    });
  }
  const counts = { allowed: 0, blocked: 0 };
  const reported = data && data.counts && typeof data.counts === 'object' ? data.counts : null;
  if (reported) {
    for (const key of ['allowed', 'blocked']) {
      const value = Number(reported[key]);
      if (Number.isFinite(value) && value >= 0) counts[key] = Math.floor(value);
    }
  }
  if (!counts.allowed && !counts.blocked && events.length) {
    for (const event of events) counts[event.verdict] += 1;
  }
  const tracked = Number(data && data.tracked);
  const rawHints = data && typeof data.hints === 'object' && data.hints ? data.hints : {};
  const byKind = rawHints.by_kind && typeof rawHints.by_kind === 'object' ? rawHints.by_kind : {};
  const hints = {
    message: str(rawHints.message),
    gateway: Number(byKind.flow) || 0,
    service: Number(byKind.service) || 0,
    dns: Number(byKind.dns) || 0
  };
  return {
    loaded: true,
    hints,
    events,
    counts,
    tracked: Number.isFinite(tracked) && tracked >= 0 ? Math.floor(tracked) : 0,
    updated: str(data && data.updated),
    lastError: str(data && data.last_error),
    categories: Array.isArray(data && data.categories) ? [...new Set(data.categories.map((value) => str(value)).filter(Boolean))] : []
  };
}

function activityVisibleEvents() {
  const query = searchQuery('activity-search');
  const sources = { flow: '网关连接 gateway flow', dns: 'DNS 域名解析', service: '本机服务 local service' };
  const kindMatches = (event) => activityKindValue === 'all'
    || (activityKindValue === 'external' ? event.kind !== 'service' : event.kind === activityKindValue);
  return activity.events.filter((event) => (activityFilterValue === 'all' || event.verdict === activityFilterValue)
    && kindMatches(event)
    && matchesSearch(query, [event.domain, event.dst, event.client, event.port, event.name, event.category, event.kind, sources[event.kind]]));
}

function pruneActivitySelection() {
  const visible = new Set(activityVisibleEvents().filter((event) => event.selectable).map((event) => event.target));
  for (const target of activitySelected) if (!visible.has(target)) activitySelected.delete(target);
}

function activityDescribe(event) {
  const host = event.domain || event.dst || '目标';
  return event.port === null ? host : `${host}:${event.port}`;
}

function activityEmptyRow(message, withCta = false) {
  const row = document.createElement('tr');
  const cell = document.createElement('td');
  cell.colSpan = 9;
  cell.className = 'empty-state';
  const text = document.createElement('p');
  text.textContent = message;
  cell.append(text);
  if (withCta) {
    const wrap = document.createElement('p');
    wrap.className = 'empty-cta';
    const link = document.createElement('a');
    link.className = 'empty-link';
    link.href = '#/overview';
    link.textContent = '查看 PS5 网络设置';
    const ps5Link = document.createElement('a');
    ps5Link.className = 'empty-link';
    ps5Link.href = '#/ps5';
    ps5Link.textContent = '管理 PS5 主机';
    wrap.append(link, ps5Link);
    cell.append(wrap);
  }
  row.append(cell);
  return row;
}

function activityResultNote(text, tone) {
  const note = document.createElement('span');
  note.className = 'result-note';
  note.dataset.tone = tone;
  note.textContent = text;
  return note;
}

function activityRow(event) {
  const row = document.createElement('tr');
  row.dataset.verdict = event.verdict;

  const checkCell = document.createElement('td');
  checkCell.className = 'check-cell';
  if (event.selectable) {
    const label = document.createElement('label');
    label.className = 'activity-check-target';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.className = 'activity-check';
    box.dataset.target = event.target;
    box.checked = activitySelected.has(event.target);
    box.setAttribute('aria-label', `选择 ${activityDescribe(event)}`);
    box.addEventListener('change', () => toggleActivityTarget(event.target, box.checked));
    label.append(box);
    checkCell.append(label);
  }

  const timeCell = document.createElement('td');
  const time = document.createElement('time');
  time.className = 'mono';
  time.textContent = formatTime(event.time);
  if (event.time) time.setAttribute('datetime', event.time);
  timeCell.append(time);

  const clientCell = document.createElement('td');
  clientCell.className = 'mono';
  clientCell.textContent = asText(event.client);

  const domainCell = document.createElement('td');
  domainCell.className = 'mono';
  domainCell.textContent = asText(event.domain);
  if (!event.domain) domainCell.dataset.empty = 'true';
  // 内置域名由本机 DNS 直接应答，不是白名单放行；标在域名后面，不挤进结果列。
  if (event.kind === 'dns' && event.local) {
    const note = document.createElement('span');
    note.className = 'domain-note';
    note.textContent = '本机劫持';
    domainCell.append(note);
  }

  const ipCell = document.createElement('td');
  ipCell.className = 'mono';
  ipCell.textContent = asText(event.dst);

  const portCell = document.createElement('td');
  portCell.className = 'mono';
  portCell.textContent = event.port === null ? '—' : String(event.port);

  const verdictCell = document.createElement('td');
  const [label, tone] = event.verdict === 'blocked' ? ['未放行', 'warning'] : ['允许', 'good'];
  const badge = document.createElement('span');
  badge.className = 'result-badge';
  badge.dataset.tone = tone;
  badge.textContent = label;
  verdictCell.append(badge);
  // verdict 是历史结果；allowed_now 表示当前白名单是否已覆盖该目标。
  if (event.verdict === 'blocked' && event.allowedNow === true) verdictCell.append(activityResultNote('已放行（现在）', 'good'));
  else if (event.verdict === 'allowed' && event.allowedNow === false) verdictCell.append(activityResultNote('已移出白名单', 'warning'));

  const categoryCell = document.createElement('td');
  categoryCell.textContent = asText(event.category);

  const nameCell = document.createElement('td');
  if (event.kind === 'dns') {
    const tag = document.createElement('span');
    tag.className = 'activity-kind';
    tag.textContent = '域名解析';
    nameCell.append(tag);
    if (event.name) {
      const label = document.createElement('span');
      label.className = 'activity-label-name';
      label.textContent = event.name;
      nameCell.append(label);
    }
  } else {
    nameCell.textContent = asText(event.name, '');
  }
  if (!nameCell.textContent) nameCell.dataset.empty = 'true';

  row.append(checkCell, timeCell, clientCell, domainCell, ipCell, portCell, verdictCell, categoryCell, nameCell);
  // List layout (phones, tablets in portrait): tapping anywhere on the row toggles it.
  const box = checkCell.querySelector('input');
  if (box) {
    row.addEventListener('click', (clickEvent) => {
      if (clickEvent.target.closest('label, input, button, a') || !window.matchMedia('(max-width: 1023px)').matches) return;
      box.click();
    });
  }
  return row;
}

function renderActivityRows() {
  const visible = activityVisibleEvents();
  pruneActivitySelection();
  $('activity-search-summary').textContent = activity.loaded ? `显示 ${visible.length} / ${activity.events.length} 条活动`
    : activityReadError ? '网络活动读取失败，请刷新重试。' : '正在读取网络活动…';
  const fragment = document.createDocumentFragment();
  if (!activity.loaded) {
    fragment.append(activityReadError ? activityEmptyRow('网络活动读取失败，请刷新重试。') : skeletonRows(5, 9));
  } else if (!activity.events.length) {
    fragment.append(activityEmptyRow(activity.hints.message || '暂无网关连接、DNS 查询或本机服务事件。', true));
  } else if (!visible.length) {
    fragment.append(activityEmptyRow('没有匹配的网络活动，请调整搜索或结果筛选。'));
  } else {
    for (const event of visible) fragment.append(activityRow(event));
  }
  $('activity-body').replaceChildren(fragment);
}

function renderActivityCategories() {
  const key = activity.categories.join('\n');
  if (key === activityCategoryKey) return;
  activityCategoryKey = key;
  const fragment = document.createDocumentFragment();
  for (const category of activity.categories) {
    const option = document.createElement('option');
    option.value = category;
    option.textContent = category;
    fragment.append(option);
  }
  $('activity-category-options').replaceChildren(fragment);
}

function renderActivity() {
  const { allowed, blocked } = activity.counts;
  if (!activity.loaded) {
    $('activity-summary').textContent = '正在读取网络活动…';
  } else {
    const parts = [`共 ${allowed + blocked} 条`, `允许 ${allowed}`, `未放行 ${blocked}`];
    const { gateway, service, dns } = activity.hints;
    if (gateway || service || dns) parts.push(`来源：网关 ${gateway} · 本机服务 ${service} · DNS ${dns}`);
    parts.push(`本机跟踪 ${activity.tracked} 个连接`);
    $('activity-summary').textContent = parts.join(' · ');
  }
  setBadge('activity', activity.loaded ? blocked : 0);
  const updated = $('activity-updated');
  if (activity.updated) {
    updated.textContent = `更新于 ${formatTime(activity.updated)}`;
    updated.hidden = false;
  } else {
    updated.textContent = '';
    updated.hidden = true;
  }
  const warning = $('activity-warning');
  if (activity.lastError) {
    warning.textContent = activity.lastError;
    warning.hidden = false;
  } else {
    warning.textContent = '';
    warning.hidden = true;
  }
  renderActivityCategories();
  renderActivityRows();
  updateActivityControls();
  renderQuickLinks();
}

function updateActivityControls() {
  const locked = !authenticated || busy || activityBusy || activityLoading;
  const boxes = $$('#activity-body input[type="checkbox"]');
  const targets = new Set();
  for (const box of boxes) {
    box.disabled = locked;
    if (!box.dataset.target) continue;
    box.checked = activitySelected.has(box.dataset.target);
    targets.add(box.dataset.target);
  }
  let checkedVisible = 0;
  for (const target of targets) if (activitySelected.has(target)) checkedVisible += 1;

  const selectAll = $('activity-select-all');
  selectAll.disabled = locked || targets.size === 0;
  selectAll.checked = targets.size > 0 && checkedVisible === targets.size;
  selectAll.indeterminate = checkedVisible > 0 && checkedVisible < targets.size;

  const selectAllMobile = $('activity-select-all-mobile');
  selectAllMobile.disabled = locked || targets.size === 0;
  selectAllMobile.textContent = targets.size > 0 && checkedVisible === targets.size ? '取消全选' : '全选';

  const hasSelection = activitySelected.size > 0;
  setDisabled('allow', locked || !hasSelection);
  setDisabled('label', locked || !hasSelection);
  $('activity-filter').disabled = !authenticated || activityBusy;
  $('activity-kind').disabled = !authenticated || activityBusy;
  $('activity-search').disabled = !authenticated || activityBusy;
  for (const segment of $$('.segment')) segment.disabled = !authenticated || activityBusy;
  $('activity-selection').textContent = hasSelection ? `已选 ${activitySelected.size} 项` : '未选择';
  const bulkBar = $('activity-bulk-bar');
  bulkBar.dataset.visible = String(hasSelection);
  document.body.dataset.bulk = hasSelection ? 'on' : 'off';
}

function toggleActivityTarget(target, checked) {
  if (!target) return;
  if (checked) activitySelected.add(target);
  else activitySelected.delete(target);
  updateActivityControls();
  if (!activitySelected.size) flushActivityPending();
}

function setActivityFilter(value) {
  activityFilterValue = value === 'blocked' || value === 'allowed' ? value : 'all';
  if ($('activity-filter').value !== activityFilterValue) $('activity-filter').value = activityFilterValue;
  for (const segment of $$('.segment')) segment.setAttribute('aria-pressed', String(segment.dataset.filter === activityFilterValue));
  renderActivityRows();
  updateActivityControls();
  flushActivityPending();
}

function setActivityKind(value) {
  activityKindValue = ['flow', 'dns', 'service', 'external'].includes(value) ? value : 'all';
  if ($('activity-kind').value !== activityKindValue) $('activity-kind').value = activityKindValue;
  try { localStorage.setItem('ps5jb.activityKind', activityKindValue); } catch { /* private mode */ }
  renderActivityRows();
  updateActivityControls();
  flushActivityPending();
}

function applyActivity(next) {
  // 打开的对话框、进行中的批量操作或已有选择时，表格归用户所有，快照先暂存。
  if (dialogsOpen() || activityBusy || activitySelected.size) {
    activityPending = true;
    return;
  }
  Object.assign(activity, next);
  activityPending = false;
  renderActivity();
}

function flushActivityPending() {
  if (!activityPending || !authenticated || dialogsOpen() || activityBusy || activitySelected.size) return;
  activityPending = false;
  refreshActivity();
}

function resetActivityPanel() {
  activityReadError = '';
  $('activity-search').value = '';
  Object.assign(activity, { loaded: false, events: [], counts: { allowed: 0, blocked: 0 }, tracked: 0, updated: '', lastError: '', categories: [] });
  activityPending = false;
  activityCategoryKey = null;
  activitySelected.clear();
  setActivityFilter('all');
  $('activity-label-form').reset();
  $('activity-label-fields').disabled = false;
  $('activity-label-cancel').disabled = false;
  $('activity-label-error').textContent = '';
  $('activity-label-error').hidden = true;
  $('activity-label-status').textContent = '';
  $('activity-allow-error').textContent = '';
  $('activity-allow-error').hidden = true;
  $('activity-allow-cancel').disabled = false;
  $('activity-allow-confirm').disabled = false;
  renderActivity();
}

async function refreshActivity({ manual = false } = {}) {
  if (!authenticated) return false;
  if (activityLoading || activityBusy || dialogsOpen() || activitySelected.size) {
    if (activityBusy || activitySelected.size) activityPending = true;
    if (manual) toast('已选择目标或正在进行操作，清除后才能刷新网络活动。', 'info');
    return false;
  }
  activityLoading = true;
  activityPending = false;
  const requestEpoch = epoch;
  if (manual) actBusy('refresh', true);
  updateActivityControls();
  try {
    const data = await api(`/api/activity?limit=${ACTIVITY_LIMIT}`);
    if (requestEpoch !== epoch) return false;
    activityReadError = '';
    applyActivity(normalizeActivity(data));
    if (manual) toast('网络活动已刷新。', 'success');
    return true;
  } catch (error) {
    if (requestEpoch !== epoch) return false;
    if (error.status === 401) {
      showLogin(error.message);
      return false;
    }
    activityReadError = error.message;
    $('activity-warning').textContent = `网络活动读取失败：${error.message}`;
    $('activity-warning').hidden = false;
    if (!activity.loaded) $('activity-summary').textContent = '网络活动读取失败。';
    renderActivityRows();
    return false;
  } finally {
    activityLoading = false;
    if (manual) actBusy('refresh', false);
    updateActivityControls();
  }
}

function openActivityAllowDialog() {
  if (!authenticated || activityBusy || activityLoading || !activitySelected.size || $('activity-allow-dialog').open) return;
  $('activity-allow-count').textContent = String(activitySelected.size);
  $('activity-allow-error').textContent = '';
  $('activity-allow-error').hidden = true;
  $('activity-allow-cancel').disabled = false;
  $('activity-allow-confirm').disabled = false;
  $('activity-allow-dialog').showModal();
  $('activity-allow-cancel').focus({ preventScroll: true });
}

async function confirmActivityAllow() {
  if (activityBusy || !authenticated) return;
  pruneActivitySelection();
  const targets = [...activitySelected];
  if (!targets.length) {
    $('activity-allow-dialog').close();
    return;
  }
  activityBusy = true;
  updateActivityControls();
  $('activity-allow-error').textContent = '';
  $('activity-allow-error').hidden = true;
  $('activity-allow-cancel').disabled = true;
  $('activity-allow-confirm').disabled = true;
  buttonBusy('activity-allow-confirm', true, '正在放行…');
  const requestEpoch = epoch;
  let allowed = 0;
  try {
    const result = await api('/api/allow', { targets });
    if (requestEpoch !== epoch) return;
    if (result.ok !== true) throw new ApiError('服务未确认放行结果，请刷新后检查。');
    allowed = targets.length;
    $('activity-allow-dialog').close();
  } catch (error) {
    if (requestEpoch !== epoch) return;
    if (error.status === 401) {
      $('activity-allow-dialog').close();
      showLogin(error.message);
      return;
    }
    $('activity-allow-error').textContent = error.message;
    $('activity-allow-error').hidden = false;
  } finally {
    activityBusy = false;
    buttonBusy('activity-allow-confirm', false, '确认放行');
    $('activity-allow-cancel').disabled = false;
    updateActivityControls();
  }
  if (!allowed) return;
  activitySelected.clear();
  toast(`已放行 ${allowed} 项，白名单已更新。`, 'success');
  updateActivityControls();
  flushActivityPending();
  refreshActivity();
}

function openActivityLabelDialog() {
  if (!authenticated || activityBusy || activityLoading || !activitySelected.size || $('activity-label-dialog').open) return;
  $('activity-label-count').textContent = String(activitySelected.size);
  $('activity-label-name').value = '';
  $('activity-label-category').value = '';
  $('activity-label-error').textContent = '';
  $('activity-label-error').hidden = true;
  $('activity-label-status').textContent = '';
  $('activity-label-fields').disabled = false;
  $('activity-label-cancel').disabled = false;
  $('activity-label-save').disabled = false;
  $('activity-label-dialog').showModal();
  $('activity-label-name').focus({ preventScroll: true });
}

async function submitActivityLabels(event) {
  event.preventDefault();
  if (activityBusy || !authenticated) return;
  pruneActivitySelection();
  const targets = [...activitySelected];
  if (!targets.length) {
    $('activity-label-dialog').close();
    return;
  }
  const name = $('activity-label-name').value.trim();
  const category = $('activity-label-category').value.trim();
  activityBusy = true;
  updateActivityControls();
  $('activity-label-error').textContent = '';
  $('activity-label-error').hidden = true;
  $('activity-label-fields').disabled = true;
  $('activity-label-cancel').disabled = true;
  $('activity-label-save').disabled = true;
  buttonBusy('activity-label-save', true, '正在保存…');
  const requestEpoch = epoch;
  let applied = 0;
  try {
    for (const target of targets) {
      $('activity-label-status').textContent = `正在应用 ${applied + 1}/${targets.length}…`;
      const result = await api('/api/labels', { target, name, category });
      if (requestEpoch !== epoch) return;
      if (result.ok !== true) throw new ApiError(`目标 ${target} 未确认保存，请重试。`);
      applied += 1;
      // 已写入的目标移出选择，重试时只处理剩下的。
      activitySelected.delete(target);
    }
    $('activity-label-dialog').close();
  } catch (error) {
    if (requestEpoch !== epoch) return;
    if (error.status === 401) {
      $('activity-label-dialog').close();
      showLogin(error.message);
      return;
    }
    $('activity-label-error').textContent = applied
      ? `已更新 ${applied} 项，写入第 ${applied + 1} 项（${targets[applied]}）时失败：${error.message}`
      : error.message;
    $('activity-label-error').hidden = false;
    if (applied) activityPending = true;
  } finally {
    activityBusy = false;
    buttonBusy('activity-label-save', false, '应用命名');
    $('activity-label-fields').disabled = false;
    $('activity-label-cancel').disabled = false;
    $('activity-label-status').textContent = '';
    $('activity-label-count').textContent = String(activitySelected.size);
    updateActivityControls();
  }
  if (!applied) return;
  toast(`已更新 ${applied} 项命名。`, 'success');
  if ($('activity-label-dialog').open) {
    activityPending = true;
  } else {
    flushActivityPending();
    refreshActivity();
  }
}

/* ── 网络活动：历史与统计 ───────────────────────────────────────────── */

const actHistory = { mode: 'live', loaded: false, events: [], next: null, busy: false, stats: null, days: 7, searchTimer: null };

function setActivityMode(mode) {
  actHistory.mode = ['live', 'history', 'stats'].includes(mode) ? mode : 'live';
  for (const button of $$('[data-activity-mode]')) button.setAttribute('aria-pressed', String(button.dataset.activityMode === actHistory.mode));
  $('activity-live').hidden = actHistory.mode !== 'live';
  $('activity-history').hidden = actHistory.mode !== 'history';
  $('activity-stats').hidden = actHistory.mode !== 'stats';
  if (actHistory.mode === 'history' && !actHistory.loaded) loadHistory(true);
  if (actHistory.mode === 'stats') loadStats();
}

function historyQuery() {
  const params = new URLSearchParams();
  const range = Number($('history-range').value);
  if (range > 0) params.set('from', String(Math.floor(Date.now() / 1000) - range));
  for (const [id, key] of [['history-verdict', 'verdict'], ['history-kind', 'kind']]) {
    if ($(id).value) params.set(key, $(id).value);
  }
  const text = $('history-search').value.trim();
  if (text) params.set('q', text.slice(0, 200));
  return params;
}

function renderHistory() {
  const body = $('history-body');
  const rows = normalizeActivity({ events: actHistory.events }).events.map((event) => activityRow({ ...event, selectable: false }));
  body.replaceChildren(...(rows.length ? rows : [emptyHistoryRow(actHistory.loaded ? '这个时间范围内没有符合条件的记录。' : '正在读取…')]));
  $('history-more').hidden = !actHistory.next;
  $('history-summary').textContent = actHistory.loaded
    ? `已显示 ${actHistory.events.length} 条${actHistory.next ? '，还有更早的记录' : ''}。`
    : '正在读取历史…';
}

function emptyHistoryRow(text) {
  const row = document.createElement('tr');
  const cell = makeElement('td', 'empty-state', text);
  cell.colSpan = 9;
  row.append(cell);
  return row;
}

async function loadHistory(reset) {
  if (!authenticated || actHistory.busy) return;
  actHistory.busy = true;
  if (!reset) buttonBusy('history-more', true);
  try {
    const params = historyQuery();
    params.set('limit', '200');
    if (!reset && actHistory.next) params.set('before_id', String(actHistory.next));
    const data = await api(`/api/activity/history?${params}`);
    const events = Array.isArray(data.events) ? data.events : [];
    actHistory.events = reset ? events : actHistory.events.concat(events);
    actHistory.next = Number.isInteger(data.next_before) ? data.next_before : null;
    actHistory.loaded = true;
    renderHistory();
    if (data.error) toast(str(data.error), 'warning');
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    $('history-summary').textContent = `读取历史失败：${error.message}`;
  } finally {
    actHistory.busy = false;
    buttonBusy('history-more', false);
  }
}

function svgNode(tag, attributes = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

function renderStatsChart(days) {
  const svg = $('stats-chart');
  const width = 600;
  const height = 160;
  const top = 8;
  const bottom = 20;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  const max = Math.max(1, ...days.map((day) => day.allowed + day.blocked));
  const slot = width / Math.max(days.length, 1);
  const barWidth = Math.max(2, slot * 0.7);
  const nodes = [];
  days.forEach((day, index) => {
    const x = index * slot + (slot - barWidth) / 2;
    const scale = (height - top - bottom) / max;
    const allowedHeight = day.allowed * scale;
    const blockedHeight = day.blocked * scale;
    const base = height - bottom;
    const allowed = svgNode('rect', { class: 'bar-allowed', x: x.toFixed(1), y: (base - allowedHeight).toFixed(1), width: barWidth.toFixed(1), height: allowedHeight.toFixed(1) });
    const blocked = svgNode('rect', { class: 'bar-blocked', x: x.toFixed(1), y: (base - allowedHeight - blockedHeight).toFixed(1), width: barWidth.toFixed(1), height: blockedHeight.toFixed(1) });
    const title = svgNode('title');
    title.textContent = `${day.day}：允许 ${day.allowed}，未放行 ${day.blocked}`;
    allowed.append(title);
    nodes.push(allowed, blocked);
    if (days.length <= 7 || index % 5 === 0) {
      const label = svgNode('text', { class: 'axis-label', x: (index * slot + slot / 2).toFixed(1), y: height - 6, 'text-anchor': 'middle' });
      label.textContent = day.day.slice(5);
      nodes.push(label);
    }
  });
  svg.replaceChildren(...nodes);
}

function statsTarget(item, allowable) {
  const node = makeElement('li', 'stats-item');
  const main = makeElement('div', 'stats-item-main');
  const [kind, ...rest] = str(item.target).split(':');
  main.append(makeElement('span', 'stats-item-target', rest.join(':')));
  const meta = [`${item.hits} 次`, `${item.clients} 个客户端`];
  if (item.name) meta.unshift(str(item.name));
  if (item.category) meta.push(str(item.category));
  if (item.allowed_now && allowable) meta.push('已放行（现在）');
  main.append(makeElement('span', 'stats-item-meta', meta.join(' · ')));
  node.append(main);
  if (allowable && !item.allowed_now && ['domain', 'ip'].includes(kind)) {
    const button = makeElement('button', 'button button-secondary button-small', '放行');
    button.type = 'button';
    button.addEventListener('click', () => allowStatsTarget(str(item.target), button));
    node.append(button);
  }
  return node;
}

function renderStats() {
  const stats = actHistory.stats;
  if (!stats) return;
  const days = (Array.isArray(stats.per_day) ? stats.per_day : []).map((day) => ({ day: str(day.day), allowed: Number(day.allowed) || 0, blocked: Number(day.blocked) || 0 }));
  renderStatsChart(days);
  const total = days.reduce((sum, day) => sum + day.allowed + day.blocked, 0);
  const blocked = days.reduce((sum, day) => sum + day.blocked, 0);
  $('stats-summary').textContent = total
    ? `最近 ${stats.days} 天共 ${total} 条记录，其中未放行 ${blocked} 条；历史库共 ${Number(stats.rows) || 0} 条。`
    : `最近 ${stats.days} 天还没有记录。`;
  const fill = (id, list, allowable) => {
    const items = Array.isArray(list) ? list : [];
    $(id).replaceChildren(...(items.length ? items.map((item) => statsTarget(item, allowable)) : [makeElement('li', 'stats-empty', '暂无')]));
  };
  fill('stats-top-blocked', stats.top_blocked, true);
  fill('stats-top-allowed', stats.top_allowed, false);
  const clients = Array.isArray(stats.clients) ? stats.clients : [];
  $('stats-clients').replaceChildren(...clients.map((item) => {
    const row = document.createElement('tr');
    const last = Number(item.last) ? new Date(Number(item.last) * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
    for (const [text, mono] of [[str(item.client) || '—', true], [String(item.allowed || 0), false], [String(item.blocked || 0), false], [last, false]]) {
      row.append(makeElement('td', mono ? 'mono' : '', text));
    }
    return row;
  }));
  if (stats.error) toast(str(stats.error), 'warning');
}

async function loadStats() {
  if (!authenticated) return;
  try {
    actHistory.stats = await api(`/api/activity/stats?days=${actHistory.days}`);
    renderStats();
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    $('stats-summary').textContent = `读取统计失败：${error.message}`;
  }
}

async function allowStatsTarget(target, button) {
  if (!authenticated) return;
  buttonBusy(button, true);
  try {
    await api('/api/allow', { targets: [target] });
    toast(`已放行 ${target.split(':').slice(1).join(':')}。`, 'success');
    await loadStats();
    refreshState({ forceConfig: !dirty });
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(error.message, 'error');
    buttonBusy(button, false);
  }
}

async function exportHistory() {
  if (!authenticated) return;
  const params = historyQuery();
  const button = $('history-export');
  buttonBusy(button, true);
  try {
    const response = await fetch(`/api/activity/export?${new URLSearchParams({ ...(params.get('from') ? { from: params.get('from') } : {}) })}`,
      { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new ApiError(response.status === 401 ? '登录已失效，请重新输入管理密码。' : '导出失败，请稍后重试。', response.status);
    const blob = await response.blob();
    saveBlob(blob, backupFilename(response.headers.get('Content-Disposition'), 'ps5-activity.csv'));
    toast(response.headers.get('X-Export-Truncated') === '1' ? '已导出最早的 50 000 条；缩小时间范围可导出其余记录。' : '已导出 CSV。', 'success');
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(error.message || '导出失败。', 'error');
  } finally {
    buttonBusy(button, false);
  }
}

async function clearHistory() {
  if (!authenticated || !window.confirm('清空全部活动历史？实时列表不受影响，这个操作不能撤销。')) return;
  buttonBusy('history-clear', true);
  try {
    const data = await api('/api/activity/history/clear', {});
    actHistory.loaded = false;
    actHistory.events = [];
    actHistory.next = null;
    toast(`已清空 ${Number(data.removed) || 0} 条活动历史。`, 'success');
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(error.message, 'error');
  } finally {
    buttonBusy('history-clear', false);
  }
}

function bindHistoryEvents() {
  for (const button of $$('[data-activity-mode]')) button.addEventListener('click', () => setActivityMode(button.dataset.activityMode));
  for (const id of ['history-range', 'history-verdict', 'history-kind']) $(id).addEventListener('change', () => loadHistory(true));
  $('history-search').addEventListener('input', () => {
    clearTimeout(actHistory.searchTimer);
    actHistory.searchTimer = setTimeout(() => loadHistory(true), 300);
  });
  $('history-more').addEventListener('click', () => loadHistory(false));
  $('history-export').addEventListener('click', exportHistory);
  $('history-clear').addEventListener('click', clearHistory);
  for (const button of $$('[data-stats-days]')) {
    button.addEventListener('click', () => {
      actHistory.days = Number(button.dataset.statsDays) === 30 ? 30 : 7;
      for (const other of $$('[data-stats-days]')) other.setAttribute('aria-pressed', String(other === button));
      loadStats();
    });
  }
}
