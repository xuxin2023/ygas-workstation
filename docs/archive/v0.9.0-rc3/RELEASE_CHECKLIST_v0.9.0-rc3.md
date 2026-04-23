# RELEASE CHECKLIST v0.9.0-rc3

## 1. 基本信息

- APP_VERSION：`0.9.0-rc3`
- 源码包：`dist/GasAxisStudio_Source_v0.9.0-rc3.zip`
- 源码包 SHA256：以 `SHA256SUMS.txt` 为准
- 现场试用资料包：`dist/GasAxisStudio_FieldTrial_v0.9.0-rc3.zip`
- 资料包 SHA256：以外部 `dist/GasAxisStudio_FieldTrial_v0.9.0-rc3.zip.sha256` 为准
- 本轮边界：不改协议、不改命令语义、不放宽安全限制、不改变导出字段；本轮包含监测首屏 UI 收口、测试去重、帮助文档与打包校验更新

## 2. 自动化校验

以下结果均来自本轮实际运行：

- `python -S -m compileall -q ygas_monitor tests`：通过
- `python -m pytest tests/test_protocol.py -q`：`13 passed`
- `python -m pytest tests/test_registry.py -q`：`19 passed`
- `python -m pytest tests/test_services.py -q`：`56 passed`
- `python -m pytest tests/test_session_ui.py -q`：`134 passed, 12 subtests passed in 304.34s (0:05:04)`
- `python -m pytest tests/test_charts.py -q`：`9 passed`
- `python -m pytest tests/test_export.py -q`：`1 passed`
- `python -m pytest tests/test_rc1.py -q`：`11 passed, 7 subtests passed in 2.15s`
- `python -m pytest --collect-only tests/test_session_ui.py -q`：`134 tests collected in 1.83s`
- `python -m pytest -q`：`252 passed, 34 subtests passed in 335.87s (0:05:35)`
- `python -m pytest tests/test_release_consistency.py -q`：`5 passed, 15 subtests passed`

需同时记录：

- `tests/test_session_ui.py` 静态 test 方法数：`134`
- `pytest --collect-only` 实际收集数：`134`
- 是否存在 skipped / xfailed / warnings：否

## 3. 发布一致性

- 当前版本资料文档不得出现旧版本号或旧 SHA。
- `FIELD_TRIAL_QUICK_CARD_v0.9.0-rc3.md`、`FIELD_TRIAL_TEST_PLAN_v0.9.0-rc3.md`、`RELEASE_CHECKLIST_v0.9.0-rc3.md` 以 `SHA256SUMS.txt` 与外部 `.sha256` 为校验准绳。
- `package_source_zip.ps1` 必须纳入 `tests/test_export.py`、`tests/test_rc1.py` 和全量 `python -m pytest -q`。
- `package_fieldtrial_zip.ps1` 必须在打包后运行发布一致性检查脚本。

## 4. 打包要求

- Source zip 只保留当前版本资料，不与旧版本现场资料并列分发。
- FieldTrial zip 只包含当前版本 Source zip，不得混入旧版本 Source zip。
- zip entry 全部使用 `/`。
- 顶层目录必须是 ASCII。
- 不得包含 `__pycache__`、`*.pyc`、`.pytest_cache`、`build`、`logs`、`exports`、`data/user_settings.json`。

## 5. 分发结论

- 外部分发只使用 `dist/GasAxisStudio_FieldTrial_v0.9.0-rc3.zip`。
- 若本轮任一自动化校验、打包校验或一致性校验失败，则不得外发。
