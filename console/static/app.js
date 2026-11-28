/* wxbot 控制台前端:无构建步骤的单页应用(hash 路由)。
 * 每个微信的接口都经控制台代理:/a/<账号id>/api/...  控制台自己的接口:/api/console/... */
'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------- 请求 ----------------
async function api(url, opt = {}) {
  const o = Object.assign({credentials: 'same-origin', cache: 'no-store'}, opt);
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers = Object.assign({'content-type': 'application/json'}, o.headers || {}); delete o.json; }
  const r = await fetch(url, o);
  if (r.status === 401 && url.startsWith('/api/console')) { location.replace('/ui/login.html'); throw new Error('需要登录'); }
  let d = null; try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok || (d && d.ok === false)) throw new Error((d && (d.error || d.message)) || `请求失败(${r.status})`);
  return d;
}
const A = (id, path, opt) => api(`/a/${id}${path}`, opt);

// ---------------- 提示 / 弹窗 ----------------
function toast(msg, kind = '') {
  const t = document.createElement('div'); t.className = 'toast ' + kind; t.textContent = msg;
  $('#toasts').appendChild(t); setTimeout(() => t.remove(), kind === 'err' ? 6000 : 3200);
}
function modal({title, body = '', ok = '确定', cancel = '取消', danger = false, extra = ''}) {
  return new Promise((resolve) => {
    const bg = document.createElement('div'); bg.className = 'modal-bg';
    bg.innerHTML = `<div class="modal"><div class="mh"><h2>${esc(title)}</h2></div><div class="mb">${body}${extra}</div>
      <div class="mf">${cancel ? `<button class="btn" data-r="0">${esc(cancel)}</button>` : ''}<button class="btn ${danger ? 'danger' : 'primary'}" data-r="1">${esc(ok)}</button></div></div>`;
    bg.addEventListener('click', (e) => {
      const b = e.target.closest('[data-r]'); if (!b && e.target !== bg) return;
      const val = b ? b.dataset.r === '1' : false;
      const form = {}; $$('input,select', bg).forEach((i) => { form[i.name || i.id] = i.type === 'checkbox' ? i.checked : i.value; });
      bg.remove(); resolve(val ? form : null);
    });
    document.body.appendChild(bg); const f = $('input', bg); if (f) f.focus();
  });
}
async function busy(btn, fn) {
  const html = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${btn.textContent}`;
  try { return await fn(); } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; btn.innerHTML = html; }
}
function lightbox(src) {
  const d = document.createElement('div'); d.className = 'lightbox'; d.innerHTML = `<img src="${esc(src)}">`;
  d.onclick = () => d.remove(); document.body.appendChild(d);
}

// ---------------- 账号状态 ----------------
const STATE = {
  online:       {t: '在线', c: 'green', hint: '微信已登录，密钥齐全，一切正常'},
  need_enter:   {t: '待进入', c: 'amber', pulse: true, hint: '微信停在「进入微信」页（容器或微信刚重启）。控制台会自动点，十几秒没好就手动点「进入微信」，不用扫码'},
  need_login:   {t: '待扫码', c: 'amber', hint: '微信在运行但没登录：打开「扫码与桌面」，用手机扫码（或点「进入微信」）'},
  keys_missing: {t: '缺密钥', c: 'amber', hint: '已登录但还读不了消息：点「刷新密钥」（约一分钟）'},
  wechat_down:  {t: '微信未运行', c: 'red', hint: '容器在跑但微信进程没起来：点「重启微信」'},
  starting:     {t: '启动中', c: 'amber', pulse: true, hint: '容器刚启动，后台还没就绪，稍等几十秒'},
  stopped:      {t: '已停止', c: 'gray', hint: '容器已停止，点「启动」'},
  missing:      {t: '容器不存在', c: 'red', hint: '容器被删除了；可以把它从控制台移除'},
  error:        {t: '出错', c: 'red', hint: '读不到容器状态'},
  unknown:      {t: '未知', c: 'gray', hint: ''},
};
const st = (s) => STATE[s.state] || STATE.unknown;
// 显示名:有备注用备注,没有就用微信昵称
const nm = (s) => s.label || s.name || s.container || s.id;
async function editNote(s) {
  const f = await modal({title: '备注', body: `<p class="muted small">只在控制台里显示，方便区分。留空就显示微信昵称${s.name ? `「${esc(s.name)}」` : ''}。</p>`,
    extra: `<input class="input" name="label" value="${esc(s.label || '')}" maxlength="30" placeholder="${esc(s.name || '例如：客服号、小号')}">`, ok: '保存'});
  if (!f) return;
  await api(`/api/console/accounts/${s.id}`, {method: 'PATCH', json: {label: f.label.trim()}});
  toast('已保存'); await loadAccounts(true);
}
const badge = (s) => `<span class="badge ${st(s).c}"><span class="dot ${st(s).c} ${st(s).pulse ? 'pulse' : ''}"></span>${st(s).t}</span>`;
function avatar(s, cls = '') {
  const letter = esc((s.name || s.label || '?').trim().slice(0, 1));
  if (s.wxid && s.backend) return `<img class="avatar ${cls}" src="/a/${s.id}/api/avatar?username=${encodeURIComponent(s.wxid)}" onerror="this.outerHTML='<div class=&quot;avatar ${cls}&quot;>${letter}</div>'">`;
  return `<div class="avatar ${cls}">${letter}</div>`;
}

let accounts = [];
let pollTimer = null;
async function loadAccounts(fresh = false) {
  try {
    accounts = (await api('/api/console/accounts' + (fresh ? '?fresh=1' : ''))).accounts;
    renderSide();
    window.dispatchEvent(new Event('accounts'));
  } catch (e) { /* 401 已跳转;网络抖动下次再试 */ }
}
const acct = (id) => accounts.find((a) => a.id === id);

// ---------------- 侧边栏 ----------------
const MENU = [
  [null, 'overview', '概览', '◉'],
  [null, 'chats', '消息', '💬'],
  [null, 'desktop', '扫码与桌面', '🖥'],
  ['机器人', 'bot', '机器人与规则', '🤖'],
  ['机器人', 'schedule', '定时任务', '⏰'],
  ['机器人', 'ai', 'AI 模型与知识库', '🧠'],
  ['机器人', 'persona', '人设与画像', '🎭'],
  ['机器人', 'battle', '战斗模式', '⚔️'],
  ['发送', 'send', '发送设置', '⚡'],
  ['发送', 'direct', '直发测试', '✉️'],
  ['发送', 'api', '发送接口', '⌘'],
  ['语音', 'voices', '音色与复刻', '🎙'],
  ['语音', 'voicereply', '语音回复', '🔊'],
  ['内容', 'moments', '朋友圈', '🖼'],
  ['设置', 'settings', '配置导入导出', '⚙️'],
];
// 还没重做成新页面时,回退到旧版页面(经代理嵌入)
const LEGACY = {
  bot: ['/', '机器人与规则'], schedule: ['/', '定时任务'], ai: ['/', 'AI 模型与知识库'], settings: ['/', '配置导入导出'],
  persona: ['/personalization', '人设与画像'], battle: ['/battle', '战斗模式'],
  direct: ['/direct-send', '直发测试'], voices: ['/direct-send', '音色与复刻'], voicereply: ['/direct-send', '语音回复'],
  api: ['/send-curl', '发送接口'], moments: ['/moments', '朋友圈'],
};
// 页面模块(pages/*.js)注册到这里:WXPAGES[菜单 key] = async (view, 账号id) => {...}
window.WXPAGES = window.WXPAGES || {};
function renderSide() {
  const r = route();
  $('#navHome').classList.toggle('active', r.page === 'home');
  $('#acctList').innerHTML = accounts.length ? accounts.map((s) => {
    const on = r.id === s.id;
    let h = `<a class="acct ${on ? 'active' : ''}" href="#/a/${s.id}">
      <div class="avatar-wrap">${avatar(s, 'sm')}<span class="dot ${st(s).c}"></span></div>
      <div class="grow"><div class="name ellipsis">${esc(nm(s))}</div><div class="sub ellipsis">${esc(s.label && s.name ? s.name : st(s).t)}</div></div></a>`;
    if (on) {
      let group = undefined;
      h += '<div class="nav">';
      for (const [g, key, label, ico] of MENU) {
        if (g !== group) { if (g) h += `<div class="nav-group">${g}</div>`; group = g; }
        h += `<a href="#/a/${s.id}/${key === 'overview' ? '' : key}" class="${(r.sub || 'overview') === key ? 'active' : ''}"><span class="ico">${ico}</span>${label}</a>`;
      }
      h += '</div>';
    }
    return h;
  }).join('') : '<div class="faint small" style="padding:6px 8px">还没有微信，点「＋ 添加」</div>';
}

// ---------------- 路由 ----------------
function route() {
  const p = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  if (!p.length) return {page: 'home'};
  if (p[0] === 'add') return {page: 'add'};
  if (p[0] === 'a') return {page: 'acct', id: p[1], sub: p[2] || 'overview'};
  return {page: 'home'};
}
let cleanup = [];
function onLeave(fn) { cleanup.push(fn); }
function setTop(title, actions = '') { $('#topTitle').innerHTML = title; $('#topActions').innerHTML = actions; }
async function render() {
  cleanup.forEach((f) => { try { f(); } catch (e) { /**/ } }); cleanup = [];
  $$('.modal-bg, .lightbox').forEach((el) => el.remove());     // 换页时关掉上一页没关的弹窗
  $('#app').classList.remove('side-open');
  const r = route(); renderSide();
  const view = $('#view'); view.className = 'content'; view.innerHTML = '';
  if (r.page === 'home') return pageHome(view);
  if (r.page === 'add') return pageAdd(view);
  if (!acct(r.id)) { if (!accounts.length) await loadAccounts(); if (!acct(r.id)) { location.hash = '#/'; return; } }
  const fn = {overview: pageAccount, chats: pageChats, desktop: pageDesktop, send: pageSend}[r.sub] || window.WXPAGES[r.sub];
  if (fn) return fn(view, r.id);
  if (LEGACY[r.sub]) return pageLegacy(view, r.id, r.sub);
  location.hash = `#/a/${r.id}`;
}
window.addEventListener('hashchange', render);

