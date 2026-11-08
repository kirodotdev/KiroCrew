"""The vocabulary every provider read projects into, shared so it cannot drift.

Provider JSON is coerced to the payload contract here (lists, dicts, authors,
counts, https-only links, issue labels and milestones), and the chip lifecycle and
merge-state vocabularies both caches compare are defined once. A second copy of
any of these in a provider module is how the chip and the panel start to disagree.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.dashboard.source_providers import contract

# One page of a secondary read (files, comments, commits, jobs). A full page is
# reported as possibly truncated rather than paged further.
_SECONDARY_PAGE_SIZE = 100


def _or_empty(value: Any) -> Any:
    """Coerce an already-recorded gather failure into an empty section."""
    if isinstance(value, BaseException):
        return []
    return value


def _mark_partial(partial_sections: list[str], section: str) -> None:
    """Add a partial-result section once while preserving display order."""
    if section not in partial_sections:
        partial_sections.append(section)


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce *value* to a dict, returning an empty dict for non-dict inputs."""
    return value if isinstance(value, dict) else {}


def _author(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("login") or value.get("username") or value.get("name") or "")
    return str(value or "")


# --- Shared chip-status projection ------------------------------------------
#
# The sidebar chips and the detail panel derive the same {state, ci} chip
# projection from two different provider reads (a lightweight chip fetch and a
# full payload). The two caches are mutually invalidating, so the
# invariant "both projections agree" is load-bearing: any vocabulary drift turns
# a common steady state into a sustained cache ping-pong. To make drift
# structurally impossible rather than convention-enforced, BOTH paths route
# every raw provider value through the single functions below. Do not inline a
# second copy of this vocabulary anywhere.


def _rollup_ci(buckets: list[str]) -> str | None:
    """Roll per-check buckets up to a single chip CI value (or ``None``)."""
    if not buckets:
        return None
    if "failed" in buckets:
        return "failed"
    if "pending" in buckets:
        return "running"
    return "passed"


# Chip-status key set when a FAILED rollup still has checks in flight. ``ci``
# alone cannot say it: failure wins the rollup, so "one check failed, the rest
# are still running" and "every check finished, one failed" both read
# ``failed``, and the chip had no way to show that CI is still moving. The
# value is the string ``"1"`` because the chip cache is ``dict[str, str]``;
# the key is absent otherwise, never ``"0"``.
CI_PENDING_KEY = "ciPending"

# The providers whose chip read computes ``ciPending``. A registered plugin's
# chip status is ``ci`` and ``state`` only, so the full-payload write-through
# must not project the marker for one either, or the two would disagree on
# every chip refresh and refetch the full payload.
CI_PENDING_PROVIDERS = frozenset(
    {"github", contract.BITBUCKET_SERVER_PROVIDER, contract.GERRIT_PROVIDER}
)

# The CI fields a degraded read carries forward together: a kept ``ci`` with
# a dropped ``ciPending`` would silently claim the run had finished.
CI_STATUS_FIELDS = ("ci", CI_PENDING_KEY)


def _record_merge_state(result: dict[str, str], mergeable: str, merge_state: str) -> None:
    """Add the merge fields to a chip-status entry, each only once it is real.

    An unanswered field is left out entirely rather than written as ``unknown``:
    the chip cache is a short-TTL hint the client compares against its loaded
    pull-request payload, and "still computing" must not read as a disagreement
    with a real answer the payload already has. The two fields are recorded
    independently because GitLab settles `need_rebase` and its branch-protection
    gates in the detail field while ``mergeable`` stays ``unknown`` — dropping the
    detail because its sibling is unknown would leave exactly those banners
    invisible to the poll.
    """
    if _merge_state_real(mergeable):
        result["mergeable"] = mergeable
    if _merge_state_real(merge_state):
        result["mergeStateStatus"] = merge_state


def _record_ci(result: dict[str, str], checks: list[dict[str, Any]]) -> None:
    """Write the rolled-up CI fields for the check rows *checks* into a chip entry.

    The single writer of ``ci`` + ``ciPending`` from per-check rows, so the chip
    read and the full-payload write-through cannot disagree about either. ``ci``
    rolls up the buckets; ``ciPending`` is set on a ``failed`` rollup that still
    has a row bucketed ``pending``.
    """
    buckets = [str(check.get("bucket") or "") for check in checks]
    ci = _rollup_ci(buckets)
    if ci is None:
        return
    result["ci"] = ci
    if ci == "failed" and "pending" in buckets:
        result[CI_PENDING_KEY] = "1"


def _project_state(raw_state: str, *, draft: bool) -> str | None:
    """Map a provider PR/MR lifecycle state to the chip ``state`` vocabulary.

    GitHub reports OPEN/MERGED/CLOSED; GitLab opened/merged/closed/locked. A
    ``draft`` flag only means "draft" while the PR is still open — GitLab keeps
    ``draft: true`` on an MR closed while in draft, so the draft mapping must be
    gated on the open state or the two paths diverge (chip "draft" vs full
    "closed") and ping-pong forever.

    ``locked`` is GitLab's *transient* state while a merge is in progress — it
    is not a terminal lifecycle. Mapping it to ``closed`` painted a false
    "closed" glyph on an MR that is actually mid-merge and conflicted with the
    detail panel's own locked handling. Project nothing for it (both paths agree
    on "no lifecycle change") and let the next read resolve to merged/closed once
    GitLab settles.
    """
    state = raw_state.lower()
    if state in {"open", "opened"}:
        return "draft" if draft else "open"
    if state == "merged":
        return "merged"
    if state == "closed":
        return "closed"
    return None


