"""
Database operations for the AI Agent System.
Handles users, conversations, and messages with SQLite.
"""

import sqlite3
import json
import uuid
from datetime import datetime
from contextlib import contextmanager
from typing import List, Dict, Optional, Any
from pathlib import Path

# Database path - use user's home directory for Windows compatibility
DB_DIR = Path.home() / ".ai_agent_system" / "database"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "chatdata.db"
DEFAULT_USERNAME = "GAKR"

# Track if database is initialized
_db_initialized = False


def get_schema_sql() -> str:
    """Return schema SQL directly to avoid file encoding issues."""
    return """
-- Users table for multi-user support
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Conversations table for chat sessions
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT DEFAULT 'New Chat',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Messages table for chat history
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT CHECK(role IN ('user', 'assistant', 'system')) NOT NULL,
    content TEXT NOT NULL,
    files_json TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

-- Streaming state table for resumable generation
CREATE TABLE IF NOT EXISTS streaming_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    message_id INTEGER,  -- The assistant message being streamed
    is_active BOOLEAN DEFAULT 1,
    last_content TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);
CREATE INDEX IF NOT EXISTS idx_streaming_states_conversation ON streaming_states(conversation_id);
CREATE INDEX IF NOT EXISTS idx_streaming_states_active ON streaming_states(is_active);
"""


def _ensure_single_default_user(conn: sqlite3.Connection) -> None:
    """Ensure all conversations belong to one canonical user."""
    cursor = conn.execute(
        "SELECT id FROM users WHERE username = ?",
        (DEFAULT_USERNAME,),
    )
    row = cursor.fetchone()
    if row:
        default_user_id = row[0]
    else:
        cursor = conn.execute(
            "INSERT INTO users (username) VALUES (?)",
            (DEFAULT_USERNAME,),
        )
        default_user_id = cursor.lastrowid

    # Repoint all conversations to the canonical user, then remove extra users.
    conn.execute(
        "UPDATE conversations SET user_id = ? WHERE user_id != ?",
        (default_user_id, default_user_id),
    )
    conn.execute(
        "DELETE FROM users WHERE id != ?",
        (default_user_id,),
    )


def init_database():
    """Initialize database with schema.

    This function creates the SQLite file and applies the schema directly
    without using :func:`get_db_connection` to avoid a recursive loop.
    """
    global _db_initialized
    if _db_initialized:
        return

    try:
        # create parent directory if missing
        DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        try:
            conn.executescript(get_schema_sql())
            _ensure_single_default_user(conn)
            conn.commit()
        finally:
            conn.close()

        _db_initialized = True
        print(f"Database initialized at: {DB_PATH}")
    except Exception as e:
        # log the error and re‑raise so callers see the issue
        print(f"Database initialization error: {e}")
        raise


@contextmanager
def get_db_connection():
    """Get database connection with proper configuration."""
    # Ensure database is initialized
    if not _db_initialized:
        init_database()
    
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# User Operations

def get_or_create_user(username: str) -> int:
    """Get the canonical single user ID (always GAKR)."""
    _ = username  # API compatibility: this app now uses a single fixed user.
    with get_db_connection() as conn:
        cursor = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (DEFAULT_USERNAME,)
        )
        row = cursor.fetchone()
        if row:
            return row['id']
        
        cursor = conn.execute(
            "INSERT INTO users (username) VALUES (?)",
            (DEFAULT_USERNAME,)
        )
        conn.commit()
        return cursor.lastrowid


def get_user(username: str) -> Optional[Dict[str, Any]]:
    """Get user by username."""
    _ = username  # Keep signature stable while enforcing single-user mode.
    with get_db_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (DEFAULT_USERNAME,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


# Conversation Operations

def create_conversation(user_id: int, title: str = "New Chat") -> int:
    """Create new conversation. Returns conversation_id."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO conversations (user_id, title) VALUES (?, ?)",
            (user_id, title)
        )
        conn.commit()
        return cursor.lastrowid


def get_conversations(user_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    """Get all conversations for user, sorted by most recent."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            SELECT id, title, created_at, updated_at,
                   strftime('%Y-%m-%d %H:%M:%S', updated_at) as updated_at_formatted
            FROM conversations 
            WHERE user_id = ? 
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (user_id, limit)
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_conversation(conversation_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """Get single conversation by ID and user."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            SELECT * FROM conversations 
            WHERE id = ? AND user_id = ?
            """,
            (conversation_id, user_id)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def update_conversation_title(conversation_id: int, user_id: int, title: str) -> bool:
    """Update conversation title and timestamp."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE conversations 
            SET title = ?, updated_at = CURRENT_TIMESTAMP 
            WHERE id = ? AND user_id = ?
            """,
            (title, conversation_id, user_id)
        )
        conn.commit()
        return cursor.rowcount > 0


def mark_conversation_updated(conversation_id: int) -> bool:
    """Update conversation's updated_at timestamp."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,)
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_conversation(conversation_id: int, user_id: int) -> bool:
    """Delete conversation and all its related data (messages, streaming states)."""
    with get_db_connection() as conn:
        # First delete streaming states for this conversation
        conn.execute("DELETE FROM streaming_states WHERE conversation_id = ?", (conversation_id,))
        
        # Delete conversation (this will cascade delete messages due to FOREIGN KEY)
        cursor = conn.execute(
            """
            DELETE FROM conversations 
            WHERE id = ? AND user_id = ?
            """,
            (conversation_id, user_id)
        )
        conn.commit()
        return cursor.rowcount > 0


# Message Operations

def add_message(
    conversation_id: int,
    role: str,
    content: str,
    files: Optional[List[Dict]] = None
) -> int:
    """Add message to conversation and update conversation timestamp. Returns message_id."""
    files_json = json.dumps(files) if files else None
    
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (conversation_id, role, content, files_json)
            VALUES (?, ?, ?, ?)
            """,
            (conversation_id, role, content, files_json)
        )
        
        # Update conversation's updated_at timestamp
        conn.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,)
        )
        
        conn.commit()
        return cursor.lastrowid