// ---------------- 总览 ----------------
function pageHome(view) {
  setTop('<h1>总览</h1>', `<button class="btn" id="scanBtn">扫描已有容器</button><a class="btn primary" href="#/add">＋ 添加微信</a>`);
  $('#scanBtn').onclick = (e) => busy(e.currentTarget, async () => {
    const d = await api('/api/console/scan', {method: 'POST'}); toast(d.added ? `纳入了 ${d.added} 个微信` : '没有发现新的微信容器'); await loadAccounts(true);
  });
  const sel = new Set();
  const draw = () => {
    const n = (k) => accounts.filter((a) => k.includes(a.state)).length;
    for (const id of [...sel]) if (!accounts.some((a) => a.id === id)) sel.delete(id);
    const all = accounts.length && sel.size === accounts.length;
    view.innerHTML = `<div class="page">
      <div class="grid cols-4" style="margin-bottom:18px">
        ${[['微信总数', accounts.length, ''], ['在线', n(['online']), 'var(--brand-d)'], ['需要处理', n(['need_enter', 'need_login', 'keys_missing', 'wechat_down', 'missing', 'error']), '#a5700c'], ['已停止', n(['stopped']), 'var(--text-3)']]
          .map(([l, v, c]) => `<div class="card stat"><div class="n" style="color:${c || 'inherit'}">${v}</div><div class="l">${l}</div></div>`).join('')}
      </div>
      ${accounts.length ? `<div class="card bulk-bar">
        <label class="row" style="gap:8px;cursor:pointer"><input type="checkbox" id="selAll" ${all ? 'checked' : ''}> 全选</label>
        <span class="muted small grow">${sel.size ? `已选 ${sel.size} 个微信` : '勾选卡片左上角可以批量启停'}</span>
        <button class="btn sm primary" data-bulk="start" ${sel.size ? '' : 'disabled'}>启动</button>
        <button class="btn sm" data-bulk="restart" ${sel.size ? '' : 'disabled'}>重启</button>
        <button class="btn sm danger" data-bulk="stop" ${sel.size ? '' : 'disabled'}>停止</button>
      </div>` : ''}
      <div class="grid cols-3">${accounts.map((a) => cardHtml(a, sel.has(a.id))).join('')}
        <a class="card acct-card add" href="#/add"><div class="plus">＋</div><div style="font-weight:600">添加微信</div><div class="small faint">新建一个容器，扫码登录</div></a>
      </div></div>`;
    bindCardActions(view);
    $$('[data-note]', view).forEach((b) => b.onclick = () => editNote(acct(b.dataset.note)).catch((e) => toast(e.message, 'err')));
    $$('[data-sel]', view).forEach((c) => c.onchange = () => { c.checked ? sel.add(c.dataset.sel) : sel.delete(c.dataset.sel); draw(); });
    const sa = $('#selAll', view);
    if (sa) sa.onchange = () => { sel.clear(); if (sa.checked) accounts.forEach((a) => sel.add(a.id)); draw(); };
    $$('[data-bulk]', view).forEach((b) => b.onclick = () => bulk(b, b.dataset.bulk));
  };
  const OPN = {start: '启动', stop: '停止', restart: '重启'};
  async function bulk(btn, op) {
    const picked = accounts.filter((a) => sel.has(a.id));
    const todo = picked.filter((a) => op === 'start' ? !isUp(a) : op === 'stop' ? isUp(a) : a.state !== 'missing');
    if (!todo.length) return toast(op === 'start' ? '选中的都已经在运行' : op === 'stop' ? '选中的都已经停止' : '没有可重启的', 'err');
    if (op !== 'start' && !await modal({title: `${OPN[op]} ${todo.length} 个微信？`, danger: op === 'stop', ok: OPN[op],
      body: `<p>${todo.map((a) => esc(nm(a))).join('、')}</p><p class="muted small">${op === 'stop'
        ? '会停掉整个容器：微信下线、机器人不回复、定时任务不执行。登录状态保留：启动后微信停在「进入微信」页，控制台会自动点进去，一般不用重新扫码。'
        : '容器重启约需 1 分钟，期间这些微信收不到消息。'}</p>`})) return;
    await busy(btn, async () => {
      const r = await Promise.allSettled(todo.map((a) => api(`/api/console/accounts/${a.id}/action`, {method: 'POST', json: {op}})));
      const bad = r.map((x, i) => x.status === 'rejected' ? `${nm(todo[i])}：${x.reason.message}` : '').filter(Boolean);
      bad.length ? toast(`${todo.length - bad.length} 个成功；失败 ${bad.join('；')}`, 'err') : toast(`已${OPN[op]} ${todo.length} 个`);
      sel.clear(); await sleep(1500); await loadAccounts(true);
    });
  }
  draw(); window.addEventListener('accounts', draw); onLeave(() => window.removeEventListener('accounts', draw));
}
const isUp = (s) => !['stopped', 'missing'].includes(s.state);
function cardHtml(s, checked = false) {
  const chips = [];
  if (s.state === 'online' || s.state === 'keys_missing') {
    chips.push(`<span class="badge ${s.bot_running ? 'green' : ''}">机器人 ${s.bot_running ? '运行中' : '已停止'}</span>`);
    chips.push(`<span class="badge ${s.send_mode === 'direct' ? 'blue' : ''}">${s.send_mode === 'direct' ? '直发' : '界面发送'}</span>`);
    if (s.direct && s.direct.version) chips.push(`<span class="badge">微信 ${esc(s.direct.version.split(' ')[0])}</span>`);
  }
  let act = '';
  if (s.state === 'stopped') act = `<button class="btn sm primary" data-op="start" data-id="${s.id}">启动</button>`;
  else if (s.state === 'need_login') act = `<a class="btn sm primary" href="#/a/${s.id}/desktop">去扫码</a>`;
  else if (s.state === 'need_enter') act = `<button class="btn sm primary" data-op="enter_wechat" data-id="${s.id}">进入微信</button>`;
  else if (s.state === 'wechat_down') act = `<button class="btn sm primary" data-op="restart_wechat" data-id="${s.id}">重启微信</button>`;
  else if (s.state === 'keys_missing') act = `<a class="btn sm primary" href="#/a/${s.id}">刷新密钥</a>`;
  const power = s.state === 'missing' ? '' : isUp(s)
    ? `<button class="btn sm ghost" data-op="stop" data-id="${s.id}" data-label="${esc(nm(s))}" title="停止这个微信的容器">停止</button>`
    : (s.state === 'stopped' ? '' : `<button class="btn sm primary" data-op="start" data-id="${s.id}">启动</button>`);
  return `<div class="card acct-card${checked ? ' picked' : ''}">
    <div class="top"><input type="checkbox" class="acct-sel" data-sel="${s.id}" ${checked ? 'checked' : ''} title="选中后可批量启停">${avatar(s, 'lg')}<div class="grow"><div class="row" style="gap:4px"><h2 class="ellipsis">${esc(nm(s))}</h2><button class="note-btn" data-note="${s.id}" title="${s.label ? '改备注' : '加备注'}">✎</button></div>
      <div class="muted small ellipsis">${s.label && s.name ? esc(s.name) + ' · ' : ''}${esc(s.wxid || '未登录')}</div></div>${badge(s)}</div>
    <div class="meta"><div class="small muted" style="margin-bottom:8px">${esc(st(s).hint)}</div><div class="chips">${chips.join('')}</div></div>
    <div class="actions"><a class="btn sm" href="#/a/${s.id}">进入</a><a class="btn sm" href="#/a/${s.id}/chats">消息</a>${act}<span class="grow"></span>${power}</div></div>`;
}
function bindCardActions(root) {
  $$('[data-op]', root).forEach((b) => b.onclick = async () => {
    if (b.dataset.op === 'stop' && !await modal({title: `停止「${b.dataset.label}」？`, danger: true, ok: '停止',
      body: '<p class="muted small">会停掉整个容器：微信下线、机器人不回复、定时任务不执行。登录状态保留：启动后微信停在「进入微信」页，控制台会自动点进去，一般不用重新扫码。</p>'})) return;
    return busy(b, async () => {
    await api(`/api/console/accounts/${b.dataset.id}/action`, {method: 'POST', json: {op: b.dataset.op}});
    toast('已执行'); await sleep(1500); loadAccounts(true);
  });
  });
}

