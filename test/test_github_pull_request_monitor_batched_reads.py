"""Batched reads, the plural probe boundary, the REST fallback and the PR comment digest.

Pins the batched read, one GraphQL document per evidence kind for every subject on one host
(the request count, aliases and bound variables, chunking, partial failures, one host per
query), a rollup read against the head it was asked about, the plural probe boundary with
the shadow probe's handling of a subset or an untyped answer and of a stalled watch, the
REST fallback that answers a GraphQL rate limit, and the PR-level comment digest with its
fixed-width retention. The canonical observation and the check-rollup collapse live in
``test_github_pull_request_monitor.py``, readiness classification in
``test_github_pull_request_monitor_readiness.py``, provider failure categories and the
shadow probe's persistence in ``test_github_pull_request_monitor_errors_and_shadow.py`` and
the shared fixtures in ``github_pull_request_monitor_helpers.py``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from copy import deepcopy
from unittest import mock

import pytest
from github_pull_request_monitor_helpers import (
    _HEAD,
    _check_run,
    _comments,
    _completed,
    _CompletedRunner,
    _core_and_rollup,
    _envelope,
    _pr_node,
    _primary,
    _probe_one,
    _provider,
    _rollup_node,
    _threads,
    _threads_node,
)

from kiro_crew.monitoring import github_pull_request
from kiro_crew.monitoring.github_pull_request import (
    _MAX_SUBJECTS_PER_QUERY,
    GitHubPullRequestProbeResult,
    GitHubPullRequestProvider,
    GitHubPullRequestTarget,
    parse_github_pull_request_target,
)
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_CADENCE_SECS,
    DEFAULT_MONITOR_PROVIDER_ERRORS,
    DEFAULT_MONITOR_STALL_TICKS,
    MONITOR_REVISION_KEY_SPACE,
    MONITOR_STOP_VERDICT_STALL,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorProbe,
    MonitorProbeResult,
    MonitorState,
    ProviderErrorKind,
)
from kiro_crew.monitoring.pull_request import (
    PullRequestCheck,
    PullRequestFacts,
    classify_pull_request_facts,
)
from kiro_crew.monitoring.shadow import run_shadow_probe


def _alias_error(
    index: int, *, type_name: str = "NOT_FOUND", message: str = ""
) -> dict[str, object]:
    """One reported error naming exactly the subject at *index* in the batch."""
    return {"type": type_name, "path": [f"s{index}", "pullRequest"], "message": message}


def _target_on_another_host(
    target: GitHubPullRequestTarget, host: str = "github.example.com"
) -> GitHubPullRequestTarget:
    """The same subject on a second host, which the target type refuses to build.

    ``GitHubPullRequestTarget`` validates its host on construction and accepts only
    ``github.com``, so a second host cannot be reached through the type or through
    ``parse_github_pull_request_target``. The provider still checks that a chunk
    names one host, and the state that check exists for has to be assembled around
    the validator rather than through it. Every other field stays as the real
    parser produced it.
    """
    twin = deepcopy(target)
    object.__setattr__(twin, "host", host)
    return twin


def _batched_reads(*payloads: Mapping[str, object]) -> list[dict[str, object]]:
    """The responses one batched tick reads for several subjects.

    One document per evidence kind, each carrying one alias per subject in the
    order they were asked. A per-subject SEQUENCE of single-alias responses would
    also let a batched probe finish -- every alias past the first reads as absent --
    so a batch test built that way passes while proving nothing.

    Only live subjects appear in the supplemental documents, because only live
    subjects are asked about.
    """
    live = [p for p in payloads if p.get("state") not in {"MERGED", "CLOSED"}]
    responses = [_envelope(*(_pr_node(payload) for payload in payloads))]
    if live:
        responses.append(_envelope(*(_rollup_node(payload) for payload in live)))
        responses.append(_envelope(*(_threads_node() for _ in live)))
    return responses


class TestBatchedReads:
    """Subjects sharing a host and a credential share each request.

    The spec's rule is that a probe which CAN batch MUST, and GitHub's GraphQL API
    answers for many pull requests in one document. So the cost of a tick is what
    these tests are about: a per-subject loop and a batch are indistinguishable from
    the results alone, and only the request count tells them apart.
    """

    @staticmethod
    def _urls(count: int, *, repo: str = "repo") -> tuple[str, ...]:
        return tuple(
            f"https://github.com/owner/{repo}/pull/{number}" for number in range(1, count + 1)
        )

    def test_one_unreadable_subject_leaves_every_other_verdict_intact(self) -> None:
        """The rule that matters: a partial failure degrades only what it covers.

        GitHub answers a partial failure by populating the readable aliases and
        naming the rest in ``errors[].path`` -- and ``gh`` exits NON-ZERO while
        still writing that payload. A probe that read the exit code as the verdict
        would fail all five subjects for one bad one, which is why this asserts the
        four survivors' facts rather than only the failure.
        """
        urls = self._urls(5)
        readable = [_primary(number=number) for number in (1, 2, 4, 5)]
        primary = _envelope(
            _pr_node(readable[0]),
            _pr_node(readable[1]),
            None,
            _pr_node(readable[2]),
            _pr_node(readable[3]),
            errors=[_alias_error(2)],
        )
        live = _envelope(*(_rollup_node(payload) for payload in readable))
        threads = _envelope(*(_threads_node() for _ in readable))
        runner = _CompletedRunner(
            [
                _completed(primary, returncode=1, stderr="gh: Could not resolve to a PullRequest"),
                _completed(live),
                _completed(threads),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(urls)

        assert set(results) == set(urls)
        unreadable = results[urls[2]]
        assert unreadable.observation.status is MonitorObservationStatus.PROVIDER_ERROR
        assert unreadable.observation.provider_error is ProviderErrorKind.NOT_FOUND
        assert unreadable.observation.reason_code == "provider_not_found"
        assert unreadable.canonical == {}
        for index in (0, 1, 3, 4):
            survivor = results[urls[index]]
            assert survivor.observation.provider_error is None
            assert survivor.observation.status is MonitorObservationStatus.SUCCESS
            assert survivor.observation.reason_code == "review_ready"
            assert survivor.canonical["head_revision"] == _HEAD
            assert survivor.canonical["checks"]["passed"] == ["CI / test", "lint"]
            assert survivor.canonical["checks_complete"] is True
            assert survivor.canonical["review_threads_complete"] is True
        # The unreadable subject is also dropped from the supplemental documents,
        # so it cannot consume an alias the survivors' evidence is read from.
        assert runner.calls[1][4].count("repository(") == 4

    def test_a_tick_costs_four_requests_whatever_the_subject_count_is(self) -> None:
        """Ten subjects were thirty invocations before this; the count is the point.

        Four documents now: the primary read, the check rollup, the review-thread
        read and the PR-level comment read, each batched over every subject.
        """
        payloads = [_primary(number=number) for number in range(1, 11)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(10))

        assert len(results) == 10
        assert all(result.observation.provider_error is None for result in results.values())
        assert len(runner.calls) == 4
        assert all(call[1] == "api" and call[2] == "graphql" for call in runner.calls)

    def test_every_subject_gets_its_own_alias_and_bound_variables(self) -> None:
        """No part of a subject reaches the document text.

        The names below are chosen so they cannot occur in GraphQL syntax: finding
        either one inside the document would mean a subject was interpolated into
        it rather than bound as a variable.
        """
        payloads = [_primary(number=1), _primary(number=2)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        provider.probe(
            (
                "https://github.com/owner-alpha/repo-alpha/pull/1",
                "https://github.com/owner-beta/repo-beta/pull/2",
            )
        )

        document = runner.calls[0][4]
        assert "s0:repository(owner:$o0,name:$r0)" in document
        assert "s1:repository(owner:$o1,name:$r1)" in document
        for interpolated in ("owner-alpha", "repo-alpha", "owner-beta", "repo-beta"):
            assert interpolated not in document
        assert runner.calls[0][5:] == [
            "-f",
            "o0=owner-alpha",
            "-f",
            "r0=repo-alpha",
            "-F",
            "n0=1",
            "-f",
            "o1=owner-beta",
            "-f",
            "r1=repo-beta",
            "-F",
            "n1=2",
        ]

    def test_subjects_in_different_repositories_share_one_query(self) -> None:
        """One call is one batch, so two repositories on one host do not split it."""
        payloads = [_primary(number=1), _primary(number=2)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(
            (
                "https://github.com/owner/first/pull/1",
                "https://github.com/other/second/pull/2",
            )
        )

        assert len(runner.calls) == 4
        assert all(result.observation.provider_error is None for result in results.values())
        assert runner.calls[0][5:11] == ["-f", "o0=owner", "-f", "r0=first", "-F", "n0=1"]
        assert runner.calls[0][11:] == ["-f", "o1=other", "-f", "r1=second", "-F", "n1=2"]

    def test_more_subjects_than_the_document_bound_are_split_not_dropped(self) -> None:
        """A document that grows without a bound is a request that times out."""
        count = _MAX_SUBJECTS_PER_QUERY + 1
        payloads = [_primary(number=number) for number in range(1, count + 1)]
        first_chunk = payloads[:_MAX_SUBJECTS_PER_QUERY]
        second_chunk = payloads[_MAX_SUBJECTS_PER_QUERY:]
        runner = _CompletedRunner(
            [
                _completed(payload)
                for payload in _batched_reads(*first_chunk) + _batched_reads(*second_chunk)
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(count))

        assert len(results) == count
        assert all(result.observation.provider_error is None for result in results.values())
        assert len(runner.calls) == 8
        assert runner.calls[0][4].count("repository(") == _MAX_SUBJECTS_PER_QUERY
        assert runner.calls[4][4].count("repository(") == 1

    def test_an_error_naming_no_subject_is_charged_to_all_of_them(self) -> None:
        """A document-level failure means none of them was read.

        Dropping an unattributable error would report a verdict from a response
        that carried none, so it fails the whole batch rather than silently
        passing.

        Instanced on ``INTERNAL`` rather than a rate limit because a rate limit is
        the ONE kind that is re-read on the REST bucket before it is charged
        (``TestRestFallbackOnRateLimit``). A property that holds for every failure
        has to be shown on a failure the fallback does not answer, or the test
        measures the fallback instead of the property.
        """
        primary = _envelope(
            None,
            None,
            errors=[{"type": "INTERNAL", "message": "something went wrong"}],
        )
        runner = _CompletedRunner(
            [_completed(primary, returncode=1, stderr="HTTP 503: unavailable")]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(2))

        assert len(results) == 2
        for result in results.values():
            assert result.observation.provider_error is ProviderErrorKind.TRANSIENT
            assert result.observation.reason_code == "provider_transient"
        assert len(runner.calls) == 1

    def test_one_document_advances_subjects_on_different_pages(self) -> None:
        """Each subject carries its own cursor, so a batch is not held to one page."""
        payloads = [_primary(number=1), _primary(number=2)]
        first_page = _envelope(
            _threads_node([{"isResolved": False}], has_next=True, cursor="cursor-1"),
            _threads_node([{"isResolved": True}]),
        )
        # Only the subject that advertised another page is asked again.
        second_page = _envelope(_threads_node([{"isResolved": False}]))
        runner = _CompletedRunner(
            [
                _completed(_envelope(*(_pr_node(payload) for payload in payloads))),
                _completed(_envelope(*(_rollup_node(payload) for payload in payloads))),
                _completed(first_page),
                _completed(second_page),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(2))

        paginated = results["https://github.com/owner/repo/pull/1"]
        settled = results["https://github.com/owner/repo/pull/2"]
        assert paginated.canonical["unresolved_review_threads"] == 2
        assert paginated.canonical["review_threads_complete"] is True
        assert settled.canonical["unresolved_review_threads"] == 0
        assert settled.canonical["review_threads_complete"] is True
        assert len(runner.calls) == 5
        assert "c0=cursor-1" in runner.calls[3]
        assert runner.calls[3][4].count("repository(") == 1

    def test_a_terminal_subject_is_left_out_of_the_supplemental_documents(self) -> None:
        """A merged subject issues neither supplemental request, batched or not."""
        payloads = [_primary(number=1, state="MERGED"), _primary(number=2)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(2))

        merged = results["https://github.com/owner/repo/pull/1"]
        assert merged.observation.status is MonitorObservationStatus.SUCCESS
        assert merged.observation.reason_code == "pull_request_merged"
        assert results["https://github.com/owner/repo/pull/2"].observation.provider_error is None
        assert len(runner.calls) == 4
        assert runner.calls[0][4].count("repository(") == 2
        assert runner.calls[1][4].count("repository(") == 1
        assert runner.calls[2][4].count("repository(") == 1

    def test_an_unparseable_subject_never_reaches_a_query(self) -> None:
        """A subject that is not a pull-request URL cannot be asked about at all."""
        payloads = [_primary(number=1)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(
            ("https://github.com/owner/repo/pull/1", "https://example.com/not-a-pull-request")
        )

        assert results["https://example.com/not-a-pull-request"].observation.reason_code == (
            "provider_malformed_response"
        )
        assert results["https://github.com/owner/repo/pull/1"].observation.provider_error is None
        assert runner.calls[0][4].count("repository(") == 1

    def test_a_repeated_subject_is_asked_about_once(self) -> None:
        """The result mapping is keyed by subject, so a duplicate cannot cost a read."""
        payloads = [_primary(number=1)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        url = "https://github.com/owner/repo/pull/1"

        results = provider.probe((url, url))

        assert list(results) == [url]
        assert runner.calls[0][4].count("repository(") == 1

    def test_refused_credentials_audit_the_query_once_not_each_subject(self) -> None:
        """The query that was not allowed to run is what the refusal records."""
        runner = _CompletedRunner([])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        denials: list[str] = []

        with mock.patch.object(
            github_pull_request,
            "audit_provider_cli_denied",
            side_effect=denials.append,
        ):
            results = provider.probe(self._urls(3), use_owner_credentials=False)

        assert denials == ["gh"]
        assert len(results) == 3
        for result in results.values():
            assert result.observation.provider_error is ProviderErrorKind.AUTHORIZATION
            assert result.observation.reason_code == "provider_authorization"
        assert runner.calls == []

    def test_a_chunk_naming_one_host_reports_it_and_two_hosts_report_nothing(self) -> None:
        """The check that lets a chunk become a query, exercised on both answers."""
        here = parse_github_pull_request_target("https://github.com/owner/repo/pull/1")
        elsewhere = _target_on_another_host(here)

        one = github_pull_request._shared_host(
            (
                github_pull_request._BatchSubject("a", here),
                github_pull_request._BatchSubject("b", here),
            )
        )
        two = github_pull_request._shared_host(
            (
                github_pull_request._BatchSubject("a", here),
                github_pull_request._BatchSubject("b", elsewhere),
            )
        )

        assert one == "github.com"
        assert two is None

    def test_a_chunk_naming_two_hosts_is_refused_rather_than_pinned_to_one(self) -> None:
        """A query carries one token, so it must never span two hosts.

        The refusal is the whole query, because the query is what carries the
        identity -- reading the second host's subject under the first host's token
        is the harm. Nothing can reach this state today, which is why the check
        exists rather than a per-host grouping pass: a check can be exercised, and
        this is where it is.
        """
        runner = _CompletedRunner([])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        here = parse_github_pull_request_target("https://github.com/owner/repo/pull/1")
        elsewhere = _target_on_another_host(
            parse_github_pull_request_target("https://github.com/owner/repo/pull/2")
        )
        admitted = {"https://github.com/owner/repo/pull/1": here, "elsewhere": elsewhere}

        with mock.patch.object(
            github_pull_request,
            "parse_github_pull_request_target",
            side_effect=lambda raw: admitted[raw],
        ):
            results = provider.probe(tuple(admitted))

        assert runner.calls == []
        assert set(results) == set(admitted)
        for result in results.values():
            assert result.observation.provider_error is ProviderErrorKind.SETUP
            assert result.observation.reason_code == "provider_setup"


class TestRollupDescribesTheHeadItWasAskedAbout:
    """A rollup for another commit is incomplete evidence, never another head's checks.

    One document reports the head field and the commit the rollup hangs off, so a
    disagreement between them is visible here where the previous per-call read could
    only compare the head field across two responses.
    """

    URL = "https://github.com/owner/repo/pull/123"

    def test_a_rollup_hanging_off_another_commit_is_incomplete(self) -> None:
        payload = _primary(statusCheckRollup=[_check_run(conclusion="FAILURE")])
        runner = _CompletedRunner(
            [
                _completed(_envelope(_pr_node(payload))),
                _completed(_envelope(_rollup_node(payload, commit_oid="f" * 40))),
                _completed(_threads()),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        result = _probe_one(provider, self.URL)

        assert result.response is not None
        assert result.canonical["head_revision"] == _HEAD
        assert result.canonical["checks"]["failed"] == []
        assert result.canonical["checks_complete"] is False
        assert result.observation.status is MonitorObservationStatus.PENDING
        assert result.observation.supplemental_provider_error is ProviderErrorKind.TRANSIENT

    def test_an_unreported_head_is_judged_on_the_head_field_alone(self) -> None:
        """A subject whose head GitHub did not report keeps the earlier comparison.

        With no head to compare against, the rollup's commit says nothing about
        whether the page is current, so it cannot be what makes the evidence
        incomplete.
        """
        payload = _primary(headRefOid="", statusCheckRollup=[_check_run(conclusion="FAILURE")])
        runner = _CompletedRunner(
            [
                _completed(_envelope(_pr_node(payload))),
                _completed(_envelope(_rollup_node(payload, commit_oid="f" * 40))),
                _completed(_threads()),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        result = _probe_one(provider, self.URL)

        assert result.canonical["head_revision"] == ""
        assert result.canonical["checks"]["failed"] == ["CI / test"]
        assert result.canonical["checks_complete"] is True
        assert result.observation.supplemental_provider_error is None


class TestPluralProbeBoundary:
    """The probe boundary's arity and keying, independent of what GitHub derives.

    These are the tests that would have to change if the signature were made
    plural later instead of now, which is why it is plural now.
    """

    def test_several_subjects_yield_one_result_each_keyed_as_passed(self) -> None:
        # The response's own number is validated against the requested target, so
        # each subject needs an alias that answers for ITS pull request.
        runner = _CompletedRunner(
            [
                _completed(payload)
                for payload in _batched_reads(
                    _primary(number=1),
                    _primary(number=2, statusCheckRollup=[_check_run(conclusion="FAILURE")]),
                )
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        first_url = "https://github.com/owner/repo/pull/1"
        second_url = "https://github.com/owner/repo/pull/2"

        results = provider.probe((first_url, second_url))

        assert set(results) == {first_url, second_url}
        # Both are real observations, not one verdict beside an absent alias.
        assert results[first_url].observation.provider_error is None
        assert results[second_url].observation.provider_error is None
        assert results[first_url].canonical["checks"]["failed"] == []
        assert results[second_url].canonical["checks"]["failed"] == ["CI / test"]
        assert (
            results[first_url].observation.fingerprint
            != results[second_url].observation.fingerprint
        )

    def test_the_mapping_is_keyed_by_the_subject_as_passed_not_a_derived_identity(
        self,
    ) -> None:
        """A caller can only look up what it asked for.

        The canonical facts carry GitHub's own normalized identity
        (``github.com/owner/repo#123``), which is NOT the URL the caller handed
        over. Keying by the derived form would make the mapping unreadable to the
        caller that built the request.
        """
        core, rollup = _core_and_rollup()
        runner = _CompletedRunner(
            [_completed(core), _completed(rollup), _completed(_threads()), _completed(_comments())]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        url = "https://github.com/owner/repo/pull/123"

        results = provider.probe((url,))

        assert list(results) == [url]
        assert results[url].canonical["target"] != url

    def test_each_subject_sees_only_its_own_previous_observation(self) -> None:
        """A head carried against the wrong subject would fake a changed head."""
        runner = _CompletedRunner(
            [
                _completed(payload)
                for payload in _batched_reads(_primary(number=1), _primary(number=2))
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        changed = "https://github.com/owner/repo/pull/1"
        unchanged = "https://github.com/owner/repo/pull/2"

        results = provider.probe(
            (changed, unchanged),
            previous_observations={changed: {"head_revision": "a-different-head"}},
        )

        assert results[changed].observation.head_changed is True
        assert results[unchanged].observation.head_changed is False

    def test_no_subjects_probes_nothing(self) -> None:
        runner = _CompletedRunner([])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        assert provider.probe(()) == {}
        assert runner.calls == []

    def test_a_kind_that_is_not_github_satisfies_the_same_boundary(self) -> None:
        """The shared boundary names no host, so a second kind can satisfy it.

        This implementation touches no GitHub type and is accepted by the shared
        protocol. A boundary whose return type named one kind's own result would
        reject it whatever it returned, which is what keeps this test honest
        about the abstraction rather than about GitHub.
        """

        class _CalendarProbe:
            def probe(
                self,
                subjects: Sequence[str],
                *,
                previous_observations: object = None,
            ) -> dict[str, MonitorProbeResult]:
                return {
                    subject: MonitorProbeResult(
                        canonical={"kind": "calendar", "target": subject, "slots_free": 0},
                        observation=MonitorObservation(
                            f"calendar-{subject}",
                            MonitorObservationStatus.PENDING,
                            reason_code="calendar_pending",
                        ),
                    )
                    for subject in subjects
                }

        probe: MonitorProbe = _CalendarProbe()
        results = probe.probe(("team-standup",))

        assert set(results) == {"team-standup"}
        assert results["team-standup"].canonical["kind"] == "calendar"
        assert results["team-standup"].observation.status is MonitorObservationStatus.PENDING

    def test_the_github_provider_satisfies_the_shared_boundary(self) -> None:
        probe: MonitorProbe = GitHubPullRequestProvider(
            resolver=lambda: "/trusted/bin/gh",
            runner=_CompletedRunner([]),
        )

        assert probe.probe(()) == {}

    def test_the_github_result_is_an_implementation_of_the_shared_result(self) -> None:
        """So a caller typed to the shared record can hold this kind's result."""
        core, rollup = _core_and_rollup()
        runner = _CompletedRunner(
            [_completed(core), _completed(rollup), _completed(_threads()), _completed(_comments())]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        result = _probe_one(provider)

        assert isinstance(result, MonitorProbeResult)


@pytest.mark.asyncio
async def test_shadow_fails_closed_on_a_subset_answer_like_the_controller_does() -> None:
    """Both boundary consumers must agree on what an unusable answer means.

    A plural boundary lets a provider answer for a subset of what it was asked.
    Guarding that in one consumer and letting the other raise on the same input
    would make the hazard's meaning depend on which path observed it.
    """
    snapshots: list[MonitorState] = []

    class Provider:
        def probe(self, subjects: object, **kwargs: object) -> dict[str, object]:
            return {}

    async def persist(updated: MonitorState) -> None:
        snapshots.append(deepcopy(updated))

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
    )

    verdict = await run_shadow_probe(state, Provider(), persist, now=1_100.0)

    assert verdict.decision is MonitorDecision.RETRY_PROVIDER
    assert state.last_provider_error is ProviderErrorKind.TRANSIENT
    assert len(snapshots) == 1


@pytest.mark.asyncio
async def test_shadow_fails_closed_on_an_untyped_result() -> None:
    """An untyped value would fail inside the decision engine, not at the boundary."""

    class Provider:
        def probe(self, subjects: object, **kwargs: object) -> dict[str, object]:
            return {subject: "not a probe result" for subject in subjects}  # type: ignore[union-attr]

    async def persist(updated: MonitorState) -> None:
        return None

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
    )

    verdict = await run_shadow_probe(state, Provider(), persist, now=1_100.0)

    assert verdict.decision is MonitorDecision.RETRY_PROVIDER
    assert state.last_provider_error is ProviderErrorKind.TRANSIENT


