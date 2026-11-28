/* 直发测试:手动直发文字(可 @、可引用)/图片/视频/音频、撤回上一条、直发记录。见 console/PAGES.md
 * 发送方式开关在原生「发送设置」页;音色/语音回复在 voices.js。 */
(() => {
  const {$, $$, esc, A, toast, modal, setTop, avatar, acct, onLeave, pickChat, fmtTime} = WX;
  const AUDIO_EXT = /\.(mp3|m4a|wav|ogg|flac|amr)$/i;
  const IMG_TYPE = /^image\/(png|jpeg|gif|webp)$/;
  const VID_TYPE = /^video\/(mp4|quicktime)$/;
  const FILEHELPER = {username: 'filehelper', name: '文件传输助手', is_group: false};
  const KIND = {image: ['🖼', '图片'], video: ['🎬', '视频'], file: ['📎', '文件'], voice_bar: ['🎙', '语音条'], voice: ['🔊', '语音'], revoke: ['⤺', '撤回'], quote: ['↩', '引用回复'], text: ['💬', '']};
  const SRC = {bot: ['机器人', 'blue'], test: ['测试', 'amber'], manual: ['手动', '']};
  const fileKind = (f) => VID_TYPE.test(f.type) ? 'video' : (/^audio\//.test(f.type) || AUDIO_EXT.test(f.name || '')) ? 'audio' : IMG_TYPE.test(f.type) ? 'image' : 'file';   // 其它文件发文件卡片
  const isGroup = (chat) => /@chatroom$/.test(chat || '');
  const P = (id, u) => `/a/${id}/api/avatar?username=${encodeURIComponent(u)}`;

  WXPAGES.direct = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>直发测试</h1></div>`, `<a class="btn sm" href="#/a/${id}/send">发送设置</a>`);
    let st = {}, target = FILEHELPER, file = null, fileUrl = '', sending = false, hist = [], scope = 'all', alive = true;
    onLeave(() => { alive = false; if (fileUrl) URL.revokeObjectURL(fileUrl); });

    view.innerHTML = `<div class="page stack dr-page">
      <div class="card"><div class="card-body dr-status" id="drStatus"><span class="spinner"></span><span class="faint small">检查直发状态…</span></div></div>
      <div class="dr-grid">
        <div class="stack">
          <div class="card"><div class="card-head"><h2>发消息</h2><span class="faint small">在微信进程里直接调发送，不动界面</span></div>
            <div class="card-body stack">
              <div class="dr-target" id="drTarget"></div>
              <textarea class="input dr-text" id="drText" maxlength="2000" placeholder="输入要发的文字，可以直接粘贴截图&#10;Ctrl / ⌘ + Enter 发送"></textarea>
              <div class="dr-file hidden" id="drFile"></div>
              <div class="dr-opts">
                <div class="field hidden" id="drAtF"><label>@ 群成员</label><select class="input" id="drAt"><option value="">不 @ 人</option></select></div>
                <div class="field grow" id="drQuoteF"><label>引用回复</label><select class="input" id="drQuote"><option value="">不引用</option></select></div>
              </div>
              <div class="faint small" id="drOptHint"></div>
              <div class="row dr-actions">
                <button class="btn primary" id="drSend" disabled>发送</button>
                <button class="btn" id="drTts" disabled title="把输入框里的文字用音色读出来，发成语音">🔊 发成语音</button>
                <button class="btn" id="drPick" title="图片(20MB 内)、MP4 / MOV 视频(25MB 内)、音频，或任意文件(100MB 内，发文件卡片)">＋ 图片 / 视频 / 文件</button>
                <input type="file" id="drInput" hidden>
                <span class="grow"></span>
                <button class="btn danger" id="drRevoke" disabled>撤回上一条</button>
              </div>
              <div class="dr-result small hidden" id="drResult"></div>
            </div></div>
          <div class="callout blue small">「已交给微信发送」= 微信已经收下这条。文字基本等于发出；图片、视频还要等微信后台上传，有没有真正送达请在手机或「扫码与桌面」里看有没有红色感叹号。音频按「音色与复刻」里的发送形式发成声波视频或 mp3 文件。</div>
        </div>
        <div class="card dr-hist"><div class="card-head"><h2>直发记录</h2>
          <div class="seg" id="drScope"><button data-s="all" class="on">全部</button><button data-s="chat">当前对象</button></div></div>
          <div class="dr-hist-list" id="drHist"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>
          <div class="dr-hist-foot faint small">最近 50 条，含机器人自动直发的回复；点一条可把它的会话设为发送对象</div></div>
      </div></div>`;

    const ready = () => !!(st.attached && st.logged_in);
    function upd() {
      const has = $('#drText').value.trim() || file;
      $('#drSend').disabled = !(ready() && target && !sending && has);
      $('#drTts').disabled = !(ready() && target && !sending && $('#drText').value.trim());
      const noRevoke = target.username === 'filehelper' ? '文件传输助手不支持撤回' : !st.revoke ? '这个微信版本不支持撤回' : '';
      $('#drRevoke').disabled = !(ready() && !sending) || !!noRevoke;
      $('#drRevoke').title = noRevoke || '撤回我在这个会话里最近发的一条(只能撤 2 分钟内的)';
    }
    function result(text, kind = '') { const r = $('#drResult'); r.className = 'dr-result small ' + kind + (text ? '' : ' hidden'); r.textContent = text; }

    // ---------- 状态 ----------
    async function loadStatus() {
      try { st = await A(id, '/api/direct/status'); } catch (e) { st = {error: e.message}; }
      if (!alive) return;
      const b = (ok, t, warn) => `<span class="badge ${ok ? 'green' : warn ? 'amber' : 'red'}">${ok ? '✓' : '✗'} ${esc(t)}</span>`;
      const caps = ['at:@ 提醒', 'quote:引用', 'revoke:撤回', 'voice:语音条'].map((x) => { const [k, l] = x.split(':'); return `<span class="badge ${st[k] ? 'green' : ''}">${st[k] ? '✓' : '–'} ${l}</span>`; }).join('');
      const mode = (acct(id) || {}).send_mode;
      $('#drStatus').innerHTML = `
        ${st.supported ? b(true, '微信 ' + st.version) : b(false, st.sha256 ? '微信版本未适配' : '找不到微信')}
        ${b(st.attached, st.attached ? '已连接 PID ' + st.pid : '未连接')}
        ${b(st.logged_in, st.logged_in ? '已登录' : '未登录')}
        <span class="dr-sep"></span>${caps}
        <span class="grow"></span>
        <span class="faint small">机器人回复现在走</span><span class="badge ${mode === 'direct' ? 'blue' : ''}">${mode === 'direct' ? '直发' : '界面发送'}</span>
        ${st.error ? `<div class="dr-status-err small">${esc(st.error)}</div>` : ''}`;
      drawOpts(); upd();
    }

    // ---------- 发送对象 ----------
    function drawTarget() {
      const t = target;
      $('#drTarget').innerHTML = `<img class="avatar" src="${P(id, t.username)}" onerror="this.style.visibility='hidden'">
        <div class="grow" style="min-width:0"><div class="row" style="gap:8px"><b class="ellipsis">${esc(t.name || t.username)}</b>
          <span class="badge ${t.is_group ? 'blue' : ''}">${t.is_group ? '群' : '私聊'}</span></div>
          <div class="faint small mono ellipsis">${esc(t.username)}</div></div>
        ${t.username !== 'filehelper' ? '<button class="btn sm ghost" id="drFh" title="切回文件传输助手(给自己发,最安全的测试对象)">文件传输助手</button>' : ''}
        <button class="btn sm" id="drChange">更换</button>`;
      $('#drChange').onclick = async () => { const x = await pickChat(id, {title: '发给谁'}).catch((e) => { toast(e.message, 'err'); return null; }); if (x) setTarget(x); };
      if ($('#drFh')) $('#drFh').onclick = () => setTarget(FILEHELPER);
    }
    function setTarget(x) {
      target = {username: x.username, name: x.name || x.username, is_group: !!x.is_group};
      drawTarget(); loadExtra(); result(''); upd(); if (scope === 'chat') drawHist(); $('#drText').focus();
    }
    function drawOpts() {
      $('#drAtF').classList.toggle('hidden', !target.is_group);
      $('#drAt').disabled = !st.at; $('#drQuote').disabled = !st.quote;
      const tips = [];
      if (target.is_group) tips.push(st.at ? '正文里直接写「@成员名」也会变成真 @' : '这个微信版本不支持 @ 提醒');
      if (!st.quote) tips.push('这个微信版本不支持引用回复');
      $('#drOptHint').textContent = tips.join('；');
      $('#drOptHint').classList.toggle('hidden', !tips.length);
    }
    async function loadExtra() {
      const t = target; drawOpts();
      $('#drAt').innerHTML = '<option value="">不 @ 人</option>';
      $('#drQuote').innerHTML = '<option value="">加载最近消息…</option>';
      const [ms, mem] = await Promise.all([
        A(id, `/api/messages?chat=${encodeURIComponent(t.username)}&limit=30`).catch(() => []),
        t.is_group ? A(id, `/api/groups/${encodeURIComponent(t.username)}/members`).catch(() => []) : []]);
      if (!alive || target !== t) return;
      const who = (m) => m.is_self ? '我' : (m.sender_name || m.sender || t.name);
      const list = (Array.isArray(ms) ? ms : []).filter((m) => m.type !== 10000 && m.local_id).reverse();
      $('#drQuote').innerHTML = `<option value="">${list.length ? '不引用' : '不引用(没有最近消息)'}</option>` + list.map((m) =>
        `<option value="${m.local_id}">${esc(fmtTime(m.create_time))}  ${esc(who(m))}：${esc(String(m.content || '[' + (m.type_name || m.type) + ']').replace(/\s+/g, ' ').slice(0, 40))}</option>`).join('');
      if (t.is_group) {
        const nm = (m) => m.group_nick || m.name || m.nick_name || m.wxid;
        $('#drAt').innerHTML = '<option value="">不 @ 人</option>' + (Array.isArray(mem) ? mem : []).filter((m) => m.wxid)
          .map((m) => `<option value="${esc(m.wxid)}">${esc(nm(m))}</option>`).join('');
      }
    }

    // ---------- 附件 ----------
    function setFile(f) {
      if (f) {
        const k = fileKind(f);
        const lim = {video: 25, file: 100}[k] || 20;
        if (f.size > lim * 1024 * 1024) { result(`✗ ${{video: '视频', file: '文件'}[k] || '文件'}超过 ${lim}MB`, 'err'); return; }
      }
      if (fileUrl) { URL.revokeObjectURL(fileUrl); fileUrl = ''; }
      file = f || null;
      const box = $('#drFile');
      if (!file) { box.classList.add('hidden'); box.innerHTML = ''; $('#drInput').value = ''; upd(); return; }
      const k = fileKind(file);
      if (k === 'image') fileUrl = URL.createObjectURL(file);
      const ico = {video: '🎬', audio: '🔊', image: '', file: '📄'}[k];
      box.innerHTML = `${fileUrl ? `<img src="${fileUrl}" alt="">` : `<div class="dr-file-ico">${ico}</div>`}
        <div class="grow" style="min-width:0"><div class="ellipsis">${esc(file.name || '粘贴的图片')}</div>
        <div class="faint small">${{image: '图片', video: '视频', audio: '音频', file: '文件'}[k]} · ${Math.ceil(file.size / 1024)} KB</div></div>
        <button class="btn sm ghost" id="drRm">移除</button>`;
      box.classList.remove('hidden'); $('#drRm').onclick = () => setFile(null);
      result(''); upd();
    }
    $('#drPick').onclick = () => $('#drInput').click();
    $('#drInput').onchange = (e) => setFile(e.target.files[0]);
    $('#drText').addEventListener('paste', (e) => {
      const it = [...((e.clipboardData && e.clipboardData.items) || [])].find((x) => x.kind === 'file' && x.type.startsWith('image/'));
      if (it) { e.preventDefault(); setFile(it.getAsFile()); }
    });
    // 也支持把文件拖进输入框
    $('#drText').addEventListener('dragover', (e) => e.preventDefault());
    $('#drText').addEventListener('drop', (e) => { const f = e.dataTransfer && e.dataTransfer.files[0]; if (f) { e.preventDefault(); setFile(f); } });

    // ---------- 发送 / 撤回 ----------
    async function doSend() {
      if ($('#drSend').disabled) return;
      const content = $('#drText').value, f = file, t = target;
      sending = true; upd(); result('发送中…');
      const done = [];
      try {
        if (f) {
          const fd = new FormData(); fd.append('chat', t.username); fd.append('name', t.name); fd.append('file', f, f.name || 'paste.png');
          await A(id, '/api/direct/send_image', {method: 'POST', body: fd});
          done.push({image: '图片', video: '视频', audio: '语音', file: '文件'}[fileKind(f)]); setFile(null);
        }
        if (content.trim()) {
          const body = {chat: t.username, name: t.name, content};
          if ($('#drAt').value && t.is_group) body.at = [$('#drAt').value];
          if ($('#drQuote').value) body.quote = +$('#drQuote').value;
          await A(id, '/api/direct/send', {method: 'POST', json: body});
          done.push(body.quote ? '引用回复' : body.at ? '@ 消息' : '文字');
          $('#drText').value = ''; $('#drAt').value = ''; $('#drQuote').value = '';
        }
        result('✓ ' + done.join('和') + '已交给微信发送', 'ok'); toast('已交给微信发送', 'ok');
      } catch (e) {
        result((done.length ? '✓ ' + done.join('和') + '已发；' : '') + '✗ ' + (e.message || '网络错误'), 'err');
      }
      sending = false; upd(); loadHistory(); loadStatus(); setTimeout(loadExtra, 1500);
    }
    $('#drSend').onclick = doSend;
    // 发成语音:音色 = 这个好友在「语音回复」里单独设置的音色,没有就用默认音色;发送形式按「音色与复刻」里的设置
    $('#drTts').onclick = () => busy($('#drTts'), async () => {
      const t = target, text = $('#drText').value.trim(); if (!text) return;
      let voiceNote = '默认音色';
      try {
        const [vr, tc] = await Promise.all([A(id, '/api/voice_reply'), A(id, '/api/tts/config')]);
        const v = ((vr.chats || {})[t.username] || {}).voice;
        if (v) voiceNote = '这个好友专用的「' + (((tc.voices || []).find((x) => x.key === v) || {}).label || v) + '」';
      } catch (e) { /* 读不到就按默认 */ }
      if (!await WX.modal({title: '发成语音？', body: `把这段文字读出来发给「${esc(t.name)}」，用${esc(voiceNote)}。`, ok: '发送'})) return;
      sending = true; upd(); result('合成并发送中…');
      try {
        const r = await A(id, '/api/direct/send_tts', {method: 'POST', json: {chat: t.username, name: t.name, text}});
        result('✓ 语音' + (r.message ? '：' + r.message : '已交给微信发送'), 'ok'); $('#drText').value = '';
      } catch (e) { result('✗ ' + e.message, 'err'); }
      sending = false; upd(); loadHistory();
    });
    $('#drText').oninput = upd;
    $('#drText').onkeydown = (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doSend(); } };
    $('#drRevoke').onclick = async () => {
      if ($('#drRevoke').disabled) return;
      const t = target;
      if (!await modal({title: '撤回上一条？', body: `撤回我发给「${esc(t.name || t.username)}」的最近一条消息。只能撤 2 分钟内、自己发的。`, ok: '撤回', danger: true})) return;
      sending = true; upd(); result('撤回中…');
      try { await A(id, '/api/direct/revoke', {method: 'POST', json: {chat: t.username, name: t.name}}); result('✓ 已撤回', 'ok'); toast('已撤回', 'ok'); }
      catch (e) { result('✗ ' + (e.message || '网络错误'), 'err'); }
      sending = false; upd(); loadHistory(); setTimeout(loadExtra, 1500);
    };

    // ---------- 记录 ----------
    async function loadHistory() {
      try { hist = (await A(id, '/api/direct/history')).history || []; } catch (e) { if (alive) $('#drHist').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      if (alive) drawHist();
    }
    function drawHist() {
      const list = scope === 'chat' ? hist.filter((x) => x.chat === target.username) : hist;
      $('#drHist').innerHTML = list.map((x, i) => {
        const [ico, kl] = KIND[x.kind] || ['💬', x.kind || ''];
        const [sl, sc] = SRC[x.source] || [x.source || '', ''];
        const okTxt = x.ok ? (x.kind === 'revoke' ? '✓ 已撤回' : '✓ 已交给微信') : '✗ ' + (x.error || x.code || '失败');
        const extra = x.seconds ? ` · ${x.seconds} 秒` : x.size ? ` · ${Math.ceil(x.size / 1024)} KB` : '';
        const atTag = x.at && x.at.length ? `<span class="badge">@ ${x.at.length} 人</span>` : '';
        return `<div class="dr-h" data-i="${hist.indexOf(x)}" title="设为发送对象">
          <div class="dr-h-ico">${ico}</div>
          <div class="grow" style="min-width:0">
            <div class="row dr-h-top"><b class="ellipsis">${esc(x.name || x.chat)}</b>${sl ? `<span class="badge ${sc}">${esc(sl)}</span>` : ''}${atTag}<span class="grow"></span><span class="faint small">${esc(fmtTime(x.t))}</span></div>
            <div class="dr-h-text">${kl ? `<span class="faint">${esc(kl)}${esc(extra)}${x.content && x.content !== kl ? ' · ' : ''}</span>` : ''}${x.content !== kl ? esc(x.content || '') : ''}</div>
            <div class="small ${x.ok ? 'dr-ok' : 'dr-err'}">${esc(okTxt)}</div>
          </div></div>`;
      }).join('') || `<div class="empty">${scope === 'chat' ? '这个对象还没有直发记录' : '还没有记录'}</div>`;
      $$('.dr-h', $('#drHist')).forEach((el) => el.onclick = () => {
        const x = hist[+el.dataset.i]; if (!x || x.chat === target.username) return;
        setTarget({username: x.chat, name: x.name || x.chat, is_group: isGroup(x.chat)});
      });
    }
    $$('#drScope button').forEach((b) => b.onclick = () => { scope = b.dataset.s; $$('#drScope button').forEach((x) => x.classList.toggle('on', x === b)); drawHist(); });

    drawTarget(); upd();
    await Promise.all([loadStatus(), loadHistory(), loadExtra()]);
    const t1 = setInterval(() => { if (!sending) loadHistory(); }, 6000);
    const t2 = setInterval(loadStatus, 20000);
    onLeave(() => { clearInterval(t1); clearInterval(t2); });
  };
})();
