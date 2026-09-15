"""Tests for the kiro_cli_logs reader (diagnostics.read_kiro_cli_logs) + MCP tool.

Two security-critical properties, and both have a mutation guard so a green run
is attributable to the code rather than to the fixture:

1. SCOPE. The tool reads kiro-cli's mcp/lsp PROTOCOL logs only. The two
   conversation-bearing sources kiro-cli also writes — ``kiro-chat.log`` and the
   ``sessions/cli/<sid>.jsonl`` transcripts — are single shared host files per
   gateway, so returning either would disclose another session's private
   conversation to the calling session; the credential-oriented redaction stack
   does not narrow prose. ``test_chat_log_is_not_a_source`` plants a populated
   chat log and asserts none of it comes back.
2. REDACTION. Every live-secret shape kiro-cli writes into its logs — a bearer
   token, a mid-line ``Authorization: Basic <b64>`` header, an ``mc_token`` auth
   cookie, and the serialized ``"authorization": ...`` JSON form — must be gone
   from the returned text. ``test_redaction_is_load_bearing`` empties the extra
   stack and asserts the very same secrets then leak, so the passing assertion
   is not vacuous.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import diagnostics
from kiro_crew.mcp_tools import logs as logs_tool

# All four secret shapes the issue names, in one log body.
_LOG = (
    "2026-09-06T16:00:01 boot ok\n"
    "2026-09-06T16:00:02 Authorization: Bearer sk-ant-SECRETtoken1234567890abcXYZ\n"
    "2026-09-06T16:00:03 Set-Cookie: mc_token_5476=supersecretcookievalueABCDEF123456\n"
    "2026-09-06T16:00:04 ERROR request used Authorization: Basic TWlkTGluZUxFQUsxMjNhYmM=\n"
    '2026-09-06T16:00:05 {"headers": {"authorization": "Basic UVVPVEVEbGVha0FCQzEyMw=="}}\n'
    "2026-09-06T16:00:06 a perfectly normal log line\n"
)

_SECRETS = (
    "sk-ant-SECRETtoken1234567890abcXYZ",
    "supersecretcookievalueABCDEF123456",
    "TWlkTGluZUxFQUsxMjNhYmM=",
    "UVVPVEVEbGVha0FCQzEyMw==",
)


def _source(monkeypatch, tmp_path: Path, body: str, name: str = "mcp.log") -> Path:
    """Write ``body`` to a protocol log and make it the reader's only source.

    ``encoding="utf-8"`` is required, not tidiness: bare ``write_text`` encodes
    with the platform's locale codec, which on a Windows runner is cp1252 and
    cannot represent the zero-width space a hidden-character fixture needs -- the
    test dies in its own setup with a ``UnicodeEncodeError``. UTF-8 also matches
    what the reader does, since ``_read_log_tail`` decodes bytes as UTF-8
    explicitly rather than following the locale.
    """
    log = tmp_path / name
    log.write_text(body, encoding="utf-8")
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [log])
    return log


def _no_sources(monkeypatch) -> None:
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])


def test_reads_and_redacts_every_secret_shape(tmp_path, monkeypatch):
    _source(monkeypatch, tmp_path, _LOG)

    out = diagnostics.read_kiro_cli_logs()

    for secret in _SECRETS:
        assert secret not in out, f"secret leaked from kiro_cli_logs: {secret!r}"
    assert "a perfectly normal log line" in out
    assert "[REDACTED]" in out
    assert "mcp.log" in out


def test_redaction_is_load_bearing(tmp_path, monkeypatch):
    """Mutation guard: disable the extra-redaction stack and the secrets leak.

    A redaction test that still passes with redaction disabled proves nothing.
    Here we empty ``_EXTRA_REDACTIONS`` (the stack that covers the bearer /
    Authorization / mc_token shapes) and assert the SAME secrets now appear —
    so the green assertion in ``test_reads_and_redacts_every_secret_shape`` is
    attributable to that stack, not to the secrets never being there.
    """
    _source(monkeypatch, tmp_path, _LOG)
    monkeypatch.setattr(diagnostics, "_EXTRA_REDACTIONS", ())

    out = diagnostics.read_kiro_cli_logs()

    # With the extra stack gone, the header/bearer/cookie shapes leak verbatim
    # (redact_credentials / redact_exfiltration_urls do not cover them — that is
    # exactly why _EXTRA_REDACTIONS exists).
    leaked = [s for s in _SECRETS if s in out]
    assert leaked, "no secret leaked with _EXTRA_REDACTIONS disabled — the test is vacuous"


def test_chat_log_is_not_a_source(tmp_path, monkeypatch):
    """kiro-chat.log is cross-session conversation content and must not be read.

    ``_kiro_cli_chat_log`` resolves ONE fixed host path
    (``<runtime-or-tmp>/kiro-log/kiro-chat.log``), not a per-session file, so on a
    host running one gateway with many concurrent sessions it interleaves every
    session's request/response traffic. ``_scrub`` is a credential pass and does
    not narrow conversation prose, and there is no per-session delimiter to filter
    on — so an agent-callable read of it would hand session A's private
    conversation to session B. The bundle collector still includes it (that path
    is user-to-user); this tool must not.

    The guard is mutation-shaped: the chat log EXISTS and is discoverable here,
    so an implementation that appended it as a source would fail this test.
    """
    chat = tmp_path / "kiro-chat.log"
    chat.write_text("2026-09-06T16:00:00 SESSION-B-PRIVATE-CONVERSATION-PROSE\n")
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: chat)
    # A protocol log is present too, so the tool has something to return and the
    # assertion below cannot pass merely because the reader found nothing at all.
    _source(monkeypatch, tmp_path, "2026-09-06T16:00:00 mcp handshake ok\n")

    out = diagnostics.read_kiro_cli_logs()

    assert "mcp handshake ok" in out, "the protocol log should still be read"
    assert "SESSION-B-PRIVATE-CONVERSATION-PROSE" not in out
    assert "=== kiro-chat.log" not in out, "the chat log must not appear as a section"


def test_oversized_single_line_returns_only_the_marker(tmp_path, monkeypatch):
    """An unterminated line longer than the cap has no boundary to cut on.

    The byte window is meant to start on a line boundary, because
    ``_EXTRA_REDACTIONS`` anchors on the header NAME and redacts to end-of-line:
    a cut landing after ``Authorization:`` but before its value strips the token
    that would have redacted the value and hands the raw secret to the caller.

    Reaching the no-newline case needs the file's final line to be BOTH longer
    than the cap AND UNTERMINATED -- which is a log being appended to right now,
    with a large JSON-RPC frame partially flushed. If the file ended with a
    newline the read window would contain it (the window runs to EOF), the cut
    would land there and the result would be just the marker anyway; so a
    fixture with a trailing newline does not exercise this branch at all.

    Mutation-verified: the secret sits AFTER the cut point with its anchor
    BEFORE it, the arrangement `_scrub` cannot catch, so the previous behavior
    (return the fragment) leaks it and fails this test.
    """
    cap = 4096
    secret = "OVERSIZEDlineLEAKsecret9876543210"
    # ONE unterminated line: anchor first, padding so the last `cap` bytes begin
    # past the anchor, then the secret. No trailing newline -- see the docstring.
    one_line = "2026-09-06T16:00:00 Authorization: Basic " + ("Q" * cap * 3) + secret
    log = _source(monkeypatch, tmp_path, one_line)
    monkeypatch.setattr(diagnostics, "_MAX_LOG_READ_BYTES", cap)

    # Preconditions: this really is the no-boundary leak arrangement.
    assert log.stat().st_size > cap
    window = one_line[-cap:]
    assert "\n" not in window, "fixture must put NO newline in the read window"
    assert "Authorization" not in window, "anchor must fall outside the window"
    assert secret in window, "secret must fall inside the window"

    out = diagnostics.read_kiro_cli_logs()

    assert secret not in out, "a mid-line fragment leaked a credential past its anchor"
    assert "truncated" in out, "the truncation marker should still say the tail was cut"


def test_oversized_terminated_line_also_yields_no_fragment(tmp_path, monkeypatch):
    """The sibling case: same oversized line, but newline-terminated.

    Here the window DOES contain a newline (it runs to EOF), so the cut lands on
    it and the remainder is empty. Pinned alongside the unterminated case so a
    future change to the cut logic cannot fix one and regress the other.
    """
    cap = 4096
    secret = "TERMINATEDoversizedLEAK55555"
    _source(
        monkeypatch,
        tmp_path,
        "2026-09-06T16:00:00 Authorization: Basic " + ("Q" * cap * 3) + secret + "\n",
    )
    monkeypatch.setattr(diagnostics, "_MAX_LOG_READ_BYTES", cap)

    out = diagnostics.read_kiro_cli_logs()

    assert secret not in out
    assert "truncated" in out


def test_since_drops_continuations_of_rejected_events(tmp_path, monkeypatch):
    """A continuation line inherits the verdict of the event it belongs to.

    Keeping continuations unconditionally left orphaned bodies from events
    ``since`` had rejected, so the filter did not actually narrow the window --
    the bulk of an old event (its payload) rode along without the header line
    that identified it.
    """
    _source(
        monkeypatch,
        tmp_path,
        "2026-09-06T15:00:00 HEADEROLD\n"
        "  BODYOLD is the old event's payload\n"
        "2026-09-06T17:00:00 HEADERNEW\n"
        "  BODYNEW is the new event's payload\n",
    )

    out = diagnostics.read_kiro_cli_logs(since="2026-09-06T16:")

    assert "HEADEROLD" not in out
    assert "BODYOLD" not in out, "continuation of a rejected event rode along"
    assert "HEADERNEW" in out
    assert "BODYNEW" in out, "continuation of a KEPT event must still ride along"


def test_since_keeps_the_truncation_marker(tmp_path, monkeypatch):
    """The marker precedes every event, so it has no verdict to inherit -- keep it.

    Guard against fixing the orphaned-continuation bug by dropping every
    pre-event line, which would silently hide the fact that the tail was cut.
    """
    marker_line = "...[truncated: showing last 4096 bytes]...\n"
    filtered = diagnostics._filter_since(
        marker_line + "2026-09-06T17:00:00 late event\n",
        "2026-09-06T16:",
    )
    assert "truncated" in filtered
    assert "late event" in filtered


def test_response_budget_stays_under_the_transport_ceiling():
    """The reader's own ceiling must sit under the MCP transport's.

    This is the whole point of `_MAX_LOG_RESPONSE_CHARS`: every tool response
    leaves through `validation.build_tool_response`, whose `sanitize_response`
    truncates the TAIL at `MAX_RESPONSE_LEN`. If the reader's budget ever rose to
    or above that, the transport would start cutting again -- and it cuts the end,
    which for a log tail is the newest lines.

    The coupling deliberately lives HERE rather than as an import in
    `diagnostics`, which does not otherwise depend on `validation`. The test is
    what keeps the two numbers in the right order, so a change to either side
    fails rather than silently re-opening the truncation.
    """
    from kiro_crew import validation

    assert diagnostics._MAX_LOG_RESPONSE_CHARS < validation.MAX_RESPONSE_LEN
    # And with room for the framing the reader adds on top of the log text.
    assert diagnostics._MAX_LOG_RESPONSE_CHARS + 2000 <= validation.MAX_RESPONSE_LEN


def test_response_over_budget_keeps_the_newest_lines(tmp_path, monkeypatch):
    """When the whole response will not fit, the OLDEST output goes.

    The transport would drop the tail, i.e. the newest lines -- the entire reason
    a tail was requested. The reader trims from the front instead, so this test
    asserts the direction: the last line survives, the first does not, the header
    stays (it states the tail/since/redaction count), and the drop is labelled.
    """
    monkeypatch.setattr(diagnostics, "_MAX_LOG_RESPONSE_CHARS", 3000)
    body = "".join(f"2026-09-06T16:00:00 line{i:05d} {'z' * 60}\n" for i in range(200))
    _source(monkeypatch, tmp_path, body)

    out = diagnostics.read_kiro_cli_logs(tail=100000)

    assert len(out) <= 3000, "the reader must respect its own response budget"
    assert "line00199" in out, "the NEWEST line must survive"
    assert "line00000" not in out, "the oldest line should have been dropped"
    assert out.startswith("kiro-cli logs (tail="), "the header must survive the trim"
    assert "older output dropped" in out, "the drop must say which end went"


def test_per_source_cap_is_divided_across_sources(tmp_path, monkeypatch):
    """Two sources share the response budget, so neither is crowded out.

    Giving each source the full per-source cap and trimming the assembled result
    afterwards would drop one source entirely -- the trim runs from the front, so
    the first section would go. Dividing up front means each source contributes
    its own newest lines.
    """
    a = tmp_path / "mcp.log"
    b = tmp_path / "lsp.log"
    a.write_text("".join(f"2026-09-06T16:00:00 AAA{i:05d}\n" for i in range(400)))
    b.write_text("".join(f"2026-09-06T16:00:00 BBB{i:05d}\n" for i in range(400)))
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [a, b])
    monkeypatch.setattr(diagnostics, "_MAX_LOG_RESPONSE_CHARS", 4000)

    out = diagnostics.read_kiro_cli_logs(tail=100000)

    assert len(out) <= 4000
    assert "AAA00399" in out, "the first source's newest line must survive"
    assert "BBB00399" in out, "the second source's newest line must survive"


def test_byte_cap_bounds_output_regardless_of_tail(tmp_path, monkeypatch):
    # Write a large log the honest way.
    big = "".join(f"2026-09-06T16:00:{i:02d} {'y' * 80}\n" for i in range(1000))
    _source(monkeypatch, tmp_path, big)
    monkeypatch.setattr(diagnostics, "_MAX_LOG_READ_BYTES", 4096)

    # A huge tail cannot enlarge the output past the byte cap.
    out = diagnostics.read_kiro_cli_logs(tail=100000)
    # Output = header + section framing + <=4096 bytes of log; give generous slack.
    assert len(out) < 4096 + 500
    assert "truncated" in out  # the tail marker _read_log_tail prepends


def test_tail_limits_line_count(tmp_path, monkeypatch):
    _source(
        monkeypatch,
        tmp_path,
        "".join(f"2026-09-06T16:00:00 line{i}\n" for i in range(50)),
    )

    out = diagnostics.read_kiro_cli_logs(tail=3)

    assert "line49" in out
    assert "line47" in out
    assert "line46" not in out  # only the last 3 survive


def test_since_filters_by_leading_timestamp(tmp_path, monkeypatch):
    _source(
        monkeypatch,
        tmp_path,
        "2026-09-06T15:00:00 early line\n"
        "2026-09-06T16:00:00 kept line\n"
        "  continuation of kept event\n"
        "2026-09-06T17:00:00 later line\n",
    )

    out = diagnostics.read_kiro_cli_logs(since="2026-09-06T16:")

    assert "early line" not in out
    assert "kept line" in out
    assert "continuation of kept event" in out  # non-timestamp line rides along
    assert "later line" in out


def test_no_logs_returns_a_note(tmp_path, monkeypatch):
    _no_sources(monkeypatch)
    out = diagnostics.read_kiro_cli_logs()
    assert "No kiro-cli protocol logs found" in out


def test_sensitive_path_source_is_refused(tmp_path, monkeypatch):
    """Defense in depth: a source that resolves to a fenced/sensitive path is skipped."""
    _source(monkeypatch, tmp_path, _LOG)
    # Force every path to read as sensitive; the reader must return zero sources.
    monkeypatch.setattr(diagnostics, "is_sensitive_path", lambda p: True)

    out = diagnostics.read_kiro_cli_logs()
    assert "No kiro-cli protocol logs found" in out


@requires_symlinks
def test_symlinked_source_is_not_followed(tmp_path, monkeypatch):
    off_tree = tmp_path / "off_tree_secret.txt"
    off_tree.write_text("TOPSECRETsymlinkVALUE123\n")
    link = tmp_path / "mcp.log"
    link.symlink_to(off_tree)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [link])

    out = diagnostics.read_kiro_cli_logs()
    assert "TOPSECRETsymlinkVALUE123" not in out
    assert "No kiro-cli protocol logs found" in out


@requires_symlinks
@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason=(
        "O_NOFOLLOW is a POSIX flag absent on Windows; the kernel-enforced "
        "no-follow open this test exercises cannot be applied there. On Windows "
        "the symlink defense is the caller's pre-open is_symlink check (covered "
        "by test_symlinked_source_is_not_followed, which runs on both), so "
        "this platform-specific strengthening is gated rather than asserted "
        "cross-platform."
    ),
)
def test_toctou_symlink_swap_is_refused_by_nofollow(tmp_path, monkeypatch):
    """A path swapped to a symlink AFTER the checks must not be followed (POSIX).

    The source dirs include the world-writable /tmp/kiro-log, so a local writer
    could replace a validated regular log with a symlink to ~/.ssh/config
    between the is_sensitive_path check and the open. _read_log_tail opens with
    O_NOFOLLOW and tails that same descriptor, so the swapped-in link is refused
    (ELOOP) rather than read. Simulated by making the source path a symlink and
    pushing the pre-open checks past it: only the O_NOFOLLOW open stands between
    the reader and the target.
    """
    target = tmp_path / "secret_target"
    target.write_text("TOCTOUsymlinkTARGETleak5555\n")
    link = tmp_path / "mcp.log"
    link.symlink_to(target)

    # The reader is what we exercise; force the pre-open guards to pass so the
    # O_NOFOLLOW open is the ONLY thing that can stop the swapped-in link.
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [link])
    monkeypatch.setattr(diagnostics.Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(diagnostics, "is_sensitive_path", lambda p: False)

    out = diagnostics.read_kiro_cli_logs()
    assert "TOCTOUsymlinkTARGETleak5555" not in out
    assert "No kiro-cli protocol logs found" in out


def test_read_log_tail_reads_a_regular_file(tmp_path):
    """_read_log_tail returns a regular file's bytes on every platform."""
    regular = tmp_path / "plain.log"
    regular.write_text("regular content\n")
    assert diagnostics._read_log_tail(regular, 4096) == "regular content\n"


