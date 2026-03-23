#!/usr/bin/env python3
"""
Secure Filesystem MCP Server (Python FastMCP migration).

This module is a functional migration of the JavaScript MCP filesystem server
into Python using FastMCP from the official MCP Python SDK.

Architecture sections:
1) imports
2) configuration constants
3) filesystem security configuration
4) path validation utilities
5) filesystem helper utilities
6) tool implementations
7) server initialization
8) FastMCP tool registration (via decorators)
9) server execution
"""

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.types import ToolAnnotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import datetime
import difflib
import fnmatch
import json
import mimetypes
import os
import secrets
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any


# ============================================================================
# Configuration constants
# ============================================================================

SERVER_NAME = "secure-filesystem-server"
SERVER_VERSION = "0.2.0"
SERVER_INSTRUCTIONS = (
    "Secure filesystem MCP server with strict path validation, symlink protection, "
    "and tools for reading, writing, editing, searching, and inspecting files."
)
SERVER_WEBSITE_URL = os.environ.get("MCP_SERVER_WEBSITE_URL") or None
DEFAULT_TEXT_ENCODING = "utf-8"
TAIL_HEAD_CHUNK_SIZE = 1024
SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


HTTP_JSON_RESPONSE = _env_flag("MCP_JSON_RESPONSE", default=False)
HTTP_STATELESS = _env_flag("MCP_STATELESS_HTTP", default=False)
HTTP_RETRY_INTERVAL = _env_int("MCP_HTTP_RETRY_INTERVAL")
DEFAULT_TRANSPORT = (os.environ.get("MCP_TRANSPORT") or "stdio").strip().lower()

READ_ONLY_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
WRITE_IDEMPOTENT_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
)

SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"]

MEDIA_MIME_FALLBACKS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Deprecated alias for read_text_file.",
        "input_format": {
            "path": "str (required)",
            "tail": "int (optional)",
            "head": "int (optional)",
        },
        "returns": "Text payload with file content.",
        "changes_filesystem": False,
    },
    {
        "name": "read_text_file",
        "description": "Read full text file or first/last N lines.",
        "input_format": {
            "path": "str (required)",
            "tail": "int (optional)",
            "head": "int (optional)",
        },
        "returns": "Text payload with file content.",
        "changes_filesystem": False,
    },
    {
        "name": "read_media_file",
        "description": "Read binary media and return base64 plus MIME type.",
        "input_format": {"path": "str (required)"},
        "returns": "Media content payload with type/image/audio/blob, data, and mimeType.",
        "changes_filesystem": False,
    },
    {
        "name": "read_multiple_files",
        "description": "Read multiple text files in one call; per-file failures are inlined.",
        "input_format": {"paths": "list[str] (required, non-empty)"},
        "returns": "Combined text payload separated by --- blocks.",
        "changes_filesystem": False,
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with atomic safety behavior.",
        "input_format": {
            "path": "str (required)",
            "content": "str (required)",
        },
        "returns": "Success text payload.",
        "changes_filesystem": True,
    },
    {
        "name": "edit_file",
        "description": "Apply sequential text edits and return unified diff output.",
        "input_format": {
            "path": "str (required)",
            "edits": "list[{oldText: str, newText: str}] (required)",
            "dryRun": "bool (optional, default false)",
        },
        "returns": "Text payload containing a unified diff.",
        "changes_filesystem": True,
    },
    {
        "name": "create_directory",
        "description": "Create a directory recursively.",
        "input_format": {"path": "str (required)"},
        "returns": "Success text payload.",
        "changes_filesystem": True,
    },
    {
        "name": "list_directory",
        "description": "List immediate directory entries with [DIR]/[FILE] tags.",
        "input_format": {"path": "str (required)"},
        "returns": "Text payload with one entry per line.",
        "changes_filesystem": False,
    },
    {
        "name": "list_directory_with_sizes",
        "description": "List directory entries with sizes and totals.",
        "input_format": {
            "path": "str (required)",
            "sortBy": "str (optional: name|size, default name)",
        },
        "returns": "Text payload with size table and summary.",
        "changes_filesystem": False,
    },
    {
        "name": "directory_tree",
        "description": "Return recursive directory tree JSON.",
        "input_format": {
            "path": "str (required)",
            "excludePatterns": "list[str] (optional)",
        },
        "returns": "Text payload containing JSON tree.",
        "changes_filesystem": False,
    },
    {
        "name": "move_file",
        "description": "Move or rename a file/directory.",
        "input_format": {
            "source": "str (required)",
            "destination": "str (required)",
        },
        "returns": "Success text payload.",
        "changes_filesystem": True,
    },
    {
        "name": "delete_file",
        "description": "Delete an existing file.",
        "input_format": {
            "path": "str (required)",
        },
        "returns": "Success text payload.",
        "changes_filesystem": True,
    },
    {
        "name": "delete_directory",
        "description": "Delete a directory (optionally recursive).",
        "input_format": {
            "path": "str (required)",
            "recursive": "bool (optional, default false)",
        },
        "returns": "Success text payload.",
        "changes_filesystem": True,
    },
    {
        "name": "rename_file",
        "description": "Rename a file path.",
        "input_format": {
            "source": "str (required)",
            "destination": "str (required)",
        },
        "returns": "Success text payload.",
        "changes_filesystem": True,
    },
    {
        "name": "rename_directory",
        "description": "Rename a directory path.",
        "input_format": {
            "source": "str (required)",
            "destination": "str (required)",
        },
        "returns": "Success text payload.",
        "changes_filesystem": True,
    },
    {
        "name": "path_exists",
        "description": "Check whether a path exists within allowed directories.",
        "input_format": {
            "path": "str (required)",
        },
        "returns": "Text payload containing JSON with existence and type metadata.",
        "changes_filesystem": False,
    },
    {
        "name": "search_files",
        "description": "Recursively search files/directories by glob-style pattern.",
        "input_format": {
            "path": "str (required)",
            "pattern": "str (required)",
            "excludePatterns": "list[str] (optional)",
        },
        "returns": "Text payload with matching paths or 'No matches found'.",
        "changes_filesystem": False,
    },
    {
        "name": "get_file_info",
        "description": "Return metadata including size, timestamps, flags, and permissions.",
        "input_format": {"path": "str (required)"},
        "returns": "Text payload of key:value metadata lines.",
        "changes_filesystem": False,
    },
    {
        "name": "list_allowed_directories",
        "description": "List currently configured allowed root directories.",
        "input_format": {},
        "returns": "Text payload listing roots.",
        "changes_filesystem": False,
    },
    {
        "name": "list_tool_definitions",
        "description": "Return complete catalog of tools, formats, outputs, and side effects.",
        "input_format": {"outputFormat": "str (optional: json|text, default json)"},
        "returns": "Tool catalog payload in text and structuredContent.",
        "changes_filesystem": False,
    },
    {
        "name": "sync_client_roots",
        "description": "Request MCP roots from the connected client and update allowed directories.",
        "input_format": {},
        "returns": "Text payload with sync result and current allowed roots.",
        "changes_filesystem": False,
    },
    {
        "name": "get_tool_catalog_structured",
        "description": "Return tool catalog as native FastMCP structured output.",
        "input_format": {},
        "returns": "Structured catalog object with server metadata and tools list.",
        "changes_filesystem": False,
    },
]


