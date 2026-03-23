"""
Main FastAPI application for the AI Agent System.
Provides SSE streaming, chat management, and file upload endpoints.
"""

import os
import json
import asyncio
import re
import time
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, BackgroundTasks, Request, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging

# Import local modules
from .core.database import (
    get_or_create_user, get_conversations, get_conversation,
    create_conversation, delete_conversation, get_messages,
    add_message, create_empty_assistant_message, append_to_last_assistant_message,
    update_conversation_title, start_streaming_state, update_streaming_content,
    stop_streaming_state, complete_streaming_state, resume_streaming_state, is_streaming_state_active,
    get_active_streaming_state, get_latest_streaming_state, get_streaming_history, get_message_by_id,
    DEFAULT_USERNAME
)
from .core.file_processor import process_uploaded_files, format_files_for_prompt
from .services.react_loop import ReActAgent
from .services.hf_client import close_client, get_model_status, get_client
from ..tools import initialize_tool_runtime, shutdown_tool_runtime, execute_tool_async, list_tools
from ..tools.mcp_bridge import get_mcp_bridge


def _configure_backend_logging() -> None:
    """
    Configure a dedicated logger tree for backend modules.
    This keeps backend logs visible even when Uvicorn applies its own log config.
    """
    level_name = os.getenv("BACKEND_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    backend_logger = logging.getLogger("backend.app")
    backend_logger.setLevel(level)

    handler_name = "backend-app-console"
    has_handler = any(
        isinstance(handler, logging.StreamHandler) and getattr(handler, "name", "") == handler_name
        for handler in backend_logger.handlers
    )
    if not has_handler:
        handler = logging.StreamHandler()
        handler.set_name(handler_name)
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | [%(filename)s:%(lineno)d] %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        backend_logger.addHandler(handler)

    # Prevent duplicate entries from propagating to root handlers.
    backend_logger.propagate = False

    # Keep Uvicorn access/error logs visible even if external config muted them.
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)


_configure_backend_logging()
logger = logging.getLogger(__name__)
SMTP_FIXED_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_FIXED_PORT = int(os.getenv("SMTP_PORT", "587"))
STREAM_LOG_PREVIEW_STEP = max(40, int(os.getenv("BACKEND_STREAM_LOG_PREVIEW_STEP", "120")))
STREAM_LOG_PREVIEW_CHARS = max(60, int(os.getenv("BACKEND_STREAM_LOG_PREVIEW_CHARS", "220")))
WORKSPACE_ROOT = Path(__file__).resolve().parents[2] / "workspace"
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


def _resolve_workspace_path(path: str) -> Path:
    """Resolve a user path inside the workspace root."""
    normalized = str(path or "").replace("\\", "/").strip().lstrip("/")
    if not normalized or normalized == ".":
        return WORKSPACE_ROOT

    resolved = (WORKSPACE_ROOT / normalized).resolve()
    try:
        resolved.relative_to(WORKSPACE_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("Path is outside workspace") from exc
    return resolved


def _sanitize_upload_path(filename: str) -> str:
    """Sanitize uploaded file path while preserving folder structure."""
    raw = str(filename or "").replace("\\", "/").strip().lstrip("/")
    if not raw:
        raise ValueError("Invalid filename")

    safe_parts: List[str] = []
    for part in raw.split("/"):
        cleaned = part.strip()
        if not cleaned or cleaned in {".", ".."}:
            continue
        safe_parts.append(cleaned)

    if not safe_parts:
        raise ValueError("Invalid filename")
    return "/".join(safe_parts)


def _workspace_relative(path: Path) -> str:
    """Return workspace-relative POSIX path."""
    return path.relative_to(WORKSPACE_ROOT).as_posix()

# Pydantic models

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[int] = None
    username: str = DEFAULT_USERNAME


class ChatResponse(BaseModel):
    conversation_id: int
    message_id: int


class ConversationCreate(BaseModel):
    username: Optional[str] = DEFAULT_USERNAME
    title: Optional[str] = "New Chat"


class ConversationRename(BaseModel):
    username: Optional[str] = DEFAULT_USERNAME
    title: str


class SettingsUpdate(BaseModel):
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    hf_space_url: Optional[str] = None
    custom_instructions: Optional[str] = None


# In-memory settings store (replace with database for persistence)
_settings = {
    "smtp_host": SMTP_FIXED_HOST,
    "smtp_port": SMTP_FIXED_PORT,
    "smtp_username": os.getenv("SMTP_USERNAME", ""),
    "smtp_password": os.getenv("SMTP_PASSWORD", ""),
    "hf_space_url": os.getenv("HF_SPACE_URL", "https://your-text-worker.your-subdomain.workers.dev"),
    "custom_instructions": os.getenv("CUSTOM_INSTRUCTIONS", ""),
}

os.environ["CUSTOM_INSTRUCTIONS"] = _settings["custom_instructions"]


# Lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    try:
        await initialize_tool_runtime()
        trace_log("startup", "MCP tool runtime initialized")
    except Exception as exc:
        trace_log("startup", "MCP tool runtime initialization failed: %s", str(exc))
    trace_log("startup", "setup complete")
    trace_log("startup", "HF Space URL: %s", _settings["hf_space_url"])
    trace_log(
        "startup",
        "HTTP logging active (backend=%s, uvicorn.access=%s)",
        logging.getLevelName(logging.getLogger("backend.app").getEffectiveLevel()),
        logging.getLevelName(logging.getLogger("uvicorn.access").getEffectiveLevel()),
    )
    yield
    # Shutdown
    trace_log("shutdown", "shutting down AI Agent System")
    await shutdown_tool_runtime()
    await close_client()


# Create FastAPI app
app = FastAPI(
    title="AI Agent System",
    description="Production-grade AI agent with streaming, tools, and persistent chat",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Conversation-Id"],
)


@app.middleware("http")
async def log_http_requests(request: Request, call_next):
    """Log every backend request/response with latency."""
    start_ts = time.perf_counter()
    method = request.method
    path = request.url.path
    client = request.client.host if request.client else "-"

    trace_log("http", "request received method=%s path=%s client=%s", method, path, client)
    try:
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start_ts) * 1000
        trace_log(
            "http",
            "request completed method=%s path=%s status=%d duration_ms=%.1f",
            method,
            path,
            response.status_code,
            elapsed_ms,
        )
        return response
    except Exception:
        elapsed_ms = (time.perf_counter() - start_ts) * 1000
        trace_log("http", "request failed method=%s path=%s duration_ms=%.1f", method, path, elapsed_ms)
        logger.exception("Unhandled exception while processing HTTP request")
        raise


