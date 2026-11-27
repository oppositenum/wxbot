'use strict';
/*
 * 直发 agent:在微信进程内直接调用 SendMessage(不经过界面)。
 * 仅适配 WeChat Linux 4.1.8 aarch64(sha256 e40116597803…),偏移见
 * docs/记忆 wechat-send-interface-re。Python 侧(core/direct_send.py)先校验哈希再加载。
 *
 * 约束(实测踩坑):
 *  - SendMessage 必须在协程里调,所以用 co_launch 把 C 回调投递到业务执行器。
 *  - 协程栈很小:这里绝不挂 JS Interceptor 到发送路径上。
 *  - Frida 的 Memory.alloc 在 JS 引用消失后会被 GC 释放,所有交给微信的缓冲区都要保留引用。
 */
const B = Process.getModuleByName('wechat').base;

const OFF = {
  sendMessage: 0x4baee74,  // SendMessage(svc, const shared_ptr<Req>&) -> x8
  svcA: 0x483104c, svcB: 0x4950c40, svcC: 0x496004c,  // 服务定位链,均经 x8 返回
  svcVt: 0x7f1fb80,
  opNew: 0x7d7e420,
  makeReq: 0x39deee4,      // make_shared<Req> 控制块构造,对象在 blk+0x18
  strAssign: 0x3639df8,    // libc++ std::string 拷贝赋值
  coLaunch: 0x49bf7bc,
  appCtx: 0x82bb2a8,       // *(B+appCtx)+0x290 = 业务执行器
  typeInfo: 0x7f6a440,
};

const G = Memory.alloc(0x400);  // g[1]=&sp g[2]=done g[3]=calls g[11]=step
const VT = Memory.alloc(9 * 8);

function sret(target) {
  // 很多函数经 x8 返回结构体,TinyCC 不会设 x8:跳板 mov x8,x4; br target
  const code = Memory.alloc(Process.pageSize);
  Memory.protect(code, Process.pageSize, 'rwx');
  const w = new Arm64Writer(code);
  w.putMovRegReg('x8', 'x4'); w.putLdrRegAddress('x16', target); w.putBrReg('x16'); w.flush();
  return code;
}

const cm = new CModule(`
#include <stdint.h>
typedef void (*x8_fn)(void*, void*, void*, void*, void*);
typedef void* (*new_fn)(unsigned long);
extern char g_send[]; extern char g_new[]; extern char g_ti[]; extern uint64_t g[];
extern char g_ta[]; extern char g_tb[]; extern char g_tc[]; extern char g_svcvt[]; extern char g_launch[];
struct F { void** vt; };
void f_dtor(struct F* s){}
void f_ddtor(struct F* s){}
void* f_clone0(struct F* s){ struct F* n=(struct F*)((new_fn)g_new)(16); n->vt=s->vt; return n; }
void f_clone1(struct F* s, struct F* p){ p->vt=s->vt; }
void f_destroy(struct F* s){}
void f_destroy_dealloc(struct F* s){}
void f_call(struct F* s){ g[3]++;
  uint64_t* a=&g[32]; uint64_t* b=&g[36]; uint64_t* c=&g[40];
  ((x8_fn)g_ta)(0,0,0,0,a); if(!a[0]){ g[11]=1; return; }
  ((x8_fn)g_tb)((void*)a[0],0,0,0,b); if(!b[0]){ g[11]=2; return; }
  ((x8_fn)g_tc)((void*)b[0],0,0,0,c); if(!c[0]){ g[11]=3; return; }
  if(*(uint64_t*)c[0] != (uint64_t)g_svcvt){ g[11]=4; return; }
  ((x8_fn)g_send)((void*)c[0],(void*)g[1],0,0,(void*)&g[16]); g[2]++; g[11]=9; }
void* f_target(struct F* s, void* ti){ return 0; }
void* f_target_type(struct F* s){ return (void*)g_ti; }
uint64_t do_launch(void* ex, void* loc, void* fnstore, uint64_t* out){
  ((x8_fn)g_launch)(ex, loc, fnstore, (void*)1, (void*)&out[3]); return 1; }
`, {
  g_send: sret(B.add(OFF.sendMessage)), g_ta: sret(B.add(OFF.svcA)), g_tb: sret(B.add(OFF.svcB)),
  g_tc: sret(B.add(OFF.svcC)), g_launch: sret(B.add(OFF.coLaunch)),
  g_svcvt: B.add(OFF.svcVt), g_new: B.add(OFF.opNew), g_ti: B.add(OFF.typeInfo), g: G,
});
['f_dtor', 'f_ddtor', 'f_clone0', 'f_clone1', 'f_destroy', 'f_destroy_dealloc', 'f_call', 'f_target', 'f_target_type']
  .forEach((n, i) => VT.add(i * 8).writePointer(cm[n]));

