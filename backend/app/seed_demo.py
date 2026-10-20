"""Validate and seed the clearly synthetic CampusLens demo fixture.

The live path deliberately uses the same RLS transaction setup as the API.  It
does not use a service key, create Auth users, or disable RLS.  Running without
``--apply`` is a validation/dry-run and is therefore safe by default.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from . import database

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / "docs" / "DEMO_DATA.json"
MARKER_PREFIX = "[CampusLens synthetic demo:"
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_STATUSES = {"Reported", "In Progress", "Resolved"}


@dataclass(frozen=True)
class DemoRecord:
    key: str
    description: str
    location: str
    category: str
    status: str

    @property
    def marker(self) -> str:
        return f"{MARKER_PREFIX} {self.key}]"

    @property
    def seeded_description(self) -> str:
        return f"{self.marker} {self.description}"


def _error(message: str) -> ValueError:
    return ValueError(f"Invalid demo data: {message}")


def validate_payload(payload: Any) -> list[DemoRecord]:
    """Validate the fixture and return immutable records for seeding."""
    if not isinstance(payload, dict):
        raise _error("top level must be an object")
    notice = payload.get("notice")
    if not isinstance(notice, str) or "synthetic" not in notice.lower():
        raise _error("notice must explicitly identify synthetic data")
    rows = payload.get("complaints")
    if not isinstance(rows, list) or not rows:
        raise _error("complaints must be a non-empty list")

    result: list[DemoRecord] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise _error(f"complaint {index} must be an object")
        key = row.get("demo_id")
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise _error(f"complaint {index} needs a valid demo_id")
        if key in seen:
            raise _error(f"duplicate demo_id {key!r}")
        seen.add(key)
        description = row.get("description")
        location = row.get("location")
        category = row.get("category")
        status = row.get("status", "Reported")
        if not isinstance(description, str) or not 10 <= len(description) <= 4000:
            raise _error(f"{key}: description must be 10-4000 characters")
        if not isinstance(location, str) or not 2 <= len(location) <= 200:
            raise _error(f"{key}: location must be 2-200 characters")
        if not isinstance(category, str) or not category.strip():
            raise _error(f"{key}: category must be non-empty text")
        if status not in _STATUSES:
            raise _error(f"{key}: status must be one of {sorted(_STATUSES)}")
        result.append(DemoRecord(key, description, location, category, status))
    return result


def load_payload(path: Path = DEFAULT_DATA) -> list[DemoRecord]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read demo data {path}: {exc}") from None
    return validate_payload(payload)


def seeded_keys(rows: Iterable[dict[str, Any]]) -> set[str]:
    """Return demo keys represented by complaint descriptions (pure/idempotent)."""
    keys: set[str] = set()
    for row in rows:
        description = str(row.get("description") or "")
        if description.startswith(MARKER_PREFIX) and "]" in description:
            keys.add(description[len(MARKER_PREFIX):].split("]", 1)[0].strip())
    return keys


def _uuid_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    try:
        UUID(value)
    except ValueError:
        raise ValueError(f"{name} must be an existing Supabase Auth user UUID") from None
    return value


def apply_seed(records: list[DemoRecord], database_url: str, student_id: str, admin_id: str) -> tuple[int, int]:
    """Insert missing records and update only their synthetic metadata/status."""
    inserted: list[tuple[DemoRecord, str]] = []
    created_count = 0
    # Validate both existing identities before changing any complaint. This
    # prevents a typo in the admin UUID from leaving a half-seeded fixture.
    with database.session(database_url, database.Identity(admin_id, "admin")) as conn:
        if database.load_role(conn, admin_id) != "admin":
            raise ValueError("DEMO_ADMIN_ID must refer to an existing admin profile")

    student = database.Identity(student_id, "student")
    with database.session(database_url, student) as conn:
        if database.load_role(conn, student_id) != "student":
            raise ValueError("DEMO_STUDENT_ID must refer to an existing student profile")
        for record in records:
            existing = conn.execute(
                "SELECT id FROM public.complaints WHERE student_id = %s AND description = %s",
                (student_id, record.seeded_description),
            ).fetchone()
            if existing:
                inserted.append((record, str(existing["id"])))
                continue
            row = database.create_complaint(
                conn,
                student_id=student_id,
                description=record.seeded_description,
                location_raw=record.location,
                location_code=None,
                image_path=None,
            )
            database.record_created_event(conn, str(row["id"]), student_id, "student")
            inserted.append((record, str(row["id"])))
            created_count += 1

    admin = database.Identity(admin_id, "admin")
    with database.session(database_url, admin) as conn:
        for record, complaint_id in inserted:
            # This existing SECURITY DEFINER function is intentionally limited to
            # the seven advisory ai_* columns; it cannot alter score/status.
            database.store_ai_suggestion(
                conn,
                complaint_id,
                {
                    "ai_category": record.category,
                    "ai_summary": "Synthetic demo classification; verify before acting.",
                    "ai_evidence": "Fixture supplied by the CampusLens demo data file.",
                    "ai_uncertainty": "Synthetic example; not a real campus report.",
                    "ai_urgency": None,
                    "ai_impact": None,
                    "ai_safety": None,
                },
            )
            current = database.get_complaint(conn, complaint_id)
            if current and current["status"] != record.status:
                previous = current["status"]
                database.update_status(conn, complaint_id, record.status)
                database.record_status_event(
                    conn, complaint_id, admin_id, "admin", previous, record.status
                )
    return len(inserted), created_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--apply", action="store_true", help="write through normal RLS sessions")
    args = parser.parse_args(argv)
    try:
        records = load_payload(args.data)
        print(f"Validated {len(records)} synthetic demo records from {args.data}.")
        if not args.apply:
            print("Dry run only; pass --apply with DEMO_STUDENT_ID and DEMO_ADMIN_ID to write.")
            return 0
        database_url = (os.environ.get("DATABASE_URL") or "").strip()
        if not database_url:
            raise ValueError("DATABASE_URL is required with --apply")
        student_id = _uuid_env("DEMO_STUDENT_ID")
        admin_id = _uuid_env("DEMO_ADMIN_ID")
        total, created = apply_seed(records, database_url, student_id, admin_id)
        print(f"Seed complete: {total} records present ({total - created} already existed).")
        return 0
    except (ValueError, database.DatabaseError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
