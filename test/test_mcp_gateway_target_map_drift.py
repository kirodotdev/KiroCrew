"""An adopted daemon's target map goes stale, and nothing used to say so.

The daemon's target map is baked into its process environment at
``GatewayManager._spawn_once`` and a frozen ``GatewaySpec`` is never re-applied
afterwards. ``_start_locked`` adopts any process answering ``pong``, so a daemon
that outlived a ``stub_servers`` change keeps serving a map that predates it --
for as long as it holds the socket. Observed in the field as a 25-day-old daemon
whose env knew only ``excalidraw`` and ``pdf``: every session's
``kirocrew-core`` stub registered, pre-flighted ``ensure_backend``, was refused
for an unknown target, and died in 0.2s, removing that server's whole tool
surface with no durable record anywhere.

Same mixed-version hazard as ``test_mcp_gateway_stub_poolable_ack`` -- adoption
with no handshake -- so the same shape of fix: the daemon says what it can serve,
and the adopting side checks.

Two properties are pinned here:

* the drift is DETECTED and reported (it was previously invisible), and
* an unknown target at the pre-flight is fallback-ELIGIBLE, so the stub degrades
  to a per-session exec instead of dying.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway.gatewayd import _TargetUnknown, resolvable_target_stems
from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec
from kiro_crew.mcp_gateway.stub import (
    TARGET_UNKNOWN_REASON,
    fallback_counts,
    log_fallback,
    must_degrade_unknown_target,
)


def _manager(target_env: dict[str, str]) -> GatewayManager:
    return GatewayManager(
        GatewaySpec(socket_path=Path("/tmp/does-not-need-to-exist.sock"), mcp_target_env=target_env)
    )


# --------------------------------------------------------------- stem parsing


def test_stems_strip_the_prefix_and_the_args_hash() -> None:
    """A hashed twin and its bare key describe the SAME server, so both must
    reduce to one stem -- otherwise a daemon carrying only the disambiguated
    entry would read as not serving the server at all."""
    env = {
        "KIROCREW_MCP_TARGET_KIROCREW_CORE": "kirocrew mcp-core",
        "KIROCREW_MCP_TARGET_KIROCREW_CORE__61774e2023ff185e": "kirocrew mcp-core",
        "KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf --stdio",
        "PATH": "/usr/bin",
        "KIROCREW_HOME": "/home/x/.kiro/crew",
    }
    assert resolvable_target_stems(env) == ["KIROCREW_CORE", "PDF"]


def test_the_legacy_prefix_still_counts_as_coverage() -> None:
    """``env_target_resolver`` accepts ``MC_MCP_TARGET_``, so a daemon holding
    only that spelling CAN serve the server. Reporting it as uncovered would
    raise a false drift alarm on an old-overlay install."""
    assert resolvable_target_stems({"MC_MCP_TARGET_EXCALIDRAW": "node x.js"}) == ["EXCALIDRAW"]


def test_no_target_keys_is_an_empty_report_not_a_crash() -> None:
    assert resolvable_target_stems({"PATH": "/usr/bin"}) == []


# ------------------------------------------------------- required-stem derivation


def test_required_stems_come_from_the_spec_the_rewriter_just_computed() -> None:
    """Derived from ``spec.mcp_target_env`` rather than a second config read, so
    the check cannot disagree with what a spawn would actually have applied."""
    mgr = _manager(
        {
            "KIROCREW_MCP_TARGET_KIROCREW_CORE": "kirocrew mcp-core",
            "KIROCREW_MCP_TARGET_KIROCREW_CORE__deadbeef": "kirocrew mcp-core",
            "KIROCREW_MCP_TARGET_EXCALIDRAW": "node x.js",
        }
    )
    assert mgr._required_target_stems() == {"KIROCREW_CORE", "EXCALIDRAW"}


# ------------------------------------------------------------- the drift report


def test_the_field_case_is_reported(caplog) -> None:
    """The exact production shape: config wants core stubbed, the incumbent's
    env knows only the two servers it was spawned with."""
    mgr = _manager(
        {
            "KIROCREW_MCP_TARGET_KIROCREW_CORE": "kirocrew mcp-core",
            "KIROCREW_MCP_TARGET_EXCALIDRAW": "node x.js",
            "KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf --stdio",
        }
    )
    with caplog.at_level(logging.WARNING):
        assert mgr._adoption_drift({"type": "pong", "targets": ["EXCALIDRAW", "PDF"]}) == [
            "KIROCREW_CORE"
        ]
    assert "KIROCREW_CORE" in caplog.text
    assert "STALE" in caplog.text


def test_a_covering_daemon_is_silent(caplog) -> None:
    """No warning on the healthy path, or the signal is worthless."""
    mgr = _manager({"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf --stdio"})
    with caplog.at_level(logging.WARNING):
        assert mgr._adoption_drift({"type": "pong", "targets": ["PDF"]}) == []
    assert caplog.text == ""


def test_a_superset_daemon_is_also_silent(caplog) -> None:
    """Coverage is a SUPERSET test: a daemon serving more than this spec needs is
    fine (another agent's servers), and must not be flagged."""
    mgr = _manager({"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf --stdio"})
    with caplog.at_level(logging.WARNING):
        assert (
            mgr._adoption_drift({"type": "pong", "targets": ["PDF", "EXCALIDRAW", "KIROCREW_CORE"]})
            == []
        )
    assert caplog.text == ""


def test_a_daemon_that_cannot_report_is_flagged_as_unverifiable(caplog) -> None:
    """The pre-upgrade survivor omits ``targets`` entirely -- and that is exactly
    the class of daemon that carries a stale map, so silence would hide the one
    case this exists to catch."""
    mgr = _manager({"KIROCREW_MCP_TARGET_KIROCREW_CORE": "kirocrew mcp-core"})
    with caplog.at_level(logging.WARNING):
        assert mgr._adoption_drift({"type": "pong"}) == ["KIROCREW_CORE"]
    assert "does not report its target map" in caplog.text


def test_nothing_configured_means_nothing_to_verify(caplog) -> None:
    """A gateway with no stubs has no coverage requirement; an old daemon on the
    socket is then not a drift finding."""
    mgr = _manager({})
    with caplog.at_level(logging.WARNING):
        assert mgr._adoption_drift({"type": "pong"}) == []
    assert caplog.text == ""


# ------------------------------------------- the legacy daemon (untagged reply)
#
# The gatewayd-side ``fallback: true`` tag only helps when the daemon is current.
# The drift victim is by definition an OLD daemon: adoption has no version
# handshake, so a survivor of a package upgrade serves brand-new stubs while
# predating every wire field added since -- and its frozen target map is exactly
# why it is missing a server. Without the stub-side classifier the fix would be
# inert for the case that motivated it.


def test_the_stub_side_string_matches_the_daemon_that_sends_it() -> None:
    """Anti-drift pin. ``stub.py`` deliberately does not import ``gatewayd`` (it
    is the per-session hot path), so the two sides agree only by convention --
    and one of the senders is an old process whose text can never be patched.
    If gatewayd's wording is ever edited, this fails instead of the classifier
    silently going dead."""
    reason = str(
        _TargetUnknown(
            "no target mapping for server 'kirocrew-core'; "
            "set KIROCREW_MCP_TARGET_<SERVER> env var or pass a target_resolver"
        )
    )
    assert TARGET_UNKNOWN_REASON in reason


def test_an_untagged_target_unknown_still_degrades() -> None:
    """The reported failure verbatim: upgrade, old daemon survives, adoption,
    untagged target-unknown. This must route to the fallback exec, not exit."""
    assert (
        must_degrade_unknown_target(
            "no target mapping for server 'kirocrew-core'; "
            "set MC_MCP_TARGET_<SERVER> env var or pass a target_resolver"
        )
        is True
    )


def test_the_legacy_env_prefix_spelling_does_not_break_the_match() -> None:
    """Only the stable leading clause is matched. The remedy clause names an env
    prefix that HAS changed across versions (``MC_MCP_TARGET_`` ->
    ``KIROCREW_MCP_TARGET_``), so matching the whole sentence would miss exactly
    the old daemons this exists for."""
    for spelling in ("MC_MCP_TARGET_", "KIROCREW_MCP_TARGET_"):
        assert must_degrade_unknown_target(
            f"no target mapping for server 'x'; set {spelling}<SERVER> env var"
        )


@pytest.mark.parametrize(
    "reason",
    [
        "backend spawn failed: ENOMEM",
        "circuit breaker OPEN",
        "pool full",
        "internal error: gateway bug",
        "",
    ],
)
def test_a_genuine_failure_stays_terminal(reason: str) -> None:
    """The narrowness is the safety property. Degrading every untagged rejection
    would turn a genuinely unrunnable backend into a per-session crash-loop,
    which is the reason the terminal path exists at all."""
    assert must_degrade_unknown_target(reason) is False


def test_a_missing_reason_is_not_treated_as_unknown_target() -> None:
    """``ready.get("reason")`` can be absent; ``None`` must not match."""
    assert must_degrade_unknown_target(None) is False  # type: ignore[arg-type]


# ------------------------------------------------ terminal vs fallback telemetry
#
# Both events share one log (one rotation, one place to look) but they are not the
# same event and must not be added together: `fallback_counts` feeds gatewayd's
# `stats` reply, and an operator reads that rate to decide whether pooling is
# engaging. Counting stubs that DIED as degradations misreports exactly that.


class _Args:
    server = "kirocrew-core"
    agent = "kirocrew"
    channel_id = ""
    target_command = "/x/kirocrew"


def _counts_in(tmp_path, monkeypatch, records):
    """Write `records` through the real writer, then aggregate them."""
    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.stub._fallback_log_path",
        lambda: tmp_path / "stub_fallback.jsonl",
    )
    for reason, terminal in records:
        log_fallback(reason, "uuid-x", "kirocrew:kirocrew-core", _Args(), terminal=terminal)
    return fallback_counts()


