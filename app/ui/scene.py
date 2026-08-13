"""An isometric town that shows what the download list is doing.

One tower per download, as many blocks tall as the transfer is far along; a
truck per active download, moving at a speed taken from the real transfer
rate; clouds drifting; and a sky that follows the clock on the machine. When a
download finishes its tower flashes and throws a handful of blocks into the
air.

It is decoration, and decoration has to be honest about its cost: the timer
only runs while something is actually moving. An idle list draws one static
frame and stops - a download manager that burns a core drawing clouds while
you are not downloading anything would deserve to be uninstalled.

The widget takes plain tuples rather than the controller, so a test can hand
it a made-up town and check what comes out.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPolygon
from PySide6.QtWidgets import QWidget

from ..util.fmt import human_speed
from . import theme, voxel
from .i18n import tr

#: 20 frames a second: smooth enough for drifting clouds, cheap enough that
#: nobody notices it in the process list.
FRAME_MS = 50
#: how many blocks a tower has when a download is finished
MAX_LEVELS = 8
#: the ground plate
GRID_W, GRID_D = 9, 4
#: a truck at this speed crosses the whole scene in about ten seconds
FULL_SPEED = 8 * 1024 * 1024


@dataclass
class Building:
    """One download, as a tower."""

    key: int
    name: str
    percent: float
    speed: float
    done: bool
    hue: int
    #: 0..1, how far the tower has grown towards `percent` - the blocks slide
    #: up rather than teleporting
    shown: float = 0.0
    #: where its truck is along the road, 0..1
    truck: float = 0.0
    #: frames left of the finished-flash
    flash: int = 0


@dataclass
class Spark:
    """A block thrown up when a download finishes."""

    x: float
    y: float
    z: float
    vz: float
    hue: int
    life: int = 24


@dataclass
class Cloud:
    x: float
    y: int
    speed: float
    width: int


#: tower colours, cycled by position: the "many colours" of the brief
HUES = ["accent", "success", "warning", "danger", "muted", "selection"]


class SceneWidget(QWidget):
    """The town. Feed it `update_downloads`, it does the rest."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setMaximumHeight(230)
        self._buildings: list[Building] = []
        self._sparks: list[Spark] = []
        self._clouds = [
            Cloud(x=random.random(), y=18 + i * 14, speed=0.0006 + i * 0.0004,
                  width=3 + i % 3)
            for i in range(4)
        ]
        self._tick = 0
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._advance)

    # ------------------------------------------------------------------ input

    def update_downloads(self, rows: list[tuple[int, str, float, float, bool]]) -> None:
        """`rows` is (id, name, percent, speed, finished), newest last."""
        seen = {b.key: b for b in self._buildings}
        town: list[Building] = []
        for index, (key, name, percent, speed, done) in enumerate(rows[-GRID_W:]):
            building = seen.get(key)
            if building is None:
                building = Building(key=key, name=name, percent=percent,
                                    speed=speed, done=done,
                                    hue=index % len(HUES))
            else:
                was_done = building.done
                building.percent, building.speed, building.done = percent, speed, done
                if done and not was_done:
                    building.flash = 20
                    self._burst(index, building.hue)
            town.append(building)
        self._buildings = town
        self._sync_timer()
        self.update()

    def _burst(self, column: int, hue: int) -> None:
        for _ in range(8):
            self._sparks.append(Spark(
                x=column + random.uniform(-0.3, 0.3),
                y=random.uniform(0, 1.5),
                z=MAX_LEVELS,
                vz=random.uniform(0.15, 0.4),
                hue=hue,
            ))

    @property
    def busy(self) -> bool:
        """Is anything on screen still moving?"""
        return bool(
            self._sparks
            or any(b.flash for b in self._buildings)
            or any(b.speed > 0 and not b.done for b in self._buildings)
            or any(abs(b.shown - b.percent / 100) > 0.005 for b in self._buildings)
        )

    def _sync_timer(self) -> None:
        if self.busy and self.isVisible():
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def showEvent(self, event) -> None:  # pragma: no cover - Qt plumbing
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:  # pragma: no cover - Qt plumbing
        super().hideEvent(event)
        self._timer.stop()

    # -------------------------------------------------------------- animation

    def _advance(self) -> None:
        self._tick += 1
        for building in self._buildings:
            target = max(0.0, min(1.0, building.percent / 100))
            # Ease towards the real value: a tower that jumps looks like a bug.
            building.shown += (target - building.shown) * 0.18
            if building.speed > 0 and not building.done:
                building.truck = (building.truck +
                                  min(0.05, building.speed / FULL_SPEED * 0.05)) % 1.0
            if building.flash:
                building.flash -= 1
        for cloud in self._clouds:
            cloud.x = (cloud.x + cloud.speed) % 1.2
        for spark in list(self._sparks):
            spark.z += spark.vz
            spark.vz -= 0.035
            spark.life -= 1
            if spark.life <= 0 or spark.z < 0:
                self._sparks.remove(spark)
        self._sync_timer()
        self.update()

    # ---------------------------------------------------------------- drawing

    def daylight(self, hour: int | None = None) -> float:
        """0 at midnight, 1 at noon - the sky and the sun ride on this."""
        hour = time.localtime().tm_hour if hour is None else hour
        return (1 - math.cos(hour / 24 * 2 * math.pi)) / 2

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        palette = theme.current()
        light = self.daylight()

        self._paint_sky(painter, palette, light)
        camera = self.camera()
        self._paint_ground(painter, camera, palette)
        self._paint_town(painter, camera, palette)
        self._paint_sparks(painter, camera, palette)
        self._paint_caption(painter, palette)

    def camera(self) -> voxel.Camera:
        """Centred on the ground plate, standing on the bottom edge.

        The grid is a diamond, so its middle is not `GRID_W / 2`: the widest
        point sits where x is largest and y smallest. Centred on the towers
        that actually exist, so two downloads sit in the middle rather than
        hugging the left edge of a nine-wide plate.
        """
        columns = max(1, len(self._buildings))
        span = (columns - 1) - (GRID_D - 1)
        return voxel.Camera(
            origin_x=self.width() // 2 - span * voxel.TILE_W // 2,
            origin_y=self.height() - (GRID_D + 1) * voxel.TILE_H - 30,
        )

    def _paint_sky(self, painter, palette, light: float) -> None:
        top = voxel.shade(palette.color("window"), int(85 + 55 * light))
        painter.fillRect(self.rect(), top)

        if light < 0.35:   # night: a handful of steady stars
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.color("text"))
            random.seed(7)
            for _ in range(26):
                x = random.randint(0, max(1, self.width()))
                y = random.randint(0, max(1, self.height() // 2))
                painter.drawRect(x, y, 2, 2)

        # The sun rides an arc across the top; the moon takes the night shift.
        angle = math.pi * (1 - light) if light >= 0.35 else math.pi * light
        cx = int(self.width() * (0.15 + 0.7 * light))
        cy = int(26 + 30 * abs(math.cos(angle)))
        sky_camera = voxel.Camera(origin_x=cx, origin_y=cy, tile_w=9, tile_h=5)
        voxel.draw_cube(
            painter, sky_camera, 0, 0, 0, height=7,
            color=palette.color("warning") if light >= 0.35 else palette.color("muted"),
        )

        painter.setBrush(voxel.shade(palette.color("surface_alt"), 120))
        for cloud in self._clouds:
            x = int((cloud.x - 0.1) * self.width())
            for block in range(cloud.width):
                painter.drawRect(x + block * 10, cloud.y - (block % 2) * 4, 12, 6)

    def _paint_ground(self, painter, camera, palette) -> None:
        # Only as much ground as there is town to stand on, plus the road.
        columns = max(2, len(self._buildings))
        voxel.draw_floor(
            painter, camera, columns, GRID_D,
            color=voxel.shade(palette.color("surface_alt"), 118),
            alternate=voxel.shade(palette.color("surface_alt"), 96),
        )

    def _paint_town(self, painter, camera, palette) -> None:
        for column, building in enumerate(self._buildings):
            levels = max(1, int(round(building.shown * MAX_LEVELS)))
            colour = palette.color(HUES[building.hue])
            if building.flash:
                colour = voxel.shade(colour, 100 + 60 * (building.flash % 4 > 1))
            voxel.draw_stack(painter, camera, column, 0, levels, color=colour)
            if building.done:
                # A beacon on a finished tower, bobbing one pixel.
                bob = (self._tick // 6) % 2
                voxel.draw_cube(
                    painter, camera, column, 0, levels + 0.35 + bob * 0.08,
                    color=palette.color("success"), height=6,
                )
            self._paint_truck(painter, camera, palette, column, building)

    def _paint_truck(self, painter, camera, palette, column: int, building) -> None:
        if building.done or building.speed <= 0:
            return
        # The road is the row in front of the buildings; the truck runs along
        # it at a pace taken from the real transfer rate.
        y = 1.2 + building.truck * (GRID_D - 2)
        voxel.draw_cube(painter, camera, column, y, 0,
                        color=palette.color("on_accent"), height=5)
        voxel.draw_cube(painter, camera, column, y, 0.4,
                        color=palette.color(HUES[building.hue]), height=5)

    def _paint_sparks(self, painter, camera, palette) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        for spark in self._sparks:
            colour = palette.color(HUES[spark.hue])
            colour.setAlpha(max(40, int(255 * spark.life / 24)))
            point = camera.project(spark.x, spark.y, spark.z)
            painter.setBrush(colour)
            painter.drawRect(QRect(point.x() - 3, point.y() - 3, 6, 6))

    def _caption_text(self) -> str:
        """The line above the town - split out so a test can read it."""
        active = [b for b in self._buildings if not b.done and b.speed > 0]
        if active:
            total = sum(b.speed for b in active)
            return f"{tr('Downloading')}: {len(active)}   {human_speed(total)}"
        if self._buildings:
            return tr("All downloads finished")
        return tr("Nothing downloading - the town is quiet")

    def _paint_caption(self, painter, palette) -> None:
        text = self._caption_text()
        # Deliberately the ordinary UI font: this line is prose, and the
        # bitmap font has no Vietnamese glyphs - "Đang tải" would come out as
        # a row of empty boxes.
        painter.setPen(palette.color("text"))
        painter.drawText(
            self.rect().adjusted(10, 8, -10, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            text,
        )


def rows_from_items(items) -> list[tuple[int, str, float, float, bool]]:
    """Turn controller items into what the scene wants."""
    from ..core.task import TaskState

    rows = []
    for item in items:
        rows.append((
            item.db_id,
            item.filename,
            float(item.percent or 0),
            float(item.speed or 0),
            item.state is TaskState.COMPLETED,
        ))
    return rows