# ============================================================================
# Filesystem security configuration
# ============================================================================

_ALLOWED_DIRECTORIES: list[str] = []


def set_allowed_directories(directories: list[str]) -> None:
    """Set the globally allowed root directories."""
    global _ALLOWED_DIRECTORIES
    _ALLOWED_DIRECTORIES = [normalize_path(d) for d in directories]


def get_allowed_directories() -> list[str]:
    """Get a copy of the currently allowed directories."""
    return list(_ALLOWED_DIRECTORIES)


def _default_allowed_root() -> str:
    """Prefer the first configured allowed directory for relative path resolution."""
    allowed = get_allowed_directories()
    if allowed:
        return allowed[0]
    return os.getcwd()


def _resolve_requested_absolute_path(requested_path: str) -> str:
    """Resolve a requested path, anchoring relative paths to the default allowed root."""
    expanded = expand_home(requested_path)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(_default_allowed_root(), expanded))


# ============================================================================
# Path validation utilities (migration of path-utils.js + path checks)
# ============================================================================


def _is_windows_platform() -> bool:
    return os.name == "nt"


def convert_to_windows_path(p: str) -> str:
    """
    Convert Unix-style Windows paths to Windows format when appropriate.

    Migration note:
    - WSL paths (/mnt/c/...) are intentionally preserved.
    - /c/... style paths are converted only on Windows.
    """
    if p.startswith("/mnt/"):
        return p

    if _is_windows_platform() and len(p) >= 3 and p[0] == "/" and p[2] == "/" and p[1].isalpha():
        drive_letter = p[1].upper()
        rest = p[2:].replace("/", "\\")
        return f"{drive_letter}:{rest}"

    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        return p.replace("/", "\\")

    return p


def normalize_path(p: str) -> str:
    """
    Normalize path while preserving platform-specific behavior from JS server.

    Includes:
    - quote/whitespace trimming
    - WSL path preservation
    - UNC stabilization
    - drive letter normalization on Windows
    """
    p = p.strip().strip('"').strip("'")

    is_unix_absolute = p.startswith("/")
    is_wsl_path = p.lower().startswith("/mnt/") and len(p) > 5 and p[5].isalpha() and (len(p) == 6 or p[6] == "/")
    is_unix_style_windows = len(p) >= 3 and p[0] == "/" and p[2] == "/" and p[1].isalpha()

    is_unix_path_to_preserve = is_unix_absolute and (
        is_wsl_path
        or (not _is_windows_platform())
        or (_is_windows_platform() and not is_unix_style_windows)
    )

    if is_unix_path_to_preserve:
        while "//" in p:
            p = p.replace("//", "/")
        if len(p) > 1 and p.endswith("/"):
            p = p[:-1]
        return p

    p = convert_to_windows_path(p)

    if p.startswith("\\"):
        p = "\\" + p.lstrip("\\")
        rest = p[2:].replace("\\\\", "\\")
        p = "\\\\" + rest
    else:
        p = p.replace("\\\\", "\\")

    normalized = os.path.normpath(p)

    if p.startswith("\\\\") and not normalized.startswith("\\\\"):
        normalized = "\\" + normalized

    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        result = normalized.replace("/", "\\")
        if result[0].islower():
            result = result[0].upper() + result[1:]
        return result

    if _is_windows_platform():
        return normalized.replace("/", "\\")

    return normalized


def expand_home(filepath: str) -> str:
    """Expand ~ and ~/ prefixes."""
    if filepath == "~" or filepath.startswith("~/"):
        return os.path.join(str(Path.home()), filepath[1:])
    return filepath


def _normalized_real_or_abs(path_value: str) -> str:
    """Normalize path after resolving symlinks where possible."""
    real = os.path.realpath(path_value)
    return normalize_path(real)


def is_path_within_allowed_directories(candidate_path: str, allowed_dirs: list[str]) -> bool:
    """
    Check whether candidate path is contained within at least one allowed root.

    Uses os.path.commonpath for safe boundary checks.
    """
    if not allowed_dirs:
        return False

    candidate_norm = normalize_path(os.path.abspath(candidate_path))
    candidate_cmp = os.path.normcase(candidate_norm)

    for allowed in allowed_dirs:
        allowed_norm = normalize_path(os.path.abspath(allowed))
        allowed_cmp = os.path.normcase(allowed_norm)
        try:
            common = os.path.commonpath([candidate_cmp, allowed_cmp])
            if common == allowed_cmp:
                return True
        except ValueError:
            continue

    return False


