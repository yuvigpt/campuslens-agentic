"""CampusLens Agentic orchestration.

The orchestration layer deliberately returns observations, not a score.  The
observations are validated and then converted into a score by ``scoring.py``.
The default implementation is deterministic and local, which keeps demo mode
useful when a provider is unavailable while still giving us a clean seam for a
real model-backed provider.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Optional
from uuid import uuid4

LEVELS = ("Low", "Medium", "High")
CATEGORIES = (
    "Electrical", "Plumbing", "Infrastructure", "Fire Hazard", "Safety",
    "WiFi/IT", "Furniture", "Cleanliness", "Hostel", "Academic Facilities",
    "Security", "Other",
)
DEPARTMENTS = (
    "Electrical Maintenance", "Civil Maintenance", "Plumbing", "IT/WiFi",
    "Hostel Management", "Housekeeping", "Security", "Academic Facilities",
    "Grounds/Maintenance", "Safety Office", "Other",
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class AgentObservation:
    agent: str
    status: str = "COMPLETED"
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    output: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _has(text: str, *words: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in words)


def _same_location(left: str, right: str) -> bool:
    """Require meaningful location agreement, not just shared words."""
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return False
    # A conflicting room/asset code (A507 vs A509) is evidence against a merge.
    left_codes = {token for token in left_tokens if any(char.isdigit() for char in token) and any(char.isalpha() for char in token)}
    right_codes = {token for token in right_tokens if any(char.isdigit() for char in token) and any(char.isalpha() for char in token)}
    if left_codes and right_codes and left_codes.isdisjoint(right_codes):
        return False
    overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    return overlap >= 0.6


def understanding(description: str, location: str, image_available: bool = False) -> AgentObservation:
    text = f"{description} {location}".strip()
    rules = [
        (("spark", "sparking", "socket", "wire", "electric", "fan", "light"), "Electrical", "Electrical issue"),
        (("water", "leak", "leaking", "tap", "pipe", "no water"), "Plumbing", "Water or plumbing issue"),
        (("wifi", "wi-fi", "internet", "network"), "WiFi/IT", "Connectivity issue"),
        (("chair", "desk", "table", "bench"), "Furniture", "Furniture issue"),
        (("clean", "garbage", "trash", "dirty", "hygiene"), "Cleanliness", "Cleanliness issue"),
        (("security", "theft", "guard", "intruder"), "Security", "Security issue"),
        (("road", "floor", "ceiling", "wall", "door", "stair", "building"), "Infrastructure", "Infrastructure issue"),
    ]
    category, evidence = "Other", []
    for words, candidate, proof in rules:
        if _has(text, *words):
            category, evidence = candidate, [proof]
            break
    uncertainty = []
    if category == "Other":
        uncertainty.append("Complaint description is insufficient to identify the exact issue.")
    if not image_available:
        evidence.append("Classification is based on text and location only.")
    summary = description.strip()[:240] if description.strip() else "No description provided."
    confidence = 0.88 if category != "Other" else 0.28
    return AgentObservation("understanding", confidence=confidence, evidence=evidence,
                            uncertainty=uncertainty, output={"category": category, "summary": summary,
                            "extracted_entities": {"location": location.strip()},
                            "image_available": image_available})


def safety(description: str, understanding_result: AgentObservation) -> AgentObservation:
    text = description.lower()
    high = ("spark", "sparking", "smoke", "fire", "exposed wire", "collapse", "chemical")
    medium = ("leak", "leaking", "flood", "railing", "floor", "socket")
    if _has(text, *high):
        level, hazards, confidence = "High", ["Potential immediate physical or electrical hazard"], 0.92
    elif _has(text, *medium):
        level, hazards, confidence = "Medium", ["Potential maintenance safety concern"], 0.76
    else:
        level, hazards, confidence = "Low", [], 0.74
    return AgentObservation("safety", confidence=confidence, evidence=hazards or ["No high-risk keywords detected"],
                            output={"safety_level": level, "hazards": hazards})


def impact(description: str, location: str, understanding_result: AgentObservation) -> AgentObservation:
    text = f"{description} {location}"
    high = _has(text, "hostel", "lecture hall", "classroom", "laboratory", "wifi", "no water", "entire")
    medium = _has(text, "hall", "common", "corridor", "floor", "block")
    level = "High" if high else "Medium" if medium else "Low"
    evidence = ["Facility context suggests a shared campus space"] if high else ["Affected population was not given as an exact count"]
    return AgentObservation("impact", confidence=0.72 if not high else 0.82, evidence=evidence,
                            uncertainty=["Exact number of affected people is unknown"],
                            output={"impact_level": level, "affected_population": "high" if high else "estimated"})


def urgency(description: str, safety_result: AgentObservation) -> AgentObservation:
    level = safety_result.output.get("safety_level")
    if level == "High" or _has(description, "no water", "outage", "unavailable"):
        result = "High"
    elif level == "Medium" or _has(description, "not working", "broken", "leak"):
        result = "Medium"
    else:
        result = "Low"
    return AgentObservation("urgency", confidence=0.8, evidence=[f"Safety level is {level}"],
                            output={"urgency_level": result})


def duplicate(description: str, location: str, candidates: list[dict[str, Any]]) -> AgentObservation:
    current_tokens = _tokens(description)
    similar: list[dict[str, Any]] = []
    for row in candidates:
        other = row.get("description", "")
        other_loc = row.get("location_raw", "")
        if not other or other == description:
            continue
        lexical = SequenceMatcher(None, description.lower(), other.lower()).ratio()
        overlap = len(current_tokens & _tokens(other)) / max(1, len(current_tokens | _tokens(other)))
        location_match = _same_location(location, other_loc)
        if location_match and (lexical >= 0.52 or overlap >= 0.34):
            similar.append({"id": str(row.get("id")), "relation": "LIKELY SAME ISSUE", "similarity": round(max(lexical, overlap), 3)})
    group = None
    if similar:
        # Stable for the same candidate cluster: retries do not create a new
        # group id, and the first known complaint becomes the anchor.
        group = "dup-" + str(similar[0]["id"])[:12]
    return AgentObservation("duplicate", confidence=0.82 if similar else 0.65,
                            evidence=[f"{len(similar)} same-location candidate(s) matched"],
                            uncertainty=[] if similar else ["No sufficiently similar same-location complaint found"],
                            output={"duplicate_group_id": group, "duplicate_count": len(similar),
                                    "similar_complaint_ids": [item["id"] for item in similar],
                                    "matches": similar})


def routing(understanding_result: AgentObservation, safety_result: AgentObservation) -> AgentObservation:
    category = understanding_result.output.get("category")
    mapping = {"Electrical": "Electrical Maintenance", "Plumbing": "Plumbing", "WiFi/IT": "IT/WiFi",
               "Furniture": "Academic Facilities", "Cleanliness": "Housekeeping", "Infrastructure": "Civil Maintenance",
               "Security": "Security", "Fire Hazard": "Safety Office", "Safety": "Safety Office"}
    department = mapping.get(category, "Other")
    return AgentObservation("routing", confidence=0.9 if department != "Other" else 0.3,
                            evidence=[f"Category {category} maps to configured department"],
                            uncertainty=[] if department != "Other" else ["No configured department matched"],
                            output={"department": department})


def resolution(understanding_result: AgentObservation, safety_result: AgentObservation, routing_result: AgentObservation) -> AgentObservation:
    category = understanding_result.output.get("category")
    actions = {"Electrical": "Immediately isolate the affected electrical equipment and request inspection by electrical maintenance.",
               "Plumbing": "Inspect the affected plumbing line and isolate the leak or supply interruption if necessary.",
               "WiFi/IT": "Check access point connectivity and network status for the affected area.",
               "Furniture": "Inspect and repair or replace the damaged furniture.",
               "Cleanliness": "Dispatch housekeeping to inspect and restore the area.",
               "Infrastructure": "Inspect the affected facility and schedule the appropriate civil maintenance work."}
    return AgentObservation("resolution", confidence=0.84, evidence=["Action is a recommendation, not a completion claim"],
                             output={"recommended_action": actions.get(category, "Route to the appropriate campus operations team for human assessment.")})


def llm_analysis(analysis: Optional[dict[str, Any]]) -> Optional[AgentObservation]:
    """Turn the optional provider response into an auditable observation.

    Provider output is advisory.  In particular, its levels are deliberately
    kept separate from the deterministic observations used by the scorer.
    """
    if not analysis:
        return None
    category = analysis.get("ai_category") or analysis.get("category")
    summary = analysis.get("ai_summary") or analysis.get("summary")
    levels = {
        "urgency": analysis.get("ai_urgency") or analysis.get("urgency"),
        "impact": analysis.get("ai_impact") or analysis.get("impact"),
        "safety": analysis.get("ai_safety") or analysis.get("safety"),
    }
    levels = {key: value for key, value in levels.items() if value in LEVELS}
    evidence = [str(analysis["ai_evidence"])] if analysis.get("ai_evidence") else []
    uncertainty = [str(analysis["ai_uncertainty"])] if analysis.get("ai_uncertainty") else []
    output = {"category": category, "summary": summary, **levels}
    for key in ("image_observation", "text_observation", "image_text_conflict", "conflict", "conflict_reason", "image_quality"):
        if analysis.get(key) is not None:
            output[key] = analysis[key]
    return AgentObservation("llm", confidence=0.65, evidence=evidence,
                            uncertainty=uncertainty, output=output)


def critic(observations: dict[str, AgentObservation], image_observation: Optional[dict[str, Any]] = None) -> AgentObservation:
    warnings: list[str] = []
    if observations.get("understanding", AgentObservation("x")).output.get("category") == "Other":
        warnings.append("Category is uncertain.")
    if observations.get("safety", AgentObservation("x")).output.get("safety_level") == "High" and observations.get("urgency", AgentObservation("x")).output.get("urgency_level") != "High":
        warnings.append("Safety and urgency disagree.")
    llm = observations.get("llm")
    if llm:
        rule_category = observations.get("understanding", AgentObservation("x")).output.get("category")
        llm_category = llm.output.get("category")
        if llm_category and rule_category and llm_category != rule_category and llm_category != "Other" and rule_category != "Other":
            warnings.append(f"LLM category ({llm_category}) disagrees with rule category ({rule_category}).")
        for label, rule_key, llm_key in (("safety", "safety_level", "safety"), ("impact", "impact_level", "impact"), ("urgency", "urgency_level", "urgency")):
            rule_value = observations.get(label, AgentObservation("x")).output.get(rule_key)
            llm_value = llm.output.get(llm_key)
            if llm_value and rule_value and llm_value != rule_value:
                warnings.append(f"LLM {label} ({llm_value}) disagrees with rule {label} ({rule_value}).")
        if llm.output.get("image_text_conflict") is True or llm.output.get("conflict") is True:
            warnings.append(str(llm.output.get("conflict_reason") or "Image and complaint text describe materially different issues.")[:500])
    if image_observation:
        if image_observation.get("image_quality") in {"poor", "unclear"}:
            warnings.append("Image quality is insufficient for a reliable visual conclusion.")
        if image_observation.get("conflict") is True or image_observation.get("image_text_conflict") is True:
            reason = image_observation.get("conflict_reason") or "Image and complaint text describe materially different issues."
            warnings.append(str(reason)[:500])
    result = "HUMAN_REVIEW_REQUIRED" if warnings else "PASS"
    return AgentObservation("critic", confidence=0.9 if not warnings else 0.7, evidence=warnings or ["No contradictions detected"],
                            uncertainty=warnings, output={"result": result, "warnings": warnings})


def _run(name: str, fn: Callable[[], AgentObservation], timeout_seconds: float) -> AgentObservation:
    started = time.perf_counter()
    try:
        result = fn()
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
    except Exception as exc:  # agent failure is data, not a workflow crash
        return AgentObservation(name, status="FAILED", error=type(exc).__name__, confidence=0,
                                 uncertainty=["Agent failed; human review required"], duration_ms=int((time.perf_counter() - started) * 1000))


def orchestrate(*, description: str, location: str, candidates: Optional[list[dict[str, Any]]] = None,
                 image_available: bool = False, image_observation: Optional[dict[str, Any]] = None,
                 llm_analysis_result: Optional[dict[str, Any]] = None,
                 timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Run independent observations in parallel, then dependent agents."""
    candidates = candidates or []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="campus-agent") as pool:
        futures = {
            pool.submit(_run, "understanding", lambda: understanding(description, location, image_available), timeout_seconds): "understanding",
            pool.submit(_run, "safety", lambda: safety(description, understanding(description, location, image_available)), timeout_seconds): "safety",
            pool.submit(_run, "impact", lambda: impact(description, location, understanding(description, location, image_available)), timeout_seconds): "impact",
            pool.submit(_run, "urgency", lambda: urgency(description, safety(description, understanding(description, location, image_available))), timeout_seconds): "urgency",
            pool.submit(_run, "duplicate", lambda: duplicate(description, location, candidates), timeout_seconds): "duplicate",
        }
        observations = {name: future.result() for future, name in ((future, name) for future, name in futures.items())}
    route = _run("routing", lambda: routing(observations["understanding"], observations["safety"]), timeout_seconds)
    resolve = _run("resolution", lambda: resolution(observations["understanding"], observations["safety"], route), timeout_seconds)
    observations.update(routing=route, resolution=resolve)
    llm_result = llm_analysis(llm_analysis_result)
    if llm_result:
        observations["llm"] = llm_result
    else:
        # The deterministic agents remain available as a safe fallback, but a
        # missing provider observation is explicitly represented as failed so
        # the UI never implies that the complete LLM-backed workflow ran.
        observations["llm"] = AgentObservation(
            "llm", status="FAILED", confidence=0.0,
            uncertainty=["LLM analysis unavailable; human review required"],
            error="provider_unavailable",
        )
    observations["critic"] = _run("critic", lambda: critic(observations, image_observation), timeout_seconds)
    critic_result = observations["critic"]
    all_uncertainty = [u for result in observations.values() for u in result.uncertainty]
    confidence = round(min((result.confidence for result in observations.values()), default=0.0), 2)
    human_review = confidence < 0.55 or critic_result.output.get("result") != "PASS" or any(r.status != "COMPLETED" for r in observations.values())
    category = observations["understanding"].output.get("category", "Other")
    category_band = "Critical" if observations["safety"].output.get("safety_level") == "High" else "Moderate" if category not in {"Other", "Furniture", "Cleanliness"} else "Minor"
    from .scoring import agentic_priority
    priority = agentic_priority(
        safety=observations["safety"].output.get("safety_level", "Low"),
        impact=observations["impact"].output.get("impact_level", "Low"),
        urgency=observations["urgency"].output.get("urgency_level", "Low"),
        category_band=category_band,
        duplicate_count=observations["duplicate"].output.get("duplicate_count", 0),
    )
    return {
        "run_id": str(uuid4()), "analysis_status": "completed" if llm_result and not human_review else "failed_review_required",
        "category": category, "category_band": category_band,
        "summary": observations["understanding"].output.get("summary", description[:240]),
        "safety": observations["safety"].output.get("safety_level", "Low"),
        "impact": observations["impact"].output.get("impact_level", "Low"),
        "urgency": observations["urgency"].output.get("urgency_level", "Low"),
        "department": route.output.get("department", "Other"),
        "recommended_action": resolve.output.get("recommended_action"),
        "duplicate_group_id": observations["duplicate"].output.get("duplicate_group_id"),
        "duplicate_count": observations["duplicate"].output.get("duplicate_count", 0),
        "similar_complaint_ids": observations["duplicate"].output.get("similar_complaint_ids", []),
        "confidence": confidence, "uncertainty": list(dict.fromkeys(all_uncertainty)),
        "human_review_required": human_review,
        "priority_score": priority["score"], "priority_band": priority["band"],
        "priority_breakdown": priority["breakdown"],
        "critic": critic_result.output,
         "image_analysis": image_observation or {"image_available": image_available},
         "llm_analysis": llm_result.output if llm_result else None,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "agents": [result.to_dict() for result in observations.values()],
    }
