"""Publish-provider interface for sharing KiroCrew artifacts to external
destinations.

A ``PublishProvider`` abstracts "publish this artifact's bytes to a destination
and give me back a stable id + URL, then keep versions/sharing in sync." The
interface is vendor-neutral: any destination that can accept bytes and return a
stable id/URL can plug in by implementing this interface and registering itself.
Mirrors KiroCrew's ``LLMProvider`` ABC pattern.

In the public (standalone) edition NO concrete provider is registered — the
registry is empty, so ``get_provider`` raises ``PublishUnavailableError`` (→ 503)
and ``list_providers`` returns ``[]``, which the dashboard renders as
"publishing unavailable" with no core branching. An out-of-repo companion
edition registers its concrete providers through the ``platform`` CPP seam
(``PublishRegistry.register_publish_providers``) at boot — the core never
imports a companion provider.

Layering:
- ``publish_provider`` (this module) — interface + result/exception types +
  registry. No networking, no store access.
- ``publish_sync`` — provider-agnostic orchestration that resolves a provider
  via the artifact's ``publication.provider`` and dispatches.
- concrete ``*_provider`` modules — companion-only; each self-registers.

Error model:
- ``publish`` / ``update_sharing`` / ``unpublish`` raise ``PublishError`` (or a
  subclass) on failure; the orchestration propagates to the HTTP handler, which
  maps each subclass to a status code.
- ``push_version`` is **best-effort** and NEVER raises for upstream failures —
  it returns a ``PushResult`` whose ``error`` is set (and ``conflict`` flags an
  optimistic-concurrency mismatch). This preserves the "a sync failure never
  fails the KiroCrew update" invariant.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)

# Neutral registry key used as the default provider name. The public edition
# ships an EMPTY registry, so ``get_provider`` always raises
# ``PublishUnavailableError`` regardless of this value; a companion edition
# registers its concrete provider(s) and may key its default off this name.
DEFAULT_PROVIDER = "default"


# ── Capability negotiation ───────────────────────────────────────────────────


class Capability(str, Enum):
    """Facets a provider may implement. Checked via capabilities()."""

    CONTENT_VERSIONS = "content_versions"
    CONTENT_PULL = "content_pull"
    SHARING = "sharing"
    COMMENTS_READ = "comments_read"
    COMMENTS_WRITE = "comments_write"
    COMMENTS_EDIT = "comments_edit"  # in-place edit of an existing comment body
    REVIEW = "review"
    PRESENCE = "presence"
    REALTIME = "realtime"
    MULTI_AGENT = "multi_agent"


#: Artifact kinds the publish SEAM cannot carry, whatever a provider could host.
#: The render-and-tempfile path in ``publish_sync`` is ``str``-typed end to end and
#: reads the body out of ``content``; a ``kind="image"`` artifact keeps its raster at
#: ``source_path`` and has no text body, so the seam refuses it rather than shipping a
#: zero-byte object. It lives HERE, on the module both the engine and every provider
#: already import, so a provider's ``kind_support`` declaration and the seam's refusal
#: cannot drift apart -- declaring a kind hostable that the seam then rejects offers the
#: user a publish that fails at the end, which is what this set exists to prevent.
NON_TEXT_KINDS: frozenset[str] = frozenset({"image"})


class KindSupport(str, Enum):
    """Second capability axis (design §1.3b): how well a provider hosts a given
    artifact ``kind``, independent of *which operations* it supports.

    Drives the share-panel picker: ``UNSUPPORTED`` disables the provider for
    that kind, ``DEGRADED`` warns ("won't render"), ``CONVERTED`` notes a
    lossy transform, ``NATIVE`` is first-class.

    ``str`` mixin (not ``StrEnum``) for Py3.10 compat, matching ``Capability``.
    """

    NATIVE = "native"
    CONVERTED = "converted"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


# ── Self-describing provider descriptors (design §1.4 / §2 / §2.5) ───────────


@dataclass
class SharingModel:
    """Declares the shape of a provider's sharing surface so the UI renders the
    right controls (design §2). Default is alias-principal-shaped (alias
    principals, roles, public, programmable via the provider API)."""

    supports_private: bool = True
    supports_shared: bool = True
    supports_public: bool = True
    #: "alias" | "team" | "bindle" | "wiki_acl" | "iam_principal" | "none"
    #: "none" means org-wide-readable with no per-principal grant list.
    principal_kind: str = "alias"
    supports_roles: bool = True
    #: Sharing has a time dimension (TTL).
    supports_expiration: bool = False
    #: Can KiroCrew set sharing via the provider API? When False the UI shows an
    #: "out of band" link instead of grant controls (web-UI-only sharing).
    programmable: bool = True
    #: Where the user manages sharing when not ``programmable`` (may contain a
    #: ``{docId}``/``{external_id}`` placeholder the UI substitutes).
    out_of_band_url: str = ""


@dataclass
class SyncModel:
    """Generalizes the two-state ``collab_mode`` into authority × concurrency
    (design §1.4). Drives push routing and the conflict/Force-push UI.

    - ``authority="kirocrew"`` + ``concurrency="token"`` → MIRROR_GUARDED
      (sha256 guard).
    - ``authority="kirocrew"`` + ``concurrency="lww"`` → MIRROR_LWW
      (blind last-write-wins; no Force-push).
    - ``authority="remote"`` + ``concurrency="crdt"`` → LIVE_CRDT
      (KiroCrew is a participant; no version-conflict UI).
    """

    #: "kirocrew" (MIRROR / collab_mode=mirror) | "remote" (LIVE / collab_mode=live)
    authority: str = "kirocrew"
    #: "token" (sha256/ETag guard) | "lww" (blind) | "crdt"
    concurrency: str = "token"

    @property
    def collab_mode(self) -> str:
        """Coarse authority bit carried on the artifact publication."""
        return "live" if self.authority == "remote" else "mirror"


@dataclass
class DiscoveryModel:
    """Which discovery primitives the browse UI can offer for a provider
    (design §2 / §2.5). The mine + shared-with-me + public shape is the
    default; a full-text provider declares full-text + mine only."""

    list_mine: bool = True
    list_shared_with_me: bool = True
    list_public: bool = True
    full_text_search: bool = False
    #: Reach an item by URL/id even if it is in no listing.
    pull_by_id: bool = True


@dataclass
class RemoteListing:
    """Provider-neutral discovery row (powers the browse UI + ``list_remote``).

    Each provider maps its native list/search shape onto this. ``external_id``
    is the provider's stable id; ``view_url`` is the human link; ``updated_at``
    is a best-effort ISO/epoch string for sort.
    """

    external_id: str
    title: str = ""
    owner: str = ""
    view_url: str = ""
    updated_at: str = ""
    snippet: str = ""


# ── Exceptions (handler maps each to an HTTP status) ─────────────────────────


class PublishError(Exception):
    """Base publish/sharing/unpublish failure (handler → HTTP 502)."""


class PublishUnavailableError(PublishError):
    """The destination's tooling is not installed / not launchable (→ 503)."""


