"""
Long-term summarized memory storage.
Persists concise per-chat summaries in workspace/long_term_memory.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_MEMORY_BASE = Path(__file__).resolve().parents[3] / "workspace"
LONG_TERM_MEMORY_PATH = str(_MEMORY_BASE / "long_term_memory.json")
SUMMARY_TRIGGER_CHARS = 2000


def _read_store() -> Dict[str, Any]:
    if not os.path.exists(LONG_TERM_MEMORY_PATH):
        return {}
    try:
        with open(LONG_TERM_MEMORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_store(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(LONG_TERM_MEMORY_PATH), exist_ok=True)
    with open(LONG_TERM_MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _fallback_summary(text: str, limit: int = 800) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


def load_long_term_memory(chat_id: str) -> str:
    """Load saved long-term summary for a chat."""
    store = _read_store()
    entry = store.get(str(chat_id), {})
    if isinstance(entry, dict):
        return str(entry.get("summary", "")).strip()
    if isinstance(entry, str):
        return entry.strip()
    return ""


async def update_long_term_memory(
    chat_id: str,
    conversation_chunk: str,
    summary_generator: Optional[Callable[[str, str], Any]] = None,
) -> str:
    """
    Update long-term summary.
    If merged text exceeds threshold, prefer LLM summary generator when provided.
    """
    key = str(chat_id)
    incoming = str(conversation_chunk or "").strip()
    if not incoming:
        return load_long_term_memory(key)

    store = _read_store()
    existing_summary = ""
    existing_entry = store.get(key, {})
    if isinstance(existing_entry, dict):
        existing_summary = str(existing_entry.get("summary", "")).strip()
    elif isinstance(existing_entry, str):
        existing_summary = existing_entry.strip()

    merged_text = f"{existing_summary}\n{incoming}".strip() if existing_summary else incoming

    summary_text = ""
    if len(merged_text) > SUMMARY_TRIGGER_CHARS and summary_generator is not None:
        try:
            generated = await summary_generator(merged_text, existing_summary)
            summary_text = str(generated or "").strip()
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning("LLM long-term summary generation failed: %s", exc)

    if not summary_text:
        summary_text = _fallback_summary(merged_text)

    store[key] = {
        "summary": summary_text,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source_chars": len(merged_text),
    }
    _write_store(store)
    return summary_text
