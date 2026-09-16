"""Recorded project directories widen the browsing allow-list, safely.

``recent_projects.json`` now decides authorization, so these tests pin both
halves: a recorded project becomes browsable, and nothing about the file's
content, shape, size or identity can widen the allow-list further. The
dashboard rewrites it with ``os.replace`` while this server may be reading,
so replacement is covered too.

The Windows and zero-inode branches are monkeypatched rather than skipped, so
every shard runs the same assertions on any host.
"""

import errno
import json
import ntpath
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.file_explorer import server
from kiro_crew.dashboard import chat_handlers


@pytest.fixture(autouse=True)
def _reset_recent_roots_cache():
    """Keep one test's fixture file out of the next test's cache."""
    server._RECENT_ROOTS_CACHE = None
    yield
    server._RECENT_ROOTS_CACHE = None


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """An ephemeral config dir so tests never touch the real user config."""
    cfg = tmp_path / "kiro-config"
    cfg.mkdir()
    monkeypatch.setattr(server, "config_dir", lambda: cfg)
    return cfg


@pytest.fixture
def project(tmp_path):
    """A directory standing in for a project outside the static roots."""
    p = tmp_path / "outside" / "proj"
    p.mkdir(parents=True)
    return p


@pytest.fixture
def pinned_static_roots(tmp_path, monkeypatch):
    """Pin ALLOWED_ROOTS: the real list contains the temp dir, and pytest's
    ``tmp_path`` can live inside it -- so an "outside" path would be admitted."""
    static = tmp_path / "static-root"
    static.mkdir()
    monkeypatch.setattr(server, "ALLOWED_ROOTS", [static.resolve()])
    return static


def _write_recent(cfg: Path, payload) -> Path:
    fp = cfg / "recent_projects.json"
    body = payload if isinstance(payload, str) else json.dumps(payload)
    fp.write_text(body, encoding="utf-8")
    return fp


def _never_resolve(*_args, **_kwargs):
    """A resolve() seam that proves a path never reached a filesystem probe."""
    raise AssertionError("the path reached resolve() instead of being refused")


def _counting_open(calls: list):
    """Wrap the fd opener so a test can prove the cache skipped a reopen."""
    real = server._open_recent_projects_fd

    def _spy(fp):
        calls.append(str(fp))
        return real(fp)

    return _spy


# ---------------------------------------------------------------------------
# The snapshot loader
# ---------------------------------------------------------------------------