@pytest.mark.asyncio
async def test_shadow_records_a_stalled_watch_as_a_stall_not_as_the_subject() -> None:
    """The second writer of ``stopped_reason`` needs the same precedence.

    ``run_shadow_probe`` otherwise takes the reason from the observation, so a
    watch retired for repeating itself would persist as ``checks_failed`` on this
    path while the delivering path called it a stall. One rule, both writers.
    """
    red = MonitorProbeResult(
        canonical={"head_revision": "abc123"},
        observation=MonitorObservation(
            "red-1",
            MonitorObservationStatus.ACTIONABLE,
            reason_code="checks_failed",
            summary="One check is failing.",
        ),
    )

    class Provider:
        def probe(self, subjects: object, **kwargs: object) -> dict[str, object]:
            return {subject: red for subject in subjects}  # type: ignore[union-attr]

    async def persist(updated: MonitorState) -> None:
        return None

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
        # Already alerted and inside the re-alert interval, so every tick decides
        # NO_CHANGE and the verdict never moves. The key carries the revision key
        # space, which is where a subject naming no conditions is remembered.
        last_fingerprint="red-1",
        last_wake_fingerprint="red-1",
        coalesce_alerted={f"{MONITOR_REVISION_KEY_SPACE}red-1": 1_000.0},
        budgets=MonitorBudgets(max_runtime_secs=2_592_000),
    )

    for tick in range(DEFAULT_MONITOR_STALL_TICKS):
        verdict = await run_shadow_probe(
            state, Provider(), persist, now=1_100.0 + tick * DEFAULT_MONITOR_CADENCE_SECS
        )

    assert verdict.decision is MonitorDecision.STOP_BLOCKED
    assert state.outcome is MonitorOutcome.BLOCKED
    assert state.stopped_reason == MONITOR_STOP_VERDICT_STALL
    assert state.stopped_reason != "checks_failed"


