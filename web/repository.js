'use strict';

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
