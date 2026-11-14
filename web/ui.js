'use strict';

/* ── 通用 DOM 小工具（新页面共用） ─────────────────────────────────── */

const reducedMotion = () => Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
// Phone layout (same breakpoint as the CSS tab bar / bottom sheets).
const isPhone = () => Boolean(window.matchMedia && window.matchMedia('(max-width: 767px)').matches);

/* ── 手机：底部操作面板（action sheet） ─────────────────────────────── */
// Popover menus and long button rows become a bottom sheet on phones. The
// chosen action runs after the sheet has closed so follow-up dialogs
// (delete confirm, rename…) open on a clean stack.
let actionSheetState = null;

function openActionSheet({ title, sub = '', items, returnFocus = null, onDismiss = null }) {
  const dialog = $('action-sheet');
  if (!dialog || dialog.open) return;
  $('action-sheet-title').textContent = title;
  const subNode = $('action-sheet-sub');
  subNode.textContent = sub;
  subNode.hidden = !sub;
  const list = $('action-sheet-list');
  const fragment = document.createDocumentFragment();
  actionSheetState = { chosen: null, returnFocus, onDismiss };
  for (const item of items) {
    const button = makeElement('button', 'action-sheet-item');
    button.type = 'button';
    if (item.icon) button.append(makeIcon(item.icon));
    const text = makeElement('span', 'action-sheet-text');
    text.append(makeElement('span', 'action-sheet-label', item.label));
    if (item.hint) text.append(makeElement('span', 'action-sheet-hint', item.hint));
    button.append(text);
    if (item.danger) button.dataset.tone = 'danger';
    button.disabled = Boolean(item.disabled);
    button.addEventListener('click', () => {
      if (actionSheetState) actionSheetState.chosen = item;
      dialog.close();
    });
    fragment.append(button);
  }
  list.replaceChildren(fragment);
  dialog.showModal();
  const first = list.querySelector('button:not(:disabled)') || $('action-sheet-cancel');
  first.focus({ preventScroll: true });
}

function onActionSheetClosed() {
  const state = actionSheetState;
  actionSheetState = null;
  $('action-sheet-list').replaceChildren();
  if (!state) return;
  if (state.chosen) {
    state.chosen.onSelect();
    return;
  }
  if (state.onDismiss) state.onDismiss();
  if (state.returnFocus && state.returnFocus.isConnected) state.returnFocus.focus({ preventScroll: true });
}

/* ── 手机：下拉关闭底部面板 ─────────────────────────────────────────── */
// Drag the sheet's top strip (handle / title) downwards to dismiss it, like
// native sheets. The cancel event is honoured, so busy dialogs stay put.
function bindSheetSwipe(dialog) {
  if (!dialog.querySelector('.sheet-handle')) return;
  let drag = null;
  const reset = () => {
    dialog.style.transform = '';
    dialog.style.transition = '';
  };
  dialog.addEventListener('pointerdown', (event) => {
    if (!isPhone() || event.button !== 0 || dialog.dataset.closing === 'true') return;
    if (event.target.closest('button, a, input, select, textarea, label')) return;
    const top = dialog.getBoundingClientRect().top;
    if (event.clientY - top > 64 || dialog.scrollTop > 0) return;
    drag = { id: event.pointerId, y: event.clientY, t: event.timeStamp, dy: 0 };
  });
  dialog.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    drag.dy = Math.max(0, event.clientY - drag.y);
    if (drag.dy > 4 && !dialog.hasPointerCapture(event.pointerId)) dialog.setPointerCapture(event.pointerId);
    dialog.style.transition = 'none';
    dialog.style.transform = `translateY(${drag.dy}px)`;
  });
  const end = (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const { dy, t } = drag;
    drag = null;
    const fast = dy > 40 && dy / Math.max(1, event.timeStamp - t) > 0.6;
    if (dy > 110 || fast) {
      const cancel = new Event('cancel', { cancelable: true });
      if (dialog.dispatchEvent(cancel)) {
        dialog.style.transition = '';
        dialog.close();
        return;
      }
    }
    dialog.style.transition = 'transform 220ms var(--ease-drawer)';
    dialog.style.transform = '';
    setTimeout(reset, 240);
  };
  dialog.addEventListener('pointerup', end);
  dialog.addEventListener('pointercancel', end);
  dialog.addEventListener('close', reset);
}

// Mark a container so its CSS enter animation plays once; polling renders
// later do not carry the flag and therefore never replay it.
const enterTimers = new WeakMap();
function playEnter(node, duration = 520) {
  if (!node) return;
  clearTimeout(enterTimers.get(node));
  node.removeAttribute('data-enter');
  void node.offsetWidth; // restart the animation when navigating quickly
  node.dataset.enter = 'true';
  enterTimers.set(node, setTimeout(() => node.removeAttribute('data-enter'), duration));
}

