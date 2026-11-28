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
  makeReq: 0x39deee4,      // make_shared<文字Req>(vt 0x7e1b790),块 0x18+0x410,对象在 blk+0x18
  makeImgReq: 0x39dee1c,   // make_shared<图片Req>(vt 0x7e07a18),块 0x18+0x450
  videoCtor: 0x40f95f8,    // 视频Req 构造(vt 0x7e8bfb8,对象 0x410);微信自己是内联 make_shared,见 0x4225c84
  videoBlkVtGot: 0x80e5430,
  makeFileReq: 0x40f97dc,  // make_shared<文件Req>(vt 0x7e07948),块 0x18+0x3e0;工厂里随后写 +0x94/+0x98 = 49/6(appmsg 文件) // 视频 make_shared 控制块 vtable 组所在 GOT 槽,块头 = *GOT + 0x10
  revokeSvc: 0x4bbe8bc,    // 撤回服务定位第3步(前两步同 svcA/svcB),x8 返回 shared_ptr
  revokeSvcVt: 0x7f4b030,
  revoke: 0x5081554,
  msgByLocalId: 0x4bc9fe4, // GetMsgByLocalId(x8=输出{Msg 0x1f8; bool 找到@+0x1f8}, x0=消息服务(=svcC), x1=&会话string, w2=local_id, w3=create_time 选分库)       // Revoke(x8=结果{u8 ok,…}, x0=svc, x1=const 消息行&),须在协程里
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
extern char g_tr[]; extern char g_rv[]; extern char g_rvvt[]; extern char g_mbl[];
typedef void (*x8_fn3)(void*, void*, uint64_t, uint64_t, void*);
struct F { void** vt; };
void f_dtor(struct F* s){}
void f_ddtor(struct F* s){}
void* f_clone0(struct F* s){ struct F* n=(struct F*)((new_fn)g_new)(16); n->vt=s->vt; return n; }
void f_clone1(struct F* s, struct F* p){ p->vt=s->vt; }
void f_destroy(struct F* s){}
void f_destroy_dealloc(struct F* s){}
void f_call(struct F* s){ g[3]++;
  /* g[15] = 本次发送专用缓冲区:a/b/c 服务定位结果 + SendMessage 的 x8 返回值。
     图片的返回值是异步上传任务,必须活到上传结束,不能在多次发送间复用。 */
  uint64_t* w=(uint64_t*)g[15];
  uint64_t* a=&w[0]; uint64_t* b=&w[8]; uint64_t* c=&w[16];
  ((x8_fn)g_ta)(0,0,0,0,a); if(!a[0]){ g[11]=1; return; }
  ((x8_fn)g_tb)((void*)a[0],0,0,0,b); if(!b[0]){ g[11]=2; return; }
  if(g[16]==1){  /* 撤回:g[17]=消息行 g[18]=local_id g[19]=Msg缓冲(0x400) g[20]=假控制块 g[21]=create_time;结果首字节=成功 */
    uint64_t* row=(uint64_t*)g[17]; unsigned char* m=(unsigned char*)g[19]; uint64_t* ctl=(uint64_t*)g[20];
    ((x8_fn)g_tc)((void*)b[0],0,0,0,c); if(!c[0]){ g[11]=3; return; }
    if(*(uint64_t*)c[0] != (uint64_t)g_svcvt){ g[11]=4; return; }
    /* 让微信自己按 local_id 从库里加载完整 Msg */
    ((x8_fn3)g_mbl)((void*)c[0],(void*)(g[17]+0x20),g[18],g[21],(void*)m);
    if(!m[0x1f8]){ g[11]=5; return; }
    /* 用微信内存里的实时值覆盖(解密库同步有延迟):server_id/sort_seq/create_time/status */
    row[0x70>>3]=*(uint64_t*)(m+0x98); row[0x78>>3]=*(uint64_t*)(m+0xa0);
    *(uint32_t*)((char*)row+0x80)=*(uint32_t*)(m+0xb4); *(uint32_t*)((char*)row+0x84)=*(uint32_t*)(m+0xb8);
    /* 行 +0x1b0/+0x1c0 两个 shared_ptr 取自 Msg +0x1a0/+0x1b0;拷贝要加引用计数 */
    for(int i=0;i<2;i++){ uint64_t o=*(uint64_t*)(m+0x1a0+i*16), cb=*(uint64_t*)(m+0x1a8+i*16);
      row[(0x1b0>>3)+i*2]=o; row[(0x1b8>>3)+i*2]=cb; if(cb) (*(long*)(cb+8))++; }
    /* 行 +0x1d8 = shared_ptr<Msg>:指向我们的 Msg 缓冲,控制块计数设极大,永不释放 */
    row[0x1d8>>3]=(uint64_t)m; row[0x1e0>>3]=(uint64_t)ctl;
    c[0]=0; c[1]=0;
    ((x8_fn)g_tr)((void*)b[0],0,0,0,c); if(!c[0]){ g[11]=6; return; }
    if(*(uint64_t*)c[0] != (uint64_t)g_rvvt){ g[11]=7; return; }
    ((x8_fn)g_rv)((void*)c[0],(void*)g[17],0,0,(void*)&w[32]);
    g[12]=*(unsigned char*)&w[32]; g[2]++; g[11]=9; return; }
  ((x8_fn)g_tc)((void*)b[0],0,0,0,c); if(!c[0]){ g[11]=3; return; }
  if(*(uint64_t*)c[0] != (uint64_t)g_svcvt){ g[11]=4; return; }
  ((x8_fn)g_send)((void*)c[0],(void*)g[1],0,0,(void*)&w[32]); g[2]++; g[11]=9; }
