"""Declarative native-tool contracts and app-owned policy.

This module deliberately contains *declarations*, not execution.  The ACP/tool
runtime imports these specs so provider translation, policy display, and actual
execution share one schema without mixing filesystem or process code into the
contract layer.
"""

from __future__ import annotations

import json
import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import home_dir
from .paths import crew_data_home

@dataclass(frozen=True)
class NativeRuntimePolicy:
    read_only: bool
    allowed_roots: tuple[str, ...]
    web_enabled: bool = True
    shell_timeout_seconds: float = 120.0
    shell_max_output_bytes: int = 1024 * 1024
    process_max_concurrent: int = 8
    process_max_history: int = 32
    process_buffer_bytes: int = 1024 * 1024
    web_timeout_seconds: float = 15.0
    web_max_output_bytes: int = 2 * 1024 * 1024



def native_policy_path() -> Path:
    return home_dir() / "config" / "native_tools.json"


def _load_native_policy() -> NativeRuntimePolicy:
    """Load app-owned native policy without coupling it to providers.json."""
    raw: dict[str, Any] = {}
    path = native_policy_path()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(parsed, dict):
            raw = parsed
    except (OSError, json.JSONDecodeError):
        pass

    read_only = bool(raw.get("read_only", False))
    roots: list[str] = []
    # KiroCrew's data home is an explicit host fact when available.  Exposing it
    # as an allowed root fixes the common portable layout where the active
    # workspace is <CrewData>/workspace but the requested file is elsewhere
    # under the same Crew data tree.
    crew_root = crew_data_home()
    if crew_root is not None:
        roots.append(str(crew_root))
    configured_roots = raw.get("allowed_roots")
    if isinstance(configured_roots, list):
        roots.extend(str(value) for value in configured_roots if isinstance(value, str) and value.strip())
    env_roots = os.environ.get("KIRO_BYO_ALLOWED_ROOTS", "").strip()
    if env_roots:
        roots.extend(part for part in env_roots.split(os.pathsep) if part.strip())


    def _number(name: str, default: float, minimum: float, maximum: float) -> float:
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return max(minimum, min(float(value), maximum))

    return NativeRuntimePolicy(
        read_only=read_only,
        allowed_roots=tuple(dict.fromkeys(roots)),
        web_enabled=bool(raw.get("web_enabled", True)),
        shell_timeout_seconds=_number("shell_timeout_seconds", 120.0, 1.0, 1800.0),
        shell_max_output_bytes=int(_number("shell_max_output_bytes", 1024 * 1024, 4096, 16 * 1024 * 1024)),
        process_max_concurrent=int(_number("process_max_concurrent", 8, 1, 32)),
        process_max_history=int(_number("process_max_history", 32, 4, 256)),
        process_buffer_bytes=int(_number("process_buffer_bytes", 1024 * 1024, 4096, 16 * 1024 * 1024)),
        web_timeout_seconds=_number("web_timeout_seconds", 15.0, 1.0, 120.0),
        web_max_output_bytes=int(_number("web_max_output_bytes", 2 * 1024 * 1024, 4096, 16 * 1024 * 1024)),
    )


@dataclass(frozen=True)
class NativeToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    acp_kind: str
    capability: str

    def __post_init__(self) -> None:
        # Native tool calls may carry an optional per-operation explanation for
        # KiroCrew's activity history.  It is not an executable parameter or an
        # authorization signal.  Keep the advertised schema and validation in
        # agreement, without changing any required or existing tool fields.
        if self.name == "update_plan":
            return
        schema = dict(self.input_schema)
        properties = dict(schema.get("properties", {}))
        properties["description"] = {
            "type": "string",
            "maxLength": 160,
            "description": (
                "Optional short, plain-language description of THIS operation for "
                "the activity history. Use the BYO interface language, not "
                "necessarily the answer language. Do not include the command, "
                "credentials, secrets or URLs. Display only; never affects execution."
            ),
        }
        schema["properties"] = properties
        object.__setattr__(self, "input_schema", schema)

    def to_tool_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.input_schema),
            },
        }


