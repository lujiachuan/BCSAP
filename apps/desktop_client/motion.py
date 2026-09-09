"""少量、可关闭的界面过渡控制。"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, QSettings
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from apps.desktop_client.ui_tokens import METERS


class PageTransitionController(QObject):
    """复用单个淡入动画；切页时会停止上一次动画。"""

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._animation: QPropertyAnimation | None = None
        self._effect: QGraphicsOpacityEffect | None = None

    @property
    def reduced(self) -> bool:
        return self._settings.value("appearance/reduceMotion", False, type=bool)

    def set_reduced(self, reduced: bool) -> None:
        self._settings.setValue("appearance/reduceMotion", reduced)
        if reduced:
            self.stop()

    def fade_in(self, widget: QWidget) -> None:
        self.stop()
        if self.reduced or not widget.isVisible():
            return
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.12)
        widget.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(METERS["motion"]["base"])
        animation.setStartValue(0.12)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(self.stop)
        self._effect = effect
        self._animation = animation
        animation.start()

    def stop(self) -> None:
        animation, effect = self._animation, self._effect
        self._animation = None
        self._effect = None
        if animation is not None:
            animation.stop()
            animation.deleteLater()
        if effect is not None:
            owner = effect.parent()
            if isinstance(owner, QWidget) and owner.graphicsEffect() is effect:
                # Qt 在移除 graphicsEffect 时拥有并销毁 effect，不能再 deleteLater。
                owner.setGraphicsEffect(None)
            else:
                effect.deleteLater()


__all__ = ["PageTransitionController"]
