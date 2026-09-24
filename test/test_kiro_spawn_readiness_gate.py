"""Poll-driven kiro-cli spawn sites must not run the CLI while signed out.

``kiro-cli`` auto-launches an interactive browser login whenever a subcommand
runs unauthenticated (``--no-interactive`` does not suppress it and there is no
opt-out env var). ``/api/models`` is polled every 8s while the model list is
degraded and ``/api/sessions/usage`` every 30s, so an unauthenticated gateway
that spawns the CLI on every cycle opens dozens of browser windows and leaves
the dashboard unusable.

These tests pin the guard: both handlers consult the prerequisite readiness latch
BEFORE resolving or spawning the binary, and return the shared 503 instead.

The signed-out cases drive the REAL ``reject_if_kiro_unverified`` (never a
stubbed guard) and pin binary resolution to a fixed path, so a deleted or
relocated gate must reach ``create_subprocess_exec`` — on a CI runner with no
kiro-cli installed as much as on a developer machine that has one.

Ordinary sends are UNGATED — the ACP attempt reports auth failures itself and
they mutate nothing up front. These two sites (and the destructive reruns, which
rewrite persisted history before their turn) keep failing closed because neither
can use the ACP attempt as its authority.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_ready_kiro_prerequisite, _make_state

from kiro_crew.acp.client import AcpAuthRequired
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_KNOWN,
    backends_retired_by_host_logout,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import chat_persistence, chat_regenerate, chat_rewind, kiro_readiness
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.handlers import agents, sessions
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService
from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent
from kiro_crew.session import SessionManager

_RESOLVE_TARGET = "kiro_crew.acp.client._resolve_kiro_bin_for_spawn"
_FAKE_KIRO_BIN = "/usr/bin/kiro-cli"


@pytest.fixture(autouse=True)
def _reset_refusal_warning():
    """Clear the gate's warn-once flag around every test in this module.

    ``_refusal_warned_at`` is module state by necessity — the fail-closed path has no
    service object to hang it on — so without this reset every test after the first
    would see its refusal demoted to DEBUG, and the WARNING assertions would be
    passing or failing on test ORDER rather than on behaviour.
    """
    kiro_readiness._clear_refusal_warning()
    yield
    kiro_readiness._clear_refusal_warning()


class _SignedOutKiroPrerequisiteService(KiroPrerequisiteService):
    """Not-ready latch: the guard's ``isinstance`` check must still accept it."""

    async def session_ready(self) -> bool:
        return False

    # The gate authorizes on a fresh probe (`verified_ready`), never the bare
    # latch — a stale ready=True would otherwise green-light a signed-out spawn.
    async def verified_ready(self, *, max_age_secs: float) -> bool:
        del max_age_secs
        return False


def _make_signed_out_kiro_prerequisite() -> KiroPrerequisiteService:
    """Return a filesystem-free NOT-ready prerequisite service."""

    return object.__new__(_SignedOutKiroPrerequisiteService)


def _request(service: KiroPrerequisiteService) -> MagicMock:
    """A request whose app carries *service* as the prerequisite latch.

    ``reject_if_kiro_unverified`` reads ``app["kiro_prerequisite_service"]`` and
    falls back to ``app["state"].kiro_prerequisite_service``; both are wired so
    the real guard runs either way. ``state`` also carries the background-task
    set ``api_sessions_usage`` uses, so a removed gate reaches the scheduling
    line instead of dying on an unrelated AttributeError.
    """

    tasks: set[object] = set()
    app: dict[str, object] = {
        "kiro_prerequisite_service": service,
        "state": SimpleNamespace(
            kiro_prerequisite_service=service,
            _background_tasks=tasks,
        ),
    }
    request = MagicMock()
    request.app = app
    return request


@pytest.mark.asyncio
async def test_api_models_does_not_spawn_while_signed_out() -> None:
    request = _request(_make_signed_out_kiro_prerequisite())
    with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)) as resolve:
        with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            resp = await agents.api_models(request)

    # The gate must run BEFORE resolution, not merely before the spawn.
    resolve.assert_not_called()
    # ``create_task``-style call sites make the coroutine without awaiting it,
    # so only ``assert_not_called`` proves the spawn was never reached.
    spawn.assert_not_called()
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"


@pytest.mark.asyncio
async def test_api_sessions_usage_does_not_schedule_fetch_while_signed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the refresh branch live so a removed gate really schedules a fetch.
    monkeypatch.setattr(sessions, "_usage_cache_ts", 0.0)
    request = _request(_make_signed_out_kiro_prerequisite())
    with patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch:
        resp = await sessions.api_sessions_usage(request)

    # The handler schedules the fetch with ``asyncio.create_task``, so the
    # coroutine is CALLED but never AWAITED — ``assert_not_awaited`` would pass
    # even if the gate were moved below the scheduling line.
    fetch.assert_not_called()
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"


@pytest.mark.asyncio
async def test_api_models_still_reaches_spawn_path_when_ready() -> None:
    """The gate must be a pure add — a ready gateway keeps its existing behavior."""
    request = _request(_make_ready_kiro_prerequisite())
    with patch(_RESOLVE_TARGET, AsyncMock(return_value="")) as resolve:
        resp = await agents.api_models(request)

    # The handler got past the gate and into the pre-existing degraded branch:
    # binary unresolved, whose 503 body differs from the gate's.
    resolve.assert_awaited_once()
    assert resp.status == 503
    assert json.loads(resp.body) == {"error": "kiro binary not resolved"}


