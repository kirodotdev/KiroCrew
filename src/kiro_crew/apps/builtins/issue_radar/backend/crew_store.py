"""Crew records, work items and the append-only event ledger.

One repository's crews live under its repo data dir::

    repos/<owner>/<repo>/crews/settings.json      protocol constants, repo-wide
    repos/<owner>/<repo>/crews/<crew_id>.json     one crew
    repos/<owner>/<repo>/crews/<crew_id>/<n>.json one work item (crew × issue)
    repos/<owner>/<repo>/crews/events.jsonl       append-only progress log

Every file carries ``schema``. Issue Radar's usual versioning strategy — a schema
mismatch is a cache miss, refetch from the forge — does NOT transfer here: a crew
record has no upstream to refetch from, so readers coerce forward on read and a
real migration is required if the shape ever changes incompatibly.

Locking. ``store.py``'s per-record lock is the model, with one deliberate
difference: work-item writes take the **crew-level** lock, not a per-item one.
The "at most one item in an editing phase" invariant is a statement about the
whole crew, so the check and the write must be atomic together; a per-item lock
would let two concurrent writes each observe no other editor and both proceed.

LOCK ORDER, for the one path that holds more than one lock. A crew's progress
write spans three files, so :func:`commit_work_progress` holds locks across the
WHOLE transaction and takes the remaining ones from inside that hold:
**crew -> skip(number) -> records -> events**. Nothing anywhere takes two of
these in the other relative order, so the order is total.

The outer two are held across the whole transaction, INCLUDING its rollback,
because each of them guards a value the rollback has to still be entitled to
change:

  * the CREW lock, for the work item. The transaction restores a snapshot taken
    before the first write, and a rollback target another writer can move while
    the snapshot is held is not a rollback — it puts a stale snapshot over a value
    that committed.
  * the SKIP lock, for one issue number in the shared index. The index is
    REPO-WIDE and the crew lock is PER-CREW, so the crew lock serialises nothing
    at all between two crews passing on the same issue: the second crew's
    ``record_skip`` finds the first crew's entry, commits its own item and ledger
    line against it, and the first crew's rollback then deletes an entry the
    second one has already committed against. Per-NUMBER rather than repo-wide, so
    two crews passing on two DIFFERENT issues still run concurrently — the pair
    that has to be serialised is the pair contending for one index entry.

See :func:`commit_work_progress`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import re
import secrets
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write

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
#                         crew, enforced in `upsert_work_item`.
#
# A crew NEVER holds an issue waiting for a human. When it needs a human decision
# or a human investigation it says so on the issue, labels it, records the pass
# (`skipped`, with a scope naming which of the two it needs) and releases the
# claim — so every non-terminal phase is one the crew is itself the actor in, and
# every one of them occupies a work slot.
PHASES = (
    "selected",          # local only, pre-claim — never public
    "claimed",
    "investigating",
    "implementing",
    "awaiting-ci",
    "addressing-review",
    "awaiting-merge",
    "awaiting-reply",
    "resolved",
    "skipped",
    "yielded",
    "handed-back",
    "preempted",
)
TERMINAL_PHASES = frozenset({"resolved", "skipped", "yielded", "handed-back", "preempted"})
TTL_ACTIVE_PHASES = frozenset({"claimed", "investigating", "implementing"})
EDITING_PHASES = frozenset({"implementing", "addressing-review"})

EVENT_KINDS = (
    "claim", "investigate", "reply", "implement", "ci",
    "review", "conflict", "merge", "handback", "skip", "yield",
)

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
SKIP_SCOPES = (
    "architecture",
    "new-feature",
    "needs-design",
    "needs-decision",
    "needs-investigation",
    "duplicate",
    "already-fixed",
    "not-reproducible",
    "wrong-root-cause",
    "breaking-change",
    "gate-config",
    "other",
)
DEFAULT_SKIP_SCOPE = "other"

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
    truncates, so ``47.9`` used to store as ``47`` — a value the operator never
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


def _skip_lock_path(owner: str, repo: str, number: int, root: Path | None = None) -> Path:
    """The lock that serialises PASSES ON ONE ISSUE across every crew in the repo.

    Distinct from ``_records_lock_path`` because the two protect different things.
    That one makes a single read-modify-write of the whole ``skipped.json`` file
    atomic; this one makes one crew's OWNERSHIP of an index entry hold still for
    longer than the write that created it — from before the entry is inserted until
    after the transaction that inserted it has either committed or rolled back.

    Nothing shorter works, and the crew lock in particular does not. The index is
    repo-wide; the crew lock is per-crew. Two crews passing on the same issue hold
    two DIFFERENT crew locks, so they are not serialised against each other on the
    shared entry at all: the second one's :func:`record_skip` finds the first one's
    entry, reports no creation, and commits its own work item and ledger line
    against it — after which the first one's rollback removes the entry the second
    one has just committed against. The issue then reads as un-passed to the whole
    fleet while a crew's own item and log say it passed on it.

    Per-NUMBER, not repo-wide, and that is the whole design: only two transactions
    contending for the SAME index entry can do this to each other, so that is the
    only pair worth serialising. A repo-wide hold across the ledger append would
    park every other crew in the repo behind one transaction's slowest write for no
    additional safety.

    ``int(number)`` is the only sanitising this needs — the result is always
    ``-?\\d+``, so unlike a crew id it cannot escape the directory. Same convention
    as :func:`work_item_path` and ``store.issue_write_lock``.
    """
    return crews_dir(owner, repo, root) / f"skip-{int(number)}.lock"


@contextlib.contextmanager
def _skip_lock(owner: str, repo: str, number: int, root: Path | None = None):
    """Hold :func:`_skip_lock_path` for *number*. See it for why, and the module
    docstring for where this sits in the lock order."""
    with open(_skip_lock_path(owner, repo, number, root), "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


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
    overwrite the other's field. Work ITEMS keep the per-crew lock — different
    files, different invariant.

    The shared SKIP INDEX takes this lock too, for the same reason and not merely
    by analogy: ``skipped.json`` is one file every crew in the repo writes, so two
    crews passing on two different issues under two different per-crew locks would
    each read an index that predates the other and each write it back whole,
    dropping one of the two decisions. Losing a skip is not cosmetic — the issue
    it dropped goes back to being re-investigated by every crew.

    This lock makes ONE read-modify-write of that file atomic, and that is all it
    does. It does NOT keep a crew's entry its own for the length of a transaction —
    it is released the moment :func:`record_skip` returns, and two crews passing on
    one issue then race over who may un-index the entry. That is
    ``_skip_lock_path``'s job, and it is a different lock because it is a different
    granularity: per-issue and held far longer.

    No path nests this OUTSIDE ``_crew_lock_path`` or ``_skip_lock_path``. The one
    path that holds more than one lock, :func:`commit_work_progress`, takes the crew
    lock and then the per-issue skip lock and takes THIS one from inside both — so
    the order is **crew -> skip(number) -> records** everywhere, and it is total.
    Two crews passing at once both wait for this lock while holding locks the other
    does not want in a conflicting order, so neither waits on a lock the other
    holds. See the module docstring for the full order.
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
            stored = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
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
    with open(lock_path, "w") as fd:
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
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
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
        rec = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
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
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            if name in taken_names(owner, repo, root):
                raise CrewStoreError(f"crew name {name!r} is already taken in this repo")
            crew_id = f"c_{secrets.token_hex(4)}"
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
    with open(lock_path, "w") as fd:
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
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            record = read_crew(owner, repo, crew_id, root) or record
            record["retired_at"] = store._now_iso()
            atomic_write(crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2))
    return _coerce_crew(record)


# ── work items ──────────────────────────────────────────────────────────────


def read_work_item(
    owner: str, repo: str, crew_id: str, number: int, root: Path | None = None
) -> dict[str, Any] | None:
    path = work_item_path(owner, repo, crew_id, number, root)
    if not path.is_file():
        return None
    try:
        rec = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


def list_work_items(
    owner: str, repo: str, crew_id: str, root: Path | None = None, *, open_only: bool = False
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = crews_dir(owner, repo, root) / crew_id
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        if open_only and rec.get("phase") in TERMINAL_PHASES:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("last_progress_at") or "", reverse=True)
    return out


def open_slot_count(
    owner: str, repo: str, crew_id: str, root: Path | None = None
) -> int:
    """Work items occupying a slot: every unfinished one.

    No exemption, because there is no longer a phase in which the crew is not the
    actor: an item it cannot progress without a human is recorded as a pass and its
    claim released, so anything still open is work this crew owes.
    """
    return len(list_work_items(owner, repo, crew_id, root, open_only=True))


def serialize_work_item(record: dict[str, Any]) -> str:
    """The exact text a work item occupies on disk.

    The ONE serialisation of this file, so a record and the bytes standing for it
    cannot drift: :func:`_upsert_work_item_locked` stores what this returns, and the
    store's own test pins the two against each other.

    Byte-for-byte, on every platform: the write passes ``newline=""``, so the file
    holds this string literally. With the default translation it would hold
    ``\\r\\n`` on Windows and no reader could match it.
    """
    return json.dumps(record, indent=2)


def upsert_work_item(
    owner: str,
    repo: str,
    crew_id: str,
    number: int,
    patch: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """Merge *patch* into one work item, per field, and return the stored record.

    Takes the crew-level lock (see the module docstring for why it is not per-item)
    and delegates to :func:`_upsert_work_item_locked`. A caller that already holds
    that lock — :func:`commit_work_progress` — must call the locked form directly:
    the lock is a file lock taken on a fresh descriptor, so re-entering it from the
    same thread blocks on itself forever rather than nesting.
    """
    lock_path = _crew_lock_path(owner, repo, crew_id, root)
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            return _upsert_work_item_locked(owner, repo, crew_id, number, patch, root)


def _upsert_work_item_locked(
    owner: str,
    repo: str,
    crew_id: str,
    number: int,
    patch: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """:func:`upsert_work_item`'s body. THE CALLER MUST HOLD THE CREW LOCK.

    ``claimed_at`` is stamped once. ``last_progress_at`` moves ONLY when the patch
    carries real progress — a phase change, a new ``next``, a PR number, a CI
    reading, or an appended ``tried`` entry. A bare read-back must not renew a
    claim, because the TTL is measured from this field.

    Refuses a second item entering an editing phase. That check reads the crew's
    OTHER items, which is why the lock it needs is the crew's and not the item's.
    """
    number = int(number)
    now = store._now_iso()
    existing = read_work_item(owner, repo, crew_id, number, root) or {}
    phase = existing.get("phase") if existing.get("phase") in PHASES else "selected"

    if "phase" in patch:
        new_phase = str(patch["phase"] or "").strip()
        if new_phase not in PHASES:
            raise CrewStoreError(f"unknown phase {new_phase!r}")
        if new_phase in EDITING_PHASES and phase not in EDITING_PHASES:
            other = _editing_item(owner, repo, crew_id, root, exclude=number)
            if other is not None:
                raise CrewStoreError(
                    f"crew {crew_id} is already editing #{other} — finish or "
                    "commit that before entering an editing phase on another issue"
                )
        phase = new_phase

    record: dict[str, Any] = {
        "schema": CREW_SCHEMA,
        "crew_id": crew_id,
        "owner": owner,
        "repo": repo,
        "number": number,
        "phase": phase,
        "outcome": existing.get("outcome"),
        "decision": existing.get("decision", ""),
        "why": existing.get("why", ""),
        "next": existing.get("next", ""),
        "tried": list(existing.get("tried") or []),
        "worktree": existing.get("worktree", ""),
        "branch": existing.get("branch", ""),
        "base_sha": existing.get("base_sha", ""),
        # Carried forward through `_finite_int` rather than copied, so a value that
        # was hand-edited into the file cannot be re-serialised by this write: once
        # `Infinity` is written back the record stops being JSON any strict parser
        # will read, and the crew's own page is served from it.
        "pr_number": _finite_int(existing.get("pr_number")),
        "ci_state": existing.get("ci_state") or {},
        "claim_comment_id": _finite_int(existing.get("claim_comment_id")),
        "labels_applied": list(existing.get("labels_applied") or []),
        "claimed_at": existing.get("claimed_at") or (
            now if phase not in ("selected",) else None
        ),
        "last_progress_at": existing.get("last_progress_at") or now,
        "finished_at": existing.get("finished_at"),
    }

    progressed = "phase" in patch and patch["phase"] != existing.get("phase")

    for key in ("decision", "why", "next", "worktree", "branch", "base_sha"):
        if key in patch and isinstance(patch[key], str):
            record[key] = patch[key]
            if key == "next" and patch[key] != existing.get("next"):
                progressed = True
    if "pr_number" in patch:
        record["pr_number"] = _finite_int(patch["pr_number"])
        progressed = True
    if "claim_comment_id" in patch:
        record["claim_comment_id"] = _finite_int(patch["claim_comment_id"])
    if "ci_state" in patch and isinstance(patch["ci_state"], dict):
        record["ci_state"] = {**record["ci_state"], **patch["ci_state"]}
        progressed = True
    if "labels_applied" in patch and isinstance(patch["labels_applied"], list):
        record["labels_applied"] = [
            str(x) for x in patch["labels_applied"] if isinstance(x, str)
        ]
    if "outcome" in patch and isinstance(patch["outcome"], str):
        record["outcome"] = patch["outcome"].strip() or None
    tried = patch.get("tried_approach")
    if isinstance(tried, str) and tried.strip():
        record["tried"].append(
            {
                "approach": tried.strip(),
                "rejected_because": str(patch.get("tried_rejected_because") or ""),
                "at": now,
            }
        )
        progressed = True

    if progressed:
        record["last_progress_at"] = now
    if phase in TERMINAL_PHASES:
        # Stamp once per terminal ARRIVAL, not once per item's lifetime.
        if not record["finished_at"]:
            record["finished_at"] = now
    else:
        # Reopened: a resolved issue can come back and be handled again by the
        # same crew, which reuses this very item. EVERY field that describes a
        # finished result has to be dropped here, or the item reports a terminal
        # outcome while it is demonstrably being worked again.
        #
        # These two are the whole terminal set, and they are cleared together on
        # purpose — clearing one and not the other is exactly how this bug got
        # reported twice. The other carried-forward fields are deliberately NOT
        # cleared: `decision`/`why`/`next`/`tried` are the crew's memory of what
        # it already ruled out (losing them makes it repeat rejected approaches),
        # and `worktree`/`branch`/`base_sha`/`pr_number`/`ci_state`/
        # `claim_comment_id`/`labels_applied` describe where the work lives and
        # what is on the forge right now, which a reopen does not invalidate.
        #
        # `finished_at`: a stale stamp put the second resolution outside the
        # `resolved24h` window, so the crew's page under-reported its own work.
        # `outcome`: a stale outcome made the ledger assert a terminal result on
        # active work — a reader cannot tell that from a genuinely finished item.
        record["finished_at"] = None
        record["outcome"] = None

    atomic_write(
        work_item_path(owner, repo, crew_id, number, root),
        serialize_work_item(record),
        newline="",
    )
    return record


def _editing_item(
    owner: str, repo: str, crew_id: str, root: Path | None = None, *, exclude: int | None = None
) -> int | None:
    """The issue number this crew is currently editing, if any."""
    for it in list_work_items(owner, repo, crew_id, root, open_only=True):
        if it.get("phase") in EDITING_PHASES and it.get("number") != exclude:
            num = it.get("number")
            if isinstance(num, int):
                return num
    return None


# ── event ledger ────────────────────────────────────────────────────────────


def _event_id(ts: str, crew_id: str, number: int, kind: str, text: str) -> str:
    raw = f"{ts}|{crew_id}|{number}|{kind}|{text}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def append_event(
    owner: str,
    repo: str,
    crew_id: str,
    number: int,
    kind: str,
    text: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Append one progress line.

    The id is content-addressed so a duplicated line merges on read rather than
    conflicting — the same discipline as ops-mission-control's ledger, whose own
    docstring records that it shipped without a lock and was caught in review.

    ``text`` BECOMES PUBLIC: it is rendered both on the crew page and inside the
    ``<details>`` block of the claim comment on the forge. Callers must keep
    absolute paths, host names and anything else environment-specific out of it;
    worktree paths belong in the work item's own fields.
    """
    if kind not in EVENT_KINDS:
        raise CrewStoreError(f"unknown event kind {kind!r}")
    ts = store._now_iso()
    entry = {
        "id": _event_id(ts, crew_id, int(number), kind, text),
        "ts": ts,
        "crew_id": crew_id,
        "number": int(number),
        "kind": kind,
        "text": text,
    }
    path = events_path(owner, repo, root)
    lock_path = crews_dir(owner, repo, root) / "events.lock"
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            with open(path, "a", encoding="utf-8") as out:
                out.write(json.dumps(entry) + "\n")
    return entry


