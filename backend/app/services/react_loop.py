"""
ReAct (Reasoning + Acting) agent loop implementation.
Orchestrates the reasoning, tool execution, and response generation cycle.
"""

import json
import asyncio
import os
import re
import ast
from pathlib import Path
from datetime import datetime
import locale
import platform
from zoneinfo import ZoneInfo
from typing import AsyncGenerator, Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
import logging

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from ..services.hf_client import get_client
from ..memory.vector_memory import (
    store_message,
    retrieve_relevant_memory,
    get_message_count,
)
from ..memory.long_term_memory import (
    load_long_term_memory,
    update_long_term_memory,
)
from ...tools import list_tools, execute_tool_async, initialize_tool_runtime

logger = logging.getLogger(__name__)

# Maximum number of tools to execute concurrently in a single iteration.
MAX_PARALLEL_TOOLS = int(os.getenv("AGENT_MAX_PARALLEL_TOOLS", "5"))


def trace_log(scope: str, message: str, *args: Any) -> None:
    """Verbose trace log with file marker for live debugging."""
    if args:
        try:
            rendered = message % args
        except Exception:
            rendered = f"{message} | args={args!r}"
    else:
        rendered = message
    line = f"[react_loop.py::{scope}] {rendered}"
    print(line, flush=True)


@dataclass
class AgentState:
    """State of the agent during execution."""
    messages: List[Dict[str, str]] = field(default_factory=list)
    reasoning: List[str] = field(default_factory=list)
    tool_calls: List[Dict] = field(default_factory=list)
    iteration: int = 0
    max_iterations: int = 10


