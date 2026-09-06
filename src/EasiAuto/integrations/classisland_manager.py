import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import cast

import win32api
import win32con
import win32event
from loguru import logger
from pydantic import AliasPath, BaseModel, ConfigDict, Field

from PySide6.QtCore import QObject, Signal

from EasiAuto.consts import EA_EXECUTABLE, EA_PREFIX
from EasiAuto.core.utils import kill_process
from EasiAuto.models.profile import EasiAutomation, profile


class CiSubject(BaseModel):
    id: str
    name: str


class ManagedCiAutomation(BaseModel):
    """受管理的 ClassIsland 自动化"""

    model_config = ConfigDict(validate_by_name=True)

    guid: str = Field(validation_alias=AliasPath("ActionSet", "Guid"))
    name: str = Field(validation_alias=AliasPath("ActionSet", "Name"))
    is_enabled: bool = Field(validation_alias=AliasPath("ActionSet", "IsEnabled"))
    subject_id: str = Field(validation_alias=AliasPath("Ruleset", "Groups", 0, "Rules", 0, "Settings", "SubjectId"))
    pretime: int = Field(validation_alias=AliasPath("Triggers", 0, "Settings", "TimeSeconds"))
    args: str = Field(validation_alias=AliasPath("ActionSet", "Actions", 0, "Settings", "Args"))

    def dump(self) -> dict:
        return self.build_ci_raw(
            guid=self.guid,
            name=self.name,
            is_enabled=self.is_enabled,
            subject_id=self.subject_id,
            pretime=self.pretime,
            args=self.args,
        )

    def get_arg(self, flag: str) -> str | None:
        if not flag:
            return None

        try:
            tokens = shlex.split(self.args)
            for i, token in enumerate(tokens):
                if f"--{flag}" in token or f"-{flag[0]}" in token:
                    return tokens[i + 1]
        except (ValueError, IndexError):
            pass
        return None

    def get_name(self) -> str | None:
        match = re.match(rf"^{re.escape(EA_PREFIX)} .+ - (.+)$", self.name)
        return match.group(1) if match else None

    @property
    def account(self) -> str | None:
        return self.get_arg("account")

    @property
    def password(self) -> str | None:
        return self.get_arg("password")

    @property
    def id(self) -> str | None:
        return self.get_arg("id")

    @staticmethod
    def build_ci_raw(
        guid: str,
        name: str,
        is_enabled: bool,
        subject_id: str | list[str],
        pretime: int,
        args: str,
    ) -> dict:
        rule_next: list[dict] = []  # 下节课是...
        rule_pre: list[dict] = []  # 上节课不是...
        for subject in subject_id if isinstance(subject_id, list) else [subject_id]:
            if not subject:
                raise ValueError("Subject ID 不能为空")
            rule_next.append(
                {
                    "IsReversed": False,
                    "Id": "classisland.lessons.nextSubject",
                    "Settings": {"SubjectId": subject_id},
                }
            )
            rule_pre.append(
                {
                    "IsReversed": True,
                    "Id": "classisland.lessons.previousSubject",
                    "Settings": {"SubjectId": subject_id},
                }
            )

        return {
            "Ruleset": {
                "Mode": 1,  # AND
                "IsReversed": False,
                "Groups": [
                    {
                        "Rules": rule_next,
                        "Mode": 0,  # OR
                        "IsReversed": False,
                        "IsEnabled": True,
                    },
                    {
                        "Rules": rule_pre,
                        "Mode": 0,  # OR
                        "IsReversed": False,
                        "IsEnabled": True,
                    },
                ],
            },
            "ActionSet": {
                "IsEnabled": is_enabled,
                "Name": name,
                "Guid": guid,
                "IsOn": False,
                "Actions": [
                    {
                        "Id": "classisland.os.run",
                        "Settings": {
                            "Value": str(EA_EXECUTABLE),
                            "Args": args,
                        },
                    }
                ],
                "IsRevertEnabled": False,
            },
            "Triggers": [
                {
                    "Id": "classisland.lessons.preTimePoint",
                    "Settings": {"TargetState": 1, "TimeSeconds": pretime},
                }
            ],
            "IsConditionEnabled": True,
        }


class ClassIslandNotifier(QObject):
    changed = Signal()


