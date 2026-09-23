"""The Fargate lane's single-task read and the route that shows it.

``GET /api/cloud/launch/{id}/task`` is how the cloud panel answers "is my
crew's container task still up" for a launch the Instances registry cannot
speak for (the Fargate lane registers nothing). No AWS: the engine's read is
driven through a fake ``aws.checked_json`` and the route through a fake engine,
so each assertion is about the mapping or the refusal, never about a network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.cloud import aws
from kiro_crew.cloud import fargate_engine as fe
from kiro_crew.cloud import launch_job as lj
from kiro_crew.dashboard import handlers_cloud as hc

ARN = "arn:aws:ecs:us-east-1:123456789012:task/kc-crew/1abc2def3abc4def5abc6def7abc8def"
SHORT_ARN = "arn:aws:ecs:us-east-1:123456789012:task/1abc2def3abc4def5abc6def7abc8def"


# ── engine ─────────────────────────────────────────────────────────────


class TestSplitTaskArn:
    def test_long_form_names_cluster_and_id(self):
        assert fe.split_task_arn(ARN) == ("kc-crew", "1abc2def3abc4def5abc6def7abc8def")

    def test_cluster_with_underscore_survives(self):
        # rpartition on the LAST slash, so a cluster name holding any other
        # character (an underscore is legal) is returned whole.
        arn = "arn:aws:ecs:us-east-1:123456789012:task/team_a-crews/deadbeef"
        assert fe.split_task_arn(arn) == ("team_a-crews", "deadbeef")

    def test_short_form_has_no_cluster(self):
        assert fe.split_task_arn(SHORT_ARN) == ("", "1abc2def3abc4def5abc6def7abc8def")

    @pytest.mark.parametrize("junk", ["", "i-0abc123456789def0", "arn:aws:ecs:us-east-1:1:task/"])
    def test_not_a_task_arn_gives_nothing(self, junk):
        assert fe.split_task_arn(junk) == ("", "")


class TestSightingFromTask:
    def test_maps_every_lifecycle_field(self):
        task = {
            "taskArn": ARN,
            "lastStatus": "STOPPED",
            "desiredStatus": "STOPPED",
            "startedBy": "kirocrew-cloud-kc-abc123",
            "createdAt": "2026-09-21T10:00:00.000000+00:00",
            "startedAt": "2026-09-21T10:01:00.000000+00:00",
            "stoppedAt": "2026-09-21T12:00:00.000000+00:00",
            "stoppedReason": "Essential container in task exited",
            "tags": [{"key": "kirocrew:managed", "value": "true"}],
        }
        s = fe.sighting_from_task(task)
        assert s.task_arn == ARN
        assert s.last_status == "STOPPED"
        assert s.desired_status == "STOPPED"
        assert s.started_by == "kirocrew-cloud-kc-abc123"
        assert s.tags == {"kirocrew:managed": "true"}
        # startedAt wins over createdAt for the age; stoppedAt is its own moment.
        assert s.started_at is not None and s.stopped_at is not None
        assert s.stopped_at - s.started_at == pytest.approx(119 * 60)
        assert s.stopped_reason == "Essential container in task exited"
        assert s.is_running is False

    def test_running_task_has_no_stop_fields(self):
        s = fe.sighting_from_task({"taskArn": ARN, "lastStatus": "RUNNING"})
        assert s.is_running is True
        assert s.stopped_at is None
        assert s.stopped_reason == ""
        assert s.desired_status == ""

    def test_walker_and_single_read_share_the_mapping(self, monkeypatch):
        """Both reads decode a task through ``sighting_from_task``: mutate it
        and BOTH change, which is the property the shared mapping exists for."""
        seen: list[str] = []

        def fake_map(task):
            seen.append(str(task.get("taskArn")))
            return fe.TaskSighting(task_arn="mapped", tags={})

        monkeypatch.setattr(fe, "sighting_from_task", fake_map)
        engine = fe.FargateLaunchEngine(spec=None)
        described = {"tasks": [{"taskArn": ARN}], "failures": []}
        monkeypatch.setattr(aws, "checked_json", lambda args, profile, region, action="": described)
        got = engine.describe_task(task_arn=ARN, profile="p", region="us-east-1")
        # The match is on the raw entry's ARN; the RETURNED value is whatever the
        # shared mapping made of it, so a swapped mapping shows through here.
        assert got is not None and got.task_arn == "mapped"
        assert seen == [ARN]


class TestDescribeTask:
    def _engine(self):
        return fe.FargateLaunchEngine(spec=None)

    def test_reads_exactly_the_arn_in_the_arns_own_cluster(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(args, profile, region, action=""):
            calls.append(list(args))
            return {"tasks": [{"taskArn": ARN, "lastStatus": "RUNNING"}], "failures": []}

        monkeypatch.setattr(aws, "checked_json", fake)
        got = self._engine().describe_task(task_arn=ARN, profile="dev", region="us-east-1")
        assert got is not None and got.last_status == "RUNNING"
        [args] = calls
        assert args[:2] == ["ecs", "describe-tasks"]
        # The cluster comes from the ARN, not from a spec (this engine has none).
        assert args[args.index("--cluster") + 1] == "kc-crew"
        assert args[args.index("--tasks") + 1] == ARN
        assert "TAGS" in args

    def test_missing_task_is_none_not_a_state(self, monkeypatch):
        monkeypatch.setattr(
            aws,
            "checked_json",
            lambda *a, **k: {"tasks": [], "failures": [{"arn": ARN, "reason": "MISSING"}]},
        )
        assert self._engine().describe_task(task_arn=ARN, profile="p", region="r") is None

    def test_another_tasks_entry_is_not_taken_for_this_one(self, monkeypatch):
        """A response carrying a task that is not the ARN asked for is not this
        launch's task: returning it would show one crew another crew's status."""
        other = ARN.replace("1abc2def", "ffffffff")
        monkeypatch.setattr(
            aws,
            "checked_json",
            lambda *a, **k: {
                "tasks": [{"taskArn": other, "lastStatus": "RUNNING"}],
                "failures": [],
            },
        )
        assert self._engine().describe_task(task_arn=ARN, profile="p", region="r") is None

    def test_short_arn_without_spec_refuses_rather_than_guessing(self, monkeypatch):
        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: pytest.fail("must not call AWS"))
        with pytest.raises(ValueError, match="names no cluster"):
            self._engine().describe_task(task_arn=SHORT_ARN, profile="p", region="r")

    def test_aws_failure_propagates_as_awserror(self, monkeypatch):
        def boom(*a, **k):
            raise aws.AWSError("ecs:DescribeTasks failed")

        monkeypatch.setattr(aws, "checked_json", boom)
        with pytest.raises(aws.AWSError):
            self._engine().describe_task(task_arn=ARN, profile="p", region="r")


# ── route ──────────────────────────────────────────────────────────────


class NoReadEngine:
    """The EC2 shape: launches, but has no single-task read."""

    def teardown(self, *, tag, profile, region):
        return True


class ReadingEngine(NoReadEngine):
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def describe_task(self, *, task_arn, profile, region):
        self.calls.append({"task_arn": task_arn, "profile": profile, "region": region})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _state(tmp_path, engine):
    return SimpleNamespace(
        owner_id="owner-1",
        cloud_launch_sync=True,
        cloud_launch_engine=engine,
        cloud_launch_store=lj.LaunchJobStore(root=tmp_path / "launch-jobs"),
    )


def _job(state, *, instance_id=ARN, provider_id="aws_fargate"):
    job = state.cloud_launch_store.create(
        profile="dev", region="us-east-1", size_key="balanced", provider_id=provider_id
    )
    job.status = lj.DONE
    job.instance_id = instance_id
    state.cloud_launch_store.save(job)
    return job


def _req(path, *, state, job_id, user=True, slack=False):
    app = web.Application()
    app["state"] = state
    headers = {"X-Session-Key": "slack:x"} if slack else {}
    req = make_mocked_request("GET", path, headers=headers, app=app, match_info={"id": job_id})
    if user:
        req["user"] = "owner-1"
        req["app"] = ""
    return req


def _body(resp):
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _posix_host(monkeypatch):
    monkeypatch.setattr(hc.sys, "platform", "linux")


@pytest.mark.asyncio
class TestLaunchTaskRoute:
    async def test_slack_origin_rejected(self, tmp_path):
        state = _state(tmp_path, ReadingEngine(None))
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id="j", slack=True))
        assert resp.status == 403

    async def test_windows_rejected_it_shells_to_aws(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hc.sys, "platform", "win32")
        state = _state(tmp_path, ReadingEngine(None))
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id="j"))
        assert resp.status == 400
        assert _body(resp)["code"] == "posix_host_required"

    async def test_unknown_job_is_404(self, tmp_path):
        state = _state(tmp_path, ReadingEngine(None))
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id="nope"))
        assert resp.status == 404
        assert _body(resp)["code"] == "launch_job_not_found"

    async def test_launch_without_a_task_is_refused_before_any_read(self, tmp_path):
        engine = ReadingEngine(None)
        state = _state(tmp_path, engine)
        job = _job(state, instance_id="")
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id=job.id))
        assert resp.status == 400
        assert _body(resp)["code"] == "launch_task_not_recorded"
        assert engine.calls == []

    async def test_engine_without_the_read_is_named_not_500(self, tmp_path):
        state = _state(tmp_path, NoReadEngine())
        job = _job(state, provider_id="aws_ec2")
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id=job.id))
        assert resp.status == 400
        assert _body(resp)["code"] == "provisioner_cannot_describe"

    async def test_unconfigured_lane_is_unknown_provisioner(self, tmp_path, monkeypatch):
        state = _state(tmp_path, None)
        job = _job(state)

        def no_engine(state_, provider_id="aws_ec2", confirmed_recipient=""):
            raise KeyError(provider_id)

        monkeypatch.setattr(hc, "_engine", no_engine)
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id=job.id))
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_provisioner"

    async def test_engine_is_resolved_off_the_event_loop(self, tmp_path, monkeypatch):
        """Resolving the Fargate lane reads cloud.json; on the loop thread that
        read would stall every request behind one panel open."""
        import threading

        seen: list[bool] = []

        def resolver(state_, provider_id="aws_ec2", confirmed_recipient=""):
            seen.append(threading.current_thread() is threading.main_thread())
            return ReadingEngine(None)

        state = _state(tmp_path, None)
        job = _job(state)
        monkeypatch.setattr(hc, "_engine", resolver)
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id=job.id))
        assert resp.status == 200
        assert seen == [False], "the engine must be resolved on a worker thread, not the loop"

    async def test_aws_failure_is_502_with_the_message(self, tmp_path):
        state = _state(tmp_path, ReadingEngine(aws.AWSError("throttled")))
        job = _job(state)
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id=job.id))
        assert resp.status == 502
        body = _body(resp)
        assert body["code"] == "aws_call_failed"
        assert "throttled" in body["error"]

    async def test_success_carries_the_sighting_split_and_a_read_instant(self, tmp_path):
        sighting = fe.TaskSighting(
            task_arn=ARN,
            tags={"kirocrew:managed": "true"},
            started_by="kirocrew-cloud-kc-abc123",
            last_status="RUNNING",
            started_at=1_700_000_000.0,
            desired_status="RUNNING",
        )
        engine = ReadingEngine(sighting)
        state = _state(tmp_path, engine)
        job = _job(state)
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id=job.id))
        assert resp.status == 200
        body = _body(resp)
        assert body["job_id"] == job.id
        assert body["task_arn"] == ARN
        assert isinstance(body["read_at"], float) and body["read_at"] > 1_700_000_000
        task = body["task"]
        assert task["cluster"] == "kc-crew"
        assert task["task_id"] == "1abc2def3abc4def5abc6def7abc8def"
        assert task["last_status"] == "RUNNING"
        assert task["desired_status"] == "RUNNING"
        assert task["started_at"] == 1_700_000_000.0
        assert task["stopped_at"] is None
        assert task["stopped_reason"] == ""
        # Ownership inputs stay server-side: the panel has no reader for them.
        assert "tags" not in task and "started_by" not in task
        # The read is driven with the JOB's coordinates, never the request's.
        assert engine.calls == [{"task_arn": ARN, "profile": "dev", "region": "us-east-1"}]

    async def test_missing_task_is_null_not_a_status(self, tmp_path):
        state = _state(tmp_path, ReadingEngine(None))
        job = _job(state)
        resp = await hc.api_cloud_launch_task(_req("/x", state=state, job_id=job.id))
        assert resp.status == 200
        body = _body(resp)
        assert body["task"] is None
        assert body["task_arn"] == ARN
