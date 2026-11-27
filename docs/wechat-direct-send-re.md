# 微信底层直发：逆向过程与调用方法

| 字段 | 值 |
| --- | --- |
| 适配版本 | WeChat Linux 4.1.8，aarch64，`/opt/wechat/wechat` sha256 `e40116597803a5e0d9fc4a2b727df4b8f7a3dd6de1c336140ea373be2b6626e9` |
| 日期 | 2026-11-26 ~ 2026-11-27 |
| 代码 | `core/direct_send_agent.js`（注入微信的 agent）、`core/direct_send.py`（会话与锁）、`core/direct_sender.py`（bot 回复通道）、`core/send_mode.py`（开关） |
| 工具 | `tools/wechat_re/`（静态分析脚本、Ghidra 脚本、Frida 抓包脚本） |

本文写给"微信升级了，偏移全失效"的那一天。第 1 节是结论速查，第 2 节是环境和工具，第 3 节按时间顺序写当时怎么一步步找到的（包括走不通的路，免得重走），第 4 节是升级后的重新定位清单，第 5 节是坑。

所有地址都是**模块内偏移**（镜像基址 0），等于 Frida 里 `addr - Process.getModuleByName('wechat').base`。Ghidra 默认镜像基址是 `0x100000`，所以 Ghidra 里的 `FUN_04caee74` 就是偏移 `0x4baee74`。

---

## 1. 结论速查

### 1.1 一次直发做了什么

```
后台页面 / bot 回复
  └─ core/direct_send.py      持有一个 Frida 会话(微信 PID 变了自动重附加)、全局锁、写历史
       └─ agent.send / sendImage(core/direct_send_agent.js,在微信进程里跑)
            1. operator new 一块内存,调 make_shared 构造函数 → 得到"发送请求"对象 Req
            2. 用微信自己的 std::string 赋值函数填 收件人 / 正文或图片路径,再填类型等整数字段
            3. 手工拼一个 libc++ std::function<void()>,回调是我们 CModule 里的 C 函数 f_call
            4. co_launch(业务执行器, 位置信息, &function) → 微信在业务线程的协程里执行 f_call
       f_call(在协程里):
            5. 服务定位链 svcA() → svcB(a) → svcC(b) 得到发送服务 svc,校验 vtable
            6. SendMessage(svc, &shared_ptr<Req>),x8 指向本次独立的返回值缓冲
            7. 写 step=9
       Python 轮询 step:9=已交给微信;1~4=服务定位失败(一定没发);超时=结果不确定
```

之后的事全由微信自己完成：写本地库、生成 `SendMsgRequestNew` protobuf、（图片）压缩/缩略图/AES 加密/上传 CDN、走 mars 长连接发出。

### 1.2 偏移表（4.1.8）

| 名称 | 偏移 | 说明 |
| --- | --- | --- |
| `SendMessage` | `0x4baee74` | `(x0=svc, x1=const shared_ptr<Req>&)`，结果经 **x8** 返回 |
| 服务定位 A | `0x483104c` | `()`，x8 返回 shared_ptr |
| 服务定位 B | `0x4950c40` | `(a)`，x8 返回 shared_ptr |
| 服务定位 C | `0x496004c` | `(b)`，x8 返回 shared_ptr，结果即 svc |
| svc 的 vtable | `0x7f1fb80` | 校验用，不符就不调用 |
| `operator new` | `0x7d7e420` | 微信自带分配器，必须用它分配 Req 的内存 |
| 文字 Req 构造（make_shared） | `0x39deee4` | 控制块 0x18 + 对象 0x410 = **0x428**，对象在 `blk+0x18`，vtable `0x7e1b790` |
| 图片 Req 构造（make_shared） | `0x39dee1c` | 0x18 + 0x450 = **0x468**，vtable `0x7e07a18` |
| `std::string` 拷贝赋值 | `0x3639df8` | libc++ `operator=(const string&)` |
| `co_launch` | `0x49bf7bc` | `(x0=执行器, x1=位置信息, x2=&std::function, w3=1)`，x8 返回协程句柄 |
| 应用上下文全局量 | `0x82bb2a8` | `*(B+0x82bb2a8)+0x290` = 业务执行器；执行器 `+0x10` 为 0 表示还没登录完 |
| 任意合法 `type_info` | `0x7f6a440` | 给 std::function 的 `target_type()` 返回 |

