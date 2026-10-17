"""Structured runtime event ledger for Kiro BYO.

TurnTelemetry remains the small, content-free performance ledger.  This module
adds a separate bounded event stream for users who need to inspect the harness
from ACP ingress through provider rounds and tool execution.

Trace mode is controlled by the ``KIRO_BYO_RUNTIME_TRACE`` environment variable:

``metadata`` (default)
    Record event names, routing/runtime metadata and payload shapes/counts.
``full``
    Additionally retain prompt/message/tool/result payloads after recursive
    credential redaction.  This may contain workspace/user content and is meant
    for local debugging only.
``off``
    Disable the runtime ledger.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from .paths import app_home

_TRACE_ENV = "KIRO_BYO_RUNTIME_TRACE"
_VALID_MODES = frozenset({"off", "metadata", "full"})
_SECRET_KEYS = frozenset({
    "api_key", "apikey", "authorization", "proxy-authorization", "x-api-key",
    "token", "access_token", "refresh_token", "password", "secret", "credential",
    "credentials", "cookie", "set-cookie",
})


def trace_mode() -> str:
    raw = os.environ.get(_TRACE_ENV, "metadata").strip().lower()
    return raw if raw in _VALID_MODES else "metadata"


def _redact(value: Any, *, key: str = "") -> Any:
    if key.lower() in _SECRET_KEYS:
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _shape(value: Any) -> Any:
    """Return a content-free structural summary suitable for metadata mode."""
    if isinstance(value, Mapping):
        return {str(k): _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return {"type": "list", "length": len(value)}
    if isinstance(value, tuple):
        return {"type": "tuple", "length": len(value)}
    if isinstance(value, str):
        return {"type": "str", "chars": len(value)}
    if value is None:
        return None
    return {"type": type(value).__name__}


class RuntimeTraceStore:
    """Bounded, thread-safe JSONL runtime event stream."""

    DEFAULT_MAX_RECORDS = 2_000
    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Path | None = None, *, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        self.path = path or (app_home() / "data" / "runtime_trace" / "events.jsonl")
        self.max_records = max(100, min(int(max_records), 20_000))
        try:
            key = str(self.path.resolve())
        except OSError:
            key = str(self.path)
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.Lock())

    @property
    def mode(self) -> str:
        return trace_mode()

    def record(
        self,
        event: str,
        *,
        session_id: str = "",
        data: Mapping[str, Any] | None = None,
        content: Mapping[str, Any] | None = None,
    ) -> None:
        mode = self.mode
        if mode == "off":
            return
        payload: dict[str, Any] = {
            "timestamp": time.time(),
            "event": str(event),
            "session_id": str(session_id or ""),
            "mode": mode,
            "data": _redact(dict(data or {})),
        }
        if content:
            payload["content"] = _redact(dict(content)) if mode == "full" else _shape(content)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
            self._trim_locked()

    def _trim_locked(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                tail = deque(handle, maxlen=self.max_records + 1)
        except OSError:
            return
        if len(tail) <= self.max_records:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.writelines(list(tail)[-self.max_records:])
            tmp.replace(self.path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def recent(self, limit: int = 100, *, session_id: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if not self.path.exists():
            return []
        # Read a larger bounded tail when filtering by session so sparse session
        # events are still discoverable without reading an unbounded file.
        scan_limit = min(self.max_records, max(limit, limit * 8))
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    raw_lines = deque(handle, maxlen=scan_limit)
            except OSError:
                return []
        out: list[dict[str, Any]] = []
        for line in raw_lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if session_id and str(item.get("session_id") or "") != session_id:
                continue
            out.append(item)
        return out[-limit:]

    def clear(self) -> int:
        with self._lock:
            if not self.path.exists():
                return 0
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    count = sum(1 for _ in handle)
            except OSError:
                count = 0
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                try:
                    self.path.write_text("", encoding="utf-8")
                except OSError:
                    return 0
            return count

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "retention_limit": self.max_records,
            "path": str(self.path),
            "full_content_warning": self.mode == "full",
            "env": _TRACE_ENV,
        }
