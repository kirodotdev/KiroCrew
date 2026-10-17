"""A symlinked home hands the launcher ONE name per directory, and the launcher runs.

``/home/u -> /mnt/home/u``: ``Path.home()`` keeps the link spelling and
``config_dir()`` returns the resolved one, so every crew-home path has two
spellings that reach one directory. The launcher keeps its records per NAME --
``mask_occupants`` from the pre-spawn pass, ``masked_names`` from its mask stages --
and compares them against the filesystem by identity, so two names for one
object make those records disagree with what a name reaches. Every spawn
failure this layout has produced had that one shape.

This suite holds the two halves of the answer. The producers emit one spelling
per directory (the one the tier lists carry, so the occupant pass records under a
name the launcher looks up): read from the plan line of the launcher
``namespace_argv`` really writes for such a host. And the launcher's own stages
(``kiro_crew.sandbox_launcher_program``) run to the end against that plan, with a
bind that HIDES its target the way a real mount does; the crew-home alias checks on
either side of the masks refuse a home link re-aimed in between, at the point in
the child run where each one stands.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import CoveringLibc, launch, refusal, rendered_payload

import kiro_crew.sandbox as sb
from kiro_crew import kiro_prerequisite as kp
from kiro_crew import platform_compat, sandbox_launcher_program

program = sandbox_launcher_program

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="the launcher script and the symlinked home are POSIX mechanisms",
)

_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the mask stages pin through O_PATH and /proc/self/fd; the namespace launcher "
    "is Linux-only",
)


@pytest.fixture()
def symlinked_home(tmp_path, monkeypatch):
    """``$HOME`` is a link into the real tree; the data home sits under both spellings."""
    real_home = tmp_path / "mnt" / "home" / "u"
    data_home = real_home / ".kirocrew"
    for leaf in ("diag", "run", "apps/aws-control/data", "quarantined-clones"):
        (data_home / leaf).mkdir(parents=True)
    (data_home / ".env").write_text("SECRET=1\n")
    (tmp_path / "home").symlink_to(tmp_path / "mnt" / "home", target_is_directory=True)
    link_home = tmp_path / "home" / "u"
    assert link_home.is_dir() and not (link_home / ".kiro").exists()
    monkeypatch.setattr(sb.Path, "home", classmethod(lambda _cls: link_home))
    monkeypatch.setattr(sb, "config_dir", lambda: data_home)
    monkeypatch.setattr(sb, "_backend", "namespace")
    return link_home, data_home


def _probe_script(link_home: Path, data_home: Path) -> str:
    """The launcher the readiness probe writes for this host, through the real pass."""
    service = kp.KiroPrerequisiteService(
        platform_name="linux", home=link_home, data_home=data_home, environ={}
    )
    argv = sb.namespace_argv(
        ["/usr/bin/env", "kiro-cli", "--version"],
        "strict",
        extra_hidden_dirs=service._hidden_probe_dirs,
    )
    script_path = next(a for a in argv if a.endswith(".py") and "kirocrew_sandbox_" in a)
    try:
        return Path(script_path).read_text()
    finally:
        os.unlink(script_path)


def _probe_plan(link_home: Path, data_home: Path) -> dict:
    """The plan the probe's launcher carries for this host."""
    return rendered_payload(_probe_script(link_home, data_home))


def _child_plan(plan: dict) -> dict:
    """*plan* with no host stand-in root to probe, so a child run stages under ``tmp_path``."""
    return dict(plan, stand_in_roots=[])


def _re_aim_home(tmp_path: Path, to: str) -> Path:
    """Re-aim the ``$HOME`` link at ``tmp_path/<to>/home``; returns the new ``u``."""
    target = tmp_path / to / "home" / "u"
    (target / ".kirocrew").mkdir(parents=True, exist_ok=True)
    home_link = tmp_path / "home"
    home_link.unlink()
    home_link.symlink_to(tmp_path / to / "home", target_is_directory=True)
    return target


def _identity(path: str) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def test_the_probes_home_spellings_fold_onto_the_data_home(symlinked_home) -> None:
    """The probe names the data home three ways; the launcher receives it once."""
    link_home, data_home = symlinked_home
    service = kp.KiroPrerequisiteService(
        platform_name="linux", home=link_home, data_home=data_home, environ={}
    )
    assert str(link_home / ".kirocrew") in service._hidden_probe_dirs, "fixture drifted"
    dirs = _probe_plan(link_home, data_home)["sensitive_dirs"]
    assert dirs.count(str(data_home)) == 1
    assert str(link_home / ".kirocrew") not in dirs


