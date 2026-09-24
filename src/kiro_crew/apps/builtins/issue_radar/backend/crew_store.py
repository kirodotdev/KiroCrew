"""Crew records, work items and the append-only event ledger.

One repository's crews live under its repo data dir::

    repos/<owner>/<repo>/crews/settings.json      protocol constants, repo-wide
    repos/<owner>/<repo>/crews/<crew_id>.json     one crew

Those two are operator configuration and are files. The crew's WORK -- its work
items, its progress lines and its passes -- is not: it is a projection of the
crew's own crew log. ``issue_radar_crew_record`` appends one ``radar/recorded``
entry per call to the session log of the slot the crew runs on, and every read of
a work item, a progress line or the repository's shared skip index folds those
entries (the ``radar`` projection in ``kiro_crew.crew_log.projection``). See the
ledger section below for what that buys and what it costs.

Every file carries ``schema``. Issue Radar's usual versioning strategy — a schema
mismatch is a cache miss, refetch from the forge — does NOT transfer here: a crew
record has no upstream to refetch from, so readers coerce forward on read and a
real migration is required if the shape ever changes incompatibly.

Locking. ``store.py``'s per-record lock is the model for the crew record. The
ledger takes no lock of its own: an append to a crew log is serialised by the
crew log's writer, and every invariant the old three-file transaction held under
three locks -- a phase never moves without its logged reason, an issue is never
skipped without being indexed -- is a property of ONE entry instead.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.entry_types import (
    RADAR_CLEARABLE_FIELDS,
    RADAR_CREW_LEVEL_EVENT_KIND,
    RADAR_DEFAULT_SKIP_SCOPE,
    RADAR_EDITING_PHASES,
    RADAR_ENTRY_TYPE,
    RADAR_EVENT_KINDS,
    RADAR_NUMBER_BOUNDS,
    RADAR_PHASES,
    RADAR_SKIP_SCOPES,
    RADAR_TERMINAL_PHASES,
    RADAR_TTL_ACTIVE_PHASES,
)
from kiro_crew.crew_log.store import log_exception_text

from . import store

logger = logging.getLogger(__name__)

CREW_SCHEMA = 1

# ── phases ──────────────────────────────────────────────────────────────────
#
# Two classifications hang off this enum and they deliberately do NOT coincide,
# which is why neither can be collapsed into a boolean on the record:
#
#   TTL_ACTIVE          — only these age toward the claim TTL. A parked pull
#                         request is stronger evidence of a live claim than any
#                         heartbeat could be, and a crew waiting on a human's
#                         review for three days has no progress to record.
#   EDITING             — a worktree with uncommitted changes. At most one per
#                         crew, enforced in `commit_work_progress`.
#
# A crew NEVER holds an issue waiting for a human. When it needs a human decision
# or a human investigation it says so on the issue, labels it, records the pass
# (`skipped`, with a scope naming which of the two it needs) and releases the
# claim — so every non-terminal phase is one the crew is itself the actor in, and
# every one of them occupies a work slot.
PHASES = RADAR_PHASES
TERMINAL_PHASES = RADAR_TERMINAL_PHASES
TTL_ACTIVE_PHASES = RADAR_TTL_ACTIVE_PHASES
EDITING_PHASES = RADAR_EDITING_PHASES

#: ``sweep`` is the one kind that does NOT belong to an issue: it records that the
#: crew looked at the queue and took nothing, which is the only step in the
#: protocol with no work item behind it. Every other kind names something done TO
#: an item, so those lines carry a number and ``sweep`` lines do not. Without it a
#: crew that found an empty queue could only report the cycle by attributing it to
#: some issue it did not act on.
EVENT_KINDS = RADAR_EVENT_KINDS

#: The one kind that records a crew-level step rather than one issue's. It is the
#: only kind the write route accepts without a number and with no work-item patch.
#: A named constant rather than a set of one: the frontend already tests
#: `kind === 'sweep'` by equality, and a collection whose only justification is a
#: second member that does not exist reads as generality this app has not earned.
CREW_LEVEL_EVENT_KIND = RADAR_CREW_LEVEL_EVENT_KIND

#: Why an issue was passed over, as a closed vocabulary. Two things need it to be
#: closed rather than free prose: a crew reads the recent-skip list to calibrate
#: what this fleet does not take on, and a human scanning the index wants to see
#: whether the passes cluster on `needs-design` (a backlog problem) or on
#: `not-reproducible` (a triage problem). Free text gives neither.
#:
#: ``needs-decision`` and ``needs-investigation`` are the two that mean "a human
#: has to do the next step". They are scopes on a PASS rather than a state of
#: their own precisely because the crew does not wait for that human: it says what
#: it needs on the issue, labels it with the repo's ``needs_human_label``, records
#: the pass and moves on. The issue is then found again by whoever answers, not
#: held by a crew that cannot proceed.
#:
#: An unrecognised value is COERCED to ``other`` rather than refused — see
#: :func:`coerce_skip_scope`.
SKIP_SCOPES = RADAR_SKIP_SCOPES
DEFAULT_SKIP_SCOPE = RADAR_DEFAULT_SKIP_SCOPE

#: Work-item fields an update may EMPTY by name. An omitted field means
#: "unchanged"; the only way to erase one is to name it here (the route's and the
#: tool's ``clear`` list, or an explicit ``None`` in a store-level patch), which the
#: entry carries as its declared ``clear`` field and the fold applies before the
#: same update's set fields. Declared with the entry type so the fold and every
#: writer agree on the list.
CLEARABLE_FIELDS = RADAR_CLEARABLE_FIELDS

#: Galaxy names. No two share their first two letters, so a crew name is
#: unambiguous at a glance in a log line — `Cartwheel`/`Pinwheel` and
#: `Circinus`/`Cigar` were dropped for exactly that reason, and `Pegasus` /
#: `Phoenix` / `Sextans` because they collide with well-known software or read
#: badly in a work context.
NAME_POOL = (
    "Andromeda", "Bode", "Butterfly", "Carina", "Cigar", "Cocoon",
    "Draco", "Fireworks", "Fornax", "Grus", "Hoag", "Leo",
    "Mayall", "Medusa", "Pinwheel", "Porpoise", "Sculptor", "Sombrero",
    "Spindle", "Tadpole", "Triangulum", "Tucana", "Ursa", "Whirlpool",
)

#: Ceiling on every free-text repo setting. These are read back into a crew's
#: prompt on every resume and one of them is written to the forge as a label, so
#: an unbounded value is a context cost and a failed label write rather than a
#: cosmetic problem. Generous enough for a templated trailer, short enough that a
#: pasted document cannot become a label.
MAX_SETTING_TEXT = 200

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema": CREW_SCHEMA,
    "claim_ttl_hours": 48,
    #: The label a crew applies when it needs a human decision or a human
    #: investigation, alongside the pass it records. Configurable because label
    #: vocabularies belong to the repository, not to this app: a project that
    #: already triages with `needs: maintainer` should not be made to grow a
    #: second word for the same thing.
    #:
    #: Repo-wide for the same reason the TTL is: two crews labelling the same
    #: condition differently gives the person answering two queues to watch. And
    #: it is one of only TWO labels a crew ever writes — this one and
    #: `crew: in progress` — so it is validated as input on the way in
    #: (:func:`_validated_label`), not trusted because a settings form produced it.
    "needs_human_label": "crew: needs human",
    "commit_trailer": "Crew: {name} (Kiro Crew Issue Radar)",
}

_DEFAULT_CREW: dict[str, Any] = {
    "labels": [],
    "auto_resolve_conflicts": True,
    "auto_merge": True,
    "unattended": True,
    "max_open": 3,
    "agent": "kirocrew",
    "model": "",
    "extra_prompt": "",
    "worktree_root": "",
    "enabled": True,
    "paused_reason": "",
}


class CrewStoreError(Exception):
    """A store invariant was violated — a duplicate name, an unknown crew, or a
    second work item trying to enter an editing phase."""


# ── numbers ─────────────────────────────────────────────────────────────────
#
# Every number on these records arrives as a DECODED JSON number, from a request
# body or from a file in the data home, and neither source is limited to the ones
# `int()` accepts.


#: A crew's slot cap, as a closed range. Named because the bound is applied on the
#: write path AND on the read path (:func:`_validated_max_open`), and a bound that
#: is spelled out twice is one edit away from being two different bounds. The
#: editor mirrors it in the dashboard for the same reason.
MIN_MAX_OPEN = 1
MAX_MAX_OPEN = 20


def _finite_int(value: Any) -> int | None:
    """*value* as an int, or ``None`` to mean "not a usable number".

    Two decoded JSON values reach here that ``int()`` cannot convert, and both are
    reachable from a request body and from a stored file:

    * ``Infinity``/``-Infinity``/``NaN``, which Python's ``json`` accepts by
      default — and which ``1e309`` also produces, by overflowing SILENTLY on the
      way in, so a plain-looking literal is enough. ``int(inf)`` raises
      ``OverflowError`` and ``int(nan)`` raises ``ValueError``, which is a 500 from
      whichever route touched the value rather than the clean refusal the caller
      earned.
    * ``True``/``False``. ``bool`` is a subclass of ``int``, so ``json`` ``true``
      would otherwise store as ``1`` — the same trap ``routes._pr_number_field``
      guards, and for the same reason.

    Non-finite is refused rather than clamped, because there is no defensible
    finite value to clamp it to and a stored ``inf`` is worse than the crash it
    replaces: ``json.dumps`` writes it back as a bare ``Infinity``, which is not
    JSON, so the dashboard's ``JSON.parse`` rejects the whole payload — one poisoned
    crew record takes the Crews page down for every crew in the repo. A comparison
    against it is quieter still: ``open_count >= inf`` is simply ``False`` for every
    count, so a cap expressed that way is defeated without raising anything.

    Returning ``None`` for "not a number" is :func:`_validated_text_setting`'s
    convention — the caller decides whether that means the default, ``None`` on the
    record, or a refusal. A FRACTIONAL float reads as "not a number" too: ``int()``
    truncates, so ``47.9`` would store as ``47`` — a value the operator never
    asked for, silently, with the form reporting success. Truncation is the same
    silent substitution the frontend's ``Number.isInteger`` guard refuses one layer
    up; refusing here as well means neither layer can invent a value on its own.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _validated_max_open(value: Any) -> int | None:
    """A crew's slot cap, or ``None`` to mean "keep the default".

    ONE definition for the write path and the read path. The write path bounds it,
    so a stored value outside the range was hand-edited or written by another
    version — and a crew record, unlike an issue, has no upstream to refetch from
    (module docstring), so the read has to answer with something usable.
    """
    number = _finite_int(value)
    if number is None or not (MIN_MAX_OPEN <= number <= MAX_MAX_OPEN):
        return None
    return number


def _validated_ttl_hours(value: Any) -> int | None:
    """The claim TTL, or ``None`` to mean "keep the default".

    Shared by :func:`read_settings` and :func:`write_settings` for the reason
    :data:`_TEXT_SETTINGS` gives: one implementation is what stops a value passing
    on one of the two paths and being refused on the other.
    """
    number = _finite_int(value)
    if number is None or number <= 0:
        return None
    return number


# ── paths ───────────────────────────────────────────────────────────────────


def crews_dir(owner: str, repo: str, root: Path | None = None) -> Path:
    d = store.repo_data_dir(owner, repo, root) / "crews"
    d.mkdir(parents=True, exist_ok=True)
    return d


#: The only shape a crew id may have — `c_` plus the 8 hex chars `create_crew`
#: mints from ``secrets.token_hex(4)``.
_CREW_ID_RE = re.compile(r"^c_[0-9a-f]{8}$")


def is_crew_id(crew_id: str) -> bool:
    """Whether *crew_id* has the shape this store mints. Public so the routes can
    answer a malformed id with 400 instead of the 409 a raised CrewStoreError
    would become."""
    return bool(_CREW_ID_RE.match(crew_id or ""))


def _require_crew_id(crew_id: str) -> str:
    """Gate every crew id before it can reach a filesystem path.

    ``Path("/store") / crew_id`` DISCARDS the base when ``crew_id`` is absolute
    (pathlib semantics), and honours ``..`` when it is relative — so an id taken
    from a request is an arbitrary-file read on ``GET /crew`` and an arbitrary-file
    WRITE on ``PUT /crew``. ``work_item_path`` compounds it by calling
    ``mkdir(parents=True)`` on the joined path, which would create directories
    outside the store.

    The check lives here, at the single choke point every path constructor passes
    through, rather than at each route: a route-level check protects only the
    routes someone remembered, and this store is also driven by MCP tools and the
    watcher. Ids are server-minted, so a rejection is a bug or an attack, never a
    user typo.
    """
    if not _CREW_ID_RE.match(crew_id or ""):
        raise CrewStoreError(f"invalid crew id {crew_id!r}")
    return crew_id


def crew_path(owner: str, repo: str, crew_id: str, root: Path | None = None) -> Path:
    return crews_dir(owner, repo, root) / f"{_require_crew_id(crew_id)}.json"


