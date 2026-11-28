/* 机器人与规则:运行状态 + 实时日志 + 监听/跟发/主动与问候/推送/管理员/上下文与画像。接口见 server.py /api/bot* */
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct, onLeave, pickChat, fmtTime, loadAccounts} = WX;

  const OFFICIAL = (u) => u.startsWith('gh_') || u.endsWith('@openim') || u.includes('@kefu') ||
    ['brandsessionholder', 'weixin', 'filehelper', 'newsapp', 'fmessage', 'medianote', 'floatbottle', 'notifymessage', 'qqmail', 'tmessage', 'qmessage', 'qqsafe', 'mphelper', 'notification_messages', 'exmail_tool', 'brandservicesessionholder', 'opencustomerservicemsg'].includes(u);
  const ALL_GROUPS = {username: '*', name: '所有群', sub: '包括以后新加入的群', star: true};
  const INTERVALS = {'30m': '每 30 分钟', '1h': '每 1 小时', '2h': '每 2 小时', '3h': '每 3 小时'};
  const CRON2INT = {'*/30 * * * *': '30m', '0 * * * *': '1h', '0 */2 * * *': '2h', '0 */3 * * *': '3h'};
  const SCOPE = {legacy_search: '界面搜索发送（文字 + 图片）', private_text: '仅私聊文字', unknown: '未知'};
  const TABS = [['watch', '监听'], ['proactive', '主动与问候'], ['follow', '跟发'], ['push', '推送'], ['admins', '管理员'], ['context', '上下文与画像']];
  let lastTab = 'watch';

  const ago = (ts) => {
    if (!ts) return '—';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return `${Math.round(s)} 秒前`;
    if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
    return fmtTime(ts);
  };
  const sw = (id, t, d, on) => `<div class="setting"><div class="grow"><div class="t">${t}</div><div class="d">${d}</div></div>
    <label class="switch"><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span></span></label></div>`;
  const img = (id, u) => u === '*' ? '<div class="avatar sm">群</div>'
    : `<img class="avatar sm" loading="lazy" src="/a/${id}/api/avatar?username=${encodeURIComponent(u)}" onerror="this.style.visibility='hidden'">`;

  // 可搜索的勾选列表。items: [{username,name,sub}],selected: 用户名数组。返回 {get(), set(list)}
  function checklist(el, id, items, selected, {onChange, empty = '没有可选的'} = {}) {
    const sel = new Set(selected);
    const known = new Set(items.map((x) => x.username));
    for (const u of selected) if (!known.has(u)) items.push({username: u, name: u, sub: '不在通讯录 / 群列表里'});
    let onlySel = false;
    el.innerHTML = `<div class="bot-cl"><div class="bot-cl-bar"><input class="input" placeholder="搜索名字、备注、微信号"><button class="btn sm ghost" data-only></button>
      <button class="btn sm ghost" data-none>全不选</button></div><div class="bot-cl-list"></div></div>`;
    const q = $('input', el), list = $('.bot-cl-list', el), only = $('[data-only]', el);
    const draw = () => {
      const k = q.value.trim().toLowerCase();
      only.textContent = onlySel ? `显示全部（${items.length}）` : `只看已选（${sel.size}）`;
      const rows = items.filter((x) => (!onlySel || sel.has(x.username)) &&
        (!k || x.name.toLowerCase().includes(k) || x.username.toLowerCase().includes(k) || (x.sub || '').toLowerCase().includes(k)))
        .sort((a, b) => (b.star ? 2 : 0) - (a.star ? 2 : 0) || (sel.has(b.username) ? 1 : 0) - (sel.has(a.username) ? 1 : 0));
      list.innerHTML = rows.map((x) => `<label class="chat-item bot-cl-item ${sel.has(x.username) ? 'on' : ''}">
        <input type="checkbox" value="${esc(x.username)}" ${sel.has(x.username) ? 'checked' : ''}>${img(id, x.username)}
        <div class="grow"><div class="n ellipsis">${esc(x.name)}</div><div class="s ellipsis">${esc(x.sub || x.username)}</div></div></label>`).join('')
        || `<div class="empty small">${k ? '没有匹配的' : esc(empty)}</div>`;
    };
    list.onchange = (e) => {
      const c = e.target; if (c.type !== 'checkbox') return;
      c.checked ? sel.add(c.value) : sel.delete(c.value);
      c.closest('label').classList.toggle('on', c.checked);
      only.textContent = onlySel ? `显示全部（${items.length}）` : `只看已选（${sel.size}）`;
      onChange && onChange();
    };
    q.oninput = draw;
    only.onclick = () => { onlySel = !onlySel; draw(); };
    $('[data-none]', el).onclick = () => { sel.clear(); draw(); onChange && onChange(); };
    draw();
    return {get: () => [...sel], set: (l) => { sel.clear(); l.forEach((u) => sel.add(u)); draw(); }};
  }

  // 每个分页底部的保存栏:有改动时提示
  const saveBar = (key, label = '保存') => `<div class="bot-save"><span class="faint small" data-dirty="${key}"></span><button class="btn primary" data-save="${key}">${label}</button></div>`;
  const markDirty = (view, key, on = true) => { const e = $(`[data-dirty="${key}"]`, view); if (e) e.textContent = on ? '有未保存的修改' : ''; };

  WXPAGES.bot = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>机器人与规则</h1></div>`,
      `<button class="btn" id="greetNow">👋 打招呼</button><button class="btn" id="resetTop">清理上下文</button>`);
    view.innerHTML = '<div class="page"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>';

    let bot, contacts, groups, sessions, follow, push, greet, access;
    try {
      [bot, contacts, groups, sessions, follow, push, greet, access] = await Promise.all([
        A(id, '/api/bot'), A(id, '/api/contacts').catch(() => []), A(id, '/api/groups').catch(() => []),
        A(id, '/api/sessions').catch(() => []), A(id, '/api/bot/follow'), A(id, '/api/bot/push'),
        A(id, '/api/greeting').catch(() => ({tasks: []})), A(id, '/api/admin/access').catch(() => ({}))]);
    } catch (e) { view.innerHTML = `<div class="page"><div class="callout red">读不到机器人状态：${esc(e.message)}</div></div>`; return; }
    if (!Array.isArray(contacts)) contacts = [];
    if (!Array.isArray(groups)) groups = [];
    if (!Array.isArray(sessions)) sessions = [];

    // 名字表 + 候选列表
    const names = {};
    const people = contacts.filter((c) => !OFFICIAL(c.username) && !c.username.endsWith('@chatroom'))
      .map((c) => ({username: c.username, name: c.remark || c.name || c.nick_name || c.username, sub: c.alias ? `微信号 ${c.alias}` : c.username}));
    for (const x of sessions) if (!x.is_group && !x.username.endsWith('@chatroom') && !OFFICIAL(x.username) && !people.some((p) => p.username === x.username))
      people.push({username: x.username, name: x.name || x.username, sub: '最近会话'});
    const grps = groups.map((g) => ({username: g.username, name: g.name || g.username, sub: `群 · ${g.member_count || '?'} 人`}));
    [...people, ...grps].forEach((x) => { names[x.username] = x.name; });
    names['*'] = '所有群';
    const nm = (u) => names[u] || u;
    const pPeople = () => people.map((x) => ({...x, sub: x.sub}));
    const pGroups = () => grps.map((x) => ({...x}));

    const R = bot.rules || {};
    const pc = R.proactive || {};
    const rule = (R.rules || []).find((r) => r.action && r.action.type === 'reply_ai');

    view.innerHTML = `<div class="page stack" style="gap:16px">
      <div class="card"><div class="card-body bot-status">
        <div class="bot-run"><div class="bot-run-ico" id="runIco">🤖</div><div class="grow"><div class="bot-run-t" id="runT"></div><div class="muted small" id="runD"></div></div>
          <label class="switch bot-big"><input type="checkbox" id="botSw"><span></span></label></div>
        <div class="bot-facts" id="facts"></div>
      </div></div>

      <div class="card"><div class="card-head"><h2>实时日志</h2><div class="row">
          <input class="input bot-log-q" id="logQ" placeholder="筛选日志…">
          <label class="row small muted" style="gap:6px;cursor:pointer"><input type="checkbox" id="logFollow" checked>自动滚到底</label>
          <button class="btn sm ghost" id="logCopy">复制</button></div></div>
        <div class="bot-log" id="log"></div>
        <div class="faint small bot-log-foot">显示最近 30 条，每 4 秒刷新 · <span id="logTime"></span></div></div>

      <div class="card"><div class="card-head bot-tabs-head"><div class="seg" id="tabs">${TABS.map(([k, l]) => `<button data-tab="${k}">${l}</button>`).join('')}</div></div>
        <div class="card-body">

        <section data-pane="watch">
          <p class="bot-intro">勾选的会话才会自动回复：<b>私聊</b>对方一发消息就回；<b>群</b>被 @ 或被引用时回（开了「主动与问候 → 群里安静时接话」后也会主动接话）。没勾的会话机器人不动。</p>
          ${sw('wSelf', '回复我自己发的消息', '默认关。开着时你在手机上发的消息也会触发回复；战斗模式开着时等于自己怼自己。', R.include_self)}
          <div class="bot-sub">监听的会话</div><div id="wList"></div>
          ${saveBar('watch', '保存监听')}
        </section>

        <section data-pane="proactive" hidden>
          <p class="bot-intro">机器人在对方不说话时主动开口。所有主动消息都遵守免打扰时段，对方明确说过「别主动找我」也不会发。</p>
          ${sw('pOn', '开启主动模式', '总开关。关掉后下面两项都不生效。', pc.enabled === true)}
          ${sw('pPriv', '私聊冷场后主动跟进', '我方说完最后一句、对方一直没回时，过一段时间用人设再找个话题（分享近况）。只对监听中的私聊生效。', pc.private_share_enabled === true)}
          ${sw('pGroup', '群里安静时接话', '监听中的群停顿一段时间后，读最近约 10 条消息自然地接一句；每个群至少间隔 30 分钟。', pc.group_enabled === true)}
          <div class="grid cols-3 bot-nums">
            <div class="field"><label>群里停顿多久后接话（分钟）</label><input class="input" type="number" min="1" max="1440" id="pGroupAfter" value="${Math.round((pc.group_after ?? 300) / 60)}"></div>
            <div class="field"><label>免打扰开始（北京时间，点）</label><input class="input" type="number" min="0" max="23" id="pQs" value="${pc.quiet_start ?? 22}"></div>
            <div class="field"><label>免打扰结束（点）</label><input class="input" type="number" min="0" max="23" id="pQe" value="${pc.quiet_end ?? 8}"></div>
          </div>
          <details class="bot-more"><summary>私聊跟进的节奏（高级）</summary>
            <div class="grid cols-3 bot-nums">
              <div class="field"><label>冷场多久后第一次跟进（小时）</label><input class="input" type="number" min="0.5" step="0.5" id="pAfter" value="${(pc.after ?? 7200) / 3600}"></div>
              <div class="field"><label>没回的话再隔多久（小时）</label><input class="input" type="number" min="0.5" step="0.5" id="pGap" value="${(pc.gap ?? 14400) / 3600}"></div>
              <div class="field"><label>一轮最多跟进几次</label><input class="input" type="number" min="0" max="10" id="pMax" value="${pc.max ?? 2}"></div>
            </div><div class="faint small" style="margin-top:8px">对方一开口就重新计数。默认：冷场 2 小时跟进一次，再隔 4 小时一次，最多 2 次。</div>
          </details>
          ${saveBar('proactive', '保存主动设置')}

          <div class="bot-divider"></div>
          <div class="row" style="justify-content:space-between;align-items:flex-end"><div><div class="bot-sub" style="margin:0">定时问候</div>
            <div class="muted small" style="margin-top:2px">到点按当前时段、用该会话的人设发一句「就在身边」的陪伴问候。免打扰时段内不发；对方正在聊天时本次跳过。</div></div>
            <span class="badge" id="gCount"></span></div>
          <div id="gList" class="bot-greets"></div>
          <details class="bot-more" id="gNewBox"><summary>＋ 新建问候</summary>
            <div class="stack" style="gap:12px;margin-top:12px">
              <div class="field"><label>问候对象（可多选，每人各建一条）</label><div id="gTargets"></div></div>
              <div class="grid cols-3 bot-nums">
                <div class="field"><label>频率</label><select class="input" id="gInt">${Object.entries(INTERVALS).map(([k, l]) => `<option value="${k}" ${k === '1h' ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
                <div class="field"><label>免打扰开始</label><input class="input" type="time" id="gQs" value="23:00"></div>
                <div class="field"><label>免打扰结束</label><input class="input" type="time" id="gQe" value="08:00"></div>
              </div>
              <label class="row small" style="cursor:pointer"><input type="checkbox" id="gEn" checked> 建好就启用</label>
              <div><button class="btn primary" id="gCreate">新建问候</button></div>
            </div></details>
        </section>

        <section data-pane="follow" hidden>
          <p class="bot-intro">接龙 / +1：勾选的群里，同一句话被<b>连续</b>发到第 N 次时，机器人跟发这句一次。同一串只跟一次，内容变了才重新计数。</p>
          <div class="field" style="max-width:260px"><label>连续相同几次触发（N ≥ 2）</label><input class="input" type="number" min="2" id="fThr" value="${follow.threshold || 3}"></div>
          <div class="bot-sub">开启跟发的群</div><div id="fList"></div>
          ${saveBar('follow', '保存跟发')}
        </section>

        <section data-pane="push" hidden>
          <p class="bot-intro">把监听会话里<b>任何人</b>的<b>任何消息</b>（文字、图片、红包、转账…）转发给下面的目标，适合把重要的群消息转到自己的大号或者企业微信 / 飞书机器人。</p>
          ${sw('puOn', '启用监听推送', '关掉时只保存配置、不推送。', push.enabled)}
          ${sw('puSelf', '连这个微信自己发的消息也推', '默认只推别人发的。', push.include_self)}
          <div class="bot-sub">推送目标</div>
          <div id="puTargets" class="stack" style="gap:8px"></div>
          <div class="row" style="margin-top:10px"><button class="btn sm" id="puAddWx">＋ 微信好友 / 群</button><a class="btn sm ghost" href="#/a/${id}/webhook">推给外部系统？用 Webhook 订阅（加密）→</a></div>
          <details class="bot-more"><summary>推送来源（默认＝监听的会话）</summary>
            <div class="muted small" style="margin:10px 0">一个都不选时，跟随「监听」里勾选的会话。推送目标本身永远不会被当作来源（防止循环转发）。</div>
            <div id="puSrc"></div></details>
          <div class="bot-save"><span class="faint small" data-dirty="push"></span><button class="btn" id="puTest">发测试消息</button><button class="btn primary" data-save="push">保存推送</button></div>
        </section>

        <section data-pane="admins" hidden>
          <p class="bot-intro">管理员可以在跟这个微信的<b>会话里</b>发 <code>/命令</code> 直接触发系统功能（私聊直接发，群里要 @ 我）。只授权你信任的人。</p>
          <details class="bot-more" open><summary>可用命令</summary><div class="bot-cmds">
            <div><code>/朋友圈 内容</code><span>发朋友圈（内容留空＝自动生成）</span></div>
            <div><code>/配图朋友圈 内容</code><span>发图文朋友圈（按内容生成配图）</span></div>
            <div><code>/同步朋友圈</code><span>立即同步朋友圈</span></div>
            <div><code>/朋友圈状态</code><span>查看同步与自动化状态</span></div>
            <div><code>/设置 项 开|关</code><span>项：自动评论 / 自动发布 / 聊天反思 / 配图 / 网络观点 / 同步</span></div>
            <div><code>/发 联系人 内容</code><span>代发一条消息</span></div>
            <div><code>/开启战斗模式</code> <code>/开启持续模式</code><span>在当前会话开启（对应「关闭」）</span></div>
            <div><code>/帮助</code><span>查看全部命令</span></div></div></details>
          <div class="bot-sub">管理员（只能选联系人）</div><div id="aList"></div>
          ${saveBar('admins', '保存管理员')}
        </section>

        <section data-pane="context" hidden>
          <div class="grid cols-2">
            <div class="bot-tile"><div class="bot-tile-t">清理某个会话的上下文</div>
              <div class="muted small">机器人在这个会话里「翻旧账」、记混了、或者换了人设想重新开始时用：丢掉排队中的回复，之后只按当前人设和这之后的新消息回复，旧对话不再当记忆。不会删除聊天记录。</div>
              <button class="btn" id="resetBtn">选择会话并清理…</button></div>
            <div class="bot-tile"><div class="bot-tile-t">联系人画像</div>
              ${sw('learnOn', '聊天中自动抽画像', '机器人回复时顺带从对话里提取对方的喜好、近况等写进画像。会多花 token，默认关。', R.learn_profiles === true)}
              <div class="muted small" style="margin-bottom:10px">也可以对单个会话立刻抽一次（不受上面开关影响）。画像内容在「人设与画像」里查看和修改。</div>
              <div class="row"><button class="btn" id="extractBtn">选择会话并立刻抽取…</button><a class="btn ghost" href="#/a/${id}/persona">人设与画像 →</a></div></div>
          </div>
        </section>
      </div></div></div>`;

    // ---------- 状态与日志 ----------
    const drawStatus = () => {
      const run = !!bot.running, mr = bot.message_reader || {}, a = acct(id) || {};
      $('#botSw').checked = run;
      $('#runIco').classList.toggle('on', run);
      $('#runT').innerHTML = run ? '机器人运行中' : '机器人已停止';
      $('#runD').textContent = !run ? '不会自动回复任何消息；定时任务照常执行。'
        : bot.sending_available === false ? '在监听，但微信发送不可用：自动回复和打招呼发不出去。'
          : `正在监听 ${(R.watch || []).length} 个会话，按规则和人设自动回复。`;
      const mrOk = mr.running && mr.reason === 'ready';
      const sendMode = a.send_mode === 'direct' ? '直发' : '界面自动化';
      $('#facts').innerHTML = `
        <div><span>消息读取</span><b><span class="dot ${mrOk ? 'green' : 'amber'}"></span>${mrOk ? '正常' : esc(mr.reason || '未运行')}</b><em>${mr.refresh_ms != null ? `刷新 ${mr.refresh_ms}ms · ` : ''}${ago(mr.last_ok)}</em></div>
        <div><span>发送</span><b><span class="dot ${bot.sending_available === false ? 'red' : 'green'}"></span>${sendMode}</b><em>${bot.sending_available === false ? '发送不可用' : a.send_mode === 'direct' ? '微信 ' + esc((a.direct && a.direct.version) || '') + (a.direct && a.direct.attached ? ' · 已连接' : ' · 未连接') : esc(SCOPE[bot.sending_scope] || bot.sending_scope || '')} · <a href="#/a/${id}/send">设置</a></em></div>
        <div><span>回复人设</span><b>${rule ? esc(rule.action.persona || '默认人设') : '没有 AI 回复规则'}</b><em>群聊自动回复 ${R.group_auto_reply ? '开' : '关'} · <a href="#/a/${id}/persona">人设</a></em></div>
        <div><span>规则</span><b>${(R.watch || []).length} 监听 · ${(R.admins || []).length} 管理员</b><em>跟发 ${(follow.groups || []).length} 群 · 推送 ${push.enabled ? (push.targets || []).length + ' 个目标' : '关'}</em></div>`;
    };
    let logSig = '';
    const drawLog = () => {
      const k = $('#logQ').value.trim();
      const lines = (bot.log || []).filter((l) => !k || l.includes(k));
      const sig = k + '\u0001' + lines.join('\n');
      $('#logTime').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', {hour12: false});
      if (sig === logSig) return; logSig = sig;
      const box = $('#log');
      box.innerHTML = lines.map((l) => {
        const cls = /AI回复|已发送|submitted|已确认/.test(l) && !/'ok': False|not_sent/.test(l) ? 'ok'
          : /失败|错误|Traceback|异常|Exception|not_sent|uncertain|'ok': False|^\s*error:|Error:/.test(l) ? 'err'
            : /跳过|skipped/.test(l) ? 'dim' : /^\s/.test(l) ? 'sub' : '';
        return `<div class="${cls}">${esc(l)}</div>`;
      }).join('') || `<div class="dim">${k ? '没有匹配的日志' : '还没有日志'}</div>`;
      if ($('#logFollow').checked) box.scrollTop = box.scrollHeight;
    };
    drawStatus(); drawLog();
    const poll = async () => {
      try { const b = await A(id, '/api/bot'); bot = b; Object.assign(R, b.rules || {}); drawStatus(); drawLog(); } catch (e) { /* 下次再试 */ }
    };
    const timer = setInterval(poll, 4000); onLeave(() => clearInterval(timer));
    $('#logQ').oninput = drawLog;
    $('#logCopy').onclick = async () => { try { await navigator.clipboard.writeText((bot.log || []).join('\n')); toast('已复制'); } catch (e) { toast('复制失败', 'err'); } };
    $('#botSw').onchange = async (e) => {
      const on = e.target.checked;
      if (!on && !await modal({title: '停止机器人？', body: '停止后不会再自动回复任何消息（定时任务照常执行），之后可以随时再打开。', ok: '停止', danger: true})) { e.target.checked = true; return; }
      try { await A(id, on ? '/api/bot/start' : '/api/bot/stop', {method: 'POST'}); toast(on ? '机器人已启动' : '机器人已停止'); await poll(); loadAccounts(true); }
      catch (x) { toast(x.message, 'err'); e.target.checked = !on; }
    };

    // ---------- 分页 ----------
    const showTab = (k) => {
      lastTab = k;
      $$('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === k));
      $$('[data-pane]', view).forEach((p) => { p.hidden = p.dataset.pane !== k; });
    };
    $$('#tabs button').forEach((b) => b.onclick = () => showTab(b.dataset.tab));
    showTab(lastTab);
    const onDirty = (key) => () => markDirty(view, key);
    // 后端每个保存接口都是「读整个 bot_rules.json → 改一项 → 整个写回」且没有加锁,并发两个保存会把文件写坏。
    // 所以本页所有写规则的请求排队,一个完成再发下一个。
    let chain = Promise.resolve();
    const serial = (fn) => { const p = chain.then(fn); chain = p.catch(() => {}); return p; };
    const save = (key, fn) => { const b = $(`[data-save="${key}"]`, view); b.onclick = () => busy(b, () => serial(async () => { await fn(); markDirty(view, key, false); toast('已保存'); poll(); })); };

    // 监听
    const wl = checklist($('#wList'), id, [ALL_GROUPS, ...pGroups(), ...pPeople()], R.watch || [], {onChange: onDirty('watch')});
    $('#wSelf').onchange = onDirty('watch');
    save('watch', async () => {
      const r = await A(id, '/api/bot/watch', {method: 'POST', json: {watch: wl.get(), include_self: $('#wSelf').checked}});
      R.watch = r.watch; R.include_self = r.include_self;
    });

    // 主动
    ['pOn', 'pPriv', 'pGroup', 'pGroupAfter', 'pQs', 'pQe', 'pAfter', 'pGap', 'pMax'].forEach((k) => { $('#' + k).addEventListener('input', onDirty('proactive')); $('#' + k).addEventListener('change', onDirty('proactive')); });
    save('proactive', async () => {
      const hr = (k, def) => { const v = parseFloat($('#' + k).value); return Math.round((isFinite(v) && v > 0 ? v : def) * 3600); };
      const clampH = (k, def) => { const v = parseInt($('#' + k).value, 10); return isFinite(v) ? Math.min(23, Math.max(0, v)) : def; };
      const body = {enabled: $('#pOn').checked, private_share_enabled: $('#pPriv').checked, group_enabled: $('#pGroup').checked,
        group_after: Math.max(1, parseInt($('#pGroupAfter').value, 10) || 5) * 60, quiet_start: clampH('pQs', 22), quiet_end: clampH('pQe', 8)};
      // 高级项只在改过或原来就有时才提交,避免把默认值固化进配置
      const adv = {after: hr('pAfter', 2), gap: hr('pGap', 4), max: Math.max(0, parseInt($('#pMax').value, 10) || 0)};
      const def = {after: 7200, gap: 14400, max: 2};
      for (const k of Object.keys(adv)) if (pc[k] != null || adv[k] !== def[k]) body[k] = adv[k];
      const r = await A(id, '/api/bot/proactive', {method: 'POST', json: body});
      Object.assign(pc, r.proactive || body); R.proactive = pc;
    });

    // 定时问候
    const drawGreets = () => {
      const tasks = greet.tasks || [];
      $('#gCount').textContent = `${tasks.length} 条`;
      $('#gList').innerHTML = tasks.map((t) => {
        const iv = CRON2INT[t.cron || ''];
        return `<div class="bot-greet ${t.enabled === false ? 'off' : ''}">${img(id, t.target_username || t.target)}
          <div class="grow"><div class="n ellipsis">${esc(t.target_display || t.target || '')}${t.enabled === false ? ' <span class="badge">已停用</span>' : ''}</div>
          <div class="s">${esc(iv ? INTERVALS[iv] : (t.schedule_desc || t.cron || ''))} · ${t.quiet ? `免打扰 ${esc(t.quiet.start)}–${esc(t.quiet.end)}` : '不设免打扰'}${t.persona ? ' · 人设 ' + esc(t.persona) : ''}</div>
          <div class="s faint">下次 ${esc(t.next || '—')}${t.last_fired ? ' · 上次 ' + esc(t.last_fired.slice(5)) : ''}</div></div>
          <div class="row"><button class="btn sm" data-g-edit="${t.id}">修改</button><button class="btn sm ghost" data-g-run="${t.id}" title="立即给对方发一条问候">试发</button><button class="btn sm ghost danger" data-g-del="${t.id}">删除</button></div></div>`;
      }).join('') || '<div class="empty small">还没有定时问候</div>';
      $$('[data-g-edit]', view).forEach((b) => b.onclick = () => editGreet(tasks.find((x) => String(x.id) === b.dataset.gEdit)));
      $$('[data-g-run]', view).forEach((b) => b.onclick = async () => {
        const t = tasks.find((x) => String(x.id) === b.dataset.gRun);
        if (!await modal({title: '现在试发一次？', body: `会立即用人设生成一句问候，<b>真的发给「${esc(t.target_display || t.target)}」</b>。`, ok: '发送'})) return;
        busy(b, async () => { await A(id, `/api/schedule/${t.id}/run`, {method: 'POST'}); toast('已触发，去「消息」里看结果', 'ok'); });
      });
      $$('[data-g-del]', view).forEach((b) => b.onclick = async () => {
        const t = tasks.find((x) => String(x.id) === b.dataset.gDel);
        if (!await modal({title: '删除这条问候？', body: `不再定时问候「${esc(t.target_display || t.target)}」。`, ok: '删除', danger: true})) return;
        busy(b, async () => { await A(id, `/api/schedule/${t.id}/delete`, {method: 'POST'}); toast('已删除'); await reloadGreets(); });
      });
    };
    const reloadGreets = async () => { greet = await A(id, '/api/greeting'); drawGreets(); };
    const editGreet = async (t) => {
      if (!t) return;
      const iv = CRON2INT[t.cron || ''] || '1h';
      const f = await modal({title: `修改问候 · ${t.target_display || t.target}`, ok: '保存',
        extra: `<div class="stack" style="gap:12px">
          <div class="field"><label>频率</label><select class="input" name="interval">${Object.entries(INTERVALS).map(([k, l]) => `<option value="${k}" ${k === iv ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
          <div class="grid cols-2"><div class="field"><label>免打扰开始</label><input class="input" type="time" name="qs" value="${esc((t.quiet && t.quiet.start) || '')}"></div>
          <div class="field"><label>免打扰结束</label><input class="input" type="time" name="qe" value="${esc((t.quiet && t.quiet.end) || '')}"></div></div>
          <div class="faint small">两个时间都清空＝全天都可能发。</div>
          <label class="row" style="cursor:pointer"><input type="checkbox" name="enabled" ${t.enabled === false ? '' : 'checked'}> 启用</label></div>`});
      if (!f) return;
      try {
        await A(id, '/api/greeting', {method: 'POST', json: {id: t.id, interval: f.interval, quiet_start: f.qs, quiet_end: f.qe, enabled: f.enabled}});
        toast('已保存'); await reloadGreets();
      } catch (e) { toast(e.message, 'err'); }
    };
    drawGreets();
    const gt = checklist($('#gTargets'), id, pPeople(), [], {empty: '没有联系人'});
    $('#gCreate').onclick = (e) => busy(e.currentTarget, async () => {
      const targets = gt.get();
      if (!targets.length) { toast('先勾选问候对象', 'err'); return; }
      const r = await A(id, '/api/greeting', {method: 'POST', json: {targets, interval: $('#gInt').value, quiet_start: $('#gQs').value, quiet_end: $('#gQe').value, enabled: $('#gEn').checked}});
      const n = (r.tasks || [r.task]).length;
      toast(`已新建 ${n} 条问候` + ((r.failed || []).length ? `，${r.failed.length} 条失败：${r.failed.map((x) => x.message).join('；')}` : ''), (r.failed || []).length ? 'err' : 'ok');
      gt.set([]); $('#gNewBox').open = false; await reloadGreets();
    });

    // 跟发
    const fl = checklist($('#fList'), id, pGroups(), follow.groups || [], {onChange: onDirty('follow'), empty: '这个微信还没有群'});
    $('#fThr').oninput = onDirty('follow');
    save('follow', async () => {
      const r = await A(id, '/api/bot/follow', {method: 'POST', json: {groups: fl.get(), threshold: Math.max(2, parseInt($('#fThr').value, 10) || 3)}});
      follow = {groups: r.groups, threshold: r.threshold}; $('#fThr').value = r.threshold;
    });

    // 推送
    let targets = (push.targets || []).map((t) => ({...t}));
    const nameList = [...new Set([...people, ...grps].map((x) => x.name))].sort((a, b) => a.localeCompare(b, 'zh'));
    const drawTargets = () => {
      $('#puTargets').innerHTML = (targets.map((t, i) => `<div class="bot-target ${t.enabled === false ? 'off' : ''}">
        <span class="badge ${t.type === 'wechat' ? 'green' : 'blue'}">${t.type === 'wechat' ? '微信' : 'Webhook'}</span>
        ${t.type === 'wechat'
    ? `<input class="input grow" data-t="${i}" data-f="to" list="puNames" value="${esc(t.to || '')}" placeholder="好友或群的显示名"><button class="btn sm ghost bot-pick" data-pick="${i}">选择…</button>`
    : `<input class="input grow mono" data-t="${i}" data-f="url" value="${esc(t.url || '')}" placeholder="https://…"><span class="bot-pick-sp"></span>`}
        <label class="switch" title="启用"><input type="checkbox" data-t="${i}" data-f="enabled" ${t.enabled === false ? '' : 'checked'}><span></span></label>
        <button class="btn sm ghost danger" data-rm="${i}" title="删除">✕</button></div>`).join('')
        || '<div class="empty small" style="padding:20px">还没有推送目标</div>') + `<datalist id="puNames">${nameList.map((n) => `<option value="${esc(n)}">`).join('')}</datalist>`;
    };
    drawTargets();
    $('#puTargets').addEventListener('input', (e) => {
      const el = e.target; if (!el.dataset.t) return; const t = targets[+el.dataset.t];
      if (el.dataset.f === 'enabled') { t.enabled = el.checked; el.closest('.bot-target').classList.toggle('off', !el.checked); }
      else { t[el.dataset.f] = el.value; if (el.dataset.f === 'to') delete t.username; }
      markDirty(view, 'push');
    });
    $('#puTargets').addEventListener('click', async (e) => {
      const rm = e.target.closest('[data-rm]'), pk = e.target.closest('[data-pick]');
      if (rm) { targets.splice(+rm.dataset.rm, 1); drawTargets(); markDirty(view, 'push'); }
      if (pk) {
        const c = await pickChat(id, {title: '推送给谁'}); if (!c) return;
        Object.assign(targets[+pk.dataset.pick], {to: c.name, username: c.username}); drawTargets(); markDirty(view, 'push');
      }
    });
    $('#puAddWx').onclick = () => { targets.push({type: 'wechat', to: '', enabled: true}); drawTargets(); markDirty(view, 'push'); };
    ['puOn', 'puSelf'].forEach((k) => { $('#' + k).onchange = onDirty('push'); });
    const src = checklist($('#puSrc'), id, [ALL_GROUPS, ...pGroups(), ...pPeople()], push.sources || [], {onChange: onDirty('push')});
    save('push', async () => {
      const r = await A(id, '/api/bot/push', {method: 'POST', json: {enabled: $('#puOn').checked, include_self: $('#puSelf').checked, sources: src.get(), targets}});
      push = r.push; targets = (push.targets || []).map((t) => ({...t})); drawTargets();
    });
    $('#puTest').onclick = async (e) => {
      const on = targets.filter((t) => t.enabled !== false && (t.to || t.url));
      if (!on.length) { toast('先添加并启用至少一个目标', 'err'); return; }
      const list = on.map((t) => `<li>${t.type === 'wechat' ? '微信 · ' + esc(t.to) : 'Webhook · ' + esc(t.url)}</li>`).join('');
      if (!await modal({title: '发一条测试推送？', body: `会给这些目标<b>真的发</b>一条「【wxbot 推送测试】这是一条测试消息」：<ul class="bot-ul">${list}</ul>`, ok: '发送'})) return;
      busy(e.currentTarget, async () => {
        const r = await A(id, '/api/bot/push/test', {method: 'POST', json: {targets}});
        await modal({title: '测试已发出', body: `<pre class="bot-pre">${esc((r.log || []).join('\n') || '没有日志')}</pre>`, cancel: ''});
      });
    };

    // 管理员
    const al = checklist($('#aList'), id, pPeople(), R.admins || [], {onChange: onDirty('admins'), empty: '没有联系人'});
    save('admins', async () => {
      const want = al.get();
      const r = await A(id, '/api/bot/admins', {method: 'POST', json: {admins: want}});
      R.admins = r.admins; al.set(r.admins);
      const lost = want.filter((u) => !r.admins.includes(u));
      if (lost.length) toast(`${lost.map(nm).join('、')} 不是这个微信的联系人，没有加入`, 'err');
    });

    // 上下文与画像
    const resetCtx = async () => {
      const c = await pickChat(id, {title: '清理哪个会话的上下文'}); if (!c) return;
      if (!await modal({title: `清理「${c.name}」的上下文？`, body: '之后只按当前人设和这之后的新消息回复，旧对话不再当记忆；排队中还没发的回复会被丢掉。聊天记录不会删除。', ok: '清理', danger: true})) return;
      try { await A(id, '/api/bot/reset_context', {method: 'POST', json: {chat: c.username}}); toast('已清理，下一句起重新开始', 'ok'); }
      catch (e) { toast(e.message, 'err'); }
    };
    $('#resetBtn').onclick = resetCtx; $('#resetTop').onclick = resetCtx;
    $('#learnOn').onchange = async (e) => {
      try { const r = await serial(() => A(id, '/api/bot/learn_profiles', {method: 'POST', json: {enabled: e.target.checked}})); R.learn_profiles = r.enabled; toast(r.enabled ? '已开启自动抽画像' : '已关闭自动抽画像'); }
      catch (x) { toast(x.message, 'err'); e.target.checked = !e.target.checked; }
    };
    $('#extractBtn').onclick = (e) => {
      const btn = e.currentTarget;
      (async () => {
        const c = await pickChat(id, {title: '从哪个会话抽取画像'}); if (!c) return;
        busy(btn, async () => {
          const r = await A(id, '/api/profiles/extract', {method: 'POST', json: {chat: c.username}, headers: {'X-Wxbot-Local-Admin': '1'}});
          toast(`已抽取，更新了 ${r.updated || 0} 人的画像`, 'ok');
        });
      })();
    };

    // 打招呼(立即)
    $('#greetNow').onclick = async () => {
      const c = await pickChat(id, {title: '跟谁打招呼'}); if (!c) return;
      const f = await modal({title: `跟「${c.name}」打招呼`, ok: '生成并发送',
        body: '<div class="small" style="margin-bottom:10px">按这个会话最近的上下文和人设生成一句话，<b>立即发给对方</b>。</div>',
        extra: `<input class="input" name="hint" placeholder="可空：不填＝自动打招呼；填了＝当作你想说的意思再润色发出" style="width:100%">
          <div class="faint small" style="margin-top:8px">写「继续 / 你来弄 / 多发几次」默认连发 3 条；写了次数按次数，最多 10 条。</div>`});
      if (!f) return;
      toast(f.hint ? '按你写的内容生成中…' : 'AI 生成打招呼并发送中…');
      try {
        const r = await A(id, '/api/greet', {method: 'POST', json: {chat: c.username, hint: (f.hint || '').trim()}});
        toast((r.verified ? '已确认发送：' : '已提交微信：') + (r.message || '').slice(0, 40), 'ok');
      } catch (e) { toast('没发出：' + e.message, 'err'); }
    };
  };
})();