READ_SPEC = NativeToolSpec(
    name="read",
    description=(
        "Read a UTF-8/text file or list a directory. Relative paths are anchored at the current "
        "workspace; absolute paths and '..' are allowed only when their canonical target remains "
        "inside a configured allowed root. Credential-bearing paths remain blocked."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory path."},
            "offset": {"type": "integer", "minimum": 1, "default": 1, "description": "1-based first line for file reads."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 400, "description": "Maximum lines or directory entries to return."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    acp_kind="read",
    capability="fs_read",
)

WRITE_SPEC = NativeToolSpec(
    name="write",
    description=(
        "Create or replace a UTF-8 text file, matching Kiro CLI's canonical write/fs_write tool. "
        "The destination must resolve inside an allowed root and sensitive credential paths are blocked."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Destination file path."},
            "content": {"type": "string", "description": "Complete UTF-8 file content."},
            "create_parent": {"type": "boolean", "default": True, "description": "Create missing parent directories inside the allowed root."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    acp_kind="edit",
    capability="fs_write",
)

EDIT_SPEC = NativeToolSpec(
    name="edit",
    description=(
        "Apply a unified-diff patch atomically to one UTF-8 text file. This is the precise edit/apply-patch "
        "path; use write when replacing the complete file is intentional."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Existing text file to patch."},
            "patch": {"type": "string", "description": "Unified diff containing one or more @@ hunks for this file."},
        },
        "required": ["path", "patch"],
        "additionalProperties": False,
    },
    acp_kind="edit",
    capability="fs_write",
)

GLOB_SPEC = NativeToolSpec(
    name="glob",
    description=(
        "Find files and directories using a glob pattern. Search is rooted at the separately canonicalized "
        "path argument and respects .gitignore; symlink/junction escapes are pruned."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern such as **/*.py or src/**."},
            "path": {"type": "string", "default": ".", "description": "Directory to search from."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    acp_kind="search",
    capability="fs_read",
)

GREP_SPEC = NativeToolSpec(
    name="grep",
    description=(
        "Search text files using a regular expression. Returns paths, line numbers and matching text; "
        "respects .gitignore and the configured allowed roots."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python-compatible regular expression."},
            "path": {"type": "string", "default": ".", "description": "File or directory to search."},
            "glob": {"type": "string", "default": "**/*", "description": "Optional file glob filter."},
            "case_sensitive": {"type": "boolean", "default": True},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    acp_kind="search",
    capability="fs_read",
)


READ_FILES_SPEC = NativeToolSpec(
    name="read_files",
    description=(
        "Read multiple UTF-8/text files in one call. Paths use the same workspace/allowed-root and "
        "sensitive-path protections as read. Use this when comparing a small set of known files."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 20,
                "description": "Files to read, in order.",
            },
            "offset": {"type": "integer", "minimum": 1, "default": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        "required": ["paths"],
        "additionalProperties": False,
    },
    acp_kind="read",
    capability="fs_read",
)

LIST_DIRECTORY_SPEC = NativeToolSpec(
    name="list_directory",
    description=(
        "List directory entries without reading file contents. Returns canonical workspace-relative paths; "
        "ignored paths and symlink/junction escapes are pruned."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 400},
        },
        "additionalProperties": False,
    },
    acp_kind="read",
    capability="fs_read",
)

DELETE_FILE_SPEC = NativeToolSpec(
    name="delete_file",
    description=(
        "Delete one regular file inside the workspace or another configured allowed root. Directories are "
        "never removed by this tool. Sensitive paths and sandbox escapes are blocked."
    ),
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Existing regular file to delete."}},
        "required": ["path"],
        "additionalProperties": False,
    },
    acp_kind="edit",
    capability="fs_write",
)

WEB_FETCH_SPEC = NativeToolSpec(
    name="web_fetch",
    description=(
        "Fetch a public HTTP/HTTPS URL and return readable text. Local/private network destinations, embedded "
        "credentials, oversized responses and non-text content are blocked."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Public http:// or https:// URL."},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000, "default": 30000},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    acp_kind="read",
    capability="web_fetch",
)

WEB_SEARCH_SPEC = NativeToolSpec(
    name="web_search",
    description=(
        "Search the public web and return ranked result titles, URLs and snippets. This is a network read "
        "operation and remains subject to Crew's permission gate."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 12, "default": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    acp_kind="search",
    capability="web_search",
)

EXECUTE_SHELL_PROCESS_SPEC = NativeToolSpec(
    name="execute_shell_process",
    description=(
        "Start a long-running shell process and return a stable process_id. The process runs in its own "
        "process group, stdout/stderr are captured in bounded buffers, and the owning ACP session cleans it up."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command text to execute."},
            "cwd": {"type": "string", "default": ".", "description": "Working directory inside an allowed root."},
            "initial_wait_ms": {"type": "integer", "minimum": 0, "maximum": 2000, "default": 120},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    acp_kind="execute",
    capability="shell",
)

CONTROL_SHELL_PROCESS_SPEC = NativeToolSpec(
    name="control_shell_process",
    description=(
        "Inspect or control a process created by execute_shell_process. Actions: status/read/stdin/terminate/kill. "
        "Process ids are scoped to the owning ACP session runtime."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "process_id": {"type": "string"},
            "action": {"type": "string", "enum": ["status", "read", "stdin", "terminate", "kill"]},
            "input": {"type": "string", "default": ""},
            "append_newline": {"type": "boolean", "default": False},
            "max_chars": {"type": "integer", "minimum": 256, "maximum": 50000, "default": 12000},
            "grace_seconds": {"type": "number", "minimum": 0.05, "maximum": 10, "default": 2.0},
        },
        "required": ["process_id", "action"],
        "additionalProperties": False,
    },
    acp_kind="execute",
    capability="shell",
)

CODE_SYMBOLS_SPEC = NativeToolSpec(
    name="code_symbols",
    description=(
        "Return structural symbols and a code overview for a Python/JavaScript/TypeScript file. Uses Tree-sitter "
        "when its runtime grammars are installed; Python has an explicit stdlib-AST fallback."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string", "default": "", "description": "Optional regex filtering symbol names."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    acp_kind="search",
    capability="fs_read",
)

CODE_QUERY_SPEC = NativeToolSpec(
    name="code_query",
    description=(
        "Run a raw Tree-sitter structural query against one supported code file and return captures with source spans. "
        "This is syntax intelligence, not semantic LSP go-to-definition."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "query": {"type": "string", "description": "Tree-sitter query expression."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        "required": ["path", "query"],
        "additionalProperties": False,
    },
    acp_kind="search",
    capability="fs_read",
)

SHELL_SPEC = NativeToolSpec(
    name="shell",
    description=(
        "Execute a shell command. On Windows this uses PowerShell (pwsh or Windows PowerShell) with "
        "the selected cwd. The process is cancellable/steer-interruptible and time-bounded. Shell runs "
        "with the current OS user authority rather than acting as a filesystem sandbox, so it requires "
        "Crew permission by default. For file inspection prefer read/glob/grep/list/read_files; when a "
        "requested filesystem directory is outside the current session scope, those tools can trigger "
        "a Crew permission prompt for that directory. Do not use shell to bypass a directory access "
        "request that the user explicitly denied."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command text to execute."},
            "cwd": {"type": "string", "default": ".", "description": "Working directory inside an allowed root."},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 1800, "description": "Optional timeout override."},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    acp_kind="execute",
    capability="shell",
)

UPDATE_PLAN_SPEC = NativeToolSpec(
    name="update_plan",
    description=(
        "Track a bounded plan for the current ACP session only. replace sets at most 20 "
        "steps (pending/in_progress/completed); get reads the current plan. "
        "This does not schedule Crew tasks, grant tools, or persist across process restarts."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["replace", "get"]},
            "steps": {"type": "array", "maxItems": 20, "items": {
                "type": "object", "properties": {
                    "id": {"type": "string"}, "title": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                }, "required": ["id", "title", "status"], "additionalProperties": False,
            }},
        },
        "required": ["operation"], "additionalProperties": False,
    },
    acp_kind="other",
    capability="plan",
)

NATIVE_TOOL_SPECS: tuple[NativeToolSpec, ...] = (
    # Stable, compact filesystem discovery first; then richer read/write and web
    # tools.  Provider adapters preserve this order when serializing schemas.
    READ_SPEC,
    GLOB_SPEC,
    GREP_SPEC,
    READ_FILES_SPEC,
    LIST_DIRECTORY_SPEC,
    WRITE_SPEC,
    EDIT_SPEC,
    DELETE_FILE_SPEC,
    SHELL_SPEC,
    EXECUTE_SHELL_PROCESS_SPEC,
    CONTROL_SHELL_PROCESS_SPEC,
    CODE_SYMBOLS_SPEC,
    CODE_QUERY_SPEC,
    WEB_FETCH_SPEC,
    WEB_SEARCH_SPEC,
    UPDATE_PLAN_SPEC,
)

TOOL_ALIASES: dict[str, str] = {
    "fs_read": "read",
    "fsRead": "read",
    "read_file": "read",
    "readFile": "read",
    "readFiles": "read_files",
    "listDirectory": "list_directory",
    "fs_write": "write",
    "fsWrite": "write",
    "grep_search": "grep",
    "file_search": "glob",
    "deleteFile": "delete_file",
    "webFetch": "web_fetch",
    "webSearch": "web_search",
    "execute_bash": "shell",
    "execute_cmd": "shell",
    "execute_bash_process": "execute_shell_process",
    "control_bash_process": "control_shell_process",
}


def advertised_native_tool_names() -> list[str]:
    """Return the tools this installation can honestly advertise under current policy."""
    from .code_intelligence import tree_sitter_runtime_available

    policy = _load_native_policy()
    tree_sitter_ok = tree_sitter_runtime_available()
    return [
        spec.name
        for spec in NATIVE_TOOL_SPECS
        if (not policy.read_only or spec.capability in {"fs_read", "web_fetch", "web_search", "plan"})
        and (policy.web_enabled or spec.capability not in {"web_fetch", "web_search"})
        and (spec.name != "code_query" or tree_sitter_ok)
    ]


