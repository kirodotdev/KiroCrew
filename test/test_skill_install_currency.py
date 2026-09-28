"""An installed skill must be able to tell its operator it is behind the package.

The packaged-to-installed hop is content-verified when the sync decides to copy,
but the DECISION to copy is mtime-based. An installed copy whose mtime is newer
than anything the package ships is judged up to date and skipped, so the install
keeps running superseded code and nothing anywhere says so.

``installed_skill_currency`` reports the comparison that gate throws away. The
load-bearing test here is ``test_stale_install_the_mtime_gate_skips_is_reported``:
it drives the real sync into exactly the state that hides staleness, proves the
install did NOT update, and only then asserts the check still names it. An
in-sync test alone would stay green with the comparison deleted outright, so
agreement is not evidence the instrument works.

Currency is deliberately local. "Behind" means the install does not match the
package THIS build ships; no test here reaches a network, because the check does
not either.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import skills as skills_mod
from kiro_crew.skills import (
    SKILL_INSTALL_BEHIND,
    SKILL_INSTALL_EDITED,
    SKILL_INSTALL_IN_SYNC,
    SKILL_INSTALL_UNVERIFIABLE,
    InstalledSkillCurrency,
    installed_skill_currency,
)

_MANIFEST = "---\nname: {name}\ndescription: fixture skill\n---\nbody\n"

# The check itself is POSIX-only, so every case that asserts a VERDICT is
# POSIX-only too: on Windows the call returns nothing and each of these would
# assert against an empty result. The cases that simulate Windows belong here
# as well -- they fake the platform on a host whose link semantics they need.
_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the currency check is POSIX-only; on Windows it reads nothing"
)


@pytest.fixture
def trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """``(install base, packaged source root)`` with the sync pointed at both."""
    base = tmp_path / "home" / "skills"
    packaged = tmp_path / "packaged"
    base.mkdir(parents=True)
    packaged.mkdir(parents=True)
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: base)
    monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", packaged)
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: None)
    return base, packaged


def _ship(root: Path, name: str, *, script: str = "print('v1')\n") -> Path:
    """Author skill *name* under *root* shipping ``scripts/probe.py``."""
    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_MANIFEST.format(name=name), encoding="utf-8")
    (skill_dir / "scripts" / "probe.py").write_text(script, encoding="utf-8")
    return skill_dir


def _state_for(name: str) -> str:
    states = {entry.state for entry in installed_skill_currency() if entry.name == name}
    assert len(states) == 1, f"{name} not reported exactly once: {states}"
    return states.pop()


def _entry_for(name: str) -> InstalledSkillCurrency:
    matches = [entry for entry in installed_skill_currency() if entry.name == name]
    assert len(matches) == 1, f"{name} not reported exactly once: {matches}"
    return matches[0]


def _names() -> set[str]:
    return {entry.name for entry in installed_skill_currency()}


def _bump_mtime(root: Path, when: float) -> None:
    """Stamp every entry under *root* so the sync's mtime gate reads it as newest."""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            os.utime(Path(dirpath) / name, (when, when))
        os.utime(dirpath, (when, when))


@_POSIX_ONLY
def test_clean_install_reads_in_sync(trees: tuple[Path, Path]) -> None:
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    assert _state_for("probe-skill") == SKILL_INSTALL_IN_SYNC


@_POSIX_ONLY
def test_package_moving_on_reads_behind(trees: tuple[Path, Path]) -> None:
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    # The package gains the fix; the install has not taken it yet.
    (packaged / "probe-skill" / "scripts" / "probe.py").write_text(
        "print('v2')\n", encoding="utf-8"
    )

    entry = _entry_for("probe-skill")
    assert entry.state == SKILL_INSTALL_BEHIND
    # Doctor prints this path, so it must name the tree the verdict came from.
    assert entry.source == packaged / "probe-skill"


