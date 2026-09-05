import shutil
import subprocess
import sys
from pathlib import Path

from packaging.version import Version

from EasiAuto import __version__

APP_NAME = "EasiAuto"
ROOT = Path(__file__).parent.parent  # 项目根目录
MAIN = str(ROOT / "main.py")
OUTPUT_DIR = ROOT / "build"
RESOURCES = ROOT / "resources"
ICON = RESOURCES / "icons" / "EasiAuto.ico"

VERSION = Version(__version__)


def run_pyinstaller():
    """执行 PyInstaller 打包（单一完整版本）"""
    target_dir = OUTPUT_DIR

    # PyInstaller 命令
    cmd = [
        "uv",
        "run",
        "pyinstaller",
        # ------ 基本参数 ------
        f"--name={APP_NAME}",
        "--onedir",
        "--clean",
        "--noconfirm",
        # ------ 排除不需要的 Qt 模块（减小体积）------
        "--exclude-module=PySide6.QtPdf",
        "--exclude-module=PySide6.QtDataVisualization",
        "--exclude-module=PySide6.QtOpenGL",
        "--exclude-module=PySide6.QtOpenGLWidgets",
        # ------ 输出 ------
        f"--distpath={target_dir}",
        f"--workpath={OUTPUT_DIR / 'work'}",
        f"--specpath={OUTPUT_DIR / 'spec'}",
        # ------ Windows 配置 ------
        "--windowed",
        f"--icon={ICON}",
        # ------ 入口 ------
        MAIN,
    ]

    print("Building EasiAuto...")
    print(f"Executing command: {' '.join(cmd)}")

    try:
        subprocess.run(cmd, check=True)
        print(f"Build succeeded! Output path: {target_dir}")
    except subprocess.CalledProcessError as e:
        print(f"Build failed: {e}")
        sys.exit(1)

    # PyInstaller --onedir 输出到 {distpath}/{name}/
    dist_path = target_dir / APP_NAME

    # 复制 resources
    dest_resources = dist_path / "resources"
    if dest_resources.exists():
        shutil.rmtree(dest_resources)
    print(f"Copying resources to {dest_resources}...")
    shutil.copytree(RESOURCES, dest_resources)

    # 复制 vendors 目录 (FULL/LITE 均包含 DllPatcher 等组件)
    vendors_dir = ROOT / "vendors"

    # DllPatcher 编译产物先放入 vendors，再随 vendors 整体复制
    dllpatcher_proj = ROOT / "tools/DllPatcher/DllPatcher.csproj"
    dllpatcher_dir = ROOT / "tools/DllPatcher/bin/Release/net8.0"
    if not dllpatcher_dir.exists():
        # 未预编译时自动编译，避免发行版静默缺失 DllPatcher
        print("DllPatcher 未编译，正在自动编译...")
        try:
            subprocess.run(
                ["dotnet", "build", "-c", "Release", str(dllpatcher_proj)],
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"DllPatcher 编译失败: {e}", file=sys.stderr)
            sys.exit(1)
    if dllpatcher_dir.exists():
        dest_patcher = vendors_dir / "DllPatcher"
        if dest_patcher.exists():
            shutil.rmtree(dest_patcher)
        print(f"Copying DllPatcher to {dest_patcher}...")
        shutil.copytree(dllpatcher_dir, dest_patcher)

    if vendors_dir.exists():
        dest_vendors = dist_path / "vendors"
        if dest_vendors.exists():
            shutil.rmtree(dest_vendors)
        print(f"Copying vendors to {dest_vendors}...")
        shutil.copytree(vendors_dir, dest_vendors)

    # 删除冗余/不需要的 DLL
    redundant_patterns = [
        "**/opengl32sw.dll",
        "**/Qt6Pdf*.dll",
        "**/Qt6Qml*.dll",
        "**/Qt6Quick*.dll",
        "**/Qt6OpenGL*.dll",
        "**/Qt6OpenGLWidgets*.dll",
    ]
    for pattern in redundant_patterns:
        for item in dist_path.glob(pattern):
            print(f"Removing redundant file: {item}")
            item.unlink()

    # 压缩打包结果
    name = "_".join([APP_NAME, f"v{VERSION}"])

    zip_path = OUTPUT_DIR / name
    print(f"Creating archive: {zip_path}.zip ...")

    shutil.make_archive(str(zip_path), "zip", dist_path)
    print(f"Archive completed: {zip_path}.zip")


if __name__ == "__main__":
    run_pyinstaller()
