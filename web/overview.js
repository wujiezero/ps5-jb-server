'use strict';

/* ── 概览渲染 ───────────────────────────────────────────────────────── */

function actionInfo(action) {
  const value = asText(action, '').toLowerCase();
  if (value === 'local') return ['局域网', 'good'];
  if (/^(allow|allowed|accept|accepted|permit|permitted|forward|forwarded|resolved|cache|cached)$/.test(value)) return ['已允许', 'good'];
  if (/^(deny|denied|block|blocked|drop|dropped|reject|rejected|refused|nxdomain)$/.test(value)) return ['已拒绝', 'error'];
  const other = { nodata: '无解析记录', upstream: '上游查询', error: '解析异常' };
  return [other[value] || asText(action, '未知'), 'neutral'];
}

function setField(name, text, datetime) {
  for (const element of $$(`[data-field="${name}"]`)) {
    element.textContent = text;
    if (element.tagName === 'TIME') {
      if (datetime) element.setAttribute('datetime', String(datetime));
      else element.removeAttribute('datetime');
    }
  }
}

function setBadge(name, count) {
  const value = Number(count);
  for (const node of $$(`[data-badge="${name}"]`)) {
    if (Number.isFinite(value) && value > 0) {
      node.textContent = String(value);
      node.hidden = false;
    } else {
      node.textContent = '';
      node.hidden = true;
    }
  }
}

function renderService(id, value) {
  const statuses = {
    running: ['运行中', 'good'], active: ['已启用', 'good'], ok: ['正常', 'good'],
    stopped: ['已停止', 'error'], inactive: ['未启用', 'warning'], failed: ['故障', 'error'],
    error: ['异常', 'error'], unknown: ['状态未知', 'neutral'], unavailable: ['不可用', 'error']
  };
  const [label, tone] = statuses[String(value).toLowerCase()] || [asText(value, '状态未知'), 'neutral'];
  $(id).textContent = label;
  $(id).dataset.tone = tone;
}

function renderQueryLogs(logs) {
  const entries = Array.isArray(logs) ? logs.filter((entry) => entry && typeof entry === 'object') : [];
  let allowed = 0;
  let blocked = 0;
  for (const log of entries) {
    const [, tone] = actionInfo(log.action);
    if (tone === 'good') allowed += 1;
    if (tone === 'error') blocked += 1;
  }
  $('query-total').textContent = String(entries.length);
  $('query-allowed').textContent = String(allowed);
  $('query-blocked').textContent = String(blocked);
  $('query-other').textContent = String(entries.length - allowed - blocked);

  const fragment = document.createDocumentFragment();
  const preview = entries.slice(0, 5);
  if (!preview.length) {
    const empty = document.createElement('li');
    empty.className = 'log-item muted';
    empty.textContent = '暂无 DNS 请求。PS5 使用本机 DNS 后，请求会显示在这里。';
    fragment.append(empty);
  } else {
    for (const log of preview) {
      const [label, tone] = actionInfo(log.action);
      const item = document.createElement('li');
      item.className = 'log-item';
      const head = document.createElement('div');
      head.className = 'log-head';
      const name = document.createElement('span');
      name.className = 'log-name mono';
      name.textContent = asText(log.name);
      const badge = document.createElement('span');
      badge.className = 'result-badge';
      badge.dataset.tone = tone;
      badge.textContent = label;
      head.append(name, badge);
      item.append(head);

      const meta = document.createElement('p');
      meta.className = 'log-meta';
      meta.textContent = [formatTime(log.time), asText(log.client), asText(log.qtype)]
        .filter((value) => value && value !== '—' && value !== '尚无记录')
        .join(' · ');
      item.append(meta);

      const detail = asText(log.detail, '');
      if (detail && detail !== '—') {
        const detailNode = document.createElement('p');
        detailNode.className = 'log-detail';
        detailNode.textContent = detail;
        item.append(detailNode);
      }
      fragment.append(item);
    }
  }
  $('overview-logs').replaceChildren(fragment);
}