class TestTheLoaderReturnsOneTrustworthySnapshot:
    def test_string_entries_survive_and_junk_does_not(self, config_dir):
        fp = _write_recent(config_dir, ["/a", 42, None, "", "/b", {"p": "/c"}])

        result = server._load_recent_projects_entries(fp)
        assert result is not None
        stamp, entries = result

        assert entries == ["/a", "/b"], entries
        assert stamp is not None

    def test_a_missing_file_is_not_a_snapshot(self, config_dir):
        assert server._load_recent_projects_entries(config_dir / "recent_projects.json") is None

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not json at all", id="malformed"),
            pytest.param("", id="empty"),
            pytest.param('{"dirs": ["/a"]}', id="object-not-list"),
            pytest.param("null", id="null"),
            pytest.param("[" * 5000 + "]" * 5000, id="deeply-nested"),
        ],
    )
    def test_unusable_content_yields_a_stamp_and_no_entries(self, config_dir, payload):
        """A stamp with no entries is what lets a stable bad file be cached."""
        fp = _write_recent(config_dir, payload)

        result = server._load_recent_projects_entries(fp)
        assert result is not None
        stamp, entries = result

        assert entries == []
        assert stamp is not None

    def test_undecodable_bytes_yield_no_entries(self, config_dir):
        fp = config_dir / "recent_projects.json"
        fp.write_bytes(b"\xff\xfe not utf-8")

        result = server._load_recent_projects_entries(fp)
        assert result is not None
        stamp, entries = result

        assert entries == []
        assert stamp is not None

    def test_a_directory_at_the_name_is_not_read_as_a_file(self, config_dir):
        d = config_dir / "recent_projects.json"
        d.mkdir()

        result = server._load_recent_projects_entries(d)

        # A directory is refused either at the open or by the S_ISREG check;
        # what matters is that no entries come back.
        assert result is None or result[1] == []

    def test_an_oversized_file_is_bounded_rather_than_slurped(self, config_dir, monkeypatch):
        """A growth after fstat must still be bounded by the read itself."""
        cap = 512
        monkeypatch.setattr(server, "MAX_RECENT_PROJECTS_BYTES", cap)
        fp = _write_recent(config_dir, ["/a" * 400, "/b" * 400])
        actual = fp.stat()
        reads = []
        real_fstat = os.fstat
        real_fdopen = os.fdopen

        def _small_fstat(fd):
            st = real_fstat(fd)
            return SimpleNamespace(
                st_mode=st.st_mode,
                st_size=0,
                st_dev=st.st_dev,
                st_ino=st.st_ino,
                st_mtime_ns=st.st_mtime_ns,
                st_ctime_ns=st.st_ctime_ns,
            )

        def _bounded_fdopen(fd, *args, **kwargs):
            fh = real_fdopen(fd, *args, **kwargs)

            class _Reader:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    fh.close()

                def read(self, size):
                    reads.append(size)
                    return fh.read(size)

            return _Reader()

        monkeypatch.setattr(server.os, "fstat", _small_fstat)
        monkeypatch.setattr(server.os, "fdopen", _bounded_fdopen)

        result = server._load_recent_projects_entries(fp)
        assert result is not None
        stamp, entries = result

        assert actual.st_size > cap
        assert entries == []
        assert stamp is not None
        assert reads == [cap + 1]

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="O_NOFOLLOW is POSIX-only")
    def test_a_symlink_at_the_name_is_refused_without_being_followed(self, config_dir, tmp_path):
        """The control file decides authorization, so it must not be a link."""
        real = tmp_path / "elsewhere.json"
        real.write_text(json.dumps(["/tmp"]), encoding="utf-8")
        link = config_dir / "recent_projects.json"
        link.symlink_to(real)

        assert server._load_recent_projects_entries(link) is None
        # Guard the guard: the link really does resolve to readable bytes, so
        # the refusal above is the no-follow open and not an unreadable target.
        assert json.loads(link.read_text(encoding="utf-8")) == ["/tmp"]

    def test_a_replacement_mid_read_is_discarded_rather_than_trusted(self, config_dir, monkeypatch):
        """Bytes from the old inode must not be cached under the new pathname's
        identity, so a load spanning a replacement is dropped and retried."""
        fp = _write_recent(config_dir, ["/first"])
        replaced = {"done": False}
        real_lstat = Path.lstat

        def _replace_then_lstat(path):
            result = real_lstat(path)
            if path == fp and not replaced["done"]:
                # Land the replacement AFTER the pre-read stamp is taken, so the
                # post-read stamp sees a different inode -- a real mid-read swap.
                replaced["done"] = True
                replacement = config_dir / "replacement.tmp"
                replacement.write_text(json.dumps(["/second"]), encoding="utf-8")
                os.replace(replacement, fp)
            return result

        monkeypatch.setattr(Path, "lstat", _replace_then_lstat)

        assert server._load_recent_projects_entries(fp) is None
        assert replaced["done"], "the racing writer never ran; nothing was proven"

    def test_path_stat_without_file_index_serves_but_does_not_cache(self, config_dir, monkeypatch):
        """Windows lstat may lack an inode even when fstat has a file index."""
        fp = _write_recent(config_dir, ["/project"])
        real_stamp = server._file_stamp
        calls = []

        def _asymmetric_stamp(st):
            calls.append(st)
            return real_stamp(st) if len(calls) == 1 else None

        monkeypatch.setattr(server, "_file_stamp", _asymmetric_stamp)

        result = server._load_recent_projects_entries(fp)

        assert result == (None, ["/project"])
        assert len(calls) == 2

    def test_a_transient_open_failure_is_not_a_snapshot(self, config_dir, monkeypatch):
        fp = _write_recent(config_dir, ["/a"])

        def _deny(_fp):
            raise PermissionError(errno.EACCES, "simulated")

        monkeypatch.setattr(server, "_open_recent_projects_fd", _deny)

        assert server._load_recent_projects_entries(fp) is None


