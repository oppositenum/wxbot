# 画像与专属人设：独立集成验收

2026-09-10。**本地实现、未部署；真实画像分析未执行。** 本报告是隔离集成验收，不是生产上线批准，不是微信发送能力验收。

## 独立预览

入口：http://127.0.0.1:5188/personalization 。合成管理员 Token：`acceptance-only`。当前保留预览供体验。

在项目根目录启动（同一端口已有预览时不要重复启动）：

```sh
python3 -B tools/acceptance_preview.py --data-dir /tmp/wxbot-acceptance-20260910 --port 5188
```

前台启动用 Ctrl-C 停止。已运行实例的 PID 保存在 `/tmp/wxbot-acceptance-20260910/preview.pid`；停止前先核对进程：

```sh
preview_pid=$(cat /tmp/wxbot-acceptance-20260910/preview.pid)
ps -p "$preview_pid" -o pid=,args=
```

仅在确认是带上述 data-dir 和 port 的 `tools/acceptance_preview.py` 后执行 `kill "$preview_pid"`。不得使用 pkill Python 或全项目停服命令。全新合成数据可改用一个不存在的 `/tmp/wxbot-acceptance-...` 目录及其他空闲端口；不需清理现有数据。临时目录可能被操作系统清理。

复用产品 `static/personalization.html`、原 Blueprint、原管理员鉴权、真实 llm.chat HTTP 请求组装函数。没有复制产品逻辑。仅传输 `_post` 替换为确定性 MOCK，不读取生产模型配置或密钥。自启动前即安装审计钩子：禁止出站 socket、子进程、隔离目录外写入及 SQLite 访问、生产账号/work/微信数据和配置凭据读取；同时封锁发送、Webhook、工具、画图及后台循环函数。最小 Flask app 未注册生产发送/启动/调度/Push 路由；没有启动生产 server.main、poller 或 schedule。

数据：合成账号 acceptance_account；A/B 同昵称，但 ID 分别为 demo_contact_A/B；C 为 demo_contact_C。A/B 各 12 条合成文本，日期 2026-09-01 至 09-04，跨四个日期片段；C 一条“你好”。未装载任何真实联系人绑定、定时任务或历史。

隔离为验收用途，不是面向恶意 Python 代码的通用沙箱。Flask 开发服务器只监听本机。合成 Token 是公开测试值，绝不能作为生产 Token。

## 实际浏览器验收

Playwright CLI 包不可用，改用本环境已有的真实 Playwright 浏览器能力；未安装新依赖。实际点击、选择、输入与处理确认弹窗；并发修改、会话状态和中断状态由仅供隔离环境的合成 scenario 接口注入，再检查真实页面。

| 项目 | 结果与边界 |
|---|---|
| 管理员登录 | 错误 Token 返回 403，页面显示独立管理员授权提示；正确 Token 可加载 |
| 全局/迁移 | 查看旧规则 sun 清单，首次保存仍为 sun；继承者随全局切换；显式 moon 不受影响；最终全局恢复 sun |
| 联系人角色 | A 指定 moon 后来源 contact；清除恢复 global；B 保持 moon；稳定 ID 与同昵称同时展示 |
| 偏好/锁定/开关 | 保存 A 的 tone=直接并锁定，关闭自动更新成功；并发修改后旧 revision 保存被拒绝 |
| 样本/分析 | 日期范围内 12 条、4 个有样本片段、一次预计调用；可展开必要证据；批准后只执行 MOCK |
| 审核/撤销 | 拒绝→显式重评→接受→撤销走通；未审批草稿不影响配置；接受明确反馈保持 user_explicit |
| 结束状态 | 告别后显示暂停；新真实入站合成事件恢复临时会话；长期禁令在新入站和隔离进程重启后仍保留 |
| 空/不足 | 零样本预计调用 0、无批次；C 一条样本经一次 MOCK 返回零建议，偏好仍空 |
| 模型失败 | 批次 failed，预留一次调用不回退，需显式重试；顶部错误提示不准确，见下面缺口 |
| 中断 | 注入原代次 running/reserved；打开页面不重试；点击明确重新调用并确认后只增加一次 MOCK，预留次数 1→2 |
| 桌面布局 | 1440×1000、1366×768 可滚动操作，无横向溢出、按钮未越出横向边界；已查看实际 PNG |
| 控制台 | 故障场景有预期 HTTP 403/409/400；初始预览隔离器 SQLite 字节路径错误产生 503，已修正；最终新导航后无控制台 error。未观察到 JavaScript 运行异常 |

截图位于 `output/playwright/`：01 登录错误，02 样本，03 MOCK 审核，04 并发冲突，05 长期禁令，06 不足，07 模型失败，08 中断，09–11 三案例，12 完整页面，13 角色和旧规则。全部合成数据。

