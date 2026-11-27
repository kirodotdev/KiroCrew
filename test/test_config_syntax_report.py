"""A config file with a JSON syntax error is named at start, with its line and a fix.

Contract under test:
- ``find_config_syntax_errors`` names each of ``config.json`` and
  ``config.local.json`` that does not parse, with the parser's line and column
  and one fix: a missing comma names the line it belongs at the end of, a
  trailing comma the line it must leave, and on a line that goes on, the column;
- no text of the file reaches the report, so no credential on the broken line does;
- a broken overlay says ``config.json`` still applies;
- a file that parses, is absent, is not a regular file, or cannot be decoded
  reports nothing;
- the report leaves the broken file byte-identical;
- ``config_syntax_note.publish`` prints each error to stderr, keeps one critical
  ``system.agent`` bell note naming the file and the line, publishes nothing new
  for the same errors, and removes the note once the files parse.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from kiro_crew.config import loosening_provenance, syntax_report
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.dashboard.state_parts.notification_log import (
    _load_notifications,
    _persist_notification,
)
from kiro_crew.notifications import config_syntax_note as note_mod
from kiro_crew.notifications.bus import NotificationBus
from kiro_crew.testing.synthetic_credentials import SAMPLE_SLACK_BOT_TOKEN

MISSING_COMMA = (
    '{\n  "dashboard": {\n    "theme": "dark"\n    "url": "http://127.0.0.1:5476"\n  }\n}\n'
)
TRAILING_COMMA = '{\n  "dashboard": {\n    "theme": "dark",\n  }\n}\n'


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    base = tmp_path / "config.json"
    local = tmp_path / "config.local.json"
    monkeypatch.setattr(syntax_report, "config_path", lambda: base)
    monkeypatch.setattr(syntax_report, "config_local_path", lambda: local)
    # The host's sandbox verdict decides whether an endorsed copy is served; pin it off.
    monkeypatch.setattr(loosening_provenance, "enforcement_active", lambda: False)
    return base, local


def test_a_missing_comma_names_the_file_the_line_and_where_the_comma_goes(
    files: tuple[Path, Path],
) -> None:
    base, _local = files
    base.write_text(MISSING_COMMA, encoding="utf-8")

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.path == base
    assert (err.line, err.column) == (4, 5)
    assert err.message == "Expecting ',' delimiter"
    assert err.fix == "add a comma at the end of line 3"
    text = err.explain()
    assert str(base) in text
    assert "line 4, column 5" in text
    assert "add a comma at the end of line 3" in text
    assert "it will not save over the file until it parses again" in text
    # The interactive CLI writers reset a corrupt file on purpose; the report says so.
    # Pinned in test_config_overlay.py: test_config_set_file_replaces_corrupt_base.
    assert "but `kirocrew config set --file` replaces it." in text
    assert "--local" not in text
    assert err.backup is None
    assert "earlier copy" not in text


def test_a_backup_beside_the_file_is_named(files: tuple[Path, Path]) -> None:
    base, _local = files
    base.write_text(MISSING_COMMA, encoding="utf-8")
    backup = Path(str(base) + ".bak")
    backup.write_text("{}", encoding="utf-8")

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.backup == backup
    assert f"An earlier copy from a settings migration is at {backup}" in err.explain()


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('"slack": {"bot_token": "' + SAMPLE_SLACK_BOT_TOKEN + '"}', id="quoted"),
        pytest.param('"slack_bot_token": ' + SAMPLE_SLACK_BOT_TOKEN, id="unquoted"),
        pytest.param('"token": ' + SAMPLE_SLACK_BOT_TOKEN + '"', id="half-quoted"),
        pytest.param('"' + SAMPLE_SLACK_BOT_TOKEN + '": 1', id="followed-by-a-colon"),
        pytest.param(
            '"url": https://user:' + SAMPLE_SLACK_BOT_TOKEN + "@example.invalid/x",
            id="bare-url",
        ),
    ],
)
def test_no_text_of_the_offending_line_reaches_the_report(
    files: tuple[Path, Path], line: str
) -> None:
    base, _local = files
    base.write_text('{\n  "a": 1\n  ' + line + "\n}\n", encoding="utf-8")

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.line == 3
    assert SAMPLE_SLACK_BOT_TOKEN not in err.explain()
    assert SAMPLE_SLACK_BOT_TOKEN not in note_mod.note_body([err])


@pytest.mark.parametrize("overlay", [False, True], ids=["config.json", "config.local.json"])
def test_a_served_endorsed_copy_says_the_approved_settings_apply(
    files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, overlay: bool
) -> None:
    base, local = files
    broken = local if overlay else base
    base.write_text("{}", encoding="utf-8")
    broken.write_text(MISSING_COMMA, encoding="utf-8")
    monkeypatch.setattr(loosening_provenance, "enforcement_active", lambda: True)
    monkeypatch.setattr(loosening_provenance, "endorsed_bytes", lambda leaf: b'{"a": 1}')

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.path == broken
    assert err.endorsed is True
    text = err.explain()
    assert "running on the last settings it approved" in text
    assert "Settings > Security" in text
    assert "kirocrew config pending" in text
    assert "default settings" not in text
    assert "then restart Kiro Crew" not in text
    assert "add a comma at the end of line 3" in text


@pytest.mark.parametrize(
    "endorsed",
    [
        pytest.param(None, id="no-copy"),
        pytest.param(b"{", id="copy-does-not-parse"),
        pytest.param(OSError("unreadable"), id="copy-unreadable"),
    ],
)
def test_without_a_usable_endorsed_copy_the_report_says_defaults(
    files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    endorsed: bytes | OSError | None,
) -> None:
    base, _local = files
    base.write_text(MISSING_COMMA, encoding="utf-8")

    def _endorsed(leaf: str) -> bytes | None:
        if isinstance(endorsed, OSError):
            raise endorsed
        return endorsed

    monkeypatch.setattr(loosening_provenance, "enforcement_active", lambda: True)
    monkeypatch.setattr(loosening_provenance, "endorsed_bytes", _endorsed)

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.endorsed is False
    assert "running on default settings" in err.explain()


def test_a_trailing_comma_names_the_line_it_must_leave(files: tuple[Path, Path]) -> None:
    base, _local = files
    base.write_text(TRAILING_COMMA, encoding="utf-8")

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.fix == "remove the comma at the end of line 3"


@pytest.mark.parametrize(
    ("content", "fix"),
    [
        pytest.param(
            '{"dashboard":{"theme":"dark" "auto_open_browser":false}}',
            "add a comma on line 1 just before column 30",
            id="missing-comma",
        ),
        pytest.param(
            '{"dashboard":{"theme":"dark",}}',
            "remove the comma on line 1 at column 29",
            id="trailing-comma",
        ),
    ],
)
def test_on_a_one_line_file_the_fix_names_the_column(
    files: tuple[Path, Path], content: str, fix: str
) -> None:
    base, _local = files
    base.write_text(content, encoding="utf-8")

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.fix == fix


@pytest.mark.parametrize(
    ("content", "fix"),
    [
        pytest.param(
            '{\n  "dashboard": {"theme_mode": "dark"}\n',
            "correct the JSON on line 3 at column 1",
            id="missing-final-brace",
        ),
        pytest.param(
            '{\n  "a": [1, 2}\n}\n',
            "correct the JSON on line 2 at column 13",
            id="mismatched-bracket",
        ),
        pytest.param(
            '{\n  "a": 1,\n  b: 2\n}\n',
            "correct the JSON on line 3 at column 3",
            id="unquoted-key",
        ),
        pytest.param(
            "{\n  \"a\": 1,\n  'b': 2\n}\n",
            "correct the JSON on line 3 at column 3",
            id="single-quoted-key",
        ),
        pytest.param("[1, foo]", "correct the JSON on line 1 at column 5", id="bare-word"),
    ],
)
def test_no_comma_fix_where_a_comma_is_not_the_mistake(
    files: tuple[Path, Path], content: str, fix: str
) -> None:
    base, _local = files
    base.write_text(content, encoding="utf-8")

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.fix == fix


def test_a_trailing_comma_in_an_array_names_the_line_it_must_leave(
    files: tuple[Path, Path],
) -> None:
    base, _local = files
    base.write_text('{\n  "a": [\n    1,\n  ]\n}\n', encoding="utf-8")

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.fix == "remove the comma at the end of line 3"


def test_the_overlay_is_reported_after_the_base(files: tuple[Path, Path]) -> None:
    base, local = files
    base.write_text(MISSING_COMMA, encoding="utf-8")
    local.write_text("{", encoding="utf-8")

    errors = syntax_report.find_config_syntax_errors()

    assert [e.path for e in errors] == [base, local]
    assert errors[1].fix == "correct the JSON on line 1 at column 2"
    assert "running on default settings" in errors[0].explain()
    assert "running on config.json alone" not in errors[0].explain()


def test_a_broken_overlay_says_config_json_still_applies(files: tuple[Path, Path]) -> None:
    base, local = files
    base.write_text("{}", encoding="utf-8")
    local.write_text(MISSING_COMMA, encoding="utf-8")

    (err,) = syntax_report.find_config_syntax_errors()

    assert err.path == local
    assert err.overlay is True
    text = err.explain()
    assert "ignoring the overrides in this file and running on config.json alone" in text
    assert "default settings" not in text
    # Pinned in test_config_overlay.py: test_local_set_handles_corrupt_existing_file.
    assert "but `kirocrew config set --local` replaces it." in text
    assert "--file" not in text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_a_fifo_in_place_of_the_file_is_refused_not_read(files: tuple[Path, Path]) -> None:
    # A FIFO with no writer would block a plain read forever.
    base, _local = files
    os.mkfifo(base)

    assert syntax_report.find_config_syntax_errors() == []


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="absent"),
        pytest.param(b'{"dashboard": {"theme": "dark"}}', id="valid"),
        pytest.param(b"\xef\xbb\xbf{}", id="valid-with-bom"),
        pytest.param(b"\xff\xfe{", id="not-utf8"),
    ],
)
def test_reports_nothing(files: tuple[Path, Path], content: bytes | None) -> None:
    base, _local = files
    if content is not None:
        base.write_bytes(content)

    assert syntax_report.find_config_syntax_errors() == []


def test_the_broken_file_is_left_as_it_was(files: tuple[Path, Path]) -> None:
    base, _local = files
    base.write_text(MISSING_COMMA, encoding="utf-8")

    syntax_report.find_config_syntax_errors()

    assert base.read_text(encoding="utf-8") == MISSING_COMMA
    assert sorted(p.name for p in base.parent.iterdir()) == ["config.json"]


def test_no_config_writer_saves_over_the_broken_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What the report promises: a degraded load followed by a settings save (the
    # theme change of the onboarding story) leaves the broken bytes, and no backup.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    path = config_path()
    assert path.parent == tmp_path
    path.write_text(MISSING_COMMA, encoding="utf-8")

    cfg = KiroCrewConfig.load()
    cfg.dashboard.theme_mode = "light"
    with pytest.raises(ConfigReadError):
        cfg.save()
    with pytest.raises(ConfigReadError):
        update_config_locked(path, mutate=lambda doc: {**doc, "x": 1})

    assert path.read_text(encoding="utf-8") == MISSING_COMMA
    assert not Path(str(path) + ".bak").exists()
    (err,) = syntax_report.find_config_syntax_errors()
    assert err.path == path


# ── the start report ──


class _State:
    """What the note publisher reads off the dashboard state."""

    def __init__(self) -> None:
        self._notification_log: list[dict] = []
        self.notification_bus = NotificationBus(sink=self._deliver)
        self.last_notification_persist: asyncio.Future[bool] | None = None
        self.dismissed = 0

    def _deliver(self, note: dict) -> None:
        self._notification_log.append(note)
        done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        done.set_result(True)
        self.last_notification_persist = done

    async def dismiss_notification(self, *, source: str, key: str) -> tuple[bool, bool]:
        self.dismissed += 1
        held = [
            n for n in self._notification_log if n.get("source") == source and n.get("key") == key
        ]
        self._notification_log[:] = [n for n in self._notification_log if n not in held]
        return bool(held), True


@pytest.mark.asyncio
async def test_a_start_prints_the_error_and_rings_the_bell_once(
    files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    base, _local = files
    base.write_text(MISSING_COMMA, encoding="utf-8")
    state = _State()

    assert await note_mod.publish(state) is True

    err_out = capsys.readouterr().err
    assert str(base) in err_out
    assert "add a comma at the end of line 3" in err_out
    (note,) = state._notification_log
    assert note["channel"] == "system.agent"
    assert note["priority"] == "critical"
    assert note["key"] == note_mod.KEY
    assert note["title"] == "config.json has a syntax error and is ignored"
    assert str(base) in note["body"]
    assert "line 4, column 5" in note["body"]

    # A restart with the same error prints again but rings nothing new.
    assert await note_mod.publish(state) is False
    assert str(base) in capsys.readouterr().err
    assert len(state._notification_log) == 1


class _StoredState(_State):
    """A state whose notes go through the real notification store and reload from it."""

    def _deliver(self, note: dict) -> None:
        super()._deliver(note)
        assert _persist_notification(note)

    @classmethod
    def restarted(cls) -> "_StoredState":
        state = cls()
        state._notification_log = _load_notifications()
        return state


@pytest.mark.asyncio
async def test_a_restart_reads_the_stored_note_and_rings_nothing_new(
    files: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    base, _local = files
    base.write_text(MISSING_COMMA, encoding="utf-8")
    assert await note_mod.publish(_StoredState()) is True

    restarted = _StoredState.restarted()
    (stored,) = restarted._notification_log
    assert stored["key"] == note_mod.KEY

    assert await note_mod.publish(restarted) is False
    assert len(restarted._notification_log) == 1
    assert restarted.dismissed == 0


@pytest.mark.asyncio
async def test_a_different_error_replaces_the_note_and_a_fix_removes_it(
    files: tuple[Path, Path],
) -> None:
    base, _local = files
    base.write_text(MISSING_COMMA, encoding="utf-8")
    state = _State()
    assert await note_mod.publish(state) is True

    base.write_text(TRAILING_COMMA, encoding="utf-8")
    assert await note_mod.publish(state) is True
    (note,) = state._notification_log
    assert "remove the comma at the end of line 3" in note["body"]

    base.write_text("{}", encoding="utf-8")
    assert await note_mod.publish(state) is False
    assert state._notification_log == []


@pytest.mark.asyncio
async def test_without_a_dashboard_the_error_is_still_printed(
    files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    base, _local = files
    base.write_text(MISSING_COMMA, encoding="utf-8")

    assert await note_mod.publish(None) is False

    assert "line 4, column 5" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_clean_start_with_no_note_touches_nothing(files: tuple[Path, Path]) -> None:
    state = _State()

    assert await note_mod.publish(state) is False

    assert state._notification_log == []
    assert state.dismissed == 0


@pytest.mark.parametrize("enforcing", [False, True], ids=["plain", "sandbox-sealed"])
@pytest.mark.asyncio
async def test_a_broken_file_survives_real_loads_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforcing: bool
) -> None:
    # The start path end to end: loads (each one may run a migration write-back),
    # then the report. The broken bytes stay, no .bak appears, and a note is pushed.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(loosening_provenance, "enforcement_active", lambda: enforcing)
    path = config_path()
    approved = b'{"dashboard": {"theme_mode": "light"}}'
    if enforcing:
        loosening_provenance.record(loosening_provenance.LEAF_CONFIG, approved, strict=True)
    path.write_text(MISSING_COMMA, encoding="utf-8")

    # A load and then a "restart": a sealed file keeps serving the approved copy.
    for _ in range(2):
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.theme_mode == ("light" if enforcing else "")

    assert path.read_text(encoding="utf-8") == MISSING_COMMA
    assert not Path(str(path) + ".bak").exists()
    if enforcing:
        assert loosening_provenance.leaf_pending(loosening_provenance.LEAF_CONFIG)

    state = _State()
    assert await note_mod.publish(state) is True
    (note,) = state._notification_log
    assert str(path) in note["body"]
    assert ("Settings > Security" in note["body"]) is enforcing
    assert ("running on default settings" in note["body"]) is not enforcing
    assert path.read_text(encoding="utf-8") == MISSING_COMMA
