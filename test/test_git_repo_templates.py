"""``git_repo_templates``: a copied repo is private, clean to git, and built once.

One module-scoped fixture builds a template holding every way two repos name
each other (a bare remote, a clone of it, a linked worktree, a ``--shared``
clone with ``alternates``, a hard-linked local clone, a config include), copies
it twice, writes into the first copy with real git and records what git says.
The tests assert on that record, so git runs once for the module, not per test.
"""

from __future__ import annotations

import builtins
import hashlib
import io
import os
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import git_repo_templates as templates
import pytest
from git_maintenance_helpers import GIT_MAINTENANCE_OFF_CONFIG, git_maintenance_off_entries
from git_repo_templates import RepoTemplateError, RepoTemplates, copy_repo
from push_guard_helpers import _git

from conftest import requires_symlinks
from kiro_crew.testing.links import make_dir_link

repo_templates = templates.repo_templates  # the fixture, bound for this module

WORKTREES = ("work", "feat")
#: Values a config file must round-trip: a Windows path, quotes, comment
#: characters, edge spaces and a tab.
CONFIG_VALUES = (
    r"C:\Users\runner admin\AppData\Local\Temp\origin.git",
    "/tmp/plain/origin.git",
    'say "hi"',
    "a # b ; c",
    " lead and trail ",
    "tab\there",
)


def _snapshot(root: Path) -> dict[str, str]:
    """Every file under *root* by relative path: a digest of its bytes, or its link target."""
    found = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[rel] = "-> " + os.readlink(path)
        elif path.is_file():
            found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def _inodes(root: Path) -> set[tuple[int, int]]:
    return {(st.st_dev, st.st_ino) for p in root.rglob("*") if (st := p.lstat()) and p.is_file()}


def _index(root: Path, worktree: str) -> Path:
    git_dir = root / "work" / ".git"
    return git_dir / "index" if worktree == "work" else git_dir / "worktrees" / worktree / "index"


def _pointers(root: Path) -> dict[str, str]:
    """The text of every file in which git records a path to another repo."""
    names = {"config", "gitdir", ".git", "alternates"}
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.name in names and path.is_file()
    }


def _build(root: Path) -> None:
    origin, work = root / "origin.git", root / "work"
    origin.mkdir()
    _git(str(origin), "init", "--bare", "-b", "main")
    # A push starts receive-pack here without our env (see git_maintenance_helpers).
    for key, value in GIT_MAINTENANCE_OFF_CONFIG:
        _git(str(origin), "config", key, value)
    _git(str(root), "clone", str(origin), "work")
    (work / "README.md").write_text("hello\n", encoding="utf-8", newline="\n")
    (work / "sub").mkdir()
    (work / "sub" / "kept.txt").write_text("kept\n", encoding="utf-8", newline="\n")
    _git(str(work), "add", ".")
    _git(str(work), "commit", "-m", "seed")
    _git(str(work), "push", "-u", "origin", "main")
    _git(str(work), "remote", "set-url", "origin", str(origin))
    _git(str(work), "worktree", "add", "-q", "-b", "feat", str(root / "feat"))
    _git(str(root), "clone", "-q", "--shared", str(work), "shared")
    _git(str(root), "clone", "-q", "--local", str(work), "local")
    (root / "extra.gitconfig").write_text("[extra]\n\tkey = 1\n", encoding="utf-8")
    _git(str(work), "config", "include.path", str(root / "extra.gitconfig"))
    # A tracked file edited after the commit: it must stay modified in a copy.
    (work / "sub" / "kept.txt").write_text("edit\n", encoding="utf-8", newline="\n")