def read_events(
    owner: str,
    repo: str,
    root: Path | None = None,
    *,
    crew_id: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Newest first, duplicate ids collapsed. A malformed line is skipped rather
    than failing the whole read — the ledger is append-only and a torn tail must
    not hide the history in front of it."""
    path = events_path(owner, repo, root)
    if not path.is_file():
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or "")
        if rid and rid in seen:
            continue
        if crew_id and rec.get("crew_id") != crew_id:
            continue
        seen.add(rid)
        out.append(rec)
        if len(out) >= limit:
            break
    return out


# ── shared skip index ───────────────────────────────────────────────────────
#
# One file per repo, keyed by issue number as a STRING because that is what JSON
# object keys are: round-tripping ints through `json.dump` would turn 42 into
# "42" on write and leave the reader guessing which form it holds. Callers pass
# ints and this module does the conversion at both ends.


def coerce_skip_scope(scope: Any) -> str:
    """*scope* if it is a known one, else ``other``.

    Coerced rather than refused, deliberately. The alternative — raising on an
    unknown scope — makes an imperfect label cost the entire skip record, and a
    skip that fails to record is exactly the waste this index exists to remove.
    The scope is a filter label; the ``reason`` is the substance, and it is free
    text precisely so nothing forces a decision into the wrong bucket.
    """
    text = str(scope or "").strip().lower()
    return text if text in SKIP_SCOPES else DEFAULT_SKIP_SCOPE


def read_skips(owner: str, repo: str, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """The whole index, keyed by ``str(number)``.

    A malformed or missing file reads as empty rather than raising: this is
    consulted on the path where a crew decides whether to investigate, and a torn
    file must degrade into "nothing is known to be skipped" (one wasted
    investigation) rather than into a crash that stops the crew.
    """
    path = skips_path(owner, repo, root)
    if not path.is_file():
        return {}
    try:
        stored = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(stored, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, val in stored.items():
        if not isinstance(val, dict):
            continue
        number = val.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            # Tolerate an entry whose `number` was written as a string by an older
            # writer: the KEY is authoritative, and an entry that cannot be read
            # back as an int would silently drop out of `skipped_numbers`.
            try:
                number = int(str(key))
            except (TypeError, ValueError):
                continue
        out[str(number)] = {
            "number": number,
            "reason": str(val.get("reason") or ""),
            "scope": coerce_skip_scope(val.get("scope")),
            "crew_id": str(val.get("crew_id") or ""),
            "decided_at": str(val.get("decided_at") or ""),
        }
    return out


def is_skipped(owner: str, repo: str, number: int, root: Path | None = None) -> bool:
    """Whether any crew in this repo has already passed on *number*."""
    return str(int(number)) in read_skips(owner, repo, root)


def record_skip(
    owner: str,
    repo: str,
    number: int,
    reason: str,
    scope: str,
    crew_id: str,
    root: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Index a pass on *number*: the entry that now STANDS, and whether THIS call
    is the one that created it.

    Idempotent by keeping the FIRST decision. A re-skip is not an error and does
    not overwrite: the first crew's reason is the audit trail, and it is the one a
    human reads when asking why this issue keeps being passed over. A later crew
    that reaches the same conclusion adds no information; one that reaches a
    DIFFERENT conclusion is a disagreement to surface on its own work item, not a
    silent edit of someone else's record. So the first element is what is stored
    after the call, which for a re-skip is the earlier crew's entry — the caller can
    compare ``crew_id`` to see that its own reason was not the one kept.

    THE SECOND ELEMENT IS NOT DERIVABLE BY THE CALLER, which is why it is returned
    rather than left to be inferred. Only this function, holding the repo-wide lock
    across the read and the write, can say whether the entry standing afterwards is
    the one it just added. A caller comparing a pre-read of the index, or matching
    the stored entry's fields against the ones it supplied, cannot separate two
    IDENTICAL concurrent passes: both pre-read an empty index and both recognise
    the winner's entry as their own, so the loser un-indexes a decision that
    committed — putting the issue back in front of every crew in the fleet. Anything
    that compensates a failed write needs this flag to be the truth, so it is part
    of the single return contract rather than an opt-in a later caller could go
    around.

    Takes the repo-wide record lock, not the calling crew's: see
    ``_records_lock_path``. Every crew writes this one file whole, so a per-crew
    lock would let two of them drop each other's decisions.

    THE FLAG IS TRUE AT THE MOMENT OF THE WRITE AND NO LONGER. It says this call
    inserted the entry; it cannot say the entry is still this caller's to remove,
    because this function's lock is released before it returns. A caller that will
    later COMPENSATE the write — un-index the entry if a subsequent write of its own
    fails — must hold ``_skip_lock_path`` for *number* across both, or a second crew
    slips in between, adopts this entry and commits against it, and the
    compensation deletes a decision that stood. :func:`commit_work_progress` is
    that caller and holds it.
    """
    number = int(number)
    key = str(number)
    entry: dict[str, Any] = {
        "number": number,
        "reason": str(reason or ""),
        "scope": coerce_skip_scope(scope),
        "crew_id": str(crew_id or ""),
        "decided_at": store._now_iso(),
    }
    lock_path = _records_lock_path(owner, repo, root)
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            index = read_skips(owner, repo, root)
            existing = index.get(key)
            if existing is not None:
                return existing, False
            index[key] = entry
            atomic_write(skips_path(owner, repo, root), json.dumps(index, indent=2))
    return entry, True


def unrecord_skip(
    owner: str, repo: str, number: int, entry: dict[str, Any], root: Path | None = None
) -> None:
    """Remove *entry* from the repo's shared skip index — and only *entry*.

    Compared before deleting, never deleted by key. The index is repo-wide and
    :func:`record_skip` keeps the first decision, so the entry standing under this
    number may belong to another crew; deleting by key would let one crew's failed
    request erase another crew's recorded pass, which sends every crew in the fleet
    back to re-investigating an issue somebody already decided about.

    Takes the same repo-wide record lock :func:`record_skip` takes, for the reason
    that function's docstring gives: every crew writes this one file whole, so an
    unlocked read-modify-write here would drop a skip recorded in between.

    THE COMPARISON IS AN OWNERSHIP CHECK, NOT A STALENESS CHECK, and it is not what
    makes a compensating caller safe. *entry* is the value the caller itself wrote,
    so equality means "still the entry I inserted" — but a second crew that ADOPTED
    that entry rather than writing its own leaves it byte-identical, so equality
    also holds in exactly the interleaving where deleting is wrong. What excludes
    that interleaving is the caller holding ``_skip_lock_path`` for *number* from
    before its insert until after this call, which is why
    :func:`_rollback_work_progress` documents that hold as a precondition rather
    than relying on the comparison below.
    """
    key = str(int(number))
    lock_path = _records_lock_path(owner, repo, root)
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            index = read_skips(owner, repo, root)
            if index.get(key) != entry:
                return
            del index[key]
            atomic_write(skips_path(owner, repo, root), json.dumps(index, indent=2))


def recent_skips(
    owner: str, repo: str, root: Path | None = None, *, limit: int = 20
) -> list[dict[str, Any]]:
    """The newest *limit* entries, newest first.

    Ordered by ``decided_at`` with the issue number as the tie-break, so the order
    is total: several skips inside one clock tick would otherwise come back in
    whatever order the JSON object happened to hold, and a list that reshuffles
    between two reads of the same data reads as churn on the crew page.
    """
    rows = sorted(
        read_skips(owner, repo, root).values(),
        key=lambda r: (str(r.get("decided_at") or ""), int(r.get("number") or 0)),
        reverse=True,
    )
    return rows[: max(0, limit)]


# ── one progress write, all or nothing ──────────────────────────────────────
#
# A crew's progress write touches THREE files — its work item, the repo-wide skip
# index, and the append-only ledger — and no ordering makes three files atomic:
# whichever write goes last can fail with the earlier ones committed. So the
# transaction compensates, and it holds a lock on each thing it may have to
# compensate for the WHOLE of that span.
#
# Why held across the span and not just across each write. Compensation restores a
# value the transaction observed before it wrote, so that observation has to still
# be true when the rollback uses it. Taking and releasing a lock per write leaves
# gaps on both sides, and a writer that commits in either gap invalidates the
# observation:
#
#   * for the WORK ITEM the observation is a text snapshot, and a writer in the gap
#     makes it stale — the rollback then puts a value that PREDATES that writer over
#     a value that committed, which is a lost update rather than a rollback.
#   * for the SKIP INDEX the observation is `record_skip`'s `created` flag, and a
#     writer in the gap makes it obsolete rather than stale: the entry is still
#     exactly the one this transaction inserted, but a second crew has since ADOPTED
#     it — found it standing, reported no creation of its own, and committed its item
#     and its ledger line against it. Un-indexing then erases a decision the fleet
#     is already relying on.
#
# No comparison closes either gap, which is why no compare-and-set appears below. A
# comparison can only prove the file still holds what THIS transaction wrote, and in
# both interleavings that is precisely what it does hold: in the first because this
# transaction wrote last, in the second because the adopter wrote nothing.
#
# Reordering was considered and rejected. Appending the ledger line first would
# trade a missing line for a FALSE one, and the ledger is append-only and
# content-addressed: there is no retraction, so a line asserting a phase change the
# store then refuses (the second-editing-item refusal is routine, not only an I/O
# fault) stays in the crew's memory for good, and the retry appends a second line
# contradicting the first. A missing line is recoverable; a lie in the log is not.
# Indexing the skip first is worse again — see `record_skip`.


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
) -> dict[str, Any]:
    """Write one work item, optionally index a pass, append one ledger line —
    or leave all three as they were. Returns ``{"item", "event", "skip"}``.

    *skip_reason* is what makes this index a pass: ``None`` means "no skip", and any
    string (the caller derives it from the request) records one. The reason is a
    caller's to choose, but the COUPLING is not: an issue cannot be recorded as
    passed without the same call storing the item and the line that explain it.

    ORDER: item -> skip index -> ledger line. The item first because the store's own
    refusals (an unknown phase, a second editing item) then happen before anything
    else is written. The index after it because indexing first could mark an issue
    passed repo-wide that the store then refused to move — an issue permanently
    filtered out of every crew's queue with no decision behind it. The ledger last
    because a line is the one write with no retraction.

    LOCKS, in this order: the crew's, then — only when this call records a pass —
    the shared index's lock for THIS ISSUE NUMBER, then the repo-wide record lock
    for the index file, then the ledger's. The first two are held across
    everything, including the rollback; the last two are taken and released inside
    that hold, by :func:`record_skip` and :func:`append_event`.

    The per-number skip lock is the one that cannot be dropped, and the crew lock
    cannot stand in for it: the index is repo-wide and the crew lock is per-crew, so
    two crews passing on the same issue hold two different crew locks and are
    serialised on the shared entry by nothing at all. See ``_skip_lock_path``. It is
    acquired only on the skip path because a transaction that indexes nothing has
    nothing there to compensate, and skipping the acquisition can never invert an
    order.

    Nothing anywhere takes any two of these four in the other relative order, so
    the order is total and two crews cannot deadlock. A crew lock is only ever
    acquired FIRST, so nobody waits for one while holding a skip, records or events
    lock; the skip lock for one number is the only skip lock a frame ever holds;
    and records and events are each taken and released without acquiring anything
    else. The work-item write calls :func:`_upsert_work_item_locked` rather than
    :func:`upsert_work_item` because that one would take the crew lock again — a
    file lock on a second descriptor, which blocks on the lock this frame already
    holds instead of nesting.
    """
    number = int(number)
    lock_path = _crew_lock_path(owner, repo, crew_id, root)
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            # Under the lock that also guards the write, so no writer can land
            # between the two and leave this holding a value that is already stale.
            before = _read_work_item_text(owner, repo, crew_id, number, root)
            item = _upsert_work_item_locked(owner, repo, crew_id, number, patch, root)
            skip: dict[str, Any] | None = None
            own_skip: dict[str, Any] | None = None
            with contextlib.ExitStack() as held:
                try:
                    if skip_reason is not None:
                        # Acquired INSIDE the try so that failing to acquire is
                        # rolled back like any other step. The item is already
                        # written by this point, so an exception here — the lock
                        # file is one more open descriptor, and fd exhaustion or a
                        # permission fault raises — used to escape past the rollback
                        # and leave the item changed with neither its skip-index
                        # entry nor its ledger event: the one outcome this
                        # transaction exists to prevent.
                        #
                        # Release is unaffected: the ExitStack encloses this try, so
                        # the lock is still dropped only after the rollback has run,
                        # which is what keeps `created` below describing the entry
                        # the rollback acts on. Another crew passing on this same
                        # issue still waits here and cannot commit against an entry
                        # this transaction may withdraw.
                        held.enter_context(_skip_lock(owner, repo, number, root))
                        # `created` comes from inside the index's own lock because
                        # that is the only place it is knowable: two identical
                        # passes on one number both see it unindexed beforehand and
                        # both recognise the winner's entry as their own, so
                        # anything computed out here would let the loser un-index a
                        # decision that committed.
                        skip, created = record_skip(
                            owner, repo, number, skip_reason, skip_scope, crew_id, root
                        )
                        if created:
                            own_skip = skip
                    event = append_event(
                        owner, repo, crew_id, number, event_kind, event_text, root
                    )
                except BaseException:
                    _rollback_work_progress(
                        owner, repo, crew_id, number, before, own_skip, root
                    )
                    raise
                return {"item": item, "event": event, "skip": skip}