# SSE Event formatting
def format_sse_event(event_type: str, data: Any) -> str:
    """Format data as SSE event."""
    # Always send JSON on a single SSE data line so spaces/newlines in tokens
    # are preserved during streaming.
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


def preview_text(value: Any, max_len: int = STREAM_LOG_PREVIEW_CHARS) -> str:
    """Return a one-line preview for logging."""
    text = str(value).replace("\n", "\\n")
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def trace_log(scope: str, message: str, *args: Any) -> None:
    """
    Verbose trace logger with file marker for quick debugging.
    Example: [main.py::chat_stream] query received ...
    """
    if args:
        try:
            rendered = message % args
        except Exception:
            rendered = f"{message} | args={args!r}"
    else:
        rendered = message
    line = f"[main.py::{scope}] {rendered}"
    print(line, flush=True)


def get_next_chat_title(user_id: int) -> str:
    """Return the next default title in the form Chat N."""
    conversations = get_conversations(user_id)
    max_index = 0
    for conv in conversations:
        title = str(conv.get("title", "")).strip()
        match = re.match(r"^chat\s+(\d+)$", title, re.IGNORECASE)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return f"Chat {max_index + 1}"


# Routes

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "AI Agent System",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/api/model-status")
async def model_status():
    """Get model status from HF Space."""
    try:
        status = await get_model_status()
        trace_log("model_status", "model status loaded=%s", status)
        return {"status": status}
    except Exception as e:
        logger.error(f"Error getting model status: {e}")
        trace_log("model_status", "model status error=%s", str(e))
        return {"status": "error", "error": str(e)}


# Settings endpoints
@app.get("/api/settings")
async def get_settings():
    """Get current settings (excluding sensitive data)."""
    return {
        "smtp_host": _settings["smtp_host"],
        "smtp_port": _settings["smtp_port"],
        "smtp_username": _settings["smtp_username"],
        "smtp_password_set": bool(_settings["smtp_password"]),
        "hf_space_url": _settings["hf_space_url"],
        "custom_instructions": _settings["custom_instructions"],
    }


@app.post("/api/settings")
async def update_settings(settings: SettingsUpdate):
    """Update settings."""
    global _settings

    # SMTP host and port are fixed and not user-editable.
    _settings["smtp_host"] = SMTP_FIXED_HOST
    _settings["smtp_port"] = SMTP_FIXED_PORT
    os.environ["SMTP_HOST"] = SMTP_FIXED_HOST
    os.environ["SMTP_PORT"] = str(SMTP_FIXED_PORT)
    
    if settings.smtp_username is not None:
        _settings["smtp_username"] = settings.smtp_username
        os.environ["SMTP_USERNAME"] = settings.smtp_username
    
    if settings.smtp_password is not None:
        _settings["smtp_password"] = settings.smtp_password
        os.environ["SMTP_PASSWORD"] = settings.smtp_password
    
    if settings.hf_space_url is not None:
        _settings["hf_space_url"] = settings.hf_space_url
        os.environ["HF_SPACE_URL"] = settings.hf_space_url
        # Reset the client so it will be recreated with the new URL on next use
        await close_client()
    
    if settings.custom_instructions is not None:
        _settings["custom_instructions"] = settings.custom_instructions
        os.environ["CUSTOM_INSTRUCTIONS"] = settings.custom_instructions
    
    return {"success": True}


