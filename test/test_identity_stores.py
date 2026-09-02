"""The single canonical identity-store table and its projections (#6352).

These tests pin two things: the table's structural invariants (every projection
stays a subset of the table; TRUSTED implies FENCED), and GOLDEN freezes of the
pre-refactor outputs so a future edit that changes an order or drops a location
fails here rather than shipping. The six former readers are now thin wrappers
over these projections; their own behaviour is pinned in their own suites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import identity_stores as ids
from kiro_crew.identity_stores import (
    IDENTITY_STORE_ROOTS,
    Platform,
    Product,
    Trust,
    fenced_home_dirs,
    selected_store,
    sqlite_dbs,
    state_db_candidates,
    store_mappings,
)


class TestTableInvariants:
    def test_every_row_dir_is_in_fenced_home_dirs(self) -> None:
        fenced = fenced_home_dirs()
        for root in IDENTITY_STORE_ROOTS:
            assert root.home_relative_dir in fenced

    def test_trusted_implies_kiro_cli_and_every_row_is_fenced(self) -> None:
        # TRUSTED-implies-FENCED holds by construction: every row is a fenced
        # store dir, and only kiro-cli rows are TRUSTED.
        fenced = set(fenced_home_dirs())
        for root in IDENTITY_STORE_ROOTS:
            assert root.home_relative_dir in fenced
            if root.trust is Trust.TRUSTED:
                assert root.product is Product.KIRO_CLI

    def test_fenced_dirs_are_unique(self) -> None:
        dirs = fenced_home_dirs()
        assert len(dirs) == len(set(dirs))


class TestProjectionsAreSubsetsOfTable:
    def _table_dirs(self) -> set[str]:
        return {root.home_relative_dir for root in IDENTITY_STORE_ROOTS}

    def test_sqlite_dbs_dirs_subset(self) -> None:
        home = Path("/home/u")
        for trust in (Trust.TRUSTED, Trust.OTHER):
            dbs = sqlite_dbs(trust, home)
            # Non-vacuous: each trust class must project real rows. (A prior
            # revision passed Product members here, matched nothing, and this
            # loop asserted over an empty tuple.)
            assert len(dbs) == 4
            for db in dbs:
                rel = db.parent.relative_to(home).as_posix()
                assert rel in self._table_dirs()

    def test_sqlite_dbs_rejects_a_product_member(self) -> None:
        with pytest.raises(TypeError):
            sqlite_dbs(Product.KIRO_CLI)  # type: ignore[arg-type]

    def test_store_mappings_staged_dirs_subset(self) -> None:
        for platform in ("darwin", "win32", "posix"):
            for mapping in store_mappings(platform, Path("/home/u"), {}):
                assert mapping.staged_relative.as_posix() in self._table_dirs()

    def test_state_db_candidates_dirs_subset(self) -> None:
        home = Path("/home/u")
        for platform in ("darwin", "win32", "posix"):
            for db in state_db_candidates(platform, home, {}):
                rel = db.parent.relative_to(home).as_posix()
                assert rel in self._table_dirs()

    def test_selected_store_dir_subset(self) -> None:
        """Live-store selection is table-derived like every other projection.

        This is the projection logout detection fingerprints from -- if it ever
        re-hardcodes a path, a relocated table row would update the fence,
        tuples, staging, and state-db candidates while selection silently kept
        the old path."""
        home = Path("/home/u")
        for platform in ("darwin", "win32", "posix"):
            store = selected_store(platform, home)
            rel = store.parent.relative_to(home).as_posix()
            assert rel in self._table_dirs()


class TestGoldenFencedDirs:
    def test_exactly_eight_dirs_in_frozen_order(self) -> None:
        assert fenced_home_dirs() == (
            ".local/share/kiro-cli",
            ".local/share/amazon-q",
            "Library/Application Support/kiro-cli",
            "Library/Application Support/amazon-q",
            "AppData/Local/kiro-cli",
            "AppData/Local/amazon-q",
            "AppData/Roaming/kiro-cli",
            "AppData/Roaming/amazon-q",
        )


class TestGoldenSqliteTuples:
    def test_kiro_cli_tuple_exact_order(self) -> None:
        home = Path("/home/u")
        assert sqlite_dbs(Trust.TRUSTED, home) == (
            home / ".local" / "share" / "kiro-cli" / "data.sqlite3",
            home / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3",
            home / "AppData" / "Local" / "kiro-cli" / "data.sqlite3",
            home / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3",
        )

    def test_amazon_q_tuple_exact_order(self) -> None:
        home = Path("/home/u")
        assert sqlite_dbs(Trust.OTHER, home) == (
            home / ".local" / "share" / "amazon-q" / "data.sqlite3",
            home / "Library" / "Application Support" / "amazon-q" / "data.sqlite3",
            home / "AppData" / "Local" / "amazon-q" / "data.sqlite3",
            home / "AppData" / "Roaming" / "amazon-q" / "data.sqlite3",
        )

    def test_default_home_matches_explicit(self) -> None:
        assert sqlite_dbs(Trust.TRUSTED) == sqlite_dbs(Trust.TRUSTED, Path.home())


class TestGoldenSelectedStore:
    def test_darwin_fixed_anchor(self) -> None:
        home = Path("/home/u")
        assert selected_store("darwin", home) == (
            home / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3"
        )

    def test_posix_fixed_anchor(self) -> None:
        home = Path("/home/u")
        assert selected_store("linux", home) == (
            home / ".local" / "share" / "kiro-cli" / "data.sqlite3"
        )

    def test_win32_neither_exists_prefers_local(self, tmp_path: Path) -> None:
        assert selected_store("win32", tmp_path) == (
            tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        )

    def test_win32_only_roaming_exists(self, tmp_path: Path) -> None:
        roaming = tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        roaming.parent.mkdir(parents=True)
        roaming.write_bytes(b"x")
        assert selected_store("win32", tmp_path) == roaming

    def test_win32_both_exist_newer_roaming_wins(self, tmp_path: Path) -> None:
        import os

        local = tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        roaming = tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        for db in (local, roaming):
            db.parent.mkdir(parents=True)
            db.write_bytes(b"x")
        os.utime(local, (1000, 1000))
        os.utime(roaming, (2000, 2000))
        assert selected_store("win32", tmp_path) == roaming

    def test_win32_equal_mtime_prefers_local(self, tmp_path: Path) -> None:
        import os

        local = tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        roaming = tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        for db in (local, roaming):
            db.parent.mkdir(parents=True)
            db.write_bytes(b"x")
            os.utime(db, (1500, 1500))
        assert selected_store("win32", tmp_path) == local

    def test_win32_wal_sidecar_counts_as_recency(self, tmp_path: Path) -> None:
        import os

        local = tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        roaming = tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        for db in (local, roaming):
            db.parent.mkdir(parents=True)
            db.write_bytes(b"x")
        # Local main file is newest, but Roaming's WAL sidecar is newer still,
        # so Roaming (the actually-written store) must win.
        os.utime(local, (3000, 3000))
        os.utime(roaming, (1000, 1000))
        wal = roaming.with_name(roaming.name + "-wal")
        wal.write_bytes(b"x")
        os.utime(wal, (4000, 4000))
        assert selected_store("win32", tmp_path) == roaming


class TestGoldenStoreMappings:
    def test_linux_source_and_staged(self) -> None:
        home = Path("/home/u")
        mappings = store_mappings("linux", home, {})
        assert [(m.source, m.staged_relative, m.product) for m in mappings] == [
            (
                home / ".local" / "share" / "kiro-cli",
                Path(".local/share/kiro-cli"),
                Product.KIRO_CLI,
            ),
            (
                home / ".local" / "share" / "amazon-q",
                Path(".local/share/amazon-q"),
                Product.AMAZON_Q,
            ),
        ]

    def test_darwin_source_and_staged(self) -> None:
        home = Path("/home/u")
        mappings = store_mappings("darwin", home, {})
        assert [(m.source, m.staged_relative) for m in mappings] == [
            (
                home / "Library" / "Application Support" / "kiro-cli",
                Path("Library/Application Support/kiro-cli"),
            ),
            (
                home / "Library" / "Application Support" / "amazon-q",
                Path("Library/Application Support/amazon-q"),
            ),
        ]

    def test_win32_product_major_order_matches_pre_refactor(self) -> None:
        """PRODUCT-MAJOR: the exact emission order of the pre-refactor
        ``_auth_store_mappings`` win32 loop (all kiro-cli, then all amazon-q).
        Order-sensitive on purpose -- a reorder must fail here, not slide by."""
        home = Path("/home/u")
        mappings = store_mappings("win32", home, {})
        assert [(m.source, m.staged_relative) for m in mappings] == [
            (home / "AppData" / "Local" / "kiro-cli", Path("AppData/Local/kiro-cli")),
            (home / "AppData" / "Roaming" / "kiro-cli", Path("AppData/Roaming/kiro-cli")),
            (home / "AppData" / "Local" / "amazon-q", Path("AppData/Local/amazon-q")),
            (home / "AppData" / "Roaming" / "amazon-q", Path("AppData/Roaming/amazon-q")),
        ]

    def test_win32_env_vars_honoured_on_source_only(self) -> None:
        home = Path("/home/u")
        mappings = store_mappings("win32", home, {"LOCALAPPDATA": "/loc", "APPDATA": "/roam"})
        kiro = {m.source: m.staged_relative for m in mappings if m.source.name == "kiro-cli"}
        assert kiro == {
            Path("/loc/kiro-cli"): Path("AppData/Local/kiro-cli"),
            Path("/roam/kiro-cli"): Path("AppData/Roaming/kiro-cli"),
        }

    def test_linux_xdg_data_home_honoured_on_source_only(self) -> None:
        home = Path("/home/u")
        mappings = store_mappings("linux", home, {"XDG_DATA_HOME": "/xdg"})
        kiro = [m for m in mappings if m.product is Product.KIRO_CLI][0]
        assert kiro.source == Path("/xdg/kiro-cli")
        assert kiro.staged_relative == Path(".local/share/kiro-cli")


class TestGoldenStateDbCandidates:
    def test_darwin(self) -> None:
        home = Path("/home/u")
        assert state_db_candidates("darwin", home, {}) == (
            home / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3",
        )

    def test_linux(self) -> None:
        home = Path("/home/u")
        assert state_db_candidates("linux", home, {}) == (
            home / ".local" / "share" / "kiro-cli" / "data.sqlite3",
        )

    def test_win32_local_before_roaming(self) -> None:
        home = Path("/home/u")
        assert state_db_candidates("win32", home, {}) == (
            home / "AppData" / "Local" / "kiro-cli" / "data.sqlite3",
            home / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3",
        )

    def test_linux_xdg_data_home_honoured(self) -> None:
        home = Path("/home/u")
        assert state_db_candidates("linux", home, {"XDG_DATA_HOME": "/xdg"}) == (
            Path("/xdg/kiro-cli/data.sqlite3"),
        )

    def test_win32_localappdata_honoured_roaming_fixed(self) -> None:
        # Matches the pre-refactor kiro_cli_state_dbs: LOCALAPPDATA moves the
        # Local candidate, APPDATA is NOT followed (Roaming stays anchored).
        home = Path("/home/u")
        assert state_db_candidates(
            "win32",
            home,
            {"LOCALAPPDATA": "/loc", "APPDATA": "/roam"},
        ) == (
            Path("/loc/kiro-cli/data.sqlite3"),
            home / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3",
        )

    def test_win32_localappdata_equal_to_default_dedupes(self) -> None:
        home = Path("/home/u")
        # When LOCALAPPDATA points at the default Local root, no duplicate.
        result = state_db_candidates(
            "win32",
            home,
            {"LOCALAPPDATA": str(home / "AppData" / "Local")},
        )
        assert result == (
            home / "AppData" / "Local" / "kiro-cli" / "data.sqlite3",
            home / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3",
        )


class TestConstants:
    def test_auth_sqlite_db_filename(self) -> None:
        assert ids.AUTH_SQLITE_DB == "data.sqlite3"

    def test_platform_product_trust_values(self) -> None:
        assert Platform.DARWIN.value == "darwin"
        assert Platform.WIN32.value == "win32"
        assert Platform.POSIX.value == "posix"
        assert Product.KIRO_CLI.value == "kiro-cli"
        assert Product.AMAZON_Q.value == "amazon-q"
        assert {t.value for t in Trust} == {"trusted", "other"}


class TestUsageTuplesAnchorTheRealHome:
    """Replacement pin for the host-isolation ratchet's deleted exclusions.

    ``kiro_usage_api._CLI_SQLITE_DBS`` / ``_OTHER_SQLITE_DBS`` used to be direct
    import-time ``Path.home()`` bindings, tracked by
    ``test_host_isolation_floor.py``'s ratchet as excluded-with-reason security
    anchors ("must name the REAL home"). As projections they no longer match
    that tripwire's AST shape, so THIS test carries the property forward: the
    tuples must anchor the operator's real home at import, because an entry is
    trusted precisely when it equals a home-anchored path inside the
    ``_SENSITIVE_HOME_DIRS`` fence -- a redirected value would manufacture a
    forgeable "trusted" path. Tests that need different paths must stub the
    READER (as ``test_kiro_usage_api.py`` does per test), never move the anchor.
    """

    def test_import_time_tuples_are_real_home_anchored(self) -> None:
        from kiro_crew.dashboard.handlers import kiro_usage_api as api

        real_home = Path.home()
        for db in (*api._CLI_SQLITE_DBS, *api._OTHER_SQLITE_DBS):
            assert real_home in db.parents, (
                f"{db} is not anchored at the real home -- the trusted-store "
                "anchor property regressed (stub the reader, never the anchor)"
            )
            assert db.name == ids.AUTH_SQLITE_DB
