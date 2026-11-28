# 控制台页面开发说明

控制台(console/)是一个无构建步骤的单页应用。框架在 `static/app.js`,设计系统在 `static/app.css`。
每个菜单页是一个独立文件 `static/pages/<key>.js`(+ 可选 `static/pages/<key>.css`),`index.html` 已经全部引入。
页面没注册时,菜单会回退到旧版页面(经代理嵌入),所以可以逐个替换。

## 注册页面

```js
(() => {
  const {$, $$, esc, A, toast, modal, busy, setTop, avatar, acct, onLeave, pickChat, fmtTime, sleep} = WX;
  WXPAGES.bot = async (view, id) => {          // key 对应 app.js 里 MENU 的 key
    const s = acct(id);                        // 账号状态 {id,label,name,wxid,state,bot_running,send_mode,direct:{…}}
    setTop(`<div class="row">${avatar(s, 'sm')}<h1>机器人与规则</h1></div>`, '<button class="btn primary" id="x">…</button>');
    view.innerHTML = `<div class="page stack">…</div>`;
    const t = setInterval(…, 5000); onLeave(() => clearInterval(t));   // 离开页面时清理
  };
})();
```

一个文件可以注册多个 key(例如 voices.js 注册 `voices` 和 `voicereply`)。
`view` 默认有 24px 内边距并可滚动;需要铺满(聊天、iframe)时设 `view.className = 'content flush'`。

## 公共工具(window.WX)

| 名字 | 作用 |
| --- | --- |
| `A(id, '/api/xxx', {method, json})` | 调这个微信容器的接口(经控制台代理 `/a/<id>/api/xxx`)。返回 JSON;HTTP 出错或 `{ok:false}` 时抛 Error(message 是后端 error 文本) |
| `api(url, opts)` | 调控制台自己的接口 `/api/console/...` |
| `toast(msg, 'ok'/'err')` | 右下角提示 |
| `modal({title, body, extra, ok, cancel, danger})` | 确认框;`extra` 里的 `<input name=…>` / `<select name=…>` 会作为结果对象返回,取消返回 null |
| `busy(btn, async fn)` | 按钮转圈、禁用,出错自动 toast |
| `pickChat(id, {title, groups:false})` | 选联系人/群弹窗,返回 `{username, name, is_group}` 或 null |
| `setTop(titleHtml, actionsHtml)` | 顶栏标题和右侧按钮 |
| `avatar(s, 'sm'/'lg')` | 账号头像;联系人头像用 `<img class="avatar sm" src="/a/${id}/api/avatar?username=…">` |
| `esc(s)` | HTML 转义(所有插进 innerHTML 的后端数据都要转义) |
| `fmtTime(unix秒)` `sleep(ms)` `onLeave(fn)` `$` `$$` `lightbox(src)` | |

## 样式(app.css 已有,优先复用,不要另起一套)

- 布局:`.page`(居中 max 1180)、`.page-head`、`.stack`、`.row`、`.grid.cols-2/3/4`、`.grow`
- 卡片:`.card` > `.card-head`(h2 + 右侧操作) + `.card-body`
- 按钮:`.btn` `.btn.primary` `.btn.danger` `.btn.ghost` `.btn.sm` `.btn.lg`
- 表单:`.field` > `label` + `.input`(input/select/textarea 都用 `.input`)
- 开关行:`.setting` > `.grow`(`.t` 标题 + `.d` 说明) + `label.switch > input[type=checkbox] + span`
- 分段选择:`.seg > button(.on)`
- 徽章 `.badge.green/.amber/.red/.blue`、状态点 `.dot.green…`、键值 `dl.kv > dt/dd`、空态 `.empty`、提示块 `.callout.blue/.amber/.red/.green`
- 列表行可以复用 `.chat-item`(头像 + `.n` 名字 + `.s` 副标题)
- 页面专属样式写进自己的 `pages/<key>.css`,类名加页面前缀(如 `.bot-log`),不要改 app.css

风格:简洁、留白、一个页面一个主题;说明文字短而具体(告诉用户这个开关做什么、什么时候用);
危险操作要 `modal` 确认;保存成功 `toast('已保存')`;不要堆按钮,把高级/少用的选项折叠起来(`<details>`)。

## 旧页面 → 接口

旧页面在各微信容器里:`static/index.html`(旧首页,含 AI/机器人/定时/推送/蒸馏/人设等弹窗)、`static/personalization.html`、
`static/battle.html`、`static/direct-send.html`、`static/send-curl.html`、`static/moments.html`。
接口实现都在仓库根目录 `server.py`(`grep -n '@app.' server.py`)。新页面必须覆盖旧页面的全部功能,不能少。

## 本机验证

- 控制台在 `http://127.0.0.1:5090`(容器 `wxbot-console`),静态文件直接从磁盘读:
  `docker cp console/static/. wxbot-console:/app/console/static/` 之后刷新即可,不用重启。
- 账号:`w2` = 主号(wxbot-ubuntu-manual),`w1` = 第二个号(wxbot-second),都在线。
- 已登录的 cookie 在 `/tmp/c.cookies`。截图(无头 Chrome):
  ```bash
  C=$(grep wxconsole /tmp/c.cookies | awk '{print $6"="$7}')
  cd /tmp/re && node shot.mjs "$C" /tmp/shots/<前缀> "http://127.0.0.1:5090/#/a/w2/<key>@@4000" \
     "http://127.0.0.1:5090/#/a/w2/<key>@@3000@@document.querySelector('#xxx').click()@@2500"
  ```
  每个 URL 截一张 1440×900 的图 `<前缀>-N.png`;`@@等待毫秒@@要执行的JS@@再等毫秒`。控制台 JS 报错会打印出来。
  截完用 Read 工具看图,确认版面、数据都对。
- 直接调接口:`curl -s -b /tmp/c.cookies http://127.0.0.1:5090/a/w2/api/xxx`
- JS 语法:`node --check console/static/pages/<key>.js`

## 安全规则(这两个是真实在用的微信)

- **只读验证为主**。不要发消息给任何人(唯一例外:主号的「文件传输助手」filehelper);不要启停机器人、不要改发送方式。
- 测试「保存」时:先读出原值,保存同样的值(或改了立刻改回),确认配置没被改变。不要删除人设/音色/定时任务/知识库条目;
  测试「新增」要立刻删掉自己新增的那一条。
- 不要改 `server.py`、`core/`、`console/server.py`、`static/app.js`、`static/app.css`、`static/index.html`。
  确实缺后端接口时,先用现有接口实现;实在不行在最终报告里写清楚需要什么接口、为什么。
- 不要 git commit。
