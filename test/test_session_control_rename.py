"""``session_rename``: relabel the caller or a session it may control.

The verb reuses ``authorize_target`` with ``allow_self`` and writes the title
through ``chat_title.apply_manual_title``, the code the sidebar rename runs. The
tests cover the reach (self, peer, the ownership fence), the refusal classes, the
title rules, the route and the MCP tool.
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_title import _TITLE_ORIGIN_USER
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _key(slot) -> str:
    return slot_history_key(slot)


def _rename(state, caller, target: str, title: str) -> dict:
    return asyncio.run(
        sc.rename_target(state, caller_session_key=_key(caller), target=target, title=title)
    )


# Upper bound on each step of a race scenario. Every step finishes in
# milliseconds; the bound only turns a hang into a reported failure.
_RACE_STEP_TIMEOUT_S = 10.0


def _run_race(make_first, during, started: asyncio.Event, release: asyncio.Event):
    """Run ``make_first()`` as a task, wait until it parks in its save
    (``started``), run ``during()``, then release the save and collect both.

    Returns ``(first_result, during_result)``. Every wait is bounded, a first
    task that fails before reaching the save re-raises its own exception, and
    the task is always released and awaited, so a broken race fails the test
    instead of hanging the worker.
    """

    async def _scenario():
        first = asyncio.create_task(make_first())
        waiter = asyncio.create_task(started.wait())
        try:
            done, _ = await asyncio.wait(
                {first, waiter},
                timeout=_RACE_STEP_TIMEOUT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not started.is_set():
                if first in done:
                    first.result()
                    raise AssertionError("first rename finished without reaching its save")
                raise AssertionError("first rename never reached its save")
            during_result = await asyncio.wait_for(during(), _RACE_STEP_TIMEOUT_S)
            release.set()
            first_result = await asyncio.wait_for(asyncio.shield(first), _RACE_STEP_TIMEOUT_S)
            return first_result, during_result
        finally:
            release.set()
            waiter.cancel()
            if not first.done():
                first.cancel()
            await asyncio.gather(first, waiter, return_exceptions=True)

    return asyncio.run(_scenario())


def test_the_race_helper_reports_a_first_task_that_fails_before_its_save():
    started, release = asyncio.Event(), asyncio.Event()

    async def _boom():
        raise RuntimeError("failed before the save")

    async def _never():
        raise AssertionError("during must not run")

    with pytest.raises(RuntimeError, match="failed before the save"):
        _run_race(_boom, _never, started, release)


def test_the_race_helper_times_out_a_first_task_that_never_reaches_its_save(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_RACE_STEP_TIMEOUT_S", 0.05)
    started, release = asyncio.Event(), asyncio.Event()
    parked = asyncio.Event()

    async def _stuck():
        await parked.wait()

    async def _never():
        raise AssertionError("during must not run")

    with pytest.raises(AssertionError, match="never reached its save"):
        _run_race(_stuck, _never, started, release)


def test_a_peer_session_is_renamed_as_a_final_manual_title(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    epoch = target._title_epoch

    out = _rename(state, caller, "chat-2", "PR 15164: session_rename")

    assert out == {"ok": True, "target": "chat-2", "title": "PR 15164: session_rename"}
    assert target.title == "PR 15164: session_rename"
    # Final, like a person's rename: the automatic titler never refreshes it.
    assert target._title_origin == _TITLE_ORIGIN_USER
    assert target._titled is True
    # The epoch bump is what makes an in-flight auto-title attempt stand down.
    assert target._title_epoch == epoch + 1


def test_a_session_may_rename_itself(tmp_path):
    """Mutation guard: without ``allow_self`` this is the ``self_target`` refusal."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")

    out = _rename(state, caller, "chat-1", "My own label")

    assert out["target"] == "chat-1"
    assert caller.title == "My own label"


def test_an_agent_created_session_may_rename_itself(tmp_path):
    """The self case waives the creator fence, which would otherwise refuse a
    session whose ``_created_by`` names its parent rather than itself."""
    state = _make_state(tmp_path)
    worker = state.get_or_create_slot("chat-1")
    worker._created_by = "chat-0"

    _rename(state, worker, "chat-1", "Worker: CI round 3")

    assert worker.title == "Worker: CI round 3"


