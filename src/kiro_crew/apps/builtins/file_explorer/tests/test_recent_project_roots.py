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
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.file_explorer import server


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
        real_stamp = server._recent_projects_stamp

        def _replace_then_stamp(path):
            if not replaced["done"]:
                replaced["done"] = True
                _write_recent(config_dir, ["/second"])
            return real_stamp(path)

        monkeypatch.setattr(server, "_recent_projects_stamp", _replace_then_stamp)

        assert server._load_recent_projects_entries(fp) is None
        assert replaced["done"], "the racing writer never ran; nothing was proven"

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
        monkeypatch.setenv("HOME", str(project.parent))
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
