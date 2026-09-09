"""A non-default ``KIROCREW_HOME`` must not rewrite the shared ``~/.kiro/agents`` specs.

The failure these pin: a throwaway gateway booted with ``KIROCREW_HOME=<scratch>``
(no ``KIRO_HOME``, not a worktree, not under the temp root) runs
``rebuild_agent_config`` on boot, rewrites the operator's machine-wide
``~/.kiro/agents/*.json`` and pins ``KIROCREW_HOME=<scratch>`` into every
managed MCP server's ``env``. Every ``kirocrew-core`` stub the REAL gateway's
sessions spawn afterwards resolves ``config_dir()`` to the scratch home, finds
no signed session-pid mapping there, and every strict-identity tool is refused
with "signed pid mapping did not verify" -- while ``kirocrew doctor`` on the real
gateway reports a healthy trust root.

Three layers are pinned here:

* ``config.paths.foreign_data_home`` / ``adopt_isolated_kiro_home`` -- a
  non-default data home is given its OWN kiro home (``<data home>/kiro``) via
  ``KIRO_HOME``, the same recipe pods and the E2E harness already use by hand;
* ``agent._decline_shared_agent_home`` -- and if a caller bypasses that prologue,
  the write guard refuses the shared target outright (audited);
* ``doctor_spec_home`` -- ``kirocrew doctor`` names a spec whose pin disagrees
  with the data home it runs on, with the one-command remedy.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import requires_symlinks
from kiro_crew.config import paths
from kiro_crew.config.paths import (
    adopt_isolated_kiro_home,
    foreign_data_home,
    isolated_agents_dir,
    isolated_kiro_home,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _relocate_main_homes(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point the default/legacy data homes at tmp so no test names the real ones."""
    default = tmp_path / "user" / ".kiro" / "crew"
    legacy = tmp_path / "user" / ".kirocrew"
    monkeypatch.setattr(paths, "_default_home", lambda: default)
    monkeypatch.setattr(paths, "_legacy_home", lambda: legacy)
    return default, legacy


# --------------------------------------------------------------------------
# foreign_data_home: which instance owns ~/.kiro
# --------------------------------------------------------------------------
class TestForeignDataHome:
    def test_no_override_is_the_main_instance(self, monkeypatch, tmp_path):
        _relocate_main_homes(monkeypatch, tmp_path)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        assert foreign_data_home() is None

    def test_override_naming_the_default_home_is_still_main(self, monkeypatch, tmp_path):
        default, _ = _relocate_main_homes(monkeypatch, tmp_path)
        default.mkdir(parents=True)
        # Lexical re-spellings of the same home (a trailing slash here; ``~`` and
        # ``..`` segments fold the same way) read as main. A symlink alias is the
        # documented gap -- see ``foreign_data_home``'s docstring -- and is not
        # promised here.
        monkeypatch.setenv("KIROCREW_HOME", str(default) + "/")
        assert foreign_data_home() is None

    def test_override_naming_the_legacy_home_is_still_main(self, monkeypatch, tmp_path):
        _, legacy = _relocate_main_homes(monkeypatch, tmp_path)
        legacy.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(legacy))
        assert foreign_data_home() is None

    def test_any_other_valid_override_is_foreign(self, monkeypatch, tmp_path):
        _relocate_main_homes(monkeypatch, tmp_path)
        scratch = tmp_path / "scratch" / "runtime-3c0a7e8b" / "pfshot" / "home"
        scratch.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        assert foreign_data_home() == scratch.resolve()

    def test_an_unsafe_override_is_not_foreign(self, monkeypatch, tmp_path):
        """A refused override (``/``) falls back to the default home everywhere
        else, so it must read as the main instance here too -- otherwise the
        write guard would refuse the real install its own specs. ``Path.home`` is
        pinned because the default-path resolution drops a breadcrumb beside it."""
        _relocate_main_homes(monkeypatch, tmp_path)
        (tmp_path / "user").mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
        monkeypatch.setenv("KIROCREW_HOME", "/")
        assert foreign_data_home() is None


