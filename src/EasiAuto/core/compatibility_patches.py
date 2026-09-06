"""第三方库兼容性补丁。

集中放置针对依赖库已知问题的运行时补丁，应在 QApplication 创建后、窗口显示前应用。
"""

from __future__ import annotations

from typing import cast

from loguru import logger

from PySide6.QtCore import QEasingCurve, Qt

from EasiAuto.core.refresh_animation import (
    RefreshDrivenAnimation,
    RefreshFadeInOutAnimation,
    RefreshFastDismissAnimation,
    RefreshFastInvokeAnimation,
    RefreshFluentAnimation,
    RefreshParallelGroup,
    RefreshPointToPointAnimation,
    RefreshScaleSlideAnimation,
    RefreshSoftDismissAnimation,
    RefreshStrongInvokeAnimation,
    make_icon_slide_animation,
)
from EasiAuto.core.utils import get_main_display_refresh_rate

_APPLIED = [False]  # 全部补丁是否已应用


def patch_frameless_window_uaci() -> None:
    """修补 qframelesswindow 在 UIPI 场景下的 GetCursorPos 崩溃。

    背景：当本进程（普通权限，中完整性）与提权子进程（管理员，高完整性）共存时，
    若高完整性窗口（如 UAC 同意窗口）处于前台，本进程在 nativeEvent 中调用
    win32api.GetCursorPos() 会因用户界面特权隔离（UIPI）返回 ERROR_ACCESS_DENIED，
    抛出未捕获的 pywintypes.error 导致崩溃。

    补丁包裹 WindowsFramelessWindowBase.nativeEvent，捕获该错误并降级为默认处理，
    仅影响命中测试的边缘场景，正常情况无副作用。
    """
    try:
        from qframelesswindow.windows import WindowsFramelessWindowBase
    except Exception as e:
        logger.debug(f"qframelesswindow 不可用，跳过 UIPI 补丁: {e}")
        return

    if getattr(WindowsFramelessWindowBase, "_uaci_patched", False):
        return

    original_native_event = WindowsFramelessWindowBase.nativeEvent

    def nativeEvent(self, event_type, message):  # type: ignore[no-untyped-def]
        try:
            return original_native_event(self, event_type, message)
        except Exception as e:
            # 仅吞 GetCursorPos 的拒绝访问（UIPI），其余重新抛出
            if getattr(e, "winerror", None) == 5 and getattr(e, "funcname", "") == "GetCursorPos":
                logger.debug(f"GetCursorPos 被 UIPI 拒绝，已降级处理: {e}")
                return False, 0
            raise

    WindowsFramelessWindowBase.nativeEvent = nativeEvent  # type: ignore[assignment]
    WindowsFramelessWindowBase._uaci_patched = True  # type: ignore[attr-defined]


def patch_smooth_scroll_fps() -> None:
    """全局提升 qfluentwidgets 平滑滚动帧率，跟随主显示器刷新率。

    滚动引擎（FixedStep/Adaptive）默认以 60fps（16ms 定时器）驱动滚轮插值，
    在 Windows 默认定时器精度下实际更慢。此处以主显示器刷新率为 fps，
    使定时器间隔为 1000/fps 毫秒，所有滚动组件（滚动区、下拉、列表）一致受益。
    """
    try:
        from qfluentwidgets.common.smooth_scroll import SmoothScrollEngineBase
    except Exception as e:
        logger.debug(f"qfluentwidgets 不可用，跳过滚动帧率补丁: {e}")
        return

    if getattr(SmoothScrollEngineBase, "_fps_patched", False):
        return

    fps = get_main_display_refresh_rate()
    original_init = SmoothScrollEngineBase.__init__

    def engine_init(self, widget, orient=Qt.Orientation.Vertical):  # type: ignore[no-untyped-def]
        original_init(self, widget, orient)
        self.fps = fps

    SmoothScrollEngineBase.__init__ = engine_init  # type: ignore[assignment]
    SmoothScrollEngineBase._fps_patched = True  # type: ignore[attr-defined]


