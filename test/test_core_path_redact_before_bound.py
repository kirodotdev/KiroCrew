"""Redact-before-bound at the core-path slice sites (issue #7390).

A bounded slice applied to text BEFORE it is passed to a redactor can cut a
credential at the slice boundary into fragments no redaction regex matches, so
the raw fragment escapes redaction and reaches the sink (log line, error
payload, external display surface). The fix is the established mechanical
reorder: feed the redactor the FULL text and bound AFTER —
``security.redact_and_truncate(text, n)`` for head cuts (identical composition
to ``redact(text)[:n]``), or ``redact(text)[-n:]`` for tail cuts.

Two layers of pinning, mirroring ``test_theme_clone_stderr_redact_before_bound``:

- Behavioral straddle tests on the pure display helpers: the secret is laid out
  so the bound falls INSIDE it, so a raw slice AND a slice-then-redact reorder
  both go red.
- A structural AST scan over every module the issue names: a bounded char slice
  may never appear INSIDE a redact call's argument expression. Deliberate
  limits: a slice of a renamed local later fed to a redactor is beyond this
  scan — the behavioral tests carry that shape. Constant bounds under 10 are
  ignored to keep whole-item idioms (``splitlines()[-1:]``, ``raw[:7]`` hex)
  out of scope: it is the wide CHAR slice that severs a secret.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kiro_crew.acp import _dispatch as acp_dispatch
from kiro_crew.dashboard.handlers import artifacts as artifacts_handlers
from kiro_crew.security import redact_credentials
from kiro_crew.telegram import renderer as tg_renderer

_REPO_ROOT = Path(__file__).resolve().parents[1]

# A fake AWS access key id: matches (?:AKIA|ASIA)[A-Z0-9]{16}, asserted absent.
_TOKEN = "AKIA" + "STRADDLE0123456A"


class TestTelegramOptionLabelStraddle:
    """A credential straddling the 64-char button-label bound must not leak."""

    def test_label_bound_falls_inside_the_credential(self) -> None:
        bound = 64
        prefix = "x" * (bound - 9)
        label = f"{prefix}{_TOKEN} tail"
        # Premise guard: the bound must actually cut into the token, or this
        # test silently stops pinning the invariant.
        start = label.index(_TOKEN)
        assert start < bound < start + len(_TOKEN)

        keyboard = tg_renderer.build_inline_keyboard([label], "telegram:test")
        assert keyboard is not None
        texts = [btn["text"] for row in keyboard["inline_keyboard"] for btn in row]
        joined = "\n".join(texts)
        assert _TOKEN not in joined
        # The exact fragment a bound-before-redact implementation leaks —
        # everything of the token left of the bound — must be absent too.
        leaked_prefix = _TOKEN[: bound - start]
        assert leaked_prefix and leaked_prefix not in joined


class TestArtifactSnippetStraddle:
    """The gallery snippet path must redact the FULL text before any bound."""

    def test_prefix_snippet_shrink_slide_does_not_leak(self) -> None:
        """Straddle + shrink layout for ``_snippet_from``.

        The token straddles the old 3x pre-redaction window edge, and a long
        labelled secret BEFORE it shrinks under redaction — so under a
        windowed (bound-before-redact) implementation the unmatched token
        fragment slides left into the final ``_SNIPPET_MAX_LEN`` cut. Only
        full-text redact-then-bound scrubs it.
        """
        max_len = artifacts_handlers._SNIPPET_MAX_LEN
        window = max_len * 3
        secret_label = "SecretAccessKey=" + "A1b2" * 100
        pad = "x" * (window - 10 - len(secret_label) - 1)
        stripped = f"{secret_label} {pad}{_TOKEN} tail text"

        # Premise guards: the token must straddle the old window edge …
        start = stripped.index(_TOKEN)
        assert start < window < start + len(_TOKEN)
        # … and redacting the labelled secret must shrink the text enough that
        # the token fragment would land INSIDE the final snippet under a
        # windowed implementation (otherwise absence proves nothing).
        shrink = len(secret_label) - len(redact_credentials(secret_label)[0])
        leaked_len = window - start
        assert start - shrink + leaked_len <= max_len

        snippet = artifacts_handlers._snippet_from(stripped)
        assert len(snippet) <= max_len
        assert _TOKEN not in snippet
        leaked_prefix = _TOKEN[:leaked_len]
        assert leaked_prefix and leaked_prefix not in snippet
        assert "[REDACTED" in snippet

    def test_context_snippet_line_bound_does_not_leak(self) -> None:
        """A credential straddling the per-line context cut must not leak."""
        line_len = artifacts_handlers._CONTEXT_LINE_LEN
        prefix = "needle " + "y" * (line_len - 10 - len("needle "))
        content = f"before\n{prefix}{_TOKEN} after-token\nafter"
        # Premise guard: the line cut must fall inside the token.
        line = f"{prefix}{_TOKEN} after-token"
        start = line.index(_TOKEN)
        assert start < line_len < start + len(_TOKEN)

        out = artifacts_handlers._context_snippet(content, "needle")
        assert "needle" in out
        assert _TOKEN not in out
        leaked_prefix = _TOKEN[: line_len - start]
        assert leaked_prefix and leaked_prefix not in out


class TestToolResultPartStraddle:
    """Tool-output parts must be redacted as ONE combined text, bounded after.

    Two failure shapes are pinned: a credential straddling what used to be a
    per-part cut (a bound applied before redaction severs it into unmatchable
    fragments), and a multi-line PEM key whose header and footer arrive in
    DIFFERENT parts (per-part redaction can never see it whole — only the
    joined text can)."""

    def test_part_bound_falls_inside_the_credential(self) -> None:
        part_bound = 4000
        prefix = "x" * (part_bound - 9)
        part = f"{prefix}{_TOKEN} tail"
        start = part.index(_TOKEN)
        # Premise guard: the token must straddle the former part cut.
        assert start < part_bound < start + len(_TOKEN)

        update = {
            "toolCallId": "t1",
            "content": [{"content": {"type": "text", "text": part}}],
        }
        event = acp_dispatch._build_tool_result_event(update)
        assert event is not None
        assert _TOKEN not in event.tool_output
        leaked_prefix = _TOKEN[: part_bound - start]
        assert leaked_prefix and leaked_prefix not in event.tool_output
        assert "[REDACTED" in event.tool_output

    def test_pem_key_split_across_parts_is_redacted(self) -> None:
        """A PEM header in one part and its footer in another: the multi-line
        pattern only matches the JOINED text, so a per-part redaction pass
        leaves the key body raw. Pins join-level redaction."""
        key_body = "FAKEKEYMATERIALLINE1/abcdefghijklmnop"  # fake, asserted absent
        part1 = f"log line\n-----BEGIN RSA PRIVATE KEY-----\n{key_body}"
        part2 = "MORELINES==\n-----END RSA PRIVATE KEY-----\ntrailing"
        update = {
            "toolCallId": "t2",
            "content": [
                {"content": {"type": "text", "text": part1}},
                {"content": {"type": "text", "text": part2}},
            ],
        }
        event = acp_dispatch._build_tool_result_event(update)
        assert event is not None
        assert key_body not in event.tool_output
        assert "BEGIN RSA PRIVATE KEY" not in event.tool_output
        assert "[REDACTED" in event.tool_output


# Every module the issue names, relative to the repo root. The scan pins the
# WHOLE module, not just the fixed lines, so a new slice-before-redact call
# shape in these files goes red immediately.
_SCANNED_MODULES = [
    "src/kiro_crew/channel.py",
    "src/kiro_crew/dashboard/chat_runner.py",
    "src/kiro_crew/acp/_dispatch.py",
    # Its near-twin: AcpClient carries a hand-copied tool-result extractor with
    # the same shape, and its absence from this list is why the bound-then-redact
    # ordering survived there (issue #7799). NOTE the scan would not have caught
    # that instance -- the slice was a separate statement, not an argument to the
    # redactor -- so the behavioural pin for it lives in
    # test_acp_client.py::test_credential_straddling_the_bound_is_still_redacted.
    "src/kiro_crew/acp/client.py",
    "src/kiro_crew/dashboard/handlers/artifacts.py",
    "src/kiro_crew/dashboard/handlers/discover.py",
    "src/kiro_crew/mcp_tools/control.py",
    "src/kiro_crew/mcp_tools/spawn.py",
    "src/kiro_crew/vector_memory.py",
    "src/kiro_crew/knowledge/agent_fetch.py",
    "src/kiro_crew/slack/gateway.py",
    "src/kiro_crew/telegram/renderer.py",
    "src/kiro_crew/subagent_manager/continuation.py",
    "src/kiro_crew/apps/builtins/auto_research/handlers.py",
]


class TestNoSliceInsideRedactCallInNamedModules:
    """Structural class pin over every module issue #7390 names."""

    def test_no_bounded_slice_inside_a_redact_call(self) -> None:
        offenders: list[str] = []
        for rel in _SCANNED_MODULES:
            offenders += _find_slice_inside_redact_call(_REPO_ROOT / rel)
        assert offenders == []


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _constant_bound(sl: ast.Slice) -> int | None:
    """The largest integer literal edge of a slice, or None when non-constant."""
    bound = None
    for edge in (sl.lower, sl.upper):
        if isinstance(edge, ast.Constant) and isinstance(edge.value, int):
            bound = max(bound or 0, edge.value)
        elif (
            isinstance(edge, ast.UnaryOp)
            and isinstance(edge.op, ast.USub)
            and isinstance(edge.operand, ast.Constant)
            and isinstance(edge.operand.value, int)
        ):
            bound = max(bound or 0, edge.operand.value)
    return bound


def _find_slice_inside_redact_call(path: Path) -> list[str]:
    """Char slices appearing INSIDE a redact call's argument expression.

    Slicing the redact call's RESULT is the sanctioned composition (redaction
    ran over the full text; the cut can at worst split a redaction marker), so
    only slices nested in the call's own arguments are offenders. A constant
    bound under 10 is a whole-item idiom, not a secret-severing cut; a
    non-constant bound cannot be sized, so it is flagged conservatively.
    """
    offenders: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or "redact" not in _call_name(node).lower():
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            for sub in ast.walk(arg):
                if not isinstance(sub, ast.Subscript) or not isinstance(sub.slice, ast.Slice):
                    continue
                bound = _constant_bound(sub.slice)
                if bound is not None and bound < 10:
                    continue
                offenders.append(f"{path.name}:{sub.lineno}")
    return offenders