def test_a_relocated_home_is_still_listed_under_its_own_spelling(tmp_path, monkeypatch) -> None:
    """The identity check narrows the duplicate case only; a real relocation keeps its rule."""
    elsewhere = tmp_path / "srv" / "crew"
    (elsewhere / "diag").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sb.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(sb, "config_dir", lambda: elsewhere)
    assert str(elsewhere / "diag") in sb._relocated_crew_targets(("diag",))


def test_the_launcher_is_handed_one_spelling_per_directory(symlinked_home) -> None:
    link_home, data_home = symlinked_home
    plan = _probe_plan(link_home, data_home)

    seen: dict[tuple[int, int], str] = {}
    for name in plan["sensitive_dirs"]:
        ident = _identity(name)
        if ident is None:
            continue
        assert ident not in seen, f"{name} and {seen[ident]} are two names for one directory"
        seen[ident] = name
    # The resolved spelling is the canonical one: it is how the pre-spawn passes
    # already spell the leaves they record, and how ``.vault`` -- a tier entry with
    # no relocated twin -- is still masked after the fold.
    assert str(data_home / "diag") in plan["sensitive_dirs"]
    assert str(data_home / ".vault") in plan["sensitive_dirs"]
    assert not any(name.startswith(str(link_home / ".kirocrew")) for name in plan["sensitive_dirs"])


def test_the_carried_leaf_identity_is_under_the_spelling_the_launcher_lists(
    symlinked_home,
) -> None:
    """The record and the list agree on the name, so the identity check runs for the leaf."""
    link_home, data_home = symlinked_home
    plan = _probe_plan(link_home, data_home)
    leaf = str(data_home / "diag")
    assert leaf in plan["sensitive_dirs"]
    assert leaf in plan["mask_occupants"], "the crew leaf lost its carried identity"
    assert not any(
        name.startswith(str(link_home / ".kirocrew")) for name in plan["mask_occupants"]
    ), "an occupant was recorded under the alias spelling"


@_LINUX_ONLY
def test_the_hiding_region_runs_to_the_end_on_a_symlinked_home(symlinked_home, tmp_path) -> None:
    """The plan the producers write for this host, run through the mask stages with a covering bind.

    Every hiding mount is placed and read back for real, between the two crew-home
    alias checks, as the child runs them.
    """
    link_home, data_home = symlinked_home
    (link_home / ".ssh").mkdir()
    (link_home / ".ssh" / "known_hosts").write_text("example.com ssh-rsa AAAA\n")
    (link_home / ".kiro" / "agents").mkdir(parents=True)
    plan = _probe_plan(link_home, data_home)
    libc = CoveringLibc()
    run = launch(tmp_path, _child_plan(plan), libc=libc, environ={"HOME": str(link_home)})
    try:
        program.check_crew_home_aliases(run)
        program.place_masks(run)
        program.confirm_crew_home_aliases(run)
    except SystemExit as exc:
        pytest.fail(f"the launcher refused on a symlinked home: {exc.code}")
    assert libc.covered.count(str(data_home)) == 1, "the data home was masked more than once"
    assert not (data_home / "diag").exists(), "the stand-in is not empty"
    assert run.masked_names, "no mask was recorded"
    for name, stand_in_id in run.masked_names.items():
        assert tuple(stand_in_id) in run.own_stand_ins, name


def test_the_folded_alias_travels_with_the_identity_it_rested_on(symlinked_home) -> None:
    link_home, data_home = symlinked_home
    aliases = _probe_plan(link_home, data_home)["crew_home_aliases"]
    info = os.stat(data_home)
    assert [str(link_home / ".kirocrew"), str(data_home), info.st_dev, info.st_ino] in aliases
    # ``.kiro/crew`` is absent on this host, so it is no alias and keeps its own rules.
    assert not any(alias.endswith(".kiro/crew") for alias, *_ in aliases)


