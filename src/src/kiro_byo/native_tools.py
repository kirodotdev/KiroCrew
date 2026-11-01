"""Kiro BYO native host-tool runtime.

The runtime is provider-neutral: tool metadata and execution live here, while a
provider adapter only translates schemas into its wire format.  Native tools use
Kiro CLI's canonical public names (read/write/glob/grep/shell) so prompts and
skills can move between the Kiro CLI and Kiro BYO backends without translation.

Filesystem access is governed by an app-owned execution policy (workspace +
allowed roots, read-only flag and sensitive-path denies). Crew alone owns approval
and authorization decisions for calls that survive these hard execution checks;
Kiro BYO does not maintain a second per-tool approval policy.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .config import home_dir
from .sandbox import SandboxError, WorkspaceSandboxBackend, create_sandbox
from .web_tools import SafeWebClient, WebToolError
from .process_manager import ProcessError, ProcessManager
from .code_intelligence import CodeIntelligenceEngine, CodeIntelligenceError


class NativeToolError(RuntimeError):
    pass


class ToolValidationError(NativeToolError):
    pass


class PermissionDecision(str, Enum):
    DENY = "deny"
    HOST_GATE = "host_gate"
    ALLOW = "allow"


from .native_specs import (
    NATIVE_TOOL_SPECS,
    TOOL_ALIASES,
    NativeRuntimePolicy,
    NativeToolSpec,
    advertised_native_tool_names,
    native_policy_path,
    _load_native_policy,
)

@dataclass(frozen=True)
class NativeInvocation:
    spec: NativeToolSpec
    arguments: dict[str, Any]


@dataclass(frozen=True)
class NativeToolResult:
    text: str
    touched_paths: tuple[str, ...] = ()
    exit_code: int = 0
    # Provider/session control is intentionally kept out of individual tool
    # schemas.  The runtime only reports that the *current invocation* yielded
    # to the ACP control plane.  TurnRunner decides whether that means ending
    # the turn (cancelled) or injecting a new user steer and replanning.
    interruption: str = ""


class NativePermissionEngine:
    """Harness-local policy seam before Crew's permission/hook gate.

    Read-only is a hard app execution boundary, not an approval policy. Every
    non-denied invocation returns through Crew's host gate so Side Chat, Crewmate
    Permission, hooks and Crew trust rules remain authoritative.
    """

    def __init__(self, policy: NativeRuntimePolicy) -> None:
        self.policy = policy

    def evaluate(self, invocation: NativeInvocation) -> PermissionDecision:
        if self.policy.read_only and invocation.spec.capability not in {"fs_read", "web_fetch", "web_search", "plan"}:
            return PermissionDecision.DENY
        # Approval is deliberately not configurable here. Side Chat and each
        # Crewmate's permission policy live at Crew's host boundary.
        return PermissionDecision.HOST_GATE


class AuditLogger:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    @staticmethod
    def _path() -> Path:
        return home_dir() / "data" / "audit" / "native_tools.jsonl"

    def record(
        self,
        *,
        session_id: str,
        call_id: str,
        tool: str,
        capability: str,
        permission: str,
        outcome: str,
        exit_code: int | None,
        touched_paths: tuple[str, ...] = (),
        detail: str = "",
    ) -> None:
        payload = {
            "ts": time.time(),
            "session_id": session_id,
            "call_id": call_id,
            "tool": tool,
            "capability": capability,
            "permission": permission,
            "outcome": outcome,
            "exit_code": exit_code,
            "touched_paths": list(touched_paths[:200]),
            "touched_path_count": len(touched_paths),
            "touched_paths_truncated": len(touched_paths) > 200,
        }
        if detail:
            payload["detail"] = detail[:1000]
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self._lock:
                with path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
        except OSError:
            # Audit is best-effort. A hardened deployment can choose to make
            # audit persistence fail closed independently of tool execution.
            return


class GitIgnoreMatcher:
    """Small no-dependency .gitignore subset for discovery tools.

    It intentionally handles the common path/glob/negation cases but is not sold
    as a byte-for-byte gitignore implementation.  The sandbox boundary remains
    authoritative even when ignore matching is imperfect.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.rules: list[tuple[bool, str, bool]] = []
        path = root / ".gitignore"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:]
            if not line:
                continue
            directory_only = line.endswith("/")
            line = line.rstrip("/").replace("\\", "/")
            self.rules.append((negated, line, directory_only))

    @staticmethod
    def _matches(rel: str, pattern: str) -> bool:
        rel = rel.replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        pattern = pattern[2:] if pattern.startswith("./") else pattern
        anchored = pattern.startswith("/")
        pattern = pattern.lstrip("/")
        candidates = [rel]
        if "/" not in pattern and not anchored:
            candidates.extend(part for part in rel.split("/") if part)
        for candidate in candidates:
            if fnmatch.fnmatchcase(candidate, pattern):
                return True
            if pattern.startswith("**/") and fnmatch.fnmatchcase(candidate, pattern[3:]):
                return True
            if not any(ch in pattern for ch in "*?[") and (
                candidate == pattern or candidate.startswith(pattern + "/")
            ):
                return True
        return False

    def ignored(self, path: Path, *, is_dir: bool = False) -> bool:
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return True
        if rel == ".git" or rel.startswith(".git/"):
            return True
        ignored = False
        for negated, pattern, directory_only in self.rules:
            if directory_only and not is_dir and "/" not in rel:
                continue
            if self._matches(rel, pattern):
                ignored = not negated
        return ignored


