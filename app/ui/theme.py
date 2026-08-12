"""One palette, one stylesheet, applied to the whole application.

Qt's native Windows style is fine but looks like 2009 and gives no control
over the parts this app leans on - the segment map, the speed graph, the
progress column. So the colours live here as tokens, every widget that paints
by hand asks for them, and the rest is a stylesheet generated from the same
tokens. Two consequences worth the trouble:

* light and dark are the *same* stylesheet with different values, so there is
  no second code path to keep in sync;
* "follow Windows" is a real setting - `QStyleHints.colorScheme` tells us what
  the user picked, and the app repaints when they change it.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from ..util.log import get_logger

log = get_logger(__name__)

AUTO, LIGHT_NAME, DARK_NAME = "auto", "light", "dark"


@dataclass(frozen=True)
class Palette:
    """Every colour the application is allowed to use."""

    name: str
    label: str           # shown in the theme picker
    dark: bool
    window: str          # behind everything
    surface: str         # panels, tables, inputs
    surface_alt: str     # headers, alternating rows, hover
    border: str
    text: str
    muted: str           # secondary labels, disabled text
    accent: str
    accent_hover: str
    on_accent: str
    success: str
    warning: str
    danger: str
    track: str           # progress bar / graph background
    selection: str
    #: 0-255: how solid panels are. Below 255 the window shows through, which
    #: is what makes the glass themes glass.
    opacity: int = 255
    #: Windows 11 backdrop to ask the compositor for: "mica" or "acrylic".
    backdrop: str | None = None

    @property
    def is_dark(self) -> bool:
        return self.dark

    @property
    def translucent(self) -> bool:
        return self.opacity < 255

    def color(self, token: str) -> QColor:
        return QColor(getattr(self, token))

    def alpha(self, token: str, alpha: int) -> QColor:
        c = self.color(token)
        c.setAlpha(alpha)
        return c

    def css(self, token: str, alpha: int | None = None) -> str:
        """A colour for the stylesheet, honouring the theme's opacity."""
        c = self.color(token)
        value = self.opacity if alpha is None else alpha
        if value >= 255:
            return c.name()
        return f"rgba({c.red()}, {c.green()}, {c.blue()}, {value / 255:.3f})"


DARK = Palette(
    name=DARK_NAME, label="Dark", dark=True,
    window="#12141a",
    surface="#191c24",
    surface_alt="#212736",
    border="#2c3345",
    text="#e6e9f0",
    muted="#98a0b3",
    accent="#4f8cff",
    accent_hover="#6ba0ff",
    on_accent="#0b1020",
    success="#3ecf8e",
    warning="#f0a33f",
    danger="#ff6b6b",
    track="#262c3a",
    selection="#24334f",
)

LIGHT = Palette(
    name=LIGHT_NAME, label="Light", dark=False,
    window="#f4f6fa",
    surface="#ffffff",
    surface_alt="#eef1f7",
    border="#d7dce6",
    text="#1b2330",
    muted="#5d6b85",
    accent="#2563eb",
    accent_hover="#1d4ed8",
    on_accent="#ffffff",
    success="#12855f",
    warning="#b45309",
    danger="#c0392b",
    track="#e2e7f0",
    selection="#dbe6ff",
)

#: Blade Runner by way of a download manager: violet night, magenta signage.
CYBERPUNK = Palette(
    name="cyberpunk", label="Cyberpunk", dark=True,
    window="#0d0716",
    surface="#160d26",
    surface_alt="#1f1236",
    border="#3b1f63",
    text="#f2e9ff",
    muted="#a98fc9",
    accent="#ff2e97",
    accent_hover="#ff5fb0",
    on_accent="#12001f",
    success="#00f5d4",
    warning="#ffd166",
    danger="#ff4d6d",
    track="#241340",
    selection="#3a1a5e",
)

