"""找"哪段代码引用了某个地址"(字符串、vtable、全局对象),不依赖 Ghidra,几秒出结果。

aarch64 取地址是两条指令:ADRP xN, page  +  ADD xN, xN, #lo12(或 LDR xM, [xN, #lo12])。
本脚本扫 .text 里所有 ADRP,看后面紧跟的 ADD/LDR 能否拼出目标地址。

用法:
  python3 xref.py 0xfdeec6                # 谁引用了这个字符串(ADRP+ADD)
  python3 xref.py --ldr 0x80df308         # 谁从这个 GOT 槽读指针(ADRP+LDR)
找字符串地址:  strings -t x wechat | grep newsendmsg   (rodata 段偏移=虚拟地址,其他段用 elfmap 换算)
"""
import struct

import elfmap


def scan(elf, targets, want_ldr=False, window=40):
    T = elf.text()
    va0 = elf.text_va
    pages = {t & ~0xFFF: [] for t in targets}
    for t in targets:
        pages[t & ~0xFFF].append(t)
    hits = []
    for i in range(0, len(T) - 4, 4):
        insn = struct.unpack_from("<I", T, i)[0]
        page = elfmap.adrp_target(insn, va0 + i)
        if page is None or page not in pages:
            continue
        rd = insn & 31
        for k in range(1, window):
            if i + 4 * k + 4 > len(T):
                break
            n = struct.unpack_from("<I", T, i + 4 * k)[0]
            if ((n >> 5) & 31) != rd:
                continue
            is_ldr = (n & 0xFFC00000) == 0xF9400000
            is_add = (n & 0xFF800000) == 0x91000000
            if not (is_ldr or is_add):
                continue
            lo = ((n >> 10) & 0xFFF) * (8 if is_ldr else 1)
            for t in pages[page]:
                if lo == (t & 0xFFF) and is_ldr == want_ldr:
                    hits.append((t, va0 + i + 4 * k))
            break
    return hits


def main():
    ap = elfmap.parser(__doc__)
    ap.add_argument("--ldr", action="store_true", help="匹配 ADRP+LDR(读 GOT 槽),默认匹配 ADRP+ADD")
    ap.add_argument("addr", nargs="+", type=elfmap.hexint)
    a = ap.parse_args()
    elf = elfmap.Elf(a.bin)
    for t, at in scan(elf, a.addr, a.ldr):
        print(f"0x{t:x} 被引用于 0x{at:x}")


if __name__ == "__main__":
    main()
