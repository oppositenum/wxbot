# 朋友圈管理

入口：后台「朋友圈」，或 `http://localhost:5100/moments`。使用与多账号管理相同的管理员口令。口令仅留在页面内存，刷新页面需重新授权。

## 使用方式

- **刷新动态**：右上角「刷新微信动态」打开当前 Linux 微信朋友圈、刷新并读取缓存。不会保证获取所有历史或所有好友动态。
- **评论 / 回复**：「动态 → 查看与评论」选择具体回复对象，填写文字，或点「AI 自动回复」生成文案。点击「发送评论」才提交；保存草稿不会发送。在自己的朋友圈下，AI 以发布者身份回答朋友的评论。
- **发布**：「草稿」页填写文案，或点「AI 写一条」。可以上传最多九张 JPG、PNG、WebP 图片（每张 15 MB、2500 万像素以内）。点击「发布到微信」进入任务队列。图片转换为 JPEG 并移除元数据。已保存草稿也可点击「发送到微信」。
- **查看结果**：「执行记录」每五秒更新，有排队、准备中、核对回执、已确认、失败、跳过、待人工核对和取消状态。只有查到微信新增记录才显示「已确认」。等待中的任务可取消。
- **自动运行**：「设置」开启「自动评论与回复」或「每天主动发朋友圈」，选好时段与话题后保存。开关默认关闭，不会因为升级自行开启。

## 自动互动规则

| 设置 | 默认 / 含义 |
|---|---|
| 监听间隔 | 10 分钟；允许 5–1440 分钟。自动评论或发布开启后会自动刷新，无需另外打开监听开关 |
| 免打扰 | 北京时间 22:00–次日 08:00，可配置；拦截自动评论和发布，手动发送不受此限制 |
| 每日评论上限 | 3 条，允许 1–20 |
| 每日发布上限 | 1 条，允许 1–5；当前每天只有一个自动发布时刻，提高上限不会增加自动时刻 |
| 最小互动间隔 | 120 分钟，允许 30–10080；评论和发布共用间隔 |
| 每日发布时间 | 12:30，北京时间；开启时会校验它不能落在免打扰时段 |
| 话题 | 喜悦、愤怒、轻微烦躁、平静、趣事、随想，可多选；按天轮换 |
| 好友范围 | 每行一个当前账号好友标识；留空表示所有好友 |

自动评论只考虑开关启用之后的新事件，且事件不超过 24 小时。好友的新动态和自己动态下好友的新评论分开定位。已经由本账号回复的事件不会再次回复。AI 判断不适合互动、没有自然可说的话或需要看图才能理解时，会跳过，避免硬聊。

每日分享只在设定时刻之后的半小时内执行。开启前已过去的时刻不补发，服务停机错过的日期也不追补。失败、跳过和不确定任务都保留记录，不自动重新发送；需要时可以人工核对后创建新草稿。调整设置会取消尚未进入提交阶段的旧规则任务。

手动发送、自动发送以及结果不确定但已进入提交阶段的操作，都会计入自动模式的当天数量和最近互动时间，避免手动发过后自动模式继续密集发送。只统计本系统提交的操作；在原生微信中手动发布的数量不一定能完整统计。

自动文案使用已配置的主聊天模型；评论使用对应好友的人设和偏好，发布使用账号默认人设。只给模型提供动态、指定评论或近期自己发布的文字，不提供私聊记录。公开表达不能泄露私人画像和关系设定。没有真实活动资料时，提示词要求用随想、文字游戏或明确的想象表达，不虚构旅行、用餐、见人等真实经历；这属于模型约束，仍建议定期检查执行记录中的实际文案。

## 客户端与能力边界

原生适配器针对 Ubuntu ARM64 Linux WeChat 4.1.1.8 当前布局：