class PublishConflictError(PublishError):
    """An optimistic-concurrency conflict on a version push (→ 409)."""


class NotPublishedError(PublishError):
    """Precondition failure — the artifact is not published (→ 409)."""


class DriveNotFound(PublishError):
    """The destination this publication is bound to could not be resolved.

    A TYPE rather than a message, because callers must decide on it. Whether a
    published copy can still be withdrawn is the difference between deleting the
    local record safely and stranding a world-readable object with no handle, so
    the answer has to survive refactors, localisation and provider-specific
    wording. Matching substrings like "NoSuchBucket" or "404" in stderr cannot:
    it silently answers "gone" for a throttled or unauthorized reply, which is
    exactly the reading that turns a temporary failure into a permanent orphan.

    Raised ONLY where absence is PROVEN. Every other failure keeps its own type,
    so a caller that cannot withdraw is never told the destination is gone.
    Subclasses ``PublishError`` so existing handlers keep their 502 behaviour and
    only callers that need the distinction have to look for it.

    NO SITE RAISES THIS TODAY, deliberately, and the two branches that read it are
    therefore unreachable. The personal drive resolves its destination by TAG, so a
    lookup miss says the lookup failed -- the bucket and distribution may still be
    serving -- and treating that as proof would strand a copy on evidence no stronger
    than the substring match this type exists to replace. Proving absence needs a
    POSITIVE probe: ask the destination directly about the resource named in the
    publication (the bucket name is derived deterministically, so a direct 404 is
    proof where a tag miss is not) rather than asking a directory whether it can
    still find it. Until such a probe exists, nothing releases a handle except a
    confirmed withdrawal -- which is the safe side of the trade, because a record
    that will not clear is recoverable and a public copy with no handle is not.
    Keep the type: the distinction is real, the callers are already correct, and the
    missing piece is an oracle rather than a contract.
    """


