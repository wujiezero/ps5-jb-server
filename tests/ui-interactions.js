'use strict';

// Run the existing browser module in a small DOM fixture. Network startup is
// omitted so these tests exercise filtering and rendering without a service.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.style = {};
    this.value = '';
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
  }
  append(...nodes) { this.children.push(...nodes.flatMap((node) => node.tagName === 'fragment' ? node.children : [node])); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, listener) {
    if (!this.listeners) this.listeners = new Map();
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  dispatch(type) { for (const listener of this.listeners?.get(type) || []) listener({ target: this }); }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatch('close'); }
  reset() {}
  get lastChild() { return this.children[this.children.length - 1] || null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  get classList() {
    const owner = this;
    return { add(name) { owner.className = `${owner.className || ''} ${name}`.trim(); }, remove() {}, toggle() {}, contains: () => false };
  }
}

function fixture(extra = {}) {
  const nodes = new Map();
  const labels = [];
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Element());
      return nodes.get(id);
    },
    // Only the title-name labels are looked up by selector in these tests.
    querySelectorAll(selector) { return selector.startsWith('.title-name') ? labels : []; },
    createElement: (tag) => new Element(tag),
    createElementNS: (_, tag) => new Element(tag),
    createDocumentFragment: () => new Element('fragment'),
    body: new Element('body')
  };
  // index.html loads classic scripts in order; joined they share one scope, as in the browser.
  // UI_WEB_DIR runs the same tests against a release package's bundled web/ (see release.yml).
  const webDir = process.env.UI_WEB_DIR || path.join(__dirname, '../web');
  const html = fs.readFileSync(path.join(webDir, 'index.html'), 'utf8');
  const scripts = [...html.matchAll(/<script src="\/([a-z0-9._-]+\.js)" defer><\/script>/g)].map((match) => match[1]);
  assert.equal(scripts[scripts.length - 1], 'app.js', 'app.js (event binding and startup) must load last');
  const source = '(() => {\n' + scripts.map((name) => fs.readFileSync(path.join(webDir, name), 'utf8')).join('\n');
  const startup = source.lastIndexOf('\nbindEvents();');
  assert.ok(startup > 0, 'browser module startup must be identifiable');
  const exposed = `${source.slice(0, startup)}
    globalThis.subject = { activity, repo, activitySelected, activityVisibleEvents,
      normalizeActivity, activityRow, renderBuiltinDomains,
      setActivityFilter, renderRepo, renderPs5Catalog,
      files, filesRow, nasRow, filesVisibleEntries, titleIdOf, titleNames, flushTitleNames, normalizeFilesEntries,
      nas, nasArchiveSelection, extractTask, packageTask, filesTransferTasks, filesTransferCard, normalizePs5,
      setNasJobs(value) { nasJobs = value; },
      renderRestoreResult, backupFilename, renderSelfcheck, renderNotify, notifyState, normalizeNotify,
      renderQueryLogs, renderLogs, logs,
      library, normalizeLibrary, libraryCard, libraryVisible, renderLibrary, backupSources, backupTask,
      migrateTask, uninstallTask, libraryPackages, volumeKind, libraryScanNote, libraryCounts,
      actHistory, renderStats, renderHistory, setActivityMode,
      updates, renderUpdates, loadVersion, refreshUpdates, checkUpdates, applyUpdate,
      scheduleUpdatePoll, stopUpdatePolling,
      normalizeRepo, renderUpstream, categoryChoices,
      inferUploadMetadata, autofillUploadMetadata, markUploadFieldEdited, bindUploadAutofill,
      resetUploadForm, openUploadDialog, closeUploadDialog,
      advanceEpoch() { epoch += 1; },
      setAuthenticated(value) { authenticated = value; csrf = value ? 'test-csrf' : ''; } };
  })();`;
  const context = { document, console, location: { hostname: '192.168.1.2' }, setTimeout, clearTimeout, clearInterval,
    AbortController, ...extra };
  vm.runInNewContext(exposed, context);
  return { ...context.subject, labels, node: (id) => document.getElementById(id) };
}

const event = (target, extra = {}) => ({ target, selectable: true, domain: '', dst: '', client: '', port: null, name: '', category: '', kind: 'flow', verdict: 'blocked', ...extra });
const textOf = (node) => [node.textContent, ...node.children.map(textOf)].join(' ');

test('activity search combines status with trimmed, case-insensitive literal fields', () => {
  const ui = fixture();
  ui.activity.events = [
    event('domain:playstation.net', { domain: 'PlayStation.NET', client: '192.168.1.50', port: 443, name: 'Console', category: 'Gaming' }),
    event('domain:other.net', { domain: 'other.net', verdict: 'allowed', kind: 'dns' }),
    event('', { selectable: false, kind: 'service', name: '管理服务' })
  ];
  for (const query of [' playSTATION ', '192.168.1.50', '443', 'console', 'gaming', '网关']) {
    ui.node('activity-search').value = query;
    assert.equal(ui.activityVisibleEvents()[0].target, 'domain:playstation.net', query);
    assert.equal(ui.activityVisibleEvents().length, 1, query);
  }
  ui.node('activity-search').value = 'DNS';
  assert.equal(ui.activityVisibleEvents()[0].target, 'domain:other.net');
  ui.node('activity-search').value = '本机服务';
  assert.equal(ui.activityVisibleEvents()[0].kind, 'service');
  ui.node('activity-search').value = '.*';
  assert.equal(ui.activityVisibleEvents().length, 0, 'query must not be interpreted as regex');
  ui.node('activity-search').value = 'other';
  ui.setActivityFilter('blocked');
  assert.equal(ui.activityVisibleEvents().length, 0, 'status and query both apply');
});

test('filter changes remove hidden activity targets before batch actions', () => {
  const ui = fixture();
  ui.activity.loaded = true;
  ui.activity.events = [event('ip:1', { dst: '1' }), event('ip:2', { dst: '2', verdict: 'allowed' })];
  ui.activitySelected.add('ip:1');
  ui.activitySelected.add('ip:2');
  ui.setActivityFilter('blocked');
  assert.deepEqual([...ui.activitySelected], ['ip:1']);
  ui.node('activity-search').value = 'no match';
  ui.setActivityFilter('all');
  assert.equal(ui.activitySelected.size, 0);
});

test('repository search keeps raw totals and rerenders when only query changes', () => {
  const ui = fixture();
  Object.assign(ui.repo, { loaded: true, host: '192.168.1.2', items: [
    { name: 'Alpha', filename: 'alpha.elf', version: '1.0', category: 'Tools', author: 'Jane', description: 'Storage helper', size: 10 },
    { name: 'Beta', filename: 'beta.bin', version: '2.0', category: 'Games', author: 'Sam', description: 'Game helper', size: 20 }
  ], totalBytes: 30 });
  ui.node('repo-search').value = ' JANE ';
  ui.renderRepo();
  assert.equal(ui.node('repo-body').children.length, 1);
  assert.match(textOf(ui.node('repo-body')), /Alpha/);
  assert.match(ui.node('repo-summary').textContent, /共 2 个插件/);
  assert.match(ui.node('repo-search-summary').textContent, /1.*2/);
  ui.node('repo-search').value = 'missing';
  ui.renderRepo();
  assert.match(textOf(ui.node('repo-body')), /没有匹配/);
  ui.node('repo-search').value = '';
  ui.renderRepo();
  assert.equal(ui.node('repo-body').children.length, 2);
});

test('PS5 catalog searches description and distinguishes no match from empty', () => {
  const ui = fixture();
  ui.repo.loaded = true;
  ui.repo.items = [{ name: 'Alpha', filename: 'alpha.elf', version: '1.0', category: 'Tools', description: 'Storage helper', size: 10 }];
  const host = { ip: '192.168.1.50', installed: [], running: [], sourceAttached: true };
  ui.node('ps5-catalog-search').value = ' STORAGE ';
  ui.renderPs5Catalog(host);
  assert.match(textOf(ui.node('ps5-catalog-body')), /Alpha/);
  ui.node('ps5-catalog-search').value = 'missing';
  ui.renderPs5Catalog(host);
  assert.match(textOf(ui.node('ps5-catalog-body')), /没有匹配/);
  ui.repo.items = [];
  ui.renderPs5Catalog(host);
  assert.match(textOf(ui.node('ps5-catalog-body')), /插件目录为空/);
});