def _git_view(root: Path) -> dict[str, object]:
    """What git reports for the repos under *root*."""
    view: dict[str, object] = {
        "url": _git(str(root / "work"), "config", "--get", "remote.origin.url"),
        "local_url": _git(str(root / "local"), "config", "--get", "remote.origin.url"),
        "extra": _git(str(root / "work"), "config", "--get", "extra.key"),
        "common": _git(
            str(root / "feat"), "rev-parse", "--path-format=absolute", "--git-common-dir"
        ),
        "shared_head": _git(str(root / "shared"), "cat-file", "-t", "HEAD"),
        "refs": _git(str(root / "work"), "for-each-ref", "--format=%(refname) %(objectname)"),
        "origin_refs": _git(
            str(root / "origin.git"), "for-each-ref", "--format=%(refname) %(objectname)"
        ),
        "name": _git(str(root / "work"), "config", "--get", "--default", "", "user.name"),
    }
    for worktree in WORKTREES:
        view["changed " + worktree] = _git(str(root / worktree), "diff-files", "--name-only")
    return view


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    template = tmp_path_factory.mktemp("copy-proof-template")
    _build(template)
    before = _snapshot(template)
    template_view = _git_view(template)

    first = copy_repo(template, tmp_path_factory.mktemp("copy-proof-first"))
    first_view = _git_view(first)
    # Git's own refresh finds nothing to rewrite in an index it agrees with.
    untouched = {}
    for worktree in WORKTREES:
        index = _index(first, worktree).read_bytes()
        _git(str(first / worktree), "update-index", "-q", "--really-refresh")
        untouched[worktree] = _index(first, worktree).read_bytes() == index

    # Everything a test can do to its copy: a commit, a push, a config change, a ref update.
    work = first / "work"
    (work / "new.txt").write_text("new\n", encoding="utf-8")
    _git(str(work), "add", "new.txt")
    _git(str(work), "commit", "-m", "in the first copy")
    _git(str(work), "push", "origin", "main")
    _git(str(work), "config", "user.name", "First Copy")
    _git(str(work), "update-ref", "refs/heads/moved", "HEAD")
    _git(str(first / "feat"), "reset", "-q", "--hard", "main")
    _git(str(first / "shared"), "gc", "-q", "--prune=now")

    second = copy_repo(template, tmp_path_factory.mktemp("copy-proof-second"))
    return SimpleNamespace(
        template=template,
        first=first,
        second=second,
        before=before,
        after=_snapshot(template),
        template_view=template_view,
        template_view_after=_git_view(template),
        first_view=first_view,
        first_written=_git_view(first),
        second_view=_git_view(second),
        untouched=untouched,
    )


def test_writes_in_one_copy_never_reach_the_template(world: SimpleNamespace) -> None:
    assert world.after == world.before
    assert world.template_view_after == world.template_view


def test_the_next_copy_sees_nothing_the_first_copy_wrote(world: SimpleNamespace) -> None:
    assert world.first_written["refs"] != world.first_view["refs"], "the first copy wrote nothing"
    assert world.first_written["origin_refs"] != world.first_view["origin_refs"]
    assert world.first_written["name"] == "First Copy"
    for key in ("refs", "origin_refs", "name", "extra", "shared_head"):
        assert world.second_view[key] == world.template_view[key], key


@pytest.mark.parametrize("copy", ["first", "second"])
def test_a_copy_names_its_own_repos_and_never_the_template(
    world: SimpleNamespace, copy: str
) -> None:
    root = getattr(world, copy)
    view = world.first_view if copy == "first" else world.second_view
    assert view["url"] == str(root / "origin.git")
    assert Path(str(view["local_url"])) == root / "work"
    assert Path(str(view["common"])) == root / "work" / ".git"
    assert view["extra"] == "1" and view["shared_head"] == "commit"
    for name, text in _pointers(root).items():
        assert world.template.name not in text, name


def test_no_file_of_a_copy_is_the_templates_file(world: SimpleNamespace) -> None:
    # ``git clone --local`` hard-links objects inside the template; a copy must not
    # carry a link to them, nor share one with another copy.
    theirs = _inodes(world.template)
    linked = [
        p for p in (world.template / "local").rglob("*") if p.is_file() and p.stat().st_nlink > 1
    ]
    assert theirs
    if sys.platform != "win32":  # the clone copies instead where it cannot link
        assert linked, "the template has no hard link to guard against"
    assert not theirs & _inodes(world.first)
    assert not theirs & _inodes(world.second)
    assert not _inodes(world.first) & _inodes(world.second)


@pytest.mark.parametrize("worktree", WORKTREES)
def test_git_reads_a_fresh_copy_as_it_reads_the_template(
    world: SimpleNamespace, worktree: str
) -> None:
    key = "changed " + worktree
    assert world.first_view[key] == world.template_view[key] == world.second_view[key]
    assert world.untouched[worktree], "git's refresh rewrote an index the copy had re-read"


def test_a_file_edited_in_the_template_stays_edited_in_a_copy(world: SimpleNamespace) -> None:
    # The re-read takes a file's stat data only when its bytes are the indexed
    # blob, so it cannot make a modified file look clean.
    assert world.template_view["changed work"] == "sub/kept.txt"
    assert world.second_view["changed work"] == "sub/kept.txt"
    assert world.second_view["changed feat"] == ""


