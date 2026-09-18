"""skills.sh bundle budget and typed fetch outcomes.

Authored with the change; NOT run locally (host rule), no CI has run them yet.

A bundle above the response cap and every other failure (404, 429, an HTML
page, a dead network) are distinct outcomes, and the install handler makes ONE
download for them rather than re-requesting the same bundle through the
single-file fallback. This file pins that contract:

* the download budget is its own 10 MiB figure, streamed and abandoned at the
  limit, while search keeps its 1 MiB figure;
* each failure is a typed ``SkillFetchError`` with its own status and a message
  that carries only sizes and status codes -- never the remote body;
* the handlers map those to distinct HTTP answers and make ONE download.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import discover as h
from kiro_crew.skill_providers import skillsh
from kiro_crew.skill_providers.base import (
    ProviderRegistry,
    SkillBadFormat,
    SkillFetchError,
    SkillNotFound,
    SkillRateLimited,
    SkillTooLarge,
    SkillUnreachable,
    SkillUpstreamStatus,
)
from kiro_crew.skill_providers.skillsh import SkillsShProvider
from kiro_crew.skills import SkillsLoader

URL = "https://skills.sh/api/download/owner/repo/skill"


class _Resp:
    """A minimal ``urllib`` response: status, headers, streamed body."""

    def __init__(self, body: bytes, status: int = 200, content_length: int | None = None):
        self._buf = io.BytesIO(body)
        self.status = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.closed = False
        self.reads = 0

    def read(self, n: int = -1) -> bytes:
        self.reads += 1
        return self._buf.read(n)

    def close(self) -> None:
        self.closed = True


def _open_returning(resp: Any):
    return patch.object(skillsh, "_open_no_internal_redirect", return_value=resp)


def _open_raising(exc: BaseException):
    return patch.object(skillsh, "_open_no_internal_redirect", side_effect=exc)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "x", {}, None)  # type: ignore[arg-type]


# ── budgets ────────────────────────────────────────────────────────────────


def test_bundle_budget_is_ten_mib_and_search_budget_stays_one_mib() -> None:
    assert skillsh._MAX_BUNDLE_BYTES == 10 * 1024 * 1024
    assert skillsh._MAX_RESPONSE_BYTES == 1 * 1024 * 1024
    assert h._MAX_INSTALL_BYTES == skillsh._MAX_BUNDLE_BYTES


def test_bundle_between_one_and_ten_mib_is_read_whole() -> None:
    files = [
        {"path": "SKILL.md", "contents": "# s"},
        {"path": "a.txt", "contents": "x" * (2_400_000)},
    ]
    body = json.dumps({"files": files}).encode()
    assert 1024 * 1024 < len(body) < 10 * 1024 * 1024
    resp = _Resp(body)
    with _open_returning(resp):
        data = skillsh._sync_fetch_json(URL, skillsh._MAX_BUNDLE_BYTES)
    assert data["files"][1]["path"] == "a.txt"
    assert resp.closed


def test_bundle_over_ten_mib_is_abandoned_mid_stream_with_its_size() -> None:
    limit = skillsh._MAX_BUNDLE_BYTES
    body = b"[" + b"x" * (limit + 5 * 64 * 1024) + b"]"
    resp = _Resp(body)  # no Content-Length: the size is only learned by reading
    with _open_returning(resp):
        with pytest.raises(SkillTooLarge) as exc:
            skillsh._sync_fetch_json(URL, limit)
    # Streamed in 64 KiB chunks and stopped right past the limit -- far fewer
    # reads than the body holds, so nothing beyond the budget was retained.
    assert resp.reads <= limit // skillsh._HTTP_READ_CHUNK_BYTES + 2
    assert resp.closed
    assert exc.value.http_status == 413 and exc.value.code == "too_large"
    # The size is the bytes actually read -- a lower bound just past the limit,
    # never the (untrusted) declared length -- and the wording says so.
    assert exc.value.aborted is True
    assert limit < exc.value.size <= limit + skillsh._HTTP_READ_CHUNK_BYTES
    assert exc.value.message.startswith("Skill bundle is more than 10.")
    assert exc.value.message.endswith(", above the 10.0 MiB limit")


def test_declared_oversize_is_refused_before_reading_the_body() -> None:
    limit = skillsh._MAX_BUNDLE_BYTES
    resp = _Resp(b"{}", content_length=2_405_598 * 5)
    with _open_returning(resp):
        with pytest.raises(SkillTooLarge) as exc:
            skillsh._sync_fetch_json(URL, limit)
    assert resp.reads == 0 and resp.closed
    assert exc.value.size == 2_405_598 * 5 and exc.value.aborted is False
    assert (
        exc.value.message
        == f"Skill bundle is {2_405_598 * 5 / (1024 * 1024):.1f} MiB, above the 10.0 MiB limit"
    )


def test_a_size_that_rounds_to_the_limit_reads_as_just_over() -> None:
    limit = skillsh._MAX_BUNDLE_BYTES
    exact = SkillTooLarge(limit + 1, limit)
    assert exact.message == "Skill bundle is just over 10.0 MiB, above the 10.0 MiB limit"
    aborted = SkillTooLarge(limit + 1, limit, aborted=True)
    assert aborted.message == "Skill bundle is more than 10.0 MiB, above the 10.0 MiB limit"
    clear = SkillTooLarge(limit + 1024 * 1024, limit)
    assert clear.message == "Skill bundle is 11.0 MiB, above the 10.0 MiB limit"


def test_a_lying_content_length_does_not_shape_the_aborted_size() -> None:
    """A declared length under the limit does not exempt the stream from the
    budget, and the refusal reports what was read, not what was declared."""
    limit = skillsh._MAX_BUNDLE_BYTES
    body = b"[" + b"x" * (limit + 64 * 1024) + b"]"
    resp = _Resp(body, content_length=1024)
    with _open_returning(resp):
        with pytest.raises(SkillTooLarge) as exc:
            skillsh._sync_fetch_json(URL, limit)
    assert exc.value.aborted is True and exc.value.size > limit
    assert "more than" in exc.value.message


def test_search_response_over_one_mib_is_still_refused() -> None:
    body = b"[" + b"x" * (skillsh._MAX_RESPONSE_BYTES + 64 * 1024) + b"]"
    with _open_returning(_Resp(body)):
        with pytest.raises(SkillTooLarge):
            skillsh._sync_fetch_json(
                "https://skills.sh/api/search?q=x", skillsh._MAX_RESPONSE_BYTES
            )


# ── classification ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "expected", "status"),
    [
        (404, SkillNotFound, 404),
        (429, SkillRateLimited, 429),
        (403, SkillUpstreamStatus, 502),
        (500, SkillUpstreamStatus, 502),
    ],
)
def test_http_statuses_are_distinct_outcomes(code: int, expected: type, status: int) -> None:
    with _open_raising(_http_error(code)):
        with pytest.raises(expected) as exc:
            skillsh._sync_fetch_json(URL, skillsh._MAX_BUNDLE_BYTES)
    assert exc.value.http_status == status
    if expected is SkillUpstreamStatus:
        assert exc.value.status == code
        assert f"HTTP {code}" in exc.value.message


def test_html_body_is_bad_format_and_the_body_is_not_echoed() -> None:
    html = b"<!doctype html><html><body>SECRET-MARKER maintenance</body></html>"
    with _open_returning(_Resp(html)):
        with pytest.raises(SkillBadFormat) as exc:
            skillsh._sync_fetch_json(URL, skillsh._MAX_BUNDLE_BYTES)
    assert exc.value.http_status == 502 and exc.value.code == "bad_format"
    assert "SECRET-MARKER" not in exc.value.message


def test_no_response_is_unreachable() -> None:
    with _open_returning(None):
        with pytest.raises(SkillUnreachable) as exc:
            skillsh._sync_fetch_json(URL, skillsh._MAX_BUNDLE_BYTES)
    assert exc.value.http_status == 502 and exc.value.code == "unreachable"


def test_ssrf_blocked_url_is_unreachable_without_a_request() -> None:
    with patch.object(skillsh, "_open_no_internal_redirect") as opener:
        with pytest.raises(SkillUnreachable):
            skillsh._sync_fetch_json("http://127.0.0.1/api/download/x", skillsh._MAX_BUNDLE_BYTES)
    opener.assert_not_called()


@pytest.mark.asyncio
async def test_provider_not_found_names_the_requested_skill() -> None:
    with patch.object(
        skillsh, "_sync_fetch_json", side_effect=SkillNotFound("requested skill", "skills.sh")
    ):
        with pytest.raises(SkillNotFound) as exc:
            await SkillsShProvider().fetch_skill_bundle("owner/repo/skill")
    assert exc.value.message == "Skill 'owner/repo/skill' was not found on skills.sh"


@pytest.mark.asyncio
async def test_provider_uses_the_bundle_budget_for_downloads_and_the_small_one_for_search() -> None:
    seen: list[tuple[str, int]] = []

    def fake(url: str, max_bytes: int):
        seen.append((url, max_bytes))
        return (
            {"files": [{"path": "SKILL.md", "contents": "# s"}]}
            if "download" in url
            else {"skills": []}
        )

    with patch.object(skillsh, "_sync_fetch_json", side_effect=fake):
        p = SkillsShProvider()
        await p.fetch_skill_bundle("o/r/s")
        await p.search("x")
    assert [b for u, b in seen if "download" in u] == [skillsh._MAX_BUNDLE_BYTES]
    assert [b for u, b in seen if "search" in u] == [skillsh._MAX_RESPONSE_BYTES]


@pytest.mark.asyncio
async def test_search_swallows_typed_failures_into_no_rows() -> None:
    with patch.object(skillsh, "_sync_fetch_json", side_effect=SkillRateLimited("skills.sh")):
        assert await SkillsShProvider().search("docker") == []


@pytest.mark.asyncio
async def test_fetch_content_propagates_the_typed_failure() -> None:
    with patch.object(
        skillsh, "_sync_fetch_json", side_effect=SkillTooLarge(11 * 1024 * 1024, 10 * 1024 * 1024)
    ):
        with pytest.raises(SkillTooLarge):
            await SkillsShProvider().fetch_skill_content("o/r/s")


# ── handler mapping, one download ─────────────────────────────────────────


class _FailingProvider:
    def __init__(self, exc: SkillFetchError) -> None:
        self.exc = exc
        self.bundle_calls = 0
        self.content_calls = 0

    @property
    def name(self) -> str:
        return "covprov"

    @property
    def display_name(self) -> str:
        return "Cov Provider"

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, *, limit: int = 20):
        return []

    async def fetch_skill_bundle(self, skill_id: str):
        self.bundle_calls += 1
        raise self.exc

    async def fetch_skill_content(self, skill_id: str):
        self.content_calls += 1
        raise self.exc


@pytest.fixture()
def state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(h, "_skills_dir", lambda: root)
    monkeypatch.setattr(h, "_sel", lambda: MagicMock())
    st = MagicMock(context_builder=None)
    st._standalone_skills = SkillsLoader(skills_path=root, install_builtins=False)
    return st


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> ProviderRegistry:
    reg = ProviderRegistry()
    monkeypatch.setattr(h, "_registry", reg)
    return reg


def _mk(method: str, path: str, *, state: Any, body: Any = ...) -> web.Request:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(method, path, app=app)
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(resp: web.Response) -> dict:
    return json.loads(resp.text or "{}")


_CASES = [
    (
        SkillTooLarge(2_405_598, 1024 * 1024),
        413,
        "too_large",
        "Skill bundle is 2.3 MiB, above the 1.0 MiB limit",
    ),
    (
        SkillNotFound("o/r/s", "skills.sh"),
        404,
        "not_found",
        "Skill 'o/r/s' was not found on skills.sh",
    ),
    (
        SkillRateLimited("skills.sh"),
        429,
        "rate_limited",
        "skills.sh is rate-limiting requests; try again shortly",
    ),
    (SkillUpstreamStatus(403, "skills.sh"), 502, "http_status", "skills.sh answered HTTP 403"),
    (
        SkillBadFormat("skills.sh", "non-JSON response"),
        502,
        "bad_format",
        "skills.sh returned an non-JSON response",
    ),
    (SkillUnreachable("skills.sh"), 502, "unreachable", "Could not reach skills.sh"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "status", "code", "message"), _CASES)
async def test_install_maps_each_outcome_and_downloads_once(
    state: MagicMock,
    registry: ProviderRegistry,
    exc: SkillFetchError,
    status: int,
    code: str,
    message: str,
) -> None:
    provider = _FailingProvider(exc)
    registry.register(provider)
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "o/r/s"})
    response = await h.api_skills_discover_install(request)
    assert response.status == status
    assert _body(response) == {"error": message, "code": code}
    # The definitive failure ends the fetch: no single-file fallback re-request.
    assert provider.bundle_calls == 1 and provider.content_calls == 0


@pytest.mark.asyncio
async def test_typed_error_message_goes_through_egress_redaction(
    state: MagicMock, registry: ProviderRegistry
) -> None:
    """The not-found message names the requested id, which is request-controlled:
    a credential-shaped id must come back scrubbed, on install and preview."""
    secret_id = "o/r/AKIAIOSFODNN7EXAMPLEXYZ"
    registry.register(_FailingProvider(SkillNotFound(secret_id, "skills.sh")))
    install = await h.api_skills_discover_install(
        _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": secret_id})
    )
    preview = await h.api_skills_discover_preview(
        _mk("GET", f"/p?provider=covprov&id={secret_id}", state=state)
    )
    for response in (install, preview):
        assert response.status == 404
        body = _body(response)
        assert body["code"] == "not_found"
        assert "AKIAIOSFODNN7EXAMPLEXYZ" not in body["error"]
        assert "[REDACTED" in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "status", "code", "message"), _CASES)
async def test_preview_maps_each_outcome_and_downloads_once(
    state: MagicMock,
    registry: ProviderRegistry,
    exc: SkillFetchError,
    status: int,
    code: str,
    message: str,
) -> None:
    provider = _FailingProvider(exc)
    registry.register(provider)
    request = _mk("GET", "/p?provider=covprov&id=o/r/s", state=state)
    response = await h.api_skills_discover_preview(request)
    assert response.status == status
    assert _body(response) == {"error": message, "code": code}
    assert provider.bundle_calls == 1 and provider.content_calls == 0