const findAll = (node, predicate) => [...(predicate(node) ? [node] : []), ...node.children.flatMap((child) => findAll(child, predicate))];

test('game title IDs in file and NAS names get a name label filled from one batch request', async () => {
  const requests = [];
  const fetch = async (url) => {
    requests.push(url);
    return {
      ok: true, status: 200,
      json: async () => ({
        titles: {
          PPSA21567: { name: '宇宙机器人', en: 'ASTRO BOT', zh: '宇宙机器人', source: 'builtin' },
          PPSA99000: { name: 'ProsperoStore', en: 'ProsperoStore', zh: '', source: 'console' }
        },
        pending: []
      })
    };
  };
  const ui = fixture({ fetch });
  ui.setAuthenticated(true);
  const folder = ui.filesRow({ name: 'PPSA99000', path: '/data/homebrew/PPSA99000', dir: true, size: 0, mtime: 0 });
  const image = ui.filesRow({ name: 'PPSA21567.ffpfsc', path: '/mnt/ext0/games/PPSA21567.ffpfsc', dir: false, size: 1, mtime: 0 });
  const plain = ui.filesRow({ name: 'README.md', path: '/data/homebrew/README.md', dir: false, size: 1, mtime: 0 });
  const unknown = ui.nasRow({ name: 'CUSA00000-app', path: '/games/CUSA00000-app', dir: true, size: 0, mtime: 0 });
  const nas = ui.nasRow({ name: 'ppsa21567.part01.rar', path: '/games/ppsa21567.part01.rar', dir: false, size: 1, mtime: 0 });
  const isLabel = (node) => node.className === 'title-name';
  const labels = [folder, image, plain, unknown, nas].map((row) => findAll(row, isLabel));
  assert.deepEqual(labels.map((found) => found.length), [1, 1, 0, 1, 1], 'only title-ID names get a label');
  ui.labels.push(...labels.flat());
  // The original file name is untouched.
  assert.match(textOf(image), /PPSA21567\.ffpfsc/);
  await ui.flushTitleNames();
  assert.equal(requests.length, 1, 'all rows share one request');
  const url = new URL(requests[0], 'https://x');
  assert.deepEqual(url.searchParams.get('ids').split(',').sort(), ['CUSA00000', 'PPSA21567', 'PPSA99000']);
  assert.deepEqual(url.searchParams.getAll('folder'), ['/data/homebrew/PPSA99000'], 'only PS5 folders ask for param.json');
  const [folderLabel, imageLabel, , unknownLabel, nasLabel] = labels.map((found) => found[0]);
  assert.equal(folderLabel.textContent, 'ProsperoStore');
  assert.equal(imageLabel.textContent, '宇宙机器人');
  assert.equal(imageLabel.attributes.title, '宇宙机器人\nASTRO BOT');
  assert.equal(nasLabel.textContent, '宇宙机器人');
  assert.equal(unknownLabel.hidden, true, 'unknown IDs keep only the file name');
  assert.equal(unknownLabel.textContent, '');
  // Cached afterwards (including misses): re-rendering issues no new request.
  ui.filesRow({ name: 'PPSA21567.ffpfsc', path: '/x/PPSA21567.ffpfsc', dir: false, size: 1, mtime: 0 });
  ui.nasRow({ name: 'CUSA00000-app', path: '/games/CUSA00000-app', dir: true, size: 0, mtime: 0 });
  assert.equal(ui.titleNames.queued.size, 0);
  // The file filter also matches the game name.
  ui.files.entries = [{ name: 'PPSA21567.ffpfsc', dir: false }, { name: 'README.md', dir: false }];
  ui.files.filter = '宇宙';
  assert.deepEqual(ui.filesVisibleEntries().map((entry) => entry.name), ['PPSA21567.ffpfsc']);
});

test('failed title lookups leave file names alone and are retried later', async () => {
  const fetch = async () => ({ ok: false, status: 500, json: async () => ({ error: 'boom' }) });
  const ui = fixture({ fetch });
  ui.setAuthenticated(true);
  const row = ui.filesRow({ name: 'PPSA03671', path: '/mnt/ext0/games/PPSA03671', dir: true, size: 0, mtime: 0 });
  await ui.flushTitleNames();
  assert.equal(ui.titleNames.cache.has('PPSA03671'), false);
  assert.equal(ui.titleNames.sentFolders.has('/mnt/ext0/games/PPSA03671'), false);
  assert.match(textOf(row), /PPSA03671/);
  assert.equal(ui.titleIdOf('UP9000-PPSA03671_00-MARVELSWOLVERINE'), 'PPSA03671');
  assert.equal(ui.titleIdOf('XPPSA03671'), null);
});

test('extract install is offered for one RAR volume and its job reads as an install', () => {
  const ui = fixture();
  ui.nas.entries = [
    { name: 'PPSA03671-app.part02.rar', path: '/dl/PPSA03671-app.part02.rar', dir: false },
    { name: 'notes.txt', path: '/dl/notes.txt', dir: false },
    { name: 'PPSA03671-app', path: '/dl/PPSA03671-app', dir: true }
  ];
  ui.nas.selected.add('/dl/PPSA03671-app.part02.rar');
  assert.equal(ui.nasArchiveSelection().name, 'PPSA03671-app.part02.rar');
  ui.nas.selected.add('/dl/notes.txt');
  assert.equal(ui.nasArchiveSelection(), null, 'only a single archive selection');
  ui.nas.selected.clear();
  ui.nas.selected.add('/dl/PPSA03671-app');
  assert.equal(ui.nasArchiveSelection(), null, 'folders are not archives');
  ui.nas.entries.push({ name: '._PPSA03671-app.part02.rar', path: '/dl/._PPSA03671-app.part02.rar', dir: false });
  ui.nas.selected.clear();
  ui.nas.selected.add('/dl/._PPSA03671-app.part02.rar');
  assert.equal(ui.nasArchiveSelection(), null, 'macOS ._ metadata files are not archives');

  const base = { id: 'j1', kind: 'extract', name: 'PPSA03671-app（7 个分卷）', source: 'NAS', dest: '/mnt/ext0/games',
    mode: 'install', bytes_done: 5, bytes_total: 10, files_done: 3, current: 'PPSA03671-app/eboot.bin', speed: 1, elapsed: 2, eta: 5 };
  const running = ui.extractTask({ ...base, state: 'running' });
  assert.equal(running.kind, 'extract');
  assert.equal(running.sub, 'NAS → /mnt/ext0/games');
  assert.match(running.line, /已解出 3 个文件 · 当前 PPSA03671-app\/eboot.bin/);
  assert.equal(typeof running.cancel, 'function');
  const done = ui.extractTask({ ...base, state: 'done', result_path: '/mnt/ext0/games/PPSA03671', note: '目标已存在同名文件夹' });
  assert.equal(done.sub, '已安装到 /mnt/ext0/games/PPSA03671');
  assert.match(done.line, /目标已存在同名文件夹/);
  assert.equal(done.cancel, null);
  assert.match(ui.extractTask({ ...base, state: 'queued', mode: 'merge' }).sub, /（合并）/);
  assert.match(ui.extractTask({ ...base, state: 'running', files_total: 308 }).line, /已解出 3 \/ 308 个文件/);
});

function uploadFixture() {
  const ui = fixture();
  ui.setAuthenticated(true);
  ui.node('repo-upload-form').reset = () => {
    for (const field of ['name', 'version', 'category', 'author', 'description']) ui.node(`repo-upload-${field}`).value = '';
    ui.node('repo-upload-file').files = [];
  };
  ui.bindUploadAutofill();
  return ui;
}
function chooseUpload(ui, filename) {
  ui.node('repo-upload-file').files = filename ? [{ name: filename }] : [];
  ui.node('repo-upload-file').dispatch('change');
}
function editUpload(ui, field, value) {
  ui.node(`repo-upload-${field}`).value = value;
  ui.node(`repo-upload-${field}`).dispatch('input');
}

