"""Unit tests for :mod:`kiro_crew.mcp_caller` — KIROCREW_HOST_PID resolution.

The fork carries only the host-pid env-shortcut tests here (the wider
caller-identity wire-contract suite lives upstream); each test resets the
fork-only process-lifetime ``_FROM_ENV_CACHE`` so an already-resolved
identity cannot leak between tests.
"""

from __future__ import annotations

import asyncio
import os
from unittest import mock

import pytest

import kiro_crew.mcp_caller
from kiro_crew.mcp_caller import (
    _TENANT_NONCE_BYTES,
    TENANT_META_KEY,
    TENANT_SCHEMA_VERSION,
    CallerContext,
    build_caller_meta,
    build_tenant_meta,
    new_tenant_nonce,
    tenant_nonce_from_meta,
)


def test_single_session_identity_preserves_cached_caller(monkeypatch):
    """A memoised per-process identity is served without re-walking the tree."""
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.delenv("KIROCREW_STUB_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(
        kiro_crew.mcp_caller,
        "_FROM_ENV_CACHE",
        CallerContext(session_key="single"),
    )

    def _must_not_walk():
        raise AssertionError("the pid walk ran although a memo was present")

    monkeypatch.setattr(kiro_crew.mcp_caller, "_identity_from_pid_mapping", _must_not_walk)
    assert CallerContext.from_env().session_key == "single"


def test_an_unresolved_identity_is_not_memoised(monkeypatch):
    """A memoised absence would outlive the claim that fixes it.

    A warm-pool child resolves before its pidfile exists, so the first answer
    is legitimately empty; memoising it would leave the process unable to name
    its session for as long as it lives. The same holds for a co-tenant
    refusal, which must be re-read -- and re-reported -- rather than frozen.
    """
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.delenv("KIROCREW_STUB_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)

    answers = iter([kiro_crew.mcp_caller.OwnIdentity(), None])

    def _walk():
        nxt = next(answers, None)
        if nxt is not None:
            return nxt
        return kiro_crew.mcp_caller.OwnIdentity(
            session_key="dashboard:chat-claimed",
            source=kiro_crew.mcp_caller.SOURCE_PIDFILE,
        )

    monkeypatch.setattr(kiro_crew.mcp_caller, "_identity_from_pid_mapping", _walk)
    assert CallerContext.from_env().session_key == ""
    assert kiro_crew.mcp_caller._FROM_ENV_CACHE is None
    # The pidfile has since appeared: the second call must see it.
    assert CallerContext.from_env().session_key == "dashboard:chat-claimed"
    assert kiro_crew.mcp_caller._FROM_ENV_CACHE is not None


def test_a_refusing_protected_record_never_reaches_the_memo(monkeypatch):
    """Rung 1's refusal is an EMPTY key, so emptiness cannot mean "keep going".

    A record that exists and is invalid must stop the ladder: every rung below
    it -- a token, an env var, a pid mapping -- is writable by the same uid the
    binding exists to fence. The memo sits below all of them, so reading the
    refusal as an absence would answer a fenced process from a cached identity.
    """
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.delenv("KIROCREW_STUB_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(
        kiro_crew.mcp_caller,
        "_FROM_ENV_CACHE",
        CallerContext(session_key="someone-elses-session"),
    )
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.protected_member_session_for_pid",
        lambda _pid: "",
    )
    ctx = CallerContext.from_env()
    assert ctx.session_key == ""
    assert ctx.session_type == kiro_crew.mcp_caller.SOURCE_PROTECTED


def test_the_memo_never_shadows_a_session_scoped_rung(monkeypatch):
    """The ordering, not the key, is what keeps a rekey visible.

    A memo consulted in front of the token would answer for the life of the
    process, and no key can rescue that: a rekey rewrites the token's mapping
    while the token STRING survives, so every value this process can read is
    byte-identical before and after. On a runtime hosting several sessions that
    is how the first co-tenant to resolve becomes every later caller's answer,
    so the memo is reached only once rungs 1-3 have declined.
    """
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.delenv("KIROCREW_STUB_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(
        kiro_crew.mcp_caller,
        "_FROM_ENV_CACHE",
        CallerContext(session_key="stale-co-tenant"),
    )
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-mine")
    ctx = CallerContext.from_env()
    assert ctx.session_key == "dashboard:chat-mine"
    assert ctx.session_type == kiro_crew.mcp_caller.SOURCE_ENV
    # A session-scoped answer is not written back either: the next call must
    # re-read the rung rather than inherit this one.
    assert kiro_crew.mcp_caller._FROM_ENV_CACHE.session_key == "stale-co-tenant"