def validate_path(requested_path: str) -> str:
    """
    Validate a requested path against allowed directories with symlink protections.

    Security checks:
    - deny paths outside allowed roots
    - deny symlink escapes via realpath validation
    - for non-existent files, validate parent realpath before allowing creation
    """
    absolute = _resolve_requested_absolute_path(requested_path)

    normalized_requested = normalize_path(absolute)

    if not is_path_within_allowed_directories(normalized_requested, _ALLOWED_DIRECTORIES):
        allowed_str = ", ".join(_ALLOWED_DIRECTORIES)
        raise PermissionError(
            f"Access denied - path outside allowed directories: {absolute} not in {allowed_str}"
        )

    if os.path.lexists(absolute):
        real_path = os.path.realpath(absolute)
        normalized_real = normalize_path(real_path)
        if not is_path_within_allowed_directories(normalized_real, _ALLOWED_DIRECTORIES):
            allowed_str = ", ".join(_ALLOWED_DIRECTORIES)
            raise PermissionError(
                f"Access denied - symlink target outside allowed directories: {real_path} not in {allowed_str}"
            )
        return real_path

    parent_dir = os.path.dirname(absolute)
    if not os.path.isdir(parent_dir):
        raise FileNotFoundError(f"Parent directory does not exist: {parent_dir}")

    real_parent = os.path.realpath(parent_dir)
    normalized_parent = normalize_path(real_parent)
    if not is_path_within_allowed_directories(normalized_parent, _ALLOWED_DIRECTORIES):
        allowed_str = ", ".join(_ALLOWED_DIRECTORIES)
        raise PermissionError(
            f"Access denied - parent directory outside allowed directories: {real_parent} not in {allowed_str}"
        )

    return absolute


# ============================================================================
# Root directory management (migration of roots-utils.js)
# ============================================================================


def parse_root_uri(root_uri: str) -> str | None:
    """
    Convert MCP root URI/path into a normalized absolute directory path.

    Accepts either file:// URIs or plain paths.
    Returns None for inaccessible/invalid paths.
    """
    try:
        raw_path = root_uri[7:] if root_uri.startswith("file://") else root_uri
        expanded = expand_home(raw_path)
        absolute = os.path.abspath(expanded)
        resolved = os.path.realpath(absolute)
        return normalize_path(resolved)
    except Exception:
        return None


def format_directory_error(directory: str, error: Exception | None = None, reason: str | None = None) -> str:
    """Format validation errors to mirror JS server logging style."""
    if reason:
        return f"Skipping {reason}: {directory}"
    if error is None:
        return f"Skipping invalid directory: {directory}"
    return f"Skipping invalid directory: {directory} due to error: {error}"


def get_valid_root_directories(requested_roots: list[dict[str, Any]]) -> list[str]:
    """
    Resolve and validate requested roots.

    Input root items are expected to include a URI in the form:
    {"uri": "file:///...", "name": "optional"}
    """
    validated: list[str] = []

    for requested_root in requested_roots:
        uri_value = str(requested_root.get("uri", ""))
        resolved = parse_root_uri(uri_value)
        if not resolved:
            print(format_directory_error(uri_value, reason="invalid path or inaccessible"), file=sys.stderr)
            continue

        try:
            stat_info = os.stat(resolved)
            if os.path.isdir(resolved):
                _ = stat_info
                validated.append(resolved)
            else:
                print(format_directory_error(resolved, reason="non-directory root"), file=sys.stderr)
        except Exception as exc:
            print(format_directory_error(resolved, error=exc), file=sys.stderr)

    return validated


def update_allowed_directories_from_roots(requested_roots: list[dict[str, Any]]) -> None:
    """
    Update allowed directories from roots payload.

    This mirrors the JS behavior used during roots update callbacks.
    """
    validated = get_valid_root_directories(requested_roots)
    if validated:
        set_allowed_directories(validated)
        print(
            f"Updated allowed directories from MCP roots: {len(validated)} valid directories",
            file=sys.stderr,
        )
    else:
        print("No valid root directories provided by client", file=sys.stderr)


# ============================================================================
# Filesystem helper utilities (migration of lib.js)
# ============================================================================


def format_size(bytes_count: int) -> str:
    """Convert bytes to human-readable units."""
    if bytes_count == 0:
        return "0 B"

    unit_index = 0
    value = float(bytes_count)
    while value >= 1024.0 and unit_index < len(SIZE_UNITS) - 1:
        value /= 1024.0
        unit_index += 1

    if unit_index == 0:
        return f"{bytes_count} {SIZE_UNITS[0]}"

    return f"{value:.2f} {SIZE_UNITS[unit_index]}"


def normalize_line_endings(text: str) -> str:
    """Normalize CRLF line endings to LF."""
    return text.replace("\r\n", "\n")


def create_unified_diff(original_content: str, new_content: str, filepath: str = "file") -> str:
    """Generate a unified diff between two strings."""
    normalized_original = normalize_line_endings(original_content)
    normalized_new = normalize_line_endings(new_content)

    original_lines = normalized_original.splitlines(keepends=True)
    new_lines = normalized_new.splitlines(keepends=True)

    diff_lines = difflib.unified_diff(
        original_lines,
        new_lines,
        fromfile=filepath,
        tofile=filepath,
        fromfiledate="original",
        tofiledate="modified",
        n=3,
    )
    return "".join(diff_lines)


def format_diff_block(diff: str) -> str:
    """Format diff using JS-style fenced block with dynamic backtick count."""
    num_backticks = 3
    while ("`" * num_backticks) in diff:
        num_backticks += 1
    ticks = "`" * num_backticks
    return f"{ticks}diff\n{diff}{ticks}\n\n"


def get_file_stats(file_path: str) -> dict[str, Any]:
    """Collect file metadata details."""
    stat_info = os.stat(file_path)
    return {
        "size": stat_info.st_size,
        "created": datetime.datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
        "modified": datetime.datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
        "accessed": datetime.datetime.fromtimestamp(stat_info.st_atime).isoformat(),
        "isDirectory": os.path.isdir(file_path),
        "isFile": os.path.isfile(file_path),
        "permissions": oct(stat_info.st_mode)[-3:],
    }


def read_file_content(file_path: str, encoding: str = DEFAULT_TEXT_ENCODING) -> str:
    """Read text file content."""
    with open(file_path, "r", encoding=encoding) as handle:
        return handle.read()


