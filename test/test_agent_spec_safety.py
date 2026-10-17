"""Tests for reading, resetting and re-materialising a spec safely.

``ensure_agent_materialized``'s self-heal, ``agent_spec_path``'s name and
symlink checks, size-capped reads, ``reset_agent_model``'s ambiguity refusal, the
reset CLI's output escaping, and the redaction of a rejected hook's SEL command.
"""

from __future__ import annotations

import argparse
import json
import unittest.mock
from pathlib import Path

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401

from conftest import requires_symlinks

# ── ensure_agent_materialized (self-heal for kiro-cli "Mode not found") ──


def test_ensure_agent_materialized_noop_for_non_managed_agent(tmp_path, monkeypatch):
    """A non-managed (app/custom) agent can't be regenerated here → returns
    False and never touches rebuild_agent_config."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)
    rebuild = unittest.mock.MagicMock()
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    assert agent_mod.ensure_agent_materialized("some-app-agent") is False
    rebuild.assert_not_called()


def test_ensure_agent_materialized_present_is_noop(tmp_path, monkeypatch):
    """Managed default already on disk → True, no regeneration."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / agent_mod.AGENT_FILENAME).write_text("{}", encoding="utf-8")
    rebuild = unittest.mock.MagicMock()
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    managed = Path(agent_mod.AGENT_FILENAME).stem
    assert agent_mod.ensure_agent_materialized(managed) is True
    rebuild.assert_not_called()


def test_ensure_agent_materialized_regenerates_when_missing(tmp_path, monkeypatch):
    """Managed default missing → rebuild_agent_config is invoked and the file
    is materialized (the reporter's fresh-checkout case)."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)

    def _fake_rebuild(*_a, **_k):
        path = tmp_path / agent_mod.AGENT_FILENAME
        path.write_text("{}", encoding="utf-8")
        return path

    rebuild = unittest.mock.MagicMock(side_effect=_fake_rebuild)
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    managed = Path(agent_mod.AGENT_FILENAME).stem
    assert agent_mod.ensure_agent_materialized(managed) is True
    rebuild.assert_called_once()


def test_ensure_agent_materialized_swallows_errors(tmp_path, monkeypatch):
    """Best-effort: a rebuild failure never propagates (it sits on the spawn
    hot path) — returns False instead."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)
    rebuild = unittest.mock.MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    managed = Path(agent_mod.AGENT_FILENAME).stem
    assert agent_mod.ensure_agent_materialized(managed) is False


class TestAgentSpecPathRejectsTraversal:
    """``agent_spec_path`` validates the name BEFORE the path join.

    The path it returns is one ``reset_agent_model`` then WRITES, and the CLI
    takes the name from a user-supplied ``--agent``, so a traversal would rewrite
    an arbitrary JSON file and strip its ``model`` key. The guard lives at the
    resolver so every caller inherits it.
    """

    def test_traversal_is_refused_and_the_outside_file_is_untouched(
        self, tmp_path: Path, monkeypatch
    ):
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        outsider = tmp_path / "victim.json"
        original = json.dumps({"model": "claude-opus-4.8", "keep": 1})
        outsider.write_text(original, encoding="utf-8")
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        assert agent_mod.agent_spec_path("../victim") is None
        with pytest.raises(FileNotFoundError):
            agent_mod.reset_agent_model("../victim")
        assert outsider.read_text(encoding="utf-8") == original

    @pytest.mark.parametrize(
        "name",
        [
            "../victim",
            "a/b",
            "..",
            ".hidden",
            "trailing.",
            "",
            "with space",
            "sub/../../x",
            "tab\tname",
            "reviewer.v2\n",
        ],
    )
    def test_names_outside_the_grammar_are_refused(self, tmp_path: Path, monkeypatch, name):
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)
        assert agent_mod.agent_spec_path(name) is None

    @pytest.mark.parametrize("name", ["my-agent_2", "reviewer.v2", "a" * 64])
    def test_a_valid_name_still_resolves(self, tmp_path: Path, monkeypatch, name):
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)
        assert agent_mod.agent_spec_path(name) == agents / f"{name}.json"


