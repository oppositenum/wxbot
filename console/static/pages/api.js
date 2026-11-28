/* 发送接口:给每个好友/群一键复制 curl(/api/send),附接口字段说明。见 console/PAGES.md、docs/api-send.md
 * 和旧版 send-curl 一样:复制出去的命令里带真实 token,页面上只显示打码的 token。 */
(() => {
  const {$, $$, esc, A, toast, setTop, avatar, acct} = WX;
  const MASK = '••••••••';
  const shq = (s) => "'" + String(s).replace(/'/g, "'\\''") + "'";   // shell 单引号转义

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* 非 https 时回退 */ }
    const t = document.createElement('textarea'); t.value = text; t.style.cssText = 'position:fixed;left:-9999px;top:0';
    document.body.appendChild(t); t.select(); let ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; } t.remove(); return ok;
  }
  async function copyBtn(btn, text) {
    const ok = await copy(text); const old = btn.dataset.label || (btn.dataset.label = btn.textContent);
    btn.textContent = ok ? '已复制' : '复制失败'; btn.classList.toggle('api-done', ok);
    toast(ok ? '已复制到剪贴板' : '复制失败，请手动选中复制', ok ? 'ok' : 'err');
    setTimeout(() => { btn.textContent = old; btn.classList.remove('api-done'); }, 1400);
  }
  const code = (key, text) => `<div class="api-code"><pre>${esc(text)}</pre><button class="btn sm api-copy" data-copy="${key}">复制</button></div>`;

  WXPAGES.api = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>发送接口</h1></div>`, `<a class="btn sm" href="#/a/${id}/send">发送设置</a>`);
    view.innerHTML = '<div class="page"><div class="empty"><span class="spinner" style="margin:auto"></span></div></div>';
    let d;
    try { d = await A(id, '/api/send/targets'); }
    catch (e) { view.innerHTML = `<div class="page"><div class="callout red">读取发送对象失败：${esc(e.message)}</div></div>`; return; }
    const rows = d.targets || [], token = d.token || '';
    const urlKey = `wx.api.url.${id}`;
    // 默认走控制台的公网/本机地址:/a/<账号>/api/send 带 token 就能调,不用登录控制台
    const def = `${location.origin}/a/${id}/api/send`;
    let url = localStorage.getItem(urlKey) || def;
    let kind = 'all', text = '';
    const snippets = {};

    // 命令:real=true 时带真实 token(复制用),否则打码(显示用)
    const body = (r, real) => {
      const b = {}; if (token) b.token = real ? token : MASK;
      return Object.assign(b, {to: r.to, chat: r.chat, type: 'text', content: text});
    };
    const curl = (r, real) => `curl --url ${shq(url)} \\\n  -H 'content-type: application/json' \\\n  --data-raw ${shq(JSON.stringify(body(r, real)))}`;
    const withToken = (t) => (real) => t.replace(/\$TOKEN/g, real ? token : MASK);
    const base = () => url.replace(/\/api\/send\/?$/, '');

    view.innerHTML = `<div class="page stack api-page">
      <div class="card"><div class="card-body api-conn">
        <div class="field grow"><label>接口地址</label><input class="input mono" id="apUrl" value="${esc(url)}" spellcheck="false"></div>
        <div class="api-token">${token
          ? `<span class="badge green">✓ 已配置 API token</span><div class="faint small">复制的命令里自动带上，页面不显示</div>`
          : `<span class="badge amber">没有配置 token</span><div class="faint small">容器没设 WXBOT_API_TOKEN，命令只能在登录后台的情况下用</div>`}</div>
      </div>
      <div class="api-hint faint small">默认是控制台地址（你现在打开的这个域名），带 token 就能从任何地方调用，不用登录控制台；
        也可以用 <span class="mono">${esc(location.origin)}/api/send</span> 加 <span class="mono">"account"</span>（微信号 / 昵称 / 备注）选号，不填 account 就用排第一的号。
        同一个 Docker 网络里的程序也可以直连容器 <span class="mono">${esc(d.url || '')}</span>。改动只保存在这个浏览器里。
        <a href="#" id="apUrlReset">恢复默认</a></div></div>
      <div class="api-grid">
        <div class="card api-targets"><div class="card-head"><h2>发给谁</h2><span class="faint small" id="apCount"></span></div>
          <div class="card-body api-tools">
            <input class="input grow" id="apQ" placeholder="搜索名字或微信号 / 群号">
            <div class="seg" id="apKind"><button data-k="all" class="on">全部</button><button data-k="私聊">私聊</button><button data-k="群">群</button></div>
          </div>
          <div class="api-tools" style="padding-top:0"><input class="input grow" id="apText" placeholder="要发的文字（可留空，复制后在命令里填 content）" maxlength="2000"></div>
          <div class="api-list" id="apList"></div></div>
        <div class="stack api-ref">
          <div class="card"><div class="card-head"><h2>POST /api/send</h2><span class="faint small">发文字或图片，进发送队列</span></div><div class="card-body stack">
            <table class="api-fields"><tbody>
              <tr><td><code>to</code><span class="badge red">必填</span></td><td>微信显示名（备注或群名），用来打开会话</td></tr>
              <tr><td><code>chat</code></td><td>微信号或群号（<span class="mono">…@chatroom</span>）；建议总是带上，避免重名发错</td></tr>
              <tr><td><code>type</code></td><td><code>text</code>（默认）或 <code>image</code></td></tr>
              <tr><td><code>content</code></td><td>文字内容，<code>type=text</code> 时必填</td></tr>
              <tr><td><code>path</code></td><td>图片在容器里的绝对路径，<code>type=image</code> 时必填；先用 <code>/api/upload</code> 上传拿到</td></tr>
              <tr><td><code>at</code></td><td>群里要 @ 的成员微信号列表 <code>["wxid_…"]</code>；不传时正文里的「@成员名」也会变真 @（直发）</td></tr>
              <tr><td><code>quote</code></td><td>引用回复：被引用消息的 <code>local_id</code>（直发 + 微信 4.1.13）</td></tr>
              <tr><td><code>sync</code></td><td><code>true</code> 等发送结果再返回（失败 HTTP 500）；默认立即返回 <code>job</code></td></tr>
              <tr><td><code>token</code></td><td>API token；也可以放请求头 <code>X-Wxbot-Token</code></td></tr>
            </tbody></table>
            <div class="faint small">按「发送设置」里的发送方式走（直发 / 界面）。成功返回 <code>{"ok":true,"queued":true,"job":"…","ahead":0}</code>。</div>
          </div></div>
          <div class="card"><div class="card-head"><h2>查结果</h2><span class="faint small">GET /api/send/status</span></div><div class="card-body stack">
            ${code('status', '')}
            <div class="faint small">状态有 <code>confirmed</code> 已确认、<code>submitted</code> 已提交、<code>failed</code> 失败、<code>uncertain</code> 结果待核对、<code>unknown</code>。
              <b>uncertain 不要自动重发</b>，否则可能发两遍。</div>
          </div></div>
          <details class="card api-more"><summary class="card-head"><h2>发图片 · 撤回</h2><span class="faint small">展开</span></summary><div class="card-body stack">
            <div class="small muted">1. 先上传图片，返回 <code>path</code>：</div>${code('upload', '')}
            <div class="small muted">2. 用这个 path 发送（<code>to</code>/<code>chat</code> 换成目标）：</div>${code('image', '')}
            <div class="small muted">撤回我在某个会话最近发的一条（2 分钟内；文件传输助手不支持；可加 <code>local_id</code> 指定）：</div>${code('revoke', '')}
          </div></details>
        </div>
      </div></div>`;

    function buildSnippets() {
      const b = base(), tk = token ? ` \\\n  -H 'X-Wxbot-Token: $TOKEN'` : '';
      snippets.status = withToken(`curl ${shq(b + '/api/send/status?job=<job-id>')}${tk}`);
      snippets.upload = withToken(`curl ${shq(b + '/api/upload')}${tk} \\\n  -F 'file=@/tmp/photo.png'`);
      const img = {to: '项目群', chat: '123@chatroom', type: 'image', path: '/app/work/uploads/photo.png'};
      if (token) img.token = '$TOKEN';
      snippets.image = withToken(`curl --url ${shq(b + '/api/send')} \\\n  -H 'content-type: application/json' \\\n  --data-raw ${shq(JSON.stringify(img))}`);
      const rv = {chat: 'wxid_xxx'}; if (token) rv.token = '$TOKEN';
      snippets.revoke = withToken(`curl --url ${shq(b + '/api/direct/revoke')} \\\n  -H 'content-type: application/json' \\\n  --data-raw ${shq(JSON.stringify(rv))}`);
      $$('[data-copy]', view).forEach((btn) => { const f = snippets[btn.dataset.copy]; if (f) btn.previousElementSibling.textContent = f(false); });
    }
    $$('[data-copy]', view).forEach((btn) => btn.onclick = () => copyBtn(btn, snippets[btn.dataset.copy](true)));

    let open = null;   // 展开预览的那一行 chat
    function drawList() {
      const q = $('#apQ').value.trim().toLowerCase();
      const list = rows.filter((r) => (kind === 'all' || r.kind === kind) && (!q || (r.to + ' ' + r.chat).toLowerCase().includes(q)));
      $('#apCount').textContent = `${list.length} / ${rows.length}`;
      $('#apList').innerHTML = list.slice(0, 400).map((r) => {
        const i = rows.indexOf(r), on = open === r.chat;
        return `<div class="api-row ${on ? 'open' : ''}" data-i="${i}">
          <div class="api-row-main">
            <img class="avatar sm" src="/a/${id}/api/avatar?username=${encodeURIComponent(r.chat)}" onerror="this.style.visibility='hidden'">
            <div class="grow" style="min-width:0"><div class="row" style="gap:6px"><b class="ellipsis">${esc(r.to)}</b><span class="badge ${r.kind === '群' ? 'blue' : ''}">${esc(r.kind)}</span></div>
              <div class="faint small mono ellipsis">${esc(r.chat)}</div></div>
            <button class="btn sm ghost" data-a="id" title="复制微信号 / 群号">复制 ID</button>
            <button class="btn sm ghost" data-a="view">${on ? '收起' : '查看命令'}</button>
            <button class="btn sm primary" data-a="curl">复制 curl</button>
          </div>
          ${on ? `<div class="api-code api-row-code"><pre>${esc(curl(r, false))}</pre></div>` : ''}
        </div>`;
      }).join('') || '<div class="empty">没有匹配的人或群</div>';
    }
    $('#apList').onclick = (e) => {
      const b = e.target.closest('[data-a]'), el = e.target.closest('.api-row'); if (!el) return;
      const r = rows[+el.dataset.i];
      if (!b) return;
      if (b.dataset.a === 'curl') copyBtn(b, curl(r, true));
      else if (b.dataset.a === 'id') copyBtn(b, r.chat);
      else { open = open === r.chat ? null : r.chat; drawList(); }
    };
    $('#apQ').oninput = drawList;
    $('#apText').oninput = () => { text = $('#apText').value; if (open) drawList(); };
    $$('#apKind button').forEach((b) => b.onclick = () => { kind = b.dataset.k; $$('#apKind button').forEach((x) => x.classList.toggle('on', x === b)); drawList(); });
    $('#apUrl').onchange = () => {
      url = $('#apUrl').value.trim() || def;
      if (url === d.url) localStorage.removeItem(urlKey); else localStorage.setItem(urlKey, url);
      $('#apUrl').value = url; buildSnippets(); drawList(); toast('接口地址已更新');
    };
    $('#apUrlReset').onclick = (e) => { e.preventDefault(); localStorage.removeItem(urlKey); $('#apUrl').value = def; $('#apUrl').onchange(); };
    buildSnippets(); drawList();
  };
})();
