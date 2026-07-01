"""
Archive integrity validation.

Validation uses Python's stdlib ``zipfile.testzip()`` — never an external
``zip -T``. An optional size check compares the archive against the size declared
in the bulk metadata.
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    sha256: str | None = None
    bad_file: str | None = None
    reason: str | None = None


def sha256_of(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Compute the SHA256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def validate_zip(path: Path, *, compute_hash: bool = True) -> ValidationResult:
    """Validate ZIP integrity with ``testzip()`` and optionally hash the archive."""
    if not path.exists():
        return ValidationResult(ok=False, reason="missing")
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
    except zipfile.BadZipFile as exc:
        return ValidationResult(ok=False, reason=f"bad zip: {exc}")
    if bad is not None:
        return ValidationResult(ok=False, bad_file=bad, reason="crc mismatch")
    sha = sha256_of(path) if compute_hash else None
    return ValidationResult(ok=True, sha256=sha)


def check_size(path: Path, declared_bytes: int | None, *, tolerance: float = 0.0) -> bool:
    """Optional size check against a declared byte count. ``None`` declared -> True."""
    if declared_bytes is None:
        return True
    actual = path.stat().st_size
    if tolerance <= 0:
        return actual == declared_bytes
    return abs(actual - declared_bytes) <= declared_bytes * tolerance
