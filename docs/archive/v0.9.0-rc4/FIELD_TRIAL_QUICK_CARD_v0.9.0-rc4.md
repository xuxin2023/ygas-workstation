# Field Trial Quick Card v0.9.0-rc4

## 1. 到包先做什么

1. 先核对外部 `.sha256` 文件。
2. 打开 FieldTrial zip，核对顶层 `SHA256SUMS.txt`。
3. 确认 `GasAxisStudio_Source_v0.9.0-rc4.zip` 和现场资料文件都与 `SHA256SUMS.txt` 一致。

## 2. 现场默认主流程

1. 目标设备使用明确三位 ID，不使用 `FFF`。
2. 保持默认“实时监测模式”。
3. 采集方式选择“自动上传 / LISTEN”。
4. 解析模式保持 `AUTO`。
5. 点击连接后，软件会自动尝试发送 `SETCOMWAY=1`。

## 3. 如果没有曲线

- 已连接但未启动实时流：点“启动实时流”重试。
- 启动实时流失败：检查设备 ID、波特率、线缆、目标设备和 ACK。
- ACK 成功但无有效帧：检查设备是否真的输出、解析模式是否匹配。
- 收到原始 RX 但无有效帧：优先切回 `AUTO`，再检查设备输出是 `MODE1` 还是 `MODE2`。

## 4. 现场留证

- 优先导出会话包。
- 发生问题时同步截图、命令日志和异常说明。
- 详细步骤见 `FIELD_TRIAL_TEST_PLAN_v0.9.0-rc4.md` 和 `RELEASE_CHECKLIST_v0.9.0-rc4.md`。
