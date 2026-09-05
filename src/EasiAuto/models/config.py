"""应用配置存储
提供一个全局 config 单例，用于访问应用的所有配置。
基于 pydantic，配置项支持类型验证与自动保存，且可将元数据传递给 UI 生成 SettingCard。
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from functools import total_ordering
from typing import Any

import qt_pydantic as qtp
from loguru import logger
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, computed_field, field_validator
from pydantic.fields import FieldInfo

from PySide6.QtGui import QColor

from EasiAuto import __version__
from EasiAuto.consts import CONFIG_PATH
from EasiAuto.core.utils import desensitize_account


@total_ordering
class InformativeEnum(Enum):
    """带显示名称的枚举类"""

    def __init__(self, value, display_name, description=None, extra=None):
        self.display_name: str = display_name
        self.description: str | None = description
        self.extra: dict[str, Any] = extra or {}
        self._value_ = value

    def __lt__(self, other: InformativeEnum):
        if self.__class__ is other.__class__ and isinstance(self.value, int) and isinstance(other.value, int):
            return self.value < other.value
        return NotImplemented


class LogLevelEnum(InformativeEnum):
    TRACE = (5, "追踪")
    DEBUG = (10, "调试")
    INFO = (20, "信息")
    WARNING = (30, "警告")
    ERROR = (40, "错误")
    CRITICAL = (50, "灾难")

# fmt: off
class LoginMethod(InformativeEnum):
    FIXED = (0, "固定位置", "大部分情况下开箱即用，仅在特殊情况需手动设置坐标", {"tags": ["较快", "较稳定", "默认"]})
    CV = (1, "图像识别", "仅支持常规分辨率与缩放，基于精确像素匹配", {"tags": ["较慢", "不稳定"]})
    UIA = (2, "自动定位", "基于 UI Automation 直接获取页面元素，在部分机器上可能极慢", {"tags": ["较慢", "最稳定"]})
    TOKEN = (3, "令牌投递", "通过希沃接口获取登录令牌并投递至希沃白板，免密极速登录", {"tags": ["最快", "较稳定", "需要修补"]})
# fmt: on

class ThemeOptions(InformativeEnum):
    AUTO = ("Auto", "跟随系统")
    LIGHT = ("Light", "浅色")
    DARK = ("Dark", "深色")


class UpdateMode(InformativeEnum):
    NEVER = (0, "从不自动更新")
    CHECK_AND_NOTIFY = (1, "自动检查更新并通知")
    CHECK_AND_DOWNLOAD = (2, "自动检查更新并下载")
    CHECK_AND_INSTALL = (3, "自动检查更新并安装")


class UpdateChannel(InformativeEnum):
    RELEASE = ("release", "稳定通道")
    DEV = ("dev", "测试通道")


class DownloadSource(InformativeEnum):
    AUTO = ("auto", "自动选择")
    GITHUB = ("github", "GitHub")
    GHPROXY = ("ghproxy", "ghproxy")
    GHFAST = ("ghfast", "ghfast")


class ConfigModel(BaseModel):
    """带自动保存能力的配置模型"""

    model_config = ConfigDict(validate_by_name=True, validate_assignment=True)

    _parent: ConfigModel | None = PrivateAttr(default=None)
    _initialized: bool = PrivateAttr(default=False)

    @contextmanager
    def initialize(self, rebind: bool = True):
        self._initialized = False
        yield
        if rebind:
            self._bind_children()
        self._initialized = True

    def model_post_init(self, __context):
        super().model_post_init(__context)

        self._bind_children()
        self._initialized = True

    def _root(self) -> ConfigModel:
        root: ConfigModel = self
        while root._parent is not None:
            root = root._parent
        return root

    def save(self) -> None:
        path = CONFIG_PATH
        try:
            data = self._root().model_dump(mode="json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def __setattr__(self, name: str, value):
        super().__setattr__(name, value)
        if getattr(self, "_initialized", False) and not name.startswith("_"):
            self.save()

    def _bind_children(self):
        """递归绑定所有子模型的父模型"""
        for value in self.__dict__.values():
            if isinstance(value, ConfigModel):
                value._parent = self
                value._bind_children()

    def set_by_path(self, parts: tuple[str, ...], value: Any):
        target: Any = self
        for key in parts[:-1]:
            target = getattr(target, key)
        setattr(target, parts[-1], value)

    def iter_items(
        self,
        only: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[ConfigItem | ConfigGroup]:
        return _iter_config_items(self, only=only, exclude=exclude)

    def get_item(self, item: str) -> ConfigItem:
        """通过点分路径获取叶子配置项，如 "Login.Timeout.Terminate"

        路径相对 self 解析，返回的 ConfigItem 始终携带完整路径并绑定根模型。
        路径指向子模型、计算属性或不存在的字段时抛出异常。（不存在的字段应在开发阶段就被发现并修正）
        """
        parts = tuple(p for p in item.split(".") if p)
        if not parts:
            raise ValueError(f"无效的配置项路径: {item}")

        obj: Any = self
        for key in parts[:-1]:
            if key.startswith("_") or not isinstance(getattr(obj, key, None), ConfigModel):
                raise ValueError(f"配置项路径不存在: {item}")

            obj = getattr(obj, key)

        name = parts[-1]
        field_info = type(obj).model_fields.get(name)
        if field_info is None:
            raise ValueError(f"配置项路径不存在: {item}")

        # 回溯父链，将相对路径补全为从根模型出发的完整路径
        full_parts = parts
        node, parent = self, self._parent
        while parent is not None:
            owner_name = next((n for n, v in parent.__dict__.items() if v is node), None)
            if owner_name is None:
                raise ValueError(f"配置项路径不存在: {item}")
            full_parts = (owner_name, *full_parts)
            node, parent = parent, parent._parent

        return ConfigItem(
            path=".".join(full_parts),
            name=name,
            group=".".join(full_parts[:-1]) or None,
            type_=field_info.annotation or type(getattr(obj, name)),
            field_info=field_info,
            title=field_info.title or name,
            description=field_info.description,
            json_schema_extra=field_info.json_schema_extra,
            _root=node,
        )


class EasiNoteConfig(ConfigModel):
    """希沃白板相关配置"""

    AutoPath: bool = Field(
        default=True,
        title="自动检测路径",
        description="启用后，将忽略自定义路径",
    )
    Path: str = Field(
        default=r"C:\Program Files (x86)\Seewo\EasiNote5\swenlauncher\swenlauncher.exe",
        title="自定义路径",
    )
    ProcessName: str = Field(
        default="EasiNote",
        title="进程名",
    )
    WindowTitle: str = Field(
        default="希沃白板",
        title="窗口标题",
    )
    Args: str = Field(
        default="",
        title="启动参数",
        description="使用固定位置且非 iwb 模式时无效",
    )
    ExtraKills: str = Field(
        default="",
        title="查杀进程列表",
        description="启动希沃白板时一并结束这些进程，用英文逗号分隔",
    )


class TimeoutConfig(ConfigModel):
    """BaseAutomator 使用的等待时长配置"""

    # 通用
    Terminate: float = Field(
        default=1,
        ge=0,
        le=60,
        title="终止进程等待时间",
        description="终止进程后，等待其彻底结束的时间",
    )
    LaunchPollingTimeout: float = Field(
        default=30,
        ge=0,
        le=60,
        title="等待启动超时时间",
        description="启动后，等待其启动完成的最大时间",
    )
    LaunchPollingInterval: float = Field(
        default=0.5,
        ge=0.1,
        le=60,
        title="等待启动轮询间隔",
        description="轮询检测是否启动完成的时间间隔",
    )
    AfterLaunch: float = Field(
        default=2,
        ge=0,
        le=60,
        title="启动后等待时间",
        description="启动后，等待界面加载的时间",
    )
    # 模拟点击
    EnterLoginUI: float = Field(
        default=3,
        ge=0,
        le=60,
        title="进入登录界面等待时间",
        description="点击左下角“登录”按钮后，等待界面出现的时间",
    )
    SwitchTab: float = Field(
        default=1,
        ge=0,
        le=60,
        title="切换标签等待时间",
        description="等待切换到账号登录标签页的时间",
    )


class PositionConfig(ConfigModel):
    """FixedAutomator 使用的位置坐标"""

    EnableScaling: bool = Field(
        default=True,
        title="启用智能缩放",
        description="根据系统分辨率和缩放自动调整坐标。若启用，设置的坐标必须基于 1920x1080 100% 缩放",
    )
    EnterLogin: tuple[int, int] = Field(
        default=(172, 1044),
        title="进入登录界面按钮",
    )
    AccountLoginTab: tuple[int, int] = Field(
        default=(1090, 350),
        title="切换到“账号登录”标签页的按钮",
    )
    AccountInput: tuple[int, int] = Field(
        default=(1000, 420),
        title="账号输入框",
    )
    PasswordInput: tuple[int, int] = Field(
        default=(1000, 490),
        title="密码输入框",
    )
    AgreementCheckbox: tuple[int, int] = Field(
        default=(935, 724),
        title="同意协议复选框",
    )
    LoginButton: tuple[int, int] = Field(
        default=(1090, 560),
        title="登录按钮",
    )
    BaseSize: tuple[int, int] = Field(
        default=(1920, 1080),
        title="基准分辨率",
        json_schema_extra={"hidden": True},
    )
    LoginWindowSize: tuple[int, int] = Field(
        default=(808, 582),
        title="登录界面窗口大小",
        json_schema_extra={"hidden": True},
    )


class LoginConfig(ConfigModel):
    Method: LoginMethod = Field(
        default=LoginMethod.FIXED,
        title="登录方式",
        description="选择用于进行自动登录的方式",
        json_schema_extra={"icon": "Application", "style": "expand_selector"},
    )
    SkipOnce: bool = Field(
        default=False,
        title="跳过一次",
        description="下次运行时跳过自动登录，适用于公开课等需临时禁用的场景",
        json_schema_extra={"icon": "Send"},
    )
    SkipIfLoggedIn: bool = Field(
        default=True,
        title="已登录则跳过",
        description="若当前希沃白板已登录同一账号，则跳过登录",
        json_schema_extra={"icon": "PageRight"},
    )
    KillAgent: bool = Field(
        default=False,
        title="终止 EasiAgent 服务",
        description="可避免某些情况下自动登录被希沃白板的快捷登录打断",
        json_schema_extra={"icon": "PowerButton"},
    )
    IsIwb: bool = Field(
        default=True,
        title="需要进入登录界面",
        description="适用于打开希沃白板后直接进入黑板界面（iwb）的情况，在非希沃机器上需要关闭",
        json_schema_extra={"icon": "People"},
    )
    Is4K: bool = Field(
        default=False,
        title="图像识别 4K 适配",
        description="在图像识别登录方式下，启用对 3840x2160 200% 缩放的支持",
        json_schema_extra={"icon": "FitPage"},
    )
    ForceEnableScaling: bool = Field(
        default=False,
        title="强制启用兼容模式输入",
        description="强制使用复制粘贴进行输入，对自动定位不起作用。不要调整此选项，除非你知道自己在做什么",
        json_schema_extra={"icon": "Asterisk"},
    )

    EasiNote: EasiNoteConfig = Field(
        default_factory=EasiNoteConfig,
        title="希沃白板选项",
        description="配置希沃白板的路径、进程名、窗口标题和启动参数",
        json_schema_extra={"icon": "Application"},
    )
    Timeout: TimeoutConfig = Field(
        default_factory=TimeoutConfig,
        title="等待时长",
        description="配置自动登录过程中的等待时长（秒）",
        json_schema_extra={"icon": "StopWatch"},
    )
    Position: PositionConfig = Field(
        default_factory=PositionConfig,
        title="位置坐标",
        description="配置固定位置登录方式下的各个按钮和输入框的位置坐标\n默认值已配置为 1920x1080 100% 缩放下的坐标",
        json_schema_extra={"icon": "Move"},
    )


class WarningConfig(ConfigModel):
    Enabled: bool = Field(
        default=True,
        title="启用警告弹窗",
        description="在运行自动登录前显示警告弹窗，在超时时长内可手动取消或推迟登录",
        json_schema_extra={"icon": "Completed"},
    )
    Timeout: int = Field(
        default=60,
        ge=5,
        le=600,
        title="超时时长（秒）",
        description="等待用户确认的最大时长",
        json_schema_extra={"icon": "RemoveFrom"},
    )
    MaxDelays: int = Field(
        default=1,
        ge=0,
        le=3,
        title="最大推迟次数",
        description="最大允许推迟登录的次数",
        json_schema_extra={"icon": "Pause"},
    )
    DelayTime: int = Field(
        default=150,
        ge=5,
        le=300,
        title="推迟时长（秒）",
        description="每次推迟登录后等待的时长",
        json_schema_extra={"icon": "History"},
        validation_alias="Delay",
    )
    ShowUserName: bool = Field(
        default=True,
        title="显示用户名",
        description="在警告弹窗中显示目标登录账号的用户名",
        json_schema_extra={"icon": "QuickNote"},
    )


class BannerStyleConfig(ConfigModel):
    Text: str = Field(
        default="  ⚠️WARNING⚠️  正在运行希沃白板自动登录  请勿触摸一体机",
        title="文本",
        description="横幅中滚动的文本内容",
        json_schema_extra={"icon": "Label"},
    )
    TextFont: str = Field(
        default="HarmonyOS Sans SC",
        title="文字字体",
        description="横幅文本使用的字体名称",
        json_schema_extra={"icon": "Font"},
    )
    TextColor: qtp.QColor = Field(
        default=QColor("#FFFFDE59"),
        title="文字颜色",
        description="横幅文本的颜色",
        json_schema_extra={"icon": "Palette", "enable_alpha": True},
    )
    FgColor: qtp.QColor = Field(
        default=QColor("#C8FFDE59"),
        title="前景颜色",
        description="横幅高亮或装饰元素的颜色",
        json_schema_extra={"icon": "Highlight", "enable_alpha": True},
    )
    BgColor: qtp.QColor = Field(
        default=QColor("#B4E4080A"),
        title="背景颜色",
        description="横幅的背景色",
        json_schema_extra={"icon": "BackgroundColor", "enable_alpha": True},
    )
    Fps: int = Field(
        default=60,
        ge=1,
        le=120,
        title="刷新率",
        description="横幅的每秒的更新次数",
        json_schema_extra={"icon": "SpeedHigh", "style": "slider"},
    )
    TextSpeed: int = Field(
        default=3,
        ge=-8,
        le=8,
        title="文字滚动距离",
        description="横幅文本每次刷新滚动的距离",
        json_schema_extra={"icon": "RightArrow", "style": "slider"},
    )
    YOffset: int = Field(
        default=20, title="垂直偏移", description="横幅距离屏幕顶部的像素偏移量", json_schema_extra={"icon": "Down"}
    )


class BannerConfig(ConfigModel):
    Enabled: bool = Field(
        default=True,
        title="启用警示横幅",
        description="运行自动运行时在屏幕顶部显示一个醒目的警示横幅",
        json_schema_extra={"icon": "Flag"},
    )
    Style: BannerStyleConfig = Field(
        default_factory=BannerStyleConfig,
        title="横幅样式",
        description="定制警示横幅的样式与外观",
        json_schema_extra={"icon": "Brush"},
    )


class StatusOverlayConfig(ConfigModel):
    Enabled: bool = Field(
        default=True,
        title="启用状态浮窗",
        description="登录时，在屏幕中间下方位置显示当前登录任务执行状态",
        json_schema_extra={"icon": "Info"},
    )


class AppConfig(ConfigModel):
    MaxRetries: int = Field(
        default=2,
        ge=0,
        le=5,
        title="最大重试次数",
        description="自动登录失败时的最大重试次数",
        json_schema_extra={"icon": "Sync"},
    )
    Theme: ThemeOptions = Field(
        default=ThemeOptions.AUTO,
        title="应用主题",
        description="控制应用的明暗主题",
        json_schema_extra={"icon": "Constract"},
    )
    LogEnabled: bool = Field(
        default=True,
        title="启用日志记录",
        description="在应用 /logs 目录记录日志文件，以便于进行调试和问题排查",
        json_schema_extra={"icon": "Document"},
    )
    TelemetryEnabled: bool = Field(
        default=True,
        title="参与匿名使用情况分析",
        description="通过 Sentry SDK 收集此应用的错误信息以帮助我们改进此应用\n你的信息会匿名上传，且不会包含任何你的个人信息。你随时可以手动关闭该选项",
        json_schema_extra={"icon": "Feedback"},
    )


class PrivacyMaskConfig(ConfigModel):
    Enabled: bool = Field(
        default=True,
        title="启用隐私保护遮罩",
        description="登录时，在输入框上显示一个遮罩，用于遮挡可能的隐私信息。仅在固定位置与自动定位中可用\n下方选项用于控制固定位置下的显示，部分选项继承自位置坐标",
    )
    MaskLeftTop: tuple[int, int] = Field(
        default=(868, 381),
        title="遮罩位置",
        description="即左上角的坐标",
    )
    MaskSize: tuple[int, int] = Field(
        default=(440, 386),
        title="遮罩大小",
    )


class ExperimentalConfig(ConfigModel):
    PrivacyMask: PrivacyMaskConfig = Field(
        default_factory=PrivacyMaskConfig,
        title="隐私保护遮罩",
        json_schema_extra={"icon": "VPN"},
    )

class ProfileConfig(ConfigModel):
    SkipVerify: bool = Field(
        default=False,
        title="保存档案时跳过账号校验",
        description="保存档案时不再连接希沃校验账号密码并缓存用户信息",
        json_schema_extra={"icon": "CheckBox"},
    )


class ClassIslandConfig(ConfigModel):
    AutoPath: bool = Field(
        default=True,
        title="自动获取路径",
        description="自动获取 ClassIsland 的路径",
    )
    Path: str = Field(
        default="",
        title="自定义路径",
        description="自定义 ClassIsland 的路径",
    )
    DefaultDisplayName: str = Field(
        default="自动登录希沃白板",
        title="默认显示名称",
        description="（重启生效）",
    )
    DefaultPreTime: int = Field(
        default=300,
        ge=0,
        le=1800,
        title="默认提前时长",
        description="（重启生效）",
    )


class UpdateConfig(ConfigModel):
    Mode: UpdateMode = Field(
        default=UpdateMode.CHECK_AND_INSTALL,
        title="更新模式",
        description="设置应用的更新模式",
        json_schema_extra={"icon": "Application"},
    )
    CheckAfterLogin: bool = Field(
        default=True,
        title="登录后检查更新",
        description="登录完成后，尝试按照设置的更新模式检查更新（安装时将会静默）",
        json_schema_extra={"icon": "Megaphone"},
    )
    TargetUpdateChannel: UpdateChannel = Field(
        default=UpdateChannel.RELEASE,
        title="更新通道",
        description="设置应用的更新目标版本（测试通道可能含有不稳定的功能，谨慎使用）",
        json_schema_extra={"icon": "Tag"},
    )
    TargetDownloadSource: DownloadSource = Field(
        default=DownloadSource.AUTO,
        title="下载镜像源设置",
        description="设置更新包的下载镜像源",
        json_schema_extra={"icon": "Download"},
    )

    LastVersion: str = Field(
        default="Unknown",
        title="上个版本",
        description="用于在更新完成后下一次启动应用显示更新成功提示",
        json_schema_extra={"hidden": True},
    )


class DebugConfig(ConfigModel):
    EasterEggEnabled: bool = Field(
        default=False,
        title="启用彩蛋",
        description="唔……似乎某些地方有点不对劲的说喵？",
        json_schema_extra={"icon": "Question"},
    )
    DebugMode: bool = Field(
        default=False,
        title="启用开发选项",
        json_schema_extra={"icon": "DeveloperTools"},
    )
    VerboseLog: bool = Field(
        default=False,
        title="启用诊断日志",
        json_schema_extra={"icon": "DeveloperTools"},
    )
    AlternateFindWindowMethod: bool = Field(default=False)


class InternalConfig(ConfigModel):
    IsOobeCompleted: bool = Field(default=False)
    IsProfilePageNoticeShown: bool = Field(default=False)
    IsAutomationPageNoticeShown: bool = Field(default=False)
    IsEasiNotePatched: bool = Field(default=False)
    LastUpdateCheckTime: datetime | None = Field(default=None)
    HiddenAnnouncementIds: list[str] = Field(default_factory=list)


class StatisticsConfig(ConfigModel):
    # Enabled: bool = Field(default=True)  TODO: 全局开关

    FirstRunDate: datetime = Field(default=datetime.now(UTC))

    LoginCounts: int = Field(default=0)
    LoginSuccessCounts: int = Field(default=0)
    LoginInterruptCounts: int = Field(default=0)

    @computed_field
    @property
    def LoginFailureCounts(self) -> int:
        return self.LoginCounts - self.LoginSuccessCounts - self.LoginInterruptCounts

    ThisInstanceLaunchTime: datetime = Field(default=datetime.now(UTC), exclude=True)
    TotalRunTime: float = Field(default=0)
    TotalLoginTime: float = Field(default=0)
    MaxLoginTime: float = Field(default=0)

    LoginCountsPerAccount: dict[str, int] = Field(default_factory=dict)

    @field_validator("LoginCountsPerAccount", mode="after")
    @classmethod
    def _desensitize_account_keys(cls, value: dict[str, int]) -> dict[str, int]:
        """把遗留明文账号键迁移为脱敏哈希，避免配置落盘泄露真实账号。"""
        if not value:
            return value
        return {
            desensitize_account(key) if not re.fullmatch(r"[0-9a-f]{16}", key) else key: count
            for key, count in value.items()
        }


PAGE_INDEX: dict[str, list[str]] = {
    "SettingsPage": ["Login", "Warning", "Banner", "StatusOverlay", "App", "Experimental"],
    "AutomationPage": ["ClassIsland"],
    "UpdatePage": ["Update"],
}


class Config(ConfigModel):
    # NOTE: 添加配置项后，需在上方 PAGE_INDEX 同步更新以生效

    # SettingsPage
    Login: LoginConfig = Field(default_factory=LoginConfig, title="登录选项")
    Warning: WarningConfig = Field(default_factory=WarningConfig, title="警告弹窗")
    Banner: BannerConfig = Field(default_factory=BannerConfig, title="警示横幅")
    StatusOverlay: StatusOverlayConfig = Field(default_factory=StatusOverlayConfig, title="状态浮窗")
    App: AppConfig = Field(default_factory=AppConfig, title="应用设置")
    Experimental: ExperimentalConfig = Field(default_factory=ExperimentalConfig, title="实验性选项")

    Profile: ProfileConfig = Field(default_factory=ProfileConfig, title="档案设置")
    ClassIsland: ClassIslandConfig = Field(default_factory=ClassIslandConfig, title="ClassIsland 设置")
    Update: UpdateConfig = Field(default_factory=UpdateConfig, title="更新设置")

    # 内部
    Debug: DebugConfig = Field(default_factory=DebugConfig, title="调试选项")
    Internal: InternalConfig = Field(default_factory=InternalConfig, title="内部数据")
    Statistics: StatisticsConfig = Field(default_factory=StatisticsConfig, title="统计数据")

    @staticmethod
    def migrate_config(obj: Any):
        """对旧配置进行额外的迁移"""

        if not isinstance(obj, dict):
            return obj

        version_text = obj.get("Update", {}).get("LastVersion", "Unknown")
        if version_text == "Unknown":
            return obj

        try:
            last_version = Version(version_text)
            assert last_version
        except Exception:
            return obj

        backup_obj = deepcopy(obj)

        try:
            transferred = False
            if last_version <= Version("1.1.3"):  # noqa: SIM102
                transferred = True
                if obj["Login"]["Directly"]:
                    del obj["Login"]["Directly"]
                    obj["Login"]["IsIwb"] = False

            if transferred:
                logger.info(f"已迁移配置 ({last_version} -> {__version__})")

            return obj
        except Exception as e:
            logger.warning(f"迁移失败, 跳过迁移: {e}")
            return backup_obj

    @classmethod
    def load(cls) -> Config:
        path = CONFIG_PATH

        if not path.exists():
            config = cls()
            config.save()
            return config

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            migrated = cls.migrate_config(data)
            cfg = cls(**migrated)
        except Exception as e:
            raise RuntimeError("配置文件解析失败") from e

        return cfg

    def reset_all(self):
        """重置所有配置为默认值并保存"""
        logger.info("正在重置配置为默认值")

        with self.initialize():
            default_instance = Config()
            for name in Config.model_fields:
                default_value = getattr(default_instance, name)
                setattr(self, name, default_value)
        self.save()
        logger.success("已重置")

    def reset_by_path(self, path: str) -> bool:
        """重置指定路径下的配置为默认值并保存

        参数示例：
        - "Login.Timeout.Terminate"（重置单个字段）
        - "Login.Timeout"（将整个子配置重置为默认实例）

        返回重置成功/失败
        """
        logger.info(f"正在重置配置路径: {path}")

        with self.initialize():
            parts = tuple(p for p in path.split(".") if p)
            if not parts:
                raise ValueError("路径不能为空")

            default_instance = Config()
            self_parent: Any = self
            default_parent: Any = default_instance

            try:
                for key in parts[:-1]:
                    if not hasattr(self_parent, key) or not hasattr(default_parent, key):
                        raise AttributeError(f"无效路径 {path}")
                    self_parent = getattr(self_parent, key)
                    default_parent = getattr(default_parent, key)

                final = parts[-1]
                if not hasattr(default_parent, final):
                    raise AttributeError(f"无效路径 {path}")

                default_value = getattr(default_parent, final)
                setattr(self_parent, final, default_value)
            except Exception as e:
                logger.error(f"重置路径 {path} 失败: {e}")
                return False

        self.save()
        logger.success(f"已重置配置路径 {path}")
        return True

    def load_page(self, page: str) -> list[ConfigItem | ConfigGroup]:
        return self.iter_items(only=PAGE_INDEX[page])


@dataclass
class ConfigItem:
    """
    ConfigModel 的单个配置项

    Parameters
    ----------
    path : str
        完整路径，如 "Login.Timeout.Terminate"
    name : str
        字段名，如 "Terminate"
    group : str | None
        字段所属的顶层组路径（如果有），如 "Login.Timeout"
    value : Any
        当前值
    type_ :
        字段类型
    field : FieldInfo
        pydantic 的 FieldInfo，包含 title/description/约束等全部信息
    title : str
        字段标题
    description : str | None
        字段描述
    json_schema_extra : dict[str, Any]
        附加信息
    """

    _root: ConfigModel = field(repr=False)
    _parts: tuple[str, ...] = field(init=False, repr=False)

    path: str
    name: str
    group: str | None
    type_: type[Any]
    field_info: FieldInfo
    title: str
    description: str | None
    json_schema_extra: Any = None
    is_group: bool = False

    def __post_init__(self):
        self._parts = tuple(self.path.split("."))

    @property
    def value(self):
        obj: Any = self._root
        for key in self._parts:
            obj = getattr(obj, key)
        return obj

    @value.setter
    def value(self, new_value: Any):
        self._root.set_by_path(self._parts, new_value)


@dataclass
class ConfigGroup:
    """配置项组"""

    path: str
    name: str
    title: str
    description: str | None
    type_ = ConfigItem
    is_group: bool = True
    json_schema_extra: Any = None
    children: list[ConfigGroup | ConfigItem] = field(default_factory=list)


def _iter_config_items(
    obj: ConfigModel,
    prefix: str = "",
    group: str | None = None,
    root: ConfigModel | None = None,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[ConfigItem | ConfigGroup]:
    """
    从任意 ConfigModel 实例递归地枚举出所有字段，并保留层级结构。

    返回一个列表：
    - 根节点的字段直接存在列表中
    - 子模型作为 ConfigGroup，其子字段存在 children 中
    """
    cls = type(obj)
    result: list[ConfigItem | ConfigGroup] = []
    if root is None:
        root = obj

    for name, field_info in cls.model_fields.items():
        value = getattr(obj, name)
        path = f"{prefix}.{name}" if prefix else name

        if (only and not any(o in path for o in only)) or (exclude and any(e in path for e in exclude)):
            continue

        if (extra := field_info.json_schema_extra) and extra.get("hidden", False):  # type: ignore
            continue

        if isinstance(value, ConfigModel):
            # 递归获取子节点
            children = _iter_config_items(value, prefix=path, group=group, root=root, only=only, exclude=exclude)

            group_node = ConfigGroup(
                path=path,
                name=name,
                title=field_info.title or name,
                description=field_info.description,
                json_schema_extra=field_info.json_schema_extra,
                children=children,
            )
            result.append(group_node)
        else:
            item_node = ConfigItem(
                path=path,
                name=name,
                group=group,
                type_=field_info.annotation or type(value),
                field_info=field_info,
                title=field_info.title or name,
                description=field_info.description,
                json_schema_extra=field_info.json_schema_extra,
                _root=root,
            )
            result.append(item_node)

    return result


config = Config.load()