def test_a_fenced_caller_renames_a_session_it_created(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    caller._created_by = "chat-0"
    child = state.get_or_create_slot("chat-2")
    child._created_by = "chat-1"

    _rename(state, caller, "chat-2", "Child: lane 02")

    assert child.title == "Child: lane 02"


def test_a_fenced_caller_cannot_rename_a_persons_own_session(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    caller._created_by = "chat-0"
    person = state.get_or_create_slot("chat-2")
    person.title = "Pearce's planning"

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-2", "hijacked")

    assert exc.value.code == "not_creator"
    assert person.title == "Pearce's planning"


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda s: setattr(s, "memory_mode", "incognito"), "ephemeral_target"),
        (
            lambda s: setattr(s, "linked_session_key", "slack:1786300000.000100"),
            "linked_session_target",
        ),
        (lambda s: setattr(s, "_app", "some-app"), "app_scoped_target"),
    ],
)
def test_out_of_bounds_targets_are_refused(tmp_path, setup, code):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.title = "untouched"
    setup(target)

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-2", "new")

    assert exc.value.code == code
    assert target.title == "untouched"


def test_a_session_that_is_not_open_is_not_found(tmp_path):
    """Archived (history) sessions are not live slots, so they are not addressable."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-archived", "new")

    assert exc.value.code == "target_not_found"


def test_a_padded_title_is_stored_stripped(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    out = _rename(state, caller, "chat-2", "   spaced out  ")

    assert out["title"] == "spaced out"
    assert target.title == "spaced out"


@pytest.mark.parametrize(
    "title",
    ["", "   ", "x" * 201, "two\nlines", "tab\there", "esc\x1b[31m", "sep\u2028line"],
)
def test_a_bad_title_is_refused_and_nothing_changes(tmp_path, title):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.title = "untouched"
    epoch = target._title_epoch

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-2", title)

    assert exc.value.code == "invalid_title"
    assert exc.value.status == 400
    assert target.title == "untouched"
    assert target._title_epoch == epoch


def test_a_title_at_the_cap_is_kept_whole(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    _rename(state, caller, "chat-2", "y" * 200)

    assert target.title == "y" * 200


def test_a_bad_title_is_refused_before_the_target_is_looked_up(tmp_path):
    """A malformed argument must not read as an access decision."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-does-not-exist", "")

    assert exc.value.code == "invalid_title"


def test_the_sidebar_route_still_renames_through_the_shared_helper(tmp_path):
    """The factored-out helper keeps the sidebar rename's behaviour."""
    from kiro_crew.dashboard.chat_title import api_chat_slot_rename

    state = _make_state(tmp_path)
    target = state.get_or_create_slot("chat-2")
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"slot": "chat-2"}

    async def _json():
        return {"title": "  from the sidebar  "}

    request.json = _json
    resp = asyncio.run(api_chat_slot_rename(request))

    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True, "title": "from the sidebar"}
    assert target.title == "from the sidebar"
    assert target._title_origin == _TITLE_ORIGIN_USER


# ── Route ────────────────────────────────────────────────────────────────────


def _request(tmp_path, *, internal: bool, body: dict):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/rename"
    request.method = "POST"
    request.headers = {"X-Session-Key": _key(caller)}
    request.query = {}
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )

    async def _json():
        return body

    request.json = _json
    return request, state


def test_route_without_the_secret_is_forbidden(tmp_path):
    req, _ = _request(tmp_path, internal=False, body={"target": "chat-2", "title": "x"})
    resp = asyncio.run(handlers_sc.api_session_control_rename(req))
    assert resp.status == 403


def test_route_refuses_a_non_string_title(tmp_path):
    req, _ = _request(tmp_path, internal=True, body={"target": "chat-2", "title": 5})
    resp = asyncio.run(handlers_sc.api_session_control_rename(req))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_request"


