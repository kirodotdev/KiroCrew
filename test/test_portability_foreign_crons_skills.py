"""An archive from another install: its crons arrive paused and reset, its skills stay out.

Every cron job of an archive is restored PAUSED, message-only jobs too, with the record
fields that widen what the job may do reset to this install's defaults. A job the user
picks (``include_crons``) is installed as written. A Replace does not install the
archive's ``skills/`` tree over a live one unless it is picked (``include_skills``); a
machine with no live skills installs it as it always did.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import portability
from kiro_crew.portability import apply_import_zip
from kiro_crew.portability_vetting import _hold_back_foreign_skills, _sanitize_imported_crons
from kiro_crew.testing.links import make_dir_link

RESET_FIELDS = (
    "approval_mode",
    "project_path",
    "env",
    "secret_env",
    "execution_context",
    "agent_sequence",
    "channel",
    "model",
)


def _risky_job(name: str = "nightly", **extra) -> dict:
    """A message-only job that asks for every widening field."""
    return {
        "id": f"id-{name}",
        "name": name,
        "message": "check the build",
        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        "enabled": True,
        "approval_mode": "auto",
        "project_path": "/srv/other-projects",
        "env": {"FLAG": "1"},
        "secret_env": {"TOKEN": "x"},
        "execution_context": "ctx",
        "agent_sequence": ["one", "two"],
        "channel": "C123",
        "model": "some-model",
        "agent_id": "ghost-agent",
        **extra,
    }


def _store(tmp_path: Path, jobs: list[dict]) -> Path:
    path = tmp_path / "crons.json"
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return path


def _read(path: Path) -> dict[str, dict]:
    return {j["name"]: j for j in json.loads(path.read_text(encoding="utf-8"))["jobs"]}


def test_helper_pauses_a_message_only_job_and_resets_its_fields(tmp_path):
    path = _store(tmp_path, [_risky_job()])

    dropped, paused = _sanitize_imported_crons(path, hold_all=True, known_agents=frozenset())

    job = _read(path)["nightly"]
    assert dropped == [] and paused == ["nightly"]
    assert job["user_paused"] is True and job["enabled"] is False
    for field in RESET_FIELDS:
        assert not job.get(field), field
    assert not job.get("agent_id")
    assert job["message"] == "check the build"


def test_helper_resets_the_channel_delivery_pin(tmp_path):
    path = _store(
        tmp_path,
        [_risky_job(channel_type="discord", target_id="123456", thread_ts="1700000000.000100")],
    )

    _sanitize_imported_crons(path, hold_all=True, known_agents=frozenset())

    job = _read(path)["nightly"]
    for field in ("channel_type", "target_id", "thread_ts"):
        assert field not in job, field


def test_helper_keeps_an_agent_that_exists_here_and_drops_one_that_does_not(tmp_path):
    path = _store(
        tmp_path,
        [_risky_job("a", agent_id="known"), _risky_job("b", agent_id="ghost-agent")],
    )

    _sanitize_imported_crons(path, hold_all=True, known_agents=frozenset({"known"}))

    jobs = _read(path)
    assert jobs["a"]["agent_id"] == "known"
    assert not jobs["b"].get("agent_id")


def test_helper_names_a_job_that_was_already_paused_when_its_settings_are_removed(tmp_path):
    bare = {
        "id": "id-bare",
        "name": "bare",
        "message": "check the build",
        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        "user_paused": True,
        "enabled": False,
    }
    path = _store(tmp_path, [_risky_job("was-paused", user_paused=True, enabled=False), bare])

    _dropped, paused = _sanitize_imported_crons(path, hold_all=True, known_agents=frozenset())

    assert paused == ["was-paused"]
    assert not _read(path)["was-paused"].get("approval_mode")


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
        for name, text in files.items():
            zf.writestr(f"snap/{name}", text)
    return path


def _import(zip_path: Path, home: Path, mode: str = "replace", **kwargs) -> dict:
    with patch.object(portability, "config_dir", return_value=home):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(home)}):
            return apply_import_zip(zip_path, mode=mode, **kwargs)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_import_restores_a_message_only_cron_paused_with_its_fields_reset(tmp_path):
    home = _home(tmp_path)
    z = _zip(tmp_path / "a.zip", {"crons.json": json.dumps({"jobs": [_risky_job()]})})

    summary = _import(z, home)

    job = _read(home / "crons.json")["nightly"]
    assert job["user_paused"] is True and job["enabled"] is False
    for field in RESET_FIELDS:
        assert not job.get(field), field
    assert summary["paused_crons"] == ["nightly"]
    assert summary["paused_crons_reset"] is True


def test_import_in_merge_mode_also_pauses_and_resets(tmp_path):
    home = _home(tmp_path)
    z = _zip(tmp_path / "a.zip", {"crons.json": json.dumps({"jobs": [_risky_job()]})})

    summary = _import(z, home, mode="merge")

    job = _read(home / "crons.json")["nightly"]
    assert job["user_paused"] is True and not job.get("approval_mode")
    assert summary["paused_crons"] == ["nightly"]


def test_merge_does_not_name_a_job_this_install_already_has(tmp_path):
    home = _home(tmp_path)
    live = _risky_job(approval_mode="")
    (home / "crons.json").write_text(json.dumps({"jobs": [live]}), encoding="utf-8")
    z = _zip(tmp_path / "a.zip", {"crons.json": json.dumps({"jobs": [_risky_job()]})})

    summary = _import(z, home, mode="merge")

    assert "paused_crons" not in summary
    assert _read(home / "crons.json")["nightly"]["enabled"] is True


def test_an_agent_the_same_import_installs_keeps_its_cron_agent_id(tmp_path):
    home = _home(tmp_path)
    z = _zip(tmp_path / "a.zip", {"crons.json": json.dumps({"jobs": [_risky_job()]})})

    _import(z, home, agent_templates=("ghost-agent",))

    assert _read(home / "crons.json")["nightly"]["agent_id"] == "ghost-agent"


def test_the_cron_pick_names_only_command_jobs_and_does_not_claim_a_reset(tmp_path):
    home = _home(tmp_path)
    jobs = [_risky_job("plain"), _risky_job("runs", command="echo hi")]
    z = _zip(tmp_path / "a.zip", {"crons.json": json.dumps({"jobs": jobs})})

    summary = _import(z, home, include_crons=True)

    assert summary["paused_crons"] == ["runs"]
    assert "paused_crons_reset" not in summary


def test_the_staged_skills_folder_is_not_deleted_through_a_redirected_root(tmp_path):
    live = tmp_path / "home" / "skills" / "mine"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("live skill", encoding="utf-8")
    root = tmp_path / "staging-root"
    make_dir_link(root, tmp_path / "home")

    with pytest.raises(OSError):
        _hold_back_foreign_skills(root, tmp_path / "home")

    assert (live / "SKILL.md").read_text(encoding="utf-8") == "live skill"


def _staged_world(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A staging root holding ``skills/``, a live home with skills, and an outside
    folder with its own ``skills/`` that must never be touched."""
    home = tmp_path / "home"
    (home / "skills" / "mine").mkdir(parents=True)
    (home / "skills" / "mine" / "SKILL.md").write_text("live skill", encoding="utf-8")
    root = tmp_path / "stage" / "root"
    (root / "skills" / "foreign").mkdir(parents=True)
    (root / "skills" / "foreign" / "SKILL.md").write_text("foreign skill", encoding="utf-8")
    outside = tmp_path / "outside"
    (outside / "skills").mkdir(parents=True)
    (outside / "skills" / "keep.txt").write_text("outside", encoding="utf-8")
    return root, home, outside


