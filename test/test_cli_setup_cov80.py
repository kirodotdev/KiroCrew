"""``cli_setup`` — the two helpers that touch the user's own files unattended.

Both are run by ``kirocrew setup`` without asking, and neither is exercised by
the existing setup suites:

* ``_fix_shell_profiles`` REWRITES ``~/.zshrc`` and friends in place. It must
  delete a line only when it carries BOTH the stale marker and ``PATH`` — a
  looser match would eat an unrelated export — must leave a profile with no
  stale entry byte-identical (a rewrite churns mtime and invites a merge
  conflict for nothing), must swallow an unreadable profile rather than aborting
  setup, and must name every profile it touched so the user knows what to
  re-source.
* ``_find_electron_dir`` resolves the desktop-app sources. ``KIROCREW_PROJECT_DIR``
  must win over the walk-up, the walk-up must find a real checkout, and a
  pip-installed tree with no ``website/electron`` anywhere must return ``None``
  rather than a path that does not exist.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import kiro_crew.skills as skills_module
from kiro_crew import pinned_fs
from kiro_crew.cli_setup import (
    _find_electron_dir,
    _fix_shell_profiles,
    _remove_retired_conductor_skill,
    _setup_slash_command,
    _setup_whatsapp,
)
from kiro_crew.skills import RETIRED_CONDUCTOR_SKILL_SHA256

_STALE = 'export PATH="$HOME/.kirocrew-app/bin:$PATH"\n'


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at a tmp dir so no real profile is ever touched."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


class TestFixShellProfiles:
    def test_removes_a_stale_path_line_and_keeps_everything_else(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        zshrc = home / ".zshrc"
        zshrc.write_text(f"# zibble\n{_STALE}alias q=quux\n", encoding="utf-8")

        _fix_shell_profiles()

        assert zshrc.read_text(encoding="utf-8") == "# zibble\nalias q=quux\n"
        out = capsys.readouterr().out
        assert ".zshrc" in out
        assert "source ~/.zshrc" in out

    def test_a_marker_line_without_path_is_left_alone(self, home: Path) -> None:
        """The match is marker AND ``PATH`` — a bare mention must not be deleted."""
        bashrc = home / ".bashrc"
        original = "# see ~/.kirocrew-app for notes\nexport EDITOR=vi\n"
        bashrc.write_text(original, encoding="utf-8")

        _fix_shell_profiles()

        assert bashrc.read_text(encoding="utf-8") == original

    def test_a_clean_profile_is_not_rewritten(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        profile = home / ".profile"
        profile.write_text("export EDITOR=vi\n", encoding="utf-8")
        before = profile.stat().st_mtime_ns

        _fix_shell_profiles()

        assert profile.stat().st_mtime_ns == before
        assert capsys.readouterr().out == ""

    def test_absent_profiles_are_skipped_silently(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _fix_shell_profiles()
        assert capsys.readouterr().out == ""

    def test_every_cleaned_profile_is_named_in_the_re_source_hint(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for name in (".zshrc", ".bash_profile"):
            (home / name).write_text(_STALE, encoding="utf-8")

        _fix_shell_profiles()

        out = capsys.readouterr().out
        assert "source ~/.zshrc" in out
        assert "source ~/.bash_profile" in out
        assert " or " in out

    def test_an_unreadable_profile_does_not_abort_setup(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One bad profile must not stop the others from being cleaned."""
        (home / ".zshrc").write_text(_STALE, encoding="utf-8")
        (home / ".bashrc").write_text(_STALE, encoding="utf-8")
        real_read_text = Path.read_text

        def _read_text(self: Path, *a: object, **kw: object) -> str:
            if self.name == ".zshrc":
                raise OSError("zibble permission denied")
            return real_read_text(self, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", _read_text)

        _fix_shell_profiles()

        out = capsys.readouterr().out
        assert ".bashrc" in out
        assert ".zshrc" not in out


class TestSetupSlashCommand:
    """``_setup_slash_command`` writes a Slack command name into config.json.

    Every guard here exists because the value lands in a Slack app manifest: a
    name with a space or 40 characters is rejected by Slack at registration
    time, long after setup has claimed success. The function must fall back to
    the current value rather than persist something unusable.
    """

    @pytest.fixture()
    def cfg_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        target = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: target)
        return target

    @staticmethod
    def _answer(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
        monkeypatch.setattr("kiro_crew.cli_setup._input_or_skip", lambda prompt: value)

    def _saved(self, cfg_file: Path) -> str:
        return json.loads(cfg_file.read_text(encoding="utf-8"))["slack"]["command"]

    def test_a_valid_name_is_saved_with_the_leading_slash_stripped(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._answer(monkeypatch, "/zibble-cmd")
        _setup_slash_command()
        assert self._saved(cfg_file) == "zibble-cmd"

    def test_an_empty_answer_keeps_the_configured_name(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file.write_text(json.dumps({"slack": {"command": "quux"}}), encoding="utf-8")
        self._answer(monkeypatch, None)
        _setup_slash_command()
        assert self._saved(cfg_file) == "quux"

    def test_an_illegal_character_falls_back_to_the_current_name(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._answer(monkeypatch, "has space")
        _setup_slash_command()
        assert self._saved(cfg_file) == "kirocrew"
        assert "letters, numbers, hyphens" in capsys.readouterr().out

    def test_an_over_long_name_falls_back_to_the_current_name(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._answer(monkeypatch, "z" * 33)
        _setup_slash_command()
        assert self._saved(cfg_file) == "kirocrew"
        assert "too long" in capsys.readouterr().out

    def test_an_unreadable_config_aborts_the_step_without_writing(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A corrupt config must not be silently replaced by a fresh one."""
        cfg_file.write_text("{not json", encoding="utf-8")
        self._answer(monkeypatch, "zibble")
        _setup_slash_command()
        assert cfg_file.read_text(encoding="utf-8") == "{not json"
        assert "Could not read" in capsys.readouterr().out


class TestFindElectronDir:
    def test_the_project_dir_env_var_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "checkout"
        electron = root / "website" / "electron"
        electron.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(root))

        assert _find_electron_dir() == electron

    def test_falls_back_to_walking_up_from_the_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no env var, a source checkout is still found from this file's location."""
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)

        found = _find_electron_dir()

        assert found is not None
        assert found.is_dir()
        assert found.parts[-2:] == ("website", "electron")

    def test_returns_none_when_no_checkout_is_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pip-installed tree has no desktop sources — say so, don't guess a path."""
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr(Path, "is_dir", lambda self: False)

        assert _find_electron_dir() is None

    def test_an_env_var_pointing_nowhere_is_ignored_not_returned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "does-not-exist"))

        found = _find_electron_dir()

        # It either resolves the real checkout or gives up — never the bogus root.
        assert found != (tmp_path / "does-not-exist" / "website" / "electron")


class TestSetupWhatsApp:
    """``_setup_whatsapp``: the guided WhatsApp opt-in (`kirocrew setup --whatsapp`).

    WhatsApp has no token to collect: it pairs as a linked device on the operator's
    own account, and pairing is a QR scan served by the RUNNING gateway. So the step
    carries the three things an operator cannot discover elsewhere: the Terms of
    Service / ban risk of automating a personal account, whether the optional wheel
    is installed, and that ENABLING the channel is a config flag separate from
    pairing it. It must also persist that flag without ever damaging a config it
    could not parse.
    """

    @pytest.fixture()
    def cfg_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        target = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: target)
        monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path / "home")
        monkeypatch.setattr("kiro_crew.whatsapp.client.neonize_available", lambda: True)
        return target

    @staticmethod
    def _answer(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
        monkeypatch.setattr("kiro_crew.cli_setup._input_or_skip", lambda prompt: value)

    def test_yes_enables_the_channel_and_keeps_the_rest_of_the_config(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file.write_text(
            json.dumps({"timezone": "Europe/London", "whatsapp": {"dm_policy": "allowlist"}}),
            encoding="utf-8",
        )
        self._answer(monkeypatch, "y")

        _setup_whatsapp()

        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["whatsapp"]["enabled"] is True
        assert saved["whatsapp"]["dm_policy"] == "allowlist"
        assert saved["timezone"] == "Europe/London"

    def test_declining_writes_nothing(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._answer(monkeypatch, None)  # bare Enter, or a non-interactive EOF

        _setup_whatsapp()

        assert not cfg_file.exists(), "a declined opt-in must not create a config"
        assert "Left disabled" in capsys.readouterr().out

    def test_the_terms_of_service_risk_is_stated_before_the_prompt(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Enabling this channel automates the operator's PERSONAL account, which
        can get the number banned. An opt-in that does not say so is not informed."""
        self._answer(monkeypatch, "n")

        _setup_whatsapp()

        out = capsys.readouterr().out
        assert "Terms of Service" in out
        assert "banned" in out

    def test_a_missing_extra_is_named_with_its_install_command(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("kiro_crew.whatsapp.client.neonize_available", lambda: False)
        self._answer(monkeypatch, "y")

        _setup_whatsapp()

        out = capsys.readouterr().out
        # neonize by name -- the extras form installs from no index.
        assert "neonize" in out
        assert "kirocrew[" not in out
        # Still enabled: the operator can install the extra afterwards, and doctor
        # reports the gap until they do. Refusing here would strand them.
        assert json.loads(cfg_file.read_text(encoding="utf-8"))["whatsapp"]["enabled"] is True

    def test_an_unreadable_config_aborts_the_step_without_writing(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_file.write_text("{not json", encoding="utf-8")
        self._answer(monkeypatch, "y")

        _setup_whatsapp()

        assert cfg_file.read_text(encoding="utf-8") == "{not json"
        assert "Could not read" in capsys.readouterr().out

    def test_a_non_object_whatsapp_section_is_left_alone(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_file.write_text(json.dumps({"whatsapp": "on"}), encoding="utf-8")
        self._answer(monkeypatch, "y")

        _setup_whatsapp()

        assert json.loads(cfg_file.read_text(encoding="utf-8")) == {"whatsapp": "on"}
        assert "not an object" in capsys.readouterr().out

    def test_the_step_never_imports_neonize(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wizard reports on the credential's path, never through it: importing
        neonize loads a ~19 MB ctypes CDLL, and setup runs on every install."""
        import builtins

        real_import = builtins.__import__

        def _guard(name, *args, **kwargs):
            if name.split(".")[0] == "neonize":
                raise AssertionError(f"setup imported {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _guard)
        self._answer(monkeypatch, "y")

        _setup_whatsapp()

        assert json.loads(cfg_file.read_text(encoding="utf-8"))["whatsapp"]["enabled"] is True


class TestRemoveRetiredConductorSkill:
    """``agent.conductor_skill`` (the dashboard's "Orchestrator Mode" toggle) used
    to generate an always-on ``<skills>/conductor/SKILL.md``. The flag is retired
    — crew routing goes through the ``select_crew`` MCP tool — so setup removes
    the generated file on old installs. Only bytes the retired generator itself
    wrote are recognised, by exact SHA-256 of the two static revisions. Anything
    else under that path — a user's own skill, an edited copy of ours, or the
    oldest roster-inlining revision that has no byte-exact identity — is never
    touched: a wrongly kept file costs one stale skill, a wrongly deleted one
    costs the user's work.
    """

    _FIXTURES = Path(__file__).parent / "fixtures" / "retired_conductor_skill"

    # The fixed text the oldest (roster-inlining) generator wrote before the
    # per-install roster; it has no byte-exact identity, so it must NOT be a
    # deletion trigger even when the whole prefix matches.
    _LEGACY_HEAD = (
        "---\nalways: true\n---\n# Agent Delegation\n\n"
        'You have access to specialist agents via `spawn_run(agent="<name>", '
        'task="<description>")`.\n\n## Default behavior\n\n'
    )

    @pytest.fixture()
    def skills_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "skills"
        root.mkdir()
        monkeypatch.setattr("kiro_crew.skills.skills_dir", lambda: root)
        return root

    @pytest.mark.parametrize("fixture", ["select-crew-v2.md", "select-crew-v1.md"])
    def test_each_static_generated_revision_is_removed_with_its_empty_directory(
        self, skills_root: Path, fixture: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_bytes((self._FIXTURES / fixture).read_bytes())

        _remove_retired_conductor_skill()

        assert not skill.exists()
        assert not skill.parent.exists()
        assert "Removed retired conductor skill" in capsys.readouterr().out

    @pytest.mark.parametrize("fixture", ["select-crew-v2.md", "select-crew-v1.md"])
    def test_each_windows_generated_revision_is_removed(
        self, skills_root: Path, fixture: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        data = (self._FIXTURES / fixture).read_bytes()
        skill.write_bytes(data.replace(b"\n", b"\r\n"))

        _remove_retired_conductor_skill()

        assert not skill.exists()
        assert not skill.parent.exists()
        assert "Removed retired conductor skill" in capsys.readouterr().out

    def test_a_user_skill_borrowing_the_heading_is_left_alone(
        self, skills_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The heading alone must not be the trigger: a user who titled their own
        delegation notes `# Agent Delegation` would otherwise lose them on setup."""
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        body = "---\nalways: true\n---\n# Agent Delegation\n\nMy own routing notes.\n"
        skill.write_text(body, encoding="utf-8")

        _remove_retired_conductor_skill()

        assert skill.read_text(encoding="utf-8") == body
        assert capsys.readouterr().out == ""

    def test_an_edited_copy_of_the_generated_skill_is_left_alone(self, skills_root: Path) -> None:
        """One changed byte means the user has made it theirs."""
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        data = (self._FIXTURES / "select-crew-v2.md").read_bytes()
        skill.write_bytes(data + b"\n- Also route billing questions to finance.\n")

        _remove_retired_conductor_skill()

        assert skill.read_bytes() == data + b"\n- Also route billing questions to finance.\n"

    def test_the_roster_inlining_revision_is_left_alone_even_with_our_exact_prefix(
        self, skills_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A prefix match cannot tell the untouched legacy file from one the user
        extended below the roster, so neither is deleted."""
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        body = (
            self._LEGACY_HEAD + "## Available Agents\n\n### pr-reviewer\n\nReviews.\n\nMy notes.\n"
        )
        skill.write_text(body, encoding="utf-8")

        _remove_retired_conductor_skill()

        assert skill.read_text(encoding="utf-8") == body
        assert capsys.readouterr().out == ""

    def test_a_sibling_file_keeps_the_directory(self, skills_root: Path) -> None:
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_bytes((self._FIXTURES / "select-crew-v2.md").read_bytes())
        keep = skill.parent / "notes.md"
        keep.write_text("mine\n", encoding="utf-8")

        _remove_retired_conductor_skill()

        assert not skill.exists()
        assert keep.exists()

    def test_a_missing_file_is_a_silent_no_op(
        self, skills_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _remove_retired_conductor_skill()

        assert capsys.readouterr().out == ""

    def test_a_symlinked_skill_file_is_not_followed(self, skills_root: Path) -> None:
        target = skills_root.parent / "generated.md"
        target.write_bytes((self._FIXTURES / "select-crew-v2.md").read_bytes())
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        try:
            skill.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        assert skills_module.remove_retired_conductor_skill() is False
        assert skill.is_symlink()
        assert target.exists()

    def test_a_symlinked_conductor_directory_is_not_followed(self, skills_root: Path) -> None:
        target_dir = skills_root.parent / "external-conductor"
        target_dir.mkdir()
        target = target_dir / "SKILL.md"
        target.write_bytes((self._FIXTURES / "select-crew-v2.md").read_bytes())
        parent = skills_root / "conductor"
        try:
            parent.symlink_to(target_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        assert skills_module.remove_retired_conductor_skill() is False
        assert parent.is_symlink()
        assert target.exists()

    def test_an_oversized_skill_is_not_read_or_removed(
        self, skills_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_bytes(b"x" * (skills_module._RETIRED_CONDUCTOR_SKILL_MAX_BYTES + 1))

        real_open = skills_module.os.open

        def _refuse_skill_open(path: str | Path, *args, **kwargs):
            if Path(path).name == "SKILL.md":
                pytest.fail("oversized skill was opened")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(skills_module.os, "open", _refuse_skill_open)

        assert skills_module.remove_retired_conductor_skill() is False
        assert skill.exists()

    def test_a_replaced_inode_is_not_unlinked(
        self, skills_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identity-checked unlink only runs on the pinned-descriptor path."""
        if not pinned_fs.supports_pinned_walk():
            pytest.skip("no descriptor-relative opens on this platform")
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_bytes((self._FIXTURES / "select-crew-v2.md").read_bytes())
        replacement_data = b"user-owned replacement\n"
        original_unlink = pinned_fs.unlink_verified

        def _replace_before_unlink(holder_fd: int, name: str, expect: tuple[int, int]) -> bool:
            replacement = skill.with_name("replacement.md")
            replacement.write_bytes(replacement_data)
            replacement.replace(skill)
            return original_unlink(holder_fd, name, expect)

        monkeypatch.setattr(pinned_fs, "unlink_verified", _replace_before_unlink)

        assert skills_module.remove_retired_conductor_skill() is False
        assert skill.read_bytes() == replacement_data

    def test_the_conductor_directory_is_never_re_resolved_by_name(
        self, skills_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-resolving the name would follow a link swapped in after the check."""
        if not pinned_fs.supports_pinned_walk():
            pytest.skip("no descriptor-relative opens on this platform")
        skill = skills_root / "conductor" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_bytes((self._FIXTURES / "select-crew-v2.md").read_bytes())
        real_realpath = skills_module.os.path.realpath

        def _asking_module() -> str:
            # The nearest caller outside the path-handling stdlib: production
            # reaching realpath through `Path.resolve()` still answers as
            # production, while an observer's own normalisation answers as itself.
            frame = sys._getframe(2)
            while frame is not None and frame.f_globals.get("__name__", "").split(".")[0] in (
                "posixpath",
                "ntpath",
                "genericpath",
                "os",
                "pathlib",
            ):
                frame = frame.f_back
            return frame.f_globals.get("__name__", "") if frame is not None else ""

        def _no_conductor_realpath(path, *args, **kwargs):
            # `skills_module.os.path` IS the process-wide `os.path`, so this trap
            # sees every caller -- including observers that resolve the same
            # operand for their own bookkeeping (an audit hook on `os.rename`, a
            # coverage or hygiene plugin). Only PRODUCTION re-resolving the name
            # is the defect; a bystander normalising a path it was handed is not,
            # so the verdict is keyed on who asked.
            if str(path).endswith("conductor") and _asking_module().startswith("kiro_crew"):
                pytest.fail("the conductor path was re-resolved by name")
            return real_realpath(path, *args, **kwargs)

        monkeypatch.setattr(skills_module.os.path, "realpath", _no_conductor_realpath)

        assert skills_module.remove_retired_conductor_skill() is True
        assert not skill.exists()

    def test_the_pinned_hashes_match_the_fixtures(self) -> None:
        """The fixtures are the byte-exact outputs of the deleted generator's two
        static revisions; the hash set in skills must name exactly those."""
        import hashlib

        digests = {
            hashlib.sha256((self._FIXTURES / name).read_bytes()).hexdigest()
            for name in ("select-crew-v1.md", "select-crew-v2.md")
        }
        assert digests == set(RETIRED_CONDUCTOR_SKILL_SHA256)
