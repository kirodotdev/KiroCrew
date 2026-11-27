"""No config_dir() inside async functions.

config_dir() runs maintenance (mkdir, breadcrumb refresh, a shutil.rmtree sweep) on
every call, which would block the event loop. Async code uses data_home().

The scan reads every module under ``SRC``, so a new module is checked without being
registered anywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

pytestmark = pytest.mark.xdist_group(name="tree_scan_no_config_dir_in_async")

#: Ratchet: the event-loop functions that still call config_dir(), as
#: ``"<path under kiro_crew/>::<function>": <call count>``. A new site, or a further
#: call in a listed function, fails; a fixed site fails until its line is lowered or
#: deleted. The record may only shrink.
_KNOWN_ASYNC_CONFIG_DIR: dict[str, int] = {
    "apps/builtin_adoption_apply.py::_stage_one": 1,
    "apps/job_sdk.py::remove_all_async": 1,
    "dashboard/chat_folders_records.py::migrate_folder_member_labels": 2,
    "dashboard/chat_manual_order.py::_write_manual_order": 1,
    "dashboard/chat_manual_order.py::ensure_manual_order_loaded": 1,
    "dashboard/handlers/agent_templates.py::api_agent_template_delete": 1,
    "dashboard/handlers/browser_harness_status.py::_run": 1,
    "dashboard/handlers/browser_harness_status.py::auto_setup_browser_harness": 1,
    "dashboard/handlers/browser_ops.py::_supervisor": 1,
    "dashboard/handlers/connections.py::_mutate": 1,
    "dashboard/handlers/connections.py::_write_oauth_client": 1,
    "dashboard/handlers/connections.py::api_connections_oauth_client_delete": 1,
    "dashboard/handlers/cron_secret_grants.py::_promote_pending_grant": 1,
    "dashboard/handlers/secrets.py::api_secrets_delete": 1,
    "dashboard/handlers/secrets.py::api_secrets_list": 1,
    "dashboard/handlers/secrets.py::api_secrets_set": 1,
    "dashboard/handlers/secrets_usage.py::api_secrets_usage": 1,
    "dashboard/state_parts/sidebar_stores.py::mutate_folders": 1,
}


def _takes_the_turn(node: ast.AST) -> bool:
    """A function cut from the dashboard turn's coroutine takes the turn first: its body ran
    inside ``_run_chat``, on the event loop, so the async rule reads it as it read that
    coroutine."""
    return (
        isinstance(node, ast.FunctionDef)
        and bool(node.args.args)
        and node.args.args[0].arg == "turn"
    )


def _sites_in(rel: str, tree: ast.AST) -> dict[str, list[int]]:
    """``"<rel>::<function>"`` -> config_dir() call lines, for each event-loop function."""
    sites: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) or _takes_the_turn(node)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                if name == "config_dir":
                    sites.setdefault(f"{rel}::{node.name}", []).append(sub.lineno)
    return sites


def _async_config_dir_sites() -> dict[str, list[int]]:
    """Every event-loop function under ``SRC`` that calls config_dir()."""
    sites: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "config_dir" in text:
            sites.update(_sites_in(path.relative_to(SRC).as_posix(), ast.parse(text)))
    return sites


class TestNoConfigDirInAsync:
    """config_dir() must not be called inside async functions."""

    def test_update_layout_channel_helpers_never_maintain(self) -> None:
        """The channel read/write must use ``data_home()``, not ``config_dir()``, and no
        event-loop function in the package adds a config_dir() call.

        The channel helpers are not ``async def``, but async handlers call
        ``release_channel()`` and ``set_release_channel()`` (``POST
        /api/update/channel``).
        """
        tree = ast.parse((SRC / "platform" / "update_layout.py").read_text(encoding="utf-8"))
        # AST, not a substring scan: the module's docstrings NAME config_dir to
        # explain why it is the wrong helper here, and a text match would flag
        # exactly the comment that documents the fix.
        called = {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        imported = {
            alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for alias in n.names
        }
        assert "config_dir" not in called and "config_dir" not in imported, (
            "update_layout.py must resolve the data home with data_home(); "
            "config_dir() re-runs start-of-process maintenance on every call and "
            "these helpers are reached from async request handlers"
        )
        assert "data_home" in called, "the channel helpers must resolve a path at all"

        offenders = [
            f"{site.split('::')[0]}:{line} in async {site.split('::')[1]}()"
            for site, lines in sorted(_async_config_dir_sites().items())
            if len(lines) > _KNOWN_ASYNC_CONFIG_DIR.get(site, 0)
            for line in lines
        ]
        assert not offenders, (
            "config_dir() called inside an async function or a function cut from the "
            "dashboard turn, which runs its maintenance on the event loop. "
            "Use data_home() instead:\n  " + "\n  ".join(offenders)
        )

    def test_the_scan_reads_the_package(self) -> None:
        sites = _async_config_dir_sites()
        assert sites, "the scan found no config_dir() call at all; it is reading nothing"

    def test_the_ratchet_only_shrinks(self) -> None:
        sites = _async_config_dir_sites()
        fixed = sorted(
            f"{site} ({len(sites.get(site, []))} < {count})"
            for site, count in _KNOWN_ASYNC_CONFIG_DIR.items()
            if len(sites.get(site, [])) < count
        )
        assert not fixed, (
            "these _KNOWN_ASYNC_CONFIG_DIR sites call config_dir() on the event loop "
            f"fewer times now; lower or delete their lines: {fixed}"
        )
