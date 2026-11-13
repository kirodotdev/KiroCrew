"""The ``rm -rf /.*`` row reads past a plain ``rm`` of direct ``/tmp`` entries only.

Recorded false positives: ``rm -rf /tmp/<name> && mkdir -p /tmp/<name> && ...`` was
refused as "recursive force-deletion rooted at the filesystem root".
"""

from __future__ import annotations

import pytest

from kiro_crew.security import is_denied

ALLOWED = [
    "rm -rf /tmp/f2-base && mkdir -p /tmp/f2-base",
    "git fetch -q origin main && rm -rf /tmp/pwt-wt; git worktree add -q /tmp/pwt-wt",
    "uname -m; rm -rf /tmp/iioff 2>/dev/null; ls",
    "rm -rf /tmp/a /tmp/b",
    # kiro-cli's tool-call title, judged by the title tier
    "Running: rm -rf /tmp/f2-base && mkdir -p /tmp/f2-base",
]

DENIED = [
    "rm -rf /",
    "rm -rf /tmp/x; rm -rf /",
    "rm -rf /tmp/x /etc",
    "rm -rf /etc",
    "rm -rf /tmp",
    "rm -rf /tmp/",
    "rm -rf /tmp/*",
    "rm -rf /tmp/x/",
    "rm -rf /tmp/../etc",
    "rm -rf /tmp/./x",
    "rm -rf /tmp/$X",
    "rm -rf '/tmp/x' /usr",
    "sudo rm -rf /tmp/x",
    'bash -c "rm -rf /tmp/x; rm -rf /usr"',
    "rm -rf /tmp/x && echo rm -rf /usr | sh",
    "/bin/rm -rf /tmp/x /usr",
    # a folder on the way can be a symlink an earlier command plants
    "ln -s / /tmp/l; rm -rf /tmp/l/etc",
    "rm -rf /tmp/a/b",
    "rm -rf /var/tmp/b/c",
    # a substitution glued on an edge adds operands bash splits out of its output
    "rm -rf /tmp/x`printf ' /etc'`",
    "`printf 'r%s -rf /srv ' m`rm -rf /tmp/a",
    'B="$(rm -rf /tmp/x)$(echo /etc)"',
    # a redirection's substitution runs even though it is not an operand
    'rm -rf /tmp/a >"$(rm -rf /srv/project)" /tmp/b',
    "Running: rm -rf /",
    # a backtick after a blank still splices its output into the rm's argv
    "rm -rf /tmp/scratch `printf /srv/project`",
    "rm -rf /tmp/scratch $(printf /srv/project)",
]


def test_an_operator_authored_copy_of_the_row_keeps_reading_the_full_text() -> None:
    from kiro_crew.security.denied_rules import BUILTIN_DENIED_RULES, compute_effective_denied

    rows = [r for r in BUILTIN_DENIED_RULES if r.id == "local-destructive-rm-rf-root"]
    regexes = compute_effective_denied(list(BUILTIN_DENIED_RULES), [], True, [rows[0].pattern], ())
    assert is_denied("rm -rf /tmp/build", denied_regexes=regexes) is not None


def test_a_view_refusal_records_the_command_as_submitted(monkeypatch) -> None:
    from kiro_crew.security import deny_gate

    seen: "list[str]" = []
    monkeypatch.setattr(deny_gate, "_emit_deny_event", lambda tool, *a, **k: seen.append(tool))
    command = "rm -rf /tmp/x; rm -rf /etc"
    assert deny_gate.is_denied(command) is not None
    assert seen == [command]


@pytest.mark.parametrize("command", ALLOWED)
def test_scratch_rm_is_allowed(command: str) -> None:
    assert is_denied(command) is None


@pytest.mark.parametrize("command", DENIED)
def test_root_or_unproven_rm_stays_denied(command: str) -> None:
    assert is_denied(command) is not None
