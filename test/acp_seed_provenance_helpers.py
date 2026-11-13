"""Fixtures and helpers shared by the ``settings.local.json`` seed-provenance tests.

Imported by name by ``test_acp_seed_provenance.py`` and its siblings
(``test_cc_permission_mode_seed.py`` goes through it). Every importer takes the autouse
``_pinned_kiro_cli_version`` and ``isolated_records``, so each test starts on the floor
kiro-cli release with empty provenance state.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path

import pytest

from kiro_crew.acp import seed_provenance as sp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

_SERVED = [
    "global.anthropic.claude-opus-5[1m]",
    "global.anthropic.claude-opus-4-8[1m]",
]

# The owner token a bare-sidecar test records under. A client uses its own
# ``_seed_owner``; these tests only need one stable identity.
_OWNER = "owner-under-test"


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(_floor_monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    A client start reaches ``_write_derived_permissions``, which otherwise spawns the
    host's real ``kiro-cli --version``. Pinned to the floor release, as in
    ``test_agent.py``.
    """
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


@pytest.fixture(autouse=True)
def isolated_records(_floor_monkeypatch):
    """Per-test provenance state.

    ``_RECORDS`` and ``_LIVE`` are process-wide runtime state (like
    ``model_registry._ADVERTISED_MODELS``); the sidecar itself already lands in a
    per-test ``KIROCREW_HOME``.
    """
    _floor_monkeypatch.setattr(sp, "_RECORDS", {})
    _floor_monkeypatch.setattr(sp, "_LIVE", {})
    _floor_monkeypatch.setattr(sp, "_SHARERS", {})


def _client(tmp_path: Path, **kw) -> AcpClient:
    return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)


def _fp(payload: str) -> tuple[int, str]:
    """The ``(size, sha256)`` fingerprint of *payload*, as the record stores it."""
    raw = payload.encode("utf-8")
    return len(raw), hashlib.sha256(raw).hexdigest()


_FOREIGN_PID = 99_999_999_999
_FOREIGN_START_ID = "foreign-process-incarnation"
_OWN_START_ID = "test-process-incarnation"


def _pin_holder_identities(monkeypatch, *, foreign_live: bool) -> dict[str, object]:
    """Give holder-liveness tests deterministic local and foreign identities."""
    own_pid = os.getpid()
    real_pid_exists = sp.platform_compat.pid_exists
    real_get_start_id = sp.platform_compat.get_process_start_id

    def _pid_exists(pid: int) -> bool:
        if pid == _FOREIGN_PID:
            return foreign_live
        if pid == own_pid:
            return True
        return real_pid_exists(pid)

    def _get_start_id(pid: int) -> str | None:
        if pid == _FOREIGN_PID:
            return _FOREIGN_START_ID
        if pid == own_pid:
            return _OWN_START_ID
        return real_get_start_id(pid)

    monkeypatch.setattr(sp.platform_compat, "pid_exists", _pid_exists)
    monkeypatch.setattr(sp.platform_compat, "get_process_start_id", _get_start_id)
    return {"pid": _FOREIGN_PID, "start_id": _FOREIGN_START_ID}


def _write_durable_holder(
    path: Path,
    payload: str,
    *,
    kind: str,
    owner: str,
    identity: dict[str, object],
) -> None:
    """Write one durable holder without publishing it into this process's caches."""
    holders: dict[str, dict[str, dict[str, object]]] = {
        sp._HOLDER_OWNERS: {},
        sp._HOLDER_SHARERS: {},
    }
    holders[kind][owner] = dict(identity)
    size, sha256 = _fp(payload)
    sp._sidecar_path().parent.mkdir(parents=True, exist_ok=True)
    sp._sidecar_path().write_text(
        json.dumps(
            {
                "seeds": {
                    os.fspath(path): {
                        "size": size,
                        "sha256": sha256,
                        "holders": holders,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _settings(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.local.json"


def _seed(tmp_path: Path) -> dict:
    return json.loads(_settings(tmp_path).read_text(encoding="utf-8"))


def _teardown(client: AcpClient) -> None:
    """Tear a client down the way every real caller does.

    Await ``_discard_claude_settings_seed`` (the disk revoke and unlink), then
    ``_reset_state`` (in-memory only, so alone it leaves the file on purpose).
    """
    asyncio.run(client._discard_claude_settings_seed())
    client._reset_state()


def _unwritable_sidecar(*_args, **_kwargs):
    """What a read-only data home (or a full disk) does to the sidecar publish.

    Patched over ``seed_provenance.atomic_write`` specifically, which is a DIFFERENT
    binding from the one ``acp.client`` uses for the settings file itself -- so the
    seed still lands and only its durable grant fails, which is the case under test.
    """
    raise OSError("EROFS: read-only file system")


def _break_the_lock_when(monkeypatch, broken) -> None:
    """Make the cross-process registry lock unopenable whenever *broken()* is true.

    As a read-only data home or root-owned ``.settings_seeds.lock`` would: ``os.open``
    raises. Checked per acquisition, so a test can break it only after the write.
    """
    real_lock = sp._cross_process_lock

    @contextlib.contextmanager
    def _lock():
        if broken():
            raise OSError("EACCES: .settings_seeds.lock is not openable")
        with real_lock():
            yield

    monkeypatch.setattr(sp, "_cross_process_lock", _lock)


def _the_owning_process_died() -> None:
    """Simulate a crashed process: leases stale, sidecar digest still durable."""
    if sp._sidecar_path().is_file():
        sidecar = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))
        for entry in sidecar.get("seeds", {}).values():
            for group in entry.get("holders", {}).values():
                for identity in group.values():
                    identity["start_id"] = "stale-process-incarnation"
        sp._sidecar_path().write_text(json.dumps(sidecar), encoding="utf-8")
    sp._RECORDS.clear()
    sp._LIVE.clear()
    sp._SHARERS.clear()
    sp._load()
