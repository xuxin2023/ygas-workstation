# 专业气体分析仪工作站 RC-1

这是一个独立的 Windows 桌面上位机软件，用于 YGAS 气体分析仪的实时监测、只读联调、正式控制、原始帧诊断、CSV 回放、会话归档和诊断包导出。

当前版本定位为 `RC-1`：
- 保持 Beta-2 的监测、控制、回放、导出和权限体系
- 继续收口真机联调稳定性、诊断可追溯性、设置迁移和 Windows 打包交付
- 与任何 V1/V2 自动标定主流程无耦合

## 主要能力

- 实时监测页：大字数值卡片、实时曲线、状态位解码、原始帧查看、异常摘要
- 设备控制页：按业务意图组织命令，支持查询、写入、命令预览、权限与 FFF 风险控制
- 系数中心：`GETCO`、`SENCO1~9`、`CLEARSENCOx` 等正式命令入口
- 信号与滤波：`SETPOW`、`SETCO2`、`TIMEOUT`、`SENTEMP1/2`、`AVERAGE1/2`
- 专家终端：原始命令调试、抓包和响应查看
- 数据导出与回放：结构化 CSV、会话包、诊断包、轻量真实回放
- 真机联调辅助：安全握手、只读会话锁、自动重连、串口诊断、会话备注、日志目录打开、环境信息复制

## 技术栈

- Python 3.11+
- PySide6
- pyqtgraph
- pyserial
- pandas

## 目录结构

```text
.
├─ main.py
├─ requirements.txt
├─ README.md
├─ YGasWorkstation.spec
├─ build_windows.ps1
├─ assets/
├─ tests/
└─ ygas_monitor/
   ├─ app.py
   ├─ config.py
   ├─ version.py
   ├─ models.py
   ├─ commanding/
   ├─ protocols/
   ├─ serial/
   ├─ services/
   └─ ui/
```

## 安装依赖

建议 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 本地运行

```powershell
python main.py
```

推荐联调顺序：

1. 打开软件后默认进入“实时监测”
2. 在顶部快速连接区选择串口和波特率
3. 联调模式优先选“只监听”或“安全握手”
4. 点击“连接”
5. 如需只读探测，点击“安全握手”
6. 确认 ID、MODE、FTD、串口参数后，再按需切到“工程模式”

## 联调模式说明

- 只监听：绝不主动发命令，只接收实时流
- 安全握手：只允许查询类命令；内置安全握手按钮会读取 `ID / MODE / FTD / SETCOM`
- 工程模式：允许正式控制命令，但仍受权限等级、只读锁和 FFF 策略约束
- 只读会话锁：全局禁止写命令，即使切页也不能误发写入类命令

## 运行期目录

发布版不再依赖项目源码目录下的 `data/`、`logs/`、`exports/`。

Windows 默认目录：

- 设置目录：`%APPDATA%\YGasWorkstation\`
  - `user_settings.json`
  - `command_templates.json`
- 日志目录：`%LOCALAPPDATA%\YGasWorkstation\logs\`
- 导出目录：`%LOCALAPPDATA%\YGasWorkstation\exports\`
- 缓存目录：`%LOCALAPPDATA%\YGasWorkstation\cache\`
  - `replay\`

首次启动会自动创建这些目录。若检测到旧版项目目录中的 `data/user_settings.json`，会尝试一次性迁移；配置损坏时自动回退默认值，不会导致程序崩溃。

## 导出说明

### 1. 结构化 CSV

导出当前会话缓存的解析数据。

### 2. 会话包

会话包目录命名格式：

```text
session_YYYYMMDD_HHMMSS
```

内容至少包含：

- `structured_data.csv`
- `raw_frames.log`
- `command_log.tsv`
- `config_snapshot.json`
- `session_summary.json`
- `session_summary.txt`
- `session_logger.log`（若当前会话已生成日志文件）

### 3. 诊断包

诊断包用于现场排障，内容至少包含：

- `app_info.json`
- `config_snapshot.json`
- `recent_session_summary.json`
- `recent_raw_frames.log`
- `recent_command_log.tsv`
- `recent_exceptions.log`
- `session_note.txt`
- `session_logger.log`（若存在）

## 回放说明

回放页支持：

- 加载 CSV
- 开始 / 暂停
- 1x / 2x / 5x
- 进度条拖动
- 单步前进 / 单步后退
- 跳到开头 / 跳到末尾

回放开始时会清空并重建：

- 数据卡片
- 曲线
- 状态面板
- 事件时间线
- 原始帧视图

回放期间禁止向真实串口发送命令。

## Windows 打包

当前优先保证 `onedir` 交付稳定性。

### 方式一：直接用 spec

```powershell
pip install pyinstaller
pyinstaller --noconfirm .\YGasWorkstation.spec
```

### 方式二：使用构建脚本

```powershell
.\build_windows.ps1
```

构建脚本会：

1. 清理旧的 `build/` 和 `dist/`
2. 清理工作区内 `__pycache__/`
3. 安装 `pyinstaller`
4. 依据 `YGasWorkstation.spec` 生成发布目录

默认产物目录：

```text
dist\YGasWorkstation\
```

如果 `assets\app.ico` 不存在，仍可正常打包，只是使用默认图标。

## 发布版构建流程

建议按下面顺序执行：

1. `python -m unittest discover -s tests -v`
2. `python -m compileall main.py ygas_monitor tests`
3. `.\build_windows.ps1`
4. 在 `dist\YGasWorkstation\` 下做一次冷启动验证
5. 确认发布版首次启动会自动创建 `%APPDATA%` / `%LOCALAPPDATA%` 目录

## 常见问题

### 串口打开失败

常见原因：

- 串口被其他程序占用
- 当前账号无权限访问串口
- 设备已拔掉或串口号变化

软件会在界面中给出明确中文提示，并记录到日志。

### 为什么默认不直接发初始化写命令

RC-1 以真机联调安全为优先。首次接入真机时，建议先只监听或安全握手，确认设备身份和工作模式后，再进入工程模式。

### 为什么 FFF 默认关闭

`FFF` 是广播地址，可能同时影响多台设备。只有在明确现场隔离、权限满足、并经过二次确认后才应启用。

### 回放 CSV 至少需要哪些列

至少需要：

- `timestamp`
- `mode`
- `raw`

缺少关键列时，软件会给出中文错误提示。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall main.py ygas_monitor tests
```
