'use strict';

/* ── 游戏库 ─────────────────────────────────────────────────────────── */

const library = { loaded: false, loading: false, games: [], volumes: [], errors: [], sizing: '', host: '', scanPaths: [], scanConfig: {},
  apps: { loaded: false, loading: false, available: false, error: '', titles: [] } };

function normalizeLibrary(data) {
  const games = Array.isArray(data && data.games) ? data.games.filter((game) => game && typeof game.path === 'string') : [];
  return {
    games: games.map((game) => ({
      title: str(game.title), name: str(game.name) || str(game.folder) || game.path.split('/').pop(),
      folder: str(game.folder), path: game.path, volume: str(game.volume), volumeLabel: str(game.volume_label) || str(game.volume),
      platform: str(game.platform), mtime: Number(game.mtime) || 0,
      size: Number.isFinite(game.size) ? game.size : null,
      kind: game.kind === 'image' ? 'image' : 'folder', format: str(game.format), scanRoot: str(game.scan_root)
    })),
    volumes: Array.isArray(data && data.volumes) ? data.volumes.filter((v) => v && typeof v.path === 'string') : [],
    errors: Array.isArray(data && data.errors) ? data.errors.map(str) : [],
    host: str(data && data.host),
    scanPaths: Array.isArray(data && data.scan_paths) ? data.scan_paths.filter((item) => item && typeof item.path === 'string').map((item) => ({
      path: item.path, volume: str(item.volume), volumeLabel: str(item.volume_label) || str(item.volume),
      exists: item.exists !== false, games: Number(item.games) || 0
    })) : [],
    scanConfig: data && data.scan_config && typeof data.scan_config === 'object' ? {
      source: str(data.scan_config.source), file: str(data.scan_config.file), depth: Number(data.scan_config.depth) || 1,
      skipped: Array.isArray(data.scan_config.skipped) ? data.scan_config.skipped.map(str) : []
    } : { source: '', file: '', depth: 1, skipped: [] }
  };
}

function libraryVisible() {
  const text = $('library-search').value.trim().toLowerCase();
  const volume = $('library-volume').value;
  const match = (game) => !text || game.name.toLowerCase().includes(text) || game.title.toLowerCase().includes(text) || game.path.toLowerCase().includes(text);
  const list = library.games.filter((game) => (!volume || game.scanRoot === volume) && match(game));
  if (!volume || volume === 'pkg') list.push(...libraryPackages().filter(match));
  if (volume === 'pkg') return list.filter((game) => game.pkg);
  if ($('library-sort').value === 'size') {
    list.sort((a, b) => (b.size === null ? -1 : b.size) - (a.size === null ? -1 : a.size) || a.name.localeCompare(b.name));
  } else {
    list.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN') || a.path.localeCompare(b.path));
  }
  return list;
}

function libraryCover(game) {
  const blank = makeElement('span', 'library-cover library-cover-blank', game.platform || '游戏');
  if (!game.title) return blank;
  const image = document.createElement('img');
  image.className = 'library-cover';
  image.alt = '';
  image.loading = 'lazy';
  image.decoding = 'async';
  image.addEventListener('error', () => image.replaceWith(blank));
  image.src = `/api/library/icon?path=${encodeURIComponent(game.path)}`;
  return image;
}

