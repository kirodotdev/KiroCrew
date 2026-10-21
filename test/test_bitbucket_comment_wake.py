"""Bitbucket PR-level review-comment wake coverage.

Split out of ``test_source_provider_pull_request_monitors.py`` to keep that file
under the 2,000-line cap (see AGENTS.md): these tests cover the one surface the
Bitbucket adapter gained -- digesting ``/pullrequests/{id}/comments`` bodies into
``pr_comment_body_digest`` so an in-place verdict-comment edit wakes the watch.
"""

import json
from urllib.error import HTTPError

from kiro_crew.monitoring import bitbucket_pull_request as bitbucket_module
from kiro_crew.monitoring.bitbucket_pull_request import BitbucketPullRequestProvider
from kiro_crew.monitoring.models import MonitorObservationStatus


def _probe(provider, target: str, **kwargs):
    previous = kwargs.pop("previous_observation", None)
    call_kwargs = dict(kwargs)
    if previous is not None:
        call_kwargs["previous_observations"] = {target: previous}
    results = provider.probe((target,), **call_kwargs)
    return results[target]


def _bitbucket_comment_payloads(comments_page):
    """An OPEN, otherwise-ready PR carrying one unresolved task so it is ACTIONABLE.

    An ACTIONABLE observation is the only one that carries conditions, so the
    unresolved task is what lets the comment-body wake be observed at all.
    """
    return {
        "pull_request": {
            "state": "OPEN",
            "draft": False,
            "source": {"commit": {"hash": "fed"}},
            "participants": [{"role": "REVIEWER", "approved": True, "state": "approved"}],
        },
        "statuses": {"values": [], "next": None},
        "tasks": {"values": [{"state": "OPEN"}], "next": None},
        "conflicts": {"values": [], "next": None},
        "comments": comments_page,
    }


def _bitbucket_comment(raw_body):
    return {"content": {"raw": raw_body}}


def _bitbucket_comment_conditions(result):
    return [
        condition.key
        for condition in result.observation.conditions
        if condition.key.startswith("review_comment_bodies:")
    ]


def _bitbucket_probe(comments_page):
    provider = BitbucketPullRequestProvider(
        fetch=lambda _target, resource: _bitbucket_comment_payloads(comments_page)[resource]
    )
    return _probe(provider, "https://bitbucket.org/acme/widgets/pull-requests/10")


def test_bitbucket_comment_body_change_flips_digest_and_wakes():
    before = _bitbucket_probe(
        {"values": [_bitbucket_comment("codex-ai-review: PASS")], "next": None}
    )
    after = _bitbucket_probe(
        {"values": [_bitbucket_comment("codex-ai-review: CHANGES REQUESTED")], "next": None}
    )

    before_keys = _bitbucket_comment_conditions(before)
    after_keys = _bitbucket_comment_conditions(after)
    assert len(before_keys) == 1
    assert len(after_keys) == 1
    # The digest is inside the condition KEY, and the engine dedupes per key with
    # resets_on=NEVER, so a body change yields a key it has not seen -- which is
    # the wake. (The actionable fingerprint deliberately omits the comment digest,
    # exactly as it does for GitHub; the per-condition key is the wake path.)
    assert before_keys != after_keys
    assert before.canonical["pr_comment_body_digest"] != after.canonical["pr_comment_body_digest"]


def test_bitbucket_in_place_edit_with_unchanged_created_at_flips_digest():
    # Bitbucket carries no created_at on the body digest at all -- the digest is
    # over the body text, so an in-place edit that leaves every timestamp frozen
    # still moves it. This is the case a count or newest-timestamp probe misses.
    original = _bitbucket_probe(
        {
            "values": [
                {
                    "created_on": "2026-01-01T00:00:00Z",
                    "updated_on": "2026-01-01T00:00:00Z",
                    "content": {"raw": "first-principles-review: PASS"},
                }
            ],
            "next": None,
        }
    )
    edited = _bitbucket_probe(
        {
            "values": [
                {
                    "created_on": "2026-01-01T00:00:00Z",
                    "updated_on": "2026-01-01T00:00:00Z",
                    "content": {"raw": "first-principles-review: FAIL"},
                }
            ],
            "next": None,
        }
    )

    assert _bitbucket_comment_conditions(original) != _bitbucket_comment_conditions(edited)


def test_bitbucket_incomplete_comment_read_yields_empty_digest_and_no_wake():
    # A comments page still advertising a `next` at hand-off is an incomplete read.
    result = _bitbucket_probe(
        {
            "values": [_bitbucket_comment("design-review: PASS")],
            "next": "https://api.bitbucket.org/2.0/next-comments",
        }
    )

    assert "pr_comment_body_digest" not in result.canonical
    assert _bitbucket_comment_conditions(result) == []