@pytest.mark.asyncio
async def test_refused_call_is_visible_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gate must SAY it refused.

    Every post-spawn failure branch in ``api_models`` logs a WARNING, so a reader
    who greps the log for that endpoint and finds nothing concludes the endpoint is
    healthy. A silent refusal inverts that conclusion, because an absent log line
    gets read as evidence.
    """
    request = _request(_make_signed_out_kiro_prerequisite())
    request.path = "/api/models"

    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.kiro_readiness"):
        with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)):
            resp = await agents.api_models(request)

    assert resp.status == 503
    refusals = [
        r
        for r in caplog.records
        if r.name == "kiro_crew.dashboard.kiro_readiness" and r.levelname == "WARNING"
    ]
    assert refusals, "the readiness gate refused a call without logging anything"
    message = refusals[0].getMessage()
    # The endpoint has to be IN the line: a grep for the path is how the reader
    # arrives, and a line that omits it does not answer the question they asked.
    assert "/api/models" in message
    assert "kiro_prerequisite_required" in message


@pytest.mark.asyncio
async def test_the_refusal_log_cannot_break_the_fail_closed_path() -> None:
    """The diagnostic must not add a failure mode to the branch it reports on.

    ``reject_if_kiro_unverified`` is the fail-CLOSED gate, so anything on that
    branch has to survive a caller that is only request-LIKE — which is what
    ``test_missing_route_prerequisite_wiring_fails_closed`` exercises. Reading
    ``.path`` directly raises for such a caller and converts a correct 503 into a
    500. A silent refusal is the defect this logging removes; an exception here is
    worse than the silence it replaced.
    """
    request = SimpleNamespace(app={"kiro_prerequisite_service": None})

    resp = await kiro_readiness.reject_if_kiro_unverified(request)  # type: ignore[arg-type]

    assert resp is not None
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"


@pytest.mark.asyncio
async def test_a_non_str_path_does_not_reach_the_formatter() -> None:
    """A request-like double whose ``path`` is not a string must still 503.

    Distinct from the case above: there the attribute is ABSENT, here it exists
    and is the wrong type, which a plain regex substitution would raise on. Both
    have to degrade, because both are on the fail-closed branch.
    """
    request = _request(_make_signed_out_kiro_prerequisite())  # MagicMock: .path is a mock

    with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)):
        resp = await agents.api_models(request)

    assert resp.status == 503
    assert kiro_readiness._log_safe_path(request) == "<unknown path>"


@pytest.mark.asyncio
async def test_a_newline_in_the_path_cannot_forge_a_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A decoded newline must not become a second ``gateway.log`` line.

    ``Request.path`` is URL-decoded, so ``%0A`` in a ``{slot}`` segment arrives as
    a real newline and the route pattern (which excludes only ``/{}``) matches it.
    Logged verbatim, a caller could append whatever line they liked to the log —
    forging the very evidence this logging exists to provide.

    Asserted on the RECORD THE GATE ACTUALLY EMITS, not on the helper in
    isolation: a helper-only test stays green if someone drops the sanitising
    call from the ``logger.warning`` below, which is the whole regression worth
    catching.
    """
    forged = "/api/chat/slots/a\nWARNING forged line/regenerate"
    request = _request(_make_signed_out_kiro_prerequisite())
    request.path = forged

    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.kiro_readiness"):
        with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)):
            resp = await agents.api_models(request)

    assert resp.status == 503
    records = [r for r in caplog.records if r.name == "kiro_crew.dashboard.kiro_readiness"]
    assert records, "the gate refused without logging anything"
    message = records[0].getMessage()
    # ``splitlines`` is the property that matters, not an absence of two literals:
    # it is what a Python consumer of the log actually re-splits on, and it covers
    # every boundary character at once.
    assert len(message.splitlines()) == 1
    # Neutralised, not discarded: a caller who sent a control byte is precisely
    # what the reader of the log wants to know about. ``repr`` spells LF with the
    # short escape, so this is ``\\n`` rather than ``\\x0a``.
    assert "\\n" in message
    assert "forged line" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoded", "escaped"),
    [("\x85", "\\x85"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")],
    ids=["U+0085-NEL", "U+2028-LS", "U+2029-PS"],
)
async def test_unicode_line_separators_cannot_forge_a_log_line(
    caplog: pytest.LogCaptureFixture,
    encoded: str,
    escaped: str,
) -> None:
    """C0 is not the whole boundary set — ``splitlines()`` is wider than C0.

    ``%C2%85`` / ``%E2%80%A8`` / ``%E2%80%A9`` decode to U+0085 / U+2028 / U+2029.
    None of them is a ``0a`` byte, so none forges a physical line in the log FILE;
    all three ARE ``str.splitlines()`` boundaries, so a Python consumer that
    re-splits the log does see a forged line. A guard scoped to C0+DEL let every
    one of them through.
    """
    request = _request(_make_signed_out_kiro_prerequisite())
    request.path = f"/api/chat/slots/a{encoded}WARNING forged/regenerate"

    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.kiro_readiness"):
        with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)):
            resp = await agents.api_models(request)

    assert resp.status == 503
    records = [r for r in caplog.records if r.name == "kiro_crew.dashboard.kiro_readiness"]
    assert records, "the gate refused without logging anything"
    message = records[0].getMessage()
    assert len(message.splitlines()) == 1
    assert escaped in message


def test_every_control_byte_is_neutralised_not_just_crlf() -> None:
    """The whole C0+DEL class, not the two bytes that happen to split a line.

    An ESC byte reaching a terminal that is tailing the log is the same defect
    wearing different clothes.
    """
    rendered = kiro_readiness._log_safe_path(SimpleNamespace(path="/api/a\x1b[2Kb\x00c\x7fd"))

    assert rendered == "'/api/a\\x1b[2Kb\\x00c\\x7fd'"


def test_invisible_formatting_characters_are_neutralised() -> None:
    """``Cf`` splits no line and still forges evidence.

    U+202E RIGHT-TO-LEFT OVERRIDE reorders what a human READS in a rendered log
    view, and U+200B hides a boundary that is not there. Neither is a
    ``splitlines()`` boundary, so a guard built only from line-breaking characters
    would miss both — which is why the class is the ``Cf`` CATEGORY rather than a
    list of the characters someone happened to think of.
    """
    rendered = kiro_readiness._log_safe_path(SimpleNamespace(path="/api/a\u202eb\u200bc"))

    assert rendered == "'/api/a\\u202eb\\u200bc'"


def test_categories_a_hand_written_set_would_miss() -> None:
    """The reason this delegates to ``repr`` instead of listing categories.

    A set of ``Cc``/``Cf``/``Zl``/``Zp`` — the categories that come to mind — lets all
    of these through, and every one is reachable through ``yarl``'s percent-decoding:
    ``%C2%A0`` and ``%E3%80%80`` are ``Zs``, ``%EE%80%80`` is ``Co``, ``%CD%B8`` is
    ``Cn``. ``str.isprintable()`` rejects the lot, so ``repr`` covers them without
    anyone having to remember to extend a list.
    """
    rendered = kiro_readiness._log_safe_path(
        SimpleNamespace(path="/api/a\u00a0b\u3000c\ue000d\u0378e")
    )

    assert rendered == "'/api/a\\xa0b\\u3000c\\ue000d\\u0378e'"


def test_a_lone_surrogate_still_produces_an_encodable_line() -> None:
    """Robustness, not a live vector: ``yarl`` does not decode ``%ED%A0%80``.

    Worth pinning anyway because of the failure mode it guards. A surrogate emitted
    verbatim is a string a UTF-8 log handler cannot encode, and ``logging`` swallows
    handler errors, so the line vanishes silently. A function whose whole purpose is
    "the log must not lie about what happened" must not be able to delete the
    record.
    """
    rendered = kiro_readiness._log_safe_path(SimpleNamespace(path="/api/a\ud800b"))

    assert rendered.encode("utf-8")
    assert "\\ud800" in rendered


def test_visible_non_ascii_is_left_alone() -> None:
    """Over-escaping guard: the goal is an unforgeable path, not an ASCII one.

    Folding every non-ASCII byte would be the easy way to satisfy the finding and
    would make a legitimate path unreadable for exactly the operators who most need
    to read it.
    """
    rendered = kiro_readiness._log_safe_path(SimpleNamespace(path="/api/文档/café"))

    assert rendered == "'/api/文档/café'"