// ---------------- 添加微信 ----------------
function pageAdd(view) {
  setTop('<h1>添加微信</h1>');
  let step = 1, id = null, timer = null;
  onLeave(() => clearInterval(timer));
  const steps = () => `<div class="steps">${['创建', '扫码登录', '读取密钥', '完成'].map((t, i) =>
    `<div class="step ${i + 1 === step ? 'on' : i + 1 < step ? 'done' : ''}"><span class="n">${i + 1 < step ? '✓' : i + 1}</span>${t}</div>`).join('')}</div>`;
  const draw = (inner) => { view.innerHTML = `<div class="page" style="max-width:880px">${steps()}${inner}</div>`; };
  draw(`<div class="card card-body stack">
      <h2>新建一个微信</h2>
      <div class="muted">会新建一个独立的容器（独立的微信、数据和机器人），创建后在下一步用手机扫码登录。</div>
      <div class="field"><label>备注（可不填，登录后默认显示微信昵称）</label><input class="input" id="label" placeholder="例如：客服号、小号 3" maxlength="30"></div>
      <div class="row"><button class="btn primary" id="go">创建</button><span class="faint small">第一次创建要拉镜像，可能要一会儿</span></div></div>`);
  $('#label').focus();
  $('#go').onclick = (e) => busy(e.currentTarget, async () => {
    const label = $('#label').value.trim();
    const d = await api('/api/console/accounts', {method: 'POST', json: {label}});
    id = d.id; await loadAccounts(true); step = 2; waitLogin();
  });
  function waitLogin() {
    draw(`<div class="card"><div class="card-head"><h2>用手机微信扫码登录</h2><span id="ws" class="badge amber"><span class="dot amber pulse"></span>等待容器启动…</span></div>
      <div class="vnc-wrap" style="height:520px;border-radius:0 0 12px 12px;overflow:hidden" id="vnc"><div class="empty" style="color:#aab">容器启动中，大约 20～60 秒…</div></div></div>`);
    let shown = false;
    timer = setInterval(async () => {
      await loadAccounts(true); const s = acct(id); if (!s) return;
      if (s.backend && !shown) { shown = true; $('#vnc').innerHTML = await vncIframe(id); }
      $('#ws').outerHTML = `<span id="ws">${badge(s)}</span>`;
      if (s.logged_in) { clearInterval(timer); step = 3; readKeys(); }
    }, 3000);
  }
  async function readKeys() {
    draw(`<div class="card card-body stack"><div class="row"><span class="spinner"></span><h2>登录成功，正在读取数据库密钥</h2></div>
      <div class="muted">会往这个微信自己的「文件传输助手」发一条文字和一条语音，触发微信读库，约一分钟。这些消息可以删掉。</div>
      <pre class="mono" id="klog" style="white-space:pre-wrap;background:var(--gray-50);padding:12px;border-radius:8px;min-height:60px"></pre></div>`);
    try {
      await sleep(8000);
      const d = await A(id, '/api/keys/refresh', {method: 'POST'});
      $('#klog').textContent = d.log || '';
    } catch (e) { $('#klog').textContent = '读取密钥失败：' + e.message + '\n可以稍后在账号概览里点「刷新密钥」重试。'; }
    step = 4;
    draw(`<div class="card card-body stack" style="align-items:flex-start"><h2>✓ 添加完成</h2>
      <div class="muted">机器人默认已启动。接下来可以在「机器人与规则」「人设与画像」里配置。</div>
      <a class="btn primary" href="#/a/${id}">打开这个微信</a></div>`);
    loadAccounts(true);
  }
}

