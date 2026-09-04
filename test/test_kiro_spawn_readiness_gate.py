"""Poll-driven kiro-cli spawn sites must not run the CLI while signed out.

``kiro-cli`` auto-launches an interactive browser login whenever a subcommand
runs unauthenticated (``--no-interactive`` does not suppress it and there is no
opt-out env var). ``/api/models`` is polled every 8s while the model list is
degraded and ``/api/sessions/usage`` every 30s, so an unauthenticated gateway
used to spawn a browser-opening CLI on every cycle — dozens of browser windows
and an unusable dashboard.

These tests pin the fix: both handlers consult the prerequisite readiness latch
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

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard import kiro_readiness
from kiro_crew.dashboard.handlers import agents, sessions
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

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
    """The gate must SAY it refused (issue #4577).

    Every post-spawn failure branch in ``api_models`` logs a WARNING, so a reader
    who greps the log for that endpoint and finds nothing concludes the endpoint is
    healthy. When the gate refused silently that conclusion was exactly inverted,
    and it cost hours of misdiagnosis. An absent log line gets read as evidence.
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
