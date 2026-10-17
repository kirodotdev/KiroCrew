"""Structured metadata beside a channel approval message.

The approval card decides which trust tiers to offer. The server is the only
side that can refuse one (``handlers_channel.approve`` answers
``pattern_underivable``), so the message carries the server's verdict as flat
string facts next to the unchanged prose, and the card reads those instead of
re-deriving them from the text.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.channel import (
    _APPROVAL_FIELD_MAX_CHARS,
    _APPROVAL_SHELL_TITLE_PREFIX,
    Channel,
    ChannelMessage,
    _stream_task,
)
from kiro_crew.config.loader import KiroCrewConfig, config_local_path, config_path
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST


def _make_agent():
    return SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:test",
        _approval_future=None,
        _trusted_commands=set(),
        _trusted_bases=set(),
        _pending_approval_command="",
    )


def _make_channel(agent):
    """Channel stub whose ``post`` resolves the pending approval as rejected
    (the interactive path otherwise awaits it for 3600s)."""
    ch = SimpleNamespace(id="c1", trusted=False, members={})

    async def _post(*args, **kw):
        if kw.get("msg_type") == "approval":
            if agent._approval_future is not None and not agent._approval_future.done():
                agent._approval_future.set_result("rejected")

    ch.post = AsyncMock(side_effect=_post)
    return ch


def _make_client(events):
    client = SimpleNamespace()

    async def _stream(message):
        for ev in events:
            yield ev

    client.stream = _stream
    client.approve_tool = AsyncMock()
    client.reject_tool = AsyncMock()
    return client


def _perm_event(*, title="", text="", tool_input="", is_shell=False, tool_input_redacted=False):
    _shell_command = None
    if is_shell and tool_input:
        try:
            _shell_command = json.loads(tool_input).get("command")
        except (ValueError, AttributeError):
            _shell_command = None
    return SimpleNamespace(
        kind=EVENT_PERMISSION_REQUEST,
        text=text,
        title=title,
        request_id=7,
        tool_input=tool_input,
        is_shell=is_shell,
        tool_input_redacted=tool_input_redacted,
        shell_command=_shell_command,
    )


def _done():
    return SimpleNamespace(kind=EVENT_COMPLETE)


async def _approval_meta(event) -> dict[str, str]:
    agent = _make_agent()
    ch = _make_channel(agent)
    await _stream_task(agent, ch, _make_client([event, _done()]), "hi")
    posts = [kw for _args, kw in ch.post.call_args_list if kw.get("msg_type") == "approval"]
    assert len(posts) == 1, "expected exactly one approval message"
    meta = posts[0].get("meta")
    assert isinstance(meta, dict), "approval message must carry structured meta"
    assert all(isinstance(v, str) for v in meta.values()), "meta values are flat strings"
    return meta


@pytest.mark.asyncio
async def test_simple_shell_command_offers_both_per_command_tiers():
    meta = await _approval_meta(
        _perm_event(title="List files", tool_input=json.dumps({"command": "ls -la"}), is_shell=True)
    )
    assert meta["tool_title"] == "Running: ls -la"
    assert meta["tool_input"] == json.dumps({"command": "ls -la"})
    assert meta["command_grantable"] == "1"
    assert meta["base_derivable"] == "1"
    # The very binary the endpoint would grant -- not a first-token guess.
    assert meta["base_command"] == "ls"


@pytest.mark.asyncio
async def test_compound_command_has_no_derivable_base():
    """``cat f | wc -l``: the exact tier is available (one literal string), the
    base tier is not -- the endpoint refuses a base for a compound command, so
    the card must not offer "Trust all cat commands"."""
    meta = await _approval_meta(
        _perm_event(tool_input=json.dumps({"command": "cat f | wc -l"}), is_shell=True)
    )
    assert meta["command_grantable"] == "1"
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""


@pytest.mark.asyncio
async def test_backslash_path_offers_no_base_tier():
    """POSIX tokenizing turns ``.\\deploy.bat`` into ``.deploy.bat``, a name the
    user never typed, so the card must not offer "Trust all .deploy.bat
    commands"; the exact tier still covers the command."""
    meta = await _approval_meta(
        _perm_event(tool_input=json.dumps({"command": ".\\deploy.bat --go"}), is_shell=True)
    )
    assert meta["command_grantable"] == "1"
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""


