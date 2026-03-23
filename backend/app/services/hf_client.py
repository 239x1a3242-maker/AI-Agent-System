"""
HuggingFace Space client for remote LLM inference.
Handles streaming communication with Nanbeige4.1-3B model.
"""

import os
import json
import asyncio
import aiohttp
import time
from typing import AsyncGenerator, Dict, Any, List, Optional, Callable
import logging

logger = logging.getLogger(__name__)


def trace_log(scope: str, message: str, *args: Any) -> None:
    """Verbose trace log with file marker for live debugging."""
    if args:
        try:
            rendered = message % args
        except Exception:
            rendered = f"{message} | args={args!r}"
    else:
        rendered = message
    line = f"[hf_client.py::{scope}] {rendered}"
    print(line, flush=True)

# HuggingFace Space API configuration
HF_SPACE_URL = os.getenv('HF_SPACE_URL', 'https://your-text-worker.your-subdomain.workers.dev')
HF_API_TOKEN = os.getenv('HF_API_TOKEN', '')


class HFSpaceClient:
    """Client for HuggingFace Space inference API."""
    
    def __init__(self, space_url: Optional[str] = None, api_token: Optional[str] = None):
        self.space_url = (space_url or HF_SPACE_URL).rstrip('/')
        self.api_token = api_token or HF_API_TOKEN
        self.session: Optional[aiohttp.ClientSession] = None
        self._model_status = "unknown"
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self.session is None or self.session.closed:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'text/event-stream'
            }
            if self.api_token:
                headers['Authorization'] = f'Bearer {self.api_token}'
            
            self.session = aiohttp.ClientSession(headers=headers)
        return self.session
    
    async def close(self):
        """Close the client session."""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def check_health(self) -> Dict[str, Any]:
        """Check the health/status of the HF Space."""
        try:
            trace_log("health", "checking model health url=%s", self.space_url)
            session = await self._get_session()
            async with session.get(
                f"{self.space_url}/health",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    self._model_status = "loaded" if data.get('model_loaded') else "loading"
                    trace_log("health", "model status loaded=%s raw=%s", self._model_status, json.dumps(data))
                    return data
                else:
                    self._model_status = "error"
                    trace_log("health", "model status error http=%d", response.status)
                    return {"status": "error", "code": response.status}
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            self._model_status = "offline"
            trace_log("health", "health exception error=%s", str(e))
            return {"status": "offline", "error": str(e)}
    
    @property
    def model_status(self) -> str:
        """Get current model status."""
        return self._model_status

    def _parse_tool_arguments(self, value: Any) -> Dict[str, Any]:
        """Parse tool-call arguments into a dict."""
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {}
        return {}

    def _normalize_tool_call(self, candidate: Any) -> Optional[Dict[str, Any]]:
        """Normalize tool-call shapes to {'name': str, 'parameters': dict}."""
        if not isinstance(candidate, dict):
            return None

        name = candidate.get("name") or candidate.get("tool") or candidate.get("function_name")
        arguments = (
            candidate.get("parameters")
            or candidate.get("arguments")
            or candidate.get("args")
        )

        function_payload = candidate.get("function")
        if isinstance(function_payload, dict):
            name = name or function_payload.get("name")
            if arguments is None:
                arguments = (
                    function_payload.get("arguments")
                    or function_payload.get("parameters")
                    or function_payload.get("args")
                )

        if not isinstance(name, str) or not name.strip():
            return None

        return {
            "name": name.strip(),
            "parameters": self._parse_tool_arguments(arguments),
        }

    def _collect_tool_calls(
        self,
        value: Any,
        *,
        _seen: Optional[set[int]] = None,
        _depth: int = 0,
    ) -> List[Dict[str, Any]]:
        """Recursively collect normalized tool calls from response payloads."""
        if _depth > 6 or value is None:
            return []

        seen = _seen if _seen is not None else set()
        calls: List[Dict[str, Any]] = []

        if isinstance(value, list):
            for item in value:
                calls.extend(self._collect_tool_calls(item, _seen=seen, _depth=_depth + 1))
            return calls

        if not isinstance(value, dict):
            return calls

        obj_id = id(value)
        if obj_id in seen:
            return calls
        seen.add(obj_id)

        normalized = self._normalize_tool_call(value)
        if normalized:
            calls.append(normalized)

        tool_calls = value.get("tool_calls")
        if isinstance(tool_calls, list):
            for item in tool_calls:
                normalized_item = self._normalize_tool_call(item)
                if normalized_item:
                    calls.append(normalized_item)

        function_call = value.get("function_call")
        if isinstance(function_call, dict):
            normalized_fn = self._normalize_tool_call(function_call)
            if normalized_fn:
                calls.append(normalized_fn)

        for key in ("choices", "message", "delta", "output", "content", "response"):
            nested = value.get(key)
            if nested is not None:
                calls.extend(self._collect_tool_calls(nested, _seen=seen, _depth=_depth + 1))

        return calls

    def _extract_tool_call_payload(self, response_json: Any) -> str:
        """Extract a compact JSON tool-call payload from model response."""
        calls = self._collect_tool_calls(response_json)
        if not calls:
            return ""

        unique: List[Dict[str, Any]] = []
        signatures: set[str] = set()
        for call in calls:
            signature = json.dumps(call, ensure_ascii=False, sort_keys=True)
            if signature not in signatures:
                signatures.add(signature)
                unique.append(call)

        if not unique:
            return ""

        payload_obj: Any = unique[0] if len(unique) == 1 else unique
        return json.dumps(payload_obj, ensure_ascii=False)

    def _extract_text_payload(self, value: Any, *, _depth: int = 0) -> str:
        """Best-effort recursive extraction of plain text answer content."""
        if _depth > 8 or value is None:
            return ""

        if isinstance(value, str):
            stripped = value.strip()
            return stripped

        if isinstance(value, list):
            chunks: List[str] = []
            for item in value:
                text = self._extract_text_payload(item, _depth=_depth + 1)
                if text:
                    chunks.append(text)
            return "\n".join(chunks).strip()

        if not isinstance(value, dict):
            return ""

        direct_keys = ("content", "text", "response", "answer", "output", "message")
        for key in direct_keys:
            if key in value:
                text = self._extract_text_payload(value.get(key), _depth=_depth + 1)
                if text:
                    return text

        for key in ("choices", "delta", "data", "result"):
            if key in value:
                text = self._extract_text_payload(value.get(key), _depth=_depth + 1)
                if text:
                    return text

        return ""

    async def _recover_content_from_non_stream(
        self,
        session: aiohttp.ClientSession,
        payload: Dict[str, Any],
        *,
        prefer_tool_payload: bool,
    ) -> str:
        """Fallback when streaming yields no content; recover tool payload or plain text."""
        fallback_payload = dict(payload)
        fallback_payload["stream"] = False

        try:
            async with session.post(
                f"{self.space_url}/chat",
                json=fallback_payload,
                timeout=aiohttp.ClientTimeout(total=90, sock_connect=30, sock_read=60),
            ) as fallback_response:
                if fallback_response.status != 200:
                    trace_log(
                        "stream_chat",
                        "tool fallback request failed http=%d",
                        fallback_response.status,
                    )
                    return ""

                try:
                    fallback_json = await fallback_response.json(content_type=None)
                except Exception:
                    fallback_text = await fallback_response.text()
                    trace_log(
                        "stream_chat",
                        "tool fallback json parse failed text_preview=%s",
                        fallback_text[:220].replace("\n", "\\n"),
                    )
                    return ""

                if prefer_tool_payload:
                    payload_text = self._extract_tool_call_payload(fallback_json)
                    if payload_text:
                        trace_log(
                            "stream_chat",
                            "tool fallback extracted payload preview=%s",
                            payload_text[:220].replace("\n", "\\n"),
                        )
                        return payload_text

                text_payload = self._extract_text_payload(fallback_json)
                if text_payload:
                    trace_log(
                        "stream_chat",
                        "text fallback extracted preview=%s",
                        text_payload[:220].replace("\n", "\\n"),
                    )
                    return text_payload

                # Try tool payload extraction last even when not preferred.
                payload_text = self._extract_tool_call_payload(fallback_json)
                if payload_text:
                    trace_log(
                        "stream_chat",
                        "tool fallback extracted payload preview=%s",
                        payload_text[:220].replace("\n", "\\n"),
                    )
                    return payload_text

                trace_log("stream_chat", "non-stream fallback found no recoverable payload")
                return ""
        except Exception as exc:
            trace_log("stream_chat", "tool fallback exception error=%s", str(exc))
            return ""
    
    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        max_tokens: int = 131072,  # Increased from 2048, supports up to 131072
        temperature: float = 0.6,
        retry_count: int = 3,
        stop_checker: Optional[Callable[[], bool]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream chat completion from HuggingFace Space.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of available tools
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            retry_count: Number of retries on failure
        
        Yields:
            Event dicts with 'type' and 'content' keys
        """
        session = await self._get_session()
        preview_chars = max(40, int(os.getenv("HF_CLIENT_LOG_PREVIEW_CHARS", "180")))
        preview_step = max(40, int(os.getenv("HF_CLIENT_LOG_PREVIEW_STEP", "120")))

        # Format messages for the HF Space API
        formatted_messages = []
        for msg in messages:
            formatted_messages.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", "")
            })
        
        payload = {
            "messages": formatted_messages,
            "stream": True,
            "max_completion_tokens": max_tokens,
            "temperature": temperature
        }
        trace_log(
            "stream_chat",
            "request prepared messages=%d tools=%d max_tokens=%d temp=%.2f",
            len(formatted_messages),
            len(tools or []),
            max_tokens,
            temperature,
        )
        
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            trace_log(
                "stream_chat",
                "tool-enabled request payload preview=%s",
                json.dumps(payload, ensure_ascii=False)[:280].replace("\n", "\\n"),
            )
        
        last_error = None
        
        for attempt in range(retry_count):
            try:
                if stop_checker and stop_checker():
                    trace_log("stream_chat", "stop requested before dispatch")
                    return

                trace_log("stream_chat", "query sent attempt=%d", attempt + 1)
                
                async with session.post(
                    f"{self.space_url}/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"HTTP {response.status}: {error_text}")
                        trace_log(
                            "stream_chat",
                            "request failed http=%d error=%s",
                            response.status,
                            error_text[:260].replace("\n", "\\n"),
                        )
                        raise Exception(f"HTTP {response.status}: {error_text}")
                    
                    # Process SSE stream
                    buffer = ""
                    parsed_event_count = 0
                    next_preview_at = preview_step
                    last_activity_ts = time.monotonic()
                    while True:
                        if stop_checker and stop_checker():
                            trace_log("stream_chat", "stop requested during stream")
                            return

                        if response.content.at_eof():
                            break

                        try:
                            chunk = await asyncio.wait_for(response.content.readany(), timeout=1.0)
                        except asyncio.TimeoutError:
                            # Keep waiting for long first-token latency.
                            continue

                        if not chunk:
                            await asyncio.sleep(0)
                            continue

                        try:
                            chunk_text = chunk.decode('utf-8', errors='ignore')
                        except:
                            continue
                        last_activity_ts = time.monotonic()
                        # Normalize CRLF -> LF so SSE boundary parsing remains stable.
                        buffer += chunk_text.replace('\r\n', '\n')
                        
                        # Process complete SSE events
                        while '\n\n' in buffer:
                            event_end = buffer.index('\n\n')
                            event_data = buffer[:event_end]
                            buffer = buffer[event_end + 2:]
                            
                            # Parse SSE event
                            event_lines = event_data.split('\n')
                            event_type = None
                            data_lines: List[str] = []
                            
                            for line in event_lines:
                                line = line.rstrip('\r')
                                if line.startswith('event:'):
                                    event_type = line[6:].strip()
                                elif line.startswith('data:'):
                                    value = line[5:]
                                    if value.startswith(' '):
                                        value = value[1:]
                                    data_lines.append(value)

                            data_content = "\n".join(data_lines)
                            
                            if data_content != "":
                                try:
                                    parsed = json.loads(data_content)
                                    if (
                                        str(parsed.get("type", "")).lower() == "error"
                                        and "no content generated"
                                        in str(parsed.get("content", "")).lower()
                                    ):
                                        recovered_payload = await self._recover_content_from_non_stream(
                                            session,
                                            payload,
                                            prefer_tool_payload=bool(tools),
                                        )
                                        if recovered_payload:
                                            yield {"type": "token", "content": recovered_payload}
                                            yield {"type": "done", "content": ""}
                                            return
                                    parsed_event_count += 1
                                    if parsed_event_count == 1:
                                        trace_log(
                                            "stream_chat",
                                            "model stream first_event type=%s preview=%s",
                                            str(parsed.get("type", event_type or "token")),
                                            str(parsed.get("content", ""))[:preview_chars].replace("\n", "\\n"),
                                        )
                                    elif parsed_event_count >= next_preview_at:
                                        trace_log(
                                            "stream_chat",
                                            "stream preview events=%d type=%s preview=%s",
                                            parsed_event_count,
                                            str(parsed.get("type", event_type or "token")),
                                            str(parsed.get("content", ""))[:preview_chars].replace("\n", "\\n"),
                                        )
                                        next_preview_at += preview_step
                                    yield parsed
                                except json.JSONDecodeError:
                                    # Handle plain text data
                                    parsed_event_count += 1
                                    if parsed_event_count == 1:
                                        trace_log(
                                            "stream_chat",
                                            "plain first_event type=%s preview=%s",
                                            event_type or "token",
                                            data_content[:preview_chars].replace("\n", "\\n"),
                                        )
                                    yield {"type": event_type or "token", "content": data_content}
                            elif event_type:
                                # Preserve explicit no-content events like done.
                                parsed_event_count += 1
                                yield {"type": event_type, "content": ""}
                    
                    # Process remaining buffer
                    if buffer.strip():
                        lines = buffer.strip().split('\n')
                        parsed_any_remaining = False
                        for line in lines:
                            normalized_line = line.rstrip('\r')
                            if normalized_line.startswith('data:'):
                                data_content = normalized_line[5:]
                                if data_content.startswith(' '):
                                    data_content = data_content[1:]
                                try:
                                    parsed = json.loads(data_content)
                                    yield parsed
                                    parsed_any_remaining = True
                                    parsed_event_count += 1
                                except json.JSONDecodeError:
                                    yield {"type": "token", "content": data_content}
                                    parsed_any_remaining = True
                                    parsed_event_count += 1
                        if not parsed_any_remaining:
                            trace_log(
                                "stream_chat",
                                "remaining buffer not parsed preview=%s",
                                buffer[:220].replace('\n', '\\n'),
                            )

                    if parsed_event_count == 0:
                        trace_log(
                            "stream_chat",
                            "stream empty idle_for=%.2f",
                            time.monotonic() - last_activity_ts,
                        )
                        yield {
                            "type": "error",
                            "content": "No stream data received from model server.",
                        }
                        return

                    trace_log(
                        "stream_chat",
                        "stream completed attempt=%d events=%d",
                        attempt + 1,
                        parsed_event_count,
                    )
                    return
            
            except asyncio.TimeoutError:
                if stop_checker and stop_checker():
                    trace_log("stream_chat", "stop requested after timeout")
                    return
                last_error = "Request timeout"
                trace_log("stream_chat", "timeout attempt=%d", attempt + 1)
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
            
            except Exception as e:
                if stop_checker and stop_checker():
                    trace_log("stream_chat", "stop requested after stream error")
                    return
                last_error = str(e)
                trace_log("stream_chat", "attempt failed attempt=%d error=%s", attempt + 1, str(e))
                await asyncio.sleep(2 ** attempt)
        
        # All retries exhausted
        trace_log("stream_chat", "all retries exhausted last_error=%s", str(last_error))
        yield {"type": "error", "content": f"Failed after {retry_count} attempts: {last_error}"}
    
    async def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        max_tokens: int = 131072,  # Increased from 2048
        temperature: float = 0.6
    ) -> Dict[str, Any]:
        """
        Non-streaming chat completion.
        
        Returns:
            Complete response dict
        """
        content_parts = []
        tool_calls = None
        
        async for event in self.stream_chat(messages, tools, max_tokens, temperature):
            if event.get("type") == "token":
                content_parts.append(event.get("content", ""))
            elif event.get("type") == "done":
                tool_calls = event.get("tool_calls")
                break
            elif event.get("type") == "error":
                return {"error": event.get("content")}
        
        return {
            "content": "".join(content_parts),
            "tool_calls": tool_calls
        }


# Global client instance
_client: Optional[HFSpaceClient] = None


def get_client() -> HFSpaceClient:
    """Get global HF Space client instance."""
    global _client
    if _client is None:
        # Read the current HF_SPACE_URL from environment (allows runtime changes)
        current_url = os.getenv('HF_SPACE_URL', 'https://your-text-worker.your-subdomain.workers.dev')
        _client = HFSpaceClient(space_url=current_url)
    return _client


async def close_client():
    """Close global client."""
    global _client
    if _client:
        await _client.close()
        _client = None


async def get_model_status() -> str:
    """Get current model status."""
    client = get_client()
    await client.check_health()
    return client.model_status
