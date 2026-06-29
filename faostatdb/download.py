"""Parallel download with a hot-restart manifest and retry/backoff.

State machine (recorded per dataset in ``manifest.jsonl``)::

    pending -> downloading -> downloaded -> zip_valid | zip_invalid
            -> importing -> imported | failed

Archives download to ``*.part`` and are atomically renamed to ``*.zip`` on
completion. Valid archives are reused across relaunches; archives are never
deleted until the build succeeds.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

RETRY_BACKOFF_SECONDS = (2, 5, 15)
MAX_RETRIES = 3


class State(str, Enum):
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
    dataset_code: str
    state: str = State.PENDING.value
    archive_path: str | None = None
    archive_sha256: str | None = None
    url: str | None = None
    error: str | None = None
    updated_at: str | None = None


class Manifest:
    """Append-only JSONL manifest with last-write-wins per ``dataset_code``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, ManifestEntry] = {}
        if path.exists():
            self._load()

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
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

    def needs_download(self, dataset_code: str, archive_path: Path) -> bool:
        """True unless a valid, present archive already exists for this dataset."""
        entry = self.get(dataset_code)
        if entry is None:
            return True
        terminal_ok = {State.ZIP_VALID.value, State.IMPORTING.value, State.IMPORTED.value}
        return not (entry.state in terminal_ok and archive_path.exists())


def download_file(url: str, dest: Path, *, timeout: float = 300.0) -> Path:
    """Download ``url`` to ``dest`` via a ``.part`` temp file + atomic rename.

    Returns the final ``dest`` path. Raises on network/HTTP error so callers can
    apply retry/backoff.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "faostatdb"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, part.open("wb") as out:  # noqa: S310
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    part.replace(dest)
    return dest


def download_with_retry(
    url: str,
    dest: Path,
    *,
    backoff: Iterable[float] = RETRY_BACKOFF_SECONDS,
    sleep=time.sleep,
) -> Path:
    """Download with exponential backoff. Re-raises the last error after retries."""
    delays = list(backoff)
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return download_file(url, dest)
        except Exception as exc:  # noqa: BLE001 — retried/re-raised below
            last_exc = exc
            if attempt < len(delays):
                sleep(delays[attempt])
            else:
                break
    assert last_exc is not None
    raise last_exc
