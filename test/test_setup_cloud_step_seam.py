"""`kirocrew setup`'s cloud step follows the ``remote_provisioners`` seam.

The first-run wizard's last step offers to launch Kiro Crew on EC2 in the user's
own AWS account. A managed edition that withdraws the built-in ``aws_ec2`` lane
from the seam (so its dashboard Set-up tab does not offer it) must not have the
wizard put the same launch in front of its users.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew import cli_setup
from kiro_crew.platform import context as platform_context
from kiro_crew.platform.defaults import (
    BUILTIN_REMOTE_PROVISIONER,
    FARGATE_REMOTE_PROVISIONER,
)


class _Provisioners:
    def __init__(self, rows=None, exc: Exception | None = None):
        self._rows = rows or []
        self._exc = exc

    def provisioners(self):
        if self._exc is not None:
            raise self._exc
        return list(self._rows)

    def engine_for(self, provisioner_id, *, confirmed_recipient=""):
        raise KeyError(provisioner_id)


@pytest.fixture
def with_provisioners(monkeypatch):
    """Install a context whose ``remote_provisioners`` seam is *provider*."""

    def _install(provider):
        base = platform_context.current_context()
        ctx = dataclasses.replace(base, remote_provisioners=provider)
        monkeypatch.setattr(platform_context, "current_context", lambda: ctx)

    return _install


def _prompted(monkeypatch) -> list[str]:
    asked: list[str] = []

    def fake_input(prompt=""):
        asked.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def _offered_launch(asked: list[str]) -> bool:
    # The wizard's own prompt text spells the product as one word; match its
    # two stable halves rather than restating that spelling here.
    return any(p.lstrip().startswith("Launch") and "on AWS" in p for p in asked)


def test_offers_launch_when_builtin_lane_present(monkeypatch, capsys, with_provisioners):
    with_provisioners(_Provisioners([BUILTIN_REMOTE_PROVISIONER]))
    asked = _prompted(monkeypatch)

    cli_setup._maybe_setup_cloud()

    assert _offered_launch(asked)
    assert "Run on AWS" in capsys.readouterr().out


@pytest.mark.parametrize(
    "rows",
    [[], [dataclasses.replace(FARGATE_REMOTE_PROVISIONER)]],
    ids=["no-lanes", "only-non-ec2-lane"],
)
def test_skips_entirely_when_edition_withdraws_ec2(monkeypatch, capsys, with_provisioners, rows):
    with_provisioners(_Provisioners(rows))
    asked = _prompted(monkeypatch)

    cli_setup._maybe_setup_cloud()

    # Neither the heading nor the prompt: the step must not ask for AWS access.
    assert asked == []
    out = capsys.readouterr().out
    assert "Run on AWS" not in out
    assert "EC2" not in out


def test_degraded_seam_keeps_the_public_offer(monkeypatch, with_provisioners):
    # Same fallback as the Set-up tab (handlers_cloud._provisioners): a transient
    # seam failure keeps the built-in lane rather than silently hiding it.
    with_provisioners(_Provisioners(exc=RuntimeError("transient")))
    asked = _prompted(monkeypatch)

    cli_setup._maybe_setup_cloud()

    assert _offered_launch(asked)


def test_composition_error_is_not_swallowed(monkeypatch, with_provisioners):
    from kiro_crew.platform.context import PlatformCompositionError

    with_provisioners(_Provisioners(exc=PlatformCompositionError("companion failed")))
    _prompted(monkeypatch)

    with pytest.raises(PlatformCompositionError):
        cli_setup._maybe_setup_cloud()
