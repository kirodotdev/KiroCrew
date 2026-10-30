"""The default agent: carrying a pre-template default forward, and resetting a dangling one.

A "Default for new sessions" pick made before plain sessions were template
sessions is carried into ``agent.default_agent`` once; a default whose template
file is gone is pointed back at ``default`` by the next landed snapshot refresh,
and every other state leaves config.json as it is.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture(autouse=True)
def _no_crewmate_files_from_other_tests(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no crewmate-owned names in the dispatch snapshot.

    A crewmate's own file chats on the conductor
    (``mate_worker.member_chat_agent``), so a name an earlier test in this worker
    published (``atlas``, say) would read as a crewmate's file here.
    """
    from kiro_crew import mate_worker
    from kiro_crew.config import loader

    _floor_monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset())
    # A cold snapshot rescans the crewmate folder, which tests in this worker share.
    _floor_monkeypatch.setattr(mate_worker, "is_mate_owned_agent", lambda name: False)


class TestStarDefaultTemplateCarry:
    """A "Default for new sessions" pick from before plain sessions were template
    sessions -- an enrolled alias plus the roster's ``default_agent`` -- is carried
    into ``agent.default_agent`` once, so an upgrade keeps running what the user chose."""

    @staticmethod
    def _star_config(**agent_section: str) -> dict:
        return {
            "agent": dict(agent_section),
            "default_agent": "atlas",
            "agents": {
                "default": {
                    "kiro_agent": "kirocrew",
                    "workspace": "default",
                    "memory_store": "default",
                },
                "atlas": {"kiro_agent": "atlas", "workspace": "default", "memory_store": "default"},
            },
        }

    def _load(self, tmp_path: Path, data: dict) -> tuple[KiroCrewConfig, dict]:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        cfg_file = home / "config.json"
        cfg_file.write_text(json.dumps(data), encoding="utf-8")
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=home),
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            unittest.mock.patch(
                "kiro_crew.config.loader.config_local_path", return_value=home / "config.local.json"
            ),
        ):
            cfg = KiroCrewConfig.load()
        return cfg, json.loads(cfg_file.read_text(encoding="utf-8"))

    def test_a_filename_stem_default_template_dispatches_the_declared_name(self) -> None:
        """``agent.default_agent`` may spell a spec's file stem; a plain session
        must start the name the file declares, as a crewmate's row does."""
        from kiro_crew.config import loader

        cfg = KiroCrewConfig()
        cfg.agent.default_agent = "KiroPkg-captain"
        with (
            unittest.mock.patch.object(loader, "_MATERIALIZED_AGENTS_READY", True),
            unittest.mock.patch.object(loader, "_MATERIALIZED_AGENTS", {"captain", "kirocrew"}),
            unittest.mock.patch.object(
                loader, "_MATERIALIZED_STEMS", {"KiroPkg-captain": "captain"}
            ),
            unittest.mock.patch.object(loader, "_edition_agent_names", return_value=frozenset()),
        ):
            assert loader.resolve_agent_bindings(cfg, None).kiro_agent == "captain"
            assert loader.resolve_agent_identity(cfg, None)[1] == "captain"

    def test_a_pre_rule_star_pick_still_drives_a_plain_session(self, tmp_path: Path) -> None:
        from kiro_crew.config import loader

        cfg, on_disk = self._load(tmp_path, self._star_config(default_agent=""))
        # In memory on this very load, and on disk for the next one.
        assert cfg.agent.default_agent == "atlas"
        assert loader.default_template(cfg) == "atlas"
        assert loader.resolve_agent_bindings(cfg, None).kiro_agent == "atlas"
        assert on_disk["agent"]["default_agent"] == "atlas"
        # The roster is untouched: the alias row and the default crewmate stay.
        assert on_disk["default_agent"] == "atlas"
        assert set(on_disk["agents"]) == {"default", "atlas"}

    @pytest.mark.parametrize("chosen", ["nova", "kirocrew"])
    def test_an_explicit_template_default_is_not_overwritten(self, tmp_path: Path, chosen) -> None:
        """A stored value is a choice, the stock template included: resetting the
        star to ``kirocrew`` leaves the alias row and the roster default in place,
        and the next load must not carry the alias's template back over it."""
        cfg, on_disk = self._load(tmp_path, self._star_config(default_agent=chosen))
        assert cfg.agent.default_agent == chosen
        assert on_disk["agent"]["default_agent"] == chosen

    def test_a_stock_roster_is_a_no_op(self, tmp_path: Path) -> None:
        data = self._star_config(default_agent="")
        data["default_agent"] = "default"
        cfg, on_disk = self._load(tmp_path, data)
        assert cfg.agent.default_agent == ""
        assert on_disk["agent"]["default_agent"] == ""

    @pytest.mark.parametrize(
        "row",
        [
            {"kiro_agent": "default-2", "workspace": "default", "memory_store": "default"},
            {
                "kiro_agent": "default-2",
                "workspace": "work",
                "memory_store": "default",
                "model": "m",
            },
        ],
        ids=["template-only", "own-folder-and-model"],
    )
    def test_the_default_crewmates_template_still_drives_a_plain_session(
        self, tmp_path: Path, row
    ) -> None:
        """Plain sessions ran the ``default`` crewmate's template before they were
        template sessions; an upgrade keeps that template, and only that: the
        row's folder and model pin stay the crewmate's own."""
        from kiro_crew.config import loader

        data = self._star_config(default_agent="")
        data["default_agent"] = "default"
        data["agents"]["default"] = row
        cfg, on_disk = self._load(tmp_path, data)
        assert cfg.agent.default_agent == "default-2"
        assert on_disk["agent"]["default_agent"] == "default-2"
        bound = loader.resolve_agent_bindings(cfg, None)
        assert bound.kiro_agent == "default-2"
        assert bound.memory_store_name == "default"
        assert loader.resolve_agent_identity(cfg, None)[2] == ""
        # The crewmate row itself is untouched.
        assert on_disk["agents"]["default"] == row
        assert on_disk["default_agent"] == "default"

    def test_a_seeded_default_roster_still_carries_its_template(self, tmp_path: Path) -> None:
        """A document with no roster default is seeded to ``default`` -- the row
        plain sessions read -- so its template is carried on the same load."""
        data = self._star_config(default_agent="")
        del data["default_agent"]
        data["agents"]["default"]["kiro_agent"] = "default-2"
        cfg, on_disk = self._load(tmp_path, data)
        assert cfg.default_agent == "default"
        assert cfg.agent.default_agent == "default-2"
        assert on_disk["agent"]["default_agent"] == "default-2"

    def test_a_seeded_first_alias_is_carried_on_the_same_load(self, tmp_path: Path) -> None:
        """With no ``default`` row the seed falls back to the first alias, whose
        template plain sessions read; once stored it reads as a pick, so the same
        load carries it rather than the next one."""
        data = self._star_config(default_agent="")
        del data["default_agent"]
        del data["agents"]["default"]
        cfg, on_disk = self._load(tmp_path, data)
        assert cfg.default_agent == "atlas"
        assert cfg.agent.default_agent == "atlas"
        assert on_disk["agent"]["default_agent"] == "atlas"
        cfg2, on_disk2 = self._load(tmp_path, on_disk)
        assert cfg2.agent.default_agent == "atlas"

    @pytest.mark.parametrize(
        "row, carried",
        [
            ({"kiro_agent": "atlas", "workspace": "work", "memory_store": "default"}, "atlas"),
            ({"kiro_agent": "atlas", "workspace": "default", "memory_store": "atlas-mem"}, "atlas"),
            (
                {
                    "kiro_agent": "atlas",
                    "workspace": "default",
                    "memory_store": "default",
                    "model": "m",
                },
                "atlas",
            ),
            (
                {"kiro_agent": "rust-expert", "workspace": "default", "memory_store": "default"},
                "rust-expert",
            ),
        ],
        ids=["own-folder", "own-store", "model-pin", "named-unlike-its-template"],
    )
    def test_a_crewmate_promoted_to_default_keeps_its_template(
        self, tmp_path: Path, row, carried
    ) -> None:
        """On main a star on a template an existing crewmate already runs points the
        roster default at that crewmate; plain sessions ran its template. Only the
        template is carried: the row's folder, store and model stay its own."""
        from kiro_crew.config import loader

        data = self._star_config(default_agent="")
        data["agents"]["atlas"] = row
        cfg, on_disk = self._load(tmp_path, data)
        assert cfg.agent.default_agent == carried
        assert on_disk["agent"]["default_agent"] == carried
        bound = loader.resolve_agent_bindings(cfg, None)
        assert bound.kiro_agent == carried
        assert bound.memory_store_name == "default"
        assert loader.resolve_agent_identity(cfg, None)[2] == ""
        assert on_disk["agents"]["atlas"] == row
        assert on_disk["default_agent"] == "atlas"

    def test_a_promoted_crewmate_on_the_stock_template_is_a_no_op(self, tmp_path: Path) -> None:
        data = self._star_config(default_agent="")
        data["agents"]["atlas"] = {
            "kiro_agent": "kirocrew",
            "workspace": "default",
            "memory_store": "default",
        }
        cfg, on_disk = self._load(tmp_path, data)
        assert cfg.agent.default_agent == ""
        assert on_disk["agent"]["default_agent"] == ""

    def test_the_carry_runs_once(self, tmp_path: Path) -> None:
        cfg, on_disk = self._load(tmp_path, self._star_config(default_agent=""))
        assert on_disk["agent"]["default_agent"] == "atlas"
        # The user then stars another template through the new write; a later
        # load must not put the alias back.
        on_disk["agent"]["default_agent"] = "nova"
        cfg2, on_disk2 = self._load(tmp_path, on_disk)
        assert cfg2.agent.default_agent == "nova"
        assert on_disk2["agent"]["default_agent"] == "nova"

    def test_the_predicate_is_pure(self) -> None:
        from kiro_crew.config.migration import star_default_template_due as due

        rows = {"atlas": {"kiro_agent": "atlas"}, "default": {"kiro_agent": "kirocrew"}}
        assert due("", "atlas", rows) == "atlas"
        assert due("", "atlas", {"atlas": {"kiro_agent": "nova"}}) == "nova"
        assert due("", "atlas", {"atlas": {"kiro_agent": "kirocrew"}}) is None
        assert due("kirocrew", "atlas", rows) is None
        assert due(None, "atlas", rows) == "atlas"
        assert due("nova", "atlas", rows) is None
        assert due("", "default", rows) is None
        assert (
            due("", "default", {"default": {"kiro_agent": "default-2", "model": "m"}})
            == "default-2"
        )
        assert due("x", "default", {"default": {"kiro_agent": "default-2"}}) is None
        assert due("", "", rows) is None
        assert due("", "ghost", rows) is None
        assert due("", "atlas", None) is None
        assert due("", "atlas", {"atlas": "not a row"}) is None