function libraryCard(game) {
  const node = makeElement('li', 'library-card');
  node.dataset.path = game.path || `pkg:${game.title}`;
  node.append(game.pkg ? makeElement('span', 'library-cover library-cover-blank', game.platform || 'PKG') : libraryCover(game),
    makeElement('span', 'library-name', game.name));
  const ids = [game.title, game.platform].filter(Boolean).join(' · ');
  node.append(makeElement('span', 'library-meta', ids || game.folder));
  if (game.pkg) {
    node.append(makeElement('span', 'library-meta', 'PKG 安装 · 由主机管理'));
    const actions = makeElement('div', 'library-actions');
    const remove = makeElement('button', 'button button-secondary button-small', '卸载');
    remove.type = 'button';
    remove.setAttribute('aria-label', `卸载：${game.name}`);
    remove.disabled = !authenticated;
    remove.addEventListener('click', () => openUninstallDialog([libraryKey(game)]));
    actions.append(remove);
    node.append(actions);
    return node;
  }
  const registered = libraryRegistration(game);
  const form = game.kind === 'image' ? `镜像 ${game.format.toUpperCase()} · ` : '';
  node.append(makeElement('span', 'library-meta', `${form}${volumeKind(game.volume, game.volumeLabel)} · ${game.path}${registered ? ' · 已注册图标' : ''}`));
  const actions = makeElement('div', 'library-actions');
  const button = (label, aria, handler, disabled = false) => {
    const element = makeElement('button', 'button button-secondary button-small', label);
    element.type = 'button';
    element.setAttribute('aria-label', `${aria}：${game.name}`);
    element.disabled = disabled || !authenticated;
    element.addEventListener('click', handler);
    return element;
  };
  const sizing = library.sizing === game.path;
  actions.append(button(game.size === null ? (sizing ? '计算中…' : '计算大小') : formatBytes(game.size), '计算大小',
    () => sizeLibraryGame(game), Boolean(library.sizing)));
  actions.append(button('打开文件夹', '在 PS5 文件页打开所在文件夹', () => openLibraryFolder(game)));
  actions.append(button('迁移', '迁移到其它扫描路径', () => openMigrateDialog([game.path]), library.scanPaths.length < 2));
  if (game.kind === 'folder' && game.title && game.folder === game.title) {
    actions.append(button('注册图标', '重新注册主屏幕图标', () => registerLibraryGame(game)));
  }
  actions.append(button('卸载', '卸载游戏', () => openUninstallDialog([libraryKey(game)])));
  node.append(actions);
  return node;
}

function renderLibrary() {
  const select = $('library-volume');
  const chosen = select.value;
  const option = (label, value) => {
    const element = makeElement('option', '', label);
    element.value = value;
    return element;
  };
  select.replaceChildren(option('全部路径', ''), ...library.scanPaths.filter((item) => item.games).map((item) => option(`${item.path}（${item.games}）`, item.path)),
    ...(libraryPackages().length ? [option('PKG 安装', 'pkg')] : []));
  select.value = [...select.options].some((item) => item.value === chosen) ? chosen : '';
  const visible = libraryVisible();
  $('library-grid').replaceChildren(...visible.map(libraryCard));
  $('library-grid').hidden = !visible.length;
  $('library-empty').hidden = visible.length > 0 || !library.loaded;
  $('library-empty').textContent = library.games.length ? '没有符合条件的游戏。' : 'ShadowMount+ 的扫描路径里没有找到游戏。';
  const known = library.games.filter((game) => game.size !== null);
  const total = known.reduce((sum, game) => sum + game.size, 0);
  const packages = libraryPackages().length;
  $('library-summary').textContent = library.loaded
    ? `${library.host ? `PS5 ${library.host} · ` : ''}${libraryCounts()}${packages ? `、${packages} 个 PKG 安装` : ''}${known.length ? ` · 已知大小 ${known.length} 个，共 ${formatBytes(total)}` : ''}`
    : 'ShadowMount+ 扫描路径里的游戏。';
  $('library-scan-note').textContent = library.loaded ? libraryScanNote() : '';
  $('library-scan-note').hidden = !$('library-scan-note').textContent;
  $('library-apps-note').textContent = library.apps.available
    ? 'PKG 安装的游戏来自 ps5upload 引擎读取的主机应用列表；系统应用不显示。'
    : `通过 PKG 安装的游戏由 ps5upload 引擎列出${library.apps.error ? `（现在不可用：${library.apps.error}）` : ''}；引擎不可用时这里只显示扫描路径里的游戏。`;
  $('library-migrate').disabled = !authenticated || library.scanPaths.length < 2;
  $('library-uninstall').disabled = !authenticated || (!library.games.length && !packages);
  $('library-warnings').textContent = library.errors.length ? `部分路径读取失败：${library.errors.join('；')}` : '';
  $('library-warnings').hidden = !library.errors.length;
}