// <dialog>.close() is instant; play a short exit first. Code that checks
// dialog.open right after close() still works: open flips once it is done.
function animateDialogClose(dialog) {
  const close = dialog.close.bind(dialog);
  dialog.close = (value) => {
    if (!dialog.open || dialog.dataset.closing === 'true') return;
    if (reducedMotion() && !window.matchMedia('(max-width: 767px)').matches) {
      close(value);
      return;
    }
    dialog.dataset.closing = 'true';
    const done = () => {
      dialog.removeEventListener('animationend', done);
      clearTimeout(timer);
      delete dialog.dataset.closing;
      close(value);
    };
    const timer = setTimeout(done, 260);
    dialog.addEventListener('animationend', done);
  };
  dialog.addEventListener('close', () => { delete dialog.dataset.closing; });
}

const makeElement = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const makeIcon = (name) => {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#${name}`);
  svg.append(use);
  return svg;
};

const tableEmptyRow = (colSpan, message) => {
  const row = document.createElement('tr');
  const cell = document.createElement('td');
  cell.colSpan = colSpan;
  cell.className = 'empty-state';
  cell.textContent = message;
  row.append(cell);
  return row;
};

const sameStringSet = (left, right) => {
  const a = new Set(Array.from(left || []));
  const b = new Set(Array.from(right || []));
  return a.size === b.size && [...a].every((value) => b.has(value));
};

/* ── 进度展示（服务更新 / 插件上游 / 主机安装共用） ─────────────────── */

let ps5Progress = null;          // 当前主机安装 / 更新的服务端阶段
let ps5Batch = null;             // 批量更新：{ index, total, name }
let ps5UpgradeTarget = null;     // 更新确认对话框的目标
let upstreamBatch = null;        // 插件上游更新：{ index, total, name, version, progress }

const PS5_STAGES = { checking: '检查主机状态', stopping: '停止旧进程', reading: '读取仓库文件',
  uploading: '上传到主机', verifying: '确认已保存', running: '启动新版本' };
const PS5_STAGE_SPAN = { checking: [0, 0.06], stopping: [0.06, 0.14], reading: [0.14, 0.2],
  uploading: [0.2, 0.86], verifying: [0.86, 0.93], running: [0.93, 0.99] };
const UPSTREAM_STAGES = { downloading: '下载', verifying: '校验', publishing: '写入仓库' };
const UPDATE_STEPS = [['downloading', '下载'], ['validating', '校验'], ['backing_up', '备份'],
  ['installing', '安装'], ['restarting', '重启'], ['verifying', '确认']];

const percentOf = (done, total) => (Number(total) > 0 ? Math.max(0, Math.min(1, Number(done) / Number(total))) : null);

// 主请求进行时轮询一个只读进度接口；主请求自己负责报告失败。
function startProgressPoll(path, onData, interval = 450) {
  let stopped = false;
  let timer = null;
  const tick = async () => {
    if (stopped) return;
    try {
      const data = await api(path, undefined, 5000);
      if (!stopped) onData(data);
    } catch { /* 进度只是锦上添花 */ }
    if (!stopped) timer = setTimeout(tick, interval);
  };
  timer = setTimeout(tick, 200);
  return () => { stopped = true; clearTimeout(timer); };
}

function renderOpProgress(id, state) {
  const box = $(id);
  if (!box) return;
  box.hidden = !state;
  if (!state) return;
  const [label, percent] = box.querySelectorAll('.op-label > span');
  const fill = box.querySelector('.op-bar > span');
  const fraction = typeof state.fraction === 'number' ? Math.max(0, Math.min(1, state.fraction)) : null;
  if (label) label.textContent = state.label || '';
  if (percent) percent.textContent = fraction === null ? '' : `${Math.round(fraction * 100)}%`;
  box.dataset.indeterminate = String(fraction === null);
  if (fill) fill.style.width = fraction === null ? '' : `${(fraction * 100).toFixed(1)}%`;
}

function ps5StageFraction(progress) {
  const span = PS5_STAGE_SPAN[progress && progress.stage] || [0, 0.05];
  const inner = progress && progress.stage === 'uploading' ? percentOf(progress.done, progress.total) : null;
  return span[0] + (span[1] - span[0]) * (inner === null ? 0.5 : inner);
}

function ps5StageText(progress) {
  const label = PS5_STAGES[progress && progress.stage] || '处理中';
  const inner = progress && progress.stage === 'uploading' ? percentOf(progress.done, progress.total) : null;
  return inner === null ? `${label}…` : `${label} ${Math.round(inner * 100)}%`;
}