def _safe_atomic_write(file_path: str, content: str) -> None:
    """
    Write via temp file + atomic replace to reduce TOCTOU/symlink race risks.
    """
    temp_path = f"{file_path}.{secrets.token_hex(16)}.tmp"
    try:
        with open(temp_path, "w", encoding=DEFAULT_TEXT_ENCODING) as temp_file:
            temp_file.write(content)
        os.replace(temp_path, file_path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


def write_file_content(file_path: str, content: str) -> None:
    """
    Write content with JS-equivalent security semantics.

    Flow:
    - Try exclusive create first.
    - If file already exists, use atomic replace path.
    """
    try:
        with open(file_path, "x", encoding=DEFAULT_TEXT_ENCODING) as handle:
            handle.write(content)
        return
    except FileExistsError:
        _safe_atomic_write(file_path, content)


def apply_file_edits(file_path: str, edits: list[dict[str, str]], dry_run: bool = False) -> str:
    """
    Apply sequential text edits with exact and whitespace-tolerant matching.

    Mirrors the JS behavior:
    - exact replacement when possible
    - fallback line-by-line match with trim-based comparison
    - preserve leading indentation of replacement
    - return unified diff
    """
    with open(file_path, "r", encoding=DEFAULT_TEXT_ENCODING) as handle:
        content = normalize_line_endings(handle.read())

    modified_content = content

    for edit in edits:
        old_text = normalize_line_endings(str(edit.get("oldText", "")))
        new_text = normalize_line_endings(str(edit.get("newText", "")))

        if old_text in modified_content:
            modified_content = modified_content.replace(old_text, new_text, 1)
            continue

        old_lines = old_text.split("\n")
        content_lines = modified_content.split("\n")
        match_found = False

        for i in range(0, len(content_lines) - len(old_lines) + 1):
            potential_match = content_lines[i : i + len(old_lines)]
            is_match = True
            for j, old_line in enumerate(old_lines):
                if old_line.strip() != potential_match[j].strip():
                    is_match = False
                    break

            if not is_match:
                continue

            original_indent = content_lines[i][: len(content_lines[i]) - len(content_lines[i].lstrip())]
            new_lines_raw = new_text.split("\n")
            new_lines_adjusted: list[str] = []

            for j, new_line in enumerate(new_lines_raw):
                if j == 0:
                    new_lines_adjusted.append(original_indent + new_line.lstrip())
                    continue

                old_indent = ""
                if j < len(old_lines):
                    old_line = old_lines[j]
                    old_indent = old_line[: len(old_line) - len(old_line.lstrip())]

                new_indent = new_line[: len(new_line) - len(new_line.lstrip())]
                if old_indent and new_indent:
                    relative_indent = len(new_indent) - len(old_indent)
                    relative_indent = max(0, relative_indent)
                    new_lines_adjusted.append(original_indent + (" " * relative_indent) + new_line.lstrip())
                else:
                    new_lines_adjusted.append(new_line)

            content_lines[i : i + len(old_lines)] = new_lines_adjusted
            modified_content = "\n".join(content_lines)
            match_found = True
            break

        if not match_found:
            raise ValueError(f"Could not find exact match for edit:\n{old_text}")

    diff = create_unified_diff(content, modified_content, file_path)
    formatted_diff = format_diff_block(diff)

    if not dry_run:
        _safe_atomic_write(file_path, modified_content)

    return formatted_diff


def tail_file(file_path: str, num_lines: int) -> str:
    """Read the last N lines efficiently by scanning chunks from EOF."""
    if num_lines <= 0:
        return ""

    file_size = os.path.getsize(file_path)
    if file_size == 0:
        return ""

    lines: list[str] = []
    position = file_size
    remaining_text = ""

    with open(file_path, "rb") as handle:
        while position > 0 and len(lines) < num_lines:
            size = min(TAIL_HEAD_CHUNK_SIZE, position)
            position -= size
            handle.seek(position)
            chunk_bytes = handle.read(size)
            if not chunk_bytes:
                break

            chunk_text = chunk_bytes.decode(DEFAULT_TEXT_ENCODING, errors="replace") + remaining_text
            chunk_text = normalize_line_endings(chunk_text)
            chunk_lines = chunk_text.split("\n")

            if position > 0:
                remaining_text = chunk_lines[0]
                chunk_lines = chunk_lines[1:]

            for line in reversed(chunk_lines):
                if len(lines) >= num_lines:
                    break
                lines.insert(0, line)

    return "\n".join(lines)


def head_file(file_path: str, num_lines: int) -> str:
    """Read the first N lines efficiently by chunking from BOF."""
    if num_lines <= 0:
        return ""

    lines: list[str] = []
    buffer = ""

    with open(file_path, "rb") as handle:
        while len(lines) < num_lines:
            chunk_bytes = handle.read(TAIL_HEAD_CHUNK_SIZE)
            if not chunk_bytes:
                break

            buffer += chunk_bytes.decode(DEFAULT_TEXT_ENCODING, errors="replace")
            buffer = normalize_line_endings(buffer)

            last_newline = buffer.rfind("\n")
            if last_newline != -1:
                complete_lines = buffer[:last_newline].split("\n")
                buffer = buffer[last_newline + 1 :]
                for line in complete_lines:
                    lines.append(line)
                    if len(lines) >= num_lines:
                        break

    if buffer and len(lines) < num_lines:
        lines.append(buffer)

    return "\n".join(lines)


def _match_with_exclusion(relative_path: str, pattern: str, exclude_patterns: list[str]) -> bool:
    """Apply include and exclude matching semantics."""
    rel = relative_path.replace("\\", "/")
    for exclude in exclude_patterns:
        if fnmatch.fnmatch(rel, exclude):
            return False
    return fnmatch.fnmatch(rel, pattern)


def search_files_with_validation(
    root_path: str,
    pattern: str,
    allowed_directories: list[str],
    exclude_patterns: list[str] | None = None,
) -> list[str]:
    """Recursively search files using glob-like matching with path validation."""
    _ = allowed_directories
    excludes = exclude_patterns or []
    results: list[str] = []

    for current_root, dir_names, file_names in os.walk(root_path):
        names = list(dir_names) + list(file_names)

        for entry_name in names:
            full_path = os.path.join(current_root, entry_name)
            try:
                validate_path(full_path)
            except Exception:
                continue

            relative_path = os.path.relpath(full_path, root_path).replace("\\", "/")

            excluded = any(fnmatch.fnmatch(relative_path, ex) for ex in excludes)
            if excluded:
                continue

            if fnmatch.fnmatch(relative_path, pattern):
                results.append(full_path)

    return results


def read_file_as_base64_stream(file_path: str) -> str:
    """Read binary file and return base64-encoded string."""
    with open(file_path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def _format_text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    """Return JS-compatible text content payload."""
    payload: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"content": text},
    }
    if is_error:
        payload["isError"] = True
    return payload


