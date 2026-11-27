"""The provisioner's ``requirements.txt`` refusal names the condition that fired.

An app author reads the refusal to learn what to fix, so a file over the size cap
must not be described as an escaping symlink. The cause comes from
``requirements_verdict``, the one spelling of the rule ``requirements_in_tree`` applies.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import _record_runs

import kiro_crew.apps.backend as bmod
from conftest import requires_symlinks
from kiro_crew.apps.manifest import (
    REQUIREMENTS_TXT_MAX_BYTES,
    requirements_in_tree,
    requirements_verdict,
)


def _app_root(tmp_path: Path) -> Path:
    root = tmp_path / "app-root"
    root.mkdir()
    return root


def _provision(root: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any) -> tuple[str, list]:
    """Run the real provisioner on *root* and return its error and SEL events,
    asserting the refusal contract every case shares: pip never runs, and the
    ERROR log and the deps_provision_failed event fire once each."""
    events: list = []
    monkeypatch.setattr(
        bmod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
    )
    runs = _record_runs(monkeypatch)
    with caplog.at_level(logging.ERROR):
        err = bmod.provision_app_deps("demo", root)
    assert err.startswith("Refusing requirements.txt for app demo: "), err
    assert not any("install" in argv for argv in runs), runs
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [err]
    assert [e.get("outcome") for e in events] == ["deps_provision_failed"], events
    return err, events


def test_an_oversized_file_names_the_size_cap_not_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    root = _app_root(tmp_path)
    (root / "requirements.txt").write_bytes(b"#" + b"x" * (REQUIREMENTS_TXT_MAX_BYTES + 10))
    err, _ = _provision(root, monkeypatch, caplog)
    assert f"{REQUIREMENTS_TXT_MAX_BYTES}-byte size cap" in err, err
    assert "symlink" not in err, err


@requires_symlinks
def test_a_link_escaping_the_root_names_the_escape_and_no_outside_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    root = _app_root(tmp_path)
    outside = tmp_path / "outside-secret" / "req.txt"
    outside.parent.mkdir()
    outside.write_bytes(b"requests\n")
    (root / "requirements.txt").symlink_to(outside)
    err, _ = _provision(root, monkeypatch, caplog)
    assert "resolves outside the app directory" in err, err
    assert "size cap" not in err, err
    assert "outside-secret" not in err and str(tmp_path) not in err, err


def test_a_directory_says_it_is_not_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    root = _app_root(tmp_path)
    (root / "requirements.txt").mkdir()
    err, _ = _provision(root, monkeypatch, caplog)
    assert "not a readable regular file" in err, err


def _cause(root: Path, req: Path) -> str:
    return requirements_verdict(root, req)[1]


class TestRequirementsVerdict:
    """The cause agrees with the rule case by case: empty exactly when it accepts."""

    def test_an_accepted_file_has_no_cause(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements.txt"
        req.write_bytes(b"x" * REQUIREMENTS_TXT_MAX_BYTES)
        assert requirements_in_tree(tmp_path, req) is not None
        assert requirements_verdict(tmp_path, req) == (requirements_in_tree(tmp_path, req), "")

    def test_each_refusal_names_its_own_condition(self, tmp_path: Path) -> None:
        (tmp_path / "a-dir").mkdir()
        big = tmp_path / "big.txt"
        big.write_bytes(b"#" + b"x" * REQUIREMENTS_TXT_MAX_BYTES)
        cases = {
            tmp_path / "missing.txt": "missing",
            tmp_path / "a-dir": "not a regular file",
            big: "size cap",
        }
        for req, expected in cases.items():
            assert requirements_in_tree(tmp_path, req) is None, req
            assert expected in _cause(tmp_path, req), req

    @requires_symlinks
    def test_a_dangling_link_and_an_escaping_link_differ(self, tmp_path: Path) -> None:
        root = _app_root(tmp_path)
        dangling = root / "dangling.txt"
        dangling.symlink_to(root / "gone.txt")
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"requests\n")
        escaping = root / "escaping.txt"
        escaping.symlink_to(outside)
        assert "dangling symlink" in _cause(root, dangling)
        assert "outside the app directory" in _cause(root, escaping)
