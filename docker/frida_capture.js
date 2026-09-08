// wxbot 收图秒抢 Frida agent v2 —— 撤回也抢不走
// -----------------------------------------------------------------------------
// 实测微信图片落盘机制(诊断确认)：
//   · 发图：临时名 → libc `rename` 成正式名(.dat/明文.jpg)。
//   · 收图/下载：`.dat`(原图)、`_h.dat`(高清)、`_b.dat`(~500px) 都是
//     `open(O_WRONLY|O_CREAT|O_TRUNC) + write + close` **直写正式路径**(不经 rename！
//     只有 _b.dat 额外 self-rename)。→ 只 hook rename 会漏收图的 .dat/_h.dat。
//   · 撤回：微信 `unlink` 删掉本地 .dat。
// 故三管齐下(都极低频/非紧密循环，实测不卡不崩)：
//   ① rename onEnter 硬链接(发图 + 任何 rename 落定)
//   ② open(O_CREAT 图片) 记 fd→path，close 时硬链接(收图直写完成的瞬间)
//   ③ unlink onEnter 硬链接(撤回删图前最后一抢，兜住一切)
// 抢到的 inode 存 /root/wxbot_capture/<wxid>/<原basename>，撤回删原图字节也不丢。
// -----------------------------------------------------------------------------
const libc = Process.getModuleByName("libc.so.6");
const c_link  = new NativeFunction(libc.getExportByName("link"),  "int", ["pointer", "pointer"]);
const c_mkdir = new NativeFunction(libc.getExportByName("mkdir"), "int", ["pointer", "int"]);
const c_access= new NativeFunction(libc.getExportByName("access"),"int", ["pointer", "int"]);

const CAP_ROOT = "/root/wxbot_capture";
const O_CREAT = 0x40;
c_mkdir(Memory.allocUtf8String(CAP_ROOT), 0x1ff);

function rd(p) { try { return p.readUtf8String(); } catch (e) { return null; } }
function exists(path) { return c_access(Memory.allocUtf8String(path), 0) === 0; }

function isImg(s) {
  if (!s) return false;
  const hasDir = (s.indexOf("/Img/") >= 0 || s.indexOf("Bubble") >= 0 || s.indexOf("ImageUtils") >= 0);
  const ext = (s.indexOf(".dat") >= 0 || s.indexOf(".jpg") >= 0 ||
               s.indexOf(".png") >= 0 || s.indexOf(".webp") >= 0);
  return hasDir && ext;
}
function base(s) { const i = s.lastIndexOf("/"); return i < 0 ? s : s.slice(i + 1); }
function acct(s) {
  const m = /\/(wxid_[0-9a-z]+_[0-9a-z]+)\//i.exec(s);
  return m ? m[1] : "_shared";
}

function capture(path, why) {
  if (!path || !exists(path)) return;
  const dir = CAP_ROOT + "/" + acct(path);
  c_mkdir(Memory.allocUtf8String(dir), 0x1ff);
  const dst = dir + "/" + base(path);
  if (exists(dst)) return;                       // 已抢过，跳过
  const rc = c_link(Memory.allocUtf8String(path), Memory.allocUtf8String(dst));
  if (rc === 0) send({ tag: "CAP", dst: dst, why: why });
}

// ① rename 家族
["rename", "renameat", "renameat2"].forEach(name => {
  let addr; try { addr = libc.getExportByName(name); } catch (e) { return; }
  if (!addr) return;
  Interceptor.attach(addr, {
    onEnter(args) {
      const oldp = name === "rename" ? rd(args[0]) : rd(args[1]);
      const newp = name === "rename" ? rd(args[1]) : rd(args[3]);
      if (isImg(oldp)) { try { capture(oldp, "rename"); } catch (e) {} }
      if (isImg(newp) && newp !== oldp) { try { capture(newp, "rename"); } catch (e) {} }
    }
  });
});

// ② open/openat(O_CREAT 图片) → 记 fd；close 时硬链接(收图直写完成)
const fdmap = {};
function hookOpen(name, pathIdx, flagIdx) {
  let addr; try { addr = libc.getExportByName(name); } catch (e) { return; }
  if (!addr) return;
  Interceptor.attach(addr, {
    onEnter(args) { this.p = rd(args[pathIdx]); this.created = (args[flagIdx].toInt32() & O_CREAT) !== 0; },
    onLeave(ret) {
      if (!this.created || !this.p || !isImg(this.p)) return;
      const fd = ret.toInt32();
      if (fd >= 0) fdmap[fd] = this.p;
    }
  });
}
hookOpen("open", 0, 1);
hookOpen("openat", 1, 2);

(function () {   // close：只对我们跟踪的图片 fd 动作(其余 close 仅一次哈希查找，极廉价)
  let addr; try { addr = libc.getExportByName("close"); } catch (e) { return; }
  if (!addr) return;
  Interceptor.attach(addr, {
    onEnter(args) {
      const fd = args[0].toInt32();
      const p = fdmap[fd];
      if (p) { delete fdmap[fd]; try { capture(p, "close"); } catch (e) {} }
    }
  });
})();

// ③ unlink 家族：撤回/清理删图前最后一抢
["unlink", "unlinkat"].forEach(name => {
  let addr; try { addr = libc.getExportByName(name); } catch (e) { return; }
  if (!addr) return;
  Interceptor.attach(addr, {
    onEnter(args) {
      const p = name === "unlink" ? rd(args[0]) : rd(args[1]);
      if (isImg(p)) { try { capture(p, "unlink"); } catch (e) {} }
    }
  });
});

send({ tag: "READY", root: CAP_ROOT });