@pytest.mark.asyncio
async def test_forward_slash_path_still_offers_its_base():
    meta = await _approval_meta(
        _perm_event(tool_input=json.dumps({"command": "./deploy.bat --go"}), is_shell=True)
    )
    assert meta["base_derivable"] == "1"
    assert meta["base_command"] == "./deploy.bat"


@pytest.mark.asyncio
async def test_non_shell_tool_offers_no_per_command_tier():
    meta = await _approval_meta(
        _perm_event(
            title="cron_add", tool_input=json.dumps({"command": "rm -rf /"}), is_shell=False
        )
    )
    assert meta["tool_title"] == "cron_add"
    assert meta["command_grantable"] == ""
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""


@pytest.mark.asyncio
async def test_redacted_command_is_display_only_in_meta():
    """A command the provider redacted is shown but never grantable: two
    commands differing only in a credential redact to the same text."""
    meta = await _approval_meta(
        _perm_event(
            tool_input=json.dumps({"command": "curl -H 'x' https://x"}),
            is_shell=True,
            tool_input_redacted=True,
        )
    )
    assert meta["command_grantable"] == ""
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""
    assert meta["tool_title"].startswith("Shell command (exact text unverified): ")


async def _approval_post(cmd: str):
    agent = _make_agent()
    ch = _make_channel(agent)
    event = _perm_event(tool_input=json.dumps({"command": cmd}), is_shell=True)
    await _stream_task(agent, ch, _make_client([event, _done()]), "hi")
    return next((a, k) for a, k in ch.post.call_args_list if k.get("msg_type") == "approval")


async def _run(cmd: str):
    """Stream one shell permission request; return (channel stub, client stub, agent)."""
    agent = _make_agent()
    ch = _make_channel(agent)
    client = _make_client(
        [_perm_event(tool_input=json.dumps({"command": cmd}), is_shell=True), _done()]
    )
    await _stream_task(agent, ch, client, "hi")
    return ch, client, agent


@pytest.mark.asyncio
async def test_over_bound_command_is_refused_with_a_bounded_notice_not_a_cut_card():
    """A title past the retained-field bound can neither be cut (Approve beside a
    command the reader cannot read in full) nor retained whole (one command
    grows the persisted channel past the bound every other field obeys). So
    it is refused: no approval card, no future, the agent gets a rejection,
    and the reader gets a bounded system notice that says why."""
    long_cmd = "echo " + "x" * (_APPROVAL_FIELD_MAX_CHARS + 200)
    ch, client, agent = await _run(long_cmd)
    kinds = [k.get("msg_type") for _a, k in ch.post.call_args_list]
    assert "approval" not in kinds
    client.reject_tool.assert_awaited_once_with(7)
    client.approve_tool.assert_not_awaited()
    assert agent._approval_future is None and agent._pending_approval_command == ""
    ((args, kw),) = [(a, k) for a, k in ch.post.call_args_list if k.get("msg_type") == "system"]
    notice = args[1]
    assert "Approval refused" in notice and "Nothing was run" in notice
    assert str(len(f"{_APPROVAL_SHELL_TITLE_PREFIX}{long_cmd}")) in notice
    assert str(_APPROVAL_FIELD_MAX_CHARS) in notice
    # The notice is bounded too: it carries the same bounded excerpt the card
    # would have, never the whole command.
    assert "x" * (_APPROVAL_FIELD_MAX_CHARS + 1) not in notice
    assert kw.get("meta") is None


