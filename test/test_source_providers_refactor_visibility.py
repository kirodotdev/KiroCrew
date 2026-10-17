"""The repository-visibility gate behind public-repo chip status, pinned at the handler.

A non-owner may see a chip's status only for a repository positively known to be
public. These cases pin every answer that gate gives -- per provider, per failure,
across a TTL, and across a forced public-to-private flip -- through the handler's
own names, so they hold whichever module owns the cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from kiro_crew.dashboard.handlers import source_providers as sp

_GH = "https://github.com/acme/repo/pull/7"
_GH_KEY = "github|github.com|acme|repo"
_GL = "https://gitlab.com/grp/sub/proj/-/merge_requests/4"


@pytest.fixture(autouse=True)
def _isolated_visibility_state():
    """Start and end every case with empty visibility and chip state."""

    def reset() -> None:
        sp._visibility_cache.clear()
        sp._visibility_inflight.clear()
        sp._visibility_force_gen.clear()
        sp._check_cache.clear()
        sp._check_generations.clear()
        sp._check_inflight.clear()
        sp._check_forced_at.clear()
        sp._check_force_pending.clear()
        sp._check_flap.clear()
        sp._check_flap_damped.clear()
        for task in list(sp._VISIBILITY_TASKS):
            if not task.get_loop().is_closed():
                task.cancel()
        sp._VISIBILITY_TASKS.clear()
        handle = sp._check_update_handle
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.cancel()
        sp._check_update_handle = None
        sp._check_update_callbacks.clear()

    reset()
    yield
    reset()


def _run_json_answering(monkeypatch, answer):
    calls: list[tuple[str, ...]] = []

    async def fake(*argv: str, **kwargs: object) -> object:
        calls.append(argv)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(sp, "_run_json", fake)
    return calls


def test_an_unknown_repository_is_not_public() -> None:
    assert sp.is_repo_public("https://example.com/not/a/pr") is None
    assert sp.is_repo_public("https://acme.atlassian.net/browse/PROJ-1") is None
    assert sp.is_repo_public(_GH) is None


def test_a_known_answer_is_served_until_its_ttl_lapses(monkeypatch) -> None:
    sp._visibility_cache[_GH_KEY] = (time.monotonic(), True)
    assert sp.is_repo_public(_GH) is True

    sp._visibility_cache[_GH_KEY] = (time.monotonic() - sp._VISIBILITY_TTL_SECS - 1, True)
    assert sp.is_repo_public(_GH) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "public"),
    [
        ({"isPrivate": True, "visibility": "PUBLIC"}, False),
        ({"isPrivate": False, "visibility": "PUBLIC"}, True),
        ({"isPrivate": False, "visibility": "internal"}, False),
        ({"isPrivate": False}, None),
        (["not", "a", "dict"], None),
        (sp.SourceProviderError("no auth"), None),
    ],
)
async def test_github_visibility_requires_exactly_public(monkeypatch, answer, public) -> None:
    calls = _run_json_answering(monkeypatch, answer)
    ref = sp.parse_source_url(_GH)

    assert await sp._fetch_repo_visibility(ref) is public
    assert calls == [("gh", "repo", "view", "acme/repo", "--json", "isPrivate,visibility")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "public"),
    [
        (
            {
                "visibility": "public",
                "merge_requests_access_level": "enabled",
                "builds_access_level": "enabled",
                "public_jobs": True,
            },
            True,
        ),
        (
            {
                "visibility": "public",
                "merge_requests_access_level": "private",
                "builds_access_level": "enabled",
                "public_jobs": True,
            },
            False,
        ),
        (
            {
                "visibility": "public",
                "merge_requests_access_level": "enabled",
                "builds_access_level": "enabled",
                "public_jobs": False,
            },
            False,
        ),
        ({"visibility": "internal"}, False),
        ({"no": "visibility"}, None),
    ],
)
async def test_gitlab_visibility_requires_public_features_too(monkeypatch, answer, public) -> None:
    calls = _run_json_answering(monkeypatch, answer)
    ref = sp.parse_source_url(_GL)

    assert await sp._fetch_repo_visibility(ref) is public
    assert calls == [("glab", "api", "projects/grp%2Fsub%2Fproj")]


@pytest.mark.asyncio
async def test_a_positive_read_is_stored_and_announces_the_flip(monkeypatch) -> None:
    _run_json_answering(monkeypatch, {"isPrivate": False, "visibility": "public"})
    updates: list[int] = []

    await sp._refresh_repo_visibility(sp.parse_source_url(_GH), lambda: updates.append(1))
    await asyncio.sleep(sp._CHECK_UPDATE_DEBOUNCE_SECS * 2)

    assert sp._visibility_cache[_GH_KEY][1] is True
    assert updates == [1]


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_last_answer_and_its_age(monkeypatch) -> None:
    stamped = time.monotonic() - 5
    sp._visibility_cache[_GH_KEY] = (stamped, True)
    _run_json_answering(monkeypatch, sp.SourceProviderError("down"))

    await sp._refresh_repo_visibility(sp.parse_source_url(_GH))

    assert sp._visibility_cache[_GH_KEY] == (stamped, True)


@pytest.mark.asyncio
async def test_a_failed_first_read_is_recorded_as_unknown(monkeypatch) -> None:
    _run_json_answering(monkeypatch, sp.SourceProviderError("down"))

    await sp._refresh_repo_visibility(sp.parse_source_url(_GH))

    assert sp._visibility_cache[_GH_KEY][1] is None
    assert sp.is_repo_public(_GH) is None


@pytest.mark.asyncio
async def test_a_positive_read_that_raced_a_forced_flip_is_discarded(monkeypatch) -> None:
    async def read_then_flip(*argv: str, **kwargs: object) -> object:
        sp._visibility_force_gen[_GH_KEY] = sp._visibility_force_gen.get(_GH_KEY, 0) + 1
        return {"isPrivate": False, "visibility": "public"}

    monkeypatch.setattr(sp, "_run_json", read_then_flip)

    await sp._refresh_repo_visibility(sp.parse_source_url(_GH))

    assert sp._visibility_cache[_GH_KEY][1] is None


@pytest.mark.asyncio
async def test_scheduling_reads_each_repository_once_per_ttl(monkeypatch) -> None:
    reads: list[str] = []

    async def fake_refresh(ref, on_update=None, *, prev_public_override=None) -> None:
        reads.append(sp._visibility_key(ref))
        sp._visibility_inflight.discard(sp._visibility_key(ref))

    monkeypatch.setattr(sp, "_refresh_repo_visibility", fake_refresh)
    other_pr_same_repo = "https://github.com/acme/repo/pull/8"
    sp._visibility_cache["github|github.com|fresh|repo"] = (time.monotonic(), True)

    sp.schedule_visibility_refresh(
        [_GH, other_pr_same_repo, "https://github.com/fresh/repo/pull/1", "not a url"]
    )
    await asyncio.gather(*list(sp._VISIBILITY_TASKS))

    assert reads == [_GH_KEY]


@pytest.mark.asyncio
async def test_a_forced_refresh_fails_a_public_answer_closed_at_once(monkeypatch) -> None:
    started: list[bool | None] = []

    async def fake_refresh(ref, on_update=None, *, prev_public_override=None) -> None:
        started.append(prev_public_override)

    monkeypatch.setattr(sp, "_refresh_repo_visibility", fake_refresh)
    sp._visibility_cache[_GH_KEY] = (time.monotonic(), True)

    sp.schedule_visibility_refresh([_GH], force=True)

    assert sp.is_repo_public(_GH) is None
    assert sp._visibility_force_gen[_GH_KEY] == 1
    await asyncio.gather(*list(sp._VISIBILITY_TASKS))
    assert started == [True]


@pytest.mark.asyncio
async def test_an_inflight_repository_is_not_read_twice(monkeypatch) -> None:
    started: list[str] = []

    async def fake_refresh(ref, on_update=None, *, prev_public_override=None) -> None:
        started.append(sp._visibility_key(ref))

    monkeypatch.setattr(sp, "_refresh_repo_visibility", fake_refresh)
    sp._visibility_inflight.add(_GH_KEY)

    sp.schedule_visibility_refresh([_GH], force=True)

    assert list(sp._VISIBILITY_TASKS) == []
    assert sp._visibility_force_gen[_GH_KEY] == 1
    assert started == []


def test_the_visibility_cache_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(sp, "_VISIBILITY_CACHE_MAX", 2)
    for index in range(4):
        sp._visibility_cache[f"k{index}"] = (float(index), True)

    sp._trim_visibility_cache()

    assert sorted(sp._visibility_cache) == ["k2", "k3"]


def test_one_trim_bounds_every_per_url_chip_map(monkeypatch) -> None:
    monkeypatch.setattr(sp, "_CHECK_CACHE_MAX", 1)
    sp._check_cache.update({"a": (1.0, None), "b": (2.0, None)})
    sp._check_generations.update({"a": 1, "b": 1, "c": 1})
    sp._check_forced_at.update({"a": 1.0, "b": 2.0})
    sp._check_flap.update({"a": (("", ""), 1), "b": (("", ""), 1)})
    sp._check_flap_damped.update({"a", "b"})
    sp._check_force_pending.update({"a", "b"})

    sp._trim_check_cache()

    assert list(sp._check_cache) == ["b"]
    assert list(sp._check_generations) == ["b"]
    assert list(sp._check_forced_at) == ["b"]
    assert list(sp._check_flap) == ["b"]
    assert sp._check_flap_damped == {"b"}
    assert sp._check_force_pending == set()
