"""``wrap_argv``'s synchronous SEL degrades must refuse inside a cron child.

Every "SEL audit failed ... proceeding unaudited" site in ``sandbox.py`` is a
deliberate availability-over-audit choice: refusing a spawn on an audit hiccup
would brick built-in tooling, in-sandbox MCP calls and every agent subprocess on
a backend-less host. That is right for the gateway and wrong for a detached cron
script child, which nobody is watching and whose ``ENOSYS`` audit failure means
its whole filesystem is gone -- it can persist nothing it goes on to do, while
its parent records the run as ok.

Two things are pinned: the nested-sandbox passthrough (the site a child that
outlived its sandbox reaches on Linux) behaves correctly in all three
combinations, and no site whose audit write can actually surface an ``ENOSYS`` is
left without the guard.
"""

from __future__ import annotations

import ast
import errno
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.sandbox import CRON_SCRIPT_CHILD_ENV, UnauditedSpawnRefused, reset_backend, wrap_argv

# Shares the subprocess_spawn group with test_sandbox_argv.py /
# test_sandbox_nested_tier.py: same module under test, same cached-backend
# globals. Requires --dist loadgroup.
pytestmark = pytest.mark.xdist_group(name="subprocess_spawn")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Isolate marker/level env vars, cached backend, and the one-shot log flag."""
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.delenv("KIROCREW_SANDBOX_LEVEL", raising=False)
    monkeypatch.delenv(CRON_SCRIPT_CHILD_ENV, raising=False)
    monkeypatch.setattr(
        "kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    had_flag = getattr(sandbox_mod.wrap_argv, "_nested_passthrough_logged", None)
    if had_flag is not None:
        delattr(sandbox_mod.wrap_argv, "_nested_passthrough_logged")
    reset_backend()
    yield
    if had_flag is not None:
        sandbox_mod.wrap_argv._nested_passthrough_logged = had_flag
    elif hasattr(sandbox_mod.wrap_argv, "_nested_passthrough_logged"):
        delattr(sandbox_mod.wrap_argv, "_nested_passthrough_logged")
    reset_backend()


@pytest.fixture
def inside_sandbox(monkeypatch):
    """Put ``wrap_argv`` on the nested-sandbox passthrough branch."""
    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "standard")
    monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
    # Keep the return value a verbatim passthrough: an empty scrub delta means no
    # `env -u` prefix, so the assertions below read the audit decision only.
    monkeypatch.setattr(sandbox_mod, "_sandbox_env_unset_args", lambda *a, **k: [])


class TestNestedPassthroughAuditFailure:
    """The one site a torn-down cron child reaches, in all three combinations."""

    @patch("kiro_crew.sandbox.detect_backend")
    def test_enosys_in_a_cron_child_refuses_the_spawn(
        self, mock_detect, inside_sandbox, monkeypatch
    ):
        monkeypatch.setenv(CRON_SCRIPT_CHILD_ENV, "1")
        with patch("kiro_crew.sel.sel", side_effect=OSError(errno.ENOSYS, "nope")):
            with pytest.raises(UnauditedSpawnRefused) as exc_info:
                wrap_argv(["git", "status"], mode="standard")

        assert "nested-sandbox passthrough audit" in str(exc_info.value)
        assert "ENOSYS" in str(exc_info.value)
        mock_detect.assert_not_called()

    @patch("kiro_crew.sandbox.detect_backend")
    def test_enosys_reached_through_the_cause_chain_refuses(
        self, mock_detect, inside_sandbox, monkeypatch
    ):
        """SEL wraps its writes, so the errno sits below the surface error."""
        monkeypatch.setenv(CRON_SCRIPT_CHILD_ENV, "1")
        try:
            raise OSError(errno.ENOSYS, "Function not implemented")
        except OSError as inner:
            wrapped = RuntimeError("SEL flush failed")
            wrapped.__cause__ = inner
        with patch("kiro_crew.sel.sel", side_effect=wrapped):
            with pytest.raises(UnauditedSpawnRefused):
                wrap_argv(["git", "status"], mode="standard")

    @patch("kiro_crew.sandbox.detect_backend")
    def test_enosys_in_the_gateway_still_proceeds(
        self, mock_detect, inside_sandbox, monkeypatch, caplog
    ):
        """Unchanged behaviour off the cron path: confinement over audit."""
        argv = ["git", "status"]
        with patch("kiro_crew.sel.sel", side_effect=OSError(errno.ENOSYS, "nope")):
            with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
                result, cleanup = wrap_argv(argv, mode="standard")

        assert result == argv
        assert cleanup is None
        assert any("proceeding " in r.message for r in caplog.records)

    @patch("kiro_crew.sandbox.detect_backend")
    def test_other_errno_in_a_cron_child_still_proceeds(
        self, mock_detect, inside_sandbox, monkeypatch
    ):
        """A permission or disk hiccup is what the best-effort posture is for."""
        monkeypatch.setenv(CRON_SCRIPT_CHILD_ENV, "1")
        argv = ["git", "status"]
        with patch("kiro_crew.sel.sel", side_effect=OSError(errno.EACCES, "denied")):
            result, cleanup = wrap_argv(argv, mode="standard")

        assert result == argv
        assert cleanup is None

    @patch("kiro_crew.sandbox.detect_backend")
    def test_a_healthy_audit_is_untouched(self, mock_detect, inside_sandbox, monkeypatch):
        monkeypatch.setenv(CRON_SCRIPT_CHILD_ENV, "1")
        argv = ["git", "status"]
        with patch("kiro_crew.sel.sel") as mock_sel:
            result, cleanup = wrap_argv(argv, mode="standard")

        assert result == argv
        assert cleanup is None
        assert mock_sel.return_value.log_tool_invocation.called


#: The one SEL-failure handler that is not a degrade: it refuses the delegation
#: and hands the spawn back to Kiro Crew's own audited policy -- macOS seatbelt,
#: or a fail-closed error on Windows -- so a cron child already gets a loud
#: failure there and needs no carve-out. Asserted to still match exactly one
#: handler, so it cannot quietly grow to cover a site that proceeds unaudited.
_REFUSES_INSTEAD_OF_DEGRADING = "refusing unaudited delegation"


def _sel_failure_handlers(*, critical_only: bool) -> dict[str, ast.ExceptHandler]:
    """SEL-failure ``except`` handlers in ``sandbox.py``, keyed by message text.

    ``critical_only`` keeps those whose audit write passes ``critical=True``.
    Those are the writes ``sel.log`` performs SYNCHRONOUSLY and raises on; the
    rest are enqueued to a background writer that swallows the error, so an
    ``ENOSYS`` can never reach a handler there and a guard would be dead code.
    """
    # encoding pinned: the module carries non-ASCII prose and the Windows locale
    # codec (cp1252) cannot decode it.
    source = Path(sandbox_mod.__file__).read_text(encoding="utf-8")
    found: dict[str, ast.ExceptHandler] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Try):
            continue
        critical = any(
            isinstance(inner, ast.keyword)
            and inner.arg == "critical"
            and isinstance(inner.value, ast.Constant)
            and inner.value.value is True
            for stmt in node.body
            for inner in ast.walk(stmt)
        )
        if critical_only and not critical:
            continue
        for handler in node.handlers:
            texts = [
                inner.value
                for inner in ast.walk(handler)
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
            ]
            joined = " ".join(texts)
            if "SEL audit failed" in joined:
                found[joined] = handler
    return found


class TestNoReachableSiteIsLeftBehind:
    """A guard on one branch of a chain is a guard the next round moves.

    So the invariant is asserted over the module: every SEL-failure handler whose
    write can actually raise must consult the cron-child rule first.
    """

    def test_every_synchronous_sel_handler_calls_the_guard(self):
        handlers = {
            key: node
            for key, node in _sel_failure_handlers(critical_only=True).items()
            if _REFUSES_INSTEAD_OF_DEGRADING not in key
            and not any(isinstance(inner, ast.Raise) for inner in ast.walk(node))
        }
        assert handlers, "no synchronous SEL degrade handlers found -- wording changed?"
        missing = [
            key[:80]
            for key, node in handlers.items()
            if not any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "refuse_unaudited_on_dead_fs"
                for inner in ast.walk(node)
            )
        ]
        assert not missing, f"synchronous SEL degrade sites with no cron-child guard: {missing}"

    @pytest.mark.parametrize("site", ["mode=off delegation", "nested-sandbox passthrough"])
    def test_each_known_site_is_covered(self, site):
        """Names the sites, so a silently DELETED handler is caught too."""
        joined = " ".join(_sel_failure_handlers(critical_only=True))
        assert site in joined, f"the {site!r} SEL degrade site is no longer recognised"

    def test_the_carve_out_covers_exactly_one_refusing_handler(self):
        """The exclusion must stay as narrow as its reason."""
        refusing = [
            key
            for key in _sel_failure_handlers(critical_only=True)
            if _REFUSES_INSTEAD_OF_DEGRADING in key
        ]
        assert len(refusing) == 1, (
            "the 'refuses instead of degrading' carve-out now matches "
            f"{len(refusing)} handlers: {[k[:60] for k in refusing]}"
        )

    def test_the_guard_is_called_from_production_code(self):
        """A guard with no non-test caller is a fix that never runs."""
        src = Path(sandbox_mod.__file__).parent
        callers = [
            path.name
            for path in src.rglob("*.py")
            if "refuse_unaudited_on_dead_fs(" in path.read_text(encoding="utf-8", errors="replace")
        ]
        assert "sandbox.py" in callers


def test_the_cron_marker_is_not_set_in_this_process():
    """Guard: the suite itself must not look like a cron child."""
    assert os.environ.get(CRON_SCRIPT_CHILD_ENV) is None
