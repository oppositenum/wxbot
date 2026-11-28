'use strict';
/*
 * 直发 agent:在微信进程内直接调用 SendMessage(不经过界面)。
 * 按微信版本注入偏移(core/direct_send_profiles.py),Python 侧先校验二进制 sha256 再加载。
 * 过程与重新定位方法见 docs/wechat-direct-send-re.md。
 *
 * 约束(实测踩坑):
 *  - SendMessage 必须在协程里调,所以用 co_launch 把 C 回调投递到业务执行器。
 *  - 协程栈很小:这里绝不挂 JS Interceptor 到发送路径上。
 *  - Frida 的 Memory.alloc 在 JS 引用消失后会被 GC 释放,所有交给微信的缓冲区都要保留引用。
 */
const B = Process.getModuleByName('wechat').base;

// 偏移和字段布局按微信版本由 core/direct_send.py 注入(core/direct_send_profiles.py):const PROFILE = {...};
const P = PROFILE;
const F = P.fields;
const RV = P.revoke;   // 没适配撤回的版本为 null
const BATCH = !!(RV && RV.style === 'batch');   // 4.1.13:GetMsg → 消息行 → 批量撤回
const Q = BATCH ? (P.quote || null) : null;     // 引用回复要用同一套取消息行

const G = Memory.alloc(0x400);  // g[1]=&sp g[2]=done g[3]=calls g[11]=step
const VT = Memory.alloc(9 * 8);

const TRAMPS = [];   // 跳板页必须一直被引用,否则 Frida GC 会回收,调用时访问违规
function sret(target) {
  // 很多函数经 x8 返回结构体,TinyCC 不会设 x8:跳板 mov x8,x4; br target
  const code = Memory.alloc(Process.pageSize);
  TRAMPS.push(code);
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
extern char g_cv[]; extern char g_rc[]; extern char g_sa[];
typedef void (*x8_fn3)(void*, void*, uint64_t, uint64_t, void*);
typedef void (*fn2)(void*, void*);
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
  if(g[16]==2 || g[16]==3){
    /* 4.1.13:2=撤回 3=引用回复。g[30]=&会话 g[31]=local_id g[32]=create_time g[33]=Msg 缓冲 g[34]=行缓冲
       g[35]=vector 缓冲 g[36]=文字请求 g[37]=显示名 string(可 0) g[40..] 偏移(见 JS launch) */
    unsigned char* m=(unsigned char*)g[33]; unsigned char* row=(unsigned char*)g[34];
    ((x8_fn)g_tc)((void*)b[0],0,0,0,c); if(!c[0]){ g[11]=3; return; }
    if(*(uint64_t*)c[0] != (uint64_t)g_svcvt){ g[11]=4; return; }
    ((x8_fn3)g_mbl)((void*)c[0],(void*)g[30],g[31],g[32],(void*)m);
    if(!m[g[40]]){ g[11]=5; return; }
    ((x8_fn)g_cv)((void*)m,0,0,0,(void*)row);
    if(g[37]) ((fn2)g_sa)((void*)(row+g[44]),(void*)g[37]);
    if(g[16]==2){
      uint64_t* r=&w[24]; uint64_t* vec=(uint64_t*)g[35];
      ((x8_fn)g_tr)((void*)b[0],0,0,0,r); if(!r[0]){ g[11]=6; return; }
      if(*(uint64_t*)r[0] != (uint64_t)g_rvvt){ g[11]=7; return; }
      vec[0]=(uint64_t)row; vec[1]=(uint64_t)(row+g[41]); vec[2]=vec[1];
      ((x8_fn)g_rv)((void*)r[0],(void*)vec,0,0,(void*)&w[32]);
      g[12]=w[32]; g[13]=w[33]; g[14]=w[34]; g[2]++; g[11]=9; return; }
    unsigned char* q=(unsigned char*)g[36];
    ((fn2)g_rc)((void*)(q+g[42]),(void*)row); q[g[43]]=1;
    *(uint32_t*)(q+g[48])=*(uint32_t*)(row+g[45]); *(uint32_t*)(q+g[49])=*(uint32_t*)(row+g[46]);
    *(uint64_t*)(q+g[50])=*(uint64_t*)(row+g[47]); q[g[51]]=1;
    ((x8_fn)g_send)((void*)c[0],(void*)g[1],0,0,(void*)&w[32]); g[2]++; g[11]=9; return; }
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
  g_send: sret(B.add(P.sendMessage)), g_ta: sret(B.add(P.svcA)), g_tb: sret(B.add(P.svcB)),
  g_tc: sret(B.add(P.svcC)), g_launch: sret(B.add(P.coLaunch)),
  // 没适配撤回时这几个符号不会被调用(rpc.revoke 直接返回 revoke_unsupported)
  g_tr: RV ? sret(B.add(RV.revokeSvc)) : B, g_mbl: RV ? sret(B.add(RV.msgByLocalId)) : B,
  g_rv: RV ? sret(B.add(RV.style === 'batch' ? RV.batch : RV.revoke)) : B, g_rvvt: RV ? B.add(RV.revokeSvcVt) : B,
  g_cv: BATCH ? sret(B.add(RV.toRow)) : B, g_rc: Q ? B.add(Q.rowCopy) : B, g_sa: B.add(P.strAssign),
  g_svcvt: B.add(P.svcVt), g_new: B.add(P.opNew), g_ti: B.add(P.typeInfo), g: G,
});
['f_dtor', 'f_ddtor', 'f_clone0', 'f_clone1', 'f_destroy', 'f_destroy_dealloc', 'f_call', 'f_target', 'f_target_type']
  .forEach((n, i) => VT.add(i * 8).writePointer(cm[n]));

