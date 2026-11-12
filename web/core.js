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
const SECTIONS = ['overview', 'policy', 'activity', 'ps5', 'files', 'library', 'repository', 'settings'];
const views = {
  overview: $('view-overview'),
  policy: $('view-policy'),
  activity: $('view-activity'),
  ps5: $('view-ps5'),
  files: $('view-files'),
  library: $('view-library'),
  repository: $('view-repository'),
  settings: $('view-settings')
};
const SECTION_META = {
  overview: { title: '概览', subtitle: '服务状态、运行时间与最近请求' },
  policy: { title: '访问策略', subtitle: '域名、IPv4 / CIDR 白名单与上游 DNS' },
  activity: { title: '网络活动', subtitle: '网关连接、DNS 查询与本机服务事件' },
  ps5: { title: 'PS5 主机', subtitle: '发现局域网主机，安装并运行插件载荷' },
  files: { title: 'PS5 文件', subtitle: '浏览主机存储、上传与整理文件' },
  library: { title: '游戏库', subtitle: 'PS5 存储上的游戏文件夹、大小与封面' },
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
const nas = { loaded: false, backupRoots: [], connections: [], protocols: { webdav: true, smb: true, ftp: true }, activeId: '', path: '/', entries: [], selected: new Set(), loading: false, error: '', editing: null, busy: false, seq: 0 };
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
