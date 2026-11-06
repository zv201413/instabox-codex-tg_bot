# VPS Controller

本交付默认同 bot 网关模式，手动保活，最长 480 分钟。机器命令包括 /resume、/hold、/release、/sleep、/vmstatus、/mode、/schedule。

Python 标准库、SQLite 持久化、固定机器绑定、TG 私聊白名单；恢复和休眠操作使用持久化幂等键。冷却与限流按平台实际截止重试；不使用 Resume 失败后 Start 兜底。休眠调用 Suspend，保留内存进程。

默认使用 Python 3.11+ 普通进程：配置、SQLite、日志放在 `/home/container/.tg_bridge/instabox-controller/`，supervisor 自动重启，启动钩子接入现有 `start-all.sh`。无需 root、Docker、systemd 或第三方 Python 依赖。监听端口可自选，公网入口通过已有反向代理提供 HTTPS，私有网络支持私有 IP 的 HTTP。

Docker Compose 仍可选，使用非 root、只读根目录和持久卷；默认宿主机 `127.0.0.1:8080`。

安装、策略、切换与回退详见 [SINGLE-BOT-SETUP.md](SINGLE-BOT-SETUP.md)。