# Chip-vocabulary states (see ``_project_state``) that never move on their own.
_TERMINAL_CHIP_STATES = frozenset({"merged", "closed"})


def _chip_state(status: dict[str, str] | None) -> str:
    """The chip-vocabulary lifecycle of a cached chip status, ``""`` when unknown."""
    return str(status.get("state") or "") if status else ""


# Both providers compute mergeability lazily: reading a pull request that has
# not been evaluated recently returns "not known yet" (GitHub ``UNKNOWN``,
# GitLab ``checking``/``unchecked``) *and* kicks off the computation, so the
# real answer is only available on a later read. A single read therefore reports
# a conflicting pull request as having no merge blocker at all — which would show
# the panel's conflict banner only once the user hit refresh.
# These bound a short re-read of the merge fields alone (not the whole fanout),
# issued concurrently with the secondary provider calls so most of the wait is
# absorbed by work the request was already doing.
_MERGE_STATE_REREADS = 2
_MERGE_STATE_REREAD_DELAY_SECS = 0.8
# The one normalized value that means "the provider has not answered yet". It is
# shared by both fields of the merge pair and by both providers.
_UNSETTLED_MERGE_STATE = "unknown"


def _merge_state_real(value: str) -> bool:
    """Whether one normalized merge field carries a real answer."""
    return bool(value) and value != _UNSETTLED_MERGE_STATE


def _merge_state_settled(mergeable: str, merge_state: str) -> bool:
    """Whether a normalized merge pair is a real answer, so no re-read is due.

    A pair is settled once **either** field is real. GitLab reports `need_rebase`
    and its branch-protection gates with ``mergeable == 'unknown'`` — the detail
    IS the answer there, so keying only on ``mergeable`` would re-read a state
    the provider had already settled and then discard it. A pair that is empty
    rather than unknown means the provider did not report the fields at all, so
    re-reading cannot settle it either.
    """
    if mergeable == _UNSETTLED_MERGE_STATE or merge_state == _UNSETTLED_MERGE_STATE:
        return _merge_state_real(mergeable) or _merge_state_real(merge_state)
    return True


# --- Issues -----------------------------------------------------------------
#
# Issues reuse the pull-request transport wholesale (`_run_json` isolation,
# redaction, byte caps, the validated-ref identity rule) and add only their own
# normalization, which starts here and continues in each provider module. They
# deliberately do NOT touch the chip-status cache: an issue has no CI or merge
# state, so `record_full_payload_status` is never called for one and
# `get_cached_check_status` is never consulted for one either.

# Contract order for the reaction counters, paired with GitHub's own REST keys.
_GITHUB_REACTION_KEYS: tuple[tuple[str, str], ...] = (
    ("plus1", "+1"),
    ("minus1", "-1"),
    ("laugh", "laugh"),
    ("hooray", "hooray"),
    ("confused", "confused"),
    ("heart", "heart"),
    ("rocket", "rocket"),
    ("eyes", "eyes"),
)


def _int_or_zero(value: Any) -> int:
    """Coerce a provider-supplied count to a non-negative int."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _safe_https_url(value: Any) -> str:
    """Keep only an https URL from provider-echoed link fields.

    Unlike the payload's own ``url`` (which comes from the validated ref), a
    linked change or comment permalink can only come from the provider, and it
    reaches an ``href`` in the browser. Restricting it to https drops
    ``javascript:``/``data:`` and any other scheme before it can be rendered as
    a link; a rejected value degrades to an empty string, which the frontend
    renders as plain text.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if text.lower().startswith("https://") else ""


def _issue_label(item: Any) -> dict[str, str]:
    """Normalize one label to the contract's ``{name, color, description}``.

    ``color`` is a BARE six-hex-digit string: GitHub already reports it that way
    and GitLab reports ``#rrggbb``, so the leading ``#`` is stripped here rather
    than left for the frontend to handle twice. GitLab also returns plain label
    NAMES unless ``with_labels_details`` is requested, so a bare string is
    accepted as a name-only label.
    """
    if isinstance(item, str):
        return {"name": item, "color": "", "description": ""}
    if not isinstance(item, dict):
        return {"name": "", "color": "", "description": ""}
    return {
        "name": str(item.get("name") or ""),
        "color": str(item.get("color") or "").lstrip("#"),
        "description": str(item.get("description") or ""),
    }


def _issue_labels(value: Any) -> list[dict[str, str]]:
    """Normalize a provider label list, tolerating GitLab's name-only form.

    ``_as_list`` cannot be reused here: it keeps only dict rows, which would
    silently drop every label from a GitLab reply that came back as bare
    strings. Nameless rows are dropped -- there is nothing to render.
    """
    if not isinstance(value, list):
        return []
    labels = [_issue_label(item) for item in value if isinstance(item, (str, dict))]
    return [label for label in labels if label["name"]]


def _issue_milestone(value: Any) -> dict[str, str] | None:
    """Normalize a milestone, or ``None`` when the issue has none."""
    if not isinstance(value, dict):
        return None
    return {
        "title": str(value.get("title") or ""),
        "state": str(value.get("state") or ""),
        # GitHub calls it due_on, GitLab due_date.
        "dueOn": str(value.get("due_on") or value.get("due_date") or ""),
    }