function libraryCounts() {
  const images = library.games.filter((game) => game.kind === 'image').length;
  const folders = library.games.length - images;
  return [folders ? `${folders} 个游戏文件夹` : '', images ? `${images} 个镜像` : ''].filter(Boolean).join('、') || '0 个游戏';
}

// Where the list came from: ShadowMount+'s config.ini, or its built-in roots.
function libraryScanNote() {
  const config = library.scanConfig;
  const roots = library.scanPaths.map((item) => item.path + (item.exists ? '' : '（不存在）'));
  const from = config.source === 'config' ? `按 ShadowMount+ 配置（${config.file}）扫描`
    : config.source === 'missing' ? '没找到 ShadowMount+ 配置，按它的默认路径和 [ps5] game_dirs 扫描'
      : 'ShadowMount+ 配置里没有扫描路径，按它的默认路径扫描';
  const skipped = config.skipped.length ? `；${config.skipped.join('、')} 不在 /data、/mnt/ext*、/mnt/usb* 上，读不到` : '';
  return `${from}，深度 ${config.depth}：${roots.join('、') || '（主机上没有这些路径的存储）'}${skipped}。`;
}

async function loadLibrary(refresh = false) {
  if (!authenticated || library.loading) return;
  library.loading = true;
  buttonBusy('library-refresh', true, '扫描中…');
  $('library-error').hidden = true;
  try {
    const data = await api(`/api/library${refresh ? '?refresh=1' : ''}`, undefined, 60000);
    Object.assign(library, normalizeLibrary(data), { loaded: true });
    loadLibraryApps();
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    $('library-error').textContent = `游戏库读取失败：${error.message}`;
    $('library-error').hidden = false;
  } finally {
    library.loading = false;
    buttonBusy('library-refresh', false, '重新扫描');
    renderLibrary();
  }
}

async function sizeLibraryGame(game) {
  if (library.sizing) return;
  library.sizing = game.path;
  renderLibrary();
  try {
    const data = await api(`/api/library/size?path=${encodeURIComponent(game.path)}`, undefined, 600000);
    game.size = Number(data.size) || 0;
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(error.message, 'error');
  } finally {
    library.sizing = '';
    renderLibrary();
  }
}

function openLibraryFolder(game) {
  const parent = game.path.slice(0, game.path.lastIndexOf('/')) || game.volume;
  location.hash = '#/files';
  let tries = 0;
  const go = () => {
    if (files.loaded && !files.loading && !files.busy) { navigateFiles(parent); return; }
    if ((tries += 1) < 40) setTimeout(go, 250);
  };
  go();
}

function whenClosed(id, handler) {
  $(id).addEventListener('close', handler, { once: true });
}

async function registerLibraryGame(game) {
  try {
    await api('/api/library/register', { path: game.path, host: library.host }, 90000);
    toast(`已在主屏幕注册 ${game.name}。`, 'success');
  } catch (error) {
    if (error.status === 401) { showLogin(error.message); return; }
    toast(error.message, 'error');
  }
}

/* ── 存储类型、PKG 安装与图标注册 ───────────────────────────────────── */

function volumeKind(path, label) {
  const root = str(path);
  const kind = root === '/data' ? '内置存储' : /^\/mnt\/ext\d+$/.test(root) ? 'M.2 / 扩展存储' : /^\/mnt\/usb\d+$/.test(root) ? 'USB 外接硬盘' : '';
  const name = str(label) && str(label) !== root ? str(label) : '';
  if (!kind) return name || root;
  return name && !name.includes(kind) ? `${name}（${root}）` : `${kind}（${root}）`;
}

// PKG installs as library cards: from the engine, not on any whitelisted volume.
function libraryPackages() {
  return library.apps.titles.filter((app) => app.origin === 'pkg' && !app.system).map((app) => ({
    pkg: true, title: app.title, name: app.name || app.title, folder: '', path: '', volume: '', volumeLabel: 'PKG 安装',
    platform: titlePlatform(app.title), size: null, mtime: 0, kind: 'pkg', format: '', scanRoot: ''
  }));
}

