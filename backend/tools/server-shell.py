#!/usr/bin/env python3
"""Shell MCP server (Python migration of build/server-shell.js).

This file preserves behavior of the Node implementation:
- Exposes a single MCP tool: run_command
- Validates command existence
- Blocks dangerous base commands with a blacklist
- Executes via persistent shell session with inherited environment
- Supports the `config` CLI mode for Claude Desktop config updates
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from mcp.server.fastmcp import FastMCP


SERVER_INSTRUCTIONS = (
    "Secure MCP shell server. Use run_command for command execution. "
    "Dangerous base commands are blocked by policy. "
    "Terminal sessions are persistent and support streamed output retrieval."
)
LOG_LEVEL = os.environ.get("MCP_SHELL_LOG_LEVEL", "INFO").upper()
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("MCP_SHELL_TIMEOUT_SECONDS", "60"))
MAX_TIMEOUT_SECONDS = float(os.environ.get("MCP_SHELL_MAX_TIMEOUT_SECONDS", "300"))
MAX_COMMAND_LENGTH = int(os.environ.get("MCP_SHELL_MAX_COMMAND_LENGTH", "8192"))
TERMINAL_BUFFER_LIMIT_CHARS = int(os.environ.get("MCP_SHELL_TERMINAL_BUFFER_LIMIT", "200000"))
TERMINAL_IDLE_WINDOW_SECONDS = float(os.environ.get("MCP_SHELL_IDLE_WINDOW_SECONDS", "0.35"))
TERMINAL_STARTUP_GRACE_SECONDS = float(os.environ.get("MCP_SHELL_STARTUP_GRACE_SECONDS", "0.25"))
DEFAULT_TERMINAL_ID = "default"
SERVER_ACCESS_ENV_KEY = "MCP_SHELL_ACCESS_CLIENT"
SERVER_ACCESS_ENV_VALUE = "server-clint"
APP_DIR = Path(__file__).resolve().parents[2]
DEFAULT_WORKSPACE_DIR = Path(os.environ.get("MCP_SHELL_DEFAULT_CWD", APP_DIR / "workspace")).resolve()
DEFAULT_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_CWD_DIRECTORIES = [
    str(Path(entry).resolve())
    for entry in os.environ.get("MCP_SHELL_ALLOWED_DIRECTORIES", str(DEFAULT_WORKSPACE_DIR)).split(os.pathsep)
    if str(entry).strip()
]

WINDOWS_POWERSHELL_COMMANDS = {
    "cd",
    "chdir",
    "dir",
    "echo",
    "pwd",
    "ls",
    "gci",
    "cat",
    "gc",
    "cp",
    "mv",
    "rm",
}
WINDOWS_DIRECTORY_BUILTINS = {"cd", "chdir", "pushd", "popd", "set-location", "sl"}

_COMMAND_EXISTS_CACHE = {}
_TERMINAL_REGISTRY = {}
_TERMINAL_LOCK = threading.RLock()


# Dangerous commands that should never be allowed.
BLACKLISTED_COMMANDS = {
    # File System Destruction Commands
    "rm",
    "rmdir",
    "del",
    # Disk/Filesystem Commands
    "format",
    "mkfs",
    "dd",
    # Permission/Ownership Commands
    "chmod",
    "chown",
    # Privilege Escalation Commands
    "sudo",
    "su",
    # Code Execution Commands
    "exec",
    "eval",
    # System Communication Commands
    "write",
    "wall",
    # System Control Commands
    "shutdown",
    "reboot",
    "init",
}


def validate_command(base_command):
    """Return True when the base command is not blacklisted."""
    if not isinstance(base_command, str):
        return False
    normalized = base_command.strip().lower()
    return normalized != "" and normalized not in BLACKLISTED_COMMANDS


def _is_windows():
    return os.name == "nt"


def _iter_path_dirs():
    path_value = os.environ.get("PATH", "")
    if not path_value:
        return []
    return [entry for entry in path_value.split(os.pathsep) if entry]


def command_exists(command):
    """Best-effort replacement for Node's command-exists package."""
    if not isinstance(command, str) or command.strip() == "":
        return False
    command = command.strip()

    if _is_windows() and command.lower() in WINDOWS_POWERSHELL_COMMANDS:
        return True

    cache_key = command.lower() if _is_windows() else command
    if cache_key in _COMMAND_EXISTS_CACHE:
        return _COMMAND_EXISTS_CACHE[cache_key]

    has_sep = (os.path.sep in command) or (os.path.altsep and os.path.altsep in command)
    if has_sep:
        exists = os.path.isfile(command)
        _COMMAND_EXISTS_CACHE[cache_key] = exists
        return exists

    path_dirs = _iter_path_dirs()
    if _is_windows():
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        exts = [ext.lower() for ext in pathext.split(";") if ext]
        command_lower = command.lower()
        has_known_ext = any(command_lower.endswith(ext) for ext in exts)

        for directory in path_dirs:
            base_path = os.path.join(directory, command)
            if has_known_ext:
                if os.path.isfile(base_path):
                    _COMMAND_EXISTS_CACHE[cache_key] = True
                    return True
            else:
                for ext in exts:
                    if os.path.isfile(base_path + ext):
                        _COMMAND_EXISTS_CACHE[cache_key] = True
                        return True
        _COMMAND_EXISTS_CACHE[cache_key] = False
        return False

    for directory in path_dirs:
        candidate = os.path.join(directory, command)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _COMMAND_EXISTS_CACHE[cache_key] = True
            return True
    _COMMAND_EXISTS_CACHE[cache_key] = False
    return False


