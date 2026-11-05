# instabox-codex-tg_bot

在常开 VPS 上接收 Telegram 指令，使用 instabox 官方 API 唤醒机器，并按需要、手动期限或工作时段续租保活。机器空闲时允许平台正常休眠；VPS 控制端持续在线，休眠后仍能收到唤醒指令。

本项目基于 [pingmike2/instabox-keepalive](https://github.com/pingmike2/instabox-keepalive) 扩展。**TG 唤醒由 Python Controller 实现，单独安装旧 `keepalive.sh` 不会安装 TG 功能。**

## 当前发布范围

| 内容 | 本仓库 |
| --- | --- |
| VPS Controller：TG 命令、保活策略、Resume、冷却等待、SQLite 持久化 | 已包含 |
| 通用 Bridge 接口与客户端：pull / ack / heartbeat、本地 inbox | 已包含在 `bridge/gateway_client.py`、`bridge/inbox.py` |
| 现有 Codex Bridge 的完整源码与具体接入改造 | **暂不发布**；本地 `bridge/codex/` 保留，不包含在本次提交或发布历史中 |
| 旧 shell / Docker / Worker 持续保活工具 | 保留；不提供 TG 消息接收 |
| 云端运行状态 | 尚未部署验收；当前验证为本地模拟 |

因此可以直接部署**独立控制 bot**：在控制 bot 唤醒机器，再回原 Codex 对话 bot 使用。若要在**同一个 bot** 中“发普通消息就唤醒并继续 Codex 对话”，还需要将机器内的 Bridge 改为网关领取模式；该具体 Bridge 本次不随仓库分发。

## TG 如何唤醒机器

1. VPS 上的 Controller 使用 Telegram `getUpdates` 长轮询，机器休眠时它仍在线。
2. 合法用户发送 `/resume` 或点击“唤醒”，Controller 保存恢复请求，调用 `POST /v1/machines/{id}/resume`。
3. 平台处于冷却时，保存截止时间并等待；重复请求合并，重试复用同一个幂等键。
4. 确认机器运行后，按保存的保活策略续租；结束保活只释放自己的租约，不直接暂停机器。
5. 同 bot 的 gateway 模式还会保存对话消息，等 Bridge 就绪后交付。独立控制 bot 不转发对话。

机器手动暂停时不会因为普通对话或定时器自行恢复，需要显式唤醒。Start 默认关闭，Resume 失败不会转 Start。

## 两种使用方式

| 模式 | 设置 | 适用情况 |
| --- | --- | --- |
| 独立控制 bot（本次发布推荐） | `TELEGRAM_GATEWAY_MODE=control_only` | 一个新 bot 管理机器，原 Codex bot 保持现状，无需发布或改造原 Bridge |
| 同 bot 网关 | `TELEGRAM_GATEWAY_MODE=gateway` | VPS 成为唯一收消息端；机器内 Bridge 必须另外实现 pull/ack/heartbeat，并停掉原 `getUpdates` |

**不要让 VPS 和原 Bridge 同时轮询同一个 bot。** 同 bot 部署需先按 [切换手册](docs/CONTROLLER.md#4-现有-codex-bridge-切换与回退) 完成 Bridge 对接。

## 快速部署：独立控制 bot

需要常开的 VPS、Docker Compose、一个固定的 instabox 机器 ID，以及独立 Telegram 控制 bot。控制器只使用 Python 标准库。

```bash
git clone https://github.com/zv201413/instabox-codex-tg_bot.git
cd instabox-codex-tg_bot
cp docker/.env.controller.example docker/.env.controller
chmod 600 docker/.env.controller
```

用编辑器填写 `docker/.env.controller`，示例文件默认选择 `control_only`：

| 配置 | 填写内容 |
| --- | --- |
| `INSTABOX_API_KEY` | VPS 可用的 instabox API key；核验机器读取、租约写入/释放、Resume 和操作查询权限 |
| `INSTABOX_MACHINE_ID` | 承载 Codex Bridge 的固定 `m_...` ID，不使用 all |
| `TELEGRAM_BOT_TOKEN` | 新建的控制 bot token，不能与仍在轮询的 Codex 对话 bot 相同 |
| `TELEGRAM_ALLOWED_USERS` | 允许操作的 Telegram 用户数字 ID，逗号分隔；不能为空 |
| `AGENT_API_SECRET` | 独立随机密钥，至少 32 字符；agent 接口不默认放行 |
| `TELEGRAM_GATEWAY_MODE` | `control_only` |
| `TZ` | 计划时区，如 `Asia/Shanghai` |

凭据不要写进代码、提交或命令行。模型提供方凭据继续留在 instabox。平台出站代理的 Secrets 占位符不能直接复制到 VPS。

```bash
docker compose -f docker/docker-compose.controller.yml up -d --build
docker compose -f docker/docker-compose.controller.yml ps
docker compose -f docker/docker-compose.controller.yml logs --tail=50
curl -f http://127.0.0.1:8080/healthz
```

给控制 bot 发送 `/vmstatus`、`/resume` 或 `/hold 60m`。独立控制 bot 可以确认机器状态；没有接入 agent 时“Bridge 未就绪”表示未收到 Bridge 心跳，不代表机器未恢复。

Compose 默认使用非 root、只读根目录、持久化数据卷、日志轮转和 0.5 CPU / 256 MiB 限制。agent 端口只发布到宿主机 `127.0.0.1:8080`。独立控制 bot 无需公开 agent 入口；同 bot 接入应使用 HTTPS 或已有私有网络，参考 [Caddy 示例](docker/Caddyfile.example)。

## 命令和保活策略

常驻按钮：唤醒、保活 1 小时、结束保活、机器状态、保活设置。

| 命令 | 行为 |
| --- | --- |
| `/resume` | 显式唤醒固定机器，重复请求合并 |
| `/hold 60m`、`/hold 2h` | 保活指定时长；暂停时先恢复，确认运行后开始计时 |
| `/release` | 切换 manual，清除保活与待恢复意图，停止续租并释放自己的租约 |
| `/vmstatus` | 查看机器、agent、保活、租约、恢复阶段、冷却和 API 等待截止时间 |
| `/mode auto` | 切回按需保活 |
| `/mode manual` | 仅显式 hold 保活；彻底结束已有窗口使用 release |
| `/schedule` | 查看计划 |
| `/schedule Asia/Shanghai 09:00-18:00 0,1,2,3,4 wake=0` | 保存工作日计划；0=周一，6=周日；支持跨午夜 |
| `/schedule on`、`/schedule off` | 明确启用计划或切换 manual |

`manual` 仅显式 hold 保活。`on_demand` 是默认策略：显式唤醒创建使用窗口，gateway 模式的合法对话和任务心跳也可延长。`scheduled` 在时段内续租，时段外释放；计划主动唤醒默认关闭，开启后仍尊重手动暂停。

默认短租约 TTL **300 秒**、续租间隔 **120 秒**、每次交互窗口 **60 分钟**，连续按需保活上限 **360 分钟**。策略期限与每份租约 TTL 分别计算。环境设置只初始化新数据卷，重启不会覆盖已保存的用户模式和计划。

活动租约**不延长平台 `maxRunMinutes`**。达到平台运行上限仍可能暂停和冷却，具体以平台截止时间为准。release 不会中断任务；其他连接、任务或租约仍可能使机器继续在线。

## 同 bot 网关与消息可靠性

VPS 先持久化整批更新和 TG 游标，再确认后续批次。Bridge 先可靠提交本地 inbox，再 ack VPS；未 ack 可重投，按 `update_id` 去重。审批和任务控制使用独立交付额度，避免普通队列满时阻塞。

网关客户端见 [gateway_client.py](bridge/gateway_client.py)，持久 inbox 见 [inbox.py](bridge/inbox.py)。具体 Bridge 还必须保留账号、消息 ID、prompt token 和任务身份校验，拒绝过期审批、晚到文字和目标已改变的任务指令。**这些客户端文件不会自动把任意 Codex Bridge 改成网关模式。**

交付为至少一次传输，不承诺所有外部副作用严格一次。回复在网络超时但 Telegram 实际接受时可能重复。切换或回退前检查待交付队列，避免 VPS 已确认的消息被遗漏。

## 验证与文档

```bash
sh tools/run_tests.sh all
```

当前本地模拟验证：Controller **38 项**测试、shell/Worker **66 项**断言通过。完整本地 Bridge 曾通过 **47 项**测试；它不在本次发布包内，公开包会明确跳过 Bridge 测试。尚未构建运行 Docker 镜像或执行真实云端休眠、TG 唤醒、附件和审批验收。

- [Controller 部署、切换与回退](docs/CONTROLLER.md)
- [修复与验证记录](docs/VALIDATION.md)
- [旧持续保活工具手册](docs/LEGACY-KEEPALIVE.md)
- [instabox CLI 操作手册](docs/CLI.md)

旧 `keepalive.sh`、Worker 或 cron 如仍在续租，需要先停止；单独释放租约不能阻止旧程序下一轮重新建立。原始上游 raw 地址只代表上游版本，TG 部署应使用本仓库的 Controller 入口。

## 来源与许可

扩展自 [pingmike2/instabox-keepalive](https://github.com/pingmike2/instabox-keepalive)，保留上游 shell、Worker 和 Docker 工具及版权说明。早期 Worker 多账号实现来自 [jyucoeng/instabox-keepalive](https://github.com/jyucoeng/instabox-keepalive)。

使用 [MIT License](LICENSE)。本项目使用官方租约和 Resume 接口，遵守平台运行上限与冷却规则。