# --------------------------------------------------------------------------
# adopt_isolated_kiro_home: the prologue export
# --------------------------------------------------------------------------
class TestAdoptIsolatedKiroHome:
    def test_default_home_exports_nothing(self, monkeypatch, tmp_path):
        _relocate_main_homes(monkeypatch, tmp_path)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        assert adopt_isolated_kiro_home() is None
        assert "KIRO_HOME" not in os.environ

    def test_foreign_home_adopts_its_own_kiro_home(self, monkeypatch, tmp_path):
        _relocate_main_homes(monkeypatch, tmp_path)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.delenv("KIRO_HOME", raising=False)

        adopted = adopt_isolated_kiro_home()

        assert adopted == isolated_kiro_home(scratch.resolve())
        assert os.environ["KIRO_HOME"] == str(adopted)
        # The whole point: the agents dir kiro-cli and every writer now resolve
        # is the dedicated one the write guard's private-target exemption admits,
        # not the machine-wide ``~/.kiro/agents``.
        assert paths.kiro_home() == adopted
        assert paths.ambient_agents_dir() == isolated_agents_dir(scratch.resolve())
        assert paths.kiro_sessions_dir().is_relative_to(adopted)

    def test_an_explicit_kiro_home_is_never_overridden(self, monkeypatch, tmp_path):
        """``KIRO_HOME`` set by the operator is a choice -- including naming the
        shared ``~/.kiro`` on purpose -- and the prologue must not second-guess it."""
        _relocate_main_homes(monkeypatch, tmp_path)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "scratch-home"))
        chosen = tmp_path / "user" / ".kiro"
        monkeypatch.setenv("KIRO_HOME", str(chosen))
        assert adopt_isolated_kiro_home() is None
        assert os.environ["KIRO_HOME"] == str(chosen)

    @pytest.mark.skipif(sys.platform == "win32", reason="/etc is a POSIX system dir")
    def test_an_invalid_kiro_home_is_left_to_the_resolver(self, monkeypatch, tmp_path):
        """``KIRO_HOME=/etc`` is one ``kiro_home()`` discards -- but judging that
        means resolving it, and the prologue does no filesystem work. The declared
        value is left alone; the resolver falls back to the shared ``~/.kiro`` with
        its own warning, and the write guard keeps that dir read-only for a foreign
        home (``test_an_invalid_kiro_home_is_not_an_opt_in``), so the outcome is
        the ``KIRO_HOME=~/.kiro`` opt-out, not a bypass."""
        import os as _os

        _relocate_main_homes(monkeypatch, tmp_path)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", "/etc")
        calls: list[str] = []
        for name in ("stat", "lstat", "readlink"):
            real = getattr(_os, name)

            def _spy(*a, _n=name, _real=real, **k):
                calls.append(_n)
                return _real(*a, **k)

            monkeypatch.setattr(_os, name, _spy)

        assert adopt_isolated_kiro_home() is None
        assert os.environ["KIRO_HOME"] == "/etc" and calls == []
        with patch("pathlib.Path.home", return_value=tmp_path / "user"):
            assert paths.kiro_home() == tmp_path / "user" / ".kiro"

    def test_adoption_logs_the_opt_out_once_at_info(self, monkeypatch, tmp_path, caplog):
        """The adoption line names the ``KIRO_HOME=~/.kiro`` opt-out and its cost; the
        upgrade transition itself is reported by ``kirocrew doctor`` (Data Home),
        because telling a first start from a later one would need a filesystem
        probe on the boot path."""
        _relocate_main_homes(monkeypatch, tmp_path)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        with caplog.at_level("INFO", logger="kiro_crew.config.paths"):
            adopt_isolated_kiro_home()
        records = [r for r in caplog.records if r.name == "kiro_crew.config.paths"]
        assert [r.levelname for r in records] == ["INFO"]
        assert "export KIRO_HOME=" in records[0].getMessage()
        assert "kirocrew doctor" in records[0].getMessage()

    def test_adoption_touches_no_filesystem(self, monkeypatch, tmp_path):
        """The prologue runs before the gateway binds, and a stat or resolve on a
        roaming/UNC home is a network round-trip that would hold readiness
        hostage. After ``config_dir()`` is memoised (which ``ensure_data_home()``
        does first in every prologue), adoption must issue no filesystem call."""
        import os as _os

        _relocate_main_homes(monkeypatch, tmp_path)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        paths.config_dir()  # what ensure_data_home() does: resolve + memoise once
        # The memo re-prime is part of adoption and is held to the same rule: it
        # SEEDS ``hooks``' UNC-root memo from the already-resolved data home. The
        # suite's agents-dir pin would make the seed defer to the resolving
        # prime, so it is lifted here to exercise the production branch.
        import kiro_crew.hooks  # noqa: F401 -- the memo must be loaded to be re-primed

        monkeypatch.setattr(paths, "_agents_dir_override", None)
        # The log line is covered by ``test_adoption_logs_the_opt_out_once_at_info``;
        # silenced here so a stat-ing log handler in the test process (a
        # WatchedFileHandler) cannot be mistaken for adoption's own work.
        monkeypatch.setattr(paths.logger, "info", lambda *a, **k: None)
        # Resolve the expectation BEFORE the spies go in: ``Path.resolve()`` is
        # itself a stat/lstat, and it must not be counted against adoption.
        expected = isolated_kiro_home(scratch.resolve())

        calls: list[str] = []
        for name in ("stat", "lstat", "scandir", "listdir", "readlink", "mkdir"):
            real = getattr(_os, name)

            def _spy(*a, _n=name, _real=real, **k):
                calls.append(_n)
                return _real(*a, **k)

            monkeypatch.setattr(_os, name, _spy)

        adopted = adopt_isolated_kiro_home()
        seen = list(calls)

        assert seen == [], f"adoption touched the filesystem: {seen}"
        assert adopted == expected

    @requires_symlinks
    def test_a_kiro_home_symlink_cycle_is_invalid_not_a_crash(self, monkeypatch, tmp_path):
        """A ``KIRO_HOME`` that cannot be resolved (a link cycle) is a bad value, not
        an abort: the resolver reads it as unset and falls back, and the prologue
        -- which does not resolve it at all -- leaves the declared value alone."""
        _relocate_main_homes(monkeypatch, tmp_path)
        loop = tmp_path / "loop"
        loop.symlink_to(loop)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", str(loop))

        assert paths.explicit_kiro_home() is None
        with patch("pathlib.Path.home", return_value=tmp_path / "user"):
            assert paths.kiro_home() == tmp_path / "user" / ".kiro"
        assert adopt_isolated_kiro_home() is None
        assert os.environ["KIRO_HOME"] == str(loop)

    def test_idempotent(self, monkeypatch, tmp_path):
        _relocate_main_homes(monkeypatch, tmp_path)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "scratch-home"))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        first = adopt_isolated_kiro_home()
        assert first is not None
        assert adopt_isolated_kiro_home() is None  # already adopted -> no-op
        assert os.environ["KIRO_HOME"] == str(first)

    def test_isolated_agents_dir_derives_from_isolated_kiro_home(self, tmp_path):
        """One definition of the recipe: the write guard's privacy test and the
        exported ``KIRO_HOME`` cannot drift apart."""
        home = tmp_path / "h"
        assert isolated_agents_dir(home) == isolated_kiro_home(home) / "agents"


# --------------------------------------------------------------------------
# The write guard: a non-default data home does not own the shared agents dir
# --------------------------------------------------------------------------
def _pretend_target_is_shared(monkeypatch, agent_mod, agents_dir: Path) -> None:
    """Same seam ``test_agent_home_isolation`` uses: present *agents_dir* as both the
    write target and what the ambient environment resolves."""
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent_mod, "ambient_agents_dir", lambda: agents_dir)


def _durable_primary_checkout(monkeypatch, agent_mod) -> None:
    """The failing shape: NOT a linked worktree, NOT under the temp root."""
    monkeypatch.setattr(agent_mod, "__file__", "/durable-install/KiroCrew/src/kiro_crew/agent.py")


def _capture_sel(monkeypatch, agent_mod) -> list[dict]:
    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())
    return events


