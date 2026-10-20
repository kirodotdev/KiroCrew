"""The SEL recorder and the fake ``urlopen`` response the ``cli_server`` coverage files share.

``sel_rec`` replaces ``cli_server.sel`` with a ``_SelRecorder`` that keeps every audit
call, so a test asserts on the audit record directly; ``_Resp`` is a minimal ``urlopen``
result usable as a context manager. ``test_cli_server_more_coverage.py`` and its
``_client`` and ``_lifecycle`` siblings import what they use of these by name.
"""

import pytest

from kiro_crew import cli_server


class _SelRecorder:
    """Stand-in for :func:`kiro_crew.sel.sel` that records audit calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_api_access(self, **kw) -> None:
        self.calls.append(kw)

    @property
    def operations(self) -> list[str]:
        return [c.get("operation", "") for c in self.calls]


@pytest.fixture(name="sel_rec")
def sel_rec_fixture(monkeypatch):
    rec = _SelRecorder()
    monkeypatch.setattr(cli_server, "sel", lambda: rec)
    return rec


class _Resp:
    """Minimal ``urlopen`` result usable as a context manager."""

    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._payload if n is None or n < 0 else self._payload[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False