test('upload filenames infer names and real versions without removing software prefixes', () => {
  const ui = uploadFixture();
  for (const [filename, name, version] of [
    ['zftpd-ps5-v1.7.0.elf', 'zftpd', 'v1.7.0'],
    ['etaHEN_2.5B.bin', 'etaHEN', '2.5B'],
    ['ps5upload-12.4.2.elf', 'ps5upload', '12.4.2'],
    ['zftpd-v1.7.0-ps5.ELF', 'zftpd', 'v1.7.0'],
    ['zftpd_ps5_v1.7.0.bin', 'zftpd', 'v1.7.0'],
    ['payload-v2.1.0-rc.1.elf', 'payload', 'v2.1.0-rc.1'],
    ['ps5upload.elf', 'ps5upload', ''],
    ['custom-tool-ps5.bin', 'custom-tool-ps5', ''],
    ['payload-2024.elf', 'payload-2024', '']
  ]) {
    const metadata = ui.inferUploadMetadata(filename);
    assert.equal(metadata.name, name, filename);
    assert.equal(metadata.version, version, filename);
    for (const field of ['author', 'category', 'description']) assert.equal(metadata[field], '', 'unknown metadata stays empty');
  }
});

test('selecting another upload replaces only automatically filled values and clears stale version', () => {
  const ui = uploadFixture();
  chooseUpload(ui, 'zftpd-ps5-v1.7.0.elf');
  assert.equal(ui.node('repo-upload-name').value, 'zftpd');
  assert.equal(ui.node('repo-upload-version').value, 'v1.7.0');
  chooseUpload(ui, 'etaHEN_2.5B.bin');
  assert.equal(ui.node('repo-upload-name').value, 'etaHEN');
  assert.equal(ui.node('repo-upload-version').value, '2.5B');
  chooseUpload(ui, 'ps5upload.elf');
  assert.equal(ui.node('repo-upload-name').value, 'ps5upload');
  assert.equal(ui.node('repo-upload-version').value, '');
});

test('upload filename inference preserves manual edits including intentionally emptied fields', () => {
  const ui = uploadFixture();
  chooseUpload(ui, 'zftpd-ps5-v1.7.0.elf');
  editUpload(ui, 'name', '我的文件服务');
  editUpload(ui, 'version', '');
  editUpload(ui, 'author', 'Maintainer');
  chooseUpload(ui, 'etaHEN_2.5B.bin');
  assert.equal(ui.node('repo-upload-name').value, '我的文件服务');
  assert.equal(ui.node('repo-upload-version').value, '');
  assert.equal(ui.node('repo-upload-author').value, 'Maintainer');
  const fresh = uploadFixture();
  fresh.node('repo-upload-name').value = '预先填写的名称';
  chooseUpload(fresh, 'ps5upload-12.4.2.elf');
  assert.equal(fresh.node('repo-upload-name').value, '预先填写的名称');
  assert.equal(fresh.node('repo-upload-version').value, '12.4.2');
});

test('upload metadata uses exact repository filename matches only and clears automatic rich metadata on switch', () => {
  const ui = uploadFixture();
  ui.repo.items = [{ filename: 'etaHEN_2.5B.bin', name: 'etaHEN 工具箱', version: '2.5 Beta',
    category: '系统工具', author: 'Official author', description: '已有仓库描述' }];
  chooseUpload(ui, 'etaHEN_2.5B.bin');
  for (const [field, expected] of Object.entries({ name: 'etaHEN 工具箱', version: '2.5 Beta',
    category: '系统工具', author: 'Official author', description: '已有仓库描述' })) {
    assert.equal(ui.node(`repo-upload-${field}`).value, expected);
  }
  editUpload(ui, 'author', '我的作者信息');
  chooseUpload(ui, 'etaHEN_2.5C.bin');
  assert.equal(ui.node('repo-upload-name').value, 'etaHEN');
  assert.equal(ui.node('repo-upload-version').value, '2.5C');
  assert.equal(ui.node('repo-upload-category').value, '');
  assert.equal(ui.node('repo-upload-description').value, '');
  assert.equal(ui.node('repo-upload-author').value, '我的作者信息');
  chooseUpload(ui, 'ETAHEN_2.5B.bin');
  assert.equal(ui.node('repo-upload-name').value, 'ETAHEN', 'metadata matching is case-sensitive and exact');
  assert.equal(ui.node('repo-upload-category').value, '');
});

test('manual rich metadata and values restored to the automatic text retain ownership', () => {
  const ui = uploadFixture();
  chooseUpload(ui, 'etaHEN_2.5B.bin');
  editUpload(ui, 'name', 'etaHEN');
  editUpload(ui, 'category', '我的分类');
  editUpload(ui, 'description', '我的描述');
  ui.repo.items = [{ filename: 'zftpd-ps5-v1.7.0.elf', name: 'zftpd', category: '网络', description: '仓库描述' }];
  chooseUpload(ui, 'zftpd-ps5-v1.7.0.elf');
  assert.equal(ui.node('repo-upload-name').value, 'etaHEN', 'input marks manual ownership even if text matches old automatic value');
  assert.equal(ui.node('repo-upload-category').value, '我的分类');
  assert.equal(ui.node('repo-upload-description').value, '我的描述');
  assert.equal(ui.node('repo-upload-version').value, 'v1.7.0', 'untouched fields still update');
});

test('cancelled file selection and unsupported extensions leave upload fields intact', () => {
  const ui = uploadFixture();
  chooseUpload(ui, 'ps5upload-12.4.2.elf');
  chooseUpload(ui, null);
  assert.equal(ui.node('repo-upload-name').value, 'ps5upload');
  assert.equal(ui.node('repo-upload-version').value, '12.4.2');
  chooseUpload(ui, 'notes.txt');
  assert.equal(ui.node('repo-upload-name').value, 'ps5upload');
});

test('upload reset and close/reopen clear automatic and manual ownership', () => {
  const ui = uploadFixture();
  chooseUpload(ui, 'etaHEN_2.5B.bin');
  editUpload(ui, 'name', 'Custom');
  ui.resetUploadForm();
  chooseUpload(ui, 'ps5upload-12.4.2.elf');
  assert.equal(ui.node('repo-upload-name').value, 'ps5upload');
  editUpload(ui, 'name', 'Custom again');
  ui.closeUploadDialog();
  ui.openUploadDialog();
  chooseUpload(ui, 'zftpd-ps5-v1.7.0.elf');
  assert.equal(ui.node('repo-upload-name').value, 'zftpd');
  assert.equal(ui.node('repo-upload-version').value, 'v1.7.0');
});

test('replacement uploads retain existing blank-means-keep metadata semantics', () => {
  const ui = uploadFixture();
  ui.openUploadDialog({ filename: 'zftpd.elf', name: '我的文件服务', version: '1.0', author: 'Existing author' });
  chooseUpload(ui, 'zftpd-ps5-v1.7.0.elf');
  assert.equal(ui.node('repo-upload-name').value, '');
  assert.equal(ui.node('repo-upload-version').value, '');
  assert.equal(ui.node('repo-upload-author').value, '');
  assert.equal(ui.node('repo-upload-name').placeholder, '我的文件服务');
  editUpload(ui, 'version', 'v1.7.0');
  chooseUpload(ui, 'other-v2.0.0.elf');
  assert.equal(ui.node('repo-upload-version').value, 'v1.7.0');
});