class TestWriteGuardRefusesForeignHome:
    def test_scratch_home_without_kiro_home_is_declined_and_audited(self, monkeypatch, tmp_path):
        """The reproduction, at the guard: ``KIROCREW_HOME=<scratch>``, no
        ``KIRO_HOME``, durable checkout, shared target -> refused, not written."""
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        events = _capture_sel(monkeypatch, agent)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_primary_checkout(monkeypatch, agent)
        shared = tmp_path / "user" / ".kiro" / "agents"
        _pretend_target_is_shared(monkeypatch, agent, shared)

        declined = agent._decline_shared_agent_home()

        assert declined == shared / agent.AGENT_FILENAME
        denied = [e for e in events if e.get("outcome") == "denied"]
        assert len(denied) == 1, events
        assert denied[0]["operation"] == "agent_home_write"
        assert str(shared) in denied[0]["resources"]
        assert str(isolated_agents_dir(scratch.resolve())) in denied[0]["error"]

    def test_rebuild_writes_nothing_from_a_scratch_home(self, monkeypatch, tmp_path):
        """End to end through ``rebuild_agent_config``: the shared dir is untouched."""
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        _capture_sel(monkeypatch, agent)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_primary_checkout(monkeypatch, agent)
        shared = tmp_path / "user" / ".kiro" / "agents"
        _pretend_target_is_shared(monkeypatch, agent, shared)

        returned = agent.rebuild_agent_config()

        assert returned == shared / agent.AGENT_FILENAME
        assert not shared.exists(), "a non-default data home must not create the shared agent home"

    def test_the_refusal_names_the_remedy(self, monkeypatch, tmp_path, caplog):
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        _capture_sel(monkeypatch, agent)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_primary_checkout(monkeypatch, agent)
        _pretend_target_is_shared(monkeypatch, agent, tmp_path / "user" / ".kiro" / "agents")

        with caplog.at_level("WARNING", logger="kiro_crew.agent"):
            agent._decline_shared_agent_home()

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "non-default data home" in text
        assert f"KIRO_HOME={isolated_kiro_home(scratch.resolve())}" in text

    def test_adopted_kiro_home_makes_the_target_private(self, monkeypatch, tmp_path):
        """With the prologue's export in force the instance writes its OWN specs:
        the target is ``isolated_agents_dir(own home)`` and the guard stands aside.
        Being refused would not be harmless here -- it would hand this instance the
        shared spec, whose env pins the LIVE data home."""
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        assert adopt_isolated_kiro_home() is not None
        _durable_primary_checkout(monkeypatch, agent)
        _pretend_target_is_shared(monkeypatch, agent, isolated_agents_dir(scratch.resolve()))

        assert agent._decline_shared_agent_home() is None

    def test_an_explicit_kiro_home_is_not_write_authorization(self, monkeypatch, tmp_path):
        """``KIRO_HOME=~/.kiro`` from a foreign data home makes kiro-cli READ the
        shared specs; it never makes this instance their writer. An environment
        variable is set by whoever launched the process -- an agent running
        ``KIRO_HOME=$HOME/.kiro kirocrew setup --agent-only`` included -- so it
        cannot be the thing that authorises rewriting the shared file with this
        home pinned into it. Ownership is the data home, nothing else."""
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        events = _capture_sel(monkeypatch, agent)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "relocated-home"))
        shared_kiro = tmp_path / "user" / ".kiro"
        monkeypatch.setenv("KIRO_HOME", str(shared_kiro))
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_primary_checkout(monkeypatch, agent)
        _pretend_target_is_shared(monkeypatch, agent, shared_kiro / "agents")

        assert agent._decline_shared_agent_home() is not None
        assert [e["outcome"] for e in events] == ["denied"]

    @requires_symlinks
    def test_a_planted_link_under_the_data_home_gets_no_exemption(self, monkeypatch, tmp_path):
        """An agent with write access to the data home plants
        ``<data home>/kiro -> ~/.kiro``. With ``KIRO_HOME=<data home>/kiro`` every
        path resolves onto the shared tree, so a RESOLVED comparison would read the
        machine-wide agents dir as this instance's private one. The exemption
        requires the isolated path to be link-free."""
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        events = _capture_sel(monkeypatch, agent)
        shared_kiro = tmp_path / "user" / ".kiro"
        (shared_kiro / "agents").mkdir(parents=True)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        isolated_kiro_home(scratch).symlink_to(shared_kiro, target_is_directory=True)
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", str(isolated_kiro_home(scratch)))
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_primary_checkout(monkeypatch, agent)
        _pretend_target_is_shared(monkeypatch, agent, shared_kiro / "agents")

        assert agent._decline_shared_agent_home() is not None
        assert [e["outcome"] for e in events] == ["denied"]

    def test_a_regular_file_at_the_isolated_home_gets_no_exemption(self, monkeypatch, tmp_path):
        """A stray regular file at ``<data home>/kiro``: ``resolve()`` does not
        notice, the exemption would pass, and the writer's ``mkdir(parents=True)``
        would then crash the boot with ``NotADirectoryError``. The exemption asks
        for directory shape explicitly, so the guard refuses instead."""
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        events = _capture_sel(monkeypatch, agent)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        isolated_kiro_home(scratch).write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", str(isolated_kiro_home(scratch)))
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_primary_checkout(monkeypatch, agent)
        _pretend_target_is_shared(monkeypatch, agent, isolated_agents_dir(scratch))

        assert agent._decline_shared_agent_home() is not None
        assert [e["outcome"] for e in events] == ["denied"]

    def test_default_home_still_owns_the_shared_dir(self, monkeypatch, tmp_path):
        """The ordinary install is unchanged: no override, durable checkout -> writes."""
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        events = _capture_sel(monkeypatch, agent)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_primary_checkout(monkeypatch, agent)
        _pretend_target_is_shared(monkeypatch, agent, tmp_path / "user" / ".kiro" / "agents")

        assert agent._decline_shared_agent_home() is None
        assert [e["outcome"] for e in events] == ["allowed"]

    @pytest.mark.skipif(sys.platform == "win32", reason="/etc is a POSIX system dir")
    def test_an_invalid_kiro_home_is_not_an_opt_in(self, monkeypatch, tmp_path):
        """``KIRO_HOME=/etc`` is discarded by ``kiro_home()``, so the target is the
        shared dir after all; the raw variable being set must not read as the
        operator's consent to write it."""
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        events = _capture_sel(monkeypatch, agent)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "scratch-home"))
        monkeypatch.setenv("KIRO_HOME", "/etc")
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_primary_checkout(monkeypatch, agent)
        _pretend_target_is_shared(monkeypatch, agent, tmp_path / "user" / ".kiro" / "agents")

        assert agent._decline_shared_agent_home() is not None
        assert [e["outcome"] for e in events] == ["denied"]


