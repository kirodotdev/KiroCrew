"""The push-verdict floor denies EVERY agent publish on an activated install.

The subtraction (First Principles items 3/7/8) removed the receipt store's agent-facing
authority: the gateway publishes on the agent's behalf, so the agent never holds publish
authority and an agent-visible ``git push`` has no legitimate reason to run. On an activated
install the floor therefore refuses every one of them outright, with rule
``git-publish-agent-denied`` -- no receipt matching, no tree binding. A non-activated install
is unaffected (the floor's other, always-on git-publish rules still apply, but the activation
branch does not fire).
"""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.security import push_verdict


@pytest.fixture
def _activated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the push-verdict gate ON without touching the keystone on disk."""
    monkeypatch.setattr(push_verdict, "activation_enabled", lambda: True)


@pytest.fixture
def _not_activated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_verdict, "activation_enabled", lambda: False)


def test_an_agent_publish_is_denied_outright_when_activated(_activated: None) -> None:
    """Even a publish to the session's own feature branch is refused under activation.

    Mutation check: the pre-subtraction floor ALLOWED a publish that matched a recorded
    receipt, so with no receipt recorded this would still have refused -- but it refused with
    ``git-publish-no-push-verdict``. The NEW contract refuses with ``git-publish-agent-denied``
    and never consults a receipt at all. If the activation branch were reverted to allow a
    matching publish, a recorded verdict would make this return None and the test fails.
    """
    reason = security.is_denied("git push origin feature-x")
    assert reason is not None
    assert "git-publish-agent-denied" in reason


def test_a_force_with_lease_publish_is_denied_when_activated(_activated: None) -> None:
    reason = security.is_denied("git push --force-with-lease origin HEAD:refs/heads/feature-x")
    assert reason is not None
    assert "git-publish-agent-denied" in reason


def test_a_wrapped_publish_is_denied_when_activated(_activated: None) -> None:
    """The descent still finds the publish inside a shell wrapper, then denies it."""
    reason = security.is_denied("bash -c 'git push origin feature-x'")
    assert reason is not None
    assert "git-publish-agent-denied" in reason


def test_a_feature_publish_is_allowed_when_not_activated(_not_activated: None) -> None:
    """A non-activated install does not gain the agent-publish deny.

    The feature-branch publish passes every always-on git-publish floor rule, so with
    activation off the command is allowed -- proving the deny is the activation branch's and
    not some other rule's. Mutation check: arming the deny unconditionally would refuse this.
    """
    assert security.is_denied("git push origin feature-x") is None
