"""Tests for startup handling of inherited llama.cpp library paths."""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

import pytest

from kiro_crew import _llama_lib_path as llama_lib_path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BUNDLED_PLATFORMS = (
    "linux_x86_64",
    "linux_aarch64",
    "macos_arm64",
    "macos_x86_64",
    "win_amd64",
)
_ENTRYPOINTS = (
    _REPO_ROOT / "src" / "kiro_crew" / "__main__.py",
    _REPO_ROOT / "src" / "kiro_crew" / "cli.py",
    _REPO_ROOT / "src" / "kiro_crew" / "mcp_gateway" / "gatewayd.py",
)


@pytest.fixture(autouse=True)
def _clear_startup_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the process-wide startup record local to each test.

    Both halves: the value doctor reads, and the flag that decides whether the
    one INFO line is still owed.
    """
    monkeypatch.setattr(llama_lib_path, "_dropped_inherited", None)
    monkeypatch.setattr(llama_lib_path, "_dropped_inherited_unreported", False)


class TestOperatorLibPathOverride:
    @pytest.mark.parametrize(
        "value",
        [
            "/opt/my-gpu-llama",
            "/srv/llama.cpp/build/bin",
            "/srv/kiro_crew/_vendor/llama_cpp_libs",
            "/srv/kiro_crew/_vendor/llama_cpp_libs/ppc64le",
            "/srv/other_pkg/_vendor/llama_cpp_libs/linux_x86_64",
            "/srv/kiro_crew/vendor/llama_cpp_libs/linux_x86_64",
        ],
    )
    def test_an_operator_directory_is_an_override(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, value)
        assert llama_lib_path.operator_lib_path_override() == value

    @pytest.mark.parametrize("platform_name", _BUNDLED_PLATFORMS)
    def test_a_bundled_directory_of_any_install_is_not_an_override(
        self, monkeypatch: pytest.MonkeyPatch, platform_name: str
    ) -> None:
        elsewhere = (
            Path("/srv/kirocrew/0.7.9/lib/python3.12/site-packages")
            / "kiro_crew"
            / "_vendor"
            / llama_lib_path._LIBS_DIR_NAME
            / platform_name
        )
        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, str(elsewhere))
        assert llama_lib_path.operator_lib_path_override() is None

        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, str(elsewhere) + os.sep)
        assert llama_lib_path.operator_lib_path_override() is None

    def test_unset_or_empty_is_not_an_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(llama_lib_path._LIB_PATH_ENV, raising=False)
        assert llama_lib_path.operator_lib_path_override() is None

        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, "")
        assert llama_lib_path.operator_lib_path_override() is None


class TestDropInheritedLibPath:
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "/srv/kirocrew/site-packages/kiro_crew/_vendor/" "llama_cpp_libs/linux_x86_64",
        ],
    )
    def test_bundled_and_empty_values_are_removed_and_recorded(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
    ) -> None:
        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, value)

        with caplog.at_level(logging.DEBUG):
            removed = llama_lib_path.drop_inherited_lib_path()

        assert removed == value
        assert llama_lib_path._LIB_PATH_ENV not in os.environ
        assert llama_lib_path.dropped_inherited_lib_path() == value
        # Silent: the prelude runs before logging is configured, so it records
        # the removal and leaves the one INFO line to the loader. Asserted at
        # DEBUG so a record emitted here would be caught whatever its level.
        assert caplog.records == []
        assert llama_lib_path._dropped_inherited_unreported is True

    def test_operator_override_is_left_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        override = "/srv/operator/llama-libs"
        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, override)

        assert llama_lib_path.drop_inherited_lib_path() is None
        assert os.environ[llama_lib_path._LIB_PATH_ENV] == override
        assert llama_lib_path.dropped_inherited_lib_path() is None

    def test_repeated_drop_is_a_noop_and_preserves_the_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inherited = "/srv/kirocrew/site-packages/kiro_crew/_vendor/" "llama_cpp_libs/macos_arm64"
        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, inherited)

        assert llama_lib_path.drop_inherited_lib_path() == inherited
        assert llama_lib_path.drop_inherited_lib_path() is None
        assert llama_lib_path.dropped_inherited_lib_path() == inherited


class TestConsumeDroppedInheritedLibPath:
    """The hand-over that turns a silent record into exactly one line."""

    def test_the_record_is_handed_over_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inherited = "/srv/kirocrew/site-packages/kiro_crew/_vendor/" "llama_cpp_libs/win_amd64"
        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, inherited)
        llama_lib_path.drop_inherited_lib_path()

        assert llama_lib_path.consume_dropped_inherited_lib_path() == inherited
        assert llama_lib_path.consume_dropped_inherited_lib_path() is None
        # Consuming must not disturb what doctor reads.
        assert llama_lib_path.dropped_inherited_lib_path() == inherited

    def test_a_removed_empty_value_is_not_mistaken_for_no_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`""` is a removal worth reporting; only None means nothing pending.

        A truthiness test here would silently drop the empty-value line, which
        is the one case where the variable's own value says nothing.
        """
        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, "")
        llama_lib_path.drop_inherited_lib_path()

        assert llama_lib_path.consume_dropped_inherited_lib_path() == ""
        assert llama_lib_path.consume_dropped_inherited_lib_path() is None

    def test_nothing_is_pending_when_nothing_was_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(llama_lib_path._LIB_PATH_ENV, raising=False)
        assert llama_lib_path.drop_inherited_lib_path() is None
        assert llama_lib_path.consume_dropped_inherited_lib_path() is None


