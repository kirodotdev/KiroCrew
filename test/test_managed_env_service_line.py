"""A row never shows the guard's raw text. When the guard has stopped the client
("client stopped earlier in this run: ListSandboxes answered HTTP 503"), the List answer and every
row line read one plain line, with no HTTP code and no op name; the state reads unknown as before.
"""

from __future__ import annotations

import pytest
from managed_env_fake_bff import FakeBff
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_crew_rename import TAG, rename
from test_managed_env_crews_from_list import _env
from test_managed_env_routes import own

from kiro_crew.managed_env.guard import ClientStopped
from kiro_crew.managed_env.routes import SERVICE_TEXT, _short

PLAIN = "Can't reach the sandbox service right now. Restart the dashboard to try again."
SAFE = (
    "Crew stopped calling the sandbox service to keep your sandboxes safe. "
    "Restart the dashboard to try again."
)


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


def _raw_free(text: str) -> bool:
    raw = ("HTTP", "ListSandboxes", "client stopped", "503", "answered")
    return not any(w in text for w in raw)


def test_a_write_after_the_stop_reads_plainly_too(bff, tmp_path, monkeypatch):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    sid = own(bff, guard, reg, name=f"crew-sandbox-01-{TAG}", crew="crew-sandbox-01")
    routes.list_sync()
    guard.note_status("ListSandboxes", 503)
    status, body = rename(routes, sid, "my-crew")
    assert status == 200 and body == {"renamed": "no", "line": PLAIN}
    assert not any(r["op"] == "UpdateSandbox" for r in bff.requests)


@pytest.mark.parametrize("status", [401, 403])
def test_a_stop_not_from_the_service_reads_plainly_as_well(bff, tmp_path, monkeypatch, status):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    own(bff, guard, reg)
    guard.note_status("ListSandboxes", status)
    answer = routes.list_sync()
    assert answer["error"] == SERVICE_TEXT["stopped"] == SAFE and _raw_free(answer["error"])
    assert "401" not in answer["error"] and "403" not in answer["error"]


def test_the_guard_text_is_kept_for_the_log_only():
    raw = "client stopped earlier in this run: ListSandboxes answered HTTP 503"
    exc = ClientStopped("stopped", raw, by_service=True)
    assert "ListSandboxes answered HTTP 503" in str(exc)  # the guard and the log keep it
    assert _short(exc) == PLAIN
    assert _short(ClientStopped("stopped", raw)) == SAFE
    assert _short(ValueError("something else")) == "something else"  # other lines as before
