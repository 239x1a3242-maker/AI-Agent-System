# Unified Server Guide

## 1) What this project is

This workspace contains three MCP backend servers and one unified bridge server:

- server-fetch.py: web fetching, parsing, readable extraction, web search, YouTube transcript extraction.
- server-filesystem.py: secure filesystem operations with allowed-root enforcement and symlink safety.
- server-shell.py: secure shell command execution through a persistent terminal.
- server.py: bridge layer that accepts a JSON tool request, decides which backend owns the tool, executes the tool, and returns a normalized response.

Primary purpose:

- Use one entrypoint for tools from multiple domains (web, files, shell).
- Keep backend logic separated by responsibility.
- Allow LLM-style tool calling using a predictable JSON contract.

---

## 2) How it works

### High-level flow

1. Client (or test script) sends a JSON request with tool and arguments.
2. server.py parses and validates the request.
3. server.py chooses backend by tool name.
4. Backend tool executes.
5. server.py returns a unified response that includes:
   - ok
   - tool
   - backend
   - id and model (if provided)
   - result payload

### Routing ownership

- Fetch tools -> server-fetch.py
- Filesystem tools -> server-filesystem.py
- Shell tool -> server-shell.py

### Bridge tools

- bridge_list_tools: returns merged catalog of all bridged tools.
- bridge_request: accepts one JSON request string and dispatches it.

---

## 3) How to use

## A) Quick start with unified bridge

Prerequisites:

- Python 3.10+
- mcp Python SDK installed

Run bridge server over stdio:

Windows PowerShell example:

- Set FASTMCP_TRANSPORT=stdio
- Set BRIDGE_ALLOWED_DIRECTORIES=<app>\workspace
- Set MCP_SHELL_DEFAULT_CWD=<app>\workspace
- python .\server.py

Use config file:

- mcp-config.json is provided as a ready local sample that points to `backend/tools/server.py`.

## B) Direct one-shot JSON call (CLI mode)

You can call one request without running long-lived MCP:

- python .\server.py --json "{\"tool\":\"bridge_list_tools\",\"arguments\":{}}"

## C) Run end-to-end tests

- python .\test.py

Test flow simulates an LLM named gpt-oss-120b calling tools through server.py.

---

## 4) Request format and expected output

## Request object format

Required fields:

- tool: string
- arguments: object (optional in request, defaults to empty object in bridge parser)

Optional fields:

- id: string
- model: string
- tool_name or name can be used as aliases for tool in request parsing.

Example request object:

{
  "id": "req-001",
  "model": "gpt-oss-120b",
  "tool": "read_text_file",
  "arguments": {
    "path": "C:\\Users\\gajja\\Documents\\mcp-servers-tools\\test.py"
  }
}

## Bridge response format

Success response shape:

{
  "ok": true,
  "tool": "read_text_file",
  "backend": "server-filesystem.py",
  "model": "gpt-oss-120b",
  "id": "req-001",
  "result": { ...backend payload... }
}

Error response shape:

{
  "ok": false,
  "error": "...message...",
  "traceback": "...optional traceback text..."
}

## Backend payload conventions

Fetch and filesystem generally return MCP-compatible wrappers:

- content: [{ type, text or media fields }]
- optional isError: true on failures
- optional structuredContent

Shell via bridge currently returns text wrapper through server.py.

---

## 5) Complete tool catalog (all three servers)




## 5.1 Fetch server tools (server-fetch.py)

1. fetch_html
- Purpose: Fetch page and return raw HTML text.
- Key input: url, headers, max_length, start_index, proxy.
- Expected output: text payload with HTML content.

2. fetch_markdown
- Purpose: Fetch page and convert to Markdown.
- Key input: url, headers, max_length, start_index, proxy.
- Expected output: text payload with Markdown.

3. fetch_txt
- Purpose: Fetch page and return plain text.
- Key input: url, headers, max_length, start_index, proxy.
- Expected output: text payload with plain text.

4. fetch_json
- Purpose: Fetch JSON URL and return parsed/serialized JSON text.
- Key input: url, headers, max_length, start_index, proxy.
- Expected output: text payload with JSON content.

5. fetch_url
- Purpose: Fetch and produce rich structured extraction.
- Key input: url, headers, max_length, start_index, proxy, workers.
- Expected output: text payload containing JSON with metadata like title, content_type, cleaned_content, paragraphs, keywords, readability metrics.

6. fetch_readable
- Purpose: Extract readable article-like content and convert to Markdown.
- Key input: url, headers, max_length, start_index, proxy.
- Expected output: text payload with readable Markdown.

7. fetch_youtube_transcript
- Purpose: Get YouTube transcript/captions.
- Key input: url, headers, max_length, start_index, proxy, lang.
- Expected output: transcript text with language header.

8. web_search
- Purpose: DuckDuckGo web search.
- Key input: query, max_results, region, safesearch, workers.
- Expected output: text payload containing JSON list of ranked results.

## 5.2 Filesystem server tools (server-filesystem.py)

1. read_file (deprecated alias of read_text_file)
- Input: path, tail?, head?
- Output: text payload.

2. read_text_file
- Input: path, tail?, head?
- Output: text payload.

3. read_media_file
- Input: path
- Output: media payload with base64 and mimeType.