// ---------------- 账号概览 ----------------
function pageAccount(view, id) {
  const draw = () => {
    const s = acct(id); if (!s) return;
    setTop(`<div class="row">${avatar(s, 'sm')}<h1 class="ellipsis">${esc(nm(s))}</h1>${badge(s)}</div>`, '<button class="btn" id="noteTop">✎ 备注</button>');
    $('#noteTop').onclick = () => editNote(s).catch((e) => toast(e.message, 'err'));
    const d = s.direct || {};
    const caps = ['revoke:撤回', 'voice:语音条', 'quote:引用', 'at:@提醒'].map((x) => { const [k, l] = x.split(':'); return `<span class="badge ${d[k] ? 'green' : ''}">${d[k] ? '✓' : '–'} ${l}</span>`; }).join('');
    const tip = st(s).hint ? `<div class="callout ${s.state === 'online' ? 'green' : st(s).c === 'red' ? 'red' : 'amber'}">${esc(st(s).hint)}</div>` : '';
    view.innerHTML = `<div class="page stack" style="gap:16px">
      <div class="card"><div class="card-body row gap-16" style="align-items:center">
        ${avatar(s, 'lg')}<div class="grow"><h2>${esc(s.name || nm(s))}</h2><div class="muted small">${s.label ? '备注「' + esc(s.label) + '」 · ' : ''}${esc(s.wxid || '未登录')} · 容器 ${esc(s.container)}</div></div>
        <div class="row wrap">${stateActions(s)}</div></div>${tip ? `<div style="padding:0 20px 18px">${tip}</div>` : ''}</div>
      <div class="grid cols-2">
        <div class="card"><div class="card-head"><h2>微信与密钥</h2></div><div class="card-body">
          <dl class="kv"><dt>微信进程</dt><dd>${s.wechat_running ? '运行中' : '未运行'}</dd>
          <dt>登录</dt><dd>${s.logged_in ? '已登录' : '未登录'}</dd>
          <dt>数据库密钥</dt><dd>${s.keys_ready ? '<span class="badge green">齐全</span>' : '<span class="badge amber">缺</span>'}</dd>
          <dt>图片密钥</dt><dd>${s.img_ready ? '<span class="badge green">有</span>' : '<span class="badge amber">没有（看不了收到的图）</span>'}</dd></dl>
          <div class="row wrap" style="margin-top:14px"><button class="btn sm" id="keyBtn">刷新密钥</button><button class="btn sm" id="imgKeyBtn">抓图片密钥</button></div></div></div>
        <div class="card"><div class="card-head"><h2>机器人</h2><label class="switch"><input type="checkbox" id="botSw" ${s.bot_running ? 'checked' : ''}><span></span></label></div>
          <div class="card-body"><div class="muted small" style="margin-bottom:12px">${s.bot_running ? '正在监听新消息并按规则/人设自动回复' : '已停止：不会自动回复'}</div>
          <div class="row wrap"><a class="btn sm" href="#/a/${id}/bot">机器人与规则</a><a class="btn sm" href="#/a/${id}/persona">人设与画像</a></div></div></div>
        <div class="card"><div class="card-head"><h2>发送</h2><span class="badge ${s.send_mode === 'direct' ? 'blue' : ''}">${s.send_mode === 'direct' ? '直发' : '界面发送'}</span></div>
          <div class="card-body"><dl class="kv"><dt>微信版本</dt><dd>${esc(d.version || '未适配')}</dd><dt>直发</dt><dd>${d.supported ? (d.attached ? '已连接' : '支持，未连接') : '这个版本/架构不支持'}</dd></dl>
          <div class="chips" style="margin:12px 0">${caps}</div><a class="btn sm" href="#/a/${id}/send">发送设置</a></div></div>
        <div class="card"><div class="card-head"><h2>容器</h2><span class="badge">${esc(s.container_state || '-')}</span></div>
          <div class="card-body"><dl class="kv"><dt>容器名</dt><dd class="mono">${esc(s.container)}</dd><dt>镜像</dt><dd class="mono ellipsis" title="${esc(s.image || '')}">${esc(s.image || '-')}</dd>
          <dt>启动于</dt><dd>${s.started_at ? new Date(s.started_at).toLocaleString('zh-CN', {hour12: false}) : '-'}</dd><dt>来源</dt><dd>${s.managed ? '控制台创建' : '已有容器（纳入管理）'}</dd></dl>
          <div class="row wrap" style="margin-top:14px">
            <button class="btn sm" data-op="restart_wechat">重启微信</button><button class="btn sm" data-op="restart">重启容器</button>
            ${s.container_state === 'running' ? '<button class="btn sm" data-op="stop">停止</button>' : '<button class="btn sm primary" data-op="start">启动</button>'}
            <span class="grow"></span><button class="btn sm ghost" id="renameBtn">备注</button><button class="btn sm danger" id="rmBtn">移除</button></div></div></div>
      </div></div>`;
    bind(s);
  };
  const bind = (s) => {
    $$('[data-op]', view).forEach((b) => b.onclick = async () => {
      const op = b.dataset.op;
      if ((op === 'stop' || op === 'restart' || op === 'restart_wechat') && !await modal({title: b.textContent + '？', body: op === 'restart_wechat' ? '微信会退出再启动，之后要点一次「进入微信」（或扫码）。机器人中断一两分钟。' : '这个微信的机器人会中断。', ok: b.textContent})) return;
      busy(b, async () => { await api(`/api/console/accounts/${id}/action`, {method: 'POST', json: {op}}); toast('已执行'); await sleep(2000); loadAccounts(true); });
    });
    $('#keyBtn').onclick = (e) => busy(e.currentTarget, async () => {
      toast('正在读取密钥，约一分钟…'); const d = await A(id, '/api/keys/refresh', {method: 'POST'});
      await modal({title: d.ok ? '密钥已刷新' : '还有库缺密钥', body: `<pre class="mono" style="white-space:pre-wrap">${esc(d.log || '')}</pre>`, cancel: ''}); loadAccounts(true);
    });
    $('#imgKeyBtn').onclick = (e) => busy(e.currentTarget, async () => {
      toast('会自动点开几个会话让微信显示图片，约半分钟…');
      const d = await A(id, '/api/keys/capture_img', {method: 'POST', json: {force: true}}); toast(d.ok ? '图片密钥已保存' : '没抓到：' + d.key, d.ok ? 'ok' : 'err'); loadAccounts(true);
    });
    $('#botSw').onchange = async (e) => {
      try { await A(id, e.target.checked ? '/api/bot/start' : '/api/bot/stop', {method: 'POST'}); toast(e.target.checked ? '机器人已启动' : '机器人已停止'); loadAccounts(true); }
      catch (x) { toast(x.message, 'err'); e.target.checked = !e.target.checked; }
    };
    $('#renameBtn').onclick = () => editNote(s).catch((e) => toast(e.message, 'err'));
    $('#rmBtn').onclick = async () => {
      const f = await modal({title: `移除「${nm(s)}」？`, danger: true, ok: '移除',
        body: s.managed ? '会删除这个微信的容器。' : '只从控制台列表移除，不会动这个容器（之后可以「扫描已有容器」再纳入）。',
        extra: s.managed ? `<label class="row" style="margin-top:12px"><input type="checkbox" name="purge"> 同时删除数据卷（登录状态、聊天记录、密钥全部清空，不能恢复）</label>` : ''});
      if (!f) return;
      await api(`/api/console/accounts/${id}${f.purge ? '?purge=1' : ''}`, {method: 'DELETE'}); toast('已移除'); await loadAccounts(true); location.hash = '#/';
    };
  };
  draw(); window.addEventListener('accounts', draw); onLeave(() => window.removeEventListener('accounts', draw));
}
function stateActions(s) {
  if (s.state === 'stopped') return `<button class="btn primary" data-op="start">启动</button>`;
  if (s.state === 'need_login') return `<a class="btn primary" href="#/a/${s.id}/desktop">去扫码</a><button class="btn" data-op="enter_wechat">进入微信</button>`;
  if (s.state === 'need_enter') return `<button class="btn primary" data-op="enter_wechat">进入微信</button><a class="btn" href="#/a/${s.id}/desktop">看桌面</a>`;
  if (s.state === 'wechat_down') return `<button class="btn primary" data-op="restart_wechat">重启微信</button>`;
  return `<a class="btn" href="#/a/${s.id}/chats">打开消息</a><a class="btn" href="#/a/${s.id}/desktop">桌面</a>`;
}