class CapabilityNotSupportedError(PublishError):
    """Raised when a provider is asked for a facet it doesn't implement."""

    def __init__(self, capability: Capability | str = ""):
        # Use the stable enum value (not str(enum)) — mixin-enum str/format
        # formatting differs across Python versions (3.10 renders the value,
        # 3.11+ renders "Capability.NAME"), which would make the message and
        # any assertions on it version-dependent.
        cap_str = capability.value if isinstance(capability, Capability) else capability
        super().__init__(f"capability not supported: {cap_str}")
        self.capability = capability


# ── Comment value types (provider-neutral) ───────────────────────────────────


@dataclass
class CommentAnchor:
    """Portable anchor for comment positioning across providers."""

    quote: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    version_number: int | None = None
    line: int | None = None
    column: int | None = None


@dataclass
class RemoteComment:
    """Provider-neutral representation of a remote comment."""

    remote_id: str
    thread_id: str
    author: str
    body: str
    anchor: CommentAnchor | None = None
    parent_id: str | None = None
    status: str = "open"  # open | review | resolved
    deleted: bool = False
    is_agent: bool = False
    created_at: str = ""
    updated_at: str = ""


# ── Result types (provider-agnostic) ─────────────────────────────────────────


@dataclass
class PublishResult:
    """Outcome of an initial publish."""

    external_id: str  # destination's stable id
    view_url: str  # stable shareable URL
    version_number: int  # destination version number (usually 1)
    concurrency_token: str  # opaque token to pass to the next push (sha256)
    owner: str = ""  # destination-side owner alias ("shared by")
    # Non-empty when the publish SUCCEEDED but the link is not usable yet -- the content
    # is stored at the destination and this result's handle is valid, so the publication
    # must be recorded, but something the user needs to know stands between them and a
    # working link (e.g. a CDN rollout that has not finished). Raising instead would be
    # lossy: the caller stores the publication only on success, so an abort after the
    # content is already uploaded strands it with no withdrawal handle. Recorded as the
    # publication's ``notice`` -- NOT ``last_error``, because ``last_error`` is read as
    # failure by every consumer, so a success reported there renders the publish red and
    # withholds the URL.
    notice: str = ""
    # Machine-readable discriminator for ``notice``, so the frontend can pick per-case
    # copy instead of printing one fixed "still rolling out" string for every notice.
    # Exactly one of the allowed values -- ``"rolling_out"`` (a fresh CDN deploy that will
    # resolve shortly), ``"distribution_disabled"`` (the delivery network is off, whose
    # remedy is to re-enable it in the provider console, NOT to wait), ``"unknown"`` (a
    # notice with no recognised discriminator) -- or ``""`` when there is no notice. It
    # always moves with ``notice``: a result carrying ``notice`` text carries a code, and
    # an empty ``notice`` carries an empty code.
    notice_code: str = ""


@dataclass
class PushResult:
    """Outcome of a best-effort version push.

    ``error`` non-empty means the push failed; ``conflict`` distinguishes an
    optimistic-concurrency mismatch (someone changed the destination artifact
    out-of-band) from a generic failure. On success ``error`` is empty and
    ``version_number`` / ``concurrency_token`` carry the new state.
    """

    version_number: int = 0
    concurrency_token: str = ""
    conflict: bool = False
    error: str = ""


# ── Provider interface ───────────────────────────────────────────────────────


