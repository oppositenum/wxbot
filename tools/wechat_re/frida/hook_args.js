'use strict';
/*
 * 在若干函数入口同步拷下 x0/x1/x2 指向的 0x600 字节,定时线程里解析出指针归属和 libc++ 字符串。
 * 用法:run.py hook_args.js --cfg '{"hooks":["0x5081554","0x5083d40"]}' --out /tmp/x.jsonl
 * 3.10 节抓撤回参数用的就是它。onEnter 只做 Memory.copy(协程栈很小,见第 5 节)。
 */
const B = Process.getModuleByName('wechat').base;
function rel(p){ try{ const m=Process.findModuleByAddress(p); return m? m.name+'!0x'+p.sub(m.base).toString(16):null;}catch(e){return null;} }
function lstr(p){ try{ const b=p.readU8(); if((b&1)===0){const n=b>>1; return n>22||n==0?null:p.add(1).readUtf8String(n);} const n=p.add(8).readU64().toNumber(); if(n<1||n>4096) return null; return p.add(16).readPointer().readUtf8String(Math.min(n,200)); }catch(e){return null;} }
const q=[]; const N=0x600;
const HOOKS = CFG.hooks;
for (const h of HOOKS) {
  const off=parseInt(h,16);
  Interceptor.attach(B.add(off), { onEnter(){
    const c=this.context; const regs={x0:c.x0,x1:c.x1,x2:c.x2,x3:c.x3,x8:c.x8}; const snaps={};
    for (const k of ['x0','x1','x2']) { try{ const b=Memory.alloc(N); Memory.copy(b,regs[k],N); snaps[k]=b; }catch(e){} }
    q.push([h,Date.now(),regs,snaps]);
  }});
}
function dump(b){ const words={},strs={}; for(let o=0;o<N;o+=8){ const v=b.add(o).readU64(); if(!v.equals(0)){ const p=ptr(v.toString()); const r=rel(p); words['+'+o.toString(16)]= r||v.toString(16);} const s=lstr(b.add(o)); if(s&&/^[\x20-\x7e -￿]+$/.test(s)) strs['+'+o.toString(16)]=s; } return {words,strs}; }
setInterval(()=>{ while(q.length){ const [h,t,regs,snaps]=q.shift(); const r={fn:h,t:new Date(t).toISOString(),regs:{}}; for(const k in regs) r.regs[k]=regs[k].toString(); for(const k in snaps){ r[k]=dump(snaps[k]); } send(r);} },10);
send({ready:HOOKS});
