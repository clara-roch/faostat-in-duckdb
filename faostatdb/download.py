"""
Parallel download with a hot-restart manifest and retry/backoff.

State machine (recorded per dataset in ``manifest.jsonl``)::

    pending -> downloading -> downloaded -> zip_valid | zip_invalid
            -> importing -> imported | failed

Archives download to ``*.part`` and are atomically renamed to ``*.zip`` on
completion. Valid archives are reused across relaunches ("hot restart"); archives
are never deleted until the build succeeds. The manifest is append-only JSONL
with last-write-wins per dataset code, so it survives crashes and is trivial to
inspect by hand.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import asdict, dataclass, fields, replace
from enum import Enum
from pathlib import Path
from typing import Iterable

RETRY_BACKOFF_SECONDS = (2, 5, 15)
MAX_RETRIES = 3


class State(str, Enum):
    """The lifecycle states a dataset moves through during a build."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    ZIP_VALID = "zip_valid"
    ZIP_INVALID = "zip_invalid"
    IMPORTING = "importing"
    IMPORTED = "imported"
    FAILED = "failed"


@dataclass
class ManifestEntry:
    """One dataset's current state in the download manifest.

    Mirrors the record shape suggested in FAOSTATdb.md (dataset_code, url,
    expected size/rows, archive_path, sha256, status, attempts, last_error,
    downloaded_at). ``updated_at`` timestamps the last transition.
    """

    dataset_code: str
    state: str = State.PENDING.value
    archive_path: str | None = None
    archive_sha256: str | None = None
    url: str | None = None
    expected_size: str | None = None
    expected_rows: int | None = None
    attempts: int = 0
    error: str | None = None
    downloaded_at: str | None = None
    updated_at: str | None = None


# Field names of ManifestEntry, used to filter unknown keys when loading an older
# manifest written by a previous version (forward/backward compatibility).
_ENTRY_FIELDS = {f.name for f in fields(ManifestEntry)}


class Manifest:
    """Append-only JSONL manifest with last-write-wins per ``dataset_code``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, ManifestEntry] = {}
        if path.exists():
            self._load()

    def _load(self) -> None:
        """Replay the JSONL log; later lines override earlier ones per code."""
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            # Drop keys this version doesn't know about so a manifest written by a
            # newer/older build still loads instead of raising TypeError.
            data = {k: v for k, v in data.items() if k in _ENTRY_FIELDS}
            entry = ManifestEntry(**data)
            self._entries[entry.dataset_code] = entry

    def get(self, dataset_code: str) -> ManifestEntry | None:
        return self._entries.get(dataset_code)

    def all(self) -> list[ManifestEntry]:
        return list(self._entries.values())

    def update(self, entry: ManifestEntry, *, now: str | None = None) -> None:
        """Record a new state for an entry and append it to the manifest file."""
        entry.updated_at = now
        self._entries[entry.dataset_code] = entry
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry)) + "\n")

    def update_state(
        self, dataset_code: str, *, now: str | None = None, **changes
    ) -> ManifestEntry:
        """Update one entry while preserving unchanged provenance fields.

        The manifest is append-only and last-write-wins per dataset, so writing a
        partial fresh ``ManifestEntry`` would accidentally erase earlier metadata
        such as ``downloaded_at``, ``attempts`` or declared size/rows. State
        transitions should call this helper with only the fields that actually
        changed; omitted fields are carried forward from the current entry.
        """
        unknown = set(changes) - _ENTRY_FIELDS
        if unknown:
            raise TypeError(f"unknown manifest field(s): {', '.join(sorted(unknown))}")
        current = self.get(dataset_code) or ManifestEntry(dataset_code=dataset_code)
        entry = replace(current, **changes)
        self.update(entry, now=now)
        return entry

    def attempts(self, dataset_code: str) -> int:
        """How many download attempts have been recorded for this dataset."""
        entry = self.get(dataset_code)
        return entry.attempts if entry else 0

    def needs_download(self, dataset_code: str, archive_path: Path) -> bool:
        """True unless a present, presumed-good archive already exists.

        A re-download is needed when there is no prior record, when the archive
        file is gone from disk, or when the last recorded state is a known-bad
        one (``failed`` / ``zip_invalid``) that warrants a fresh fetch. Once an
        archive has been ``downloaded`` it is reused even if the build later
        crashed before importing it: Phase 2 re-validates every archive with
        ``testzip()`` anyway, so a corrupt reuse is caught and re-handled there
        rather than forcing a blind re-download of everything.

        This is the heart of "keep the .zip in the cache so we don't re-download
        every run": as long as a valid archive is on disk and recorded, it is
        reused verbatim.
        """
        entry = self.get(dataset_code)
        if entry is None:
            return True
        if not archive_path.exists():
            return True
        reusable = {
            State.DOWNLOADED.value,
            State.ZIP_VALID.value,
            State.IMPORTING.value,
            State.IMPORTED.value,
        }
        return entry.state not in reusable


def download_file(
    url: str, dest: Path, *, timeout: float = 300.0, on_progress=None
) -> Path:
    """Download ``url`` to ``dest`` via a ``.part`` temp file + atomic rename.

    Returns the final ``dest`` path. Raises on network/HTTP error so callers can
    apply retry/backoff. The atomic rename guarantees a ``*.zip`` on disk is
    always a *complete* download — a killed process leaves only a ``*.part``.

    ``on_progress(bytes_so_far, total_or_None)`` is called after each chunk so a
    progress UI can render a bar; ``total`` comes from the ``Content-Length``
    header when the server sends it (``None`` otherwise).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "faostatdb"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, part.open("wb") as out:  # noqa: S310
        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        done = 0
        if on_progress is not None:
            on_progress(0, total)
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if on_progress is not None:
                on_progress(done, total)
    part.replace(dest)
    return dest


def download_with_retry(
    url: str,
    dest: Path,
    *,
    backoff: Iterable[float] = RETRY_BACKOFF_SECONDS,
    sleep=time.sleep,
    on_progress=None,
) -> Path:
    """Download with exponential backoff. Re-raises the last error after retries.

    Retries at ``backoff`` delays (default 2s, 5s, 15s) up to ``MAX_RETRIES``
    times, then gives up and re-raises so the caller can mark the dataset failed.
    """
    delays = list(backoff)
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return download_file(url, dest, on_progress=on_progress)
        except Exception as exc:  # noqa: BLE001 — retried/re-raised below
            last_exc = exc
            if attempt < len(delays):
                sleep(delays[attempt])
            else:
                break
    assert last_exc is not None
    raise last_exc
