from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, cast

from loguru import logger

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QScroller,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    ComboBox,
    FlowLayout,
    FluentIcon,
    HorizontalSeparator,
    IconWidget,
    IndeterminateProgressBar,
    InfoBar,
    InfoBarPosition,
    Pivot,
    PrimaryPushButton,
    ProgressBar,
    PushSettingCard,
    SmoothScrollArea,
    SubtitleLabel,
    TitleLabel,
    TransparentPushButton,
    setFont,
)

from EasiAuto.consts import CACHE_DIR
from EasiAuto.core import utils
from EasiAuto.models.config import ConfigGroup, DownloadSource, UpdateMode, config
from EasiAuto.services.toast_service import ToastNotifier
from EasiAuto.services.update_service import ChangeLog, UpdateDecision, update_service
from EasiAuto.view.components import SettingCard, TagLabel
from EasiAuto.view.helpers import get_app, get_main_container, get_main_window, set_tooltip, wrap_centered


class HighlightedChangeLogCard(CardWidget):
    def __init__(self, name: str, description: str):
        super().__init__()

        self.setFixedWidth(256)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)

        name_label = SubtitleLabel(name)
        changelog_label = BodyLabel(description)
        name_label.setWordWrap(True)
        changelog_label.setWordWrap(True)

        layout.addWidget(name_label)
        layout.addSpacing(6)
        layout.addWidget(changelog_label)


