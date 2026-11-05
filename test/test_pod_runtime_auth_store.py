"""The pod stages the agent runtime's OWN identity store, not just the SSO cache.

kiro-cli resolves its token from `.local/share/kiro-cli` (or `amazon-q`), not from
`.aws/sso/cache`, so under the pod's remapped ``HOME`` that store must exist too.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew import pinned_fs
from kiro_crew import sandbox as sb
from kiro_crew import security
from kiro_crew.identity_stores import fenced_home_dirs, store_mappings
from kiro_crew.pod.runtime import (
    _RUNTIME_AUTH_STORE_FILE_CAP,
    _is_sqlite_sidecar,
    _runtime_auth_store_mappings,
    _stage_runtime_auth_store,
)


def _kiro_mapping(host: Path, platform: str = "linux", environ: dict | None = None):
    # The kiro-cli mapping for *platform*, straight from the authoritative table.
    return next(
        m for m in store_mappings(platform, host, environ or {}) if m.product.value == "kiro-cli"
    )


_OS_HOME_PARTS = ("os-home",)


def _write_identity_db(path: Path, token: str) -> None:
    """A REAL SQLite identity store holding *token*.

    Staging snapshots through SQLite's backup API, which refuses a non-database, so
    a text fixture would hit the refusal path. A real database lets tests check rows.
    """
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS auth (token TEXT)")
        conn.execute("INSERT INTO auth (token) VALUES (?)", (token,))
        conn.commit()


def _read_identity_tokens(path: Path) -> list[str]:
    """Every token row in the identity store at *path*."""
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return [row[0] for row in conn.execute("SELECT token FROM auth")]


#: The staging path writes through PINNED no-follow descriptors, so its POSIX
#: behaviour is only observable where the platform provides them. Gated on the
#: CAPABILITY (the same predicate production consults), never on a bare platform
#: name, and the no-capability contract is asserted unconditionally below.
requires_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="staging requires O_DIRECTORY/O_NOFOLLOW + dir_fd; contract asserted separately",
)


def test_a_platform_without_pinned_walk_stages_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform-honest contract, asserted on EVERY platform.

    Windows has no ``O_DIRECTORY``/``O_NOFOLLOW``/``dir_fd``, and this staging moves
    sign-in material, so there is no by-name fallback to degrade to: it stages
    nothing and reports 0. Forced through the documented seam so the branch is
    covered on POSIX too, rather than only running where it happens to be true.
    """
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    host = tmp_path / "host-home"
    store = host / ".local" / "share" / "kiro-cli"
    store.mkdir(parents=True)
    _write_identity_db(store / "data.sqlite3", "token-bearing store")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: host))
    os_home = tmp_path / "pod" / "os-home"
    os_home.mkdir(parents=True)

    assert _stage_runtime_auth_store(os_home, _kiro_mapping(host)) == 0
    assert not (os_home / ".local").exists()