@pytest.mark.asyncio
async def test_a_polled_outage_warns_once_not_once_per_request() -> None:
    """The visible line must not become the thing that destroys the log.

    A signed-out gateway with an open dashboard refuses ``/api/models`` every 8s and
    ``/api/sessions/usage`` every 30s, and BOTH refuse at this gate — the sibling
    branches that log in ``api_models`` sit below it and are never reached in that
    state. Left per-request that is ~570 lines/hour into a ``deque(maxlen=1000)``,
    which churns the whole ring every ~1.8 hours and evicts the diagnostics an
    operator opened the log to read.
    """
    request = _request(_make_signed_out_kiro_prerequisite())
    request.path = "/api/models"

    with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)):
        with patch.object(kiro_readiness.logger, "warning") as warn:
            with patch.object(kiro_readiness.logger, "debug") as dbg:
                for _ in range(25):
                    resp = await agents.api_models(request)

    assert resp.status == 503
    # Still visible: silence was the original defect and must not come back.
    assert warn.call_count == 1
    # And accounted for, not dropped — a reader who turns up DEBUG sees the rest.
    assert dbg.call_count == 24


@pytest.mark.asyncio
async def test_a_later_outage_warns_again_after_recovery() -> None:
    """One WARNING per OUTAGE, not one per process lifetime.

    Without the clear-on-authorize half, the first outage after boot consumes the
    only WARNING the process ever emits and every later outage is silent — the
    original defect back in a subtler form. This is why
    ``mcp_discovery._clear_unresolvable`` exists next to its warn-once.
    """
    signed_out = _request(_make_signed_out_kiro_prerequisite())
    signed_out.path = "/api/models"
    ready = _request(_make_ready_kiro_prerequisite())
    ready.path = "/api/models"

    with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)):
        with patch.object(kiro_readiness.logger, "warning") as warn:
            await agents.api_models(signed_out)  # outage 1 -> WARNING
            await agents.api_models(signed_out)  # same outage -> DEBUG
            assert warn.call_count == 1

            await kiro_readiness.reject_if_kiro_unverified(ready)  # recovered
            await agents.api_models(signed_out)  # outage 2 -> WARNING again

    assert warn.call_count == 2


@pytest.mark.asyncio
async def test_an_unobserved_recovery_does_not_silence_the_next_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clear-on-authorize half is not sufficient on its own.

    Nothing calls the authorized branch on a gateway whose dashboard is closed: the
    pollers have stopped, so a recovery that happens in that window is never OBSERVED
    and the flag stays set. Without a floor the next outage logs only DEBUG — the
    subtler silence the docstring names and the whole point of the mechanism. So the
    guarantee cannot depend on seeing the recovery: after
    ``_REFUSAL_REWARN_SECS`` an ongoing or fresh refusal speaks up regardless.
    """
    fake_now = [1000.0]
    monkeypatch.setattr(kiro_readiness, "_clock", lambda: fake_now[0])
    request = _request(_make_signed_out_kiro_prerequisite())
    request.path = "/api/models"

    with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)):
        with patch.object(kiro_readiness.logger, "warning") as warn:
            await agents.api_models(request)  # outage 1 -> WARNING
            assert warn.call_count == 1

            # Still inside the floor: quiet, so the ring is not churned.
            fake_now[0] += kiro_readiness._REFUSAL_REWARN_SECS - 1
            await agents.api_models(request)
            assert warn.call_count == 1

            # Floor elapsed, and NO authorized call ever happened in between.
            fake_now[0] += 2
            await agents.api_models(request)

    assert warn.call_count == 2


@pytest.mark.asyncio
async def test_a_ready_gateway_logs_no_refusal() -> None:
    """No line on the authorized path: the log must stay a signal, not a heartbeat."""
    request = _request(_make_ready_kiro_prerequisite())
    request.path = "/api/models"

    with patch(_RESOLVE_TARGET, AsyncMock(return_value="")):
        with patch.object(kiro_readiness.logger, "warning") as warn:
            await agents.api_models(request)

    warn.assert_not_called()


# ── The latch governs only a harness that signs in through kiro-cli ──────────
#
# ``reject_if_kiro_unverified`` describes a kiro-cli sign-in. On a deployment
# whose ``agent.acp_backend`` is claude-agent-acp or codex-acp that latch says
# nothing about the sessions, and refusing on it locked regenerate, rewind,
# ``/v1/chat/completions``, ``/api/models`` and ``/api/sessions/usage`` behind
# a kiro-cli sign-in the operator had deliberately stopped needing. The gate
# now reads the selected backend as POSITIVE membership in
# ``backends_retired_by_host_logout()`` (harness-parity H5/H6); the two kiro-cli
# spawn sites test that membership themselves so a foreign harness never has the
# browser-opening binary resolved on its behalf either.


def _config_selecting(backend: str, *, member_backend: str | None = None):
    """Patch the config read the gate performs so it reports *backend*.

    *member_backend* sets ``agent.member_acp_backend`` -- the field a ``member-*``
    session is routed on -- for the tests that need it to DIFFER from the default.
    Left ``None`` the attribute is absent, which is what every non-member test had
    before the field existed here.
    """

    agent = SimpleNamespace(acp_backend=backend, model="")
    if member_backend is not None:
        agent.member_acp_backend = member_backend
    cfg = SimpleNamespace(agent=agent)
    return patch.object(KiroCrewConfig, "load", classmethod(lambda cls: cfg))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
async def test_gate_applies_exactly_to_the_kiro_identity_store_members(backend: str) -> None:
    """Membership, not identity: every known backend is answered from the set."""
    request = _request(_make_signed_out_kiro_prerequisite())
    with _config_selecting(backend):
        resp = await kiro_readiness.reject_if_kiro_unverified(request)

    if backend in backends_retired_by_host_logout():
        assert resp is not None and resp.status == 503
        assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"
    else:
        assert resp is None


@pytest.mark.asyncio
async def test_a_foreign_backend_never_consults_the_prerequisite_service() -> None:
    """Even an ABSENT service (which fails closed for kiro) is not asked."""
    request = MagicMock()
    request.app = {}
    with _config_selecting(ACP_BACKEND_CLAUDE):
        assert await kiro_readiness.reject_if_kiro_unverified(request) is None


@pytest.mark.asyncio
async def test_an_unreadable_config_keeps_the_gate(caplog: pytest.LogCaptureFixture) -> None:
    """Fail closed toward the latch: a broken config must not un-gate the spawn."""
    from kiro_crew.config import KiroCrewConfig

    def _boom(cls):
        raise OSError("config.json unreadable")

    request = _request(_make_signed_out_kiro_prerequisite())
    with patch.object(KiroCrewConfig, "load", classmethod(_boom)):
        with caplog.at_level("WARNING", logger=kiro_readiness.logger.name):
            resp = await kiro_readiness.reject_if_kiro_unverified(request)

    assert resp is not None and resp.status == 503
    assert any("agent.acp_backend" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_api_models_refuses_a_foreign_backend_before_resolving_kiro_cli() -> None:
    """A selectable harness with no model list of its own (opencode, at the time of
    writing) is refused before kiro-cli is resolved: the kiro-cli list is not its
    answer, and claude and codex each read their adapter's advertised list
    instead."""
    request = _request(_make_signed_out_kiro_prerequisite())
    with _config_selecting(ACP_BACKEND_OPENCODE):
        with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)) as resolve:
            with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
                resp = await agents.api_models(request)

    resolve.assert_not_called()
    spawn.assert_not_called()
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "model_list_backend_unsupported"


@pytest.mark.asyncio
async def test_api_sessions_usage_hides_the_pill_on_a_foreign_backend_without_a_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed-out kiro-cli left on a Claude Code host must not be scraped."""
    monkeypatch.setattr(sessions, "_usage_cache", {"credits_plan": 10.0})
    monkeypatch.setattr(sessions, "_usage_cache_ts", 0.0)
    request = _request(_make_signed_out_kiro_prerequisite())
    with _config_selecting(ACP_BACKEND_CLAUDE):
        with patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch:
            resp = await sessions.api_sessions_usage(request)

    fetch.assert_not_called()
    assert resp.status == 200
    assert json.loads(resp.body) == {"usage": {"available": False}}