class TestAppAgentWritersHonourOwnership:
    """``apps.bridges`` is the other writer of the agents dir. Pointed at the SHARED
    directory from a foreign home -- the ``KIRO_HOME=~/.kiro`` opt-out, or a
    prologue-bypassing caller -- it must neither materialise, prune nor remove app
    specs there: two instances with different app sets would otherwise fight over
    the default instance's files."""

    @staticmethod
    def _foreign_on_shared(monkeypatch, tmp_path):
        from kiro_crew import agent
        from kiro_crew.apps import bridges

        _relocate_main_homes(monkeypatch, tmp_path)
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        shared = tmp_path / "user" / ".kiro" / "agents"
        shared.mkdir(parents=True)
        monkeypatch.setenv("KIRO_HOME", str(shared.parent))
        _pretend_target_is_shared(monkeypatch, agent, shared)
        monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", shared)
        return agent, bridges, shared, scratch

    def test_predicate_names_the_foreign_home_for_the_shared_dir(self, monkeypatch, tmp_path):
        agent, _, shared, scratch = self._foreign_on_shared(monkeypatch, tmp_path)
        assert agent.foreign_home_targets_shared_agents_dir(shared) == scratch.resolve()
        # A redirect of the caller's own is not the shared dir.
        assert agent.foreign_home_targets_shared_agents_dir(tmp_path / "elsewhere") is None
        # The instance's dedicated dir is its own to write.
        own = isolated_agents_dir(scratch.resolve())
        own.mkdir(parents=True)
        monkeypatch.setattr(agent, "ambient_agents_dir", lambda: own)
        assert agent.foreign_home_targets_shared_agents_dir(own) is None

    def test_predicate_is_silent_on_the_default_home(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _relocate_main_homes(monkeypatch, tmp_path)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        shared = tmp_path / "user" / ".kiro" / "agents"
        _pretend_target_is_shared(monkeypatch, agent, shared)
        assert agent.foreign_home_targets_shared_agents_dir(shared) is None

    def test_register_writes_nothing_into_the_shared_dir(self, monkeypatch, tmp_path, caplog):
        from types import SimpleNamespace

        _, bridges, shared, _ = self._foreign_on_shared(monkeypatch, tmp_path)
        before = sorted(p.name for p in shared.iterdir())
        with caplog.at_level("WARNING", logger=bridges.logger.name):
            registered = bridges._register_agents(
                "some-app", SimpleNamespace(agents=["agents/a.json"]), tmp_path / "app"
            )
        assert registered == []
        assert sorted(p.name for p in shared.iterdir()) == before
        assert "not writing agent specs into the shared" in caplog.text

    def test_deregister_and_prune_leave_the_shared_dir_alone(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        _, bridges, shared, _ = self._foreign_on_shared(monkeypatch, tmp_path)
        theirs = shared / (bridges._safe_link_name("some-app/agent") + ".json")
        theirs.write_text("{}", encoding="utf-8")

        assert bridges._deregister_agents("some-app") == 0
        bridges._prune_stale_app_resources(
            "some-app", SimpleNamespace(agents=[], skills=[], mcpServers={}), tmp_path / "app"
        )
        assert theirs.exists()


# --------------------------------------------------------------------------
# kirocrew doctor: name the drifted pin
# --------------------------------------------------------------------------
def _write_spec(agents_dir: Path, name: str, servers: dict) -> Path:
    agents_dir.mkdir(parents=True, exist_ok=True)
    p = agents_dir / name
    p.write_text(json.dumps({"name": name[:-5], "mcpServers": servers}), encoding="utf-8")
    return p


class TestDoctorSpecHomeDrift:
    @pytest.fixture
    def own_home(self, monkeypatch, tmp_path: Path) -> Path:
        home = tmp_path / "gateway-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        return home.resolve()

    def test_a_managed_spec_pinning_another_home_is_drift(self, own_home, tmp_path):
        from kiro_crew.doctor_spec_home import check_spec_home_drift

        agents = tmp_path / "agents"
        foreign = tmp_path / "scratch" / "runtime-3c0a7e8b" / "pfshot" / "home"
        _write_spec(
            agents,
            "kirocrew.json",
            {
                "kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": str(foreign)}},
                "kirocrew-cron": {"command": "kirocrew", "env": {"KIROCREW_HOME": str(foreign)}},
                "other-tool": {"command": "x"},
            },
        )

        report = check_spec_home_drift(agents_dir=agents)

        assert report.expected == str(own_home)
        assert report.scanned == 1
        assert sorted(d.server for d in report.managed) == ["kirocrew-core", "kirocrew-cron"]
        assert all(d.pinned == str(foreign) and d.managed for d in report.drift)
        assert report.foreign == []

    def test_a_matching_pin_and_no_pin_are_both_healthy(self, own_home, tmp_path):
        from kiro_crew.doctor_spec_home import check_spec_home_drift

        agents = tmp_path / "agents"
        _write_spec(
            agents,
            "kirocrew.json",
            {
                # Pinned to OUR home, spelled with a trailing slash.
                "kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": f"{own_home}/"}},
                # A default-home writer pins nothing; not a finding.
                "kirocrew-cron": {"command": "kirocrew"},
            },
        )
        report = check_spec_home_drift(agents_dir=agents)
        assert report.drift == []
        assert report.scanned == 1

    def test_a_foreign_spec_is_reported_apart_from_managed_ones(self, own_home, tmp_path):
        from kiro_crew.doctor_spec_home import check_spec_home_drift

        agents = tmp_path / "agents"
        _write_spec(
            agents,
            "some-aim-agent.json",
            {"kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": "/elsewhere"}}},
        )
        report = check_spec_home_drift(agents_dir=agents)
        assert report.managed == []
        assert [d.spec for d in report.foreign] == ["some-aim-agent.json"]

    def test_foreign_drift_is_named_but_never_an_issue(self, own_home, tmp_path, capsys):
        """A third-party spec is not this install's to repair and
        ``setup --agent-only`` does not touch it, so doctor prints the ⚠️ line and
        exits zero -- a nonzero exit the operator cannot clear would only teach
        them to ignore the section."""
        from kiro_crew.doctor_spec_home import doctor_spec_home_drift

        agents = tmp_path / "agents"
        _write_spec(
            agents,
            "some-aim-agent.json",
            {"kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": "/elsewhere"}}},
        )
        issues: list[str] = []
        doctor_spec_home_drift(issues, agents_dir=agents)
        out = capsys.readouterr().out
        assert "some-aim-agent.json" in out and "foreign spec" in out
        assert issues == []

    def test_a_custom_server_inside_an_owned_spec_is_not_managed(self, own_home, tmp_path):
        """``setup --agent-only`` rewrites only Kiro Crew's OWN server entries and
        preserves a user-added one, so a drifted pin on a custom server inside
        ``kirocrew.json`` would survive the remedy doctor names for managed
        drift. It is reported apart, as edit-by-hand."""
        from kiro_crew.doctor_spec_home import check_spec_home_drift

        agents = tmp_path / "agents"
        _write_spec(
            agents,
            "kirocrew.json",
            {
                "kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": "/elsewhere"}},
                "my-custom-srv": {"command": "srv", "env": {"KIROCREW_HOME": "/elsewhere"}},
            },
        )
        report = check_spec_home_drift(agents_dir=agents)
        assert [d.server for d in report.managed] == ["kirocrew-core"]
        assert [d.server for d in report.foreign] == ["my-custom-srv"]

    def test_a_pin_is_compared_lexically_and_never_touches_the_filesystem(
        self, own_home, tmp_path, monkeypatch
    ):
        """A spec pin is untrusted text. On Windows ``Path.resolve()`` OPENS a
        UNC path, so a pin of ``\\\\attacker\\share`` would authenticate to that
        host during a doctor walk. The comparison must be lexical: no stat, no
        resolve on the pin -- and the pin is still reported as drift."""
        import os as _os

        from kiro_crew import doctor_spec_home

        agents = tmp_path / "agents"
        unc = "\\\\attacker\\share\\home"
        _write_spec(agents, "kirocrew.json", {"kirocrew-core": {"env": {"KIROCREW_HOME": unc}}})

        touched: list[str] = []
        real_stat = _os.stat

        def _spy(path, *a, **k):
            touched.append(str(path))
            return real_stat(path, *a, **k)

        monkeypatch.setattr(_os, "stat", _spy)
        report = doctor_spec_home.check_spec_home_drift(agents_dir=agents)

        assert [d.pinned for d in report.drift] == [unc]
        assert not [p for p in touched if "attacker" in p], touched
        # The normaliser itself is filesystem-free by construction (code body,
        # after its docstring).
        src = Path(doctor_spec_home.__file__).read_text(encoding="utf-8")
        body = src.split("def _lexical(")[1].split("\ndef ")[0].split('"""')[-1]
        assert "resolve(" not in body and "stat(" not in body and "exists(" not in body

    def test_a_tilde_user_pin_never_consults_the_account_database(self, monkeypatch):
        """``~name/...`` makes ``os.path.expanduser`` look the name up in the
        account database -- an external probe on spec-supplied text. Only a bare
        ``~`` / ``~/`` prefix is expanded; anything else compares as written."""
        import os as _os

        from kiro_crew import doctor_spec_home

        def _boom(*a, **k):  # pragma: no cover - the assertion is that it is not called
            raise AssertionError("expanduser consulted for a ~name pin")

        monkeypatch.setattr(_os.path, "expanduser", _boom)
        assert doctor_spec_home._lexical("~attacker/crew") == _os.path.normcase(
            _os.path.normpath("~attacker/crew")
        )
        assert doctor_spec_home._lexical("/plain/home/") == _os.path.normcase("/plain/home")

    def test_a_bare_tilde_pin_still_expands(self, monkeypatch, tmp_path):
        from kiro_crew import doctor_spec_home

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        expected = doctor_spec_home._lexical(tmp_path / ".kiro" / "crew")
        assert doctor_spec_home._lexical("~/.kiro/crew") == expected

    def test_malformed_specs_are_skipped_not_fatal(self, own_home, tmp_path):
        from kiro_crew.doctor_spec_home import check_spec_home_drift

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "broken.json").write_text("{not json", encoding="utf-8")
        (agents / "list.json").write_text("[1, 2]", encoding="utf-8")
        _write_spec(agents, "kirocrew.json", {"kirocrew-core": {"env": "not-an-object"}})
        report = check_spec_home_drift(agents_dir=agents)
        assert report.scanned == 1  # only kirocrew.json parsed as an object
        assert report.drift == []

    def test_missing_agents_dir_is_empty_not_an_error(self, own_home, tmp_path):
        from kiro_crew.doctor_spec_home import check_spec_home_drift

        report = check_spec_home_drift(agents_dir=tmp_path / "nope")
        assert report.scanned == 0 and report.drift == []

    def test_renderer_flags_managed_drift_with_the_remedy(self, own_home, tmp_path, capsys):
        from kiro_crew.doctor_spec_home import doctor_spec_home_drift

        agents = tmp_path / "agents"
        _write_spec(
            agents,
            "kirocrew.json",
            {"kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": "/scratch/x"}}},
        )
        issues: list[str] = []
        doctor_spec_home_drift(issues, agents_dir=agents)
        out = capsys.readouterr().out
        assert "Agent Spec Data Home" in out
        assert "kirocrew.json" in out and "KIROCREW_HOME=/scratch/x" in out
        assert "kirocrew setup --agent-only" in out
        assert issues == ["agent specs pin a different KIROCREW_HOME than this data home"]

    def test_renderer_is_green_and_silent_in_issues_when_healthy(self, own_home, tmp_path, capsys):
        from kiro_crew.doctor_spec_home import doctor_spec_home_drift

        agents = tmp_path / "agents"
        _write_spec(agents, "kirocrew.json", {"kirocrew-core": {"command": "kirocrew"}})
        issues: list[str] = []
        doctor_spec_home_drift(issues, agents_dir=agents)
        assert "✅" in capsys.readouterr().out
        assert issues == []

    def test_renderer_neutralizes_terminal_controls_in_a_pin(self, own_home, tmp_path, capsys):
        """A spec is untrusted input; an escape byte in its pin must not reach the
        terminal raw (same rule as the dead-path check)."""
        from kiro_crew.doctor_spec_home import doctor_spec_home_drift

        agents = tmp_path / "agents"
        _write_spec(
            agents,
            "kirocrew.json",
            {"kirocrew-core": {"env": {"KIROCREW_HOME": "/x\x1b]0;pwned\x07"}}},
        )
        doctor_spec_home_drift([], agents_dir=agents)
        out = capsys.readouterr().out
        assert "\x1b" not in out and "\\x1b" in out

    @requires_symlinks
    def test_specs_are_read_through_the_hardened_gate(self, own_home, tmp_path, monkeypatch):
        """Every spec read goes through ``agent_discovery._read_agent_spec`` -- the
        one reader that refuses a symlink whose RESOLVED target is sensitive and
        caps the size -- never a bare ``read_text``. Driven through the reader's
        own sensitive-path refusal, the way the hardened-read suite does."""
        from kiro_crew import agent_discovery
        from kiro_crew.doctor_spec_home import check_spec_home_drift

        agents = tmp_path / "agents"
        agents.mkdir()
        protected = tmp_path / "protected.json"
        protected.write_text(
            json.dumps({"mcpServers": {"kirocrew-core": {"env": {"KIROCREW_HOME": "/x"}}}}),
            encoding="utf-8",
        )
        (agents / "kirocrew.json").symlink_to(protected)
        monkeypatch.setattr(
            agent_discovery, "is_sensitive_path", lambda p: str(protected) in str(p)
        )

        report = check_spec_home_drift(agents_dir=agents)

        assert report.scanned == 0, "a link to a protected target must not be parsed"
        assert report.drift == []


