# wxbot — 微信抓取 + 发送后台

在 **Docker 里跑一个 Linux 微信（小号）**，可远程扫码登录、可视操作；后台负责：
读取**私聊/群成员/群聊天**，发送**文本/图片**，Web 界面展示登录状态、列数据、发消息。

> 主账号继续在你自己的 Mac 上用；容器里登录一个**小号**当机器人，互不影响。

## 架构

```
┌─ Docker 容器 (arm64, Linux 微信 4.1.1) ──────────────┐
│  Xvfb + fluxbox + x11vnc + noVNC(6080)               │
│  /opt/wechat/wechat  ← 小号扫码登录                    │
│  linux_keys.py  从 /proc/pid/mem 取密钥(明文 x'hex')  │
│  wx_send.py     xdotool 驱动界面发文本/图片            │
│  数据卷 /root ──挂载──> docker/wxdata (宿主可读)       │
└───────────────────────────────────────────────────────┘
        ▲ docker exec (发送/取密钥)   │ 读 db 文件
        │                             ▼
┌─ 宿主 (macOS) ────────────────────────────────────────┐
│  core/decrypt.py  逐页 AES-256-CBC 解密 db             │
│  core/contacts|messages|avatars  读联系人/群/消息      │
│  core/docker_wx.py  与容器交互(状态/发送/取密钥)        │
│  server.py  Flask API + 前端  http://localhost:5100    │
└───────────────────────────────────────────────────────┘
```

关键点：**Linux 微信把库密钥以明文 `x'<hex>'` 存在进程内存**（不像 macOS 4.1.x 做了混淆），
容器内 root 直接读 `/proc/pid/mem` 秒取全部 per-db 密钥。数据库格式与 macOS 4.1 同代，
解密/表结构/protobuf 解析完全复用。发送用 `xdotool`（容器内 X，坐标稳定，比 macOS 屏幕自动化可靠）。

## 目录

```
docker/
  Dockerfile            arm64 Debian + VNC/noVNC + 微信 + 依赖 + 脚本
  start.sh              容器入口：起 X/VNC/微信
  linux_keys.py         取密钥(容器内)
  wx_send.py            xdotool 发送(容器内)
  WeChatLinux_arm64.deb  微信安装包
  wxdata/               容器数据卷(挂载, 内含 db + keys.json)
core/
  decrypt.py  db.py  contacts.py  messages.py  avatars.py  protobuf.py
  docker_wx.py          宿主↔容器 交互
config.py               自动探测 docker/wxdata 数据目录
server.py               后台 API + 前端
static/index.html       单页 UI
run.sh                  一键：起容器→取密钥→解密→起后台
```

## 使用

### 首次
```bash
# 1. 构建镜像（含微信 + 全部依赖，约 3GB）
cd docker && docker build --platform linux/arm64 -t wxbot-wechat . && cd ..

# 2. 一键启动
./run.sh
#    首次会提示打开 noVNC 扫码：浏览器开 http://localhost:6080/vnc.html → Connect
#    用【小号】手机微信扫码登录，登录后再次运行 ./run.sh
```