function titlePlatform(title) {
  if (!title || title.startsWith('NPXS')) return '';
  return /^(PPSA|ELAS|ECAS|ELJS|ECJS|ELJM|ECJM|ELKS|ECKS)/.test(title) ? 'PS5' : 'PS4';
}

const libraryKey = (game) => (game.pkg ? `pkg:${game.title}` : game.path);

function libraryRegistration(game) {
  if (!game.title || !library.apps.available) return null;
  return library.apps.titles.find((app) => app.title === game.title && app.origin === 'registered'
    && (game.kind === 'image' ? app.imageBacked : app.source === game.path || app.source.startsWith(`${game.path}/`))) || null;
}

async function loadLibraryApps() {
  if (!authenticated || library.apps.loading) return;
  library.apps.loading = true;
  try {
    const data = await api(`/api/library/apps${library.host ? `?host=${encodeURIComponent(library.host)}` : ''}`, undefined, 40000);
    const titles = Array.isArray(data && data.titles) ? data.titles.filter((app) => app && typeof app.title === 'string') : [];
    library.apps = {
      loaded: true, loading: false, available: data.available === true, error: str(data.error),
      titles: titles.map((app) => ({
        title: app.title, name: str(app.name), origin: str(app.origin), source: str(app.source).replace(/\/+$/, ''), system: app.system === true,
        imageBacked: app.image_backed === true || str(app.source).startsWith('/mnt/shadowmnt/')
      }))
    };
  } catch (error) {
    library.apps = { loaded: true, loading: false, available: false, error: error.message, titles: [] };
    if (error.status === 401) { showLogin(error.message); return; }
  }
  renderLibrary();
}

// files.js calls this when the last running NAS / migrate / uninstall job ends.
function libraryJobsSettled() {
  if (library.loaded && !$('view-library').hidden) loadLibrary(true);
  else library.loaded = false;
}

function gamePick(value, title, detail, checked) {
  const wrapper = makeElement('label', 'game-pick');
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.value = value;
  input.checked = checked;
  const text = makeElement('span', 'game-pick-text');
  text.append(makeElement('span', 'game-pick-name', title), makeElement('span', 'game-pick-meta', detail));
  wrapper.append(input, text);
  return wrapper;
}

function syncAllBox(listId, allId) {
  const boxes = $$(`#${listId} input`);
  const chosen = boxes.filter((box) => box.checked).length;
  $(allId).checked = boxes.length > 0 && chosen === boxes.length;
  $(allId).indeterminate = chosen > 0 && chosen < boxes.length;
  $(allId).disabled = !boxes.length;
}

function startedJobs(jobs, message) {
  const list = Array.isArray(jobs) ? jobs.filter((job) => job && job.id) : [];
  const ids = new Set(list.map((job) => job.id));
  nasJobs = [...list, ...nasJobs.filter((job) => !ids.has(job.id))];
  toast(message, 'success');
  files.drawer = true;
  renderFilesTransfers();
  ensureFilesPolling();
  revealFilesTransfers();
}

/* ── 迁移 ─────────────────────────────────────────────────────────── */

const libraryMigrate = { busy: false, chosen: new Set() };

async function openMigrateDialog(paths = []) {
  if (!authenticated) return;
  if (!library.loaded) await loadLibrary();
  if (!library.loaded) { toast('游戏库读取失败，请先在游戏库页面重新扫描。', 'error'); return; }
  if (library.scanPaths.length < 2) {
    toast('ShadowMount+ 只配置了一个能访问的扫描路径。在它的配置里加上 M.2 或 USB 硬盘上的路径后再迁移。', 'error');
    return;
  }
  const first = library.games.find((game) => game.path === paths[0]);
  const withGames = library.scanPaths.find((item) => library.games.some((game) => game.scanRoot === item.path));
  const source = first ? first.scanRoot : (withGames || library.scanPaths[0]).path;
  const option = (label, value) => { const element = makeElement('option', '', label); element.value = value; return element; };
  $('migrate-source').replaceChildren(...library.scanPaths.map((item) => option(scanPathLabel(item), item.path)));
  $('migrate-source').value = source;
  libraryMigrate.chosen = new Set(paths);
  $('migrate-delete').checked = true;
  $('migrate-register').checked = true;
  showFieldMessage('migrate-error', '');
  renderMigrateDialog(true);
  $('migrate-dialog').showModal();
  $('migrate-target').focus({ preventScroll: true });
}

