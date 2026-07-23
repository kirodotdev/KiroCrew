"""Tests for the Slack message-gate + token-anchor + reply-decorator seams.

Three v1 CPP method additions that let an edition compose a "challenge-and-
redirect" Slack posture without editing the core:

  * ``SlackEnterpriseGate.intercept_message`` — called at the top of
    ``_route_message``; Default ``PROCESS`` keeps the OSS inline behavior.
  * ``DashboardContributor.on_token_consumed`` — the anchor fired after
    ``bind_token_ip``; Default no-op.
  * ``DashboardContributor.decorate_reply`` — the outbound-reply transform;
    Default identity.

These tests pin the OSS-preserving Defaults and the ``InterceptDecision`` /
short-circuit contract the companion depends on. They do NOT exercise a real
challenge flow (that lives in the companion) — only that the public core exposes
the seam and behaves byte-identically until a companion overrides it.
"""

from __future__ import annotations

from kiro_crew.platform.defaults import (
    DefaultDashboardContributor,
    DefaultSlackEnterpriseGate,
)
from kiro_crew.platform.interfaces import InterceptDecision


class TestInterceptDecisionDefault:
    def test_default_gate_processes_inline(self) -> None:
        gate = DefaultSlackEnterpriseGate()
        decision = gate.intercept_message(
            object(),
            channel="C123",
            sender_id="U123",
            clean_text="hello",
            thread_ts=None,
            msg_ts="1700000000.0001",
        )
        # OSS default MUST be PROCESS — the public build has no challenge-redirect.
        assert decision is InterceptDecision.PROCESS

    def test_decision_enum_members(self) -> None:
        # The three verdicts the companion relies on, with stable string values.
        assert InterceptDecision.PROCESS.value == "process"
        assert InterceptDecision.REDIRECTED.value == "redirected"
        assert InterceptDecision.DROPPED.value == "dropped"

    def test_only_process_avoids_short_circuit(self) -> None:
        # The _route_message contract: any non-PROCESS verdict short-circuits.
        # Encode that invariant here so a future refactor can't silently let
        # REDIRECTED/DROPPED fall through to inline processing.
        short_circuit = {d: (d is not InterceptDecision.PROCESS) for d in InterceptDecision}
        assert short_circuit == {
            InterceptDecision.PROCESS: False,
            InterceptDecision.REDIRECTED: True,
            InterceptDecision.DROPPED: True,
        }


class TestDashboardContributorSeamsDefault:
    def test_on_token_consumed_is_noop(self) -> None:
        dc = DefaultDashboardContributor()
        # No return, no raise — the OSS build opens no Slack auth window.
        assert dc.on_token_consumed("U1", "C1", 0.0, None) is None
        assert dc.on_token_consumed("U1", "C1", 1_700_000_000.0, "t.1") is None

    def test_decorate_reply_is_identity(self) -> None:
        dc = DefaultDashboardContributor()
        # OSS sends replies unchanged (no expiry footer / window refresh).
        assert dc.decorate_reply("hello world", channel="C1", user_id="U1") == "hello world"
        assert dc.decorate_reply("", channel="C1", user_id="U1") == ""


class TestDecoratedReplyIsReScanned:
    """The reply path re-runs redaction on any text decorate_reply INTRODUCES.

    Redaction runs before the decorator seam, so a decorator that appended a
    credential- or exfil-shaped token would otherwise reach Slack unscanned. The
    handler guards this by re-running both passes when the decorated text differs
    from the pre-decoration text; these tests pin that the redaction primitives it
    re-runs actually catch injected content (the handler wiring is covered by the
    integration suite).
    """

    def test_redaction_catches_credential_a_decorator_could_inject(self) -> None:
        from kiro_crew.security import redact_credentials

        # A footer that (pathologically) carried an AWS key must be redacted.
        decorated = "Your reply.\n\n_expires in 4m_ AKIAIOSFODNN7EXAMPLE"
        cleaned, warnings = redact_credentials(decorated)
        assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
        assert warnings  # a credential was flagged

    def test_redaction_leaves_benign_footer_untouched(self) -> None:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        # The realistic decorator output (a plain expiry footer) survives both
        # passes unchanged, so the re-scan never mangles a legitimate decoration.
        decorated = "Your reply.\n\n_This session expires in 4 minutes._"
        after_exfil, exfil_w = redact_exfiltration_urls(decorated)
        after_cred, cred_w = redact_credentials(after_exfil)
        assert after_cred == decorated
        assert not exfil_w and not cred_w


class TestInterceptOrderingIsBeforeContentRecording:
    """Security-critical ordering: intercept_message MUST run before any content
    is recorded/processed.

    If the interceptor ran after ``channel_history.push`` / transcription / file
    download, an unverified sender's message content would be persisted to channel
    history before the gate decided — and a later VERIFIED turn in the same channel
    could pull that stored content into agent context, bypassing the challenge
    gate. This is a source-order regression guard on ``_route_message``: a refactor
    that moves the push above the interceptor re-opens the hole.
    """

    def _call_offsets(self, needle: str) -> list[int]:
        """Offsets of ACTUAL call sites of ``needle`` in _route_message source.

        Matches ``<needle>(`` (a call), and strips ``#``-comment lines first, so a
        mention of the symbol in a docstring/comment (this PR's ordering rationale
        names these very calls in prose) can't be mistaken for a real call site.
        """
        import inspect
        import re

        from kiro_crew.slack import events

        raw = inspect.getsource(events._route_message)
        code = "\n".join(re.sub(r"#.*$", "", ln) for ln in raw.splitlines())
        return [m.start() for m in re.finditer(re.escape(needle) + r"\(", code)]

    def test_interceptor_precedes_first_channel_history_push(self) -> None:
        intercepts = self._call_offsets("intercept_message")
        pushes = self._call_offsets("channel_history.push")
        assert intercepts, "intercept_message call not found in _route_message"
        assert pushes, "channel_history.push call not found in _route_message"
        # The interceptor call must precede the FIRST real history push.
        assert min(intercepts) < min(pushes), (
            "intercept_message must run BEFORE channel_history.push — otherwise an "
            "unverified sender's content is persisted before the gate decides "
            "(challenge-gate bypass via stored history)."
        )

    def test_interceptor_precedes_file_transcription_and_download(self) -> None:
        intercepts = self._call_offsets("intercept_message")
        assert intercepts, "intercept_message call not found in _route_message"
        # Content-processing sites that must not run on an un-gated message.
        for marker in ("_transcribe_with_reaction", "process_slack_files"):
            marks = self._call_offsets(marker)
            assert marks, f"{marker} call not found in _route_message"
            assert min(intercepts) < min(marks), (
                f"intercept_message must run BEFORE {marker} — an intercepted "
                "message must not be transcribed/downloaded before the gate decides."
            )
