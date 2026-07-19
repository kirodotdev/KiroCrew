"""Publish-provider interface for sharing KiroCrew artifacts to external
destinations (Mesh-1880).

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

DEFAULT_PROVIDER = "artifactory"


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