def test_reader_works_without_o_nofollow(tmp_path, monkeypatch):
    """The tool still reads when O_NOFOLLOW is unavailable (the Windows path).

    Regression guard: an earlier version returned None (read nothing) whenever
    O_NOFOLLOW was absent, which silently disabled the whole tool on Windows.
    The no-follow open is a POSIX strengthening, not a precondition for reading.
    Exercised on any platform by deleting the attribute so `getattr(os,
    "O_NOFOLLOW", 0)` degrades to 0 (a plain open), matching Windows.
    """
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    _source(monkeypatch, tmp_path, "2026-09-06T16:00:00 windows path reads fine\n")

    out = diagnostics.read_kiro_cli_logs()
    assert "windows path reads fine" in out
    assert "No kiro-cli protocol logs found" not in out


def test_reader_works_without_o_nonblock(tmp_path, monkeypatch):
    """O_NONBLOCK is also a strengthening, not a precondition for reading.

    Same class of regression guard as the O_NOFOLLOW one, for the platform that
    does not define the flag (Windows). Simulated by SETTING it to 0 rather than
    deleting it: 0 is exactly what ``getattr(os, "O_NONBLOCK", 0)`` yields when
    the attribute is absent, and unlike a delattr it does not break unrelated
    machinery in this process that reads the attribute during the test.
    """
    monkeypatch.setattr(os, "O_NONBLOCK", 0, raising=False)
    _source(monkeypatch, tmp_path, "2026-09-06T16:00:00 no nonblock reads fine\n")

    out = diagnostics.read_kiro_cli_logs()
    assert "no nonblock reads fine" in out


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="os.mkfifo is POSIX-only; the FIFO swap this test plants cannot exist on Windows.",
)
def test_fifo_swapped_in_is_refused_without_hanging(tmp_path, monkeypatch):
    """A FIFO swapped in past the checks is refused, not waited on (O_NONBLOCK).

    Companion to the symlink TOCTOU case. O_NOFOLLOW refuses a swapped-in
    SYMLINK, but the same local writer to the world-writable /tmp/kiro-log can
    swap the validated regular file for a FIFO, and an O_RDONLY open of a FIFO
    with no writer BLOCKS INDEFINITELY — the fstat regular-file guard cannot run,
    because it only sees a descriptor the open already returned, so the agent's
    tool call would hang. O_NONBLOCK makes the open return immediately so the
    S_ISREG check can reject it.

    This test would HANG (not fail) without O_NONBLOCK, which is the point: no
    reader is ever opened on the FIFO.
    """
    fifo = tmp_path / "mcp.log"
    os.mkfifo(fifo)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [fifo])
    # Push the pre-open guards past it so the open flags are the only defense.
    monkeypatch.setattr(diagnostics.Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(diagnostics.Path, "is_file", lambda self: True)
    monkeypatch.setattr(diagnostics, "is_sensitive_path", lambda p: False)

    assert diagnostics._read_log_tail(fifo, 4096) is None
    out = diagnostics.read_kiro_cli_logs()
    assert "No kiro-cli protocol logs found" in out


@requires_symlinks
@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason=(
        "O_NOFOLLOW is POSIX-only; on Windows the open would follow the link "
        "and _read_log_tail would return the target's bytes, so the None result "
        "this asserts is a POSIX guarantee. The Windows symlink defense is the "
        "caller's pre-open is_symlink check, exercised by "
        "test_symlinked_source_is_not_followed."
    ),
)
def test_read_log_tail_refuses_a_symlink_on_posix(tmp_path):
    """_read_log_tail: None for a symlinked final component (O_NOFOLLOW ELOOP)."""
    target = tmp_path / "target.log"
    target.write_text("SECRETviaSymlink\n")
    link = tmp_path / "link.log"
    link.symlink_to(target)
    assert diagnostics._read_log_tail(link, 4096) is None