def test_a_terminal_record_does_not_inflate_the_fallback_rate(tmp_path, monkeypatch):
    """The regression: one real degradation and one death must not read as two
    degradations."""
    c = _counts_in(
        tmp_path,
        monkeypatch,
        [("at capacity", False), ("terminal:backend spawn failed: ENOMEM", True)],
    )
    assert c["total"] == 1
    assert c["by_server"] == {"kirocrew-core": 1}
    assert c["by_reason"] == {"at capacity": 1}
    assert c["terminal_total"] == 1
    assert c["terminal_by_server"] == {"kirocrew-core": 1}


def test_a_terminal_reason_does_not_leak_into_by_reason(tmp_path, monkeypatch):
    """`by_reason` is the degradation breakdown; a terminal reason appearing there
    would send an operator chasing a fallback that never happened."""
    c = _counts_in(tmp_path, monkeypatch, [("terminal:whatever", True)])
    assert c["total"] == 0
    assert c["by_reason"] == {}
    assert c["terminal_total"] == 1


def test_the_split_is_driven_by_the_field_not_the_reason_prefix(tmp_path, monkeypatch):
    """A degradation whose reason merely starts with the word is still a
    degradation. Parsing the prefix would misclassify it; the structured flag
    cannot."""
    c = _counts_in(tmp_path, monkeypatch, [("terminal:looks-terminal", False)])
    assert c["total"] == 1
    assert c["terminal_total"] == 0


def test_a_record_predating_the_flag_still_counts_as_a_fallback(tmp_path, monkeypatch):
    """Back-compat: an old log line has no `terminal` key, and it WAS a fallback,
    so it must keep counting as one rather than vanishing from the rate."""
    import json
    import time

    log = tmp_path / "stub_fallback.jsonl"
    log.write_text(
        json.dumps({"ts": time.time(), "reason": "at capacity", "server": "pdf"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.mcp_gateway.stub._fallback_log_path", lambda: log)
    c = fallback_counts()
    assert c["total"] == 1
    assert c["terminal_total"] == 0
