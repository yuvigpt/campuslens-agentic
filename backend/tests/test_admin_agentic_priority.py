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


def test_admin_list_reads_persisted_agentic_priority(client):
    cid = _complaint(client)
    client.store.agent_runs.append({"id": "run-1", "complaint_id": cid, "status": "COMPLETED", "result": json.dumps(_result(92, "Critical"))})
    response = client.get("/api/admin/complaints", headers={"Authorization": "Bearer " + client.tokens["admin"]})
    assert response.status_code == 200
    rows = response.json()
    item = next(r for r in rows if r["id"] == cid)
    assert item["agentic_priority_score"] == 92
    assert item["agentic_priority_band"] == "Critical"


def test_admin_rerun_updates_persisted_run_and_returns_analysis(client):
    cid = _complaint(client)
    rerun_resp = client.post(f"/api/admin/complaints/{cid}/agentic-analysis", headers={"Authorization": "Bearer " + client.tokens["admin"]})
    assert rerun_resp.status_code == 200
    data = rerun_resp.json()
    assert data["priority_score"] is not None
    assert data["priority_band"] is not None

    detail_resp = client.get(f"/api/admin/complaints/{cid}", headers={"Authorization": "Bearer " + client.tokens["admin"]})
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["agentic_priority_score"] == data["priority_score"]
    assert detail["agentic_priority_band"] == data["priority_band"]
