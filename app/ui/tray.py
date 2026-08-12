"""System tray icon with the total transfer rate in its tooltip."""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..util.fmt import human_speed
from . import icons
from .i18n import tr


class TrayIcon(QSystemTrayIcon):
    def __init__(self, window) -> None:
        super().__init__(icons.app_icon(), window)
        self.window = window
        menu = QMenu()

        self.show_action = QAction(tr("Show window"), menu)
        self.show_action.triggered.connect(self._show_window)
        self.add_action = QAction(icons.add_icon(), tr("Add URL"), menu)
        self.add_action.triggered.connect(window.add_url)
        self.pause_action = QAction(icons.pause_icon(), tr("Pause All"), menu)
        self.pause_action.triggered.connect(window.controller.pause_all)
        self.resume_action = QAction(icons.download_icon(), tr("Resume All"), menu)
        self.resume_action.triggered.connect(window.controller.resume_all)
        self.exit_action = QAction(tr("Exit"), menu)
        self.exit_action.triggered.connect(window.quit_application)

        menu.addAction(self.show_action)
        menu.addAction(self.add_action)
        menu.addSeparator()
        menu.addAction(self.pause_action)
        menu.addAction(self.resume_action)
        menu.addSeparator()
        menu.addAction(self.exit_action)
        self.setContextMenu(menu)
        self._menu = menu

        self.activated.connect(self._on_activated)
        self.update_tooltip(0.0, 0)

    def _show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_window()

    def update_tooltip(self, speed: float, active: int) -> None:
        if active:
            text = f"IDMClone - {tr('Downloading')}: {active}, {human_speed(speed)}"
        else:
            text = f"IDMClone - {tr('idle')}"
        self.setToolTip(text)