class TestSpecPathRefusesSymlinks:
    """A spec is read and then written back, so a symlink is refused, not followed.

    Following one copies the target's contents into the agents directory, which
    launders a file the reader may not otherwise be allowed to open into a freely
    readable location.
    """

    @requires_symlinks
    def test_a_symlinked_spec_is_refused_and_the_target_is_not_copied(
        self, tmp_path: Path, monkeypatch
    ):
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        secret = tmp_path / "protected.json"
        secret.write_text(json.dumps({"model": "leaked", "secret": "s"}), encoding="utf-8")
        link = agents / "kirocrew.json"
        link.symlink_to(secret)
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        assert agent_mod.agent_spec_path("kirocrew") is None
        with pytest.raises(FileNotFoundError):
            agent_mod.reset_agent_model("kirocrew")
        # The link is intact and nothing was copied into the agents directory.
        assert link.is_symlink()
        assert json.loads(secret.read_text(encoding="utf-8"))["secret"] == "s"

    @requires_symlinks
    def test_the_name_scan_also_skips_a_symlink(self, tmp_path: Path, monkeypatch):
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"name": "wanted", "model": "x"}), encoding="utf-8")
        (agents / "some-file.json").symlink_to(outside)
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        assert agent_mod.agent_spec_path("wanted") is None

    def test_a_plain_spec_is_still_accepted(self, tmp_path: Path, monkeypatch):
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "kirocrew.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        assert agent_mod.agent_spec_path("kirocrew") == agents / "kirocrew.json"


class TestSpecPathPrefersTheDeclaredName:
    """A declared ``name`` wins over a matching filename.

    The caller WRITES to the path this returns, so selecting ``<name>.json``
    when that file declares a different agent clears the wrong agent's pin and
    leaves the requested one pinned. Matches the order the repo's other two
    resolvers already use.
    """

    def _dir(self, tmp_path: Path, monkeypatch) -> Path:
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)
        return agents

    def test_a_filename_declaring_another_agent_is_not_selected(self, tmp_path: Path, monkeypatch):
        """The resolver prefers the declared name. (The WRITE path additionally
        refuses this state outright -- see TestResetRefusesAnAmbiguousName --
        because the runtime's choice between the two files is undefined.)"""
        import kiro_crew.agent as agent_mod

        agents = self._dir(tmp_path, monkeypatch)
        (agents / "foo.json").write_text(
            json.dumps({"name": "bar", "model": "bar-model"}), encoding="utf-8"
        )
        (agents / "elsewhere.json").write_text(
            json.dumps({"name": "foo", "model": "foo-model"}), encoding="utf-8"
        )

        assert agent_mod.agent_spec_path("foo") == agents / "elsewhere.json"

    def test_a_mismatched_filename_is_used_when_nothing_declares_the_name(
        self, tmp_path: Path, monkeypatch
    ):
        """`foo.json` declaring `bar`, with nothing declaring `foo`: the runtime
        matches it by STEM, so it is the live spec for `--agent foo` and refusing
        it would leave a live pin unresettable."""
        import kiro_crew.agent as agent_mod

        agents = self._dir(tmp_path, monkeypatch)
        (agents / "foo.json").write_text(
            json.dumps({"name": "bar", "model": "m"}), encoding="utf-8"
        )

        assert agent_mod.agent_spec_path("foo") == agents / "foo.json"
        spec_path, previous = agent_mod.reset_agent_model("foo")
        assert spec_path.name == "foo.json"
        assert previous == "m"

    def test_the_filename_is_accepted_when_it_declares_no_name(self, tmp_path: Path, monkeypatch):
        import kiro_crew.agent as agent_mod

        agents = self._dir(tmp_path, monkeypatch)
        (agents / "foo.json").write_text(json.dumps({"model": "m"}), encoding="utf-8")
        assert agent_mod.agent_spec_path("foo") == agents / "foo.json"

    def test_the_filename_is_accepted_when_it_declares_the_same_name(
        self, tmp_path: Path, monkeypatch
    ):
        import kiro_crew.agent as agent_mod

        agents = self._dir(tmp_path, monkeypatch)
        (agents / "foo.json").write_text(json.dumps({"name": "foo"}), encoding="utf-8")
        assert agent_mod.agent_spec_path("foo") == agents / "foo.json"


