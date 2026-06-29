"""Fetch and parse FAOSTAT bulk metadata; dataset selection.

The bulk inventory lives at
``https://bulks-faostat.fao.org/production/datasets_E.json`` and drives selection,
download, and validation. This module fetches it (stdlib ``urllib``), parses the
dataset records, and applies the ``all`` / ``include`` / ``exclude`` selection.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import DatasetsConfig

METADATA_URL = "https://bulks-faostat.fao.org/production/datasets_E.json"


@dataclass(frozen=True)
class DatasetRecord:
    """One FAOSTAT dataset entry from ``datasets_E.json``."""

    dataset_code: str
    dataset_name: str
    date_update: str | None
    file_location: str | None
    file_size: str | None
    file_rows: int | None

    @property
    def code(self) -> str:
        return self.dataset_code


@dataclass(frozen=True)
class MetadataSnapshot:
    """A fetched copy of the bulk metadata plus its content hash."""

    url: str
    sha256: str
    datasets: list[DatasetRecord]


def fetch_metadata_bytes(url: str = METADATA_URL, *, timeout: float = 60.0) -> bytes:
    """Download the raw metadata JSON bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "faostatdb"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        return resp.read()


def parse_metadata(raw: bytes, *, url: str = METADATA_URL) -> MetadataSnapshot:
    """Parse metadata bytes into a :class:`MetadataSnapshot`.

    The structure of ``datasets_E.json`` is ``{"Datasets": {"Dataset": [ ... ]}}``.
    Field names are matched leniently to tolerate minor upstream renames.
    """
    sha = hashlib.sha256(raw).hexdigest()
    doc = json.loads(raw)
    entries = _extract_entries(doc)
    records = [_record_from_entry(e) for e in entries]
    return MetadataSnapshot(url=url, sha256=sha, datasets=records)


def fetch_and_parse(url: str = METADATA_URL) -> MetadataSnapshot:
    return parse_metadata(fetch_metadata_bytes(url), url=url)


def _extract_entries(doc: Any) -> list[dict[str, Any]]:
    datasets = doc.get("Datasets", doc) if isinstance(doc, dict) else doc
    if isinstance(datasets, dict):
        datasets = datasets.get("Dataset", [])
    if isinstance(datasets, dict):  # single-record edge case
        datasets = [datasets]
    return list(datasets or [])


def _record_from_entry(e: dict[str, Any]) -> DatasetRecord:
    def pick(*keys: str) -> Any:
        for k in keys:
            if k in e and e[k] not in (None, ""):
                return e[k]
        return None

    rows = pick("FileRows", "Rows")
    try:
        rows_int = int(rows) if rows is not None else None
    except (TypeError, ValueError):
        rows_int = None

    return DatasetRecord(
        dataset_code=str(pick("DatasetCode", "datasetCode", "code") or "").strip(),
        dataset_name=str(pick("DatasetName", "datasetName", "name") or "").strip(),
        date_update=_opt_str(pick("DateUpdate", "dateUpdate")),
        file_location=_opt_str(pick("FileLocation", "fileLocation", "URL")),
        file_size=_opt_str(pick("FileSize", "fileSize")),
        file_rows=rows_int,
    )


def _opt_str(v: Any) -> str | None:
    return str(v).strip() if v not in (None, "") else None


def select_datasets(
    datasets: list[DatasetRecord], selection: DatasetsConfig
) -> list[DatasetRecord]:
    """Apply ``all`` / ``include`` / ``exclude`` selection to dataset records."""
    mode = (selection.mode or "all").lower()
    if mode == "include":
        wanted = {c.upper() for c in selection.include}
        return [d for d in datasets if d.code.upper() in wanted]
    if mode == "exclude":
        blocked = {c.upper() for c in selection.exclude}
        return [d for d in datasets if d.code.upper() not in blocked]
    return list(datasets)
