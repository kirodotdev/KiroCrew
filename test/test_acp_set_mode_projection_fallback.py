"""``session/set_mode`` never activates a copy of an agent older than the one prepared.

kiro-cli 2.25.0 and 2.26.0 reload ``~/.kiro/agents`` on a data write and never on
a rename, so an alias ``atomic_write`` renamed into place after the process
started answers ``Mode '<alias>' not found`` while the file is on disk. The
runtime re-prepares the projection right before ``set_mode``, so any change to a
source spec since spawn names exactly such an alias. The bracket forces a reload,
retries, and then FAILS the start with the remedy rather than activate an older
alias or the authored spec kiro-cli cached, either of which may still carry a
permission the change removed.

These drive the REAL reader and ``_send_and_await`` against a fake kiro-cli that
answers ``set_mode`` from the set of names it has "loaded", so the name
translation, the error mapping and the refusal are all the product's own.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpModeNotFound, AcpRuntimeError
from kiro_crew.acp.skill_projection import NativeSkillProjection
from kiro_crew.acp.types import METHOD_SET_MODE

STALE_ALIAS = "kirocrew-skill-view-" + "a" * 24
FRESH_ALIAS = "kirocrew-skill-view-" + "b" * 24


class _FakeKiro:
    """Answers awaited requests the way kiro-cli does, from what it has loaded."""

    def __init__(self, reader: asyncio.StreamReader, loaded: set[str]) -> None:
        self.reader = reader
        self.loaded = loaded
        self.set_modes: list[str] = []
        self.reload_after_misses: int | None = None
        self.other_error: dict | None = None
        self._misses = 0

    def write(self, data: bytes) -> None:
        frame = json.loads(data)
        if "id" not in frame:
            return
        method = frame.get("method")
        if method == METHOD_SET_MODE:
            mode = frame["params"]["modeId"]
            self.set_modes.append(mode)
            if self.other_error is not None:
                self._answer(frame["id"], error=self.other_error)
            elif mode in self.loaded:
                self._answer(frame["id"], result={})
            else:
                self._misses += 1
                if (
                    self.reload_after_misses is not None
                    and self._misses >= self.reload_after_misses
                ):
                    self.loaded.add(FRESH_ALIAS)
                self._answer(
                    frame["id"],
                    error={
                        "code": -32603,
                        "message": "Internal error",
                        "data": f"Mode '{mode}' not found",
                    },
                )
        else:
            self._answer(frame["id"], result={})

    def _answer(self, req_id: int, **body: object) -> None:
        self.reader.feed_data(
            (json.dumps({"jsonrpc": "2.0", "id": req_id, **body}) + "\n").encode()
        )


def _runtime(loaded: set[str]) -> tuple[AcpRuntime, _FakeKiro]:
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()
    kiro = _FakeKiro(reader, loaded)
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock(side_effect=kiro.write)
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    rt._native_skill_projection = NativeSkillProjection({"ops": FRESH_ALIAS})
    return rt, kiro


@pytest.fixture(autouse=True)
def announced(monkeypatch):
    """Record the in-place rescan nudges instead of touching an agents directory."""
    from kiro_crew.acp import skill_projection

    seen: list[str] = []
    monkeypatch.setattr(skill_projection, "announce_alias", seen.append)
    return seen


@pytest.fixture
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(runtime_mod, "_PROJECTED_MODE_RETRY_DELAYS_SECS", (0.0, 0.0))


@pytest.fixture
def counted(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(runtime_mod, "emit_counter", lambda name, attrs: seen.append((name, attrs)))
    return seen


async def _with_reader(rt: AcpRuntime, coro):
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    try:
        return await asyncio.wait_for(coro, timeout=10)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _activate(
    rt: AcpRuntime,
    projection: NativeSkillProjection | None,
    handle: object | None = None,
) -> None:
    """Run the real bracket with the re-preparation returning *projection*.

    *handle* defaults to one with no recorded ``spec_fingerprint`` (the mid-life
    activation guard is then a no-op), so the pre-existing cases are unchanged.
    """
    rt.terminate_session = AsyncMock()  # type: ignore[method-assign]
    if handle is None:
        handle = MagicMock()
        handle.spec_denied_tools = frozenset()
        handle.spec_fingerprint = None
    with (
        patch(
            "kiro_crew.acp.skill_projection.prepare_native_skill_projection",
            return_value=projection,
        ),
        patch("kiro_crew.agent.require_unchanged_derived_spec", return_value=None),
    ):
        await rt._activate_mode_bracketed(
            "s1",
            "ops",
            budget=5.0,
            payload_snapshot=None,
            wire_registered=True,
            handle=handle,
        )


def _attempts() -> int:
    return 1 + len(runtime_mod._PROJECTED_MODE_RETRY_DELAYS_SECS)


SIBLING_SPAWN_ALIAS = "kirocrew-skill-view-" + "c" * 24
SIBLING_FRESH_ALIAS = "kirocrew-skill-view-" + "d" * 24


def _listed(rt: AcpRuntime, *ids: str) -> list[str]:
    """What ``availableModes`` says after the runtime's inbound translation."""
    frame = rt._native_skill_projection.frame({"availableModes": [{"id": i} for i in ids]})
    return [mode["id"] for mode in frame["availableModes"]]


