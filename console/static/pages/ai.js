/* AI 模型与知识库:模型路由 / 三套中转 / 生图 / 连接测试 / Agent 工具 / 路由记录 / 知识库(RAG)。
 * 接口:/api/llm /api/llm/config /api/llm/test /api/agent/config /api/kb /api/kb/add /api/kb/delete /api/kb/search
 * 知识库接口需要管理员授权:本机部署免授权;否则要 X-Wxbot-Admin-Token,凭据只存在页面内存(window.WXADMIN),刷新即失效。 */
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct} = WX;
  const PROV = {claude: 'Claude', gpt: 'GPT', grok: 'Grok'};
  const TOOL = {
    web_search: ['联网搜索', '查新闻、天气、实时信息'],
    search_history: ['查聊天记录', '翻当前会话以前说过的话'],
    get_member_profile: ['查人物画像', '读取对方的长期记忆事实'],
    search_kb: ['查知识库', '从下方知识库检索资料'],
    draw_image: ['画图', '按要求生成图片并发出'],
  };
  const PHASE = {chat: '聊天', tools_round: '工具轮', tools_final: '工具最终回复', tools_reply: '工具后回复'};
  const QUALITY = {low: 'low(更快)', standard: 'standard', medium: 'medium', auto: 'auto'};

  // 管理员凭据:只放内存,按账号区分;两个页面(ai / persona)共用
  window.WXADMIN = window.WXADMIN || {};
  const PA = (id, path, opt = {}) => A(id, path, Object.assign({}, opt, {
    headers: Object.assign({'X-Wxbot-Local-Admin': '1'}, window.WXADMIN[id] ? {'X-Wxbot-Admin-Token': window.WXADMIN[id]} : {}, opt.headers || {})}));

  const provOpts = (cur, follow) => (follow ? `<option value="">${esc(follow)}</option>` : '') +
    Object.entries(PROV).map(([k, v]) => `<option value="${k}" ${cur === k ? 'selected' : ''}>${v}</option>`).join('');

  WXPAGES.ai = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>AI 模型与知识库</h1></div>`,
      `<button class="btn" id="aiTest">测试连接</button><button class="btn primary" id="aiSave">保存模型配置</button>`);
    view.innerHTML = '<div class="page"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>';
    let llm, ag;
    try { [llm, ag] = await Promise.all([A(id, '/api/llm'), A(id, '/api/agent/config').catch(() => null)]); }
    catch (e) { view.innerHTML = `<div class="page"><div class="callout red">读取 AI 配置失败：${esc(e.message)}</div></div>`; return; }
    const efforts = llm.reasoning_efforts || ['none', 'low', 'medium', 'high', 'xhigh'];
    const hasToolRoute = !!llm.tool_route;
    const img = llm.image || {};

    const provCard = (p) => {
      const c = llm[p] || {};
      const reason = p === 'claude' ? '' : `<div class="field"><label>reasoning</label><select class="input" data-p="${p}" data-f="reason">
        ${efforts.map((v) => `<option value="${v}" ${(llm[p + '_reasoning_effort'] || 'low') === v ? 'selected' : ''}>${v}</option>`).join('')}</select>
        <div class="faint small">${p === 'gpt' ? '越低越快；none 表示不发送该参数' : 'grok-4.5 用 low 明显比 high 快'}</div></div>`;
      return `<div class="ai-prov ${llm.provider === p ? 'on' : ''}" data-box="${p}">
        <div class="row"><b class="grow">${PROV[p]} 中转</b>${llm.provider === p ? '<span class="badge green">主用</span>' : ''}
          ${c.has_key ? '<span class="badge green">已存密钥</span>' : '<span class="badge">无密钥</span>'}</div>
        <div class="field"><label>Base URL</label><input class="input" data-p="${p}" data-f="base_url" value="${esc(c.base_url || '')}" placeholder="${p === 'grok' ? 'https://api.x.ai/v1' : 'https://…'}"></div>
        <div class="field"><label>API Key</label><input class="input" type="password" autocomplete="new-password" data-p="${p}" data-f="api_key" placeholder="${c.has_key ? '已保存（不显示），留空不改' : '填入密钥'}"></div>
        <div class="field"><label>模型</label><input class="input" data-p="${p}" data-f="model" value="${esc(c.model || '')}" placeholder="${{claude: 'claude-sonnet-5', gpt: 'gpt-6-astra', grok: 'grok-4.5'}[p]}"></div>
        ${reason}</div>`;
    };
    const tr = llm.tool_route || {};
    const stat = (l, v, sub) => `<div class="card stat"><div class="l">${l}</div><div class="ai-stat-v ellipsis" title="${esc(v)}">${esc(v)}</div><div class="faint small ellipsis">${sub}</div></div>`;

    view.innerHTML = `<div class="page stack" style="gap:16px">
      <div class="grid cols-4">
        ${stat('聊天（主用）', `${PROV[llm.provider] || llm.provider} · ${(llm[llm.provider] || {}).model || '?'}`, llm.configured ? '<span class="badge green">已配置</span>' : '<span class="badge amber">未配置</span>')}
        ${stat('工具调用', hasToolRoute ? `${PROV[tr.provider] || tr.provider} · ${tr.model || '?'}` : '跟随主用', hasToolRoute ? (tr.independent ? '独立路由' : '跟随主用') + (tr.adapter_supports_tools === false ? ' · <span class="badge amber">适配器不支持工具</span>' : '') : '')}
        ${stat('视觉（看图）', llm.vision_provider ? `${PROV[llm.vision_provider]} · ${(llm[llm.vision_provider] || {}).model || '?'}` : '跟随主用', llm.vision_provider ? '独立指定' : '')}
        ${stat('生图', img.model || '未配置', `${img.follow ? '跟随 ' + (PROV[img.follow] || img.follow) : '独立 URL'} · ${esc(img.quality || '')} · ${img.has_key ? '有密钥' : '无密钥'}`)}
      </div>

      <div class="card"><div class="card-head"><h2>模型路由</h2><span class="faint small">聊天、工具、看图分别走哪一套中转</span></div><div class="card-body stack" style="gap:16px">
        <div class="ai-route">
          <div class="field"><label>${hasToolRoute ? '主用（聊天，以及没单独指定的工具/视觉）' : '主用聊天'}</label>
            <div class="seg ai-seg" id="provSeg">${Object.entries(PROV).map(([k, v]) => `<button data-v="${k}" class="${llm.provider === k ? 'on' : ''}">${v}</button>`).join('')}</div></div>
          <div class="field"><label>视觉（看图）</label><select class="input" id="visProv">${provOpts(llm.vision_provider || '', '跟随主用')}</select></div>
        </div>
        ${hasToolRoute ? `<div class="ai-route">
          <div class="field"><label>工具调用 provider</label><select class="input" id="toolsProv">${provOpts(llm.tools_provider || '', '跟随主用 provider')}</select></div>
          <div class="field"><label>工具调用模型</label><input class="input" id="toolsModel" value="${esc(llm.tools_model || '')}" placeholder="留空 = 用该 provider 的模型"></div>
        </div>
        <div class="callout blue"><div>实际工具请求路由：<b>${esc(PROV[tr.provider] || tr.provider || llm.provider)} / ${esc(tr.model || '')}</b>。整轮工具调用和最终回复走同一路由，失败不会偷偷换 provider。这里显示的是请求字段，不代表中转底层真实模型。</div></div>` : ''}
      </div></div>

      <div class="card"><div class="card-head"><h2>中转凭据</h2><span class="faint small">三套独立中转；密钥只写不读，留空保持原值</span></div>
        <div class="card-body"><div class="grid cols-3">${['claude', 'gpt', 'grok'].map(provCard).join('')}</div></div></div>

      <div class="card"><div class="card-head"><h2>生图</h2><span class="faint small">和聊天分开配，可以单独换模型试效果</span></div><div class="card-body">
        <div class="grid cols-3">
          <div class="field"><label>生图中转</label><select class="input" id="imgFollow">
            <option value="grok" ${img.follow === 'grok' ? 'selected' : ''}>跟随 Grok 中转</option>
            <option value="gpt" ${img.follow === 'gpt' ? 'selected' : ''}>跟随 GPT 中转</option>
            <option value="" ${!img.follow ? 'selected' : ''}>独立 URL / Key</option></select></div>
          <div class="field"><label>模型</label><input class="input" id="imgModel" value="${esc(img.model || '')}" placeholder="grok-imagine-image-2.0"></div>
          <div class="field"><label>质量</label><select class="input" id="imgQuality">${Object.entries(QUALITY).map(([k, v]) => `<option value="${k}" ${img.quality === k ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
        </div>
        <div class="grid cols-2 ${img.follow ? 'hidden' : ''}" id="imgOwn" style="margin-top:14px">
          <div class="field"><label>独立 Base URL</label><input class="input" id="imgBase" value="${esc(!img.follow ? img.base_url || '' : '')}" placeholder="https://…"></div>
          <div class="field"><label>独立 API Key</label><input class="input" type="password" autocomplete="new-password" id="imgKey" placeholder="${img.has_key ? '已保存（不显示），留空不改' : '填入密钥'}"></div>
        </div>
        <details class="ai-adv"><summary>高级</summary>
          <div class="field" style="margin-top:10px;max-width:520px"><label>代理</label><input class="input" id="llmProxy" value="${esc(llm.proxy || '')}" placeholder="留空 = 不走代理，例如 http://127.0.0.1:7890"></div>
        </details>
      </div></div>

      <div class="card hidden" id="testCard"><div class="card-head"><h2>连接测试</h2><span class="faint small">每套发一句「OK」再看一张小图，不会给任何人发消息</span></div><div class="card-body" id="testBody"></div></div>

      <div class="grid cols-2">
        <div class="card"><div class="card-head"><h2>Agent 工具</h2>${ag ? `<label class="switch"><input type="checkbox" id="agOn" ${ag.enabled ? 'checked' : ''}><span></span></label>` : ''}</div>
          <div class="card-body">${ag ? `<div class="muted small" style="margin-bottom:6px">开启后机器人回复前可以先调用下面勾选的工具，查到再答。单个联系人可在「人设与画像」里关掉。</div>
            <div id="agTools" class="${ag.enabled ? '' : 'ai-off'}">${ag.all_tools.map((t) => `<div class="setting"><div class="grow"><div class="t">${esc((TOOL[t] || [t])[0])} <span class="mono faint">${esc(t)}</span></div><div class="d">${esc((TOOL[t] || ['', ''])[1])}</div></div>
              <label class="switch"><input type="checkbox" data-tool="${esc(t)}" ${ag.tools.includes(t) ? 'checked' : ''}><span></span></label></div>`).join('')}</div>`
            : '<div class="callout amber">读不到 Agent 配置</div>'}</div></div>
        <div class="card"><div class="card-head"><h2>最近路由记录</h2><div class="row"><span class="badge" id="diagN"></span><button class="btn sm ghost" id="diagRefresh">刷新</button></div></div>
          <div class="ai-diag" id="diag"></div>
          <div class="card-body faint small" style="padding-top:10px">只记录请求阶段和路由（最多 100 条，最新在上），不含网址、密钥或聊天内容；重启后清空。</div></div>
      </div>

      <div class="card" id="kbCard"><div class="card-head"><h2>知识库（RAG）</h2><span class="badge" id="kbN"></span></div><div class="card-body" id="kbBody"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div></div>
    </div>`;

    // ---- 模型路由 ----
    let provider = llm.provider;
    $$('#provSeg button').forEach((b) => b.onclick = () => {
      provider = b.dataset.v; $$('#provSeg button').forEach((x) => x.classList.toggle('on', x === b));
      $$('.ai-prov').forEach((x) => x.classList.toggle('on', x.dataset.box === provider));
    });
    $('#imgFollow').onchange = (e) => $('#imgOwn').classList.toggle('hidden', !!e.target.value);

    $('#aiSave').onclick = (e) => busy(e.currentTarget, async () => {
      const v = (p, f) => { const el = $(`[data-p="${p}"][data-f="${f}"]`); return el ? el.value.trim() : ''; };
      const body = {provider, vision_provider: $('#visProv').value, proxy: $('#llmProxy').value.trim(),
        gpt_reasoning_effort: v('gpt', 'reason') || 'low', grok_reasoning_effort: v('grok', 'reason') || 'low'};
      if (hasToolRoute) { body.tools_provider = $('#toolsProv').value; body.tools_model = $('#toolsModel').value.trim(); }
      for (const p of ['claude', 'gpt', 'grok']) {
        body[p] = {base_url: v(p, 'base_url'), model: v(p, 'model')};
        if (v(p, 'api_key')) body[p].api_key = v(p, 'api_key');
      }
      const follow = $('#imgFollow').value;
      body.image = {follow, model: $('#imgModel').value.trim(), quality: $('#imgQuality').value};
      if (!follow) { body.image.base_url = $('#imgBase').value.trim(); if ($('#imgKey').value.trim()) body.image.api_key = $('#imgKey').value.trim(); }
      const r = await A(id, '/api/llm/config', {method: 'POST', json: body});
      toast(r.configured ? '已保存，AI 可用' : '已保存（还没配置完整）', r.configured ? 'ok' : '');
      WXPAGES.ai(view, id);
    });

    // ---- 连接测试 ----
    $('#aiTest').onclick = (e) => busy(e.currentTarget, async () => {
      $('#testCard').classList.remove('hidden');
      $('#testBody').innerHTML = '<div class="row muted"><span class="spinner"></span>测试中，每套限时，最长约 45 秒…（只测已保存的配置）</div>';
      $('#testCard').scrollIntoView({behavior: 'smooth', block: 'nearest'});
      const ac = new AbortController(); const t = setTimeout(() => ac.abort(), 45000);
      try {
        const r = await A(id, '/api/llm/test', {method: 'POST', signal: ac.signal});
        const cls = (x) => /^✅/.test(x || '') ? 'green' : /未配置|跳过/.test(x || '') ? '' : 'red';
        $('#testBody').innerHTML = `<dl class="kv ai-test">${[['claude', 'Claude'], ['gpt', 'GPT'], ['grok', 'Grok'], ['vision', '视觉']].map(([k, l]) =>
          `<dt>${l}</dt><dd><span class="badge ${cls(r[k])}">${esc(r[k] || '?')}</span></dd>`).join('')}</dl>`;
      } catch (x) {
        $('#testBody').innerHTML = `<div class="callout amber">${x.name === 'AbortError' ? '测试超时。聊天可能仍然可用；Grok 推理较慢，可以把视觉改到 GPT 再测。' : '测试失败：' + esc(x.message)}</div>`;
      } finally { clearTimeout(t); }
    });

    // ---- Agent ----
    if (ag) {
      const saveAg = async () => {
        const tools = $$('[data-tool]').filter((x) => x.checked).map((x) => x.dataset.tool);
        try { await A(id, '/api/agent/config', {method: 'POST', json: {enabled: $('#agOn').checked, tools}}); toast('已保存'); }
        catch (x) { toast(x.message, 'err'); }
      };
      $('#agOn').onchange = () => { $('#agTools').classList.toggle('ai-off', !$('#agOn').checked); saveAg(); }; $$('[data-tool]').forEach((x) => x.onchange = saveAg);
    }

    // ---- 路由记录 ----
    const drawDiag = (list) => {
      list = (list || []).slice().reverse();
      $('#diagN').textContent = `${list.length} 条`;
      $('#diag').innerHTML = list.length ? `<table class="ai-table"><thead><tr><th>#</th><th>阶段</th><th>Provider</th><th>模型</th><th>请求 ID</th></tr></thead><tbody>
        ${list.map((d, i) => `<tr><td class="faint">${i + 1}</td><td><span class="badge ${String(d.phase).startsWith('tools') ? 'blue' : ''}">${esc(PHASE[d.phase] || d.phase)}</span></td>
          <td>${esc(PROV[d.provider] || d.provider)}</td><td class="mono">${esc(d.model)}</td><td class="mono faint" title="${esc(d.request_id)}">${esc(String(d.request_id || '').slice(0, 8))}</td></tr>`).join('')}</tbody></table>`
        : '<div class="empty">这次启动后还没有模型请求</div>';
    };
    drawDiag(llm.route_diagnostics);
    $('#diagRefresh').onclick = (e) => busy(e.currentTarget, async () => drawDiag((await A(id, '/api/llm')).route_diagnostics));

    // ---- 知识库 ----
    async function loadKb() {
      const body = $('#kbBody');
      let kb;
      try { kb = await PA(id, '/api/kb'); }
      catch (e) {
        $('#kbN').textContent = '';
        body.innerHTML = `<div class="callout amber" style="margin-bottom:12px">${esc(e.message)}。知识库管理需要管理员授权：填入后台环境变量 WXBOT_ADMIN_READ_TOKEN 的值，只保存在这个页面的内存里，刷新即失效。</div>
          <div class="row" style="max-width:520px"><input class="input" type="password" autocomplete="off" id="kbTok" placeholder="管理员授权凭据"><button class="btn primary" id="kbUnlock">授权</button></div>`;
        $('#kbUnlock').onclick = () => { window.WXADMIN[id] = $('#kbTok').value; loadKb(); };
        $('#kbTok').onkeydown = (ev) => { if (ev.key === 'Enter') $('#kbUnlock').click(); };
        return;
      }
      $('#kbN').textContent = `共 ${kb.total || 0} 块`;
      const docs = kb.docs || [];
      body.innerHTML = `<div class="ai-kb">
        <div class="stack">
          <div class="muted small">粘贴文档或 FAQ，按段落自动切块。开启 Agent 的「查知识库」后，机器人会先检索这里再回答。</div>
          <textarea class="input" id="kbText" rows="6" placeholder="例如：营业时间是每天 9:00–21:00；退货请在 7 天内……"></textarea>
          <div class="row"><input class="input" id="kbSrc" placeholder="来源标签（可选，如 faq）" style="max-width:240px"><span class="grow"></span><button class="btn primary" id="kbAdd">切块导入</button></div>
          <details class="ai-adv"><summary>检索测试</summary>
            <div class="row" style="margin-top:10px"><input class="input" id="kbQ" placeholder="输入问题，看能检索到哪些块"><button class="btn" id="kbSearch">检索</button></div>
            <div id="kbHits" style="margin-top:8px"></div></details>
        </div>
        <div class="ai-kb-list">${docs.length ? docs.map((d) => `<div class="ai-kb-item">
            <div class="grow"><div class="row"><span class="badge blue">${esc(d.source || '—')}</span>${d.title ? `<b class="ellipsis">${esc(d.title)}</b>` : ''}<span class="faint small">#${d.id}</span></div>
            <div class="small muted ai-kb-prev">${esc(d.preview || '')}</div></div>
            <button class="btn sm ghost danger" data-kbdel="${d.id}">删除</button></div>`).join('') + (kb.total > docs.length ? `<div class="faint small" style="padding:8px 4px">只显示前 ${docs.length} 块</div>` : '')
          : '<div class="empty"><div class="big">📚</div>知识库是空的</div>'}</div></div>`;
      $('#kbAdd').onclick = (e) => busy(e.currentTarget, async () => {
        const content = $('#kbText').value.trim(); if (!content) throw new Error('先粘贴内容');
        const r = await PA(id, '/api/kb/add', {method: 'POST', json: {content, source: $('#kbSrc').value.trim() || 'import', chunk: true}});
        toast(`已导入 ${r.chunks || 1} 块`, 'ok'); loadKb();
      });
      $('#kbSearch').onclick = (e) => busy(e.currentTarget, async () => {
        const q = $('#kbQ').value.trim(); if (!q) return;
        const r = await PA(id, `/api/kb/search?q=${encodeURIComponent(q)}&k=5`);
        const hits = r.hits || [];
        $('#kbHits').innerHTML = hits.length ? hits.map((h) => `<div class="ai-kb-hit"><span class="badge">${esc(h.source || '')}</span> ${esc(h.title || '')}<div class="small muted">${esc(String(h.content || h.preview || h.text || '').slice(0, 200))}</div></div>`).join('')
          : '<div class="faint small">没有检索到</div>';
      });
      $$('[data-kbdel]', body).forEach((b) => b.onclick = async () => {
        const d = docs.find((x) => String(x.id) === b.dataset.kbdel);
        if (!await modal({title: '删除这一块？', danger: true, ok: '删除', body: `<div class="small" style="white-space:pre-wrap">${esc((d && d.preview) || '')}…</div>`})) return;
        busy(b, async () => { await PA(id, '/api/kb/delete', {method: 'POST', json: {id: +b.dataset.kbdel}}); toast('已删除'); loadKb(); });
      });
    }
    loadKb();
  };
})();
