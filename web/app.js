(() => {
  'use strict';

  /* ── 基础工具 ───────────────────────────────────────────────────────── */

  const $ = (id) => document.getElementById(id);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const POLL_MS = 8000;
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
    activity: { title: '网络活动', subtitle: '经过本机网关的 IPv4 连接' },
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

  // PS5 主机页的瞬时状态：写入互斥、轮询暂存与待确认操作。
  let ps5Busy = false;
  let ps5Loading = false;
  let ps5Pending = null;
  let ps5Action = null;
  let ps5Draft = null;
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
    configBusy: false
  };

  const activitySelected = new Set();
  const activity = { loaded: false, events: [], counts: { allowed: 0, blocked: 0 }, tracked: 0, updated: '', lastError: '', categories: [] };
  const repo = { loaded: false, items: [], totalBytes: 0, host: '', port: REPO_PORT, catalogName: '' };
  const toastTimers = new Map();

  class ApiError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.status = status;
    }
  }

  const asText = (value, fallback = '—') => value === null || value === undefined || value === '' ? fallback : String(value);
  const str = (value) => typeof value === 'string' ? value.trim() : value === null || value === undefined ? '' : String(value).trim();
  const lines = (value) => value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
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
    const parsed = new Date(value);
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
    return `${(bytes / 1048576).toFixed(1)} MB`;
  };

  /* ── 网络请求（契约保持不变） ───────────────────────────────────────── */

  async function api(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
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
      const fallbacks = { 400: '上传失败，请检查文件名与文件内容。', 401: '登录已失效，请重新输入管理密码。', 409: '仓库中已存在同名文件，请改名或先删除原文件。', 413: '文件超过 64 MB 限制。' };
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

  function showSection(section, options = {}) {
    if (!authenticated) return;
    currentSection = section;
    for (const key of SECTIONS) {
      const view = views[key];
      if (view) view.hidden = key !== section;
    }
    for (const link of $$('.nav-item[data-section], .tab-item[data-section]')) {
      if (link.dataset.section === section) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    const meta = SECTION_META[section];
    $('app-title').textContent = meta.title;
    $('app-subtitle').textContent = meta.subtitle;
    document.title = `${meta.title} · PS5 网络访问控制`;
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
    if (section !== 'files') stopFilesPolling();
    if (section === 'activity' && !activity.loaded) refreshActivity();
    if (section === 'repository' && !repo.loaded) refreshRepo();
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
    document.title = '登录 · PS5 网络访问控制';
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
    if (entering) ensureRoute();
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
    setDisabled('rollback', busy || !authenticated);
    setDisabled('logout', busy || uploading);
    setDisabled('refresh', busy || refreshing);
    $('config-fields').disabled = busy || !authenticated;
    $('password-fields').disabled = busy || passwordBusy || !authenticated;
    setDisabled('password', busy || passwordBusy || !authenticated);
    updateRepoControls();
    updateActivityControls();
    updatePs5Controls();
    updateFilesControls();
  }

  function updateRepoControls() {
    const locked = busy || repoBusy || !authenticated;
    setDisabled('upload', locked);
    $('repo-upload-fields').disabled = locked || uploading;
    $('repo-upload-submit').disabled = locked || uploading;
    $('repo-upload-cancel').disabled = locked || uploading;
    for (const button of $$('#repo-body button.repo-action')) button.disabled = locked;
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
    $('uptime').textContent = uptime(status.uptime);
    const dynamic = Number(status.dynamic_addresses);
    $('dynamic-addresses').textContent = Number.isFinite(dynamic) && dynamic >= 0 ? `${Math.floor(dynamic)} 个` : '—';
    $$('[data-host-ip]').forEach((element) => { element.textContent = asText(status.host_ip, '—'); });
    setField('last-applied', formatTime(status.last_applied, true), status.last_applied);
    $('ipv6-note').textContent = status.ipv6_forwarding === true
      ? '检测到主机已开启 IPv6 转发。当前策略仅控制 IPv4；请确认家庭网络仍关闭 IPv6 互联网。'
      : '家庭网络已关闭 IPv6 互联网；本页网关策略不提供 IPv6 访问控制。';

    renderLogs(data.logs);
    renderFirewall(data.firewall);
    setConnection('状态已同步', 'good');
    $('sync-time').textContent = `${formatTime(new Date())} · 每 8 秒刷新`;
    updateControls();
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
      if (requestEpoch === epoch && currentSection === 'ps5') refreshPs5();
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
        size: Number.isFinite(size) && size >= 0 ? size : null
      });
    }
    const reportedTotal = Number(data && data.total_bytes);
    const totalBytes = Number.isFinite(reportedTotal) && reportedTotal >= 0
      ? reportedTotal
      : items.reduce((sum, item) => sum + (item.size || 0), 0);
    const reportedPort = Number(data && data.port);
    return {
      loaded: true,
      items,
      totalBytes,
      host: str(data && data.host),
      port: Number.isInteger(reportedPort) && reportedPort > 0 && reportedPort <= 65535 ? reportedPort : REPO_PORT,
      catalogName: str(catalog.name)
    };
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
    Object.assign(repo, { loaded: false, items: [], totalBytes: 0, host: '', port: REPO_PORT, catalogName: '' });
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

    const versionCell = document.createElement('td');
    versionCell.textContent = asText(item.version);
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

    const remove = document.createElement('button');
    remove.className = 'repo-action';
    remove.type = 'button';
    remove.dataset.tone = 'danger';
    remove.textContent = '删除';
    remove.setAttribute('aria-label', `删除 ${item.filename}`);
    remove.addEventListener('click', () => openDeleteDialog(item));

    actions.append(download, edit, remove);
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
    const current = JSON.stringify([repo.items, repo.totalBytes, repo.host, repo.port, repo.catalogName]);
    $('repo-summary').textContent = repo.loaded ? `共 ${repo.items.length} 个插件 · 合计 ${formatBytes(repo.totalBytes)}` : '正在读取插件仓库…';
    const endpoint = repo.loaded ? `${repo.catalogName ? `${repo.catalogName} · ` : ''}${repoBaseUrl()}/payloads/` : '';
    $('repo-endpoint').textContent = endpoint;
    $('repo-endpoint').hidden = !endpoint;
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
      fragment.append(skeletonRows(4, 6));
    } else if (!repo.items.length) {
      fragment.append(emptyRow(6, '仓库中暂无插件。', '上传第一个插件', () => openUploadDialog()));
    } else {
      for (const item of repo.items) fragment.append(repoRow(item));
    }
    $('repo-body').replaceChildren(fragment);
    updateRepoControls();
    ps5CatalogRefreshHook();
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
      $('repo-error').textContent = `插件仓库读取失败：${error.message}`;
      $('repo-error').hidden = false;
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

  function clearUploadErrors() {
    for (const [field, error] of [['repo-upload-file', 'repo-upload-file-error'], ['repo-upload-name', 'repo-upload-name-error'], ['repo-upload-version', 'repo-upload-version-error'], ['repo-upload-category', 'repo-upload-category-error'], ['repo-upload-author', 'repo-upload-author-error'], ['repo-upload-description', 'repo-upload-description-error']]) {
      $(field).removeAttribute('aria-invalid');
      $(error).textContent = '';
      $(error).hidden = true;
    }
    $('repo-upload-error').textContent = '';
    $('repo-upload-error').hidden = true;
  }

  function resetUploadForm() {
    $('repo-upload-form').reset();
    clearUploadErrors();
    $('repo-upload-status').textContent = '';
  }

  function openUploadDialog() {
    if (!authenticated || uploading || repoBusy) return;
    resetUploadForm();
    $('repo-upload-dialog').showModal();
    $('repo-upload-file').focus({ preventScroll: true });
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
    if (!file) {
      invalid('repo-upload-file', 'repo-upload-file-error', '请选择要上传的 .elf 或 .bin 文件。');
    } else {
      const lower = file.name.toLowerCase();
      if (!UPLOAD_EXTENSIONS.some((extension) => lower.endsWith(extension))) invalid('repo-upload-file', 'repo-upload-file-error', '只支持 .elf 与 .bin 文件。');
      else if (!file.size) invalid('repo-upload-file', 'repo-upload-file-error', '所选文件为空，请重新选择。');
      else if (file.size > UPLOAD_MAX) invalid('repo-upload-file', 'repo-upload-file-error', `文件大小 ${formatBytes(file.size)} 超过 64 MB 限制。`);
    }
    if (!name) invalid('repo-upload-name', 'repo-upload-name-error', '请填写插件名称。');
    if (first) {
      first.focus();
      return;
    }
    const query = new URLSearchParams({
      filename: file.name,
      name,
      version: $('repo-upload-version').value.trim(),
      category: $('repo-upload-category').value.trim(),
      author: $('repo-upload-author').value.trim(),
      description: $('repo-upload-description').value.trim()
    });
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
      toast(`已上传 ${asText(result.filename, file.name)}。`, 'success');
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
        // Service rows are terminal observations, not whitelist targets.
        selectable: kind !== 'service' && Boolean(target),
        time: str(entry.time),
        client: str(entry.client),
        dst,
        port: Number.isInteger(port) && port > 0 && port <= 65535 ? port : null,
        domain,
        name: str(entry.name),
        category: str(entry.category),
        verdict: str(entry.verdict).toLowerCase() === 'blocked' ? 'blocked' : 'allowed',
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
    return activityFilterValue === 'all'
      ? activity.events
      : activity.events.filter((event) => event.verdict === activityFilterValue);
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
    } else {
      nameCell.textContent = asText(event.name, '');
    }
    if (!nameCell.textContent) nameCell.dataset.empty = 'true';

    row.append(checkCell, timeCell, clientCell, domainCell, ipCell, portCell, verdictCell, categoryCell, nameCell);
    return row;
  }

  function renderActivityRows() {
    const visible = activityVisibleEvents();
    const fragment = document.createDocumentFragment();
    if (!activity.loaded) {
      fragment.append(skeletonRows(5, 9));
    } else if (!activity.events.length) {
      fragment.append(activityEmptyRow(activity.hints.message || '还没有经过本机的连接记录。', true));
    } else if (!visible.length) {
      fragment.append(activityEmptyRow('当前筛选条件下没有记录。'));
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
      warning.textContent = `规则分类更新失败：${activity.lastError}`;
      warning.hidden = false;
    } else {
      warning.textContent = '';
      warning.hidden = true;
    }
    renderActivityCategories();
    renderActivityRows();
    updateActivityControls();
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
      applyActivity(normalizeActivity(data));
      if (manual) toast('网络活动已刷新。', 'success');
      return true;
    } catch (error) {
      if (requestEpoch !== epoch) return false;
      if (error.status === 401) {
        showLogin(error.message);
        return false;
      }
      $('activity-warning').textContent = `网络活动读取失败：${error.message}`;
      $('activity-warning').hidden = false;
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
    const remaining = Number(statusRaw.remaining);
    const done = Number(statusRaw.done);
    const repoPayloads = Number(entry.repo_payloads);
    return {
      ip,
      online: entry.online === true,
      elfldr: entry.elfldr === true,
      pldmgr: entry.pldmgr === true,
      version: str(entry.version),
      sourceAttached: entry.source_attached === true,
      sourceUrl: str(entry.source_url),
      repoPayloads: Number.isFinite(repoPayloads) && repoPayloads >= 0 ? Math.floor(repoPayloads) : null,
      installed: Array.isArray(entry.installed) ? [...new Set(entry.installed.map((name) => str(name)).filter(Boolean))] : [],
      running,
      autoload: {
        enabled: autoloadRaw.enabled === true,
        list: Array.isArray(autoloadRaw.list) ? [...new Set(autoloadRaw.list.map((name) => str(name)).filter(Boolean))] : [],
        status: {
          remaining: Number.isFinite(remaining) && remaining >= 0 ? Math.floor(remaining) : null,
          done: Number.isFinite(done) && done >= 0 ? Math.floor(done) : null,
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
      ['shutdown', '关机', '关闭主机', true]
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
      actions.append(button);
    }

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
    let reason = !host.online ? '主机当前离线' : '';
    if (action === 'select') {
      disabled = false;
    } else if (action === 'attach') {
      if (!host.pldmgr) {
        disabled = true;
        reason = '需要主机运行 pldmgr（8084）';
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
    if (meta) meta.textContent = host.sourceUrl ? `仓库源 ${host.sourceUrl}` : '仓库源地址未知';

    const online = card.querySelector('[data-field="online"]');
    online.textContent = host.online ? '在线' : '离线';
    online.dataset.tone = host.online ? 'good' : 'neutral';
    const elfldr = card.querySelector('[data-field="elfldr"]');
    elfldr.textContent = host.elfldr ? 'elfldr 可用' : 'elfldr 不可用';
    elfldr.dataset.tone = host.elfldr ? 'good' : 'neutral';
    const version = ps5VersionLabel(host.version);
    const pldmgr = card.querySelector('[data-field="pldmgr"]');
    pldmgr.textContent = host.pldmgr ? `pldmgr 8084${version ? ` · ${version}` : ''}` : 'pldmgr 不可用';
    pldmgr.dataset.tone = host.pldmgr ? 'good' : 'neutral';
    const source = card.querySelector('[data-field="source"]');
    source.textContent = host.sourceAttached ? '仓库已接入' : '仓库未接入';
    source.dataset.tone = host.sourceAttached ? 'good' : 'neutral';

    const catalogItems = repo.loaded ? repo.items : [];
    const installable = catalogItems.length
      ? catalogItems.filter((item) => !host.installed.includes(item.filename)).length
      : host.repoPayloads === null ? 0 : host.repoPayloads;
    const counts = card.querySelector('[data-field="counts"]');
    counts.textContent = `可安装 ${installable} 个插件 · 运行中 ${host.running.length}`;

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
    const cardBusy = Boolean(ps5Action && ps5Action.ip === host.ip);
    busyNode.hidden = !cardBusy;
    const busyLabel = busyNode.querySelector('[data-field="busy-label"]');
    if (busyLabel) busyLabel.textContent = (ps5Action && ps5Action.label) || '处理中…';
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
    if (!repo.loaded) {
      note.textContent = '正在读取本服务的插件目录…';
      if (body.dataset.mode !== 'loading') {
        body.dataset.mode = 'loading';
        body.replaceChildren(tableEmptyRow(6, '正在读取插件目录…'));
      }
      return;
    }
    const items = repo.items;
    if (!items.length) {
      note.textContent = '本服务的插件仓库为空。';
      if (body.dataset.mode !== 'empty') {
        body.dataset.mode = 'empty';
        body.replaceChildren(tableEmptyRow(6, '插件目录为空，请先在「插件仓库」上传载荷。'));
      }
      return;
    }
    const rowError = ps5InlineError && ps5InlineError.scope === 'install' && ps5InlineError.ip === host.ip ? ps5InlineError : null;
    note.textContent = `本服务仓库共 ${items.length} 个插件 · 主机已安装 ${host.installed.length} 个${host.sourceAttached ? '' : ' · 仓库未接入，安装将尝试通过 elfldr 执行'}`;
    const key = JSON.stringify([
      host.ip,
      host.installed,
      host.running.map((item) => [item.pid, item.name]),
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
    for (const [action, label] of [['install', '安装'], ['install-run', '安装并运行']]) {
      const button = makeElement('button', 'button button-secondary button-small');
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
      const installed = host.installed.includes(filename);
      const running = host.running.some((item) => item.name === filename);
      const badge = makeElement('span', 'result-badge');
      if (busy) {
        badge.dataset.tone = 'neutral';
        badge.textContent = '安装中…';
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
      if (rowError) content.append(makeElement('span', 'ps5-row-error', rowError));
      const statusCell = row.querySelector('[data-field="status"]');
      if (statusCell) statusCell.replaceChildren(content);
      row.dataset.busy = String(busy);
      for (const button of $$('[data-ps5-action]', row)) {
        const buttonBusy = busy;
        const allowed = host.online && (host.pldmgr || host.elfldr);
        button.disabled = !authenticated || ps5Busy || !allowed;
        button.setAttribute('aria-busy', String(buttonBusy));
        const spinner = button.querySelector('.spinner');
        if (spinner) spinner.hidden = !buttonBusy;
        if (!allowed) button.title = !host.online ? '主机当前离线' : '需要 pldmgr 或 elfldr';
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

  function ps5AutoloadValues(host) {
    if (ps5Draft && ps5Draft.ip === host.ip) {
      return { enabled: ps5Draft.enabled, files: [...ps5Draft.files] };
    }
    return { enabled: host.autoload.enabled, files: [...host.autoload.list] };
  }

  function ps5EnsureDraft(host) {
    if (!ps5Draft || ps5Draft.ip !== host.ip) {
      ps5Draft = { ip: host.ip, enabled: host.autoload.enabled, files: new Set(host.autoload.list) };
    }
    return ps5Draft;
  }

  function ps5DraftDirty(host) {
    if (!ps5Draft || ps5Draft.ip !== host.ip) return false;
    return ps5Draft.enabled !== host.autoload.enabled || !sameStringSet(ps5Draft.files, host.autoload.list);
  }

  function ps5AutoloadOptions(host) {
    const names = new Set([...host.installed, ...host.autoload.list]);
    if (ps5Draft && ps5Draft.ip === host.ip) for (const name of ps5Draft.files) names.add(name);
    return [...names].sort((left, right) => left.localeCompare(right, 'zh', { numeric: true }));
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
    for (const box of $$('#ps5-autoload-items input[type="checkbox"]')) box.disabled = locked;
    const save = $('ps5-autoload-save');
    const saveBusy = Boolean(ps5Action && ps5Action.kind === 'autoload' && ps5Action.ip === host.ip);
    save.disabled = locked || saveBusy;
    save.setAttribute('aria-busy', String(saveBusy));
    const spinner = save.querySelector('.spinner');
    if (spinner) spinner.hidden = !saveBusy;
    const errorNode = $('ps5-autoload-error');
    const error = ps5InlineError && ps5InlineError.scope === 'autoload' && ps5InlineError.ip === host.ip ? ps5InlineError.message : '';
    errorNode.textContent = error;
    errorNode.hidden = !error;
  }

  function renderPs5Autoload(host, options = {}) {
    const values = ps5AutoloadValues(host);
    syncPs5AutoloadMeta(host);
    const note = $('ps5-autoload-note');
    if (!host.pldmgr) {
      note.textContent = '自动加载需要 pldmgr（8084）。该主机当前未运行 pldmgr，设置已禁用。';
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
    const hasProgress = Boolean(status && (status.remaining !== null || status.done !== null || status.current));
    if (hasProgress) {
      const parts = [];
      if (status.remaining !== null) parts.push(`剩余 ${status.remaining}`);
      if (status.done !== null) parts.push(`已完成 ${status.done}`);
      if (status.current) parts.push(`当前 ${status.current}`);
      progress.textContent = `pldmgr 自动加载：${parts.join(' · ')}`;
      progress.hidden = false;
    } else {
      progress.textContent = '';
      progress.hidden = true;
    }
    const optionsList = ps5AutoloadOptions(host);
    $('ps5-autoload-list').hidden = optionsList.length === 0;
    $('ps5-autoload-empty').hidden = optionsList.length > 0;
    const key = JSON.stringify([
      host.ip,
      optionsList,
      host.autoload.enabled,
      host.autoload.list,
      host.autoload.status,
      host.installed,
      host.pldmgr,
      host.online
    ]);
    if (!options.force && key === ps5AutoloadKey) {
      syncPs5AutoloadControls(host, values);
      return;
    }
    ps5AutoloadKey = key;
    const fragment = document.createDocumentFragment();
    for (const filename of optionsList) {
      const label = makeElement('label', 'ps5-autoload-item');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.className = 'ps5-autoload-check';
      box.dataset.ps5File = filename;
      box.checked = values.files.includes(filename);
      box.setAttribute('aria-label', `自动加载 ${filename}`);
      label.append(box, makeElement('span', 'mono ps5-autoload-name', filename));
      if (!host.installed.includes(filename)) label.append(makeElement('span', 'ps5-autoload-missing', '未安装'));
      fragment.append(label);
    }
    $('ps5-autoload-items').replaceChildren(fragment);
    syncPs5AutoloadControls(host, values);
  }

  function onPs5AutoloadToggle(event) {
    const host = ps5SelectedHost();
    if (!host || ps5Busy || !authenticated) return;
    const draft = ps5EnsureDraft(host);
    draft.enabled = event.target.checked;
    syncPs5AutoloadMeta(host);
    updatePs5Controls();
  }

  function onPs5AutoloadFile(event) {
    const host = ps5SelectedHost();
    if (!host || ps5Busy || !authenticated) return;
    const box = event.target.closest('.ps5-autoload-check');
    if (!box || !box.dataset.ps5File) return;
    const draft = ps5EnsureDraft(host);
    if (box.checked) draft.files.add(box.dataset.ps5File);
    else draft.files.delete(box.dataset.ps5File);
    syncPs5AutoloadMeta(host);
    updatePs5Controls();
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
    if (!host || ps5Busy || !authenticated) return;
    const values = ps5AutoloadValues(host);
    const files = [...values.files].sort((left, right) => left.localeCompare(right, 'zh', { numeric: true }));
    const overwrite = !sameStringSet(files, host.autoload.list);
    if (overwrite) {
      ps5AutoloadTarget = { ip: host.ip, enabled: values.enabled, files };
      $('ps5-autoload-dialog-host').textContent = host.ip;
      $('ps5-autoload-dialog-current').textContent = String(host.autoload.list.length);
      $('ps5-autoload-dialog-next').textContent = String(files.length);
      const removed = host.autoload.list.filter((name) => !files.includes(name));
      $('ps5-autoload-dialog-note').textContent = removed.length
        ? `将移除：${removed.join('、')}`
        : '列表按文件名字母序保存。';
      $('ps5-autoload-dialog-error').textContent = '';
      $('ps5-autoload-dialog-error').hidden = true;
      $('ps5-autoload-dialog-cancel').disabled = false;
      $('ps5-autoload-dialog-confirm').disabled = false;
      ps5RememberFocus();
      $('ps5-autoload-dialog').showModal();
      $('ps5-autoload-dialog-cancel').focus({ preventScroll: true });
      return;
    }
    submitPs5Autoload(host.ip, values.enabled, files);
  }

  async function submitPs5Autoload(ip, enabled, files) {
    if (ps5Busy || !authenticated) return;
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
      const result = await api('/api/ps5/autoload', { ip, enabled: Boolean(enabled), files });
      if (requestEpoch !== epoch) return;
      if (result.ok !== true) throw new ApiError('服务未确认自动加载设置，请刷新后检查。');
      if (result.host) updatePs5Host(result.host);
      ps5Draft = null;
      saved = true;
      if (dialog.open) dialog.close();
      toast('自动加载设置已保存。', 'success');
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
    try {
      const result = await api('/api/ps5/install', { ip, filename, run: Boolean(run) });
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
    ps5Begin('shutdown', ip, { label: '正在发送关机请求…' });
    $('ps5-shutdown-error').textContent = '';
    $('ps5-shutdown-error').hidden = true;
    $('ps5-shutdown-cancel').disabled = true;
    $('ps5-shutdown-confirm').disabled = true;
    buttonBusy('ps5-shutdown-confirm', true, '关机中…');
    const requestEpoch = epoch;
    let requested = false;
    try {
      const result = await api('/api/ps5/shutdown', { ip });
      if (requestEpoch !== epoch) return;
      if (result.ok !== true) throw new ApiError('服务未确认关机请求，请刷新后检查。');
      requested = true;
      dialog.close();
      toast(`已请求 ${ip} 关机。`, 'success');
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
      buttonBusy('ps5-shutdown-confirm', false, '确认关机');
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

  function onPs5ViewClick(event) {
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
      const total = Number(entry.total);
      const free = Number(entry.free);
      volumes.push({
        path,
        label: str(entry.label) || filesVolumeLabel(str(entry.kind)),
        kind: str(entry.kind) || 'system',
        total: Number.isFinite(total) && total >= 0 ? total : null,
        free: Number.isFinite(free) && free >= 0 ? free : null
      });
    }
    return volumes;
  }

  function normalizeFilesEntries(data, fallbackPath) {
    const path = str(data && data.path) || fallbackPath;
    const entries = [];
    for (const item of Array.isArray(data && data.entries) ? data.entries : []) {
      if (!item || typeof item !== 'object') continue;
      const name = str(item.name);
      if (!name) continue;
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
      ? (info.reachable ? `已连接 ${info.host || '主机'}${info.base ? ` · ${info.base}` : ''}` : `无法连接 ${info.host || '文件服务'}`)
      : '选择要浏览的 PS5 主机。';
    if (showSetup) renderFilesSetup();
    if (showUnreachable) {
      $('files-unreachable-text').textContent = info.error || `无法连接 ${info.host || '文件服务'}，请确认主机已开机并运行 pldmgr。`;
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
      const isActive = Boolean(active && active.path === volume.path);
      if (isActive) button.dataset.active = 'true';
      button.setAttribute('aria-pressed', String(isActive));
      button.setAttribute('aria-label', `${volume.label}（${volume.path}），可用 ${formatBytes(volume.free)}，共 ${formatBytes(volume.total)}`);
      const top = makeElement('div', 'files-vol-top');
      top.append(makeIcon(filesVolumeIcon(volume.kind)), makeElement('span', 'files-vol-name', volume.label));
      const sub = makeElement('div', 'files-vol-sub');
      sub.append(makeElement('span', 'mono', volume.path), makeElement('span', '', filesVolumeLabel(volume.kind)));
      const bar = makeElement('div', 'usage');
      const fill = document.createElement('i');
      fill.style.width = `${used === null ? 0 : used.toFixed(1)}%`;
      if (used !== null && used >= 92) fill.className = 'full';
      bar.append(fill);
      const foot = makeElement('p', 'files-vol-foot num', `可用 ${formatBytes(volume.free)} · 共 ${formatBytes(volume.total)}`);
      button.append(top, sub, bar, foot);
      button.addEventListener('click', () => navigateFiles(volume.path));
      fragment.append(button);
    }
    box.replaceChildren(fragment);
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
    fragment.append(addCrumb('概览', volume.path, files.path === volume.path));
    const relative = files.path.slice(volume.path.length).split('/').filter(Boolean);
    let at = volume.path;
    for (const part of relative) {
      at += `/${part}`;
      fragment.append(makeElement('span', 'files-crumb-sep', '/'));
      fragment.append(addCrumb(part, at, at === files.path));
    }
    box.replaceChildren(fragment);
  }

  function filesVisibleEntries() {
    const needle = files.filter.trim().toLowerCase();
    const list = needle ? files.entries.filter((entry) => entry.name.toLowerCase().includes(needle)) : files.entries.slice();
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
      nameWrap.append(open);
    } else {
      nameWrap.append(makeElement('span', 'files-name-text', entry.name));
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
    setButton('files-job-cancel', !files.jobActive || files.busy);
    setButton('files-name-cancel', files.busy);
    setButton('files-dest-cancel', files.busy);
    setButton('files-host-cancel', files.configBusy);
    for (const button of $$('#files-body [data-files-menu-button]')) button.disabled = locked || !mainReady;
    for (const item of $$('#files-menu [data-files-menu]')) item.disabled = locked || !files.menuTarget;
    $('files-transfers-toggle').setAttribute('aria-expanded', String(files.drawer));
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

  async function loadFilesDest(path) {
    if (!authenticated) return;
    files.destPath = path;
    $('files-dest-path').textContent = path || '存储位置';
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
    $('files-transfers').hidden = false;
    renderFilesTransfers();
    ensureFilesPolling();
    filesTransfersTick();
  }

  async function filesTransfersTick() {
    if (!authenticated || currentSection !== 'files' || filesPollBusy) return;
    if (!files.drawer && !files.jobActive && !files.uploading) return;
    filesPollBusy = true;
    const requestEpoch = epoch;
    try {
      if (files.drawer || files.uploading) {
        const data = await api('/api/files/transfers');
        if (requestEpoch !== epoch) return;
        files.transfers = normalizeFilesTransfers(data);
        files.transfersError = '';
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
    const should = authenticated && currentSection === 'files' && (files.drawer || files.jobActive || files.uploading);
    if (should && !filesPollTimer) filesPollTimer = setInterval(filesTransfersTick, 1200);
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
    buttonBusy('files-job-cancel', true, '取消中…');
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
      buttonBusy('files-job-cancel', false, '取消');
      updateFilesControls();
      renderFilesTransfers();
    }
    filesTransfersTick();
  }

  function filesUploadRow(task) {
    const row = document.createElement('tr');
    row.dataset.uploadId = String(task.id);
    const typeCell = makeElement('td', '', filesKindLabel(task.kind));
    const nameCell = makeElement('td', 'files-transfer-name', task.name);
    nameCell.title = task.name;
    const sizeCell = makeElement('td', 'num', formatBytes(task.size));
    const progressCell = document.createElement('td');
    const progress = makeElement('div', 'files-progress');
    const fill = document.createElement('i');
    const percent = task.size > 0 ? Math.min(100, (task.done / task.size) * 100) : task.state === 'done' ? 100 : 0;
    fill.style.width = `${percent}%`;
    progress.append(fill);
    progressCell.append(progress);
    const stateCell = makeElement('td', task.state === 'fail' ? 'bad' : task.state === 'done' ? 'good' : 'mut');
    stateCell.textContent = task.state === 'run' ? `上传中 ${percent.toFixed(percent < 10 ? 1 : 0)}%` : asText(task.state === 'sync' ? '正在落盘' : filesStateLabel(task.state), '');
    if (task.note) stateCell.title = task.note;
    if (task.state === 'fail' && task.note) stateCell.textContent = `失败：${task.note}`;
    const speedCell = makeElement('td', 'num mut', task.speed > 0 ? `${task.speed.toFixed(1)} MB/s` : '—');
    row.append(typeCell, nameCell, sizeCell, progressCell, stateCell, speedCell);
    return row;
  }

  function filesTransferRow(item) {
    const row = document.createElement('tr');
    const typeCell = makeElement('td', '', filesKindLabel(item.kind));
    const nameCell = makeElement('td', 'files-transfer-name', item.name);
    nameCell.title = item.name;
    const sizeCell = makeElement('td', 'num', item.size === null ? '—' : formatBytes(item.size));
    const progressCell = document.createElement('td');
    const progress = makeElement('div', 'files-progress');
    const fill = document.createElement('i');
    const size = item.size === null ? 0 : item.size;
    const percent = size > 0 && item.done !== null ? Math.min(100, (item.done / size) * 100) : item.state === 'done' ? 100 : 0;
    fill.style.width = `${percent}%`;
    progress.append(fill);
    progressCell.append(progress);
    const stateCell = makeElement('td');
    const stateName = str(item.state).toLowerCase();
    stateCell.className = stateName === 'done' ? 'good' : stateName === 'failed' ? 'bad' : 'mut';
    stateCell.textContent = filesStateLabel(item.state);
    const speed = Number.isFinite(item.speed) && item.speed > 0
      ? `${item.speed.toFixed(1)} MB/s`
      : item.done !== null && item.ms > 0 ? `${(item.done / item.ms / 1000).toFixed(1)} MB/s` : '—';
    const speedCell = makeElement('td', 'num mut', speed);
    row.append(typeCell, nameCell, sizeCell, progressCell, stateCell, speedCell);
    return row;
  }

  function renderFilesJob() {
    const box = $('files-job');
    const job = files.job;
    const show = Boolean(job && job.state !== 'idle');
    if (!show) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    $('files-job-label').textContent = `${job.kind === 'move' ? '移动' : '复制'}任务`;
    $('files-job-fill').style.width = `${job.percent === null ? 0 : job.percent}%`;
    const parts = [filesStateLabel(job.state)];
    if (job.percent !== null) parts.push(`${job.percent.toFixed(job.percent < 10 ? 1 : 0)}%`);
    if (job.done !== null && job.total !== null) parts.push(`${formatBytes(job.done)} / ${formatBytes(job.total)}`);
    if (job.error) parts.push(job.error);
    $('files-job-text').textContent = parts.join(' · ');
    $('files-job-cancel').disabled = !files.jobActive || files.busy;
  }

  function renderFilesTransfers() {
    if (!$('files-transfers')) return;
    const activeUploads = files.uploads.filter((task) => ['wait', 'run', 'sync'].includes(task.state));
    const activeTransfers = files.transfers.filter((item) => String(item.state).toLowerCase() === 'active');
    const count = activeUploads.length + activeTransfers.length + (files.jobActive ? 1 : 0);
    const badge = $('files-transfers-count');
    badge.textContent = count ? String(count) : '';
    badge.hidden = !count;
    $('files-transfers').hidden = !files.drawer;
    $('files-transfers-toggle').setAttribute('aria-expanded', String(files.drawer));
    if (!files.drawer) return;
    renderFilesJob();
    const body = $('files-transfers-body');
    const localNames = new Set(files.uploads.map((task) => task.name));
    const rows = [];
    for (const task of files.uploads) rows.push(filesUploadRow(task));
    for (const item of files.transfers) {
      if (String(item.kind).toLowerCase() === 'upload' && localNames.has(item.name)) continue;
      rows.push(filesTransferRow(item));
    }
    if (!rows.length) body.replaceChildren(tableEmptyRow(6, '暂无传输任务。'));
    else body.replaceChildren(...rows);
    const error = $('files-transfers-error');
    error.textContent = files.transfersError || '';
    error.hidden = !files.transfersError;
    updateFilesControls();
  }

  function toggleFilesDrawer(force) {
    files.drawer = typeof force === 'boolean' ? force : !files.drawer;
    renderFilesTransfers();
    ensureFilesPolling();
    if (files.drawer) filesTransfersTick();
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
        note: '',
        file,
        overwrite: overwrite.has(file.name)
      });
    }
    files.drawer = true;
    renderFilesTransfers();
    ensureFilesPolling();
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
        if (now - lastAt >= 200) {
          task.speed = ((event.loaded - lastBytes) / 1048576) / ((now - lastAt) / 1000);
          lastAt = now;
          lastBytes = event.loaded;
          renderFilesTransfers();
        }
      });
      xhr.addEventListener('load', () => {
        const payload = xhr.response && typeof xhr.response === 'object' ? xhr.response : {};
        if (xhr.status >= 200 && xhr.status < 300 && typeof payload.error !== 'string') {
          task.done = task.size;
          task.speed = (task.size / 1048576) / Math.max(0.001, (performance.now() - started) / 1000);
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
    $('files-transfers-body').replaceChildren(tableEmptyRow(6, '暂无传输任务。'));
    $('files-job').hidden = true;
    $('files-transfers-error').textContent = '';
    $('files-transfers-error').hidden = true;
    $('files-drop').hidden = true;
    closeFilesMenu({ restoreFocus: false });
    renderFiles();
    updateFilesControls();
  }



  /* ── 事件绑定 ───────────────────────────────────────────────────────── */

  function bindEvents() {
    $('login-form').addEventListener('submit', submitLogin);

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
    for (const button of acts('upload')) button.addEventListener('click', openUploadDialog);

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

    for (const button of acts('allow')) button.addEventListener('click', openActivityAllowDialog);
    for (const button of acts('label')) button.addEventListener('click', openActivityLabelDialog);
    $('activity-filter').addEventListener('change', () => setActivityFilter($('activity-filter').value));
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
    $('ps5-tabs').addEventListener('keydown', onPs5TabsKeydown);
    $('ps5-autoload-enabled').addEventListener('change', onPs5AutoloadToggle);
    $('ps5-autoload-items').addEventListener('change', onPs5AutoloadFile);
    $('ps5-autoload-save').addEventListener('click', requestPs5AutoloadSave);
    $('ps5-shutdown-cancel').addEventListener('click', () => $('ps5-shutdown-dialog').close());
    $('ps5-shutdown-confirm').addEventListener('click', confirmPs5Shutdown);
    $('ps5-kill-cancel').addEventListener('click', () => $('ps5-kill-dialog').close());
    $('ps5-kill-confirm').addEventListener('click', confirmPs5Kill);
    $('ps5-autoload-dialog-cancel').addEventListener('click', () => $('ps5-autoload-dialog').close());
    $('ps5-autoload-dialog-confirm').addEventListener('click', () => {
      const target = ps5AutoloadTarget;
      if (target) submitPs5Autoload(target.ip, target.enabled, target.files);
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
    $('files-transfers-toggle').addEventListener('click', () => toggleFilesDrawer());
    $('files-transfers-close').addEventListener('click', () => toggleFilesDrawer(false));
    $('files-setup-connect').addEventListener('click', connectFilesSetup);
    $('files-setup-host').addEventListener('change', () => setFilesSetupError(''));
    $('files-retry').addEventListener('click', () => {
      setFilesError('');
      loadFiles({ manual: true });
    });
    $('files-switch-host').addEventListener('click', openFilesHostDialog);
    $('files-job-cancel').addEventListener('click', cancelFilesJob);
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

  /* ── 启动 ───────────────────────────────────────────────────────────── */

  bindEvents();
  renderRepo();
  updateRepoControls();
  renderActivity();
  renderPs5();
  renderFiles();
  updateControls();
  refreshState();
})();
