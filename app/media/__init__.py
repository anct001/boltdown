"""Streaming media support: HLS playlists, yt-dlp extraction, ffmpeg muxing."""

from __future__ import annotations

from .detect import MediaKind, classify

__all__ = ["MediaKind", "classify"]
