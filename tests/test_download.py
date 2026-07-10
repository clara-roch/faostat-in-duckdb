"""Hot-restart logic for the download manifest."""

from __future__ import annotations

from faostatdb.download import Manifest, ManifestEntry, State


def _manifest(tmp_path):
    return Manifest(tmp_path / "manifest.jsonl")


def test_needs_download_when_no_record(tmp_path):
    m = _manifest(tmp_path)
    assert m.needs_download("QCL", tmp_path / "QCL.zip") is True


def test_reuses_downloaded_archive_present_on_disk(tmp_path):
    # Regression: a build that downloaded everything then crashed before the
    # import phase leaves archives in state 'downloaded'. They must be reused,
    # not re-downloaded.
    archive = tmp_path / "QCL.zip"
    archive.write_bytes(b"PK\x03\x04")
    m = _manifest(tmp_path)
    m.update(ManifestEntry(dataset_code="QCL", state=State.DOWNLOADED.value))
    assert m.needs_download("QCL", archive) is False


def test_redownloads_when_archive_missing(tmp_path):
    m = _manifest(tmp_path)
    m.update(ManifestEntry(dataset_code="QCL", state=State.DOWNLOADED.value))
    assert m.needs_download("QCL", tmp_path / "gone.zip") is True


def test_redownloads_known_bad_states(tmp_path):
    archive = tmp_path / "QCL.zip"
    archive.write_bytes(b"corrupt")
    for bad in (State.FAILED, State.ZIP_INVALID):
        m = _manifest(tmp_path)
        m.update(ManifestEntry(dataset_code="QCL", state=bad.value))
        assert m.needs_download("QCL", archive) is True, bad


def test_reuses_imported_and_validated(tmp_path):
    archive = tmp_path / "QCL.zip"
    archive.write_bytes(b"PK\x03\x04")
    for good in (State.ZIP_VALID, State.IMPORTING, State.IMPORTED):
        m = _manifest(tmp_path)
        m.update(ManifestEntry(dataset_code="QCL", state=good.value))
        assert m.needs_download("QCL", archive) is False, good


def test_update_state_preserves_provenance_across_transitions(tmp_path):
    path = tmp_path / "manifest.jsonl"
    m = Manifest(path)
    m.update_state(
        "QCL",
        state=State.DOWNLOADED.value,
        archive_path=str(tmp_path / "QCL.zip"),
        url="https://example.test/QCL.zip",
        expected_size="1MB",
        expected_rows=123,
        attempts=2,
        downloaded_at="2026-01-01T00:00:00+00:00",
        now="2026-01-01T00:00:01+00:00",
    )
    m.update_state(
        "QCL",
        state=State.IMPORTING.value,
        archive_sha256="abc",
        now="2026-01-01T00:00:02+00:00",
    )
    m.update_state(
        "QCL",
        state=State.IMPORTED.value,
        now="2026-01-01T00:00:03+00:00",
    )

    reloaded = Manifest(path).get("QCL")
    assert reloaded is not None
    assert reloaded.state == State.IMPORTED.value
    assert reloaded.archive_sha256 == "abc"
    assert reloaded.downloaded_at == "2026-01-01T00:00:00+00:00"
    assert reloaded.expected_size == "1MB"
    assert reloaded.expected_rows == 123
    assert reloaded.attempts == 2
