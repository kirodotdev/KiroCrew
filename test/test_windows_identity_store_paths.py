"""Every reader of kiro-cli's Windows identity store must name the same places.

Several places in the tree resolve that store, each with its own hardcoded
per-platform list and no shared helper, so they drift apart one reader at a time.
The fence (``_SENSITIVE_HOME_DIRS``), the trusted live-store list
(``_CLI_SQLITE_DBS``), the logout fingerprint (``_win32_identity_store_path``)
and ``kiro_cli_state_dbs`` all cover both AppData roots; sign-in staging was the
last one still naming ``AppData/Local`` alone, so on a host whose CLI keeps its
store under Roaming it staged nothing and the staged home looked signed-out.

These tests pin the agreement, and in particular the ordering constraint that
makes it safe: a path may be TRUSTED only if it is also FENCED. A trusted path
outside the fence is one an agent file tool could author, which would let it
forge the identity rows these readers believe.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from kiro_crew import identity_stores as ids
from kiro_crew import kiro_prerequisite as kp
from kiro_crew.dashboard.handlers import kiro_usage_api as usage_api
from kiro_crew.identity_stores import Trust
from kiro_crew.security import _SENSITIVE_HOME_DIRS

# The two roots the installed CLI is observed using, for each product whose store
# holds a live SSO bearer token.
_EXPECTED_WINDOWS_STORE_DIRS = frozenset(
    {
        "AppData/Local/kiro-cli",
        "AppData/Local/amazon-q",
        "AppData/Roaming/kiro-cli",
        "AppData/Roaming/amazon-q",
    }
)


def _home_relative(path: Path) -> str:
    """Return ``path`` relative to the real home, as a forward-slash string.

    The store tuples are built from ``Path.home()`` at import time, so on a POSIX
    CI host the Windows entries are POSIX-shaped strings under that home. Only
    the home-relative remainder is comparable to ``_SENSITIVE_HOME_DIRS``, which
    is itself expressed home-relative with forward slashes.
    """

    return path.relative_to(Path.home()).as_posix()


def _windows_entries(paths: tuple[Path, ...]) -> set[str]:
    """Home-relative DIRECTORIES of the Windows entries in a store tuple."""

    relatives = (_home_relative(path) for path in paths)
    return {
        str(PurePosixPath(relative).parent)
        for relative in relatives
        if relative.startswith("AppData/")
    }


def _is_fenced(relative_dir: str) -> bool:
    """Whether a home-relative directory is inside a fenced store directory."""

    candidate = PurePosixPath(relative_dir)
    return any(
        candidate == PurePosixPath(fenced) or PurePosixPath(fenced) in candidate.parents
        for fenced in _SENSITIVE_HOME_DIRS
    )


class TestWindowsIdentityStorePathsAgree:
    def test_fence_covers_every_expected_store(self) -> None:
        """The fence is the floor: it must name every location anyone trusts."""

        assert _EXPECTED_WINDOWS_STORE_DIRS <= set(_SENSITIVE_HOME_DIRS)

    def test_every_trusted_windows_store_is_fenced(self) -> None:
        """The ordering constraint, stated as an assertion.

        Membership in the trusted tuple sets ``from_cli_store=True``, a claim that
        rests entirely on agent file tools being unable to write the path. Adding
        a location to a store tuple without fencing it first creates a forgeable
        trusted path, so this fails on that mistake rather than letting it ship.

        Derived from the tuples themselves rather than from a hardcoded list, so a
        future entry is covered without anyone remembering to extend this test --
        which is the failure mode that let staging drift in the first place.
        """

        trusted = _windows_entries(usage_api._CLI_SQLITE_DBS) | _windows_entries(
            usage_api._OTHER_SQLITE_DBS
        )
        assert trusted, "no Windows entries found -- the tuples changed shape"
        unfenced = sorted(entry for entry in trusted if not _is_fenced(entry))
        assert not unfenced, f"trusted but NOT fenced: {unfenced}"

    def test_read_lists_cover_both_roots_for_both_products(self) -> None:
        """A store fenced on Windows but absent from the read list is unreadable."""

        assert _windows_entries(usage_api._CLI_SQLITE_DBS) == {
            "AppData/Local/kiro-cli",
            "AppData/Roaming/kiro-cli",
        }
        assert _windows_entries(usage_api._OTHER_SQLITE_DBS) == {
            "AppData/Local/amazon-q",
            "AppData/Roaming/amazon-q",
        }


class TestSigninStagingCoversBothAppDataRoots:
    """Staging was the last reader naming one root, so a Roaming host staged nothing."""

    def test_staging_covers_both_roots(self, tmp_path: Path) -> None:
        mappings = kp._auth_store_mappings("win32", tmp_path, {})
        staged = {
            mapping.source.relative_to(tmp_path).as_posix()
            for mapping in mappings
            if mapping.source.is_relative_to(tmp_path / "AppData")
        }
        assert staged == set(_EXPECTED_WINDOWS_STORE_DIRS)

    def test_the_two_roots_of_one_product_are_alternates(self, tmp_path: Path) -> None:
        """They share a group, which is what defers the abort across them."""

        mappings = kp._auth_store_mappings("win32", tmp_path, {})
        for app_name in ("kiro-cli", "amazon-q"):
            roots = [m for m in mappings if m.group == f"win32:{app_name}"]
            assert len(roots) == 2
            assert len({m.staged_relative for m in roots}) == 2

    def test_each_root_stages_under_its_own_relative_path(self, tmp_path: Path) -> None:
        """Distinct staged paths, so one root cannot overwrite the other's store."""

        mappings = kp._auth_store_mappings("win32", tmp_path, {})
        staged_relatives = [m.staged_relative for m in mappings]
        assert len(staged_relatives) == len(set(staged_relatives))

    def test_the_aws_sso_cache_is_not_an_alternate_of_anything(self, tmp_path: Path) -> None:
        """One location holding several token files: losing any one must still abort."""

        mappings = kp._auth_store_mappings("win32", tmp_path, {})
        sso = [m for m in mappings if m.source == tmp_path / ".aws" / "sso" / "cache"]
        assert len(sso) == 1
        assert sso[0].group is None

    def test_env_vars_are_honoured_for_the_source_side(self, tmp_path: Path) -> None:
        """The real store lives where the variables point; the staged side is fixed."""

        mappings = kp._auth_store_mappings(
            "win32",
            tmp_path,
            {"APPDATA": str(tmp_path / "roam"), "LOCALAPPDATA": str(tmp_path / "loc")},
        )
        kiro = {m.source: m.staged_relative for m in mappings if m.source.name == "kiro-cli"}
        assert kiro == {
            tmp_path / "loc" / "kiro-cli": Path("AppData") / "Local" / "kiro-cli",
            tmp_path / "roam" / "kiro-cli": Path("AppData") / "Roaming" / "kiro-cli",
        }