test('repository upstream status marks updatable plugins and keeps history for rollback', () => {
  const ui = fixture();
  ui.setAuthenticated(true);
  const sum = (c) => c.repeat(64);
  const data = ui.normalizeRepo({
    catalog: { items: [
      { filename: 'ftpsrv_v0.21.elf', name: 'FTP', version: 'v0.21', checksum: sum('a'),
        history: [{ filename: 'ftpsrv_v0.20.elf', version: 'v0.20', checksum: sum('b'), size: 10 }, { checksum: 'bad' }] },
      { filename: 'zftpd_v1.8.0.elf', name: 'zftpd', version: 'v1.8.0' },
      { filename: 'mine.elf', name: 'Mine' }
    ] },
    upstream: { checked_at: '2026-11-11T10:00:00', available: 1, items: {
      'ftpsrv_v0.21.elf': { state: 'available', version: 'v0.22', filename: 'ftpsrv_v0.22.elf' },
      'zftpd_v1.8.0.elf': { state: 'local_newer', version: 'v1.7.0' },
      'mine.elf': { state: 'duplicate', version: 'v0.22', filename: 'ftpsrv_v0.22.elf', owner: 'FTP', owner_filename: 'ftpsrv_v0.22.elf' } } }
  });
  assert.equal(data.upstream.items['mine.elf'].ownerFilename, 'ftpsrv_v0.22.elf', 'duplicate keeps the owning entry');
  assert.equal(data.items[0].history.map((record) => record.version).join(), 'v0.20', 'malformed records dropped');
  assert.equal(data.items[2].history.length, 0);
  Object.assign(ui.repo, data);
  ui.renderRepo();
  assert.match(ui.node('repo-upstream-line').textContent, /1 个插件有新版本/);
  assert.match(textOf(ui.node('repo-body')), /重复 · 最新版在「FTP」/);
  assert.equal(ui.node('repo-upstream-all').hidden, true, 'batch button only for two or more');
  ui.repo.upstream = null;
  ui.renderUpstream();
  assert.match(ui.node('repo-upstream-line').textContent, /尚未检查/);
});

test('upload category offers used categories first, defaults after, and creating a new one', () => {
  const ui = fixture();
  ui.repo.items = [
    { filename: 'a.elf', category: 'Files' }, { filename: 'b.elf', category: '工具' },
    { filename: 'c.elf', category: '工具' }, { filename: 'd.elf', category: 'Uncategorized' },
    { filename: 'ps5-payload-repository', category: '说明' }
  ];
  const all = ui.categoryChoices('');
  assert.deepEqual([...all.options], ['工具', 'Files', 'Loader', 'Kernel', 'System', 'Networking', 'Diagnostics']);
  assert.equal(all.create, '', 'nothing typed, nothing to create');
  assert.deepEqual([...ui.categoryChoices('net').options], ['Networking']);
  assert.equal(ui.categoryChoices('net').create, 'net', 'a partial match can still become a new category');
  assert.equal(ui.categoryChoices(' files ').create, '', 'an existing category in another case is not created twice');
  assert.equal(ui.categoryChoices('uncategorized').create, '', 'the server default is not offered as a category');
  assert.equal(ui.categoryChoices('模拟器').create, '模拟器');
});

const updateStatus = (overrides = {}) => ({
  current: '1.4.0', latest: null, available: false, checked_at: null,
  release_url: 'https://github.com/wujiezero/ps5-jb-server/releases', notes: '',
  check_error: '', supported: true, unsupported_reason: '', job: null, ...overrides
});
const jsonResponse = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
function updateFixture(fetch) {
  const timers = new Map();
  let sequence = 0;
  const ui = fixture({ fetch, setTimeout(callback, delay) { const id = ++sequence; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); } });
  ui.setAuthenticated(true);
  return { ...ui, timers };
}

test('public version appears in settings and sidebar before release checks', async () => {
  const requests = [];
  const ui = updateFixture(async (url) => { requests.push(url); return jsonResponse({ version: '1.4.0', repository: 'wujiezero/ps5-jb-server' }); });
  await ui.loadVersion();
  assert.equal(ui.node('update-current').textContent, 'v1.4.0');
  assert.equal(ui.node('sidebar-version').textContent, 'v1.4.0');
  assert.equal(ui.node('update-latest').textContent, '未检查');
  assert.deepEqual(requests, ['/api/version']);
  assert.equal(ui.timers.size, 0, 'version lookup never starts update checks or polling');
});

test('update latest status distinguishes unchecked, no release, latest and available', () => {
  const ui = updateFixture();
  for (const [data, expected] of [
    [updateStatus(), '未检查'],
    [updateStatus({ checked_at: '2026-11-10T12:00:00Z' }), '暂无发布'],
    [updateStatus({ latest: '1.4.0', checked_at: '2026-11-10T12:00:00Z' }), 'v1.4.0 · 已是最新'],
    [updateStatus({ latest: '1.5.0', available: true }), '有更新 · v1.5.0']
  ]) {
    ui.updates.data = data;
    ui.renderUpdates();
    assert.equal(ui.node('update-latest').textContent, expected);
    assert.equal(ui.node('update-current').textContent, 'v1.4.0');
  }
});

test('settings shows whether internet fetches use a proxy', () => {
  const ui = updateFixture();
  for (const [proxy, expected] of [
    [undefined, '未知'],
    [{ configured: false, url: '', variable: '', error: '' }, '直连（未设置代理环境变量）'],
    [{ configured: true, url: 'http://192.0.2.50:6152', variable: 'https_proxy', error: '' },
      'http://192.0.2.50:6152 · 来自 https_proxy'],
    [{ configured: false, url: '', variable: 'https_proxy',
       error: 'https_proxy 不是可用的 HTTP 代理（只支持 http:// 或 https://，不支持 SOCKS）' }, /不支持 SOCKS/]
  ]) {
    ui.updates.data = updateStatus({ proxy });
    ui.renderUpdates();
    const cell = ui.node('update-proxy');
    if (expected instanceof RegExp) assert.match(cell.textContent, expected);
    else assert.equal(cell.textContent, expected);
  }
});

test('the last release check is a date, not 1970', () => {
  const ui = updateFixture();
  // The API sends Unix seconds; Date would read that number as milliseconds.
  ui.updates.data = updateStatus({ checked_at: 1794369653.6677148 });
  ui.renderUpdates();
  const shown = ui.node('update-checked').textContent;
  assert.doesNotMatch(shown, /^01\/22/, shown);
  assert.doesNotMatch(shown, /^1794/, shown);
  assert.match(shown, /^11\/1[01] /, shown);
});

