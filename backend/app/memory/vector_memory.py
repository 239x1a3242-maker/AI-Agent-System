"""
Vector memory layer (ChromaDB + sentence-transformers).
Stores/retrieves semantically relevant conversation snippets per chat_id.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import chromadb
    from sentence_transformers import SentenceTransformer
except Exception:  # pragma: no cover - optional dependency fallback
    chromadb = None
    SentenceTransformer = None


VECTOR_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "workspace", "vector_db")
VECTOR_DB_PATH = os.path.normpath(VECTOR_DB_PATH)
COLLECTION_NAME = "chat_memory"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
CHUNK_TRIGGER_CHARS = 500
CHUNK_SIZE = 400
CHUNK_OVERLAP = 50
SIMILARITY_THRESHOLD = 0.75

_client = None
_collection = None
_embedder = None
_init_failed = False


def _chunk_text(content: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    text = str(content or "").strip()
    if not text:
        return []
    if len(text) <= CHUNK_TRIGGER_CHARS:
        return [text]

    chunks: List[str] = []
    start = 0
    length = len(text)
    step = max(1, chunk_size - overlap)
    while start < length:
        end = min(length, start + chunk_size)
        chunks.append(text[start:end])
        if end >= length:
            break
        start += step
    return chunks


def initialize_vector_db() -> bool:
    """Initialize ChromaDB collection and embedding model lazily."""
    global _client, _collection, _embedder, _init_failed

    if _collection is not None and _embedder is not None:
        return True
    if _init_failed:
        return False
    if chromadb is None or SentenceTransformer is None:
        logger.warning("Vector memory disabled: chromadb/sentence-transformers not installed.")
        _init_failed = True
        return False

    try:
        os.makedirs(VECTOR_DB_PATH, exist_ok=True)
        _client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        _embedder = SentenceTransformer(EMBEDDING_MODEL)
        return True
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Vector memory initialization failed: %s", exc)
        _init_failed = True
        return False


def store_message(chat_id: str, role: str, content: str) -> bool:
    """Store a message in vector memory with chunking and metadata."""
    if not str(content or "").strip():
        return False
    if not initialize_vector_db():
        return False

    try:
        text_chunks = _chunk_text(content)
        if not text_chunks:
            return False

        embeddings = _embedder.encode(
            text_chunks,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        now = datetime.now(timezone.utc).isoformat()
        message_id = f"{chat_id}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        ids = [f"{message_id}-chunk-{idx}" for idx in range(len(text_chunks))]
        metadatas = [
            {
                "chat_id": str(chat_id),
                "role": str(role),
                "timestamp": now,
                "message_id": message_id,
                "chunk_index": idx,
                "chunk_count": len(text_chunks),
            }
            for idx in range(len(text_chunks))
        ]

        _collection.add(
            ids=ids,
            documents=text_chunks,
            embeddings=embeddings.tolist(),
            metadatas=metadatas,
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Vector store_message failed: %s", exc)
        return False


def retrieve_relevant_memory(chat_id: str, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """Retrieve semantically relevant memory snippets filtered by chat_id."""
    if not str(query or "").strip():
        return []
    if not initialize_vector_db():
        return []

    try:
        query_embedding = _embedder.encode(
            [query],
            normalize_embeddings=True,
            convert_to_numpy=True,
        )[0].tolist()

        # Query extra and then filter by similarity threshold.
        fetch_k = max(top_k * 3, top_k)
        result = _collection.query(
            query_embeddings=[query_embedding],
            n_results=fetch_k,
            where={"chat_id": str(chat_id)},
            include=["documents", "metadatas", "distances"],
        )

        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]

        memories: List[Dict[str, Any]] = []
        for doc, meta, dist in zip(docs, metas, dists):
            distance = float(dist or 0.0)
            similarity = max(0.0, 1.0 - distance)
            if similarity < SIMILARITY_THRESHOLD:
                continue
            memories.append(
                {
                    "content": str(doc or ""),
                    "role": str((meta or {}).get("role", "unknown")),
                    "timestamp": str((meta or {}).get("timestamp", "")),
                    "similarity": similarity,
                }
            )

        memories.sort(key=lambda x: float(x.get("similarity", 0.0)), reverse=True)
        return memories[:top_k]
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Vector retrieve_relevant_memory failed: %s", exc)
        return []


def get_message_count(chat_id: str) -> int:
    """Return stored chunk count for a given chat_id."""
    if not initialize_vector_db():
        return 0
    try:
        result = _collection.get(where={"chat_id": str(chat_id)}, include=["metadatas"])
        metadatas = result.get("metadatas", []) or []
        unique_message_ids = {
            str(meta.get("message_id"))
            for meta in metadatas
            if isinstance(meta, dict) and meta.get("message_id")
        }
        return len(unique_message_ids)
    except Exception:
        return 0