def test_a_copy_git_reads_differently_fails_the_test_that_asked(
    world: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(templates, "_CHECKED", set())
    monkeypatch.setattr(templates, "_restat_index", lambda index, worktree: None)
    with pytest.raises(RepoTemplateError, match="as changed where the template is not"):
        copy_repo(world.template, tmp_path / "stale")


def test_the_first_copy_of_a_template_is_the_only_one_git_checks(
    world: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks: list[Path] = []
    monkeypatch.setattr(templates, "_CHECKED", set())
    monkeypatch.setattr(templates, "_check_first_copy", lambda t, d, w: checks.append(d))
    for name in ("one", "two", "three"):
        copy_repo(world.template, tmp_path / name)
    assert checks == [tmp_path / "one"]


def test_every_git_the_helper_starts_runs_with_auto_maintenance_off(
    world: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envs: list[dict[str, str]] = []

    def record(argv, **kwargs):  # type: ignore[no-untyped-def]
        envs.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(templates, "_CHECKED", set())
    monkeypatch.setattr(templates.subprocess, "run", record)
    copy_repo(world.template, tmp_path / "copy")
    monkeypatch.undo()
    assert len(envs) == len(WORKTREES) + 2, "one check per working tree: work, feat, shared, local"
    for env in envs:
        assert git_maintenance_off_entries(env) == {}, "the env leaves auto maintenance on"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def _bare_git_dir(root: Path) -> Path:
    """A directory git would take for a repository, made without git."""
    git_dir = root / "repo" / ".git"
    (git_dir / "objects" / "info").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    return git_dir


def test_an_object_store_outside_the_copy_is_refused(tmp_path: Path) -> None:
    template, outside = tmp_path / "template", tmp_path / "elsewhere" / "objects"
    git_dir = _bare_git_dir(template)
    (git_dir / "objects" / "info" / "alternates").write_text(f"{outside}\n", encoding="utf-8")
    with pytest.raises(RepoTemplateError, match="shares an object store outside the copy"):
        copy_repo(template, tmp_path / "copy")


@pytest.mark.parametrize(
    "line",
    ["[include]\n\tpath = file://{root}/extra\n", '[remote "o"]\n\turl = "{root}/origin.git\n'],
    ids=["a-path-inside-a-longer-value", "a-value-that-runs-on"],
)
def test_a_config_value_that_cannot_be_re_pointed_is_refused(tmp_path: Path, line: str) -> None:
    template = tmp_path / "template"
    git_dir = _bare_git_dir(template)
    with (git_dir / "config").open("a", encoding="utf-8") as config:
        config.write(line.format(root=template.as_posix()))
    with pytest.raises(RepoTemplateError, match="names the template"):
        copy_repo(template, tmp_path / "copy")


def test_only_git_s_own_files_are_re_pointed(tmp_path: Path) -> None:
    # A template named like a path segment others use, a URL that shares only
    # that name, and working files named like git's own.
    template = tmp_path / "repo"
    git_dir = _bare_git_dir(template)
    url = "https://example.invalid/org/repo"
    with (git_dir / "config").open("a", encoding="utf-8") as config:
        config.write(f'[remote "o"]\n\turl = {url}\n\tpushurl = {template.as_posix()}/x.git\n')
    tracked = template / "repo" / "notes"
    tracked.mkdir()
    for name in ("HEAD", "config", "index"):
        (tracked / name).write_text(f"path = {template.as_posix()}/x\n", encoding="utf-8")
    copied = copy_repo(template, tmp_path / "copy")
    config_text = (copied / "repo" / ".git" / "config").read_text(encoding="utf-8")
    assert f"\turl = {url}\n" in config_text
    assert f"\tpushurl = {copied.as_posix()}/x.git\n" in config_text
    for name in ("HEAD", "config", "index"):
        assert (copied / "repo" / "notes" / name).read_bytes() == (tracked / name).read_bytes()


def test_a_template_path_under_another_spelling_is_refused(tmp_path: Path) -> None:
    template = tmp_path / "template"
    git_dir = _bare_git_dir(template)
    make_dir_link(tmp_path / "alias", template)
    (template / "repo" / ".git" / "worktrees" / "w").mkdir(parents=True)
    (git_dir / "worktrees" / "w" / "gitdir").write_text(
        f"{(tmp_path / 'alias' / 'w' / '.git').as_posix()}\n", encoding="utf-8"
    )
    with pytest.raises(RepoTemplateError, match="still names the template"):
        copy_repo(template, tmp_path / "copy")


def test_a_directory_link_in_the_template_never_leaves_a_copy_reading_the_template(
    tmp_path: Path,
) -> None:
    # A symlink is refused; a Windows junction is copied as the directory it
    # names. Either way no copy holds a link that resolves into the template.
    template = tmp_path / "template"
    (template / "real").mkdir(parents=True)
    (template / "real" / "file.txt").write_text("x\n", encoding="utf-8")
    make_dir_link(template / "linked", template / "real")
    copied = tmp_path / "copy"
    try:
        copy_repo(template, copied)
    except RepoTemplateError as refused:
        assert "is a link into the template" in str(refused)
        return
    inside = (copied / "linked" / "file.txt").resolve()
    assert inside.read_text(encoding="utf-8") == "x\n"
    assert template.resolve() not in inside.parents


def test_a_hidden_git_file_is_rewritten_in_place(
    world: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Git for Windows hides a worktree's ``.git`` file and Windows refuses to
    # open a hidden file in a creating mode. Here every existing file of the
    # copy refuses that, so each rewrite has to open the file for update.
    dest = tmp_path / "copy"
    real_open = builtins.open

    def no_creating_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if set(mode) & set("wxa") and dest in Path(os.fspath(file)).parents:
            if os.path.lexists(file) and Path(file).name in (".git", "gitdir", "config", "index"):
                raise PermissionError(13, "Permission denied", os.fspath(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", no_creating_open)
    monkeypatch.setattr(io, "open", no_creating_open)
    copy_repo(world.template, dest)
    monkeypatch.undo()
    assert f"gitdir: {(dest / 'work').as_posix()}/.git/worktrees/feat" in (
        dest / "feat" / ".git"
    ).read_text(encoding="utf-8")
    for name, text in _pointers(dest).items():
        assert world.template.name not in text, name
    for worktree in WORKTREES:
        assert _index(dest, worktree).read_bytes() != _index(world.template, worktree).read_bytes()


@requires_symlinks
def test_a_link_into_the_template_is_refused(tmp_path: Path) -> None:
    template = tmp_path / "template"
    template.mkdir()
    (template / "real.txt").write_text("x\n", encoding="utf-8")
    (template / "link.txt").symlink_to(template / "real.txt")
    (template / "near.txt").symlink_to("real.txt")  # relative: stays inside the copy
    with pytest.raises(RepoTemplateError, match="is a link into the template"):
        copy_repo(template, tmp_path / "copy")
    (template / "link.txt").unlink()
    copied = copy_repo(template, tmp_path / "again")
    assert os.readlink(copied / "near.txt") == "real.txt"


@pytest.mark.parametrize(
    ("header", "why"),
    [
        (struct.pack(">4sII", b"DIRC", 4, 0), "index version 4 is not handled"),
        (struct.pack(">4sII", b"DIRC", 2, 0) + b"\xff" * 32, "not a version 2 SHA-1 index"),
    ],
    ids=["version-4", "sha-256-trailer"],
)
def test_an_index_in_a_form_not_handled_is_refused(tmp_path: Path, header: bytes, why: str) -> None:
    template = tmp_path / "template"
    git_dir = _bare_git_dir(template)
    (git_dir / "index").write_bytes(header + b"\0" * 20)
    with pytest.raises(RepoTemplateError, match=why):
        copy_repo(template, tmp_path / "copy")


def test_a_copy_into_a_directory_never_replaces_what_is_there(tmp_path: Path) -> None:
    template, dest = tmp_path / "template", tmp_path / "dest"
    for name in ("repo", "other"):
        (template / name).mkdir(parents=True)
        (template / name / "file.txt").write_text(name, encoding="utf-8")
    (template / "loose.txt").write_text("loose", encoding="utf-8")
    (dest / "mine").mkdir(parents=True)
    assert copy_repo(template, dest) == dest
    assert sorted(p.name for p in dest.iterdir()) == ["loose.txt", "mine", "other", "repo"]
    with pytest.raises(FileExistsError):
        copy_repo(template, dest)


@pytest.fixture(scope="module")
def config_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A config file git wrote ``CONFIG_VALUES`` into, as ``v.k0``, ``v.k1``, ..."""
    path = tmp_path_factory.mktemp("config-proof") / "config"
    for number, value in enumerate(CONFIG_VALUES):
        _git(str(path.parent), "config", "--file", str(path), f"v.k{number}", value)
    return path


@pytest.mark.parametrize("number", range(len(CONFIG_VALUES)))
def test_a_config_value_is_read_and_written_as_git_does(config_file: Path, number: int) -> None:
    value = CONFIG_VALUES[number]
    written = {
        key.strip(): text
        for key, _, text in (
            line.partition(" = ") for line in config_file.read_text(encoding="utf-8").splitlines()
        )
    }[f"k{number}"]
    assert templates._config_text(value) == written
    assert templates._config_value(written) == value


# -- templates built on first use --------------------------------------------


class _Builder:
    """Counts its runs and writes one file; optionally fails."""

    def __init__(self, fail: bool = False, name: str = "_Builder") -> None:
        self.runs = 0
        self.fail = fail
        self.__module__, self.__qualname__, self.__name__ = __name__, name, "_builder"

    def __call__(self, root: Path) -> None:
        self.runs += 1
        (root / "built.txt").write_text(f"run {self.runs}", encoding="utf-8")
        if self.fail:
            raise OSError("the builder's own failure")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RepoTemplates:
    root = tmp_path / "store"
    root.mkdir()
    config = tmp_path / "global.gitconfig"
    config.write_text("[user]\n\tname = One\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    return RepoTemplates(root)


def test_a_template_is_built_once_and_every_copy_is_its_own(
    store: RepoTemplates, tmp_path: Path
) -> None:
    build = _Builder()
    one = store.copy(build, tmp_path / "one")
    (one / "built.txt").write_text("changed in one", encoding="utf-8")
    two = store.copy(build, tmp_path / "two")
    assert build.runs == 1
    assert (two / "built.txt").read_text(encoding="utf-8") == "run 1"
    (kept,) = (tmp_path / "store").iterdir()
    assert (kept / "built.txt").read_text(encoding="utf-8") == "run 1"


def test_a_different_key_gets_its_own_template(store: RepoTemplates, tmp_path: Path) -> None:
    build = _Builder()
    store.copy(build, tmp_path / "a", key="tree-a")
    store.copy(build, tmp_path / "b", key="tree-b")
    store.copy(build, tmp_path / "a-again", key="tree-a")
    assert build.runs == 2


@pytest.mark.parametrize(
    "change",
    ["author", "committer-email", "config-count", "global-content", "global-path", "no-global"],
)
def test_a_template_is_not_reused_under_another_identity_or_config(
    store: RepoTemplates, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    build = _Builder()
    store.copy(build, tmp_path / "before")
    if change == "author":
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Somebody Else")
    elif change == "committer-email":
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "else@example.invalid")
    elif change == "config-count":
        count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{count}", "commit.gpgsign")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{count}", "true")
        monkeypatch.setenv("GIT_CONFIG_COUNT", str(count + 1))
    elif change == "global-content":
        Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text("[user]\n\tname = Two\n", encoding="utf-8")
    elif change == "global-path":
        other = tmp_path / "other.gitconfig"
        other.write_text("[user]\n\tname = Two\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(other))
    else:
        monkeypatch.delenv("GIT_CONFIG_GLOBAL")
        monkeypatch.setenv("HOME", str(tmp_path / "another-home"))
    store.copy(build, tmp_path / "after")
    assert build.runs == 2
    store.copy(build, tmp_path / "after-again")
    assert build.runs == 2


def test_a_builder_that_fails_fails_the_test_and_leaves_no_template(
    store: RepoTemplates, tmp_path: Path
) -> None:
    build = _Builder(fail=True)
    for attempt in (1, 2):
        with pytest.raises(OSError, match="the builder's own failure"):
            store.copy(build, tmp_path / f"copy-{attempt}")
        assert build.runs == attempt, "a failed build was kept and reused"
        assert not (tmp_path / f"copy-{attempt}").exists()
        assert list((tmp_path / "store").iterdir()) == []
    build.fail = False
    assert (store.copy(build, tmp_path / "copy-3") / "built.txt").read_text("utf-8") == "run 3"


def test_modules_of_one_session_share_a_session_template(
    tmp_path_factory: pytest.TempPathFactory, repo_templates: RepoTemplates, tmp_path: Path
) -> None:
    build = _Builder(name=str(tmp_path))  # its own template on a repeated run
    first = templates.session_template(tmp_path_factory, build)
    assert templates.session_template(tmp_path_factory, build) == first
    assert build.runs == 1
    assert templates._store(tmp_path_factory) is repo_templates
