module.exports = async (page) => {
  // Reusable browser scenario: require('./console-smoke.js')(page), supplying
  // an existing Playwright Page while web/ is served at 127.0.0.1:8765.
  // Every API is intercepted; no console is contacted.
  const results = [];
  const errors = [];
  const requests = [];
  const writes = [];
  let expired = false;
  let repoFailure = false;
  let emptyRepo = false;
  const now = '2026-11-10T01:20:00Z';
  const payloads = [
    { filename: 'ftpsrv.elf', name: 'FTP 文件服务', version: '0.21', category: '文件工具', author: 'Console tools', description: '局域网文件传输', size: 204800 },
    { filename: 'websrv.elf', name: 'Web 文件管理', version: '0.34', category: '文件工具', author: 'Console tools', description: '网页文件浏览', size: 307200 },
    { filename: 'klogsrv.elf', name: '内核日志服务', version: '0.9', category: '调试工具', author: 'Debug tools', description: '查看运行日志', size: 102400 }
  ];
  const events = [
    { kind: 'flow', target: 'domain:cdn.example.net', time: now, client: '192.168.1.142', domain: 'cdn.example.net', dst: '203.0.113.10', port: 443, verdict: 'blocked', name: '游戏下载', category: '下载' },
    { kind: 'dns', target: 'domain:updates.example.net', time: now, client: '192.168.1.142', domain: 'updates.example.net', dst: '', port: 53, verdict: 'blocked', name: '更新检查', category: '系统' },
    { kind: 'flow', target: 'domain:media.example.net', time: now, client: '192.168.1.142', domain: 'media.example.net', dst: '203.0.113.20', port: 443, verdict: 'allowed', name: '媒体服务', category: '娱乐' },
    { kind: 'service', target: 'ip:192.168.1.10', time: now, client: '192.168.1.142', domain: '', dst: '192.168.1.10', port: 8443, verdict: 'allowed', name: '管理服务', category: '本机服务' }
  ];
  const state = {
    csrf: 'local-ui-test-token', config: { domains: ['media.example.net'], networks: [], upstream_dns: '1.1.1.1' },
    status: { dns: 'running', gateway: 'active', host: 'running', uptime: 172800, host_ip: '192.168.1.10', dynamic_addresses: 3, last_applied: now, ipv6_forwarding: false },
    logs: events.slice(0, 3).map((e) => ({ time: now, name: e.domain, client: e.client, qtype: 'A', action: e.verdict === 'blocked' ? 'blocked' : 'allowed' })),
    firewall: { available: true, counters: [] }
  };
  const host = { ip: '192.168.1.142', online: true, pldmgr: true, elfldr: true, version: '0.6.3', source_attached: true, source_url: 'http://192.168.1.10:8080/repo.json', repo_payloads: 3, installed: ['ftpsrv.elf'], running: [{ pid: 404, name: 'ftpsrv.elf' }], autoload: { enabled: true, list: ['ftpsrv.elf'], status: { remaining: 0, done: 1 } } };
  const onError = (error) => errors.push(error.message);
  page.on('pageerror', onError);
  await page.unroute('**/api/**');
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    requests.push(url.pathname);
    if (expired) return route.fulfill({ status: 401, json: { error: '登录已过期，请重新登录。' } });
    if (request.method() !== 'GET') {
      writes.push({ path: url.pathname, body: request.postData() });
      return route.fulfill({ status: 400, json: { error: 'UI verification forbids writes.' } });
    }
    let json;
    switch (url.pathname) {
      case '/api/state': json = state; break;
      case '/api/repo':
        if (repoFailure) return route.fulfill({ status: 503, json: { error: '仓库暂时不可用' } });
        json = { host: '192.168.1.10', port: 8080, catalog: { name: '家庭插件仓库', items: emptyRepo ? [] : payloads }, total_bytes: emptyRepo ? 0 : 614400 }; break;
      case '/api/activity': json = { events, counts: { allowed: 2, blocked: 2 }, tracked: 4, updated: now, categories: ['系统', '下载', '娱乐'], hints: { by_kind: { flow: 2, dns: 1, service: 1 } } }; break;
      case '/api/ps5': json = { hosts: [host], discovery: { subnet: '192.168.1.0/24', scanned: 254, enabled: true, scanning: false, duration_ms: 1200, last_scan: now } }; break;
      case '/api/files/info': json = { configured: true, reachable: true, host: host.ip, hosts: [host.ip], base: 'http://192.168.1.142' }; break;
      case '/api/files/volumes': json = { volumes: [{ path: '/data', label: '内置存储', kind: 'internal', total: 1000000000000, free: 450000000000 }] }; break;
      case '/api/files/list': json = { path: url.searchParams.get('path') || '/data', entries: [{ name: 'homebrew', dir: true, path: '/data/homebrew', size: 0, mtime: 1794200000 }, { name: 'pldmgr', dir: true, path: '/data/pldmgr', size: 0, mtime: 1794200000 }, { name: 'console-settings-backup-with-a-long-file-name.json', dir: false, path: '/data/console-settings-backup-with-a-long-file-name.json', size: 4096, mtime: 1794200000 }] }; break;
      case '/api/files/transfers':
      case '/api/transfers': json = { transfers: [] }; break;
      case '/api/nas': json = { connections: [] }; break;
      case '/api/titles': json = { titles: {} }; break;
      case '/api/files/job': json = { state: 'idle' }; break;
      case '/api/library': json = { host: host.ip, games: [{ title: 'CUSA12345', name: '一个名字很长很长的游戏用来检查卡片在窄屏上会不会撑破页面', folder: 'CUSA12345', path: '/data/games/CUSA12345', volume: '/data', volume_label: '内置存储', platform: 'PS4', mtime: 1794200000, size: null }], volumes: [{ path: '/data', label: '内置存储' }], errors: [], game_dirs: ['games'] }; break;
      case '/api/library/icon': return route.fulfill({ status: 404, json: { error: '这个游戏没有封面' } });
      default: return route.fulfill({ status: 404, json: { error: 'Unknown fixture API: ' + url.pathname } });
    }
    await route.fulfill({ json });
  });
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const test = async (name, fn) => {
    try { await fn(); results.push({ name, status: 'pass' }); }
    catch (error) { results.push({ name, status: 'fail', error: error.message.split('\n')[0] }); }
  };
  const routeTo = async (section) => {
    await page.evaluate((value) => { location.hash = '#/' + value; }, section);
    await page.locator('#view-' + section).waitFor({ state: 'visible' });
  };
  const eventually = async (fn, message) => {
    for (let n = 0; n < 50; n++) {
      if (await fn()) return;
      await page.waitForTimeout(50);
    }
    throw new Error(message);
  };
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
  await page.goto('http://127.0.0.1:8765/');
  await page.reload();
  await page.locator('#app-shell').waitFor({ state: 'visible' });

  await test('mobile primary navigation exposes PS5 and files', async () => {
    check(await page.locator('.tabbar a[data-section="ps5"]').count() === 1, 'Missing mobile PS5 tab');
    check(await page.locator('.tabbar a[data-section="files"]').count() === 1, 'Missing mobile files tab');
    await page.locator('.tabbar a[data-section="ps5"]').click();
    await page.locator('#view-ps5').waitFor({ state: 'visible' });
    await page.locator('.tabbar a[data-section="files"]').click();
    await page.locator('#files-body tr').first().waitFor();
    check(await page.locator('#view-files').isVisible(), 'Files page was not reached');
  });
  await test('More dialog closes on Escape and restores focus', async () => {
    const trigger = page.locator('#mobile-more-trigger');
    check(await trigger.count() === 1, 'Missing More trigger');
    await trigger.click();
    check(await page.locator('#mobile-more-dialog').isVisible(), 'More dialog is hidden');
    check(await trigger.getAttribute('aria-expanded') === 'true', 'More aria-expanded missing');
    await page.keyboard.press('Escape');
    await page.locator('#mobile-more-dialog').waitFor({ state: 'hidden' });
    await eventually(async () => await trigger.evaluate((el) => document.activeElement === el), 'Focus was not restored');
    await eventually(async () => await trigger.getAttribute('aria-expanded') === 'false', 'More aria-expanded did not reset');
  });
  await test('More links expose all secondary routes and current state', async () => {
    check(await page.locator('#mobile-more-trigger').count() === 1, 'Missing More trigger');
    for (const section of ['policy', 'repository', 'settings']) {
      await page.locator('#mobile-more-trigger').click();
      await page.locator('#mobile-more-dialog a[data-section="' + section + '"]').click();
      await page.locator('#view-' + section).waitFor({ state: 'visible' });
      await page.locator('#mobile-more-dialog').waitFor({ state: 'hidden' });
      check(await page.locator('#mobile-more-trigger').getAttribute('aria-current') === 'page', 'More has no current-page state');
    }
  });
  await test('Activity text search combines with verdict and prunes hidden selections', async () => {
    await routeTo('activity');
    await eventually(async () => await page.locator('#activity-body input[type="checkbox"]').count() === 3, 'Activity fixture did not render');
    check(await page.locator('#activity-search').count() === 1, 'Missing activity search');
    await page.locator('#activity-search').fill('cdn.example');
    check(await page.locator('#activity-body input[type="checkbox"]').count() === 1, 'Activity search did not filter rows');
    await page.locator('#activity-body .activity-check-target').click();
    await page.locator('#activity-search').fill('updates.example');
    check(await page.locator('#activity-body input:checked').count() === 0, 'Hidden selection remained checked');
    check((await page.locator('#activity-selection').innerText()).includes('未选择'), 'Hidden selection persisted in bulk actions');
    await page.locator('#activity-search').fill('media.example');
    await page.locator('.segment[data-filter="blocked"]').click();
    check(await page.locator('#activity-body input[type="checkbox"]').count() === 0, 'Verdict filter ignored search');
    check((await page.locator('#activity-body').innerText()).includes('筛选'), 'No-match state is unclear');
    await page.locator('.segment[data-filter="all"]').click();
    await page.locator('#activity-search').fill('本机服务');
    check(await page.locator('#activity-body tr').count() === 1, 'Service source search failed');
    check(await page.locator('#activity-body input[type="checkbox"]').count() === 0, 'Local service became an authorization target');
    await page.locator('#activity-search').fill('');
  });
  await test('Repository search supports metadata, empty results and refresh', async () => {
    await routeTo('repository');
    check(await page.locator('#repo-search').count() === 1, 'Missing repository search');
    await page.locator('#repo-search').fill('FTP');
    check(await page.locator('#repo-body tr').count() === 1 && (await page.locator('#repo-body').innerText()).includes('ftpsrv.elf'), 'Repository filename/name search failed');
    await page.locator('#app-refresh').click();
    await eventually(async () => !(await page.locator('#app-refresh').isDisabled()), 'Refresh never completed');
    check(await page.locator('#repo-search').inputValue() === 'FTP', 'Refresh cleared search');
    check(await page.locator('#repo-body tr').count() === 1, 'Refresh ignored active search');
    await page.locator('#repo-search').fill('no-such-payload');
    check((await page.locator('#repo-body').innerText()).includes('匹配'), 'Repository no-match state missing');
    check((await page.locator('#repo-summary').innerText()).includes('3'), 'Search modified original repository count');
    await page.locator('#repo-search').fill('');
  });
  await test('Host catalog search retains original installed state', async () => {
    await routeTo('ps5');
    await page.locator('#ps5-detail').waitFor({ state: 'visible' });
    check(await page.locator('#ps5-catalog-search').count() === 1, 'Missing host catalog search');
    await page.locator('#ps5-catalog-search').fill('FTP');
    check(await page.locator('#ps5-catalog-body tr[data-filename]').count() === 1, 'Catalog search did not filter');
    check((await page.locator('#ps5-catalog-body').innerText()).includes('运行中'), 'Installed/running state was lost');
    await page.locator('#ps5-catalog-search').fill('no-such-payload');
    check((await page.locator('#ps5-catalog-body').innerText()).includes('匹配'), 'Catalog no-match state missing');
    await page.locator('#ps5-catalog-search').fill('');
  });
  await test('All routes fit 320/390/768/1440px without page overflow', async () => {
    const overflows = [];
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      for (const section of ['overview', 'policy', 'activity', 'ps5', 'files', 'library', 'repository', 'settings']) {
        await routeTo(section);
        await page.evaluate(() => new Promise(requestAnimationFrame));
        const size = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
        if (size.content > size.viewport + 1) overflows.push({ width, section, ...size });
      }
    }
    check(!overflows.length, 'Page overflows: ' + JSON.stringify(overflows));
  });
  await test('All routes share compact gutters, aligned panels and consistent corners', async () => {
    const violations = [];
    const samples = [];
    for (const colorScheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' });
      for (const width of [320, 390, 767, 768, 1023, 1024, 1440, 1920, 2560]) {
        await page.setViewportSize({ width, height: 900 });
        for (const section of ['overview', 'policy', 'activity', 'ps5', 'files', 'library', 'repository', 'settings']) {
          await routeTo(section);
          await page.evaluate(() => new Promise(requestAnimationFrame));
          const sample = await page.evaluate(() => {
            const header = document.querySelector('.app-bar');
            const view = document.querySelector('.view:not([hidden])');
            const rect = (el) => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, width: r.width }; };
            const visible = (el) => el.getClientRects().length > 0;
            const panel = (el) => getComputedStyle(el).borderTopLeftRadius;
            const controls = [...view.querySelectorAll('.button, .search-field, .files-search, input:not([type="checkbox"]):not([type="radio"]):not([type="file"]):not([type="search"]), textarea, select')].filter(visible);
            return {
              header: rect(header), view: rect(view),
              sidebar: visible(document.querySelector('.sidebar')) ? rect(document.querySelector('.sidebar')) : null,
              viewport: document.documentElement.clientWidth,
              usableWidth: document.body.clientWidth,
              scrollWidth: document.documentElement.scrollWidth,
              panelRadii: [header, ...view.querySelectorAll('.card')].filter(visible).map(panel),
              controls: controls.map((el) => ({ id: el.id || el.className, radius: panel(el) }))
            };
          });
          const gutter = width < 768 ? 12 : 16;
          const prefix = `${colorScheme}/${width}/${section}`;
          const report = (condition, message) => { if (!condition) violations.push(`${prefix}: ${message}`); };
          report(Math.abs(sample.header.left - sample.view.left) <= 1 && Math.abs(sample.header.right - sample.view.right) <= 1, 'header and content edges do not align');
          report(Math.abs(sample.view.left - (sample.sidebar?.right || 0) - gutter) <= 1 && Math.abs(sample.usableWidth - sample.view.right - gutter) <= 1, 'page gutters exceed compact spacing');
          report(sample.panelRadii.every((radius) => radius === '12px'), 'major panels use inconsistent corners: ' + sample.panelRadii.join(','));
          report(sample.controls.every((control) => control.radius === '8px'), 'controls use inconsistent corners: ' + JSON.stringify(sample.controls.filter((control) => control.radius !== '8px')));
          report(sample.scrollWidth <= sample.viewport + 1, 'page overflows horizontally');
          samples.push({ colorScheme, width, section, ...sample });
        }
      }
    }
    // Retain compact measurements for the calling runner/report.
    page.__layoutSamples = samples;
    check(!violations.length, violations.join('; '));
  });
  await test('Overview details keep one inset and file storage follows the panel corner', async () => {
    for (const width of [390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await routeTo('overview');
      const details = await page.locator('.detail-card').evaluateAll((elements) => elements.map((el) => ({
        padding: getComputedStyle(el).padding,
        summaryInset: el.querySelector('summary').getBoundingClientRect().left - el.getBoundingClientRect().left,
        radius: getComputedStyle(el).borderTopLeftRadius
      })));
      check(details.every((item) => item.padding === '0px' && item.summaryInset <= 1 && item.radius === '12px'), 'Overview details gained a duplicate inset: ' + JSON.stringify(details));
      await routeTo('files');
      const corners = await page.locator('.files-rail').evaluate((el) => ({
        rail: getComputedStyle(el).borderBottomLeftRadius,
        frame: getComputedStyle(el.parentElement).borderBottomLeftRadius
      }));
      check(corners.frame === '11px' && corners.rail === (width >= 1024 ? '11px' : '0px'), 'Storage background misses the panel corner: ' + JSON.stringify(corners));
      if (width < 1024) {
        const empty = await page.locator('#nas-list .files-rail-empty').boundingBox();
        check(empty && empty.width >= 160 && empty.height < 100, 'Empty NAS hint was squeezed into a tall narrow column: ' + JSON.stringify(empty));
      }
    }
  });
  await test('Disclosure focus remains inside its clip and host details clear the sticky header', async () => {
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 500 });
      await routeTo('overview');
      await page.keyboard.press('Tab');
      const focus = await page.locator('.detail-summary').first().evaluate((el) => {
        el.focus();
        const style = getComputedStyle(el);
        return { width: parseFloat(style.outlineWidth), offset: parseFloat(style.outlineOffset) };
      });
      check(focus.width >= 2 && focus.offset <= -focus.width, 'Disclosure focus ring is clipped: ' + JSON.stringify(focus));
      await routeTo('ps5');
      await page.locator('#ps5-detail').waitFor({ state: 'visible' });
      await page.locator('#ps5-detail').evaluate((el) => el.scrollIntoView({ block: 'start', behavior: 'instant' }));
      const positions = await page.evaluate(() => ({
        header: document.querySelector('.app-bar').getBoundingClientRect().bottom,
        detail: document.querySelector('#ps5-detail').getBoundingClientRect().top
      }));
      check(positions.detail >= positions.header + 8, 'Sticky header covers the host detail: ' + JSON.stringify(positions));
      await page.evaluate(() => window.scrollTo(0, 0));
    }
  });
  await test('More dialog closes when switching to desktop', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    check(await page.locator('#mobile-more-trigger').count() === 1, 'Missing More trigger');
    await page.locator('#mobile-more-trigger').click();
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.locator('#mobile-more-dialog').waitFor({ state: 'hidden', timeout: 2000 });
  });
  await test('Repository failures keep usable data and show recovery message', async () => {
    await routeTo('repository');
    repoFailure = true;
    await page.locator('#app-refresh').click();
    await page.locator('#repo-error').waitFor({ state: 'visible' });
    check((await page.locator('#repo-error').innerText()).includes('仓库'), 'Repository error was not described');
    check((await page.locator('#repo-body').innerText()).includes('ftpsrv.elf'), 'Failed read discarded usable list');
    repoFailure = false;
    await page.locator('#app-refresh').click();
    await page.locator('#repo-error').waitFor({ state: 'hidden' });
  });
  await test('Empty repository is distinguished from search no-match', async () => {
    await routeTo('repository');
    emptyRepo = true;
    await page.locator('#app-refresh').click();
    await eventually(async () => (await page.locator('#repo-summary').innerText()).startsWith('共 0 个插件'), 'Empty fixture not applied');
    check((await page.locator('#repo-body').innerText()).includes('暂无插件'), 'Empty repository state missing');
    emptyRepo = false;
    await page.locator('#app-refresh').click();
    await eventually(async () => (await page.locator('#repo-body').innerText()).includes('ftpsrv.elf'), 'Repository did not recover');
  });
  await test('Session expiry closes More and returns to login', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await routeTo('overview');
    check(await page.locator('#mobile-more-trigger').count() === 1, 'Missing More trigger');
    await page.locator('#mobile-more-trigger').click();
    expired = true;
    await page.evaluate(() => document.querySelector('#app-refresh').click());
    await page.locator('#login-view').waitFor({ state: 'visible' });
    await page.locator('#mobile-more-dialog').waitFor({ state: 'hidden' });
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      const login = await page.locator('.login-main').evaluate((el) => ({
        padding: getComputedStyle(el).padding,
        radius: getComputedStyle(el.querySelector('.login-card')).borderTopLeftRadius,
        content: document.documentElement.scrollWidth,
        viewport: document.documentElement.clientWidth
      }));
      check(login.padding === '0px' && login.radius === '12px' && login.content <= login.viewport + 1, 'Login inherits app layout or overflows: ' + JSON.stringify({ width, ...login }));
    }
    expired = false;
  });
  await test('Read-only UI interactions did not issue any writes or JS errors', async () => {
    check(writes.length === 0, 'Unexpected writes: ' + JSON.stringify(writes));
    check(errors.length === 0, 'JavaScript errors: ' + JSON.stringify(errors));
  });
  // Leave the page displaying realistic fixture data for visual inspection.
  expired = false;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('http://127.0.0.1:8765/#/overview');
  await page.reload();
  await page.locator('#app-shell').waitFor({ state: 'visible' });
  page.off('pageerror', onError);
  return { results, passed: results.filter((r) => r.status === 'pass').length, failed: results.filter((r) => r.status === 'fail').length, errors, writes, requestCount: requests.length };
}
