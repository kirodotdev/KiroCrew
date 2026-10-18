"""A skill provider can say WHY it produced nothing.

``search() -> []`` means the provider looked and nothing matched, and
``fetch_skill_bundle() -> None`` means there is nothing to fetch; a provider that
could not look raises ``SkillProviderError`` instead, so a rate limit, a missing
repository and an empty catalog read differently. These pin that channel below
the handler:

- ``_http`` classifies a failed fetch into a :class:`FetchError` only when asked,
  and keeps the ``None`` contract otherwise;
- the GitHub and skills.sh providers turn that into a ``SkillProviderError`` with a
  code and a sentence built from fixed text, never the response body;
- the registry keeps the reason on the provider's outcome, and still reports an
  empty answer as ``ok``.
"""

from __future__ import annotations

import io
import time
import urllib.error
from email.message import Message
from unittest.mock import patch

import pytest

from kiro_crew.skill_providers import _http
from kiro_crew.skill_providers import github as gh
from kiro_crew.skill_providers import skillsh
from kiro_crew.skill_providers.base import (
    ProviderRegistry,
    SkillProviderError,
    SkillSearchResult,
)

_COMMIT = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    msg = Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", msg, io.BytesIO(b"SECRET BODY")
    )


class _Opener:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def open(self, req, timeout=None):  # noqa: ANN001 -- urllib signature
        raise self.exc


def _fetch_with(exc: Exception, *, raise_on_failure: bool):
    with patch.object(_http.urllib.request, "build_opener", return_value=_Opener(exc)):
        return _http.sync_fetch_bytes(
            "https://api.github.com/x",
            allowed_hosts={"api.github.com"},
            internal_check=lambda _u: False,
            raise_on_failure=raise_on_failure,
        )


class TestFetchError:
    def test_github_primary_rate_limit_is_named_with_its_reset(self):
        reset = int(time.time()) + 600
        err = _http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})
        with pytest.raises(_http.FetchError) as caught:
            _fetch_with(err, raise_on_failure=True)
        assert caught.value.rate_limited is True
        assert caught.value.status == 403
        assert caught.value.reset_at == reset

    def test_retry_after_wins_over_the_primary_window_reset(self):
        # A secondary limit: budget left, Retry-After 60, and the primary window's
        # reset most of an hour away. The wait is the 60 seconds.
        far = int(time.time()) + 3000
        err = _http_error(
            403, {"X-RateLimit-Remaining": "41", "X-RateLimit-Reset": str(far), "Retry-After": "60"}
        )
        with pytest.raises(_http.FetchError) as caught:
            _fetch_with(err, raise_on_failure=True)
        assert caught.value.rate_limited is True
        assert (caught.value.reset_at or 0) <= int(time.time()) + 60

    def test_an_absurd_reset_is_dropped(self):
        err = _http_error(429, {"X-RateLimit-Reset": "9" * 30})
        with pytest.raises(_http.FetchError) as caught:
            _fetch_with(err, raise_on_failure=True)
        assert caught.value.rate_limited is True and caught.value.reset_at is None

    def test_429_is_a_rate_limit_and_retry_after_sets_the_reset(self):
        before = int(time.time())
        with pytest.raises(_http.FetchError) as caught:
            _fetch_with(_http_error(429, {"Retry-After": "120"}), raise_on_failure=True)
        assert caught.value.rate_limited is True
        assert before + 120 <= (caught.value.reset_at or 0) <= int(time.time()) + 120

    def test_a_plain_403_is_not_a_rate_limit(self):
        with pytest.raises(_http.FetchError) as caught:
            _fetch_with(_http_error(403), raise_on_failure=True)
        assert caught.value.rate_limited is False and caught.value.status == 403

    def test_the_body_never_rides_on_the_error(self):
        with pytest.raises(_http.FetchError) as caught:
            _fetch_with(_http_error(404), raise_on_failure=True)
        assert "SECRET" not in str(caught.value)
        assert caught.value.status == 404

    def test_no_answer_is_network(self):
        with pytest.raises(_http.FetchError) as caught:
            _fetch_with(urllib.error.URLError("dns"), raise_on_failure=True)
        assert caught.value.reason == "network" and caught.value.status is None

    def test_the_default_contract_is_still_none(self):
        assert _fetch_with(_http_error(404), raise_on_failure=False) is None

    def test_a_blocked_url_raises_blocked(self):
        with pytest.raises(_http.FetchError) as caught:
            _http.sync_fetch_bytes(
                "http://127.0.0.1/",
                allowed_hosts=set(),
                internal_check=lambda _u: True,
                raise_on_failure=True,
            )
        assert caught.value.reason == "blocked"

    @pytest.mark.asyncio
    async def test_run_off_loop_lets_a_fetch_error_through(self):
        def _boom():
            raise _http.FetchError("http", status=404)

        with pytest.raises(_http.FetchError):
            await _http.run_off_loop(_boom)