class TestTheOpenerDoesNotBlockTheWriterOrThisWorker:
    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="POSIX flag branch")
    def test_posix_opens_no_follow_and_non_blocking(self, config_dir, monkeypatch):
        """``O_NONBLOCK`` is load-bearing: a FIFO at this name would otherwise
        park the worker until someone opened the write end."""
        fp = _write_recent(config_dir, ["/a"])
        seen: dict = {}
        real_open = os.open

        def _record(path, flags, *a, **kw):
            seen["flags"] = flags
            return real_open(path, flags, *a, **kw)

        monkeypatch.setattr(server.os, "open", _record)
        os.close(server._open_recent_projects_fd(fp))

        assert seen["flags"] & os.O_NOFOLLOW, seen
        assert seen["flags"] & os.O_NONBLOCK, seen

    def test_windows_uses_the_share_delete_reader(self, config_dir, monkeypatch):
        """Patched, not skipped, so the Windows branch is pinned on every host.

        That reader's share mode includes ``FILE_SHARE_DELETE``, which is what
        keeps the dashboard's ``os.replace`` working while this file is open.
        """
        fp = _write_recent(config_dir, ["/a"])
        used: list[str] = []

        def _fake_tail_open(path):
            used.append(str(path))
            return os.open(path, os.O_RDONLY)

        monkeypatch.setattr(server.platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(server.platform_compat, "open_log_file_for_tail", _fake_tail_open)
        os.close(server._open_recent_projects_fd(fp))

        assert used == [str(fp)], used


# ---------------------------------------------------------------------------
# Per-entry validation
# ---------------------------------------------------------------------------


class TestEachRecordedEntryIsValidatedOnItsOwn:
    def test_a_real_directory_is_accepted_and_canonical(self, project):
        assert server._resolve_project_root(str(project)) == project.resolve()

    def test_a_missing_path_is_not_a_root(self, tmp_path):
        assert server._resolve_project_root(str(tmp_path / "gone")) is None

    def test_a_regular_file_is_not_a_root(self, tmp_path):
        f = tmp_path / "notadir"
        f.write_text("x", encoding="utf-8")
        assert server._resolve_project_root(str(f)) is None

    def test_a_sensitive_path_is_not_a_root(self, tmp_path, monkeypatch):
        """The module's own ``_is_sensitive`` is the gate, not a narrower check:
        it also covers case-folded names and the crew data home."""
        d = tmp_path / "creds"
        d.mkdir()
        monkeypatch.setattr(server, "_is_sensitive", lambda p: p == d.resolve())
        assert server._resolve_project_root(str(d)) is None

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_a_symlink_is_judged_by_its_target_not_its_name(self, tmp_path, monkeypatch):
        """Canonicalise first, then apply policy -- otherwise an innocent-looking
        name pointing at a credential directory would pass."""
        secret = tmp_path / "dot-aws"
        secret.mkdir()
        link = tmp_path / "looks-fine"
        link.symlink_to(secret)
        monkeypatch.setattr(server, "_is_sensitive", lambda p: p == secret.resolve())

        assert server._resolve_project_root(str(link)) is None

    def test_a_tilde_entry_expands(self, monkeypatch, project):
        monkeypatch.setattr(
            server.os.path, "expanduser", lambda entry: str(project) if entry == "~/proj" else entry
        )
        monkeypatch.setattr(server, "_is_sensitive", lambda _path: False)
        assert server._resolve_project_root("~/proj") == project.resolve()

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param(r"\\server\share\project", id="backslash-unc"),
            pytest.param("//server/share/project", id="slash-unc"),
            pytest.param(r"\\?\UNC\server\share\project", id="extended-unc"),
        ],
    )
    def test_windows_unc_entry_is_rejected_before_resolve(self, entry, monkeypatch):
        monkeypatch.setattr(server.platform_compat, "IS_WINDOWS", True)

        def _unexpected_probe(*_args, **_kwargs):
            raise AssertionError("UNC path reached a filesystem probe")

        monkeypatch.setattr(Path, "resolve", _unexpected_probe)

        assert server._resolve_project_root(entry) is None

    def test_windows_tilde_expanding_to_unc_is_rejected_before_resolve(self, monkeypatch):
        monkeypatch.setattr(server.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            server.os.path, "expanduser", lambda _entry: r"\\server\share\user\project"
        )

        def _unexpected_probe(*_args, **_kwargs):
            raise AssertionError("expanded UNC path reached a filesystem probe")

        monkeypatch.setattr(Path, "resolve", _unexpected_probe)

        assert server._resolve_project_root("~/project") is None

    def test_a_probe_that_raises_drops_only_that_entry(self, project, monkeypatch):
        def _boom(_self):
            raise OSError(errno.EIO, "simulated I/O error")

        monkeypatch.setattr(Path, "is_dir", _boom)
        assert server._resolve_project_root(str(project)) is None

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param("/tmp/\x00/project", id="embedded-null"),
            pytest.param("\x00", id="bare-null"),
        ],
    )
    def test_an_entry_with_a_null_byte_drops_itself(self, entry):
        """``Path.resolve`` raises ValueError, not OSError, on an embedded null.
        Uncaught it would escape every gate rather than dropping one entry."""
        assert server._resolve_project_root(entry) is None

    def test_a_null_byte_entry_does_not_break_the_other_roots(self, config_dir, project):
        """One poisoned entry must not take the whole allow-list with it."""
        _write_recent(config_dir, ["/tmp/\x00/bad", str(project)])

        assert server._recent_project_roots() == [project.resolve()]
        # The gate itself must still answer rather than raising.
        assert server._is_in_allowed_roots(project.resolve()) is True