def _write_identity_store(path: Path) -> None:
    """Write a store holding every identity table, so projection succeeds."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    with contextlib.closing(connection):
        for table in kp._AUTH_IDENTITY_TABLES:
            connection.execute(f'CREATE TABLE "{table}" (key TEXT PRIMARY KEY, value TEXT)')
        connection.commit()


def _write_unusable_store(path: Path) -> None:
    """Write a store with NO identity table, the shape that fails projection."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    with contextlib.closing(connection):
        connection.execute("CREATE TABLE unrelated (k TEXT)")
        connection.commit()


class TestStagingToleratesTheUnusedAppDataRoot:
    """Widening staging to both roots must not turn a stale store into a failure.

    Staging aborts rather than omit a matched store, because a staged home with no
    identity looks signed-out. That rule is right per LOCATION and wrong across
    alternates: once both roots are staged, a leftover database in the root a host
    does not use would abort staging from the root it does, breaking sign-in on
    exactly the hosts this change is meant to fix.
    """

    def _stage(self, home: Path, monkeypatch, staging: Path) -> None:
        monkeypatch.setattr(kp, "_ensure_auth_staging_parent", lambda h: staging)
        kp._prepare_auth_workspace("win32", home, {}, {})

    def test_a_usable_root_survives_a_dead_store_in_the_other(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        staging = tmp_path / "stage"
        staging.mkdir()
        _write_identity_store(tmp_path / "AppData" / "Local" / "kiro-cli" / kp._AUTH_SQLITE_DB)
        _write_unusable_store(tmp_path / "AppData" / "Roaming" / "kiro-cli" / kp._AUTH_SQLITE_DB)

        self._stage(tmp_path, monkeypatch, staging)

    def test_the_reverse_shape_also_survives(self, tmp_path: Path, monkeypatch) -> None:
        """A downgraded host: live store in Roaming, stale leftover in Local."""

        staging = tmp_path / "stage"
        staging.mkdir()
        _write_unusable_store(tmp_path / "AppData" / "Local" / "kiro-cli" / kp._AUTH_SQLITE_DB)
        _write_identity_store(tmp_path / "AppData" / "Roaming" / "kiro-cli" / kp._AUTH_SQLITE_DB)

        self._stage(tmp_path, monkeypatch, staging)

    def test_no_usable_store_in_any_root_still_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """The signed-out-looking case must stay loud."""

        staging = tmp_path / "stage"
        staging.mkdir()
        _write_unusable_store(tmp_path / "AppData" / "Local" / "kiro-cli" / kp._AUTH_SQLITE_DB)

        with pytest.raises(OSError):
            self._stage(tmp_path, monkeypatch, staging)

    def test_a_dead_store_in_both_roots_aborts(self, tmp_path: Path, monkeypatch) -> None:
        staging = tmp_path / "stage"
        staging.mkdir()
        for root in ("Local", "Roaming"):
            _write_unusable_store(tmp_path / "AppData" / root / "kiro-cli" / kp._AUTH_SQLITE_DB)

        with pytest.raises(OSError):
            self._stage(tmp_path, monkeypatch, staging)

    def test_a_roaming_only_host_actually_stages_its_store(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The bug this change fixes: Roaming-only staged nothing at all."""

        staging = tmp_path / "stage"
        staging.mkdir()
        _write_identity_store(tmp_path / "AppData" / "Roaming" / "kiro-cli" / kp._AUTH_SQLITE_DB)

        monkeypatch.setattr(kp, "_ensure_auth_staging_parent", lambda h: staging)
        workspace = kp._prepare_auth_workspace("win32", tmp_path, {}, {})

        staged_db = Path(workspace.root) / "AppData" / "Roaming" / "kiro-cli" / kp._AUTH_SQLITE_DB
        assert staged_db.exists()


class TestReadersAgreeWithCanonicalTable:
    """The six former copies are now projections of ``identity_stores`` (#6352).

    These re-point the ratchet at the single canonical table: every reader must
    equal the projection it now wraps, so drift is impossible by construction
    rather than by remembering to update N hardcoded lists.
    """

    def test_fence_identity_dirs_come_from_the_table(self) -> None:
        """Every fenced identity dir is a canonical row, and vice versa."""

        fenced_identity = {
            entry for entry in _SENSITIVE_HOME_DIRS if entry in set(ids.fenced_home_dirs())
        }
        assert fenced_identity == set(ids.fenced_home_dirs())

    def test_usage_read_lists_are_the_projections(self) -> None:
        assert usage_api._CLI_SQLITE_DBS == ids.sqlite_dbs(Trust.TRUSTED)
        assert usage_api._OTHER_SQLITE_DBS == ids.sqlite_dbs(Trust.OTHER)

    def test_identity_store_path_matches_selected_store(self, tmp_path: Path) -> None:
        for platform in ("darwin", "linux", "win32"):
            assert kp.kiro_identity_store_path(platform, tmp_path, {}) == (
                ids.selected_store(platform, tmp_path)
            )

    def test_state_dbs_match_the_projection(self, tmp_path: Path) -> None:
        from kiro_crew import kiro_cli

        for platform in ("darwin", "linux", "win32"):
            assert kiro_cli.kiro_cli_state_dbs(platform, tmp_path, {}) == (
                ids.state_db_candidates(platform, tmp_path, {})
            )

    def test_auth_sqlite_db_constant_is_the_alias(self) -> None:
        assert kp._AUTH_SQLITE_DB == ids.AUTH_SQLITE_DB
        from kiro_crew import kiro_cli

        assert kiro_cli.KIRO_CLI_STATE_DB == ids.AUTH_SQLITE_DB

    def test_staging_sources_track_the_table_on_win32(self, tmp_path: Path) -> None:
        """Staging's win32 source dirs are the table's win32 mapping sources."""

        staged = {
            m.source.relative_to(tmp_path).as_posix()
            for m in kp._auth_store_mappings("win32", tmp_path, {})
            if m.source.is_relative_to(tmp_path / "AppData")
        }
        table = {m.staged_relative.as_posix() for m in ids.store_mappings("win32", tmp_path, {})}
        assert staged == table