def _format_media_result(content_item: dict[str, Any]) -> dict[str, Any]:
    """Return JS-compatible media content payload."""
    return {
        "content": [content_item],
        "structuredContent": {"content": [content_item]},
    }


def _error_message_from_exception(exc: Exception) -> str:
    """Convert exceptions to human-readable tool errors."""
    return str(exc)


def _handle_tool_exception(exc: Exception) -> dict[str, Any]:
    """Standardized error payload for tool handlers."""
    return _format_text_result(_error_message_from_exception(exc), is_error=True)


def _format_tool_catalog(output_format: str = "json") -> dict[str, Any]:
    """Return the tool catalog in both human-readable and structured forms."""
    fmt = output_format.strip().lower()
    if fmt not in {"json", "text"}:
        raise ValueError("outputFormat must be either 'json' or 'text'")

    if fmt == "json":
        text = json.dumps(
            {
                "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "toolCount": len(TOOL_DEFINITIONS),
                "tools": TOOL_DEFINITIONS,
            },
            indent=2,
        )
    else:
        lines = [
            f"Server: {SERVER_NAME} v{SERVER_VERSION}",
            f"Tool count: {len(TOOL_DEFINITIONS)}",
            "",
        ]
        for tool in TOOL_DEFINITIONS:
            lines.append(f"- {tool['name']}")
            lines.append(f"  Description: {tool['description']}")
            lines.append(f"  Input format: {json.dumps(tool['input_format'])}")
            lines.append(f"  Returns: {tool['returns']}")
            lines.append(f"  Changes filesystem: {tool['changes_filesystem']}")
            lines.append("")
        text = "\n".join(lines).rstrip()

    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "toolCount": len(TOOL_DEFINITIONS),
            "tools": TOOL_DEFINITIONS,
        },
    }


# ============================================================================
# Server initialization
# ============================================================================


@dataclass
class AppLifespanContext:
    """Lifespan context for startup metadata and root initialization state."""

    startup_time_utc: str
    initial_allowed_directories: list[str]


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[AppLifespanContext]:
    """Initialize and validate allowed directories at server startup."""
    initial_allowed = _initialize_allowed_directories_from_args()
    if len(initial_allowed) == 0:
        print(
            "Started without allowed directories - waiting for client to provide roots via MCP protocol",
            file=sys.stderr,
        )
    yield AppLifespanContext(
        startup_time_utc=datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        initial_allowed_directories=initial_allowed,
    )


mcp = FastMCP(
    name=SERVER_NAME,
    instructions=SERVER_INSTRUCTIONS,
    website_url=SERVER_WEBSITE_URL,
    json_response=HTTP_JSON_RESPONSE,
    stateless_http=HTTP_STATELESS,
    retry_interval=HTTP_RETRY_INTERVAL,
    lifespan=app_lifespan,
)


def _initialize_allowed_directories_from_args() -> list[str]:
    """Parse and validate startup allowed directories from CLI arguments."""
    args = sys.argv[1:]

    if len(args) == 0:
        print(
            "Usage: mcp-server-filesystem [allowed-directory] [additional-directories...]",
            file=sys.stderr,
        )
        print("Note: Allowed directories can be provided via:", file=sys.stderr)
        print("  1. Command-line arguments (shown above)", file=sys.stderr)
        print("  2. MCP roots protocol (if client supports it)", file=sys.stderr)
        print(
            "At least one directory must be provided by EITHER method for the server to operate.",
            file=sys.stderr,
        )

    initial_allowed: list[str] = []

    for directory in args:
        expanded = expand_home(directory)
        absolute = os.path.abspath(expanded)
        try:
            resolved = os.path.realpath(absolute)
            initial_allowed.append(normalize_path(resolved))
        except Exception:
            initial_allowed.append(normalize_path(absolute))

    for directory in initial_allowed:
        try:
            if not os.path.isdir(directory):
                print(f"Error: {directory} is not a directory", file=sys.stderr)
                sys.exit(1)
            _ = os.stat(directory)
        except Exception as exc:
            print(f"Error accessing directory {directory}: {exc}", file=sys.stderr)
            sys.exit(1)

    set_allowed_directories(initial_allowed)
    return initial_allowed


# ============================================================================
# Tool implementations
# ============================================================================


def _read_text_file_impl(path: str, tail: int | None = None, head: int | None = None) -> dict[str, Any]:
    """Shared implementation for read_file (deprecated) and read_text_file."""
    valid_path = validate_path(path)

    if tail is not None and head is not None:
        raise ValueError("Cannot specify both head and tail parameters simultaneously")

    if tail is not None:
        content = tail_file(valid_path, int(tail))
    elif head is not None:
        content = head_file(valid_path, int(head))
    else:
        content = read_file_content(valid_path)

    return _format_text_result(content)


