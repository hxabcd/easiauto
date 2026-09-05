from loguru import logger

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QScroller,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    AvatarWidget,
    BodyLabel,
    CaptionLabel,
    ExpandGroupSettingCard,
    FluentIcon,
    HorizontalSeparator,
    HyperlinkCard,
    ImageLabel,
    SimpleCardWidget,
    SmoothScrollArea,
    SubtitleLabel,
    TitleLabel,
)

from EasiAuto import __version__
from EasiAuto.core.utils import get_resource, get_third_party_libs
from EasiAuto.view.components.tag import PrimaryTagLabel
from EasiAuto.view.tokens import MAX_CONTENT_WIDTH, TEXT_SECONDARY_DARK, TEXT_SECONDARY_LIGHT

_GITHUB_URL = "https://github.com/hxabcd/EasiAuto"

_AUTHOR_LINKS = (
    (FluentIcon.GLOBE, "个人网站", "0xabcd.dev"),
    (FluentIcon.HOME_FILL, "哔哩哔哩主页", "space.bilibili.com/401002238"),
    (FluentIcon.GITHUB, "GitHub 主页", "github.com/hxabcd"),
)

_ACKNOWLEDGEMENTS = (
    "智教联盟 对本项目的宣传",
    "Class-Widget 对本项目代码提供参考",
    "ClassIsland 「自动化」 对本项目提供载体",
    "我的初中英语老师 为本项目提供动机",
)

class _BannerImageLabel(ImageLabel):
    """横幅图片：不固定宽度，最大 720px，按宽高比随可用宽度缩放"""

    _banner_ratio = 1.0

    def setImage(self, image=None):
        super().setImage(image)
        # 从真实图片尺寸计算宽高比，避免被布局收缩后的控件尺寸误导
        pm = self.pixmap()
        if pm.width() > 0:
            self._banner_ratio = pm.height() / pm.width()

    def resizeToWidth(self, width: int):
        """按指定宽度缩放（受最大宽度限制），保持宽高比"""
        width = min(width, MAX_CONTENT_WIDTH)
        if width <= 0:
            return
        self.setScaledSize(QSize(width, round(width * self._banner_ratio)))