@pytest.mark.asyncio
async def test_switching_back_to_kiro_refreshes_usage_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The foreign-backend marker must not start a refresh interval.

    ``_publish_usage`` stamps the cache with now; a switch back to a kiro
    identity-store backend inside ``_USAGE_REFRESH_SECS`` would then find a
    fresh-looking cache and skip the fetch, hiding the pill for up to ten minutes
    on a harness that does have a plan to show.
    """
    monkeypatch.setattr(sessions, "_usage_cache", {"credits_plan": 10.0})
    monkeypatch.setattr(sessions, "_usage_cache_ts", 0.0)
    with patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch:
        with _config_selecting(ACP_BACKEND_CLAUDE):
            await sessions.api_sessions_usage(_request(_make_signed_out_kiro_prerequisite()))
        fetch.assert_not_called()
        assert sessions._usage_cache == {"available": False}

        with _config_selecting(ACP_BACKEND_KIRO):
            resp = await sessions.api_sessions_usage(_request(_make_ready_kiro_prerequisite()))

    fetch.assert_called_once()
    assert resp.status == 200


@pytest.mark.asyncio
async def test_foreign_backend_clears_an_existing_unavailable_refresh_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idempotent unavailable marker still invalidates a stale refresh stamp."""
    monkeypatch.setattr(sessions, "_usage_cache", {"available": False})
    monkeypatch.setattr(sessions, "_usage_cache_ts", 123.0)
    request = _request(_make_signed_out_kiro_prerequisite())

    with _config_selecting(ACP_BACKEND_CLAUDE):
        with patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch:
            response = await sessions.api_sessions_usage(request)

    fetch.assert_not_called()
    assert response.status == 200
    assert sessions._usage_cache_ts == 0.0


@pytest.mark.asyncio
async def test_an_explicit_backend_snapshot_is_used_instead_of_a_config_read() -> None:
    """A caller's snapshot is authoritative: the gate must not read config again."""
    from kiro_crew.config import KiroCrewConfig

    def _boom(cls):
        raise AssertionError("the gate re-read config despite being handed a backend")

    request = _request(_make_signed_out_kiro_prerequisite())
    with patch.object(KiroCrewConfig, "load", classmethod(_boom)):
        assert (
            await kiro_readiness.reject_if_kiro_unverified(request, backend=ACP_BACKEND_CLAUDE)
            is None
        )
        resp = await kiro_readiness.reject_if_kiro_unverified(request, backend=ACP_BACKEND_KIRO)
    assert resp is not None and resp.status == 503


@pytest.mark.asyncio
async def test_api_models_gates_on_its_own_snapshot_not_a_second_read() -> None:
    """A PATCH landing between the handler's read and the gate's must not admit a spawn.

    The handler read kiro; by the time the gate would read again the default has
    flipped to a foreign backend. With two reads the gate stands aside and the
    kiro-cli branch spawns unauthenticated once. With one snapshot it refuses.
    """
    request = _request(_make_signed_out_kiro_prerequisite())
    with _config_selecting(ACP_BACKEND_KIRO):
        with patch.object(
            kiro_readiness, "selected_backend", AsyncMock(return_value=ACP_BACKEND_CLAUDE)
        ):
            with patch(_RESOLVE_TARGET, AsyncMock(return_value=_FAKE_KIRO_BIN)) as resolve:
                resp = await agents.api_models(request)

    resolve.assert_not_called()
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"


def test_live_session_verdict_is_the_providers_own_declaration_and_nothing_else() -> None:
    live = SimpleNamespace(
        sessions=SimpleNamespace(
            _sessions={
                "k": SimpleNamespace(provider=SimpleNamespace(uses_kiro_identity_store=True)),
                "f": SimpleNamespace(provider=SimpleNamespace(uses_kiro_identity_store=False)),
            }
        )
    )
    assert kiro_readiness.live_session_signs_in_via_kiro_cli(live, "k") is True
    assert kiro_readiness.live_session_signs_in_via_kiro_cli(live, "f") is False
    assert kiro_readiness.live_session_signs_in_via_kiro_cli(live, "other") is None
    assert (
        kiro_readiness.live_session_signs_in_via_kiro_cli(
            SimpleNamespace(sessions=MagicMock()), "k"
        )
        is None
    )
    undeclared = SimpleNamespace(
        sessions=SimpleNamespace(_sessions={"k": SimpleNamespace(provider=SimpleNamespace())})
    )
    assert kiro_readiness.live_session_signs_in_via_kiro_cli(undeclared, "k") is None


# ── A member thread is judged by the backend it is ROUTED to ─────────────────
#
# ``members.select_provider_backend`` -- the one per-session selection gate the
# provider factory calls -- routes every ``member-*`` session onto
# ``agent.member_acp_backend`` (default kas) and everything else onto
# ``agent.acp_backend``. ``selected_backend(session_key)`` goes through that same
# helper, and every destructive rerun resolves its verdict with it --
# ``backend_signs_in_via_kiro_cli(await selected_backend(key))`` -- before
# handing that bool to the gate (the gate's own fallback read, with no verdict
# and no backend, is the gateway default). Reading ``agent.acp_backend`` for a
# member thread judged it by a backend it never runs on, wrong in both
# directions: a kiro default held a Claude-routed member rerun behind a kiro-cli
# sign-in it never needs, and a Claude default let a kas-routed member rerun
# rewrite its history on a signed-out kiro-cli.

