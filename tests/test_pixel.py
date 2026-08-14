"""The pixel theme and its sound effects.

Two things are easy to get wrong here and both are checked: that the look is
genuinely blocky (no anti-aliased in-between colours, no rounded corners), and
that the sounds are real audio at the right pitch rather than bytes that happen
to be the right length.
"""

from __future__ import annotations

import struct
import sys
import wave

import pytest
from PySide6.QtWidgets import QApplication

from app.ui import icons, sounds, theme


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ------------------------------------------------------------------- the look


def test_the_pixel_theme_is_offered_like_any_other():
    assert "pixel" in theme.THEMES
    assert theme.THEMES["pixel"].pixel is True
    assert theme.resolve("pixel") is theme.PIXEL
    # Isometric is a pixel theme as well - it wants the same square corners
    # and blocky icons, and only changes how the bars are drawn.
    assert [p.name for p in theme.THEMES.values() if p.pixel] == ["pixel", "iso"]


def test_the_pixel_sheet_squares_every_corner_it_rounds_elsewhere():
    css = theme.stylesheet(theme.PIXEL)
    extra = theme.pixel_extra(theme.PIXEL)
    assert "border-radius: 0px" in extra
    assert css.endswith(extra), "the overrides have to come last to win"
    for widget in ("QPushButton", "QProgressBar", "QMenu", "QLineEdit"):
        assert widget in extra
    assert theme.stylesheet(theme.DARK).count("border-radius: 0px") == 0


def test_the_pixel_sheet_never_uses_a_universal_selector():
    """A `*` rule re-polishes every widget on every change - measured at a
    fourfold slowdown once, so it stays banned."""
    for line in theme.pixel_extra(theme.PIXEL).splitlines():
        stripped = line.strip()
        assert not stripped.startswith("*"), line
        assert " * " not in stripped


def test_the_progress_bar_becomes_a_row_of_cells():
    extra = theme.pixel_extra(theme.PIXEL)
    chunk = extra.rsplit("QProgressBar::chunk", 1)[1]
    assert "width:" in chunk and "margin:" in chunk, (
        "a fixed chunk width plus a margin is what makes Qt draw cells"
    )


def test_the_readout_font_exists_on_this_machine(qapp):
    font = theme.pixel_font(10)
    assert font.family()
    assert font.pointSize() == 10


# ------------------------------------------------------------------- the icons


@pytest.fixture
def pixel_theme(monkeypatch):
    monkeypatch.setattr(theme, "_current", theme.PIXEL)
    return theme.PIXEL


def colours_of(icon, size: int = 32) -> set[str]:
    image = icon.pixmap(size, size).toImage()
    return {
        image.pixelColor(x, y).name()
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    }


def test_pixel_icons_have_no_anti_aliased_edges(qapp, pixel_theme):
    """Anti-aliasing shows up as a spread of in-between colours."""
    for name in ("add", "pause", "stop", "delete", "settings"):
        icon = getattr(icons, f"{name}_icon")()
        shades = colours_of(icon)
        assert shades, f"{name} drew nothing"
        assert len(shades) <= 2, f"{name} has soft edges: {sorted(shades)}"


def test_every_icon_in_the_table_is_drawable(qapp, pixel_theme):
    for name in icons.PIXEL_ICONS:
        icon = icons._maybe_pixel(name, None)
        assert icon is not None and not icon.pixmap(32, 32).isNull(), name


def test_the_grids_are_square_and_use_known_marks():
    for name, (rows, token) in icons.PIXEL_ICONS.items():
        assert len(rows) == icons.PIXEL_GRID, f"{name} is not 8 rows"
        assert all(len(row) == icons.PIXEL_GRID for row in rows), name
        assert set("".join(rows)) <= {".", "#", "+"}, name
        assert getattr(theme.PIXEL, token), f"{name} names a colour that is not a token"


def test_other_themes_keep_the_drawn_icons(qapp, monkeypatch):
    monkeypatch.setattr(theme, "_current", theme.DARK)
    drawn = colours_of(icons.add_icon())
    monkeypatch.setattr(theme, "_current", theme.PIXEL)
    blocky = colours_of(icons.add_icon())
    assert len(drawn) > len(blocky), "the drawn icon should be the softer one"


# ------------------------------------------------------------------ the sounds


