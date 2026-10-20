from __future__ import annotations

from app import database
from app.agentic import orchestrate
from tests.conftest import ADMIN, FakeConn


def test_agentic_run_persists_steps_and_priority(store):
    result = orchestrate(
        description="Electrical socket is producing sparks in a lecture hall.",
        location="Hall 12 lecture hall",
    )
    conn = FakeConn(store)
    conn.identity = database.Identity(ADMIN, "admin")

    assert database.persist_agentic_run(conn, "complaint-1", result) is True
    assert len(store.agent_runs) == 1
    assert store.agent_runs[0]["id"] == result["run_id"]
    assert len(store.agent_steps) == len(result["agents"]) + 1
    priority = next(step for step in store.agent_steps if step["agent_name"] == "priority_engine")
    assert priority["output"]
    assert result["critic"]["result"] in store.agent_runs[0]["result"]


def test_agentic_persistence_is_best_effort():
    class BrokenConn:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("observability unavailable")

    assert database.persist_agentic_run(
        BrokenConn(), "complaint-1", {"run_id": "run-1", "agents": []}
    ) is False
