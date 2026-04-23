# RELEASE CHECKLIST v0.9.0-rc2

## 基本信息

- APP_VERSION: `0.9.0-rc2`
- 源码包: `dist/GasAxisStudio_Source_v0.9.0-rc2.zip`
- 源码包 SHA256: `15DE815DB0A1A532F181DAE5BFCE1ECEF046B4D3C10B979324860EBDA2CF88C0`
- 现场试用资料包: `dist/GasAxisStudio_FieldTrial_v0.9.0-rc2.zip`
- 分发策略: `方案 B - 生成现场试用资料包`
- 说明: 源码包 `GasAxisStudio_Source_v0.9.0-rc2.zip` 保持原样不变；现场试用文档通过资料包单独整理分发
- 生成方式: `powershell -ExecutionPolicy Bypass -File .\package_source_zip.ps1`

## 分发说明

### 源码包交付

- 只使用 `dist/GasAxisStudio_Source_v0.9.0-rc2.zip`
- 禁止上传工作区压缩包

### 现场试用交付

- 只使用 `dist/GasAxisStudio_FieldTrial_v0.9.0-rc2.zip`
- 该资料包内包含源码包和现场试用文档
- 禁止上传工作区压缩包

## 执行命令结果

- `python -S -m compileall -q ygas_monitor tests`
  - 结果: 通过
- `python -m pytest -q`
  - 结果: `235 passed, 19 subtests passed in 314.05s (0:05:14)`
- `powershell -ExecutionPolicy Bypass -File .\clean_packaging.ps1`
  - 结果: `Packaging cleanup finished.`
- `powershell -ExecutionPolicy Bypass -File .\check_packaging.ps1`
  - 结果: `Packaging check passed. No blocked workspace paths found.`
- `powershell -ExecutionPolicy Bypass -File .\package_source_zip.ps1`
  - 结果: 通过
  - 附加提示: `请只上传 dist/GasAxisStudio_Source_v0.9.0-rc2.zip，不要上传项目工作区压缩包。`
- `powershell -ExecutionPolicy Bypass -File .\check_packaging.ps1 -ZipPath .\dist\GasAxisStudio_Source_v0.9.0-rc2.zip`
  - 结果: `Packaging check passed. Workspace and zip contents are clean.`

## zip-level 检查结果

- zip 路径: `dist/GasAxisStudio_Source_v0.9.0-rc2.zip`
- zip 顶层路径是否全部 ASCII: 是
- zip 顶层非 ASCII 条目: `NONE`
- zip 阻断项命中数: `0`
- zip 阻断项样本: `NONE`
- zip 内版本文件: `ygas_monitor/version.py`
- zip 内版本号是否匹配 `0.9.0-rc2`: 是

### zip 顶层条目

- `.gitignore`
- `build_installer.ps1`
- `build_windows.ps1`
- `check_packaging.ps1`
- `clean_packaging.ps1`
- `GasAxisStudio.spec`
- `main.py`
- `package_source_zip.ps1`
- `PACKAGING.md`
- `README.md`
- `requirements.txt`
- `assets`
- `installer`
- `tests`
- `ygas_monitor`

## 阻断项明细

- `__pycache__`: 未包含
- `*.pyc`: 未包含
- `.pytest_cache`: 未包含
- `logs`: 未包含
- `exports`: 未包含
- `data/user_settings.json`: 未包含
- 中文乱码顶层目录: 未包含

## 旧词扫描结果

- 扫描范围:
  - `README.md`
  - `PACKAGING.md`
  - `assets/help_zh_cn.md`
  - `ygas_monitor/ui/widgets/command_cards.py`
  - `ygas_monitor/ui/session_widget.py`
- 关键词:
  - `自动静音`
  - `静音读取`
  - `广播静音`
  - `静音窗口`
- 结果: `0` 命中

## action_label_zh 覆盖结果

- 模型层:
  - `ygas_monitor/models.py`
- 导出层:
  - `ygas_monitor/services/export_service.py`
- UI 层:
  - `ygas_monitor/ui/session_widget.py`
- 测试覆盖:
  - `tests/test_services.py`
  - `tests/test_session_ui.py`
  - `tests/test_rc1.py`
- 结果: 已覆盖模型、UI、导出和测试

## command_log.csv BOM 检查结果

- BOM: 是
- 表头包含 `action_label_zh`: 是
- 中文动作标签写入正常: 是
- 抽查样本动作: `保持主动上传关闭`

## check_packaging.ps1 额外脚本验证

- 嵌套缓存样例 `GasAxisStudio_Source_v0.9.0-rc2/ygas_monitor/__pycache__/x.pyc`: 已阻断
- 非 ASCII 顶层样例 `气体分析仪实时数据/main.py`: 已阻断
- 干净包样例: 通过

## 发布结论

- 本次验收通过
- 源码包交付应为 `dist/GasAxisStudio_Source_v0.9.0-rc2.zip`
- 现场试用最终上传/分发包应为 `dist/GasAxisStudio_FieldTrial_v0.9.0-rc2.zip`
- 不应上传项目工作区快照、中文根目录手工压缩包或任何包含缓存文件的 zip
