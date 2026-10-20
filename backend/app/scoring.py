"""
CampusLens - deterministic priority scoring.

The five-factor formula agreed for Stage 2:

    Urgency     0-30     admin-confirmed
    Impact      0-25     admin-confirmed
    Recurrence  0-20     system-computed (90-day window, structured key)
    Age         0-15     system-computed (completed 24h periods since created_at)
    Safety      0-10     admin-confirmed
    ------------------------------------------------------
    total       0-100

Bands: Low 0-29 | Medium 30-54 | High 55-74 | Critical 75-100

Safety floor (D3): when confirmed Safety is High the band can never be lower
than Medium. The numeric score is NEVER altered - both `band_raw` and the
floored `band` are reported so the adjustment stays auditable.

AI suggestions are deliberately absent from this module. The scorer reads only
confirmed_* values, so no AI response can move a score.

Pure Python: imports nothing from FastAPI, psycopg or the AI client, so it can
be unit-tested and reasoned about on its own.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Input vocabularies
# ---------------------------------------------------------------------------
LEVELS: tuple[str, ...] = ("Low", "Medium", "High")

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_MIME: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)
ALLOWED_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp"}
)

# ---------------------------------------------------------------------------
# Weight tables - the single source of truth for the scoring system (D4)
# ---------------------------------------------------------------------------
URGENCY_POINTS: dict[str, int] = {"Low": 0, "Medium": 15, "High": 30}   # max 30
IMPACT_POINTS: dict[str, int] = {"Low": 0, "Medium": 12, "High": 25}    # max 25
SAFETY_POINTS: dict[str, int] = {"Low": 0, "Medium": 5, "High": 10}     # max 10

RECURRENCE_POINTS: dict[str, int] = {"None": 0, "Low": 7, "Medium": 13, "High": 20}
AGE_MAX = 15
MAX_POSSIBLE = 100

# Band boundaries (D2/D7) - the exact agreed values.
BAND_LOW_MAX = 29
BAND_MEDIUM_MAX = 54
BAND_HIGH_MAX = 74

BANDS: tuple[str, ...] = ("Critical", "High", "Medium", "Low")

# Recurrence window and age cap (D2/D1).
RECURRENCE_WINDOW_DAYS = 90
AGE_CAP_DAYS = 30

STATUSES: tuple[str, ...] = ("Reported", "In Progress", "Resolved")

# Agentic score weights are intentionally separate from the legacy confirmed
# score contract above.  This lets existing deployments and tests keep their
# five-factor score while new analysis responses get the requested explainable
# safety-first score.
AGENTIC_SAFETY_POINTS = {"High": 40, "Medium": 22, "Low": 8}
AGENTIC_IMPACT_POINTS = {"High": 25, "Medium": 15, "Low": 5}
AGENTIC_URGENCY_POINTS = {"High": 25, "Medium": 14, "Low": 6}
AGENTIC_CATEGORY_POINTS = {"Critical": 10, "Moderate": 6, "Minor": 3}

# The only complaints columns an admin-confirmed score may depend on.
SCORER_INPUTS: tuple[str, ...] = (
    "confirmed_urgency",
    "confirmed_impact",
    "confirmed_safety",
)


class ScoringError(ValueError):
    """A score could not be produced. Never returns a partial or guessed value."""


# ---------------------------------------------------------------------------
# Related complaint scoring
# ---------------------------------------------------------------------------
_RELATED_STOPWORDS = frozenset(
    "a an and are at in is of on the to with for from this that report reported complaint".split()
)


def _related_tokens(value: Any) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if token not in _RELATED_STOPWORDS
    }


def _related_date(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def score_related_complaints(
    target: dict[str, Any], candidates: list[dict[str, Any]], *, now: Optional[datetime] = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Rank active complaints related to ``target`` without mutating input.

    Category disagreement is a hard exclusion when both rows have a category.
    The remaining score is explainable and bounded: category (35), location /
    building (30), recency (15), and description text (20). Missing fields do
    not invent similarity; they simply contribute zero. The response contains
    the top ``limit`` rows and the count of all qualifying relationships.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    target_category = str(target.get("ai_category") or "").strip().casefold()
    target_location = str(target.get("location_raw") or "").strip().casefold()
    target_code = str(target.get("location_code") or "").strip().casefold()
    target_text = _related_tokens(target.get("description"))
    target_date = _related_date(target.get("created_at"))
    related: list[dict[str, Any]] = []

    for candidate in candidates:
        if str(candidate.get("id")) == str(target.get("id")):
            continue
        if str(candidate.get("status") or "").strip().casefold() == "resolved":
            continue
        candidate_category = str(candidate.get("ai_category") or "").strip().casefold()
        if target_category and candidate_category and target_category != candidate_category:
            continue

        score = 0.0
        reasons: list[str] = []
        if target_category and candidate_category and target_category == candidate_category:
            score += 35
            reasons.append("same category")

        candidate_location = str(candidate.get("location_raw") or "").strip().casefold()
        candidate_code = str(candidate.get("location_code") or "").strip().casefold()
        if target_code and candidate_code and target_code == candidate_code:
            score += 30
            reasons.append("same location code")
        elif target_location and candidate_location and target_location == candidate_location:
            score += 25
            reasons.append("same location")
        elif target_location and candidate_location and (
            target_location in candidate_location or candidate_location in target_location
        ):
            score += 15
            reasons.append("same building/location")
        else:
            target_building = (target_location.split(",", 1)[0].strip().split() or [""])[0]
            candidate_building = (candidate_location.split(",", 1)[0].strip().split() or [""])[0]
            if target_building and target_building == candidate_building:
                score += 10
                reasons.append("same building")

        candidate_date = _related_date(candidate.get("created_at"))
        if target_date and candidate_date:
            age_days = abs((reference - candidate_date).total_seconds()) / 86400
            target_age_days = abs((reference - target_date).total_seconds()) / 86400
            recency = max(0.0, 15.0 * (1.0 - min(90.0, max(age_days, target_age_days)) / 90.0))
            score += recency
            if recency >= 10:
                reasons.append("recent reports")

        candidate_text = _related_tokens(candidate.get("description"))
        if target_text and candidate_text:
            overlap = len(target_text & candidate_text) / len(target_text | candidate_text)
            text_points = 20.0 * overlap
            score += text_points
            if overlap >= 0.2:
                reasons.append("similar description")

        if score > 0:
            related.append({
                "id": str(candidate.get("id")),
                "score": round(score, 3),
                "reasons": reasons,
                "category": candidate.get("ai_category"),
                "location": candidate.get("location_raw"),
                "created_at": candidate.get("created_at"),
                "status": candidate.get("status"),
                "priority_band": band_for_score(float(candidate["priority_score"]))
                if candidate.get("priority_score") is not None else None,
            })

    related.sort(key=lambda item: (-item["score"], item["id"]))
    return {"total_count": len(related), "related": related[:limit]}


def agentic_priority(*, safety: Any, impact: Any, urgency: Any,
                     category_band: str = "Minor", duplicate_count: int = 0) -> dict[str, Any]:
    """Deterministic safety-first score for structured agent observations.

    Duplicate volume receives a bounded adjustment (0/2/4/6), and therefore can
    never turn a harmless report into a critical safety decision by itself.
    """
    try:
        s = str(safety).strip().title()
        i = str(impact).strip().title()
        u = str(urgency).strip().title()
        category = str(category_band).strip().title()
        if s not in AGENTIC_SAFETY_POINTS or i not in AGENTIC_IMPACT_POINTS or u not in AGENTIC_URGENCY_POINTS:
            raise ScoringError("Agentic levels must be Low, Medium or High")
        if category not in AGENTIC_CATEGORY_POINTS:
            category = "Minor"
        count = max(0, int(duplicate_count or 0))
    except (TypeError, ValueError) as exc:
        raise ScoringError("Invalid agentic score inputs") from exc
    duplicate_boost = 0 if count <= 1 else 2 if count <= 4 else 4 if count <= 9 else 6
    base = AGENTIC_SAFETY_POINTS[s] + AGENTIC_IMPACT_POINTS[i] + AGENTIC_URGENCY_POINTS[u] + AGENTIC_CATEGORY_POINTS[category]
    score = min(100, base + duplicate_boost)
    band = "Critical" if score >= 75 else "High" if score >= 50 else "Medium" if score >= 25 else "Low"
    return {
        "score": score, "band": band,
        "breakdown": {
            "safety": {"level": s, "points": AGENTIC_SAFETY_POINTS[s], "max": 40},
            "impact": {"level": i, "points": AGENTIC_IMPACT_POINTS[i], "max": 25},
            "urgency": {"level": u, "points": AGENTIC_URGENCY_POINTS[u], "max": 25},
            "category": {"band": category, "points": AGENTIC_CATEGORY_POINTS[category], "max": 10},
            "duplicates": {"count": count, "boost": duplicate_boost, "max": 6},
            "total": score, "version": "agentic-v1",
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def canonical_level(value: Any, field: str) -> str:
    """Normalise a level string, or raise."""
    if not isinstance(value, str):
        raise ScoringError(f"{field} must be one of {list(LEVELS)}")
    candidate = value.strip().title()
    if candidate not in LEVELS:
        raise ScoringError(f"{field} must be one of {list(LEVELS)}")
    return candidate


def age_points(age_days: int) -> int:
    """min(15, ceil(age_days * 0.5)).

    `age_days` is always a count of COMPLETED 24-hour periods (never calendar
    days); callers compute it via `age_days_between`. This one rule is used by
    the API, the worker and every test.
    """
    if age_days < 0:
        raise ScoringError("age_days cannot be negative")
    return min(AGE_MAX, math.ceil(age_days * 0.5))


def age_days_between(created_at: datetime, now: Optional[datetime] = None) -> int:
    """Completed 24-hour periods between created_at and now (UTC).

    Calendar-day arithmetic is deliberately avoided: it is timezone dependent
    and would make the score drift at midnight.
    """
    reference = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    elapsed = (reference - created_at).total_seconds()
    if elapsed <= 0:
        return 0
    return int(elapsed // 86400)


def recurrence_points(count: int, *, not_applicable: bool = False) -> int:
    """Map a prior-similar-complaint count onto 0-20.

    `not_applicable` (D10) short-circuits to 0: a complaint with no valid
    location_code, or one an admin excluded, is never grouped.
    """
    if not_applicable:
        return 0
    if count < 0:
        raise ScoringError("recurrence count cannot be negative")
    if count == 0:
        return 0
    if count <= 2:
        return RECURRENCE_POINTS["Low"]       # 7
    if count <= 5:
        return RECURRENCE_POINTS["Medium"]    # 13
    return RECURRENCE_POINTS["High"]          # 20


def band_for_score(score: float) -> str:
    """Band for a numeric score, using the agreed boundaries."""
    if score < 0 or score > MAX_POSSIBLE:
        raise ScoringError("score must be between 0 and 100")
    if score <= BAND_LOW_MAX:
        return "Low"
    if score <= BAND_MEDIUM_MAX:
        return "Medium"
    if score <= BAND_HIGH_MAX:
        return "High"
    return "Critical"


def apply_safety_floor(band: str, safety: str) -> str:
    """D3: a confirmed High safety can never be banded Low.

    Only the BAND is floored. The numeric score is returned untouched.
    """
    if safety == "High" and band == "Low":
        return "Medium"
    return band


# ---------------------------------------------------------------------------
# Submission-time deterministic estimate (BCIP-1144)
# ---------------------------------------------------------------------------


def _level_to_points(level: Any, table: dict[str, float]) -> float:
    """Map a level string onto its points; anything unknown falls back to 0 (LOW)."""
    try:
        return float(table.get(str(level).strip().title(), "Low"))
    except (AttributeError, TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Score:
    """Pure in-memory estimate result (submission-time only, never persisted)."""

    total: float
    band: str
    breakdown: dict[str, Any]
    urgency: float
    impact: float
    recurrence: float
    age: float
    safety: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "band": self.band,
            "breakdown": self.breakdown,
            "urgency": self.urgency,
            "impact": self.impact,
            "recurrence": self.recurrence,
            "age": self.age,
            "safety": self.safety,
        }


def estimate_score_complaint(
    *,
    urgency: str = "Low",
    impact: str = "Low",
    safety: str = "Low",
    age_days: float = 0.0,
    recurrence_count: int = 0,
    recurrence_not_applicable: bool = True,
) -> Score:
    """Submission-time priority estimate (BCIP-1144).

    Pure in memory: no database, no network, no external AI.  Unknown or
    missing levels fall back to LOW(0), so a complaint can never fail to be
    given a deterministic band.  New complaints have safety LOW(0); recurrence
    is DISABLED (0) while the structured campus location/category reference
    data is unavailable, and any structured recurrence fields present are only
    used to decide that it is disabled (0=no match, 1=open, 2=recurrence tier).

    Age uses the same helper and specification as ``scoring.age_points``
    (max 15, floor 0).  Total is 0-100; bands are Low 0-29 | Medium 30-54 |
    High 55-74 | Critical 75-100.  The safety floor only raises the DISPLAYED
    band (Low -> Medium when safety is High); the raw total is never changed.

    Called exactly once per submission inside ``submit_complaint``.  The
    estimate is shown to the student and is NEVER persisted.  The
    admin-confirmed score is produced separately by ``score_complaint`` in
    ``confirm_and_score`` and written to the database there.
    """
    urgency_raw = _level_to_points(urgency, URGENCY_POINTS)
    impact_raw = _level_to_points(impact, IMPACT_POINTS)
    safety_raw = _level_to_points(safety, SAFETY_POINTS)
    # Age uses the same helper/specification as scoring.age_points: max 15,
    # floor 0 (age_days is a count of completed 24-hour periods).
    age_raw = age_points(int(age_days)) if age_days >= 0 else 0
    recurrence_raw = recurrence_points(
        recurrence_count, not_applicable=recurrence_not_applicable
    )

    total = round(urgency_raw + impact_raw + safety_raw + age_raw + recurrence_raw, 4)
    band_raw = band_for_score(total)
    band = apply_safety_floor(band_raw, safety)

    return Score(
        total=total,
        band=band,
        breakdown={
            "urgency": urgency_raw,
            "impact": impact_raw,
            "recurrence": recurrence_raw,
            "age": age_raw,
            "safety": safety_raw,
        },
        urgency=urgency_raw,
        impact=impact_raw,
        recurrence=recurrence_raw,
        age=age_raw,
        safety=safety_raw,
    )


def score_complaint(
    *,
    urgency: Any,
    impact: Any,
    safety: Any,
    age_days: int,
    recurrence_count: int,
    recurrence_not_applicable: bool = False,
) -> ScoreResult:
    """Score one complaint from admin-confirmed levels plus two derived inputs.

    Raises ScoringError - and returns NO partial score - when a confirmed level
    is missing or invalid. An unscored complaint is represented by SQL NULL, not
    by a guess.
    """
    u = canonical_level(urgency, "confirmed_urgency")
    i = canonical_level(impact, "confirmed_impact")
    s = canonical_level(safety, "confirmed_safety")

    a_pts = age_points(age_days)
    r_pts = recurrence_points(recurrence_count, not_applicable=recurrence_not_applicable)

    raw_total = URGENCY_POINTS[u] + IMPACT_POINTS[i] + SAFETY_POINTS[s] + r_pts + a_pts
    total = min(MAX_POSSIBLE, max(0.0, float(raw_total)))

    band_raw = band_for_score(total)
    band = apply_safety_floor(band_raw, s)

    breakdown: dict[str, Any] = {
        "urgency": {"level": u, "points": float(URGENCY_POINTS[u]), "max": 30},
        "impact": {"level": i, "points": float(IMPACT_POINTS[i]), "max": 25},
        "recurrence": {
            "count": recurrence_count,
            "points": float(r_pts),
            "max": 20,
            "window_days": RECURRENCE_WINDOW_DAYS,
            "not_applicable": recurrence_not_applicable,
        },
        "age": {
            "days": age_days,
            "points": float(a_pts),
            "max": AGE_MAX,
            "rule": "completed 24-hour periods",
        },
        "safety": {"level": s, "points": float(SAFETY_POINTS[s]), "max": 10},
        "total": total,
        "band": band,
        "band_raw": band_raw,
        "safety_floor_applied": band != band_raw,
        "max_possible": MAX_POSSIBLE,
        "version": "v1",
    }

    return ScoreResult(
        score=total,
        band=band,
        band_raw=band_raw,
        safety_floor_applied=band != band_raw,
        breakdown=breakdown,
    )


@dataclass(frozen=True)
class ScoreResult:
    score: float
    band: str
    band_raw: str
    safety_floor_applied: bool
    breakdown: dict[str, Any]
