"""Download-directory resolution tests (offline, deterministic)."""

import os

from faostatdb.paths import (
    ENV_DOWNLOAD_DIR,
    PROJECT_LOCAL_DIRNAME,
    resolve_download_dir,
)


def test_explicit_wins(tmp_path):
    target = tmp_path / "explicit"
    out = resolve_download_dir(str(target), cwd=tmp_path)
    assert out == target
    assert out.is_dir()


def test_keep_archives_uses_project_local(tmp_path, monkeypatch):
    # Must be hermetic: a developer machine (or this repo's secrets.env) may set
    # FAOSTATDB_DOWNLOAD_DIR in the real environment, which would otherwise win.
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
