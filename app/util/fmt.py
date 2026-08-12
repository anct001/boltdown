"""Human readable formatting helpers."""

from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in _UNITS:
        if abs(n) < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"


def human_speed(bps: float | None) -> str:
    if bps is None:
        return "?"
    return f"{human_size(bps)}/s"


def human_duration(seconds: float | None) -> str:
    """Format a duration as d:hh:mm:ss, dropping empty leading units."""
    if seconds is None or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_size(text: str) -> int:
    """Parse '500', '2M', '1.5MB', '300k' into a byte count."""
    s = text.strip().lower().replace("b", "")
    if not s:
        raise ValueError("empty size")
    mult = 1
    if s[-1] in "kmgt":
        mult = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[s[-1]]
        s = s[:-1]
    value = float(s)
    if value < 0:
        raise ValueError("size must be positive")
    return int(value * mult)
