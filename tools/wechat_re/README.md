# wechat_re

微信主程序逆向用的小工具。过程、结论、升级后的重新定位步骤都在 [docs/wechat-direct-send-re.md](../../docs/wechat-direct-send-re.md)。

| 文件 | 作用 |
| --- | --- |
| `elfmap.py` | ELF 段、地址↔文件偏移、动态重定位(其余脚本的公共模块) |
| `xref.py` | 谁引用了某地址(字符串/GOT 槽),几秒出结果 |
| `vtowner.py` | vtable → GOT 槽 → 写 vptr 的代码(找构造函数) |
| `fnstart.py` | 指令 → 函数入口(启发式) |
| `mdparse.py` | 解析微信 crashpad minidump |
| `ghidra/` | headless 全量分析与按地址查询(反编译/调用者/被调用者) |
| `frida/` | 在 SendMessage 入口拷请求对象、两条请求逐字段对比;`hook_args.js` 拷任意函数入口的 x0~x2 指向内容 |

二进制路径:`--bin` 或环境变量 `WECHAT_BIN`。地址一律是模块内偏移(Ghidra 里要加 0x100000)。