def test_read_log_tail_refuses_when_descriptor_identity_differs(tmp_path):
    """A descriptor that is not the checked file is refused, flag or no flag.

    This is the Windows defense: there is no ``O_NOFOLLOW`` there to refuse a
    junction at open time, and a reparse point is not reported by ``is_symlink``,
    so a swap landing after the pre-open check would satisfy ``S_ISREG`` and its
    bytes would reach the caller. ``lstat`` before the open and ``fstat`` after
    must name the same file.

    Simulated portably by making ``lstat`` describe a DIFFERENT file than the one
    opened, which is exactly what a followed link or a mid-flight swap produces.
    Restored in a ``finally`` rather than by ``monkeypatch``, because
    ``diagnostics.os`` IS the ``os`` module: the stub is process-wide, and pytest's
    own fixture cleanup would run against it before monkeypatch undid it.
    """
    real = tmp_path / "mcp.log"
    real.write_text("SWAPPEDtargetCONTENT4242\n")
    decoy = tmp_path / "decoy.log"
    decoy.write_text("decoy\n")
    decoy_stat = os.lstat(decoy)

    real_lstat = os.lstat
    try:
        os.lstat = lambda p, **kw: decoy_stat  # type: ignore[assignment]
        result = diagnostics._read_log_tail(real, 4096)
    finally:
        os.lstat = real_lstat  # type: ignore[assignment]

    assert result is None