void* f_target(struct F* s, void* ti){ return 0; }
void* f_target_type(struct F* s){ return (void*)g_ti; }
uint64_t do_launch(void* ex, void* loc, void* fnstore, uint64_t* out){
  ((x8_fn)g_launch)(ex, loc, fnstore, (void*)1, (void*)&out[3]); return 1; }
`, {
  g_send: sret(B.add(OFF.sendMessage)), g_ta: sret(B.add(OFF.svcA)), g_tb: sret(B.add(OFF.svcB)),
  g_tc: sret(B.add(OFF.svcC)), g_launch: sret(B.add(OFF.coLaunch)),
  g_tr: sret(B.add(OFF.revokeSvc)), g_mbl: sret(B.add(OFF.msgByLocalId)), g_rv: sret(B.add(OFF.revoke)), g_rvvt: B.add(OFF.revokeSvcVt),
  g_svcvt: B.add(OFF.svcVt), g_new: B.add(OFF.opNew), g_ti: B.add(OFF.typeInfo), g: G,
});
['f_dtor', 'f_ddtor', 'f_clone0', 'f_clone1', 'f_destroy', 'f_destroy_dealloc', 'f_call', 'f_target', 'f_target_type']
  .forEach((n, i) => VT.add(i * 8).writePointer(cm[n]));

const doLaunch = new NativeFunction(cm.do_launch, 'uint64', ['pointer', 'pointer', 'pointer', 'pointer']);
const opNew = new NativeFunction(B.add(OFF.opNew), 'pointer', ['size_t']);
const makeReq = new NativeFunction(B.add(OFF.makeReq), 'void', ['pointer']);
const makeImgReq = new NativeFunction(B.add(OFF.makeImgReq), 'void', ['pointer']);
const videoCtor = new NativeFunction(B.add(OFF.videoCtor), 'void', ['pointer']);
const makeFileReq = new NativeFunction(B.add(OFF.makeFileReq), 'void', ['pointer']);
const memsetZero = (p, n) => { for (let o = 0; o < n; o += 8) p.add(o).writeU64(0); };
const strAssign = new NativeFunction(B.add(OFF.strAssign), 'pointer', ['pointer', 'pointer']);

// 每次发送用到的 Frida 内存都挂在这里,保留最近 256 次,防 GC 释放仍在用的缓冲区(图片上传是异步的)。
const KEEP = [];
const FOREVER = [];  // 撤回用的 Msg/控制块/消息行,永不释放
const DUMMY_VT = Memory.alloc(16 * 8);  // 假 shared_ptr 控制块的 vtable,每槽都是空函数(计数永不归零,理论上不会被调)
for (let i = 0; i < 16; i++) DUMMY_VT.add(i * 8).writePointer(cm.f_dtor);
function hold(...xs) { KEEP.push(xs); if (KEEP.length > 256) KEEP.shift(); }

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

function readStr(p) {  // libc++ std::string
  return p.readU8() & 1 ? p.add(16).readPointer().readUtf8String(p.add(8).readU64().toNumber()) : p.add(1).readUtf8String(p.readU8() >> 1);
}
function loggedIn(ex) { return !!ex && !ex.isNull() && !ex.add(0x10).readPointer().isNull(); }

// 字段布局(文字/图片/视频共用基类,实测自界面发送):
// +0x6c=1 +0x70=7 +0x78 收件人 +0x94 类型(1 文字/3 图片/43 视频) +0xe8 正文字节数或文件大小
// 文字 +0x3e0 正文;图片/视频 +0xa0 本地文件路径;视频 +0x3e0(u8) 界面发送时为 1
const KINDS = {
  text: { type: 1, field: 0x3e0 },
  image: { type: 3, field: 0xa0 },
  video: { type: 43, field: 0xa0 },
  file: { type: 49, field: 0xa0 },  // appmsg,子类型 +0x98=6
};
function newReq(kind) {
  if (kind === 'video') {  // 照抄微信工厂 0x4225c84 里的内联 make_shared
    const blk = opNew(0x428);
    blk.writePointer(B.add(OFF.videoBlkVtGot).readPointer().add(0x10));
    blk.add(8).writeU64(0); blk.add(16).writeU64(0);
    memsetZero(blk.add(0x18), 0x410);
    videoCtor(blk.add(0x18));
    return blk;
  }
  if (kind === 'file') {  // 构造函数不清零,先自己清
    const blk = opNew(0x3f8);
    memsetZero(blk, 0x3f8);
    makeFileReq(blk);
    return blk;
  }
  const blk = opNew(kind === 'image' ? 0x468 : 0x428);
  (kind === 'image' ? makeImgReq : makeReq)(blk);
  return blk;
}
function build(kind, to, payload, size) {
  const keep = [];
  const k = KINDS[kind];
  const blk = newReq(kind);
  const obj = blk.add(0x18);
  strAssign(obj.add(0x78), longStr(to, keep));
  strAssign(obj.add(k.field), longStr(payload, keep));
  obj.add(0x6c).writeU32(1); obj.add(0x70).writeU32(7);
  obj.add(0x94).writeU32(k.type);
  if (kind === 'file') obj.add(0x98).writeU32(6);
  obj.add(0xe8).writeU32(kind === 'text' ? utf8len(payload) : size);
  if (kind === 'video') obj.add(0x3e0).writeU8(1);
  if (readStr(obj.add(0x78)) !== to || readStr(obj.add(k.field)) !== payload) return null;
  return { obj, blk, keep };
}

function launch(ex, req) {
  const sp = Memory.alloc(16); if (req.obj) { sp.writePointer(req.obj); sp.add(8).writePointer(req.blk); }
  const fobj = Memory.alloc(16); fobj.writePointer(VT);
  const fstore = Memory.alloc(0x40); fstore.add(0x20).writePointer(fobj);
  const file = Memory.allocUtf8String('bot_direct.cc'); const func = Memory.allocUtf8String('BotDirectSend');
  const loc = Memory.alloc(0x80); loc.writePointer(file); loc.add(8).writePointer(func); loc.add(16).writeU32(1);
  const out = Memory.alloc(0x40);
  const work = Memory.alloc(0x400);  // 零初始化;[0..23] 服务定位,[32..] SendMessage 返回值
  hold(req.keep, sp, fobj, fstore, file, func, loc, out, work);
  G.add(8).writePointer(sp);
  G.add(15 * 8).writePointer(work);
  G.add(11 * 8).writeU64(0); G.add(12 * 8).writeU64(0);
  G.add(16 * 8).writeU64(req.mode || 0); G.add(17 * 8).writePointer(req.row || NULL);
  if (req.mode === 1) { G.add(18 * 8).writeU64(req.localId); G.add(19 * 8).writePointer(req.msg); G.add(20 * 8).writePointer(req.ctl); G.add(21 * 8).writeU64(req.createTime); }
  const calls = G.add(3 * 8).readU64().toNumber();
  doLaunch(ex, loc, fstore, out);
  return { ok: true, calls };
}

// 消息行(0x268,纯数据:string/整数/shared_ptr,全零即合法空对象;拷贝构造 0x37e66e8)。
// 字段按界面撤回时实测:+0 类型 +0x8/+0x38 发送者 +0x20/+0x50 会话 +0x6c local_id +0x70 server_id
// +0x78 sort_seq +0x80 create_time +0x84 status +0x90 正文 +0xa8 msgsource +0x1d0 是否我发的
function buildRow(r) {
  const keep = [];
  const row = Memory.alloc(0x268);  // Memory.alloc 已清零
  row.writeU32(r.type);
  for (const [off, v] of [[0x8, r.sender], [0x20, r.chat], [0x38, r.sender], [0x50, r.chat],
                          [0x90, r.content || ''], [0xa8, r.source || '']]) {
    if (v) strAssign(row.add(off), longStr(v, keep));
  }
  row.add(0x6c).writeU32(r.localId);
  row.add(0x70).writeU64(uint64(r.serverId));
  row.add(0x78).writeU64(uint64(r.sortSeq));
  row.add(0x80).writeU32(r.createTime);
  row.add(0x84).writeU32(r.status);
  row.add(0x1d0).writeU16(0x101);  // +0x1d0=我发的(撤回首道校验 0x4f1aa4c 要求非 0);+0x1d1 界面也是 1
  if (readStr(row.add(0x20)) !== r.chat) return null;
  return { row, keep };
}

function sendKind(kind, to, payload, size) {
  const ex = executor();
  if (!loggedIn(ex)) return { ok: false, error: 'not_logged_in' };
  const req = build(kind, to, payload, size);
  if (!req) return { ok: false, error: 'request_mismatch' };
  return launch(ex, req);
}

rpc.exports = {
  ready() {
    return { attached: true, logged_in: loggedIn(executor()) };
  },
  send(to, content) { return sendKind('text', to, content, 0); },
  sendImage(to, path, size) { return sendKind('image', to, path, size); },
  sendVideo(to, path, size) { return sendKind('video', to, path, size); },
  sendFile(to, path, size) { return sendKind('file', to, path, size); },
  // r = {type, sender, chat, localId, serverId(十进制字符串), sortSeq(字符串), createTime, status, content, source}
  revoke(r) {
    const ex = executor();
    if (!loggedIn(ex)) return { ok: false, error: 'not_logged_in' };
    const b = buildRow(r);
    if (!b) return { ok: false, error: 'request_mismatch' };
    // Msg 缓冲(微信把找到的 Msg 移动构造进来)和假控制块都永久保留:撤回后微信可能还持有引用
    const msg = Memory.alloc(0x400);
    const ctl = Memory.alloc(0x18); ctl.writePointer(DUMMY_VT); ctl.add(8).writeU64(uint64('0x40000000')); ctl.add(16).writeU64(uint64('0x40000000'));
    FOREVER.push(msg, ctl, b.row, b.keep);
    return launch(ex, { mode: 1, row: b.row, localId: r.localId, createTime: r.createTime, msg, ctl, keep: [] });
  },
  // 只构造不发送,返回对象字节(十六进制),用来和界面发的请求逐字段对比
  dryBuild(kind, to, path, size) {
    const r = build(kind, to, path, size); if (!r) return null;
    hold(r.keep, r.blk);
    return { vt: r.obj.readPointer().sub(B).toString(16), hex: Array.from(new Uint8Array(r.obj.readByteArray(kind === 'file' ? 0x3e0 : 0x410))).map(b => b.toString(16).padStart(2, '0')).join('') };
  },
  poll() {
    return { step: G.add(11 * 8).readU64().toNumber(), calls: G.add(3 * 8).readU64().toNumber(),
             result: G.add(12 * 8).readU64().toNumber() };
  },
};