# --------------------------------------------------------------------------
# Wiring ratchets
# --------------------------------------------------------------------------
def test_cli_prologue_adopts_the_kiro_home_after_the_data_home():
    """Every ``kirocrew`` verb shares one prologue; the adoption must sit in it,
    after ``ensure_data_home()`` (the override is validated there) and before any
    subcommand dispatch. Source-level so the ordering itself is what is pinned."""
    src = (REPO_ROOT / "src" / "kiro_crew" / "cli.py").read_text(encoding="utf-8")
    body = src.split("def main(", 1)[1]
    ensure_at = body.index("ensure_data_home()")
    adopt_at = body.index("adopt_isolated_kiro_home()")
    dispatch_at = body.index("if args.command is None:")
    assert ensure_at < adopt_at < dispatch_at


def test_doctor_runs_the_spec_home_check_beside_the_trust_root():
    src = (REPO_ROOT / "src" / "kiro_crew" / "cli_doctor.py").read_text(encoding="utf-8")
    assert re.search(
        r"_doctor_trust_root\(\)\s*\n(?:\s*#.*\n)*\s*doctor_spec_home_drift\(issues, agents_dir=_agents_dir\(\)\)",
        src,
    ), "doctor must run the spec-home drift check right after the trust-root check"


def test_adoption_reprimes_the_loaded_unc_agents_root(monkeypatch, tmp_path):
    """``hooks`` memoizes the agents dir (the UNC gate's trusted root) keyed on
    KIRO_HOME and primes it at import, which precedes the prologue. After the
    export the memo is stale and the FIRST gate check would resolve the path on
    whatever thread asked -- the event loop, on a UNC home an SMB round-trip. The
    re-prime is coupled INSIDE ``adopt_isolated_kiro_home`` so no entrypoint can
    adopt without it: after adoption the memo already answers the adopted dir."""
    from kiro_crew import hooks

    _relocate_main_homes(monkeypatch, tmp_path)
    scratch = tmp_path / "scratch-home"
    scratch.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(scratch))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    # The suite's agents-dir pin defers to a set KIRO_HOME, so the memo will
    # resolve the real (adopted) location once the export lands.
    hooks._unc_agents_root()  # primed under the PRE-adoption configuration
    stale_key = hooks._unc_agents_root_cache[0]

    adopted = adopt_isolated_kiro_home()

    assert adopted is not None
    assert hooks._unc_agents_root_cache is not None
    assert hooks._unc_agents_root_cache[0] != stale_key, "memo not re-primed after adoption"
    assert hooks._unc_agents_root_cache[0][0] == str(adopted)
    assert hooks._unc_agents_root_cache[1] == isolated_agents_dir(scratch.resolve())