尚存页面问题：`profile_drafts.run_batch` 将模型失败包装为 ValueError，API 统一显示“参数或配置无效”，未准确提示模型失败。任务总状态仍可能为 analyzing，但具体批次为 failed/running；中断未单独翻译为中文状态。数据没有丢失、没有自动重调；可理解性未完全达标。本轮按“仅修复三个逻辑点”边界记录，没有改动这些路径。画像列表的长期存储保留策略也未改变。

## 三项逻辑核查与具体修改

1. 来源：原审核接受 reception 一律 source=inferred，明确长期要求会降为弱建议。现在在已验证证据快照内重新用现有明确规则识别：匹配的长期要求记 user_explicit 与原消息证据、证据时间；跨片段重复反馈但非明确长期要求仍 inferred；管理员修改建议值记 admin_manual。现有同值强来源保留，推断不覆盖已明确设置，历史要求不覆盖更新的明确设置，锁定项仍拒绝覆盖。仅模型草稿给出“明确”判断不够，必须通过现有保守规则。当前请求仍优先，长度冲突时不选入长期长度项。
2. 拒绝：保留联系人+类别/维度/值签名抑制。新增管理员 `reevaluate`，只重新打开指定已拒绝草稿，不调用模型、不自动应用、不解除后续同类自动提案抑制；记录 review_history。之后真实用户的新明确要求走原 learn_live/_ingest，不依赖拒绝签名，不会被旧拒绝吞掉。重新分析产生的同签名建议仍抑制，管理员可审核原条目或手动编辑。
3. 预算：任务持久保存 output_token_limit（1800），执行读取同一值，预算预留 input_estimate + output_token_limit；旧任务无该字段时按原有 1800 兼容。GPT Chat Completions 与 Claude Messages 的真实序列化请求均带 max_tokens=1800、single_attempt 导致 _post retries=0。模型名不修改。只证明本地请求参数和持久预算一致；未验证真实中转/所选模型是否接受和执行该参数，不能称为供应商硬计费上限。输入量为 UTF-8 字节近似，未配置价格不显示费用金额。

修改位置：`core/profile_drafts.py` preview/run_batch/review；`static/personalization.html` renderAnalysis；`tests/test_profile_drafts.py`。另新增验收启动器 `tools/acceptance_preview.py`、隔离自检 `tests/test_acceptance_preview.py` 和本文档。其他累计产品补丁均非本轮新增修复。

## 三组合成案例与模型上下文

| 案例 | 实际角色/来源 | 平常入选 | 本轮明确要求及覆盖 |
|---|---|---|---|
| A | 晨光 / global | 简短（user_explicit）、直接（admin_manual；浏览器验收另锁定） | “请详细逐步解释这道合成题”：仅直接进入偏好，不注入简短 |
| B | 明月 / contact | 详细、温和（admin_manual） | “请用简短结论回答这次的问题”：仅温和进入偏好，不注入详细 |
| C | 晨光 / global | 无，资料不足 | 按当前问题回答，无虚构偏好或补齐标签 |

真实产品 bot._ai_reply → llm.chat → MOCK _post 的五份请求（A/B 各普通与覆盖场景、C 一场景）保存在交付目录 `examples.json`，不是手写的假请求。只输入合成数据，事实选择器返回空，agent 关闭，未发送。结构：

```text
messages[0].role = system
  通用准确性、当前请求优先、媒体诚实性
  本轮机器人角色正文（A/C 晨光；B 明月）
  当前相关交流偏好（至多少量；明确来源不会标作弱推断）
  必要当前作用域事实（本案例为空）
messages[1].role = user
  当前时间、近期对话背景（本案例为空）
  当前合成请求与本批文字
```

五次返回均明确为：`【MOCK 合成输出，不是真实模型生成】按当前请求及所选角色、偏好组织回答。` 固定返回仅用于证明调用链，不证明自然度或风格效果。页面结构预览本身模型调用 0。

会话转换：active → 明确告别 → paused；paused + 新真实入站（包括表情/语音）→ active；长期禁止主动联系置 no_proactive 后，新入站只恢复临时会话、不撤销禁令。普通回复仍可执行。测试还断言在途主动生成遇到告别或新入站时生成一次、发送零次；独立 schedule 不读取结束状态，不取消提醒。依赖已解密库可见事件，真实微信同步延迟的最终发送边界仍未解决。

## 测试结果与重现

- `python3 -B tests/test_acceptance_preview.py`：31 个断言；测试输出一个仅含合成证据的临时目录。验证页面/启动零模型调用、无后台线程、发送/Webhook/工具/网络/外部写/非隔离 DB/生产凭据/子进程阻断、真实上下文组装与临时/长期状态。
- `python3 -B tests/test_profile_drafts.py`：45 项（含新增来源保留、管理员编辑、推断不能覆盖明确设置、显式重评、新反馈不被抑制、两适配器实际 body 与预算一致）。
- `python3 -B tests/test_contact_personalization.py`：38 项。
- 发送安全回归 38 项；此前路由/媒体/授权回归 33 项；记忆上下文 35、调度 28、媒体跟进 17 个断言。
- Node 模拟 DOM 7+9 项仅作为回归补充，真实浏览器证据单独保存。部分既有测试有 ResourceWarning（文件句柄关闭），不影响断言，但不是零警告。

