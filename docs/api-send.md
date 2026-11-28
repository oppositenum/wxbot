# 微信发送 API

后台地址默认为 `http://127.0.0.1:5100`。接口支持向好友或群发送文字、图片。

本机调用带请求头 `X-Wxbot-Token: $WXBOT_API_TOKEN`，或在 JSON 里加 `"token": "$WXBOT_API_TOKEN"`，不必先登录网页。这个凭据只放行 `/api/send`、`/api/send/status`、`/api/upload`、`/api/bot/push`、`/api/bot/push/test`、`/api/direct/revoke` 和 `/api/direct/send_tts`。其余后台接口仍要网页登录。未设置 `WXBOT_API_TOKEN` 时，请求头和 body 里的 token 都无效。

## 发送文字或图片

`POST /api/send` 接收 JSON：

```json
{
  "token": "可选，与 X-Wxbot-Token 相同",
  "to": "好友备注或群名称",
  "chat": "可选的稳定微信账号或群 ID",
  "type": "text",
  "content": "你好"
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `to` | 是 | 微信显示名称，用于打开目标会话。建议同时传 `chat` 消除同名歧义。 |
| `chat` | 否 | 稳定的好友微信 ID 或群 ID（群通常以 `@chatroom` 结尾）。 |
| `type` | 是 | `text` 或 `image`，默认 `text`。 |
| `content` | 文字必填 | 要发送的文字。 |
| `path` | 图片必填 | 后台所在机器可读取的图片绝对路径。 |
| `sync` | 否 | `true` 等待发送结果；默认 `false`，立即进入发送队列。 |

异步发送示例：

```bash
curl -X POST http://127.0.0.1:5100/api/send \
  -H 'Content-Type: application/json' \
  -d '{"to":"小明","chat":"wxid_xxx","type":"text","content":"晚上好"}'
```

成功返回：

```json
{"ok":true,"queued":true,"job":"<job-id>","ahead":0}
```

图片需要先上传：

```bash
curl -X POST http://127.0.0.1:5100/api/upload \
  -F 'file=@/tmp/photo.png'
```

把返回的 `path` 用于发送：

```bash
curl -X POST http://127.0.0.1:5100/api/send \
  -H 'Content-Type: application/json' \
  -d '{"to":"项目群","chat":"123@chatroom","type":"image","path":"/app/work/uploads/photo.png"}'
```

## 查询发送结果

`GET /api/send/status?job=<job-id>`

发送结果会区分 `confirmed`、`submitted`、`failed`、`uncertain` 和 `unknown`。`uncertain` 表示界面已经开始操作但后台无法确认微信最终结果，调用方不得自动重发同一消息。

## 同步模式

需要本次请求直接等待结果时传 `sync: true`。接口会返回发送器的原始结果；成功返回 HTTP 200，失败返回 HTTP 500：

```json
{"to":"小明","chat":"wxid_xxx","type":"text","content":"现在发送","sync":true}
```

## 撤回

`POST /api/direct/revoke` 撤回机器人账号自己发出的消息，直接调用微信底层撤回，不经过界面：

```json
{"token":"…","chat":"wxid_xxx 或 xxx@chatroom","local_id":528}
```

- `local_id` 可省略，省略时撤回该会话里我最近发的一条。
- 只能撤自己发的、2 分钟内的消息；文件传输助手不支持撤回。
- 成功返回 HTTP 200 和 `{"ok":true,"kind":"revoke","local_id":…,"message":"已撤回"}`。
- 失败返回 HTTP 409，`code` 可能是 `too_old`（超过 2 分钟）、`not_mine`（不是自己发的）、`already_revoked`、`not_found`（库里还没有这条，稍后再试）、`revoke_failed`（微信拒绝）、`unsupported`（微信版本未适配）。

## 文字转语音

`POST /api/direct/send_tts` 把文字用配置好的音色读出来，转成带声波画面的视频直发（Linux 版微信发不了语音条）：

```json
{"token":"…","chat":"wxid_xxx","text":"晚上好，早点休息。","voice":"calm_deep"}
```

- `voice` 是音色 key，省略时用默认音色。文字最多 500 字。
- 语音来自免费的 Microsoft Edge 在线语音（edge-tts），容器要能上网。
- 内置音色：`warm_low` 温柔低语(男，默认)、`calm_deep` 沉稳克制(男)、`magnetic` 磁性低音(男)、`sunny` 阳光少年(男)、`boyish` 少年感(男)、`cantonese` 粤语(男)、`female_warm` 温暖女声、`female_lively` 活泼女声、`female_tw` 台湾腔女声。
- 音色在直发页「语音（文字转语音）」里试听和编辑：可改语速、音调、换微软音色、新增自定义音色、设默认。配置存在 `work/tts.json`。后台接口 `GET/POST /api/tts/config`、`POST /api/tts/preview`（返回 mp3）需要网页登录。

## 错误

参数错误返回 HTTP 400，例如缺少 `to`、文字为空、图片路径不存在或 `type` 无效。后台、微信窗口或登录状态不可用时，接口会返回发送失败或结果待核对；这类结果应先查询状态，不要盲目重试。

接口使用当前已登录的机器人微信账号，并复用现有会话校验和发送队列。请只在受保护的本机或内网访问 5100 端口，不要把接口直接暴露到公网。