def test_route_renames_the_target(tmp_path):
    req, state = _request(tmp_path, internal=True, body={"target": "chat-2", "title": "done"})
    resp = asyncio.run(handlers_sc.api_session_control_rename(req))
    assert resp.status == 200
    assert json.loads(resp.body)["title"] == "done"
    assert state.get_slot("chat-2").title == "done"


def test_the_route_is_registered_strict_internal():
    """An unlisted session-control path falls through to cookie auth, and the
    MCP caller's secret is then ignored in production."""
    from kiro_crew.dashboard import server

    assert "/api/session-control/rename" in server._STRICT_INTERNAL_API_PATHS


# ── MCP tool ─────────────────────────────────────────────────────────────────

_VERIFIED = Caller.strict("dashboard:chat-verified")


def _call(args: dict, reply: object) -> tuple[str, list[DashboardRequest]]:
    """One tools/call frame against an in-memory dashboard; returns every write."""
    dash = InMemoryDashboardClient({"POST /api/session-control/rename": reply})
    out = TABLE.call("session_rename", args, ToolContext(dash, _VERIFIED))
    return out, [r for r in dash.requests if r.method != "GET"]


def _call_as(caller: Caller) -> tuple[str, list[DashboardRequest]]:
    dash = InMemoryDashboardClient({"POST /api/session-control/rename": {"ok": True}})
    out = TABLE.call(
        "session_rename", {"target": "chat-2", "title": "x"}, ToolContext(dash, caller)
    )
    return out, list(dash.requests)


def test_tool_carries_the_verified_key_and_reports_the_title():
    out, writes = _call(
        {"target": "chat-2", "title": "Worker: lane 02"},
        {"ok": True, "target": "chat-2", "title": "Worker: lane 02"},
    )
    (req,) = writes
    assert (req.method, req.path) == ("POST", "/api/session-control/rename")
    assert req.body == {"target": "chat-2", "title": "Worker: lane 02"}
    assert req.session_key == "dashboard:chat-verified"
    assert "Renamed `chat-2` to 'Worker: lane 02'" in out


def test_tool_reports_a_refusal_as_an_error():
    out, _ = _call(
        {"target": "chat-2", "title": "x"},
        {"error": "this session can only control sessions it created itself", "code": "x"},
    )
    assert out.startswith("Error: could not rename that session:")


def test_tool_refuses_an_unverifiable_caller_without_a_request():
    out, requests = _call_as(Caller.unverified("", "no session"))
    assert out.startswith("Error:")
    assert requests == []


def test_the_schema_leaves_the_title_cap_to_the_route():
    """A padded title that is under the cap once stripped must reach the route;
    a schema cap on the raw string would refuse it before the strip."""
    from kiro_crew.validation import SESSION_RENAME_SCHEMA, validate_tool_args

    padded = " " * 20 + "t" * 200 + " " * 20
    args = validate_tool_args({"target": "chat-2", "title": padded}, SESSION_RENAME_SCHEMA)
    assert args["title"].strip() == "t" * 200


def test_a_scheduled_run_cannot_rename_its_own_session(tmp_path):
    """``allow_self`` waives only the self-target refusal; a cron tab is still an
    unattended target, so the tool and docs must not promise a cron run a
    self-rename. Shaped as ``inject_cron_result_to_dashboard`` mints the tab."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.state import SlotOrigin

    state = _make_state(tmp_path)
    state.crons.list_jobs.return_value = [SimpleNamespace(id="2c3b2e25", created_by="")]
    cron = state.get_or_create_slot(
        "cron-2c3b2e25", linked_session_key="cron:2c3b2e25", origin=SlotOrigin.CRON
    )
    cron.title = "nightly"

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.rename_target(state, caller_session_key="cron:2c3b2e25", target=cron.key, title="x")
        )

    assert exc.value.code == "unattended_target"
    assert cron.title == "nightly"


def test_a_credential_shaped_title_is_sanitized_before_it_is_stored(tmp_path):
    """Mutation pin for ``_scrub_text``: the title is broadcast and persisted."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"

    out = _rename(state, caller, "chat-2", f"deploy with {secret}")

    assert secret not in target.title
    assert secret not in out["title"]