class TestALinkIsRecognisedWithoutFollowingIt:
    """Windows junctions are not symlinks, so ``is_symlink()`` misses them while
    ``stat()`` still follows them. Detection goes through the shared helper."""

    def test_a_junction_counts_even_though_it_is_not_a_symlink(self, tmp_path, monkeypatch):
        plain = tmp_path / "junction"
        plain.mkdir()
        monkeypatch.setattr(server.platform_compat, "is_link_or_junction", lambda _p: True)

        assert server._is_link_like(plain) is True

    def test_an_ordinary_directory_is_not_a_link(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()

        assert server._is_link_like(plain) is False

    def test_a_name_that_cannot_be_classified_counts_as_a_link(self, tmp_path, monkeypatch):
        """Fail closed: an unclassifiable name must not have its target probed."""

        def _boom(self):
            raise OSError(errno.EIO, "simulated I/O error")

        monkeypatch.setattr(Path, "lstat", _boom)

        assert server._is_link_like(tmp_path / "whatever") is True


class TestAUncTargetIsRefusedBeforeAnythingResolvesIt:
    """``resolve()`` OPENS what it resolves, so a link aimed at a share
    authenticates this process against that host. Every hop is judged by shape
    from ``readlink``, which never traverses."""

    @pytest.fixture
    def on_windows(self, monkeypatch):
        monkeypatch.setattr(server.platform_compat, "IS_WINDOWS", True)
        # Default to "no linked ancestor" so a leaf case is not perturbed by the
        # host's own layout (/var is a symlink on macOS, and tmp_path lives under
        # it). The ancestor cases override this.
        monkeypatch.setattr(server.platform_compat, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(Path, "resolve", _never_resolve)

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param(r"\\server\share\project", id="backslash"),
            pytest.param("//server/share/project", id="slash"),
            pytest.param(r"\\?\UNC\server\share\project", id="extended-unc"),
        ],
    )
    def test_a_link_to_a_share_is_refused(self, tmp_path, monkeypatch, on_windows, target):
        link = tmp_path / "docs"
        monkeypatch.setattr(
            server.platform_compat, "is_link_or_junction", lambda p: str(p) == str(link)
        )
        monkeypatch.setattr(server.os, "readlink", lambda _p: target)

        assert server._link_chain_reaches_unc(link) is True

    def test_a_local_link_chain_ending_at_a_share_is_refused(
        self, tmp_path, monkeypatch, on_windows
    ):
        """The first hop looks local; only the second reaches the share."""
        first = tmp_path / "docs"
        second = tmp_path / "hop"
        chain = {str(first): str(second), str(second): r"\\server\share"}
        monkeypatch.setattr(
            server.platform_compat, "is_link_or_junction", lambda p: str(p) in chain
        )
        monkeypatch.setattr(server.os, "readlink", lambda p: chain[str(p)])

        assert server._link_chain_reaches_unc(first) is True

    def test_an_extended_local_target_is_not_a_share(self, tmp_path, monkeypatch, on_windows):
        """Windows readlink returns local targets in extended form; refusing them
        would refuse every junctioned project."""
        link = tmp_path / "docs"
        monkeypatch.setattr(
            server.platform_compat, "is_link_or_junction", lambda p: str(p) == str(link)
        )
        monkeypatch.setattr(server.os, "readlink", lambda _p: r"\\?\C:\Users\someone\project")

        assert server._link_chain_reaches_unc(link) is False

    def test_an_unreadable_hop_is_refused(self, tmp_path, monkeypatch, on_windows):
        link = tmp_path / "docs"
        monkeypatch.setattr(
            server.platform_compat, "is_link_or_junction", lambda p: str(p) == str(link)
        )

        def _boom(_p):
            raise OSError(errno.EIO, "simulated I/O error")

        monkeypatch.setattr(server.os, "readlink", _boom)

        assert server._link_chain_reaches_unc(link) is True

    def test_a_linked_ancestor_aimed_at_a_share_is_refused(self, tmp_path, monkeypatch, on_windows):
        """The path itself is not UNC-shaped; only its ancestor's target is."""
        ancestor = tmp_path / "mount"
        nested = ancestor / "project"
        monkeypatch.setattr(
            server.platform_compat, "first_linked_ancestor", lambda _p: str(ancestor)
        )
        monkeypatch.setattr(server.os, "readlink", lambda _p: r"\\server\share")

        assert server._link_chain_reaches_unc(nested) is True

    def test_the_leaf_is_never_probed_before_its_ancestors(self, tmp_path, monkeypatch, on_windows):
        """``lstat`` on the leaf traverses every component above it, so probing the
        leaf while an ancestor is unknown performs the very traversal this screen
        exists to prevent."""
        ancestor = tmp_path / "mount"
        leaf = ancestor / "project" / "sub"
        probed: list[str] = []

        def _record(path):
            probed.append(str(path))
            return False

        monkeypatch.setattr(server.platform_compat, "is_link_or_junction", _record)
        monkeypatch.setattr(
            server.platform_compat, "first_linked_ancestor", lambda _p: str(ancestor)
        )
        monkeypatch.setattr(server.os, "readlink", lambda _p: r"\\server\share")

        assert server._link_chain_reaches_unc(leaf) is True
        assert str(leaf) not in probed, probed

    def test_the_path_below_a_local_link_keeps_being_screened(
        self, tmp_path, monkeypatch, on_windows
    ):
        """Re-anchoring must carry the suffix onto the link's target; dropping it
        would leave everything below the link unscreened."""
        ancestor = tmp_path / "mount"
        leaf = ancestor / "project"
        rebased = ntpath.join(r"D:\real", "project")
        read: list[str] = []

        def _first_linked(path):
            return str(ancestor) if str(path) == str(leaf) else None

        def _readlink(path):
            read.append(str(path))
            return r"D:\real" if str(path) == str(ancestor) else r"\\server\share"

        monkeypatch.setattr(server.platform_compat, "first_linked_ancestor", _first_linked)
        monkeypatch.setattr(
            server.platform_compat, "is_link_or_junction", lambda p: str(p) == rebased
        )
        monkeypatch.setattr(server.os, "readlink", _readlink)

        assert server._link_chain_reaches_unc(leaf) is True
        assert rebased in read, read

    def test_posix_does_not_pay_for_the_screen(self, tmp_path, monkeypatch):
        """Off Windows, resolving through a symlink is harmless and _is_sensitive
        on the resolved path is the real guard."""
        monkeypatch.setattr(server.platform_compat, "IS_WINDOWS", False)

        def _unexpected(_p):
            raise AssertionError("readlink must not run off Windows")

        monkeypatch.setattr(server.os, "readlink", _unexpected)

        assert server._link_chain_reaches_unc(tmp_path) is False

    def test_a_recorded_entry_whose_link_reaches_a_share_is_refused(
        self, tmp_path, monkeypatch, on_windows
    ):
        monkeypatch.setattr(server, "_link_chain_reaches_unc", lambda _p: True)

        assert server._resolve_project_root(str(tmp_path)) is None


