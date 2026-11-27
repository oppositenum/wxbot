"""逐字段对比 hook_sendmessage.js 抓到的两条请求(比如"界面发的"和"我们构造的")。

字符串字段按内容比,其余按 8 字节原值比;指针值每次都不同,用 --ignore-ptr 忽略看起来像堆指针的差异。
注意:短字符串(libc++ SSO)尾部是未初始化内存,短串所在的几个 8 字节出现差异不代表字段不同。

用法: python3 diff_req.py send.jsonl 3 5 --ignore-ptr     # 第 3 行和第 5 行(从 1 数)
"""
import argparse
import json


def looks_ptr(v):
    x = int(v, 16)
    return 0xaaaa00000000 <= x < 0x1000000000000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("a", type=int)
    ap.add_argument("b", type=int)
    ap.add_argument("--ignore-ptr", action="store_true")
    o = ap.parse_args()
    rows = [json.loads(l) for l in open(o.jsonl, encoding="utf-8") if l.strip()]
    A, B = rows[o.a - 1], rows[o.b - 1]
    print("A:", A.get("t"), A.get("reqVt"), "  B:", B.get("t"), B.get("reqVt"))
    for k in sorted(set(A.get("strs", {})) | set(B.get("strs", {})), key=lambda s: int(s[1:], 16)):
        print(f"  str {k:7} A={A['strs'].get(k)!r:40} B={B['strs'].get(k)!r}")
    wa, wb = A.get("words", {}), B.get("words", {})
    for k in sorted(set(wa) | set(wb), key=lambda s: int(s[1:], 16)):
        va, vb = wa.get(k, "0"), wb.get(k, "0")
        if va == vb:
            continue
        if o.ignore_ptr and looks_ptr(va) and looks_ptr(vb):
            continue
        print(f"  {k:7} A={va:18} B={vb:18}  <-- 不同")


if __name__ == "__main__":
    main()