def test_bitbucket_malformed_comment_fails_the_digest_closed_not_the_probe():
    # A malformed comment item is treated as an incomplete read (empty digest, no
    # wake), never as a provider error -- comment completeness never gates
    # readiness.
    result = _bitbucket_probe({"values": ["not-an-object"], "next": None})

    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert "pr_comment_body_digest" not in result.canonical
    assert _bitbucket_comment_conditions(result) == []


def test_bitbucket_unchanged_comments_keep_the_same_digest_and_no_new_wake():
    page = {
        "values": [
            _bitbucket_comment("claude-ai-review: PASS"),
            _bitbucket_comment("a human remark"),
        ],
        "next": None,
    }
    first = _bitbucket_probe(page)
    second = _bitbucket_probe(page)

    first_keys = _bitbucket_comment_conditions(first)
    assert first_keys == _bitbucket_comment_conditions(second)
    assert first.observation.fingerprint == second.observation.fingerprint
    # Order on the page must not move the digest: fingerprints are sorted first.
    reordered = _bitbucket_probe(
        {
            "values": [
                _bitbucket_comment("a human remark"),
                _bitbucket_comment("claude-ai-review: PASS"),
            ],
            "next": None,
        }
    )
    assert _bitbucket_comment_conditions(reordered) == first_keys


def test_bitbucket_empty_and_bodiless_comments_emit_no_condition():
    result = _bitbucket_probe(
        {
            "values": [
                {"content": {"raw": ""}},
                {"content": None},
                {"id": 7},
            ],
            "next": None,
        }
    )

    assert "pr_comment_body_digest" not in result.canonical
    assert _bitbucket_comment_conditions(result) == []


def test_bitbucket_terminal_state_reads_no_comments():
    calls: list[str] = []

    def fetch(_target, resource):
        calls.append(resource)
        if resource != "pull_request":
            raise AssertionError("supplemental read on a terminal PR")
        return {
            "state": "MERGED",
            "draft": False,
            "source": {"commit": {"hash": "fed"}},
            "participants": [],
        }

    result = _probe(
        BitbucketPullRequestProvider(fetch=fetch),
        "https://bitbucket.org/acme/widgets/pull-requests/10",
    )

    assert "comments" not in calls
    assert "pr_comment_body_digest" not in result.canonical


def test_bitbucket_https_comment_fetch_follows_next_across_pages(monkeypatch):
    # Real-path pagination: the opener serves two pages, the follower accumulates
    # both bodies and clears `next` only once the whole collection was read.
    pages = iter(
        [
            {
                "values": [{"content": {"raw": "page-one verdict"}}],
                "next": "https://api.bitbucket.org/2.0/comments?page=2",
            },
            {
                "values": [{"content": {"raw": "page-two reply"}}],
                "next": None,
            },
        ]
    )
    urls: list[str] = []

    class Response:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(self._body).encode("utf-8")

    class Opener:
        def open(self, request, *, timeout):
            urls.append(request.full_url)
            return Response(next(pages))

    monkeypatch.setattr(bitbucket_module, "build_opener", lambda _redirect: Opener())
    monkeypatch.setattr(
        bitbucket_module,
        "_audit_bitbucket",
        lambda outcome, *, critical=False, reason="": None,
    )
    target = bitbucket_module.parse_bitbucket_pull_request_target(
        "https://bitbucket.org/acme/widgets/pull-requests/10"
    )

    page = BitbucketPullRequestProvider._fetch_https(target, "comments", {})

    assert page == {
        "values": [
            {"content": {"raw": "page-one verdict"}},
            {"content": {"raw": "page-two reply"}},
        ],
        "next": None,
    }
    assert len(urls) == 2
    assert urls[0].endswith("/comments?pagelen=100")
    assert urls[1] == "https://api.bitbucket.org/2.0/comments?page=2"


def test_bitbucket_https_comment_fetch_caps_pages_and_reports_incomplete(monkeypatch):
    # A collection that never stops advertising a DISTINCT `next` is truncated at
    # the page cap and reported incomplete, so the digest fails closed.
    counter = {"n": 0}

    class EndlessPage:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            counter["n"] += 1
            return json.dumps(
                {
                    "values": [{"content": {"raw": f"page-{counter['n']}"}}],
                    "next": f"https://api.bitbucket.org/2.0/comments?page={counter['n'] + 1}",
                }
            ).encode("utf-8")

    class Opener:
        def open(self, _request, *, timeout):
            return EndlessPage()

    monkeypatch.setattr(bitbucket_module, "build_opener", lambda _redirect: Opener())
    monkeypatch.setattr(
        bitbucket_module,
        "_audit_bitbucket",
        lambda outcome, *, critical=False, reason="": None,
    )
    target = bitbucket_module.parse_bitbucket_pull_request_target(
        "https://bitbucket.org/acme/widgets/pull-requests/10"
    )

    page = BitbucketPullRequestProvider._fetch_https(target, "comments", {})

    assert page["next"] == "truncated"
    values, complete = bitbucket_module._page(page)
    assert complete is False
    # Exactly the page cap's worth of pages were read, each contributing one row.
    assert len(values) == bitbucket_module._COMMENT_MAX_PAGES
    assert counter["n"] == bitbucket_module._COMMENT_MAX_PAGES