def test_the_github_result_requires_its_response_explicitly() -> None:
    """No default: a caller that forgets the typed response should not compile past it."""
    with pytest.raises(TypeError):
        GitHubPullRequestProbeResult(  # type: ignore[call-arg]
            canonical={},
            observation=MonitorObservation("fp", MonitorObservationStatus.PENDING),
        )


_GRAPHQL_RATE_LIMIT_STDERR = "HTTP 403: API rate limit exceeded"
_REST_PULL_REQUEST_PATH = "repos/owner/repo/pulls/123"
_REST_STATUS_PATH = f"repos/owner/repo/commits/{_HEAD}/status?per_page=100"


def _rest_pull_request(**changes: object) -> dict[str, object]:
    """One REST pull request in the field spellings GitHub actually answers with.

    Measured against ``repos/kirodotdev/KiroCrew/pulls/11830``: ``merged`` is a
    boolean BESIDE ``state`` rather than a third state, ``mergeable`` is a boolean
    rather than an enum, ``mergeable_state`` is lowercase, and the revision is
    reached through ``head.sha``. Writing the fixture in GraphQL's spellings would
    make every test below pass against a translation that cannot read a real
    response.
    """
    payload: dict[str, object] = {
        "number": 123,
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": _HEAD},
    }
    payload.update(changes)
    return payload


