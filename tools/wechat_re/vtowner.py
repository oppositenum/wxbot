"""从 vtable 地址找构造函数:vtable → vtable 组起点 → 指向它的 GOT 槽 → 读该槽的代码。

C++ 对象构造时会把 vptr 写进对象。PIE 里代码不直接写 vtable 地址,而是从 GOT 槽读
(GOT 槽的值 = vtable 组起点,由 R_AARCH64_RELATIVE 重定位填)。所以:
  1. vtable 地址往前找组起点:[offset_to_top=0 或负数][typeinfo 指针][第一个虚函数]...
  2. 在重定位表里找 addend == 组起点 的槽 = GOT 槽
  3. 扫 .text 里 ADRP+LDR 读这个槽的位置 = 写 vptr 的代码,所在函数通常就是构造函数
     (也可能是析构函数:析构开头会重置 vptr。看反编译区分:构造函数里有 memset/成员初始化)

vtable 地址来自运行时:Frida 读对象首 8 字节 - wechat 模块基址。

用法: python3 vtowner.py 0x7e07a18
然后: python3 fnstart.py <加载点>    或 Ghidra query.sh 看加载点所在函数
"""
import elfmap
import xref


def owner(elf, vt):
    R = elf.rela()
    p = vt
    while p > vt - 0x400:
        p -= 8
        top_is_reloc = p in R
        raw = elf.u64(p) if not top_is_reloc else None
        ti = R.get(p + 8)
        fn = R.get(p + 16)
        # 组起点:offset_to_top 是常量 0/负数(不是重定位),后面是 typeinfo(数据段)和虚函数(代码段)
        if (not top_is_reloc and raw is not None and (raw == 0 or raw >= 1 << 63)
                and ti is not None and not elf.is_code(ti) and fn is not None and elf.is_code(fn)):
            return p, ti
    return None, None


def main():
    ap = elfmap.parser(__doc__)
    ap.add_argument("vtable", nargs="+", type=elfmap.hexint)
    a = ap.parse_args()
    elf = elfmap.Elf(a.bin)
    R = elf.rela()
    inv = {}
    for k, v in R.items():
        inv.setdefault(v, []).append(k)
    for vt in a.vtable:
        grp, ti = owner(elf, vt)
        if grp is None:
            print(f"0x{vt:x}: 没找到 vtable 组起点")
            continue
        ap_ = grp + 16
        print(f"0x{vt:x}: 组起点 0x{grp:x}  地址点 0x{ap_:x}  第 {(vt - ap_) // 8} 槽  typeinfo 0x{ti:x}")
        for g in (grp, ap_):
            for slot in inv.get(g, []):
                loads = [hex(at) for _t, at in xref.scan(elf, [slot], want_ldr=True)]
                print(f"   GOT 槽 0x{slot:x} -> 0x{g:x}  读取点 {loads or '无'}")


if __name__ == "__main__":
    main()
