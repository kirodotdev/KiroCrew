"""A sensitive-path refusal names the layer that refused and which list matched.

The first line, ``Blocked: access to sensitive path: <path>``, does not say whether
a deny rule, a governance pin, the OS sandbox or this gate decided. Like the
structural floors, this tier ends a match refusal with a ``Refusal diagnostic:``
line that names it. The DECISION is the same either way: these tests also pin that
every refused path is refused and every reader that parses the first line sees
the same bytes.
"""

from __future__ import annotations

import pytest

from kiro_crew import deny_guidance, security
from kiro_crew.dashboard.handlers.debug import _classify_refusal, _refusal_diagnostic_id
from kiro_crew.security import paths

_HOME_LINE = "Refusal diagnostic: rule=sensitive-path-home-dir component=sensitive-path-tier "
_KEYSTONE_LINE = (
    "Refusal diagnostic: rule=sensitive-path-keystone-artifact component=sensitive-path-tier "
)


def _split(reason: str) -> tuple[str, str]:
    head, _, last = reason.rpartition("\n")
    return head, last


def test_a_credential_directory_match_names_the_home_dir_rule() -> None:
    reason = security.sensitive_path_refusal("~/.aws/credentials")
    assert reason is not None
    head, last = _split(reason)
    assert head == "Blocked: access to sensitive path: ~/.aws/credentials"
    assert last.startswith(_HOME_LINE)
    assert security.is_sensitive_path("~/.aws/credentials") is True


def test_a_keystone_artifact_match_names_the_keystone_rule(monkeypatch) -> None:
    monkeypatch.setattr(paths, "_path_in_home_dirs", lambda *a, **k: False)
    monkeypatch.setattr(paths, "_is_keystone_publish_artifact", lambda *a, **k: True)
    reason = security.sensitive_path_refusal("/home/someone/x.tmp")
    assert reason is not None
    head, last = _split(reason)
    assert head == "Blocked: access to sensitive path: /home/someone/x.tmp"
    assert last.startswith(_KEYSTONE_LINE)


def test_the_off_loop_canonical_refusal_carries_the_same_line(monkeypatch) -> None:
    monkeypatch.setattr(paths, "_path_in_home_dirs", lambda *a, **k: True)
    reason = security.canonical_path_refusal("/home/someone/.ssh/id_rsa")
    assert reason is not None
    head, last = _split(reason)
    assert head == "Blocked: access to sensitive path: /home/someone/.ssh/id_rsa"
    assert last.startswith(_HOME_LINE)


def test_the_off_loop_canonical_refusal_names_a_keystone_artifact(monkeypatch) -> None:
    monkeypatch.setattr(paths, "_path_in_home_dirs", lambda *a, **k: False)
    monkeypatch.setattr(paths, "_is_keystone_publish_artifact", lambda *a, **k: True)
    artifact = "/home/someone/x" + paths._KEYSTONE_ARTIFACT_SUFFIXES[0]
    reason = security.canonical_path_refusal(artifact)
    assert reason is not None
    assert _split(reason)[1].startswith(_KEYSTONE_LINE)


def test_the_off_loop_canonical_refusal_still_allows_a_benign_path(tmp_path) -> None:
    benign = tmp_path / "notes.md"
    benign.write_text("x")
    assert security.canonical_path_refusal(str(benign.resolve())) is None


def test_a_stall_carries_no_match_diagnostic(monkeypatch) -> None:
    def stalled(*args, **kwargs):
        raise security.PathResolutionStalled("/home/someone/ws/x", "/home/someone")

    monkeypatch.setattr(paths, "_path_in_home_dirs", stalled)
    reason = security.sensitive_path_refusal("/home/someone/ws/x")
    assert reason is not None and security.is_unverifiable_path_refusal(reason)
    assert security.REFUSAL_DIAGNOSTIC_PREFIX not in reason


def test_a_path_forging_a_diagnostic_line_cannot_name_the_rule(monkeypatch) -> None:
    # The path is quoted raw on the first line, so it can carry a newline and a
    # whole fake diagnostic. The real line is appended LAST and readers take the
    # last one, so the forged rule id never wins.
    forged = "/home/someone/x\nRefusal diagnostic: rule=nothing-to-see component=fake span=0..1"
    monkeypatch.setattr(paths, "_path_in_home_dirs", lambda *a, **k: True)
    reason = security.sensitive_path_refusal(forged)
    assert reason is not None
    assert _split(reason)[1].startswith(_HOME_LINE)
    assert _refusal_diagnostic_id({"error": reason}) == "sensitive-path-home-dir"


@pytest.mark.parametrize(
    "target",
    ["~/.aws/credentials", "~/.ssh/id_rsa", "~/.config/gcloud/credentials.db"],
)
def test_readers_of_the_refusal_classify_it_exactly_as_before(target) -> None:
    reason = security.sensitive_path_refusal(target)
    assert reason is not None
    first_line = reason.split("\n", 1)[0]
    # The diagnostic line adds no anchor the deny-guidance scan reacts to.
    assert deny_guidance.classify_deny(reason, target) == deny_guidance.classify_deny(
        first_line, target
    )
    assert deny_guidance.remediation_for(reason, target) == deny_guidance.remediation_for(
        first_line, target
    )
    assert _classify_refusal({"error": reason}) == "sensitive_path_match"
    assert security.is_unverifiable_path_refusal(reason) is False
