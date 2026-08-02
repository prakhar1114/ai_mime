"""Tests for memory FastAPI endpoints in server.py."""
from __future__ import annotations

import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from ai_mime.editor.server import create_app


@pytest.fixture
def test_client():
    with tempfile.TemporaryDirectory(prefix="test-api-workflows-") as tmpdir:
        workflows_dir = Path(tmpdir) / "workflows"
        recordings_dir = Path(tmpdir) / "recordings"
        workflows_dir.mkdir(parents=True, exist_ok=True)
        recordings_dir.mkdir(parents=True, exist_ok=True)

        app = create_app(workflows_root=workflows_dir, recordings_root=recordings_dir)
        with TestClient(app) as client:
            yield client


def test_api_memory_lifecycle(test_client):
    # Store memory
    store_resp = test_client.post(
        "/api/memory/store",
        json={"content": "User prefers dark mode UI.", "metadata": {"source": "settings_panel"}},
    )
    assert store_resp.status_code == 200
    stored_data = store_resp.json()
    assert stored_data["id"].startswith("mem_")
    assert stored_data["content"] == "User prefers dark mode UI."
    assert stored_data["metadata"] == {"source": "settings_panel"}
    mem_id = stored_data["id"]

    # Get memory
    get_resp = test_client.get(f"/api/memory/get?memory_id={mem_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == mem_id

    # Search memory
    search_resp = test_client.post(
        "/api/memory/search",
        json={"query": "dark mode UI preference", "limit": 10, "threshold": 0.1},
    )
    assert search_resp.status_code == 200
    search_results = search_resp.json()
    assert isinstance(search_results, list)
    assert len(search_results) >= 1
    assert search_results[0]["id"] == mem_id
    assert "date" in search_results[0]
    assert search_results[0]["date"] != ""

    # Get & Set threshold
    thresh_get = test_client.get("/api/memory/threshold")
    assert thresh_get.status_code == 200
    assert "memory_threshold" in thresh_get.json()

    thresh_set = test_client.post("/api/memory/threshold", json={"threshold": 0.75})
    assert thresh_set.status_code == 200
    assert thresh_set.json()["memory_threshold"] == 0.75

    # Delete memory
    del_resp = test_client.delete(f"/api/memory/delete?memory_id={mem_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True

    # Confirm deleted
    get_deleted = test_client.get(f"/api/memory/get?memory_id={mem_id}")
    assert get_deleted.status_code == 404
