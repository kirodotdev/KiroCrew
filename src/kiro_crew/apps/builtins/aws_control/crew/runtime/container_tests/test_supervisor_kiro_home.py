"""The task owns its agent-spec directory, so the backend is allowed to write it.

The failure this pins. With no ``KIRO_HOME`` the agent specs
resolve to the process HOME's ``~/.kiro/agents``, which every instance under that
``$HOME`` shares. The backend runs on a non-default data home
(``KIROCREW_HOME=<data home>``), and Kiro Crew REFUSES to rewrite a shared agents dir
from one, because the specs it writes pin the writer's data home into every managed MCP
server entry and break strict session identity for a default-home gateway. A crew spec
installed into that shared directory carries no ownership provenance, so the backend
reads it as another home's, declines to write, and never creates the DEFAULT spec
``kirocrew.json``. The turn then dies:

    kiro_crew.agent.DerivedSpecStale: the default agent spec
    /var/lib/crew/.kiro/agents/kirocrew.json is missing, so the
    kirocrew-worker.json mirror cannot be verified or rebuilt

The fix does not touch that guard. It uses the guard's own documented private-target
case: ``<data home>/kiro/agents`` is exactly ``config.paths.isolated_agents_dir(data
home)``, a directory this task's teardown owns, so the guard stands aside.

Two invariants carry the fix, and each gets a test that fails if it breaks:

1. The installer and the backend resolve the SAME directory. They get there by
   different routes -- the installer mirrors kiro-cli's ``$KIRO_HOME`` rule from the
   environment, the backend goes through Kiro Crew's resolver -- so agreement is a
   property to prove, not to assume.
2. That directory is the one Kiro Crew's guard exempts. Asserted against the REAL
   ``isolated_agents_dir`` and the REAL guard, not against a copy of the path, so a
   change to either side of the contract reddens here rather than in production.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from container.supervisor import __main__ as sup
from container.supervisor import backend as be
from container.supervisor import bundle as bundle_mod

from ._settings_helper import make_settings

#: The crew this deployment ships. It is also one of the spec names Kiro Crew derives for
#: itself, which is what puts the installed spec in front of the guard's ownership probe.
DEPLOYED_CREW = "kirocrew-worker"


def _install_crew_spec(agents_dir, crew: str):
    """Write a crew spec the way the bundle installer does: plain, vouched for by nobody.

    The absence of provenance is the point. Kiro Crew stamps its own data home into every
    managed server entry of a spec it writes, and reads that back to tell its own specs
    from another home's; a bundle spec carries no such entry, so it reads as another
    home's.
    """
    path = agents_dir / f"{crew}.json"
    path.write_text(json.dumps({"name": crew}), encoding="utf-8")
    return path


def test_the_kiro_home_is_the_directory_the_guard_exempts(tmp_path):
    """``Settings.kiro_home`` must land on Kiro Crew's own private-target path.

    The exemption is an EXACT match on ``<data home>/kiro/agents`` -- deliberately not
    "anywhere beneath the data home", which would read the machine-wide dir as private.
    So the ``kiro`` segment is load-bearing and this compares against the real
    definition rather than re-spelling it.
    """
    from kiro_crew.config.paths import isolated_agents_dir

    settings = make_settings(tmp_path)

    assert settings.kiro_home / "agents" == isolated_agents_dir(settings.data_home)


def test_the_backend_environment_carries_the_task_owned_kiro_home(tmp_path):
    settings = make_settings(tmp_path)

    env = be.build_backend_env(settings, {})

    assert env[be.ENV_KIRO_HOME] == str(settings.kiro_home)


def test_a_stale_inherited_kiro_home_does_not_reach_the_backend(tmp_path):
    """The value is a function of the settings, never of what the base mapping holds.

    A base carrying someone else's ``KIRO_HOME`` -- the image's, a previous task's, an
    operator's -- would otherwise point the backend at an agents dir the crew was not
    installed into, and nothing in the backend reports that: it just serves the agents
    it finds.
    """
    settings = make_settings(tmp_path)

    env = be.build_backend_env(settings, {"KIRO_HOME": str(tmp_path / "somewhere-else")})

    assert env[be.ENV_KIRO_HOME] == str(settings.kiro_home)


def test_export_points_the_installer_at_the_same_directory(tmp_path, monkeypatch):
    """The installer resolves its destination from the environment, so the export is
    what makes the two agree. Checked through the installer's OWN resolver."""
    settings = make_settings(tmp_path)
    monkeypatch.delenv("KIRO_HOME", raising=False)

    agents = sup.export_kiro_home(settings)

    assert os.environ["KIRO_HOME"] == str(settings.kiro_home)
    assert bundle_mod.default_kiro_agents_dir() == agents == settings.kiro_home / "agents"
    assert agents.is_dir(), "the export must leave the directory ready for the install"


