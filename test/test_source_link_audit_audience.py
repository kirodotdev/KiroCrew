"""Only a projection a non-owner dashboard user receives audits its public-status decision.

``_project_source_links`` writes one ``source_link_public_status`` SEL row per
change link when it decides for a non-owner dashboard user
(``dashboard_user=True``): ``allowed`` for a repo known public, ``denied``
otherwise. A pass that serves the owner, or whose source links nobody
receives, makes no decision for a non-owner, so it writes no such row. A real
non-owner projection keeps both halves: a private link's status stays hidden
and its denial is still recorded.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

import kiro_crew.dashboard.state as state_mod
from kiro_crew.dashboard.handlers import crew_workers
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.websocket_hub import SLOT_DIFF_WS_FLAG, SLOT_PATCH_WS_FLAG

PUBLIC = "https://github.com/acme/public/pull/1"
PRIVATE = "https://github.com/acme/private/pull/2"


@pytest.fixture(name="audits")
def _audits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The ``(outcome, url)`` of every public-status SEL row the test writes."""
    rows: list[tuple[str, str]] = []

    class _Sel:
        def log_api_access(self, **kw: Any) -> None:
            if kw.get("operation") == "source_link_public_status":
                rows.append((str(kw["outcome"]), str(kw["resources"])))

    monkeypatch.setattr(state_mod, "sel", lambda: _Sel())
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers._session_card_chips_snapshot", True
    )
    monkeypatch.setattr(
        state_mod, "_cached_check_status", lambda url: {"ci": "passed", "state": "OPEN"}
    )
    monkeypatch.setattr(state_mod, "_repo_is_public", lambda url: url == PUBLIC)
    tables = (state_mod._PUBLIC_STATUS_GRANT_AUDIT, state_mod._PUBLIC_STATUS_DENY_AUDIT)
    for table in tables:
        table.clear()
    yield rows
    for table in tables:
        table.clear()