### 1.3 Req 对象字段（文字、图片共用基类）

| 偏移 | 类型 | 文字 | 图片 |
| --- | --- | --- | --- |
| `+0x00` | vptr | `0x7e1b790` | `0x7e07a18` |
| `+0x6c` | u32 | 1 | 1 |
| `+0x70` | u32 | 7 | 7 |
| `+0x78` | std::string | 收件人 wxid / `xxx@chatroom` / `filehelper` | 同左 |
| `+0x94` | u32 | 1（文字） | 3（图片） |
| `+0xa0` | std::string | — | 本地图片路径（微信用户可读） |
| `+0xe8` | u32 | 正文 UTF-8 字节数 | 文件字节数 |
| `+0x3c8` | std::string | 构造函数自动生成的 UUID（客户端消息 id） | 同左 |
| `+0x3e0` | std::string | 正文 | — |

字符串都是 libc++ 布局：首字节 bit0=1 为长串（`+8` 长度，`+16` 数据指针），否则短串（长度 = 首字节>>1，数据从 `+1` 起，最多 22 字节）。

### 1.4 怎么判断真的发出去了

agent 的 step=9 只说明 SendMessage 已返回。真正结果看解密后的消息库 `message` 里 `Msg_<md5(会话id)>` 表：

| `status` | 含义 |
| --- | --- |
| 2 | 发送成功 |
| 5 | 发送失败（界面上是红色感叹号） |
| 3 | 收到的消息 |

图片失败时 `message_content`（zstd 压缩的 XML）里 `cdnbigimgurl`、`aeskey`、`md5` 为空、`length="0"`，说明 CDN 根本没上传。

---

## 2. 环境与工具

### 2.1 目标环境

- 微信跑在 Docker 容器 `wxbot-ubuntu-manual` 里（Ubuntu 24.04，aarch64），用户 `wechat`。
- 容器有 `CAP_SYS_PTRACE` + `seccomp=unconfined`，且没有 yama，**`wechat` 用户就能用 Frida 附加微信**，后台进程（同为 wechat 用户）因此可以直接持有 Frida 会话。
- 主程序 130MB，strip（无符号）、PIE。类名被 MD5 混淆，只有静态链接的 Qt 类保留名字。日志字符串、源文件名（`co_core.cpp`、`mm_cgi.h`、`net_core.cc`）大量保留，是最重要的线索。

### 2.2 工具

| 工具 | 用途 | 位置 |
| --- | --- | --- |
| Frida 17（Python 绑定） | 动态 hook、在微信里调函数 | 容器 `/tmp/fr`，安装见 `tools/wechat_re/frida/run.py` 顶部 |
| Ghidra 12 headless + JDK 21 | 全量反编译，查调用关系 | `tools/wechat_re/ghidra/run_full.sh`、`query.sh` |
| `elfmap.py` | ELF 段解析、地址↔文件偏移、动态重定位表 | `tools/wechat_re/` |
| `xref.py` | 几秒内找"谁引用了某地址"（ADRP+ADD / ADRP+LDR） | 同上 |
| `vtowner.py` | vtable → GOT 槽 → 写 vptr 的代码（找构造函数） | 同上 |
| `fnstart.py` | 指令 → 函数入口（启发式，可能差一两条指令） | 同上 |
| `mdparse.py` | 解析 crashpad minidump，崩溃 pc/寄存器/栈上返回地址 | 同上 |
| `frida/hook_sendmessage.js` + `run.py` | 在 SendMessage 入口拷下请求对象 | `tools/wechat_re/frida/` |
| `frida/diff_req.py` | 两条请求逐字段对比 | 同上 |

二进制先拷出来：`docker cp wxbot-ubuntu-manual:/opt/wechat/wechat ./wechat`，然后 `export WECHAT_BIN=$PWD/wechat`。

