"""What a source provider and its callers agree on.

The validated refs every entry point hands around, the full-change payload a
provider returns, the protocol a downstream edition implements to add a provider,
and the errors each layer raises. Only types and the URL bound live here, so
every sibling can import this module and it imports none of them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields
from typing import Any, Protocol, TypedDict

# Every owner module logs under the handlers' historical name, not its own
# ``__name__`` — the facade-surface test pins this so logging configuration keyed
# on the old path keeps reaching every relocated helper. The shared
# ``LOGGER_NAME`` constant lives in this package's ``__init__``, but ``contract``
# deliberately imports none of its siblings (see the module docstring), and
# importing it from the package ``__init__`` would be a cycle — so spell the same
# literal here. Keep it identical to ``source_providers.LOGGER_NAME``.
logger = logging.getLogger("kiro_crew.dashboard.handlers.source_providers")

_MAX_URL_LENGTH = 2048


class SourceProviderError(RuntimeError):
    """A provider CLI could not return the requested source data."""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class SourceCapacityError(SourceProviderError):
    """Admission room did not free up within the wait budget.

    Distinct from its parent so the HTTP layer can mark it retryable: nothing is
    wrong with the request or the provider, the gateway was simply holding its
    concurrent-fetch memory ceiling for longer than the caller agreed to wait.
    """


class SourceProviderNotConfigured(SourceProviderError):
    """A registered plugin cannot reach its provider until an operator sets it up.

    The built-in equivalent is a missing ``gh``/``glab``, which is answered with
    :func:`_provider_setup_message`. A plugin raises this instead of composing its
    own guidance at every call site, and the dispatch substitutes the plugin's
    :meth:`SourceProviderPlugin.setup_message` -- so the "here is how to fix it"
    text is authored in ONE place per provider, exactly as it is for gh/glab.
    """


@dataclass(frozen=True)
class SourceRef:
    provider: str
    url: str
    host: str
    owner: str
    repo: str
    number: int
    project: str = ""
    # Which namespace the number belongs to: "change" (pull/merge request) or
    # "issue". Defaults to "change" so every pre-existing construction site and
    # test fixture keeps its current meaning. Load-bearing for safety, not just
    # display: GitHub shares one number counter between issues and pull
    # requests, so an issue ref reaching a pull-request-only path would address a
    # DIFFERENT object with the same number. See :func:`_require_change_ref`.
    kind: str = "change"

    @property
    def identity(self) -> tuple:
        """Stable identity of the referenced object, for dedup keying.

        Every field except the canonical URL: a provider whose grammar accepts
        more than one URL shape for the same change (e.g. an optional revision
        pin kept in the canonical URL) must collapse to one identity, so the
        URL cannot participate. Derived from the dataclass fields rather than
        hand-listed, so a future identity-bearing field is included
        automatically instead of silently falling out and over-collapsing
        distinct objects.

        Jira is the one exception that adds the URL back in: a self-hosted
        instance's context path (the ``/jira`` in
        ``https://host/jira/browse/PROJ-1``) exists only in the URL, so two
        instances on one host would otherwise collide on the same issue key.
        Safe to include because :func:`_jira_ref` emits exactly one canonical
        URL per issue per instance -- it can never split one object in two.
        """
        instance_context = self.url if self.provider == "jira" else ""
        return tuple(getattr(self, f.name) for f in fields(self) if f.name != "url") + (
            instance_context,
        )


# Defensive cap on a dismissed-identity key travelling in a URL path segment,
# rejecting a hand-crafted oversized path before it is decoded/hashed. A GitHub
# or GitLab identity's canonical JSON is far below this. The one identity that
# could approach it is a self-hosted Jira, whose identity embeds the full
# instance URL (its context path is load-bearing for uniqueness); a
# pathologically long such URL would exceed the cap and its chip would then be
# un-unlinkable (a graceful degradation — 400 on unlink, never a crash), which
# is an accepted trade to keep the bound tight against abuse.
_MAX_SOURCE_IDENTITY_KEY_LENGTH = 512


def _identity_shape() -> tuple[int, frozenset[int]]:
    """Derive the serialized-identity arity and which member slots are ints.

    ``SourceRef.identity`` is ``[every field except url, in field order] +
    [instance_context]`` (see the property). Rather than hardcode "8 members,
    int at index 4" -- which silently rots the moment a field is added to the
    dataclass, dropping every persisted dismissal on restore -- derive the arity
    and the integer slots from the dataclass itself, so the validator tracks the
    identity's true shape automatically. ``instance_context`` is a str, so it
    adds one to the arity and no int slot.
    """
    members = [f for f in fields(SourceRef) if f.name != "url"]
    int_slots = frozenset(i for i, f in enumerate(members) if f.type in ("int", int))
    return len(members) + 1, int_slots


_IDENTITY_ARITY, _IDENTITY_INT_SLOTS = _identity_shape()


def source_ref_identity_key(identity: tuple) -> str:
    """Serialize a :attr:`SourceRef.identity` tuple to a stable string key.

    The dismissed-identity suppression set is persisted to disk and echoed in a
    DELETE URL path, neither of which can carry a Python tuple. This renders the
    identity to canonical JSON: a fixed member order (the tuple's own), no
    incidental whitespace, and ``ensure_ascii`` so a non-ASCII owner/repo cannot
    change the byte shape between a writer and a reader on different locales. The
    mapping is total and deterministic, so the same object always yields the same
    key and two distinct objects never collide.

    Keyed on the identity rather than the URL for the same reason the derivation
    dedups on identity: one change can be mentioned through more than one URL
    shape, and a dismiss must suppress the object, not one spelling of it.
    """
    return json.dumps(list(identity), ensure_ascii=True, separators=(",", ":"))


def is_valid_source_identity_key(key: object) -> bool:
    """True when *key* is a well-formed serialized identity key.

    The DELETE endpoint takes the key from an untrusted URL path segment, so it
    is validated before it is recorded: it must be a bounded string that decodes
    to the exact JSON shape :func:`source_ref_identity_key` emits — the 8-member
    ``SourceRef.identity`` list ``(provider, host, owner, repo, number, project,
    kind, instance_context)`` whose ``number`` slot is an int and whose other
    seven members are strings — and it must re-serialize byte-for-byte to the
    same key (rejecting any non-canonical spelling). A key that is merely a list
    of scalars but the wrong arity/type (e.g. ``[1]``) is rejected too: it could
    never match a real identity and would otherwise accumulate as stored junk.
    """
    if not isinstance(key, str) or not key or len(key) > _MAX_SOURCE_IDENTITY_KEY_LENGTH:
        return False
    try:
        decoded = json.loads(key)
    except (ValueError, TypeError):
        return False
    if not isinstance(decoded, list) or not decoded:
        return False
    # Enforce the exact ``SourceRef.identity`` arity and per-field types, not
    # just "a list of scalars": without this a canonical-but-nonsense key such
    # as ``[1]`` round-trips through the re-serialize check below and is stored
    # -- inert (it can never match a real identity) but accumulating as junk.
    # The arity and which slots are ints are DERIVED from the dataclass
    # (``_identity_shape``) so a new field cannot silently invalidate this. Every
    # non-int slot is a string; ``bool`` is a subclass of ``int`` so it is
    # rejected explicitly for the int slots.
    if len(decoded) != _IDENTITY_ARITY:
        return False
    for i, member in enumerate(decoded):
        if i in _IDENTITY_INT_SLOTS:
            if isinstance(member, bool) or not isinstance(member, int):
                return False
        elif not isinstance(member, str):
            return False
    return json.dumps(decoded, ensure_ascii=True, separators=(",", ":")) == key


def bounded_valid_identities(raw: object, cap: int) -> set[str]:
    """Collect at most *cap* distinct VALID identity keys from *raw*, bounding
    retention DURING iteration.

    Every reader of an on-disk ``dismissed_source_links`` line filters it through
    :func:`is_valid_source_identity_key` and then bounds it to the per-slot
    ceiling. Doing that as ``{k for k in raw if valid}`` first and slicing after
    materializes the WHOLE (externally-controllable, possibly oversized/tampered)
    list before the cap applies — the ``a-bound-bounds-every-field-it-retains``
    hazard. At most *cap* distinct valid keys are ever RESIDENT: once the kept
    set is full it grows no further, so no more than *cap* keys are held
    regardless of how large *raw* is. Iteration continues past that only to COUNT
    the valid keys it had to drop and emit one truncation warning — a single
    integer counter, not more retained keys — because the invariant requires the
    overflow be said out loud once per snapshot rather than silently discarded. A
    non-list *raw* yields the empty set (the line records no dismissals).
    """
    out: set[str] = set()
    if not isinstance(raw, list):
        return out
    # Count VALID keys that arrived once the cap was already reached and were
    # therefore dropped, and say so once — the sibling bounded helpers in
    # ``chat_persistence`` (write + restore) both count and warn, and
    # ``a-bound-bounds-every-field-it-retains`` requires the overflow be counted
    # and surfaced once per snapshot rather than silently discarded. A key
    # already collected (a duplicate) is not a drop, so it is not counted.
    truncated = 0
    for key in raw:
        if not is_valid_source_identity_key(key):
            continue
        if len(out) >= cap:
            if key not in out:
                truncated += 1
            continue
        out.add(key)
    if truncated:
        logger.warning(
            "dismissed_source_links read bounded: dropped %d valid key(s) past the %d cap",
            truncated,
            cap,
        )
    return out


class SourceChangeCommit(TypedDict):
    """One commit row in the panel's Commits tab."""

    sha: str
    title: str
    body: str
    #: Author login/display name. A plain string, not a user object.
    author: str
    #: ISO-8601 timestamp, ``""`` when the provider does not report one.
    date: str
    #: Web URL for the commit, ``""`` when the provider has no per-commit page.
    url: str


class SourceChangeFile(TypedDict):
    """One changed file in the panel's Files tab."""

    path: str
    #: Provider-vocabulary status: ``added`` / ``modified`` / ``removed`` / ...
    status: str
    additions: int
    deletions: int
    #: Unified-diff hunks for this file, ``""`` when the provider cannot serve
    #: per-file patches (the panel then renders the row without a diff body).
    patch: str


class SourceChangeComment(TypedDict):
    """One comment row: a top-level comment, a review verdict, or an inline
    review comment. ``kind`` says which; the thread fields are only ever
    populated on inline comments."""

    id: str
    #: ``"comment"`` (top-level) | ``"review"`` (verdict) | ``"inline"``.
    kind: str
    author: str
    body: str
    #: Review verdict state (``APPROVED`` / ``CHANGES_REQUESTED`` / ...),
    #: ``""`` for non-review comments.
    state: str
    createdAt: str
    url: str
    #: File path an inline comment anchors to, ``""`` otherwise.
    path: str
    line: int | None
    #: Provider thread id, ``""`` when the comment is not part of a resolvable
    #: thread. This is the id handed back to ``resolve_thread`` /
    #: ``reply_to_thread``, so it must be self-contained.
    threadId: str
    resolvable: bool
    resolved: bool


class _SourceChangePayloadExtras(TypedDict, total=False):
    """Optional keys a change payload MAY carry on top of the required set."""

    #: Authoritative aggregate CI for the sidebar chip glyph (``running`` /
    #: ``passed`` / ``failed``), consumed by ``status_from_full_payload`` when
    #: present. The GitLab fetcher emits it; a plugin usually should not --
    #: the optional ``fetch_check_status`` hook is the cheaper way to feed the
    #: chip without a full-payload fetch.
    ciStatus: str


class SourceChangePayload(_SourceChangePayloadExtras):
    """The full-change payload contract: what :meth:`SourceProviderPlugin.fetch_full`
    returns and what the built-in GitHub/GitLab fetchers already produce.

    Every key declared on this class is required -- a provider without a
    concept fills the neutral value (``""``, ``0``, ``[]``) rather than
    omitting the key, so the frontend never distinguishes "provider lacks it"
    from "fetch went wrong". Optional extras live on the ``total=False`` base.
    String enums stay provider vocabulary (the frontend renders them mostly
    verbatim); the two the panel *branches* on are ``state`` (``OPEN``/
    ``MERGED``/``CLOSED``-style, upper-cased) and ``mergeable``/
    ``mergeStateStatus`` (GitHub vocabulary; a provider without merge-state
    detail fills ``""`` and sets its frontend descriptor's
    ``capabilities.mergeState`` to false so the banner never reads them).
    """

    provider: str
    #: The canonical URL from the validated ref -- never a provider echo.
    url: str
    number: int
    title: str
    description: str
    state: str
    draft: bool
    mergedAt: str
    mergeable: str
    mergeStateStatus: str
    autoMerge: bool
    updatedAt: str
    headBranch: str
    baseBranch: str
    headSha: str
    author: str
    additions: int
    deletions: int
    changedFiles: int
    commits: list[SourceChangeCommit]
    #: Same normalized check dicts :meth:`fetch_checks` returns; ``[]`` when
    #: checks ride the separate degradable read or the provider has no CI.
    checks: list[dict[str, Any]]
    comments: list[SourceChangeComment]
    files: list[SourceChangeFile]
    #: Names of sections known to be truncated by pagination caps, surfaced as
    #: a "partial data" note in the panel. ``[]`` when complete.
    partialSections: list[str]


class SourceProviderPlugin(Protocol):
    """What a downstream edition implements to add a source provider.

    Registration order is consultation order and built-ins always win, so a
    plugin can only ever claim URLs no built-in recognized.
    """

    #: The ``provider`` value this plugin owns. Must equal ``SourceRef.provider``
    #: on every ref it returns, and must match the frontend descriptor's ``id``.
    id: str

    def parse(self, raw_url: str) -> SourceRef | None:
        """Recognize one URL and return a NORMALIZED ref, or None.

        Called only after every built-in host check declined, and only with a URL
        the shared validator already proved is ``https`` with no userinfo and
        within ``_MAX_URL_LENGTH``. The returned ``url`` must be the canonical
        form -- it becomes the cache key, the audit subject, and the string the
        dashboard persists and re-parses.

        The ref must have ``kind="change"``. Issue refs are refused at
        admission: no plugin fetch path serves them (``fetch_full`` is a
        change-payload contract and the issue pipeline is built-in-only), so an
        admitted issue ref could only ever produce a chip whose panel 400s.
        """
        ...

    async def fetch_full(self, ref: SourceRef, *, refresh: bool = False) -> SourceChangePayload:
        """Fetch the full change payload -- the :class:`SourceChangePayload` schema.

        Called inside the shared cache and admission layer, so it must not add
        caching of its own. Raise :class:`SourceProviderNotConfigured` when the
        provider is unreachable until an operator acts;
        :class:`SourceProviderError` for any other provider-side failure.
        """
        ...

    async def fetch_checks(self, ref: SourceRef) -> list[dict[str, Any]]:
        """Fetch current CI checks, in the shape ``_fetch_github_checks`` returns.

        A LIST of normalized check dicts (each with at least ``name`` and a
        ``bucket`` of ``failed``/``pending``/``passed``/``skipped``); the gateway
        wraps it as ``{"checks": [...]}`` for the wire. Return ``[]`` for a
        provider with no CI concept -- and set the frontend descriptor's
        ``capabilities.checks`` to false so the tab is not offered at all.
        """
        ...

    def setup_message(self) -> str:
        """Operator-facing guidance shown when the provider is not configured.

        The plugin's counterpart to :func:`_provider_setup_message`, surfaced
        verbatim when :meth:`fetch_full` or :meth:`fetch_checks` raises
        :class:`SourceProviderNotConfigured`.
        """
        ...

    # Optional hooks. Each is looked up with `getattr`, so a plugin implements
    # only what its provider can do; an absent hook makes the matching endpoint
    # answer "not supported by this provider" instead of failing obscurely
    # inside a built-in code path.
    #
    #   def chip_label(self, ref: SourceRef) -> str
    #   def path_markers(self) -> Sequence[str]
    #   def search_ref(self, token: str) -> tuple[str, Sequence[str]] | None
    #       Recognize ONE casefolded query token as this provider's item and
    #       answer `(canonical_spelling, alternative_spellings)` -- every spelling
    #       casefolded -- or None. Contributed to transcript search through
    #       `source_search_ref()`, so a query naming a review by id gates on the
    #       item rather than on the literal string. Spellings only: a provider
    #       cannot contribute lead-in vocabulary, and a bare all-digit token is
    #       never offered to a provider at all. Must be PURE and allocation-cheap -- no
    #       I/O, no network, no config read -- it is called for every term of
    #       every query, at least twice per search. Nothing in this repo registers
    #       a provider, so `FakeAcmePlugin.search_ref` in
    #       test/test_source_provider_plugin.py is the reference implementation.
    #   async def fetch_check_status(self, ref: SourceRef) -> dict[str, str]
    #   async def comment(self, ref: SourceRef, body: str) -> None
    #   async def resolve_thread(self, ref: SourceRef, thread_id: str,
    #                            *, resolved: bool) -> None
    #   async def reply_to_thread(self, ref: SourceRef, thread_id: str,
    #                             body: str) -> None
    #   async def mark_ready(self, ref: SourceRef) -> None
    #   async def enable_auto_merge(self, ref: SourceRef, *,
    #                              confirm_immediate_merge: bool) -> str


@dataclass(frozen=True)
class RepoRef:
    """A validated bare repository reference (host, owner, repo -- no number)."""

    provider: str
    host: str
    owner: str
    repo: str


class ConfirmationRequired(ValueError):
    """A mutation refused because the caller has not acknowledged its effect.

    Distinct from an ordinary rejection so the response can carry a machine-
    readable marker: the request is not malformed and is not permanently
    refused, it is waiting on an acknowledgement the client can only make
    meaningfully once the server has told it what is actually at stake.
    """