// ---------------- 扫码与桌面 ----------------
let vncPw = null;
async function vncIframe(id) {
  if (vncPw === null) { try { vncPw = (await api('/api/console/vnc')).password || ''; } catch (e) { vncPw = ''; } }
  const q = new URLSearchParams({autoconnect: '1', resize: 'scale', reconnect: '1', reconnect_delay: '2000', path: `a/${id}/websockify`});
  if (vncPw) q.set('password', vncPw);
  return `<iframe src="/novnc/vnc.html?${q}" allow="clipboard-read; clipboard-write"></iframe>`;
}
async function pageDesktop(view, id) {
  const s = acct(id);
  setTop(`<div class="row">${avatar(s, 'sm')}<h1>扫码与桌面</h1>${badge(s)}</div>`,
    `<button class="btn" data-op="enter_wechat">进入微信</button><button class="btn" data-op="restart_wechat">重启微信</button>`);
  view.className = 'content flush';
  view.innerHTML = `<div class="callout blue" style="border-radius:0">这是这个微信所在容器的桌面。没登录时会显示二维码，用手机微信扫码；重启微信后点「进入微信」。</div><div class="vnc-wrap" id="vnc"></div>`;
  $('#vnc').innerHTML = await vncIframe(id);
  $$('#topActions [data-op]').forEach((b) => b.onclick = () => busy(b, async () => {
    await api(`/api/console/accounts/${id}/action`, {method: 'POST', json: {op: b.dataset.op}}); toast('已执行'); loadAccounts(true);
  }));
}