@pytest.fixture
def host_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture host home with a kiro-cli store shaped like the real one."""
    home = tmp_path / "host-home"
    store = home / ".local" / "share" / "kiro-cli"
    store.mkdir(parents=True)
    _write_identity_db(store / "data.sqlite3", "token-bearing store")
    # Sidecars, as a LIVE database keeps them. Staging must not carry these across:
    # the snapshot already contains every transaction they hold, and copying them
    # beside a separately-read main file is what produced a mismatched set.
    (store / "data.sqlite3-wal").write_bytes(b"\x00wal-frames")
    (store / "data.sqlite3-shm").write_bytes(b"\x00shared-memory")
    (store / "tui.js").write_text("non-credential asset")
    nested = store / "cache"
    nested.mkdir()
    (nested / "nested-token.json").write_text("nested")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    return home


@requires_pinned_walk
def test_the_runtime_auth_store_is_staged_into_the_pod_home(
    tmp_path: Path, host_home: Path
) -> None:
    """The regression: without this the child has no token to resolve."""
    os_home = tmp_path / "pod" / "os-home"
    os_home.mkdir(parents=True)

    staged = _stage_runtime_auth_store(os_home, _kiro_mapping(host_home))

    target = os_home / ".local" / "share" / "kiro-cli"
    assert staged == 3
    assert _read_identity_tokens(target / "data.sqlite3") == ["token-bearing store"]
    # Sidecars are NOT staged -- the snapshot subsumes them.
    assert not (target / "data.sqlite3-wal").exists()
    assert not (target / "data.sqlite3-shm").exists()
    # And no staging temp is left behind by a successful snapshot.
    assert not (target / ".data.sqlite3.staging").exists()
    # Nested levels are mirrored too: the store's internal layout is the runtime's
    # contract, and guessing a filename is what made the SSO staging a no-op.
    assert (target / "cache" / "nested-token.json").read_text() == "nested"


@requires_pinned_walk
def test_staged_files_and_directories_get_owner_only_modes(tmp_path: Path, host_home: Path) -> None:
    os_home = tmp_path / "pod" / "os-home"
    os_home.mkdir(parents=True)

    _stage_runtime_auth_store(os_home, _kiro_mapping(host_home))

    target = os_home / ".local" / "share" / "kiro-cli"
    assert oct(target.stat().st_mode)[-3:] == "700"
    assert oct((target / "data.sqlite3").stat().st_mode)[-3:] == "600"


@requires_pinned_walk
def test_an_absent_store_is_not_an_error(tmp_path: Path, host_home: Path) -> None:
    """Best-effort: a host without the store still boots a signed-out pod."""
    os_home = tmp_path / "pod" / "os-home"
    os_home.mkdir(parents=True)

    assert (
        _stage_runtime_auth_store(
            os_home,
            next(
                m for m in store_mappings("linux", host_home, {}) if m.product.value == "amazon-q"
            ),
        )
        == 0
    )


@requires_pinned_walk
def test_existing_pod_files_are_not_clobbered(tmp_path: Path, host_home: Path) -> None:
    """Create-only, so a pod that refreshed its own credential keeps it."""
    os_home = tmp_path / "pod" / "os-home"
    target = os_home / ".local" / "share" / "kiro-cli"
    target.mkdir(parents=True)
    (target / "data.sqlite3").write_text("pod's own refreshed token")

    _stage_runtime_auth_store(os_home, _kiro_mapping(host_home))

    assert (target / "data.sqlite3").read_text() == "pod's own refreshed token"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@requires_pinned_walk
def test_an_owner_executable_store_file_stays_runnable(tmp_path: Path, host_home: Path) -> None:
    """kiro-cli runs its engine straight from the staged store, so ``node`` must stay executable.

    The host ships ``node`` as 0o755. Staging it at 0o600 is what made every pod's
    kiro-cli exit at boot with "failed to spawn the v3 engine". Owner execute is
    kept, and group and other bits are still dropped on both kinds of file.
    """
    store = host_home / ".local" / "share" / "kiro-cli"
    (store / "node").write_bytes(b"\x7fELF engine")
    (store / "node").chmod(0o755)
    (store / "tui.js").chmod(0o644)
    os_home = tmp_path / "pod" / "os-home"
    os_home.mkdir(parents=True)

    _stage_runtime_auth_store(os_home, _kiro_mapping(host_home))

    target = os_home / ".local" / "share" / "kiro-cli"
    assert _mode(target / "node") == 0o700
    assert (target / "node").read_bytes() == b"\x7fELF engine"
    assert _mode(target / "tui.js") == 0o600


@requires_pinned_walk
def test_a_copy_an_earlier_boot_left_at_0600_gets_owner_exec_back(
    tmp_path: Path, host_home: Path
) -> None:
    """A restart reuses the pod's os-home, and staging is create-only.

    Without a repair, a ``node`` that an earlier boot staged at 0o600 stays
    unrunnable on every later boot of that pod. The repair adds owner execute
    only. It does not replace the pod's bytes.
    """
    store = host_home / ".local" / "share" / "kiro-cli"
    (store / "node").write_bytes(b"host engine")
    (store / "node").chmod(0o755)
    os_home = tmp_path / "pod" / "os-home"
    target = os_home / ".local" / "share" / "kiro-cli"
    target.mkdir(parents=True)
    (target / "node").write_bytes(b"engine staged by an earlier boot")
    (target / "node").chmod(0o600)

    _stage_runtime_auth_store(os_home, _kiro_mapping(host_home))

    assert _mode(target / "node") == 0o700
    assert (target / "node").read_bytes() == b"engine staged by an earlier boot"


@requires_pinned_walk
def test_the_exec_repair_changes_only_a_plain_0600_copy(tmp_path: Path, host_home: Path) -> None:
    """The repair never follows a link, never changes a hardlinked inode, and keeps other modes.

    It also needs a source that staging would copy: a host executable with a second
    link, or a FIFO, causes no repair of the pod's copy.
    """
    store = host_home / ".local" / "share" / "kiro-cli"
    for name in ("linked", "aliased", "own-mode", "plain-source", "linked-source"):
        (store / name).write_bytes(b"host executable")
        (store / name).chmod(0o755)
    os.link(store / "linked-source", tmp_path / "second-host-link")
    os.mkfifo(store / "fifo-source", 0o700)
    os_home = tmp_path / "pod" / "os-home"
    target = os_home / ".local" / "share" / "kiro-cli"
    target.mkdir(parents=True)
    outside = tmp_path / "outside-the-pod"
    outside.write_bytes(b"not the pod's file")
    outside.chmod(0o600)
    (target / "linked").symlink_to(outside)
    alias_twin = tmp_path / "alias-twin"
    alias_twin.write_bytes(b"one inode, two names")
    alias_twin.chmod(0o600)
    os.link(alias_twin, target / "aliased")
    (target / "own-mode").write_bytes(b"pod chose this mode")
    (target / "own-mode").chmod(0o400)
    (store / "plain-source").chmod(0o644)
    (target / "plain-source").write_bytes(b"not executable on the host")
    (target / "plain-source").chmod(0o600)
    for name in ("linked-source", "fifo-source"):
        (target / name).write_bytes(b"copy from an earlier boot")
        (target / name).chmod(0o600)

    _stage_runtime_auth_store(os_home, _kiro_mapping(host_home))

    assert _mode(outside) == 0o600
    assert (target / "linked").is_symlink()
    assert _mode(alias_twin) == 0o600
    assert _mode(target / "own-mode") == 0o400
    assert _mode(target / "plain-source") == 0o600
    assert _mode(target / "linked-source") == 0o600
    assert _mode(target / "fifo-source") == 0o600


def test_the_mapping_set_is_derived_from_the_store_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pinned to its source. The earlier revision named the two POSIX
    # .local/share paths literally, so a macOS host -- or one with a redirected
    # XDG_DATA_HOME -- staged NOTHING, and the viability probe then ACCEPTED the
    # signed-out pod that produced, because signed-out is a legitimate boot state.
    home = Path("/home/user")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    assert _runtime_auth_store_mappings() == store_mappings(sys.platform, home, os.environ)
    assert _RUNTIME_AUTH_STORE_FILE_CAP > 0


@requires_pinned_walk
@pytest.mark.parametrize(
    ("platform", "relative"),
    [
        pytest.param(
            "darwin",
            ("Library", "Application Support", "kiro-cli"),
            id="macos",
        ),
        pytest.param("linux", (".local", "share", "kiro-cli"), id="posix"),
    ],
)
def test_a_per_platform_host_store_is_staged(
    tmp_path: Path, platform: str, relative: tuple[str, ...]
) -> None:
    # Each platform's real layout stages from the table, not from a POSIX guess.
    # The mapping's platform is a parameter; whether staging can happen is a host
    # capability (``pinned_fs.supports_pinned_walk()``, False on Windows), gated here.
    # ``test_a_platform_without_pinned_walk_stages_nothing`` covers the other half.
    host = tmp_path / f"host-{platform}"
    (host / Path(*relative)).mkdir(parents=True)
    _write_identity_db(host / Path(*relative) / "data.sqlite3", "token")
    os_home = tmp_path / f"pod-{platform}" / "os-home"
    os_home.mkdir(parents=True)

    assert _stage_runtime_auth_store(os_home, _kiro_mapping(host, platform)) == 1
    assert _read_identity_tokens(os_home / Path(*relative) / "data.sqlite3") == ["token"]


@requires_pinned_walk
def test_an_xdg_redirected_source_is_followed(tmp_path: Path) -> None:
    # The table honours XDG_DATA_HOME on the SOURCE side; staging follows it.
    host = tmp_path / "host"
    redirected = tmp_path / "elsewhere" / "share"
    (redirected / "kiro-cli").mkdir(parents=True)
    _write_identity_db(redirected / "kiro-cli" / "data.sqlite3", "token")
    os_home = tmp_path / "pod" / "os-home"
    os_home.mkdir(parents=True)

    mapping = _kiro_mapping(host, "linux", {"XDG_DATA_HOME": str(redirected)})
    assert _stage_runtime_auth_store(os_home, mapping) == 1
    # Staged at the FIXED default layout, which is where the child will look.
    assert (os_home / ".local" / "share" / "kiro-cli" / "data.sqlite3").exists()


class TestALiveDatabaseIsSnapshottedNotFileCopied:
    """A live store is a SET of files that only agree at an instant, so it is snapshotted.

    A checkpoint between copying ``data.sqlite3`` and its ``-wal`` tears the pair; no
    per-file copy loop can close that window.
    """

    @staticmethod
    def _store(host: Path) -> Path:
        store = host / ".local" / "share" / "kiro-cli"
        store.mkdir(parents=True)
        return store

    @requires_pinned_walk
    def test_a_checkpoint_between_file_copies_cannot_tear_the_snapshot(
        self, tmp_path: Path
    ) -> None:
        """Rows left uncheckpointed in the WAL still reach the staged store.

        The main file alone lacks them, so a byte copy would stage no token; a
        snapshot reads through the engine.
        """
        host = tmp_path / "host-home"
        store = self._store(host)
        db = store / "data.sqlite3"
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE auth (token TEXT)")
            conn.commit()
            conn.execute("INSERT INTO auth (token) VALUES ('live-token')")
            conn.commit()
            # Premise of the tear, asserted rather than assumed: the row is
            # committed but lives in the WAL, so the main file's OWN bytes do not
            # carry it. A per-file copy that reads the main file at this instant --
            # which is what sorted order made it do -- stages a database with no
            # token in it, whatever it copies next.
            assert (store / "data.sqlite3-wal").exists()
            main_only = tmp_path / "main-file-only.sqlite3"
            main_only.write_bytes(db.read_bytes())
            with pytest.raises(sqlite3.Error):
                # Not merely tokenless -- in WAL mode the SCHEMA is in the WAL too,
                # so the main file alone does not even have the table. That is the
                # torn snapshot a per-file copy stages.
                _read_identity_tokens(main_only)

            os_home = tmp_path / "pod" / "os-home"
            os_home.mkdir(parents=True)
            staged = _stage_runtime_auth_store(os_home, _kiro_mapping(host))

        target = os_home / ".local" / "share" / "kiro-cli" / "data.sqlite3"
        assert staged == 1, "only the database, never its sidecars"
        assert _read_identity_tokens(target) == ["live-token"]

    @requires_pinned_walk
    def test_a_source_that_is_not_a_database_refuses_the_whole_store(self, tmp_path: Path) -> None:
        """Refuse, don't half-stage: the token is what the store is for.

        A store without a usable database looks provisioned while every turn fails to
        sign in. Databases are handled first, so the refusal lands before any staging.
        """
        host = tmp_path / "host-home"
        store = self._store(host)
        (store / "data.sqlite3").write_bytes(b"this is not a database")
        (store / "tui.js").write_text("non-credential asset")
        os_home = tmp_path / "pod" / "os-home"
        os_home.mkdir(parents=True)

        assert _stage_runtime_auth_store(os_home, _kiro_mapping(host)) == 0
        target = os_home / ".local" / "share" / "kiro-cli"
        assert not (target / "data.sqlite3").exists()
        assert not (target / "tui.js").exists(), "refusal must not leave a half-staged tree"
        assert not (target / ".data.sqlite3.staging").exists()

    @requires_pinned_walk
    def test_the_snapshot_is_owner_only_and_the_host_store_is_not_written(
        self, tmp_path: Path
    ) -> None:
        """0o600 on the snapshot, and read-only on the operator's live store."""
        host = tmp_path / "host-home"
        store = self._store(host)
        _write_identity_db(store / "data.sqlite3", "token")
        before = sorted(p.name for p in store.iterdir())
        os_home = tmp_path / "pod" / "os-home"
        os_home.mkdir(parents=True)

        _stage_runtime_auth_store(os_home, _kiro_mapping(host))

        staged = os_home / ".local" / "share" / "kiro-cli" / "data.sqlite3"
        assert oct(staged.stat().st_mode)[-3:] == "600"
        assert sorted(p.name for p in store.iterdir()) == before

    @requires_pinned_walk
    def test_a_stale_staging_temp_from_a_killed_boot_is_replaced(self, tmp_path: Path) -> None:
        """The temp is created O_EXCL, so a leftover must be cleared first.

        An interrupted backup leaves a short file at the temp name. Without the
        unlink, the next boot's exclusive create fails and the store is refused
        forever -- a killed boot would permanently wedge staging.
        """
        host = tmp_path / "host-home"
        store = self._store(host)
        _write_identity_db(store / "data.sqlite3", "token")
        os_home = tmp_path / "pod" / "os-home"
        target = os_home / ".local" / "share" / "kiro-cli"
        target.mkdir(parents=True)
        (target / ".data.sqlite3.staging").write_bytes(b"truncated backup")

        assert _stage_runtime_auth_store(os_home, _kiro_mapping(host)) == 1
        assert _read_identity_tokens(target / "data.sqlite3") == ["token"]
        assert not (target / ".data.sqlite3.staging").exists()

    def test_sidecar_recognition_is_anchored_on_the_database_suffix(self) -> None:
        """Only a real sidecar is skipped -- an unrelated name is still staged."""
        assert _is_sqlite_sidecar("data.sqlite3-wal")
        assert _is_sqlite_sidecar("data.sqlite3-shm")
        assert _is_sqlite_sidecar("data.sqlite3-journal")
        assert _is_sqlite_sidecar("other.sqlite-wal")
        # Not sidecars: the stem is not a database name.
        assert not _is_sqlite_sidecar("data.sqlite3")
        assert not _is_sqlite_sidecar("notes-wal")
        assert not _is_sqlite_sidecar("kiro-cli-shm")
        assert not _is_sqlite_sidecar("tui.js")