def test_caller_round_trip_needs_no_member_capability():
    ctx = CallerContext(session_key="dashboard:reviewer", from_gateway=True)
    meta = build_caller_meta(ctx)
    parsed = CallerContext.from_meta(meta)
    assert parsed is not None and parsed.session_key == ctx.session_key
    assert set(meta[kiro_crew.mcp_caller.CALLER_META_KEY]) == {
        "schemaVersion",
        "sessionKey",
        "sessionType",
        "principalId",
        "channelId",
    }


def test_session_token_round_trips_only_when_present():
    """The token is a bearer name: the wire block carries it only for the
    control-plane calls gatewayd chose to hand it to, and a backend reads it
    back into the typed field rather than digging in ``raw``."""
    bare = build_caller_meta(CallerContext(session_key="dashboard:reviewer"))
    assert "sessionToken" not in bare[kiro_crew.mcp_caller.CALLER_META_KEY]
    assert CallerContext.from_meta(bare).session_token == ""

    tokened = build_caller_meta(
        CallerContext(session_key="dashboard:reviewer", session_token="t" * 64)
    )
    assert tokened[kiro_crew.mcp_caller.CALLER_META_KEY]["sessionToken"] == "t" * 64
    parsed = CallerContext.from_meta(tokened)
    assert parsed is not None and parsed.from_gateway and parsed.session_token == "t" * 64


@pytest.mark.asyncio
async def test_interleaved_member_calls_keep_their_ordinary_session_identity(monkeypatch):
    from kiro_crew import mcp_core

    monkeypatch.setattr(mcp_core, "internal_caller", lambda: "core")
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "stale-process-session")
    both_started = asyncio.Event()
    count = 0

    async def request(session):
        nonlocal count
        caller = CallerContext.from_meta(build_caller_meta(CallerContext(session_key=session)))
        kiro_crew.mcp_caller.set_current_caller(caller)
        count += 1
        if count == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=2)
        try:
            return mcp_core._resolve_session_key_strict(), mcp_core._caller_header()
        finally:
            kiro_crew.mcp_caller.set_current_caller(None)

    assert await asyncio.gather(request("member:reviewer"), request("member:writer")) == [
        ("member:reviewer", {"X-Internal-Caller": "core"}),
        ("member:writer", {"X-Internal-Caller": "core"}),
    ]
    assert kiro_crew.mcp_caller.current_caller() is None


def test_from_env_uses_host_pid_env_before_walk(tmp_path, monkeypatch) -> None:
    """The sandbox launcher exports KIROCREW_HOST_PID (its own host pid — the
    exact pid the gateway keys session_pid files by). from_env must resolve
    via that env var directly, without depending on the /proc ancestor walk,
    which cannot match when the process's pid view diverges from the host's
    (PID-namespace sandboxing)."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    # File keyed by a pid that is NOT in this test process's real ancestry —
    # only the env var can find it.
    pid_file = tmp_path / "session_pid_987654.txt"
    pid_file.write_text("hostpid-session-789", encoding="utf-8")

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": "987654"},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            ctx = CallerContext.from_env()
    assert ctx.session_key == "hostpid-session-789"
    assert ctx.session_type == "pidfile"


def test_from_env_host_pid_missing_file_falls_back_to_walk(tmp_path, monkeypatch) -> None:
    """A stale/dangling KIROCREW_HOST_PID (no matching file) must not break
    the existing ancestor-walk fallback."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    parent_pid = os.getppid()
    (tmp_path / f"session_pid_{parent_pid}.txt").write_text("walk-session-111", encoding="utf-8")

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": "999999"},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            ctx = CallerContext.from_env()
    assert ctx.session_key == "walk-session-111"


# --- Only an ANSWER stops the walk ------------------------------------------
#
# ``session_pid_<pid>.txt`` lives in the same-uid, agent-writable config dir, so
# any local process can plant one at a NEARER ancestor pid. If an unparseable or
# proven-stale file stopped the walk there, the caller would end up with no
# session key -- and ``tools/call`` does not refuse on that (``no_session_key``
# is not in ``mcp_shared._UNRESOLVED_REFUSES_CALL``), so the operator's tool
# exclusions would go unenforced for a call the full walk resolves and enforces.
# These pin that only a named key and a CO-TENANT refusal (a real answer about
# this pid) stop it.

