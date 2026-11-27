"""从函数内某条指令往回找函数入口(strip 过的二进制没有符号)。

识别 `stp x29, x30, [sp, #-N]!`(pre-index)、`stp x29, x30, [sp, #N]`(带偏移,前面通常有
`sub sp, sp, #M`)或 `paciasp`;前一条若是 `sub sp, sp, #N` 一并算入。
只是启发式:Ghidra 全量分析后以 Ghidra 的函数边界为准。

用法: python3 fnstart.py 0x39dee7c
"""
import elfmap


def find(elf, a, limit=0x20000):
    p = a - 4
    while p > a - limit:
        w = elf.u32(p)
        if (w & 0xFFC07FFF) in (0xA9807BFD, 0xA9007BFD) or w == 0xD503233F:
            s = p
            if (elf.u32(p - 4) & 0xFF8003FF) == 0xD10003FF:
                s = p - 4
            return s
        p -= 4
    return None


def main():
    ap = elfmap.parser(__doc__)
    ap.add_argument("addr", nargs="+", type=elfmap.hexint)
    a = ap.parse_args()
    elf = elfmap.Elf(a.bin)
    for x in a.addr:
        s = find(elf, x)
        print(f"0x{x:x} -> 函数入口 {hex(s) if s else '未找到'}" + (f"  (距离 0x{x - s:x})" if s else ""))


if __name__ == "__main__":
    main()