def test_the_staged_store_is_readable_through_the_pod_mask_in_every_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified, not assumed: no tier re-anchors a mask over the staged store.

    The tier lists exclude `.local/share/kiro-cli` by design, so
    `_pod_os_home_targets` should never produce an entry covering it. Asserted for
    all three tiers because staging a token into a bind-masked-empty directory is
    exactly the failure mode this PR already hit once with `.aws`.
    """
    os_home = "/pods/p1/os-home"
    monkeypatch.setenv("KIROCREW_POD", "1")
    monkeypatch.setenv("KIROCREW_OS_HOME", os_home)

    for dirs in (
        tuple(sb._sandbox_policy().strict_dirs()),
        tuple(sb._sandbox_policy().cc_dirs()),
        tuple(sb._STANDARD_DIRS),
    ):
        targets = _pod_targets = sb._pod_os_home_targets(dirs)
        for mapping in store_mappings("linux", Path("/home/user"), {}):
            path = os.path.join(os_home, *mapping.staged_relative.parts)
            blocked = [t for t in _pod_targets if path == t or path.startswith(t + os.sep)]
            assert blocked == [], f"{path} masked by {blocked} (tier had {len(targets)} entries)"


class TestTheStagedStoresResidualIsPinned:
    """What DOES and does NOT fence the staged identity store (a bearer-token database).

    1. the TOOL gate refuses every staged store path, on every platform row;
    2. the bash-TEXT layer does not, for any path (see the ``KIROCREW_HOME`` parity);
    3. the OS bind-mask does not, because the harness resolves its token from it.

    Fact 3 holds because the harness and its tool subprocesses share one mount
    namespace and one mask; closing it needs a pod-scoped sign-in, not a mask entry.
    """

    @staticmethod
    def _staged_dbs(os_home: str) -> list[str]:
        """Every platform row's staged database path, from the seeding's own table."""
        return [
            os.path.join(os_home, *leaf.split("/"), "data.sqlite3") for leaf in fenced_home_dirs()
        ]

    def test_the_tool_gate_refuses_every_staged_store_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``is_sensitive_path`` is the layer that DOES separate the two audiences.

        A tool call naming the path is refused; only a raw ``open()`` in a spawned
        shell reaches the bytes. Driven off ``fenced_home_dirs`` -- the same table
        the seeding stages from -- so a store row added there is covered with no
        second edit, which is the property that keeps this from rotting.
        """
        os_home = str(tmp_path / "pod-os-home")
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", os_home)

        staged = self._staged_dbs(os_home)
        assert staged, "fenced_home_dirs() is empty; the fence has nothing to cover"
        for path in staged:
            assert security.is_sensitive_path(path) is True, path
        # The sidecars carry the same bytes.
        first = staged[0]
        assert security.is_sensitive_path(f"{first}-wal") is True

    def test_the_real_home_store_stays_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pod anchoring is ADDITIVE -- it must not displace the host's fence."""
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.delenv("KIROCREW_OS_HOME", raising=False)

        for leaf in fenced_home_dirs():
            path = str(Path.home() / Path(*leaf.split("/")) / "data.sqlite3")
            assert security.is_sensitive_path(path) is True, path

    def test_no_path_spelling_is_covered_by_the_command_matcher_any_more(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command matcher fences NO path, the real home's credential files included.

        Asserted both ways (the resolving gate still refuses each), so a change to
        either layer fails here.
        """
        os_home = str(tmp_path / "pod-os-home")
        crew_home = str(tmp_path / "crew-home")
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", os_home)
        monkeypatch.setenv("KIROCREW_HOME", crew_home)

        spellings = [
            os.path.join(os_home, ".local", "share", "kiro-cli", "data.sqlite3"),
            os.path.join(crew_home, ".env"),
            # The real home's own files -- the half the old parity claim got wrong.
            str(Path.home() / ".aws" / "credentials"),
            str(Path.home() / ".ssh" / "id_rsa"),
        ]
        for path in spellings:
            assert security.is_sensitive_bash_command(f"cat {path}") is None, path
            assert security.is_sensitive_path(path) is True, path

    def test_the_surviving_command_tiers_still_fire(self) -> None:
        """Three tiers survive; a read of a fenced path is not one of them.

        Asserted so "the matcher allows every path" is read as a scoped fact rather
        than as the gate being off: the IMDS tier still refuses, which is what
        distinguishes a deliberate narrowing from a regression.
        """
        assert (
            security.is_sensitive_bash_command("curl http://169.254.169.254/latest/meta-data/iam/")
            is not None
        )

    def test_masking_the_store_would_take_the_harness_with_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The staged copy stays reachable at the child's own ``$HOME`` (fact 3).

        Pod-home side of ``test_the_agent_runtime_auth_stores_stay_visible``: a mask
        entry covering it would break sign-in.
        """
        os_home = "/pods/p1/os-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", os_home)

        store_leaves = set(fenced_home_dirs())
        for tier_name in ("_STRICT_DIRS", "_CC_DIRS", "_STANDARD_DIRS"):
            tier = getattr(sb, tier_name, None)
            if tier is None:  # pragma: no cover - tier renamed
                continue
            covering = [
                entry
                for entry in tier
                if entry in store_leaves or any(s.startswith(f"{entry}/") for s in store_leaves)
            ]
            assert not covering, f"{tier_name} masks the identity store: {covering}"