def test_read_log_tail_reads_when_identity_matches(tmp_path):
    """The identity check must not reject the ordinary case it guards."""
    regular = tmp_path / "plain.log"
    regular.write_text("identity matches\n")
    assert diagnostics._read_log_tail(regular, 4096) == "identity matches\n"


def test_read_log_tail_bounds_a_growing_file(tmp_path):
    """The read stops at max_bytes even when the file never reaches EOF.

    ``size`` is a snapshot taken at ``fstat``. A writer appending faster than the
    loop drains means EOF never arrives, so a loop bounded only by EOF grows
    until the process is OOM-killed -- and the world-writable log directory is
    where a local writer can do exactly that. ``max_bytes`` has to bound the
    non-truncating branch too, not only the truncating one.

    Simulated with an ``os.read`` that never returns empty, so the ONLY thing that
    can end the loop is the byte budget. Without it this test hangs or dies on
    memory rather than failing.

    Restored in a ``finally`` rather than by ``monkeypatch``: ``diagnostics.os``
    IS the ``os`` module, so this replacement is process-wide, and monkeypatch
    undoes it at teardown -- after pytest's own fixture cleanup has already tried
    to use the stubbed ``os.read``.
    """
    log = tmp_path / "mcp.log"
    log.write_text("seed\n")
    cap = 8192
    real_read = os.read
    try:
        os.read = lambda fd, n: b"A" * n  # type: ignore[assignment]
        out = diagnostics._read_log_tail(log, cap)
    finally:
        os.read = real_read  # type: ignore[assignment]

    assert out is not None
    assert len(out) <= cap, "the read must stop at max_bytes on a file with no EOF"