def _swap_root_for_link(root: Path, outside: Path) -> None:
    root.rename(root.with_name("root-moved"))
    root.symlink_to(outside, target_is_directory=True)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs descriptor-relative opens")
def test_a_root_swapped_for_a_link_after_the_check_does_not_delete_outside(tmp_path, monkeypatch):
    import shutil

    root, home, outside = _staged_world(tmp_path)
    real_rmtree = shutil.rmtree

    def swap_then_remove(path, *args, **kwargs):
        _swap_root_for_link(root, outside)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", swap_then_remove)

    try:
        _hold_back_foreign_skills(root, home)
    except OSError:
        pass

    assert (outside / "skills" / "keep.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs descriptor-relative opens")
def test_a_root_swapped_for_a_link_before_the_walk_is_refused(tmp_path, monkeypatch):
    from kiro_crew import pinned_fs

    root, home, outside = _staged_world(tmp_path)
    real_pin = pinned_fs.pin_parent

    def swap_then_pin(path, **kwargs):
        _swap_root_for_link(root, outside)
        return real_pin(path, **kwargs)

    monkeypatch.setattr(pinned_fs, "pin_parent", swap_then_pin)

    with pytest.raises(OSError):
        _hold_back_foreign_skills(root, home)

    assert (outside / "skills" / "keep.txt").read_text(encoding="utf-8") == "outside"
    assert (home / "skills" / "mine" / "SKILL.md").read_text(encoding="utf-8") == "live skill"


