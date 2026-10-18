"""A crew's record and settings, and where its files live.

The record schema with its phases, event kinds and skip scopes, the guards on every
decoded number, the crew-id rules and the paths below the app's data root, the
repository's crew settings, and the crew records themselves: list, read, create with
a minted id, update, model hold, pause and retire. Callers reach every name here
through :mod:`kiro_crew.apps.builtins.issue_radar.backend.crew_store`.
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.crew_log.entry_types import (
    RADAR_CLEARABLE_FIELDS,
    RADAR_CREW_LEVEL_EVENT_KIND,
    RADAR_DEFAULT_SKIP_SCOPE,
    RADAR_EDITING_PHASES,
    RADAR_EVENT_KINDS,
    RADAR_PHASES,
    RADAR_SKIP_SCOPES,
    RADAR_TERMINAL_PHASES,
    RADAR_TTL_ACTIVE_PHASES,
)
from kiro_crew.validation import MODEL_PICKER_HIDDEN_MODEL_ID_RE

from . import store

logger = logging.getLogger("kiro_crew.apps.builtins.issue_radar.backend.crew_store")

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
    "Andromeda",
    "Bode",
    "Butterfly",
    "Carina",
    "Cigar",
    "Cocoon",
    "Draco",
    "Fireworks",
    "Fornax",
    "Grus",
    "Hoag",
    "Leo",
    "Mayall",
    "Medusa",
    "Pinwheel",
    "Porpoise",
    "Sculptor",
    "Sombrero",
    "Spindle",
    "Tadpole",
    "Triangulum",
    "Tucana",
    "Ursa",
    "Whirlpool",
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
    # The model for turns that only check the queue (see ``crew_model``). Empty
    # means the crew runs every turn on ``model``.
    "triage_model": "",
    # Written by the runtime when a person picked the model of the crew's session
    # by hand, so the crew stops switching it. A patch may only clear it ("Resume
    # switching"); an edit that changes ``model`` or ``triage_model`` clears it too
    # (see :func:`update_crew`).
    "model_held": False,
    "extra_prompt": "",
    "worktree_root": "",
    "enabled": True,
    "paused_reason": "",
}


class CrewStoreError(Exception):
    """A store invariant was violated — a duplicate name, an unknown crew, or a
    second work item trying to enter an editing phase."""


CrewStoreError.__module__ = "kiro_crew.apps.builtins.issue_radar.backend.crew_store"


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
    (``crew_store``'s module docstring), so the read has to answer with something usable.
    """
    number = _finite_int(value)
    if number is None or not (MIN_MAX_OPEN <= number <= MAX_MAX_OPEN):
        return None
    return number


def _validated_model_id(value: Any) -> str | None:
    """A crew's triage model id, ``""`` for unset, or ``None`` for "not a model id".

    The shape rule is the dashboard's own model-id grammar
    (``MODEL_PICKER_HIDDEN_MODEL_ID_RE``), so an id the model picker would refuse
    is refused here too. Whether the account can RUN the id is a question only a
    live session answers; ``crew_model`` asks it and falls back when it cannot.
    """
    if not isinstance(value, str):
        return None
    model = value.strip()
    if not model:
        return ""
    return model if MODEL_PICKER_HIDDEN_MODEL_ID_RE.fullmatch(model) else None


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


# ── paths ───────────────────────────────────────────────────────────────────


def _crews_path(owner: str, repo: str, root: Path | None = None) -> Path:
    """Where :func:`crews_dir` lives, without creating or probing any component of it.

    :func:`store.repo_data_dir`'s layout without its ``mkdir``. A caller that must hold
    every component of a chain open before anything resolves its name needs the path
    first and holds what is already there: ``mkdir(parents=True)`` resolves a missing tail
    by name, and where a component may be a junction aimed at a remote share, resolving
    one is itself an outbound authentication as this process. ``crew_store``'s tests assert
    the result equals :func:`store.repo_data_dir`'s location, which is the layout restated
    here, so the two cannot drift apart.

    The claim covers the chain this function names -- ``repos/<owner>/<repo>/crews`` --
    and not the data root it hangs from. With no *root* the base comes from
    :func:`store.app_data_dir`, which creates the app's own data directory exactly as
    every other call in this app does; that directory is the store itself, so a hold
    below it cannot be the thing that brings it into being.
    """
    base = root if root is not None else store.app_data_dir(store.APP_NAME)
    return base / "repos" / owner / repo / "crews"


