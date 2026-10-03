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
and with the switch on the same cron key is accepted.
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


def _register_script_job(gw) -> None:
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
        CronJob(id=_JOB_ID, name="nightly dispatcher", message="", script="dispatch.py:run")
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
