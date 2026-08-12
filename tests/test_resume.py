from __future__ import annotations

from app.core.probe import ProbeResult
from app.core.resume import ResumeMeta, cleanup, meta_path_for
from app.core.segment import Segment, plan_segments


def _probe(size=1000, etag='"v1"', last_modified="Mon, 01 Jan 2026 00:00:00 GMT"):
    return ProbeResult(
        url="http://h/f", final_url="http://h/f", size=size, resumable=True,
        etag=etag, last_modified=last_modified, content_type=None,
        filename="f", status=206,
    )


def _meta(**kw):
    defaults = dict(
        url="http://h/f", final_url="http://h/f", filename="f", size=1000,
        resumable=True, segments=plan_segments(1000, 2), etag='"v1"',
        last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
    )
    defaults.update(kw)
    return ResumeMeta(**defaults)


def test_plan_segments_covers_the_whole_file():
    segments = plan_segments(10 * 1024 * 1024, 8)
    assert len(segments) == 8
    assert segments[0].start == 0
    assert segments[-1].end == 10 * 1024 * 1024 - 1
    assert sum(s.total for s in segments) == 10 * 1024 * 1024


def test_plan_segments_avoids_tiny_slices():
    assert len(plan_segments(500_000, 8)) == 1
    assert len(plan_segments(3 * 1024 * 1024, 8)) == 3


def test_plan_segments_unknown_size():
    segments = plan_segments(None, 8)
    assert len(segments) == 1
    assert segments[0].end is None
    assert segments[0].remaining is None


def test_segment_geometry():
    seg = Segment(index=0, start=100, end=199, done=40)
    assert seg.total == 100
    assert seg.current == 140
    assert seg.remaining == 60
    assert not seg.is_complete
    seg.done = 100
    assert seg.is_complete


def test_meta_roundtrip(tmp_path):
    path = tmp_path / "f.part.idmdown"
    meta = _meta()
    meta.segments[0].done = 123
    meta.save(path)
    loaded = ResumeMeta.load(path)
    assert loaded is not None
    assert loaded.downloaded == 123
    assert [s.to_dict() for s in loaded.segments] == [s.to_dict() for s in meta.segments]
    assert not (tmp_path / "f.part.idmdown.tmp").exists()


def test_meta_load_ignores_corrupt_file(tmp_path):
    path = tmp_path / "bad.idmdown"
    path.write_text("{not json")
    assert ResumeMeta.load(path) is None


def test_meta_load_missing_file(tmp_path):
    assert ResumeMeta.load(tmp_path / "nope.idmdown") is None


def test_mismatch_detects_size_change():
    assert "size changed" in _meta().mismatch_reason(_probe(size=2000))


def test_mismatch_detects_etag_change():
    assert _meta().mismatch_reason(_probe(etag='"v2"')) == "ETag changed"


def test_mismatch_falls_back_to_last_modified():
    meta = _meta(etag=None)
    reason = meta.mismatch_reason(_probe(etag=None, last_modified="Tue, 02 Jan 2026 00:00:00 GMT"))
    assert reason == "Last-Modified changed"


def test_mismatch_accepts_identical_resource():
    assert _meta().mismatch_reason(_probe()) is None


def test_mismatch_rejects_incoherent_segments():
    meta = _meta(segments=[Segment(0, 0, 499)])  # only covers half the file
    assert "do not cover" in meta.mismatch_reason(_probe())


def test_cleanup_removes_part_and_meta(tmp_path):
    part = tmp_path / "x.part"
    part.write_bytes(b"data")
    meta_path_for(part).write_text("{}")
    cleanup(part)
    assert not part.exists()
    assert not meta_path_for(part).exists()
