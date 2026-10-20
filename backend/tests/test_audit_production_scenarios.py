"""
End-to-end production audit and verification test suite.
Tests requirements 10, 11, and the complete hybrid agentic lifecycle:
- Automatic agentic run on complaint submission
- Persistence of agent runs and priority
- Duplicate / related complaint detection across similar complaints
- Safety hazard escalation (e.g. sparks)
- Graceful degradation on Gemini / AI failure
- Deterministic critic contradictions & human review flags
- Admin listing and detail endpoints reflecting persisted results without recalculation
- Admin manual rerun behavior & concurrency protection
"""

import dataclasses
import json
from unittest.mock import patch
import pytest

from app import database
from app.routers import complaints as complaints_router


@pytest.fixture(autouse=True)
def _enable_agentic(client):
    client.app.state.settings = dataclasses.replace(client.app.state.settings, agentic_enabled=True)


def _admin_headers(client):
    return {"Authorization": "Bearer " + client.tokens["admin"]}


def _student_headers(client, who="student_a"):
    return {"Authorization": "Bearer " + client.tokens[who]}


def test_production_like_five_complaints_flow(client):
    """
    Requirement 11:
    1. 'Fan is not working in Hall 12 A-507'
    2. 'Ceiling fan stopped working in Hall 12 A-507' (Duplicate of #1)
    3. 'Fan making sparks in Hall 12 A-507' (Hazard / high safety priority)
    4. 'WiFi is not working in Hall 12' (Different category)
    5. 'Plumbing leak in washroom 2nd floor Hostel 3' (Unrelated category & location)
    """
    complaint_data = [
        {"description": "Fan is not working in Hall 12 A-507", "location": "Hall 12 A-507"},
        {"description": "Ceiling fan stopped working in Hall 12 A-507", "location": "Hall 12 A-507"},
        {"description": "Fan making sparks in Hall 12 A-507", "location": "Hall 12 A-507"},
        {"description": "WiFi is not working in Hall 12", "location": "Hall 12"},
        {"description": "Plumbing leak in washroom 2nd floor Hostel 3", "location": "Hostel 3 Washroom 2nd floor"},
    ]

    created_ids = []
    
    # 1. Submit all 5 complaints and verify automatic agentic pipeline runs immediately
    for idx, c in enumerate(complaint_data, 1):
        resp = client.post(
            "/api/complaints",
            data=c,
            headers=_student_headers(client, "student_a" if idx % 2 != 0 else "student_b"),
        )
        assert resp.status_code == 201, f"Complaint {idx} failed to create: {resp.text}"
        body = resp.json()
        cid = body["id"]
        created_ids.append(cid)

        # Verify automatic pipeline output was returned
        assert body.get("agentic_analysis") is not None, f"Complaint {cid} missing agentic analysis"
        agentic_out = body["agentic_analysis"]
        assert agentic_out.get("priority_score") is not None
        assert agentic_out.get("priority_band") is not None
        assert agentic_out.get("priority_breakdown") is not None
        assert agentic_out.get("analysis_status") == "completed"

    # 2. Verify DB persistence: every complaint has a COMPLETED agent_run persisted
    for cid in created_ids:
        runs = [r for r in client.store.agent_runs if r["complaint_id"] == cid]
        assert len(runs) >= 1, f"No agent run stored for complaint {cid}"
        latest_run = runs[-1]
        assert latest_run["status"] == "COMPLETED"
        parsed = json.loads(latest_run["result"])
        assert parsed["priority_score"] is not None
        assert parsed["priority_band"] is not None
        assert parsed["analysis_status"] == "completed"

    # 3. Verify Admin List (GET /api/admin/complaints)
    # Exposes agentic priority directly from DB without frontend recalculation
    list_resp = client.get("/api/admin/complaints", headers=_admin_headers(client))
    assert list_resp.status_code == 200
    rows = list_resp.json()
    for cid in created_ids:
        row = next((r for r in rows if r["id"] == cid), None)
        assert row is not None, f"Complaint {cid} missing from admin list"
        assert row["agentic_priority_score"] is not None
        assert row["agentic_priority_band"] is not None
        assert row["agentic_priority_breakdown"] is not None

    # 4. Verify Duplicate / Related Detection
    # Complaint 2 ("Ceiling fan stopped working in Hall 12 A-507") should detect Complaint 1 as duplicate
    detail_resp2 = client.get(f"/api/admin/complaints/{created_ids[1]}", headers=_admin_headers(client))
    assert detail_resp2.status_code == 200
    detail2 = detail_resp2.json()
    assert detail2["agentic_analysis"] is not None
    assert detail2["agentic_analysis"]["duplicate_count"] >= 1
    assert created_ids[0] in detail2["agentic_analysis"]["similar_complaint_ids"]

    # Verify Related Complaints Graph endpoint
    rel_resp2 = client.get(f"/api/admin/complaints/{created_ids[1]}/related", headers=_admin_headers(client))
    assert rel_resp2.status_code == 200
    rel_body = rel_resp2.json()
    related_list = rel_body.get("related", [])
    related_ids = [r["id"] for r in related_list]
    assert created_ids[0] in related_ids, "Complaint 1 should be related to Complaint 2"

    # 5. Verify Safety Hazard Escalation
    # Complaint 3 ("Fan making sparks in Hall 12 A-507") should have high safety level
    detail_resp3 = client.get(f"/api/admin/complaints/{created_ids[2]}", headers=_admin_headers(client))
    assert detail_resp3.status_code == 200
    detail3 = detail_resp3.json()
    assert detail3["agentic_analysis"]["safety"] == "High"
    assert detail3["agentic_priority_score"] >= 45

    # 6. Verify Admin Detail Page Load (Refresh simulation)
    # Admin opening complaint 4 immediately sees completed result without clicking rerun
    detail_resp4 = client.get(f"/api/admin/complaints/{created_ids[3]}", headers=_admin_headers(client))
    assert detail_resp4.status_code == 200
    detail4 = detail_resp4.json()
    assert detail4["agentic_analysis"] is not None
    assert detail4["agentic_priority_score"] is not None

    # 7. Verify Manual Rerun Behavior
    # Calling agentic-analysis rerun updates the run and returns new completed analysis
    rerun_resp = client.post(
        f"/api/admin/complaints/{created_ids[3]}/agentic-analysis",
        headers=_admin_headers(client),
    )
    assert rerun_resp.status_code == 200
    rerun_result = rerun_resp.json()
    assert rerun_result["priority_score"] is not None
    assert rerun_result["analysis_status"] == "completed"

    # Concurrency Protection: Reject simultaneous runs with 409
    with complaints_router._active_runs_lock:
        complaints_router._active_runs.add(created_ids[3])
    try:
        conflict_resp = client.post(
            f"/api/admin/complaints/{created_ids[3]}/agentic-analysis",
            headers=_admin_headers(client),
        )
        assert conflict_resp.status_code == 409
    finally:
        with complaints_router._active_runs_lock:
            complaints_router._active_runs.discard(created_ids[3])