const doLaunch = new NativeFunction(cm.do_launch, 'uint64', ['pointer', 'pointer', 'pointer', 'pointer']);
const opNew = new NativeFunction(B.add(P.opNew), 'pointer', ['size_t']);
const memsetZero = (p, n) => { for (let o = 0; o < n; o += 8) p.add(o).writeU64(0); };
const strAssign = new NativeFunction(B.add(P.strAssign), 'pointer', ['pointer', 'pointer']);
const fn1 = off => new NativeFunction(B.add(off), 'void', ['pointer']);
// x8 返回 shared_ptr 的创建函数:经跳板把第 5 个参数放进 x8
const fnX8 = off => new NativeFunction(sret(B.add(off)), 'void', ['pointer', 'pointer', 'pointer', 'pointer', 'pointer']);
// 第 6 个参数放进 x8 的跳板:语音的创建函数自己要用 x1~x4 传参
function sret5(target) {
  const code = Memory.alloc(Process.pageSize);
  TRAMPS.push(code);
  Memory.protect(code, Process.pageSize, 'rwx');
  const w = new Arm64Writer(code);
  w.putMovRegReg('x8', 'x5'); w.putLdrRegAddress('x16', target); w.putBrReg('x16'); w.flush();
  return code;
}
const VOICE_MAKER = (P.voice && P.req.voice)
  ? new NativeFunction(sret5(B.add(P.req.voice.maker)), 'void', ['pointer', 'pointer', 'pointer', 'pointer', 'pointer', 'pointer'])
  : null;
const MAKERS = {};
for (const kind of ['text', 'image', 'video', 'file']) {
  const r = P.req[kind];
  MAKERS[kind] = P.req.style === 'maker' ? fnX8(r.maker) : fn1(r.make || r.ctor);
}

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
  const ctx = B.add(P.appCtx).readPointer();
  if (ctx.isNull()) return null;
  return ctx.add(P.execOff).readPointer();
}

function readStr(p) {  // libc++ std::string
  return p.readU8() & 1 ? p.add(16).readPointer().readUtf8String(p.add(8).readU64().toNumber()) : p.add(1).readUtf8String(p.readU8() >> 1);
}
function loggedIn(ex) { return !!ex && !ex.isNull() && !ex.add(0x10).readPointer().isNull(); }