class TestResetRefusesAnAmbiguousName:
    """Two specs claiming one name is refused, not guessed.

    The runtime resolver accepts EITHER a declared-name match or a filename
    match and iterates an unordered glob, so which of the two is live is
    undefined. Clearing either could leave the live pin in place and strip the
    model from a spec nothing reads.
    """

    def test_a_declared_match_plus_a_filename_match_is_refused(self, tmp_path: Path, monkeypatch):
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "foo.json").write_text(
            json.dumps({"name": "bar", "model": "bar-model"}), encoding="utf-8"
        )
        (agents / "elsewhere.json").write_text(
            json.dumps({"name": "foo", "model": "foo-model"}), encoding="utf-8"
        )
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        with pytest.raises(ValueError) as exc:
            agent_mod.reset_agent_model("foo")
        assert "undefined" in str(exc.value)
        # NEITHER file was touched.
        assert json.loads((agents / "foo.json").read_text(encoding="utf-8"))["model"] == (
            "bar-model"
        )
        assert json.loads((agents / "elsewhere.json").read_text(encoding="utf-8"))["model"] == (
            "foo-model"
        )
        assert agent_mod.agent_state.get_model_managed("foo") is None

    def test_an_unambiguous_declared_match_still_resets(self, tmp_path: Path, monkeypatch):
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "elsewhere.json").write_text(
            json.dumps({"name": "foo", "model": "foo-model"}), encoding="utf-8"
        )
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        spec_path, previous = agent_mod.reset_agent_model("foo")
        assert spec_path.name == "elsewhere.json"
        assert previous == "foo-model"

    def test_two_specs_declaring_the_same_name_is_refused(self, tmp_path: Path, monkeypatch):
        """Same undefined-liveness argument as the filename collision: the
        runtime iterates unordered, so a writer cannot pick."""
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "a.json").write_text(
            json.dumps({"name": "dup", "model": "a-model"}), encoding="utf-8"
        )
        (agents / "b.json").write_text(
            json.dumps({"name": "dup", "model": "b-model"}), encoding="utf-8"
        )
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        with pytest.raises(ValueError) as exc:
            agent_mod.agent_spec_path("dup")
        assert "undefined" in str(exc.value)

        with pytest.raises(ValueError):
            agent_mod.reset_agent_model("dup")
        # Neither model was cleared.
        assert json.loads((agents / "a.json").read_text(encoding="utf-8"))["model"] == "a-model"
        assert json.loads((agents / "b.json").read_text(encoding="utf-8"))["model"] == "b-model"
        assert agent_mod.agent_state.get_model_managed("dup") is None


class TestSpecReadsAreSizeCapped:
    """Spec reads go through the hardened, size-capped gate.

    The agents directory is user-writable and shared with other tools, so an
    oversized file there must be refused rather than slurped into memory. Uses a
    LOWERED cap instead of a real 50 MB fixture -- writing 50 MB in a test is not
    acceptable, and the property under test is "the cap is consulted", not its
    value. Verified against the real 50 MB cap once by hand before landing.
    """

    def test_an_oversized_spec_is_refused_by_resolver_and_reset(self, tmp_path: Path, monkeypatch):
        import kiro_crew.agent as agent_mod
        from kiro_crew import hooks

        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "model": "m", "pad": "x" * 1024}), encoding="utf-8"
        )
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        assert agent_mod.agent_spec_path("kirocrew") is None
        with pytest.raises(FileNotFoundError):
            agent_mod.reset_agent_model("kirocrew")

    def test_a_normal_sized_spec_under_the_same_cap_still_resets(self, tmp_path: Path, monkeypatch):
        """The A-side of the cap test: proves the refusal above is the SIZE, not
        the lowered cap breaking every read."""
        import kiro_crew.agent as agent_mod
        from kiro_crew import hooks

        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "model": "claude-opus-4.8"}), encoding="utf-8"
        )
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        _, previous = agent_mod.reset_agent_model("kirocrew")
        assert previous == "claude-opus-4.8"


