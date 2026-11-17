"""A malformed ``{slug}`` is a 400 on the publish, relocate and comment routes, never a 500.

The store rejects a slug outside its grammar with ``ArtifactValidationError``
before any file access; these routes only mapped ``ArtifactNotFoundError``, so the
validation error escaped the handler as a server error.
"""

from __future__ import annotations

import pytest
from artifacts_handlers_helpers import (  # noqa: F401 -- fixtures are requested by name
    _request,
    isolated_store_fixture,
    patch_restricted_fixture,
)

from kiro_crew.artifacts import ArtifactValidationError
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_comments,
    api_artifact_mark_review,
    api_artifact_overwrite_remote,
    api_artifact_publish,
    api_artifact_pull_latest,
    api_artifact_relocate,
    api_artifact_reopen_comment,
    api_artifact_update_sharing,
)

_BAD_SLUG = "Bad_Slug"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "body"),
    [
        (api_artifact_publish, {"visibility": "PRIVATE"}),
        (api_artifact_update_sharing, {"visibility": "PRIVATE"}),
        (api_artifact_pull_latest, None),
        (api_artifact_overwrite_remote, None),
        (api_artifact_relocate, {"source_path": ""}),
        (api_artifact_comments, None),
        (api_artifact_mark_review, None),
        (api_artifact_reopen_comment, None),
    ],
    ids=[
        "publish",
        "update_sharing",
        "pull_latest",
        "overwrite_remote",
        "relocate",
        "comments",
        "mark_review",
        "reopen_comment",
    ],
)
async def test_a_malformed_slug_is_a_400(isolated_store, patch_restricted, handler, body) -> None:
    try:
        resp = await handler(_request(body=body, match={"slug": _BAD_SLUG, "comment_id": "c1"}))
    except ArtifactValidationError as exc:
        # aiohttp answers an exception that escapes a handler with a 500.
        pytest.fail(f"the slug error escaped the handler as a 500: {exc}")
    assert resp.status == 400