// 字段布局见 core/direct_send_profiles.py(实测自界面发送)。
const KINDS = {
  text: { type: 1, field: 'content' },
  image: { type: 3, field: 'path' },
  video: { type: 43, field: 'path' },
  file: { type: 49, field: 'path' },  // appmsg,子类型 6=文件
};
// 返回 {obj, sp}:sp 是 16 字节 shared_ptr {obj, 控制块}
function newReq(kind) {
  const r = P.req[kind];
  if (P.req.style === 'maker') {
    const sp = Memory.alloc(16);
    MAKERS[kind](NULL, NULL, NULL, NULL, sp);
    return { obj: sp.readPointer(), sp };
  }
  const blk = opNew(r.blk);
  if (r.ctrlGot) {  // 4.1.8 视频:照抄微信工厂里的内联 make_shared
    blk.writePointer(B.add(r.ctrlGot).readPointer().add(0x10));
    blk.add(8).writeU64(0); blk.add(16).writeU64(0);
    memsetZero(blk.add(0x18), r.blk - 0x18);
    MAKERS[kind](blk.add(0x18));
  } else {
    if (r.zero) memsetZero(blk, r.blk);   // 4.1.8 文件:构造函数不清零
    MAKERS[kind](blk);
  }
  const sp = Memory.alloc(16); sp.writePointer(blk.add(0x18)); sp.add(8).writePointer(blk);
  return { obj: blk.add(0x18), sp };
}
function basename(p) { const i = p.lastIndexOf('/'); return i >= 0 ? p.slice(i + 1) : p; }
function build(kind, to, payload, size) {
  const keep = [];
  const k = KINDS[kind];
  const { obj, sp } = newReq(kind);
  if (!obj || obj.isNull()) return null;
  const field = F[k.field];
  strAssign(obj.add(F.to), longStr(to, keep));
  strAssign(obj.add(field), longStr(payload, keep));
  if (F.filename && kind !== 'text') strAssign(obj.add(F.filename), longStr(basename(payload), keep));
  obj.add(F.one).writeU32(1); obj.add(F.seven).writeU32(7);
  obj.add(F.type).writeU32(k.type);
  if (kind === 'file') obj.add(F.sub).writeU32(6);
  obj.add(F.len).writeU32(kind === 'text' ? utf8len(payload) : size);
  if (kind === 'video') for (const [off, v] of F.video_flags) obj.add(off).writeU8(v);
  if (readStr(obj.add(F.to)) !== to || readStr(obj.add(field)) !== payload) return null;
  return { obj, sp, keep };
}