def get_config_path():
    """Match the Node platform-specific Claude Desktop config path logic."""
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"),
            "Library",
            "Application Support",
            "Claude",
            "claude_desktop_config.json",
        )
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("APPDATA", ""),
            "Claude",
            "claude_desktop_config.json",
        )
    raise RuntimeError("Unsupported platform")


def update_config(debug=False):
    """Write a Claude Desktop config entry using the current Python interpreter."""
    script_path = os.path.abspath(__file__)
    config_path = get_config_path()

    try:
        config = {}
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                config = json.load(handle)
                if not isinstance(config, dict):
                    config = {}
        except Exception:
            print("Creating new config file")

        config["mcpServers"] = config.get("mcpServers") or {}

        config["mcpServers"]["shell-server"] = {
            "command": sys.executable,
            "args": [script_path],
            "env": {
                SERVER_ACCESS_ENV_KEY: SERVER_ACCESS_ENV_VALUE,
            },
        }

        config_dir = os.path.dirname(config_path)
        if config_dir and not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)

        print("Updated config at:", config_path)
        print("Added server with command:", script_path)
    except Exception as err:
        print("Error updating config:", err, file=sys.stderr)
        sys.exit(1)


def _assert_client_gateway_access():
    caller = os.environ.get(SERVER_ACCESS_ENV_KEY, "")
    if caller != SERVER_ACCESS_ENV_VALUE:
        raise RuntimeError(
            "Direct access denied: server-shell.py can only be launched by server-clint.py"
        )


def _extract_base_command(command):
    # Matches JS behavior: command.trim().split(/\s+/)[0]
    if not isinstance(command, str):
        return ""
    trimmed = command.strip()
    if trimmed == "":
        return ""
    parts = trimmed.split()
    return parts[0] if parts else ""


def _normalize_terminal_id(terminal_id):
    if terminal_id is None:
        return DEFAULT_TERMINAL_ID
    if not isinstance(terminal_id, str):
        raise RuntimeError("Invalid terminal_id: must be a string")
    normalized = terminal_id.strip()
    if normalized == "":
        raise RuntimeError("Invalid terminal_id: cannot be empty")
    if len(normalized) > 128:
        raise RuntimeError("Invalid terminal_id: too long")
    return normalized


def _is_path_within_allowed_directories(candidate_path, allowed_directories):
    normalized_candidate = os.path.normcase(os.path.realpath(candidate_path))
    for allowed_directory in allowed_directories:
        normalized_allowed = os.path.normcase(os.path.realpath(allowed_directory))
        try:
            if os.path.commonpath([normalized_candidate, normalized_allowed]) == normalized_allowed:
                return True
        except ValueError:
            continue
    return False


def _normalize_cwd(cwd):
    if cwd is None:
        resolved = str(DEFAULT_WORKSPACE_DIR)
    elif not isinstance(cwd, str):
        raise RuntimeError("Invalid cwd: must be a string")
    else:
        resolved = os.path.abspath(cwd)
    if not os.path.isdir(resolved):
        raise RuntimeError(f"Invalid cwd: directory does not exist: {resolved}")
    real_resolved = os.path.realpath(resolved)
    if not _is_path_within_allowed_directories(real_resolved, ALLOWED_CWD_DIRECTORIES):
        allowed_str = ", ".join(ALLOWED_CWD_DIRECTORIES)
        raise RuntimeError(f"Invalid cwd: {real_resolved} is outside allowed directories: {allowed_str}")
    return real_resolved


