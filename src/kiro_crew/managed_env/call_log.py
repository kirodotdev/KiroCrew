"""A log of every managed-environment service call the client makes: op, status, field names. Never values.

One JSON line per call, appended to a 0600 file (``bff-calls.log`` in the sandbox state
directory, or the path in ``KIROCREW_SBX_CALL_LOG``). A line holds the UTC time, the
operation, the HTTP status (``null`` when there was no answer), the error kind, the
*names* of the request and answer fields (``sandboxes[].status``, nested to depth 3), and
``guard``: the client guard's mode word, ``product``. That word is a constant of
the process, set when the guard is built, never a value from the wire or the request.
No value, token, id, ssh tuple, stream URL or message text is ever written: only keys.
A write failure is swallowed, so the log can never break a call.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

ENV_PATH = "KIROCREW_SBX_CALL_LOG"
FILE_NAME = "bff-calls.log"
_MAX_DEPTH = 3
_MAX_NAMES = 64
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
#: The ``guard`` word; anything else is written as ``product``, never as given.
GUARD_PRODUCT = "product"


def field_names(obj: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Sorted key paths of *obj* (lists as ``name[]``); leaf values are never read out."""
    out: set[str] = set()

    def walk(o: Any, pre: str, d: int) -> None:
        if d >= _MAX_DEPTH:
            return
        if isinstance(o, dict):
            for k in o:
                name = str(k) if _NAME_RE.match(str(k)) else "<key>"
                path = f"{pre}.{name}" if pre else name
                out.add(path)
                walk(o[k], path, d + 1)
        elif isinstance(o, list):
            for item in o[:20]:
                walk(item, pre + "[]", d)

    walk(obj, prefix, depth)
    return sorted(out)[:_MAX_NAMES]


class CallLog:
    def __init__(self, path: str | os.PathLike[str], *, clock=time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        op: str,
        status: int | None,
        *,
        kind: str = "",
        request: Any = None,
        response: Any = None,
        guard: str = GUARD_PRODUCT,
    ) -> None:
        line = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
            "op": op,
            "status": status,
            "kind": kind if _NAME_RE.match(kind or "") else ("" if not kind else "<kind>"),
            "request_fields": field_names(request),
            "response_fields": field_names(response),
            "guard": GUARD_PRODUCT,
        }
        try:
            with self._lock:
                self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError:
            pass


def from_env(state_dir: str | os.PathLike[str]) -> CallLog:
    return CallLog(os.environ.get(ENV_PATH) or Path(state_dir) / FILE_NAME)