class UpdateContentView(QWidget):
    def __init__(self, change_log: ChangeLog | None = None):
        super().__init__()
        layout = QVBoxLayout(self)
        # 外层 wrap_centered 已提供 24px 边距，这里保留 6px 使总边距与原先的 30px 一致
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(2)

        self.pivot = Pivot()
        self.stacked_widget = QStackedWidget()

        self.change_log_container = self._init_change_log_interface()
        self.settings_container = self._init_update_settings()

        self.addSubInterface(self.change_log_container, "changeLogContainer", "更新日志")
        self.addSubInterface(self.settings_container, "settingsContainer", "更新设置")

        # qfluentwidgets 的 PivotItem 字号高达 18，丑爆了……
        for item in self.pivot.items.values():
            setFont(item, 15)

        self.stacked_widget.currentChanged.connect(self.onCurrentIndexChanged)
        self.stacked_widget.setCurrentWidget(self.change_log_container)
        self.pivot.setCurrentItem(self.change_log_container.objectName())

        layout.addWidget(self.pivot, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.stacked_widget)

    def _init_change_log_interface(self):
        container = QWidget()

        scroll_layout = QVBoxLayout(container)

        self.description_container = QWidget()
        self.description_layout = QVBoxLayout(self.description_container)
        self.description_layout.setContentsMargins(0, 0, 0, 0)
        self.description_layout.setSpacing(4)
        self._description_rows: list[QWidget] = []

        self.highlights_title = SubtitleLabel("✨ 亮点")
        self.highlights_layout = FlowLayout()

        self.others_title = SubtitleLabel("📃 其他")
        self.others_layout = QVBoxLayout()

        self.placeholder_label = BodyLabel("暂无日志")
        self.placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder_label.setWordWrap(True)

        scroll_layout.addWidget(self.placeholder_label)
        scroll_layout.addWidget(self.description_container)
        scroll_layout.addSpacing(10)
        scroll_layout.addWidget(self.highlights_title)
        scroll_layout.addLayout(self.highlights_layout)
        scroll_layout.addSpacing(20)
        scroll_layout.addWidget(self.others_title)
        scroll_layout.addLayout(self.others_layout)
        scroll_layout.addStretch(1)

        scroll_area = SmoothScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        QScroller.grabGesture(scroll_area.viewport(), QScroller.ScrollerGestureType.LeftMouseButtonGesture)
        scroll_area.setWidget(container)

        return scroll_area

    def _attach_settings(self, layout: QVBoxLayout):
        # 手动检测延迟按钮
        download_source_card = cast(SettingCard, SettingCard.index["Update.TargetDownloadSource"])
        download_source_card.widget.setMinimumWidth(180)
        self.check_latency_button = TransparentPushButton(icon=FluentIcon.WIFI, text="检测延迟")
        set_tooltip(self.check_latency_button, "重新检测各下载源的连接延迟，并显示结果")
        self.check_latency_button.clicked.connect(update_service.test_source_latency_async)

        download_source_card.valueChanged.connect(self._handle_source_change)
        download_source_card.hBoxLayout.insertWidget(5, self.check_latency_button)
        download_source_card.hBoxLayout.insertSpacing(6, 12)

        self.download_source_combo = cast(ComboBox, download_source_card.widget)
        self._auto_source_index = download_source_card.options_index.index(DownloadSource.AUTO)
        update_service.latency_test_started.connect(self._on_latency_test_started)
        update_service.latency_test_finished.connect(self._on_latency_test_finished)
        update_service.latency_test_failed.connect(self._on_latency_test_failed)

        self._update_latency_test_ui()

        # 强制检查更新按钮
        force_check_card = PushSettingCard(
            text="强制检查",
            icon=FluentIcon.ASTERISK,
            title="强制检查更新",
            content="强制将应用更新到当前通道及分支上的最新版本，可以通过这种方式切换分支",
        )
        force_check_card.clicked.connect(lambda: update_service.check_async(force=True))
        layout.addWidget(force_check_card)

        layout.addStretch(1)

    def _init_update_settings(self):
        container = QWidget()
        scroll_layout = QVBoxLayout(container)
        scroll_layout.setSpacing(2)

        page = cast(ConfigGroup, config.load_page("UpdatePage")[0])
        for item in page.children:
            scroll_layout.addWidget(
                SettingCard.from_config_group(item)
                if isinstance(item, ConfigGroup)
                else SettingCard.from_config_item(item)
            )

        self._attach_settings(scroll_layout)

        scroll_area = SmoothScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        QScroller.grabGesture(scroll_area.viewport(), QScroller.ScrollerGestureType.LeftMouseButtonGesture)
        scroll_area.setWidget(container)

        return scroll_area

    def _clear_description_rows(self):
        """清空描述区（每行为版本 TagLabel + 描述 BodyLabel）"""
        for row in self._description_rows:
            row.deleteLater()
        self._description_rows.clear()

    def _build_description_row(self, line: str) -> QWidget | None:
        """把一行 "[版本] 描述" 渲染为版本 TagLabel + 描述 BodyLabel，空行返回 None"""
        line = line.strip()
        if not line:
            return None

        version, text = None, line
        if line.startswith("[") and "]" in line:
            end = line.find("]")
            if candidate := line[1:end].strip():
                version, text = candidate, line[end + 1 :].strip()

        row = QWidget()
        h_layout = QHBoxLayout(row)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(8)
        if version:
            h_layout.addWidget(TagLabel(version), 0, Qt.AlignmentFlag.AlignTop)
        if text:
            label = BodyLabel(text)
            label.setWordWrap(True)
            h_layout.addWidget(label, 1)
        return row

    def set_change_log(self, change_log: ChangeLog | None):
        """允许初始化后传入/更新 changelog"""
        self._clear_description_rows()
        self.highlights_layout.takeAllWidgets()
        while self.others_layout.count():
            w = self.others_layout.takeAt(0).widget()  # type: ignore
            if w:
                w.deleteLater()

        self.placeholder_label.setVisible(not bool(change_log))
        self.description_container.setVisible(bool(getattr(change_log, "description", None)))
        self.highlights_title.setVisible(bool(getattr(change_log, "highlights", None)))
        self.others_title.setVisible(bool(getattr(change_log, "others", None)))

        if not change_log:
            return

        try:
            for line in change_log.description.splitlines():
                if row := self._build_description_row(line):
                    self.description_layout.addWidget(row)
                    self._description_rows.append(row)

            for item in change_log.highlights:
                card = HighlightedChangeLogCard(item["name"], item["description"])
                self.highlights_layout.addWidget(card)

            for desc in change_log.others:
                label = BodyLabel(f"• {desc}")
                label.setWordWrap(True)
                self.others_layout.addWidget(label)
        except Exception as e:
            logger.warning(f"显示更新日志时发生错误: {e}")
            self._clear_description_rows()
            self.description_container.setVisible(False)
            self.placeholder_label.setVisible(True)
            self.highlights_title.setVisible(False)
            self.others_title.setVisible(False)

    def addSubInterface(self, widget: QWidget, object_name: str, text: str):
        widget.setObjectName(object_name)

        self.stacked_widget.addWidget(widget)
        self.pivot.addItem(
            routeKey=object_name,
            text=text,
            onClick=lambda: self.stacked_widget.setCurrentWidget(widget),
        )

    def onCurrentIndexChanged(self, index):
        widget = self.stacked_widget.widget(index)
        self.pivot.setCurrentItem(widget.objectName())  # type: ignore

    def _handle_source_change(self, value):
        self._update_latency_test_ui()
        if value == DownloadSource.AUTO:
            update_service.init_latency()

    def _on_latency_test_started(self):
        self._update_latency_test_ui()

    def _on_latency_test_finished(self, result: dict[DownloadSource, float | None] | None, manual: bool):
        self._update_latency_test_ui()
        if not manual or not result:
            return

        selected = update_service.auto_selected_source
        lines = []
        for source, latency in result.items():
            if latency is None:
                lines.append(f"{source.display_name}: 连接失败")
            else:
                lines.append(f"{source.display_name}: {latency * 1000:.0f} ms{' (当前)' if source == selected else ''}")

        InfoBar.info(
            title="镜像源延迟测试",
            content="\n".join(lines),
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=get_main_container(),
        )

    def _on_latency_test_failed(self, error: str, manual: bool):
        self._update_latency_test_ui("检测失败")
        if not manual:
            return
        InfoBar.error(
            title="镜像源延迟测试失败",
            content=error,
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=get_main_container(),
        )

    def _update_latency_test_ui(self, detail: str | None = None):
        if config.Update.TargetDownloadSource != DownloadSource.AUTO:
            self.check_latency_button.setDisabled(True)
            return

        self.check_latency_button.setDisabled(update_service._latency_probe_running)
        text = DownloadSource.AUTO.display_name
        if detail is None:
            if update_service._latency_probe_running:
                detail = "检测中"
            elif (selected := update_service.auto_selected_source) is not None:
                detail = selected.display_name
        self.download_source_combo.setItemText(self._auto_source_index, f"{text} ({detail})" if detail else text)


