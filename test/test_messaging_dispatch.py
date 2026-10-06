"""What stays pinned beside the channel turn pipeline's interface tests.

The pipeline's behaviour is tested through its interface in
``test_channel_turns.py``. This file keeps what is not a turn case: the shared
governance cancel predicate, the tool-less agent spec, the generation reader, and
the repository-wide tripwires over every turn-open site. The ``_Sessions``,
``_Renderer``, ``_CtxBuilder``, ``_Driver``, ``_turn`` and ``_patch_pipeline``
helpers stay because other suites still drive ``drive_turn`` with them.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn
from kiro_crew.session_allocation import SessionClosingError


class _Sessions:
    """Minimal stand-in that counts the calls this contract is about."""

    def __init__(self, raise_on_acquire: bool = False, closing: bool = False):
        self.released = 0
        self.successes = 0
        self.failures = 0
        self.resets = 0
        self._raise_on_acquire = raise_on_acquire
        #: Mirrors SessionManager._closing. When set, begin_turn refuses the
        #: dispatch exactly as the real gate does once close_all has run.
        self.closing = closing
        self.begin_turns = 0
        #: ``(key, agent)`` of every acquire, and the agent an existing session is
        #: bound to (``None`` = the session is new and takes the agent asked for).
        self.acquired: list[tuple[str, Any]] = []
        self.acquire_extra: list[dict[str, Any]] = []
        self.bound_agent: str | None = None
        #: The ACP backend the returned provider reports, read the way the real
        #: pipeline reads it (``provider.client.backend``).
        self.backend: str | None = ACP_BACKEND_KIRO

    async def get_or_create(self, key, agent=None, channel_id=None, **extra):
        if self._raise_on_acquire:
            raise RuntimeError("cold start failed")
        self.acquired.append((key, agent))
        self.acquire_extra.append(dict(extra))
        if self.bound_agent is None:
            self.bound_agent = agent
        return SimpleNamespace(client=SimpleNamespace(backend=self.backend)), False, False

    def get_agent(self, key):
        return self.bound_agent or ""

    def begin_turn(self, key):
        """The real manager's synchronous pre-dispatch closing gate."""
        self.begin_turns += 1
        if self.closing:
            raise SessionClosingError("SessionManager is closing")

    async def set_channel(self, key, channel_id):
        pass

    def record_success(self, key):
        self.successes += 1

    async def reset(self, key):
        self.resets += 1

    async def record_failure(self, key):
        self.failures += 1

    def release(self, key):
        self.released += 1

    def get_provider(self, key):
        return object()


class _Renderer:
    """Renderer whose ``close`` can fail the way a real one can mid-flush."""

    def __init__(self, close_raises: bool = False):
        self.close_raises = close_raises
        self.closed = 0
        self.notes: list[str] = []

    async def on_turn_start(self):
        pass

    async def on_text_chunk(self, text):
        self.notes.append(text)

    async def on_done(self):
        pass

    async def close(self):
        self.closed += 1
        if self.close_raises:
            raise RuntimeError("renderer finalization failed")


class _CtxBuilder:
    def build_message(self, text, is_new, session_key, **kw):
        return text, None


class _Driver:
    last_stop_reason = ""
    completion_observed = True

    def __init__(self, *a, **kw):
        # Mirrors the real TurnDriver: the shutdown gate is supplied at
        # construction and invoked by run(), immediately before the provider
        # stream would open. A stand-in that swallowed it would let these tests
        # pass while the gate was wired nowhere.
        self._closing_gate = kw.get("closing_gate")

    async def run(self, message):
        if self._closing_gate is not None:
            self._closing_gate()
        return "the reply"


def _turn(renderer: Any) -> ChannelTurn:
    return ChannelTurn(
        channel_type="weixin",
        session_key="weixin:agentA:direct:userA",
        conversation_id="weixin:userA",
        agent="agentA",
        user_text="hi",
        renderer=renderer,
        approval_mode="auto",
    )


def _patch_pipeline(monkeypatch, *, permitted: bool = True):
    """Stub everything drive_turn touches except the finalization under test."""

    async def _permitted(_channel_type):
        return permitted

    async def _publish(_sessions, _key):
        pass

    async def _embed(fn, *args, **kw):
        return fn(*args, **kw)

    monkeypatch.setattr(D, "inbound_permitted", _permitted)
    monkeypatch.setattr(D, "publish_turn_identity", _publish)
    monkeypatch.setattr(D, "run_in_embed_pool", _embed)
    monkeypatch.setattr(D, "TurnDriver", _Driver)