function scanPathLabel(item, free = false) {
  const volume = library.volumes.find((entry) => entry.path === item.volume);
  const kind = volumeKind(item.volume, item.volumeLabel).replace(/（.*）$/, '');
  const room = free && volume && Number.isFinite(volume.free) ? ` · 剩余 ${formatBytes(volume.free)}` : '';
  return `${item.path}（${kind}${item.exists ? '' : '，尚未创建'}${room}）`;
}

function migrateTargetPath() {
  return library.scanPaths.find((item) => item.path === $('migrate-target').value) || null;
}

function renderMigrateDialog(resetTarget = false) {
  const source = library.scanPaths.find((item) => item.path === $('migrate-source').value) || library.scanPaths[0];
  const target = $('migrate-target');
  const previous = target.value;
  const others = library.scanPaths.filter((item) => item.path !== source.path);
  target.replaceChildren(...others.map((item) => {
    const element = makeElement('option', '', scanPathLabel(item, true));
    element.value = item.path;
    return element;
  }));
  // Internal -> the first external path; external -> internal; else the first other drive.
  const preferred = source.volume === '/data' ? others.find((item) => item.volume !== '/data')
    : others.find((item) => item.volume === '/data') || others.find((item) => item.volume !== source.volume);
  target.value = !resetTarget && others.some((item) => item.path === previous) ? previous : (preferred || others[0] || {}).path || '';
  const games = library.games.filter((game) => game.scanRoot === source.path);
  $('migrate-games').replaceChildren(...games.map((game) => gamePick(game.path, game.name,
    `${game.kind === 'image' ? `镜像 ${game.format.toUpperCase()} · ` : ''}${game.path}${game.size !== null ? ` · ${formatBytes(game.size)}` : ''}`,
    libraryMigrate.chosen.has(game.path))));
  if (!games.length) $('migrate-games').append(makeElement('p', 'empty-state', '这个扫描路径里没有游戏。'));
  syncAllBox('migrate-games', 'migrate-all');
  updateMigrateSummary();
}

function migrateChosen() {
  return library.games.filter((game) => $$('#migrate-games input').some((box) => box.checked && box.value === game.path));
}

function updateMigrateSummary() {
  const chosen = migrateChosen();
  libraryMigrate.chosen = new Set(chosen.map((game) => game.path));
  const target = migrateTargetPath();
  const sameDrive = Boolean(target) && chosen.length > 0 && chosen.every((game) => game.volume === target.volume);
  const volume = target && library.volumes.find((item) => item.path === target.volume);
  const known = chosen.filter((game) => game.size !== null);
  const total = known.reduce((sum, game) => sum + game.size, 0);
  const parts = [`已选 ${chosen.length} 个游戏`];
  if (known.length) parts.push(`已知大小 ${formatBytes(total)}${known.length < chosen.length ? `（另有 ${chosen.length - known.length} 个未计算）` : ''}`);
  if (volume && Number.isFinite(volume.free) && !sameDrive) parts.push(`目标剩余 ${formatBytes(volume.free)}`);
  let text = parts.join(' · ');
  if (sameDrive) text += '。在同一个存储里移动，不用复制，几乎立刻完成。';
  else if (volume && Number.isFinite(volume.free) && total + (1 << 30) > volume.free) text += '。空间可能不够，每个游戏开始前都会再检查一次（另留 1 GB）。';
  if (!$('migrate-delete').checked && !sameDrive) text += '。不删除原来的那份时两边各有一份，ShadowMount+ 可能同时识别到两个。';
  $('migrate-summary').textContent = text;
  buttonBusy('migrate-start', libraryMigrate.busy, chosen.length > 1 ? `迁移 ${chosen.length} 个游戏` : '开始迁移');
  if (!libraryMigrate.busy) $('migrate-start').disabled = !chosen.length || !target;
}