class PublishProvider(ABC):
    """A destination an artifact can be published/shared to.

    Implementations set the class attributes ``name`` (registry key) and
    ``install_hint`` (shown when ``available()`` is False).
    """

    name: str = ""
    #: Human-facing provider name for any user- or agent-facing string. Engine/UI
    #: messages MUST use this instead of a hardcoded vendor literal so the
    #: publishing surface stays vendor-neutral. Defaults to a generic phrase;
    #: each provider overrides it with its real name.
    display_name: str = "the publishing provider"
    install_hint: str = ""

    @abstractmethod
    def available(self) -> bool:
        """Cheap check that the destination's tooling is installed/launchable."""

    async def ensure_ready(self) -> bool:
        """Ensure the destination's tooling is installed and launchable,
        installing it if absent. Default: no install story — just report
        :meth:`available`. Providers with an automated install override this to
        self-install silently so a first publish completes with no manual setup.
        Returns ``True`` when ready.
        """
        return self.available()

    def reachable_for(self, *, external_id: str) -> bool:
        """Whether the account THIS publication is bound to can still be reached.

        :meth:`available` answers a destination-WIDE question -- is this kind of
        destination configured at all -- which is the right question when deciding
        whether to offer it for a new publish. Paths acting on an EXISTING
        publication need the narrower one, because a publication is bound to one
        specific account: a registry that still holds *an* account can be missing
        the one this artifact was published to, and asking the wide question there
        reports a destination as reachable when this artifact's own is gone. The
        withdrawal paths then attempt a call that cannot succeed and report its
        failure as retryable, so an artifact bound to a removed account can neither
        be withdrawn nor deleted -- it just keeps advising a retry that will never
        work.

        Default: the destination-wide answer, which is correct for any provider
        that binds nothing per publication.
        """
        return self.available()

    def installable(self) -> bool:
        """True when :meth:`ensure_ready` has a real automated install story,
        so the provider is usable even while :meth:`available` is ``False``
        (the first publish self-installs). Drives the share-panel picker: a
        not-yet-installed but installable provider is still offered instead of
        being hidden entirely. Default: ``False`` — only providers that
        override :meth:`ensure_ready` with a self-install should return
        ``True``.
        """
        return False

    @abstractmethod
    def view_url_for(self, external_id: str) -> str:
        """Fallback stable URL for an external id (used if publish omits one)."""

    @abstractmethod
    async def publish(
        self,
        *,
        file_path: str,
        content_type: str,
        title: str,
        summary: str,
        tags: list[str],
        visibility: str,
        shared_with: list[str],
    ) -> PublishResult:
        """Create a new destination artifact. Raises ``PublishError`` on failure."""

    @abstractmethod
    async def push_version(
        self, *, external_id: str, file_path: str, expected_token: str
    ) -> PushResult:
        """Push new bytes as a new version. Best-effort — returns a
        ``PushResult`` (never raises for upstream errors)."""

    @abstractmethod
    async def update_sharing(
        self, *, external_id: str, visibility: str, shared_with: list[str]
    ) -> None:
        """Change visibility / shared-with. Raises ``PublishError`` on failure."""

    @abstractmethod
    async def unpublish(self, *, external_id: str) -> None:
        """Delete from the destination. Raises ``PublishError`` on failure."""

    async def fetch_state(self, *, external_id: str) -> dict | None:
        """Return live sharing state ``{visibility, shared_with}`` from the
        destination, or ``None`` if the provider can't read it back.

        Used to reconcile sharing changes made out-of-band (directly in the
        destination's UI) so the dashboard reflects truth. Optional — the
        default returns ``None`` (no reconcile) so providers that can't read
        state back don't have to implement it. Best-effort: implementations
        must not raise.
        """
        return None

    async def serving_notice(self, *, external_id: str) -> tuple[str, str] | None:
        """Re-derive the CURRENT serving notice for an already-published id, as
        ``(notice_text, notice_code)`` -- or ``None`` when the provider cannot
        re-check.

        This is the read-back half of the publish-time notice: a publish records
        a ``notice`` (e.g. a CDN still rolling out) that later resolves on its
        own, but nothing on the happy path ever revisits it, so the stale banner
        would otherwise persist forever. The dashboard calls this to bring the
        record up to date; ``publish_sync.reprobe_notice`` clears the stored
        ``notice`` / ``notice_code`` only when this returns an EMPTY pair
        ``("", "")`` -- i.e. the provider re-checked and the condition has
        actually cleared. Never on a timer, never without checking.

        The two non-clearing outcomes are kept distinct so the re-probe never
        clears a notice it could not verify:

        - ``None`` -- the provider cannot re-check serving state (the default,
          and what an unavailable / unregistered provider yields). The re-probe
          leaves any stored notice exactly as it was.
        - ``(text, code)`` with a non-empty ``code`` -- the condition is still
          live (possibly a DIFFERENT one, e.g. rolling-out has finished but the
          distribution is now disabled); the re-probe refreshes the stored
          notice to match rather than clearing it.

        Best-effort: implementations must not raise.
        """
        return None

    async def fetch_content(self, *, external_id: str) -> dict | None:
        """Download the upstream artifact's current bytes + metadata, or
        ``None`` if unavailable / unreadable / too large.

        Returns a dict ``{content, content_type, title, owner, visibility,
        shared_with, tags, current_version, view_url, sha256}``. This is the
        read half of bidirectional sync: ``publish_sync.pull_upstream`` /
        ``clone_from_remote`` use it to pull an upstream-ahead version into a new
        local snapshot and to clone a remote artifact into the local store.
        Requires ``Capability.CONTENT_PULL``; the default returns ``None`` so
        providers that can't read content back don't have to implement it.
        Best-effort: implementations must not raise.
        """
        return None

    # ── capability negotiation ────────────────────────────────────────────

    def capabilities(self) -> set[Capability]:
        """Declare which facets this provider supports."""
        return {Capability.CONTENT_VERSIONS, Capability.SHARING}

    # ── self-describing descriptors (M0-remainder) ────────────────────────

    def kind_support(self, kind: str) -> KindSupport:
        """How well this provider hosts an artifact ``kind`` (design §1.3b).

        Default assumes a blob store that serves any bytes.
        """
        return KindSupport.NATIVE

    def sharing_model(self) -> SharingModel:
        """Shape of the sharing surface (design §2). Alias-principal default."""
        return SharingModel()

    def sync_model(self) -> SyncModel:
        """Authority × concurrency (design §1.4). MIRROR + token-guarded default."""
        return SyncModel()

    def discovery_model(self) -> DiscoveryModel:
        """Which discovery primitives the browse UI can offer (design §2.5)."""
        return DiscoveryModel()

    # ── discovery (optional — default returns None = unsupported) ─────────

    async def list_remote(
        self, *, scope: str = "mine", page_token: str | None = None
    ) -> dict | None:
        """List remote items for a discovery ``scope`` (mine/shared/public).

        Returns ``{"artifacts": list[RemoteListing-as-dict], "next_page_token":
        str | None}`` or ``None`` when the provider can't list for that scope.
        Powers the provider-routed browse UI. Best-effort: must not raise.
        """
        return None

    async def search_remote(self, *, query: str, page_token: str | None = None) -> dict | None:
        """Full-text search across all accessible remote items.

        Same return shape as :meth:`list_remote`. ``None`` when unsupported
        (only providers whose ``discovery_model().full_text_search`` is True
        implement it). Best-effort: must not raise.
        """
        return None

    # ── comments (optional — default raises) ──────────────────────────────

    async def fetch_comments(self, *, external_id: str) -> list[RemoteComment]:
        """Fetch all comments from the provider. Raises if unsupported."""
        raise CapabilityNotSupportedError(Capability.COMMENTS_READ)

    async def post_comment(
        self, *, external_id: str, body: str, anchor: CommentAnchor | None = None
    ) -> RemoteComment:
        """Post a new top-level comment. Raises if unsupported."""
        raise CapabilityNotSupportedError(Capability.COMMENTS_WRITE)

    async def reply_comment(
        self, *, external_id: str, parent_remote_id: str, body: str
    ) -> RemoteComment:
        """Reply to an existing thread. Raises if unsupported."""
        raise CapabilityNotSupportedError(Capability.COMMENTS_WRITE)

    async def mark_review(self, *, external_id: str, remote_id: str) -> None:
        """Advance a thread to REVIEW status. Raises if unsupported."""
        raise CapabilityNotSupportedError(Capability.COMMENTS_WRITE)

    async def delete_comment(self, *, external_id: str, remote_id: str) -> None:
        """Soft-delete a comment. Raises if unsupported."""
        raise CapabilityNotSupportedError(Capability.COMMENTS_WRITE)

    async def edit_comment(self, *, external_id: str, remote_id: str, body: str) -> None:
        """Edit an existing comment's body IN PLACE (preserving its remote id,
        thread position, and replies). Raises if unsupported — providers whose
        surface has no in-place edit primitive leave this at the default and the
        caller keeps the edit local-only."""
        raise CapabilityNotSupportedError(Capability.COMMENTS_EDIT)


