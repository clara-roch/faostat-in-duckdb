"""Metadata parsing + size parsing tests (offline, deterministic)."""

from __future__ import annotations

import json

from faostatdb.metadata import parse_metadata, parse_size_bytes


SAMPLE = json.dumps(
    {
        "Datasets": {
            "Dataset": [
                {
                    "DatasetCode": "AE",
                    "DatasetName": "ASTI Expenditures",
                    "Topic": "Investment",
                    "DatasetDescription": "Financial indicators.",
                    "Contact": "FAO",
                    "Email": "faostat@fao.org",
                    "DateUpdate": "2026-04-30T00:00:00",
                    "CompressionFormat": "zip",
                    "FileType": "csv",
                    "FileSize": "77KB",
                    "FileRows": "7789",
                    "FileLocation": "https://example.org/AE.zip",
                }
            ]
        }
    }
).encode("utf-8")


def test_parse_captures_all_fields():
    snap = parse_metadata(SAMPLE, url="https://example.org/datasets_E.json")
    assert len(snap.datasets) == 1
    rec = snap.datasets[0]
    assert rec.code == "AE"
    assert rec.topic == "Investment"
    assert rec.email == "faostat@fao.org"
    assert rec.compression_format == "zip"
    assert rec.file_type == "csv"
    assert rec.file_rows == 7789  # coerced to int
    assert rec.file_location.endswith("AE.zip")
    # The raw entry is preserved verbatim (for full-metadata reproducibility).
    assert rec.raw_json is not None
    assert json.loads(rec.raw_json)["DatasetCode"] == "AE"


def test_snapshot_hash_is_stable():
    a = parse_metadata(SAMPLE)
    b = parse_metadata(SAMPLE)
    assert a.sha256 == b.sha256
    assert len(a.sha256) == 64


def test_parse_size_bytes():
    assert parse_size_bytes("77KB") == 77 * 1024
    assert parse_size_bytes("1.2GB") == int(1.2 * 1024**3)
    assert parse_size_bytes("500") == 500
    assert parse_size_bytes(None) is None
    assert parse_size_bytes("not-a-size") is None
