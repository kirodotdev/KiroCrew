"""The win32 pod-seed branch, exercised on every platform.

``seed_home_from_scenario`` gates on ``pinned_fs.supports_pinned_tree_walk()``,
which is False on Windows because ``os.open`` and ``os.mkdir`` accept no
``dir_fd`` there. The Windows branch is otherwise pure Python, so these tests
force that gate False and ``IS_WINDOWS`` True and run the real code on
``tmp_path``. Only the containment WITNESS is mocked: ``fd_real_path`` reads
``GetFinalPathNameByHandleW`` on win32 and ``/proc/self/fd`` on Linux, so the
Linux route answers here and the tests that need a controlled witness pin what
the call receives rather than replacing the branch around it.

What only a real windows-latest run can establish is listed in the pod README's
Platform section: whether ``GetFinalPathNameByHandleW`` answers for a directory
handle opened with these flags, and whether a real junction is caught by
``is_reparse_point`` before any byte lands.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from kiro_crew import pinned_fs
from kiro_crew import seed as seed_mod
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> PodConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods-env"))
    monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    return PodConfig.load()


@pytest.fixture
def windows_seed(monkeypatch):
    """Force the win32 branch: no pinned tree walk, and IS_WINDOWS true."""
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(rt.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)


def _fixture_names() -> list[str]:
    return sorted(seed_mod.available_fixtures())


@pytest.fixture
def scenario() -> str:
    names = _fixture_names()
    assert names, "the packaged fixture registry must ship at least one scenario"
    return "minimal" if "minimal" in names else names[0]


# --------------------------------------------------------------------------
# The refusal that is NOT relaxed
# --------------------------------------------------------------------------
def test_a_non_windows_host_without_a_pinned_walk_still_refuses(cfg, scenario, monkeypatch):
    """Only win32 gets the by-name branch; every other such host keeps refusing.

    The gate is a capability probe, so a POSIX host that somehow lacks the walk is
    a host whose ancestors cannot be pinned AND whose reparse screening is not the
    documented substitute. It must not inherit the Windows compromise.
    """
    monkeypatch.setattr(rt.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(rt, "IS_WINDOWS", False)
    with pytest.raises(rt.PodError, match="refusing an unpinned pod seed"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)


def test_the_posix_branch_is_untouched_when_the_walk_is_available(cfg, scenario, monkeypatch):
    """A host WITH the pinned walk must never reach the Windows code.

    Pins that this change is additive: the POSIX path is selected by the same probe
    it always was, and setting IS_WINDOWS true cannot divert it.
    """
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(
        rt, "_seed_home_windows", lambda *a, **k: pytest.fail("the pinned host must not divert")
    )
    if not pinned_fs.supports_pinned_tree_walk():
        pytest.skip("this host has no pinned tree walk, so the POSIX branch cannot run here")
    assert rt.seed_home_from_scenario(cfg, "demo", scenario) is True


# --------------------------------------------------------------------------
# Reparse-point screening happens BEFORE any write
# --------------------------------------------------------------------------
def test_a_reparse_point_anywhere_above_the_home_is_refused_before_any_write(
    cfg, scenario, windows_seed, monkeypatch
):
    """A planted junction must be caught while the home still does not exist.

    This is the win32 stand-in for ``O_NOFOLLOW`` on every component of a pinned
    walk, so the assertion that matters is not only the refusal but that nothing
    was created: a screen that runs after the mkdir would already have written
    through the swapped component.
    """
    home = cfg.home_dir("demo")
    planted = home.parent

    def fake_is_reparse(path):
        return Path(path) == planted

    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", fake_is_reparse)
    monkeypatch.setattr(
        seed_mod, "copy_fixture_into_witnessed_dir", lambda *a: pytest.fail("must not copy")
    )
    with pytest.raises(rt.PodError, match="symbolic link or a junction"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)
    assert not home.exists(), "the home must not be created through a screened component"


def test_the_home_itself_being_a_reparse_point_is_refused(cfg, scenario, windows_seed, monkeypatch):
    home = cfg.home_dir("demo")
    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", lambda p: Path(p) == home)
    with pytest.raises(rt.PodError, match="symbolic link or a junction"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)


def test_the_screen_walks_from_the_root_downward(cfg, monkeypatch):
    """The OUTERMOST swapped component is the one reported.

    A screen that started at the home would name a leaf while an ancestor was the
    real redirect, which is the less useful half of the same fact.
    """
    home = cfg.home_dir("demo")
    seen: list[Path] = []

    def record(path):
        seen.append(Path(path))
        return False

    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", record)
    rt._refuse_reparse_chain(home)
    assert seen[0] == Path(home.anchor or seen[0])
    assert seen[-1] == home
    assert len(seen) == len(home.parents) + 1


# --------------------------------------------------------------------------
# The witness
# --------------------------------------------------------------------------
def test_a_home_whose_real_path_cannot_be_read_is_refused(cfg, scenario, windows_seed, monkeypatch):
    """Fail CLOSED with no witness: a by-name write would validate against nothing."""
    monkeypatch.setattr(rt.pinned_fs, "fd_real_path", lambda fd: None)
    monkeypatch.setattr(
        seed_mod, "copy_fixture_into_witnessed_dir", lambda *a: pytest.fail("must not copy")
    )
    with pytest.raises(rt.PodError, match="cannot be validated"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)


def test_the_witness_is_taken_on_the_homes_own_handle(cfg, scenario, windows_seed, monkeypatch):
    """The argument is pinned: the witness must come from the open descriptor.

    Passing a PATH here would re-resolve the name, which is the whole thing the
    witness exists to avoid, so the call receiving an int is the assertion.
    """
    home = cfg.home_dir("demo")
    got: list[object] = []
    real = pinned_fs.fd_real_path

    def spy(fd):
        got.append(fd)
        return real(fd) or str(home)

    monkeypatch.setattr(rt.pinned_fs, "fd_real_path", spy)
    assert rt.seed_home_from_scenario(cfg, "demo", scenario) is True
    assert got, "the branch must witness the home handle"
    assert all(isinstance(fd, int) for fd in got)


def test_a_home_that_changes_identity_mid_seed_is_refused_before_the_marker(
    cfg, scenario, windows_seed, monkeypatch
):
    """The re-witness is what makes the by-name destination detectable.

    It cannot undo bytes already written, which is the residual the branch's
    docstring names; what it must do is refuse to PUBLISH a completion marker over
    a home whose identity differs from the one that was witnessed.
    """
    answers = iter(["C:\\\\plane\\\\demo", "C:\\\\plane\\\\somewhere-else"])
    monkeypatch.setattr(rt.pinned_fs, "fd_real_path", lambda fd: next(answers, "x"))
    monkeypatch.setattr(
        seed_mod,
        "publish_fixture_manifest_into_witnessed_dir",
        lambda *a: pytest.fail("the marker must not be published after an identity change"),
    )
    with pytest.raises(rt.PodError, match="changed while it was being seeded"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)


# --------------------------------------------------------------------------
# The seed itself
# --------------------------------------------------------------------------
def test_a_fresh_home_is_seeded_with_the_marker_published_last(cfg, scenario, windows_seed):
    assert rt.seed_home_from_scenario(cfg, "demo", scenario) is True
    home = cfg.home_dir("demo")
    marker = home / seed_mod.FIXTURE_MANIFEST
    assert marker.is_file(), "the completion marker must be published"
    assert rt._seeded_scenario_in_dir(home) == scenario
    assert (home / "workspace").is_dir()
    config = json.loads((home / "config.json").read_text())
    assert isinstance(config, dict)


def test_the_config_floor_is_applied_to_the_seeded_home(cfg, scenario, windows_seed):
    """A seeded config must not be able to publish the pod or grab an identity."""
    rt.seed_home_from_scenario(cfg, "demo", scenario)
    config = json.loads((cfg.home_dir("demo") / "config.json").read_text())
    for section in rt.SEED_DISABLED_SECTIONS:
        node = config.get(section)
        if isinstance(node, dict) and "enabled" in node:
            assert node["enabled"] is False, section


def test_a_partial_home_with_no_marker_is_refused_rather_than_completed(
    cfg, scenario, windows_seed
):
    """A crash mid-copy must not be mistaken for a finished seed on the next up."""
    home = cfg.home_dir("demo")
    home.mkdir(parents=True)
    (home / "leftover.json").write_text("{}")
    with pytest.raises(rt.PodError, match="no completion marker"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)


def test_a_non_directory_home_refuses_in_the_pod_vocabulary(cfg, scenario, windows_seed):
    """The win32 twin of ``test_a_non_directory_home_refuses_instead_of_crashing``.

    A stale FILE at the pod-home name reaches ``pin_directory``, which refuses a
    non-directory with ``NotADirectoryError``. Nothing between here and the CLI
    catches ``OSError`` -- ``boot`` and the scenario call site catch ``PodError``
    only -- so an untranslated one escaped as a bare traceback: no FATAL line, no
    recorded refusal for ``pod status`` to report, no ``pod.boot`` audit event and a
    default exit 1 rather than the provisioning code. The POSIX branch has always
    translated the equivalent open; this asserts the Windows branch does too.
    """
    home = cfg.home_dir("demo")
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_text("stale file where a directory belongs")
    with pytest.raises(rt.PodError, match="not a plain directory"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)


def test_the_restart_path_translates_an_os_error_into_a_refusal(
    cfg, scenario, windows_seed, monkeypatch
):
    """The already-seeded path needs the same translation frame as the fresh one.

    ``_prepare_seeded_home_dir`` and the ``iterdir`` above it can both raise a bare
    ``OSError`` -- on Windows a sharing violation from another process holding a
    seeded file open is the ordinary way -- and they sat OUTSIDE the frame that
    turns one into a ``PodError``. The POSIX branch spans both paths with a single
    ``try``; so does this one now.
    """
    assert rt.seed_home_from_scenario(cfg, "demo", scenario) is True

    def denied(home_dir):
        raise PermissionError(errno.EACCES, "denied by the host")

    monkeypatch.setattr(rt, "_prepare_seeded_home_dir", denied)
    with pytest.raises(rt.PodError) as excinfo:
        rt.seed_home_from_scenario(cfg, "demo", scenario)
    assert "denied by the host" in str(excinfo.value)


def test_a_home_seeded_from_another_scenario_is_refused(cfg, scenario, windows_seed):
    names = _fixture_names()
    other = next((n for n in names if n != scenario), None)
    if other is None:
        pytest.skip("only one packaged fixture, so a mismatched marker cannot be built")
    assert rt.seed_home_from_scenario(cfg, "demo", other) is True
    with pytest.raises(rt.PodError, match="refusing to boot or overwrite"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)


def test_a_complete_home_for_the_same_scenario_is_restarted_unchanged(cfg, scenario, windows_seed):
    """Second call returns False and re-runs setup only, matching the POSIX contract."""
    assert rt.seed_home_from_scenario(cfg, "demo", scenario) is True
    payload = cfg.home_dir("demo") / "sessions-marker.txt"
    payload.write_text("kept")
    assert rt.seed_home_from_scenario(cfg, "demo", scenario) is False
    assert payload.read_text() == "kept", "a restart must not wipe the pod's own state"


def test_the_restart_path_is_pinned_and_witnessed_too(cfg, scenario, windows_seed, monkeypatch):
    """The already-seeded path has the least to hold, so pin it explicitly.

    A home that already carries this scenario's marker is restarted rather than
    re-seeded. Reading the completion marker and re-preparing the home purely BY
    NAME would let a pod seeded once be restarted out of a directory swapped in
    since, and the branch that does hold a pin -- the fresh seed -- never runs for
    it. Both paths take the pin, and the witness is re-read before the restart is
    allowed to return.
    """
    assert rt.seed_home_from_scenario(cfg, "demo", scenario) is True

    pinned: list[str] = []
    witnessed: list[int] = []
    real_pin = rt.pin_directory
    real_witness = rt.pinned_fs.fd_real_path

    def pin_spy(path):
        pinned.append(str(path))
        return real_pin(path)

    def witness_spy(fd):
        witnessed.append(fd)
        return real_witness(fd)

    monkeypatch.setattr(rt, "pin_directory", pin_spy)
    monkeypatch.setattr(rt.pinned_fs, "fd_real_path", witness_spy)

    assert rt.seed_home_from_scenario(cfg, "demo", scenario) is False

    home = str(cfg.home_dir("demo"))
    assert home in pinned, "the restart path must hold the home pinned, not just read it by name"
    assert len(witnessed) >= 2, (
        "the witness must be taken AND re-read on the restart path, so a home that "
        f"changed identity mid-restart is refused; saw {len(witnessed)} read(s)"
    )


def test_every_destination_is_created_exclusively(cfg, scenario, windows_seed, monkeypatch):
    """O_EXCL is what makes the by-name destination unable to overwrite anything.

    Pinned by argv: the copy must pass the source as a descriptor and the
    destination as a path this branch just created under the witnessed home.
    """
    calls: list[dict] = []
    real = pinned_fs.copy_file_pinned

    def spy(by_name, dst=None, **kwargs):
        calls.append({"dst": dst, "src_fd": kwargs.get("src_fd"), "dir_fd": kwargs.get("dir_fd")})
        return real(by_name, dst, **kwargs)

    monkeypatch.setattr(seed_mod.pinned_fs, "copy_file_pinned", spy)
    rt.seed_home_from_scenario(cfg, "demo", scenario)
    assert calls, "the fixture must be copied through copy_file_pinned"
    home = cfg.home_dir("demo")
    for call in calls:
        assert isinstance(call["src_fd"], int), "the source must be a pinned descriptor"
        assert call["dir_fd"] is None, "win32 has no dir_fd; a dir_fd form would be a lie"
        assert Path(str(call["dst"])).is_relative_to(home)


def test_a_link_in_the_fixture_tree_is_refused(cfg, scenario, windows_seed, monkeypatch):
    """The source is screened too: a link in the payload must not be followed."""
    src = seed_mod._resolve_fixture(scenario)
    monkeypatch.setattr(seed_mod.pinned_fs, "is_reparse_point", lambda p: Path(p) == src)
    with pytest.raises(rt.PodError, match="unsupported symlink"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)


def test_the_source_pin_is_the_authority_not_the_name_screen(
    cfg, scenario, windows_seed, monkeypatch
):
    """The screen is an early message; the PIN is what settles the source.

    A screen by name is a check-to-use window: it reports what the name meant a
    moment ago, and an adversary that can plant a junction chooses when to plant
    it. So the walk holds a pinned handle to each source directory — on Windows
    that also blocks a rename of it or of anything above it — and a refusal from
    the pin has to surface as the same ``unsupported symlink`` the screen reports,
    or the stronger check would answer in a shape no caller recognises.

    Driven by making the pin refuse while the screen passes, which is exactly the
    ordering a stale screen produces.
    """
    monkeypatch.setattr(seed_mod.pinned_fs, "is_reparse_point", lambda p: False)

    def _refuse(path):
        raise NotADirectoryError(f"reparse point at {path}")

    monkeypatch.setattr(seed_mod.platform_compat, "pin_directory", _refuse)

    with pytest.raises(rt.PodError, match="unsupported symlink"):
        rt.seed_home_from_scenario(cfg, "demo", scenario)


def test_the_marker_open_is_the_authority_not_its_name_screen(
    cfg, scenario, windows_seed, monkeypatch
):
    """The completion marker gets the same treatment as the tree it certifies.

    Publishing the marker is the COMMIT of the win32 seeding transaction, so a
    reparse point planted at its name after the screen would publish a host file's
    contents as this pod's marker, inside a home the agent can read. The open has
    to refuse in the same operation, and its refusal has to read as the link
    message the screen would have given.

    Driven by making the open refuse while the screen passes -- the ordering a
    stale screen produces.
    """
    home = cfg.home_dir("demo")
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(seed_mod.pinned_fs, "is_reparse_point", lambda p: False)

    def _refuse(path):
        raise OSError(errno.ELOOP, "reparse point")

    monkeypatch.setattr(seed_mod.platform_compat, "open_file_no_reparse", _refuse)

    with pytest.raises(seed_mod.SeedError, match="completion marker is a link"):
        seed_mod.publish_fixture_manifest_into_witnessed_dir(scenario, home)


def test_the_marker_read_refuses_a_link_at_its_own_name(cfg, scenario, windows_seed, monkeypatch):
    """A link planted at the marker name reads as absent, never as a valid seed."""
    home = cfg.home_dir("demo")
    home.mkdir(parents=True)
    marker = home / seed_mod.FIXTURE_MANIFEST
    marker.write_text(f'fixture-name: "{scenario}"\n')
    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", lambda p: Path(p) == marker)
    assert rt._seeded_scenario_in_dir(home) is None


def test_a_link_at_the_seeded_config_name_is_refused(cfg, windows_seed, monkeypatch, tmp_path):
    """A by-name config read that followed a link would import config from outside."""
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.json"
    config.write_text("{}")
    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", lambda p: Path(p) == config)
    with pytest.raises(rt.PodError, match="config.json is a link"):
        rt._prepare_seeded_home_dir(home)


def test_an_oversized_seeded_config_is_refused(cfg, windows_seed, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{" + " " * (1024 * 1024 + 8) + "}")
    with pytest.raises(rt.PodError, match="1 MiB pod setup limit"):
        rt._prepare_seeded_home_dir(home)


def test_a_non_object_seeded_config_is_refused(cfg, windows_seed, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("[]")
    with pytest.raises(rt.PodError, match="must contain a JSON object"):
        rt._prepare_seeded_home_dir(home)


def test_the_home_is_created_owner_only(cfg, scenario, windows_seed):
    """Pod homes are 0700 on every platform that reports a mode."""
    rt.seed_home_from_scenario(cfg, "demo", scenario)
    home = cfg.home_dir("demo")
    if os.name == "nt":  # pragma: no cover - NTFS ACLs, not mode bits
        pytest.skip("Windows reports no POSIX mode; the ACL is the control there")
    assert home.stat().st_mode & 0o777 == 0o700


def test_the_seeded_scenario_is_read_back_through_a_pinned_home_on_win32(
    cfg, scenario, windows_seed, monkeypatch
):
    """``pod up`` verifies the seed landed by reading the marker back, and on
    win32 that read must not reach ``open_dir_pinned`` (``os.O_DIRECTORY`` does not
    exist there). The home is held through ``pin_directory`` for the read and the
    marker is read by name under it."""
    assert rt.seed_home_from_scenario(cfg, "wt", scenario) is True
    pinned: list[Path] = []
    real_pin = rt.pin_directory

    def recording(path):
        pinned.append(Path(path))
        return real_pin(path)

    monkeypatch.setattr(rt, "pin_directory", recording)
    monkeypatch.setattr(
        rt.pinned_fs,
        "open_dir_pinned",
        lambda *a, **k: pytest.fail("the POSIX descriptor path ran on win32"),
    )
    assert rt.seeded_scenario_in_home(cfg, "wt") == scenario
    assert pinned == [cfg.home_dir("wt")]


def test_a_missing_home_reads_as_unseeded_on_win32(cfg, windows_seed):
    assert rt.seeded_scenario_in_home(cfg, "never-seeded") is None