4. read_multiple_files
- Input: paths list
- Output: combined text payload (per-file content/errors).

5. write_file
- Input: path, content
- Output: success text payload.

6. edit_file
- Input: path, edits list, dryRun?
- Output: unified diff text payload.

7. create_directory
- Input: path
- Output: success text payload.

8. list_directory
- Input: path
- Output: tagged entries text payload.

9. list_directory_with_sizes
- Input: path, sortBy name|size
- Output: table-like text summary.

10. directory_tree
- Input: path, excludePatterns?
- Output: JSON tree text payload.

11. move_file
- Input: source, destination
- Output: success text payload.

12. delete_file
- Input: path
- Output: success text payload.

13. delete_directory
- Input: path, recursive?
- Output: success text payload.

14. rename_file
- Input: source, destination
- Output: success text payload.

15. rename_directory
- Input: source, destination
- Output: success text payload.

16. path_exists
- Input: path
- Output: JSON metadata text payload (exists, realPath, isFile, isDirectory).

17. search_files
- Input: path, pattern, excludePatterns?
- Output: matching paths text payload.

18. get_file_info
- Input: path
- Output: key:value metadata text payload.

19. list_allowed_directories
- Input: none
- Output: configured allowed roots text payload.

20. list_tool_definitions
- Input: outputFormat json|text
- Output: full catalog payload.

21. sync_client_roots
- Input: none
- Output: root sync result text payload (requires MCP session roots capability).

22. get_tool_catalog_structured
- Input: none
- Output: native structured catalog object.

## 5.3 Shell server tools (server-shell.py)

1. run_command
- Input: command, timeout_seconds?, terminal_id?
- Output: command output text.

Behavior notes:
- Uses persistent shell session.
- Validates command existence.
- Blocks blacklisted base commands.

## 5.4 Bridge server tools (server.py)

1. bridge_list_tools
- Input: output_format json|text
- Output: merged tool catalog with backend ownership.

2. bridge_request
- Input: request_json string
- Output: bridge-dispatched response as text plus structuredContent.

---

## 6) Security model summary

Fetch server:
- Allows only HTTP/HTTPS.
- Blocks localhost/private addresses to reduce SSRF risk.
- Enforces response byte limit.

Filesystem server:
- Operates only inside configured allowed directories.
- Relative paths resolve from the configured workspace root by default.
- Normalizes/validates paths.
- Validates real paths for symlink escape prevention.

Shell server:
- Uses command blacklist for dangerous commands.
- Validates command existence.
- Defaults to the workspace directory and resets to that directory before each command.
- Uses timeout and restarts terminal on timeout.

Bridge server:
- Dispatch-only layer; backend security checks still apply.

---

## 7) Current checks, issues, and errors

Validation run summary:
- test.py passed end-to-end in this workspace.
- Static checker reported optional imports unresolved in server-fetch.py:
  - readability
  - markdownify

Impact:
- Not fatal. server-fetch.py already has fallback logic for missing optional packages.
- Readability/Markdown quality may be reduced when those packages are absent.

Known functional limitation in bridge:
- sync_client_roots requires active MCP session context and is intentionally not supported in plain bridge_request dispatch mode.

Observations:
- server-shell.py uses an environment gate value string named server-clint. This appears intentional in current code and config flow, but naming may be a typo versus server-client.

---

## 8) Enhancement opportunities

## High-impact functional enhancements

1. Strong JSON schema validation in bridge_request
- Add strict schema checks (required fields, type safety, unknown-field policy).
- Benefit: safer and clearer tool-call failures.

2. Unified error contract for shell calls
- Today execute_command returns plain text, which can make success/error distinction less explicit.
- Improve by returning structured object with explicit isError and exit semantics.

3. Add per-tool authorization policy in bridge
- Optional allowlist/denylist by model or client identity.
- Benefit: least-privilege access for high-risk tools.

4. Add native async dispatch in bridge
- Support async filesystem tools that require context in a full MCP session path.
- Benefit: broader parity with backend capabilities.

## Performance enhancements

1. Add lightweight metrics and tracing
- Measure per-tool latency, failures, payload sizes.
- Benefit: easier tuning and reliability monitoring.

2. Add optional result caching for deterministic tools
- Example: web_search and fetch_url cache by input key and TTL.
- Benefit: lower latency and fewer outbound requests.

3. Add output truncation controls in bridge
- Guard against very large payloads in shell and fetch responses.
- Benefit: better model token efficiency and transport stability.

4. Parallel fan-out mode (optional)
- Add a batch request mode where independent tool calls can run concurrently.
- Benefit: faster multi-step workflows.

## Reliability enhancements

1. Retry policy with backoff for transient network errors in fetch paths.
2. Add graceful shutdown hooks in bridge to close shell sessions explicitly.
3. Add integration tests for failure paths (invalid json, unknown tool, denied path, command blacklist).

---

## 9) Practical usage tips

- Prefer bridge_list_tools first to discover exact tool names and expected fields.
- Keep BRIDGE_ALLOWED_DIRECTORIES narrowly scoped for safety.
- For shell usage, keep timeouts small by default and increase only when needed.
- For fetch usage, set max_length to control output size and model token cost.

---

## 10) Related files in this workspace

- server.py
- test.py
- mcp-config.json
- server-fetch.py
- server-filesystem.py
- server-shell.py
