from datetime import datetime, timedelta, timezone

from app.scoring import score_related_complaints


def _row(cid, *, category="Lighting", location="Science Block, stairwell", text="broken light", days=1, status="Reported"):
    return {
        "id": cid, "ai_category": category, "location_raw": location,
        "description": text, "status": status,
        "created_at": datetime(2026, 1, 10, tzinfo=timezone.utc) - timedelta(days=days),
    }


def test_related_scoring_excludes_other_categories_and_resolved():
    target = _row("target")
    result = score_related_complaints(target, [
        _row("same"), _row("other", category="Plumbing"), _row("done", status="Resolved"),
    ], now=datetime(2026, 1, 11, tzinfo=timezone.utc))
    assert result["total_count"] == 1
    assert result["related"][0]["id"] == "same"
    assert "same category" in result["related"][0]["reasons"]


def test_related_scoring_is_top_ten_and_reports_total_count():
    target = _row("target")
    candidates = [_row(str(i), text="broken light", days=i + 1) for i in range(12)]
    result = score_related_complaints(target, candidates, now=datetime(2026, 1, 11, tzinfo=timezone.utc))
    assert result["total_count"] == 12
    assert len(result["related"]) == 10
    assert result["related"][0]["score"] >= result["related"][1]["score"]


def test_related_endpoint_is_admin_only(client):
    target = client.post(
        "/api/complaints", data={"description": "Broken light", "location": "Science Block"},
        headers={"Authorization": "Bearer " + client.tokens["student_a"]},
    ).json()["id"]
    assert client.get(
        f"/api/admin/complaints/{target}/related",
        headers={"Authorization": "Bearer " + client.tokens["student_a"]},
    ).status_code == 403
    response = client.get(
        f"/api/admin/complaints/{target}/related",
        headers={"Authorization": "Bearer " + client.tokens["admin"]},
    )
    assert response.status_code == 200
    assert response.json()["total_count"] == 0