class UpdateStatus(Enum):
    FAILED = "failed"
    CHECK = "check"
    CHECKING = "checking"
    DOWNLOAD = "download"
    DOWNLOADING = "downloading"
    DOWNLOAD_CANCELED = "downloadCanceled"
    INSTALL = "install"


@dataclass(kw_only=True)
class StateConfig:
    title: Callable[[UpdatePage], str]
    detail: Callable[[UpdatePage], str] | None = None
    button_text: str
    button_enabled: bool = True
    progress: Literal["none", "indeterminate", "determinate"] = "none"


UPDATE_STATUS_MAP: dict[UpdateStatus, StateConfig] = {
    UpdateStatus.CHECK: StateConfig(
        title=lambda _: "你使用的是最新版本",
        detail=lambda s: f"上次检查时间：{s._last_check or '暂未检查'}",
        button_text="检查更新",
    ),
    UpdateStatus.CHECKING: StateConfig(
        title=lambda _: "正在检查更新……",
        button_text="检查更新",
        button_enabled=False,
        progress="indeterminate",
    ),
    UpdateStatus.DOWNLOAD: StateConfig(
        title=lambda s: (
            f"更新可用：{s._decision.target_version}"  # type: ignore
            if not s._decision.confirm_required  # type: ignore
            else f"需要确认的更新：{s._decision.target_version}"  # type: ignore
        ),
        detail=lambda s: f"上次检查时间：{s._last_check or '暂未检查'}",
        button_text="下载",
    ),
    UpdateStatus.DOWNLOADING: StateConfig(
        title=lambda _: "正在下载更新……",
        button_text="取消",
        progress="determinate",
    ),
    UpdateStatus.DOWNLOAD_CANCELED: StateConfig(
        title=lambda s: (
            f"更新可用：{s._decision.target_version}"  # type: ignore
            if not s._decision.confirm_required  # type: ignore
            else f"需要确认的更新：{s._decision.target_version}"  # type: ignore
        ),
        detail=lambda s: (
            f"上次检查时间：{s._last_check or '暂未检查'}"
            if s._tried_downloads < 2
            else "若多次尝试后仍下载缓慢或无法下载，可在设置中手动切换镜像下载源"
        ),
        button_text="下载",
    ),
    UpdateStatus.INSTALL: StateConfig(
        title=lambda _: "更新已就绪",
        detail=lambda _: (
            "应用退出后将自动应用更新，或者你也可以现在重启以应用更新"
            if config.Update.Mode >= UpdateMode.CHECK_AND_INSTALL
            else "需要手动确认以应用更新"
        ),
        button_text="重启并应用更新",
        progress="none",
    ),
    UpdateStatus.FAILED: StateConfig(
        title=lambda _: "发生错误",
        detail=lambda s: f"错误信息：{s._last_error}" if s._last_error else "未知错误，请重试或向开发者报告问题",
        button_text="重试",
        progress="none",
    ),
}


