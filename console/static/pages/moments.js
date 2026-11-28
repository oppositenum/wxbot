/* 朋友圈:动态 / 发布与草稿 / 执行记录 / 设置。接口见 docs/moments.md,约定见 console/PAGES.md
 * 所有写接口都要带 GET /api/moments 返回的 session;账号切换或后台重启后返回 409,页面会重新加载。 */
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct, onLeave, pickChat, lightbox} = WX;

  const PAGE = 20;
  const MOODS = ['喜悦', '愤怒', '轻微烦躁', '平静', '趣事', '随想'];
  const JOB_STATE = {
    queued: ['等待处理', 'blue'], preparing: ['准备中', 'blue'], initiated: ['正在核对回执', 'blue'],
    confirmed: ['已确认', 'green'], uncertain: ['待人工核对', 'amber'], failed: ['失败', 'red'],
    skipped: ['已跳过', ''], cancelled: ['已取消', ''],
  };
  const JOB_KIND = {publish: '发布朋友圈', comment: '评论 / 回复', like: '点赞', chat: '主动私聊'};
  const JOB_ORIGIN = {manual: '手动', automatic: '自动', reflection: '聊天感悟'};
  const DRAFT_STATUS = {draft: ['未发送', ''], confirmed: ['已发送', 'green'], failed: ['失败', 'red'], cancelled: ['已取消', ''],
    discarded: ['已丢弃', ''], queued: ['排队中', 'blue'], uncertain: ['待核对', 'amber']};

  // 设置表单的字段:[键, 类型]。保存时只把和加载值不同的字段放进 patch(后端支持部分更新)。
  const FIELDS = {
    sync_enabled: 'bool', sync_interval_minutes: 'int', quiet_start: 'time', quiet_end: 'time', late_night_override: 'bool',
    auto_comment: 'bool', auto_like: 'bool', auto_chat_after_like: 'bool',
    daily_comment_limit: 'int', daily_like_limit: 'int', daily_chat_limit: 'int', min_interval_minutes: 'int',
    auto_publish: 'bool', chat_reflection: 'bool', publish_interval_minutes: 'int', daily_publish_limit: 'int',
    publish_images: 'bool', publish_web_opinions: 'bool',
  };

  const fullTime = (ts) => ts ? new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(ts * 1000)) : '—';
  function ago(ts) {
    if (!ts) return '—';
    const s = Date.now() / 1000 - ts;
    if (s < 60) return '刚刚';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    if (s < 86400 * 30) return Math.floor(s / 86400) + ' 天前';
    return fullTime(ts).slice(0, 10);
  }

  WXPAGES.moments = async (view, id) => {
    const s = acct(id);
    const P = (p) => `/a/${id}${p}`;
    const av = (u, cls = 'sm') => `<img class="avatar ${cls}" src="${P('/api/avatar?username=' + encodeURIComponent(u))}" onerror="this.style.visibility='hidden'">`;

    // ---------- 状态 ----------
    let session = null, cfg = null, caps = {}, status = {}, quietNow = false;
    let feed = {items: [], total: 0, authors: [], last_sync: null}, offset = 0;
    let active = null;                 // 当前查看的动态(详情)
    let aiBusy = false, commentRev = 0;
    let assets = [], edit = null;      // 发布编辑器:图片 asset id、正在编辑的草稿
    let draftsCache = [], jobsCache = [], jobFilter = 'all';
    let names = {};                    // wxid -> 显示名(通讯录 + 动态里出现过的名字)
    let allow = [];                    // 好友范围编辑中的值
    let tab = sessionStorage.getItem('mo-tab') || 'feed';

    setTop(`<div class="row">${avatar(s, 'sm')}<h1>朋友圈</h1><span id="moTopBadge"></span></div>`,
      `<button class="btn" id="moReload">刷新页面数据</button><button class="btn primary" id="moSync">刷新微信动态</button>`);
    view.innerHTML = `<div class="page stack mo" style="gap:16px">
      <div class="row wrap"><div class="seg" id="moTabs">
        <button data-t="feed">动态</button><button data-t="compose">发布与草稿</button><button data-t="jobs">执行记录</button><button data-t="settings">设置</button></div>
        <span class="grow"></span><span class="faint small" id="moStatusLine"></span></div>
      <div id="moAlert"></div>
      <section data-pane="feed"></section><section data-pane="compose" hidden></section>
      <section data-pane="jobs" hidden></section><section data-pane="settings" hidden></section></div>`;
    const pane = (k) => $(`[data-pane="${k}"]`, view);

    // ---------- 请求 ----------
    const isStale = (e) => /账号已变化|账号已切换|重新加载/.test(e.message || '');
    async function post(path, json = {}) {
      try { return await A(id, path, {method: 'POST', json: Object.assign({}, json, {session})}); }
      catch (e) { if (isStale(e)) { toast('账号或后台已变化，已重新加载；刚才的操作没有执行', 'err'); await loadCatalog().catch(() => {}); throw new Error('已重新加载'); } throw e; }
    }
    const sq = () => 'session=' + encodeURIComponent(JSON.stringify(session));
    const assetSrc = (aid) => P(`/api/moments/assets/${encodeURIComponent(aid)}?${sq()}`);
    const nameOf = (u) => names[u] || u || '';

    async function loadNames() {
      try {
        const cs = await A(id, '/api/contacts');
        for (const c of cs) names[c.username] = c.remark || c.name || c.nick_name || c.username;
      } catch (e) { /* 只影响显示名 */ }
      if (s.wxid && !names[s.wxid]) names[s.wxid] = s.name || '我';
    }

    // ---------- 顶部状态 ----------
    function drawStatus() {
      const run = status.automation_running;
      $('#moTopBadge').innerHTML = `<span class="badge ${run ? 'green' : ''}"><span class="dot ${run ? 'green' : 'gray'}"></span>自动服务${run ? '运行中' : '未运行'}</span>`;
      const on = cfg ? [['auto_comment', '自动评论'], ['auto_like', '自动点赞'], ['auto_chat_after_like', '主动私聊'], ['auto_publish', '定时分享'], ['chat_reflection', '聊天感悟']]
        .filter(([k]) => cfg[k]).map(([, l]) => l) : [];
      $('#moStatusLine').textContent = `${on.length ? '已开启：' + on.join('、') : '自动互动全部关闭'} · ${quietNow ? '现在是免打扰时段' : '不在免打扰时段'} · 最近检查 ${ago(status.last_worker_tick)}`;
      const errs = [];
      if (status.worker_error) errs.push('自动服务出错：' + status.worker_error);
      if (status.sync_error) errs.push('同步出错：' + (status.sync_error.message || status.sync_error));
      if (caps.send === false) errs.push('当前不能发送/自动互动：' + (caps.reason || '微信界面不可用') + '（读取与草稿不受影响）');
      $('#moAlert').innerHTML = errs.map((x) => `<div class="callout amber" style="margin-bottom:8px">${esc(x)}</div>`).join('');
    }

    // ---------- 标签页 ----------
    function showTab(k) {
      tab = k; sessionStorage.setItem('mo-tab', k);
      $$('#moTabs button').forEach((b) => b.classList.toggle('on', b.dataset.t === k));
      $$('[data-pane]', view).forEach((p) => { p.hidden = p.dataset.pane !== k; });
      if (k === 'compose') loadDrafts();
      if (k === 'jobs') loadJobs();
      if (k === 'settings') drawSettings();
    }
    $$('#moTabs button').forEach((b) => b.onclick = () => showTab(b.dataset.t));

    // =====================================================================
    // 动态
    // =====================================================================
    pane('feed').innerHTML = `<div class="mo-split">
      <div class="stack" style="gap:12px;min-width:0">
        <form class="row wrap mo-toolbar" id="moFilter">
          <input class="input grow" id="moSearch" placeholder="搜索动态文字或昵称" style="min-width:180px">
          <select class="input" id="moAuthor" style="width:220px"><option value="">所有作者</option></select>
          <button class="btn" type="submit">筛选</button></form>
        <div class="faint small" id="moSyncMeta">读取中…</div>
        <div id="moFeed" class="stack" style="gap:12px"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>
        <div class="row" id="moPager"><button class="btn sm" id="moPrev">上一页</button><span class="grow faint small" style="text-align:center" id="moPage"></span><button class="btn sm" id="moNext">下一页</button></div>
      </div>
      <aside class="card mo-inspector" id="moInspector"><div class="card-head"><h2>评论与回复</h2></div>
        <div class="card-body"><div class="empty" style="padding:30px 10px">在左边点一条动态的「查看与评论」，这里显示点赞、评论，并可以写评论或回复。</div></div></aside></div>`;

    function mediaHtml(item) {
      const ms = item.media || [];
      if (!ms.length) return '';
      const n = Math.min(ms.length, 9);
      const cls = n === 1 ? 'one' : (n === 4 || n === 2) ? 'two' : 'three';
      let imgIdx = 0;          // 后端按「第几张图片」取图(视频不算)
      const cells = ms.slice(0, 9).map((m) => {
        const video = m.type === '6' || m.duration;
        const ratio = n === 1 && m.width && m.height ? `aspect-ratio:${m.width}/${m.height}` : '';
        const tip = m.width && m.height ? m.width + '×' + m.height : '';
        if (video) return `<div class="mo-media-cell" style="${ratio}" title="${tip}">▶${m.duration ? ' ' + m.duration + 's' : ''}</div>`;
        const src = `/a/${id}/api/moments/media/${encodeURIComponent(item.id)}/${imgIdx++}`;
        return `<div class="mo-media-cell" style="${ratio}" title="${tip}"><img class="mo-media-img" loading="lazy" src="${src}" data-lb onerror="this.parentNode.title='这张图微信加密了，暂时不能预览';this.replaceWith(document.createTextNode('🔒'))"></div>`;
      }).join('');
      return `<div class="mo-media ${cls}">${cells}</div>`;
    }
    function commentLine(c, item) {
      const who = esc(c.name || nameOf(c.author));
      let target = '';
      if (c.reply_to) {
        const t = (item.comments || []).find((x) => x.author === c.reply_to);
        target = ` <span class="faint">回复</span> <b>${esc(t ? (t.name || nameOf(t.author)) : nameOf(c.reply_to))}</b>`;
      }
      return `<div class="mo-c"><b>${who}</b>${target}：${esc(c.text)}</div>`;
    }
    function postHtml(item) {
      const likes = item.likes || [], comments = item.comments || [];
      const own = session && item.author === session.account;
      const body = item.text || item.title || '';
      const extra = item.text && item.title ? `<div class="mo-link">🔗 ${esc(item.title)}</div>` : '';
      const social = likes.length || comments.length ? `<div class="mo-social">
          ${likes.length ? `<div class="mo-likes">♡ ${likes.map((l) => esc(l.name || nameOf(l.author))).join('，')}</div>` : ''}
          ${comments.length ? `<div class="mo-comments">${comments.slice(0, 6).map((c) => commentLine(c, item)).join('')}${comments.length > 6 ? `<div class="faint small">还有 ${comments.length - 6} 条…</div>` : ''}</div>` : ''}
        </div>` : '';
      return `<article class="card mo-post ${active && active.id === item.id ? 'on' : ''}" data-fid="${esc(item.id)}">
        ${av(item.author, '')}
        <div class="grow" style="min-width:0">
          <div class="row"><span class="mo-name">${esc(item.name || nameOf(item.author))}</span>${own ? '<span class="badge green">我</span>' : ''}</div>
          ${body ? `<div class="mo-text">${esc(body)}</div>` : '<div class="mo-text faint">图片 / 视频动态</div>'}
          ${extra}${mediaHtml(item)}
          <div class="row mo-foot"><span class="faint small grow" title="${esc(fullTime(item.created))}">${esc(ago(item.created))}</span>
            <span class="faint small">${likes.length} 赞 · ${comments.length} 评论</span>
            <button class="btn sm ghost" data-open="${esc(item.id)}">查看与评论 →</button></div>
          ${social}
        </div></article>`;
    }
    function drawFeed() {
      const sel = $('#moAuthor'), cur = sel.value;
      const label = new Map(feed.items.map((x) => [x.author, x.name]));
      sel.innerHTML = '<option value="">所有作者</option>' + feed.authors.map((a) =>
        `<option value="${esc(a.author)}">${esc((label.get(a.author) || nameOf(a.author)) + ' · ' + a.count)}</option>`).join('');
      sel.value = cur;
      const ls = feed.last_sync;
      $('#moSyncMeta').textContent = ls ? `最近同步 ${fullTime(ls.at)}${Date.now() / 1000 - ls.at < 86400 * 30 ? '（' + ago(ls.at) + '）' : ''} · 共 ${feed.total} 条匹配 · 缓存只包含微信里已加载过的动态，不代表全部历史${ls.invalid ? ' · ' + ls.invalid + ' 条格式暂未识别' : ''}`
        : '还没有缓存。点右上角「刷新微信动态」读取一次。';
      $('#moFeed').innerHTML = feed.items.length ? feed.items.map(postHtml).join('')
        : `<div class="card empty"><div class="big">🖼</div>${ls ? '没有符合条件的动态，换个筛选条件试试。' : '暂时没有动态。点右上角「刷新微信动态」读取。'}</div>`;
      $$('[data-open]', $('#moFeed')).forEach((b) => b.onclick = () => openFeed(b.dataset.open));
      $$('img[data-lb]', $('#moFeed')).forEach((i) => i.onclick = () => lightbox(i.src));
      $('#moPrev').disabled = offset === 0; $('#moNext').disabled = offset + PAGE >= feed.total;
      $('#moPage').textContent = feed.total ? `${offset + 1}–${Math.min(offset + PAGE, feed.total)} / ${feed.total}` : '';
      $('#moPager').hidden = feed.total <= PAGE;
    }
    async function loadCatalog() {
      const q = `?limit=${PAGE}&offset=${offset}&author=${encodeURIComponent($('#moAuthor').value)}&search=${encodeURIComponent($('#moSearch').value.trim())}`;
      const d = await A(id, '/api/moments' + q);
      session = d.session; caps = d.capabilities || {}; status = d.status || {}; quietNow = !!d.quiet_now;
      feed = {items: d.items || [], total: d.total || 0, authors: d.authors || [], last_sync: d.last_sync};
      for (const it of feed.items) {
        if (it.name && !names[it.author]) names[it.author] = it.name;
        for (const c of [...(it.comments || []), ...(it.likes || [])]) if (c.name && !names[c.author]) names[c.author] = c.name;
      }
      const fresh = !cfg || cfg.revision !== d.settings.revision;
      cfg = d.settings;
      if (fresh && tab === 'settings' && !dirtyCount()) drawSettings();
      drawStatus(); drawFeed();
    }
    $('#moFilter').onsubmit = (e) => { e.preventDefault(); offset = 0; loadCatalog().catch((x) => toast(x.message, 'err')); };
    $('#moAuthor').onchange = () => { offset = 0; loadCatalog().catch((x) => toast(x.message, 'err')); };
    $('#moPrev').onclick = () => { offset = Math.max(0, offset - PAGE); loadCatalog().catch((x) => toast(x.message, 'err')); };
    $('#moNext').onclick = () => { offset += PAGE; loadCatalog().catch((x) => toast(x.message, 'err')); };

    // ---------- 详情 + 评论 ----------
    async function openFeed(fid) {
      if (tab !== 'feed') showTab('feed');
      const box = $('#moInspector');
      box.querySelector('.card-body').innerHTML = '<div class="empty"><span class="spinner" style="margin:auto"></span></div>';
      let d;
      try { d = await A(id, `/api/moments/feed/${encodeURIComponent(fid)}?${sq()}`); }
      catch (e) { box.querySelector('.card-body').innerHTML = `<div class="callout red">${esc(e.message)}</div>`; return; }
      active = d.item;
      $$('.mo-post', view).forEach((p) => p.classList.toggle('on', p.dataset.fid === active.id));
      drawInspector();
      if (innerWidth <= 1000) box.scrollIntoView({behavior: 'smooth', block: 'start'});
    }
    function drawInspector() {
      const it = active, own = it.author === session.account;
      const comments = it.comments || [], likes = it.likes || [];
      const replyable = comments.filter((c) => c.id && c.id !== '0');
      const others = replyable.filter((c) => c.author !== session.account).sort((a, b) => a.created - b.created);
      const box = $('#moInspector');
      box.querySelector('.card-head').innerHTML = `<h2>评论与回复</h2><button class="btn sm ghost" id="moCloseInsp">✕</button>`;
      box.querySelector('.card-body').innerHTML = `
        <div class="row" style="align-items:flex-start;gap:10px">${av(it.author)}<div class="grow" style="min-width:0">
          <div class="mo-name">${esc(it.name || nameOf(it.author))}${own ? ' <span class="badge green">我的动态</span>' : ''}</div>
          <div class="faint small">${esc(fullTime(it.created))}</div></div></div>
        <div class="mo-quote">${esc(it.text || it.title || '图片 / 视频动态')}</div>
        ${likes.length ? `<div class="small muted" style="margin-bottom:8px">♡ ${likes.map((l) => esc(l.name || nameOf(l.author))).join('、')}</div>` : ''}
        <div class="mo-thread">${comments.length ? comments.map((c) => `<div class="mo-tc">
            ${av(c.author)}<div class="grow" style="min-width:0"><div class="small"><b>${esc(c.name || nameOf(c.author))}</b>${c.reply_to ? ` <span class="faint">回复</span> ${esc((comments.find((x) => x.author === c.reply_to) || {}).name || nameOf(c.reply_to))}` : ''}
            <span class="faint" style="float:right">${esc(ago(c.created))}</span></div><div class="mo-tc-text">${esc(c.text)}</div></div></div>`).join('')
          : '<div class="faint small" style="padding:8px 0">缓存中暂无评论</div>'}</div>
        <div class="field" style="margin-top:14px"><label>回复对象</label>
          <select class="input" id="moReplyTo"><option value="">${own ? '自动回复最新好友评论 / 补充自己的动态' : '评论这条动态'}</option>
          ${replyable.map((c) => `<option value="${esc(c.id)}">回复 ${esc(c.name || nameOf(c.author))}：${esc(c.text.slice(0, 25))}</option>`).join('')}</select></div>
        <div class="field" style="margin-top:10px"><label>评论内容</label>
          <textarea class="input" id="moCommentText" rows="3" maxlength="2000" placeholder="写下评论…"></textarea></div>
        <div class="row wrap" style="margin-top:10px">
          <button class="btn" id="moAiReply">✨ AI 生成回复</button><span class="grow"></span>
          <button class="btn" id="moSaveComment">保存草稿</button><button class="btn primary" id="moSendComment">发送评论</button></div>
        <div class="faint small" style="margin-top:8px">AI 按这条动态和回复对象生成文案，只填进输入框，不会发送。「发送评论」会真的在微信里评论，进入队列后在「执行记录」看回执。${own ? '自己的动态下，AI 以发布者身份回复朋友。' : ''}</div>`;
      if (own && others.length) $('#moReplyTo').value = others[others.length - 1].id;
      commentRev++;
      $('#moCloseInsp').onclick = () => { active = null; $$('.mo-post', view).forEach((p) => p.classList.remove('on'));
        box.querySelector('.card-head').innerHTML = '<h2>评论与回复</h2>';
        box.querySelector('.card-body').innerHTML = '<div class="empty" style="padding:30px 10px">在左边点一条动态的「查看与评论」。</div>'; };
      $('#moCommentText').oninput = () => commentRev++;
      $('#moReplyTo').onchange = () => commentRev++;
      $('#moAiReply').onclick = aiReply;
      $('#moSaveComment').onclick = (e) => busy(e.currentTarget, async () => {
        await saveCommentDraft(); $('#moCommentText').value = ''; commentRev++; toast('评论草稿已保存，尚未发送', 'ok');
      });
      $('#moSendComment').onclick = (e) => sendComment(e.currentTarget);
    }
    async function saveCommentDraft() {
      const text = $('#moCommentText').value.trim();
      if (!text) throw new Error('先写评论内容');
      return (await post('/api/moments/drafts', {kind: 'comment', feed_id: active.id, feed_digest: active.digest, reply_id: $('#moReplyTo').value, text, assets: []})).draft;
    }
    async function sendComment(btn) {
      const text = $('#moCommentText').value.trim();
      if (!text) return toast('先写评论内容', 'err');
      const sel = $('#moReplyTo'), target = sel.value ? sel.options[sel.selectedIndex].text : '评论「' + (active.name || '') + '」的动态';
      const ok = await modal({title: '发送到微信？', ok: '发送', body: `<div class="stack" style="gap:8px"><div class="muted small">${esc(target)}</div>
        <div class="mo-quote" style="margin:0">${esc(text)}</div><div class="faint small">会操作微信界面真的发出评论（聊天发送优先，可能要排队）。结果以「执行记录」里的微信回执为准。</div></div>`});
      if (!ok) return;
      await busy(btn, async () => {
        const d = await saveCommentDraft();
        await post(`/api/moments/drafts/${d.id}/submit`, {revision: d.revision});
        $('#moCommentText').value = ''; commentRev++;
        toast('评论已加入队列，正在核对微信回执', 'ok'); showTab('jobs');
      });
    }
    async function aiReply() {
      if (aiBusy || !active) return;
      const fid = active.id, digest = active.digest, reply = $('#moReplyTo').value, rev = commentRev, orig = $('#moCommentText').value;
      const btn = $('#moAiReply'); aiBusy = true;
      await busy(btn, async () => {
        const d = await post('/api/moments/ai-reply', {feed_id: fid, feed_digest: digest, reply_id: reply});
        if (!active || active.id !== fid || active.digest !== digest || !$('#moReplyTo') || $('#moReplyTo').value !== reply || commentRev !== rev || $('#moCommentText').value !== orig) {
          toast('生成期间目标或输入已变化，没有覆盖当前内容'); return;
        }
        if (d.reply_id !== reply) $('#moReplyTo').value = d.reply_id;
        $('#moCommentText').value = d.text; commentRev++;
        toast('AI 回复已填入，检查后再点发送', 'ok');
      });
      aiBusy = false;
    }

    // =====================================================================
    // 发布与草稿
    // =====================================================================
    pane('compose').innerHTML = `<div class="mo-compose-grid">
      <div class="card"><div class="card-head"><h2 id="moEditTitle">发一条朋友圈</h2><button class="btn sm ghost" id="moCancelEdit" hidden>取消编辑</button></div>
        <div class="card-body stack" style="gap:12px">
          <textarea class="input" id="moPubText" rows="6" maxlength="10000" placeholder="这一刻的想法…"></textarea>
          <div class="mo-assets" id="moAssets"></div>
          <div class="row wrap"><label class="btn sm" id="moAddImg">＋ 添加图片<input type="file" id="moFiles" accept="image/jpeg,image/png,image/webp" multiple hidden></label>
            <span class="faint small">最多 9 张 JPG / PNG / WebP，每张 15 MB 以内；会转成 JPEG 并去掉定位等元数据。暂不支持视频。</span></div>
          <div class="row wrap" style="border-top:1px solid var(--line-2);padding-top:12px">
            <button class="btn" id="moAiPost">✨ AI 写一条</button><span class="grow"></span>
            <button class="btn" id="moSaveDraft">保存草稿</button><button class="btn primary" id="moPublish">发布到微信</button></div>
          <div class="faint small">AI 按「设置 → 主动发朋友圈」里选的情绪与话题写一条，只填进输入框。「发布到微信」会真的发朋友圈，成功以执行记录里的微信回执为准。</div>
        </div></div>
      <div class="card"><div class="card-head"><h2>已保存草稿</h2><button class="btn sm ghost" id="moReloadDrafts">刷新</button></div>
        <div class="card-body" id="moDrafts"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div></div></div>`;

    function drawAssets() {
      $('#moAssets').innerHTML = assets.map((a) => `<figure class="mo-asset"><img src="${esc(assetSrc(a))}" data-lb="${esc(a)}" alt="草稿图片">
        <button class="mo-asset-x" data-rm="${esc(a)}" title="移除">✕</button></figure>`).join('');
      $$('[data-lb]', $('#moAssets')).forEach((i) => i.onclick = () => lightbox(i.src));
      $$('[data-rm]', $('#moAssets')).forEach((b) => b.onclick = () => { assets = assets.filter((x) => x !== b.dataset.rm); drawAssets(); });
      $('#moAddImg').style.display = assets.length >= 9 ? 'none' : '';
    }
    function resetEditor() {
      edit = null; assets = []; $('#moPubText').value = ''; drawAssets();
      $('#moCancelEdit').hidden = true; $('#moEditTitle').textContent = '发一条朋友圈';
    }
    $('#moCancelEdit').onclick = resetEditor;
    $('#moFiles').onchange = async () => {
      const files = [...$('#moFiles').files]; $('#moFiles').value = '';
      if (files.length + assets.length > 9) return toast('每条最多 9 张图片', 'err');
      for (const f of files) {
        if (f.size > 15 * 1024 * 1024) { toast(`「${f.name}」超过 15 MB`, 'err'); continue; }
        try {
          const fd = new FormData(); fd.append('session', JSON.stringify(session)); fd.append('file', f);
          const d = await A(id, '/api/moments/upload', {method: 'POST', body: fd});
          if (!assets.includes(d.asset.id)) assets.push(d.asset.id);
          drawAssets();
        } catch (e) { toast(`「${f.name}」上传失败：${e.message}`, 'err'); if (isStale(e)) loadCatalog().catch(() => {}); }
      }
    };
    async function savePublishDraft() {
      const text = $('#moPubText').value.trim();
      if (!text && !assets.length) throw new Error('先写点文字或加图片');
      const d = (await post('/api/moments/drafts', Object.assign({kind: 'publish', text, assets}, edit ? {id: edit.id, revision: edit.revision} : {}))).draft;
      return d;
    }
    $('#moSaveDraft').onclick = (e) => busy(e.currentTarget, async () => {
      await savePublishDraft(); resetEditor(); toast('草稿已保存，尚未发布', 'ok'); loadDrafts();
    });
    $('#moPublish').onclick = async (e) => {
      const text = $('#moPubText').value.trim();
      if (!text && !assets.length) return toast('先写点文字或加图片', 'err');
      const ok = await modal({title: '发布到朋友圈？', ok: '发布', body: `<div class="stack" style="gap:8px">
        <div class="mo-quote" style="margin:0">${esc(text || '（无文字）')}</div>${assets.length ? `<div class="muted small">附 ${assets.length} 张图片</div>` : ''}
        <div class="faint small">会操作微信界面真的发一条朋友圈，好友可见。进入队列后在「执行记录」查看回执。</div></div>`});
      if (!ok) return;
      busy(e.currentTarget, async () => {
        const d = await savePublishDraft();
        await post(`/api/moments/drafts/${d.id}/submit`, {revision: d.revision});
        resetEditor(); toast('已加入发布队列', 'ok'); showTab('jobs');
      });
    };
    $('#moAiPost').onclick = (e) => busy(e.currentTarget, async () => {
      const orig = $('#moPubText').value;
      const d = await post('/api/moments/ai-post', {});
      if ($('#moPubText').value !== orig) { toast('生成期间输入已变化，没有覆盖'); return; }
      $('#moPubText').value = d.text; toast('文案已生成，可修改后发布', 'ok');
    });
    $('#moReloadDrafts').onclick = () => loadDrafts();

    async function loadDrafts() {
      try { draftsCache = (await A(id, '/api/moments/drafts')).items || []; }
      catch (e) { $('#moDrafts').innerHTML = `<div class="callout red">${esc(e.message)}</div>`; return; }
      drawDrafts();
    }
    function draftHtml(d, open) {
      const [st, c] = DRAFT_STATUS[d.status] || [d.status, ''];
      const snap = d.snapshot || {};
      const head = d.kind === 'publish' ? '朋友圈' : `评论 · ${esc(snap.name || '')}`;
      return `<div class="mo-draft" data-d="${esc(d.id)}">
        <div class="row"><span class="badge ${d.kind === 'publish' ? 'blue' : ''}">${head}</span><span class="badge ${c}">${st}</span>
          <span class="grow"></span><span class="faint small" title="${esc(fullTime(d.updated))}">${esc(ago(d.updated))}</span></div>
        <div class="mo-draft-text">${esc(d.text) || '<span class="faint">（无文字）</span>'}</div>
        ${d.kind === 'comment' && (snap.text) ? `<div class="mo-quote small">${esc(snap.text)}</div>` : ''}
        ${d.assets && d.assets.length ? `<div class="mo-assets sm">${d.assets.map((a) => `<img src="${esc(assetSrc(a))}" data-lb>`).join('')}</div>` : ''}
        ${open ? `<div class="row wrap" style="margin-top:8px">
          <button class="btn sm primary" data-act="send">发送到微信</button>
          ${d.kind === 'publish' ? '<button class="btn sm" data-act="edit">编辑</button>' : '<button class="btn sm" data-act="target">打开动态</button>'}
          <button class="btn sm ghost" data-act="copy">复制文案</button><span class="grow"></span>
          <button class="btn sm ghost" data-act="discard">丢弃</button></div>` : ''}</div>`;
    }
    function drawDrafts() {
      const open = draftsCache.filter((d) => d.status === 'draft');
      const done = draftsCache.filter((d) => d.status !== 'draft');
      $('#moDrafts').innerHTML = (open.length ? open.map((d) => draftHtml(d, true)).join('') : '<div class="empty" style="padding:28px 10px">还没有未发送的草稿</div>')
        + (done.length ? `<details class="mo-done"><summary>已处理的草稿（${done.length}）</summary>${done.slice(0, 50).map((d) => draftHtml(d, false)).join('')}</details>` : '');
      $$('#moDrafts img[data-lb]').forEach((i) => i.onclick = () => lightbox(i.src));
      $$('#moDrafts [data-act]').forEach((b) => b.onclick = () => draftAction(b, draftsCache.find((x) => x.id === b.closest('[data-d]').dataset.d)));
    }
    async function draftAction(btn, d) {
      const act = btn.dataset.act;
      if (act === 'copy') { try { await navigator.clipboard.writeText(d.text); toast('文案已复制', 'ok'); } catch (e) { toast('浏览器不允许复制', 'err'); } return; }
      if (act === 'edit') {
        edit = d; assets = [...(d.assets || [])]; $('#moPubText').value = d.text; drawAssets();
        $('#moCancelEdit').hidden = false; $('#moEditTitle').textContent = '编辑草稿'; $('#moPubText').focus(); return;
      }
      if (act === 'target') { openFeed(d.feed_id); return; }
      if (act === 'discard') {
        if (!await modal({title: '丢弃这条草稿？', body: '丢弃后不能恢复，不会影响微信。', ok: '丢弃', danger: true})) return;
        return busy(btn, async () => { await post(`/api/moments/drafts/${d.id}/discard`, {revision: d.revision}); toast('已丢弃'); if (edit && edit.id === d.id) resetEditor(); loadDrafts(); });
      }
      if (act === 'send') {
        const what = d.kind === 'publish' ? '发一条朋友圈' : `在「${(d.snapshot || {}).name || ''}」的动态下评论`;
        if (!await modal({title: '发送到微信？', ok: '发送', body: `<div class="stack" style="gap:8px"><div class="muted small">会真的${esc(what)}：</div>
          <div class="mo-quote" style="margin:0">${esc(d.text || '（无文字）')}</div><div class="faint small">进入队列后在「执行记录」查看回执。</div></div>`})) return;
        return busy(btn, async () => { await post(`/api/moments/drafts/${d.id}/submit`, {revision: d.revision}); toast('已加入发送队列', 'ok'); showTab('jobs'); });
      }
    }

    // =====================================================================
    // 执行记录
    // =====================================================================
    pane('jobs').innerHTML = `<div class="card"><div class="card-head"><div class="seg" id="moJobFilter">
        <button data-f="all" class="on">全部</button><button data-f="active">进行中</button><button data-f="confirmed">已确认</button><button data-f="bad">失败 / 待核对</button><button data-f="other">跳过 / 取消</button></div>
        <div class="row"><span class="faint small">每 5 秒自动刷新</span><button class="btn sm ghost" id="moReloadJobs">刷新</button></div></div>
      <div class="callout blue" style="margin:14px 20px 0">只有在微信记录里查到新内容才算「已确认」。「待人工核对」可能已经发出，系统不会自动重发，请去微信里看一眼。</div>
      <div id="moJobs" style="padding:6px 0 4px"></div></div>`;
    const JOB_GROUP = {active: ['queued', 'preparing', 'initiated'], confirmed: ['confirmed'], bad: ['failed', 'uncertain'], other: ['skipped', 'cancelled']};
    $$('#moJobFilter button').forEach((b) => b.onclick = () => { jobFilter = b.dataset.f; $$('#moJobFilter button').forEach((x) => x.classList.toggle('on', x === b)); drawJobs(); });
    $('#moReloadJobs').onclick = () => loadJobs();
    async function loadJobs() {
      try { jobsCache = (await A(id, '/api/moments/jobs')).items || []; }
      catch (e) { $('#moJobs').innerHTML = `<div class="callout red" style="margin:14px 20px">${esc(e.message)}</div>`; return; }
      drawJobs();
    }
    function jobText(j) {
      const p = j.payload || {};
      if (j.kind === 'like') return '点赞好友动态' + (p.name ? ' · ' + p.name : '');
      if (j.kind === 'chat') return '点赞后主动私聊 · ' + (p.name || '好友') + (p.text ? '：' + p.text : '');
      return p.text || '待生成文案';
    }
    function drawJobs() {
      const list = jobFilter === 'all' ? jobsCache : jobsCache.filter((j) => JOB_GROUP[jobFilter].includes(j.state));
      if (!list.length) { $('#moJobs').innerHTML = `<div class="empty">${jobsCache.length ? '这个分类下没有记录' : '暂无执行记录'}</div>`; return; }
      $('#moJobs').innerHTML = `<table class="mo-table"><thead><tr><th style="width:120px">时间</th><th style="width:110px">类型</th><th style="width:110px">状态</th><th>内容与结果</th><th style="width:96px"></th></tr></thead><tbody>
        ${list.map((j) => { const [st, c] = JOB_STATE[j.state] || [j.state, '']; const p = j.payload || {};
          return `<tr data-j="${esc(j.id)}"><td><div>${esc(ago(j.created))}</div><div class="faint small">${esc(Date.now() / 1000 - j.created < 86400 * 30 ? fullTime(j.created).slice(5) : fullTime(j.created).slice(11))}</div></td>
            <td><div>${esc(JOB_KIND[j.kind] || j.kind)}</div><div class="faint small">${esc(JOB_ORIGIN[j.origin] || j.origin)}</div></td>
            <td><span class="badge ${c}">${['queued', 'preparing', 'initiated'].includes(j.state) ? `<span class="dot blue pulse"></span>` : ''}${esc(st)}</span></td>
            <td><div class="mo-job-text">${esc(jobText(j))}</div>
              ${j.message ? `<div class="faint small mo-job-msg">${esc(j.message)}</div>` : ''}
              <div class="row wrap faint small" style="gap:10px">${j.receipt ? `<span>微信记录 <span class="mono">${esc(j.receipt)}</span></span>` : ''}
              ${p.feed_id ? `<a href="javascript:void 0" data-feed="${esc(p.feed_id)}">查看动态</a>` : ''}${p.assets && p.assets.length ? `<span>${p.assets.length} 张图</span>` : ''}</div></td>
            <td style="text-align:right">${j.state === 'queued' ? '<button class="btn sm" data-jact="cancel">取消</button>' : ''}
              ${['failed', 'cancelled', 'skipped'].includes(j.state) ? '<button class="btn sm" data-jact="retry">重试</button>' : ''}</td></tr>`; }).join('')}</tbody></table>`;
      $$('#moJobs [data-feed]').forEach((a) => a.onclick = () => openFeed(a.dataset.feed));
      $$('#moJobs [data-jact]').forEach((b) => b.onclick = () => jobAction(b, jobsCache.find((x) => x.id === b.closest('[data-j]').dataset.j)));
    }
    async function jobAction(btn, j) {
      if (btn.dataset.jact === 'cancel') {
        if (!await modal({title: '取消这个任务？', body: '只有还在排队的任务能取消。', ok: '取消任务', cancel: '不取消', danger: true})) return;
        return busy(btn, async () => { await post(`/api/moments/jobs/${j.id}/cancel`, {}); toast('已取消'); loadJobs(); });
      }
      if (!await modal({title: '重新执行？', ok: '重试', body: `<div class="stack" style="gap:8px"><div class="muted small">${esc(JOB_KIND[j.kind] || j.kind)}：</div>
        <div class="mo-quote" style="margin:0">${esc(jobText(j))}</div><div class="faint small">手动重试会立刻排队、真的在微信里执行，不受免打扰和每日上限限制。</div></div>`})) return;
      busy(btn, async () => { await post(`/api/moments/jobs/${j.id}/retry`, {}); toast('已重新排队', 'ok'); loadJobs(); });
    }

    // =====================================================================
    // 设置
    // =====================================================================
    const sw = (k, t, d, capKey) => `<div class="setting"><div class="grow"><div class="t">${t}</div><div class="d">${d}</div></div>
      <label class="switch"><input type="checkbox" data-k="${k}" ${capKey && caps[capKey] === false ? 'disabled' : ''}><span></span></label></div>`;
    const num = (k, label, min, max, hint = '') => `<div class="field"><label>${label}</label>
      <input class="input" type="number" data-k="${k}" min="${min}" max="${max}" step="1">${hint ? `<div class="faint small">${hint}</div>` : ''}</div>`;
    function drawSettings() {
      if (!cfg) { pane('settings').innerHTML = '<div class="empty"><span class="spinner" style="margin:auto"></span></div>'; return; }
      pane('settings').innerHTML = `<div class="stack" style="gap:16px">
        <div class="mo-set-grid">
          <div class="card"><div class="card-head"><h2>自动互动</h2><span class="faint small">只处理开启之后的新动态</span></div><div class="card-body">
            ${sw('auto_comment', '自动评论与回复', '好友发新动态、或有人评论你的动态时，AI 判断要不要回应、怎么回；不适合就跳过。不补刷历史。', 'auto_comment')}
            ${sw('auto_like', '自动点赞好友新动态', '范围内好友的新动态全部点赞（不经 AI 判断）；只点别人的、不重复点。和自动评论互相独立。', 'auto_like')}
            ${sw('auto_chat_after_like', '点赞后主动私聊', '点赞成功后，AI 看动态情绪私聊对方：伤感就关心，趣事就共鸣，广告/转发跳过。同一条最多私聊一次；对方说过「别主动找我」会跳过。', 'auto_chat_after_like')}
            <div class="mo-fields">
              ${num('daily_comment_limit', '每日评论上限', 0, 1000, '0 = 不限')}
              ${num('daily_like_limit', '每日点赞上限', 0, 1000, '0 = 不限')}
              ${num('daily_chat_limit', '每日主动私聊上限', 0, 1000, '0 = 不限')}
              ${num('min_interval_minutes', '最小互动间隔（分钟）', 0, 10080, '评论、发布共用；0 = 不限')}
            </div></div></div>
          <div class="card"><div class="card-head"><h2>主动发朋友圈</h2></div><div class="card-body">
            ${sw('auto_publish', '定时分享', '每隔一段时间主动发一条（间隔在设定值到 +60 分钟之间浮动），只在免打扰时段外发。文案按默认人设，不编造真实经历。', 'auto_publish')}
            ${sw('chat_reflection', '聊天有感悟时发朋友圈', '机器人私聊回复成功后，AI 判断有没有值得分享的开心、感叹或趣事；不会公开私聊原话和对方信息。同一好友至少隔 15 分钟判断一次。', 'chat_reflection')}
            <div class="mo-fields">
              ${num('publish_interval_minutes', '定时分享间隔（分钟）', 30, 1440, '实际间隔为该值到 +60 分钟')}
              ${num('daily_publish_limit', '每日发布上限', 0, 1000, '定时分享和聊天感悟共用；0 = 不限')}
            </div>
            <div class="field" style="margin-top:12px"><label>情绪与话题（可多选，按天轮换）</label>
              <div class="mo-moods" id="moMoods">${MOODS.map((m) => `<button type="button" data-mood="${m}">${m}</button>`).join('')}</div></div>
            <details class="mo-adv"><summary>更多</summary>
              ${sw('publish_images', '允许配图', '定时分享时 AI 可以为文案配一张图。')}
              ${sw('publish_web_opinions', '参考网络热点', '写文案时参考近期网络新闻/观点作为素材。')}
            </details></div></div>
          <div class="card"><div class="card-head"><h2>免打扰</h2>${quietNow ? '<span class="badge amber">现在是免打扰时段</span>' : '<span class="badge">现在不在免打扰时段</span>'}</div><div class="card-body">
            <div class="muted small" style="margin-bottom:12px">北京时间。这段时间里不自动评论、不自动发布；你手动点发送不受限制，读取动态也不受影响。</div>
            <div class="row" style="gap:12px"><div class="field grow"><label>开始</label><input class="input" type="time" data-k="quiet_start"></div>
              <div class="field grow"><label>结束</label><input class="input" type="time" data-k="quiet_end"></div></div>
            ${sw('late_night_override', '深夜动态照常关心', '开启后，免打扰时段里好友新发的深夜动态仍会自动点赞并主动私聊（伤感的深夜动态更值得关心）；自动评论和定时发布仍然遵守免打扰。')}
          </div></div>
          <div class="card"><div class="card-head"><h2>朋友圈监听</h2></div><div class="card-body">
            ${sw('sync_enabled', '定期刷新微信动态', '定期打开微信朋友圈刷新并读取（聊天发送优先）。开了自动评论/发布时会自动刷新，不用单独开这个。只能读到这个号可见、客户端加载过的内容。')}
            <div class="mo-fields">${num('sync_interval_minutes', '刷新间隔（分钟）', 5, 1440, '5–1440')}</div></div></div>
        </div>
        <div class="card"><div class="card-head"><h2>好友范围</h2><button class="btn sm" id="moAllowAdd">＋ 添加好友</button></div><div class="card-body">
          <div class="muted small" style="margin-bottom:10px">自动评论、点赞、私聊只针对这些好友。留空 = 这个号的所有好友。</div>
          <div class="mo-allow" id="moAllow"></div></div></div>
        <div class="mo-savebar card"><span class="grow small" id="moDirty"></span>
          <button class="btn" id="moRevert">撤销修改</button><button class="btn primary" id="moSave">保存设置</button></div>
        <div class="faint small">保存只提交你改动过的项；调整设置会取消还没进入提交阶段的旧自动任务。设置版本 ${cfg.revision}。</div></div>`;
      fillSettings(); updDirty();
      $$('[data-k]', pane('settings')).forEach((i) => { i.oninput = updDirty; i.onchange = updDirty; });
      $$('#moMoods button').forEach((b) => b.onclick = () => { b.classList.toggle('on'); updDirty(); });
      $('#moAllowAdd').onclick = async () => {
        const c = await pickChat(id, {title: '添加到好友范围', groups: false});
        if (c && !c.is_group && !allow.includes(c.username)) { allow.push(c.username); if (c.name) names[c.username] = c.name; drawAllow(); updDirty(); }
      };
      $('#moRevert').onclick = () => { fillSettings(); updDirty(); };
      $('#moSave').onclick = (e) => saveSettings(e.currentTarget);
    }
    function fillSettings() {
      const root = pane('settings');
      for (const [k, t] of Object.entries(FIELDS)) {
        const i = $(`[data-k="${k}"]`, root); if (!i) continue;
        if (t === 'bool') i.checked = !!cfg[k]; else i.value = cfg[k] ?? '';
      }
      $$('#moMoods button').forEach((b) => b.classList.toggle('on', (cfg.moods || []).includes(b.dataset.mood)));
      allow = [...(cfg.friend_allowlist || [])]; drawAllow();
    }
    function drawAllow() {
      $('#moAllow').innerHTML = allow.length ? allow.map((u) => `<span class="mo-chip">${av(u)}<span class="ellipsis">${esc(nameOf(u))}</span>
        <button data-rmu="${esc(u)}" title="移除">✕</button></span>`).join('') : '<span class="faint small">所有好友</span>';
      $$('[data-rmu]', $('#moAllow')).forEach((b) => b.onclick = () => { allow = allow.filter((x) => x !== b.dataset.rmu); drawAllow(); updDirty(); });
    }
    function formValues() {
      const root = pane('settings'), v = {};
      for (const [k, t] of Object.entries(FIELDS)) {
        const i = $(`[data-k="${k}"]`, root); if (!i) continue;
        v[k] = t === 'bool' ? i.checked : t === 'int' ? (i.value === '' ? NaN : Number(i.value)) : i.value;
      }
      v.moods = MOODS.filter((m) => $(`#moMoods [data-mood="${m}"]`).classList.contains('on'));
      v.friend_allowlist = [...allow];
      return v;
    }
    function diff() {
      const v = formValues(), patch = {};
      for (const k of Object.keys(v)) if (JSON.stringify(v[k]) !== JSON.stringify(cfg[k])) patch[k] = v[k];
      return patch;
    }
    function dirtyCount() { return pane('settings').querySelector('#moSave') ? Object.keys(diff()).length : 0; }
    function updDirty() {
      const n = Object.keys(diff()).length;
      $('#moDirty').innerHTML = n ? `<span class="badge amber">有 ${n} 项未保存</span>` : '<span class="faint">没有未保存的修改</span>';
      $('#moSave').disabled = !n; $('#moRevert').disabled = !n;
    }
    async function saveSettings(btn) {
      const patch = diff();
      if (!Object.keys(patch).length) return toast('没有改动');
      for (const [k, v] of Object.entries(patch)) {
        if (FIELDS[k] === 'int') {
          const i = $(`[data-k="${k}"]`, pane('settings'));
          if (!Number.isInteger(v) || v < +i.min || v > +i.max) { i.focus(); return toast(`「${i.closest('.field').querySelector('label').textContent}」应为 ${i.min}–${i.max} 的整数`, 'err'); }
        }
      }
      if ('moods' in patch && !patch.moods.length) return toast('至少选一个情绪与话题', 'err');
      const qs = patch.quiet_start ?? cfg.quiet_start, qe = patch.quiet_end ?? cfg.quiet_end;
      if (qs === qe) return toast('免打扰开始和结束时间不能相同', 'err');
      await busy(btn, async () => {
        try { cfg = (await post('/api/moments/settings', {revision: cfg.revision, patch})).settings; }
        catch (e) { if (/设置已更新/.test(e.message)) { await loadCatalog(); throw new Error('设置刚被别处改过，已重新加载，请重新修改后保存'); } throw e; }
        toast('已保存', 'ok'); drawSettings(); drawStatus();
      });
    }

    // ---------- 顶部按钮 ----------
    $('#moReload').onclick = (e) => busy(e.currentTarget, async () => { await loadCatalog(); if (tab === 'compose') await loadDrafts(); if (tab === 'jobs') await loadJobs(); toast('已刷新'); });
    $('#moSync').onclick = async (e) => {
      if (!await modal({title: '刷新微信动态？', ok: '开始刷新', body: '会在这个微信的桌面上打开朋友圈、下拉刷新并读取（只读，不点赞不评论），大约十几秒到一分钟。聊天正在发送时会先让聊天。'})) return;
      busy(e.currentTarget, async () => {
        const d = await post('/api/moments/sync', {});
        offset = 0; await loadCatalog();
        toast(`同步完成：新增 ${d.result.added} 条，更新 ${d.result.changed} 条`, 'ok');
      });
    };

    // ---------- 启动 ----------
    showTab(tab);
    await loadNames();
    try { await loadCatalog(); }
    catch (e) { pane('feed').innerHTML = `<div class="callout red">读取朋友圈失败：${esc(e.message)}</div>`; return; }
    drawAssets();
    const t = setInterval(() => {
      if (document.hidden) return;
      if (tab === 'jobs') loadJobs();
    }, 5000);
    const t2 = setInterval(() => { if (!document.hidden) loadCatalog().catch(() => {}); }, 60000);
    onLeave(() => { clearInterval(t); clearInterval(t2); });
  };
})();
