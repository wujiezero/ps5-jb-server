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
}

function fixture() {
  const nodes = new Map();
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Element());
      return nodes.get(id);
    },
    querySelectorAll() { return []; },
    createElement: (tag) => new Element(tag),
    createDocumentFragment: () => new Element('fragment'),
    body: new Element('body')
  };
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const startup = source.lastIndexOf('  bindEvents();');
  assert.ok(startup > 0, 'browser module startup must be identifiable');
  const exposed = `${source.slice(0, startup)}
    globalThis.subject = { activity, repo, activitySelected, activityVisibleEvents,
      setActivityFilter, renderRepo, renderPs5Catalog,
      setAuthenticated(value) { authenticated = value; } };
  })();`;
  const context = { document, console, location: { hostname: '192.168.1.2' }, setTimeout, clearTimeout };
  vm.runInNewContext(exposed, context);
  return { ...context.subject, node: (id) => document.getElementById(id) };
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