# Chat streaming endpoint
@app.post("/api/chat/stream")
async def chat_stream(
    request: Request,
    message: str = Form(...),
    conversation_id: Optional[int] = Form(None),
    username: str = Form(DEFAULT_USERNAME),
    files: List[UploadFile] = File(default=[])
):
    """
    Stream chat response with SSE.
    Supports file uploads and conversation continuity.
    """
    # Get or create user
    _ = username  # Single-user mode: all requests map to GAKR
    user_id = get_or_create_user(DEFAULT_USERNAME)
    trace_log(
        "chat_stream",
        "query received username=%s conversation_id=%s message_preview=%s files=%d",
        username,
        str(conversation_id) if conversation_id is not None else "new",
        preview_text(message, 180),
        len(files),
    )
    
    # Create conversation if needed
    if conversation_id is None:
        conversation_id = create_conversation(user_id, title=get_next_chat_title(user_id))
    else:
        # Verify conversation belongs to user
        conv = get_conversation(conversation_id, user_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Process uploaded files
    processed_files = []
    if files:
        file_data = []
        for file in files:
            if file.filename:
                content = await file.read()
                file_data.append((content, file.filename))
        
        if file_data:
            processed_files = process_uploaded_files(file_data)
    
    # Build user message with file content
    full_message = message
    if processed_files:
        files_prompt = format_files_for_prompt([
            type('obj', (object,), {
                'filename': f['filename'],
                'mime_type': f['mime_type'],
                'size': f['size'],
                'content': f['content'],
                'success': f['success']
            }) for f in processed_files
        ])
        full_message = f"{files_prompt}\n\nUser message: {message}"
    
    # Save user message
    add_message(conversation_id, "user", message, processed_files)
    trace_log("chat_stream", "query stored conversation_id=%d", conversation_id)
    
    # Create empty assistant message for streaming
    assistant_msg_id = create_empty_assistant_message(conversation_id)
    
    # Start streaming state
    streaming_state_id = start_streaming_state(conversation_id, assistant_msg_id)
    trace_log(
        "chat_stream",
        "stream setup complete conversation_id=%d streaming_state_id=%d",
        conversation_id,
        streaming_state_id,
    )
    
    # Get conversation history
    history = get_messages(conversation_id, limit=20)
    messages = []
    for msg in history[:-1]:  # Exclude the empty assistant message we just created
        messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })
    # Ensure current-turn uploaded file content is included in model context.
    if processed_files:
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = full_message
        else:
            messages.append({"role": "user", "content": full_message})
        trace_log(
            "chat_stream",
            "attached files injected into prompt files=%d prompt_chars=%d",
            len(processed_files),
            len(full_message),
        )
    
    async def event_generator():
        """Generate SSE events with fast token-by-token streaming."""
        agent = ReActAgent()
        accumulated_response = ""
        accumulated_thinking = ""
        last_db_update = ""
        stopped_by_user = False
        next_preview_chars = STREAM_LOG_PREVIEW_STEP
        next_thinking_preview_chars = STREAM_LOG_PREVIEW_STEP
        first_preview_logged = False
        first_thinking_logged = False
        done_emitted = False
        
        try:
            # Send conversation ID as an explicit SSE event for clients that cannot read custom headers.
            yield format_sse_event("conversation", {"conversation_id": conversation_id})
            trace_log(
                "chat_stream",
                "stream started conversation_id=%d state=%d history_messages=%d",
                conversation_id,
                streaming_state_id,
                len(messages),
            )

            async for event in agent.run(
                messages,
                should_stop=lambda: not is_streaming_state_active(streaming_state_id),
                chat_id=conversation_id,
            ):
                # Handle client/network disconnects as resumable stops.
                if await request.is_disconnected():
                    stopped_by_user = True
                    stop_streaming_state(streaming_state_id)
                    break

                # Stop immediately when /api/chat/stop deactivates this state.
                if not is_streaming_state_active(streaming_state_id):
                    stopped_by_user = True
                    break

                event_type = event.get("type", "token")
                content = event.get("content", "")
                if event_type == "error":
                    trace_log(
                        "chat_stream",
                        "stream event error conversation=%d state=%d message=%s",
                        conversation_id,
                        streaming_state_id,
                        preview_text(content, 240),
                    )
                
                # Batch database updates (every 200 chars) to avoid I/O blocking
                if event_type == "token":
                    accumulated_response += content
                    # Update streaming state
                    update_streaming_content(streaming_state_id, accumulated_response)
                    if not first_preview_logged:
                        trace_log(
                            "chat_stream",
                            "first stream token conversation=%d state=%d preview=%s",
                            conversation_id,
                            streaming_state_id,
                            preview_text(content),
                        )
                        first_preview_logged = True
                    if len(accumulated_response) >= next_preview_chars:
                        trace_log(
                            "chat_stream",
                            "stream preview conversation=%d state=%d chars=%d preview=%s",
                            conversation_id,
                            streaming_state_id,
                            len(accumulated_response),
                            preview_text(accumulated_response),
                        )
                        next_preview_chars += STREAM_LOG_PREVIEW_STEP
                    # Only update DB every 200 chars, not every 50
                    if len(accumulated_response) - len(last_db_update) >= 200:
                        new_content = accumulated_response[len(last_db_update):]
                        append_to_last_assistant_message(conversation_id, new_content)
                        last_db_update = accumulated_response
                elif event_type == "thinking":
                    accumulated_thinking += content
                    if not first_thinking_logged:
                        trace_log(
                            "chat_stream",
                            "first thinking token conversation=%d state=%d preview=%s",
                            conversation_id,
                            streaming_state_id,
                            preview_text(content),
                        )
                        first_thinking_logged = True
                    if len(accumulated_thinking) >= next_thinking_preview_chars:
                        trace_log(
                            "chat_stream",
                            "thinking preview conversation=%d state=%d chars=%d preview=%s",
                            conversation_id,
                            streaming_state_id,
                            len(accumulated_thinking),
                            preview_text(accumulated_thinking),
                        )
                        next_thinking_preview_chars += STREAM_LOG_PREVIEW_STEP
                
                # Yield SSE event immediately (no buffering)
                yield format_sse_event(event_type, content)
                if event_type == "done":
                    done_emitted = True
                    break
            
            # Final database update with remaining content
            if accumulated_response and len(accumulated_response) > len(last_db_update):
                final_content = accumulated_response[len(last_db_update):]
                append_to_last_assistant_message(conversation_id, final_content)
            
            # Stop streaming state
            if not stopped_by_user:
                complete_streaming_state(streaming_state_id)
                trace_log(
                    "chat_stream",
                    "stream complete conversation=%d state=%d total_chars=%d preview=%s",
                    conversation_id,
                    streaming_state_id,
                    len(accumulated_response),
                    preview_text(accumulated_response),
                )
            else:
                trace_log(
                    "chat_stream",
                    "stream stopped conversation=%d state=%d total_chars=%d",
                    conversation_id,
                    streaming_state_id,
                    len(accumulated_response),
                )
        
        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            trace_log("chat_stream", "stream exception conversation=%d error=%s", conversation_id, str(e))
            yield format_sse_event("error", str(e))
            # Stop streaming state on error
            stop_streaming_state(streaming_state_id)
        
        finally:
            trace_log("chat_stream", "stream finished conversation=%d state=%d", conversation_id, streaming_state_id)
            if not done_emitted:
                yield format_sse_event("done", "")
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
            "X-Conversation-Id": str(conversation_id)
        }
    )