_MALFORMED_BODY = "planted-key\nplain-one\nplain-two"  # two plain lines -> refused parse


def test_a_malformed_nearer_mapping_does_not_stop_the_walk(tmp_path, monkeypatch) -> None:
    """HEADLINE: a planted unparseable file at the host pid must not strand the
    walk short of the ancestor holding the real mapping."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    (tmp_path / "session_pid_987654.txt").write_text(_MALFORMED_BODY, encoding="utf-8")
    real = tmp_path / f"session_pid_{os.getppid()}.txt"
    real.write_text("real-ancestor-session", encoding="utf-8")

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": "987654"},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            ctx = CallerContext.from_env()
    assert ctx.session_key == "real-ancestor-session"


def test_a_malformed_mapping_mid_chain_does_not_stop_the_walk(tmp_path, monkeypatch) -> None:
    """Same rule at the WALK site: the planted file sits on the immediate
    parent, the real mapping one hop further up."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    nearer, further = os.getppid(), 424242
    (tmp_path / f"session_pid_{nearer}.txt").write_text(_MALFORMED_BODY, encoding="utf-8")
    (tmp_path / f"session_pid_{further}.txt").write_text("further-session", encoding="utf-8")

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": ""},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            with mock.patch.object(
                kiro_crew.mcp_caller, "_parent_pid", side_effect=lambda p: further
            ):
                ctx = CallerContext.from_env()
    assert ctx.session_key == "further-session"


def test_a_recycled_mapping_does_not_stop_the_walk(tmp_path, monkeypatch) -> None:
    """The same stop fired on a proven recycle, which needs no planting at all:
    the sweep deliberately leaves a stale file on disk for every live recycled
    pid, so a legitimate tree would strand on its own housekeeping."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    from kiro_crew import platform_compat

    nearer, further = os.getppid(), 424242
    # A recorded token that cannot match the live one -> REFUSAL_RECYCLED.
    (tmp_path / f"session_pid_{nearer}.txt").write_text(
        "previous-owner-session\nrecorded-token-aaa", encoding="utf-8"
    )
    (tmp_path / f"session_pid_{further}.txt").write_text("further-session", encoding="utf-8")

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": ""},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            with mock.patch.object(
                platform_compat, "get_process_start_id", return_value="live-token-bbb"
            ):
                with mock.patch.object(
                    kiro_crew.mcp_caller, "_parent_pid", side_effect=lambda p: further
                ):
                    ctx = CallerContext.from_env()
    assert ctx.session_key == "further-session"


def test_a_co_tenant_refusal_still_stops_the_walk(tmp_path, monkeypatch) -> None:
    """The converse, so the narrowing cannot be widened into "walk past
    everything": a co-tenant refusal IS this pid's answer. Walking past it
    would reach a different process's mapping and name one of ITS sessions --
    the misattribution the tenant section exists to make visible."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    nearer, further = os.getppid(), 424242
    (tmp_path / f"session_pid_{nearer}.txt").write_text(
        "first-tenant\ntenants=2\ntenant=first-tenant\ntenant=second-tenant",
        encoding="utf-8",
    )
    (tmp_path / f"session_pid_{further}.txt").write_text("further-session", encoding="utf-8")

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": ""},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            with mock.patch.object(
                kiro_crew.mcp_caller, "_parent_pid", side_effect=lambda p: further
            ):
                ctx = CallerContext.from_env()
    assert ctx.session_key == ""
    assert ctx.session_key != "further-session"


def _exhausted_walk(tmp_path, *, plant: str | None, live_token: str = "live-token-bbb"):
    """Run the pid rung over a chain that holds no usable mapping at all.

    ``plant`` is the body written at the immediate parent, or ``None`` for a
    chain holding nothing. ``_parent_pid`` returns 1 so the walk ends after that
    one hop, which is what makes this the EXHAUSTION path rather than a stop.
    """
    from kiro_crew import platform_compat

    if plant is not None:
        (tmp_path / f"session_pid_{os.getppid()}.txt").write_text(plant, encoding="utf-8")
    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": ""},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            with mock.patch.object(
                platform_compat, "get_process_start_id", return_value=live_token
            ):
                with mock.patch.object(kiro_crew.mcp_caller, "_parent_pid", return_value=1):
                    return kiro_crew.mcp_caller._identity_from_pid_mapping()


