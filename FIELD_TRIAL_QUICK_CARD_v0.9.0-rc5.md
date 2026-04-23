# Field Trial Quick Card v0.9.0-rc5

## 1. 到包先做什么

1. 先核对外部 `.sha256` 文件。
2. 打开 FieldTrial zip，核对顶层 `SHA256SUMS.txt`。
3. 确认 `GasAxisStudio_Source_v0.9.0-rc5.zip` 和现场资料文件都与 `SHA256SUMS.txt` 一致。

## 2. 现场默认主流程

1. 目标设备使用明确三位 ID，不使用 `FFF`。
2. 保持默认“实时监测模式”。
3. 采集方式选择“自动上传 / LISTEN”。
4. 解析模式保持 `AUTO`。
5. 点击连接后，软件会自动尝试发送 `SETCOMWAY=1`。

## 3. 没有曲线排查卡

1. 是否已连接？
2. 实时流状态是“启动中 / 已开启 / 启动失败 / 禁止自动广播”中的哪一种？
3. `SETCOMWAY=1` 是否 ACK？
4. `RX` 是否有？
5. `有效帧` 是否有？
6. `解析模式` 是否为 `AUTO`？
7. 目标设备 ID 是否正确？
8. 波特率、线缆、设备输出 `MODE` 是否正确？

## 4. 现场留证

- 优先导出会话包。
- 发生问题时同步截图、命令日志和异常说明。
- 详细步骤见 `FIELD_TRIAL_TEST_PLAN_v0.9.0-rc5.md` 和 `RELEASE_CHECKLIST_v0.9.0-rc5.md`。
