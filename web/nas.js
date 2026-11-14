'use strict';

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
    nas.backupRoots = Array.isArray(data.backup_roots) ? data.backup_roots.filter((item) => typeof item === 'string') : [];
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
    const action = nasRowAction(entry);
    if (action) wrap.append(action);
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
  $('files-dest-cheats-row').hidden = true;
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
  $('files-dest-cheats-row').hidden = true;
  $('files-dest-cheats').checked = false;
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
    loadNasCheatPlan(item.id, entry.path, target);
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

// Cheat ZIPs found next to the archive become an optional last step of the extraction.
async function loadNasCheatPlan(nasId, path, target) {
  try {
    const plan = await api(`/api/nas/plan?id=${encodeURIComponent(nasId)}&paths=${encodeURIComponent(path)}`);
    if (files.destTarget !== target) return;
    target.cheats = Array.isArray(plan.cheats) ? plan.cheats.filter((item) => typeof item === 'string') : [];
    if (!target.cheats.length) return;
    $('files-dest-cheats-label').textContent = target.cheats.length === 1
      ? `同时导入金手指（${target.cheats[0].split('/').pop()}）`
      : `同时导入金手指（${target.cheats.length} 个 ZIP）`;
    $('files-dest-cheats').checked = true;
    $('files-dest-cheats-row').hidden = false;
  } catch (error) {
    // Optional: the extraction works without it.
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
      mode: $('files-dest-extract-mode').value === 'merge' ? 'merge' : 'install',
      cheats: !$('files-dest-cheats-row').hidden && $('files-dest-cheats').checked ? target.cheats || [] : []
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
  if (done && job.cheat_result && Array.isArray(job.cheat_result.written) && job.cheat_result.written.length) {
    line += `${line ? ' · ' : ''}已导入 ${job.cheat_result.written.length} 个金手指文件，需要在 etaHEN 里重新加载金手指`;
  }
  if (job.cheat_error) line += `${line ? ' · ' : ''}${job.cheat_error}`;
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

const PACKAGE_STEPS = [['prepare', '准备'], ['transfer', '传输'], ['install', '安装'], ['verify', '确认']];

function packageTask(job) {
  const done = job.state === 'done' || job.state === 'done_with_warnings';
  const index = PACKAGE_STEPS.findIndex(([key]) => key === job.phase);
  let line = '';
  if (XFER_ACTIVE.has(job.state)) {
    line = PACKAGE_STEPS.map(([key, label], at) => (at === index ? `【${label}】` : label)).join(' → ');
  }
  if (done) line = job.warning || `已安装到 PS5 ${job.host || ''}`.trim();
  if (job.state === 'failed' && job.error_code === 'busy') line = 'ps5upload 引擎正忙，等另一个安装结束后点「重试」';
  return {
    key: `nas:${job.id}`, kind: 'package', name: job.name, state: job.state,
    sub: `${job.source} → PS5 ${job.host || ''}`.trim(),
    done: job.bytes_done, total: job.bytes_total, speed: job.speed, elapsed: job.elapsed, eta: job.eta,
    line, error: job.error, errors: job.errors || [],
    interrupted: job.interrupted === true,
    cancel: XFER_ACTIVE.has(job.state) ? () => cancelNasJob(job.id) : null,
    retry: ['failed', 'canceled'].includes(job.state) ? () => retryNasJob(job.id) : null
  };
}

const NAS_PACKAGE = /\.pkg$/i;
const NAS_CHEAT = /\.zip$/i;
const NAS_CHEAT_NAME = /金手指|cheat|trainer/i;

function nasRowAction(entry) {
  if (entry.dir || entry.name.startsWith('._')) return null;
  let label = '';
  let handler = null;
  if (NAS_PACKAGE.test(entry.name)) {
    label = '安装 PKG';
    handler = () => installNasPackage(entry);
  } else if (NAS_CHEAT.test(entry.name) && NAS_CHEAT_NAME.test(entry.name)) {
    label = '导入金手指';
    handler = () => importNasCheats(entry);
  } else {
    return null;
  }
  const button = makeElement('button', 'button button-secondary button-small nas-row-action', label);
  button.type = 'button';
  button.setAttribute('aria-label', `${label}：${entry.name}`);
  button.disabled = !authenticated || nas.busy || !(files.info && files.info.reachable);
  button.addEventListener('click', handler);
  return button;
}

async function installNasPackage(entry) {
  const item = nasActive();
  const host = files.info && files.info.host;
  if (!authenticated || !item || !host) return;
  if (!window.confirm(`把 ${entry.name}（${formatBytes(entry.size)}）安装到 PS5 ${host}？\n\n由 ps5upload 引擎安装，主机直接从 NAS 读取 PKG，不占服务器磁盘。需要 PS5 上正在运行 ps5upload 插件；安装期间请保持 PS5 开机。`)) return;
  nas.busy = true;
  renderNasPane();
  try {
    const result = await api('/api/nas/install', { id: item.id, paths: [entry.path], host }, 90000);
    if (result.job && result.job.id) nasJobs = [result.job, ...nasJobs.filter((job) => job.id !== result.job.id)];
    toast(`已开始安装 ${entry.name}，进度见传输任务。`, 'success');
    files.drawer = true;
    renderFilesTransfers();
    ensureFilesPolling();
    revealFilesTransfers();
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(error.message, 'error');
  } finally {
    nas.busy = false;
    renderNasPane();
  }
}

async function importNasCheats(entry) {
  const item = nasActive();
  if (!authenticated || !item) return;
  if (!window.confirm(`把 ${entry.name} 里的金手指写入 PS5 的 /data/etaHEN/cheats？同名文件会被覆盖。`)) return;
  nas.busy = true;
  renderNasPane();
  try {
    const result = await api('/api/nas/cheats', { id: item.id, path: entry.path, title: titleIdOf(entry.name) || '' }, 60000);
    const written = Array.isArray(result.written) ? result.written.length : 0;
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    if (written) {
      toast(`已导入 ${written} 个金手指文件。${str(result.note)}${warnings.length ? `（${warnings.length} 条提醒：${str(warnings[0])}）` : ''}`, warnings.length ? 'info' : 'success');
    } else {
      toast(`没有导入任何金手指：${str(warnings[0]) || 'ZIP 里没有 etaHEN 金手指文件'}`, 'error');
    }
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(error.message, 'error');
  } finally {
    nas.busy = false;
    renderNasPane();
  }
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
    const kind = result.job && result.job.kind;
    toast(kind === 'package' ? '已重新排队安装。' : kind === 'migrate' ? '已重新排队迁移。' : '已重新排队，PS5 上已有的同名文件会跳过。', 'success');
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
    verify_tls: $('nas-verify').checked,
    writable: $('nas-writable').checked
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
  $('nas-writable').checked = Boolean(item && item.writable === true);
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
    $('nas-dialog-status').textContent = `连接成功，起始路径下有 ${result.entries} 项${result.write === 'ok' ? '，写入测试通过' : ''}。`;
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

/* ── PS5 → NAS 备份 ─────────────────────────────────────────────────── */

const nasBackup = { busy: false, entry: null };

function backupSources(entry) {
  const list = [];
  if (entry && entry.path) list.push({ path: entry.path, label: entry.path, checked: true });
  for (const root of nas.backupRoots || []) {
    if (!list.some((item) => item.path === root)) list.push({ path: root, label: `${root}（预设，只读）`, checked: false });
  }
  return list;
}

async function openBackupDialog(entry) {
  if (!authenticated) return;
  if (!nas.loaded) await refreshNas();
  const writable = nas.connections.filter((item) => item.writable === true);
  if (!writable.length) {
    toast('还没有允许写入的 NAS 连接：在 NAS 存储里编辑连接，勾选「允许写入」并测试。', 'error');
    return;
  }
  nasBackup.entry = entry;
  $('backup-nas-sources').replaceChildren(...backupSources(entry).map((source) => {
    const wrapper = makeElement('label', 'nas-check');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = source.path;
    input.checked = source.checked;
    wrapper.append(input, makeElement('span', '', source.label));
    return wrapper;
  }));
  const select = $('backup-nas-target');
  select.replaceChildren(...writable.map((item) => {
    const option = makeElement('option', '', str(item.name) || str(item.host));
    option.value = item.id;
    return option;
  }));
  showFieldMessage('backup-nas-error', '');
  $('backup-nas-dialog').showModal();
  $('backup-nas-target').focus({ preventScroll: true });
}

function backupPayload() {
  return {
    id: $('backup-nas-target').value,
    paths: $$('#backup-nas-sources input').filter((input) => input.checked).map((input) => input.value),
    dest: $('backup-nas-dest').value.trim() || '/',
    mode: $('backup-nas-mode').value === 'snapshot' ? 'snapshot' : 'mirror',
    host: files.info && files.info.host ? files.info.host : undefined
  };
}

async function startBackup(event) {
  event.preventDefault();
  if (nasBackup.busy) return;
  const payload = backupPayload();
  if (!payload.paths.length) {
    showFieldMessage('backup-nas-error', '请至少选择一个备份来源。');
    return;
  }
  nasBackup.busy = true;
  buttonBusy('backup-nas-start', true, '提交中…');
  try {
    const result = await api('/api/nas/backup', payload);
    if (result.job && result.job.id) nasJobs = [result.job, ...nasJobs.filter((job) => job.id !== result.job.id)];
    $('backup-nas-dialog').close();
    toast('已开始备份，进度见传输任务。', 'success');
    files.drawer = true;
    renderFilesTransfers();
    ensureFilesPolling();
    revealFilesTransfers();
  } catch (error) {
    if (error.status === 401) { $('backup-nas-dialog').close(); showLogin(error.message); return; }
    showFieldMessage('backup-nas-error', error.message);
  } finally {
    nasBackup.busy = false;
    buttonBusy('backup-nas-start', false, '开始备份');
  }
}

function backupTask(job) {
  const done = job.state === 'done' || job.state === 'done_with_warnings';
  return {
    key: `nas:${job.id}`, kind: 'backup', name: job.name, state: job.state,
    sub: `${job.source} → ${str(job.nas_name) || 'NAS'} ${job.target || job.dest}${job.mode === 'snapshot' ? '（快照）' : ''}`,
    done: job.bytes_done, total: job.bytes_total, speed: job.speed, elapsed: job.elapsed, eta: job.eta,
    files: { done: job.files_done, total: job.files_total, skipped: job.files_skipped, failed: job.files_failed, retries: job.retries || 0, resumed: 0 },
    current: job.state === 'running' ? job.current : '',
    line: done && job.files_skipped ? `${job.files_skipped} 个文件与 NAS 上的相同，已跳过` : '',
    error: job.error, errors: job.errors || [],
    interrupted: job.interrupted === true,
    cancel: XFER_ACTIVE.has(job.state) ? () => cancelNasJob(job.id) : null,
    retry: ['failed', 'canceled'].includes(job.state) ? () => retryNasJob(job.id) : null
  };
}