def test_reprime_seeds_the_unc_root_lexically_without_resolving(monkeypatch, tmp_path):
    """Outside the suite's agents-dir pin (production), the re-prime does not
    re-resolve the adopted home: the memo is seeded with the lexical
    ``<data home>/kiro/agents`` under the key the gate will look up, and no
    stat/lstat is issued -- the data home was resolved once by ``config_dir()``
    and a second round-trip on a UNC-backed home would sit on the boot path."""
    import os as _os

    from kiro_crew import hooks

    _relocate_main_homes(monkeypatch, tmp_path)
    scratch = tmp_path / "scratch-home"
    scratch.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(scratch))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.setattr(paths, "_agents_dir_override", None)
    paths.config_dir()
    monkeypatch.setattr(paths.logger, "info", lambda *a, **k: None)
    hooks._unc_agents_root()  # primed under the PRE-adoption configuration
    expected_root = isolated_agents_dir(scratch.resolve())

    calls: list[str] = []
    for name in ("stat", "lstat", "readlink"):
        real = getattr(_os, name)

        def _spy(*a, _n=name, _real=real, **k):
            calls.append(_n)
            return _real(*a, **k)

        monkeypatch.setattr(_os, name, _spy)

    adopted = adopt_isolated_kiro_home()

    assert adopted is not None and calls == [], calls
    assert hooks._unc_agents_root_cache == (
        (str(adopted), paths.kiro_agents_dir, None),
        expected_root,
    )
    # And the seeded entry is what the gate reads back: a lookup is a key
    # comparison, still no filesystem.
    assert hooks._unc_agents_root() == expected_root and calls == []


