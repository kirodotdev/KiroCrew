"""The environment-credential tier says WHICH check fired and WHERE.

The in-band notice quotes only the display title, which is cut to about 200
characters, so a match further into a long command is invisible there. The
refusal therefore carries the payload-free diagnostic line the structural floors
emit: the rule id, the component, and the span's offsets, never the matched bytes.
"""

from __future__ import annotations

import re

import pytest

from kiro_crew import security
from kiro_crew.security.denied_rules import (
    _ENV_CRED_DENIAL_REASON,
    _ENV_CRED_PATTERN_IDS,
    _ENV_CRED_PATTERNS,
    _ENV_CRED_SHARED_RULES,
    _check_env_credential_access,
    _deny_matcher,
)
from kiro_crew.security.diagnostics import REFUSAL_DIAGNOSTIC_PREFIX

_DIAG_RE = re.compile(
    r"^Refusal diagnostic: rule=(?P<rule>\S+) component=(?P<component>\S+) "
    r"span=(?P<start>\d+)\.\.(?P<end>\d+) shape=\S+$"
)

# A long, benign one-line prefix pushes the match well past the ~200-char title
# cut, which is the shape the issue reports: the notice's quoted command never
# reaches it. (The tier's ``.*`` does not cross a newline, so the prefix is one line.)
_PAD = 'python3 -c "import os, subprocess; ' + "x = 1; " * 30


def _diagnostic(reason: str) -> re.Match[str]:
    lines = reason.splitlines()
    assert lines[0] == _ENV_CRED_DENIAL_REASON, "line one must stay byte-identical"
    assert len(lines) == 2, lines
    hit = _DIAG_RE.match(lines[1])
    assert hit, lines[1]
    return hit


# (command, expected rule id, the token the span must land on)
_CASES = [
    ("declare -p AWS_SECRET_ACCESS_KEY", "env-cred-declare-print", "AWS_SECRET"),
    ("echo ${AWS_SESSION_TOKEN}", "env-cred-echo-expansion", "AWS_SESSION"),
    ("awk 'BEGIN{print ENVIRON[\"AWS_SECRET_ACCESS_KEY\"]}'", "env-cred-awk-environ", "AWS_SECRET"),
    (
        _PAD + "os.environ.pop('AWS_SECRET_ACCESS_KEY', None)\"",
        "env-cred-interpreter-environ",
        "AWS_SECRET",
    ),
    ("env | grep AWS_SECRET", "credential-exfil-env-grep-aws", "AWS_SECRET"),
    ("printenv AWS_SECRET_ACCESS_KEY", "credential-exfil-printenv-aws", "AWS_SECRET"),
]


@pytest.mark.parametrize(("command", "rule_id", "token"), _CASES)
def test_refusal_names_the_check_and_the_span(command: str, rule_id: str, token: str) -> None:
    reason = _check_env_credential_access(command)
    assert reason is not None
    diag = _diagnostic(reason)
    assert diag["rule"] == rule_id
    assert diag["component"] == "env-credential"
    start, end = int(diag["start"]), int(diag["end"])
    # The span ends on the secret-bearing variable name: a reader holding the
    # command can jump to the exact region that matched.
    assert command[start:end].upper().endswith(token)


def test_every_check_is_enumerated() -> None:
    """Each of the six checks in the tier has a case above, so none reports blind."""
    covered = {rule_id for _c, rule_id, _t in _CASES}
    assert covered == set(_ENV_CRED_PATTERN_IDS) | {r.id for r in _ENV_CRED_SHARED_RULES}
    assert len(_ENV_CRED_PATTERN_IDS) == len(_ENV_CRED_PATTERNS)


def test_span_lands_past_the_title_cut() -> None:
    """The reported case: the match sits beyond the ~200 chars the notice quotes."""
    command = _CASES[3][0]
    diag = _diagnostic(_check_env_credential_access(command) or "")
    assert int(diag["end"]) > 200


def test_diagnostic_never_echoes_the_matched_text() -> None:
    """A refusal about a credential read must not carry the credential's name or value."""
    secret = "AKIAIOSFODNN7EXAMPLEwJalrXUtnFEMI"
    command = f"echo $AWS_SECRET_ACCESS_KEY {secret}"
    reason = _check_env_credential_access(command) or ""
    diag_line = reason.splitlines()[1]
    assert diag_line.startswith(REFUSAL_DIAGNOSTIC_PREFIX)
    assert "AWS_" not in diag_line.upper()
    assert secret not in reason


def test_keystone_bash_gate_carries_the_diagnostic() -> None:
    """The public gate passes the annotated reason through unchanged."""
    reason = security.is_sensitive_bash_command("env | grep AWS_SECRET")
    assert reason is not None
    assert _diagnostic(reason)["rule"] == "credential-exfil-env-grep-aws"


def test_clean_command_is_still_allowed() -> None:
    assert _check_env_credential_access("echo hello && ls -la") is None


@pytest.mark.parametrize("rule", _ENV_CRED_SHARED_RULES, ids=lambda r: r.id)
def test_matcher_span_agrees_with_match(rule) -> None:
    """``match`` is ``span is not None``: the two can never disagree on a verdict."""
    matcher = _deny_matcher(rule.pattern)
    for text in ("env | grep aws_secret", "printenv aws_secret_access_key", "ls -la", ""):
        assert matcher.match(text) is (matcher.span(text) is not None)
