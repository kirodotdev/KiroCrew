"""Reconcile persisted posting operations before retrying an external create."""

from __future__ import annotations

import copy
from pathlib import Path
from uuid import uuid4

from sage_lib import adapters, discovery, pipeline, results
from sage_lib import review_driver as driver
from sage_lib import store
from sage_lib.delivery_intent import (
    _OPERATION_ID_RE,
    DeliveryProbe,
    _canonical_target,
    _intent_payload,
    _marker_free_body,
    _operation_marker,
    _outbound_payload,
    _payload_digest,
    _write_intent,
)


def _remote_payload(
    review: dict,
    comments: list[dict],
    *,
    host,
    owner: str,
    repo: str,
    number,
    pinned: tuple[str, str] | None,
) -> dict | None:
    """Project GitHub's review echo into the canonical outbound-payload shape."""
    projected = []
    lines_at = None
    for comment in comments:
        path = str(comment.get("path") or "")
        line = driver._resolved_line(comment)
        side = comment.get("side") or "RIGHT"
        if line is None:
            if lines_at is None:
                positions = driver._diff_positions(
                    host, owner, repo, number, str(review.get("commit_id") or ""), pinned
                )
                if positions is None:
                    return None
                lines_at = {
                    p: {pos: ln for ln, pos in mapping.items()} for p, mapping in positions.items()
                }
            position = comment.get("position")
            if not isinstance(position, int) or isinstance(position, bool):
                return None
            line = lines_at.get(path, {}).get(position)
            if line is None or side != "RIGHT":
                return None
        projected.append(
            {"path": path, "line": line, "side": side, "body": comment.get("body") or ""}
        )
    return {
        "body": review.get("body") or "",
        "commit_id": review.get("commit_id") or "",
        "comments": projected,
    }


def _authenticated_login(*, host: str) -> str | None:
    """The `gh` login this host posts as, or None when it cannot be determined."""
    try:
        return discovery.current_login(host=host)
    except (discovery.GhError, discovery.GhSetupError, OSError):
        return None


