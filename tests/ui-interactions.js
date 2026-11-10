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
    this.value = '';
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
  }
  append(...nodes) { this.children.push(...nodes.flatMap((node) => node.tagName === 'fragment' ? node.children : [node])); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener() {}
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
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const startup = source.lastIndexOf('  bindEvents();');
  assert.ok(startup > 0, 'browser module startup must be identifiable');
  const exposed = `${source.slice(0, startup)}
    globalThis.subject = { activity, repo, activitySelected, activityVisibleEvents,
      setActivityFilter, renderRepo, renderPs5Catalog,
      files, filesRow, nasRow, filesVisibleEntries, titleIdOf, titleNames, flushTitleNames, normalizeFilesEntries,
      nas, nasArchiveSelection, extractTask,
      setAuthenticated(value) { authenticated = value; } };
  })();`;
  const context = { document, console, location: { hostname: '192.168.1.2' }, setTimeout, clearTimeout,
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

test('macOS ._ metadata files never show in the file list', () => {
  const ui = fixture();
  const listed = ui.normalizeFilesEntries({ path: '/data', entries: [
    { name: '._PPSA03671', kind: 'file', size: 4096 }, { name: 'PPSA03671', dir: true }, { name: '.hidden', size: 1 }
  ] }, '/data');
  assert.equal(JSON.stringify(listed.entries.map((entry) => entry.name)), JSON.stringify(['PPSA03671', '.hidden']));
});
