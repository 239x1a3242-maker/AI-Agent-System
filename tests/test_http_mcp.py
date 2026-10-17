"""Exercise real MCP sessions with a deterministic HTTP wire fixture.

The model in the ReAct test is scripted; no live inference is claimed.
"""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from backend import tools
from backend.tools import mcp_bridge

ENDPOINT = "https://search.parallel.ai/mcp"
SOURCE_URL = "https://docs.python.org/3/library/asyncio.html"
REMOTE_SEARCH = "parallel-search_web_search"
REMOTE_FETCH = "parallel-search_web_fetch"
HTTP_CLIENT = httpx.AsyncClient


class MCPWire:
    def __init__(self):
        self.requests = []
        self.failure = None
        self.called = asyncio.Event()

    async def handle(self, request):
        payload = json.loads(request.content) if request.content else {}
        self.requests.append((request, payload))
        method = payload.get("method")
        if method == "notifications/initialized" or method == "notifications/cancelled":
            return httpx.Response(202)
        if method == "initialize":
            if self.failure == "initial_http":
                return httpx.Response(403, text="Forbidden")
            result = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": [
                {"name": "web_search", "description": "Search the web", "inputSchema": {
                    "type": "object", "properties": {"objective": {"type": "string"}, "search_queries": {"type": "array", "items": {"type": "string"}}}, "required": ["objective", "search_queries"]}},
                {"name": "web_fetch", "description": "Fetch pages", "inputSchema": {
                    "type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}}}, "required": ["urls"]}},
            ]}
        elif method == "tools/call":
            self.called.set()
            if self.failure == "slow":
                await asyncio.Event().wait()
            if self.failure == "http":
                return httpx.Response(403, text="Forbidden")
            if self.failure == "permission":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"],
                    "error": {"code": -32001, "message": "Permission denied"}})
            result = {
                "content": [{"type": "text", "text": f"Python asyncio documentation: {SOURCE_URL}"}],
                "structuredContent": {"results": [{"title": "asyncio", "url": SOURCE_URL}]},
                "isError": self.failure == "tool",
            }
            if result["isError"]:
                result["content"][0]["text"] = "Service quota exceeded"
        else:
            raise AssertionError(f"Unexpected MCP request: {payload}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def client(self, **kwargs):
        return HTTP_CLIENT(transport=httpx.MockTransport(self.handle), **kwargs)

    def calls(self):
        return [p for _, p in self.requests if p.get("method") == "tools/call"]


class HTTPMCPTests(unittest.IsolatedAsyncioTestCase):
    def config(self, directory, servers):
        path = Path(directory) / "mcp.json"
        path.write_text(json.dumps({"mcpServers": servers}))
        return path

    def remote(self, **overrides):
        return {"type": "http", "url": ENDPOINT, **overrides}

    async def test_default_preset_is_disabled_and_stdio_remains(self):
        bridge = mcp_bridge.MCPToolBridge()
        with patch.object(mcp_bridge.httpx, "AsyncClient", side_effect=AssertionError("Unexpected HTTP")):
            try:
                await bridge.ensure_started()
                self.assertTrue(bridge.has_tool("web_search"))
                self.assertFalse(bridge.has_tool(REMOTE_SEARCH))
                self.assertEqual(bridge.get_server_errors(), {})
                binding = bridge._bindings["web_search"]
                self.assertEqual(binding.server_name, "unified-bridge-server")
                self.assertEqual(binding.backend, "server-fetch.py")
            finally:
                await bridge.shutdown()

    async def test_registry_search_fetch_headers_urls_and_saved_config(self):
        wire = MCPWire()
        bridge = mcp_bridge.MCPToolBridge()
        with tempfile.TemporaryDirectory() as directory:
            # Deliberately put remote first: it must not shadow web_search.
            primary = self.config(directory, {"parallel-search": self.remote(timeout=7, headers={"X-Preference": "saved"})})
            bridge._config_candidates = [primary, Path("backend/tools/mcp-config.json")]
            with patch.object(mcp_bridge, "_MCP_BRIDGE", bridge), patch.object(
                mcp_bridge.httpx, "AsyncClient", side_effect=wire.client
            ), patch.dict(os.environ, {"PARALLEL_API_KEY": "ambient-must-not-leak", "TAVILY_API_KEY": "also-isolated"}):
                try:
                    await tools.initialize_tool_runtime()
                    self.assertEqual(bridge.get_server_errors(), {})
                    specs = {t["function"]["name"]: t for t in tools.list_tools()}
                    self.assertIn(REMOTE_SEARCH, specs)
                    self.assertIn(REMOTE_FETCH, specs)
                    self.assertIn("web_search", specs)
                    self.assertIn("run_command", specs)
                    search = await tools.execute_tool_async(REMOTE_SEARCH, {"objective": "Python asyncio documentation", "search_queries": ["Python asyncio documentation"]})
                    fetch = await tools.execute_tool_async(REMOTE_FETCH, {"urls": [SOURCE_URL]})
                    for result in [search, fetch]:
                        self.assertTrue(result["success"], result)
                        self.assertIn(SOURCE_URL, result["result"]["text"])
                        self.assertEqual(result["result"]["structured"]["results"][0]["url"], SOURCE_URL)
                    self.assertEqual([p["params"]["name"] for p in wire.calls()], ["web_search", "web_fetch"])
                    self.assertEqual(wire.calls()[1]["params"]["arguments"]["urls"], [SOURCE_URL])
                    for request, payload in wire.requests:
                        self.assertEqual(str(request.url), ENDPOINT)
                        self.assertEqual(request.headers["User-Agent"], mcp_bridge.PROJECT_USER_AGENT)
                        self.assertEqual(request.headers["X-Preference"], "saved")
                        self.assertNotIn("Authorization", request.headers)
                        self.assertEqual(request.extensions["timeout"]["read"], 7)
                    # A saved disable takes effect without reconnecting to HTTP.
                    self.config(directory, {"parallel-search": self.remote(disabled=True)})
                    count = len(wire.requests)
                    await tools.initialize_tool_runtime()
                    self.assertFalse(bridge.has_tool(REMOTE_SEARCH))
                    self.assertEqual(len(wire.requests), count)
                    self.assertTrue(bridge.has_tool("web_search"))
                finally:
                    await bridge.shutdown()

    async def test_explicit_endpoint_and_headers_are_preserved(self):
        wire = MCPWire()
        bridge = mcp_bridge.MCPToolBridge()
        with tempfile.TemporaryDirectory() as directory:
            endpoint = "https://search.parallel.ai/mcp?max_results=3"
            bridge._config_candidates = [self.config(directory, {"parallel-search": self.remote(
                url=endpoint, headers={"Authorization": "Bearer explicitly-saved"})})]
            with patch.object(mcp_bridge.httpx, "AsyncClient", side_effect=wire.client):
                try:
                    await bridge.ensure_started()
                    await bridge.call_tool(REMOTE_SEARCH, {"objective": "asyncio", "search_queries": ["Python asyncio documentation"]})
                    for request, _ in wire.requests:
                        self.assertEqual(str(request.url), endpoint)
                        self.assertEqual(request.headers["Authorization"], "Bearer explicitly-saved")
                        self.assertEqual(request.headers["User-Agent"], mcp_bridge.PROJECT_USER_AGENT)
                finally:
                    await bridge.shutdown()

    async def test_errors_timeout_and_cancellation_do_not_replay_or_fallback(self):
        for failure in ["tool", "permission", "slow", "http", "cancel"]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                wire = MCPWire()
                bridge = mcp_bridge.MCPToolBridge()
                bridge._config_candidates = [self.config(directory, {"parallel-search": self.remote(timeout=0.15)})]
                with patch.object(mcp_bridge, "_MCP_BRIDGE", bridge), patch.object(
                    mcp_bridge.httpx, "AsyncClient", side_effect=wire.client
                ), patch.object(tools, "handle_json_request", side_effect=AssertionError("No fallback")):
                    try:
                        await tools.initialize_tool_runtime()
                        wire.failure = "slow" if failure == "cancel" else failure
                        if failure == "cancel":
                            task = asyncio.create_task(tools.execute_tool_async(REMOTE_SEARCH, {"objective": "asyncio", "search_queries": ["Python asyncio documentation"]}))
                            await asyncio.wait_for(wire.called.wait(), 1)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                            # Cancellation must leave the owner's next request usable.
                            wire.failure = None
                            recovered = await tools.execute_tool_async(REMOTE_FETCH, {"urls": [SOURCE_URL]})
                            self.assertTrue(recovered["success"], recovered)
                        else:
                            result = await tools.execute_tool_async(REMOTE_SEARCH, {"objective": "asyncio", "search_queries": ["Python asyncio documentation"]})
                            self.assertFalse(result["success"], result)
                            self.assertTrue(result["error"])
                        self.assertEqual(len(wire.calls()), 2 if failure == "cancel" else 1)
                    finally:
                        await bridge.shutdown()

    async def test_http_initialization_failure_keeps_local_tools_available(self):
        wire = MCPWire()
        wire.failure = "initial_http"
        bridge = mcp_bridge.MCPToolBridge()
        with tempfile.TemporaryDirectory() as directory:
            bridge._config_candidates = [self.config(directory, {"parallel-search": self.remote(timeout=1)})]
            with patch.object(mcp_bridge.httpx, "AsyncClient", side_effect=wire.client):
                try:
                    await bridge.ensure_started()
                    self.assertIn("parallel-search", bridge.get_server_errors())
                    self.assertFalse(bridge.has_tool(REMOTE_SEARCH))
                    self.assertTrue(bridge.has_tool("web_search"))
                    self.assertEqual(len(wire.requests), 1)
                finally:
                    await bridge.shutdown()

    async def test_shutdown_with_cancelled_http_request_finishes(self):
        wire = MCPWire()
        bridge = mcp_bridge.MCPToolBridge()
        with tempfile.TemporaryDirectory() as directory:
            bridge._config_candidates = [self.config(directory, {"parallel-search": self.remote()})]
            with patch.object(mcp_bridge.httpx, "AsyncClient", side_effect=wire.client):
                try:
                    await bridge.ensure_started()
                    session = bridge._bindings[REMOTE_SEARCH].session
                    wire.failure = "slow"
                    task = asyncio.create_task(bridge.call_tool(REMOTE_SEARCH, {
                        "objective": "asyncio", "search_queries": ["Python asyncio documentation"]}))
                    await asyncio.wait_for(wire.called.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    # The HTTP owner can close even if request cancellation and
                    # shutdown overlap. Its SDK scopes stay in their owner task.
                    await asyncio.wait_for(session.close(), 1)
                    self.assertTrue(session._task.done())
                finally:
                    await bridge.shutdown()

    async def test_invalid_or_disabled_remote_never_sends_requests(self):
        bridge = mcp_bridge.MCPToolBridge()
        cases = {
            "disabled": self.remote(disabled=True),
            "scheme": self.remote(url="file:///etc/passwd"),
            "credentials": self.remote(url="https://user:password@example.com/mcp"),
            "timeout": self.remote(timeout=-1),
            "headers": self.remote(headers={"Authorization": 42}),
            "transport": {"type": "sse", "url": ENDPOINT},
        }
        with tempfile.TemporaryDirectory() as directory:
            bridge._config_candidates = [self.config(directory, cases)]
            with patch.object(mcp_bridge.httpx, "AsyncClient", side_effect=AssertionError("Unexpected HTTP")):
                try:
                    await bridge.ensure_started()
                    self.assertEqual(set(bridge.get_server_errors()), set(cases) - {"disabled"})
                    self.assertTrue(bridge.has_tool("web_search"))
                finally:
                    await bridge.shutdown()

    async def test_scripted_react_loop_consumes_remote_url_results(self):
        from backend.app.services import react_loop

        wire = MCPWire()
        bridge = mcp_bridge.MCPToolBridge()
        testcase = self

        class ScriptedModel:
            calls = 0

            async def stream_chat(self, messages, tools, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    testcase.assertIn(REMOTE_SEARCH, [t["function"]["name"] for t in tools])
                    content = '<tool_call>' + json.dumps({"tool": REMOTE_SEARCH,
                        "arguments": {"objective": "Python asyncio documentation", "search_queries": ["Python asyncio documentation"]}}) + '</tool_call>'
                elif self.calls == 2:
                    testcase.assertIn(SOURCE_URL, json.dumps(messages))
                    content = '<tool_call>' + json.dumps({"tool": REMOTE_FETCH,
                        "arguments": {"urls": [SOURCE_URL]}}) + '</tool_call>'
                else:
                    testcase.assertIn(SOURCE_URL, json.dumps(messages))
                    content = f"Read the asyncio documentation: {SOURCE_URL}"
                yield {"type": "token", "content": content}

        with tempfile.TemporaryDirectory() as directory:
            bridge._config_candidates = [self.config(directory, {"parallel-search": self.remote()})]
            model = ScriptedModel()
            with patch.object(mcp_bridge, "_MCP_BRIDGE", bridge), patch.object(
                mcp_bridge.httpx, "AsyncClient", side_effect=wire.client
            ), patch.object(react_loop, "get_client", return_value=model):
                try:
                    events = [event async for event in react_loop.ReActAgent().run(
                        [{"role": "user", "content": "Use Parallel to find and read Python asyncio documentation."}])]
                    results = [e["content"]["result"] for e in events if e["type"] == "tool_result"]
                    self.assertEqual(len(results), 2)
                    self.assertTrue(all(r["success"] for r in results))
                    self.assertEqual(model.calls, 3)
                    self.assertIn(SOURCE_URL, "".join(str(e["content"]) for e in events if e["type"] == "token"))
                finally:
                    await bridge.shutdown()


if __name__ == "__main__":
    unittest.main()
