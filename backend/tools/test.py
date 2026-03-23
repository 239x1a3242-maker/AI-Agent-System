#!/usr/bin/env python3
"""Comprehensive bridge test runner.

This script simulates LLM-style tool calling (model: gpt-oss-120b), tests all
31 backend tools routed through server.py, tests 2 bridge tools separately, and
stores machine-readable results under test-results/.
"""

from __future__ import annotations

import base64
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any

from server import bridge_list_tools, bridge_request, handle_json_request, list_all_tools


MODEL_NAME = "gpt-oss-120b"
BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "test-results"
SANDBOX_DIR = RESULTS_DIR / "sandbox"


def _pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def llm_call(tool: str, arguments: dict[str, Any] | None = None, req_id: str | None = None) -> dict[str, Any]:
    request = {
        "id": req_id or f"req-{tool}",
        "model": MODEL_NAME,
        "tool": tool,
        "arguments": arguments or {},
    }
    return handle_json_request(request)


def _extract_text(response: dict[str, Any]) -> str:
    result = response.get("result", {})
    if not isinstance(result, dict):
        return ""
    content = result.get("content", [])
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return str(first.get("text", ""))
    return ""


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _setup_sandbox() -> dict[str, str]:
    if SANDBOX_DIR.exists():
        shutil.rmtree(SANDBOX_DIR)
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

    dir_a = SANDBOX_DIR / "dir_a"
    dir_b = SANDBOX_DIR / "dir_b"
    rename_src = SANDBOX_DIR / "rename_me"
    delete_dir = SANDBOX_DIR / "delete_dir"

    for directory in (dir_a, dir_b, rename_src, delete_dir):
        directory.mkdir(parents=True, exist_ok=True)

    alpha = dir_a / "alpha.txt"
    beta = dir_a / "beta.txt"
    write_target = dir_b / "write_target.txt"
    delete_file = SANDBOX_DIR / "delete_me.txt"
    delete_dir_file = delete_dir / "child.txt"
    tiny_png = dir_a / "tiny.png"

    _write_text(alpha, "alpha line 1\nalpha line 2\n")
    _write_text(beta, "beta line 1\nbeta line 2\n")
    _write_text(write_target, "before edit\n")
    _write_text(delete_file, "delete me\n")
    _write_text(delete_dir_file, "delete dir child\n")

    tiny_png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7M4QAAAABJRU5ErkJggg=="
    )
    tiny_png.write_bytes(tiny_png_data)

    return {
        "sandbox": str(SANDBOX_DIR),
        "dir_a": str(dir_a),
        "dir_b": str(dir_b),
        "new_dir": str(SANDBOX_DIR / "new_created_dir"),
        "alpha": str(alpha),
        "beta": str(beta),
        "tiny_png": str(tiny_png),
        "write_target": str(write_target),
        "delete_file": str(delete_file),
        "delete_dir": str(delete_dir),
        "rename_src": str(rename_src),
        "rename_dst": str(SANDBOX_DIR / "renamed_dir"),
        "moved_alpha": str(dir_b / "alpha_moved.txt"),
        "renamed_alpha": str(dir_b / "alpha_renamed.txt"),
    }


def _catalog_counts() -> dict[str, Any]:
    catalog = list_all_tools()
    tools = catalog.get("tools", [])

    fetch_count = 0
    fs_count = 0
    shell_count = 0

    for tool in tools:
        backend = str(tool.get("backend", ""))
        if backend == "server-fetch.py":
            fetch_count += 1
        elif backend == "server-filesystem.py":
            fs_count += 1
        elif backend == "server-shell.py":
            shell_count += 1

    total = fetch_count + fs_count + shell_count
    counts = {
        "expected": {
            "total": 31,
            "fetch": 8,
            "filesystem": 22,
            "shell": 1,
            "bridge_extra": 2,
        },
        "discovered": {
            "total": total,
            "fetch": fetch_count,
            "filesystem": fs_count,
            "shell": shell_count,
        },
    }

    counts["match_expected"] = counts["expected"]["total"] == counts["discovered"]["total"] and (
        counts["expected"]["fetch"] == counts["discovered"]["fetch"]
    ) and (
        counts["expected"]["filesystem"] == counts["discovered"]["filesystem"]
    ) and (
        counts["expected"]["shell"] == counts["discovered"]["shell"]
    )
    return counts