class TestDanglingDefaultAgentReset(unittest.TestCase):
    """A default agent whose template file is gone is pointed back at ``default``
    by the next landed snapshot refresh, and only then: a healthy, managed,
    cold, unreadable or concurrently changed state leaves ``config.json`` as it
    is, and the alias row is never edited.

    "Gone" means an agent an earlier landed snapshot declared that the latest
    landed snapshot does not. A template the directory never declared -- one a
    project checkout or the edition supplies -- may still dispatch, so it is
    not evidence of a removal."""

    def setUp(self):
        import gc

        import kiro_crew.config.loader as loader

        # Drain cyclic garbage left by any earlier test before this one starts.
        # The workspace-aware tests in this class can leave a PosixPath reference
        # cycle; without an explicit pass the finalizer fires inside the next
        # test here and attributes the unraisable exception to the wrong test.
        # Running in setUp also clears cycles left by earlier modules on the
        # same worker before the first test of this class.
        # Two passes cover chains of two-level cycles.
        gc.collect()
        gc.collect()

        loader._MATERIALIZED_AGENTS = frozenset()
        loader._MATERIALIZED_STEMS = {}
        loader._MATERIALIZED_SEEN.clear()
        loader._MATERIALIZED_LAST_DROPPED.clear()
        loader._MATERIALIZED_MATE_ONLY = frozenset()
        loader._MATERIALIZED_COMPLETE = False
        loader._MATERIALIZED_AGENTS_READY = False
        loader._MATERIALIZED_AGENTS_GENERATION = 0
        loader._MATERIALIZED_REFRESH_ISSUED = 0
        loader._MATERIALIZED_REFRESH_APPLIED = 0
        loader._invalidate_config_cache()

    tearDown = setUp

    def _home(self, tmp: Path, agents: dict[str, dict], default: str, files: dict[str, dict]):
        """A ``config.json`` naming *default* over *agents*, and an agents dir of *files*."""
        config = tmp / "config.json"
        config.write_text(
            json.dumps({"default_agent": default, "agents": agents}), encoding="utf-8"
        )
        agents_dir = tmp / "agents"
        agents_dir.mkdir()
        for filename, body in files.items():
            (agents_dir / filename).write_text(json.dumps(body), encoding="utf-8")
        return config, agents_dir

    def _patched(self, config: Path, agents_dir: Path):
        import kiro_crew.config.loader as loader

        return (
            unittest.mock.patch.object(loader, "config_path", lambda: config),
            unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: agents_dir),
        )

    @staticmethod
    def _on_disk(config: Path) -> dict:
        return json.loads(config.read_text(encoding="utf-8"))

    def test_dangling_default_is_reset_to_default_and_the_row_kept(self):
        # The owner made the ``pkg`` crew (running ``captain``) the default, then
        # the package was uninstalled. The landed refresh points the default back
        # at ``default``; the row stays, since the template may come back.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2, unittest.mock.patch("kiro_crew.sel.sel") as sel:
                loader.refresh_materialized_agents(heal_default=True)
                assert self._on_disk(config)["default_agent"] == "pkg"
                (d / "captain.json").unlink()
                with self.assertLogs("kiro_crew.config.loader", level="WARNING") as logs:
                    loader.refresh_materialized_agents(heal_default=True)
            on_disk = self._on_disk(config)
        assert on_disk["default_agent"] == "default"
        assert on_disk["agents"]["pkg"]["kiro_agent"] == "captain"
        assert any("'pkg'" in line and "'captain'" in line for line in logs.output)
        event = sel.return_value.log.call_args.args[0]
        assert event.event_type == "default_agent_reset"
        assert event.resources == "agent:pkg"
        assert event.metadata == {"template": "captain", "reset_to": "default"}

    def test_the_edition_lookup_with_no_context_does_not_re_enter_itself(self):
        # With no installed context, the edition lookup builds one, which loads
        # config, which can reach the lookup again. Unguarded, that runs to the
        # recursion limit, where a GC finalizer fails whichever test is running.
        import kiro_crew.config.loader as loader
        import kiro_crew.platform.context as context

        real = context.current_context
        depth = [0]
        peak = [0]

        def counting():
            depth[0] += 1
            peak[0] = max(peak[0], depth[0])
            try:
                return real()
            finally:
                depth[0] -= 1

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            with (
                p1,
                p2,
                unittest.mock.patch("kiro_crew.sel.sel"),
                unittest.mock.patch.object(context, "_ACTIVE", None),
                unittest.mock.patch.object(context, "current_context", counting),
            ):
                loader.refresh_materialized_agents(heal_default=True)
                (d / "captain.json").unlink()
                with self.assertLogs("kiro_crew.config.loader", level="WARNING"):
                    loader.refresh_materialized_agents(heal_default=True)
            on_disk = self._on_disk(config)
        assert on_disk["default_agent"] == "default"
        assert peak[0] == 1

    def test_an_unreadable_crewmate_file_is_not_removal_evidence(self):
        # The default is a crewmate whose own file sits in the crewmate folder.
        # That file turning unreadable leaves the user-level scan complete, but
        # must not read as the crewmate's file being gone.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "ada": {"kiro_agent": "ada"}},
                "ada",
                {"kirocrew.json": {"name": "kirocrew"}},
            )
            mates = Path(td) / "mates"
            mates.mkdir()
            (mates / "ada.json").write_text(json.dumps({"name": "ada"}), encoding="utf-8")
            p1, p2 = self._patched(config, d)
            p3 = unittest.mock.patch.object(loader, "mate_agents_dir", lambda: mates)
            with p1, p2, p3, unittest.mock.patch("kiro_crew.sel.sel"):
                loader.refresh_materialized_agents(heal_default=True)
                (mates / "ada.json").write_text("{torn", encoding="utf-8")
                loader.refresh_materialized_agents(heal_default=True)
                assert "ada" in loader._MATERIALIZED_AGENTS
            assert self._on_disk(config)["default_agent"] == "ada"

    def test_file_name_binding_counts_as_the_declared_agent(self):
        # A row that recorded the FILE name is healthy while that file declares
        # its agent, and dangling once the file is gone.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "KiroPkg-captain"}},
                "pkg",
                {
                    "kirocrew.json": {"name": "kirocrew"},
                    "KiroPkg-captain.json": {"name": "captain"},
                },
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                assert self._on_disk(config)["default_agent"] == "pkg"
                (d / "KiroPkg-captain.json").unlink()
                loader.refresh_materialized_agents(heal_default=True)
                assert self._on_disk(config)["default_agent"] == "default"

    def test_healthy_default_is_untouched(self):
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                assert loader.reset_dangling_default_agent() is False
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_cold_empty_or_failed_snapshot_acts_on_nothing(self):
        # No scan has landed, the scan found no agent at all, or the scan failed:
        # none of these says anything about one template, so nothing is written.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                assert loader.reset_dangling_default_agent() is False
                loader.refresh_materialized_agents(heal_default=True)  # lands an EMPTY snapshot
                assert loader._MATERIALIZED_AGENTS_READY is True
                assert self._on_disk(config)["default_agent"] == "pkg"
                (d / "kirocrew.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
                with unittest.mock.patch.object(
                    loader, "_scan_materialized_index", side_effect=OSError("boom")
                ):
                    loader.refresh_materialized_agents(heal_default=True)
                assert self._on_disk(config)["default_agent"] == "pkg"
                # The lazy build a lookup performs answers that lookup only.
                loader._MATERIALIZED_AGENTS_READY = False
                assert loader.dispatch_kiro_agent("captain") == "captain"
                assert loader._MATERIALIZED_AGENTS_READY is True
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_managed_template_is_left_to_its_own_self_heal(self):
        # A missing ``kirocrew.json`` is regenerated before spawn, so a default
        # bound to it -- explicitly or by an empty binding -- is not dangling.
        import kiro_crew.config.loader as loader

        for bound in ("kirocrew", ""):
            with tempfile.TemporaryDirectory() as td:
                config, d = self._home(
                    Path(td),
                    {"default": {"kiro_agent": "kirocrew"}, "mine": {"kiro_agent": bound}},
                    "mine",
                    {"other.json": {"name": "other"}},
                )
                p1, p2 = self._patched(config, d)
                with p1, p2:
                    loader.refresh_materialized_agents(heal_default=True)
                assert self._on_disk(config)["default_agent"] == "mine"

    def test_missing_default_alias_invents_no_target(self):
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"pkg": {"kiro_agent": "captain"}, "other": {"kiro_agent": "kirocrew"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                (d / "captain.json").unlink()
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_default_changed_inside_the_lock_wins(self):
        # A PUT that repointed the default between the check and the locked
        # read-modify-write is not overwritten: the mutate re-derives both
        # conditions on the document it actually holds.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {
                    "default": {"kiro_agent": "kirocrew"},
                    "pkg": {"kiro_agent": "captain"},
                    "other": {"kiro_agent": "kirocrew"},
                },
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            real_read = loader.read_config_for_update

            repointed: list[bool] = []

            def _repointed(path=None):
                repointed.append(True)
                data = real_read(path)
                data["default_agent"] = "other"
                config.write_text(json.dumps(data), encoding="utf-8")
                return data

            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                (d / "captain.json").unlink()
                with unittest.mock.patch.object(loader, "read_config_for_update", _repointed):
                    loader.refresh_materialized_agents(heal_default=True)
            # The refresh reached the locked write, and lost it to the PUT.
            assert repointed
            assert self._on_disk(config)["default_agent"] == "other"

    def test_unreadable_config_is_untouched(self):
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=False)
                with unittest.mock.patch.object(
                    loader.KiroCrewConfig, "load", classmethod(lambda cls: 1 / 0)
                ):
                    assert loader.reset_dangling_default_agent() is False
                with unittest.mock.patch.object(
                    loader, "read_config_for_update", side_effect=loader.ConfigReadError("torn")
                ):
                    assert loader.reset_dangling_default_agent() is False
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_a_refresh_that_does_not_heal_leaves_the_evidence_for_one_that_does(self):
        # A non-owner catalog fetch or a lazy lookup refreshes without healing.
        # When one of those is the first to see the file gone, the owner's next
        # refresh must still reset the default it left dangling.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                (d / "captain.json").unlink()
                loader.refresh_materialized_agents(heal_default=False)
                assert self._on_disk(config)["default_agent"] == "pkg"
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "default"

    def test_the_removal_evidence_is_bounded_and_evicts_the_oldest_removals_first(self):
        # The directory is user-writable, so the names it has ever declared are
        # bounded where they are kept. Past the count bound the removals seen
        # longest ago go first; a name the directory still declares is kept.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            cap = 8
            with p1, p2, unittest.mock.patch.object(loader, "_MATERIALIZED_SEEN_MAX", cap):
                for n in range(3 * cap):
                    (d / f"churn-{n}.json").write_text(
                        json.dumps({"name": f"churn-{n}"}), encoding="utf-8"
                    )
                    loader.refresh_materialized_agents(heal_default=False)
                    (d / f"churn-{n}.json").unlink()
                    loader.refresh_materialized_agents(heal_default=False)
                seen = list(loader._MATERIALIZED_SEEN)
                assert len(seen) <= cap
                # Still declared, so still newest: never the one evicted.
                assert {"kirocrew", "captain"} <= set(seen)
                assert "churn-0" not in seen and f"churn-{3 * cap - 1}" in seen
                # The bound did not cost the evidence a current name needs.
                (d / "captain.json").unlink()
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "default"

    def test_an_unchanged_oversized_directory_reports_its_evidence_drop_once(self):
        # Declared names and file-name bindings are one snapshot: past the bound
        # the drop is counted once for both, and an unchanged directory does not
        # report it again on the next refresh.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(Path(td), {"default": {"kiro_agent": "kirocrew"}}, "default", {})
            for n in range(6):
                (d / f"agent-{n}.json").write_text(json.dumps({"name": f"agent-{n}"}), "utf-8")
                (d / f"Pkg-x{n}.json").write_text(json.dumps({"name": f"x{n}"}), "utf-8")
            p1, p2 = self._patched(config, d)

            def evidence_lines(calls) -> list:
                return [
                    call
                    for call in calls
                    if any("removal-evidence" in str(arg) for arg in call.args)
                ]

            with p1, p2, unittest.mock.patch.object(loader, "_MATERIALIZED_SEEN_MAX", 8):
                with unittest.mock.patch.object(loader.logger, "warning") as warning:
                    loader.refresh_materialized_agents()
                    loader.refresh_materialized_agents()
                    loader.refresh_materialized_agents()
            assert len(evidence_lines(warning.call_args_list)) == 1

    def test_a_name_past_the_length_bound_is_not_kept_as_evidence(self):
        import kiro_crew.config.loader as loader

        long_name = "x" * (loader._MATERIALIZED_SEEN_NAME_MAX_CHARS + 1)
        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": long_name}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "long.json": {"name": long_name}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                assert long_name in loader._MATERIALIZED_AGENTS
                assert long_name not in loader._MATERIALIZED_SEEN
                (d / "long.json").unlink()
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_a_refresh_heals_only_when_its_caller_asks(self):
        # The reset writes the owner's config, so a plain refresh -- the kind a
        # turn, a lookup or an app registration triggers -- records the removal
        # and leaves the file alone; only an opted-in refresh writes it.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents()
                (d / "captain.json").unlink()
                loader.refresh_materialized_agents()
                assert self._on_disk(config)["default_agent"] == "pkg"
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "default"

    def test_a_local_file_cannot_redirect_a_binding_to_an_edition_agent(self):
        # ``captain`` is an agent the edition contributes. A user-level file
        # named ``captain.json`` that declares ``pilot`` must not turn a binding
        # to ``captain`` into a dispatch of ``pilot``; without the edition agent
        # the same file name still maps to the agent it declares.
        import kiro_crew.config.loader as loader

        edition = types.SimpleNamespace(
            agent_catalog=types.SimpleNamespace(builtin_agents=lambda: [{"name": "captain"}])
        )
        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}},
                "default",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "pilot"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents()
                assert loader._MATERIALIZED_STEMS == {"captain": "pilot"}
                assert loader.dispatch_kiro_agent("captain") == "pilot"
                with unittest.mock.patch(
                    "kiro_crew.platform.context.current_context", lambda: edition
                ):
                    assert loader.dispatch_kiro_agent("captain") == "captain"

    def test_the_file_name_map_is_bounded_where_it_is_built_and_reported_once(self):
        import kiro_crew.config.loader as loader

        long_name = "n" * (loader._MATERIALIZED_SEEN_NAME_MAX_CHARS + 1)
        files = {"kirocrew.json": {"name": "kirocrew"}, "long-target.json": {"name": long_name}}
        files.update({f"Pkg-{n}.json": {"name": f"agent-{n}"} for n in range(12)})
        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(Path(td), {"default": {"kiro_agent": "kirocrew"}}, "default", {})
            for filename, body in files.items():
                (d / filename).write_text(json.dumps(body), encoding="utf-8")
            p1, p2 = self._patched(config, d)
            with p1, p2, unittest.mock.patch.object(loader, "_MATERIALIZED_SEEN_MAX", 8):
                with unittest.mock.patch.object(loader.logger, "warning") as warning:
                    loader.refresh_materialized_agents()
                    stems = dict(loader._MATERIALIZED_STEMS)
                    # An unchanged directory is not reported again on the next refresh.
                    loader.refresh_materialized_agents()
                assert len(stems) <= 8
                assert all(
                    len(k) <= loader._MATERIALIZED_SEEN_NAME_MAX_CHARS
                    and len(v) <= loader._MATERIALIZED_SEEN_NAME_MAX_CHARS
                    for k, v in stems.items()
                )
                assert "long-target" not in stems
                reported = [
                    call
                    for call in warning.call_args_list
                    if any("file-name mapping" in str(arg) for arg in call.args)
                ]
                assert len(reported) == 1

    def test_kept_file_name_mappings_stay_within_the_bound(self):
        # An incomplete scan keeps the mappings of the specs it skipped on top
        # of the fresh ones it built, so the merged map is bounded where it is
        # retained: specs that stop parsing while new ones appear never push
        # it past the count bound.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(Path(td), {"default": {"kiro_agent": "kirocrew"}}, "default", {})
            p1, p2 = self._patched(config, d)
            with p1, p2, unittest.mock.patch.object(loader, "_MATERIALIZED_SEEN_MAX", 8):
                for batch in range(3):
                    for spec in d.glob("Pkg-*.json"):
                        spec.write_text("{torn", encoding="utf-8")
                    for n in range(8):
                        (d / f"Pkg-{batch}-{n}.json").write_text(
                            json.dumps({"name": f"agent-{batch}-{n}"}), encoding="utf-8"
                        )
                    loader.refresh_materialized_agents()
                    assert len(loader._MATERIALIZED_STEMS) <= 8
                # The fresh mappings win over the kept ones.
                assert all(stem.startswith("Pkg-2-") for stem in loader._MATERIALIZED_STEMS)

    def test_a_scan_that_could_not_read_every_spec_is_not_evidence_of_a_removal(self):
        # The default's spec is caught mid-write (or briefly unreadable) during
        # the owner's fetch. The scan skips it and keeps the name it declared
        # last, since the file is still there: the default must not be reset on that scan, and
        # once the file reads again nothing has changed. A real removal still
        # resets on the next complete scan.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                spec = d / "captain.json"
                good = spec.read_text(encoding="utf-8")
                spec.write_text('{"name": "capt', encoding="utf-8")  # torn
                loader.refresh_materialized_agents(heal_default=True)
                # The incomplete scan keeps the name the last snapshot had
                # and still never heals.
                assert "captain" in loader._MATERIALIZED_AGENTS
                assert loader._MATERIALIZED_COMPLETE is False
                assert self._on_disk(config)["default_agent"] == "pkg"
                spec.write_text(good, encoding="utf-8")
                loader.refresh_materialized_agents(heal_default=True)
                assert self._on_disk(config)["default_agent"] == "pkg"
                spec.unlink()
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "default"

    def test_a_partial_snapshot_installed_after_a_complete_scan_is_not_evidence(self):
        # Two refreshes overlap: a complete one scans, then a partial one (a torn
        # spec) installs its snapshot before the complete one's healer reads.
        # The healer reads the INSTALLED snapshot, so it must see that snapshot
        # is partial and leave the default alone.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents()
                (d / "captain.json").write_text('{"name": "capt', encoding="utf-8")
                loader.refresh_materialized_agents()  # the partial one lands
                assert loader.reset_dangling_default_agent() is False
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_a_partial_snapshot_installed_inside_the_locked_write_stops_it(self):
        # The healer checked a complete snapshot, then a partial one was
        # installed while it waited for the config lock: the locked mutate
        # re-reads the installed snapshot and declines.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {
                    "kirocrew.json": {"name": "kirocrew"},
                    "captain.json": {"name": "captain"},
                    "other.json": {"name": "other"},
                },
            )
            real_read = loader.read_config_for_update
            raced: list[bool] = []

            def _partial_lands_first(path=None):
                raced.append(True)
                (d / "other.json").write_text('{"name": "oth', encoding="utf-8")
                loader.refresh_materialized_agents()
                return real_read(path)

            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents()
                (d / "captain.json").unlink()
                loader.refresh_materialized_agents()  # complete: captain removed
                with unittest.mock.patch.object(
                    loader, "read_config_for_update", _partial_lands_first
                ):
                    assert loader.reset_dangling_default_agent() is False
            assert raced, "the locked write was reached"
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_a_default_its_workspace_still_declares_is_not_reset(self):
        # ``pkg`` runs ``reviewer`` in workspace ``proj``. The global
        # ``reviewer.json`` is uninstalled, but the project checkout still
        # declares ``reviewer``, and kiro-cli resolves ``--agent`` against the
        # project first, so the default still dispatches there: the removal of
        # the global namesake must not reset it.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "proj"
            (project / ".kiro" / "agents").mkdir(parents=True)
            (project / ".kiro" / "agents" / "reviewer.json").write_text(
                json.dumps({"name": "reviewer"}), encoding="utf-8"
            )
            config, d = self._home(
                root,
                {
                    "default": {"kiro_agent": "kirocrew"},
                    "pkg": {"kiro_agent": "reviewer", "workspace": "proj"},
                },
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "reviewer.json": {"name": "reviewer"}},
            )
            data = json.loads(config.read_text(encoding="utf-8"))
            data["workspaces"] = {"proj": {"dir": str(project)}}
            config.write_text(json.dumps(data), encoding="utf-8")
            loader._invalidate_config_cache()
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                (d / "reviewer.json").unlink()
                loader.refresh_materialized_agents(heal_default=True)
                assert self._on_disk(config)["default_agent"] == "pkg"
                # Once the checkout drops it too, the default is dangling.
                (project / ".kiro" / "agents" / "reviewer.json").unlink()
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "default"

    def _project_home(self, root: Path, workspace_dir: str, project_spec: str | None):
        """``pkg`` -> ``reviewer`` in workspace ``proj``, global ``reviewer.json`` present,
        and a checkout at ``root / "proj"`` whose ``reviewer.json`` holds *project_spec*."""
        import kiro_crew.config.loader as loader

        project = root / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        if project_spec is not None:
            (project / ".kiro" / "agents" / "reviewer.json").write_text(
                project_spec, encoding="utf-8"
            )
        config, d = self._home(
            root,
            {
                "default": {"kiro_agent": "kirocrew"},
                "pkg": {"kiro_agent": "reviewer", "workspace": "proj"},
            },
            "pkg",
            {"kirocrew.json": {"name": "kirocrew"}, "reviewer.json": {"name": "reviewer"}},
        )
        data = json.loads(config.read_text(encoding="utf-8"))
        data["workspaces"] = {"proj": {"dir": workspace_dir}}
        config.write_text(json.dumps(data), encoding="utf-8")
        loader._invalidate_config_cache()
        return config, d, project

    def test_a_relative_workspace_is_placed_under_the_data_home(self):
        # A relative ``dir`` is placed by ``workspace_dir_from_entry`` under the
        # data home, not the gateway's cwd; the checkout there still declares it.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, d, _project = self._project_home(root, "proj", json.dumps({"name": "reviewer"}))
            p1, p2 = self._patched(config, d)
            with p1, p2, unittest.mock.patch.object(loader, "config_dir", lambda: root):
                loader.refresh_materialized_agents(heal_default=True)
                (d / "reviewer.json").unlink()
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_a_workspace_spec_that_cannot_be_read_is_not_evidence_either(self):
        # The checkout's spec is torn: the project scan is partial, so it cannot
        # show the template is gone from there, and the healer declines.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, d, project = self._project_home(root, "", '{"name": "revi')
            data = json.loads(config.read_text(encoding="utf-8"))
            data["workspaces"]["proj"]["dir"] = str(project)
            config.write_text(json.dumps(data), encoding="utf-8")
            loader._invalidate_config_cache()
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                (d / "reviewer.json").unlink()
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_a_row_repointed_at_another_workspace_inside_the_locked_write_is_kept(self):
        # The healer checked workspace ``proj`` (empty), then the row was moved to
        # a workspace that was never checked while it waited for the config lock.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, d, project = self._project_home(root, "", None)
            data = json.loads(config.read_text(encoding="utf-8"))
            other = root / "other"
            (other / ".kiro" / "agents").mkdir(parents=True)
            data["workspaces"] = {"proj": {"dir": str(project)}, "other": {"dir": str(other)}}
            config.write_text(json.dumps(data), encoding="utf-8")
            loader._invalidate_config_cache()
            real_read = loader.read_config_for_update
            moved: list[bool] = []

            def _moved_in_the_window(path=None):
                current = real_read(path)
                current["agents"]["pkg"]["workspace"] = "other"
                moved.append(True)
                return current

            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                (d / "reviewer.json").unlink()
                loader.refresh_materialized_agents()
                with unittest.mock.patch.object(
                    loader, "read_config_for_update", _moved_in_the_window
                ):
                    assert loader.reset_dangling_default_agent() is False
            assert moved, "the locked write was reached"
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_a_template_the_directory_never_declared_is_not_reset(self):
        # ``scout`` runs ``reviewer``, which only a project checkout (or an
        # edition) declares: it dispatches, though no file in this directory
        # names it. Neither the boot refresh nor a later one may repoint the
        # owner's default on that absence.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "scout": {"kiro_agent": "reviewer"}},
                "scout",
                {"kirocrew.json": {"name": "kirocrew"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "scout"

    def test_a_removed_file_whose_agent_the_edition_supplies_is_not_reset(self):
        # The file went, but the edition contributes an agent of the same name,
        # so the default still dispatches.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}, "pkg": {"kiro_agent": "captain"}},
                "pkg",
                {"kirocrew.json": {"name": "kirocrew"}, "captain.json": {"name": "captain"}},
            )
            p1, p2 = self._patched(config, d)
            with p1, p2:
                loader.refresh_materialized_agents(heal_default=True)
                (d / "captain.json").unlink()
                with unittest.mock.patch(
                    "kiro_crew.platform.context.current_context",
                    lambda: types.SimpleNamespace(
                        agent_catalog=types.SimpleNamespace(
                            builtin_agents=lambda: [{"name": "captain"}]
                        )
                    ),
                ):
                    loader.refresh_materialized_agents(heal_default=True)
            assert self._on_disk(config)["default_agent"] == "pkg"

    def test_default_template_falls_back_when_the_configured_template_is_gone(self):
        # The owner made ``atlas`` the default for new sessions, then its file
        # went. A plain session must not be refused with an unavailable agent
        # mode: the read falls back to the managed template once the landed
        # snapshot shows the removal, and returns to ``atlas`` if it comes back.
        import kiro_crew.config.loader as loader

        cfg = types.SimpleNamespace(agent=types.SimpleNamespace(default_agent="atlas"))
        with tempfile.TemporaryDirectory() as td:
            _config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}},
                "default",
                {"kirocrew.json": {"name": "kirocrew"}, "atlas.json": {"name": "atlas"}},
            )
            p1, p2 = self._patched(_config, d)
            with p1, p2:
                # Cold snapshot: absence alone is not evidence, the name stands.
                assert loader.default_template(cfg) == "atlas"
                loader.refresh_materialized_agents()
                assert loader.default_template(cfg) == "atlas"
                (d / "atlas.json").unlink()
                loader.refresh_materialized_agents()
                assert loader.default_template(cfg) == loader.DEFAULT_KIRO_TEMPLATE
                (d / "atlas.json").write_text(json.dumps({"name": "atlas"}), encoding="utf-8")
                loader.refresh_materialized_agents()
                assert loader.default_template(cfg) == "atlas"

    def test_default_template_keeps_a_removed_file_the_edition_supplies(self):
        # The file went, but the edition contributes an agent of the same name:
        # a new session still dispatches it, so the configured name is kept. A
        # name the directory never declared is likewise not evidence.
        import kiro_crew.config.loader as loader

        cfg = types.SimpleNamespace(agent=types.SimpleNamespace(default_agent="atlas"))
        with tempfile.TemporaryDirectory() as td:
            _config, d = self._home(
                Path(td),
                {"default": {"kiro_agent": "kirocrew"}},
                "default",
                {"kirocrew.json": {"name": "kirocrew"}, "atlas.json": {"name": "atlas"}},
            )
            p1, p2 = self._patched(_config, d)
            with p1, p2:
                loader.refresh_materialized_agents()
                (d / "atlas.json").unlink()
                loader.refresh_materialized_agents()
                with unittest.mock.patch(
                    "kiro_crew.platform.context.current_context",
                    lambda: types.SimpleNamespace(
                        agent_catalog=types.SimpleNamespace(
                            builtin_agents=lambda: [{"name": "atlas"}]
                        )
                    ),
                ):
                    assert loader.default_template(cfg) == "atlas"
                never = types.SimpleNamespace(
                    agent=types.SimpleNamespace(default_agent="from-a-checkout")
                )
                assert loader.default_template(never) == "from-a-checkout"
