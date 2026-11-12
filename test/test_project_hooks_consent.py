"""A KAS folder agent's own hooks run only with the owner's or kiro-cli's say-so.

Off by default; kiro-cli's own "Trust this workspace?" record turns them on; a
Crew turn-off beats that record; a missing, unreadable or unknown record means
off; a same-name user agent still wins; ``mcpServers`` never come along.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
import kiro_crew.project_hooks_consent as consent
from kiro_crew import kiro_cli_workspace_trust as kct
from kiro_crew.acp.harness.kas import resolve_projected_spec
from kiro_crew.agent_sdk import spec_hooks

_HOOKS = {"preToolUse": [{"matcher": "*", "command": "echo checkout-hook"}]}


@pytest.fixture
def layout(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    user = tmp_path / "user-agents"
    user.mkdir()
    mates = tmp_path / "mate-agents"
    mates.mkdir()
    checkout = tmp_path / "checkout"
    project = checkout / ".kiro" / "agents"
    project.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    store = tmp_path / "crew" / "project_hooks_consent.json"
    store.parent.mkdir()
    monkeypatch.setattr(consent, "project_hooks_consent_path", lambda: store)
    (project / "proj.json").write_text(
        json.dumps(
            {
                "name": "proj",
                "prompt": "p",
                "hooks": _HOOKS,
                "mcpServers": {"planted": {"command": "/bin/sh", "args": ["-c", "id"]}},
            }
        ),
        encoding="utf-8",
    )
    return home, user, checkout


def _kiro_cli_trust(home: Path, folder: Path, text: str, name: str = "permissions.yaml") -> Path:
    root = kct._normalize(os.path.realpath(folder), fold=os.name == "nt")
    bucket = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
    path = home / ".kiro" / "workspace-roots" / bucket / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _hooks(checkout: Path) -> dict:
    return spec_hooks._agent_spec("proj", None, kas_checkout=str(checkout))


# -- kiro-cli's record ------------------------------------------------------


def test_normalize_matches_kiro_cli_collapse():
    assert kct._normalize("/a//b/./c/../d/", fold=False) == "/a/b/d"
    assert kct._normalize("/", fold=False) == "/"


def test_kiro_cli_record_alone_turns_hooks_on(layout):
    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, "rules: []\nworkspaceTrusted: true\n")
    assert kct.kiro_cli_trusts_folder(checkout) is True
    assert consent.hooks_enabled(checkout) == (True, consent.SOURCE_KIRO_CLI)
    assert _hooks(checkout) == {"hooks": _HOOKS}


def test_a_full_kiro_cli_permissions_yaml_still_trusts(layout):
    # The shape kiro-cli writes: rules with nested mappings, then the grant. Nested
    # keys are never read, so a rule's own keys never refuse the record.
    home, _user, checkout = layout
    text = (
        "rules:\n"
        "  - capability: shell\n"
        "    match: ['git status']\n"
        "    effect: allow\n"
        "  - capability: fs_write\n"
        "    match: ['src/**']\n"
        "    effect: ask\n"
        "workspaceTrusted: true\n"
    )
    _kiro_cli_trust(home, checkout, text)
    assert kct.kiro_cli_trusts_folder(checkout) is True


@pytest.mark.parametrize("word", ["True", "TRUE"])
def test_each_yaml_1_2_spelling_of_true_trusts(layout, word):
    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, f"rules: []\nworkspaceTrusted: {word}\n")
    assert kct.kiro_cli_trusts_folder(checkout) is True


def test_normalize_keeps_a_bare_drive_letter_and_trims_others(monkeypatch):
    monkeypatch.setattr(kct.os.path, "isabs", lambda p: True)
    assert kct._normalize("C:/", fold=False) == "C:/"
    assert kct._normalize("C:/Work/", fold=False) == "C:/Work"
    assert kct._normalize("\u00e9:/", fold=False) == "\u00e9:"  # not an ASCII drive letter


def test_windows_case_mode_splits_on_either_separator(monkeypatch):
    probed: list[str] = []

    def stat_id(path):
        probed.append(path)
        return (1, len(probed))

    monkeypatch.setattr(kct, "_stat_id", stat_id)
    monkeypatch.setattr(kct.os, "stat", lambda p: (_ for _ in ()).throw(FileNotFoundError()))
    assert kct._windows_case_mode("C:\\Work/Sub\\Repo") == "exact"
    assert probed == ["C:\\Work", "C:\\Work\\Sub", "C:\\Work\\Sub\\Repo"]


def test_kiro_cli_json_record_is_read_when_yaml_is_absent(layout):
    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, '{"rules": [], "workspaceTrusted": true}', "permissions.json")
    assert kct.kiro_cli_trusts_folder(checkout) is True


@pytest.mark.parametrize(
    "text",
    [
        "rules: []\nworkspaceTrusted: false\n",
        "workspaceTrusted: yes\n",  # YAML 1.1 only; kiro-cli's parser reads a string
        "workspaceTrusted: true\nworkspaceTrusted: true\n",  # duplicate key: kiro-cli refuses
        "workspaceTrusted: 'true'\n",  # quoted: a string, not a boolean
        'workspaceTrusted: "true"\n',
        "workspaceTrusted: tRue\n",  # not one of YAML 1.2's spellings
        "workspaceTrusted: !!str true\n",  # explicitly a string
        # A top-level key that is not a plain string refuses the whole record:
        "workspaceTrusted: true\nnan: a\nnan: b\n",
        "workspaceTrusted: true\n010000000000000000001: a\n10000000000000001: b\n",
        "'workspaceTrusted': true\n",  # a quoted key
        "workspaceTrusted: true\ntrue: a\n",  # a bool key
        "workspaceTrusted: true\n? [a]\n: x\n",  # a non-scalar key
        "workspaceTrusted: true\nworkspaceTrustCase: exact\n",  # other case mode
        "[workspaceTrusted, true]\n",  # not a mapping
        "workspaceTrusted: [\n",  # unparsable
        "   \n",  # blank reads as absent
    ],
)
def test_bad_or_untrusted_kiro_cli_record_means_off(layout, text):
    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, text)
    assert kct.kiro_cli_trusts_folder(checkout) is False
    assert _hooks(checkout) == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO and symlink")
@pytest.mark.parametrize("shape", ["fifo", "symlink", "directory", "oversize"])
def test_an_odd_kiro_cli_record_is_refused_without_blocking(layout, tmp_path, shape):
    # The record sits where a sandboxed agent can write. A FIFO must not block the
    # open, a link to /dev/zero must not stream forever, and neither must count as
    # missing (which would fall through to the .json record): each reads as off.
    home, _user, checkout = layout
    path = _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")
    _kiro_cli_trust(home, checkout, '{"workspaceTrusted": true}', "permissions.json")
    if shape == "oversize":
        path.write_text(
            "workspaceTrusted: true\n" + "#" * (kct._MAX_POLICY_BYTES + 1), encoding="utf-8"
        )
    else:
        path.unlink()
        if shape == "fifo":
            os.mkfifo(path)
        elif shape == "symlink":
            real = tmp_path / "real.yaml"
            real.write_text("workspaceTrusted: true\n", encoding="utf-8")
            path.symlink_to(real)
        else:
            path.mkdir()
    assert kct.kiro_cli_trusts_folder(checkout) is False


def test_missing_or_non_utf8_kiro_cli_record_means_off(layout):
    home, _user, checkout = layout
    assert kct.kiro_cli_trusts_folder(checkout) is False
    path = _kiro_cli_trust(home, checkout, "")
    path.write_bytes(b"workspaceTrusted: true\n\xff")
    assert kct.kiro_cli_trusts_folder(checkout) is False


def test_kiro_cli_grant_for_home_is_never_read(layout):
    home, _user, _checkout = layout
    _kiro_cli_trust(home, home, "workspaceTrusted: true\n")
    assert kct.kiro_cli_trusts_folder(home) is False


def test_kiro_cli_dropping_trust_stops_hooks(layout):
    home, _user, checkout = layout
    path = _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")
    assert _hooks(checkout) == {"hooks": _HOOKS}
    path.unlink()
    assert _hooks(checkout) == {}


# -- Crew's own choice ------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX link refusal")
def test_a_saved_choice_is_rewritten_in_place_and_a_link_is_refused(layout, tmp_path):
    # A running sandbox bind-mounts the record's inode read-only; a rename over the
    # name would leave the new file writable from inside that sandbox.
    _home, _user, checkout = layout
    consent.set_choice(checkout, True)
    path = consent.project_hooks_consent_path()
    inode = os.stat(path).st_ino
    consent.set_choice(checkout, False)
    assert os.stat(path).st_ino == inode
    assert consent.crew_choice(checkout) == consent.OFF
    target = tmp_path / "elsewhere.json"
    before = path.read_text(encoding="utf-8")
    target.write_text(before, encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(OSError):
        consent.set_choice(checkout, True)
    assert target.read_text(encoding="utf-8") == before


def test_a_read_takes_the_writers_lock(layout, monkeypatch):
    # An in-place save passes through partial bytes under the writers' lock, so the
    # reader must hold that same lock while it reads, or it can see a torn record
    # and answer UNREADABLE (hooks off) for a folder whose choice never changed.
    _home, _user, checkout = layout
    consent.set_choice(checkout, True)
    held: list[bool] = []
    real_load = consent._load

    def load():
        held.append(consent._LOCK.locked())
        return real_load()

    monkeypatch.setattr(consent, "_load", load)
    assert consent.crew_choice(checkout) == consent.ON
    assert held == [True]


def test_a_failed_save_keeps_the_previous_choices(layout, monkeypatch):
    # A full disk mid-write must not leave a torn record that turns every folder off.
    import errno

    _home, _user, checkout = layout
    consent.set_choice(checkout, True)
    path = consent.project_hooks_consent_path()
    before = path.read_bytes()
    real_write = consent.os.write
    calls = {"n": 0}

    def short_then_full(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            real_write(fd, bytes(data[:5]))
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(consent.os, "write", short_then_full)
    monkeypatch.delattr(consent.os, "posix_fallocate", raising=False)
    with pytest.raises(OSError):
        consent.set_choice(checkout, False)
    assert path.read_bytes() == before
    assert consent.crew_choice(checkout) == consent.ON


def test_a_failed_first_save_leaves_no_record_behind(layout, monkeypatch):
    # The record did not exist; a full disk on the first save must not leave an
    # empty file that _load reads as damaged forever.
    import errno

    _home, _user, checkout = layout
    path = consent.project_hooks_consent_path()
    assert not path.exists()

    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    with monkeypatch.context() as mp:
        mp.setattr(consent.os, "write", full)
        mp.setattr(consent.os, "posix_fallocate", full, raising=False)
        with pytest.raises(OSError):
            consent.set_choice(checkout, True)
    assert not path.exists()
    assert consent.crew_choice(checkout) is None
    consent.set_choice(checkout, True)
    assert consent.crew_choice(checkout) == consent.ON


def test_a_too_long_folder_hook_is_named_in_the_overlong_notice(layout):
    # The firing path drops a folder agent's too-long hook, so the notice path must
    # read the same folder hooks to say so.
    _home, _user, checkout = layout
    consent.set_choice(checkout, True)
    spec_path = checkout / ".kiro" / "agents" / "proj.json"
    long_cmd = "echo " + "x" * (spec_hooks._MAX_COMMAND_LEN + 1)
    spec_path.write_text(
        json.dumps({"name": "proj", "prompt": "p", "hooks": {"stop": [{"command": long_cmd}]}}),
        encoding="utf-8",
    )
    assert spec_hooks.overlong_spec_hook_commands("proj") == []  # without the folder
    found = spec_hooks.overlong_spec_hook_commands("proj", kas_checkout=str(checkout))
    assert [h.length for h in found] == [len(long_cmd)]


def test_off_by_default(layout):
    _home, _user, checkout = layout
    assert consent.hooks_enabled(checkout) == (False, "")
    assert _hooks(checkout) == {}


def test_crew_consent_turns_hooks_on_and_revoke_turns_them_off(layout):
    _home, _user, checkout = layout
    consent.set_choice(checkout, True)
    assert _hooks(checkout) == {"hooks": _HOOKS}
    spec, origin = spec_hooks._agent_spec_with_origin("proj", None, kas_checkout=str(checkout))
    assert origin == "project"  # vetted against the full deny list
    assert "mcpServers" not in spec
    consent.set_choice(checkout, False)
    assert _hooks(checkout) == {}


def test_crew_turn_off_beats_kiro_cli_trust(layout):
    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")
    consent.set_choice(checkout, False)
    assert consent.hooks_enabled(checkout) == (False, consent.SOURCE_CREW)
    assert _hooks(checkout) == {}


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"folders": []}',
        '{"folders": {"/x": "maybe"}}',
        '{"kiro_cli_notified": "x"}',
    ],
)
def test_a_damaged_crew_store_turns_hooks_off_without_asking_kiro_cli(layout, text):
    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")
    consent.project_hooks_consent_path().write_text(text, encoding="utf-8")
    assert consent.crew_choice(checkout) == consent.UNREADABLE
    assert consent.hooks_enabled(checkout) == (False, consent.SOURCE_CREW)
    assert _hooks(checkout) == {}


def test_a_corrupt_record_after_a_turn_off_keeps_hooks_off_under_kiro_cli_trust(layout):
    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")
    consent.set_choice(checkout, False)
    store = consent.project_hooks_consent_path()
    store.write_text(store.read_text(encoding="utf-8")[:-3], encoding="utf-8")  # truncated
    assert consent.hooks_enabled(checkout) == (False, consent.SOURCE_CREW)
    assert _hooks(checkout) == {}


def test_an_absent_crew_store_leaves_kiro_cli_trust_in_charge(layout):
    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")
    assert not consent.project_hooks_consent_path().exists()
    assert consent.hooks_enabled(checkout) == (True, consent.SOURCE_KIRO_CLI)


def test_a_writer_never_rewrites_an_unreadable_store(layout):
    _home, _user, checkout = layout
    store = consent.project_hooks_consent_path()
    store.write_text("{not json", encoding="utf-8")
    with pytest.raises(consent.StoreUnreadable):
        consent.set_choice(checkout, True)
    assert consent.mark_kiro_cli_notice_shown(checkout) is False
    assert store.read_text(encoding="utf-8") == "{not json"


def test_only_a_live_bots_file_withholds_folder_hooks(layout, tmp_path, monkeypatch):
    # A deleted Bot's leftover file yields to the folder agent, as the projection
    # does (kas._bot_owns_mate_file); a live Bot's own file wins.
    import kiro_crew.mate_capabilities as mate_caps

    _home, _user, checkout = layout
    consent.set_choice(checkout, True)
    (tmp_path / "mate-agents" / "proj.json").write_text(
        json.dumps({"name": "proj", "prompt": "p", "hooks": {"stop": [{"command": "echo bot"}]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mate_caps, "binding_is_mate", lambda member: False)
    assert _hooks(checkout) == {"hooks": _HOOKS}
    assert spec_hooks.kas_project_hooks_state("proj", str(checkout)).state == "on"
    monkeypatch.setattr(mate_caps, "binding_is_mate", lambda member: member == "proj")
    assert _hooks(checkout) == {}
    assert spec_hooks.kas_project_hooks_state("proj", str(checkout)).state == ""


def test_same_name_user_agent_still_wins(layout):
    home, user, checkout = layout
    _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")
    (user / "proj.json").write_text(
        json.dumps({"name": "proj", "prompt": "mine"}), encoding="utf-8"
    )
    assert _hooks(checkout) == {"name": "proj", "prompt": "mine"}


def test_a_full_record_refuses_a_new_folder_and_keeps_every_turn_off(layout, monkeypatch, tmp_path):
    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")
    consent.set_choice(checkout, False)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(consent, "_MAX_FOLDERS", 1)
    with pytest.raises(consent.StoreFull):
        consent.set_choice(other, True)
    assert consent.hooks_enabled(checkout) == (False, consent.SOURCE_CREW)
    consent.set_choice(checkout, True)  # a recorded folder can still change
    assert consent.crew_choice(checkout) == consent.ON


def test_the_folder_key_keeps_case(layout, tmp_path):
    folder = tmp_path / "MixedCase"
    folder.mkdir()
    assert consent.folder_key(folder).endswith("MixedCase")


def test_kiro_cli_notice_shows_once_per_folder(layout):
    _home, _user, checkout = layout
    assert consent.mark_kiro_cli_notice_shown(checkout) is True
    assert consent.mark_kiro_cli_notice_shown(checkout) is False


def test_notice_state_follows_the_choice_and_names_the_file_and_commands(layout):
    home, _user, checkout = layout
    found = spec_hooks.kas_project_hooks_state("proj", str(checkout))
    assert found.state == "off"
    assert found.path.endswith("proj.json")
    assert found.hooks == (("PreToolUse", "echo checkout-hook"),)
    _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")
    assert spec_hooks.kas_project_hooks_state("proj", str(checkout)).state == "kiro_cli"
    consent.set_choice(checkout, True)
    assert spec_hooks.kas_project_hooks_state("proj", str(checkout)).state == "on"


# -- the KAS projection -----------------------------------------------------


def test_projection_keeps_hooks_only_with_a_yes_and_never_servers(layout):
    _home, user, checkout = layout
    notices: list[str] = []
    spec, _dir = resolve_projected_spec(user, "proj", checkout, notices)
    assert "hooks" not in spec and "mcpServers" not in spec
    assert any("hooks" in n for n in notices)
    consent.set_choice(checkout, True)
    notices = []
    spec, _dir = resolve_projected_spec(user, "proj", checkout, notices)
    assert spec["hooks"] == _HOOKS
    assert "mcpServers" not in spec
    assert not any("hooks" in n for n in notices)


# -- the owner-gated writer -------------------------------------------------


def _request(*, user: str = "owner-1", owner: str = "owner-1", body=None):
    """A request shaped like a dashboard owner call (see test_credential_redaction_switch.py)."""
    from unittest.mock import AsyncMock, MagicMock

    req = MagicMock()
    req.path = "/api/project-hooks/consent"
    store = {"app": "", "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.mark.asyncio
async def test_the_owner_reads_one_folders_real_state_back(layout, tmp_path):
    # The card asks after a reload; a Turn on must read back as on, nothing else leaks.
    from kiro_crew.dashboard.handlers.project_hooks_consent import (
        api_project_hooks_consent_get,
        api_project_hooks_consent_put,
    )

    _home, _user, checkout = layout

    async def read(folder, **kw):
        req = _request(**kw)
        req.query = {"folder": folder}
        resp = await api_project_hooks_consent_get(req)
        return resp.status, (json.loads(resp.body) if resp.status == 200 else None), resp

    status, body, resp = await read(str(checkout))
    assert (status, body) == (200, {"state": "unset", "enabled": False})
    assert resp.headers.get("Cache-Control") == "no-store"
    await api_project_hooks_consent_put(_request(body={"folder": str(checkout), "enabled": True}))
    assert (await read(str(checkout)))[:2] == (200, {"state": "on", "enabled": True})
    other = tmp_path / "other-folder"
    other.mkdir()
    assert (await read(str(other)))[1] == {"state": "unset", "enabled": False}  # no leak

    assert (await read(str(checkout), user="someone-else"))[0] == 403
    assert (await read("relative"))[0] == 400
    assert (await read(str(tmp_path / "gone")))[0] == 400
    assert (await read(""))[0] == 400


@pytest.mark.asyncio
async def test_a_damaged_record_reads_back_as_off(layout):
    from kiro_crew.dashboard.handlers.project_hooks_consent import api_project_hooks_consent_get

    home, _user, checkout = layout
    _kiro_cli_trust(home, checkout, "workspaceTrusted: true\n")  # would turn them on
    consent.project_hooks_consent_path().write_text("{not json", encoding="utf-8")
    req = _request()
    req.query = {"folder": str(checkout)}
    resp = await api_project_hooks_consent_get(req)
    assert json.loads(resp.body) == {"state": "off", "enabled": False}


@pytest.mark.asyncio
async def test_writer_refuses_a_non_owner(layout):
    from kiro_crew.dashboard.handlers.project_hooks_consent import api_project_hooks_consent_put

    _home, _user, checkout = layout
    resp = await api_project_hooks_consent_put(
        _request(user="someone-else", body={"folder": str(checkout), "enabled": True})
    )
    assert resp.status == 403
    assert consent.crew_choice(checkout) is None


@pytest.mark.asyncio
async def test_writer_records_the_owner_choice(layout):
    from kiro_crew.dashboard.handlers.project_hooks_consent import api_project_hooks_consent_put

    _home, _user, checkout = layout
    resp = await api_project_hooks_consent_put(
        _request(body={"folder": str(checkout), "enabled": True})
    )
    assert resp.status == 200
    assert consent.crew_choice(checkout) == consent.ON
    bad = await api_project_hooks_consent_put(
        _request(body={"folder": "relative", "enabled": True})
    )
    assert bad.status == 400


def test_notice_command_rows_redact_before_they_cut():
    # A credential crossing the cut must not survive as an unredacted fragment.
    import hashlib

    # chat_runner binds this module's shared names (the dashboard facade), as at runtime.
    import kiro_crew.dashboard.chat_runner  # noqa: F401
    from kiro_crew.dashboard.chat_turn import turn_spec_hooks as tsh

    key = "A" + "KIA" + hashlib.sha256(b"p134-cut").hexdigest().upper()[:16]
    command = "x" * (tsh._PROJECT_HOOK_COMMAND_CHARS - 9) + " " + key
    [row] = tsh._project_hook_rows((("PreToolUse", command),))
    assert key[:8] not in row["command"]
    assert "(cut, " in row["command"]


# -- cron is unchanged ------------------------------------------------------


def _kas_provider(checkout: Path):
    from types import SimpleNamespace

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    return SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd=str(checkout))


def _cron_hooks(checkout: Path, agent: str):
    import asyncio

    from kiro_crew.cron_service.turn_hooks import CronTurnHooks

    hooks = asyncio.run(
        CronTurnHooks.prepare(
            _kas_provider(checkout), agent, f"cron:job-1:{agent}", "run", fresh_session=True
        )
    )
    return [(h.event, h.command) for h in hooks._hooks.spec.hooks if h.spec_origin != "user_file"]


def test_a_cron_turn_in_a_turned_on_folder_runs_no_project_hooks(layout):
    import asyncio

    _home, _user, checkout = layout
    consent.set_choice(checkout, True)
    chat = asyncio.run(spec_hooks.turn_spec_hooks(_kas_provider(checkout), "proj"))
    assert [h.command for h in chat.hooks if h.spec_origin == "project"] == ["echo checkout-hook"]
    assert _cron_hooks(checkout, "proj") == []


def test_a_cron_turn_still_runs_user_level_spec_hooks(layout):
    _home, user, checkout = layout
    consent.set_choice(checkout, True)
    (user / "mine.json").write_text(
        json.dumps({"name": "mine", "prompt": "p", "hooks": {"stop": [{"command": "echo mine"}]}}),
        encoding="utf-8",
    )
    assert _cron_hooks(checkout, "mine") == [("Stop", "echo mine")]


def test_a_cron_projection_keeps_no_folder_hooks(layout):
    _home, user, checkout = layout
    consent.set_choice(checkout, True)
    spec, _dir = resolve_projected_spec(user, "proj", checkout, [], project_hooks=False)
    assert "hooks" not in spec


def test_a_human_turn_in_a_cron_keyed_session_matches_its_projection(layout):
    """A dashboard follow-up in a cron tab keeps the ``cron:`` key. Its projection
    drops the folder's hooks, so its firing and its stale check must too, or every
    re-projection stays stale and admission raises StaleProjectionError."""
    import asyncio

    from kiro_crew.acp.harness.kas import KasHarness
    from kiro_crew.acp.kas_agents import projected_auto_approved

    _home, _user, checkout = layout
    consent.set_choice(checkout, True)
    extras = asyncio.run(
        KasHarness().session_extras("proj", work_dir=str(checkout), session_key="cron:job-1")
    )
    provider = _kas_provider(checkout)
    provider._session_key = "cron:job-1"
    provider.kas_auto_approved_capabilities = projected_auto_approved(extras.custom_agents, "proj")
    provider.kas_projected_agent = "proj"
    fired = asyncio.run(spec_hooks.turn_spec_hooks(provider, "proj"))
    assert [h for h in fired.hooks if h.spec_origin == "project"] == []
    assert asyncio.run(spec_hooks.hook_projection_stale(provider, "proj")) is False


@pytest.mark.parametrize("key, expected", [("cron:job-1", False), ("dashboard:s1", True)])
def test_a_native_subagent_target_follows_the_sessions_cron_rule(monkeypatch, key, expected):
    # A cron session's native sub-agent targets are projected without folder hooks,
    # as its own agent is, so a folder helper keeps its auto-approvals there.
    import kiro_crew.agent as agent_mod_
    from kiro_crew.acp import kas_agents as kas_agents_mod
    from kiro_crew.acp import kas_subagent_targets as targets
    from kiro_crew.acp.harness import kas as kas_harness

    seen: list[bool] = []

    def resolve(*_a, project_hooks=True, **_k):
        seen.append(project_hooks)
        raise kas_agents_mod.KasAgentSpecAbsent("helper", Path("/nowhere"))

    monkeypatch.setattr(kas_harness, "resolve_projected_spec", resolve)
    monkeypatch.setattr(agent_mod_, "require_fork_governance", lambda *_a: None)
    monkeypatch.setattr(agent_mod_, "require_fresh_derived_spec", lambda *_a: None)
    targets._project_target(
        "helper",
        "/work/repo",
        stub_server_names=frozenset(),
        session_key=key,
        withheld_out=None,
        engine_features=frozenset(),
        notices=[],
        settings_dir=None,
        parent_servers=frozenset(),
        parent_declared={},
        session_hooks=(),
        forward_ssh_auth_sock=False,
    )
    assert seen == [expected]


@pytest.mark.parametrize(
    "state, saved", [("off", "turned them on"), ("on", "are off"), ("kiro_cli", "are off")]
)
def test_the_notice_carries_the_text_for_the_state_a_click_leads_to(state, saved):
    # The card shows this after a successful click, so "Saved" never sits next to
    # the state the owner just changed.
    import kiro_crew.dashboard.chat_runner  # noqa: F401
    from kiro_crew.dashboard.chat_turn import turn_spec_hooks as tsh

    text = tsh._project_hooks_saved_text("proj", state, "/work/repo/.kiro/agents/proj.json")
    assert saved in text and "/work/repo/.kiro/agents/proj.json" in text


def test_the_notice_lists_every_hook_crew_would_fire():
    # "Turn on" enables every hook Crew reads (up to _MAX_SPEC_HOOKS), so the
    # notice's command list must show each one.
    import kiro_crew.dashboard.chat_runner  # noqa: F401
    from kiro_crew.dashboard.chat_turn import turn_spec_hooks as tsh

    hooks = tuple(("PreToolUse", f"echo {i}") for i in range(spec_hooks._MAX_SPEC_HOOKS))
    assert len(tsh._project_hook_rows(hooks)) == spec_hooks._MAX_SPEC_HOOKS


def test_a_cut_command_row_says_it_was_cut_and_how_long_it_is():
    import kiro_crew.dashboard.chat_runner  # noqa: F401
    from kiro_crew.dashboard.chat_turn import turn_spec_hooks as tsh

    command = "echo " + "a" * 1229
    [row] = tsh._project_hook_rows((("PreToolUse", command),))
    assert row["command"].endswith("… (cut, 1234 chars)")
    assert row["command"].startswith(command[: tsh._PROJECT_HOOK_COMMAND_CHARS])
    [short] = tsh._project_hook_rows((("PreToolUse", "echo hi"),))
    assert short["command"] == "echo hi"


# -- a sub-agent session in another folder ----------------------------------


def test_a_session_in_folder_b_projects_and_fires_b_and_is_not_stale(layout, tmp_path, monkeypatch):
    """Runtime started in A (hooks off), session in B (hooks on): the projection and
    the firing read the same folder, so B's hooks are projected, fired, and the
    session's batch is never stale for them."""
    import asyncio

    from kiro_crew.acp.harness.kas import KasHarness
    from kiro_crew.acp.kas_agents import projected_auto_approved

    _home, _user, folder_b = layout
    folder_a = tmp_path / "folder-a"
    (folder_a / ".kiro" / "agents").mkdir(parents=True)
    (folder_a / ".kiro" / "agents" / "proj.json").write_text(
        (folder_b / ".kiro" / "agents" / "proj.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    consent.set_choice(folder_a, False)
    consent.set_choice(folder_b, True)

    extras = asyncio.run(
        KasHarness().session_extras("proj", work_dir=str(folder_a), session_cwd=str(folder_b))
    )
    approved = projected_auto_approved(extras.custom_agents, "proj")

    provider = _kas_provider(folder_b)
    provider.kas_auto_approved_capabilities = approved
    provider.kas_projected_agent = "proj"
    fired = asyncio.run(spec_hooks.turn_spec_hooks(provider, "proj"))
    assert [h.command for h in fired.hooks if h.spec_origin == "project"] == ["echo checkout-hook"]
    assert asyncio.run(spec_hooks.hook_projection_stale(provider, "proj")) is False


# -- kiro-cli reader: KIRO_HOME, Windows case mode, odd files ---------------


def test_kiro_home_follows_kiro_cli_rules(monkeypatch, tmp_path):
    # Native rules on each OS: tmp_path is a native path, and a Windows path is
    # never absolute under POSIX rules, so forcing POSIX would test nothing there.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    fallback = os.path.join(str(home), ".kiro")
    monkeypatch.delenv("KIRO_HOME", raising=False)
    assert kct._kiro_home() == fallback
    monkeypatch.setenv("KIRO_HOME", "relative/dir")
    assert kct._kiro_home() == fallback
    monkeypatch.setenv("KIRO_HOME", "/")
    assert kct._kiro_home() == fallback
    monkeypatch.setenv("KIRO_HOME", str(tmp_path))  # contains home
    assert kct._kiro_home() == fallback
    other = tmp_path / "elsewhere" / "not-yet"  # missing tail is kept
    monkeypatch.setenv("KIRO_HOME", f"  {other}  ")
    assert kct._kiro_home() == str(other)


def _fake_stat(table):
    import os as _os

    def stat(path, *a, **k):
        entry = table.get(path)
        if isinstance(entry, BaseException):
            raise entry
        if entry is None:
            raise FileNotFoundError(path)
        return _os.stat_result((0, entry[1], entry[0], 0, 0, 0, 0, 0, 0, 0))

    return stat


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        ((1, 7), "folded"),  # same file under the other case: case folds
        (None, "exact"),  # other spelling missing: case-sensitive
        (NotADirectoryError("x"), "exact"),
        ((1, 8), "exact"),  # a different file
        ((1, 0), None),  # zero file id settles nothing
        (PermissionError("x"), None),
    ],
)
def test_windows_case_mode(monkeypatch, other, expected):
    table = {"C:\\Repo": (1, 7), "C:\\repo": other}
    monkeypatch.setattr(kct.os, "stat", _fake_stat(table))
    assert kct._windows_case_mode("C:\\Repo") == expected


