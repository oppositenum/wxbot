// AI 语音通话(实验):拨号、实时状态、名单与自动接听、定时电话、通话记录
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct, onLeave, pickChat, fmtTime} = WX;
  const PHASE = {preparing: '准备中（生成开场白）', dialing: '正在打开聊天并拨号', ringing: '响铃中，等对方接', answering: '正在接听',
    connected: '通话中', idle: '空闲'};
  const RESULT = {connected: ['green', '已接通'], no_answer: ['amber', '没人接'], declined: ['amber', '被挂断/拒接'],
    aborted: ['red', '没拨出'], error: ['red', '出错']};
  const TEST = {username: 'wxid_vn2w1t3doieu22', name: '🐮🐮🌱besos'};

  WXPAGES.voicecall = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>语音通话</h1><span class="badge amber">实验</span></div>`,
      `<a class="btn sm" href="#/a/${id}/schedule">定时任务</a>`);
    let st = {busy: false}, cfg = null, calls = [], tasks = [], openId = null, timer = null;
    onLeave(() => clearTimeout(timer));

    const allow = () => (cfg && cfg.allow && cfg.allow.length) ? cfg.allow : [TEST];
    const secs = (n) => n >= 60 ? `${Math.floor(n / 60)}分${Math.round(n % 60)}秒` : `${Math.round(n)}秒`;

    async function loadAll() {
      const [a, b, c, d] = await Promise.all([A(id, '/api/voicecall/status'), A(id, '/api/voicecall/settings'),
        A(id, '/api/voicecall/history'), A(id, '/api/schedule').catch(() => ({tasks: []}))]);
      st = a; cfg = b; calls = c.calls || []; tasks = (d.tasks || []).filter((t) => t.kind === 'call');
      draw();
    }

    async function poll() {
      clearTimeout(timer);
      try {
        const was = st.busy;
        st = await A(id, '/api/voicecall/status');
        if (was && !st.busy) { calls = (await A(id, '/api/voicecall/history')).calls || []; draw(); }
        else drawLive();
      } catch { /* 下次再试 */ }
      timer = setTimeout(poll, st.busy ? 1200 : 5000);
    }

    function liveHtml() {
      if (!st.busy) {
        const l = st.last;
        return `<div class="vc-live idle"><div class="vc-dot"></div><div class="grow"><b>空闲</b>
          <div class="small muted">${l ? `上一通：${esc(l.name || l.chat)} · ${(RESULT[l.result] || ['', l.result])[1]}${l.secs ? ' · ' + secs(l.secs) : ''}` : '还没打过电话'}</div></div></div>`;
      }
      const el = Math.max(0, Date.now() / 1000 - (st.connected_at || st.started));
      const lines = (st.transcript || []).slice(-8).map((x) => `<div class="vc-line ${x.who}"><span>${x.who === 'ai' ? '🤖' : '🗣'}</span>${esc(x.text)}</div>`).join('');
      return `<div class="vc-live on"><div class="vc-dot pulse"></div><div class="grow">
          <div class="row" style="gap:8px"><b>${st.direction === 'in' ? '📥 来电' : '📞 拨给'} ${esc(st.name || st.chat)}</b>
            <span class="badge ${st.phase === 'connected' ? 'green' : 'amber'}">${esc(PHASE[st.phase] || st.phase)}</span>
            ${st.phase === 'connected' ? `<span class="small muted">${secs(el)}</span>` : ''}</div>
          ${st.purpose ? `<div class="small muted">事由：${esc(st.purpose)}</div>` : ''}
          ${lines ? `<div class="vc-transcript">${lines}</div>` : ''}</div>
        <button class="btn danger" id="vcHang">挂断</button></div>`;
    }
    function drawLive() { const b = $('#vcLive', view); if (b) { b.innerHTML = liveHtml(); bindLive(); } }
    function bindLive() {
      const h = $('#vcHang', view);
      if (h) h.onclick = () => busy(h, async () => { await A(id, '/api/voicecall/hangup', {method: 'POST'}); toast('正在挂断'); });
    }

    function draw() {
      const list = allow();
      view.innerHTML = `<div class="page stack vc-page">
        <div class="callout amber"><div>AI 用你给对方配的<b>阿里云复刻音色</b>实时通话：你说完到它开口大约 3～4 秒。
          自动打电话比发消息更容易被微信风控，<b>只打给名单里的熟人</b>，别频繁打。</div></div>
        <div class="card"><div class="card-body" id="vcLive">${liveHtml()}</div></div>

        <div class="grid cols-2">
          <div class="card"><div class="card-head"><h2>打一个</h2></div><div class="card-body stack">
            <div class="field"><label>打给谁</label><select class="input" id="vcWho">${list.map((x) => `<option value="${esc(x.username)}">${esc(x.name || x.username)}</option>`).join('')}</select></div>
            <div class="field"><label>打电话的事由（可不填，不填就是想你了随便聊）</label><input class="input" id="vcWhy" maxlength="200" placeholder="例如：提醒吃药、提醒明天带伞"></div>
            <div class="row"><button class="btn primary" id="vcCall" ${st.busy ? 'disabled' : ''}>📞 拨打</button>
              <span class="small faint">准备开场白要十几秒，然后才响铃</span></div>
          </div></div>

          <div class="card"><div class="card-head"><h2>定时电话</h2><span class="badge">${tasks.length} 个</span></div><div class="card-body stack">
            <div class="small muted">在微信里对机器人说，或者在「定时任务」页用一句话创建：</div>
            <div class="vc-eg">明天早上 8 点给我打电话提醒我吃药</div>
            <div class="vc-eg">每天晚上 10 点打电话提醒蜜蜜早点睡</div>
            <div class="small faint">到点 AI 会打过去说这件事；没人接${cfg.fallback_text ? '会改发一条文字提醒' : '就算了（设置里可改成发文字）'}。</div>
            ${tasks.length ? `<div class="vc-tasks">${tasks.map((t) => `<div class="row" style="gap:8px"><span>📞</span><span class="grow ellipsis">${esc(t.title)} → ${esc(t.target_display || t.target)}</span><span class="small muted">${esc(t.next || t.schedule || t.once_at || t.cron || '')}</span></div>`).join('')}</div>` : ''}
          </div></div>
        </div>

        <div class="card"><div class="card-head"><h2>设置</h2><button class="btn sm primary" id="vcSave">保存设置</button></div><div class="card-body stack">
          <div class="field"><label>可拨打 / 自动接听名单 ${cfg.allow && cfg.allow.length ? '' : '<span class="badge amber">没配置：只允许测试号 🐮🐮🌱besos</span>'}</label>
            <div class="chips" id="vcAllow">${(cfg.allow || []).map((x) => `<span class="badge">${esc(x.name || x.username)} <a href="#" data-rm="${esc(x.username)}">×</a></span>`).join('')}
              <button class="btn sm" id="vcAdd">＋ 添加联系人</button></div></div>
          <div class="setting"><div class="grow"><div class="t">自动接听</div><div class="d">名单里的人打来，AI 自动接起来跟他聊。名单外的人打来不接。</div></div>
            <label class="switch"><input type="checkbox" id="vcAuto" ${cfg.auto_answer ? 'checked' : ''}><span></span></label></div>
          <div class="setting"><div class="grow"><div class="t">允许别人在聊天里让我给他打电话</div><div class="d">打开后，名单外的人在聊天里说「明天 8 点给我打电话」也会创建定时电话（只能打给他自己）。</div></div>
            <label class="switch"><input type="checkbox" id="vcSelf" ${cfg.self_request ? 'checked' : ''}><span></span></label></div>
          <div class="setting"><div class="grow"><div class="t">定时电话没接通时，改发文字提醒</div><div class="d">对方没接、拒接，或者当时正在别的通话里。</div></div>
            <label class="switch"><input type="checkbox" id="vcFb" ${cfg.fallback_text ? 'checked' : ''}><span></span></label></div>
          <div class="grid cols-2">
            <div class="field"><label>每通最长（分钟）</label><input class="input" id="vcMax" type="number" min="1" max="30" value="${cfg.max_minutes}"></div>
            <div class="field"><label>拨号前先发的一句话（对方会话不在最近列表时用，用来把会话顶上来）</label><input class="input" id="vcPre" maxlength="60" value="${esc(cfg.precall_text || '')}"></div>
          </div>
        </div></div>

        <div class="card"><div class="card-head"><h2>通话记录</h2><span class="small faint">最近 50 通</span></div>
          <div class="vc-calls">${calls.length ? calls.map(callRow).join('') : '<div class="empty small">还没有通话</div>'}</div></div>
      </div>`;
      bind();
    }

    function callRow(c) {
      const [color, label] = RESULT[c.result] || ['', c.result];
      return `<div class="vc-call" data-c="${esc(c.id)}"><div class="row" style="gap:10px">
          <span>${c.direction === 'in' ? '📥' : '📞'}</span>
          <div class="grow"><b>${esc(c.name || c.chat)}</b> <span class="small muted">${esc(fmtTime(c.started))}${c.purpose ? ' · ' + esc(c.purpose) : ''}${c.source === 'schedule' ? ' · 定时' : ''}</span>
            ${c.error ? `<div class="small" style="color:var(--red)">${esc(c.error)}</div>` : ''}</div>
          <span class="badge ${color}">${esc(label)}</span><span class="small muted">${c.secs ? secs(c.secs) + ' · ' + c.turns + ' 轮' : ''}</span></div>
        ${openId === c.id ? '<div class="vc-detail" id="vcDetail"><span class="spinner"></span></div>' : ''}</div>`;
    }

    async function showDetail(c) {
      const box = $('#vcDetail', view); if (!box) return;
      const ev = (await A(id, `/api/voicecall/history/${c.id}`)).events || [];
      const turns = ev.filter((e) => e.kind === 'turn');
      const fa = turns.map((t) => t.first_audio).filter((x) => x != null).sort((a, b) => a - b);
      const lines = (c.transcript || []).map((x) => `<div class="vc-line ${x.who}"><span>${x.who === 'ai' ? '🤖' : '🗣'}</span>${esc(x.text)}</div>`).join('');
      box.innerHTML = `${fa.length ? `<div class="small muted">对方说完到 AI 开口：中位 ${fa[Math.floor(fa.length / 2)].toFixed(1)} 秒，最慢 ${fa[fa.length - 1].toFixed(1)} 秒</div>` : ''}
        ${lines ? `<div class="vc-transcript">${lines}</div>` : '<div class="small faint">没有对话内容</div>'}
        ${c.connected ? `<div class="small muted" style="margin-top:8px">对方的声音录音：</div><audio controls preload="none" src="/a/${id}/api/voicecall/history/${encodeURIComponent(c.id)}/audio"></audio>` : ''}`;
    }

    function bind() {
      bindLive();
      $('#vcCall').onclick = (e) => busy(e.currentTarget, async () => {
        const sel = $('#vcWho');
        await A(id, '/api/voicecall/start', {method: 'POST', json: {chat: sel.value, name: sel.selectedOptions[0].textContent, purpose: $('#vcWhy').value.trim()}});
        toast('开始拨号'); st = await A(id, '/api/voicecall/status'); draw(); poll();
      });
      $('#vcAdd').onclick = async () => {
        const c = await pickChat(id, {title: '添加到可拨打名单', groups: false});
        if (!c) return;
        cfg.allow = [...(cfg.allow || []).filter((x) => x.username !== c.username), {username: c.username, name: c.name || c.username}];
        draw(); toast('已加入，记得保存');
      };
      $$('[data-rm]', view).forEach((a) => a.onclick = (e) => { e.preventDefault(); cfg.allow = cfg.allow.filter((x) => x.username !== a.dataset.rm); draw(); toast('已移除，记得保存'); });
      $('#vcSave').onclick = (e) => busy(e.currentTarget, async () => {
        cfg = await A(id, '/api/voicecall/settings', {method: 'POST', json: {allow: cfg.allow || [], auto_answer: $('#vcAuto').checked,
          self_request: $('#vcSelf').checked, fallback_text: $('#vcFb').checked, max_minutes: +$('#vcMax').value || 5, precall_text: $('#vcPre').value}});
        toast('已保存'); draw();
      });
      $$('[data-c]', view).forEach((el) => el.querySelector('.row').onclick = () => {
        openId = openId === el.dataset.c ? null : el.dataset.c; draw();
        const c = calls.find((x) => x.id === openId); if (c) showDetail(c).catch((e) => toast(e.message, 'err'));
      });
    }

    try { await loadAll(); } catch (e) { view.innerHTML = `<div class="page"><div class="callout red">${esc(e.message)}</div></div>`; return; }
    poll();
  };
})();