@mcp.tool(
    title="Read File (Deprecated)",
    description="Deprecated alias of read_text_file.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def read_file(path: str, tail: int | None = None, head: int | None = None) -> dict[str, Any]:
    """
    Read file text content (deprecated alias of read_text_file).

    Args:
        path: Path to file.
        tail: If set, return only last N lines.
        head: If set, return only first N lines.
    """
    try:
        return _read_text_file_impl(path=path, tail=tail, head=head)
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Read Text File",
    description="Read complete text file content or the first/last N lines.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def read_text_file(path: str, tail: int | None = None, head: int | None = None) -> dict[str, Any]:
    """
    Read complete text content from a file.

    Args:
        path: Path to file.
        tail: If set, return only last N lines.
        head: If set, return only first N lines.
    """
    try:
        return _read_text_file_impl(path=path, tail=tail, head=head)
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Read Media File",
    description="Read image/audio/binary files and return base64 payload with MIME type.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def read_media_file(path: str) -> dict[str, Any]:
    """
    Read an image/audio/binary file and return base64 with MIME type.

    Args:
        path: Path to media file.
    """
    try:
        valid_path = validate_path(path)
        extension = os.path.splitext(valid_path)[1].lower()

        mime_type = MEDIA_MIME_FALLBACKS.get(extension)
        if not mime_type:
            guessed, _ = mimetypes.guess_type(valid_path)
            mime_type = guessed or "application/octet-stream"

        data = read_file_as_base64_stream(valid_path)

        if mime_type.startswith("image/"):
            content_type = "image"
        elif mime_type.startswith("audio/"):
            content_type = "audio"
        else:
            content_type = "blob"

        content_item = {
            "type": content_type,
            "data": data,
            "mimeType": mime_type,
        }
        return _format_media_result(content_item)
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Read Multiple Files",
    description="Read multiple files in one request and include per-file errors inline.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def read_multiple_files(paths: list[str]) -> dict[str, Any]:
    """
    Read multiple text files in one call.

    Args:
        paths: List of file paths.
    """
    try:
        if not paths:
            raise ValueError("At least one file path must be provided")

        results: list[str] = []
        for file_path in paths:
            try:
                valid_path = validate_path(file_path)
                content = read_file_content(valid_path)
                results.append(f"{file_path}:\n{content}\n")
            except Exception as exc:
                results.append(f"{file_path}: Error - {exc}")

        text = "\n---\n".join(results)
        return _format_text_result(text)
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Write File",
    description="Create or overwrite a file with atomic write semantics.",
    annotations=WRITE_IDEMPOTENT_ANNOTATIONS,
)
async def write_file(
    path: str,
    content: str,
    ctx: Context[ServerSession, AppLifespanContext],
) -> dict[str, Any]:
    """
    Create or overwrite file content atomically.

    Args:
        path: Target file path.
        content: Full content to write.
    """
    try:
        await ctx.debug(f"write_file requested for path: {path}")
        valid_path = validate_path(path)
        write_file_content(valid_path, content)
        await ctx.info(f"write_file succeeded for path: {path}")
        return _format_text_result(f"Successfully wrote to {path}")
    except Exception as exc:
        await ctx.error(f"write_file failed for path {path}: {exc}")
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Edit File",
    description="Apply sequential text edits and return unified diff output.",
    annotations=WRITE_ANNOTATIONS,
)
async def edit_file(
    path: str,
    edits: list[dict[str, str]],
    ctx: Context[ServerSession, AppLifespanContext],
    dryRun: bool = False,
) -> dict[str, Any]:
    """
    Apply line-oriented edits and return a unified diff.

    Args:
        path: Target file path.
        edits: List of objects with oldText and newText.
        dryRun: If True, do not write changes.
    """
    try:
        await ctx.report_progress(progress=0.0, total=1.0, message="Starting edit_file")
        valid_path = validate_path(path)
        result = apply_file_edits(valid_path, edits, dry_run=dryRun)
        await ctx.report_progress(progress=1.0, total=1.0, message="Completed edit_file")
        return _format_text_result(result)
    except Exception as exc:
        await ctx.error(f"edit_file failed for path {path}: {exc}")
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Create Directory",
    description="Create a directory recursively.",
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True),
)
def create_directory(path: str) -> dict[str, Any]:
    """
    Create directory recursively.

    Args:
        path: Directory path.
    """
    try:
        valid_path = validate_path(path)
        os.makedirs(valid_path, exist_ok=True)
        return _format_text_result(f"Successfully created directory {path}")
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="List Directory",
    description="List immediate children in a directory with [DIR]/[FILE] labels.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def list_directory(path: str) -> dict[str, Any]:
    """
    List direct children of a directory.

    Args:
        path: Directory path.
    """
    try:
        valid_path = validate_path(path)
        entries = os.listdir(valid_path)

        formatted_rows: list[str] = []
        for entry in entries:
            full = os.path.join(valid_path, entry)
            tag = "[DIR]" if os.path.isdir(full) else "[FILE]"
            formatted_rows.append(f"{tag} {entry}")

        return _format_text_result("\n".join(formatted_rows))
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="List Directory With Sizes",
    description="List directory children with sizes and summary totals.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def list_directory_with_sizes(path: str, sortBy: str = "name") -> dict[str, Any]:
    """
    List directory entries with file sizes and summary.

    Args:
        path: Directory path.
        sortBy: Either name or size.
    """
    try:
        if sortBy not in {"name", "size"}:
            raise ValueError("sortBy must be either 'name' or 'size'")

        valid_path = validate_path(path)
        names = os.listdir(valid_path)

        detailed_entries: list[dict[str, Any]] = []
        for name in names:
            entry_path = os.path.join(valid_path, name)
            is_directory = os.path.isdir(entry_path)
            try:
                stat_info = os.stat(entry_path)
                size = stat_info.st_size
                mtime = datetime.datetime.fromtimestamp(stat_info.st_mtime).isoformat()
            except Exception:
                size = 0
                mtime = datetime.datetime.fromtimestamp(0).isoformat()

            detailed_entries.append(
                {
                    "name": name,
                    "isDirectory": is_directory,
                    "size": size,
                    "mtime": mtime,
                }
            )

        if sortBy == "size":
            sorted_entries = sorted(detailed_entries, key=lambda x: int(x["size"]), reverse=True)
        else:
            sorted_entries = sorted(detailed_entries, key=lambda x: str(x["name"]))

        formatted_entries: list[str] = []
        for entry in sorted_entries:
            tag = "[DIR]" if entry["isDirectory"] else "[FILE]"
            name = str(entry["name"]).ljust(30)
            size_display = "" if entry["isDirectory"] else format_size(int(entry["size"])).rjust(10)
            formatted_entries.append(f"{tag} {name} {size_display}".rstrip())

        total_files = sum(1 for e in detailed_entries if not e["isDirectory"])
        total_dirs = sum(1 for e in detailed_entries if e["isDirectory"])
        total_size = sum(int(e["size"]) for e in detailed_entries if not e["isDirectory"])

        summary = [
            "",
            f"Total: {total_files} files, {total_dirs} directories",
            f"Combined size: {format_size(total_size)}",
        ]

        text = "\n".join(formatted_entries + summary)
        return _format_text_result(text)
    except Exception as exc:
        return _handle_tool_exception(exc)


