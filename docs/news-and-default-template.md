# 新闻显示与默认人设偏好模板

## 新闻显示

后台首页 → 其他 → 腾讯新闻。新闻按文章标题和可点击链接展示。
腾讯新闻实际存于 biz_message，local_type=1，正文为 mmreader；支持 category 下的 item 和 newitem，兼容普通 appmsg 卡片。消息 API 同步刷新普通及公众号数据库。链接仅允许 HTTP/HTTPS，页面转义属性且使用 noopener/noreferrer。

## 默认模板

后台首页 → 画像与人设 → 加载管理数据 → 默认人设与偏好模板。

- 选择模板来源好友，点击“用所选好友设置保存模板”。模板复制解析后的人设引用、画像开关和交流偏好（包括称呼、反感表达和锁定值）。可先编辑来源好友的偏好，再保存模板。
- 开启“未配置好友自动继承”后，没有独立设置的好友在读取配置时继承模板，无需逐个保存。
- 已有独立配置保持不变。选择好友后，“一键套用默认模板”可替换其人设与偏好；操作前有确认，原设置保留审计记录。
- 默认模板不复制聊天、事实画像、证据 ID 或历史分析结果，不设置监听列表或主动发言开关。群、系统账号及当前机器人账号不继承。
- 单独保存继承配置后，该好友转为独立配置，不再随模板变化。关闭自动继承不删除已套用的独立配置。
- 当前账号首次模板来源为 🐮🐮🌱besos，保存于 communication.sqlite3 的 __default_template__ 记录。账号隔离、配置版本冲突检查及管理员访问要求继续有效。

## 管理 API

所有接口沿用 /api/personalization 的管理员授权与账号 session 校验。
GET /api/personalization 返回 default_template。
POST /api/personalization/template 接收 contact、revision（模板版本）、enabled、session。
POST /api/personalization/template/apply 接收 contact、revision（联系人版本）、template_revision、session。
参数不合法返回 400；版本或账号变化返回 409；未授权返回 403。

## 验证

离线覆盖新闻根节点与多篇文章、模板继承与个人覆盖、跨账号隔离、群与系统排除、版本冲突、模板关闭及页面保存/套用流程。真实新闻数据已用于解析检查，首页“其他 → 腾讯新闻”已作浏览器展示核对。模板已在当前账号初始化，未发送测试微信或调用真实模型。
