"""Second-hop cron script source verification (Refs #7093).

The packaged ``builtin_skills/`` to installed-skills hop is content-verified,
``scripts/`` included -- ``test_builtin_skill_sync_safety.py`` pins that with
``test_script_only_package_update_reaches_the_install``. The next hop, installed
skill asset to ``<config_dir>/crons/``, is a hand-run ``cp`` that nothing ever
compared, so a deployed cron script could run superseded code indefinitely while
looking healthy.

These tests pin the hop-2 comparison. The load-bearing one is
``test_diverged_deploy_is_detected``: an identical-copy test alone stays green
even if the comparison is deleted outright, so agreement is not evidence the
instrument works. The scope guard matters just as much -- a deployed script with
no source at all must NOT be reported, because cron script bodies are
LLM-writeable by design and whether they ought to have sources is a product
question this check does not raise.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import skills as skills_mod
from kiro_crew.skills import (
    CRON_SOURCE_DIVERGED,
    CRON_SOURCE_IN_SYNC,
    CRON_SOURCE_UNVERIFIABLE,
    deployed_cron_script_sources,
)


@pytest.fixture
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp data home with the two directories the comparison spans."""
    home = tmp_path / "crew"
    (home / "crons").mkdir(parents=True)
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(skills_mod, "config_dir", lambda: home)
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: home / "skills")
    return home


def _ship_skill_script(home: Path, skill: str, script: str, body: str) -> Path:
    """Install a skill under *skill* shipping ``scripts/<script>`` with *body*."""
    skill_dir = home / "skills" / skill
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill.replace('/', '-')}\ndescription: fixture skill\n---\nbody\n",
        encoding="utf-8",
    )
    target = skill_dir / "scripts" / script
    target.write_text(body, encoding="utf-8")
    return target


def _deploy(home: Path, script: str, body: str) -> Path:
    """Write *body* to ``crons/<script>`` the way a hand-run ``cp`` would."""
    target = home / "crons" / script
    target.write_text(body, encoding="utf-8")
    return target


def _state_for(name: str) -> str:
    states = {entry.name: entry.state for entry in deployed_cron_script_sources()}
    assert name in states, f"{name} absent from {states}"
    return states[name]


