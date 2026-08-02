"""Local on-device ChromaDB memory storage and semantic vector search."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import uuid
from pathlib import Path
from typing import Any

from ai_mime.app_data import APP_DATA_DIR, get_onnx_models_dir
from ai_mime.provider_settings import read_memory_threshold

_STORE_INSTANCE: MemoryStore | None = None


def get_chroma_db_dir() -> Path:
    """Get the directory where ChromaDB persistent data lives."""
    return APP_DATA_DIR / "chroma_db"


def ensure_embedding_model_installed(models_dir: Path | None = None) -> Path:
    """Ensure the ONNX embedding model is downloaded into APP_DATA_DIR / onnx_models."""
    import chromadb.utils.embedding_functions as ef

    target_dir = models_dir or get_onnx_models_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    
    fn = ef.ONNXMiniLM_L6_V2()
    fn.DOWNLOAD_PATH = str(target_dir)
    fn._download_model_if_not_exists()
    return target_dir


class MemoryStore:
    """Persistent ChromaDB memory engine running fully on-device."""

    def __init__(self, db_dir: Path | None = None, models_dir: Path | None = None):
        import chromadb
        import chromadb.utils.embedding_functions as ef

        self.db_dir = db_dir or get_chroma_db_dir()
        self.models_dir = models_dir or get_onnx_models_dir()
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)

        self.embedding_fn = ef.ONNXMiniLM_L6_V2()
        self.embedding_fn.DOWNLOAD_PATH = str(self.models_dir)
        self.embedding_fn._download_model_if_not_exists()

        try:
            self.collection = self.client.get_or_create_collection(
                name="ai_mime_memories",
                embedding_function=self.embedding_fn,
            )
        except ValueError:
            self.collection = self.client.get_collection(name="ai_mime_memories")

    def store_memory(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a text snippet into ChromaDB with optional metadata."""
        if not content or not content.strip():
            raise ValueError("Memory content cannot be empty.")

        memory_id = f"mem_{uuid.uuid4()}"
        now_iso = datetime.now(timezone.utc).isoformat()
        
        meta = {
            "created_at": now_iso,
            "updated_at": now_iso,
            "custom_metadata_json": json.dumps(metadata or {}),
        }

        self.collection.add(
            ids=[memory_id],
            documents=[content.strip()],
            metadatas=[meta],
        )

        return {
            "id": memory_id,
            "content": content.strip(),
            "metadata": metadata or {},
            "created_at": now_iso,
            "updated_at": now_iso,
        }

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        """Fetch a single memory record by ID."""
        res = self.collection.get(ids=[memory_id])
        if not res or not res.get("ids") or len(res["ids"]) == 0:
            return None

        doc = res["documents"][0]
        meta = res["metadatas"][0] if res.get("metadatas") else {}
        custom_meta = {}
        if meta and "custom_metadata_json" in meta:
            try:
                custom_meta = json.loads(meta["custom_metadata_json"])
            except Exception:
                custom_meta = {}

        return {
            "id": res["ids"][0],
            "content": doc,
            "metadata": custom_meta,
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
        }

    def search_memory(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Perform semantic vector search returning top N results matching score >= threshold with pagination."""
        if not query or not query.strip():
            return []

        if threshold is None:
            threshold = read_memory_threshold()

        total_count = self.collection.count()
        if total_count == 0:
            return []

        # Request candidates from ChromaDB
        fetch_k = min(max(total_count, 1), max(limit + offset + 20, 100))
        results = self.collection.query(
            query_texts=[query.strip()],
            n_results=fetch_k,
            include=["documents", "metadatas", "distances"],
        )

        ids = results.get("ids", [[]])[0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        candidates = []
        for memory_id, doc, meta, dist in zip(ids, documents, metadatas, distances):
            # Convert cosine distance d in [0, 2] to similarity score s in [0, 1]
            score = max(0.0, min(1.0, 1.0 - float(dist)))
            
            if score >= threshold:
                custom_meta = {}
                if meta and "custom_metadata_json" in meta:
                    try:
                        custom_meta = json.loads(meta["custom_metadata_json"])
                    except Exception:
                        custom_meta = {}

                candidates.append({
                    "id": memory_id,
                    "content": doc,
                    "metadata": custom_meta,
                    "score": round(score, 4),
                    "created_at": meta.get("created_at", "") if meta else "",
                    "updated_at": meta.get("updated_at", "") if meta else "",
                })

        # Sort candidates by score descending
        candidates.sort(key=lambda x: x["score"], reverse=True)

        # Apply offset and limit pagination
        return candidates[offset : offset + limit]

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory record by ID."""
        try:
            self.collection.delete(ids=[memory_id])
            return True
        except Exception:
            return False


def get_memory_store() -> MemoryStore:
    """Get or initialize singleton MemoryStore."""
    global _STORE_INSTANCE
    if _STORE_INSTANCE is None:
        _STORE_INSTANCE = MemoryStore()
    return _STORE_INSTANCE


def store_memory(content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return get_memory_store().store_memory(content, metadata)


def get_memory(memory_id: str) -> dict[str, Any] | None:
    return get_memory_store().get_memory(memory_id)


def search_memory(
    query: str,
    limit: int = 10,
    offset: int = 0,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    return get_memory_store().search_memory(query, limit=limit, offset=offset, threshold=threshold)


def delete_memory(memory_id: str) -> bool:
    return get_memory_store().delete_memory(memory_id)