Ghidra 全量分析 130MB 约 45 分钟（8 核，10G 内存），`PreFast.java` 关掉了 10 个最慢的分析器，否则跑不完。之后 `query.sh` 查一批地址只要一两分钟。REA 的 Ghidra 直接分析这个二进制会超时，所以用 headless 后台跑。

### 2.3 用来定位的"标记消息"

在界面里给文件传输助手发一条独特的长字符串（≥16 字节，避开 std::string 的短串优化），例如 `REAPROBE7731DEADBEEF`，然后在内存里搜这串字节，或者在候选函数的参数里找它。后来全部改用"界面发一条 + hook 拷对象"。

---

## 3. 分析过程（按时间顺序）

### 3.1 协议层：先知道微信"发什么"

`strings -t x wechat | grep -i sendmsg` 能捞出主程序内嵌的 CGI 路由表和 protobuf 描述符：

- 发文字 = CGI `newsendmsg`，cmdid 237，路径 `/cgi-bin/micromsg-bin/newsendmsg`
- 请求 protobuf = `micromsg.SendMsgRequestNew`：`ToUserName`、`Content`、`Type`、`CreateTime`、`ClientMsgId`、`MsgSource`
- 发图 = `uploadmsgimg`（cmdid 9），先上传 CDN

这一步只说明协议长什么样。自己拼 protobuf 发包要处理 mmtls 加密、长连接、序列号、CDN，不现实。目标因此定为：**找到业务层"给收件人和内容就发"的函数，在微信进程里直接调它**。

### 3.2 走不通的路（不要再试）

| 尝试 | 结果 | 原因 |
| --- | --- | --- |
| hook `libilink2.so` 的 `TdiNetwork::SendRequest` | 聊天消息零触发 | libilink2 只管登录、鉴权、IoT 通道 |
| hook `libilink_protobuf` 的序列化函数 | 抓不到 | 主程序静态链接了自己的 protobuf |
| hook libc `send` / `write` / `memcpy` | 抓不到业务调用栈 | mars 在独立网络线程写 socket；主程序用自带的 memcpy |
| 扫 libilink_network 主 vtable 全部 22 个槽 | 零命中明文 | 传输层只见密文，且没有 wechat! 业务帧 |
| Frida 硬件断点 `setHardwareWatchpoint` | **微信闪退**（两次） | 与容器 ptrace / 反调试不兼容 |
| Frida Stalker 跟踪主线程 | **微信闪退** | 同上 |
| `Thread.backtrace(ACCURATE)` 在 mars 网络线程 | **闪退** | 一律用 FUZZY |
| 追"持有正文的长驻对象"（vtable `0x7efa348`） | 死胡同 | 那是会话摘要对象，跨消息复用，不是待发消息 |

结论：明文发送接口只存在于主程序业务层，只能用软件 hook（`Interceptor.attach`）加静态分析去找。

### 3.3 业务层定位：从日志字符串到发送链

1. 找带业务含义的日志字符串：`StartSendMessageSyncStage`、`GetAddSendMessageToDb`、`newsendmsg`。用 `xref.py <字符串地址>` 找引用它们的代码，再用 `fnstart.py` 找到所在函数。
2. 在这些函数上挂**只记次数**的 hook，界面发一条消息，看哪些恰好触发一次。得到 `0x4baef14 → 0x4bb09a4 → 0x4bdbb74`（准备 → 写库 → 进入发送阶段）。
3. `0x4bdbb74` 触发后，马上扫一遍内存，找 `<长度><标记>` 形式的 protobuf，抓到了 `SendMsgRequestNew` 明文（约 30ms 后就被释放）。这一步验证了 3.1 的协议结论。
4. Ghidra 全量分析完成后，用 `query.sh` 查调用关系，得到静态发送链：
   `0x4f746ec`（按消息类型分发，内含 `TextMessageHandler`/`ImageMessageHandler` 字符串）→ 文字处理器投递任务 → `0x4fcbc0c` 组装 `SendMsgRequestNew` → `CoCgiSendRequest`（`0x4f62668`，`mm_cgi.h`，带退避重试）→ mars。