### 之后
```bash
./run.sh          # 起容器(若停)→取密钥→解密→ http://localhost:5100
```
微信重启后密钥会变，后台点「刷新密钥」或重跑 `./run.sh` 即可。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/status` | 容器/微信/登录/密钥/解密 状态 + noVNC 地址 |
| POST | `/api/login`  | 返回 noVNC 地址（浏览器扫码登录小号） |
| POST | `/api/keys/refresh` | 重新取密钥 + 解密（微信重启后用） |
| POST | `/api/sync`   | 增量解密刷新 |
| GET  | `/api/sessions` `/api/contacts` `/api/groups` | 会话/联系人/群 |
| GET  | `/api/groups/<chatroom>/members` | 群成员 |
| GET  | `/api/messages?chat=<username>&limit=` | 消息 |
| GET  | `/api/avatar?username=` | 头像 |
| POST | `/api/send` `{to, type:text|image, content|path}` | 发送 |
| POST | `/api/upload` (multipart) | 上传图片，返回宿主路径供 send 用 |

完整字段、异步任务状态、图片上传和 curl 示例见 [docs/api-send.md](docs/api-send.md)。

`to` 用**联系人备注/昵称或群名**（后台按名搜索并打开首个匹配的会话）。

| GET  | `/api/bot` | 机器人状态 + 规则 + 日志 |
| POST | `/api/bot/start` `/api/bot/stop` | 启停机器人 |

## 机器人（自动回复 / 转发）

规则写在 `bot_rules.json`（后台**热加载**，改完即生效）：

```json
{
  "poll_interval": 5,
  "include_self": false,
  "watch": ["wxid_...(联系人)", "xxxxx@chatroom(群)"],
  "rules": [
    {"name":"ping", "match":{"type":"keyword","value":"ping"},
     "action":{"type":"reply","text":"pong 🏓"}},
    {"name":"echo", "match":{"type":"regex","value":"^echo\\s*(.+)"},
     "action":{"type":"reply","text":"你说：{group1}"}},
    {"name":"fwd",  "match":{"type":"keyword","value":"上报"},
     "action":{"type":"forward","to":"某群名","prefix":"[转发] "}}
  ]
}
```
- `match.type`：`keyword`(含)｜`regex`(可用 `{group1}` 引用捕获)｜`any`(任意)
- `action.type`：`reply`(回当前会话)｜`forward`(转发到 `to`)；`text` 支持 `{content}{sender}{groupN}`
- `include_self=false`：只对**他人**消息触发（机器人不回自己）
- `watch`：监听哪些会话（联系人 wxid 或 群 `@chatroom`）
- 引擎轮询解密库（**已叠加 WAL，秒级**）检测新消息，命中即经容器 `xdotool` 发送
- 前端「机器人」按钮启停；命令行： `python3 -m core.bot`

机器人核心：`core/bot.py`（规则匹配 + 状态跟踪 `work/bot_state.json`，只处理启动后的新消息）。

## 蒸馏某人 + 用其风格自动回复（@我/引用我 触发）

让机器人在群里被 **@** 或 **被引用** 时，用某个人的说话风格自动回复。

### 1. 配置大模型（Claude 或 GPT）
编辑 `llm_config.json`（或后台「AI设置」）：
```json
{ "provider":"claude", "api_key":"sk-ant-...", "model":"claude-sonnet-5",
  "gpt_model":"gpt-4o", "proxy":"http://127.0.0.1:7890", "temperature":0.9 }
```
`provider` 填 `claude` 或 `gpt`；海外 API 需 `proxy`（走你主机的代理）。

### 2. 蒸馏目标人（需该人在群里发言够多）
```bash
# 列出群里发言候选人（数据源=当前登录容器的账号，需有历史）
python3 -m core.distill candidates <群@chatroom>
# 蒸馏其中某人（wxid），产出 personas/<slug>.json
python3 -m core.distill run <群@chatroom> <wxid> [名字]
```
或后台 API：`POST /api/distill/candidates {group}`、`POST /api/distill/run {group,wxid,name}`。
蒸馏得到一段「回复风格人设」（可直接当 LLM system prompt）+ 口吻样例，存 `personas/`。

### 3. 配置机器人用该人设回复
`bot_rules.json`：把目标群加入 `watch`，`style-reply` 规则的 `persona` 填蒸馏出的 slug：
```json
{ "watch":["<目标群@chatroom>"],
  "rules":[{ "name":"style-reply",
             "match":{"type":"mention"},          // @我 或 引用我 触发
             "action":{"type":"reply_ai","persona":"<slug>"} }] }
```
- `match.type`：`mention`(@我或引用我)｜`at_me`｜`quote_me`｜`keyword`｜`regex`｜`any`
- `action.type=reply_ai`：取「最近对话上下文 + 对方@你这句」发给 LLM，按人设生成回复并发出
- 启动机器人后，群里有人 @ 你/引用你 → 机器人用该人风格回一句

新增模块：`core/llm.py`(Claude/GPT 统一客户端)、`core/distill.py`(语料提取+人设生成)；
`core/messages.py` 已能识别每条消息的 `at_me`/`at_all`/`quote_me`。

| POST | `/api/llm/config` | 配置大模型 key |
| GET  | `/api/llm` `/api/personas` | LLM 状态 / 人设列表 |
| POST | `/api/distill/candidates` `/api/distill/run` | 列候选 / 蒸馏 |

## 命令行直用

```bash
# 取密钥(容器内) → 解密(宿主)
docker exec wxbot python3 /usr/local/bin/linux_keys.py \
    /root/xwechat_files/<wxid>/db_storage /root/keys.json