- 同时核对当前运行进程、打开的账号数据库、账号代次、唯一正文和作者名称；回复还要核对指定评论正文。昵称有备注时接受当前通讯录中的对应名称。
- 本地文字识别与两个按钮的外观模板配合；提交前回读文案，图片选择回读路径并核对缩略图。微信的公开可见设置需能够核对，否则不发布。
- 共享跨线程、跨进程 UI 锁，聊天优先；模型生成不占用 UI 锁。找不到目标、正文重复、仅有图片无正文、评论不完整显示、昵称识别不可靠或客户端弹出其他对话框时停止，记录失败，不猜测发送。
- 提交前持久化任务，提交后读取 SNS 缓存寻找新的本账号记录，并核对内容、目标评论引用或图片数量。超时、进程中断、账号切换等导致无法确认时记为不确定，不盲目重发。
- 该模块不支持视频发布、点赞/删除操作、好友动态原图预览和图片画面理解，也不是微信官方全量抓取接口。未加载、不可见或无权访问的动态无法获取；缓存中消失不等于已删除。

## API

所有 `/api/moments*` 接口需要 `X-Wxbot-Admin-Token` 或项目已有的严格本地管理员授权。写接口必须携带 GET 返回的完整 `session`；账号或后台重启导致代次变化时返回 409，请重新加载。动态、评论、资产 ID 均使用字符串，不转换成 JavaScript Number。响应均为 `Cache-Control: no-store`。

| 方法 | 路径 | 参数与结果 |
|---|---|---|
| GET | `/api/moments` | `limit` 1–100、`offset`、`author`、`search`；返回动态、settings、capabilities、status、session |
| POST | `/api/moments/sync` | `{session}`；真实刷新微信界面并同步缓存，聊天忙时返回冲突 |
| GET | `/api/moments/feed/{id}` | 动态、评论、点赞及 digest |
| POST | `/api/moments/ai-reply` | `{session,feed_id,feed_digest,reply_id}`；只生成正文，不发送 |
| POST | `/api/moments/ai-post` | `{session}`；按当前话题设置生成朋友圈正文，不发送 |
| GET | `/api/moments/drafts` | 最近 200 条草稿，包括已处理项 |
| POST | `/api/moments/drafts` | `{session,kind,text,assets,feed_id?,feed_digest?,reply_id?,id?,revision?}` |
| POST | `/api/moments/drafts/{id}/discard` | `{session,revision}`；仅未处理草稿可丢弃 |
| POST | `/api/moments/drafts/{id}/submit` | `{session,revision}`；返回 `{job}`，只是排队，不代表发送成功。同一草稿重复请求返回原任务 |
| GET | `/api/moments/jobs` | `{items}`，最近 100 条执行记录 |
| POST | `/api/moments/jobs/{id}/cancel` | `{session}`；只能取消 queued 任务 |
| POST | `/api/moments/settings` | `{session,revision,patch}`；设置版本冲突返回 409 |
| POST | `/api/moments/upload` | multipart `file` 与 JSON 字符串 `session`；返回 asset |
| GET | `/api/moments/assets/{id}` | 必须携带 JSON 编码的 session 查询参数；返回当前账号 JPEG 图片 |

获取当前会话并保存文字草稿（需要 curl 和 jq）：

```bash
export WXBOT_ADMIN_TOKEN='填写管理员口令'
MOMENTS_BASE='http://localhost:5100'
MOMENTS_SESSION=$(curl --fail --silent --show-error \
  -H "X-Wxbot-Admin-Token: $WXBOT_ADMIN_TOKEN" \
  "$MOMENTS_BASE/api/moments" | jq -c '.session')

MOMENTS_DRAFT=$(jq -n --argjson session "$MOMENTS_SESSION" \
  '{session:$session,kind:"publish",text:"给小小的快乐留一点位置。",assets:[]}' |
  curl --fail --silent --show-error \
    -H "X-Wxbot-Admin-Token: $WXBOT_ADMIN_TOKEN" \
    -H 'Content-Type: application/json' --data-binary @- \
    "$MOMENTS_BASE/api/moments/drafts")
```

下面的请求会**实际发布到微信**，并返回异步任务：

```bash
MOMENTS_DRAFT_ID=$(printf '%s' "$MOMENTS_DRAFT" | jq -r '.draft.id')
MOMENTS_REVISION=$(printf '%s' "$MOMENTS_DRAFT" | jq '.draft.revision')
jq -n --argjson session "$MOMENTS_SESSION" --argjson revision "$MOMENTS_REVISION" \
  '{session:$session,revision:$revision}' |
  curl --fail --silent --show-error \
    -H "X-Wxbot-Admin-Token: $WXBOT_ADMIN_TOKEN" \
    -H 'Content-Type: application/json' --data-binary @- \
    "$MOMENTS_BASE/api/moments/drafts/$MOMENTS_DRAFT_ID/submit"

curl --fail --silent --show-error \
  -H "X-Wxbot-Admin-Token: $WXBOT_ADMIN_TOKEN" \
  "$MOMENTS_BASE/api/moments/jobs"
```

