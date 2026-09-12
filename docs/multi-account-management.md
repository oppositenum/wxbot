# 多微信账号管理规划与第一版实现

## 运行模型

每个微信号对应一个独立的 `wxbot-<id>` 容器、独立 `/home/wechat` 数据卷、独立账号配置目录、独立后台端口和独立 VNC 端口。这样登录态、微信进程、密钥、解密库、发送队列和机器人状态不会在账号之间混用。

账号管理页是运营入口：`http://127.0.0.1:5100/accounts`。它显示账号名称、容器状态、后台/VNC 入口，并支持添加账号、启动、停止、重启和批量启动/停止。

## 状态边界

“运行中”只表示对应容器存在并处于 running；“微信在线”必须由该账号后台实际报告登录成功，不能从 Docker 进程存在推断。当前 registry 页面先展示这一差异，后续可在每个账号后台提供带签名的状态探针后显示在线绿点。

## API

- `GET /api/accounts`：返回账号注册表与容器状态。
- `POST /api/accounts`：`{"label":"客服号","id":"optional-slug"}` 创建隔离环境登记。
- `POST /api/accounts/<id>/start|stop|restart`：操作单个容器。
- `POST /api/accounts/batch`：`{"action":"start"|"stop"|"restart"}` 批量操作。

这些接口需要现有 `WXBOT_ADMIN_READ_TOKEN` 或本机管理员访问头。Docker 不可用时只返回明确的 `docker_unavailable`，不会假装启动成功。

## 当前已实现范围与下一步

本轮已实现账号注册表、端口/卷分配、管理 API、权限保护、账号卡片和批量操作界面。由于当前后台运行在没有 Docker socket 的容器里，页面可以登记和查看计划，但不能从这个容器直接创建宿主 Docker 容器；生产部署时应把管理 API 放到宿主管理进程，或只给专用 manager 容器挂载受限 Docker socket。创建实例时使用 registry 生成的独立卷、端口和 `/app/accounts/<id>` 挂载，再逐个 VNC 扫码登录。

建议第二阶段加入：宿主 manager 的容器创建器、每账号状态探针、批量配置模板、按账号审计日志、端口冲突检查、启动失败重试和滚动升级。账号登录、发送和密钥刷新仍必须绑定各自账号 epoch，禁止跨账号复用。

