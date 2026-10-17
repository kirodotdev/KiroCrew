"""Packaged docs (troubleshooting..workflows) state facts that match the code.

Each test reads one shipped doc under ``src/kiro_crew/docs`` and checks a fact
against the constant that defines it, so a later code change that moves the
fact fails here instead of leaving a running agent reading stale text.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "docs"


def _doc(name: str) -> str:
    """The doc with every whitespace run folded to one space, so wrapping is moot."""
    return re.sub(r"\s+", " ", (DOCS / name).read_text(encoding="utf-8"))


def test_turn_ceiling_entry_matches_the_code():
    from kiro_crew.messaging import turn_ceiling as tc

    text = _doc("troubleshooting.md")
    assert tc.REFUSAL_TEXT.startswith("This conversation hit its turn limit and is paused")
    assert '"This conversation hit its turn limit and is paused"' in text
    assert f"The default is {tc.DEFAULT_MAX_TURNS} turns per hour" in text
    assert tc.DEFAULT_WINDOW_SECS == 3600.0
    assert f"`{tc.ENV_MAX_TURNS}`" in text
    assert f"`{tc.ENV_WINDOW_SECS}`" in text


def test_restart_ready_timeout_entry_matches_the_code():
    from kiro_crew import cli_server

    text = _doc("troubleshooting.md")
    assert f"default {cli_server._RESTART_READY_TIMEOUT_DEFAULT} seconds" in text
    lo, hi = cli_server._RESTART_READY_TIMEOUT_MIN, cli_server._RESTART_READY_TIMEOUT_MAX
    assert f"clamped to {lo}–{hi}" in text
    assert "`KIROCREW_RESTART_READY_TIMEOUT`" in text


def test_stdlib_shadow_entry_matches_the_code():
    from kiro_crew import stdlib_shadow

    text = _doc("troubleshooting.md")
    assert f"exits with status {stdlib_shadow.SHADOW_EXIT_STATUS}" in text
    assert '"Refusing to start: the Python standard library is shadowed."' in text


def test_speech_and_run_dir_markers_match_the_code():
    from kiro_crew.doctor_checks.resources import _RUN_DIR_BACKLOG_WARN
    from kiro_crew.session_work_dir import RUN_DIR_MARKER
    from kiro_crew.stt.preflight import LOAD_MARKER_NAME

    text = _doc("troubleshooting.md")
    assert f"`{LOAD_MARKER_NAME}`" in text
    assert f"carry no {RUN_DIR_MARKER} marker" in text
    assert f"above {_RUN_DIR_BACKLOG_WARN} the row becomes a warning" in text


def test_build_failures_install_the_dev_extra():
    text = _doc("troubleshooting.md")
    assert 'pip install -e ".[dev]"' in text
    assert "pip install -e . &&" not in text


def test_wecom_outbound_file_ceilings_match_the_code():
    from kiro_crew.wecom.media_upload import MAX_BYTES_BY_TYPE

    text = _doc("wecom-integration.md")
    assert "not wired up yet, so if the agent produces an image" not in text
    assert MAX_BYTES_BY_TYPE["image"] == MAX_BYTES_BY_TYPE["voice"] == 2_000_000
    assert MAX_BYTES_BY_TYPE["file"] == MAX_BYTES_BY_TYPE["video"] == 20_000_000
    assert "2 MB per image or voice note, 20 MB per file or video" in text


def test_weixin_doc_has_no_messaging_extra():
    text = _doc("weixin-integration.md")
    assert "messaging extra" not in text
    assert "`weixin_transport.authorize`" in text
    assert "`weixin.attachment_skip`" in text


def test_whatsapp_help_row_lists_every_alias():
    from kiro_crew.whatsapp.commands import COMMANDS

    help_cmd = next(c for c in COMMANDS if c.aliases[0] == "/help")
    row = (
        "| " + " (or ".join(f"`{a}`" for a in help_cmd.aliases) + ")" * (len(help_cmd.aliases) > 1)
    )
    assert row in _doc("whatsapp-integration.md")


def test_work_ledger_refusal_codes_exist():
    from kiro_crew import work_ledger
    from kiro_crew.dashboard.handlers import work_ledger as handler

    text = _doc("work-ledger.md")
    for code in ("crew_log_unrecorded", "work_entry_too_large", "work_item_too_large"):
        assert code in handler.ROUTE_CODES, code
        assert f" {code}` |" in text, code
    assert handler._CODE_STATUS[work_ledger.CODE_CACHE_DIRTY] == 409
    assert handler._CODE_STATUS[work_ledger.CODE_CREW_LOG_INCOMPLETE] == 409
    assert f"`409 {work_ledger.CODE_CACHE_DIRTY}`" in text
    assert f"`409 {work_ledger.CODE_CREW_LOG_INCOMPLETE}`" in text
    assert "## No dashboard page" not in (DOCS / "work-ledger.md").read_text(encoding="utf-8")
