# instabox-codex-tg_bot

在常开 VPS 上通过 Telegram 唤醒 instabox 机器，并按需、手动或定时续租保活。支持冷却等待、重复请求合并和 SQLite 持久化。

## 部署

需要 Docker Compose、instabox API key、固定机器 ID 和 Telegram bot。

```bash
git clone https://github.com/zv201413/instabox-codex-tg_bot.git
cd instabox-codex-tg_bot
cp docker/.env.controller.example docker/.env.controller
chmod 600 docker/.env.controller
```

编辑 `docker/.env.controller`，填写 API key、机器 ID、bot token、允许操作的用户 ID，以及独立 agent 密钥，然后启动：

```bash
docker compose -f docker/docker-compose.controller.yml up -d --build
```

默认 `control_only` 使用独立控制 bot，原 Codex 对话 bot 保持原有方式。同 bot 的 `gateway` 模式需要将现有 Bridge 接入消息网关，并停止原 TG 轮询。仓库提供控制器和通用接口，完整 Codex Bridge 需自行接入。

## 使用

- `/resume`：唤醒机器。
- `/hold 60m`：保活一小时。
- `/release`：结束保活，释放本服务租约。
- `/vmstatus`：查看机器与保活状态。
- `/mode`、`/schedule`：设置按需、手动或工作时段策略。

TG 唤醒由 Controller 实现；旧 `keepalive.sh` 仅用于持续保活。活动租约不延长平台运行上限，冷却以平台截止时间为准。

## 文档

- [Controller 部署与配置](docs/CONTROLLER.md)
- [现有 Codex Bridge 对接](docs/BRIDGE-INTEGRATION.md)
- [旧保活工具](docs/LEGACY-KEEPALIVE.md)

基于 [pingmike2/instabox-keepalive](https://github.com/pingmike2/instabox-keepalive) 扩展，遵循 [MIT License](LICENSE)。
