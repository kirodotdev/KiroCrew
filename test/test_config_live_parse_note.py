"""A config file that stops parsing while the gateway runs is reported in the bell.

Contract under test:
- a save that leaves ``config.json`` unparseable under a running watcher keeps the
  settings in force, leaves the file bytes alone, logs one WARNING carrying the
  line and column, and publishes one critical bell note naming the file, the line
  and the column and saying the previous settings are still in force;
- a second save with the same error publishes nothing new; a save with a
  different error replaces the note;
- a gateway booted on a torn file runs on defaults: the bell holds the start report's
  note (never rung twice for the same body), a different error replaces it, a file
  that is not an object is reported by the watcher, and the repair removes the note;
- under the sandbox seal a torn save reads as whole to the watcher, so the files on
  disk decide whether the note rings, stays or is removed;
- a reset write over an unreadable file refuses cleanly and never overwrites a copy
  an earlier reset kept;
- a repaired file applies its values and removes the note;
- ``update_config_locked(..., on_corrupt="reset")`` (the ``config set --file``
  writer) still installs the new document over an unreadable file, and first
  logs a WARNING naming the file and the parse error and keeps the unreadable
  bytes beside it as ``config.json.unreadable``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from pathlib import Path

import pytest
from _hot_reload_helpers import write_config as _write
from config_live_helpers import cfg_file_fixture  # noqa: F401 -- fixture used by name

from kiro_crew.config.live import ConfigWatch
from kiro_crew.config.loader import KiroCrewConfig, update_config_locked
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
from kiro_crew.config.syntax_report import syntax_error_in
from kiro_crew.notifications import config_syntax_note
from kiro_crew.notifications.bus import NotificationBus


class _State:
    """What the note publisher reads off the dashboard state."""

    def __init__(self) -> None:
        self._notification_log: list[dict] = []
        self.notification_bus = NotificationBus(sink=self._deliver)
        self.last_notification_persist: asyncio.Future[bool] | None = None
        self.pushed = 0
        self.dismiss_saves = True

    def _deliver(self, note: dict) -> None:
        self.pushed += 1
        # A keyed note replaces the stored note with the same source and key.
        self._notification_log[:] = [
            n
            for n in self._notification_log
            if not (n.get("source") == note.get("source") and n.get("key") == note.get("key"))
        ]
        self._notification_log.append(note)
        done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        done.set_result(True)
        self.last_notification_persist = done

    async def dismiss_notification(self, *, source: str, key: str) -> tuple[bool, bool]:
        held = [
            n for n in self._notification_log if n.get("source") == source and n.get("key") == key
        ]
        if not self.dismiss_saves:
            return bool(held), False
        self._notification_log[:] = [n for n in self._notification_log if n not in held]
        return bool(held), True


@pytest.fixture(autouse=True)
def _report_reads_the_fixture_files(cfg_file: Path, _floor_monkeypatch) -> None:
    """The start report and the live clear read the same files the watcher loads."""
    _floor_monkeypatch.setattr("kiro_crew.config.syntax_report.config_path", lambda: cfg_file)
    _floor_monkeypatch.setattr(
        "kiro_crew.config.syntax_report.config_local_path", lambda: cfg_file.with_name("none.json")
    )


def _save(path: Path, text: str, step: int) -> None:
    """Write *text* and move the mtime forward, so the watcher sees a new save."""
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + step * 2_000_000_000))


def _notes(state: _State) -> list[dict]:
    return [n for n in state._notification_log if n.get("key") == config_syntax_note.KEY]


@pytest.mark.asyncio
async def test_a_file_torn_under_a_running_watcher_rings_once_and_clears_on_repair(
    cfg_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = _State()
    w = ConfigWatch()
    w.prime(KiroCrewConfig.load())
    w.tear_reporter = functools.partial(config_syntax_note.publish_tear, state)

    missing_comma = '{\n  "agent": {\n    "model": "model-b"\n    "log_level": "INFO"\n  }\n}\n'
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.live"):
        _save(cfg_file, missing_comma, 1)
        assert await w.refresh_now() is None
    assert cfg_file.read_text(encoding="utf-8") == missing_comma, "the file is left alone"
    snap = w.snapshot()
    assert snap is not None and snap.agent.model == "model-a", "the previous settings stay"
    warned = [r.getMessage() for r in caplog.records if "does not parse" in r.getMessage()]
    assert len(warned) == 1 and "line 4, column 5" in warned[0]

    notes = _notes(state)
    assert len(notes) == 1
    note = notes[0]
    assert note["priority"] == "critical"
    assert str(cfg_file) in note["body"] and "line 4, column 5" in note["body"]
    assert "settings it has loaded" in note["body"]

    # The same error saved again rings nothing new.
    _save(cfg_file, missing_comma, 2)
    assert await w.refresh_now() is None
    assert len(_notes(state)) == 1 and state.pushed == 1

    # A different error replaces the note.
    _save(cfg_file, '{"agent": {"model": "model-b",}}', 3)
    assert await w.refresh_now() is None
    notes = _notes(state)
    assert len(notes) == 1 and state.pushed == 2
    assert "line 1, column" in notes[0]["body"] and "line 4, column 5" not in notes[0]["body"]

    # The repair applies and removes the note.
    _write(cfg_file, {"agent": {"model": "model-b", "log_level": "INFO"}})
    change = await w.refresh_now()
    assert change is not None and change.new.agent.model == "model-b"
    assert _notes(state) == []


@pytest.mark.asyncio
async def test_the_live_note_names_the_error_as_the_start_report_does_in_the_same_bell_slot(
    cfg_file: Path,
) -> None:
    """One sentence formats the position, and one note slot holds both producers."""
    state = _State()
    w = ConfigWatch()
    w.prime(KiroCrewConfig.load())
    w.tear_reporter = functools.partial(config_syntax_note.publish_tear, state)
    _save(cfg_file, '{"agent": {"model": "model-b",}}', 1)
    assert await w.refresh_now() is None

    err = syntax_error_in(cfg_file)
    assert err is not None
    (note,) = _notes(state)
    assert err.summary() in note["body"]

    # The start report then rings the same slot: the live note is replaced, not joined.
    assert await config_syntax_note.publish(state) is True
    (start_note,) = _notes(state)
    assert start_note["body"] != note["body"] and err.summary() in start_note["body"]


@pytest.mark.asyncio
async def test_a_gateway_booted_on_a_torn_file_keeps_the_start_note_until_a_different_error(
    cfg_file: Path,
) -> None:
    """The start report says defaults run; the watcher rings nothing for the same error,
    words a different one the same way, and the repair removes whichever note stands."""
    torn = '{"agent": {"model": "model-b",}}'
    _save(cfg_file, torn, 1)
    boot = KiroCrewConfig.load()
    assert DEGRADED_WHOLE_CONFIG in boot.degraded_sections
    state = _State()
    assert await config_syntax_note.publish(state) is True
    (start_note,) = _notes(state)
    assert "default settings" in start_note["body"]

    w = ConfigWatch()
    w.prime(boot)
    w.tear_reporter = functools.partial(config_syntax_note.publish_tear, state)
    assert await w.refresh_now() is None
    _save(cfg_file, torn, 2)
    assert await w.refresh_now() is None
    assert state.pushed == 1 and _notes(state) == [start_note], "the same error rings nothing"

    _save(cfg_file, '{"agent": {"model": }}', 3)
    assert await w.refresh_now() is None
    (note,) = _notes(state)
    assert state.pushed == 2 and "default settings" in note["body"], "the new position, same words"
    assert note["body"] != start_note["body"]

    _write(cfg_file, {"agent": {"model": "model-b", "log_level": "INFO"}})
    await w.refresh_now()
    assert _notes(state) == []


@pytest.mark.asyncio
async def test_a_gateway_booted_on_a_file_that_is_not_an_object_still_rings(
    cfg_file: Path,
) -> None:
    """The start report names only syntax errors, so the watcher's note is the only one."""
    _save(cfg_file, "[]", 1)
    boot = KiroCrewConfig.load()
    assert DEGRADED_WHOLE_CONFIG in boot.degraded_sections
    state = _State()
    assert await config_syntax_note.publish(state) is False and _notes(state) == []

    w = ConfigWatch()
    w.prime(boot)
    w.tear_reporter = functools.partial(config_syntax_note.publish_tear, state)
    assert await w.refresh_now() is None
    (note,) = _notes(state)
    assert "is not a JSON object" in note["body"]


