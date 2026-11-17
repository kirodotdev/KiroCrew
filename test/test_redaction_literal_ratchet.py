"""Ratchet: no new test asserts an AWS key ID literal is REMOVED from a value.

The structured redactor keeps AWS key IDs (``AKIA`` plus 16 characters) and the
AWS documentation example key visible as identifiers. A test that plants one and
asserts it is gone goes red or green with detector policy, not with the code it
names, so two PRs that are each green on their own base red ``main`` together.
testing-conventions.md holds the rule and its history: build a credential that
must vanish with ``kiro_crew.testing.synthetic_credentials``.

A site is ``<key> not in <value>`` or ``assertNotIn(<key>, ...)`` where ``<key>``
is a string literal holding an AWS key ID, or a name the same function (or the
module) binds to one. Asserting the key IS kept (``in``) stays allowed: that is
where such a literal belongs.

``_BASELINE`` holds the sites that predate this gate, as exact counts per file.
It only shrinks: a new site in any file fails, and a removed one must lower its
count in the same change. A key built by a call or a loop is not seen; the
shapes pinned here are the ones every recorded site uses.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterator

import pytest
from source_corpus import repo_root

#: One worker owns the whole-tree scan.
pytestmark = pytest.mark.xdist_group(name="tree_scan_redaction_literal_ratchet")

_AWS_KEY_ID = re.compile(r"AKIA[0-9A-Z]{16}")

_RULE = (
    "A test that asserts a credential is REMOVED builds it with "
    "kiro_crew.testing.synthetic_credentials (github_classic(), aws_secret(), ...), "
    "never an AWS key ID literal: the redactor keeps AWS key IDs, so the test "
    "tracks detector policy instead of the code under test. Rule: "
    "docs/system-specs/common/testing-conventions.md, 'A test that asserts a "
    "credential is REMOVED'."
)

#: Sites that predate this gate: test file (relative to ``test/``) -> count.
_BASELINE: dict[str, int] = {
    "eventlog/test_eventlog.py": 4,
    "test_acp_client_model_substitution.py": 2,
    "test_acp_client_tool_call_updates.py": 1,
    "test_acp_session_handle_capabilities.py": 1,
    "test_acp_structured_refusal.py": 1,
    "test_agent_hooks_both_shapes.py": 4,
    "test_ai_review_workflows_block_adjudication.py": 1,
    "test_autonudge_banner.py": 2,
    "test_autonudge_handlers_cov80.py": 1,
    "test_aws_control_backup_job.py": 1,
    "test_aws_control_storage.py": 1,
    "test_capability_agents_plugins.py": 1,
    "test_chat_title.py": 1,
    "test_connections_mint_flow.py": 8,
    "test_connections_mint_kas.py": 1,
    "test_connections_warm_mint_composition_contract.py": 2,
    "test_decisions_memory_recall.py": 1,
    "test_fargate_closed_forward_remedy.py": 1,
    "test_governance_distribution_redaction.py": 1,
    "test_handler_cron_list.py": 2,
    "test_imessage_renderer.py": 1,
    "test_job_sdk.py": 1,
    "test_job_sdk_invariants.py": 1,
    "test_mcp_cron_list_compact.py": 1,
    "test_mcp_cron_security.py": 1,
    "test_mcp_cron_security_scripts.py": 1,
    "test_mcp_oauth_banner.py": 4,
    "test_mcp_shared.py": 1,
    "test_members_eventlog_wiring.py": 4,
    "test_messaging_driver.py": 1,
    "test_remote_artifacts.py": 1,
    "test_review_fixes.py": 1,
    "test_sel.py": 1,
    "test_sel_edge_cases.py": 1,
    "test_session_directive.py": 1,
    "test_session_start_timeout_diagnostics.py": 1,
    "test_slack_gateway_coverage_nudge_routing.py": 1,
    "test_snapshot_text_detection.py": 1,
    "test_subagent_scale.py": 1,
    "test_transport_permission_floor.py": 5,
    "test_voice_reply.py": 1,
    "test_webex_dispatch_cards.py": 1,
    "tracing/test_tracing_turns.py": 1,
}

_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _literal(node: ast.expr) -> str | None:
    """The constant text of a string literal, f-string or ``+`` of literals."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _holds_key(node: ast.expr) -> bool:
    text = _literal(node)
    return text is not None and bool(_AWS_KEY_ID.search(text))


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node of *scope*, without descending into a nested function."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _key_names(scope: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if _holds_key(value):
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _removal_subjects(node: ast.AST) -> Iterator[ast.expr]:
    if isinstance(node, ast.Compare):
        subjects = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            if isinstance(op, ast.NotIn):
                yield subjects[index]
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "assertNotIn"
        and node.args
    ):
        yield node.args[0]


