// Webhook 订阅:把收到的消息加密推给外部系统
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct, onLeave, pickChat, fmtTime} = WX;

  const CATS = {text: '文字', image: '图片', voice: '语音', video: '视频', file: '文件', link: '链接', sticker: '表情',
    red_packet: '红包', transfer: '转账', location: '位置', card: '名片', miniapp: '小程序', video_channel: '视频号',
    merged: '聊天记录', quote: '引用', voip: '音视频通话', system: '系统', revoke: '撤回'};

  const copy = async (text) => {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* 非 https 时回退 */ }
    const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select();
    const ok = document.execCommand('copy'); t.remove(); return ok;
  };

  const scope = (h) => {
    const parts = [];
    parts.push(h.all_chats ? '全部会话' : `选定 ${(h.sources || []).length} 个会话`);
    if (!h.private) parts.push('只要群'); else if (!h.groups) parts.push('只要私聊');
    if ((h.categories || []).length) parts.push(h.categories.map((c) => CATS[c] || c).join('/'));
    if (h.include_self) parts.push('含自己发的');
    return parts.join(' · ');
  };

  const SAMPLE = `{
  "event": "message",
  "account": {"wxid": "wxid_机器人"},
  "message": {
    "chat": "123@chatroom", "chat_name": "群名", "is_group": true,
    "sender": "wxid_xxx", "sender_name": "张三", "is_self": false,
    "type": 1, "category": "text", "category_name": "文字",
    "content": "你好", "summary": "你好", "meta": {},
    "time": "2026-11-29 10:20:30", "timestamp": 1732846830,
    "local_id": 1234, "server_id": "…"
  }
}`;
  const CODE = {
    Python: `# pip install cryptography
import base64, json, time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
SECRET = "密钥"

def decrypt(body: bytes) -> dict:
    env = json.loads(body)
    if abs(time.time() - int(env["ts"])) > 300:
        raise ValueError("过期")
    aad = f'{env["id"]}.{env["ts"]}'.encode()
    plain = AESGCM(bytes.fromhex(SECRET)).decrypt(
        base64.b64decode(env["iv"]), base64.b64decode(env["data"]), aad)
    return json.loads(plain)`,
    'Node.js': `const crypto = require('crypto');
const SECRET = '密钥';

function decrypt(body) {               // 原始请求体字符串
  const env = JSON.parse(body);
  if (Math.abs(Date.now() / 1000 - env.ts) > 300) throw new Error('过期');
  const raw = Buffer.from(env.data, 'base64');
  const d = crypto.createDecipheriv('aes-256-gcm', Buffer.from(SECRET, 'hex'), Buffer.from(env.iv, 'base64'));
  d.setAAD(Buffer.from(\`\${env.id}.\${env.ts}\`));
  d.setAuthTag(raw.subarray(raw.length - 16));
  const plain = Buffer.concat([d.update(raw.subarray(0, raw.length - 16)), d.final()]);
  return JSON.parse(plain.toString('utf8'));
}`,
    Go: `key, _ := hex.DecodeString(secret)
iv, _ := base64.StdEncoding.DecodeString(env.IV)
data, _ := base64.StdEncoding.DecodeString(env.Data)
block, _ := aes.NewCipher(key)
gcm, _ := cipher.NewGCM(block)
plain, err := gcm.Open(nil, iv, data, []byte(fmt.Sprintf("%s.%d", env.ID, env.TS)))`,
    Java: `Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(HexFormat.of().parseHex(SECRET), "AES"),
       new GCMParameterSpec(128, Base64.getDecoder().decode(iv)));
c.updateAAD((id + "." + ts).getBytes(StandardCharsets.UTF_8));
String json = new String(c.doFinal(Base64.getDecoder().decode(data)), StandardCharsets.UTF_8);`,
    PHP: `$env = json_decode($body, true);
$raw = base64_decode($env['data']);
$plain = openssl_decrypt(substr($raw, 0, -16), 'aes-256-gcm', hex2bin(SECRET), OPENSSL_RAW_DATA,
                         base64_decode($env['iv']), substr($raw, -16), $env['id'] . '.' . $env['ts']);`,
  };

  const guide = () => `对接说明：wxbot Webhook（消息加密推送）

1. 我们会向你的地址发 POST，Content-Type: application/json，请求头带
   X-Wxbot-Event（message / test）、X-Wxbot-Id（事件 id，重试不变，用来去重）、X-Wxbot-Timestamp。
2. 请求体：{"v":1,"alg":"AES-256-GCM","id":"evt_…","ts":秒,"iv":"base64","data":"base64"}
3. 解密：AES-256-GCM；key = 十六进制解码(密钥)，32 字节；nonce = base64解码(iv)，12 字节；
   AAD = "id.ts"（例如 evt_abc.1732800000）；data 解码后最后 16 字节是认证标签。
   解密失败一律丢弃；ts 与当前时间相差超过 5 分钟的丢弃；重复的 id 直接返回 200。
4. 8 秒内返回 2xx 算成功，否则 10 秒、60 秒后各重试一次。
5. 解开后的 JSON：
${SAMPLE}

Python 示例：
${CODE.Python}

Node.js 示例：
${CODE['Node.js']}

（Go / Java / PHP 示例见项目 docs/webhook.md）
密钥单独发给你，请勿放进代码仓库。`;

  WXPAGES.webhook = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>Webhook 订阅</h1></div>`,
      `<button class="btn" id="whGuide">复制对接说明</button><button class="btn primary" id="whNew">＋ 新建订阅</button>`);
    let hooks = [], openLog = null;

    async function load() {
      try { hooks = (await A(id, '/api/webhooks')).hooks || []; } catch (e) { toast(e.message, 'err'); }
      draw();
    }

    function draw() {
      const off = s && !s.bot_running;
      view.innerHTML = `<div class="page stack wh-page">
        <div class="callout blue"><div>机器人收到的消息会 <b>用 AES-256-GCM 加密</b> 后 POST 到下面的地址，每个订阅一把独立密钥，
          只有拿着密钥的对接方能解开、也能确认消息没被伪造。把「接收地址 + 密钥 + 对接说明」交给需要的人即可。</div></div>
        ${off ? `<div class="callout amber"><div>这个微信的机器人现在 <b>没在运行</b>，不会推送。到「<a href="#/a/${id}/bot">机器人与规则</a>」打开。</div></div>` : ''}
        ${hooks.length ? hooks.map(card).join('') : `<div class="card"><div class="empty">
          <div style="font-size:30px">🔗</div><div>还没有订阅</div>
          <div class="faint small">点右上角「新建订阅」，填对方给你的接收地址</div></div></div>`}
        <details class="card wh-doc"><summary class="card-head"><h2>对接文档</h2><span class="faint small">发给对接方的技术说明</span></summary>
          <div class="card-body stack">
            <div class="small muted">请求体 <span class="mono">{"v":1,"alg":"AES-256-GCM","id","ts","iv","data"}</span>；
              key = hex 解码密钥（32 字节），nonce = base64 解码 iv（12 字节），AAD = <span class="mono">"id.ts"</span>，data 末尾 16 字节是认证标签。
              8 秒内返回 2xx 算成功，失败 10 秒、60 秒后重试；按 <span class="mono">X-Wxbot-Id</span> 去重，丢弃 5 分钟前的 ts。</div>
            <div class="seg" id="whLang">${Object.keys(CODE).map((k, i) => `<button data-l="${esc(k)}" class="${i ? '' : 'on'}">${esc(k)}</button>`).join('')}</div>
            <pre class="wh-code" id="whCode">${esc(CODE.Python)}</pre>
            <div class="small muted">解开后的内容：</div><pre class="wh-code">${esc(SAMPLE)}</pre>
          </div></details>
      </div>`;
      bind();
    }

    function card(h) {
      const st = h.stats || {};
      const health = !h.enabled ? '<span class="badge">已暂停</span>'
        : st.last_fail && (!st.last_ok || st.last_fail > st.last_ok) ? '<span class="badge red">最近投递失败</span>'
        : st.last_ok ? '<span class="badge green">正常</span>' : '<span class="badge">还没推送过</span>';
      return `<div class="card wh-card" data-h="${esc(h.id)}">
        <div class="card-body">
          <div class="row" style="gap:12px;align-items:flex-start">
            <div class="grow" style="min-width:0">
              <div class="row" style="gap:8px"><h2 class="ellipsis">${esc(h.name || '未命名订阅')}</h2>${health}</div>
              <div class="mono small ellipsis wh-url" title="${esc(h.url)}">${esc(h.url)}</div>
              <div class="small muted" style="margin-top:6px">${esc(scope(h))} · 密钥 <span class="mono">${esc(h.secret_hint)}</span></div>
              <div class="small faint" style="margin-top:4px">成功 ${st.ok || 0} · 失败 ${st.fail || 0}${st.last_ok ? ' · 最近成功 ' + esc(fmtTime(st.last_ok)) : ''}${st.last_error ? ` · <span style="color:var(--red)">${esc(st.last_error)}</span>` : ''}
                <span class="faint">（重启后清零）</span></div>
            </div>
            <label class="switch" title="暂停 / 恢复"><input type="checkbox" data-a="toggle" ${h.enabled ? 'checked' : ''}><span></span></label>
          </div>
          <div class="row wrap" style="gap:8px;margin-top:14px">
            <button class="btn sm" data-a="test">发测试消息</button>
            <button class="btn sm" data-a="edit">编辑</button>
            <button class="btn sm" data-a="secret">查看密钥</button>
            <button class="btn sm" data-a="log">投递记录</button>
            <span class="grow"></span>
            <button class="btn sm ghost" data-a="rotate">换密钥</button>
            <button class="btn sm danger" data-a="del">删除</button>
          </div>
          ${openLog === h.id ? '<div class="wh-log" id="whLog"><span class="spinner"></span></div>' : ''}
        </div></div>`;
    }

    function bind() {
      $('#whNew').onclick = () => edit(null);
      $('#whGuide').onclick = async () => toast(await copy(guide()) ? '对接说明已复制（不含密钥）' : '复制失败', 'ok');
      $$('#whLang button', view).forEach((b) => b.onclick = () => {
        $$('#whLang button', view).forEach((x) => x.classList.toggle('on', x === b));
        $('#whCode').textContent = CODE[b.dataset.l];
      });
      $$('[data-h]', view).forEach((el) => {
        const h = hooks.find((x) => x.id === el.dataset.h);
        $$('[data-a]', el).forEach((b) => {
          const a = b.dataset.a;
          if (a === 'toggle') b.onchange = () => save(h.id, {enabled: b.checked}, b.checked ? '已恢复' : '已暂停').catch(() => { b.checked = !b.checked; });
          else b.onclick = () => act(a, h, b);
        });
      });
      if (openLog) showLog(openLog);
    }

    async function save(hid, body, msg) {
      try { await A(id, `/api/webhooks/${hid}`, {method: 'PATCH', json: body}); toast(msg || '已保存'); await load(); }
      catch (e) { toast(e.message, 'err'); throw e; }
    }

    async function act(a, h, b) {
      if (a === 'edit') return edit(h);
      if (a === 'log') { openLog = openLog === h.id ? null : h.id; return draw(); }
      if (a === 'test') return busy(b, async () => {
        const r = await A(id, `/api/webhooks/${h.id}/test`, {method: 'POST'}).catch((e) => ({ok: false, error: e.message}));
        r.ok ? toast(`对方收到了（HTTP ${r.delivery.status}）`) : toast('投递失败：' + (r.error || '未知'), 'err');
        await load();
      });
      if (a === 'secret') return busy(b, async () => showSecret((await A(id, `/api/webhooks/${h.id}/secret`)).secret, h, false));
      if (a === 'rotate') {
        if (!await modal({title: `给「${h.name || h.url}」换密钥？`, danger: true, ok: '换密钥',
          body: '<p class="muted small">旧密钥立刻失效，对方解不开新消息，直到换成新密钥。怀疑密钥泄露时用。</p>'})) return;
        return busy(b, async () => { const r = await A(id, `/api/webhooks/${h.id}/rotate`, {method: 'POST'}); await load(); showSecret(r.secret, h, true); });
      }
      if (a === 'del') {
        if (!await modal({title: `删除订阅「${h.name || h.url}」？`, danger: true, ok: '删除',
          body: '<p class="muted small">删除后不再推送，密钥作废。</p>'})) return;
        return busy(b, async () => { await A(id, `/api/webhooks/${h.id}`, {method: 'DELETE'}); toast('已删除'); await load(); });
      }
    }

    function showSecret(secret, h, fresh) {
      modal({title: fresh ? '新密钥（请交给对接方）' : `「${h.name || '订阅'}」的密钥`, cancel: '', ok: '关闭',
        body: `<p class="muted small">${fresh ? '保存好这把密钥，' : ''}只发给这个订阅的对接方，别放进代码仓库或群聊。随时可以回来「查看密钥」。</p>
          <div class="wh-secret"><span class="mono" id="whSec">${esc(secret)}</span><button class="btn sm" id="whCopy">复制</button></div>`});
      setTimeout(() => { const c = $('#whCopy'); if (c) c.onclick = async () => toast(await copy(secret) ? '已复制' : '复制失败'); }, 0);
    }

    async function showLog(hid) {
      const box = $('#whLog'); if (!box) return;
      try {
        const d = (await A(id, `/api/webhooks/${hid}/deliveries`)).deliveries || [];
        box.innerHTML = d.length ? `<table class="wh-table"><thead><tr><th>时间</th><th>事件</th><th>会话</th><th>第几次</th><th>结果</th></tr></thead><tbody>${d.map((x) => `<tr>
          <td>${esc(fmtTime(x.time))}</td><td>${esc(x.event)}</td><td class="ellipsis">${esc(x.chat || '—')}</td><td>${x.attempt}</td>
          <td>${x.ok ? `<span class="badge green">HTTP ${x.status}</span>` : `<span class="badge red">${esc(x.error || 'HTTP ' + x.status)}</span>${x.final ? '' : ' <span class="faint small">会重试</span>'}`}</td></tr>`).join('')}</tbody></table>
          <div class="faint small">最近 50 次，容器重启后清空。</div>` : '<div class="faint small">还没有投递记录</div>';
      } catch (e) { box.innerHTML = `<div class="callout red">${esc(e.message)}</div>`; }
    }

    async function edit(h) {
      const v = h || {all_chats: true, groups: true, private: true, include_self: false, categories: [], sources: [], enabled: true};
      let sources = [...(v.sources || [])];
      const labels = {...(v.source_labels || {})};
      const bg = document.createElement('div'); bg.className = 'modal-bg';
      bg.innerHTML = `<div class="modal wh-modal"><div class="mh"><h2>${h ? '编辑订阅' : '新建订阅'}</h2></div><div class="mb stack" style="gap:14px">
        <div class="field"><label>名字（写给谁用的，方便自己区分）</label><input class="input" id="fName" maxlength="40" value="${esc(v.name || '')}" placeholder="例如：订单系统、客服工单"></div>
        <div class="field"><label>接收地址</label><input class="input mono" id="fUrl" value="${esc(v.url || '')}" placeholder="https://对方的服务器/wx-hook"></div>
        <div class="field"><label>推哪些会话</label>
          <div class="seg" id="fAll"><button data-v="1" class="${v.all_chats ? 'on' : ''}">全部会话</button><button data-v="0" class="${v.all_chats ? '' : 'on'}">只推选定的</button></div>
          <div id="fSrc" style="margin-top:8px"></div></div>
        <div class="row wrap" style="gap:18px">
          <label class="row" style="gap:6px"><input type="checkbox" id="fPriv" ${v.private !== false ? 'checked' : ''}> 私聊</label>
          <label class="row" style="gap:6px"><input type="checkbox" id="fGrp" ${v.groups !== false ? 'checked' : ''}> 群聊</label>
          <label class="row" style="gap:6px"><input type="checkbox" id="fSelf" ${v.include_self ? 'checked' : ''}> 包括我自己发的</label></div>
        <div class="field"><label>消息类型（都不勾 = 全部类型）</label>
          <div class="chips wh-cats">${Object.entries(CATS).map(([k, l]) => `<label class="chip-check"><input type="checkbox" value="${k}" ${(v.categories || []).includes(k) ? 'checked' : ''}><span>${l}</span></label>`).join('')}</div></div>
        </div><div class="mf"><button class="btn" data-r="0">取消</button><button class="btn primary" data-r="1">${h ? '保存' : '创建并生成密钥'}</button></div></div>`;
      document.body.appendChild(bg);
      const drawSrc = () => {
        const all = $('#fAll .on', bg).dataset.v === '1';
        $('#fSrc', bg).innerHTML = all ? '<div class="faint small">机器人能看到的所有会话的新消息都会推送。</div>'
          : `<div class="chips">${sources.map((u) => `<span class="badge" title="${esc(u)}">${esc(labels[u] || u)} <a href="#" data-rm="${esc(u)}">×</a></span>`).join('')}
             <button class="btn sm" id="fAdd">＋ 选会话</button></div>`;
        const add = $('#fAdd', bg);
        if (add) add.onclick = async () => { const c = await pickChat(id, {title: '推送哪个会话', groups: true}); if (c && !sources.includes(c.username)) { sources.push(c.username); labels[c.username] = c.name || c.username; drawSrc(); } };
        $$('[data-rm]', bg).forEach((x) => x.onclick = (e) => { e.preventDefault(); sources = sources.filter((u) => u !== x.dataset.rm); drawSrc(); });
      };
      $$('#fAll button', bg).forEach((b) => b.onclick = () => { $$('#fAll button', bg).forEach((x) => x.classList.toggle('on', x === b)); drawSrc(); });
      drawSrc();
      const close = () => bg.remove();
      bg.addEventListener('click', (e) => { if (e.target === bg || e.target.closest('[data-r="0"]')) close(); });
      $('[data-r="1"]', bg).onclick = (e) => busy(e.currentTarget, async () => {
        const body = {name: $('#fName', bg).value.trim(), url: $('#fUrl', bg).value.trim(),
          all_chats: $('#fAll .on', bg).dataset.v === '1', sources,
          source_labels: Object.fromEntries(sources.map((u) => [u, labels[u] || u])),
          private: $('#fPriv', bg).checked, groups: $('#fGrp', bg).checked, include_self: $('#fSelf', bg).checked,
          categories: $$('.wh-cats input:checked', bg).map((x) => x.value)};
        if (!body.url) throw new Error('填一下接收地址');
        if (!body.private && !body.groups) throw new Error('私聊和群聊至少勾一个');
        if (!body.all_chats && !sources.length) throw new Error('选至少一个会话，或者改成「全部会话」');
        if (h) { await A(id, `/api/webhooks/${h.id}`, {method: 'PATCH', json: body}); close(); toast('已保存'); await load(); return; }
        const r = await A(id, '/api/webhooks', {method: 'POST', json: body});
        close(); await load(); showSecret(r.secret, r.hook, true);
      });
    }

    await load();
  };
})();