def test_an_exhausted_walk_that_passed_a_malformed_record_refuses(tmp_path) -> None:
    """HEADLINE: walking PAST an unusable record must not also FORGET it. A chain
    that ends with no key, having held a record this reader could not use, is
    not the warm-pool absence -- and only the absence is a class ``tools/call``
    proceeds on, so reporting it as one runs the call with no exclusions."""
    identity = _exhausted_walk(tmp_path, plant=_MALFORMED_BODY)

    assert identity.session_key == ""
    assert identity.failed is True
    assert identity.source == kiro_crew.mcp_caller.SOURCE_PIDFILE


def test_an_exhausted_walk_that_passed_a_recycled_record_refuses(tmp_path) -> None:
    """The same rule for the refusal that needs no planting, so the pin cannot be
    satisfied by special-casing one shape: any record the walk could not use is
    evidence, whatever made it unusable."""
    identity = _exhausted_walk(tmp_path, plant="previous-owner-session\nrecorded-token-aaa")

    assert identity.session_key == ""
    assert identity.failed is True


def test_an_exhausted_walk_that_saw_nothing_stays_permissive(tmp_path) -> None:
    """The converse, and the reason the rule is keyed on a record being PRESENT
    rather than on the walk failing: a warm-pool session whose mapping is not
    published yet has an empty chain, and it must keep working rather than start
    denying its own calls."""
    identity = _exhausted_walk(tmp_path, plant=None)

    assert identity.session_key == ""
    assert identity.failed is False


@pytest.mark.parametrize(
    ("plant", "expected"),
    [
        (None, ""),
        (_MALFORMED_BODY, None),
    ],
    ids=["absent-proceeds", "unusable-refuses"],
)
def test_the_policy_lookup_separates_an_absence_from_an_unusable_record(
    tmp_path, monkeypatch, plant, expected
) -> None:
    """The consequence at the layer that acts on it. ``""`` is ``no_session_key``,
    which ``tools/call`` proceeds on; ``None`` is ``resolution_failed``, which it
    refuses. Both arrive with an empty key, so the flag is the only thing that
    separates an unenforced call from a refused one."""
    from kiro_crew import mcp_shared

    monkeypatch.setattr(
        kiro_crew.mcp_caller,
        "resolve_own_identity",
        lambda **kw: _exhausted_walk(tmp_path, plant=plant),
    )
    monkeypatch.setattr(
        mcp_shared, "resolve_own_identity", kiro_crew.mcp_caller.resolve_own_identity
    )

    assert mcp_shared._policy_session_key() == expected


# --- Per-connection tenant nonce --------------------------------------------
#
# The nonce exists for the case the caller block cannot serve: a connection the
# gateway cannot NAME. It is a namespace separator, so what these pin is that it
# stays one -- parsed leniently, never promoted to an identity, and never derived
# from anything a stub supplies.


def test_a_tenant_block_carries_the_nonce_and_nothing_else() -> None:
    meta = build_tenant_meta("n0nce")
    assert meta[TENANT_META_KEY]["nonce"] == "n0nce"
    assert meta[TENANT_META_KEY]["schemaVersion"] == TENANT_SCHEMA_VERSION
    assert tenant_nonce_from_meta(meta) == "n0nce"


def test_a_tenant_block_is_not_an_identity() -> None:
    """The separation the whole design rests on.

    If ``from_meta`` accepted a tenant block, an unnamed caller would reach every
    consumer of a session key -- cron ownership, callback routing, audit --
    carrying a value that names no principal while looking resolved.
    """
    assert CallerContext.from_meta(build_tenant_meta("n0nce")) is None


def test_a_caller_block_carries_no_nonce() -> None:
    """And the converse: the two blocks are independent, so a named caller's
    frame does not smuggle a separator the fallback would then append."""
    assert tenant_nonce_from_meta(build_caller_meta(CallerContext(session_key="s"))) == ""


@pytest.mark.parametrize(
    "meta",
    [
        None,
        "not a dict",
        {},
        {TENANT_META_KEY: "not a dict"},
        {TENANT_META_KEY: {}},
        {TENANT_META_KEY: {"nonce": "n"}},  # no schemaVersion
        {TENANT_META_KEY: {"schemaVersion": "1", "nonce": "n"}},  # not an int
        {TENANT_META_KEY: {"schemaVersion": 0, "nonce": "n"}},  # below v1
        {TENANT_META_KEY: {"schemaVersion": 1}},  # no nonce
        {TENANT_META_KEY: {"schemaVersion": 1, "nonce": 7}},  # not a string
    ],
)
def test_a_malformed_tenant_block_reads_as_absent(meta) -> None:
    """Every bad shape degrades to "no separator", never to an exception.

    The consumer's fallback (its own process) is correct in the 1:1 topology, so
    an unparseable block must land there rather than failing the tool call.
    """
    assert tenant_nonce_from_meta(meta) == ""


