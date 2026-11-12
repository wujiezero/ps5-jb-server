'use strict';

/* ── 路由与外壳 ─────────────────────────────────────────────────────── */

function sectionFromHash() {
  const match = /^#\/([a-z0-9]+)$/.exec(location.hash);
  return match && SECTIONS.includes(match[1]) ? match[1] : null;
}

function openMobileMore() {
  const dialog = $('mobile-more-dialog');
  if (!authenticated || dialog.open || !window.matchMedia('(max-width: 767px)').matches) return;
  mobileMoreCloseReason = '';
  $('mobile-more-trigger').setAttribute('aria-expanded', 'true');
  dialog.showModal();
  const current = dialog.querySelector('[aria-current="page"]') || dialog.querySelector('.more-link');
  if (current) current.focus({ preventScroll: true });
}

function closeMobileMore(reason = '') {
  const dialog = $('mobile-more-dialog');
  if (!dialog.open) return;
  mobileMoreCloseReason = reason;
  $('mobile-more-trigger').setAttribute('aria-expanded', 'false');
  dialog.close();
}

function restoreMobileMoreFocus() {
  $('mobile-more-trigger').setAttribute('aria-expanded', 'false');
  if (authenticated) {
    const trigger = $('mobile-more-trigger');
    const target = mobileMoreCloseReason === 'navigation' ? $('app-title')
      : trigger.getClientRects().length ? trigger
        : document.querySelector('.nav-item[aria-current="page"]') || $('app-title');
    target.focus({ preventScroll: true });
  }
  mobileMoreCloseReason = '';
  flushRepoPending();
  flushActivityPending();
  flushPs5Pending();
}