class UpdatePage(QWidget):
    def __init__(self):
        super().__init__()
        logger.debug("初始化更新页")
        self.setObjectName("UpdatePage")
        self.setStyleSheet("border: none; background-color: transparent;")

        update_service.check_started.connect(self.check_started)
        update_service.check_finished.connect(self.check_finished)
        update_service.check_failed.connect(self.check_failed)

        update_service.download_started.connect(self.download_started)
        update_service.download_progress.connect(self.download_progress)
        update_service.download_finished.connect(self.download_finished)
        update_service.download_failed.connect(self.download_failed)

        self._action: UpdateStatus
        self._decision: UpdateDecision | None = None
        self._update_file: str = "EasiAuto_Unknown.zip"
        self._last_error: str | None = None
        self._signal_connected: bool = False
        self._tried_downloads: int = 0
        self._first_show: bool = True

        self.init_ui()
        self.action = UpdateStatus.CHECK

    def showEvent(self, event):
        super().showEvent(event)
        if self._first_show and config.Update.Mode > UpdateMode.NEVER:
            self._first_show = False
            update_service.check_async()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = TitleLabel("更新")
        title.setContentsMargins(36, 8, 0, 12)
        layout.addWidget(title)

        status_widget = QWidget()
        status_widget.setFixedHeight(96)
        status_widget.setContentsMargins(12, 0, 12, 0)
        status_layout = QHBoxLayout(status_widget)

        icon = IconWidget(FluentIcon.SYNC)
        icon.setFixedSize(48, 48)
        text_layout = QVBoxLayout()
        text_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.title = SubtitleLabel()
        font = self.title.font()
        font.setPixelSize(24)
        self.title.setFont(font)
        self.detail = BodyLabel()
        self.indeterminate_progress_bar = IndeterminateProgressBar()
        self.indeterminate_progress_bar.hide()
        self.progress_bar = ProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.hide()
        self.action_button = PrimaryPushButton()
        self.action_button.clicked.connect(self.handle_button_action)

        status_layout.addWidget(icon)
        status_layout.addSpacing(8)
        text_layout.addWidget(self.title)
        text_layout.addSpacing(3)
        text_layout.addWidget(self.detail)
        text_layout.addWidget(self.indeterminate_progress_bar)
        text_layout.addWidget(self.progress_bar)
        status_layout.addLayout(text_layout, 1)
        status_layout.addWidget(self.action_button, alignment=Qt.AlignmentFlag.AlignRight)

        self.content_widget = UpdateContentView()

        # 整个页面内容（状态栏、分隔线、内容区）统一包裹为居中、限宽的列
        page_content = QWidget()
        page_layout = QVBoxLayout(page_content)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(status_widget)
        page_layout.addWidget(HorizontalSeparator())
        page_layout.addWidget(self.content_widget)

        layout.addWidget(wrap_centered(page_content))

    @property
    def _last_check(self) -> str | None:
        """从配置读取上次检查时间（返回格式化字符串）"""
        val = config.Internal.LastUpdateCheckTime
        if val is None:
            return None
        return val.strftime("%Y-%m-%d %H:%M:%S")

    @_last_check.setter
    def _last_check(self, value: datetime | None) -> None:
        """设置上次检查时间并持久化到配置"""
        config.Internal.LastUpdateCheckTime = value

    @property
    def action(self) -> UpdateStatus:
        return self._action

    @action.setter
    def action(self, new: UpdateStatus):
        """更新状态管理"""
        self._action = new

        # 内部逻辑处理
        match new:
            case UpdateStatus.CHECK:
                self.content_widget.set_change_log(None)
            case UpdateStatus.DOWNLOAD:
                if not self._decision:
                    self._last_error = "无可用更新"
                    self.action = UpdateStatus.FAILED
                    return
                logger.info(
                    f"更新可用：{self._decision.target_version}"
                    if not self._decision.confirm_required
                    else f"需要确认的更新：{self._decision.target_version}"
                )
                handle = ToastNotifier(self).show(
                    "更新可用" if not self._decision.confirm_required else "存在需要确认的更新",
                    f"新版本：{self._decision.target_version}",
                    buttons=[
                        {"content": "查看详情", "arguments": "open", "activationType": "protocol"},
                        {"content": "忽略", "arguments": "dismiss", "activationType": "protocol"},
                    ],
                )
                handle.activated.connect(self._on_toast_activated)
                self.content_widget.set_change_log(self._decision.change_log)
                if config.Update.Mode >= UpdateMode.CHECK_AND_DOWNLOAD and not self._decision.confirm_required:
                    update_service.download_async(self._decision.downloads[0], filename=self._update_file)
                    # 状态在 download_started() 中通过事件响应更新
            case UpdateStatus.DOWNLOADING:
                logger.info("正在下载更新")
            case UpdateStatus.DOWNLOAD_CANCELED:
                if not self._decision:
                    self._last_error = "无可用更新"
                    self.action = UpdateStatus.FAILED
                    return
            case UpdateStatus.INSTALL:
                logger.success("更新已就绪")
                if config.Update.Mode >= UpdateMode.CHECK_AND_INSTALL:
                    get_app().aboutToQuit.connect(
                        lambda: update_service.apply_script(zip_path=CACHE_DIR / self._update_file),
                    )
                    self._signal_connected = True

            case UpdateStatus.FAILED:
                logger.error("检查更新时发生错误")
                # 清除错误已延后至UI更新后

        # 界面更新
        self.update_ui(UPDATE_STATUS_MAP[new])

        # 其他内部逻辑处理
        if new == UpdateStatus.FAILED and self._last_error:
            self._last_error = None

    def _on_toast_activated(self, args: dict) -> None:
        if args.get("arguments") == "open":
            # 唤起窗口
            window = get_main_window()
            window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
            window.show()
            window.raise_()
            window.activateWindow()
            # 切换到更新页
            window.switchTo(self)

    def update_ui(self, cfg: StateConfig):
        """使用状态数据更新界面"""
        self.title.setText(cfg.title(self))

        if detail_visible := (cfg.detail is not None):
            self.detail.setText(cfg.detail(self))
        self.detail.setVisible(detail_visible)

        self.action_button.setText(cfg.button_text)
        self.action_button.setEnabled(cfg.button_enabled)

        self.indeterminate_progress_bar.setVisible(cfg.progress == "indeterminate")
        self.progress_bar.setVisible(cfg.progress == "determinate")

    def handle_button_action(self):
        """响应更新各步骤的操作（按钮点击）"""
        match self.action:
            case UpdateStatus.CHECK | UpdateStatus.FAILED:
                update_service.check_async()
            case UpdateStatus.DOWNLOAD | UpdateStatus.DOWNLOAD_CANCELED:
                if not self._decision:
                    self._last_error = "无可用更新"
                    self.action = UpdateStatus.FAILED
                    return
                update_service.download_async(self._decision.downloads[0], filename=self._update_file)
            case UpdateStatus.DOWNLOADING:  # 取消下载
                update_service.cancel_download()
            case UpdateStatus.INSTALL:
                if not self._signal_connected:
                    get_app().aboutToQuit.connect(
                        lambda: update_service.apply_script(zip_path=CACHE_DIR / self._update_file, reopen=True),
                    )
                utils.stop()

    def check_started(self):
        self.action = UpdateStatus.CHECKING

    def check_finished(self, decision: UpdateDecision):
        self._last_check = datetime.now()

        if decision.available and len(decision.downloads) > 0:
            self._decision = decision
            self._update_file = f"EasiAuto_{decision.target_version or 'Unknown'}.zip"
            self.action = UpdateStatus.DOWNLOAD
        else:
            self.action = UpdateStatus.CHECK

    def check_failed(self, error: str):
        self._last_error = error
        self.action = UpdateStatus.FAILED

    def download_started(self):
        self.action = UpdateStatus.DOWNLOADING

    def download_progress(self, downloaded, total):
        if total > 0:
            self.progress_bar.setValue(round(100 * downloaded / total))
        else:
            self.progress_bar.hide()
            self.indeterminate_progress_bar.show()

    def download_finished(self):
        self.action = UpdateStatus.INSTALL

    def download_failed(self, error):
        if "取消" in error:
            self.progress_bar.setValue(0)
            self.action = UpdateStatus.DOWNLOAD_CANCELED
        else:
            self._last_error = error
            self.action = UpdateStatus.FAILED