def _rest_statuses(*rows: tuple[str, str]) -> dict[str, object]:
    """One REST combined-status response, carrying the latest state per context."""
    return {
        "state": "pending",
        "total_count": len(rows),
        "statuses": [{"context": context, "state": state} for context, state in rows],
    }


def _rest_board(**changes: object) -> dict[str, dict[str, object]]:
    """The two REST reads one healthy fallback tick makes."""
    return {
        _REST_PULL_REQUEST_PATH: _rest_pull_request(**changes),
        _REST_STATUS_PATH: _rest_statuses(("PR Readiness", "pending")),
    }


class _BucketRunner:
    """Answer by BUCKET: the GraphQL document refuses, the REST paths answer.

    Keyed on the REQUEST rather than on call order, because the property under
    test is that one transport refuses while the other answers. An order-keyed
    fake passes just as well when the probe sends its reads to the wrong buckets,
    which is the mistake this whole fallback could make.
    """

    def __init__(
        self,
        *,
        rest: Mapping[str, dict[str, object]] | None = None,
        graphql_error: str = "RATE_LIMITED",
        graphql_stderr: str = _GRAPHQL_RATE_LIMIT_STDERR,
        rest_stderr: str = "",
    ) -> None:
        self._rest = dict(rest or {})
        self._graphql_error = graphql_error
        self._graphql_stderr = graphql_stderr
        self._rest_stderr = rest_stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if list(argv[1:3]) == ["api", "graphql"]:
            return _completed(
                {"errors": [{"type": self._graphql_error, "message": "budget exhausted"}]},
                returncode=1,
                stderr=self._graphql_stderr,
            )
        if self._rest_stderr:
            return _completed({"message": "refused"}, returncode=1, stderr=self._rest_stderr)
        payload = self._rest.get(argv[2])
        if payload is None:
            raise AssertionError(f"unexpected REST path: {argv[2]}")
        return _completed(payload)

    @property
    def graphql_calls(self) -> list[list[str]]:
        return [call for call in self.calls if list(call[1:3]) == ["api", "graphql"]]

    @property
    def rest_paths(self) -> list[str]:
        return [call[2] for call in self.calls if list(call[1:3]) != ["api", "graphql"]]


