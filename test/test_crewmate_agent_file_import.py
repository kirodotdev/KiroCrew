"""``dashboard.import_agent_files``: one agent definition file through the Bot import card.

An agent definition file is Markdown: a front matter block, then the prompt. With the setting
on, ``POST /api/members/import`` raises the same card a ``.kirobot`` file does; with it off
(the default) such a file gets main's refusal, as every file that is not a zip does.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile

import change_cards_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import crewmate_stage
from kiro_crew.change_card_catalog import CardCatalogError
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crewmate_bundle import JSON_MAX_BYTES, crewmate_import_params, read_crewmate_file
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.testing.synthetic_credentials import github_classic

world = change_cards_helpers.world

AGENT_FILE = (
    "---\n"
    "name: code-reviewer\n"
    "description: Reviews a change for bugs before it merges.\n"
    "model: sonnet\n"
    "tools: Read, Grep, Bash\n"
    "hooks: {PreToolUse: [./guard.sh]}\n"
    "color: blue\n"
    "---\n"
    "Read the diff first.\n"
    "Name each bug with its file and line.\n"
)
RULES = "Read the diff first.\nName each bug with its file and line."
ZIP = {"Content-Type": "application/zip"}
REFUSAL = (400, {"error": "this is not a .kirobot file", "code": "invalid_bundle"})


async def _no_sweep(store) -> None:
    return None


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(routes, "_run_stage_sweep", _no_sweep)
    _floor_monkeypatch.setattr(routes, "_STAGE_SWEEP", None)


def _setting(tmp_path, on: bool) -> None:
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    config = json.dumps({"dashboard": {"import_agent_files": on}})
    (tmp_path / "home" / "config.json").write_text(config, encoding="utf-8")


def _kirobot() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        manifest = {"format": "kirocrew.kirobot", "formatVersion": 1, "name": "Release Captain"}
        zf.writestr("kirobot.json", json.dumps(manifest))
        zf.writestr("agent.json", "{}")
        zf.writestr("rules.json", json.dumps({"rules": "Never push to main."}))
    return buf.getvalue()


async def _owner_gate(request: web.Request):
    if request.headers.get("X-Test-Auth") == "owner" and request.get("app") == "":
        return None
    return web.json_response({"error": "owner only", "code": "owner_only"}, status=403)


def _post(world, monkeypatch, tmp_path, data: bytes, auth: str = "owner"):
    async def read_state(kind, params, evidence, *, state, app):
        return {"exists": False, "inherited_auto_approve": []}

    monkeypatch.setattr(cards, "read_state", read_state)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
        lambda request, op: _owner_gate(request),
    )
    store = CardStore(tmp_path / "c.json")

    async def main():
        app = change_cards_helpers._app(world, store)
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/api/members/import", data=data, headers={**ZIP, "X-Test-Auth": auth})
            return r.status, await r.json()

    status, body = asyncio.run(main())
    return status, body, store


def test_the_setting_is_off_by_default():
    assert KiroCrewConfig().dashboard.import_agent_files is False


def test_with_the_setting_on_an_agent_file_raises_one_card(world, monkeypatch, tmp_path):
    _setting(tmp_path, True)
    data = AGENT_FILE.encode()
    status, body, store = _post(world, monkeypatch, tmp_path, data)
    assert status == 200, body
    params = store.get(body["id"])["params"]
    assert params["name"] == "code-reviewer"
    assert params["description"] == "Reviews a change for bugs before it merges."
    assert params["rules"] == RULES
    assert params["model_hint"] == "sonnet"
    # No sender: a file of this form names no person, so no "From" row and no saved label.
    assert not params.get("sender")
    assert "sender" not in {row["field"] for row in store.get(body["id"])["changes"]}
    assert params["bundle_sha256"] == hashlib.sha256(data).hexdigest()
    # The tool list, the hooks and the colour are not carried: no grant reaches the card.
    for key in ("skills", "crons", "carried", "goal", "not_imported"):
        assert not params.get(key), key
    assert "Read, Grep" not in json.dumps(params) and "guard.sh" not in json.dumps(params)
    assert not crewmate_stage.stage_dir().exists()


def test_with_the_setting_off_an_agent_file_is_refused_as_a_non_zip(world, monkeypatch, tmp_path):
    status, body, store = _post(world, monkeypatch, tmp_path, AGENT_FILE.encode())
    assert (status, body) == REFUSAL
    # A file that is not an agent file at all gets the identical answer.
    assert _post(world, monkeypatch, tmp_path, b"# just markdown\n")[:2] == REFUSAL
    assert store.pending(None) == []


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
def test_a_kirobot_file_imports_the_same_either_way(world, monkeypatch, tmp_path, on):
    _setting(tmp_path, on)
    status, body, store = _post(world, monkeypatch, tmp_path, _kirobot())
    assert status == 200, body
    assert store.get(body["id"])["params"]["rules"] == "Never push to main."


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
@pytest.mark.parametrize(
    "data",
    [b"# just markdown\n", b"PK\x03\x04not a zip", b"--- \nname: x\n---\n", b"\xff---\n"],
)
def test_every_other_file_keeps_its_refusal(world, monkeypatch, tmp_path, on, data):
    _setting(tmp_path, on)
    assert _post(world, monkeypatch, tmp_path, data)[:2] == REFUSAL


def test_an_app_token_still_gets_403(world, monkeypatch, tmp_path):
    _setting(tmp_path, True)
    status, body, store = _post(world, monkeypatch, tmp_path, AGENT_FILE.encode(), auth="app")
    assert status == 403 and store.pending(None) == []


def _params(text: str | bytes) -> dict:
    data = text if isinstance(text, bytes) else text.encode()
    files = read_crewmate_file(data, allow_agent_file=True)
    return crewmate_import_params(files.bundle, files)


def test_a_bom_and_crlf_endings_read_the_same():
    crlf = "\ufeff" + AGENT_FILE.replace("\n", "\r\n")
    params = _params(crlf)
    assert params["name"] == "code-reviewer" and params["rules"] == RULES


def test_only_a_body_becomes_rules():
    params = _params("---\nname: Quiet\nprompt: file://elsewhere.md\n---\n")
    assert params["name"] == "Quiet" and "rules" not in params and "model_hint" not in params


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("---\ndescription: no name\n---\nHi\n", "invalid_bundle"),
        ("---\nname: [a, b]\n---\nHi\n", "invalid_bundle"),
        ("---\nname: x\n", "invalid_bundle"),
        ("---\n- a\n- b\n---\nHi\n", "invalid_bundle"),
        ("---\nname: x\n---\n" + "a" * JSON_MAX_BYTES, "too_large"),
        ("---\nname: x\n---\nkey " + github_classic() + "\n", "invalid_text"),
        ("---\nname: x\n---\nbell \x07\n", "invalid_text"),
        ("---\nname: x\n---\n" + "a" * 4001, "invalid_bundle"),
        (b"---\nname: x\n---\n\xff\n", "invalid_text"),
    ],
    ids="no-name name-not-text unclosed-fence not-a-mapping over-64-kib credential control-char "
    "rules-over-cap not-utf8".split(),
)
def test_a_bad_agent_file_reuses_the_card_refusals(text, code):
    with pytest.raises(CardCatalogError) as caught:
        _params(text)
    assert caught.value.code == code