// ---------------- 发送设置 ----------------
async function pageSend(view, id) {
  const s = acct(id);
  setTop(`<div class="row">${avatar(s, 'sm')}<h1>发送设置</h1></div>`);
  view.innerHTML = '<div class="page"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>';
  let mode, ds;
  try { [mode, ds] = await Promise.all([A(id, '/api/send_mode'), A(id, '/api/direct/status').catch(() => ({}))]); }
  catch (e) { view.innerHTML = `<div class="page"><div class="callout red">${esc(e.message)}</div></div>`; return; }
  const can = !!ds.supported;
  const sw = (k, t, d) => `<div class="setting"><div class="grow"><div class="t">${t}</div><div class="d">${d}</div></div><label class="switch"><input type="checkbox" data-k="${k}" ${mode[k] ? 'checked' : ''}><span></span></label></div>`;
  view.innerHTML = `<div class="page stack" style="max-width:820px;gap:16px">
    <div class="card"><div class="card-head"><h2>发送方式</h2>${can ? '<span class="badge green">本机微信支持直发</span>' : '<span class="badge amber">本机微信不支持直发</span>'}</div><div class="card-body stack">
      <div class="seg" id="modeSeg"><button data-m="ui">界面自动化</button><button data-m="direct">直发</button></div>
      <div class="muted small">机器人回复、定时消息、发送接口都按这里走，改了立即生效。<b>直发</b>在微信进程里直接调用发送函数：一条约 0.1 秒，不抢桌面，还能撤回、真 @、引用回复、发语音条（arm64 + 微信 4.1.13）。<b>界面自动化</b>模拟鼠标键盘，任何版本都能用，较慢。</div>
      ${sw('fallback', '直发失败时改用界面发送', '只在「确定没发出」（没连上、没登录、版本不支持）时回退；结果不确定时不回退，避免重复发送')}
    </div></div>
    <div class="card"><div class="card-head"><h2>群聊与画图</h2><span class="faint small">需要直发 + 微信 4.1.13，做不到时照常发文字</span></div><div class="card-body">
      ${sw('group_quote', '群里回复时引用对方那句话', '第一条回复引用触发它的那条消息')}
      ${sw('group_at', '群里多人说话时 @ 要回答的人', '最近 10 分钟有两人以上在说话时，第一条回复 @ 对方（对方收到「有人@我」）')}
      ${sw('draw_quote', '画图时引用对方的请求', '发图前那句「在画了」引用对方要图的消息')}
    </div></div>
    <div class="card"><div class="card-head"><h2>直发能力</h2></div><div class="card-body">
      <dl class="kv"><dt>微信版本</dt><dd>${esc(ds.version || '未适配')}</dd><dt>连接</dt><dd>${ds.attached ? '已连接' : '未连接'}${ds.logged_in ? ' · 已登录' : ''}</dd></dl>
      <div class="chips" style="margin-top:12px">${['revoke:撤回', 'voice:语音条', 'quote:引用回复', 'at:@提醒'].map((x) => { const [k, l] = x.split(':'); return `<span class="badge ${ds[k] ? 'green' : ''}">${ds[k] ? '✓' : '–'} ${l}</span>`; }).join('')}</div>
      <div class="row" style="margin-top:14px"><a class="btn sm" href="#/a/${id}/direct">直发测试</a><a class="btn sm" href="#/a/${id}/voices">音色与复刻</a></div></div></div></div>`;
  const seg = () => $$('#modeSeg button').forEach((b) => b.classList.toggle('on', b.dataset.m === mode.mode));
  seg();
  const save = async (patch) => { try { mode = Object.assign(mode, await A(id, '/api/send_mode', {method: 'POST', json: patch})); seg(); toast('已保存'); loadAccounts(true); } catch (e) { toast(e.message, 'err'); } };
  $$('#modeSeg button').forEach((b) => b.onclick = () => { if (b.dataset.m === 'direct' && !can && !confirm('这个微信不支持直发，选了也会回退到界面发送。继续？')) return; save({mode: b.dataset.m}); });
  $$('[data-k]', view).forEach((i) => i.onchange = () => save({[i.dataset.k]: i.checked}));
}

// ---------------- 旧版页面 ----------------
function pageLegacy(view, id, key) {
  const [path, title] = LEGACY[key]; const s = acct(id);
  setTop(`<div class="row">${avatar(s, 'sm')}<h1>${esc(title)}</h1><span class="faint small ellipsis">旧版页面</span></div>`,
    `<a class="btn sm ghost" href="/a/${id}${path}" target="_blank">新窗口打开 ↗</a>`);
  view.className = 'content flush';
  view.innerHTML = `<iframe class="frame" src="/a/${id}${path}"></iframe>`;
}