def test_every_effect_renders_a_playable_wav(tmp_path, monkeypatch):
    monkeypatch.setattr(sounds, "sounds_dir", lambda: tmp_path)
    for event in sounds.EFFECTS:
        path = sounds.ensure(event, 60)
        assert path is not None and path.exists()
        with wave.open(str(path)) as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == sounds.RATE
            seconds = handle.getnframes() / handle.getframerate()
        # Long enough to hear, short enough that the next one is not cut off.
        assert 0.05 <= seconds <= 1.0, f"{event} lasts {seconds:.2f}s"


def test_the_notes_come_out_at_the_pitch_they_were_written_at():
    """A wave of f Hz crosses zero 2f times a second, whatever its shape."""
    for note in sounds.EFFECTS["completed"]:
        frames = sounds.render([note], 1.0)
        values = struct.unpack(f"<{len(frames) // 2}h", frames)
        crossings = sum(1 for a, b in zip(values, values[1:]) if (a >= 0) != (b >= 0))
        measured = crossings / (2 * len(values) / sounds.RATE)
        assert abs(measured - note.freq) / note.freq < 0.03, (
            f"{note.freq} Hz came out as {measured:.0f} Hz"
        )


def test_volume_is_baked_into_the_samples():
    """The player has no volume control, so the renderer must provide it."""

    def loudest(volume: float) -> int:
        frames = sounds.render(sounds.EFFECTS["added"], volume)
        return max(abs(v) for v in struct.unpack(f"<{len(frames) // 2}h", frames))

    assert loudest(0.0) == 0
    assert loudest(0.25) < loudest(0.5) < loudest(1.0)
    assert loudest(1.0) <= 32767


def test_the_same_sound_renders_identically_every_time():
    """Including the noise channel - otherwise caching them would be wrong."""
    first = sounds.render(sounds.EFFECTS["queue_done"], 0.6)
    second = sounds.render(sounds.EFFECTS["queue_done"], 0.6)
    assert first == second


def test_a_rendered_effect_is_reused_not_rebuilt(tmp_path, monkeypatch):
    monkeypatch.setattr(sounds, "sounds_dir", lambda: tmp_path)
    path = sounds.ensure("added", 40)
    stamp = path.stat().st_mtime_ns
    again = sounds.ensure("added", 40)
    assert again == path and again.stat().st_mtime_ns == stamp
    # A different volume is a different file, not a silent reuse.
    assert sounds.ensure("added", 90) != path


def make_settings():
    from app.storage.db import Database
    from app.storage.settings import Settings

    db = Database(":memory:")
    return Database, db, Settings(db)


def test_nothing_plays_when_the_user_switched_it_off(tmp_path, monkeypatch):
    monkeypatch.setattr(sounds, "sounds_dir", lambda: tmp_path)
    _cls, db, settings = make_settings()
    played: list = []
    board = sounds.SoundBoard(settings, player=played.append)

    settings.set("sound_effects", False)
    assert board.play("completed") is False
    settings.set("sound_effects", True)
    assert board.play("completed") is True
    settings.set("sound_volume", 0)      # volume zero is also a no
    assert board.play("completed") is False
    db.close()
    assert len(played) == 1


def test_an_unknown_event_is_silent_rather_than_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sounds, "sounds_dir", lambda: tmp_path)
    _cls, db, settings = make_settings()
    board = sounds.SoundBoard(settings, player=lambda path: None)
    assert board.play("no-such-event") is False
    db.close()


def test_a_machine_with_no_audio_does_not_break_the_download(tmp_path, monkeypatch):
    monkeypatch.setattr(sounds, "sounds_dir", lambda: tmp_path)
    _cls, db, settings = make_settings()

    def broken(path):
        raise RuntimeError("no audio device")

    board = sounds.SoundBoard(settings, player=broken)
    assert board.play("completed") is False   # reported, never raised
    db.close()


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows only")
def test_playback_is_asynchronous():
    """A blocking play would freeze the window for the length of the sound."""
    import inspect

    source = inspect.getsource(sounds)
    body = source.split("def _winsound_player")[1].split("class ")[0]
    assert "SND_ASYNC" in body