def _probe_delivery(link: str, intent: dict, payload: dict) -> tuple[DeliveryProbe, str, str]:
    """Reconcile an operation against GitHub without mutating remote state."""
    operation_id = intent.get("operation_id")
    if not isinstance(operation_id, str) or not _OPERATION_ID_RE.fullmatch(operation_id):
        return DeliveryProbe.CONFLICT, "", "delivery intent has invalid operation ID"
    predecessor = intent.get("predecessor") if isinstance(intent.get("predecessor"), dict) else None
    predecessor_operation_id = (predecessor or {}).get("operation_id")
    if predecessor is not None and (
        not isinstance(predecessor_operation_id, str)
        or not _OPERATION_ID_RE.fullmatch(predecessor_operation_id)
    ):
        return DeliveryProbe.CONFLICT, "", "delivery predecessor has invalid operation ID"
    try:
        host, owner, repo, number = adapters.github_pr_ref(link)
    except adapters.AdapterError as exc:
        return DeliveryProbe.CONFLICT, "", f"delivery target is no longer valid: {exc}"
    if _canonical_target(link) != str(intent.get("target") or ""):
        return DeliveryProbe.CONFLICT, "", "delivery target differs from the prepared intent"
    expected_revision = str(intent.get("revision") or "")
    if not expected_revision or expected_revision != str(payload.get("commit_id") or ""):
        return DeliveryProbe.CONFLICT, "", "delivery revision differs from the prepared intent"
    try:
        reviews = discovery.run_gh_json(
            f"repos/{owner}/{repo}/pulls/{number}/reviews", jq=".[]", paginate=True, host=host
        )
    except (discovery.GhError, discovery.GhSetupError, OSError) as exc:
        return DeliveryProbe.UNKNOWN, "", f"could not read GitHub delivery state: {exc}"
    marker = _operation_marker(operation_id)
    predecessor_id = str((predecessor or {}).get("review_id") or "")
    predecessor_marker = _operation_marker(str(predecessor_operation_id)) if predecessor else ""
    predecessor_digest = str((predecessor or {}).get("payload_digest") or "")
    matches = [review for review in reviews if marker in str(review.get("body") or "")]
    if len(matches) > 1:
        return DeliveryProbe.CONFLICT, "", "multiple reviews match the operation marker"
    found_id = ""
    predecessor_seen = False
    predecessor_submitted = False
    for review in sorted(reviews, key=lambda r: marker not in str(r.get("body") or "")):
        review_id = str(review.get("id") or "")
        body = str(review.get("body") or "")
        pinned = None
        if marker in body or (review_id == predecessor_id and predecessor_marker in body):
            try:
                pinned = driver._pull_revisions(host, owner, repo, number)
            except (discovery.GhError, discovery.GhSetupError, OSError):
                pass
        if marker in body:
            try:
                comments = discovery.run_gh_json(
                    f"repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/comments",
                    jq=".[]",
                    paginate=True,
                    host=host,
                )
            except (discovery.GhError, discovery.GhSetupError, OSError) as exc:
                return DeliveryProbe.UNKNOWN, "", f"could not read posted review {review_id}: {exc}"
            remote = _remote_payload(
                review, comments, host=host, owner=owner, repo=repo, number=number, pinned=pinned
            )
            if remote is None:
                return DeliveryProbe.UNKNOWN, "", "could not resolve posted review coordinates"
            if _payload_digest(remote) != str(intent.get("payload_digest") or ""):
                return (
                    DeliveryProbe.CONFLICT,
                    "",
                    "operation marker matched a review with different payload",
                )
            # Matching body and payload prove the CONTENT is ours, not that we
            # posted it. The marker is readable by anyone who can read this PR,
            # and the record carrying the intent is worker-writable, so content
            # alone would let a review posted by another account be adopted as
            # this operation's own delivery. Checked here rather than at the
            # marker, so a genuine payload mismatch still reports as the
            # conflict it is.
            author = str(((review.get("user") or {}).get("login") or ""))
            login = driver._authenticated_login(host=host)
            if login is None:
                return (
                    DeliveryProbe.UNKNOWN,
                    "",
                    "could not confirm the posting account for a matched review",
                )
            if author.casefold() != login.casefold():
                return (
                    DeliveryProbe.CONFLICT,
                    "",
                    "operation marker matched a review posted by another account",
                )
            found_id = review_id
            continue
        if found_id and str(review.get("state") or "") != "PENDING":
            continue
        if review_id == predecessor_id:
            if predecessor_marker not in body:
                return DeliveryProbe.CONFLICT, "", "known predecessor no longer has its marker"
            try:
                comments = discovery.run_gh_json(
                    f"repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/comments",
                    jq=".[]",
                    paginate=True,
                    host=host,
                )
            except (discovery.GhError, discovery.GhSetupError, OSError) as exc:
                return (
                    DeliveryProbe.UNKNOWN,
                    "",
                    f"could not read predecessor review {review_id}: {exc}",
                )
            remote = _remote_payload(
                review, comments, host=host, owner=owner, repo=repo, number=number, pinned=pinned
            )
            if remote is None:
                return (
                    DeliveryProbe.UNKNOWN,
                    "",
                    f"Could not resolve predecessor review {review_id} against the current diff. "
                    "Submit or delete that draft on GitHub, then retry Post. Nothing was resent.",
                )
            if _payload_digest(remote) != predecessor_digest:
                return (
                    DeliveryProbe.CONFLICT,
                    "",
                    "known predecessor no longer matches its recorded payload",
                )
            if str(review.get("state") or "") != "PENDING":
                login = driver._authenticated_login(host=host)
                if login is None:
                    return DeliveryProbe.UNKNOWN, "", "could not confirm the predecessor account"
                author = str((review.get("user") or {}).get("login") or "")
                if author.casefold() != login.casefold():
                    return DeliveryProbe.CONFLICT, "", "predecessor was posted by another account"
                if str(review.get("state") or "") not in {
                    "COMMENTED",
                    "APPROVED",
                    "CHANGES_REQUESTED",
                    "DISMISSED",
                }:
                    return DeliveryProbe.UNKNOWN, "", "could not confirm the predecessor state"
                predecessor_submitted = True
                continue
            # The verified content rides to the poster, which re-reads the
            # review and compares it to this immediately before any DELETE: a
            # human can edit the pending draft between this probe and that call.
            if predecessor is not None:
                predecessor["payload"] = {
                    "body": remote.get("body") or "",
                    "comments": [
                        {
                            "path": c.get("path"),
                            "line": c.get("line"),
                            "side": c.get("side"),
                            "position": c.get("position"),
                            "body": c.get("body") or "",
                        }
                        for c in comments
                    ],
                }
            predecessor_seen = True
            continue
        # A pending review that is not our recorded predecessor is not the
        # probe's business to block on: the driver has no receipt for it, so it
        # must never tell the poster to delete it. The POST itself fails with
        # 422 when such a draft is in the way, and the poster's step 2 tells it
        # to stop and report that failure instead of deleting anything.
    if found_id:
        if predecessor_seen:
            return DeliveryProbe.CONFLICT, "", "multiple pending drafts block confirmation"
        return DeliveryProbe.FOUND, found_id, ""
    if predecessor_submitted:
        return DeliveryProbe.PREDECESSOR_SUBMITTED, predecessor_id, ""
    if predecessor and not predecessor_seen:
        intent.pop("predecessor", None)
    return DeliveryProbe.ABSENT, "", ""