_MEMBER_KEY = "dashboard:member-atlas"
_PLAIN_KEY = "dashboard:chat-1"


async def _gate_for_session(request: MagicMock, session_key: str) -> web.Response | None:
    """Run the gate exactly as a destructive rerun does for *session_key*.

    The handler resolves the verdict itself -- the same bool also decides its
    rollback branch -- and the gate only consumes it.
    """

    backend = await kiro_readiness.selected_backend(session_key)
    verdict = kiro_readiness.backend_signs_in_via_kiro_cli(backend)
    return await kiro_readiness.reject_if_kiro_unverified(request, signs_in_via_kiro_cli=verdict)


@pytest.mark.asyncio
async def test_a_member_thread_on_a_foreign_backend_is_not_held_behind_kiro_cli() -> None:
    """Default kiro, members on Claude: the member rerun passes, the plain slot is refused."""
    request = _request(_make_signed_out_kiro_prerequisite())
    with _config_selecting(ACP_BACKEND_KIRO, member_backend=ACP_BACKEND_CLAUDE):
        assert await kiro_readiness.selected_backend(_MEMBER_KEY) == ACP_BACKEND_CLAUDE
        assert await kiro_readiness.selected_backend(_PLAIN_KEY) == ACP_BACKEND_KIRO
        assert await _gate_for_session(request, _MEMBER_KEY) is None
        resp = await _gate_for_session(request, _PLAIN_KEY)
    assert resp is not None and resp.status == 503


@pytest.mark.asyncio
async def test_a_member_thread_on_kas_is_gated_even_when_the_default_is_foreign() -> None:
    """Default Claude, members on kas (the shipped default): the member rerun rebuilds
    on a kiro-identity-store harness, so a signed-out kiro-cli must refuse it -- while
    the plain slot, which really does run on Claude, passes."""
    request = _request(_make_signed_out_kiro_prerequisite())
    with _config_selecting(ACP_BACKEND_CLAUDE, member_backend=ACP_BACKEND_KAS):
        assert await kiro_readiness.selected_backend(_MEMBER_KEY) == ACP_BACKEND_KAS
        assert kiro_readiness.backend_signs_in_via_kiro_cli(ACP_BACKEND_KAS) is True
        resp = await _gate_for_session(request, _MEMBER_KEY)
        assert resp is not None and resp.status == 503
        assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"
        assert await _gate_for_session(request, _PLAIN_KEY) is None


@pytest.mark.asyncio
async def test_an_unselectable_member_backend_degrades_to_kiro_and_is_gated() -> None:
    """The member arm resolves through ``resolve_selected_backend`` exactly as the
    factory does: a value this build cannot serve degrades to kiro, and the gate
    must judge THAT, not the raw field."""
    request = _request(_make_signed_out_kiro_prerequisite())
    with _config_selecting(ACP_BACKEND_CLAUDE, member_backend="not-a-backend"):
        assert await kiro_readiness.selected_backend(_MEMBER_KEY) == ACP_BACKEND_KIRO
        resp = await _gate_for_session(request, _MEMBER_KEY)
    assert resp is not None and resp.status == 503


@pytest.mark.asyncio
async def test_without_a_session_key_the_member_field_is_never_consulted() -> None:
    """The poll-driven sites act for the gateway, not for one session."""
    request = _request(_make_signed_out_kiro_prerequisite())
    with _config_selecting(ACP_BACKEND_CLAUDE, member_backend=ACP_BACKEND_KAS):
        assert await kiro_readiness.selected_backend() == ACP_BACKEND_CLAUDE
        assert kiro_readiness.backend_signs_in_via_kiro_cli(ACP_BACKEND_CLAUDE) is False
        # The gate's own fallback read is that same keyless default.
        assert await kiro_readiness.reject_if_kiro_unverified(request) is None