@_POSIX_ONLY
def test_stale_install_the_mtime_gate_skips_is_reported(trees: tuple[Path, Path]) -> None:
    """The reported defect: the sync skips the install and the check still names it."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    (packaged / "probe-skill" / "scripts" / "probe.py").write_text(
        "print('v2')\n", encoding="utf-8"
    )
    # An installed copy newer than anything the package ships: the update gate
    # compares mtimes, so this is the state in which staleness goes unobserved.
    newest_packaged = skills_mod._tree_newest_mtime(packaged / "probe-skill")
    assert newest_packaged is not None
    _bump_mtime(base / "probe-skill", newest_packaged + 3600)

    skills_mod._ensure_builtin_skills(base)

    installed_body = (base / "probe-skill" / "scripts" / "probe.py").read_text(encoding="utf-8")
    assert installed_body == "print('v1')\n", "sync unexpectedly updated; scenario invalid"
    assert _state_for("probe-skill") == SKILL_INSTALL_BEHIND


@_POSIX_ONLY
def test_locally_edited_install_reads_edited(trees: tuple[Path, Path]) -> None:
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    (base / "probe-skill" / "scripts" / "probe.py").write_text("print('mine')\n", encoding="utf-8")

    assert _state_for("probe-skill") == SKILL_INSTALL_EDITED


@_POSIX_ONLY
def test_edited_outranks_behind_when_both_apply(trees: tuple[Path, Path]) -> None:
    """An edit is what has to be reconciled first, so it is the reported fact."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    (base / "probe-skill" / "scripts" / "probe.py").write_text("print('mine')\n", encoding="utf-8")
    (packaged / "probe-skill" / "scripts" / "probe.py").write_text(
        "print('v2')\n", encoding="utf-8"
    )

    assert _state_for("probe-skill") == SKILL_INSTALL_EDITED


@_POSIX_ONLY
def test_unmarked_install_cannot_be_compared(trees: tuple[Path, Path]) -> None:
    """A pre-provenance or user-authored copy carries no marker to compare."""
    base, packaged = trees
    _ship(packaged, "probe-skill", script="print('v2')\n")
    _ship(base, "probe-skill", script="print('v1')\n")

    assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE


@_POSIX_ONLY
def test_install_that_is_a_link_cannot_be_compared(trees: tuple[Path, Path]) -> None:
    """The sync only creates real directories, so a link is user-made."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    elsewhere = _ship(base.parent / "elsewhere", "probe-skill")
    (base / "probe-skill").symlink_to(elsewhere, target_is_directory=True)

    assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE


@_POSIX_ONLY
def test_install_that_is_a_dangling_link_cannot_be_compared(trees: tuple[Path, Path]) -> None:
    """A link whose target is gone is an install of unknown currency, not an absence.

    Presence is judged by a readable SKILL.md, which a dangling link fails, so
    such an install would drop out of the result entirely and the link branch of
    the state check would only ever see links that DO resolve -- the safer case.
    """
    base, packaged = trees
    _ship(packaged, "probe-skill")
    (base / "probe-skill").symlink_to(base.parent / "gone", target_is_directory=True)
    assert not (base / "probe-skill" / "SKILL.md").is_file()

    assert "probe-skill" in _names()
    assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE


@_POSIX_ONLY
def test_unhashable_install_cannot_be_compared(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree over the fingerprint ceiling is unprovable, never agreement."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    assert _state_for("probe-skill") == SKILL_INSTALL_IN_SYNC

    monkeypatch.setattr(skills_mod, "_FINGERPRINT_MAX_ENTRIES", 1)

    assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE


@pytest.mark.skipif(
    # Both conditions live in ONE decorator because every skipif expression is
    # evaluated at collection time, on every platform: a second decorator
    # calling os.geteuid() would raise AttributeError on Windows during import
    # and error the whole module rather than skipping this one test. getattr
    # keeps the call off platforms that do not define it, and root is skipped
    # because it reads through the unreadable bit this test depends on.
    os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="needs POSIX mode bits and a non-root euid",
)
def test_unreadable_packaged_tree_cannot_be_compared(trees: tuple[Path, Path]) -> None:
    """An unreadable packaged entry is unprovable, so no verdict is claimed."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    assert _state_for("probe-skill") == SKILL_INSTALL_IN_SYNC

    blocked = packaged / "probe-skill" / "scripts"
    blocked.chmod(0o000)
    try:
        assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE
    finally:
        blocked.chmod(0o755)


@_POSIX_ONLY
def test_install_no_source_ships_is_absent(trees: tuple[Path, Path]) -> None:
    """With no packaged tree there is nothing to be out of step with."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    _ship(base, "my-own-skill")

    reported = _names()
    assert "probe-skill" in reported
    assert "my-own-skill" not in reported


@_POSIX_ONLY
def test_packaged_but_not_installed_is_absent(trees: tuple[Path, Path]) -> None:
    """An absent directory has no currency to judge."""
    _base, packaged = trees
    _ship(packaged, "probe-skill")

    assert _names() == set()


@_POSIX_ONLY
def test_project_skill_is_judged_against_the_project_tree(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shadowing project skill must not read as behind the builtin it replaces."""
    base, packaged = trees
    project = base.parent.parent / "project-skills"
    project.mkdir(parents=True)
    _ship(project, "probe-skill", script="print('project')\n")
    _ship(packaged, "probe-skill", script="print('packaged')\n")
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: project)

    skills_mod._ensure_builtin_skills(base)

    assert (base / "probe-skill" / "scripts" / "probe.py").read_text(
        encoding="utf-8"
    ) == "print('project')\n"
    entry = _entry_for("probe-skill")
    assert entry.state == SKILL_INSTALL_IN_SYNC
    # The printed source is what makes a shadowing skill legible to the reader.
    assert entry.source == project / "probe-skill"