def test_hidden_character_split_credential_does_not_survive_the_transport(tmp_path, monkeypatch):
    """A credential split by an invisible must not be rejoined downstream.

    ``redact_credentials`` matches the literal shape of a secret, so an invisible
    planted inside one defeats it: ``AKIA<ZWSP>IOSFODNN7EXAMPLE`` matches no
    pattern. The danger is what happens NEXT. Every MCP response leaves through
    ``validation.build_tool_response`` -> ``sanitize_response`` ->
    ``sanitize_string`` -> ``strip_hidden_unicode``, which removes the invisible
    and REJOINS the credential -- after redaction has already been asked about it
    and declined. Stripping late is worse than not stripping, and
    ``strip_hidden_unicode`` documents that it is meant to run BEFORE
    ``redact_credentials`` for exactly this reason.

    So this asserts the property end to end, through the real transport call, not
    just that `_scrub` returned something.
    """
    from kiro_crew import validation

    secret = "AKIAIOSFODNN7EXAMPLE"
    split = "AKIA\u200bIOSFODNN7EXAMPLE"
    _source(monkeypatch, tmp_path, f"2026-09-06T16:00:00 request key={split}\n")

    out = diagnostics.read_kiro_cli_logs()
    delivered = validation.sanitize_response(out)

    assert secret not in out, "the reader returned a credential a later strip would rejoin"
    assert secret not in delivered, "the transport rejoined the credential after redaction"