def _read_work_item_text(
    owner: str, repo: str, crew_id: str, number: int, root: Path | None = None
) -> str | None:
    """The work item's stored text as it stands, or ``None`` if there is no item.

    Text rather than the parsed record: a rollback has to reproduce the FILE, and a
    dict round-trip is a re-serialisation that can legitimately differ from what was
    on disk. ``newline=""`` here and on the write back is what makes that literal —
    the default translates line endings, so a snapshot taken and restored on Windows
    would come back with different bytes than it started with.

    Only a MISSING file reads as ``None``. Anything else — a permission fault, bytes
    that are not UTF-8 — propagates, and it propagates from BEFORE the first
    mutation, so such a call fails having written nothing rather than mutating with a
    snapshot it could not roll back to.
    """
    path = work_item_path(owner, repo, crew_id, number, root)
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _restore_work_item_locked(
    owner: str,
    repo: str,
    crew_id: str,
    number: int,
    snapshot: str | None,
    root: Path | None = None,
) -> None:
    """Put the work item back exactly as *snapshot* found it. HOLD THE CREW LOCK.

    ``None`` means there was no item, so the compensation is a DELETE rather than a
    write: the upsert CREATES as well as updates, and an item the failed transaction
    brought into existence has no earlier value to return to. Leaving a stub would
    count toward the crew's open slots and against its one-editing-item limit for an
    issue it never took.

    Unconditional, and that is only safe because the caller has held the crew lock
    since before the snapshot was taken: no other writer can have touched the file,
    so the snapshot still describes it and there is nothing for a comparison to
    detect.
    """
    path = work_item_path(owner, repo, crew_id, number, root)
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    atomic_write(path, snapshot, newline="")