# Check streaming state endpoint
@app.get("/api/chat/streaming-state")
async def check_streaming_state(
    conversation_id: int,
    username: str = DEFAULT_USERNAME
):
    """Check if conversation has active or continuable streaming state."""
    _ = username
    user_id = get_or_create_user(DEFAULT_USERNAME)
    
    # Verify conversation belongs to user
    conv = get_conversation(conversation_id, user_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    latest_state = get_latest_streaming_state(conversation_id)
    has_active_streaming = bool(latest_state and latest_state["is_active"])
    can_continue = False
    if latest_state and not latest_state["is_active"]:
        saved_content = str(latest_state.get("last_content", ""))
        if saved_content.strip():
            can_continue = True
        else:
            message_id = latest_state.get("message_id")
            if message_id:
                message = get_message_by_id(int(message_id))
                can_continue = bool(
                    message
                    and message.get("role") == "assistant"
                    and not str(message.get("content", "")).strip()
                )

    return {
        "hasActiveStreaming": has_active_streaming,
        "canContinue": can_continue
    }


# Stop streaming endpoint
@app.post("/api/chat/stop")
async def stop_streaming(
    conversation_id: int = Form(...),
    username: str = Form(DEFAULT_USERNAME)
):
    """Stop active streaming for a conversation."""
    _ = username
    user_id = get_or_create_user(DEFAULT_USERNAME)
    
    # Verify conversation belongs to user
    conv = get_conversation(conversation_id, user_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Get active streaming state
    active_state = get_active_streaming_state(conversation_id)
    if active_state:
        stop_streaming_state(active_state['id'])
        return {"success": True, "message": "Streaming stopped"}

    # If already stopped but resumable, treat as successful stop.
    latest_state = get_latest_streaming_state(conversation_id)
    if latest_state and not latest_state["is_active"] and str(latest_state.get("last_content", "")).strip():
        return {"success": True, "message": "Streaming already stopped"}

    return {"success": False, "message": "No active streaming found"}


# Continue streaming endpoint
@app.post("/api/chat/continue")
async def continue_streaming(
    request: Request,
    conversation_id: int = Form(...),
    username: str = Form(DEFAULT_USERNAME)
):
    """Continue streaming from last saved state."""
    _ = username
    user_id = get_or_create_user(DEFAULT_USERNAME)
    trace_log("continue_stream", "continue request received conversation_id=%d username=%s", conversation_id, username)
    
    # Verify conversation belongs to user
    conv = get_conversation(conversation_id, user_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Resume from the latest stopped state for this conversation.
    latest_state = get_latest_streaming_state(conversation_id)
    if not latest_state:
        raise HTTPException(status_code=400, detail="No streaming state found to continue")

    if latest_state["is_active"]:
        raise HTTPException(status_code=400, detail="Streaming is already active")

    initial_content = str(latest_state.get("last_content", ""))
    if not initial_content.strip():
        message_id = latest_state.get("message_id")
        message = get_message_by_id(int(message_id)) if message_id else None
        can_resume_empty = bool(
            message
            and message.get("role") == "assistant"
            and not str(message.get("content", "")).strip()
        )
        if not can_resume_empty:
            raise HTTPException(status_code=400, detail="No saved content to continue from")

    resume_streaming_state(latest_state["id"])
    trace_log(
        "continue_stream",
        "continue setup complete conversation_id=%d state_id=%d initial_chars=%d",
        conversation_id,
        latest_state["id"],
        len(initial_content),
    )
    
    # Get conversation history
    history = get_messages(conversation_id, limit=20)
    messages = []
    for msg in history:
        messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })
    
    # Add continuation prompt
    continuation_prompt = (
        f"\n\n[Continuing from previous response: {initial_content[-200:]}...]\n\n"
        "Please continue the response from where it left off."
        if initial_content.strip()
        else "\n\nThe previous response was interrupted before output. Continue answering the user's last request."
    )
    messages.append({
        "role": "user",
        "content": continuation_prompt
    })
    
    async def event_generator():
        """Generate SSE events for continuation."""
        agent = ReActAgent()
        accumulated_response = initial_content
        accumulated_thinking = ""
        last_flushed_len = 0
        stopped_by_user = False
        next_preview_chars = STREAM_LOG_PREVIEW_STEP
        next_thinking_preview_chars = STREAM_LOG_PREVIEW_STEP
        first_preview_logged = False
        first_thinking_logged = False
        done_emitted = False
        
        try:
            trace_log(
                "continue_stream",
                "continue started conversation=%d state=%d history_messages=%d",
                conversation_id,
                latest_state["id"],
                len(messages),
            )
            async for event in agent.run(
                messages,
                should_stop=lambda: not is_streaming_state_active(latest_state["id"]),
                chat_id=conversation_id,
            ):
                # Handle client/network disconnects as resumable stops.
                if await request.is_disconnected():
                    stopped_by_user = True
                    stop_streaming_state(latest_state["id"])
                    break

                # Stop immediately when /api/chat/stop deactivates this state.
                if not is_streaming_state_active(latest_state["id"]):
                    stopped_by_user = True
                    break

                event_type = event.get("type", "token")
                content = event.get("content", "")
                if event_type == "error":
                    trace_log(
                        "continue_stream",
                        "continue event error conversation=%d state=%d message=%s",
                        conversation_id,
                        latest_state["id"],
                        preview_text(content, 240),
                    )
                
                if event_type == "token":
                    accumulated_response += content
                    # Update streaming state
                    update_streaming_content(latest_state["id"], accumulated_response)
                    if not first_preview_logged:
                        trace_log(
                            "continue_stream",
                            "first continue token conversation=%d state=%d preview=%s",
                            conversation_id,
                            latest_state["id"],
                            preview_text(content),
                        )
                        first_preview_logged = True
                    continuation_delta = accumulated_response[len(initial_content):]
                    if len(continuation_delta) >= next_preview_chars:
                        trace_log(
                            "continue_stream",
                            "continue preview conversation=%d state=%d chars=%d preview=%s",
                            conversation_id,
                            latest_state["id"],
                            len(continuation_delta),
                            preview_text(continuation_delta),
                        )
                        next_preview_chars += STREAM_LOG_PREVIEW_STEP

                    # Persist continuation chunks in batches.
                    if len(continuation_delta) - last_flushed_len >= 200:
                        chunk = continuation_delta[last_flushed_len:]
                        append_to_last_assistant_message(conversation_id, chunk)
                        last_flushed_len = len(continuation_delta)
                elif event_type == "thinking":
                    accumulated_thinking += content
                    if not first_thinking_logged:
                        trace_log(
                            "continue_stream",
                            "first continue thinking token conversation=%d state=%d preview=%s",
                            conversation_id,
                            latest_state["id"],
                            preview_text(content),
                        )
                        first_thinking_logged = True
                    if len(accumulated_thinking) >= next_thinking_preview_chars:
                        trace_log(
                            "continue_stream",
                            "continue thinking preview conversation=%d state=%d chars=%d preview=%s",
                            conversation_id,
                            latest_state["id"],
                            len(accumulated_thinking),
                            preview_text(accumulated_thinking),
                        )
                        next_thinking_preview_chars += STREAM_LOG_PREVIEW_STEP
                
                yield format_sse_event(event_type, content)
                if event_type == "done":
                    done_emitted = True
                    break
            
            # Flush any remaining continuation text.
            continuation_delta = accumulated_response[len(initial_content):]
            if len(continuation_delta) > last_flushed_len:
                chunk = continuation_delta[last_flushed_len:]
                append_to_last_assistant_message(conversation_id, chunk)

            if not stopped_by_user:
                complete_streaming_state(latest_state["id"])
                continuation_delta = accumulated_response[len(initial_content):]
                trace_log(
                    "continue_stream",
                    "continue complete conversation=%d state=%d total_chars=%d preview=%s",
                    conversation_id,
                    latest_state["id"],
                    len(continuation_delta),
                    preview_text(continuation_delta),
                )
            else:
                continuation_delta = accumulated_response[len(initial_content):]
                trace_log(
                    "continue_stream",
                    "continue stopped conversation=%d state=%d total_chars=%d",
                    conversation_id,
                    latest_state["id"],
                    len(continuation_delta),
                )
        
        except Exception as e:
            logger.error(f"Continuation streaming error: {e}", exc_info=True)
            trace_log("continue_stream", "continue exception conversation=%d error=%s", conversation_id, str(e))
            yield format_sse_event("error", str(e))
            stop_streaming_state(latest_state["id"])
        
        finally:
            trace_log("continue_stream", "continue finished conversation=%d state=%d", conversation_id, latest_state["id"])
            if not done_emitted:
                yield format_sse_event("done", "")
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
            "X-Conversation-Id": str(conversation_id)
        }
    )