def test_scrub_strips_hidden_characters_before_redacting(tmp_path):
    """Unit form of the ordering, plus the control that makes it non-vacuous.

    The control matters: it shows `redact_credentials` DOES catch this credential
    when it is intact, so the split form passing through is attributable to the
    invisible rather than to the pattern never having covered the shape.
    """
    from kiro_crew import validation

    secret = "AKIAIOSFODNN7EXAMPLE"

    intact, n_intact = diagnostics._scrub(f"key={secret}\n")
    assert n_intact >= 1 and secret not in intact, "control: the intact form must be redacted"

    split, n_split = diagnostics._scrub("key=AKIA\u200bIOSFODNN7EXAMPLE\n")
    assert secret not in split, "the split form must not survive _scrub"
    assert secret not in validation.sanitize_response(split), "and must not be rejoinable"


def test_a_frame_bearing_source_is_refused_whole(tmp_path, monkeypatch):
    """A source recording protocol FRAMES is refused, not filtered.

    The scope argument holds only while mcp.log / lsp.log record protocol traffic
    rather than frame bodies. A `tools/call` frame carries conversation-derived
    arguments from every session sharing the host file, so if kiro-cli starts
    logging bodies the source stops being readable here. This asserts the refusal
    is WHOLE and VISIBLE: none of the payload comes back, and the output says the
    source was refused rather than looking like an empty log.
    """
    payload = "SESSION-B-TOOL-ARGUMENT-PROSE"
    _source(
        monkeypatch,
        tmp_path,
        "2026-09-06T16:00:00 sending\n"
        '{"jsonrpc": "2.0", "method": "tools/call", "params": {"q": "' + payload + '"}}\n',
    )

    out = diagnostics.read_kiro_cli_logs()

    assert payload not in out, "a frame body reached the caller"
    assert "REFUSED" in out, "the refusal must be visible, not a silent skip"
    assert "No kiro-cli protocol logs found" not in out, "refusal must not read as absence"


def test_a_real_shaped_log_is_not_refused(tmp_path, monkeypatch):
    """The tripwire must not fire on the log shape kiro-cli actually writes.

    Modelled on the observed lsp.log on kiro-cli 2.21.1: single-line
    `<timestamp> ERROR <module>: <message>` records. Includes the two shapes a
    length-based rule would get wrong -- a long legitimate line, and prose that
    merely mentions the protocol -- since the tripwire keys on the serialized
    JSON-RPC key instead of on length.
    """
    _source(
        monkeypatch,
        tmp_path,
        "2026-09-06T16:00:00 ERROR code_agent_sdk::sdk::workspace_manager: 1: "
        "Failed to start LSP server for " + ("/very/deep/path" * 12) + "\n"
        "2026-09-06T16:00:01 ERROR transport: failed to parse jsonrpc reply\n",
    )

    out = diagnostics.read_kiro_cli_logs()

    assert "REFUSED" not in out, "the tripwire fired on an honest log record"
    assert "Failed to start LSP server" in out
    assert "failed to parse jsonrpc reply" in out, "prose mentioning the protocol is not a frame"


def test_truncation_cannot_bypass_the_frame_refusal(tmp_path, monkeypatch):
    """An oversized MULTILINE frame must not smuggle its tail past the tripwire.

    `_looks_like_protocol_frames` keys on a frame's envelope tokens, and in a
    pretty-printed frame those sit at the TOP. The byte cut runs from the FRONT,
    so an oversized multiline frame can have its envelope fall outside the window
    while its trailing ARGUMENT lines survive: the marker then finds nothing to
    match and returns lines carrying another session's conversation content.

    `_begin_at_event_boundary` closes it by discarding leading lines of a cut
    window until a timestamped record starts one, so orphaned frame lines never
    reach the caller. Mutation-verified: without that call this fixture leaks
    `payload`.
    """
    payload = "SESSION-B-PRIVATE-ARGUMENT-TEXT"
    frame = (
        '{\n  "jsonrpc": "2.0",\n  "method": "tools/call",\n  "params": {\n'
        '    "name": "search",\n'
        + "".join(f'    "pad{i}": "{"x" * 80}",\n' for i in range(200))
        + f'    "query": "{payload}"\n  }}\n}}\n'
    )
    _source(monkeypatch, tmp_path, frame)
    monkeypatch.setattr(diagnostics, "_MAX_LOG_READ_BYTES", 4096)

    out = diagnostics.read_kiro_cli_logs()

    assert payload not in out, "a truncated frame's arguments reached the caller"