def _sites(tree: ast.Module) -> list[int]:
    """Line numbers where *tree* asserts an AWS key ID literal is absent."""
    module_names = _key_names(tree)
    lines: list[int] = []
    for scope in [tree, *(n for n in ast.walk(tree) if isinstance(n, _SCOPES[1:]))]:
        names = module_names | (_key_names(scope) if scope is not tree else set())
        for node in _own_nodes(scope):
            for subject in _removal_subjects(node):
                if _holds_key(subject) or (isinstance(subject, ast.Name) and subject.id in names):
                    lines.append(node.lineno)
    return sorted(lines)


#: Built at run time so this file carries no key-shaped literal of its own.
_KEY = "AKIA" + "Q" * 16


def _lines(source: str) -> list[int]:
    return _sites(ast.parse(source))


def test_the_detector_sees_every_removal_spelling() -> None:
    assert _lines(f'assert "{_KEY}" not in out\n') == [1]
    assert _lines(f'assert "key={_KEY}&x" not in out\n') == [1]
    assert _lines(f'assert f"{{h}}?t={_KEY}" not in out\n') == [1]
    assert _lines(f'assert "{_KEY[:8]}" + "{_KEY[8:]}" not in out\n') == [1]
    assert _lines(f'self.assertNotIn("{_KEY}", out)\n') == [1]
    assert _lines(f'def t():\n    secret = "{_KEY}"\n    assert secret not in out\n') == [3]
    assert _lines(f'SECRET: str = "{_KEY}"\ndef t():\n    assert SECRET not in out\n') == [3]


def test_the_detector_allows_a_kept_key_and_other_values() -> None:
    # Asserting the identifier SURVIVES is where the literal belongs.
    assert _lines(f'assert "{_KEY}" in out\n') == []
    assert _lines(f'self.assertIn("{_KEY}", out)\n') == []
    # Not key-shaped: too short, lower case, or a synthetic helper's value.
    assert _lines('assert "AKIA123" not in out\n') == []
    assert _lines(f'assert "{_KEY.lower()}" not in out\n') == []
    assert _lines("assert github_classic() not in out\n") == []
    # A name bound to a key in ANOTHER function is not this function's key.
    source = f'def a():\n    secret = "{_KEY}"\ndef b():\n    assert secret not in out\n'
    assert _lines(source) == []


def _tree_sites() -> Counter[str]:
    test_root = repo_root() / "test"
    counts: Counter[str] = Counter()
    for path in sorted(test_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "AKIA" not in text:
            continue  # every site spells the prefix
        found = len(_sites(ast.parse(text)))
        if found:
            counts[path.relative_to(test_root).as_posix()] = found
    return counts


def test_no_new_test_asserts_an_aws_key_id_literal_is_removed() -> None:
    counts = _tree_sites()
    grown = {rel: n for rel, n in counts.items() if n > _BASELINE.get(rel, 0)}
    assert not grown, f"{_RULE} New sites (file: count): {sorted(grown.items())}"
    shrunk = {rel: counts.get(rel, 0) for rel, n in _BASELINE.items() if counts.get(rel, 0) < n}
    assert not shrunk, (
        "a file in _BASELINE holds fewer AWS-key-removal assertions than recorded. "
        "Lower its count (delete the row at zero) so the baseline only shrinks. "
        f"Now: {sorted(shrunk.items())}"
    )


def test_the_baseline_has_no_empty_rows() -> None:
    assert all(count > 0 for count in _BASELINE.values())