def test_a_driver_without_a_stop_reason_still_finishes_the_turn(monkeypatch) -> None:
    """The stop-reason read is defensive, like every other attribute read on
    this seam. ``TurnDriver`` is resolved through the module attribute, so a
    stand-in that predates the field must mean "no synthetic completion" — not
    an AttributeError raised at a real inbound message AFTER the turn already
    ran and the user already got the answer."""

    class _FieldlessDriver:
        def __init__(self, *a, **kw) -> None:
            pass

        async def run(self, message: str) -> str:
            return "the answer"

    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(D, "TurnDriver", _FieldlessDriver)
    sessions = _Sessions()
    renderer = _Renderer()

    asyncio.run(drive_turn(_turn(renderer), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert sessions.resets == 0
    assert sessions.released == 1


def test_every_turn_open_site_is_gated_on_the_shutdown_state() -> None:
    """Ratchet: the shutdown gate is wired at every site AND placed atomically.

    Two halves, because either alone is satisfiable while the race stays open.

    A gate at the CALL SITE is not enough, which is what the first version of
    this change got wrong: ``TurnDriver.run`` awaits ``renderer.on_turn_start()``
    -- a platform round-trip -- before opening the provider stream, so a restart
    landing there still let the prompt register behind ``close_all``'s drain
    snapshot. The only atomic placement is inside ``run()``, immediately before
    the stream, so the gate lives there and each dispatcher passes it in.

    So: every ``TurnDriver(...)`` construction must pass ``closing_gate``, and in
    the driver no await may occur between the gate and provider stream. A
    structured monitor may synchronously mark its claim accepted in that span;
    because it cannot yield, shutdown still cannot take a drain snapshot there.
    """
    src = Path(D.__file__).resolve().parent.parent

    # Half 1 -- every construction wires a gate.
    unwired: list[str] = []
    sites = 0
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "TurnDriver(" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "TurnDriver"
            ):
                continue
            sites += 1
            if not any(keyword.arg == "closing_gate" for keyword in node.keywords):
                unwired.append(f"{path.relative_to(src)}:{node.lineno}")
    assert not unwired, "TurnDriver built without a shutdown gate:\n" + "\n".join(unwired)
    assert sites >= 4, f"expected the known TurnDriver sites, found {sites}"

    # Half 2 -- the gate is atomic with the stream the driver opens.
    driver_lines = (src / "messaging" / "driver.py").read_text(encoding="utf-8").splitlines()
    opens = [i for i, ln in enumerate(driver_lines) if "self.provider.stream(" in ln]
    assert opens, "could not find the provider stream in the driver"
    for idx in opens:
        window = driver_lines[max(0, idx - 12) : idx]
        gates = [i for i, line in enumerate(window) if "self.closing_gate()" in line]
        assert gates, f"messaging/driver.py:{idx + 1} opens a turn without the gate"
        after_gate = window[gates[-1] + 1 :]
        assert not any(
            "await " in line for line in after_gate
        ), f"messaging/driver.py:{idx + 1} yields between the gate and stream"


def test_the_generation_reader_is_a_noop_for_keys_without_one() -> None:
    """A Slack thread key has no generation grammar and a double may lack the
    reader; neither can manufacture a supersession."""

    class _NoReader:
        pass

    class _Reader:
        def max_generation(self, bucket):
            return 7

    assert D.session_conversation_generation(_Reader(), "slack:1700000000.000100") == 0
    assert D.session_conversation_generation(_NoReader(), "weixin:agentA:direct:userA") == 0
    assert D.session_conversation_generation(_Reader(), "weixin:agentA:direct:userA:gen3") == 7


def test_every_pipeline_channel_stop_path_records_the_stop() -> None:
    """Discovery tripwire, not a hand-kept list. Every channel dispatcher that
    rides ``drive_turn`` and offers a Stop must record it on the session
    manager BEFORE its busy check -- through ``stop_turn`` or
    ``stop_running_turn`` (which record on their own) or by calling
    ``note_user_stop`` next to a direct ``provider.cancel``. A channel that
    cancels the provider directly without recording leaves the transient
    compaction replay blind to a Stop issued in the reset gap."""
    root = Path(__file__).resolve().parents[1] / "src/kiro_crew"
    checked: list[str] = []
    for path in sorted(root.glob("*/transport_dispatch.py")):
        source = path.read_text(encoding="utf-8")
        if "drive_turn(" not in source:
            continue
        cancels_directly = "cancel(wait_ack_timeout=0)" in source
        records = (
            ".stop_turn(" in source or "stop_running_turn(" in source or "note_user_stop(" in source
        )
        if cancels_directly:
            assert "note_user_stop(" in source or ".stop_turn(" in source, path
            checked.append(path.parent.name)
        elif records:
            checked.append(path.parent.name)
    # Non-vacuity: the channels known to cancel directly are all covered.
    assert {"webex", "wecom", "weixin", "teams", "whatsapp"} <= set(checked), checked


def test_the_driver_reaches_its_renderer_only_through_the_guarded_surface() -> None:
    """Tripwire for the wrapper: the guard subclasses ``Renderer`` and forwards
    every declared handler, but the two methods the driver itself calls are the
    ones that carry the hold logic. A driver that starts calling something else
    must widen the guard on purpose, not silently bypass it."""
    source = (Path(__file__).resolve().parents[1] / "src/kiro_crew/messaging/driver.py").read_text(
        encoding="utf-8"
    )
    used = set(re.findall(r"self\.renderer\.([a-z_]+)", source))
    assert used == {"dispatch", "on_turn_start"}, used
    for name in used:
        assert name in D._TransientCompactionRetryGuard.__dict__, name


class _GovernanceStub:
    """Records what the shared gate asked governance, and answers a fixed verdict."""

    def __init__(self, permitted: bool) -> None:
        self.permitted = permitted
        self.asked: list[str] = []

    async def __call__(self, channel_type: str) -> bool:
        self.asked.append(channel_type)
        return self.permitted


def _gate(monkeypatch, *, permitted: bool) -> _GovernanceStub:
    stub = _GovernanceStub(permitted)
    monkeypatch.setattr(D, "channel_inbound_permitted", stub)
    return stub


class TestPureCancelPredicate:
    """PURE is what makes the governance exemption safe to grant."""

    def test_every_channel_spelling_is_recognised(self) -> None:
        # 停止 is WeCom's, and it is the one that was missing: the ASCII spellings
        # are not reachable for a user whose whole surface is Chinese.
        for text in ("/stop", "/cancel", "!stop", "!cancel", "停止"):
            assert D.is_pure_cancel(text), text
            assert D.is_pure_cancel(f"  {text.upper()}  "), text

    def test_an_attachment_makes_it_impure(self) -> None:
        """The channel fetches media AFTER authorizing, so this is the leak edge."""
        assert D.is_pure_cancel("/stop", has_attachments=True) is False

    def test_anything_beyond_the_word_is_an_ordinary_message(self) -> None:
        for text in (
            "/stop please",
            "please /stop",
            "/stopwatch",
            "/restart",
            "!restart",
            "stop",
            "",
        ):
            assert D.is_pure_cancel(text) is False, text

    def test_the_shared_set_covers_the_channel_command_tables(self) -> None:
        """Drift tripwire: a channel alias the shared gate does not know is a hole.

        DISCOVERED rather than listed. The first version of this test imported
        Discord's and Telegram's tables by name, which made it blind in exactly
        the way the mirror it guards is blind: WeCom, Teams and WhatsApp each
        declare their own stop spellings, and WeCom's ``停止`` reached its
        ``/help`` card while the shared exemption did not know the word. A test
        that hand-lists the channels is the same mirror one level up, so this
        walks the packages instead and a new channel is covered by existing.

        The three shapes below are the ones in the tree. An unrecognised shape
        FAILS rather than being skipped: a channel whose table this cannot read
        is a channel whose drift it cannot see, and silence there is the whole
        defect being re-created.
        """
        import importlib
        import pkgutil

        import kiro_crew

        found: dict[str, set[str]] = {}
        unreadable: list[str] = []
        for mod in pkgutil.iter_modules(kiro_crew.__path__):
            # `messaging` is the shared layer that OWNS the union rather than a
            # channel that contributes to it, so it is not a mirror of anything.
            if not mod.ispkg or mod.name == "messaging":
                continue
            try:
                commands = importlib.import_module(f"kiro_crew.{mod.name}.commands")
            except ModuleNotFoundError:
                continue
            aliases: set[str] = set()
            # Shape 1: a private frozenset (discord, telegram, wecom).
            stop_set = getattr(commands, "_STOP_ALIASES", None)
            if stop_set is not None:
                aliases |= set(stop_set)
            # Shape 2: ``(canonical, aliases, description)`` rows (teams).
            for row in getattr(commands, "COMMAND_SPEC", ()) or ():
                if len(row) == 3 and row[0] == "stop" and isinstance(row[1], tuple):
                    aliases |= set(row[1])
            # Shape 3: dataclass rows carrying ``.name`` / ``.aliases`` (whatsapp).
            for row in getattr(commands, "COMMANDS", ()) or ():
                if getattr(row, "name", "") == "stop":
                    aliases |= set(getattr(row, "aliases", ()))
            if aliases:
                found[mod.name] = aliases
                continue
            # No stop spellings read. That is legitimate for a channel with no
            # cancel command at all, but suspicious if the module mentions one.
            source = getattr(commands, "__doc__", "") or ""
            if "/stop" in source or "/cancel" in source:
                unreadable.append(mod.name)

        assert not unreadable, (
            "channel command tables this tripwire could not parse, so their drift "
            f"is invisible to it: {unreadable}"
        )
        # The channels known to ship a cancel today. A channel dropping out of
        # this set means the discovery above silently stopped seeing it.
        assert {"discord", "telegram", "wecom", "teams", "whatsapp"} <= set(found), found

        union = set().union(*found.values())
        missing = union - D._CANCEL_ALIASES
        assert not missing, f"cancel spellings the shared exemption would gate: {missing}"
        # And the reverse: a spelling in the shared set that no channel accepts is
        # a governance exemption granted to a word nothing can act on.
        assert not D._CANCEL_ALIASES - union, D._CANCEL_ALIASES - union


class TestCancellationSurvivesAGovernanceDeny:
    """A denied channel must still be able to halt the session it started.

    ``max_buttons=0`` channels have no Reject button to press, so the typed cancel
    is the only cancel affordance there is: gating it strands a runaway turn with
    no way to stop it, which is the opposite of what a deny is for.
    """

    def test_a_pure_cancel_is_permitted_on_a_denied_channel(self, monkeypatch) -> None:
        _gate(monkeypatch, permitted=False)
        assert asyncio.run(D.inbound_permitted("whatsapp", text="/stop")) is True

    def test_an_ordinary_message_is_still_dropped(self, monkeypatch) -> None:
        """Non-vacuity: the deny must still deny everything that is not a cancel."""
        _gate(monkeypatch, permitted=False)
        assert asyncio.run(D.inbound_permitted("whatsapp", text="summarise my inbox")) is False

    def test_a_restart_is_not_a_cancellation(self, monkeypatch) -> None:
        _gate(monkeypatch, permitted=False)
        assert asyncio.run(D.inbound_permitted("whatsapp", text="/restart")) is False

    def test_an_attachment_bearing_cancel_is_gated(self, monkeypatch) -> None:
        """Otherwise the denied channel still pays for the download."""
        _gate(monkeypatch, permitted=False)
        assert (
            asyncio.run(D.inbound_permitted("whatsapp", text="/stop", has_attachments=True))
            is False
        )

    def test_the_argument_less_call_stays_strict(self, monkeypatch) -> None:
        """``drive_turn``'s backstop names no text, so nothing is exempt there."""
        _gate(monkeypatch, permitted=False)
        assert asyncio.run(D.inbound_permitted("whatsapp")) is False

    def test_a_permitted_channel_still_short_circuits(self, monkeypatch) -> None:
        stub = _gate(monkeypatch, permitted=True)
        assert asyncio.run(D.inbound_permitted("whatsapp", text="anything")) is True
        assert stub.asked == ["whatsapp"], "governance must be consulted first, once"


class TestToollessAgentSpecIsTheBoundary:
    """The guest spec is the whole enforcement for an untrusted sender's turn. A
    change that grants it a tool or an MCP server would hand that tool to every
    untrusted sender on the kiro backend, so the emptiness is pinned here, where
    the boundary lives, not only where the file is written. It also talks to a
    person, so it carries a prompt of its own rather than the background helper's
    empty one."""

    def test_the_regenerated_guest_spec_mounts_no_tools_and_no_servers(self, tmp_path, monkeypatch):
        import json

        from kiro_crew import agent as agent_module

        monkeypatch.setattr(agent_module, "kiro_agents_dir_path", lambda: tmp_path)
        agent_module._install_guest_agent()
        spec = json.loads((tmp_path / agent_module._GUEST_AGENT_FILENAME).read_text())
        assert spec["name"] == D.TOOLLESS_TURN_AGENT
        assert spec["tools"] == [] and spec["mcpServers"] == {}
        # The user-level mcp.json must not be mounted either: kiro-cli defaults
        # ``includeMcpJson`` to True, which would spawn every configured server.
        assert spec["includeMcpJson"] is False
        assert "no tools" in spec["prompt"]

    def test_the_guest_spec_is_installed_with_the_lite_one(self, tmp_path, monkeypatch):
        """Every rebuild that writes the background agent writes the guest agent."""
        from kiro_crew import agent as agent_module

        monkeypatch.setattr(agent_module, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr(agent_module, "_background_agent_model", lambda: "auto")
        monkeypatch.setattr(agent_module, "_background_cc_model", lambda: "auto")
        monkeypatch.setattr(agent_module.agent_state, "set_cc_model", lambda *_a, **_k: None)
        agent_module._install_aim_capabilities()
        assert (tmp_path / agent_module._GUEST_AGENT_FILENAME).is_file()
        assert (tmp_path / agent_module._LITE_AGENT_FILENAME).is_file()