def _run_backend_case(
    index: int,
    group: str,
    tool: str,
    args: dict[str, Any],
    expect_ok: bool = True,
    expect_contains: str | None = None,
) -> dict[str, Any]:
    print(f"\\n[{index:02d}] {group} -> {tool}")
    print("Arguments:")
    print(_pretty(args))

    started = time.perf_counter()
    exception_text = ""
    response: dict[str, Any] = {}
    try:
        response = llm_call(tool, args, req_id=f"backend-{index}")
    except Exception as exc:
        exception_text = str(exc)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    if exception_text:
        print(f"Exception: {exception_text}")
        return {
            "index": index,
            "group": group,
            "tool": tool,
            "args": args,
            "expect_ok": expect_ok,
            "actual_ok": False,
            "status": "fail",
            "duration_ms": elapsed_ms,
            "exception": exception_text,
            "response": {},
        }

    actual_ok = bool(response.get("ok", False))
    output_text = _extract_text(response)
    preview = output_text if len(output_text) <= 280 else output_text[:280] + "..."

    print(
        _pretty(
            {
                "ok": actual_ok,
                "backend": response.get("backend"),
                "id": response.get("id"),
                "duration_ms": elapsed_ms,
            }
        )
    )
    if preview:
        print("Output preview:")
        print(preview)

    pass_ok = actual_ok == expect_ok
    pass_contains = True
    if expect_contains is not None:
        pass_contains = expect_contains in output_text

    status = "pass" if pass_ok and pass_contains else "fail"
    return {
        "index": index,
        "group": group,
        "tool": tool,
        "args": args,
        "expect_ok": expect_ok,
        "expect_contains": expect_contains,
        "actual_ok": actual_ok,
        "status": status,
        "duration_ms": elapsed_ms,
        "response": response,
        "output_preview": preview,
    }


