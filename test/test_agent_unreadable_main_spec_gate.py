"""The main-agent start gate and the launch bound while ``kirocrew.json`` cannot be read.

Which starts the episode refuses: keyed on the spec FILE a name resolves to (a renamed
main agent, a stem-only match, a project spec), the bare-name launch bound that binds a
start with no prepared view to the episode and admits it while the kept spec stays
unreadable, the readability probe, and a launch view bound to the revision it was built
against. The skip, the sibling pass and the early retry are in
``test_agent_unreadable_main_spec.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401
from agent_helpers import _run_install
from mcp_merge_helpers import bundled_defaults as _bundled_defaults
from unreadable_spec_helpers import custom_spec


class TestUnreadableMainSpecGate:
    """Which starts an open unreadable-main-spec episode refuses, and which it admits."""

    _custom_spec = staticmethod(custom_spec)

    def _renamed_main(self, tmp_path: Path, held_flag, declared_before: str):
        """A ``kirocrew.json`` that now declares ``custom-main``, with the gate armed.

        *declared_before* is the name the last WRITTEN spec declared: the rename
        may land after that write, so the gate cannot rely on remembering it.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        data = json.loads(spec.read_text(encoding="utf-8"))
        data["name"] = declared_before
        spec.write_text(json.dumps(data), encoding="utf-8")
        _run_install(tmp_path, cfg_dir)
        assert held_flag._main_spec_declared_name == declared_before
        data = json.loads(spec.read_text(encoding="utf-8"))
        data["name"] = "custom-main"
        spec.write_text(json.dumps(data), encoding="utf-8")
        held_flag._main_spec_unreadable = True
        return cfg_dir, spec

    def test_a_renamed_main_agent_is_gated_by_its_spec_file(self, tmp_path: Path, held_flag):
        """``kirocrew.json`` may declare its own ``name``: the gate follows the FILE.

        Renamed after the last write, so no remembered name matches it. While the
        file cannot be read the name resolves to nothing and is gated. Once it reads
        again it resolves to ``kirocrew.json`` and stays gated until a rebuild
        writes it. Mutation pin: a gate keyed on the fixed and remembered names
        admits ``custom-main`` once the file reads.
        """
        cfg_dir, spec = self._renamed_main(tmp_path, held_flag, declared_before="kirocrew")
        real_read = held_flag._read_spec_capped

        def _unreadable_spec(path):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read(path)

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            with patch("kiro_crew.agent._read_spec_capped", _unreadable_spec):
                assert held_flag.agent_spec_path("custom-main") is None
                with pytest.raises(held_flag.ForkGovernanceUnresolved, match="'kirocrew'"):
                    held_flag.require_main_spec_projected("custom-main")
            # Readable again, not yet rebuilt: found by resolving to the file.
            assert held_flag.agent_spec_path("custom-main") == spec
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("custom-main")
            _run_install(tmp_path, cfg_dir)
            held_flag.require_main_spec_projected("custom-main")
        assert held_flag._main_spec_unreadable is False
        assert held_flag._main_spec_declared_name == "custom-main"

    def test_an_agent_on_its_own_spec_is_not_gated(self, tmp_path: Path, held_flag):
        """A start resolving to another spec that declares it is admitted."""
        _cfg_dir, spec = self._renamed_main(tmp_path, held_flag, declared_before="kirocrew")
        (spec.parent / "helper.json").write_text(
            json.dumps({"name": "helper", "tools": []}), encoding="utf-8"
        )
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            held_flag.require_main_spec_projected("helper")

    def test_a_stem_only_match_is_gated_while_the_main_spec_cannot_be_read(
        self, tmp_path: Path, held_flag
    ):
        """A file found by its stem alone loses to any spec that declares the name.

        A nameless spec, or one declaring another name, is what the resolver
        falls back to only while no readable spec declares the name. The
        unreadable main spec may be that spec once it reads again, and kiro-cli
        would then run it, so the start is gated for the episode. A file that
        declares the name is admitted, and a hard link to the main spec under
        another stem is the main spec's file and stays gated. Mutation pin:
        admitting every name that resolves to a non-main file admits
        ``my-agent``.
        """
        spec = self._custom_spec(tmp_path)
        (spec.parent / "my-agent.json").write_text(json.dumps({"tools": []}), encoding="utf-8")
        (spec.parent / "declared.json").write_text(
            json.dumps({"name": "declared", "tools": []}), encoding="utf-8"
        )
        held_flag._main_spec_unreadable = True
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            assert held_flag.agent_spec_path("my-agent") == spec.parent / "my-agent.json"
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("my-agent")
            held_flag.require_main_spec_projected("declared")
            os.link(spec, spec.parent / "linked-main.json")
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("linked-main")
        held_flag._main_spec_unreadable = False
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            held_flag.require_main_spec_projected("my-agent")

    def test_a_renamed_main_on_another_specs_stem_is_refused_with_one_denial(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """The reported bypass: the main agent renamed to another spec's file stem.

        ``kirocrew.json`` now declares ``code-review-sage-reviewer``, renamed after
        the last write, so no remembered name matches it.
        ``code-review-sage-reviewer.json`` declares ``helper``. While the main
        spec cannot be read the name resolves to that file by stem; once read
        access returns, kiro-cli resolves it to the main spec, which declares it.
        The gate refuses it through the shared refusal: one SEL denial and one
        early retry. Mutation pin: admitting a stem match on another file
        (``return resolved is None``) starts it.
        """
        spec = self._custom_spec(tmp_path)
        data = json.loads(spec.read_text(encoding="utf-8"))
        data["name"] = "code-review-sage-reviewer"
        spec.write_text(json.dumps(data), encoding="utf-8")
        (spec.parent / "code-review-sage-reviewer.json").write_text(
            json.dumps({"name": "helper", "tools": []}), encoding="utf-8"
        )
        held_flag._main_spec_unreadable = True
        audited: list[str] = []
        retries: list[bool] = []
        monkeypatch.setattr(
            held_flag, "_audit_main_spec_denial", lambda agent, **_: audited.append(agent)
        )
        monkeypatch.setattr(
            held_flag, "_schedule_main_spec_early_retry", lambda: retries.append(True)
        )
        real_read = held_flag._read_spec_capped

        def _unreadable_spec(path):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read(path)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent._read_spec_capped", _unreadable_spec),
        ):
            assert (
                held_flag.agent_spec_path("code-review-sage-reviewer")
                == spec.parent / "code-review-sage-reviewer.json"
            )
            with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
                held_flag.require_fork_governance("code-review-sage-reviewer")
        assert audited == ["code-review-sage-reviewer"]
        assert retries == [True]

    def test_an_unbound_launch_is_refused_for_the_episode(self, tmp_path: Path, held_flag):
        """A start kiro-cli resolves by name itself is bound to the episode.

        No file can be pinned for it, so while the episode is open it is refused,
        even for a name another readable spec declares; a name a project spec
        declares is admitted, since kiro-cli resolves the checkout first, unless
        it is one of the main spec's own names or the host prefers the user level.
        Outside an episode it returns at once. Mutation pin: checking only the
        gate's resolution admits ``helper``.
        """
        spec = self._custom_spec(tmp_path)
        (spec.parent / "helper.json").write_text(
            json.dumps({"name": "helper", "tools": []}), encoding="utf-8"
        )
        project = tmp_path / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        for name in ("proj", "kirocrew"):
            (project / ".kiro" / "agents" / f"{name}.json").write_text(
                json.dumps({"name": name, "tools": []}), encoding="utf-8"
            )
        bound = held_flag.require_main_spec_launch_bound
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            bound("helper")
            held_flag._main_spec_unreadable = True
            held_flag.require_main_spec_projected("helper")
            with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
                bound("helper")
            # A project spec declaring the name is no exemption either.
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                bound("proj", project)
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                bound("kirocrew", project)

    @pytest.mark.parametrize("form", ["json", "markdown", "linked-to-main"])
    def test_no_project_spec_exempts_a_start_while_an_episode_is_open(
        self, tmp_path: Path, held_flag, monkeypatch, form
    ):
        """A checkout's own spec of the name does not admit it while an episode is open.

        kiro-cli may not load the project file (a Markdown spec), the file may be
        the kept main spec under another name (a link), and a renamed main agent
        cannot be learned from a file that cannot be read, so no project spec is
        an exemption. A healthy host admits the same start. Mutation pin: an
        exemption for a name a project spec declares admits it during the episode.
        """
        spec = self._custom_spec(tmp_path)
        agents = tmp_path / "proj" / ".kiro" / "agents"
        agents.mkdir(parents=True)
        if form == "json":
            (agents / "fresh-name.json").write_text(
                json.dumps({"name": "fresh-name"}), encoding="utf-8"
            )
        elif form == "markdown":
            (agents / "fresh-name.md").write_text(
                "---\nname: fresh-name\ndescription: x\n---\nPrompt\n", encoding="utf-8"
            )
        else:
            if os.name == "nt":
                pytest.skip("symlink setup is POSIX here")
            (agents / "fresh-name.json").symlink_to(spec)
        audited: list[str] = []
        monkeypatch.setattr(
            held_flag, "_audit_main_spec_denial", lambda agent, **_: audited.append(agent)
        )
        monkeypatch.setattr(held_flag, "_schedule_main_spec_early_retry", lambda: None)
        bound = held_flag.require_main_spec_launch_bound
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            bound("fresh-name", tmp_path / "proj")
            held_flag._main_spec_unreadable = True
            with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
                bound("fresh-name", tmp_path / "proj")
        assert audited == ["fresh-name"]

    def test_a_read_by_name_refuses_an_episode_begun_or_open_around_it(
        self, tmp_path: Path, held_flag
    ):
        """*before* covers a read Crew makes itself: open then, open now, or begun between.

        Mutation pin: checking the flag only after the read admits the episode
        that opened and closed while the spec was read.
        """
        spec = self._custom_spec(tmp_path)
        bound = held_flag.require_main_spec_launch_bound
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            before = held_flag.main_spec_episode()
            bound("helper", before=before)
            held_flag._main_spec_episode_revision += 1
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                bound("helper", before=before)
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                bound("helper", before=(before[0] + 1, True))

    def test_a_launch_view_is_refused_only_while_an_episode_is_open(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """A view is admitted whenever no episode is open, whatever its source.

        A view built before an episode is admitted once a rebuild closes it, and
        so is a view of another file and one whose source is unknown. While an
        episode is open each of them is refused: no read across an episode can
        be shown not to be the kept main spec. Mutation pins: refusing a view
        built before an episode after the close refuses the three calls made
        after it; admitting a view during the episode writes fewer denials.
        """
        spec = self._custom_spec(tmp_path)
        audited: list[str] = []
        monkeypatch.setattr(
            held_flag, "_audit_main_spec_denial", lambda agent, **_: audited.append(agent)
        )
        monkeypatch.setattr(held_flag, "_schedule_main_spec_early_retry", lambda: None)
        bound = held_flag.require_main_spec_launch_bound
        main = spec.as_posix()
        other = (spec.parent / "helper.json").as_posix()
        views = (("kirocrew", main), ("kirocrew", ""), ("helper", other))
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
            for agent, source in views:
                bound(agent, view_source=source)
            # An episode opens and a rebuild closes it: the revision moved on.
            held_flag._main_spec_episode_revision += 1
            for agent, source in views:
                bound(agent, view_source=source)
            assert audited == []
            held_flag._main_spec_unreadable = True
            for agent, source in views:
                with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
                    bound(agent, view_source=source)
        assert audited == ["kirocrew", "kirocrew", "helper"]

    def test_a_name_matched_only_by_file_stem_is_gated_while_the_main_spec_cannot_be_read(
        self, tmp_path: Path, held_flag
    ):
        """Unreadable, the renamed spec cannot be found by its name, and another file can.

        ``custom-main.json`` declaring a different agent is what resolution falls
        back to by stem. No readable spec declares ``custom-main``, so the
        unreadable main spec may, and the start is gated.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        data = json.loads(spec.read_text(encoding="utf-8"))
        data["name"] = "custom-main"
        spec.write_text(json.dumps(data), encoding="utf-8")
        _run_install(tmp_path, cfg_dir)
        (spec.parent / "custom-main.json").write_text(
            json.dumps({"name": "someone-else", "tools": []}), encoding="utf-8"
        )
        held_flag._main_spec_unreadable = True
        real_read = held_flag._read_spec_capped

        def _unreadable_spec(path):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read(path)

        # A scoped read stub, not file modes, so every OS and a root run take it.
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent._read_spec_capped", _unreadable_spec),
        ):
            assert held_flag.agent_spec_path("custom-main") == spec.parent / "custom-main.json"
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("custom-main")

    def test_a_project_only_agent_is_not_governed(self, tmp_path: Path, held_flag):
        """kiro-cli resolves ``--agent`` in ``<cwd>/.kiro/agents`` first.

        A readable project spec declaring the name is what runs, so it is admitted.
        The same name with no project is unresolvable and refused, and the main
        agent stays governed although the project declares it too.
        """
        spec = self._custom_spec(tmp_path)
        held_flag._main_spec_unreadable = True
        real_capped = held_flag._read_spec_capped

        def _capped(path):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return real_capped(path)

        project = tmp_path / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / "proj.json").write_text(
            json.dumps({"name": "proj", "tools": []}), encoding="utf-8"
        )
        (project / ".kiro" / "agents" / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "tools": []}), encoding="utf-8"
        )
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent._read_spec_capped", _capped),
        ):
            held_flag.require_fork_governance("proj", project)
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("proj")
            with pytest.raises(held_flag.ForkGovernanceUnresolved):
                held_flag.require_main_spec_projected("kirocrew", project)