def crews_dir(owner: str, repo: str, root: Path | None = None) -> Path:
    d = _crews_path(owner, repo, root)
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

    Crew records are the only files ``crew_store`` still writes: the work items, the
    progress lines and the skip index are appends to the crew log and take no lock
    here (see ``crew_store_ledger``).
    """
    return crews_dir(owner, repo, root) / "_create.lock"


def slot_key_for(crew_id: str) -> str:
    """The slot a crew runs on -- the key its crew log units are headed with."""
    return f"crew-{_require_crew_id(crew_id)}"


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
        # records, and a `name == "settings.json"` check would parse the first
        # recorded skip as a crew: it has no `id`, so the watchdog would
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
    # Same read-side discipline for the triage model: a hand-edited id the write
    # path would have refused reads as unset, so the crew runs on its work model.
    triage = _validated_model_id(out.get("triage_model"))
    out["triage_model"] = triage if triage is not None else ""
    out["model_held"] = out.get("model_held") is True
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
    return {str(c.get("name") or "") for c in list_crews(owner, repo, root, include_retired=True)}


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
            atomic_write(crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2))
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
    if "triage_model" in patch:
        triage = _validated_model_id(patch["triage_model"])
        if triage is not None:
            out["triage_model"] = triage
    if patch.get("model_held") is False:
        # "Switch back to triage model" on the crew page. A patch may RELEASE a
        # hand-picked model hold but never set one: only the runtime, seeing the
        # pick, does.
        out["model_held"] = False
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
            settings_before = model_settings(record)
            validated = _validated_crew_patch(patch)
            record.update(validated)
            released = validated.get("model_held") is False
            if model_settings(record) != settings_before:
                # Changing which models the crew runs on hands its session's model
                # back to the crew: a person's hand pick held it until now.
                record["model_held"] = False
                released = True
            record["schema"] = CREW_SCHEMA
            atomic_write(crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2))
            if released:
                # After the write, under the same lock the hold write checks it in.
                _hold_releases[crew_id] = _hold_releases.get(crew_id, 0) + 1
    return _coerce_crew(record)


#: crew id -> how many times this process has released a hand-picked model hold on
#: the crew's record (:func:`update_crew`: an explicit ``model_held: false`` or a
#: change to either model). A hold write carries the count it was SEEN under and is
#: refused once a release has moved it (:func:`set_crew_model_held`), so a write
#: queued before a release cannot land after it and undo it. Process memory covers
#: every case it orders: the only hold writer is this process's runtime, and a
#: queued write does not outlive the process, so a restart has none to refuse. Crew
#: ids are unique across the data home (:func:`_mint_crew_id`), so the id is the key.
_hold_releases: dict[str, int] = {}


def hold_releases(crew_id: str) -> int:
    """How many hold releases :data:`_hold_releases` has counted for *crew_id*."""
    return _hold_releases.get(crew_id, 0)


def model_settings(crew: Mapping[str, Any]) -> tuple[str, str]:
    """The two settings a hand-picked model hold is released by changing.

    Read the way ``crew_model.work_model`` and ``crew_model.triage_model`` read
    them, so the runtime's compare-and-set below names the same pair.
    """
    model, triage = crew.get("model"), crew.get("triage_model")
    return (
        model if isinstance(model, str) else "",
        triage.strip() if isinstance(triage, str) else "",
    )


def set_crew_model_held(
    owner: str,
    repo: str,
    crew_id: str,
    *,
    model: str,
    triage_model: str,
    seen_releases: int,
    root: Path | None = None,
) -> bool:
    """Record that a person picked the crew session's model by hand. Returns whether it wrote.

    COMPARE-AND-SET on what the hold was seen under: the two model settings, and
    the crew's release count (:func:`hold_releases`) at that moment. Any release
    since -- an edit that changed either model, or an explicit ``model_held:
    false`` from "Switch back to triage model" -- moved one of them, and a hold seen
    before it must not come back after it, however long the write sat queued. Not
    a patch field, so a request body cannot set it.
    """
    lock_path = _records_lock_path(owner, repo, root)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            if hold_releases(crew_id) != seen_releases:
                return False
            record = read_crew(owner, repo, crew_id, root)
            if record is None or model_settings(record) != (model, triage_model):
                return False
            if record.get("model_held") is True:
                return False
            record["model_held"] = True
            record["schema"] = CREW_SCHEMA
            atomic_write(crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2))
    return True


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


def retire_crew(owner: str, repo: str, crew_id: str, root: Path | None = None) -> dict[str, Any]:
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