def patch_expand_card_animation() -> None:
    """将 qfluentwidgets 展开卡片的展开/箭头动画替换为高刷新率驱动。

    ExpandSettingCard.expandAni 与 ExpandButton.rotateAni 均由
    QPropertyAnimation 驱动（实测约 53Hz），替换为跟随主显示器刷新率的
    RefreshDrivenAnimation，覆盖应用内的全部展开设置卡片。
    """
    try:
        from qfluentwidgets.components.settings.expand_setting_card import (
            ExpandButton,
            ExpandSettingCard,
        )
    except Exception as e:
        logger.debug(f"qfluentwidgets 不可用，跳过展开卡片动画补丁: {e}")
        return

    if getattr(ExpandSettingCard, "_expand_animation_patched", False):
        return

    original_card_init = ExpandSettingCard.__init__

    def card_init(self, icon, title, content=None, parent=None):  # type: ignore[no-untyped-def]
        # 先运行原初始化，再替换为高刷新率动画
        # qfw 的 content 类型注解为 str = None，实际可为 None，故 cast
        original_card_init(self, icon, title, cast(str, content), parent)
        old_ani = self.expandAni
        duration = old_ani.duration()
        ani = RefreshDrivenAnimation(self.verticalScrollBar(), b"value", self)
        ani.setDuration(duration if duration > 0 else 200)
        # 原 OutQuad 视觉上接近线性，改用减速更明显的 OutCubic
        ani.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.expandAni = ani
        ani.valueChanged.connect(self._onExpandValueChanged)

    original_button_init = ExpandButton.__init__

    def button_init(self, parent=None):  # type: ignore[no-untyped-def]
        original_button_init(self, parent)
        ani = RefreshDrivenAnimation(self, b"angle", self)
        ani.setDuration(200)
        self.rotateAni = ani

    ExpandSettingCard.__init__ = card_init  # type: ignore[assignment]
    ExpandButton.__init__ = button_init  # type: ignore[assignment]
    ExpandSettingCard._expand_animation_patched = True  # type: ignore[attr-defined]


def patch_pivot_animation() -> None:
    """将 qfluentwidgets 的 Pivot / 导航指示器动画替换为高刷新率驱动。

    两者的指示器动画均为 ScaleSlideAnimation，内部由 QPropertyAnimation
    驱动（实测约 53Hz），替换为跟随主显示器刷新率的 RefreshScaleSlideAnimation。
    """
    try:
        from qfluentwidgets.common import animation as animation_module
        from qfluentwidgets.components.navigation import navigation_widget, pivot, segmented_widget
    except Exception as e:
        logger.debug(f"qfluentwidgets 不可用，跳过 Pivot 动画补丁: {e}")
        return

    if getattr(animation_module, "_scale_slide_patched", False):
        return

    for module in (animation_module, pivot, navigation_widget, segmented_widget):
        module.ScaleSlideAnimation = RefreshScaleSlideAnimation  # type: ignore[attr-defined]

    animation_module._scale_slide_patched = True  # type: ignore[attr-defined]


