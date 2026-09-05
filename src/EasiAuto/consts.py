import sys
from contextlib import suppress
from pathlib import Path

# 基本
IS_DEV = not getattr(sys, "frozen", False)
EA_EXECUTABLE = ((Path(sys.executable) if not IS_DEV else Path(sys.argv[0])).parent / "EasiAuto.exe").resolve()
EA_BASEDIR = EA_EXECUTABLE.parent

# 标识
EA_PREFIX = "[EasiAuto]"
IPC_SERVER_NAME = "EasiAuto_Argv_IPC_v1"

# 数据目录
EA_DATADIR = EA_BASEDIR / "data"
CONFIG_PATH = EA_DATADIR / "config.json"
PROFILE_PATH = EA_DATADIR / "profile.json"
LOG_DIR = EA_DATADIR / "logs"
CACHE_DIR = EA_DATADIR / "cache"
AVATAR_DIR = EA_DATADIR / "avatars"

# 资源目录
EA_RESDIR = EA_BASEDIR / "resources"
VENDOR_PATH = EA_BASEDIR / "vendors"

if str(VENDOR_PATH) not in sys.path:
    sys.path.insert(0, str(VENDOR_PATH))


# 数据目录迁移
def _migrate_legacy_file(legacy: Path, target: Path) -> None:
    if not legacy.exists() or target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy.replace(target)


def _migrate_legacy_directory(legacy: Path, target: Path) -> None:
    if not legacy.exists() or not legacy.is_dir():
        return

    target.mkdir(parents=True, exist_ok=True)
    for child in legacy.iterdir():
        destination = target / child.name
        if child.is_dir():
            _migrate_legacy_directory(child, destination)
            with suppress(OSError):
                child.rmdir()
        elif not destination.exists():
            child.replace(destination)

    with suppress(OSError):
        legacy.rmdir()


def migrate_legacy_data_layout() -> None:
    """将旧版运行数据目录迁移到 data 结构。"""
    EA_DATADIR.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_file(EA_BASEDIR / "config.json", CONFIG_PATH)
    _migrate_legacy_directory(EA_BASEDIR / "logs", LOG_DIR)
    _migrate_legacy_directory(EA_BASEDIR / "cache", CACHE_DIR)


if not EA_DATADIR.exists():
    migrate_legacy_data_layout()