class TestHopTwoVerification:
    def test_diverged_deploy_is_detected(self, data_home: Path) -> None:
        """A deployed copy whose source moved on reads as diverged.

        This is the assertion the whole check exists for. The source gains a
        line the deployed copy has never seen -- exactly the shape of the real
        drift, where a package split a helper out and the deploy kept the old
        inline body.
        """
        _ship_skill_script(
            data_home, "kirocrew-dev/babysit", "pr_watch.py", "def watch(ctx):\n    pass\n"
        )
        _deploy(data_home, "pr_watch.py", "def watch(ctx):\n    return None\n")

        assert _state_for("pr_watch.py") == CRON_SOURCE_DIVERGED

    def test_identical_deploy_agrees(self, data_home: Path) -> None:
        body = "def watch(ctx):\n    pass\n"
        _ship_skill_script(data_home, "kirocrew-dev/babysit", "pr_watch.py", body)
        _deploy(data_home, "pr_watch.py", body)

        assert _state_for("pr_watch.py") == CRON_SOURCE_IN_SYNC

    def test_whitespace_only_difference_still_diverges(self, data_home: Path) -> None:
        """Comparison is on bytes, so a trailing-newline drift is not waved through."""
        _ship_skill_script(data_home, "pack/skill", "poller.py", "def run(ctx):\n    pass\n")
        _deploy(data_home, "poller.py", "def run(ctx):\n    pass")

        assert _state_for("poller.py") == CRON_SOURCE_DIVERGED

    def test_deployed_script_without_a_source_is_not_reported(self, data_home: Path) -> None:
        """Scope guard: the sourceless majority is out of scope, not a finding.

        Reporting these would re-raise the Tier 0 product question -- whether
        operational cron scripts ought to ship from the repo at all -- which a
        maintainer has ruled is not this check's call.
        """
        _ship_skill_script(data_home, "pack/skill", "poller.py", "def run(ctx):\n    pass\n")
        _deploy(data_home, "poller.py", "def run(ctx):\n    pass\n")
        _deploy(data_home, "gh_cleanup.py", "def cleanup(ctx):\n    pass\n")

        reported = {entry.name for entry in deployed_cron_script_sources()}
        assert reported == {"poller.py"}
        assert "gh_cleanup.py" not in reported

    def test_agreement_with_any_candidate_source_is_in_sync(self, data_home: Path) -> None:
        """Two skills shipping one name: matching either is agreement.

        The copy records no owner, so reporting a mismatch against an
        arbitrarily chosen candidate would be a fabricated finding.
        """
        shared = "def run(ctx):\n    return 2\n"
        _ship_skill_script(data_home, "pack/first", "shared.py", "def run(ctx):\n    return 1\n")
        _ship_skill_script(data_home, "pack/second", "shared.py", shared)
        _deploy(data_home, "shared.py", shared)

        assert _state_for("shared.py") == CRON_SOURCE_IN_SYNC

    def test_divergence_from_every_candidate_is_still_diverged(self, data_home: Path) -> None:
        _ship_skill_script(data_home, "pack/first", "shared.py", "def run(ctx):\n    return 1\n")
        _ship_skill_script(data_home, "pack/second", "shared.py", "def run(ctx):\n    return 2\n")
        _deploy(data_home, "shared.py", "def run(ctx):\n    return 3\n")

        assert _state_for("shared.py") == CRON_SOURCE_DIVERGED

    def test_unreadable_side_is_unverifiable_not_agreement(
        self, data_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An instrument whose read failed must not report the two sides equal.

        A body over the ceiling is the cheapest way to make the read fail; the
        verdict must be ``unverifiable``, never ``in-sync``.
        """
        body = "def run(ctx):\n    pass\n"
        _ship_skill_script(data_home, "pack/skill", "poller.py", body)
        _deploy(data_home, "poller.py", body)
        monkeypatch.setattr(skills_mod, "_CRON_SOURCE_MAX_BYTES", 4)

        state = _state_for("poller.py")
        assert state == CRON_SOURCE_UNVERIFIABLE
        assert state != CRON_SOURCE_IN_SYNC

    def test_no_skills_dir_yields_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "crew"
        (home / "crons").mkdir(parents=True)
        monkeypatch.setattr(skills_mod, "config_dir", lambda: home)
        monkeypatch.setattr(skills_mod, "skills_dir", lambda: home / "skills")

        assert deployed_cron_script_sources() == []


class TestDoctorRendersNamesInert:
    """A deployed script's NAME is chosen by an agent, so doctor must not print it raw.

    ``cron_script.py`` states that bodies under ``<config_dir>/crons/`` are
    LLM-writeable by design and enumerates the mitigations that make that
    acceptable. So the filename is not merely untrusted in the abstract -- the
    design deliberately lets an agent pick it, which makes a diagnostic that
    echoes it the wrong consumer for that directory. An OSC/ANSI sequence would
    drive the operator's terminal, and a newline would let a name forge the
    surrounding verdict lines.

    The contract under test is what doctor PRINTS, so these inject the state
    rather than staging a file. That is deliberate and not a convenience: a
    control character is legal in a POSIX filename and rejected by Win32, so an
    on-disk fixture would test the filesystem's rules instead of the renderer's,
    and would fail on Windows with ``EINVAL`` before reaching the code under
    test. Injecting covers the renderer identically on every platform.

    Each test asserts the RAW escape is absent and the escaped form present.
    Asserting that an ordinary name renders correctly would pass with or without
    ``_safe_display``, so it would prove nothing.
    """

    ESC_NAME = "pr_watch\x1b[31m\n_forged.py"

    def _render(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        state: str,
    ) -> str:
        from kiro_crew import cli_doctor

        entry = skills_mod.CronScriptSource(
            name=self.ESC_NAME,
            source=Path("/skills/pack/skill/scripts") / self.ESC_NAME,
            state=state,
        )
        monkeypatch.setattr(skills_mod, "deployed_cron_script_sources", lambda: [entry])
        issues: list[str] = []
        cli_doctor._doctor_cron_script_sources(issues)
        return capsys.readouterr().out

    @pytest.mark.parametrize(
        ("state", "marker"),
        [
            (CRON_SOURCE_IN_SYNC, "agrees with"),
            (CRON_SOURCE_DIVERGED, "DIVERGED"),
            (CRON_SOURCE_UNVERIFIABLE, "could not be compared"),
        ],
    )
    def test_every_branch_renders_a_control_sequence_inert(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        state: str,
        marker: str,
    ) -> None:
        """All three verdict branches print a name, so all three must escape it.

        Parametrised rather than written once against the diverged branch: fixing
        only the branch a reviewer pointed at is half a fix that reads as
        complete.
        """
        out = self._render(monkeypatch, capsys, state)

        assert marker in out
        assert "\x1b" not in out
        assert "\\x1b" in out

    def test_source_path_is_also_rendered_inert(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The source half is read off disk too, so it gets the same treatment."""
        out = self._render(monkeypatch, capsys, CRON_SOURCE_DIVERGED)

        # The escaped name appears twice: once as the deployed name, once inside
        # the source path. Neither may carry a live escape.
        assert out.count("\\x1b") >= 2
        assert "\x1b" not in out


@pytest.mark.skipif(
    os.name == "nt",
    reason="Win32 rejects control characters in filenames (EINVAL), so a "
    "control-character cron script name cannot exist on that platform. The "
    "renderer itself is covered on every platform by "
    "TestDoctorRendersNamesInert, which injects the name instead.",
)
def test_control_character_name_on_disk_is_enumerated_and_escaped(
    data_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end on POSIX, where such a filename is legal and the vector is live.

    Proves the premise the injected tests assume: a control-character name is
    accepted by the filesystem, survives enumeration, and still reaches the
    operator inert.
    """
    from kiro_crew import cli_doctor

    name = "poller\x1b[31m.py"
    _ship_skill_script(data_home, "pack/skill", name, "def run(ctx):\n    pass\n")
    _deploy(data_home, name, "def run(ctx):\n    return 1\n")

    issues: list[str] = []
    cli_doctor._doctor_cron_script_sources(issues)
    out = capsys.readouterr().out

    assert "DIVERGED" in out
    assert "\x1b" not in out
    assert "\\x1b" in out