5. 在 `0x4fcbc0c` 上挂 hook（回调里只存寄存器，解析放到 setInterval），读到了**成品消息对象**（vtable `0x7f03780`）：`+0x30` 收件人、`+0xe8` 正文、`+0x0c` 类型、`+0xb8` 状态。

### 3.4 往上找"最上层的发送入口"

从 `0x4baef14` 往调用者方向走，找到界面层函数 `0x542d3c0`：它遍历 `vector<shared_ptr<Msg>>`，用三步服务定位拿到发送服务 `svc = C(B(A()))`，然后调用 **`SendMessage = 0x4baee74(svc, const shared_ptr<Req>&)`**。

在 `0x4baee74` 入口挂 hook，拷下 x1 指向的对象（就是 `tools/wechat_re/frida/hook_sendmessage.js`），发现：

- 传进来的不是成品消息，而是一个"发送请求" Req（vtable `0x7e1b790`，约 0x410 字节）。
- Req 被**移动构造**消费后会清零，所以必须在 onEnter 里同步拷贝。
- 字段见 1.3 节。

到这里，"调用 SendMessage 就能发"的入口已经找到。

### 3.5 在微信进程里调用 SendMessage

需要解决四件事：

**(1) 构造 Req。** `vtowner.py 0x7e1b790` 能找到读取 Req vtable 的代码，所在函数 `0x39deee4` 就是 `make_shared<Req>` 的构造函数（反编译能看到：写控制块 vptr，`memset(obj, 0, 0x410)`，生成 UUID，写对象 vptr）。用微信自己的 `operator new(0x428)` 分配内存，再调它即可。字符串字段用微信的 `std::string` 赋值函数 `0x3639df8` 写入，这样内存由微信的分配器管理。

**(2) 必须在协程里调。** 直接从 Frida 线程调用 SendMessage，会在 `co_core.cpp` 里 abort，报 `"%@() must be called from coroutine context"`。解决办法是照抄界面层启动协程的写法：函数 `0x542cc30` 的被调用者里有一组函数：取上下文 `0x482fd04`（读全局 `0x82bb2a8`）、取执行器 `0x4831138`（上下文 `+0x290`）、构造位置信息 `0x366adf4`、启动协程 **`co_launch 0x49bf7bc`**、`operator new 0x7d7e420`。我们手工拼一个 libc++ `std::function<void()>`：对象 0x28 字节，`+0x20` 存 `__f_` 指针；`__f_` 指向的小对象首 8 字节是 vtable，9 个槽依次是 析构 / 删除析构 / clone / clone(placement) / destroy / destroy_deallocate / **operator()** / target / target_type，全部用 CModule 里的 C 函数填，operator() 就是 f_call。然后调用 `co_launch(执行器, &loc, &function, 1)`。

**(3) 服务对象要在协程里取。** 从 Frida 线程调服务定位链会返回空，所以把定位链放进 f_call，在协程里执行。

**(4) x8 返回约定。** 服务定位函数、SendMessage、co_launch 都通过 **x8** 返回结构体（Ghidra 显示为 `param_1`，参数整体后移一位）。Frida CModule 用的 TinyCC 不会设置 x8，所以每个这类函数都套一个跳板：`mov x8, x4; ldr x16, =target; br x16`，调用时把返回缓冲区作为第 5 个参数传进去。跳板内存要 `Memory.alloc` 后 `Memory.protect(rwx)` 直接写，`Memory.patchCode` 不行。

第一次跑通时消息还是没发出去，问题在下面 3.6。

### 3.6 Bug 1：字符串被 Frida GC 释放 → 收件人是乱码

现象：调用正常返回，但写本地库被拒（返回 `{2, 200}`），后面的组包和网络都没执行。

方法：界面上发一条 `UI_COMPARE_01`，用 hook 拷下真实 Req，再拷下我们自己构造的 Req，逐字段对比（现在用 `diff_req.py`）。结果收件人、正文的**长度字节是对的，内容却是一串指针字节**（`b0 31 48 68 ff ff ...`），正好是 glibc 已释放块开头的 tcache next 指针和 key。

