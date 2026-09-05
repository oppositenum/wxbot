"""lldb 脚本：断点 CommonCrypto 的 CCCryptorCreate，抓取 AES-256 key（keyLen==32）。

WeChat(WCDB) 用 CommonCrypto 做页加解密：
    CCCryptorCreate(op, alg=AES, options, key(x3), keyLen(x4)=32, iv, ...)
每次解密一个 DB 页都会调用，x3 指向 32 字节的 raw enc_key —— 正是我们要的库密钥。

用法（由 run_cc_catch.sh 调起）：
    sudo lldb -b -o "process attach -p <PID>" \
        -o "command script import core/cc_catch.py" -o "cont"
抓到的去重 key 写入 /tmp/cc_keys.txt，每行一个 64-hex。
"""
import lldb

OUT = "/tmp/cc_keys.txt"
SEEN = set()
HITS = [0]
MAX_HITS = 5_000_000   # 靠时间(外部 kill)控制时长，不靠命中数提前 detach
MAX_KEYS = 400


def bp_cb(frame, bp_loc, extra_args, internal_dict):
    HITS[0] += 1
    try:
        x4 = frame.FindRegister("x4").GetValueAsUnsigned()
        if x4 == 32:
            x3 = frame.FindRegister("x3").GetValueAsUnsigned()
            err = lldb.SBError()
            proc = frame.GetThread().GetProcess()
            data = proc.ReadMemory(x3, 32, err)
            if err.Success() and data:
                h = data.hex()
                if h not in SEEN:
                    SEEN.add(h)
                    with open(OUT, "a") as f:
                        f.write(h + "\n")
    except Exception:
        pass
    if HITS[0] >= MAX_HITS or len(SEEN) >= MAX_KEYS:
        try:
            frame.GetThread().GetProcess().Detach()
        except Exception:
            pass
    return False  # 自动继续，不停住


def __lldb_init_module(debugger, internal_dict):
    # 不清空，跨次累积；预载已抓到的 key 去重
    try:
        for ln in open(OUT):
            ln = ln.strip()
            if len(ln) == 64:
                SEEN.add(ln)
    except OSError:
        pass
    target = debugger.GetSelectedTarget()
    bp = target.BreakpointCreateByName("CCCryptorCreate")
    bp.SetCondition("$x4 == 32")   # 只在 AES-256 (keyLen=32) 时触发，降低开销
    bp.SetScriptCallbackFunction("cc_catch.bp_cb")
    print(f"[cc_catch] breakpoint set on CCCryptorCreate, locations={bp.GetNumLocations()}")