def test_a_cut_window_keeps_records_from_the_first_event(tmp_path, monkeypatch):
    """The boundary trim drops the orphan and keeps every whole record after it.

    Guard against over-trimming: only the leading partial record goes, and the
    truncation note stays so the caller still knows the tail was cut.
    """
    body = "  ORPHANCONTINUATION belonging to a record whose start was cut\n" + "".join(
        f"2026-09-06T16:00:00 KEPTRECORD{i:04d} {'y' * 60}\n" for i in range(200)
    )
    _source(monkeypatch, tmp_path, body)
    monkeypatch.setattr(diagnostics, "_MAX_LOG_READ_BYTES", 4096)

    out = diagnostics.read_kiro_cli_logs()

    assert "truncated" in out, "the cut must still be announced"
    assert "ORPHANCONTINUATION" not in out
    assert "KEPTRECORD0199" in out, "whole records after the boundary must survive"


def test_an_untruncated_window_is_not_trimmed(tmp_path, monkeypatch):
    """Nothing was cut, so the first line is real output and must be kept.

    Trimming an untruncated window would discard genuine content for no safety
    gain: a frame in a file small enough to be read whole still carries its
    envelope, so the marker catches it.
    """
    _source(monkeypatch, tmp_path, "  leading continuation\n2026-09-06T16:00:00 record\n")

    out = diagnostics.read_kiro_cli_logs()

    assert "truncated" not in out
    assert "leading continuation" in out
    assert "record" in out


def test_a_numeric_frame_line_is_not_a_record_boundary(tmp_path, monkeypatch):
    """A bare-number JSON line must not pass as the start of a log record.

    `_begin_at_event_boundary` trims a cut window forward to the first line that
    STARTS a record. A leading-digit test is too loose for that: a serialized frame
    can carry a JSON array of bare numbers, so `      1000000,` begins with a digit
    and would end the trim INSIDE the frame, returning the argument text after it.
    The predicate matches an ISO date followed by `T` or a space, which no JSON
    number can satisfy.
    """
    payload = "SESSION-B-ARGUMENT-AFTER-NUMERIC-LINE"
    frame = (
        '{\n  "jsonrpc": "2.0",\n  "method": "tools/call",\n  "params": {\n'
        + "".join(f'    "pad{i}": "{"x" * 80}",\n' for i in range(200))
        + '    "ids": [\n'
        + "".join(f"      {1000000 + i},\n" for i in range(30))
        + "    ],\n"
        + f'    "query": "{payload}"\n  }}\n}}\n'
    )
    _source(monkeypatch, tmp_path, frame)
    monkeypatch.setattr(diagnostics, "_MAX_LOG_READ_BYTES", 3000)

    out = diagnostics.read_kiro_cli_logs()

    assert payload not in out, "a numeric JSON line was treated as a record boundary"


def test_starts_a_record_accepts_real_records_and_rejects_frame_lines():
    """The predicate's two directions, stated as a unit test.

    Both ISO variants the module's `since` semantics describe are records; a JSON
    number, a quoted key and an indented continuation are not.
    """
    assert diagnostics._starts_a_record("2026-09-06T16:00:00.123Z ERROR boot failed\n")
    assert diagnostics._starts_a_record("2026-09-06 16:00:00 ERROR boot failed\n")
    assert not diagnostics._starts_a_record("      1000000,\n")
    assert not diagnostics._starts_a_record("      1234-5,\n")
    assert not diagnostics._starts_a_record('    "query": "text"\n')
    assert not diagnostics._starts_a_record("  continuation of an event\n")


def test_session_transcripts_are_not_read(tmp_path, monkeypatch):
    """Cross-session conversation content is deliberately out of scope.

    Session transcripts (`sessions/cli/<sid>.jsonl`) are shared across every
    gateway session, including incognito/temporary ones, and `_scrub` is a
    credential pass that does not narrow conversation prose — so returning the
    globally-newest ones would disclose another session's private conversation.
    The tool stays scoped to the mcp/lsp PROTOCOL logs: with no protocol log
    present it reports none found rather than falling back to transcripts.
    """
    # A populated sessions dir must NOT be a source.
    sessions = tmp_path / "sessions" / "cli"
    sessions.mkdir(parents=True)
    (sessions / "abc.jsonl").write_text("2026-09-06T16:00:00 private conversation\n")
    _no_sources(monkeypatch)

    # There is no session-log reader to stub — the feature was removed.
    assert not hasattr(diagnostics, "_kiro_cli_session_logs")
    out = diagnostics.read_kiro_cli_logs()
    assert "private conversation" not in out
    assert "No kiro-cli protocol logs found" in out


# ── MCP tool wrapper ─────────────────────────────────────────────────────────


def _stub_sel(monkeypatch):
    from unittest.mock import MagicMock

    sel = MagicMock()
    monkeypatch.setattr("kiro_crew.mcp_core.sel", lambda: sel)
    monkeypatch.setattr("kiro_crew.mcp_core._resolve_session_key", lambda: "sk")
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.private_memory_boundaries_active", lambda: False
    )
    return sel