def _should_exclude_directory_tree(relative_path: str, exclude_patterns: list[str]) -> bool:
    """Replicate JS directory_tree exclusion semantics using fnmatch."""
    rel = relative_path.replace("\\", "/")

    for pattern in exclude_patterns:
        pattern_norm = pattern.replace("\\", "/")

        if "*" in pattern_norm:
            if fnmatch.fnmatch(rel, pattern_norm):
                return True
            continue

        if (
            fnmatch.fnmatch(rel, pattern_norm)
            or fnmatch.fnmatch(rel, f"**/{pattern_norm}")
            or fnmatch.fnmatch(rel, f"**/{pattern_norm}/**")
        ):
            return True

    return False


def _build_directory_tree(root_path: str, current_path: str, exclude_patterns: list[str]) -> list[dict[str, Any]]:
    """Recursively build directory tree JSON structure."""
    valid_path = validate_path(current_path)

    result: list[dict[str, Any]] = []
    entries = sorted(os.listdir(valid_path))

    for entry in entries:
        full_path = os.path.join(current_path, entry)
        relative_path = os.path.relpath(full_path, root_path).replace("\\", "/")

        if _should_exclude_directory_tree(relative_path, exclude_patterns):
            continue

        is_directory = os.path.isdir(validate_path(full_path))
        entry_data: dict[str, Any] = {
            "name": entry,
            "type": "directory" if is_directory else "file",
        }

        if is_directory:
            entry_data["children"] = _build_directory_tree(root_path, full_path, exclude_patterns)

        result.append(entry_data)

    return result


@mcp.tool(
    title="Directory Tree",
    description="Return recursive directory tree JSON with optional exclude patterns.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def directory_tree(path: str, excludePatterns: list[str] | None = None) -> dict[str, Any]:
    """
    Return recursive directory tree as JSON string.

    Args:
        path: Root path for the tree.
        excludePatterns: Glob-like exclusion patterns.
    """
    try:
        excludes = excludePatterns or []
        _ = validate_path(path)
        tree_data = _build_directory_tree(path, path, excludes)
        text = json.dumps(tree_data, indent=2)
        return _format_text_result(text)
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Move File",
    description="Move or rename files and directories.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def move_file(source: str, destination: str) -> dict[str, Any]:
    """
    Move or rename files/directories.

    Args:
        source: Source path.
        destination: Destination path.
    """
    try:
        valid_source = validate_path(source)
        valid_destination = validate_path(destination)
        os.rename(valid_source, valid_destination)
        return _format_text_result(f"Successfully moved {source} to {destination}")
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Delete File",
    description="Delete an existing file.",
    annotations=WRITE_ANNOTATIONS,
)
def delete_file(path: str) -> dict[str, Any]:
    """
    Delete a file.

    Args:
        path: Target file path.
    """
    try:
        valid_path = validate_path(path)
        if not os.path.exists(valid_path):
            raise FileNotFoundError(f"File does not exist: {path}")
        if os.path.isdir(valid_path):
            raise IsADirectoryError(f"Path is a directory, use delete_directory: {path}")

        os.remove(valid_path)
        return _format_text_result(f"Successfully deleted file {path}")
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Delete Directory",
    description="Delete a directory; supports recursive deletion when requested.",
    annotations=WRITE_ANNOTATIONS,
)
def delete_directory(path: str, recursive: bool = False) -> dict[str, Any]:
    """
    Delete a directory.

    Args:
        path: Target directory path.
        recursive: If True, delete recursively.
    """
    try:
        valid_path = validate_path(path)
        if not os.path.exists(valid_path):
            raise FileNotFoundError(f"Directory does not exist: {path}")
        if not os.path.isdir(valid_path):
            raise NotADirectoryError(f"Path is not a directory: {path}")

        if recursive:
            shutil.rmtree(valid_path)
        else:
            os.rmdir(valid_path)

        return _format_text_result(f"Successfully deleted directory {path}")
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Rename File",
    description="Rename a file from source to destination.",
    annotations=WRITE_ANNOTATIONS,
)
def rename_file(source: str, destination: str) -> dict[str, Any]:
    """
    Rename a file.

    Args:
        source: Existing file path.
        destination: New file path.
    """
    try:
        valid_source = validate_path(source)
        valid_destination = validate_path(destination)

        if not os.path.exists(valid_source):
            raise FileNotFoundError(f"File does not exist: {source}")
        if not os.path.isfile(valid_source):
            raise IsADirectoryError(f"Source is not a file: {source}")

        os.rename(valid_source, valid_destination)
        return _format_text_result(f"Successfully renamed file {source} to {destination}")
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Rename Directory",
    description="Rename a directory from source to destination.",
    annotations=WRITE_ANNOTATIONS,
)
def rename_directory(source: str, destination: str) -> dict[str, Any]:
    """
    Rename a directory.

    Args:
        source: Existing directory path.
        destination: New directory path.
    """
    try:
        valid_source = validate_path(source)
        valid_destination = validate_path(destination)

        if not os.path.exists(valid_source):
            raise FileNotFoundError(f"Directory does not exist: {source}")
        if not os.path.isdir(valid_source):
            raise NotADirectoryError(f"Source is not a directory: {source}")

        os.rename(valid_source, valid_destination)
        return _format_text_result(f"Successfully renamed directory {source} to {destination}")
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Path Exists",
    description="Check if a path exists and return type metadata.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def path_exists(path: str) -> dict[str, Any]:
    """
    Check whether a path exists within allowed directories.

    Args:
        path: Candidate path to check.
    """
    try:
        absolute = _resolve_requested_absolute_path(path)
        normalized_requested = normalize_path(absolute)

        if not is_path_within_allowed_directories(normalized_requested, get_allowed_directories()):
            allowed_str = ", ".join(get_allowed_directories())
            raise PermissionError(
                f"Access denied - path outside allowed directories: {absolute} not in {allowed_str}"
            )

        exists = os.path.lexists(absolute)
        result: dict[str, Any] = {
            "path": path,
            "absolutePath": absolute,
            "exists": exists,
        }

        if exists:
            real_path = os.path.realpath(absolute)
            normalized_real = normalize_path(real_path)
            if not is_path_within_allowed_directories(normalized_real, get_allowed_directories()):
                allowed_str = ", ".join(get_allowed_directories())
                raise PermissionError(
                    f"Access denied - symlink target outside allowed directories: {real_path} not in {allowed_str}"
                )

            result["realPath"] = real_path
            result["isFile"] = os.path.isfile(real_path)
            result["isDirectory"] = os.path.isdir(real_path)

        return _format_text_result(json.dumps(result, indent=2))
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Search Files",
    description="Recursively search files and directories using glob-style patterns.",
    annotations=READ_ONLY_ANNOTATIONS,
)
async def search_files(
    path: str,
    pattern: str,
    ctx: Context[ServerSession, AppLifespanContext],
    excludePatterns: list[str] | None = None,
) -> dict[str, Any]:
    """
    Recursively search files/dirs by pattern.

    Args:
        path: Root search path.
        pattern: Glob-style pattern relative to root.
        excludePatterns: Optional exclusion patterns.
    """
    try:
        await ctx.report_progress(progress=0.0, total=1.0, message="Starting search_files")
        excludes = excludePatterns or []
        valid_path = validate_path(path)
        results = search_files_with_validation(
            root_path=valid_path,
            pattern=pattern,
            allowed_directories=get_allowed_directories(),
            exclude_patterns=excludes,
        )
        text = "\n".join(results) if results else "No matches found"
        await ctx.info(f"search_files found {len(results)} matches for pattern '{pattern}'")
        await ctx.report_progress(progress=1.0, total=1.0, message="Completed search_files")
        return _format_text_result(text)
    except Exception as exc:
        await ctx.error(f"search_files failed for root {path}: {exc}")
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Get File Info",
    description="Return metadata for a file or directory.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def get_file_info(path: str) -> dict[str, Any]:
    """
    Return detailed metadata for a file or directory.

    Args:
        path: File or directory path.
    """
    try:
        valid_path = validate_path(path)
        info = get_file_stats(valid_path)
        text = "\n".join(f"{k}: {v}" for k, v in info.items())
        return _format_text_result(text)
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="List Allowed Directories",
    description="List currently configured allowed root directories.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def list_allowed_directories() -> dict[str, Any]:
    """List currently allowed root directories."""
    try:
        allowed = get_allowed_directories()
        text = "Allowed directories:\n" + "\n".join(allowed)
        return _format_text_result(text)
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="List Tool Definitions",
    description="Return complete tool catalog with names, descriptions, formats, and side effects.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def list_tool_definitions(outputFormat: str = "json") -> dict[str, Any]:
    """
    Return all tool names, descriptions, argument formats, outputs, and side effects.

    Args:
        outputFormat: Response format, either 'json' or 'text'.
    """
    try:
        return _format_tool_catalog(output_format=outputFormat)
    except Exception as exc:
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Sync Client Roots",
    description="Fetch roots from client session and update allowed directories.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)