@pytest.mark.asyncio
async def test_an_alias_the_host_never_loads_fails_the_start_with_the_remedy(
    no_retry_wait, counted, caplog
):
    """The incident's shape: the view changed since spawn and kiro-cli never loads
    the new alias. The start fails naming the agent and the restart -- and neither
    the authored agent nor any older alias is activated in its place."""
    rt, kiro = _runtime({STALE_ALIAS, "ops"})
    caplog.set_level(logging.WARNING, logger="kiro_crew.acp.runtime")

    with pytest.raises(AcpRuntimeError, match="Restart the gateway") as excinfo:
        await _with_reader(rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS})))

    assert "'ops'" in str(excinfo.value)
    assert kiro.set_modes == [FRESH_ALIAS] * _attempts()
    rt.terminate_session.assert_awaited_once_with("s1")
    assert [a["outcome"] for _n, a in counted] == ["refused_unloaded"]
    assert any("refusing to start" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_alias_the_host_loads_after_its_reload_is_used(no_retry_wait, caplog):
    """The forced reload makes kiro-cli pick the alias up; the retry lands on it."""
    rt, kiro = _runtime({"ops"})
    kiro.reload_after_misses = 1
    caplog.set_level(logging.WARNING, logger="kiro_crew.acp.runtime")

    await _with_reader(rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS})))

    rt.terminate_session.assert_not_awaited()
    assert kiro.set_modes == [FRESH_ALIAS, FRESH_ALIAS]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_without_a_projection_a_missing_mode_keeps_the_repair_sentence(no_retry_wait):
    """No alias was involved, so there is nothing to reload or retry, and the
    existing actionable error stands."""
    rt, kiro = _runtime(set())
    rt._native_skill_projection = None

    with pytest.raises(AcpModeNotFound) as excinfo:
        await _with_reader(rt, _activate(rt, None))

    assert kiro.set_modes == ["ops"]
    assert "kirocrew setup --agent-only" in str(excinfo.value)


@pytest.mark.asyncio
async def test_any_other_set_mode_error_propagates_without_a_retry(no_retry_wait):
    rt, kiro = _runtime({FRESH_ALIAS})
    kiro.other_error = {"code": -32603, "message": "Internal error", "data": "boom"}

    with pytest.raises(AcpRuntimeError) as excinfo:
        await _with_reader(rt, _activate(rt, rt._native_skill_projection))

    assert not isinstance(excinfo.value, AcpModeNotFound)
    assert kiro.set_modes == [FRESH_ALIAS]


def test_the_retry_schedule_outlasts_kiro_clis_reload_debounce_and_stays_bounded():
    """kiro-cli reloads 500 ms after the LAST data write (a trailing debounce,
    then a full rescan); measured on 2.26.0 a written alias became selectable
    between 0.7 s and 1.5 s after it was published. No wait may be shorter than
    the debounce, the schedule must cover that window with room to spare, and
    it must stay a small fraction of the set_mode budget."""
    delays = runtime_mod._PROJECTED_MODE_RETRY_DELAYS_SECS
    assert delays and min(delays) >= 0.5
    assert 2.5 <= sum(delays) <= 5.0