// ---------------- 消息 ----------------
function fmtTime(ts) {
  if (!ts) return ''; const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
  if (d.toDateString() === now.toDateString()) return hm;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return '昨天 ' + hm;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}
async function pageChats(view, id) {
  const s = acct(id);
  setTop(`<div class="row">${avatar(s, 'sm')}<h1>消息</h1>${badge(s)}</div>`);
  view.className = 'content flush';
  view.innerHTML = `<div class="chat" id="chat">
    <div class="chat-list"><div class="search"><input class="input" id="q" placeholder="搜索会话"></div><div class="chat-items" id="items"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div></div>
    <div class="chat-pane" id="pane"><div class="empty" style="margin:auto"><div class="big">💬</div>选择左边的会话</div></div></div>`;
  let sessions = [], cur = null, quote = null, sig = '';
  const P = (p) => `/a/${id}${p}`;
  async function loadSessions() {
    try { sessions = await A(id, '/api/sessions'); drawList(); } catch (e) { $('#items').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }
  function drawList() {
    const q = $('#q').value.trim().toLowerCase();
    const list = sessions.filter((x) => !q || (x.name || '').toLowerCase().includes(q) || (x.username || '').includes(q));
    $('#items').innerHTML = list.map((x) => `<div class="chat-item ${cur && cur.username === x.username ? 'active' : ''}" data-u="${esc(x.username)}">
      <img class="avatar" src="${P('/api/avatar?username=' + encodeURIComponent(x.username))}" onerror="this.style.visibility='hidden'">
      <div class="grow"><div class="row"><span class="n grow ellipsis">${esc(x.name || x.username)}</span><span class="t">${fmtTime(x.last_timestamp)}</span></div>
      <div class="row"><span class="s grow ellipsis">${esc(x.summary || '')}</span>${x.unread ? `<span class="unread">${x.unread > 99 ? '99+' : x.unread}</span>` : ''}</div></div></div>`).join('') || '<div class="empty">没有会话</div>';
    $$('.chat-item').forEach((el) => el.onclick = () => open(sessions.find((x) => x.username === el.dataset.u)));
  }
  $('#q').oninput = drawList;
  function open(x) {
    cur = x; quote = null; sig = ''; drawList(); $('#chat').classList.add('open');
    $('#pane').innerHTML = `<div class="chat-head"><button class="btn ghost sm menu-btn" id="back">←</button><img class="avatar sm" src="${P('/api/avatar?username=' + encodeURIComponent(x.username))}" onerror="this.style.visibility='hidden'">
      <div class="grow"><b class="ellipsis">${esc(x.name || x.username)}</b><div class="faint small mono">${esc(x.username)}</div></div>
      ${x.is_group ? '<button class="btn sm" id="atBtn">@ 成员</button>' : ''}</div>
      <div class="msgs" id="msgs"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>
      <div class="compose"><div id="qbar"></div><textarea id="text" placeholder="输入消息，Enter 发送，Shift+Enter 换行"></textarea>
      <div class="row"><span class="faint small grow" id="sendHint">${s.send_mode === 'direct' ? '直发' : '界面发送'} · 群里写「@成员名」会变成真 @（直发）</span><button class="btn primary" id="sendBtn">发送</button></div></div>`;
    $('#back').onclick = () => $('#chat').classList.remove('open');
    $('#text').onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } };
    $('#sendBtn').onclick = send;
    if ($('#atBtn')) $('#atBtn').onclick = pickMember;
    loadMsgs(true);
  }
  async function pickMember() {
    try {
      const ms = (await A(id, `/api/groups/${encodeURIComponent(cur.username)}/members`)).filter((m) => m.wxid);
      const nm = (m) => m.group_nick || m.name || m.nick_name || m.wxid;
      const f = await modal({title: '@ 谁', ok: '插入', extra: `<select class="input" name="who">${ms.map((m) => `<option value="${esc(nm(m))}">${esc(nm(m))}</option>`).join('')}</select>`});
      if (f && f.who) { const t = $('#text'); t.value += (t.value && !t.value.endsWith(' ') ? ' ' : '') + '@' + f.who + ' '; t.focus(); }
    } catch (e) { toast(e.message, 'err'); }
  }
  function renderMsg(m) {
    if (m.type === 10000) return `<div class="sys">${esc(m.content)}</div>`;
    let body;
    if (m.type === 3) body = `<img src="${P(`/api/msgimage?chat=${encodeURIComponent(cur.username)}&id=${m.local_id}`)}" onclick="lightbox(this.src)" onerror="this.outerHTML='[图片]'">`;
    else if (m.type === 34) body = `<audio controls preload="none" src="${P(`/api/msgvoice?chat=${encodeURIComponent(cur.username)}&id=${m.local_id}`)}" style="height:32px;max-width:220px"></audio><div class="faint small">${esc(m.content)}</div>`;
    else if (m.type === 43) body = `<img src="${P(`/api/msgvideothumb?chat=${encodeURIComponent(cur.username)}&id=${m.local_id}`)}" onerror="this.outerHTML='[视频]'"><div class="faint small">视频</div>`;
    else body = esc(m.content || `[${m.type_name || m.type}]`);
    if (m.refer) body += `<div class="refer">${esc((m.refer.displayname ? m.refer.displayname + '：' : '') + (m.refer.content || ''))}</div>`;
    const who = cur.is_group && !m.is_self ? `<div class="who">${esc(m.sender_name || m.sender)}</div>` : '';
    const canQuote = (acct(id).direct || {}).quote && m.local_id && m.type !== 10000;
    return `<div class="msg ${m.is_self ? 'self' : ''}">${m.is_self ? '' : `<img class="avatar sm" src="${P('/api/avatar?username=' + encodeURIComponent(m.sender || cur.username))}" onerror="this.style.visibility='hidden'">`}
      <div>${who}<div class="bubble">${body}</div></div>
      ${canQuote ? `<div class="msg-tools"><button class="btn ghost sm" data-q="${m.local_id}" title="引用回复">↩</button></div>` : ''}</div>`;
  }
  async function loadMsgs(force) {
    if (!cur) return; const u = cur.username;
    let ms; try { ms = await A(id, `/api/messages?chat=${encodeURIComponent(u)}&limit=60`); } catch (e) { return; }
    if (!cur || cur.username !== u) return;
    const ns = ms.map((m) => m.local_id + ':' + m.type + ':' + (m.content || '').length).join(',');
    if (ns === sig && !force) return; sig = ns;
    const box = $('#msgs'); const atBottom = force || box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    let last = 0; let h = '';
    for (const m of ms) { if (m.create_time - last > 300) h += `<div class="time-sep">${fmtTime(m.create_time)}</div>`; last = m.create_time; h += renderMsg(m); }
    box.innerHTML = h || '<div class="empty">没有消息</div>';
    $$('[data-q]', box).forEach((b) => b.onclick = () => { const m = ms.find((x) => String(x.local_id) === b.dataset.q); quote = m; drawQuote(); $('#text').focus(); });
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
  function drawQuote() {
    $('#qbar').innerHTML = quote ? `<div class="quote-bar"><span class="grow ellipsis">引用 ${esc(quote.sender_name || (quote.is_self ? '我' : ''))}：${esc(quote.content || '')}</span><button class="btn ghost sm" id="qx">✕</button></div>` : '';
    if (quote) $('#qx').onclick = () => { quote = null; drawQuote(); };
  }
  async function send() {
    const t = $('#text'); const content = t.value.trim(); if (!content || !cur) return;
    const btn = $('#sendBtn'); btn.disabled = true;
    try {
      const body = {to: cur.name || cur.username, chat: cur.username, content};
      if (quote) body.quote = quote.local_id;
      const d = await A(id, '/api/send', {method: 'POST', json: body});
      t.value = ''; quote = null; drawQuote();
      for (let i = 0; i < 40 && d.job; i++) {
        await sleep(700); const r = await A(id, `/api/send/status?job=${d.job}`).catch(() => null);
        if (r && !['queued', 'running'].includes(r.status)) { if (!r.ok && r.status !== 'done') toast('发送结果：' + (r.reason || r.status), 'err'); break; }
      }
      loadMsgs(false); setTimeout(() => loadMsgs(false), 1500);
    } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; t.focus(); }
  }
  loadSessions();
  const t1 = setInterval(() => loadMsgs(false), 2500), t2 = setInterval(loadSessions, 8000);
  onLeave(() => { clearInterval(t1); clearInterval(t2); });
}
window.lightbox = lightbox;

