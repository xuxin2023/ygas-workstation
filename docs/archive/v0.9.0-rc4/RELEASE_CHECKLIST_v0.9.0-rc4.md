# Release Checklist v0.9.0-rc4

## 版本与源码

- [ ] `APP_VERSION = 0.9.0-rc4`
- [ ] `ygas_monitor.__version__ = 0.9.0-rc4`
- [ ] `dist/GasAxisStudio_Source_v0.9.0-rc4.zip` 已由当前工作区源码重打
- [ ] Source zip 内不包含 `SHA256SUMS.txt`

## 监测主流程

- [ ] 默认会话模式为实时监测模式
- [ ] 默认采集方式为自动上传 / `LISTEN`
- [ ] 默认解析模式为 `AUTO`
- [ ] 连接后默认自动启动实时流
- [ ] 自动发送的唯一命令是 `SETCOMWAY=1`
- [ ] 严格只读 / 严格只听 / 回放 / 手动读取 / `FFF` 广播都不会自动发送
- [ ] ACK 成功但无首帧时，有明确超时提示
- [ ] 收到原始 RX 但无有效帧时，有明确解析失败提示

## UI 与帮助文档

- [ ] 顶部状态条不超过 64 px
- [ ] 监测页保持曲线优先
- [ ] 默认 6 张核心 KPI
- [ ] 辅助区默认隐藏
- [ ] 曲线设置默认收起
- [ ] 小屏 / Windows 缩放下，设备控制、系数中心、信号参数、专家页、导出页可完整滚动访问
- [ ] 帮助文档已同步“连接后自动启动实时流”主流程

## 打包与校验

- [ ] `dist/GasAxisStudio_FieldTrial_v0.9.0-rc4.zip` 已生成
- [ ] `dist/GasAxisStudio_FieldTrial_v0.9.0-rc4.zip.sha256` 已生成
- [ ] FieldTrial 顶层包含 `SHA256SUMS.txt`
- [ ] `SHA256SUMS.txt` 中 Source zip 和现场资料 SHA256 逐项匹配
- [ ] `.sha256` 文件内容与 FieldTrial zip 实际 SHA256 一致
- [ ] FieldTrial zip 只包含 rc4 Source zip，不包含 rc3 Source zip
- [ ] `SHA256SUMS.txt` 与 `.sha256` 已复核

## 分发

- [ ] 对外只分发 `dist/GasAxisStudio_FieldTrial_v0.9.0-rc4.zip`
- [ ] 不分发工作区压缩包
- [ ] 不分发旧版 Source zip
