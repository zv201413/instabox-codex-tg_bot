# 实际 Codex Bridge 网关接入

完整源码在 bridge/codex/。安装器将 bridge.py、elicitation.py、gateway.py 更新到原 /data/home/telegram-codex/，备份并保留账号、模型配置、会话和状态库。

message_source=telegram 保持旧接收方式；message_source=gateway 启动独立领取、心跳和派发循环，不启动旧 getUpdates。文件下载/发送、任务执行、审批与表单继续由原 Bridge 处理。

新 gateway-inbox.sqlite3 先保存完整更新再确认 VPS，update_id 去重。普通任务队列最多 20 条；命令、审批和当前表单文字答案使用控制通道。问题 token、消息 ID、任务 ID 和 TG 时间用于拒绝过期交互。

Agent API：POST /agent/heartbeat、POST /agent/pull、POST /agent/ack。所有请求携带 X-Agent-Secret、machine_id、agent_id。pull 可携带 input_prompt={message_id,created_at}，让队列满时当前表单答案仍能交付。

安装和切换步骤详见 [SINGLE-BOT-SETUP.md](SINGLE-BOT-SETUP.md)。
