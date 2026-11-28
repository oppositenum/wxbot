'use strict';
// 记录调用者返回地址 + x0..x3(+向量首元素),onEnter 只做轻量读取
const B = Process.getModuleByName('wechat').base;
const rel = p => { try { const m = Process.findModuleByAddress(p); return m ? m.name + '!0x' + p.sub(m.base).toString(16) : p.toString(); } catch (e) { return p.toString(); } };
const q = [];
for (const h of CFG.hooks) {
  Interceptor.attach(B.add(parseInt(h, 16)), { onEnter() {
    const c = this.context; const r = { fn: h, ra: this.returnAddress, lr: c.lr, x0: c.x0, x1: c.x1, x2: c.x2, x3: c.x3, x8: c.x8 };
    if (CFG.vec && CFG.vec.includes(h)) { try { r.vb = c.x2.readPointer(); r.ve = c.x2.add(8).readPointer(); r.snap = Memory.alloc(0x360); Memory.copy(r.snap, r.vb, 0x360); } catch (e) {} }
    q.push(r);
  }});
}
setInterval(() => { while (q.length) { const r = q.shift(); const o = { fn: r.fn, ra: rel(r.ra) };
  for (const k of ['x0', 'x1', 'x2', 'x3', 'x8']) { o[k] = r[k].toString(); try { o[k + 'vt'] = rel(r[k].readPointer()); } catch (e) {} }
  if (r.snap) { o.vb = r.vb.toString(); o.ve = r.ve.toString(); o.rowvt = rel(r.snap.readPointer()); const w = {}; for (let i = 0; i < 0x360; i += 8) { const v = r.snap.add(i).readU64(); if (!v.equals(0)) w['+' + i.toString(16)] = v.toString(16); } o.row = w; }
  send(o); } }, 20);
send({ ready: CFG.hooks });