function renderFirewall(firewall) {
  const counters = [];
  const totals = { packets: 0, bytes: 0 };
  let hasTotals = false;
  let partial = false;
  const labels = { packets: '数据包', bytes: '字节', allowed: '允许', blocked: '拒绝', dropped: '丢弃', accepted: '接受', dns: 'DNS', forward: '转发', input: '入站', output: '出站', counter: '计数器', counters: '计数器', nftables: '防火墙', rule: '规则', expr: '匹配项' };
  const walk = (node, path = [], depth = 0) => {
    if (node === null || node === undefined) return;
    if (counters.length >= 64 || depth > 16) { partial = true; return; }
    if (typeof node === 'number' && Number.isFinite(node)) {
      counters.push([path.join(' / ') || '计数', node.toLocaleString('zh-CN')]);
      if (path.at(-1) === '数据包') { totals.packets += node; hasTotals = true; }
      if (path.at(-1) === '字节') { totals.bytes += node; hasTotals = true; }
    } else if (Array.isArray(node)) {
      node.forEach((item, index) => walk(item, [...path, asText(item && (item.name || item.comment), String(index + 1))], depth + 1));
    } else if (typeof node === 'object') {
      const name = node.name || node.comment;
      for (const [key, value] of Object.entries(node)) {
        if (['handle', 'index', 'family', 'name', 'comment', 'version', 'schema'].includes(key)) continue;
        walk(value, [...path, ...(name && !path.includes(String(name)) ? [String(name)] : []), labels[key] || key], depth + 1);
      }
    }
  };
  walk(firewall);
  if (!counters.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '暂无网关计数数据。';
    $('firewall-counters').replaceChildren(empty);
    return;
  }
  if (hasTotals) counters.unshift(['已展示规则累计数据包', totals.packets.toLocaleString('zh-CN')], ['已展示规则累计字节', totals.bytes.toLocaleString('zh-CN')]);
  const list = document.createElement('dl');
  for (const [label, value] of counters) {
    const row = document.createElement('div');
    const term = document.createElement('dt');
    const count = document.createElement('dd');
    term.textContent = label;
    count.textContent = value;
    count.className = 'mono';
    row.append(term, count);
    list.append(row);
  }
  const explanation = document.createElement('p');
  explanation.className = 'secondary-note';
  explanation.textContent = `${partial ? '条目较多，仅展示部分计数。' : ''}计数按规则累加，同一数据包可能被多次计数。`;
  $('firewall-counters').replaceChildren(list, explanation);
}

function renderState(data, forceConfig = false) {
  if (!data.config || typeof data.config !== 'object' || typeof data.csrf !== 'string' || !data.csrf) throw new ApiError('状态数据不完整，请刷新后重试。');
  csrf = data.csrf;
  const config = cleanConfig(data.config);
  const nextSignature = signature(config);
  // 未保存的编辑属于用户，轮询结果不能覆盖。
  const keepDraft = dirty && !forceConfig;
  showDashboard();
  if (!keepDraft) fillConfig(config);
  else if (remoteSignature && nextSignature !== remoteSignature) toast('服务器配置已更新。当前未保存的编辑已保留；保存将覆盖服务器上的配置。', 'info');
  remoteSignature = nextSignature;

  const status = data.status && typeof data.status === 'object' ? data.status : {};
  const diagnostics = [...new Set([status.last_error, status.gateway_error]
    .filter((message) => typeof message === 'string' && message.trim())
    .map((message) => message.trim()))];
  const diagnosticsNode = $('status-diagnostics');
  diagnosticsNode.textContent = diagnostics.length ? `服务诊断：${diagnostics.join('；')}` : '';
  diagnosticsNode.hidden = diagnostics.length === 0;

  renderService('dns-status', status.dns);
  renderService('gateway-status', status.gateway);
  renderService('host-status', status.host);
  hasPrevious = status.has_previous !== false;
  serviceInfo = data.service && typeof data.service === 'object' ? data.service : null;
  renderServiceInfo(status);
  $('uptime').textContent = uptime(status.uptime);
  const dynamic = Number(status.dynamic_addresses);
  $('dynamic-addresses').textContent = Number.isFinite(dynamic) && dynamic >= 0 ? `${Math.floor(dynamic)} 个` : '—';
  $$('[data-host-ip]').forEach((element) => { element.textContent = asText(status.host_ip, '—'); });
  renderBuiltinDomains(data.local_domains, status.host_ip);
  setField('last-applied', formatTime(status.last_applied, true), status.last_applied);
  $('ipv6-note').textContent = status.ipv6_forwarding === true
    ? '检测到主机已开启 IPv6 转发。当前策略仅控制 IPv4；请确认家庭网络仍关闭 IPv6 互联网。'
    : '家庭网络已关闭 IPv6 互联网；本页网关策略不提供 IPv6 访问控制。';

  renderQueryLogs(data.logs);
  renderFirewall(data.firewall);
  renderQuickLinks();
  setConnection('状态已同步', 'good');
  $('sync-time').textContent = `${formatTime(new Date())} · 每 8 秒刷新`;
  updateControls();
}

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

