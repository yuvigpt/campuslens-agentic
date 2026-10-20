from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.seed_demo import DEFAULT_DATA, load_payload, seeded_keys, validate_payload


def test_checked_in_fixture_is_valid_and_has_unique_stable_keys():
    records = load_payload(DEFAULT_DATA)
    assert len(records) == 12
    assert len({record.key for record in records}) == len(records)
    assert all(record.marker.startswith("[CampusLens synthetic demo:") for record in records)


def test_validation_rejects_non_synthetic_or_duplicate_payload():
    base = {"notice": "synthetic demo only", "complaints": [{
        "demo_id": "one", "description": "A sufficiently long fixture report",
        "location": "Demo Hall", "category": "Other",
    }]}
    invalid_notice = {**base, "notice": "ordinary records"}
    with pytest.raises(ValueError, match="synthetic"):
        validate_payload(invalid_notice)

    duplicate = {**base, "complaints": [base["complaints"][0], base["complaints"][0]]}
    with pytest.raises(ValueError, match="duplicate"):
        validate_payload(duplicate)


def test_seeded_keys_are_repeatable_and_ignore_unmarked_rows():
    records = load_payload(DEFAULT_DATA)
    rows = [{"description": records[0].seeded_description}, {"description": "real report"}]
    assert seeded_keys(rows) == {records[0].key}
    assert seeded_keys(rows) == seeded_keys(rows)


def test_validation_rejects_invalid_status(tmp_path: Path):
    payload = {
        "notice": "Synthetic demo records only",
        "complaints": [{
            "demo_id": "one", "description": "A sufficiently long fixture report",
            "location": "Demo Hall", "category": "Other", "status": "Closed",
        }],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="status"):
        load_payload(path)