# Non-streaming chat endpoint (for testing)
@app.post("/api/chat")
async def chat(
    request: ChatRequest,
    background_tasks: BackgroundTasks
):
    """Non-streaming chat endpoint."""
    _ = request.username
    user_id = get_or_create_user(DEFAULT_USERNAME)
    
    if request.conversation_id is None:
        conversation_id = create_conversation(user_id, title=get_next_chat_title(user_id))
    else:
        conversation_id = request.conversation_id
        conv = get_conversation(conversation_id, user_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Save user message
    add_message(conversation_id, "user", request.message)
    
    # Get history
    history = get_messages(conversation_id, limit=20)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    
    # Run agent
    agent = ReActAgent()
    response_parts = []
    
    async def collect_response():
        async for event in agent.run(messages, chat_id=conversation_id):
            if event.get("type") == "token":
                response_parts.append(event.get("content", ""))
            elif event.get("type") == "done":
                break
    
    await collect_response()
    
    response = "".join(response_parts)
    add_message(conversation_id, "assistant", response)
    
    return {
        "conversation_id": conversation_id,
        "response": response
    }


# Conversation management endpoints

@app.get("/api/conversations/{username}")
async def list_user_conversations(username: str):
    """Get all conversations for a user."""
    _ = username
    user_id = get_or_create_user(DEFAULT_USERNAME)
    conversations = get_conversations(user_id)
    return {"conversations": conversations}


@app.post("/api/conversations")
async def create_new_conversation(request: ConversationCreate):
    """Create a new conversation."""
    _ = request.username
    user_id = get_or_create_user(DEFAULT_USERNAME)
    requested_title = (request.title or "").strip()
    title = requested_title if requested_title and requested_title.lower() != "new chat" else get_next_chat_title(user_id)
    conversation_id = create_conversation(user_id, title)
    return {
        "conversation_id": conversation_id,
        "title": title
    }


@app.get("/api/conversations/{username}/{conversation_id}")
async def get_conversation_messages(username: str, conversation_id: int):
    """Get messages for a conversation."""
    _ = username
    user_id = get_or_create_user(DEFAULT_USERNAME)
    conv = get_conversation(conversation_id, user_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    messages = get_messages(conversation_id)
    return {
        "conversation": conv,
        "messages": messages
    }


@app.put("/api/conversations/{username}/{conversation_id}")
async def rename_conversation(
    username: str,
    conversation_id: int,
    request: ConversationRename
):
    """Rename a conversation."""
    _ = username
    _ = request.username
    user_id = get_or_create_user(DEFAULT_USERNAME)
    success = update_conversation_title(conversation_id, user_id, request.title)
    
    if not success:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    return {"success": True}


@app.delete("/api/conversations/{username}/{conversation_id}")
async def remove_conversation(username: str, conversation_id: int):
    """Delete a conversation."""
    _ = username
    user_id = get_or_create_user(DEFAULT_USERNAME)
    success = delete_conversation(conversation_id, user_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    return {"success": True}


# File upload endpoint
@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """Upload and process files."""
    if len(files) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 files allowed")
    
    file_data = []
    for file in files:
        if file.filename:
            content = await file.read()
            file_data.append((content, file.filename))
    
    processed = process_uploaded_files(file_data)
    
    return {
        "files": processed,
        "success": all(f["success"] for f in processed)
    }


# Tool execution endpoint (for testing)
@app.get("/api/tools")
async def list_active_tools():
    """Return the active model-visible tool list and MCP runtime status."""
    await initialize_tool_runtime()
    bridge = get_mcp_bridge()
    tools = list_tools()
    tool_names = {
        str(tool.get("function", {}).get("name", "")).strip()
        for tool in tools
        if isinstance(tool, dict)
    }
    return {
        "count": len(tools),
        "tools": [tool.get("function", {}) for tool in tools],
        "mcp": {
            "bridge_connected": bridge.has_server("unified-bridge-server"),
            "fetch": any(name in tool_names for name in {"fetch_html", "fetch_markdown", "fetch_txt", "fetch_json", "fetch_url", "fetch_readable", "fetch_youtube_transcript", "web_search"}),
            "filesystem": any(name in tool_names for name in {"read_text_file", "write_file", "edit_file", "list_directory", "directory_tree", "search_files"}),
            "shell": "run_command" in tool_names,
            "errors": bridge.get_server_errors(),
        },
    }


@app.post("/api/tools/{tool_name}")
async def execute_tool_endpoint(tool_name: str, arguments: Dict[str, Any]):
    """Execute a tool directly (for testing)."""
    result = await execute_tool_async(tool_name, arguments)
    return result


# Workspace management endpoints
@app.get("/api/workspace/list")
async def list_workspace():
    """List all files and directories in the workspace."""
    try:
        entries: List[Dict[str, Any]] = []
        for item in WORKSPACE_ROOT.rglob("*"):
            if item.is_symlink():
                continue

            relative_path = item.relative_to(WORKSPACE_ROOT).as_posix()
            entries.append(
                {
                    "path": relative_path,
                    "name": item.name,
                    "is_directory": item.is_dir(),
                    "size": 0 if item.is_dir() else item.stat().st_size,
                }
            )

        entries.sort(key=lambda e: (e["path"].count("/"), e["is_directory"] is False, e["path"].lower()))
        return {"files": entries}
    except Exception as e:
        logger.error(f"Error listing workspace: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list workspace: {str(e)}")


@app.post("/api/workspace/create-file")
async def create_workspace_file(request: Dict[str, str]):
    """Create a new file in the workspace."""
    try:
        path = str(request.get("path", "")).strip()
        content = request.get("content", "")
        if not path:
            raise HTTPException(status_code=400, detail="Path is required")

        filepath = _resolve_workspace_path(path)
        if filepath.exists() and filepath.is_dir():
            raise HTTPException(status_code=400, detail="Path points to a directory")

        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(str(content), encoding="utf-8")
        return {"success": True, "message": f"File created successfully: {path}"}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create file: {str(e)}")


@app.post("/api/workspace/create-folder")
async def create_workspace_folder(request: Dict[str, str]):
    """Create a new folder in the workspace."""
    try:
        path = str(request.get("path", "")).strip()

        if not path:
            raise HTTPException(status_code=400, detail="Path is required")

        folder_path = _resolve_workspace_path(path)
        folder_path.mkdir(parents=True, exist_ok=True)
        return {"success": True, "message": f"Directory created successfully: {path}"}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating folder: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create folder: {str(e)}")


@app.post("/api/workspace/upload")
async def upload_workspace_files(files: List[UploadFile] = File(...)):
    """Upload files/folder contents directly into the workspace."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    if len(files) > 200:
        raise HTTPException(status_code=400, detail="Maximum 200 files allowed per upload")

    uploaded: List[Dict[str, Any]] = []
    for file in files:
        if not file.filename:
            continue

        try:
            safe_relative_path = _sanitize_upload_path(file.filename)
            target_path = _resolve_workspace_path(safe_relative_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        content = await file.read()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(content)

        uploaded.append(
            {
                "path": target_path.relative_to(WORKSPACE_ROOT).as_posix(),
                "name": target_path.name,
                "size": len(content),
            }
        )

    if not uploaded:
        raise HTTPException(status_code=400, detail="No valid files uploaded")

    return {"success": True, "files": uploaded}


@app.get("/api/workspace/read-file")
async def read_workspace_file(path: str = Query(..., description="Workspace-relative file path")):
    """Read a text file from the workspace."""
    try:
        filepath = _resolve_workspace_path(path)
        if not filepath.exists():
            raise HTTPException(status_code=404, detail="File not found")
        if not filepath.is_file():
            raise HTTPException(status_code=400, detail="Path points to a directory")

        content = filepath.read_text(encoding="utf-8", errors="ignore")
        return {
            "success": True,
            "path": filepath.relative_to(WORKSPACE_ROOT).as_posix(),
            "name": filepath.name,
            "size": filepath.stat().st_size,
            "content": content,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading workspace file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to read file: {str(e)}")


@app.post("/api/workspace/save-file")
async def save_workspace_file(request: Dict[str, str]):
    """Save edited text content into a workspace file."""
    try:
        path = str(request.get("path", "")).strip()
        content = str(request.get("content", ""))
        if not path:
            raise HTTPException(status_code=400, detail="Path is required")

        filepath = _resolve_workspace_path(path)
        if filepath.exists() and filepath.is_dir():
            raise HTTPException(status_code=400, detail="Path points to a directory")

        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")

        return {
            "success": True,
            "path": filepath.relative_to(WORKSPACE_ROOT).as_posix(),
            "size": filepath.stat().st_size,
            "message": f"File saved successfully: {path}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving workspace file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")


@app.post("/api/workspace/delete")
async def delete_workspace_path(request: Dict[str, str]):
    """Delete a file or folder from the workspace."""
    try:
        path = str(request.get("path", "")).strip()
        if not path:
            raise HTTPException(status_code=400, detail="Path is required")

        target = _resolve_workspace_path(path)
        if target.resolve() == WORKSPACE_ROOT.resolve():
            raise HTTPException(status_code=400, detail="Cannot delete workspace root")
        if not target.exists():
            raise HTTPException(status_code=404, detail="Path not found")

        deleted_type = "directory" if target.is_dir() else "file"
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

        return {
            "success": True,
            "deleted": path,
            "type": deleted_type,
            "message": f"Deleted {deleted_type}: {path}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting workspace path: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete path: {str(e)}")


@app.post("/api/workspace/rename")
async def rename_workspace_path(request: Dict[str, str]):
    """Rename/move a file or folder inside workspace."""
    try:
        path = str(request.get("path", "")).strip()
        new_path = str(request.get("new_path", "")).strip()
        if not path or not new_path:
            raise HTTPException(status_code=400, detail="Both path and new_path are required")

        source = _resolve_workspace_path(path)
        destination = _resolve_workspace_path(new_path)

        if source.resolve() == WORKSPACE_ROOT.resolve():
            raise HTTPException(status_code=400, detail="Cannot rename workspace root")
        if not source.exists():
            raise HTTPException(status_code=404, detail="Source path not found")
        if destination.exists():
            raise HTTPException(status_code=400, detail="Destination already exists")

        if source.is_dir():
            src_resolved = source.resolve()
            dst_resolved = destination.resolve()
            if src_resolved in dst_resolved.parents:
                raise HTTPException(status_code=400, detail="Cannot move a folder into itself")

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

        return {
            "success": True,
            "path": _workspace_relative(destination),
            "message": f"Renamed {path} to {new_path}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error renaming workspace path: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to rename path: {str(e)}")


@app.post("/api/workspace/copy")
async def copy_workspace_path(request: Dict[str, str]):
    """Copy a file or folder inside workspace."""
    try:
        path = str(request.get("path", "")).strip()
        destination_path = str(request.get("destination_path", "")).strip()
        if not path or not destination_path:
            raise HTTPException(
                status_code=400,
                detail="Both path and destination_path are required",
            )

        source = _resolve_workspace_path(path)
        destination = _resolve_workspace_path(destination_path)
        if not source.exists():
            raise HTTPException(status_code=404, detail="Source path not found")
        if destination.exists():
            raise HTTPException(status_code=400, detail="Destination already exists")

        if source.is_dir():
            src_resolved = source.resolve()
            dst_resolved = destination.resolve()
            if src_resolved in dst_resolved.parents:
                raise HTTPException(status_code=400, detail="Cannot copy folder into itself")
            shutil.copytree(source, destination)
            copied_type = "directory"
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_type = "file"

        return {
            "success": True,
            "path": _workspace_relative(destination),
            "type": copied_type,
            "message": f"Copied {path} to {destination_path}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error copying workspace path: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to copy path: {str(e)}")


@app.post("/api/workspace/move")
async def move_workspace_path(request: Dict[str, str]):
    """Move a file or folder inside workspace."""
    try:
        path = str(request.get("path", "")).strip()
        destination_path = str(request.get("destination_path", "")).strip()
        if not path or not destination_path:
            raise HTTPException(
                status_code=400,
                detail="Both path and destination_path are required",
            )

        source = _resolve_workspace_path(path)
        destination = _resolve_workspace_path(destination_path)
        if source.resolve() == WORKSPACE_ROOT.resolve():
            raise HTTPException(status_code=400, detail="Cannot move workspace root")
        if not source.exists():
            raise HTTPException(status_code=404, detail="Source path not found")
        if destination.exists():
            raise HTTPException(status_code=400, detail="Destination already exists")

        if source.is_dir():
            src_resolved = source.resolve()
            dst_resolved = destination.resolve()
            if src_resolved in dst_resolved.parents:
                raise HTTPException(status_code=400, detail="Cannot move a folder into itself")

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

        return {
            "success": True,
            "path": _workspace_relative(destination),
            "message": f"Moved {path} to {destination_path}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error moving workspace path: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to move path: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
