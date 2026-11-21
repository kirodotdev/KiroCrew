"""Git repos built once and copied per test, with no git spawn per copy.

A git spawn costs about 0.26 s on the Windows CI runner (run 44897846076: across
13 test files, setup seconds per test follow the number of git spawns in the
fixtures at that rate, and two ``copytree`` calls per test do not show), so a
fixture that builds a repo for every test pays that on each spawn, and so does a
fixture that copies a prebuilt repo and then runs ``git remote set-url`` and
``git reset --hard`` on the copy.

:func:`copy_repo` is the one way a test gets a repo here. It copies a template
directory, which may hold several repos that name each other (a clone and its
bare remote, a checkout and its linked worktrees, a submodule), and then does
in this process what those two git commands did:

* every absolute path into the template that git recorded in the copy (a remote
  or submodule URL and any other config value, a worktree's ``.git`` file and
  its ``gitdir`` backlink, ``objects/info/alternates``) is re-pointed at the
  copy, so nothing a test does in its copy can reach the template or another
  test's copy;
* every index in the copy gets the copied files' stat data, entry by entry, and
  only for an entry whose file content hashes to the blob the index names. Git
  compares that stat data, not content, in ``diff-files`` and in every command
  built on it; a copied file has a new inode and change time (creation time on
  Windows), so without this a copied checkout reads as modified.

Text git only shows (a reflog message) and committed content (``.gitmodules``)
keep the path they were written with.

The first copy of each template is then checked with real git (see
:func:`_check_first_copy`), so a platform where this does not hold fails the
test instead of handing it a repo git reads differently.

A template is never handed to a test. A fixture either builds it in a
``scope="session"`` fixture of its own and calls :func:`copy_repo`, or asks the
``repo_templates`` fixture, which builds on first use under the environment of
the test that asks and keeps one template per git identity and config.

The session template builders here run before the per-test git floor applies,
so they use the scrubbed env from :func:`push_guard_helpers._fixture_git_env`:
no host config, templates or hooks reach them.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
from collections.abc import Callable, Hashable, Iterator
from pathlib import Path

import pytest

from kiro_crew.testing.wait import default_timeout

__all__ = [
    "RepoTemplateError",
    "RepoTemplates",
    "copy_repo",
    "repo_templates",
    "session_template",
]


class RepoTemplateError(AssertionError):
    """A template could not be built, or its copy is not a private, clean repo."""


# -- copy -------------------------------------------------------------------


def copy_repo(template: Path | str, dest: Path | str) -> Path:
    """A private copy of the directory *template* at *dest*; returns *dest*.

    *dest* may already exist (a test's ``tmp_path``): the template's entries are
    then copied into it, and an entry that is already there is an error.
    Symlinks are kept as links, file times are kept, and nothing is hard-linked,
    so no file is shared with the template. See the module docstring for what
    is re-pointed and re-read in the copy.
    """
    template, dest = Path(template), Path(dest)
    if dest.exists():
        for entry in sorted(template.iterdir()):
            target = dest / entry.name
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, target, symlinks=True)
            elif os.path.lexists(target):
                raise FileExistsError(str(target))
            else:
                shutil.copy2(entry, target, follow_symlinks=False)
    else:
        shutil.copytree(template, dest, symlinks=True)
    worktrees = _adopt(template, dest)
    if template not in _CHECKED:
        _check_first_copy(template, dest, worktrees)
        _CHECKED.add(template)
    return dest


#: Templates whose first copy real git has agreed with, in this process.
_CHECKED: set[Path] = set()


def _adopt(template: Path, dest: Path) -> list[Path]:
    """Re-point and re-read the copy at *dest*; the working trees whose index was read."""
    indexes: list[Path] = []
    for directory, entries in _walk(dest):
        names = {entry.name for entry in entries}
        git_dir = "HEAD" in names and bool(names & {"objects", "commondir"})
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                target = os.readlink(entry.path).removeprefix("\\\\?\\")
                if _under(target, template) is not None:
                    raise RepoTemplateError(f"{path} is a link into the template: {target}")
            elif not entry.is_file():
                continue
            elif entry.name == ".git":
                _rewrite_lines(path, template, dest, prefix="gitdir: ")
            elif entry.name == "gitdir" and directory.parent.name == "worktrees":
                _rewrite_lines(path, template, dest)
            elif entry.name == "alternates" and directory.parts[-2:] == ("objects", "info"):
                _rewrite_lines(path, template, dest, private=True)
            elif git_dir and entry.name in ("config", "config.worktree"):
                _rewrite_config(path, template, dest)
            elif git_dir and entry.name == "index":
                indexes.append(path)
    worktrees = []
    for index in indexes:
        worktree = _worktree_of(index.parent)
        _restat_index(index, worktree)
        worktrees.append(worktree)
    return worktrees


def _walk(top: Path) -> Iterator[tuple[Path, list[os.DirEntry[str]]]]:
    """Every directory under *top* with its entries; links are not followed."""
    stack = [top]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as scan:
            entries = list(scan)
        yield directory, entries
        stack.extend(Path(e.path) for e in entries if e.is_dir(follow_symlinks=False))


def _under(value: str, root: Path) -> str | None:
    """The part of the path *value* below *root* ("" for *root* itself), else ``None``.

    Compared the way the OS compares paths, so on Windows a drive letter's case
    and the two slashes do not matter.
    """
    norm, base = os.path.normcase(value), os.path.normcase(str(root))
    if norm == base:
        return ""
    if norm.startswith(base) and norm[len(base)] in "\\/":
        return value[len(base) :]
    return None


def _repoint(value: str, template: Path, dest: Path, where: Path) -> str:
    """*value* with a leading template path replaced by the copy's, in the same spelling."""
    tail = _under(value, template)
    if tail is not None:
        head = value[: len(value) - len(tail)]
        return (str(dest) if "\\" in head else dest.as_posix()) + tail
    if _names(value, template) or _resolves_into(value, template):
        raise RepoTemplateError(f"{where} still names the template: {value!r}")
    return value


def _resolves_into(value: str, template: Path) -> bool:
    """Whether the absolute path *value* is in the template under another spelling."""
    if not os.path.isabs(value):
        return False
    return _under(os.path.realpath(value), Path(os.path.realpath(template))) is not None


def _names(text: str, template: Path) -> bool:
    """Whether *text* holds the template's path anywhere, escaped for a config file or not."""
    path = os.path.normcase(str(template))
    return any(path in os.path.normcase(form) for form in (text, text.replace("\\\\", "\\")))


def _rewrite_lines(
    path: Path, template: Path, dest: Path, *, prefix: str = "", private: bool = False
) -> None:
    """Re-point each line of a pointer file (after *prefix*) that names the template.

    With *private*, an absolute path that is not in the template is an error:
    an ``alternates`` entry outside the copy is an object store the copy would
    share.
    """
    old = path.read_bytes().decode("utf-8", "surrogateescape")
    lines = []
    for line in old.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body.startswith(prefix) and body[len(prefix) :]:
            value = body[len(prefix) :]
            new = _repoint(value, template, dest, path)
            if private and new == value and os.path.isabs(value):
                raise RepoTemplateError(f"{path} shares an object store outside the copy: {value}")
            line = prefix + new + line[len(body) :]
        lines.append(line)
    if (text := "".join(lines)) != old:
        path.write_bytes(text.encode("utf-8", "surrogateescape"))


_CONFIG_PAIR = re.compile(r"^(\s*[A-Za-z][A-Za-z0-9-]*\s*=\s*)(.*)$")


def _config_value(raw: str) -> str | None:
    """The value git reads from the text after ``=``, or ``None`` if it runs on.

    Git's own rules (``config.c``, ``parse_value``): quotes group, a backslash
    escapes ``\\ " n t b``, and ``#`` or ``;`` outside quotes starts a comment.
    """
    out: list[str] = []
    quoted, trailing, chars = False, 0, iter(raw)
    for char in chars:
        if char == "\\":
            escaped = next(chars, None)
            if escaped is None or escaped not in '\\"ntb':
                return None
            out.append({"n": "\n", "t": "\t", "b": "\b"}.get(escaped, escaped))
            trailing = 0
        elif char == '"':
            quoted, trailing = not quoted, 0
        elif not quoted and char in "#;":
            break
        else:
            out.append(char)
            trailing = trailing + 1 if (char.isspace() and not quoted) else 0
    if quoted:
        return None
    return "".join(out[: len(out) - trailing] if trailing else out)


def _config_text(value: str) -> str:
    """*value* as git writes it after ``=`` (``config.c``, ``write_pair``)."""
    quote = '"' if (value[:1] == " " or value[-1:] == " " or re.search("[#;]", value)) else ""
    escapes = {"\n": "\\n", "\t": "\\t", '"': '\\"', "\\": "\\\\"}
    return quote + "".join(escapes.get(char, char) for char in value) + quote


def _rewrite_config(path: Path, template: Path, dest: Path) -> None:
    """Re-point every value in a git config file that is a path into the template."""
    old = path.read_bytes().decode("utf-8", "surrogateescape")
    lines = []
    for line in old.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        pair = _CONFIG_PAIR.match(body)
        value = _config_value(pair.group(2)) if pair else None
        if pair and value is not None:
            new = _repoint(value, template, dest, path)
            if new != value:
                line = pair.group(1) + _config_text(new) + line[len(body) :]
        elif _names(body, template):
            raise RepoTemplateError(f"{path} names the template in a line not understood: {body!r}")
        lines.append(line)
    if (text := "".join(lines)) != old:
        path.write_bytes(text.encode("utf-8", "surrogateescape"))


def _worktree_of(git_dir: Path) -> Path:
    """The working tree whose index lives in *git_dir*."""
    backlink = git_dir / "gitdir"
    if backlink.is_file():  # a linked worktree: the backlink names its ``.git`` file
        text = backlink.read_bytes().decode("utf-8", "surrogateescape").strip()
        return Path(os.path.normpath(git_dir / text)).parent
    in_core = False
    for line in (
        (git_dir / "config").read_text(encoding="utf-8", errors="surrogateescape").split("\n")
    ):
        if line.lstrip().startswith("["):
            in_core = line.strip().lower() == "[core]"
        elif in_core and (pair := _CONFIG_PAIR.match(line)) and "worktree" in pair.group(1).lower():
            value = _config_value(pair.group(2))
            if value:  # a submodule: relative to its git dir
                return Path(os.path.normpath(git_dir / value))
    if git_dir.name == ".git":
        return git_dir.parent
    raise RepoTemplateError(f"{git_dir / 'index'}: cannot tell which working tree it describes")


_ENTRY_HEAD = struct.Struct(">10I20sH")  # ten stat words, the blob id, the flags
_STAGE_MASK, _EXTENDED, _ASSUME_VALID, _NAME_MASK = 0x3000, 0x4000, 0x8000, 0x0FFF


def _stat_words(st: os.stat_result) -> tuple[int, ...]:
    """The eight stat words git stores for a file with this ``lstat``, mode aside.

    ``statinfo.c`` (``fill_stat_data``) on what git's own ``lstat`` returns: on
    Windows that is the creation time for ctime and zero for device, inode and
    owner (``compat/mingw.c``).
    """
    if sys.platform == "win32":
        changed = getattr(st, "st_birthtime_ns", st.st_ctime_ns)
        device = inode = uid = gid = 0
    else:
        changed, device, inode, uid, gid = (
            st.st_ctime_ns,
            st.st_dev,
            st.st_ino,
            st.st_uid,
            st.st_gid,
        )
    size = st.st_size & 0xFFFFFFFF
    if not size and st.st_size:
        size = 0x80000000
    words = (*divmod(changed, 10**9), *divmod(st.st_mtime_ns, 10**9), device, inode, uid, gid, size)
    return tuple(word & 0xFFFFFFFF for word in words)


def _file_blob(path: str, st: os.stat_result) -> bytes | None:
    """What git would hash for the file at *path* with no conversion, or ``None``."""
    if stat.S_ISREG(st.st_mode):
        with open(path, "rb") as handle:
            return handle.read()
    if stat.S_ISLNK(st.st_mode) and sys.platform != "win32":
        return os.fsencode(os.readlink(path))
    return None


def _restat_index(index: Path, worktree: Path) -> None:
    """Give each index entry whose file still holds the indexed blob that file's stat data.

    What ``git update-index --refresh`` does, for the entries it can do it for
    without converting content: a regular file or link at stage 0 whose bytes
    hash to the entry's blob id. Every other entry is left as it is, so git
    still compares its content. Index versions 2 and 3 with SHA-1 ids; anything
    else is an error.
    """
    data = bytearray(index.read_bytes())
    signature, version, count = struct.unpack_from(">4sII", data)
    if signature != b"DIRC" or version not in (2, 3):
        raise RepoTemplateError(f"{index}: index version {version} is not handled")
    try:
        _restat_entries(data, count, worktree)
    except (struct.error, ValueError) as exc:
        raise RepoTemplateError(f"{index}: not a version {version} SHA-1 index") from exc
    index.write_bytes(data)


def _restat_entries(data: bytearray, count: int, worktree: Path) -> None:
    offset = 12
    for _ in range(count):
        *words, blob, flags = _ENTRY_HEAD.unpack_from(data, offset)
        extended = 0
        if flags & _EXTENDED:
            extended = struct.unpack_from(">H", data, offset + _ENTRY_HEAD.size)[0]
        name_at = offset + _ENTRY_HEAD.size + (2 if flags & _EXTENDED else 0)
        length = flags & _NAME_MASK
        if length == _NAME_MASK:
            length = data.index(0, name_at) - name_at
        name = bytes(data[name_at : name_at + length])
        mode = words[6]
        plain = not (flags & (_STAGE_MASK | _ASSUME_VALID)) and not extended
        if plain and stat.S_IFMT(mode) in (stat.S_IFREG, stat.S_IFLNK):
            path = os.path.join(worktree, os.fsdecode(name))
            try:
                st = os.lstat(path)
                content = _file_blob(path, st)
            except OSError:
                content = None
            if content is not None and _blob_id(content) == blob:
                new = _stat_words(st)
                struct.pack_into(">6I", data, offset, *new[:6])
                struct.pack_into(">3I", data, offset + 28, *new[6:])
        offset += (name_at - offset + length + 8) & ~7  # NUL-padded to a multiple of eight
    end = len(data) - 20
    while offset < end:  # extensions: a four-letter name, a size, the body
        if not data[offset : offset + 4].isalpha():
            raise ValueError("no extension name")
        offset += 8 + struct.unpack_from(">I", data, offset + 4)[0]
    if offset != end:
        raise ValueError("the entries and extensions do not end at the checksum")
    if any(data[end:]):  # all zero under index.skipHash
        data[end:] = _git_sha1(data[:end])


def _blob_id(content: bytes) -> bytes:
    return _git_sha1(b"blob %d\0" % len(content) + content)


def _git_sha1(data: bytes | bytearray) -> bytes:
    """The SHA-1 git's formats fix: a blob's object id and the index's trailing checksum."""
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1 -- git's own object id and index checksum in a SHA-1 repository, compared with what git wrote; not a security digest
    return hashlib.sha1(data, usedforsecurity=False).digest()


def _check_first_copy(template: Path, dest: Path, worktrees: list[Path]) -> None:
    """Fail unless real git reads the copy's working trees as it reads the template's.

    ``git diff-files`` compares stat data without refreshing the index, so it
    lists an entry the re-stat missed. Run once per template and process.
    """
    from push_guard_helpers import _fixture_git_env

    env = _fixture_git_env()

    def changed(where: str) -> bytes:
        proc = subprocess.run(
            ["git", "diff-files", "--name-only", "-z"],
            cwd=where,
            env=env,
            capture_output=True,
            timeout=default_timeout(),
        )
        if proc.returncode:
            raise RepoTemplateError(f"git diff-files failed in {where}: {proc.stderr!r}")
        return proc.stdout

    for worktree in worktrees:
        tail = _under(str(worktree), dest)
        assert tail is not None, worktree
        in_copy = changed(str(worktree))
        # Nothing listed is the usual answer and needs no second opinion.
        if in_copy and in_copy != (in_template := changed(str(template) + tail)):
            raise RepoTemplateError(
                f"git reads the copy {worktree} as changed where the template is not: "
                f"{in_copy!r} against {in_template!r}"
            )


# -- templates built on first use --------------------------------------------


def _git_config_identity() -> tuple[tuple[str, str], ...]:
    """What decides the identity and config git would build a repo with right now.

    Every ``GIT_*`` variable, and the content of the files the config variables
    name. The floor points ``GIT_CONFIG_GLOBAL`` at one file for the whole
    session; where it is unset git reads the home directory's files instead, so
    the home is part of the identity and a test with its own home shares no
    template.
    """
    env = os.environ
    parts = [(key, env[key]) for key in sorted(env) if key.startswith("GIT_")]
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        path = env.get(key)
        if path is None:
            parts += [
                (name, env.get(name, "")) for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME")
            ]
        elif path and path != os.devnull:
            try:
                digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                digest = "unreadable"
            parts.append((key + " content", digest))
    return tuple(parts)


class RepoTemplates:
    """Templates built on first use, one per builder, key and git identity."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._built: dict[Hashable, Path] = {}
        self._attempts = 0

    def _template(self, build: Callable[[Path], object], key: Hashable) -> Path:
        identity = (build.__module__, build.__qualname__, key, _git_config_identity())
        template = self._built.get(identity)
        if template is None:
            self._attempts += 1
            template = self._root / f"{self._attempts}-{build.__name__.strip('_')}"
            template.mkdir()
            try:
                build(template)
            except BaseException:
                shutil.rmtree(template, ignore_errors=True)
                raise
            self._built[identity] = template
        return template

    def copy(
        self, build: Callable[[Path], object], dest: Path | str, *, key: Hashable = ()
    ) -> Path:
        """A private copy at *dest* of what ``build(directory)`` makes; returns *dest*.

        *build* fills an empty directory and runs once per *key* and git
        identity; *key* must hold everything else it reads. If it raises, the
        error reaches the test and nothing is kept, so the next test builds
        again. The template itself is never returned.
        """
        return copy_repo(self._template(build, key), dest)


#: One store per pytest base temp directory, so every test module of a worker
#: shares a template instead of building its own.
_STORES: dict[Path, RepoTemplates] = {}


def _store(tmp_path_factory: pytest.TempPathFactory) -> RepoTemplates:
    base = tmp_path_factory.getbasetemp()
    if base not in _STORES:
        _STORES[base] = RepoTemplates(tmp_path_factory.mktemp("repo-templates"))
    return _STORES[base]


@pytest.fixture(scope="session")
def repo_templates(tmp_path_factory: pytest.TempPathFactory) -> RepoTemplates:
    """The session's :class:`RepoTemplates`; import it by name into a test module."""
    return _store(tmp_path_factory)


def session_template(
    tmp_path_factory: pytest.TempPathFactory, build: Callable[[Path], object]
) -> Path:
    """The template directory ``build`` fills, for a ``scope="session"`` fixture.

    Shared by every module that binds that fixture. The fixture's consumers
    copy from it with :func:`copy_repo` and may read it; nothing writes to it.
    """
    return _store(tmp_path_factory)._template(build, ())


# -- the code-channel surface repo --------------------------------------------


def _surface_git(*args: str, cwd: Path) -> str:
    from push_guard_helpers import _fixture_git_env

    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_fixture_git_env(),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture(scope="session")
def _surface_root_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``proj`` and its channel worktree, as ``worktree_fixture`` builds them per test.

    ``proj`` has one commit on ``main`` holding ``README.md``, with user and
    gc.auto set; the worktree sits beside it on the channel branch. Seven git
    spawns per test there; here they run once.
    """
    from slack_code_channel_surface_helpers import BRANCH, _channel_tree

    root = tmp_path_factory.mktemp("surface-root") / "proj"
    root.mkdir()
    _surface_git("init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        _surface_git("config", key, value, cwd=root)
    (root / "README.md").write_text("hello\n")
    _surface_git("add", ".", cwd=root)
    _surface_git("commit", "-q", "-m", "init", cwd=root)
    _surface_git("worktree", "add", "-q", "-b", BRANCH, str(_channel_tree(root)), cwd=root)
    return root


@pytest.fixture(name="worktree")
def surface_worktree_fixture(tmp_path: Path, _surface_root_template: Path) -> tuple[Path, Path]:
    """``(root, tree)`` like ``worktree_fixture``: a copy of the root and its channel worktree.

    The two name each other by absolute path; ``copy_repo`` points the copies
    at each other.
    """
    from slack_code_channel_surface_helpers import _channel_tree

    copy_repo(_surface_root_template.parent, tmp_path)
    root = tmp_path / "proj"
    return root, _channel_tree(root)  # where a bind from this root cuts it