def test_export_refuses_when_the_two_resolvers_disagree(tmp_path, monkeypatch):
    """The drift tripwire. A future change to either spelling must fail at boot.

    Simulated by making the installer's resolver answer a different directory while the
    settings keep theirs: that is precisely the shape a rename would produce, and
    without the check the deployment boots, installs the crew in one directory and
    serves agents out of another.
    """
    settings = make_settings(tmp_path)
    monkeypatch.setattr(
        bundle_mod, "default_kiro_agents_dir", lambda: tmp_path / "elsewhere" / "agents"
    )

    with pytest.raises(sup.common.ConfigError) as err:
        sup.export_kiro_home(settings)

    assert "agents directory" in str(err.value)


def test_export_refuses_when_the_directory_cannot_be_created(tmp_path, monkeypatch):
    """Fail closed rather than let the backend decline for a second reason later."""
    settings = make_settings(tmp_path)
    blocker = settings.data_home / "kiro"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.delenv("KIRO_HOME", raising=False)

    with pytest.raises(sup.common.ConfigError) as err:
        sup.export_kiro_home(settings)

    assert "agent-spec directory" in str(err.value)


@pytest.mark.parametrize("planted", ["kiro", "kiro/agents"])
def test_export_refuses_a_symlinked_agent_home(tmp_path, monkeypatch, planted):
    """A link at either path this task owns is refused, and refused before the export.

    Whether the target is private is decided on RESOLVED paths on both sides of the
    comparison, so a link pointing into a shared agents tree makes that tree test as this
    task's own -- and the backend then rewrites specs belonging to another data home,
    which is worse than the failure the export exists to fix. The planter is reachable:
    a previous task's model worker runs unsandboxed under this uid with the volume
    writable, and a link it leaves behind survives into the next boot.
    """
    settings = make_settings(tmp_path)
    elsewhere = tmp_path / "someone-elses-agents"
    elsewhere.mkdir(parents=True)
    link = settings.data_home / planted
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.delenv("KIRO_HOME", raising=False)

    with pytest.raises(sup.common.ConfigError) as err:
        sup.export_kiro_home(settings)

    # Matched on a PHRASE, not the bare word: pytest names the tmp dir after the
    # test, so "symlink" appears in every path this message quotes and a
    # substring check on it passes whatever the refusal says.
    assert "symlink, and the container refuses" in str(err.value)
    assert "KIRO_HOME" not in os.environ, (
        "the refusal came after the export, so a poisoned layout still reached the "
        "backend's environment"
    )


def test_export_refuses_a_link_planted_after_the_symlink_check(tmp_path, monkeypatch):
    """The window between reading the path and creating it is closed too.

    The refusals above read the path; ``mkdir`` then uses it, so a link appearing in
    between is followed rather than refused. The resolved-location check is what closes
    that, and it is stated as the guard's own equation, so it needs no assumption about
    where a link could be planted. Planted here from inside ``mkdir`` itself, which is
    exactly the interleaving.
    """
    settings = make_settings(tmp_path)
    elsewhere = tmp_path / "someone-elses-agents"
    (elsewhere / "agents").mkdir(parents=True)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    link = settings.data_home / "kiro"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(elsewhere, target_is_directory=True)
    # The link is already there, and the no-follow check is made to MISS it -- which is
    # what losing the race looks like from this function's point of view. Only the
    # resolved-location check can still catch it.
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)

    with pytest.raises(sup.common.ConfigError) as err:
        sup.export_kiro_home(settings)

    assert "resolves to" in str(err.value)


