'use strict';
/*
 * 在 SendMessage 入口拷下"发送请求"对象,用来:
 *   - 界面发一条,看真实请求长什么样(字段、vtable、类型值)
 *   - 和我们自己构造的请求逐字段对比(diff_req.py)
 * 由 run.py 在前面注入 CFG = { send: ["0x4baee74", ...], size: 拷贝字节数, log: 文件 }(偏移可写十六进制字符串)。
 *
 * 规则(实测,违反会让微信闪退):
 *   - onEnter 跑在业务协程上,协程栈很小:只做 Memory.copy + 存寄存器,不读字符串、不做 backtrace。
 *   - 解析放 setInterval(Frida 自己的 JS 线程)里做。
 *   - 请求对象会被移动构造清零,必须在 onEnter 里同步拷贝,不能事后再读。
 */
const B = Process.getModuleByName('wechat').base;
const LOG = new File(CFG.log, 'a');
function out(o) { try { LOG.write(JSON.stringify(o) + '\n'); LOG.flush(); } catch (e) {} send(o); }
function rel(p) { try { const m = Process.findModuleByAddress(p); return m ? m.name + '!0x' + p.sub(m.base).toString(16) : null; } catch (e) { return null; } }
// libc++ std::string:bit0=1 长串(size@+8, data@+16);否则短串(长度=首字节>>1,数据从 +1 开始)
function lstr(p) {
  try {
    const b = p.readU8();
    if ((b & 1) === 0) { const n = b >> 1; return n > 22 ? null : p.add(1).readUtf8String(n); }
    const n = p.add(8).readU64().toNumber();
    if (n < 0 || n > 8192) return null;
    return p.add(16).readPointer().readUtf8String(Math.min(n, 300));
  } catch (e) { return null; }
}
const q = [];
const BUFS = []; for (let i = 0; i < 16; i++) BUFS.push(Memory.alloc(CFG.size)); let bi = 0;
for (const off of CFG.send.map(x => typeof x === 'string' ? parseInt(x, 16) : x)) {
  Interceptor.attach(B.add(off), {
    onEnter() {
      const c = this.context;  // SendMessage(x0=svc, x1=const shared_ptr<Req>&),x8=返回值缓冲
      let m = null, buf = null;
      try { m = c.x1.readPointer(); buf = BUFS[bi++ % 16]; Memory.copy(buf, m, CFG.size); } catch (e) {}
      q.push([off, Date.now(), Process.getCurrentThreadId(), c.x0, m, buf]);
    }
  });
}
setInterval(() => {
  while (q.length) {
    const [off, t, tid, svc, m, buf] = q.shift();
    const r = { fn: '0x' + off.toString(16), t: new Date(t).toISOString(), tid, req: m ? m.toString() : null };
    try { r.svcVt = rel(svc.readPointer()); } catch (e) {}
    if (buf) {
      r.reqVt = rel(buf.readPointer());
      const words = {}, strs = {};
      for (let o = 0; o < CFG.size; o += 8) {
        const v = buf.add(o).readU64();
        if (!v.equals(0)) words['+' + o.toString(16)] = v.toString(16);
        const s = lstr(buf.add(o));
        if (s && /^[\x20-\x7e -￿]+$/.test(s)) strs['+' + o.toString(16)] = s;
      }
      r.words = words; r.strs = strs;
    }
    out(r);
  }
}, 5);
send({ ready: CFG });
