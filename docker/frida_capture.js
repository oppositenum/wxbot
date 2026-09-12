// Observe completed media file operations in this WeChat process. No UI calls,
// internal WeChat offsets, message sending, key extraction or shared-account pool.
'use strict';
const VERSION = 'media-files-v3';
const CAP_ROOT = '/root/wxbot_capture';
const libc = Process.getModuleByName('libc.so.6');
function native(name, ret, args) { return new NativeFunction(libc.getExportByName(name), ret, args); }
const linkFile = native('link','int',['pointer','pointer']);
const mkdir = native('mkdir','int',['pointer','uint']);
const access = native('access','int',['pointer','int']);
const readlink = native('readlink','long',['pointer','pointer','ulong']);
const stats = {version:VERSION, captures:0, failures:0, hooks:[], probe_captures:0};
const fdmap = new Map();
const listeners = [];
function str(s) { return Memory.allocUtf8String(s); }
function read(p) { try { return p.readUtf8String(); } catch (_) { return null; } }
function exists(p) { return access(str(p),0) === 0; }
function fdPath(fd) {
  const b = Memory.alloc(4096); const n = Number(readlink(str('/proc/self/fd/'+fd),b,4095));
  return n > 0 ? b.readUtf8String(n) : null;
}
function absolute(path, dirfd) {
  if (!path || path[0] === '/') return path;
  const base = dirfd === -100 ? '/proc/self/cwd' : '/proc/self/fd/'+dirfd;
  const b = Memory.alloc(4096); const n = Number(readlink(str(base),b,4095));
  return n > 0 ? b.readUtf8String(n)+'/'+path : null;
}
function classify(path) {
  if (!path || path.includes('/../') || path.includes('/./')) return null;
  let match = /^\/root\/xwechat_files\/([a-zA-Z0-9_.-]+)\/(.+)$/.exec(path);
  let probe = false;
  if (!match) {
    match = /^\/root\/wxbot_capture\/\.probe-source\/([a-zA-Z0-9_.-]+)\/(.+)$/.exec(path);
    probe = !!match;
  }
  if (!match || match[1] === '.' || match[1] === '..') return null;
  const rel = match[2], name = rel.slice(rel.lastIndexOf('/')+1);
  const image = /(?:^|\/)(?:Img|Bubble|ImageUtils)\//.test(rel) && /\.(?:dat|jpe?g|png|gif|webp)$/i.test(name);
  const video = /^msg\/video\//.test(rel) && /\.(?:mp4|jpe?g|png)$/i.test(name);
  if ((!image && !video) || name.length > 180) return null;
  return {account:probe?'_selftest':match[1], name:name, probe:probe};
}
function capture(path, why, identity) {
  const info = identity || classify(path);
  if (!info || !path || !exists(path)) return;
  const dir = CAP_ROOT+'/'+info.account; mkdir(str(CAP_ROOT),448); mkdir(str(dir),448);
  const dest = dir+'/'+info.name;
  if (exists(dest)) return;
  if (linkFile(str(path),str(dest)) === 0) {
    if (info.probe) stats.probe_captures++; else stats.captures++;
    send({tag:'CAP', why:why, probe:info.probe});
  } else stats.failures++;
}
function hook(name, callbacks) {
  try { listeners.push(Interceptor.attach(libc.getExportByName(name),callbacks)); stats.hooks.push(name); }
  catch (_) { /* Optional libc aliases differ between builds. */ }
}
for (const name of ['open','open64','openat','openat64']) {
  const at = name.startsWith('openat');
  hook(name, {
    onEnter(args) { this.path=read(args[at?1:0]); this.flags=args[at?2:1].toInt32(); },
    onLeave(ret) {
      if (!(this.flags & 3) || !this.path || !/\.(?:dat|jpe?g|png|gif|webp|mp4)$/i.test(this.path)) return;
      const fd=ret.toInt32(); if (fd < 0 || fdmap.size >= 8192) return;
      const path=fdPath(fd); if (classify(path)) fdmap.set(fd,path);
    }
  });
}
hook('close', {
  onEnter(args) { const fd=args[0].toInt32(); this.path=fdmap.get(fd); fdmap.delete(fd); },
  onLeave(ret) { if (ret.toInt32()===0 && this.path) capture(this.path,'closed'); }
});
for (const name of ['rename','renameat','renameat2']) {
  hook(name, {
    onEnter(args) { this.dest=name==='rename'?read(args[1]):absolute(read(args[3]),args[2].toInt32()); },
    onLeave(ret) { if (ret.toInt32()===0) capture(this.dest,'renamed'); }
  });
}
for (const name of ['unlink','unlinkat']) {
  hook(name, {onEnter(args) {
    const path=name==='unlink'?read(args[0]):absolute(read(args[1]),args[0].toInt32());
    capture(path,'before_remove');
  }});
}
rpc.exports = {
  status() { return Object.assign({},stats,{tracked_files:fdmap.size}); },
  // Controlled local-file exercise. It neither reads account data nor uses UI.
  selftest() {
    const root=CAP_ROOT+'/.probe-source'; mkdir(str(CAP_ROOT),448); mkdir(str(root),448);
    mkdir(str(root+'/test'),448); mkdir(str(root+'/test/temp'),448); mkdir(str(root+'/test/temp/ImageUtils'),448);
    const name='probe-'+Process.id+'-'+Date.now()+'.png'; const path=root+'/test/temp/ImageUtils/'+name;
    const fopen=native('fopen','pointer',['pointer','pointer']);
    const fwrite=native('fwrite','ulong',['pointer','ulong','ulong','pointer']);
    const fclose=native('fclose','int',['pointer']);
    const remove=native('unlink','int',['pointer']);
    const f=fopen(str(path),str('wb')); if (f.isNull()) return {ok:false,reason:'probe_open_failed'};
    const marker=str('WXBOT_HOOK_LOCAL_PROBE'); fwrite(marker,1,22,f); fclose(f);
    // libc stdio may use internal aliases; exercise the observed exported close
    // through a writable descriptor as well.
    const open=native('open','int',['pointer','int','uint']); const close=native('close','int',['int']);
    const fd=open(str(path),1,384); if(fd>=0) close(fd);
    const dest=CAP_ROOT+'/_selftest/'+name; const ok=exists(dest);
    remove(str(path)); if(exists(dest)) remove(str(dest));
    return {ok:ok,probe_captures:stats.probe_captures,version:VERSION};
  }
};
send({tag:'READY',version:VERSION,hooks:stats.hooks});
