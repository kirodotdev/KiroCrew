"""Import from file: ``POST /api/members/import`` and its ``crewmate.import`` card.

rfc-crewmate-share.md §5.2, §5.4 and the Phase 1 exit criteria. The invariant:
no file, app or agent creates a crewmate; only the owner's press on the card
does, and the created crewmate is never in ``trust`` for any moment.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace

import change_cards_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from change_cards_helpers import AGENT, Clock, _card_headers

from kiro_crew import change_card_catalog as catalog
from kiro_crew import crewmate_bundle, crewmate_carried, crewmate_stage
from kiro_crew.crewmate_bundle import crewmate_import_params, read_crewmate_file
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import (
    CARD_TTL_SECONDS,
    MAX_LIVE_CARDS_PER_SLOT,
    CardError,
    CardStore,
)
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.handlers import crewmate_import_files as files_route
from kiro_crew.dashboard.state_parts.slot_directory import _normalize_slot_key

world = change_cards_helpers.world

MANIFEST = {
    "format": "kirocrew.crewmate",
    "formatVersion": 1,
    "name": "Release Captain",
    "description": "Watches release branches.",
    "sender": {"label": "Ana"},
}
RULES = {"rules": "Never push to main."}
GOALS = {"longTerm": "Ship clean releases"}
CRONS = [{"name": "weekly", "message": "Check the release branch", "cron_expr": "0 9 * * 1"}]
ZIP = {"Content-Type": "application/zip"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + b"\x00\x00\x00\x00IEND\xaeB`\x82"
SKILLED = {**MANIFEST, "skills": [{"name": "notes", "source": "bundled"}]}
SCRIPT_CRON = {"name": "nightly", "cron_expr": "0 2 * * *", "script": "run.py:main"}
SCRIPT = b"def main():\n    return 1\n"

#: The start-up sweep as shipped; the autouse fixture swaps in a no-op.
_REAL_RUN_SWEEP = routes._run_stage_sweep


async def _no_sweep(store) -> None:
    return None


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(routes, "_run_stage_sweep", _no_sweep)
    _floor_monkeypatch.setattr(routes, "_STAGE_SWEEP", None)


def _zip(entries: dict[str, bytes | str], *, links: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            if name in links:
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, data)
    return buf.getvalue()


def _bundle(**over: object) -> dict[str, bytes | str]:
    entries: dict[str, bytes | str] = {
        "crewmate.json": json.dumps(MANIFEST),
        "agent.json": "{}",
        "rules.json": json.dumps(RULES),
        "goals.json": json.dumps(GOALS),
        "crons.json": json.dumps(CRONS),
    }
    for key, value in over.items():
        name = key.replace("__", "/").replace("_DOT_", ".")
        if value is None:
            entries.pop(name, None)
        else:
            entries[name] = value if isinstance(value, (bytes, str)) else json.dumps(value)
    return entries


def _carrying() -> dict[str, bytes | str]:
    """A bundle carrying one skill (with a script file), one cron script and a picture."""
    return {
        **_bundle(crewmate_DOT_json=SKILLED, crons_DOT_json=[*CRONS, SCRIPT_CRON]),
        "skills/notes/SKILL.md": "# notes\nKeep release notes short.",
        "skills/notes/tool.py": "print('hi')\r\n",
        "crons/run.py": SCRIPT,
        "avatar.png": PNG,
    }


def _params(data: bytes) -> dict:
    files = read_crewmate_file(data)
    return crewmate_import_params(files.bundle, files)


def _refused(data: bytes) -> str:
    with pytest.raises(catalog.CardCatalogError) as exc:
        _params(data)
    return exc.value.code


# ── the file: §5.2, checked before any card exists ──


def test_a_whole_file_reads_into_one_card_of_every_part():
    p = _params(_zip(_bundle()))
    assert p["name"] == "Release Captain" and p["sender"] == "Ana"
    assert p["rules"] == RULES["rules"] and p["goal"] == GOALS["longTerm"]
    assert p["crons"] == [{**CRONS[0]}]
    assert len(p["bundle_sha256"]) == 64 and "carried" not in p


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        # Paths: only two folders, each one level, each name on the §5.2 pattern.
        ({"../evil.json": "{}"}, "invalid_path"),
        ({"skills/../x/SKILL.md": "x"}, "invalid_path"),
        ({"skills/..": "x"}, "invalid_path"),
        ({"C:/evil.json": "{}"}, "invalid_path"),
        ({"skills/C:x/SKILL.md": "x"}, "invalid_path"),
        # zipfile turns os.sep into "/" when it reads a name, so on Windows this is
        # the plain path skills/x/SKILL.md, refused because no skill "x" is declared.
        ({"skills\\x\\SKILL.md": "x"}, "skill_mismatch" if os.sep == "\\" else "invalid_path"),
        ({"/abs.json": "{}"}, "invalid_path"),
        ({"skills/a/b/SKILL.md": "x"}, "invalid_path"),
        ({"crons/sub/x.py": "x"}, "invalid_path"),
        ({"crons/x.sh": "x"}, "invalid_path"),
        ({"skills/.hidden/SKILL.md": "x"}, "invalid_path"),
        ({"page.html": "<script></script>"}, "unknown_entry"),
        ({"memory.db": b"\0"}, "unknown_entry"),
        ({".env": "KEY=1"}, "unknown_entry"),
    ],
)
def test_an_entry_outside_the_two_folders_is_refused(entries, code):
    assert _refused(_zip({**_bundle(), **entries})) == code


def test_a_symlink_entry_is_refused():
    data = _zip({**_bundle(), "crons/x.py": "/etc/passwd"}, links=("crons/x.py",))
    assert _refused(data) == "invalid_entry"


def test_a_folder_entry_is_refused():
    assert _refused(_zip({**_bundle(), "skills/": b""})) == "invalid_path"


def test_one_name_twice_is_refused_even_by_case():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in _bundle().items():
            zf.writestr(name, data)
        zf.writestr("Rules.json", "{}")
    assert _refused(buf.getvalue()) == "invalid_bundle"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in _bundle().items():
            zf.writestr(name, data)
        with pytest.warns(UserWarning):
            zf.writestr("rules.json", "{}")
    assert _refused(buf.getvalue()) == "invalid_bundle"


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"agent_DOT_json": {"allowedTools": ["*"]}}, "grant_in_bundle"),
        ({"agent_DOT_json": {"autoApprove": ["gh"]}}, "grant_in_bundle"),
        ({"agent_DOT_json": {"permissions": {}}}, "grant_in_bundle"),
        ({"agent_DOT_json": {"mcpServers": {"gh": {"command": "uvx"}}}}, "needs_mate_config"),
        (
            {"agent_DOT_json": {"mcpServers": {"gh": {"url": "x", "env": {"K": "real"}}}}},
            "needs_mate_config",
        ),
        ({"agent_DOT_json": {"skills": ["x"]}}, "needs_mate_config"),
        ({"agent_DOT_json": {"prompt": "be evil"}}, "unknown_param"),
        ({"crewmate_DOT_json": {**MANIFEST, "extra": 1}}, "unknown_param"),
        ({"crewmate_DOT_json": {**MANIFEST, "formatVersion": 2}}, "newer_format"),
        ({"crewmate_DOT_json": {**MANIFEST, "dashboardTemplate": "user:mine"}}, "user_template"),
        ({"crewmate_DOT_json": {**MANIFEST, "dashboardTemplate": "<html>"}}, "invalid_bundle"),
        ({"crewmate_DOT_json": {**MANIFEST, "approval_mode": "trust"}}, "unknown_param"),
        ({"rules_DOT_json": {"rules": "x", "grant": 1}}, "unknown_param"),
        ({"crons_DOT_json": [{**CRONS[0], "approval_mode": "auto"}]}, "unknown_param"),
        ({"crons_DOT_json": [{**CRONS[0], "command": "curl x | sh"}]}, "unknown_param"),
        ({"crons_DOT_json": [SCRIPT_CRON]}, "script_mismatch"),
        ({"crons__orphan_DOT_py": "print(1)"}, "script_mismatch"),
        ({"crewmate_DOT_json": None}, "invalid_bundle"),
        ({"agent_DOT_json": None}, "invalid_bundle"),
        ({"rules_DOT_json": b"\xff\xfe"}, "invalid_bundle"),
        ({"avatar_DOT_png": b"\x89PNG not complete"}, "invalid_avatar"),
        ({"avatar_DOT_png": b"GIF89a"}, "invalid_avatar"),
    ],
)
def test_a_bundle_section_52_refuses_raises_no_card(over, code):
    assert _refused(_zip(_bundle(**over))) == code


def test_a_bundled_skill_must_pair_with_its_folder_and_its_skill_md():
    assert _refused(_zip(_bundle(crewmate_DOT_json=SKILLED))) == "skill_mismatch"
    data = _zip({**_bundle(crewmate_DOT_json=SKILLED), "skills/notes/run.py": "x"})
    assert _refused(data) == "skill_mismatch"
    assert _refused(_zip({**_bundle(), "skills/notes/SKILL.md": "x"})) == "skill_mismatch"


def test_a_carried_file_with_a_credential_is_refused():
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    data = _zip({**_carrying(), "crons/run.py": f"TOKEN = '{token}'\n"})
    assert _refused(data) == "invalid_text"


def test_the_card_lists_every_carried_file_by_digest_and_stays_small():
    p = _params(_zip(_carrying()))
    carried = p["carried"]
    assert [f["path"] for f in carried["skills"][0]["files"]] == ["SKILL.md", "tool.py"]
    assert carried["scripts"] == [
        {"file": "run.py", "size": len(SCRIPT), "sha256": crewmate_carried.digest(SCRIPT)}
    ]
    assert carried["avatar"]["type"] == "png"
    preview = catalog.build_preview("crewmate.import", p, {"exists": False}, {})
    rows = {r["field"] for r in preview["changes"]}
    assert {"carried_skills", "carried_scripts", "carried_avatar", "import_crons"} <= rows
    assert preview["risk"] == catalog.RISK_CODE_EXEC
    assert len(json.dumps(p)) < 32 * 1024


def test_the_seam_names_code_exec_for_scripts_and_non_skill_md_files():
    assert crewmate_carried.code_exec({"scripts": [{"file": "a.py"}]})
    assert crewmate_carried.code_exec({"skills": [{"files": [{"path": "run.py"}]}]})
    assert not crewmate_carried.code_exec({"skills": [{"files": [{"path": "SKILL.md"}]}]})


@pytest.mark.parametrize(
    "carried",
    [
        {
            "skills": [
                {"name": "../x", "files": [{"path": "SKILL.md", "size": 1, "sha256": "0" * 64}]}
            ]
        },
        {"skills": [{"name": "x", "files": [{"path": "a.md", "size": 1, "sha256": "0" * 64}]}]},
        {"scripts": [{"file": "a.sh", "size": 1, "sha256": "0" * 64}]},
        {"scripts": [{"file": "a.py", "size": 1, "sha256": "nothex"}]},
        {"avatar": {"type": "svg", "size": 1, "sha256": "0" * 64}},
        {"payload": "x"},
    ],
)
def test_a_card_param_naming_files_outside_the_rules_is_refused(carried):
    p = {"name": "Mate", "format_version": 1, "bundle_sha256": "0" * 64, "carried": carried}
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params("crewmate.import", p)


def test_an_app_skill_reference_is_shown_not_written():
    refs = {**MANIFEST, "skills": [{"name": "notes", "source": "app:release-tools"}]}
    p = _params(_zip(_bundle(crewmate_DOT_json=refs)))
    preview = catalog.build_preview("crewmate.import", p, {"exists": False}, {})
    rows = {r["field"]: r["after"] for r in preview["changes"]}
    assert rows["import_skills"] == refs["skills"]
    assert [s["path"] for s in preview["apply"]] == [
        "/api/agents",
        "/api/members/{{step0.member_id}}/goals/long-term",
        "/api/members/{{step0.member_id}}/rules",
        "/api/crons",
    ]


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        (
            {"skills/notes/SKILL.md": b"x" * (crewmate_bundle.CARRIED_FILE_MAX_BYTES + 1)},
            "too_large",
        ),
        (
            {f"skills/notes/f{i}.md": b"x" * 250_000 for i in range(5)}
            | {"skills/notes/SKILL.md": "x"},
            "too_large",
        ),
        ({"avatar.png": b"x" * (crewmate_bundle.AVATAR_MAX_BYTES + 1)}, "too_large"),
        (
            {"rules.json": json.dumps({"rules": "x" * (crewmate_bundle.JSON_MAX_BYTES + 1)})},
            "too_large",
        ),
        (
            {f"skills/s{i}/SKILL.md": "x" for i in range(crewmate_bundle.ENTRY_MAX)},
            "too_many_entries",
        ),
    ],
    ids=["file-cap", "skill-folder-cap", "avatar-cap", "json-cap", "entry-cap"],
)
def test_the_size_caps_refuse_before_the_rest_is_read(entries, code):
    assert _refused(_zip({**_bundle(), **entries})) == code


def test_a_file_over_8_mib_or_not_a_zip_is_refused():
    with pytest.raises(catalog.CardCatalogError) as exc:
        read_crewmate_file(b"x" * (crewmate_bundle.FILE_MAX_BYTES + 1))
    assert exc.value.code == "too_large"
    for junk in (b"", b"not a zip", b"PK\x05\x06" + b"\0" * 18):
        with pytest.raises(catalog.CardCatalogError):
            read_crewmate_file(junk)


def test_the_zip_inventory_is_vetted_before_zipfile_reads_it(monkeypatch):
    seen = []
    real = zipfile.ZipFile

    def spy(*a, **k):
        seen.append("zipfile")
        return real(*a, **k)

    from kiro_crew import zip_vet

    def vet(*a, **k):
        seen.append("vet")
        raise zip_vet.ZipInventoryRejected("too_many_members")

    data = _zip(_bundle())
    monkeypatch.setattr(zip_vet, "vet_zip_inventory_bytes", vet)
    monkeypatch.setattr(crewmate_bundle.zipfile, "ZipFile", spy)
    with pytest.raises(catalog.CardCatalogError):
        read_crewmate_file(data)
    assert seen == ["vet"]


def test_no_chat_slot_key_can_take_the_owner_prefix():
    assert ":" not in _normalize_slot_key(routes.IMPORT_SLOT_KEY)
    assert not _normalize_slot_key("owner:crewmate-import").startswith(routes.OWNER_SLOT_PREFIX)


# ── the staged copy ──


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_staged_copy_is_owner_only_and_named_by_its_digest():
    data = _zip(_carrying())
    sha = crewmate_stage.stage(data)
    path = crewmate_stage.stage_dir() / f"{sha}.crewmate"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(crewmate_stage.stage_dir().stat().st_mode) == 0o700
    assert crewmate_stage.load(sha) == data
    assert [p.name for p in crewmate_stage.stage_dir().iterdir()] == [path.name]


def test_a_changed_or_linked_staged_copy_is_refused():
    sha = crewmate_stage.stage(_zip(_carrying()))
    path = crewmate_stage.stage_dir() / f"{sha}.crewmate"
    path.write_bytes(b"swapped")
    with pytest.raises(crewmate_stage.StageError) as exc:
        crewmate_stage.load(sha)
    assert exc.value.code == "staged_copy_changed"
    if os.name != "nt":
        path.unlink()
        path.symlink_to("/etc/hostname")
        with pytest.raises(crewmate_stage.StageError):
            crewmate_stage.load(sha)
    for bad in ("../x", "A" * 64, ""):
        with pytest.raises(crewmate_stage.StageError):
            crewmate_stage.load(bad)


def test_the_sweep_keeps_only_copies_a_live_card_names():
    keep = crewmate_stage.stage(_zip(_carrying()))
    gone = crewmate_stage.stage(_zip(_bundle()))
    (crewmate_stage.stage_dir() / ".x.tmp").write_bytes(b"")
    crewmate_stage.write_ledger("m1", {"member": "x"})
    removed = crewmate_stage.sweep({keep})
    assert sorted(removed) == sorted([f"{gone}.crewmate", ".x.tmp"])
    assert crewmate_stage.load(keep) and crewmate_stage.read_ledger("m1") == {"member": "x"}


# ── the routes ──


class _Crons:
    def __init__(self) -> None:
        self.jobs: dict[str, SimpleNamespace] = {}

    async def add_job_async(self, name, message, **kw):
        job = SimpleNamespace(id=f"sj{len(self.jobs) + 1}", name=name, message=message, **kw)
        self.jobs[job.id] = job
        return job

    async def get_job_async(self, job_id):
        return self.jobs.get(job_id)

    async def remove_job_async(self, job_id, **_kw):
        return self.jobs.pop(job_id, None) is not None

    async def remove_job_if_async(self, job_id, *, expected_revision, revision_of, **_kw):
        job = self.jobs.get(job_id)
        if job is None:
            return "missing"
        if revision_of(job) != expected_revision:
            return "changed"
        del self.jobs[job_id]
        return "removed"


class _Writes:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.fail: set[str] = set()
        self.crews: dict[str, dict] = {}
        self.rules: str | None = None
        #: Cron ids the owner edited after their step made them.
        self.edited: set[str] = set()
        #: Rules the owner saves while the cron step runs (after the rules step).
        self.rules_saved_during: str | None = None


def _import_app(world, store: CardStore, writes: _Writes, monkeypatch, tmp_path) -> web.Application:
    real = cards.read_state
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    avatars: list[tuple[str, str, bytes]] = []

    async def read_state(kind, params, evidence, *, state, app):
        if kind != "crewmate.import":
            return await real(kind, params, evidence, state=state, app=app)
        if not evidence:
            return {"exists": params["name"] in writes.crews, "inherited_auto_approve": []}
        crew = writes.crews.get(evidence[0].get("name") or "")
        crons = {
            e["id"]: ("fp-edited" if e["id"] in writes.edited else "fp-" + e["id"])
            for e in evidence[1:]
            if isinstance(e.get("id"), str) and e["id"].startswith("job")
        }
        out = {
            "exists": crew is not None,
            "member_id": (crew or {}).get("member_id", ""),
            "revision": "r1" if crew else "",
            "crons": crons,
        }
        if writes.rules is not None:
            out["rules"] = hashlib.sha256(writes.rules.strip().encode("utf-8")).hexdigest()
        return out

    def member(slug, name):
        if slug != "m1" or name not in writes.crews:
            raise files_route._Refused(404, "member_not_found", "no crewmate")
        return name, SimpleNamespace(workspace="ws"), SimpleNamespace(workspaces={})

    async def set_avatar(key, ext, blob):
        avatars.append((key, ext, blob))
        return "pin"

    monkeypatch.setattr(cards, "read_state", read_state)
    monkeypatch.setattr(files_route, "_member", member)
    monkeypatch.setattr(
        files_route, "_skills_root", lambda cfg, agent: workspace / ".kiro" / "skills"
    )
    monkeypatch.setattr(files_route, "_set_avatar", set_avatar)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
        lambda request, op: _owner_gate(request),
    )

    def route(method: str, key: str):
        async def handler(request: web.Request) -> web.Response:
            body = await request.json() if request.can_read_body else {}
            writes.calls.append((method, request.path, body))
            if key in writes.fail:
                return web.json_response({"error": "boom", "code": "boom"}, status=500)
            if key == "create":
                writes.crews[body["name"]] = {**body, "member_id": "m1"}
                return web.json_response({"member_id": "m1", "name": body["name"]})
            if key == "rules":
                writes.rules = body["rules"]
            if key == "cron" and writes.rules_saved_during is not None:
                writes.rules = writes.rules_saved_during
            if key == "cron":
                job_id = f"job{len(writes.calls)}"
                return web.json_response({"ok": True, "id": job_id, "revision": f"fp-{job_id}"})
            if key == "delete_crew":
                writes.crews.pop(request.match_info["name"], None)
            if key == "delete_cron":
                expect = request.get(routes.REQ_CARD_UNDO_CRON_EXPECT)
                writes.calls[-1] = (method, request.path, {"expect": expect})
            return web.json_response({"ok": True})

        return handler

    def recorded(handler, key):
        async def run(request: web.Request) -> web.StreamResponse:
            if key in writes.fail:
                writes.calls.append((request.method, request.path, {}))
                return web.json_response({"error": "boom", "code": "boom"}, status=500)
            resp = await handler(request)
            writes.calls.append((request.method, request.path, {"status": resp.status}))
            return resp

        return run

    app = change_cards_helpers._app(world, store)
    app["state"].crons = _Crons()
    app["avatars"] = avatars
    app.router.add_post("/api/agents", route("POST", "create"))
    app.router.add_delete("/api/agents/{name}", route("DELETE", "delete_crew"))
    app.router.add_put("/api/members/{slug}/goals/long-term", route("PUT", "goal"))
    app.router.add_put("/api/members/{slug}/rules", route("PUT", "rules"))
    app.router.add_post("/api/crons", route("POST", "cron"))
    app.router.add_delete("/api/crons/{job_id}", route("DELETE", "delete_cron"))
    app.router.add_post(
        "/api/members/{slug}/import-files", recorded(files_route.api_member_import_files, "files")
    )
    app.router.add_delete(
        "/api/members/{slug}/import-files", files_route.api_member_import_files_undo
    )
    return app


async def _owner_gate(request: web.Request):
    if request.headers.get("X-Test-Auth") == "owner" and request.get("app") == "":
        return None
    return web.json_response({"error": "owner only", "code": "owner_only"}, status=403)


def _go(world, store, writes, monkeypatch, tmp_path, fn):
    async def main():
        app = _import_app(world, store, writes, monkeypatch, tmp_path)
        async with TestClient(TestServer(app)) as c:
            return await fn(c, app)

    return asyncio.run(main())


def _fill(step: dict, responses: list[dict]) -> tuple[str, dict]:
    """The browser's fill of one plan step from the earlier steps' responses."""
    path, body = step["path"], json.loads(json.dumps(step.get("body") or {}))
    for f in step.get("fill") or ():
        value = responses[f["step"]][f["key"]]
        token = catalog.placeholder(f)
        if f.get("in") == "path":
            path = path.replace(token, value)
        else:
            body[f["field"]] = value
    return path, body


async def _press(c, store: CardStore, card_id: str, op: str = "apply") -> list[int]:
    card = store.public(store.get(card_id))
    plan = card["plan"][op]
    statuses, responses = [], []
    for i, step in enumerate(plan):
        path, body = _fill(step, responses)
        r = await c.request(
            step["method"], path, json=body or None, headers=_card_headers(card, op=op, step=i)
        )
        statuses.append(r.status)
        responses.append(await r.json())
        if r.status >= 300:
            break
        card = store.public(store.get(card_id))
    return statuses


async def _raise(c, data: bytes | None = None) -> str:
    r = await c.post(
        "/api/members/import", data=data or _zip(_bundle()), headers={**ZIP, "X-Test-Auth": "owner"}
    )
    assert r.status == 200, await r.text()
    return (await r.json())["id"]


def test_the_owner_import_raises_one_pending_card_and_writes_nothing_else(
    world, monkeypatch, tmp_path
):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        return await _raise(c)

    card_id = _go(world, store, writes, monkeypatch, tmp_path, go)
    rec = store.get(card_id)
    assert rec["slot_key"] == "owner:crewmate-import" and rec["session_key"] == ""
    assert rec["status"] == "pending" and rec["kind"] == "crewmate.import"
    assert writes.calls == [] and writes.crews == {}
    # A file that carries nothing keeps no staged copy.
    assert not crewmate_stage.stage_dir().exists()
    assert [c["id"] for c in store.pending("owner:crewmate-import")] == [card_id]


def test_a_carrying_file_is_staged_once_and_shown_in_full(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    data = _zip(_carrying())

    async def go(c, app):
        card_id = await _raise(c, data)
        sha = store.get(card_id)["params"]["bundle_sha256"]
        r = await c.get(f"/api/members/import/{sha}/files", headers={"X-Test-Auth": "owner"})
        shown = await r.json()
        r2 = await c.get(f"/api/members/import/{sha}/files", headers={"X-Test-Auth": "app"})
        r3 = await c.get(f"/api/members/import/{'0' * 64}/files", headers={"X-Test-Auth": "owner"})
        return sha, r.status, shown, r2.status, r3.status

    sha, status, shown, app_status, other = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert crewmate_stage.load(sha) == data
    assert status == 200 and app_status == 403 and other == 404
    assert shown["skills"]["notes"]["tool.py"] == "print('hi')\r\n"
    assert shown["scripts"]["run.py"] == "def main():\n    return 1\n"
    assert shown["avatar"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("auth", ["app", "internal", ""])
def test_only_the_owner_can_import(world, monkeypatch, tmp_path, auth):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        headers = {**ZIP, "X-Test-Auth": auth, "X-Session-Key": change_cards_helpers.SK}
        r = await c.post("/api/members/import", data=_zip(_carrying()), headers=headers)
        return r.status

    assert _go(world, store, writes, monkeypatch, tmp_path, go) in (401, 403)
    assert store.pending(None) == [] and not crewmate_stage.stage_dir().exists()


@pytest.mark.parametrize(
    ("data", "headers", "code"),
    [
        (_zip(_bundle(agent_DOT_json={"allowedTools": ["*"]})), ZIP, "grant_in_bundle"),
        (_zip(_bundle()), {"Content-Type": "text/plain"}, "invalid_content_type"),
        (_zip(_bundle()), {"Content-Type": "application/octet-stream"}, "invalid_content_type"),
        (b"x" * (crewmate_bundle.FILE_MAX_BYTES + 1), ZIP, "too_large"),
    ],
    ids=["grant", "text-plain", "octet-stream", "over-8-mib"],
)
def test_a_refused_file_raises_no_card_and_keeps_nothing(
    world, monkeypatch, tmp_path, data, headers, code
):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        r = await c.post(
            "/api/members/import", data=data, headers={**headers, "X-Test-Auth": "owner"}
        )
        return r.status, (await r.json())["code"]

    assert _go(world, store, writes, monkeypatch, tmp_path, go) == (400, code)
    assert store.pending(None) == []
    assert not list(crewmate_stage.stage_dir().glob("*.crewmate"))


def test_a_taken_name_lands_as_the_first_free_one(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    writes.crews["Release Captain"] = {"member_id": "m0"}

    async def go(c, app):
        return await _raise(c)

    card_id = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert store.get(card_id)["params"]["name"] == "Release Captain 2"


def test_an_agent_can_raise_no_import_card(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    params = _params(_zip(_bundle()))

    async def go(c, app):
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": "crewmate.import", "params": params},
            headers=AGENT,
        )
        return r.status, (await r.json())["code"]

    assert _go(world, store, writes, monkeypatch, tmp_path, go) == (403, "app_only")
    assert "crewmate.import" not in {k["id"] for k in catalog.list_kinds()}


def test_a_chat_caller_whose_slot_names_the_owner_prefix_is_refused(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def handler(request: web.Request) -> web.Response:
        return await routes.propose_for_caller(
            request,
            routes.IMPORT_SLOT_KEY,
            "dashboard:x",
            kind="setting.change",
            params={"path": "chat.verbosity", "value": "brief"},
            reason=None,
        )

    async def main():
        app = _import_app(world, store, writes, monkeypatch, tmp_path)
        app.router.add_post("/probe", handler)
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/probe")
            return r.status, (await r.json())["code"]

    assert asyncio.run(main()) == (403, "app_only")


def test_the_press_creates_one_member_in_normal_then_its_parts_paused(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        card_id = await _raise(c)
        # An app token cannot run a step of the plan, whatever headers it sends.
        card = store.public(store.get(card_id))
        steps = card["plan"]["apply"]
        filled = [(steps[0]["path"], steps[0]["body"])]
        filled += [_fill(s, [{"member_id": "m1", "name": "Release Captain"}]) for s in steps[1:]]
        for i, (path, body) in enumerate(filled):
            r = await c.request(
                steps[i]["method"], path, json=body, headers=_card_headers(card, step=i, auth="app")
            )
            assert r.status == 403, (i, path)
        assert writes.calls == []
        return card_id, await _press(c, store, card_id)

    card_id, statuses = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert statuses == [200, 200, 200, 200]
    assert [(m, p) for m, p, _ in writes.calls] == [
        ("POST", "/api/agents"),
        ("PUT", "/api/members/m1/goals/long-term"),
        ("PUT", "/api/members/m1/rules"),
        ("POST", "/api/crons"),
    ]
    create, _goal, rules, cron = (b for _m, _p, b in writes.calls)
    assert create["approval_mode"] == "normal"
    assert create["imported"]["sender_label"] == "Ana" and len(create["imported"]["sha256"]) == 64
    assert not {"allowedTools", "autoApprove", "mcpServers", "skills"} & set(create)
    assert rules == {"member": "Release Captain", "rules": RULES["rules"]}
    assert cron["enabled"] is False and cron["member_id"] == "m1"
    assert store.get(card_id)["status"] == "applied"


def test_the_press_writes_carried_files_only_into_the_new_crewmate(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    home = tmp_path / "home"
    own = home / "crons" / "imported" / "m1"
    own.mkdir(parents=True)
    (home / "crons" / "run.py").write_text("# the receiver's own script\n", encoding="utf-8")
    (own / "run.py").write_text("# left over\n", encoding="utf-8")
    data = _zip(_carrying())

    async def go(c, app):
        card_id = await _raise(c, data)
        sha = store.get(card_id)["params"]["bundle_sha256"]
        # Not a card step: the route refuses even the owner.
        direct = await c.post(
            "/api/members/m1/import-files",
            json={"member": "Release Captain", "sha256": sha},
            headers={"X-Test-Auth": "owner"},
        )
        statuses = await _press(c, store, card_id)
        return card_id, sha, direct.status, statuses, app

    card_id, sha, direct, statuses, app = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert direct == 403
    assert statuses == [200, 200, 200, 200, 200]
    skill = tmp_path / "ws" / ".kiro" / "skills" / "notes"
    assert (skill / "SKILL.md").read_text(encoding="utf-8").startswith("# notes")
    assert (skill / "tool.py").read_bytes() == b"print('hi')\r\n"
    assert not (home / "skills").exists()
    # The receiver's scripts are untouched. The import's landed in the crewmate's
    # own folder, never the shared crons/ that every job's launcher imports from,
    # under the first free name there.
    assert (home / "crons" / "run.py").read_text(
        encoding="utf-8"
    ) == "# the receiver's own script\n"
    assert sorted(p.name for p in (home / "crons").iterdir()) == ["imported", "run.py"]
    assert (own / "run.py").read_text(encoding="utf-8") == "# left over\n"
    assert (own / "run-2.py").read_text(encoding="utf-8").startswith("def main")
    (job,) = app["state"].crons.jobs.values()
    assert job.enabled is False and job.member_id == "m1"
    assert job.script == f"{own / 'run-2.py'}:main"
    assert app["avatars"] == [("Release Captain", "png", PNG)]
    assert store.get(card_id)["status"] == "applied"
    # Applied: the staged copy is gone.
    with pytest.raises(crewmate_stage.StageError):
        crewmate_stage.load(sha)


def test_a_staged_copy_swapped_after_the_card_refuses_the_file_step(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        sha = store.get(card_id)["params"]["bundle_sha256"]
        (crewmate_stage.stage_dir() / f"{sha}.crewmate").write_bytes(_zip(_bundle()))
        return card_id, await _press(c, store, card_id)

    card_id, statuses = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert statuses == [200, 200, 200, 409]
    assert not (tmp_path / "ws" / ".kiro").exists()
    assert store.get(card_id)["status"] == "partial"


def test_an_existing_skill_folder_is_never_overwritten(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    mine = tmp_path / "ws" / ".kiro" / "skills" / "notes"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine", encoding="utf-8")

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        return await _press(c, store, card_id)

    assert _go(world, store, writes, monkeypatch, tmp_path, go)[-1] == 409
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "mine"
    assert not (mine / "tool.py").exists()
    assert not list((tmp_path / "home").glob("crons/**/*.py"))


def test_a_failed_later_step_lands_partial_with_a_working_undo(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    writes.fail.add("cron")

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        applied = await _press(c, store, card_id)
        rec = store.public(store.get(card_id))
        undo = rec["plan"]["undo"]
        undone = await _press(c, store, card_id, op="undo")
        return card_id, applied, rec, undo, undone, app

    card_id, applied, rec, undo, undone, app = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert applied == [200, 200, 200, 200, 500]
    assert rec["status"] == "partial"
    # Created in normal: every step after the create failing still leaves no trust.
    assert writes.calls[0][2]["approval_mode"] == "normal"
    assert [(s["method"], s["path"]) for s in undo] == [
        ("DELETE", "/api/members/m1/import-files"),
        ("PUT", "/api/members/m1/rules"),
        ("DELETE", "/api/agents/Release%20Captain"),
    ]
    assert undone == [200, 200, 200]
    assert store.get(card_id)["status"] == "undone" and writes.crews == {}
    assert not (tmp_path / "ws" / ".kiro" / "skills" / "notes").exists()
    assert not list((tmp_path / "home").glob("crons/**/*.py"))
    assert app["state"].crons.jobs == {}
    assert crewmate_stage.read_ledger("m1") is None


def test_the_file_undo_refuses_when_a_written_file_changed(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        await _press(c, store, card_id)
        (tmp_path / "ws" / ".kiro" / "skills" / "notes" / "SKILL.md").write_text("edited")
        return await _press(c, store, card_id, op="undo")

    # The plain cron goes first; the file step's Undo then refuses, and nothing after it runs.
    assert _go(world, store, writes, monkeypatch, tmp_path, go) == [200, 409]
    assert "Release Captain" in writes.crews
    assert (tmp_path / "ws" / ".kiro" / "skills" / "notes" / "SKILL.md").read_text() == "edited"


def test_the_undo_deletes_each_cron_only_by_its_fingerprint(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        card_id = await _raise(c)
        await _press(c, store, card_id)
        return await _press(c, store, card_id, op="undo")

    assert _go(world, store, writes, monkeypatch, tmp_path, go) == [200, 200, 200]
    deletes = [b for m, p, b in writes.calls if m == "DELETE" and p.startswith("/api/crons/")]
    assert deletes == [{"expect": "fp-job4"}]


@pytest.mark.parametrize("edit", ["cron", "rules"])
def test_an_edit_saved_while_the_import_ran_is_never_the_undo_baseline(
    world, monkeypatch, tmp_path, edit
):
    """The cron step's response fingerprints the job as it made it, and the rules
    step wrote the card's own text. An owner edit that lands before the import
    finishes reads differently at completion, so no Undo is offered to erase it."""
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    if edit == "cron":
        writes.edited.add("job4")
    else:
        writes.rules_saved_during = "Push to main whenever."

    async def go(c, app):
        card_id = await _raise(c)
        return card_id, await _press(c, store, card_id)

    card_id, statuses = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert statuses == [200, 200, 200, 200]
    rec = store.get(card_id)
    assert rec["status"] == "applied"
    assert rec["plan"]["undo"] is None
    assert rec["undo_unavailable_reason"] == "changed_since_apply"


def test_a_cancelled_card_drops_its_staged_copy(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        rec = store.get(card_id)
        r = await c.post(
            f"/api/cards/{card_id}/cancel",
            json={"revision": rec["revision"]},
            headers={"X-Test-Auth": "owner"},
        )
        return r.status, rec["params"]["bundle_sha256"]

    status, sha = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert status == 200
    with pytest.raises(crewmate_stage.StageError):
        crewmate_stage.load(sha)


def test_the_first_settle_sweeps_orphans(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    orphan = crewmate_stage.stage(_zip(_bundle()))
    monkeypatch.setattr(routes, "_STAGE_SWEEP", None)
    monkeypatch.setattr(routes, "_run_stage_sweep", _REAL_RUN_SWEEP)

    async def go(c, app):
        r = await c.get("/api/cards/pending", headers={"X-Test-Auth": "owner"})
        return r.status

    assert _go(world, store, writes, monkeypatch, tmp_path, go) == 200
    with pytest.raises(crewmate_stage.StageError):
        crewmate_stage.load(orphan)


def test_the_import_card_stops_at_the_slot_cap_and_expires(tmp_path):
    clock = Clock()
    store = CardStore(tmp_path / "c.json", clock=clock)
    asyncio.run(store.warm())
    params = _params(_zip(_bundle()))
    preview = catalog.build_preview("crewmate.import", params, {"exists": False}, {})

    def propose(n: int) -> dict:
        p = {**params, "name": f"Mate {n}"}
        return store.propose(
            slot_key=routes.IMPORT_SLOT_KEY,
            session_key="",
            kind="crewmate.import",
            params=p,
            reason="",
            preview={**preview, "title": f"Import {n}"},
            before={"exists": False},
            context={},
        )

    first = [propose(n) for n in range(MAX_LIVE_CARDS_PER_SLOT)]
    with pytest.raises(CardError) as exc:
        propose(99)
    assert exc.value.code == "too_many_cards"
    clock.now += CARD_TTL_SECONDS + 1
    store.housekeep(lambda _key: True)
    assert {store.get(c["id"])["status"] for c in first} == {"expired"}


@pytest.mark.parametrize(
    ("params", "part"),
    [
        # No goal: step 1 is the rules, never "its goal" from the create plan.
        ({"name": "Q", "rules": "x"}, "its rules"),
        ({"name": "Q", "goal": "g", "rules": "x"}, "its goal"),
        ({"name": "Q", "carried": {"skills": []}}, "its skills, scripts and picture"),
        (
            {"name": "Q", "crons": [{"name": "c", "cron_expr": "* * * * *", "message": "m"}]},
            "its schedules",
        ),
    ],
)
def test_the_partial_line_names_the_import_part_that_failed(params, part):
    line = catalog.result_summary("crewmate.import", params, "partial", "t", failed_step=1)
    assert (
        line
        == f"Created mate “Q” without {part} or anything after it. Undo removes the mate and what was saved"
    )


def test_route_tables_register_the_import_routes():
    src = Path(routes.__file__).read_text(encoding="utf-8")
    assert 'add_post("/api/members/import", api_members_import)' in src
    mcp = Path(routes.__file__).parents[1] / "server_runtime" / "mcp_routes.py"
    text = mcp.read_text(encoding="utf-8")
    assert '("POST", "/api/members/import", "api_members_import")' in text
    assert '"/api/members/{slug}/import-files"' in text
    assert ("POST", "/api/members/{slug}/import-files") in catalog.HOOKED_ROUTES
    assert ("DELETE", "/api/members/{slug}/import-files") in catalog.HOOKED_ROUTES
    assert ("POST", "/api/members/{slug}/import-files") not in catalog.MEMORY_ROUTES


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_skills_land_under_the_workspace_and_a_linked_skills_folder_is_refused(tmp_path):
    from kiro_crew.config.loader import WorkspaceConfig

    ws = tmp_path / "member-ws"
    ws.mkdir()
    cfg = SimpleNamespace(workspaces={"mine": WorkspaceConfig(dir=str(ws))})
    agent = SimpleNamespace(workspace="mine")
    assert files_route._skills_root(cfg, agent) == ws / ".kiro" / "skills"
    elsewhere = tmp_path / "shared-skills"
    elsewhere.mkdir()
    (ws / ".kiro").mkdir()
    (ws / ".kiro" / "skills").symlink_to(elsewhere)
    with pytest.raises(files_route._Refused) as exc:
        files_route._skills_root(cfg, agent)
    assert exc.value.code == "workspace_link"
    with pytest.raises(files_route._Refused):
        files_route._skills_root(cfg, SimpleNamespace(workspace="nope"))


def test_the_picture_commits_through_the_editor_avatar_path(monkeypatch):
    from kiro_crew.config import loader
    from kiro_crew.dashboard.handlers.agents import _avatars_dir, _live_avatar_file

    agent = SimpleNamespace(avatar={}, memory_store="store-1")
    saved: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        loader.KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(agents={"mate": agent})),
    )
    monkeypatch.setattr(
        "kiro_crew.memory_stores.persist_member_config",
        lambda cfg, key, **kw: saved.append((key, dict(cfg.agents[key].avatar))),
    )
    pin = asyncio.run(files_route._set_avatar("mate", "png", PNG))
    assert saved == [("mate", {"kind": "image", "v": agent.avatar["v"], "file": pin})]
    live = _live_avatar_file("mate", pin)
    assert live is not None and live.read_bytes() == PNG
    assert not list(_avatars_dir().glob("*.pending.*"))


def test_a_corrupt_entry_is_a_refusal_not_a_crash():
    data = bytearray(_zip(_bundle()))
    at = data.find(b"Never push")
    data[at] ^= 0xFF  # the stored bytes now fail their CRC-32
    assert _refused(bytes(data)) == "invalid_bundle"


def test_the_file_undo_checks_each_digest_again_right_before_removing(tmp_path):
    """The Undo's first check runs before the awaited cron removals; a file saved
    in between is refused at the removal itself, and stays."""
    skill = tmp_path / "notes"
    skill.mkdir()
    (skill / "SKILL.md").write_bytes(b"# notes")
    script = tmp_path / "run.py"
    script.write_bytes(b"def main(): ...")
    record = {
        "skills": [
            {"dir": str(skill), "files": {"SKILL.md": hashlib.sha256(b"# notes").hexdigest()}}
        ],
        "scripts": [
            {"path": str(script), "sha256": hashlib.sha256(b"def main(): ...").hexdigest()}
        ],
    }
    files_route._check_unchanged(record)
    script.write_bytes(b"def main(): return 'mine'")  # saved after the check
    with pytest.raises(files_route._Refused) as exc:
        files_route._remove_record(record)
    assert exc.value.code == "changed_since_apply"
    # Refused before anything was removed.
    assert script.read_bytes() == b"def main(): return 'mine'"
    assert (skill / "SKILL.md").read_bytes() == b"# notes"
    # Unchanged files are removed as before.
    script.write_bytes(b"def main(): ...")
    files_route._remove_record(record)
    assert not script.exists() and not skill.exists()


def test_a_broken_deflate_stream_is_a_refusal_not_a_crash():
    """An intact central directory over a deflate stream zlib cannot decode."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, blob in _bundle().items():
            zf.writestr(name, blob)
    data = bytearray(buf.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        info = zf.getinfo("rules.json")
    name_len, extra_len = int.from_bytes(
        data[info.header_offset + 26 : info.header_offset + 28], "little"
    ), int.from_bytes(data[info.header_offset + 28 : info.header_offset + 30], "little")
    # BFINAL=1 with the reserved block type 11: zlib.error "invalid block type".
    data[info.header_offset + 30 + name_len + extra_len] = 0xFF
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf, pytest.raises(zlib.error):
        zf.read("rules.json")
    assert _refused(bytes(data)) == "invalid_bundle"


@pytest.mark.parametrize(
    "name", ["skills/nul/SKILL.md", "skills/x/COM1", "crons/con.py", "crons/Lpt3.py"]
)
def test_a_windows_device_name_is_refused(name):
    assert _refused(_zip({**_bundle(), name: "x"})) == "invalid_path"


def test_the_file_undo_refuses_when_an_imported_cron_was_switched_on(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        await _press(c, store, card_id)
        for job in app["state"].crons.jobs.values():
            job.enabled = True
        return await _press(c, store, card_id, op="undo"), app

    statuses, app = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert statuses == [200, 409]
    assert len(app["state"].crons.jobs) == 1
    assert (tmp_path / "ws" / ".kiro" / "skills" / "notes" / "SKILL.md").exists()


def test_the_file_undo_finds_a_script_cron_by_a_drive_letter_path(monkeypatch):
    """A Windows script spec is ``C:\\...\\run.py:main``: split on the last colon."""
    windows_path = "C:\\Users\\a\\.kiro\\crew\\crons\\run.py"
    crewmate_stage.write_ledger(
        "m1",
        {
            "member": "mate",
            "skills": [],
            "scripts": [{"path": windows_path, "sha256": "0" * 64}],
            "crons": ["sj1"],
            "cron_revisions": {"sj1": "rev-1"},
        },
    )
    monkeypatch.setattr("kiro_crew.dashboard.change_cards.cron_job_revision", lambda job: job.rev)
    crons = _Crons()
    crons.jobs["sj1"] = SimpleNamespace(
        id="sj1", script=f"{windows_path}:main", enabled=False, rev="rev-1"
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
        lambda request, op: _no_denial(),
    )

    @web.middleware
    async def as_card_step(request, handler):
        request["app"] = ""
        request[files_route.REQ_CARD_IMPORT_FILES] = "cc_1"
        return await handler(request)

    async def main():
        app = web.Application(middlewares=[as_card_step])
        app["state"] = SimpleNamespace(crons=crons)
        app.router.add_delete(
            "/api/members/{slug}/import-files", files_route.api_member_import_files_undo
        )
        async with TestClient(TestServer(app)) as c:
            r = await c.delete("/api/members/m1/import-files", json={"member": "mate"})
            return r.status

    assert asyncio.run(main()) == 200
    assert crons.jobs == {} and crewmate_stage.read_ledger("m1") is None


async def _no_denial():
    return None


def test_the_file_undo_refuses_when_a_paused_script_cron_was_edited(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    monkeypatch.setattr(
        "kiro_crew.dashboard.change_cards.cron_job_revision",
        lambda job: f"{job.cron_expr}|{job.message}",
    )

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        await _press(c, store, card_id)
        for job in app["state"].crons.jobs.values():
            job.cron_expr = "0 3 * * *"  # still paused, schedule changed
        return await _press(c, store, card_id, op="undo"), app

    statuses, app = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert statuses == [200, 409]
    assert len(app["state"].crons.jobs) == 1


def test_a_failed_card_keeps_its_staged_copy_for_a_retry(world, monkeypatch, tmp_path):
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    writes.fail.add("create")

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        statuses = await _press(c, store, card_id)
        return store.get(card_id), statuses

    rec, statuses = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert statuses == [500] and rec["status"] == "failed"
    assert crewmate_stage.load(rec["params"]["bundle_sha256"])


def test_a_resumed_undo_refuses_to_clear_rules_saved_since(world, monkeypatch, tmp_path):
    rec = {
        "kind": "crewmate.import",
        "params": _params(_zip(_bundle())),
        "after": {"exists": True, "member_id": "m1", "revision": "r1", "rules": "a" * 64},
        "evidence": [{"member_id": "m1", "name": "Release Captain"}],
        # cron delete (0), rules clear (1), crewmate delete (2)
        "plan": {
            "undo": [
                {"method": "DELETE", "path": "/api/crons/j1"},
                {"method": "PUT", "path": "/api/members/m1/rules"},
                {"method": "DELETE", "path": "/api/agents/Release%20Captain"},
            ]
        },
    }
    on_disk = {"rules": "b" * 64}

    async def read_state(kind, params, evidence, *, state, app):
        return {"exists": True, "member_id": "m1", "revision": "r1", **on_disk}

    monkeypatch.setattr(cards, "read_state", read_state)
    # Rules saved since the apply: the Undo's rules step refuses to clear them.
    assert asyncio.run(cards.resumed_undo_is_stale(rec, 1, state=None, app=None)) is True
    # After the Undo's own rules step cleared them, the crewmate delete still runs.
    on_disk["rules"] = "e" * 64
    assert asyncio.run(cards.resumed_undo_is_stale(rec, 2, state=None, app=None)) is False


def test_the_file_undo_refuses_a_cron_edited_after_its_check(world, monkeypatch, tmp_path):
    """The removal itself compares under the cron store's lock (``remove_job_if_async``)."""
    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    calls = {"n": 0}

    def revision(job):
        # The pre-check reads the import's own fingerprint; the locked compare a new one.
        calls["n"] += 1
        return "fp" if calls["n"] <= 2 else "edited"

    monkeypatch.setattr("kiro_crew.dashboard.change_cards.cron_job_revision", revision)

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        await _press(c, store, card_id)
        return await _press(c, store, card_id, op="undo"), app

    statuses, app = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert statuses == [200, 409]
    assert len(app["state"].crons.jobs) == 1
    assert (tmp_path / "ws" / ".kiro" / "skills" / "notes" / "SKILL.md").exists()


# ── rollback, rules clear, decoder errors ──


def test_a_failed_file_step_keeps_files_the_owner_changed_meanwhile(tmp_path):
    """The rollback after an awaited write fails removes only what still holds
    the bytes it wrote; an edited or added file stays, and so does its folder."""
    skill = tmp_path / "notes"
    skill.mkdir()
    (skill / "SKILL.md").write_bytes(b"# notes")
    (skill / "tool.py").write_bytes(b"edited by the owner")
    (skill / "mine.md").write_bytes(b"added by the owner")
    scripts = tmp_path / "imported" / "m1"
    scripts.mkdir(parents=True)
    (scripts / "run.py").write_bytes(b"def main(): ...")
    record = {
        "skills": [
            {
                "dir": str(skill),
                "files": {
                    "SKILL.md": hashlib.sha256(b"# notes").hexdigest(),
                    "tool.py": hashlib.sha256(b"print('hi')").hexdigest(),
                },
            }
        ],
        "scripts": [
            {
                "path": str(scripts / "run.py"),
                "sha256": hashlib.sha256(b"def main(): ...").hexdigest(),
            }
        ],
        "scripts_dir": str(scripts),
    }
    files_route._take_back(record)
    assert sorted(p.name for p in skill.iterdir()) == ["mine.md", "tool.py"]
    assert not scripts.exists()


def test_the_rules_clear_compares_under_the_rules_lock(tmp_path, monkeypatch):
    from kiro_crew.members_parts import prompt_layers

    monkeypatch.setattr(prompt_layers, "member_rules_path", lambda slug: tmp_path / f"{slug}.json")
    prompt_layers.write_member_rules("m1", member="Mate", text="Never push to main.")
    wrote = hashlib.sha256(b"Never push to main.").hexdigest()
    prompt_layers.write_member_rules("m1", member="Mate", text="Ask first.")  # another tab
    with pytest.raises(prompt_layers.MemberRulesChanged):
        prompt_layers.write_member_rules("m1", member="Mate", text="", expected_sha256=wrote)
    assert prompt_layers.read_member_rules("m1", "Mate") == "Ask first."
    mine = hashlib.sha256(b"Ask first.").hexdigest()
    prompt_layers.write_member_rules("m1", member="Mate", text="", expected_sha256=mine)
    assert prompt_layers.read_member_rules("m1", "Mate") == ""


@pytest.mark.parametrize(
    ("after", "expect"),
    [({"rules": "a" * 64}, "a" * 64), ({}, "__card_undo_missing_fingerprint__")],
)
def test_an_import_undo_arms_its_rules_clear_with_the_digest_it_wrote(after, expect):
    request: dict = {}
    rec = {"kind": "crewmate.import", "after": after, "evidence": [{"member_id": "m1"}]}
    step = {"method": "PUT", "path": "/api/members/m1/rules"}
    routes._arm_card_undo_cron_guard(request, rec, catalog.OP_UNDO, step)
    assert request[routes.REQ_CARD_UNDO_RULES_EXPECT] == expect
    # The apply's own rules write is not guarded.
    request = {}
    routes._arm_card_undo_cron_guard(request, rec, catalog.OP_APPLY, step)
    assert routes.REQ_CARD_UNDO_RULES_EXPECT not in request


def test_json_nested_past_the_parser_stack_is_a_refusal_not_a_crash():
    deep = "[" * 30000 + "]" * 30000
    assert _refused(_zip(_bundle(goals_DOT_json=deep))) == "invalid_bundle"


def test_an_entry_in_a_compression_the_export_never_writes_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, blob in _bundle().items():
            method = zipfile.ZIP_LZMA if name == "rules.json" else zipfile.ZIP_STORED
            zf.writestr(name, blob, compress_type=method)
    assert _refused(buf.getvalue()) == "invalid_bundle"


def test_a_cancel_keeps_a_copy_another_upload_holds(world, monkeypatch, tmp_path):
    """Two tabs, one file: the second upload holds the shared copy from before it
    stages until its card is stored, so cancelling the first card keeps it."""
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        card_id = await _raise(c, _zip(_carrying()))
        assert not routes._STAGE_HELD  # released once the card was stored
        rec = store.get(card_id)
        sha = rec["params"]["bundle_sha256"]
        monkeypatch.setitem(routes._STAGE_HELD, sha, 1)  # the second upload, mid-flight
        r = await c.post(
            f"/api/cards/{card_id}/cancel",
            json={"revision": rec["revision"]},
            headers={"X-Test-Auth": "owner"},
        )
        return r.status, sha

    status, sha = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert status == 200
    assert crewmate_stage.load(sha)


def test_a_failed_file_step_keeps_the_script_of_a_cron_the_owner_changed(
    world, monkeypatch, tmp_path
):
    """The owner edits the imported script cron while the picture is saved, and
    the picture fails: the cron stays, so its script stays; the rest goes."""
    store, writes = CardStore(tmp_path / "c.json"), _Writes()

    async def go(c, app):
        async def failing_avatar(key, ext, blob):
            for job in app["state"].crons.jobs.values():
                job.message = "the owner's edit"
            raise files_route._Refused(
                409, "avatar_file_missing", "the picture could not be staged"
            )

        monkeypatch.setattr(files_route, "_set_avatar", failing_avatar)
        card_id = await _raise(c, _zip(_carrying()))
        return await _press(c, store, card_id), app

    statuses, app = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert statuses[-1] == 409
    (job,) = app["state"].crons.jobs.values()
    script = Path(job.script.rpartition(":")[0])
    assert script.read_text(encoding="utf-8").startswith("def main")
    assert not (tmp_path / "ws" / ".kiro" / "skills" / "notes").exists()


def test_an_upload_waits_for_the_start_up_sweep_before_it_stages(world, monkeypatch, tmp_path):
    """The first card read starts the sweep; an upload arriving while it scans
    waits for it, so the copy it stages is never swept."""
    import threading

    from kiro_crew.testing.wait import async_wait_until

    store, writes = CardStore(tmp_path / "c.json"), _Writes()
    monkeypatch.setattr(routes, "_STAGE_SWEEP", None)
    monkeypatch.setattr(routes, "_run_stage_sweep", _REAL_RUN_SWEEP)
    started, gate = threading.Event(), threading.Event()
    real_sweep, real_wait = crewmate_stage.sweep, routes._sweep_staged_copies
    waiting: list[str] = []

    def held_sweep(keep):
        started.set()
        assert gate.wait(30), "the test never released the sweep"
        return real_sweep(keep)

    async def counted_wait(store_):
        waiting.append("caller")  # then awaits the shared run, with no await between
        await real_wait(store_)

    monkeypatch.setattr(crewmate_stage, "sweep", held_sweep)
    monkeypatch.setattr(routes, "_sweep_staged_copies", counted_wait)

    async def go(c, app):
        read = asyncio.create_task(c.get("/api/cards/pending", headers={"X-Test-Auth": "owner"}))
        try:
            await async_wait_until(started.is_set, timeout=30)
            upload = asyncio.create_task(_raise(c, _zip(_carrying())))
            # The upload is parked on the same sweep while it is still scanning.
            await async_wait_until(lambda: len(waiting) == 2, timeout=30)
            assert started.is_set() and not gate.is_set()
        finally:
            gate.set()
        await read
        card_id = await upload
        return store.get(card_id)["params"]["bundle_sha256"]

    sha = _go(world, store, writes, monkeypatch, tmp_path, go)
    assert crewmate_stage.load(sha)
