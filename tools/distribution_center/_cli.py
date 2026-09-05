"""EasiAuto 发行中心 CLI —— 无需 Qt / PySide6，可在无图形界面的自动化环境中使用。

用法::

    dist-center update-manifest --version 1.2.0 ...
    dist-center release --version 1.2.0 [--build-first] ...
    dist-center pull [--token ...]
    dist-center push --file announcements.json [--token ...]
    dist-center ui                          # 需要 Qt 环境
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

from ._announcement_core import normalize_payload
from ._release_core import (
    _detect_version,
    do_full_release,
    update_manifest,
)
from ._shared import (
    ANNOUNCEMENT_FILE_PATH,
    ANNOUNCEMENT_REPO,
    fetch_json_from_repo,
    put_json_to_repo,
    resolve_token,
)

# ── 辅助函数 ──────────────────────────────────────────────────────────


def _resolve_is_dev(version_str: str) -> bool:
    """基于 PEP 440 自动判断是否为预发布版本。

    预发布（True）：devN, aN, bN, rcN
    正式版/发布后修订（False）：无后缀, postN, rN
    """
    try:
        v = Version(version_str)
        return v.is_devrelease or v.is_prerelease
    except InvalidVersion:
        return False


# ── 命令处理函数 ──────────────────────────────────────────────────────


def cmd_update_manifest(args: argparse.Namespace) -> None:
    """更新远端更新清单 (update.json)。"""
    token = args.token or resolve_token()
    if token:
        os.environ["RELEASE_PAT"] = token

    is_dev = _resolve_is_dev(args.version) if args.is_dev == "auto" else (args.is_dev == "yes")

    update_manifest(
        dist_dir=Path(args.dist_dir),
        version=args.version,
        is_dev=is_dev,
        confirm_required=args.confirm_required,
        desc=args.desc or None,
        highlights=json.loads(args.highlights),
        others=json.loads(args.others),
        push_to_beta=args.push_to_beta,
    )
    print(f"✅ 版本 {args.version} 的清单已更新")


def cmd_release(args: argparse.Namespace) -> None:
    """执行完整发版流程（创建 Release → 上传资产 → 更新清单）。"""
    token = args.token or resolve_token()
    if token:
        os.environ["RELEASE_PAT"] = token

    version = args.version or _detect_version()
    if not version:
        print("❌ 无法检测版本号，请通过 --version 指定或在 EasiAuto/__init__.py 中设置 __version__")
        sys.exit(1)

    dist_dir = Path(args.dist_dir)
    if not dist_dir.exists():
        print(f"❌ 构建产物目录未找到: {dist_dir}")
        sys.exit(1)

    # 可选：先构建再发版
    if args.build_first:
        print("🔨 开始构建...")
        cmd = ["uv", "run", "python", "tools/build.py"]
        result = subprocess.run(cmd, cwd=Path(__file__).parent.parent.parent, check=False)
        if result.returncode != 0:
            print(f"❌ 构建失败 (exit code {result.returncode})")
            sys.exit(1)
        print("✅ 构建完成")
        print()

    is_dev = _resolve_is_dev(version) if args.is_dev == "auto" else (args.is_dev == "yes")

    try:
        msg = do_full_release(
            dist_dir=dist_dir,
            version=version,
            is_dev=is_dev,
            confirm_required=args.confirm_required,
            desc=args.desc or None,
            highlights=json.loads(args.highlights),
            others=json.loads(args.others),
            push_to_beta=args.push_to_beta,
            is_draft=args.draft,
        )
        print(f"✅ {msg}")
    except Exception as e:
        print(f"❌ 发版失败: {e}")
        sys.exit(1)


def cmd_pull(args: argparse.Namespace) -> None:
    """从远端拉取公告并打印。"""
    token = args.token or resolve_token()
    if not token:
        print("❌ 未找到 GitHub Token，请传入 --token 或设置 RELEASE_PAT 环境变量")
        sys.exit(1)

    payload, _ = fetch_json_from_repo(ANNOUNCEMENT_REPO, ANNOUNCEMENT_FILE_PATH, token)
    print(json.dumps(normalize_payload(payload), indent=4, ensure_ascii=False))


def cmd_push(args: argparse.Namespace) -> None:
    """将本地公告 JSON 推送到远端。"""
    token = args.token or resolve_token()
    if not token:
        print("❌ 未找到 GitHub Token，请传入 --token 或设置 RELEASE_PAT 环境变量")
        sys.exit(1)

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"❌ 文件未找到: {file_path}")
        sys.exit(1)

    payload = normalize_payload(json.loads(file_path.read_text(encoding="utf-8")))
    sha = None
    if not args.skip_pull:
        _, sha = fetch_json_from_repo(ANNOUNCEMENT_REPO, ANNOUNCEMENT_FILE_PATH, token)

    put_json_to_repo(
        ANNOUNCEMENT_REPO,
        ANNOUNCEMENT_FILE_PATH,
        sha,
        payload,
        f"Update announcements ({len(payload['announcements'])} items)",
        token,
    )
    print("✅ 远端公告已更新")


def cmd_ui(_args: argparse.Namespace) -> None:
    """启动发行中心图形界面（需要 Qt 环境）。"""
    # 延期导入 —— 仅在真正需要 GUI 时才加载 Qt
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import FluentIcon, FluentTranslator, FluentWindow, Theme, setTheme

    from ._announcement import AnnouncementManagerWidget
    from ._build import BuildWidget
    from ._config import ConfigWidget
    from ._release import ReleaseFormWidget
    from ._shared import resolve_token as _resolve_token

    class DistributionCenter(FluentWindow):
        def __init__(self):
            super().__init__()
            setTheme(Theme.AUTO)
            self.setWindowIcon(QIcon("src/EasiAuto/resources/icons/EasiAuto.ico"))
            self.setWindowTitle("EasiAuto 发行中心")
            self.resize(900, 720)

            self.navigationInterface.setExpandWidth(160)
            self.navigationInterface.setCollapsible(False)

            self.config_widget = ConfigWidget(self)
            self.build_widget = BuildWidget(self)
            self.release_form_widget = ReleaseFormWidget(self)
            self.announcement_widget = AnnouncementManagerWidget(self)

            self.addSubInterface(self.config_widget, FluentIcon.SETTING, "配置")
            self.addSubInterface(self.build_widget, FluentIcon.DEVELOPER_TOOLS, "构建")
            self.addSubInterface(self.release_form_widget, FluentIcon.UPDATE, "发版")
            self.addSubInterface(self.announcement_widget, FluentIcon.INFO, "公告管理")

            if _resolve_token():
                QTimer.singleShot(500, self.announcement_widget.pull_if_token_available)

    app = QApplication(sys.argv)
    translator = FluentTranslator()
    app.installTranslator(translator)
    w = DistributionCenter()
    w.show()
    sys.exit(app.exec())


# ── 参数解析 & 主入口 ─────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """构建 ArgumentParser（可供外部程序复用）。"""
    parser = argparse.ArgumentParser(
        prog="dist-center",
        description="EasiAuto 发行中心 —— 统一管理构建、发版与公告。CLI 模式无需 Qt 环境。",
    )
    subparsers = parser.add_subparsers(title="子命令", dest="command")

    # ── update-manifest ──
    p = subparsers.add_parser("update-manifest", help="仅更新远端更新清单 (update.json)")
    p.add_argument("--version", required=True, help="版本号, 如 1.2.0")
    p.add_argument(
        "--is-dev",
        nargs="?",
        const="yes",
        default="auto",
        help="预发布标志: 默认根据版本号自动判断; 传 --is-dev 强制标记为预发布; 传 --is-dev=no 强制标记为正式版",
    )
    p.add_argument("--confirm-required", action="store_true", help="是否需要用户确认更新")
    p.add_argument("--desc", default="", help="版本描述内容")
    p.add_argument(
        "--highlights", default="[]", help='JSON 格式的亮点列表, 如 \'[{"name":"功能","description":"描述"}]\''
    )
    p.add_argument("--others", default="[]", help='JSON 格式的其他更新列表, 如 \'["修复Bug","优化性能"]\'')
    p.add_argument("--dist-dir", default="build", help="构建产物所在目录 (默认: build)")
    p.add_argument("--push-to-beta", action="store_true", help="正式版同步推送到测试版通道")
    p.add_argument("--token", default="", help="GitHub Personal Access Token (也可通过 RELEASE_PAT 环境变量设置)")
    p.set_defaults(func=cmd_update_manifest)

    # ── release (新增：完整发版流程) ──
    p = subparsers.add_parser("release", help="执行完整发版流程（创建 Release、上传资产、更新清单）")
    p.add_argument("--version", default="", help="版本号 (默认自动从 EasiAuto 包读取)")
    p.add_argument(
        "--is-dev",
        nargs="?",
        const="yes",
        default="auto",
        help="预发布标志: 默认根据版本号自动判断; 传 --is-dev 强制标记为预发布; 传 --is-dev=no 强制标记为正式版",
    )
    p.add_argument("--confirm-required", action="store_true", help="是否需要用户确认更新")
    p.add_argument("--desc", default="", help="版本描述内容")
    p.add_argument("--highlights", default="[]", help="JSON 格式的亮点列表")
    p.add_argument("--others", default="[]", help="JSON 格式的其他更新列表")
    p.add_argument("--dist-dir", default="build", help="构建产物所在目录 (默认: build)")
    p.add_argument("--push-to-beta", action="store_true", help="正式版同步推送到测试版通道")
    p.add_argument("--draft", action="store_true", help="创建为草稿 Release（不公开发布）")
    p.add_argument("--token", default="", help="GitHub Personal Access Token")
    p.add_argument("--build-first", action="store_true", help="先执行构建再发版")
    p.set_defaults(func=cmd_release)

    # ── pull ──
    p = subparsers.add_parser("pull", help="从远端拉取公告 JSON 并打印到 stdout")
    p.add_argument("--token", default="", help="GitHub Token")
    p.set_defaults(func=cmd_pull)

    # ── push ──
    p = subparsers.add_parser("push", help="将本地公告 JSON 文件推送到远端")
    p.add_argument("--file", required=True, help="本地公告 JSON 文件路径")
    p.add_argument("--token", default="", help="GitHub Token")
    p.add_argument("--skip-pull", action="store_true", help="跳过拉取远端 SHA（覆盖远端内容时使用）")
    p.set_defaults(func=cmd_push)

    # ── ui ──
    p = subparsers.add_parser("ui", help="打开发行中心图形界面（需要 Qt 环境）")
    p.set_defaults(func=cmd_ui)

    return parser


def main(args: list[str] | None = None) -> None:
    """CLI 主入口。"""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    ns = parser.parse_args(args)

    if hasattr(ns, "func"):
        ns.func(ns)
    else:
        # 无子命令时默认启动 GUI
        cmd_ui(ns)


if __name__ == "__main__":
    main()