def patch_remaining_animations() -> None:
    """将 qfluentwidgets 其余 QPropertyAnimation 动画一并替换为高刷新率驱动。

    按模块换绑 QPropertyAnimation / QParallelAnimationGroup / FluentAnimation，
    覆盖菜单、浮出层、InfoBar、遮罩对话框、卡片悬停、导航栏/面板指示器与
    背景悬停渐变等动画，全部跟随主显示器刷新率。
    """
    try:
        from qfluentwidgets.common import animation as animation_module
        from qfluentwidgets.components.dialog_box import mask_dialog_base
        from qfluentwidgets.components.navigation import (
            navigation_bar,
            navigation_panel,
            navigation_widget,
            segmented_widget,
        )
        from qfluentwidgets.components.widgets import card_widget, flyout, info_bar, menu
    except Exception as e:
        logger.debug(f"qfluentwidgets 不可用，跳过剩余动画补丁: {e}")
        return

    if getattr(animation_module, "_remaining_animation_patched", False):
        return

    modules = [
        animation_module,
        card_widget,
        flyout,
        info_bar,
        menu,
        mask_dialog_base,
        navigation_bar,
        navigation_panel,
        navigation_widget,
    ]
    for module in modules:
        module.QPropertyAnimation = RefreshDrivenAnimation  # type: ignore[attr-defined]
        if hasattr(module, "QParallelAnimationGroup"):
            module.QParallelAnimationGroup = RefreshParallelGroup  # type: ignore[attr-defined]

    # FluentAnimation 家族（create() 注册表内的子类也一并换掉）
    animation_module.FluentAnimation = RefreshFluentAnimation  # type: ignore[attr-defined]
    animation_module.FastInvokeAnimation = RefreshFastInvokeAnimation  # type: ignore[attr-defined]
    animation_module.StrongInvokeAnimation = RefreshStrongInvokeAnimation  # type: ignore[attr-defined]
    animation_module.FastDismissAnimation = RefreshFastDismissAnimation  # type: ignore[attr-defined]
    animation_module.SoftDismissAnimation = RefreshSoftDismissAnimation  # type: ignore[attr-defined]
    animation_module.PointToPointAnimation = RefreshPointToPointAnimation  # type: ignore[attr-defined]
    animation_module.FadeInOutAnimation = RefreshFadeInOutAnimation  # type: ignore[attr-defined]

    # 类定义期就绑定的引用需要按名换掉
    navigation_bar.IconSlideAnimation = make_icon_slide_animation(RefreshDrivenAnimation)  # type: ignore[attr-defined]
    for module in (segmented_widget, navigation_widget):
        if hasattr(module, "FluentAnimation"):
            module.FluentAnimation = RefreshFluentAnimation  # type: ignore[attr-defined]

    patch_menu_easing(menu)

    animation_module._remaining_animation_patched = True  # type: ignore[attr-defined]


def patch_menu_easing(menu) -> None:
    """菜单下拉/上拉动画统一改为 OutCubic（原 OutQuad 观感接近线性）。

    基类初始化处设一次曲线（DROP_DOWN/PULL_UP），FADE_IN_* 的 exec 会
    再次写入 OutQuad，因此在其 exec 后重新套用 OutCubic。
    """
    if getattr(menu.MenuAnimationManager, "_menu_easing_patched", False):
        return

    original_init = menu.MenuAnimationManager.__init__

    def manager_init(self, manager_menu):  # type: ignore[no-untyped-def]
        original_init(self, manager_menu)
        self.ani.setEasingCurve(QEasingCurve.Type.OutCubic)

    menu.MenuAnimationManager.__init__ = manager_init  # type: ignore[assignment]

    for cls in (
        menu.FadeInDropDownMenuAnimationManager,
        menu.FadeInPullUpMenuAnimationManager,
    ):
        original_exec = cls.exec

        def exec(self, pos, _original=original_exec):  # type: ignore[no-untyped-def]
            _original(self, pos)
            self.ani.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.opacityAni.setEasingCurve(QEasingCurve.Type.OutCubic)

        cls.exec = exec  # type: ignore[assignment]

    menu.MenuAnimationManager._menu_easing_patched = True  # type: ignore[attr-defined]


def apply_all() -> None:
    """应用全部兼容性补丁（幂等，仅首次生效）"""
    if _APPLIED[0]:
        return
    patch_frameless_window_uaci()
    patch_smooth_scroll_fps()
    patch_expand_card_animation()
    patch_pivot_animation()
    # patch_remaining_animations()
    _APPLIED[0] = True
    logger.debug(f"已应用全部兼容性补丁（平滑滚动 fps={get_main_display_refresh_rate()}）")
