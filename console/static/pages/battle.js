/* 战斗模式:吵架/辩论人设、强制红线、专用 Grok 模型、正在战斗的会话。见 console/PAGES.md */
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct, onLeave} = WX;

  WXPAGES.battle = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>战斗模式</h1></div>`);
    view.innerHTML = '<div class="page"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>';
    let d;
    try { d = await A(id, '/api/battle'); }
    catch (e) { view.innerHTML = `<div class="page"><div class="callout red"><div>读取战斗模式失败：${esc(e.message)}</div></div></div>`; return; }

    view.innerHTML = `<div class="page stack bt" style="max-width:860px;gap:16px">
      <div class="card bt-hero"><div class="card-body row" style="gap:16px;align-items:flex-start">
        <div class="bt-ico">⚔️</div>
        <div class="grow stack" style="gap:6px">
          <div style="font-weight:600;font-size:15px">吵架 / 辩论模式</div>
          <div class="muted small">对某个会话开启后，对方的<b>每一条消息</b>都会被自动逐条回击、据理力争——<b>不用 @、不用引用</b>也回。只在观点上交锋，有强制红线兜底，不会写成长篇大论。</div>
          <div class="muted small">开启：在微信里给机器人发 <code>/开启战斗模式</code>（当前会话）或 <code>/开启战斗模式 群名或联系人</code>；收兵：发 <code>/关闭战斗模式</code>，或在下面点「收兵」。与「持续模式」互斥。</div>
        </div></div></div>

      <div class="card"><div class="card-head"><h2>正在战斗的会话 <span class="faint small" id="bt-n"></span></h2><button class="btn sm ghost" id="bt-refresh">刷新</button></div>
        <div id="bt-chats"></div></div>

      <div class="card"><div class="card-head"><h2>吵架人设</h2><span id="bt-tag"></span></div>
        <div class="card-body stack" style="gap:12px">
          <div class="muted small">决定吵架时的<b>说话风格</b>（语气、套路）。系统会强制短句连发：每条很短，多说几句会拆成多条微信发出。恢复默认 = 用内置人设。</div>
          <textarea class="input" id="bt-persona" rows="7" placeholder="例如：语气强硬犀利，逐点戳穿对方逻辑漏洞，多用反问，绝不认输，但只讲道理不骂人…"></textarea>
          <div class="row"><button class="btn primary" id="bt-save">保存人设</button><button class="btn" id="bt-reset">恢复默认</button>
            <span class="grow"></span><details class="bt-def"><summary class="small faint">查看默认人设</summary><pre>${esc(d.default_persona || '')}</pre></details></div>
          <div class="callout red bt-red"><div><b>强制红线（人设改不掉）</b><pre id="bt-redline"></pre></div></div>
        </div></div>

      <div class="card"><div class="card-head"><h2>战斗专用模型</h2><span class="faint small">只在战斗模式生效，日常聊天仍用「AI 模型」里的主模型</span></div>
        <div class="card-body stack" style="gap:12px">
          <div class="grid cols-2" style="gap:12px">
            <div class="field"><label>Grok 模型 ID</label><input class="input" id="bt-model" placeholder="留空 = 跟随主用 Grok（当前 ${esc(d.grok_main_model || '未配置')}）"></div>
            <div class="field"><label>推理强度（reasoning）</label><select class="input" id="bt-reason"></select></div>
          </div>
          <div class="faint small">填了模型后，战斗时固定走 Grok 且只尝试一次（不回退 GPT），保证回击够快。例：<code>grok-4.5</code>、<code>grok-4.20-0309-non-reasoning</code>。</div>
          <div class="row"><button class="btn primary" id="bt-save-model">保存模型</button></div>
        </div></div>
    </div>`;

    const drawPersona = () => {
      $('#bt-persona').value = d.persona || '';
      $('#bt-tag').innerHTML = d.is_custom ? '<span class="badge blue">已自定义</span>' : '<span class="badge">默认人设</span>';
      $('#bt-reset').disabled = !d.is_custom;
    };
    const drawModel = () => {
      $('#bt-model').value = d.grok_model || '';
      $('#bt-reason').innerHTML = '<option value="">跟随主配置</option>' + (d.reasoning_efforts || []).map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
      $('#bt-reason').value = d.grok_reasoning_effort || '';
    };
    const drawChats = () => {
      const cs = d.chats || [];
      $('#bt-n').textContent = cs.length ? `${cs.length} 个` : '';
      $('#bt-chats').innerHTML = cs.length ? cs.map((c) => `<div class="bt-chat">
          <img class="avatar sm" src="/a/${id}/api/avatar?username=${encodeURIComponent(c.username)}" onerror="this.style.visibility='hidden'">
          <div class="grow" style="min-width:0"><div class="ellipsis" style="font-weight:500">${esc(c.name || c.username)}</div><div class="faint small mono ellipsis">${esc(c.username)}</div></div>
          <span class="badge red"><span class="dot red pulse"></span>战斗中</span>
          <button class="btn sm" data-off="${esc(c.username)}">收兵</button></div>`).join('')
        : '<div class="empty" style="padding:32px"><div class="big">🕊️</div>当前没有会话在战斗模式</div>';
      $$('[data-off]', view).forEach((b) => b.onclick = async () => {
        const c = cs.find((x) => x.username === b.dataset.off);
        if (!await modal({title: '收兵', body: `停止对「${esc(c ? c.name || c.username : b.dataset.off)}」的自动回击？之后要 @ 才会回。`, ok: '收兵'})) return;
        busy(b, async () => { await A(id, '/api/battle/off', {method: 'POST', json: {chat: b.dataset.off}}); toast('已收兵', 'ok'); await reload(); });
      });
    };
    const reload = async () => { d = await A(id, '/api/battle'); drawChats(); };
    $('#bt-redline').textContent = (d.red_line || '').replace(/^【[^】]*】\s*/, '');
    drawPersona(); drawModel(); drawChats();

    $('#bt-save').onclick = (e) => busy(e.currentTarget, async () => {
      let text = $('#bt-persona').value.trim();
      if (text === (d.default_persona || '').trim()) text = '';   // 和默认一样就不存成"自定义"
      const r = await A(id, '/api/battle/persona', {method: 'POST', json: {persona: text}});
      d.persona = r.persona; d.is_custom = r.is_custom; drawPersona(); toast('已保存', 'ok');
    });
    $('#bt-reset').onclick = async (e) => {
      const btn = e.currentTarget;
      if (!await modal({title: '恢复默认人设', body: '自定义的吵架人设会被清空，改用内置默认人设。', ok: '恢复默认', danger: true})) return;
      busy(btn, async () => {
        const r = await A(id, '/api/battle/persona', {method: 'POST', json: {persona: ''}});
        d.persona = r.persona; d.is_custom = r.is_custom; drawPersona(); toast('已恢复默认', 'ok');
      });
    };
    $('#bt-save-model').onclick = (e) => busy(e.currentTarget, async () => {
      const r = await A(id, '/api/battle/model', {method: 'POST', json: {grok_model: $('#bt-model').value.trim(), grok_reasoning_effort: $('#bt-reason').value}});
      d.grok_model = r.grok_model; d.grok_reasoning_effort = r.grok_reasoning_effort; drawModel(); toast('已保存', 'ok');
    });
    $('#bt-refresh').onclick = (e) => busy(e.currentTarget, reload);
    const t = setInterval(() => reload().catch(() => {}), 15000);
    onLeave(() => clearInterval(t));
  };
})();
