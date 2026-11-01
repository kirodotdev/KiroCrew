"""Call-site guard: no NEW direct Slack ``.post_message(`` call goes unrecorded.

``render_for_slack`` / ``render_one_for_slack`` (``slack/format.py``) are the safe
way to put agent text on Slack, but nothing makes a send path use them: a path
that posts raw text skips redaction, mrkdwn conversion and the OPTIONS lifecycle
and still passes every other gate (``test_slack_render_pipeline.py`` polices the
conversion primitive, not the send). Moving every sender onto one shared seam
changes how existing messages look, so it is tracked as a design; until then this
guard stops the count of direct sends from growing.

How it works: an AST scan counts every ``<anything>.post_message(...)`` call in
each ``kiro_crew`` module and compares the count with :data:`BASELINE`, which
records today's call sites per module with a one-line reason.

- A module that is not in the baseline, or that has more calls than its row
  says, fails: a new direct send path has to be looked at, not slip in.
- A module that has FEWER calls than its row also fails, so the baseline only
  ever shrinks and a freed slot cannot later hide a new call.

When this fails on your change, prefer routing the text through
``render_for_slack`` (a parts list) or ``render_one_for_slack`` (one string). If
the direct call is right -- the text is already rendered, or it is a fixed
notice that is not agent output -- add or raise the module's row and say why in
its reason. Removed a call? Lower the row, or delete it at zero.

Scope, stated so it is not over-read: the scan counts attribute CALLS named
``post_message``. A bound-method alias (``send = client.post_message``) or a
``getattr`` lookup is not counted, and neither are other Slack send methods
(``post_blocks``, ``update_message``). Test code bundled inside an app
(``.../tests/...``) is skipped: it calls fakes, not Slack. The guard changes no
runtime behaviour.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path, PurePosixPath

import pytest
from source_corpus import parsed_candidates, src_root

_METHOD = "post_message"

# ``<module path relative to the kiro_crew package>``: ``(direct calls, reason)``.
# Keep rows sorted by path. A reason says why the module posts directly today.
BASELINE: dict[str, tuple[int, str]] = {
    "apps/builtins/ops_mission_control/backend/slack_out.py": (
        1,
        "incident detail note; the app screens and clips the text itself first",
    ),
    "dashboard/chat_compaction_notice.py": (1, "fixed compaction notice into the linked thread"),
    "dashboard/chat_slack.py": (
        6,
        "link, unlink and disconnect notices, and the backfill send helper",
    ),
    "dashboard/chat_turn/recipient.py": (1, "notice into the session's linked thread"),
    "dashboard/chat_turn/turn_dispatch_gates.py": (
        2,
        "skips the renderer on purpose: the prompt echo is a quoted, escaped user "
        "prompt (mirror_prompt_echo) and the other post is the fixed Thinking placeholder",
    ),
    "dashboard/chat_turn/turn_spec_hooks.py": (
        1,
        "posts parts already produced by render_for_slack",
    ),
    "dashboard/handlers/hooks.py": (1, "webhook agent result sent to the owner DM"),
    "dashboard/messaging_api/proactive_send.py": (
        2,
        "send_message tool: text plus a plain fallback when Slack rejects the button form",
    ),
    "dashboard/server_runtime/owner_notices.py": (1, "owner DM notice"),
    "shared_crew/turn_gate.py": (1, "fixed capacity note for a message fair admission refused"),
    "slack/allowlist.py": (1, "access-request DM to an allowlist approver"),
    "slack/events.py": (5, "!stop and !restart notices and the not-authorized refusal"),
    "slack/files.py": (1, "notes for images Slack refused to upload"),
    "slack/gateway_runtime/cron_delivery.py": (
        1,
        "overflow parts of a cron result after its first message",
    ),
    "slack/gateway_runtime/cron_failure.py": (
        1,
        "cron failure alert to the job channel or owner DM",
    ),
    "slack/gateway_runtime/nudge_channel_fire.py": (
        1,
        "posts parts already produced by render_for_slack",
    ),
    "slack/gateway_runtime/result_egress.py": (5, "unattended result parts and heartbeat delivery"),
    "slack/gateway_runtime/service_boot.py": (1, "owner DM with a boot notice"),
    "slack/gateway_runtime/subagent_injection.py": (
        1,
        "posts parts already produced by render_for_slack",
    ),
    "slack/gateway_runtime/subagent_wiring.py": (1, "owner DM with a subagent notice"),
    "slack/handler_runtime/commands.py": (
        74,
        "slash and text command replies: fixed status and help text",
    ),
    "slack/handler_runtime/delivery.py": (
        5,
        "turn answer, its parts, and redaction and failure notices",
    ),
    "slack/handler_runtime/dispatch.py": (
        9,
        "command and hook replies, status and permission notices",
    ),
    "slack/handler_runtime/inbound.py": (2, "approval mode notice and the not-authorized refusal"),
    "slack/handler_runtime/stream.py": (
        5,
        "stream continuations, Thinking placeholders, delivery-debt notice",
    ),
    "slack/handler_runtime/turn.py": (2, "replay and turn-ceiling notices"),
    "slack/interactions.py": (3, "session thread header, DM resume header, replayed history lines"),
    "slack/interactions_approvals.py": (
        3,
        "approved or edited draft posted to the channel, and its redaction notice",
    ),
    "slack/interactions_modals.py": (
        1,
        "re-posts a forwarded message as the user's visible message",
    ),
    "slack/interactions_options.py": (
        1,
        "re-posts the chosen OPTIONS label as the user's visible message",
    ),
    "slack/interactions_sessions.py": (
        6,
        "stop and reset notices, resume label, new-session greeting",
    ),
    "slack/interactions_settings.py": (2, "allowlist approved and denied DMs"),
    "slack/renderer.py": (
        7,
        "the channel renderer: placeholders, answer parts, fallback and notices",
    ),
    "slack/transport.py": (1, "the Slack transport adapter: forwards content the caller rendered"),
    "slack/transport_dispatch.py": (4, "hook reply, stats, ping and transport error text"),
}


def _is_bundled_test(rel: str) -> bool:
    return "tests" in PurePosixPath(rel).parts[:-1]


def count_calls(tree: ast.AST) -> int:
    """How many ``<expr>.post_message(...)`` calls *tree* holds."""
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == _METHOD
    )


def count_tree(files: list[tuple[str, str]]) -> dict[str, int]:
    """Per-module call counts for ``(relative path, source)`` pairs."""
    out: dict[str, int] = collections.Counter()
    for rel, text in files:
        if _is_bundled_test(rel):
            continue
        n = count_calls(ast.parse(text))
        if n:
            out[rel] = n
    return dict(out)


def compare(counts: dict[str, int], baseline: dict[str, tuple[int, str]]) -> list[str]:
    """Every way *counts* disagrees with *baseline*, one line each."""
    problems: list[str] = []
    for rel in sorted(set(counts) | set(baseline)):
        have = counts.get(rel, 0)
        want = baseline.get(rel, (0, ""))[0]
        if have > want:
            what = "not in the baseline" if rel not in baseline else f"baseline says {want}"
            problems.append(f"  {rel}: {have} direct call(s), {what} -- a new direct send path")
        elif have < want:
            problems.append(
                f"  {rel}: {have} direct call(s), baseline says {want} -- lower the row"
            )
    return problems


def _repo_counts() -> dict[str, int]:
    root = src_root()
    files: list[tuple[str, str]] = []
    # A post_message attribute call cannot exist without the literal in the text,
    # so narrowing the corpus on it drops only non-matches.
    for path, text, _tree in parsed_candidates(require_all=(_METHOD,), skip_syntax_errors=False):
        files.append((Path(path).relative_to(root).as_posix(), text))
    return count_tree(files)


def test_no_new_direct_post_message_call_sites() -> None:
    problems = compare(_repo_counts(), BASELINE)
    if problems:
        raise AssertionError(
            "Direct Slack .post_message( calls disagree with BASELINE in "
            "test/test_slack_post_message_call_sites.py.\n\n"
            "Route agent text through render_for_slack() / render_one_for_slack(). "
            "If a direct call is right, add or raise the module's row with a "
            "one-line reason; if you removed calls, lower the row.\n\n" + "\n".join(problems)
        )


def test_every_baseline_row_has_a_count_and_a_reason() -> None:
    for rel, (count, reason) in BASELINE.items():
        assert count > 0, f"{rel}: a row at zero must be deleted"
        assert reason.strip(), f"{rel}: every row needs a one-line reason"


def test_baseline_rows_are_sorted() -> None:
    assert list(BASELINE) == sorted(BASELINE)


# ── Meta-tests: prove the guard both fires and stays quiet ────────────────────

_NEW_DIRECT_CALL = (
    "async def notify(client, channel, text):\n" "    await client.post_message(channel, text)\n"
)


def test_a_new_direct_call_in_an_unlisted_module_fails() -> None:
    counts = count_tree([("slack/new_sender.py", _NEW_DIRECT_CALL)])
    problems = compare(counts, {})
    assert len(problems) == 1
    assert "slack/new_sender.py" in problems[0] and "not in the baseline" in problems[0]


def test_an_extra_call_in_a_listed_module_fails() -> None:
    src = _NEW_DIRECT_CALL + "    await client.post_message(channel, 'again')\n"
    problems = compare(count_tree([("slack/old.py", src)]), {"slack/old.py": (1, "x")})
    assert len(problems) == 1 and "baseline says 1" in problems[0]


def test_a_removed_call_asks_to_lower_the_row() -> None:
    problems = compare(count_tree([("slack/old.py", _NEW_DIRECT_CALL)]), {"slack/old.py": (2, "x")})
    assert len(problems) == 1 and "lower the row" in problems[0]


def test_a_listed_call_passes() -> None:
    assert (
        compare(count_tree([("slack/old.py", _NEW_DIRECT_CALL)]), {"slack/old.py": (1, "x")}) == []
    )


@pytest.mark.parametrize(
    "src",
    [
        pytest.param("def f(post_message):\n    return post_message('c', 't')\n", id="bare_name"),
        pytest.param("def f(c):\n    return c.post_blocks('c', [])\n", id="other_method"),
        pytest.param("def f(c):\n    return c.post_message\n", id="attribute_not_called"),
        pytest.param("X = 'client.post_message(c, t)'\n", id="inside_a_string"),
    ],
)
def test_non_calls_are_not_counted(src: str) -> None:
    assert count_tree([("slack/x.py", src)]) == {}


def test_bundled_app_tests_are_skipped() -> None:
    rel = "apps/builtins/some_app/tests/test_x.py"
    assert count_tree([(rel, _NEW_DIRECT_CALL)]) == {}