def _member_slot_request(tmp_path, monkeypatch, route: str):
    """A real ``DashboardState`` with a signed-out latch and a member-mode slot,
    plus a request for *route* on that slot.

    ``mode="member"`` is the one way the slot registry admits a ``member-`` key
    (``handlers/members.py`` is the only production caller), so this is the
    shape a member DM thread actually has when a rerun reaches these handlers.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.kiro_prerequisite_service = _make_signed_out_kiro_prerequisite()
    state.sessions._session_map.get = MagicMock(return_value="")
    slot = state.get_or_create_slot("member-atlas", mode="member")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    app = web.Application()
    app["state"] = state
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/member-atlas/{route}",
        match_info={"slot": "member-atlas"},
        app=app,
    )

    async def _json():
        return {"index": 0, "content": "edited"}

    request.json = _json  # type: ignore[method-assign]
    return state, slot, request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "handler", "route"),
    [
        (chat_regenerate, chat_regenerate.api_chat_slot_edit_resend, "edit-resend"),
        (chat_rewind, chat_rewind.api_chat_slot_rewind, "rewind"),
    ],
    ids=["edit-resend", "rewind"],
)
async def test_destructive_reruns_judge_a_member_slot_by_its_own_backend(
    tmp_path, monkeypatch, module, handler, route: str
) -> None:
    """Both routes discard and REBUILD the session, so the backend that matters is
    the one the factory will build ``dashboard:member-<slug>`` on. Driven through
    the real handler and the real gate: a mocked gate passes whatever key the
    handler forgot to send.
    """
    # Default kiro, members on Claude: a signed-out kiro-cli must not hold the rerun.
    _state, slot, request = _member_slot_request(tmp_path / "pass", monkeypatch, route)
    with _config_selecting(ACP_BACKEND_KIRO, member_backend=ACP_BACKEND_CLAUDE):
        with patch.object(module, "_run_chat", AsyncMock()) as run:
            resp = await handler(request)
            # The turn is dispatched from a task that resolves ``_run_chat`` when
            # it runs; yield so it lands on THIS mock, not the next block's.
            await asyncio.sleep(0)
    assert resp.status == 200, resp.body
    assert [m["content"] for m in slot.messages] == ["edited"]
    assert run.await_args.args[2] == "edited"

    # Default Claude, members on kas: the rebuild lands on a kiro-identity-store
    # harness, so the same signed-out kiro-cli must refuse BEFORE the truncation.
    state, slot, request = _member_slot_request(tmp_path / "refuse", monkeypatch, route)
    original = list(slot.messages)
    with _config_selecting(ACP_BACKEND_CLAUDE, member_backend=ACP_BACKEND_KAS):
        with patch.object(module, "_run_chat", AsyncMock()) as run:
            resp = await handler(request)
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "kiro_prerequisite_required"
    assert slot.messages == original
    run.assert_not_called()
    state.sessions.discard_conversation.assert_not_awaited()


# ── The live-session peek is checked against the REAL session manager ────────
#
# ``live_session_signs_in_via_kiro_cli`` reads
# ``state.sessions._sessions[key].provider.uses_kiro_identity_store`` and answers
# ``None`` for any shape it does not recognise, at which point the gate falls
# back to the configured default. Every other test here hands it a
# ``SimpleNamespace`` built to that shape, so a rename of ``_sessions`` or of the
# provider's declaration would leave them green while the peek silently answered
# ``None`` in production. This one drives a real ``SessionManager`` and sets the
# configured default to the OPPOSITE of the live session on both sides of a
# switch, so the fallback is the wrong answer and drift goes red.


def _provider_on(backend: str) -> AsyncMock:
    """A provider double that DECLARES its identity store from *backend*, the way
    ``AcpProvider.uses_kiro_identity_store`` does, and satisfies the manager's
    allocation checks (alive, idle, no context)."""
    m = AsyncMock()
    m.start = AsyncMock()
    m.shutdown = AsyncMock()
    m.memory_mode = "persistent"
    m.is_process_alive = lambda: True
    m.is_alive = lambda: True
    m.context_usage_pct = lambda: 0.0
    m.context_window_tokens = lambda: 0
    m.has_active_turn = lambda: False
    m.runtime_info = lambda: (None, None)
    m.uses_kiro_identity_store = backend in backends_retired_by_host_logout()
    return m


@pytest.mark.asyncio
async def test_the_live_verdict_follows_a_backend_switch_on_a_real_session_manager() -> None:
    built_on = [ACP_BACKEND_KIRO]

    def _factory(session_key=None, agent=None, channel_id=None, **kwargs):
        return _provider_on(built_on[0])

    manager = SessionManager(KiroCrewConfig(), provider_factory=_factory)
    state = SimpleNamespace(sessions=manager)
    request = _request(_make_signed_out_kiro_prerequisite())
    try:
        await manager.get_or_create(_PLAIN_KEY)
        manager.release(_PLAIN_KEY)

        # Live on kiro while the default has been switched to Claude. A live
        # session keeps the backend it started on (regenerate's case), so the
        # gate must still refuse; a peek that had drifted to ``None`` would fall
        # back to the Claude default and let the rewrite through.
        with _config_selecting(ACP_BACKEND_CLAUDE):
            verdict = kiro_readiness.live_session_signs_in_via_kiro_cli(state, _PLAIN_KEY)
            assert verdict is True
            resp = await kiro_readiness.reject_if_kiro_unverified(
                request, signs_in_via_kiro_cli=verdict
            )
        assert resp is not None and resp.status == 503

        # The session is rebuilt on Claude (the discard-and-rebuild that
        # edit-resend and rewind perform) while the default is switched back to
        # kiro. Now the live verdict must un-gate; a ``None`` fallback would
        # refuse on the kiro default.
        await manager.destroy(_PLAIN_KEY)
        built_on[0] = ACP_BACKEND_CLAUDE
        await manager.get_or_create(_PLAIN_KEY)
        manager.release(_PLAIN_KEY)

        with _config_selecting(ACP_BACKEND_KIRO):
            verdict = kiro_readiness.live_session_signs_in_via_kiro_cli(state, _PLAIN_KEY)
            assert verdict is False
            assert (
                await kiro_readiness.reject_if_kiro_unverified(
                    request, signs_in_via_kiro_cli=verdict
                )
                is None
            )
    finally:
        await manager.close_all()


# ── A foreign harness auth failure rolls back destructive reruns ─────────────


def _install_foreign_auth_failure_turn(
    state, *, partial_text: str = "", tool_call_before_failure: bool = False
) -> None:
    """Make the real runner reach ``AcpAuthRequired`` on a Claude session.

    ``tool_call_before_failure`` follows the partial text with a tool call, so the
    runner finalizes that text as a flushed segment before the failure lands.
    """
    client = MagicMock()

    async def _raise_auth(_message):
        if partial_text:
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=partial_text)
        if tool_call_before_failure:
            yield LLMEvent(
                kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="fs_read", tool_name="fs_read"
            )
        raise AcpAuthRequired("Claude Code is not signed in.", backend=ACP_BACKEND_CLAUDE)

    client.stream = _raise_auth
    client.stream_command = _raise_auth
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    state.sessions.aflush = AsyncMock()
    state.sessions._session_map.get = MagicMock(return_value="")
    state.is_yolo_active = MagicMock(return_value=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "handler", "route", "body"),
    [
        (
            chat_rewind,
            chat_rewind.api_chat_slot_rewind,
            "rewind",
            {"at_message_index": 0, "content": "edited"},
        ),
        (
            chat_regenerate,
            chat_regenerate.api_chat_slot_edit_resend,
            "edit-resend",
            {"index": 0, "content": "edited"},
        ),
        (
            chat_regenerate,
            chat_regenerate.api_chat_slot_regenerate,
            "regenerate",
            {},
        ),
    ],
    ids=["rewind", "edit-resend", "regenerate"],
)
async def test_foreign_auth_failure_preserves_destructive_rerun_history(
    tmp_path, monkeypatch, module, handler, route: str, body: dict
) -> None:
    """A foreign harness owns its auth verdict, but a failed verdict must not erase history."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    _install_foreign_auth_failure_turn(state)
    slot = state.get_or_create_slot("foreign-auth")
    slot.title = "Foreign auth history"
    slot._titled = True
    slot.append("user", "first question", ts="t1")
    slot.append("assistant", "first answer", ts="t2")
    slot.append("user", "second question", ts="t3")
    slot.append("assistant", "second answer", ts="t4")
    slot.drain()
    original = [(row["role"], row["content"]) for row in slot.messages]
    await asyncio.to_thread(state.flush_slot_now, slot)

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
    cfg.agent.member_acp_backend = ACP_BACKEND_CLAUDE
    app = web.Application()
    app["state"] = state
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/foreign-auth/{route}",
        match_info={"slot": "foreign-auth"},
        app=app,
    )
    request["app"] = ""

    async def _json():
        return body

    request.json = _json  # type: ignore[method-assign]
    # Rewind discards the backing queue with the suffix and the rollback
    # re-inserts it, so a prompt held before the rewind must reach the other
    # open clients again -- the inverse of the ``queue_cancel`` the discard sent.
    held_id = slot.queue_append("held before rewind") if route == "rewind" else ""
    rollback_started = asyncio.Event()
    release_rollback = asyncio.Event()
    real_restore = module.restore_destructive_history_after_auth_failure

    async def _gated_restore(*args, **kwargs):
        rollback_started.set()
        await release_rollback.wait()
        return await real_restore(*args, **kwargs)

    with (
        patch.object(KiroCrewConfig, "load", classmethod(lambda cls: cfg)),
        patch.object(module, "_run_chat", _run_chat),
        patch.object(module, "restore_destructive_history_after_auth_failure", _gated_restore),
    ):
        response = await handler(request)
        assert response.status == 200, response.body
        turn_task = slot.task
        assert turn_task is not None
        successor_id = ""
        try:
            await asyncio.wait_for(rollback_started.wait(), timeout=5)
            assert slot.task is turn_task
            assert slot.running is True
            successor_id = slot.queue_append("successor during auth rollback")
            assert all(
                row.get("content") != "successor during auth rollback" for row in slot.messages
            )
        finally:
            release_rollback.set()
            await asyncio.wait_for(asyncio.shield(turn_task), timeout=5)

    assert slot.task is None
    assert successor_id in [entry["id"] for entry in slot._queue]
    if route == "rewind":
        assert held_id in [entry["id"] for entry in slot._queue]
        queue_frames = [
            (kind, payload)
            for (kind, payload), _kw in (
                (call.args, call.kwargs) for call in state.broadcast_ws.call_args_list
            )
            if kind in ("queue_cancel", "queue_push") and payload.get("queue_id") == held_id
        ]
        kinds = [kind for kind, _payload in queue_frames]
        assert kinds == ["queue_cancel", "queue_push"], kinds
        pushed = queue_frames[-1][1]
        assert pushed["slot"] == slot.key
        assert pushed["content"] == "held before rewind"
        assert pushed["ts"]

    live = [(row["role"], row["content"]) for row in slot.messages]
    persisted = [
        (row["role"], row["content"])
        for row in state.conversation_log.read_messages("dashboard:foreign-auth")
    ]
    assert live[: len(original)] == original
    assert persisted[: len(original)] == original
    assert ("user", "edited") not in live
    assert ("user", "edited") not in persisted
    assert any(role == "error" and "not signed in" in content for role, content in live)
    assert any(role == "error" and "not signed in" in content for role, content in persisted)