async function startMigrate(event) {
  event.preventDefault();
  if (libraryMigrate.busy) return;
  const chosen = migrateChosen();
  if (!chosen.length) { showFieldMessage('migrate-error', '请至少选择一个游戏。'); return; }
  const payload = {
    paths: chosen.map((game) => game.path), target: $('migrate-target').value,
    keep_source: !$('migrate-delete').checked, register: $('migrate-register').checked,
    host: library.host || undefined
  };
  libraryMigrate.busy = true;
  buttonBusy('migrate-start', true, '提交中…');
  try {
    const result = await api('/api/library/migrate', payload);
    $('migrate-dialog').close();
    startedJobs(result.jobs, `已加入传输任务：迁移 ${chosen.length} 个游戏，按顺序执行。`);
  } catch (error) {
    if (error.status === 401) { $('migrate-dialog').close(); showLogin(error.message); return; }
    showFieldMessage('migrate-error', error.message);
  } finally {
    libraryMigrate.busy = false;
    updateMigrateSummary();
  }
}

/* ── 卸载 ─────────────────────────────────────────────────────────── */

const libraryUninstall = { busy: false, chosen: new Set() };

function uninstallCandidates() {
  return [...library.games, ...libraryPackages()];
}

async function openUninstallDialog(keys = []) {
  if (!authenticated) return;
  if (!library.loaded) await loadLibrary();
  if (!library.loaded) { toast('游戏库读取失败，请先在游戏库页面重新扫描。', 'error'); return; }
  libraryUninstall.chosen = new Set(keys);
  $('uninstall-cheats').checked = true;
  $('uninstall-saves').checked = false;
  showFieldMessage('uninstall-error', '');
  renderUninstallDialog();
  $('uninstall-dialog').showModal();
  $('uninstall-cancel').focus({ preventScroll: true });
  if (!library.apps.loaded) loadLibraryApps().then(() => { if ($('uninstall-dialog').open) renderUninstallDialog(); });
}

function renderUninstallDialog() {
  const games = uninstallCandidates();
  $('uninstall-games').replaceChildren(...games.map((game) => {
    const registered = libraryRegistration(game);
    const where = game.pkg ? 'PKG 安装' : `${game.kind === 'image' ? '镜像 · ' : ''}${volumeKind(game.volume, game.volumeLabel)} · ${game.path}`;
    const extra = [game.title, registered ? '已注册图标' : '', game.size !== null ? formatBytes(game.size) : ''].filter(Boolean).join(' · ');
    return gamePick(libraryKey(game), game.name, `${where}${extra ? ` · ${extra}` : ''}`, libraryUninstall.chosen.has(libraryKey(game)));
  }));
  if (!games.length) $('uninstall-games').append(makeElement('p', 'empty-state', '没有找到游戏。'));
  const apps = library.apps;
  $('uninstall-engine').textContent = apps.loading || !apps.loaded ? '正在读取主机上已安装的游戏…'
    : apps.available ? '' : `ps5upload 引擎不可用（${apps.error || '未知原因'}）：只会删除游戏文件和金手指，主屏幕图标和存档需要引擎才能处理。`;
  $('uninstall-engine').hidden = !$('uninstall-engine').textContent;
  syncAllBox('uninstall-games', 'uninstall-all');
  updateUninstallSummary();
}

function uninstallChosen() {
  const keys = new Set($$('#uninstall-games input').filter((box) => box.checked).map((box) => box.value));
  return uninstallCandidates().filter((game) => keys.has(libraryKey(game)));
}

