"""Script crons drive dashboard sessions with their own credential, and nothing more.

Both tests present exactly what a script cron child holds at run time: the
gateway's internal secret from the 0600 file the runner writes, the job's
``cron:<id>`` session key, and the signed session token the runner publishes
for the run. Neither test holds a dashboard token, because a script cron cannot
mint one: ``POST /api/token/local`` refuses a sandboxed child on purpose.

The first test is the constraint that refusal protects. The internal secret is
not limited to the ``/api/chat`` routes, so the proof that matters is a route it
must NOT reach: the owner-only keystone write behind
``PATCH /api/security/denied-commands/disable-all``. The second test is the
feature: the four ``ScriptContext`` methods against the booted gateway, built
from the same environment contract ``run_script_sandboxed`` hands its child.

The rest pin ``agent.session_control`` on the gateway side. While the switch is
off, the gateway refuses a ``cron:`` key on ``POST /api/chat/slots`` and
``POST /api/chat`` with ``session_control_disabled``. The owner is not refused,
and with the switch on the same cron key is accepted. A slot a cron opens on
either route is labelled cron-created: origin CRON and ``_created_by`` set, so
it is neither counted nor exposed as a person's own tab, while the owner's own
slot stays USER.

The last group pins the creator fence on those two routes. A ``cron:`` key
reaches only a slot that same job created, whether the slot is live or only
persisted. A key naming the owner's slot, another job's slot, or a closed slot
whose transcript names no matching creator is refused with ``not_creator``, and
nothing is minted or queued. The owner still reaches a cron's slot.

The final group pins ``ScriptContext.set_session_mode`` and the cron rules on
``POST /api/chat/mode``: a cron sets ``trust`` or ``trust_reads`` on its own
slot and is refused every other mode, the owner's slot, another job's slot and
the route itself while the switch is off, each refusal audited, while the owner
keeps every mode on every slot.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

pytestmark = pytest.mark.timeout(120)

_JOB_ID = "nightly-dispatcher"


def _register_script_job(gw, job_id: str = _JOB_ID) -> None:
    """Record a script job on the live scheduler, as the operator's crons.json would.

    The internal-auth middleware refuses a ``cron:`` caller whose job record is
    gone (``caller_record_missing``), so the job has to exist for the credential
    to count. Appending to the in-memory list registers the record without
    arming a tick: the job's ``every`` schedule carries no interval, and
    ``cron_service.schedule.is_due`` answers False for that shape, so the
    scheduler never runs ``dispatch.py``.
    """
    from kiro_crew.cron import CronJob

    gw.state.crons._jobs.append(
        CronJob(id=job_id, name=job_id.replace("-", " "), message="", script="dispatch.py:run")
    )


def _switch_session_control_off(home: Path) -> None:
    """Turn ``agent.session_control`` off in the operator's override file.

    Merges into the file the harness wrote, so its unsandboxed consent stays.
    """
    path = home / "config.local.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    doc.setdefault("agent", {})["session_control"] = False
    path.write_text(json.dumps(doc), encoding="utf-8")


@contextmanager
def _script_context(gw, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """A ``ScriptContext`` built from the runner's env contract, in this process.

    The secret sits in a 0600 file named by ``_KIROCREW_SECRET_FILE``, the dial
    port is the one the runner resolved, and the run's signed token names
    ``cron:<job id>``. The token is retracted when the block ends.
    """
    from kiro_crew.cron_script import ScriptContext
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV, mint_stub_session_token
    from kiro_crew.session_token_sig import publish_session_token, retract_session_token

    secret_file = tmp_path / "kirocrew_secret_run"
    secret_file.write_text(gw.app["local_secret"], encoding="utf-8")
    secret_file.chmod(0o600)
    token = mint_stub_session_token()
    publish_session_token(token, f"cron:{_JOB_ID}")
    monkeypatch.setenv("_KIROCREW_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("_KIROCREW_DIAL_PORT", str(gw.port))
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, token)
    try:
        ctx = ScriptContext(job=SimpleNamespace(id=_JOB_ID, message=""))
        # __post_init__ consumed the file and the env, as it does in a run.
        assert not secret_file.exists()
        assert "_KIROCREW_SECRET_FILE" not in os.environ
        yield ctx
    finally:
        retract_session_token(token)


def _denied_commands_bytes(home: Path) -> bytes | None:
    path = home / "denied_commands.json"
    return path.read_bytes() if path.exists() else None


@pytest.mark.asyncio
async def test_a_cron_credential_cannot_disable_the_denied_commands(gateway_boot) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)
        before = _denied_commands_bytes(gw.home)

        resp = await gw.patch(
            "/api/security/denied-commands/disable-all",
            {"value": True},
            auth=False,
            headers=gw.mcp_headers(f"cron:{_JOB_ID}"),
        )

        body = await resp.text()
        assert resp.status == 403, body
        assert _denied_commands_bytes(gw.home) == before
        # The owner's own view agrees: the ceiling is still on.
        snapshot = await gw.get_json("/api/security/denied-commands")
        assert snapshot.get("disable_all") is not True, json.dumps(snapshot)[:500]


@pytest.mark.asyncio
async def test_a_script_context_lists_creates_opens_and_seeds_a_session(
    gateway_boot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)

        with _script_context(gw, tmp_path, monkeypatch) as ctx:
            # The methods block on urllib, so they run off the gateway's loop.
            folders_before = await asyncio.to_thread(ctx.list_session_folders)
            assert isinstance(folders_before, list)

            folder = await asyncio.to_thread(ctx.create_session_folder, "Nightly runs")
            folder_id = str(folder["id"])
            assert folder_id
            folders_after = await asyncio.to_thread(ctx.list_session_folders)
            assert folder_id in {str(f.get("id")) for f in folders_after}
            assert folder_id not in {str(f.get("id")) for f in folders_before}

            slot_key = await asyncio.to_thread(
                ctx.open_session, "Nightly triage", folder_id=folder_id
            )
            assert slot_key
            slot = gw.state._slots[slot_key]
            assert slot.folder_id == folder_id

            receipt = await asyncio.to_thread(
                ctx.send_to_session, slot_key, "Triage tonight's queue."
            )
            assert receipt.get("ok") is True, json.dumps(receipt)[:500]
            assert receipt.get("slot") == slot_key
            # The seed is the session's first user row, recorded by the gateway.
            rows = list(slot.messages)
            assert any(
                r.get("role") == "user" and "Triage tonight's queue." in str(r.get("content"))
                for r in rows
            ), json.dumps(rows)[:800]


async def _assert_switched_off_refusal(resp) -> None:
    body = await resp.text()
    assert resp.status == 403, body
    assert json.loads(body).get("code") == "session_control_disabled", body


@pytest.mark.asyncio
async def test_a_switched_off_gateway_refuses_a_cron_key_opening_a_session(
    gateway_boot, integration_home: Path
) -> None:
    _switch_session_control_off(integration_home)
    async with gateway_boot() as gw:
        _register_script_job(gw)
        slots_before = set(gw.state._slots)

        resp = await gw.post(
            "/api/chat/slots",
            {"name": "x"},
            auth=False,
            headers=gw.mcp_headers(f"cron:{_JOB_ID}"),
        )

        await _assert_switched_off_refusal(resp)
        assert set(gw.state._slots) == slots_before


@pytest.mark.asyncio
async def test_a_switched_off_gateway_refuses_a_cron_key_seeding_a_session(
    gateway_boot, integration_home: Path
) -> None:
    _switch_session_control_off(integration_home)
    seed = "Seed from the switched-off cron."
    async with gateway_boot() as gw:
        _register_script_job(gw)
        key = (await gw.post_json("/api/chat/slots", {"name": "Owner tab"}))["key"]

        resp = await gw.post(
            "/api/chat?ws=1",
            {"slot": key, "message": seed},
            auth=False,
            headers=gw.mcp_headers(f"cron:{_JOB_ID}"),
        )

        await _assert_switched_off_refusal(resp)
        rows = list(gw.state._slots[key].messages)
        assert not any(
            r.get("role") == "user" and seed in str(r.get("content")) for r in rows
        ), json.dumps(rows)[:800]


@pytest.mark.asyncio
async def test_a_switched_off_gateway_still_lets_the_owner_open_a_session(
    gateway_boot, integration_home: Path
) -> None:
    _switch_session_control_off(integration_home)
    async with gateway_boot() as gw:
        made = await gw.post_json("/api/chat/slots", {"name": "Owner tab"})

        assert made["key"] in gw.state._slots


@pytest.mark.asyncio
async def test_a_switched_on_gateway_accepts_a_cron_key_on_both_routes(gateway_boot) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)

        opened = await gw.post(
            "/api/chat/slots",
            {"name": "Nightly triage"},
            auth=False,
            headers=gw.mcp_headers(f"cron:{_JOB_ID}"),
        )
        body = await opened.text()
        assert opened.status == 200, body
        key = json.loads(body)["key"]
        assert key in gw.state._slots

        seeded = await gw.post(
            "/api/chat?ws=1",
            {"slot": key, "message": "Triage tonight's queue."},
            auth=False,
            headers=gw.mcp_headers(f"cron:{_JOB_ID}"),
        )
        body = await seeded.text()
        assert seeded.status == 200, body
        assert json.loads(body).get("ok") is True, body


@pytest.mark.asyncio
async def test_a_switched_off_gateway_refusal_reaches_a_script_context(
    gateway_boot, integration_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_session_control_off(integration_home)
    async with gateway_boot() as gw:
        _register_script_job(gw)
        slots_before = set(gw.state._slots)

        with _script_context(gw, tmp_path, monkeypatch) as ctx:
            with pytest.raises(RuntimeError, match="session_control_disabled"):
                await asyncio.to_thread(ctx.open_session, "Nightly triage")

        assert set(gw.state._slots) == slots_before


@pytest.mark.asyncio
async def test_a_cron_key_opens_a_slot_labelled_cron_created(gateway_boot) -> None:
    from kiro_crew.dashboard.state import SlotOrigin

    async with gateway_boot() as gw:
        _register_script_job(gw)

        opened = await gw.post(
            "/api/chat/slots",
            {"name": "Nightly triage"},
            auth=False,
            headers=gw.mcp_headers(f"cron:{_JOB_ID}"),
        )
        body = await opened.text()
        assert opened.status == 200, body
        key = json.loads(body)["key"]
        slot = gw.state._slots[key]
        assert slot._origin == SlotOrigin.CRON
        assert slot._created_by == f"cron:{_JOB_ID}"

        # The label does not cost the cron its own slot: the seed still lands.
        seeded = await gw.post(
            "/api/chat?ws=1",
            {"slot": key, "message": "Triage tonight's queue."},
            auth=False,
            headers=gw.mcp_headers(f"cron:{_JOB_ID}"),
        )
        body = await seeded.text()
        assert seeded.status == 200, body
        assert json.loads(body).get("ok") is True, body


@pytest.mark.asyncio
async def test_a_cron_key_seeding_a_new_name_opens_a_slot_labelled_cron_created(
    gateway_boot,
) -> None:
    from kiro_crew.dashboard.state import SlotOrigin

    async with gateway_boot() as gw:
        _register_script_job(gw)
        slots_before = set(gw.state._slots)
        assert "nightly-triage-fresh" not in slots_before

        seeded = await gw.post(
            "/api/chat?ws=1",
            {"slot": "nightly-triage-fresh", "message": "Triage tonight's queue."},
            auth=False,
            headers=gw.mcp_headers(f"cron:{_JOB_ID}"),
        )
        body = await seeded.text()
        assert seeded.status == 200, body
        (key,) = set(gw.state._slots) - slots_before
        slot = gw.state._slots[key]
        assert slot._origin == SlotOrigin.CRON
        assert slot._created_by == f"cron:{_JOB_ID}"


@pytest.mark.asyncio
async def test_an_owner_opened_slot_stays_user_origin(gateway_boot) -> None:
    from kiro_crew.dashboard.state import SlotOrigin

    async with gateway_boot() as gw:
        made = await gw.post_json("/api/chat/slots", {"name": "Owner tab"})

        slot = gw.state._slots[made["key"]]
        assert slot._origin == SlotOrigin.USER
        assert slot._created_by == ""


_OTHER_JOB_ID = "weekly-digest"


async def _cron_post(gw, path: str, body: dict, job_id: str = _JOB_ID):
    """POST *body* as the script job *job_id*, with its own credential only."""
    return await gw.post(path, body, auth=False, headers=gw.mcp_headers(f"cron:{job_id}"))


async def _cron_opens(gw, name: str, job_id: str = _JOB_ID) -> str:
    """Open a slot as the script job *job_id* and return its key."""
    resp = await _cron_post(gw, "/api/chat/slots", {"name": name}, job_id)
    body = await resp.text()
    assert resp.status == 200, body
    return json.loads(body)["key"]


async def _assert_not_creator_refusal(resp) -> None:
    body = await resp.text()
    assert resp.status == 403, body
    assert json.loads(body).get("code") == "not_creator", body


async def _close_as_owner(gw, key: str) -> None:
    """Close *key* the way the owner closes a tab."""
    resp = await gw.delete(f"/api/chat/slots/{key}")
    body = await resp.text()
    assert resp.status == 200, body
    assert key not in gw.state._slots


async def _seed_and_settle(gw, key: str, message: str, job_id: str = "") -> None:
    """Seed *key* as the owner, or as *job_id*, and wait for the turn to end.

    A message-less slot leaves no transcript when it closes, so a test that
    needs a closed slot's metadata line seeds the slot first.
    """
    body = {"slot": key, "message": message}
    if job_id:
        resp = await _cron_post(gw, "/api/chat?ws=1", body, job_id)
    else:
        resp = await gw.post("/api/chat?ws=1", body)
    text = await resp.text()
    assert resp.status == 200, text
    slot = gw.state._slots[key]
    for _ in range(300):
        if not slot.running:
            break
        await asyncio.sleep(0.1)
    assert not slot.running


async def _persisted_meta(gw, key: str) -> dict:
    """The metadata line the gateway persisted for slot *key*, read off the loop."""
    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    log = gw.state.conversation_log
    transcript = slot_transcript_key(key)
    assert await asyncio.to_thread(log.has_log, transcript), transcript
    return await asyncio.to_thread(log.get_metadata, transcript)


@pytest.mark.asyncio
async def test_a_cron_key_cannot_seed_the_owners_slot(gateway_boot) -> None:
    seed = "Seed aimed at the owner's tab."
    async with gateway_boot() as gw:
        _register_script_job(gw)
        key = (await gw.post_json("/api/chat/slots", {"name": "Owner tab"}))["key"]
        slot = gw.state._slots[key]
        rows_before = list(slot.messages)
        queue_before = list(slot._queue)

        resp = await _cron_post(gw, "/api/chat?ws=1", {"slot": key, "message": seed})

        await _assert_not_creator_refusal(resp)
        assert gw.state._slots[key] is slot
        assert list(slot.messages) == rows_before
        assert list(slot._queue) == queue_before
        assert slot.task is None and not slot.running


@pytest.mark.asyncio
async def test_a_cron_key_cannot_seed_another_jobs_slot(gateway_boot) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)
        _register_script_job(gw, _OTHER_JOB_ID)
        key = await _cron_opens(gw, "Nightly triage")
        rows_before = list(gw.state._slots[key].messages)

        resp = await _cron_post(
            gw, "/api/chat?ws=1", {"slot": key, "message": "Not my job's tab."}, _OTHER_JOB_ID
        )

        await _assert_not_creator_refusal(resp)
        assert gw.state._slots[key]._created_by == f"cron:{_JOB_ID}"
        assert list(gw.state._slots[key].messages) == rows_before


@pytest.mark.asyncio
async def test_a_cron_key_reaches_its_own_slot_from_an_earlier_run(gateway_boot) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)
        key = await _cron_opens(gw, "Nightly triage")
        await _seed_and_settle(gw, key, "Triage tonight's queue.", _JOB_ID)
        await _close_as_owner(gw, key)
        meta = await _persisted_meta(gw, key)
        assert meta.get("created_by") == f"cron:{_JOB_ID}", meta

        resp = await _cron_post(
            gw, "/api/chat?ws=1", {"slot": key, "message": "Pick up where the last run stopped."}
        )

        body = await resp.text()
        assert resp.status == 200, body
        assert json.loads(body).get("ok") is True, body
        assert gw.state._slots[key]._created_by == f"cron:{_JOB_ID}"


@pytest.mark.asyncio
async def test_the_owner_can_seed_a_cron_opened_slot(gateway_boot) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)
        key = await _cron_opens(gw, "Nightly triage")

        resp = await gw.post("/api/chat?ws=1", {"slot": key, "message": "Owner follow-up."})

        body = await resp.text()
        assert resp.status == 200, body
        assert json.loads(body).get("ok") is True, body


@pytest.mark.asyncio
async def test_a_switched_off_gateway_still_lets_the_owner_seed_a_cron_slot(
    gateway_boot, integration_home: Path
) -> None:
    from kiro_crew.dashboard.state import SlotOrigin

    _switch_session_control_off(integration_home)
    async with gateway_boot() as gw:
        # The switch refuses the cron on both routes, so the cron-created slot is
        # placed the way a run before the switch was turned off left it.
        slot = gw.state.get_or_create_slot("nightly-triage", origin=SlotOrigin.CRON)
        slot._created_by = f"cron:{_JOB_ID}"

        resp = await gw.post("/api/chat?ws=1", {"slot": slot.key, "message": "Owner follow-up."})

        body = await resp.text()
        assert resp.status == 200, body
        assert json.loads(body).get("ok") is True, body


@pytest.mark.asyncio
async def test_a_cron_key_cannot_reopen_the_owners_slot_by_name(gateway_boot) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)
        key = (await gw.post_json("/api/chat/slots", {"name": "Owner tab"}))["key"]

        refused = await _cron_post(gw, "/api/chat/slots", {"name": key})
        await _assert_not_creator_refusal(refused)
        assert gw.state._slots[key]._created_by == ""

        fresh = await _cron_post(gw, "/api/chat/slots", {"name": "Nightly triage"})
        body = await fresh.text()
        assert fresh.status == 200, body
        assert gw.state._slots[json.loads(body)["key"]]._created_by == f"cron:{_JOB_ID}"


@pytest.mark.asyncio
async def test_a_cron_key_cannot_mint_a_closed_owner_slot_by_name(gateway_boot) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)
        key = (await gw.post_json("/api/chat/slots", {"name": "Owner archive"}))["key"]
        await _seed_and_settle(gw, key, "Owner notes.")
        await _close_as_owner(gw, key)
        meta = await _persisted_meta(gw, key)
        assert not meta.get("created_by"), meta

        resp = await _cron_post(gw, "/api/chat/slots", {"name": key})

        await _assert_not_creator_refusal(resp)
        assert key not in gw.state._slots


# ── the approval mode of a cron-opened session ──
#
# ``ScriptContext.set_session_mode`` posts to ``POST /api/chat/mode``. A caller
# that presents a ``cron:`` key sets ``trust`` or ``trust_reads`` on a slot that
# same job created, and nothing else: ``yolo`` and ``normal`` are refused before
# governance or the safety override is consulted, the owner's slot and another
# job's slot are refused by the creator fence, and the switch refuses the route
# as it refuses the other two. The owner keeps every mode on every slot.


async def _cron_sets_mode(gw, key: str, mode: str, job_id: str = _JOB_ID):
    return await _cron_post(gw, "/api/chat/mode", {"slot": key, "mode": mode}, job_id)


async def _owner_sets_mode(gw, key: str, mode: str) -> None:
    resp = await gw.post("/api/chat/mode", {"slot": key, "mode": mode})
    body = await resp.text()
    assert resp.status == 200, body
    assert json.loads(body) == {"ok": True, "mode": mode}, body


def _cron_slot_without_the_routes(gw, key: str, job_id: str = _JOB_ID):
    """A slot the cron owns, minted directly: for a gateway whose switch is off."""
    from kiro_crew.dashboard.state import SlotOrigin

    slot = gw.state.get_or_create_slot(key, origin=SlotOrigin.CRON)
    slot._created_by = f"cron:{job_id}"
    return slot


def _posture(slot) -> tuple[bool, bool]:
    """``(_trust, _trust_reads)``: the per-slot grant the approval flow consults.

    The session approval policy is not asserted here: ``set_approval_policy``
    is a no-op for a slot whose ACP session has not started, which is every slot
    these tests open and never run a turn on.
    """
    return bool(getattr(slot, "_trust", False)), bool(getattr(slot, "_trust_reads", False))


async def _audit_rows(gw, *, operation_prefix: str) -> list[dict]:
    """SEL access rows whose operation starts with *operation_prefix*, newest first."""
    from kiro_crew.sel import sel

    rows = await asyncio.to_thread(sel().recent, 200)
    return [
        r
        for r in rows
        if r.get("event_type") == "api_access"
        and str(r.get("operation", "")).startswith(operation_prefix)
    ]


@pytest.mark.asyncio
async def test_a_script_context_sets_trust_and_trust_reads_on_its_own_session(
    gateway_boot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.safety_override import safety_override

    async with gateway_boot() as gw:
        _register_script_job(gw)

        with _script_context(gw, tmp_path, monkeypatch) as ctx:
            key = await asyncio.to_thread(ctx.open_session, "Nightly triage")
            slot = gw.state._slots[key]
            assert _posture(slot) == (False, False)

            receipt = await asyncio.to_thread(ctx.set_session_mode, key, "trust")
            assert receipt == {"ok": True, "mode": "trust"}, json.dumps(receipt)[:300]
            assert _posture(slot) == (True, False)

            receipt = await asyncio.to_thread(ctx.set_session_mode, key, "trust_reads")
            assert receipt == {"ok": True, "mode": "trust_reads"}, json.dumps(receipt)[:300]
            assert _posture(slot) == (False, True)

        # A slot-scoped grant is per slot: the process-global override stays off.
        assert safety_override().is_active() is False
        # Both calls are on the record, under the job's own key, naming the slot.
        rows = await _audit_rows(gw, operation_prefix="mode_change:")
        ops = [(r["operation"], r["caller_identity"], r["resources"]) for r in rows]
        assert ("mode_change:trust", f"cron:{_JOB_ID}", key) in ops, ops
        assert ("mode_change:trust_reads", f"cron:{_JOB_ID}", key) in ops, ops


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["yolo", "normal"])
async def test_a_script_context_is_refused_every_other_mode(
    gateway_boot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from kiro_crew.safety_override import safety_override

    async with gateway_boot() as gw:
        _register_script_job(gw)

        with _script_context(gw, tmp_path, monkeypatch) as ctx:
            key = await asyncio.to_thread(ctx.open_session, "Nightly triage")
            await asyncio.to_thread(ctx.set_session_mode, key, "trust")
            slot = gw.state._slots[key]
            assert _posture(slot) == (True, False)

            with pytest.raises(RuntimeError, match="mode_not_allowed"):
                await asyncio.to_thread(ctx.set_session_mode, key, mode)

        # Refused before anything moved: the grant it held, the policy, the override.
        assert _posture(slot) == (True, False)
        assert safety_override().is_active() is False
        rows = await _audit_rows(gw, operation_prefix="chat.control")
        denials = [r for r in rows if r.get("error") == "mode_not_allowed"]
        assert denials, [(r["operation"], r["error"]) for r in rows]
        assert f"slot={key}" in denials[0]["resources"], denials[0]
        assert f"mode={mode!r}" in denials[0]["resources"], denials[0]


@pytest.mark.asyncio
async def test_a_cron_key_cannot_set_the_mode_of_the_owners_slot(gateway_boot) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)
        key = (await gw.post_json("/api/chat/slots", {"name": "Owner tab"}))["key"]

        resp = await _cron_sets_mode(gw, key, "trust")

        await _assert_not_creator_refusal(resp)
        assert _posture(gw.state._slots[key]) == (False, False)


@pytest.mark.asyncio
async def test_a_cron_key_cannot_set_the_mode_of_another_jobs_slot(gateway_boot) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)
        _register_script_job(gw, _OTHER_JOB_ID)
        theirs = await _cron_opens(gw, "Weekly digest", _OTHER_JOB_ID)

        resp = await _cron_sets_mode(gw, theirs, "trust_reads")

        await _assert_not_creator_refusal(resp)
        assert _posture(gw.state._slots[theirs]) == (False, False)


@pytest.mark.asyncio
async def test_a_cron_key_must_name_the_slot(gateway_boot) -> None:
    """The owner's all-slots request is not a cron's to make."""
    async with gateway_boot() as gw:
        _register_script_job(gw)
        mine = await _cron_opens(gw, "Nightly triage")
        owner = (await gw.post_json("/api/chat/slots", {"name": "Owner tab"}))["key"]

        resp = await _cron_post(gw, "/api/chat/mode", {"mode": "trust"})

        body = await resp.text()
        assert resp.status == 400, body
        assert json.loads(body).get("code") == "slot_required", body
        assert _posture(gw.state._slots[mine]) == (False, False)
        assert _posture(gw.state._slots[owner]) == (False, False)


@pytest.mark.asyncio
async def test_a_switched_off_gateway_refuses_a_cron_key_setting_a_mode(
    gateway_boot, integration_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_session_control_off(integration_home)
    async with gateway_boot() as gw:
        _register_script_job(gw)
        slot = _cron_slot_without_the_routes(gw, "nightly-triage")

        resp = await _cron_sets_mode(gw, slot.key, "trust")
        await _assert_switched_off_refusal(resp)
        assert _posture(slot) == (False, False)

        with _script_context(gw, tmp_path, monkeypatch) as ctx:
            with pytest.raises(RuntimeError, match="session_control_disabled"):
                await asyncio.to_thread(ctx.set_session_mode, slot.key, "trust")
        assert _posture(slot) == (False, False)

        # The switch binds the cron, not the owner, who still sets the cron's slot.
        await _owner_sets_mode(gw, slot.key, "trust")
        assert _posture(slot) == (True, False)


@pytest.mark.asyncio
async def test_the_owner_still_sets_and_clears_the_mode_of_a_cron_opened_slot(
    gateway_boot,
) -> None:
    async with gateway_boot() as gw:
        _register_script_job(gw)
        key = await _cron_opens(gw, "Nightly triage")
        slot = gw.state._slots[key]

        await _owner_sets_mode(gw, key, "trust")
        assert _posture(slot) == (True, False)

        await _owner_sets_mode(gw, key, "trust_reads")
        assert _posture(slot) == (False, True)

        await _owner_sets_mode(gw, key, "normal")
        assert _posture(slot) == (False, False)