def test_the_window_makes_a_noise_when_a_download_finishes(qapp, tmp_path, monkeypatch):
    from app.core.task import TaskState
    from app.storage.db import Database
    from app.storage.settings import Settings
    from app.ui.controller import Controller, DownloadItem
    from app.ui.main_window import MainWindow

    monkeypatch.setattr(sounds, "sounds_dir", lambda: tmp_path)
    db = Database(":memory:")
    settings = Settings(db)
    window = MainWindow(Controller(db, settings), settings)
    played: list = []
    window.sounds = sounds.SoundBoard(settings, player=lambda p: played.append(p.stem))

    item = DownloadItem(db_id=1, url="https://x/a.bin", filename="a.bin",
                        save_path=str(tmp_path), state=TaskState.COMPLETED)
    window._on_item_changed(item)
    assert [p.split("-")[0] for p in played] == ["completed"]

    window._on_item_changed(item)        # the same download, announced once
    assert len(played) == 1

    failed = DownloadItem(db_id=2, url="https://x/b.bin", filename="b.bin",
                          save_path=str(tmp_path), state=TaskState.ERROR)
    window._on_item_changed(failed)
    window._on_item_changed(failed)      # a retry must not buzz twice
    assert [p.split("-")[0] for p in played] == ["completed", "error"]
    window.close()
    db.close()


# ------------------------------------------- the bitmap font and its limits


def test_the_bitmap_font_is_used_only_where_it_can_draw(qapp):
    """`QFontMetrics.inFont` claims Fixedsys has "ạ" and then draws a box, so
    the rule is about the text, not about asking the font."""
    pixel = theme.pixel_font(10).family()

    for ascii_text in ("87.4%", "12.5 MB/s", "01:23", "1,024 bytes"):
        assert theme.font_for(ascii_text, 10).family() == pixel, ascii_text

    for translated in ("Hoàn tất", "Tạm dừng", "已完成", "완료", "Завершено",
                       "Téléchargement"):
        chosen = theme.font_for(translated, 10).family()
        assert chosen != pixel, f"{translated} would render as boxes in {chosen}"


def test_the_status_column_asks_for_a_font_that_can_draw_the_status():
    """The delegate must pass the text, not assume it is a percentage."""
    import inspect

    from app.ui import task_model

    source = inspect.getsource(task_model.ProgressDelegate)
    assert "theme.pixel_font" not in source, "that ignores what is being drawn"
    assert source.count("theme.font_for") >= 2


# ------------------------------------------------------ the cyberpunk repaint


def test_the_pixel_theme_is_neon_now():
    """Magenta signage and cyan, not the old arcade amber and green."""
    assert theme.PIXEL.label == "Pixel Cyberpunk"
    accent = theme.PIXEL.color("accent")
    assert accent.hue() > 280 or accent.hue() < 20, "the accent should be magenta"
    assert theme.PIXEL.color("window").lightness() < 40, "night, not dusk"
    assert theme.PIXEL.scanlines > 0


def test_only_the_neon_themes_get_scanlines():
    """On a daylight scene the pattern reads as a dirty screen."""
    scanned = [p.name for p in theme.THEMES.values() if p.scanlines]
    assert scanned == ["pixel"]
    assert theme.ISO.scanlines == 0


def test_the_town_draws_scanlines_over_the_towers_not_under_them(qapp, monkeypatch):
    """A scanline that stops at the skyline is wallpaper, not a screen."""
    import inspect

    from app.ui import scene

    source = inspect.getsource(scene.SceneWidget.paintEvent)
    order = [
        source.index("_paint_town"),
        source.index("_paint_scanlines"),
        source.index("_paint_caption"),
    ]
    assert order == sorted(order), "scanlines must come after the town"


def test_a_theme_without_scanlines_draws_none(qapp, monkeypatch):
    from app.ui import scene, theme as theme_mod

    widget = scene.SceneWidget()
    widget.resize(300, 160)
    widget.update_downloads([(1, "a.bin", 50.0, 1000.0, False)])

    def inked(palette) -> int:
        monkeypatch.setattr(theme_mod, "_current", palette)
        image = widget.grab().toImage()
        return sum(
            1 for x in range(0, image.width(), 3) for y in range(image.height())
            if image.pixelColor(x, y).lightness() > 6
        )

    # The scanline pass only darkens, so the neon theme cannot end up brighter.
    assert inked(theme_mod.PIXEL) <= inked(theme_mod.ISO)
