from __future__ import annotations

import contextlib
import hashlib
import shutil
import socket
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import requests
from loguru import logger
from packaging.version import Version

from PySide6.QtCore import QObject, QThread, Signal, Slot

from EasiAuto import __version__
from EasiAuto.consts import CACHE_DIR, EA_BASEDIR, EA_EXECUTABLE, IS_DEV
from EasiAuto.models.config import DownloadSource, UpdateChannel, config

HEADERS = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}

MANIFEST_TIMEOUT = (3, 5)  # connect, read
DOWNLOAD_TIMEOUT = (8, 60)  # connect, read
LATENCY_TIMEOUT = (2, 3)  # connect, read

MANIFEST_URLS = [
    "https://easiauto.0xabcd.dev/update.json",
    "https://raw.githubusercontent.com/hxabcd/EasiAutoWeb/main/public/update.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/hxabcd/EasiAutoWeb/main/public/update.json",
    "https://ghfast.top/https://raw.githubusercontent.com/hxabcd/EasiAutoWeb/main/public/update.json",
]


DOWNLOAD_SOURCES: dict[DownloadSource, str] = {
    DownloadSource.GITHUB: "https://github.com",
    DownloadSource.GHPROXY: "https://ghproxy.net/https://github.com",
    DownloadSource.GHFAST: "https://ghfast.top/https://github.com",
}


@dataclass(frozen=True)
class DownloadItem:
    url: str
    sha256: str | None


@dataclass
class ChangeLog:
    description: str
    highlights: list[dict[Literal["name", "description"], str]]
    others: list[str]


@dataclass(frozen=True)
class UpdateDecision:
    available: bool
    target_version: str | None
    confirm_required: bool
    change_log: ChangeLog | None
    downloads: tuple[DownloadItem, ...]


class UpdateError(RuntimeError):
    pass


class DownloadCancelled(UpdateError):
    pass


# -------------------- 独立 Worker 类 --------------------


class CheckWorker(QObject):
    """执行检查更新的子线程 Worker"""

    finished = Signal(object)  # UpdateDecision
    failed = Signal(str)

    def __init__(self, checker: UpdateService, force: bool):
        super().__init__()
        self._checker = checker
        self._force = force

    @Slot()
    def run(self):
        try:
            # 在子线程执行耗时的同步网络请求
            result = self._checker.check(self._force)
            self.finished.emit(result)
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
            self.failed.emit(str(e))


class DownloadWorker(QObject):
    """执行下载更新的子线程 Worker"""

    started = Signal(str)  # url
    progress = Signal(int, int)  # done, total
    finished = Signal(str)  # file path
    failed = Signal(str)

    def __init__(
        self,
        checker: UpdateService,
        item: DownloadItem,
        filename: str | None,
        chunk_size: int,
    ):
        super().__init__()
        self._checker = checker
        self.item = item
        self.filename = filename
        self.chunk_size = chunk_size

    @Slot()
    def run(self):
        full_url = self._checker.resolve_download_url(self.item.url, allow_latency_check=False)
        self.started.emit(full_url)
        try:
            # 定义回调函数将进度转发给信号
            def _on_progress(done: int, total: int):
                self.progress.emit(done, total)

            # 执行同步下载
            path = self._checker.download_update(
                self.item,
                filename=self.filename,
                chunk_size=self.chunk_size,
                on_progress=_on_progress,
                cancel_checker=self._checker._is_download_cancelled,
                resolved_url=full_url,
            )
            self.finished.emit(str(path))
        except DownloadCancelled as e:
            logger.info("下载任务已取消")
            self.failed.emit(str(e))
        except Exception as e:
            logger.error(f"下载更新失败: {e}")
            self.failed.emit(str(e))


class LatencyWorker(QObject):
    """执行延迟检测的 Worker"""

    finished = Signal(object)  # dict[DownloadSource, float | None] | None
    failed = Signal(str)

    def __init__(self, checker: UpdateService):
        super().__init__()
        self._checker = checker

    @Slot()
    def run(self):
        try:
            result = self._checker.test_source_latency()
            self.finished.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


# -------------------- 主逻辑类 --------------------