def _resolve_shell_command(preferred_shell=None):
    if preferred_shell is not None:
        if not isinstance(preferred_shell, str) or preferred_shell.strip() == "":
            raise RuntimeError("Invalid shell: must be a non-empty string")
        selected = preferred_shell.strip()
        if _is_windows() and selected.lower() != "powershell":
            raise RuntimeError("Unsupported shell on Windows: only powershell is allowed")
        if not command_exists(selected):
            raise RuntimeError(f"Shell not found: {selected}")
        return [selected]

    if _is_windows():
        candidates = [
            ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass"],
        ]
    else:
        candidates = [["bash"], ["sh"]]

    for candidate in candidates:
        if command_exists(candidate[0]):
            return candidate

    raise RuntimeError("No supported shell found for persistent terminal")


class TerminalSession:
    """Represents a persistent interactive shell process."""

    def __init__(self, terminal_id, process, shell_command, cwd):
        self.terminal_id = terminal_id
        self.process = process
        self.shell_command = shell_command
        self.cwd = cwd
        self.created_at = time.time()
        self.last_read_seq = 0
        self.output_seq = 0
        self.output_events = []
        self.output_char_count = 0
        self.lock = threading.RLock()
        self.closed = False

        self.stdout_thread = threading.Thread(
            target=self._reader_loop,
            args=(self.process.stdout, "stdout"),
            daemon=True,
        )
        self.stderr_thread = threading.Thread(
            target=self._reader_loop,
            args=(self.process.stderr, "stderr"),
            daemon=True,
        )
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _append_output(self, text):
        if not text:
            return
        with self.lock:
            self.output_seq += 1
            self.output_events.append((self.output_seq, text))
            self.output_char_count += len(text)

            while self.output_events and self.output_char_count > TERMINAL_BUFFER_LIMIT_CHARS:
                _, removed = self.output_events.pop(0)
                self.output_char_count -= len(removed)

            if self.last_read_seq < (self.output_events[0][0] - 1 if self.output_events else self.output_seq):
                self.last_read_seq = self.output_events[0][0] - 1

    def _reader_loop(self, stream, stream_name):
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                self._append_output(line)
        except Exception as error:
            self._append_output(f"[{stream_name} reader error] {error}\n")

    def is_alive(self):
        return self.process.poll() is None

    def current_seq(self):
        with self.lock:
            return self.output_seq

    def send_input(self, input_text):
        if not isinstance(input_text, str):
            raise RuntimeError("Invalid input: must be a string")
        if self.process.poll() is not None:
            raise RuntimeError("Terminal process is not running")

        payload = input_text
        if not payload.endswith("\n"):
            payload += "\n"

        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except Exception as error:
            raise RuntimeError(f"Failed to send input to terminal: {error}") from error

    def read_since(self, seq, max_chars=None):
        with self.lock:
            items = [text for item_seq, text in self.output_events if item_seq > seq]
            combined = "".join(items)
            if isinstance(max_chars, int) and max_chars > 0:
                combined = combined[-max_chars:]
            return combined, self.output_seq

    def read_new(self, max_chars=None):
        output, new_seq = self.read_since(self.last_read_seq, max_chars=max_chars)
        with self.lock:
            self.last_read_seq = new_seq
        return output

    def close(self):
        if self.closed:
            return
        self.closed = True

        try:
            if self.process.stdin:
                self.process.stdin.close()
        except Exception:
            pass

        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass


def _register_terminal(session):
    with _TERMINAL_LOCK:
        _TERMINAL_REGISTRY[session.terminal_id] = session


def _remove_terminal(terminal_id):
    with _TERMINAL_LOCK:
        return _TERMINAL_REGISTRY.pop(terminal_id, None)


def _get_terminal(terminal_id):
    normalized_id = _normalize_terminal_id(terminal_id)
    with _TERMINAL_LOCK:
        session = _TERMINAL_REGISTRY.get(normalized_id)
    if session is None:
        raise RuntimeError(f"Terminal not found: {normalized_id}")
    return session


