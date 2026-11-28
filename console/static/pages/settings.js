/* 配置导入导出:/api/config/export(zip 下载)、/api/config/import(multipart 上传)。两者都要带 X-Wxbot-Local-Admin: 1 */
(() => {
  const {$, esc, toast, modal, busy, setTop, avatar, acct} = WX;

  const fmtSize = (n) => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B';

  // 不解压整个包,只读 zip 的中央目录(文件名+大小)和 wxbot-manifest.json,用来在导入前预览
  async function readZip(blob) {
    const tail = new DataView(await blob.slice(Math.max(0, blob.size - 65557)).arrayBuffer());
    let eocd = -1;
    for (let i = tail.byteLength - 22; i >= 0; i--) if (tail.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    if (eocd < 0) throw new Error('不是 zip 文件');
    const count = tail.getUint16(eocd + 10, true), cdSize = tail.getUint32(eocd + 12, true), cdOff = tail.getUint32(eocd + 16, true);
    const cd = new DataView(await blob.slice(cdOff, cdOff + cdSize).arrayBuffer());
    const utf8 = new TextDecoder('utf-8'), files = [];
    let p = 0;
    for (let n = 0; n < count && p + 46 <= cd.byteLength; n++) {
      if (cd.getUint32(p, true) !== 0x02014b50) break;
      const method = cd.getUint16(p + 10, true), csize = cd.getUint32(p + 20, true), size = cd.getUint32(p + 24, true);
      const nl = cd.getUint16(p + 28, true), xl = cd.getUint16(p + 30, true), cl = cd.getUint16(p + 32, true), off = cd.getUint32(p + 42, true);
      files.push({name: utf8.decode(new Uint8Array(cd.buffer, cd.byteOffset + p + 46, nl)), size, csize, method, off});
      p += 46 + nl + xl + cl;
    }
    let manifest = null;
    const m = files.find((f) => f.name === 'wxbot-manifest.json');
    if (m) {
      try {
        const lh = new DataView(await blob.slice(m.off, m.off + 30).arrayBuffer());
        const start = m.off + 30 + lh.getUint16(26, true) + lh.getUint16(28, true);
        let data = blob.slice(start, start + m.csize);
        if (m.method === 8) data = await new Response(data.stream().pipeThrough(new DecompressionStream('deflate-raw'))).blob();
        manifest = JSON.parse(await data.text());
      } catch (e) { manifest = null; }
    }
    return {files: files.filter((f) => f.name !== 'wxbot-manifest.json' && !f.name.endsWith('/')), manifest};
  }

  // 按文件归类,告诉用户包里有什么
  const KINDS = [
    [/\/bot_rules\.json$/, '机器人规则（监听、管理员、跟发、推送、主动回复）'],
    [/\/personas\//, '人设'], [/\/profiles\//, '联系人画像'],
    [/\/schedules\.json$|\/schedule_fires\.json$/, '定时任务与定时问候'],
    [/\/kb\.db$/, '知识库'], [/\/moments[._]/, '朋友圈数据与素材'],
    [/\/communication\.sqlite3/, '记忆与沟通状态'], [/\/imgarchive\//, '收到的图片存档'],
    [/\/(battle_mode|continuous_mode)\.json$/, '战斗 / 持续模式'],
    [/keys\.json\.bak/, '⚠️ 数据库密钥备份'],
  ];
  function summarize(files) {
    const accts = new Map(), kinds = new Map();
    for (const f of files) {
      const a = f.name.split('/')[1]; if (a) accts.set(a, (accts.get(a) || 0) + 1);
      const k = (KINDS.find(([re]) => re.test(f.name)) || [null, '运行状态等其他文件'])[1];
      const v = kinds.get(k) || {n: 0, size: 0}; v.n++; v.size += f.size; kinds.set(k, v);
    }
    return {accts, kinds, total: files.reduce((s, f) => s + f.size, 0)};
  }
  function summaryHtml({files, manifest}, title) {
    const {accts, kinds, total} = summarize(files);
    const keys = files.filter((f) => /keys\.json\.bak/.test(f.name)).length;
    return `<div class="set-sum">
      <div class="set-sum-h"><b>${esc(title)}</b><span class="faint small">${files.length} 个文件 · 解压后 ${fmtSize(total)}${manifest && manifest.created_at ? ' · 导出于 ' + new Date(manifest.created_at * 1000).toLocaleString('zh-CN', {hour12: false}) : ''}</span></div>
      ${manifest ? (manifest.format === 'wxbot-config-v1' ? '' : `<div class="callout red">配置包版本是「${esc(manifest.format || '未知')}」，后台只接受 wxbot-config-v1，导入会被拒绝。</div>`)
    : '<div class="callout red">包里没有 wxbot-manifest.json，不是 wxbot 导出的配置包，导入会被拒绝。</div>'}
      <table class="set-table"><tbody>${[...kinds].sort((a, b) => b[1].n - a[1].n).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.n} 个</td><td>${fmtSize(v.size)}</td></tr>`).join('')}</tbody></table>
      <div class="small muted">涉及 ${accts.size} 个微信账号目录：${[...accts.keys()].map((a) => `<code>${esc(a)}</code>`).join(' ')}</div>
      ${keys ? `<div class="callout amber">包里有 ${keys} 个数据库密钥备份文件（keys.json.bak-*），能解开聊天记录。不要把这个包发给别人。</div>` : ''}</div>`;
  }

  WXPAGES.settings = async (view, id) => {
    const s = acct(id);
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>配置导入导出</h1></div>`);
    view.innerHTML = `<div class="page stack" style="max-width:860px;gap:16px">
      <div class="callout blue"><div>配置包按<b>容器</b>打包：包含这个容器里登录过的所有微信账号的配置（每个账号一个目录）。用来备份、或者把配置搬到另一台机器 / 另一个容器。</div></div>

      <div class="card"><div class="card-head"><h2>导出配置</h2><button class="btn primary" id="expBtn">导出为 zip</button></div>
        <div class="card-body stack" style="gap:14px">
          <div class="grid cols-2 set-lists">
            <div><div class="set-lt ok">会导出</div><ul>
              <li>机器人规则：监听、管理员、跟发、推送、主动回复</li><li>人设、联系人画像</li><li>定时任务、定时问候</li>
              <li>知识库、朋友圈数据</li><li>记忆与沟通状态、收到的图片存档</li><li>战斗 / 持续模式里开着的会话</li></ul></div>
            <div><div class="set-lt no">不会导出</div><ul>
              <li>微信登录状态、聊天数据库（解密库、消息、媒体缓存）</li><li>数据库密钥 keys.json、AI 模型配置和 API key（llm_config.json），包括密钥备份 keys.json.bak-*</li>
              <li>发送记录（send_ledger）</li><li>容器级设置：发送方式、语音 / 音色、语音回复、战斗模式人设（这些在 <code>work/</code> 下，需要在新环境重新设）</li></ul></div>
          </div>
          <div class="faint small">图片存档较多时包会比较大（几十 MB），打包需要一点时间。</div>
          <div id="expOut"></div>
        </div></div>

      <div class="card"><div class="card-head"><h2>导入配置</h2></div>
        <div class="card-body stack" style="gap:14px">
          <label class="set-drop" id="drop"><input type="file" accept=".zip,application/zip" id="impFile" hidden>
            <div class="set-drop-ico">📦</div><div><b>选择或拖入配置包（.zip）</b></div><div class="faint small">先预览包里有什么，确认后才会导入</div></label>
          <div id="impOut"></div>
          <div class="callout amber"><div>导入会把包里的文件<b>覆盖</b>到同名位置；本机多出来的文件不会删除。不会动微信登录状态、密钥和 AI 配置。导入后要<b>重启这个微信的容器</b>才会全部生效（在「概览」里点「重启容器」）。</div></div>
        </div></div></div>`;

    // ---------- 导出 ----------
    $('#expBtn').onclick = (e) => busy(e.currentTarget, async () => {
      const out = $('#expOut');
      out.innerHTML = '<div class="muted small"><span class="spinner"></span> 正在打包…</div>';
      const r = await fetch(`/a/${id}/api/config/export`, {headers: {'X-Wxbot-Local-Admin': '1'}, credentials: 'same-origin', cache: 'no-store'});
      if (!r.ok) {
        let msg = `导出失败（${r.status}）`; try { msg = (await r.json()).error || msg; } catch (x) { /**/ }
        out.innerHTML = `<div class="callout red">${esc(msg)}</div>`; throw new Error(msg);
      }
      // 边下边显示进度
      const total = +r.headers.get('content-length') || 0, chunks = [];
      let got = 0; const rd = r.body.getReader();
      for (;;) {
        const {done, value} = await rd.read(); if (done) break;
        chunks.push(value); got += value.length;
        out.innerHTML = `<div class="muted small"><span class="spinner"></span> 正在下载 ${fmtSize(got)}${total ? ' / ' + fmtSize(total) : ''}</div>`;
      }
      const blob = new Blob(chunks, {type: 'application/zip'});
      const stamp = new Date().toLocaleString('sv-SE', {hour12: false}).replace(/[-:]/g, '').replace(' ', '-').slice(0, 13);
      const name = `wxbot-config-${(s && s.label) || id}-${stamp}.zip`.replace(/[\\/:*?"<>|\s]+/g, '_');
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 30000);
      let info = ''; try { info = summaryHtml(await readZip(blob), `已下载 ${name}（${fmtSize(blob.size)}）`); } catch (x) { info = `<div class="callout green">已下载 ${esc(name)}（${fmtSize(blob.size)}）</div>`; }
      out.innerHTML = info; toast('已导出', 'ok');
    });

    // ---------- 导入 ----------
    let file = null;
    const preview = async (f) => {
      file = f; const out = $('#impOut');
      if (!f) { out.innerHTML = ''; return; }
      out.innerHTML = '<div class="muted small"><span class="spinner"></span> 正在读取…</div>';
      let z = null;
      try { z = await readZip(f); } catch (e) { out.innerHTML = `<div class="callout red">${esc(f.name)}：${esc(e.message)}</div>`; file = null; return; }
      const bad = !z.manifest || z.manifest.format !== 'wxbot-config-v1';
      out.innerHTML = `${summaryHtml(z, `${f.name}（${fmtSize(f.size)}）`)}
        <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="btn ghost" id="impClear">换一个</button>
        <button class="btn primary" id="impBtn" ${bad ? 'disabled' : ''}>导入到这个容器</button></div>`;
      $('#impClear').onclick = () => { $('#impFile').value = ''; preview(null); };
      $('#impBtn').onclick = (e) => doImport(e.currentTarget, z);
    };
    const doImport = async (btn, z) => {
      if (!file) return;
      const accts = summarize(z.files).accts.size;
      if (!await modal({title: '导入这个配置包？', danger: true, ok: '导入',
        body: `会把 ${z.files.length} 个文件（${accts} 个账号目录）写进容器 <b>${esc((s && s.container) || id)}</b>，覆盖同名的规则、人设、画像、定时任务等。<b>不能撤销</b>，建议先导出一份当前配置备份。`})) return;
      await busy(btn, async () => {
        const fd = new FormData(); fd.append('file', file, file.name);
        const r = await fetch(`/a/${id}/api/config/import`, {method: 'POST', headers: {'X-Wxbot-Local-Admin': '1'}, body: fd, credentials: 'same-origin'});
        let d = {}; try { d = await r.json(); } catch (x) { /**/ }
        if (!r.ok || d.ok === false || d.error) throw new Error(d.error || `导入失败（${r.status}）`);
        $('#impOut').innerHTML = `<div class="callout green"><div>${esc(d.message || '配置已导入')}。到 <a href="#/a/${id}">概览</a> 点「重启容器」让配置全部生效。</div></div>`;
        file = null; $('#impFile').value = ''; toast('已导入', 'ok');
      });
    };
    $('#impFile').onchange = (e) => preview(e.target.files[0] || null);
    const drop = $('#drop');
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
    drop.ondragleave = () => drop.classList.remove('over');
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove('over'); const f = e.dataTransfer.files[0]; if (f) preview(f); };
  };
})();
