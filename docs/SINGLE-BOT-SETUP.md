# 单 bot 安装、切换与回退

VPS 接收 Telegram 消息，处理唤醒、保活和休眠。instabox 内的 Bridge 主动领取消息，继续使用原有 Codex RPC、模型配置、账号绑定、会话和任务队列。两端使用当前对话 bot 的同一个 token。

## 1. 普通用户 VPS 容器

需要 Linux、Python 3.11+（含 SQLite）以及到 Telegram、instabox API 的出站网络。Controller 仅使用 Python 标准库，无需 sudo、Docker、systemd 或 pip 依赖。

若平台只持久化 `/home/container`，仓库、配置、SQLite、日志都放在这里：

```bash
cd /home/container
git clone https://github.com/zv201413/instabox-codex-tg_bot.git
cd instabox-codex-tg_bot
python3 --version
python3 tools/configure_vps.py --mode process --port 18080
```

`18080` 是示例，可用容器允许的任意未占用高端口（1024–65535）。不传 `--port` 时交互选择，默认 8080。工具询问固定机器 ID、原 Bridge 已绑定的 TG 用户 ID、网关 URL、时区，隐藏输入 instabox API key 和当前对话 bot token，自动生成独立 agent 密钥。系统没有时区数据库时默认 `UTC+08:00`。

默认持久目录 `/home/container/.tg_bridge/instabox-controller/`：

| 文件 | 用途 |
| --- | --- |
| `controller.env` | Controller 配置，权限 0600 |
| `bridge-connection.json` | 网关连接与独立共享密钥，权限 0600 |
| `controller.db` | 待交付消息、控制状态、请求去重 |
| `controller.log`、`controller.log.1` | 日志与轮转副本 |
| `supervisor.json`、`*.lock` | 进程身份与启动锁 |

可用 `--data-dir /home/container/其他目录` 自选；之后服务命令和启动钩子均需使用相同 `--data-dir`。已有凭据文件时工具拒绝覆盖。修改配置后停止、再启动服务；SQLite 中通过 TG 选择的模式和计划会保留。

生成的默认策略：

```dotenv
TELEGRAM_GATEWAY_MODE=gateway
INSTABOX_KEEP_MODE=manual
HOLD_WINDOW_MINUTES=60
MAX_HOLD_MINUTES=480
INSTABOX_LEASE_TTL=300
INSTABOX_RENEW_EVERY=120
INSTABOX_AUTO_START=0
```

API key 需要固定机器读取、操作查询、活动租约、Resume 和 Suspend 权限。模型凭据留在 instabox；Secrets 代理占位符不能作为 VPS 的真实 API key。凭据文件不上传 GitHub，也不通过 TG 或聊天传递。

## 2. 网关端口和入口

单 bot 模式需要 **instabox 能访问 VPS 网关**。Controller 监听 `0.0.0.0:所选端口`。配置工具不会替平台开放端口或安装反向代理，需在容器面板分配/映射端口，或使用已有反向代理、私有网络。

| 方式 | Bridge 配置示例 |
| --- | --- |
| 公网 HTTPS 反向代理 → 容器 18080 | `https://gateway.example.com` |
| 外部 HTTPS 18443 → 容器 18080 | `https://gateway.example.com:18443` |
| 私有网络直达容器 18080 | `http://10.0.0.2:18080` |

外部端口与容器监听端口可以不同。公网入口必须提供 HTTPS，Controller 自身提供 HTTP；无法安装代理时可用面板已有的 HTTPS 转发。私有 IP 的 HTTP 被支持。不要把 VPS 的 localhost 填成 instabox 的地址。instabox 无需开放入站端口。

## 3. 接入现有 start-all.sh

在 VPS 仓库目录执行（配置工具也支持同名 `--startup-script` 参数）：

```bash
python3 tools/install_startup.py \
  --script /home/container/start-all.sh \
  --data-dir /home/container/.tg_bridge/instabox-controller
```

工具备份旧脚本，保留原命令和权限，将启动钩子放在已有 `exec` / `exit` 之前。重复执行更新同一块。工具不会立即启动 Controller。平台重启后须实际执行 `start-all.sh`，钩子才生效。

钩子启动脱离终端的 supervisor，子进程退出后以 2–30 秒退避重启，避免重复启动，日志约 10 MiB 轮转。容器退出时所有进程停止，重建后依靠平台启动入口重新启动。若面板要求以前台程序维持容器，可将此命令作为前台入口：

```bash
python3 tools/controller_service.py run
```

此时使用一个 supervisor 即可。面板直接启动 Python 时使用 `run`；面板运行多服务 `start-all.sh` 时使用脚本钩子。

`run` 的输出交给面板或调用者收集；`start` 和脚本钩子将日志写到上述持久目录。

