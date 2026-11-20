from __future__ import annotations

import os

import pytest

from kiro_crew import pinned_fs_primitives, skill_trust
from kiro_crew import skills_discovery as discovery

needs_descriptor_walk = pytest.mark.skipif(
    not (pinned_fs_primitives.supports_pinned_tree_walk() and os.scandir in os.supports_fd),
    reason="the descriptor-relative skill walk is unavailable on this platform",
)
needs_symlinks = pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks only")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    skill_trust.reset_cache_for_tests()
    yield
    skill_trust.reset_cache_for_tests()


def _skill(root, rel):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {rel.rsplit('/', 1)[-1]}\ndescription: d\n---\n\nBODY\n",
        encoding="utf-8",
    )


def _names(base):
    return [name for name, _ in discovery._iter_skill_files(base)]


def _open_fds():
    return len(os.listdir("/dev/fd"))


def _walk_by_name(monkeypatch):
    monkeypatch.setattr(pinned_fs_primitives, "supports_pinned_tree_walk", lambda: False)


def _swap_on(monkeypatch, trigger, action):
    real_skip = discovery._skippable_skill_dir
    fired = []

    def swapping(name):
        if name == trigger and not fired:
            fired.append(name)
            action()
        return real_skip(name)

    monkeypatch.setattr(discovery, "_skippable_skill_dir", swapping)
    return fired


@needs_descriptor_walk
@needs_symlinks
@pytest.mark.parametrize("by_descriptor", [True, False])
def test_folder_swapped_for_an_outside_link_after_listing_is_refused(
    tmp_path, monkeypatch, by_descriptor
):
    if not by_descriptor:
        _walk_by_name(monkeypatch)
    base = tmp_path / "skills"
    _skill(base, "keep")
    _skill(base, "victim")
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap():
        os.rename(base / "victim", outside / "victim")
        os.symlink(outside / "victim", base / "victim")

    fired = _swap_on(monkeypatch, "victim", swap)

    assert _names(base) == ["keep"]
    assert fired == ["victim"]


@needs_descriptor_walk
@needs_symlinks
@pytest.mark.parametrize("by_descriptor", [True, False])
def test_skill_file_swapped_for_an_outside_link_after_listing_is_refused(
    tmp_path, monkeypatch, by_descriptor
):
    if not by_descriptor:
        _walk_by_name(monkeypatch)
    base = tmp_path / "skills"
    _skill(base, "keep")
    _skill(base, "victim")
    (base / "victim" / "trigger").mkdir()
    outside = tmp_path / "outside"
    _skill(outside, "secret")

    def swap():
        os.unlink(base / "victim" / "SKILL.md")
        os.symlink(outside / "secret" / "SKILL.md", base / "victim" / "SKILL.md")

    fired = _swap_on(monkeypatch, "trigger", swap)

    assert _names(base) == ["keep"]
    assert fired == ["trigger"]


@needs_symlinks
def test_a_linked_skill_folder_under_an_allowed_root_still_loads(tmp_path, monkeypatch):
    provider = tmp_path / "provider"
    _skill(provider, "app-skill")
    _skill(provider, "app-skill/nested")
    base = tmp_path / "skills"
    _skill(base, "own")
    os.symlink(provider / "app-skill", base / "linked")
    (base / "own" / "SKILL.md").unlink()
    os.symlink(provider / "app-skill" / "SKILL.md", base / "own" / "SKILL.md")
    monkeypatch.setattr(discovery, "_trusted_skill_roots", lambda: (os.path.realpath(provider),))

    rows = dict(discovery._iter_skill_files(base))

    assert sorted(rows) == ["linked", "linked/nested", "own"]
    assert rows["linked"] == base / "linked" / "SKILL.md"
    assert rows["linked/nested"] == base / "linked" / "nested" / "SKILL.md"


@needs_symlinks
def test_a_linked_folder_whose_target_is_sensitive_is_refused(tmp_path, monkeypatch):
    provider = tmp_path / "provider"
    _skill(provider, "app-skill")
    _skill(provider, "secret")
    base = tmp_path / "skills"
    base.mkdir()
    os.symlink(provider / "app-skill", base / "linked")
    os.symlink(provider / "secret", base / "creds")
    fenced = os.path.realpath(provider / "secret")
    monkeypatch.setattr(discovery, "_trusted_skill_roots", lambda: (os.path.realpath(provider),))
    monkeypatch.setattr(
        discovery,
        "is_sensitive_resolved_path",
        lambda path: path == fenced or path.startswith(fenced + os.sep),
    )

    assert _names(base) == ["linked"]


