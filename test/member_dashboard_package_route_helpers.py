"""Fixtures and fakes shared by the ``test_member_dashboard_package_route*.py`` modules.

The crewmate, owner and package names, the package factory, an artifact store narrowed
to one APPLIED package, the ``wired`` route app that records the frames it broadcasts,
and the request, call and publish helpers. ``wired`` is declared
``@pytest.fixture(name=...)`` and imported under its ``*_fixture`` function name, so a
test's parameter does not shadow the import.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.crew_log import bus as crew_log_bus
from kiro_crew.dashboard.handlers import member_dashboard as md


def _owner_write(store: Any, slug: str, **kwargs: Any) -> Any:
    """A dashboard content write under the record's CURRENT token.

    ``ArtifactStore.update`` refuses a kind or content write on a record whose kind is
    ``dashboard`` unless it names the read it was authorized against, and an absent
    token is a refusal -- see
    ``test_dashboard_package_owner_gate.TestAnUntokenedWriteCannotTakeADashboardOver``.
    The route reads the slug before every write and hands the store that token; a test
    playing the owner reads it here for the same reason.
    """
    token = store.get(slug, assign_token=True).content_token
    return store.update(slug, snapshot_token=token, **kwargs)


CREW = "atlas"
SLUG = "atlas"
OWNER = "U_OWNER"
PACKAGE_SLUG = "atlas-dashboard"

#: Above any revision the eager folder has already minted for the slot: the feed
#: subscribes with a baseline, and the cache drops an event that is not newer.
_REVISION_BASE = 2_000_000

PAGE = (
    '<p data-dashboard-field="goal"></p>'
    "<script>window.kirocrew.subscribe('goal', (v) => v);</script>"
)


def _package(bound_to: str = f"crewmate:{SLUG}", *, html: str = PAGE) -> dict[str, Any]:
    return dp.validate_package(
        {
            "kind": "dashboard",
            "bound_to": bound_to,
            "model": {
                "types": {
                    "goal": {
                        "type": "text",
                        "label": "Goal",
                        "source": {"fold": "work", "path": "conductor.goal"},
                    }
                }
            },
            "view": {"html": html},
            "theme": {"tokens": {}},
        }
    )


def _fake_cfg() -> Any:
    return SimpleNamespace(
        agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew", description="d", model="m1")},
        default_agent=CREW,
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
    )


#: The lifecycle event a PERSON's approval leaves on the record: a version cut with the
#: `user` actor. The render path serves the newest version carrying one, so a fixture
#: standing in for an APPLIED package has to carry it -- a stub without it models a
#: package whose owner agent has written pages nobody approved, which renders nothing.
APPLIED_VERSION = 4
APPLIED_EVENTS: list[dict[str, Any]] = [
    {"type": "iterated", "by": "agent", "version": 3},
    {"type": "edited", "by": "user", "version": APPLIED_VERSION},
]


class _Store:
    """The artifact store, narrowed to what the route reads: one APPLIED package.

    `get(slug)` answers the record (with its events) and `get(slug, version=N)` the
    bytes of that version, which is the pair the render path uses: the events say which
    version a person applied, and the versioned read is what it serves.
    """

    def __init__(self, content: str) -> None:
        self.content = content
        self.version_reads: list[int] = []

    def get(self, slug: str, *, version: int | None = None) -> Any:
        if slug != PACKAGE_SLUG:
            from kiro_crew.artifacts import ArtifactError

            raise ArtifactError(f"no artifact {slug!r}")
        if version is not None:
            from kiro_crew.artifacts import ArtifactError

            self.version_reads.append(version)
            if version != APPLIED_VERSION:
                raise ArtifactError(f"version {version} not found for {slug}")
        return SimpleNamespace(
            slug=slug,
            content=self.content,
            version=APPLIED_VERSION,
            kind="dashboard",
            events=list(APPLIED_EVENTS),
        )


@pytest.fixture(name="wired")
def wired_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The route's app, with the frames it broadcasts recorded."""
    frames: list[tuple[str, Any]] = []
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    monkeypatch.setattr(
        state, "broadcast_ws_owners", lambda kind, data: frames.append((kind, data)), raising=False
    )
    app = web.Application()
    app["state"] = state
    store = _Store(dp.dump_package(_package()))
    monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
    # The slot the fold values are keyed by, resolved rather than guessed in
    # production; pinned here so the publish below names the same one.
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: SLUG)
    monkeypatch.setattr(md, "_write_session", lambda slug, member: "")
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, config=None: SLUG)
    yield SimpleNamespace(app=app, frames=frames, store=store)
    for page in list(app.get("member_dashboard_live_pages", {}).values()):
        page.disarm()


def _request(app: web.Application, method: str, path: str) -> web.Request:
    req = make_mocked_request(method, path, app=app, match_info={"slug": SLUG})
    req["app"] = ""
    req["user"] = OWNER
    return req


async def _call(handler: Any, req: web.Request) -> tuple[int, Any]:
    with patch.object(md.KiroCrewConfig, "load", return_value=_fake_cfg()):
        resp = await handler(req)
    return resp.status, json.loads(resp.body)


def _publish(fold: str, value: dict[str, Any], revision: int) -> None:
    crew_log_bus.publish(
        crew_log_bus.FOLD_ADVANCED,
        crew_log_bus.FoldAdvanced(
            scope=crew_log_bus.SCOPE_SLOT,
            key=SLUG,
            fold=fold,
            revision=_REVISION_BASE + revision,
            seq=revision,
            value=value,
        ),
    )


PATH = f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={PACKAGE_SLUG}"