// ---------------- 公共组件:选择联系人/群 ----------------
// 返回 {username, name, is_group} 或 null。opts.groups=false 只列联系人;opts.title 标题
async function pickChat(id, opts = {}) {
  const [contacts, groups] = await Promise.all([A(id, '/api/contacts').catch(() => []),
    opts.groups === false ? [] : A(id, '/api/groups').catch(() => [])]);
  const items = [...groups.map((g) => ({username: g.username, name: g.name || g.username, is_group: true, sub: `群 · ${g.member_count || '?'} 人`})),
    ...contacts.map((c) => ({username: c.username, name: c.remark || c.name || c.nick_name || c.username, is_group: false, sub: c.alias || c.username}))];
  return new Promise((resolve) => {
    const bg = document.createElement('div'); bg.className = 'modal-bg';
    bg.innerHTML = `<div class="modal" style="width:min(520px,calc(100vw - 32px))"><div class="mh"><h2>${esc(opts.title || '选择联系人或群')}</h2></div>
      <div class="mb"><input class="input" placeholder="搜索名字、备注、微信号" id="pk-q"><div id="pk-l" style="max-height:52vh;overflow:auto;margin-top:10px"></div></div>
      <div class="mf"><button class="btn" data-x>取消</button></div></div>`;
    const draw = () => {
      const q = $('#pk-q', bg).value.trim().toLowerCase();
      const list = items.filter((x) => !q || x.name.toLowerCase().includes(q) || x.username.toLowerCase().includes(q) || (x.sub || '').toLowerCase().includes(q)).slice(0, 300);
      $('#pk-l', bg).innerHTML = list.map((x, i) => `<div class="chat-item" data-i="${items.indexOf(x)}" style="border-radius:8px">
        <img class="avatar sm" src="/a/${id}/api/avatar?username=${encodeURIComponent(x.username)}" onerror="this.style.visibility='hidden'">
        <div class="grow"><div class="n ellipsis">${esc(x.name)}</div><div class="s ellipsis">${esc(x.sub || '')}</div></div></div>`).join('') || '<div class="empty">没有匹配的</div>';
    };
    bg.addEventListener('click', (e) => {
      const it = e.target.closest('[data-i]');
      if (it) { bg.remove(); resolve(items[+it.dataset.i]); return; }
      if (e.target === bg || e.target.closest('[data-x]')) { bg.remove(); resolve(null); }
    });
    document.body.appendChild(bg); $('#pk-q', bg).oninput = draw; draw(); $('#pk-q', bg).focus();
  });
}

// 给页面模块用的公共工具
window.WX = {$, $$, esc, sleep, api, A, toast, modal, busy, lightbox, badge, avatar, acct: (id) => acct(id),
  accounts: () => accounts, loadAccounts, setTop, onLeave, fmtTime, pickChat, STATE};

// ---------------- 启动 ----------------
$('#menuBtn').onclick = () => $('#app').classList.toggle('side-open');
$('#addBtn').onclick = () => { location.hash = '#/add'; };
$('#logoutBtn').onclick = async () => { await api('/api/console/logout', {method: 'POST'}).catch(() => {}); location.replace('/ui/login.html'); };
(async () => {
  try { const a = await api('/api/console/auth'); if (!a.ok) { location.replace('/ui/login.html'); return; } $('#whoami').textContent = a.user; } catch (e) { return; }
  await loadAccounts(true);
  render();
  pollTimer = setInterval(() => loadAccounts(false), 5000);
})();