def _rollback_work_progress(
    owner: str,
    repo: str,
    crew_id: str,
    number: int,
    before: str | None,
    own_skip: dict[str, Any] | None,
    root: Path | None = None,
) -> None:
    """Undo a failed transaction's committed writes, newest first. HOLD THE CREW
    LOCK, AND — whenever *own_skip* can be non-``None`` — THE SKIP LOCK FOR
    *number*, both since before the writes being undone.

    What this buys: the work item moves only if the progress line explaining the
    move landed, and an issue enters the shared skip index only if the same is true.
    Without it a failed write leaves the crew's state disagreeing with the crew's
    memory — and in the skipped case leaves an issue passed over repo-wide, filtered
    out by every other crew, with nothing in the log saying who passed on it or why.
    The crew's retry cannot tell which of the writes stood.

    Never raises, and each step is guarded separately: the caller already has an
    error and that error is the one it must surface, while a failure to undo one file
    must not skip the other. A step that does fail is logged at error, because the
    log is then the only record that the two disagree.

    Only ``own_skip`` — the entry THIS transaction created — is un-indexed. A
    re-skip found somebody else's decision standing and has nothing to undo. That
    test is necessary but not sufficient on its own: an entry this transaction
    created is one a SECOND crew may since have adopted and committed against, and
    the adopter leaves it byte-identical, so nothing readable here can tell the two
    apart. The caller's skip-lock hold is what excludes the adopter — it makes the
    other crew wait until this rollback has finished, after which it records a pass
    of its own and owns it. See ``_skip_lock_path``.
    """
    if own_skip is not None:
        try:
            unrecord_skip(owner, repo, number, own_skip, root)
        except Exception:
            logger.error(
                "crew %s: could not un-index the skip on #%s after a failed work "
                "write — the issue reads as skipped repo-wide with no event for it",
                crew_id, number, exc_info=True,
            )
    try:
        _restore_work_item_locked(owner, repo, crew_id, number, before, root)
    except Exception:
        logger.error(
            "crew %s: could not restore work item #%s after a failed work write — "
            "its phase may have moved with no event explaining it",
            crew_id, number, exc_info=True,
        )