function updateUninstallSummary() {
  const chosen = uninstallChosen();
  libraryUninstall.chosen = new Set(chosen.map(libraryKey));
  const parts = [];
  const folders = chosen.filter((game) => !game.pkg).length;
  if (folders) parts.push(`${folders} 个游戏文件夹或镜像会被删除，无法恢复`);
  if (chosen.length - folders) parts.push(`${chosen.length - folders} 个 PKG 安装由主机卸载`);
  if ($('uninstall-saves').checked) parts.push('这些游戏在主机上所有用户的存档都会被删除');
  // The same title elsewhere shares its saves and cheats.
  const keys = new Set(chosen.map(libraryKey));
  const shared = chosen.filter((game) => game.title && uninstallCandidates().some((other) => other.title === game.title && !keys.has(libraryKey(other))));
  if (shared.length && ($('uninstall-saves').checked || $('uninstall-cheats').checked)) {
    parts.push(`${shared.map((game) => game.title).join('、')} 还有另一份没选，它们共用存档和金手指`);
  }
  $('uninstall-summary').textContent = parts.length ? `${parts.join('；')}。` : '请选择要卸载的游戏。';
  buttonBusy('uninstall-start', libraryUninstall.busy, chosen.length > 1 ? `卸载 ${chosen.length} 个游戏` : '卸载');
  if (!libraryUninstall.busy) $('uninstall-start').disabled = !chosen.length;
}

async function startUninstall(event) {
  event.preventDefault();
  if (libraryUninstall.busy) return;
  const chosen = uninstallChosen();
  if (!chosen.length) { showFieldMessage('uninstall-error', '请至少选择一个游戏。'); return; }
  libraryUninstall.busy = true;
  buttonBusy('uninstall-start', true, '提交中…');
  try {
    const result = await api('/api/library/uninstall', {
      items: chosen.map((game) => (game.pkg ? { origin: 'pkg', title: game.title, name: game.name } : { path: game.path })),
      delete_cheats: $('uninstall-cheats').checked, delete_saves: $('uninstall-saves').checked,
      host: library.host || undefined
    });
    $('uninstall-dialog').close();
    startedJobs(result.jobs, `已加入传输任务：卸载 ${chosen.length} 个游戏。`);
  } catch (error) {
    if (error.status === 401) { $('uninstall-dialog').close(); showLogin(error.message); return; }
    showFieldMessage('uninstall-error', error.message);
  } finally {
    libraryUninstall.busy = false;
    updateUninstallSummary();
  }
}

function bindLibraryDialogs() {
  $('library-migrate').addEventListener('click', () => openMigrateDialog());
  $('files-migrate').addEventListener('click', () => openMigrateDialog());
  $('library-uninstall').addEventListener('click', () => openUninstallDialog());
  $('migrate-source').addEventListener('change', () => { libraryMigrate.chosen.clear(); renderMigrateDialog(true); });
  for (const id of ['migrate-target', 'migrate-delete', 'migrate-register']) $(id).addEventListener('change', updateMigrateSummary);
  $('migrate-games').addEventListener('change', () => { syncAllBox('migrate-games', 'migrate-all'); updateMigrateSummary(); });
  $('migrate-all').addEventListener('change', () => {
    for (const box of $$('#migrate-games input')) box.checked = $('migrate-all').checked;
    updateMigrateSummary();
  });
  $('migrate-form').addEventListener('submit', startMigrate);
  $('migrate-cancel').addEventListener('click', () => $('migrate-dialog').close());
  for (const id of ['uninstall-cheats', 'uninstall-saves']) $(id).addEventListener('change', updateUninstallSummary);
  $('uninstall-games').addEventListener('change', () => { syncAllBox('uninstall-games', 'uninstall-all'); updateUninstallSummary(); });
  $('uninstall-all').addEventListener('change', () => {
    for (const box of $$('#uninstall-games input')) box.checked = $('uninstall-all').checked;
    updateUninstallSummary();
  });
  $('uninstall-form').addEventListener('submit', startUninstall);
  $('uninstall-cancel').addEventListener('click', () => $('uninstall-dialog').close());
}