def _run_bridge_tools(paths: dict[str, str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    print("\\n[Bridge] Testing bridge_list_tools function")
    started = time.perf_counter()
    bridge_list_resp = bridge_list_tools("json")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    bridge_list_ok = not bool(bridge_list_resp.get("isError", False))
    results.append(
        {
            "tool": "bridge_list_tools",
            "status": "pass" if bridge_list_ok else "fail",
            "duration_ms": elapsed_ms,
            "response": bridge_list_resp,
        }
    )

    print("[Bridge] Testing bridge_request function")
    nested_request = {
        "id": "bridge-nested-1",
        "model": MODEL_NAME,
        "tool": "path_exists",
        "arguments": {
            "path": paths["beta"],
        },
    }
    started = time.perf_counter()
    bridge_request_resp = bridge_request(json.dumps(nested_request))
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    bridge_request_ok = not bool(bridge_request_resp.get("isError", False))
    results.append(
        {
            "tool": "bridge_request",
            "status": "pass" if bridge_request_ok else "fail",
            "duration_ms": elapsed_ms,
            "response": bridge_request_resp,
        }
    )

    return results


def run_tests() -> int:
    print("Running full tool matrix tests as model:", MODEL_NAME)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    counts = _catalog_counts()
    print("Discovered tool counts:")
    print(_pretty(counts))

    paths = _setup_sandbox()

    backend_cases: list[tuple[str, str, dict[str, Any], bool, str | None]] = [
        ("fetch", "fetch_html", {"url": "https://example.com", "max_length": 400}, True, None),
        ("fetch", "fetch_markdown", {"url": "https://example.com", "max_length": 400}, True, None),
        ("fetch", "fetch_txt", {"url": "https://example.com", "max_length": 400}, True, None),
        ("fetch", "fetch_json", {"url": "https://jsonplaceholder.typicode.com/todos/1"}, True, None),
        ("fetch", "fetch_url", {"url": "https://example.com", "max_length": 600, "workers": 2}, True, None),
        ("fetch", "fetch_readable", {"url": "https://example.com", "max_length": 500}, True, None),
        (
            "fetch",
            "fetch_youtube_transcript",
            {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "max_length": 700, "lang": "en"},
            True,
            None,
        ),
        ("fetch", "web_search", {"query": "Model Context Protocol", "max_results": 3}, True, None),
        ("filesystem", "read_file", {"path": paths["alpha"], "head": 1}, True, "alpha line 1"),
        ("filesystem", "read_text_file", {"path": paths["beta"], "tail": 1}, True, "beta line 2"),
        ("filesystem", "read_media_file", {"path": paths["tiny_png"]}, True, None),
        (
            "filesystem",
            "read_multiple_files",
            {"paths": [paths["alpha"], paths["beta"]]},
            True,
            "alpha line 1",
        ),
        (
            "filesystem",
            "write_file",
            {"path": paths["write_target"], "content": "write_file content\\n"},
            True,
            "Successfully wrote",
        ),
        (
            "filesystem",
            "edit_file",
            {
                "path": paths["write_target"],
                "edits": [{"oldText": "write_file content", "newText": "edited content"}],
                "dryRun": False,
            },
            True,
            "diff",
        ),
        ("filesystem", "create_directory", {"path": paths["new_dir"]}, True, "Successfully created"),
        ("filesystem", "list_directory", {"path": paths["sandbox"]}, True, None),
        ("filesystem", "list_directory_with_sizes", {"path": paths["sandbox"], "sortBy": "name"}, True, "Total:"),
        ("filesystem", "directory_tree", {"path": paths["sandbox"], "excludePatterns": ["**/*.tmp"]}, True, "dir_a"),
        (
            "filesystem",
            "move_file",
            {"source": paths["alpha"], "destination": paths["moved_alpha"]},
            True,
            "Successfully moved",
        ),
        ("filesystem", "delete_file", {"path": paths["delete_file"]}, True, "Successfully deleted file"),
        (
            "filesystem",
            "delete_directory",
            {"path": paths["delete_dir"], "recursive": True},
            True,
            "Successfully deleted directory",
        ),
        (
            "filesystem",
            "rename_file",
            {"source": paths["moved_alpha"], "destination": paths["renamed_alpha"]},
            True,
            "Successfully renamed file",
        ),
        (
            "filesystem",
            "rename_directory",
            {"source": paths["rename_src"], "destination": paths["rename_dst"]},
            True,
            "Successfully renamed directory",
        ),
        ("filesystem", "path_exists", {"path": paths["renamed_alpha"]}, True, '"exists": true'),
        ("filesystem", "search_files", {"path": paths["sandbox"], "pattern": "*.txt"}, True, "beta.txt"),
        ("filesystem", "get_file_info", {"path": paths["renamed_alpha"]}, True, "size:"),
        ("filesystem", "list_allowed_directories", {}, True, "Allowed directories"),
        ("filesystem", "list_tool_definitions", {"outputFormat": "json"}, True, '"toolCount"'),
        (
            "filesystem",
            "sync_client_roots",
            {},
            False,
            "requires an active MCP session context",
        ),
        (
            "filesystem",
            "get_tool_catalog_structured",
            {},
            True,
            '"toolCount"',
        ),
        (
            "shell",
            "run_command",
            {"command": "echo bridge-all-tools-ok", "timeout_seconds": 20},
            True,
            "bridge-all-tools-ok",
        ),
    ]

    backend_results: list[dict[str, Any]] = []
    for i, (group, tool, args, expect_ok, expect_contains) in enumerate(backend_cases, start=1):
        backend_results.append(
            _run_backend_case(
                i,
                group,
                tool,
                args,
                expect_ok=expect_ok,
                expect_contains=expect_contains,
            )
        )

    bridge_results = _run_bridge_tools(paths)

    backend_pass = sum(1 for item in backend_results if item["status"] == "pass")
    backend_fail = len(backend_results) - backend_pass
    bridge_pass = sum(1 for item in bridge_results if item["status"] == "pass")
    bridge_fail = len(bridge_results) - bridge_pass

    run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_report_path = RESULTS_DIR / f"test-results-{run_stamp}.json"
    latest_json_path = RESULTS_DIR / "latest-test-results.json"
    latest_text_path = RESULTS_DIR / "latest-summary.txt"

    report = {
        "model": MODEL_NAME,
        "run_timestamp": datetime.now().isoformat(),
        "counts": counts,
        "backend_tests": {
            "total": len(backend_results),
            "pass": backend_pass,
            "fail": backend_fail,
            "results": backend_results,
        },
        "bridge_tests": {
            "total": len(bridge_results),
            "pass": bridge_pass,
            "fail": bridge_fail,
            "results": bridge_results,
        },
    }

    _write_text(json_report_path, json.dumps(report, ensure_ascii=False, indent=2))
    _write_text(latest_json_path, json.dumps(report, ensure_ascii=False, indent=2))

    summary_lines = [
        "Unified Bridge Full Tool Test Summary",
        f"Model: {MODEL_NAME}",
        f"Run timestamp: {report['run_timestamp']}",
        "",
        "Expected backend tool counts:",
        "- Total tools in the three backend servers: 31",
        "- Fetch tools: 8",
        "- Filesystem tools: 22",
        "- Shell tools: 1",
        "- Bridge server adds 2 extra bridge tools",
        "",
        f"Discovered count match: {counts['match_expected']}",
        f"Backend tests: {backend_pass}/{len(backend_results)} passed, {backend_fail} failed",
        f"Bridge tests: {bridge_pass}/{len(bridge_results)} passed, {bridge_fail} failed",
        "",
        f"Detailed JSON report: {json_report_path}",
        f"Latest JSON report: {latest_json_path}",
    ]
    _write_text(latest_text_path, "\n".join(summary_lines) + "\n")

    print("\n========== FINAL SUMMARY ==========")
    for line in summary_lines:
        print(line)

    if not counts["match_expected"]:
        return 1
    if backend_fail > 0 or bridge_fail > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