async def sync_client_roots(ctx: Context[ServerSession, AppLifespanContext]) -> dict[str, Any]:
    """
    Sync allowed directories from MCP client roots in the active session.

    This is the FastMCP-compatible equivalent of JS roots refresh behavior.
    """
    try:
        await ctx.info("sync_client_roots started")
        client_params = getattr(ctx.session, "client_params", None)
        capabilities = getattr(client_params, "capabilities", None)
        roots_capability = getattr(capabilities, "roots", None) if capabilities is not None else None

        if not roots_capability:
            allowed = get_allowed_directories()
            text = (
                "Client does not advertise MCP roots capability; kept current allowed directories.\n"
                + "Allowed directories:\n"
                + "\n".join(allowed)
            )
            await ctx.warning("sync_client_roots skipped: client roots capability not available")
            return _format_text_result(text)

        roots_result = await ctx.session.list_roots()
        roots = getattr(roots_result, "roots", [])
        requested_roots = [{"uri": str(getattr(root, "uri", "")), "name": getattr(root, "name", None)} for root in roots]

        update_allowed_directories_from_roots(requested_roots)
        allowed = get_allowed_directories()
        text = "Synced allowed directories from client roots.\nAllowed directories:\n" + "\n".join(allowed)
        await ctx.info(f"sync_client_roots completed with {len(allowed)} allowed directories")
        return _format_text_result(text)
    except Exception as exc:
        await ctx.error(f"sync_client_roots failed: {exc}")
        return _handle_tool_exception(exc)


@mcp.tool(
    title="Tool Catalog (Structured)",
    description="Return tool catalog as native FastMCP structured output.",
    annotations=READ_ONLY_ANNOTATIONS,
)
def get_tool_catalog_structured() -> dict[str, Any]:
    """Return tool catalog using native structured output instead of content wrappers."""
    return {
        "server": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "instructions": SERVER_INSTRUCTIONS,
        },
        "toolCount": len(TOOL_DEFINITIONS),
        "tools": TOOL_DEFINITIONS,
    }


# ============================================================================
# FastMCP registration notes
# ============================================================================
# Tool registration is completed by @mcp.tool() decorators above.


# ============================================================================
# Server execution
# ============================================================================


def main() -> None:
    """Run the server over stdio transport."""
    transport = DEFAULT_TRANSPORT if DEFAULT_TRANSPORT in SUPPORTED_TRANSPORTS else "stdio"
    if transport != DEFAULT_TRANSPORT:
        print(
            f"Invalid MCP_TRANSPORT '{DEFAULT_TRANSPORT}', falling back to stdio",
            file=sys.stderr,
        )

    print(
        (
            f"Secure MCP Filesystem Server running on {transport} "
            f"(json_response={HTTP_JSON_RESPONSE}, stateless_http={HTTP_STATELESS})"
        ),
        file=sys.stderr,
    )

    try:
        mcp.run(transport=transport)
    except Exception:
        print("Fatal error running server:", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