def _let_the_real_resolver_answer(monkeypatch, agent_mod) -> None:
    """Release the suite's per-test agent-spec pin so the guard sees a real layout.

    Required, and the reason the control test below exists. The guard's FIRST act is to
    answer "no decline" when the write target is not what the ambient environment would
    produce -- a privately redirected target is private by definition -- and this
    repository's conftest installs exactly such a redirect (``KIRO_AGENTS_DIR``) for
    every test. Left in place, both tests below answer "no decline" for a reason that
    has nothing to do with the layout, which is the false green the control catches.

    Clearing the documented hook is enough: the conftest's own resolver already defers
    to the real resolution once a test has moved ``HOME`` or set ``KIRO_HOME``, so the
    target and the ambient directory then agree by construction rather than by a patch
    that asserts they do. Both tests move ``HOME`` to a tmp path, so nothing here can
    reach the operator's own agents directory.
    """
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", None)


def test_the_guard_stands_aside_for_the_container_layout(tmp_path, monkeypatch):
    """The decisive one: the REAL guard must not decline under the container's env.

    This is the assertion that would have caught the failure. It runs Kiro Crew's own
    ``_decline_shared_agent_home`` with the variables the supervisor sets, against a spec
    file carrying no ownership provenance -- which is what the crew installer writes --
    and requires a verdict of "no decline". The control below runs the same setup on the
    SHARED directory and requires a decline, so a green verdict here cannot come from a
    guard that has stopped declining anything.

    The crew name is the one the deployment ships, because it decides whether the guard
    looks at the installed spec at all: the ownership probe reads only the spec names Kiro
    Crew owns, and ``kirocrew-worker.json`` is one of them. A crew named anything else
    leaves the shared directory holding no spec the probe recognises, which reads as
    "nothing to preserve" and lets the write through -- into a directory this task has no
    business writing. The layout is what fixes that, for every crew name.
    """
    from kiro_crew import agent as agent_mod

    settings = make_settings(tmp_path, crew=DEPLOYED_CREW)
    monkeypatch.setenv("HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIROCREW_HOME", str(settings.data_home))
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    _let_the_real_resolver_answer(monkeypatch, agent_mod)

    agents = sup.export_kiro_home(settings)
    _install_crew_spec(agents, settings.crew_name)

    assert agent_mod._decline_shared_agent_home(audit=False) is None


def test_the_shared_layout_declines(tmp_path, monkeypatch):
    """The control: the same setup on the SHARED directory must decline.

    Same data home, same crew, same unvouched spec; only the directory differs -- the
    process HOME's shared agents dir instead of the task-owned one. A decline here is what
    makes the test above a statement about the LAYOUT rather than about the guard. It does
    not claim which of the guard's arms answered, only that the shared directory is
    refused and the private one is not, which is the whole of what the fix rests on.
    """
    from kiro_crew import agent as agent_mod

    settings = make_settings(tmp_path, crew=DEPLOYED_CREW)
    monkeypatch.setenv("HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIROCREW_HOME", str(settings.data_home))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    _let_the_real_resolver_answer(monkeypatch, agent_mod)
    # The shared directory, taken from the installer's OWN resolver with no KIRO_HOME
    # set. Not spelled out as a literal: ``test_agent_home_isolation`` forbids a
    # hard-coded copy of the machine-wide agents dir anywhere in ``src``.
    shared = bundle_mod.default_kiro_agents_dir()
    shared.mkdir(parents=True)
    _install_crew_spec(shared, settings.crew_name)

    declined = agent_mod._decline_shared_agent_home(audit=False)

    assert declined is not None, (
        "the shared agents dir no longer declines a non-default data home, so the "
        "test above proves nothing about the fix"
    )
    assert declined.parent == shared