test('update notes are text and unsupported installs still allow explicit checks', () => {
  const ui = updateFixture();
  ui.updates.data = updateStatus({ latest: '1.5.0', available: true, notes: '<img src=x onerror=alert(1)>',
    supported: false, unsupported_reason: '当前安装方式不支持在线更新', release_url: 'javascript:alert(1)' });
  ui.renderUpdates();
  assert.equal(ui.node('update-notes').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(ui.node('update-notes').children.length, 0);
  assert.equal(ui.node('update-check').disabled, false);
  assert.equal(ui.node('update-apply').disabled, true);
  assert.match(ui.node('update-unsupported').textContent, /不支持在线更新/);
  assert.equal(ui.node('update-release').hidden, true, 'unsafe release links are never shown');
});

test('every active update phase disables actions and terminal failures stop polling', () => {
  const ui = updateFixture();
  for (const state of ['queued', 'downloading', 'validating', 'backing_up', 'installing', 'restarting', 'verifying']) {
    ui.updates.data = updateStatus({ latest: '1.5.0', available: true, job: { id: 'j1', state, version: '1.5.0' } });
    ui.renderUpdates();
    assert.equal(ui.node('update-check').disabled, true, state);
    assert.equal(ui.node('update-apply').disabled, true, state);
    assert.equal(ui.node('update-stepper').hidden, false, state);
    assert.equal(ui.node('update-progress').hidden, true, `${state}: the stepper replaces the text banner`);
  }
  ui.updates.data.job = { id: 'j1', state: 'downloading', version: '1.5.0', error: '临时网络错误，正在重试' };
  ui.renderUpdates();
  assert.equal(ui.node('update-progress').hidden, false, 'errors still show the text banner');
  for (const [state, text] of [['failed', /更新失败/], ['rolled_back', /已回滚/], ['done', /更新完成/]]) {
    ui.updates.data.job = { id: 'j1', state, error: state === 'done' ? '' : '下载或验证失败' };
    ui.renderUpdates();
    ui.scheduleUpdatePoll();
    assert.match(ui.node('update-progress').textContent, text);
    if (state !== 'done') assert.match(ui.node('update-progress').textContent, /下载或验证失败/);
    assert.equal(ui.node('update-check').disabled, false);
    assert.equal(ui.timers.size, 0, state);
  }
});

test('explicit check reports download metadata errors without hiding current version', async () => {
  const requests = [];
  const ui = updateFixture(async (url, options) => {
    requests.push([url, options]);
    return jsonResponse(updateStatus({ check_error: '无法连接 GitHub', checked_at: '2026-11-10T12:00:00Z' }));
  });
  await ui.checkUpdates();
  assert.equal(requests[0][0], '/api/updates/check');
  assert.equal(requests[0][1].method, 'POST');
  assert.equal(requests[0][1].headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(requests[0][1].body, '{}');
  assert.match(ui.node('update-error').textContent, /无法连接 GitHub/);
  assert.match(ui.node('update-latest').textContent, /检查失败/);
  assert.doesNotMatch(ui.node('update-latest').textContent, /暂无发布/);
  assert.equal(ui.node('update-state').dataset.tone, 'error');
  assert.equal(ui.node('update-current').textContent, 'v1.4.0');
  assert.equal(ui.timers.size, 0);
});

test('one click applies latest version once and polls only while a job is active', async () => {
  const requests = [];
  const ui = updateFixture(async (url, options) => {
    requests.push([url, options]);
    return jsonResponse(updateStatus({ latest: '1.5.0', available: true,
      job: { id: 'j1', state: requests.length === 1 ? 'queued' : 'done', version: '1.5.0' } }));
  });
  ui.updates.data = updateStatus({ latest: '1.5.0', available: true });
  await ui.applyUpdate();
  assert.equal(requests[0][0], '/api/updates/apply');
  assert.equal(requests[0][1].body, '{"version":"1.5.0"}');
  await ui.applyUpdate();
  assert.equal(requests.length, 1, 'busy update cannot be submitted twice');
  assert.equal([...ui.timers.values()][0].delay, 2000);
  await ui.refreshUpdates();
  assert.equal(requests[1][0], '/api/updates');
  assert.equal(ui.timers.size, 0, 'finished jobs have no update polling');
});

test('restart network errors show reconnect and stop after five minutes with manual recovery', async () => {
  const ui = updateFixture(async () => { throw new TypeError('offline'); });
  ui.updates.data = updateStatus({ job: { id: 'j1', state: 'restarting', version: '1.5.0' } });
  await ui.refreshUpdates();
  assert.match(ui.node('update-progress').textContent, /重新连接/);
  assert.equal([...ui.timers.values()][0].delay, 2000);
  ui.updates.deadline = Date.now() - 1;
  ui.scheduleUpdatePoll();
  assert.equal(ui.timers.size, 0);
  assert.match(ui.node('update-progress').textContent, /5 分钟/);
  assert.equal(ui.node('update-retry').hidden, false);
  assert.equal(ui.node('update-reload').hidden, false);
  assert.equal(ui.node('update-apply').disabled, true, 'unknown result must not permit another install');
});

test('uncertain apply response reconnects without submitting another update', async () => {
  const requests = [];
  const ui = updateFixture(async (url) => { requests.push(url); throw new TypeError('offline'); });
  ui.updates.data = updateStatus({ latest: '1.5.0', available: true });
  await ui.applyUpdate();
  assert.match(ui.node('update-progress').textContent, /重新连接/);
  assert.equal(ui.node('update-apply').disabled, true);
  assert.equal([...ui.timers.values()][0].delay, 2000);
  await ui.applyUpdate();
  assert.deepEqual(requests, ['/api/updates/apply']);
});

test('an unrelated configuration change cannot strand an in-flight update status request', async () => {
  let respond;
  const ui = updateFixture(() => new Promise((resolve) => { respond = resolve; }));
  const pending = ui.refreshUpdates();
  ui.advanceEpoch();
  respond(jsonResponse(updateStatus({ current: '1.5.0' })));
  await pending;
  assert.equal(ui.updates.loading, false);
  assert.equal(ui.node('update-current').textContent, 'v1.5.0');
});

test('an explicit apply rejection shows server error without pretending to reconnect', async () => {
  const ui = updateFixture(async () => jsonResponse({ error: '更新包下载失败，请重试' }, 502));
  ui.updates.data = updateStatus({ latest: '1.5.0', available: true });
  await ui.applyUpdate();
  assert.match(ui.node('update-error').textContent, /更新包下载失败/);
  assert.equal(ui.node('update-progress').hidden, true);
  assert.equal(ui.node('update-apply').disabled, false);
  assert.equal(ui.timers.size, 0);
});

test('failed rollback reports both install and recovery errors', () => {
  const ui = updateFixture();
  ui.updates.data = updateStatus({ job: { id: 'j1', state: 'failed', version: '1.5.0',
    error: '服务验证失败', rollback_error: '无法恢复备份，请手动恢复' } });
  ui.renderUpdates();
  assert.match(ui.node('update-progress').textContent, /服务验证失败/);
  assert.match(ui.node('update-progress').textContent, /回滚失败：无法恢复备份，请手动恢复/);
  assert.doesNotMatch(ui.node('update-progress').textContent, /已回滚/);
});

test('plain status reads do not trigger release checks or idle polling', async () => {
  const requests = [];
  const ui = updateFixture(async (url) => { requests.push(url); return jsonResponse(updateStatus()); });
  await ui.refreshUpdates();
  assert.deepEqual(requests, ['/api/updates']);
  assert.equal(ui.timers.size, 0);
});

test('expired update session returns to login and stops polling', async () => {
  const ui = updateFixture(async () => jsonResponse({ error: '登录已失效' }, 401));
  ui.updates.data = updateStatus({ job: { id: 'j1', state: 'downloading' } });
  await ui.refreshUpdates();
  assert.equal(ui.node('app-shell').hidden, true);
  assert.equal(ui.node('login-view').hidden, false);
  assert.match(ui.node('login-message').textContent, /登录已失效/);
  assert.equal(ui.timers.size, 0);
});

test('macOS ._ metadata files never show in the file list', () => {
  const ui = fixture();
  const listed = ui.normalizeFilesEntries({ path: '/data', entries: [
    { name: '._PPSA03671', kind: 'file', size: 4096 }, { name: 'PPSA03671', dir: true }, { name: '.hidden', size: 1 }
  ] }, '/data');
  assert.equal(JSON.stringify(listed.entries.map((entry) => entry.name)), JSON.stringify(['PPSA03671', '.hidden']));
});

test('builtin DNS rows mark 本机劫持 after the domain, not in the verdict cell', () => {
  const ui = fixture();
  const normalized = ui.normalizeActivity({ events: [
    { kind: 'dns', domain: 'manuals.playstation.net', verdict: 'allowed', local: true, port: 53 },
    { kind: 'dns', domain: 'ps5.playstation.net', verdict: 'allowed', port: 53 }
  ] });
  assert.equal(normalized.events[0].local, true);
  assert.equal(normalized.events[1].local, false);
  const localRow = ui.activityRow(normalized.events[0]);
  const otherRow = ui.activityRow(normalized.events[1]);
  const domainCell = (row) => row.children[3];
  const verdictCell = (row) => row.children[6];
  assert.match(textOf(domainCell(localRow)), /manuals\.playstation\.net 本机劫持/);
  assert.doesNotMatch(textOf(verdictCell(localRow)), /本机劫持/);
  assert.match(textOf(verdictCell(localRow)), /允许/);
  assert.doesNotMatch(textOf(otherRow), /本机劫持/);
});

test('builtin domain block lists server domains and hides when absent', () => {
  const ui = fixture();
  ui.renderBuiltinDomains(['manuals.playstation.net', 'ena.net.playstation.net'], '192.0.2.10');
  assert.equal(ui.node('builtin-domains').hidden, false);
  assert.equal(ui.node('builtin-domain-list').children.length, 2);
  assert.equal(ui.node('builtin-host-ip').textContent, '192.0.2.10');
  ui.renderBuiltinDomains([], '192.0.2.10');
  assert.equal(ui.node('builtin-domains').hidden, true);
});

test('interrupted NAS transfers show 已中断 and a retry button; extraction only a cleanup hint', () => {
  const ui = fixture();
  const base = { source: 'NAS', dest: '/data', bytes_done: 10, bytes_total: 100, speed: 0, elapsed: 3, eta: null,
    files_done: 0, files_total: 2, files_skipped: 0, files_failed: 0, errors: [] };
  ui.setNasJobs([
    { ...base, id: 'a', kind: 'nas', name: 'movies', state: 'failed', error: '服务重启时中断', interrupted: true },
    { ...base, id: 'b', kind: 'extract', name: 'Game.rar', state: 'failed', error: '服务重启时中断', interrupted: true,
      staging: '/data/games/.extract-b' },
    { ...base, id: 'c', kind: 'nas', name: 'running', state: 'running' }
  ]);
  const [nasTask, extract, running] = ui.filesTransferTasks();
  assert.equal(typeof nasTask.retry, 'function');
  assert.equal(extract.retry, undefined);
  assert.equal(running.retry, null);
  assert.match(extract.line, /\.extract-b/);
  const card = ui.filesTransferCard(nasTask);
  assert.match(textOf(card), /已中断/);
  assert.match(textOf(card), /重试/);
  assert.doesNotMatch(textOf(ui.filesTransferCard(running)), /重试/);
});

test('automatic retries and resumes are counted on the NAS task card', () => {
  const ui = fixture();
  const base = { source: 'NAS', dest: '/data', bytes_done: 10, bytes_total: 100, speed: 0, elapsed: 3, eta: null,
    files_done: 1, files_total: 2, files_skipped: 0, files_failed: 0, errors: [], kind: 'nas', state: 'running' };
  ui.setNasJobs([{ ...base, id: 'a', name: 'movies', retries: 2, resumed: 1 }, { ...base, id: 'b', name: 'old' }]);
  const [retried, older] = ui.filesTransferTasks();
  assert.match(textOf(ui.filesTransferCard(retried)), /自动重试 2 次.*续传 1 次/);
  assert.doesNotMatch(textOf(ui.filesTransferCard(older)), /重试|续传/, 'older servers omit the fields');
});

test('PKG and cheat ZIP rows get their own action; package jobs show steps and a retry', () => {
  const ui = fixture();
  ui.setAuthenticated(true);
  ui.files.info = { reachable: true, host: '192.0.2.5' };
  const find = (node, test) => (test(node) ? node : node.children.map((child) => find(child, test)).find(Boolean));
  const action = (entry) => find(ui.nasRow({ dir: false, size: 1, mtime: 0, ...entry }),
    (node) => node.tagName === 'button' && /nas-row-action/.test(node.className || ''));
  assert.equal(action({ name: 'CUSA12345.pkg', path: '/g/CUSA12345.pkg' }).textContent, '安装 PKG');
  assert.equal(action({ name: 'CUSA12345 金手指.zip', path: '/g/a.zip' }).textContent, '导入金手指');
  assert.equal(action({ name: 'Trainer v2.ZIP', path: '/g/b.zip' }).textContent, '导入金手指');
  assert.equal(action({ name: 'saves.zip', path: '/g/c.zip' }), undefined);
  assert.equal(action({ name: '._CUSA12345.pkg', path: '/g/._x.pkg' }), undefined);

  const base = { id: 'p', kind: 'package', name: 'CUSA12345.pkg', source: 'NAS', host: '192.0.2.5',
    bytes_done: 10, bytes_total: 100, speed: 0, elapsed: 3, eta: null, errors: [] };
  const running = ui.packageTask({ ...base, state: 'running', phase: 'install' });
  assert.equal(running.kind, 'package');
  assert.match(running.line, /准备 → 传输 → 【安装】 → 确认/);
  assert.equal(running.retry, null);
  const busy = ui.packageTask({ ...base, state: 'failed', error_code: 'busy', error: 'x' });
  assert.equal(typeof busy.retry, 'function');
  assert.match(busy.line, /引擎正忙/);
  assert.match(ui.packageTask({ ...base, state: 'done_with_warnings', warning: '游戏可能无法启动' }).line, /可能无法启动/);
  ui.setNasJobs([{ ...base, state: 'queued', phase: 'prepare' }]);
  assert.match(textOf(ui.filesTransferCard(ui.filesTransferTasks()[0])), /CUSA12345\.pkg/);
});

test('extraction cards mention imported cheats and a failed cheat step', () => {
  const ui = fixture();
  const base = { id: 'e', kind: 'extract', name: 'Game.rar', source: 'NAS', dest: '/data', bytes_done: 1, bytes_total: 1,
    files_done: 1, errors: [], state: 'done' };
  assert.match(ui.extractTask({ ...base, cheat_result: { written: ['a', 'b'] } }).line, /已导入 2 个金手指文件.*重新加载/);
  assert.match(ui.extractTask({ ...base, state: 'done_with_warnings', cheat_error: '金手指导入失败：坏 ZIP' }).line, /坏 ZIP/);
});

test('game library cards: cover only for title IDs, register only for ID-named folders, size sorting', () => {
  const ui = fixture();
  ui.setAuthenticated(true);
  const data = ui.normalizeLibrary({ host: '192.0.2.5', games: [
    { title: 'CUSA12345', name: '某游戏', folder: 'CUSA12345', path: '/data/homebrew/CUSA12345', volume: '/data', volume_label: 'data', platform: 'PS4', size: null, kind: 'folder', scan_root: '/data/homebrew' },
    { title: 'PPSA01234', name: 'Big', folder: 'PPSA01234-app', path: '/mnt/ext0/games/PPSA01234-app', volume: '/mnt/ext0', platform: 'PS5', size: 5 << 30, scan_root: '/mnt/ext0/games' },
    { title: '', name: 'Homebrew<script>', folder: 'Homebrew', path: '/data/homebrew/Homebrew', volume: '/data', size: 1024, scan_root: '/data/homebrew' },
    { name: 'bad' }
  ], volumes: [{ path: '/data', label: 'data' }, { path: '/mnt/ext0', label: 'ext0' }],
  scan_paths: [{ path: '/data/homebrew', volume: '/data', exists: true, games: 2 }, { path: '/mnt/ext0/games', volume: '/mnt/ext0', exists: true, games: 1 }],
  scan_config: { source: 'config', file: '/data/shadowmount/config.ini', depth: 1, skipped: ['/mnt/smb/x'] } });
  assert.equal(data.games.length, 3);
  assert.equal(data.scanPaths.length, 2);
  const buttons = (card) => {
    const out = [];
    const walk = (node) => { if (node.tagName === 'button') out.push(node.textContent); node.children.forEach(walk); };
    walk(card);
    return out;
  };
  const [plain, big, homebrew] = data.games.map(ui.libraryCard);
  assert.deepEqual(buttons(plain), ['计算大小', '打开文件夹', '迁移', '注册图标', '卸载']);
  assert.ok(!buttons(big).includes('注册图标'), 'folder name differs from the title ID');
  assert.ok(buttons(big).includes('5.0 GB') || buttons(big).some((label) => /GB/.test(label)));
  assert.equal(homebrew.children[0].tagName, 'span', 'no cover request without a title ID');
  assert.equal(plain.children[0].tagName, 'img');
  assert.match(plain.children[0].src, /\/api\/library\/icon\?path=%2Fdata%2Fhomebrew%2FCUSA12345/);
  assert.match(textOf(homebrew), /Homebrew<script>/, 'names are text, never markup');
  Object.assign(ui.library, data, { loaded: true });
  ui.node('library-sort').value = 'size';
  assert.deepEqual(ui.libraryVisible().map((game) => game.name), ['Big', 'Homebrew<script>', '某游戏']);
  ui.node('library-sort').value = 'name';
  ui.node('library-search').value = 'cusa';
  assert.deepEqual(ui.libraryVisible().map((game) => game.title), ['CUSA12345']);
  ui.node('library-search').value = '';
  ui.node('library-volume').value = '/mnt/ext0/games';
  assert.deepEqual(ui.libraryVisible().map((game) => game.name), ['Big']);
  const note = ui.libraryScanNote();
  assert.match(note, /config\.ini/);
  assert.match(note, /\/data\/homebrew、\/mnt\/ext0\/games/);
  assert.match(note, /\/mnt\/smb\/x/);
  assert.equal(ui.libraryCounts(), '3 个游戏文件夹');
});

test('image games: listed with their format, no register button, registration from the ShadowMount+ mount', () => {
  const ui = fixture();
  ui.setAuthenticated(true);
  Object.assign(ui.library, ui.normalizeLibrary({ host: '192.0.2.5', games: [
    { title: 'PPSA01476', name: 'Rift Apart', folder: 'PPSA01476.ffpfsc', path: '/mnt/ext0/games/PPSA01476.ffpfsc', volume: '/mnt/ext0',
      platform: 'PS5', size: 37 << 30, kind: 'image', format: 'ffpfsc', scan_root: '/mnt/ext0/games' },
    { title: 'PPSA03671', name: 'Folder', folder: 'PPSA03671', path: '/mnt/ext0/games/PPSA03671', volume: '/mnt/ext0', kind: 'folder', scan_root: '/mnt/ext0/games' }
  ], volumes: [{ path: '/data' }, { path: '/mnt/ext0' }], scan_paths: [{ path: '/mnt/ext0/games', volume: '/mnt/ext0' }, { path: '/data/homebrew', volume: '/data' }] }), { loaded: true });
  ui.library.apps = { loaded: true, loading: false, available: true, error: '', titles: [
    { title: 'PPSA01476', name: 'Rift Apart', origin: 'registered', source: '/mnt/shadowmnt/PPSA01476_944c3c47', system: false, imageBacked: true }] };
  const image = ui.libraryCard(ui.library.games[0]);
  const labels = [];
  const walk = (node) => { if (node.tagName === 'button') labels.push(node.textContent); node.children.forEach(walk); };
  walk(image);
  assert.ok(!labels.includes('注册图标'), 'ShadowMount+ registers images itself');
  assert.ok(labels.includes('迁移'));
  assert.match(textOf(image), /镜像 FFPFSC/);
  assert.match(textOf(image), /已注册图标/);
  assert.doesNotMatch(textOf(ui.libraryCard(ui.library.games[1])), /已注册图标/, 'the folder copy is not the mounted image');
  assert.equal(ui.libraryCounts(), '1 个游戏文件夹、1 个镜像');
});

test('PKG installs join the library as uninstall-only cards; registered folders are marked', () => {
  const ui = fixture();
  ui.setAuthenticated(true);
  Object.assign(ui.library, ui.normalizeLibrary({ host: '192.0.2.5', games: [
    { title: 'PPSA01234', name: 'Folder', folder: 'PPSA01234', path: '/data/games/PPSA01234', volume: '/data', size: null }
  ], volumes: [{ path: '/data' }, { path: '/mnt/ext0' }] }), { loaded: true });
  ui.library.apps = { loaded: true, loading: false, available: true, error: '', titles: [
    { title: 'PPSA01234', name: 'Folder', origin: 'registered', source: '/data/games/PPSA01234', system: false },
    { title: 'PPSA05555', name: 'Pkg', origin: 'pkg', source: '', system: false },
    { title: 'NPXS40000', name: 'System', origin: 'system', source: '', system: true }] };
  assert.deepEqual(ui.libraryPackages().map((game) => game.title), ['PPSA05555']);
  ui.node('library-sort').value = 'name';
  ui.node('library-search').value = '';
  ui.node('library-volume').value = '';
  assert.deepEqual(ui.libraryVisible().map((game) => game.name), ['Folder', 'Pkg']);
  ui.node('library-volume').value = 'pkg';
  assert.deepEqual(ui.libraryVisible().map((game) => game.name), ['Pkg']);
  const buttons = (card) => {
    const out = [];
    const walk = (node) => { if (node.tagName === 'button') out.push(node.textContent); node.children.forEach(walk); };
    walk(card);
    return out;
  };
  assert.deepEqual(buttons(ui.libraryCard(ui.libraryPackages()[0])), ['卸载']);
  assert.match(textOf(ui.libraryCard(ui.library.games[0])), /已注册图标/);
  assert.equal(ui.volumeKind('/mnt/usb0', ''), 'USB 外接硬盘（/mnt/usb0）');
  assert.equal(ui.volumeKind('/data', '/data'), '内置存储（/data）');
});

test('migrate and uninstall task cards show their steps, results and retry', () => {
  const ui = fixture();
  const migrating = ui.migrateTask({ id: 'm1', kind: 'migrate', name: 'Demo', state: 'running', phase: 'verify', auto_register: true,
    paths: ['/data/games/X'], target: '/mnt/ext0/games/X', bytes_done: 5, bytes_total: 10 });
  assert.match(migrating.line, /复制 → 【校验】 → 放到位 → 注册图标/);
  assert.equal(migrating.sub, '/data/games/X → /mnt/ext0/games/X');
  const failed = ui.migrateTask({ id: 'm2', kind: 'migrate', name: 'Demo', state: 'failed', phase: 'copy', error: 'x', target: '/t' });
  assert.ok(failed.retry);
  assert.match(failed.line, /原文件夹没有改动/);
  const done = ui.migrateTask({ id: 'm3', kind: 'migrate', name: 'Demo', state: 'done_with_warnings', target: '/mnt/ext0/games/X',
    source_error: '原文件夹没删掉' });
  assert.match(done.line, /已迁移到 \/mnt\/ext0\/games\/X · 原文件夹没删掉/);
  const removing = ui.uninstallTask({ id: 'u1', kind: 'uninstall', name: 'Pkg', state: 'running', phase: 'icon', path: '', title: 'PPSA05555',
    delete_cheats: false, delete_saves: true });
  assert.equal(removing.line, '检查 → 【移除图标】 → 存档');
  assert.equal(removing.cancel, null, 'a running uninstall cannot be canceled half way');
  const removed = ui.uninstallTask({ id: 'u2', kind: 'uninstall', name: 'Folder', state: 'done', path: '/data/games/X', title: 'PPSA01234',
    steps: ['icon', 'files', 'cheats'], cheats_deleted: 2, warnings: [] });
  assert.equal(removed.line, '已移除主屏幕图标 · 已删除游戏文件夹 · 删除 2 个金手指文件');
  const card = ui.filesTransferCard(removed);
  assert.match(textOf(card), /游戏卸载/);
  assert.doesNotMatch(textOf(card), /预计剩余/, 'no byte statistics for an uninstall');
});

test('backup sources add read-only presets once; backup cards show target, skips and retry', () => {
  const ui = fixture();
  ui.nas.backupRoots = ['/user/av_contents', '/data/savedata'];
  const sources = ui.backupSources({ path: '/data/savedata', name: 'savedata', dir: true });
  assert.deepEqual(JSON.parse(JSON.stringify(sources.map((item) => [item.path, item.checked]))), [['/data/savedata', true], ['/user/av_contents', false]]);
  assert.match(sources[1].label, /只读/);
  const base = { id: 'b', kind: 'backup', name: 'savedata', source: 'PS5 192.0.2.5', nas_name: '家里 NAS',
    target: '/ps5/192.0.2.5/20261112-101010', dest: '/ps5', mode: 'snapshot', bytes_done: 1, bytes_total: 2,
    files_done: 1, files_total: 2, files_skipped: 0, files_failed: 0, errors: [] };
  const running = ui.backupTask({ ...base, state: 'running' });
  assert.equal(running.kind, 'backup');
  assert.match(running.sub, /家里 NAS \/ps5\/192\.0\.2\.5\/20261112-101010（快照）/);
  assert.equal(running.retry, null);
  assert.equal(typeof ui.backupTask({ ...base, state: 'failed', error: '2 个文件没有备份成功' }).retry, 'function');
  assert.match(ui.backupTask({ ...base, state: 'done', files_skipped: 3 }).line, /3 个文件.*跳过/);
  ui.setNasJobs([{ ...base, state: 'queued' }]);
  assert.equal(ui.filesTransferTasks()[0].kind, 'backup');
});

test('overview DNS preview has its own renderer and never writes into the run-log viewer', () => {
  const ui = fixture();
  assert.notEqual(ui.renderQueryLogs, ui.renderLogs);
  ui.renderQueryLogs([{ action: 'allowed', domain: 'a.example' }, { action: 'blocked', domain: 'b.example' }, { action: 'cached' }]);
  assert.equal(ui.node('query-total').textContent, '3');
  assert.equal(ui.node('logs-view').children.length, 0);
});

test('restore results are listed as text with their state; backup names are sanitized', () => {
  const ui = fixture();
  const items = ui.renderRestoreResult({ items: [
    { id: 'policy', title: '访问策略', state: 'restored', detail: '已应用' },
    { id: 'nas', title: 'NAS 连接', state: 'weird', detail: '<b>x</b>' }
  ] });
  assert.equal(items.length, 2);
  const list = ui.node('restore-result');
  assert.equal(list.hidden, false);
  assert.equal(list.children[0].textContent, '访问策略：已恢复 · 已应用');
  assert.equal(list.children[1].dataset.state, 'failed', 'unknown states show as failures');
  assert.match(list.children[1].textContent, /<b>x<\/b>/, 'server text stays text');
  assert.equal(ui.backupFilename('attachment; filename="ps5-jb-backup-1.tar.gz"', 'x'), 'ps5-jb-backup-1.tar.gz');
  assert.equal(ui.backupFilename('attachment; filename="../evil"', 'fallback.tar.gz'), 'fallback.tar.gz');
  ui.renderRestoreResult({});
  assert.equal(list.hidden, true);
});

test('self-check results show a badge, detail and a fix only for problems', () => {
  const ui = fixture();
  ui.renderSelfcheck({ time: '2026-11-12T10:00:00+08:00', results: [
    { id: 'dns', title: 'DNS 劫持', status: 'ok', detail: '3 个内置域名都解析到本机', fix: 'ignored' },
    { id: 'gateway', title: '网关', status: 'fail', detail: 'ps5guard 规则表不存在', fix: '查看运行日志' },
    { id: 'odd', title: '<i>x</i>', status: 'strange', detail: '', fix: '' }
  ] });
  const list = ui.node('selfcheck-list');
  assert.equal(list.hidden, false);
  assert.equal(list.children.length, 3);
  assert.equal(list.children[0].dataset.status, 'ok');
  assert.doesNotMatch(textOf(list.children[0]), /建议/);
  assert.match(textOf(list.children[1]), /异常.*网关.*ps5guard.*建议：查看运行日志/);
  assert.equal(list.children[2].dataset.status, 'fail', 'unknown status is shown as a failure');
  assert.match(textOf(list.children[2]), /<i>x<\/i>/);
  assert.match(ui.node('selfcheck-time').textContent, /1 项异常/);
  ui.renderSelfcheck(null);
  assert.equal(list.hidden, true);
});

test('notification channels list type, events and a missing secret, never a secret value', () => {
  const ui = fixture();
  Object.assign(ui.notifyState, ui.normalizeNotify({ channels: [
    { id: 'a', name: '手机', type: 'bark', enabled: true, events: ['transfer.finished', 'transfer.failed'], has_secret: true, secret_ok: true },
    { id: 'b', name: '', type: 'telegram', enabled: false, events: [], has_secret: false, secret_ok: true },
    { name: 'no id' }
  ], events: { 'transfer.finished': 'NAS 传输完成' } }));
  ui.renderNotify();
  const list = ui.node('notify-list');
  assert.equal(list.hidden, false);
  assert.equal(ui.node('notify-empty').hidden, true);
  assert.equal(list.children.length, 2, 'entries without an id are dropped');
  assert.match(textOf(list.children[0]), /手机.*Bark · 2 个事件/);
  assert.match(textOf(list.children[1]), /Telegram.*已停用.*需要重新填写密钥/);
  assert.match(textOf(list.children[0]), /测试.*编辑.*删除/);
  Object.assign(ui.notifyState, ui.normalizeNotify({ channels: [] }));
  ui.renderNotify();
  assert.equal(ui.node('notify-empty').hidden, false);
  assert.deepEqual(Object.keys(ui.notifyState.events), ['transfer.finished'], 'event labels are kept');
});

test('activity stats draw SVG bars by attribute and offer 放行 only for blocked targets not yet allowed', () => {
  const ui = fixture();
  ui.actHistory.stats = { days: 7, rows: 12, per_day: [{ day: '2026-11-11', allowed: 3, blocked: 1 }, { day: '2026-11-12', allowed: 0, blocked: 5 }],
    top_blocked: [{ target: 'domain:ads.example', hits: 5, clients: 1, last: 1, name: '', category: '广告', allowed_now: false },
      { target: 'ip:203.0.113.9', hits: 1, clients: 1, last: 1, allowed_now: true }],
    top_allowed: [{ target: 'domain:ok.example', hits: 3, clients: 2, last: 1 }], clients: [{ client: '192.0.2.142', allowed: 3, blocked: 6, last: 1800000000 }] };
  ui.renderStats();
  const chart = ui.node('stats-chart');
  const bars = chart.children.filter((node) => node.tagName === 'rect');
  assert.equal(bars.length, 4);
  assert.ok(bars.every((bar) => bar.attributes.height !== undefined && bar.attributes.style === undefined), 'sizes are attributes, never inline style');
  assert.equal(bars[3].attributes.class, 'bar-blocked');
  const blocked = ui.node('stats-top-blocked').children;
  assert.match(textOf(blocked[0]), /ads\.example.*5 次.*广告.*放行/);
  assert.doesNotMatch(textOf(blocked[1]), /放行$/);
  assert.match(textOf(blocked[1]), /已放行（现在）/);
  assert.doesNotMatch(textOf(ui.node('stats-top-allowed').children[0]), /放行$/);
  assert.match(ui.node('stats-summary').textContent, /共 9 条记录，其中未放行 6 条/);
  assert.equal(ui.node('stats-clients').children.length, 1);
});

test('activity history rows reuse the live row without checkboxes', () => {
  const ui = fixture();
  ui.actHistory.loaded = true;
  ui.actHistory.events = [{ id: 3, kind: 'dns', domain: 'ads.example', client: '192.0.2.142', verdict: 'blocked', time: '2026-11-12T10:00:00+08:00', target: 'domain:ads.example', allowed_now: false }];
  ui.actHistory.next = 3;
  ui.renderHistory();
  const body = ui.node('history-body');
  assert.equal(body.children.length, 1);
  assert.match(textOf(body.children[0]), /ads\.example.*未放行/);
  assert.equal(ui.node('history-more').hidden, false);
  assert.match(ui.node('history-summary').textContent, /已显示 1 条，还有更早的记录/);
});

test('hosts remembered from an earlier run are marked until the scan reaches them', () => {
  const ui = fixture();
  const { hosts } = ui.normalizePs5({ hosts: [
    { ip: '192.0.2.8', online: false, remembered: true, last_seen: 1800000000, name: 'Den', firmware: '9.60' },
    { ip: '192.0.2.9', online: true }
  ] });
  assert.equal(hosts[0].remembered, true);
  assert.equal(hosts[0].lastSeen, 1800000000 * 1000);
  assert.equal(hosts[0].rememberedName, 'Den');
  assert.equal(hosts[1].remembered, false, 'older servers omit the field');
});