function renderPs5OpProgress() {
  if (!ps5Batch || !ps5Progress) {
    renderOpProgress('ps5-op-progress', ps5Batch ? { label: `正在更新 ${ps5Batch.index + 1} / ${ps5Batch.total}：${ps5Batch.name}`, fraction: null } : null);
    return;
  }
  const step = ps5StageFraction(ps5Progress);
  const bytes = ps5Progress.stage === 'uploading' && Number(ps5Progress.total) > 0
    ? ` · ${formatBytes(ps5Progress.done)} / ${formatBytes(ps5Progress.total)}` : '';
  renderOpProgress('ps5-op-progress', {
    label: `正在更新 ${ps5Batch.index + 1} / ${ps5Batch.total}：${ps5Batch.name} · ${PS5_STAGES[ps5Progress.stage] || '处理中'}${bytes}`,
    fraction: (ps5Batch.index + step) / ps5Batch.total
  });
}

function watchPs5Progress(ip, filename) {
  return startProgressPoll(`/api/ps5/progress?ip=${encodeURIComponent(ip)}`, (data) => {
    const progress = data && data.progress && typeof data.progress === 'object' ? data.progress : null;
    if (progress && progress.filename && progress.filename !== filename) return;
    if (progress) ps5Progress = { ...progress, filename };
    renderPs5OpProgress();
    const host = ps5HostByIp(ip);
    if (host && ps5Tab === 'catalog' && !$('ps5-detail').hidden) syncPs5CatalogRows(host);
  });
}

function renderUpstreamProgress() {
  if (!upstreamBatch) {
    renderOpProgress('repo-upstream-progress', null);
    return;
  }
  const progress = upstreamBatch.progress;
  const stage = progress && progress.stage;
  const inner = stage === 'downloading' ? percentOf(progress.done, progress.total)
    : stage === 'verifying' ? 1 : stage === 'publishing' ? 1 : null;
  const share = stage === 'downloading' ? 0.85 * (inner ?? 0.3) : stage === 'verifying' ? 0.9 : stage === 'publishing' ? 0.96 : 0.02;
  const bytes = stage === 'downloading' && progress.done
    ? ` · ${formatBytes(progress.done)}${Number(progress.total) > 0 ? ` / ${formatBytes(progress.total)}` : ''}` : '';
  const prefix = upstreamBatch.total > 1 ? `${upstreamBatch.index + 1} / ${upstreamBatch.total}：` : '';
  renderOpProgress('repo-upstream-progress', {
    label: `${prefix}${upstreamBatch.name} ${upstreamBatch.version} · ${UPSTREAM_STAGES[stage] || '连接上游'}${bytes}`.replace(/\s+·/, ' ·'),
    fraction: stage === 'downloading' && inner === null ? null : (upstreamBatch.index + share) / upstreamBatch.total
  });
}

function renderUpdateStepper(job, active) {
  const box = $('update-stepper');
  if (!box) return;
  const state = job && job.state;
  const show = Boolean(active || updates.reconnect);
  box.hidden = !show;
  if (!show) return;
  let current = UPDATE_STEPS.findIndex(([key]) => key === state);
  if (updates.reconnect && current < 4) current = 4;   // 断开连接通常意味着正在重启
  const list = $('update-steps');
  const items = UPDATE_STEPS.map(([key, label], index) => {
    const item = makeElement('li', 'op-step', label);
    item.dataset.state = index < current ? 'done' : index === current ? 'active' : 'pending';
    if (index === current) item.setAttribute('aria-current', 'step');
    return item;
  });
  list.replaceChildren(...items);
  const download = state === 'downloading' ? percentOf(job.bytes_done, job.bytes_total) : null;
  const base = Math.max(0, current);
  const fraction = state === 'queued' ? 0.02 : (base + (state === 'downloading' ? (download ?? 0.15) : 0.5)) / UPDATE_STEPS.length;
  const fill = $('update-bar');
  if (fill) fill.style.width = `${(Math.max(0.02, Math.min(1, fraction)) * 100).toFixed(1)}%`;
  const label = state === 'downloading' && Number(job.bytes_total) > 0
    ? `正在下载更新包 ${formatBytes(job.bytes_done || 0)} / ${formatBytes(job.bytes_total)}`
    : updates.reconnect ? '服务正在重启，页面会自动重新连接…'
      : state === 'queued' ? '已提交，等待开始…' : `${UPDATE_STATE_LABELS[state] || '处理中'}…`;
  $('update-bar-label').textContent = label;
}

/* ── 功能说明：图标悬停 / 聚焦 / 点按显示 ─────────────────────────── */

