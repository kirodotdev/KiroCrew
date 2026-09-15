"""Windows-support tests for the ``aws-control`` builtin app.

Two things are pinned here.

**The manifest declaration.** ``aws-control``'s backend is in-process (hooks +
routes loaded inside the gateway, no ``backend.type`` launcher), and its Python
was already written cross-platform: ``platform_compat.IS_POSIX`` branches around
``chmod``, ``getattr(os, "O_NOFOLLOW", 0)`` for the flags Windows lacks,
``platform_compat.pin_directory`` / ``file_lock`` / ``is_link_or_junction``
instead of raw POSIX calls, ``tempfile`` instead of ``/tmp``, and the standard
library's ``tarfile`` instead of shelling out to ``tar``. ``platform.os`` is a
published capability label, not an enable gate -- ``apps/routes.py`` only calls
``supports_platform`` for a ``platform.installMode == "client"`` app, which no
builtin sets, so an absent or wrong ``platform`` block never actually blocked
this app from running on Windows; it only misinformed the App Store detail
page a user reads before enabling it.

**The one feature that genuinely cannot run there.** The sessions backup archives
agent-writable directories and uploads them unattended, so it walks the tree
descriptor-pinned (``openat``) and REFUSES rather than falling back to a
name-based walk that a junction swapped in mid-descent could redirect. Windows
has neither ``dir_fd`` support nor an fd-accepting ``os.scandir``, so the refusal
is permanent there. These tests pin that the condition is answerable BEFORE the
work rather than only as a ``RuntimeError`` inside a failed run record after the
owner presses the button, that both surfaces quote one explanation, and that the
snapshot backup is untouched by it.

Everything resolves relative to this file, so the suite is machine independent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod
from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest, PlatformConfig

# .../aws_control/tests/test_aws_control_windows.py
#   parents[0] = tests
#   parents[1] = aws_control   (the app root)
APP_ROOT = Path(__file__).resolve().parents[1]
APP_JSON = APP_ROOT / "app.json"

APP_NAME = "aws-control"
DECLARED_OS = ["macos", "linux", "windows"]


@pytest.fixture(scope="module")
def raw_manifest() -> dict[str, Any]:
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> AppManifest:
    return AppManifest.from_json_file(APP_JSON)


# ---------------------------------------------------------------------------
# 1. The platform declaration
# ---------------------------------------------------------------------------


def test_typed_manifest_carries_the_same_platform_list(manifest: AppManifest):
    # The raw JSON and the parsed manifest must not disagree: the gates read the
    # typed value, while humans read the file.
    assert manifest.platform.os == DECLARED_OS


def test_the_implicit_default_would_have_excluded_windows():
    """Documents why the block is needed at all, so the test above is meaningful."""
    default_cfg = PlatformConfig()
    assert default_cfg.os == ["macos", "linux"]
    assert not default_cfg.supports_platform("win32")


def test_discovery_serializes_the_platform_declaration():
    """``PlatformConfig.to_dict()`` emits ``os`` ONLY when it differs from the
    default, so a regression back to ``["macos", "linux"]`` does not merely change
    the value -- it removes the key, and the App Store detail page then renders no
    platform row at all.
    """
    entry = next((a for a in discover_builtin_apps() if a.get("name") == APP_NAME), None)
    assert entry is not None, f"{APP_NAME!r} not discovered"
    assert entry["platform"]["os"] == DECLARED_OS


def test_manifest_validates_with_no_errors(manifest: AppManifest):
    # Guards the hand-edited JSON: a malformed manifest is dropped by discovery
    # rather than reported, so the app would just vanish from the App Store.
    assert manifest.validate(app_root=APP_ROOT) == []


def test_platform_block_did_not_disturb_the_rest_of_the_manifest(raw_manifest: dict[str, Any]):
    # The block was inserted between `defaultEnabled` and `permissions`; this pins
    # that the surrounding contract survived the hand edit.
    assert raw_manifest["defaultEnabled"] is False
    assert raw_manifest["backend"]["routes"] == "backend.routes:register_routes"
    assert raw_manifest["permissions"]["jobs"] is True
    assert raw_manifest["ui"]["pages"][0]["route"] == "/aws-control"


# ---------------------------------------------------------------------------
# 2. The sessions backup degrades honestly instead of raising at the owner
# ---------------------------------------------------------------------------


def test_sessions_is_reported_unavailable_when_the_traversal_cannot_be_pinned(monkeypatch):
    """This is the Windows case, expressed as the condition rather than as a name.

    Asserting on ``IS_WINDOWS`` would make the test pass for the wrong reason on a
    POSIX runner, so the capability flag is what is flipped -- it is also the exact
    thing the production code branches on.
    """
    monkeypatch.setattr(backup_mod, "_CAN_PIN_TRAVERSAL", False)
    reason = backup_mod.kind_unavailable_reason(backup_mod.KIND_SESSIONS)
    assert reason
    assert "openat" in reason


def test_snapshot_stays_available_on_every_platform(monkeypatch):
    """The snapshot backup goes through the standard library's ``tarfile`` and
    never needs a pinned descent, so the sessions refusal must not take it down
    with it -- losing the nightly backup on Windows would be a real regression.
    """
    monkeypatch.setattr(backup_mod, "_CAN_PIN_TRAVERSAL", False)
    assert backup_mod.kind_unavailable_reason(backup_mod.KIND_SNAPSHOT) is None


def test_nothing_is_unavailable_where_the_traversal_can_be_pinned(monkeypatch):
    monkeypatch.setattr(backup_mod, "_CAN_PIN_TRAVERSAL", True)
    for kind in backup_mod.JOB_KINDS:
        assert backup_mod.kind_unavailable_reason(kind) is None


def test_an_unknown_kind_is_not_reported_as_a_platform_limitation():
    """ "We do not have that" and "this host cannot do that" are different answers.

    Collapsing them would tell a caller who mistyped a kind that their platform is
    at fault.
    """
    assert backup_mod.kind_unavailable_reason("not-a-kind") is None


def test_job_kinds_is_not_filtered_by_availability(monkeypatch):
    """``JOB_KINDS`` stays whole on purpose.

    ``hooks._register_job_runners`` registers every kind in it and
    ``routes._account_jobs`` calls ``sdk.list_active(kind)`` for each, so filtering
    the tuple would unregister ``sessions`` and leave any run record already in the
    store unresolvable. Availability is a separate question from existence.
    """
    monkeypatch.setattr(backup_mod, "_CAN_PIN_TRAVERSAL", False)
    assert backup_mod.JOB_KINDS == (backup_mod.KIND_SNAPSHOT, backup_mod.KIND_SESSIONS)


def test_the_stated_reason_is_the_same_text_the_worker_would_raise(monkeypatch):
    """The pre-check and the fail-close must quote ONE explanation.

    If they drift, the UI ends up promising one thing while the run record says
    another -- and the fail-close is the authority, since it is what actually
    refuses to upload.
    """
    monkeypatch.setattr(backup_mod, "_CAN_PIN_TRAVERSAL", False)
    stated = backup_mod.kind_unavailable_reason(backup_mod.KIND_SESSIONS)
    with pytest.raises(RuntimeError) as exc:
        backup_mod.run_sessions_backup(
            "111122223333", "prof", "us-east-1", "bucket", caller=backup_mod.CALLER_OWNER
        )
    assert str(exc.value) == stated


def test_the_tree_walker_still_refuses_independently(monkeypatch):
    """``_add_tree``'s own guard is defense in depth and must not be removed: it is
    what stops a future caller from reintroducing a name-based walk of these
    agent-writable directories by going around ``run_sessions_backup``.
    """
    monkeypatch.setattr(backup_mod, "_CAN_PIN_TRAVERSAL", False)
    with pytest.raises(RuntimeError):
        backup_mod._add_tree(cast(Any, None), APP_ROOT, "crew")


# ---------------------------------------------------------------------------
# 3. The route says so before a run record exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starting_an_unsupported_kind_is_refused_before_any_job_is_started(monkeypatch):
    """501 and not 400: the request is well-formed and would be honoured on another
    host, so it is this server that does not implement it.

    ``get_job_sdk`` is replaced with a boom so the test also proves the refusal
    happens BEFORE the run is started -- an accepted-then-failed run is exactly the
    outcome this change exists to remove.
    """
    monkeypatch.setattr(backup_mod, "_CAN_PIN_TRAVERSAL", False)

    async def _drive(_request: Any) -> tuple[str, str, str, str]:
        return ("111122223333", "prof", "us-east-1", "bucket")

    async def _body(_request: Any) -> dict[str, Any]:
        return {"kind": backup_mod.KIND_SESSIONS}

    def _boom(_app_name: str) -> Any:  # pragma: no cover - must never be reached
        raise AssertionError("the run must be refused before the job runtime is touched")

    monkeypatch.setattr(routes_mod, "_require_drive", _drive)
    monkeypatch.setattr(routes_mod, "_body", _body)
    monkeypatch.setattr(routes_mod, "get_job_sdk", _boom)

    response = await routes_mod._handle_backup_run(object())  # type: ignore[arg-type]
    assert response.status == 501
    payload = json.loads(response.text or "")
    assert payload["error"] == backup_mod.kind_unavailable_reason(backup_mod.KIND_SESSIONS)


@pytest.mark.asyncio
async def test_a_supported_kind_still_reaches_the_job_runtime(monkeypatch):
    """The guard must be narrow: snapshot has to pass straight through it, or the
    refusal has quietly become an outage.
    """
    monkeypatch.setattr(backup_mod, "_CAN_PIN_TRAVERSAL", False)
    reached: list[str] = []

    async def _drive(_request: Any) -> tuple[str, str, str, str]:
        return ("111122223333", "prof", "us-east-1", "bucket")

    async def _body(_request: Any) -> dict[str, Any]:
        return {"kind": backup_mod.KIND_SNAPSHOT}

    def _sdk(app_name: str) -> Any:
        reached.append(app_name)
        return None  # a 503 "runtime unavailable" is fine; getting here is the point

    monkeypatch.setattr(routes_mod, "_require_drive", _drive)
    monkeypatch.setattr(routes_mod, "_body", _body)
    monkeypatch.setattr(routes_mod, "get_job_sdk", _sdk)

    response = await routes_mod._handle_backup_run(object())  # type: ignore[arg-type]
    assert reached == [backup_mod.APP_NAME]
    assert response.status != 501


@pytest.mark.asyncio
async def test_an_unknown_kind_is_still_a_bad_request(monkeypatch):
    """The new branch sits next to the existing one and must not swallow it."""

    async def _drive(_request: Any) -> tuple[str, str, str, str]:
        return ("111122223333", "prof", "us-east-1", "bucket")

    async def _body(_request: Any) -> dict[str, Any]:
        return {"kind": "not-a-kind"}

    monkeypatch.setattr(routes_mod, "_require_drive", _drive)
    monkeypatch.setattr(routes_mod, "_body", _body)

    response = await routes_mod._handle_backup_run(object())  # type: ignore[arg-type]
    assert response.status == 400
    assert json.loads(response.text or "")["code"] == "invalid_kind"
