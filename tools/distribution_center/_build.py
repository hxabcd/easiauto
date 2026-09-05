import os
import subprocess
import time

from windows11toast import toast

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    InfoBar,
    PrimaryPushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TextEdit,
)


class BuildThread(QThread):
    log_signal = Signal(str)
    finished_signal = Signal(bool, float)

    def run(self):
        start_time = time.time()
        self.log_signal.emit("\n🚀 Starting build...\n")
        cmd = ["uv", "run", "python", "tools/build.py"]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    self.log_signal.emit(line.rstrip())

            if process.returncode != 0:
                self.log_signal.emit(f"❌ Build failed with code {process.returncode}")
                self.finished_signal.emit(False, 0.0)
                return

            self.log_signal.emit("✅ Build completed successfully.")

        except Exception as e:
            self.log_signal.emit(f"❌ Error executing build: {str(e)}")
            self.finished_signal.emit(False, 0.0)
            return

        end_time = time.time()
        self.finished_signal.emit(True, end_time - start_time)


class BuildWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BuildWidget")
        self.build_thread = None
        self.pending_callback = None
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        root.addWidget(SubtitleLabel("构建", self))

        # Build button
        self.build_btn = PrimaryPushButton("开始构建", self)
        self.build_btn.setIcon(FluentIcon.PLAY)
        self.build_btn.setFixedWidth(200)
        self.build_btn.clicked.connect(self.start_build)
        root.addWidget(self.build_btn)

        # Log area
        root.addWidget(StrongBodyLabel("构建日志:", self))
        self.log_view = TextEdit(self)
        self.log_view.setFontFamily("Consolas")
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("等待构建...")
        root.addWidget(self.log_view, 1)

    def start_build(self, on_success=None):
        self.pending_callback = on_success
        self.build_btn.setEnabled(False)
        self.log_view.clear()

        self.build_thread = BuildThread()
        self.build_thread.log_signal.connect(self._append_log)
        self.build_thread.finished_signal.connect(self._on_build_finished)
        self.build_thread.start()
        self.build_btn.setText("构建中...")

    def _append_log(self, text):
        self.log_view.append(text)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_build_finished(self, success, duration):
        self.build_btn.setEnabled(True)
        self.build_btn.setText("开始构建")

        if success:
            time_str = f"{duration:.2f}s"
            if duration > 60:
                m, s = divmod(duration, 60)
                time_str = f"{int(m)}m {s:.2f}s"
            msg = f"构建全部完成，用时: {time_str}"
            InfoBar.success("成功", msg, parent=self)
            toast("EasiAuto Build", msg)
        else:
            InfoBar.error("失败", "构建过程中发生错误", parent=self)
            toast("EasiAuto Build", "❌ 构建失败")
            self.pending_callback = None

        if success and self.pending_callback:
            callback = self.pending_callback
            self.pending_callback = None
            callback()


class BuildManager:
    """Non-UI handler for triggering builds programmatically (used by release flow)."""

    def __init__(self):
        self.build_thread = None
        self.pending_callback = None

    def start_build(self, on_success=None):
        self.pending_callback = on_success
        self.build_thread = BuildThread()
        self.build_thread.finished_signal.connect(self._on_finished)
        self.build_thread.start()

    def _on_finished(self, success, _duration):
        if success and self.pending_callback:
            cb = self.pending_callback
            self.pending_callback = None
            cb()