#: Pure black with electric cyan - the "neon sign at 3am" look.
NEON = Palette(
    name="neon", label="Neon", dark=True,
    window="#05070a",
    surface="#0b1016",
    surface_alt="#111a24",
    border="#1d3547",
    text="#e8fbff",
    muted="#7fa8bd",
    accent="#00e5ff",
    accent_hover="#5cf2ff",
    on_accent="#00232b",
    success="#39ff14",
    warning="#ffe14d",
    danger="#ff3864",
    track="#0f1d28",
    selection="#0c3a4a",
)

#: Frosted panels over the desktop; Windows 11 supplies the actual blur.
GLASS = Palette(
    name="glass", label="Glass", dark=True,
    window="#0f1420",
    surface="#1a2233",
    surface_alt="#243049",
    border="#3a4763",
    text="#eef2fb",
    muted="#a7b4cd",
    accent="#6ea8fe",
    accent_hover="#8dbcff",
    on_accent="#0a1020",
    success="#5ee2a0",
    warning="#ffcc66",
    danger="#ff7b8a",
    track="#26314a",
    selection="#314060",
    opacity=200,
    backdrop="acrylic",
)

NORD = Palette(
    name="nord", label="Nord", dark=True,
    window="#2e3440",
    surface="#3b4252",
    surface_alt="#434c5e",
    border="#4c566a",
    text="#eceff4",
    muted="#a2adbe",
    accent="#88c0d0",
    accent_hover="#8fbcbb",
    on_accent="#22272f",
    success="#a3be8c",
    warning="#ebcb8b",
    danger="#bf616a",
    track="#434c5e",
    selection="#4c566a",
)

DRACULA = Palette(
    name="dracula", label="Dracula", dark=True,
    window="#282a36",
    surface="#2f3240",
    surface_alt="#383b4a",
    border="#4b4f66",
    text="#f8f8f2",
    muted="#a7abbe",
    accent="#bd93f9",
    accent_hover="#d0aeff",
    on_accent="#1d1e26",
    success="#50fa7b",
    warning="#f1fa8c",
    danger="#ff5555",
    track="#3a3d4d",
    selection="#44475a",
)

#: name -> palette, in the order the picker shows them
THEMES: dict[str, Palette] = {
    p.name: p for p in (LIGHT, DARK, CYBERPUNK, NEON, GLASS, NORD, DRACULA)
}

_current: Palette = LIGHT


def current() -> Palette:
    """The palette in force. Safe to call before `apply`."""
    return _current


def system_is_dark(app: QApplication | None = None) -> bool:
    """What Windows is set to right now."""
    app = app or QApplication.instance()
    if app is None:  # pragma: no cover - only in headless helpers
        return False
    hints = app.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    if scheme is not None:
        try:
            return scheme() == Qt.ColorScheme.Dark
        except (AttributeError, TypeError):  # pragma: no cover - older Qt
            pass
    # Fallback for Qt < 6.5: ask the palette whether the window is darker
    # than the text drawn on it.
    palette = app.palette()
    return palette.color(QPalette.ColorRole.Window).lightness() < palette.color(
        QPalette.ColorRole.WindowText
    ).lightness()


def resolve(preference: str | None, app: QApplication | None = None) -> Palette:
    """Turn a stored preference into a palette; unknown names follow Windows."""
    if preference in THEMES:
        return THEMES[preference]
    return DARK if system_is_dark(app) else LIGHT