python3 -m core.decrypt

# 读
python3 -m core.contacts     # 联系人/群/成员
python3 -m core.messages     # 会话/消息

# 发送(容器内 xdotool)
docker exec wxbot python3 /usr/local/bin/wx_send.py text  "某联系人" "你好"
docker exec wxbot python3 /usr/local/bin/wx_send.py image "某群名"  /tmp/pic.png
```

## 已知局限

- **消息延迟**：微信用 WAL，宿主读的是已 checkpoint 的主库，最新几条可能有秒/分钟级延迟；后台每 10s 增量解密。
- **发送目标**：对真实联系人/群，搜索名字回车即打开首个匹配并发送；`文件传输助手` 等**系统功能**因搜索首项是「搜一搜」网页结果，需特殊处理（真实机器人目标一般用不到）。
- **单设备登录**：小号登录容器期间不能同时在别处登录该小号。
- **密钥时效**：微信重启后密钥变化，需重新 `linux_keys.py`（后台「刷新密钥」按钮）。
- **窗口坐标**：`wx_send.py` 用固定窗口几何偏移点击侧栏搜索/文件按钮；若手动改了容器窗口大小可能需微调偏移。

## 在 Linux 服务器部署（x86 / arm 一体化镜像，推荐）

服务器上用**单个容器**同时跑 微信 + 后端（无需在宿主装 Python，也不用挂 docker.sock）。
镜像已推到私有 registry：`<your-registry>/wxbot-wechat`（含 `amd64`/`arm64`）。

### 一键起

```bash
# 1) 装好 docker + docker compose 后，拿到 docker-compose.yml 和 .env.example
cp .env.example .env
vi .env            # 至少改 VNC_PASSWORD；要对外访问再把 BIND_HOST 改 0.0.0.0
docker compose pull
docker compose up -d
docker compose logs -f     # 看启动日志
```

### 首次登录

1. 浏览器开 `http://<服务器>:6080/vnc.html`（默认只绑 127.0.0.1，见下"安全"）→ 输入 `VNC_PASSWORD` → 用**小号**扫码登录容器里的微信。
2. 开 `http://<服务器>:5100` 后台 → 点「刷新密钥」（容器内自动 `/proc/pid/mem` 取密钥并解密）。
3. 之后就能看会话/消息、发送、配 AI/机器人了。数据存在 `wxdata`/`appdata` 两个卷里，重启不丢。

### 端口与安全（重要）

- 默认 `BIND_HOST=127.0.0.1`：`5100`(后台) 和 `6080`(noVNC) **只在服务器本机**，请用 **SSH 隧道**访问：
  `ssh -L 5100:127.0.0.1:5100 -L 6080:127.0.0.1:6080 user@服务器`，然后本地开 `http://localhost:5100`。
- 若要局域网/公网访问：`.env` 里 `BIND_HOST=0.0.0.0`，**务必设 `VNC_PASSWORD`**，并在前面加反向代理 + HTTP 认证 + HTTPS。后台 `5100` 本身无鉴权，别裸奔公网。
- 容器需要 `seccomp=unconfined`(微信 CEF) 和 `SYS_PTRACE`(读内存取密钥)，compose 里已配好。

### 自己构建 / 多架构

```bash
# 在项目根目录(Dockerfile 会按目标架构自动下载对应微信 deb)
# 单架构(在对应架构的机器上直接构建最快)：
docker build -f docker/Dockerfile -t <your-registry>/wxbot-wechat:latest .

# 多架构一次构建并推送(需 buildx)：
docker buildx build --platform linux/amd64,linux/arm64 \
  -f docker/Dockerfile -t <your-registry>/wxbot-wechat:latest --push .
```

> 海外服务器构建慢的话，可 `--build-arg APT_MIRROR=deb.debian.org` 换回官方 apt 源。

### 宿主/开发模式（macOS）
本仓库同时兼容"后端在宿主 + 微信在容器"的老模式（`run.sh` + `python3 server.py`），
靠环境变量 `WXBOT_LOCAL` 区分：**不设=宿主模式(docker exec)**，**=1=容器内模式(本地执行)**。
服务器一体化镜像里已设 `WXBOT_LOCAL=1`。

