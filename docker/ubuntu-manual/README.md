# Ubuntu 微信手动对比环境

用于对比原 Debian 微信的图片发送问题。Ubuntu 24.04.4 ARM64、XFCE 桌面、微信 4.1.1.8；普通用户运行，独立 Docker 数据卷，无机器人、Frida 或自动发送脚本。这个环境仍通过 Docker 的虚拟显示运行，不是物理机上的完整 Ubuntu GNOME 会话。

浏览器入口：<http://localhost:6082/vnc.html?autoconnect=true&resize=scale&view_only=false>。VNC 端口为本机 5902。两个端口仅绑定 127.0.0.1。

## 构建与启动

在项目根目录执行。基础镜像 `wxbot-ubuntu-base:24.04.4` 由 Ubuntu 官方 ARM64 base rootfs 导入；本次下载及 SHA256 核查记录在 `output/ubuntu-manual-base-verification.json`。微信安装包使用已有的 `docker/WeChatLinux_arm64.deb`，与原环境版本相同。

```sh
docker build --pull=false \
  --build-arg APT_PROXY=http://host.docker.internal:7890 \
  --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= \
  --build-arg http_proxy= --build-arg https_proxy= \
  --build-arg ALL_PROXY= --build-arg all_proxy= \
  -t wxbot-ubuntu-manual:24.04 -f docker/ubuntu-manual/Dockerfile .
docker compose -f docker/ubuntu-manual/compose.yaml up -d
```

镜像构建上下文由专用 Dockerfile.dockerignore 限制为安装包和新环境脚本，不包含原微信数据、聊天记录、机器人配置或密钥。

## 手动测试

扫码登录后，在微信中手动选择要测试的会话，再通过文件选择器打开 `/home/wechat/Pictures/pelican-original.png`。该文件复制自先前失败发送的原始生成图。另有 `pelican-api-test.png`，来自接口独立测试。

登录数据保存在 `wxbot-ubuntu-manual-home`，与旧 `wxbot` 的 `/root` 绑定目录分开。示例图片以只读方式挂载。首次使用需要重新扫码，登录同一账号可能影响旧客户端的登录状态。

停止测试环境可执行 `docker compose -f docker/ubuntu-manual/compose.yaml stop`；不要附加删除数据卷的参数。原 Debian 容器和数据仍保留。

本次排查发现原微信进程的 HTTP/HTTPS 代理指向一台已不可达的主机，Docker Desktop 的透明 HTTP 转发也使用该离线代理。新容器单独使用可达的本机代理 `host.docker.internal:7890`，没有修改全局 Docker 设置；运行时需要本机 ClashX 保持在线。因此即使新环境手动发图成功，也只能证明新组合可用，不能单独归因于 Ubuntu 发行版。真实微信图片送达以用户手动测试结果为准。

2026-09-11 登录排查进一步确认：仅设置容器环境变量不足以让微信实际连接使用代理。已在微信登录页右上角的网络代理设置中启用 `192.168.65.254:7890`（当前 `host.docker.internal` 解析地址），账号和密码留空。通过进程连接检查确认微信连接到了此代理。用户随后确认登录成功且能够手动发送图片。若 Docker 宿主网关变化，应重新解析宿主地址并更新微信内的代理设置。

## 管理后台接入：桌面管理模式

原管理地址 `http://127.0.0.1:5100/` 可运行独立的桌面管理模式。默认选择 Ubuntu，页面内嵌入可交互的 noVNC 桌面，也可选择原 Debian 实例或在独立窗口打开。实例选择仅作用于当前浏览器页面，不修改其他页面的控制对象。

在停止占用 5100 的旧后台后，从项目根目录启动：

```sh
python3 -m core.desktop_management
```

此入口只加载 Flask 和桌面管理模块，不加载机器人、定时任务、联系人数据库、密钥读取或发送队列。旧聊天页面访问发送、同步、密钥、消息等接口统一返回 `409 desktop_only`，请刷新页面进入桌面管理。不会重启微信容器或清理登录数据。

`GET /api/desktop/instances` 返回固定实例列表，`GET /api/desktop/status?instance=ubuntu|legacy` 只检查对应容器和微信进程。进程运行不代表已经登录，界面明确提示以微信桌面为准。自动回复、后台消息同步、后台文本/图片发送和 Hook 本阶段均未接入。

验证：`python3 -m unittest tests.test_desktop_management -v`。覆盖旧页面误发阻断、实例隔离、非白名单实例拒绝、连接超时，以及不把进程存在误报为已登录。