def test_an_unknown_schema_version_is_read_additively() -> None:
    """Same forward-compatibility rule as the caller block: a v2 gateway talking
    to a v1 backend must still get its nonce across."""
    assert (
        tenant_nonce_from_meta(
            {TENANT_META_KEY: {"schemaVersion": 99, "nonce": "n0nce", "future": 1}}
        )
        == "n0nce"
    )


def test_each_minted_nonce_is_distinct_and_unguessable() -> None:
    """Two connections must never collide, and one stub must not be able to
    predict a peer's namespace from its own."""
    minted = {new_tenant_nonce() for _ in range(100)}
    assert len(minted) == 100
    assert all(len(n) == _TENANT_NONCE_BYTES * 2 for n in minted)


def _raise_config_dir(*_a, **_k):
    """A data home that cannot be created, which is what ``mkdir`` reports."""
    raise NotADirectoryError(20, "Not a directory", "/nonexistent/under-a-file")


def _decline_rungs_1_to_3(monkeypatch):
    """Leave only rung 4 able to answer, and empty the memo in front of it."""
    import kiro_crew.member_memory_auth as member_auth

    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.delenv("KIROCREW_STUB_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    # Rung 1 is imported inside the ladder, so the seam is on ITS module.
    monkeypatch.setattr(member_auth, "protected_member_session_for_pid", lambda _pid: None)
    monkeypatch.setattr(kiro_crew.mcp_caller, "session_key_from_env_token", lambda: "")


def test_the_pid_rung_reports_a_broken_data_home_instead_of_raising(monkeypatch):
    """The rung owes the never-raises contract, so it holds the guard itself.

    ``config_dir()`` CREATES the data home, so an unusable ``KIROCREW_HOME`` --
    an ordinary misconfiguration, not an exotic one -- makes it raise. Attaching
    the guarantee to the rung rather than to a call site is what makes it true
    for every consumer, including one added later.
    """
    import kiro_crew.config.loader as loader

    _decline_rungs_1_to_3(monkeypatch)
    monkeypatch.setattr(loader, "config_dir", _raise_config_dir)

    identity = kiro_crew.mcp_caller._identity_from_pid_mapping()
    assert identity.failed is True
    assert identity.session_key == ""


def test_from_env_survives_a_broken_data_home(monkeypatch):
    """The direct rung-4 call must degrade, because the stub's caller cannot.

    ``from_env`` consults the ladder in two halves so the memo can sit between
    rung 3 and rung 4, which puts the second half outside the ladder function's
    own guard. The register payload this feeds is built uncaught, so a raise here
    takes the whole stub down -- losing every server in that session -- instead
    of reaching the deliberate degradation below it.
    """
    import kiro_crew.config.loader as loader

    _decline_rungs_1_to_3(monkeypatch)
    monkeypatch.setattr(loader, "config_dir", _raise_config_dir)

    ctx = CallerContext.from_env()
    assert ctx.session_key == ""
    assert ctx.from_gateway is False
    # And NOT memoised: the failure is a misconfiguration an operator can fix
    # while this process lives, so freezing it would outlast the cause.
    assert kiro_crew.mcp_caller._FROM_ENV_CACHE is None


def test_the_guarded_ladder_caller_is_unchanged_by_the_rungs_own_guard(monkeypatch):
    """Moving the guard down must not alter what the ladder already returned.

    ``resolve_own_identity`` wraps every rung, so a raise in rung 4 already
    surfaced as ``failed``. The rung now reports the same value itself, which is
    what makes this a strictly additive fix rather than a behaviour change for
    the three resolvers that reach rung 4 through the ladder.
    """
    import kiro_crew.config.loader as loader

    _decline_rungs_1_to_3(monkeypatch)
    monkeypatch.setattr(loader, "config_dir", _raise_config_dir)

    identity = kiro_crew.mcp_caller.resolve_own_identity()
    assert identity.failed is True
    assert identity.session_key == ""
