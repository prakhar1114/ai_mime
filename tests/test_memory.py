"""Tests for local ChromaDB memory store and semantic search."""
from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from ai_mime.memory.store import MemoryStore
from ai_mime.provider_settings import read_memory_threshold, write_memory_threshold


@pytest.fixture
def temp_memory_store():
    with tempfile.TemporaryDirectory(prefix="test-ai-mime-memory-") as tmpdir:
        tmp_path = Path(tmpdir)
        db_dir = tmp_path / "chroma_db"
        models_dir = tmp_path / "onnx_models"
        store = MemoryStore(db_dir=db_dir, models_dir=models_dir)
        yield store


def test_memory_store_and_get(temp_memory_store):
    res = temp_memory_store.store_memory("User prefers dark mode for IDEs.", metadata={"env": "dev"})
    assert res["id"].startswith("mem_")
    assert res["content"] == "User prefers dark mode for IDEs."
    assert res["metadata"] == {"env": "dev"}

    fetched = temp_memory_store.get_memory(res["id"])
    assert fetched is not None
    assert fetched["id"] == res["id"]
    assert fetched["content"] == "User prefers dark mode for IDEs."
    assert fetched["metadata"] == {"env": "dev"}


def test_memory_delete(temp_memory_store):
    res = temp_memory_store.store_memory("Temporary note to delete")
    mem_id = res["id"]

    assert temp_memory_store.get_memory(mem_id) is not None
    deleted = temp_memory_store.delete_memory(mem_id)
    assert deleted is True
    assert temp_memory_store.get_memory(mem_id) is None


def test_semantic_search_and_threshold(temp_memory_store):
    temp_memory_store.store_memory("Python is a popular programming language.")
    temp_memory_store.store_memory("Dark mode user interface theme preference.")
    temp_memory_store.store_memory("Cooking recipe for homemade pizza.")

    # Search for coding / Python
    results = temp_memory_store.search_memory("python programming language", limit=10, threshold=0.3)
    assert len(results) >= 1
    assert "Python" in results[0]["content"]
    assert "score" in results[0]
    assert "date" in results[0]
    assert results[0]["date"] != ""
    assert results[0]["score"] >= 0.3

    # Search with strict threshold
    strict_results = temp_memory_store.search_memory("python programming language", limit=10, threshold=0.999)
    # Extremely strict threshold should filter out less exact vector matches
    assert all(item["score"] >= 0.999 for item in strict_results)


def test_semantic_search_pagination(temp_memory_store):
    for i in range(15):
        temp_memory_store.store_memory(f"System log entry number {i} regarding database operations.")

    # Fetch page 1 (top 10)
    page1 = temp_memory_store.search_memory("database operations", limit=10, offset=0, threshold=0.1)
    assert len(page1) == 10

    # Fetch page 2 (next 5)
    page2 = temp_memory_store.search_memory("database operations", limit=10, offset=10, threshold=0.1)
    assert len(page2) <= 5

    # Page 1 and Page 2 should not overlap IDs
    page1_ids = {m["id"] for m in page1}
    page2_ids = {m["id"] for m in page2}
    assert page1_ids.isdisjoint(page2_ids)


def test_config_memory_threshold():
    with tempfile.TemporaryDirectory(prefix="test-config-") as tmpdir:
        cfg_path = Path(tmpdir) / "user_config.yml"
        
        # Default read should be 0.7
        assert read_memory_threshold(config_path=cfg_path) == 0.7

        # Write custom value
        write_memory_threshold(0.85, config_path=cfg_path)
        assert read_memory_threshold(config_path=cfg_path) == 0.85
