"""GitHub structured-source connector.

GitHub's issues, pull requests, commits and check-runs are not prose: each is a
record with a stable identity and typed fields. This connector turns those
records into the knowledge Library's existing row shape so they are searchable
and refreshable like any other source, WITHOUT a second knowledge base, registry
or sync engine — it is a :class:`BaseConnector`, driven by the same
``SyncScheduler`` as ``local_folder``.

What it owns, and what it borrows:

* **Owns** the domain model — the typed rows below, the primary keys that make a
  refresh idempotent, the conversion from a GitHub API payload into a row, the
  ``since`` + primary-key-diff that makes a refresh incremental, and the
  resumable checkpoint that lets a refresh interrupted mid-run continue instead
  of restarting.
* **Borrows** everything else. The checkpoint lives in the source's existing
  ``properties`` blob (the same per-source state the scheduler already reads for
  ``last_synced`` / ``consecutive_failures``); no new table is added. Ingestion,
  de-duplication, storage and per-row lineage recording stay in the pipeline the
  scheduler already calls.

**Live fetch is not wired here.** Reaching GitHub needs the real transport
executor from the connections stack, which is not on ``main`` yet, and
multi-page extraction consumes ``connections.vendors.github`` pagination once
that lands rather than growing a second copy of paging here. Until then
:meth:`fetch` refuses rather than returning a partial or mocked dataset: a mock
is not a live read and must never be presented as one. Everything else — the
typed rows, the keys, the conversion, the incremental diff, the checkpoint and
the per-row lineage — is exercised on ``main`` today by the pure functions this
module exposes and their tests.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from .base import BaseConnector

# ── entity types ───────────────────────────────────────────────────────────
# One string per GitHub record kind. Stored verbatim on every row's lineage so a
# search hit can say which kind of GitHub object it came from.
ENTITY_ISSUE = "github_issue"
ENTITY_PULL_REQUEST = "github_pull_request"
ENTITY_COMMIT = "github_commit"
ENTITY_CHECK_RUN = "github_check_run"

SOURCE_TYPE = "github"

# The default GitHub instance host. A GitHub Enterprise Server deployment is a
# DIFFERENT instance, so the same ``owner/repo/123`` on two hosts is two
# distinct records and must key apart. Callers pass their own instance; this is
# the public-github fallback.
DEFAULT_INSTANCE = "github.com"

# Primary-key segment separator. A vertical bar cannot occur in a GitHub
# instance host, repo full name, entity kind or numeric/sha entity key, so it
# joins the domain segments without ambiguity. Every segment is checked for it
# at construction time so a value carrying the separator can never forge a key.
_PK_SEP = "|"

# ``owner/name`` — the shape every GitHub repository full name takes. Anchored so
# a stray path segment or a trailing slash is rejected rather than silently
# accepted as a repo.
_REPO_FULL_NAME_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


class LineageError(ValueError):
    """A row was built without complete provenance.

    Raised by :meth:`RowLineage.validate`. Provenance is not optional: a row a
    search can surface without being able to say where it came from is worse
    than an absent row, so the conversion fails loudly rather than storing one.
    """


@dataclass(frozen=True)
class RowLineage:
    """Per-row provenance and the full primary-key domain for every GitHub row.

    Every typed row carries exactly one of these. It answers, for one stored
    row: which knowledge SOURCE it belongs to, which GitHub INSTANCE (host) it
    came from, which REPOSITORY within that instance, what KIND of object it is,
    the primary key that identifies it, the GitHub API URL it was read from, and
    when it was read.

    ``source_id`` + ``instance`` + ``repo_full_name`` + ``entity_type`` are the
    key DOMAIN — the qualifiers that keep two records that share an entity key
    (the same issue number, the same short sha suffix) from colliding when they
    belong to different sources, hosts, repos or kinds. The kind's own entity
    key (number / sha / id) is appended to that domain by
    :func:`primary_key_for`. Without the domain, one repo's issue #1 and another
    repo's issue #1 would collapse to one stored row.
    """

    source_id: str
    instance: str
    repo_full_name: str
    entity_type: str
    primary_key: str
    api_url: str
    fetched_at: str

    def validate(self) -> None:
        """Raise :class:`LineageError` unless every fact is present and well-formed."""
        # The four key-domain segments must all be present, and none may carry
        # the segment separator, or a value could forge a different key.
        for label, value in (
            ("source_id", self.source_id),
            ("instance", self.instance),
        ):
            if not (value or "").strip():
                raise LineageError(f"{label} is required")
            if _PK_SEP in value:
                raise LineageError(f"{label} may not contain {_PK_SEP!r}")
        if not _REPO_FULL_NAME_RE.match(self.repo_full_name or ""):
            raise LineageError(f"repo_full_name must be 'owner/name', got {self.repo_full_name!r}")
        if self.entity_type not in _ENTITY_TYPES:
            raise LineageError(f"unknown entity_type {self.entity_type!r}")
        if not (self.primary_key or "").strip():
            raise LineageError("primary_key is required")
        if not (self.api_url or "").strip():
            raise LineageError("api_url is required")
        if not (self.fetched_at or "").strip():
            raise LineageError("fetched_at is required")


_ENTITY_TYPES = frozenset({ENTITY_ISSUE, ENTITY_PULL_REQUEST, ENTITY_COMMIT, ENTITY_CHECK_RUN})


# ── typed rows ─────────────────────────────────────────────────────────────
# Each row is a record with real fields, not an opaque blob. The class-level
# PRIMARY_KEY_FIELDS name the fields whose values form the row's identity; that
# identity — never the title — is the dedup/idempotency key across refreshes.


@dataclass(frozen=True)
class IssueOrPullRow:
    """A GitHub issue or pull request.

    The kind's entity key is ``number``: GitHub numbers issues and pull requests
    in one shared per-repository sequence, so the number is unique within a repo.
    The repository, instance and source are separate segments of the full key
    domain (see :class:`RowLineage`), so ``number`` alone is the entity part. The
    title is a mutable field, never part of the key — the same issue keeps its
    identity when it is renamed.
    """

    PRIMARY_KEY_FIELDS = ("number",)

    repo_full_name: str
    number: int
    is_pull_request: bool
    title: str
    state: str
    author: str | None
    body: str
    labels: tuple[str, ...]
    created_at: str
    updated_at: str
    closed_at: str | None
    html_url: str
    lineage: RowLineage

    @property
    def primary_key(self) -> str:
        return primary_key_for(self)


@dataclass(frozen=True)
class CommitRow:
    """A GitHub commit. Identity is the ``sha`` — globally unique by construction."""

    PRIMARY_KEY_FIELDS = ("sha",)

    repo_full_name: str
    sha: str
    message: str
    author_name: str | None
    author_email: str | None
    authored_date: str | None
    committer_name: str | None
    committed_date: str | None
    parents: tuple[str, ...]
    html_url: str
    lineage: RowLineage

    @property
    def primary_key(self) -> str:
        return primary_key_for(self)


@dataclass(frozen=True)
class CheckRunRow:
    """A GitHub check-run. Identity is the numeric check-run ``id``."""

    PRIMARY_KEY_FIELDS = ("id",)

    repo_full_name: str
    id: int
    name: str
    head_sha: str
    status: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    details_url: str | None
    html_url: str
    lineage: RowLineage

    @property
    def primary_key(self) -> str:
        return primary_key_for(self)


TypedRow = IssueOrPullRow | CommitRow | CheckRunRow


def primary_key_for(row: TypedRow) -> str:
    """The row's identity string over the FULL key domain.

    Composed as ``source_id | instance | repo_full_name | entity_type |
    <entity-key>``, where the entity key is the row's declared
    ``PRIMARY_KEY_FIELDS`` joined by ``/``. The four domain segments come from
    the row's lineage, so two records that share an entity key never collapse
    when they belong to different sources, GitHub hosts, repositories or kinds:
    one repo's issue #1 and another repo's issue #1 are distinct keys, and so
    are the same repo mirrored under two knowledge sources or two instances.

    A single stable string so refreshes can diff by identity with set
    operations. Each entity-key part is the field value rendered verbatim.
    """
    entity_key = "/".join(str(getattr(row, f)) for f in row.PRIMARY_KEY_FIELDS)
    lin = row.lineage
    return _PK_SEP.join(
        (lin.source_id, lin.instance, lin.repo_full_name, lin.entity_type, entity_key)
    )


# ── conversion: GitHub payload shape → typed row ────────────────────────────
# Each takes a repository full name, one raw GitHub API object, and the read
# timestamp, and returns one typed row with validated lineage. A malformed
# payload (missing required identity field, bad repo name) raises rather than
# producing a row that cannot be keyed or traced.


def _require(payload: dict, key: str, entity: str):
    if key not in payload or payload[key] is None:
        raise LineageError(f"{entity} payload missing required field {key!r}")
    return payload[key]


def _labels(payload: dict) -> tuple[str, ...]:
    out: list[str] = []
    for lbl in payload.get("labels") or ():
        if isinstance(lbl, dict):
            name = lbl.get("name")
        else:
            name = lbl
        if name:
            out.append(str(name))
    return tuple(out)


def _make_lineage(
    *,
    source_id: str,
    instance: str,
    repo_full_name: str,
    entity_type: str,
    entity_key: str,
    api_url: str,
    fetched_at: str,
) -> RowLineage:
    """Build a validated lineage whose ``primary_key`` is the full-domain key.

    The composed key is ``source_id | instance | repo | kind | entity_key`` — the
    same string :func:`primary_key_for` derives from a finished row, computed
    here so the lineage a row carries and the row's ``primary_key`` property
    always agree.
    """
    primary_key = _PK_SEP.join((source_id, instance, repo_full_name, entity_type, entity_key))
    lineage = RowLineage(
        source_id=source_id,
        instance=instance,
        repo_full_name=repo_full_name,
        entity_type=entity_type,
        primary_key=primary_key,
        api_url=api_url,
        fetched_at=fetched_at,
    )
    lineage.validate()
    return lineage


def issue_or_pull_from_payload(
    repo_full_name: str,
    payload: dict,
    *,
    source_id: str,
    fetched_at: str,
    instance: str = DEFAULT_INSTANCE,
) -> IssueOrPullRow:
    number = int(_require(payload, "number", "issue/pull"))
    # GitHub's issues API returns pull requests too, marked by a ``pull_request``
    # sub-object; the pulls API omits it but every PR object carries it. Either
    # signal makes this a PR.
    is_pr = "pull_request" in payload or payload.get("_is_pull_request", False)
    entity = ENTITY_PULL_REQUEST if is_pr else ENTITY_ISSUE
    user = payload.get("user") or {}
    lineage = _make_lineage(
        source_id=source_id,
        instance=instance,
        repo_full_name=repo_full_name,
        entity_type=entity,
        entity_key=str(number),
        api_url=str(_require(payload, "url", "issue/pull")),
        fetched_at=fetched_at,
    )
    return IssueOrPullRow(
        repo_full_name=repo_full_name,
        number=number,
        is_pull_request=is_pr,
        title=str(payload.get("title") or ""),
        state=str(payload.get("state") or ""),
        author=(user.get("login") if isinstance(user, dict) else None),
        body=str(payload.get("body") or ""),
        labels=_labels(payload),
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        closed_at=(payload.get("closed_at") or None),
        html_url=str(payload.get("html_url") or ""),
        lineage=lineage,
    )


def commit_from_payload(
    repo_full_name: str,
    payload: dict,
    *,
    source_id: str,
    fetched_at: str,
    instance: str = DEFAULT_INSTANCE,
) -> CommitRow:
    sha = str(_require(payload, "sha", "commit"))
    commit = payload.get("commit") or {}
    author = commit.get("author") or {}
    committer = commit.get("committer") or {}
    parents = tuple(str(p.get("sha")) for p in (payload.get("parents") or ()) if p.get("sha"))
    lineage = _make_lineage(
        source_id=source_id,
        instance=instance,
        repo_full_name=repo_full_name,
        entity_type=ENTITY_COMMIT,
        entity_key=sha,
        api_url=str(_require(payload, "url", "commit")),
        fetched_at=fetched_at,
    )
    return CommitRow(
        repo_full_name=repo_full_name,
        sha=sha,
        message=str(commit.get("message") or ""),
        author_name=(author.get("name") or None),
        author_email=(author.get("email") or None),
        authored_date=(author.get("date") or None),
        committer_name=(committer.get("name") or None),
        committed_date=(committer.get("date") or None),
        parents=parents,
        html_url=str(payload.get("html_url") or ""),
        lineage=lineage,
    )


def check_run_from_payload(
    repo_full_name: str,
    payload: dict,
    *,
    source_id: str,
    fetched_at: str,
    instance: str = DEFAULT_INSTANCE,
) -> CheckRunRow:
    run_id = int(_require(payload, "id", "check-run"))
    lineage = _make_lineage(
        source_id=source_id,
        instance=instance,
        repo_full_name=repo_full_name,
        entity_type=ENTITY_CHECK_RUN,
        entity_key=str(run_id),
        api_url=str(_require(payload, "url", "check-run")),
        fetched_at=fetched_at,
    )
    return CheckRunRow(
        repo_full_name=repo_full_name,
        id=run_id,
        name=str(payload.get("name") or ""),
        head_sha=str(payload.get("head_sha") or ""),
        status=str(payload.get("status") or ""),
        conclusion=(payload.get("conclusion") or None),
        started_at=(payload.get("started_at") or None),
        completed_at=(payload.get("completed_at") or None),
        details_url=(payload.get("details_url") or None),
        html_url=str(payload.get("html_url") or ""),
        lineage=lineage,
    )


# ── incremental refresh: since + primary-key diff ───────────────────────────
# GitHub exposes no single delta token, so an incremental refresh is computed:
# ask only for records touched since the last watermark, then diff the returned
# keys against the keys already stored. The result names what to upsert and what
# has disappeared, and never relies on a title or ordinal position.


@dataclass(frozen=True)
class RefreshPlan:
    """The outcome of diffing a fetched key set against the stored key set."""

    upserts: tuple[TypedRow, ...]
    # Keys present in the store, absent from a FULL listing of the repo — i.e.
    # genuinely gone, safe for a caller to retire. Populated ONLY when the diff
    # was over a full listing (``full_listing=True``). On a windowed (``since``)
    # refresh this is always empty, because a window returns only CHANGED
    # records: "absent from this window" is the normal state of nearly every
    # stored key and says nothing about whether the record still exists.
    disappeared: tuple[str, ...]
    # The watermark to persist once this plan is applied — the max ``updated_at``
    # seen, so the next refresh asks only for what changed after it.
    next_since: str | None


def _max_timestamp(rows: tuple[TypedRow, ...]) -> str | None:
    stamps: list[str] = []
    for row in rows:
        stamp = (
            getattr(row, "updated_at", None)
            or getattr(row, "committed_date", None)
            or getattr(row, "completed_at", None)
            or row.lineage.fetched_at
        )
        if stamp:
            stamps.append(stamp)
    return max(stamps) if stamps else None


def diff_rows(
    fetched: tuple[TypedRow, ...],
    stored_keys: frozenset[str],
    *,
    prior_since: str | None,
    full_listing: bool = False,
) -> RefreshPlan:
    """Diff a freshly-fetched set against what is already stored.

    ``fetched`` are the typed rows a query returned; ``stored_keys`` are the
    primary keys already in the source. Every fetched row is an upsert.

    ``full_listing`` says whether ``fetched`` is a COMPLETE listing of the repo
    (no ``since`` filter) or an incremental WINDOW. It decides ``disappeared``:

    * full listing -> a stored key not in ``fetched`` is genuinely gone, so it
      is reported for the caller to retire.
    * window (the default, incremental case) -> ``disappeared`` is always empty.
      A ``since`` window carries only CHANGED records, so nearly every stored
      key is "absent from this window" without being gone; reporting those would
      invite a caller to purge live records.

    A primary-key collision inside one fetched set (two rows sharing a key)
    keeps the LAST occurrence, matching an upsert's last-writer-wins semantics,
    so a page boundary that re-lists a straddling record is idempotent.
    """
    by_key: dict[str, TypedRow] = {}
    for row in fetched:
        by_key[row.primary_key] = row  # last-writer-wins on collision
    upserts = tuple(by_key.values())
    disappeared = tuple(sorted(stored_keys - by_key.keys())) if full_listing else ()
    window_max = _max_timestamp(upserts)
    if window_max is None:
        next_since = prior_since
    elif prior_since is None:
        next_since = window_max
    else:
        # Never move the watermark backwards: a clock-skewed or re-listed record
        # with an older stamp must not reopen an already-covered window.
        next_since = max(window_max, prior_since)
    return RefreshPlan(upserts=upserts, disappeared=disappeared, next_since=next_since)


# ── resumable checkpoint (stored in the source's existing properties blob) ──
# No new table: the checkpoint is a small dict under a reserved key in the
# ``properties`` blob the scheduler already round-trips. It records the
# watermark and the position within a multi-entity, multi-page refresh so an
# interrupted run resumes instead of restarting.

_CHECKPOINT_KEY = "github_structured_checkpoint"

# The entity kinds a refresh walks, in a fixed order so a resume knows which
# kinds are already done and which remains.
REFRESH_ENTITY_ORDER = (ENTITY_ISSUE, ENTITY_COMMIT, ENTITY_CHECK_RUN)


@dataclass
class Checkpoint:
    """Where a refresh is up to, so it can resume after an interruption.

    ``since`` is the watermark for the *next* full refresh. ``entity_index`` and
    ``page_cursor`` locate the position inside an in-progress refresh: which
    entity kind (index into :data:`REFRESH_ENTITY_ORDER`) and which page cursor
    within it were last completed. ``in_progress`` distinguishes a clean
    watermark-only checkpoint from one paused mid-walk.
    """

    since: str | None = None
    entity_index: int = 0
    page_cursor: str | None = None
    in_progress: bool = False

    def to_dict(self) -> dict:
        return {
            "since": self.since,
            "entity_index": self.entity_index,
            "page_cursor": self.page_cursor,
            "in_progress": self.in_progress,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Checkpoint":
        if not data:
            return cls()
        try:
            entity_index = int(data.get("entity_index", 0))
        except (TypeError, ValueError):
            entity_index = 0
        # Clamp a persisted index that does not name a valid entity (a shorter
        # order between versions) so a resume starts over rather than indexing
        # out of range.
        if not 0 <= entity_index < len(REFRESH_ENTITY_ORDER):
            entity_index = 0
        return cls(
            since=(data.get("since") or None),
            entity_index=entity_index,
            page_cursor=(data.get("page_cursor") or None),
            in_progress=bool(data.get("in_progress", False)),
        )


def read_checkpoint(source: dict) -> Checkpoint:
    """Read the checkpoint out of a source's ``properties`` blob.

    ``source`` is a sources-row dict whose ``properties`` may already be a parsed
    dict (as the scheduler hands it) or a JSON string (as the raw row carries
    it); both are accepted so callers need not normalise first.
    """
    props = source.get("properties")
    if isinstance(props, str):
        try:
            props = json.loads(props or "{}")
        except ValueError:
            props = {}
    if not isinstance(props, dict):
        props = {}
    return Checkpoint.from_dict(props.get(_CHECKPOINT_KEY))


def write_checkpoint(properties: dict, checkpoint: Checkpoint) -> dict:
    """Return a copy of ``properties`` with the checkpoint written under its key.

    Pure: the caller persists the result through ``store.update_source`` (the
    existing per-source state write); nothing here touches the store, so the
    checkpoint logic is testable without a database.
    """
    updated = dict(properties or {})
    updated[_CHECKPOINT_KEY] = checkpoint.to_dict()
    return updated


# ── rendering a typed row into the pipeline's (text, metadata) shape ────────
# The scheduler-facing contract is prose-shaped: ``fetch`` returns one text blob
# plus metadata that the ingestion pipeline chunks and stores, and lineage is
# recorded per stored item. A typed row renders to a stable, human-readable text
# projection (so a search hit is legible) and a flat metadata dict carrying the
# primary key and the full lineage domain (so the stored item stays traceable).


def render_row_text(row: TypedRow) -> str:
    """A stable, legible text projection of a typed row for the search index."""
    d = asdict(row)
    d.pop("lineage", None)
    lines = [f"{row.lineage.entity_type} {row.primary_key}"]
    for key, value in d.items():
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def render_row_metadata(row: TypedRow) -> dict:
    """A flat metadata dict: the primary key plus the full lineage domain."""
    return {
        "primary_key": row.primary_key,
        "source_id": row.lineage.source_id,
        "instance": row.lineage.instance,
        "repo_full_name": row.lineage.repo_full_name,
        "entity_type": row.lineage.entity_type,
        "api_url": row.lineage.api_url,
        "fetched_at": row.lineage.fetched_at,
    }


class GithubStructuredConnector(BaseConnector):
    """Consume GitHub issues/PRs/commits/check-runs as one structured source.

    Conforms to :class:`BaseConnector` so the existing ``SyncScheduler`` drives
    it. The typed-row conversion, primary-key diffing and checkpointing are the
    module-level pure functions above; this class binds them to the connector
    contract and owns config validation.

    The transport is deliberately absent. :meth:`fetch` and :meth:`detect_changes`
    refuse until the live executor is wired, because a mock read is not a
    live read; and :meth:`validate_config` refuses source *creation* for the same
    reason. The pure conversion/diff/checkpoint surface is what is verified in
    this PR (it is not on ``main`` yet).
    """

    def source_type(self) -> str:
        return SOURCE_TYPE

    def validate_config(self, config: dict) -> tuple[bool, str]:
        # The add-source POST path passes the location under ``"url"`` (see the
        # knowledge handler's ``validate_config({**properties, "url": uri})``),
        # so read ``url`` first and fall back to ``uri`` exactly as
        # ``local_folder`` does — otherwise the value below is invisible here.
        raw = config.get("url") or config.get("uri", "")
        # Guard the type before any string op: the handler forwards whatever JSON
        # the request carried, so ``uri: 123`` would reach ``.strip()`` and raise
        # AttributeError (an HTTP 500 on a request we reject anyway). Reject a
        # non-string cleanly with a 400-shaped (False, reason) instead.
        if not isinstance(raw, str):
            return False, "GitHub repository (owner/name) must be a string"
        # A ``github://owner/repo`` form is normalised to the bare repo.
        repo = raw.strip().removeprefix("github://")
        if not repo:
            return False, "GitHub repository (owner/name) is required"
        if not _REPO_FULL_NAME_RE.match(repo):
            return False, f"repo must be 'owner/name', got {repo!r}"
        # Shape is valid, but creation is REFUSED until a real runtime is wired.
        # The connector is registered (``source_type='github'`` resolves here),
        # yet fetch/detect_changes still refuse because the connections transport
        # executor and sync runner are not assembled on ``main`` yet. Admitting a
        # source now would create a row the scheduler can never sync — so we fail
        # closed at creation with a specific reason, not a generic error. This is
        # lifted to allowed (with creation + sync tests) when PR-3 wires the
        # runner; no feature flag, env switch, or mock transport is used here.
        return (
            False,
            "GitHub structured source is registered but cannot be created yet: "
            "its live runtime (the connections transport executor and sync "
            "runner) is not assembled on main. Creation is enabled when PR-3 "
            "wires the runner.",
        )

    async def detect_changes(self, source: dict) -> bool:
        # UNVERIFIED until the transport lands: a real detect_changes issues a
        # conditional/`since` probe against GitHub. It raises here — fail-closed,
        # scheduling no ingest — rather than fabricating a "changed" answer from
        # a mock.
        raise NotImplementedError(
            "GitHub live change-detection needs the connections transport "
            "executor, not on main yet"
        )

    async def fetch(self, source: dict) -> tuple[str, dict]:
        # UNVERIFIED until the transport lands and pagination is consumed from
        # connections.vendors.github. Refusing here is deliberate: a
        # first-page-only or mocked payload is not a live dataset and must not be
        # stored as one.
        raise NotImplementedError(
            "GitHub live fetch needs the connections transport executor and "
            "connections.vendors.github.pagination; neither is on main yet"
        )
