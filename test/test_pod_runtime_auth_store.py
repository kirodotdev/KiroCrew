"""The pod stages the agent runtime's OWN identity store, not just the SSO cache.

`test_the_agent_runtime_auth_stores_stay_visible` in the sandbox-mask suite pins
`.local/share/kiro-cli` and `.local/share/amazon-q` out of every masking tier
because "the agent runtime is itself spawned inside this sandbox and resolves its
own access token from that store". Not masking it is only half the requirement:
under the pod's remapped ``HOME`` the store must also EXIST there. Staging only
`.aws/sso/cache` left the pod's child at kiro-cli's login gate with a readable,
correctly-unmasked corridor -- the cache is not where the token is resolved.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
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

    The fixtures write a real database here, not plain text. Staging is not a byte
    copy: a live database is snapshotted through SQLite's backup
    API, which refuses a file that is not a database -- so a text fixture would
    exercise the refusal path on every test instead of the staging path. Writing a
    genuine database is also what lets these tests assert the property that matters
    (the ROWS arrived), rather than that some bytes did.
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
    #
    # Two independent axes, which is what made this fail as ``[macos] 0 == 1`` on a
    # runner that is neither macOS nor Linux. The MAPPING's platform decides the
    # source and staged layout and is supplied explicitly here, so this test is
    # about the table's rows rather than the host's identity. Whether staging can
    # HAPPEN at all is a host CAPABILITY: ``_stage_runtime_auth_store`` refuses and
    # returns 0 where ``pinned_fs.supports_pinned_walk()`` is False (Windows has no
    # ``O_DIRECTORY``/``O_NOFOLLOW``/``dir_fd``), because the pod's own credential
    # tree is published through the pinned no-follow chokepoint and degrading that
    # to a by-name write is not on offer. So the platform ROWS are parametrized and
    # the CAPABILITY is gated -- the no-capability half is asserted unconditionally
    # by ``test_a_platform_without_pinned_walk_stages_nothing``, so nothing about
    # this contract goes unproven on a host that skips this case.
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
    """A live store is a SET of files that only agree at an instant.

    The host's kiro-cli is a running process. Copying ``data.sqlite3`` and then its
    ``-wal`` with two separate reads takes them at two different times, so a
    checkpoint landing between the copies yields a main file from after it and a WAL
    from before -- a torn snapshot staged into the pod as if it were sign-in state.
    The window is BETWEEN the copies, so no per-file copy loop can close it; the fix
    is to stop copying the file set and snapshot the database instead.
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
        """RED-FIRST: the generation tear, forced at the point it happens.

        The old loop staged names in sorted order, so ``data.sqlite3`` was read
        before ``data.sqlite3-wal``. This drives a WAL-mode database to exactly that
        interleaving: rows are committed into the WAL and left UNCHECKPOINTED, so the
        main file alone does not contain them. A byte copy of the main file therefore
        stages a database missing the token, while a snapshot reads through the engine
        and contains every committed row.
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

        A tree staged without a usable database presents the pod as provisioned while
        every agent turn fails to sign in, which is strictly worse than the
        signed-out boot the viability probe already reports. Databases are handled
        before a directory's plain files and the database sits at the store root, so
        the refusal lands before anything is staged rather than half-way through.
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
    """What DOES and does NOT fence the staged identity store, measured.

    The staged store is a host-derived bearer-token database sitting in a tree the
    pod's agent can reach, so its exposure needs pinning in both directions rather
    than describing. Three facts, each asserted so a silent drift in any of them
    fails here:

    1. the TOOL gate refuses every staged store path, on every platform row;
    2. the bash-TEXT layer does not, and that is main's stated posture rather than
       a gap this staging opened (see the ``KIROCREW_HOME`` parity below);
    3. the OS bind-mask does not cover the store either, because the harness
       resolves its token from it and a mask acts on the whole mount namespace.

    Fact 3 is why the finding's "mask it from tool subprocesses only" cannot be
    implemented as stated: Crew wraps the HARNESS, and the harness spawns its tool
    subprocesses inside that wrapper, so the two audiences share one namespace and
    one mask. Closing it means a pod-scoped sign-in that never puts a host bearer in
    the pod tree -- a design change, not a mask entry.
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
        """Stated, not silent: the text layer fences NO path at all.

        What is left is simpler and worth pinning as such: the command matcher
        allows every path spelling, the real home's own credential files included,
        so the pod tree is not a special case and no parity claim applies. The
        assertion runs in BOTH directions -- the resolving gate still refuses each
        of these -- so a future round that reinstates a text-layer path matcher, or
        loses the resolving fence, fails here rather than drifting quietly.
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
        """The structural reason fact 3 holds, pinned as a requirement.

        ``test_the_agent_runtime_auth_stores_stay_visible`` in the sandbox-mask
        suite states the harness side: the runtime resolves its own access token
        from this store while running inside the sandbox. This asserts the pod-home
        consequence -- the staged copy must be reachable at the path the child's own
        ``$HOME`` resolves to, so a future mask entry covering it is a break rather
        than a hardening and fails here.
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
