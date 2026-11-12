'use strict';

/* ── 游戏库 ─────────────────────────────────────────────────────────── */

const library = { loaded: false, loading: false, games: [], volumes: [], errors: [], sizing: '', host: '' };

function normalizeLibrary(data) {
  const games = Array.isArray(data && data.games) ? data.games.filter((game) => game && typeof game.path === 'string') : [];
  return {
    games: games.map((game) => ({
      title: str(game.title), name: str(game.name) || str(game.folder) || game.path.split('/').pop(),
      folder: str(game.folder), path: game.path, volume: str(game.volume), volumeLabel: str(game.volume_label) || str(game.volume),
      platform: str(game.platform), mtime: Number(game.mtime) || 0,
      size: Number.isFinite(game.size) ? game.size : null
    })),
    volumes: Array.isArray(data && data.volumes) ? data.volumes.filter((v) => v && typeof v.path === 'string') : [],
    errors: Array.isArray(data && data.errors) ? data.errors.map(str) : [],
    host: str(data && data.host)
  };
}

function libraryVisible() {
  const text = $('library-search').value.trim().toLowerCase();
  const volume = $('library-volume').value;
  const list = library.games.filter((game) => (!volume || game.volume === volume)
    && (!text || game.name.toLowerCase().includes(text) || game.title.toLowerCase().includes(text) || game.path.toLowerCase().includes(text)));
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
  node.dataset.path = game.path;
  node.append(libraryCover(game), makeElement('span', 'library-name', game.name));
  const ids = [game.title, game.platform].filter(Boolean).join(' · ');
  node.append(makeElement('span', 'library-meta', ids || game.folder));
  node.append(makeElement('span', 'library-meta', `${game.volumeLabel} · ${game.path}`));
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
  actions.append(button('移动', '移动到其它存储', () => moveLibraryGame(game)));
  if (game.title && game.folder === game.title) {
    actions.append(button('注册图标', '重新注册主屏幕图标', () => registerLibraryGame(game)));
  }
  actions.append(button('删除', '删除游戏文件夹', () => deleteLibraryGame(game)));
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
  select.replaceChildren(option('全部存储', ''), ...library.volumes.map((volume) => option(str(volume.label) || volume.path, volume.path)));
  select.value = library.volumes.some((volume) => volume.path === chosen) ? chosen : '';
  const visible = libraryVisible();
  $('library-grid').replaceChildren(...visible.map(libraryCard));
  $('library-grid').hidden = !visible.length;
  $('library-empty').hidden = visible.length > 0 || !library.loaded;
  $('library-empty').textContent = library.games.length ? '没有符合条件的游戏。' : '没有找到游戏文件夹。';
  const known = library.games.filter((game) => game.size !== null);
  const total = known.reduce((sum, game) => sum + game.size, 0);
  $('library-summary').textContent = library.loaded
    ? `${library.host ? `PS5 ${library.host} · ` : ''}${library.games.length} 个游戏${known.length ? ` · 已计算 ${known.length} 个，共 ${formatBytes(total)}` : ''}`
    : 'PS5 存储上的游戏文件夹。';
  $('library-warnings').textContent = library.errors.length ? `部分存储读取失败：${library.errors.join('；')}` : '';
  $('library-warnings').hidden = !library.errors.length;
}

async function loadLibrary(refresh = false) {
  if (!authenticated || library.loading) return;
  library.loading = true;
  buttonBusy('library-refresh', true, '扫描中…');
  $('library-error').hidden = true;
  try {
    const data = await api(`/api/library${refresh ? '?refresh=1' : ''}`, undefined, 60000);
    Object.assign(library, normalizeLibrary(data), { loaded: true });
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

function moveLibraryGame(game) {
  openFilesDest('move', { name: game.folder || game.path.split('/').pop(), path: game.path, dir: true });
  whenClosed('files-dest-dialog', () => setTimeout(() => loadLibrary(), 1500));
}

function deleteLibraryGame(game) {
  openFilesDelete({ name: game.folder || game.path.split('/').pop(), path: game.path, dir: true });
  $('files-delete-extra').textContent = `游戏「${game.name}」的整个文件夹会被删除${game.size !== null ? `（${formatBytes(game.size)}）` : '（大小未计算）'}，无法恢复。`;
  whenClosed('files-delete-dialog', () => loadLibrary());
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