class UpdateService(QObject):
    # ================== 外部信号 ==================
    # 异步检查信号
    check_started = Signal()
    check_finished = Signal(UpdateDecision)
    check_failed = Signal(str)

    # 异步下载信号
    download_started = Signal(str)  # url
    download_progress = Signal(int, int)  # bytes_done, total_bytes(-1未知)
    download_finished = Signal(str)  # zip 文件路径
    download_failed = Signal(str)
    latency_test_started = Signal()
    latency_test_finished = Signal(object, bool)  # dict[DownloadSource, float | None], manual
    latency_test_failed = Signal(str, bool)  # error, manual

    def __init__(
        self,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = requests.Session()

        self.auto_selected_source: DownloadSource | None = None

        # 线程管理
        self._threads: list[QThread] = []
        self._thread_counter = 0
        self._cancel_download_flag: bool = False
        self._active_response: requests.Response | None = None
        self._update_script_path: Path | None = None
        self._script_reopen: bool = False
        self._latency_probe_running = False
        self._shutting_down = False

    # ================== 同步 API ==================

    def check(self, force: bool = False) -> UpdateDecision:
        """检查更新"""
        manifest = self._fetch_manifest()
        return self._decide(manifest, force)

    def download_update(
        self,
        item: DownloadItem,
        *,
        filename: str | None = None,
        chunk_size: int = 1024 * 1024,
        on_progress: Callable[[int, int], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        resolved_url: str | None = None,
        allow_latency_check: bool = False,
    ) -> Path:
        """下载更新"""
        dest_dir = CACHE_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / (filename or Path(item.url).name)

        # 1. 检查本地是否存在
        if out_path.exists() and item.sha256 and self._check_sha256(out_path, item.sha256):
            logger.info(f"文件已存在且校验通过, 跳过下载: {out_path}")
            if on_progress:
                # 模拟进度完成
                size = out_path.stat().st_size
                on_progress(size, size)
            return out_path

        # 2. 准备下载
        url = resolved_url or self.resolve_download_url(item.url, allow_latency_check=allow_latency_check)
        total = -1
        done = 0
        logger.info(f"开始下载更新包: {url}")

        self._download_to_file(
            url=url,
            out_path=out_path,
            chunk_size=chunk_size,
            on_progress=on_progress,
            cancel_checker=cancel_checker,
            done=done,
            total=total,
        )

        # 3. 校验
        if item.sha256:
            self._verify_sha256(out_path, item.sha256)
            logger.success(f"下载完成并校验通过: {out_path}")
        else:
            logger.success(f"下载完成(无校验): {out_path}")

        return out_path

    def resolve_download_url(self, raw_url: str, *, allow_latency_check: bool = False) -> str:
        """应用镜像源"""
        selected_source = config.Update.TargetDownloadSource
        if selected_source == DownloadSource.AUTO:
            if not allow_latency_check:
                if self.auto_selected_source is None:
                    return raw_url
                selected_source = self.auto_selected_source
            else:
                selected_source = self._auto_select_source()
        mirror = DOWNLOAD_SOURCES.get(selected_source, "https://github.com")

        return raw_url.replace("https://github.com", mirror, 1)

    def test_source_latency(self) -> dict[DownloadSource, float | None]:
        candidates = (DownloadSource.GITHUB, DownloadSource.GHPROXY, DownloadSource.GHFAST)
        result: dict[DownloadSource, float | None] = dict.fromkeys(candidates)
        available: list[tuple[DownloadSource, float]] = []

        for source in candidates:
            try:
                latency = self._probe_source_latency(source)
            except Exception:
                latency = None
            result[source] = latency
            if latency is not None:
                available.append((source, latency))

        if available:
            self.auto_selected_source = min(available, key=lambda x: x[1])[0]
            logger.success(f"成功检测下载源延迟, 已选中 {self.auto_selected_source.display_name}")
        else:
            self.auto_selected_source = DownloadSource.GITHUB
            logger.warning(f"无法检测下载源延迟, 回退至 {self.auto_selected_source.display_name}")

        return result

    def init_latency(self) -> None:
        """预测试延迟"""
        self._ensure_auto_selected_source(is_init=True)

    def test_source_latency_async(self) -> None:
        """异步检测延迟，避免阻塞 UI 线程"""
        worker = LatencyWorker(self)
        self._set_latency_probe_running(True)

        def _connect_signals(thread: QThread) -> None:
            thread.started.connect(self.latency_test_started)
            thread.started.connect(worker.run)
            worker.finished.connect(lambda result: self.latency_test_finished.emit(result, True))
            worker.failed.connect(lambda error: self.latency_test_failed.emit(error, True))
            worker.finished.connect(lambda _=None: self._set_latency_probe_running(False))
            worker.failed.connect(lambda _=None: self._set_latency_probe_running(False))
            worker.finished.connect(thread.quit)
            worker.failed.connect(thread.quit)
            thread.finished.connect(lambda _=None: self._set_latency_probe_running(False))

        self._start_worker_thread(worker, connect_signals=_connect_signals)

    def create_update_script(self, zip_path: Path, reopen: bool = True) -> Path:
        """解压更新包并生成批处理脚本"""
        staging = Path(tempfile.mkdtemp(prefix="EasiAuto_update_"))
        extract_dir = staging / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"解压更新包到: {extract_dir}")
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(extract_dir)
        except zipfile.BadZipFile as e:
            raise UpdateError("解压失败, 文件可能已损坏") from e

        extract_root = self._normalize_extract_root(extract_dir)

        target_dir = str(EA_BASEDIR)
        script = staging / "apply_update.bat"

        # 构建更新脚本：删除旧文件 -> 复制新文件 -> 重启
        # 保留 data 目录
        script_content = [
            "@echo off",
            "chcp 65001",
            "setlocal enabledelayedexpansion",
            "timeout /t 1 /nobreak",  # 等待主程序完全退出
            "",
            f"set TARGET_DIR={target_dir}",
            "",
            # 使用 PowerShell 删除除了保留列表外的文件
            (
                r'powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "'
                r"$t = '%TARGET_DIR%'; "
                r"$keep = @('data'); "
                r"Get-ChildItem -LiteralPath $t -Force | Where-Object { $keep -notcontains $_.Name } | "
                r'Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"'
            ),
            "",
            f'robocopy "{extract_root}" "%TARGET_DIR%" /E /MOV',
            f'"{EA_EXECUTABLE}"' if reopen else "",
            "endlocal",
        ]

        script.write_text("\r\n".join(script_content), encoding="utf-8")
        logger.success(f"已生成更新脚本: {script} ")

        self._update_script_path = script
        return script.resolve()

    def apply_script(self, zip_path: Path, reopen: bool = False) -> None:
        """执行更新脚本（通常此时应退出主程序）"""

        if IS_DEV:
            logger.warning("检测到开发环境, 为防止删除源代码, 已禁止执行更新脚本")  # 为什么会有这个防护，好难猜啊
            return

        if self._update_script_path and self._script_reopen == reopen:
            path = self._update_script_path
        else:
            self._script_reopen = reopen
            path = self.create_update_script(zip_path, reopen=reopen)

        subprocess.Popen(
            str(path),
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    # ================== 异步 API ==================

    def check_async(self, force: bool = False) -> None:
        worker = CheckWorker(self, force)

        def _connect_signals(thread: QThread) -> None:
            # 信号转发：Worker -> Self (直接连接)
            worker.finished.connect(self.check_finished)
            worker.failed.connect(self.check_failed)

            # 生命周期管理
            thread.started.connect(self.check_started)
            thread.started.connect(worker.run)
            worker.finished.connect(thread.quit)
            worker.failed.connect(thread.quit)

        self._start_worker_thread(worker, connect_signals=_connect_signals)

    def download_async(
        self,
        item: DownloadItem,
        *,
        filename: str,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        """启动异步下载线程"""
        self._cancel_download_flag = False

        worker = DownloadWorker(
            self,
            item,
            filename,
            chunk_size,
        )

        def _connect_signals(thread: QThread) -> None:
            # 信号转发
            worker.started.connect(self.download_started)
            worker.progress.connect(self.download_progress)
            worker.finished.connect(self.download_finished)
            worker.failed.connect(self.download_failed)

            # 生命周期管理
            thread.started.connect(worker.run)
            worker.finished.connect(thread.quit)
            worker.failed.connect(thread.quit)

        self._start_worker_thread(worker, connect_signals=_connect_signals)

    def cancel_download(self) -> None:
        """取消当前下载"""
        self._cancel_download_flag = True
        # 强制关闭连接，使 requests 立即抛出异常，不用等待 chunk 读取
        if self._active_response:
            with contextlib.suppress(Exception):
                self._active_response.close()

    def _is_download_cancelled(self) -> bool:
        return self._cancel_download_flag

    def shutdown(self, *, wait_ms: int = 2) -> None:
        """应用退出前停止内部线程"""
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            self.cancel_download()
            with contextlib.suppress(Exception):
                self.session.close()

            self._cleanup_threads()
            for thread in list(self._threads):
                with contextlib.suppress(RuntimeError):
                    if not thread.isRunning():
                        continue
                    thread.quit()
                    thread.wait(wait_ms)
        finally:
            self._cleanup_threads()
            # 避免异常退出时状态卡住
            self._set_latency_probe_running(False)
            self._shutting_down = False

    # ================== 内部辅助方法 ==================

    def _cleanup_threads(self):
        """清理已结束的线程引用"""
        cleaned_threads = []
        for t in self._threads:
            with contextlib.suppress(RuntimeError):
                if t.isRunning():
                    cleaned_threads.append(t)
        self._threads = cleaned_threads

    def _start_worker_thread(
        self,
        worker: QObject,
        *,
        connect_signals: Callable[[QThread], None],
    ) -> QThread:
        self._cleanup_threads()

        thread = QThread()
        self._thread_counter += 1
        thread.setObjectName(f"UpdateWorker:{worker.__class__.__name__}#{self._thread_counter}")
        worker.moveToThread(thread)
        thread._worker_ref = worker  # 保存引用

        connect_signals(thread)

        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(worker.deleteLater)

        self._threads.append(thread)
        thread.start()
        return thread

    def _fetch_manifest(self) -> dict[str, Any]:
        if self._likely_offline():
            raise UpdateError("设备似乎处于离线状态")

        last_error = None
        resp: requests.Response | None = None

        for url in MANIFEST_URLS:
            resp, last_error = self._try_fetch_manifest(url)
            if resp is not None:
                logger.success("成功获取更新清单")
                break

        if resp is None or resp.status_code != 200:
            raise last_error or UpdateError("所有更新清单 URL 都不可用")

        return self._parse_manifest_json(resp)

    def _decide(self, manifest: dict[str, Any], force: bool = False) -> UpdateDecision:
        target_key = "latest_dev" if config.Update.TargetUpdateChannel == UpdateChannel.DEV else "latest"
        target_ver_str = manifest.get(target_key)

        if not target_ver_str:
            return UpdateDecision(False, None, False, None, ())

        if not force and Version(target_ver_str) <= Version(__version__):  # 强制检查忽略版本
            return UpdateDecision(False, target_ver_str, False, None, ())

        versions = manifest.get("versions", {})
        target_info = versions.get(target_ver_str, {})

        confirm_required = bool(target_info.get("confirm_required", False))
        changelog = self._build_changelog(manifest, Version(target_ver_str), force=force)

        all_downloads = self._extract_downloads(target_info)
        downloads = self._select_downloads(all_downloads)
        if downloads:
            self._ensure_auto_selected_source(is_init=False)

        return UpdateDecision(True, target_ver_str, confirm_required, changelog, downloads)

    def _build_changelog(
        self,
        manifest: dict[str, Any],
        target_version: Version,
        force: bool = False,
    ) -> ChangeLog | None:
        versions = manifest.get("versions", {})
        # 获取所有大于当前版本且小于等于目标版本的信息
        in_range: list[str] = []
        for v_str in versions:
            try:
                v = Version(v_str)
                if (Version(__version__) < v <= target_version) or (force and v == target_version):
                    # 强制获取时，至少展示最新版日志
                    in_range.append(v_str)
            except Exception:
                continue

        # 按版本号倒序排列
        in_range.sort(key=Version, reverse=True)

        if not in_range:
            return None

        descriptions = []
        highlights = []
        others = []
        for v in in_range:
            info: dict = versions[v]
            if bool(info.get("is_dev")) != (
                config.Update.TargetUpdateChannel == UpdateChannel.DEV
            ):  # 不读取不同通道的更新日志
                continue

            if d := info.get("description"):
                descriptions.append(f"[{v}] {d}")
            if h := info.get("highlights"):
                highlights.extend(h)
            if o := info.get("others"):
                others.extend(o)
        description = "\n".join(descriptions)

        return ChangeLog(description=description, highlights=highlights, others=others)

    def _extract_downloads(self, version_info: dict[str, Any]) -> list[DownloadItem]:
        raw_list = version_info.get("downloads", [])
        result = []
        for item in raw_list:
            if isinstance(item, dict) and item.get("url"):
                result.append(
                    DownloadItem(
                        url=item["url"],
                        sha256=item.get("sha256"),
                    )
                )
        return result

    def _select_downloads(self, all_downloads: list[DownloadItem]) -> tuple[DownloadItem, ...]:
        if not all_downloads:
            logger.warning("获取到的下载项为空")
            return ()

        return tuple(all_downloads)

    def _check_sha256(self, path: Path, expected_sha256: str) -> bool:
        expected = expected_sha256.lower().strip()
        h = hashlib.sha256()
        try:
            with path.open("rb") as f:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
            return h.hexdigest().lower() == expected
        except Exception:
            return False

    def _verify_sha256(self, path: Path, expected_sha256: str) -> None:
        if not self._check_sha256(path, expected_sha256):
            # 校验失败删除文件
            with contextlib.suppress(Exception):
                path.unlink(missing_ok=True)
            raise UpdateError("文件 SHA256 校验失败")

    def _normalize_extract_root(self, extract_dir: Path) -> Path:
        """如果解压后只有一层文件夹，则以此文件夹为根，防止套娃"""
        entries = list(extract_dir.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return extract_dir

    def _auto_select_source(self) -> DownloadSource:
        if self.auto_selected_source is not None:
            return self.auto_selected_source

        results = self.test_source_latency()
        available = [(source, latency) for source, latency in results.items() if latency is not None]

        if not available:
            logger.warning("镜像源延迟检测失败, 回退至 GitHub 直连")
            return DownloadSource.GITHUB

        selected_source, selected_latency = min(available, key=lambda x: x[1])
        logger.info(f"自动选择下载源: {selected_source.display_name} ({selected_latency * 1000:.0f} ms)")
        return selected_source

    def _ensure_auto_selected_source(self, *, is_init: bool) -> None:
        if config.Update.TargetDownloadSource != DownloadSource.AUTO:
            return
        if self.auto_selected_source is not None:
            return
        if self._latency_probe_running:
            return

        if is_init:
            worker = LatencyWorker(self)
            self._latency_probe_running = True

            def _connect_signals(thread: QThread) -> None:
                thread.started.connect(worker.run)
                worker.finished.connect(lambda result: self.latency_test_finished.emit(result, False))
                worker.failed.connect(lambda error: self.latency_test_failed.emit(error, False))
                worker.finished.connect(lambda _=None: self._set_latency_probe_running(False))
                worker.finished.connect(thread.quit)
                worker.failed.connect(lambda e: logger.warning(f"启动延迟初始化失败: {e}"))
                worker.failed.connect(lambda _=None: self._set_latency_probe_running(False))
                worker.failed.connect(thread.quit)

            self._start_worker_thread(worker, connect_signals=_connect_signals)
            return

        try:
            self._auto_select_source()
        except Exception as e:
            logger.warning(f"初始化下载源失败, 已回退默认直连: {e}")

    def _set_latency_probe_running(self, value: bool) -> None:
        self._latency_probe_running = value

    @property
    def latency_probe_running(self) -> bool:
        return self._latency_probe_running

    def _probe_source_latency(self, source: DownloadSource) -> float | None:
        # 优先通过 requests 探测，避免 TUN 代理下不可用
        probe_url = DOWNLOAD_SOURCES.get(source)
        if probe_url:
            latency = self._probe_http_latency(probe_url)
            if latency is not None:
                return latency

        # 失败则回退到 TCP 探测
        raw_url = DOWNLOAD_SOURCES.get(source)
        host = str(urlparse(raw_url).hostname or "").lower()
        if not host:
            return None

        return self._probe_tcp_latency(host)

    def _probe_http_latency(self, url: str) -> float | None:
        start = time.perf_counter()
        try:
            with self.session.get(
                url,
                headers={**HEADERS, "Range": "bytes=0-0"},
                timeout=LATENCY_TIMEOUT,
                stream=True,
                allow_redirects=False,
            ) as response:
                if response.status_code >= 500:  # NOTE: 可能过于宽松
                    return None
            return time.perf_counter() - start
        except Exception:
            return None

    def _probe_tcp_latency(self, host: str, port: int = 443) -> float | None:
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=LATENCY_TIMEOUT[0]):
                pass
            return time.perf_counter() - start
        except Exception:
            return None

    @staticmethod
    def _quote(s: str) -> str:
        s = str(s)
        if any(c in s for c in ' \t"'):
            return '"' + s.replace('"', '\\"') + '"'
        return s

    def _likely_offline(self) -> bool:
        """通过快速 DNS 解析判断是否可能离线，避免无网环境下长时间等待"""
        hosts = {urlparse(url).hostname for url in MANIFEST_URLS}
        hosts.discard(None)

        for host in hosts:
            try:
                socket.getaddrinfo(str(host), 443, proto=socket.IPPROTO_TCP)
                return False
            except socket.gaierror:
                continue
            except OSError:
                continue
        return True

    def _format_network_error(self, action: str, err: Exception) -> str:
        if isinstance(err, requests.ConnectTimeout):
            return f"{action}失败：连接超时，请检查网络后重试"
        if isinstance(err, requests.ReadTimeout):
            return f"{action}失败：读取超时，请稍后重试"
        if isinstance(err, requests.ConnectionError):
            return f"{action}失败：网络不可用或服务器无法连接"
        if isinstance(err, OSError):
            return f"{action}失败：网络异常（{err!s}）"
        return f"{action}失败：{err!s}"

    def _download_to_file(
        self,
        *,
        url: str,
        out_path: Path,
        chunk_size: int,
        on_progress: Callable[[int, int], None] | None,
        cancel_checker: Callable[[], bool] | None,
        done: int,
        total: int,
    ) -> None:
        try:
            with self.session.get(
                url,
                headers=HEADERS,
                timeout=DOWNLOAD_TIMEOUT,
                stream=True,
            ) as r:
                self._active_response = r  # 保存引用以便取消

                if r.status_code != 200:
                    raise UpdateError(f"下载失败 HTTP {r.status_code}: {r.text[:200]}")

                total_hdr = r.headers.get("Content-Length")
                total = int(total_hdr) if total_hdr and total_hdr.isdigit() else -1

                with out_path.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        # 检查取消
                        if cancel_checker and cancel_checker():
                            raise DownloadCancelled("下载已取消")
                        if not chunk:
                            continue

                        f.write(chunk)
                        done += len(chunk)

                        if on_progress:
                            on_progress(done, total)
        except (requests.RequestException, OSError) as e:
            self._handle_download_exception(e, out_path)
        finally:
            self._active_response = None

    def _handle_download_exception(self, err: requests.RequestException | OSError, out_path: Path) -> None:
        # 清理未完成文件
        with contextlib.suppress(Exception):
            out_path.unlink(missing_ok=True)
        # 如果是主动关闭 socket 引发的错误，视为取消
        if self._cancel_download_flag:
            raise DownloadCancelled("下载已取消") from err
        raise UpdateError(self._format_network_error("下载更新包", err)) from err

    def _try_fetch_manifest(self, url: str) -> tuple[requests.Response | None, UpdateError | None]:
        try:
            logger.info(f"尝试从 {url} 获取更新清单")
            resp = self.session.get(url, headers=HEADERS, timeout=MANIFEST_TIMEOUT)
            if resp.status_code != 200:
                err = UpdateError(f"更新清单服务器返回错误：{resp.status_code}")
                logger.warning(f"URL {url} 失败: {err}")
                return None, err
            return resp, None
        except requests.RequestException as e:
            err = UpdateError(self._format_network_error("检查更新", e))
            logger.warning(f"URL {url} 请求异常: {err}")
            return None, err

    def _parse_manifest_json(self, resp: requests.Response) -> dict[str, Any]:
        try:
            return resp.json()
        except ValueError as e:
            raise UpdateError(f"更新清单 JSON 解析失败: {e!s}") from e


update_service = UpdateService()


def cleanup_update_cache() -> None:
    """清理更新缓存目录中的残留文件。"""
    if not CACHE_DIR.exists():
        return

    removed_files = 0
    removed_dirs = 0

    for item in CACHE_DIR.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
                removed_dirs += 1
            else:
                item.unlink(missing_ok=True)
                removed_files += 1
        except Exception as e:
            logger.warning(f"清理更新缓存失败: {item} -> {e}")

    if removed_files or removed_dirs:
        logger.info(f"已清理更新缓存: 文件 {removed_files} 个, 目录 {removed_dirs} 个")
