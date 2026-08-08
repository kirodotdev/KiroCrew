"""The app's tests must never write to the operator's live data home.

A leak here is silent: the assertions of the test that leaks still pass, and the damage
shows up later as an app configured against a reaped temporary directory. These tests fail
if the autouse redirect in ``conftest.py`` is removed or stops covering a path, which is the
only signal the suite can give for damage that lands outside its own assertions.
"""

from __future__ import annotations

from pathlib import Path

from ..backend import store


class TestTheDataHomeIsRedirected:
    """Every store path a test can reach must resolve inside the test's temp dir."""

    def test_the_data_root_is_the_tests_temp_dir(self, tmp_path: Path) -> None:
        assert store.data_dir().is_relative_to(tmp_path), (
            f"store.data_dir() resolved to {store.data_dir()}, outside {tmp_path} — "
            "the autouse redirect in conftest is not in effect"
        )

    def test_the_config_path_is_the_tests_temp_dir(self, tmp_path: Path) -> None:
        # config.json is the file that actually got clobbered: it names the repository the
        # app is turned loose on, so a stray write retargets the operator's app.
        assert store.config_path().is_relative_to(tmp_path)

    def test_the_clone_scratch_root_is_the_tests_temp_dir(self, tmp_path: Path) -> None:
        # Clones are large and, once created, the app refuses to reuse them under a
        # different requested URL — so a leaked clone is not merely litter.
        assert store.scratch_dir().is_relative_to(tmp_path)

    def test_the_config_home_resolver_is_the_tests_temp_dir(self, tmp_path: Path) -> None:
        # The strongest of these: `config_dir()` is reached by code that never touches
        # `store` (`do_not_pollute_paths` is one), and on the default path it MUTATES the
        # operator's home — legacy migration, a recovery breadcrumb written outside
        # `~/.kiro/`, and a sweep that deletes archive leftovers. Redirecting `data_dir`
        # alone leaves that path pointed at the real home.
        from kiro_crew.config.loader import config_dir

        assert Path(config_dir()).is_relative_to(tmp_path)


class TestAProductionConfigWriteStaysInTheTempDir:
    """The exact shape that leaked: a test writing config through the production helper."""

    def test_write_json_atomic_to_config_path_lands_in_the_temp_dir(
        self, tmp_path: Path
    ) -> None:
        target = store.config_path()
        # Checked BEFORE writing, deliberately. A test that verifies isolation must not
        # depend on isolation to be harmless: asserting after the write would clobber the
        # operator's config on the very run where the redirect is broken — the failure it
        # exists to report.
        assert target.is_relative_to(tmp_path), (
            f"refusing to write {target}: outside {tmp_path}, so the redirect is broken"
        )
        store.write_json_atomic(target, {"clone": str(tmp_path / "clone"), "branch": "work"})
        assert target.is_file()
        assert (store.read_json(target, {}) or {}).get("branch") == "work"

    def test_the_redirect_is_per_test_so_writes_do_not_accumulate(
        self, tmp_path: Path
    ) -> None:
        # A fresh temp root per test is what keeps one test's config from being read as
        # another's fixture state; a session-scoped redirect would still isolate the
        # operator but let tests see each other's writes.
        assert not store.config_path().exists()