def get_messages(conversation_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    """Get all messages for conversation."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            SELECT id, conversation_id, role, content, files_json,
                   strftime('%Y-%m-%d %H:%M:%S', timestamp) as timestamp_formatted
            FROM messages 
            WHERE conversation_id = ? 
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (conversation_id, limit)
        )
        rows = cursor.fetchall()
        messages = []
        for row in rows:
            msg = dict(row)
            if msg['files_json']:
                msg['files'] = json.loads(msg['files_json'])
            else:
                msg['files'] = []
            del msg['files_json']
            messages.append(msg)
        return messages


def get_message_by_id(message_id: int) -> Optional[Dict[str, Any]]:
    """Get a single message by ID."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            "SELECT id, conversation_id, role, content FROM messages WHERE id = ?",
            (message_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_recent_messages_for_context(
    conversation_id: int,
    max_messages: int = 20
) -> List[Dict[str, Any]]:
    """Get recent messages for LLM context window."""
    messages = get_messages(conversation_id, limit=max_messages)
    return messages


def append_to_last_assistant_message(conversation_id: int, content: str) -> bool:
    """Append content to the most recent assistant message and update conversation timestamp."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            SELECT id, content FROM messages 
            WHERE conversation_id = ? AND role = 'assistant'
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (conversation_id,)
        )
        row = cursor.fetchone()
        
        if row:
            new_content = row['content'] + content
            conn.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (new_content, row['id'])
            )
            
            # Update conversation's updated_at timestamp
            conn.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (conversation_id,)
            )
            
            conn.commit()
            return True
        return False


def create_empty_assistant_message(conversation_id: int) -> int:
    """Create empty assistant message for streaming."""
    return add_message(conversation_id, 'assistant', '')


# Streaming state management
def start_streaming_state(conversation_id: int, message_id: int) -> int:
    """Start a new streaming state for a conversation."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO streaming_states (conversation_id, message_id) VALUES (?, ?)",
            (conversation_id, message_id)
        )
        conn.commit()
        return cursor.lastrowid


def update_streaming_content(state_id: int, content: str):
    """Update the content of an active streaming state."""
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE streaming_states SET last_content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND is_active = 1",
            (content, state_id)
        )
        conn.commit()


def stop_streaming_state(state_id: int):
    """Stop a streaming state."""
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE streaming_states SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (state_id,)
        )
        conn.commit()


def complete_streaming_state(state_id: int):
    """Mark streaming as completed and clear resumable content."""
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE streaming_states SET is_active = 0, last_content = '', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (state_id,)
        )
        conn.commit()


def resume_streaming_state(state_id: int):
    """Mark an existing streaming state as active again."""
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE streaming_states SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (state_id,)
        )
        conn.commit()


def is_streaming_state_active(state_id: int) -> bool:
    """Return True when the given streaming state is active."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_active FROM streaming_states WHERE id = ?",
            (state_id,)
        )
        row = cursor.fetchone()
        return bool(row and row[0])


def _streaming_state_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a streaming_states row into a dictionary."""
    return {
        'id': row[0],
        'conversation_id': row[1],
        'message_id': row[2],
        'is_active': row[3],
        'last_content': row[4],
        'created_at': row[5],
        'updated_at': row[6]
    }


def get_active_streaming_state(conversation_id: int) -> Optional[Dict[str, Any]]:
    """Get the active streaming state for a conversation."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM streaming_states WHERE conversation_id = ? AND is_active = 1 ORDER BY created_at DESC LIMIT 1",
            (conversation_id,)
        )
        row = cursor.fetchone()
        if row:
            return _streaming_state_from_row(row)
        return None


def get_latest_streaming_state(conversation_id: int) -> Optional[Dict[str, Any]]:
    """Get the latest streaming state for a conversation, active or inactive."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM streaming_states WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
            (conversation_id,)
        )
        row = cursor.fetchone()
        if row:
            return _streaming_state_from_row(row)
        return None


def get_streaming_history(conversation_id: int) -> List[Dict[str, Any]]:
    """Get all streaming states for a conversation."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM streaming_states WHERE conversation_id = ? ORDER BY created_at DESC",
            (conversation_id,)
        )
        return [_streaming_state_from_row(row) for row in cursor.fetchall()]