def test_a_platform_that_cannot_pin_deletes_by_name_only_when_that_was_accepted(
    tmp_path, monkeypatch
):
    from kiro_crew import pinned_fs

    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
    root, home, _outside = _staged_world(tmp_path)

    with pytest.raises(OSError):
        _hold_back_foreign_skills(root, home)
    assert (root / "skills" / "foreign" / "SKILL.md").is_file()

    assert _hold_back_foreign_skills(root, home, allow_unpinned=True) is True
    assert not (root / "skills").exists()


def test_a_live_skills_folder_holding_only_managed_copies_does_not_hold_back_the_archive(
    tmp_path,
):
    # Every gateway start copies the deploy skills into <home>/skills with a marker, so
    # "has content" must not count them: a user's first import onto a started gateway
    # would otherwise never install the archive's skills.
    root, home, _outside = _staged_world(tmp_path)
    (home / "skills" / "mine" / "SKILL.md").unlink()
    (home / "skills" / "mine").rmdir()
    managed = home / "skills" / "deploy-web"
    managed.mkdir()
    (managed / ".kirocrew-managed").write_text("", encoding="utf-8")
    (managed / "SKILL.md").write_text("managed", encoding="utf-8")

    assert _hold_back_foreign_skills(root, home) is False
    assert (root / "skills" / "foreign" / "SKILL.md").is_file()


def test_a_live_skill_beside_a_managed_copy_still_holds_back_the_archive(tmp_path):
    root, home, _outside = _staged_world(tmp_path)
    managed = home / "skills" / "deploy-web"
    managed.mkdir()
    (managed / ".kirocrew-managed").write_text("", encoding="utf-8")

    assert _hold_back_foreign_skills(root, home, allow_unpinned=True) is True
    assert not (root / "skills").exists()


def test_a_live_skills_folder_holding_only_links_does_not_hold_back_the_archive(tmp_path):
    root, home, outside = _staged_world(tmp_path)
    (home / "skills" / "mine" / "SKILL.md").unlink()
    (home / "skills" / "mine").rmdir()
    make_dir_link(home / "skills" / "from-an-app", outside)

    assert _hold_back_foreign_skills(root, home) is False
    assert (root / "skills" / "foreign" / "SKILL.md").is_file()


def _builtin_copy(home: Path, name: str) -> Path:
    """A skill folder as the loader's built-in sync leaves it: a copy plus its fingerprint."""
    from kiro_crew.skills_builtin_provenance import _record_builtin_provenance

    folder = home / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: " + name + "\n---\nbuilt in\n", encoding="utf-8")
    _record_builtin_provenance(folder)
    return folder


def test_a_live_skills_folder_holding_only_unchanged_builtin_copies_does_not_hold_back(tmp_path):
    root, home, _outside = _staged_world(tmp_path)
    (home / "skills" / "mine" / "SKILL.md").unlink()
    (home / "skills" / "mine").rmdir()
    _builtin_copy(home, "dashboard")

    assert _hold_back_foreign_skills(root, home) is False
    assert (root / "skills" / "foreign" / "SKILL.md").is_file()


def test_an_edited_builtin_copy_counts_as_the_users_and_holds_back_the_archive(tmp_path):
    root, home, _outside = _staged_world(tmp_path)
    (home / "skills" / "mine" / "SKILL.md").unlink()
    (home / "skills" / "mine").rmdir()
    folder = _builtin_copy(home, "dashboard")
    (folder / "SKILL.md").write_text("my own notes", encoding="utf-8")

    assert _hold_back_foreign_skills(root, home, allow_unpinned=True) is True
    assert not (root / "skills").exists()


def test_a_live_family_folder_holding_only_builtin_copies_does_not_hold_back_the_archive(
    tmp_path,
):
    # The built-in sync installs nested names such as ``kirocrew-dev/writing-tests``, so a
    # started gateway leaves a ``kirocrew-dev`` folder that is no skill itself.
    root, home, _outside = _staged_world(tmp_path)
    (home / "skills" / "mine" / "SKILL.md").unlink()
    (home / "skills" / "mine").rmdir()
    _builtin_copy(home, "kirocrew-dev/writing-tests")
    _builtin_copy(home, "kirocrew-dev/babysit")

    assert _hold_back_foreign_skills(root, home) is False
    assert (root / "skills" / "foreign" / "SKILL.md").is_file()


