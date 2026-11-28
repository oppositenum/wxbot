/* 人设与画像:人设库(全局人设/导入/查看/纠正/删除)、按会话的人设与个性化、默认模板与批量构建、联系人画像、蒸馏。
 * 接口:/api/personas* /api/personalization* /api/profiles* /api/bot/learn_profiles /api/distill/*
 * /api/personalization* 和 /api/profiles* 需要管理员授权:本机部署免授权;否则要 X-Wxbot-Admin-Token,只存在页面内存(window.WXADMIN)。 */
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct, pickChat, fmtTime} = WX;
  window.WXADMIN = window.WXADMIN || {};
  const PA = (id, path, opt = {}) => A(id, path, Object.assign({}, opt, {
    headers: Object.assign({'X-Wxbot-Local-Admin': '1'}, window.WXADMIN[id] ? {'X-Wxbot-Admin-Token': window.WXADMIN[id]} : {}, opt.headers || {})}));
  const TABS = [['lib', '人设库'], ['chat', '按会话设置'], ['tpl', '模板与批量'], ['prof', '联系人画像'], ['distill', '蒸馏']];
  const SOURCE = {contact: '联系人指定', global: '继承全局', legacy_global: '继承现有唯一规则人设（待显式迁移）', legacy_rule_conflict: '旧规则冲突：角色随原触发规则变化，尚未迁移',
    fallback: '无效配置后的内置回退', builtin: '内置默认助手'};
  const PREF_SRC = {user_explicit: '明确偏好 · 对方表达', admin_manual: '明确偏好 · 管理员设置', inferred: '暂定推断'};
  const DEFAULTS = {length: '适中', tone: '温和', emoji: '少量', jokes: '接受', advice: '视情况', followup: '少追问', address: '朋友', language: '中文', dislikes: '无'};
  const CAT = {expression: '观察到的表达习惯（不控制回复）', reception: '有依据的接收偏好', strategy: '可尝试回复策略（弱参考）'};
  const SW = [
    ['personalization_enabled', '启用表达风格适配', '按下面的交流偏好调整回复长度、语气、表情等', true],
    ['auto_update', '允许自动更新交流偏好', '对方明确说出长期偏好时自动记下（本地规则，不调用模型）；锁定项不会被覆盖', true],
    ['conversation_control_enabled', '会话结束与不主动联系控制', '识别到聊完了或对方说别主动找时，暂停主动跟进', true],
    ['agent_enabled', '工具 Agent', '可以查资料、画图；关掉后直接用聊天模型回复，不经工具链', false],
    ['skip_closing_replies', '结束语不回复', '「好的 / 嗯 / 收到」这类单条确认不回；关掉后每句都按人设回', false],
    ['shared_sessions', '会话共享', '多个私聊好友都打开、且生效人设相同时，互通记忆与近况；不同人设保持隔离', true],
  ];
  let lastTab = 'lib';
  const dt = (ts) => ts ? new Date(ts * 1000).toLocaleString('zh-CN', {hour12: false}) : '—';
  const isGroup = (u) => String(u || '').endsWith('@chatroom');
  const av = (id, u, cls = 'sm') => `<img class="avatar ${cls}" src="/a/${id}/api/avatar?username=${encodeURIComponent(u)}" onerror="this.style.visibility='hidden'">`;
  const spin = '<div class="empty"><span class="spinner" style="margin:auto"></span></div>';

  WXPAGES.persona = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>人设与画像</h1></div>`);
    view.innerHTML = `<div class="page stack" style="gap:16px">
      <div class="row"><div class="seg" id="psTabs">${TABS.map(([k, l]) => `<button data-t="${k}">${l}</button>`).join('')}</div><span class="grow"></span>
        <span class="faint small">本页不会给任何人发消息；只有「蒸馏」「立刻抽取」「批准模型分析」会调用大模型</span></div>
      <div id="psBody"></div></div>`;
    const body = () => $('#psBody');
    let personas = [], cat = null, catErr = null, curContact = '', detail = null, analysis = null, curPersona = '', curProfile = '';
    let alive = true; WX.onLeave(() => { alive = false; });

    // ---------- 数据 ----------
    const loadPersonas = async () => { personas = await A(id, '/api/personas'); return personas; };
    const loadCat = async () => {
      try { cat = await PA(id, '/api/personalization'); catErr = null; } catch (e) { cat = null; catErr = e; }
      return cat;
    };
    const pz = async (path, data) => {
      if (!cat) throw new Error('管理数据未加载');
      const opt = data === undefined ? {} : {method: 'POST', json: Object.assign({}, data, {session: cat.session})};
      const d = await PA(id, '/api/personalization' + path, opt);
      if (d.session && (d.session.account !== cat.session.account || d.session.generation !== cat.session.generation)) {
        cat = null; detail = null; analysis = null;
        throw new Error('微信账号已变化，请刷新页面重新加载；本次修改未保存');
      }
      return d;
    };
    const cName = (u) => { const c = cat && cat.contacts.find((x) => x.username === u); return c ? (c.remark || c.name || c.nick_name || u) : u; };
    const pName = (slug) => { const p = personas.find((x) => x.slug === slug); return p ? p.name : slug; };
    const globalSlug = () => cat ? (cat.global_config.revision ? cat.global_config.persona_id : cat.legacy.unique) : null;
    const personaOpts = (value, first) => `<option value="">${esc(first)}</option>` + personas.map((p) => `<option value="${esc(p.slug)}" ${p.slug === value ? 'selected' : ''}>${esc(p.name)}</option>`).join('');

    // 需要管理员授权时的提示框
    function authBox(el, retry) {
      el.innerHTML = `<div class="card card-body stack" style="max-width:640px">
        <div class="callout amber"><div>${esc(catErr ? catErr.message : '需要管理员授权')}</div></div>
        <div class="muted small">按会话设置、模板和联系人画像需要独立的管理员授权：填入后台环境变量 WXBOT_ADMIN_READ_TOKEN 的值。凭据只保存在这个页面的内存里，刷新即失效，不会写进地址或本地存储。</div>
        <div class="row"><input class="input" type="password" autocomplete="off" id="psTok" placeholder="管理员授权凭据"><button class="btn primary" id="psUnlock">授权</button></div></div>`;
      $('#psUnlock').onclick = (e) => busy(e.currentTarget, async () => { window.WXADMIN[id] = $('#psTok').value; await loadCat(); retry(); });
      $('#psTok').onkeydown = (e) => { if (e.key === 'Enter') $('#psUnlock').click(); };
    }

    // ---------- 标签切换 ----------
    async function show(tab) {
      lastTab = tab;
      $$('#psTabs button').forEach((b) => b.classList.toggle('on', b.dataset.t === tab));
      body().innerHTML = spin;
      try { await ({lib: tabLib, chat: tabChat, tpl: tabTpl, prof: tabProf, distill: tabDistill})[tab](); }
      catch (e) { if (alive) body().innerHTML = `<div class="callout red"><div>${esc(e.message)}</div></div>`; }
    }
    $$('#psTabs button').forEach((b) => b.onclick = () => show(b.dataset.t));

    // =====================================================================
    // 人设库
    // =====================================================================
    async function tabLib() {
      await Promise.all([loadPersonas(), cat ? null : loadCat()]);
      if (!alive || lastTab !== 'lib') return;
      const g = globalSlug();
      const migrated = cat && cat.global_config.revision;
      const gMissing = cat && g && !personas.some((p) => p.slug === g) ? g : '';
      body().innerHTML = `<div class="stack" style="gap:16px">
        <div class="card"><div class="card-head"><h2>全局人设</h2>${cat ? (migrated ? '<span class="badge green">已迁移到统一配置</span>' : '<span class="badge amber">尚未迁移</span>') : ''}</div>
          <div class="card-body stack">${cat ? `
            <div class="muted small">没给单个联系人指定人设时，机器人用这个。${migrated ? '旧规则不会再覆盖联系人设置。' : '现在沿用旧规则里的角色。第一次保存必须和当前唯一的规则角色一致；规则有冲突时会拒绝自动迁移。'}</div>
            ${gMissing ? `<div class="callout red"><div>全局/旧规则引用的人设「${esc(gMissing)}」已删除。请另选可用人设或内置默认助手后保存。</div></div>` : ''}
            <div class="row" style="max-width:640px"><select class="input" id="psGlobal">${personaOpts(gMissing ? '' : g, '内置默认助手')}</select><button class="btn primary" id="psGlobalSave">核对并保存</button></div>
            <details class="ps-adv"><summary>旧规则清单${cat.legacy.conflict ? ' <span class="badge red">有冲突</span>' : ''}</summary>
              <table class="ps-table" style="margin-top:8px"><thead><tr><th>#</th><th>规则</th><th>触发</th><th>人设</th></tr></thead><tbody>
              ${(cat.legacy.entries || []).map((e) => `<tr><td>${e.index}</td><td>${esc(e.name || '')}</td><td class="mono">${esc(JSON.stringify(e.match || {}))}</td><td>${e.persona_id ? esc(pName(e.persona_id)) : '<span class="faint">无</span>'}</td></tr>`).join('') || '<tr><td colspan="4" class="faint">没有 AI 回复规则</td></tr>'}
              </tbody></table></details>` : `<div class="callout amber"><div>${esc(catErr ? catErr.message : '')}。设置全局人设需要管理员授权，可在「按会话设置」里授权。</div></div>`}
          </div></div>
        <div class="card"><div class="card-head"><h2>人设库</h2><div class="row"><span class="badge">${personas.length} 个</span><button class="btn sm primary" id="psImpBtn">导入人设</button></div></div>
          <div class="card-body ps-imp hidden" id="psImp">
            <div class="grid cols-2"><div class="field"><label>名字</label><input class="input" id="impName" placeholder="例如：老王"></div><div></div></div>
            <div class="field"><label>人设内容</label><textarea class="input" id="impText" rows="7" placeholder="第二人称描述，例如：你是老王，说话简短，爱用反问……"></textarea></div>
            <div class="field"><label>说话样例（可选，一行一句）</label><textarea class="input" id="impSamples" rows="3" placeholder="真的假的？\n行吧，那你先去"></textarea></div>
            <div class="row"><span class="grow"></span><button class="btn" id="impCancel">取消</button><button class="btn primary" id="impOk">导入</button></div></div>
          <div class="ps-split"><div class="ps-list" id="psList"></div><div class="ps-detail" id="psDetail"></div></div></div></div>`;
      if (cat) $('#psGlobalSave').onclick = (e) => {
        const v = $('#psGlobal').value, btn = e.currentTarget;
        modal({title: '设置全局人设？', ok: '保存', body: `<div>全局人设改为「${esc(v ? pName(v) : '内置默认助手')}」。</div><div class="small" style="margin-top:8px">请先核对上面的旧规则清单。继承全局的联系人会随之变化；单独绑定了人设的联系人不变。</div>`})
          .then((ok) => ok && busy(btn, async () => {
            await pz('/global', {persona_id: v || null, revision: cat.global_config.revision, accept_migration: true});
            toast('已保存', 'ok'); await loadCat(); tabLib();
          }));
      };
      $('#psImpBtn').onclick = () => { $('#psImp').classList.toggle('hidden'); $('#impName').focus(); };
      $('#impCancel').onclick = () => $('#psImp').classList.add('hidden');
      $('#impOk').onclick = (e) => busy(e.currentTarget, async () => {
        const name = $('#impName').value.trim(), persona = $('#impText').value.trim();
        if (!name || !persona) throw new Error('填写名字和人设内容');
        const r = await A(id, '/api/personas/import', {method: 'POST', json: {name, persona, samples: $('#impSamples').value}});
        toast('已导入「' + name + '」', 'ok'); curPersona = r.slug; await loadPersonas(); if (cat) await loadCat(); tabLib();
      });
      const list = () => {
        $('#psList').innerHTML = personas.map((p) => `<div class="chat-item ${p.slug === curPersona ? 'active' : ''}" data-slug="${esc(p.slug)}">
          <div class="avatar sm">${esc((p.name || '?').slice(0, 1))}</div>
          <div class="grow"><div class="row"><span class="n ellipsis grow">${esc(p.name)}</span>${p.slug === g ? '<span class="badge green">全局</span>' : ''}</div>
          <div class="s ellipsis">${esc(p.slug)}${p.count ? ' · ' + p.count + ' 条样本发言' : ''}</div></div></div>`).join('') || '<div class="empty">还没有人设，先蒸馏或导入一个</div>';
        $$('#psList [data-slug]').forEach((el) => el.onclick = () => { curPersona = el.dataset.slug; list(); personaDetail(); });
      };
      if (!curPersona || !personas.some((p) => p.slug === curPersona)) curPersona = (personas[0] || {}).slug || '';
      list(); personaDetail();
    }
    async function personaDetail() {
      const box = $('#psDetail'); if (!box) return;
      if (!curPersona) { box.innerHTML = '<div class="empty">选择左边的人设</div>'; return; }
      box.innerHTML = spin;
      const slug = curPersona;
      let d; try { d = await A(id, '/api/personas/' + encodeURIComponent(slug)); } catch (e) { box.innerHTML = `<div class="callout red"><div>${esc(e.message)}</div></div>`; return; }
      if (slug !== curPersona || !$('#psDetail')) return;
      const st = d.stats || {};
      const src = d.group === 'imported' ? '手动导入' : d.group === '(自己·全部会话)' ? '蒸馏自己' : d.group ? (isGroup(d.group) ? '群聊蒸馏' : '私聊蒸馏') : '—';
      box.innerHTML = `<div class="stack">
        <div class="row"><div class="grow"><h2>${esc(d.name)}</h2><div class="faint small mono">${esc(d.slug)}</div></div>
          <button class="btn sm danger" id="pDel">删除</button></div>
        <div class="chips"><span class="badge">${src}</span>${st.count && d.group !== 'imported' ? `<span class="badge">${st.count} 条发言</span>` : ''}${st.avg_len ? `<span class="badge">平均 ${esc(st.avg_len)} 字</span>` : ''}
          <span class="badge">样例 ${(d.samples || []).length} 条</span>${(d.corrections || []).length ? `<span class="badge amber">已纠正 ${d.corrections.length} 次</span>` : ''}</div>
        ${(st.top_words || []).length ? `<div class="small muted">高频词：${(st.top_words || []).slice(0, 12).map(esc).join('、')}</div>` : ''}
        <pre class="ps-text">${esc(d.persona || '')}</pre>
        ${(d.samples || []).length ? `<details class="ps-adv"><summary>说话样例（${d.samples.length}）</summary><div class="ps-samples">${d.samples.slice(0, 200).map((x) => `<div>${esc(typeof x === 'string' ? x : x.text || JSON.stringify(x))}</div>`).join('')}</div></details>` : ''}
        ${(d.corrections || []).length ? `<div class="small"><b>已有纠正：</b>${d.corrections.map(esc).join('；')}</div>` : ''}
        <div class="field"><label>纠正这个人设</label><div class="row"><input class="input" id="corrIn" placeholder="例如：TA 从不用感叹号"><button class="btn" id="corrBtn">加入纠正</button></div>
          <div class="faint small">把纠正并入人设文本，不用重新蒸馏，也不调用模型</div></div></div>`;
      $('#corrBtn').onclick = (e) => busy(e.currentTarget, async () => {
        const feedback = $('#corrIn').value.trim(); if (!feedback) throw new Error('先写纠正内容');
        await A(id, '/api/personas/' + encodeURIComponent(slug) + '/correct', {method: 'POST', json: {feedback}}); toast('已精修人设', 'ok'); personaDetail();
      });
      $('#pDel').onclick = async () => {
        if (!await modal({title: `删除人设「${d.name}」？`, danger: true, ok: '删除',
          body: '删除后不能恢复。绑定了它的联系人和全局设置会自动解除绑定，改用全局人设或内置默认助手。'})) return;
        busy($('#pDel'), async () => {
          const r = await A(id, '/api/personas/delete', {method: 'POST', json: {slug}});
          if (!r.ok) throw new Error(r.error || '删除失败');
          toast('人设已删除，失效绑定已清除', 'ok'); curPersona = ''; await loadCat(); tabLib();
        });
      };
    }

    // =====================================================================
    // 按会话设置
    // =====================================================================
    async function tabChat() {
      if (!cat) await loadCat();
      if (!alive || lastTab !== 'chat') return;
      if (!cat) return authBox(body(), tabChat);
      if (!personas.length) await loadPersonas();
      body().innerHTML = `<div class="card ps-split ps-split-chat">
        <div class="ps-list-wrap"><div class="ps-search"><input class="input" id="cq" placeholder="搜索联系人或群"><button class="btn" id="cPick" title="从全部联系人/群里选">选择…</button></div>
          <div class="ps-list" id="cList"></div></div>
        <div class="ps-detail" id="cDetail"><div class="empty"><div class="big">🎭</div>选择一个联系人或群，单独设置它的人设和交流偏好</div></div></div>`;
      const draw = () => {
        const q = $('#cq').value.trim().toLowerCase();
        const items = cat.contacts.filter((c) => !q || [c.remark, c.name, c.nick_name, c.username, c.alias].some((x) => String(x || '').toLowerCase().includes(q)) || c.username === curContact);
        $('#cList').innerHTML = items.map((c) => `<div class="chat-item ${c.username === curContact ? 'active' : ''}" data-u="${esc(c.username)}">${av(id, c.username)}
          <div class="grow"><div class="n ellipsis">${esc(c.remark || c.name || c.nick_name || c.username)}</div><div class="s ellipsis">${isGroup(c.username) ? '群 · ' : ''}${esc(c.username)}</div></div></div>`).join('') || '<div class="empty">没有匹配的</div>';
        $$('#cList [data-u]').forEach((el) => el.onclick = () => { curContact = el.dataset.u; draw(); contactDetail(); });
      };
      $('#cq').oninput = draw;
      $('#cPick').onclick = async () => {
        const c = await pickChat(id, {title: '选择要单独设置的联系人或群'}); if (!c) return;
        if (!cat.contacts.some((x) => x.username === c.username)) { toast('这个会话不在当前账号的可管理列表里', 'err'); return; }
        curContact = c.username; $('#cq').value = ''; draw(); contactDetail();
        const el = $(`#cList [data-u="${CSS.escape(c.username)}"]`); if (el) el.scrollIntoView({block: 'nearest'});
      };
      draw(); if (curContact) contactDetail();
    }
    async function contactDetail() {
      const box = $('#cDetail'); if (!box || !curContact) return;
      const u = curContact; box.innerHTML = spin;
      try { detail = await pz('/contact?contact=' + encodeURIComponent(u)); } catch (e) { box.innerHTML = `<div class="callout red"><div>${esc(e.message)}</div></div>`; return; }
      if (u !== curContact || !$('#cDetail')) return;
      if (analysis && analysis.job.contact !== u) analysis = null;
      const c = detail.config, eff = detail.effective, grp = isGroup(u);
      const missing = c.persona_id && !personas.some((p) => p.slug === c.persona_id) ? c.persona_id : '';
      const prefs = c.preferences || {};
      const hasPref = Object.values(prefs).some((p) => p.value !== '未知');
      const tpl = cat.default_template;
      box.innerHTML = `<div class="stack" style="gap:18px">
        <div class="row">${av(id, u, '')}<div class="grow"><h2 class="ellipsis">${esc(cName(u))}</h2><div class="faint small mono">${esc(u)}</div></div>
          ${grp ? '<span class="badge blue">群</span>' : ''}<span class="badge ${eff.error ? 'red' : 'green'}" title="${esc(SOURCE[eff.source] || eff.source)}">生效：${esc(eff.name)}</span></div>
        <div class="callout ${eff.error ? 'red' : 'blue'}"><div>实际生效「${esc(eff.name)}」· ${esc(SOURCE[eff.source] || eff.source)}${eff.error ? ' · ' + esc(eff.error) : ''}${c.inherited_template ? '<br>当前继承默认模板；单独保存后变成独立配置。' : ''}${c.config_error ? '<br>' + esc(c.config_error) : ''}</div></div>
        ${missing ? `<div class="callout red"><div>绑定的人设「${esc(missing)}」已删除。请另选人设，或选「使用全局人设」后保存即可清除。</div></div>` : ''}
        <section><h3 class="ps-h">角色与开关</h3>
          <div class="field" style="max-width:420px;margin-bottom:6px"><label>角色</label><select class="input" id="cPersona">${personaOpts(missing ? '' : c.persona_id, '使用全局人设（默认）')}</select></div>
          ${SW.map(([k, t, d, noGroup]) => {
            const on = k === 'agent_enabled' || k === 'skip_closing_replies' ? c[k] !== false : c[k] === true;
            const dis = grp && noGroup;
            return `<div class="setting ${dis ? 'ps-dis' : ''}"><div class="grow"><div class="t">${t}</div><div class="d">${dis ? '群聊不可用' : d}</div></div><label class="switch"><input type="checkbox" data-sw="${k}" ${on ? 'checked' : ''} ${dis ? 'disabled' : ''}><span></span></label></div>`;
          }).join('')}
          <div class="row" style="margin-top:6px"><button class="btn primary" id="cSave">保存角色与开关</button>
            ${tpl && tpl.revision && !grp ? '<button class="btn" id="cTpl">一键套用默认模板</button>' : ''}<span class="faint small grow">保存后才对这个${grp ? '群' : '联系人'}生效</span></div></section>
        ${grp ? '<div class="callout blue"><div>群聊不加载任何私聊交流画像，只设置群里的角色。</div></div>' : `
        <section><div class="row" style="margin-bottom:6px"><h3 class="ps-h grow" style="margin:0">交流偏好</h3><button class="btn sm danger" id="cClear">清空偏好</button></div>
          <div class="muted small" style="margin-bottom:8px">${hasPref ? '以下是有记录的偏好；没有证据的其他方面仍是未知。' : '未填写的交流偏好使用系统默认值；填写后可随时覆盖。'}锁定项不会被自动更新覆盖；修改都记为管理员手动设置，删除不删审计记录。</div>
          <table class="ps-table"><thead><tr><th style="width:96px">偏好</th><th>值</th><th style="width:60px">锁定</th><th>来源</th><th style="width:120px"></th></tr></thead><tbody>
          ${Object.entries(cat.fields).map(([f, vals]) => {
            const p = prefs[f] || {}; const v = p.value || DEFAULTS[f] || '适中';
            const input = vals ? `<select class="input ps-in" data-pf="${f}">${vals.map((x) => `<option ${x === v ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select>`
              : `<input class="input ps-in" data-pf="${f}" value="${esc(v)}" maxlength="160">`;
            const src = p.source ? `<div>${esc(PREF_SRC[p.source] || p.source)}</div><div class="faint">${dt(p.updated)}${p.source === 'inferred' ? ' · 模型自评 ' + esc(p.confidence) : ''}${(p.evidence_ids || []).length ? ' · 消息 ' + esc(p.evidence_ids.join(', ')) : ''}</div>` : '<span class="faint">系统默认</span>';
            return `<tr><td>${esc(cat.labels[f] || f)}</td><td>${input}</td><td><input type="checkbox" data-lock="${f}" ${p.locked ? 'checked' : ''}></td><td class="small">${src}</td>
              <td><button class="btn sm" data-psave="${f}">保存</button>${p.source ? `<button class="btn sm ghost" data-pdel="${f}">删除</button>` : ''}</td></tr>`;
          }).join('')}</tbody></table></section>`}
        <details class="ps-adv"><summary>修改记录（最近 ${detail.audit.length} 次）</summary>
          <table class="ps-table" style="margin-top:8px"><thead><tr><th>时间</th><th>版本</th><th>来源</th><th>角色</th><th>偏好</th></tr></thead><tbody>
          ${detail.audit.map((a) => `<tr><td class="ps-nw">${dt(a.ts)}</td><td>${a.revision}</td><td class="ps-nw">${esc(PREF_SRC[a.source] || a.source)}</td><td class="ps-nw">${a.data && a.data.persona_id ? esc(pName(a.data.persona_id)) : '<span class="faint">全局</span>'}</td>
            <td class="small">${esc(Object.entries((a.data && a.data.preferences) || {}).map(([k, v]) => (cat.labels[k] || k) + '=' + v.value).join('；')) || '<span class="faint">—</span>'}</td></tr>`).join('') || '<tr><td colspan="5" class="faint">没有记录</td></tr>'}</tbody></table></details>
        <details class="ps-adv"><summary>本地上下文预览（不调用模型）</summary>
          <div class="row" style="margin-top:8px"><input class="input" id="cQuery" placeholder="例如：请详细解释这道题"><button class="btn" id="cPrev">预览</button></div>
          <pre class="ps-pre" id="cPrevOut" style="margin-top:8px">尚未预览</pre></details>
        ${cat.model_drafts && !grp ? draftsHtml() : ''}
      </div>`;
      const save = (patch) => pz('/contact', {contact: u, patch, revision: detail.config.revision});
      $('#cSave').onclick = (e) => busy(e.currentTarget, async () => {
        const patch = {persona_id: $('#cPersona').value || null};
        $$('[data-sw]').forEach((x) => { if (!x.disabled) patch[x.dataset.sw] = x.checked; });
        await save(patch); toast('已保存', 'ok'); contactDetail();
      });
      if ($('#cTpl')) $('#cTpl').onclick = async () => {
        if (!await modal({title: '套用默认模板？', ok: '套用', body: '用默认模板替换这位好友的人设和偏好。原配置保留在修改记录里。'})) return;
        busy($('#cTpl'), async () => { await pz('/template/apply', {contact: u, revision: detail.config.revision, template_revision: cat.default_template.revision}); toast('已套用', 'ok'); contactDetail(); });
      };
      $$('[data-psave]').forEach((b) => b.onclick = () => busy(b, async () => {
        const f = b.dataset.psave;
        await save({preferences: {[f]: {value: $(`[data-pf="${f}"]`).value.trim(), locked: $(`[data-lock="${f}"]`).checked}}}); toast('已保存', 'ok'); contactDetail();
      }));
      $$('[data-pdel]').forEach((b) => b.onclick = () => busy(b, async () => { await save({preferences: {[b.dataset.pdel]: null}}); toast('已删除'); contactDetail(); }));
      if ($('#cClear')) $('#cClear').onclick = async () => {
        if (!await modal({title: '清空交流偏好？', danger: true, ok: '清空', body: '只清空这个联系人的交流偏好；事实记忆和角色不变，修改记录保留。想保持清空，可以同时关掉「允许自动更新」。'})) return;
        busy($('#cClear'), async () => { await save({preferences: Object.fromEntries(Object.keys(detail.config.preferences || {}).map((k) => [k, null]))}); toast('已清空'); contactDetail(); });
      };
      $('#cPrev').onclick = (e) => busy(e.currentTarget, async () => {
        const r = await pz('/preview', {contact: u, query: $('#cQuery').value});
        $('#cPrevOut').textContent = [`角色：${r.role_name}（${SOURCE[r.source] || r.source}）${r.error ? ' · ' + r.error : ''}`, `选中偏好 ${r.preference_count} 项 · 模型调用 ${r.model_calls} 次 · 发送 ${r.sends} 条`, '', ...(r.context || [])].join('\n');
      });
      if ($('#dPrev')) bindDrafts(u);
    }

    // ---- 模型辅助画像草稿 ----
    function draftsHtml() {
      const st = detail.conversation || {}, c = detail.config;
      const today = new Date(), ago = new Date(Date.now() - 30 * 864e5), d2 = (d) => d.toISOString().slice(0, 10);
      return `<section class="ps-drafts"><h3 class="ps-h">模型辅助画像草稿 <span class="badge amber">需逐批批准才调用模型</span></h3>
        <div class="muted small">本地明确偏好规则不调用模型。下面的分析会调用当前配置的模型，结果只是草稿，人工审核前不生效。表达习惯不等于接收偏好，尝试策略也不是确定偏好。</div>
        <div class="small" style="margin:8px 0">主动联系状态：${!c.conversation_control_enabled ? '未启用会话结束控制，沿用原主动跟进规则' : st.no_proactive ? '长期不主动联系（新消息不会撤销）' : st.paused ? '本次交流已结束，暂停主动跟进' : '允许按现有规则主动跟进'}。普通回复和定时任务不受影响。</div>
        <div class="ps-form">
          <div class="field"><label>开始</label><input class="input" type="date" id="aStart" value="${d2(ago)}"></div>
          <div class="field"><label>结束（含当日）</label><input class="input" type="date" id="aEnd" value="${d2(today)}"></div>
          <div class="field"><label>时间片段</label><input class="input" type="number" id="aSeg" min="2" max="12" value="6"></div>
          <div class="field"><label>最多调用次数</label><input class="input" type="number" id="aCalls" min="1" max="6" value="3"></div>
          <div class="field"><label>token 估算预算</label><input class="input" type="number" id="aBudget" min="2000" max="180000" value="60000"></div>
        </div>
        <div class="row" style="margin-top:8px"><button class="btn" id="dPrev">预览样本（不调用模型）</button><span class="faint small grow">每个片段最多取三条相邻的对方文本；不提供机器人旧消息。token 是保守估算，不是实际账单。</span></div>
        ${(detail.analysis_jobs || []).length ? `<div class="chips" style="margin-top:10px">${detail.analysis_jobs.map((j) => `<button class="btn sm" data-aj="${esc(j.id)}">分析 ${esc(j.id.slice(0, 8))} · ${esc(j.status || '')} · 已用 ${j.calls_reserved}/${j.max_calls} 次</button>`).join('')}</div>` : ''}
        <div id="aView"></div></section>`;
    }
    function bindDrafts(u) {
      $('#dPrev').onclick = (e) => busy(e.currentTarget, async () => {
        const start = new Date($('#aStart').value + 'T00:00:00').getTime() / 1000;
        const end = new Date($('#aEnd').value + 'T00:00:00'); end.setDate(end.getDate() + 1);
        if (!Number.isFinite(start) || !Number.isFinite(end.getTime())) throw new Error('请选择有效日期范围');
        analysis = await pz('/analysis/preview', {contact: u, start, end: end.getTime() / 1000, segments: +$('#aSeg').value, max_calls: +$('#aCalls').value, token_budget: +$('#aBudget').value});
        await contactDetail();
      });
      $$('[data-aj]').forEach((b) => b.onclick = () => busy(b, () => loadAnalysis(b.dataset.aj)));
      if (analysis && analysis.job.contact === u) renderAnalysis();
    }
    async function loadAnalysis(jid) {
      const d = await pz('/analysis/detail?id=' + encodeURIComponent(jid));
      if (d.job.contact !== curContact) throw new Error('联系人已变化，请重新选择分析任务');
      analysis = d; renderAnalysis();
    }
    function renderAnalysis() {
      const box = $('#aView'); if (!box || !analysis) return;
      const {job, batches, items} = analysis;
      const rows = batches.flatMap((b) => b.rows || []);
      box.innerHTML = `<div class="ps-ana">
        <div class="row"><b class="grow">分析任务 ${esc(job.id.slice(0, 8))}</b><span class="badge">${esc(job.status || '')}</span></div>
        <dl class="kv small" style="margin:10px 0">
          <dt>时间范围</dt><dd>${dt(job.start)} — ${dt(job.end)}</dd>
          <dt>样本</dt><dd>对方消息 ${job.message_count} 条 · 片段 ${job.sampled_segments}/${job.segments}</dd>
          <dt>调用</dt><dd>预计首次 ${job.expected_calls} 次 · 已预留 ${job.calls_reserved}/${job.max_calls} 次 · 单次输出上限 ${job.output_token_limit || 1800}</dd>
          <dt>token</dt><dd>预计输入 ${job.input_token_estimate} · 已预留 ${job.tokens_reserved} / 上限 ${job.token_budget}（${esc(job.token_estimation || '估算')}）</dd>
          <dt>路由</dt><dd class="mono">${esc(typeof job.route === 'object' ? JSON.stringify(job.route) : job.route || '')}</dd>
          <dt>费用</dt><dd>价格未确定，不估算金额</dd></dl>
        ${batches.length ? '' : '<div class="callout amber"><div>资料不足：没有可分析的样本，不会调用模型。</div></div>'}
        ${batches.map((b) => `<details class="ps-adv"><summary>批次 ${b.ordinal} · ${esc(b.status)} · 已尝试 ${b.attempts} 次 · 查看要发给模型的证据片段（${(b.rows || []).length} 条）</summary>
          <table class="ps-table" style="margin-top:8px"><thead><tr><th>证据</th><th>时间</th><th>片段</th><th>来源</th><th>文本</th></tr></thead><tbody>
          ${(b.rows || []).map((m) => `<tr><td class="mono">${esc(m.id)}</td><td class="small">${dt(m.time)}</td><td>${esc(m.snippet)}</td><td class="small">${esc(m.source)}</td><td>${esc(m.text)}${m.truncated ? ' <span class="faint">（截短）</span>' : ''}</td></tr>`).join('')}</tbody></table>
          <div class="faint small" style="margin:6px 0">姓名和账号 ID 不会发给模型，但消息文本可能含私人信息，请在批准前检查。</div>
          <button class="btn sm ${b.status === 'ready' ? 'primary' : ''}" data-arun="${b.ordinal}" data-st="${esc(b.status)}" ${b.status === 'done' || job.calls_reserved >= job.max_calls ? 'disabled' : ''}>
            ${b.status === 'done' ? '本批已完成' : b.status === 'ready' ? '批准本批模型调用，生成草稿' : '明确重新调用（上次可能已计费）'}</button></details>`).join('')}
        <h3 class="ps-h" style="margin-top:14px">逐条审核 <span class="faint small">模型自评不是校准概率</span></h3>
        ${items.length ? items.map((it) => {
          const bucket = {expression: 'observations', reception: 'preferences', strategy: 'strategies'}[it.category];
          const cur = ((analysis.current || {})[bucket] || {})[it.dimension];
          const ev = rows.filter((m) => (it.evidence_ids || []).includes(m.id) || (it.conflict_ids || []).includes(m.id));
          return `<div class="ps-item"><div class="row"><b class="grow">${esc(CAT[it.category] || it.category)} · ${esc(it.dimension)}</b><span class="badge ${it.status === 'accepted' ? 'green' : it.status === 'rejected' ? 'red' : ''}">${esc(it.status)}</span></div>
            <div class="small">当前：${esc(cur ? cur.value : '未设置')} → 草稿：<b>${esc(it.value)}</b>${cur && cur.locked ? ' · 当前项已锁定' : ''}</div>
            <div class="small muted">依据：${esc(it.rationale)}；模型自评 ${esc(it.confidence)}（非概率）；资料${it.sufficient ? '模型认为充分，仍需人工核对' : '不足，不可直接应用'}${it.review_note ? '；' + esc(it.review_note) : ''}${it.locked_conflict ? '；与手动锁定项冲突' : ''}；范围 ${esc(it.scope)}</div>
            ${ev.length ? `<details class="ps-adv"><summary>证据与反例（${ev.length}）</summary>${ev.map((m) => `<div class="small">${(it.conflict_ids || []).includes(m.id) ? '<span class="badge red">反例</span> ' : ''}${esc(m.snippet)} · ${esc(m.text)}</div>`).join('')}</details>` : ''}
            <div class="row" style="margin-top:6px">${it.status === 'pending' ? `<input class="input" style="max-width:220px" data-aval="${esc(it.id)}" value="${esc(it.value)}">
              <button class="btn sm primary" data-aacc="${esc(it.id)}" ${!it.sufficient || (cur && cur.locked) || it.locked_conflict ? 'disabled' : ''}>${it.category === 'expression' ? '接受为观察记录' : '接受为回复设置'}</button>
              <button class="btn sm" data-arej="${esc(it.id)}">拒绝并抑制同类建议</button>` : ''}
              ${it.status === 'rejected' ? `<button class="btn sm" data-areo="${esc(it.id)}">重新评估本条（不调用模型）</button>` : ''}
              ${it.status === 'accepted' ? `<button class="btn sm" data-aundo="${esc(it.application_id)}">撤销本次应用</button>` : ''}</div></div>`;
        }).join('') : '<div class="faint small">暂无草稿建议；未执行、资料不足或输出未通过校验都不会生成生效画像。</div>'}</div>`;
      const rev = () => analysis.current.revision;
      const after = async (refresh) => { if (refresh) await contactDetail(); await loadAnalysis(job.id); };
      $$('[data-arun]', box).forEach((b) => b.onclick = async () => {
        const unknown = b.dataset.st !== 'ready';
        if (!await modal({title: unknown ? '重新调用这一批？' : '批准调用模型？', ok: '批准调用',
          body: unknown ? '上次可能已经调用并计费。会在原预算内重新调用这一批。' : '已检查本批私人文本、时间范围和模型路由，批准调用一次模型生成草稿（草稿不会自动生效）。'})) return;
        busy(b, async () => { try { await pz('/analysis/run', {id: job.id, ordinal: +b.dataset.arun, confirm_model_call: true, retry_unknown: unknown}); } finally { await loadAnalysis(job.id); } });
      });
      $$('[data-aacc]', box).forEach((b) => b.onclick = () => busy(b, async () => {
        await pz('/analysis/review', {item_id: b.dataset.aacc, action: 'accept', revision: rev(), value: $(`[data-aval="${CSS.escape(b.dataset.aacc)}"]`, box).value}); await after(true);
      }));
      $$('[data-arej]', box).forEach((b) => b.onclick = () => busy(b, async () => { await pz('/analysis/review', {item_id: b.dataset.arej, action: 'reject', revision: rev()}); await after(true); }));
      $$('[data-areo]', box).forEach((b) => b.onclick = () => busy(b, async () => { await pz('/analysis/review', {item_id: b.dataset.areo, action: 'reevaluate', revision: rev()}); await after(false); }));
      $$('[data-aundo]', box).forEach((b) => b.onclick = () => busy(b, async () => { await pz('/analysis/undo', {application_id: b.dataset.aundo, revision: rev()}); await after(true); }));
    }

    // =====================================================================
    // 模板与批量构建
    // =====================================================================
    async function tabTpl() {
      await loadCat();
      if (!alive || lastTab !== 'tpl') return;
      if (!cat) return authBox(body(), tabTpl);
      if (!personas.length) await loadPersonas();
      const t = cat.default_template;
      const sys = ['filehelper', 'newsapp', 'notifymessage', 'weixin', cat.session.account];
      const privs = cat.contacts.filter((c) => !isGroup(c.username) && !c.username.startsWith('gh_') && !sys.includes(c.username));
      const allPriv = cat.contacts.filter((c) => !isGroup(c.username));
      body().innerHTML = `<div class="stack" style="gap:16px">
        ${t ? `<div class="card"><div class="card-head"><h2>默认人设与偏好模板</h2>${t.revision ? `<span class="badge green">版本 ${t.revision}</span>` : '<span class="badge">尚未保存</span>'}</div><div class="card-body stack">
          <div class="muted small">没有单独设置的好友自动使用默认模板；已有设置保持原样，可以在「按会话设置」里一键套用。只复制角色和交流偏好，不复制事实、聊天记录或主动消息设置；群和系统账号不继承。</div>
          <div class="grid cols-2"><div class="field"><label>用这位好友的当前设置做模板</label><select class="input" id="tSrc">${privs.map((c) => `<option value="${esc(c.username)}" ${c.username === t.template_source ? 'selected' : ''}>${esc(c.remark || c.name || c.username)} · ${esc(c.username)}</option>`).join('')}</select></div>
            <div class="setting" style="border:0;align-self:end;padding:0 0 6px"><div class="grow"><div class="t">未配置的好友自动继承</div></div><label class="switch"><input type="checkbox" id="tOn" ${t.template_enabled === true ? 'checked' : ''}><span></span></label></div></div>
          <div class="row"><button class="btn primary" id="tSave">保存为默认模板</button></div>
          ${t.revision ? `<dl class="kv ps-tpl"><dt>人设</dt><dd>${esc(t.persona_id ? pName(t.persona_id) : '全局人设')}</dd><dt>来源好友</dt><dd>${esc(cName(t.template_source))}</dd><dt>保存于</dt><dd>${dt(t.template_updated)}</dd>
            <dt>开关</dt><dd class="chips">${SW.map(([k, l]) => `<span class="badge ${(k === 'agent_enabled' || k === 'skip_closing_replies' ? t[k] !== false : t[k] === true) ? 'green' : ''}">${l}</span>`).join('')}</dd>
            <dt>偏好</dt><dd class="chips">${Object.entries(t.preferences || {}).map(([k, v]) => `<span class="badge">${esc(cat.labels[k] || k)}：${esc(v.value)}</span>`).join('') || '<span class="faint">无</span>'}</dd></dl>` : ''}
        </div></div>` : ''}
        <div class="card"><div class="card-head"><h2>限量构建 / 增量更新</h2><span class="faint small">本地规则提取明确反馈，模型调用 0 次</span></div><div class="card-body stack">
          <div class="muted small">选最多 10 位私聊联系人，每位每批最多扫描 1000 条。只读文本及来源，不提取机器人回复或系统消息。中断后从已提交的位置继续；失败由你点重试，不在后台自动跑。不会生成完整人格画像。</div>
          <div class="ps-batch">
            <div><input class="input" id="bq" placeholder="搜索联系人"><div class="ps-checks" id="bList"></div></div>
            <div class="stack"><div class="field"><label>每位扫描条数</label><input class="input" type="number" id="bLimit" min="1" max="1000" value="100"></div>
              <div class="faint small" id="bN">已选 0 位</div><button class="btn primary" id="bScan">预览扫描范围</button></div></div>
          <div id="bRanges"></div></div></div>
        <div class="card"><div class="card-head"><h2>任务进度</h2><button class="btn sm ghost" id="jRefresh">刷新</button></div><div id="jobs"></div></div></div>`;
      if (t) $('#tSave').onclick = async () => {
        if (!await modal({title: '保存默认模板？', ok: '保存', body: `用「${esc(cName($('#tSrc').value))}」的人设、偏好和画像开关保存为默认模板。未配置的好友会按「自动继承」开关使用它。`})) return;
        busy($('#tSave'), async () => { await pz('/template', {contact: $('#tSrc').value, revision: t.revision, enabled: $('#tOn').checked}); toast('已保存', 'ok'); tabTpl(); });
      };
      const chosen = new Set();
      const drawB = () => {
        const q = $('#bq').value.trim().toLowerCase();
        $('#bList').innerHTML = allPriv.filter((c) => !q || [c.remark, c.name, c.username].some((x) => String(x || '').toLowerCase().includes(q))).map((c) =>
          `<label class="row ps-check"><input type="checkbox" value="${esc(c.username)}" ${chosen.has(c.username) ? 'checked' : ''}><span class="grow ellipsis">${esc(c.remark || c.name || c.username)}</span><span class="faint small mono">${esc(c.username)}</span></label>`).join('');
        $$('#bList input').forEach((x) => x.onchange = () => {
          if (x.checked && chosen.size >= 10) { x.checked = false; toast('每批最多 10 位', 'err'); return; }
          x.checked ? chosen.add(x.value) : chosen.delete(x.value); $('#bN').textContent = `已选 ${chosen.size} 位`;
        });
      };
      $('#bq').oninput = drawB; drawB();
      $('#bScan').onclick = (e) => busy(e.currentTarget, async () => {
        if (!chosen.size) throw new Error('先选择联系人');
        const d = await pz('/history/preview', {contacts: [...chosen], limit: +$('#bLimit').value});
        $('#bRanges').innerHTML = `<div class="small muted" style="margin-bottom:8px">联系人 ${d.contact_count} 位；预计模型调用 ${d.model_calls} 次</div>
          <table class="ps-table"><thead><tr><th>联系人</th><th>范围</th><th>条数</th><th></th></tr></thead><tbody>
          ${d.previews.map((p, i) => `<tr><td>${esc(cName(p.contact))}</td><td class="mono">${esc(p.lo)} → ${esc(p.hi)}</td><td>${esc(p.total)}</td>
            <td><button class="btn sm" data-bc="${i}" ${p.total === 0 ? 'disabled' : ''}>确认范围并创建任务</button></td></tr>`).join('')}</tbody></table>
          <div class="faint small" style="margin-top:6px">创建任务不会立刻扫描，在下面的任务进度里逐批继续。</div>`;
        $$('[data-bc]').forEach((b) => b.onclick = () => busy(b, async () => { await pz('/history/create', d.previews[+b.dataset.bc]); b.disabled = true; b.textContent = '已创建'; await loadCat(); drawJobs(); }));
      });
      const drawJobs = () => {
        const labels = {ready: '待继续', done: '完成', failed: '失败，进度保留'};
        $('#jobs').innerHTML = cat.jobs.length ? `<table class="ps-table"><thead><tr><th>联系人</th><th>类型</th><th>状态</th><th>进度</th><th></th></tr></thead><tbody>
          ${cat.jobs.map((j) => `<tr><td>${esc(cName(j.contact))}<div class="faint small mono">${esc(j.id.slice(0, 8))}</div></td><td>${j.kind === 'live' ? '实时明确反馈' : '历史构建'}</td>
            <td><span class="badge ${j.status === 'done' ? 'green' : j.status === 'failed' ? 'red' : 'amber'}">${esc(labels[j.status] || j.status)}</span>${j.error ? `<div class="small" style="color:var(--red)">${esc(j.error)}</div>` : ''}</td>
            <td><div class="ps-bar"><i style="width:${j.total ? Math.min(100, Math.round(j.scanned / j.total * 100)) : 0}%"></i></div><span class="small faint">${j.scanned}/${j.total} 条</span></td>
            <td>${j.status !== 'done' ? `<button class="btn sm" data-js="${esc(j.id)}">${j.status === 'failed' ? '重试下一批' : '继续下一批'}（最多 50 条）</button>` : ''}</td></tr>`).join('')}</tbody></table>`
          : '<div class="empty">还没有任务</div>';
        $$('[data-js]').forEach((b) => b.onclick = () => busy(b, async () => { try { await pz('/history/step', {id: b.dataset.js}); } finally { await loadCat(); drawJobs(); } }));
      };
      drawJobs();
      $('#jRefresh').onclick = (e) => busy(e.currentTarget, async () => { await loadCat(); drawJobs(); });
    }

    // =====================================================================
    // 联系人画像
    // =====================================================================
    async function tabProf() {
      let profs, rules = {};
      try { profs = (await PA(id, '/api/profiles')).profiles || []; }
      catch (e) { if (!alive || lastTab !== 'prof') return; catErr = e; return authBox(body(), tabProf); }
      try { rules = (await A(id, '/api/bot')).rules || {}; } catch (e) { /* 读不到规则时开关显示为关 */ }
      if (!alive || lastTab !== 'prof') return;
      profs.sort((a, b) => (b.updated || 0) - (a.updated || 0));
      body().innerHTML = `<div class="stack" style="gap:16px">
        <div class="card card-body">
          <div class="setting" style="padding-top:0"><div class="grow"><div class="t">聊天中自动抽画像</div><div class="d">机器人回复时顺便从对话里记下对方的长期事实（会多花 token，默认关）。关掉后后台不再抽。</div></div>
            <label class="switch"><input type="checkbox" id="lpOn" ${rules.learn_profiles ? 'checked' : ''}><span></span></label></div>
          <div class="setting"><div class="grow"><div class="t">立刻抽某个会话</div><div class="d">从这个会话最近 80 条消息里抽画像，不受上面开关影响；会调用一次大模型。图片/语音只在监听会话回复时识别。</div></div>
            <button class="btn" id="lpNow">选择会话…</button></div></div>
        <div class="card"><div class="card-head"><h2>联系人画像</h2><span class="badge">${profs.length} 人</span></div>
          <div class="ps-split"><div class="ps-list" id="prList"></div><div class="ps-detail" id="prDetail"></div></div></div></div>`;
      $('#lpOn').onchange = async (e) => {
        try { await A(id, '/api/bot/learn_profiles', {method: 'POST', json: {enabled: e.target.checked}}); toast(e.target.checked ? '已开启自动抽画像' : '已关闭自动抽画像'); }
        catch (x) { toast(x.message, 'err'); e.target.checked = !e.target.checked; }
      };
      $('#lpNow').onclick = async () => {
        const c = await pickChat(id, {title: '从哪个会话抽画像'}); if (!c) return;
        if (!await modal({title: `从「${c.name}」抽画像？`, ok: '开始抽取', body: '会读取这个会话最近 80 条消息并调用一次大模型，提取对方的长期事实写进画像。不会发送任何消息。'})) return;
        busy($('#lpNow'), async () => {
          const r = await PA(id, '/api/profiles/extract', {method: 'POST', json: {chat: c.username}});
          toast(`已抽取：扫描 ${r.scanned || 0} 条，更新 ${r.updated || 0} 人`, 'ok'); tabProf();
        });
      };
      const list = () => {
        $('#prList').innerHTML = profs.map((p) => `<div class="chat-item ${p.wxid === curProfile ? 'active' : ''}" data-w="${esc(p.wxid)}">${av(id, p.wxid)}
          <div class="grow"><div class="row"><span class="n ellipsis grow">${esc(p.name || p.wxid)}</span><span class="t">${p.facts} 条</span></div>
          <div class="s ellipsis">${esc(p.summary || '暂无摘要')}</div></div></div>`).join('') || '<div class="empty">还没学到画像。可以打开自动抽，或「立刻抽某个会话」。</div>';
        $$('#prList [data-w]').forEach((el) => el.onclick = () => { curProfile = el.dataset.w; list(); profDetail(); });
      };
      if (!profs.some((p) => p.wxid === curProfile)) curProfile = (profs[0] || {}).wxid || '';
      list(); profDetail();
      async function profDetail() {
        const box = $('#prDetail'); if (!box) return;
        if (!curProfile) { box.innerHTML = '<div class="empty">选择左边的联系人</div>'; return; }
        const w = curProfile; box.innerHTML = spin;
        let d; try { d = await PA(id, '/api/profiles/' + encodeURIComponent(w)); } catch (e) { box.innerHTML = `<div class="callout red"><div>${esc(e.message)}</div></div>`; return; }
        if (w !== curProfile || !$('#prDetail')) return;
        const facts = (d.facts || []).slice().sort((a, b) => ((a.status || 'active') === 'active' ? 0 : 1) - ((b.status || 'active') === 'active' ? 0 : 1) || (b.recorded_ts || 0) - (a.recorded_ts || 0));
        const active = facts.filter((f) => (f.status || 'active') === 'active').length;
        const styles = Object.entries(d.style_by_scope || {});
        const emo = (d.emotion_history || []).slice(-8).reverse();
        box.innerHTML = `<div class="stack">
          <div class="row">${av(id, w, '')}<div class="grow"><h2 class="ellipsis">${esc(d.name || w)}</h2><div class="faint small mono">${esc(w)} · 更新于 ${dt(d.updated)}</div></div><button class="btn sm danger" id="prDel">删除画像</button></div>
          ${d.summary ? `<div class="callout blue"><div>${esc(d.summary)}</div></div>` : ''}
          <div class="row"><h3 class="ps-h grow" style="margin:0">事实</h3><span class="faint small">有效 ${active} 条 / 共 ${facts.length} 条</span></div>
          <div class="ps-facts">${facts.map((f) => { const st = f.status || 'active'; const t = f.text || f.fact || (typeof f === 'string' ? f : '');
            return `<div class="ps-fact ${st !== 'active' ? 'off' : ''}"><div class="grow">${esc(t)}</div><div class="row small faint" style="flex:none">
              ${st !== 'active' ? `<span class="badge">${st === 'cancelled' ? '已取消' : st === 'superseded' ? '已更新' : esc(st)}</span>` : ''}${f.type && f.type !== 'unknown' ? `<span class="badge">${esc(f.type)}</span>` : ''}
              ${f.scope && String(f.scope).startsWith('group') ? '<span class="badge blue">群</span>' : ''}<span title="${esc(f.scope || '')}">${f.recorded_ts ? fmtTime(f.recorded_ts) : ''}</span></div></div>`; }).join('') || '<div class="faint small">没有事实</div>'}</div>
          ${styles.length || emo.length ? `<details class="ps-adv"><summary>表达习惯与近期情绪</summary>
            ${styles.map(([sc, v]) => `<div class="small" style="margin-top:6px"><span class="badge">${String(sc).startsWith('group') ? '群聊' : '私聊'}</span> 消息 ${v.messages} 条 · 平均 ${v.messages ? Math.round(v.chars / v.messages) : 0} 字 · 短回复 ${v.short_replies} · 问句 ${v.questions} · 表情 ${v.emoji} · 感叹 ${v.exclamations}</div>`).join('')}
            ${emo.length ? `<div class="chips" style="margin-top:8px">${emo.map((x) => `<span class="badge">${esc(x.label)} · ${fmtTime(x.ts)}</span>`).join('')}</div>` : ''}</details>` : ''}</div>`;
        $('#prDel').onclick = async () => {
          if (!await modal({title: `删除「${d.name || w}」的画像？`, danger: true, ok: '删除', body: '这个联系人的全部长期事实、摘要和表达统计都会删除，不能恢复。机器人之后会从头开始记。'})) return;
          busy($('#prDel'), async () => { await PA(id, '/api/profiles/' + encodeURIComponent(w) + '/delete', {method: 'POST'}); toast('已删除'); curProfile = ''; tabProf(); });
        };
      }
    }

    // =====================================================================
    // 蒸馏
    // =====================================================================
    async function tabDistill() {
      const [groups, sess, llm] = await Promise.all([A(id, '/api/groups').catch(() => []), A(id, '/api/sessions').catch(() => []), A(id, '/api/llm').catch(() => ({}))]);
      if (!alive || lastTab !== 'distill') return;
      const privs = sess.filter((x) => !isGroup(x.username) && x.category !== 'other');
      body().innerHTML = `<div class="stack" style="gap:16px">
        ${llm.configured === false ? '<div class="callout amber"><div>还没配置大模型，蒸馏会失败。先到「AI 模型与知识库」配置。</div></div>' : ''}
        <div class="callout blue"><div>蒸馏会把某人的全部发言交给大模型，总结出说话风格，生成一个新人设放进人设库（需要至少 20 条发言，约 1 分钟，会产生模型费用）。生成后在「按会话设置」里绑定给联系人，或设为全局人设。</div></div>
        <div id="dResult"></div>
        <div class="grid cols-2" style="align-items:start">
          <div class="card"><div class="card-head"><h2>从群聊里蒸馏某人</h2></div><div class="card-body stack">
            <div class="row"><select class="input" id="dGroup">${groups.map((g) => `<option value="${esc(g.username)}">${esc(g.name || g.username)}（${g.member_count || '?'} 人）</option>`).join('') || '<option value="">没有群</option>'}</select><button class="btn" id="dCand">列出发言人</button></div>
            <div id="dCands" class="ps-cands"><div class="faint small">选一个群，列出发言最多的人</div></div></div></div>
          <div class="stack" style="gap:16px">
            <div class="card"><div class="card-head"><h2>蒸馏私聊对象</h2></div><div class="card-body stack">
              <select class="input" id="dPriv">${privs.map((x) => `<option value="${esc(x.username)}">${esc(x.name || x.username)}</option>`).join('') || '<option value="">暂无私聊</option>'}</select>
              <div class="row"><span class="faint small grow">用对方在这段私聊里的全部发言学 TA 的风格</span><button class="btn primary" id="dPrivBtn">蒸馏</button></div></div></div>
            <div class="card"><div class="card-head"><h2>蒸馏我自己</h2></div><div class="card-body stack">
              <input class="input" id="dSelfName" placeholder="人设名字（默认：我）">
              <div class="row"><span class="faint small grow">汇总你在所有群聊和私聊里自己发的话</span><button class="btn primary" id="dSelfBtn">蒸馏</button></div></div></div>
          </div></div></div>`;
      const run = async (btn, title, url, payload) => {
        if (!await modal({title, ok: '开始蒸馏', body: '会调用大模型分析发言并生成新人设，大约需要 1 分钟，会产生模型费用。不会发送任何消息。'})) return;
        busy(btn, async () => {
          toast('蒸馏中，调用大模型，请稍等…');
          const r = await A(id, url, {method: 'POST', json: payload});
          if (!r.ok) throw new Error('蒸馏失败：' + (r.error || ''));
          await loadPersonas(); if (cat) await loadCat();
          $('#dResult').innerHTML = `<div class="card"><div class="card-head"><h2>已生成人设「${esc(r.name)}」</h2><div class="row"><span class="badge">${(r.stats || {}).count || '?'} 条发言</span><button class="btn sm primary" id="dGo">在人设库查看</button></div></div>
            <div class="card-body"><pre class="ps-text">${esc(r.persona || '')}</pre></div></div>`;
          $('#dGo').onclick = () => { curPersona = r.slug; show('lib'); };
          $('#dResult').scrollIntoView({behavior: 'smooth', block: 'nearest'});
        });
      };
      $('#dCand').onclick = (e) => busy(e.currentTarget, async () => {
        const g = $('#dGroup').value; if (!g) return;
        const cs = await A(id, '/api/distill/candidates', {method: 'POST', json: {group: g}});
        $('#dCands').innerHTML = cs.length ? cs.map((c, i) => `<div class="chat-item">${av(id, c.wxid)}<div class="grow"><div class="n ellipsis">${esc(c.name)}</div><div class="s ellipsis">${c.count} 条发言 · ${esc(c.wxid)}</div></div>
          <button class="btn sm ${c.count < 20 ? '' : 'primary'}" data-dc="${i}" ${c.count < 20 ? 'disabled title="发言少于 20 条"' : ''}>蒸馏</button></div>`).join('') : '<div class="faint small">这个群没有发言记录</div>';
        $$('[data-dc]').forEach((b) => b.onclick = () => { const c = cs[+b.dataset.dc]; run(b, `蒸馏「${c.name}」？`, '/api/distill/run', {group: g, wxid: c.wxid, name: c.name}); });
      });
      $('#dPrivBtn').onclick = (e) => {
        const sel = $('#dPriv'); const u = sel.value; if (!u) { toast('没有可蒸馏的私聊', 'err'); return; }
        const nm = sel.options[sel.selectedIndex].text;
        run(e.currentTarget, `蒸馏「${nm}」？`, '/api/distill/run', {group: u, wxid: u, name: nm});
      };
      $('#dSelfBtn').onclick = (e) => run(e.currentTarget, '蒸馏你自己？', '/api/distill/self', {name: $('#dSelfName').value.trim() || '我'});
    }

    show(lastTab);
  };
})();