@pytest.mark.asyncio
async def test_a_changed_view_sends_the_fresh_alias_and_never_the_spawn_one(no_retry_wait, counted):
    """The spawn alias may hold a generation of the spec an edit has since removed
    a server or an auto-approval from, so set_mode names the fresh alias; a host
    that loaded it answers first time and nothing is counted."""
    rt, kiro = _runtime({STALE_ALIAS, FRESH_ALIAS})
    spawn = NativeSkillProjection({"ops": STALE_ALIAS})
    rt._native_skill_projection = spawn
    rt._spawn_skill_projection = spawn
    fresh = NativeSkillProjection({"ops": FRESH_ALIAS})

    await _with_reader(rt, _activate(rt, fresh))

    assert kiro.set_modes == [FRESH_ALIAS]
    assert rt._native_skill_projection is fresh
    assert counted == []
    rt.terminate_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_revoked_auto_approval_is_never_usable_on_a_rename_blind_host(no_retry_wait):
    """The spec lost an auto-approval after spawn and the host never reloads, so
    its only copies of the agent are the spawn alias and the authored spec it
    cached -- both carrying the revoked grant. Neither is ever activated."""
    rt, kiro = _runtime({STALE_ALIAS, "ops"})
    spawn = NativeSkillProjection({"ops": STALE_ALIAS})
    rt._native_skill_projection = spawn
    rt._spawn_skill_projection = spawn

    with pytest.raises(AcpRuntimeError, match="Restart the gateway"):
        await _with_reader(rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS})))

    assert STALE_ALIAS not in kiro.set_modes
    assert "ops" not in kiro.set_modes


@pytest.mark.asyncio
async def test_a_re_preparation_that_cannot_run_fails_the_start(no_retry_wait, counted):
    """``None`` (the alias lock is busy): nothing proves any alias still says what
    the spec says now, so the start fails and nothing is sent."""
    rt, kiro = _runtime({STALE_ALIAS, "ops"})
    spawn = NativeSkillProjection({"ops": STALE_ALIAS})
    rt._native_skill_projection = spawn

    with pytest.raises(AcpRuntimeError, match="could not be prepared"):
        await _with_reader(rt, _activate(rt, None))

    assert kiro.set_modes == []
    rt.terminate_session.assert_awaited_once_with("s1")
    assert counted == [(runtime_mod.SKILL_VIEW_FALLBACKS, {"outcome": "refused_unprepared"})]


@pytest.mark.asyncio
async def test_aliases_the_host_loaded_earlier_stay_selectable_after_a_fresh_view(
    no_retry_wait, counted
):
    """Adopting a projection that renamed a SIBLING must not hide the sibling:
    the host still lists it under its spawn alias, and ``_mode_available`` reads
    ``availableModes`` through the adopted projection. Recognition survives a
    second adoption too."""
    rt, kiro = _runtime({STALE_ALIAS, SIBLING_SPAWN_ALIAS})
    spawn = NativeSkillProjection({"ops": STALE_ALIAS, "dev": SIBLING_SPAWN_ALIAS})
    rt._spawn_skill_projection = spawn
    rt._native_skill_projection = spawn

    for _ in range(2):
        await _with_reader(
            rt,
            _activate(rt, NativeSkillProjection({"ops": STALE_ALIAS, "dev": SIBLING_FRESH_ALIAS})),
        )
        assert _listed(rt, STALE_ALIAS, SIBLING_SPAWN_ALIAS) == ["ops", "dev"]
        assert rt._mode_available(
            "dev",
            {
                "modes": rt._native_skill_projection.frame(
                    {"availableModes": [{"id": SIBLING_SPAWN_ALIAS}]}
                )
            },
        )

    assert kiro.set_modes == [STALE_ALIAS, STALE_ALIAS]
    assert counted == []


def test_an_agent_listed_only_under_its_authored_id_stays_selectable():
    """The host may list the agent only under its authored id; it stays listed
    (once), so the start reaches set_mode and its clear error rather than
    "not installed", while unprojected host agents stay hidden."""
    projection = NativeSkillProjection({"ops": FRESH_ALIAS})
    frame = projection.frame(
        {"availableModes": [{"id": FRESH_ALIAS}, {"id": "ops"}, {"id": "kiro_default"}]}
    )
    assert [m["id"] for m in frame["availableModes"]] == ["ops"]
    assert [
        m["id"] for m in projection.frame({"availableModes": [{"id": "ops"}]})["availableModes"]
    ] == ["ops"]


@pytest.mark.asyncio
async def test_every_outcome_below_the_first_try_is_counted(no_retry_wait, counted):
    rt, kiro = _runtime({"ops"})
    kiro.reload_after_misses = 1
    await _with_reader(rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS})))
    assert counted == [(runtime_mod.SKILL_VIEW_FALLBACKS, {"outcome": "loaded_after_retry"})]

    counted.clear()
    rt, kiro = _runtime({"ops"})
    with pytest.raises(AcpRuntimeError):
        await _with_reader(rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS})))
    assert counted == [(runtime_mod.SKILL_VIEW_FALLBACKS, {"outcome": "refused_unloaded"})]