function renderServiceInfo(status) {
  const info = serviceInfo || {};
  const ports = info.ports && typeof info.ports === 'object' ? info.ports : {};
  const paths = info.paths && typeof info.paths === 'object' ? info.paths : {};
  const units = info.units && typeof info.units === 'object' ? info.units : {};
  const hostIp = asText(status.host_ip, location.hostname);
  const port = (key, fallback) => (Number.isInteger(ports[key]) ? ports[key] : fallback);
  const adminPort = port('admin', 8443);
  const repoPort = port('repo', 8080);
  $('dns-status-hint').textContent = `UDP / TCP ${port('dns', 53)}`;
  $('host-status-hint').textContent = `HTTPS ${port('https', 443)} · ${asText(units.host && units.host.name, 'ps5-jb-host')}`;
  $('info-admin').textContent = `https://${hostIp}:${adminPort}/`;
  $('info-ports').textContent = `DNS ${port('dns', 53)} · 用户手册 ${port('https', 443)}/${port('http', 80)} · 仓库 ${repoPort} · 管理 ${adminPort}`;
  $('info-subnet').textContent = asText(info.subnet, '—');
  // Older servers do not report the interface; keep the row hidden then.
  const iface = typeof status.interface === 'string' ? status.interface : '';
  $('info-interface').textContent = iface || '—';
  $('info-interface-row').hidden = !iface;
  $('info-repo-dir').textContent = paths.repo ? `${paths.repo}/（repo.json + payloads/）` : '—';
  $('info-data-dir').textContent = asText(paths.data, '—');
  $('info-config').textContent = asText(paths.config, '—');
  const names = ['host', 'server', 'repo'].map((role) => units[role] && units[role].name).filter(Boolean);
  $('info-units').textContent = names.length ? names.map((name) => `${name}.service`).join(' · ') : '—';
  $('info-repo-url').textContent = `http://${hostIp}:${repoPort}/repo.json`;
  if (names.length) $('info-stop-command').textContent = `systemctl disable --now ${names.join(' ')}`;
}

function renderQuickLinks() {
  const set = (id, text) => { const node = $(id); if (node && text) node.textContent = text; };
  if (activity.loaded) {
    const { blocked } = activity.counts;
    set('quick-activity', blocked ? `${blocked} 条未放行 · 共 ${activity.events.length} 条记录` : `共 ${activity.events.length} 条记录，暂无未放行`);
  }
  const domains = lines($('domains').value).length;
  const networks = lines($('networks').value).length;
  if (baseline !== null) set('quick-policy', `域名 ${domains} 项 · IPv4 ${networks} 项${dirty ? ' · 有未保存修改' : ''}`);
  if (ps5.loaded) {
    const online = ps5.hosts.filter((host) => host.online);
    if (ps5.discovery.scanning && !ps5.hosts.length) set('quick-ps5', '正在扫描局域网…');
    else if (!ps5.hosts.length) set('quick-ps5', '未发现主机，进入页面重新扫描');
    else {
      const upgradable = ps5.hosts.reduce((sum, host) => sum + ps5UpgradeCandidates(host).length, 0);
      set('quick-ps5', `${online.length} 台在线 · ${ps5.hosts.map((host) => host.ip).join('、')}${upgradable ? ` · ${upgradable} 个插件可更新` : ''}`);
    }
  }
  if (files.info && files.info.host) set('quick-files', `${files.info.reachable ? '已连接' : '无法连接'} ${files.info.host}`);
}

/* ── 轮询 ───────────────────────────────────────────────────────────── */

function stopPolling() {
  clearTimeout(pollTimer);
  pollTimer = null;
}

function schedulePoll() {
  stopPolling();
  if (authenticated) pollTimer = setTimeout(() => refreshState(), POLL_MS);
}

async function refreshState({ forceConfig = false, manual = false } = {}) {
  if (refreshing || busy || uploading || passwordBusy) {
    schedulePoll();
    return false;
  }
  stopPolling();
  refreshing = true;
  const requestEpoch = epoch;
  if (manual) actBusy('refresh', true);
  updateControls();
  try {
    const data = await api('/api/state');
    if (requestEpoch !== epoch) return false;
    renderState(data, forceConfig);
    if (manual) toast('服务状态已刷新。', 'success');
    if (requestEpoch === epoch) refreshRepo();
    if (requestEpoch === epoch) refreshActivity();
    // The PS5 page polls hosts every cycle; elsewhere a slower refresh keeps
    // the overview summary current without hammering the console.
    if (requestEpoch === epoch && (currentSection === 'ps5' || Date.now() - ps5FetchedAt > 30000)) refreshPs5();
    return true;
  } catch (error) {
    if (requestEpoch !== epoch) return false;
    if (error.status === 401) {
      showLogin(authenticated ? `${error.message}${dirty ? ' 未保存的编辑已暂存，重新登录后可继续。' : ''}` : '');
    } else if (!authenticated) {
      showLogin(error.message);
    } else {
      setConnection('同步失败 · 数据可能已过期', 'error');
      toast(error.message, 'error');
    }
    return false;
  } finally {
    refreshing = false;
    if (manual) actBusy('refresh', false);
    updateControls();
    schedulePoll();
  }
}

function handleRefresh() {
  if (!authenticated) return;
  if (currentSection === 'activity') refreshActivity({ manual: true });
  else if (currentSection === 'repository') refreshRepo({ manual: true });
  else refreshState({ manual: true });
}