class ClassIslandManager:
    def __init__(self, exe_path: Path | str):

        self.exe_path = Path(exe_path)
        if not self.exe_path.exists():
            raise FileNotFoundError(f"ClassIsland 可执行文件不存在: {self.exe_path}")

        self.is_v2 = self._check_is_v2()
        self.ci_settings: dict = {}
        self.ci_profile: dict = {}
        self.ci_automations_raw: list[dict] = []

        self.unmanaged_automations: list[dict] = []
        self.managed_automations: list[ManagedCiAutomation] = []
        self.notifier = ClassIslandNotifier()
        self.is_imports_available = False

        self.reload()

    def _check_is_v2(self) -> bool:
        try:
            info = win32api.GetFileVersionInfo(str(self.exe_path), "\\")
            ms = info["FileVersionMS"]
            return (ms >> 16) >= 2
        except Exception:
            return False

    @property
    def data_dir(self) -> Path:
        return self.exe_path.parent / "data" if self.is_v2 else self.exe_path.parent

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "Settings.json"

    @property
    def current_profile_path(self) -> Path:
        name = self.ci_settings.get("SelectedProfile", "Default.json")
        return self.data_dir / "Profiles" / name

    @property
    def current_automation_path(self) -> Path:
        name = self.ci_settings.get("CurrentAutomationConfig", "Default")
        return self.data_dir / "Config" / "Automations" / f"{name}.json"

    @staticmethod
    def _signature(raw: list[dict]) -> str:
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)

    def reload(self, notify_on_change: bool = True):
        """重新加载所有配置"""
        try:
            previous_signature = self._signature(self.ci_automations_raw)
            self.ci_settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            self.ci_profile = json.loads(self.current_profile_path.read_text(encoding="utf-8"))
            self.ci_automations_raw = json.loads(self.current_automation_path.read_text(encoding="utf-8"))

            self._resolve_automations()

            if notify_on_change:
                current_signature = self._signature(self.ci_automations_raw)
                if previous_signature != current_signature:
                    self.notifier.changed.emit()
        except Exception as e:
            raise RuntimeError("加载 ClassIsland 配置时出错") from e

    def _resolve_automations(self) -> list[ManagedCiAutomation]:
        """将原始自动化按照受管理状态分离

        Returns:
            list[ManagedCiAutomation]: 可导入的旧自动化
        """
        self.unmanaged_automations = []
        self.managed_automations = []
        pending_imports: list[ManagedCiAutomation] = []
        imported_account: set[str] = set()
        for raw in self.ci_automations_raw:
            try:
                if raw.get("ActionSet", {}).get("Name", "").startswith(EA_PREFIX):
                    auto = ManagedCiAutomation(**raw)
                    if auto.id and profile.get_automation(auto.id) is not None:
                        self.managed_automations.append(auto)
                    elif auto.account and auto.password:
                        if auto.account in imported_account:
                            continue

                        pending_imports.append(auto)
                        imported_account.add(auto.account)
                        self.managed_automations.append(auto)
                    else:
                        logger.warning(f"无效的自动化: {auto.name}, 已清除")
                else:
                    self.unmanaged_automations.append(raw)
            except Exception as e:
                logger.warning(f"解析 ClassIsland 自动化时出错: {e}")
                self.unmanaged_automations.append(raw)

        self.is_imports_available = len(pending_imports) > 0
        return pending_imports

    def import_legacy_automation(self) -> tuple[bool, str]:
        """
        解析自动化，并同步导入旧档案后写回 ClassIsland

        Returns:
            tuple[bool, str]: 导入是否成功及提示消息
        """
        pending_imports = self._resolve_automations()
        if not pending_imports:
            return True, "没有需要导入的旧自动化"

        imported_count: int = 0
        failed_count: int = 0
        for auto in pending_imports:
            try:
                # _resolve_automations 已确保 account 和 password 不为空
                account = cast(str, auto.account)
                password = cast(str, auto.password)

                new_auto = EasiAutomation(
                    account=account,
                    password=password,
                    name=auto.get_name(),
                )
                profile.upsert_automation(new_auto)
                auto.args = f"--id {new_auto.id}"

                imported_count += 1
                logger.info(f"已导入旧的自动化档案: {account}")
            except Exception as e:
                failed_count += 1
                logger.warning(f"导入旧自动化失败: {auto}，原因: {e}")

        if imported_count <= 0:
            return False, "没有成功导入任何条目"

        if need_restart := self.is_running:
            self.stop_ci(wait=True)
        try:
            profile.save(reason="automation_saved")
            if not self.save_automations(self.managed_automations):
                return False, "保存至 ClassIsland 失败"
        except Exception:
            return False, "保存时出错"
        if need_restart:
            self.start_ci()

        if failed_count > 0:
            return True, f"{failed_count} 项导入失败"
        return True, ""

    def get_automations(self) -> list[ManagedCiAutomation]:
        return self.managed_automations

    def save_automations(self, automations: list[ManagedCiAutomation]) -> bool:
        """保存自动化至 ClassIsland"""
        if self.is_running:
            logger.warning("无法保存自动化: ClassIsland 正在运行")
            return False

        try:
            output = self.unmanaged_automations
            for auto in automations:
                output.append(auto.dump())

            content = json.dumps(output)
            self.current_automation_path.write_text(content, encoding="utf-8")

            self.reload(notify_on_change=False)
            self.notifier.changed.emit()
            return True
        except Exception as e:
            logger.error(f"保存自动化至 ClassIsland 时出错: {e}")
            return False

    def get_subjects(self) -> list[CiSubject]:
        subjects = self.ci_profile.get("Subjects", {})
        return [CiSubject(id=k, name=v.get("Name", "Unknown")) for k, v in subjects.items()]

    @property
    def is_running(self) -> bool:
        """使用互斥锁检查 ClassIsland 的运行状态"""
        mutex = "Global\\ClassIsland.Lock" if self.is_v2 else "ClassIsland.Lock"
        try:
            h = win32event.OpenMutex(win32con.SYNCHRONIZE, False, mutex)
            if h:
                win32api.CloseHandle(h)
                return True
        except Exception:
            pass
        return False

    def start_ci(self):
        subprocess.Popen(self.exe_path, cwd=self.exe_path.parent)

    def stop_ci(self, force: bool = False, wait: bool = False, timeout: int = 2):
        kill_process("ClassIsland.Desktop" if self.is_v2 else "ClassIsland", force=force, wait=wait, timeout=timeout)


class _ClassIslandManagerProxy:
    """ClassIslandManager 代理，以便动态初始化单例"""

    def __init__(self):
        self._impl = None

    def initialize(self, path: Path):
        self._impl = ClassIslandManager(path)

    def __getattr__(self, item):
        return getattr(self._impl, item)

    def __bool__(self):
        return self._impl is not None


classisland_manager = cast(ClassIslandManager, _ClassIslandManagerProxy())