任务 `state=confirmed` 且有 `receipt` 才是已查到微信回执。`uncertain` 请人工查看微信，不要另建相同文案绕过防重复机制。十分钟内相同目标和内容的活动/已确认/不确定任务也会阻止重复提交。

图片上传：

```bash
curl --fail --silent --show-error \
  -H "X-Wxbot-Admin-Token: $WXBOT_ADMIN_TOKEN" \
  -F "session=$MOMENTS_SESSION" \
  -F 'file=@/absolute/path/photo.jpg' "$MOMENTS_BASE/api/moments/upload"
```

将返回的 `asset.id` 放进发布草稿的 `assets`。不接受任意路径或其他账号资产。评论草稿用 `kind=comment`，必须附 `feed_id` 和最新 `feed_digest`；`reply_id` 为空表示评论动态，非空表示回复该条评论。

## 数据与部署

`accounts/<account>/moments.sqlite3` 保存缓存、设置、草稿和任务；`moments_assets/` 保存图片。沿用账号目录持久化挂载。只读解密微信库到临时快照，绝不向微信原库写数据或返回密钥和私有媒体 URL。

依赖：`xdotool wmctrl scrot xclip xprop xwininfo tesseract-ocr tesseract-ocr-chi-sim`，已写入 `docker/ubuntu-manual/Dockerfile`。`core/moments_templates/` 的 PNG 只包含两个按钮，无账号内容。

源代码不实时挂载到当前容器。发布需更新 `core/moments*.py`、`core/moments_templates/`、`core/ui_lock.py`、共享锁变更后的 `core/docker_wx.py` 和 `static/moments.html`，再仅重启后端；不要重启整个微信容器。原有 `server.py` 已注册页面/API 并启动 `moments.start_loop()`。

后台重启或账号切换会使旧 queued/preparing 任务取消；旧 initiated 任务转为 uncertain。确认记录永久保留在对应账号数据库，UI 最近记录列表只展示 100 条。


## 聊天感悟发朋友圈

「设置 → 主动互动 → 聊天有所感悟时发朋友圈」是独立开关，默认关闭；不依赖每日定时发布开关。仅开启后的机器人私聊 AI 回复在微信回执确认成功时触发，不回扫历史聊天，不监听人工在微信里发送的回复，也不从群聊生成感悟。

取当前好友最近最多十条文字消息和本轮已确认回复，后台判断是否值得公开表达开心、感叹、愤怒或趣味。使用这次聊天对应的人设；平常寒暄、没有明显触动、难以脱离隐私表达的内容会跳过。文案先检查昵称、号码、链接和原话复用，再调用模型进行公开隐私与事实边界检查，通过后进入现有发布与回执流程。模型检查不能保证绝对正确，执行记录保留实际文案供查看。

此流程不在聊天回复线程调用模型或操作微信界面。原始上下文只在内存短暂保存，最多半小时，完成、取消或过期后清除，不写入朋友圈任务库。后台重启时不补发旧感悟。执行记录用「聊天感悟」标识来源，只保存通过检查的公开文案与通用结果。

全局最多每五分钟启动一次感悟判断，同一好友至少间隔十五分钟；遵守好友范围、北京时间免打扰、最小互动间隔和**每日发布总上限**，提交前再次检查开关及设置版本。每日定时分享和聊天感悟共享发布总上限：默认一天一条，若希望两类都能发，可将每日发布上限调到 2 或 3。触发不代表必发，模型可以选择跳过。

API 设置字段：`chat_reflection`（布尔值）；例如向 `/api/moments/settings` 提交 `{session, revision, patch:{chat_reflection:true}}`。任务仍走 `/api/moments/jobs`，`origin=reflection`、`kind=publish`。部署需额外包含 `core/moments_reflection.py` 及 `core/bot.py` 的确认回复触发点。
