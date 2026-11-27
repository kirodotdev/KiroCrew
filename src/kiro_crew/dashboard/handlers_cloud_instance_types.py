"""The custom-instance-type half of the built-in Remote Crew launch.

``GET /api/cloud/instance-types`` lists the EC2 instance types a Region offers
that fit a Crew host (:mod:`kiro_crew.cloud.instance_types`), and
:func:`check_requested_instance_type` refuses a launch body naming a type that
Region does not offer, before a job file exists. Same owner-only guard as every
other ``/api/cloud`` route.
"""

from __future__ import annotations

from typing import Optional

from aiohttp import web

from kiro_crew.cloud import ec2, instance_types
from kiro_crew.cloud import launch_job as lj
from kiro_crew.cloud.aws import AWSError, CloudActionDenied
from kiro_crew.dashboard.handlers_cloud import (
    _audit,
    _guard,
    _in_executor,
    _sandbox_unavailable_response,
)
from kiro_crew.platform.interfaces import BUILTIN_PROVISIONER_ID
from kiro_crew.sandbox import SandboxUnavailableError
from kiro_crew.validation import ValidationError


async def api_cloud_instance_types(request: web.Request) -> web.Response:
    """GET /api/cloud/instance-types?profile=&region= — the offered Crew host types.

    Each row is ``{instance_type, vcpu, memory_gib, arch, zones}``. "Offered"
    means AWS sells the type in those zones, not that capacity is free now.
    """
    denied = _guard(request, "instance_types")
    if denied is not None:
        return denied
    try:
        profile = ec2.validate_profile(request.query.get("profile", ""))
        region = ec2.validate_region(request.query.get("region", ""))
        if not region:
            # The list is per Region, and the form always names one: an empty
            # value would read the CLI's default Region instead of the form's.
            raise ValidationError("region", "region is required")
    except ValidationError as e:
        _audit("instance_types", "denied", error=str(e))
        return web.json_response({"error": str(e), "code": "invalid_request"}, status=400)
    try:
        rows = await _in_executor(instance_types.list_offered, profile, region)
    except SandboxUnavailableError as exc:
        return _sandbox_unavailable_response("instance_types", exc, "The instance type list")
    except CloudActionDenied as e:
        # A gateway started from an agent session may not run this read; say so,
        # as every other cloud route does, instead of an unaudited 500.
        _audit("instance_types", "denied", error=str(e))
        return web.json_response({"error": str(e), "code": "cloud_action_denied"}, status=403)
    except AWSError as e:
        _audit("instance_types", "failure", error=str(e))
        return web.json_response({"error": str(e), "code": "aws_call_failed"}, status=502)
    _audit("instance_types", "success")
    return web.json_response({"region": region, "instance_types": [r.to_dict() for r in rows]})


async def check_requested_instance_type(
    body: dict, provider_id: str
) -> tuple[str, Optional[web.Response]]:
    """The custom instance type a launch body names, or the 400 that refuses it.

    Empty when the body names none. Only the built-in EC2 lane takes one, and
    only a type the body's Region offers for a Crew host.
    """
    requested = str(body.get("instance_type") or "").strip()
    if not requested:
        return "", None
    try:
        if provider_id != BUILTIN_PROVISIONER_ID:
            raise ValueError("only the built-in EC2 launch takes a custom instance type")
        name = instance_types.validate_name(requested)
        profile = ec2.validate_profile(str(body.get("profile") or ""))
        # An empty Region is the configured one, as the launch job resolves it.
        region = ec2.validate_region(str(body.get("region") or "") or lj._default_region())
        await _in_executor(instance_types.find_offered, name, profile, region)
    except (ValueError, ValidationError) as e:
        _audit("launch_create", "denied", error=f"invalid instance type: {e}")
        return "", web.json_response({"error": str(e), "code": "invalid_instance_type"}, status=400)
    except SandboxUnavailableError as exc:
        return "", _sandbox_unavailable_response("launch_create", exc, "The instance type check")
    except CloudActionDenied as e:
        _audit("launch_create", "denied", error=str(e))
        return "", web.json_response({"error": str(e), "code": "cloud_action_denied"}, status=403)
    except AWSError as e:
        _audit("launch_create", "failure", error=str(e))
        return "", web.json_response({"error": str(e), "code": "aws_call_failed"}, status=502)
    return name, None
