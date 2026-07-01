"""Progress Reporter tests (offline)."""

from __future__ import annotations

import json

from faostatdb.progress import Reporter


def test_json_mode_emits_jsonl_on_stdout(capsys):
    r = Reporter(json_mode=True)
    r.event("QCL", "import", "imported", rows=123, message="done")
    out = capsys.readouterr().out.strip()
    obj = json.loads(out)
    assert obj == {
        "dataset": "QCL",
        "stage": "import",
        "status": "imported",
        "rows": 123,
        "message": "done",
    }


def test_human_mode_writes_to_stderr(capsys):
    r = Reporter(json_mode=False, ascii_mode=True, no_progress=True)
    r.event("QCL", "download", "downloaded")
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout in human mode
    assert "QCL" in captured.err
    assert "[OK]" in captured.err  # ASCII icon


def test_download_phase_no_rich_is_noop(capsys):
    # With no_progress the tracker must be a safe no-op (no exceptions).
    r = Reporter(json_mode=False, no_progress=True)
    with r.download_phase(3) as tracker:
        tracker.start("QCL", 1000)
        tracker.advance("QCL", 500, 1000)
        tracker.finish("QCL")
