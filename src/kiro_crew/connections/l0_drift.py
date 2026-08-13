"""Consecutive-failure accounting for the nightly L0 metadata probe.

A public metadata endpoint can fail for reasons that are not drift: a CDN blip,
a rate limit, a rolling deploy serving a half-written document for a minute.
Treating any single failure as drift makes the nightly job a coin flip, and a
job that cries wolf gets muted -- which costs more than the check is worth. So
each provider carries a streak of consecutive failing runs and only reports
drift once it has failed ``threshold`` runs in a row.

State travels between runs as a build ARTIFACT rather than a committed file. An
artifact needs no repository write permission, so a nightly job that makes
outbound network requests never gets ``contents: write`` and never pushes a bot
commit to a protected branch; it is also workflow bookkeeping rather than a fact
about the product, so it does not belong in the source tree churning ``main``.

Losing the artifact is fail-safe -- streaks restart at zero, which errs toward
green -- but it must never be SILENT, because a discarded file resets a
two-night streak and hides the third night that would have gone red. So every
load reports whether a file was present and, if it was unusable, why: the caller
prints that reason and puts it in the report.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple, TypedDict

from kiro_crew.atomic_write import atomic_write

DEFAULT_DRIFT_THRESHOLD = 3
STATE_SCHEMA_VERSION = 2
# A provider broken for years needs no bigger integer than one broken for a
# month: both are past the threshold. Clamping keeps the carried state small.
_MAX_STREAK = 99


class StateLoad(NamedTuple):
    """Carried streaks plus why they are (or are not) the previous run's.

    ``discarded`` is ``None`` when there was simply no prior state -- a first
    run, a renamed workflow -- and a reason string when a file WAS present but
    could not be used. Only the second case is a lost streak.
    """

    streaks: dict[str, int]
    loaded: bool
    discarded: str | None
    recorded_at: str | None


class DriftVerdict(TypedDict):
    """Whether the run reports drift, and the evidence for that call."""

    threshold: int
    prior_state_loaded: bool
    prior_state_recorded_at: str | None
    prior_state_discarded: str | None
    streaks: dict[str, int]
    drifted: list[str]
    ok: bool


def _digest(streaks: Mapping[str, int]) -> str:
    """Bind the streaks to the document that carries them.

    An INTEGRITY check, not authentication: there is no secret here, so it
    detects a truncated or half-written artifact, not a deliberately forged one.
    Forgery is not in scope -- anything that can rewrite the artifact can also
    rewrite the digest -- but a transfer that drops half the file is exactly what
    silently resets a streak, and that this catches.
    """

    canonical = json.dumps(dict(sorted(streaks.items())), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_streaks(path: Path | None, *, prior_run_expected: bool = False) -> StateLoad:
    """Read carried streaks, reporting whether they came from the previous run.

    Every failure mode degrades to "no prior state" rather than raising: the
    probe reports on providers and must not go red because its own bookkeeping
    file was lost or truncated in transit. Each one names itself in
    ``discarded`` so the loss is visible instead of looking like a first run.

    ``prior_run_expected`` closes the one gap that silence could still hide. An
    ABSENT file means two very different things -- nothing has ever run, or a
    previous run's artifact expired or failed to download -- and the second one
    resets a streak that was two nights deep. The workflow knows which case it is
    (it looked for a prior run before trying the download) and passes it here, so
    an expected-but-missing artifact is reported as ``artifact_missing`` while a
    genuine first run stays ``None``.
    """

    if path is None or not path.is_file():
        return StateLoad({}, False, "artifact_missing" if prior_run_expected else None, None)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        return StateLoad({}, False, f"state file could not be parsed: {error}", None)
    if not isinstance(raw, dict):
        return StateLoad({}, False, "state file is not an object", None)
    if raw.get("schema_version") != STATE_SCHEMA_VERSION:
        return StateLoad(
            {}, False, f"state schema {raw.get('schema_version')!r} is not supported", None
        )
    streaks = raw.get("streaks")
    if not isinstance(streaks, dict):
        return StateLoad({}, False, "state file has no streaks object", None)

    loaded: dict[str, int] = {}
    for slug, value in streaks.items():
        # bool is an int subclass; True would otherwise read as a streak of 1.
        if not isinstance(slug, str) or isinstance(value, bool):
            continue
        if not isinstance(value, int) or value < 0:
            continue
        loaded[slug] = min(value, _MAX_STREAK)

    recorded_at = raw.get("updated_at")
    if not isinstance(recorded_at, str):
        recorded_at = None
    expected = raw.get("digest")
    if not isinstance(expected, str) or expected != _digest(loaded):
        return StateLoad({}, False, "state digest does not match its streaks", recorded_at)
    return StateLoad(loaded, True, None, recorded_at)


def update_streaks(previous: Mapping[str, int], outcomes: Mapping[str, bool]) -> dict[str, int]:
    """Advance streaks for this run's ``outcomes`` (slug -> passed).

    Keyed only by the slugs in ``outcomes``, so a provider dropped from the
    registry leaves the carried state instead of accumulating forever, and a new
    provider starts at zero rather than inheriting a reused slug's streak.
    """

    return {
        slug: 0 if passed else min(previous.get(slug, 0) + 1, _MAX_STREAK)
        for slug, passed in outcomes.items()
    }


def verdict(
    streaks: Mapping[str, int],
    *,
    threshold: int,
    prior: StateLoad,
) -> DriftVerdict:
    """Decide whether any provider has failed often enough to report drift."""

    if threshold < 1:
        raise ValueError("threshold must be at least 1")
    drifted = sorted(slug for slug, streak in streaks.items() if streak >= threshold)
    return {
        "threshold": threshold,
        "prior_state_loaded": prior.loaded,
        "prior_state_recorded_at": prior.recorded_at,
        "prior_state_discarded": prior.discarded,
        "streaks": dict(sorted(streaks.items())),
        "drifted": drifted,
        "ok": not drifted,
    }


def write_state(path: Path, streaks: Mapping[str, int]) -> None:
    """Persist streaks for the next run to pick up."""

    ordered = dict(sorted(streaks.items()))
    document: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "digest": _digest(ordered),
        "streaks": ordered,
    }
    # atomic_write, not write_text: an in-place write truncates the destination
    # first, so a full disk or an interrupt mid-write publishes a half-written
    # state file as the artifact. The next run cannot tell that apart from a
    # tampered one, discards it, and a two-night streak silently restarts at zero
    # -- which loses the third night that would have reported drift. The helper
    # renames a temp file from the same directory into place, so the artifact
    # collected afterwards is either the whole previous state or the whole new
    # one. fsync because the rename must not publish a temp whose bytes have not
    # reached disk.
    #
    # No newline="" here, unlike the registry writer: this document is parsed,
    # never read back and rewritten byte-for-byte, and the digest covers the
    # parsed streaks rather than the raw bytes, so newline translation is
    # invisible to every reader.
    atomic_write(path, json.dumps(document, indent=2, sort_keys=True) + "\n", fsync=True)
