# 复刻音色:多厂商、多 API Key 轮询

代码:`core/voice_providers.py`(各家接口)、`core/voice_keys.py`(key 存储与轮询)、`core/tts.py`(合成、换 key 补复刻)、
`core/voice_clone.py`(样本整理、创建音色)。后台:直发页「语音厂商与 API Key」卡片、「编辑音色 → 复刻音色」。

## 厂商(接口均按 2026-11 官方文档)

| 厂商 | 复刻 | 合成 | 音色绑定模型 | 换 key 的信号 | 注意 |
| --- | --- | --- | --- | --- | --- |
| 阿里云百炼 CosyVoice | `voice-enrollment` create_voice(data URI) | dashscope SDK | 是(v2 / v3-flash / v3-plus / v3.5-flash / v3.5-plus) | 401 InvalidApiKey;Arrearage / insufficient_quota / FreeTierOnly;429 Throttling | 创建免费;每账号 1000 个音色 |
| MiniMax(api.minimax.cn) | `/v1/files/upload` + `/v1/voice_clone` | `/v1/t2a_v2`(hex) | 否 | 1004 鉴权;1008 余额;1002/1039/1041/1045 限流;2038 未认证 | 要实名认证;7 天不用音色被删(合成时发现会自动重新复刻) |
| 火山引擎豆包 | `/api/v3/tts/voice_clone`,`get_voice` 轮询训练状态 | `/api/v3/tts/unidirectional`,`X-Api-Resource-Id: seed-icl-2.0/1.0` | 否 | 文档无专门余额码:按 HTTP 401/403/429 与信息关键词判断 | 后付费自定义音色首次合成收槽位费 → 默认不自动补复刻;也可填已购 `S_` 音色号 |
| ElevenLabs | `/v1/voices/add` | `/v1/text-to-speech/{id}` | 否 | 401 / 402 / 429;404=音色没了 | 容器要能走代理 |

## Key 轮询

- 每家可配多个 key,按顺序轮流用。限流冷却 60 秒;余额不足/欠费冷却 6 小时(后台可「恢复」);key 无效停用到手动恢复。
  参数错误、网络超时等不换 key,直接报错。
- **账号分组**:复刻出来的音色属于创建它的厂商账号。同账号的 key 填同一个分组,共用音色;
  轮到另一个分组的 key 时,用保存的录音样本(`work/voice_clone/samples/`)在那个账号下自动复刻一次,音色 ID 记在
  `tts.json` 音色的 `ids`(`分组` 或 `分组|模型`)。可按厂商关掉自动补复刻(火山引擎默认关)。
- 所有 key 都不能用时,退回内置的微软免费音色,语音功能不中断。
- key 存在 `/home/wechat/.wxbot/voice_keys.json`(数据卷,权限 600);接口只返回打码后的 key,管理接口只认后台登录。
  旧版的百炼 key(`DASHSCOPE_API_KEY` / `dashscope_api_key` 文件)第一次用时自动导入为分组 `default`。

## 对比不同厂商/模型

复刻表单选「沿用已有音色的录音」,换厂商或模型再复刻一份,用「试听」对比。绑定模型的厂商(百炼)在编辑音色里
改合成模型时,下次合成会自动用录音在新模型上复刻。