@pytest.mark.asyncio
async def test_title_bound_is_measured_on_the_title_the_reader_would_see():
    """The longest command whose ``Running: `` title fits is posted whole with
    every tier; one character more is refused. Measured on the title, not the
    command, so a posted title is always complete."""
    room = _APPROVAL_FIELD_MAX_CHARS - len(_APPROVAL_SHELL_TITLE_PREFIX)
    fits = "echo " + "y" * (room - len("echo "))
    assert len(fits) == room
    ch, client, _agent = await _run(fits)
    ((args, kw),) = [(a, k) for a, k in ch.post.call_args_list if k.get("msg_type") == "approval"]
    assert kw["meta"]["tool_title"] == f"{_APPROVAL_SHELL_TITLE_PREFIX}{fits}"
    assert kw["meta"]["command_grantable"] == "1" and kw["meta"]["base_command"] == "echo"
    assert all(len(v) <= _APPROVAL_FIELD_MAX_CHARS for v in kw["meta"].values())
    assert f"**{_APPROVAL_SHELL_TITLE_PREFIX}{fits}**" in args[1]
    client.reject_tool.assert_awaited_once_with(7)  # the stub channel rejects the card

    ch, client, _agent = await _run(fits + "y")
    assert not [k for _a, k in ch.post.call_args_list if k.get("msg_type") == "approval"]
    assert [k for _a, k in ch.post.call_args_list if k.get("msg_type") == "system"]
    client.reject_tool.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_prose_content_is_unchanged_beside_meta():
    """Renderers that ignore ``meta`` (Slack mirrors, older dashboards) must
    see exactly the message they saw before the field existed."""
    agent = _make_agent()
    ch = _make_channel(agent)
    event = _perm_event(tool_input=json.dumps({"command": "ls -la"}), is_shell=True)
    await _stream_task(agent, ch, _make_client([event, _done()]), "hi")
    args, kw = next((a, k) for a, k in ch.post.call_args_list if k.get("msg_type") == "approval")
    assert args[1] == '⚠️ Approval needed: **Running: ls -la**\n```\n{"command": "ls -la"}\n```'
    assert kw["meta"]["tool_title"] == "Running: ls -la"


@pytest.mark.asyncio
async def test_post_threads_meta_and_defaults_to_none():
    ch = Channel(id="c1", topic="t")
    plain = await ch.post("human", "hello")
    assert plain.meta is None
    assert plain.to_dict()["meta"] is None
    facts = {"command_grantable": "1", "base_command": "ls"}
    tagged = await ch.post("a1", "card", msg_type="approval", meta=facts)
    assert tagged.meta == facts
    assert tagged.to_dict()["meta"] == facts


def test_serialize_round_trips_meta_and_tolerates_its_absence():
    ch = Channel(id="c1", topic="t")
    with_meta = ChannelMessage(
        id="m1",
        from_id="a1",
        from_role="dev",
        content="card",
        msg_type="approval",
        meta={"command_grantable": "1", "base_command": "ls"},
    )
    without = ChannelMessage(id="m2", from_id="human", from_role="human", content="hi")
    ch.messages.extend([with_meta, without])

    data = ch.serialize()
    # A message persisted before the field existed has no ``meta`` key at all.
    legacy = dict(without.to_dict())
    del legacy["meta"]
    data["messages"].append({**legacy, "id": "m3"})

    restored = Channel.deserialize(data)
    by_id = {m.id: m for m in restored.messages}
    assert by_id["m1"].meta == {"command_grantable": "1", "base_command": "ls"}
    assert by_id["m2"].meta is None
    assert by_id["m3"].meta is None
    assert by_id["m1"].to_dict() == with_meta.to_dict()


# --------------------------------------------------------------------------- #
# How long an unanswered card waits: agent.tool_approval_timeout_secs, capped
# --------------------------------------------------------------------------- #
#
# The window comes out of a real config.json, written the way the gateway writes
# one. ``KiroCrewConfig.save()`` publishes every field, so a config nobody edited
# still holds a ``tool_approval_timeout_secs`` (the field default). A test that
# hands the helper only the keys under test cannot see that.


def _silent_channel():
    """Channel stub whose card nobody answers."""
    ch = SimpleNamespace(id="c1", trusted=False, members={})
    ch.post = AsyncMock()
    return ch


