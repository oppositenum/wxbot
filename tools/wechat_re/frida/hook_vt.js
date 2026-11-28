'use strict';
// 给一批函数挂轻量钩子:onEnter 只拷 x0~x2 指向的前 8 字节(vtable),定时汇总"函数 → 参数 vtable"出现次数。
// CFG = {hooks:[...], ms: 汇总间隔}
const B = Process.getModuleByName('wechat').base;
const rel = p => { try { const m = Process.findModuleByAddress(p); return m && m.name === 'wechat' ? '0x' + p.sub(m.base).toString(16) : null; } catch (e) { return null; } };
const cnt = {}; const q = [];
for (const h of CFG.hooks) {
  try { Interceptor.attach(B.add(parseInt(h, 16)), { onEnter() { const c = this.context; let v0 = 0, v1 = 0, v2 = 0;
    try { v0 = c.x0.readPointer(); } catch (e) {} try { v1 = c.x1.readPointer(); } catch (e) {} try { v2 = c.x2.readPointer(); } catch (e) {}
    q.push([h, v0, v1, v2, this.returnAddress]); } }); } catch (e) { send({ fail: h, e: '' + e }); }
}
setInterval(() => { while (q.length) { const [h, a, b, c, ra] = q.shift(); const k = h + ' x0=' + (a ? rel(a) : '-') + ' x1=' + (b ? rel(b) : '-') + ' x2=' + (c ? rel(c) : '-') + ' ra=' + rel(ra); cnt[k] = (cnt[k] || 0) + 1; } }, 20);
rpc.exports = { take() { const o = Object.assign({}, cnt); for (const k in cnt) delete cnt[k]; return o; } };
send({ ready: CFG.hooks.length });