均为隔离测试，模型与发送 mock。mock 不能证明真实画像准确性、自然回复、真实转写质量、微信身份识别或送达。

## 累计依赖与部署清单（待审核，非上线授权）

HEAD `ab5f6c5f2e749330761bf485eeeb40f18065260b`；当前脏工作区保留，不提交、不推送、不 reset。累计重叠文件不能按文件名认作单一补丁。

| 顺序 | 类别 | 主要范围/依赖 |
|---|---|---|
| 0 | 早期模型、媒体、权限及其他既有修改 | llm/agent/tools 模型路由；media_read/imgdec/media 媒体状态；read_access/memory/server 管理权限；另有此前 README、requirements 等。本轮开始基线及上一发送审查 baseline 保留，未全部重新验收 |
| 1 | 发送阻断与可靠性 | send-safety-20260909/this-round.patch；account_session/send_ledger/sender/sendq、bot/schedule、UI 锁、docker/wx_send。默认微信发送阻断，不能作可用发送部署 |
| 2 | 联系人画像、人设与原生转写 | contact-personalization-20260909/contact-personalization.patch；personalization/API/page、bot 统一角色、messages/voice_text/media_read；依赖账号代次与管理鉴权 |
| 3 | 模型草稿与结束状态 | profile-drafts-closure-20260910/profile-drafts-closure.patch；profile_drafts/conversation_state、通信 SQLite 增表、sender/send_ledger 最终主动状态核对；依赖 1/2 与 llm 单次请求机制 |
| 4 | 本轮独立集成验收 | this-round.patch：三个逻辑点、隔离启动/验证、文档；无发送行为变更，无真实配置迁移 |

三个旧独立补丁的 before/after manifest 和本轮基线用于核对连续性；不可对 HEAD 盲目顺序 apply，更不可直接部署整个工作区。交付的 acceptance-code.tar.gz 是明确文件白名单的复现源代码快照（含累计依赖，**仅供隔离验收**），没有普通生产启动脚本，不能称为已批准生产版本。含文件 SHA256 清单、独立当前 diff、累计分类清单；不含 .env、llm_config、账号目录、密钥文件、聊天数据、账本或数据库。合成截图/请求证据另存，不混入代码包。

部署前仍须：解决可用发送适配器/一致版本组合；明确配置管理员 Token 并使新页面与后端同版；单独评审模型实际路由改变与输出参数支持；核对旧规则人设迁移（冲突拒绝自动迁移）；改善模型失败与中断提示后复验。不要恢复盲目重试或跳过身份确认。

此前兼容事项单列：agent 从“有 Claude 凭据即 Claude”变为实际所选 provider/model；主 GPT 会改变工具请求路由。管理权限新增 Token，但既有交付记录显示生产尚未配置，本轮不读取生产密钥/环境验证也不修改，因此不能声称当前已就绪。

回滚：本轮相对开始时脏基线保留独立 patch，先比较 after SHA256 并做 reverse --check，再在经批准的发布目录处理；不能覆盖后来修改。未部署时只停止独立预览即可撤回体验。将来如果已有审核应用/长期禁令，备份通信库并保持这些限制，不能让旧代码忽略禁令；发送账本不得删掉重放。

## 真实画像最小试点（未执行）

1. 管理员明确选择一个稳定账号+私聊 ID；限定短日期范围、最多几个跨日期片段，排查不应外发的证据。不要默认已有联系人获授权。
2. 预览实际 provider/model、消息数、时间范围、预计输入与输出上限；调用上限设 1。先确认供应商接口支持对应 max_tokens，价格无配置就只展示次数/token 预算。
3. 单独批准一次分析；失败/结果未知不自动重调。审核每项证据和冲突，接受或修改必要设置，核对 revision；查看本地上下文预览。真实模型回复预览属另一次调用，必须另行明确批准；不发送微信。

## 发送适配器调查的独立结论

**未完成可用发送适配器的可行性验收，继续列为待办。** 本轮找到发送安全交付及后续画像文档，未找到独立完成的可行性调查报告。已有报告确认当前代码无可靠身份适配器、账号仍依赖数据目录观测；这只能证明现有实现与已有证据不足，不能据此宣布环境不支持。A 发前稳定收件人确认、B 发起后可信结果核对是两项未完成的真实能力验证。保留“不确定不自动重发”和持久发送记录，不通过本轮画像测试替代发送结论。

生产 PID 49418 于 2026-09-09 18:51:19 启动，本轮末仍相同。只重启了独立预览；生产未重启。时间证据支持未替换进程的推断，不证明每个已加载模块内容。没有付费模型调用、真实微信/Webhook 发送、真实历史批量读取、真实联系人配置写入、生产配置变更、任务启停或 Git 提交推送。本轮完成后停止扩展，等待体验反馈。
