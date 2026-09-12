# 微信发送 API

后台地址默认为 `http://127.0.0.1:5100`。接口支持向好友或群发送文字、图片。

## 发送文字或图片

`POST /api/send` 接收 JSON：

```json
{
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

## 错误

参数错误返回 HTTP 400，例如缺少 `to`、文字为空、图片路径不存在或 `type` 无效。后台、微信窗口或登录状态不可用时，接口会返回发送失败或结果待核对；这类结果应先查询状态，不要盲目重试。

接口使用当前已登录的机器人微信账号，并复用现有会话校验和发送队列。请只在受保护的本机或内网访问 5100 端口，不要把接口直接暴露到公网。