# ── Registry ─────────────────────────────────────────────────────────────────

_FACTORIES: dict[str, Callable[[], PublishProvider]] = {}
_INSTANCES: dict[str, PublishProvider] = {}


def register_provider(name: str, factory: Callable[[], PublishProvider]) -> None:
    """Register a provider factory under ``name`` (idempotent)."""
    _FACTORIES[name] = factory


def get_provider(name: str = DEFAULT_PROVIDER) -> PublishProvider:
    """Return the (lazily-instantiated, cached) provider for ``name``.

    Raises ``PublishUnavailableError`` if no provider is registered under the
    name — this surfaces to the user as a 503 rather than a 500. In the public
    edition the registry is empty, so this always raises (no publish provider).
    """
    inst = _INSTANCES.get(name)
    if inst is not None:
        return inst
    factory = _FACTORIES.get(name)
    if factory is None:
        raise PublishUnavailableError(f"unknown publish provider: {name!r}")
    inst = factory()
    _INSTANCES[name] = inst
    return inst


def reset_providers() -> None:
    """Drop cached provider instances (test-only helper)."""
    _INSTANCES.clear()


def list_providers() -> list[PublishProvider]:
    """Return all registered providers (lazily instantiated)."""
    return [get_provider(name) for name in _FACTORIES]
