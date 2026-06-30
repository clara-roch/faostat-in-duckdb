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
