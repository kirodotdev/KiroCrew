"""The profile-store isolation and the policy installer the chokepoint tests share.

``test_governance_chokepoints.py`` and ``test_governance_chokepoints_scope_gates.py``
both install a governance ceiling with ``_install`` and both need the profiles dir
and the platform context reset around every test. An autouse fixture applies only in
a module that imports it, so each of them imports ``_isolate`` by name.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in a module that imports this fixture calls monkeypatch.undo()
def _isolate(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _install(policy_body):
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))