## macOS 本机方案（历史/备选）

`core/keys.py`（内存扫描）+ `core/sender.py`（Accessibility）+ `core/cc_catch.py`（lldb 断点 CommonCrypto 取密钥）
是早期直接操作 Mac 微信 4.1.13 的方案。因 macOS 4.1.x 密钥混淆，取密钥需 lldb 抓 `CCCryptorCreate`；
发送用屏幕自动化不够稳。**已被 Docker 方案取代**，代码保留备查。


## 本轮修复后的路由、媒体与读取授权（代码待部署）

- 普通 `chat` 使用主 `provider` 及该 provider 原有的模型字段。工具调用默认也使用该路由；存在另一套凭据不构成切换授权。
- 可显式设置 `tools_provider` 和/或 `tools_model`，AI 设置页展示并保存这两个字段。`tools_provider` 留空跟随主 provider；`tools_model` 留空使用所选 provider 的模型。工具轮和工具后的最终回复固定在同一条路由。不改写任何中转模型名称。
- 当前本地工具协议适配器支持 Claude Messages 和 GPT Chat Completions function calls。这只说明请求格式支持，不证明中转部署的具体模型有工具能力。未知适配器、缺凭据、接口拒绝或最终回复为空均报错，不静默跨 provider 回退。
- `/api/llm` 的 `tool_route` 展示解析后的请求路由，`route_diagnostics` 保留最近 100 条请求阶段记录（`request_id/phase/provider/model`）。这些记录不含 URL、凭据、工具参数或聊天正文；不是底层真实模型认证。
- 图片先取文件/解密，再由 Pillow 验证完整解码，之后才请求视觉接口；视觉响应必须是明确标注 success 且有非空 description 的 JSON；unreadable 或格式错误均不当成读图成功。状态区分缺密钥、文件不可用、解码失败、缺解码器、接口未配置、接口失败、结果无效、模型报告不可识别、功能未启用及成功。STT 保持原开关，不自动启用。
- 纯媒体批全部读取失败时，直接告知未能读取并请对方重发或转文字，不调用回复模型编造。混合文字批保留文字并明确媒体内容未知。视频只分析封面，不代表看过整个视频。
- 模型读取工具要求 `agent.run` 依据服务端会话签发的 `read_access.Access`。直接调用工具函数同样要求该授权；模型参数不能补齐账号、会话或成员。读取固定在签发账号的文件目录，返回前重新验证当前账号。
- `get_member_profile` 只返回当前授权成员、当前 scope、active 且未过期的事实；不返回聚合 summary、取消/取代事实或跨 scope 内容。历史工具读取当前会话的普通文本，排除本账号已记录或带前缀的定时消息；不把媒体 XML 当成已解析内容。
- 全量画像和知识库管理是独立的管理员入口：`/api/profiles*` 和 `/api/kb*` 要求环境变量 `WXBOT_ADMIN_READ_TOKEN` 对应的 `X-Wxbot-Admin-Token` 请求头。未配置/未提供则拒绝。AI 设置页可输入该凭据，仅保留在页面内存，刷新即失效。管理入口记录端点、方法、账号摘要和授权结果，不记录凭据或正文。此限制只覆盖这些管理入口，不代表其他后台 API 已完成鉴权。

本轮不修改实际配置、不设置管理员凭据、不重启、不发送真实消息、不调用真实模型。后端行为需部署后才生效。静态页面可能被现有进程直接读取，因此新增设置按 `/api/llm` 的后端能力标记显示，旧后端不展示新增控件或提交新增字段。

离线验证：`python3 -B tests/test_review_fixes.py`，以及原有三个 `tests/test_*` 脚本。新测试阻止网络、子进程和真实发送，使用临时账号文件和模拟接口；不能证明真实中转工具能力、识图/转写质量或微信 UI 发送可靠性。

新闻显示修复及默认模板使用方法见 [新闻与默认模板](docs/news-and-default-template.md)。

朋友圈监听、评论回复、文字/图片发布、自动互动和接口文档见 [朋友圈管理](docs/moments.md)。后台「朋友圈 → 设置」可开启自动评论和每日分享；默认关闭，按北京时间免打扰，并记录每次执行与微信回执。
