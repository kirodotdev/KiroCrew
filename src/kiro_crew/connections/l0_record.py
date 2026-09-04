"""Write L0 baselines captured from a live fetch back into the provider registry.

The registry's ``l0_expectations`` are what the nightly probe diffs against, so
how they get written matters more than the values themselves. ``l0_probe
--record`` runs the same unauthenticated fetch the nightly probe runs and writes
back what the provider actually advertised.

What that does and does NOT guarantee, stated plainly because the obvious reading
is too strong: ``verified_on`` is a REFRESH MARKER, not proof. It is written here
from a date taken at capture time, but a date in a file is self-attested and a
determined human can type one. The real guarantee is the nightly probe --
it re-derives every value from the live provider and trips the drift gate when
the file disagrees, whatever the date claims. The stamp exists so a baseline
nobody has re-derived in months gets noticed.

Stamp churn is intentional. Every successful capture rewrites ``verified_on`` for
every provider it reached, including ones whose values did not change, because
the point of the field is "when was this last re-derived from the provider" --
not "when did this value last change". A confirming capture on a later day is
exactly the refresh the freshness tiers are asking for, and it SHOULD move the
date. (A capture on the SAME day as the committed stamp changes nothing and
writes nothing.)

An ISSUER CHANGE is never written. ``apply_baselines`` refuses to move
``authorization_server`` and reports the provider for human approval instead. The
recorder is not allowed to redirect the one URL it is permitted to fetch: an
issuer the recorder accepted on the provider's word would be an issuer an
attacker could relocate, and moving it is a decision that belongs in a reviewed
commit. DCR and PKCE refresh freely -- they are booleans read from a document
served by the already-approved issuer.

Rewriting is style-preserving on purpose. ``registry.json`` is one compact object
per line, which is what keeps a provider edit to a one-line diff a reviewer can
read; reformatting would rewrite every line of a security-relevant file.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence, TypedDict

from kiro_crew.atomic_write import atomic_write


class ObservedBaseline(TypedDict):
    """What a live provider advertised, as captured by the probe."""

    authorization_server: str
    dcr: bool
    pkce: bool


class RecordOutcome(NamedTuple):
    """What one capture changed, and what it refused to change.

    ``needs_approval`` maps slug -> the issuer the provider advertised, for every
    provider whose advertised issuer differs from the committed one. Those
    entries are left untouched in the file.
    """

    changed: list[str]
    needs_approval: dict[str, str]


class RecordError(RuntimeError):
    """Raised when the registry could not be read or rewritten safely."""


def render_registry(providers: Sequence[Mapping[str, Any]]) -> str:
    """Serialize the registry in its committed one-object-per-line form."""

    lines = [json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in providers]
    return "[\n" + ",\n".join(f"  {line}" for line in lines) + "\n]\n"


def apply_baselines(
    providers: Sequence[dict[str, Any]],
    observed: Mapping[str, ObservedBaseline],
    captured_on: date,
) -> RecordOutcome:
    """Refresh ``l0_expectations`` in place for every slug in ``observed``.

    Providers absent from ``observed`` are left untouched, which is how a failed
    capture is handled: the stale baseline stays put to age into the freshness
    tiers rather than being overwritten from a half-answered fetch.

    A provider whose advertised issuer differs from the committed one is also
    left untouched and returned in ``needs_approval`` -- including its stamp, so
    an unapproved issuer change cannot buy itself another 90 days of silence.
    """

    stamp = captured_on.isoformat()
    changed: list[str] = []
    needs_approval: dict[str, str] = {}
    for provider in providers:
        slug = provider.get("slug")
        if not isinstance(slug, str) or slug not in observed:
            continue
        capture = observed[slug]
        committed = provider.get("l0_expectations")
        committed_issuer = (
            committed.get("authorization_server") if isinstance(committed, dict) else None
        )
        if committed_issuer is not None and capture["authorization_server"] != committed_issuer:
            needs_approval[slug] = capture["authorization_server"]
            continue
        # Key order matches the committed file, so a capture that confirms the
        # baseline is a no-op diff rather than a reordering of every entry.
        expectations = {
            "authorization_server": capture["authorization_server"],
            "dcr": capture["dcr"],
            "pkce": capture["pkce"],
            "verified_on": stamp,
        }
        if committed != expectations:
            changed.append(slug)
        provider["l0_expectations"] = expectations
    return RecordOutcome(changed=changed, needs_approval=needs_approval)


def record_baselines(
    registry_path: Path, observed: Mapping[str, ObservedBaseline], captured_on: date
) -> RecordOutcome:
    """Rewrite ``registry_path`` with the observed baselines.

    The file is left untouched when nothing changed, so a capture that confirms
    every baseline on the day it was already stamped produces no diff at all.
    """

    try:
        providers = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecordError(f"could not read provider registry: {error}") from error
    if not isinstance(providers, list) or not all(isinstance(p, dict) for p in providers):
        raise RecordError("provider registry must be an array of objects")

    outcome = apply_baselines(providers, observed, captured_on)
    if not outcome.changed:
        return outcome
    try:
        # atomic_write, not write_text: an in-place write truncates first, so a
        # full disk or an interrupt mid-write leaves a half-written registry --
        # and this file is parsed at import time, so a corrupt one is a startup
        # failure for the whole package, not a failed command. The helper writes
        # a temp file in the SAME directory and renames, so a reader sees either
        # the old registry or the new one.
        #
        # newline="" is load-bearing: the default translates "\n" to "\r\n" on
        # Windows, which would change the bytes of a file this module reads back,
        # edits and rewrites -- and which test_connections_l0_probe pins as a
        # byte-identical round trip. fsync so the rename cannot publish a temp
        # whose contents have not reached disk.
        atomic_write(
            registry_path, render_registry(providers), fsync=True, newline=""
        )
    except OSError as error:
        raise RecordError(f"could not write provider registry: {error}") from error
    return outcome