class TestTheRemovalIsReportedByTheLoader:
    """The one INFO line the prelude cannot emit, emitted where logging exists.

    The prelude runs with the root logger at WARNING and no handlers, so the
    line has to come from the first reader that runs after logging is
    configured. These drive the real loader entry point rather than the helper
    alone, so the call site is pinned too.
    """

    @staticmethod
    def _lib_path_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if llama_lib_path._LIB_PATH_ENV in record.getMessage()
        ]

    @staticmethod
    def _run_loader(monkeypatch: pytest.MonkeyPatch) -> None:
        """Run ``_load_llama_class`` as far as reporting, and no further.

        An unsupported platform returns before any native work, so this
        exercises the real entry point without loading libs. The cache is
        cleared on both sides: ``lru_cache`` would otherwise skip the body on
        the second run and hide whether the guard is the thing stopping the
        line from repeating.
        """
        import kiro_crew.embeddings as emb

        monkeypatch.setattr(emb, "_platform_libs_dirname", lambda: None)
        emb._load_llama_class.cache_clear()
        try:
            assert emb._load_llama_class() is None
        finally:
            emb._load_llama_class.cache_clear()

    def test_the_loader_logs_the_removed_value_once_and_not_again(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import kiro_crew.embeddings  # noqa: F401  (import noise before capture)

        inherited = (
            "/srv/kirocrew/0.7.9/lib/python3.12/site-packages/kiro_crew/_vendor/"
            "llama_cpp_libs/linux_x86_64"
        )
        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, inherited)

        with caplog.at_level(logging.INFO):
            assert llama_lib_path.drop_inherited_lib_path() == inherited
            assert self._lib_path_records(caplog) == []

            self._run_loader(monkeypatch)
            reported = self._lib_path_records(caplog)

        assert len(reported) == 1
        assert reported[0].levelno == logging.INFO
        assert reported[0].name == "kiro_crew.embeddings"
        assert llama_lib_path._LIB_PATH_ENV in reported[0].getMessage()
        assert inherited in reported[0].getMessage()
        # Still there for `kirocrew doctor`, which reads it after the line.
        assert llama_lib_path.dropped_inherited_lib_path() == inherited

        caplog.clear()
        with caplog.at_level(logging.INFO):
            self._run_loader(monkeypatch)
        assert self._lib_path_records(caplog) == []
        assert llama_lib_path.dropped_inherited_lib_path() == inherited

    def test_a_removed_empty_value_is_reported_too(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import kiro_crew.embeddings  # noqa: F401  (import noise before capture)

        monkeypatch.setenv(llama_lib_path._LIB_PATH_ENV, "")
        assert llama_lib_path.drop_inherited_lib_path() == ""

        with caplog.at_level(logging.INFO):
            self._run_loader(monkeypatch)
            reported = self._lib_path_records(caplog)

        assert len(reported) == 1
        assert "empty" in reported[0].getMessage()

    def test_an_untouched_environment_reports_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import kiro_crew.embeddings  # noqa: F401  (import noise before capture)

        monkeypatch.delenv(llama_lib_path._LIB_PATH_ENV, raising=False)
        assert llama_lib_path.drop_inherited_lib_path() is None

        with caplog.at_level(logging.INFO):
            self._run_loader(monkeypatch)

        assert self._lib_path_records(caplog) == []


def _top_level_call_name(node: ast.stmt) -> str | None:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return None
    function = node.value.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS, ids=lambda path: path.name)
def test_entry_prelude_drops_the_inherited_path_immediately_after_ssl_setup(
    entrypoint: Path,
) -> None:
    body = ast.parse(entrypoint.read_text(encoding="utf-8"), filename=str(entrypoint)).body
    ssl_index = next(
        index
        for index, node in enumerate(body)
        if _top_level_call_name(node) == "_ensure_ssl_certs"
    )
    assert _top_level_call_name(body[ssl_index + 1]) == "drop_inherited_lib_path"


def test_startup_module_imports_only_the_standard_library() -> None:
    source = Path(llama_lib_path.__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported_modules = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    )
    # No `logging` either: the prelude cannot usefully log (nothing is
    # configured yet), so the leaf records the removal and the loader reports
    # it. Importing logging here would invite a line that reaches no handler.
    assert imported_modules == {"__future__", "os", "pathlib"}