原因：`Memory.allocUtf8String` 返回的数据指针只存在 JS 局部变量里，函数返回后就被 Frida GC 回收了，`std::string` 赋值拷到的是已释放的内存。

修复：所有交给微信的 Frida 内存都放进全局数组保留引用（agent 里的 `hold()`）。

**副作用（已处理）**：修复前那几次失败的直发，让微信把乱码收件人写进了 `Name2Id` 表，bot 读库时 UTF-8 解码失败，每一轮都报错。`core/db.py` 现在对 TEXT 用 `errors="replace"` 解码。

### 3.7 加图片：同一个入口，换一个请求子类

1. 挂上 `hook_sendmessage.js`，用界面发送一张图。结果图片**也走 `0x4baee74`**，只是 Req 的 vtable 变成 `0x7e07a18`，`+0x94=3`，`+0xa0` 是图片路径（界面会先把图片复制到 `xwechat_files/<账号>/temp/InputTemp/<uuid>.png`），`+0xe8` 是文件字节数（和该临时文件大小完全一致）。
2. `vtowner.py 0x7e07a18` 找到读取点 `0x39dee7c`，`fnstart.py` 得到函数入口 **`0x39dee1c`**。反编译和文字版 `0x39deee4` 结构相同，区别只是 `memset` 了 0x450 字节，所以分配 0x18+0x450=0x468。
3. agent 里按 1.3 节填字段，交给同一个 SendMessage。路径给我们自己的副本（`work/direct_media/`）即可，不必放进 InputTemp。

### 3.8 Bug 2：返回值缓冲区复用 → 同一会话只有第一张图成功

现象：每个 Frida 会话里第一张图成功，之后的图全部失败（status 5，CDN 字段为空），而且之后连界面发图也受影响，只能重启微信才能恢复。文字不受影响。

原因：SendMessage 经 x8 返回的对象，对图片来说是**异步上传任务**，要一直存活到 CDN 上传结束。最初所有发送共用同一块返回值缓冲区，第二次发送把第一张图还在运行的任务覆盖了。

修复：每次发送单独分配一块缓冲区（服务定位结果 + SendMessage 返回值），并保留引用（`hold()`，保留最近 256 次）。修复后同一会话背靠背连发 3 张，全部成功。

---

## 4. 微信升级后怎么重新定位

先确认新版本 sha256 与 `core/direct_send.py` 里的 `SUPPORTED_SHA256` 不同。不同时直发会自动禁用（页面显示"微信版本未适配"），bot 回复会回退到界面自动化，所以不着急。

### 4.1 准备

```bash
docker cp wxbot-ubuntu-manual:/opt/wechat/wechat ./wechat && export WECHAT_BIN=$PWD/wechat
sha256sum wechat
# 后台跑 Ghidra 全量分析(约 45 分钟)
GHIDRA_HOME=... JAVA_HOME=... tools/wechat_re/ghidra/run_full.sh $WECHAT_BIN ~/wxbot-re/ghidra/proj
```

### 4.2 按顺序重新找每个偏移

| 步骤 | 找什么 | 怎么找 | 怎么确认 |
| --- | --- | --- | --- |
| 1 | `SendMessage` | 字符串 `StartSendMessageSyncStage` / `GetAddSendMessageToDb` → `xref.py` → `fnstart.py` 得到写库函数；在 Ghidra 里沿 callers 往上找两三层，找到参数是 `(svc, shared_ptr&)`、被界面层遍历消息列表的函数调用的那个 | 在候选入口挂 `hook_sendmessage.js`，界面发一条，`strs` 里能看到收件人和正文 |
| 2 | 文字 / 图片 Req 的 vtable | 第 1 步抓到的 `reqVt` 字段（文字、图片各发一条） | 两者 `+0x94` 分别是 1 和 3 |
| 3 | 两个 make_shared 构造函数 | `vtowner.py <reqVt>` 的读取点 → `fnstart.py` / Ghidra | 反编译里有 `memset(param_1+3, 0, N)`；N+0x18 就是要分配的大小 |
| 4 | 字段偏移 | 对比第 1 步抓到的 `strs` / `words` 和 1.3 节表格 | 收件人、正文、路径都能在 `strs` 里看到 |
| 5 | 服务定位链和 svc vtable | Ghidra 里调用 SendMessage 的界面函数，反编译里 SendMessage 前的三层嵌套调用 | hook 抓到的 `svcVt` |
| 6 | `co_launch`、取上下文、取执行器、全局量 | 字符串 `must be called from coroutine context`（`co_core.cpp`）附近；更直接的方法是在界面层找一个启动协程的函数，看它的 callees：`operator new`、位置信息构造、取上下文（读一个全局指针）、取执行器（`+0x290` 一类的固定偏移）、co_launch（入口读 `[x0+0x10]` 判断执行器是否就绪） | agent 的 `ready()` 返回 `logged_in: true` |
| 7 | `std::string` 赋值、`operator new` | 赋值函数有几千个调用者，反编译就是 libc++ `operator=`（见 3.5）；`operator new` 是构造点前最常见的分配调用 | 构造出的 Req 用 `diff_req.py` 和界面发的比，除了指针和 UUID 外完全一致 |
| 8 | `type_info` | 任意一个 typeinfo 地址即可（`vtowner.py` 会打印 typeinfo） | — |