def _save_config(**agent_fields):
    """Write ``config.json`` as the gateway's first boot does: ``KiroCrewConfig().save()``.

    ``agent_fields`` are set on the in-memory config before the write. The suite
    pins ``KIROCREW_HOME`` to a scratch directory for every test.
    """
    cfg = KiroCrewConfig()
    for name, value in agent_fields.items():
        setattr(cfg.agent, name, value)
    cfg.save()


def _write_config(document):
    """Write ``config.json`` as an operator editing the file by hand would."""
    config_path().write_text(json.dumps(document), encoding="utf-8")


async def _expire_one_card(monkeypatch, *, loader=None):
    """Run one approval to expiry; return (waited, steered reasons, client).

    The module's own ``asyncio`` binding is replaced by a namespace that
    delegates every other name to the real module, so ``wait_for`` reports the
    window it was given and expires at once: no wall clock is involved. The
    window is read from the ``config.json`` the test wrote. ``loader`` replaces
    ``KiroCrewConfig`` on the module for the one case a real loader cannot
    produce: a config read that raises.
    """
    import asyncio
    import types

    import kiro_crew.channel as channel_mod

    if loader is not None:
        # ``raising=False``: on a channel module that never read the setting, the
        # name is absent, and the test must still reach its window assertion.
        monkeypatch.setattr(channel_mod, "KiroCrewConfig", loader, raising=False)
    waited: list = []

    async def _expiring_wait_for(aw, timeout=None):
        waited.append(timeout)
        if asyncio.iscoroutine(aw):
            aw.close()
        raise asyncio.TimeoutError

    fake_asyncio = types.SimpleNamespace(**vars(asyncio))
    fake_asyncio.wait_for = _expiring_wait_for
    monkeypatch.setattr(channel_mod, "asyncio", fake_asyncio)
    steered = AsyncMock()
    monkeypatch.setattr(channel_mod, "_steer_host_deny", steered)

    client = _make_client([_perm_event(text="Run tool"), _done()])
    await asyncio.wait_for(_stream_task(_make_agent(), _silent_channel(), client, "hi"), timeout=30)
    reasons = [c.args[2] for c in steered.await_args_list]
    return waited, reasons, client


@pytest.mark.asyncio
async def test_an_unanswered_card_waits_the_configured_approval_window(monkeypatch):
    _save_config(tool_approval_timeout_secs=30)
    waited, reasons, client = await _expire_one_card(monkeypatch)
    assert waited == [30]
    client.reject_tool.assert_awaited_once()  # the expired card is still declined
    assert reasons == ["the channel approval card went unanswered for 30s"]


@pytest.mark.asyncio
async def test_an_unanswered_card_waits_the_full_cap_when_no_window_is_set(monkeypatch):
    _write_config({})
    waited, reasons, client = await _expire_one_card(monkeypatch)
    assert waited == [3600]
    client.reject_tool.assert_awaited_once()
    assert reasons == ["the channel approval card went unanswered for 3600s"]


@pytest.mark.asyncio
async def test_a_config_saved_with_defaults_keeps_the_full_cap(monkeypatch):
    """The first boot's ``save()`` writes the default window; nobody chose it."""
    _save_config()
    written = json.loads(config_path().read_text(encoding="utf-8"))["agent"]
    default = KiroCrewConfig().agent.tool_approval_timeout_secs
    # The premise: the file carries the key, at the field default.
    assert written["tool_approval_timeout_secs"] == default
    waited, reasons, client = await _expire_one_card(monkeypatch)
    assert waited == [3600]
    client.reject_tool.assert_awaited_once()
    assert reasons == ["the channel approval card went unanswered for 3600s"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ceiling", "loaded"), [(300, 240), (659, 599), (660, 600), (14400, 600)], ids=str
)
async def test_the_default_window_under_a_short_turn_ceiling_keeps_the_full_cap(
    monkeypatch, ceiling, loaded
):
    """The loader pulls the window 60 s under ``agent.chat_turn_timeout_secs``, so
    the saved default of 600 loads as 240 under a 300 s ceiling. It is still the
    default, not a choice."""
    _save_config(chat_turn_timeout_secs=ceiling)
    # The premise: what the loader leaves of the saved default at this ceiling.
    assert KiroCrewConfig.load().agent.tool_approval_timeout_secs == loaded
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [3600]


