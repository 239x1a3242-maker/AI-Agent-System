#!/usr/bin/env python3
"""Unified MCP bridge server for fetch, filesystem, and shell backends.

This server provides two bridge tools:
- bridge_list_tools: returns all available tools across three backend servers.
- bridge_request: accepts a JSON request, dispatches to the correct backend tool,
  and returns the backend result.

It can also be used as a local Python module for tests via:
- list_all_tools()
- handle_json_request(request_dict)
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path
import traceback
import types
from typing import Any, Callable
from typing import get_args, get_origin

from mcp.server.fastmcp import FastMCP


SERVER_NAME = "unified-bridge-server"
SERVER_INSTRUCTIONS = (
    "Unified MCP bridge for fetch, filesystem, and shell tools. "
    "Use bridge_list_tools to inspect tool formats and bridge_request to dispatch "
    "JSON tool calls."
)
SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}

BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR.parent.parent
DEFAULT_WORKSPACE_DIR = (APP_DIR / "workspace").resolve()
DEFAULT_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def _load_module(module_name: str, filename: str) -> Any:
    module_path = BASE_DIR / filename
    if not module_path.exists():
        raise RuntimeError(f"Missing backend module file: {module_path}")

    spec = importlib.util.spec_from_file_location(module_name, str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to create import spec for: {module_path}")

    module = importlib.util.module_from_spec(spec)
    # Pydantic resolves the filesystem server's generic Context annotations
    # through its defining module while that module is being imported.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


FETCH_MODULE = _load_module("backend_fetch", "server-fetch.py")
FILESYSTEM_MODULE = _load_module("backend_filesystem", "server-filesystem.py")
SHELL_MODULE = _load_module("backend_shell", "server-shell.py")


def _init_filesystem_allowed_directories() -> list[str]:
    configured = os.environ.get("BRIDGE_ALLOWED_DIRECTORIES", "").strip()
    if configured:
        raw_dirs = [item.strip() for item in configured.split(os.pathsep) if item.strip()]
    else:
        raw_dirs = [str(DEFAULT_WORKSPACE_DIR)]

    normalized: list[str] = []
    for directory in raw_dirs:
        expanded = os.path.expanduser(directory)
        absolute = os.path.abspath(expanded)
        normalized.append(os.path.realpath(absolute))

    if hasattr(FILESYSTEM_MODULE, "set_allowed_directories"):
        FILESYSTEM_MODULE.set_allowed_directories(normalized)

    return normalized


ALLOWED_DIRECTORIES = _init_filesystem_allowed_directories()
DEFAULT_ALLOWED_DIRECTORY = ALLOWED_DIRECTORIES[0] if ALLOWED_DIRECTORIES else str(DEFAULT_WORKSPACE_DIR)
os.environ.setdefault("MCP_SHELL_DEFAULT_CWD", DEFAULT_ALLOWED_DIRECTORY)
os.environ.setdefault("MCP_SHELL_ALLOWED_DIRECTORIES", os.pathsep.join(ALLOWED_DIRECTORIES or [str(DEFAULT_WORKSPACE_DIR)]))


def _text_result(text: str, is_error: bool = False, structured: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        payload["isError"] = True
    if structured is not None:
        payload["structuredContent"] = structured
    return payload


FETCH_DISPATCH: dict[str, tuple[Callable[[dict[str, Any]], dict[str, Any]], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "fetch_html": (FETCH_MODULE.parse_request_payload, FETCH_MODULE.Fetcher.html),
    "fetch_markdown": (FETCH_MODULE.parse_request_payload, FETCH_MODULE.Fetcher.markdown),
    "fetch_txt": (FETCH_MODULE.parse_request_payload, FETCH_MODULE.Fetcher.txt),
    "fetch_json": (FETCH_MODULE.parse_request_payload, FETCH_MODULE.Fetcher.json),
    "fetch_url": (FETCH_MODULE.parse_fetch_url_payload, FETCH_MODULE.Fetcher.fetch_url),
    "fetch_readable": (FETCH_MODULE.parse_request_payload, FETCH_MODULE.Fetcher.readable),
    "fetch_youtube_transcript": (FETCH_MODULE.parse_youtube_payload, FETCH_MODULE.Fetcher.youtube_transcript),
    "web_search": (FETCH_MODULE.parse_web_search_payload, FETCH_MODULE.Fetcher.web_search),
}

FETCH_TOOL_NAMES = set(FETCH_DISPATCH.keys())
FILESYSTEM_TOOL_NAMES = {item.get("name", "") for item in FILESYSTEM_MODULE.TOOL_DEFINITIONS}
SHELL_TOOL_NAMES = {"run_command"}

FETCH_TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    name: getattr(FETCH_MODULE, name)
    for name in FETCH_TOOL_NAMES
    if callable(getattr(FETCH_MODULE, name, None))
}
FILESYSTEM_TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    name: getattr(FILESYSTEM_MODULE, name)
    for name in FILESYSTEM_TOOL_NAMES
    if callable(getattr(FILESYSTEM_MODULE, name, None))
}


def _annotation_to_json_schema(annotation: Any) -> dict[str, Any]:
    if annotation in (inspect.Signature.empty, Any):
        return {}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is None:
        if annotation is str:
            return {"type": "string"}
        if annotation is int:
            return {"type": "integer"}
        if annotation is float:
            return {"type": "number"}
        if annotation is bool:
            return {"type": "boolean"}
        if annotation is dict:
            return {"type": "object", "additionalProperties": True}
        if annotation is list:
            return {"type": "array", "items": {}}
        return {}

    if origin in (list, tuple, set):
        item_schema = _annotation_to_json_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}

    if origin is dict:
        value_schema = _annotation_to_json_schema(args[1]) if len(args) > 1 else {}
        return {"type": "object", "additionalProperties": value_schema or True}

    if origin in (types.UnionType,) or str(origin) == "typing.Union":
        non_none_args = [arg for arg in args if arg is not type(None)]
        if len(non_none_args) == 1 and len(non_none_args) != len(args):
            schema = _annotation_to_json_schema(non_none_args[0])
            if schema:
                schema["nullable"] = True
            return schema
        variants = [_annotation_to_json_schema(arg) for arg in non_none_args]
        variants = [variant for variant in variants if variant]
        if len(non_none_args) != len(args):
            variants.append({"type": "null"})
        return {"anyOf": variants} if variants else {}

    return {}


def _build_parameter_schema(func: Callable[..., Any], descriptions: dict[str, Any] | None = None) -> dict[str, Any]:
    signature = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in signature.parameters.items():
        if name in {"self", "cls", "ctx"}:
            continue
        if param.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            continue

        schema = _annotation_to_json_schema(param.annotation)
        if not schema:
            schema = {"type": "string"}

        if descriptions and name in descriptions:
            schema["description"] = str(descriptions[name])

        if param.default is inspect.Signature.empty:
            required.append(name)
        elif param.default is not None and "default" not in schema:
            schema["default"] = param.default
        elif param.default is None:
            schema["nullable"] = True

        properties[name] = schema

    payload: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        payload["required"] = required
    return payload


def list_all_tools() -> dict[str, Any]:
    """Return all bridge-managed tools with descriptions and input format metadata."""
    tools: list[dict[str, Any]] = []

    for item in FETCH_MODULE.TOOL_CATALOG:
        tool_name = item.get("name")
        tools.append(
            {
                "name": tool_name,
                "backend": "server-fetch.py",
                "description": item.get("description"),
                "input_format": item.get("input_format", {}),
                "parameters": _build_parameter_schema(
                    FETCH_TOOL_FUNCTIONS.get(str(tool_name)),
                    item.get("input_format", {}),
                ) if FETCH_TOOL_FUNCTIONS.get(str(tool_name)) else {"type": "object", "properties": {}},
                "output_format": item.get("output_format", {}),
            }
        )

    for item in FILESYSTEM_MODULE.TOOL_DEFINITIONS:
        tool_name = item.get("name")
        tools.append(
            {
                "name": tool_name,
                "backend": "server-filesystem.py",
                "description": item.get("description"),
                "input_format": item.get("input_format", {}),
                "parameters": _build_parameter_schema(
                    FILESYSTEM_TOOL_FUNCTIONS.get(str(tool_name)),
                    item.get("input_format", {}),
                ) if FILESYSTEM_TOOL_FUNCTIONS.get(str(tool_name)) else {"type": "object", "properties": {}},
                "returns": item.get("returns"),
                "changes_filesystem": item.get("changes_filesystem", False),
            }
        )

    tools.append(
        {
            "name": "run_command",
            "backend": "server-shell.py",
            "description": "Run a shell command in a persistent terminal session",
            "input_format": {
                "command": "string (required)",
                "timeout_seconds": "number (optional)",
                "terminal_id": "string (optional)",
            },
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run inside the workspace-backed terminal session.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Optional timeout in seconds.",
                    },
                    "terminal_id": {
                        "type": "string",
                        "description": "Optional terminal session identifier.",
                    },
                },
                "required": ["command"],
            },
            "returns": "Text output from command execution",
            "changes_filesystem": "depends on command",
        }
    )

    return {
        "server": SERVER_NAME,
        "backend_count": 3,
        "tool_count": len(tools),
        "allowed_directories": ALLOWED_DIRECTORIES,
        "tools": tools,
    }


def _filter_call_kwargs(func: Callable[..., Any], raw_args: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(func)
    accepted: set[str] = set()
    for param in signature.parameters.values():
        if param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            accepted.add(param.name)
    return {k: v for k, v in raw_args.items() if k in accepted}


def _dispatch_fetch(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    parser_fn, handler_fn = FETCH_DISPATCH[tool_name]
    payload = parser_fn(arguments)
    return handler_fn(payload)


def _dispatch_filesystem(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "write_file":
        path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("write_file expects {path: string, content: string}")
        valid_path = FILESYSTEM_MODULE.validate_path(path)
        FILESYSTEM_MODULE.write_file_content(valid_path, content)
        return FILESYSTEM_MODULE._format_text_result(f"Successfully wrote to {path}")

    if tool_name == "edit_file":
        path = arguments.get("path")
        edits = arguments.get("edits")
        dry_run = bool(arguments.get("dryRun", False))
        if not isinstance(path, str) or not isinstance(edits, list):
            raise ValueError("edit_file expects {path: string, edits: list, dryRun?: bool}")
        valid_path = FILESYSTEM_MODULE.validate_path(path)
        diff_text = FILESYSTEM_MODULE.apply_file_edits(valid_path, edits, dry_run=dry_run)
        return FILESYSTEM_MODULE._format_text_result(diff_text)

    if tool_name == "search_files":
        path = arguments.get("path")
        pattern = arguments.get("pattern")
        exclude_patterns = arguments.get("excludePatterns") or []
        if not isinstance(path, str) or not isinstance(pattern, str):
            raise ValueError("search_files expects {path: string, pattern: string, excludePatterns?: list}")
        valid_path = FILESYSTEM_MODULE.validate_path(path)
        results = FILESYSTEM_MODULE.search_files_with_validation(
            root_path=valid_path,
            pattern=pattern,
            allowed_directories=FILESYSTEM_MODULE.get_allowed_directories(),
            exclude_patterns=exclude_patterns,
        )
        text = "\n".join(results) if results else "No matches found"
        return FILESYSTEM_MODULE._format_text_result(text)

    if tool_name == "sync_client_roots":
        return FILESYSTEM_MODULE._format_text_result(
            "sync_client_roots requires an active MCP session context and is not supported via bridge_request",
            is_error=True,
        )

    if tool_name == "get_tool_catalog_structured":
        data = FILESYSTEM_MODULE.get_tool_catalog_structured()
        return _text_result(json.dumps(data, ensure_ascii=False, indent=2), structured=data)

    func = getattr(FILESYSTEM_MODULE, tool_name, None)
    if not callable(func):
        raise ValueError(f"Unknown filesystem tool: {tool_name}")
    if inspect.iscoroutinefunction(func):
        raise ValueError(f"Tool requires MCP context and is not directly invocable: {tool_name}")

    kwargs = _filter_call_kwargs(func, arguments)
    return func(**kwargs)


def _dispatch_shell(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name != "run_command":
        raise ValueError(f"Unknown shell tool: {tool_name}")

    command = arguments.get("command")
    timeout_seconds = arguments.get("timeout_seconds")
    terminal_id = arguments.get("terminal_id")
    output = SHELL_MODULE.execute_command(
        command,
        timeout_seconds=timeout_seconds,
        terminal_id=terminal_id,
    )
    return _text_result(str(output))


def _parse_request_object(request: dict[str, Any]) -> tuple[str, dict[str, Any], str | None, str | None]:
    tool_name = (
        request.get("tool")
        or request.get("tool_name")
        or request.get("name")
    )
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("Request must include a non-empty tool/tool_name/name field")

    arguments = request.get("arguments")
    if arguments is None:
        arguments = request.get("args", {})
    if not isinstance(arguments, dict):
        raise ValueError("Request arguments/args must be an object")

    model = request.get("model")
    request_id = request.get("id")
    return tool_name.strip(), arguments, model if isinstance(model, str) else None, request_id if isinstance(request_id, str) else None


def handle_json_request(request: dict[str, Any]) -> dict[str, Any]:
    """Route one JSON request to the backend server that owns the requested tool."""
    tool_name, arguments, model_name, request_id = _parse_request_object(request)

    if tool_name in {"list_tools", "tool_list", "bridge_list_tools"}:
        catalog = list_all_tools()
        return {
            "ok": True,
            "tool": tool_name,
            "backend": "bridge",
            "model": model_name,
            "id": request_id,
            "result": catalog,
        }

    if tool_name in FETCH_TOOL_NAMES:
        backend = "server-fetch.py"
        result = _dispatch_fetch(tool_name, arguments)
    elif tool_name in FILESYSTEM_TOOL_NAMES:
        backend = "server-filesystem.py"
        result = _dispatch_filesystem(tool_name, arguments)
    elif tool_name in SHELL_TOOL_NAMES:
        backend = "server-shell.py"
        result = _dispatch_shell(tool_name, arguments)
    else:
        raise ValueError(f"Unknown tool: {tool_name}")

    return {
        "ok": not bool(result.get("isError", False)) if isinstance(result, dict) else True,
        "tool": tool_name,
        "backend": backend,
        "model": model_name,
        "id": request_id,
        "result": result,
    }


def handle_json_request_text(request_json: str) -> dict[str, Any]:
    """Parse request JSON string and dispatch through handle_json_request."""
    try:
        parsed = json.loads(request_json)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"Invalid JSON request: {exc}",
        }

    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "error": "JSON request must decode to an object",
        }

    try:
        return handle_json_request(parsed)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)


@mcp.tool(name="bridge_list_tools", description="List all bridged tools with descriptions and input formats")
def bridge_list_tools(output_format: str = "json") -> dict[str, Any]:
    catalog = list_all_tools()

    if output_format.strip().lower() == "text":
        lines = [
            f"Server: {catalog['server']}",
            f"Backend count: {catalog['backend_count']}",
            f"Tool count: {catalog['tool_count']}",
            "",
            "Tools:",
        ]
        for tool in catalog["tools"]:
            lines.append(f"- {tool['name']} ({tool['backend']})")
            lines.append(f"  Description: {tool.get('description', '')}")
            lines.append(f"  Input format: {json.dumps(tool.get('input_format', {}), ensure_ascii=False)}")

        text = "\n".join(lines)
        return _text_result(text, structured=catalog)

    return _text_result(json.dumps(catalog, ensure_ascii=False, indent=2), structured=catalog)


@mcp.tool(
    name="bridge_request",
    description=(
        "Dispatch a JSON tool request to fetch/filesystem/shell backends. "
        "Request format: {id?, model?, tool, arguments?}"
    ),
)
def bridge_request(request_json: str) -> dict[str, Any]:
    response = handle_json_request_text(request_json)
    return _text_result(
        json.dumps(response, ensure_ascii=False, indent=2),
        is_error=not bool(response.get("ok", False)),
        structured=response,
    )


def _run_cli_request_mode(request_json: str) -> int:
    response = handle_json_request_text(request_json)
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response.get("ok", False) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified MCP bridge server")
    parser.add_argument(
        "--json",
        dest="json_request",
        help="Run one JSON request in CLI mode and print response",
    )
    args = parser.parse_args()

    if args.json_request:
        raise SystemExit(_run_cli_request_mode(args.json_request))

    transport = os.environ.get("FASTMCP_TRANSPORT", "stdio").strip().lower()
    mount_path = os.environ.get("FASTMCP_MOUNT_PATH")
    if transport not in SUPPORTED_TRANSPORTS:
        print(
            f'Invalid FASTMCP_TRANSPORT="{transport}"; falling back to "stdio"',
            file=os.sys.stderr,
        )
        transport = "stdio"

    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "sse":
        mcp.run(transport="sse", mount_path=mount_path)
    else:
        mcp.run(transport="streamable-http", mount_path=mount_path)


if __name__ == "__main__":
    main()