const HELP = {
  dns: { title: 'DNS 服务', body: '本机在 UDP / TCP 53 端口为 PS5 解析域名，只解析「访问策略」白名单里的域名，其余查询一律拒绝，PS5 因此连不上系统更新、遥测等服务。另有几个内置域名（用户手册入口等）固定由本机应答，见「访问策略」里的说明。',
    steps: ['在 PS5 的网络设置里把主 DNS 指向本机（见概览「PS5 网络设置」）。', '每次查询及结果都记录在「网络活动」。'] },
  gateway: { title: 'IPv4 网关', body: 'PS5 把默认网关指向本机后，出网的 IPv4 流量都要经过本机的 nftables 规则：默认拒绝，只放行白名单域名解析出的 IP 和白名单里的 IP / 网段。局域网访问不受影响。',
    steps: ['IPv6 不经本机转发，家庭网络应关闭 IPv6 出网。', '「错误」通常是规则写入失败，详情见设置 → 运行日志。'] },
  manual: { title: '用户手册入口', body: 'PS5 的「用户手册」会访问 manuals.playstation.net 等内置域名。本机把这些域名解析到自己（不需要、也不应该加入白名单），并在 HTTPS 443 端口托管越狱页面，在 PS5 上打开用户手册即可进入。',
    steps: ['PS5：设置 → 用户指南、健康与安全以及其他信息 → 用户手册。', '状态异常时看设置 → 服务信息里的用户手册服务（ps5-jb-host）。'] },
  'repo-service': { title: '插件仓库', body: '本机在 8080 端口提供 Payload Manager（pldmgr）能识别的插件源 repo.json，PS5 添加这个源后就能下载这里的插件。',
    steps: ['在「插件仓库」上传 .elf / .bin，或从官方源检查更新。', '在「PS5 主机」卡片上点「接入本服务仓库」，自动把源加到主机。'] },
  runtime: { title: '运行信息', body: '运行时间是管理服务本次启动以来的时长；「最近应用」是白名单最后一次写入网关的时间；「动态授权地址」是白名单域名当前解析出、正临时放行的 IP 数，DNS 记录过期后自动收回。' },
  'dns-log': { title: '最近 DNS 请求', body: '最近几条 PS5 的 DNS 查询和处理结果。「已拒绝 · 不在域名白名单」表示该域名被拦截。',
    steps: ['需要放行时，到「网络活动」勾选后批量放行，或在「访问策略」手动添加。'] },
  'ps5-network': { title: 'PS5 网络设置', body: 'PS5 需要手动设置网络才会经过本机：IP 地址保持自动获取，主 DNS 与默认网关都填本机地址，次 DNS 留空。',
    steps: ['PS5：设置 → 网络 → 设置 → 设定互联网连接，选中当前网络后进入「高级设置」。', '设好后「测试互联网连接」能获取 IP 即可；连接互联网失败是默认拒绝的正常结果。'] },
  counters: { title: '网关计数器', body: '网关防火墙（nftables）各条规则累计的数据包和字节数，用来判断 PS5 的流量是否真的经过本机，以及被放行还是被丢弃。',
    steps: ['重新应用白名单或重启服务后计数可能清零。'] },
  whitelist: { title: '访问白名单', body: '允许 PS5 访问的互联网目标，其余全部拒绝。修改后点右上角「保存并应用」才生效。',
    steps: ['example.com 只匹配该域名本身；*.example.com 匹配所有子域名但不含根域名，两者都要时分两行写。', 'IPv4 / CIDR 按目标地址直接放行，也适用于不经 DNS 的连接。', '共用 CDN 的域名放行后，同一 IP 上的其他服务也可能可达。', '用户手册、连网检测用的内置域名始终由本机应答，不用写在这里。'] },
  'upstream-dns': { title: '上游 DNS', body: '本机解析白名单域名时转发到的 DNS 服务器，只支持 IPv4 地址，一般填路由器地址或公共 DNS（例如 223.5.5.5）。' },
  apply: { title: '应用与回退', body: '「保存并应用」立即重写 DNS 白名单和网关规则；服务器保留上一版配置，改错时用「回退上一版」一键恢复。' },
  activity: { title: '网络活动', body: '实时记录经过本机网关的 IPv4 连接、PS5 的 DNS 查询，以及访问本机服务（管理页、插件仓库、pldmgr 等）的事件。',
    steps: ['勾选「未放行」的目标后点「放行选中」，域名或 IP 加入白名单并立即生效。', '「命名选中」给目标起名、指定分类，以后更容易辨认。', '标「本机劫持」的 DNS 记录来自内置域名，由本机直接应答，不是白名单放行。', '本机服务事件只作记录，不能放行。', '「历史」按时间、结果、来源和关键字查以前的记录（默认保留 30 天，可在 config.toml 的 [activity] retention_days 修改），可导出 CSV。', '「统计」按天汇总，并列出被拦截最多的目标，可直接放行。'] },
  discovery: { title: '主机发现', body: '扫描本机所在的 IPv4 网段，找出运行 pldmgr（8084）或 elfldr（9021）的 PS5；处于待机的主机也会通过 DDP 协议被发现。扫描不访问互联网。',
    steps: ['扫不到时手动输入 PS5 的 IP 添加。', '越狱后先在 PS5 上运行 Payload Manager，管理功能才完整。'] },
  hosts: { title: '发现的主机', body: '每张卡片是一台 PS5：在线状态、pldmgr / elfldr 是否可用、是否已接入本服务仓库，以及可安装、运行中、可更新的插件数。',
    steps: ['「接入本服务仓库」把本机插件源加到这台 PS5 的 pldmgr。', '「待机 / 唤醒」需要先「配对遥控游玩」，唤醒通过遥控游玩协议完成。', '「退出 pldmgr」只让 Payload Manager 退出，不会关闭主机。'] },
  'ps5-detail': { title: '主机详情', body: '选中主机的插件管理，分三个标签：',
    steps: ['插件库：把本服务仓库里的插件安装到主机，或安装后立即运行；主机上的版本比仓库旧时显示「可更新」，可一键「更新并应用」。', '运行中：查看并停止主机上正在运行的插件进程。', '自动加载：设置 pldmgr 启动后按什么顺序、间隔多久自动加载哪些插件。'] },
  'ps5-upgrade': { title: '更新并应用', body: '把主机上旧版本的插件替换为插件仓库里的版本。',
    steps: ['旧版本正在运行：先停止旧进程，上传新版本，再启动新版本。', '没有运行：只替换文件，下次手动运行或自动加载时生效。', '版本按 pldmgr 的插件槽位与文件名里的版本号比较；读不出版本号时显示「可能有更新」，请自行确认。', '通过本服务安装过的插件会记住当时的文件，比对更准确。'] },
  autoload: { title: '自动加载', body: 'pldmgr 启动后先倒计时，再按列表顺序加载插件；每个插件后面可以设等待时间再加载下一个。设置保存在 PS5 上，下次越狱启动 pldmgr 时生效。',
    steps: ['常见顺序：先加载 kstuff 等内核补丁，等几秒，再加载文件服务、ShadowMount+ 等。', '如果主机上的列表已在别处被修改，保存前会提示是否覆盖。'] },
  files: { title: 'PS5 文件', body: '通过本机代理浏览 PS5 的存储（内置 /data、M.2 扩展 /mnt/ext*、USB /mnt/usb*），浏览器不直连 PS5。',
    steps: ['PS5 需要运行 zftpd（FTP 2120）或 tom-ps5-usbnet 的文件服务，本页自动选择可用的一个。', '支持上传（可拖拽）、新建文件夹、重命名、复制 / 移动、删除和下载，删除与覆盖都会先确认。', '游戏文件夹和镜像按标题 ID 显示游戏名。'] },
  'ps5-storage': { title: 'PS5 存储', body: 'PS5 上可浏览的存储位置和剩余空间。FTP 模式下容量通过 PS5 上 web 文件管理（8888 端口）读取，读不到时显示未知。' },
  nas: { title: 'NAS 存储', body: '把家里 NAS 的共享目录接进来，文件从 NAS 经本机中转直接写到 PS5，不落本机磁盘。',
    steps: ['点「＋ 添加」选择 SMB、WebDAV 或 FTP，填写地址、共享名与账号。', '账号密码用 AES-256-GCM 加密保存，页面和接口都不会回显密码。', '勾选文件后点「传到 PS5」，进度在「传输任务」查看。'] },
  'nas-extract': { title: '流式解压安装', body: '把 NAS 上的 RAR 压缩包（单个 .rar 或 *.partNN.rar 分卷）边读边解压，直接写进 PS5，不用先下载到电脑，也不占本机磁盘。',
    steps: ['在 NAS 列表勾选压缩包（分卷任选一个即可），点「解压安装」；同组分卷会自动找齐，缺卷会提前提示。', '「安装」按包内 eboot.bin 与 param.json 找到游戏根目录，放到目标位置下以标题 ID 命名的文件夹，ShadowMount+ 可直接识别；「合并」用于把补丁覆盖进已有游戏文件夹。', '解压完成后默认在 PS5 主屏幕注册游戏图标（需要 ps5upload 引擎）。', '不支持带密码的包和 RAR4 旧格式；固实包或大字典包需要服务器装有 unrar 引擎。失败或取消会自动清理半成品。', '压缩包旁边有名字含「金手指 / cheat / trainer」的 ZIP 时，可以勾选「同时导入金手指」，解压完成后写入 /data/etaHEN/cheats。', '.pkg 文件行有「安装 PKG」：由 ps5upload 引擎安装，主机经本服务直接读取 NAS 上的 PKG（需要 PS5 上运行 ps5upload 插件）；金手指 ZIP 行有「导入金手指」。导入后需要在 etaHEN 里重新加载金手指。'] },
  transfers: { title: '传输任务', body: '集中显示上传、复制 / 移动、NAS 传输、解压、游戏迁移和卸载任务的进度、速度与剩余时间，可随时取消。',
    steps: ['NAS 传输与解压在服务器上执行，关闭页面不会中断。', '从浏览器上传的文件需要保持页面打开直到完成。'] },
  repo: { title: '插件载荷', body: 'PS5 能安装的插件载荷（.elf / .bin）。这里的文件同时发布到本机插件源，PS5 的 pldmgr 和「PS5 主机」页都从这里安装。',
    steps: ['右上角「上传插件」添加文件，名称和版本会从文件名自动识别。', '每行可编辑信息、查看历史版本或删除；每个插件保留最近 3 个旧版本，可回退。'] },
  'repo-endpoint': { title: 'Payload Manager 源地址', body: 'pldmgr 使用的插件源地址（repo.json）。在 PS5 的 Payload Manager 里添加这个源即可浏览安装；也可以在「PS5 主机」卡片上一键接入。' },
  upstream: { title: '上游更新', body: '对照 Payload Manager 官方源（itsPLK/ps5-payloads-mirror）检查仓库插件的新版本。只在点击时联网，不会自动检查。',
    steps: ['下载的文件按官方索引的 SHA-256 校验，不一致就放弃。', '更新后旧版本自动保留在「历史」里，可随时回退。', '仓库更新后，PS5 上的旧版本会在「PS5 主机 → 插件库」提示一键更新。', '网络受限时可给服务配置代理（设置 → 服务更新 → 网络代理）。'] },
  'service-update': { title: '服务更新', body: '从 GitHub 检查本服务的正式发布版本，并在网页上一键升级。',
    steps: ['升级包会校验 SHA-256，先备份再替换程序，配置、插件和数据都保留。', '升级中服务会重启，页面短暂断开后自动重连；新版本启动失败会自动回滚。', '有传输或解压任务时不能升级。'] },
  proxy: { title: '网络代理', body: '访问 GitHub 和插件官方源时使用的 HTTP 代理，来自服务的环境变量 PS5JB_PROXY 或 HTTPS_PROXY。PS5、NAS 和本机插件仓库始终直连。',
    steps: ['安装时用 install.sh --proxy 设置，或给 systemd 服务加 drop-in 设置环境变量后重启服务。'] },
  selfcheck: { title: '自检', body: '一次检查 PS5 用到的各个环节：本机 DNS 是否劫持内置域名、用户手册与管理页证书、插件仓库、网关规则与网卡、上游 DNS、conntrack、每台已知 PS5 的 DNS 和网关是否指向本机、IPv6 绕过风险、可选组件和磁盘空间。',
    steps: ['自检会主动发出 DNS 查询和连接，每项最多几秒，整体不超过 10 秒。', '「PS5 网络指向」看的是最近的 DNS 记录和连接表：PS5 待机或刚开机时可能显示「注意」，开机用一会儿后再检查。', '最近一次结果会附在「导出日志」的诊断摘要里。', '服务器上也可以运行 sudo ./install.sh doctor 查看其中不依赖服务运行的几项。'] },
  notify: { title: '通知', body: '任务结束或出问题时推送消息：NAS 传输、解压安装完成或失败，服务更新结果，网关出错。支持 Bark、ntfy、Telegram 和通用 Webhook（POST JSON）。',
    steps: ['令牌、设备密钥、主题和 Webhook 地址加密保存，页面不会再显示；编辑时留空表示不修改。', '同一事件 1 分钟内只发一次；发送失败会重试两次，不影响任务本身。', '公网地址默认走服务代理（Telegram 常需要），局域网地址始终直连。', '保存后点「测试」确认能收到。'] },
  'nas-backup': { title: 'PS5 → NAS 备份', body: '把 PS5 上的文件夹（游戏、截图导出、存档目录等）复制到允许写入的 NAS 连接。',
    steps: ['先在 NAS 存储里编辑连接、勾选「允许写入」，用「测试连接」确认能建、删文件。', '「镜像」写进固定文件夹，NAS 上已有同名且大小相同的文件会跳过；「快照」每次新建「主机/日期时间」文件夹。', '每个文件先写成 .part，写完核对大小再改名；取消或失败不会留下半截文件，失败的任务可以「重试」补齐。', '存档是加密镜像，绑定这台主机和账号，备份只适合在同一台主机上恢复；本服务不提供恢复。截图、存档等系统目录需要在配置 [ps5] backup_roots 里列出后才会出现在来源里（只读）。'] },
  library: { title: '游戏库', body: '列出 PS5 各存储（/data、/mnt/ext*、/mnt/usb*）上的游戏文件夹：卷根目录下以游戏编号命名的文件夹，以及 [ps5] game_dirs（默认 games、homebrew、etaHEN/games）下的子文件夹。只看一层，不会递归扫描。',
    steps: ['名称来自内置游戏库或文件夹里的 param 文件；封面取 sce_sys/icon0.png。', '大小需要逐个点「计算大小」：FTP 模式下遍历很慢，结果按路径和修改时间缓存。', '「迁移」在内置存储和 M.2 / USB 硬盘之间搬运游戏，「卸载」删除游戏并清理图标和金手指，进度都在「传输任务」里。', '「注册图标」需要 ps5upload 引擎，只对以游戏编号命名的文件夹可用；卡片上的「已注册图标」也来自引擎。', '通过 PKG 安装的游戏不在文件白名单内，由 ps5upload 引擎列出（筛选「PKG 安装」），只能卸载。'] },
  migrate: { title: '迁移游戏', body: '把游戏文件夹从内置存储搬到 M.2 / USB 硬盘，或者反过来。数据在 PS5 上从一块盘复制到另一块，不经过本机，可以关闭页面。',
    steps: ['开始前检查：原文件夹在、目标没有同名文件夹、目标空间够（游戏大小再留 1 GB）、主机上没有别的复制任务。', '先复制到目标旁边的隐藏文件夹 .migrate-<名字>，复制完核对总大小和文件数，一致才改名放到位，ShadowMount 不会扫到半截游戏。', '放到位后才删除原文件夹；失败或取消时删掉半成品，原文件夹不动，可以点「重试」。', '「放到」默认保持原来的相对位置（/data/games/X → /mnt/ext0/games/X），也可以改放到存储根目录或其它游戏目录，注意要在 ShadowMount 的扫描目录里。', '勾选「重新注册」时，以游戏编号命名的文件夹会在新位置注册主屏幕图标（需要 ps5upload 引擎）。', '多个游戏按顺序一个一个迁移。PKG 安装的游戏请用主机的「设置 → 存储」移动。'] },
  uninstall: { title: '卸载游戏', body: '从主机上删除游戏，同时清理它留下的主屏幕图标和金手指文件；内置存储、M.2 和 USB 硬盘上的游戏都可以卸载。',
    steps: ['游戏文件夹：如果 ps5upload 引擎显示图标是从这个文件夹注册的，先移除图标（卸载挂载、删除 /user/appmeta 里的副本、清掉主机的应用记录），再删除整个文件夹。', '同一个游戏编号的图标属于另一份游戏或 PKG 安装时不会动它，任务里会提醒。', 'PKG 安装的游戏由主机自带的卸载程序删除，和「设置 → 存储 → 删除」一样。', '金手指：删除 /data/etaHEN/cheats 下文件名含这个游戏编号的文件。', '存档默认保留。勾选后会删除这个游戏在主机所有用户下的存档文件夹（需要引擎），无法恢复，需要的话先用「备份到 NAS」。', '移除图标失败（例如游戏正在运行）时什么都不删，退出游戏后重试。'] },
  backup: { title: '备份与恢复', body: '把访问策略、活动标签、文件主机、已知主机、NAS 连接和插件仓库打包成一个文件，重装系统或换机器后从备份恢复。',
    steps: ['NAS 账号在备份里仍是密文；不勾选「包含 NAS 密钥」时，换机恢复后需要重新填写 NAS 用户名和密码。', '勾选「包含 NAS 密钥」时整个备份用口令加密（.ps5bak），口令不保存，请自己记好。', '「包含插件文件」会把仓库里的 .elf / .bin 和历史版本一起打包，文件会大很多。', '恢复时访问策略立即应用、当前策略成为「上一版」；插件仓库只补上当前没有的插件。', '不包含管理密码、登录会话、证书和活动历史。'] },
  password: { title: '修改管理密码', body: '修改登录本管理页的密码，至少 10 个字符。修改后所有设备上的登录都会失效，需要用新密码重新登录。' },
  'service-info': { title: '服务信息', body: '本服务的端口、网段、数据目录和 systemd 服务名，排查问题或手动维护时使用。' },
  logs: { title: '运行日志', body: '管理服务的运行记录：警告与错误、主机 / 插件 / 文件操作、更新过程等。页面定时刷新这类成功的读取请求不会记录。',
    steps: ['按级别或关键字筛选；勾选「自动刷新」时每 5 秒追加新记录。', '「导出日志」下载文本文件，包含诊断摘要、服务日志和本浏览器最近的错误提示，反馈问题时附上即可。', '解压工作单元的详细日志在服务器上用 journalctl -u ps5-extract-<任务> 查看。'] },
  disable: { title: '停用与回滚', body: '需要停用本服务时，先把 PS5 的网关和 DNS 改回路由器，再按步骤停止服务。停止后网关规则保持默认拒绝，不会意外放开 PS5 的外网访问。' }
};