def _bucket_provider(**kwargs: object) -> tuple[GitHubPullRequestProvider, _BucketRunner]:
    runner = _BucketRunner(**kwargs)  # type: ignore[arg-type]
    return GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner), runner


class TestRestFallbackOnRateLimit:
    """GraphQL points and REST requests are separate budgets, and the watch reads both.

    A spent GraphQL point budget refuses every read this monitor makes on that
    bucket while the REST bucket stays untouched and answering, so a probe bound to
    GraphQL alone retires a healthy pull request on ``max_provider_errors``.
    """

    def test_a_rate_limited_read_yields_facts_instead_of_a_provider_error(self) -> None:
        """THE bug: the tick reports the subject rather than a refusal.

        The two error fields are asserted together because the budget counts
        ``provider_error or supplemental_provider_error`` as one value -- either
        one left set spends the same retirement budget and retires the watch on
        the same cadence.
        """
        provider, runner = _bucket_provider(rest=_rest_board())

        result = _probe_one(provider)

        assert result.observation.status is MonitorObservationStatus.PENDING
        assert result.observation.provider_error is None
        assert result.observation.supplemental_provider_error is None
        assert result.canonical["state"] == "open"
        assert result.canonical["head_revision"] == _HEAD
        assert runner.rest_paths == [_REST_PULL_REQUEST_PATH, _REST_STATUS_PATH]

    def test_a_degraded_board_is_pending_and_can_never_be_review_ready(self) -> None:
        """Two independent reasons, so removing either one still leaves it closed.

        REST carries only half the board and no review decision at all, so the
        observation is held at PENDING by its incompleteness AND by its unknown
        review decision. The classifier assertion keeps the second reason
        load-bearing if the first ever stops applying.
        """
        provider, _ = _bucket_provider(
            rest={
                _REST_PULL_REQUEST_PATH: _rest_pull_request(),
                _REST_STATUS_PATH: _rest_statuses(("PR Readiness", "success")),
            },
        )

        result = _probe_one(provider)

        assert result.observation.status is MonitorObservationStatus.PENDING
        assert result.observation.reason_code == "checks_incomplete"
        assert result.canonical["review_decision"] == "unknown"
        assert result.canonical["checks_complete"] is False
        assert result.canonical["review_threads_complete"] is False
        status, reason = classify_pull_request_facts(
            PullRequestFacts(
                kind="github_pull_request",
                target="github.com/owner/repo#123",
                state="open",
                draft=False,
                head_revision=_HEAD,
                mergeability="mergeable",
                review_decision="unknown",
                checks=(PullRequestCheck("PR Readiness", "passed"),),
                checks_complete=True,
                unresolved_review_threads=0,
                review_threads_complete=True,
            )
        )
        assert status is MonitorObservationStatus.PENDING
        assert reason == "review_state_unknown"

    def test_a_failing_commit_status_still_wakes_the_session(self) -> None:
        """A degraded board is still worth waking on, which is the point of it.

        The identity is the ``context`` verbatim -- the same string the GraphQL
        rollup reports for the same status -- so the failure carries one identity
        across both transports instead of being reported twice under two.
        """
        provider, _ = _bucket_provider(
            rest={
                _REST_PULL_REQUEST_PATH: _rest_pull_request(mergeable_state="blocked"),
                _REST_STATUS_PATH: _rest_statuses(
                    ("PR Readiness", "failure"),
                    ("AWS CodeBuild us-east-1 (kirocrew-gha-linux)", "success"),
                ),
            },
        )

        result = _probe_one(provider)

        assert result.observation.status is MonitorObservationStatus.ACTIONABLE
        assert result.observation.reason_code == "checks_failed"
        checks = result.canonical["checks"]
        assert isinstance(checks, Mapping)
        assert checks["failed"] == ["PR Readiness"]
        assert checks["passed"] == ["AWS CodeBuild us-east-1 (kirocrew-gha-linux)"]

    @pytest.mark.parametrize(
        ("error_type", "stderr", "kind"),
        [
            ("NOT_FOUND", "HTTP 404: Not Found", ProviderErrorKind.NOT_FOUND),
            ("FORBIDDEN", "HTTP 403: resource not accessible", ProviderErrorKind.AUTHORIZATION),
            ("UNAUTHORIZED", "HTTP 401: Bad credentials", ProviderErrorKind.AUTHENTICATION),
            ("INTERNAL", "HTTP 503: unavailable", ProviderErrorKind.TRANSIENT),
        ],
    )
    def test_only_a_rate_limit_is_retried_on_the_other_bucket(
        self,
        error_type: str,
        stderr: str,
        kind: ProviderErrorKind,
    ) -> None:
        """Every other failure says something a second transport answers identically.

        A missing subject is missing on both buckets and a rejected credential is
        rejected on both, so retrying one would only make the failure slower to
        report. Asserting no REST request was sent is what pins that.
        """
        provider, runner = _bucket_provider(graphql_error=error_type, graphql_stderr=stderr)

        result = _probe_one(provider)

        assert result.observation.status is MonitorObservationStatus.PROVIDER_ERROR
        assert result.observation.provider_error is kind
        assert runner.rest_paths == []

    def test_a_refused_fallback_keeps_the_retryable_rate_limit(self) -> None:
        """The fallback is never worse than no fallback.

        A REST failure is a SECOND diagnosis of a subject already known to be
        refused. Letting it replace the first would substitute a terminal kind for
        a retryable one, so a REST ``404`` would retire on its first tick a watch
        that the GraphQL-only code retried.
        """
        provider, runner = _bucket_provider(rest_stderr="HTTP 404: Not Found")

        result = _probe_one(provider)

        assert result.observation.provider_error is ProviderErrorKind.RATE_LIMITED
        assert result.observation.reason_code == "provider_rate_limited"
        assert runner.rest_paths == [_REST_PULL_REQUEST_PATH]

    def test_a_malformed_fallback_body_keeps_the_rate_limit(self) -> None:
        """A body this adapter cannot read leaves the charge exactly as it was."""
        provider, _ = _bucket_provider(rest={_REST_PULL_REQUEST_PATH: {"number": 123}})

        result = _probe_one(provider)

        assert result.observation.provider_error is ProviderErrorKind.RATE_LIMITED

    def test_a_degraded_subject_spends_no_further_graphql_document(self) -> None:
        """The bucket that refused the first read is not asked twice in one tick."""
        provider, runner = _bucket_provider(rest=_rest_board())

        _probe_one(provider)

        assert len(runner.graphql_calls) == 1
        assert runner.rest_paths == [_REST_PULL_REQUEST_PATH, _REST_STATUS_PATH]

    def test_the_fallback_path_carries_only_validated_segments(self) -> None:
        """No provider-controlled text reaches a request path.

        ``GitHubPullRequestTarget`` admits an owner matching
        ``GITHUB_OWNER_SEGMENT_RE``, a repository matching ``GITHUB_REPO_SEGMENT_RE``
        and a positive integer number, and the revision has already
        passed ``_HEAD_REVISION_RE`` on the primary read, so both paths are built
        from validated segments alone.
        """
        provider, runner = _bucket_provider(rest=_rest_board())

        _probe_one(provider)

        assert runner.rest_paths == [
            "repos/owner/repo/pulls/123",
            f"repos/owner/repo/commits/{_HEAD}/status?per_page=100",
        ]

    @pytest.mark.parametrize(
        ("mergeable", "merge_state", "expected"),
        [
            (False, "dirty", "conflicting"),
            (True, "behind", "behind"),
            (True, "blocked", "blocked"),
            (True, "clean", "mergeable"),
            (True, "unstable", "mergeable"),
            (None, "unknown", "pending"),
        ],
    )
    def test_rest_merge_state_reaches_the_same_mergeability_enum(
        self,
        mergeable: object,
        merge_state: str,
        expected: str,
    ) -> None:
        """One mapping, not one per transport.

        REST spells ``mergeStateStatus`` in lowercase and ``mergeable`` as a
        boolean, so the translation is case and type -- ``_normalize_mergeability``
        stays the only place the enum is decided, and the two buckets cannot come
        to disagree about what ``blocked`` means.
        """
        provider, _ = _bucket_provider(
            rest={
                _REST_PULL_REQUEST_PATH: _rest_pull_request(
                    mergeable=mergeable,
                    mergeable_state=merge_state,
                ),
                _REST_STATUS_PATH: _rest_statuses(),
            },
        )

        result = _probe_one(provider)

        assert result.canonical["mergeability"] == expected

    def test_a_merged_subject_read_on_rest_is_terminal_as_merged(self) -> None:
        """REST answers a merged pull request as ``state: closed`` with ``merged: true``.

        Passing ``state`` through would report the merge as a CLOSE -- BLOCKED
        rather than SUCCESS -- which is a wrong terminal verdict on a pull request
        that landed. A terminal subject also issues no board read, on this
        transport for the same reason as on the other.
        """
        provider, runner = _bucket_provider(
            rest={_REST_PULL_REQUEST_PATH: _rest_pull_request(merged=True, state="closed")},
        )

        result = _probe_one(provider)

        assert result.observation.status is MonitorObservationStatus.SUCCESS
        assert result.observation.reason_code == "pull_request_merged"
        assert runner.rest_paths == [_REST_PULL_REQUEST_PATH]

    def test_a_rate_limited_rollup_alone_is_answered_from_rest_statuses(self) -> None:
        """The second door, and it is reachable on its own.

        GitHub prices a query in points, and the rollup document costs more than
        the core selection, so near the end of the budget the primary read
        succeeds while the rollup is refused -- persistently. That tick carries a
        SUPPLEMENTAL rate limit, which the budget counts identically, so fixing
        only the primary read would leave the watch retiring on the same cadence.
        """
        runner = _CompletedRunner(
            [
                _completed(_envelope(_pr_node(_primary()))),
                _completed(
                    {"errors": [{"type": "RATE_LIMITED", "message": "budget exhausted"}]},
                    returncode=1,
                    stderr=_GRAPHQL_RATE_LIMIT_STDERR,
                ),
                _completed(_threads()),
                _completed(_rest_statuses(("PR Readiness", "failure"))),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        result = _probe_one(provider)

        assert result.observation.supplemental_provider_error is None
        assert result.observation.status is MonitorObservationStatus.ACTIONABLE
        assert result.observation.reason_code == "checks_failed"
        checks = result.canonical["checks"]
        assert isinstance(checks, Mapping)
        assert checks["failed"] == ["PR Readiness"]
        assert runner.calls[4][2] == _REST_STATUS_PATH

    def test_a_rate_limited_thread_read_reports_incomplete_without_an_error(self) -> None:
        """The one signal REST cannot express, degraded rather than charged.

        Thread resolution has no REST projection, so a refused thread read is
        reported as an incomplete COUNT. Incompleteness already holds the subject
        at PENDING, which is what makes it safe to stop charging it: the watch
        survives without ever being able to call the subject ready. No REST
        fallback request is sent for the thread read, because there is no
        endpoint to send it to; the PR-comment read is a separate, always-issued
        supplemental, so the tick still costs four requests.
        """
        runner = _CompletedRunner(
            [
                _completed(_envelope(_pr_node(_primary()))),
                _completed(_envelope(_rollup_node(_primary()))),
                _completed(
                    {"errors": [{"type": "RATE_LIMITED", "message": "budget exhausted"}]},
                    returncode=1,
                    stderr=_GRAPHQL_RATE_LIMIT_STDERR,
                ),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        result = _probe_one(provider)

        assert result.observation.supplemental_provider_error is None
        assert result.observation.status is MonitorObservationStatus.PENDING
        assert result.observation.reason_code == "review_threads_incomplete"
        assert result.canonical["review_threads_complete"] is False
        assert result.canonical["blocking_review"] == "unknown"
        assert len(runner.calls) == 4

    @pytest.mark.asyncio
    async def test_consecutive_rate_limited_ticks_do_not_retire_the_watch(self) -> None:
        """The reported stop, end to end.

        ``max_provider_errors`` defaults to three, so three consecutive
        GraphQL-refused ticks reached ``provider_error_budget`` and retired the
        watch. Each tick now reads the REST bucket, reports facts, and CLEARS the
        streak -- so the budget is not merely approached more slowly, it is never
        charged at all.
        """
        provider, _ = _bucket_provider(rest=_rest_board())
        state = MonitorState(
            kind="github_pull_request",
            target="https://github.com/owner/repo/pull/123",
            objective="review_ready",
            created_ts=1_000.0,
        )

        async def persist(updated: MonitorState) -> None:
            return None

        ticks = DEFAULT_MONITOR_PROVIDER_ERRORS + 1
        for tick in range(ticks):
            verdict = await run_shadow_probe(
                state,
                provider,
                persist,
                now=1_000.0 + tick * DEFAULT_MONITOR_CADENCE_SECS,
            )

        assert state.probe_count == ticks
        assert state.provider_error_count == 0
        assert state.consecutive_provider_errors == 0
        assert state.last_provider_error is None
        # The reported stop, named exactly: `outcome: budget` with
        # `stopped_reason: provider_error_budget`. An unretired watch has recorded
        # no outcome at all.
        assert state.outcome is None
        assert state.outcome is not MonitorOutcome.BUDGET
        assert verdict.decision is not MonitorDecision.STOP_BLOCKED


def _comment_condition_key(result: object) -> str | None:
    """The one PR-level-comment-body condition key on a result, or None."""
    keys = [
        condition.key
        for condition in result.observation.conditions
        if condition.key.startswith("review_comment_bodies:")
    ]
    assert len(keys) <= 1, "at most one comment-digest condition per subject"
    return keys[0] if keys else None


class TestPullRequestCommentDigest:
    """The probe digests PR-level (issue) comment bodies, the surface the review
    bot verdicts actually live on.

    A verdict comment's created_at is frozen at PR open while its body is
    rewritten in place, so a count or newest-timestamp probe cannot see it; only
    a digest over the bodies can. The subject reaches ACTIONABLE by a failing
    check here, so its conditions are carried and the comment digest rides
    alongside -- exactly as the thread digest rides on an unresolved-thread
    subject.
    """

    @staticmethod
    def _failing() -> dict[str, object]:
        return _primary(statusCheckRollup=[_check_run(conclusion="FAILURE")])

    def test_an_in_place_comment_edit_changes_the_condition_key(self) -> None:
        """The body flipped in place: only a digest over the bodies sees it."""
        before, _ = _provider(
            self._failing(), _threads(), pr_comments=_comments(["no blocking findings"])
        )
        after, _ = _provider(
            self._failing(), _threads(), pr_comments=_comments(["blocking: found an issue"])
        )
        result_before = _probe_one(before)
        result_after = _probe_one(after)

        key_before = _comment_condition_key(result_before)
        key_after = _comment_condition_key(result_after)
        assert key_before is not None and key_after is not None
        assert key_before != key_after

    def test_every_comment_is_digested_not_bot_authored_only(self) -> None:
        """A human editing a comment in place is as invisible to a count and as
        load-bearing as a bot; the digest filters by no author."""
        before, _ = _provider(self._failing(), _threads(), pr_comments=_comments(["please rebase"]))
        after, _ = _provider(
            self._failing(), _threads(), pr_comments=_comments(["please rebase and squash"])
        )
        result_before = _probe_one(before)
        result_after = _probe_one(after)
        assert _comment_condition_key(result_before) != _comment_condition_key(result_after)

    def test_the_digest_is_independent_of_comment_page_order(self) -> None:
        """Sorting the bodies before hashing makes the page order irrelevant."""
        first, _ = _provider(
            self._failing(),
            _threads(),
            pr_comments=[
                _comments(["alpha"], has_next=True, cursor="cursor-1"),
                _comments(["beta"], has_next=False),
            ],
        )
        second, _ = _provider(
            self._failing(),
            _threads(),
            pr_comments=[
                _comments(["beta"], has_next=True, cursor="cursor-2"),
                _comments(["alpha"], has_next=False),
            ],
        )
        result_first = _probe_one(first)
        result_second = _probe_one(second)
        assert _comment_condition_key(result_first) is not None
        assert _comment_condition_key(result_first) == _comment_condition_key(result_second)

    def test_no_comments_emits_no_comment_condition(self) -> None:
        provider, _ = _provider(self._failing(), _threads(), pr_comments=_comments())
        result = _probe_one(provider)

        assert result.observation.status is MonitorObservationStatus.ACTIONABLE
        assert "pr_comment_body_digest" not in result.canonical
        assert _comment_condition_key(result) is None

    def test_an_incomplete_comment_read_carries_no_digest_or_condition(self) -> None:
        """The correctness trap on the comment surface: a capped read digests
        nothing, so a page that keeps failing cannot wake the owner forever."""
        pages = [
            _comments([f"comment {page}"], has_next=True, cursor=f"cursor-{page}")
            for page in range(1, 11)
        ]
        provider, _ = _provider(self._failing(), _threads(), pr_comments=pages)
        result = _probe_one(provider)

        assert "pr_comment_body_digest" not in result.canonical
        assert _comment_condition_key(result) is None


class TestRetentionIsBounded:
    """What the probe KEEPS per comment is fixed-width, whatever arrives.

    A comment body is unbounded third-party text and a paged read holds one entry
    per comment across every page, so retaining the body would let one large
    comment -- or a busy pull request full of them -- size the probe's own memory.
    The bound is applied at the moment of retention, which is what these pin: a
    huge body and a tiny one cost the same, and the body itself never survives
    into anything downstream.
    """

    HUGE = "x" * 1_000_000
    MARKER = "UNIQUE-BODY-MARKER-b7f3"

    def test_a_pr_comment_page_keeps_a_fixed_width_value_per_comment(self) -> None:
        """One tiny body and one enormous one are retained at the same width."""
        node = {
            "comments": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"body": "a"}, {"body": self.HUGE}],
            }
        }
        kept, complete, has_next, _cursor = github_pull_request._pr_comment_page(node)
        assert complete is True
        assert has_next is False
        assert len(kept) == 2
        assert {len(entry) for entry in kept} == {64}
        # Retention does not scale with what a reviewer wrote: two comments cost
        # 128 characters even when one of them is a megabyte.
        assert sum(len(entry) for entry in kept) == 128

    def test_no_pr_comment_body_survives_into_what_is_retained(self) -> None:
        """The body is not merely shortened, it is absent from the whole path."""
        body = f"before {self.MARKER} after"
        node = {
            "comments": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"body": body}],
            }
        }
        kept, _complete, _has_next, _cursor = github_pull_request._pr_comment_page(node)
        assert all(self.MARKER not in entry for entry in kept)
        digest = github_pull_request._pr_comment_body_digest(kept)
        assert self.MARKER not in digest
        assert len(digest) == 64

    def test_the_bound_still_distinguishes_two_different_bodies(self) -> None:
        """Bounding retention must not cost the digest its whole purpose.

        A fixed-width stand-in is only useful if two different bodies still
        produce different retained values -- otherwise the bound would buy memory
        by making every edit invisible, which is the failure this condition
        exists to prevent.
        """

        def page(body: str) -> list[str]:
            node = {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"body": body}],
                }
            }
            kept, _c, _h, _cur = github_pull_request._pr_comment_page(node)
            return kept

        one = page("no blocking findings")
        two = page("BLOCKING: a finding appeared")
        assert one != two
        assert github_pull_request._pr_comment_body_digest(
            one
        ) != github_pull_request._pr_comment_body_digest(two)
