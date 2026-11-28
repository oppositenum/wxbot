'use strict';
/*
 * 4.1.13 探测:在业务协程里取消息服务,按 local_id 调 GetMsgByLocalId,导出返回的 Msg;顺便取撤回服务。
 * CFG = {svcA, svcB, svcC, revokeSvc, getMsg, coLaunch, appCtx, execOff, opNew, typeInfo, chat, localId, createTime, size}
 * rpc: run() 投递一次,poll() 取结果。
 */
const B = Process.getModuleByName('wechat').base;
const H = n => B.add(typeof n === 'string' ? parseInt(n, 16) : n);
const TR = [];
function sret(t) { const c = Memory.alloc(Process.pageSize); TR.push(c); Memory.protect(c, Process.pageSize, 'rwx');
  const w = new Arm64Writer(c); w.putMovRegReg('x8', 'x4'); w.putLdrRegAddress('x16', t); w.putBrReg('x16'); w.flush(); return c; }
const G = Memory.alloc(0x200); const VT = Memory.alloc(9 * 8);
const cm = new CModule(`
#include <stdint.h>
typedef void (*x8_fn)(void*, void*, void*, void*, void*);
typedef void (*x8_fn3)(void*, void*, uint64_t, uint64_t, void*);
typedef void* (*new_fn)(unsigned long);
extern char g_ta[]; extern char g_tb[]; extern char g_tc[]; extern char g_tr[]; extern char g_gm[]; extern char g_new[]; extern char g_ti[]; extern char g_launch[]; extern char g_cv[];
extern uint64_t g[];
struct F { void** vt; };
void f_dtor(struct F* s){} void f_ddtor(struct F* s){}
void* f_clone0(struct F* s){ struct F* n=(struct F*)((new_fn)g_new)(16); n->vt=s->vt; return n; }
void f_clone1(struct F* s, struct F* p){ p->vt=s->vt; }
void f_destroy(struct F* s){} void f_destroy_dealloc(struct F* s){}
void f_call(struct F* s){ uint64_t* w=(uint64_t*)g[1]; uint64_t* a=&w[0]; uint64_t* b=&w[4]; uint64_t* c=&w[8]; uint64_t* r=&w[12];
  g[2]=1;
  ((x8_fn)g_ta)(0,0,0,0,a); if(!a[0]){ g[2]=11; return; }
  ((x8_fn)g_tb)((void*)a[0],0,0,0,b); if(!b[0]){ g[2]=12; return; }
  ((x8_fn)g_tc)((void*)b[0],0,0,0,c); if(!c[0]){ g[2]=13; return; }
  ((x8_fn)g_tr)((void*)b[0],0,0,0,r);
  ((x8_fn3)g_gm)((void*)c[0],(void*)g[3],g[4],g[5],(void*)g[6]);
  if(!((unsigned char*)g[6])[0x278]){ g[2]=14; return; }
  if(g[7]) ((x8_fn)g_cv)((void*)g[6],0,0,0,(void*)g[7]);
  g[2]=9; }
void* f_target(struct F* s, void* ti){ return 0; }
void* f_target_type(struct F* s){ return (void*)g_ti; }
uint64_t do_launch(void* ex, void* loc, void* fs, uint64_t* out){ ((x8_fn)g_launch)(ex, loc, fs, (void*)1, (void*)&out[3]); return 1; }
`, { g_ta: sret(H(CFG.svcA)), g_tb: sret(H(CFG.svcB)), g_tc: sret(H(CFG.svcC)), g_tr: sret(H(CFG.revokeSvc)), g_gm: sret(H(CFG.getMsg)),
     g_new: H(CFG.opNew), g_ti: H(CFG.typeInfo), g_launch: sret(H(CFG.coLaunch)), g_cv: sret(H(CFG.toRow || '0x5f09c2c')), g: G });
['f_dtor', 'f_ddtor', 'f_clone0', 'f_clone1', 'f_destroy', 'f_destroy_dealloc', 'f_call', 'f_target', 'f_target_type'].forEach((n, i) => VT.add(i * 8).writePointer(cm[n]));
const doLaunch = new NativeFunction(cm.do_launch, 'uint64', ['pointer', 'pointer', 'pointer', 'pointer']);
const strAssign = new NativeFunction(H(CFG.strAssign), 'pointer', ['pointer', 'pointer']);
const K = [];
function rel(p) { try { const m = Process.findModuleByAddress(p); return m ? m.name + '!0x' + p.sub(m.base).toString(16) : null; } catch (e) { return null; } }
function lstr(p) { try { const b = p.readU8(); if ((b & 1) === 0) { const n = b >> 1; return n > 22 || n === 0 ? null : p.add(1).readUtf8String(n); }
  const n = p.add(8).readU64().toNumber(); if (n < 1 || n > 8192) return null; return p.add(16).readPointer().readUtf8String(Math.min(n, 400)); } catch (e) { return null; } }
function dump(p, n) { const words = {}, strs = {}; for (let o = 0; o < n; o += 8) { const v = p.add(o).readU64(); if (!v.equals(0)) { const r = rel(ptr(v.toString())); words['+' + o.toString(16)] = r || v.toString(16); }
  const s = lstr(p.add(o)); if (s && /^[\x09\x0a\x0d\x20-\x7e -￿]+$/.test(s)) strs['+' + o.toString(16)] = s; } return { words, strs }; }
let W, OUT, ROW;
rpc.exports = {
  run(chat, localId, createTime) {
    const ex = B.add(parseInt(CFG.appCtx, 16)).readPointer().add(parseInt(CFG.execOff, 16)).readPointer();
    W = Memory.alloc(0x200); OUT = Memory.alloc(CFG.size || 0x600); ROW = Memory.alloc(0x380);
    const cs = Memory.alloc(24); const s = Memory.allocUtf8String(chat); let n = 0; for (const ch of chat) { const c = ch.codePointAt(0); n += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4; }
    const tmp = Memory.alloc(24); tmp.writeU64(uint64((n + 16) | 1)); tmp.add(8).writeU64(n); tmp.add(16).writePointer(s);
    strAssign(cs, tmp);
    const fobj = Memory.alloc(16); fobj.writePointer(VT); const fs = Memory.alloc(0x40); fs.add(0x20).writePointer(fobj);
    const file = Memory.allocUtf8String('probe.cc'); const func = Memory.allocUtf8String('Probe');
    const loc = Memory.alloc(0x80); loc.writePointer(file); loc.add(8).writePointer(func); loc.add(16).writeU32(1);
    const out = Memory.alloc(0x40); K.push(cs, s, tmp, fobj, fs, file, func, loc, out, W, OUT);
    G.add(8).writePointer(W); G.add(16).writeU64(0); G.add(24).writePointer(cs); G.add(32).writeU64(localId); G.add(40).writeU64(createTime); G.add(48).writePointer(OUT); G.add(56).writePointer(ROW); K.push(ROW);
    doLaunch(ex, loc, fs, out); return 'launched';
  },
  poll() {
    const st = G.add(16).readU64().toNumber(); if (st !== 9) return { step: st };
    const r = { step: st, revokeSvc: W.add(12 * 8).readPointer().toString(), msgSvc: W.add(8 * 8).readPointer().toString() };
    try { r.revokeSvcVt = rel(W.add(12 * 8).readPointer().readPointer()); } catch (e) {}
    try { r.msgSvcVt = rel(W.add(8 * 8).readPointer().readPointer()); } catch (e) {}
    r.out = dump(OUT, CFG.size || 0x600); r.row = dump(ROW, 0x350); r.outAddr = OUT.toString(); return r;
  },
};
