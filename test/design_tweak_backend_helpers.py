"""Fixtures and doubles shared by the ``test_design_tweak_backend*.py`` modules.

The private queue directory and the socketless JSON handler.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps.builtins.design_tweak.backend import server


class _JsonHandler(server.Handler):
    """A `Handler` with no socket, capturing `_json` responses.

    Same trick as `_StubHandler` (never call `BaseHTTPRequestHandler.__init__`,
    which would start serving) but for the JSON API methods rather than the raw
    byte responders. `_cached_body` is what `_read_body` parses.
    """

    def __init__(self, body: bytes = b"", path: str = "/") -> None:
        self.path = path
        self._cached_body = body
        self.json_sent: list[tuple[int, Any]] = []

    def _json(self, code: int, payload) -> None:
        self.json_sent.append((code, payload))


@pytest.fixture(name="isolated_queue")
def isolated_queue_fixture(tmp_path, monkeypatch):
    """Point the module's data dirs at a private tmp tree.

    Necessary rather than tidy: `_open_draft_file` and `_next_number` scan EVERY
    pending file, so a test that submits into the real queue would both attach to
    the developer's own open drafts and leave files behind. Patched as module
    globals because the containment barrier and both scanners read them at call
    time.
    """
    data = (tmp_path / "data").resolve()
    queue = data / "queue"
    handled = data / "handled"
    queue.mkdir(parents=True)
    handled.mkdir(parents=True)
    monkeypatch.setattr(server, "DATA_DIR", data)
    monkeypatch.setattr(server, "QUEUE_DIR", queue)
    monkeypatch.setattr(server, "HANDLED_DIR", handled)
    # Module state that `_h_submit` reads; pinned so an earlier test cannot leak in.
    monkeypatch.setattr(server, "_ROOT", "")
    monkeypatch.setattr(server, "_TARGET", "")
    monkeypatch.setitem(server._CFG, "projects", [])
    return queue
