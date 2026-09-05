import time

from loguru import logger

from EasiAuto.core.utils import Point, get_resource, get_scale
from EasiAuto.models.config import config

from .base import LoginError, PyAutoGuiBaseAutomator


class CVAutomator(PyAutoGuiBaseAutomator):
    """通过识别图像登录"""

    def __init__(self, account: str, password: str) -> None:
        super().__init__(account, password)

        self.path_suffix: str = ""
        if not config.Login.IsIwb:
            self.path_suffix += "_direct"
        if config.Login.Is4K:
            self.path_suffix += "_4k"

    def find_control(self, img_name: str, ext_name: str = "png", _assert: bool = False) -> Point:
        import pyautogui

        img = get_resource(f"EasiNoteUI/{img_name}{self.path_suffix}.{ext_name}")

        try:
            control = pyautogui.locateCenterOnScreen(img)
            assert control is not None
        except (pyautogui.ImageNotFoundException, AssertionError) as e:
            raise LoginError(f"未识别到控件: {img_name}") from e

        return Point(control.x, control.y)

    def login(self):
        scale = get_scale()

        # 进入登录界面
        self.check_interruption()
        if config.Login.IsIwb:
            self.update_progress("进入登录界面")

            self.click(172 * scale, 1044 * scale)
            time.sleep(config.Login.Timeout.EnterLoginUI)

        # 切换至账号登录页
        self.check_interruption()
        self.update_progress("切换至账号登录页")

        try:
            account_login_button = self.find_control("account_login_button")
            self.click(account_login_button)
            time.sleep(config.Login.Timeout.SwitchTab)
        except LoginError:
            logger.warning("未能识别到账号登录按钮, 尝试识别已选中样式")
            account_login_button = self.find_control("account_login_button")

        # 输入账号
        self.check_interruption()
        self.update_progress("输入账号")

        self.click(account_login_button.x, account_login_button.y + 70 * scale)
        self.input(self.account)

        # 输入密码
        self.check_interruption()
        self.update_progress("输入密码")

        self.click(account_login_button.x, account_login_button.y + 134 * scale)
        self.input(self.password, is_secret=True)

        # 勾选同意用户协议
        self.check_interruption()
        self.update_progress("勾选同意用户协议")

        agree_checkbox = self.find_control("agreement_checkbox")
        self.click(agree_checkbox)

        # 点击登录按钮
        self.check_interruption()
        self.update_progress("点击登录按钮")

        self.press("enter")