def test_an_allowed_rename_writes_its_audit_record(tmp_path, monkeypatch):
    """Mutation pin for the allowed ``_audit`` call: the record names the verb,
    the caller and the target, and carries the title length, not the text."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")
    seen: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: seen.append(kw))

    _rename(state, caller, "chat-2", "Audited name")

    allowed = [r for r in seen if r.get("outcome") == "allowed"]
    assert allowed == [
        {
            "caller_session_key": _key(caller),
            "operation": "rename",
            "slot_key": "chat-2",
            "outcome": "allowed",
            "detail": {"chars": len("Audited name")},
        }
    ]


def _fail_persist(monkeypatch):
    from kiro_crew.dashboard import chat_title

    async def _no(state, slot):
        return False

    monkeypatch.setattr(chat_title, "_persist_title", _no)


def test_a_failed_title_write_stays_live_and_the_verb_says_it_is_not_saved(tmp_path, monkeypatch):
    """A write that did not land is not reported as plain success: the verb raises
    ``title_persist_failed``. The live slot keeps the new title and is broadcast,
    as the sidebar rename and the channel rename do, because the slot's next full
    save writes it and a rollback could restore an overlapping rename's title."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _fail_persist(monkeypatch)
    pushed: list = []
    monkeypatch.setattr(state, "push_slot_title", lambda *a, **k: pushed.append(a))
    seen: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: seen.append(kw))

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-2", "Not saved yet")

    assert exc.value.code == "title_persist_failed"
    assert exc.value.status == 500
    assert target.title == "Not saved yet"
    assert target._title_origin == _TITLE_ORIGIN_USER
    assert pushed == [("chat-2", "Not saved yet")]
    assert [r["outcome"] for r in seen] == ["error"]


def test_overlapping_failed_renames_leave_the_newest_title(tmp_path, monkeypatch):
    """Two renames whose saves both fail, the second landing during the first's
    write: the slot ends on the newer title, never on an older one."""
    from kiro_crew.dashboard import chat_title

    state = _make_state(tmp_path)
    target = state.get_or_create_slot("chat-2")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_fail(state_, slot):
        if not started.is_set():
            started.set()
            await release.wait()
        return False

    monkeypatch.setattr(chat_title, "_persist_title", _slow_fail)

    result = _run_race(
        lambda: chat_title.apply_manual_title(state, target, "A"),
        lambda: chat_title.apply_manual_title(state, target, "B"),
        started,
        release,
    )

    assert result == (False, False)
    assert target.title == "B"


def test_the_sidebar_rename_keeps_its_best_effort_save(tmp_path, monkeypatch):
    """Unchanged from main: a failed save still answers ok with the live title."""
    from kiro_crew.dashboard import chat_title

    state = _make_state(tmp_path)
    target = state.get_or_create_slot("chat-2")
    _fail_persist(monkeypatch)
    monkeypatch.setattr(chat_title, "sel", MagicMock())
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"slot": "chat-2"}

    async def _body():
        return {"title": "Live name"}

    request.json = _body
    resp = asyncio.run(chat_title.api_chat_slot_rename(request))

    assert resp.status == 200
    assert target.title == "Live name"


def test_a_redacted_title_is_read_back_as_stored(tmp_path):
    """Redaction changes the title, and the reply carries exactly what the slot
    shows rather than what the caller sent."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    title = "keys " + ("AKIA" + "ABCDEFGHIJKLMNOP") * 2

    out = _rename(state, caller, "chat-2", title)

    assert "AKIA" + "ABCDEFGHIJKLMNOP" not in target.title
    assert len(target.title) <= 200
    assert out["title"] == target.title


def test_an_overtaken_rename_broadcasts_the_current_title(tmp_path, monkeypatch):
    """Rename "A" saves slowly and "B" lands during that save: when A finishes,
    what it broadcasts is the slot's current title, "B", not its own."""
    from kiro_crew.dashboard import chat_title

    state = _make_state(tmp_path)
    target = state.get_or_create_slot("chat-2")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_ok(state_, slot):
        if not started.is_set():
            started.set()
            await release.wait()
        return True

    monkeypatch.setattr(chat_title, "_persist_title", _slow_ok)
    pushed: list = []
    monkeypatch.setattr(state, "push_slot_title", lambda key, t, **kw: pushed.append(t))

    _run_race(
        lambda: chat_title.apply_manual_title(state, target, "A"),
        lambda: chat_title.apply_manual_title(state, target, "B"),
        started,
        release,
    )
    assert target.title == "B"
    assert pushed == ["B", "B"]


