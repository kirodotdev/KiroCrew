"""A harness's own name for a read built-in and its kiro-cli name are ONE tool.

A Side Chat read built-in reaches the gate under the harness's own name (pi's
``read``, ``find``, ``ls``) with the kiro-cli name the
permission seam mapped it to as ``harness_builtin_tool`` (``fs_read``, ``glob``).
Governance asks the two as one identity: an allow-mode rule naming either
spelling admits the call, and an explicit deny on either spelling refuses it.
Nothing else is admitted: a spelling of a DIFFERENT tool does not count, and the
sensitive-path floor still refuses a credential path.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig


@pytest.fixture
def governance():
    """Install a governance ceiling for one test, and reset the context after."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    def install(policy: dict[str, Any]) -> None:
        base = build_default_context(KiroCrewConfig.load())
        body = {"version": 1, "boot": {"fail_closed": True}, **policy}
        ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(body)))

    yield install
    ctx_mod.reset_context()


def _pi_read(native: str, builtin: str, path: str = "/srv/notes/plan.txt"):
    """A Side Chat read as pi's permission seam states it: the bare native name as
    the title and the kiro-cli built-in it maps to as ``harness_builtin_tool``."""
    return HookManager(HooksConfig()).on_tool_call(
        native,
        tool_kind="read",
        raw_params={"path": path},
        harness_builtin_tool=builtin,
        classifier_only=True,
    )


_PI_READS = [("read", "fs_read"), ("find", "glob"), ("ls", "fs_read")]


@pytest.mark.parametrize(("native", "builtin"), _PI_READS)
@pytest.mark.parametrize("spelling", ["canonical", "native"])
def test_an_allow_list_in_either_spelling_admits_the_pi_read(governance, native, builtin, spelling):
    allowed = builtin if spelling == "canonical" else native
    governance({"tools": {"mode": "allow", "allow": [allowed]}})
    result = _pi_read(native, builtin)
    assert result.action != TOOL_DENY, result.reason


@pytest.mark.parametrize(("native", "builtin"), _PI_READS)
@pytest.mark.parametrize("spelling", ["canonical", "native"])
def test_a_deny_in_either_spelling_refuses_the_pi_read(governance, native, builtin, spelling):
    denied = builtin if spelling == "canonical" else native
    governance({"tools": {"mode": "deny", "deny": [denied]}})
    assert _pi_read(native, builtin).action == TOOL_DENY


@pytest.mark.parametrize(("native", "builtin"), _PI_READS)
def test_an_allow_list_naming_neither_spelling_refuses_the_pi_read(governance, native, builtin):
    governance({"tools": {"mode": "allow", "allow": ["web_fetch"]}})
    assert _pi_read(native, builtin).action == TOOL_DENY


def test_a_title_naming_another_tool_is_not_an_alias(governance):
    """``grep`` is not a spelling of ``fs_read``: a grep grant does not admit a
    read that happens to be titled ``grep``."""
    governance({"tools": {"mode": "allow", "allow": ["grep"]}})
    assert _pi_read("grep", "fs_read").action == TOOL_DENY


@pytest.mark.parametrize("path", ["~/.ssh/id_rsa", "~/.aws/credentials"])
def test_a_credential_path_is_still_refused_under_an_alias_grant(governance, path):
    governance({"tools": {"mode": "allow", "allow": ["read", "fs_read"]}})
    assert _pi_read("read", "fs_read", path).action == TOOL_DENY