class TestDoctorKiroHome:
    """The Data Home section names the kiro home this instance reads and, on an
    adopted (isolated) home, what the host ``~/.kiro`` still holds that it skips --
    the upgrade-transition signal for a permanently relocated install."""

    def test_shared_default_home_says_nothing(self, monkeypatch, tmp_path, capsys):
        from kiro_crew import cli_doctor

        _relocate_main_homes(monkeypatch, tmp_path)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        cli_doctor._doctor_kiro_home(tmp_path / "user" / ".kiro" / "crew")
        assert capsys.readouterr().out == ""

    def test_the_opt_out_is_named_as_the_shared_host_home(self, monkeypatch, tmp_path, capsys):
        """``KIRO_HOME=~/.kiro`` on a non-default data home is the one configuration
        where this instance reads the default instance's specs; the line says so
        rather than staying silent as it does on the default home."""
        from kiro_crew import cli_doctor

        _relocate_main_homes(monkeypatch, tmp_path)
        user = tmp_path / "user"
        (user / ".kiro").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
        scratch = tmp_path / "relocated-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", str(user / ".kiro"))

        cli_doctor._doctor_kiro_home(scratch.resolve())

        out = capsys.readouterr().out
        assert out.count("\n") == 1 and "kiro home:" in out
        assert "shared host home" in out and "never writes" in out

    def test_isolated_home_with_host_content_names_the_opt_out(self, monkeypatch, tmp_path, capsys):
        from kiro_crew import cli_doctor

        _relocate_main_homes(monkeypatch, tmp_path)
        user = tmp_path / "user"
        (user / ".kiro" / "sessions" / "cli").mkdir(parents=True)
        (user / ".kiro" / "sessions" / "cli" / "old.json").write_text("{}", encoding="utf-8")
        (user / ".kiro" / "steering").mkdir()
        (user / ".kiro" / "steering" / "team.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
        scratch = tmp_path / "relocated-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", str(isolated_kiro_home(scratch)))

        cli_doctor._doctor_kiro_home(scratch.resolve())

        out = capsys.readouterr().out
        assert "kiro home:" in out and "isolated" in out
        assert "sessions, steering" in out
        assert f"export KIRO_HOME={user / '.kiro'}" in out

    def test_isolated_home_with_empty_host_is_one_line(self, monkeypatch, tmp_path, capsys):
        from kiro_crew import cli_doctor

        _relocate_main_homes(monkeypatch, tmp_path)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", str(isolated_kiro_home(scratch)))

        cli_doctor._doctor_kiro_home(scratch.resolve())

        out = capsys.readouterr().out
        assert out.count("\n") == 1 and "kiro home:" in out
        assert "host home" not in out

    def test_an_unreadable_host_subtree_does_not_abort_doctor(self, monkeypatch, tmp_path, capsys):
        """A read-only diagnostic must survive a host subtree it cannot list.
        Simulated rather than chmod'ed so the case also runs as root and on
        Windows, where mode bits do not produce ``PermissionError``."""
        from kiro_crew import cli_doctor

        _relocate_main_homes(monkeypatch, tmp_path)
        user = tmp_path / "user"
        (user / ".kiro" / "skills").mkdir(parents=True)
        (user / ".kiro" / "steering").mkdir()
        (user / ".kiro" / "steering" / "team.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
        scratch = tmp_path / "relocated-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", str(isolated_kiro_home(scratch)))
        real_iterdir = Path.iterdir

        def _iterdir(self):
            if self.name == "skills":
                raise PermissionError(13, "Permission denied", str(self))
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _iterdir)

        cli_doctor._doctor_kiro_home(scratch.resolve())

        out = capsys.readouterr().out
        assert "still holds steering" in out
        assert "skills" not in out.split("still holds", 1)[1].split("\n", 1)[0]

    def test_a_shared_spec_still_pinning_this_home_is_named(self, monkeypatch, tmp_path, capsys):
        """The leftover of a relocated install that wrote the shared specs before it
        owned an isolated kiro home: ``~/.kiro/agents/kirocrew.json`` still pins
        THIS data home, the DEFAULT instance's sessions verify against it and fail,
        and nothing on this instance rewrites that file any more. Doctor names it
        with the remedy that runs on the other instance -- a ⚠️ line, not an issue."""
        from kiro_crew import cli_doctor

        _relocate_main_homes(monkeypatch, tmp_path)
        user = tmp_path / "user"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
        scratch = tmp_path / "relocated-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", str(isolated_kiro_home(scratch)))
        _write_spec(
            user / ".kiro" / "agents",
            "kirocrew.json",
            {
                "kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": str(scratch)}},
                "kirocrew-cron": {"command": "kirocrew", "env": {"KIROCREW_HOME": "/other"}},
                # A user-added server the rebuild preserves: pinning this home there
                # is not something ``setup --agent-only`` would clear.
                "my-tool": {"command": "my-tool", "env": {"KIROCREW_HOME": str(scratch)}},
            },
        )

        cli_doctor._doctor_kiro_home(scratch.resolve())

        out = capsys.readouterr().out
        assert "shared spec:" in out and "still pins THIS data home" in out
        first = out.split("shared spec:", 1)[1].split("\n", 1)[0]
        assert "kirocrew-core" in first
        assert "kirocrew-cron" not in first and "my-tool" not in first
        assert "kirocrew setup --agent-only" in out

    def test_no_shared_spec_line_when_the_pin_is_someone_elses(self, monkeypatch, tmp_path, capsys):
        from kiro_crew import cli_doctor

        _relocate_main_homes(monkeypatch, tmp_path)
        user = tmp_path / "user"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
        scratch = tmp_path / "scratch-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        monkeypatch.setenv("KIRO_HOME", str(isolated_kiro_home(scratch)))
        _write_spec(
            user / ".kiro" / "agents",
            "kirocrew.json",
            {"kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": "/other"}}},
        )

        cli_doctor._doctor_kiro_home(scratch.resolve())

        assert "shared spec:" not in capsys.readouterr().out