def work_item_path(
    owner: str, repo: str, crew_id: str, number: int, root: Path | None = None
) -> Path:
    d = crews_dir(owner, repo, root) / _require_crew_id(crew_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{int(number)}.json"


def events_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return crews_dir(owner, repo, root) / "events.jsonl"


def settings_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return crews_dir(owner, repo, root) / "settings.json"


def skips_path(owner: str, repo: str, root: Path | None = None) -> Path:
    """The repo's shared skip index — one file for every crew, not one per crew.

    Repo-wide is the whole point. A pass recorded on a crew's own work item tells
    only that crew, so every OTHER crew re-investigates the same issue from
    scratch: the most expensive thing this fleet can do, and it repeats once per
    crew per poll. The index makes one crew's decision permanent and visible to
    all of them.
    """
    return crews_dir(owner, repo, root) / "skipped.json"


def _crew_lock_path(owner: str, repo: str, crew_id: str, root: Path | None = None) -> Path:
    return crews_dir(owner, repo, root) / f"{_require_crew_id(crew_id)}.lock"


def _records_lock_path(owner: str, repo: str, root: Path | None = None) -> Path:
    """The ONE lock every crew-RECORD write takes, repo-wide.

    Name uniqueness is a repo-wide invariant, so a per-crew lock cannot enforce
    it: two tabs renaming two DIFFERENT crews to the same name take two different
    locks, both read a ``taken_names()`` that predates the other, and both write —
    leaving two crews with one name, which makes an old check-in comment look like
    a live claim. Creation always held this lock; update and retire did not, and
    that is the hole.

    Holding it for retire as well costs nothing (crew edits are human-paced and a
    repo has a handful of crews) and closes a second, quieter window: update and
    retire both read-modify-write the same record, so on separate locks one could
    overwrite the other's field.

    Crew records are the only files this module still writes: the work items, the
    progress lines and the skip index are appends to the crew log and take no lock
    here (see the ledger section).
    """
    return crews_dir(owner, repo, root) / "_create.lock"


# ── settings ────────────────────────────────────────────────────────────────


#: Repo settings that hold free text, and are validated identically on read and on
#: write by :func:`_validated_text_setting`. One tuple so a new one cannot be added
#: to the defaults and silently skip the check on one of the two paths.
_TEXT_SETTINGS = ("needs_human_label", "commit_trailer")


def _validated_text_setting(value: Any) -> str | None:
    """*value* as a stored setting, or ``None`` to mean "keep the default".

    Trimmed, because a label with a leading space is a DIFFERENT label on the forge
    from the one the operator thinks they configured, and the mismatch shows up as
    a second queue nobody is watching rather than as an error. Blank after
    trimming, a non-string, or longer than :data:`MAX_SETTING_TEXT` all read as
    "not configured" so the caller falls back to the default — a crew must always
    have a usable label, and refusing the write would leave the previous value in
    place with the form appearing to have saved.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_SETTING_TEXT:
        return None
    return text


def read_settings(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    """Repo-wide protocol constants, with defaults filled in on read.

    These cannot be per-crew: two crews negotiating with different TTLs is how a
    short-TTL crew steals a long-TTL crew's live work.

    Validated on READ as well as on write. A settings file is an ordinary JSON file
    in the data home and can be hand-edited or restored from a backup written by
    another version, so a blank, over-long or wrong-typed value has to degrade to
    the default here — the alternative is a crew labelling an issue with whatever
    ends up in the file.
    """
    path = settings_path(owner, repo, root)
    out = dict(DEFAULT_SETTINGS)
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return out
        if isinstance(stored, dict):
            ttl = _validated_ttl_hours(stored.get("claim_ttl_hours"))
            if ttl is not None:
                out["claim_ttl_hours"] = ttl
            for key in _TEXT_SETTINGS:
                text = _validated_text_setting(stored.get(key))
                if text is not None:
                    out[key] = text
    return out


def write_settings(
    owner: str, repo: str, patch: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """Merge *patch* into the repo's protocol settings. Returns the stored doc."""
    lock_path = crews_dir(owner, repo, root) / "settings.lock"
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            record = read_settings(owner, repo, root)
            if "claim_ttl_hours" in patch:
                ttl = _validated_ttl_hours(patch["claim_ttl_hours"])
                if ttl is not None:
                    record["claim_ttl_hours"] = ttl
            for key in _TEXT_SETTINGS:
                if key in patch:
                    text = _validated_text_setting(patch[key])
                    if text is not None:
                        record[key] = text
            record["schema"] = CREW_SCHEMA
            atomic_write(settings_path(owner, repo, root), json.dumps(record, indent=2))
    return record


# ── crews ───────────────────────────────────────────────────────────────────


def list_crews(
    owner: str, repo: str, root: Path | None = None, *, include_retired: bool = False
) -> list[dict[str, Any]]:
    """Every crew in this repo, oldest first. Retired crews are excluded by
    default but their records are kept — the name stays reserved and their work
    log stays readable."""
    out: list[dict[str, Any]] = []
    for path in sorted(crews_dir(owner, repo, root).glob("*.json")):
        # ALLOWLIST the crew-id shape; do not blocklist known sibling filenames.
        # This directory holds `settings.json` and `skipped.json` beside the crew
        # records, and the previous `name == "settings.json"` check meant the first
        # recorded skip was parsed as a crew: it has no `id`, so the watchdog would
        # launch a session keyed `crew-None` and, because `unattended` defaults on,
        # hand it trust. Every future sibling file would do the same. `is_crew_id`
        # is the same gate the store's path constructors use, so a file that is not
        # a crew record cannot be one by name.
        if not is_crew_id(path.stem):
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("retired_at") and not include_retired:
            continue
        out.append(_coerce_crew(rec))
    out.sort(key=lambda r: r.get("created_at") or "")
    return out


def read_crew(
    owner: str, repo: str, crew_id: str, root: Path | None = None
) -> dict[str, Any] | None:
    path = crew_path(owner, repo, crew_id, root)
    if not path.is_file():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _coerce_crew(rec) if isinstance(rec, dict) else None


def _coerce_crew(rec: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults on read, the way ``list_connected_repos`` back-fills
    provider/host — so no caller has to know which fields a record predates."""
    out = dict(_DEFAULT_CREW)
    out.update(rec)
    out["schema"] = CREW_SCHEMA
    if not isinstance(out.get("labels"), list):
        out["labels"] = []
    # The two numbers on this record are coerced on READ as well as on write, for
    # the reason `read_settings` gives about its own file: this is an ordinary JSON
    # file in the data home, so it can be hand-edited or restored from a backup
    # written by another version. Letting a bad one through is not cosmetic here.
    # `max_open` is the crew's slot cap, and the two places it is applied both fail
    # OPEN on a non-finite value: the brief renders it as prose ("Open 2/inf", i.e.
    # unlimited) and the page compares against it (`open_count >= inf` is False for
    # every count), so neither raises and the cap is simply gone. And a non-finite
    # that survives to the response body is worse than either — `json.dumps` writes
    # a bare `Infinity`, which `JSON.parse` refuses, and `GET /crews` returns every
    # crew in one payload, so one poisoned record blanks the page for all of them.
    max_open = _validated_max_open(out.get("max_open"))
    out["max_open"] = max_open if max_open is not None else _DEFAULT_CREW["max_open"]
    out["avatar_variant"] = _finite_int(out.get("avatar_variant"))
    # The avatar seed is stored separately from the name on purpose: renaming a
    # crew must not change its face.
    if not out.get("avatar_seed"):
        out["avatar_seed"] = out.get("name") or ""
    return out


def taken_names(owner: str, repo: str, root: Path | None = None) -> set[str]:
    """Names that may not be reused — including retired crews'.

    A retired crew's name still appears in its work log and in the check-in
    comments it left on GitHub. Reusing it would make an old comment look like a
    live claim.
    """
    return {
        str(c.get("name") or "")
        for c in list_crews(owner, repo, root, include_retired=True)
    }


def suggest_names(owner: str, repo: str, root: Path | None = None, *, limit: int = 6) -> list[str]:
    """Unused pool names first; then ``<Galaxy> II``, ``III``… once it is spent.

    The degraded form is astronomically correct — Leo II, Draco II and Grus II
    are all real dwarf galaxies.
    """
    used = taken_names(owner, repo, root)
    free = [n for n in NAME_POOL if n not in used]
    if len(free) >= limit:
        return free[:limit]
    out = list(free)
    suffix = 2
    romans = {2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}
    while len(out) < limit and suffix <= 6:
        for base in NAME_POOL:
            cand = f"{base} {romans[suffix]}"
            if cand not in used and cand not in out:
                out.append(cand)
                if len(out) >= limit:
                    break
        suffix += 1
    return out[:limit]


def create_crew(
    owner: str, repo: str, spec: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """Create a crew. Raises :class:`CrewStoreError` on a duplicate name.

    Uniqueness is enforced HERE rather than only in the create dialog's
    suggestion chips, because the name field is free text.
    """
    name = str(spec.get("name") or "").strip()
    if not name:
        raise CrewStoreError("a crew needs a name")

    lock_path = _records_lock_path(owner, repo, root)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True), _mint_lock(root):
            if name in taken_names(owner, repo, root):
                raise CrewStoreError(f"crew name {name!r} is already taken in this repo")
            crew_id = _mint_crew_id(root)
            now = store._now_iso()
            record = dict(_DEFAULT_CREW)
            record.update(
                {
                    "schema": CREW_SCHEMA,
                    "id": crew_id,
                    "name": name,
                    "avatar_seed": str(spec.get("avatar_seed") or name),
                    "avatar_variant": spec.get("avatar_variant"),
                    "slot_key": f"crew-{crew_id}",
                    "created_at": now,
                    "retired_at": None,
                }
            )
            record.update(_validated_crew_patch(spec))
            atomic_write(
                crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2)
            )
    return _coerce_crew(record)


#: How many fresh ids are tried before creation gives up. One try all but always
#: suffices; the bound exists so a broken random source cannot spin forever.
_CREW_ID_MINT_TRIES = 8


def _mint_crew_id(root: Path | None) -> str:
    """A crew id no crew in ANY repository of this data home holds.

    A crew's id names its slot (``crew-<id>``), and a slot is a data-home-wide name:
    the crew log lists a slot's units by that key alone, so two crews in different
    repositories with one id would fold each other's ledgers into one record. The id
    is random (32 bits), so a clash is improbable; it is made impossible here by
    checking every repository's crew records -- under the legacy GitHub root and
    under every provider subtree -- and the crew log's own slot listing before the
    id is taken, instead of trusting the odds. Called under :func:`_mint_lock`, so
    two repositories minting at once cannot both pass the check with one id.
    """
    for _ in range(_CREW_ID_MINT_TRIES):
        crew_id = f"c_{secrets.token_hex(4)}"
        if not _crew_id_in_use(crew_id, root):
            return crew_id
    raise CrewStoreError("could not mint an unused crew id")


def _app_root(root: Path | None) -> Path:
    """The app's data root, whichever provider subtree *root* points into.

    ``root`` is the legacy GitHub root or a provider subtree beneath it
    (``<root>/@providers/<provider>/<host>``); a data-home-wide check has to start
    from the root above every subtree.

    Only the EXACT trailing three components :func:`store.provider_root` appends are
    stripped. Matching the first ``@providers`` component anywhere in the path would
    truncate an operator base path that itself carries that segment, putting both the
    mint lock and the uniqueness glob above the data home -- where the glob matches
    no crew record at all, so :func:`_crew_id_in_use` answers False for an id that IS
    held and two crews fold one ledger.
    """
    base = store.data_dir(root)
    parts = base.parts
    if len(parts) >= 3 and parts[-3] == store._PROVIDER_SUBTREE:
        return Path(*parts[:-3])
    return base


def _crew_id_in_use(crew_id: str, root: Path | None) -> bool:
    """Whether any repository's crew record holds *crew_id*, or a crew-log slot names it."""
    from kiro_crew.crew_log.store import session_units_for_slot

    app_root = _app_root(root)
    for pattern in (
        f"repos/*/*/crews/{crew_id}.json",
        f"{store._PROVIDER_SUBTREE}/*/*/repos/*/*/crews/{crew_id}.json",
    ):
        if any(app_root.glob(pattern)):
            return True
    return bool(session_units_for_slot(slot_key_for(crew_id)))


@contextmanager
def _mint_lock(root: Path | None) -> Iterator[None]:
    """One data-home-wide lock around minting a crew id and writing its record.

    The per-repository records lock serializes creations within one repository;
    two repositories creating at once hold different locks and could both mint one
    id between the check and the write. Taken INSIDE the repository lock, always in
    that order, so the two cannot deadlock.
    """
    lock_path = _app_root(root) / "crew-id-mint.lock"
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


def _validated_crew_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Only known, type-checked fields survive — same discipline as
    ``write_investigation``: an unknown key in a patch is dropped, not stored."""
    out: dict[str, Any] = {}
    for key in ("agent", "model", "extra_prompt", "worktree_root", "paused_reason"):
        if key in patch and isinstance(patch[key], str):
            out[key] = patch[key]
    for key in ("auto_resolve_conflicts", "auto_merge", "unattended", "enabled"):
        if key in patch and isinstance(patch[key], bool):
            out[key] = patch[key]
    if "max_open" in patch:
        val = _validated_max_open(patch["max_open"])
        if val is not None:
            out["max_open"] = val
    if "labels" in patch and isinstance(patch["labels"], list):
        out["labels"] = [str(x) for x in patch["labels"] if isinstance(x, str) and x.strip()]
    if "avatar_variant" in patch:
        out["avatar_variant"] = _finite_int(patch["avatar_variant"])
    if "avatar_seed" in patch and isinstance(patch["avatar_seed"], str):
        if patch["avatar_seed"].strip():
            out["avatar_seed"] = patch["avatar_seed"].strip()
    return out


def update_crew(
    owner: str, repo: str, crew_id: str, patch: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """Merge *patch* into a crew. A rename re-checks uniqueness but leaves
    ``avatar_seed`` alone, so the crew keeps its face.

    Takes the repo-wide record lock, not this crew's: the uniqueness check below
    reads every OTHER crew's name, so it has to exclude concurrent renames of
    those crews. See ``_records_lock_path``.
    """
    lock_path = _records_lock_path(owner, repo, root)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            record = read_crew(owner, repo, crew_id, root)
            if record is None:
                raise CrewStoreError(f"unknown crew {crew_id!r}")
            new_name = str(patch.get("name") or "").strip()
            if new_name and new_name != record.get("name"):
                if new_name in taken_names(owner, repo, root):
                    raise CrewStoreError(f"crew name {new_name!r} is already taken")
                record["name"] = new_name
            record.update(_validated_crew_patch(patch))
            record["schema"] = CREW_SCHEMA
            atomic_write(crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2))
    return _coerce_crew(record)


def set_crew_paused(
    owner: str,
    repo: str,
    crew_id: str,
    paused: bool,
    reason: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """Pause or resume a crew. One definition, because the state is a PAIR.

    ``enabled`` and ``paused_reason`` have to move together: a stale reason on a
    running crew makes the page explain why a working crew is stopped, and a pause
    with no reason gives the roster nothing to show. Expressing it as the verb
    rather than as two independent patch fields is what stops a caller storing half
    of it — resuming CLEARS the reason rather than leaving the last one behind.
    """
    return update_crew(
        owner,
        repo,
        crew_id,
        {"enabled": not paused, "paused_reason": reason if paused else ""},
        root,
    )


def retire_crew(
    owner: str, repo: str, crew_id: str, root: Path | None = None
) -> dict[str, Any]:
    """Retire a crew: it stops working but its record, its name reservation and
    its work log all survive."""
    record = update_crew(owner, repo, crew_id, {"enabled": False}, root)
    lock_path = _records_lock_path(owner, repo, root)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            record = read_crew(owner, repo, crew_id, root) or record
            record["retired_at"] = store._now_iso()
            atomic_write(crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2))
    return _coerce_crew(record)


# ── the ledger: work items, progress lines and passes ───────────────────────
#
# A crew's work items, its progress lines and its passes are not files of their
# own. Each ``issue_radar_crew_record`` call appends ONE ``radar/recorded`` entry to
# the crew's own crew log -- the session log of the slot the crew runs on -- and
# everything below is a FOLD of those entries: the ``radar`` projection in
# ``kiro_crew.crew_log.projection``. One entry carries the item's delta, the event
# that explains it and, when the phase is ``skipped``, the skip row, so the three
# writes the old store made all-or-nothing under three locks are one append that a
# crash cannot separate.
#
# A crew's slot owns one ACP session id at a time, so its record is spread over a
# unit per id it ran under and the read joins them (:func:`crew_log_units`). The
# repository's shared skip index is the UNION of every crew's folded passes
# (:func:`read_skips`); that is the one read made across crews, and it is a fold
# rather than a second mutable file.
#
# The pre-projection files (``crews/<crew_id>/<n>.json``, ``events.jsonl``,
# ``skipped.json``) are read once more, to be CARRIED into the log on a crew's
# first write after the upgrade (:func:`_carry_legacy_forward`), and never written.

LEDGER_ENTRY_TYPE = RADAR_ENTRY_TYPE
FOLD_NAME = "radar"
_ENTRY_SRC = "gateway"

#: How long a write waits for the crew log writer to drain before answering. The
#: route runs the write on a worker thread, so this costs no event-loop time.
_APPEND_FLUSH_SECONDS = 5.0

#: One lock per (data home, crew) held across a WRITE's fold, refusals, append and
#: answer. The refusals are checked against the folded record, so two requests for
#: one crew validating against the same fold could both pass the one-editor rule and
#: both append; the route runs writes on worker threads, so the serialization is a
#: thread lock, taken for the whole write including the drain. Reads take nothing.
_crew_write_locks: dict[tuple[str, str], threading.Lock] = {}
_crew_write_locks_guard = threading.Lock()

#: Crews whose last append had not drained to the log when the write answered, each
#: mapped to the RECEIPT for that append: the unit whose log it belongs in, the seq it
#: followed, and the entry itself. The crew's next write drains again BEFORE it folds,
#: so it does not validate against a record missing that entry -- two requests could
#: otherwise both pass the one-editor rule. The receipt is what makes the clearing
#: honest: a drained writer is not a landed entry (a dropped write empties the buffer
#: too), so the mark is released only once the entry is found in that unit's log BY
#: CONTENT. Touched only under the crew's write lock.
_undrained: dict[tuple[str, str], tuple[str, int, dict[str, Any]]] = {}


def _landed_since(
    projection: Any, session_id: str, after_seq: int, data: dict[str, Any]
) -> tuple[tuple[Any, ...] | None, bool]:
    """The entries after *after_seq* in *session_id*'s log, and whether *data* is among
    them BY CONTENT.

    Content rather than seq arithmetic, because the crew's own turn appends to the same
    log while it records, so the span holds more than the entry being looked for. Tried
    twice: a read that fails once (a transient I/O error) must not turn a landed entry
    into a refusal, and a second failure is the same answer as a missing entry -- the
    writer is done and nothing shows the entry in the file. A log that is gone answers
    ``(None, False)``: nothing landed and nothing will.
    """
    for attempt in (1, 2):
        try:
            handle = projection.open_session_log(session_id)
            if handle is None:
                return None, False
            since = tuple(handle.iter_from(after_seq + 1, known=projection.KNOWN_TYPES))
            return since, any(
                e.type == LEDGER_ENTRY_TYPE and e.data == data for e in since
            )
        except Exception:
            log_exception_text(
                logger,
                logging.DEBUG,
                "crew ledger: could not read the appended entry back (attempt %d)",
                attempt,
            )
    return None, False


def _crew_write_lock(crew_id: str) -> threading.Lock:
    key = (str(data_home()), crew_id)
    with _crew_write_locks_guard:
        lock = _crew_write_locks.get(key)
        if lock is None:
            lock = _crew_write_locks[key] = threading.Lock()
        return lock


#: Marker files the one-time carry leaves behind, so the pre-projection files are
#: never read twice and a second crew does not re-carry the repository's passes.
_ITEMS_CARRIED_MARKER = ".carried"
_SKIPS_CARRIED_MARKER = "skipped.json.carried"

#: Written when a carry BEGINS. A carry that began and has no finished marker did not
#: fully land, and the next write runs it again -- the only case in which files that
#: appear beside a crew with a live record are read: a file that shows up later (a
#: restore from a backup) is otherwise left alone rather than merged over live state.
_ITEMS_CARRY_BEGUN_MARKER = ".carrying"
_SKIPS_CARRY_BEGUN_MARKER = "skipped.json.carrying"

#: Text ceiling the carry applies to what it re-states. It MUST equal the fold's
#: ``RADAR_TEXT_LIMIT``, which equals the cap the record tool accepts: the carry is
#: one-shot and marks itself finished, so a legacy value this cuts is gone from the
#: live ledger with nothing left to re-read it from.
_MAX_CARRIED_TEXT = 5000


class CrewLedgerUnavailable(CrewStoreError):
    """The crew's session has no crew log to record into.

    The crew log is switched off, or the session has not run its first turn yet.
    A :class:`CrewStoreError` so the crew routes' conflict mapping still answers it,
    and its own class so the write route can name the condition (409
    ``crew_log_unavailable``) instead of a generic conflict.
    """


class CrewLedgerEntryTooLarge(CrewStoreError):
    """The update does not fit one crew log entry, so it can never land."""


class CrewLedgerNotRecorded(CrewStoreError):
    """The crew log writer drained without landing the update: it was refused.

    The session's log was deleted under the write, or the writer gave up on it.
    Nothing was recorded, so the caller is told so (503 ``ledger_not_recorded``)
    rather than handed a record that exists nowhere; the same body may be retried.
    """


def slot_key_for(crew_id: str) -> str:
    """The slot a crew runs on -- the key its crew log units are headed with."""
    return f"crew-{_require_crew_id(crew_id)}"


def _projection() -> Any:
    """The fold package, imported where it is used so a crew record read never pulls it."""
    from kiro_crew.crew_log import projection

    return projection


#: The crew's units in the order they first RECORDED, one id per line, kept beside the
#: crew's record (``crews/<crew_id>.unit-order``; NOT inside ``crews/<crew_id>/``,
#: which is the pre-projection items dir the one-time carry reads, and whose mere
#: existence it takes as legacy state to carry). Append order is what makes it
#: causal: a unit is written here by the write that records into it, so the sequence
#: is what happened, not what a clock said. Unit headers carry a wall clock, and a
#: clock stepped backward before a replacement unit was created sorts the
#: replacement BEFORE its predecessor, which applies a retired session's phases over
#: the current ones -- on the READ path, where no caller is inside a unit to pin it
#: last. The newest ids are kept.
_UNIT_ORDER_SUFFIX = ".unit-order"
_MAX_ORDERED_UNITS = 64
#: Reads at most this many bytes of the order file, so a file that grew before the
#: bound existed cannot make a crew's every cycle read unboundedly.
_MAX_ORDER_READ_BYTES = _MAX_ORDERED_UNITS * 2 * 256


def _unit_order_path(owner: str, repo: str, crew_id: str, root: Path | None) -> Path:
    return crews_dir(owner, repo, root) / f"{_require_crew_id(crew_id)}{_UNIT_ORDER_SUFFIX}"


def _recorded_unit_order(owner: str, repo: str, crew_id: str, root: Path | None) -> tuple[str, ...]:
    """The units this crew recorded into, oldest first; ``()`` when none is recorded.

    Deduplicated, then the newest :data:`_MAX_ORDERED_UNITS` kept. A failure to read
    answers ``()``: the caller falls back to header order, which is what every read
    did before the order existed. The file is opened refusing a link at its name
    (:func:`platform_compat.open_file_no_reparse`): the order lives where a sandboxed
    agent may be able to plant a link, and a read through one would take another
    file's lines for unit ids -- and, since the writer re-states what it read, copy
    them into this crew's order file.
    """
    try:
        path = _unit_order_path(owner, repo, crew_id, root)
        if not path.is_file():
            return ()
        fd = platform_compat.open_file_no_reparse(path)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            text = fh.read(_MAX_ORDER_READ_BYTES)
    except (OSError, ValueError):
        return ()
    seen: list[str] = []
    known: set[str] = set()
    for line in text.splitlines():
        unit = line.strip()
        if unit and unit not in known:
            known.add(unit)
            seen.append(unit)
    return tuple(seen[-_MAX_ORDERED_UNITS:])


def _record_unit_order(
    owner: str, repo: str, crew_id: str, session_id: str, root: Path | None
) -> None:
    """Note that *session_id* is the unit this crew records into now.

    Called under the crew's write lock once that unit's entry is read back from its
    own log by content, so a line here always names a unit whose append LANDED and a
    refused or queued write cannot claim precedence it never earned. A unit already
    newest is a bare read; one recorded earlier that records again is MOVED to the
    end, not appended twice, so the fold never folds a unit twice. The file is
    compacted to the bound once it outgrows it. Best-effort: a crew whose order
    cannot be written folds its units in header order instead.
    """
    if not session_id:
        return
    try:
        path = _unit_order_path(owner, repo, crew_id, root)
        known = _recorded_unit_order(owner, repo, crew_id, root)
        if known and known[-1] == session_id:
            return
        ordered = tuple(u for u in known if u != session_id) + (session_id,)
        _write_unit_order(path, ordered[-_MAX_ORDERED_UNITS:])
    except (OSError, ValueError):
        logger.warning("crew ledger: could not record crew %s's unit order", crew_id, exc_info=True)


def _write_unit_order(path: Path, lines: tuple[str, ...]) -> None:
    """Replace *path* with *lines*: a reader sees the old file or the new, and no link
    is followed on the way.

    The file is never appended to in place -- an ``open("a")`` follows a link planted
    at the name and would append to whatever it points at -- and never staged under a
    predictable temp name, which a link could be planted at just the same. It is
    written by :func:`atomic_write`: a UNIQUELY named temp file created ``O_EXCL``
    and renamed over the name, which replaces a planted link rather than writing
    through it. Where the platform supports descriptor-relative writes the parent is
    PINNED first (``O_DIRECTORY | O_NOFOLLOW``), so a link planted at the directory
    is refused too; elsewhere every ancestor and the name itself are screened with
    :func:`platform_compat.is_link_or_junction`, which unlike ``is_symlink`` also
    answers for a Windows directory junction -- the platform without descriptor-
    relative writes is the one where junctions exist, so an ``islink``-only check
    would leave that fallback with no boundary at all.
    Whole-file writes are cheap here: the file holds at most
    :data:`_MAX_ORDERED_UNITS` short lines.
    """
    content = "".join(f"{line}\n" for line in lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    if atomic_write_module.pinned_parent_replace_supported():
        parent_fd = platform_compat.pin_directory(path.parent)
        try:
            atomic_write(path, content, fsync=True, newline="", parent_dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        return
    if platform_compat.is_link_or_junction(path) or platform_compat.first_linked_ancestor(path):
        raise OSError(f"{path} is reached through a link; the unit order is not written through it")
    atomic_write(path, content, fsync=True, newline="")


def crew_log_units(
    owner: str,
    repo: str,
    crew_id: str,
    root: Path | None = None,
    *,
    live_session_id: str = "",
    strict: bool = False,
) -> tuple[str, ...]:
    """Every crew log holding *crew_id*'s ledger entries, oldest unit first.

    Units are listed by the wall clock their headers carry, then re-ordered by the
    order this crew RECORDED into them (:data:`_UNIT_ORDER_SUFFIX`): a unit nothing
    recorded into contributes no entries, so its place among them is immaterial and
    header order is kept for it, and every unit absent from the order log is older
    than everything in it -- the log keeps the newest ids -- so those apply FIRST.
    The LIVE unit applies LAST whatever either says: a writer knows which unit it is
    in, and the fold cannot be wrong about it.

    ``()`` when the crew has none -- a crew that never recorded, or a gateway whose
    crew log is off -- and, unless *strict*, on every failure to LIST them, because
    this runs on the read path of a crew's every cycle and a listing that cannot be
    made must not raise into one; a read then answers the empty record. A WRITE lists
    strictly: a listing that failed, and one that is INCOMPLETE because a unit already
    holding entries cannot be proved, are both raised rather than folded, because a
    write that validated against a record missing a unit could admit a second editor.
    """
    try:
        from kiro_crew.crew_log.store import session_units_for_slot

        units = session_units_for_slot(slot_key_for(crew_id), strict=strict)
        recorded = _recorded_unit_order(owner, repo, crew_id, root)
        if recorded:
            known = [u for u in recorded if u in units]
            rest = [u for u in units if u not in recorded]
            units = tuple(rest + known)
        if live_session_id and live_session_id in units:
            units = tuple(u for u in units if u != live_session_id) + (live_session_id,)
        return units
    except Exception:
        logger.warning("crew ledger: could not list the crew logs for crew %s", crew_id, exc_info=True)
        if strict:
            raise
        return ()


def _unit_last_seq(unit_id: str) -> int:
    """The newest seq in *unit_id*'s log as the file itself reports it, or 0."""
    try:
        handle = _projection().open_session_log(unit_id)
    except Exception:
        return 0
    if handle is None:
        return 0
    return int(getattr(handle, "last_seq", 0) or 0)


def _fold_checkpoint(crew_id: str, units: tuple[str, ...]) -> Any:
    """This crew's ledger fold, continued from where the last read left it.

    Folding is O(the log), not O(the record): the reader walks every line of every unit
    to find the ``radar/recorded`` ones, and a crew's session log carries its message
    bodies. A crew is read on every cycle it wakes, so re-walking its whole history each
    time is the cost this avoids.

    The incremental fold belongs to the crew log and is shared with every other
    slot-keyed reader (:func:`kiro_crew.crew_log.projection.fold_slot_warm`): it keeps
    this fold's cell in memory per crew, continues it over the entries that arrived
    since, and refolds cold for every shape that cannot be carried -- a different unit
    list, a unit whose log was removed and recreated under the same id or whose identity
    cannot be read, an earlier unit that grew (a forced reset tears a session down while
    a turn is still appending through the handle it holds), and a seq that went
    backwards. ONE implementation rather than one per consumer, because each of these
    readers has to enforce the same rules and a rule missing from one of them is a wrong
    record rather than a slow one.
    """
    return _projection().fold_slot_warm(FOLD_NAME, units, slot=slot_key_for(crew_id))


def read_ledger(
    owner: str,
    repo: str,
    crew_id: str,
    root: Path | None = None,
    *,
    live_session_id: str = "",
) -> dict[str, Any]:
    """*crew_id*'s whole ledger -- the ``radar`` projection's value.

    ``items`` newest progress first, ``events`` newest first, ``skips`` keyed by
    ``str(number)`` (this crew's own passes), ``phase_lines`` per item, ``counts``.
    The empty record when the crew has no crew log and nothing to carry forward.

    A crew upgraded from the pre-projection files reads its PENDING carry
    (:func:`_legacy_preview`) until a write consumes it, because the carry can only
    run from a write and the reads that happen first are the ones the crew acts on.
    """
    projection = _projection()
    units = crew_log_units(owner, repo, crew_id, root, live_session_id=live_session_id)
    try:
        base = _fold_checkpoint(crew_id, units)
        value = projection.projection_of(base).value
    except Exception:
        # A crew whose log cannot be FOLDED reads as the empty record, the same answer
        # as a crew whose logs could not be listed and the same contract the
        # pre-projection reader kept when its index would not parse. This is the read
        # path: one crew's damaged log, a segment boundary a reader cannot follow, or
        # an entry type a newer writer introduced would otherwise raise into the crew
        # page and the pre-investigate briefing for EVERY crew in the repository,
        # because those read across crews. The write path does not come through here:
        # it folds strictly (:func:`_prepare_write`), since a write that validated
        # against an empty record could admit a second editor.
        #
        # NOT previewed here either. A refused fold means a reader older than the
        # writer, so the log may hold entries this build could not read -- answering
        # with the pre-projection files would assert that the upgrade has not happened
        # on the strength of a log it failed to read.
        logger.warning(
            "crew ledger: could not fold crew %s's crew log; reading it as empty",
            crew_id,
            exc_info=True,
        )
        return projection.projection_of(projection.initial(FOLD_NAME)).value
    return _legacy_preview(owner, repo, crew_id, root, base) or value


def read_work_item(
    owner: str, repo: str, crew_id: str, number: int, root: Path | None = None
) -> dict[str, Any] | None:
    number = int(number)
    for record in read_ledger(owner, repo, crew_id, root)["items"]:
        if record.get("number") == number:
            return record
    return None


def list_work_items(
    owner: str, repo: str, crew_id: str, root: Path | None = None, *, open_only: bool = False
) -> list[dict[str, Any]]:
    """This crew's work items, newest progress first -- the fold's own order."""
    items = read_ledger(owner, repo, crew_id, root)["items"]
    if open_only:
        items = [record for record in items if record.get("phase") not in TERMINAL_PHASES]
    return items


def open_slot_count(
    owner: str, repo: str, crew_id: str, root: Path | None = None
) -> int:
    """Work items occupying a slot: every unfinished one.

    No exemption, because there is no phase in which the crew is not the
    actor: an item it cannot progress without a human is recorded as a pass and its
    claim released, so anything still open is work this crew owes.
    """
    return len(list_work_items(owner, repo, crew_id, root, open_only=True))


def _editing_item(state: dict[str, Any], *, exclude: int | None = None) -> int | None:
    """The issue number the folded crew is currently editing, if any."""
    for record in state["items"].values():
        num = record.get("number")
        if record.get("phase") in EDITING_PHASES and num != exclude and isinstance(num, int):
            return num
    return None


def read_events(
    owner: str,
    repo: str,
    root: Path | None = None,
    *,
    crew_id: str = "",
    limit: int = 200,
    require_phase: bool = False,
) -> list[dict[str, Any]]:
    """Progress lines, newest first: one crew's, or every crew's in the repository.

    Each crew's fold already collapses a duplicated line and drops a malformed one,
    so the union here only merges and orders. ``require_phase`` keeps the lines that
    carry a ``phase`` -- the ENTRIES into a phase, which is what a dwell reader wants.
    """
    if crew_id:
        sources = [read_ledger(owner, repo, crew_id, root)]
    else:
        sources = [
            read_ledger(owner, repo, str(crew.get("id") or ""), root)
            for crew in list_crews(owner, repo, root, include_retired=True)
            if crew.get("id")
        ]
    rows = [line for source in sources for line in source["events"]]
    if require_phase:
        rows = [line for line in rows if line.get("phase")]
    rows.sort(key=lambda line: str(line.get("ts") or ""), reverse=True)
    return rows[: max(0, limit)]


def coerce_skip_scope(scope: Any) -> str:
    """*scope* if it is a known one, else ``other``.

    Coercing rather than refusing -- the alternative, a 400 on a crew that used an
    unknown scope -- makes an imperfect label cost the entire skip record, and a
    skip that fails to record is exactly the waste this index exists to remove.
    The scope is a filter label; the ``reason`` is the substance, and it is free
    text precisely so nothing forces a decision into the wrong bucket. Case and
    padding are forgiven for the same reason.
    """
    text = str(scope or "").strip().lower()
    return text if text in SKIP_SCOPES else DEFAULT_SKIP_SCOPE


def read_skips(owner: str, repo: str, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """The repository's shared skip index, keyed by ``str(number)``.

    The one read made ACROSS crews: every crew of the repository, retired ones
    included, contributes the passes its own fold holds, and on each number the
    FIRST decision stands -- the same first-decision-wins rule the index always had.
    Which row is first is decided by :func:`_skip_precedence`: a row recorded while
    another crew's decision already stood carries ``deferred`` and never stands over
    it, so an established decision cannot be replaced by a later pass whatever the
    clocks say; only two passes neither of which saw the other -- recorded inside the
    propagation window below -- fall back to the recorded time, and between two
    concurrent decisions neither was established.

    STALENESS. A pass another crew recorded is visible here once that crew's append
    has DRAINED to its log, and each crew's fold is re-checked against its log on
    every read, so nothing is cached past a write. The flush budget
    (``_APPEND_FLUSH_SECONDS``) does not bound that wait: it bounds how long the
    WRITE waits before answering ``durable: false``, and an append still held by the
    writer past it lands whenever the writer drains, with no bound on when. That is
    why the rule above turns on what was ESTABLISHED rather than on how long ago
    something was recorded -- an unbounded lag cannot let a later pass displace a
    decision that already stood. A pass whose unit crew-log retention has already
    collected is gone from this index; that is the same bound every fold of the crew
    log lives under, and the spec states it.
    """
    out: dict[str, dict[str, Any]] = {}
    for crew in list_crews(owner, repo, root, include_retired=True):
        cid = str(crew.get("id") or "")
        if not cid:
            continue
        for key, row in read_ledger(owner, repo, cid, root)["skips"].items():
            standing = out.get(key)
            if standing is None or _skip_precedence(row) < _skip_precedence(standing):
                out[key] = dict(row)
    return out


def _skip_precedence(row: dict[str, Any]) -> tuple[bool, str, str]:
    """Sort key under which the row that STANDS on a number sorts first.

    A ``deferred`` row -- recorded while another crew's decision already stood --
    sorts after every row that is not, so the writer's own observation orders the
    two and a clock stepped backward cannot re-order them. Rows that never saw each
    other order by recorded time, crew id as the tie-break.
    """
    return (
        row.get("deferred") is True,
        str(row.get("decided_at") or ""),
        str(row.get("crew_id") or ""),
    )


def is_skipped(owner: str, repo: str, number: int, root: Path | None = None) -> bool:
    """Whether any crew in this repo has already passed on *number*."""
    return str(int(number)) in read_skips(owner, repo, root)


def recent_skips(
    owner: str, repo: str, root: Path | None = None, *, limit: int = 20
) -> list[dict[str, Any]]:
    """The newest *limit* entries, newest first, issue number as the tie-break."""
    rows = sorted(
        read_skips(owner, repo, root).values(),
        key=lambda r: (str(r.get("decided_at") or ""), int(r.get("number") or 0)),
        reverse=True,
    )
    return rows[: max(0, limit)]


def _require_crew_log(session_id: str) -> Any:
    """The fold package, once *session_id* is known to HAVE a crew log.

    The ledger writes through the emitter, which treats a session with no crew log
    as a policy no-op -- correct for a turn entry nobody asked for, wrong for an
    update a crew explicitly recorded. So the log's existence is established here,
    where the caller can be told, instead of being discovered as silence.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    if not session_id:
        raise CrewLedgerUnavailable(
            "this crew has no live session, so there is no crew log to record into"
        )
    if not crew_log_emit.enabled():
        raise CrewLedgerUnavailable(
            "the crew ledger is recorded in the crew's crew log, which is switched off; "
            f"set {crew_log_emit.CREW_LOG_ENV}=1 to record one"
        )
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import CrewLog

    try:
        present = CrewLog.exists(KIND_SESSION, session_id)
    except Exception as exc:
        raise CrewLedgerUnavailable(f"the crew's crew log could not be read: {exc}") from exc
    if not present:
        raise CrewLedgerUnavailable(
            "the crew's session has no crew log yet, so there is nothing to record into; "
            "it is created on the session's first turn"
        )
    return _projection()


def _entry_data(
    owner: str,
    repo: str,
    crew_id: str,
    number: int | None,
    patch: dict[str, Any],
    event_kind: str,
    event_text: str,
    *,
    skip_reason: str | None = None,
    skip_scope: str = "",
    skip_deferred: bool = False,
) -> dict[str, Any]:
    """The ``radar/recorded`` entry for one update -- only the fields the call set.

    An omitted field means "unchanged", which is what lets a partial patch be one
    line. An EXPLICIT null means "empty this field" and is carried as a name in
    ``clear``, because a typed field cannot hold a null: the fold empties each named
    field before applying the fields the same update sets. A number that cannot be
    read as a finite int, or that falls outside the range the record tool would have
    accepted, is left out rather than written, so a hand-typed ``pr_number`` of
    ``Infinity`` or ``0`` costs that field and not the whole entry -- and, because an
    omitted field reads as "unchanged", does not erase the number already stored.
    """
    data: dict[str, Any] = {"crew_id": crew_id, "owner": owner, "repo": repo}
    if number is not None:
        data["number"] = int(number)
    cleared = [
        key for key in RADAR_CLEARABLE_FIELDS if key in patch and patch[key] is None
    ]
    if cleared:
        data["clear"] = cleared
    for key in ("decision", "why", "next", "worktree", "branch", "base_sha", "outcome"):
        if key in patch and isinstance(patch[key], str):
            data[key] = patch[key]
    if "phase" in patch:
        data["phase"] = str(patch["phase"] or "").strip()
    for key in ("pr_number", "claim_comment_id"):
        if key in patch:
            value = _finite_int(patch[key])
            # The MAGNITUDE bound belongs here, not only in the fold. An out-of-range
            # but finite number (a dashboard ``pr_number`` of 0, a comment id past the
            # forge's range) would otherwise be written, and the fold -- which applies
            # RADAR_NUMBER_BOUNDS to the bytes it reads -- drops it, so the update
            # lands and ERASES the association the item already held. Left out here it
            # reads as "unchanged", which is the same answer this entry already gives
            # a number it cannot read as a finite int at all.
            if value is not None:
                low, high = RADAR_NUMBER_BOUNDS[key]
                if not low <= value <= high:
                    value = None
            if value is not None:
                data[key] = value
    if isinstance(patch.get("ci_state"), dict):
        data["ci_state"] = dict(patch["ci_state"])
    if isinstance(patch.get("labels_applied"), list):
        data["labels_applied"] = [x for x in patch["labels_applied"] if isinstance(x, str)]
    tried = patch.get("tried_approach")
    if isinstance(tried, str) and tried.strip():
        data["tried"] = {
            "approach": tried.strip(),
            "rejected_because": str(patch.get("tried_rejected_because") or ""),
        }
    if skip_reason is not None:
        data["skip"] = {"reason": str(skip_reason or ""), "scope": coerce_skip_scope(skip_scope)}
        if skip_deferred:
            # Another crew's decision on this number already stood when this pass was
            # recorded: the token that orders the two without a clock.
            data["skip"]["deferred"] = True
    data["event"] = event_text
    data["event_kind"] = event_kind
    return data


def _append(
    crew_id: str,
    session_id: str,
    data: dict[str, Any],
    base: Any,
    root: Path | None = None,
) -> dict[str, Any]:
    """Append *data* as ONE entry to *session_id*'s crew log and answer with the
    record it produces. ``{"item", "event", "skip", "coalesced", "durable"}``.

    ``skip`` is the row the SHARED index answers for the number -- the first
    decision across the repository's crews -- so a crew re-skipping an issue is told
    what stands rather than what it sent (:func:`_standing_skip`).

    A crew-level line landing on a crew-level line is COALESCED here, before the
    append, by the same rule the fold applies to the bytes: the existing sweep is
    answered and nothing is written, so an idle crew appends once per stretch.

    The answer is the fold advanced over the entry AS THE FILE HOLDS IT once the
    writer has drained -- the line's id and ``ts`` come off the log's own clock, so
    the answer a crew is handed is the line every later reader sees. Only when the
    writer did not drain inside the budget is the answer the fold advanced over the
    entry on its way to the file, stamped with this side's clock: an approximation
    the next read corrects. ``durable`` says which of the two this was. A writer
    that drained WITHOUT landing the entry refused it, and the write raises
    :class:`CrewLedgerNotRecorded`: a refusal is answered as one, never as a record.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.schema import Entry

    projection = _projection()
    number = data.get("number")
    events = base.state["events"]
    if number is None and events and events[-1].get("kind") == CREW_LEVEL_EVENT_KIND:
        return {"item": None, "event": dict(events[-1]), "skip": None, "coalesced": True, "durable": True}
    if not crew_log_emit.radar_entry_fits(data):
        raise CrewLedgerEntryTooLarge(
            "this update does not fit one crew log entry; record fewer or shorter fields"
        )
    refused_before = crew_log_emit.dropped_writes()
    overflow_before = crew_log_emit.overflow_writes(session_id)
    recorded_ms = int(time.time() * 1000)
    crew_log_emit.on_radar_recorded(session_id, data)
    drained = crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    # Remembered for the crew's NEXT write, which drains again before it folds: a
    # write that validated against a fold missing this entry could pass the
    # one-editor rule a second time. Set and cleared under the crew's write lock.
    undrained_key = (str(data_home()), crew_id)
    if drained:
        _undrained.pop(undrained_key, None)
    else:
        _undrained[undrained_key] = (session_id, base.last_seq, data)
    grown = None
    landed = False
    if drained:
        since, landed = _landed_since(projection, session_id, base.last_seq, data)
        if landed and since is not None:
            grown = projection.advance(base, since)
    if landed:
        # PUBLISH THIS UNIT'S PRECEDENCE ONLY ONCE ITS ENTRY IS IN THE FILE. The order
        # file's whole claim is "among the units that recorded, this one holds the
        # newest entry", so it is earned by an append that LANDED, and noting it any
        # earlier would let a refused or still-queued append claim precedence it never
        # earned -- pinning a unit ahead of its successor, whose next fold would then
        # read the pinned unit's phase and next step as current and act on them.
        # ``_undrained`` above is that queued case, so the window is real.
        #
        # Gated on ``landed`` rather than on ``drained`` or the refusal counters:
        # ``landed`` is read back from this unit's OWN log by content, while
        # ``dropped_writes`` is process-wide, so another session's refusal must not
        # suppress a precedence note this unit has genuinely earned.
        _record_unit_order(
            str(data.get("owner", "")), str(data.get("repo", "")), crew_id, session_id, root
        )
    refused = crew_log_emit.dropped_writes() != refused_before
    overflowed = crew_log_emit.overflow_writes(session_id) != overflow_before
    if overflowed and not landed:
        # The buffer rejected one of THIS session's appends at SUBMISSION for
        # crossing its memory ceiling while this one was in flight, and this entry is
        # not in the file: it was never queued and will never land, whatever the
        # drain says. Counted per session and apart from a storage refusal
        # (``overflow_writes`` against ``dropped_writes``), so it is checked apart --
        # a rejected entry answered as queued state would be published without ever
        # being persisted, and another session's rejection cannot flip this one.
        logger.warning("crew ledger: the crew log writer rejected this update at its ceiling")
        _undrained.pop(undrained_key, None)  # nothing of this write is queued to drain
        raise CrewLedgerNotRecorded(
            "this update was not recorded: the crew log writer is at its ceiling and "
            "rejected the append; nothing changed, send it again once it has drained"
        )
    if drained and not landed:
        # The writer is done and the entry is not in the file -- refused (the log
        # deleted under the write, the writer gave up on it) or unreadable twice. A
        # refusal is not a commit, and an entry that cannot be shown to exist is
        # answered the same way: the caller is told, never handed a record that may
        # be nowhere. The synthetic answer is made ONLY while the writer still holds
        # the entry.
        logger.warning("crew ledger: the crew log writer drained without landing this update")
        raise CrewLedgerNotRecorded(
            "this update was not recorded: the crew log did not take the append; "
            "nothing changed, the same update may be sent again"
        )
    durable = drained and landed and not refused
    if not drained:
        logger.warning(
            "crew ledger: the crew log writer did not drain within %.1fs; this update is "
            "queued and counted, not yet durable",
            _APPEND_FLUSH_SECONDS,
        )
    elif not durable:
        logger.warning(
            "crew ledger: the crew log refused an append while this update was in flight; "
            "this update landed, but a neighbouring one may not have"
        )
    if grown is None:
        # Reached only while the writer still holds the entry (not drained): the fold
        # advanced over the entry as sent, stamped with this side's clock -- an
        # approximation the next read corrects.
        pending = Entry(
            type=LEDGER_ENTRY_TYPE,
            seq=base.last_seq + 1,
            time=recorded_ms,
            src=_ENTRY_SRC,
            data=data,
        )
        grown = projection.advance(base, (pending,))
    state = projection.projection_of(grown).value
    key = str(number) if number is not None else ""
    item = next((record for record in state["items"] if str(record.get("number")) == key), None)
    skip_row = state["skips"].get(key)
    if skip_row is not None:
        skip_row = _standing_skip(data["owner"], data["repo"], root, key, skip_row)
    return {
        "item": item,
        "event": state["events"][0] if state["events"] else None,
        "skip": skip_row,
        "coalesced": False,
        "durable": durable,
    }


def _standing_skip(
    owner: str, repo: str, root: Path | None, key: str, own: dict[str, Any]
) -> dict[str, Any]:
    """The row the shared index answers for *key*, given this crew's own row *own*.

    A crew that passes on a number another crew already decided is told what
    STANDS -- the first decision, with the crew that made it -- not what it sent, so
    it can see its own reason was not the one kept. *own* is included explicitly
    because the just-appended entry may not have drained to the log yet.
    """
    rows: list[dict[str, Any]] = [own]
    rows.extend(row for k, row in read_skips(owner, repo, root).items() if k == key)
    standing = min(rows, key=_skip_precedence)
    return dict(standing)


def record_crew_checkpoint(
    owner: str,
    repo: str,
    crew_id: str,
    event_text: str,
    root: Path | None = None,
    *,
    session_id: str = "",
) -> dict[str, Any]:
    """Append one crew-level line -- a step that belongs to no issue.

    Takes no kind: there is exactly one crew-level kind, and the route already
    refuses anything else on the numberless path. Consecutive sweeps coalesce (see
    :func:`_append`): the first sweep after real work is written and a sweep landing
    on a sweep is answered with the existing line, whose timestamp marks when the
    idle stretch BEGAN.

    *session_id* is the crew's live crew log unit; the route resolves it from the
    crew's slot. Without one the write is refused (:class:`CrewLedgerUnavailable`).
    """
    _require_crew_log(session_id)
    with _crew_write_lock(crew_id):
        base = _prepare_write(owner, repo, crew_id, session_id, root)
        data = _entry_data(owner, repo, crew_id, None, {}, CREW_LEVEL_EVENT_KIND, event_text)
        return _append(crew_id, session_id, data, base, root)


def _prepare_write(
    owner: str, repo: str, crew_id: str, session_id: str, root: Path | None
) -> Any:
    """The fold a write validates against, with the one-time carry run -- the SAME
    sequence for every writer.

    The crew's unit order is published by :func:`_append`, once the entry is in this
    unit's log: the order file claims this unit holds the newest entry, and only a
    landed append earns that. The carry runs on a crew's first write after the
    upgrade whichever
    kind of write it is: an idle sweep is a routine first write, and a sweep that
    appended without carrying would leave the crew's open items unread -- reads
    reporting zero open items -- until some later item write happened to carry
    them. Called under the crew's write lock.
    """
    undrained_key = (str(data_home()), crew_id)
    receipt = _undrained.get(undrained_key)
    if receipt is not None:
        # The crew's last append was still in the writer when that write answered.
        # Drain it now, before folding, so this write validates against a record
        # that holds it. A writer STILL not draining refuses this write: a fold taken
        # now would be missing that entry, and the one-editor rule checked against
        # it could admit a second editor -- both entries would later land. Nothing
        # is changed by the refusal; the caller sends the same update again once the
        # writer has caught up, and the mark stays until the entry is SEEN.
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS):
            raise CrewLedgerNotRecorded(
                "the crew's previous update is still queued in the crew log writer; "
                "send this update again shortly"
            )
        # A QUIET WRITER IS NOT A LANDED ENTRY. A drop empties the buffer exactly as a
        # successful write does, so ``flush`` answers True either way; releasing the
        # mark on that alone would fold a record short one entry and hand the
        # one-editor rule a record that permits a second editor -- silently, because
        # the earlier call already answered. So the receipt is checked against the
        # unit's own log by content, and the mark is released only when the entry is
        # there. It is not, this write is refused like any other undrained one: the
        # caller resends, and that resend is what puts the missing update back.
        if not _landed_since(_projection(), receipt[0], receipt[1], receipt[2])[1]:
            raise CrewLedgerNotRecorded(
                "the crew's previous update never reached its crew log; send that "
                "update again before this one"
            )
        # AND PUBLISH THAT UNIT'S PRECEDENCE NOW, which is the only moment anything
        # can. The append path notes precedence only for an entry it saw land inside
        # the flush budget; an append the writer still held when that budget expired
        # lands later, and its own call has already returned. Its unit may by then be
        # RETIRED, so it has no next write of its own to make the claim -- but the
        # CREW does, and this is it. Left unpublished, a successor unit stamped by a
        # clock that went backwards sorts ahead of it in header order, and every read
        # then folds the retired unit's phase and next step as the current ones.
        #
        # Correctly ordered by WHERE this sits: the receipt's entry is the newest one
        # this crew has landed at this instant, and this runs before this write's own
        # append, whose landing moves the live unit after it.
        _record_unit_order(
            str(receipt[2].get("owner", "")),
            str(receipt[2].get("repo", "")),
            crew_id,
            receipt[0],
            root,
        )
        _undrained.pop(undrained_key, None)
    units = _units_for_write(owner, repo, crew_id, session_id, root)
    base = _fold_checkpoint(crew_id, units)
    # The carry runs when NO radar entry has ever folded into this crew's record --
    # the fold's ``crew_id`` is set by the first entry that names the crew, whichever
    # kind it is -- or when an earlier carry began and did not finish. Not when the
    # record merely holds no items and no passes: a crew whose every write was an
    # idle sweep has that shape for good, and keying on it would carry the
    # pre-projection files again whenever they are present without their marker,
    # re-stating retired work as live over a record that has moved on.
    if not base.state["crew_id"] or _carry_pending(owner, repo, crew_id, root):
        if _carry_legacy_forward(owner, repo, crew_id, session_id, root, folded=base.state):
            units = _units_for_write(owner, repo, crew_id, session_id, root)
            base = _fold_checkpoint(crew_id, units)
    return base


def _units_for_write(
    owner: str, repo: str, crew_id: str, session_id: str, root: Path | None
) -> tuple[str, ...]:
    """The crew's units for a WRITE: listed strictly, a failed listing refuses the write.

    A read that cannot list the units answers the empty record and says nothing
    false about what it could read. A write that folded an empty record because the
    listing failed would validate against it -- and the one-editor rule, checked
    against no items, admits a second editor into a log that cannot take a line back.
    """
    try:
        return crew_log_units(owner, repo, crew_id, root, live_session_id=session_id, strict=True)
    except Exception as exc:
        raise CrewLedgerNotRecorded(
            "the crew's logs could not be listed, so this update was not recorded; "
            "nothing changed, send it again"
        ) from exc


def commit_work_progress(
    owner: str,
    repo: str,
    crew_id: str,
    number: int,
    patch: dict[str, Any],
    event_kind: str,
    event_text: str,
    skip_reason: str | None = None,
    skip_scope: str = "",
    root: Path | None = None,
    *,
    session_id: str = "",
) -> dict[str, Any]:
    """Record one work-item update: the fields it sets, its progress line and, when
    it records a pass, the skip row -- as ONE entry. ``{"item", "event", "skip"}``.

    *skip_reason* is what makes this a pass: ``None`` means "no skip", and any string
    indexes one. The reason is the caller's to choose, but the COUPLING is not: an
    issue cannot be recorded as passed without the same entry carrying the item and
    the line that explain it, and there is no ordering in which a reader can see one
    without the others.

    The store's own refusals happen BEFORE anything is appended, against the crew's
    folded record: an unknown kind or phase, a crew-level kind with a number, and a
    second item entering an editing phase are all :class:`CrewStoreError`, and an
    append-only log cannot take a line back, so nothing is written until they pass.

    On a crew's FIRST write after the upgrade the pre-projection files are carried
    into the log first (:func:`_carry_legacy_forward`), so a crew upgraded mid-work
    finds its open items and the repository keeps its passes.
    """
    number = int(number)
    if event_kind not in EVENT_KINDS:
        raise CrewStoreError(f"unknown event kind {event_kind!r}")
    if event_kind == CREW_LEVEL_EVENT_KIND:
        raise CrewStoreError(f"event kind {event_kind!r} is crew-level and takes no issue number")
    _require_crew_log(session_id)
    with _crew_write_lock(crew_id):
        base = _prepare_write(owner, repo, crew_id, session_id, root)
        current = base.state["items"].get(str(number))
        if "phase" in patch:
            new_phase = str(patch["phase"] or "").strip()
            if new_phase not in PHASES:
                raise CrewStoreError(f"unknown phase {new_phase!r}")
            prev_phase = current["phase"] if current is not None else "selected"
            if new_phase in EDITING_PHASES and prev_phase not in EDITING_PHASES:
                other = _editing_item(base.state, exclude=number)
                if other is not None:
                    raise CrewStoreError(
                        f"crew {crew_id} is already editing #{other} — finish or "
                        "commit that before entering an editing phase on another issue"
                    )
        deferred = False
        if skip_reason is not None:
            # Observe the shared index BEFORE recording: a pass on a number another
            # crew already decided defers to that decision, and says so in the entry,
            # so the union orders the two by what this writer saw and not by clocks.
            standing = read_skips(owner, repo, root).get(str(number))
            deferred = standing is not None and str(standing.get("crew_id") or "") != crew_id
        data = _entry_data(
            owner, repo, crew_id, number, patch, event_kind, event_text,
            skip_reason=skip_reason, skip_scope=skip_scope, skip_deferred=deferred,
        )
        return _append(crew_id, session_id, data, base, root)


# ── carrying the pre-projection files forward ───────────────────────────────


def _read_legacy_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _legacy_text(value: Any, limit: int = _MAX_CARRIED_TEXT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _legacy_ci_state(value: dict[str, Any]) -> dict[str, Any]:
    """The members of a legacy ``ci_state`` the record tool would have accepted.

    The fold's own bounder (:func:`projection.radar_ci_state`): only the kept keys,
    each with the tool's type and ceiling, anything else dropped -- one oversized
    value is what could make a row unfit for an entry. Legacy files were written by
    ``json.dumps``, so a counter can only be an int or a float here; a float is not
    a counter and is dropped like any other wrong shape.
    """
    return _projection().radar_ci_state(value)


#: Free-text members of a carried entry, clamped together when the row must shrink.
_CARRIED_TEXT_FIELDS = ("decision", "why", "next", "worktree", "branch", "base_sha", "event")
#: The clamps tried in turn when a carried row does not fit one entry. The text
#: ceiling is in characters and the entry ceiling in bytes, and a character can
#: serialize to six bytes, so a row of long non-ASCII text can be over the ceiling
#: at the first clamp and under it at a later one.
_CARRY_SHRINK_STEPS = (_MAX_CARRIED_TEXT, 2000, 1000, 500, 256)


def _shrink_to_fit(data: dict[str, Any], fits: Any) -> dict[str, Any] | None:
    """*data* clamped until *fits* accepts it, or ``None`` when no clamp does.

    Clamping loses the tail of a long text; refusing the row loses the record. The
    fold clamps every text on the way in, so the first step costs nothing the fold
    would have kept, and a later step is taken only when the first does not fit.
    """
    for limit in _CARRY_SHRINK_STEPS:
        shrunk = dict(data)
        for key in _CARRIED_TEXT_FIELDS:
            if isinstance(shrunk.get(key), str):
                shrunk[key] = shrunk[key][:limit]
        for nested in ("tried", "skip"):
            inner = shrunk.get(nested)
            if isinstance(inner, dict):
                shrunk[nested] = {
                    k: (v[:limit] if isinstance(v, str) and k != "scope" else v)
                    for k, v in inner.items()
                }
        if fits(shrunk):
            return shrunk
    return None


def _legacy_number(value: Any, field: str = "number") -> int | None:
    """*value* as a number inside the record tool's range for *field*, or ``None``.

    The carry applies the magnitude bound the fold applies to the bytes it reads
    (:data:`RADAR_NUMBER_BOUNDS`), for the same reason the carry re-applies the CI
    bounds: a pre-projection file can hold any number, and one outside the range the
    tool would have accepted is dropped by the fold -- which on an item row would
    silently fold it as a crew-level line instead. Bounded here, such a row is unfit
    and named, so the carry does not finish and the write is refused.
    """
    number = _finite_int(value)
    if number is None:
        return None
    low, high = RADAR_NUMBER_BOUNDS[field]
    return number if low <= number <= high else None


def _legacy_item_entries(
    owner: str, repo: str, crew_id: str, legacy: dict[str, Any]
) -> list[dict[str, Any]]:
    """The carried entries for one pre-projection work item, or ``[]`` without a number.

    Carries the record a resume needs -- phase, next, decision, EVERY rejected
    approach, where the work lives and what is on the forge -- plus its own stamps,
    and an event that says where it came from. The phase rides with the event,
    keeping the rule that a phase never appears without a logged reason.

    One entry holds ONE ``tried``, so a `tried` history of *n* rows is *n* entries,
    each re-stating the whole record and differing only in the approach it carries.
    Order is the file's own, so the folded list ends with the same newest row the file
    ended with. Carrying the newest approach alone would delete the field's whole
    purpose for every pre-upgrade item the moment the marker is written, and the loss
    is not recoverable afterwards because the files are never read again. It needs
    nothing new in the fold: a carried ``tried`` is already deduplicated by content
    there and the list is already clamped, so a carry run twice is not a history listed
    twice, and re-stated scalars apply idempotently.

    The entries after the first carry no new ``phase`` VALUE, which is what keeps them
    quiet: the fold writes a phase line only for an entry that MOVED the item, so the
    item still enters its phase exactly once however long its history is.
    """
    number = _legacy_number(legacy.get("number"))
    if number is None:
        return []
    data: dict[str, Any] = {"crew_id": crew_id, "owner": owner, "repo": repo, "number": number}
    phase = legacy.get("phase")
    data["phase"] = phase if isinstance(phase, str) and phase in PHASES else "selected"
    for key in ("decision", "why", "next", "worktree", "branch", "base_sha"):
        if isinstance(legacy.get(key), str) and legacy[key]:
            data[key] = _legacy_text(legacy[key])
    if isinstance(legacy.get("outcome"), str) and legacy["outcome"]:
        data["outcome"] = _legacy_text(legacy["outcome"], 256)
    for key in ("pr_number", "claim_comment_id"):
        value = _legacy_number(legacy.get(key), key)
        if value is not None:
            data[key] = value
    if isinstance(legacy.get("ci_state"), dict) and legacy["ci_state"]:
        # Only the members the fold keeps, each bounded the way the record tool
        # bounds it on the way in: the file could hold any keys and any values, and
        # one oversized member is the one thing that could push an otherwise valid
        # row past the entry ceiling and leave it uncarried. Nothing the record tool
        # would have accepted is dropped.
        ci = _legacy_ci_state(legacy["ci_state"])
        if ci:
            data["ci_state"] = ci
    if isinstance(legacy.get("labels_applied"), list):
        # The record tool's own bounds (20 labels, short strings), for the same reason.
        data["labels_applied"] = [
            x[:256] for x in legacy["labels_applied"] if isinstance(x, str)
        ][:20]
    rows: list[dict[str, str]] = []
    dropped = 0
    tried = legacy.get("tried")
    if isinstance(tried, list):
        for row in tried:
            if (
                isinstance(row, dict)
                and isinstance(row.get("approach"), str)
                and row["approach"].strip()
            ):
                rows.append(
                    {
                        "approach": _legacy_text(row["approach"]),
                        "rejected_because": _legacy_text(row.get("rejected_because", "")),
                    }
                )
            else:
                # Not a row the fold would have kept either -- it drops a ``tried``
                # without a non-blank ``approach``. Counted so the event says so.
                dropped += 1
    tried_limit = _projection().RADAR_TRIED_LIMIT
    if len(rows) > tried_limit:
        # The fold keeps an item's newest ``tried_limit`` approaches, so rows past
        # that ceiling would be evicted by the very read that folded them. Left out
        # here instead, where the count can be SAID once rather than each row costing
        # an entry to be discarded.
        dropped += len(rows) - tried_limit
        del rows[: len(rows) - tried_limit]
    for stamp in ("claimed_at", "last_progress_at", "finished_at"):
        if isinstance(legacy.get(stamp), str) and legacy[stamp]:
            data[stamp] = _legacy_text(legacy[stamp], 64)
    data["carried"] = True
    note = "carried forward from the pre-projection crew ledger files"
    if dropped > 0:
        note += f"; not carried: {dropped} unreadable or over-ceiling rejected approach(es)"
    data["event"] = note
    data["event_kind"] = "claim"
    if rows:
        data["tried"] = rows[0]
    entries = [data]
    for position, row in enumerate(rows[1:], start=2):
        entries.append(
            {
                # The WHOLE record again, not just the identity and the row. A carried
                # entry re-states a record, and the fold applies the same scalars
                # idempotently, so repeating them costs a re-read and buys the one
                # thing an identity-only continuation cannot: if the writer takes this
                # entry and refuses the first, the item folds with its real phase and
                # next step instead of appearing as a bare ``selected`` placeholder
                # the crew would read as live work it never claimed.
                **data,
                "tried": row,
                "event": (
                    f"rejected approach {position} of {len(rows)} carried forward from "
                    "the pre-projection crew ledger files"
                ),
            }
        )
    return entries


def _legacy_skip_entry(owner: str, repo: str, crew_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """The carried entry for one pre-projection pass -- a skip row and no work item."""
    number = _legacy_number(row.get("number"))
    if number is None:
        return None
    skip: dict[str, Any] = {
        "reason": _legacy_text(row.get("reason")),
        "scope": coerce_skip_scope(row.get("scope")),
    }
    if isinstance(row.get("crew_id"), str) and row["crew_id"]:
        skip["crew_id"] = _legacy_text(row["crew_id"], 64)
    if isinstance(row.get("decided_at"), str) and row["decided_at"]:
        skip["decided_at"] = _legacy_text(row["decided_at"], 64)
    return {
        "crew_id": crew_id,
        "owner": owner,
        "repo": repo,
        "number": number,
        "skip": skip,
        "carried": True,
        "event": "pass carried forward from the pre-projection shared skip index",
        "event_kind": "skip",
    }


def _tried_key(row: Mapping[str, Any]) -> tuple[str, str]:
    """A rejected approach's identity, the way the fold deduplicates one.

    The fold strips the approach and keeps the reason as it is, so the same two
    members compared the same way are what make a row already present.
    """
    return (
        str(row.get("approach") or "").strip(),
        str(row.get("rejected_because") or ""),
    )


def _carried_history_complete(held: Mapping[str, Any], entries: list[dict[str, Any]]) -> bool:
    """Whether *held* already holds every rejected approach *entries* would carry.

    An item is several entries, so a carry can land some of them and have the rest
    refused -- and the run after it must be able to tell the two apart. A record that
    already has the whole history is left alone, because re-stating the file's fields
    over an item the crew has since worked would set it back to its pre-projection
    state. A record MISSING a row is carried again: the refused entries are the only
    copy of those approaches, the files are never read again once the carry is marked
    finished, and re-emitting costs nothing wrong -- the fold drops a carried approach
    it already has and re-applies the other fields to the same values.

    Wrong in either direction is safe, which is why it compares content rather than
    counting: a false "incomplete" re-emits rows the fold then deduplicates, and a
    false "complete" is no worse than not asking at all.
    """
    have = {
        _tried_key(row)
        for row in held.get("tried") or []
        if isinstance(row, Mapping)
    }
    return all(
        _tried_key(data["tried"]) in have
        for data in entries
        if isinstance(data.get("tried"), Mapping)
    )


def _legacy_carry_rows(
    owner: str,
    repo: str,
    crew_id: str,
    root: Path | None,
    *,
    folded: Mapping[str, Any] | None,
    fits: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The payloads a carry WOULD append, in order, and the rows it could not build.

    Shared by :func:`_carry_legacy_forward` and its read-side preview
    (:func:`_legacy_preview`), so the two cannot answer differently about what the
    upgrade brings forward: the preview FOLDS exactly these payloads where the carry
    appends them, which makes the previewed record the post-carry record by
    construction rather than by two implementations agreeing. Pure -- it reads the
    pre-projection files and writes nothing, not even the begun markers, which belong
    to the carry alone and are what make it a write.

    A work item *folded* already holds with its WHOLE carried history is left out: it
    landed on an earlier run, or the crew has since worked it, and a carried entry
    re-states the file's fields as an update. One whose history the record is missing
    a row of is carried again, because those refused entries are the only copy of
    those approaches (:func:`_carried_history_complete`). A pass the record holds is
    left out outright -- a pass is one entry, so holding it is holding all of it. A row
    that cannot be built -- unreadable, not a record, without a recoverable number,
    too large at every clamp -- is NAMED in the second list rather than passed over,
    because a carry that finished with a row left behind would discard that row's
    state for good.
    """
    crew_dir = crews_dir(owner, repo, root) / _require_crew_id(crew_id)
    items_marker = crew_dir / _ITEMS_CARRIED_MARKER
    legacy_skips = skips_path(owner, repo, root)
    skips_marker = crews_dir(owner, repo, root) / _SKIPS_CARRIED_MARKER
    held_items: Mapping[str, Any] = folded["items"] if folded else {}
    held_skips: set[str] = set(folded["skips"]) if folded else set()
    rows: list[dict[str, Any]] = []
    unfit: list[str] = []
    if crew_dir.is_dir() and not items_marker.exists():
        for path in sorted(crew_dir.glob("*.json")):
            legacy = _read_legacy_json(path)
            if not isinstance(legacy, dict):
                # Unreadable, or not a record. Named, not skipped: a skipped row
                # would be left behind by a finished carry.
                unfit.append(path.name)
                continue
            if _legacy_number(legacy.get("number")) is None:
                # The pre-projection writer named each item file by its number, so
                # a row without one still has it in the file name.
                try:
                    legacy = {**legacy, "number": int(path.stem)}
                except ValueError:
                    unfit.append(path.name)
                    continue
            entries = _legacy_item_entries(owner, repo, crew_id, legacy)
            if not entries:
                unfit.append(path.name)
                continue
            standing = held_items.get(str(entries[0]["number"]))
            if standing is not None and _carried_history_complete(standing, entries):
                continue
            fitted: list[dict[str, Any]] = []
            for data in entries:
                shrunk = _shrink_to_fit(data, fits)
                if shrunk is None:
                    # One entry of an item that fits at no clamp makes the WHOLE row
                    # unfit: appending the rest and marking the carry finished would
                    # leave that part of the item's history nowhere, and the files are
                    # never read again once marked.
                    fitted = []
                    break
                fitted.append(shrunk)
            if not fitted:
                unfit.append(path.name)
                continue
            rows.extend(fitted)
    if legacy_skips.is_file() and not skips_marker.exists():
        stored = _read_legacy_json(legacy_skips)
        if not isinstance(stored, dict):
            unfit.append(legacy_skips.name)
        else:
            for key, row in stored.items():
                if not isinstance(row, dict):
                    unfit.append(f"{legacy_skips.name}#{key}")
                    continue
                if _legacy_number(row.get("number")) is None:
                    try:
                        row = {**row, "number": int(str(key))}
                    except (TypeError, ValueError):
                        unfit.append(f"{legacy_skips.name}#{key}")
                        continue
                skip_data = _legacy_skip_entry(owner, repo, crew_id, row)
                if skip_data is None:
                    unfit.append(f"{legacy_skips.name}#{key}")
                    continue
                if str(skip_data["number"]) in held_skips:
                    continue
                fitted_skip = _shrink_to_fit(skip_data, fits)
                if fitted_skip is None:
                    unfit.append(f"{legacy_skips.name}#{key}")
                    continue
                rows.append(fitted_skip)
    return rows, unfit


def _legacy_preview(owner: str, repo: str, crew_id: str, root: Path | None, base: Any) -> dict[str, Any] | None:
    """The record *crew_id*'s pending carry WOULD fold to, or ``None`` for no carry.

    The read half of the upgrade carry. A carry appends, so it can only run from a
    write: it needs a crew log to append into, it takes the crew's write lock, and it
    refuses its caller when a row did not land. A READ has none of those -- it may be
    asked with no live session at all, and it must not raise -- so without this the
    first read after an upgrade answers the EMPTY record while the pre-projection
    files sit there waiting for a write to consume them. Those reads are not
    cosmetic: they are the pre-investigate nudge snapshot (``list_work_items``,
    ``open_slot_count``) and the repository's shared skip index (``read_skips``), so
    the first turn of an upgraded crew would be told it has no open items and that no
    issue has been passed on -- and act on it, with forge comments that cannot be
    taken back, before any carry runs.

    So the read FOLDS the pending entries in without appending them, over the same
    base, through the same projection, from the same payloads the carry builds
    (:func:`_legacy_carry_rows`). The answer is the post-carry record by construction
    rather than by agreement, and the carry stays the only thing that makes it
    PERMANENT -- which is why this is not a second authority: it appends nothing,
    marks nothing, and stops answering the moment the log holds the rows.

    Three things stop it resurrecting state the log has settled. It is reached only
    on the write path's OWN carry trigger -- no radar entry has ever folded into this
    crew's record, or a carry began and did not finish -- so the two agree on when a
    carry is owed as well as on what it carries, and a crew whose every write was an
    idle sweep is not previewed for good. A row whose number the folded base already
    holds is left out by the shared builder, so a half-finished carry previews only
    what is still missing rather than re-stating an item the crew has since worked.
    And it folds ONTO the base, so entries that carry events but no item keep them.

    Never raises: every failure answers ``None``, which is the record the caller
    already had.
    """
    try:
        if not (not base.state["crew_id"] or _carry_pending(owner, repo, crew_id, root)):
            return None
        from kiro_crew.crew_log import emit as crew_log_emit

        projection = _projection()
        pending, _unfit = _legacy_carry_rows(
            owner,
            repo,
            crew_id,
            root,
            folded=base.state,
            # The same size gate the carry applies, so a row the carry will shrink is
            # previewed shrunk. Asked through the emitter because that is the
            # serializer the append uses; it is a pure byte check that does not need
            # the log switched ON, which is what lets a crew whose recording is off
            # still READ its pre-projection state instead of the empty record.
            fits=crew_log_emit.radar_entry_fits,
        )
        if not pending:
            # Nothing to bring forward: an upgraded install with no pre-projection
            # files, or every row already carried. Not an error and not an empty
            # answer -- the caller keeps the record it folded.
            return None
        from kiro_crew.crew_log.schema import Entry

        entries = tuple(
            Entry(
                type=LEDGER_ENTRY_TYPE,
                # One past what the fold consumed, which is all ``advance`` asks.
                # These objects are never written: the carry's own append takes each
                # seq from the store under the store's lock.
                seq=base.last_seq + offset,
                time=int(time.time() * 1000),
                src=_ENTRY_SRC,
                data=data,
            )
            for offset, data in enumerate(pending, start=1)
        )
        return dict(projection.projection_of(projection.advance(base, entries)).value)
    except Exception:
        logger.warning(
            "crew ledger: could not preview crew %s's pending carry; reading the "
            "record as folded",
            crew_id,
            exc_info=True,
        )
        return None


def _carry_legacy_forward(
    owner: str,
    repo: str,
    crew_id: str,
    session_id: str,
    root: Path | None = None,
    *,
    folded: Mapping[str, Any] | None = None,
) -> bool:
    """Append the pre-projection files' state into *session_id*'s crew log, once.

    Runs on a crew's first write -- the one before any radar entry has folded into
    its record -- and again on a later write while an earlier carry is unfinished.
    Two things are carried: the crew's own work-item files, and -- by whichever crew
    of the repository writes first -- the repository's shared skip index, whose rows
    keep the crew that decided them and when. A pass is one entry and a work item is
    one entry per rejected approach it carries (at least one), so the work is bounded
    by the files and by each item's clamped `tried` history, and happens once per crew
    and once per repository.

    *folded* is the crew's record as folded before this carry. A row whose number
    the record already holds is NOT emitted again: it landed on an earlier run, or
    the crew has since written that item itself, and a carried entry re-states the
    file's fields as an update, so re-emitting it would set a live item back to its
    pre-projection state. Only rows the record lacks are appended.

    A marker beside the carried files records that the carry FINISHED; the files
    are left in place and never read again. The marker is written only when every
    row was carried and read back from the log -- a drained writer is not proof,
    since the writer can refuse an entry and drain quietly. A row that could not be
    carried -- unreadable, not a record, without a number that can be recovered,
    or too large for one entry at every clamp -- is named in the log and leaves the
    carry unfinished, so the files stay unmarked and the next write tries again:
    marking a carry finished with a row left behind would discard that row's state
    for good.

    Returns whether anything was appended, so the caller re-folds.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    crew_dir = crews_dir(owner, repo, root) / _require_crew_id(crew_id)
    items_marker = crew_dir / _ITEMS_CARRIED_MARKER
    legacy_skips = skips_path(owner, repo, root)
    skips_marker = crews_dir(owner, repo, root) / _SKIPS_CARRIED_MARKER
    seq_before = _unit_last_seq(session_id)
    # Marked BEGUN before a single row is read, so a crash mid-carry leaves the marker
    # pair ``_carry_pending`` reads as unfinished. Written HERE and not in the shared
    # row builder, which is what lets the read-side preview use the same builder and
    # still be a read.
    if crew_dir.is_dir() and not items_marker.exists():
        _mark_carry_begun(crew_dir / _ITEMS_CARRY_BEGUN_MARKER)
    if legacy_skips.is_file() and not skips_marker.exists():
        _mark_carry_begun(legacy_skips.parent / _SKIPS_CARRY_BEGUN_MARKER)
    carried, unfit = _legacy_carry_rows(
        owner, repo, crew_id, root, folded=folded, fits=crew_log_emit.radar_entry_fits
    )
    for data in carried:
        crew_log_emit.on_radar_recorded(session_id, data)
    if carried:
        if not crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS):
            # The carried rows are still in the writer. The write that triggered the
            # carry would now refold a record MISSING them -- a carried editing item
            # among them, and the one-editor rule could admit a second editor into a
            # log that cannot take a line back. So the write is refused, the crew is
            # marked undrained (its next write drains before it folds), and the files
            # stay unmarked so the carry runs again if anything did not land.
            logger.warning("crew ledger: the carry-forward did not drain within the flush budget")
            _undrained[(str(data_home()), crew_id)] = (session_id, seq_before, carried[-1])
            raise CrewLedgerNotRecorded(
                "the carry of this crew's pre-projection files is still queued in the "
                "crew log writer; send this update again shortly"
            )
        if not _all_landed(session_id, seq_before, carried):
            # Drained, but a row the writer was handed is not in the file: it refused
            # it. The record this write would fold is short of that row, so the write
            # is refused too; the files stay unmarked and are carried again next time,
            # and the warning names the crew so a row refused every time is found.
            logger.warning(
                "crew ledger: the carry-forward for crew %s did not fully land; the "
                "pre-projection files stay unmarked and are carried again on the next write",
                crew_id,
            )
            raise CrewLedgerNotRecorded(
                "a row of this crew's pre-projection files did not land in the crew "
                "log; send this update again"
            )
    if unfit:
        # A row that could not be carried -- unreadable, not a record, without a
        # recoverable number in the tool's range, or too large for one entry at every
        # clamp -- REFUSES this write, the same answer as a row that did not land.
        # Marking the carry finished would discard that row's stored state for good,
        # since the files are never read again once marked; folding without it is
        # worse than refusing, because an omitted row in an editing phase leaves its
        # item out of the record the one-editor rule reads, so another item can enter
        # that phase while the row still holds it, and a later carry then brings a
        # second editor into the record. The files stay unmarked and the rows are
        # named, so a retry appends only what is still missing once the named files
        # are repaired or moved aside.
        logger.warning(
            "crew ledger: %d pre-projection record(s) could not be carried into the crew "
            "log; the files stay unmarked and are tried again on the next write: %s",
            len(unfit),
            ", ".join(unfit),
        )
        raise CrewLedgerNotRecorded(
            "a row of this crew's pre-projection files could not be carried into the "
            f"crew log ({', '.join(unfit[:5])}); repair or move the named file(s) "
            "aside, then send this update again"
        )
    try:
        if crew_dir.is_dir():
            items_marker.touch(exist_ok=True)
            (crew_dir / _ITEMS_CARRY_BEGUN_MARKER).unlink(missing_ok=True)
        if legacy_skips.is_file():
            skips_marker.touch(exist_ok=True)
            (legacy_skips.parent / _SKIPS_CARRY_BEGUN_MARKER).unlink(missing_ok=True)
    except OSError:
        logger.warning("crew ledger: could not mark the pre-projection files as carried", exc_info=True)
    return bool(carried)


def _touch(path: Path) -> None:
    try:
        path.touch(exist_ok=True)
    except OSError:
        logger.warning("crew ledger: could not write the carry marker %s", path, exc_info=True)


def _mark_carry_begun(path: Path) -> None:
    """Write a carry's BEGUN marker, or refuse the write that would have carried.

    The begun marker is what makes a carry that stops half-way run again: without it
    a partial carry leaves a record that has folded a radar entry, and such a record
    never carries on its own.
    So a begun marker that cannot be written is not best-effort like the finished one
    (a lost finished marker only re-runs an idempotent carry) -- nothing is emitted, and
    the write is refused so the caller sends it again once the marker can be written.
    """
    try:
        path.touch(exist_ok=True)
    except OSError as exc:
        logger.warning("crew ledger: could not write the carry marker %s", path, exc_info=True)
        raise CrewLedgerNotRecorded(
            "the carry of this crew's pre-projection files could not be marked as begun; "
            "nothing was carried, send this update again"
        ) from exc


def _carry_pending(owner: str, repo: str, crew_id: str, root: Path | None = None) -> bool:
    """Whether an earlier carry for this crew or its repository began and did not finish."""
    crew_dir = crews_dir(owner, repo, root) / _require_crew_id(crew_id)
    repo_dir = crews_dir(owner, repo, root)
    items_unfinished = (crew_dir / _ITEMS_CARRY_BEGUN_MARKER).exists() and not (
        crew_dir / _ITEMS_CARRIED_MARKER
    ).exists()
    skips_unfinished = (repo_dir / _SKIPS_CARRY_BEGUN_MARKER).exists() and not (
        repo_dir / _SKIPS_CARRIED_MARKER
    ).exists()
    return items_unfinished or skips_unfinished


def _all_landed(session_id: str, seq_before: int, payloads: list[dict[str, Any]]) -> bool:
    """Whether every one of *payloads* is in *session_id*'s log past *seq_before*.

    Read from the file rather than inferred from the writer: a drained writer can
    have REFUSED an entry (too large, undeclared field) and be quiet about it.
    """
    projection = _projection()
    try:
        handle = projection.open_session_log(session_id)
        if handle is None:
            return False
        since = [
            e.data
            for e in handle.iter_from(seq_before + 1, known=projection.KNOWN_TYPES)
            if e.type == LEDGER_ENTRY_TYPE
        ]
    except Exception:
        log_exception_text(
            logger, logging.DEBUG, "crew ledger: could not read the carried entries back"
        )
        return False
    return all(any(landed == data for landed in since) for data in payloads)


#: Bump on any incompatible change to :func:`fold_fabric`'s item shape. Its own
#: field, not :data:`CREW_SCHEMA`: the fabric payload is derived, not stored, so it
#: versions on its own cadence.
FABRIC_SCHEMA = 1

#: The phases that form the drawn SPINE, in topological order. It is
#: :data:`PHASES` minus the off-spine ones, and ``resolved`` is the ONLY terminal
#: left on it — the other terminals are alternate endings, not later stages, so
#: giving them a column would imply a skipped item got further than a claimed one
#: (PLAN design decision #2). ``awaiting-reply`` is off-spine for the same reason:
#: the crew is not the actor, it has handed the issue back to a human.
SPINE_PHASES = (
    "selected",
    "claimed",
    "investigating",
    "implementing",
    "awaiting-ci",
    "addressing-review",
    "awaiting-merge",
    "resolved",
)

#: Phases that render as a stub OFF the lane, not as a column. Every phase not on
#: the spine, computed from :data:`PHASES` so a phase added to one set can never be
#: silently absent from the other.
OFF_SPINE_PHASES = frozenset(PHASES) - frozenset(SPINE_PHASES)


def _fold_one_item(
    record: dict[str, Any],
    events: list[dict[str, Any]],
    title_hints: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Fold ONE work item and its phase-bearing ledger lines into a fabric item.

    *events* are this item's lines in TIME order (oldest first). Each contributes a
    ``phase`` only if it carries one — a pre-feature line, or a write that carried
    no phase, simply has no key and is skipped here, which degrades the drawing to
    L0/L1 for that item rather than failing the fold.

    *title_hints* maps issue/PR ``number`` to the issue's REAL title, seeded from
    the issues and pulls list caches (:func:`_fabric_title_hints`). The crew ledger
    stores NO title — a work item carries ``number`` and ``phase``, not what the
    issue is called — so the title has to come from the same list caches Issue
    Radar already keeps, at zero extra API cost. A number with no cached title
    (never fetched, or aged out) folds to ``""`` and the lane shows its id only,
    rather than mislabelling the lane with the crew's resumable ``next`` intent.

    Three things a naive fold gets wrong, each pinned by a test:

    * **The live phase is the record's, authoritative — never the max timeline
      index.** A round-trip through review (``awaiting-ci -> addressing-review ->
      awaiting-ci``) ends LEFT of where it has been, so keying the head off the
      furthest column reached puts the item in a phase it already left. This
      function reads ``phase`` straight off the record and returns it as its own
      field; ``timeline`` is only the history.

    * **Off-spine phases are an ``exit``, not a timeline entry.** ``skipped`` /
      ``yielded`` / ``handed-back`` / ``preempted`` / ``awaiting-reply`` leave the
      spine's topology intact. ``exit`` is the LAST off-spine line seen — but it is
      cleared the moment a later on-spine line reopens the item, so it stands only
      when the item's live phase is itself off-spine. (The record's live phase is
      the tie-breaker: a torn ledger that ends off-spine while the record is on-spine
      still clears the exit.)

    * **Re-entering a phase after an exit is a reopen, and each restarts the dwell
      clock.** ``reopens`` counts an on-spine line landing on an already-visited
      phase while an exit was standing — the store clears ``outcome``/``finished_at``
      on exactly that transition, so it is a real second life, not churn.
    """
    timeline: list[dict[str, Any]] = []
    seen_spine: set[str] = set()
    exit_entry: dict[str, str] | None = None
    reopens = 0

    for ev in events:
        ph = ev.get("phase")
        if not isinstance(ph, str) or ph not in PHASES:
            continue
        at = str(ev.get("ts") or "")
        if ph in OFF_SPINE_PHASES:
            exit_entry = {"phase": ph, "at": at}
            continue
        # An on-spine line.
        if ph in seen_spine and exit_entry is not None:
            # Came back to a phase already visited, AND an exit was standing — this
            # is a genuine reopen (the store cleared the terminal fields to make it
            # one), not a round-trip through review within the spine.
            reopens += 1
        if exit_entry is not None:
            # Reopened: the exit does not hold, whatever it was.
            exit_entry = None
        seen_spine.add(ph)
        timeline.append({"phase": ph, "at": at})

    live_phase = record.get("phase")
    if not isinstance(live_phase, str) or live_phase not in PHASES:
        live_phase = "selected"
    # The record is authoritative. If it says the item is on-spine, no stale exit
    # from a torn tail may stand; if it says off-spine, that is the exit even when
    # the fold's own last line disagreed.
    if live_phase in OFF_SPINE_PHASES:
        if exit_entry is None or exit_entry.get("phase") != live_phase:
            exit_entry = {
                "phase": live_phase,
                "at": str(record.get("finished_at") or record.get("last_progress_at") or ""),
            }
    else:
        exit_entry = None

    number = record.get("number")
    hint_number = number if isinstance(number, int) and not isinstance(number, bool) else None
    title = ""
    if title_hints is not None and hint_number is not None:
        title = str(title_hints.get(hint_number) or "")
    # ``next`` is the crew's RESUMABLE INTENT ("add the Windows branch to
    # _safe_chmod"), not what the issue is called — it stays available under its
    # own name for a view that wants to show what the crew is about to do, but it
    # is NEVER the title. The title is the issue's real title from the list caches.
    return {
        "number": number,
        "crew_id": record.get("crew_id"),
        "title": title,
        "next": str(record.get("next") or ""),
        "pr_number": _finite_int(record.get("pr_number")),
        "phase": live_phase,
        # No `ci_state` here. It had no reader -- the app declared a type for it and
        # never touched the value -- and it was the one field in this payload carrying
        # an arbitrary nested dict straight from the record. `json.dumps` writes a
        # non-finite float as bare `NaN`, which is not JSON, so one hand-edited or
        # restored record could make `GET /crew/fabric` unparseable and the browser
        # would drop EVERY lane, not just that item. `pr_number` above already goes
        # through `_finite_int` for exactly this reason; an unread field does not earn
        # a sanitiser, it earns deletion.
        "timeline": timeline,
        "exit": exit_entry,
        "reopens": reopens,
    }


def _fabric_title_hints(owner: str, repo: str, root: Path | None = None) -> dict[int, str]:
    """``number -> issue/PR title``, seeded from the issues and pulls list caches.

    The crew ledger records no title — a work item is ``number`` + ``phase`` — so
    the lane's real title comes from the SAME list caches Issue Radar already
    keeps, at ZERO extra API cost: this reads whatever is cached and never fetches.
    A number that was never cached (or whose cache aged out) simply has no hint,
    and its lane degrades to showing the id alone rather than being mislabelled.

    Both open AND closed states are read, because a crew work item outlives the
    issue's open state — an item can be ``resolved``/``skipped`` while its issue is
    closed, so the open cache alone would drop exactly the finished lanes. Issues
    are read first and pulls layered on top: a work item that has become a PR is
    keyed by the ISSUE number it was claimed under, and the PR title is the more
    specific label for that lane once one exists, so it wins on a collision.

    Never raises: each cache read already tolerates an absent/torn/stale-schema
    file by returning ``None`` (treated as empty here), so a repo with no caches
    yields ``{}`` and every lane falls back to its id.
    """
    hints: dict[int, str] = {}

    def _absorb(rows: list[dict[str, Any]] | None) -> None:
        if not rows:
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            num = row.get("number")
            if not isinstance(num, int) or isinstance(num, bool):
                continue
            title = row.get("title")
            if isinstance(title, str) and title.strip():
                hints[num] = title.strip()

    # A cache read that fails must cost the TITLES, not the endpoint. These files are
    # Issue Radar's, written by its own refresh, so a refresh landing between the
    # reader's existence check and its read raises rather than returning empty -- and
    # a 500 for a failed title lookup contradicts the degradation this join already
    # promises everywhere else, where a number with no cached title simply renders as
    # its id. Broad on purpose: any unreadable cache means "no hints from that cache".
    for reader in (store.read_issues_cache, store.read_pulls_cache):
        for state in ("open", "closed"):
            try:
                _absorb(reader(owner, repo, root, state=state))
            except Exception:
                logger.debug(
                    "fabric title hints: %s cache unreadable for %s/%s (%s)",
                    reader.__name__, owner, repo, state, exc_info=True,
                )
    return hints


def fold_fabric(owner: str, repo: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Every crew work item in this repo, folded into a fabric lane, newest first.

    ONE pass over the repo's crews, each read from its folded ledger: the item
    records and, per item, the lines that ENTERED a phase (the fold keeps those per
    item, so a lane parked in one phase for a long time keeps its entry line however
    much the other lanes chatter). A pre-projection line that carried no phase
    contributed nothing then and contributes nothing now, so an old history degrades
    the drawing rather than breaking the fold.

    Ordered newest-progress-first — the same order the crew page lists items in — so
    an operator scanning the pipeline sees the freshly-moved lanes at the top.
    """
    # Real issue/PR titles from the list caches Issue Radar already keeps — one
    # read per cache for the WHOLE fold, not per lane.
    title_hints = _fabric_title_hints(owner, repo, root)

    items: list[dict[str, Any]] = []
    for crew in list_crews(owner, repo, root, include_retired=True):
        cid = str(crew.get("id") or "")
        if not cid:
            continue
        ledger = read_ledger(owner, repo, cid, root)
        phase_lines = ledger.get("phase_lines") or {}
        for rec in ledger["items"]:
            num = rec.get("number")
            if not isinstance(num, int) or isinstance(num, bool):
                continue
            events = [
                {"phase": row.get("phase"), "ts": row.get("at")}
                for row in phase_lines.get(str(num), [])
            ]
            item = _fold_one_item(rec, events, title_hints)
            item["_sort"] = str(rec.get("last_progress_at") or "")
            items.append(item)

    items.sort(key=lambda it: it.pop("_sort"), reverse=True)
    return items
