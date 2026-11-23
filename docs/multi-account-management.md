# 多微信账号管理

## 控制台

宿主机控制台只听 `127.0.0.1:5090`（可用 `WXBOT_CONSOLE_PORT` 改端口，绑定地址不变）。登记来自 `docker inspect` 和 `docker port`，不猜测端口，也不把注册表写进任何容器的 `work/multi_accounts.json`。

每张卡片打开该容器自己的后台和已发布的 noVNC：

- 后台：`http://{页面主机名}:{web_port}/`
- noVNC：`http://{页面主机名}:{vnc_port}/vnc.html?autoconnect=true&resize=scale&view_only=false&path=websockify`

协议固定为 http。主机名取自请求 `Host`（`urllib.parse.urlsplit`）。空主机名和 `0.0.0.0` 不生成链接。IPv6 写成 `[addr]`。某个端口没有登记时，那一条链接不出现，不回退到 6080 或 6082。

停止和重启只在 `docker inspect` 的 `Config.Env` 里 `WXBOT_SELF_CONTAINER` 等于该容器真实名字时才出现。现网第一个容器的环境变量仍是 `wxbot`，真实名字是 `wxbot-ubuntu-manual`，这张卡片没有停止按钮。出现按钮时，请求体里的 `confirm_wxid` 必须等于登记的 `expected_wxid`，不一致返回 400，不会调用 `docker stop` 或 `docker restart`。

## 容器内

容器内的账号页只说明这是本容器，并链到同一主机名上的 `http://{主机名}:5090/`。没有添加账号、批量启停，也不再要求管理 Token。

`POST /api/accounts/<id>/stop`、`/restart` 和批量 stop/restart：比较的是列表里那一行的 `container`，和 `action()` 会停的是同一个对象。注册表文件为空且仍包含默认 legacy 行时，这一行是 `{id: primary, container: wxbot-ubuntu-manual}`，不会写回文件。对不上、行不存在，或 `WXBOT_SELF_CONTAINER` 未设置，都是 403，不会猜成 `wxbot`。`WXBOT_LOCAL=1` 不能放行。批量只要有一行不是本容器就整批 403。只读的账号列表仍可查看。容器不会调用 `multi_account.create()` 把控制台注册表抄进自己的 `work/`。

## API（容器内，仅本容器）

- `GET /api/accounts`：本容器可见的账号列表。
- `POST /api/accounts/<id>/stop|restart`：仅当该行 `container` 等于 `WXBOT_SELF_CONTAINER`。
- `POST /api/accounts/batch`：`{"action":"stop"|"restart"}` 只要有一行 `container` 不是本容器就 403。