class TestARealJunctionBehavesAsTheScreenAssumes:
    """Real-Windows evidence for the premises the monkeypatched cases assert.

    Creating a junction needs no elevation (a directory SYMLINK does), so a CI
    runner can make a real one. These exercise the actual ``is_link_or_junction``,
    ``readlink`` and ``is_unc_shape`` behaviour instead of fakes. They cannot
    manufacture a real SMB share, so the refuse-a-share direction stays covered
    lexically above; what these pin is detection, readlink's real output shape,
    and that an ordinary local junction is NOT refused.
    """

    @staticmethod
    def _make_junction(link: Path, target: Path) -> bool:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and link.exists()

    @pytest.mark.skipif(
        not server.platform_compat.IS_WINDOWS, reason="junctions exist only on Windows"
    )
    def test_a_real_junction_is_detected_and_reads_as_a_local_target(self, tmp_path):
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "junction"
        if not self._make_junction(link, target):
            pytest.skip("mklink /J is unavailable on this runner")

        assert server._is_link_like(link) is True
        stored = os.readlink(link)
        assert stored, "readlink returned nothing for a real junction"
        # The load-bearing claim: readlink's real output for a LOCAL target is not
        # judged a share, so an ordinary junction is never refused.
        assert server.is_unc_shape(stored) is False, stored
        assert server._link_chain_reaches_unc(link) is False

    @pytest.mark.skipif(
        not server.platform_compat.IS_WINDOWS, reason="junctions exist only on Windows"
    )
    def test_a_real_junction_to_a_sensitive_target_is_hidden(self, config_dir, project):
        secret = project / ".ssh"
        secret.mkdir()
        (secret / "credentials").write_text("secret", encoding="utf-8")
        link = project / "docs"
        if not self._make_junction(link, secret):
            pytest.skip("mklink /J is unavailable on this runner")
        _write_recent(config_dir, [str(project)])

        entries, _ = server._list_dir(project.resolve(), depth=2)

        assert "docs" not in {entry["name"] for entry in entries}

    @pytest.mark.skipif(
        not server.platform_compat.IS_WINDOWS, reason="junctions exist only on Windows"
    )
    def test_a_real_local_junction_still_browses(self, config_dir, project):
        """The direction that would hurt users: a legitimate junction must survive
        the screen."""
        target = project / "real"
        target.mkdir()
        (target / "guide.md").write_text("guide", encoding="utf-8")
        link = project / "docs"
        if not self._make_junction(link, target):
            pytest.skip("mklink /J is unavailable on this runner")
        _write_recent(config_dir, [str(project)])

        entries, _ = server._list_dir(project.resolve(), depth=2)

        docs = next(entry for entry in entries if entry["name"] == "docs")
        assert docs["type"] == "dir"
        assert [child["name"] for child in docs.get("children", [])] == ["guide.md"]


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestTheCacheReloadsExactlyWhenTheFileChanges:
    def test_an_unchanged_file_is_not_reopened(self, config_dir, project, monkeypatch):
        _write_recent(config_dir, [str(project)])
        calls: list[str] = []
        monkeypatch.setattr(server, "_open_recent_projects_fd", _counting_open(calls))

        first = server._recent_project_roots()
        second = server._recent_project_roots()

        assert first == second == [project.resolve()]
        assert len(calls) == 1, calls

    def test_a_replacement_is_noticed_even_when_size_and_mtime_match(
        self, config_dir, tmp_path, monkeypatch
    ):
        """Identity, not timestamps: an atomic replace can land inside one
        timestamp tick and keep the same length."""
        a = tmp_path / "outside" / "alpha"
        b = tmp_path / "outside" / "bravo"
        for d in (a, b):
            d.mkdir(parents=True)
        fp = _write_recent(config_dir, [str(a)])
        assert server._recent_project_roots() == [a.resolve()]

        before = fp.stat()
        tmp = config_dir / "next.tmp"
        tmp.write_text(json.dumps([str(b)]), encoding="utf-8")
        os.replace(tmp, fp)
        os.utime(fp, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert server._recent_project_roots() == [b.resolve()]

    def test_a_transient_failure_is_not_remembered_as_emptiness(
        self, config_dir, project, monkeypatch
    ):
        """A chmod or mount blip must not pin an empty answer until the file's
        timestamp happens to change."""
        _write_recent(config_dir, [str(project)])
        failing = {"now": True}
        real_load = server._load_recent_projects_entries

        def _maybe_fail(fp):
            return None if failing["now"] else real_load(fp)

        monkeypatch.setattr(server, "_load_recent_projects_entries", _maybe_fail)

        assert server._recent_project_roots() == []
        assert server._RECENT_ROOTS_CACHE is None

        failing["now"] = False
        assert server._recent_project_roots() == [project.resolve()]

    def test_a_stable_broken_file_is_only_parsed_once(self, config_dir, monkeypatch):
        _write_recent(config_dir, "not json")
        calls: list[str] = []
        monkeypatch.setattr(server, "_open_recent_projects_fd", _counting_open(calls))

        assert server._recent_project_roots() == []
        assert server._recent_project_roots() == []
        assert len(calls) == 1, calls

    def test_a_volume_with_no_file_index_still_works_but_never_caches(
        self, config_dir, project, monkeypatch
    ):
        """``st_ino == 0`` is no identity (see ``project_scan.root_identity``):
        caching under it would make every later replacement read as unchanged.
        """
        _write_recent(config_dir, [str(project)])
        monkeypatch.setattr(server, "_file_stamp", lambda st: None)
        calls: list[str] = []
        monkeypatch.setattr(server, "_open_recent_projects_fd", _counting_open(calls))

        assert server._recent_project_roots() == [project.resolve()]
        assert server._recent_project_roots() == [project.resolve()]
        assert server._RECENT_ROOTS_CACHE is None
        assert len(calls) == 2, "a zero inode must not be cached"

    def test_a_missing_file_leaves_no_cache_so_a_first_pick_is_seen(self, config_dir, project):
        assert server._recent_project_roots() == []
        assert server._RECENT_ROOTS_CACHE is None

        _write_recent(config_dir, [str(project)])
        assert server._recent_project_roots() == [project.resolve()]

    def test_duplicate_and_equivalent_entries_collapse(self, config_dir, project):
        _write_recent(config_dir, [str(project), str(project), f"{project}{os.sep}.{os.sep}"])
        assert server._recent_project_roots() == [project.resolve()]

    def test_only_the_first_capped_entries_are_validated(self, config_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "MAX_RECENT_PROJECTS", 2)
        dirs = []
        for i in range(5):
            d = tmp_path / "outside" / f"p{i}"
            d.mkdir(parents=True)
            dirs.append(d)
        _write_recent(config_dir, [str(d) for d in dirs])

        assert server._recent_project_roots() == [dirs[0].resolve(), dirs[1].resolve()]

    def test_the_caller_cannot_mutate_the_cache(self, config_dir, project):
        _write_recent(config_dir, [str(project)])
        first = server._recent_project_roots()
        first.append(Path("/injected"))

        assert server._recent_project_roots() == [project.resolve()]


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestEveryAuthorizationPathHonoursTheSameRoots:
    """One recorded project, checked at every gate that guards a request.

    Separate tests per gate: the defect being prevented is exactly that one
    call site keeps reading the static list.
    """

    def test_the_static_roots_still_come_first(self, config_dir, project):
        _write_recent(config_dir, [str(project)])
        roots = server._effective_allowed_roots()

        assert roots[: len(server.ALLOWED_ROOTS)] == server.ALLOWED_ROOTS
        assert project.resolve() in roots

    def test_a_recorded_project_that_is_already_static_appears_once(self, config_dir):
        home = server.ALLOWED_ROOTS[0]
        _write_recent(config_dir, [str(home)])

        roots = server._effective_allowed_roots()

        assert roots.count(home) == 1
        assert roots == server._effective_allowed_roots()

    def test_safe_path_admits_the_project_and_still_refuses_a_stranger(
        self, config_dir, project, pinned_static_roots, tmp_path
    ):
        _write_recent(config_dir, [str(project)])
        (project / "file.txt").write_text("x", encoding="utf-8")
        stranger = tmp_path / "unrecorded"
        stranger.mkdir()

        assert server._safe_path(str(project / "file.txt")) == (project / "file.txt").resolve()
        with pytest.raises(server.PathError) as denied:
            server._safe_path(str(stranger))
        assert denied.value.status == 403

    def test_the_containment_barrier_admits_the_project_and_refuses_a_stranger(
        self, config_dir, project, pinned_static_roots, tmp_path
    ):
        _write_recent(config_dir, [str(project)])
        stranger = tmp_path / "unrecorded"
        stranger.mkdir()

        assert server._contain_in_allowed_roots(project, operation="tree_list") == project.resolve()
        with pytest.raises(server.PathError):
            server._contain_in_allowed_roots(stranger, operation="tree_list")

    def test_a_deep_listing_recurses_inside_the_project(self, config_dir, project):
        """The recursive-child check is its own call site, so a snapshot that
        only reached the top level would still hide every subdirectory."""
        (project / "sub").mkdir()
        (project / "sub" / "leaf.txt").write_text("x", encoding="utf-8")
        _write_recent(config_dir, [str(project)])

        entries, _ = server._list_dir(project.resolve(), depth=2)

        sub = next(e for e in entries if e["name"] == "sub")
        assert [c["name"] for c in sub.get("children", [])] == ["leaf.txt"], sub

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_listing_safe_symlink_preserves_visible_paths(self, config_dir, project):
        target = project / "real"
        target.mkdir()
        (target / "guide.md").write_text("guide", encoding="utf-8")
        link = project / "docs"
        link.symlink_to(target, target_is_directory=True)
        _write_recent(config_dir, [str(project)])

        entries, _ = server._list_dir(project.resolve(), depth=2)

        docs = next(entry for entry in entries if entry["name"] == "docs")
        assert docs["path"] == str(link)
        assert [child["path"] for child in docs["children"]] == [str(link / "guide.md")]

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_listing_hides_innocent_symlink_to_sensitive_target(self, config_dir, project):
        secret = project / ".ssh"
        secret.mkdir()
        (secret / "credentials").write_text("secret", encoding="utf-8")
        (project / "docs").symlink_to(secret, target_is_directory=True)
        _write_recent(config_dir, [str(project)])

        entries, _ = server._list_dir(project.resolve(), depth=2)

        assert "docs" not in {entry["name"] for entry in entries}

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_listing_hides_symlink_outside_effective_roots(
        self, config_dir, project, pinned_static_roots, tmp_path
    ):
        stranger = tmp_path / "unrecorded"
        stranger.mkdir()
        (stranger / "private.txt").write_text("private", encoding="utf-8")
        (project / "docs").symlink_to(stranger, target_is_directory=True)
        _write_recent(config_dir, [str(project)])

        entries, _ = server._list_dir(project.resolve(), depth=2)

        assert "docs" not in {entry["name"] for entry in entries}

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_listing_keeps_an_unresolvable_symlink_visible_as_missing(
        self, config_dir, project, monkeypatch
    ):
        """A child whose target cannot be validated degrades instead of vanishing."""
        link = project / "docs"
        link.symlink_to(project / "gone", target_is_directory=True)
        _write_recent(config_dir, [str(project)])
        root = project.resolve()
        real_resolve = Path.resolve

        def _selective_resolve(self, *args, **kwargs):
            if self.name == "docs":
                raise OSError(errno.EIO, "simulated I/O error")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _selective_resolve)

        entries, _ = server._list_dir(root, depth=2)

        docs = next(entry for entry in entries if entry["name"] == "docs")
        assert docs["type"] == "missing"
        assert docs["path"] == str(link)

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_listing_hides_a_sensitive_target_even_when_is_symlink_lies(
        self, config_dir, project, monkeypatch
    ):
        """A Windows junction reports ``is_symlink()`` False, so the target gate
        must not depend on it."""
        secret = project / ".ssh"
        secret.mkdir()
        (secret / "credentials").write_text("secret", encoding="utf-8")
        (project / "docs").symlink_to(secret, target_is_directory=True)
        _write_recent(config_dir, [str(project)])
        monkeypatch.setattr(Path, "is_symlink", lambda self: False)

        entries, _ = server._list_dir(project.resolve(), depth=2)

        assert "docs" not in {entry["name"] for entry in entries}

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_sorting_never_probes_a_child_the_target_gate_rejects(
        self, config_dir, project, monkeypatch
    ):
        """The sort key calls is_dir(), which follows a link; a junction aimed at
        an SMB share would authenticate there, so rejection must come first."""
        secret = project / ".ssh"
        secret.mkdir()
        (project / "docs").symlink_to(secret, target_is_directory=True)
        (project / "keep.txt").write_text("keep", encoding="utf-8")
        _write_recent(config_dir, [str(project)])
        probed: list[str] = []
        real_key = server._entry_sort_key

        def _recording(p):
            probed.append(p.name)
            return real_key(p)

        monkeypatch.setattr(server, "_entry_sort_key", _recording)

        server._list_dir(project.resolve(), depth=2)

        assert "docs" not in probed, probed
        assert "keep.txt" in probed, probed

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_listing_drops_a_child_whose_link_reaches_a_share(
        self, config_dir, project, monkeypatch
    ):
        """A junction planted in a browsed root and aimed at a share must be
        refused before resolve() can authenticate against that host."""
        target = project / "real"
        target.mkdir()
        (target / "guide.md").write_text("guide", encoding="utf-8")
        (project / "docs").symlink_to(target, target_is_directory=True)
        (project / "keep.txt").write_text("keep", encoding="utf-8")
        _write_recent(config_dir, [str(project)])
        monkeypatch.setattr(server, "_link_chain_reaches_unc", lambda p: Path(p).name == "docs")

        entries, _ = server._list_dir(project.resolve(), depth=2)

        names = {entry["name"] for entry in entries}
        assert "docs" not in names
        assert {"real", "keep.txt"} <= names

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_completion_hides_a_link_to_a_sensitive_target(self, config_dir, project, monkeypatch):
        """The widened roots feed this endpoint too, so it needs the same screen
        the tree listing got."""
        secret = project / ".ssh"
        secret.mkdir()
        (secret / "credentials").write_text("secret", encoding="utf-8")
        (project / "docs").symlink_to(secret, target_is_directory=True)
        (project / "keep").mkdir()
        _write_recent(config_dir, [str(project)])
        handler = server.FileExplorerHandler.__new__(server.FileExplorerHandler)
        responses: list = []
        monkeypatch.setattr(
            handler, "_json", lambda code, payload: responses.append((code, payload))
        )

        handler._h_complete({"path": [f"{project}/"], "kind": ["all"]})

        names = {entry["name"] for entry in responses[0][1]["entries"]}
        assert "docs" not in names
        assert "keep" in names

    @pytest.mark.skipif(not server.platform_compat.IS_POSIX, reason="needs POSIX symlinks")
    def test_completion_never_sorts_a_child_the_screen_rejects(
        self, config_dir, project, monkeypatch
    ):
        """Its sort key probes is_dir(), which follows a junction."""
        secret = project / ".ssh"
        secret.mkdir()
        (project / "docs").symlink_to(secret, target_is_directory=True)
        (project / "keep").mkdir()
        _write_recent(config_dir, [str(project)])
        probed: list[str] = []
        real_key = server._entry_sort_key

        def _recording(p):
            probed.append(p.name)
            return real_key(p)

        monkeypatch.setattr(server, "_entry_sort_key", _recording)
        handler = server.FileExplorerHandler.__new__(server.FileExplorerHandler)
        monkeypatch.setattr(handler, "_json", lambda code, payload: None)

        handler._h_complete({"path": [f"{project}/"], "kind": ["all"]})

        assert "docs" not in probed, probed
        assert "keep" in probed, probed

    def test_a_deep_listing_does_not_read_the_control_file_per_child(self, config_dir, project):
        """A wide tree must cost no more reads than a narrow one. The absolute
        count is incidental; what matters is that it does not grow with width."""
        _write_recent(config_dir, [str(project)])

        def _reads_for(width: int) -> int:
            root = project / f"w{width}"
            root.mkdir()
            for i in range(width):
                (root / f"d{i}").mkdir()
                (root / f"d{i}" / "leaf.txt").write_text("x", encoding="utf-8")
            server._RECENT_ROOTS_CACHE = None
            server._recent_project_roots()  # warm, so this measures the walk

            calls: list[str] = []
            real = server._recent_projects_stamp

            def _spy(path):
                calls.append(str(path))
                return real(path)

            try:
                server._recent_projects_stamp = _spy
                server._list_dir(root.resolve(), depth=3)
            finally:
                server._recent_projects_stamp = real
            return len(calls)

        narrow = _reads_for(2)
        wide = _reads_for(20)

        assert narrow == wide, f"reads scaled with width: {narrow} -> {wide}"

    def test_git_status_admits_project_repo_and_refuses_stranger(
        self, config_dir, project, pinned_static_roots, tmp_path, monkeypatch
    ):
        _write_recent(config_dir, [str(project)])
        stranger = tmp_path / "unrecorded" / "repo"
        stranger.mkdir(parents=True)
        repo = {"root": project.resolve()}
        monkeypatch.setattr(server, "_git_repo_root", lambda p: repo["root"])
        monkeypatch.setattr(
            server, "_git_status", lambda root: {"repoRoot": str(root), "statuses": {}}
        )
        monkeypatch.setattr(server, "_sel_audit", lambda *a, **kw: None)
        handler = server.FileExplorerHandler.__new__(server.FileExplorerHandler)
        responses = []
        monkeypatch.setattr(
            handler, "_json", lambda code, payload: responses.append((code, payload))
        )

        handler._h_git_status({"path": [str(project)]})
        assert responses == [(200, {"repoRoot": str(project.resolve()), "statuses": {}})]

        repo["root"] = stranger.resolve()
        with pytest.raises(server.PathError) as denied:
            handler._h_git_status({"path": [str(project)]})
        assert denied.value.status == 403

    def test_a_sensitive_recorded_entry_never_becomes_a_root(
        self, config_dir, tmp_path, pinned_static_roots, monkeypatch
    ):
        """Belt and braces: rejected at ingest, and still refused at the gate."""
        secret = tmp_path / "outside" / "creds"
        secret.mkdir(parents=True)
        _write_recent(config_dir, [str(secret)])
        monkeypatch.setattr(server, "_is_sensitive", lambda p: p == secret.resolve())

        assert secret.resolve() not in server._effective_allowed_roots()
        with pytest.raises(server.PathError):
            server._safe_path(str(secret))


