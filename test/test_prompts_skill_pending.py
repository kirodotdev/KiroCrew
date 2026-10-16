"""The staged skill candidates' dismiss routes dismiss exactly what they are asked to.

The pending-review routes live in ``dashboard/handlers/prompts_skill_pending.py`` and are
reached, like every prompt and skill route, through the ``prompts`` facade.
``POST /api/skills/-/pending/-/dismiss-all`` takes the set of candidates the client showed
the user, so a candidate staged after the confirmation dialog is never dismissed, and a
missing, empty or malformed ``slugs`` list dismisses nothing. ``POST
/api/skills/-/pending/{slug}/dismiss`` refuses a slug that is not a plain stem and answers a
coded 404 for an unknown one. Every outcome leaves a SEL record, and a failure is answered
with a 500, not an unhandled error. The list route still answers when the store cannot be
read. The owner gate on these routes has its own coverage in ``test_skill_write_guard.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import prompts as H
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

_OMITTED = object()


class _Req:
    """Minimal aiohttp-request stand-in for the handler."""

    def __init__(self, loader: SkillsLoader, *, body: object = _OMITTED, bad_json: bool = False):
        state = SimpleNamespace(context_builder=SimpleNamespace(skills=loader))
        self.app = {"state": state}
        self.match_info: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self._body = {} if body is _OMITTED else body
        self._bad_json = bad_json

    async def json(self) -> object:
        if self._bad_json:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._body


def _payload(resp) -> dict:
    return json.loads(resp.body.decode())


@pytest.fixture(autouse=True)
def _owner(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as the dashboard owner; the gate itself is covered elsewhere.

    On the floor monkeypatch (D11), so a test calling ``monkeypatch.undo()`` cannot lift it.
    """
    _floor_monkeypatch.setattr(
        H, "is_owner_dashboard_request", lambda _request: True, raising=False
    )


@pytest.fixture()
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every SEL tool-invocation record the route writes, in order."""
    recorded: list[dict] = []
    monkeypatch.setattr(
        H,
        "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: recorded.append(kw)),
    )
    return recorded


@pytest.fixture()
def loader(tmp_path) -> SkillsLoader:
    ld = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    for slug in ("deploy-helper", "triage-helper"):
        ld.stage_skill_candidate(
            slug,
            description=f"{slug} description",
            triggers=slug,
            procedure_md="## Steps\n1. go\n",
            provenance=AutoSkillProvenance(
                session_key="s", created_at=AutoSkillProvenance.now_iso()
            ),
        )
    return ld


def _pending_slugs(loader: SkillsLoader) -> set[str]:
    return {row["slug"] for row in loader.list_pending_skills()}


@pytest.mark.asyncio
async def test_only_the_named_candidates_are_dismissed(loader, events):
    resp = await H.api_skills_pending_dismiss_all(_Req(loader, body={"slugs": ["deploy-helper"]}))

    assert resp.status == 200
    assert _payload(resp) == {"dismissed_count": 1}
    assert _pending_slugs(loader) == {"triage-helper"}
    assert [(e["tool_name"], e["outcome"], e["metadata"]) for e in events] == [
        ("api_skills_pending_dismiss_all", "ok", {"count": 1})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_kwargs",
    [{"body": {}}, {"body": {"slugs": []}}, {"body": {"slugs": None}}, {"bad_json": True}],
    ids=["no-slugs-key", "empty-slugs", "null-slugs", "unparseable-body"],
)
async def test_without_slugs_nothing_is_dismissed(loader, events, request_kwargs):
    resp = await H.api_skills_pending_dismiss_all(_Req(loader, **request_kwargs))

    assert resp.status == 400
    assert _payload(resp)["code"] == "slugs_required"
    assert _pending_slugs(loader) == {"deploy-helper", "triage-helper"}
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ([], "invalid_body"),
        ("deploy-helper", "invalid_body"),
        ({"slugs": "deploy-helper"}, "invalid_slugs"),
        ({"slugs": ["deploy-helper", 7]}, "invalid_slugs"),
    ],
    ids=["array-body", "string-body", "string-slugs", "non-string-slug"],
)
async def test_a_malformed_body_is_refused_before_anything_is_dismissed(loader, events, body, code):
    resp = await H.api_skills_pending_dismiss_all(_Req(loader, body=body))

    assert resp.status == 400
    assert _payload(resp)["code"] == code
    assert _pending_slugs(loader) == {"deploy-helper", "triage-helper"}
    assert events == []


@pytest.mark.asyncio
async def test_a_failed_dismissal_is_audited_and_answered_with_a_coded_500(
    loader, events, monkeypatch
):
    def _boom(_slugs):
        raise OSError("read-only skills dir")

    monkeypatch.setattr(loader, "dismiss_pending_slugs", _boom)

    resp = await H.api_skills_pending_dismiss_all(_Req(loader, body={"slugs": ["deploy-helper"]}))

    assert resp.status == 500
    assert _payload(resp) == {"error": "internal error", "code": "internal_error"}
    assert [(e["tool_name"], e["outcome"]) for e in events] == [
        ("api_skills_pending_dismiss_all", "error")
    ]
    assert _pending_slugs(loader) == {"deploy-helper", "triage-helper"}


@pytest.mark.asyncio
async def test_one_candidate_is_dismissed_by_its_slug(loader, events):
    req = _Req(loader)
    req.match_info = {"slug": "deploy-helper"}

    resp = await H.api_skill_pending_dismiss(req)

    assert resp.status == 200
    assert _payload(resp) == {"dismissed": "deploy-helper"}
    assert _pending_slugs(loader) == {"triage-helper"}
    assert [(e["tool_name"], e["outcome"]) for e in events] == [("api_skill_pending_dismiss", "ok")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slug", "status", "outcome"),
    [("../etc", 400, "rejected"), ("no-such-candidate", 404, "not_found")],
    ids=["not-a-plain-stem", "unknown-slug"],
)
async def test_a_slug_that_names_no_candidate_dismisses_nothing(
    loader, events, slug, status, outcome
):
    req = _Req(loader)
    req.match_info = {"slug": slug}

    resp = await H.api_skill_pending_dismiss(req)

    assert resp.status == status
    if status == 404:
        assert _payload(resp)["code"] == "pending_skill_not_found"
    assert _pending_slugs(loader) == {"deploy-helper", "triage-helper"}
    assert [(e["tool_name"], e["outcome"]) for e in events] == [
        ("api_skill_pending_dismiss", outcome)
    ]


@pytest.mark.asyncio
async def test_a_failed_single_dismissal_is_audited_and_answered_with_a_500(
    loader, events, monkeypatch
):
    def _boom(_slug):
        raise OSError("read-only skills dir")

    monkeypatch.setattr(loader, "dismiss_pending_skill", _boom)
    req = _Req(loader)
    req.match_info = {"slug": "deploy-helper"}

    resp = await H.api_skill_pending_dismiss(req)

    assert resp.status == 500
    assert [(e["tool_name"], e["outcome"]) for e in events] == [
        ("api_skill_pending_dismiss", "error")
    ]


@pytest.mark.asyncio
async def test_the_list_still_answers_when_the_store_cannot_be_read(loader, events, monkeypatch):
    def _boom():
        raise OSError("skills dir unreadable")

    monkeypatch.setattr(loader, "list_pending_skills", _boom)

    resp = await H.api_skills_pending(_Req(loader))

    assert resp.status == 200
    assert _payload(resp) == {"pending": []}
    assert [(e["tool_name"], e["outcome"], e["metadata"]) for e in events] == [
        ("api_skills_pending", "ok", {"count": 0})
    ]
