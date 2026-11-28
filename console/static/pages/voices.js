/* 语音:两个页面
 *   voices      音色与复刻:音色列表/试听/编辑/默认/隐藏/删除、语音发送形式、复刻音色、语音厂商与 API Key
 *   voicereply  语音回复:机器人什么时候说语音(全局) + 按好友/群单独设置音色和模式
 * 接口:/api/tts/config /api/tts/preview /api/tts/clone /api/voice/providers /api/voice/keys /api/voice_reply
 *       /api/contacts /api/groups(取名字) */
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct, onLeave, pickChat} = WX;

  // 内置预设的名字(被隐藏后 /api/tts/config 不再返回它们,这里留一份好显示)
  const PRESET_LABELS = {
    warm_low: '温柔低语(男)', calm_deep: '沉稳克制(男)', magnetic: '磁性低音(男)', sunny: '阳光少年(男)', boyish: '少年感(男)',
    cantonese: '粤语(男)', female_warm: '温暖女声', female_lively: '活泼女声', female_tw: '台湾腔女声',
  };
  const MODES = {
    off: ['不发语音', '机器人只打字。'],
    request: ['对方要求时', '对方说「发语音 / 说给我听 / 想听你声音」时这条用语音，其它时候打字。'],
    auto: ['自动判断', '对方要求时用语音；另外模型觉得适合开口说（晚安、撒娇、简短口语）时，按下面的概率改用语音。'],
    always: ['总是语音', '每条回复都说成语音（超过最长字数的仍然打字）。'],
  };
  const SEND_AS = {
    voice_bar: ['语音条', '真正的语音消息，和手机上按住说话一样。需要微信 4.1.9+。'],
    video: ['声波视频', '带声波画面的短视频，对方点开就播。任何版本都能发。'],
    file: ['mp3 文件', '文件卡片，对方点开预览播放。'],
  };
  const KSTAT = {ok: ['正常', 'green'], rate: ['限流冷却中', 'amber'], exhausted: ['额度用完/欠费', 'red'], auth: ['key 无效或没权限', 'red'],
    disabled: ['已停用', ''], other: ['出错', 'amber']};
  const KEY_RE = /^[a-z0-9_]{1,32}$/;

  const head = (id, title, actions = '') => setTop(`<div class="row">${avatar(acct(id), 'sm')}<h1>${esc(title)}</h1></div>`, actions);
  const loading = (view) => { view.innerHTML = '<div class="page"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>'; };
  const fail = (view, e) => { view.innerHTML = `<div class="page"><div class="callout red">读取失败：${esc(e.message)}</div></div>`; };
  const signed = (n, unit) => (n >= 0 ? '+' : '') + n + unit;
  const dt = (t) => t ? new Date(t * 1000).toLocaleString('zh-CN', {hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '';

  // 试听:返回 mp3 的 objectURL(不发送任何东西)
  async function previewUrl(id, body) {
    const r = await fetch(`/a/${id}/api/tts/preview`, {method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
    if (!r.ok) { let d = {}; try { d = await r.json(); } catch (e) { /**/ } throw new Error(d.error || `试听失败(${r.status})`); }
    return URL.createObjectURL(await r.blob());
  }

  // 联系人/群名字
  async function loadNames(id) {
    const [cs, gs] = await Promise.all([A(id, '/api/contacts').catch(() => []), A(id, '/api/groups').catch(() => [])]);
    const m = {};
    for (const c of cs || []) m[c.username] = {name: c.remark || c.name || c.nick_name || c.username, sub: c.alias || c.username, group: false};
    for (const g of gs || []) m[g.username] = {name: g.name || g.username, sub: `群 · ${g.member_count || '?'} 人`, group: true};
    return m;
  }

  // ======================================================================
  // 音色与复刻
  // ======================================================================
  WXPAGES.voices = async (view, id) => {
    head(id, '音色与复刻', `<a class="btn" href="#/a/${id}/voicereply">语音回复设置</a><button class="btn primary" id="vcGoClone">＋ 复刻音色</button>`);
    loading(view);
    let tts, provs, vr;
    try {
      [tts, provs, vr] = await Promise.all([A(id, '/api/tts/config'), A(id, '/api/voice/providers').then((d) => d.providers),
        A(id, '/api/voice_reply').catch(() => null)]);
    } catch (e) { fail(view, e); return; }
    let editing = null;          // 正在编辑的音色 key
    let audioUrl = null;
    onLeave(() => { if (audioUrl) URL.revokeObjectURL(audioUrl); });

    const provOf = (pid) => provs.find((p) => p.id === pid);
    const voiceOf = (k) => tts.voices.find((v) => v.key === k);
    const clone = (v) => !!(v && v.provider);

    view.innerHTML = `<div class="page stack" style="gap:16px">
      ${tts.edge_error ? `<div class="callout amber">取微软音色列表失败：${esc(tts.edge_error)}（编辑微软音色时只能保留原来的音色名）</div>` : ''}
      <div class="card" id="vcList"></div>
      <div class="grid cols-2" style="align-items:start">
        <div class="card" id="vcClone"></div>
        <div class="card" id="vcSendAs"></div>
      </div>
      <div class="card" id="vcProv"></div>
    </div>`;
    $('#vcGoClone').onclick = () => { $('#vcClone').scrollIntoView({behavior: 'smooth', block: 'start'}); const i = $('#vcLabel'); if (i) setTimeout(() => i.focus(), 300); };

    const saveTTS = async (body, msg = '已保存') => {
      const d = await A(id, '/api/tts/config', {method: 'POST', json: body});
      tts = Object.assign(tts, d); drawList(); drawSendAs(); drawClone(); toast(msg, 'ok');
    };

    async function play(btn, body) {
      await busy(btn, async () => {
        const url = await previewUrl(id, Object.assign({text: ($('#vcText').value || '').trim() || '你好，这是试听。'}, body));
        if (audioUrl) URL.revokeObjectURL(audioUrl);
        audioUrl = url; const a = $('#vcAudio'); a.src = url; a.hidden = false;
        const who = voiceOf(body.voice); $('#vcNow').textContent = who ? `正在试听：${who.label}` : '';
        a.play().catch(() => {});
      });
    }

    // ---------- 音色列表 ----------
    function usage(k) {
      const out = [];
      if (k === tts.default) out.push('<span class="badge green">默认音色</span>');
      if (vr && vr.voice === k) out.push('<span class="badge blue">语音回复</span>');
      const n = vr ? Object.values(vr.chats || {}).filter((c) => c.voice === k).length : 0;
      if (n) out.push(`<span class="badge blue">${n} 位好友专用</span>`);
      return out.join('');
    }
    function rowHtml(v) {
      const p = clone(v) ? provOf(v.provider) : null;
      const sub = clone(v)
        ? `${esc(p ? p.label : v.provider)} · ${esc(v.model || '')}${v.sample_seconds ? ` · 样本 ${esc(v.sample_seconds)} 秒` : ''}`
        : `微软 · <span class="mono">${esc(v.name)}</span> · 语速 ${esc(v.rate || '+0%')} · 音调 ${esc(v.pitch || '+0Hz')}`;
      const tag = clone(v) ? '<span class="badge amber">复刻</span>' : v.builtin ? (v.changed ? '<span class="badge">预设 · 已改</span>' : '<span class="badge">预设</span>') : '<span class="badge">自定义</span>';
      return `<div class="vc-row ${editing === v.key ? 'open' : ''}" data-k="${esc(v.key)}">
        <div class="vc-main">
          <div class="vc-ico ${clone(v) ? 'clone' : ''}">${clone(v) ? '🎙' : '微'}</div>
          <div class="grow"><div class="row wrap"><b class="ellipsis">${esc(v.label)}</b>${tag}${usage(v.key)}</div>
            <div class="small faint ellipsis" style="margin-top:3px">${sub}</div></div>
          <div class="row vc-acts">
            <button class="btn sm" data-a="play" title="用上面的试听文字合成一段，不会发给任何人">▶ 试听</button>
            ${v.key === tts.default ? '' : '<button class="btn sm ghost" data-a="default">设为默认</button>'}
            <button class="btn sm ghost" data-a="edit">${editing === v.key ? '收起' : '编辑'}</button>
          </div>
        </div>
        ${editing === v.key ? `<div class="vc-edit">${editHtml(v)}</div>` : ''}
      </div>`;
    }
    function edgeOptions(sel) {
      const list = (tts.edge_voices || []).slice();
      if (sel && !list.some((x) => x.name === sel)) list.unshift({name: sel, locale: sel.split('-').slice(0, 2).join('-'), gender: '', tags: ''});
      const groups = {};
      for (const x of list) (groups[x.locale || '其它'] = groups[x.locale || '其它'] || []).push(x);
      const loc = {'zh-CN': '普通话', 'zh-HK': '粤语', 'zh-TW': '台湾', 'zh-CN-liaoning': '东北话', 'zh-CN-shaanxi': '陕西话'};
      return Object.entries(groups).map(([l, xs]) => `<optgroup label="${esc(loc[l] || l)}">${xs.map((x) =>
        `<option value="${esc(x.name)}" ${x.name === sel ? 'selected' : ''}>${esc(x.name.replace(/^[a-z]{2}-[A-Za-z]{2,4}-/, '').replace(/Neural$/, ''))}${x.gender ? ' · ' + (x.gender === 'Male' ? '男' : '女') : ''}${x.tags ? ' · ' + esc(x.tags) : ''}</option>`).join('')}</optgroup>`).join('');
    }
    function rpHtml(rate, pitch, pre = 'e') {
      const r = parseInt(rate) || 0, p = parseInt(pitch) || 0;
      return `<div class="field"><label>语速 <span class="vc-val" id="${pre}RateT">${signed(r, '%')}</span></label><input type="range" id="${pre}Rate" min="-50" max="50" step="1" value="${r}"></div>
        <div class="field"><label>音调 <span class="vc-val" id="${pre}PitchT">${signed(p, 'Hz')}</span></label><input type="range" id="${pre}Pitch" min="-50" max="50" step="1" value="${p}"></div>`;
    }
    function editHtml(v) {
      if (clone(v)) {
        const p = provOf(v.provider);
        const models = p ? p.models : [[v.model, v.model]];
        return `<div class="vc-grid2">
            <div class="field"><label>显示名</label><input class="input" id="eLabel" maxlength="30" value="${esc(v.label)}"></div>
            <div class="field"><label>合成模型（${esc(p ? p.label : v.provider)}）</label><select class="input" id="eModel">${models.map(([m, l]) => `<option value="${esc(m)}" ${m === v.model ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></div>
          </div>
          <dl class="kv" style="margin-top:14px"><dt>音色 key</dt><dd class="mono">${esc(v.key)}</dd>
            <dt>录音样本</dt><dd>${v.sample ? `已保存${v.sample_seconds ? `（${esc(v.sample_seconds)} 秒）` : ''}，换 key / 换模型时会用它自动重新复刻` : '没有保存样本'}</dd>
            <dt>已复刻到</dt><dd>${Object.keys(v.ids || {}).length ? Object.keys(v.ids).map((s) => `<span class="badge">${esc(s.replace('|', ' · '))}</span>`).join(' ') : '还没复刻（第一次合成时复刻）'}</dd>
            ${v.created ? `<dt>创建于</dt><dd>${dt(v.created)}</dd>` : ''}</dl>
          ${p && p.model_bound ? '<div class="small faint" style="margin-top:10px">这家的音色绑定模型：换了模型，下次合成会自动用录音在新模型上复刻一次。</div>' : ''}
          <div class="row wrap" style="margin-top:14px"><button class="btn sm primary" data-a="save">保存</button>
            <span class="grow"></span><button class="btn sm danger" data-a="delete">删除音色</button></div>`;
      }
      return `<div class="vc-grid2">
          <div class="field"><label>显示名</label><input class="input" id="eLabel" maxlength="30" value="${esc(v.label)}"></div>
          <div class="field"><label>微软音色</label><select class="input" id="eName">${edgeOptions(v.name)}</select></div>
          ${rpHtml(v.rate, v.pitch)}
        </div>
        <div class="row wrap" style="margin-top:14px"><button class="btn sm primary" data-a="save">保存</button>
          <button class="btn sm" data-a="tryedit" title="按滑块上的语速和音调试听（不保存）">▶ 试听调整后的</button>
          <span class="small faint" id="eNameHint"></span>
          <span class="grow"></span>
          ${v.builtin ? `<button class="btn sm ghost" data-a="hide">隐藏这个预设</button>${v.changed ? '<button class="btn sm danger" data-a="delete">恢复预设参数</button>' : ''}` : '<button class="btn sm danger" data-a="delete">删除音色</button>'}</div>`;
    }
    function bindRP(root, pre = 'e') {
      const r = $(`#${pre}Rate`, root), p = $(`#${pre}Pitch`, root);
      if (!r) return;
      r.oninput = () => { $(`#${pre}RateT`, root).textContent = signed(+r.value, '%'); };
      p.oninput = () => { $(`#${pre}PitchT`, root).textContent = signed(+p.value, 'Hz'); };
    }
    function drawList() {
      if (editing && !voiceOf(editing)) editing = null;
      const hidden = tts.hidden || [];
      const n = tts.voices.length, nc = tts.voices.filter(clone).length;
      const text = $('#vcText') ? $('#vcText').value : '你好，这是试听。今天过得怎么样？';
      $('#vcList').innerHTML = `<div class="card-head"><h2>音色 <span class="faint small" style="font-weight:400">${n} 个${nc ? ` · 其中复刻 ${nc} 个` : ''}</span></h2>
          <span class="small faint">默认音色：<b>${esc((voiceOf(tts.default) || {}).label || tts.default)}</b></span></div>
        <div class="card-body vc-try">
          <input class="input grow" id="vcText" maxlength="200" value="${esc(text)}" placeholder="试听用的文字">
          <audio id="vcAudio" controls hidden></audio><span class="small faint" id="vcNow"></span>
        </div>
        <div class="vc-list">${tts.voices.map(rowHtml).join('') || '<div class="empty">没有音色</div>'}</div>
        ${hidden.length ? `<div class="vc-hidden"><span class="small muted">已隐藏 ${hidden.length} 个内置音色：</span>
          ${hidden.map((k) => `<span class="vc-chip">${esc(PRESET_LABELS[k] || k)}<button class="btn ghost sm" data-unhide="${esc(k)}" title="重新显示">显示</button></span>`).join('')}
          ${hidden.length > 1 ? '<button class="btn sm ghost" data-unhide="*">全部显示</button>' : ''}</div>` : ''}
        <details class="vc-new"><summary>＋ 新建微软音色（免费，选一个微软声音再调语速音调）</summary>
          <div class="vc-grid2" style="margin-top:12px">
            <div class="field"><label>音色 key（小写字母、数字、下划线）</label><input class="input" id="nKey" maxlength="32" placeholder="例如 my_voice"></div>
            <div class="field"><label>显示名</label><input class="input" id="nLabel" maxlength="30" placeholder="例如 晚安低语"></div>
            <div class="field"><label>微软音色</label><select class="input" id="nName">${edgeOptions('zh-CN-XiaoxiaoNeural')}</select></div>
            <div></div>
            ${rpHtml(0, 0, 'n')}
          </div>
          <div class="row" style="margin-top:12px"><button class="btn sm primary" id="nGo">新建</button><span class="small faint">新建后可以在列表里试听和设为默认</span></div>
        </details>`;
      if (audioUrl) { const a = $('#vcAudio'); a.src = audioUrl; a.hidden = false; }
      bindRP($('#vcList'), 'n');
      const ed = $('.vc-edit', $('#vcList'));
      if (ed) {
        bindRP(ed);
        const v = voiceOf(editing), en = $('#eName', ed);
        if (en) en.onchange = () => { $('#eNameHint', ed).textContent = en.value !== v.name ? '换了微软音色，保存后试听才是新声音' : ''; };
      }
      $$('.vc-row', $('#vcList')).forEach((row) => row.onclick = (e) => {
        const b = e.target.closest('[data-a]'); if (!b) return;
        rowAction(b, voiceOf(row.dataset.k), row);
      });
      $$('[data-unhide]', $('#vcList')).forEach((b) => b.onclick = () => busy(b, () =>
        saveTTS({hidden: b.dataset.unhide === '*' ? [] : hidden.filter((k) => k !== b.dataset.unhide)}, '已显示')));
      $('#nGo').onclick = (e) => busy(e.currentTarget, async () => {
        const key = $('#nKey').value.trim(), label = $('#nLabel').value.trim();
        if (!KEY_RE.test(key)) throw new Error('音色 key 只能用小写字母、数字、下划线');
        if (voiceOf(key) || PRESET_LABELS[key]) throw new Error('这个 key 已经有了，换一个');
        if (!label) throw new Error('先填显示名');
        await saveTTS({voice: {key, label, name: $('#nName').value, rate: signed(+$('#nRate').value, '%'), pitch: signed(+$('#nPitch').value, 'Hz')}}, '已新建');
      });
    }
    async function rowAction(b, v, row) {
      const a = b.dataset.a;
      if (a === 'play') return play(b, {voice: v.key});
      if (a === 'edit') { editing = editing === v.key ? null : v.key; drawList(); return; }
      if (a === 'default') return busy(b, () => saveTTS({default: v.key}, `默认音色改成「${v.label}」`));
      if (a === 'tryedit') return play(b, {voice: v.key, rate: signed(+$('#eRate', row).value, '%'), pitch: signed(+$('#ePitch', row).value, 'Hz')});
      if (a === 'save') {
        const label = $('#eLabel', row).value.trim(); if (!label) { toast('显示名不能为空', 'err'); return; }
        const f = clone(v) ? {label, model: $('#eModel', row).value}
          : {label, name: $('#eName', row).value, rate: signed(+$('#eRate', row).value, '%'), pitch: signed(+$('#ePitch', row).value, 'Hz')};
        return busy(b, () => saveTTS({voice: Object.assign({key: v.key}, f)}));
      }
      if (a === 'hide') {
        const extra = v.key === tts.default ? '<div class="callout amber" style="margin-top:10px">它现在是默认音色，隐藏后默认会换成列表里的第一个。</div>' : '';
        if (!await modal({title: `隐藏「${v.label}」？`, body: '只是不在列表里显示，随时可以在下面「已隐藏」里点「显示」找回。', extra, ok: '隐藏'})) return;
        return busy(b, () => saveTTS({hidden: (tts.hidden || []).concat([v.key])}, '已隐藏'));
      }
      if (a === 'delete') {
        if (v.builtin) {
          if (!await modal({title: `恢复「${PRESET_LABELS[v.key] || v.label}」的预设参数？`, body: '显示名、微软音色、语速、音调都会恢复成内置的样子。', ok: '恢复预设'})) return;
          return busy(b, () => saveTTS({delete: v.key}, '已恢复预设'));
        }
        const used = [];
        if (v.key === tts.default) used.push('默认音色');
        if (vr && vr.voice === v.key) used.push('语音回复的全局音色');
        const n = vr ? Object.values(vr.chats || {}).filter((c) => c.voice === v.key).length : 0;
        if (n) used.push(`${n} 位好友的专用音色`);
        if (!await modal({title: `删除音色「${v.label}」？`, danger: true, ok: '删除',
          body: (clone(v) ? '复刻音色会同时从厂商账号里删掉，录音样本也不能再用来补复刻。删除后不能恢复。' : '删除后不能恢复。') +
            (used.length ? `<div class="callout amber" style="margin-top:10px">它正被用作：${used.join('、')}。删除后这些地方会退回默认音色。</div>` : '')})) return;
        editing = null;
        return busy(b, () => saveTTS({delete: v.key}, '已删除'));
      }
    }

    // ---------- 语音发送形式 ----------
    function drawSendAs() {
      const cur = tts.send_as || 'video';
      const noBar = vr && vr.available === false;
      $('#vcSendAs').innerHTML = `<div class="card-head"><h2>语音发送形式</h2><span class="badge blue">${esc(SEND_AS[cur][0])}</span></div>
        <div class="card-body stack">
          <div class="muted small">手动发语音、机器人语音回复、定时语音，以及发送接口里的语音，都按这里发。</div>
          <div class="vc-opts">${Object.entries(SEND_AS).map(([k, [t, d]]) => `<label class="vc-opt ${k === cur ? 'on' : ''}">
            <input type="radio" name="sendAs" value="${k}" ${k === cur ? 'checked' : ''}>
            <div><div class="t">${t}${k === 'voice_bar' && noBar ? ' <span class="badge amber">这个微信版本不支持</span>' : ''}</div><div class="d">${d}</div></div></label>`).join('')}</div>
        </div>`;
      $$('input[name=sendAs]', $('#vcSendAs')).forEach((i) => i.onchange = async () => {
        try { await saveTTS({send_as: i.value}, `已改成「${SEND_AS[i.value][0]}」`); } catch (e) { toast(e.message, 'err'); drawSendAs(); }
      });
    }

    // ---------- 复刻音色 ----------
    let cloneSrc = 'upload';
    function drawClone() {
      const keep = $('#cProv') ? {prov: $('#cProv').value, model: $('#cModel').value, label: $('#vcLabel').value, from: $('#cFrom') ? $('#cFrom').value : '', slot: $('#cSlot') ? $('#cSlot').value : ''} : {};
      const withSample = tts.voices.filter((v) => clone(v) && v.sample);
      if (!withSample.length) cloneSrc = 'upload';
      const pid = keep.prov && provOf(keep.prov) ? keep.prov : (provs.find((p) => p.keys.length) || provs[0] || {}).id;
      $('#vcClone').innerHTML = `<div class="card-head"><h2>复刻音色</h2><span class="small faint">只能用本人或已获授权的声音</span></div>
        <div class="card-body stack">
          <div class="muted small">上传 10～60 秒的录音或视频（一个人说话、没有背景音乐），复刻成新音色。想对比效果，可以沿用已有音色的录音，换个厂商或模型再复刻一份，然后试听对比。</div>
          <div class="vc-grid2">
            <div class="field"><label>厂商</label><select class="input" id="cProv">${provs.map((p) => `<option value="${esc(p.id)}" ${p.id === pid ? 'selected' : ''}>${esc(p.label)}${p.keys.length ? '' : '（未配 key）'}</option>`).join('')}</select></div>
            <div class="field"><label>模型</label><select class="input" id="cModel"></select></div>
          </div>
          <div class="small faint" id="cNote"></div>
          <div id="cNoKey"></div>
          <div class="field" id="cSlotWrap" style="display:none"><label>已购音色号（可留空）</label><input class="input" id="cSlot" placeholder="S_ 开头；留空 = 后付费自定义音色" value="${esc(keep.slot || '')}"></div>
          <div class="field"><label>音色名字</label><input class="input" id="vcLabel" maxlength="30" placeholder="留空自动起名" value="${esc(keep.label || '')}"></div>
          <div class="field"><label>录音</label>
            ${withSample.length ? `<div class="seg" id="cSrc"><button type="button" data-s="upload">上传新录音</button><button type="button" data-s="from">沿用已有音色的录音</button></div>` : ''}
            <div id="cSrcUpload" style="margin-top:6px"><input type="file" class="input vc-file" id="cFile" accept="audio/*,video/*"></div>
            <div id="cSrcFrom" style="margin-top:6px" hidden><select class="input" id="cFrom">${withSample.map((v) => `<option value="${esc(v.key)}" ${v.key === keep.from ? 'selected' : ''}>「${esc(v.label)}」的录音${v.sample_seconds ? `（${esc(v.sample_seconds)} 秒）` : ''}</option>`).join('')}</select></div>
          </div>
          <label class="row small"><input type="checkbox" id="cConsent"> 这是本人或已获授权的声音</label>
          <div class="row"><button class="btn primary" id="cGo">开始复刻</button><span class="small faint">约半分钟～两分钟</span></div>
        </div>`;
      const models = () => {
        const p = provOf($('#cProv').value); if (!p) return;
        $('#cModel').innerHTML = p.models.map(([m, l]) => `<option value="${esc(m)}">${esc(l)}</option>`).join('');
        $('#cModel').value = keep.prov === p.id && keep.model ? keep.model : p.default_model;
        $('#cNote').textContent = p.note || '';
        $('#cSlotWrap').style.display = (p.clone_opts || []).some((o) => o[0] === 'speaker_id') ? '' : 'none';
        $('#cNoKey').innerHTML = p.keys.length ? '' : `<div class="callout amber">${esc(p.label)} 还没配 API Key，复刻会失败。先在下面「语音厂商与 API Key」里添加。</div>`;
      };
      const src = () => {
        $$('#cSrc button').forEach((b) => b.classList.toggle('on', b.dataset.s === cloneSrc));
        $('#cSrcUpload').hidden = cloneSrc !== 'upload'; $('#cSrcFrom').hidden = cloneSrc !== 'from';
      };
      models(); src();
      $('#cProv').onchange = () => { keep.prov = null; models(); };
      $$('#cSrc button').forEach((b) => b.onclick = () => { cloneSrc = b.dataset.s; src(); });
      $('#cGo').onclick = (e) => busy(e.currentTarget, async () => {
        const f = $('#cFile').files[0], from = cloneSrc === 'from' ? $('#cFrom').value : '';
        if (!from && !f) throw new Error('先选录音文件，或沿用一个已有音色的录音');
        if (!$('#cConsent').checked) throw new Error('请先确认这是本人或已获授权的声音');
        const p = provOf($('#cProv').value) || {label: ''};
        const fd = new FormData();
        if (from) fd.append('from_voice', from); else fd.append('file', f);
        fd.append('label', $('#vcLabel').value.trim() || ((from ? (voiceOf(from) || {}).label + ' · ' : '') + p.label.replace(/\(.*\)$/, '') + ' ' + $('#cModel').value).slice(0, 30));
        fd.append('consent', '1'); fd.append('provider', $('#cProv').value); fd.append('model', $('#cModel').value);
        if ($('#cSlotWrap').style.display !== 'none' && $('#cSlot').value.trim()) fd.append('speaker_id', $('#cSlot').value.trim());
        toast('复刻中：整理样本、上传、创建音色…');
        try {
          const d = await A(id, '/api/tts/clone', {method: 'POST', body: fd});
          tts = Object.assign(tts, d); editing = null; $('#vcLabel').value = ''; $('#cFile').value = '';
          drawList(); drawClone(); toast(`已复刻，样本 ${d.sample_seconds} 秒。在上面列表里点「试听」`, 'ok');
          const row = $(`.vc-row[data-k="${CSS.escape(d.key)}"]`); if (row) { row.scrollIntoView({behavior: 'smooth', block: 'center'}); row.classList.add('flash'); }
        } finally { reloadProv(); }
      });
    }

    // ---------- 语音厂商与 API Key ----------
    async function reloadProv() { try { provs = (await A(id, '/api/voice/providers')).providers; drawProv(); } catch (e) { /**/ } }
    function drawProv() {
      $('#vcProv').innerHTML = `<div class="card-head"><h2>语音厂商与 API Key</h2><span class="small faint">key 只显示打码后的样子</span></div>
        <div class="card-body">
          <div class="muted small" style="margin-bottom:6px">复刻音色用哪家合成，就要配哪家的 key。同一家可以配多个 key 轮流用：限流的冷却 1 分钟，额度用完/欠费的冷却 6 小时（可点「恢复」），无效的停用到你恢复为止。
            「账号分组」相同的 key 共用复刻出来的音色；轮到别的分组时，会用保存的录音在那个账号下自动复刻一次（可按厂商关掉）。所有 key 都不能用时退回免费的微软音色。</div>
          ${provs.map(provHtml).join('')}
        </div>`;
      $$('.vc-prov', $('#vcProv')).forEach((box) => {
        const pid = box.dataset.p;
        box.onclick = (e) => { const b = e.target.closest('button[data-a]'); if (b) keyAction(b, pid, b.closest('[data-kid]')); };
        const au = $('[data-auto]', box);
        au.onchange = async () => {
          try { provs = (await A(id, '/api/voice/keys', {method: 'POST', json: {provider: pid, action: 'auto_enroll', on: au.checked}})).providers; drawProv(); toast('已保存', 'ok'); }
          catch (e) { toast(e.message, 'err'); au.checked = !au.checked; }
        };
      });
    }
    function provHtml(p) {
      const keys = p.keys.map((k) => {
        const [st, c] = KSTAT[k.status] || [k.status, ''];
        return `<div class="vc-key" data-kid="${esc(k.id)}">
          <code class="mono">${esc(k.masked)}</code>
          <div class="grow" style="min-width:160px"><div class="ellipsis">${esc(k.label || '未备注')} <span class="faint small">· 分组 ${esc(k.group || '（独立）')}</span></div>
            <div class="small faint ellipsis">成功 ${k.uses || 0} 次${k.fails ? ` · 失败 ${k.fails} 次` : ''}${k.last_ok ? ` · 最近成功 ${dt(k.last_ok)}` : ''}${k.error ? ` · ${esc(k.error.slice(0, 80))}` : ''}</div></div>
          <span class="badge ${c}">${esc(st)}${k.until && k.status !== 'auth' && k.status !== 'ok' ? ' 至 ' + dt(k.until) : ''}</span>
          <div class="row">${k.status !== 'ok' && k.status !== 'disabled' ? '<button class="btn sm" data-a="reset">恢复</button>' : ''}
            <button class="btn sm ghost" data-a="edit">编辑</button>
            <button class="btn sm ghost" data-a="${k.enabled ? 'disable' : 'enable'}">${k.enabled ? '停用' : '启用'}</button>
            <button class="btn sm ghost danger" data-a="delete">删除</button></div></div>`;
      }).join('');
      return `<div class="vc-prov" data-p="${esc(p.id)}">
        <div class="row wrap"><b>${esc(p.label)}</b>${p.keys.length ? `<span class="badge green">${p.keys.length} 个 key</span>` : '<span class="badge">未配置</span>'}
          ${p.model_bound ? '<span class="badge">音色绑定模型</span>' : ''}<span class="grow"></span>
          <label class="row small muted" title="轮到另一个账号分组的 key 时，用保存的录音自动复刻一次">换分组时自动复刻<label class="switch"><input type="checkbox" data-auto ${p.auto_enroll ? 'checked' : ''}><span></span></label></label></div>
        <div class="small faint" style="margin:4px 0 8px">${esc(p.note || '')}</div>
        ${keys}
        <div class="row" style="margin-top:6px"><button class="btn sm" data-a="add">＋ 添加 key</button></div>
      </div>`;
    }
    async function keyAction(b, pid, row) {
      const a = b.dataset.a, p = provOf(pid), k = row ? p.keys.find((x) => x.id === row.dataset.kid) : null;
      let body;
      if (a === 'add') {
        const groups = [...new Set(p.keys.map((x) => x.group).filter(Boolean))];
        const f = await modal({title: `添加 ${p.label} 的 API Key`, ok: '添加',
          body: '<div class="small muted" style="margin-bottom:12px">key 只保存在这个微信容器的数据卷里（权限 600），页面之后只显示打码后的样子。</div>',
          extra: `<div class="stack"><div class="field"><label>API Key</label><input class="input" type="password" name="key" autocomplete="off"></div>
            <div class="field"><label>备注</label><input class="input" name="label" placeholder="例如 主账号"></div>
            <div class="field"><label>账号分组（同一个厂商账号的 key 填一样）</label><input class="input" name="group" placeholder="${esc(groups[0] || 'default')}" list="vcGroups"><datalist id="vcGroups">${groups.map((g) => `<option value="${esc(g)}">`).join('')}</datalist></div></div>`});
        if (!f) return;
        if (!f.key.trim()) { toast('先填 API Key', 'err'); return; }
        body = {action: 'add', key: f.key.trim(), label: f.label.trim(), group: f.group.trim()};
      } else if (a === 'edit') {
        const f = await modal({title: `编辑 key ${k.masked}`, ok: '保存',
          extra: `<div class="stack"><div class="field"><label>备注</label><input class="input" name="label" value="${esc(k.label || '')}"></div>
            <div class="field"><label>账号分组</label><input class="input" name="group" value="${esc(k.group || '')}"></div>
            <div class="small faint">改分组后，这个 key 不再共用原分组复刻出来的音色。</div></div>`});
        if (!f) return;
        body = {action: 'update', id: k.id, label: f.label.trim(), group: f.group.trim()};
      } else if (a === 'reset') body = {action: 'reset', id: k.id};
      else if (a === 'enable' || a === 'disable') body = {action: 'update', id: k.id, enabled: a === 'enable'};
      else if (a === 'delete') {
        if (!await modal({title: `删除 key ${k.masked}？`, danger: true, ok: '删除', body: `${esc(p.label)} 的这个 key 会被删掉，不能恢复（只删本地保存的 key，不影响厂商那边的账号）。`})) return;
        body = {action: 'delete', id: k.id};
      } else return;
      await busy(b, async () => {
        provs = (await A(id, '/api/voice/keys', {method: 'POST', json: Object.assign({provider: pid}, body)})).providers;
        drawProv(); drawClone(); toast('已保存', 'ok');
      });
    }

    drawList(); drawSendAs(); drawClone(); drawProv();
  };

  // ======================================================================
  // 语音回复
  // ======================================================================
  WXPAGES.voicereply = async (view, id) => {
    head(id, '语音回复', `<a class="btn" href="#/a/${id}/voices">音色与复刻</a>`);
    loading(view);
    let vr, tts, names;
    try { [vr, tts, names] = await Promise.all([A(id, '/api/voice_reply'), A(id, '/api/tts/config'), loadNames(id)]); }
    catch (e) { fail(view, e); return; }
    let audioUrl = null; const audio = new Audio();
    onLeave(() => { audio.pause(); if (audioUrl) URL.revokeObjectURL(audioUrl); });

    const voiceOf = (k) => tts.voices.find((v) => v.key === k);
    const vName = (k) => (voiceOf(k) || {}).label || PRESET_LABELS[k] || k;
    const defLabel = () => vName(tts.default);
    const voiceOpts = (sel, first) => `<option value="" ${!sel ? 'selected' : ''}>${esc(first)}</option>` + tts.voices.map((v) =>
      `<option value="${esc(v.key)}" ${v.key === sel ? 'selected' : ''}>${v.provider ? '🎙 ' : ''}${esc(v.label)}${v.key === tts.default ? '（默认）' : ''}</option>`).join('') +
      (sel && !voiceOf(sel) ? `<option value="${esc(sel)}" selected>${esc(vName(sel))}（已不在列表）</option>` : '');
    const globalVoiceText = () => vr.voice ? vName(vr.voice) : `默认音色「${defLabel()}」`;

    view.innerHTML = `<div class="page stack" style="gap:16px;max-width:980px">
      ${vr.available ? '' : '<div class="callout amber">这个微信的版本没有「发语音」（需要 4.1.9+）：设置会保存，但机器人不会真的发语音条。</div>'}
      <div class="card" id="vrGlobal"></div>
      <div class="card" id="vrChats"></div>
      <details class="card vr-tips"><summary class="card-body"><b>机器人怎么判断要不要说语音</b></summary>
        <div class="card-body" style="padding-top:0"><ul class="small muted">
          <li>对方这一批消息里说「发语音 / 说给我听 / 想听你声音」，这条就用语音（字数放宽到约 150 字）；说「别发语音 / 打字」，这条就用文字。</li>
          <li>对方说「以后都用语音回我」或「以后别发语音了」，会自动记成这个人的单独设置（模式「总是语音」或「不发语音」），在下面列表里能看到、能改。</li>
          <li>战斗模式、接龙、发图的时候不发语音。超过最长字数的回复照常打字，免得太长听着累。</li>
          <li>语音条的发送形式（语音条 / 声波视频 / mp3 文件）在「音色与复刻」里设置。</li></ul></div></details>
    </div>`;

    // ---------- 全局 ----------
    let g = null;
    function drawGlobal() {
      g = {enabled: !!vr.enabled, mode: vr.mode, chance: vr.chance, voice: vr.voice || '', max_chars: vr.max_chars};
      const st = !vr.enabled ? '<span class="badge">已关闭</span>' : vr.mode === 'off' ? '<span class="badge">不发语音</span>' : `<span class="badge green">已开启 · ${esc(MODES[vr.mode][0])}</span>`;
      $('#vrGlobal').innerHTML = `<div class="card-head"><h2>全局设置</h2>${st}</div>
        <div class="card-body">
          <div class="setting"><div class="grow"><div class="t">让机器人用语音回复</div><div class="d">关掉后机器人只打字（单独设置了「开启」的好友除外）。</div></div>
            <label class="switch"><input type="checkbox" id="gOn" ${g.enabled ? 'checked' : ''}><span></span></label></div>
          <div class="setting" style="display:block"><div class="t" style="margin-bottom:8px">什么时候用语音</div>
            <div class="seg" id="gMode">${['request', 'auto', 'always', 'off'].map((m) => `<button data-m="${m}">${MODES[m][0]}</button>`).join('')}</div>
            <div class="small muted" id="gModeD" style="margin-top:8px"></div></div>
          <div class="setting" style="display:block"><div class="grid cols-3">
            <div class="field" id="gChanceF"><label>自动判断时说语音的概率 <span class="vc-val" id="gChanceT"></span></label><input type="range" id="gChance" min="0" max="100" step="5" value="${Math.round((g.chance || 0) * 100)}"></div>
            <div class="field"><label>最长字数（5～300）</label><input class="input" type="number" id="gMax" min="5" max="300" value="${esc(g.max_chars)}"></div>
            <div class="field"><label>音色</label><select class="input" id="gVoice">${voiceOpts(g.voice, `跟随默认音色（${defLabel()}）`)}</select></div>
          </div></div>
          <div class="row" style="padding-top:12px;border-top:1px solid var(--line-2)"><button class="btn primary" id="gSave">保存全局设置</button>
            <button class="btn ghost" id="gTry">▶ 试听这个音色</button><span class="small faint" id="gDirty"></span></div>
        </div>`;
      const mode = () => {
        $$('#gMode button').forEach((b) => b.classList.toggle('on', b.dataset.m === g.mode));
        $('#gModeD').textContent = MODES[g.mode] ? MODES[g.mode][1] : '';
        $('#gChanceF').style.opacity = g.mode === 'auto' ? 1 : .45;
        $('#gChanceT').textContent = $('#gChance').value + '%';
      };
      const dirty = () => {
        const same = g.enabled === !!vr.enabled && g.mode === vr.mode && Math.abs(g.chance - vr.chance) < 1e-6 && g.voice === (vr.voice || '') && +g.max_chars === +vr.max_chars;
        $('#gDirty').textContent = same ? '' : '有改动，记得保存';
      };
      mode();
      $('#gOn').onchange = (e) => { g.enabled = e.target.checked; dirty(); };
      $$('#gMode button').forEach((b) => b.onclick = () => { g.mode = b.dataset.m; mode(); dirty(); });
      $('#gChance').oninput = (e) => { g.chance = +e.target.value / 100; mode(); dirty(); };
      $('#gMax').oninput = (e) => { g.max_chars = e.target.value; dirty(); };
      $('#gVoice').onchange = (e) => { g.voice = e.target.value; dirty(); };
      $('#gTry').onclick = (e) => tryVoice(e.currentTarget, g.voice || tts.default);
      $('#gSave').onclick = (e) => busy(e.currentTarget, async () => {
        const n = parseInt(g.max_chars, 10);
        if (!(n >= 5 && n <= 300)) throw new Error('最长字数要在 5～300 之间');
        vr = await A(id, '/api/voice_reply', {method: 'POST', json: {global: {enabled: g.enabled, mode: g.mode, chance: g.chance, voice: g.voice, max_chars: n}}});
        drawGlobal(); drawChats(); toast('已保存', 'ok');
      });
    }
    async function tryVoice(btn, key) {
      await busy(btn, async () => {
        const url = await previewUrl(id, {text: '你好呀，这是我回你语音时的声音。', voice: key});
        if (audioUrl) URL.revokeObjectURL(audioUrl); audioUrl = url; audio.src = url; await audio.play().catch(() => {});
      });
    }

    // ---------- 按好友单独设置 ----------
    const chatAvatar = (u) => { const l = esc([...(who(u).name || '?').trim()][0] || '?');
      return `<img class="avatar" src="/a/${id}/api/avatar?username=${encodeURIComponent(u)}" onerror="this.outerHTML='<div class=&quot;avatar&quot;>${l}</div>'">`; };
    const who = (u) => names[u] || {name: u, sub: u.endsWith('@chatroom') ? '群' : u, group: u.endsWith('@chatroom')};
    function effective(c) {
      const en = c.enabled !== undefined ? c.enabled : vr.enabled;
      const m = c.mode !== undefined ? c.mode : vr.mode;
      const v = c.voice !== undefined ? c.voice : vr.voice;
      const vt = v ? `「${vName(v)}」` : `默认音色「${defLabel()}」`;
      if (!vr.available) return `这个微信发不了语音条；手动发语音时用${vt}`;
      if (!en || m === 'off') return `机器人不会用语音回他；手动给他发语音时用${vt}`;
      return `机器人${m === 'always' ? '每条都用语音回他' : m === 'auto' ? '自动判断要不要说语音' : '在他要求时说语音'}，用${vt}`;
    }
    function chipsHtml(c) {
      const out = [];
      if (c.voice !== undefined) out.push(c.voice ? `<span class="badge ${voiceOf(c.voice) ? 'blue' : 'red'}">🎙 音色：${esc(vName(c.voice))}${voiceOf(c.voice) ? '' : '（已删除）'}</span>` : `<span class="badge blue">🎙 音色：默认音色</span>`);
      if (c.mode !== undefined) out.push(`<span class="badge">模式：${esc((MODES[c.mode] || [c.mode])[0])}</span>`);
      if (c.enabled !== undefined) out.push(`<span class="badge ${c.enabled ? 'green' : 'amber'}">${c.enabled ? '开启' : '关闭'}</span>`);
      if (c.chance !== undefined) out.push(`<span class="badge">概率：${Math.round(c.chance * 100)}%</span>`);
      if (c.max_chars !== undefined) out.push(`<span class="badge">最长 ${esc(c.max_chars)} 字</span>`);
      return out.join('') || '<span class="badge">全部跟随全局</span>';
    }
    function drawChats() {
      const chats = Object.entries(vr.chats || {});
      $('#vrChats').innerHTML = `<div class="card-head"><h2>按好友单独设置 <span class="faint small" style="font-weight:400">${chats.length ? chats.length + ' 个' : ''}</span></h2>
          <button class="btn primary sm" id="vrAdd">＋ 为好友单独设置</button></div>
        <div class="card-body stack">
          <div class="callout blue"><div>给某个好友（或群）单独指定<b>音色</b>后，凡是对他说话的语音都用这个音色：机器人语音回复他时，以及你手动给他发语音、没有另选音色时（发送接口不带 voice 也一样）。
            模式和开关也可以单独设，没设的项跟随上面的全局设置。</div></div>
          ${chats.length ? `<div class="vr-list">${chats.map(([u, c]) => { const w = who(u); return `<div class="vr-item" data-u="${esc(u)}">
              ${chatAvatar(u)}
              <div class="grow"><div class="row wrap"><b class="ellipsis">${esc(w.name)}</b><span class="small faint mono ellipsis">${esc(w.group ? w.sub : u)}</span></div>
                <div class="chips" style="margin-top:6px">${chipsHtml(c)}</div>
                <div class="small muted" style="margin-top:6px">${esc(effective(c))}</div></div>
              <div class="row"><button class="btn sm" data-a="edit">编辑</button><button class="btn sm ghost danger" data-a="clear">移除</button></div>
            </div>`; }).join('')}</div>`
            : '<div class="empty" style="padding:28px 20px"><div class="big">🎙</div>还没有单独设置的好友。<br>点右上角「＋ 为好友单独设置」，比如让某位好友听到的是你复刻的声音。</div>'}
        </div>`;
      $('#vrAdd').onclick = async (e) => {
        const btn = e.currentTarget; btn.disabled = true;
        let c; try { c = await pickChat(id, {title: '为谁单独设置语音'}); } finally { btn.disabled = false; }
        if (!c) return;
        if (!names[c.username]) names[c.username] = {name: c.name, sub: c.is_group ? '群' : c.username, group: c.is_group};
        editChat(c.username);
      };
      $$('.vr-item', $('#vrChats')).forEach((row) => row.onclick = async (e) => {
        const b = e.target.closest('[data-a]'); if (!b) return;
        const u = row.dataset.u;
        if (b.dataset.a === 'edit') return editChat(u);
        if (!await modal({title: `移除「${who(u).name}」的单独设置？`, body: '移除后他的音色、模式、开关都跟随全局设置。', ok: '移除', danger: true})) return;
        busy(b, async () => { vr = await A(id, '/api/voice_reply', {method: 'POST', json: {chat: u, clear: true}}); drawChats(); toast('已移除', 'ok'); });
      });
    }
    async function editChat(u) {
      const cur = (vr.chats || {})[u] || {};
      const isNew = !(vr.chats || {})[u];
      const pick = (k) => cur[k] === undefined ? '__' : String(cur[k]);
      const sel = (name, opts, v) => `<select class="input" name="${name}">${opts.map(([k, l]) => `<option value="${esc(k)}" ${k === v ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
      const vsel = pick('voice');
      const voiceSel = `<select class="input" name="voice" id="ecVoice">` +
        [['__', `跟随全局（${globalVoiceText()}）`], ['', `默认音色（${defLabel()}）`],
          ...tts.voices.map((v) => [v.key, (v.provider ? '🎙 ' : '') + v.label]),
          ...(vsel !== '__' && vsel && !voiceOf(vsel) ? [[vsel, vName(vsel) + '（已不在列表）']] : [])]
          .map(([k, l]) => `<option value="${esc(k)}" ${k === vsel ? 'selected' : ''}>${esc(l)}</option>`).join('') + '</select>';
      const w = who(u);
      const p = modal({title: isNew ? `为「${w.name}」单独设置` : `编辑「${w.name}」的单独设置`, ok: '保存',
        body: `<div class="row" style="margin-bottom:14px">${chatAvatar(u)}
          <div class="grow"><b class="ellipsis">${esc(w.name)}</b><div class="small faint mono ellipsis">${esc(u)}</div></div></div>`,
        extra: `<div class="stack">
          <div class="field"><label>音色（对他说话都用这个声音）</label><div class="row">${voiceSel}<button type="button" class="btn" id="ecTry">▶ 试听</button></div></div>
          <div class="field"><label>什么时候用语音</label>${sel('mode', [['__', `跟随全局（${MODES[vr.mode] ? MODES[vr.mode][0] : vr.mode}）`], ...['request', 'auto', 'always', 'off'].map((m) => [m, MODES[m][0]])], pick('mode'))}</div>
          <div class="field"><label>语音回复开关</label>${sel('enabled', [['__', `跟随全局（${vr.enabled ? '开启' : '关闭'}）`], ['true', '对他开启'], ['false', '对他关闭']], pick('enabled'))}</div>
          <div class="small faint">选「跟随全局」的项不会单独保存，以后改全局设置时会跟着变。${vr.enabled ? '' : '<br>全局语音回复现在是关闭的：想让机器人对他说语音，把开关设成「对他开启」。'}</div>
        </div>`});
      const bg = [...document.querySelectorAll('.modal-bg')].pop();
      const tryBtn = bg && bg.querySelector('#ecTry');
      if (tryBtn) tryBtn.onclick = () => { const v = bg.querySelector('#ecVoice').value; tryVoice(tryBtn, v === '__' ? (vr.voice || tts.default) : (v || tts.default)); };
      const f = await p;
      audio.pause();
      if (!f) return;
      // 组装新的覆盖:只放没选「跟随全局」的项;chance/max_chars 等这里不改的原样保留
      const next = {};
      for (const [k, x] of Object.entries(cur)) if (!['voice', 'mode', 'enabled'].includes(k)) next[k] = x;
      if (f.voice !== '__') next.voice = f.voice;
      if (f.mode !== '__') next.mode = f.mode;
      if (f.enabled !== '__') next.enabled = f.enabled === 'true';
      const removed = Object.keys(cur).some((k) => !(k in next));
      try {
        if (!Object.keys(next).length) {
          if (!isNew) vr = await A(id, '/api/voice_reply', {method: 'POST', json: {chat: u, clear: true}});
          drawChats(); toast(isNew ? '全部跟随全局，没有保存' : '已改成全部跟随全局', 'ok'); return;
        }
        // 后端是合并保存:有项改回「跟随全局」时先清掉再写入剩下的
        if (removed) await A(id, '/api/voice_reply', {method: 'POST', json: {chat: u, clear: true}});
        vr = await A(id, '/api/voice_reply', {method: 'POST', json: {chat: u, set: next}});
        drawChats(); toast('已保存', 'ok');
      } catch (e) {
        toast(e.message, 'err');
        try { vr = await A(id, '/api/voice_reply'); drawChats(); } catch (x) { /**/ }
      }
    }

    drawGlobal(); drawChats();
  };
})();