def test_bitbucket_https_comment_fetch_stops_on_a_repeated_next_link(monkeypatch):
    # A provider that returns the SAME `next` link is a non-advancing read: stop
    # and report it incomplete rather than loop until the page cap.
    class RepeatPage:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "values": [{"content": {"raw": "stuck"}}],
                    "next": "https://api.bitbucket.org/2.0/comments?page=same",
                }
            ).encode("utf-8")

    opens = {"n": 0}

    class Opener:
        def open(self, _request, *, timeout):
            opens["n"] += 1
            return RepeatPage()

    monkeypatch.setattr(bitbucket_module, "build_opener", lambda _redirect: Opener())
    monkeypatch.setattr(
        bitbucket_module,
        "_audit_bitbucket",
        lambda outcome, *, critical=False, reason="": None,
    )
    target = bitbucket_module.parse_bitbucket_pull_request_target(
        "https://bitbucket.org/acme/widgets/pull-requests/10"
    )

    page = BitbucketPullRequestProvider._fetch_https(target, "comments", {})

    # First page plus the one repeated-link page, then the guard stops the walk.
    assert opens["n"] == 2
    _values, complete = bitbucket_module._page(page)
    assert complete is False


def test_bitbucket_https_comment_fetch_refuses_an_off_host_next_link(monkeypatch):
    # Security: the owner credential rides every request, so a provider `next`
    # pointing off-host must NOT be opened. The walk stops before the second
    # request and the read is reported incomplete (digest fails closed).
    opened: list[str] = []

    class Page:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "values": [{"content": {"raw": "on-host page"}}],
                    "next": "https://outside.example/collect",
                }
            ).encode("utf-8")

    class Opener:
        def open(self, request, *, timeout):
            opened.append(request.full_url)
            return Page()

    monkeypatch.setattr(bitbucket_module, "build_opener", lambda _redirect: Opener())
    monkeypatch.setattr(
        bitbucket_module,
        "_audit_bitbucket",
        lambda outcome, *, critical=False, reason="": None,
    )
    target = bitbucket_module.parse_bitbucket_pull_request_target(
        "https://bitbucket.org/acme/widgets/pull-requests/10"
    )

    page = BitbucketPullRequestProvider._fetch_https(
        target,
        "comments",
        {"BITBUCKET_EMAIL": "user@example.com", "BITBUCKET_API_TOKEN": "secret"},
    )

    # Only the first (host-pinned) request was made; the off-host link was refused.
    assert len(opened) == 1
    assert opened[0].startswith("https://api.bitbucket.org/")
    assert not any("outside.example" in url for url in opened)
    _values, complete = bitbucket_module._page(page)
    assert complete is False


def test_bitbucket_pinned_to_api_host_rejects_credential_leaking_urls():
    assert bitbucket_module._pinned_to_api_host(
        "https://api.bitbucket.org/2.0/repositories/a/b/pullrequests/1/comments?page=2"
    )
    for bad in (
        "http://api.bitbucket.org/2.0/x",  # not https
        "https://outside.example/collect",  # wrong host
        "https://user:pw@api.bitbucket.org/2.0/x",  # embedded userinfo
        "https://api.bitbucket.org:8443/2.0/x",  # explicit port
    ):
        assert not bitbucket_module._pinned_to_api_host(bad)


def test_bitbucket_failed_comment_read_keeps_readiness_facts_and_no_wake():
    # A comment-page read that raises (byte limit, 429, timeout, malformed) must
    # fail ONLY the digest closed -- the checks/tasks/conflicts facts still stand
    # and the probe is not a provider error. Here the PR is otherwise review-ready.
    def fetch(_target, resource):
        if resource == "comments":
            raise HTTPError("https://api.bitbucket.org", 429, "limited", {}, None)
        return {
            "pull_request": {
                "state": "OPEN",
                "draft": False,
                "source": {"commit": {"hash": "fed"}},
                "participants": [{"role": "REVIEWER", "approved": True, "state": "approved"}],
            },
            "statuses": {"values": [], "next": None},
            "tasks": {"values": [], "next": None},
            "conflicts": {"values": [], "next": None},
        }[resource]

    result = _probe(
        BitbucketPullRequestProvider(fetch=fetch),
        "https://bitbucket.org/acme/widgets/pull-requests/10",
    )

    # Not a provider error -- readiness facts survived the failed comment read.
    assert result.observation.status is MonitorObservationStatus.SUCCESS
    assert result.canonical["review_decision"] == "approved"
    assert "pr_comment_body_digest" not in result.canonical