def test_an_overtaken_rename_replies_with_the_current_title(tmp_path, monkeypatch):
    """session_rename "A" saves slowly and "B" lands during that save: A's reply
    names "B", the title the session now shows, because the caller acts on it."""
    from kiro_crew.dashboard import chat_title

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_ok(state_, slot):
        if not started.is_set():
            started.set()
            await release.wait()
        return True

    monkeypatch.setattr(chat_title, "_persist_title", _slow_ok)

    out, _ = _run_race(
        lambda: sc.rename_target(
            state, caller_session_key=_key(caller), target="chat-2", title="A"
        ),
        lambda: chat_title.apply_manual_title(state, target, "B"),
        started,
        release,
    )
    assert target.title == "B"
    assert out == {"ok": True, "target": "chat-2", "title": "B"}


def test_a_channel_title_on_a_live_slot_renames_through_the_shared_helper(tmp_path, monkeypatch):
    """``rename_channel_title_live`` routes a live slot through
    ``apply_manual_title``, so a channel rename is final like a sidebar one."""
    from kiro_crew.dashboard import channel_slots

    calls: list = []

    async def _spy(state_, slot, title):
        calls.append((slot.key, title))
        return True

    monkeypatch.setattr(channel_slots, "apply_manual_title", _spy)
    state = _make_state(tmp_path)
    state.get_or_create_slot("chat-2")

    renamed = asyncio.run(
        channel_slots.rename_channel_title_live(state, "dashboard:chat-2", "From Telegram")
    )

    assert renamed is True
    assert calls == [("chat-2", "From Telegram")]


def test_an_older_title_write_that_lands_last_writes_the_newest_title(tmp_path):
    """Rename "A" snapshots its fields, then "B" lands before A's write takes the
    transcript lock. Because the under-lock guard re-snapshots when the epoch
    moved, A's write already carries "B", so the disk ends on the newer title,
    and the write reports success without a redundant second write that could
    fail and report ``title_persist_failed`` for a title already on disk."""
    from kiro_crew.dashboard import chat_title

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-2")
    slot.title, slot._titled, slot._title_origin = "A", True, _TITLE_ORIGIN_USER
    slot._title_epoch += 1
    written: list[dict] = []

    class _Log:
        calls = 0

        def update_metadata_if(self, key, fields, guard, **_kw):
            _Log.calls += 1
            if _Log.calls == 1:
                # "B" lands while A's write waits for the lock.
                slot.title = "B"
                slot._title_epoch += 1
                assert guard({})
                written.append(dict(fields))
                return True
            return False  # a second write would fail

    state.conversation_log = _Log()

    assert asyncio.run(chat_title._persist_title(state, slot)) is True
    assert _Log.calls == 1
    assert [w["title"] for w in written] == ["B"]
    assert written[0]["title_origin"] == _TITLE_ORIGIN_USER


@pytest.mark.parametrize(
    "title",
    [
        "\u200b",
        "\u200b \u200c",
        "\u202eevil",
        "a\u2066b",
        "report \u200f(2026) prod",
        "a\u061cb",
        "\u3164",
        "\u2800\u2800",
        "\u115f\u200b",
        "\ufe0f\ufe0e",
    ],
)
def test_an_invisible_or_direction_changing_title_is_refused(tmp_path, title):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.title

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-2", title)

    assert exc.value.code == "invalid_title"
    assert target.title == before


