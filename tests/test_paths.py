"""Download-directory resolution tests (offline, deterministic)."""

import os

from faostatdb.paths import (
    ENV_DATABASE_DIR,
    ENV_DOWNLOAD_DIR,
    PROJECT_LOCAL_DIRNAME,
    resolve_database_path,
    resolve_download_dir,
)


def test_explicit_wins(tmp_path):
    target = tmp_path / "explicit"
    out = resolve_download_dir(str(target), cwd=tmp_path)
    assert out == target
    assert out.is_dir()


def test_keep_archives_uses_project_local(tmp_path, monkeypatch):
    # Must be hermetic: a developer machine may set FAOSTATDB_DOWNLOAD_DIR in the
    # real environment, which would otherwise win.
    monkeypatch.delenv(ENV_DOWNLOAD_DIR, raising=False)
    out = resolve_download_dir(None, keep_archives=True, cwd=tmp_path)
    assert out == tmp_path / PROJECT_LOCAL_DIRNAME
    assert out.is_dir()


def test_env_var_used_when_no_explicit(tmp_path, monkeypatch):
    target = tmp_path / "from_env"
    monkeypatch.setenv(ENV_DOWNLOAD_DIR, str(target))
    out = resolve_download_dir(None, cwd=tmp_path)
    assert out == target


def test_explicit_takes_precedence_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_DOWNLOAD_DIR, str(tmp_path / "env"))
    explicit = tmp_path / "explicit"
    out = resolve_download_dir(str(explicit), cwd=tmp_path)
    assert out == explicit


def test_var_reference_in_value_is_expanded(tmp_path, monkeypatch):
    target = tmp_path / "expanded"
    monkeypatch.setenv(ENV_DOWNLOAD_DIR, str(target))
    out = resolve_download_dir("${%s}" % ENV_DOWNLOAD_DIR, cwd=tmp_path)
    assert out == target


def test_unset_var_reference_falls_through(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_DOWNLOAD_DIR, raising=False)
    # An unexpanded "${VAR}" must not become a literal directory; with
    # keep_archives it should fall through to the project-local dir.
    out = resolve_download_dir(
        "${UNSET_FAOSTATDB_VAR_XYZ}", keep_archives=True, cwd=tmp_path
    )
    assert out == tmp_path / PROJECT_LOCAL_DIRNAME


def test_database_bare_filename_is_project_local(tmp_path, monkeypatch):
    # A bare filename must land next to the project (cwd), not in an OS data dir.
    monkeypatch.delenv(ENV_DATABASE_DIR, raising=False)
    out = resolve_database_path("faostat.duckdb", cwd=tmp_path)
    assert out == tmp_path / "faostat.duckdb"


def test_database_absolute_used_verbatim(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_DATABASE_DIR, raising=False)
    target = tmp_path / "elsewhere" / "db.duckdb"
    out = resolve_database_path(str(target), cwd=tmp_path)
    assert out == target
    assert out.parent.is_dir()


def test_database_env_dir_overrides_parent(tmp_path, monkeypatch):
    parent = tmp_path / "external_volume"
    monkeypatch.setenv(ENV_DATABASE_DIR, str(parent))
    out = resolve_database_path("faostat.duckdb", cwd=tmp_path)
    assert out == parent / "faostat.duckdb"
    assert parent.is_dir()


def test_database_unset_env_dir_reference_falls_through_to_cwd(tmp_path, monkeypatch):
    # An unexpanded "${VAR}" for the dir must not become a literal parent dir;
    # resolution falls through to project-local.
    monkeypatch.setenv(ENV_DATABASE_DIR, "${UNSET_FAOSTATDB_VAR_XYZ}")
    out = resolve_database_path("faostat.duckdb", cwd=tmp_path)
    assert out == tmp_path / "faostat.duckdb"