@pytest.fixture(name="state")
def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, audits: list) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = DashboardState(
        sessions=MagicMock(count=0), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    monkeypatch.setattr(state, "is_yolo_active", lambda: False)
    slot = state.get_or_create_slot("chat-a")
    slot.append("assistant", f"opened {PUBLIC} and {PRIVATE}", "msg")
    # Building the slot ran passes of its own; count only what the test runs.
    audits.clear()
    for table in (state_mod._PUBLIC_STATUS_GRANT_AUDIT, state_mod._PUBLIC_STATUS_DENY_AUDIT):
        table.clear()
    return state


class _Socket:
    """An open dashboard socket that records the frames sent to it."""

    def __init__(self, **flags: Any) -> None:
        self.closed = False
        self.frames: list[dict] = []
        self._flags = {"_is_dashboard_user": True, **flags}

    def get(self, key: str, default: Any = None) -> Any:
        return self._flags.get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        self._flags[key] = value


def _capture(state: DashboardState, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, msg: ws.frames.append(json.loads(msg)))


def _chips(frame: dict) -> dict[str, dict]:
    assert frame["type"] == "slots"
    return {link["url"]: link for row in frame["data"] for link in row.get("source_links", [])}


@pytest.mark.asyncio
async def test_owner_crew_workers_read_writes_no_public_status_row(
    state: DashboardState, audits: list
) -> None:
    # Every crew-workers route (list, approve, timeline, steer) reads its rows
    # here, behind the owner gate, and none of them sends a source link.
    rows = await crew_workers._stamped_rows(state)
    assert [row["key"] for row in rows] == ["chat-a"]
    assert audits == []


@pytest.mark.parametrize("field", ["title", "pinned"])
def test_slot_patch_writes_no_public_status_row(
    state: DashboardState, audits: list, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # A patch row carries no ``source_links``, so no decision reaches anyone. A
    # title is projected before the audience checks; any other field only once
    # a patch socket is open.
    _capture(state, monkeypatch)
    owner = _Socket(**{SLOT_PATCH_WS_FLAG: True})
    state.register_ws(owner, owner=True)  # type: ignore[arg-type]
    state.push_slot_patch("chat-a", [field])
    assert owner.frames[-1]["type"] == "slot_patch"
    assert audits == []


def test_broadcast_with_only_the_owner_watching_writes_no_public_status_row(
    state: DashboardState, audits: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture(state, monkeypatch)
    owner = _Socket()
    state.register_ws(owner, owner=True)  # type: ignore[arg-type]
    state._do_slots_broadcast()
    assert audits == []
    # The owner still sees status on every link, private included.
    chips = _chips(owner.frames[-1])
    assert chips[PRIVATE]["ci"] == "passed"
    assert chips[PUBLIC]["ci"] == "passed"


def test_broadcast_with_nobody_watching_writes_no_public_status_row(
    state: DashboardState, audits: list
) -> None:
    state._do_slots_broadcast()
    assert audits == []


def test_broadcast_to_an_app_socket_only_writes_no_public_status_row(
    state: DashboardState, audits: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``filter_slots_for_app`` strips every field the dashboard-user view adds,
    # so an app socket reads the bare list and no non-owner decision is made.
    _capture(state, monkeypatch)
    # Admit the frame past the app's event-scope gate, which is not under test.
    monkeypatch.setattr(state, "_ws_client_allowed", lambda ws, msg_type, data, *a: True)
    app = _Socket(_is_dashboard_user=False, _app="notes")
    state.register_ws(app)  # type: ignore[arg-type]
    state._do_slots_broadcast()
    assert app.frames[-1]["type"] == "slots"
    assert audits == []


def test_legacy_only_broadcast_skips_a_patch_socket_and_writes_no_public_status_row(
    state: DashboardState, audits: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner's legacy tab is owed the full list after a patch; the teammate's
    # patch socket already applied the patch and is skipped by that list.
    _capture(state, monkeypatch)
    legacy_owner, teammate = _Socket(), _Socket(**{SLOT_PATCH_WS_FLAG: True})
    state.register_ws(legacy_owner, owner=True)  # type: ignore[arg-type]
    state.register_ws(teammate)  # type: ignore[arg-type]
    state.push_slot_patch("chat-a", ["pinned"])
    assert [frame["type"] for frame in legacy_owner.frames] == ["slots"]
    assert [frame["type"] for frame in teammate.frames] == ["slot_patch"]
    assert audits == []


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param({}, id="generic-socket"),
        pytest.param({SLOT_PATCH_WS_FLAG: True, SLOT_DIFF_WS_FLAG: True}, id="diff-socket"),
    ],
)
def test_broadcast_to_a_non_owner_hides_private_status_and_audits_it(
    state: DashboardState, audits: list, monkeypatch: pytest.MonkeyPatch, flags: dict
) -> None:
    _capture(state, monkeypatch)
    owner, teammate = _Socket(**flags), _Socket(**flags)
    state.register_ws(owner, owner=True)  # type: ignore[arg-type]
    state.register_ws(teammate)  # type: ignore[arg-type]
    state._do_slots_broadcast()
    assert sorted(audits) == [("allowed", PUBLIC), ("denied", PRIVATE)]
    chips = _chips(teammate.frames[-1])
    assert "ci" not in chips[PRIVATE] and "state" not in chips[PRIVATE]
    assert chips[PUBLIC]["ci"] == "passed"


def test_non_owner_slot_list_hides_private_status_and_audits_it(
    state: DashboardState, audits: list
) -> None:
    # The slot list a non-owner dashboard user GETs.
    rows = state.serialize_slots(dashboard_user=True)
    chips = {link["url"]: link for link in rows[0]["source_links"]}
    assert "ci" not in chips[PRIVATE] and "state" not in chips[PRIVATE]
    assert chips[PUBLIC]["ci"] == "passed"
    assert sorted(audits) == [("allowed", PUBLIC), ("denied", PRIVATE)]