@pytest.mark.asyncio
async def test_a_retry_never_re_translates_through_a_projection_swapped_in_meanwhile(
    no_retry_wait, counted
):
    """The retry sleeps; a concurrent session start can replace the runtime's
    projection in that window with an older preparation whose alias the host DID
    load and which still carries grants an edit removed. Every attempt sends the
    alias this bracket prepared -- never that one."""
    rt, kiro = _runtime({STALE_ALIAS, "ops"})
    original = rt._send_and_await

    async def send_then_swap(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        finally:
            rt._native_skill_projection = NativeSkillProjection({"ops": STALE_ALIAS})

    rt._send_and_await = send_then_swap  # type: ignore[method-assign]

    with pytest.raises(AcpRuntimeError, match="Restart the gateway"):
        await _with_reader(rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS})))

    assert kiro.set_modes == [FRESH_ALIAS] * _attempts()


@pytest.mark.asyncio
async def test_a_miss_forces_a_rescan_before_each_retry(no_retry_wait, announced, monkeypatch):
    """A miss triggers no reload in kiro-cli, and the rename that published the
    alias is invisible to its watcher; each retry is preceded by a same-bytes
    in-place rewrite of the alias, which is a data write it DOES reload on."""
    from kiro_crew.acp import skill_projection

    rt, kiro = _runtime({"ops"})

    def rescan(alias):
        announced.append(alias)
        kiro.loaded.add(alias)

    monkeypatch.setattr(skill_projection, "announce_alias", rescan)
    await _with_reader(rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS})))

    assert announced == [FRESH_ALIAS]
    assert kiro.set_modes == [FRESH_ALIAS, FRESH_ALIAS]


def test_recognised_aliases_stay_bounded_keep_the_spawn_ones_first_and_warn(monkeypatch, caplog):
    """A process whose specs are edited without end publishes an alias per edit;
    what a projection keeps translating is bounded, the spawn aliases are
    recognised first, and dropping any says so once."""
    from kiro_crew.acp import skill_projection

    assert skill_projection._RECOGNISED_ALIASES_MAX == 1024
    monkeypatch.setattr(skill_projection, "_RECOGNISED_ALIASES_MAX", 8)
    spawn = NativeSkillProjection({"ops": STALE_ALIAS, "dev": SIBLING_SPAWN_ALIAS})
    earlier = NativeSkillProjection({"ops": FRESH_ALIAS})
    for n in range(50):
        earlier._recognised[f"kirocrew-skill-view-{n:024d}"] = "ops"
    current = NativeSkillProjection({"ops": "kirocrew-skill-view-" + "e" * 24})

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        current.recognise(spawn)
        current.recognise(earlier)
        current.recognise(earlier)

    assert len(current._recognised) == 8
    assert current._recognised[STALE_ALIAS] == "ops"
    assert current._recognised[SIBLING_SPAWN_ALIAS] == "dev"
    warned = [r for r in caplog.records if "not recognised past" in r.getMessage()]
    assert len(warned) == 1


# --- Mid-life edit window: whole-spec fingerprint guard ---
#
# The mount fingerprints the resolved agent spec at ``session/new``
# (``handle.spec_fingerprint``). The activation bracket re-reads the spec the
# same way and refuses the first turn if the fingerprint changed, or if the spec
# cannot be read. Any edit between ``session/new`` and activation refuses by
# construction -- there is no per-field predicate to leave a case uncovered.


@pytest.mark.asyncio
async def test_the_spec_changed_in_the_gap_refuses_the_session(no_retry_wait):
    """The spec resolved at activation fingerprints differently from the one
    recorded at session/new (any edit in the gap), so the start is refused."""
    rt, kiro = _runtime({FRESH_ALIAS})
    handle = MagicMock()
    handle._bound_cwd = ""
    handle.spec_fingerprint = "sha-at-session-new"

    with patch(
        "kiro_crew.acp.runtime._resolved_spec_fingerprint",
        return_value="sha-after-an-edit",  # the activated spec hashes differently
    ):
        with pytest.raises(AcpRuntimeError) as excinfo:
            await _with_reader(
                rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS}), handle=handle)
            )

    assert "settings changed" in str(excinfo.value)
    assert "Reopen the chat" in str(excinfo.value)
    rt.terminate_session.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_the_spec_unchanged_in_the_gap_starts(no_retry_wait):
    """The activated spec fingerprints identically to the session/new read, so
    nothing changed in the gap and the start proceeds."""
    rt, kiro = _runtime({FRESH_ALIAS})
    handle = MagicMock()
    handle._bound_cwd = ""
    handle.spec_fingerprint = "sha-stable"

    with patch(
        "kiro_crew.acp.runtime._resolved_spec_fingerprint",
        return_value="sha-stable",
    ):
        await _with_reader(
            rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS}), handle=handle)
        )

    rt.terminate_session.assert_not_awaited()
    assert kiro.set_modes == [FRESH_ALIAS]