const help = { pop: null, anchor: null, showTimer: null, hideTimer: null, pinned: false };

function initHelpTips() {
  const pop = makeElement('div', 'help-pop');
  pop.id = 'help-pop';
  pop.setAttribute('role', 'tooltip');
  pop.hidden = true;
  document.body.append(pop);
  help.pop = pop;
  for (const tip of $$('.help-tip[data-help]')) {
    const entry = HELP[tip.dataset.help];
    if (!entry) { tip.remove(); continue; }
    tip.setAttribute('aria-label', `功能说明：${entry.title}`);
    tip.setAttribute('aria-expanded', 'false');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-help');
    svg.append(use);
    tip.replaceChildren(svg);
  }
  const tipOf = (event) => (event.target instanceof Element ? event.target.closest('.help-tip[data-help]') : null);
  document.addEventListener('mouseover', (event) => {
    const tip = tipOf(event);
    if (tip) {
      clearTimeout(help.hideTimer);
      if (help.anchor === tip) return;
      clearTimeout(help.showTimer);
      help.showTimer = setTimeout(() => showHelp(tip, false), help.anchor ? 0 : 140);
    } else if (event.target instanceof Element && event.target.closest('#help-pop')) {
      clearTimeout(help.hideTimer);
    }
  });
  document.addEventListener('mouseout', (event) => {
    const from = event.target instanceof Element ? event.target.closest('.help-tip[data-help], #help-pop') : null;
    const to = event.relatedTarget instanceof Element ? event.relatedTarget.closest('.help-tip[data-help], #help-pop') : null;
    if (!from || to) return;
    clearTimeout(help.showTimer);
    if (!help.pinned) help.hideTimer = setTimeout(hideHelp, 180);
  });
  document.addEventListener('focusin', (event) => {
    const tip = tipOf(event);
    if (tip && tip.matches(':focus-visible')) showHelp(tip, false);
  });
  document.addEventListener('focusout', (event) => {
    if (tipOf(event) && !help.pinned) hideHelp();
  });
  // 点按（触屏）固定显示；在 <summary> 里时不让它展开 / 收起卡片。
  document.addEventListener('click', (event) => {
    const tip = tipOf(event);
    if (tip) {
      event.preventDefault();
      event.stopPropagation();
      if (help.anchor === tip && help.pinned) hideHelp();
      else showHelp(tip, true);
      return;
    }
    if (help.anchor && !(event.target instanceof Element && event.target.closest('#help-pop'))) hideHelp();
  }, true);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && help.anchor) {
      const anchor = help.anchor;
      hideHelp();
      if (anchor.isConnected) anchor.focus({ preventScroll: true });
    }
  });
  window.addEventListener('resize', () => { if (help.anchor) placeHelp(); });
  window.addEventListener('scroll', () => { if (help.anchor) placeHelp(); }, true);
}

