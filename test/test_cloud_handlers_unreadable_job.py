"""Cloud routes refuse while a launch job file cannot be read.

The job store skips a job file it cannot read: bytes that are not UTF-8, bad JSON
(a file cut short) or JSON that is not an object. Its status is unknown, so it may
be a launch still running or the record of another lane's crew. Launch create and
stop, start and destroy refuse with ``409 launch_record_unreadable`` instead of
reading it as absent.
"""

from __future__ import annotations

import json

import pytest
from test_cloud_handlers import _body, _posix_host, _req, _state  # noqa: F401

from kiro_crew.dashboard import handlers_cloud as hc

# Contents of a job file that holds no readable job, so its status is unknown: one
# entry per way the store can fail to parse a record that exists.
UNREADABLE_JOB_FILES = {
    "not_utf8": b'\xff\xfe{"status": "running"}',
    "bad_json": b'{"status": "running", "tag": "kc-',
    "not_an_object": b'["running"]',
}


@pytest.mark.asyncio
class TestLaunchAdmission:
    @pytest.mark.parametrize("kind", sorted(UNREADABLE_JOB_FILES))
    async def test_a_launch_is_refused_while_a_job_record_cannot_be_read(self, tmp_path, kind):
        """The list skips a job file it cannot read (not UTF-8, bad JSON such as a
        file cut short, or JSON that is not an object), so its status is unknown
        and it may be a launch still running. A new launch is refused rather than
        risking a second billed stack."""
        state = _state(tmp_path)
        store = state.cloud_launch_store
        unreadable = store.create(profile="dev", region="us-east-1", size_key="balanced")
        store._path(unreadable.id).write_bytes(UNREADABLE_JOB_FILES[kind])

        try:
            resp = await hc.api_cloud_launch_create(
                _req(
                    "POST",
                    "/api/cloud/launch",
                    state=state,
                    body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
                )
            )
        except Exception as exc:  # a refusal is the contract, not a raise
            pytest.fail(f"launch create raised {exc!r} instead of refusing")

        assert resp.status == 409
        body = _body(resp)
        assert body["code"] == "launch_record_unreadable"
        assert f"{unreadable.id}.json" in body["error"]
        assert len(list(store.root.glob("*.json"))) == 1  # no second job was written

    async def test_admission_reads_the_job_store_once(self, tmp_path, monkeypatch):
        """Both admission checks come from one snapshot. Two scans (``list`` then
        ``unreadable_ids``) let a worker's save() land between them, so a running
        job whose file was unreadable for the first could pass both."""
        state = _state(tmp_path)
        store = state.cloud_launch_store
        unreadable = store.create(profile="dev", region="us-east-1", size_key="balanced")
        store._path(unreadable.id).write_bytes(UNREADABLE_JOB_FILES["not_utf8"])
        state.cloud_launch_reaped = True  # the one-time orphan reap is not admission
        for name in ("list", "unreadable_ids"):
            monkeypatch.setattr(
                store, name, lambda *a, _n=name, **k: pytest.fail(f"admission scanned via {_n}")
            )

        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
            )
        )

        assert resp.status == 409
        assert _body(resp)["code"] == "launch_record_unreadable"


@pytest.mark.asyncio
class TestInstanceMutations:
    @pytest.mark.parametrize(
        ("verb", "method", "path"),
        [
            ("stop", "POST", "/api/cloud/kc-2a276d/stop"),
            ("start", "POST", "/api/cloud/kc-2a276d/start"),
            ("destroy", "DELETE", "/api/cloud/kc-2a276d"),
        ],
    )
    @pytest.mark.parametrize("kind", sorted(UNREADABLE_JOB_FILES))
    async def test_a_verb_is_refused_while_a_job_record_cannot_be_read(
        self, tmp_path, monkeypatch, verb, method, path, kind
    ):
        """The lane and destroy's instance id come from the job store, which skips a
        job file it cannot read. That file may be this crew's record, so reading it
        as absent would run the built-in EC2 path against a crew another lane
        created. The verb refuses instead, naming the file."""
        for name in ("stop", "start", "destroy", "describe"):
            monkeypatch.setattr(hc.ec2, name, lambda *a, _n=name, **k: pytest.fail(f"ec2.{_n} ran"))

        class _LaneEngine:
            def teardown(self, *, tag, profile, region):
                pytest.fail("the lane engine tore the crew down")

        state = _state(tmp_path)
        state.cloud_launch_engine = _LaneEngine()
        store = state.cloud_launch_store
        job = store.create(profile="dev", region="us-east-1", size_key="", provider_id="microvm")
        job.tag = "kc-2a276d"
        store.save(job)
        store._path(job.id).write_bytes(UNREADABLE_JOB_FILES[kind])

        handler = {
            "stop": hc.api_cloud_stop,
            "start": hc.api_cloud_start,
            "destroy": hc.api_cloud_destroy,
        }[verb]
        resp = await handler(_req(method, path, state=state, match_info={"tag": "kc-2a276d"}))

        assert resp.status == 409, resp.text
        body = json.loads(resp.text or "{}")
        assert body["code"] == "launch_record_unreadable"
        assert f"{job.id}.json" in body["error"]
