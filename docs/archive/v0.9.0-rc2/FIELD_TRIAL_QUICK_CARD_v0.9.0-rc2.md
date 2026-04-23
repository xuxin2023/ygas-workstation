# FIELD TRIAL QUICK CARD v0.9.0-rc2

## 1. 本轮试用包

- APP_VERSION：`v0.9.0-rc2`
- 源码包：`GasAxisStudio_Source_v0.9.0-rc2.zip`
- 源码包 SHA256：`15DE815DB0A1A532F181DAE5BFCE1ECEF046B4D3C10B979324860EBDA2CF88C0`
- 现场试用资料包：`GasAxisStudio_FieldTrial_v0.9.0-rc2.zip`
- 现场要求：同一轮试用只使用同一套资料包和同一 SHA256 的源码包

## 2. 试验前必须确认

- 设备 ID
- 是否多设备总线
- 是否允许写命令
- 是否允许 `FFF`
- 当前主动上传状态
- 试验后要恢复到什么状态

## 3. 高风险禁止事项

- 未确认目标设备不得执行写命令
- 未隔离的多设备生产总线上不得直接试 `FFF` 写命令
- 不得跳过试验后状态恢复检查
- 不得未导出会话包就结束失败场景
- 不得手工修改日志后提交问题单
- 不得混用不同 SHA256 的包做同一轮试用

## 4. 暂停上传后读取说明

- 不会停止测量
- 会临时暂停设备主动上报实时数据
- 读取后按原始主动上传状态决定是否恢复

## 5. 恢复失败时怎么做

- 先截图
- 可点 `重试恢复主动上传`
- 如选择 `保持关闭`，必须导出 `command_log.csv`
- 必要时手动执行 `SETCOMWAY=1`

## 6. 必须导出的文件

- `session package`
- `command_log.csv`
- `parameter_change_journal.csv/json`
- `session_summary.json`
- 原始串口日志
- 截图

## 7. P0 / P1 立即上报条件

- 误写设备
- `FFF` 误触发
- 读命令无确认改变设备状态
- 长时间采集导出不完整
- 只读锁失效
- 关键状态显示错误
- 恢复主动上传失败无法处理