@pytest.mark.parametrize("private_boundaries", [False, True])
def test_tool_global_scope_keeps_redacted_logs_and_captured_audit_identity(
    tmp_path, monkeypatch, private_boundaries
):
    from unittest.mock import Mock

    sel = _stub_sel(monkeypatch)
    _source(monkeypatch, tmp_path, _LOG)
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.private_memory_boundaries_active", lambda: private_boundaries
    )
    strict = Mock(return_value=("dashboard:global", ""))
    scope = Mock(return_value="")
    monkeypatch.setattr(logs_tool.mcp_core, "require_strict_session_key", strict)
    monkeypatch.setattr("kiro_crew.member_memory_auth.mcp_memory_scope", scope)

    result = logs_tool.kiro_cli_logs("kiro_cli_logs", {"tail": 10})

    assert "a perfectly normal log line" in result
    assert all(secret not in result for secret in _SECRETS)
    audit = sel.log_tool_invocation.call_args.kwargs
    assert audit["outcome"] == "success"
    assert audit["session_key"] == ("dashboard:global" if private_boundaries else "sk")
    if private_boundaries:
        strict.assert_called_once()
        scope.assert_called_once_with("dashboard:global")
    else:
        strict.assert_not_called()
        scope.assert_not_called()


@pytest.mark.parametrize("identity", ["private", "missing", "invalid", "unreadable"])
def test_tool_refuses_shared_logs_before_reading_for_private_or_unverified_caller(
    monkeypatch, identity
):
    from unittest.mock import Mock

    sel = _stub_sel(monkeypatch)
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.private_memory_boundaries_active", lambda: True
    )
    session_key = "" if identity == "missing" else "dashboard:member"
    strict = Mock(
        return_value=(session_key, "Error: identity unavailable" if not session_key else "")
    )
    scope = Mock(return_value="member-store")
    if identity == "invalid":
        scope.side_effect = ValueError("invalid protected proof")
    elif identity == "unreadable":
        scope.side_effect = OSError("protected binding cannot be read")
    reader = Mock(return_value="ANOTHER SESSION'S ORDINARY LOG PROSE")
    lenient = Mock(return_value="dashboard:forged-global")
    monkeypatch.setattr(logs_tool.mcp_core, "require_strict_session_key", strict)
    monkeypatch.setattr(logs_tool.mcp_core, "_resolve_session_key", lenient)
    monkeypatch.setattr("kiro_crew.member_memory_auth.mcp_memory_scope", scope)
    monkeypatch.setattr(logs_tool.diagnostics, "read_kiro_cli_logs", reader)

    result = logs_tool.kiro_cli_logs("kiro_cli_logs", {"tail": 10})

    assert result.startswith("Error:")
    assert "ANOTHER SESSION" not in result
    reader.assert_not_called()
    lenient.assert_not_called()
    if session_key:
        scope.assert_called_once_with(session_key)
    else:
        scope.assert_not_called()
    sel.log_tool_invocation.assert_called_once_with(
        session_key=session_key,
        source="mcp",
        tool_name="kiro_cli_logs",
        tool_kind="read",
        outcome="denied_memory_scope",
    )


def test_tool_denial_audit_failure_cannot_allow_shared_log_read(monkeypatch):
    from unittest.mock import Mock

    sel = _stub_sel(monkeypatch)
    sel.log_tool_invocation.side_effect = OSError("audit unavailable")
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.private_memory_boundaries_active", lambda: True
    )
    monkeypatch.setattr(
        logs_tool.mcp_core, "require_strict_session_key", lambda *_args: ("dashboard:member", "")
    )
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.mcp_memory_scope", lambda _key: "member-store"
    )
    reader = Mock()
    monkeypatch.setattr(logs_tool.diagnostics, "read_kiro_cli_logs", reader)

    assert logs_tool.kiro_cli_logs("kiro_cli_logs", {}).startswith("Error:")
    reader.assert_not_called()
    sel.log_tool_invocation.assert_called_once()


def test_tool_returns_reader_output_and_audits(tmp_path, monkeypatch):
    sel = _stub_sel(monkeypatch)
    monkeypatch.setattr(
        logs_tool.diagnostics, "read_kiro_cli_logs", lambda **kw: "REDACTED LOGS OK"
    )

    result = logs_tool.kiro_cli_logs("kiro_cli_logs", {"tail": 10})

    assert result == "REDACTED LOGS OK"
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "success"
    assert sel.log_tool_invocation.call_args.kwargs["tool_kind"] == "read"


def test_tool_error_is_prefixed_and_audited(monkeypatch):
    sel = _stub_sel(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(logs_tool.diagnostics, "read_kiro_cli_logs", _boom)

    result = logs_tool.kiro_cli_logs("kiro_cli_logs", {})

    assert result.startswith("Error:")
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "error"


def test_tool_rejects_out_of_range_tail(monkeypatch):
    _stub_sel(monkeypatch)
    # Schema bound is 1..100000; a validation error surfaces as a string, not a raise.
    from kiro_crew.validation import ValidationError

    with pytest.raises(ValidationError):
        logs_tool.kiro_cli_logs("kiro_cli_logs", {"tail": 0})


def test_tool_descriptor_does_not_advertise_the_chat_log(monkeypatch):
    """The advertisement must match the scope: no chat log, no transcripts.

    The model picks a tool off this description, so a description promising
    kiro-chat.log would send the agent looking for conversation content the
    reader deliberately does not return.
    """
    (descriptor,) = logs_tool.schemas()
    description = descriptor["description"]
    assert "mcp/lsp" in description
    assert "kiro-chat.log" in description, "the exclusion should be stated, not silent"
    assert "chat/mcp/lsp" not in description, "stale claim: the chat log is not read"
