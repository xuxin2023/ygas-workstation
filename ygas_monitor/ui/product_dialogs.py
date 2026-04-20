"""Product-facing dialogs for help and about content."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import config as app_config
from ..version import APP_VERSION, BUILD_DATE, get_version_info

APP_NAME = getattr(app_config, "APP_NAME", "GasAxis Studio")
if APP_NAME == "YGAS Analyzer Monitor":
    APP_NAME = "GasAxis Studio"
APP_SUBTITLE_ZH = getattr(app_config, "APP_SUBTITLE_ZH", "气体分析仪监测与联调平台")
APP_SUBTITLE_EN = getattr(app_config, "APP_SUBTITLE_EN", "Gas Analyzer Monitoring & Commissioning Platform")
ASSET_DIR = app_config.ASSET_DIR
LOG_DIR = app_config.LOG_DIR
SETTINGS_DIR = app_config.SETTINGS_DIR

HELP_DOC_PATH = ASSET_DIR / "help_zh_cn.md"
HELP_FALLBACK_MARKDOWN = """# {{APP_NAME}}

{{APP_SUBTITLE_ZH}}

{{APP_SUBTITLE_EN}}

## 运行信息

- 版本：{{APP_VERSION}}
- 构建日期：{{BUILD_DATE}}
- 配置目录：`{{SETTINGS_DIR}}`
- 日志目录：`{{LOG_DIR}}`

帮助文档缺失时，将显示此内置说明页。
"""


def _load_help_markdown(path: Path | None = None) -> str:
    help_path = path or HELP_DOC_PATH
    if help_path.exists():
        base_markdown = help_path.read_text(encoding="utf-8")
    else:
        base_markdown = HELP_FALLBACK_MARKDOWN

    version_info = get_version_info()
    replacements = {
        "{{APP_NAME}}": APP_NAME,
        "{{APP_SUBTITLE_ZH}}": APP_SUBTITLE_ZH,
        "{{APP_SUBTITLE_EN}}": APP_SUBTITLE_EN,
        "{{APP_VERSION}}": APP_VERSION,
        "{{BUILD_DATE}}": BUILD_DATE,
        "{{SETTINGS_DIR}}": str(SETTINGS_DIR),
        "{{LOG_DIR}}": str(LOG_DIR),
        "{{PYTHON_VERSION}}": str(version_info["python"]),
        "{{PLATFORM}}": str(version_info["platform"]),
    }
    rendered = base_markdown
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered


class HelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"软件说明 - {APP_NAME}")
        self.resize(920, 760)

        layout = QVBoxLayout(self)
        title = QLabel(APP_NAME)
        title.setProperty("accent", True)
        subtitle = QLabel(f"{APP_SUBTITLE_ZH}\n{APP_SUBTITLE_EN}")
        subtitle.setWordWrap(True)
        subtitle.setProperty("muted", True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setMarkdown(_load_help_markdown())
        layout.addWidget(self.browser, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class AboutDialog(QDialog):
    def __init__(self, *, open_help_callback, parent: QWidget | None = None):
        super().__init__(parent)
        self._open_help_callback = open_help_callback
        self.setWindowTitle(f"关于 - {APP_NAME}")
        self.resize(640, 460)

        layout = QVBoxLayout(self)

        title = QLabel(APP_NAME)
        title.setProperty("accent", True)
        subtitle_zh = QLabel(APP_SUBTITLE_ZH)
        subtitle_en = QLabel(APP_SUBTITLE_EN)
        subtitle_en.setProperty("muted", True)
        layout.addWidget(title)
        layout.addWidget(subtitle_zh)
        layout.addWidget(subtitle_en)

        summary = QTextBrowser()
        summary.setOpenExternalLinks(False)
        summary.setMarkdown(
            "\n".join(
                [
                    f"**版本号**：{APP_VERSION}",
                    "",
                    "**支持能力概览**",
                    "- 实时监测总览与双图槽趋势查看",
                    "- 连接、联调、命令执行与安全权限控制",
                    "- 诊断信息收集、会话导出与数据回放",
                    "- MODE1 / MODE2、自动上传与原始帧辅助诊断",
                    "",
                    "**运行目录**",
                    f"- 配置目录：`{SETTINGS_DIR}`",
                    f"- 日志目录：`{LOG_DIR}`",
                ]
            )
        )
        layout.addWidget(summary, 1)

        buttons_row = QHBoxLayout()
        help_button = QPushButton("软件说明")
        help_button.clicked.connect(self._open_help)
        buttons_row.addWidget(help_button)

        open_settings_button = QPushButton("打开配置目录")
        open_settings_button.clicked.connect(lambda: self._open_path(SETTINGS_DIR))
        buttons_row.addWidget(open_settings_button)

        open_logs_button = QPushButton("打开日志目录")
        open_logs_button.clicked.connect(lambda: self._open_path(LOG_DIR))
        buttons_row.addWidget(open_logs_button)

        buttons_row.addStretch(1)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        buttons_row.addWidget(close_button)
        layout.addLayout(buttons_row)

    def _open_help(self) -> None:
        self._open_help_callback()

    @staticmethod
    def _open_path(path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
