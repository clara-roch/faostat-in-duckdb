"""Fetch and parse FAOSTAT bulk metadata; dataset selection.

The bulk inventory lives at
``https://bulks-faostat.fao.org/production/datasets_E.json`` and drives selection,
download, and validation. This module fetches it (stdlib ``urllib``), parses the
dataset records, and applies the ``all`` / ``include`` / ``exclude`` selection.

The JSON is an object shaped like::

    {"Datasets": {"Dataset": [ {DatasetCode: "AE", DatasetName: "...", ...}, ... ]}}

Each entry carries: ``DatasetCode``, ``DatasetName``, ``Topic``,
``DatasetDescription``, ``Contact``, ``Email``, ``DateUpdate``,
``CompressionFormat``, ``FileType``, ``FileSize``, ``FileRows``, ``FileLocation``.
We keep *all* of these (FAOSTATdb.md asks for the full metadata, not just a few
parsed fields) and additionally stash the raw per-dataset JSON so nothing is lost.
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
    """One FAOSTAT dataset entry from ``datasets_E.json``.

    Every field the bulk inventory publishes is preserved. ``raw_json`` holds the
    exact source entry (serialized) so downstream code can persist the untouched
    metadata for reproducibility even if we never parse a particular key.
    """

    dataset_code: str
    dataset_name: str
    topic: str | None = None
    dataset_description: str | None = None
    contact: str | None = None
    email: str | None = None
    date_update: str | None = None
    compression_format: str | None = None
    file_type: str | None = None
    file_location: str | None = None
    file_size: str | None = None
    file_rows: int | None = None
    raw_json: str | None = None

    @property
    def code(self) -> str:
        # Convenience alias used all over the codebase; ``dataset_code`` is the
        # canonical name but ``rec.code`` reads better at call sites.
        return self.dataset_code


@dataclass(frozen=True)
class MetadataSnapshot:
    """A fetched copy of the bulk metadata plus its content hash.

    ``sha256`` is the hash of the *raw bytes* exactly as downloaded — this is the
    reproducibility anchor recorded into ``faostat_build.metadata_snapshot_sha256``
    so a rebuild can prove it started from the same inventory.
    """

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
    """Convenience: fetch the bytes and parse them in one call."""
    return parse_metadata(fetch_metadata_bytes(url), url=url)


def _extract_entries(doc: Any) -> list[dict[str, Any]]:
    """Dig the list of dataset entries out of the JSON, tolerating shape drift."""
    datasets = doc.get("Datasets", doc) if isinstance(doc, dict) else doc
    if isinstance(datasets, dict):
        datasets = datasets.get("Dataset", [])
    if isinstance(datasets, dict):  # single-record edge case
        datasets = [datasets]
    return list(datasets or [])


def _record_from_entry(e: dict[str, Any]) -> DatasetRecord:
    """Build a :class:`DatasetRecord` from one raw JSON entry.

    ``pick`` returns the first present, non-empty value among candidate key names
    so we survive casing/renaming differences between FAOSTAT metadata revisions.
    """

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
        topic=_opt_str(pick("Topic", "topic")),
        dataset_description=_opt_str(pick("DatasetDescription", "datasetDescription")),
        contact=_opt_str(pick("Contact", "contact")),
        email=_opt_str(pick("Email", "email")),
        date_update=_opt_str(pick("DateUpdate", "dateUpdate")),
        compression_format=_opt_str(pick("CompressionFormat", "compressionFormat")),
        file_type=_opt_str(pick("FileType", "fileType")),
        file_location=_opt_str(pick("FileLocation", "fileLocation", "URL")),
        file_size=_opt_str(pick("FileSize", "fileSize")),
        file_rows=rows_int,
        # Store the untouched source entry so the exact metadata is auditable.
        raw_json=json.dumps(e, ensure_ascii=False, sort_keys=True),
    )


def _opt_str(v: Any) -> str | None:
    """Return a stripped string, or ``None`` for missing / empty values."""
    return str(v).strip() if v not in (None, "") else None


def select_datasets(
    datasets: list[DatasetRecord], selection: DatasetsConfig
) -> list[DatasetRecord]:
    """Apply ``all`` / ``include`` / ``exclude`` selection to dataset records.

    Codes are compared case-insensitively. ``include`` wins when the mode is
    ``include``; ``exclude`` removes the listed codes; ``all`` returns everything.
    """
    mode = (selection.mode or "all").lower()
    if mode == "include":
        wanted = {c.upper() for c in selection.include}
        return [d for d in datasets if d.code.upper() in wanted]
    if mode == "exclude":
        blocked = {c.upper() for c in selection.exclude}
        return [d for d in datasets if d.code.upper() not in blocked]
    return list(datasets)


def parse_size_bytes(value: str | None) -> int | None:
    """Best-effort parse of FAOSTAT's human file-size string (e.g. ``"77KB"``) to bytes.

    FAOSTAT publishes sizes like ``"182MB"`` or ``"1.2GB"``. We use binary units
    (1 KB = 1024 bytes) which matches how the archives actually land on disk
    closely enough for a *rough* estimate — this feeds the pre-build size summary,
    not any correctness check. Returns ``None`` when the string can't be parsed.
    """
    if not value:
        return None
    text = value.strip().upper().replace(" ", "")
    # Order matters: check multi-letter suffixes before the bare "B".
    units = [("KB", 1024), ("MB", 1024**2), ("GB", 1024**3), ("TB", 1024**4), ("B", 1)]
    for suffix, factor in units:
        if text.endswith(suffix):
            num = text[: -len(suffix)]
            try:
                return int(float(num) * factor)
            except ValueError:
                return None
    try:
        return int(float(text))
    except ValueError:
        return None
