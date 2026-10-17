"""State-file readers ride out the Windows sharing-violation window.

On Windows a read of a whole, correct file raises ``PermissionError`` (``WinError 32``)
while another handle holds it open -- the tmp-file-plus-``os.replace`` write of the
store that owns it, or an indexer or scanner. POSIX permits the read, so the fault only
reaches the ``Backend Tests (Windows)`` matrix and Windows hosts. ``atomic_write.
read_bytes_with_retry`` retries that window off the event loop; each reader below has to
read its bytes through it and parse after, instead of calling ``read_text``.

``held_open`` reproduces the fault on any OS: the file is held for its first N reads
whichever API the reader uses (``Path.read_text`` and ``Path.read_bytes`` are both
faulted), and ``atomic_write`` is shown a Windows host so the helper takes its retry
branch. Only ``atomic_write``'s view of the platform changes; the lock and the writes of
the code under test stay on the real POSIX path. The cases prove each site is wired to
the helper and keeps its existing refusal when the hold outlasts the retry budget. The
real OS behaviour is the Windows job's, and none of these cases needs a real Windows host,
so they run on every platform in the matrix.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import pytest
from windows_sim import read_sharing_violation, read_text_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat, webhooks
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.cloud import connect
from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.workflow_memory import WorkflowMemoryError, read_binding
from kiro_crew.workflows.store import WorkflowRunStore

#: Two faulted reads, then the real one: inside the retry budget.
RIDDEN_OUT = 2
#: Far past the budget: the hold outlasts every attempt.
PERSISTENT = 10_000


class _WindowsHost:
    """``platform_compat`` as ``atomic_write`` sees it on Windows: the real module, but ``IS_WINDOWS``.

    Rebinding only ``atomic_write``'s name keeps ``webhooks.locked`` and every other
    module on the real POSIX path (a global flip would send the file lock to ``msvcrt``).
    """

    IS_WINDOWS = True

    def __getattr__(self, name: str) -> Any:
        return getattr(platform_compat, name)


@contextmanager
def held_open(path: Path, times: int) -> Iterator[dict[str, int]]:
    """*path* is held open by another handle for its first *times* reads.

    Yields the ``read_bytes`` counter: ``n`` is every read the retry helper made, the
    faulted ones and the one that finally landed.
    """
    with (
        mock.patch.object(aw, "platform_compat", _WindowsHost()),
        mock.patch.object(aw, "_REPLACE_BACKOFF_SECONDS", 0),
        read_sharing_violation(match=str(path), times=times) as reads,
        read_text_sharing_violation(match=str(path), times=times),
    ):
        yield reads


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# -- apps/manifest.py ---------------------------------------------------------------


def _manifest_file(tmp_path: Path) -> Path:
    return _write_json(
        tmp_path / "app.json",
        {"name": "demo", "version": "1.0.0", "displayName": "Demo", "description": "A demo"},
    )


def test_manifest_read_rides_out_a_sharing_violation(tmp_path):
    path = _manifest_file(tmp_path)

    with held_open(path, RIDDEN_OUT) as reads:
        manifest = AppManifest.from_json_file(path)

    assert manifest.name == "demo"
    assert reads["n"] == RIDDEN_OUT + 1


def test_manifest_read_surfaces_a_hold_that_outlasts_the_budget(tmp_path):
    path = _manifest_file(tmp_path)

    with held_open(path, PERSISTENT) as reads:
        with pytest.raises(PermissionError):
            AppManifest.from_json_file(path)

    assert reads["n"] == aw._REPLACE_MAX_ATTEMPTS


# -- cloud/connect.py ---------------------------------------------------------------


def _launch_job(monkeypatch, tmp_path: Path) -> Path:
    import kiro_crew.cloud.launch_job as ljmod

    store = ljmod.LaunchJobStore(root=tmp_path / "launch-jobs")
    monkeypatch.setattr(ljmod, "LaunchJobStore", lambda *a, **k: store)
    job = store.create(profile="dev", region="us-west-2", size_key="light")
    job.instance_id = "i-0abc1234"
    store.save(job)
    return store.root / f"{job.id}.json"


def test_launched_instance_check_rides_out_a_sharing_violation(monkeypatch, tmp_path):
    job_file = _launch_job(monkeypatch, tmp_path)

    with held_open(job_file, RIDDEN_OUT) as reads:
        assert connect.is_launched_instance("i-0abc1234") is True

    assert reads["n"] == RIDDEN_OUT + 1


def test_launched_instance_check_fails_closed_when_the_hold_outlasts_the_budget(
    monkeypatch, tmp_path
):
    """A job file that cannot be read never reads as "not launched"."""
    job_file = _launch_job(monkeypatch, tmp_path)

    with held_open(job_file, PERSISTENT) as reads:
        with pytest.raises(PermissionError):
            connect.is_launched_instance("i-0abc1234")

    assert reads["n"] == aw._REPLACE_MAX_ATTEMPTS


# -- dashboard_templates/instance.py ------------------------------------------------

SLUG = "read-retry-probe"


def _adopt_two_versions() -> None:
    """Version 1 is the default page, version 2 another page."""
    other = next(
        entry.id
        for entry in catalog.list_templates().entries
        if entry.id != instance.DEFAULT_TEMPLATE_ID
    )
    instance.stage_preview(SLUG, template_id=instance.DEFAULT_TEMPLATE_ID)
    instance.apply_preview(SLUG, session_id="")
    instance.stage_preview(SLUG, template_id=other)
    instance.apply_preview(SLUG, session_id="")


def test_instance_record_read_rides_out_a_sharing_violation():
    _adopt_two_versions()
    record = instance.instance_dir(SLUG) / "instance.json"

    with held_open(record, RIDDEN_OUT) as reads:
        current = instance.read(SLUG)

    assert current.instance_version == 2
    assert current.state != instance.STATE_ERROR
    assert reads["n"] == RIDDEN_OUT + 1


def test_instance_record_read_keeps_its_refusal_when_the_hold_outlasts_the_budget():
    _adopt_two_versions()
    record = instance.instance_dir(SLUG) / "instance.json"

    with held_open(record, PERSISTENT) as reads:
        with pytest.raises(instance.InstanceError, match="cannot be read"):
            instance.read(SLUG)

    assert reads["n"] == aw._REPLACE_MAX_ATTEMPTS


def test_instance_version_read_rides_out_a_sharing_violation():
    _adopt_two_versions()
    kept = instance.instance_dir(SLUG) / "versions" / "1.json"

    with held_open(kept, RIDDEN_OUT) as reads:
        back = instance.rollback(SLUG, 1, session_id="")

    assert back.instance_version == 3
    assert back.template_id == instance.DEFAULT_TEMPLATE_ID
    assert reads["n"] == RIDDEN_OUT + 1


def test_instance_version_read_keeps_its_refusal_when_the_hold_outlasts_the_budget():
    _adopt_two_versions()
    kept = instance.instance_dir(SLUG) / "versions" / "1.json"

    with held_open(kept, PERSISTENT) as reads:
        with pytest.raises(instance.InstanceError, match="cannot be read"):
            instance.rollback(SLUG, 1, session_id="")

    assert reads["n"] == aw._REPLACE_MAX_ATTEMPTS


def test_instance_history_read_rides_out_a_sharing_violation():
    _adopt_two_versions()
    savepoint = instance.instance_dir(SLUG) / "history.json"
    rows_before = instance.history(SLUG)
    assert rows_before

    with held_open(savepoint, RIDDEN_OUT) as reads:
        rows = instance.history(SLUG)

    assert rows == rows_before
    assert reads["n"] == RIDDEN_OUT + 1


def test_staged_preview_read_rides_out_a_sharing_violation():
    instance.stage_preview(SLUG, template_id=instance.DEFAULT_TEMPLATE_ID)
    staged = instance.instance_dir(SLUG) / "preview.json"

    with held_open(staged, RIDDEN_OUT) as reads:
        preview = instance.staged_preview(SLUG)

    assert preview is not None
    assert preview.template_id == instance.DEFAULT_TEMPLATE_ID
    assert reads["n"] == RIDDEN_OUT + 1


# -- webhooks.py --------------------------------------------------------------------


@pytest.fixture
def webhook_store(tmp_path) -> webhooks.WebhookTokenStore:
    store = webhooks.WebhookTokenStore(tmp_path)
    assert store.set_switch(False) is False
    return store


def test_webhook_store_read_rides_out_a_sharing_violation(webhook_store):
    with held_open(webhook_store.path, RIDDEN_OUT) as reads:
        assert webhook_store.is_switch_on() is False

    assert reads["n"] == RIDDEN_OUT + 1


def test_webhook_store_read_stays_unreadable_when_the_hold_outlasts_the_budget(webhook_store):
    """A hold that outlasts the budget is refused, never read as an empty store."""
    with held_open(webhook_store.path, PERSISTENT) as reads:
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            webhook_store.is_switch_on()

    assert reads["n"] == aw._REPLACE_MAX_ATTEMPTS


# -- workflow_memory.py -------------------------------------------------------------

RUN_ID = "wf_000001"


def _run_record() -> Path:
    context = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
    path = WorkflowRunStore()._path_for(RUN_ID)
    return _write_json(
        path, {"session_key": "dashboard:probe", "execution_context": context.to_record()}
    )


def test_workflow_binding_read_rides_out_a_sharing_violation():
    record = _run_record()

    with held_open(record, RIDDEN_OUT) as reads:
        binding = read_binding(RUN_ID, required=True)

    assert binding["run_id"] == RUN_ID
    assert binding["origin"] == "dashboard:probe"
    assert reads["n"] == RIDDEN_OUT + 1


def test_workflow_binding_read_surfaces_a_hold_that_outlasts_the_budget():
    record = _run_record()

    with held_open(record, PERSISTENT) as reads:
        with pytest.raises(PermissionError):
            read_binding(RUN_ID, required=True)

    assert reads["n"] == aw._REPLACE_MAX_ATTEMPTS


# -- controls: an absent file and a damaged file are not retried ---------------------


@pytest.fixture
def attempts(monkeypatch) -> dict[str, int]:
    """Count the reads the helper makes, on a Windows view with no fault injected."""
    counted = {"n": 0}
    real = aw._read_bytes

    def _counting(target: Path, max_bytes: int | None) -> bytes:
        counted["n"] += 1
        return real(target, max_bytes)

    monkeypatch.setattr(aw, "platform_compat", _WindowsHost())
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(aw, "_read_bytes", _counting)
    return counted


def test_an_absent_store_is_still_the_default_and_is_read_once(tmp_path, attempts):
    store = webhooks.WebhookTokenStore(tmp_path)

    assert store.is_switch_on() is True
    assert attempts["n"] == 1


def test_a_damaged_store_is_still_refused_and_is_read_once(webhook_store, attempts):
    webhook_store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(webhooks.WebhookStoreUnreadable):
        webhook_store.is_switch_on()

    assert attempts["n"] == 1


def test_an_absent_workflow_run_is_still_unavailable_and_is_read_once(attempts):
    assert read_binding(RUN_ID) is None
    with pytest.raises(WorkflowMemoryError):
        read_binding(RUN_ID, required=True)

    assert attempts["n"] == 2


def test_an_absent_instance_record_is_still_not_adopted_and_is_read_once(attempts):
    assert instance.read(SLUG).instance_version == 0
    assert attempts["n"] == 1