def _raising(exc: _http.FetchError):
    def _fetch(*_args, **_kwargs):
        raise exc

    return _fetch


class TestGitHubReasons:
    @pytest.mark.asyncio
    async def test_rate_limit_names_the_reset_time(self):
        # 599 s away, so the rounded-up wait is 10 minutes however the clock ticks.
        exc = _http.FetchError(
            "http",
            status=403,
            rate_limited=True,
            reset_at=int(time.time()) + 599,
        )
        with patch.object(gh, "_sync_fetch_text", _raising(exc)):
            with pytest.raises(SkillProviderError) as caught:
                await gh.GitHubRepoProvider().search("acme/widgets")
        assert caught.value.code == "rate_limited"
        assert caught.value.message == "GitHub is limiting requests. Try again in about 10 minutes."

    @pytest.mark.asyncio
    async def test_a_limit_without_a_reset_says_within_the_hour(self):
        exc = _http.FetchError("http", status=403, rate_limited=True)
        with patch.object(gh, "_sync_fetch_text", _raising(exc)):
            with pytest.raises(SkillProviderError) as caught:
                await gh.GitHubRepoProvider().search("acme/widgets")
        assert caught.value.code == "rate_limited"
        assert caught.value.message == "GitHub is limiting requests. Try again within the hour."

    @pytest.mark.asyncio
    async def test_an_unreadable_answer_is_not_called_unreachable(self):
        with patch.object(gh, "_sync_fetch_text", _raising(_http.FetchError("decode"))):
            with pytest.raises(SkillProviderError) as caught:
                await gh.GitHubRepoProvider().search("acme/widgets")
        assert caught.value.code == "unavailable"
        assert caught.value.message == "GitHub sent an answer that could not be read."

    @pytest.mark.asyncio
    async def test_a_missing_repository_is_not_found(self):
        with patch.object(gh, "_sync_fetch_text", _raising(_http.FetchError("http", status=404))):
            with pytest.raises(SkillProviderError) as caught:
                await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert caught.value.code == "not_found"
        assert "acme/widgets" in caught.value.message and "private" in caught.value.message

    @pytest.mark.asyncio
    async def test_a_missing_ref_names_the_ref(self):
        with patch.object(gh, "_sync_fetch_text", _raising(_http.FetchError("http", status=422))):
            with pytest.raises(SkillProviderError) as caught:
                await gh.GitHubRepoProvider().search("acme/widgets@nope")
        assert caught.value.code == "not_found" and "'nope'" in caught.value.message

    @pytest.mark.asyncio
    async def test_a_bare_slug_that_is_no_repository_searches_empty(self):
        # "react/hooks" is also a catalog query, so a 404 for it is no match.
        with patch.object(gh, "_sync_fetch_text", _raising(_http.FetchError("http", status=404))):
            assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_a_pasted_url_that_is_no_repository_is_not_found(self):
        with patch.object(gh, "_sync_fetch_text", _raising(_http.FetchError("http", status=404))):
            with pytest.raises(SkillProviderError) as caught:
                await gh.GitHubRepoProvider().search("https://github.com/acme/widgets")
        assert caught.value.code == "not_found"

    @pytest.mark.asyncio
    async def test_a_bare_slug_still_reports_a_rate_limit(self):
        exc = _http.FetchError("http", status=429, rate_limited=True)
        with patch.object(gh, "_sync_fetch_text", _raising(exc)):
            with pytest.raises(SkillProviderError) as caught:
                await gh.GitHubRepoProvider().search("acme/widgets")
        assert caught.value.code == "rate_limited"

    @pytest.mark.asyncio
    async def test_a_missing_directory_names_the_path(self):
        with patch.multiple(
            gh,
            _sync_fetch_text=lambda *_a, **_k: _COMMIT,
            _sync_fetch_json=_raising(_http.FetchError("http", status=404)),
        ):
            with pytest.raises(SkillProviderError) as caught:
                await gh.GitHubRepoProvider().search("acme/widgets:skills/gone")
        assert caught.value.code == "not_found" and "'skills/gone'" in caught.value.message

    @pytest.mark.asyncio
    async def test_a_repository_with_no_skill_is_measured_empty(self):
        tree = {"truncated": False, "tree": [{"path": "README.md", "type": "blob", "size": 3}]}
        with patch.multiple(
            gh,
            _sync_fetch_text=lambda *_a, **_k: _COMMIT,
            _sync_fetch_json=lambda *_a, **_k: tree,
        ):
            assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_a_rate_limit_while_fetching_files_refuses_with_that_reason(self):
        tree = {"truncated": False, "tree": [{"path": "SKILL.md", "type": "blob", "size": 9}]}
        with patch.multiple(
            gh,
            _sync_fetch_text=lambda *_a, **_k: _COMMIT,
            _sync_fetch_json=lambda *_a, **_k: tree,
            _sync_fetch_bytes=_raising(_http.FetchError("http", status=429, rate_limited=True)),
        ):
            with pytest.raises(SkillProviderError) as caught:
                await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert caught.value.code == "rate_limited" and "within the hour" in caught.value.message