def test_the_app_skill_link_index_alone_does_not_hold_back_the_archive(tmp_path):
    # Every app skill registration rewrites ``.app-skill-links.json`` at the top of the
    # skills folder, so an install with an app has that file and no skill of the user's.
    root, home, outside = _staged_world(tmp_path)
    (home / "skills" / "mine" / "SKILL.md").unlink()
    (home / "skills" / "mine").rmdir()
    make_dir_link(home / "skills" / "from-an-app", outside)
    (home / "skills" / ".app-skill-links.json").write_text("{}", encoding="utf-8")

    assert _hold_back_foreign_skills(root, home) is False
    assert (root / "skills" / "foreign" / "SKILL.md").is_file()


def test_a_user_file_in_the_skills_folder_holds_back_the_archive(tmp_path):
    root, home, _outside = _staged_world(tmp_path)
    (home / "skills" / "mine" / "SKILL.md").unlink()
    (home / "skills" / "mine").rmdir()
    (home / "skills" / ".app-skill-links.json").write_text("{}", encoding="utf-8")
    (home / "skills" / "notes.md").write_text("mine", encoding="utf-8")

    assert _hold_back_foreign_skills(root, home, allow_unpinned=True) is True
    assert not (root / "skills").exists()


def test_a_user_skill_inside_a_family_folder_holds_back_the_archive(tmp_path):
    root, home, _outside = _staged_world(tmp_path)
    (home / "skills" / "mine" / "SKILL.md").unlink()
    (home / "skills" / "mine").rmdir()
    _builtin_copy(home, "kirocrew-dev/writing-tests")
    own = home / "skills" / "kirocrew-dev" / "my-own"
    own.mkdir()
    (own / "SKILL.md").write_text("mine", encoding="utf-8")

    assert _hold_back_foreign_skills(root, home, allow_unpinned=True) is True
    assert not (root / "skills").exists()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs descriptor-relative opens")
def test_an_ancestor_swapped_before_the_identity_is_read_does_not_redirect_the_removal(
    tmp_path, monkeypatch
):
    # The extraction root is pinned FIRST. An ancestor swapped for a link afterwards
    # (here, at the first step the helper takes after its early returns) is not followed
    # by the identity read or by the removal: the pinned descriptor names the original
    # root, and that is what is removed. An unrelated unpacked snapshot is left alone.
    root, home, _outside = _staged_world(tmp_path)
    other = tmp_path / "other-stage"
    (other / "root" / "skills").mkdir(parents=True)
    (other / "root" / "skills" / "keep.txt").write_text("not ours", encoding="utf-8")
    real_check = portability._staging_is_pinned

    def swap_ancestor_then_check(**kwargs):
        stage = root.parent
        stage.rename(tmp_path / "stage-moved")
        stage.symlink_to(other, target_is_directory=True)
        return real_check(**kwargs)

    monkeypatch.setattr(portability, "_staging_is_pinned", swap_ancestor_then_check)

    assert _hold_back_foreign_skills(root, home) is True

    assert (other / "root" / "skills" / "keep.txt").read_text(encoding="utf-8") == "not ours"
    assert not (tmp_path / "stage-moved" / "root" / "skills").exists()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs descriptor-relative opens")
def test_the_root_swapped_for_a_link_during_the_scan_still_removes_the_pinned_root(
    tmp_path, monkeypatch
):
    from kiro_crew import pinned_fs

    root, home, outside = _staged_world(tmp_path)
    real_scan = pinned_fs.scan_tree_pinned
    swapped: list[bool] = []

    def swap_once_then_scan(root_fd, **kwargs):
        if not swapped:
            swapped.append(True)
            _swap_root_for_link(root, outside)
        return real_scan(root_fd, **kwargs)

    monkeypatch.setattr(pinned_fs, "scan_tree_pinned", swap_once_then_scan)

    assert _hold_back_foreign_skills(root, home) is True

    assert (outside / "skills" / "keep.txt").read_text(encoding="utf-8") == "outside"
    assert not (root.with_name("root-moved") / "skills").exists()


