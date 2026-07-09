"""Download-directory resolution tests (offline, deterministic)."""

from faostatdb.paths import (
    MANIFEST_DIRNAME,
    PROJECT_LOCAL_DIRNAME,
    clean_cache,
    resolve_database_path,
    resolve_download_dir,
)


def test_explicit_wins(tmp_path):
    target = tmp_path / "explicit"
    out = resolve_download_dir(str(target), cwd=tmp_path)
    assert out == target
    assert out.is_dir()


def test_keep_archives_uses_project_local(tmp_path):
    out = resolve_download_dir(None, keep_archives=True, cwd=tmp_path)
    assert out == tmp_path / PROJECT_LOCAL_DIRNAME
    assert out.is_dir()


def test_download_env_var_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("FAOSTATDB_DOWNLOAD_DIR", str(tmp_path / "from_env"))
    out = resolve_download_dir(None, keep_archives=True, cwd=tmp_path)
    assert out == tmp_path / PROJECT_LOCAL_DIRNAME


def test_explicit_download_dir_is_used_even_if_env_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("FAOSTATDB_DOWNLOAD_DIR", str(tmp_path / "env"))
    explicit = tmp_path / "explicit"
    out = resolve_download_dir(str(explicit), cwd=tmp_path)
    assert out == explicit


def test_environment_reference_in_explicit_value_is_not_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("FAOSTATDB_TMP_DIR", str(tmp_path / "expanded"))
    out = resolve_download_dir("${FAOSTATDB_TMP_DIR}", cwd=tmp_path)
    assert out == tmp_path / "${FAOSTATDB_TMP_DIR}"


def test_database_bare_filename_is_project_local(tmp_path):
    # A bare filename must land next to the project (cwd), not in an OS data dir.
    out = resolve_database_path("faostat.duckdb", cwd=tmp_path)
    assert out == tmp_path / "faostat.duckdb"


def test_database_absolute_used_verbatim(tmp_path):
    target = tmp_path / "elsewhere" / "db.duckdb"
    out = resolve_database_path(str(target), cwd=tmp_path)
    assert out == target
    assert out.parent.is_dir()


def test_database_env_dir_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("FAOSTATDB_DATABASE_DIR", str(tmp_path / "external_volume"))
    out = resolve_database_path("faostat.duckdb", cwd=tmp_path)
    assert out == tmp_path / "faostat.duckdb"


def test_clean_cache_can_remove_empty_download_dir(tmp_path):
    download_dir = tmp_path / PROJECT_LOCAL_DIRNAME
    manifest_dir = download_dir / MANIFEST_DIRNAME
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.jsonl").write_text("{}\n", encoding="utf-8")
    (download_dir / "QCL.zip").write_bytes(b"archive")

    removed, freed = clean_cache(download_dir, remove_dir=True)

    assert removed == 1
    assert freed == len(b"archive")
    assert not download_dir.exists()


def test_clean_cache_keeps_download_dir_with_unrelated_files(tmp_path):
    download_dir = tmp_path / PROJECT_LOCAL_DIRNAME
    download_dir.mkdir()
    (download_dir / "QCL.zip").write_bytes(b"archive")
    (download_dir / "README.txt").write_text("keep me", encoding="utf-8")

    clean_cache(download_dir, remove_dir=True)

    assert download_dir.is_dir()
    assert not (download_dir / "QCL.zip").exists()
    assert (download_dir / "README.txt").is_file()
