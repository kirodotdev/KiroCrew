"""Whether a workflow's runs may be cancelled and re-run without a human.

``heal_safe_declared`` reads the declared sets (``HEAL_SAFE_WORKFLOWS`` and
``HEAL_SAFE_PULL_REQUEST_WORKFLOWS`` in ``runner_watchdog``); ``workflow_text_is_heal_safe``
is the derived gate over the workflow's own text (its concurrency group and any publish or
deploy step); ``workflow_is_heal_safe_at_revision`` applies both at one run's own revision;
and ``_mark_heal_exempt`` records a refusal on the run's verdict. ``runner_watchdog``
imports this module and re-exports every name in it.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from collections.abc import Iterator

from runner_watchdog import (
    _BRANCH_GROUP_SIGNAL,
    _PUBLISH_OR_DEPLOY_PATTERNS,
    _PULL_REQUEST_GROUP_SIGNAL,
    CANCELLED_ORPHAN,
    HEAL_EXEMPT,
    HEAL_SAFE_PULL_REQUEST_WORKFLOWS,
    HEAL_SAFE_WORKFLOWS,
    ORPHANED,
    SKIPPED_ATTEMPT_CAP,
    WATCHED_WORKFLOWS,
    Api,
    ApiError,
)
from runner_watchdog_parts.classify import RunVerdict


def _yaml_mapping_entries(text: str) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield simple block-style YAML keys with their mapping path and raw value."""
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+):(?P<value>.*)$", line)
        if match is None:
            continue
        indent = len(match.group("indent"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = match.group("key")
        yield tuple(item[1] for item in stack) + (key,), match.group("value").strip()
        stack.append((indent, key))


def _workflow_without_full_line_comments(raw: str) -> str:
    return "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))


def workflow_has_ref_or_pr_concurrency(text: str, *, event: str = "push") -> bool:
    """Whether the run-level concurrency group names a ref, or -- for a pull-request
    run -- a pull-request number.

    The event decides which key is enough. A push run's successor is judged by the
    branch listing, so its group must be ref-keyed. A pull-request run's successor is
    judged by head SHA against the open pull requests on its branch, which does not
    depend on the group at all, so the PR-number key is admitted for it; on a push
    that key is empty and the group a constant, so it is never admitted there.
    """
    groups = [
        value.partition("#")[0]
        for keys, value in _yaml_mapping_entries(text)
        if keys == ("concurrency", "group") or (keys == ("concurrency",) and value)
    ]
    signals: tuple[re.Pattern[str], ...] = (_BRANCH_GROUP_SIGNAL,)
    if event == "pull_request":
        signals = (_BRANCH_GROUP_SIGNAL, _PULL_REQUEST_GROUP_SIGNAL)
    return any(signal.search(group) for group in groups for signal in signals)


def workflow_text_has_publish_or_deploy_step(raw: str) -> bool:
    """Whether workflow text can publish, deploy, or sign durable output."""
    text = _workflow_without_full_line_comments(raw)
    if any(pattern.search(text) for pattern in _PUBLISH_OR_DEPLOY_PATTERNS):
        return True
    for keys, value in _yaml_mapping_entries(text):
        if len(keys) == 3 and keys[0] == "jobs" and keys[2] == "environment":
            return True
        permission = value.partition("#")[0].strip().strip("\"'")
        if (
            len(keys) >= 2
            and keys[-2] == "permissions"
            and keys[-1] in {"pages", "packages", "deployments"}
            and permission == "write"
        ):
            return True
    return bool(
        re.search(r"\buses:\s*docker/build-push-action@", text, re.IGNORECASE)
        and re.search(r"^\s*push:\s*true\s*(?:#.*)?$", text, re.IGNORECASE | re.MULTILINE)
    )


def workflow_text_is_heal_safe(raw: str, *, event: str = "push") -> bool:
    """Whether workflow text is branch-scoped (for this event) and free of durable side effects."""
    text = _workflow_without_full_line_comments(raw)
    return workflow_has_ref_or_pr_concurrency(
        text, event=event
    ) and not workflow_text_has_publish_or_deploy_step(text)


def heal_safe_declared(workflow: str, event: str) -> bool:
    """The DECLARED gate for one run: is this workflow declared heal-safe for this event.

    A ref-keyed declaration covers every event, pull-request runs included. A
    PR-keyed declaration covers pull-request runs only: on any other event the PR
    number is empty, so the workflow has no concurrency group and nothing protects
    a re-run of it from a live successor; such a run stays exempt.
    """
    if workflow in HEAL_SAFE_WORKFLOWS:
        return True
    return workflow in HEAL_SAFE_PULL_REQUEST_WORKFLOWS and event == "pull_request"


def heal_exempt_workflows(
    workflows: tuple[str, ...] = WATCHED_WORKFLOWS,
    heal_safe: frozenset[str] = HEAL_SAFE_WORKFLOWS | HEAL_SAFE_PULL_REQUEST_WORKFLOWS,
) -> frozenset[str]:
    """Watched workflows that require human recovery: everything declared safe for NO event.

    Classification reads the declaration alone. The YAML itself is read at the
    RUN'S OWN revision, by ``workflow_is_heal_safe_at_revision``, immediately
    before every mutation. Reading the tick's checkout here as well would judge a
    weaker subject: a branch can change a group or add publishing, and the
    checkout cannot see that. Anything the checkout read would have stopped, the
    revision read stops too, before a run is touched.

    A workflow declared safe for pull-request runs only is NOT in this set; its
    push runs are marked exempt per run by ``_mark_heal_exempt`` through
    ``heal_safe_declared``, which sees the event.
    """
    return frozenset(workflow for workflow in workflows if workflow not in heal_safe)


def workflow_is_heal_safe_at_revision(
    api: Api, repo: str, workflow: str, head_sha: str, *, event: str = "push"
) -> bool | None:
    """Whether a workflow is heal-safe AT ONE RUN'S OWN revision, for that run's event.

    Three answers, because a declared-unsafe workflow and one whose safety cannot
    be established are not the same fact and must not share an outcome. ``False``
    means read and judged unsafe, or declared unsafe with no read needed: a human
    recovers it, and the tick is healthy. ``None`` means undeterminable -- no
    ``head_sha``, or the contents read or its decoding failed -- which is a FAILED
    outcome, so a run whose safety nobody could establish cannot age out of its
    recovery window behind a green tick.

    Either way nothing is cancelled or re-run: only ``True`` admits a mutation.
    """
    if not heal_safe_declared(workflow, event):
        return False
    if not head_sha:
        return None
    query = urllib.parse.urlencode({"ref": head_sha})
    encoded_workflow = urllib.parse.quote(workflow, safe="")
    path = f"repos/{repo}/contents/.github/workflows/{encoded_workflow}?{query}"
    try:
        payload = api.get(path)
    except ApiError:
        return None
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    try:
        encoded = "".join(content.split())
        raw = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (UnicodeError, ValueError):
        return None
    return workflow_text_is_heal_safe(raw, event=event)


def _mark_heal_exempt(verdict: RunVerdict, exempt_workflows: frozenset[str]) -> RunVerdict:
    # The attempt cap is included deliberately. That cap exists to stop US looping
    # re-runs, and an exempt workflow is never re-run by us at all, so the cap is
    # not the operative reason nobody touched the run -- and reporting it instead
    # would leave a stuck orphan behind a GREEN tick, since the attempt cap carries
    # no outcome. The label the summary shows becomes `heal-exempt`; the attempt is
    # kept in the detail so the escalation reason is not lost. Forks are left alone:
    # nobody's token can re-run one, which is a more specific answer than exemption.
    # A workflow declared safe for pull-request runs only is exempt on its push runs
    # (`heal_safe_declared`), so the event is consulted alongside the set.
    exempt = verdict.workflow in exempt_workflows or not heal_safe_declared(
        verdict.workflow, verdict.event
    )
    if exempt and verdict.verdict in (
        ORPHANED,
        CANCELLED_ORPHAN,
        SKIPPED_ATTEMPT_CAP,
    ):
        if verdict.verdict == SKIPPED_ATTEMPT_CAP and not verdict.orphans:
            return verdict
        at_cap = (
            f" It is also at attempt {verdict.run_attempt}, the watchdog's cap."
            if verdict.verdict == SKIPPED_ATTEMPT_CAP
            else ""
        )
        verdict.verdict = HEAL_EXEMPT
        verdict.detail = (
            "the workflow is not heal-safe: its run concurrency is not keyed to a ref or pull "
            "request, or it publishes, deploys, or signs durable output; automatic cancel and "
            f"full re-run are disabled, so a human must recover this orphan.{at_cap}"
        )
    return verdict
