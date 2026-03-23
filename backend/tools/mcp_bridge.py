"""MCP client bridge for local stdio servers."""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import mcp.types as mcp_types
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    MCP_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    mcp_types = None  # type: ignore[assignment]
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    MCP_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_BRIDGE_SERVER_NAME = "unified-bridge-server"


@dataclass
class MCPToolBinding:
    """Resolved MCP tool mapped to its owning session."""

    public_name: str
    server_name: str
    server_tool_name: str
    description: str
    input_schema: dict[str, Any]
    session: Any
    proxy_tool_name: str | None = None
    backend: str | None = None


class MCPToolBridge:
    """Manage stdio MCP server sessions and expose their tools."""

    def __init__(self) -> None:
        self._app_root = Path(__file__).resolve().parents[2]
        self._tools_root = Path(__file__).resolve().parent
        self._workspace_root = (self._app_root / "workspace").resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._config_candidates = [
            self._app_root / ".vscode" / "mcp.json",
            self._tools_root / "mcp-config.json",
        ]
        self._session_stack: Optional[contextlib.AsyncExitStack] = None
        self._config_signature = ""
        self._bindings: dict[str, MCPToolBinding] = {}
        self._server_errors: dict[str, str] = {}
        self._reserved_names: set[str] = set()
        self._init_lock: Optional[Any] = None

    def _get_lock(self):
        if self._init_lock is None:
            import asyncio

            self._init_lock = asyncio.Lock()
        return self._init_lock

    def _default_bridge_server_config(self) -> dict[str, Any]:
        return {
            "command": sys.executable,
            "args": [str((self._tools_root / "server.py").resolve())],
            "cwd": str(self._app_root),
            "env": {
                "FASTMCP_TRANSPORT": "stdio",
                "BRIDGE_ALLOWED_DIRECTORIES": str(self._workspace_root),
                "MCP_SHELL_DEFAULT_CWD": str(self._workspace_root),
                "MCP_SHELL_ALLOWED_DIRECTORIES": str(self._workspace_root),
                "MCP_SHELL_TIMEOUT_SECONDS": "60",
                "MCP_SHELL_MAX_TIMEOUT_SECONDS": "300",
                "MCP_SHELL_MAX_COMMAND_LENGTH": "8192",
            },
        }

    def _load_config(self) -> dict[str, Any]:
        for config_path in self._config_candidates:
            if not config_path.exists():
                continue
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return loaded
            except Exception as exc:
                logger.warning("Failed to load MCP config %s: %s", config_path, exc)
        return {}

    def _build_runtime_config(self) -> dict[str, Any]:
        config = self._load_config()
        runtime_config = dict(config) if isinstance(config, dict) else {}
        raw_servers = runtime_config.get("mcpServers")
        if not isinstance(raw_servers, dict) or not raw_servers:
            raw_servers = runtime_config.get("servers", {})
        if not isinstance(raw_servers, dict):
            raw_servers = {}

        servers = dict(raw_servers)
        servers[DEFAULT_BRIDGE_SERVER_NAME] = self._default_bridge_server_config()
        runtime_config["mcpServers"] = servers
        return runtime_config

    def _make_signature(self, config: dict[str, Any]) -> str:
        try:
            return json.dumps(config, sort_keys=True)
        except Exception:
            return ""

    def _make_public_name(self, server_name: str, tool_name: str, used: set[str]) -> str:
        if tool_name not in used:
            return tool_name
        candidate = f"{server_name}_{tool_name}"
        if candidate not in used:
            return candidate
        suffix = 2
        while f"{candidate}_{suffix}" in used:
            suffix += 1
        return f"{candidate}_{suffix}"

    def _normalize_env(self, server_name: str, server_config: dict[str, Any]) -> dict[str, str] | None:
        raw_env = server_config.get("env")
        env = {str(key): str(value) for key, value in raw_env.items()} if isinstance(raw_env, dict) else {}
        if server_name == DEFAULT_BRIDGE_SERVER_NAME:
            merged = self._default_bridge_server_config()["env"]
            merged.update(env)
            return merged
        return env or None

    def _extract_text_from_payload(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return str(payload)

        text_chunks: list[str] = []
        for block in payload.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_value = str(block.get("text", "")).strip()
                if text_value:
                    text_chunks.append(text_value)
        return "\n\n".join(text_chunks).strip()

    def _parse_json_text(self, value: str) -> Any:
        try:
            return json.loads(value)
        except Exception:
            return None

    def _unwrap_mcp_tool_payload(self, payload: Any) -> Any:
        if (
            isinstance(payload, dict)
            and "structuredContent" in payload
            and isinstance(payload.get("content"), list)
        ):
            structured = payload.get("structuredContent")
            if structured is not None:
                return structured
        return payload

    def _normalize_backend_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {
                "text": str(payload),
                "is_error": False,
            }

        text = self._extract_text_from_payload(payload)
        structured = payload.get("structuredContent")
        extra_blocks: list[dict[str, Any]] = []

        for block in payload.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                continue
            dumped = dict(block)
            if dumped.get("type") in {"image", "audio"} and "data" in dumped:
                dumped["data_length"] = len(str(dumped.get("data", "")))
                dumped.pop("data", None)
            extra_blocks.append(dumped)

        result: dict[str, Any] = {
            "is_error": bool(payload.get("isError", False)),
        }
        if text:
            result["text"] = text
        if structured is not None:
            result["structured"] = structured
        if extra_blocks:
            result["content"] = extra_blocks
        return result

    def _normalize_tool_result(self, result: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}

        text_chunks: list[str] = []
        extra_blocks: list[dict[str, Any]] = []
        for block in getattr(result, "content", []) or []:
            dumped = block.model_dump(by_alias=True, exclude_none=True)
            block_type = dumped.get("type")
            if block_type == "text":
                text_value = str(dumped.get("text", "")).strip()
                if text_value:
                    text_chunks.append(text_value)
                continue
            if block_type in {"image", "audio"} and "data" in dumped:
                dumped["data_length"] = len(str(dumped.get("data", "")))
                dumped.pop("data", None)
            extra_blocks.append(dumped)

        if text_chunks:
            payload["text"] = "\n\n".join(text_chunks)
        structured_content = getattr(result, "structuredContent", None)
        if structured_content is not None:
            payload["structured"] = structured_content
        if extra_blocks:
            payload["content"] = extra_blocks
        payload["is_error"] = bool(getattr(result, "isError", False))
        return payload

    def _normalize_bridge_response(self, result: Any) -> dict[str, Any]:
        outer = self._normalize_tool_result(result)
        bridge_response = self._unwrap_mcp_tool_payload(outer.get("structured"))
        if not isinstance(bridge_response, dict):
            bridge_response = self._unwrap_mcp_tool_payload(self._parse_json_text(outer.get("text", "")))

        if not isinstance(bridge_response, dict):
            return {
                "text": outer.get("text", "Bridge response was not JSON"),
                "is_error": True,
            }

        if not bool(bridge_response.get("ok", False)):
            error_message = str(bridge_response.get("error") or outer.get("text") or "Bridge tool returned an error")
            return {
                "text": error_message,
                "structured": bridge_response,
                "is_error": True,
            }

        return self._normalize_backend_payload(bridge_response.get("result"))

    async def _load_bridge_catalog(self, session: Any) -> list[dict[str, Any]]:
        result = await session.call_tool("bridge_list_tools", {"output_format": "json"})
        normalized = self._normalize_tool_result(result)

        catalog = self._unwrap_mcp_tool_payload(normalized.get("structured"))
        if not isinstance(catalog, dict):
            catalog = self._unwrap_mcp_tool_payload(self._parse_json_text(normalized.get("text", "")))
        if not isinstance(catalog, dict):
            raise RuntimeError("bridge_list_tools did not return a catalog object")

        tools = catalog.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("bridge_list_tools catalog is missing tools")
        return [tool for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)]

    async def _register_session_tools(
        self,
        server_name: str,
        session: Any,
        tools: list[Any],
        bindings: dict[str, MCPToolBinding],
        used_names: set[str],
    ) -> None:
        tool_by_name = {getattr(tool, "name", ""): tool for tool in tools}
        if {"bridge_list_tools", "bridge_request"}.issubset(tool_by_name):
            try:
                bridge_tools = await self._load_bridge_catalog(session)
                if bridge_tools:
                    for item in bridge_tools:
                        public_name = self._make_public_name(server_name, str(item.get("name")), used_names)
                        used_names.add(public_name)
                        bindings[public_name] = MCPToolBinding(
                            public_name=public_name,
                            server_name=str(server_name),
                            server_tool_name=str(item.get("name")),
                            description=str(item.get("description") or "").strip(),
                            input_schema=item.get("parameters") or {"type": "object", "properties": {}},
                            session=session,
                            proxy_tool_name="bridge_request",
                            backend=str(item.get("backend") or ""),
                        )
                    return
            except Exception as exc:
                logger.warning("Failed to expand bridge tools for '%s': %s", server_name, exc)

        for tool in tools:
            public_name = self._make_public_name(str(server_name), tool.name, used_names)
            used_names.add(public_name)
            bindings[public_name] = MCPToolBinding(
                public_name=public_name,
                server_name=str(server_name),
                server_tool_name=tool.name,
                description=(tool.description or "").strip(),
                input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                session=session,
            )

    async def ensure_started(self, reserved_names: Optional[set[str]] = None, force: bool = False) -> None:
        if reserved_names is not None:
            self._reserved_names = set(reserved_names)

        lock = self._get_lock()
        async with lock:
            config = self._build_runtime_config()
            signature = self._make_signature(config)
            if not force and self._session_stack is not None and signature == self._config_signature:
                return

            await self._shutdown_locked()

            if not MCP_SDK_AVAILABLE:
                self._server_errors = {
                    "mcp": "Python MCP SDK is not installed; MCP tools are disabled.",
                }
                self._config_signature = signature
                return

            servers = config.get("mcpServers")
            if not isinstance(servers, dict) or not servers:
                self._config_signature = signature
                return

            stack = contextlib.AsyncExitStack()
            bindings: dict[str, MCPToolBinding] = {}
            errors: dict[str, str] = {}
            used_names = set(self._reserved_names)

            try:
                for server_name, server_config in servers.items():
                    if not isinstance(server_config, dict):
                        continue

                    command = str(server_config.get("command", "")).strip()
                    args = [str(value) for value in server_config.get("args", []) or []]
                    if not command:
                        errors[str(server_name)] = "Missing server command"
                        continue

                    try:
                        server_params = StdioServerParameters(
                            command=command,
                            args=args,
                            env=self._normalize_env(str(server_name), server_config),
                            cwd=str(server_config.get("cwd") or self._app_root),
                        )
                        read_stream, write_stream = await stack.enter_async_context(stdio_client(server_params))
                        session = await stack.enter_async_context(
                            ClientSession(
                                read_stream,
                                write_stream,
                                client_info=mcp_types.Implementation(
                                    name="ai-agent-system",
                                    version="1.0.0",
                                ),
                            )
                        )
                        await session.initialize()
                        list_result = await session.list_tools()
                        await self._register_session_tools(
                            str(server_name),
                            session,
                            list(list_result.tools or []),
                            bindings,
                            used_names,
                        )
                    except Exception as exc:
                        errors[str(server_name)] = str(exc)
                        logger.warning("Failed to initialize MCP server '%s': %s", server_name, exc)

                self._session_stack = stack
                self._bindings = bindings
                self._server_errors = errors
                self._config_signature = signature
            except Exception:
                await stack.aclose()
                raise

    async def shutdown(self) -> None:
        lock = self._get_lock()
        async with lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        if self._session_stack is not None:
            try:
                await self._session_stack.aclose()
            finally:
                self._session_stack = None
        self._bindings = {}
        self._server_errors = {}

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for binding in self._bindings.values():
            prefix = f"[MCP:{binding.server_name}]"
            if binding.backend:
                prefix = f"{prefix} [{binding.backend}]"
            description = binding.description or "No description provided."
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": binding.public_name,
                        "description": f"{prefix} {description}".strip(),
                        "parameters": binding.input_schema or {"type": "object", "properties": {}},
                    },
                }
            )
        return tools

    def has_tool(self, name: str) -> bool:
        return name in self._bindings

    def has_server(self, server_name: str) -> bool:
        target = str(server_name or "").strip()
        if not target:
            return False
        return any(
            binding.server_name == target or binding.backend == target
            for binding in self._bindings.values()
        )

    def get_server_errors(self) -> dict[str, str]:
        return dict(self._server_errors)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        await self.ensure_started()
        binding = self._bindings.get(name)
        if binding is None:
            raise KeyError(f"MCP tool '{name}' not found")

        try:
            if binding.proxy_tool_name:
                request_json = json.dumps(
                    {
                        "tool": binding.server_tool_name,
                        "arguments": arguments or {},
                    },
                    ensure_ascii=False,
                )
                result = await binding.session.call_tool(
                    binding.proxy_tool_name,
                    {"request_json": request_json},
                )
                normalized = self._normalize_bridge_response(result)
            else:
                result = await binding.session.call_tool(binding.server_tool_name, arguments or {})
                normalized = self._normalize_tool_result(result)
        except Exception as exc:
            logger.warning("MCP tool '%s' failed once, retrying after reconnect: %s", name, exc)
            await self.ensure_started(force=True)
            binding = self._bindings.get(name)
            if binding is None:
                raise KeyError(f"MCP tool '{name}' disappeared after reconnect") from exc
            if binding.proxy_tool_name:
                request_json = json.dumps(
                    {
                        "tool": binding.server_tool_name,
                        "arguments": arguments or {},
                    },
                    ensure_ascii=False,
                )
                result = await binding.session.call_tool(
                    binding.proxy_tool_name,
                    {"request_json": request_json},
                )
                normalized = self._normalize_bridge_response(result)
            else:
                result = await binding.session.call_tool(binding.server_tool_name, arguments or {})
                normalized = self._normalize_tool_result(result)

        if normalized.get("is_error"):
            error_message = normalized.get("text") or normalized.get("structured") or "MCP tool returned an error"
            raise RuntimeError(str(error_message))
        return normalized


_MCP_BRIDGE = MCPToolBridge()


def get_mcp_bridge() -> MCPToolBridge:
    """Return the singleton MCP bridge."""
    return _MCP_BRIDGE