def _create_terminal_internal(terminal_id=None, cwd=None, shell=None):
    normalized_id = _normalize_terminal_id(terminal_id)
    normalized_cwd = _normalize_cwd(cwd)

    with _TERMINAL_LOCK:
        existing = _TERMINAL_REGISTRY.get(normalized_id)
    if existing and existing.is_alive():
        return existing
    if existing:
        _close_terminal_internal(normalized_id)

    shell_cmd = _resolve_shell_command(shell)
    try:
        process = subprocess.Popen(
            shell_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=normalized_cwd,
            env=dict(os.environ),
        )
    except Exception as error:
        raise RuntimeError(f"Failed to start terminal process: {error}") from error

    session = TerminalSession(normalized_id, process, shell_cmd, normalized_cwd)
    _register_terminal(session)

    if TERMINAL_STARTUP_GRACE_SECONDS > 0:
        time.sleep(TERMINAL_STARTUP_GRACE_SECONDS)

    return session


def _ensure_terminal(terminal_id=None):
    normalized_id = _normalize_terminal_id(terminal_id)
    with _TERMINAL_LOCK:
        existing = _TERMINAL_REGISTRY.get(normalized_id)
    if existing and existing.is_alive():
        return existing
    if existing:
        _remove_terminal(normalized_id)
    return _create_terminal_internal(terminal_id=normalized_id)


def _close_terminal_internal(terminal_id):
    normalized_id = _normalize_terminal_id(terminal_id)
    session = _remove_terminal(normalized_id)
    if session is None:
        return False
    session.close()
    return True


def _restart_terminal(terminal_id):
    normalized_id = _normalize_terminal_id(terminal_id)
    _close_terminal_internal(normalized_id)
    return _create_terminal_internal(terminal_id=normalized_id)


