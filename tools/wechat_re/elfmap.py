"""微信主程序(aarch64 ELF, PIE, strip)的最小解析:段、地址换算、动态重定位。

所有地址都是"模块内偏移"(镜像基址 0),与 Frida 里 `addr - Process.getModuleByName('wechat').base`
一致。注意 Ghidra 默认把镜像基址设为 0x100000,Ghidra 里的 FUN_0506177c 对应偏移 0x4f6177c。

二进制路径:命令行 --bin,或环境变量 WECHAT_BIN,默认 ./wechat。
从容器拷出:docker cp wxbot-ubuntu-manual:/opt/wechat/wechat ./wechat
"""
import argparse
import os
import struct

PF_X, PF_W = 1, 2


class Elf:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.d = f.read()
        d = self.d
        if d[:4] != b"\x7fELF" or d[4] != 2:
            raise SystemExit(f"{path} 不是 64 位 ELF")
        phoff, shoff = struct.unpack_from("<QQ", d, 0x20)
        phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from("<HHHHH", d, 0x36)
        self.loads = []  # (vaddr, offset, filesz, memsz, flags)
        for i in range(phnum):
            p_type, p_flags, p_off, p_vaddr, _pa, p_filesz, p_memsz, _al = struct.unpack_from(
                "<IIQQQQQQ", d, phoff + i * phentsize)
            if p_type == 1:
                self.loads.append((p_vaddr, p_off, p_filesz, p_memsz, p_flags))
        self.sections = {}
        raw = [struct.unpack_from("<IIQQQQIIQQ", d, shoff + i * shentsize) for i in range(shnum)]
        stro = raw[shstrndx][4]
        for nm, ty, fl, addr, off, sz, lk, inf, al, es in raw:
            name = d[stro + nm:d.index(b"\0", stro + nm)].decode()
            self.sections[name] = dict(type=ty, addr=addr, off=off, size=sz, link=lk, entsize=es)
        t = self.sections[".text"]
        self.text_va, self.text_off, self.text_size = t["addr"], t["off"], t["size"]
        self._rela = None

    # ---- 地址换算 ----
    def va2off(self, va):
        for vaddr, off, filesz, _m, _f in self.loads:
            if vaddr <= va < vaddr + filesz:
                return va - vaddr + off
        raise ValueError(f"0x{va:x} 不在任何文件内 LOAD 段")

    def seg_flags(self, va):
        for vaddr, _o, _fs, memsz, flags in self.loads:
            if vaddr <= va < vaddr + memsz:
                return flags
        return 0

    def is_code(self, va):
        return bool(self.seg_flags(va) & PF_X)

    def u32(self, va):
        return struct.unpack_from("<I", self.d, self.va2off(va))[0]

    def u64(self, va):
        return struct.unpack_from("<Q", self.d, self.va2off(va))[0]

    def cstr(self, va, limit=300):
        o = self.va2off(va)
        return self.d[o:self.d.index(b"\0", o)][:limit]

    def text(self):
        return self.d[self.text_off:self.text_off + self.text_size]

    # ---- 动态重定位(PIE 里 vtable 槽、GOT 槽都是 R_AARCH64_RELATIVE) ----
    def rela(self):
        """{位置: addend}。vtable 里的函数指针、GOT 里的 vtable 地址都在这里。"""
        if self._rela is None:
            self._rela = {}
            for s in self.sections.values():
                if s["type"] == 4:  # SHT_RELA
                    for j in range(0, s["size"], 24):
                        r_off, _info, r_add = struct.unpack_from("<QQq", self.d, s["off"] + j)
                        self._rela[r_off] = r_add
        return self._rela


def adrp_target(insn, pc):
    """ADRP 指令算出的页地址;不是 ADRP 返回 None。"""
    if (insn & 0x9F000000) != 0x90000000:
        return None
    imm = (((insn >> 5) & 0x7FFFF) << 2) | ((insn >> 29) & 3)
    if imm & (1 << 20):
        imm -= 1 << 21
    return (pc & ~0xFFF) + (imm << 12)


def parser(desc):
    ap = argparse.ArgumentParser(description=desc)
    ap.add_argument("--bin", default=os.environ.get("WECHAT_BIN", "wechat"),
                    help="微信主程序路径(默认 $WECHAT_BIN 或 ./wechat)")
    return ap


def hexint(s):
    return int(s, 16)
