"""Tool registry for the AI Agent System."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .date_time import current_datetime
from .mcp_bridge import get_mcp_bridge
from .send_email import send_email
from .server import handle_json_request, list_all_tools

TOOL_NAME_ALIASES = {
    "fatch_url": "fetch_url",
}


def _normalize_tool_result_payload(result: Any) -> Dict[str, Any]:
    """Convert mixed local tool return styles into a consistent success/error envelope."""
    if isinstance(result, dict) and result.get("error"):
        return {
            "success": False,
            "error": str(result.get("error")),
        }

    if (
        isinstance(result, list)
        and len(result) == 1
        and isinstance(result[0], dict)
        and result[0].get("error")
    ):
        return {
            "success": False,
            "error": str(result[0].get("error")),
        }

    if isinstance(result, str) and result.strip().lower().startswith("error"):
        return {
            "success": False,
            "error": result.strip(),
        }

    return {
        "success": True,
        "result": result,
    }


@dataclass
class Tool:
    """Tool definition."""

    name: str
    description: str
    parameters: Dict[str, Any]
    function: Callable[..., Any]


TOOLS: Dict[str, Tool] = {}


def register_tool(
    name: str,
    description: str,
    parameters: Dict[str, Any],
    function: Callable[..., Any],
) -> None:
    """Register a new local tool."""
    TOOLS[name] = Tool(
        name=name,
        description=description,
        parameters=parameters,
        function=function,
    )


def get_tool(name: str) -> Optional[Tool]:
    """Get a local tool by name, including aliases."""
    return TOOLS.get(TOOL_NAME_ALIASES.get(name, name))


def _direct_bridge_tools() -> List[Dict[str, Any]]:
    """Return unified bridge tools as OpenAI function specs."""
    catalog = list_all_tools()
    tools: List[Dict[str, Any]] = []
    for item in catalog.get("tools", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(item.get("description", "")).strip(),
                    "parameters": item.get("parameters") or {"type": "object", "properties": {}},
                },
            }
        )
    return tools


def _merge_tool_specs(*tool_groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge OpenAI-format tool lists by tool name."""
    merged: Dict[str, Dict[str, Any]] = {}
    for group in tool_groups:
        for tool in group:
            function = tool.get("function", {}) if isinstance(tool, dict) else {}
            name = str(function.get("name", "")).strip()
            if not name:
                continue
            merged[name] = tool
    return list(merged.values())


async def initialize_tool_runtime() -> None:
    """Start MCP-backed tools if configured."""
    await get_mcp_bridge().ensure_started(reserved_names=set(TOOLS.keys()))


async def shutdown_tool_runtime() -> None:
    """Close MCP-backed tool sessions."""
    await get_mcp_bridge().shutdown()


def list_tools() -> List[Dict[str, Any]]:
    """List all available tools in OpenAI function format."""
    local_tools = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in TOOLS.values()
    ]
    bridge_tools = get_mcp_bridge().list_tools()
    fallback_tools = _direct_bridge_tools()
    return _merge_tool_specs(local_tools, fallback_tools, bridge_tools)


def _normalize_bridge_execution_response(response: Dict[str, Any]) -> Dict[str, Any]:
    if not bool(response.get("ok", False)):
        return {
            "success": False,
            "error": str(response.get("error") or "Bridge tool request failed"),
        }
    return {
        "success": True,
        "result": response.get("result"),
    }


async def execute_tool_async(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a local, MCP-backed, or direct-bridge tool with given arguments."""
    canonical_name = TOOL_NAME_ALIASES.get(name, name)

    tool = get_tool(canonical_name)
    if tool:
        try:
            result = await asyncio.to_thread(tool.function, **arguments)
            return _normalize_tool_result_payload(result)
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
            }

    bridge = get_mcp_bridge()
    if bridge.has_tool(canonical_name):
        try:
            result = await bridge.call_tool(canonical_name, arguments)
            return {
                "success": True,
                "result": result,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
            }

    try:
        response = await asyncio.to_thread(
            handle_json_request,
            {
                "tool": canonical_name,
                "arguments": arguments or {},
            },
        )
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }

    if isinstance(response, dict) and "ok" in response:
        return _normalize_bridge_execution_response(response)

    return {
        "success": False,
        "error": f"Tool '{canonical_name}' not found",
    }


def execute_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous compatibility wrapper for tool execution."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(execute_tool_async(name, arguments))
    return {
        "success": False,
        "error": f"Tool '{name}' requires async execution; use execute_tool_async().",
    }


register_tool(
    name="send_email",
    description=(
        "Send an email using the configured SMTP settings. "
        "Requires recipient email, subject, and HTML body."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient email address.",
            },
            "subject": {
                "type": "string",
                "description": "Email subject line.",
            },
            "body": {
                "type": "string",
                "description": "Email body content in HTML format.",
            },
            "from_email": {
                "type": "string",
                "description": "Optional sender email address override.",
            },
        },
        "required": ["to", "subject", "body"],
    },
    function=send_email,
)

register_tool(
    name="current_datetime",
    description="Return the current date and time, optionally for a specific IANA timezone.",
    parameters={
        "type": "object",
        "properties": {
            "timezone_name": {
                "type": "string",
                "description": 'Optional IANA timezone, for example "Asia/Kolkata".',
            }
        },
    },
    function=current_datetime,
)