def qpalette(p: Palette) -> QPalette:
    """Base colours as a real QPalette.

    Deliberately *not* a `QWidget { background: ... }` stylesheet rule: a
    universal selector makes Qt re-polish every widget in the application on
    every change, which showed up as a four-fold slowdown in the GUI tests.
    The palette does the same job for free; the stylesheet below only shapes
    the widgets that need it.
    """
    palette = QPalette()
    window, surface = p.color("window"), p.color("surface")
    text, muted = p.color("text"), p.color("muted")
    roles = QPalette.ColorRole
    palette.setColor(roles.Window, window)
    palette.setColor(roles.WindowText, text)
    palette.setColor(roles.Base, surface)
    palette.setColor(roles.AlternateBase, p.color("surface_alt"))
    palette.setColor(roles.Text, text)
    palette.setColor(roles.Button, p.color("surface_alt"))
    palette.setColor(roles.ButtonText, text)
    palette.setColor(roles.ToolTipBase, p.color("surface_alt"))
    palette.setColor(roles.ToolTipText, text)
    palette.setColor(roles.Highlight, p.color("selection"))
    palette.setColor(roles.HighlightedText, text)
    palette.setColor(roles.Link, p.color("accent"))
    palette.setColor(roles.PlaceholderText, muted)
    for group in (QPalette.ColorGroup.Disabled,):
        palette.setColor(group, roles.WindowText, muted)
        palette.setColor(group, roles.Text, muted)
        palette.setColor(group, roles.ButtonText, muted)
    return palette


def stylesheet(p: Palette) -> str:
    """Shape and accent, on top of `qpalette`."""
    return f"""
    QToolTip {{
        background: {p.surface_alt};
        color: {p.text};
        border: 1px solid {p.border};
        padding: 4px 6px;
    }}

    /* ---------------------------------------------------------- chrome */
    QMenuBar {{ background: {p.window}; padding: 2px 4px; }}
    QMainWindow, QDialog {{ background: {p.css('window')}; }}
    QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; }}
    QMenuBar::item:selected {{ background: {p.surface_alt}; }}
    QMenu {{
        background: {p.css('surface')};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {p.selection}; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 8px; }}

    QToolBar {{
        background: {p.css('surface')};
        border: 0;
        border-bottom: 1px solid {p.border};
        padding: 6px 8px;
        spacing: 4px;
    }}
    QToolBar QToolButton {{
        border: 1px solid transparent;
        border-radius: 8px;
        padding: 6px 10px;
        color: {p.text};
    }}
    QToolBar QToolButton:hover {{
        background: {p.surface_alt};
        border-color: {p.border};
    }}
    QToolBar QToolButton:pressed {{ background: {p.selection}; }}
    QToolBar QToolButton:disabled {{ color: {p.muted}; }}
    QToolBar::separator {{ background: {p.border}; width: 1px; margin: 6px 6px; }}

    QStatusBar {{ background: {p.css('surface')}; border-top: 1px solid {p.border}; }}
    QStatusBar QLabel {{ color: {p.muted}; padding: 0 6px; }}
    QSplitter::handle {{ background: {p.window}; width: 4px; }}
    QSplitter::handle:hover {{ background: {p.accent}; }}

    /* ----------------------------------------------------------- lists */
    QTreeWidget, QTreeView, QTableView, QListWidget {{
        background: {p.css('surface')};
        alternate-background-color: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: 10px;
        outline: 0;
        selection-background-color: {p.selection};
        selection-color: {p.text};
    }}
    QTreeWidget::item, QListWidget::item {{ padding: 4px; border-radius: 6px; }}
    QListWidget::item {{ padding: 2px 6px; }}
    QTreeWidget::item:hover, QListWidget::item:hover, QTableView::item:hover {{
        background: {p.surface_alt};
    }}
    QTreeWidget::item:selected, QListWidget::item:selected {{
        background: {p.selection};
        color: {p.text};
    }}
    QTableView {{ gridline-color: {p.border}; }}
    QTableView::item {{ padding: 4px 6px; }}
    QHeaderView::section {{
        background: {p.css('surface_alt')};
        color: {p.muted};
        border: 0;
        border-right: 1px solid {p.border};
        border-bottom: 1px solid {p.border};
        padding: 6px 8px;
        font-weight: 600;
    }}
    QHeaderView::section:hover {{ color: {p.text}; }}

    /* ---------------------------------------------------------- inputs */
    QLineEdit, QSpinBox, QComboBox, QTimeEdit, QPlainTextEdit, QTextEdit {{
        background: {p.css('surface')};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 6px 8px;
        selection-background-color: {p.accent};
        selection-color: {p.on_accent};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTimeEdit:focus,
    QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {p.accent}; }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {p.muted}; }}
    QComboBox::drop-down {{ border: 0; width: 20px; }}
    QComboBox QAbstractItemView {{
        background: {p.surface};
        border: 1px solid {p.border};
        selection-background-color: {p.selection};
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QTimeEdit::up-button, QTimeEdit::down-button {{ width: 16px; border: 0; }}

    QCheckBox, QRadioButton {{ spacing: 8px; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {p.border};
        border-radius: 4px;
        background: {p.surface};
    }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {p.accent};
        border-color: {p.accent};
    }}
    QCheckBox::indicator:disabled {{ background: {p.surface_alt}; }}

    /* --------------------------------------------------------- buttons */
    QPushButton {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 6px 14px;
        min-height: 18px;
    }}
    QPushButton:hover {{ border-color: {p.accent}; }}
    QPushButton:pressed {{ background: {p.selection}; }}
    QPushButton:disabled {{ color: {p.muted}; border-color: {p.border}; }}
    QPushButton:default, QPushButton[accent="true"] {{
        background: {p.accent};
        border-color: {p.accent};
        color: {p.on_accent};
        font-weight: 600;
    }}
    QPushButton:default:hover, QPushButton[accent="true"]:hover {{
        background: {p.accent_hover};
    }}

    /* ---------------------------------------------------------- groups */
    QGroupBox {{
        border: 1px solid {p.border};
        border-radius: 10px;
        margin-top: 14px;
        padding: 10px 8px 8px 8px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 6px;
        color: {p.muted};
        font-weight: 600;
    }}
    QTabWidget::pane {{
        border: 1px solid {p.border};
        border-radius: 10px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {p.muted};
        padding: 7px 14px;
        margin-right: 2px;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
    }}
    QTabBar::tab:hover {{ color: {p.text}; }}
    QTabBar::tab:selected {{
        background: {p.surface};
        color: {p.text};
        border: 1px solid {p.border};
        border-bottom-color: {p.surface};
    }}

    /* -------------------------------------------------------- progress */
    QProgressBar {{
        background: {p.track};
        border: 0;
        border-radius: 7px;
        height: 14px;
        text-align: center;
        color: {p.text};
    }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: 7px; }}

    /* ------------------------------------------------------ scrollbars */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle {{ background: {p.border}; border-radius: 5px; min-height: 28px; }}
    QScrollBar::handle:hover {{ background: {p.muted}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """


#: DwmSetWindowAttribute constants (Windows 11 22H2 and later)
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_BACKDROPS = {"none": 1, "mica": 2, "acrylic": 3, "tabbed": 4}


def apply_backdrop(window, palette: Palette | None = None) -> bool:
    """Ask Windows for a blurred backdrop behind `window`.

    This is the real thing - the compositor blurs whatever is behind the
    window - and it only exists on Windows 11 22H2+. Anywhere else the call
    fails harmlessly and the theme still looks like frosted panels, just
    without the blur.
    """
    import sys

    palette = palette or _current
    if sys.platform != "win32":  # pragma: no cover - Windows only feature
        return False
    try:
        import ctypes

        hwnd = int(window.winId())
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1 if palette.is_dark else 0)
        dwm.DwmSetWindowAttribute(
            hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), 4
        )
        kind = ctypes.c_int(_BACKDROPS.get(palette.backdrop or "none", 1))
        result = dwm.DwmSetWindowAttribute(
            hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(kind), 4
        )
        return result == 0
    except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
        log.debug("no system backdrop: %s", exc)
        return False


def apply(app: QApplication, preference: str | None = AUTO) -> Palette:
    """Paint the application with `preference` and remember the result."""
    global _current
    _current = resolve(preference, app)
    app.setPalette(qpalette(_current))
    app.setStyleSheet(stylesheet(_current))
    for window in app.topLevelWidgets():
        # Translucency has to be on before the backdrop shows through, and
        # off again when switching back to a solid theme.
        window.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, _current.translucent
        )
        if window.isVisible():
            apply_backdrop(window, _current)
    return _current