class TestSkillsShReasons:
    @pytest.mark.asyncio
    async def test_a_rate_limited_search_says_so(self):
        exc = _http.FetchError("http", status=429, rate_limited=True)
        with patch.object(skillsh, "_sync_fetch_json", _raising(exc)):
            with pytest.raises(SkillProviderError) as caught:
                await skillsh.SkillsShProvider().search("docker")
        assert caught.value.code == "rate_limited"

    @pytest.mark.asyncio
    async def test_a_missing_download_is_not_found(self):
        with patch.object(
            skillsh, "_sync_fetch_json", _raising(_http.FetchError("http", status=404))
        ):
            with pytest.raises(SkillProviderError) as caught:
                await skillsh.SkillsShProvider().fetch_skill_bundle("acme/repo/skill")
        assert caught.value.code == "not_found"

    @pytest.mark.asyncio
    async def test_an_unreachable_host_names_the_status_only(self):
        with patch.object(
            skillsh, "_sync_fetch_json", _raising(_http.FetchError("http", status=503))
        ):
            with pytest.raises(SkillProviderError) as caught:
                await skillsh.SkillsShProvider().search("docker")
        assert caught.value.code == "unavailable" and "HTTP 503" in caught.value.message


class _Prov:
    def __init__(self, behaviour) -> None:  # noqa: ANN001
        self.behaviour = behaviour

    name = "p"
    display_name = "P"

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, *, limit: int = 20) -> list[SkillSearchResult]:
        return self.behaviour()

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        return None


class TestRegistryOutcome:
    @pytest.mark.asyncio
    async def test_the_reason_is_kept_on_the_outcome(self):
        def _fail():
            raise SkillProviderError("not_found", "no such repo")

        reg = ProviderRegistry()
        reg.register(_Prov(_fail))
        response = await reg.search_with_outcomes("q")
        (outcome,) = response.provider_outcomes
        assert (outcome.status, outcome.code, outcome.message) == (
            "error",
            "not_found",
            "no such repo",
        )

    @pytest.mark.asyncio
    async def test_an_empty_answer_is_ok(self):
        reg = ProviderRegistry()
        reg.register(_Prov(list))
        (outcome,) = (await reg.search_with_outcomes("q")).provider_outcomes
        assert (outcome.status, outcome.code, outcome.message) == ("ok", "", "")

    def test_an_unknown_code_is_reported_as_unavailable(self):
        assert SkillProviderError("boom", "x").code == "unavailable"
