"""Golden behaviour table locking the GitHub pull-request probe's output.

The monitoring substrate is being refactored into a plug-in shape across several
changes: a verdict that carries a payload, a probe result that names no host, and
a registry of kinds. Every one of those is meant to be behaviour-preserving, and
"behaviour" here means the exact bytes this provider derives from a gh response:
the canonical fact dictionary, the observation status, the reason code, and the
fingerprint the decision engine compares against persisted state.

A table produced *after* a refactor records the new behaviour as if it were the
old one and proves nothing. So this digest is captured on the pristine tree
before a change and asserted afterwards. If a refactor is genuinely a refactor,
:data:`GOLDEN_DIGEST` does not move.

The matrix is driven through the public :meth:`GitHubPullRequestProvider.probe`
with a fake gh runner, exactly as the rest of the provider's tests are, so it
covers response normalization as well as canonicalization rather than reaching
into private helpers.

When a change is *meant* to move the table, re-pin both constants in the same
commit from the values the failure prints, and say in the pull request which
groups moved and why.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
from collections.abc import Sequence

from kiro_crew.monitoring.github_pull_request import (
    GitHubPullRequestProvider,
    parse_github_pull_request_target,
)

TARGET_URL = "https://github.com/owner/repo/pull/123"
HEAD = "0123456789abcdef0123456789abcdef01234567"
PREVIOUS_HEAD = "fedcba9876543210fedcba9876543210fedcba98"

#: sha256 over the canonical probe output of every matrix row. Captured on
#: kirocrew/main at 53987e756 -- before the verdict-payload change -- and
#: unchanged by it.
GOLDEN_DIGEST = "2ccba80c98aaf46ec5de2f6180664b0f063fe28c1f3502083a270d1c0e2ef7af"

#: The same output digested per pull-request state and check-rollup shape, so a
#: mismatch names the shape that moved instead of only the whole table. Captured
#: on the same pristine tree.
GOLDEN_GROUP_DIGESTS: dict[str, str] = {
    "CLOSED/absent": "19569bf418bb733eeba562f4081dd46ff1cce8334ad13269fe802c186b62f533",
    "CLOSED/cancelled": "2596fce96d8b0a0175a07cb85cf4cb6d57d1931f1bbab0d34474434099bbe85c",
    "CLOSED/empty": "e25b40e2cd8efeb9a59d6fb3ec504163469ed363a984eb47d189fe992a2b9b29",
    "CLOSED/green": "d15716691af7ad6b3ac04775dfa9b27ce2ee2a362c863481eb0f9e6afcd7e8f5",
    "CLOSED/one-red": "92c6f15700e7af3b44af8e6d7e9c1ac9862d22e873bac9163a5cc4c665f59387",
    "CLOSED/pending": "2769af520bb849ef8aca111d5ea156ee12fcd1f04c326abf2e66603169864f39",
    "CLOSED/unknown-conclusion": "e51bd441d39b62cd11a5a24e32e4a13cb9a6ce173d336f5cfc580737dc6f25e6",
    "MERGED/absent": "b2beee0d9d2fe3bb9b6e356075718c37158185e00559926aca60bfeac132e7df",
    "MERGED/cancelled": "8fd31fa1c61a9184a88a53ed066eda91ecaab26dcec25029d5cc3b912abb31d5",
    "MERGED/empty": "fde0471c35ce908e27bd80c0d5c0f57c45af771ff7ba8bc5dc4cf3574db2acc3",
    "MERGED/green": "22417ae5c7382e4d3fe08e9175ea23536af2d9db1a25004aa1c17e4eb800e445",
    "MERGED/one-red": "3536fe6854e8d9b10c49b09954b7bae49259d49c4acf89790dc4b81f37c7aff3",
    "MERGED/pending": "b14b7c234c9b263f1d2c826927de8764ef75dd47963a5c0e310a8ab1bc9742cf",
    "MERGED/unknown-conclusion": "fa71c711557fdc696b963c6c0a8b907d3ac73f69bba4248f2efeabe078984159",
    "OPEN/absent": "8da02cd1eafa6607b707e340afeb08abe7e209d986da9edd438ce6777cd63625",
    "OPEN/cancelled": "e53740c00f90ff470750453a963c8b16339e0a09ece5c2ba11303dfce2722c81",
    "OPEN/empty": "ee0711180b5362fcf823529c7b1bb1fe8c5a6fe20e69db055e995884f15e9d35",
    "OPEN/green": "d99ab3c3c63fb1d4b7db7ea89aa999843a28de9ba06f81e0860607b7f9d69b9c",
    "OPEN/one-red": "9156973395e03b051d1332c8848616f7954bc9b0cd6c8dbfc834a7807868c72c",
    "OPEN/pending": "e44ec8b8bf86c57dd4be7c110730cd0f78b3efa5b46d9d869f637c522bb60926",
    "OPEN/unknown-conclusion": "dba77d405759eed23d6e096b40623ca51604e37e168c2c8f8c6be072ec99a2c4",
    "SOMETHING_ELSE/absent": "ea62fb42ed605b0bd939500e7e541a71b8a7cadeb3750668229bad37ebf7a01c",
    "SOMETHING_ELSE/cancelled": "5e0eb46902fd14f25ff977d1e1ae04f30172f8600c8de44489ad60e5a6c23e4a",
    "SOMETHING_ELSE/empty": "199cd5dadc65f72e021bf4cad522f12d6380cdb330d0da54aa39e9eec79db510",
    "SOMETHING_ELSE/green": "f08fbc813748c96f45745627d8b1e8404f4214d8a73769d4f2ea933f1340c64c",
    "SOMETHING_ELSE/one-red": "91c5ca8eb69e927b6220dd527fc692f96816e4aef5d4f0462e4ff16a7aa89382",
    "SOMETHING_ELSE/pending": "fd5ffcdcf545d46a478bed51dd97047a7f1391b94efa99f4d1e1ff79973e1ea9",
    "SOMETHING_ELSE/unknown-conclusion": (
        "10f49a47b6623badb98a81b2b16a75a3f578092f41a1afa24cdfdc7eab9d63f2"
    ),
}

_ROLLUPS: tuple[tuple[str, object], ...] = (
    ("absent", None),
    ("empty", []),
    (
        "green",
        [
            {
                "__typename": "CheckRun",
                "name": "test",
                "workflowName": "CI",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {"__typename": "StatusContext", "context": "lint", "state": "SUCCESS"},
        ],
    ),
    (
        "one-red",
        [
            {
                "__typename": "CheckRun",
                "name": "test",
                "workflowName": "CI",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            },
            {"__typename": "StatusContext", "context": "lint", "state": "SUCCESS"},
        ],
    ),
    (
        "pending",
        [
            {
                "__typename": "CheckRun",
                "name": "test",
                "workflowName": "CI",
                "status": "IN_PROGRESS",
                "conclusion": None,
            }
        ],
    ),
    (
        "unknown-conclusion",
        [
            {
                "__typename": "CheckRun",
                "name": "test",
                "workflowName": "CI",
                "status": "COMPLETED",
                "conclusion": "NEUTRAL_SOMETHING",
            }
        ],
    ),
    (
        "cancelled",
        [
            {
                "__typename": "CheckRun",
                "name": "test",
                "workflowName": "CI",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
            }
        ],
    ),
)

_THREADS: tuple[tuple[str, list[dict[str, object]]], ...] = (
    ("none", []),
    ("resolved", [{"isResolved": True, "isOutdated": False}]),
    ("unresolved", [{"isResolved": False, "isOutdated": False}]),
    ("outdated-unresolved", [{"isResolved": False, "isOutdated": True}]),
    (
        "mixed",
        [
            {"isResolved": False, "isOutdated": False},
            {"isResolved": True, "isOutdated": False},
            {"isResolved": False, "isOutdated": True},
        ],
    ),
)

_STATES = ("OPEN", "CLOSED", "MERGED", "SOMETHING_ELSE")
_DRAFTS = (False, True)
_MERGEABLE = ("MERGEABLE", "CONFLICTING", "UNKNOWN")
_MERGE_STATE = ("CLEAN", "BEHIND", "BLOCKED", "DIRTY", "UNSTABLE")
_REVIEW_DECISIONS = ("APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", None)


class _MatrixRunner:
    """Answer the provider's three gh calls from one row's fixtures."""

    def __init__(self, primary: dict[str, object], threads: list[dict[str, object]]) -> None:
        self._primary = primary
        self._threads = threads

    def __call__(self, argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(str(part) for part in argv)
        if "graphql" in joined:
            payload: object = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": self._threads,
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            }
        elif "statusCheckRollup" in joined:
            payload = {
                "headRefOid": self._primary["headRefOid"],
                "statusCheckRollup": self._primary.get("statusCheckRollup"),
            }
        else:
            payload = {
                key: value for key, value in self._primary.items() if key != "statusCheckRollup"
            }
        return subprocess.CompletedProcess(list(argv), 0, stdout=json.dumps(payload), stderr="")


def _rows() -> list[dict[str, object]]:
    """Probe every combination and record only its externally meaningful output."""
    parse_github_pull_request_target(TARGET_URL)
    rows: list[dict[str, object]] = []
    combos = itertools.product(
        _STATES,
        _DRAFTS,
        _MERGEABLE,
        _MERGE_STATE,
        _REVIEW_DECISIONS,
        _ROLLUPS,
        _THREADS,
    )
    for (
        state,
        draft,
        mergeable,
        merge_state,
        review,
        (rollup_label, rollup),
        (
            threads_label,
            threads,
        ),
    ) in combos:
        primary: dict[str, object] = {
            "number": 123,
            "state": state,
            "isDraft": draft,
            "headRefOid": HEAD,
            "mergeable": mergeable,
            "mergeStateStatus": merge_state,
            "reviewDecision": review,
            "statusCheckRollup": rollup,
        }
        provider = GitHubPullRequestProvider(
            resolver=lambda: "/usr/bin/gh",
            runner=_MatrixRunner(primary, threads),
        )
        result = provider.probe(
            TARGET_URL,
            previous_observation={"head_revision": PREVIOUS_HEAD},
        )
        observation = result.observation
        rows.append(
            {
                "key": [
                    state,
                    draft,
                    mergeable,
                    merge_state,
                    review or "none",
                    rollup_label,
                    threads_label,
                ],
                "canonical": result.canonical,
                "fingerprint": observation.fingerprint,
                "status": observation.status.value,
                "reason_code": observation.reason_code,
                "head_changed": observation.head_changed,
                "provider_error": (
                    observation.provider_error.value
                    if observation.provider_error is not None
                    else None
                ),
                "supplemental_provider_error": (
                    observation.supplemental_provider_error.value
                    if observation.supplemental_provider_error is not None
                    else None
                ),
            }
        )
    return rows


def _digest(rows: list[dict[str, object]]) -> str:
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _group_digests(rows: list[dict[str, object]]) -> dict[str, str]:
    """Digest each pull-request state and rollup shape on its own.

    One hash over every row says only that something moved. Grouping localizes a
    mismatch to the shape that caused it, so the next refactor reads a group name
    instead of regenerating the table blind.
    """
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        key = row["key"]
        assert isinstance(key, list)
        grouped.setdefault(f"{key[0]}/{key[5]}", []).append(row)
    return {name: _digest(members) for name, members in sorted(grouped.items())}


def test_probe_behaviour_table_has_not_moved() -> None:
    """Lock every canonical fact, classification and fingerprint the probe derives."""
    rows = _rows()
    assert rows, "the matrix must not be empty"

    groups = _group_digests(rows)
    moved = sorted(
        name
        for name, digest in groups.items()
        if GOLDEN_GROUP_DIGESTS.get(name) not in (None, digest)
    )
    missing = sorted(set(GOLDEN_GROUP_DIGESTS) - set(groups))
    appeared = sorted(set(groups) - set(GOLDEN_GROUP_DIGESTS))
    actual = _digest(rows)

    assert (actual, moved, missing, appeared) == (GOLDEN_DIGEST, [], [], []), (
        "the GitHub pull-request probe's observable output changed.\n"
        f"  digest now: {actual}\n"
        f"  digest pinned: {GOLDEN_DIGEST}\n"
        f"  groups that moved: {moved or 'none'}\n"
        f"  groups that disappeared: {missing or 'none'}\n"
        f"  groups that appeared: {appeared or 'none'}\n"
        "A refactor meant to preserve behaviour must not move any of this. If the "
        "change is deliberate, re-pin GOLDEN_DIGEST and GOLDEN_GROUP_DIGESTS from "
        "the values above in the same commit, and say in the pull request which "
        "groups moved and why."
    )