def _mixed_tree(tmp_path):
    provider = tmp_path / "provider"
    _skill(provider, "app-skill")
    outside = tmp_path / "outside"
    _skill(outside, "stray")
    base = tmp_path / "skills"
    _skill(base, "family/leaf")
    _skill(base, "family/SKILL.md")
    _skill(base, "own")
    _skill(base, "own/scripts/node_modules/vendored")
    _skill(base, ".hidden/skill")
    os.symlink(provider / "app-skill", base / "app")
    os.symlink(base / "own", base / "alias")
    os.symlink(outside / "stray", base / "stray")
    os.symlink(tmp_path / "missing", base / "dangling")
    return base, provider


@needs_descriptor_walk
@needs_symlinks
def test_the_by_name_fallback_answers_what_the_descriptor_walk_answers(tmp_path, monkeypatch):
    base, provider = _mixed_tree(tmp_path)
    monkeypatch.setattr(discovery, "_trusted_skill_roots", lambda: (os.path.realpath(provider),))
    by_descriptor = discovery._iter_skill_files(base)

    real_os_open = os.open

    def refuse(path, *args, **kwargs):
        if os.path.abspath(os.fspath(path)).startswith(str(tmp_path)) or "dir_fd" in kwargs:
            raise AssertionError(f"the by-name walk opened {path!r} as a descriptor")
        return real_os_open(path, *args, **kwargs)

    _walk_by_name(monkeypatch)
    monkeypatch.setattr(os, "open", refuse)
    by_name = discovery._iter_skill_files(base)
    monkeypatch.setattr(os, "open", real_os_open)

    assert by_name == by_descriptor
    assert [name for name, _ in by_name] == ["alias", "app", "family/SKILL.md", "family/leaf"]


@needs_descriptor_walk
def test_realpath_runs_once_per_skill_file_and_never_per_directory(tmp_path, monkeypatch):
    small = tmp_path / "small"
    large = tmp_path / "large"
    for index in range(3):
        _skill(small, f"family/skill-{index}")
        (small / "family" / f"skill-{index}" / "references").mkdir()
    for index in range(60):
        _skill(large, f"family/skill-{index}")
        (large / "family" / f"skill-{index}" / "references").mkdir()
    real_realpath = os.path.realpath
    calls = []
    walked = []

    def counting(path, *args, **kwargs):
        if os.fspath(path).startswith(str(walked[-1])):
            calls.append(path)
        return real_realpath(path, *args, **kwargs)

    counts = []
    for base in (small, large):
        calls.clear()
        walked.append(base)
        with monkeypatch.context() as patch:
            patch.setattr(os.path, "realpath", counting)
            discovery._iter_skill_files(base)
        counts.append(len(calls))

    assert counts == [1 + 3, 1 + 60]


@needs_descriptor_walk
@needs_symlinks
def test_a_link_retargeted_after_it_was_resolved_is_refused(tmp_path, monkeypatch):
    provider = tmp_path / "provider"
    _skill(provider, "app-skill")
    outside = tmp_path / "outside"
    _skill(outside, "stray")
    base = tmp_path / "skills"
    _skill(base, "keep")
    os.symlink(provider / "app-skill", base / "linked")
    approved = os.path.realpath(provider / "app-skill")
    monkeypatch.setattr(discovery, "_trusted_skill_roots", lambda: (os.path.realpath(provider),))
    real_realpath = os.path.realpath
    fired = []

    def retargeting(path, *args, **kwargs):
        resolved = real_realpath(path, *args, **kwargs)
        if resolved == approved and not fired and os.fspath(path).endswith(os.sep + "linked"):
            fired.append(resolved)
            os.unlink(base / "linked")
            os.symlink(outside / "stray", base / "linked")
        return resolved

    monkeypatch.setattr(os.path, "realpath", retargeting)

    assert _names(base) == ["keep"]
    assert fired == [approved]


