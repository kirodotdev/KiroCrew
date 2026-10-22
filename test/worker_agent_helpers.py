"""Fixtures, pins and spec shapes shared by the ``kirocrew-worker`` agent tests.

``test_worker_agent.py`` and its ``_spawn_gate`` and ``_load_bracket`` siblings
import these by name. ``_accepting_kiro_cli`` is autouse, so every one of them pins
an accepting kiro-cli.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from source_corpus import iter_source_texts

from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

#: A release that accepts a spec ``permissions`` block, and one that refuses it,
#: expressed against the floor so raising it cannot strand these tests.
_ACCEPTS = SPEC_PERMISSIONS_MIN_VERSION
_REFUSES = (SPEC_PERMISSIONS_MIN_VERSION[0], SPEC_PERMISSIONS_MIN_VERSION[1] - 1, 0)


def _pin_spec_permissions_cli(monkeypatch, which):
    """Pin what the shared writer gate believes the installed kiro-cli is.

    ``_write_derived_permissions`` reads ``installed_kiro_cli_version``
    function-locally from ``kiro_crew.kiro_cli``, so the patch lands there.
    ``which`` is ``"accepts"``, ``"refuses"`` or ``"unknown"``.
    """
    version = {"accepts": _ACCEPTS, "refuses": _REFUSES, "unknown": None}[which]
    monkeypatch.setattr("kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: version)


def _package_sources() -> Iterator[tuple[Path, str]]:
    """Every ``kiro_crew`` module except ``agent.py``, streamed off the shared corpus.

    Four enumeration tests in ``test_worker_agent_spawn_gate.py`` and
    ``test_worker_agent_load_bracket.py`` reason over the package. Streamed, not
    collected: the text of the ~1,700 modules under ``src/`` is ~130 MB of ``str`` (a
    quarter of the files hold an emoji, which makes CPython store the whole file at four
    bytes per code point), and a tuple holding all of it was the worker tests' entire
    +229 MiB high-water mark. ``iter_source_texts`` keeps one file live at a time, and the
    gates' rule-shaped assertions only ever look at one file at a time anyway. The
    ``agent.py`` exclusion stays in the gates' own helper rather than in the shared
    ``source_corpus`` one, because which files a gate polices is that gate's contract
    (and ``agent.py``, binding every name these gates hunt for, would match them all).
    """
    for path, text in iter_source_texts():
        if path.name != "agent.py":
            yield path, text


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_worker_agent*.py modules calls monkeypatch.undo()
def _accepting_kiro_cli(monkeypatch):
    """Pin an ACCEPTING kiro-cli for every worker test by default.

    The worker writer now gates its ``permissions`` write on the installed
    kiro-cli, like the conductors and the default spec. Almost every worker test
    asserts the derived block, and CI's absent binary reads as "unknown" and
    withholds it -- so without this pin those assertions would fail for a host
    reason rather than a code one. The one test that exercises the gate itself
    re-pins a refusing and an unknown version over this default.
    """
    _pin_spec_permissions_cli(monkeypatch, "accepts")


#: A default agent spec shaped the way a real one is after a user has used the
#: product: servers an app registration and the dashboard mounted, whole-server
#: grants for them, and a model pinned through ``config.json``. None of it reaches
#: ``build_agent_config``, which composes the shipped template with the override
#: file only — which is exactly why the worker mirrors the FILE.
_DEFAULT_SPEC_ON_DISK: dict[str, Any] = {
    "name": "kirocrew",
    "model": "claude-opus-5",
    "tools": [
        "execute_bash",
        "fs_read",
        "fs_write",
        "@kirocrew-core",
        "@kirocrew-cron",
        "@kirocrew-dashboard",
        "@builder-mcp",
        "@pdf",
    ],
    "allowedTools": [
        "fs_read",
        "@kirocrew-cron",
        "@kirocrew-dashboard/session_create",
        "@builder-mcp",
        "@pdf",
    ],
    "mcpServers": {
        "kirocrew-core": {"command": "kirocrew", "args": ["mcp-core"]},
        "kirocrew-cron": {"command": "kirocrew", "args": ["mcp-cron"]},
        "kirocrew-dashboard": {"command": "kirocrew", "args": ["mcp-dashboard"]},
        "builder-mcp": {"command": "builder-mcp", "args": []},
        "pdf": {"command": "uvx", "args": ["pdf-mcp"]},
    },
}


def _stale_worker_spec() -> dict[str, Any]:
    """A worker spec as an older build left it: template-only, and out of date."""
    return {
        "name": "kirocrew-worker",
        "description": "an older build's description",
        "prompt": "an older build's prompt",
        "model": "auto",
        "tools": ["fs_read", "@kirocrew-core", "@kirocrew-work"],
        "allowedTools": ["fs_read", "@kirocrew-work/work_brief"],
        "mcpServers": {"kirocrew-core": {"command": "kirocrew", "args": ["mcp-core"]}},
    }
