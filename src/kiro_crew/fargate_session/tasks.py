"""ECS calls for a Fargate session task, through the ``cloud.aws`` chokepoint.

Every call goes through :func:`kiro_crew.cloud.aws.checked_json`, which runs the
``aws`` CLI with the profile's own named credentials (never a raw key) under the
OS sandbox, and refuses mutations from an agent subprocess. These run in the
gateway process on a session's behalf, never as agent tools.

Placement and ARN parsing come from the whole-Crew Fargate lane
(:func:`kiro_crew.cloud.fargate.runtask.network_configuration`,
:func:`kiro_crew.cloud.fargate_engine.split_task_arn`), so both lanes place a
task the same way. The request itself is this module's: a session task runs the
owner's own task definition with the bootstrap as its command, not that lane's
crew container, and it carries none of that lane's ``kirocrew:managed`` tags,
so neither lane's teardown can claim the other's tasks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

from kiro_crew.session_environment import SessionEnvironmentUnavailable

#: Tag on every session task, so the gateway only ever stops what it started.
SESSION_TAG = "kirocrew:session"
#: ``startedBy`` on every session task; the Crew lane uses ``kirocrew-cloud-<tag>``.
STARTED_BY = "kirocrew-session"
_WAIT_SECS = 300.0
_POLL_SECS = 3.0
_ERROR_CHARS = 400


@dataclass(frozen=True)
class FargateTask:
    arn: str
    task_id: str
    cluster: str
    runtime_id: str = ""

    @property
    def ssm_target(self) -> str:
        """The SSM target for a port forward into this task's container."""
        return f"ecs:{self.cluster}_{self.task_id}_{self.runtime_id}"


def session_tag(session_key: str) -> str:
    """A tag value that names the session without putting its key in AWS."""
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]


async def _aws(args: list[str], profile: Mapping[str, Any], action: str) -> Any:
    from kiro_crew.cloud import aws

    def call() -> Any:
        return aws.checked_json(
            args,
            str(profile.get("aws_profile") or ""),
            str(profile.get("region") or ""),
            action=action,
        )

    try:
        return await asyncio.to_thread(call)
    except aws.AWSError as exc:
        raise SessionEnvironmentUnavailable(f"AWS refused {action}: {exc}"[:500]) from exc


async def task_container(profile: Mapping[str, Any]) -> str:
    """The task definition's essential container, whose command the bootstrap replaces.

    Refused when it sets ``entryPoint``: an override can replace only the
    command, so an entry point would receive the bootstrap as its arguments.
    """
    data = await _aws(
        ["ecs", "describe-task-definition", "--task-definition", str(profile["task_definition"])],
        profile,
        "ecs:DescribeTaskDefinition",
    )
    containers = ((data or {}).get("taskDefinition") or {}).get("containerDefinitions") or []
    essential = [c for c in containers if c.get("essential", True)]
    if not essential:
        raise SessionEnvironmentUnavailable("the task definition has no essential container")
    container = essential[0]
    if container.get("entryPoint"):
        raise SessionEnvironmentUnavailable(
            "the task definition's container sets an entryPoint; session tasks need "
            "an image whose command can be replaced (no entryPoint)"
        )
    return str(container["name"])