class AboutPage(QWidget):
    """设置 - 关于页"""

    def __init__(self):
        super().__init__()
        logger.debug("初始化关于页")
        self.setObjectName("AboutPage")
        self.setStyleSheet("border: none; background-color: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = TitleLabel("关于")
        title.setContentsMargins(36, 8, 0, 12)
        layout.addWidget(title)

        self.scroll_area = SmoothScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        QScroller.grabGesture(self.scroll_area.viewport(), QScroller.ScrollerGestureType.LeftMouseButtonGesture)
        layout.addWidget(self.scroll_area)
        # 窗口尺寸变化时重新计算横幅宽度
        self.scroll_area.installEventFilter(self)
        # 居中容器
        self.scroll_container = QWidget()
        self.scroll_area.setWidget(self.scroll_container)

        self.scroll_container_layout = QHBoxLayout(self.scroll_container)
        # 卡片之外的边距，避免窄窗口下贴边
        self.scroll_container_layout.setContentsMargins(24, 0, 24, 0)
        self.scroll_container_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.content_widget = QWidget()
        self.content_widget.setMaximumWidth(MAX_CONTENT_WIDTH)
        self.scroll_container_layout.addWidget(self.content_widget)

        # 内容容器
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 20)
        self.content_layout.setSpacing(20)

        self.content_layout.addWidget(self._create_banner_section())
        self.content_layout.addWidget(self._create_author_section())
        self.content_layout.addStretch(1)

    def eventFilter(self, obj, event):
        if obj is self.scroll_area and event.type() == QEvent.Type.Resize:
            self._update_banner_width()
        return super().eventFilter(obj, event)

    def _update_banner_width(self):
        """按滚动区可用宽度更新横幅图片宽度（忽略滚动条占位）"""
        margins = self.scroll_container_layout.contentsMargins()
        self.banner_image.resizeToWidth(
            self.scroll_area.width() - margins.left() - margins.right()
        )

    def _create_banner_section(self) -> SimpleCardWidget:
        """产品信息卡片（主视觉图、简介、链接、鸣谢）"""
        self.banner_container = SimpleCardWidget()
        banner_container_layout = QVBoxLayout(self.banner_container)
        banner_container_layout.setContentsMargins(0, 0, 0, 0)
        banner_container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 主视觉图
        _banner_img_src = QPixmap(get_resource("banner.png"))
        self.banner_image = _BannerImageLabel(_banner_img_src)
        self.banner_image.setBorderRadius(8, 8, 0, 0)
        banner_container_layout.addWidget(self.banner_image)

        banner_layout = QVBoxLayout()
        banner_layout.setContentsMargins(24, 0, 24, 12)
        banner_layout.setSpacing(0)
        banner_layout.addSpacing(10)

        # 标题行（应用名 + 版本）
        title_layout = QHBoxLayout()
        title_label = TitleLabel("EasiAuto")
        version_label = PrimaryTagLabel(f"v{__version__}")
        version_label.setBold(True)
        title_layout.addWidget(title_label)
        title_layout.addSpacing(6)
        title_layout.addWidget(version_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        title_layout.addStretch(1)

        banner_layout.addLayout(title_layout)
        banner_layout.addSpacing(2)

        # 应用描述与许可
        banner_layout.addWidget(BodyLabel("一款自动登录希沃白板的小工具"))
        banner_layout.addSpacing(16)

        license_label = CaptionLabel("本项目基于 GNU General Public License v3.0 (GPLv3) 获得许可")
        license_label.setTextColor(QColor(TEXT_SECONDARY_LIGHT), QColor(TEXT_SECONDARY_DARK))
        banner_layout.addWidget(license_label)
        banner_layout.addSpacing(8)
        banner_layout.addWidget(HorizontalSeparator())
        banner_layout.addSpacing(8)

        # 链接
        banner_layout.addWidget(
            self._create_link_card(FluentIcon.GITHUB, "GitHub 仓库", "不妨点个 Star 支持一下？  (≧∇≦)ﾉ★", _GITHUB_URL)
        )
        banner_layout.addSpacing(4)

        # 鸣谢与第三方库
        banner_layout.addWidget(self._create_credits_card())
        banner_layout.addSpacing(4)
        banner_layout.addWidget(self._create_third_party_card())
        banner_layout.addStretch(1)

        banner_container_layout.addLayout(banner_layout)
        return self.banner_container

    def _create_author_section(self) -> SimpleCardWidget:
        """作者信息卡片（头像、昵称、个人链接）"""
        self.author_area = SimpleCardWidget()
        author_layout = QVBoxLayout(self.author_area)
        author_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        author_layout.setContentsMargins(24, 16, 24, 16)

        author_info_layout = QHBoxLayout()

        author_avatar = AvatarWidget(QPixmap(get_resource("author_avatar.jpg")))
        author_avatar.setRadius(24)

        sub_layout = QVBoxLayout()
        sub_layout.setSpacing(0)
        sub_layout.addWidget(SubtitleLabel("HxAbCd"))
        author_content = CaptionLabel("Just be yourself.  >_<")
        author_content.setTextColor(QColor(TEXT_SECONDARY_LIGHT), QColor(TEXT_SECONDARY_DARK))
        sub_layout.addWidget(author_content)

        author_info_layout.addWidget(author_avatar)
        author_info_layout.addSpacing(4)
        author_info_layout.addLayout(sub_layout)
        author_info_layout.addStretch(1)

        author_layout.addLayout(author_info_layout)
        author_layout.addSpacing(4)
        for icon, title, url in _AUTHOR_LINKS:
            author_layout.addWidget(self._create_link_card(icon, title, None, f"https://{url}", "访问"))

        return self.author_area

    def _create_link_card(
        self,
        icon,
        title: str,
        content: str | None,
        url: str,
        text: str = "访问",
    ) -> HyperlinkCard:
        """统一创建外部链接卡片"""
        return HyperlinkCard(icon=icon, title=title, content=content, url=url, text=text)

    def _create_credits_card(self) -> ExpandGroupSettingCard:
        """鸣谢展开卡：致谢文本"""
        credits_card = ExpandGroupSettingCard(icon=FluentIcon.HEART, title="鸣谢", content="特别感谢与支持")
        credits_card.viewLayout.setContentsMargins(16, 8, 16, 12)
        credits_card.viewLayout.setSpacing(6)
        credits_card.addGroupWidget(
            BodyLabel("\n  - ".join(["特别感谢：", *_ACKNOWLEDGEMENTS]) + "\n\n以及——愿意使用 EasiAuto 的你")
        )
        return credits_card

    def _create_third_party_card(self) -> ExpandGroupSettingCard:
        """第三方库展开卡：运行时自动收集的库及版本"""
        third_party_text: str = "\n".join([f"- {item}" for item in get_third_party_libs()])
        third_party_card = ExpandGroupSettingCard(
            icon=FluentIcon.LIBRARY_FILL, title="第三方库", content="本项目使用到的第三方库"
        )
        third_party_card.viewLayout.setContentsMargins(16, 8, 16, 12)
        third_party_card.viewLayout.setSpacing(6)
        third_party_card.addGroupWidget(BodyLabel(third_party_text))
        return third_party_card