@pytest.mark.asyncio
async def test_a_window_set_under_a_short_turn_ceiling_is_waited(monkeypatch):
    _save_config(chat_turn_timeout_secs=300, tool_approval_timeout_secs=60)
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [60]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ceiling", "written", "loaded"),
    [(300, 500, 240), (300, 240, 240), (300, 7200, 240), (660, 700, 600)],
    ids=str,
)
async def test_a_window_the_loader_pulls_under_the_turn_ceiling_is_waited_as_pulled(
    monkeypatch, ceiling, written, loaded
):
    """A window above ``agent.chat_turn_timeout_secs`` less 60 s loads as that figure.

    The figure can equal what a saved default loads as (240 under a 300 s ceiling,
    600 under 660 s). The window is still the operator's, so it is the window the
    card waits."""
    _save_config(chat_turn_timeout_secs=ceiling, tool_approval_timeout_secs=written)
    # The premise: what the loader leaves of the written window at this ceiling.
    assert KiroCrewConfig.load().agent.tool_approval_timeout_secs == loaded
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [loaded]


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", [300, 659, 660, 14400], ids=str)
async def test_an_omitted_window_under_a_short_turn_ceiling_keeps_the_full_cap(
    monkeypatch, ceiling
):
    """The loader pulls a window under the turn ceiling only when the key is present.

    An omitted key loads as the field default whatever the ceiling (600 s under a
    300 s ceiling, where a saved default loads as 240), and the card waits the cap
    for both."""
    _write_config({"agent": {"chat_turn_timeout_secs": ceiling}})
    # The premise: nothing pulls an omitted key, so it loads as the field default.
    assert (
        KiroCrewConfig.load().agent.tool_approval_timeout_secs
        == KiroCrewConfig().agent.tool_approval_timeout_secs
    )
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [3600]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("written", "expected"),
    [("500", 240), (500.0, 240), ("600", 3600), (600.0, 3600)],
    ids=repr,
)
async def test_a_numeric_string_or_integral_float_window_is_read_as_the_loader_reads_it(
    monkeypatch, written, expected
):
    """The loader reads ``"500"`` and ``500.0`` as 500 and ``"600"`` and ``600.0`` as
    600, so the first two are a choice and the last two are the default, as the plain
    integers are."""
    _write_config({"agent": {"chat_turn_timeout_secs": 300, "tool_approval_timeout_secs": written}})
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("written", ["soon", True, None, 5.5], ids=repr)
async def test_a_malformed_window_under_a_short_turn_ceiling_keeps_the_full_cap(
    monkeypatch, written
):
    """The loader falls back to the field default for a value it cannot read as an
    integer, and the pull under the turn ceiling skips it: the window loads as 600 s,
    where a saved default loads as 240."""
    _write_config({"agent": {"chat_turn_timeout_secs": 300, "tool_approval_timeout_secs": written}})
    assert (
        KiroCrewConfig.load().agent.tool_approval_timeout_secs
        == KiroCrewConfig().agent.tool_approval_timeout_secs
    )
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [3600]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base", "overlay", "expected"),
    [
        pytest.param(
            {"agent": {"chat_turn_timeout_secs": 300}},
            {"agent": {"tool_approval_timeout_secs": 500}},
            240,
            id="window in the overlay, ceiling in the base",
        ),
        pytest.param(
            {"agent": {"tool_approval_timeout_secs": 500}},
            {"agent": {"chat_turn_timeout_secs": 300}},
            240,
            id="window in the base, ceiling in the overlay",
        ),
        pytest.param(
            {"agent": {"tool_approval_timeout_secs": 600}},
            {"agent": {"chat_turn_timeout_secs": 300}},
            3600,
            id="ceiling in the overlay, default window in the base",
        ),
        pytest.param(
            {},
            {"agent": {"chat_turn_timeout_secs": 300}},
            3600,
            id="ceiling in the overlay, no window",
        ),
        pytest.param(
            {"agent": {"chat_turn_timeout_secs": 300, "tool_approval_timeout_secs": 500}},
            {"agent": {"tool_approval_timeout_secs": 600}},
            3600,
            id="the default in the overlay over a window in the base",
        ),
        pytest.param(
            {"agent": {"chat_turn_timeout_secs": 300, "tool_approval_timeout_secs": 500}},
            {"agent": {"tool_approval_timeout_secs": "soon"}},
            3600,
            id="a malformed value in the overlay over a window in the base",
        ),
    ],
)
async def test_the_window_is_read_from_the_two_documents_as_the_loader_merges_them(
    monkeypatch, base, overlay, expected
):
    """``config.local.json`` wins over ``config.json`` key by key, for whether a window
    was chosen as well as for its number."""
    _write_config(base)
    config_local_path().write_text(json.dumps(overlay), encoding="utf-8")
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay_text", ["{not json", "[]", ""], ids=repr)
async def test_an_overlay_the_loader_cannot_use_leaves_the_window_in_the_base(
    monkeypatch, overlay_text
):
    """The loader ignores a ``config.local.json`` that is not a JSON object, so the
    window in ``config.json`` is the window."""
    _write_config({"agent": {"chat_turn_timeout_secs": 300, "tool_approval_timeout_secs": 500}})
    config_local_path().write_text(overlay_text, encoding="utf-8")
    assert KiroCrewConfig.load().agent.tool_approval_timeout_secs == 240
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [240]


@pytest.mark.asyncio
async def test_a_window_between_the_default_and_the_cap_is_waited(monkeypatch):
    _save_config(tool_approval_timeout_secs=1000)
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [1000]


@pytest.mark.asyncio
async def test_a_window_set_in_the_local_overlay_is_waited(monkeypatch):
    """The loader merges ``config.local.json`` over ``config.json``.

    A window set only there is the operator's window, though ``config.json`` has no
    such key."""
    _write_config({})
    config_local_path().write_text(
        json.dumps({"agent": {"tool_approval_timeout_secs": 90}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().agent.tool_approval_timeout_secs == 90
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [90]


@pytest.mark.asyncio
async def test_a_configured_window_above_the_cap_is_capped(monkeypatch):
    _save_config(tool_approval_timeout_secs=7200)
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [3600]


@pytest.mark.asyncio
async def test_a_zero_window_is_raised_by_the_loader_not_replaced_by_the_cap(monkeypatch):
    """The loader never yields a window under 30 s, so there is no zero to guard."""
    _write_config({"agent": {"tool_approval_timeout_secs": 0}})
    assert KiroCrewConfig.load().agent.tool_approval_timeout_secs == 30
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [30]


@pytest.mark.asyncio
@pytest.mark.parametrize(("written", "expected"), [(599, 599), (600, 3600), (601, 601)], ids=str)
async def test_a_window_written_as_the_default_is_read_as_unset(monkeypatch, written, expected):
    """A 600 typed by hand looks like the 600 ``save()`` writes, so it reads as unset.

    The cost of deciding from the value: exactly the default cannot be pinned on
    a channel. Any other number can."""
    _write_config({"agent": {"tool_approval_timeout_secs": written}})
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [expected]


@pytest.mark.asyncio
async def test_a_malformed_window_keeps_the_full_cap(monkeypatch):
    """The loader falls back to the default for a value of the wrong type."""
    _write_config({"agent": {"tool_approval_timeout_secs": "soon"}})
    waited, _reasons, _client = await _expire_one_card(monkeypatch)
    assert waited == [3600]


@pytest.mark.asyncio
async def test_an_unreadable_config_keeps_the_full_cap(monkeypatch):
    class _Unreadable:
        @staticmethod
        def load():
            raise OSError("config unreadable")

    waited, reasons, _client = await _expire_one_card(monkeypatch, loader=_Unreadable)
    assert waited == [3600]
    assert reasons == ["the channel approval card went unanswered for 3600s"]