def _post_recorded(
    change_id: str,
    link: str,
    *,
    dispatch,
    root: Path | None = None,
    run_id: str | None = None,
    timeout: float | None = None,
    keys: list[str] | None = None,
    dismissed_keys: set[str] | None = None,
    confirm=None,
    accepted_record: dict | None = None,
) -> dict:
    """Publish a recorded review through a durable, reconcilable delivery intent."""
    if timeout is None:
        timeout = driver.DEFAULT_TASK_TIMEOUT
    cur = (
        copy.deepcopy(accepted_record)
        if accepted_record is not None
        else results.read_result(change_id, root, run_id)
    )
    if not cur:
        return {
            "post_ok": True,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": 0,
            "post_error": "no recorded review for this change",
        }
    all_entries = pipeline.build_pending_comments(cur)
    previous = cur.get("delivery_intent")
    recovering = isinstance(previous, dict) and previous.get("state") in {
        "prepared",
        "attempting",
        "indeterminate",
    }
    pending = cur.get("pending_comments", []) if isinstance(previous, dict) else []
    if not isinstance(pending, list) or any(not isinstance(entry, dict) for entry in pending):
        error = (
            "delivery intent saved comments must be a list of objects; restore the saved selection"
        )
        _write_intent(cur, previous, "indeterminate", root=root, run_id=run_id, error=error)
        return {
            "post_ok": False,
            "post_error": error,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": len(all_entries),
            "expected_units": 0,
            "posted_keys": [],
        }
    recorded_entries = {str(entry.get("key")): entry for entry in pending}
    already = (
        {
            str(entry.get("key"))
            for entry in all_entries
            if entry == recorded_entries.get(str(entry.get("key")))
            and str(entry.get("key")) in (cur.get("posted_keys") or [])
        }
        if isinstance(previous, dict)
        else set(cur.get("posted_keys") or [])
    )
    wanted = set(keys) if keys is not None else {str(entry.get("key")) for entry in all_entries}
    wanted -= dismissed_keys or set()
    new = [
        entry
        for entry in all_entries
        if str(entry.get("key")) in wanted and str(entry.get("key")) not in already
    ]
    try:
        target = _canonical_target(link)
    except adapters.AdapterError as exc:
        if "://" in str(link or ""):
            # A URL-shaped link that is not a revalidatable GitHub PR link must
            # abort fail-closed: posting through it could address a host this
            # deployment has never vetted.
            error = f"refusing to post: {exc}"
            cur["post_ok"] = False
            cur["post_error"] = error
            driver._record_post_refusal(cur, root, run_id)
            return {
                "post_ok": False,
                "post_error": error,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": 0,
                "posted_keys": sorted(already),
            }
        # Not URL-shaped at all: a bare change id (tests, or a record that
        # predates the link field). run_review entries are PR links in
        # production, and the poster prompt builder still fails closed on any
        # host it cannot revalidate, so carrying the opaque target through
        # changes nothing for real links.
        target = str(link or "")

    reconciled_absent = False
    submitted_predecessor = False
    if isinstance(previous, dict) and previous.get("state") in {
        "prepared",
        "attempting",
        "indeterminate",
    }:
        try:
            rebuilt = pipeline.build_github_review_payload(cur)
            wire = _outbound_payload(rebuilt, previous["operation_id"])
            error = ""
            if _payload_digest(wire) != previous.get("payload_digest"):
                wire, error = None, "redacted saved selection no longer matches delivery intent"
        except (KeyError, ValueError, TypeError) as exc:
            wire, error = None, f"could not rebuild saved delivery: {exc}"
        if wire is None:
            _write_intent(cur, previous, "indeterminate", root=root, run_id=run_id, error=error)
            return {
                "post_ok": False,
                "post_error": error,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": int(previous.get("selected_units") or 0),
                "posted_keys": sorted(already),
            }
        cur["github_review_payload"] = rebuilt
        probe, review_id, error = driver._probe_delivery(link, previous, wire)
        if probe is DeliveryProbe.FOUND:
            cur["posted_keys"] = sorted(
                already | {str(key) for key in previous.get("selected_keys") or []}
            )
            cur["posted_comments"] = int(previous.get("selected_units") or 0)
            cur.pop("post_partial", None)
            # The VISIBLE payload decides, as in the fresh-dispatch path below:
            # `wire` always carries the operation marker, so `wire.get("body")`
            # would claim a design comment even for a comments-only delivery.
            cur["design_comment_posted"] = bool(
                _marker_free_body(cur["github_review_payload"].get("body")).strip()
            )
            cur["posted_review_id"] = review_id
            _write_intent(cur, previous, "confirmed", root=root, run_id=run_id, review_id=review_id)
            requested_pending = sorted(
                str(entry["key"]) for entry in new if str(entry["key"]) not in cur["posted_keys"]
            )
            return {
                "post_ok": not requested_pending,
                "post_error": (
                    "Earlier delivery reconciled; requested selection is still pending. "
                    "Post the remaining selection again."
                    if requested_pending
                    else ""
                ),
                "posted_comments": cur["posted_comments"],
                "design_comment_posted": cur["design_comment_posted"],
                "pending": len(pending),
                "expected_units": cur["posted_comments"],
                "posted_keys": cur["posted_keys"],
                "posted_review_id": review_id,
            }
        if probe not in {DeliveryProbe.ABSENT, DeliveryProbe.PREDECESSOR_SUBMITTED}:
            _write_intent(cur, previous, "indeterminate", root=root, run_id=run_id, error=error)
            return {
                "post_ok": False,
                "post_error": error,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": int(previous.get("selected_units") or 0),
                "posted_keys": sorted(already),
            }
        if set(previous.get("selected_keys") or []) & (dismissed_keys or set()):
            return _failure(
                "The interrupted post includes dismissed findings. Undo their dismissal "
                "and reconcile that post before choosing another selection. Nothing was resent.",
                already,
            )
        _write_intent(cur, previous, "prepared", root=root, run_id=run_id)
        intent = previous
        submitted_predecessor = probe is DeliveryProbe.PREDECESSOR_SUBMITTED
        reconciled_absent = True
    else:
        if not new:
            return {
                "post_ok": True,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": 0,
                "posted_keys": sorted(already),
                "post_error": "nothing left to post" if all_entries else "",
            }

        predecessor = None
        if isinstance(previous, dict) and previous.get("state") == "confirmed":
            previous_operation_id = previous.get("operation_id")
            if not isinstance(previous_operation_id, str) or not _OPERATION_ID_RE.fullmatch(
                previous_operation_id
            ):
                error = "delivery intent has invalid operation ID"
                _write_intent(cur, previous, "indeterminate", root=root, run_id=run_id, error=error)
                return {
                    "post_ok": False,
                    "post_error": error,
                    "posted_comments": 0,
                    "design_comment_posted": False,
                    "pending": len(new),
                    "expected_units": int(previous.get("selected_units") or 0),
                    "posted_keys": sorted(already),
                }
            predecessor_id = str(cur.get("posted_review_id") or previous.get("review_id") or "")
            if predecessor_id:
                predecessor = {
                    "review_id": predecessor_id,
                    "operation_id": previous_operation_id,
                    "payload_digest": str(previous.get("payload_digest") or ""),
                }
        replaces = (
            {str(key) for key in previous.get("selected_keys") or []}
            if isinstance(previous, dict) and predecessor
            else {str(key) for key in cur.get("posted_keys") or []} - already
        )
        if not isinstance(previous, dict):
            replaces = already
        selected = {str(entry.get("key")) for entry in new} | replaces
        pending = [entry for entry in all_entries if str(entry.get("key")) in selected]
        cur["pending_comments"] = pending
        try:
            cur["github_review_payload"] = pipeline.build_github_review_payload(cur)
        except ValueError as exc:
            cur["post_ok"] = False
            cur["post_error"] = str(exc)
            driver._record_post_refusal(cur, root, run_id)
            return {
                "post_ok": False,
                "post_error": str(exc),
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": 0,
                "posted_keys": sorted(already),
            }
        operation_id = uuid4().hex
        wire = _outbound_payload(cur["github_review_payload"], operation_id)
        intent = {
            "operation_id": operation_id,
            "target": target,
            "revision": str(wire.get("commit_id") or ""),
            "payload_digest": _payload_digest(wire),
            "selected_keys": sorted(str(entry.get("key")) for entry in pending),
            "selected_units": pipeline.review_payload_units(cur["github_review_payload"]),
            "state": "prepared",
        }
        if predecessor:
            intent["predecessor"] = predecessor
        cur["posted_comments"] = 0
        cur["design_comment_posted"] = False
        cur.pop("posted_review_id", None)
        _write_intent(cur, intent, "prepared", root=root, run_id=run_id)

    wire, error = _intent_payload(cur, intent)
    if wire is None:
        _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=error)
        return {
            "post_ok": False,
            "post_error": error,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": len(pending),
            "expected_units": int(intent.get("selected_units") or 0),
            "posted_keys": sorted(already),
        }
    if confirm is None and not reconciled_absent:
        probe, _review_id, error = driver._probe_delivery(link, intent, wire)
        submitted_predecessor = probe is DeliveryProbe.PREDECESSOR_SUBMITTED
        if probe not in {DeliveryProbe.ABSENT, DeliveryProbe.PREDECESSOR_SUBMITTED}:
            message = error or "delivery intent is already present on GitHub"
            _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=message)
            return {
                "post_ok": False,
                "post_error": message,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": int(intent.get("selected_units") or 0),
                "posted_keys": sorted(already),
            }
    if submitted_predecessor:
        try:
            # Rebuild from the saved selection, never a retry's new selection.
            # Its digest must still prove the entry-to-payload correspondence.
            rebuilt = pipeline.build_github_review_payload(cur)
            if _payload_digest(rebuilt) != intent["payload_digest"]:
                raise ValueError("saved selection no longer matches the prepared delivery")
        except ValueError as exc:
            error = f"could not reconcile submitted predecessor: {exc}"
            _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=error)
            return {
                "post_ok": False,
                "post_error": error,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": int(intent.get("selected_units") or 0),
                "posted_keys": sorted(already),
            }
        intent.pop("predecessor", None)
        _write_intent(cur, intent, "prepared", root=root, run_id=run_id)
    cur["posted_comments"] = 0
    cur.pop("post_partial", None)
    _write_intent(cur, intent, "attempting", root=root, run_id=run_id)
    staged_ok = True
    try:
        results.write_result({**cur, "github_review_payload": wire}, root, None)
    except (OSError, ValueError, RecursionError):
        staged_ok = False
    if not staged_ok:
        error = "could not stage the prepared delivery intent for the poster"
        _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=error)
        return {
            "post_ok": False,
            "post_error": error,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": len(pending),
            "expected_units": int(intent.get("selected_units") or 0),
            "posted_keys": sorted(already),
        }
    try:
        post_prompt = driver.build_post_task(
            link, intent["operation_id"], intent.get("predecessor"), recovering=recovering
        )
    except (adapters.AdapterError, ValueError) as exc:
        error = f"refusing to post: {exc}"
        _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=error)
        return {
            "post_ok": False,
            "post_error": error,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": len(pending),
            "expected_units": int(intent.get("selected_units") or 0),
            "posted_keys": sorted(already),
        }

    # The poster owns the shared record, so its write-back cannot alter the
    # intent or wire payload that proves this dispatch reached GitHub.
    dispatched_intent = copy.deepcopy(intent)
    dispatched_wire = copy.deepcopy(wire)
    try:
        spawn = dispatch(post_prompt, timeout)
    except (OSError, TimeoutError) as exc:
        spawn = {"ok": False, "error": store.redact_text(str(exc))}
    staged = results.read_result(change_id, root, None) or {}
    reported = staged.get("posted_comments") if staged.get("change_id") == change_id else None
    reported_count = reported if type(reported) is int and reported >= 0 else 0
    after = cur
    # Restore accepted findings and driver intent before read-back. The poster's
    # only admitted metadata is an advisory nonnegative integer count.
    after["posted_comments"] = reported_count
    try:
        results.write_result(after, root, run_id)
    except (OSError, ValueError, RecursionError) as exc:
        return _failure(f"could not restore accepted review record: {exc}", already)
    if confirm is not None:
        confirmed_id = str(confirm(link, dispatched_wire) or "")
        probe = DeliveryProbe.FOUND if confirmed_id else DeliveryProbe.UNKNOWN
        error = (
            "" if confirmed_id else "the posted draft could not be confirmed on the pull request"
        )
    else:
        probe, confirmed_id, error = driver._probe_delivery(
            link, dispatched_intent, dispatched_wire
        )
    if probe is DeliveryProbe.FOUND:
        delivered_keys = sorted(already | {str(key) for key in intent.get("selected_keys") or []})
        after["posted_keys"] = delivered_keys
        after["posted_comments"] = int(intent.get("selected_units") or 0)
        after["design_comment_posted"] = bool(
            _marker_free_body(cur["github_review_payload"].get("body")).strip()
        )
        after["posted_review_id"] = confirmed_id
        try:
            _write_intent(
                after,
                dispatched_intent,
                "confirmed",
                root=root,
                run_id=run_id,
                review_id=confirmed_id,
            )
        except (OSError, ValueError, RecursionError) as exc:
            return _failure(f"could not record confirmed delivery: {exc}", already)
        requested_pending = sorted(
            str(entry["key"]) for entry in new if str(entry["key"]) not in delivered_keys
        )
        return {
            "post_ok": not requested_pending,
            "post_error": (
                "Earlier delivery reconciled; requested selection is still pending. "
                "Post the remaining selection again."
                if requested_pending
                else ""
            ),
            "posted_comments": after["posted_comments"],
            "design_comment_posted": after["design_comment_posted"],
            "pending": len(pending),
            "expected_units": after["posted_comments"],
            "posted_keys": delivered_keys,
            "posted_review_id": confirmed_id,
        }
    error = (
        str(spawn.get("error") or "")
        or error
        or "the posted draft could not be confirmed on the pull request"
    )
    after["posted_comments"] = reported_count
    if spawn.get("ok") and reported_count:
        after["post_partial"] = True
    after["design_comment_posted"] = False
    after["posted_review_id"] = ""
    _write_intent(after, dispatched_intent, "indeterminate", root=root, run_id=run_id, error=error)
    return {
        "post_ok": False,
        "post_error": error,
        "posted_comments": reported_count,
        "design_comment_posted": False,
        "pending": len(pending),
        "expected_units": int(intent.get("selected_units") or 0),
        "posted_keys": sorted(already),
        "posted_review_id": "",
    }


def _failure(error: str, already=()) -> dict:
    return {
        "post_ok": False,
        "post_error": store.redact_text(error),
        "posted_comments": 0,
        "design_comment_posted": False,
        "pending": 0,
        "expected_units": 0,
        "posted_keys": sorted(already),
    }


def post_recorded(*args, **kwargs) -> dict:
    """Keep persistence refusals local to one change and redact surfaced errors."""
    try:
        outcome = _post_recorded(*args, **kwargs)
    except (OSError, ValueError, RecursionError) as exc:
        return _failure(f"could not persist delivery state: {exc}")
    outcome["post_error"] = store.redact_text(str(outcome.get("post_error") or ""))
    return outcome
