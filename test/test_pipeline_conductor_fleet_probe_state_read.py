"""Fleet probe -- how the handled-set state file is read before a mark writes it.

``--mark-handled`` writes back the map it reads, so the read it makes decides what
survives on disk. A present file it cannot read must refuse the mark (exit 4) and
stay byte-for-byte as it was, or every recorded disposition but the new one is
gone. The patrol's read stays tolerant because it never writes.

Shared fixtures and helpers are in ``pipeline_conductor_fleet_probe_helpers.py``;
``test_pipeline_conductor_fleet_probe.py`` heads the suite and states how it is organised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pipeline_conductor_fleet_probe_helpers import mod_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import sessions_fixture  # noqa: F401 - fixture

#: Two answered dispositions that a mark on a third key must never erase.
SEEDED = {
    "handled": {
        "wA": {"tag": "BLOCKED", "digest": "aaaa", "ts": 1},
        "wB": {"tag": "DONE", "digest": "bbbb", "ts": 1},
    },
    "updated_at": 1,
}


def _seeded_text() -> str:
    return json.dumps(SEEDED, indent=1) + "\n"


def _gone_digest(mod) -> str:
    """The digest of the payload a mark on a session with no transcript resolves."""
    return mod._digest("STALLED:transcript missing")


@pytest.fixture
def config(tmp_path) -> Path:
    path = tmp_path / "probe.json"
    path.write_text("{}", encoding="utf-8")
    return path


def _state_of(config: Path) -> Path:
    return Path(f"{config}.state.json")


def _mark(mod, config: Path, key: str = "wC") -> int:
    return mod.main(["--config", str(config), "--mark-handled", key, "STALLED", _gone_digest(mod)])


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (_seeded_text()[: len(_seeded_text()) // 2].encode("utf-8"), "not valid JSON"),
        (b"", "not valid JSON"),
        (b'{"handled": {"wA": "\xff\xfe"}}\n', "not UTF-8"),
    ],
    ids=["torn", "empty", "not-utf8"],
)
def test_mark_refuses_a_present_state_it_cannot_read_and_leaves_it_untouched(
    mod, sessions, config, capsys, content, reason
):
    state = _state_of(config)
    state.write_bytes(content)
    before = state.read_bytes()
    assert _mark(mod, config) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"refused: state file {state} is unreadable" in captured.err
    assert reason in captured.err
    assert "not overwriting it" in captured.err
    assert state.read_bytes() == before
    assert sorted(p.name for p in state.parent.iterdir() if p.name.startswith(state.name)) == [
        state.name
    ]


def test_mark_refuses_when_the_state_read_raises_an_os_error(
    mod, sessions, config, capsys, monkeypatch
):
    """A sharing violation (another handle holds the file) surfaces as an OSError."""
    state = _state_of(config)
    state.write_text(_seeded_text(), encoding="utf-8")
    before = state.read_bytes()
    real_read_text = Path.read_text

    def locked(self, *args, **kwargs):
        if self == state:
            raise PermissionError(13, "The process cannot access the file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked)
    assert _mark(mod, config) == 4
    err = capsys.readouterr().err
    assert "PermissionError" in err and "not overwriting it" in err
    assert state.read_bytes() == before


def test_mark_refuses_when_the_state_path_is_a_directory(mod, sessions, config, capsys):
    state = _state_of(config)
    state.mkdir()
    assert _mark(mod, config) == 4
    assert "not overwriting it" in capsys.readouterr().err
    assert state.is_dir() and list(state.iterdir()) == []


def test_mark_on_a_readable_state_keeps_every_other_disposition(mod, sessions, config, capsys):
    state = _state_of(config)
    state.write_text(_seeded_text(), encoding="utf-8")
    assert _mark(mod, config) == 0
    assert capsys.readouterr().out.strip() == "handled wC STALLED"
    handled = json.loads(state.read_text(encoding="utf-8"))["handled"]
    assert sorted(handled) == ["wA", "wB", "wC"]
    assert handled["wA"] == SEEDED["handled"]["wA"]


def test_mark_with_no_state_file_creates_one(mod, sessions, config):
    state = _state_of(config)
    assert _mark(mod, config) == 0
    assert sorted(json.loads(state.read_text(encoding="utf-8"))["handled"]) == ["wC"]


@pytest.mark.parametrize("content", ["[1, 2]\n", '{"handled": [1, 2]}\n', "null\n"])
def test_mark_rewrites_parsed_json_that_holds_no_handled_set(mod, sessions, config, content):
    """Wrong-shaped JSON parses, so there is nothing recorded to lose: the mark repairs it."""
    state = _state_of(config)
    state.write_text(content, encoding="utf-8")
    assert _mark(mod, config) == 0
    assert sorted(json.loads(state.read_text(encoding="utf-8"))["handled"]) == ["wC"]


def test_a_stale_digest_is_still_refused_before_the_state_is_read(mod, sessions, config, capsys):
    state = _state_of(config)
    state.write_text("{torn", encoding="utf-8")
    assert mod.main(["--config", str(config), "--mark-handled", "wC", "STALLED", "stale"]) == 3
    assert "payload changed since the probe" in capsys.readouterr().err
    assert state.read_text(encoding="utf-8") == "{torn"


def test_the_patrol_still_reads_a_torn_state_as_empty(
    mod, sessions, config, tmp_path, monkeypatch, capsys
):
    proc_root = tmp_path / "proc-empty"
    proc_root.mkdir()
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc_root))
    state = _state_of(config)
    state.write_text("{torn", encoding="utf-8")
    assert mod.main(["--config", str(config)]) == 0
    assert capsys.readouterr().out.startswith("OK 0 watched")
    assert state.read_text(encoding="utf-8") == "{torn"


def test_strict_load_returns_empty_only_for_a_missing_file(mod, tmp_path):
    assert mod._load_state_strict(tmp_path / "absent.json") == {}
    good = tmp_path / "good.json"
    good.write_text(_seeded_text(), encoding="utf-8")
    assert mod._load_state_strict(good) == SEEDED
    bare = tmp_path / "bare.json"
    bare.write_text("{}", encoding="utf-8")
    assert mod._load_state_strict(bare) == {}
    torn = tmp_path / "torn.json"
    torn.write_text("{torn", encoding="utf-8")
    with pytest.raises(mod.StateUnreadable):
        mod._load_state_strict(torn)