def run_task_request(
    profile: Mapping[str, Any],
    session_key: str,
    *,
    container: str,
    command: list[str],
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """The ``RunTask`` request body for one session task. No secret is in it."""
    from kiro_crew.cloud.fargate.runtask import Placement, network_configuration

    placement = Placement(
        cluster=str(profile["cluster"]),
        subnets=tuple(profile["subnets"]),
        security_groups=tuple(profile["security_groups"]),
        assign_public_ip=bool(profile.get("assign_public_ip")),
    )
    return {
        "cluster": placement.cluster,
        "taskDefinition": str(profile["task_definition"]),
        "launchType": "FARGATE",
        "count": 1,
        # Starts the SSM agent the gateway's port forward reaches.
        "enableExecuteCommand": True,
        "startedBy": STARTED_BY,
        "networkConfiguration": network_configuration(placement),
        "overrides": {
            "containerOverrides": [
                {
                    "name": container,
                    "command": list(command),
                    "environment": [
                        {"name": name, "value": value}
                        for name, value in sorted(environment.items())
                    ],
                }
            ]
        },
        "tags": [{"key": SESSION_TAG, "value": session_tag(session_key)}],
    }


async def run_task(
    profile: Mapping[str, Any],
    session_key: str,
    *,
    container: str,
    command: list[str],
    environment: Mapping[str, str],
) -> FargateTask:
    from kiro_crew.cloud.fargate_engine import split_task_arn

    request = run_task_request(
        profile, session_key, container=container, command=command, environment=environment
    )
    data = await _aws(
        ["ecs", "run-task", "--cli-input-json", json.dumps(request)], profile, "ecs:RunTask"
    )
    started = (data or {}).get("tasks") or []
    if not started:
        failures = (data or {}).get("failures") or []
        raise SessionEnvironmentUnavailable(f"ECS started no task: {failures}"[:500])
    arn = str(started[0].get("taskArn") or "")
    cluster, task_id = split_task_arn(arn)
    if not task_id or not task_id.isalnum():
        raise SessionEnvironmentUnavailable("ECS returned an unexpected task ARN")
    return FargateTask(arn, task_id, cluster or str(profile["cluster"]))


async def describe(profile: Mapping[str, Any], task: FargateTask) -> Optional[dict]:
    """The task's ``DescribeTasks`` record with its tags, or None when ECS has none."""
    data = await _aws(
        [
            "ecs",
            "describe-tasks",
            "--cluster",
            task.cluster,
            "--tasks",
            task.arn,
            "--include",
            "TAGS",
        ],
        profile,
        "ecs:DescribeTasks",
    )
    described = (data or {}).get("tasks") or []
    failures = (data or {}).get("failures") or []
    if failures and not all(
        failure.get("reason") == "MISSING" and failure.get("arn") in (task.arn, task.task_id)
        for failure in failures
    ):
        raise SessionEnvironmentUnavailable("ECS could not describe the session task")
    return described[0] if described else None


def carries_session_tag(info: Mapping[str, Any], session_key: str) -> bool:
    """Whether a described task is tagged as ``session_key``'s session task."""
    tags = {t.get("key"): t.get("value") for t in info.get("tags") or []}
    return tags.get(SESSION_TAG) == session_tag(session_key)


async def wait_running(
    profile: Mapping[str, Any],
    task: FargateTask,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> FargateTask:
    """Poll until the task runs and its exec agent is up; the runtime id SSM needs."""
    waited = 0.0
    while waited < _WAIT_SECS:
        info = await describe(profile, task)
        if info is None:
            raise SessionEnvironmentUnavailable("the session task disappeared while starting")
        status = info.get("lastStatus")
        if status == "STOPPED":
            reason = info.get("stoppedReason") or "unknown"
            raise SessionEnvironmentUnavailable(
                f"the session task stopped while starting: {reason}"[:_ERROR_CHARS]
            )
        if status == "RUNNING":
            for container in info.get("containers") or []:
                agents = container.get("managedAgents") or []
                running = any(
                    a.get("name") == "ExecuteCommandAgent" and a.get("lastStatus") == "RUNNING"
                    for a in agents
                )
                if running and container.get("runtimeId"):
                    return FargateTask(
                        task.arn, task.task_id, task.cluster, str(container["runtimeId"])
                    )
        await sleep(_POLL_SECS)
        waited += _POLL_SECS
    raise SessionEnvironmentUnavailable("the session task did not become reachable in time")


async def stop_task(profile: Mapping[str, Any], task: FargateTask, reason: str) -> None:
    await _aws(
        [
            "ecs",
            "stop-task",
            "--cluster",
            task.cluster,
            "--task",
            task.arn,
            "--reason",
            reason[:255],
        ],
        profile,
        "ecs:StopTask",
    )