function showSection(section, options = {}) {
  if (!authenticated) return;
  closeMobileMore('navigation');
  const changed = currentSection !== section;
  currentSection = section;
  if (changed) playEnter(views[section]);
  for (const key of SECTIONS) {
    const view = views[key];
    if (view) view.hidden = key !== section;
  }
  for (const link of $$('.nav-item[data-section], .tab-item[data-section], .more-link[data-section]')) {
    if (link.dataset.section === section) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  const moreTrigger = $('mobile-more-trigger');
  if (['library', 'policy', 'repository', 'settings'].includes(section)) moreTrigger.setAttribute('aria-current', 'page');
  else moreTrigger.removeAttribute('aria-current');
  const meta = SECTION_META[section];
  $('app-title').textContent = meta.title;
  $('app-subtitle').textContent = meta.subtitle;
  document.title = `${meta.title} · PS5 管家`;
  $('app-save').hidden = section !== 'policy';
  $('app-rollback').hidden = section !== 'policy';
  $('app-upload').hidden = section !== 'repository';
  $('app-refresh').hidden = !(section === 'overview' || section === 'activity' || section === 'repository');
  window.scrollTo({ top: 0, behavior: 'auto' });
  // 路由变化时把焦点移到页面标题：既不画整列焦点框，也让读屏软件读到新页面。
  if (options.moveFocus && !dialogsOpen()) {
    const title = $('app-title');
    if (title) title.focus({ preventScroll: true });
  }
  if (section === 'activity' && !activity.loaded) refreshActivity();
  if (section === 'repository' && !repo.loaded) refreshRepo();
  if (section === 'library' && !library.loaded) loadLibrary();
  if (section === 'settings') {
    if (!selfcheck.loaded) loadSelfcheck();
    if (!notifyState.loaded) loadNotify();
    if (!updates.current) loadVersion();
    refreshUpdates();
    startLogs();
  } else stopLogs();
  if (section === 'ps5') {
    if (!repo.loaded) refreshRepo();
    if (!ps5.loaded) refreshPs5();
    flushPs5Pending();
    renderPs5();
  }
  if (section === 'files') {
    ensureFilesRoute();
    renderFiles();
    ensureFilesPolling();
    if (!nas.loaded) refreshNas();
    refreshNasJobs();
  }
}

function ensureRoute(options = {}) {
  if (!authenticated) return;
  const found = sectionFromHash();
  const section = found || 'overview';
  if (!found) {
    try { history.replaceState(null, '', '#/' + section); } catch { /* file:// */ }
  }
  showSection(section, options);
}

function setConnection(text, tone) {
  const chip = $('connection-state');
  chip.textContent = text;
  chip.dataset.tone = tone;
  const dot = $('connection-dot');
  dot.dataset.tone = tone;
  dot.setAttribute('aria-label', text);
  dot.title = text;
}

function showLogin(message = '', clearDraft = false) {
  epoch += 1;
  authenticated = false;
  csrf = '';
  stopPolling();
  stopUpdatePolling();
  stopLogs();
  updates.generation += 1;
  Object.assign(updates, { data: null, loading: false, action: '', error: '', reconnect: false,
    awaiting: false, deadline: 0, timedOut: false });
  renderUpdates();
  $$('dialog[open]').forEach((dialog) => dialog.close());
  $('boot-loading').hidden = true;
  $('app-shell').hidden = true;
  $('login-view').hidden = false;
  $('password').value = '';
  $('login-error').textContent = '';
  $('login-error').hidden = true;
  const messageNode = $('login-message');
  messageNode.textContent = message;
  messageNode.hidden = !message;
  resetPasswordPanel();
  resetRepoPanel();
  resetActivityPanel();
  resetPs5Panel();
  resetFilesPanel();
  if (location.hash) {
    try { history.replaceState(null, '', location.pathname); } catch { /* ignore */ }
  }
  document.title = '登录 · PS5 管家';
  if (clearDraft) {
    baseline = null;
    dirty = false;
    remoteSignature = '';
    fields.forEach((field) => { field.value = ''; });
    $('overview-logs').replaceChildren();
    $('firewall-counters').replaceChildren();
  }
  updateControls();
  $('password').focus({ preventScroll: true });
}

function showDashboard() {
  const entering = !authenticated;
  authenticated = true;
  $('boot-loading').hidden = true;
  $('login-view').hidden = true;
  $('app-shell').hidden = false;
  if (entering) {
    ensureRoute();
    refreshUpdates();
    refreshNasJobs();
  }
}

/* ── 全局控件状态 ───────────────────────────────────────────────────── */

function updateControls() {
  dirty = baseline !== null && signature(readConfig()) !== baseline;
  $('domain-count').textContent = `${lines($('domains').value).length} 项`;
  $('network-count').textContent = `${lines($('networks').value).length} 项`;
  const chip = $('edit-state');
  chip.textContent = dirty ? '有未保存的修改' : '已同步';
  chip.dataset.dirty = String(dirty);

  setDisabled('save', busy || !authenticated || !dirty);
  setDisabled('rollback', busy || !authenticated || !hasPrevious);
  for (const button of acts('rollback')) {
    if (!hasPrevious) button.title = '还没有可回退的上一版配置';
    else button.removeAttribute('title');
  }
  setDisabled('logout', busy || uploading);
  setDisabled('refresh', busy || refreshing);
  $('config-fields').disabled = busy || !authenticated;
  $('password-fields').disabled = busy || passwordBusy || !authenticated;
  setDisabled('password', busy || passwordBusy || !authenticated);
  updateRepoControls();
  updateActivityControls();
  updatePs5Controls();
  updateFilesControls();
  renderUpdates();
}

function updateRepoControls() {
  const locked = busy || repoBusy || !authenticated;
  setDisabled('upload', locked);
  $('repo-upload-fields').disabled = locked || uploading;
  $('repo-upload-submit').disabled = locked || uploading;
  $('repo-upload-cancel').disabled = locked || uploading;
  for (const button of $$('#repo-body button.repo-action')) button.disabled = locked;
  $('repo-upstream-check').disabled = locked || uploading || !repo.loaded;
  $('repo-upstream-all').disabled = locked || uploading;
  for (const button of $$('#repo-versions-list button')) button.disabled = locked;
}

function clearErrors() {
  for (const [field, error] of [['domains', 'domains-error'], ['networks', 'networks-error'], ['upstream-dns', 'upstream-error']]) {
    $(field).removeAttribute('aria-invalid');
    $(error).textContent = '';
    $(error).hidden = true;
  }
}

function fillConfig(config) {
  $('domains').value = config.domains.join('\n');
  $('networks').value = config.networks.join('\n');
  $('upstream-dns').value = config.upstream_dns;
  baseline = signature(config);
  clearErrors();
  updateControls();
}

function validate(config) {
  clearErrors();
  let first = null;
  const invalid = (field, errorId, message) => {
    $(field).setAttribute('aria-invalid', 'true');
    $(errorId).textContent = message;
    $(errorId).hidden = false;
    if (!first) first = $(field);
  };
  const badDomain = config.domains.findIndex((entry) => {
    const domain = entry.startsWith('*.') ? entry.slice(2) : entry;
    return !domain || domain.length > 253 || /[\s/:?#@*]/.test(domain) || domain.split('.').some((part) => !part || part.length > 63 || part.startsWith('-') || part.endsWith('-'));
  });
  if (badDomain !== -1) invalid('domains', 'domains-error', `第 ${badDomain + 1} 项格式不正确。请填写域名或 *.域名，不要包含协议、路径或端口。`);
  const badNetwork = config.networks.findIndex((entry) => {
    const parts = entry.split('/');
    return parts.length > 2 || !ipv4(parts[0]) || (parts.length === 2 && (!/^\d{1,2}$/.test(parts[1]) || Number(parts[1]) > 32));
  });
  if (badNetwork !== -1) invalid('networks', 'networks-error', `第 ${badNetwork + 1} 项格式不正确。请输入 IPv4 地址或 /0 至 /32 的 CIDR 网段。`);
  if (!ipv4(config.upstream_dns)) invalid('upstream-dns', 'upstream-error', '请输入有效的 IPv4 DNS 地址。');
  if (first) first.focus();
  return !first;
}