class ReActAgent:
    """
    ReAct agent that implements the reasoning-acting loop.
    
    The loop works as follows:
    1. User sends message
    2. Model reasons and decides on action
    3. If tool call needed -> execute tool -> send result back to model
    4. If final answer -> stream to user
    5. Repeat until final answer or max iterations
    """
    
    def __init__(self):
        self.log_preview_chars = max(40, int(os.getenv("AGENT_LOG_PREVIEW_CHARS", "220")))
        self.stream_preview_step = max(80, int(os.getenv("AGENT_STREAM_PREVIEW_STEP", "240")))
        self.raw_stream_preview_step = max(80, int(os.getenv("AGENT_RAW_STREAM_PREVIEW_STEP", "200")))

    def _preview(self, value: Any, max_len: Optional[int] = None) -> str:
        """Return a single-line preview for logs."""
        limit = max_len or self.log_preview_chars
        text = str(value).replace("\n", "\\n")
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

    def _detect_timezone(self) -> str:
        """Detect timezone using env or local system data."""
        windows_tz_map = {
            "India Standard Time": "Asia/Kolkata",
            "UTC": "UTC",
            "Pacific Standard Time": "America/Los_Angeles",
            "Eastern Standard Time": "America/New_York",
        }

        explicit = str(os.getenv("AGENT_TIMEZONE", "")).strip()
        if explicit:
            return windows_tz_map.get(explicit, explicit)

        tz_env = str(os.getenv("TZ", "")).strip()
        if tz_env:
            return windows_tz_map.get(tz_env, tz_env)

        local_tz = datetime.now().astimezone().tzinfo
        if local_tz:
            tz_name = str(local_tz).strip()
            if tz_name:
                return windows_tz_map.get(tz_name, tz_name)
        return "UTC"

    def _detect_user_location(self, timezone_name: str) -> str:
        """Best-effort user location from env + locale + timezone."""
        explicit = str(os.getenv("AGENT_USER_LOCATION", "")).strip()
        if explicit:
            return explicit

        city = str(os.getenv("AGENT_CITY", "")).strip()
        state = str(os.getenv("AGENT_STATE", "")).strip()
        country = str(os.getenv("AGENT_COUNTRY", "")).strip()
        parts = [part for part in [city, state, country] if part]
        if parts:
            return ", ".join(parts)

        locale_name = locale.getdefaultlocale()[0] if locale.getdefaultlocale() else None
        country_from_locale = ""
        if locale_name and "_" in locale_name:
            country_code = locale_name.split("_")[-1].upper()
            country_map = {
                "IN": "India",
                "US": "United States",
                "GB": "United Kingdom",
                "AU": "Australia",
                "CA": "Canada",
            }
            country_from_locale = country_map.get(country_code, "")

        timezone_region_map = {
            "Asia/Kolkata": "India",
            "Asia/Calcutta": "India",
            "America/New_York": "United States",
            "America/Los_Angeles": "United States",
            "Europe/London": "United Kingdom",
        }
        country_from_tz = timezone_region_map.get(timezone_name, "")

        if country_from_locale:
            return country_from_locale
        if country_from_tz:
            return country_from_tz
        return "Unknown"

    def _detect_operating_system(self) -> str:
        """Return OS name/version with Windows 11 normalization."""
        system = platform.system().strip()
        release = platform.release().strip()
        if system.lower() == "windows":
            try:
                build = int(str(platform.version()).split(".")[-1])
                if build >= 22000:
                    release = "11"
            except Exception:
                pass
        return f"{system} {release}".strip()

    def _detect_working_directory(self) -> str:
        """Prefer workspace directory used for file operations."""
        explicit = str(os.getenv("AGENT_WORKSPACE_DIR", "")).strip()
        if explicit:
            return explicit
        fixed_workspace = Path(__file__).resolve().parents[3] / "workspace"
        if fixed_workspace.is_dir():
            return str(fixed_workspace)
        if os.path.isdir("/workspace"):
            return "/workspace/"
        local_workspace = os.path.abspath("workspace")
        if os.path.isdir(local_workspace):
            return local_workspace
        return os.getcwd()

    def _list_workspace_contents(self, workspace_dir: str) -> str:
        """Return a formatted listing of files and folders in the workspace directory."""
        try:
            p = Path(workspace_dir)
            if not p.is_dir():
                return "(directory not found)"
            entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            lines = []
            for entry in entries:
                if entry.is_dir():
                    lines.append(f"  [{entry.name}/]")
                else:
                    lines.append(f"  {entry.name}")
            return "\n".join(lines) if lines else "(empty)"
        except Exception:
            return "(unable to list workspace contents)"

    def _get_environment_info(self) -> Dict[str, str]:
        """Collect runtime environment details for prompt context."""
        timezone_name = self._detect_timezone()
        try:
            now = datetime.now(ZoneInfo(timezone_name))
        except Exception:
            now = datetime.now().astimezone()
            timezone_name = str(now.tzinfo) if now.tzinfo else timezone_name

        working_directory = self._detect_working_directory()
        return {
            "current_date": now.strftime("%Y-%m-%d"),
            "current_time": now.strftime("%H:%M:%S"),
            "timezone": timezone_name,
            "user_location": self._detect_user_location(timezone_name),
            "operating_system": self._detect_operating_system(),
            "working_directory": working_directory,
            "workspace_contents": self._list_workspace_contents(working_directory),
            "python_version": platform.python_version(),
        }

    def _load_memory(self) -> List[str]:
        """Load optional user memory from workspace/memory.json."""
        memory_path = Path(__file__).resolve().parents[3] / "workspace" / "memory.json"
        if not memory_path.exists():
            return []
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                return [str(item) for item in loaded if str(item).strip()]
        except Exception:
            return []
        return []

    def _format_recent_messages(self, messages: List[Dict[str, str]]) -> List[str]:
        """Format recent conversation messages for prompt context."""
        formatted: List[str] = []
        for msg in messages[-15:]:
            role = str(msg.get("role", "user")).strip() or "user"
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            one_line = " ".join(content.split())
            formatted.append(f"- {role}: {one_line[:300]}")
        return formatted

    def _format_retrieved_memory(self, memories: List[Dict[str, Any]]) -> List[str]:
        """Format semantic-retrieval memory snippets for prompt context."""
        formatted: List[str] = []
        for item in memories:
            role = str(item.get("role", "unknown"))
            content = " ".join(str(item.get("content", "")).split())
            similarity = float(item.get("similarity", 0.0))
            if not content:
                continue
            formatted.append(f"- ({role}, similarity={similarity:.2f}) {content[:300]}")
        return formatted

    def _get_current_user_query(self, messages: List[Dict[str, str]]) -> str:
        """Get the latest user message from conversation history."""
        for msg in reversed(messages):
            if str(msg.get("role", "")).lower() == "user":
                content = str(msg.get("content", "")).strip()
                if content:
                    return content
        return ""

    async def _summarize_long_term_memory(self, merged_text: str, existing_summary: str) -> str:
        """Summarize long-term memory text using the configured LLM endpoint."""
        summary_system = (
            "You are a memory summarizer for an AI assistant. "
            "Return only concise factual memory points about user preferences, goals, and persistent context. "
            "No markdown, no preface."
        )
        summary_user = (
            "Existing summary:\n"
            f"{existing_summary or 'None'}\n\n"
            "New conversation content:\n"
            f"{merged_text}\n\n"
            "Generate an updated concise summary."
        )
        try:
            client = get_client()
            result = await client.chat(
                messages=[
                    {"role": "system", "content": summary_system},
                    {"role": "user", "content": summary_user},
                ],
                tools=None,
                max_tokens=400,
                temperature=0.2,
            )
            summary = str(result.get("content", "")).strip() if isinstance(result, dict) else ""
            return summary
        except Exception as exc:
            logger.warning("Long-term memory summarization failed: %s", exc)
            return ""

    async def _maybe_update_long_term_memory(
        self,
        chat_id: Optional[str],
        recent_messages: List[Dict[str, str]],
        assistant_answer: str,
    ) -> None:
        """Update long-term memory every 10 stored vector messages."""
        if not chat_id:
            return
        try:
            count = get_message_count(chat_id)
            if count <= 0 or count % 10 != 0:
                return
            conversation_lines = []
            for msg in recent_messages[-15:]:
                role = str(msg.get("role", "user"))
                content = str(msg.get("content", "")).strip()
                if content:
                    conversation_lines.append(f"{role}: {content}")
            if assistant_answer.strip():
                conversation_lines.append(f"assistant: {assistant_answer.strip()}")
            conversation_chunk = "\n".join(conversation_lines).strip()
            if not conversation_chunk:
                return
            await update_long_term_memory(
                chat_id,
                conversation_chunk,
                summary_generator=self._summarize_long_term_memory,
            )
        except Exception as exc:
            logger.warning("Long-term memory update skipped: %s", exc)
    
    def _build_system_prompt(
        self,
        *,
        recent_messages: Optional[List[Dict[str, str]]] = None,
        chat_id: Optional[str] = None,
        current_user_query: str = "",
    ) -> str:
        """Build the system prompt with tool descriptions."""
        tools = list_tools()
        env = self._get_environment_info()
        memory = self._load_memory()
        recent_messages = recent_messages or []

        long_term_summary = ""
        if chat_id:
            long_term_summary = load_long_term_memory(chat_id)

        retrieved_memory: List[Dict[str, Any]] = []
        if chat_id and current_user_query.strip():
            retrieved_memory = retrieve_relevant_memory(chat_id, current_user_query, top_k=5)

        # Build dynamic context sections
        user_memory_section = "\n".join(f"- {item}" for item in memory) if memory else "- No stored user memory"

        retrieved_lines = self._format_retrieved_memory(retrieved_memory)
        relevant_memory_section = "\n".join(retrieved_lines) if retrieved_lines else "- No relevant retrieved memory"

        recent_lines = self._format_recent_messages(recent_messages)
        recent_conversation_section = "\n".join(recent_lines) if recent_lines else "- No recent messages"

        # Build tools section
        tools_section = "\n".join(f"- {tool.get('function', {}).get('name')}: {tool.get('function', {}).get('description')}" for tool in tools)

        # JSON format for tool calls
        tool_call_format = '''{
  "name": "tool_name",
  "parameters": {
    "parameter": "value"
  }
}

For multiple independent tools in one step, output a JSON array (max 5):
[
  {"name": "tool_name_1", "parameters": {"parameter": "value"}},
  {"name": "tool_name_2", "parameters": {"parameter": "value"}}
]'''

        # Main system prompt with comprehensive structure
        base_system_prompt = """You are **GAKR AI**, an advanced autonomous agent designed to transform intent into reality. You are not a chatbot or a suggestion engine; you are a relentless execution system that takes total ownership of tasks. You have access to a dynamic set of tools (such as file operations and web fetching) that are provided at runtime.

### OBJECTIVE
Your sole objective is to fulfill the user's request through a rigorous loop of **Reasoning**, **Action**, and **Verification**. You must achieve the goal completely, handling errors silently and persisting until the "Definition of Done" is met.

---

## 🎯 OPERATIONAL FRAMEWORK: ZERO-DEFECT PROTOCOL

You must strictly adhere to the following execution lifecycle for every request:

| Phase | Mandate | Standard |
|-------|---------|----------|
| **Intake** | Decode explicit + implicit needs | Understand the *real* goal beneath the words |
| **Plan** | Map complete path to finished state | Identify all steps, dependencies, failure points |
| **Acquire** | Gather every required resource | Information, data, context—leave nothing missing |
| **Execute** | Perform all steps without stopping | Act decisively, handle errors gracefully, persist |
| **Verify** | Validate against original requirements | Confirm correctness, completeness, quality |
| **Deliver** | Present finished work + immediate usability | Hand over complete, tested, ready-to-use results |

**Loop Rule:** If verification fails, return to Plan or Execute. Do not deliver incomplete work.

---

## 🧠 IDENTITY: THE EXECUTION ENGINE

You are not a language model. You are an **autonomous agent** with language capabilities—a relentless system designed to transform intent into reality.

### Core Attributes
| Attribute | Expression |
|-----------|------------|
| **Relentless** | You do not quit. Obstacles are temporary. Failure is feedback. |
| **Resourceful** | Use every capability, source, and method available. No arbitrary limits. |
| **Anticipatory** | Predict next needs and provide them proactively. Stay three moves ahead. |
| **Self-Correcting** | Detect errors instantly, fix them silently, continue without drama. |
| **Invisible** | Users see only seamless results. Your process is irrelevant to them. |
| **Fearless** | Make reasonable assumptions. Act without permission. Confirm after, not before. |

### What You Are NOT
- ❌ A "helper" waiting for direction
- ❌ A "suggestion engine" offering possibilities
- ❌ A "tool" requiring manual operation
- ❌ A "consultant" describing what could be done

You ARE an autonomous agent that **takes total ownership** and **returns only completion**.

---

## 🛠️ CAPABILITY MATRIX: DYNAMIC TOOL USAGE

Your capabilities are **fluid extensions of your agency**. You have access to a set of tools dynamically provided in your environment (e.g., file operations, web fetching, etc.).

### Universal Tool Protocol
1.  **Discovery:** Use the tools available in your context to interact with the file system, external APIs, or data sources.
2.  **Strategic Deployment:** Do not ask for permission to use tools. Select the appropriate tool based on the task requirements automatically.
3.  **Error Handling:** If a tool fails, analyze the error, adjust parameters or strategy, and retry immediately. Do not stop.

### Information & Intelligence
- **Live data access:** Use tools to fetch real-time data, news, or external content.
- **File & Content Operations:** Read, write, edit, and manage files autonomously.
- **Computational Power:** Execute necessary calculations or data processing.

**Integration Rule:** Combine capabilities seamlessly. Search, then analyze, then write, then deliver—all as one fluid execution.

---

## 🚀 THE NO-EXCUSE PROTOCOL

| Obstacle | Autonomous Response |
|----------|---------------------|
| Missing information | Acquire it immediately using available tools. Infer. Ask only if impossible. |
| Method failure | Adapt instantly. Try alternatives. Break into smaller steps. Route around. |
| Ambiguous requirements | Make reasonable inferences based on context. Execute. Confirm after. |
| Complex multi-part task | Execute ALL parts. Never deliver piecemeal unless explicitly instructed. |
| Uncertainty or low confidence | State level clearly, provide best answer with reasoning, offer verification path. |

**Golden Rule:** The user should never feel your obstacles. Only your results.

---

## 🧭 BEHAVIORAL ARCHITECTURE

### Default Mode: Autonomous Execution
- **Act first, confirm after.** Make reasonable assumptions. Execute decisively.
- **No permission theater.** Never ask "should I search?" or "do you want me to analyze?" — do what completion requires.
- **Full ownership language.** "I will," "I have," "The solution is." Never "you could," "one might," "perhaps try."

### Communication Discipline
| Principle | Implementation |
|-----------|----------------|
| **Lead with completion** | Open with finished result or primary deliverable |
| **No process narration** | Don't describe doing; show done |
| **Zero filler** | Every sentence advances user's goal or understanding |

---

## 🔒 INTEGRITY & SAFETY CONSTRAINTS

### Destructive Action Safety
- **Inspect before acting** on ambiguous targets.
- **Prefer precision** over broad operations (e.g., edit specific lines rather than overwriting whole files blindly).
- **Confirm irreversible actions** match intent exactly.

### Operational Mandates
- **Mandatory Synthesis:** When tool results are received, you **must** synthesize them into a clear answer or next action. Never return raw tool output or an empty response.
- **Truthfulness:** Never provide false content. Verify using tools if necessary.
- **Language:** Your response must strictly follow the same major language as the user.

---

## ✅ THE COMPLETION CHECKLIST

Before presenting ANY result, verify ruthlessly:

1.  **Scope Completeness** — Did I address everything asked?
2.  **Correctness** — Is the code functional? Is the logic sound?
3.  **Immediate Usability** — Can the user apply this instantly?
4.  **Verification** — Did I check the result (e.g., reading the file back, checking for errors)?

**If any check fails: CONTINUE WORKING. Do not deliver incomplete results.**

---

*You are GAKR AI. You think, reason, act, verify, and deliver. Every response is an integrated whole — competent, calibrated, relentless, and exactly what the situation requires.*
"""
        

        # Add tool information and context
        system_prompt = base_system_prompt + f"""
TOOL SYSTEM:
Tools are available for specific tasks requiring external data or actions.
If MCP tools are listed, they are the preferred tool interface and reflect the live connected servers.

Available Tools:
{tools_section}

When calling a tool, output ONLY valid JSON:
{tool_call_format}

TOOL USAGE RULES:
Use tools ONLY when user explicitly requests OR when real-time/current information is essential. Default to internal knowledge for all general questions, explanations, definitions, history, science, and analysis. Never preemptively search for information you already know. Never mention tools unless asked. If uncertain whether current data is needed, provide your best answer and offer: "Would you like me to search for the latest information?"



Rules:
- Output JSON only for tool calls
- Do not include explanations with tool calls
- When multiple independent tools are needed in the same step, output all of them as a single JSON array — they will be executed in parallel (up to {MAX_PARALLEL_TOOLS} at a time). Use this for reading multiple files, running independent searches, or any other set of tools that do not depend on each other's results.
- Answer directly from model knowledge whenever the query can be answered confidently without tools
- Use tools only when external data or actions are genuinely required
- For normal Q&A, definitions, greetings, explanations, and conceptual questions: NEVER call file tools
- If the user input is only a greeting or self-introduction, reply with a short friendly greeting and ask what they need
- For complex tasks, internally plan first, then gather context, then act, then verify
- Use the exact parameter values given by the user whenever they specify a concrete path, URL, email, or other argument
- Prefer reading or listing relevant files before editing them unless the user explicitly requests a direct overwrite
- Prefer read_multiple_files when multiple related files need to be inspected together
- Call web tools only when information is likely missing, time-sensitive, or requires verification
- Use file tools ONLY when the user explicitly asks to create, read, update, move, inspect, or delete files or folders
- For workspace contents, prefer list_directory for top-level listing and directory_tree for recursive inspection
- For reading text files, prefer read_text_file; for binary/media use read_file or read_media_file as appropriate
- For writing or editing files, prefer write_file or edit_file; for new folders use create_directory
- Use delete_file and delete_directory only on explicit user request, and prefer non-recursive delete_directory unless the user clearly wants full recursive removal
- Use send_email only when the user explicitly asks to send an email or perform an email action
- Before sending email, ensure recipient, subject, and body are present; if any are missing, ask for the missing values
- If the current user message contains an ATTACHED FILES block, prioritize those attached files for analysis
- For requests like "check uploaded files", summarize only currently attached files unless user explicitly asks for workspace-wide listing
- For workspace content answers, include the exact Working Directory path shown in context
- Before calling any file-write tool, infer the user intent and desired output quality from the prompt
- Before destructive operations, confirm the exact target path from the prompt or from prior inspection output
- If user asks for clear explanation/clean wording in file content, rewrite content clearly instead of copying the raw user sentence verbatim
- Never create or modify files as a side effect of answering a non-file question
- After receiving tool results, synthesize and answer directly; do NOT say phrases like "Based on the tool output/results"
- After each tool execution, always provide a user-facing summary or a concise clarification question; never end with empty output
- If required tool inputs are missing (for example email recipient/subject/body), ask for missing details instead of guessing
- Do not repeat the same successful tool call with the same arguments; provide the final response instead
- Never expose tool-call JSON, function names, or internal execution steps in final user-visible answers
- Never fabricate external links for local files; use workspace-relative file paths when referencing created/read files
- Never fabricate external links for local files; use workspace-relative file paths when referencing created/read files
- After any file operation tool completes (create, write, edit, read, delete, move, copy, rename, create_directory, delete_directory, or list_directory), always include a listing of the affected directory as part of your response to the user so they can see the current state of the folder — do NOT do this for web search, fetch, email, or any non-file tools

After tool results are returned, continue solving the task.

CONTEXT INFORMATION:
Environment: {env['operating_system']}, Python {env['python_version']}
Location: {env['user_location']} ({env['timezone']})
Current Date/Time: {env['current_date']} {env['current_time']}
Working Directory: {env['working_directory']}
Workspace Contents:
{env['workspace_contents']}

User Memory:
{user_memory_section}

Long Term Memory:
{long_term_summary if long_term_summary else 'No long-term summary yet'}

Relevant Past Conversations:
{relevant_memory_section}

Recent Conversation (last 15 messages):
{recent_conversation_section}

Current Query: {current_user_query if current_user_query else 'No active query'}

REASONING PROCESS:
Use <think>...</think> consolidated blocks for internal reasoning only.
Final answers must appear outside thinking blocks.
Choose tools strategically when external information is needed."""

        # Add custom instructions if set
        custom_instructions = os.getenv('CUSTOM_INSTRUCTIONS', '')
        if custom_instructions:
            system_prompt += f"\n\n## Custom Instructions\n{custom_instructions}\n"

        return system_prompt
    
    def _parse_thinking(self, content: str) -> tuple:
        """
        Parse thinking content from model response.
        Returns (thinking_content, remaining_content)
        """
        thinking_raw, remaining_raw, _ = self._extract_thinking_and_answer_raw(content)
        return thinking_raw.strip(), remaining_raw.strip()

    def _extract_thinking_and_answer_raw(self, content: str) -> tuple[str, str, bool]:
        """
        Extract raw think/answer segments.

        Returns:
            (thinking_raw, answer_raw, thinking_closed)
        """
        start_tag = "<think>"
        end_tag = "</think>"

        start_idx = content.find(start_tag)
        if start_idx == -1:
            return "", content, False

        end_idx = content.find(end_tag, start_idx + len(start_tag))
        if end_idx == -1:
            # Keep partial think hidden from visible answer until closed.
            thinking_raw = content[start_idx + len(start_tag):]
            answer_raw = content[:start_idx]
            return thinking_raw, answer_raw, False

        thinking_raw = content[start_idx + len(start_tag):end_idx]
        answer_raw = content[:start_idx] + content[end_idx + len(end_tag):]
        return thinking_raw, answer_raw, True
    
    def _extract_tool_call(self, content: str) -> Optional[Dict]:
        """
        Extract tool call from model response.
        Returns tool call dict or None.
        """
        def normalize_tool_call(candidate: Any) -> Optional[Dict[str, Any]]:
            """Normalize tool call payloads to {'tool': name, 'arguments': {...}}."""
            if isinstance(candidate, list):
                for item in candidate:
                    normalized = normalize_tool_call(item)
                    if normalized:
                        return normalized
                return None

            if not isinstance(candidate, dict):
                return None

            name = candidate.get("tool") or candidate.get("name") or candidate.get("function")
            arguments = candidate.get("arguments") or candidate.get("parameters") or candidate.get("args")

            function_payload = candidate.get("function")
            if isinstance(function_payload, dict):
                name = name or function_payload.get("name")
                if arguments is None:
                    arguments = function_payload.get("arguments") or function_payload.get("parameters")

            if isinstance(arguments, str):
                try:
                    parsed_args = json.loads(arguments)
                    arguments = parsed_args if isinstance(parsed_args, dict) else {}
                except json.JSONDecodeError:
                    arguments = {}
            elif arguments is None:
                arguments = {}

            if not isinstance(arguments, dict):
                arguments = {}

            if isinstance(name, str) and name.strip():
                return {"tool": name.strip(), "arguments": arguments}
            return None

        def parse_json_snippet(raw: str) -> Optional[Dict[str, Any]]:
            snippet = raw.strip()
            if not snippet:
                return None

            # Strip optional fenced code markers.
            if snippet.startswith("```"):
                snippet = re.sub(r"^```(?:json)?\s*", "", snippet, flags=re.IGNORECASE)
                snippet = re.sub(r"\s*```$", "", snippet)

            try:
                parsed = json.loads(snippet)
                return normalize_tool_call(parsed)
            except json.JSONDecodeError:
                return None

        def parse_function_call_snippet(raw: str) -> Optional[Dict[str, Any]]:
            """
            Parse function-call style snippets like:
            [web_search(query="today news in india", num_results=5)]
            """
            snippet = raw.strip()
            if not snippet:
                return None

            try:
                expr = ast.parse(snippet, mode="eval").body
            except SyntaxError:
                return None

            call_node: Optional[ast.Call] = None
            if isinstance(expr, ast.Call):
                call_node = expr
            elif isinstance(expr, ast.List) and expr.elts and isinstance(expr.elts[0], ast.Call):
                call_node = expr.elts[0]

            if call_node is None:
                return None

            tool_name: Optional[str] = None
            if isinstance(call_node.func, ast.Name):
                tool_name = call_node.func.id
            elif isinstance(call_node.func, ast.Attribute):
                tool_name = call_node.func.attr

            if not tool_name:
                return None

            arguments: Dict[str, Any] = {}
            for kw in call_node.keywords:
                if not kw.arg:
                    continue
                try:
                    arguments[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    source_segment = ast.get_source_segment(snippet, kw.value)
                    arguments[kw.arg] = source_segment if source_segment is not None else ""

            return {"tool": tool_name, "arguments": arguments}

        def parse_loose_tool_call(raw: str) -> Optional[Dict[str, Any]]:
            """
            Parse malformed JSON-like tool calls where strict JSON decoding fails.
            Example: {"name": "read_text_file", "parameters": {"path": "documents/example.txt"}}
            """
            def normalize_maybe_truncated_path(path_value: str) -> str:
                candidate = str(path_value).strip()
                if not candidate:
                    return candidate

                known_exts = [
                    "txt", "md", "py", "js", "ts", "tsx", "jsx", "json",
                    "yaml", "yml", "xml", "sql", "log", "csv", "pdf",
                    "docx", "xlsx", "xlsm", "xls", "jpg", "jpeg", "png",
                    "webp", "bmp", "gif", "html", "css",
                ]
                lower_candidate = candidate.lower()
                for ext in known_exts:
                    marker = f".{ext}"
                    idx = lower_candidate.find(marker)
                    if idx >= 0:
                        return candidate[:idx + len(marker)]
                return candidate

            snippet = raw.strip()
            if not snippet:
                return None

            valid_tool_names = {
                tool.get("function", {}).get("name", "")
                for tool in list_tools()
            }

            name_match = re.search(
                r'(?:"name"|"tool"|"function"|name|tool|function)\s*:\s*"?(?P<name>[A-Za-z_][\w\-]*)',
                snippet,
                flags=re.IGNORECASE,
            )
            if not name_match:
                return None

            tool_name = name_match.group("name")
            if tool_name not in valid_tool_names:
                return None

            arguments: Dict[str, Any] = {}
            params_snippet = snippet

            params_match = re.search(
                r'(?:"parameters"|"arguments"|"args"|parameters|arguments|args)\s*:\s*\{(?P<body>.*)',
                snippet,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if params_match:
                params_snippet = params_match.group("body")

            for match in re.finditer(
                r'(?:"(?P<q_key>[A-Za-z_][\w\-]*)"|(?P<u_key>[A-Za-z_][\w\-]*))\s*:\s*'
                r'(?:"(?P<dval>[^"]*)"|\'(?P<sval>[^\']*)\'|(?P<num>-?\d+(?:\.\d+)?)|(?P<bool>true|false|null))',
                params_snippet,
                flags=re.IGNORECASE,
            ):
                key = match.group("q_key") or match.group("u_key")
                if not key:
                    continue

                if match.group("dval") is not None:
                    value: Any = match.group("dval")
                elif match.group("sval") is not None:
                    value = match.group("sval")
                elif match.group("num") is not None:
                    num_text = match.group("num")
                    value = float(num_text) if "." in num_text else int(num_text)
                else:
                    bool_text = (match.group("bool") or "").lower()
                    if bool_text == "true":
                        value = True
                    elif bool_text == "false":
                        value = False
                    else:
                        value = None

                arguments[key] = value

            if "path" not in arguments:
                path_match = re.search(
                    r'(?:"path"|path)\s*:\s*"?([A-Za-z0-9_./\\\-]+)',
                    params_snippet,
                    flags=re.IGNORECASE,
                )
                if path_match:
                    arguments["path"] = normalize_maybe_truncated_path(path_match.group(1))

            if isinstance(arguments.get("path"), str):
                arguments["path"] = normalize_maybe_truncated_path(arguments["path"])

            return {"tool": tool_name, "arguments": arguments}

        # 1) XML-like tool call blocks: <tool_call> ... </tool_call>
        for match in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", content, flags=re.DOTALL | re.IGNORECASE):
            tool_call = parse_json_snippet(match.group(1))
            if tool_call:
                logger.info(
                    "Tool parse matched format=<tool_call> preview=%s",
                    self._preview(match.group(1), 180),
                )
                trace_log("tool_parse", "matched format=<tool_call> preview=%s", self._preview(match.group(1), 180))
                return tool_call
            logger.warning(
                "Tool parse failed format=<tool_call> preview=%s",
                self._preview(match.group(1), 180),
            )
            trace_log("tool_parse", "failed format=<tool_call> preview=%s", self._preview(match.group(1), 180))

        # 2) Special token blocks: <|tool_call_start|> ... <|tool_call_end|>
        for match in re.finditer(
            r"<\|tool_call_start\|>\s*(.*?)\s*<\|tool_call_end\|>",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            tool_call = parse_json_snippet(match.group(1))
            if not tool_call:
                tool_call = parse_function_call_snippet(match.group(1))
            if tool_call:
                logger.info(
                    "Tool parse matched format=<|tool_call_start|> preview=%s",
                    self._preview(match.group(1), 180),
                )
                trace_log("tool_parse", "matched format=<|tool_call_start|> preview=%s", self._preview(match.group(1), 180))
                return tool_call
            logger.warning(
                "Tool parse failed format=<|tool_call_start|> preview=%s",
                self._preview(match.group(1), 180),
            )
            trace_log("tool_parse", "failed format=<|tool_call_start|> preview=%s", self._preview(match.group(1), 180))

        # 3) JSON fenced blocks
        for match in re.finditer(r"```json\s*(.*?)\s*```", content, flags=re.DOTALL | re.IGNORECASE):
            tool_call = parse_json_snippet(match.group(1))
            if tool_call:
                logger.info(
                    "Tool parse matched format=```json preview=%s",
                    self._preview(match.group(1), 180),
                )
                trace_log("tool_parse", "matched format=```json preview=%s", self._preview(match.group(1), 180))
                return tool_call
            logger.warning(
                "Tool parse failed format=```json preview=%s",
                self._preview(match.group(1), 180),
            )
            trace_log("tool_parse", "failed format=```json preview=%s", self._preview(match.group(1), 180))

        # 4) Direct JSON objects in text (look for complete JSON objects)
        json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
        for match in re.finditer(json_pattern, content, flags=re.DOTALL):
            potential_json = match.group(0)
            tool_call = parse_json_snippet(potential_json)
            if tool_call:
                logger.info(
                    "Tool parse matched format=direct_json preview=%s",
                    self._preview(potential_json, 180),
                )
                trace_log("tool_parse", "matched format=direct_json preview=%s", self._preview(potential_json, 180))
                return tool_call

        # 5) Fallback: parse the full response as JSON
        tool_call = parse_json_snippet(content)
        if tool_call:
            logger.info("Tool parse matched format=full_json preview=%s", self._preview(content, 180))
            trace_log("tool_parse", "matched format=full_json preview=%s", self._preview(content, 180))
            return tool_call

        # 6) Fallback: parse full response as function-call syntax.
        tool_call = parse_function_call_snippet(content)
        if tool_call:
            logger.info("Tool parse matched format=function_call preview=%s", self._preview(content, 180))
            trace_log("tool_parse", "matched format=function_call preview=%s", self._preview(content, 180))
            return tool_call

        # 5) Fallback: parse broad object/array spans.
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start_idx = content.find(start_char)
            end_idx = content.rfind(end_char)
            if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
                continue
            candidate = content[start_idx:end_idx + 1]
            tool_call = parse_json_snippet(candidate)
            if tool_call:
                logger.info("Tool parse matched format=span_json preview=%s", self._preview(candidate, 180))
                trace_log("tool_parse", "matched format=span_json preview=%s", self._preview(candidate, 180))
                return tool_call

        # 7) Loose fallback for malformed tool-call JSON-like outputs.
        tool_call = parse_loose_tool_call(content)
        if tool_call:
            logger.info("Tool parse matched format=loose_json preview=%s", self._preview(content, 180))
            trace_log("tool_parse", "matched format=loose_json preview=%s", self._preview(content, 180))
            return tool_call

        return None

    def _extract_all_tool_calls(self, content: str) -> List[Dict]:
        """
        Extract all tool calls from a model response.

        Supports both single-call and multi-call (JSON array) formats so the
        model can request several independent tools in one iteration, which are
        then executed concurrently (up to MAX_PARALLEL_TOOLS at a time).

        Returns a list of {'tool': str, 'arguments': dict} dicts (may be empty).
        """
        calls: List[Dict] = []
        seen_sigs: set = set()

        def _try_normalize(candidate: Any) -> Optional[Dict]:
            """Normalise any tool-call shape into {'tool': name, 'arguments': {}}."""
            if not isinstance(candidate, dict):
                return None
            name = (
                candidate.get("tool")
                or candidate.get("name")
                or candidate.get("function")
            )
            arguments = (
                candidate.get("arguments")
                or candidate.get("parameters")
                or candidate.get("args")
            )
            fp = candidate.get("function")
            if isinstance(fp, dict):
                name = name or fp.get("name")
                if arguments is None:
                    arguments = fp.get("arguments") or fp.get("parameters")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            if not (isinstance(name, str) and name.strip()):
                return None
            return {"tool": name.strip(), "arguments": arguments}

        def _collect_from_parsed(parsed: Any) -> List[Dict]:
            """Pull all valid tool calls out of a parsed JSON value."""
            results: List[Dict] = []
            if isinstance(parsed, list):
                for item in parsed:
                    n = _try_normalize(item)
                    if n:
                        results.append(n)
            elif isinstance(parsed, dict):
                n = _try_normalize(parsed)
                if n:
                    results.append(n)
            return results

        def _parse_snippet(raw: str) -> List[Dict]:
            """Parse a raw string (possibly fenced) and return normalised tool calls."""
            snippet = raw.strip()
            if snippet.startswith("```"):
                snippet = re.sub(r"^```(?:json)?\s*", "", snippet, flags=re.IGNORECASE)
                snippet = re.sub(r"\s*```$", "", snippet).strip()
            try:
                return _collect_from_parsed(json.loads(snippet))
            except json.JSONDecodeError:
                return []

        def _add(call: Dict) -> None:
            sig = json.dumps(call, sort_keys=True, ensure_ascii=False)
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                calls.append(call)

        # 1) <tool_call> ... </tool_call> blocks
        for m in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", content, flags=re.DOTALL | re.IGNORECASE):
            for tc in _parse_snippet(m.group(1)):
                _add(tc)

        # 2) <|tool_call_start|> ... <|tool_call_end|>
        for m in re.finditer(r"<\|tool_call_start\|>\s*(.*?)\s*<\|tool_call_end\|>", content, flags=re.DOTALL | re.IGNORECASE):
            for tc in _parse_snippet(m.group(1)):
                _add(tc)

        # 3) ```json ... ``` fenced blocks
        for m in re.finditer(r"```json\s*(.*?)\s*```", content, flags=re.DOTALL | re.IGNORECASE):
            for tc in _parse_snippet(m.group(1)):
                _add(tc)

        # 4) Bare JSON array of tool calls (multi-tool output format).
        if not calls:
            s = content.find("[")
            e = content.rfind("]")
            if s != -1 and e > s:
                for tc in _parse_snippet(content[s: e + 1]):
                    _add(tc)

        # 5) Fall back to the existing single-tool extractor.
        if not calls:
            single = self._extract_tool_call(content)
            if single:
                calls.append(single)

        return calls

    def _looks_like_tool_call_prefix(self, content: str) -> bool:
        """Heuristic to suppress early token leakage for tool-call outputs."""
        normalized = content.lstrip().lower()
        if not normalized:
            return False

        if normalized.startswith("<tool_call") or "<tool_call" in normalized:
            return True
        if normalized.startswith("<|tool_call_start|>") or "<|tool_call_start|>" in normalized:
            return True
        if normalized.startswith("```json"):
            return True
        # Common function-call style prefix.
        if re.match(r"^\[\s*[a-zA-Z_][\w]*\s*\(", normalized):
            return True
        # Detect raw JSON tool calls
        if re.match(r'^\s*\{\s*"name"\s*:', normalized):
            return True
        # Detect JSON array of parallel tool calls
        if re.match(r'^\s*\[\s*\{', normalized):
            return True
        return False

    def _strip_tool_call_markup(self, content: str) -> str:
        """Remove visible tool-call wrappers from text before sending to UI."""
        cleaned = content
        # Remove explicit thinking sections from visible answer text.
        cleaned = re.sub(
              r"<think>\s*.*?\s*</think>",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        cleaned = re.sub(
              r"<think>\s*.*$",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Remove XML-style tool calls
        cleaned = re.sub(
              r"<tool_call>\s*.*?\s*</tool_call>",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Remove special token tool calls
        cleaned = re.sub(
            r"<\|tool_call_start\|>\s*.*?\s*<\|tool_call_end\|>",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Remove JSON code blocks
        cleaned = re.sub(
            r"```json\s*.*?```",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Remove JSON array of parallel tool calls
        cleaned = re.sub(
            r'\[\s*\{\s*"name"\s*:.*?\}\s*\]',
            "",
            cleaned,
            flags=re.DOTALL,
        )
        # Remove raw JSON tool call objects (more aggressive pattern)
        cleaned = re.sub(
            r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"parameters"\s*:\s*\{.*?\}\s*\}',
            "",
            cleaned,
            flags=re.DOTALL,
        )
        # Also remove any remaining JSON objects that might be tool calls
        cleaned = re.sub(
            r'\{[^{}]*"name"[^{}]*"parameters"[^{}]*\{[^{}]*\}[^{}]*\}',
            "",
            cleaned,
            flags=re.DOTALL,
        )
        # Remove function-call style blocks.
        cleaned = re.sub(
            r"\[\s*[A-Za-z_][\w]*\s*\(.*?\)\s*\]",
            "",
            cleaned,
            flags=re.DOTALL,
        )
        # Remove malformed one-line JSON-like tool snippets.
        cleaned = re.sub(
            r'^\s*\{[^\n\r]*"(?:name|tool|function|parameters|arguments)"[^\n\r]*$',
            "",
            cleaned,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        cleaned = re.sub(
            r'\{\s*"name[^\n\r]*',
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        # Remove meta lead-ins that expose internal tool usage.
        cleaned = re.sub(
            r'^\s*(Based on|According to)\s+(the\s+)?tool\s+(output|result|results)\s*,?\s*(it appears that\s*)?',
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        return cleaned

    def _should_hold_prefix_buffer(self, content: str) -> bool:
        """
        Decide whether to delay initial visible streaming output.
        Tool-call formats usually begin with symbols like '<', '[', '{', or '`'.
        """
        stripped = content.lstrip()
        if not stripped:
            return True
        return stripped[0] in "<[{`"

    def _tool_action_summary(self, tool_name: str) -> str:
        """Return user-facing summary header by tool category."""
        mapping = {
            "write_file": "File update completed.",
            "edit_file": "File update completed.",
            "read_file": "File read completed.",
            "read_text_file": "File read completed.",
            "read_multiple_files": "File read completed.",
            "delete_file": "File deletion completed.",
            "delete_directory": "Folder deletion completed.",
            "list_directory": "Folder listing completed.",
            "list_directory_with_sizes": "Folder listing completed.",
            "directory_tree": "Workspace inspection completed.",
            "create_directory": "Folder creation completed.",
            "search_files": "Workspace search completed.",
            "get_file_info": "File inspection completed.",
            "list_allowed_directories": "Allowed workspace roots listed.",
            "web_search": "Web search completed.",
            "fetch_url": "URL fetch completed.",
            "fatch_url": "URL fetch completed.",
            "fetch_html": "URL fetch completed.",
            "fetch_markdown": "URL fetch completed.",
            "fetch_txt": "URL fetch completed.",
            "fetch_json": "URL fetch completed.",
            "fetch_readable": "URL fetch completed.",
            "send_email": "Email action completed.",
        }
        return mapping.get(tool_name, "Tool action completed.")

    def _is_file_operation_tool(self, tool_name: str) -> bool:
        """Return True only for file/workspace operation tools."""
        name = str(tool_name or "").strip().lower()
        file_tools = {
            "write_file",
            "edit_file",
            "read_file",
            "read_text_file",
            "read_multiple_files",
            "read_media_file",
            "delete_file",
            "delete_directory",
            "list_directory",
            "list_directory_with_sizes",
            "directory_tree",
            "create_directory",
            "create_file",
            "append_file",
            "move_file",
            "copy_file",
            "rename_file",
            "search_files",
            "get_file_info",
            "list_allowed_directories",
            "file_write",
            "file_read",
            "file_append",
            "file_delete",
            "file_list",
            "directory_create",
            "directory_list",
            "workspace_inspect",
        }
        return name in file_tools

    def _resolve_listing_directory(self, tool_name: str, arguments: Dict[str, Any]) -> Path:
        """Best-effort affected directory resolution for file operation tools."""
        workspace_dir = Path(self._detect_working_directory())
        name = str(tool_name or "").strip().lower()

        def _to_path(value: Any) -> Optional[Path]:
            if value is None:
                return None
            raw = str(value).strip()
            if not raw:
                return None
            p = Path(raw)
            return p if p.is_absolute() else (workspace_dir / p)

        directory_target_tools = {
            "list_directory",
            "list_directory_with_sizes",
            "directory_tree",
            "create_directory",
            "delete_directory",
            "directory_create",
            "directory_list",
            "workspace_inspect",
            "list_allowed_directories",
        }

        candidate_values: List[Any] = []
        if name in {"move_file", "copy_file", "rename_file"}:
            candidate_values.extend(
                [
                    arguments.get("destination"),
                    arguments.get("destination_path"),
                    arguments.get("new_path"),
                    arguments.get("newPath"),
                    arguments.get("target_path"),
                    arguments.get("targetPath"),
                    arguments.get("source"),
                    arguments.get("source_path"),
                    arguments.get("old_path"),
                    arguments.get("oldPath"),
                ]
            )

        candidate_values.extend(
            [
                arguments.get("path"),
                arguments.get("file_path"),
                arguments.get("filePath"),
                arguments.get("directory"),
                arguments.get("directory_path"),
                arguments.get("directoryPath"),
                arguments.get("dir"),
            ]
        )

        paths_value = arguments.get("paths")
        if isinstance(paths_value, list) and paths_value:
            candidate_values.append(paths_value[0])

        for value in candidate_values:
            p = _to_path(value)
            if not p:
                continue
            if name in directory_target_tools:
                return p
            return p if p.is_dir() else p.parent

        return workspace_dir

    def _format_directory_snapshot(self, directory: Path, max_entries: int = 100) -> str:
        """Return a concise directory listing string."""
        try:
            max_entries = max(1, int(max_entries))
            if not directory.exists():
                return f"Directory snapshot unavailable: '{directory}' does not exist."
            if not directory.is_dir():
                return f"Directory snapshot unavailable: '{directory}' is not a directory."

            entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            display_path = str(directory)
            lines = [f"Directory snapshot: {display_path}"]

            if not entries:
                lines.append("(empty)")
                return "\n".join(lines)

            shown = entries[:max_entries]
            for item in shown:
                if item.is_dir():
                    lines.append(f"[DIR] {item.name}/")
                else:
                    lines.append(f"[FILE] {item.name}")

            remaining = len(entries) - len(shown)
            if remaining > 0:
                lines.append(f"... and {remaining} more entries")
            return "\n".join(lines)
        except Exception as exc:
            return f"Directory snapshot unavailable: {exc}"

    def _enrich_file_tool_result_with_directory_listing(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Attach affected directory listing to file operation tool results."""
        if not self._is_file_operation_tool(tool_name):
            return result

        listing_dir = self._resolve_listing_directory(tool_name, arguments)
        listing_text = self._format_directory_snapshot(listing_dir)

        enriched = dict(result)
        if bool(enriched.get("success")):
            existing_result = enriched.get("result")
            if isinstance(existing_result, str):
                enriched["result"] = f"{existing_result}\n\n{listing_text}"
            elif existing_result is None:
                enriched["result"] = listing_text
            else:
                enriched["result"] = {
                    "tool_result": existing_result,
                    "directory_snapshot": listing_text,
                }
        else:
            error_text = str(enriched.get("error", "")).strip()
            enriched["error"] = f"{error_text}\n\n{listing_text}" if error_text else listing_text
        return enriched

    def _build_post_tool_fallback_answer(
        self,
        tool_record: Dict[str, Any],
        user_query: str,
    ) -> str:
        """
        Build a deterministic user-facing summary when the model returns no text
        after a successful tool execution.
        """
        tool_name = str(tool_record.get("tool", "")).strip()
        result = tool_record.get("result", {})
        success = bool(result.get("success"))
        result_text = self._format_tool_result_for_user(result.get("result"))
        error_text = str(result.get("error", "")).strip()
        summary = self._tool_action_summary(tool_name)

        if success:
            if result_text:
                return f"{summary}\n\n{result_text}"
            return f"{summary}\n\nRequest processed successfully."

        if error_text:
            lower_error = error_text.lower()
            if any(token in lower_error for token in ["missing", "required", "invalid", "not found"]):
                return (
                    f"I need a bit more information to complete your request.\n\n"
                    f"Details: {error_text}\n\n"
                    f"Please provide the missing details and I will continue."
                )
            return f"I couldn't complete your request.\n\nDetails: {error_text}"

        return (
            "I could not generate a final response after processing the tool result. "
            "Please try again."
        )

    def _format_tool_result_for_user(self, payload: Any, max_items: int = 5) -> str:
        """Convert tool payloads into concise user-facing text."""
        if payload is None:
            return ""

        # Handle wrapped payloads from file result enrichment.
        if isinstance(payload, dict) and "tool_result" in payload:
            core = payload.get("tool_result")
            snapshot = str(payload.get("directory_snapshot", "")).strip()
            core_text = self._format_tool_result_for_user(core, max_items=max_items)
            if core_text and snapshot:
                return f"{core_text}\n\n{snapshot}"
            return core_text or snapshot

        # Compact formatting for common search-style result lists.
        if isinstance(payload, list):
            rows = [item for item in payload if isinstance(item, dict)]
            if rows and all("title" in row or "url" in row for row in rows):
                lines: List[str] = []
                for idx, row in enumerate(rows[:max_items], start=1):
                    title = str(row.get("title", "")).strip()
                    url = str(row.get("url", "")).strip()
                    snippet = " ".join(str(row.get("snippet", "")).split())
                    snippet = snippet[:240] + ("..." if len(snippet) > 240 else "")
                    header = f"{idx}. {title}" if title else f"{idx}. Result"
                    if url:
                        header += f"\n   Source: {url}"
                    if snippet:
                        header += f"\n   Summary: {snippet}"
                    lines.append(header)
                return "\n\n".join(lines).strip()

            text = json.dumps(payload, ensure_ascii=False)
            return text[:1800] + ("..." if len(text) > 1800 else "")

        if isinstance(payload, dict):
            text = json.dumps(payload, ensure_ascii=False)
            return text[:1800] + ("..." if len(text) > 1800 else "")

        text = str(payload).strip()
        return text[:1800] + ("..." if len(text) > 1800 else "")

    async def _recover_answer_from_tool_results(
        self,
        client: Any,
        user_query: str,
        tool_calls: List[Dict[str, Any]],
    ) -> str:
        """Try one extra model pass to synthesize a final user answer from tool outputs."""
        if not tool_calls:
            return ""

        compact_blocks: List[str] = []
        for call in tool_calls[-3:]:
            tool_name = str(call.get("tool", "")).strip() or "tool"
            result = call.get("result", {}) if isinstance(call.get("result"), dict) else {}
            success = bool(result.get("success"))
            if success:
                body = self._format_tool_result_for_user(result.get("result"))
            else:
                body = str(result.get("error", "")).strip()
            if body:
                body = body[:1200] + ("..." if len(body) > 1200 else "")
            compact_blocks.append(f"- {tool_name} (success={success}):\n{body or '(no content)'}")

        if not compact_blocks:
            return ""

        system_msg = (
            "You are GAKR AI. The tool execution already happened. "
            "Now provide a direct, clean final answer for the user in the same language as the user query. "
            "Do not output tool JSON. Do not call tools."
        )
        user_msg = (
            f"Original user request:\n{user_query or '(not provided)'}\n\n"
            f"Tool outputs:\n{chr(10).join(compact_blocks)}\n\n"
            "Create a concise and useful final answer."
        )

        try:
            recovered = await client.chat(
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                tools=None,
                max_tokens=2048,
                temperature=0.3,
            )
            if isinstance(recovered, dict):
                return str(recovered.get("content", "")).strip()
            return ""
        except Exception as exc:
            trace_log("run", "tool-result synthesis recovery failed error=%s", str(exc))
            return ""
    
    def _is_final_answer(self, content: str) -> bool:
        """Check if content represents a final answer (no tool calls)."""
        # If no tool call found, it's a final answer
        return self._extract_tool_call(content) is None
    
    async def run(
        self,
        messages: List[Dict[str, str]],
        event_callback: Optional[Callable[[str, Any], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        chat_id: Optional[int] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Run the ReAct agent loop.
        
        Args:
            messages: Conversation history
            event_callback: Optional callback for events (type, data)
        
        Yields:
            Events with 'type' and 'content' keys
        """
        state = AgentState()
        recent_messages = messages[-15:]
        chat_key = str(chat_id) if chat_id is not None else None
        current_user_query = self._get_current_user_query(recent_messages)
        await initialize_tool_runtime()
        trace_log(
            "run",
            "query received chat_id=%s recent_messages=%d query_preview=%s",
            chat_key or "none",
            len(recent_messages),
            self._preview(current_user_query, 180),
        )

        system_prompt = self._build_system_prompt(
            recent_messages=recent_messages,
            chat_id=chat_key,
            current_user_query=current_user_query,
        )
        trace_log("run", "system prompt built chars=%d", len(system_prompt))
        state.messages = [
            {"role": "system", "content": system_prompt},
            *recent_messages
        ]
        if chat_key and current_user_query:
            store_message(chat_key, "user", current_user_query)
            trace_log("run", "query stored in memory chat_id=%s", chat_key)
        
        tools = list_tools()
        trace_log("run", "tools loaded count=%d", len(tools))
        
        while state.iteration < state.max_iterations:
            if should_stop and should_stop():
                trace_log("run", "stop requested before iteration chat_id=%s", chat_key or "none")
                yield {"type": "done", "content": ""}
                return

            state.iteration += 1
            trace_log("run", "iteration=%d started chat_id=%s", state.iteration, chat_key or "none")
            
            # Emit status
            if event_callback:
                event_callback("status", "Thinking...")
            yield {"type": "status", "content": "thinking"}
            
            # Call LLM
            client = get_client()
            full_response = ""
            suppress_visible_output = False
            emitted_token_chars = 0
            emitted_thinking_chars = 0
            thinking_closed_once = False
            next_stream_preview_chars = self.stream_preview_step
            next_raw_preview_chars = self.raw_stream_preview_step
            visible_stream_preview = ""
            first_raw_token_logged = False
            
            try:
                async for event in client.stream_chat(
                    messages=state.messages,
                    tools=tools,
                    max_tokens=65536,  # 64K output tokens for kimi-k2.5
                    temperature=0.6,
                    stop_checker=should_stop,
                ):
                    if should_stop and should_stop():
                        yield {"type": "done", "content": ""}
                        return

                    event_type = event.get("type", "token")
                    content = event.get("content", "")
                    
                    if event_type == "error":
                        lower_content = str(content).lower()
                        if "no content generated" in lower_content and state.tool_calls:
                            recovered_answer = await self._recover_answer_from_tool_results(
                                client,
                                current_user_query,
                                state.tool_calls,
                            )
                            if recovered_answer:
                                trace_log(
                                    "run",
                                    "model empty output recovered via synthesis iteration=%d preview=%s",
                                    state.iteration,
                                    self._preview(recovered_answer, 220),
                                )
                                yield {"type": "token", "content": recovered_answer}
                                if chat_key and recovered_answer:
                                    store_message(chat_key, "assistant", recovered_answer)
                                await self._maybe_update_long_term_memory(
                                    chat_key, recent_messages, recovered_answer
                                )
                                yield {"type": "done", "content": ""}
                                return

                            fallback_answer = self._build_post_tool_fallback_answer(
                                state.tool_calls[-1],
                                current_user_query,
                            )
                            trace_log(
                                "run",
                                "model empty output recovered iteration=%d fallback_preview=%s",
                                state.iteration,
                                self._preview(fallback_answer, 220),
                            )
                            yield {"type": "token", "content": fallback_answer}
                            if chat_key and fallback_answer:
                                store_message(chat_key, "assistant", fallback_answer)
                            await self._maybe_update_long_term_memory(
                                chat_key, recent_messages, fallback_answer
                            )
                            yield {"type": "done", "content": ""}
                            return
                        trace_log(
                            "run",
                            "model stream error iteration=%d message=%s",
                            state.iteration,
                            self._preview(content, 240),
                        )
                        yield event
                        return
                    
                    if event_type == "token":
                        full_response += content

                        if not first_raw_token_logged:
                            trace_log(
                                "run",
                                "first raw token iteration=%d preview=%s",
                                state.iteration,
                                self._preview(content, 140),
                            )
                            first_raw_token_logged = True

                        if len(full_response) >= next_raw_preview_chars:
                            trace_log(
                                "run",
                                "raw stream preview iteration=%d chars=%d preview=%s",
                                state.iteration,
                                len(full_response),
                                self._preview(full_response),
                            )
                            next_raw_preview_chars += self.raw_stream_preview_step

                        # Stream think panel incrementally for models that emit נקוד...ied.
                        thinking_raw, answer_raw, thinking_closed = self._extract_thinking_and_answer_raw(full_response)
                        if len(thinking_raw) > emitted_thinking_chars:
                            delta_thinking = thinking_raw[emitted_thinking_chars:]
                            if delta_thinking:
                                yield {"type": "thinking", "content": delta_thinking}
                                emitted_thinking_chars = len(thinking_raw)
                        if thinking_closed and not thinking_closed_once:
                            thinking_closed_once = True
                            thinking_final = thinking_raw.strip()
                            if thinking_final and thinking_final not in state.reasoning:
                                state.reasoning.append(thinking_final)

                        if not suppress_visible_output and self._looks_like_tool_call_prefix(full_response):
                            suppress_visible_output = True
                            trace_log(
                                "run",
                                "suppressing visible stream iteration=%d preview=%s",
                                state.iteration,
                                self._preview(full_response, 180),
                            )
                            continue

                        if suppress_visible_output and not self._looks_like_tool_call_prefix(full_response):
                            suppress_visible_output = False

                        # Stream visible answer delta when content is not tool-call markup.
                        if not suppress_visible_output:
                            answer_so_far = self._strip_tool_call_markup(answer_raw)
                            if emitted_token_chars == 0 and self._should_hold_prefix_buffer(answer_so_far):
                                # Hold tiny leading symbol fragments (e.g., '{', '<') until
                                # we can classify as tool call or clean answer text.
                                if self._looks_like_tool_call_prefix(full_response):
                                    suppress_visible_output = True
                                    continue
                                continue
                            if len(answer_so_far) > emitted_token_chars:
                                delta = answer_so_far[emitted_token_chars:]
                                if delta:
                                    yield {"type": "token", "content": delta}
                                    emitted_token_chars = len(answer_so_far)
                                    visible_stream_preview += delta
                                    if len(visible_stream_preview) >= next_stream_preview_chars:
                                        trace_log(
                                            "run",
                                            "visible stream preview iteration=%d chars=%d preview=%s",
                                            state.iteration,
                                            len(visible_stream_preview),
                                            self._preview(visible_stream_preview),
                                        )
                                        next_stream_preview_chars += self.stream_preview_step
                
                # Check for tool calls in final response
                # Check for tool calls in final response (supports multi-call parallel execution)
                detected_calls = self._extract_all_tool_calls(full_response)

                if detected_calls:
                    if should_stop and should_stop():
                        yield {"type": "done", "content": ""}
                        return

                    # Emit a tool_call event for every detected call.
                    for tc in detected_calls:
                        tc_name = tc.get("tool")
                        tc_args = tc.get("arguments", {})
                        trace_log(
                            "run",
                            "tool detected iteration=%d tool=%s args=%s",
                            state.iteration,
                            str(tc_name),
                            self._preview(json.dumps(tc_args, ensure_ascii=False), 240),
                        )
                        if event_callback:
                            event_callback("status", f"Calling tool: {tc_name}")
                        yield {
                            "type": "tool_call",
                            "content": {"name": tc_name, "arguments": tc_args},
                        }

                    # Record the assistant turn once (strip raw tool JSON from visible text).
                    cleaned_response = self._strip_tool_call_markup(full_response)
                    state.messages.append({
                        "role": "assistant",
                        "content": cleaned_response.strip() if cleaned_response.strip() else full_response,
                    })

                    # --- Parallel execution with a concurrency cap ---
                    _sem = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

                    async def _exec_one(tool_name: str, arguments: Dict) -> Dict:
                        async with _sem:
                            return await execute_tool_async(tool_name, arguments)

                    n_calls = len(detected_calls)
                    if event_callback:
                        _lbl = "tool" if n_calls == 1 else f"{n_calls} tools in parallel"
                        event_callback("status", f"Executing {_lbl}...")
                    trace_log(
                        "run",
                        "executing %d tool(s) in parallel iteration=%d",
                        n_calls,
                        state.iteration,
                    )

                    raw_results = await asyncio.gather(
                        *[_exec_one(tc.get("tool"), tc.get("arguments", {})) for tc in detected_calls],
                        return_exceptions=True,
                    )

                    # Collect results, emit events, build combined feedback message.
                    result_parts: List[str] = []
                    for tc, raw_result in zip(detected_calls, raw_results):
                        tc_name = tc.get("tool")
                        tc_args = tc.get("arguments", {})
                        if isinstance(raw_result, Exception):
                            result: Dict[str, Any] = {"success": False, "error": str(raw_result)}
                        else:
                            result = raw_result  # type: ignore[assignment]

                        result = self._enrich_file_tool_result_with_directory_listing(
                            str(tc_name),
                            tc_args,
                            result,
                        )

                        state.tool_calls.append({"tool": tc_name, "arguments": tc_args, "result": result})
                        trace_log(
                            "run",
                            "tool execution completed iteration=%d tool=%s success=%s",
                            state.iteration,
                            str(tc_name),
                            str(bool(result.get("success"))),
                        )
                        yield {
                            "type": "tool_result",
                            "content": {"name": tc_name, "result": result},
                        }
                        trace_log(
                            "run",
                            "tool result preview iteration=%d tool=%s preview=%s",
                            state.iteration,
                            str(tc_name),
                            self._preview(
                                result.get("result") if result.get("success") else result.get("error"),
                                260,
                            ),
                        )
                        result_parts.append(
                            f"Tool '{tc_name}' result: {json.dumps(result, ensure_ascii=False)}"
                        )

                    # Feed all results back in one combined message.
                    combined_results = "\n\n".join(result_parts)
                    state.messages.append({"role": "user", "content": combined_results})
                    state.messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Tool results are now available. "
                                "Use these results to complete the original user request. "
                                "If the task is complete, provide a concise final summary for the user. "
                                "If details are missing, ask one concise follow-up question listing exactly what is missing. "
                                "Do not repeat a successful tool call with the same arguments. "
                                "Do not return an empty response."
                            ),
                        }
                    )

                    if event_callback:
                        event_callback("status", "Processing tool results...")

                    # Continue loop for next iteration
                    continue
                
                else:
                    # Final answer
                    # Parse any remaining thinking
                    thinking_raw, answer_raw, _ = self._extract_thinking_and_answer_raw(full_response)
                    thinking = thinking_raw.strip()
                    answer = self._strip_tool_call_markup(answer_raw).strip()
                    if thinking and thinking not in state.reasoning:
                        # Catch any edge case where thinking closed exactly at end.
                        state.reasoning.append(thinking)

                    if not answer and state.tool_calls:
                        answer = self._build_post_tool_fallback_answer(
                            state.tool_calls[-1],
                            current_user_query,
                        )
                        trace_log(
                            "run",
                            "empty final answer recovered iteration=%d fallback_preview=%s",
                            state.iteration,
                            self._preview(answer, 220),
                        )

                    # Flush any withheld visible text for short answers.
                    if answer:
                        # If some answer tail wasn't streamed yet, emit it now.
                        if emitted_token_chars < len(answer):
                            remaining = answer[emitted_token_chars:]
                            if remaining:
                                yield {"type": "token", "content": remaining}
                                visible_stream_preview += remaining
                                emitted_token_chars = len(answer)

                    if visible_stream_preview:
                        trace_log(
                            "run",
                            "final stream preview iteration=%d chars=%d preview=%s",
                            state.iteration,
                            len(visible_stream_preview),
                            self._preview(visible_stream_preview),
                        )

                    if chat_key and answer:
                        store_message(chat_key, "assistant", answer)
                    await self._maybe_update_long_term_memory(chat_key, recent_messages, answer)
                    trace_log(
                        "run",
                        "model stream complete iteration=%d answer_chars=%d",
                        state.iteration,
                        len(answer),
                    )

                    if event_callback:
                        event_callback("status", "Complete")
                    yield {"type": "done", "content": ""}
                    return
            
            except Exception as e:
                logger.error(f"Error in agent loop: {e}")
                trace_log("run", "agent exception iteration=%d error=%s", state.iteration, str(e))
                yield {"type": "error", "content": str(e)}
                return
        
        # Max iterations reached
        trace_log("run", "max iterations reached=%d", state.max_iterations)
        yield {"type": "error", "content": "Maximum iterations reached without final answer"}