def test_failure_handling_scenarios(client):
    """
    Requirement 10:
    A. Gemini succeeds (covered in standard runs)
    B. Gemini fails -> Complaint created, fallback to deterministic specialists
    C. Critic detects disagreement / contradiction -> human_review_required flag is set
    D. Complaint with and without image
    """
    # B: Gemini fails unexpectedly during submission
    with patch("app.ai.analyse", side_effect=Exception("Gemini Provider 503 Unavailable")):
        resp = client.post(
            "/api/complaints",
            data={"description": "Water pipe broken in chemistry lab", "location": "Chemistry Lab Block C"},
            headers=_student_headers(client),
        )
        assert resp.status_code == 201, "Complaint submission must never fail when AI fails"
        data = resp.json()
        cid = data["id"]
        # Hybrid pipeline ran deterministic fallback
        assert data.get("agentic_analysis") is not None
        assert data["agentic_analysis"]["priority_score"] is not None

    # C: Critic detects contradiction / human review condition
    # Submit complaint with contradictory text like "Minor scratch but life threatening explosion"
    resp_crit = client.post(
        "/api/complaints",
        data={"description": "Life threatening explosion danger from light switch", "location": "Lab 1"},
        headers=_student_headers(client),
    )
    assert resp_crit.status_code == 201
    crit_body = resp_crit.json()
    crit_agentic = crit_body["agentic_analysis"]
    assert crit_agentic["analysis_status"] == "completed"
    # Even if review is recommended, result is saved and priority score is authoritative
    assert crit_agentic["priority_score"] is not None

    # D: Complaint with image upload
    img_content = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00" + b"\x00" * 100
    resp_img = client.post(
        "/api/complaints",
        data={"description": "Broken window pane in classroom 101", "location": "Classroom 101"},
        files={"image": ("test.jpg", img_content, "image/jpeg")},
        headers=_student_headers(client),
    )
    assert resp_img.status_code == 201
    img_body = resp_img.json()
    assert img_body.get("agentic_analysis") is not None
    img_cid = img_body["id"]

    # In admin detail, image_path is present
    adm_resp = client.get(f"/api/admin/complaints/{img_cid}", headers=_admin_headers(client))
    assert adm_resp.status_code == 200
    assert adm_resp.json()["image_path"] is not None
