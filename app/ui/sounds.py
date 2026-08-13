"""Eight-bit sound effects, synthesised rather than shipped.

Three reasons not to ship .wav files: the repository stays diffable, there is
no licence to track, and the volume setting can be baked into the samples -
which matters because the player used here has no volume control of its own.

The waveforms are the ones a 1985 sound chip could make: square and pulse for
melody, triangle for the low notes, white noise for percussion. A note is a
frequency, a length and a shape; a sound is a handful of notes. That is the
whole synthesiser.

Playback goes through `winsound`, which is part of the standard library, plays
asynchronously and needs no Qt multimedia module (that would add tens of
megabytes to the packaged build for four short blips). Its one limitation -
a new sound stops the previous one - is fine for UI feedback and is why the
sounds are kept under half a second.
"""

from __future__ import annotations

import math
import struct
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

from ..util.log import get_logger
from ..util.paths import data_dir

log = get_logger(__name__)

RATE = 22050
#: 16-bit mono: the smallest format every Windows audio stack plays without
#: resampling surprises.
AMPLITUDE = 20000

SQUARE, PULSE, TRIANGLE, NOISE, REST = "square", "pulse", "triangle", "noise", "rest"


@dataclass(frozen=True)
class Note:
    freq: float
    ms: int
    wave: str = SQUARE
    gain: float = 1.0


#: The pentatonic-ish set the era leaned on, so anything built from these
#: sounds deliberate rather than random.
C5, D5, E5, G5, A5, C6, E6, G6 = 523.3, 587.3, 659.3, 784.0, 880.0, 1046.5, 1318.5, 1568.0
G4, C4, E4, A3 = 392.0, 261.6, 329.6, 220.0

#: event -> the notes that make it. Named after what happened, not the sound.
EFFECTS: dict[str, list[Note]] = {
    # A coin drop: the two-note blip everyone recognises.
    "added": [Note(C6, 60, PULSE), Note(G6, 110, PULSE)],
    # Level start: a short rising run.
    "started": [Note(C5, 45, SQUARE), Note(E5, 45, SQUARE), Note(G5, 70, SQUARE)],
    # The 1-up: four notes, the last one held.
    "completed": [
        Note(E5, 55, SQUARE), Note(G5, 55, SQUARE),
        Note(C6, 55, SQUARE), Note(E6, 160, SQUARE),
    ],
    # Damage taken: down a minor third, on the low channel.
    "error": [Note(E4, 90, TRIANGLE), Note(C4, 90, TRIANGLE), Note(A3, 200, TRIANGLE)],
    # Pause: one soft low blip.
    "paused": [Note(G4, 70, TRIANGLE, 0.7)],
    # Stage clear: the longest one, and the only one with percussion.
    "queue_done": [
        Note(C5, 70, SQUARE), Note(E5, 70, SQUARE), Note(G5, 70, SQUARE),
        Note(C6, 70, SQUARE), Note(0, 40, REST), Note(0, 60, NOISE, 0.5),
        Note(C6, 90, PULSE), Note(G6, 220, PULSE),
    ],
}


def _sample(shape: str, phase: float, index: int) -> float:
    """One sample of `shape` at `phase` (0..1 through the period)."""
    if shape == SQUARE:
        return 1.0 if phase < 0.5 else -1.0
    if shape == PULSE:
        return 1.0 if phase < 0.25 else -1.0
    if shape == TRIANGLE:
        return 4.0 * abs(phase - 0.5) - 1.0
    if shape == NOISE:
        # A linear congruential generator, so the "noise" is identical every
        # time: two identical sounds should be identical files.
        return ((index * 1103515245 + 12345) % 65536) / 32768.0 - 1.0
    return 0.0


def render(notes: list[Note], volume: float = 1.0) -> bytes:
    """Turn notes into 16-bit PCM frames."""
    volume = max(0.0, min(1.0, volume))
    frames = bytearray()
    for note in notes:
        count = max(1, int(RATE * note.ms / 1000))
        if note.wave == REST or note.freq <= 0 and note.wave != NOISE:
            frames.extend(b"\x00\x00" * count)
            continue
        period = RATE / note.freq if note.freq > 0 else 1.0
        # A short attack and a decay to silence: without them each note ends
        # on a step and the speaker clicks.
        attack = min(count // 8, int(RATE * 0.002))
        for i in range(count):
            phase = (i % period) / period if note.freq > 0 else 0.0
            value = _sample(note.wave, phase, i)
            envelope = 1.0
            if i < attack:
                envelope = i / max(1, attack)
            else:
                envelope = 1.0 - (i - attack) / max(1, count - attack)
            level = value * envelope * note.gain * volume * AMPLITUDE
            frames.extend(struct.pack("<h", int(max(-32768, min(32767, level)))))
    return bytes(frames)


def write_wav(path: Path, frames: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written beside the target and moved into place, so a half-written file
    # can never be handed to the audio system.
    temporary = path.with_suffix(".tmp")
    with wave.open(str(temporary), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(frames)
    temporary.replace(path)
    return path


def sounds_dir() -> Path:
    return data_dir() / "sounds"


def effect_path(event: str, volume: int) -> Path:
    """Where the rendered file for this event and volume lives."""
    return sounds_dir() / f"{event}-{max(0, min(100, int(volume)))}.wav"


def ensure(event: str, volume: int = 70) -> Path | None:
    """Render `event` at `volume` unless it is already on disk."""
    notes = EFFECTS.get(event)
    if not notes:
        return None
    path = effect_path(event, volume)
    if path.exists():
        return path
    try:
        return write_wav(path, render(notes, volume / 100))
    except OSError as exc:  # pragma: no cover - disk level failure
        log.warning("could not write the sound %s: %s", event, exc)
        return None


def _winsound_player(path: Path) -> None:
    """Hand the file to Windows and return immediately."""
    import winsound

    winsound.PlaySound(
        str(path),
        winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
    )


class SoundBoard:
    """Plays the effects, if the user wants them.

    Holds the settings rather than a copy of their values: the toggle takes
    effect on the next sound, with no signal to wire up.
    """

    def __init__(self, settings, player=None) -> None:
        self.settings = settings
        self._player = player or (_winsound_player if sys.platform == "win32" else None)
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("sound_effects"))

    @property
    def volume(self) -> int:
        try:
            return max(0, min(100, int(self.settings.get("sound_volume"))))
        except (TypeError, ValueError):
            return 70

    def play(self, event: str) -> bool:
        """True when a sound was actually handed to the audio system."""
        if not self.enabled or self._player is None or self.volume == 0:
            return False
        path = ensure(event, self.volume)
        if path is None:
            return False
        try:
            self._player(path)
        except Exception as exc:  # noqa: BLE001 - a mute machine is not an error
            if not self._warned:
                log.info("sound playback is unavailable: %s", exc)
                self._warned = True
            return False
        return True

    def preview(self, event: str = "completed") -> bool:
        """Play regardless of the toggle - the button in the settings dialog."""
        if self._player is None:
            return False
        path = ensure(event, self.volume or 70)
        if path is None:
            return False
        try:
            self._player(path)
        except Exception:  # noqa: BLE001 - same reasoning as `play`
            return False
        return True

    def clear_cache(self) -> int:
        """Drop the rendered files; they cost a millisecond to make again."""
        removed = 0
        for path in sounds_dir().glob("*.wav"):
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover - filesystem dependent
                pass
        return removed