class NativeToolRuntime:
    MAX_TEXT_BYTES = 4 * 1024 * 1024
    MAX_LINE_CHARS = 4000

    def __init__(
        self,
        workspace_root: str | os.PathLike[str],
        *,
        additional_roots: tuple[str, ...] = (),
    ) -> None:
        self.policy = _load_native_policy()
        # ACP session roots are client-owned inputs and are merged with the
        # installation's hard policy roots. They do not bypass sensitive-path
        # guards and are re-supplied on every load/resume.
        effective_roots = tuple(self.policy.allowed_roots) + tuple(additional_roots)
        self.sandbox: WorkspaceSandboxBackend = create_sandbox(
            workspace_root,
            allowed_roots=effective_roots,
            read_only=self.policy.read_only,
            policy_path=native_policy_path(),
        )
        self.permission_engine = NativePermissionEngine(self.policy)
        self.audit = AuditLogger()
        self._plan_lock = threading.RLock()
        self._plan_steps: tuple[dict[str, str], ...] = ()
        self._code = CodeIntelligenceEngine(self.sandbox)
        active_specs = tuple(
            spec for spec in NATIVE_TOOL_SPECS
            if (not self.policy.read_only or spec.capability in {"fs_read", "web_fetch", "web_search", "plan"})
            and (self.policy.web_enabled or spec.capability not in {"web_fetch", "web_search"})
            and (spec.name != "code_query" or self._code.tree_sitter_available)
        )
        self._active_specs = active_specs
        self._specs = {spec.name: spec for spec in active_specs}
        self._ignore = GitIgnoreMatcher(self.sandbox.root)
        self._web = SafeWebClient(
            timeout_seconds=self.policy.web_timeout_seconds,
            max_bytes=self.policy.web_max_output_bytes,
        )
        self._processes = ProcessManager(
            max_processes=self.policy.process_max_concurrent,
            max_history=self.policy.process_max_history,
            buffer_bytes=self.policy.process_buffer_bytes,
        )

    @property
    def workspace_root(self) -> Path:
        return self.sandbox.root

    def specs(self) -> tuple[NativeToolSpec, ...]:
        return self._active_specs

    def policy_summary(self) -> dict[str, Any]:
        return {
            "read_only": self.policy.read_only,
            "allowed_roots": [str(root) for root in self.sandbox.allowed_roots],
            "policy_path": str(native_policy_path()),
            "web_enabled": self.policy.web_enabled,
            "process_max_concurrent": self.policy.process_max_concurrent,
            "process_max_history": self.policy.process_max_history,
            "process_buffer_bytes": self.policy.process_buffer_bytes,
            "code_intelligence": self._code.status(),
        }

    def tool_specs(
        self, names: set[str] | None = None, *, ui_language: str = "auto",
    ) -> list[dict[str, Any]]:
        selected = None if names is None else {self.canonical_name(name) for name in names}
        tools = [
            spec.to_tool_spec()
            for spec in self.specs()
            if selected is None or spec.name in selected
        ]
        # Tool schemas may vary by the session's UI language, without changing
        # tool identity or the runtime's canonical schema/validation rules.
        from .tool_display import display_locale
        from .i18n import locale_messages
        try:
            hint = locale_messages(display_locale(ui_language)).get("tool.description.hint", "")
        except (OSError, ValueError):
            hint = ""
        if hint:
            for tool in tools:
                props = tool["function"]["parameters"].get("properties", {})
                if "description" in props:
                    props["description"] = {**props["description"], "description": hint}
        return tools

    @staticmethod
    def canonical_name(name: str) -> str:
        return TOOL_ALIASES.get(name, name)

    def has_tool(self, name: str) -> bool:
        return self.canonical_name(name) in self._specs

    def kind_for(self, name: str) -> str:
        spec = self._specs.get(self.canonical_name(name))
        return spec.acp_kind if spec is not None else "other"

    def capability_for(self, name: str) -> str:
        spec = self._specs.get(self.canonical_name(name))
        return spec.capability if spec is not None else "unknown"

    def validate(self, name: str, arguments: dict[str, Any]) -> NativeInvocation:
        canonical = self.canonical_name(name)
        spec = self._specs.get(canonical)
        if spec is None:
            if self.policy.read_only and canonical in {"write", "edit", "delete_file", "shell", "execute_shell_process", "control_shell_process"}:
                raise ToolValidationError(
                    f"native policy is read-only; {canonical} is disabled by {native_policy_path()}"
                )
            raise ToolValidationError(f"unknown native tool: {name}")
        if not isinstance(arguments, dict):
            raise ToolValidationError("tool arguments must be an object")
        allowed = set(spec.input_schema.get("properties", {}))
        extra = sorted(set(arguments) - allowed)
        if extra:
            raise ToolValidationError(f"unexpected argument(s): {', '.join(extra)}")

        normalized = dict(arguments)
        if "description" in normalized:
            desc = normalized["description"]
            if not isinstance(desc, str) or len(desc) > 160:
                raise ToolValidationError("description must be text with at most 160 characters")
        if canonical == "read":
            self._require_nonempty_string(normalized, "path")
            normalized["offset"] = self._bounded_int(normalized.get("offset", 1), "offset", 1, 2_000_000_000)
            normalized["limit"] = self._bounded_int(normalized.get("limit", 400), "limit", 1, 2000)
        elif canonical == "read_files":
            paths = normalized.get("paths")
            if not isinstance(paths, list) or not paths or len(paths) > 20:
                raise ToolValidationError("paths must be an array containing 1 to 20 file paths")
            cleaned: list[str] = []
            for value in paths:
                if not isinstance(value, str) or not value.strip():
                    raise ToolValidationError("each paths item must be a non-empty string")
                cleaned.append(value)
            normalized["paths"] = cleaned
            normalized["offset"] = self._bounded_int(normalized.get("offset", 1), "offset", 1, 2_000_000_000)
            normalized["limit"] = self._bounded_int(normalized.get("limit", 200), "limit", 1, 1000)
        elif canonical == "list_directory":
            normalized["path"] = self._optional_string(normalized.get("path", "."), "path", ".")
            normalized["limit"] = self._bounded_int(normalized.get("limit", 400), "limit", 1, 2000)
        elif canonical == "write":
            self._require_nonempty_string(normalized, "path")
            if not isinstance(normalized.get("content"), str):
                raise ToolValidationError("content must be a string")
            normalized["create_parent"] = self._bool(normalized.get("create_parent", True), "create_parent")
        elif canonical == "edit":
            self._require_nonempty_string(normalized, "path")
            self._require_nonempty_string(normalized, "patch")
            if "@@" not in normalized["patch"]:
                raise ToolValidationError("patch must contain at least one unified-diff @@ hunk")
        elif canonical == "delete_file":
            self._require_nonempty_string(normalized, "path")
        elif canonical == "glob":
            self._require_nonempty_string(normalized, "pattern")
            normalized["path"] = self._optional_string(normalized.get("path", "."), "path", ".")
            normalized["limit"] = self._bounded_int(normalized.get("limit", 200), "limit", 1, 1000)
            self._validate_relative_pattern(normalized["pattern"])
        elif canonical == "grep":
            self._require_nonempty_string(normalized, "pattern")
            normalized["path"] = self._optional_string(normalized.get("path", "."), "path", ".")
            normalized["glob"] = self._optional_string(normalized.get("glob", "**/*"), "glob", "**/*")
            normalized["case_sensitive"] = self._bool(normalized.get("case_sensitive", True), "case_sensitive")
            normalized["max_results"] = self._bounded_int(normalized.get("max_results", 200), "max_results", 1, 1000)
            self._validate_relative_pattern(normalized["glob"])
            try:
                re.compile(normalized["pattern"])
            except re.error as exc:
                raise ToolValidationError(f"invalid regex: {exc}") from exc
        elif canonical == "shell":
            self._require_nonempty_string(normalized, "command")
            normalized["cwd"] = self._optional_string(normalized.get("cwd", "."), "cwd", ".")
            normalized["timeout_seconds"] = self._bounded_number(
                normalized.get("timeout_seconds", self.policy.shell_timeout_seconds),
                "timeout_seconds",
                1.0,
                1800.0,
            )
        elif canonical == "execute_shell_process":
            self._require_nonempty_string(normalized, "command")
            normalized["cwd"] = self._optional_string(normalized.get("cwd", "."), "cwd", ".")
            normalized["initial_wait_ms"] = self._bounded_int(
                normalized.get("initial_wait_ms", 120), "initial_wait_ms", 0, 2000
            )
        elif canonical == "control_shell_process":
            self._require_nonempty_string(normalized, "process_id")
            action = normalized.get("action")
            if action not in {"status", "read", "stdin", "terminate", "kill"}:
                raise ToolValidationError("action must be one of status/read/stdin/terminate/kill")
            normalized["input"] = self._optional_string(normalized.get("input", ""), "input", "")
            normalized["append_newline"] = self._bool(normalized.get("append_newline", False), "append_newline")
            normalized["max_chars"] = self._bounded_int(normalized.get("max_chars", 12000), "max_chars", 256, 50000)
            normalized["grace_seconds"] = self._bounded_number(
                normalized.get("grace_seconds", 2.0), "grace_seconds", 0.05, 10.0
            )
        elif canonical == "code_symbols":
            self._require_nonempty_string(normalized, "path")
            normalized["name"] = self._optional_string(normalized.get("name", ""), "name", "")
            normalized["max_results"] = self._bounded_int(normalized.get("max_results", 200), "max_results", 1, 1000)
        elif canonical == "code_query":
            self._require_nonempty_string(normalized, "path")
            self._require_nonempty_string(normalized, "query")
            normalized["max_results"] = self._bounded_int(normalized.get("max_results", 200), "max_results", 1, 1000)
        elif canonical == "update_plan":
            operation = normalized.get("operation")
            if operation not in {"replace", "get"}:
                raise ToolValidationError("operation must be replace or get")
            steps = normalized.get("steps", [])
            if not isinstance(steps, list):
                raise ToolValidationError("steps must be an array")
            if operation == "get" and ("steps" in normalized):
                raise ToolValidationError("get cannot include steps")
            if len(steps) > 20:
                raise ToolValidationError("plan exceeds 20 steps")
            cleaned = []
            seen: set[str] = set()
            for step in steps:
                if not isinstance(step, dict) or set(step) != {"id", "title", "status"}:
                    raise ToolValidationError("each step requires only id, title, status")
                sid, title, status = step["id"], step["title"], step["status"]
                if not isinstance(sid, str) or not (1 <= len(sid) <= 48) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
                    raise ToolValidationError("invalid plan step id")
                if sid in seen:
                    raise ToolValidationError("duplicate plan step id")
                if not isinstance(title, str) or not (1 <= len(title.strip()) <= 240):
                    raise ToolValidationError("invalid plan step title")
                if not isinstance(status, str) or status not in {"pending", "in_progress", "completed"}:
                    raise ToolValidationError("invalid plan step status")
                seen.add(sid)
                cleaned.append({"id": sid, "title": title.strip(), "status": status})
            if operation == "replace" and sum(x["status"] == "in_progress" for x in cleaned) > 1:
                raise ToolValidationError("only one step may be in_progress")
            normalized["steps"] = cleaned
        elif canonical == "web_fetch":
            self._require_nonempty_string(normalized, "url")
            normalized["max_chars"] = self._bounded_int(normalized.get("max_chars", 30000), "max_chars", 1000, 100000)
        elif canonical == "web_search":
            self._require_nonempty_string(normalized, "query")
            normalized["max_results"] = self._bounded_int(normalized.get("max_results", 8), "max_results", 1, 12)
        return NativeInvocation(spec=spec, arguments=normalized)

    def preflight(self, invocation: NativeInvocation) -> tuple[str, ...]:
        """Validate sandbox reachability before asking the host for consent.

        This performs no write, edit, or process launch.  It exists so an
        out-of-root or sensitive-path request fails *before* Crew asks the user
        to approve an operation that Kiro BYO can never execute.
        """
        name = invocation.spec.name
        args = invocation.arguments
        if name in {"read", "list_directory", "glob", "grep", "code_symbols", "code_query"}:
            path = self.sandbox.resolve_read_path(args.get("path", "."))
            return (self.sandbox.relative_display(path),)
        if name == "read_files":
            paths = tuple(self.sandbox.resolve_read_path(raw) for raw in args["paths"])
            for path in paths:
                if not path.is_file():
                    raise NativeToolError(f"read_files target is not a regular file: {self.sandbox.relative_display(path)}")
            return tuple(self.sandbox.relative_display(path) for path in paths)
        if name == "write":
            path = self.sandbox.resolve_write_path(args["path"])
            return (self.sandbox.relative_display(path),)
        if name == "edit":
            path = self.sandbox.resolve_write_path(args["path"])
            if not path.exists() or not path.is_file():
                raise NativeToolError(
                    f"edit target is not an existing regular file: {self.sandbox.relative_display(path)}"
                )
            return (self.sandbox.relative_display(path),)
        if name == "delete_file":
            path = self.sandbox.resolve_write_path(args["path"])
            if not path.exists() or not path.is_file():
                raise NativeToolError(
                    f"delete_file target is not an existing regular file: {self.sandbox.relative_display(path)}"
                )
            return (self.sandbox.relative_display(path),)
        if name in {"shell", "execute_shell_process"}:
            cwd = self.sandbox.resolve_cwd(args.get("cwd", "."))
            return (self.sandbox.relative_display(cwd),)
        if name == "control_shell_process":
            # The session-local ProcessManager is the ownership boundary.
            self._processes.get(args["process_id"])
            return (args["process_id"],)
        if name == "web_fetch":
            # DNS/SSRF validation is repeated at execution and on every redirect.
            # Keep WebToolError behind the NativeToolRuntime abstraction: TurnRunner
            # knows how to turn NativeToolError into a failed tool result, whereas
            # leaking WebToolError would incorrectly abort the whole ACP prompt.
            try:
                self._web._assert_public_url(args["url"])
            except WebToolError as exc:
                raise NativeToolError(str(exc)) from exc
            return (args["url"],)
        if name == "web_search":
            return (f"search:{args['query']}",)
        raise NativeToolError(f"unknown native tool during preflight: {name}")

    def required_scope_roots(self, invocation: NativeInvocation) -> tuple[Path, ...]:
        """Return out-of-scope directories required by one invocation.

        The result is advisory only: no scope is changed here. TurnRunner routes
        these roots through KiroCrew's ``session/request_permission`` surface and
        calls :meth:`grant_scope_roots` only after the user/host approves.
        """
        name = invocation.spec.name
        args = invocation.arguments
        requested: list[Path] = []

        def add(value: str, *, for_write: bool = False) -> None:
            root = self.sandbox.scope_root_for_path(value, for_write=for_write)
            if not self.sandbox.root_is_allowed(root):
                requested.append(root)

        if name in {"read", "list_directory", "glob", "grep", "code_symbols", "code_query"}:
            add(str(args.get("path", ".")))
        elif name == "read_files":
            for raw in args["paths"]:
                add(str(raw))
        elif name == "write":
            add(str(args["path"]), for_write=True)
        elif name in {"edit", "delete_file"}:
            add(str(args["path"]))
        elif name in {"shell", "execute_shell_process"}:
            add(str(args.get("cwd", ".")))

        deduped: list[Path] = []
        seen: set[str] = set()
        for root in requested:
            key = os.path.normcase(os.path.abspath(os.fspath(root)))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(root)
        return tuple(deduped)

    def grant_scope_roots(self, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        """Apply approved roots and return only roots newly added this time."""
        added: list[Path] = []
        for root in roots:
            resolved, was_added = self.sandbox.grant_root(root)
            if was_added:
                added.append(resolved)
        return tuple(added)

    def revoke_scope_roots(self, roots: tuple[Path, ...]) -> None:
        """Undo roots created for an ``allow_once`` permission selection."""
        for root in reversed(roots):
            self.sandbox.revoke_root(root)

    @staticmethod
    def _require_nonempty_string(args: dict[str, Any], key: str) -> None:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ToolValidationError(f"{key} must be a non-empty string")

    @staticmethod
    def _optional_string(value: Any, key: str, default: str) -> str:
        if value is None:
            return default
        if not isinstance(value, str):
            raise ToolValidationError(f"{key} must be a string")
        return value or default

    @staticmethod
    def _bounded_int(value: Any, key: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolValidationError(f"{key} must be an integer")
        if value < minimum or value > maximum:
            raise ToolValidationError(f"{key} must be between {minimum} and {maximum}")
        return value

    @staticmethod
    def _bounded_number(value: Any, key: str, minimum: float, maximum: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolValidationError(f"{key} must be a number")
        number = float(value)
        if number < minimum or number > maximum:
            raise ToolValidationError(f"{key} must be between {minimum:g} and {maximum:g}")
        return number

    @staticmethod
    def _bool(value: Any, key: str) -> bool:
        if not isinstance(value, bool):
            raise ToolValidationError(f"{key} must be a boolean")
        return value

    @staticmethod
    def _validate_relative_pattern(pattern: str) -> None:
        if "\x00" in pattern:
            raise ToolValidationError("glob patterns may not contain NUL")
        # The path/search-root argument is canonicalized separately and may point
        # at another allowed root.  Keep the wildcard itself relative so the glob
        # engine cannot enumerate one drive/root and filter after the fact.
        if pattern.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", pattern):
            raise ToolValidationError("glob patterns must be relative to the search path")
        if any(part == ".." for part in re.split(r"[\\/]", pattern)):
            raise ToolValidationError("glob patterns may not contain '..'; put traversal in path instead")

    def _ignored(self, path: Path, *, is_dir: bool) -> bool:
        try:
            path.relative_to(self.sandbox.root)
        except ValueError:
            return False
        return self._ignore.ignored(path, is_dir=is_dir)

    def _update_plan(self, args: dict[str, Any]) -> NativeToolResult:
        # One NativeToolRuntime belongs to one ACP session. This is an in-memory
        # progress board, never a second Crew task queue or permission grant.
        with self._plan_lock:
            if args["operation"] == "replace":
                self._plan_steps = tuple(dict(s) for s in args["steps"])
            steps = [dict(s) for s in self._plan_steps]
        return NativeToolResult(text=json.dumps({
            "scope": "current_session_only", "durable": False,
            "scheduled_by_crew": False, "steps": steps,
        }, ensure_ascii=False))

    def execute(
        self,
        invocation: NativeInvocation,
        *,
        cancel_event: threading.Event | None = None,
        interrupt_probe: Callable[[], str | None] | None = None,
    ) -> NativeToolResult:
        name = invocation.spec.name
        if name == "update_plan":
            return self._update_plan(invocation.arguments)
        if name == "read":
            return self._read(invocation.arguments)
        if name == "read_files":
            return self._read_files(invocation.arguments)
        if name == "list_directory":
            return self._list_directory(invocation.arguments)
        if name == "write":
            return self._write(invocation.arguments)
        if name == "edit":
            return self._edit(invocation.arguments)
        if name == "delete_file":
            return self._delete_file(invocation.arguments)
        if name == "glob":
            return self._glob(invocation.arguments)
        if name == "grep":
            return self._grep(invocation.arguments)
        if name == "shell":
            return self._shell(
                invocation.arguments,
                cancel_event=cancel_event,
                interrupt_probe=interrupt_probe,
            )
        if name == "execute_shell_process":
            return self._execute_shell_process(
                invocation.arguments,
                cancel_event=cancel_event,
                interrupt_probe=interrupt_probe,
            )
        if name == "control_shell_process":
            return self._control_shell_process(
                invocation.arguments,
                cancel_event=cancel_event,
                interrupt_probe=interrupt_probe,
            )
        if name == "code_symbols":
            try:
                payload = self._code.symbols(
                    invocation.arguments["path"],
                    name=invocation.arguments["name"],
                    max_results=invocation.arguments["max_results"],
                )
                return NativeToolResult(
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                    touched_paths=(str(payload["path"]),),
                )
            except CodeIntelligenceError as exc:
                raise NativeToolError(str(exc)) from exc
        if name == "code_query":
            try:
                payload = self._code.query(
                    invocation.arguments["path"],
                    invocation.arguments["query"],
                    max_results=invocation.arguments["max_results"],
                )
                return NativeToolResult(
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                    touched_paths=(str(payload["path"]),),
                )
            except CodeIntelligenceError as exc:
                raise NativeToolError(str(exc)) from exc
        if name == "web_fetch":
            try:
                return NativeToolResult(
                    text=self._web.fetch(invocation.arguments["url"], max_chars=invocation.arguments["max_chars"]),
                    touched_paths=(invocation.arguments["url"],),
                )
            except WebToolError as exc:
                raise NativeToolError(str(exc)) from exc
        if name == "web_search":
            try:
                return NativeToolResult(
                    text=self._web.search(invocation.arguments["query"], max_results=invocation.arguments["max_results"]),
                    touched_paths=(f"search:{invocation.arguments['query']}",),
                )
            except WebToolError as exc:
                raise NativeToolError(str(exc)) from exc
        raise NativeToolError(f"native tool has no executor: {name}")

    def _read_files(self, args: dict[str, Any]) -> NativeToolResult:
        sections: list[str] = []
        touched: list[str] = []
        for raw_path in args["paths"]:
            result = self._read({"path": raw_path, "offset": args["offset"], "limit": args["limit"]})
            display = result.touched_paths[0] if result.touched_paths else raw_path
            touched.extend(result.touched_paths)
            sections.append(f"===== {display} =====\n{result.text}")
        return NativeToolResult(text="\n\n".join(sections), touched_paths=tuple(touched))

    def _list_directory(self, args: dict[str, Any]) -> NativeToolResult:
        path = self.sandbox.resolve_read_path(args["path"])
        display = self.sandbox.relative_display(path)
        if not path.is_dir():
            raise NativeToolError(f"list_directory target is not a directory: {display}")
        entries: list[str] = []
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            raise NativeToolError(f"cannot list directory {display}: {exc}") from exc
        for child in children:
            if len(entries) >= args["limit"]:
                break
            if self._is_redirecting_directory(child) if child.is_dir() else child.is_symlink():
                continue
            try:
                resolved = child.resolve(strict=True)
            except OSError:
                continue
            if not self.sandbox.is_allowed_resolved(resolved):
                continue
            if self._ignored(resolved, is_dir=resolved.is_dir()):
                continue
            entries.append(self.sandbox.relative_display(resolved) + ("/" if resolved.is_dir() else ""))
        text = "\n".join(entries) if entries else "(empty directory)"
        if len(entries) >= args["limit"] and len(children) > len(entries):
            text += f"\n[truncated after {args['limit']} entries]"
        return NativeToolResult(text=text, touched_paths=(display,))

    def _delete_file(self, args: dict[str, Any]) -> NativeToolResult:
        if self.policy.read_only:
            raise NativeToolError("native policy is read-only; delete_file is disabled")
        path = self.sandbox.resolve_write_path(args["path"])
        display = self.sandbox.relative_display(path)
        if not path.exists() or not path.is_file():
            raise NativeToolError(f"delete_file target is not an existing regular file: {display}")
        try:
            path.unlink()
        except OSError as exc:
            raise NativeToolError(f"cannot delete {display}: {exc}") from exc
        return NativeToolResult(text=f"Deleted {display}", touched_paths=(display,))

    @staticmethod
    def _atomic_write(path: Path, content: str, *, create_parent: bool) -> None:
        parent = path.parent
        if create_parent:
            parent.mkdir(parents=True, exist_ok=True)
        if not parent.is_dir():
            raise NativeToolError(f"parent directory does not exist: {parent}")
        tmp_path: Path | None = None
        try:
            fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
            tmp_path = Path(raw_tmp)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, path)
            tmp_path = None
        except OSError as exc:
            raise NativeToolError(f"cannot write {path}: {exc}") from exc
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _write(self, args: dict[str, Any]) -> NativeToolResult:
        path = self.sandbox.resolve_write_path(args["path"])
        display = self.sandbox.relative_display(path)
        self._atomic_write(path, args["content"], create_parent=args["create_parent"])
        # Re-resolve after creation so a concurrent junction/symlink swap cannot
        # silently turn a successful result into a claim about an escaped target.
        resolved = self.sandbox.resolve_read_path(path)
        if resolved != path.resolve(strict=True):
            raise NativeToolError("written path changed during canonical verification")
        size = len(args["content"].encode("utf-8"))
        return NativeToolResult(
            text=f"Wrote {size} bytes to {display}",
            touched_paths=(display,),
        )

    _HUNK_RE = re.compile(
        r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+"
        r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@"
    )

    @classmethod
    def _apply_unified_patch(cls, original: str, patch: str) -> str:
        original_lines = original.splitlines()
        original_final_newline = original.endswith(("\n", "\r"))
        patch_lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out: list[str] = []
        cursor = 0
        i = 0
        hunks = 0
        while i < len(patch_lines):
            line = patch_lines[i]
            match = cls._HUNK_RE.match(line)
            if match is None:
                i += 1
                continue
            hunks += 1
            old_start = int(match.group("old_start"))
            old_count = int(match.group("old_count") or "1")
            target = max(0, old_start - 1)
            if target < cursor or target > len(original_lines):
                raise NativeToolError(f"patch hunk starts outside current file at old line {old_start}")
            out.extend(original_lines[cursor:target])
            cursor = target
            consumed = 0
            produced = 0
            i += 1
            while i < len(patch_lines) and not cls._HUNK_RE.match(patch_lines[i]):
                body = patch_lines[i]
                if body.startswith("\\ No newline at end of file"):
                    i += 1
                    continue
                if body.startswith(("diff --git ", "index ", "--- ", "+++ ")):
                    break
                if body == "" and i == len(patch_lines) - 1:
                    i += 1
                    break
                prefix = body[:1]
                content = body[1:] if prefix in {" ", "+", "-"} else body
                if prefix == " ":
                    if cursor >= len(original_lines) or original_lines[cursor] != content:
                        actual = original_lines[cursor] if cursor < len(original_lines) else "<EOF>"
                        raise NativeToolError(
                            f"patch context mismatch at line {cursor + 1}: expected {content!r}, found {actual!r}"
                        )
                    out.append(content)
                    cursor += 1
                    consumed += 1
                    produced += 1
                elif prefix == "-":
                    if cursor >= len(original_lines) or original_lines[cursor] != content:
                        actual = original_lines[cursor] if cursor < len(original_lines) else "<EOF>"
                        raise NativeToolError(
                            f"patch removal mismatch at line {cursor + 1}: expected {content!r}, found {actual!r}"
                        )
                    cursor += 1
                    consumed += 1
                elif prefix == "+":
                    out.append(content)
                    produced += 1
                else:
                    # A non-diff metadata line terminates this hunk.
                    break
                i += 1
            if consumed != old_count:
                raise NativeToolError(
                    f"patch hunk old-count mismatch: header says {old_count}, patch consumed {consumed}"
                )
        if hunks == 0:
            raise NativeToolError("patch contains no unified-diff hunks")
        out.extend(original_lines[cursor:])
        rendered = "\n".join(out)
        if original_final_newline:
            rendered += "\n"
        return rendered

    def _edit(self, args: dict[str, Any]) -> NativeToolResult:
        path = self.sandbox.resolve_write_path(args["path"])
        display = self.sandbox.relative_display(path)
        if not path.exists() or not path.is_file():
            raise NativeToolError(f"edit target is not an existing regular file: {display}")
        if path.stat().st_size > self.MAX_TEXT_BYTES:
            raise NativeToolError(f"file is too large to edit ({path.stat().st_size} bytes)")
        with path.open("rb") as handle:
            if b"\x00" in handle.read(4096):
                raise NativeToolError("binary files are not supported by edit")
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise NativeToolError(f"cannot read edit target {display}: {exc}") from exc
        rendered = self._apply_unified_patch(original, args["patch"])
        self._atomic_write(path, rendered, create_parent=False)
        return NativeToolResult(
            text=f"Applied patch to {display}",
            touched_paths=(display,),
        )

    @staticmethod
    def _shell_argv(command: str) -> list[str]:
        if os.name == "nt":
            executable = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
            if not executable:
                raise NativeToolError("PowerShell was not found (pwsh.exe or powershell.exe)")
            return [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ]
        shell = os.environ.get("SHELL") or "/bin/sh"
        return [shell, "-lc", command]

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                return
            except Exception:
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                return
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.kill()
        except OSError:
            pass

    def _format_shell_result(self, stdout: bytes, stderr: bytes, exit_code: int, *, suffix: str = "") -> str:
        limit = self.policy.shell_max_output_bytes
        combined_size = len(stdout) + len(stderr)
        if combined_size > limit:
            # Split the budget between channels while keeping both diagnostic tails.
            half = max(1024, limit // 2)
            stdout = stdout[-half:]
            stderr = stderr[-half:]
            truncated = True
        else:
            truncated = False
        out_text = stdout.decode("utf-8", errors="replace").rstrip()
        err_text = stderr.decode("utf-8", errors="replace").rstrip()
        parts: list[str] = []
        if out_text:
            parts.append(out_text)
        if err_text:
            parts.append("[stderr]\n" + err_text)
        if not parts:
            parts.append("(no output)")
        if truncated:
            parts.append(f"[output truncated to approximately {limit} bytes]")
        if suffix:
            parts.append(suffix)
        parts.append(f"[exit_code={exit_code}]")
        return "\n".join(parts)

    def _shell(
        self,
        args: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
        interrupt_probe: Callable[[], str | None] | None = None,
    ) -> NativeToolResult:
        if self.policy.read_only:
            raise NativeToolError("native policy is read-only; shell is disabled")
        cwd = self.sandbox.resolve_cwd(args["cwd"])
        display = self.sandbox.relative_display(cwd)
        argv = self._shell_argv(args["command"])
        timeout = float(args["timeout_seconds"])
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": dict(os.environ),
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc: subprocess.Popen[bytes] = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise NativeToolError(f"failed to start shell: {exc}") from exc

        deadline = time.monotonic() + timeout
        while True:
            interruption = self._native_interruption(cancel_event, interrupt_probe)
            if interruption:
                self._kill_process_tree(proc)
                stdout, stderr = proc.communicate()
                suffix = (
                    "[interrupted by user steering]"
                    if interruption == "steered"
                    else "[cancelled by host]"
                )
                return NativeToolResult(
                    text=self._format_shell_result(stdout, stderr, 130, suffix=suffix),
                    touched_paths=(display,),
                    exit_code=130,
                    interruption=interruption,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_process_tree(proc)
                stdout, stderr = proc.communicate()
                return NativeToolResult(
                    text=self._format_shell_result(stdout, stderr, 124, suffix=f"[timed out after {timeout:g}s]"),
                    touched_paths=(display,),
                    exit_code=124,
                )
            try:
                stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        return NativeToolResult(
            text=self._format_shell_result(stdout, stderr, int(proc.returncode or 0)),
            touched_paths=(display,),
            exit_code=int(proc.returncode or 0),
        )

    def _execute_shell_process(
        self,
        args: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
        interrupt_probe: Callable[[], str | None] | None = None,
    ) -> NativeToolResult:
        if self.policy.read_only:
            raise NativeToolError("native policy is read-only; long-running shell processes are disabled")
        interruption = self._native_interruption(cancel_event, interrupt_probe)
        if interruption:
            return NativeToolResult(
                text=(
                    "Process launch interrupted by user steering."
                    if interruption == "steered"
                    else "Process launch cancelled by host."
                ),
                exit_code=130,
                interruption=interruption,
            )
        cwd = self.sandbox.resolve_cwd(args["cwd"])
        display = self.sandbox.relative_display(cwd)
        try:
            handle = self._processes.start(
                self._shell_argv(args["command"]),
                command=args["command"],
                cwd=cwd,
                # Wait cooperatively below so a steer does not leave the model
                # stuck for the full startup grace before it can replan.
                initial_wait_ms=0,
            )
            wait_seconds = min(max(float(args["initial_wait_ms"]) / 1000.0, 0.0), 2.0)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                interruption = self._native_interruption(cancel_event, interrupt_probe)
                if interruption:
                    self._processes.terminate(handle.process_id, force=True, grace_seconds=0.05)
                    snapshot = self._processes.snapshot(handle.process_id)
                    snapshot["cwd"] = display
                    snapshot["interruption"] = interruption
                    return NativeToolResult(
                        text=json.dumps(snapshot, ensure_ascii=False, indent=2),
                        touched_paths=(display,),
                        exit_code=130,
                        interruption=interruption,
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            snapshot = self._processes.snapshot(handle.process_id)
        except ProcessError as exc:
            raise NativeToolError(str(exc)) from exc
        snapshot["cwd"] = display
        return NativeToolResult(
            text=json.dumps(snapshot, ensure_ascii=False, indent=2),
            touched_paths=(display,),
            exit_code=0 if snapshot["state"] == "running" else int(snapshot.get("exit_code") or 0),
        )

    def _control_shell_process(
        self,
        args: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
        interrupt_probe: Callable[[], str | None] | None = None,
    ) -> NativeToolResult:
        process_id = args["process_id"]
        action = args["action"]
        interruption = self._native_interruption(cancel_event, interrupt_probe)
        if interruption:
            return NativeToolResult(
                text=(
                    "Process control interrupted by user steering."
                    if interruption == "steered"
                    else "Process control cancelled by host."
                ),
                touched_paths=(process_id,),
                exit_code=130,
                interruption=interruption,
            )
        try:
            if action == "stdin":
                self._processes.send_stdin(
                    process_id, args["input"], append_newline=args["append_newline"]
                )
            elif action == "terminate":
                self._processes.terminate(
                    process_id, force=False, grace_seconds=args["grace_seconds"]
                )
            elif action == "kill":
                self._processes.terminate(process_id, force=True, grace_seconds=0.05)
            snapshot = self._processes.snapshot(process_id, max_chars=args["max_chars"])
        except ProcessError as exc:
            raise NativeToolError(str(exc)) from exc
        return NativeToolResult(
            text=json.dumps(snapshot, ensure_ascii=False, indent=2),
            touched_paths=(process_id,),
            exit_code=int(snapshot["exit_code"] or 0) if snapshot["state"] != "running" else 0,
        )

    @staticmethod
    def _native_interruption(
        cancel_event: threading.Event | None,
        interrupt_probe: Callable[[], str | None] | None,
    ) -> str:
        """Return the current ACP interruption reason, with cancel winning.

        ``cancel_event`` remains supported for callers/tests that predate the
        steer-aware probe.  A probe may return ``"cancelled"`` or ``"steered"``;
        any other truthy value is normalized to ``"cancelled"`` so tool code
        never has to understand arbitrary control-plane vocabulary.
        """
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        if interrupt_probe is None:
            return ""
        reason = interrupt_probe()
        if not reason:
            return ""
        normalized = str(reason).strip().lower()
        if normalized == "steered":
            return "steered"
        return "cancelled"

    def close(self) -> None:
        """Release session-owned OS resources. Safe to call repeatedly."""
        self._processes.close()

    def _read(self, args: dict[str, Any]) -> NativeToolResult:
        path = self.sandbox.resolve_read_path(args["path"])
        display = self.sandbox.relative_display(path)
        limit = args["limit"]
        if path.is_dir():
            entries: list[str] = []
            try:
                children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError as exc:
                raise NativeToolError(f"cannot list directory {display}: {exc}") from exc
            for child in children:
                if len(entries) >= limit:
                    break
                try:
                    resolved = child.resolve(strict=True)
                except OSError:
                    continue
                if not self.sandbox.is_allowed_resolved(resolved):
                    continue
                if self._ignored(resolved, is_dir=resolved.is_dir()):
                    continue
                suffix = "/" if resolved.is_dir() else ""
                entries.append(self.sandbox.relative_display(resolved) + suffix)
            truncated = len(children) > len(entries) and len(entries) >= limit
            text = "\n".join(entries) if entries else "(empty directory)"
            if truncated:
                text += f"\n[truncated after {limit} entries]"
            return NativeToolResult(text=text, touched_paths=(display,))
        if not path.is_file():
            raise NativeToolError(f"not a regular file: {display}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise NativeToolError(f"cannot stat {display}: {exc}") from exc
        if size > self.MAX_TEXT_BYTES:
            raise NativeToolError(
                f"file is too large for native read ({size} bytes > {self.MAX_TEXT_BYTES})"
            )
        try:
            with path.open("rb") as fh:
                prefix = fh.read(4096)
            if b"\x00" in prefix:
                raise NativeToolError("binary files are not supported by native read")
            offset = args["offset"]
            out: list[str] = []
            last_line = offset - 1
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for number, line in enumerate(fh, 1):
                    if number < offset:
                        continue
                    if len(out) >= limit:
                        break
                    clean = line.rstrip("\r\n")
                    if len(clean) > self.MAX_LINE_CHARS:
                        clean = clean[: self.MAX_LINE_CHARS] + " …[line truncated]"
                    out.append(f"{number}: {clean}")
                    last_line = number
        except OSError as exc:
            raise NativeToolError(f"cannot read {display}: {exc}") from exc
        text = "\n".join(out)
        if not out:
            text = f"(no lines at or after {offset})"
        elif len(out) >= limit:
            text += f"\n[read {offset}-{last_line}; use offset={last_line + 1} to continue]"
        return NativeToolResult(text=text, touched_paths=(display,))

    @staticmethod
    def _match_glob(rel: str, pattern: str) -> bool:
        rel = rel.replace("\\", "/")
        pattern = pattern.replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        if pattern.startswith("./"):
            pattern = pattern[2:]
        return fnmatch.fnmatchcase(rel, pattern) or (
            pattern.startswith("**/") and fnmatch.fnmatchcase(rel, pattern[3:])
        )

    @staticmethod
    def _is_redirecting_directory(path: Path) -> bool:
        # os.walk(followlinks=False) is enough for POSIX symlinks, but Windows
        # directory junction/reparse behavior differs across Python versions.
        # Comparing realpath with the lexical absolute path lets discovery prune
        # redirecting directories before traversal on both platforms.
        try:
            lexical = os.path.normcase(os.path.abspath(os.fspath(path)))
            physical = os.path.normcase(os.path.realpath(os.fspath(path)))
        except OSError:
            return True
        return lexical != physical

    def _iter_discovery_paths(self, base: Path, *, include_dirs: bool) -> Any:
        """Walk a canonical base while pruning ignored and redirecting dirs.

        This is deliberately stricter than globbing first and filtering later:
        no ignored .git/node_modules-style subtree, symlink, or junction needs to
        be enumerated before the workspace sandbox gets a chance to reject it.
        """
        base = self.sandbox.resolve_read_path(base)
        if base.is_file():
            if not self._ignored(base, is_dir=False):
                yield base
            return
        if not base.is_dir():
            return

        for current, dirnames, filenames in os.walk(base, followlinks=False):
            current_path = Path(current)
            kept_dirs: list[str] = []
            for dirname in sorted(dirnames, key=str.lower):
                child = current_path / dirname
                if self._is_redirecting_directory(child):
                    continue
                try:
                    resolved = child.resolve(strict=True)
                except OSError:
                    continue
                if not self.sandbox.is_allowed_resolved(resolved):
                    continue
                if self._ignored(resolved, is_dir=True):
                    continue
                kept_dirs.append(dirname)
                if include_dirs:
                    yield resolved
            dirnames[:] = kept_dirs

            for filename in sorted(filenames, key=str.lower):
                child = current_path / filename
                if child.is_symlink():
                    continue
                if not self.sandbox.is_allowed_resolved(child):
                    continue
                try:
                    resolved = child.resolve(strict=True)
                except OSError:
                    continue
                if self._ignored(resolved, is_dir=False):
                    continue
                yield resolved

    def _glob(self, args: dict[str, Any]) -> NativeToolResult:
        base = self.sandbox.resolve_read_path(args["path"])
        if not base.is_dir():
            raise NativeToolError("glob path must be a directory")
        pattern = args["pattern"]
        limit = args["limit"]
        matches: list[str] = []
        touched: list[str] = [self.sandbox.relative_display(base)]
        for path in self._iter_discovery_paths(base, include_dirs=True):
            rel_from_base = path.relative_to(base).as_posix()
            if self._match_glob(rel_from_base, pattern):
                shown = self.sandbox.relative_display(path)
                if path.is_dir():
                    shown += "/"
                matches.append(shown)
                if len(matches) >= limit:
                    break
        matches.sort(key=str.lower)
        text = "\n".join(matches) if matches else "(no matches)"
        if len(matches) >= limit:
            text += f"\n[truncated after {limit} matches]"
        return NativeToolResult(text=text, touched_paths=tuple(touched))

    def _grep(self, args: dict[str, Any]) -> NativeToolResult:
        base = self.sandbox.resolve_read_path(args["path"])
        flags = 0 if args["case_sensitive"] else re.IGNORECASE
        try:
            regex = re.compile(args["pattern"], flags)
        except re.error as exc:
            raise NativeToolError(f"invalid regex: {exc}") from exc
        file_glob = args["glob"]
        max_results = args["max_results"]
        matches: list[str] = []
        touched: list[str] = []

        candidates = [base] if base.is_file() else self._iter_discovery_paths(base, include_dirs=False)
        for path in candidates:
            if len(matches) >= max_results:
                break
            if not path.is_file():
                continue
            if self._ignored(path, is_dir=False):
                continue
            if base.is_dir():
                rel_filter = path.relative_to(base).as_posix()
                if not self._match_glob(rel_filter, file_glob):
                    continue
            display = self.sandbox.relative_display(path)
            try:
                size = path.stat().st_size
                if size > self.MAX_TEXT_BYTES:
                    continue
                with path.open("rb") as fh:
                    if b"\x00" in fh.read(4096):
                        continue
                touched.append(display)
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for number, line in enumerate(fh, 1):
                        if regex.search(line):
                            clean = line.rstrip("\r\n")
                            if len(clean) > self.MAX_LINE_CHARS:
                                clean = clean[: self.MAX_LINE_CHARS] + " …[line truncated]"
                            matches.append(f"{display}:{number}: {clean}")
                            if len(matches) >= max_results:
                                break
            except OSError:
                continue

        text = "\n".join(matches) if matches else "(no matches)"
        if len(matches) >= max_results:
            text += f"\n[truncated after {max_results} matches]"
        return NativeToolResult(text=text, touched_paths=tuple(touched))