### 4.3 改代码并验证

1. 改 `core/direct_send_agent.js` 顶部的 `OFF`，以及 `build()` 里的对象大小和字段偏移。
2. 在 `core/direct_send.py` 的 `SUPPORTED_SHA256` 里加上新 sha256。
3. 先挂 `hook_sendmessage.js`，再用直发页给文件传输助手发文字，`diff_req.py` 对比"界面发的"和"直发的"，除了指针和 UUID 外应该完全一致。
4. 用 1.4 节的方法查消息库 status=2。再在**同一个会话里连发 3 张图**（验证 3.8 节的问题没有复发），再发一次群聊。
5. 跑 `tests/test_direct_send.py`、`tests/test_direct_sender.py`。

---

## 5. 坑清单

1. **协程栈很小**：发送路径上的 hook 回调跑在业务协程栈上。onEnter 里只做 `Memory.copy` 和存寄存器，读字符串、backtrace 都放到 setInterval 里做。在发送链上挂普通 JS 计数 hook 也撑爆过一次（微信闪退，minidump 的 pc 落在 libfrida-agent 里）。直发 agent 本身不挂任何 hook。
2. **Frida GC**：交给微信的每一块 `Memory.alloc` / `allocUtf8String` 内存都要保留引用，包括异步任务用到的。
3. **x8 返回**：Ghidra 显示 `param_1` 是返回值的函数，调用时要设 x8，见 3.5 (4)。
4. **CModule**：`extern` 符号要声明成 `char sym[]` 取地址，写成 `void* sym` 会把代码字节当指针读；TinyCC 不接受整数到指针的隐式转换（`(void*)1`）。
5. **frida 17**：script 没有 `detached` 信号；agent 加载时的异常只通过 `message`（type=error）报告，`load()` 本身不抛错，必须监听 message。
6. **未登录**：微信重启后停在「进入微信」页时，执行器 `+0x10` 为 0，co_launch 不会启动，agent 会返回 `not_logged_in`。
7. **重启微信**：`pkill -x wechat` 会被忽略（缩到托盘），要用 `pkill -9 -x wechat`，再以 wechat 用户 `DISPLAY=:0 setsid nohup wechat &` 拉起，点「进入微信」（容器里可以用 `xdotool`）。
8. **崩溃排查**：`/home/wechat/.xwechat/crashinfo/completed/` 下最新的 `.dmp` 用 `mdparse.py` 解析。pc 在 libfrida-agent 里，说明是我们的回调出了问题。
9. **测试别用会话截图判断结果**：bot 会切换会话。查消息库 status。

## 6. 风险提示

- 直发绕过了界面，但发出去的仍是本账号的正常消息，频率控制（`core/humanize.py`、`reply_policy` 的上限）照常生效，不要绕开它们批量发。
- 偏移只对一个 sha256 有效。版本闸门是唯一的保护，不要为了"先用着"去掉它：偏移错了，轻则微信闪退，重则像 3.6 节那样把乱码写进微信自己的数据库。
