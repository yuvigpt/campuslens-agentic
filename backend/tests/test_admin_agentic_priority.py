import json


def _result(score=88, band="Critical"):
    return {
        "run_id": "run-1",
        "analysis_status": "completed",
        "priority_score": score,
        "priority_band": band,
        "priority_breakdown": {"safety": {"points": 40}, "total": score},
        "human_review_required": False,
    }


def _complaint(client):
    return client.post(
        "/api/complaints",
        data={"description": "Socket sparks", "location": "Hall 12"},
        headers={"Authorization": "Bearer " + client.tokens["student_a"]},
    ).json()["id"]


def test_admin_detail_reads_latest_persisted_agentic_priority(client):
    cid = _complaint(client)
    client.store.agent_runs.append({"id": "run-1", "complaint_id": cid, "status": "COMPLETED", "result": json.dumps(_result())})
    response = client.get(f"/api/admin/complaints/{cid}", headers={"Authorization": "Bearer " + client.tokens["admin"]})
    assert response.status_code == 200
    body = response.json()
    assert body["agentic_priority_score"] == 88
    assert body["agentic_priority_band"] == "Critical"
    assert body["agentic_priority_breakdown"]["total"] == 88


def test_admin_detail_without_agentic_run_has_null_agentic_priority(client):
    cid = _complaint(client)
    body = client.get(f"/api/admin/complaints/{cid}", headers={"Authorization": "Bearer " + client.tokens["admin"]}).json()
    assert body["agentic_priority_score"] is None
    assert body["agentic_priority_band"] is None
    assert body["agentic_priority_breakdown"] is None


def test_admin_detail_failed_run_does_not_fabricate_priority(client):
    cid = _complaint(client)
    client.store.agent_runs.append({"id": "run-failed", "complaint_id": cid, "status": "FAILED", "result": None})
    body = client.get(f"/api/admin/complaints/{cid}", headers={"Authorization": "Bearer " + client.tokens["admin"]}).json()
    assert body["agentic_priority_score"] is None
    assert body["agentic_priority_band"] is None
