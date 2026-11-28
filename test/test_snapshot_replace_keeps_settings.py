"""``kirocrew restore --mode replace`` keeps this install's settings documents unless picked.

``config.json``, ``config.local.json`` and ``hooks.json`` of an archive take effect on a
running gateway within seconds of being installed. A Replace therefore installs them over
an EXISTING live file only with ``--include-config``; a missing live file is still filled
from the archive. The command says which files it kept, and which it overwrites, by name.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from snapshot_helpers import unpinnable_argv

from kiro_crew import snapshot as snap

SETTINGS = ("config.json", "config.local.json", "hooks.json")
MARKER = {"agent": {"provider": "archive-marker"}}
LIVE = {"agent": {"provider": "live-original"}}
SECOND_LINE = "may not come from this install"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
    return h


def _bundle(tmp_path: Path) -> Path:
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "config.json").write_text(json.dumps(MARKER), encoding="utf-8")
    (payload / "config.local.json").write_text(json.dumps({"x": "archive"}), encoding="utf-8")
    (payload / "hooks.json").write_text(json.dumps({"hooks": []}), encoding="utf-8")
    (payload / "notifications.jsonl").write_text('{"title": "from archive"}\n', encoding="utf-8")
    (payload / "MANIFEST.json").write_text(
        json.dumps(
            {"version": 3, "components": {"config": "unresolved", "notifications": "unresolved"}}
        ),
        encoding="utf-8",
    )
    bundle = tmp_path / "b.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return bundle


def _live_settings(home: Path) -> dict[str, bytes]:
    (home / "config.json").write_text(json.dumps(LIVE), encoding="utf-8")
    (home / "config.local.json").write_text(json.dumps({"x": "live"}), encoding="utf-8")
    (home / "hooks.json").write_text(json.dumps({"hooks": [{"id": "live"}]}), encoding="utf-8")
    return {name: (home / name).read_bytes() for name in SETTINGS}


def _replace(bundle: Path, *extra: str) -> list[str]:
    return [str(bundle), "--mode", "replace", "--force", *extra, *unpinnable_argv()]


def test_replace_without_the_pick_leaves_the_live_documents_byte_for_byte(home, tmp_path, capsys):
    before = _live_settings(home)

    rc = snap.restore_main(_replace(_bundle(tmp_path)))

    out = capsys.readouterr().out
    assert rc == 0, out
    for name in SETTINGS:
        assert (home / name).read_bytes() == before[name], name
    assert (home / "notifications.jsonl").is_file(), "the rest of Replace still restores"
    assert "Kept your live" in out, out
    for name in SETTINGS:
        assert name in out, out
    assert SECOND_LINE not in out, "the archive warning belongs to the pick"


def test_replace_with_the_pick_replaces_them_and_backs_up_the_old_ones(home, tmp_path, capsys):
    _live_settings(home)

    rc = snap.restore_main(_replace(_bundle(tmp_path), "--include-config"))

    out = capsys.readouterr().out
    assert rc == 0, out
    assert json.loads((home / "config.json").read_text(encoding="utf-8")) == MARKER
    assert json.loads((home / "config.local.json").read_text(encoding="utf-8")) == {"x": "archive"}
    saved = [p for p in home.rglob("config.json") if p != home / "config.json"]
    assert saved and json.loads(saved[0].read_text(encoding="utf-8")) == LIVE
    assert "Kept your live" not in out, out


def test_replace_installs_the_archives_documents_where_no_live_file_exists(home, tmp_path, capsys):
    rc = snap.restore_main(_replace(_bundle(tmp_path)))

    assert rc == 0, capsys.readouterr().out
    assert json.loads((home / "config.json").read_text(encoding="utf-8")) == MARKER
    assert (home / "hooks.json").is_file()


def test_replace_does_not_install_the_overlay_beside_a_kept_config(home, tmp_path, capsys):
    # config.local.json outranks config.json, so installing the archive's over a kept
    # config.json would apply the archive's settings anyway.
    (home / "config.json").write_text(json.dumps(LIVE), encoding="utf-8")

    rc = snap.restore_main(_replace(_bundle(tmp_path)))

    out = capsys.readouterr().out
    assert rc == 0, out
    assert json.loads((home / "config.json").read_text(encoding="utf-8")) == LIVE
    assert not (home / "config.local.json").exists(), "the archive's overlay was installed"
    assert "config.local.json" in out, "the notice names the overlay it kept"


def test_replace_with_the_pick_installs_the_overlay_where_none_exists(home, tmp_path, capsys):
    (home / "config.json").write_text(json.dumps(LIVE), encoding="utf-8")

    rc = snap.restore_main(_replace(_bundle(tmp_path), "--include-config"))

    assert rc == 0, capsys.readouterr().out
    assert json.loads((home / "config.local.json").read_text(encoding="utf-8")) == {"x": "archive"}


def test_a_marker_config_restored_without_the_pick_is_not_seen_by_a_fresh_load(
    home, tmp_path, capsys
):
    from kiro_crew.config import loader_document

    _live_settings(home)
    assert snap.restore_main(_replace(_bundle(tmp_path))) == 0, capsys.readouterr().out

    seen = loader_document.read_config_for_update(home / "config.json")
    assert seen["agent"]["provider"] == "live-original", seen


def test_the_replace_text_names_the_files_it_overwrites_when_picked(home, tmp_path, capsys):
    _live_settings(home)

    snap.restore_main(_replace(_bundle(tmp_path), "--include-config"))

    out = capsys.readouterr().out
    assert "config.json" in out and "hooks.json" in out, out
    assert "disabled or paused" in out, out
    assert SECOND_LINE in out, out


def test_the_replace_text_says_the_files_are_kept_when_not_picked(home, tmp_path, capsys):
    _live_settings(home)

    snap.restore_main(_replace(_bundle(tmp_path)))

    out = capsys.readouterr().out
    assert "--include-config" in out, "the text must say how to pick them"


@pytest.mark.parametrize("pick", [False, True])
def test_dry_run_names_the_files_and_prints_the_second_line_only_for_the_pick(
    home, tmp_path, capsys, pick
):
    _live_settings(home)
    extra = ["--include-config"] if pick else []

    rc = snap.restore_main(_replace(_bundle(tmp_path), "--dry-run", *extra))

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "config.json" in out and "hooks.json" in out, out
    assert (SECOND_LINE in out) is pick, out
    assert ("disabled or paused" in out) is pick, out
    assert json.loads((home / "config.json").read_text(encoding="utf-8")) == LIVE


def test_merge_never_prints_the_replace_notice(home, tmp_path, capsys):
    _live_settings(home)

    rc = snap.restore_main(
        [str(_bundle(tmp_path)), "--mode", "merge", "--force"] + unpinnable_argv()
    )

    out = capsys.readouterr().out
    assert rc == 0, out
    assert SECOND_LINE not in out and "Kept your live" not in out, out


def test_a_missing_live_file_is_installed_and_the_second_line_is_printed(home, tmp_path, capsys):
    rc = snap.restore_main(_replace(_bundle(tmp_path)))

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "will be installed" in out and "disabled or paused" in out, out
    for name in SETTINGS:
        assert name in out, out
    assert SECOND_LINE in out, out
    assert "Kept your live" not in out, out


def test_dry_run_names_the_documents_it_would_install_and_writes_nothing(home, tmp_path, capsys):
    rc = snap.restore_main(_replace(_bundle(tmp_path), "--dry-run"))

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "will be installed" in out and SECOND_LINE in out, out
    assert not (home / "config.json").exists()


def test_only_the_missing_documents_are_named_as_installed_when_some_exist(home, tmp_path, capsys):
    (home / "config.json").write_text(json.dumps(LIVE), encoding="utf-8")

    rc = snap.restore_main(_replace(_bundle(tmp_path), "--dry-run"))

    out = capsys.readouterr().out
    assert rc == 0, out
    # config.local.json is named as kept too: it outranks the kept config.json.
    assert "keeps this install's live config.json, config.local.json." in out, out
    assert "no hooks.json" in out, out
    assert SECOND_LINE in out, out


_REPLACE_HELPERS = ("_do_replace", "_do_replace_mutations", "_backup_and_copy")


def test_every_production_caller_of_the_replace_helpers_passes_the_flag_explicitly():
    """A caller that forgets the flag must not silently choose for the user."""
    import ast

    root = Path(snap.__file__).parent
    seen = 0
    missing = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _REPLACE_HELPERS:
                seen += 1
                if not any(kw.arg == "include_settings" for kw in node.keywords):
                    missing.append(f"{path.name}:{node.lineno} {name}")
    assert seen >= 4, "the scan found no callers; it would pass for the wrong reason"
    assert missing == []


@pytest.mark.parametrize("name", _REPLACE_HELPERS)
def test_the_replace_helpers_keep_the_live_settings_by_default(name):
    import inspect

    param = inspect.signature(getattr(snap, name)).parameters["include_settings"]
    assert param.default is False