const doLaunch = new NativeFunction(cm.do_launch, 'uint64', ['pointer', 'pointer', 'pointer', 'pointer']);
const opNew = new NativeFunction(B.add(OFF.opNew), 'pointer', ['size_t']);
const makeReq = new NativeFunction(B.add(OFF.makeReq), 'void', ['pointer']);
const strAssign = new NativeFunction(B.add(OFF.strAssign), 'pointer', ['pointer', 'pointer']);

// 每次发送用到的 Frida 内存都挂在这里,保留最近 64 次,防 GC 释放仍在用的缓冲区。
const KEEP = [];
function hold(...xs) { KEEP.push(xs); if (KEEP.length > 64) KEEP.shift(); }

function utf8len(s) {
  let n = 0;
  for (const ch of s) { const c = ch.codePointAt(0); n += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4; }
  return n;
}
function longStr(s, keep) {  // libc++ 长串布局: cap|1, size, data
  const data = Memory.allocUtf8String(s); const n = Memory.alloc(24); keep.push(data, n);
  const len = utf8len(s);
  n.writeU64(uint64((len + 16) | 1)); n.add(8).writeU64(uint64(len)); n.add(16).writePointer(data);
  return n;
}
function executor() {
  const ctx = B.add(OFF.appCtx).readPointer();
  if (ctx.isNull()) return null;
  return ctx.add(0x290).readPointer();
}

rpc.exports = {
  ready() {
    const ex = executor();
    return { attached: true, logged_in: !!ex && !ex.isNull() && !ex.add(0x10).readPointer().isNull() };
  },
  send(to, content) {
    const ex = executor();
    if (!ex || ex.isNull() || ex.add(0x10).readPointer().isNull()) return { ok: false, error: 'not_logged_in' };
    const keep = [];
    const blk = opNew(0x428); makeReq(blk); const obj = blk.add(0x18);
    strAssign(obj.add(0x78), longStr(to, keep));
    strAssign(obj.add(0x3e0), longStr(content, keep));
    obj.add(0x6c).writeU32(1); obj.add(0x70).writeU32(7);
    obj.add(0x94).writeU32(1);  // 文字
    obj.add(0xe8).writeU32(utf8len(content));
    const echoTo = obj.add(0x78).readU8() & 1 ? obj.add(0x88).readPointer().readUtf8String() : obj.add(0x79).readUtf8String();
    if (echoTo !== to) return { ok: false, error: 'request_mismatch' };

    const sp = Memory.alloc(16); sp.writePointer(obj); sp.add(8).writePointer(blk);
    const fobj = Memory.alloc(16); fobj.writePointer(VT);
    const fstore = Memory.alloc(0x40); fstore.add(0x20).writePointer(fobj);
    const file = Memory.allocUtf8String('bot_direct.cc'); const func = Memory.allocUtf8String('BotDirectSend');
    const loc = Memory.alloc(0x80); loc.writePointer(file); loc.add(8).writePointer(func); loc.add(16).writeU32(1);
    const out = Memory.alloc(0x40);
    hold(keep, sp, fobj, fstore, file, func, loc, out);

    G.add(8).writePointer(sp);
    G.add(11 * 8).writeU64(0);
    const calls = G.add(3 * 8).readU64().toNumber();
    doLaunch(ex, loc, fstore, out);
    return { ok: true, calls };
  },
  poll() {
    return { step: G.add(11 * 8).readU64().toNumber(), calls: G.add(3 * 8).readU64().toNumber() };
  },
};