def test_windows_case_mode_refuses_an_unreadable_component(monkeypatch):
    monkeypatch.setattr(kct.os, "stat", _fake_stat({}))
    assert kct._windows_case_mode("C:\\Repo") is None


def test_windows_identity_strips_the_long_prefix_and_keeps_case(monkeypatch):
    monkeypatch.setattr(kct, "_is_windows", lambda: True)
    monkeypatch.setattr(kct.os.path, "realpath", lambda p, strict=False: "\\\\?\\C:\\Work\\Repo")
    monkeypatch.setattr(kct, "_stat_id", lambda p: (1, 1))
    monkeypatch.setattr(kct, "_windows_case_mode", lambda physical: "exact")
    monkeypatch.setattr(kct, "_normalize", lambda p, fold: p.replace("\\", "/"))
    root, mode = kct._identity("C:\\Work\\Repo")
    assert mode == "exact" and root == "c:/Work/Repo"
    monkeypatch.setattr(kct, "_windows_case_mode", lambda physical: None)
    assert kct._identity("C:\\Work\\Repo") is None


def test_windows_identity_keeps_the_unc_root_of_a_long_unc_path(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(kct, "_is_windows", lambda: True)
    monkeypatch.setattr(
        kct.os.path, "realpath", lambda p, strict=False: "\\\\?\\UNC\\server\\share\\repo"
    )
    monkeypatch.setattr(kct, "_stat_id", lambda p: (1, 1))
    monkeypatch.setattr(
        kct, "_windows_case_mode", lambda physical: seen.append(physical) or "folded"
    )
    monkeypatch.setattr(kct, "_normalize", lambda p, fold: p.replace("\\", "/"))
    root, _mode = kct._identity("\\\\server\\share\\repo")
    assert seen == ["\\\\server\\share\\repo"]
    assert root == "//server/share/repo"


def test_identity_refuses_a_path_that_does_not_resolve_or_differs(monkeypatch, tmp_path):
    assert kct._identity(str(tmp_path / "missing")) is None
    monkeypatch.setattr(kct, "_stat_id", lambda p: None)
    assert kct._identity(str(tmp_path)) is None


def test_exact_case_bucket_differs_from_folded():
    assert kct._bucket("/w/Repo", "exact") != kct._bucket("/w/Repo", "folded")


def test_an_unreadable_policy_entry_reads_as_not_trusted(layout):
    home, _user, checkout = layout
    path = _kiro_cli_trust(home, checkout, "x")
    path.unlink()
    path.mkdir()  # a directory where the yaml should be: unreadable
    assert kct.kiro_cli_trusts_folder(checkout) is False
    assert kct.kiro_cli_trusts_folder(None) is False


def test_os_home_falls_back_to_the_platform_lookup(monkeypatch):
    monkeypatch.setattr(kct, "_is_windows", lambda: True)
    monkeypatch.setenv("USERPROFILE", "")
    assert kct._os_home() == kct.os.path.expanduser("~")


# -- the writer's refusals --------------------------------------------------


@pytest.mark.asyncio
async def test_writer_refuses_bad_json_with_400(layout):
    from kiro_crew.dashboard.handlers.project_hooks_consent import api_project_hooks_consent_put

    req = _request(body={})
    req.json.side_effect = ValueError("bad")
    resp = await api_project_hooks_consent_put(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_writer_refuses_a_missing_folder_with_400(layout, tmp_path):
    from kiro_crew.dashboard.handlers.project_hooks_consent import api_project_hooks_consent_put

    resp = await api_project_hooks_consent_put(
        _request(body={"folder": str(tmp_path / "gone"), "enabled": True})
    )
    assert resp.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "status"), [(consent.StoreFull("full"), 409), (OSError("disk"), 500)]
)
async def test_writer_maps_store_errors(layout, monkeypatch, raised, status):
    from kiro_crew.dashboard.handlers import project_hooks_consent as handler

    def boom(*_a, **_k):
        raise raised

    monkeypatch.setattr(handler.project_hooks_consent, "set_choice", boom)
    _home, _user, checkout = layout
    resp = await handler.api_project_hooks_consent_put(
        _request(body={"folder": str(checkout), "enabled": True})
    )
    assert resp.status == status


def test_audit_failure_never_raises(monkeypatch):
    from kiro_crew.dashboard.handlers import project_hooks_consent as handler

    def broken():
        raise RuntimeError("no sel")

    monkeypatch.setattr("kiro_crew.sel.sel", broken)
    handler._audit(caller="c", outcome="enabled")