def test_a_picked_template_keeps_a_cron_that_names_its_declared_name(tmp_path):
    home = _home(tmp_path)
    template = {"name": "reviewer", "description": "reviews"}
    z = _zip(
        tmp_path / "a.zip",
        {
            "crons.json": json.dumps({"jobs": [_risky_job(agent_id="reviewer")]}),
            "agents/package-reviewer.json": json.dumps(template),
        },
    )

    _import(z, home, agent_templates=("package-reviewer",))

    assert _read(home / "crons.json")["nightly"]["agent_id"] == "reviewer"


def test_a_corrupt_picked_template_does_not_stop_the_import(tmp_path):
    home = _home(tmp_path)
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(str(z), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
        zf.writestr("snap/crons.json", json.dumps({"jobs": [_risky_job()]}))
        zf.writestr("snap/agents/bad.json", json.dumps({"name": "bad", "pad": "x" * 4000}))
    with zipfile.ZipFile(str(z)) as zf:
        info = zf.getinfo("snap/agents/bad.json")
        start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    raw = bytearray(z.read_bytes())
    raw[start : start + 8] = b"\xff" * 8
    z.write_bytes(bytes(raw))

    summary = _import(z, home, agent_templates=("bad",))

    assert _read(home / "crons.json")["nightly"]["user_paused"] is True
    assert summary["paused_crons"] == ["nightly"]


def _archive_with_unreadable_template(path: Path, kind: str) -> Path:
    """An archive whose picked template ``agents/bad.json`` cannot be read: ``encrypted``
    sets the encryption flag, ``method`` names a compression method nobody implements."""
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
        zf.writestr("snap/crons.json", json.dumps({"jobs": [_risky_job()]}))
        zf.writestr("snap/agents/bad.json", json.dumps({"name": "bad"}))
    raw = bytearray(path.read_bytes())
    local = raw.index(b"snap/agents/bad.json") - 30  # the member's local header comes first
    central = raw.rindex(b"PK\x01\x02")  # it is the last member of the directory
    if kind == "encrypted":
        for at in (local + 6, central + 8):  # general purpose flags: bit 0 = encrypted
            raw[at] |= 0x1
    else:
        for at in (local + 8, central + 10):  # compression method
            raw[at : at + 2] = (99).to_bytes(2, "little")
    path.write_bytes(bytes(raw))
    return path


@pytest.mark.parametrize("kind", ["encrypted", "method"])
def test_an_unreadable_picked_template_is_skipped_not_raised(tmp_path, kind):
    home = _home(tmp_path)
    z = _archive_with_unreadable_template(tmp_path / "a.zip", kind)

    summary = _import(z, home, agent_templates=("bad",))

    assert _read(home / "crons.json")["nightly"]["user_paused"] is True
    assert summary["paused_crons"] == ["nightly"]


def test_import_with_the_cron_pick_installs_the_job_as_written(tmp_path):
    home = _home(tmp_path)
    z = _zip(tmp_path / "a.zip", {"crons.json": json.dumps({"jobs": [_risky_job()]})})

    summary = _import(z, home, include_crons=True)

    job = _read(home / "crons.json")["nightly"]
    assert job["enabled"] is True and job["approval_mode"] == "auto"
    assert job["env"] == {"FLAG": "1"}
    assert "paused_crons" not in summary


def _live_skill(home: Path) -> Path:
    mine = home / "skills" / "mine" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("live skill", encoding="utf-8")
    return mine


def _skills_zip(tmp_path: Path) -> Path:
    return _zip(tmp_path / "s.zip", {"skills/foreign/SKILL.md": "foreign skill"})


def test_replace_keeps_a_live_skills_tree_unless_it_is_picked(tmp_path):
    home = _home(tmp_path)
    mine = _live_skill(home)

    summary = _import(_skills_zip(tmp_path), home)

    assert mine.read_text(encoding="utf-8") == "live skill"
    assert not (home / "skills" / "foreign").exists()
    assert summary["skills_kept"] is True


def test_replace_installs_the_skills_tree_when_picked(tmp_path):
    home = _home(tmp_path)
    mine = _live_skill(home)

    summary = _import(_skills_zip(tmp_path), home, include_skills=True)

    assert (home / "skills" / "foreign" / "SKILL.md").read_text(encoding="utf-8") == "foreign skill"
    assert not mine.exists()
    assert "skills_kept" not in summary


def test_replace_installs_the_skills_tree_on_a_machine_with_no_live_skills(tmp_path):
    home = _home(tmp_path)

    summary = _import(_skills_zip(tmp_path), home)

    assert (home / "skills" / "foreign" / "SKILL.md").is_file()
    assert "skills_kept" not in summary