function showHelp(tip, pinned) {
  const entry = HELP[tip.dataset.help];
  if (!entry || !help.pop) return;
  clearTimeout(help.hideTimer);
  if (help.anchor && help.anchor !== tip) help.anchor.setAttribute('aria-expanded', 'false');
  help.anchor = tip;
  help.pinned = pinned;
  const nodes = [makeElement('strong', 'help-pop-title', entry.title), makeElement('p', 'help-pop-body', entry.body)];
  if (entry.steps && entry.steps.length) {
    const list = makeElement('ul', 'help-pop-steps');
    for (const step of entry.steps) list.append(makeElement('li', '', step));
    nodes.push(list);
  }
  help.pop.replaceChildren(...nodes);
  help.pop.hidden = false;
  tip.setAttribute('aria-expanded', 'true');
  tip.setAttribute('aria-describedby', 'help-pop');
  placeHelp();
}

function placeHelp() {
  const tip = help.anchor;
  const pop = help.pop;
  if (!tip || !pop || !tip.isConnected || !tip.getClientRects().length) { hideHelp(); return; }
  const rect = tip.getBoundingClientRect();
  const gap = 8;
  const margin = 12;
  const width = Math.min(340, window.innerWidth - margin * 2);
  pop.style.width = `${width}px`;
  const height = pop.offsetHeight;
  const below = rect.bottom + gap + height <= window.innerHeight - margin || rect.top - gap - height < margin;
  const top = below ? rect.bottom + gap : rect.top - gap - height;
  const left = Math.min(Math.max(margin, rect.left + rect.width / 2 - width / 2), window.innerWidth - width - margin);
  pop.dataset.side = below ? 'below' : 'above';
  pop.style.top = `${Math.max(margin, top)}px`;
  pop.style.left = `${left}px`;
}

function hideHelp() {
  clearTimeout(help.showTimer);
  clearTimeout(help.hideTimer);
  if (help.anchor) {
    help.anchor.setAttribute('aria-expanded', 'false');
    help.anchor.removeAttribute('aria-describedby');
  }
  help.anchor = null;
  help.pinned = false;
  if (help.pop) help.pop.hidden = true;
}