@pytest.mark.asyncio
async def test_the_spec_cannot_be_read_at_activation_fails_closed(no_retry_wait):
    """The spec cannot be re-read at activation (the resolver returns None), so the
    fingerprint cannot be confirmed and the start fails closed."""
    rt, kiro = _runtime({FRESH_ALIAS})
    handle = MagicMock()
    handle._bound_cwd = ""
    handle.spec_fingerprint = "sha-at-session-new"

    with patch(
        "kiro_crew.acp.runtime._resolved_spec_fingerprint",
        return_value=None,  # unreadable -> no fingerprint -> differs -> refuse
    ):
        with pytest.raises(AcpRuntimeError) as excinfo:
            await _with_reader(
                rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS}), handle=handle)
            )

    assert "settings changed" in str(excinfo.value)
    rt.terminate_session.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_f6_withheld_core_plus_added_restriction_is_refused(no_retry_wait):
    """F6: at session/new the core element mounts with nothing disabled (so the gate
    carries no deny), then an operator disables a tool in the gap. Under the old
    deny-set comparison both sides read empty and the start slipped through. The
    whole-spec fingerprint catches it: adding the restriction changes the spec, so
    the fingerprint differs and the start is refused."""
    rt, kiro = _runtime({FRESH_ALIAS})
    handle = MagicMock()
    handle._bound_cwd = ""
    handle.spec_denied_tools = frozenset()  # nothing carried at session/new
    handle.spec_fingerprint = "sha-core-mounted-no-deny"

    with patch(
        "kiro_crew.acp.runtime._resolved_spec_fingerprint",
        return_value="sha-core-mounted-with-added-deny",  # the gap edit
    ):
        with pytest.raises(AcpRuntimeError) as excinfo:
            await _with_reader(
                rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS}), handle=handle)
            )

    assert "settings changed" in str(excinfo.value)
    rt.terminate_session.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_no_fingerprint_recorded_is_a_no_op(no_retry_wait):
    """A session with no recorded fingerprint (a backend that records none) runs the
    guard as a no-op and starts normally."""
    rt, kiro = _runtime({FRESH_ALIAS})
    handle = MagicMock()
    handle._bound_cwd = ""
    handle.spec_fingerprint = None

    # The resolver must not even be consulted when there is nothing to compare.
    with patch(
        "kiro_crew.acp.runtime._resolved_spec_fingerprint",
        side_effect=AssertionError("resolver should not be called with no fingerprint"),
    ):
        await _with_reader(
            rt, _activate(rt, NativeSkillProjection({"ops": FRESH_ALIAS}), handle=handle)
        )

    rt.terminate_session.assert_not_awaited()
    assert kiro.set_modes == [FRESH_ALIAS]


def test_resolved_spec_fingerprint_ignores_volatile_env_but_not_real_change():
    """The activation fingerprint normalizes per-launch MCP env nonce VALUES through
    the same volatile-env step the worker-spec fingerprint uses, so a launcher
    re-stamping a nonce on every concurrent session does not read as a spec change
    (which would refuse the session). A real change still moves the hash."""
    from kiro_crew.acp import runtime as _runtime_mod
    from kiro_crew.agent_spec_format import volatile_env_keys

    keys = sorted(volatile_env_keys())
    assert keys, "expected at least one volatile env key"
    k = keys[0]
    base = {"mcpServers": {"kirocrew-core": {"env": {k: "n1", "REAL": "x"}}}}
    volatile_only = {"mcpServers": {"kirocrew-core": {"env": {k: "n2", "REAL": "x"}}}}
    real_change = {"mcpServers": {"kirocrew-core": {"env": {k: "n1", "REAL": "y"}}}}

    with patch(
        "kiro_crew.acp.session_mcp._agent_spec_for",
        side_effect=[base, volatile_only, real_change],
    ):
        fp_base = _runtime_mod._resolved_spec_fingerprint("ops", None)
        fp_volatile = _runtime_mod._resolved_spec_fingerprint("ops", None)
        fp_real = _runtime_mod._resolved_spec_fingerprint("ops", None)

    assert fp_base == fp_volatile, "a volatile-env-only change must not move the fingerprint"
    assert fp_base != fp_real, "a real spec change must move the fingerprint"
