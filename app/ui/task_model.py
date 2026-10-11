"""Table model + progress-bar delegate for the download list."""

from __future__ import annotations

import time
import unicodedata
from datetime import datetime

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRectF,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableView

from ..core.task import TaskState
from ..util.fmt import human_duration, human_size, human_speed
from . import icons, theme
from .controller import Controller, DownloadItem
from .i18n import tr

COL_NAME, COL_SIZE, COL_STATUS, COL_LEFT, COL_SPEED, COL_ADDED = range(6)
COLUMNS = ("Name", "Size", "Status", "Left", "Speed", "Added")

ITEM_ROLE = Qt.ItemDataRole.UserRole + 1
PERCENT_ROLE = Qt.ItemDataRole.UserRole + 2
SORT_ROLE = Qt.ItemDataRole.UserRole + 3
#: sidebar entries: how many downloads they hold
COUNT_ROLE = Qt.ItemDataRole.UserRole + 4

def state_color(state: TaskState) -> QColor | None:
    """Status colours come from the active theme, not from constants."""
    palette = theme.current()
    return {
        TaskState.COMPLETED: palette.color("success"),
        TaskState.ERROR: palette.color("danger"),
        TaskState.PAUSED: palette.color("warning"),
        TaskState.CANCELLED: palette.color("muted"),
    }.get(state)


