"""Drawing isometric blocks with QPainter.

The whole trick of pixel isometry is the 2:1 tile: a cell is twice as wide as
it is tall, so a 45-degree line advances two pixels across for every one down
and lands exactly on pixel centres. Anything else and the edges shimmer.

A cube is three flat quadrilaterals - top, left, right - painted in three
shades of one colour. That is all the "3D" there is here, and it is enough:
the eye reads a light top and a dark right face as a lit solid immediately.

Everything takes integer pixels and paints with anti-aliasing off, for the
same reason the rest of the pixel theme does: a soft edge on a voxel looks
like a mistake rather than a style.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint
from PySide6.QtGui import QBrush, QColor, QPainter, QPolygon

#: half-width and half-height of one tile. 2:1 is the classic ratio.
TILE_W = 16
TILE_H = 8
#: how tall one voxel stands, in pixels
VOXEL_H = 12

#: how much lighter the top face is, and how much darker the right one
TOP_LIGHTER = 135
LEFT_SHADE = 100
RIGHT_DARKER = 62


def shade(color: QColor, factor: int) -> QColor:
    """`factor` is a percentage: 135 lightens, 62 darkens."""
    if factor >= 100:
        return color.lighter(factor)
    return color.darker(int(10000 / max(1, factor)))


@dataclass(frozen=True)
class Camera:
    """Where the grid origin sits on screen, and how big a tile is."""

    origin_x: int = 0
    origin_y: int = 0
    tile_w: int = TILE_W
    tile_h: int = TILE_H
    voxel_h: int = VOXEL_H

    def project(self, x: float, y: float, z: float = 0.0) -> QPoint:
        """Grid coordinates to the screen point at the *top centre* of a cube.

        x grows to the lower right, y to the lower left, z straight up - the
        arrangement every isometric game has used since Zaxxon.
        """
        screen_x = self.origin_x + int((x - y) * self.tile_w)
        screen_y = self.origin_y + int((x + y) * self.tile_h - z * self.voxel_h)
        return QPoint(screen_x, screen_y)

    def depth(self, x: float, y: float, z: float = 0.0) -> float:
        """Painter's-algorithm sort key: draw smaller values first."""
        return x + y + z * 0.001


def cube_faces(camera: Camera, x: float, y: float, z: float = 0.0,
               height: int | None = None) -> tuple[QPolygon, QPolygon, QPolygon]:
    """The three visible faces of one cube, as polygons."""
    h = camera.voxel_h if height is None else height
    w, t = camera.tile_w, camera.tile_h
    centre = camera.project(x, y, z)
    cx, cy = centre.x(), centre.y()

    top = QPolygon([
        QPoint(cx, cy - t), QPoint(cx + w, cy),
        QPoint(cx, cy + t), QPoint(cx - w, cy),
    ])
    left = QPolygon([
        QPoint(cx - w, cy), QPoint(cx, cy + t),
        QPoint(cx, cy + t + h), QPoint(cx - w, cy + h),
    ])
    right = QPolygon([
        QPoint(cx + w, cy), QPoint(cx, cy + t),
        QPoint(cx, cy + t + h), QPoint(cx + w, cy + h),
    ])
    return top, left, right


def draw_cube(painter: QPainter, camera: Camera, x: float, y: float,
              z: float = 0.0, *, color: QColor, height: int | None = None,
              outline: QColor | None = None) -> None:
    """One block, lit from the upper left."""
    top, left, right = cube_faces(camera, x, y, z, height)
    painter.setPen(Qt_NoPen)
    for polygon, factor in (
        (left, LEFT_SHADE), (right, RIGHT_DARKER), (top, TOP_LIGHTER)
    ):
        painter.setBrush(QBrush(shade(color, factor)))
        painter.drawPolygon(polygon)
    if outline is not None:
        painter.setPen(outline)
        painter.setBrush(Qt_NoBrush)
        painter.drawPolygon(top)


def draw_stack(painter: QPainter, camera: Camera, x: float, y: float,
               levels: int, *, color: QColor, top_color: QColor | None = None,
               outline: QColor | None = None, graded: bool = True) -> None:
    """A tower `levels` cubes tall, bottom first so the top overlaps it.

    Each level gets its own shade by default. Painted in one flat colour the
    stack reads as a smooth prism - the seams between cubes land exactly on
    the faces below them and disappear - and the whole point of voxels is that
    you can count the blocks.
    """
    levels = max(0, levels)
    shades = ramp(color, levels) if graded and levels > 1 else [color] * levels
    edge = outline if outline is not None else shade(color, 55)
    for level in range(levels):
        highest = level == levels - 1
        draw_cube(
            painter, camera, x, y, level,
            color=(top_color or shades[level]) if highest else shades[level],
            outline=edge,
        )


def draw_floor(painter: QPainter, camera: Camera, width: int, depth: int, *,
               color: QColor, alternate: QColor | None = None) -> None:
    """A checkered ground plane, far corner first."""
    painter.setPen(Qt_NoPen)
    for total in range(width + depth - 1):
        for x in range(width):
            y = total - x
            if not 0 <= y < depth:
                continue
            tile = color if (x + y) % 2 == 0 else (alternate or color)
            top, _left, _right = cube_faces(camera, x, y, 0)
            painter.setBrush(QBrush(tile))
            painter.drawPolygon(top)


def ramp(base: QColor, steps: int) -> list[QColor]:
    """`steps` shades from dark to light - the "many colours" of a voxel scene
    come from one hue at several levels, not from a random palette."""
    if steps <= 1:
        return [base]
    return [
        shade(base, int(70 + 70 * index / (steps - 1)))
        for index in range(steps)
    ]


# Imported lazily-ish: keeping the Qt enum names local makes the drawing code
# above read like geometry rather than like Qt.
from PySide6.QtCore import Qt as _Qt  # noqa: E402

Qt_NoPen = _Qt.PenStyle.NoPen
Qt_NoBrush = _Qt.BrushStyle.NoBrush