function launch(ex, req) {
  const sp = req.sp || Memory.alloc(16);
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
  if (req.mode === 2 || req.mode === 3) {
    const L = req.load;
    G.add(30 * 8).writePointer(L.chat); G.add(31 * 8).writeU64(L.localId); G.add(32 * 8).writeU64(L.createTime);
    G.add(33 * 8).writePointer(L.msg); G.add(34 * 8).writePointer(L.row); G.add(35 * 8).writePointer(L.vec);
    G.add(36 * 8).writePointer(req.obj || NULL); G.add(37 * 8).writePointer(L.name || NULL);
    const R = RV.row;
    [RV.msgFound, RV.rowSize, Q ? Q.rowOff : 0, Q ? Q.rowSet : 0, R.displayName, R.time, R.localId, R.sortSeq,
     Q ? Q.time : 0, Q ? Q.localId : 0, Q ? Q.sortSeq : 0, Q ? Q.flag : 0]
      .forEach((v, i) => G.add((40 + i) * 8).writeU64(v));
  }
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

// 语音条:silk 是微信格式的 SILK 文件(\x02#!SILK_V3 开头),ms 时长毫秒
function buildVoice(to, silkPath, ms) {
  const keep = [];
  const bytes = File.readAllBytes(silkPath);
  const n = bytes.byteLength;
  const buf = Memory.alloc(n); buf.writeByteArray(bytes); keep.push(buf);
  const data = Memory.alloc(24); keep.push(data);           // libc++ 长串 {cap|1, size, ptr},构造函数会拷走
  data.writeU64(uint64((n + 16) | 1)); data.add(8).writeU64(uint64(n)); data.add(16).writePointer(buf);
  const msp = Memory.alloc(8); msp.writeU32(ms); keep.push(msp);
  const [fmt, a, b] = P.req.voice.info;
  const info = Memory.alloc(0x30); keep.push(info);           // {u32 fmt, u32 a, u64 b, u8 0, string ""}
  info.writeU32(fmt); info.add(4).writeU32(a); info.add(8).writeU64(uint64(b));
  const sp = Memory.alloc(16);
  VOICE_MAKER(NULL, longStr(to, keep), data, msp, info, sp);
  const obj = sp.readPointer();
  if (obj.isNull()) return null;
  obj.add(F.one).writeU32(1); obj.add(F.seven).writeU32(7);
  obj.add(F.type).writeU32(34);
  if (readStr(obj.add(F.to)) !== to || obj.add(F.voiceData + 8).readU64().toNumber() !== n) return null;
  return { obj, sp, keep };
}

// 4.1.13:协程里按 local_id 取消息、转成消息行要用的缓冲区(都永久保留:微信可能还持有行里的引用)
function loadBufs(chat, localId, createTime, name) {
  const keep = [];
  const L = { chat: longStr(chat, keep), localId, createTime, msg: Memory.alloc(0x400),
              row: Memory.alloc(RV.rowSize + 0x40), vec: Memory.alloc(0x20), name: name ? longStr(name, keep) : null };
  FOREVER.push(L, keep);
  return L;
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
    return { attached: true, logged_in: loggedIn(executor()), profile: P.name, revoke: !!RV, voice: !!VOICE_MAKER,
             quote: !!Q, at: !!F.atList };
  },
  // opts = {at: [wxid...], quote: {localId, createTime, name}}:@ 提醒 / 引用回复(4.1.13)
  send(to, content, opts) {
    opts = opts || {};
    const ex = executor();
    if (!loggedIn(ex)) return { ok: false, error: 'not_logged_in' };
    if (opts.at && opts.at.length && !F.atList) return { ok: false, error: 'at_unsupported' };
    if (opts.quote && !Q) return { ok: false, error: 'quote_unsupported' };
    const req = build('text', to, content, 0);
    if (!req) return { ok: false, error: 'request_mismatch' };
    if (opts.at && opts.at.length) {
      const list = opts.at.join(',');
      strAssign(req.obj.add(F.atList), longStr(list, req.keep));
      if (readStr(req.obj.add(F.atList)) !== list) return { ok: false, error: 'request_mismatch' };
    }
    if (!opts.quote) return launch(ex, req);
    const q = opts.quote;
    req.obj.add(F.type).writeU32(Q.type); req.obj.add(F.sub).writeU32(Q.sub);
    strAssign(req.obj.add(Q.chat), longStr(to, req.keep));
    strAssign(req.obj.add(Q.md5), longStr('d41d8cd98f00b204e9800998ecf8427e', req.keep));  // 空串 md5,界面就是这样
    FOREVER.push(req.keep, req.sp);
    return launch(ex, { mode: 3, obj: req.obj, sp: req.sp, keep: req.keep,
                        load: loadBufs(to, q.localId, q.createTime, q.name || '') });
  },
  sendImage(to, path, size) { return sendKind('image', to, path, size); },
  sendVideo(to, path, size) { return sendKind('video', to, path, size); },
  sendFile(to, path, size) { return sendKind('file', to, path, size); },
  sendVoice(to, silkPath, ms) {
    if (!VOICE_MAKER) return { ok: false, error: 'voice_unsupported' };
    const ex = executor();
    if (!loggedIn(ex)) return { ok: false, error: 'not_logged_in' };
    const req = buildVoice(to, silkPath, ms);
    if (!req) return { ok: false, error: 'request_mismatch' };
    return launch(ex, req);
  },
  // r = {type, sender, chat, localId, serverId(十进制字符串), sortSeq(字符串), createTime, status, content, source}
  revoke(r) {
    if (!RV) return { ok: false, error: 'revoke_unsupported' };
    const ex = executor();
    if (!loggedIn(ex)) return { ok: false, error: 'not_logged_in' };
    if (BATCH) return launch(ex, { mode: 2, keep: [], load: loadBufs(r.chat, r.localId, r.createTime, '') });
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
    hold(r.keep, r.sp);
    return { vt: r.obj.readPointer().sub(B).toString(16), hex: Array.from(new Uint8Array(r.obj.readByteArray(F.objSize))).map(b => b.toString(16).padStart(2, '0')).join('') };
  },
  poll() {
    return { step: G.add(11 * 8).readU64().toNumber(), calls: G.add(3 * 8).readU64().toNumber(),
             result: G.add(12 * 8).readU64().toNumber(),
             raw: [12, 13, 14].map(i => G.add(i * 8).readU64().toString(16)) };
  },
};