@needs_descriptor_walk
@needs_symlinks
def test_an_opened_folder_renamed_out_and_linked_back_is_refused(tmp_path, monkeypatch):
    base = tmp_path / "skills"
    _skill(base, "keep")
    _skill(base, "victim")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = os.stat(base / "victim")
    real_scandir = os.scandir
    fired = []

    def renaming(target=".", *args, **kwargs):
        st = os.fstat(target) if isinstance(target, int) else os.stat(target)
        if (st.st_dev, st.st_ino) == (victim.st_dev, victim.st_ino) and not fired:
            fired.append(target)
            os.rename(base / "victim", outside / "victim")
            os.symlink(outside / "victim", base / "victim")
        return real_scandir(target, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", renaming)
    monkeypatch.setattr(os, "supports_fd", os.supports_fd | {renaming})

    assert _names(base) == ["keep"]
    assert len(fired) == 1


def _comb(base, depth):
    current = base
    for level in range(depth):
        _skill(current, f"z-{level}")
        current = current / f"d-{level}"
    _skill(current, "leaf")


@needs_descriptor_walk
def test_held_descriptors_stay_bounded_on_a_deep_comb(tmp_path, monkeypatch):
    base = tmp_path / "skills"
    _comb(base, 40)
    expected = discovery._iter_skill_files(base)
    monkeypatch.setattr(discovery, "_HELD_DIRS_MAX", 2, raising=False)
    before = _open_fds()
    peak = [0]
    real_skip = discovery._skippable_skill_dir

    def sampling(name):
        peak[0] = max(peak[0], _open_fds() - before)
        return real_skip(name)

    monkeypatch.setattr(discovery, "_skippable_skill_dir", sampling)
    rows = discovery._iter_skill_files(base)

    assert rows == expected
    assert len(rows) == 41
    assert peak[0] <= 2 + 3 * discovery._CATALOG_READ_WORKERS
    assert _open_fds() == before


@needs_descriptor_walk
def test_descriptor_exhaustion_restarts_the_walk_by_name(tmp_path, monkeypatch):
    import errno

    base = tmp_path / "skills"
    _skill(base, "keep")
    _skill(base, "victim/nested")
    expected = discovery._iter_skill_files(base)
    real_os_open = os.open
    real_scandir = os.scandir
    victim = str(base / "victim")
    refused = []
    restarted = []

    def exhausted(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and path == "victim" and not refused:
            refused.append(path)
            raise OSError(errno.EMFILE, "Too many open files")
        return real_os_open(path, flags, mode, dir_fd=dir_fd)

    def full_until_restart(target=".", *args, **kwargs):
        if refused and not restarted and not isinstance(target, int):
            if os.fspath(target) == str(base):
                restarted.append(target)
            elif os.fspath(target) == victim:
                raise OSError(errno.EMFILE, "Too many open files")
        return real_scandir(target, *args, **kwargs)

    monkeypatch.setattr(os, "open", exhausted)
    monkeypatch.setattr(os, "scandir", full_until_restart)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {exhausted})
    monkeypatch.setattr(os, "supports_fd", os.supports_fd | {full_until_restart})
    before = _open_fds()
    rows = discovery._iter_skill_files(base)

    assert rows == expected
    assert refused == ["victim"]
    assert len(restarted) == 1
    assert _open_fds() == before


@needs_descriptor_walk
@needs_symlinks
def test_the_walk_closes_every_descriptor_it_opens(tmp_path, monkeypatch):
    base, provider = _mixed_tree(tmp_path)
    for index in range(40):
        _skill(base, f"wide/skill-{index}/deeper")
    monkeypatch.setattr(discovery, "_trusted_skill_roots", lambda: (os.path.realpath(provider),))
    before = _open_fds()

    discovery._iter_skill_files(base)

    assert _open_fds() == before


@needs_descriptor_walk
def test_a_walk_that_raises_still_closes_its_descriptors(tmp_path, monkeypatch):
    base = tmp_path / "skills"
    for index in range(40):
        _skill(base, f"wide/skill-{index}/deeper")
    real_skip = discovery._skippable_skill_dir

    def failing(name):
        if name == "deeper":
            raise RuntimeError("walk aborted")
        return real_skip(name)

    monkeypatch.setattr(discovery, "_skippable_skill_dir", failing)
    before = _open_fds()

    with pytest.raises(RuntimeError, match="walk aborted"):
        discovery._iter_skill_files(base)

    assert _open_fds() == before


@needs_descriptor_walk
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions"
)
def test_an_ancestor_without_read_permission_still_walks_by_name(tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    base = locked / "skills"
    _skill(base, "keep")
    _skill(base, "family/leaf")
    expected = ["family/leaf", "keep"]
    real_os_open = os.open
    denied = []

    def recording(path, *args, **kwargs):
        try:
            return real_os_open(path, *args, **kwargs)
        except PermissionError:
            denied.append(path)
            raise

    monkeypatch.setattr(os, "open", recording)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {recording})
    locked.chmod(0o311)
    try:
        names = _names(base)
    finally:
        locked.chmod(0o755)

    assert names == expected
    assert denied == ["locked"]