class TestAdoptedHomeKeepsSessionMap:
    """An install that already ran on a non-default ``KIROCREW_HOME`` has its
    transcripts under the host ``~/.kiro/sessions/cli``. The first start after the
    prologue gives it its own kiro home must not read every mapped transcript as
    gone and prune the map: the transcripts move with the mapping."""

    @staticmethod
    def _relocated_install(monkeypatch, tmp_path):
        from kiro_crew import session_map as sm_mod

        _relocate_main_homes(monkeypatch, tmp_path)
        user = tmp_path / "user"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
        scratch = tmp_path / "relocated-home"
        scratch.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(scratch))
        # What the prologue exports on this install after the upgrade.
        monkeypatch.setenv("KIRO_HOME", str(isolated_kiro_home(scratch)))
        monkeypatch.setattr(paths, "_sessions_dir_override", None)
        monkeypatch.setattr(sm_mod, "_KIRO_SESSIONS_DIR", None)
        host_sessions = user / ".kiro" / "sessions" / "cli"
        host_sessions.mkdir(parents=True)
        return sm_mod, scratch, host_sessions

    def test_upgrade_moves_transcripts_and_the_map_survives_prune(self, monkeypatch, tmp_path):
        sm_mod, scratch, host_sessions = self._relocated_install(monkeypatch, tmp_path)
        # Pre-upgrade state: two of this instance's sessions, transcripts on the host.
        journal = '{"role": "user", "content": "hi"}\n'
        (host_sessions / "sid-a.json").write_text("{}", encoding="utf-8")
        (host_sessions / "sid-a.jsonl").write_text(journal, encoding="utf-8")
        (host_sessions / "sid-b.json").write_text("{}", encoding="utf-8")
        (host_sessions / "sid-b.jsonl").write_text(journal, encoding="utf-8")
        # The default instance's transcript, not ours: must stay where it is.
        (host_sessions / "sid-theirs.json").write_text("{}", encoding="utf-8")
        sm = sm_mod.SessionMap()
        sm.set("dashboard:one", "sid-a")
        sm.set("dashboard:two", "sid-b")

        moved = sm.reclaim_adopted_transcripts()
        pruned = sm.prune()

        adopted = isolated_kiro_home(scratch) / "sessions" / "cli"
        assert moved == 2 and pruned == 0
        assert sm.get("dashboard:one") == "sid-a" and sm.get("dashboard:two") == "sid-b"
        assert (adopted / "sid-a.json").is_file() and (adopted / "sid-a.jsonl").is_file()
        assert (adopted / "sid-b.json").is_file() and (adopted / "sid-b.jsonl").is_file()
        assert not (host_sessions / "sid-a.json").exists()
        assert not (host_sessions / "sid-b.jsonl").exists()
        assert (host_sessions / "sid-theirs.json").is_file()
        # Idempotent: a second start finds nothing left to move.
        assert sm.reclaim_adopted_transcripts() == 0

    def test_nothing_moves_under_the_opt_out(self, monkeypatch, tmp_path):
        """``KIRO_HOME=~/.kiro`` keeps reading the host directory, so there is
        nothing to reclaim and the host directory is not touched."""
        sm_mod, _, host_sessions = self._relocated_install(monkeypatch, tmp_path)
        monkeypatch.setenv("KIRO_HOME", str(host_sessions.parent.parent))
        (host_sessions / "sid-a.json").write_text("{}", encoding="utf-8")
        (host_sessions / "sid-a.jsonl").write_text(
            '{"role": "user", "content": "hi"}\n', encoding="utf-8"
        )
        sm = sm_mod.SessionMap()
        sm.set("dashboard:one", "sid-a")

        assert sm.reclaim_adopted_transcripts() == 0
        assert (host_sessions / "sid-a.json").is_file()
        assert sm.prune() == 0 and sm.get("dashboard:one") == "sid-a"

    def test_nothing_moves_on_the_default_home(self, monkeypatch, tmp_path):
        sm_mod, _, host_sessions = self._relocated_install(monkeypatch, tmp_path)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        (host_sessions / "sid-a.json").write_text("{}", encoding="utf-8")
        sm = sm_mod.SessionMap()
        sm.set("dashboard:one", "sid-a")

        assert sm.reclaim_adopted_transcripts() == 0
        assert (host_sessions / "sid-a.json").is_file()

    def test_a_sid_that_is_not_a_bare_filename_is_skipped(self, monkeypatch, tmp_path):
        sm_mod, scratch, host_sessions = self._relocated_install(monkeypatch, tmp_path)
        (host_sessions / "escape.json").write_text("{}", encoding="utf-8")
        sm = sm_mod.SessionMap()
        sm._data["dashboard:x"] = {"sid": "../escape"}

        assert sm.reclaim_adopted_transcripts() == 0
        assert (host_sessions / "escape.json").is_file()
        assert not (isolated_kiro_home(scratch) / "sessions").exists()

    def test_a_half_finished_move_is_completed_on_the_next_start(self, monkeypatch, tmp_path):
        """The journal moves before the transcript, and each file is judged on its
        own, so a move that dies between the two is not read as done: the next
        start moves what is still on the host side and nothing is stranded."""
        import shutil as _shutil

        sm_mod, scratch, host_sessions = self._relocated_install(monkeypatch, tmp_path)
        journal = '{"role": "user", "content": "hi"}\n'
        (host_sessions / "sid-a.json").write_text("{}", encoding="utf-8")
        (host_sessions / "sid-a.jsonl").write_text(journal, encoding="utf-8")
        sm = sm_mod.SessionMap()
        sm.set("dashboard:one", "sid-a")
        adopted = isolated_kiro_home(scratch) / "sessions" / "cli"

        real_move = _shutil.move

        def _die_on_transcript(src, dst):
            if str(src).endswith(".json"):
                raise OSError("simulated failure mid-migration")
            return real_move(src, dst)

        monkeypatch.setattr(sm_mod.shutil, "move", _die_on_transcript)
        assert sm.reclaim_adopted_transcripts() == 1
        assert (adopted / "sid-a.jsonl").is_file() and not (adopted / "sid-a.json").exists()
        assert (host_sessions / "sid-a.json").is_file()  # still on the host side

        monkeypatch.setattr(sm_mod.shutil, "move", real_move)
        assert sm.reclaim_adopted_transcripts() == 1
        assert (adopted / "sid-a.json").is_file() and (adopted / "sid-a.jsonl").is_file()
        assert not (host_sessions / "sid-a.json").exists()
        assert sm.prune() == 0 and sm.get("dashboard:one") == "sid-a"


def test_only_the_ownership_guard_consults_the_ambient_agents_dir():
    """The generalizable rule behind this fix: a writer of the SHARED agents dir
    must first ask whether this instance owns it (``foreign_data_home()``), and
    that question is asked in exactly one place -- ``agent._unexempt_shared_target``,
    behind both ``_decline_shared_agent_home`` (the managed spec) and
    ``foreign_home_targets_shared_agents_dir`` (app specs, via ``apps.bridges``).
    A new module reaching for ``ambient_agents_dir()`` directly would be a writer
    (or reader) that bypasses that ownership decision, which is how the shared
    specs got poisoned in the first place. Every other consumer goes through
    ``kiro_agents_dir()``, which the guard sits behind."""
    src_root = REPO_ROOT / "src" / "kiro_crew"
    callers: dict[str, int] = {}
    for path in src_root.rglob("*.py"):
        if path.name == "paths.py" and path.parent.name == "config":
            continue  # the resolver's own definition
        text = path.read_text(encoding="utf-8")
        # Prose mentions (``ambient_agents_dir()`` in a docstring, or a comment
        # line) are not calls.
        n = sum(
            1
            for line in text.splitlines()
            if "ambient_agents_dir()" in line
            and "``ambient_agents_dir()``" not in line
            and not line.lstrip().startswith("#")
        )
        if n:
            callers[path.relative_to(src_root).as_posix()] = n
    assert callers == {"agent.py": 1}, callers
