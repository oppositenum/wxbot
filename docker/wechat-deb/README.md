可选:把固定版本的微信安装包放在这里(不进 git),构建镜像时优先用它,不再从腾讯下载:

- `WeChatLinux_arm64.deb`(Apple Silicon / ARM 服务器)
- `WeChatLinux_x86_64.deb`(普通 x86 服务器)

版本和 sha256 必须和 `docker/Dockerfile` 里的 `WECHAT_VERSION` / `WECHAT_SHA256_*` 一致(当前 4.1.13.29)。
腾讯官网升级后直接下载会校验失败,这时把旧版 deb 放这里即可继续构建。
