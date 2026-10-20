from app.agentic import orchestrate
from app.scoring import agentic_priority


def test_electrical_hazard_is_critical_and_routed():
    result = orchestrate(
        description="Electrical socket is producing sparks in a lecture hall.",
        location="Hall 12 lecture hall",
    )
    assert result["category"] == "Electrical"
    assert result["safety"] == "High"
    assert result["impact"] == "High"
    assert result["urgency"] == "High"
    assert result["department"] == "Electrical Maintenance"
    assert result["priority_score"] == 100
    assert result["priority_band"] == "Critical"
    assert len(result["agents"]) >= 8


def test_unknown_issue_requires_human_review_without_inventing_category():
    result = orchestrate(description="Something is broken.", location="Hall 12")
    assert result["category"] == "Other"
    assert result["human_review_required"] is True
    assert result["confidence"] < 0.55


def test_duplicate_boost_is_bounded():
    small = agentic_priority(safety="Low", impact="Low", urgency="Low", duplicate_count=0)
    many = agentic_priority(safety="Low", impact="Low", urgency="Low", duplicate_count=100)
    assert many["score"] - small["score"] == 6
    assert many["band"] != "Critical"


def test_same_location_semantic_matches_are_reported():
    result = orchestrate(
        description="Fan in A507 stopped working",
        location="Hall 12 A507",
        candidates=[
            {"id": "one", "description": "Ceiling fan dead in A507", "location_raw": "Hall 12 A507"},
            {"id": "two", "description": "Fan not working in A509", "location_raw": "Hall 12 A509"},
        ],
    )
    assert result["duplicate_count"] == 1
    assert result["similar_complaint_ids"] == ["one"]


def test_image_text_conflict_requires_review():
    result = orchestrate(
        description="The ceiling fan is broken in Hall 12 A507.",
        location="Hall 12 A507",
        image_available=True,
        image_observation={
            "image_observation": "Damaged electrical socket",
            "text_observation": "Broken ceiling fan",
            "conflict": True,
            "conflict_reason": "Image suggests a socket hazard while text reports a fan.",
            "image_quality": "good",
        },
    )
    assert result["human_review_required"] is True
    assert result["critic"]["result"] == "HUMAN_REVIEW_REQUIRED"
    assert "socket hazard" in result["critic"]["warnings"][0]
