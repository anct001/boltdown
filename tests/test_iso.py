"""The isometric drawing kit and the animated town.

The geometry is checked as geometry - projection, face order, shading - rather
than by looking at pictures, and the town is checked for the two things that
decide whether it is a feature or a nuisance: that it shows what is actually
downloading, and that it stops animating when nothing is.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from app.ui import scene as scene_mod
from app.ui import theme, voxel


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# -------------------------------------------------------------- the projection


def test_a_tile_is_twice_as_wide_as_it_is_tall():
    """2:1 is what makes a 45 degree edge land on whole pixels."""
    assert voxel.TILE_W == 2 * voxel.TILE_H


def test_the_three_axes_go_where_isometry_says_they_go():
    camera = voxel.Camera(origin_x=100, origin_y=100)
    origin = camera.project(0, 0, 0)
    right = camera.project(1, 0, 0)
    left = camera.project(0, 1, 0)
    up = camera.project(0, 0, 1)

    assert right.x() > origin.x() and right.y() > origin.y(), "+x is right and down"
    assert left.x() < origin.x() and left.y() > origin.y(), "+y is left and down"
    assert up.x() == origin.x() and up.y() < origin.y(), "+z is straight up"


def test_depth_orders_far_before_near():
    camera = voxel.Camera()
    far = camera.depth(0, 0)
    near = camera.depth(3, 2)
    assert far < near, "the painter has to lay down the far tile first"


def test_a_cube_has_a_top_and_two_sides_that_meet_at_the_front_corner():
    camera = voxel.Camera(origin_x=0, origin_y=0)
    top, left, right = voxel.cube_faces(camera, 0, 0, 0)
    assert top.count() == 4 and left.count() == 4 and right.count() == 4

    front = max((top.at(i) for i in range(4)), key=lambda p: p.y())
    assert any(left.at(i) == front for i in range(4))
    assert any(right.at(i) == front for i in range(4))

    top_y = min(top.at(i).y() for i in range(4))
    side_y = min(left.at(i).y() for i in range(4))
    assert top_y < side_y, "the top face sits above the walls"


def test_the_faces_are_lit_from_one_side():
    base = QColor("#4080c0")
    top = voxel.shade(base, voxel.TOP_LIGHTER)
    left = voxel.shade(base, voxel.LEFT_SHADE)
    right = voxel.shade(base, voxel.RIGHT_DARKER)
    assert top.lightness() > left.lightness() > right.lightness()


def test_the_ramp_climbs_from_dark_to_light():
    shades = voxel.ramp(QColor("#3ae374"), 6)
    assert len(shades) == 6
    lightness = [c.lightness() for c in shades]
    assert lightness == sorted(lightness)
    assert lightness[0] < lightness[-1]
    assert voxel.ramp(QColor("#3ae374"), 1) == [QColor("#3ae374")]


def paint(width: int, height: int, draw) -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("black"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    draw(painter)
    painter.end()
    return pixmap


def test_a_taller_stack_covers_more_of_the_canvas(qapp):
    camera = voxel.Camera(origin_x=60, origin_y=140)

    def inked(levels: int) -> int:
        pixmap = paint(140, 160, lambda p: voxel.draw_stack(
            p, camera, 0, 0, levels, color=QColor("#3ae374")
        ))
        image = pixmap.toImage()
        return sum(
            1 for x in range(image.width()) for y in range(image.height())
            if image.pixelColor(x, y).lightness() > 20
        )

    assert inked(1) < inked(3) < inked(6)
    assert inked(0) == 0


def test_each_level_of_a_stack_gets_its_own_shade(qapp):
    """Painted in one flat colour a stack reads as a smooth prism."""
    camera = voxel.Camera(origin_x=60, origin_y=140)
    pixmap = paint(140, 160, lambda p: voxel.draw_stack(
        p, camera, 0, 0, 5, color=QColor("#3ae374")
    ))
    image = pixmap.toImage()
    shades = {
        image.pixelColor(x, y).name()
        for x in range(image.width()) for y in range(image.height())
        if image.pixelColor(x, y).lightness() > 20
    }
    assert len(shades) >= 5, f"only {len(shades)} shades - the seams vanish"


# ----------------------------------------------------------------- the theme


def test_the_isometric_theme_is_a_pixel_theme_too():
    assert "iso" in theme.THEMES
    assert theme.ISO.iso is True
    assert theme.ISO.pixel is True, "it wants the square corners as well"
    assert [p.name for p in theme.THEMES.values() if p.iso] == ["iso"]
    assert theme.PIXEL.iso is False


# ------------------------------------------------------------------ the town


def rows(*specs):
    return [
        (key, f"file{key}.bin", percent, speed, done)
        for key, percent, speed, done in specs
    ]


def test_one_tower_per_download(qapp):
    widget = scene_mod.SceneWidget()
    widget.update_downloads(rows((1, 10.0, 5000, False), (2, 90.0, 0, True)))
    assert len(widget._buildings) == 2
    assert [b.key for b in widget._buildings] == [1, 2]

    # The same ids again keep the same towers, so nothing jumps.
    first = widget._buildings[0]
    widget.update_downloads(rows((1, 20.0, 5000, False), (2, 90.0, 0, True)))
    assert widget._buildings[0] is first
    assert widget._buildings[0].percent == 20.0


def test_the_town_never_grows_wider_than_its_plate(qapp):
    widget = scene_mod.SceneWidget()
    widget.update_downloads(rows(*[(i, 50.0, 1000, False) for i in range(30)]))
    assert len(widget._buildings) == scene_mod.GRID_W


def test_towers_grow_towards_the_real_percentage_instead_of_jumping(qapp):
    widget = scene_mod.SceneWidget()
    widget.update_downloads(rows((1, 100.0, 5000, False)))
    assert widget._buildings[0].shown == 0.0
    for _ in range(3):
        widget._advance()
    partly = widget._buildings[0].shown
    assert 0 < partly < 1, "the blocks should slide up, not teleport"
    for _ in range(60):
        widget._advance()
    assert widget._buildings[0].shown == pytest.approx(1.0, abs=0.01)


def test_finishing_a_download_throws_blocks_in_the_air(qapp):
    widget = scene_mod.SceneWidget()
    widget.update_downloads(rows((1, 99.0, 5000, False)))
    assert not widget._sparks
    widget.update_downloads(rows((1, 100.0, 0, True)))
    assert widget._sparks, "no celebration"
    assert widget._buildings[0].flash > 0

    for _ in range(80):
        widget._advance()
    assert not widget._sparks, "the sparks have to fall back down eventually"


def test_the_animation_stops_when_nothing_is_downloading(qapp):
    """The whole point of the timer check: no CPU burnt drawing clouds at an
    idle list."""
    widget = scene_mod.SceneWidget()
    widget.show()
    try:
        widget.update_downloads(rows((1, 40.0, 900_000, False)))
        assert widget.busy and widget._timer.isActive()

        widget.update_downloads(rows((1, 100.0, 0, True)))
        for _ in range(120):     # let the flash, sparks and easing finish
            widget._advance()
        assert not widget.busy
        assert not widget._timer.isActive(), "the timer kept running while idle"
    finally:
        widget.close()


def test_a_hidden_town_does_not_animate(qapp):
    widget = scene_mod.SceneWidget()
    widget.update_downloads(rows((1, 40.0, 900_000, False)))
    assert not widget._timer.isActive(), "invisible widgets must not tick"


def test_the_sky_follows_the_clock(qapp):
    widget = scene_mod.SceneWidget()
    assert widget.daylight(12) > widget.daylight(18) > widget.daylight(0)
    assert widget.daylight(0) == pytest.approx(0.0, abs=0.01)
    assert widget.daylight(12) == pytest.approx(1.0, abs=0.01)


def test_the_town_draws_at_every_stage_without_crashing(qapp):
    widget = scene_mod.SceneWidget()
    widget.resize(600, 200)
    for town in (
        [],
        rows((1, 0.0, 0, False)),
        rows((1, 50.0, 1_000_000, False), (2, 100.0, 0, True)),
        rows(*[(i, i * 10.0, i * 1000, i % 2 == 0) for i in range(1, 8)]),
    ):
        widget.update_downloads(town)
        for _ in range(5):
            widget._advance()
        assert not widget.grab().isNull()


def test_one_frame_is_cheap_enough_to_run_at_twenty_a_second(qapp):
    widget = scene_mod.SceneWidget()
    widget.resize(900, 220)
    widget.update_downloads(rows(*[(i, 60.0, 500_000, False) for i in range(9)]))
    for _ in range(10):
        widget._advance()

    start = time.perf_counter()
    for _ in range(10):
        widget.grab()
    per_frame = (time.perf_counter() - start) / 10
    budget = scene_mod.FRAME_MS / 1000
    assert per_frame < budget, f"{per_frame * 1000:.0f} ms a frame, budget {budget * 1000:.0f}"


def test_the_caption_says_what_is_going_on(qapp):
    from app.ui.i18n import tr

    widget = scene_mod.SceneWidget()
    widget.resize(500, 180)

    def caption() -> str:
        # The caption is painted, so read it back off the widget's own text.
        return widget._caption_text()

    assert caption() == tr("Nothing downloading - the town is quiet")
    widget.update_downloads(rows((1, 100.0, 0, True)))
    assert caption() == tr("All downloads finished")
    widget.update_downloads(rows((1, 100.0, 0, True), (2, 10.0, 2048, False)))
    assert tr("Downloading") in caption()


def test_controller_items_become_town_rows():
    from app.core.task import TaskState
    from app.ui.controller import DownloadItem

    items = [
        DownloadItem(db_id=3, url="https://x/a", filename="a.bin",
                     save_path="D:/", state=TaskState.DOWNLOADING,
                     size=1000, downloaded=250, speed=99.0),
        DownloadItem(db_id=4, url="https://x/b", filename="b.bin",
                     save_path="D:/", state=TaskState.COMPLETED,
                     size=1000, downloaded=1000),
    ]
    assert scene_mod.rows_from_items(items) == [
        (3, "a.bin", 25.0, 99.0, False),
        (4, "b.bin", 100.0, 0.0, True),
    ]