@pytest.mark.asyncio
async def test_auth_rollback_drops_partial_reply_and_retired_stateless_card(
    tmp_path, monkeypatch
) -> None:
    """A failed replacement keeps its error and foreign appends, not its partial reply."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    _install_foreign_auth_failure_turn(state, partial_text="orphaned partial reply")
    name = "foreign-auth-partial"
    history_key = f"dashboard:{name}"
    slot = state.get_or_create_slot(name)
    slot.title = "Foreign partial auth history"
    slot._titled = True
    slot.append("user", "first question")
    slot.append("assistant", "first answer")
    slot.append("user", "second question")
    slot.append("assistant", "second answer")
    slot.drain()
    original = [(row["role"], row["content"]) for row in slot.messages]
    slot._question_pending = {"stateless-card": {"blocking": False}}
    await asyncio.to_thread(state.flush_slot_now, slot)

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
    cfg.agent.member_acp_backend = ACP_BACKEND_CLAUDE
    app = web.Application()
    app["state"] = state
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{name}/edit-resend",
        match_info={"slot": name},
        app=app,
    )
    request["app"] = ""

    async def _json():
        return {"index": 0, "content": "edited"}

    request.json = _json  # type: ignore[method-assign]
    real_restore = chat_persistence.restore_destructive_history_after_auth_failure

    async def _dirty_flush_then_restore(*args, **kwargs):
        assert await chat_persistence.save_slot_off_loop(state, slot, best_effort=False)
        await asyncio.to_thread(
            partial(
                state.conversation_log.append,
                history_key,
                "assistant",
                "concurrent channel reply",
                mid="foreign-channel-row",
            )
        )
        return await real_restore(*args, **kwargs)

    with (
        patch.object(KiroCrewConfig, "load", classmethod(lambda cls: cfg)),
        patch.object(chat_regenerate, "_run_chat", _run_chat),
        patch.object(
            chat_regenerate,
            "restore_destructive_history_after_auth_failure",
            _dirty_flush_then_restore,
        ),
    ):
        response = await chat_regenerate.api_chat_slot_edit_resend(request)
        assert response.status == 200, response.body
        turn_task = slot.task
        assert turn_task is not None
        await asyncio.wait_for(asyncio.shield(turn_task), timeout=5)

    live = [(row["role"], row["content"]) for row in slot.messages]
    persisted = [
        (row["role"], row["content"]) for row in state.conversation_log.read_messages(history_key)
    ]
    assert live[: len(original)] == original
    assert ("assistant", "orphaned partial reply") not in live
    assert ("assistant", "orphaned partial reply") not in persisted
    assert ("user", "edited") not in live
    assert ("user", "edited") not in persisted
    assert any(role == "error" and "not signed in" in text for role, text in live)
    assert any(role == "error" and "not signed in" in text for role, text in persisted)
    assert ("assistant", "concurrent channel reply") in persisted
    assert "stateless-card" not in slot._question_pending


@pytest.mark.asyncio
async def test_auth_rollback_drops_segment_flushed_before_the_tool_call(
    tmp_path, monkeypatch
) -> None:
    """Text a tool boundary already finalized is the failed turn's output too.

    ``_flush_segment`` appends the pre-tool text as a finished assistant row and
    resets the partial buffer, so the abnormal-end persist has nothing left to
    stamp on its own. The rollback partitions by the ``interruptedTurn`` stamp;
    an unstamped segment reads as a foreign append and is re-appended after the
    restored history, so the failed turn's output survives the rollback.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    _install_foreign_auth_failure_turn(
        state, partial_text="half an answer", tool_call_before_failure=True
    )
    name = "foreign-auth-segment"
    history_key = f"dashboard:{name}"
    slot = state.get_or_create_slot(name)
    slot.title = "Foreign segment auth history"
    slot._titled = True
    slot.append("user", "first question")
    slot.append("assistant", "first answer")
    slot.append("user", "second question")
    slot.append("assistant", "second answer")
    slot.drain()
    original = [(row["role"], row["content"]) for row in slot.messages]
    await asyncio.to_thread(state.flush_slot_now, slot)

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
    cfg.agent.member_acp_backend = ACP_BACKEND_CLAUDE
    app = web.Application()
    app["state"] = state
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{name}/edit-resend",
        match_info={"slot": name},
        app=app,
    )
    request["app"] = ""

    async def _json():
        return {"index": 0, "content": "edited"}

    request.json = _json  # type: ignore[method-assign]
    real_restore = chat_persistence.restore_destructive_history_after_auth_failure

    async def _dirty_flush_then_restore(*args, **kwargs):
        # The flushed segment reaches disk before the rollback, so its disk copy
        # must be classified as the failed branch, not as a foreign append.
        assert await chat_persistence.save_slot_off_loop(state, slot, best_effort=False)
        return await real_restore(*args, **kwargs)

    with (
        patch.object(KiroCrewConfig, "load", classmethod(lambda cls: cfg)),
        patch.object(chat_regenerate, "_run_chat", _run_chat),
        patch.object(
            chat_regenerate,
            "restore_destructive_history_after_auth_failure",
            _dirty_flush_then_restore,
        ),
    ):
        response = await chat_regenerate.api_chat_slot_edit_resend(request)
        assert response.status == 200, response.body
        turn_task = slot.task
        assert turn_task is not None
        await asyncio.wait_for(asyncio.shield(turn_task), timeout=5)

    live = [(row["role"], row["content"]) for row in slot.messages]
    persisted = [
        (row["role"], row["content"]) for row in state.conversation_log.read_messages(history_key)
    ]
    assert live[: len(original)] == original
    assert persisted[: len(original)] == original
    assert ("assistant", "half an answer") not in live
    assert ("assistant", "half an answer") not in persisted
    assert ("user", "edited") not in live
    assert ("user", "edited") not in persisted
    assert any(role == "error" and "not signed in" in text for role, text in live)
    assert any(role == "error" and "not signed in" in text for role, text in persisted)