@needs_descriptor_walk
@needs_symlinks
def test_a_base_swapped_for_an_outside_link_mid_walk_lists_nothing_outside(tmp_path, monkeypatch):
    base = tmp_path / "skills"
    _skill(base, "keep")
    _skill(base, "zeta")
    outside = tmp_path / "outside"
    _skill(outside, "keep")
    _skill(outside, "zeta")

    def swap():
        os.rename(base, tmp_path / "aside")
        os.symlink(outside, base)

    fired = _swap_on(monkeypatch, "keep", swap)
    rows = discovery._iter_skill_files(base)

    assert fired == ["keep"]
    assert rows == []


@needs_descriptor_walk
def test_an_unreadable_descriptor_listing_falls_back_to_the_by_name_probe(tmp_path, monkeypatch):
    import errno

    base = tmp_path / "skills"
    _skill(base, "keep")
    _skill(base, "victim/nested")
    expected = discovery._iter_skill_files(base)
    victim = os.stat(base / "victim")
    real_scandir = os.scandir
    refused = []

    def denied(target=".", *args, **kwargs):
        if isinstance(target, int):
            st = os.fstat(target)
            if (st.st_dev, st.st_ino) == (victim.st_dev, victim.st_ino):
                refused.append(target)
                raise PermissionError(errno.EACCES, "Permission denied")
        return real_scandir(target, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", denied)
    monkeypatch.setattr(os, "supports_fd", os.supports_fd | {denied})
    before = _open_fds()
    rows = discovery._iter_skill_files(base)

    assert rows == expected
    assert len(refused) == 1
    assert _open_fds() == before


@needs_descriptor_walk
@needs_symlinks
def test_exhaustion_while_opening_a_linked_folder_restarts_by_name(tmp_path, monkeypatch):
    import errno

    provider = tmp_path / "provider-only-in-the-link"
    _skill(provider, "app-skill")
    base = tmp_path / "skills"
    _skill(base, "keep")
    os.symlink(provider / "app-skill", base / "linked")
    monkeypatch.setattr(discovery, "_trusted_skill_roots", lambda: (os.path.realpath(provider),))
    expected = discovery._iter_skill_files(base)
    real_os_open = os.open
    refused = []

    def exhausted(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and path == provider.name:
            refused.append(path)
            raise OSError(errno.ENFILE, "Too many open files in system")
        return real_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", exhausted)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {exhausted})
    before = _open_fds()
    rows = discovery._iter_skill_files(base)

    assert rows == expected
    assert [name for name, _ in rows] == ["keep", "linked"]
    assert refused == [provider.name]
    assert _open_fds() == before


@needs_descriptor_walk
@pytest.mark.parametrize("code", ["EACCES", "EPERM", "ENOTSUP", "EOPNOTSUPP"])
def test_a_refused_descriptor_open_falls_back_to_the_by_name_probe(tmp_path, monkeypatch, code):
    import errno

    base = tmp_path / "skills"
    _skill(base, "keep")
    _skill(base, "victim/nested")
    expected = discovery._iter_skill_files(base)
    real_os_open = os.open
    refused = []

    def refusing(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and path == "victim":
            refused.append(path)
            raise OSError(getattr(errno, code), code)
        return real_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", refusing)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {refusing})
    before = _open_fds()
    rows = discovery._iter_skill_files(base)

    assert rows == expected
    assert refused == ["victim"]
    assert _open_fds() == before


@needs_descriptor_walk
@needs_symlinks
def test_a_listed_folder_replaced_by_a_link_back_into_the_root_is_refused(tmp_path, monkeypatch):
    base = tmp_path / "skills"
    _skill(base, "keep")
    _skill(base, "victim")
    _skill(base, "victim/nested")
    outside = tmp_path / "outside" / "secret"
    outside.mkdir(parents=True)
    os.symlink(base / "keep" / "SKILL.md", outside / "SKILL.md")
    victim = os.stat(base / "victim")
    real_scandir = os.scandir
    fired = []

    def replacing(target=".", *args, **kwargs):
        listing = real_scandir(target, *args, **kwargs)
        st = os.fstat(target) if isinstance(target, int) else os.stat(target)
        if (st.st_dev, st.st_ino) == (victim.st_dev, victim.st_ino) and not fired:
            fired.append(target)
            os.rename(base / "victim", tmp_path / "aside")
            os.symlink(outside, base / "victim")
        return listing

    monkeypatch.setattr(os, "scandir", replacing)
    monkeypatch.setattr(os, "supports_fd", os.supports_fd | {replacing})

    assert _names(base) == ["keep"]
    assert len(fired) == 1
