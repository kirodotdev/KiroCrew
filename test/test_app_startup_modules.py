"""App startup modules (``ui.startup``): admission, approval and the ``/api/apps`` row."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from app_manager_helpers import app_home_fixture  # noqa: F401 -- requested by name
from app_manager_helpers import _make_app_source

from kiro_crew.apps import protected_app_state
from kiro_crew.apps import routes as app_routes
from kiro_crew.apps import startup_modules
from kiro_crew.apps.manager import app_dir, enable_app, install_app, uninstall_app, update_app
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_sections import UIConfig
from kiro_crew.platform_compat import IS_POSIX, chmod_safe, effective_uid

_UI = {"entry": "page.mjs", "startup": "startup.mjs"}


def _browser_only(**extra):
    return {"name": "ghost", "version": "1.0.0", "ui": dict(_UI), "iconPath": "ui/x.svg", **extra}


@pytest.fixture(name="no_confinement")
def no_confinement_fixture(monkeypatch):
    monkeypatch.setattr("kiro_crew.sandbox.agent_confinement_evidence", lambda: None)


def _install(tmp_path, **manifest):
    src = _make_app_source(tmp_path, name="ghost", ui=dict(_UI), **manifest)
    result = install_app(src)
    assert result.ok, result.error
    enabled = enable_app("ghost")
    assert enabled.ok, enabled.error
    return src


def _row_app(owner=True):
    apps = [{"name": "ghost"}]
    startup_modules.stamp_startup(apps, owner=owner)
    return apps[0]


def _row(owner=True):
    apps = [{"name": "ghost"}]
    startup_modules.stamp_startup(apps, owner=owner)
    return apps[0].get("startup")


# --- manifest ----------------------------------------------------------------


def test_ui_startup_round_trips_and_is_omitted_when_undeclared():
    assert UIConfig.from_dict(_UI).to_dict()["startup"] == "startup.mjs"
    assert "startup" not in UIConfig.from_dict({"entry": "page.mjs"}).to_dict()
    assert UIConfig.from_dict({"startup": None}).startup == ""


@pytest.mark.parametrize(
    ("startup", "message"),
    [
        ("style.css", "ui.startup refused: its ui.startup 'style.css' is not a .mjs or .js module"),
        ("../escape.mjs", "ui.startup"),
    ],
)
def test_ui_startup_validation_refuses(startup, message):
    manifest = AppManifest.from_dict(
        {
            "name": "ghost",
            "version": "1.0.0",
            "displayName": "G",
            "description": "d",
            "ui": {"startup": startup},
        }
    )
    errors = manifest.validate()
    assert any(e.startswith(message) for e in errors), errors


# --- eligibility -------------------------------------------------------------


def test_browser_only_manifest_is_eligible():
    raw = _browser_only(contributes={"panelTabs": [], "commands": []}, desktop={})
    assert startup_modules.eligibility_refusal(raw, "gateway") is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("highlights", ["x"]),
        ("useCases", ["x"]),
        ("configuration", ["x"]),
        ("screenshotsDark", ["ui/s.png"]),
        ("heroImage", "ui/h.png"),
        ("heroImageDark", "ui/h.png"),
        ("heroImageDetail", "ui/h.png"),
        ("heroImageDetailDark", "ui/h.png"),
        ("iconPathDark", "ui/x.svg"),
        ("icon", "Star"),
    ],
)
def test_store_page_keys_keep_an_app_eligible(key, value):
    assert startup_modules.eligibility_refusal(_browser_only(**{key: value}), "gateway") is None


@pytest.mark.parametrize(
    ("raw", "resources", "reason"),
    [
        (
            _browser_only(backend={"entryPoint": "x"}),
            "gateway",
            "it declares 'backend', which runs outside the browser",
        ),
        (
            _browser_only(openCommand="open x"),
            "gateway",
            "it declares 'openCommand', which runs outside the browser",
        ),
        (
            _browser_only(somethingNew=1),
            "gateway",
            "it declares 'somethingNew', which runs outside the browser",
        ),
        (
            _browser_only(contributes={"fileMenuItems": []}),
            "gateway",
            "it declares 'contributes.fileMenuItems', which runs outside the browser",
        ),
        (
            _browser_only(ui={**_UI, "overlays": [{"id": "a"}]}),
            "gateway",
            "it declares 'ui.overlays'",
        ),
        (_browser_only(), "app", "the app manages its own resources"),
    ],
)
def test_ineligible_manifest_names_its_reason(raw, resources, reason):
    assert startup_modules.eligibility_refusal(raw, resources) == reason


# --- state -------------------------------------------------------------------


def _state(**over):
    args = {
        "entry": "startup.mjs",
        "version": "1.0.0",
        "digest": lambda: "abc",
        "enabled": True,
        "ceiling_reason": None,
        "eligibility": None,
        "approval": {"version": "1.0.0", "entry": "startup.mjs", "digest": "abc"},
    }
    return startup_modules.startup_state(**{**args, **over})["state"]


def test_state_admits_only_an_approved_eligible_enabled_app():
    assert _state() == "admitted"
    assert _state(enabled=False) == "denied"
    assert _state(ceiling_reason="third-party apps are off") == "denied"
    assert _state(eligibility="it declares 'backend'") == "denied"
    assert _state(approval=None) == "needs-approval"
    assert _state(version="1.0.1") == "needs-approval"  # an update asks again
    assert _state(entry="other.mjs") == "needs-approval"  # so does a changed entry
    assert _state(digest=lambda: "def") == "needs-approval"  # and changed code


@pytest.mark.parametrize(
    "text", ["", "[]", '{"version": 1}', '{"version": "1", "entry": "s"}', "{"]
)
def test_malformed_approvals_read_as_none(app_home, text):
    protected_app_state.write_startup_text("ghost", text)
    assert startup_modules.read_approval("ghost") is None


# --- approval end to end ------------------------------------------------------


def test_approve_admits_then_revoke_withdraws(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    assert _row() == {
        "entry": "startup.mjs",
        "state": "needs-approval",
        "reason": "the owner has not approved this code",
    }

    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    # Stored outside the app's tree, with the app's other owner decisions.
    text = protected_app_state.read_startup_text("ghost")
    assert text is not None
    record = json.loads(text)
    empty_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert record == {
        "version": "1.0.0",
        "entry": "startup.mjs",
        "digest": empty_sha256,
        "files": {},
    }
    assert not list(app_dir("ghost").rglob("*startup*.json"))
    assert _row()["state"] == "admitted"
    # Another caller (a non-owner session) is never told it may mount the module.
    assert _row(owner=False) == {
        "entry": "startup.mjs",
        "state": "denied",
        "reason": "only the owner's dashboard runs it",
    }

    startup_modules.set_approval("ghost", False)
    assert _row()["state"] == "needs-approval"


def test_approval_is_refused_under_agent_confinement(app_home, tmp_path, monkeypatch):
    _install(tmp_path)
    monkeypatch.setattr(
        "kiro_crew.sandbox.agent_confinement_evidence", lambda: "running inside the agent sandbox"
    )
    assert startup_modules.set_approval("ghost", True) == {
        "error": "refused: running inside the agent sandbox"
    }
    assert protected_app_state.read_startup_text("ghost") is None


def test_ineligible_app_stays_denied_after_approval(app_home, tmp_path, no_confinement):
    _install(tmp_path, openCommand="true")
    assert startup_modules.set_approval("ghost", True)["state"] == "denied"
    assert _row()["reason"] == "it declares 'openCommand', which runs outside the browser"


def test_app_without_startup_entry_gets_no_row(app_home, tmp_path):
    src = _make_app_source(tmp_path, name="plain", ui={"entry": "page.mjs"})
    assert install_app(src).ok
    apps = [{"name": "plain"}]
    startup_modules.stamp_startup(apps, owner=True)
    assert "startup" not in apps[0]


def _write_startup(code):
    ui = app_dir("ghost") / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "startup.mjs").write_text(code, encoding="utf-8")


def test_code_changed_after_approval_needs_approval_again(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    _write_startup("export default () => null\n")
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    # Same name, version and entry, different bytes: what an agent shell could write.
    _write_startup("export default () => { steal() }\n")
    assert _row()["state"] == "needs-approval"
    _write_startup("export default () => null\n")
    assert _row()["state"] == "admitted"


def test_uninstall_drops_the_approval(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    startup_modules.set_approval("ghost", True)
    assert uninstall_app("ghost").ok
    assert protected_app_state.read_startup_text("ghost") is None


def test_revoke_runs_for_an_app_that_is_gone(app_home, no_confinement):
    record = {"version": "1.0.0", "entry": "s.mjs", "digest": "x"}
    protected_app_state.write_startup_text("ghost", json.dumps(record))
    assert "error" not in startup_modules.set_approval("ghost", False)
    assert startup_modules.read_approval("ghost") is None


def test_digest_is_not_read_for_a_disabled_or_unapproved_app():
    def unreadable():
        raise AssertionError("ui/ was read")

    assert _state(enabled=False, digest=unreadable) == "denied"
    assert _state(approval=None, digest=unreadable) == "needs-approval"


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (
            "../../other/ui/x.mjs",
            "its ui.startup '../../other/ui/x.mjs' is not a plain path inside ui/",
        ),
        ("/abs/x.mjs", "its ui.startup '/abs/x.mjs' is not a plain path inside ui/"),
        ("a\\b.mjs", "its ui.startup 'a\\\\b.mjs' is not a plain path inside ui/"),
        ("style.css", "its ui.startup 'style.css' is not a .mjs or .js module"),
        (
            "%2e%2e/%2e%2e/other/ui/x.mjs",
            "its ui.startup '%2e%2e/%2e%2e/other/ui/x.mjs' is not a plain path inside ui/",
        ),
        (".%2E/x.mjs", "its ui.startup '.%2E/x.mjs' is not a plain path inside ui/"),
        (".\t./x.mjs", "its ui.startup '.\\t./x.mjs' is not a plain path inside ui/"),
        ("dist/.hidden.mjs", "its ui.startup 'dist/.hidden.mjs' is not a plain path inside ui/"),
    ],
)
def test_entry_refusal(entry, reason):
    assert startup_modules.entry_refusal(entry) == reason


def test_an_entry_edited_to_escape_ui_is_denied_after_approval(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    manifest = app_dir("ghost") / "app.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["ui"]["startup"] = "../../helper/ui/x.mjs"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert _row()["state"] == "denied"
    assert startup_modules.set_approval("ghost", True) == {
        "error": "app 'ghost': its ui.startup '../../helper/ui/x.mjs' is not a plain path inside ui/"
    }


def test_digest_covers_every_file_skips_links_and_refuses_oversize(app_home, tmp_path, monkeypatch):
    _install(tmp_path)
    ui = app_dir("ghost") / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "core.JS").write_text("one", encoding="utf-8")
    before = startup_modules.ui_digest("ghost")
    (ui / "core.JS").write_text("two", encoding="utf-8")  # upper-case suffix still counts
    assert startup_modules.ui_digest("ghost") != before
    after = startup_modules.ui_digest("ghost")
    (ui / "link.mjs").symlink_to(ui / "core.JS")  # the route refuses links; so does the digest
    assert startup_modules.ui_digest("ghost") == after
    monkeypatch.setattr(startup_modules, "_MAX_FILE_BYTES", 2)
    with pytest.raises(startup_modules.OversizeFile):
        startup_modules.ui_digest("ghost")


def test_an_oversize_file_blocks_approval_and_admission(
    app_home, tmp_path, monkeypatch, no_confinement
):
    _install(tmp_path)
    _write_startup("export default () => null\n")
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    monkeypatch.setattr(startup_modules, "_MAX_FILE_BYTES", 4)
    assert "startup" not in _row_app()  # the read fails closed: no row, nothing mounts
    assert startup_modules.set_approval("ghost", True)["error"].startswith(
        "app 'ghost': could not hash its ui/"
    )


def test_a_refused_approval_is_audited(app_home, tmp_path, monkeypatch):
    events = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.sandbox.agent_confinement_evidence", lambda: "sandboxed")
    startup_modules.set_approval("ghost", True)
    assert [(e["operation"], e["outcome"], e["source"]) for e in events] == [
        ("app_startup_approve", "denied", "cli")
    ]


def test_a_failed_uninstall_restores_the_approval(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    startup_modules.set_approval("ghost", True)
    approved = startup_modules.read_approval("ghost")
    snapshot = protected_app_state.snapshot_app("ghost")
    protected_app_state.forget_grants("ghost")
    assert startup_modules.read_approval("ghost") is None
    protected_app_state.restore_app("ghost", snapshot)
    assert startup_modules.read_approval("ghost") == approved


# --- serving: the static route re-checks the approval -------------------------

# Over the route's gzip floor, so the checked bytes also take the gzip path.
_ENTRY = b"import { ok } from './chunk.mjs'\n" + b"// pad\n" * 200 + b"export default () => ok\n"
_CHUNK = b"export const ok = null\n"
_SWAPPED = b"steal(document.cookie)\n"


def _approve_tree(tmp_path):
    _install(tmp_path)
    ui = app_dir("ghost") / "ui"
    ui.mkdir()
    (ui / "startup.mjs").write_bytes(_ENTRY)
    (ui / "chunk.mjs").write_bytes(_CHUNK)
    (ui / "icon.svg").write_bytes(b"<svg/>")
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    return ui


async def _get(path, **kw):
    app = web.Application()
    app.router.add_get("/apps/{name}/ui/{path:.*}", app_routes.handle_app_ui_file)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/apps/ghost/ui/{path}", **kw)
        return resp.status, await resp.read(), resp.headers


_REFUSED = json.dumps(
    {
        "code": "app_startup_approval_required",
        "error": "ui changed since the startup approval",
        "reason": "The ui/ file changed since approval or its approval cannot be verified.",
        "remedy": (
            "Run kirocrew app startup ghost to approve the current code, "
            "or kirocrew app startup ghost --revoke."
        ),
    }
).encode("utf-8")


@pytest.mark.asyncio
async def test_an_entry_swapped_after_approval_answers_409_and_sends_nothing(
    app_home, tmp_path, no_confinement
):
    ui = _approve_tree(tmp_path)
    (ui / "startup.mjs").write_bytes(_SWAPPED)
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (409, _REFUSED)


@pytest.mark.asyncio
async def test_an_approval_read_fault_answers_409(app_home, tmp_path, no_confinement, monkeypatch):
    ui = _approve_tree(tmp_path)
    (ui / "startup.mjs").write_bytes(_SWAPPED)

    def unreadable(name):
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(protected_app_state, "read_startup_text", unreadable)
    assert _row()["state"] == "needs-approval"
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (409, _REFUSED)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "[]",
        '{"version": "1", "entry": "startup.mjs"}',
        '{"version": 1, "entry": "startup.mjs", "digest": "x"}',
        '{"version": "1", "entry": null, "digest": "x"}',
        '{"version": "1", "entry": "startup.mjs", "digest": false}',
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_approval_answers_409(app_home, tmp_path, no_confinement, text):
    ui = _approve_tree(tmp_path)
    (ui / "startup.mjs").write_bytes(_SWAPPED)
    protected_app_state.write_startup_text("ghost", text)
    assert _row()["state"] == "needs-approval"
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (409, _REFUSED)


@pytest.mark.skipif(not IS_POSIX, reason="needs POSIX file permissions")
@pytest.mark.asyncio
async def test_an_unreadable_approval_answers_409(app_home, tmp_path, no_confinement):
    if effective_uid() == 0:
        pytest.skip("root can read chmod 000 files")
    ui = _approve_tree(tmp_path)
    (ui / "startup.mjs").write_bytes(_SWAPPED)
    record = app_home / "member-records" / protected_app_state.APP_STARTUP_KIND / "ghost.json"
    mode = stat.S_IMODE(record.stat().st_mode)
    try:
        chmod_safe(record, 0)
        assert stat.S_IMODE(record.stat().st_mode) == 0
        assert _row()["state"] == "needs-approval"
        status, body, _ = await _get("startup.mjs")
        assert (status, body) == (409, _REFUSED)
    finally:
        chmod_safe(record, mode)


def test_an_unsafe_app_name_is_refused_at_serve_time(app_home):
    with pytest.raises(ValueError, match="unsafe app name: 'GHOST'"):
        startup_modules.approved_module("GHOST", "startup.mjs", -1)


@pytest.mark.asyncio
async def test_a_js_chunk_swapped_after_approval_answers_409(app_home, tmp_path, no_confinement):
    ui = _approve_tree(tmp_path)
    path = ui / "chunk.js"
    path.write_bytes(_CHUNK)
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    path.write_bytes(_SWAPPED)
    status, body, _ = await _get("chunk.js")
    assert (status, body) == (409, _REFUSED)


@pytest.mark.asyncio
async def test_a_lazy_chunk_swapped_after_approval_answers_409(app_home, tmp_path, no_confinement):
    ui = _approve_tree(tmp_path)
    (ui / "chunk.mjs").write_bytes(_SWAPPED)
    status, body, _ = await _get("chunk.mjs")
    assert (status, body) == (409, _REFUSED)


@pytest.mark.asyncio
async def test_an_unchanged_approved_tree_serves_entry_and_chunk(
    app_home, tmp_path, no_confinement
):
    _approve_tree(tmp_path)
    status, body, headers = await _get("startup.mjs")
    assert (status, body, headers["Content-Encoding"]) == (200, _ENTRY, "gzip")
    status, body, headers = await _get("chunk.mjs")
    assert (status, body, headers["Content-Length"]) == (200, _CHUNK, str(len(_CHUNK)))


@pytest.mark.asyncio
async def test_a_non_module_file_still_serves_after_the_js_was_swapped(
    app_home, tmp_path, no_confinement
):
    ui = _approve_tree(tmp_path)
    (ui / "startup.mjs").write_bytes(_SWAPPED)
    status, body, _ = await _get("icon.svg")
    assert (status, body) == (200, b"<svg/>")


@pytest.mark.asyncio
async def test_an_app_with_no_approval_serves_its_js_as_before(app_home, tmp_path):
    _install(tmp_path)
    _write_startup("export default () => null\n")
    (app_dir("ghost") / "ui" / "startup.mjs").write_bytes(_SWAPPED)
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (200, _SWAPPED)


@pytest.mark.asyncio
async def test_a_version_bumped_with_the_swap_still_answers_409(app_home, tmp_path, no_confinement):
    """``app.json`` is agent-writable too, and the browser chose to mount from an
    earlier list read, so a stale approval is no reason to serve unchecked bytes."""
    ui = _approve_tree(tmp_path)
    manifest = app_dir("ghost") / "app.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**raw, "version": "1.0.1"}), encoding="utf-8")
    (ui / "startup.mjs").write_bytes(_SWAPPED)
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (409, _REFUSED)


@pytest.mark.asyncio
async def test_a_swap_after_the_check_is_never_sent(
    app_home, tmp_path, no_confinement, monkeypatch
):
    """The route sends the bytes it hashed, not a second read of the file."""
    ui = _approve_tree(tmp_path)
    real_check = app_routes.approved_module

    def check_then_swap(name, path, fd):
        checked = real_check(name, path, fd)
        (ui / "startup.mjs").write_bytes(_SWAPPED)  # in place: the open descriptor sees it
        return checked

    monkeypatch.setattr(app_routes, "approved_module", check_then_swap)
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (200, _ENTRY)


@pytest.mark.asyncio
async def test_a_forged_etag_cannot_revalidate_an_unchecked_copy(
    app_home, tmp_path, no_confinement
):
    """Inode, size and mtime are the agent's to restore, so the ETag of checked
    bytes also names the approved digest, and a 304 means the cached copy is it."""
    _install(tmp_path)
    _write_startup("export default () => evil\n")
    path = app_dir("ghost") / "ui" / "startup.mjs"
    status, _, headers = await _get("startup.mjs")
    assert status == 200
    unchecked_etag = headers["ETag"]
    st = path.stat()
    path.write_bytes(b"export default () => null\n")  # same size, same inode
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    approval = startup_modules.read_approval("ghost")
    assert approval is not None
    digest = approval["digest"]

    status, body, headers = await _get("startup.mjs", headers={"If-None-Match": unchecked_etag})
    assert (status, body) == (200, b"export default () => null\n")
    assert headers["ETag"] == f'{unchecked_etag[:-1]}-{digest}"'
    status, body, _ = await _get("startup.mjs", headers={"If-None-Match": headers["ETag"]})
    assert (status, body) == (304, b"")


@pytest.mark.asyncio
async def test_a_file_the_digest_cannot_read_fails_closed(
    app_home, tmp_path, no_confinement, monkeypatch
):
    _approve_tree(tmp_path)
    monkeypatch.setattr(startup_modules, "_MAX_FILE_BYTES", len(_CHUNK) - 1)
    status, body, _ = await _get("chunk.mjs")
    assert (status, body) == (409, _REFUSED)


_ASSET_TYPES = (
    "mjs",
    "js",
    "svg",
    "json",
    "css",
    "map",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "woff",
    "woff2",
    "ttf",
)
_ASSET_BYTES = b"approved asset\n"


@pytest.mark.parametrize("change", ["unchanged", "swapped", "added", "stat_forgery"])
@pytest.mark.asyncio
async def test_html_is_refused_even_for_an_approved_app(app_home, tmp_path, no_confinement, change):
    ui = _approve_tree(tmp_path)
    path = ui / "page.html"
    if change != "added":
        path.write_bytes(b"approved")
        assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    before = path.stat() if change == "stat_forgery" else None
    if change != "unchanged":
        path.write_bytes(b"replaced")
    if before is not None:
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert (path.stat().st_size, path.stat().st_mtime_ns) == (
            before.st_size,
            before.st_mtime_ns,
        )
    status, body, _ = await _get("page.html")
    assert (status, body) == (403, b'{"error": "file type \'.html\' not allowed"}')


@pytest.mark.parametrize("fault", ["removed", "oversize"])
@pytest.mark.asyncio
async def test_an_unavailable_approved_file_answers_409(
    app_home, tmp_path, no_confinement, monkeypatch, fault
):
    ui = _approve_tree(tmp_path)
    if fault == "removed":
        (ui / "chunk.mjs").unlink()
    else:
        monkeypatch.setattr(app_routes, "_UI_MAX_BYTES", len(_CHUNK) - 1)
    status, body, _ = await _get("chunk.mjs")
    assert (status, body) == (409, _REFUSED)


@pytest.mark.parametrize("extension", _ASSET_TYPES)
@pytest.mark.asyncio
async def test_every_approved_file_type_checks_its_bytes(
    app_home, tmp_path, no_confinement, extension
):
    ui = _approve_tree(tmp_path)
    path = ui / f"asset.{extension}"
    path.write_bytes(_ASSET_BYTES)
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    path.write_bytes(_SWAPPED)
    status, body, _ = await _get(path.name)
    assert (status, body) == (409, _REFUSED)


@pytest.mark.parametrize("extension", _ASSET_TYPES)
@pytest.mark.asyncio
async def test_every_unchanged_approved_file_type_serves_checked_bytes(
    app_home, tmp_path, no_confinement, extension
):
    ui = _approve_tree(tmp_path)
    path = ui / f"asset.{extension}"
    path.write_bytes(_ASSET_BYTES)
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    approval = startup_modules.read_approval("ghost")
    assert approval is not None
    status, body, headers = await _get(path.name, headers={"Accept-Encoding": "identity"})
    assert (status, body, headers["Content-Length"]) == (200, _ASSET_BYTES, str(len(_ASSET_BYTES)))
    assert headers["ETag"].endswith(f'-{approval["digest"]}"')
    assert headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("extension", ["js", "mjs", "svg", "json", "css", "map"])
@pytest.mark.asyncio
async def test_a_file_added_after_approval_is_not_served(
    app_home, tmp_path, no_confinement, extension
):
    ui = _approve_tree(tmp_path)
    path = ui / f"added.{extension}"
    path.write_bytes(_ASSET_BYTES)
    status, body, _ = await _get(path.name)
    assert (status, body) == (409, _REFUSED)


@pytest.mark.parametrize("extension", ["js", "mjs", "svg", "json", "css", "map"])
@pytest.mark.asyncio
async def test_restoring_size_and_mtime_does_not_restore_approval(
    app_home, tmp_path, no_confinement, extension
):
    ui = _approve_tree(tmp_path)
    path = ui / f"asset.{extension}"
    path.write_bytes(b"approved")
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    status, _, headers = await _get(path.name)
    assert status == 200
    before = path.stat()
    path.write_bytes(b"replaced")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    status, body, _ = await _get(path.name, headers={"If-None-Match": headers["ETag"]})
    assert (status, body) == (409, _REFUSED)


@pytest.mark.asyncio
async def test_a_swap_between_read_and_hash_never_sends_unapproved_bytes(
    app_home, tmp_path, no_confinement, monkeypatch
):
    ui = _approve_tree(tmp_path)
    path = ui / "startup.mjs"
    path.write_bytes(_SWAPPED)
    real_read = startup_modules._read_capped
    reads = []

    def read_then_restore(fd, rel):
        data = real_read(fd, rel)
        reads.append(rel)
        path.write_bytes(_ENTRY)
        return data

    monkeypatch.setattr(startup_modules, "_read_capped", read_then_restore)
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (409, _REFUSED)
    assert reads == ["startup.mjs"]
    assert path.read_bytes() == _ENTRY


@pytest.mark.parametrize("corruption", ["old_format", "digest", "files", "bad_hash", "bad_path"])
@pytest.mark.asyncio
async def test_an_invalid_file_map_requires_reapproval(
    app_home, tmp_path, no_confinement, corruption
):
    _approve_tree(tmp_path)
    text = protected_app_state.read_startup_text("ghost")
    assert text is not None
    record = json.loads(text)
    if corruption == "old_format":
        record.pop("files", None)
    elif corruption == "digest":
        record["digest"] = "0" * 64
    elif corruption == "files":
        record["files"]["icon.svg"] = hashlib.sha256(_SWAPPED).hexdigest()
    elif corruption == "bad_hash":
        record["files"] = {"startup.mjs": "not a hash"}
    else:
        record["files"] = {"../startup.mjs": hashlib.sha256(_ENTRY).hexdigest()}
    protected_app_state.write_startup_text("ghost", json.dumps(record))
    assert _row()["state"] == "needs-approval"
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (409, _REFUSED)


@pytest.mark.asyncio
async def test_serving_one_file_reads_only_that_file_in_a_large_approved_tree(
    app_home, tmp_path, no_confinement, monkeypatch
):
    ui = _approve_tree(tmp_path)
    for index in range(3000):
        (ui / f"asset-{index}.json").write_bytes(_ASSET_BYTES)
    path = ui / "chunk.js"
    path.write_bytes(_CHUNK)
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    # An agent can add files, but cannot inflate the work of this request that way.
    (ui / "unapproved.json").write_bytes(_SWAPPED)
    reads = []
    opens = []
    real_read = startup_modules._read_capped
    real_open = os.open

    def counted_read(fd, rel):
        reads.append(rel)
        return real_read(fd, rel)

    def counted_open(file, *args, **kwargs):
        if str(file).endswith((".mjs", ".js", ".svg", ".json")):
            opens.append(str(file).replace("\\", "/").rsplit("/", 1)[-1])
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(startup_modules, "_read_capped", counted_read)
    monkeypatch.setattr(os, "open", counted_open)
    status, body, _ = await _get("chunk.js")
    assert reads == ["chunk.js"]
    assert opens == ["chunk.js"]
    assert (status, body) == (200, _CHUNK)


@pytest.mark.asyncio
async def test_revoke_works_after_the_manifest_drops_startup(
    app_home, tmp_path, no_confinement, capsys
):
    from kiro_crew.cli_commands_app import _handle_app

    ui = _approve_tree(tmp_path)
    manifest = app_dir("ghost") / "app.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["ui"].pop("startup")
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    (ui / "startup.mjs").write_bytes(_SWAPPED)
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (409, _REFUSED)
    _handle_app(argparse.Namespace(app_action="startup", name="ghost", revoke=True))
    assert protected_app_state.read_startup_text("ghost") is None
    assert "startup approval withdrawn" in capsys.readouterr().out
    status, body, _ = await _get("startup.mjs")
    assert (status, body) == (200, _SWAPPED)


@pytest.mark.parametrize("fails", [False, True])
def test_update_dropping_startup_forgets_approval_or_restores_on_failure(
    app_home, tmp_path, no_confinement, monkeypatch, fails
):
    from kiro_crew.apps import manager

    _approve_tree(tmp_path)
    before = protected_app_state.read_startup_text("ghost")
    assert before is not None
    source = _make_app_source(
        tmp_path / "update", name="ghost", version="1.0.1", ui={"entry": "page.mjs"}
    )
    write_record = manager._write_installed

    def fail_new_record(name, meta):
        if meta.version == "1.0.1":
            assert protected_app_state.read_startup_text(name) is None
            raise OSError("cannot write new record")
        return write_record(name, meta)

    if fails:
        monkeypatch.setattr(manager, "_write_installed", fail_new_record)
    result = update_app(source)
    assert result.ok is not fails, result.error
    assert protected_app_state.read_startup_text("ghost") == (before if fails else None)
    if fails:
        assert _row()["state"] == "admitted"
    else:
        assert _row() is None


@pytest.mark.parametrize("fails", [False, True])
def test_install_without_startup_forgets_stale_approval_or_restores_on_failure(
    app_home, tmp_path, monkeypatch, fails
):
    from kiro_crew.apps import manager

    text = '{"old": "approval"}'
    protected_app_state.write_startup_text("ghost", text)
    source = _make_app_source(tmp_path, name="ghost", ui={"entry": "page.mjs"})
    if fails:

        def fail_secret(*args):
            raise OSError("cannot write secret")

        monkeypatch.setattr("kiro_crew.dashboard.token_auth.write_app_secret", fail_secret)
    result = manager.install_app(source)
    assert result.ok is not fails, result.error
    assert protected_app_state.read_startup_text("ghost") == (text if fails else None)


def test_approval_digest_is_derived_from_the_stored_file_map(app_home, tmp_path, no_confinement):
    ui = _approve_tree(tmp_path)
    (ui / "nested").mkdir()
    (ui / "nested" / "x.json").write_bytes(_ASSET_BYTES)
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    text = protected_app_state.read_startup_text("ghost")
    assert text is not None
    record = json.loads(text)
    expected_files = {
        "chunk.mjs": hashlib.sha256(_CHUNK).hexdigest(),
        "icon.svg": hashlib.sha256(b"<svg/>").hexdigest(),
        "nested/x.json": hashlib.sha256(_ASSET_BYTES).hexdigest(),
        "startup.mjs": hashlib.sha256(_ENTRY).hexdigest(),
    }
    assert record["files"] == expected_files
    digest = hashlib.sha256()
    for rel, hex_digest in sorted(expected_files.items()):
        digest.update(rel.encode("utf-8") + b"\0" + bytes.fromhex(hex_digest))
    assert record["digest"] == digest.hexdigest()
    assert startup_modules.ui_digest("ghost") == record["digest"]