@pytest.mark.asyncio
async def test_auth_rollback_preserves_foreign_disk_append(tmp_path, monkeypatch) -> None:
    """Rollback drops its failed candidate but keeps foreign and duplicate disk appends."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.push_slots_update = MagicMock()
    name = "foreign-auth-append"
    history_key = f"dashboard:{name}"
    slot = state.get_or_create_slot(name)
    slot.append("user", "first question", ts="2020-01-01T00:00:00+00:00")
    slot.append("assistant", "first answer", ts="2020-01-01T00:00:01+00:00")
    slot.drain()
    checkpoint = chat_persistence.capture_destructive_history_checkpoint(slot)
    assert await chat_persistence.save_slot_off_loop(state, slot, best_effort=False)

    slot.messages = [slot.messages[0]]
    slot.append("user", "edited question", ts="2021-01-01T00:00:00+00:00")
    slot.drain()
    candidate_messages = list(slot.messages)
    assert await chat_persistence.save_slot_off_loop(
        state,
        slot,
        candidate_messages,
        best_effort=False,
        expected_history_key=history_key,
        expected_disk_older_count=checkpoint.disk_older_count,
        expected_slot_name=name,
    )
    assert [row["content"] for row in state.conversation_log.read_messages(history_key)] == [
        "first question",
        "edited question",
    ]

    await asyncio.to_thread(
        partial(
            state.conversation_log.append,
            history_key,
            "assistant",
            "concurrent channel reply",
            mid="foreign-channel-row",
        )
    )
    await asyncio.to_thread(
        partial(
            state.conversation_log.append,
            history_key,
            "user",
            "first question",
        )
    )
    slot.append("error", "Claude Code is not signed in.", ts="9999-01-01T00:00:00+00:00")
    slot.drain()

    assert await chat_persistence.restore_destructive_history_after_auth_failure(
        state,
        slot,
        checkpoint,
        candidate_messages,
        expected_history_key=history_key,
        expected_slot_name=name,
    )

    persisted = [row["content"] for row in state.conversation_log.read_messages(history_key)]
    assert persisted == [
        "first question",
        "first answer",
        "concurrent channel reply",
        "first question",
        "Claude Code is not signed in.",
    ]
    assert persisted.count("first question") == 2
    assert "edited question" not in persisted


@pytest.mark.asyncio
async def test_auth_rollback_write_failure_leaves_the_restore_owed_to_the_flush(
    tmp_path, monkeypatch
) -> None:
    """A rollback whose own disk write fails keeps the live checkpoint and re-arms the flush.

    The restore swaps the live window and sets ``_dirty`` + ``_pending_rewrite``
    BEFORE its write, and that write is best-effort like the truncation's, so a
    transient persistence failure leaves the restored branch as the retry source:
    the periodic flush re-projects it, and the failed candidate never becomes the
    durable transcript.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    _install_foreign_auth_failure_turn(state)
    name = "foreign-auth-retry"
    history_key = f"dashboard:{name}"
    slot = state.get_or_create_slot(name)
    slot.title = "Foreign auth retry history"
    slot._titled = True
    slot.append("user", "first question")
    slot.append("assistant", "first answer")
    slot.append("user", "second question")
    slot.append("assistant", "second answer")
    slot.drain()
    original = [(row["role"], row["content"]) for row in slot.messages]
    await asyncio.to_thread(state.flush_slot_now, slot)

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
    cfg.agent.member_acp_backend = ACP_BACKEND_CLAUDE
    app = web.Application()
    app["state"] = state
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{name}/edit-resend",
        match_info={"slot": name},
        app=app,
    )
    request["app"] = ""

    async def _json():
        return {"index": 0, "content": "edited"}

    request.json = _json  # type: ignore[method-assign]
    real_save = chat_persistence._save_slot_to_history
    failed_writes: list[dict] = []

    def _fail_the_rollback_write_once(*args, **kwargs):
        # Only the rollback passes ``rewrite_foreign_basis``; the truncation save
        # before the turn and the flush afterwards take the real writer.
        if kwargs.get("rewrite_foreign_basis") is not None and not failed_writes:
            failed_writes.append(kwargs)
            raise OSError("history unavailable during the rollback write")
        return real_save(*args, **kwargs)

    with (
        patch.object(KiroCrewConfig, "load", classmethod(lambda cls: cfg)),
        patch.object(chat_regenerate, "_run_chat", _run_chat),
        patch.object(chat_persistence, "_save_slot_to_history", _fail_the_rollback_write_once),
    ):
        response = await chat_regenerate.api_chat_slot_edit_resend(request)
        assert response.status == 200, response.body
        turn_task = slot.task
        assert turn_task is not None
        await asyncio.wait_for(asyncio.shield(turn_task), timeout=5)

    assert len(failed_writes) == 1
    # (a) The live window is the checkpoint plus the actionable auth error, and
    # nothing else -- the failed write did not leave the candidate in memory.
    # (``done`` is the runner's transient end-of-turn marker; the writer skips it.)
    live = [(row["role"], row["content"]) for row in slot.messages if row.get("role") != "done"]
    assert live[: len(original)] == original
    assert ("user", "edited") not in live
    tail = live[len(original) :]
    assert len(tail) == 1
    assert tail[0][0] == "error" and "not signed in" in tail[0][1]
    # (b) The failed write left the restore owed to the periodic flush.
    assert slot._dirty and slot._pending_rewrite
    # The failure was real: disk still holds the truncated candidate.
    persisted_before = [
        (row["role"], row["content"]) for row in state.conversation_log.read_messages(history_key)
    ]
    assert ("user", "edited") in persisted_before
    assert ("assistant", "first answer") not in persisted_before

    await asyncio.to_thread(
        partial(
            state.conversation_log.append,
            history_key,
            "assistant",
            "concurrent channel reply",
            mid="foreign-channel-row",
        )
    )

    # The flush pass re-projects the restored window and lands it on disk.
    await asyncio.to_thread(state.flush_slot_now, slot)
    assert not slot._dirty and not slot._pending_rewrite
    persisted = [
        (row["role"], row["content"]) for row in state.conversation_log.read_messages(history_key)
    ]
    assert ("assistant", "concurrent channel reply") in persisted
    assert ("user", "edited") not in persisted
    persisted_iter = iter(persisted)
    assert all(row in persisted_iter for row in live)
    assert slot._pending_rewrite_basis is None
