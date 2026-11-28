/* 定时任务:提醒任务 + 陪伴问候 + 一句话创建 + 执行记录。见 console/PAGES.md */
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct, onLeave, pickChat} = WX;

  const WD = ['周日', '周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const pad = (n) => String(n).padStart(2, '0');
  const isNum = (s) => /^\d+$/.test(s);
  const GREET_INT = {'*/30 * * * *': '30m', '0 * * * *': '1h', '0 */2 * * *': '2h', '0 */3 * * *': '3h'};
  const GREET_LABEL = {'30m': '每 30 分钟', '1h': '每 1 小时', '2h': '每 2 小时', '3h': '每 3 小时'};

  // ---------- cron → 中文 ----------
  function dowText(expr) {
    if (expr === '*') return '';
    const parts = expr.split(',').map((p) => {
      const m = p.match(/^(\d)-(\d)$/);
      if (m) return `${WD[+m[1]]}至${WD[+m[2]]}`;
      return isNum(p) ? WD[+p] : p;
    });
    const s = parts.join('、');
    if (expr === '1-5') return '工作日';
    if (expr === '0,6' || expr === '6,0') return '周末';
    return '每' + s;
  }
  function hourList(h, m) {
    return h.split(',').map((x) => {
      const r = x.match(/^(\d+)-(\d+)$/);
      if (r) return `${pad(r[1])}:${pad(m)}–${pad(r[2])}:${pad(m)} 每小时`;
      return isNum(x) ? `${pad(x)}:${pad(m)}` : x;
    }).join('、');
  }
  function cronText(cron) {
    const f = String(cron || '').trim().split(/\s+/);
    if (f.length !== 5) return cron || '未设置时间';
    const [mi, h, dom, mon, dow] = f;
    let day = '';
    if (mon !== '*') day += `${mon.replace(/,/g, '、')} 月`;
    if (dom !== '*') day += `${mon === '*' ? '每月 ' : ' '}${dom.replace(/,/g, '、')} 日`;
    if (dow !== '*') day += (day ? ' ' : '') + dowText(dow);
    let tm = '';
    const st = mi.match(/^\*\/(\d+)$/), hst = h.match(/^\*\/(\d+)$/);
    if (st && h === '*') tm = `每 ${st[1]} 分钟`;
    else if (mi === '*' && h === '*') tm = '每分钟';
    else if (isNum(mi) && h === '*') tm = +mi === 0 ? '每小时整点' : `每小时第 ${mi} 分`;
    else if (isNum(mi) && hst) tm = `每 ${hst[1]} 小时${+mi ? `（第 ${mi} 分）` : '（整点）'}`;
    else if (isNum(mi) && /^[\d,-]+$/.test(h)) tm = hourList(h, mi);
    else return `cron ${cron}`;
    if (!day) return (st || hst || h === '*') ? tm : `每天 ${tm}`;
    return `${day} ${tm}`;
  }
  function onceText(at) {
    const d = parseAt(at); if (!d) return `一次性 ${at}`;
    return `一次性 · ${relDay(d)}`;
  }
  const schedText = (t) => t.once_at ? onceText(t.once_at) : cronText(t.cron);

  function parseAt(s) { // 'YYYY-MM-DD HH:MM' 或 'MM-DD HH:MM'
    if (!s) return null;
    let m = String(s).match(/^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})/);
    if (m) return new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5]);
    m = String(s).match(/^(\d{1,2})-(\d{1,2}) (\d{1,2}):(\d{2})/);
    if (m) {
      const now = new Date(); let d = new Date(now.getFullYear(), m[1] - 1, +m[2], +m[3], +m[4]);
      if (d < now - 86400000 * 180) d = new Date(now.getFullYear() + 1, m[1] - 1, +m[2], +m[3], +m[4]);
      return d;
    }
    return null;
  }
  function relDay(d) {
    const now = new Date(), hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const day0 = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((day0(d) - day0(now)) / 86400000);
    if (diff === 0) return `今天 ${hm}`;
    if (diff === 1) return `明天 ${hm}`;
    if (diff === 2) return `后天 ${hm}`;
    if (diff === -1) return `昨天 ${hm}`;
    if (diff > 2 && diff < 7) return `${WD[d.getDay()]} ${hm}`;
    return `${d.getFullYear() !== now.getFullYear() ? d.getFullYear() + '年' : ''}${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
  }
  function nextText(t) {
    if (t.enabled === false) return '<span class="faint">已停用</span>';
    if (t.once_at) {
      const d = parseAt(t.once_at);
      if (d && d < new Date()) return '<span class="badge amber">已过时间，未发出</span>';
    }
    const d = parseAt(t.next);
    return d ? esc(relDay(d)) : '<span class="faint">—</span>';
  }
  const lastText = (t) => {
    const d = parseAt(t.last_fired);
    return d ? `上次 ${esc(relDay(d))} 已发送` : '还没执行过';
  };

  // ---------- 小组件 ----------
  const chatAv = (id, u) => `<img class="avatar sm" src="/a/${id}/api/avatar?username=${encodeURIComponent(u || '')}" onerror="this.style.visibility='hidden'">`;
  const sw = (cls, on, data) => `<label class="switch" title="${on ? '已启用，点一下停用' : '已停用，点一下启用'}"><input type="checkbox" class="${cls}" data-id="${data}" ${on ? 'checked' : ''}><span></span></label>`;

  function sheet(title, html, width = 600) { // 自定义表单弹窗(modal() 不收 textarea)
    const bg = document.createElement('div'); bg.className = 'modal-bg';
    bg.innerHTML = `<div class="modal sch-sheet" style="width:min(${width}px,calc(100vw - 32px))"><div class="mh"><h2>${esc(title)}</h2></div>
      <div class="mb">${html}</div><div class="mf"><span class="grow small sch-err"></span><button class="btn" data-x>取消</button><button class="btn primary" data-ok>保存</button></div></div>`;
    document.body.appendChild(bg);
    const close = () => bg.remove();
    bg.addEventListener('click', (e) => { if (e.target === bg || e.target.closest('[data-x]')) close(); });
    onLeave(close);
    return {el: bg, close, ok: $('[data-ok]', bg), err: (m) => { $('.sch-err', bg).textContent = m || ''; }};
  }

  WXPAGES.schedule = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>定时任务</h1></div>`,
      '<button class="btn" id="sch-new-greet">＋ 定时问候</button><button class="btn primary" id="sch-new">＋ 新建提醒</button>');
    view.innerHTML = '<div class="page"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>';

    let tasks = [], greets = [], log = [], personas = null;
    const loadPersonas = async () => {
      if (!personas) personas = await A(id, '/api/personas').catch(() => []);
      return personas;
    };
    const personaOpts = (cur, emptyLabel) => `<option value="">${esc(emptyLabel)}</option>` +
      (personas || []).map((p) => `<option value="${esc(p.slug)}" ${p.slug === cur ? 'selected' : ''}>${esc(p.name || p.slug)}</option>`).join('') +
      (cur && !(personas || []).some((p) => p.slug === cur) ? `<option value="${esc(cur)}" selected>${esc(cur)}</option>` : '');

    view.innerHTML = `<div class="page stack sch" style="gap:16px">
      <div class="card"><div class="card-head"><h2>用一句话创建</h2><span class="faint small">AI 解析，也能取消、改时间、列出任务</span></div>
        <div class="card-body stack" style="gap:10px">
          <textarea class="input" id="sch-nl" rows="2" style="min-height:64px" placeholder="例：每天早上 9 点在 test-001 群 @大南 提醒他交周报"></textarea>
          <div class="row" style="flex-wrap:wrap;gap:6px">
            ${['明天下午 3 点提醒老婆去开家长会', '工作日每天 18:30 提醒我下班打卡', '每隔 2 小时随便找我说点什么', '把周报提醒改到 10 点', '取消周报提醒']
              .map((x) => `<button class="sch-ex" type="button">${esc(x)}</button>`).join('')}
            <span class="grow"></span><button class="btn primary" id="sch-nl-go">执行</button>
          </div>
          <div id="sch-nl-msg" hidden></div>
        </div></div>
      <div class="card"><div class="card-head"><h2>提醒任务 <span class="faint small" id="sch-n"></span></h2>
        <span class="faint small">到点发给对方：私聊直发，群里 @ 对方；消息自动带「【定时提醒】」前缀</span></div>
        <div id="sch-list"></div></div>
      <div class="card"><div class="card-head"><h2>定时问候 <span class="faint small" id="gr-n"></span></h2>
        <button class="btn sm" id="gr-new">＋ 新建问候</button></div>
        <div class="card-body" style="padding-bottom:0"><div class="muted small">到点按当前时段、用该会话的聊天人设，自然地发一句陪伴问候（不带提醒前缀）。免打扰时段内不发；对方 10 分钟内来过消息（正在聊）时本次自动跳过。</div></div>
        <div id="gr-list"></div></div>
      <div class="card"><div class="card-head"><h2>执行记录</h2><button class="btn sm" id="sch-fires">查看发送明细</button></div>
        <div class="card-body stack" style="gap:12px">
          <div id="sch-hist"></div>
          <div id="sch-fires-out" hidden></div>
          <details class="sch-log"><summary>调度日志 <span class="faint small" id="sch-log-n"></span></summary><pre id="sch-log"></pre></details>
        </div></div>
    </div>`;

    // ---------- 渲染 ----------
    function rowHtml(t, greet) {
      const who = esc(t.target_display || t.target || t.target_username || '?');
      const badges = [];
      if (greet) {
        badges.push(`<span class="badge">${esc(GREET_LABEL[GREET_INT[t.cron]] || cronText(t.cron))}</span>`);
        if (t.quiet && t.quiet.start) badges.push(`<span class="badge">免打扰 ${esc(t.quiet.start)}–${esc(t.quiet.end)}</span>`);
        if (t.persona) badges.push(`<span class="badge blue">人设 ${esc(t.persona)}</span>`);
      } else {
        if (t.is_group) badges.push(`<span class="badge">群${t.mention_display || t.mention ? ' · @' + esc(t.mention_display || t.mention) : ''}</span>`);
        if (t.use_llm) badges.push(`<span class="badge blue">AI 生成${t.persona ? ' · ' + esc(t.persona) : ''}</span>`);
        if (t.creator_name) badges.push(`<span class="badge">由 ${esc(t.creator_name)} 设置</span>`);
      }
      const main = greet
        ? `<div class="n">${who}</div><div class="s">${badges.join('')}</div>`
        : `<div class="n">${esc(t.title || '定时任务')}</div>
           <div class="s"><span class="sch-to">→ ${who}</span><span>·</span><span>${esc(schedText(t))}</span>${badges.join('')}</div>
           ${t.prompt ? `<div class="sch-prompt">${esc(t.prompt)}</div>` : ''}`;
      return `<div class="sch-row ${t.enabled === false ? 'off' : ''}">
        ${chatAv(id, t.target_username)}
        <div class="sch-main">${main}</div>
        <div class="sch-when"><div>下次 ${nextText(t)}</div><div class="faint small">${lastText(t)}</div></div>
        ${sw(greet ? 'gr-en' : 'sch-en', t.enabled !== false, t.id)}
        <div class="sch-acts">
          <button class="btn sm" data-edit="${t.id}" data-g="${greet ? 1 : ''}">编辑</button>
          <button class="btn sm ghost" data-run="${t.id}" title="立即真实发送一次">试发</button>
          <button class="btn sm ghost danger" data-del="${t.id}">删除</button>
        </div></div>`;
    }
    function draw() {
      $('#sch-n').textContent = tasks.length ? `${tasks.length} 个` : '';
      $('#gr-n').textContent = greets.length ? `${greets.length} 条` : '';
      $('#sch-list').innerHTML = tasks.length ? tasks.map((t) => rowHtml(t, false)).join('')
        : '<div class="empty"><div class="big">⏰</div>还没有提醒任务。用上面一句话创建，或点「新建提醒」。</div>';
      $('#gr-list').innerHTML = greets.length ? greets.map((t) => rowHtml(t, true)).join('')
        : '<div class="empty" style="padding:28px">还没有定时问候</div>';
      const fired = [...tasks, ...greets].filter((t) => t.last_fired).sort((a, b) => String(b.last_fired).localeCompare(a.last_fired));
      $('#sch-hist').innerHTML = fired.length ? `<div class="muted small" style="margin-bottom:6px">每个任务最近一次成功发送：</div>
        <div class="sch-hist">${fired.map((t) => `<div class="sch-hrow"><span class="t">${esc(relDay(parseAt(t.last_fired)))}</span>
          ${chatAv(id, t.target_username)}<span class="grow ellipsis">${esc(t.kind === 'greeting' ? '定时问候' : (t.title || '定时任务'))} → ${esc(t.target_display || t.target || '')}</span>
          <span class="badge green">已发送</span></div>`).join('')}</div>`
        : '<div class="faint small">还没有任何任务执行过。</div>';
      $('#sch-log-n').textContent = log.length ? `${log.length} 条` : '';
      $('#sch-log').textContent = log.length ? log.slice().reverse().join('\n') : '（本次后台启动以来暂无日志）';
      bindRows();
    }
    async function load() {
      const [a, g] = await Promise.all([A(id, '/api/schedule'), A(id, '/api/greeting')]);
      tasks = a.tasks || []; greets = g.tasks || []; log = a.log || [];
      draw();
    }
    const all = () => [...tasks, ...greets];
    const find = (tid) => all().find((t) => String(t.id) === String(tid));

    function bindRows() {
      $$('.sch-en,.gr-en', view).forEach((i) => i.onchange = async () => {
        i.disabled = true;
        try { await A(id, `/api/schedule/${i.dataset.id}/update`, {method: 'POST', json: {enabled: i.checked}}); toast(i.checked ? '已启用' : '已停用'); await load(); }
        catch (e) { toast(e.message, 'err'); i.checked = !i.checked; i.disabled = false; }
      });
      $$('[data-del]', view).forEach((b) => b.onclick = async () => {
        const t = find(b.dataset.del); if (!t) return;
        const name = t.kind === 'greeting' ? `给「${t.target_display || t.target}」的定时问候` : `「${t.title || '定时任务'}」`;
        if (!await modal({title: '删除任务', body: `删除${esc(name)}？删除后不会再发送，无法恢复。`, ok: '删除', danger: true})) return;
        busy(b, async () => { await A(id, `/api/schedule/${t.id}/delete`, {method: 'POST'}); toast('已删除'); await load(); });
      });
      $$('[data-run]', view).forEach((b) => b.onclick = async () => {
        const t = find(b.dataset.run); if (!t) return;
        if (!await modal({title: '立即试发', body: `会<b>马上真实发送</b>一条消息给「${esc(t.target_display || t.target)}」${t.kind === 'greeting' ? '（AI 现场生成一句问候）' : (t.use_llm ? '（AI 现场生成内容）' : '')}，不影响下次定时。确定吗？`, ok: '立即发送'})) return;
        busy(b, async () => { await A(id, `/api/schedule/${t.id}/run`, {method: 'POST'}); toast('已开始执行，到「消息」里看结果', 'ok'); setTimeout(() => load().catch(() => {}), 8000); });
      });
      $$('[data-edit]', view).forEach((b) => b.onclick = () => {
        const t = find(b.dataset.edit); if (!t) return;
        (b.dataset.g ? greetForm : taskForm)(t);
      });
    }

    // ---------- 一句话 ----------
    $$('.sch-ex', view).forEach((b) => b.onclick = () => { $('#sch-nl').value = b.textContent; $('#sch-nl').focus(); });
    const nlOut = (ok, html) => { const m = $('#sch-nl-msg'); m.hidden = false; m.className = `callout ${ok ? 'green' : 'amber'}`; m.innerHTML = `<div>${html}</div>`; };
    $('#sch-nl-go').onclick = (e) => {
      const text = $('#sch-nl').value.trim(); if (!text) { $('#sch-nl').focus(); return; }
      busy(e.currentTarget, async () => {
        let r;
        try { r = await A(id, '/api/schedule/nl', {method: 'POST', json: {text}}); }
        catch (err) { nlOut(false, esc(err.message)); return; }
        if (r.action === 'list') {
          const ls = r.tasks || [];
          nlOut(true, ls.length ? `共 ${ls.length} 个任务：<br>` + ls.map((t) => `· ${esc(t.title || '')} → ${esc(t.target_display || '')}（${esc(schedText(t))}）`).join('<br>') : '没有任务');
        } else {
          nlOut(r.ok !== false && r.action !== 'ask', esc(r.message || '完成').replace(/\n/g, '<br>'));
        }
        if (r.ok !== false && ['create', 'cancel', 'update'].includes(r.action)) $('#sch-nl').value = '';
        await load();
      });
    };
    $('#sch-nl').onkeydown = (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) $('#sch-nl-go').click(); };

    // ---------- 执行明细 ----------
    $('#sch-fires').onclick = (e) => busy(e.currentTarget, async () => {
      const out = $('#sch-fires-out'); out.hidden = false;
      out.className = 'callout blue'; out.innerHTML = '<div>查询中…</div>';
      try {
        const {fires} = await A(id, '/api/schedule/fires?limit=50');
        const names = {}; [...tasks, ...greets].forEach((x) => { names[x.id] = x.title || x.target_display || ''; });
        out.className = '';
        out.innerHTML = fires.length ? `<div class="sch-hist">${fires.map((f) => `<div class="sch-hrow">
          <span class="t">${esc(new Date(f.ts * 1000).toLocaleString('zh-CN', {hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}))}</span>
          <span class="badge ${f.ok ? 'green' : 'red'}">${f.ok ? '已发' : '失败'}</span>
          <b class="ellipsis" style="max-width:160px">${esc(names[f.task_id] || f.title || '')}</b>
          <span class="grow ellipsis muted" title="${esc(f.text || '')}">${esc(f.text || '')}</span></div>`).join('')}</div>`
          : '<div class="empty" style="padding:20px">还没有发送记录</div>';
      } catch (err) { out.className = 'callout amber'; out.innerHTML = `<div>${esc(err.message)}</div>`; }
    });

    // ---------- 提醒任务表单 ----------
    function cronToForm(t) {
      if (t.once_at) return {mode: 'once', at: t.once_at.replace(' ', 'T')};
      const f = String(t.cron || '').trim().split(/\s+/);
      if (f.length === 5) {
        const [mi, h, dom, mon, dow] = f;
        const time = isNum(mi) && isNum(h) ? `${pad(h)}:${pad(mi)}` : '';
        if (time && dom === '*' && mon === '*' && dow === '*') return {mode: 'daily', time};
        if (time && dom === '*' && mon === '*' && /^[\d,-]+$/.test(dow)) {
          const days = new Set();
          dow.split(',').forEach((p) => { const r = p.match(/^(\d)-(\d)$/); if (r) for (let i = +r[1]; i <= +r[2]; i++) days.add(i % 7); else days.add(+p % 7); });
          return {mode: 'weekly', time, days};
        }
        if (time && isNum(dom) && mon === '*' && dow === '*') return {mode: 'monthly', time, dom: +dom};
        const sm = mi.match(/^\*\/(\d+)$/);
        if (sm && h === '*' && dom === '*' && mon === '*' && dow === '*') return {mode: 'every', n: +sm[1], unit: 'm'};
        const sh = h.match(/^\*\/(\d+)$/);
        if (mi === '0' && (sh || h === '*') && dom === '*' && mon === '*' && dow === '*') return {mode: 'every', n: sh ? +sh[1] : 1, unit: 'h'};
      }
      return {mode: 'cron', cron: t.cron || ''};
    }
    function dowExpr(days) {
      const d = [...days].sort((a, b) => a - b); if (!d.length) return null;
      const runs = []; let st = d[0], pv = d[0];
      for (const x of d.slice(1).concat([null])) {
        if (x === pv + 1) { pv = x; continue; }
        runs.push(st === pv ? `${st}` : (pv - st === 1 ? `${st},${pv}` : `${st}-${pv}`)); st = pv = x;
      }
      return runs.join(',');
    }

    async function taskForm(t) {
      const isNew = !t; t = t || {enabled: true};
      await loadPersonas();
      let target = t.target_username ? {username: t.target_username, name: t.target_display || t.target, is_group: !!t.is_group} : null;
      const origTarget = target && target.username;
      const f = cronToForm(t.cron || t.once_at ? t : {cron: '0 9 * * *'});
      const d0 = new Date(Date.now() + 3600e3);
      const defAt = `${d0.getFullYear()}-${pad(d0.getMonth() + 1)}-${pad(d0.getDate())}T${pad(d0.getHours())}:00`;
      const sh = sheet(isNew ? '新建提醒任务' : '编辑提醒任务', `<div class="stack" style="gap:14px">
        <div class="field"><label>发给</label><div class="row"><div class="grow" id="tf-tg"></div><button class="btn sm" id="tf-pick" type="button">${target ? '更换' : '选择联系人或群'}</button></div></div>
        <div class="field" id="tf-mwrap" hidden><label>在群里 @ 谁（可留空 = 不 @）</label><input class="input" id="tf-mention" list="tf-members" value="${esc(t.mention || '')}" placeholder="群成员的群昵称/备注/昵称"><datalist id="tf-members"></datalist></div>
        <div class="field"><label>标题</label><input class="input" id="tf-title" value="${esc(t.title || '')}" placeholder="例如：交周报提醒（留空会用内容开头）"></div>
        <div class="field"><label>时间</label>
          <div class="seg" id="tf-mode">${[['once', '一次性'], ['daily', '每天'], ['weekly', '每周'], ['monthly', '每月'], ['every', '每隔'], ['cron', '自定义']].map(([k, l]) => `<button type="button" data-m="${k}">${l}</button>`).join('')}</div>
          <div class="sch-when-box">
            <div data-p="once"><input class="input" type="datetime-local" id="tf-at" value="${esc(f.at || defAt)}"></div>
            <div data-p="daily weekly monthly" class="row" style="gap:10px;flex-wrap:wrap">
              <span data-p="monthly" class="row" style="gap:6px">每月<input class="input" type="number" min="1" max="31" id="tf-dom" value="${f.dom || 1}" style="width:76px">日</span>
              <input class="input" type="time" id="tf-time" value="${esc(f.time || '09:00')}" style="width:130px">
            </div>
            <div data-p="weekly" class="sch-days">${[1, 2, 3, 4, 5, 6, 0].map((i) => `<label><input type="checkbox" value="${i}" ${(f.days || new Set([1, 2, 3, 4, 5])).has(i) ? 'checked' : ''}><span>${WD[i].slice(1)}</span></label>`).join('')}</div>
            <div data-p="every" class="row" style="gap:8px">每<input class="input" type="number" min="1" id="tf-n" value="${f.n || 1}" style="width:80px"><select class="input" id="tf-unit" style="width:100px"><option value="m" ${f.unit === 'm' ? 'selected' : ''}>分钟</option><option value="h" ${f.unit !== 'm' ? 'selected' : ''}>小时</option></select>发一次</div>
            <div data-p="cron"><input class="input" id="tf-cron" value="${esc(f.cron || t.cron || '')}" placeholder="分 时 日 月 周，例如 0 18 * * 3-5" style="font-family:ui-monospace,monospace"><div class="faint small" style="margin-top:4px">5 个字段依次是「分 时 日 月 周」，不用的填 *。例：<code>0 18 * * 3-5</code> = 周三至周五 18:00。</div></div>
            <div class="small muted" id="tf-preview"></div>
          </div></div>
        <div class="field"><label>内容</label><textarea class="input" id="tf-prompt" rows="3" placeholder="固定话术直接写要发的话；打开「AI 生成」时写要求，例如：总结一下今天的新闻">${esc(t.prompt || '')}</textarea></div>
        <div class="setting" style="border:0;padding:0"><div class="grow"><div class="t">AI 生成内容</div><div class="d">每次到点让 AI 按上面的要求现写一条；关闭则原样发送</div></div><label class="switch"><input type="checkbox" id="tf-llm" ${t.use_llm ? 'checked' : ''}><span></span></label></div>
        <div class="field" id="tf-pwrap"><label>用谁的口吻（人设，可选）</label><select class="input" id="tf-persona">${personaOpts(t.persona || '', '默认（不指定人设）')}</select></div>
        <div class="setting" style="border:0;padding:0"><div class="grow"><div class="t">启用</div><div class="d">停用后保留任务但不会发送</div></div><label class="switch"><input type="checkbox" id="tf-en" ${t.enabled !== false ? 'checked' : ''}><span></span></label></div>
      </div>`, 620);
      const el = sh.el; let mode = f.mode;
      const drawTarget = async () => {
        $('#tf-tg', el).innerHTML = target ? `<div class="row">${chatAv(id, target.username)}<b>${esc(target.name)}</b>${target.is_group ? '<span class="badge">群</span>' : ''}</div>` : '<span class="faint">还没选</span>';
        $('#tf-pick', el).textContent = target ? '更换' : '选择联系人或群';
        $('#tf-mwrap', el).hidden = !(target && target.is_group);
        if (target && target.is_group) {
          const ms = await A(id, `/api/groups/${encodeURIComponent(target.username)}/members`).catch(() => []);
          $('#tf-members', el).innerHTML = (Array.isArray(ms) ? ms : []).slice(0, 500).map((m) => `<option value="${esc(m.group_nick || m.remark || m.nick_name || m.name || '')}">`).join('');
        }
      };
      const cronOf = () => {
        const [hh, mm] = ($('#tf-time', el).value || '09:00').split(':').map(Number);
        if (mode === 'daily') return `${mm} ${hh} * * *`;
        if (mode === 'weekly') { const e = dowExpr($$('.sch-days input:checked', el).map((i) => +i.value)); return e ? `${mm} ${hh} * * ${e}` : null; }
        if (mode === 'monthly') { const d = Math.min(31, Math.max(1, +$('#tf-dom', el).value || 1)); return `${mm} ${hh} ${d} * *`; }
        if (mode === 'every') { const n = Math.max(1, +$('#tf-n', el).value || 1); return $('#tf-unit', el).value === 'm' ? `*/${n} * * * *` : (n === 1 ? '0 * * * *' : `0 */${n} * * *`); }
        if (mode === 'cron') return $('#tf-cron', el).value.trim() || null;
        return null;
      };
      const onceOf = () => mode === 'once' ? ($('#tf-at', el).value || '').replace('T', ' ').slice(0, 16) : '';
      const preview = () => {
        $$('#tf-mode button', el).forEach((b) => b.classList.toggle('on', b.dataset.m === mode));
        $$('[data-p]', el).forEach((p) => { p.hidden = !p.dataset.p.split(' ').includes(mode); });
        const c = cronOf(), o = onceOf();
        $('#tf-preview', el).textContent = mode === 'once' ? (o ? '将在 ' + relDay(parseAt(o)) + ' 发送一次' : '') : (c ? '→ ' + cronText(c) : '请选择至少一天');
        $('#tf-pwrap', el).hidden = !$('#tf-llm', el).checked;
      };
      $$('#tf-mode button', el).forEach((b) => b.onclick = () => { mode = b.dataset.m; preview(); });
      $$('input,select', $('.sch-when-box', el)).forEach((i) => { i.oninput = preview; i.onchange = preview; });
      $('#tf-llm', el).onchange = preview;
      $('#tf-pick', el).onclick = async () => { const c = await pickChat(id, {title: '发给谁'}); if (c) { target = c; drawTarget(); } };
      drawTarget(); preview();

      sh.ok.onclick = () => busy(sh.ok, async () => {
        sh.err('');
        if (!target) return sh.err('请选择发给谁');
        const cron = mode === 'once' ? '' : cronOf(), once_at = onceOf();
        if (mode === 'once' ? !once_at : !cron) return sh.err('请把时间填完整');
        if (mode === 'once' && parseAt(once_at) < new Date()) return sh.err('一次性时间已经过去了');
        const prompt = $('#tf-prompt', el).value.trim();
        if (!prompt) return sh.err('请填写内容');
        const fields = {title: $('#tf-title', el).value.trim(), cron, once_at, prompt,
          use_llm: $('#tf-llm', el).checked, mention: target.is_group ? $('#tf-mention', el).value.trim() : '',
          enabled: $('#tf-en', el).checked};
        // 人设只在 AI 生成时有意义;留空 = 清掉任务人设,用会话/全局人设
        fields.persona = fields.use_llm ? $('#tf-persona', el).value : '';
        if (isNew || target.username !== origTarget) fields.target = target.username;
        try {
          if (isNew) await A(id, '/api/schedule', {method: 'POST', json: fields});
          else await A(id, `/api/schedule/${t.id}/update`, {method: 'POST', json: fields});
        } catch (e) { sh.err(e.message); return; }
        sh.close(); toast(isNew ? '已创建' : '已保存', 'ok'); await load();
      });
    }

    // ---------- 定时问候表单 ----------
    async function greetForm(t) {
      const isNew = !t; t = t || {enabled: true, quiet: {start: '23:00', end: '08:00'}, cron: '0 * * * *'};
      await loadPersonas();
      let targets = t.target_username ? [{username: t.target_username, name: t.target_display || t.target}] : [];
      const origTarget = t.target_username;
      let iv = GREET_INT[t.cron] || '1h';
      const hasQuiet = !!(t.quiet && t.quiet.start);
      const sh = sheet(isNew ? '新建定时问候' : '编辑定时问候', `<div class="stack" style="gap:14px">
        <div class="field"><label>问候对象${isNew ? '（可以加多个人，各建一条）' : ''}</label><div class="chips" id="gf-tg"></div></div>
        <div class="field"><label>频率</label><div class="seg" id="gf-iv">${Object.entries(GREET_LABEL).map(([k, l]) => `<button type="button" data-v="${k}">${l}</button>`).join('')}</div>
          ${GREET_INT[t.cron] || isNew ? '' : `<div class="faint small">当前是 ${esc(cronText(t.cron))}，保存后改成所选频率</div>`}</div>
        <div class="field"><label>免打扰</label><div class="row" style="gap:8px;flex-wrap:wrap">
          <label class="switch"><input type="checkbox" id="gf-q" ${hasQuiet || isNew ? 'checked' : ''}><span></span></label>
          <input class="input" type="time" id="gf-qs" value="${esc((t.quiet && t.quiet.start) || '23:00')}" style="width:120px"><span class="muted">到</span>
          <input class="input" type="time" id="gf-qe" value="${esc((t.quiet && t.quiet.end) || '08:00')}" style="width:120px"><span class="faint small">这段时间不发，可以跨零点</span></div></div>
        <div class="field"><label>人设</label><select class="input" id="gf-persona">${personaOpts(t.persona || '', '跟随该会话的聊天人设')}</select></div>
        <div class="setting" style="border:0;padding:0"><div class="grow"><div class="t">启用</div></div><label class="switch"><input type="checkbox" id="gf-en" ${t.enabled !== false ? 'checked' : ''}><span></span></label></div>
      </div>`, 560);
      const el = sh.el;
      const drawT = () => {
        $('#gf-tg', el).innerHTML = targets.map((c, i) => `<span class="sch-chip">${chatAv(id, c.username)}${esc(c.name)}${isNew ? `<button type="button" data-rm="${i}" title="移除">×</button>` : ''}</span>`).join('') +
          `<button class="btn sm" type="button" id="gf-add">${isNew ? '＋ 添加' : '更换'}</button>`;
        $$('[data-rm]', el).forEach((b) => b.onclick = () => { targets.splice(+b.dataset.rm, 1); drawT(); });
        $('#gf-add', el).onclick = async () => {
          const c = await pickChat(id, {title: '问候谁', groups: false}); if (!c) return;
          if (isNew) { if (!targets.some((x) => x.username === c.username)) targets.push(c); } else targets = [c];
          drawT();
        };
      };
      const drawIv = () => $$('#gf-iv button', el).forEach((b) => b.classList.toggle('on', b.dataset.v === iv));
      $$('#gf-iv button', el).forEach((b) => b.onclick = () => { iv = b.dataset.v; drawIv(); });
      const drawQ = () => { const on = $('#gf-q', el).checked; $('#gf-qs', el).disabled = $('#gf-qe', el).disabled = !on; };
      $('#gf-q', el).onchange = drawQ;
      drawT(); drawIv(); drawQ();
      sh.ok.onclick = () => busy(sh.ok, async () => {
        sh.err('');
        if (!targets.length) return sh.err('请选择问候对象');
        const q = $('#gf-q', el).checked;
        const body = {interval: iv, quiet_start: q ? $('#gf-qs', el).value : '', quiet_end: q ? $('#gf-qe', el).value : '',
          enabled: $('#gf-en', el).checked};
        const persona = $('#gf-persona', el).value;
        if (persona) body.persona = persona;
        if (isNew) body.targets = targets.map((x) => x.username);
        else { body.id = t.id; if (targets[0].username !== origTarget) body.target = targets[0].username; }
        let r;
        try { r = await A(id, '/api/greeting', {method: 'POST', json: body}); } catch (e) { sh.err(e.message); return; }
        sh.close();
        const n = (r.tasks || []).length, bad = (r.failed || []).length;
        toast(isNew ? `已创建 ${n} 条问候${bad ? `，${bad} 条失败` : ''}` : '已保存', bad ? 'err' : 'ok');
        await load();
      });
    }

    $('#sch-new').onclick = () => taskForm(null);
    $('#sch-new-greet').onclick = $('#gr-new').onclick = () => greetForm(null);

    try { await load(); } catch (e) {
      view.innerHTML = `<div class="page"><div class="callout red"><div>读取定时任务失败：${esc(e.message)}</div></div></div>`; return;
    }
    const timer = setInterval(() => { if (!document.querySelector('.sch-sheet')) load().catch(() => {}); }, 30000);
    onLeave(() => clearInterval(timer));
  };
})();
