"""Scoped resource access never inherits dashboard-owner authority."""

from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request


def request(path, *, app="bridge", user="owner"):
    req = make_mocked_request("POST", path, app=web.Application())
    req["app"] = app
    req["user"] = user
    return req


def test_resource_path_has_no_ambiguous_segments():
    from kiro_crew.apps.resource_access import ResourceAccessError, resource_path

    good = resource_path("project", "documents", "existing", "design.md", "read")
    assert good == "/api/resource-access/project/documents/existing/design.md/read"
    for bad in ("..", "a/../b", "/tmp", "a\\b", "%2f", "a..b", ""):
        with pytest.raises(ResourceAccessError):
            resource_path(bad, "documents", "existing", "design.md", "read")


@pytest.mark.asyncio
async def test_app_requires_exact_resource_and_operation_grant(tmp_path):
    from kiro_crew.apps.resource_access import ResourceAccessError, ResourceRegistry, resource_path

    path = resource_path("project", "documents", "existing", "design.md", "read")
    callback = AsyncMock(return_value={"content": "saved", "revision": "a" * 64})
    registry = ResourceRegistry(
        workspace_resolver=lambda name: tmp_path if name == "project" else None,
        grants=lambda app: (path,),
        owner_check=lambda req: False,
    )
    registry.register("documents", callback)
    result = await registry.dispatch(
        request(path), "project", "documents", "existing", "design.md", "read", {}
    )
    assert result["content"] == "saved"
    assert callback.call_count == 1
    with pytest.raises(ResourceAccessError, match="resource_forbidden"):
        await registry.dispatch(
            request(path),
            "project",
            "documents",
            "existing",
            "design.md",
            "update",
            {"content": "new", "expectedRevision": "a" * 64},
        )
    assert callback.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["/api/resource-access", "/api/resource-access/*"])
async def test_prefix_or_wildcard_is_not_resource_delegation(tmp_path, grant):
    from kiro_crew.apps.resource_access import ResourceAccessError, ResourceRegistry, resource_path

    path = resource_path("project", "documents", "existing", "state", "read")
    callback = AsyncMock()
    registry = ResourceRegistry(lambda name: tmp_path, lambda app: (grant,), lambda req: False)
    registry.register("documents", callback)
    with pytest.raises(ResourceAccessError, match="resource_forbidden"):
        await registry.dispatch(
            request(path), "project", "documents", "existing", "state", "read", {}
        )
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_is_resolved_server_side_before_callback(tmp_path):
    from kiro_crew.apps.resource_access import ResourceAccessError, ResourceRegistry, resource_path

    path = resource_path("other", "documents", "existing", "state", "read")
    callback = AsyncMock()
    registry = ResourceRegistry(lambda name: None, lambda app: (path,), lambda req: False)
    registry.register("documents", callback)
    with pytest.raises(ResourceAccessError, match="workspace_forbidden"):
        await registry.dispatch(
            request(path), "other", "documents", "existing", "state", "read", {}
        )
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_non_owner_dashboard_caller_does_not_bypass_grants(tmp_path):
    from kiro_crew.apps.resource_access import ResourceAccessError, ResourceRegistry, resource_path

    path = resource_path("project", "documents", "existing", "state", "read")
    callback = AsyncMock()
    registry = ResourceRegistry(lambda name: tmp_path, lambda app: (path,), lambda req: False)
    registry.register("documents", callback)
    with pytest.raises(ResourceAccessError, match="resource_forbidden"):
        await registry.dispatch(
            request(path, app="", user="other"),
            "project",
            "documents",
            "existing",
            "state",
            "read",
            {},
        )
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_provider_and_unapproved_execution_are_refused(tmp_path):
    from kiro_crew.apps.resource_access import ResourceAccessError, ResourceRegistry, resource_path

    path = resource_path("project", "documents", "existing", "state", "read")
    callback = AsyncMock()
    registry = ResourceRegistry(lambda name: tmp_path, lambda app: (path,), lambda req: False)
    registry.register("documents", callback, enabled=lambda: False)
    with pytest.raises(ResourceAccessError, match="resource_unavailable"):
        await registry.dispatch(
            request(path), "project", "documents", "existing", "state", "read", {}
        )
    with pytest.raises(ResourceAccessError, match="unsupported_operation"):
        resource_path("project", "documents", "existing", "state", "execute")
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_owner_and_app_get_same_cas_body_validation(tmp_path):
    from kiro_crew.apps.resource_access import ResourceAccessError, ResourceRegistry, resource_path

    path = resource_path("project", "documents", "existing", "design.md", "update")
    callback = AsyncMock(return_value={"revision": "b" * 64})
    registry = ResourceRegistry(lambda name: tmp_path, lambda app: (path,), lambda req: True)
    registry.register("documents", callback)
    for body in (
        {"content": "stale"},
        {"content": "new", "expectedRevision": None},
        {"content": "new", "expectedRevision": "a" * 64, "path": "/arbitrary"},
    ):
        with pytest.raises(ResourceAccessError):
            await registry.dispatch(
                request(path, app=""),
                "project",
                "documents",
                "existing",
                "design.md",
                "update",
                body,
            )
    callback.assert_not_called()