@pytest.mark.asyncio
async def test_a_whole_read_settles_the_note_from_the_files_on_disk(cfg_file: Path) -> None:
    """A held edit reads as whole to the watcher, which serves the endorsed copy."""
    cfg_file.write_text('{"agent": {"model": "model-b",}}', encoding="utf-8")
    state = _State()
    assert await config_syntax_note.publish(state) is True
    (held,) = _notes(state)

    assert await config_syntax_note.publish_tear(state, ()) is False
    assert _notes(state) == [held] and state.pushed == 1, "the file on disk still fails"

    cfg_file.write_text('{"agent": {"model": }}', encoding="utf-8")
    assert await config_syntax_note.publish_tear(state, ()) is True
    (second,) = _notes(state)
    assert second["body"] != held["body"], "a different error on disk rings"

    _write(cfg_file, {"agent": {"model": "model-b", "log_level": "INFO"}})
    assert await config_syntax_note.publish_tear(state, ()) is False
    assert _notes(state) == []


@pytest.mark.asyncio
async def test_a_torn_save_the_watcher_reads_as_whole_still_rings_and_its_repair_clears(
    cfg_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the sandbox seal the watcher's probe parses the endorsed copy, not the save."""
    state = _State()
    w = ConfigWatch()
    w.prime(KiroCrewConfig.load())
    w.tear_reporter = functools.partial(config_syntax_note.publish_tear, state)
    monkeypatch.setattr(ConfigWatch, "_document_tear", staticmethod(lambda: ()))

    _save(cfg_file, '{"agent": {"model": "model-b",}}', 1)
    await w.refresh_now()
    (note,) = _notes(state)
    assert str(cfg_file) in note["body"] and state.pushed == 1

    await w.refresh_now()
    assert state.pushed == 1, "an unchanged tick rings nothing"

    _write(cfg_file, {"agent": {"model": "model-b", "log_level": "INFO"}})
    await w.refresh_now()
    assert _notes(state) == []


@pytest.mark.asyncio
async def test_a_note_stored_before_a_restart_is_removed_by_the_first_whole_read(
    cfg_file: Path,
) -> None:
    """The file was repaired while no gateway ran: the new process clears the note."""
    state = _State()
    await config_syntax_note.publish_tear(state, (f"{cfg_file} does not parse.",))
    assert len(_notes(state)) == 1
    w = ConfigWatch()
    w.prime(KiroCrewConfig.load())
    w.tear_reporter = functools.partial(config_syntax_note.publish_tear, state)
    await w.refresh_now()
    assert _notes(state) == []


@pytest.mark.asyncio
async def test_a_removal_that_was_not_saved_is_retried_on_an_unchanged_tick(
    cfg_file: Path,
) -> None:
    state = _State()
    w = ConfigWatch()
    w.prime(KiroCrewConfig.load())
    w.tear_reporter = functools.partial(config_syntax_note.publish_tear, state)
    await w.refresh_now()
    _save(cfg_file, '{"agent": {"model": "model-b",}}', 1)
    await w.refresh_now()
    assert len(_notes(state)) == 1

    state.dismiss_saves = False
    _write(cfg_file, {"agent": {"model": "model-b", "log_level": "INFO"}})
    await w.refresh_now()
    assert len(_notes(state)) == 1, "the removal was not saved"
    state.dismiss_saves = True
    assert await w.refresh_now() is None, "the file is unchanged"
    assert _notes(state) == [], "the unchanged tick retried the removal"


@pytest.mark.asyncio
async def test_a_repair_reported_while_the_note_write_is_in_flight_still_removes_it() -> None:
    """The note's write can outlast the reporter timeout; the removal waits behind it."""
    state = _State()
    loop = asyncio.get_running_loop()
    written: asyncio.Future[None] = loop.create_future()
    # The in-flight note enters the log when its write finishes.
    written.add_done_callback(
        lambda _f: state._notification_log.append(
            {"source": config_syntax_note.SOURCE, "key": config_syntax_note.KEY, "body": "x"}
        )
    )
    dismiss = state.dismiss_notification

    async def dismiss_after_write(*, source: str, key: str) -> tuple[bool, bool]:
        await written  # the rewrite lock the pending write holds
        return await dismiss(source=source, key=key)

    state.dismiss_notification = dismiss_after_write  # type: ignore[method-assign]
    loop.call_soon(written.set_result, None)
    await asyncio.wait_for(config_syntax_note.publish_tear(state, ()), timeout=60)
    await asyncio.wait_for(written, timeout=60)
    assert _notes(state) == []


def test_a_reset_write_keeps_the_unreadable_bytes_and_says_so(
    cfg_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    broken = '{"agent": {"model": "model-a" "x": 1}}'
    cfg_file.write_text(broken, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        update_config_locked(
            cfg_file, mutate=lambda _d: {"agent": {"model": "model-c"}}, on_corrupt="reset"
        )
    assert '"model-c"' in cfg_file.read_text(encoding="utf-8"), "the new document is installed"
    kept = cfg_file.with_name("config.json.unreadable")
    assert kept.read_text(encoding="utf-8") == broken
    if os.name == "posix":
        assert kept.stat().st_mode & 0o077 == 0, "the copy is owner-only: it can hold credentials"
    warned = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "kiro_crew.config.loader"
    ]
    assert len(warned) == 1 and str(cfg_file) in warned[0] and "line 1 column 31" in warned[0]
    assert str(kept) in warned[0]


def test_a_refused_reset_write_leaves_the_earlier_copy_and_says_nothing(
    cfg_file: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config import loader
    from kiro_crew.config.loader import ConfigWriteRefused

    kept = cfg_file.with_name("config.json.unreadable")
    kept.write_text("bytes an earlier reset replaced", encoding="utf-8")
    broken = '{"agent": {"model": "model-a" "x": 1}}'
    cfg_file.write_text(broken, encoding="utf-8")

    def refuse(*_a: object, **_k: object) -> None:
        raise ConfigWriteRefused("refused by the publish floor")

    monkeypatch.setattr(loader, "write_config_atomically", refuse)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        with pytest.raises(ConfigWriteRefused):
            update_config_locked(
                cfg_file, mutate=lambda _d: {"agent": {"model": "model-c"}}, on_corrupt="reset"
            )
    assert cfg_file.read_text(encoding="utf-8") == broken
    assert kept.read_text(encoding="utf-8") == "bytes an earlier reset replaced"
    assert not list(cfg_file.parent.glob("config.json.unreadable.*.staged"))
    assert not [r for r in caplog.records if "kept its previous bytes" in r.getMessage()]


def test_a_refused_reset_leaves_a_copy_an_earlier_reset_kept_under_its_staged_name(
    cfg_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config import loader
    from kiro_crew.config.loader import ConfigWriteRefused

    retained = cfg_file.with_name("config.json.unreadable.staged")
    retained.write_text("bytes whose rename failed", encoding="utf-8")
    cfg_file.write_text('{"agent": {"model": "model-a" "x": 1}}', encoding="utf-8")

    def refuse(*_a: object, **_k: object) -> None:
        raise ConfigWriteRefused("refused by the publish floor")

    monkeypatch.setattr(loader, "write_config_atomically", refuse)
    with pytest.raises(ConfigWriteRefused):
        update_config_locked(
            cfg_file, mutate=lambda _d: {"agent": {"model": "model-c"}}, on_corrupt="reset"
        )
    assert retained.read_text(encoding="utf-8") == "bytes whose rename failed"


@pytest.mark.skipif(os.name != "posix", reason="needs a FIFO")
@pytest.mark.timeout(30)
def test_the_unreadable_copy_refuses_a_fifo_instead_of_waiting_on_it(cfg_file: Path) -> None:
    """A sealed read refuses the special file, then the copy runs under the same lock."""
    from kiro_crew.config import loader
    from kiro_crew.config.loader import ConfigReadError

    cfg_file.unlink()
    os.mkfifo(cfg_file)
    refusal = ConfigReadError("not a regular file")
    with pytest.raises(ConfigReadError) as raised:
        loader._replace_keeping_unreadable(cfg_file, {}, refusal, False, False)
    assert raised.value is refusal
    assert not list(cfg_file.parent.glob("config.json.unreadable*"))


@pytest.mark.asyncio
async def test_the_gateway_points_the_process_watcher_at_the_bell(cfg_file: Path) -> None:
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.config import live
    from kiro_crew.dashboard.server import _register_config_watch

    state = _State()
    state.set_dynamic_cards_enabled = MagicMock()  # type: ignore[attr-defined]
    _register_config_watch(web.Application(), state, None)  # type: ignore[arg-type]
    reporter = live.watch().tear_reporter
    assert reporter is not None
    await reporter((f"{cfg_file} has a JSON syntax error on line 2, column 3: x.",))
    assert len(_notes(state)) == 1
    await reporter(())
    assert _notes(state) == []