@_POSIX_ONLY
def test_doctor_names_the_verdict_the_source_and_a_remedy_that_works(
    trees: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The printed section must name the tree compared and an action that clears it.

    Restarting the gateway does not help the case this check exists for: the
    install carries the newer mtime, so the sync reads it as up to date and
    skips it again. The output has to name the action that does work, or the
    operator is left with a diagnosis and no move.
    """
    from kiro_crew.cli_doctor import _doctor_skill_currency, _safe_display

    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    (packaged / "probe-skill" / "scripts" / "probe.py").write_text(
        "print('v2')\n", encoding="utf-8"
    )

    issues: list[str] = []
    _doctor_skill_currency(issues)
    out = capsys.readouterr().out

    assert "Installed Skill Currency" in out
    assert "probe-skill" in out
    # Every value doctor reads off disk is printed through _safe_display, which
    # reprs it so a terminal cannot act on it. A separator that repr escapes
    # means the raw path is not a substring of the line, so the assertion
    # compares the rendering the section really emits.
    assert _safe_display(str(packaged / "probe-skill")) in out, "the compared tree must be named"
    assert "OUT of the skills directory" in out, "restart alone cannot clear this case"
    assert "remove or rename" not in out, "a rename in place publishes a second copy"
    assert "NO OLDER than" in out, "the mtime gate is strict, so equal mtimes also persist"
    assert issues, "a behind install must be recorded as an issue"


@_POSIX_ONLY
def test_a_linked_install_is_never_stated_through(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presence must decide a linked install WITHOUT resolving the link.

    The presence gate and the state check are two reads of the same path, and
    only the state check refuses to resolve. The gate's ``SKILL.md`` stat goes
    THROUGH the link, so on Windows a junction aimed at ``\\\\host\\share`` turns
    this local-looking probe into an outbound SMB connection that authenticates
    as this process, before anything has judged the path.

    Order is what prevents it, and order is invisible in the verdict: a linked
    install reads unverifiable either way. So this asserts on the CALL, not on
    the result -- no stat may be attempted on any path under the link.
    """
    base, packaged = trees
    _ship(packaged, "probe-skill")
    elsewhere = _ship(base.parent / "elsewhere", "probe-skill")
    link = base / "probe-skill"
    link.symlink_to(elsewhere, target_is_directory=True)

    real_is_file = Path.is_file
    traversed: list[str] = []

    def recording_is_file(self: Path) -> bool:
        if str(self).startswith(str(link)):
            traversed.append(str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", recording_is_file)

    assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE
    assert traversed == [], f"the link was resolved before it was judged: {traversed}"


@_POSIX_ONLY
def test_a_linked_skills_directory_reports_every_name_unverifiable(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link AT the skills directory is one link above every install.

    Screening only each install leaves this one unscreened, and then the very
    first probe -- the directory test that decides whether there is anything to
    enumerate -- already resolves it. Reporting an empty result instead would
    read as a gateway with no installs, which is agreement, so every packaged
    name must come back unverifiable.
    """
    base, packaged = trees
    _ship(packaged, "probe-skill")
    _ship(packaged, "other-skill")
    real_base = base.parent / "real-skills"
    real_base.mkdir()
    base.rmdir()
    base.symlink_to(real_base, target_is_directory=True)
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: base)

    entries = installed_skill_currency()

    assert [entry.name for entry in entries] == ["other-skill", "probe-skill"]
    assert {entry.state for entry in entries} == {SKILL_INSTALL_UNVERIFIABLE}


@_POSIX_ONLY
def test_a_linked_directory_above_a_nested_install_is_never_stated_through(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested name's INTERMEDIATE directory is a link nothing else screens.

    Packaged names are nested today (``kirocrew-dev/prepare-pr``), so the
    install path has a middle component that is neither the skills directory nor
    the leaf. Screening only those two leaves it unscreened, and then every
    probe of the leaf -- including the link test on the leaf itself -- resolves
    through it: on Windows a junction there aimed at ``\\\\host\\share`` becomes an
    outbound SMB connection authenticating as this process, and on any platform
    the fingerprint hashes a tree outside the skills directory and reports it as
    the install.

    The link here targets a REAL marked install, so without the screen the
    verdict is a confident ``in-sync`` about a tree that is not there. That is
    what makes this assert on the verdict AND on the call.
    """
    base, packaged = trees
    _ship(packaged, "nest/probe-skill")
    skills_mod._ensure_builtin_skills(base)
    assert _state_for("nest/probe-skill") == SKILL_INSTALL_IN_SYNC, "scenario invalid"

    # Same tree, same marker, reached only through a link on the middle component.
    real_nest = base.parent / "real-nest"
    (base / "nest").rename(real_nest)
    link = base / "nest"
    link.symlink_to(real_nest, target_is_directory=True)

    real_is_file = Path.is_file
    traversed: list[str] = []

    def recording_is_file(self: Path) -> bool:
        if str(self).startswith(str(link)):
            traversed.append(str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", recording_is_file)

    assert "nest/probe-skill" in _names(), "a name that cannot be judged must not vanish"
    assert _state_for("nest/probe-skill") == SKILL_INSTALL_UNVERIFIABLE
    assert traversed == [], f"the link was resolved before it was judged: {traversed}"


class _WindowsNameOnly:
    """The real ``os``, reporting Windows for ``name`` and nothing else changed.

    Patching the global ``os.name`` would make ``pathlib`` build ``WindowsPath``
    and refuse to instantiate on a POSIX host. Only the module under test needs
    to believe it is on Windows.
    """

    name = "nt"

    def __getattr__(self, attr: str) -> object:
        return getattr(os, attr)


@_POSIX_ONLY
def test_on_windows_nothing_is_read_and_nothing_is_reported(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform gate must stop the reads, not just discard their answers.

    Every read in the check reaches its target by name, and the link test
    guarding each one is a separate syscall from the read it guards. On Windows
    losing that race costs an outbound authenticated connection rather than a
    wrong answer, and the descriptor-pinned walk that would close it does not
    exist there. So the requirement is that no probe is attempted at all --
    returning an empty list after reading would satisfy the verdict and miss the
    point.
    """
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    assert _state_for("probe-skill") == SKILL_INSTALL_IN_SYNC, "scenario invalid"

    monkeypatch.setattr(skills_mod, "os", _WindowsNameOnly())

    probed: list[str] = []
    real_dir = Path.is_dir
    real_file = Path.is_file

    def recording_is_dir(self: Path) -> bool:
        probed.append(str(self))
        return real_dir(self)

    def recording_is_file(self: Path) -> bool:
        probed.append(str(self))
        return real_file(self)

    monkeypatch.setattr(Path, "is_dir", recording_is_dir)
    monkeypatch.setattr(Path, "is_file", recording_is_file)

    assert installed_skill_currency() == []
    under_base = [path for path in probed if path.startswith(str(base))]
    assert under_base == [], f"the skills directory was read on Windows: {under_base}"


@_POSIX_ONLY
def test_doctor_says_the_check_does_not_run_on_windows(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence would read as agreement, so the boundary is printed.

    An absent section cannot be told apart from a gateway whose installs are all
    current, which is the false reassurance this instrument exists to remove. It
    must also not record a readiness issue: the operator's install is not at
    fault, the check simply does not run here.
    """
    from kiro_crew.cli_doctor import _doctor_skill_currency

    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    monkeypatch.setattr(skills_mod, "os", _WindowsNameOnly())
    monkeypatch.setattr("kiro_crew.cli_doctor.os.name", "nt")

    issues: list[str] = []
    _doctor_skill_currency(issues)
    out = capsys.readouterr().out

    assert "Installed Skill Currency" in out
    assert "not checked on this platform yet" in out
    assert "probe-skill" not in out, "a verdict was printed for a tree that was not read"
    assert issues == [], "a platform gap is not a fault in the operator's install"


@pytest.mark.skipif(os.name != "nt", reason="asserts the real Windows platform, not a simulation")
def test_on_a_real_windows_host_the_gate_fires() -> None:
    """The two cases above fake the platform, so one case must not.

    A simulation proves the branch, never that the branch is the one this host
    takes. This asks the real interpreter on a real Windows runner, and it needs
    no fixture: the gate returns before ``skills_dir()`` is called, so there is
    nothing to arrange and nothing is read.
    """
    from kiro_crew.cli_doctor import _doctor_skill_currency

    assert installed_skill_currency() == []

    issues: list[str] = []
    _doctor_skill_currency(issues)
    assert issues == []