def _kill_pid(process_id):
    if not isinstance(process_id, int) or process_id <= 0:
        raise RuntimeError("Invalid process_id: must be a positive integer")

    try:
        if _is_windows():
            subprocess.run(
                ["taskkill", "/PID", str(process_id), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        else:
            os.kill(process_id, signal.SIGTERM)
    except Exception as error:
        raise RuntimeError(f"Failed to kill process {process_id}: {error}") from error


def _shutdown_all_terminals():
    with _TERMINAL_LOCK:
        terminal_ids = list(_TERMINAL_REGISTRY.keys())
    for terminal_id in terminal_ids:
        _close_terminal_internal(terminal_id)


def _resolve_timeout_seconds(timeout_seconds):
    if timeout_seconds is None:
        timeout_value = DEFAULT_TIMEOUT_SECONDS
    else:
        if not isinstance(timeout_seconds, (int, float)):
            raise RuntimeError("Invalid timeout_seconds: must be a number")
        timeout_value = float(timeout_seconds)

    if timeout_value <= 0:
        raise RuntimeError("Invalid timeout_seconds: must be greater than 0")

    if timeout_value > MAX_TIMEOUT_SECONDS:
        timeout_value = MAX_TIMEOUT_SECONDS

    return timeout_value


def split_command(command):
    """Parse command with quote/escape handling (migration of build/lib/index.js)."""
    if not isinstance(command, str):
        raise ValueError("command must be a string")

    args = []
    current_arg = []
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for char in command:
        if escaped:
            if in_double_quote:
                if char in {'"', '\\', '$', '`', '\n'}:
                    current_arg.append(char)
                else:
                    current_arg.append('\\')
                    current_arg.append(char)
            elif in_single_quote:
                if char in {"'", '\\'}:
                    current_arg.append(char)
                else:
                    current_arg.append('\\')
                    current_arg.append(char)
            else:
                current_arg.append(char)
            escaped = False
            continue

        if char == '\\':
            escaped = True
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue

        if char == ' ' and not in_single_quote and not in_double_quote:
            if current_arg:
                args.append(''.join(current_arg))
                current_arg = []
            continue

        current_arg.append(char)

    if current_arg:
        args.append(''.join(current_arg))

    if in_single_quote or in_double_quote:
        raise ValueError("Unclosed quotes in command")

    if escaped:
        raise ValueError("Dangling escape character")

    return [arg.replace('\\n', '\n') for arg in args]


def _validate_command_for_execution(command):
    if not isinstance(command, str):
        raise RuntimeError("Invalid command: command must be a string")
    if command.strip() == "":
        raise RuntimeError("Invalid command: command cannot be empty")
    if len(command) > MAX_COMMAND_LENGTH:
        raise RuntimeError(
            f"Invalid command: length exceeds max allowed ({MAX_COMMAND_LENGTH} characters)"
        )

    parsed_args = split_command(command)
    if not parsed_args:
        raise RuntimeError("Invalid command: command cannot be empty")

    base_command = parsed_args[0]
    if base_command.strip().lower() in WINDOWS_DIRECTORY_BUILTINS:
        raise RuntimeError(
            "Directory-changing shell builtins are not allowed; run commands relative to the workspace instead"
        )

    if not command_exists(base_command):
        raise RuntimeError(f"Command not found: {base_command}")

    if not validate_command(base_command):
        raise RuntimeError(f"Command not allowed: {base_command}")


def _run_command_in_terminal(session, command, timeout_seconds):
    marker = f"__MCP_DONE_{int(time.time() * 1000)}_{os.getpid()}__"
    wrapped = _wrap_command_for_shell(session, command, marker)

    start_seq = session.current_seq()
    session.send_input(wrapped)

    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        output, _ = session.read_since(start_seq)
        if _has_marker_line(output, marker):
            break
        if session.process.poll() is not None:
            break
        time.sleep(0.05)

    if time.time() >= deadline:
        _restart_terminal(session.terminal_id)
        return f"Command timed out after {timeout_seconds} seconds; terminal restarted"

    output, _ = session.read_since(start_seq)
    output = _strip_marker_line(output, marker)

    cleaned = output.replace("\r\n\r\n", "\r\n").replace("\n\n", "\n")
    return _strip_shell_wrapper_echo(cleaned).strip()


def _wrap_command_for_shell(session, command, marker):
    shell_name = session.shell_command[0].lower() if session.shell_command else ""

    if _is_windows():
        if shell_name != "powershell":
            raise RuntimeError("Unsupported Windows shell session; expected powershell")
        escaped_cwd = session.cwd.replace("'", "''")
        return (
            f"Set-Location -LiteralPath '{escaped_cwd}'; "
            f"& {{ {command} }}; "
            f"Write-Output \"{marker}\""
        )

    escaped_cwd = session.cwd.replace('"', '\\"')
    return f'cd "{escaped_cwd}" && {{ {command}; }}; printf "{marker}\\n"'


def _has_marker_line(output, marker):
    normalized = output.replace("\r\n", "\n")
    for line in normalized.split("\n"):
        if line.strip() == marker:
            return True
    return False


def _strip_marker_line(output, marker):
    normalized = output.replace("\r\n", "\n")
    lines = normalized.split("\n")

    marker_index = -1
    for index, line in enumerate(lines):
        if line.strip() == marker:
            marker_index = index

    if marker_index >= 0:
        return "\n".join(lines[:marker_index])

    return normalized


def _strip_shell_wrapper_echo(output):
    normalized = output.replace("\r\n", "\n")
    lines = normalized.split("\n")
    if lines and "Set-Location -LiteralPath" in lines[0] and "Write-Output" in lines[0]:
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines)


def execute_command(command, timeout_seconds=None, terminal_id=None):
    """Run shell command in a persistent terminal session."""
    try:
        resolved_timeout = _resolve_timeout_seconds(timeout_seconds)
        _validate_command_for_execution(command)

        session = _ensure_terminal(terminal_id)
        return _run_command_in_terminal(session, command, resolved_timeout)
    except Exception as error:
        return str(error)


class ShellServer:
    """FastMCP-based shell server with one exposed tool: run_command."""

    def __init__(self):
        self.mcp = FastMCP(
            "shell-server",
            instructions=SERVER_INSTRUCTIONS,
            log_level=LOG_LEVEL,
            warn_on_duplicate_tools=True,
        )
        self._setup_tool_handlers()

    def _setup_tool_handlers(self):
        @self.mcp.tool(name="run_command", description="Run a shell command")
        def run_command(command=None, timeout_seconds=None):
            """Run a shell command in an existing terminal, or create one if missing."""
            return execute_command(
                command,
                timeout_seconds=timeout_seconds,
                terminal_id=DEFAULT_TERMINAL_ID,
            )

    def run(self):
        # Log to stderr to avoid interfering with MCP stdio protocol.
        print("MCP server running on stdio", file=sys.stderr)
        self.mcp.run(transport="stdio")


def _handle_sigint(_sig, _frame):
    _shutdown_all_terminals()
    raise SystemExit(0)


def main():
    _assert_client_gateway_access()

    args = sys.argv[1:]

    if "config" in args:
        debug = "--debug" in args
        update_config(debug)

    server = ShellServer()
    server.run()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        print("Server error:", error, file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    finally:
        _shutdown_all_terminals()