class TestResetOutputEscapesUntrustedPaths:
    """A spec FILENAME is untrusted input too.

    The declared-name scan returns whichever file declares the requested name, so
    its path is attacker-shaped even though the requested name is
    grammar-validated. Every line that prints one -- success, no-pin, and the
    ambiguity refusal -- must escape it.

    The hostile path is INJECTED rather than created on disk: control bytes are
    illegal in a Windows filename, so building the fixture would make these
    assertions Windows-only-skipped, and the property under test is "the printer
    escapes what it is handed", not "the filesystem accepts odd names". Keeping
    the assertion platform-independent follows the conftest rule of probing or
    injecting rather than blanket-skipping a platform.
    """

    HOSTILE = "/tmp/agents/evil\x1b[2J.json"

    def test_the_success_line_escapes_the_path(self, monkeypatch, capsys):
        import kiro_crew.cli_commands as cli_commands

        monkeypatch.setattr(
            cli_commands,
            "reset_agent_model",
            lambda name: (Path(self.HOSTILE), "claude-opus-4.8"),
        )

        cli_commands._agent_reset_model(argparse.Namespace(agent="kirocrew"))

        out = capsys.readouterr().out
        assert "Cleared" in out
        assert "\x1b" not in out, "raw escape from a spec filename reached the terminal"
        assert "\\x1b" in out, "the path is still shown, just escaped"

    def test_the_no_pin_line_escapes_the_path(self, monkeypatch, capsys):
        import kiro_crew.cli_commands as cli_commands

        monkeypatch.setattr(
            cli_commands, "reset_agent_model", lambda name: (Path(self.HOSTILE), "")
        )

        cli_commands._agent_reset_model(argparse.Namespace(agent="kirocrew"))

        out = capsys.readouterr().out
        assert "had no pinned model" in out
        assert "\x1b" not in out
        assert "\\x1b" in out

    def test_the_ambiguity_refusal_escapes_both_paths(self, tmp_path: Path, monkeypatch):
        """The refusal message names two paths; both come off disk."""
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "kirocrew.json").write_text(json.dumps({"name": "other"}), encoding="utf-8")
        (agents / "elsewhere.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)
        # Inject the hostile path as the CONFLICTING file, portably.
        monkeypatch.setattr(agent_mod, "_conflicting_spec_for", lambda n, c, d: Path(self.HOSTILE))

        with pytest.raises(ValueError) as exc:
            agent_mod.reset_agent_model("kirocrew")
        assert "\x1b" not in str(exc.value)
        assert "\\x1b" in str(exc.value)

    def test_the_duplicate_name_refusal_escapes_paths(self, tmp_path: Path, monkeypatch):
        """The other refusal builds its message from a list of real spec paths."""
        import kiro_crew.agent as agent_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "a.json").write_text(json.dumps({"name": "dup"}), encoding="utf-8")
        (agents / "b.json").write_text(json.dumps({"name": "dup"}), encoding="utf-8")
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)

        with pytest.raises(ValueError) as exc:
            agent_mod.agent_spec_path("dup")
        # Both paths are repr'd, so a control byte in either could not execute.
        assert "'" in str(exc.value), "paths are quoted (repr), not raw"
        assert "a.json" in str(exc.value) and "b.json" in str(exc.value)


class TestSelHookRejectedRedaction:
    """``_sel_hook_rejected`` must redact ``command`` before its 200-char cut.

    The old spelling sliced ``command[:200]`` inside the f-string and redacted
    the assembled message afterwards, so a credential cut at the boundary lost
    its tail, stopped matching the credential regex, and the raw prefix escaped
    into the SEL audit row.
    """

    def _capture_sel(self, monkeypatch) -> list:
        import kiro_crew.agent as agent_mod

        events: list = []

        class _Log:
            def log(self, event) -> None:
                events.append(event)

        monkeypatch.setattr(agent_mod, "sel", lambda: _Log())
        return events

    def test_credential_straddling_the_cut_is_not_leaked(self, monkeypatch) -> None:
        from kiro_crew.agent import _sel_hook_rejected

        events = self._capture_sel(monkeypatch)
        # fabricated AKIA-shaped literal, inlined (a ``secret``-named binding
        # trips CodeQL's name-based sensitive-source heuristic); the 200-char
        # cut lands 8 chars into the 20-char key
        command = "x" * 192 + "AKIAIOSFODNN7EXAMPLE" + " --flag"
        _sel_hook_rejected("preToolUse", command, "denied")
        assert len(events) == 1
        assert "AKIA" not in events[0].resources

    def test_plain_command_truncation_unchanged(self, monkeypatch) -> None:
        """Ordinary path is result-preserving: no secret ⇒ the same 200-char slice."""
        from kiro_crew.agent import _sel_hook_rejected

        events = self._capture_sel(monkeypatch)
        _sel_hook_rejected("preToolUse", "c" * 250, "denied")
        assert len(events) == 1
        assert events[0].resources == f"event=preToolUse command={'c' * 200}"