def test_a_plain_home_folds_nothing(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".kirocrew").mkdir(parents=True)
    monkeypatch.setattr(sb.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(sb, "config_dir", lambda: home / ".kirocrew")
    assert sb._crew_home_alias_roots() == ()


def test_a_bind_mounted_data_home_keeps_its_own_spelling(tmp_path, monkeypatch) -> None:
    """Same identity is not the test; the same NAME is.

    A data home bind-mounted at ``$HOME/.kirocrew`` (``KIROCREW_HOME=/srv/crew``,
    ``mount --bind /srv/crew ~/.kirocrew``) reports the source's ``(st_dev,
    st_ino)`` under both paths, yet it is a second mount: a mask placed on the
    canonical path's entry does not appear under the bind. Folding the alias
    onto the canonical would leave every leaf under the alias unmasked, so a
    pair that merely shares an identity is not folded. No mount is made here:
    two plain directories are given one identity, and no link joins their
    names.
    """
    elsewhere = tmp_path / "srv" / "crew"
    elsewhere.mkdir(parents=True)
    home = tmp_path / "home"
    alias = home / ".kirocrew"
    alias.mkdir(parents=True)
    canonical_id = os.stat(elsewhere)
    real_stat = os.stat

    def one_identity(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.fspath(path) in (str(alias), str(elsewhere)):
            return os.stat_result(
                (
                    result.st_mode,
                    canonical_id.st_ino,
                    canonical_id.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(sb.os, "stat", one_identity)
    monkeypatch.setattr(sb.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(sb, "config_dir", lambda: elsewhere)
    a, b = sb.os.stat(str(alias)), sb.os.stat(str(elsewhere))
    assert (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino), "the fixture did not share the identity"
    assert sb._crew_home_alias_roots() == ()


def test_an_alias_re_aimed_after_the_fold_refuses_before_any_mask(symlinked_home, tmp_path) -> None:
    """The fold is decided in the producer and acted on in the child; the link in between is a name.

    A writer re-aims the home link after the producer looked and before the child
    mounts. The alias spelling then reaches another directory, one no folded rule
    covers. The child reads the alias again, ahead of every hiding mount, and
    refuses the spawn. The same check passes while the link still holds.
    """
    link_home, data_home = symlinked_home
    plan = _probe_plan(link_home, data_home)
    assert plan["crew_home_aliases"], "the fixture produced no alias to re-aim"
    run = launch(tmp_path, plan)

    assert refusal(program.check_crew_home_aliases, run) is None  # the link holds

    elsewhere = _re_aim_home(tmp_path, "elsewhere")
    (elsewhere / ".kirocrew" / ".env").write_text("PLANTED=1\n")
    assert not os.path.samefile(link_home / ".kirocrew", data_home)

    refused = refusal(program.check_crew_home_aliases, run)
    assert refused is not None and "reaches a different directory now" in refused


def test_a_canonical_swapped_under_a_still_true_alias_refuses(symlinked_home, tmp_path) -> None:
    """Reading the alias alone would pass this swap; the canonical spelling is read too.

    A writer moves the data home aside, puts a fresh directory under its canonical
    name, and re-aims the home link so the alias still reaches the ORIGINAL. The
    alias then stats to the recorded identity, every folded rule would land on
    the replacement, and the original would be reachable through the alias. The
    re-read requires the canonical spelling to reach the recorded directory too.
    """
    link_home, data_home = symlinked_home
    plan = _probe_plan(link_home, data_home)
    assert plan["crew_home_aliases"], "the fixture produced no alias to swap under"
    run = launch(tmp_path, plan)

    assert refusal(program.check_crew_home_aliases, run) is None  # nothing moved

    aside = tmp_path / "aside" / "home" / "u"
    aside.mkdir(parents=True)
    data_home.rename(aside / ".kirocrew")  # the original keeps its identity
    data_home.mkdir()  # a fresh directory under the canonical name
    home_link = tmp_path / "home"
    home_link.unlink()
    home_link.symlink_to(tmp_path / "aside" / "home", target_is_directory=True)
    assert os.path.samefile(link_home / ".kirocrew", aside / ".kirocrew")
    assert not os.path.samefile(link_home / ".kirocrew", data_home)

    refused = refusal(program.check_crew_home_aliases, run)
    assert refused is not None and "holds a different directory now" in refused
    assert str(data_home) in refused, "the refusal names the swapped spelling"


def test_the_alias_re_read_runs_ahead_of_every_hiding_mount(symlinked_home, tmp_path) -> None:
    """Ordering is the guarantee: a re-aimed alias refuses the child before its first mask.

    The whole child run is started with the link already re-aimed, over a plan with
    masks to place; the refusal comes from the alias re-read and no mount has been
    made by then.
    """
    link_home, data_home = symlinked_home
    plan = _probe_plan(link_home, data_home)
    assert plan["crew_home_aliases"] and plan["sensitive_dirs"], "the fixture drifted"
    _re_aim_home(tmp_path, "elsewhere")
    libc = CoveringLibc()
    run = launch(tmp_path, _child_plan(plan), libc=libc, environ={"HOME": str(link_home)})

    refused = refusal(program.run_child, run, ["/usr/bin/env", "kiro-cli"])

    assert refused is not None and "reaches a different directory now" in refused
    assert libc.calls == [], "a hiding mount was placed before the alias was read again"
    assert run.execs == []


def test_an_alias_re_aimed_between_the_read_and_the_mounts_refuses_after_them(
    symlinked_home, tmp_path
) -> None:
    """The first read closes nothing by itself: a link re-aimed right after it holds until the masks land.

    So the alias is read again once every hiding mount is placed, against what
    the canonical spelling reaches then. Two names for one directory both reach
    the stand-in on its dentry and agree; an alias re-aimed in the window
    reaches an unmasked directory and disagrees, and the spawn is refused.
    """
    link_home, data_home = symlinked_home
    plan = _probe_plan(link_home, data_home)
    assert plan["crew_home_aliases"], "the fixture produced no alias to re-aim"
    run = launch(tmp_path, plan)

    assert refusal(program.confirm_crew_home_aliases, run) is None  # both names, one directory

    _re_aim_home(tmp_path, "elsewhere")
    assert not os.path.samefile(link_home / ".kirocrew", data_home)

    refused = refusal(program.confirm_crew_home_aliases, run)
    assert refused is not None and "now that the masks are placed" in refused


class _ReAimingAtTheLastMask(CoveringLibc):
    """A covering libc that re-aims the ``$HOME`` link as it binds the ``~/.ssh`` mask.

    The writer lands after the alias's first read, inside the last hiding mount. The
    bound ``~/.ssh`` stand-in moves with the link, as a mounted mask stays on its
    name, so that mask's own read-back still reaches it and only the alias tells the
    move apart.
    """

    def __init__(self, ssh: Path, re_aim) -> None:  # noqa: ANN001
        super().__init__()
        self.ssh = str(ssh)
        self.re_aim = re_aim
        self.fired = False

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        result = super().bound(source, target, fstype, flags)
        if not self.fired and self.covered and self.covered[-1] == self.ssh:
            self.fired = True
            self.re_aim()
        return result


@_LINUX_ONLY
def test_the_post_mount_alias_read_runs_after_every_hiding_mount(symlinked_home, tmp_path) -> None:
    """Ordering is the guarantee: the second read sits after the last mask, the ssh one.

    A re-aim that lands while the ``~/.ssh`` mask is being bound -- after the first
    read, inside the last hiding mount -- is still refused, and the agent is never
    exec'd.
    """
    link_home, data_home = symlinked_home
    real_ssh = data_home.parent / ".ssh"
    real_ssh.mkdir()
    (real_ssh / "known_hosts").write_text("example.com ssh-rsa AAAA\n")
    plan = _probe_plan(link_home, data_home)
    assert plan["crew_home_aliases"] and plan["hide_ssh"], "the fixture drifted"

    def _re_aim() -> None:
        moved = tmp_path / "elsewhere" / "home" / "u"
        moved.mkdir(parents=True)
        real_ssh.rename(moved / ".ssh")
        _re_aim_home(tmp_path, "elsewhere")

    libc = _ReAimingAtTheLastMask(real_ssh, _re_aim)
    run = launch(tmp_path, _child_plan(plan), libc=libc, environ={"HOME": str(link_home)})

    refused = refusal(program.run_child, run, ["/usr/bin/env", "kiro-cli"])

    assert libc.fired, "the ~/.ssh mask was never bound"
    assert libc.calls[-1].target_path == str(real_ssh), "the ~/.ssh mask is not the last mount"
    assert refused is not None and "now that the masks are placed" in refused
    assert run.execs == []


def _one_identity(*shared: Path):
    """An ``os.stat`` that reports one identity for every path in *shared*.

    Resolution stays honest: ``os.path`` is untouched, so ``realpath`` still walks
    the real links. This is a second mount of the data home as a gate would see it
    -- the same ``(st_dev, st_ino)`` under a name that does not resolve to the
    canonical one -- without a mount.
    """
    real_stat = os.stat
    names = {os.path.realpath(str(path)) for path in shared}
    anchor = real_stat(str(next(iter(shared))))

    def one_identity(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if isinstance(path, (str, bytes, os.PathLike)) and os.path.realpath(path) in names:
            return os.stat_result(
                (
                    result.st_mode,
                    anchor.st_ino,
                    anchor.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    return one_identity


_GATES = [program.check_crew_home_aliases, program.confirm_crew_home_aliases]


@pytest.mark.parametrize("gate", _GATES, ids=["before", "after"])
def test_an_alias_re_aimed_at_a_second_mount_of_the_data_home_refuses(
    symlinked_home, tmp_path, monkeypatch, gate
) -> None:
    """Identity is not the test at either gate; the name's resolution is.

    A second mount of the data home reports the data home's ``(st_dev, st_ino)``
    under another name, and a mask placed on the canonical entry does not appear
    under it. An alias re-aimed at such a mount would pass an identity test at
    both gates with every folded leaf unmasked beneath it. Each gate resolves the
    alias and requires the canonical path itself.
    """
    link_home, data_home = symlinked_home
    plan = _probe_plan(link_home, data_home)
    assert plan["crew_home_aliases"], "the fixture produced no alias to re-aim"
    run = launch(tmp_path, plan)

    second = tmp_path / "second" / "home" / "u"
    (second / ".kirocrew").mkdir(parents=True)
    one_identity = _one_identity(data_home, second / ".kirocrew")

    with monkeypatch.context() as patched:
        patched.setattr(program.os, "stat", one_identity)
        assert refusal(gate, run) is None  # the link holds

    _re_aim_home(tmp_path, "second")
    a, b = one_identity(str(link_home / ".kirocrew")), one_identity(str(data_home))
    assert (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino), "the fixture did not share the identity"

    with monkeypatch.context() as patched:
        patched.setattr(program.os, "stat", one_identity)
        refused = refusal(gate, run)
    assert (
        refused is not None and str(second / ".kirocrew") in refused
    ), "the refusal names where the alias went"


@pytest.mark.parametrize("gate", _GATES, ids=["before", "after"])
def test_a_legacy_link_canonical_under_a_symlinked_home_spawns(tmp_path, monkeypatch, gate) -> None:
    """The canonical spelling is the passes' spelling, and it may be a link itself.

    A default data home on a symlinked host: ``config_dir()`` is ``$HOME/.kiro/crew``,
    unresolved on both counts -- ``$HOME`` is a link and ``.kiro/crew`` is the
    migration link to ``.kirocrew``. The producer pairs ``$HOME/.kirocrew`` with
    that canonical because both RESOLVE to the data home. The gates must compare
    resolutions on both sides: against the raw canonical string every spawn on
    this layout would be refused on an untouched filesystem, before any mask.
    """
    real_home = tmp_path / "mnt" / "home" / "u"
    data_home = real_home / ".kirocrew"
    for leaf in ("diag", "run", "apps/aws-control/data", "quarantined-clones"):
        (data_home / leaf).mkdir(parents=True)
    (real_home / ".kiro").mkdir()
    (real_home / ".kiro" / "crew").symlink_to(data_home, target_is_directory=True)
    (tmp_path / "home").symlink_to(tmp_path / "mnt" / "home", target_is_directory=True)
    link_home = tmp_path / "home" / "u"
    legacy = link_home / ".kiro" / "crew"
    assert os.path.realpath(legacy) == str(data_home) and str(legacy) != os.path.realpath(legacy)
    monkeypatch.setattr(sb.Path, "home", classmethod(lambda _cls: link_home))
    monkeypatch.setattr(sb, "config_dir", lambda: legacy)
    monkeypatch.setattr(sb, "_backend", "namespace")

    pairs = sb._crew_home_alias_roots()
    assert [(a, c) for a, c, _d, _i in pairs] == [(str(link_home / ".kirocrew"), str(legacy))]

    run = launch(tmp_path, _probe_plan(link_home, legacy))
    assert refusal(gate, run) is None  # untouched filesystem: no refusal

    elsewhere = _re_aim_home(tmp_path, "elsewhere")
    (elsewhere / ".kiro").mkdir()
    (elsewhere / ".kiro" / "crew").symlink_to(data_home, target_is_directory=True)
    # The alias now resolves elsewhere; the canonical still to the data home.
    assert refusal(gate, run) is not None
