"""When the keystone content check is on: the host probe, its cache, and who may run it.

``loosening_provenance.enforcement_active`` is asked on every config read, on any thread.
It must be ON inside a sandbox, ON until the host probe has answered, OFF on a Linux host
without user namespaces once it has, and it must never probe on an event loop or once per
read. The gateway primes the answer at start (``service_boot.run``); other processes
(the CLI, pod tooling) probe once off-loop so they agree with the gateway.

The conftest turns the gate off and pre-sets the cached answer for every test; each test
here puts the real gate back and clears the cache itself.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sys
import textwrap

import pytest

import kiro_crew.sandbox as sb
from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.paths import config_dir

#: The real gate, captured at import: the conftest swaps it for ``lambda: False``.
_REAL_ENFORCEMENT_ACTIVE = lp.enforcement_active

_ENDORSED = (json.dumps({"agent": {"bot_name": "Kiro"}}) + "\n").encode()
_HAND_EDIT = (json.dumps({"agent": {"bot_name": "Hand"}}) + "\n").encode()


class _Probe:
    """A stand-in for ``sandbox_probe._probe_unshare_once`` that counts its calls."""

    def __init__(self, ok: bool = True, transient: bool = False, boom: bool = False) -> None:
        self.calls = 0
        self._result = (ok, transient, "stub", "")
        self._boom = boom

    def __call__(self) -> tuple[bool, bool, str, str]:
        self.calls += 1
        if self._boom:
            raise AssertionError("the namespace probe ran")
        return self._result


@pytest.fixture
def real_gate(monkeypatch):
    """The real gate on a Linux host outside any sandbox, with nothing cached yet."""
    monkeypatch.setattr(lp, "enforcement_active", _REAL_ENFORCEMENT_ACTIVE)
    monkeypatch.setattr(lp, "_host_verdict", None)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)

    def _install(probe: _Probe) -> _Probe:
        # Through the facade, which forwards the patch to every holder of the name.
        monkeypatch.setattr(sb, "_probe_unshare_once", probe)
        return probe

    return _install


def _seal_and_hand_edit() -> None:
    lp.record(lp.LEAF_CONFIG, _ENDORSED, strict=True)
    (config_dir() / lp.LEAF_CONFIG).write_bytes(_HAND_EDIT)


def _read_paths() -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.loader_document import PendingEditBlocksSave

    lp.pending_changes()
    lp.reconciled_bytes(lp.LEAF_CONFIG, config_dir() / lp.LEAF_CONFIG)
    KiroCrewConfig.load()
    lp.leaf_pending(lp.LEAF_CONFIG)
    try:
        lp.refuse_save_if_pending(lp.LEAF_CONFIG)
    except PendingEditBlocksSave:
        pass


@pytest.mark.asyncio
async def test_a_cold_cache_on_the_event_loop_is_on_and_does_not_probe(real_gate) -> None:
    probe = real_gate(_Probe(boom=True))
    assert lp.enforcement_active() is True
    assert lp.enforcement_active() is True
    assert probe.calls == 0
    assert lp._host_verdict is None  # not cached: the boot probe still decides


def test_inside_a_sandbox_it_is_on_even_over_a_cached_off(real_gate, monkeypatch) -> None:
    probe = real_gate(_Probe(boom=True))
    monkeypatch.setattr(lp, "_host_verdict", False)
    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    assert lp.enforcement_active() is True
    assert probe.calls == 0


def test_the_gateway_primes_the_probe_off_loop() -> None:
    from kiro_crew.slack.gateway_runtime import service_boot

    src = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(service_boot.run))))
    # Off-loop, and readiness never waits on it: a tracked background task.
    assert "await asyncio.to_thread(loosening_provenance.prime_host_verdict)" in src
    assert "asyncio.create_task(_prime_keystone_probe())" in src
    assert "self._background_tasks.add(_keystone_probe_task)" in src


@pytest.mark.asyncio
async def test_after_priming_on_a_host_without_userns_a_hand_edit_is_served_as_written(
    real_gate,
) -> None:
    probe = real_gate(_Probe(ok=False, transient=False))
    _seal_and_hand_edit()
    await asyncio.to_thread(lp.prime_host_verdict)  # what service_boot.run does
    assert lp.enforcement_active() is False
    path = config_dir() / lp.LEAF_CONFIG
    assert lp.reconciled_bytes(lp.LEAF_CONFIG, path) == _HAND_EDIT
    assert lp.pending_changes() == []
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_a_load_before_an_off_answer_does_not_outlive_it(real_gate) -> None:
    """A load on the loop before the probe answers reads the copy; OFF drops that cache."""
    from kiro_crew.config.loader import KiroCrewConfig

    real_gate(_Probe(ok=False, transient=False))
    _seal_and_hand_edit()
    assert KiroCrewConfig.load().agent.bot_name == "Kiro"  # ON until answered
    await asyncio.to_thread(lp.prime_host_verdict)
    assert KiroCrewConfig.load().agent.bot_name == "Hand"


@pytest.mark.asyncio
async def test_no_read_path_probes_once_the_answer_is_cached(real_gate) -> None:
    probe = real_gate(_Probe(ok=False))
    await asyncio.to_thread(lp.prime_host_verdict)
    _seal_and_hand_edit()
    probe._boom = True
    await asyncio.to_thread(_read_paths)  # off-loop, like the dashboard's reads
    _read_paths()  # and on the loop
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_no_read_path_probes_on_the_loop_with_a_cold_cache(real_gate) -> None:
    probe = real_gate(_Probe(boom=True))
    _seal_and_hand_edit()
    _read_paths()
    assert probe.calls == 0
    assert lp._host_verdict is None


def test_off_loop_a_cold_cache_probes_once_per_process(real_gate) -> None:
    probe = real_gate(_Probe(ok=False, transient=False))
    assert lp.enforcement_active() is False
    assert lp.enforcement_active() is False
    assert probe.calls == 1


@pytest.mark.parametrize(
    "probe",
    [_Probe(ok=False, transient=True), _Probe(boom=True)],
    ids=["transient", "raises"],
)
def test_a_probe_that_proves_nothing_counts_as_on(real_gate, probe: _Probe) -> None:
    real_gate(probe)
    assert lp.enforcement_active() is True
    assert lp.enforcement_active() is True
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_a_save_after_a_direct_edit_succeeds_on_a_host_without_userns(real_gate) -> None:
    """The offline E2E in miniature: its runner has no user namespaces and its harness
    edits ``config.json`` directly; every later ordinary save must still go through."""
    from kiro_crew.config import loader

    real_gate(_Probe(ok=False, transient=False))
    await asyncio.to_thread(lp.prime_host_verdict)
    path = config_dir() / lp.LEAF_CONFIG
    loader.update_config_locked(path, mutate=lambda doc: doc, stamp_meta=False)
    path.write_bytes(_HAND_EDIT)

    def _rename(doc: dict) -> dict:
        doc.setdefault("agent", {})["bot_name"] = "Saved"
        return doc

    await asyncio.to_thread(loader.update_config_locked, path, mutate=_rename, stamp_meta=False)
    assert json.loads(path.read_bytes())["agent"]["bot_name"] == "Saved"