## 4. 安装到现有 instabox Bridge

在 instabox 获取同版仓库，下列假定 `/data/workspace/instabox-codex-tg_bot`。通过 SSH/SFTP 私下把 VPS 的 `bridge-connection.json` 传到 `/data/home/telegram-codex/bridge-connection.json`。

```bash
chmod 600 /data/home/telegram-codex/bridge-connection.json
python3 /data/workspace/instabox-codex-tg_bot/tools/install_bridge.py \
  --runtime /data/home/telegram-codex \
  --connection-file /data/home/telegram-codex/bridge-connection.json
```

安装器备份 `bridge.py`、`elicitation.py`、`gateway.py`、`secrets.json` 和 SQLite，然后更新源码，合并 `message_source=gateway` 与 `gateway` 字段。原 bot token、模型配置、账号、`state.sqlite3`、上传目录和 Codex 会话保留。安装器不自动重启；其他运行目录用 `--runtime` 指定。

## 5. 切换同一个 bot

先完成正在运行的任务、审批和表单。Bridge 重启会结束 app-server 子进程，正在运行的任务会标记中断。

1. 在 instabox 停旧 Bridge 及同 bot 的其他轮询实例。
2. 在 VPS 启动 Controller，核对本地和外部网关可达。
3. 在 instabox 启动已合并配置的 Bridge。
4. 发 `/vmstatus` 确认机器运行、Bridge 已就绪，再验证对话。

原 instabox 使用用户 systemd 服务时，先执行：

```bash
systemctl --user stop telegram-codex.service
```

VPS 在仓库目录执行：

```bash
python3 tools/controller_service.py start
python3 tools/controller_service.py status
curl -f http://127.0.0.1:18080/healthz
tail -n 50 /home/container/.tg_bridge/instabox-controller/controller.log
```

随后在 instabox：

```bash
systemctl --user start telegram-codex.service
systemctl --user status telegram-codex.service
```

systemd 命令仅针对 instabox 已有服务，VPS 不需要 systemd。其他 Bridge 管理方式使用原入口停止和启动。核对并停用旧 keepalive、cron、Worker，按归属释放旧租约，避免其他服务持续保活。若 bot 已设置 webhook，Controller 会报错，不会自动删除它。

VPS 日常停服与重新启动：

```bash
python3 tools/controller_service.py stop
python3 tools/controller_service.py start
```

停服保留凭据和 SQLite，等待 Controller 完成关闭；它不是 `/sleep` 的替代命令。

## 6. Docker 可选部署

有 Docker 权限的 VPS 可用：

```bash
python3 tools/configure_vps.py --mode docker
docker compose -f docker/docker-compose.controller.yml build
# 停旧 TG 轮询后再执行：
docker compose -f docker/docker-compose.controller.yml up -d
```

配置在 `docker/.env.controller`，连接文件在根目录 `bridge-connection.json`，SQLite 放在持久卷。Compose 默认 `127.0.0.1:8080:8080`；自选宿主机端口需修改左侧，例如 `127.0.0.1:18080:8080`，容器内仍为 8080。HTTPS 代理参考 `docker/Caddyfile.example`。

## 7. 使用、验收与回退

| 命令 | 行为 |
| --- | --- |
| `/resume` | 唤醒，冷却期间等待平台截止后重试 |
| `/hold 30m`、`/hold 2h`、`/hold 8h` | 暂停时先恢复，运行后开始计时；超出上限拒绝 |
| `/release` | 结束续租、切换 manual、释放自身租约，不立即暂停 |
| `/sleep` 或 `/suspend` | 明确暂停，保留进程与内存，之后需要显式唤醒 |
| `/mode manual` | 仅明确 hold 保活；普通消息保存但不自动唤醒 |
| `/mode auto` | 按交互/任务保活；自动暂停后普通消息可唤醒；手动暂停仍需显式命令 |
| `/vmstatus`、`/schedule` | 查询状态、管理工作时段 |

对话、附件、会话续接、队列、取消、追加说明、审批和标准表单继续由 Bridge 处理。保活不改变平台运行上限、不重置已运行时间；其他连接或租约可能延迟平台空闲暂停。

部署后验收：会话续接、图片/文件、审批/表单、短时间 hold 到期、`/sleep` → `/resume`、暂停期间消息恢复后交付、Bridge 就绪提示。真实 VPS/TG 验收尚待安装完成；本地测试范围见 [VALIDATION.md](VALIDATION.md)。

网关至少一次交付并持久化去重；回复在网络结果不确定时仍可能重复。回退前先排空 VPS 和 Bridge inbox，已确认的消息不会重新出现在 TG 队列。随后停 VPS Controller、停 Bridge，把 `secrets.json` 的 `message_source` 改回 `telegram`，再启动 Bridge。不要删除状态库、会话或备份。