class DownloadTableModel(QAbstractTableModel):
    def __init__(self, controller: Controller, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        # Seed from whatever the controller already restored from SQLite - the
        # model is usually built after Controller.start() has run.
        self._items: list[DownloadItem] = controller.items()
        self._row_by_id: dict[int, int] = {}
        self._reindex()

        controller.itemAdded.connect(self.add_item)
        controller.itemChanged.connect(self.update_item)
        controller.itemRemoved.connect(self.remove_item)

    # ------------------------------------------------------- Qt model plumbing

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return tr(COLUMNS[section])

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self._items[index.row()]
        column = index.column()

        if role == ITEM_ROLE:
            return item
        if role == PERCENT_ROLE:
            return item.percent
        if role == SORT_ROLE:
            return self._sort_key(item, column)
        if role == Qt.ItemDataRole.ForegroundRole and column == COL_STATUS:
            return state_color(item.state)
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.error or item.url
        if role == Qt.ItemDataRole.DecorationRole and column == COL_NAME:
            return file_icon(item.category)
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (
            COL_SIZE, COL_LEFT, COL_SPEED
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None

        if column == COL_NAME:
            return item.filename
        if column == COL_SIZE:
            # A dash, not "?": not knowing the size yet is normal, not a question.
            return human_size(item.size) if item.size else "\u2014"
        if column == COL_STATUS:
            return self.status_text(item)
        if column == COL_LEFT:
            if item.state is TaskState.DOWNLOADING and item.eta is not None:
                return human_duration(item.eta)
            if item.size and item.state is not TaskState.COMPLETED:
                return human_size(max(0, item.size - item.downloaded))
            return ""
        if column == COL_SPEED:
            return human_speed(item.speed) if item.speed > 0 else ""
        if column == COL_ADDED:
            return friendly_time(item.added_at)
        return None

    @staticmethod
    def status_text(item: DownloadItem) -> str:
        if item.state is TaskState.ERROR:
            return f"{tr('error')}: {item.error or ''}".strip(": ")
        if item.state in (TaskState.DOWNLOADING, TaskState.PAUSED) and item.size:
            return f"{item.percent:.1f}%"
        return tr(item.state.value)

    @staticmethod
    def _sort_key(item: DownloadItem, column: int):
        return {
            COL_NAME: item.filename.lower(),
            COL_SIZE: item.size or 0,
            COL_STATUS: item.percent,
            COL_LEFT: item.eta if item.eta is not None else float("inf"),
            COL_SPEED: item.speed,
            COL_ADDED: item.added_at,
        }[column]

    # ------------------------------------------------------------- mutations

    def add_item(self, item: DownloadItem) -> None:
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(item)
        self._row_by_id[item.db_id] = row
        self.endInsertRows()

    def update_item(self, item: DownloadItem) -> None:
        row = self._row_by_id.get(item.db_id)
        if row is None:
            return
        self.dataChanged.emit(
            self.index(row, 0), self.index(row, len(COLUMNS) - 1)
        )

    def remove_item(self, db_id: int) -> None:
        row = self._row_by_id.get(db_id)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._items[row]
        self._reindex()
        self.endRemoveRows()

    def _reindex(self) -> None:
        self._row_by_id = {item.db_id: i for i, item in enumerate(self._items)}

    def item_at(self, row: int) -> DownloadItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None


def fold(text: str) -> str:
    """Lower case without accents: "Báo cáo Q4" -> "bao cao q4".

    Vietnamese file names are typed both ways, and a search that only finds
    "báo cáo" when the user types every mark is one people stop using. "đ"
    is a letter of its own, not a d with a mark, so it is mapped by hand.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return bare.replace("đ", "d")


def search_words(text: str) -> list[str]:
    return [word for word in fold(text).split() if word]


def matches_search(item: DownloadItem, words: list[str]) -> bool:
    """Every word must appear somewhere: name, address, source page, error."""
    if not words:
        return True
    haystack = fold(" ".join(
        part for part in (item.filename, item.url, item.referer or "", item.error or "")
        if part
    ))
    return all(word in haystack for word in words)


class DownloadFilterProxy(QSortFilterProxyModel):
    """Left-hand tree selection: all / unfinished / finished / category / queue."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self._kind = "all"
        self._value = ""
        self._words: list[str] = []

    def set_filter(self, kind: str, value: str = "") -> None:
        self._kind = kind
        self._value = value
        self.invalidateFilter()

    def set_search(self, text: str) -> None:
        self._words = search_words(text)
        self.invalidateFilter()

    @property
    def searching(self) -> bool:
        return bool(self._words)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        item = model.item_at(source_row)
        if item is None:
            return False
        if not matches_search(item, self._words):
            return False
        return matches_filter(item, self._kind, self._value)


def matches_filter(item: DownloadItem, kind: str, value: str = "") -> bool:
    """Whether `item` belongs in the sidebar entry (`kind`, `value`)."""
    if kind == "unfinished":
        return item.state is not TaskState.COMPLETED
    if kind == "finished":
        return item.state is TaskState.COMPLETED
    if kind == "category":
        return item.category == value
    if kind == "queue":
        return str(item.queue_id or "") == str(value)
    return True


class CountDelegate(QStyledItemDelegate):
    """A sidebar entry with its count right-aligned in the same cell."""

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        count = index.data(COUNT_ROLE)
        if not count:
            return
        palette = theme.current()
        painter.save()
        painter.setPen(palette.color("muted"))
        rect = QRectF(option.rect).adjusted(0, 0, -10, 0)
        painter.drawText(
            rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(count)
        )
        painter.restore()


class ProgressDelegate(QStyledItemDelegate):
    """Draws the Status column as a progress bar, like IDM's list.

    Painted by hand rather than through the style so the bar matches the
    theme exactly: a rounded track, an accent fill that turns green when the
    file is finished, and the percentage on top of it.
    """

    RADIUS = 6

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item: DownloadItem | None = index.data(ITEM_ROLE)
        if item is None or item.state in (TaskState.ERROR, TaskState.CANCELLED) or not item.size:
            super().paint(painter, option, index)
            return

        # The cell's own background first - selection, hover, the row line -
        # so the bar sits in the row instead of punching a hole in it.
        base = QStyleOptionViewItem(option)
        self.initStyleOption(base, index)
        base.text = ""
        style = option.widget.style() if option.widget is not None else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, base, painter, option.widget)

        palette = theme.current()
        if palette.iso:
            self._paint_iso(painter, option, item, palette)
            return
        if palette.pixel:
            self._paint_pixel(painter, option, item, palette)
            return
        height = min(20.0, max(14.0, option.rect.height() - 12.0))
        rect = QRectF(option.rect).adjusted(8, 0, -8, 0)
        rect.setTop(option.rect.center().y() - height / 2 + 0.5)
        rect.setHeight(height)
        radius = height / 2
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.color("track"))
        painter.drawRoundedRect(rect, radius, radius)

        fraction = max(0.0, min(1.0, item.percent / 100.0))
        done = item.state is TaskState.COMPLETED
        paused = item.state is TaskState.PAUSED
        fill = palette.color("success" if done else "warning" if paused else "accent")
        filled = QRectF(rect)
        if fraction > 0:
            filled.setWidth(max(rect.height(), rect.width() * fraction))
            painter.setBrush(fill)
            painter.drawRoundedRect(filled, radius, radius)
        else:
            filled.setWidth(0)

        # The percentage in two colours: readable on the fill and on the
        # track alike. One colour over both was unreadable half the time -
        # dark text on a dark accent, or light text on a light track.
        text = DownloadTableModel.status_text(item)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        on_fill = QPainterPath()
        on_fill.addRoundedRect(filled, radius, radius)
        on_track = QPainterPath()
        on_track.addRect(QRectF(option.rect))
        on_track = on_track.subtracted(on_fill)
        for clip, colour in ((on_track, palette.color("text")),
                             (on_fill, palette.color("on_accent"))):
            painter.save()
            painter.setClipPath(clip)
            painter.setPen(QPen(colour))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()
        painter.restore()


    #: isometric mode: one block every ISO_STEP px, drawn back to front
    ISO_STEP = 9

    def _paint_iso(self, painter, option, item, palette) -> None:
        """The bar as a row of blocks standing on the row, seen from above.

        Drawn right to left so each block overlaps the one behind it, which is
        the whole of the painter's algorithm at this scale.
        """
        from . import voxel

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        full = option.rect.adjusted(8, 0, -8, 0)
        text_width = 54
        track = full.adjusted(0, 0, -text_width, 0)

        blocks = max(1, track.width() // self.ISO_STEP)
        fraction = max(0.0, min(1.0, item.percent / 100.0))
        lit = int(round(blocks * fraction))
        done = item.state is TaskState.COMPLETED
        base = palette.color("success" if done else "accent")
        shades = voxel.ramp(base, max(2, lit))
        # Along the isometric x axis a row would slope downwards - fine for a
        # town, wrong inside a 24 pixel table row. So each block is placed by
        # moving the camera sideways instead: a row of dice, all at one depth.
        baseline = option.rect.center().y() - 3
        for index in range(blocks):
            filled = index < lit
            camera = voxel.Camera(
                origin_x=track.left() + index * self.ISO_STEP + 4,
                origin_y=baseline,
                tile_w=self.ISO_STEP // 2, tile_h=max(2, self.ISO_STEP // 4),
                voxel_h=7,
            )
            voxel.draw_cube(
                painter, camera, 0, 0, 0,
                color=shades[index] if filled else palette.color("surface_alt"),
                height=7 if filled else 3,
            )

        status = DownloadTableModel.status_text(item)
        painter.setPen(QPen(palette.color("text")))
        # "87.4%" gets the bitmap font; "Hoàn tất" and "已完成" must not.
        painter.setFont(theme.font_for(status, 9))
        painter.drawText(
            option.rect.adjusted(0, 0, -8, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            status,
        )
        painter.restore()

    #: pixel mode: one lit cell every CELL px
    CELL = 8

    def _paint_pixel(self, painter, option, item, palette) -> None:
        """A row of cells instead of a pill - the same bar a health meter uses.

        The percentage is drawn beside the cells rather than on top of them:
        text over a two-colour blocky bar is the one place this look becomes
        unreadable.
        """
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(Qt.PenStyle.NoPen)
        full = option.rect.adjusted(6, 8, -6, -8)
        text_width = 52
        track = full.adjusted(0, 0, -text_width, 0)

        painter.setBrush(palette.color("track"))
        painter.drawRect(track)

        cells = max(1, track.width() // self.CELL)
        fraction = max(0.0, min(1.0, item.percent / 100.0))
        lit = int(round(cells * fraction))
        done = item.state is TaskState.COMPLETED
        painter.setBrush(palette.color("success" if done else "accent"))
        for index in range(lit):
            painter.drawRect(
                track.left() + index * self.CELL + 1, track.top() + 1,
                self.CELL - 2, track.height() - 2,
            )
        painter.setPen(QPen(palette.color("border"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(track)

        status = DownloadTableModel.status_text(item)
        painter.setPen(QPen(palette.color("text")))
        # "87.4%" gets the bitmap font; "Hoàn tất" and "已完成" must not.
        painter.setFont(theme.font_for(status, 9))
        painter.drawText(
            option.rect.adjusted(0, 0, -8, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            status,
        )
        painter.restore()


#: (theme, category) -> icon; glyphs are painted in the theme's accent
_ICONS: dict[tuple[str, str], QIcon] = {}


def file_icon(category: str) -> QIcon:
    """The category glyph shown beside each file name, painted once per theme."""
    key = (theme.current().name, category)
    icon = _ICONS.get(key)
    if icon is None:
        icon = _ICONS[key] = icons.category_icon(category)
    return icon


def friendly_time(timestamp: float, now: float | None = None) -> str:
    """"Today 14:05", "Yesterday 09:12", then the date - what a list wants.

    The full date and time on every row was mostly noise: almost everything
    in the list was added today or yesterday.
    """
    when = datetime.fromtimestamp(timestamp)
    today = datetime.fromtimestamp(now if now is not None else time.time()).date()
    days = (today - when.date()).days
    if days == 0:
        return f"{tr('Today')} {when:%H:%M}"
    if days == 1:
        return f"{tr('Yesterday')} {when:%H:%M}"
    if 0 < days < 7:
        return f"{tr(when.strftime('%A'))} {when:%H:%M}"
    if when.year == today.year:
        return when.strftime("%d/%m %H:%M")
    return when.strftime("%d/%m/%Y")


class DownloadTable(QTableView):
    """The download list, with a word to say when it has nothing to show.

    An empty table is a grey void that teaches nothing; this one says how to
    add the first download - or, while searching, that nothing matched.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        #: () -> (title, hint); the window decides, it knows the filter
        self.empty_text = lambda: (tr("No downloads yet"), "")

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        model = self.model()
        if model is None or model.rowCount() > 0:
            return
        title, hint = self.empty_text()
        palette = theme.current()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        area = QRectF(self.viewport().rect())
        font = painter.font()
        big = font.pointSizeF() * 1.25 if font.pointSizeF() > 0 else 12
        font.setPointSizeF(big)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(palette.color("text"))
        middle = area.center().y()
        painter.drawText(
            QRectF(area.left(), middle - 34, area.width(), 28),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, title,
        )
        if hint:
            font.setBold(False)
            font.setPointSizeF(big / 1.25)
            painter.setFont(font)
            painter.setPen(palette.color("muted"))
            painter.drawText(
                QRectF(area.left() + 24, middle, area.width() - 48, 60),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                | Qt.TextFlag.TextWordWrap, hint,
            )
        painter.end()


def elapsed_since(timestamp: float | None) -> str:
    if not timestamp:
        return ""
    return human_duration(time.time() - timestamp)
