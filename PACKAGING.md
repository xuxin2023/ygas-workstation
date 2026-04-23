# GasAxis Studio Packaging Notes

## 发布定位

- 产品名：`GasAxis Studio`
- 内部兼容名：`YGasWorkstation`
- Source 交付包：`dist/GasAxisStudio_Source_v<APP_VERSION>.zip`
- FieldTrial 交付包：`dist/GasAxisStudio_FieldTrial_v<APP_VERSION>.zip`
- 外部校验文件：`dist/GasAxisStudio_FieldTrial_v<APP_VERSION>.zip.sha256`

## 打包命令

```powershell
.\package_source_zip.ps1
.\package_fieldtrial_zip.ps1
python .\check_release_consistency.py --workspace
python .\check_release_consistency.py --source-only
.\clean_review_snapshot.ps1
```

## Source 包规则

- 只打包当前工作区源码与当前版本资料。
- 默认排除：`SHA256SUMS.txt`、`dist/`、`build/`、`logs/`、`exports/`、`review_snapshot/`、`__pycache__/`、`*.pyc`、`.pytest_cache/`、`data/user_settings.json`。
- `docs/archive/` 下的历史资料不进入当前 Source 包。
- zip entry 必须统一使用 `/`。

## FieldTrial 包规则

- 顶层目录必须为 ASCII：`GasAxisStudio_FieldTrial_v<APP_VERSION>/`
- 只包含当前版本 Source zip 与当前版本现场资料。
- FieldTrial 顶层必须包含 `SHA256SUMS.txt`。
- 外部分发时只使用 `dist/GasAxisStudio_FieldTrial_v<APP_VERSION>.zip`。

## 校验要求

- `python check_release_consistency.py --workspace`：用于工作区发布前检查，会校验 `dist/`、FieldTrial zip、`docs/archive/` 和 `SHA256SUMS.txt`。
- `python check_release_consistency.py --source-only`：用于单独解压的 Source zip，自检源码版本、当前版本文档和禁带项；不要求 `dist/`、`docs/archive/` 或 FieldTrial zip。
- Source zip 内不得包含 `SHA256SUMS.txt`。
- FieldTrial 顶层 `SHA256SUMS.txt` 必须与包内实际文件逐项匹配。
- FieldTrial 顶层 `SHA256SUMS.txt` 中的 Source zip SHA 必须等于实际嵌入 Source zip 的 SHA256。
- FieldTrial zip 不得包含历史归档资料、旧版 Source zip 或缓存/构建垃圾文件。

## Review Snapshot

- `clean_review_snapshot.ps1` 会清理 `__pycache__/`、`*.pyc`、`.pytest_cache/`、`build/`、`logs/`、`exports/`，并生成 ASCII 顶层目录的评审快照。
- review snapshot 顶层目录固定为 `GasAxisStudio_Workspace_v<APP_VERSION>/`。
- review snapshot 只保留当前版本正式资料；旧版现场资料必须放入 `docs/archive/<version>/`。
