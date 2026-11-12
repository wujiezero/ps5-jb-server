'use strict';

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
    backup: `把 ${entry.name} 备份到 NAS`,
    delete: `删除 ${entry.name}`
  };
  for (const item of $$('#files-menu [data-files-menu]')) {
    const action = item.dataset.filesMenu;
    item.disabled = false;
    // Only folders can be backed up; hidden rather than disabled so the sheet stays short.
    if (action === 'backup') item.hidden = !entry.dir;
    item.setAttribute('aria-label', labels[action] || action);
  }
  if (isPhone()) {
    // Same actions as the popover, presented as a bottom sheet.
    const items = $$('#files-menu [data-files-menu]').filter((item) => !item.hidden).map((item) => {
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
  else if (action === 'backup') openBackupDialog(entry);
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
  $('files-dest-cheats-row').hidden = true;
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
const XFER_KIND = { upload: '上传', copy: '复制', move: '移动', nas: 'NAS → PS5', extract: '解压安装', package: 'PKG 安装', backup: 'PS5 → NAS', remote: 'PS5' };

function filesTransferTasks() {
  const tasks = [];
  for (const job of nasJobs) {
    if (job.kind === 'extract') {
      tasks.push(extractTask(job));
      continue;
    }
    if (job.kind === 'package') {
      tasks.push(packageTask(job));
      continue;
    }
    if (job.kind === 'backup') {
      tasks.push(backupTask(job));
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