def test_a_title_with_a_zero_width_joiner_emoji_is_kept(tmp_path):
    """A ZWJ sequence is format-category text with a visible result, so the
    joiner is one of the format characters a title keeps."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"

    _rename(state, caller, "chat-2", f"Team {family}")

    assert target.title == f"Team {family}"


@pytest.mark.parametrize("joiner", ["\u200d", "\ufe0f", "\u034f", "\U000e0020"])
def test_a_credential_split_by_a_kept_invisible_character_is_still_redacted(tmp_path, joiner):
    """The redactor matches literal patterns, so an invisible character the title
    rule keeps (a joiner, a variation selector, a tag) must not carry a key past
    it into the stored and broadcast title."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    tail = "ABCDEFGHIJKLMNOP"

    out = _rename(state, caller, "chat-2", f"deploy AKIA{joiner}{tail}")

    assert tail not in target.title
    assert tail not in out["title"]
    assert "[REDACTED" in target.title


@pytest.mark.parametrize(
    "title",
    [
        "deploy\u200bprod",
        "deploy\u2060prod",
        "\ufeffdeploy prod",
        "deploy\u00adprod",
        "deploy\u206aprod",
        "deploy\ufff9prod",
        # A tag-block character outside the flag-subdivision range.
        "deploy\U000e0001prod",
        "deploy AKIA\u200bABCDEFGHIJKLMNOP",
    ],
)
def test_a_format_character_inside_a_visible_title_is_refused(tmp_path, title):
    """A zero-width space, word joiner, BOM, soft hyphen or other Cf character
    inside a visible title would let a caller spoof a near-duplicate of another
    session's label, so it is refused rather than stored."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.title

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-2", title)

    assert exc.value.code == "invalid_title"
    assert target.title == before


@pytest.mark.parametrize(
    "title",
    [
        # England's flag: black flag, tag letters "gbeng", cancel tag.
        "UK \U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
        # Persian with a zero-width non-joiner, as the script is written.
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
    ],
)
def test_a_title_with_a_kept_format_character_is_stored(tmp_path, title):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    _rename(state, caller, "chat-2", title)

    assert target.title == title


@pytest.mark.parametrize(
    "title",
    [
        "a\u2800b",
        # The blank glyph split a key past the redactors before it was refused.
        "deploy AKIA" + "\u2800" + "ABCDEFGHIJKLMNOP",
    ],
)
def test_a_blank_glyph_anywhere_in_a_title_is_refused(tmp_path, title):
    """U+2800 is a printable symbol that draws nothing, so scanning
    normalization keeps it and a secret split by it would reach the sidebar
    unredacted. It is refused anywhere in a title, and nothing changes."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.title = "untouched"

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-2", title)

    assert exc.value.code == "invalid_title"
    assert target.title == "untouched"


@pytest.mark.parametrize(
    "title",
    [
        # Ten keys: 200 characters sent, longer once each key becomes a marker.
        ("AKIA" + "ABCDEFGHIJKLMNOP") * 10,
        # A marker with brackets of its own (an IPv6 URL host) past the cap.
        "a" * 160 + " http://[2001:db8::1]/ssh-rsa+",
    ],
)
def test_a_title_redaction_pushes_past_the_cap_is_refused(tmp_path, title):
    """Redaction markers are longer than what they replace. A title they push
    past 200 characters is refused, not cut, so no stored title holds a torn
    marker, and the refusal does not echo the title."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.title = "untouched"
    epoch = target._title_epoch

    with pytest.raises(sc.SessionControlError) as exc:
        _rename(state, caller, "chat-2", title)

    assert exc.value.code == "invalid_title"
    assert "redacted" in str(exc.value)
    assert "AKIA" not in str(exc.value) and "2001" not in str(exc.value)
    assert target.title == "untouched"
    assert target._title_epoch == epoch


def test_a_redacted_title_within_the_cap_is_stored_whole(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    _rename(state, caller, "chat-2", "deploy key " + secret)

    assert target.title.startswith("deploy key [REDACTED")
    assert target.title.endswith("]")
    assert secret not in target.title
