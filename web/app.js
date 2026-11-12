(() => {
  'use strict';

  /* ── 基础工具 ───────────────────────────────────────────────────────── */

  const $ = (id) => document.getElementById(id);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const POLL_MS = 8000;
  const UPDATE_POLL_MS = 2000;
  const UPDATE_TIMEOUT_MS = 5 * 60 * 1000;
  const UPDATE_ACTIVE_STATES = new Set(['queued', 'downloading', 'validating', 'backing_up', 'installing', 'restarting', 'verifying']);
  const UPDATE_STATE_LABELS = { queued: '等待更新', downloading: '正在下载更新', validating: '正在验证更新',
    backing_up: '正在备份', installing: '正在安装', restarting: '正在重启服务', verifying: '正在确认服务状态',
    done: '更新完成', rolled_back: '更新未完成，已回滚到原版本', failed: '更新失败' };
  const updates = { current: '', data: null, loading: false, action: '', error: '', reconnect: false,
    awaiting: false, deadline: 0, timedOut: false, timer: null, generation: 0 };
  const PASSWORD_MIN = 10;
  const PASSWORD_SUCCESS = '密码已更新，其它设备上的登录已失效';
  const UPLOAD_MAX = 64 * 1024 * 1024;
  const UPLOAD_EXTENSIONS = ['.elf', '.bin'];
  const REPO_PORT = 8080;
  const ACTIVITY_LIMIT = 200;
  const TOAST_MS = 4000;
  const TOAST_MAX = 4;
  const SECTIONS = ['overview', 'policy', 'activity', 'ps5', 'files', 'repository', 'settings'];
  const views = {
    overview: $('view-overview'),
    policy: $('view-policy'),
    activity: $('view-activity'),
    ps5: $('view-ps5'),
    files: $('view-files'),
    repository: $('view-repository'),
    settings: $('view-settings')
  };
  const SECTION_META = {
    overview: { title: '概览', subtitle: '服务状态、运行时间与最近请求' },
    policy: { title: '访问策略', subtitle: '域名、IPv4 / CIDR 白名单与上游 DNS' },
    activity: { title: '网络活动', subtitle: '网关连接、DNS 查询与本机服务事件' },
    ps5: { title: 'PS5 主机', subtitle: '发现局域网主机，安装并运行插件载荷' },
    files: { title: 'PS5 文件', subtitle: '浏览主机存储、上传与整理文件' },
    repository: { title: '插件仓库', subtitle: '维护 PS5 插件载荷（.elf / .bin）' },
    settings: { title: '设置', subtitle: '管理密码、服务信息与停用说明' }
  };
  const PS5_TABS = ['catalog', 'running', 'autoload'];

  let csrf = '';
  let authenticated = false;
  let baseline = null;
  let dirty = false;
  let busy = false;
  let refreshing = false;
  let passwordBusy = false;
  let repoBusy = false;
  let uploading = false;
  let repoLoading = false;
  let repoPending = null;
  let repoRendered = '';
  let editTarget = null;
  let deleteTarget = null;
  let pollTimer = null;
  let epoch = 0;
  let remoteSignature = '';
  let activityBusy = false;
  let activityLoading = false;
  let activityPending = false;
  let activityCategoryKey = null;
  let currentSection = 'overview';
  let activityFilterValue = 'all';
  let activityKindValue = 'all';
  let uploadReplace = null;
  const UPLOAD_METADATA_FIELDS = ['name', 'version', 'category', 'author', 'description'];
  const uploadAutofill = { values: new Map(), edited: new Set() };
  let serviceInfo = null;
  let hasPrevious = true;
  let ps5FetchedAt = 0;
  let mobileMoreCloseReason = '';
  let repoReadError = '';
  let activityReadError = '';

  // PS5 主机页的瞬时状态：写入互斥、轮询暂存与待确认操作。
  let ps5Busy = false;
  let ps5Loading = false;
  let ps5Pending = null;
  let ps5Action = null;
  let ps5Draft = null;
  let ps5StandbyIp = '';
  let ps5PairIp = '';
  let ps5SelectedIp = '';
  let ps5Tab = 'catalog';
  let ps5InlineError = null;
  let ps5ShutdownIp = '';
  let ps5KillTarget = null;
  let ps5AutoloadTarget = null;
  let ps5DialogReturn = null;
  let ps5CatalogKey = '';
  let ps5RunningKey = '';
  let ps5AutoloadKey = '';
  const ps5HostErrors = new Map();
  const ps5 = {
    loaded: false,
    discovery: { subnet: '', scanned: 0, lastScan: '', scanning: false, durationMs: 0, enabled: true },
    hosts: []
  };

  // PS5 文件页：不参与 8 秒轮询，只在进入页面、手动刷新和传输任务期间更新。
  let filesInfoRequested = '';
  let filesNameMode = '';
  let filesNameTarget = null;
  let filesOverwritePending = null;
  let filesPollTimer = null;
  let nasJobs = [];
  const nas = { loaded: false, connections: [], protocols: { webdav: true, smb: true, ftp: true }, activeId: '', path: '/', entries: [], selected: new Set(), loading: false, error: '', editing: null, busy: false, seq: 0 };
  let filesPollBusy = false;
  let filesDragDepth = 0;
  const files = {
    loaded: false,
    info: null,
    volumes: [],
    path: '',
    entries: [],
    loading: false,
    busy: false,
    error: '',
    filter: '',
    menuTarget: null,
    menuTrigger: null,
    destMode: '',
    destTarget: null,
    destPath: '',
    destLoading: false,
    deleteTarget: null,
    uploads: [],
    uploadSeq: 0,
    uploading: false,
    uploadXhr: null,
    transfers: [],
    transfersError: '',
    job: null,
    jobActive: false,
    refreshOnJobEnd: false,
    drawer: false,
    renderedKey: '',
    requestedHost: '',
    configBusy: false,
    mode: 'ps5'
  };

  const activitySelected = new Set();
  const activity = { loaded: false, events: [], counts: { allowed: 0, blocked: 0 }, tracked: 0, updated: '', lastError: '', categories: [] };
  const repo = { loaded: false, items: [], totalBytes: 0, host: '', port: REPO_PORT, catalogName: '', upstream: null };
  let versionsTarget = null;
  let upstreamProgress = '';
  const toastTimers = new Map();
  const clientLog = [];   // 本页最近的错误提示，随日志一并导出

  class ApiError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.status = status;
    }
  }

  const asText = (value, fallback = '—') => value === null || value === undefined || value === '' ? fallback : String(value);
  const str = (value) => typeof value === 'string' ? value.trim() : value === null || value === undefined ? '' : String(value).trim();
  const lines = (value) => value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const searchQuery = (id) => $(id).value.trim().toLowerCase();
  const matchesSearch = (query, values) => !query || values.some((value) => String(value ?? '').toLowerCase().includes(query));
  const payloadMatchesSearch = (item, query) => matchesSearch(query, [item.name, item.filename, item.version, item.author, item.category, item.description]);
  const readConfig = () => ({ domains: lines($('domains').value), networks: lines($('networks').value), upstream_dns: $('upstream-dns').value.trim() });
  const signature = (config) => JSON.stringify(config);
  const cleanConfig = (config = {}) => ({
    domains: Array.isArray(config.domains) ? config.domains.map(String) : [],
    networks: Array.isArray(config.networks) ? config.networks.map(String) : [],
    upstream_dns: asText(config.upstream_dns, '')
  });
  const fields = [$('domains'), $('networks'), $('upstream-dns')];

  const ipv4 = (value) => {
    const octets = value.split('.');
    return octets.length === 4 && octets.every((octet) => /^\d{1,3}$/.test(octet) && Number(octet) <= 255);
  };

  const formatTime = (value, withDate = false) => {
    if (!value) return '尚无记录';
    // The update API speaks Unix seconds; Date would read that as 1970.
    const numeric = typeof value === 'number' ? value
      : typeof value === 'string' && /^\d+(\.\d+)?$/.test(value.trim()) ? Number(value) : null;
    const parsed = new Date(numeric !== null && numeric < 1e12 ? numeric * 1000 : value);
    if (Number.isNaN(parsed.getTime())) return asText(value);
    return parsed.toLocaleString('zh-CN', withDate
      ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }
      : { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  };

  const uptime = (value) => {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0 || value === null || value === undefined) return '—';
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    return hours >= 24 ? `${Math.floor(hours / 24)} 天 ${hours % 24} 小时` : hours ? `${hours} 小时 ${minutes % 60} 分` : `${minutes} 分钟`;
  };

  const formatBytes = (value) => {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return '—';
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
    if (bytes < 1099511627776) return `${(bytes / 1073741824).toFixed(bytes < 10737418240 ? 2 : 1)} GB`;
    return `${(bytes / 1099511627776).toFixed(2)} TB`;
  };
  const formatLimit = (bytes) => (Number.isFinite(bytes) && bytes > 0 ? formatBytes(bytes).replace(/\.0 /, ' ') : '不限');

  /* ── 网络请求（契约保持不变） ───────────────────────────────────────── */

  async function api(path, body, timeoutMs = 15000) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const headers = { Accept: 'application/json' };
    const options = { credentials: 'same-origin', cache: 'no-store', headers, signal: controller.signal };
    if (body !== undefined) {
      options.method = 'POST';
      headers['Content-Type'] = 'application/json';
      if (path !== '/api/login') {
        if (!csrf) throw new ApiError('登录验证信息缺失，请重新登录。', 401);
        headers['X-CSRF-Token'] = csrf;
      }
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, options);
      let data;
      try { data = await response.json(); } catch { data = null; }
      if (!response.ok) {
        const fallback = response.status === 401 ? '登录已失效，请重新输入管理密码。' : response.status === 403 ? '验证失败，请刷新状态后重试。' : '操作失败，请稍后重试。';
        throw new ApiError(data && typeof data.error === 'string' ? data.error : fallback, response.status);
      }
      if (!data || typeof data !== 'object') throw new ApiError('服务返回了无效数据，请稍后重试。');
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(error.name === 'AbortError' ? '连接超时，请检查管理服务后重试。' : '无法连接管理服务，请检查网络后重试。');
    } finally {
      clearTimeout(timeout);
    }
  }

  async function apiRaw(path, blob) {
    if (!csrf) throw new ApiError('登录验证信息缺失，请重新登录。', 401);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 180000);
    let response;
    try {
      response = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json', 'Content-Type': 'application/octet-stream', 'X-CSRF-Token': csrf },
        body: blob,
        signal: controller.signal
      });
    } catch (error) {
      throw new ApiError(error.name === 'AbortError' ? '上传超时，请检查管理服务后重试。' : '无法连接管理服务，请检查网络后重试。');
    } finally {
      clearTimeout(timeout);
    }
    let data;
    try { data = await response.json(); } catch { data = null; }
    if (!response.ok) {
      const fallbacks = { 400: '上传失败，请检查文件名与文件内容。', 401: '登录已失效，请重新输入管理密码。', 409: '仓库中已存在同名文件，请改名或先删除原文件。', 413: `文件超过 ${formatLimit(repo.maxUpload || UPLOAD_MAX)} 限制。` };
      throw new ApiError(data && typeof data.error === 'string' ? data.error : fallbacks[response.status] || '上传失败，请稍后重试。', response.status);
    }
    if (!data || typeof data !== 'object') throw new ApiError('服务返回了无效数据，请稍后重试。');
    return data;
  }

  /* ── 轻提示 ─────────────────────────────────────────────────────────── */

  function dismissToast(node) {
    const timer = toastTimers.get(node);
    if (timer) clearTimeout(timer);
    toastTimers.delete(node);
    if (!node.isConnected || node.dataset.leaving === 'true') return;
    node.dataset.leaving = 'true';
    setTimeout(() => node.remove(), 190);
  }

  function toast(message, tone = 'info') {
    if (typeof message !== 'string' || !message) return;
    if (tone === 'error' || tone === 'warning') noteClient(tone, message);
    const region = $('toast-region');
    const node = document.createElement('div');
    node.className = 'toast';
    node.dataset.tone = tone;
    node.setAttribute('role', tone === 'error' ? 'alert' : 'status');

    const text = document.createElement('p');
    text.className = 'toast-text';
    text.textContent = message;

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', '关闭提示');
    close.textContent = '×';
    close.addEventListener('click', () => dismissToast(node));

    node.append(text, close);
    const existing = Array.from(region.children);
    if (existing.length >= TOAST_MAX) existing.slice(0, existing.length - TOAST_MAX + 1).forEach(dismissToast);
    region.append(node);
    toastTimers.set(node, setTimeout(() => dismissToast(node), TOAST_MS));
  }

  /* ── 按钮状态（单一入口，重复出现在外壳里的动作保持同步） ───────────── */

  const acts = (action) => $$(`[data-act="${action}"]`);

  function setDisabled(action, disabled) {
    for (const button of acts(action)) button.disabled = disabled;
  }

  function storedLabel(button) {
    if (!button.dataset.label) {
      const labelNode = button.querySelector('.button-label');
      button.dataset.label = labelNode ? labelNode.textContent : '';
    }
    return button.dataset.label;
  }

  function actBusy(action, loading, pendingLabel) {
    for (const button of acts(action)) {
      const buttonLabel = storedLabel(button);
      button.disabled = loading;
      button.setAttribute('aria-busy', String(loading));
      const spinner = button.querySelector('.spinner');
      if (spinner) spinner.hidden = !loading;
      const labelNode = button.querySelector('.button-label');
      if (labelNode) labelNode.textContent = loading && pendingLabel ? pendingLabel : buttonLabel;
    }
  }

  function buttonBusy(target, loading, label) {
    const button = typeof target === 'string' ? $(target) : target;
    if (!button) return;
    button.disabled = loading;
    button.setAttribute('aria-busy', String(loading));
    const spinner = button.querySelector('.spinner');
    if (spinner) spinner.hidden = !loading;
    const labelNode = button.querySelector('.button-label');
    if (labelNode && label) labelNode.textContent = label;
  }

  function dialogsOpen() {
    return Boolean(document.querySelector('dialog[open]'));
  }

  // 显式焦点陷阱：不依赖浏览器对 <dialog> 的默认行为，Tab / Shift+Tab 始终在面板内循环。
  function trapDialogFocus(event, dialog) {
    if (event.key !== 'Tab') return;
    const focusables = $$('a[href], button, input, textarea, select, [tabindex]:not([tabindex="-1"])', dialog)
      .filter((element) => !element.disabled && element.type !== 'hidden' && element.getClientRects().length > 0);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (!dialog.contains(document.activeElement)) {
      event.preventDefault();
      first.focus();
      return;
    }
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

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
    if (['policy', 'repository', 'settings'].includes(section)) moreTrigger.setAttribute('aria-current', 'page');
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
    if (section === 'settings') {
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

  function renderLogs(logs) {
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

    renderLogs(data.logs);
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

  /* ── 策略保存 / 回退 / 退出 ─────────────────────────────────────────── */

  async function mutate(path, body, action, pendingLabel, successMessage, afterSuccess) {
    if (busy || !authenticated) return;
    stopPolling();
    busy = true;
    epoch += 1; // 丢弃本次写入之前取回的状态响应
    const requestEpoch = epoch;
    actBusy(action, true, pendingLabel);
    updateControls();
    let succeeded = false;
    try {
      const result = await api(path, body);
      if (requestEpoch !== epoch) return;
      if (result.ok !== true) throw new ApiError('服务未确认操作成功，请刷新状态后检查。');
      succeeded = true;
      if (afterSuccess) afterSuccess();
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) showLogin(`${error.message}${dirty ? ' 未保存的编辑已暂存。' : ''}`);
      else toast(error.message, 'error');
    } finally {
      busy = false;
      actBusy(action, false);
      updateControls();
    }
    if (succeeded && authenticated) {
      if (successMessage) toast(successMessage, 'success');
      const appliedEpoch = epoch;
      try {
        refreshing = true;
        updateControls();
        const data = await api('/api/state');
        if (appliedEpoch === epoch) renderState(data, false);
      } catch (error) {
        if (appliedEpoch === epoch) {
          if (error.status === 401) showLogin(error.message);
          else if (successMessage) toast(`${successMessage} 但状态读取失败：${error.message}`, 'info');
        }
      } finally {
        refreshing = false;
        updateControls();
      }
    }
    schedulePoll();
  }

  function saveConfig() {
    if (busy || !authenticated || !dirty) return;
    const config = readConfig();
    if (!validate(config)) return;
    mutate('/api/config', config, 'save', '正在应用…', '配置已保存并应用。', () => {
      baseline = signature(config);
      remoteSignature = baseline;
      dirty = false;
      updateControls();
    });
  }

  function openRollbackDialog() {
    if (!authenticated || busy) return;
    $('rollback-dialog').showModal();
    $('rollback-cancel').focus({ preventScroll: true });
  }

  function confirmRollback() {
    $('rollback-dialog').close();
    if (busy || !authenticated) return;
    mutate('/api/rollback', {}, 'rollback', '正在回滚…', '已回滚到上一版配置。', () => {
      baseline = signature(readConfig());
      dirty = false;
    });
  }

  function logout() {
    if (busy || !authenticated) return;
    if (dirty && !window.confirm('有未保存的修改。退出登录将丢弃这些修改，是否继续？')) return;
    mutate('/api/logout', {}, 'logout', '正在退出…', '', () => showLogin('已退出登录。', true));
  }

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

  /* ── 插件仓库 ───────────────────────────────────────────────────────── */

  function repoBaseUrl() {
    const host = /^[A-Za-z0-9.\-:[\]]{1,255}$/.test(repo.host) ? repo.host : location.hostname;
    return `http://${host}:${repo.port}`;
  }

  function payloadUrl(filename) {
    return `${repoBaseUrl()}/payloads/${encodeURIComponent(filename)}`;
  }

  function normalizeRepo(data) {
    const catalog = data && typeof data.catalog === 'object' && data.catalog !== null ? data.catalog : {};
    const list = Array.isArray(catalog.items) ? catalog.items : Array.isArray(catalog.payloads) ? catalog.payloads : [];
    const items = [];
    for (const entry of list) {
      if (!entry || typeof entry !== 'object') continue;
      const filename = str(entry.filename);
      if (!filename) continue;
      const size = Number(entry.size);
      items.push({
        filename,
        name: str(entry.name) || filename,
        version: str(entry.version),
        category: str(entry.category),
        author: str(entry.author),
        description: str(entry.description),
        size: Number.isFinite(size) && size >= 0 ? size : null,
        missing: entry.missing === true,
        checksum: str(entry.checksum),
        history: Array.isArray(entry.history) ? entry.history.filter((record) => record && /^[0-9a-f]{64}$/.test(str(record.checksum))).map((record) => ({
          filename: str(record.filename), version: str(record.version), checksum: str(record.checksum),
          size: Number.isFinite(Number(record.size)) ? Number(record.size) : null,
          archivedAt: str(record.archived_at), lastUpdate: str(record.last_update)
        })) : []
      });
    }
    const reportedTotal = Number(data && data.total_bytes);
    const totalBytes = Number.isFinite(reportedTotal) && reportedTotal >= 0
      ? reportedTotal
      : items.reduce((sum, item) => sum + (item.size || 0), 0);
    const reportedPort = Number(data && data.port);
    const reportedLimit = Number(data && data.max_upload);
    return {
      loaded: true,
      maxUpload: Number.isFinite(reportedLimit) && reportedLimit > 0 ? reportedLimit : UPLOAD_MAX,
      items,
      totalBytes,
      host: str(data && data.host),
      port: Number.isInteger(reportedPort) && reportedPort > 0 && reportedPort <= 65535 ? reportedPort : REPO_PORT,
      catalogName: str(catalog.name),
      upstream: normalizeUpstream(data && data.upstream)
    };
  }

  function normalizeUpstream(data) {
    if (!data || typeof data !== 'object') return null;
    const items = {};
    if (data.items && typeof data.items === 'object') {
      for (const [filename, entry] of Object.entries(data.items)) {
        if (!entry || typeof entry !== 'object') continue;
        items[filename] = { state: str(entry.state), version: str(entry.version), filename: str(entry.filename), name: str(entry.name),
          owner: str(entry.owner), ownerFilename: str(entry.owner_filename) };
      }
    }
    return { checkedAt: str(data.checked_at), attemptedAt: str(data.attempted_at), error: str(data.error),
      available: Number(data.available) || 0, indexed: Number(data.indexed) || 0, items };
  }

  function upstreamOf(item) {
    return (repo.upstream && repo.upstream.items[item.filename]) || null;
  }

  function applyRepo(data) {
    const next = normalizeRepo(data);
    // 打开的对话框或进行中的上传拥有面板，最新列表先暂存。
    if (dialogsOpen() || uploading || repoBusy) {
      repoPending = next;
      return;
    }
    Object.assign(repo, next);
    renderRepo();
  }

  function flushRepoPending() {
    if (!repoPending || dialogsOpen() || uploading) return;
    Object.assign(repo, repoPending);
    repoPending = null;
    renderRepo();
  }

  function resetRepoPanel() {
    repoReadError = '';
    $('repo-search').value = '';
    Object.assign(repo, { loaded: false, items: [], totalBytes: 0, host: '', port: REPO_PORT, catalogName: '', upstream: null });
    versionsTarget = null;
    upstreamProgress = '';
    repoPending = null;
    repoRendered = '';
    editTarget = null;
    deleteTarget = null;
    $('repo-error').textContent = '';
    $('repo-error').hidden = true;
    resetUploadForm();
    renderRepo();
    updateRepoControls();
  }

  function repoRow(item) {
    const row = document.createElement('tr');

    const nameCell = document.createElement('td');
    const name = document.createElement('span');
    name.className = 'repo-name';
    name.textContent = item.name;
    nameCell.append(name);
    if (item.description) {
      const description = document.createElement('span');
      description.className = 'repo-desc';
      description.textContent = item.description;
      nameCell.append(description);
    }

    const filenameCell = document.createElement('td');
    filenameCell.className = 'mono';
    filenameCell.textContent = item.filename;
    if (item.missing) {
      const missing = document.createElement('span');
      missing.className = 'repo-missing';
      missing.textContent = '文件缺失';
      missing.title = '目录中记录了此插件，但仓库里没有对应文件；可用「替换」重新上传。';
      filenameCell.append(missing);
    }

    const versionCell = document.createElement('td');
    versionCell.textContent = asText(item.version);
    const upstream = upstreamOf(item);
    if (upstream && upstream.state === 'available') {
      const hint = document.createElement('span');
      hint.className = 'repo-update';
      hint.textContent = `可更新 → ${upstream.version || upstream.filename}`;
      hint.title = `上游文件 ${upstream.filename}`;
      versionCell.append(hint);
    } else if (upstream && upstream.state === 'local_newer') {
      const hint = document.createElement('span');
      hint.className = 'repo-update repo-update-muted';
      hint.textContent = `比上游 ${upstream.version} 新`;
      versionCell.append(hint);
    } else if (upstream && upstream.state === 'duplicate') {
      // 另一条目已发布上游最新版：这里是同一插件的重复条目，更新只会撞文件名。
      const hint = document.createElement('span');
      hint.className = 'repo-update repo-update-muted';
      hint.textContent = `重复 · 最新版在「${upstream.owner || upstream.ownerFilename}」`;
      hint.title = `上游 ${upstream.version || upstream.filename} 已由 ${upstream.ownerFilename} 提供，这一条可以删除`;
      versionCell.append(hint);
    }
    const categoryCell = document.createElement('td');
    categoryCell.textContent = asText(item.category);
    const sizeCell = document.createElement('td');
    sizeCell.className = 'mono repo-size';
    sizeCell.textContent = item.size === null ? '—' : formatBytes(item.size);

    const actionsCell = document.createElement('td');
    const actions = document.createElement('div');
    actions.className = 'repo-actions';

    const download = document.createElement('a');
    download.className = 'repo-action';
    download.href = payloadUrl(item.filename);
    download.target = '_blank';
    download.rel = 'noopener';
    download.textContent = '下载';
    download.setAttribute('aria-label', `下载 ${item.filename}（在新窗口打开）`);

    const edit = document.createElement('button');
    edit.className = 'repo-action';
    edit.type = 'button';
    edit.textContent = '编辑';
    edit.setAttribute('aria-label', `编辑 ${item.name} 的信息`);
    edit.addEventListener('click', () => openEditDialog(item));

    const replace = document.createElement('button');
    replace.className = 'repo-action';
    replace.type = 'button';
    replace.textContent = '替换';
    replace.setAttribute('aria-label', `上传新文件替换 ${item.filename}`);
    replace.addEventListener('click', () => openUploadDialog(item));

    const remove = document.createElement('button');
    remove.className = 'repo-action';
    remove.type = 'button';
    remove.dataset.tone = 'danger';
    remove.textContent = '删除';
    remove.setAttribute('aria-label', `删除 ${item.filename}`);
    remove.addEventListener('click', () => openDeleteDialog(item));

    if (upstream && upstream.state === 'available') {
      const update = document.createElement('button');
      update.className = 'repo-action';
      update.type = 'button';
      update.dataset.tone = 'primary';
      update.textContent = '更新';
      update.setAttribute('aria-label', `从上游下载 ${item.name} ${upstream.version}，当前版本保留可回退`);
      update.addEventListener('click', () => applyUpstream(item));
      actions.append(update);
    }
    const history = Array.isArray(item.history) ? item.history : [];
    if (history.length) {
      const versions = document.createElement('button');
      versions.className = 'repo-action';
      versions.type = 'button';
      versions.textContent = `历史 ${history.length}`;
      versions.setAttribute('aria-label', `查看 ${item.name} 的 ${history.length} 个历史版本并回退`);
      versions.addEventListener('click', () => openVersionsDialog(item));
      actions.append(versions);
    }
    actions.append(download, edit, replace, remove);
    actionsCell.append(actions);
    row.append(nameCell, filenameCell, versionCell, categoryCell, sizeCell, actionsCell);
    return row;
  }

  function skeletonRows(count, columns) {
    const fragment = document.createDocumentFragment();
    for (let index = 0; index < count; index += 1) {
      const row = document.createElement('tr');
      row.className = 'skeleton-row';
      for (let column = 0; column < columns; column += 1) {
        const cell = document.createElement('td');
        const bar = document.createElement('span');
        bar.className = column === 0 ? 'skeleton short' : column % 3 === 0 ? 'skeleton tiny' : 'skeleton';
        cell.append(bar);
        row.append(cell);
      }
      fragment.append(row);
    }
    return fragment;
  }

  function emptyRow(colSpan, message, ctaLabel, onCta) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = colSpan;
    cell.className = 'empty-state';
    const text = document.createElement('p');
    text.textContent = message;
    cell.append(text);
    if (ctaLabel && onCta) {
      const wrap = document.createElement('p');
      wrap.className = 'empty-cta';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'button button-secondary button-small';
      button.textContent = ctaLabel;
      button.addEventListener('click', onCta);
      wrap.append(button);
      cell.append(wrap);
    }
    row.append(cell);
    return row;
  }

  function renderRepo() {
    const query = searchQuery('repo-search');
    const visible = repo.items.filter((item) => payloadMatchesSearch(item, query));
    const current = JSON.stringify([repo.items, repo.totalBytes, repo.host, repo.port, repo.catalogName, repo.upstream, query]);
    renderUpstream();
    $('repo-search-summary').textContent = repo.loaded ? `显示 ${visible.length} / ${repo.items.length} 个插件`
      : repoReadError ? '插件仓库读取失败，请刷新重试。' : '正在读取插件仓库…';
    $('repo-summary').textContent = repo.loaded ? `共 ${repo.items.length} 个插件 · 合计 ${formatBytes(repo.totalBytes)}`
      : repoReadError ? '插件仓库读取失败。' : '正在读取插件仓库…';
    const endpoint = repo.loaded ? `${repoBaseUrl()}/repo.json` : '';
    $('repo-endpoint').textContent = endpoint;
    $('repo-endpoint-row').hidden = !endpoint;
    $('repo-status').textContent = repo.loaded ? `${repo.items.length} 个插件` : '—';
    $('repo-status-hint').textContent = repo.loaded ? `合计 ${formatBytes(repo.totalBytes)} · ${repoBaseUrl()}` : '等待读取仓库…';
    setBadge('repository', repo.loaded ? repo.items.length : 0);

    if (repo.loaded && current === repoRendered) {
      updateRepoControls();
      return;
    }
    repoRendered = repo.loaded ? current : '';
    const fragment = document.createDocumentFragment();
    if (!repo.loaded) {
      fragment.append(repoReadError ? emptyRow(6, '插件仓库读取失败，请刷新重试。') : skeletonRows(4, 6));
    } else if (!repo.items.length) {
      fragment.append(emptyRow(6, '仓库中暂无插件。', '上传第一个插件', () => openUploadDialog()));
    } else if (!visible.length) {
      fragment.append(emptyRow(6, '没有匹配的插件，请调整搜索条件。'));
    } else {
      for (const item of visible) fragment.append(repoRow(item));
    }
    $('repo-body').replaceChildren(fragment);
    updateRepoControls();
    ps5CatalogRefreshHook();
    renderQuickLinks();
  }

  async function refreshRepo({ manual = false } = {}) {
    if (!authenticated || repoLoading || repoBusy || uploading) return false;
    repoLoading = true;
    const requestEpoch = epoch;
    if (manual) actBusy('refresh', true);
    updateRepoControls();
    try {
      const data = await api('/api/repo');
      if (requestEpoch !== epoch) return false;
      repoReadError = '';
      $('repo-error').textContent = '';
      $('repo-error').hidden = true;
      applyRepo(data);
      if (manual) toast('插件仓库已刷新。', 'success');
      return true;
    } catch (error) {
      if (requestEpoch !== epoch) return false;
      if (error.status === 401) {
        showLogin(error.message);
        return false;
      }
      repoReadError = error.message;
      $('repo-error').textContent = `插件仓库读取失败：${error.message}`;
      $('repo-error').hidden = false;
      renderRepo();
      return false;
    } finally {
      repoLoading = false;
      if (manual) actBusy('refresh', false);
      updateRepoControls();
    }
  }

  function clearEditErrors() {
    for (const [field, error] of [['repo-edit-name', 'repo-edit-name-error'], ['repo-edit-version', 'repo-edit-version-error'], ['repo-edit-category', 'repo-edit-category-error'], ['repo-edit-author', 'repo-edit-author-error'], ['repo-edit-description', 'repo-edit-description-error']]) {
      $(field).removeAttribute('aria-invalid');
      $(error).textContent = '';
      $(error).hidden = true;
    }
    $('repo-edit-error').textContent = '';
    $('repo-edit-error').hidden = true;
  }

  function openEditDialog(item) {
    if (!authenticated || repoBusy || uploading) return;
    editTarget = item;
    clearEditErrors();
    $('repo-edit-filename').textContent = item.filename;
    $('repo-edit-name').value = item.name;
    $('repo-edit-version').value = item.version;
    $('repo-edit-category').value = item.category;
    $('repo-edit-author').value = item.author;
    $('repo-edit-description').value = item.description;
    $('repo-edit-fields').disabled = false;
    $('repo-edit-dialog').showModal();
    $('repo-edit-name').focus({ preventScroll: true });
  }

  async function submitEdit(event) {
    event.preventDefault();
    if (repoBusy || !authenticated || !editTarget) return;
    clearEditErrors();
    const name = $('repo-edit-name').value.trim();
    const body = {
      filename: editTarget.filename,
      name,
      version: $('repo-edit-version').value.trim(),
      category: $('repo-edit-category').value.trim(),
      author: $('repo-edit-author').value.trim(),
      description: $('repo-edit-description').value.trim()
    };
    if (!name) {
      $('repo-edit-name').setAttribute('aria-invalid', 'true');
      $('repo-edit-name-error').textContent = '请填写插件名称。';
      $('repo-edit-name-error').hidden = false;
      $('repo-edit-name').focus();
      return;
    }
    stopPolling();
    repoBusy = true;
    updateRepoControls();
    $('repo-edit-fields').disabled = true;
    $('repo-edit-cancel').disabled = true;
    buttonBusy('repo-edit-save', true, '保存中…');
    const requestEpoch = epoch;
    let saved = false;
    try {
      const result = await api('/api/repo/update', body);
      if (requestEpoch !== epoch) return;
      if (result.ok !== true) throw new ApiError('服务未确认修改成功，请刷新后检查。');
      saved = true;
      $('repo-edit-dialog').close();
      toast(`已更新「${name}」的信息。`, 'success');
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) {
        $('repo-edit-dialog').close();
        showLogin(error.message);
        return;
      }
      $('repo-edit-error').textContent = error.message;
      $('repo-edit-error').hidden = false;
    } finally {
      repoBusy = false;
      buttonBusy('repo-edit-save', false, '保存修改');
      $('repo-edit-fields').disabled = false;
      $('repo-edit-cancel').disabled = false;
      updateRepoControls();
      schedulePoll();
    }
    // repoBusy 期间 refreshRepo 会拒绝执行，因此回读只会在写入结束后开始。
    if (saved) refreshRepo();
  }

  function openDeleteDialog(item) {
    if (!authenticated || repoBusy || uploading) return;
    deleteTarget = item;
    $('repo-delete-filename').textContent = item.filename;
    $('repo-delete-error').textContent = '';
    $('repo-delete-error').hidden = true;
    $('repo-delete-cancel').disabled = false;
    $('repo-delete-confirm').disabled = false;
    $('repo-delete-dialog').showModal();
    $('repo-delete-cancel').focus({ preventScroll: true });
  }

  async function confirmDelete() {
    if (repoBusy || !authenticated || !deleteTarget) return;
    const filename = deleteTarget.filename;
    stopPolling();
    repoBusy = true;
    updateRepoControls();
    $('repo-delete-error').textContent = '';
    $('repo-delete-error').hidden = true;
    $('repo-delete-cancel').disabled = true;
    buttonBusy('repo-delete-confirm', true, '删除中…');
    const requestEpoch = epoch;
    let removed = false;
    try {
      const result = await api('/api/repo/delete', { filename });
      if (requestEpoch !== epoch) return;
      if (result.ok !== true) throw new ApiError('服务未确认删除成功，请刷新后检查。');
      removed = true;
      $('repo-delete-dialog').close();
      toast(`已删除 ${filename}。`, 'success');
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) {
        $('repo-delete-dialog').close();
        showLogin(error.message);
        return;
      }
      $('repo-delete-error').textContent = error.message;
      $('repo-delete-error').hidden = false;
    } finally {
      repoBusy = false;
      buttonBusy('repo-delete-confirm', false, '确认删除');
      $('repo-delete-cancel').disabled = false;
      updateRepoControls();
      schedulePoll();
    }
    if (removed) {
      refreshRepo();
      $('app-refresh').focus({ preventScroll: true });
    }
  }

  /* ── 插件上游更新与历史版本 ─────────────────────────────────────────── */

  function renderUpstream() {
    const status = repo.upstream;
    const line = $('repo-upstream-line');
    const all = $('repo-upstream-all');
    const available = repo.items.filter((item) => upstreamOf(item)?.state === 'available');
    if (upstreamProgress) {
      line.textContent = upstreamProgress;
    } else if (!status || !status.checkedAt) {
      line.textContent = status && status.error
        ? `检查失败：${status.error}`
        : '尚未检查。对照 Payload Manager 官方源查找新版，更新后旧版会保留，可随时回退。';
    } else {
      const when = formatTime(status.checkedAt, true);
      const parts = [available.length ? `${available.length} 个插件有新版本` : '仓库插件均为上游最新版本'];
      parts.push(`上次检查 ${when}`);
      if (status.error) parts.push(`最近一次检查失败：${status.error}`);
      line.textContent = parts.join(' · ');
    }
    line.dataset.tone = available.length ? 'update' : status && status.error ? 'error' : '';
    renderUpstreamProgress();
    renderRepoPs5Hint();
    all.hidden = available.length < 2;
    const label = all.querySelector('.button-label');
    if (label && all.getAttribute('aria-busy') !== 'true') label.textContent = `全部更新（${available.length}）`;
  }

  async function checkUpstream() {
    if (!authenticated || repoBusy || uploading) return;
    repoBusy = true;
    updateRepoControls();
    buttonBusy('repo-upstream-check', true, '检查中…');
    const requestEpoch = epoch;
    try {
      const status = await api('/api/repo/upstream/check', {}, 45000);
      if (requestEpoch !== epoch) return;
      repo.upstream = normalizeUpstream(status);
      repoRendered = '';
      if (repo.upstream?.error) toast(`检查上游更新失败：${repo.upstream.error}`, 'error');
      else toast(repo.upstream?.available ? `发现 ${repo.upstream.available} 个可更新的插件。` : '仓库插件均为上游最新版本。', 'success');
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) { showLogin(error.message); return; }
      toast(`检查上游更新失败：${error.message}`, 'error');
    } finally {
      repoBusy = false;
      buttonBusy('repo-upstream-check', false, '检查更新');
      renderRepo();
    }
  }

  async function runUpstream(items) {
    if (!authenticated || repoBusy || uploading || !items.length) return;
    stopPolling();
    repoBusy = true;
    updateRepoControls();
    if (items.length > 1) buttonBusy('repo-upstream-all', true, '更新中…');
    const requestEpoch = epoch;
    const done = [];
    let failure = null;
    try {
      for (const [index, item] of items.entries()) {
        const target = upstreamOf(item);
        upstreamProgress = `正在更新 ${index + 1} / ${items.length}：${item.name} ${target?.version || ''}…`;
        upstreamBatch = { index, total: items.length, name: item.name, version: target?.version || '', progress: null };
        renderUpstream();
        const stop = startProgressPoll('/api/repo/upstream/progress', (data) => {
          if (!upstreamBatch) return;
          upstreamBatch.progress = data && data.progress && typeof data.progress === 'object' ? data.progress : null;
          renderUpstreamProgress();
        });
        try {
          const result = await api('/api/repo/upstream/apply', { filename: item.filename }, 180000);
          if (requestEpoch !== epoch) return;
          done.push(`${item.name} ${result.version || ''}`.trim());
        } catch (error) {
          if (requestEpoch !== epoch) return;
          if (error.status === 401) { showLogin(error.message); return; }
          failure = `${item.name}：${error.message}`;
          break;
        } finally {
          stop();
        }
      }
    } finally {
      upstreamBatch = null;
      renderUpstreamProgress();
      upstreamProgress = '';
      repoBusy = false;
      buttonBusy('repo-upstream-all', false);
      updateRepoControls();
      schedulePoll();
    }
    if (done.length) toast(`已更新 ${done.join('、')}；旧版本已保留，可在「历史」里回退。PS5 上的旧版本会在「PS5 主机 → 插件库」提示一键更新。`, 'success');
    if (failure) toast(`更新失败 · ${failure}`, 'error');
    await refreshRepo();
    if (done.length) refreshPs5().then(() => renderRepoPs5Hint());
  }

  function applyUpstream(item) {
    return runUpstream([item]);
  }

  function applyAllUpstream() {
    return runUpstream(repo.items.filter((item) => upstreamOf(item)?.state === 'available'));
  }

  function openVersionsDialog(item) {
    if (!authenticated || repoBusy || uploading) return;
    versionsTarget = item;
    $('repo-versions-name').textContent = `${item.name}（当前 ${item.version || item.filename}）`;
    $('repo-versions-error').textContent = '';
    $('repo-versions-error').hidden = true;
    renderVersions();
    $('repo-versions-dialog').showModal();
    $('repo-versions-close').focus({ preventScroll: true });
  }

  function renderVersions() {
    const list = $('repo-versions-list');
    const fragment = document.createDocumentFragment();
    const records = versionsTarget ? versionsTarget.history : [];
    if (!records.length) {
      const empty = document.createElement('li');
      empty.className = 'version-empty';
      empty.textContent = '没有保留的旧版本。';
      fragment.append(empty);
    }
    for (const record of records) {
      const row = document.createElement('li');
      row.className = 'version-row';
      const text = document.createElement('div');
      text.className = 'version-text';
      const title = document.createElement('strong');
      title.textContent = record.version || '未标版本';
      const meta = document.createElement('span');
      meta.className = 'version-meta mono';
      meta.textContent = [record.filename, record.size === null ? '' : formatBytes(record.size),
        record.archivedAt ? `保留于 ${formatTime(record.archivedAt, true)}` : ''].filter(Boolean).join(' · ');
      text.append(title, meta);
      const actions = document.createElement('div');
      actions.className = 'version-actions';
      const restore = document.createElement('button');
      restore.type = 'button';
      restore.className = 'button button-secondary button-small';
      restore.textContent = '回退到此版本';
      restore.addEventListener('click', () => rollbackVersion(record));
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'repo-action';
      remove.dataset.tone = 'danger';
      remove.textContent = '删除';
      remove.setAttribute('aria-label', `永久删除保留的 ${record.filename}`);
      remove.addEventListener('click', () => {
        if (remove.dataset.confirm === 'true') { deleteVersion(record); return; }
        remove.dataset.confirm = 'true';
        remove.textContent = '确认删除';
      });
      actions.append(restore, remove);
      row.append(text, actions);
      fragment.append(row);
    }
    list.replaceChildren(fragment);
    updateRepoControls();
  }

  async function versionRequest(path, record, busyLabel) {
    if (!authenticated || repoBusy || !versionsTarget) return null;
    stopPolling();
    repoBusy = true;
    updateRepoControls();
    $('repo-versions-error').hidden = true;
    upstreamProgress = busyLabel;
    renderUpstream();
    const requestEpoch = epoch;
    try {
      const result = await api(path, { filename: versionsTarget.filename, checksum: record.checksum }, 60000);
      if (requestEpoch !== epoch) return null;
      if (result.ok !== true) throw new ApiError('服务未确认操作成功，请刷新后检查。');
      return result;
    } catch (error) {
      if (requestEpoch !== epoch) return null;
      if (error.status === 401) {
        $('repo-versions-dialog').close();
        showLogin(error.message);
        return null;
      }
      $('repo-versions-error').textContent = error.message;
      $('repo-versions-error').hidden = false;
      return null;
    } finally {
      upstreamProgress = '';
      repoBusy = false;
      updateRepoControls();
      renderUpstream();
      schedulePoll();
    }
  }

  async function rollbackVersion(record) {
    const result = await versionRequest('/api/repo/rollback', record, `正在回退到 ${record.version || record.filename}…`);
    if (!result) return;
    $('repo-versions-dialog').close();
    toast(`已回退到 ${result.version || result.filename}；原来的版本保留在「历史」里。`, 'success');
    await refreshRepo();
  }

  async function deleteVersion(record) {
    const result = await versionRequest('/api/repo/version/delete', record, '正在删除保留的旧版本…');
    if (!result || !versionsTarget) return;
    versionsTarget.history = versionsTarget.history.filter((old) => old.checksum !== record.checksum);
    renderVersions();
    toast(`已删除保留的 ${record.filename}。`, 'success');
    refreshRepo();
  }

  function clearUploadErrors() {
    for (const [field, error] of [['repo-upload-file', 'repo-upload-file-error'], ['repo-upload-name', 'repo-upload-name-error'], ['repo-upload-version', 'repo-upload-version-error'], ['repo-upload-category', 'repo-upload-category-error'], ['repo-upload-author', 'repo-upload-author-error'], ['repo-upload-description', 'repo-upload-description-error']]) {
      $(field).removeAttribute('aria-invalid');
      $(error).textContent = '';
      $(error).hidden = true;
    }
    $('repo-upload-error').textContent = '';
    $('repo-upload-error').hidden = true;
  }

  function inferUploadMetadata(filename) {
    const stem = String(filename).replace(/\.(?:elf|bin)$/i, '');
    const metadata = { name: stem, version: '', category: '', author: '', description: '' };
    // A dotted numeric version is evidence; a bare year or a software-name
    // prefix such as ps5upload is not. Platform tokens are removed only when
    // adjacent to a recognized version, never from an unversioned name.
    const versioned = stem.replace(/[-_ ]+(?:ps4|ps5)$/i, '');
    const match = /^(.+?)[-_ ]+(v?\d+(?:\.\d+)+(?:[a-z][a-z0-9]*)?(?:[-+][a-z0-9]+(?:[.-][a-z0-9]+)*)?)$/i.exec(versioned);
    if (match) {
      metadata.name = match[1].replace(/[-_ ]+(?:ps4|ps5)$/i, '');
      metadata.version = match[2];
    }
    const known = repo.items.find((item) => item.filename === filename);
    if (known) {
      for (const field of UPLOAD_METADATA_FIELDS) {
        if (str(known[field])) metadata[field] = str(known[field]);
      }
    }
    return metadata;
  }

  function markUploadFieldEdited(field) {
    uploadAutofill.edited.add(field);
  }

  function autofillUploadMetadata() {
    const file = $('repo-upload-file').files?.[0];
    // Replacement metadata intentionally uses blank-means-keep. Do not turn a
    // different selected source filename into an accidental metadata change.
    if (!file || uploadReplace || !/\.(?:elf|bin)$/i.test(file.name)) return;
    const metadata = inferUploadMetadata(file.name);
    for (const field of UPLOAD_METADATA_FIELDS) {
      const input = $(`repo-upload-${field}`);
      if (uploadAutofill.edited.has(field)) continue;
      const previous = uploadAutofill.values.get(field);
      if (input.value !== '' && input.value !== previous) {
        markUploadFieldEdited(field);
        continue;
      }
      input.value = metadata[field];
      uploadAutofill.values.set(field, input.value);
    }
  }

  function bindUploadAutofill() {
    $('repo-upload-file').addEventListener('change', autofillUploadMetadata);
    for (const field of UPLOAD_METADATA_FIELDS) {
      $(`repo-upload-${field}`).addEventListener('input', () => markUploadFieldEdited(field));
    }
  }

  function resetUploadForm() {
    uploadReplace = null;
    uploadAutofill.values.clear();
    uploadAutofill.edited.clear();
    $('repo-upload-form').reset();
    clearUploadErrors();
    $('repo-upload-status').textContent = '';
  }

  function openUploadDialog(replaceItem) {
    if (!authenticated || uploading || repoBusy) return;
    resetUploadForm();
    uploadReplace = replaceItem && typeof replaceItem === 'object' && replaceItem.filename ? replaceItem : null;
    const limit = formatLimit(repo.maxUpload || UPLOAD_MAX);
    if (uploadReplace) {
      $('repo-upload-title').textContent = `替换 ${uploadReplace.filename}`;
      $('repo-upload-subtitle').textContent = `上传新文件覆盖 ${uploadReplace.filename}（不超过 ${limit}），下载地址不变；留空的信息保持原值。`;
      $('repo-upload-name').placeholder = uploadReplace.name || '保持原名称';
      $('repo-upload-version').placeholder = uploadReplace.version || '保持原版本';
      $('repo-upload-category').placeholder = uploadReplace.category || '保持原分类';
      $('repo-upload-author').placeholder = uploadReplace.author || '保持原作者';
      $('repo-upload-description').placeholder = uploadReplace.description || '保持原描述';
    } else {
      $('repo-upload-title').textContent = '上传新插件';
      $('repo-upload-subtitle').textContent = `单个文件不超过 ${limit}。文件名取自所选文件，也是下载地址中的文件名。`;
      $('repo-upload-name').placeholder = '例如 etaHEN';
      $('repo-upload-version').placeholder = '例如 v1.8';
      $('repo-upload-category').placeholder = '选择或输入新分类';
      $('repo-upload-author').placeholder = '可选';
      $('repo-upload-description').placeholder = '可选，简要说明用途';
    }
    $('repo-upload-dialog').showModal();
    $('repo-upload-file').focus({ preventScroll: true });
  }

  async function copyRepoUrl() {
    const url = $('repo-endpoint').textContent;
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      toast('已复制仓库源地址。', 'success');
    } catch {
      const range = document.createRange();
      range.selectNodeContents($('repo-endpoint'));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      toast('浏览器不允许直接复制，已选中地址，请手动复制。', 'info');
    }
  }

  function closeUploadDialog() {
    if (uploading) return;
    $('repo-upload-dialog').close();
    resetUploadForm();
    $('app-upload').focus({ preventScroll: true });
  }

  async function submitUpload(event) {
    event.preventDefault();
    if (uploading || repoBusy || !authenticated) return;
    clearUploadErrors();
    const input = $('repo-upload-file');
    const file = input.files && input.files.length ? input.files[0] : null;
    const name = $('repo-upload-name').value.trim();
    let first = null;
    const invalid = (field, errorId, message) => {
      $(field).setAttribute('aria-invalid', 'true');
      $(errorId).textContent = message;
      $(errorId).hidden = false;
      if (!first) first = $(field);
    };
    const replacing = uploadReplace;
    const limit = repo.maxUpload || UPLOAD_MAX;
    if (!file) {
      invalid('repo-upload-file', 'repo-upload-file-error', '请选择要上传的 .elf 或 .bin 文件。');
    } else {
      const lower = file.name.toLowerCase();
      if (!UPLOAD_EXTENSIONS.some((extension) => lower.endsWith(extension))) invalid('repo-upload-file', 'repo-upload-file-error', '只支持 .elf 与 .bin 文件。');
      else if (!file.size) invalid('repo-upload-file', 'repo-upload-file-error', '所选文件为空，请重新选择。');
      else if (file.size > limit) invalid('repo-upload-file', 'repo-upload-file-error', `文件大小 ${formatBytes(file.size)} 超过 ${formatLimit(limit)} 限制。`);
      else if (!replacing && repo.items.some((item) => item.filename === file.name)) invalid('repo-upload-file', 'repo-upload-file-error', `仓库中已有 ${file.name}，如需更新请在列表中点「替换」。`);
    }
    if (!name && !replacing) invalid('repo-upload-name', 'repo-upload-name-error', '请填写插件名称。');
    if (first) {
      first.focus();
      return;
    }
    const query = new URLSearchParams({
      filename: replacing ? replacing.filename : file.name,
      name,
      version: $('repo-upload-version').value.trim(),
      category: $('repo-upload-category').value.trim(),
      author: $('repo-upload-author').value.trim(),
      description: $('repo-upload-description').value.trim()
    });
    if (replacing) query.set('replace', '1');
    stopPolling();
    uploading = true;
    repoBusy = true;
    updateRepoControls();
    buttonBusy('repo-upload-submit', true, '上传中…');
    $('repo-upload-status').textContent = `上传中…（${formatBytes(file.size)}）`;
    const requestEpoch = epoch;
    let uploaded = false;
    try {
      const result = await apiRaw(`/api/repo/upload?${query.toString()}`, file);
      if (requestEpoch !== epoch) return;
      if (result.ok !== true) throw new ApiError('服务未确认上传成功，请刷新后检查。');
      uploaded = true;
      $('repo-upload-dialog').close();
      resetUploadForm();
      toast(`${replacing ? '已替换' : '已上传'} ${asText(result.filename, file.name)}。`, 'success');
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) {
        $('repo-upload-dialog').close();
        showLogin(error.message);
        return;
      }
      $('repo-upload-error').textContent = error.message;
      $('repo-upload-error').hidden = false;
    } finally {
      uploading = false;
      repoBusy = false;
      buttonBusy('repo-upload-submit', false, '开始上传');
      $('repo-upload-status').textContent = '';
      updateRepoControls();
      schedulePoll();
    }
    if (uploaded) refreshRepo();
  }

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

  /* ── 通用 DOM 小工具（新页面共用） ─────────────────────────────────── */

  const reducedMotion = () => Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  // Phone layout (same breakpoint as the CSS tab bar / bottom sheets).
  const isPhone = () => Boolean(window.matchMedia && window.matchMedia('(max-width: 767px)').matches);

  /* ── 手机：底部操作面板（action sheet） ─────────────────────────────── */
  // Popover menus and long button rows become a bottom sheet on phones. The
  // chosen action runs after the sheet has closed so follow-up dialogs
  // (delete confirm, rename…) open on a clean stack.
  let actionSheetState = null;

  function openActionSheet({ title, sub = '', items, returnFocus = null, onDismiss = null }) {
    const dialog = $('action-sheet');
    if (!dialog || dialog.open) return;
    $('action-sheet-title').textContent = title;
    const subNode = $('action-sheet-sub');
    subNode.textContent = sub;
    subNode.hidden = !sub;
    const list = $('action-sheet-list');
    const fragment = document.createDocumentFragment();
    actionSheetState = { chosen: null, returnFocus, onDismiss };
    for (const item of items) {
      const button = makeElement('button', 'action-sheet-item');
      button.type = 'button';
      if (item.icon) button.append(makeIcon(item.icon));
      const text = makeElement('span', 'action-sheet-text');
      text.append(makeElement('span', 'action-sheet-label', item.label));
      if (item.hint) text.append(makeElement('span', 'action-sheet-hint', item.hint));
      button.append(text);
      if (item.danger) button.dataset.tone = 'danger';
      button.disabled = Boolean(item.disabled);
      button.addEventListener('click', () => {
        if (actionSheetState) actionSheetState.chosen = item;
        dialog.close();
      });
      fragment.append(button);
    }
    list.replaceChildren(fragment);
    dialog.showModal();
    const first = list.querySelector('button:not(:disabled)') || $('action-sheet-cancel');
    first.focus({ preventScroll: true });
  }

  function onActionSheetClosed() {
    const state = actionSheetState;
    actionSheetState = null;
    $('action-sheet-list').replaceChildren();
    if (!state) return;
    if (state.chosen) {
      state.chosen.onSelect();
      return;
    }
    if (state.onDismiss) state.onDismiss();
    if (state.returnFocus && state.returnFocus.isConnected) state.returnFocus.focus({ preventScroll: true });
  }

  /* ── 手机：下拉关闭底部面板 ─────────────────────────────────────────── */
  // Drag the sheet's top strip (handle / title) downwards to dismiss it, like
  // native sheets. The cancel event is honoured, so busy dialogs stay put.
  function bindSheetSwipe(dialog) {
    if (!dialog.querySelector('.sheet-handle')) return;
    let drag = null;
    const reset = () => {
      dialog.style.transform = '';
      dialog.style.transition = '';
    };
    dialog.addEventListener('pointerdown', (event) => {
      if (!isPhone() || event.button !== 0 || dialog.dataset.closing === 'true') return;
      if (event.target.closest('button, a, input, select, textarea, label')) return;
      const top = dialog.getBoundingClientRect().top;
      if (event.clientY - top > 64 || dialog.scrollTop > 0) return;
      drag = { id: event.pointerId, y: event.clientY, t: event.timeStamp, dy: 0 };
    });
    dialog.addEventListener('pointermove', (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      drag.dy = Math.max(0, event.clientY - drag.y);
      if (drag.dy > 4 && !dialog.hasPointerCapture(event.pointerId)) dialog.setPointerCapture(event.pointerId);
      dialog.style.transition = 'none';
      dialog.style.transform = `translateY(${drag.dy}px)`;
    });
    const end = (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      const { dy, t } = drag;
      drag = null;
      const fast = dy > 40 && dy / Math.max(1, event.timeStamp - t) > 0.6;
      if (dy > 110 || fast) {
        const cancel = new Event('cancel', { cancelable: true });
        if (dialog.dispatchEvent(cancel)) {
          dialog.style.transition = '';
          dialog.close();
          return;
        }
      }
      dialog.style.transition = 'transform 220ms var(--ease-drawer)';
      dialog.style.transform = '';
      setTimeout(reset, 240);
    };
    dialog.addEventListener('pointerup', end);
    dialog.addEventListener('pointercancel', end);
    dialog.addEventListener('close', reset);
  }

  // Mark a container so its CSS enter animation plays once; polling renders
  // later do not carry the flag and therefore never replay it.
  const enterTimers = new WeakMap();
  function playEnter(node, duration = 520) {
    if (!node) return;
    clearTimeout(enterTimers.get(node));
    node.removeAttribute('data-enter');
    void node.offsetWidth; // restart the animation when navigating quickly
    node.dataset.enter = 'true';
    enterTimers.set(node, setTimeout(() => node.removeAttribute('data-enter'), duration));
  }

  // <dialog>.close() is instant; play a short exit first. Code that checks
  // dialog.open right after close() still works: open flips once it is done.
  function animateDialogClose(dialog) {
    const close = dialog.close.bind(dialog);
    dialog.close = (value) => {
      if (!dialog.open || dialog.dataset.closing === 'true') return;
      if (reducedMotion() && !window.matchMedia('(max-width: 767px)').matches) {
        close(value);
        return;
      }
      dialog.dataset.closing = 'true';
      const done = () => {
        dialog.removeEventListener('animationend', done);
        clearTimeout(timer);
        delete dialog.dataset.closing;
        close(value);
      };
      const timer = setTimeout(done, 260);
      dialog.addEventListener('animationend', done);
    };
    dialog.addEventListener('close', () => { delete dialog.dataset.closing; });
  }

  const makeElement = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const makeIcon = (name) => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#${name}`);
    svg.append(use);
    return svg;
  };

  const tableEmptyRow = (colSpan, message) => {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = colSpan;
    cell.className = 'empty-state';
    cell.textContent = message;
    row.append(cell);
    return row;
  };

  const sameStringSet = (left, right) => {
    const a = new Set(Array.from(left || []));
    const b = new Set(Array.from(right || []));
    return a.size === b.size && [...a].every((value) => b.has(value));
  };

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

  /* ── PS5 文件 ───────────────────────────────────────────────────────── */

  const filesJoin = (dir, name) => `${String(dir).replace(/\/+$/, '')}/${name}`;

  const filesParent = (path) => {
    const value = String(path || '');
    const index = value.lastIndexOf('/');
    if (index <= 0) return '';
    return value.slice(0, index);
  };

  const filesVolumeOf = (path) => files.volumes.find((volume) => path === volume.path || String(path).startsWith(`${volume.path}/`)) || null;

  const filesVolumeLabel = (kind) => ({
    system: '系统存储',
    usb: 'USB 存储',
    extended: '扩展存储',
    ext: '扩展存储',
    m2: 'M.2 存储',
    internal: '内置存储'
  }[kind] || '存储');

  const filesVolumeIcon = (kind) => (kind === 'usb' ? 'i-usb' : kind === 'm2' || kind === 'internal' ? 'i-chip' : 'i-drive');

  const filesStateLabel = (state) => ({
    idle: '空闲',
    active: '进行中',
    pending: '等待',
    copying: '复制中',
    moving: '移动中',
    running: '进行中',
    done: '完成',
    failed: '失败',
    canceled: '已取消',
    cancel: '已取消',
    skip: '已跳过',
    wait: '等待'
  }[String(state || '').toLowerCase()] || asText(state, '未知'));

  const filesKindLabel = (kind) => ({
    upload: '上传',
    download: '下载',
    copy: '复制',
    move: '移动'
  }[String(kind || '').toLowerCase()] || '任务');

  const formatFileTime = (seconds) => {
    if (!seconds) return '—';
    const date = new Date(Number(seconds) * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
    });
  };

  function normalizeFilesInfo(data) {
    const info = data && typeof data === 'object' ? data : {};
    return {
      configured: info.configured === true,
      base: str(info.base),
      host: str(info.host),
      reachable: info.reachable === true,
      backend: str(info.backend),
      error: str(info.error),
      hosts: Array.isArray(info.hosts) ? [...new Set(info.hosts.map((host) => str(host)).filter(Boolean))] : []
    };
  }

  function normalizeFilesVolumes(data) {
    const volumes = [];
    for (const entry of Array.isArray(data && data.volumes) ? data.volumes : []) {
      if (!entry || typeof entry !== 'object') continue;
      const path = str(entry.path);
      if (!path) continue;
      const total = entry.total === null || entry.total === undefined ? NaN : Number(entry.total);
      const free = entry.free === null || entry.free === undefined ? NaN : Number(entry.free);
      volumes.push({
        path,
        label: str(entry.label) || filesVolumeLabel(str(entry.kind)),
        kind: str(entry.kind) || 'system',
        total: Number.isFinite(total) && total > 0 ? total : null,
        free: Number.isFinite(free) && free >= 0 ? free : null
      });
    }
    return volumes;
  }

  // macOS writes ._name (AppleDouble) metadata next to files it copies to SMB,
  // WebDAV, FTP and exFAT drives; they are noise in every list here.
  const isAppleDouble = (name) => name.startsWith('._');

  function normalizeFilesEntries(data, fallbackPath) {
    const path = str(data && data.path) || fallbackPath;
    const entries = [];
    for (const item of Array.isArray(data && data.entries) ? data.entries : []) {
      if (!item || typeof item !== 'object') continue;
      const name = str(item.name);
      if (!name || isAppleDouble(name)) continue;
      const size = Number(item.size);
      const mtime = Number(item.mtime);
      entries.push({
        name,
        path: str(item.path) || filesJoin(path, name),
        dir: item.dir === true,
        size: Number.isFinite(size) && size >= 0 ? size : 0,
        mtime: Number.isFinite(mtime) && mtime > 0 ? mtime : null
      });
    }
    return { path, entries };
  }

  function normalizeFilesJob(data) {
    const job = data && typeof data === 'object' ? data : {};
    const state = str(job.state) || 'idle';
    const done = Number(job.done);
    const total = Number(job.total);
    let percent = Number(job.percent);
    if (!Number.isFinite(percent) && Number.isFinite(done) && Number.isFinite(total) && total > 0) {
      percent = (done / total) * 100;
    }
    return {
      state,
      percent: Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : null,
      done: Number.isFinite(done) && done >= 0 ? done : null,
      total: Number.isFinite(total) && total >= 0 ? total : null,
      kind: str(job.kind),
      name: str(job.name),
      elapsed: Number.isFinite(Number(job.elapsed)) ? Number(job.elapsed) : null,
      speed: Number.isFinite(Number(job.speed)) ? Number(job.speed) : 0,
      error: str(job.error)
    };
  }

  function normalizeFilesTransfers(data) {
    const transfers = [];
    for (const entry of Array.isArray(data && data.transfers) ? data.transfers : []) {
      if (!entry || typeof entry !== 'object') continue;
      const size = Number(entry.size);
      const done = Number(entry.done);
      transfers.push({
        id: str(entry.id),
        kind: str(entry.kind) || 'upload',
        name: str(entry.name) || '未命名任务',
        size: Number.isFinite(size) && size >= 0 ? size : null,
        done: Number.isFinite(done) && done >= 0 ? done : null,
        state: str(entry.state) || 'active',
        speed: Number(entry.speed),
        ms: Number(entry.ms)
      });
    }
    return transfers;
  }

  function setFilesSetupError(message) {
    const node = $('files-setup-error');
    node.textContent = message || '';
    node.hidden = !message;
  }

  function setFilesError(message) {
    files.error = message || '';
    const node = $('files-error');
    node.textContent = files.error;
    node.hidden = !files.error;
  }

  function ensureFilesRoute() {
    if (files.loading) return;
    const wanted = files.requestedHost;
    const info = files.info;
    if (!files.loaded || !info || !info.configured || (wanted && info.host !== wanted)) {
      loadFiles({ configureHost: wanted });
    }
  }

  async function loadFiles({ configureHost = '', manual = false } = {}) {
    if (!authenticated || files.loading || files.busy) return false;
    if (configureHost) filesInfoRequested = configureHost;
    files.loading = true;
    files.error = '';
    updateFilesControls();
    renderFiles();
    const requestEpoch = epoch;
    try {
      let data = await api('/api/files/info');
      if (requestEpoch !== epoch) return false;
      let info = normalizeFilesInfo(data);
      const wanted = configureHost || files.requestedHost || filesInfoRequested;
      if (wanted && (!info.configured || info.host !== wanted)) {
        data = await api('/api/files/configure', { host: wanted });
        if (requestEpoch !== epoch) return false;
        info = normalizeFilesInfo(data);
        if (info.configured && info.host) files.requestedHost = info.host;
      }
      files.info = info;
      if (!info.configured) {
        files.loaded = false;
        return true;
      }
      files.loaded = true;
      if (!info.reachable) return true;
      await loadFilesVolumesAndList();
      if (requestEpoch !== epoch) return false;
      files.error = '';
      if (manual) toast('文件列表已刷新。', 'success');
      return true;
    } catch (error) {
      if (requestEpoch !== epoch) return false;
      if (error.status === 401) {
        showLogin(error.message);
        return false;
      }
      files.loaded = true;
      setFilesError(`文件服务读取失败：${error.message}`);
      return false;
    } finally {
      files.loading = false;
      updateFilesControls();
      renderFiles();
    }
  }

  async function loadFilesVolumesAndList() {
    const volumeData = await api('/api/files/volumes');
    files.volumes = normalizeFilesVolumes(volumeData);
    let path = files.path;
    if (!path || !filesVolumeOf(path)) path = files.volumes.length ? files.volumes[0].path : '';
    if (!path) {
      files.path = '';
      files.entries = [];
      return;
    }
    const listData = await api(`/api/files/list?path=${encodeURIComponent(path)}`);
    const normalized = normalizeFilesEntries(listData, path);
    files.path = normalized.path;
    files.entries = normalized.entries;
    files.renderedKey = '';
  }

  async function refreshFilesPage({ manual = true } = {}) {
    if (!authenticated || files.loading || files.busy) return;
    await loadFiles({ manual });
  }

  async function fetchFilesList(path) {
    const listData = await api(`/api/files/list?path=${encodeURIComponent(path)}`);
    const normalized = normalizeFilesEntries(listData, path);
    files.path = normalized.path;
    files.entries = normalized.entries;
    files.renderedKey = '';
  }

  async function navigateFiles(path) {
    if (!path || files.busy || files.loading || !authenticated) return;
    closeFilesMenu({ restoreFocus: false });
    files.busy = true;
    files.error = '';
    updateFilesControls();
    const requestEpoch = epoch;
    try {
      await fetchFilesList(path);
      if (requestEpoch !== epoch) return;
      files.filter = '';
      $('files-filter').value = '';
      playEnter($('files-body'));
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) {
        showLogin(error.message);
        return;
      }
      setFilesError(`文件夹读取失败：${error.message}`);
      toast(error.message, 'error');
    } finally {
      files.busy = false;
      updateFilesControls();
      renderFiles();
    }
  }

  function renderFiles() {
    if (!$('files-main')) return;
    const setup = $('files-setup');
    const unreachable = $('files-unreachable');
    const main = $('files-main');
    const loading = $('files-loading');
    const info = files.info;
    const showSetup = !files.loading && (!info || !info.configured);
    const showUnreachable = !files.loading && Boolean(info && info.configured && !info.reachable);
    const showLoading = files.loading && !info;
    setup.hidden = !showSetup;
    unreachable.hidden = !showUnreachable;
    main.hidden = showSetup || showUnreachable || showLoading;
    if (loading) loading.hidden = !showLoading;
    const chip = $('files-host-chip');
    if (showLoading) chip.textContent = '连接中…';
    else if (!info || !info.configured) chip.textContent = '未配置';
    else if (!info.reachable) chip.textContent = '未连接';
    else chip.textContent = info.host || '已连接';
    $('files-host-sub').textContent = info && info.configured
      ? (info.reachable
        ? `已连接 ${info.host || '主机'}${info.backend === 'ftp' ? ' · 通过 FTP（zftpd）' : info.backend === 'http' ? ' · 通过 tom-ps5-usbnet' : ''}${info.base ? ` · ${info.base}` : ''}`
        : `无法连接 ${info.host || '文件服务'}`)
      : '选择要浏览的 PS5 主机。';
    if (showSetup) renderFilesSetup();
    if (showUnreachable) {
      $('files-unreachable-text').textContent = info.error || `无法连接 ${info.host || '文件服务'}，请确认主机已开机，并运行 zftpd（FTP）或 tom-ps5-usbnet。`;
    }
    setFilesError(files.error);
    if (main.hidden) {
      updateFilesControls();
      renderFilesTransfers();
      return;
    }
    renderFilesVolumes();
    renderFilesCrumbs();
    renderFilesRows();
    renderFilesStatus();
    renderFilesTransfers();
    $('files-ps5-pane').hidden = files.mode !== 'ps5';
    $('files-nas-pane').hidden = files.mode !== 'nas';
    updateFilesControls();
  }

  function renderFilesSetup() {
    const select = $('files-setup-host');
    const hosts = files.info && Array.isArray(files.info.hosts) ? files.info.hosts : [];
    const previous = select.value;
    const current = (files.requestedHost && hosts.includes(files.requestedHost)) ? files.requestedHost : previous && hosts.includes(previous) ? previous : hosts[0] || '';
    const fragment = document.createDocumentFragment();
    if (!hosts.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = '未发现可浏览的主机';
      fragment.append(option);
    } else {
      for (const host of hosts) {
        const option = document.createElement('option');
        option.value = host;
        option.textContent = host;
        if (host === current) option.selected = true;
        fragment.append(option);
      }
    }
    select.replaceChildren(fragment);
    select.disabled = !authenticated || files.configBusy || !hosts.length;
    $('files-setup-connect').disabled = !authenticated || files.configBusy || !hosts.length;
  }

  function renderFilesVolumes() {
    const box = $('files-volumes');
    const active = filesVolumeOf(files.path);
    const fragment = document.createDocumentFragment();
    if (!files.volumes.length) {
      fragment.append(makeElement('p', 'files-rail-empty', '没有可用的存储位置。'));
    }
    for (const volume of files.volumes) {
      const used = volume.total && volume.free !== null
        ? Math.min(100, Math.max(0, ((volume.total - volume.free) / volume.total) * 100))
        : null;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'files-vol';
      button.dataset.path = volume.path;
      const isActive = files.mode === 'ps5' && Boolean(active && active.path === volume.path);
      if (isActive) button.dataset.active = 'true';
      button.setAttribute('aria-pressed', String(isActive));
      const capacity = volume.total === null || volume.free === null
        ? '容量未知（PS5 上未运行 web-file-mgr）'
        : `可用 ${formatBytes(volume.free)} · 共 ${formatBytes(volume.total)}`;
      button.setAttribute('aria-label', `${volume.label}（${volume.path}），${capacity}`);
      const top = makeElement('div', 'files-vol-top');
      top.append(makeIcon(filesVolumeIcon(volume.kind)), makeElement('span', 'files-vol-name', volume.label));
      const sub = makeElement('div', 'files-vol-sub');
      sub.append(makeElement('span', 'mono', volume.path), makeElement('span', '', filesVolumeLabel(volume.kind)));
      const bar = makeElement('div', 'usage');
      const fill = document.createElement('i');
      fill.style.width = `${used === null ? 0 : used.toFixed(1)}%`;
      if (used !== null && used >= 92) fill.className = 'full';
      bar.append(fill);
      bar.hidden = used === null;
      const foot = makeElement('p', 'files-vol-foot num', capacity);
      button.append(top, sub, bar, foot);
      button.addEventListener('click', () => { setFilesMode('ps5'); navigateFiles(volume.path); });
      fragment.append(button);
    }
    box.replaceChildren(fragment);
    renderNasRail();
  }

  function renderFilesCrumbs() {
    const box = $('files-crumbs');
    const volume = filesVolumeOf(files.path);
    if (!volume) {
      box.replaceChildren();
      return;
    }
    const fragment = document.createDocumentFragment();
    const addCrumb = (label, path, current) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'files-crumb';
      button.textContent = label;
      if (current) button.setAttribute('aria-current', 'page');
      button.setAttribute('aria-label', current ? `当前文件夹 ${label}` : `打开 ${label}`);
      button.addEventListener('click', () => {
        if (!current) navigateFiles(path);
      });
      return button;
    };
    const relative = files.path.slice(volume.path.length).split('/').filter(Boolean);
    // Phone-only "back" chevron (CSS hides it on wider screens).
    if (relative.length) {
      const parent = relative.length > 1 ? `${volume.path}/${relative.slice(0, -1).join('/')}` : volume.path;
      const back = makeElement('button', 'files-crumb-back');
      back.type = 'button';
      back.setAttribute('aria-label', '返回上一级文件夹');
      back.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M14.6 5.4 8 12l6.6 6.6"/></svg>';
      back.addEventListener('click', () => navigateFiles(parent));
      fragment.append(back);
    }
    fragment.append(addCrumb('概览', volume.path, files.path === volume.path));
    let at = volume.path;
    for (const part of relative) {
      at += `/${part}`;
      fragment.append(makeElement('span', 'files-crumb-sep', '/'));
      fragment.append(addCrumb(part, at, at === files.path));
    }
    box.replaceChildren(fragment);
    // On phones the trail is a single scrolling line; keep the current folder in view.
    box.scrollLeft = box.scrollWidth;
  }

  /* ── 游戏名称：CUSA12345 / PPSA01234 这类编号 → 游戏名 ─────────────── */
  // Names come from /api/titles (offline catalog + the console's param.json);
  // rows render first and their labels are filled in place when the batch returns.
  const TITLE_ID_PATTERN = /(?<![A-Za-z0-9])(CUSA|PPSA|PLAS|PCAS|PLJS|PCJS|PLJM|PCJM|PCKS|PLKS|ELAS|ECAS|ELJS|ECJS|ELJM|ECJM|ELKS|ECKS|NPXS)(\d{5})(?!\d)/i;
  const titleNames = { cache: new Map(), queued: new Set(), folders: new Set(), sentFolders: new Set(), timer: null };

  const titleIdOf = (name) => {
    const match = TITLE_ID_PATTERN.exec(str(name));
    return match ? (match[1] + match[2]).toUpperCase() : null;
  };

  const titleNameOf = (id) => {
    const entry = id ? titleNames.cache.get(id) : null;
    return entry ? entry.name : '';
  };

  function fillTitleLabel(node, entry) {
    const name = entry && entry.name ? entry.name : '';
    node.textContent = name;
    node.hidden = !name;
    const other = entry && entry.zh && entry.en && entry.en !== name ? entry.en : '';
    if (name) node.setAttribute('title', other ? `${name}\n${other}` : name);
    else node.removeAttribute('title');
  }

  // Returns a label element for names that carry a title ID, else null.
  // folder: PS5 folder path, lets the server read sce_sys/param.json once.
  function titleLabel(name, folder) {
    const id = titleIdOf(name);
    if (!id) return null;
    const node = makeElement('span', 'title-name');
    node.dataset.titleId = id;
    fillTitleLabel(node, titleNames.cache.get(id));
    if (!titleNames.cache.has(id)) titleNames.queued.add(id);
    if (folder && !titleNames.sentFolders.has(folder)) titleNames.folders.add(folder);
    if ((titleNames.queued.size || titleNames.folders.size) && !titleNames.timer) {
      titleNames.timer = setTimeout(flushTitleNames, 0);
    }
    return node;
  }

  async function flushTitleNames() {
    titleNames.timer = null;
    if (!authenticated) return;
    const ids = [...titleNames.queued].slice(0, 200);
    const folders = [...titleNames.folders].slice(0, 48);
    ids.forEach((id) => titleNames.queued.delete(id));
    folders.forEach((folder) => {
      titleNames.folders.delete(folder);
      titleNames.sentFolders.add(folder);
    });
    if (!ids.length && !folders.length) return;
    const query = [`ids=${encodeURIComponent(ids.join(','))}`, ...folders.map((folder) => `folder=${encodeURIComponent(folder)}`)];
    let data = null;
    try {
      data = await api(`/api/titles?${query.join('&')}`);
    } catch {
      // Not fatal: rows keep their file names; a later render asks again.
      folders.forEach((folder) => titleNames.sentFolders.delete(folder));
      return;
    }
    const found = data && typeof data.titles === 'object' && data.titles ? data.titles : {};
    const pending = new Set(Array.isArray(data.pending) ? data.pending : []);
    const asked = new Set([...ids, ...folders.map((folder) => titleIdOf(folder.split('/').pop())).filter(Boolean)]);
    for (const id of asked) {
      const entry = found[id];
      if (entry && typeof entry === 'object') {
        titleNames.cache.set(id, { name: str(entry.name), en: str(entry.en), zh: str(entry.zh), source: str(entry.source) });
      } else if (!pending.has(id)) {
        titleNames.cache.set(id, null);
      }
    }
    // The console did not get to these folders (time budget / offline): retry on the next render.
    folders.forEach((folder) => {
      if (pending.has(titleIdOf(folder.split('/').pop()))) titleNames.sentFolders.delete(folder);
    });
    for (const node of document.querySelectorAll('.title-name[data-title-id]')) {
      if (asked.has(node.dataset.titleId)) fillTitleLabel(node, titleNames.cache.get(node.dataset.titleId));
    }
    if (titleNames.queued.size || titleNames.folders.size) titleNames.timer = setTimeout(flushTitleNames, 0);
  }

  // Name cell content: the original file name stays as-is, the game name sits under it.
  function withTitleLabel(nameNode, name, folder) {
    const label = titleLabel(name, folder);
    if (!label) return nameNode;
    const stack = makeElement('span', 'files-name-stack');
    stack.append(nameNode, label);
    return stack;
  }

  function filesVisibleEntries() {
    const needle = files.filter.trim().toLowerCase();
    const matches = (entry) => entry.name.toLowerCase().includes(needle)
      || titleNameOf(titleIdOf(entry.name)).toLowerCase().includes(needle);
    const list = needle ? files.entries.filter(matches) : files.entries.slice();
    list.sort((left, right) => {
      if (left.dir !== right.dir) return left.dir ? -1 : 1;
      return left.name.localeCompare(right.name, 'zh', { numeric: true });
    });
    return list;
  }

  function filesRow(entry) {
    const row = document.createElement('tr');
    row.dataset.name = entry.name;
    row.dataset.path = entry.path;
    row.dataset.dir = String(entry.dir);
    const nameCell = document.createElement('td');
    const nameWrap = makeElement('div', 'files-name');
    const nameIcon = makeIcon(entry.dir ? 'i-folder' : 'i-file');
    if (entry.dir) nameIcon.classList.add('folder');
    nameWrap.append(nameIcon);
    if (entry.dir) {
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'files-name-button';
      open.textContent = entry.name;
      open.setAttribute('aria-label', `打开文件夹 ${entry.name}`);
      open.addEventListener('click', () => navigateFiles(entry.path));
      nameWrap.append(withTitleLabel(open, entry.name, entry.path));
    } else {
      nameWrap.append(withTitleLabel(makeElement('span', 'files-name-text', entry.name), entry.name));
    }
    nameCell.append(nameWrap);
    const sizeCell = makeElement('td', 'num files-size-col', entry.dir ? '—' : formatBytes(entry.size));
    const timeCell = makeElement('td', 'num files-time-col', formatFileTime(entry.mtime));
    const actionCell = document.createElement('td');
    actionCell.className = 'files-actions-col';
    const menuButton = document.createElement('button');
    menuButton.type = 'button';
    menuButton.className = 'files-row-menu';
    menuButton.dataset.filesMenuButton = entry.path;
    menuButton.setAttribute('aria-label', `更多操作：${entry.name}`);
    menuButton.setAttribute('aria-haspopup', 'menu');
    menuButton.setAttribute('aria-expanded', 'false');
    menuButton.append(makeIcon('i-menu'));
    actionCell.append(menuButton);
    row.append(nameCell, sizeCell, timeCell, actionCell);
    row.addEventListener('dblclick', (event) => {
      if (event.target.closest('button')) return;
      if (entry.dir) navigateFiles(entry.path);
    });
    // Phones: a tap anywhere on a file row opens its action sheet (folders
    // open via the full-row name button instead).
    row.addEventListener('click', (event) => {
      if (entry.dir || !isPhone() || menuButton.disabled || event.target.closest('button')) return;
      openFilesMenu(menuButton);
    });
    return row;
  }

  function renderFilesRows() {
    const body = $('files-body');
    if (!files.loaded) {
      body.replaceChildren(tableEmptyRow(4, '正在读取文件列表…'));
      return;
    }
    const entries = filesVisibleEntries();
    const key = JSON.stringify([files.path, files.filter, entries.map((entry) => [entry.name, entry.dir, entry.size, entry.mtime]), files.busy]);
    if (key === files.renderedKey) return;
    files.renderedKey = key;
    if (!files.entries.length) {
      body.replaceChildren(tableEmptyRow(4, '这个文件夹是空的。拖入文件即可上传。'));
      return;
    }
    if (!entries.length) {
      body.replaceChildren(tableEmptyRow(4, `没有匹配「${files.filter}」的项目。`));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const entry of entries) fragment.append(filesRow(entry));
    body.replaceChildren(fragment);
  }

  function renderFilesStatus() {
    const node = $('files-status');
    if (!files.loaded) {
      node.textContent = '正在读取文件列表…';
      return;
    }
    const entries = filesVisibleEntries();
    const folders = entries.filter((entry) => entry.dir).length;
    const totalSize = entries.reduce((sum, entry) => sum + (entry.dir ? 0 : entry.size), 0);
    const parts = [];
    if (files.filter) parts.push(`筛选 ${entries.length} / ${files.entries.length} 项`);
    else parts.push(`${folders} 个文件夹 · ${entries.length - folders} 个文件`);
    parts.push(`合计 ${formatBytes(totalSize)}`);
    parts.push('拖入文件即可上传');
    node.textContent = parts.join(' · ');
  }

  function updateFilesControls() {
    if (!$('files-upload')) return;
    const locked = !authenticated || files.loading || files.busy || files.configBusy;
    const mainReady = !$('files-main').hidden && files.loaded;
    const setButton = (id, disabled) => {
      const node = $(id);
      if (node) node.disabled = disabled;
    };
    setButton('files-upload', locked || !mainReady);
    setButton('files-mkdir', locked || !mainReady);
    setButton('files-refresh', !authenticated || files.loading || files.busy);
    setButton('files-transfers-toggle', !authenticated);
    setButton('files-setup-connect', locked || !(files.info && files.info.hosts && files.info.hosts.length));
    const setupHost = $('files-setup-host');
    if (setupHost) setupHost.disabled = locked || !(files.info && files.info.hosts && files.info.hosts.length);
    const filter = $('files-filter');
    if (filter) filter.disabled = locked || !mainReady;
    setButton('files-retry', files.loading || files.configBusy);
    setButton('files-switch-host', files.configBusy);
    setButton('files-name-cancel', files.busy);
    setButton('files-dest-cancel', files.busy);
    setButton('files-host-cancel', files.configBusy);
    for (const button of $$('#files-body [data-files-menu-button]')) button.disabled = locked || !mainReady;
    for (const item of $$('#files-menu [data-files-menu]')) item.disabled = locked || !files.menuTarget;
    for (const button of $$('[data-files-transfers]')) button.setAttribute('aria-expanded', String(files.drawer));
    updateNasControls();
  }

  function openFilesMenu(trigger) {
    const path = trigger.dataset.filesMenuButton;
    const entry = files.entries.find((item) => item.path === path);
    if (!entry) return;
    closeFilesMenu({ restoreFocus: false });
    files.menuTarget = entry;
    files.menuTrigger = trigger;
    const menu = $('files-menu');
    const labels = {
      download: entry.dir ? `下载 ${entry.name}（tar 归档）` : `下载 ${entry.name}`,
      rename: `重命名 ${entry.name}`,
      copy: `复制 ${entry.name} 到…`,
      move: `移动 ${entry.name} 到…`,
      delete: `删除 ${entry.name}`
    };
    for (const item of $$('#files-menu [data-files-menu]')) {
      const action = item.dataset.filesMenu;
      item.disabled = false;
      item.setAttribute('aria-label', labels[action] || action);
    }
    if (isPhone()) {
      // Same actions as the popover, presented as a bottom sheet.
      const items = $$('#files-menu [data-files-menu]').map((item) => {
        const use = item.querySelector('use');
        return {
          label: item.textContent.trim(),
          icon: use ? str(use.getAttribute('href')).replace('#', '') : '',
          danger: item.dataset.tone === 'danger',
          onSelect: () => {
            trigger.setAttribute('aria-expanded', 'false');
            onFilesMenuAction({ target: item });
            files.menuTarget = null;
            files.menuTrigger = null;
          }
        };
      });
      const titleId = titleIdOf(entry.name);
      const titleEntry = titleId ? titleNames.cache.get(titleId) : null;
      const gameName = titleEntry && titleEntry.name ? titleEntry.name : '';
      const sub = [gameName, entry.dir ? '文件夹' : formatBytes(entry.size), formatFileTime(entry.mtime)].filter(Boolean).join(' · ');
      trigger.setAttribute('aria-expanded', 'true');
      openActionSheet({
        title: entry.name,
        sub,
        items,
        returnFocus: trigger,
        onDismiss: () => {
          trigger.setAttribute('aria-expanded', 'false');
          files.menuTarget = null;
          files.menuTrigger = null;
        }
      });
      return;
    }
    menu.hidden = false;
    const rect = trigger.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    let left = Math.min(rect.right - menuRect.width, window.innerWidth - menuRect.width - 8);
    left = Math.max(8, left);
    let top = rect.bottom + 6;
    if (top + menuRect.height > window.innerHeight - 8) top = Math.max(8, rect.top - menuRect.height - 6);
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    trigger.setAttribute('aria-expanded', 'true');
    const first = menu.querySelector('button:not(:disabled)');
    if (first) first.focus({ preventScroll: true });
  }

  function closeFilesMenu({ restoreFocus = true } = {}) {
    const menu = $('files-menu');
    if (!menu || menu.hidden) return;
    const trigger = files.menuTrigger;
    menu.hidden = true;
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
    files.menuTarget = null;
    files.menuTrigger = null;
    if (restoreFocus && trigger && trigger.isConnected) trigger.focus({ preventScroll: true });
  }

  function onFilesMenuAction(event) {
    const item = event.target.closest('[data-files-menu]');
    if (!item) return;
    const action = item.dataset.filesMenu;
    const entry = files.menuTarget;
    const trigger = files.menuTrigger;
    closeFilesMenu({ restoreFocus: false });
    if (!entry) return;
    if (action === 'download') downloadFilesEntry(entry);
    else if (action === 'rename') openFilesNameDialog('rename', entry);
    else if (action === 'copy') openFilesDest('copy', entry);
    else if (action === 'move') openFilesDest('move', entry);
    else if (action === 'delete') openFilesDelete(entry);
    if (trigger && trigger.isConnected && action === 'download') trigger.focus({ preventScroll: true });
  }

  function downloadFilesEntry(entry) {
    const path = entry.dir
      ? `/api/files/tar?path=${encodeURIComponent(entry.path)}`
      : `/api/files/download?path=${encodeURIComponent(entry.path)}`;
    const link = document.createElement('a');
    link.href = path;
    link.setAttribute('download', entry.name);
    link.rel = 'noopener';
    document.body.append(link);
    link.click();
    link.remove();
  }

  function openFilesHostDialog() {
    if (!authenticated) return;
    const info = files.info;
    const hosts = info && info.hosts.length ? info.hosts : info && info.host ? [info.host] : [];
    if (!hosts.length) {
      toast('没有可切换的主机；请先在「PS5 主机」页面扫描。', 'info');
      return;
    }
    const select = $('files-host-select');
    const fragment = document.createDocumentFragment();
    for (const host of hosts) {
      const option = document.createElement('option');
      option.value = host;
      option.textContent = host;
      if (info && info.host === host) option.selected = true;
      fragment.append(option);
    }
    select.replaceChildren(fragment);
    $('files-host-error').textContent = '';
    $('files-host-error').hidden = true;
    $('files-host-cancel').disabled = false;
    $('files-host-confirm').disabled = false;
    $('files-host-dialog').showModal();
    select.focus({ preventScroll: true });
  }

  async function configureFilesHost(host, { closeDialog = false } = {}) {
    if (!host || files.configBusy || !authenticated) return false;
    files.configBusy = true;
    files.requestedHost = host;
    updateFilesControls();
    const requestEpoch = epoch;
    let configured = false;
    try {
      const data = await api('/api/files/configure', { host });
      if (requestEpoch !== epoch) return false;
      files.info = normalizeFilesInfo(data);
      files.requestedHost = files.info.host || host;
      if (files.info.configured && files.info.reachable) await loadFilesVolumesAndList();
      files.loaded = Boolean(files.info.configured);
      setFilesError('');
      setFilesSetupError('');
      configured = true;
      if (closeDialog && $('files-host-dialog').open) $('files-host-dialog').close();
      toast(`已连接 ${files.info.host || host}。`, 'success');
    } catch (error) {
      if (requestEpoch !== epoch) return false;
      if (error.status === 401) {
        showLogin(error.message);
        return false;
      }
      if ($('files-host-dialog').open) {
        $('files-host-error').textContent = error.message;
        $('files-host-error').hidden = false;
      } else {
        setFilesSetupError(error.message);
      }
      toast(error.message, 'error');
    } finally {
      files.configBusy = false;
      updateFilesControls();
      renderFiles();
    }
    return configured;
  }

  function connectFilesSetup() {
    const host = $('files-setup-host').value;
    if (!host) {
      setFilesSetupError('请选择主机。');
      return;
    }
    setFilesSetupError('');
    configureFilesHost(host);
  }

  function openFilesNameDialog(mode, entry) {
    if (!authenticated || files.busy || !files.path) return;
    filesNameMode = mode;
    filesNameTarget = entry || null;
    const renaming = mode === 'rename' && entry;
    $('files-name-title').textContent = renaming ? '重命名' : '新建文件夹';
    $('files-name-desc').textContent = renaming
      ? `重命名 ${entry.name}；文件路径会随之更新。`
      : `在 ${files.path} 中创建文件夹。`;
    $('files-name-hint').textContent = '名称不能包含斜杠或控制字符，且不能是 . 或 ..。';
    const input = $('files-name-input');
    input.value = renaming ? entry.name : '';
    input.removeAttribute('aria-invalid');
    $('files-name-error').textContent = '';
    $('files-name-error').hidden = true;
    $('files-name-cancel').disabled = false;
    $('files-name-submit').disabled = false;
    $('files-name-submit').querySelector('.button-label').textContent = renaming ? '重命名' : '创建';
    $('files-name-dialog').showModal();
    input.focus({ preventScroll: true });
    input.select();
  }

  function filesNameError(name) {
    if (!name) return '请输入名称。';
    if (name === '.' || name === '..') return '不能使用 . 或 .. 作为名称。';
    if (/[/\\\x00-\x1f\x7f]/.test(name)) return '名称不能包含斜杠或控制字符。';
    if (name.length > 120) return '名称过长。';
    return '';
  }

  async function submitFilesName(event) {
    event.preventDefault();
    if (!authenticated || files.busy || !filesNameMode) return;
    const input = $('files-name-input');
    const value = input.value.trim();
    const message = filesNameError(value);
    if (message) {
      input.setAttribute('aria-invalid', 'true');
      $('files-name-error').textContent = message;
      $('files-name-error').hidden = false;
      input.focus();
      return;
    }
    if (filesNameMode === 'rename' && filesNameTarget && value === filesNameTarget.name) {
      $('files-name-dialog').close();
      return;
    }
    const mode = filesNameMode;
    const target = filesNameTarget;
    files.busy = true;
    updateFilesControls();
    $('files-name-cancel').disabled = true;
    buttonBusy('files-name-submit', true, '保存中…');
    $('files-name-error').textContent = '';
    $('files-name-error').hidden = true;
    const requestEpoch = epoch;
    let saved = false;
    try {
      if (mode === 'mkdir') {
        await api('/api/files/mkdir', { path: filesJoin(files.path, value) });
        toast(`已创建文件夹 ${value}。`, 'success');
      } else {
        await api('/api/files/rename', { from: target.path, to: filesJoin(filesParent(target.path), value) });
        toast(`已重命名为 ${value}。`, 'success');
      }
      saved = true;
      $('files-name-dialog').close();
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) {
        $('files-name-dialog').close();
        showLogin(error.message);
        return;
      }
      $('files-name-error').textContent = error.message;
      $('files-name-error').hidden = false;
    } finally {
      files.busy = false;
      buttonBusy('files-name-submit', false, mode === 'rename' ? '重命名' : '创建');
      $('files-name-cancel').disabled = false;
      updateFilesControls();
    }
    if (saved) refreshFilesPage({ manual: false });
  }

  function openFilesDelete(entry) {
    if (!authenticated || files.busy) return;
    files.deleteTarget = entry;
    $('files-delete-name').textContent = entry.name;
    $('files-delete-extra').textContent = entry.dir ? '文件夹中的所有内容会被一并删除。' : '';
    $('files-delete-error').textContent = '';
    $('files-delete-error').hidden = true;
    $('files-delete-cancel').disabled = false;
    $('files-delete-confirm').disabled = false;
    $('files-delete-dialog').showModal();
    $('files-delete-cancel').focus({ preventScroll: true });
  }

  async function confirmFilesDelete() {
    const target = files.deleteTarget;
    if (!target || files.busy || !authenticated) return;
    files.busy = true;
    updateFilesControls();
    $('files-delete-cancel').disabled = true;
    $('files-delete-confirm').disabled = true;
    buttonBusy('files-delete-confirm', true, '删除中…');
    $('files-delete-error').textContent = '';
    $('files-delete-error').hidden = true;
    const requestEpoch = epoch;
    let removed = false;
    try {
      await api('/api/files/delete', { path: target.path });
      removed = true;
      $('files-delete-dialog').close();
      toast(`已删除 ${target.name}。`, 'success');
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) {
        $('files-delete-dialog').close();
        showLogin(error.message);
        return;
      }
      $('files-delete-error').textContent = error.message;
      $('files-delete-error').hidden = false;
    } finally {
      files.busy = false;
      buttonBusy('files-delete-confirm', false, '确认删除');
      $('files-delete-cancel').disabled = false;
      updateFilesControls();
    }
    if (removed) {
      files.deleteTarget = null;
      refreshFilesPage({ manual: false });
    }
  }

  function openFilesDest(mode, entry) {
    if (!authenticated || files.busy || !entry) return;
    files.destMode = mode;
    files.destTarget = entry;
    files.destPath = files.path;
    $('files-dest-overwrite-row').hidden = true;
    $('files-dest-extract-row').hidden = true;
    $('files-dest-archive').hidden = true;
    $('files-dest-space').hidden = true;
    $('files-dest-title').textContent = mode === 'move' ? '移动到…' : '复制到…';
    $('files-dest-description').textContent = `选择目标文件夹。目标不能是当前文件夹；跨硬盘${mode === 'move' ? '移动' : '复制'}可能较慢。`;
    $('files-dest-confirm').querySelector('.button-label').textContent = mode === 'move' ? '移动到这里' : '复制到这里';
    $('files-dest-confirm').setAttribute('aria-label', mode === 'move' ? '确认移动到所选文件夹' : '确认复制到所选文件夹');
    $('files-dest-error').textContent = '';
    $('files-dest-error').hidden = true;
    $('files-dest-cancel').disabled = false;
    $('files-dest-confirm').disabled = false;
    $('files-dest-dialog').showModal();
    $('files-dest-cancel').focus({ preventScroll: true });
    loadFilesDest(files.path);
  }

  function renderFilesDestSpace() {
    const node = $('files-dest-space');
    const target = files.destTarget;
    const volume = files.destPath ? filesVolumeOf(files.destPath) : null;
    if (!['nas', 'extract'].includes(files.destMode) || !volume || volume.free === null) {
      node.hidden = true;
      return;
    }
    const need = files.destMode === 'extract' && target && target.unpacked ? target.unpacked
      : target && Number.isFinite(target.bytes) ? target.bytes : 0;
    let text = `目标存储 ${volume.label} 可用 ${formatBytes(volume.free)}`;
    if (files.destMode === 'extract' && target && target.unpacked) text += `；解压后约 ${formatBytes(target.unpacked)}`;
    else if (need && files.destMode === 'extract') text += `；压缩包共 ${formatBytes(need)}（解压后可能更大）`;
    else if (need) text += `；选中的文件约 ${formatBytes(need)}${target.hasFolders ? '（另含文件夹，传输时再统计）' : ''}`;
    if (need && need > volume.free) text += '，空间不足，传输会失败。';
    node.textContent = text;
    node.dataset.tone = need && need > volume.free ? 'error' : '';
    node.hidden = false;
  }

  async function loadFilesDest(path) {
    if (!authenticated) return;
    files.destPath = path;
    $('files-dest-path').textContent = path || '存储位置';
    renderFilesDestSpace();
    const list = $('files-dest-list');
    list.replaceChildren(makeElement('p', 'files-dest-empty', '正在读取…'));
    files.destLoading = true;
    try {
      if (!path) {
        const fragment = document.createDocumentFragment();
        for (const volume of files.volumes) {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'files-dest-item';
          button.setAttribute('aria-label', `选择存储位置 ${volume.label}`);
          button.append(makeIcon(filesVolumeIcon(volume.kind)), makeElement('span', '', volume.label), makeElement('span', 'mono files-dest-path', volume.path));
          button.addEventListener('click', () => loadFilesDest(volume.path));
          fragment.append(button);
        }
        if (!files.volumes.length) fragment.append(makeElement('p', 'files-dest-empty', '没有可用的存储位置。'));
        list.replaceChildren(fragment);
        return;
      }
      const data = await api(`/api/files/list?path=${encodeURIComponent(path)}`);
      const entries = normalizeFilesEntries(data, path).entries.filter((entry) => entry.dir);
      const fragment = document.createDocumentFragment();
      if (!entries.length) fragment.append(makeElement('p', 'files-dest-empty', '这里没有子文件夹。'));
      for (const entry of entries) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'files-dest-item';
        button.setAttribute('aria-label', `进入文件夹 ${entry.name}`);
        const folderIcon = makeIcon('i-folder');
        folderIcon.classList.add('folder');
        button.append(folderIcon, makeElement('span', '', entry.name));
        button.addEventListener('click', () => loadFilesDest(entry.path));
        fragment.append(button);
      }
      list.replaceChildren(fragment);
    } catch (error) {
      list.replaceChildren(makeElement('p', 'files-dest-empty', error.message));
    } finally {
      files.destLoading = false;
    }
  }

  async function confirmFilesDest() {
    if (files.destMode === 'nas') {
      startNasTransfer();
      return;
    }
    if (files.destMode === 'extract') {
      startNasExtract();
      return;
    }
    const target = files.destTarget;
    if (!target || !authenticated || files.busy) return;
    if (!files.destPath) {
      $('files-dest-error').textContent = '请选择目标文件夹。';
      $('files-dest-error').hidden = false;
      return;
    }
    if (files.destPath === files.path) {
      $('files-dest-error').textContent = '目标就是当前文件夹，请选择其他位置。';
      $('files-dest-error').hidden = false;
      return;
    }
    files.busy = true;
    updateFilesControls();
    $('files-dest-cancel').disabled = true;
    buttonBusy('files-dest-confirm', true, '处理中…');
    $('files-dest-error').textContent = '';
    $('files-dest-error').hidden = true;
    const requestEpoch = epoch;
    let started = false;
    let instant = false;
    try {
      const result = await api(files.destMode === 'move' ? '/api/files/move' : '/api/files/copy', {
        from: target.path,
        to: filesJoin(files.destPath, target.name)
      });
      if (result.ok !== true) throw new ApiError('服务未确认复制或移动任务，请刷新后检查。');
      started = true;
      instant = result.instant === true;
      $('files-dest-dialog').close();
      toast(files.destMode === 'move' ? `已开始移动 ${target.name}。` : `已开始复制 ${target.name}。`, 'success');
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) {
        $('files-dest-dialog').close();
        showLogin(error.message);
        return;
      }
      $('files-dest-error').textContent = error.message;
      $('files-dest-error').hidden = false;
    } finally {
      files.busy = false;
      buttonBusy('files-dest-confirm', false, files.destMode === 'move' ? '移动到这里' : '复制到这里');
      $('files-dest-cancel').disabled = false;
      updateFilesControls();
    }
    if (!started) return;
    if (instant) {
      toast('主机在同一硬盘内瞬间完成。', 'success');
      refreshFilesPage({ manual: false });
      return;
    }
    files.job = { kind: files.destMode, state: files.destMode === 'move' ? 'moving' : 'copying', percent: 0, done: null, total: null, error: '' };
    files.jobActive = true;
    files.refreshOnJobEnd = true;
    files.drawer = true;
    renderFilesTransfers();
    ensureFilesPolling();
    filesTransfersTick();
  }

  async function filesTransfersTick() {
    if (!authenticated || filesPollBusy) return;
    if (!files.drawer && !files.jobActive && !files.uploading && !nasJobsActive()) return;
    filesPollBusy = true;
    const requestEpoch = epoch;
    try {
      if (files.drawer || files.uploading) {
        const data = await api('/api/files/transfers');
        if (requestEpoch !== epoch) return;
        files.transfers = normalizeFilesTransfers(data);
        files.transfersError = '';
      }
      if (files.drawer || nasJobsActive()) {
        const data = await api('/api/transfers');
        if (requestEpoch !== epoch) return;
        const wasActive = nasJobsActive();
        nasJobs = Array.isArray(data.jobs) ? data.jobs.filter((job) => job && typeof job === 'object' && job.id) : [];
        if (wasActive && !nasJobsActive() && files.mode === 'ps5' && files.path) refreshFilesPage({ manual: false });
      }
      if (files.drawer || files.jobActive) {
        const data = await api('/api/files/job');
        if (requestEpoch !== epoch) return;
        const job = normalizeFilesJob(data);
        const terminal = ['idle', 'done', 'failed', 'canceled'].includes(job.state);
        files.job = job.state === 'idle' && !files.jobActive ? null : job;
        if (!terminal) files.jobActive = true;
        if (terminal && files.jobActive) {
          files.jobActive = false;
          if (files.refreshOnJobEnd) {
            files.refreshOnJobEnd = false;
            refreshFilesPage({ manual: false });
          }
        }
      }
    } catch (error) {
      if (requestEpoch !== epoch) return;
      if (error.status === 401) {
        showLogin(error.message);
        return;
      }
      files.transfersError = error.message;
    } finally {
      filesPollBusy = false;
      renderFilesTransfers();
      updateFilesControls();
      ensureFilesPolling();
    }
  }

  function ensureFilesPolling() {
    const should = authenticated && (files.drawer || files.jobActive || files.uploading || nasJobsActive());
    if (should && !filesPollTimer) filesPollTimer = setInterval(filesTransfersTick, 1000);
    else if (!should && filesPollTimer) {
      clearInterval(filesPollTimer);
      filesPollTimer = null;
    }
  }

  function stopFilesPolling() {
    clearInterval(filesPollTimer);
    filesPollTimer = null;
  }

  async function cancelFilesJob() {
    if (!authenticated || files.busy || !files.jobActive) return;
    files.busy = true;
    updateFilesControls();
    try {
      await api('/api/files/cancel', {});
      toast('已请求取消当前任务。', 'info');
    } catch (error) {
      if (error.status === 401) {
        showLogin(error.message);
        return;
      }
      files.transfersError = error.message;
    } finally {
      files.busy = false;
      updateFilesControls();
      renderFilesTransfers();
    }
    filesTransfersTick();
  }

  const formatDuration = (seconds) => {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const secs = value % 60;
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}` : `${minutes}:${String(secs).padStart(2, '0')}`;
  };
  const formatRate = (bytesPerSecond) => (bytesPerSecond > 0 ? `${formatBytes(bytesPerSecond)}/s` : '—');
  const XFER_ACTIVE = new Set(['wait', 'run', 'sync', 'queued', 'scanning', 'running', 'copying', 'moving', 'active', 'pending', 'wait-confirm']);
  const XFER_LABEL = { wait: '等待', run: '传输中', sync: '正在落盘', queued: '排队中', scanning: '正在统计文件', running: '传输中', copying: '复制中', moving: '移动中', active: '进行中', pending: '等待', done: '完成', done_with_warnings: '完成（有提醒）', interrupted: '已中断', fail: '失败', failed: '失败', cancel: '已取消', canceled: '已取消', skip: '已跳过', 'wait-confirm': '等待确认' };
  const XFER_KIND = { upload: '上传', copy: '复制', move: '移动', nas: 'NAS → PS5', extract: '解压安装', remote: 'PS5' };

  function filesTransferTasks() {
    const tasks = [];
    for (const job of nasJobs) {
      if (job.kind === 'extract') {
        tasks.push(extractTask(job));
        continue;
      }
      tasks.push({
        key: `nas:${job.id}`, kind: 'nas', name: job.name, state: job.state,
        sub: `${job.source} → ${job.dest}`,
        done: job.bytes_done, total: job.bytes_total, speed: job.speed, elapsed: job.elapsed, eta: job.eta,
        files: { done: job.files_done, total: job.files_total, skipped: job.files_skipped, failed: job.files_failed, retries: job.retries || 0, resumed: job.resumed || 0 },
        current: job.state === 'running' ? job.current : '',
        error: job.error, errors: job.errors || [],
        interrupted: job.interrupted === true,
        line: job.retry_of ? '重试任务：已在 PS5 上的同名文件会跳过' : '',
        cancel: XFER_ACTIVE.has(job.state) ? () => cancelNasJob(job.id) : null,
        retry: ['failed', 'canceled'].includes(job.state) && job.kind === 'nas' ? () => retryNasJob(job.id) : null
      });
    }
    const job = files.job;
    if (job && job.state !== 'idle') {
      tasks.push({
        key: 'copy-job', kind: job.kind === 'move' ? 'move' : 'copy', name: job.name || (job.kind === 'move' ? '移动任务' : '复制任务'),
        state: job.state, sub: '在 PS5 上执行', done: job.done, total: job.total, speed: job.speed,
        elapsed: job.elapsed, eta: job.speed > 0 && job.total && job.done !== null ? (job.total - job.done) / job.speed : null,
        error: job.error, cancel: files.jobActive ? cancelFilesJob : null
      });
    }
    for (const task of files.uploads) {
      const end = task.finished || Date.now();
      const elapsed = task.started ? (end - task.started) / 1000 : null;
      tasks.push({
        key: `upload:${task.id}`, kind: 'upload', name: task.name, state: task.state,
        sub: `本机浏览器 → ${files.path || 'PS5'}`, done: task.done, total: task.size,
        speed: task.state === 'run' ? task.speed : 0, elapsed,
        eta: task.state === 'run' && task.speed > 0 ? (task.size - task.done) / task.speed : null,
        error: task.state === 'fail' ? task.note : '',
        cancel: ['wait', 'run'].includes(task.state) ? () => cancelFilesUpload(task) : null
      });
    }
    const localNames = new Set(files.uploads.map((task) => task.name));
    for (const item of files.transfers) {
      if (String(item.kind).toLowerCase() === 'upload' && localNames.has(item.name)) continue;
      tasks.push({
        key: `remote:${item.id || item.name}`, kind: 'remote', name: item.name, state: String(item.state).toLowerCase(),
        sub: filesKindLabel(item.kind), done: item.done, total: item.size,
        speed: Number.isFinite(item.speed) && item.speed > 0 ? item.speed * 1048576 : 0,
        elapsed: item.ms > 0 ? item.ms / 1000 : null, eta: null, error: '', cancel: null
      });
    }
    return tasks;
  }

  function filesTransferCard(task) {
    const item = makeElement('li', 'xfer');
    item.dataset.state = XFER_ACTIVE.has(task.state) ? 'active' : task.state;
    const head = makeElement('div', 'xfer-head');
    const title = makeElement('div', 'xfer-title');
    title.append(makeElement('span', 'xfer-kind', XFER_KIND[task.kind] || task.kind), makeElement('span', 'xfer-name', task.name));
    title.lastChild.title = task.name;
    const label = task.interrupted ? XFER_LABEL.interrupted : XFER_LABEL[task.state];
    const badge = makeElement('span', 'result-badge', label || task.state || '—');
    badge.dataset.tone = task.state === 'done' ? 'good' : task.state === 'done_with_warnings' ? 'warning'
      : ['fail', 'failed'].includes(task.state) ? 'error' : XFER_ACTIVE.has(task.state) ? 'info' : 'neutral';
    head.append(title, badge);
    if (task.retry) {
      const retry = makeElement('button', 'button button-secondary button-small', '重试');
      retry.type = 'button';
      retry.title = '重新排队，PS5 上已有的同名文件会跳过';
      retry.setAttribute('aria-label', `重试 ${task.name}`);
      retry.addEventListener('click', task.retry);
      head.append(retry);
    }
    if (task.cancel) {
      const cancel = makeElement('button', 'button button-secondary button-small', '取消');
      cancel.type = 'button';
      cancel.setAttribute('aria-label', `取消 ${task.name}`);
      cancel.addEventListener('click', task.cancel);
      head.append(cancel);
    }
    item.append(head);
    if (task.sub) item.append(makeElement('p', 'xfer-sub mono', task.sub));

    const total = Number(task.total) || 0;
    const done = Math.min(Number(task.done) || 0, total || Number(task.done) || 0);
    const percent = total > 0 ? Math.min(100, (done / total) * 100) : task.state === 'done' ? 100 : 0;
    const meter = makeElement('div', 'xfer-meter');
    meter.setAttribute('role', 'progressbar');
    meter.setAttribute('aria-valuemin', '0');
    meter.setAttribute('aria-valuemax', '100');
    meter.setAttribute('aria-valuenow', percent.toFixed(1));
    meter.setAttribute('aria-label', `${task.name} 进度`);
    const fill = document.createElement('i');
    fill.style.width = `${percent}%`;
    if (task.state === 'scanning' || task.state === 'queued') meter.dataset.indeterminate = 'true';
    meter.append(fill);
    item.append(meter);

    const stats = makeElement('div', 'xfer-stats');
    const stat = (label, value, strong = false) => {
      const node = makeElement('span', 'xfer-stat');
      node.append(makeElement('span', 'xfer-stat-label', label), makeElement(strong ? 'strong' : 'span', 'xfer-stat-value', value));
      stats.append(node);
    };
    stat('进度', total > 0 ? `${percent.toFixed(percent < 10 && percent > 0 ? 1 : 0)}%` : task.state === 'scanning' ? '统计中' : '—', true);
    stat('已传', total > 0 ? `${formatBytes(done)} / ${formatBytes(total)}` : formatBytes(done));
    stat('剩余', total > 0 ? formatBytes(Math.max(0, total - done)) : '—');
    stat('速度', XFER_ACTIVE.has(task.state) ? formatRate(task.speed) : task.elapsed > 0 && done > 0 ? `平均 ${formatRate(done / task.elapsed)}` : '—');
    stat('用时', task.elapsed !== null && task.elapsed !== undefined ? formatDuration(task.elapsed) : '—');
    stat('预计剩余', XFER_ACTIVE.has(task.state) && task.eta !== null && task.eta !== undefined ? formatDuration(task.eta) : '—');
    item.append(stats);

    if (task.line) item.append(makeElement('p', 'xfer-sub', task.line));
    if (task.files && task.files.total) {
      const parts = [`文件 ${task.files.done} / ${task.files.total}`];
      if (task.files.skipped) parts.push(`跳过 ${task.files.skipped}`);
      if (task.files.failed) parts.push(`失败 ${task.files.failed}`);
      if (task.files.retries) parts.push(`自动重试 ${task.files.retries} 次`);
      if (task.files.resumed) parts.push(`续传 ${task.files.resumed} 次`);
      if (task.current) parts.push(`当前 ${task.current}`);
      item.append(makeElement('p', 'xfer-sub', parts.join(' · ')));
    }
    if (task.error) {
      const error = makeElement('p', 'xfer-error', task.error);
      if (task.errors && task.errors.length) error.title = task.errors.join('\n');
      item.append(error);
    }
    return item;
  }

  // Finished tasks keep the dock visible only until they have been noticed:
  // successes fade out after a short while, failures wait for the panel.
  const xferFinishedAt = new Map();
  const xferSeen = new Set();
  const XFER_DONE_LINGER = 12000;
  let xferLingerTimer = null;

  function xferNeedsAttention(tasks) {
    const now = Date.now();
    let pending = false;
    for (const task of tasks) {
      if (XFER_ACTIVE.has(task.state)) {
        xferFinishedAt.delete(task.key);
        xferSeen.delete(task.key);
        continue;
      }
      if (!xferFinishedAt.has(task.key)) xferFinishedAt.set(task.key, now);
      if (xferSeen.has(task.key)) continue;
      const failed = ['fail', 'failed'].includes(task.state);
      if (failed || now - xferFinishedAt.get(task.key) < XFER_DONE_LINGER) pending = true;
      else xferSeen.add(task.key);
    }
    clearTimeout(xferLingerTimer);
    if (pending) xferLingerTimer = setTimeout(renderFilesTransfers, 1500);
    return pending;
  }

  function renderFilesTransfers() {
    const dock = $('xfer-dock');
    if (!dock) return;
    const tasks = filesTransferTasks();
    const active = tasks.filter((task) => XFER_ACTIVE.has(task.state));
    if (files.drawer) for (const task of tasks) if (!XFER_ACTIVE.has(task.state)) xferSeen.add(task.key);
    const attention = xferNeedsAttention(tasks);
    for (const badge of $$('[data-files-transfers-count]')) {
      badge.textContent = active.length ? String(active.length) : '';
      badge.hidden = !active.length;
    }
    // The dock lives outside the pages: visible whenever there is something
    // to report, or when the user opened it on purpose.
    dock.hidden = !authenticated || !(files.drawer || active.length || attention);
    document.body.dataset.dock = dock.hidden ? 'off' : 'on';
    dock.dataset.open = String(files.drawer);
    dock.dataset.active = String(active.length > 0);
    $('xfer-pill').setAttribute('aria-expanded', String(files.drawer));
    for (const button of $$('[data-files-transfers]')) button.setAttribute('aria-expanded', String(files.drawer));

    const total = active.reduce((sum, task) => sum + (Number(task.total) || 0), 0);
    const done = active.reduce((sum, task) => sum + Math.min(Number(task.done) || 0, Number(task.total) || 0), 0);
    const speed = active.reduce((sum, task) => sum + (Number(task.speed) || 0), 0);
    const percent = total > 0 ? Math.min(100, (done / total) * 100) : active.length ? 0 : 100;
    $('xfer-ring-fill').style.strokeDashoffset = String(100 - percent);
    const failed = tasks.filter((task) => ['fail', 'failed'].includes(task.state)).length;
    dock.dataset.tone = active.length ? 'active' : failed ? 'error' : 'idle';
    if (active.length) {
      $('xfer-pill-title').textContent = `${active.length} 个任务进行中 · ${percent.toFixed(percent < 10 && percent > 0 ? 1 : 0)}%`;
      const eta = speed > 0 && total > done ? ` · 约 ${formatDuration((total - done) / speed)}` : '';
      $('xfer-pill-sub').textContent = `${formatRate(speed)}${total ? ` · 剩余 ${formatBytes(total - done)}` : ''}${eta}`;
    } else {
      $('xfer-pill-title').textContent = failed ? `${failed} 个任务失败` : '传输已完成';
      $('xfer-pill-sub').textContent = tasks.length ? `共 ${tasks.length} 项，点开查看` : '暂无任务';
    }
    $('xfer-panel-sub').textContent = active.length
      ? `${active.length} 个进行中 · ${formatRate(speed)}`
      : tasks.length ? `${tasks.length} 项已结束` : '上传、复制/移动与 NAS 传输';

    const list = $('files-transfers-list');
    if (!tasks.length) {
      list.replaceChildren(makeElement('li', 'xfer-empty', '暂无传输任务。开始上传或从 NAS 传到 PS5 后会显示在这里。'));
    } else {
      // Reuse cards by task key so the enter animation plays once per task,
      // not on every one-second refresh.
      const existing = new Map($$('#files-transfers-list > .xfer').map((node) => [node.dataset.key, node]));
      const nodes = tasks.map((task) => {
        const fresh = filesTransferCard(task);
        fresh.dataset.key = task.key;
        const old = existing.get(task.key);
        if (old) {
          old.replaceChildren(...fresh.childNodes);
          old.dataset.state = fresh.dataset.state;
          return old;
        }
        fresh.classList.add('xfer-enter');
        fresh.addEventListener('animationend', () => fresh.classList.remove('xfer-enter'), { once: true });
        return fresh;
      });
      // Move only what changed: re-inserting a node restarts its animation.
      for (const node of [...list.children]) if (!nodes.includes(node)) node.remove();
      nodes.forEach((node, index) => {
        if (list.children[index] !== node) list.insertBefore(node, list.children[index] || null);
      });
    }
    $('files-transfers-clear').disabled = !tasks.some((task) => !XFER_ACTIVE.has(task.state));
    const error = $('files-transfers-error');
    error.textContent = files.transfersError || '';
    error.hidden = !files.transfersError;
    updateFilesControls();
  }

  function cancelFilesUpload(task) {
    if (task.state === 'wait') {
      task.state = 'cancel';
      renderFilesTransfers();
    } else if (task.state === 'run' && files.uploadXhr) {
      files.uploadXhr.abort();
    }
  }

  async function clearFilesTransfers() {
    files.uploads = files.uploads.filter((task) => XFER_ACTIVE.has(task.state));
    if (files.job && !files.jobActive) files.job = null;
    nasJobs = nasJobs.filter((job) => XFER_ACTIVE.has(job.state));
    renderFilesTransfers();
    try {
      await api('/api/transfers/clear', {});
    } catch (error) {
      if (error.status === 401) showLogin(error.message);
    }
  }

  function toggleFilesDrawer(force) {
    files.drawer = typeof force === 'boolean' ? force : !files.drawer;
    renderFilesTransfers();
    ensureFilesPolling();
    if (files.drawer) {
      filesTransfersTick();
      revealFilesTransfers();
    }
  }

  function revealFilesTransfers() {
    // The floating dock is always in view; opening it is enough.
    if (!files.drawer) {
      files.drawer = true;
      renderFilesTransfers();
    }
  }

  function openFilesOverwrite(names, onChoice) {
    filesOverwritePending = { names, onChoice };
    $('files-overwrite-count').textContent = String(names.length);
    const box = $('files-overwrite-names');
    const fragment = document.createDocumentFragment();
    for (const name of names.slice(0, 40)) fragment.append(makeElement('div', 'files-names-item', name));
    if (names.length > 40) fragment.append(makeElement('div', 'files-names-item muted', `还有 ${names.length - 40} 项`));
    box.replaceChildren(fragment);
    $('files-overwrite-error').textContent = '';
    $('files-overwrite-error').hidden = true;
    $('files-overwrite-cancel').disabled = false;
    $('files-overwrite-skip').disabled = false;
    $('files-overwrite-confirm').disabled = false;
    $('files-overwrite-dialog').showModal();
    $('files-overwrite-cancel').focus({ preventScroll: true });
  }

  function settleFilesOverwrite(choice) {
    const pending = filesOverwritePending;
    if (!pending) return;
    filesOverwritePending = null;
    if ($('files-overwrite-dialog').open) $('files-overwrite-dialog').close();
    pending.onChoice(choice);
  }

  function askFilesOverwriteSingle(name) {
    return new Promise((resolve) => openFilesOverwrite([name], resolve));
  }

  function enqueueFilesUploads(items, { overwrite = new Set(), skip = new Set() } = {}) {
    files.uploads = [];
    for (const file of items) {
      const id = ++files.uploadSeq;
      const skipping = skip.has(file.name);
      files.uploads.push({
        id,
        kind: 'upload',
        name: file.name,
        size: file.size,
        done: 0,
        state: skipping ? 'skip' : 'wait',
        speed: 0,
        started: 0,
        finished: 0,
        note: '',
        file,
        overwrite: overwrite.has(file.name)
      });
    }
    files.drawer = true;
    renderFilesTransfers();
    ensureFilesPolling();
    revealFilesTransfers();
    pumpFilesUploads();
  }

  function queueFilesUpload(list) {
    if (!authenticated) return;
    if (!files.path) {
      toast('请先选择目标文件夹。', 'error');
      return;
    }
    if (files.uploading) {
      toast('已有上传任务正在进行，请稍后再试。', 'info');
      return;
    }
    const items = Array.from(list || []).filter((file) => file && typeof file.name === 'string' && file.size >= 0);
    if (!items.length) {
      toast('没有可上传的文件。', 'info');
      return;
    }
    const existing = new Set(files.entries.map((entry) => entry.name));
    const clashes = items.filter((file) => existing.has(file.name));
    if (clashes.length) {
      const names = clashes.map((file) => file.name);
      openFilesOverwrite(names, (choice) => {
        if (choice === 'cancel') return;
        const overwrite = choice === 'overwrite' ? new Set(names) : new Set();
        const skip = choice === 'skip' ? new Set(names) : new Set();
        enqueueFilesUploads(items, { overwrite, skip });
      });
      return;
    }
    enqueueFilesUploads(items);
  }

  function uploadFilesOne(task) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      files.uploadXhr = xhr;
      const started = performance.now();
      task.started = Date.now();
      let lastAt = started;
      let lastBytes = 0;
      xhr.open('POST', `/api/files/upload?path=${encodeURIComponent(files.path)}&overwrite=${task.overwrite ? 'true' : 'false'}`);
      xhr.responseType = 'json';
      xhr.setRequestHeader('X-Filename', encodeURIComponent(task.name));
      xhr.setRequestHeader('X-CSRF-Token', csrf);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.addEventListener('progress', (event) => {
        task.done = event.loaded;
        const now = performance.now();
        if (now - lastAt >= 400) {
          // bytes per second, smoothed so the number does not jump around
          const instant = (event.loaded - lastBytes) / ((now - lastAt) / 1000);
          task.speed = task.speed ? task.speed * 0.6 + instant * 0.4 : instant;
          lastAt = now;
          lastBytes = event.loaded;
          renderFilesTransfers();
        }
      });
      xhr.addEventListener('load', () => {
        const payload = xhr.response && typeof xhr.response === 'object' ? xhr.response : {};
        if (xhr.status >= 200 && xhr.status < 300 && typeof payload.error !== 'string') {
          task.done = task.size;
          task.speed = task.size / Math.max(0.001, (performance.now() - started) / 1000);
          task.finished = Date.now();
          resolve();
          return;
        }
        if (xhr.status === 409 || payload.error === 'exists') {
          const error = new Error('同名文件已存在');
          error.code = 'exists';
          reject(error);
          return;
        }
        if (xhr.status === 401) {
          const error = new Error('登录已失效，请重新输入管理密码。');
          error.code = 'unauthorized';
          reject(error);
          return;
        }
        reject(new Error(typeof payload.error === 'string' && payload.error ? payload.error : `上传失败（HTTP ${xhr.status}）`));
      });
      xhr.addEventListener('error', () => reject(new Error('连接中断，无法上传。')));
      xhr.addEventListener('abort', () => {
        const error = new Error('已取消');
        error.code = 'canceled';
        reject(error);
      });
      xhr.send(task.file);
    });
  }

  async function pumpFilesUploads() {
    if (files.uploading) return;
    files.uploading = true;
    files.transfersError = '';
    ensureFilesPolling();
    for (;;) {
      const task = files.uploads.find((entry) => entry.state === 'wait');
      if (!task) break;
      task.state = 'run';
      task.done = 0;
      renderFilesTransfers();
      try {
        await uploadFilesOne(task);
        task.state = 'done';
      } catch (error) {
        if (error && error.code === 'unauthorized') {
          task.state = 'fail';
          task.note = error.message;
          renderFilesTransfers();
          showLogin(error.message);
          break;
        }
        if (error && error.code === 'exists') {
          task.state = 'wait-confirm';
          renderFilesTransfers();
          const choice = await askFilesOverwriteSingle(task.name);
          if (choice === 'overwrite') {
            task.overwrite = true;
            task.state = 'wait';
            continue;
          }
          task.state = 'skip';
        } else if (error && error.code === 'canceled') {
          task.state = 'cancel';
        } else {
          task.state = 'fail';
          task.note = (error && error.message) || '上传失败';
        }
      }
      renderFilesTransfers();
    }
    files.uploading = false;
    files.uploadXhr = null;
    renderFilesTransfers();
    updateFilesControls();
    ensureFilesPolling();
    const done = files.uploads.filter((task) => task.state === 'done').length;
    const failed = files.uploads.filter((task) => task.state === 'fail').length;
    if (done || failed) {
      toast(failed ? `上传结束：${done} 个成功，${failed} 个失败。` : `已上传 ${done} 个文件。`, failed ? 'info' : 'success');
      if (files.path) refreshFilesPage({ manual: false });
    }
  }

  function showFilesDrop(show) {
    const drop = $('files-drop');
    drop.hidden = !show;
    $('files-drop-path').textContent = files.path || '—';
  }

  const hasFileDrag = (event) => Boolean(event.dataTransfer && Array.from(event.dataTransfer.types || []).includes('Files'));

  function bindFilesDrag() {
    const view = $('view-files');
    if (!view) return;
    view.addEventListener('dragenter', (event) => {
      if (!authenticated || !hasFileDrag(event) || $('files-main').hidden) return;
      event.preventDefault();
      filesDragDepth += 1;
      showFilesDrop(true);
    });
    view.addEventListener('dragover', (event) => {
      if (!authenticated || !hasFileDrag(event) || $('files-main').hidden) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
    });
    view.addEventListener('dragleave', (event) => {
      if (!hasFileDrag(event)) return;
      filesDragDepth = Math.max(0, filesDragDepth - 1);
      if (!filesDragDepth) showFilesDrop(false);
    });
    view.addEventListener('drop', (event) => {
      if (!hasFileDrag(event) || $('files-main').hidden) return;
      event.preventDefault();
      filesDragDepth = 0;
      showFilesDrop(false);
      const dropped = Array.from(event.dataTransfer.files || []);
      if (dropped.length) queueFilesUpload(dropped);
      else toast('只能拖入文件；文件夹请先打包为压缩包。', 'info');
    });
    document.addEventListener('dragover', (event) => {
      if (!hasFileDrag(event) || $('view-files').contains(event.target)) return;
      event.preventDefault();
    });
    document.addEventListener('drop', (event) => {
      if (!hasFileDrag(event) || $('view-files').contains(event.target)) return;
      event.preventDefault();
    });
  }

  function onFilesViewClick(event) {
    const menuButton = event.target.closest ? event.target.closest('[data-files-menu-button]') : null;
    if (menuButton) {
      event.preventDefault();
      openFilesMenu(menuButton);
    }
  }

  function openFilesForHost(ip) {
    files.requestedHost = str(ip);
    filesInfoRequested = files.requestedHost;
    if (location.hash === '#/files') {
      loadFiles({ configureHost: files.requestedHost });
    } else {
      location.hash = '#/files';
    }
  }

  function resetFilesPanel() {
    stopFilesPolling();
    if (files.uploadXhr) {
      try { files.uploadXhr.abort(); } catch { /* ignore */ }
    }
    files.loaded = false;
    files.info = null;
    files.volumes = [];
    files.path = '';
    files.entries = [];
    files.loading = false;
    files.busy = false;
    files.error = '';
    files.filter = '';
    files.menuTarget = null;
    files.menuTrigger = null;
    files.destMode = '';
    files.destTarget = null;
    files.destPath = '';
    files.destLoading = false;
    files.deleteTarget = null;
    files.uploads = [];
    files.uploading = false;
    files.uploadXhr = null;
    files.transfers = [];
    files.transfersError = '';
    files.job = null;
    files.jobActive = false;
    files.refreshOnJobEnd = false;
    files.drawer = false;
    files.renderedKey = '';
    files.requestedHost = '';
    files.configBusy = false;
    filesInfoRequested = '';
    filesNameMode = '';
    filesNameTarget = null;
    filesOverwritePending = null;
    filesDragDepth = 0;
    setFilesError('');
    setFilesSetupError('');
    $('files-filter').value = '';
    $('files-body').replaceChildren(tableEmptyRow(4, '正在读取文件列表…'));
    $('files-volumes').replaceChildren();
    $('files-crumbs').replaceChildren();
    $('files-transfers-list').replaceChildren();
    nasJobs = [];
    resetNasPanel();
    $('files-transfers-error').textContent = '';
    $('files-transfers-error').hidden = true;
    $('files-drop').hidden = true;
    closeFilesMenu({ restoreFocus: false });
    renderFiles();
    updateFilesControls();
  }



  /* ── 事件绑定 ───────────────────────────────────────────────────────── */


  /* ── NAS 存储 ───────────────────────────────────────────────────────── */

  const NAS_PROTOCOL = { webdav: 'WebDAV', smb: 'SMB', ftp: 'FTP' };
  const NAS_DEFAULT_PORT = { webdav: 5005, smb: 445, ftp: 21 };
  const nasJobsActive = () => nasJobs.some((job) => ['queued', 'scanning', 'running'].includes(job.state));
  const nasActive = () => nas.connections.find((item) => item.id === nas.activeId) || null;

  function setFilesMode(mode) {
    files.mode = mode === 'nas' ? 'nas' : 'ps5';
    $('files-ps5-pane').hidden = files.mode !== 'ps5';
    $('files-nas-pane').hidden = files.mode !== 'nas';
    renderFilesVolumes();
    updateFilesControls();
  }

  function resetNasPanel() {
    Object.assign(nas, { loaded: false, connections: [], activeId: '', path: '/', entries: [], loading: false, error: '', editing: null, busy: false });
    nas.selected.clear();
    files.mode = 'ps5';
    if ($('nas-list')) $('nas-list').replaceChildren();
    if ($('nas-body')) $('nas-body').replaceChildren();
  }

  async function refreshNas() {
    if (!authenticated) return;
    try {
      const data = await api('/api/nas');
      nas.connections = Array.isArray(data.connections) ? data.connections.filter((item) => item && item.id) : [];
      nas.protocols = data.protocols && typeof data.protocols === 'object' ? data.protocols : nas.protocols;
      nas.encryption = data.encryption !== false;
      nas.archive = data.archive !== false;
      nas.loaded = true;
      if (nas.activeId && !nasActive()) setFilesMode('ps5');
    } catch (error) {
      if (error.status === 401) showLogin(error.message);
      nas.loaded = true;
    }
    renderNasRail();
  }

  function renderNasRail() {
    const box = $('nas-list');
    if (!box) return;
    const fragment = document.createDocumentFragment();
    if (nas.loaded && !nas.connections.length) {
      fragment.append(makeElement('p', 'files-rail-empty', '还没有 NAS。添加后可把 NAS 上的文件直接传到 PS5。'));
    }
    for (const item of nas.connections) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'files-vol nas-vol';
      const active = files.mode === 'nas' && item.id === nas.activeId;
      if (active) button.dataset.active = 'true';
      button.setAttribute('aria-pressed', String(active));
      button.setAttribute('aria-label', `打开 NAS ${item.name}（${NAS_PROTOCOL[item.protocol] || item.protocol} ${item.host}）`);
      const top = makeElement('div', 'files-vol-top');
      top.append(makeIcon('i-nas'), makeElement('span', 'files-vol-name', item.name));
      const sub = makeElement('div', 'files-vol-sub');
      sub.append(makeElement('span', 'mono', `${item.host}${item.share ? `/${item.share}` : ''}`), makeElement('span', '', NAS_PROTOCOL[item.protocol] || item.protocol));
      button.append(top, sub);
      if (item.credentials_ok === false) button.append(makeElement('p', 'files-vol-foot nas-warn', '账号无法解密，请编辑后重新填写'));
      button.addEventListener('click', () => openNas(item.id));
      fragment.append(button);
    }
    box.replaceChildren(fragment);
  }

  function openNas(id) {
    if (nas.activeId !== id) {
      nas.activeId = id;
      nas.selected.clear();
      nas.entries = [];
      $('nas-filter').value = '';
    }
    setFilesMode('nas');
    loadNasPath(nas.activeId === id && nas.path && nas.entries.length ? nas.path : '/');
  }

  async function loadNasPath(path) {
    const item = nasActive();
    if (!item || !authenticated) return;
    const seq = ++nas.seq;
    nas.loading = true;
    nas.error = '';
    nas.path = path;
    nas.selected.clear();
    renderNasPane();
    try {
      const data = await api(`/api/nas/list?id=${encodeURIComponent(item.id)}&path=${encodeURIComponent(path)}`);
      if (seq !== nas.seq) return;
      nas.path = str(data.path) || path;
      playEnter($('nas-body'));
      nas.entries = (Array.isArray(data.entries) ? data.entries : []).filter((entry) => entry && entry.name && !isAppleDouble(str(entry.name))).map((entry) => ({
        name: str(entry.name), path: str(entry.path), dir: entry.dir === true,
        size: Number(entry.size) || 0, mtime: Number(entry.mtime) || null
      }));
    } catch (error) {
      if (seq !== nas.seq) return;
      if (error.status === 401) {
        showLogin(error.message);
        return;
      }
      nas.entries = [];
      nas.error = error.message;
    } finally {
      if (seq === nas.seq) {
        nas.loading = false;
        renderNasPane();
      }
    }
  }

  function nasVisibleEntries() {
    const query = $('nas-filter').value.trim().toLowerCase();
    return query ? nas.entries.filter((entry) => entry.name.toLowerCase().includes(query)) : nas.entries;
  }

  function renderNasPane() {
    const item = nasActive();
    if (!item || !$('nas-body')) return;
    const crumbs = document.createDocumentFragment();
    const root = makeElement('button', 'files-crumb', item.name);
    root.type = 'button';
    root.addEventListener('click', () => loadNasPath('/'));
    crumbs.append(root);
    let acc = '';
    const parts = nas.path.split('/').filter(Boolean);
    parts.forEach((part, index) => {
      acc += `/${part}`;
      const target = acc;
      crumbs.append(makeElement('span', 'files-crumb-sep', '/'));
      const crumb = makeElement('button', 'files-crumb', part);
      crumb.type = 'button';
      if (index === parts.length - 1) crumb.setAttribute('aria-current', 'page');
      crumb.addEventListener('click', () => loadNasPath(target));
      crumbs.append(crumb);
    });
    if (!parts.length) root.setAttribute('aria-current', 'page');
    $('nas-crumbs').replaceChildren(crumbs);

    const errorNode = $('nas-error');
    errorNode.textContent = nas.error;
    errorNode.hidden = !nas.error;
    const body = $('nas-body');
    const visible = nasVisibleEntries();
    if (nas.loading) body.replaceChildren(tableEmptyRow(4, '正在读取 NAS…'));
    else if (nas.error) body.replaceChildren(tableEmptyRow(4, '读取失败，请检查连接设置后重试。'));
    else if (!nas.entries.length) body.replaceChildren(tableEmptyRow(4, '这个文件夹是空的。'));
    else if (!visible.length) body.replaceChildren(tableEmptyRow(4, '没有匹配的文件。'));
    else body.replaceChildren(...visible.map(nasRow));
    renderNasStatus();
    updateNasControls();
  }

  function renderNasStatus() {
    const plain = nas.entries.filter((entry) => !entry.dir);
    const total = plain.reduce((sum, entry) => sum + entry.size, 0);
    const chosen = nas.entries.filter((entry) => nas.selected.has(entry.path));
    const chosenBytes = chosen.reduce((sum, entry) => sum + (entry.dir ? 0 : entry.size), 0);
    let text = nas.loading ? '' : `${nas.entries.length - plain.length} 个文件夹 · ${plain.length} 个文件 · 合计 ${formatBytes(total)}`;
    if (!nas.loading && chosen.length) {
      text += ` · 已选 ${chosen.length} 项`;
      if (chosenBytes) text += `（文件 ${formatBytes(chosenBytes)}${chosen.some((entry) => entry.dir) ? '，另含文件夹' : ''}）`;
    }
    $('nas-status').textContent = text;
  }

  function nasRow(entry) {
    const row = document.createElement('tr');
    row.dataset.dir = String(entry.dir);
    // Phones: tapping a file row toggles its checkbox (folders open instead).
    row.addEventListener('click', (event) => {
      if (entry.dir || !isPhone() || event.target.closest('button, input, label')) return;
      box.click();
    });
    const check = makeElement('td', 'check-cell');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = nas.selected.has(entry.path);
    box.setAttribute('aria-label', `选择 ${entry.name}`);
    box.addEventListener('change', () => {
      if (box.checked) nas.selected.add(entry.path);
      else nas.selected.delete(entry.path);
      renderNasStatus();
      updateNasControls();
    });
    check.append(box);
    const nameCell = document.createElement('td');
    const wrap = makeElement('div', 'files-name');
    const icon = makeIcon(entry.dir ? 'i-folder' : 'i-file');
    if (entry.dir) icon.classList.add('folder');
    wrap.append(icon);
    if (entry.dir) {
      const open = makeElement('button', 'files-name-button', entry.name);
      open.type = 'button';
      open.setAttribute('aria-label', `打开文件夹 ${entry.name}`);
      open.addEventListener('click', () => loadNasPath(entry.path));
      wrap.append(withTitleLabel(open, entry.name));
    } else {
      wrap.append(withTitleLabel(makeElement('span', 'files-name-text', entry.name), entry.name));
    }
    nameCell.append(wrap);
    row.append(check, nameCell,
      makeElement('td', 'num files-size-col', entry.dir ? '—' : formatBytes(entry.size)),
      makeElement('td', 'num files-time-col', formatFileTime(entry.mtime)));
    row.addEventListener('dblclick', (event) => {
      if (!event.target.closest('button, input') && entry.dir) loadNasPath(entry.path);
    });
    return row;
  }

  function updateNasControls() {
    if (!$('nas-send')) return;
    const locked = !authenticated || nas.loading || nas.busy;
    $('nas-send').disabled = locked || !nas.selected.size || !(files.info && files.info.reachable);
    $('nas-send').title = files.info && files.info.reachable ? '' : '需要先连接 PS5 文件服务';
    const archive = nasArchiveSelection();
    $('nas-extract').disabled = locked || !archive || !(files.info && files.info.reachable) || nas.archive === false;
    $('nas-extract').title = nas.archive === false ? '服务器没有安装解压库（python3-libarchive-c）'
      : !(files.info && files.info.reachable) ? '需要先连接 PS5 文件服务'
        : archive ? '' : '选中一个 .rar 或 .partNN.rar 分卷后可以解压安装';
    $('nas-refresh').disabled = locked;
    $('nas-edit').disabled = !authenticated || !nasActive();
    $('nas-add').disabled = !authenticated;
    const all = $('nas-select-all');
    const visible = nasVisibleEntries();
    const chosen = visible.filter((entry) => nas.selected.has(entry.path)).length;
    all.disabled = locked || !visible.length;
    all.checked = visible.length > 0 && chosen === visible.length;
    all.indeterminate = chosen > 0 && chosen < visible.length;
  }

  function openNasSendDialog() {
    if (!authenticated || !nas.selected.size || !(files.info && files.info.reachable)) return;
    const paths = [...nas.selected];
    const item = nasActive();
    files.destMode = 'nas';
    const chosen = nas.entries.filter((entry) => nas.selected.has(entry.path));
    files.destTarget = {
      name: paths.length === 1 ? paths[0].split('/').pop() : `${paths.length} 项`, paths, nasId: item.id,
      bytes: chosen.reduce((sum, entry) => sum + (entry.dir ? 0 : entry.size), 0),
      hasFolders: chosen.some((entry) => entry.dir)
    };
    files.destPath = files.path;
    $('files-dest-title').textContent = '传到 PS5…';
    $('files-dest-description').textContent = `把 ${item.name} 上选中的 ${paths.length} 项（文件夹会连同内容）传到所选的 PS5 文件夹。传输由服务器执行，可以关闭页面。`;
    $('files-dest-confirm').querySelector('.button-label').textContent = '开始传输';
    $('files-dest-confirm').setAttribute('aria-label', '开始把 NAS 文件传到所选的 PS5 文件夹');
    $('files-dest-overwrite-row').hidden = false;
    $('files-dest-extract-row').hidden = true;
    $('files-dest-archive').hidden = true;
    $('files-dest-error').textContent = '';
    $('files-dest-error').hidden = true;
    $('files-dest-cancel').disabled = false;
    $('files-dest-confirm').disabled = false;
    $('files-dest-dialog').showModal();
    $('files-dest-cancel').focus({ preventScroll: true });
    loadFilesDest(files.path);
  }

  const NAS_ARCHIVE = /\.rar$/i;

  function nasArchiveSelection() {
    if (nas.selected.size !== 1) return null;
    const [path] = nas.selected;
    const entry = nas.entries.find((item) => item.path === path);
    // macOS leaves ._name metadata files next to every volume; they are not archives.
    return entry && !entry.dir && NAS_ARCHIVE.test(entry.name) && !entry.name.startsWith('._') ? entry : null;
  }

  function describeNasExtract() {
    const target = files.destTarget;
    if (files.destMode !== 'extract' || !target) return;
    const merge = $('files-dest-extract-mode').value === 'merge';
    const game = target.info && target.info.title ? target.info.title : '游戏';
    $('files-dest-description').textContent = merge
      ? `把压缩包里的文件覆盖进所选的游戏文件夹（例如 ${game}），用于补丁或 DLC。全部解压并校验通过后才会替换，失败不会改动原游戏。`
      : `在所选文件夹（例如 /mnt/ext0/games）下新建以游戏编号命名的文件夹。服务器边读 NAS 边解压，直接写进 PS5，不占服务器磁盘；可以关闭页面。`;
  }

  async function openNasExtractDialog() {
    const entry = nasArchiveSelection();
    const item = nasActive();
    if (!authenticated || !entry || !item || !(files.info && files.info.reachable)) return;
    files.destMode = 'extract';
    files.destTarget = { name: entry.name, path: entry.path, nasId: item.id, bytes: null, info: null };
    files.destPath = files.path;
    $('files-dest-title').textContent = '解压安装到 PS5…';
    $('files-dest-confirm').querySelector('.button-label').textContent = '开始解压安装';
    $('files-dest-confirm').setAttribute('aria-label', '开始把压缩包解压安装到所选的 PS5 文件夹');
    $('files-dest-overwrite-row').hidden = true;
    $('files-dest-extract-row').hidden = false;
    $('files-dest-extract-mode').value = 'install';
    describeNasExtract();
    const info = $('files-dest-archive');
    info.textContent = '正在检查压缩包…';
    info.dataset.tone = '';
    info.hidden = false;
    $('files-dest-error').textContent = '';
    $('files-dest-error').hidden = true;
    $('files-dest-cancel').disabled = false;
    $('files-dest-confirm').disabled = true;
    $('files-dest-dialog').showModal();
    $('files-dest-cancel').focus({ preventScroll: true });
    loadFilesDest(files.path);
    const target = files.destTarget;
    try {
      const data = await api(`/api/nas/archive?id=${encodeURIComponent(item.id)}&path=${encodeURIComponent(entry.path)}`);
      if (files.destTarget !== target) return;
      target.info = data;
      target.bytes = Number(data.bytes) || 0;
      target.unpacked = Number(data.unpacked) || 0;
      const parts = [`${data.format || 'RAR5'}`, data.parts > 1 ? `${data.parts} 个分卷` : '单个压缩包', `共 ${formatBytes(target.bytes)}`];
      if (target.unpacked) parts.push(`解压后 ${formatBytes(target.unpacked)}`);
      if (data.files) parts.push(`${data.files} 个文件`);
      if (data.title) parts.push(`游戏编号 ${data.title}`);
      if (data.engine === 'unrar') parts.push(`unrar 引擎${data.memory ? `（约需 ${formatBytes(data.memory)} 内存）` : ''}`);
      info.textContent = parts.join(' · ');
      $('files-dest-confirm').disabled = false;
      describeNasExtract();
      renderFilesDestSpace();
    } catch (error) {
      if (files.destTarget !== target) return;
      if (error.status === 401) {
        $('files-dest-dialog').close();
        showLogin(error.message);
        return;
      }
      info.hidden = true;
      $('files-dest-error').textContent = error.message;
      $('files-dest-error').hidden = false;
    }
  }

  async function startNasExtract() {
    const target = files.destTarget;
    if (!target || !target.info || !authenticated || files.busy) return;
    if (!files.destPath) {
      $('files-dest-error').textContent = '请选择 PS5 上的目标文件夹。';
      $('files-dest-error').hidden = false;
      return;
    }
    files.busy = true;
    buttonBusy('files-dest-confirm', true, '提交中…');
    try {
      const result = await api('/api/nas/extract', {
        id: target.nasId, path: target.path, dest: files.destPath,
        mode: $('files-dest-extract-mode').value === 'merge' ? 'merge' : 'install'
      });
      if (result.job && result.job.id) nasJobs = [result.job, ...nasJobs.filter((job) => job.id !== result.job.id)];
      $('files-dest-dialog').close();
      nas.selected.clear();
      renderNasPane();
      toast(`已开始解压安装 ${target.info.base || target.name} → ${files.destPath}。`, 'success');
      files.drawer = true;
      renderFilesTransfers();
      ensureFilesPolling();
      revealFilesTransfers();
    } catch (error) {
      if (error.status === 401) {
        $('files-dest-dialog').close();
        showLogin(error.message);
        return;
      }
      $('files-dest-error').textContent = error.message;
      $('files-dest-error').hidden = false;
    } finally {
      files.busy = false;
      buttonBusy('files-dest-confirm', false, '开始解压安装');
    }
  }

  function extractTask(job) {
    const done = job.state === 'done' || job.state === 'done_with_warnings';
    let line = '';
    if (job.files_done) line = job.files_total > job.files_done ? `已解出 ${job.files_done} / ${job.files_total} 个文件` : `已解出 ${job.files_done} 个文件`;
    if (job.state === 'running' && job.current) line += `${line ? ' · ' : ''}当前 ${job.current}`;
    if (done && job.note) line += `${line ? ' · ' : ''}${job.note}`;
    // The hidden staging folder stays behind when the service restarted mid-way.
    if (job.interrupted && job.staging) line += `${line ? ' · ' : ''}可在 PS5 文件页手动删除临时文件夹 ${job.staging}`;
    return {
      key: `nas:${job.id}`, kind: 'extract', name: job.name, state: job.state,
      sub: done && job.result_path ? `已安装到 ${job.result_path}` : `${job.source} → ${job.dest}${job.mode === 'merge' ? '（合并）' : ''}`,
      done: job.bytes_done, total: job.bytes_total, speed: job.speed, elapsed: job.elapsed, eta: job.eta,
      line, error: job.error, errors: job.errors || [],
      interrupted: job.interrupted === true,
      cancel: XFER_ACTIVE.has(job.state) ? () => cancelNasJob(job.id) : null
    };
  }

  async function startNasTransfer() {
    const target = files.destTarget;
    if (!target || !target.paths || !authenticated || files.busy) return;
    if (!files.destPath) {
      $('files-dest-error').textContent = '请选择 PS5 上的目标文件夹。';
      $('files-dest-error').hidden = false;
      return;
    }
    files.busy = true;
    buttonBusy('files-dest-confirm', true, '提交中…');
    try {
      const result = await api('/api/nas/transfer', {
        id: target.nasId, paths: target.paths, dest: files.destPath,
        overwrite: $('files-dest-overwrite').value === 'overwrite' ? 'overwrite' : 'skip',
        auto_register: Boolean(target.hasFolders && titleIdOf(target.name || '')), title: titleIdOf(target.name || '') || ''
      });
      if (result.job && result.job.id) nasJobs = [result.job, ...nasJobs.filter((job) => job.id !== result.job.id)];
      $('files-dest-dialog').close();
      nas.selected.clear();
      renderNasPane();
      toast(`已开始传输 ${target.name} → ${files.destPath}。`, 'success');
      files.drawer = true;
      renderFilesTransfers();
      ensureFilesPolling();
      revealFilesTransfers();
    } catch (error) {
      if (error.status === 401) {
        $('files-dest-dialog').close();
        showLogin(error.message);
        return;
      }
      $('files-dest-error').textContent = error.message;
      $('files-dest-error').hidden = false;
    } finally {
      files.busy = false;
      buttonBusy('files-dest-confirm', false, '开始传输');
    }
  }

  async function refreshNasJobs() {
    // Jobs run on the server: pick up ones started before this page loaded.
    if (!authenticated) return;
    try {
      const data = await api('/api/transfers');
      nasJobs = Array.isArray(data.jobs) ? data.jobs.filter((job) => job && typeof job === 'object' && job.id) : [];
      renderFilesTransfers();
      ensureFilesPolling();
    } catch (error) {
      if (error.status === 401) showLogin(error.message);
    }
  }

  async function retryNasJob(id) {
    try {
      const result = await api('/api/transfers/retry', { id });
      if (result.job && result.job.id) nasJobs = [result.job, ...nasJobs.filter((job) => job.id !== result.job.id)];
      toast('已重新排队，PS5 上已有的同名文件会跳过。', 'success');
      renderFilesTransfers();
      ensureFilesPolling();
    } catch (error) {
      if (error.status === 401) {
        showLogin(error.message);
        return;
      }
      toast(error.message, 'error');
    }
  }

  async function cancelNasJob(id) {
    try {
      await api('/api/transfers/cancel', { id });
      toast('已请求取消传输。', 'info');
    } catch (error) {
      if (error.status === 401) {
        showLogin(error.message);
        return;
      }
      toast(error.message, 'error');
    }
    filesTransfersTick();
  }

  function syncNasDialogProtocol() {
    const protocol = $('nas-protocol').value;
    $('nas-share-group').hidden = protocol !== 'smb';
    $('nas-workgroup-group').hidden = protocol !== 'smb';
    $('nas-tls-group').hidden = protocol === 'smb';
    $('nas-tls-label').textContent = protocol === 'ftp' ? '使用 FTPS（显式 TLS）' : '使用 HTTPS';
    $('nas-port').placeholder = String(NAS_DEFAULT_PORT[protocol] || '');
    const hints = {
      smb: '群晖/威联通/Windows 共享常用 445 端口；共享名留空会先列出所有共享。',
      webdav: '群晖 WebDAV 默认 HTTP 5005 / HTTPS 5006；起始路径可填 /video 等子目录。',
      ftp: '如果 NAS 只开放 FTP，可用此方式；匿名访问时用户名和密码留空。'
    };
    let hint = hints[protocol] || '';
    if (protocol === 'smb' && nas.protocols.smb === false) hint = '服务器缺少 SMB 支持：请在本服务主机执行 apt install python3-smbc 后重启服务。';
    if (nas.encryption === false) hint = '服务器缺少加密组件（python3-cryptography），为保证账号加密保存，暂时无法保存带账号的连接。';
    $('nas-hint').textContent = hint;
  }

  function nasDialogValues() {
    const port = $('nas-port').value.trim();
    return {
      id: nas.editing ? nas.editing.id : undefined,
      name: $('nas-name').value.trim(),
      protocol: $('nas-protocol').value,
      host: $('nas-host').value.trim(),
      port: port ? Number(port) : null,
      share: $('nas-share').value.trim(),
      path: $('nas-path').value.trim() || '/',
      username: $('nas-username').value,
      password: $('nas-password').value,
      keep_password: true,
      workgroup: $('nas-workgroup').value.trim(),
      tls: $('nas-tls').checked,
      verify_tls: $('nas-verify').checked
    };
  }

  function openNasDialog(item) {
    if (!authenticated) return;
    nas.editing = item || null;
    $('nas-form').reset();
    $('nas-dialog-title').textContent = item ? `编辑 ${item.name}` : '添加 NAS 存储';
    $('nas-name').value = item ? item.name : '';
    $('nas-protocol').value = item ? item.protocol : 'smb';
    $('nas-host').value = item ? item.host : '';
    $('nas-port').value = item ? String(item.port || '') : '';
    $('nas-share').value = item ? item.share || '' : '';
    $('nas-path').value = item ? item.path || '/' : '/';
    $('nas-username').value = item ? item.username || '' : '';
    $('nas-password').placeholder = item && item.has_password ? '已加密保存，留空则保持不变' : '';
    $('nas-workgroup').value = item ? item.workgroup || '' : '';
    $('nas-tls').checked = Boolean(item && item.tls);
    $('nas-verify').checked = Boolean(item && item.verify_tls);
    $('nas-delete').hidden = !item;
    $('nas-dialog-status').textContent = '';
    $('nas-dialog-error').hidden = true;
    syncNasDialogProtocol();
    $('nas-dialog').showModal();
    $('nas-name').focus({ preventScroll: true });
  }

  function nasDialogError(message) {
    $('nas-dialog-error').textContent = message || '';
    $('nas-dialog-error').hidden = !message;
  }

  async function testNasDialog() {
    if (nas.busy) return;
    nas.busy = true;
    nasDialogError('');
    $('nas-dialog-status').textContent = '正在连接 NAS…';
    buttonBusy('nas-test', true, '测试中…');
    try {
      const result = await api('/api/nas/test', nasDialogValues());
      $('nas-dialog-status').textContent = `连接成功，起始路径下有 ${result.entries} 项。`;
    } catch (error) {
      if (error.status === 401) {
        $('nas-dialog').close();
        showLogin(error.message);
        return;
      }
      $('nas-dialog-status').textContent = '';
      nasDialogError(error.message);
    } finally {
      nas.busy = false;
      buttonBusy('nas-test', false, '测试连接');
    }
  }

  async function submitNasDialog(event) {
    event.preventDefault();
    if (nas.busy) return;
    const values = nasDialogValues();
    if (!values.host) {
      nasDialogError('请填写 NAS 地址。');
      $('nas-host').focus();
      return;
    }
    nas.busy = true;
    nasDialogError('');
    buttonBusy('nas-save', true, '保存中…');
    try {
      const result = await api('/api/nas/save', values);
      $('nas-dialog').close();
      toast(`已保存 ${result.connection.name}。`, 'success');
      await refreshNas();
      openNas(result.connection.id);
    } catch (error) {
      if (error.status === 401) {
        $('nas-dialog').close();
        showLogin(error.message);
        return;
      }
      nasDialogError(error.message);
    } finally {
      nas.busy = false;
      buttonBusy('nas-save', false, '保存');
    }
  }

  async function deleteNasDialog() {
    const item = nas.editing;
    if (!item || nas.busy) return;
    if (!window.confirm(`删除 NAS 连接「${item.name}」？NAS 上的文件不受影响。`)) return;
    nas.busy = true;
    try {
      await api('/api/nas/delete', { id: item.id });
      $('nas-dialog').close();
      toast(`已删除 ${item.name}。`, 'success');
      if (nas.activeId === item.id) {
        nas.activeId = '';
        setFilesMode('ps5');
      }
      await refreshNas();
    } catch (error) {
      nasDialogError(error.message);
    } finally {
      nas.busy = false;
    }
  }

  function bindNasEvents() {
    $('nas-add').addEventListener('click', () => openNasDialog(null));
    $('nas-edit').addEventListener('click', () => openNasDialog(nasActive()));
    $('nas-refresh').addEventListener('click', () => loadNasPath(nas.path));
    $('nas-send').addEventListener('click', openNasSendDialog);
    $('nas-extract').addEventListener('click', openNasExtractDialog);
    $('files-dest-extract-mode').addEventListener('change', describeNasExtract);
    $('nas-filter').addEventListener('input', renderNasPane);
    $('nas-select-all').addEventListener('change', () => {
      const visible = nasVisibleEntries();
      if ($('nas-select-all').checked) for (const entry of visible) nas.selected.add(entry.path);
      else for (const entry of visible) nas.selected.delete(entry.path);
      for (const box of $$('#nas-body input[type="checkbox"]')) box.checked = $('nas-select-all').checked;
      renderNasStatus();
      updateNasControls();
    });
    $('nas-protocol').addEventListener('change', syncNasDialogProtocol);
    $('nas-form').addEventListener('submit', submitNasDialog);
    $('nas-test').addEventListener('click', testNasDialog);
    $('nas-cancel').addEventListener('click', () => $('nas-dialog').close());
    $('nas-delete').addEventListener('click', deleteNasDialog);
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

  function bindEvents() {
    for (const dialog of $$('dialog')) {
      animateDialogClose(dialog);
      bindSheetSwipe(dialog);
    }
    $('action-sheet-cancel').addEventListener('click', () => $('action-sheet').close());
    $('action-sheet').addEventListener('close', onActionSheetClosed);
    // Tap on the dimmed area above a phone sheet closes it, as native sheets do.
    $('action-sheet').addEventListener('click', (event) => {
      if (event.target === $('action-sheet')) $('action-sheet').close();
    });
    $('login-form').addEventListener('submit', submitLogin);
    $('mobile-more-trigger').addEventListener('click', openMobileMore);
    $('mobile-more-close').addEventListener('click', () => closeMobileMore());
    $('mobile-more-dialog').addEventListener('close', restoreMobileMoreFocus);
    for (const link of $$('.more-link[data-section]')) {
      link.addEventListener('click', () => {
        closeMobileMore('navigation');
        if (sectionFromHash() === link.dataset.section) showSection(link.dataset.section, { moveFocus: true });
      });
    }
    window.matchMedia('(max-width: 767px)').addEventListener('change', (event) => {
      if (event.matches) return;
      closeMobileMore('resize');
      // The action sheet is phone-only; wider layouts show the inline controls.
      if ($('action-sheet').open) $('action-sheet').close();
    });
    $('repo-search').addEventListener('input', () => {
      repoRendered = '';
      renderRepo();
    });
    $('activity-search').addEventListener('input', () => {
      renderActivityRows();
      updateActivityControls();
      flushActivityPending();
    });
    $('ps5-catalog-search').addEventListener('input', () => {
      ps5CatalogKey = '';
      const host = ps5SelectedHost();
      if (host) renderPs5Catalog(host);
    });

    for (const field of fields) {
      field.addEventListener('input', () => {
        updateControls();
        if (field.hasAttribute('aria-invalid')) clearErrors();
      });
    }

    $('config-form').addEventListener('submit', (event) => {
      event.preventDefault();
      saveConfig();
    });
    for (const button of acts('save')) {
      if (button.type === 'button') button.addEventListener('click', saveConfig);
    }
    for (const button of acts('rollback')) button.addEventListener('click', openRollbackDialog);
    $('rollback-cancel').addEventListener('click', () => $('rollback-dialog').close());
    $('rollback-confirm').addEventListener('click', confirmRollback);

    for (const button of acts('logout')) button.addEventListener('click', logout);
    for (const button of acts('refresh')) button.addEventListener('click', handleRefresh);
    for (const button of acts('upload')) button.addEventListener('click', () => openUploadDialog());
    $('repo-copy-url').addEventListener('click', copyRepoUrl);

    $('update-check').addEventListener('click', checkUpdates);
    $('update-apply').addEventListener('click', applyUpdate);
    $('update-retry').addEventListener('click', retryUpdateConnection);
    $('update-reload').addEventListener('click', () => location.reload());
    $('password-form').addEventListener('submit', submitPassword);
    for (const [field, error] of [['current-password', 'current-password-error'], ['new-password', 'new-password-error'], ['confirm-password', 'confirm-password-error']]) {
      $(field).addEventListener('input', () => {
        if ($(field).hasAttribute('aria-invalid')) {
          $(field).removeAttribute('aria-invalid');
          $(error).textContent = '';
          $(error).hidden = true;
        }
        if (!$('password-form-error').hidden) {
          $('password-form-error').textContent = '';
          $('password-form-error').hidden = true;
        }
        if (!$('password-success').hidden) {
          $('password-success').textContent = '';
          $('password-success').hidden = true;
        }
      });
    }

    bindUploadAutofill();
    $('repo-upload-form').addEventListener('submit', submitUpload);
    $('repo-upload-cancel').addEventListener('click', closeUploadDialog);
    for (const [field, error] of [['repo-upload-file', 'repo-upload-file-error'], ['repo-upload-name', 'repo-upload-name-error'], ['repo-upload-version', 'repo-upload-version-error'], ['repo-upload-category', 'repo-upload-category-error'], ['repo-upload-author', 'repo-upload-author-error'], ['repo-upload-description', 'repo-upload-description-error']]) {
      const clear = () => {
        $(field).removeAttribute('aria-invalid');
        $(error).textContent = '';
        $(error).hidden = true;
        $('repo-upload-error').textContent = '';
        $('repo-upload-error').hidden = true;
      };
      $(field).addEventListener('input', clear);
      $(field).addEventListener('change', clear);
    }

    $('repo-edit-form').addEventListener('submit', submitEdit);
    $('repo-edit-cancel').addEventListener('click', () => $('repo-edit-dialog').close());
    $('repo-edit-dialog').addEventListener('cancel', (event) => { if (repoBusy) event.preventDefault(); });
    $('repo-edit-dialog').addEventListener('close', () => { editTarget = null; flushRepoPending(); });

    $('repo-delete-cancel').addEventListener('click', () => $('repo-delete-dialog').close());
    $('repo-delete-confirm').addEventListener('click', confirmDelete);
    $('repo-delete-dialog').addEventListener('cancel', (event) => { if (repoBusy) event.preventDefault(); });
    $('repo-delete-dialog').addEventListener('close', () => { deleteTarget = null; flushRepoPending(); });

    $('repo-upload-dialog').addEventListener('cancel', (event) => { if (uploading) event.preventDefault(); });
    $('repo-upload-dialog').addEventListener('close', () => { flushRepoPending(); });
    $('repo-upstream-check').addEventListener('click', checkUpstream);
    $('repo-upstream-all').addEventListener('click', applyAllUpstream);
    $('repo-versions-close').addEventListener('click', () => $('repo-versions-dialog').close());
    $('repo-versions-dialog').addEventListener('cancel', (event) => { if (repoBusy) event.preventDefault(); });
    $('repo-versions-dialog').addEventListener('close', () => { versionsTarget = null; flushRepoPending(); });

    for (const button of acts('allow')) button.addEventListener('click', openActivityAllowDialog);
    for (const button of acts('label')) button.addEventListener('click', openActivityLabelDialog);
    $('activity-filter').addEventListener('change', () => setActivityFilter($('activity-filter').value));
    $('activity-kind').addEventListener('change', () => setActivityKind($('activity-kind').value));
    try { setActivityKind(localStorage.getItem('ps5jb.activityKind') || 'all'); } catch { /* private mode */ }
    for (const segment of $$('.segment')) segment.addEventListener('click', () => setActivityFilter(segment.dataset.filter));

    $('activity-select-all').addEventListener('change', () => {
      applySelectAll($('activity-select-all').checked);
    });
    $('activity-select-all-mobile').addEventListener('click', () => {
      const targets = collectVisibleTargets();
      const allSelected = targets.length > 0 && targets.every((target) => activitySelected.has(target));
      applySelectAll(!allSelected);
    });

    $('activity-allow-cancel').addEventListener('click', () => $('activity-allow-dialog').close());
    $('activity-allow-confirm').addEventListener('click', confirmActivityAllow);
    $('activity-allow-dialog').addEventListener('cancel', (event) => { if (activityBusy) event.preventDefault(); });
    $('activity-allow-dialog').addEventListener('close', () => { flushActivityPending(); });

    $('activity-label-form').addEventListener('submit', submitActivityLabels);
    $('activity-label-cancel').addEventListener('click', () => $('activity-label-dialog').close());
    $('activity-label-dialog').addEventListener('cancel', (event) => { if (activityBusy) event.preventDefault(); });
    $('activity-label-dialog').addEventListener('close', () => { flushActivityPending(); });

    // PS5 主机
    $('ps5-scan').addEventListener('click', () => scanPs5({ manual: true }));
    $('ps5-add-form').addEventListener('submit', submitPs5Add);
    $('ps5-add-ip').addEventListener('input', () => {
      if ($('ps5-add-ip').hasAttribute('aria-invalid')) setPs5AddError('');
    });
    $('view-ps5').addEventListener('click', onPs5ViewClick);
    bindPs5PowerEvents();
    $('ps5-tabs').addEventListener('keydown', onPs5TabsKeydown);
    $('ps5-autoload-enabled').addEventListener('change', onPs5AutoloadToggle);
    $('ps5-autoload-items').addEventListener('change', onPs5AutoloadFile);
    $('ps5-autoload-items').addEventListener('click', onPs5AutoloadClick);
    $('ps5-autoload-start-delay').addEventListener('change', onPs5AutoloadStartDelay);
    $('ps5-autoload-add').addEventListener('click', onPs5AutoloadAdd);
    $('ps5-autoload-save').addEventListener('click', requestPs5AutoloadSave);
    $('ps5-shutdown-cancel').addEventListener('click', () => $('ps5-shutdown-dialog').close());
    $('ps5-shutdown-confirm').addEventListener('click', confirmPs5Shutdown);
    $('ps5-kill-cancel').addEventListener('click', () => $('ps5-kill-dialog').close());
    $('ps5-kill-confirm').addEventListener('click', confirmPs5Kill);
    $('ps5-autoload-dialog-cancel').addEventListener('click', () => $('ps5-autoload-dialog').close());
    $('ps5-autoload-dialog-confirm').addEventListener('click', () => {
      const target = ps5AutoloadTarget;
      if (target) submitPs5Autoload(target);
    });
    for (const id of ['ps5-shutdown-dialog', 'ps5-kill-dialog', 'ps5-autoload-dialog']) {
      const dialog = $(id);
      dialog.addEventListener('cancel', (event) => { if (ps5Busy) event.preventDefault(); });
      dialog.addEventListener('close', () => {
        ps5ShutdownIp = '';
        ps5KillTarget = null;
        ps5AutoloadTarget = null;
        flushPs5Pending();
        ps5RestoreFocus();
      });
    }

    // PS5 文件
    $('view-files').addEventListener('click', onFilesViewClick);
    $('files-upload').addEventListener('click', () => $('files-upload-input').click());
    $('files-upload-input').addEventListener('change', (event) => {
      const list = event.target.files ? Array.from(event.target.files) : [];
      event.target.value = '';
      if (list.length) queueFilesUpload(list);
    });
    $('files-mkdir').addEventListener('click', () => openFilesNameDialog('mkdir', null));
    $('files-refresh').addEventListener('click', () => refreshFilesPage({ manual: true }));
    for (const button of $$('[data-files-transfers]')) button.addEventListener('click', () => toggleFilesDrawer());
    $('xfer-pill').addEventListener('click', () => toggleFilesDrawer());
    document.addEventListener('pointerdown', (event) => {
      if (!files.drawer || dialogsOpen()) return;
      if (event.target.closest('#xfer-dock, [data-files-transfers], #nas-send, #nas-extract, #files-upload, dialog')) return;
      toggleFilesDrawer(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && files.drawer && !dialogsOpen()) {
        toggleFilesDrawer(false);
        $('xfer-pill').focus({ preventScroll: true });
      }
    });
    $('files-transfers-clear').addEventListener('click', clearFilesTransfers);
    bindNasEvents();
    $('files-transfers-close').addEventListener('click', () => toggleFilesDrawer(false));
    $('files-setup-connect').addEventListener('click', connectFilesSetup);
    $('files-setup-host').addEventListener('change', () => setFilesSetupError(''));
    $('files-retry').addEventListener('click', () => {
      setFilesError('');
      loadFiles({ manual: true });
    });
    $('files-switch-host').addEventListener('click', openFilesHostDialog);
    $('files-filter').addEventListener('input', () => {
      files.filter = $('files-filter').value;
      renderFilesRows();
      renderFilesStatus();
    });
    $('files-name-form').addEventListener('submit', submitFilesName);
    $('files-name-cancel').addEventListener('click', () => $('files-name-dialog').close());
    $('files-name-input').addEventListener('input', () => {
      $('files-name-input').removeAttribute('aria-invalid');
      $('files-name-error').textContent = '';
      $('files-name-error').hidden = true;
    });
    $('files-delete-cancel').addEventListener('click', () => $('files-delete-dialog').close());
    $('files-delete-confirm').addEventListener('click', confirmFilesDelete);
    $('files-overwrite-cancel').addEventListener('click', () => settleFilesOverwrite('cancel'));
    $('files-overwrite-skip').addEventListener('click', () => settleFilesOverwrite('skip'));
    $('files-overwrite-confirm').addEventListener('click', () => settleFilesOverwrite('overwrite'));
    $('files-dest-cancel').addEventListener('click', () => $('files-dest-dialog').close());
    $('files-dest-confirm').addEventListener('click', confirmFilesDest);
    $('files-dest-root').addEventListener('click', () => loadFilesDest(''));
    $('files-dest-up').addEventListener('click', () => {
      const volume = filesVolumeOf(files.destPath);
      if (!files.destPath || !volume || files.destPath === volume.path) loadFilesDest('');
      else loadFilesDest(filesParent(files.destPath));
    });
    $('files-host-cancel').addEventListener('click', () => $('files-host-dialog').close());
    $('files-host-confirm').addEventListener('click', () => {
      const host = $('files-host-select').value;
      if (!host) {
        $('files-host-error').textContent = '请选择主机。';
        $('files-host-error').hidden = false;
        return;
      }
      configureFilesHost(host, { closeDialog: true });
    });
    $('files-menu').addEventListener('click', onFilesMenuAction);
    document.addEventListener('click', (event) => {
      if (event.target.closest && (event.target.closest('#files-menu') || event.target.closest('[data-files-menu-button]'))) return;
      closeFilesMenu();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (!$('files-menu').hidden) {
        event.preventDefault();
        closeFilesMenu();
      }
    });
    $('files-name-dialog').addEventListener('cancel', (event) => { if (files.busy) event.preventDefault(); });
    $('files-delete-dialog').addEventListener('cancel', (event) => { if (files.busy) event.preventDefault(); });
    $('files-dest-dialog').addEventListener('cancel', (event) => { if (files.busy) event.preventDefault(); });
    $('files-host-dialog').addEventListener('cancel', (event) => { if (files.configBusy) event.preventDefault(); });
    $('files-delete-dialog').addEventListener('close', () => { files.deleteTarget = null; });
    $('files-overwrite-dialog').addEventListener('close', () => {
      if (!filesOverwritePending) return;
      const pending = filesOverwritePending;
      filesOverwritePending = null;
      pending.onChoice('cancel');
    });
    bindFilesDrag();

    for (const dialog of $$('dialog.dialog')) {
      dialog.addEventListener('keydown', (event) => trapDialogFocus(event, dialog));
      dialog.addEventListener('click', (event) => {
        if (event.target !== dialog) return;
        if (dialog === $('repo-edit-dialog') && repoBusy) return;
        if (dialog === $('repo-delete-dialog') && repoBusy) return;
        if (dialog === $('repo-upload-dialog') && uploading) return;
        if (dialog === $('activity-allow-dialog') && activityBusy) return;
        if (dialog === $('activity-label-dialog') && activityBusy) return;
        if (dialog === $('ps5-shutdown-dialog') && ps5Busy) return;
        if (dialog === $('ps5-kill-dialog') && ps5Busy) return;
        if (dialog === $('ps5-autoload-dialog') && ps5Busy) return;
        if (dialog === $('files-name-dialog') && files.busy) return;
        if (dialog === $('files-delete-dialog') && files.busy) return;
        if (dialog === $('files-dest-dialog') && files.busy) return;
        if (dialog === $('files-host-dialog') && files.configBusy) return;
        dialog.close();
      });
    }

    window.addEventListener('hashchange', () => ensureRoute({ moveFocus: true }));
    window.addEventListener('beforeunload', (event) => {
      if (!dirty && !uploading && !files.uploading) return;
      event.preventDefault();
      event.returnValue = '';
    });
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

  /* ── 进度展示（服务更新 / 插件上游 / 主机安装共用） ─────────────────── */

  let ps5Progress = null;          // 当前主机安装 / 更新的服务端阶段
  let ps5Batch = null;             // 批量更新：{ index, total, name }
  let ps5UpgradeTarget = null;     // 更新确认对话框的目标
  let upstreamBatch = null;        // 插件上游更新：{ index, total, name, version, progress }

  const PS5_STAGES = { checking: '检查主机状态', stopping: '停止旧进程', reading: '读取仓库文件',
    uploading: '上传到主机', verifying: '确认已保存', running: '启动新版本' };
  const PS5_STAGE_SPAN = { checking: [0, 0.06], stopping: [0.06, 0.14], reading: [0.14, 0.2],
    uploading: [0.2, 0.86], verifying: [0.86, 0.93], running: [0.93, 0.99] };
  const UPSTREAM_STAGES = { downloading: '下载', verifying: '校验', publishing: '写入仓库' };
  const UPDATE_STEPS = [['downloading', '下载'], ['validating', '校验'], ['backing_up', '备份'],
    ['installing', '安装'], ['restarting', '重启'], ['verifying', '确认']];

  const percentOf = (done, total) => (Number(total) > 0 ? Math.max(0, Math.min(1, Number(done) / Number(total))) : null);

  // 主请求进行时轮询一个只读进度接口；主请求自己负责报告失败。
  function startProgressPoll(path, onData, interval = 450) {
    let stopped = false;
    let timer = null;
    const tick = async () => {
      if (stopped) return;
      try {
        const data = await api(path, undefined, 5000);
        if (!stopped) onData(data);
      } catch { /* 进度只是锦上添花 */ }
      if (!stopped) timer = setTimeout(tick, interval);
    };
    timer = setTimeout(tick, 200);
    return () => { stopped = true; clearTimeout(timer); };
  }

  function renderOpProgress(id, state) {
    const box = $(id);
    if (!box) return;
    box.hidden = !state;
    if (!state) return;
    const [label, percent] = box.querySelectorAll('.op-label > span');
    const fill = box.querySelector('.op-bar > span');
    const fraction = typeof state.fraction === 'number' ? Math.max(0, Math.min(1, state.fraction)) : null;
    if (label) label.textContent = state.label || '';
    if (percent) percent.textContent = fraction === null ? '' : `${Math.round(fraction * 100)}%`;
    box.dataset.indeterminate = String(fraction === null);
    if (fill) fill.style.width = fraction === null ? '' : `${(fraction * 100).toFixed(1)}%`;
  }

  function ps5StageFraction(progress) {
    const span = PS5_STAGE_SPAN[progress && progress.stage] || [0, 0.05];
    const inner = progress && progress.stage === 'uploading' ? percentOf(progress.done, progress.total) : null;
    return span[0] + (span[1] - span[0]) * (inner === null ? 0.5 : inner);
  }

  function ps5StageText(progress) {
    const label = PS5_STAGES[progress && progress.stage] || '处理中';
    const inner = progress && progress.stage === 'uploading' ? percentOf(progress.done, progress.total) : null;
    return inner === null ? `${label}…` : `${label} ${Math.round(inner * 100)}%`;
  }

  function renderPs5OpProgress() {
    if (!ps5Batch || !ps5Progress) {
      renderOpProgress('ps5-op-progress', ps5Batch ? { label: `正在更新 ${ps5Batch.index + 1} / ${ps5Batch.total}：${ps5Batch.name}`, fraction: null } : null);
      return;
    }
    const step = ps5StageFraction(ps5Progress);
    const bytes = ps5Progress.stage === 'uploading' && Number(ps5Progress.total) > 0
      ? ` · ${formatBytes(ps5Progress.done)} / ${formatBytes(ps5Progress.total)}` : '';
    renderOpProgress('ps5-op-progress', {
      label: `正在更新 ${ps5Batch.index + 1} / ${ps5Batch.total}：${ps5Batch.name} · ${PS5_STAGES[ps5Progress.stage] || '处理中'}${bytes}`,
      fraction: (ps5Batch.index + step) / ps5Batch.total
    });
  }

  function watchPs5Progress(ip, filename) {
    return startProgressPoll(`/api/ps5/progress?ip=${encodeURIComponent(ip)}`, (data) => {
      const progress = data && data.progress && typeof data.progress === 'object' ? data.progress : null;
      if (progress && progress.filename && progress.filename !== filename) return;
      if (progress) ps5Progress = { ...progress, filename };
      renderPs5OpProgress();
      const host = ps5HostByIp(ip);
      if (host && ps5Tab === 'catalog' && !$('ps5-detail').hidden) syncPs5CatalogRows(host);
    });
  }

  function renderUpstreamProgress() {
    if (!upstreamBatch) {
      renderOpProgress('repo-upstream-progress', null);
      return;
    }
    const progress = upstreamBatch.progress;
    const stage = progress && progress.stage;
    const inner = stage === 'downloading' ? percentOf(progress.done, progress.total)
      : stage === 'verifying' ? 1 : stage === 'publishing' ? 1 : null;
    const share = stage === 'downloading' ? 0.85 * (inner ?? 0.3) : stage === 'verifying' ? 0.9 : stage === 'publishing' ? 0.96 : 0.02;
    const bytes = stage === 'downloading' && progress.done
      ? ` · ${formatBytes(progress.done)}${Number(progress.total) > 0 ? ` / ${formatBytes(progress.total)}` : ''}` : '';
    const prefix = upstreamBatch.total > 1 ? `${upstreamBatch.index + 1} / ${upstreamBatch.total}：` : '';
    renderOpProgress('repo-upstream-progress', {
      label: `${prefix}${upstreamBatch.name} ${upstreamBatch.version} · ${UPSTREAM_STAGES[stage] || '连接上游'}${bytes}`.replace(/\s+·/, ' ·'),
      fraction: stage === 'downloading' && inner === null ? null : (upstreamBatch.index + share) / upstreamBatch.total
    });
  }

  function renderUpdateStepper(job, active) {
    const box = $('update-stepper');
    if (!box) return;
    const state = job && job.state;
    const show = Boolean(active || updates.reconnect);
    box.hidden = !show;
    if (!show) return;
    let current = UPDATE_STEPS.findIndex(([key]) => key === state);
    if (updates.reconnect && current < 4) current = 4;   // 断开连接通常意味着正在重启
    const list = $('update-steps');
    const items = UPDATE_STEPS.map(([key, label], index) => {
      const item = makeElement('li', 'op-step', label);
      item.dataset.state = index < current ? 'done' : index === current ? 'active' : 'pending';
      if (index === current) item.setAttribute('aria-current', 'step');
      return item;
    });
    list.replaceChildren(...items);
    const download = state === 'downloading' ? percentOf(job.bytes_done, job.bytes_total) : null;
    const base = Math.max(0, current);
    const fraction = state === 'queued' ? 0.02 : (base + (state === 'downloading' ? (download ?? 0.15) : 0.5)) / UPDATE_STEPS.length;
    const fill = $('update-bar');
    if (fill) fill.style.width = `${(Math.max(0.02, Math.min(1, fraction)) * 100).toFixed(1)}%`;
    const label = state === 'downloading' && Number(job.bytes_total) > 0
      ? `正在下载更新包 ${formatBytes(job.bytes_done || 0)} / ${formatBytes(job.bytes_total)}`
      : updates.reconnect ? '服务正在重启，页面会自动重新连接…'
        : state === 'queued' ? '已提交，等待开始…' : `${UPDATE_STATE_LABELS[state] || '处理中'}…`;
    $('update-bar-label').textContent = label;
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

  /* ── 功能说明：图标悬停 / 聚焦 / 点按显示 ─────────────────────────── */

  const HELP = {
    dns: { title: 'DNS 服务', body: '本机在 UDP / TCP 53 端口为 PS5 解析域名，只解析「访问策略」白名单里的域名，其余查询一律拒绝，PS5 因此连不上系统更新、遥测等服务。另有几个内置域名（用户手册入口等）固定由本机应答，见「访问策略」里的说明。',
      steps: ['在 PS5 的网络设置里把主 DNS 指向本机（见概览「PS5 网络设置」）。', '每次查询及结果都记录在「网络活动」。'] },
    gateway: { title: 'IPv4 网关', body: 'PS5 把默认网关指向本机后，出网的 IPv4 流量都要经过本机的 nftables 规则：默认拒绝，只放行白名单域名解析出的 IP 和白名单里的 IP / 网段。局域网访问不受影响。',
      steps: ['IPv6 不经本机转发，家庭网络应关闭 IPv6 出网。', '「错误」通常是规则写入失败，详情见设置 → 运行日志。'] },
    manual: { title: '用户手册入口', body: 'PS5 的「用户手册」会访问 manuals.playstation.net 等内置域名。本机把这些域名解析到自己（不需要、也不应该加入白名单），并在 HTTPS 443 端口托管越狱页面，在 PS5 上打开用户手册即可进入。',
      steps: ['PS5：设置 → 用户指南、健康与安全以及其他信息 → 用户手册。', '状态异常时看设置 → 服务信息里的用户手册服务（ps5-jb-host）。'] },
    'repo-service': { title: '插件仓库', body: '本机在 8080 端口提供 Payload Manager（pldmgr）能识别的插件源 repo.json，PS5 添加这个源后就能下载这里的插件。',
      steps: ['在「插件仓库」上传 .elf / .bin，或从官方源检查更新。', '在「PS5 主机」卡片上点「接入本服务仓库」，自动把源加到主机。'] },
    runtime: { title: '运行信息', body: '运行时间是管理服务本次启动以来的时长；「最近应用」是白名单最后一次写入网关的时间；「动态授权地址」是白名单域名当前解析出、正临时放行的 IP 数，DNS 记录过期后自动收回。' },
    'dns-log': { title: '最近 DNS 请求', body: '最近几条 PS5 的 DNS 查询和处理结果。「已拒绝 · 不在域名白名单」表示该域名被拦截。',
      steps: ['需要放行时，到「网络活动」勾选后批量放行，或在「访问策略」手动添加。'] },
    'ps5-network': { title: 'PS5 网络设置', body: 'PS5 需要手动设置网络才会经过本机：IP 地址保持自动获取，主 DNS 与默认网关都填本机地址，次 DNS 留空。',
      steps: ['PS5：设置 → 网络 → 设置 → 设定互联网连接，选中当前网络后进入「高级设置」。', '设好后「测试互联网连接」能获取 IP 即可；连接互联网失败是默认拒绝的正常结果。'] },
    counters: { title: '网关计数器', body: '网关防火墙（nftables）各条规则累计的数据包和字节数，用来判断 PS5 的流量是否真的经过本机，以及被放行还是被丢弃。',
      steps: ['重新应用白名单或重启服务后计数可能清零。'] },
    whitelist: { title: '访问白名单', body: '允许 PS5 访问的互联网目标，其余全部拒绝。修改后点右上角「保存并应用」才生效。',
      steps: ['example.com 只匹配该域名本身；*.example.com 匹配所有子域名但不含根域名，两者都要时分两行写。', 'IPv4 / CIDR 按目标地址直接放行，也适用于不经 DNS 的连接。', '共用 CDN 的域名放行后，同一 IP 上的其他服务也可能可达。', '用户手册、连网检测用的内置域名始终由本机应答，不用写在这里。'] },
    'upstream-dns': { title: '上游 DNS', body: '本机解析白名单域名时转发到的 DNS 服务器，只支持 IPv4 地址，一般填路由器地址或公共 DNS（例如 223.5.5.5）。' },
    apply: { title: '应用与回退', body: '「保存并应用」立即重写 DNS 白名单和网关规则；服务器保留上一版配置，改错时用「回退上一版」一键恢复。' },
    activity: { title: '网络活动', body: '实时记录经过本机网关的 IPv4 连接、PS5 的 DNS 查询，以及访问本机服务（管理页、插件仓库、pldmgr 等）的事件。',
      steps: ['勾选「未放行」的目标后点「放行选中」，域名或 IP 加入白名单并立即生效。', '「命名选中」给目标起名、指定分类，以后更容易辨认。', '标「本机劫持」的 DNS 记录来自内置域名，由本机直接应答，不是白名单放行。', '本机服务事件只作记录，不能放行。'] },
    discovery: { title: '主机发现', body: '扫描本机所在的 IPv4 网段，找出运行 pldmgr（8084）或 elfldr（9021）的 PS5；处于待机的主机也会通过 DDP 协议被发现。扫描不访问互联网。',
      steps: ['扫不到时手动输入 PS5 的 IP 添加。', '越狱后先在 PS5 上运行 Payload Manager，管理功能才完整。'] },
    hosts: { title: '发现的主机', body: '每张卡片是一台 PS5：在线状态、pldmgr / elfldr 是否可用、是否已接入本服务仓库，以及可安装、运行中、可更新的插件数。',
      steps: ['「接入本服务仓库」把本机插件源加到这台 PS5 的 pldmgr。', '「待机 / 唤醒」需要先「配对遥控游玩」，唤醒通过遥控游玩协议完成。', '「退出 pldmgr」只让 Payload Manager 退出，不会关闭主机。'] },
    'ps5-detail': { title: '主机详情', body: '选中主机的插件管理，分三个标签：',
      steps: ['插件库：把本服务仓库里的插件安装到主机，或安装后立即运行；主机上的版本比仓库旧时显示「可更新」，可一键「更新并应用」。', '运行中：查看并停止主机上正在运行的插件进程。', '自动加载：设置 pldmgr 启动后按什么顺序、间隔多久自动加载哪些插件。'] },
    'ps5-upgrade': { title: '更新并应用', body: '把主机上旧版本的插件替换为插件仓库里的版本。',
      steps: ['旧版本正在运行：先停止旧进程，上传新版本，再启动新版本。', '没有运行：只替换文件，下次手动运行或自动加载时生效。', '版本按 pldmgr 的插件槽位与文件名里的版本号比较；读不出版本号时显示「可能有更新」，请自行确认。', '通过本服务安装过的插件会记住当时的文件，比对更准确。'] },
    autoload: { title: '自动加载', body: 'pldmgr 启动后先倒计时，再按列表顺序加载插件；每个插件后面可以设等待时间再加载下一个。设置保存在 PS5 上，下次越狱启动 pldmgr 时生效。',
      steps: ['常见顺序：先加载 kstuff 等内核补丁，等几秒，再加载文件服务、ShadowMount+ 等。', '如果主机上的列表已在别处被修改，保存前会提示是否覆盖。'] },
    files: { title: 'PS5 文件', body: '通过本机代理浏览 PS5 的存储（内置 /data、M.2 扩展 /mnt/ext*、USB /mnt/usb*），浏览器不直连 PS5。',
      steps: ['PS5 需要运行 zftpd（FTP 2120）或 tom-ps5-usbnet 的文件服务，本页自动选择可用的一个。', '支持上传（可拖拽）、新建文件夹、重命名、复制 / 移动、删除和下载，删除与覆盖都会先确认。', '游戏文件夹和镜像按标题 ID 显示游戏名。'] },
    'ps5-storage': { title: 'PS5 存储', body: 'PS5 上可浏览的存储位置和剩余空间。FTP 模式下容量通过 PS5 上 web 文件管理（8888 端口）读取，读不到时显示未知。' },
    nas: { title: 'NAS 存储', body: '把家里 NAS 的共享目录接进来，文件从 NAS 经本机中转直接写到 PS5，不落本机磁盘。',
      steps: ['点「＋ 添加」选择 SMB、WebDAV 或 FTP，填写地址、共享名与账号。', '账号密码用 AES-256-GCM 加密保存，页面和接口都不会回显密码。', '勾选文件后点「传到 PS5」，进度在「传输任务」查看。'] },
    'nas-extract': { title: '流式解压安装', body: '把 NAS 上的 RAR 压缩包（单个 .rar 或 *.partNN.rar 分卷）边读边解压，直接写进 PS5，不用先下载到电脑，也不占本机磁盘。',
      steps: ['在 NAS 列表勾选压缩包（分卷任选一个即可），点「解压安装」；同组分卷会自动找齐，缺卷会提前提示。', '「安装」按包内 eboot.bin 与 param.json 找到游戏根目录，放到目标位置下以标题 ID 命名的文件夹，ShadowMount+ 可直接识别；「合并」用于把补丁覆盖进已有游戏文件夹。', '解压完成后默认在 PS5 主屏幕注册游戏图标（需要 ps5upload 引擎）。', '不支持带密码的包和 RAR4 旧格式；固实包或大字典包需要服务器装有 unrar 引擎。失败或取消会自动清理半成品。'] },
    transfers: { title: '传输任务', body: '集中显示上传、复制 / 移动、NAS 传输和解压任务的进度、速度与剩余时间，可随时取消。',
      steps: ['NAS 传输与解压在服务器上执行，关闭页面不会中断。', '从浏览器上传的文件需要保持页面打开直到完成。'] },
    repo: { title: '插件载荷', body: 'PS5 能安装的插件载荷（.elf / .bin）。这里的文件同时发布到本机插件源，PS5 的 pldmgr 和「PS5 主机」页都从这里安装。',
      steps: ['右上角「上传插件」添加文件，名称和版本会从文件名自动识别。', '每行可编辑信息、查看历史版本或删除；每个插件保留最近 3 个旧版本，可回退。'] },
    'repo-endpoint': { title: 'Payload Manager 源地址', body: 'pldmgr 使用的插件源地址（repo.json）。在 PS5 的 Payload Manager 里添加这个源即可浏览安装；也可以在「PS5 主机」卡片上一键接入。' },
    upstream: { title: '上游更新', body: '对照 Payload Manager 官方源（itsPLK/ps5-payloads-mirror）检查仓库插件的新版本。只在点击时联网，不会自动检查。',
      steps: ['下载的文件按官方索引的 SHA-256 校验，不一致就放弃。', '更新后旧版本自动保留在「历史」里，可随时回退。', '仓库更新后，PS5 上的旧版本会在「PS5 主机 → 插件库」提示一键更新。', '网络受限时可给服务配置代理（设置 → 服务更新 → 网络代理）。'] },
    'service-update': { title: '服务更新', body: '从 GitHub 检查本服务的正式发布版本，并在网页上一键升级。',
      steps: ['升级包会校验 SHA-256，先备份再替换程序，配置、插件和数据都保留。', '升级中服务会重启，页面短暂断开后自动重连；新版本启动失败会自动回滚。', '有传输或解压任务时不能升级。'] },
    proxy: { title: '网络代理', body: '访问 GitHub 和插件官方源时使用的 HTTP 代理，来自服务的环境变量 PS5JB_PROXY 或 HTTPS_PROXY。PS5、NAS 和本机插件仓库始终直连。',
      steps: ['安装时用 install.sh --proxy 设置，或给 systemd 服务加 drop-in 设置环境变量后重启服务。'] },
    password: { title: '修改管理密码', body: '修改登录本管理页的密码，至少 10 个字符。修改后所有设备上的登录都会失效，需要用新密码重新登录。' },
    'service-info': { title: '服务信息', body: '本服务的端口、网段、数据目录和 systemd 服务名，排查问题或手动维护时使用。' },
    logs: { title: '运行日志', body: '管理服务的运行记录：警告与错误、主机 / 插件 / 文件操作、更新过程等。页面定时刷新这类成功的读取请求不会记录。',
      steps: ['按级别或关键字筛选；勾选「自动刷新」时每 5 秒追加新记录。', '「导出日志」下载文本文件，包含诊断摘要、服务日志和本浏览器最近的错误提示，反馈问题时附上即可。', '解压工作单元的详细日志在服务器上用 journalctl -u ps5-extract-<任务> 查看。'] },
    disable: { title: '停用与回滚', body: '需要停用本服务时，先把 PS5 的网关和 DNS 改回路由器，再按步骤停止服务。停止后网关规则保持默认拒绝，不会意外放开 PS5 的外网访问。' }
  };

  const help = { pop: null, anchor: null, showTimer: null, hideTimer: null, pinned: false };

  function initHelpTips() {
    const pop = makeElement('div', 'help-pop');
    pop.id = 'help-pop';
    pop.setAttribute('role', 'tooltip');
    pop.hidden = true;
    document.body.append(pop);
    help.pop = pop;
    for (const tip of $$('.help-tip[data-help]')) {
      const entry = HELP[tip.dataset.help];
      if (!entry) { tip.remove(); continue; }
      tip.setAttribute('aria-label', `功能说明：${entry.title}`);
      tip.setAttribute('aria-expanded', 'false');
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('class', 'icon');
      svg.setAttribute('aria-hidden', 'true');
      const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
      use.setAttribute('href', '#i-help');
      svg.append(use);
      tip.replaceChildren(svg);
    }
    const tipOf = (event) => (event.target instanceof Element ? event.target.closest('.help-tip[data-help]') : null);
    document.addEventListener('mouseover', (event) => {
      const tip = tipOf(event);
      if (tip) {
        clearTimeout(help.hideTimer);
        if (help.anchor === tip) return;
        clearTimeout(help.showTimer);
        help.showTimer = setTimeout(() => showHelp(tip, false), help.anchor ? 0 : 140);
      } else if (event.target instanceof Element && event.target.closest('#help-pop')) {
        clearTimeout(help.hideTimer);
      }
    });
    document.addEventListener('mouseout', (event) => {
      const from = event.target instanceof Element ? event.target.closest('.help-tip[data-help], #help-pop') : null;
      const to = event.relatedTarget instanceof Element ? event.relatedTarget.closest('.help-tip[data-help], #help-pop') : null;
      if (!from || to) return;
      clearTimeout(help.showTimer);
      if (!help.pinned) help.hideTimer = setTimeout(hideHelp, 180);
    });
    document.addEventListener('focusin', (event) => {
      const tip = tipOf(event);
      if (tip && tip.matches(':focus-visible')) showHelp(tip, false);
    });
    document.addEventListener('focusout', (event) => {
      if (tipOf(event) && !help.pinned) hideHelp();
    });
    // 点按（触屏）固定显示；在 <summary> 里时不让它展开 / 收起卡片。
    document.addEventListener('click', (event) => {
      const tip = tipOf(event);
      if (tip) {
        event.preventDefault();
        event.stopPropagation();
        if (help.anchor === tip && help.pinned) hideHelp();
        else showHelp(tip, true);
        return;
      }
      if (help.anchor && !(event.target instanceof Element && event.target.closest('#help-pop'))) hideHelp();
    }, true);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && help.anchor) {
        const anchor = help.anchor;
        hideHelp();
        if (anchor.isConnected) anchor.focus({ preventScroll: true });
      }
    });
    window.addEventListener('resize', () => { if (help.anchor) placeHelp(); });
    window.addEventListener('scroll', () => { if (help.anchor) placeHelp(); }, true);
  }

  function showHelp(tip, pinned) {
    const entry = HELP[tip.dataset.help];
    if (!entry || !help.pop) return;
    clearTimeout(help.hideTimer);
    if (help.anchor && help.anchor !== tip) help.anchor.setAttribute('aria-expanded', 'false');
    help.anchor = tip;
    help.pinned = pinned;
    const nodes = [makeElement('strong', 'help-pop-title', entry.title), makeElement('p', 'help-pop-body', entry.body)];
    if (entry.steps && entry.steps.length) {
      const list = makeElement('ul', 'help-pop-steps');
      for (const step of entry.steps) list.append(makeElement('li', '', step));
      nodes.push(list);
    }
    help.pop.replaceChildren(...nodes);
    help.pop.hidden = false;
    tip.setAttribute('aria-expanded', 'true');
    tip.setAttribute('aria-describedby', 'help-pop');
    placeHelp();
  }

  function placeHelp() {
    const tip = help.anchor;
    const pop = help.pop;
    if (!tip || !pop || !tip.isConnected || !tip.getClientRects().length) { hideHelp(); return; }
    const rect = tip.getBoundingClientRect();
    const gap = 8;
    const margin = 12;
    const width = Math.min(340, window.innerWidth - margin * 2);
    pop.style.width = `${width}px`;
    const height = pop.offsetHeight;
    const below = rect.bottom + gap + height <= window.innerHeight - margin || rect.top - gap - height < margin;
    const top = below ? rect.bottom + gap : rect.top - gap - height;
    const left = Math.min(Math.max(margin, rect.left + rect.width / 2 - width / 2), window.innerWidth - width - margin);
    pop.dataset.side = below ? 'below' : 'above';
    pop.style.top = `${Math.max(margin, top)}px`;
    pop.style.left = `${left}px`;
  }

  function hideHelp() {
    clearTimeout(help.showTimer);
    clearTimeout(help.hideTimer);
    if (help.anchor) {
      help.anchor.setAttribute('aria-expanded', 'false');
      help.anchor.removeAttribute('aria-describedby');
    }
    help.anchor = null;
    help.pinned = false;
    if (help.pop) help.pop.hidden = true;
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
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
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

  function bindPolishEvents() {
    initHelpTips();
    window.addEventListener('error', (event) => {
      noteClient('error', `${event.message || '脚本错误'}${event.filename ? ` @ ${event.filename}:${event.lineno}` : ''}`);
    });
    window.addEventListener('unhandledrejection', (event) => {
      const reason = event.reason;
      noteClient('error', `未处理的异常：${reason && reason.message ? reason.message : String(reason)}`);
    });
    $('logs-refresh').addEventListener('click', () => loadLogs(true));
    $('logs-export').addEventListener('click', exportLogs);
    $('logs-level').addEventListener('change', () => loadLogs(true));
    $('logs-follow').addEventListener('change', () => { if ($('logs-follow').checked) loadLogs(false); else stopLogsTimer(); });
    $('logs-search').addEventListener('input', () => {
      clearTimeout(logs.searchTimer);
      logs.searchTimer = setTimeout(() => loadLogs(true), 300);
    });
    $('ps5-upgrade-all').addEventListener('click', () => { if (ps5SelectedIp) openPs5Upgrade(ps5SelectedIp, null); });
    $('ps5-upgrade-cancel').addEventListener('click', () => $('ps5-upgrade-dialog').close());
    $('ps5-upgrade-confirm').addEventListener('click', confirmPs5Upgrade);
    $('ps5-upgrade-dialog').addEventListener('close', () => {
      ps5UpgradeTarget = null;
      flushPs5Pending();
      ps5RestoreFocus();
    });
    $('repo-ps5-hint-go').addEventListener('click', (event) => goPs5Updates(event.currentTarget.dataset.ip));
    initCategoryCombo('repo-upload');
    initCategoryCombo('repo-edit');
  }

  /* ── 插件分类：下拉选择已有分类，或输入新分类 ───────────────────────── */

  // 仓库里没有用过时也给出的常用分类（与安装器自带插件的分类一致）。
  const DEFAULT_CATEGORIES = ['Loader', 'Kernel', 'System', 'Networking', 'Files', 'Diagnostics'];
  const UNCATEGORIZED = 'uncategorized';

  // 已用分类按插件数从多到少，其后补上常用分类；大小写不同视为同一分类。
  function categoryChoices(query = '') {
    const counts = new Map();
    for (const item of repo.items) {
      const name = str(item.category).trim();
      const key = name.toLowerCase();
      if (!name || key === UNCATEGORIZED || item.filename === 'ps5-payload-repository') continue;
      const entry = counts.get(key) || { name, count: 0 };
      entry.count += 1;
      counts.set(key, entry);
    }
    const names = [...counts.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name)).map((entry) => entry.name);
    for (const name of DEFAULT_CATEGORIES) if (!counts.has(name.toLowerCase())) names.push(name);
    const needle = str(query).trim().toLowerCase();
    const options = needle ? names.filter((name) => name.toLowerCase().includes(needle)) : names;
    const typed = str(query).trim();
    const exists = names.some((name) => name.toLowerCase() === needle);
    return { options, create: typed && !exists && needle !== UNCATEGORIZED ? typed.slice(0, 40) : '' };
  }

  function initCategoryCombo(prefix) {
    const input = $(`${prefix}-category`);
    const list = $(`${prefix}-category-list`);
    const toggle = $(`${prefix}-category-toggle`);
    let active = -1;

    const items = () => [...list.children].filter((node) => node.dataset && node.dataset.value !== undefined);
    const close = () => {
      list.hidden = true;
      active = -1;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
    };
    const highlight = (index) => {
      const options = items();
      if (!options.length) return;
      active = (index + options.length) % options.length;
      options.forEach((node, position) => node.setAttribute('aria-selected', String(position === active)));
      input.setAttribute('aria-activedescendant', options[active].id);
      options[active].scrollIntoView?.({ block: 'nearest' });
    };
    const open = (showAll) => {
      const { options, create } = categoryChoices(showAll ? '' : input.value);
      const nodes = options.map((name) => {
        const node = makeElement('li', 'combo-option', name);
        node.dataset.value = name;
        if (name.toLowerCase() === input.value.trim().toLowerCase()) node.dataset.current = 'true';
        return node;
      });
      if (create && !showAll) {
        const node = makeElement('li', 'combo-option combo-create', `新建分类「${create}」`);
        node.dataset.value = create;
        nodes.push(node);
      }
      if (!nodes.length) { close(); return; }
      nodes.forEach((node, index) => { node.id = `${prefix}-category-option-${index}`; node.setAttribute('role', 'option'); node.setAttribute('aria-selected', 'false'); });
      list.replaceChildren(...nodes);
      list.hidden = false;
      // 对话框底部放不下时向上展开，避免被裁掉。
      const box = input.closest?.('dialog')?.getBoundingClientRect?.();
      const rect = input.getBoundingClientRect?.();
      if (box && rect) {
        // 下方要让出对话框底部的按钮栏；两边都不够时选空间大的一边并压低列表高度。
        const below = box.bottom - rect.bottom - 76;
        const above = rect.top - box.top - 12;
        const wanted = Math.min(list.scrollHeight, 220);
        const side = below >= wanted || below >= above ? 'below' : 'above';
        list.dataset.side = side;
        list.style.maxHeight = `${Math.max(112, Math.min(220, side === 'below' ? below : above))}px`;
      }
      active = -1;
      input.setAttribute('aria-expanded', 'true');
      input.removeAttribute('aria-activedescendant');
    };
    const choose = (value) => {
      input.value = value;
      close();
      // 让上传表单把分类记为手动填写（不再被自动识别覆盖）。
      if (typeof Event === 'function') input.dispatchEvent(new Event('input', { bubbles: true }));
    };

    input.addEventListener('input', (event) => { if (event && event.isTrusted !== false) open(false); });
    input.addEventListener('focus', () => { if (!input.value.trim()) open(true); });
    input.addEventListener('blur', () => {
      // 输入了已有分类的不同大小写时，统一成已有写法。
      const typed = input.value.trim().toLowerCase();
      const same = typed && categoryChoices('').options.find((name) => name.toLowerCase() === typed);
      if (same && same !== input.value.trim()) input.value = same;
      setTimeout(close, 120);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (list.hidden) open(!input.value.trim());
        highlight(active + (event.key === 'ArrowDown' ? 1 : -1));
      } else if (event.key === 'Enter' && !list.hidden && active >= 0) {
        event.preventDefault();
        choose(items()[active].dataset.value);
      } else if (event.key === 'Escape' && !list.hidden) {
        // 只收起列表，不关闭对话框。
        event.preventDefault();
        event.stopPropagation();
        close();
      }
    });
    toggle.addEventListener('mousedown', (event) => event.preventDefault());
    toggle.addEventListener('click', () => {
      if (list.hidden) { open(true); input.focus(); } else close();
    });
    list.addEventListener('mousedown', (event) => event.preventDefault());
    list.addEventListener('click', (event) => {
      const option = event.target instanceof Element ? event.target.closest('[data-value]') : null;
      if (option) choose(option.dataset.value);
    });
  }

  function stopLogsTimer() {
    clearTimeout(logs.timer);
    logs.timer = null;
  }

  /* ── 启动 ───────────────────────────────────────────────────────────── */

  bindEvents();
  bindPolishEvents();
  loadVersion();
  renderRepo();
  updateRepoControls();
  renderActivity();
  renderPs5();
  renderFiles();
  updateControls();
  refreshState();
})();