# ---------------------------------------------------------------------------
# Writer / reader contract
# ---------------------------------------------------------------------------


class TestTheDashboardWriterAndThisReaderStayInSync:
    """Every other test writes the control file itself, so a writer-side schema
    or admission change would fail closed and undetected -- recorded projects
    would silently 403 again. These round-trip the REAL writer's output."""

    def test_a_project_the_writer_records_becomes_a_root(self, config_dir, project, monkeypatch):
        monkeypatch.setattr(chat_handlers, "config_dir", lambda: config_dir)

        chat_handlers._save_recent_project(str(project))

        assert server._recent_project_roots() == [project.resolve()]
        assert server._is_in_allowed_roots(project.resolve() / "file.txt") is True

    def test_the_writers_newest_first_order_survives_the_reader(
        self, config_dir, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(chat_handlers, "config_dir", lambda: config_dir)
        first = tmp_path / "outside" / "first"
        second = tmp_path / "outside" / "second"
        for d in (first, second):
            d.mkdir(parents=True)

        chat_handlers._save_recent_project(str(first))
        chat_handlers._save_recent_project(str(second))

        assert server._recent_project_roots() == [second.resolve(), first.resolve()]

    def test_both_sides_cap_at_the_same_number(self):
        """Neither side may truncate the other's entries by surprise."""
        assert chat_handlers._MAX_RECENT_PROJECTS == server.MAX_RECENT_PROJECTS
