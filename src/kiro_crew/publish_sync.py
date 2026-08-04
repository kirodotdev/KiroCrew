"""Publish sync engine — provider-agnostic orchestration.

Combines the pure data layer (``artifacts.ArtifactStore`` — reads/writes the
``publication`` block) with a pluggable :class:`PublishProvider` (resolved via
``publication.provider``, the registry default provider). This is the only module
that touches both the store and a provider.

Responsibilities that live here (provider-agnostic):
- Rendering a KiroCrew artifact to ``(bytes-on-disk, content_type)`` — including
  wrapping ``widget`` artifacts in a standalone HTML document.
- The version-bump → push chokepoint and the 1:1 KiroCrew↔destination version
  invariant. Reverts mirror as a new version (never a pointer-rollback).
- Best-effort sync: a ``push_version`` failure NEVER fails the KiroCrew update;
  it's recorded in ``publication.last_error`` and surfaced to the UI.
- Optimistic concurrency: the provider's concurrency token is stored in
  ``publication.last_pushed_sha256`` and passed back on the next push; a
  mismatch is surfaced as a conflict, never force-pushed.

Security (design §12): the publish path must not log artifact bytes. Content is
written to a temp file and handed to the provider by path; the temp file is
removed in a ``finally``. Artifact content is deliberately NOT redacted here —
the user is intentionally sharing it — but it is never written to a log.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from datetime import datetime, timezone

# Concrete publish providers are NOT imported here. In the public edition no
# provider is registered (the registry stays empty → get_provider() raises
# PublishUnavailableError → 503); a companion edition registers its providers at
# boot via the platform CPP seam (PublishRegistry.register_publish_providers),
# not by module-import side-effect. So this orchestration stays provider-agnostic
# and imports only the neutral publish_provider registry + result/error types.
from kiro_crew.artifacts import Artifact, ArtifactPublication, ForkMetadata, get_default_store
from kiro_crew.publish_provider import (
    DEFAULT_PROVIDER,
    Capability,
    NotPublishedError,
    PublishError,
    PublishProvider,
    PublishUnavailableError,
    get_provider,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# kind -> (file extension, content-type). ``json`` uploads as .txt because
# .json is not a publishing-provider surface (design §6); other providers can accept
# the same neutral mapping.
_KIND_MAP: dict[str, tuple[str, str]] = {
    "widget": (".html", "text/html"),
    "html": (".html", "text/html"),
    "markdown": (".md", "text/markdown"),
    "svg": (".svg", "image/svg+xml"),
    "text": (".txt", "text/plain"),
    "json": (".txt", "text/plain"),
}

# Default light-theme CSS variables baked into the standalone widget document.
# Recipients view a fixed, self-contained render (not live against any
# dashboard theme). Mirrors KiroCrewWebsite/src/lib/widgetSrcdoc.ts.
_STANDALONE_THEME_VARS: dict[str, str] = {
    "--bg": "#ffffff",
    "--bg-elevated": "#f9fafb",
    "--bg-hover": "#f3f4f6",
    "--card": "#f7f7f8",
    "--card-fg": "#1a1a1a",
    "--text": "#1a1a1a",
    "--text-strong": "#000000",
    "--muted": "#6b7280",
    "--muted-strong": "#4b5563",
    "--border": "#e5e7eb",
    "--border-strong": "#d1d5db",
    "--accent": "#d97706",
    "--accent-hover": "#b45309",
    "--accent-subtle": "#fef3c7",
    "--ok": "#16a34a",
    "--ok-subtle": "#dcfce7",
    "--warn": "#d97706",
    "--warn-subtle": "#fef3c7",
    "--danger": "#dc2626",
    "--danger-subtle": "#fee2e2",
    "--info": "#2563eb",
}

# CSP for the published standalone widget document (wrap_widget_html), rendered
# in a null-origin sandboxed iframe. Accepted risk:
# 'unsafe-eval' is required by the Tailwind Play CDN
# (cdn.tailwindcss.com), a client-side JIT compiler that calls new Function();
# 'unsafe-inline' is required because widget bodies are LLM-authored inline
# <script>/<style> that cannot be nonce/hash-pinned at author time. Residual
# XSS risk is contained by the sandbox — null origin, default-src 'none',
# connect-src 'none', form-action 'none', base-uri 'none' — so LLM content
# cannot reach cookies, storage, the parent DOM, or the network. Removing these
# tokens would break Tailwind-styled widgets with no material security gain.
_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com "
    "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
    "style-src 'unsafe-inline' https://cdn.tailwindcss.com; "
    "img-src data: blob:; font-src data:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none';"
)
_BASE_BODY_CSS = (
    "body { margin: 0; padding: 16px; font-family: -apple-system, "
    "BlinkMacSystemFont, 'Segoe UI', sans-serif; }"
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_provider(name: str) -> PublishProvider:
    return get_provider(name or DEFAULT_PROVIDER)


def _collab_mode_for(provider: PublishProvider) -> str:
    """Coarse sync authority for a provider's publications: ``"live"`` for a
    remote-authority CRDT backend, else ``"mirror"``. Read from the
    provider's ``sync_model()`` so the value is declared, not special-cased."""
    try:
        return provider.sync_model().collab_mode
    except Exception:  # pragma: no cover — defensive
        return "mirror"


async def _live_remote_hash(provider: PublishProvider, external_id: str) -> str:
    """sha256 of a LIVE provider's CURRENT remote body (a live CRDT provider canonicalizes
    markdown on write, so we hash the read-back form). Used as the drift
    baseline for a ``collab_mode="live"`` publication — compared remote-vs-remote
    so neither the provider's reformatting nor its view-triggered ``snapshot_seq``
    bumps register as false drift. Empty string on any failure; an empty
    baseline means ``upstream_status`` stays quiet rather than asserting drift.
    """
    try:
        content = await provider.fetch_content(external_id=external_id)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("live remote hash: fetch_content failed for %s: %s", external_id, exc)
        return ""
    if not isinstance(content, dict):
        return ""
    body = content.get("content")
    if not isinstance(body, str):
        return ""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


#: Sentinel comments delimiting a widget's inner fragment inside the wrapped
#: standalone document, so ``unwrap_widget_html`` can recover the exact inner
#: HTML after a publish -> pull round-trip. Provider-side body injection (e.g.
#: the publishing provider's anchor interceptor) that lands outside these markers is
#: excluded, keeping the recovered fragment clean across repeated round-trips.
_WIDGET_BODY_START = "<!--mc:widget-body-->"
_WIDGET_BODY_END = "<!--/mc:widget-body-->"


def wrap_widget_html(inner_html: str) -> str:
    """Wrap a widget's inner HTML in a self-contained standalone document.

    Mirrors the frontend ``buildSrcdoc`` (widgetSrcdoc.ts) for a fixed
    light-theme render. The publishing provider auto-injects ``<base
    target="_blank">`` and a same-page anchor interceptor on upload, so we do
    NOT add those here (design §6.1). No height reporter — that's only for the
    dashboard iframe.

    The inner fragment is delimited by sentinel comments so a pulled-back copy
    can be unwrapped to the exact inner HTML (``unwrap_widget_html``), keeping a
    round-tripped widget a *widget* — still inline-embeddable in chat — rather
    than degrading it to a full-document ``html`` artifact. Idempotent: content
    that is already a standalone document (starts with ``<!DOCTYPE``) is
    returned unchanged so it is never double-wrapped.
    """
    if inner_html.lstrip()[:9].lower() == "<!doctype":
        return inner_html  # already a standalone document — don't double-wrap
    theme_css = ";".join(f"{k}:{v}" for k, v in _STANDALONE_THEME_VARS.items())
    style = (
        f"{_BASE_BODY_CSS} "
        f":root{{{theme_css};color-scheme:light}}"
        "body{background:var(--bg);color:var(--text)}"
    )
    return (
        "<!DOCTYPE html>\n<html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
        '<script src="https://cdn.tailwindcss.com"></script>'
        "<script>tailwind.config={darkMode:'class'}</script>"
        f"<style>{style}</style>"
        '</head><body class="light">'
        f"{_WIDGET_BODY_START}{inner_html}{_WIDGET_BODY_END}"
        "</body></html>"
    )


def unwrap_widget_html(html: str) -> str | None:
    """Recover a widget's inner fragment from a document produced by
    ``wrap_widget_html``, or ``None`` when the sentinels are absent.

    Used on pull: a sentinel-bearing document was a KiroCrew widget, so we strip
    the wrapper and keep ``kind="widget"`` (preserving inline-chat embedding).
    A document without the sentinels (a foreign / pre-sentinel artifact) returns
    ``None`` and the caller keeps it as ``html``. Sentinel-delimited extraction
    excludes any provider-side body injection that lands outside the markers.
    """
    if not html:
        return None
    start = html.find(_WIDGET_BODY_START)
    if start == -1:
        return None
    inner_start = start + len(_WIDGET_BODY_START)
    end = html.find(_WIDGET_BODY_END, inner_start)
    if end == -1:
        return None
    return html[inner_start:end]


def _redact_untrusted(text: str, source: str) -> str:  # noqa: ARG001
    """Redact credential patterns / exfiltration URLs from artifact text before
    it reaches an external surface (a publish provider). Per the
    security-controls rule.

    Redaction is UNCONDITIONAL — the ``source`` label is no longer trusted as a
    bypass (kept for call-site readability). ``source`` is set once at create
    and is NOT re-derived when an agent later ``update``\\s the artifact's
    content, so a ``manual`` artifact can hold agent/LLM-authored bytes by the
    time it is published; exempting it would let that content reach a provider
    unscanned. Redaction of the rare genuinely-user-typed credential/URL is the
    safe direction (a false redaction is far cheaper than an exfiltration miss),
    so every source is scanned.
    """
    if not text:
        return text
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _render_content(art: Artifact) -> tuple[str, str]:
    """Return ``(rendered_text, content_type)`` for an artifact's current
    content, applying the widget standalone-HTML wrap where needed.
    """
    _, content_type = _KIND_MAP.get(art.kind, (".txt", "text/plain"))
    # Redact the raw content BEFORE the widget HTML wrap so the wrapper's own
    # trusted CDN URLs aren't touched.
    content = _redact_untrusted(art.content or "", art.source)
    if art.kind == "widget":
        content = wrap_widget_html(content)
    return content, content_type


def _ext_for(art: Artifact) -> str:
    ext, _ = _KIND_MAP.get(art.kind, (".txt", "text/plain"))
    return ext


def _write_tempfile(text: str, ext: str) -> str:
    fd, path = tempfile.mkstemp(suffix=ext, prefix="kc-artifact-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _safe_unlink(path: str) -> None:
    """Best-effort unlink (never raises) — used off-loop in publish/push finally."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _publication_summary(pub: ArtifactPublication) -> dict[str, object]:
    """Serialize a publication block for an HTTP/MCP response."""
    return {
        "provider": pub.provider,
        "artifact_id": pub.artifact_id,
        "view_url": pub.view_url,
        "visibility": pub.visibility,
        "shared_with": list(pub.shared_with),
        "auto_sync": pub.auto_sync,
        "collab_mode": pub.collab_mode,
        "last_synced_kirocrew_version": pub.last_synced_kirocrew_version,
        "version_map": dict(pub.version_map),
        "published_at": pub.published_at,
        "published_by": pub.published_by,
        "last_error": pub.last_error,
    }


# ── Public API ──────────────────────────────────────────────────────────────


async def publish(
    slug: str,
    *,
    visibility: str = "PRIVATE",
    shared_with: list[str] | None = None,
    actor: str = "user",
    provider_name: str = DEFAULT_PROVIDER,
) -> dict[str, object]:
    """Publish an artifact (or re-publish if already published).

    Returns the publication summary dict. Raises ``PublishError`` subclasses on
    failure (the handler maps these to HTTP status codes). ``ArtifactNotFound``
    from the store propagates for a 404.
    """
    store = get_default_store()
    # store.get reads current.html (up to MAX_CONTENT_BYTES = 25 MiB) + meta.json
    # synchronously; offload it off the asyncio gateway loop (no-blocking-call).
    art = await asyncio.to_thread(store.get, slug)  # ArtifactNotFoundError -> 404
    shared_with = shared_with or []

    # Ensure the destination tooling is installed before either branch. For a
    # first publish this silently self-installs the provider's native MCP (and
    # migrates legacy users) so the publish completes with no manual setup;
    # only a genuine install failure surfaces the manual hint as a 503.
    provider = _resolve_provider(
        art.publication.provider if art.publication is not None else provider_name
    )
    if not await provider.ensure_ready():
        raise PublishUnavailableError(provider.install_hint)

    # Idempotency: an already-published artifact re-publishes as a version push
    # plus a sharing update, reusing the same destination id/URL — never a
    # second artifact. Use the artifact's existing provider.
    if art.publication is not None:
        await push_version(art, force=True)
        # push_version is best-effort and records failures in
        # publication.last_error; update_sharing then clears last_error. Capture
        # the push error first and restore it afterward so a content-sync
        # failure during re-publish isn't silently masked by a "success"
        # sharing response.
        refreshed = await asyncio.to_thread(store.get, slug)
        push_error = refreshed.publication.last_error if refreshed.publication else ""
        # Skip the sharing reconcile for providers whose sharing is not
        # programmable via the API (e.g. a live CRDT provider — web-UI-only). Re-publish is
        # then just a content push; the existing publication summary is returned.
        if Capability.SHARING in provider.capabilities():
            result = await update_sharing(slug, visibility=visibility, shared_with=shared_with)
        else:
            result = (
                _publication_summary(refreshed.publication)
                if refreshed.publication is not None
                else {}
            )
        if push_error:
            await asyncio.to_thread(lambda: store.update_publication(slug, last_error=push_error))
            result["last_error"] = push_error
        return result

    # Render + tempfile-write are CPU/IO on a ≤25 MiB body; offload off the
    # asyncio gateway loop so a large artifact never stalls chat turns / the
    # liveness heartbeat (no-blocking-call-on-event-loop).
    text, content_type = await asyncio.to_thread(_render_content, art)
    path = await asyncio.to_thread(_write_tempfile, text, _ext_for(art))
    try:
        res = await provider.publish(
            file_path=path,
            content_type=content_type,
            title=_redact_untrusted(art.name, art.source),
            summary=_redact_untrusted(art.description, art.source),
            tags=[_redact_untrusted(t, art.source) for t in art.tags],
            visibility=visibility,
            shared_with=shared_with,
        )
    finally:
        await asyncio.to_thread(_safe_unlink, path)

    pub = ArtifactPublication(
        provider=provider.name,
        artifact_id=res.external_id,
        view_url=res.view_url,
        visibility=visibility,
        shared_with=shared_with,
        auto_sync=True,
        collab_mode=_collab_mode_for(provider),
        last_pushed_sha256=res.concurrency_token,
        last_synced_kirocrew_version=art.version,
        version_map={str(art.version): res.version_number},
        published_at=_now_iso(),
        published_by=res.owner,
        last_error="",
    )
    if pub.collab_mode == "live":
        # Establish the drift baseline from the read-back remote body (a live CRDT provider
        # canonicalizes what we just sent), so upstream_status compares
        # remote-vs-remote from the first poll.
        pub.last_synced_remote_hash = await _live_remote_hash(provider, res.external_id)
    await asyncio.to_thread(store.set_publication, slug, pub)
    logger.info(
        "artifact published: slug=%s provider=%s external_id=%s visibility=%s actor=%s",
        slug,
        provider.name,
        res.external_id,
        visibility,
        actor,
    )
    return _publication_summary(pub)


async def push_version(art: Artifact, *, force: bool = False) -> None:
    """Push the artifact's current content as a new destination version.

    Best-effort: never raises for upstream failures. On a conflict or any
    provider failure, records ``publication.last_error`` and returns. Pushes
    exactly once per KiroCrew version (idempotent + the 1:1 invariant); a force
    push (used by re-publish) bypasses the version guard.
    """
    if art.publication is None or not art.publication.auto_sync:
        return
    pub = art.publication
    store = get_default_store()
    # Offload the ≤25 MiB current.html read off the event loop (no-blocking-call).
    fresh = await asyncio.to_thread(store.get, art.slug)
    # If the live content has unsaved working edits (live_dirty), the bytes we
    # are about to push do NOT match the latest numbered snapshot. Snapshot them
    # into a real local version FIRST, so the remote version this push creates is
    # backed by a local version whose content matches it. This preserves the
    # invariant: version_map[N] <-> vN.html content <-> remote version. Without
    # it, an overwrite / dirty push records version_map[<latest snapshot #>] ->
    # remote while that snapshot's stored bytes differ from what was pushed
    # (map/content drift), and the pushed content survives only in current.html,
    # never as a numbered version. Like the pull checkpoint, this goes straight
    # through the store (NOT the auto-pushing update endpoint), so it does not
    # itself trigger a nested push. The normal snapshot-then-push path is never
    # dirty at this point, so this branch is a no-op for it.
    if fresh.live_dirty:
        try:
            # ≤25 MiB read + snapshot-write — offload off the event loop, like the
            # 8 sibling store calls in this module (no-blocking-call-on-event-loop).
            fresh = await asyncio.to_thread(lambda: store.update(art.slug, snapshot=True))
        except Exception as exc:  # pragma: no cover — snapshot must not break push
            logger.warning("push_version: pre-push snapshot failed for %s: %s", art.slug, exc)
    # Push exactly once per KiroCrew version. If this version was already synced
    # (and not force-republishing), there's nothing to do. We deliberately do
    # NOT dedupe on content — the 1:1 mapping is intentional, and a provider's
    # stored bytes (e.g. the provider's HTML auto-injection) won't match a
    # locally-computed hash anyway.
    if not force and fresh.version == pub.last_synced_kirocrew_version:
        return

    provider = _resolve_provider(pub.provider)
    if not provider.available():
        await asyncio.to_thread(
            lambda: store.update_publication(art.slug, last_error=provider.install_hint)
        )
        return

    # Offload render + tempfile write/unlink off the event loop (≤25 MiB body).
    text, _ = await asyncio.to_thread(_render_content, fresh)
    path = await asyncio.to_thread(_write_tempfile, text, _ext_for(fresh))
    try:
        res = await provider.push_version(
            external_id=pub.artifact_id,
            file_path=path,
            expected_token=pub.last_pushed_sha256,
        )
    finally:
        await asyncio.to_thread(_safe_unlink, path)

    if res.error:
        # Redact provider-controlled error strings before persisting — they may
        # embed credentials or internal URLs from the remote's error response.
        safe_err = _redact_untrusted(str(res.error), "provider")
        if res.conflict:
            await asyncio.to_thread(
                lambda: store.update_publication(
                    art.slug,
                    last_error="conflict: destination artifact changed out-of-band",
                )
            )
        else:
            await asyncio.to_thread(
                lambda: store.update_publication(art.slug, last_error=f"sync failed: {safe_err}")
            )
        logger.warning("push_version error for %s: %s", art.slug, safe_err)
        return

    version_map = dict(pub.version_map)
    version_map[str(fresh.version)] = res.version_number
    extra_pub: dict[str, object] = {}
    if pub.collab_mode == "live":
        # Re-baseline drift to the remote body we just wrote (a live CRDT provider reformats
        # it), so the next upstream_status doesn't read our own push as drift.
        extra_pub["last_synced_remote_hash"] = await _live_remote_hash(provider, pub.artifact_id)
    await asyncio.to_thread(
        lambda: store.update_publication(
            art.slug,
            last_pushed_sha256=res.concurrency_token or pub.last_pushed_sha256,
            last_synced_kirocrew_version=fresh.version,
            version_map=version_map,
            last_error="",
            **extra_pub,
        )
    )
    logger.info(
        "artifact version synced: slug=%s provider=%s kirocrew_v=%s dest_v=%s",
        art.slug,
        pub.provider,
        fresh.version,
        res.version_number,
    )


async def push_version_by_slug(slug: str, *, force: bool = False) -> None:
    """Convenience wrapper: load the artifact and push if published."""
    art = await asyncio.to_thread(get_default_store().get, slug)
    if art.publication is not None and art.publication.auto_sync:
        await push_version(art, force=force)


async def update_sharing(
    slug: str, *, visibility: str, shared_with: list[str] | None = None
) -> dict[str, object]:
    """Update visibility + shared-with on an already-published artifact."""
    store = get_default_store()
    # Offload the ≤25 MiB current.html read + meta write off the event loop.
    art = await asyncio.to_thread(store.get, slug)
    if art.publication is None:
        raise NotPublishedError(f"artifact {slug} is not published")
    shared_with = shared_with or []
    provider = _resolve_provider(art.publication.provider)
    if not await provider.ensure_ready():
        raise PublishUnavailableError(provider.install_hint)
    await provider.update_sharing(
        external_id=art.publication.artifact_id,
        visibility=visibility,
        shared_with=shared_with,
    )
    updated = await asyncio.to_thread(
        lambda: store.update_publication(
            slug, visibility=visibility, shared_with=shared_with, last_error=""
        )
    )
    assert updated.publication is not None  # just set above
    return _publication_summary(updated.publication)


async def unshare(slug: str) -> dict[str, object]:
    """Revoke sharing: set visibility back to PRIVATE and clear aliases."""
    return await update_sharing(slug, visibility="PRIVATE", shared_with=[])


async def unpublish(slug: str) -> None:
    """Delete from the destination and clear the local publication block.

    Best-effort on the destination delete (logs on failure) but always clears
    the local publication. Raises ``NotPublishedError`` only when the artifact
    isn't published.
    """
    store = get_default_store()
    art = await asyncio.to_thread(store.get, slug)
    if art.publication is None:
        raise NotPublishedError(f"artifact {slug} is not published")
    provider = _resolve_provider(art.publication.provider)
    if provider.available():
        try:
            await provider.unpublish(external_id=art.publication.artifact_id)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("unpublish: destination delete failed for %s: %s", slug, exc)
    await asyncio.to_thread(store.clear_publication, slug)
    logger.info("artifact unpublished: slug=%s", slug)


# Sentinel prefix for the out-of-band-drift sync note, so refresh can both set
# and later clear it without clobbering a genuine push-conflict message.
_DRIFT_PREFIX = "The remote copy changed outside KiroCrew"


async def refresh_publication(slug: str) -> Artifact:
    """Reconcile the local publication's sharing state with the live
    destination (picks up visibility / shared-with changes made directly in the
    destination's UI). Best-effort — never raises; returns the (possibly
    updated) artifact.

    Deliberately does NOT touch the content concurrency token, so a genuine
    out-of-band *content* change still surfaces as a conflict on the next push
    rather than being silently clobbered.
    """
    store = get_default_store()
    art = await asyncio.to_thread(store.get, slug)
    pub = art.publication
    if pub is None:
        return art
    provider = _resolve_provider(pub.provider)
    if not provider.available():
        return art
    try:
        state = await provider.fetch_state(external_id=pub.artifact_id)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("refresh_publication failed for %s: %s", slug, exc)
        return art
    if not state:
        return art
    fields: dict = {}
    vis = state.get("visibility")
    if isinstance(vis, str) and vis in ("PRIVATE", "SHARED", "PUBLIC") and vis != pub.visibility:
        fields["visibility"] = vis
    shared = state.get("shared_with")
    if isinstance(shared, list):
        normalized = [str(a) for a in shared]
        if normalized != list(pub.shared_with):
            fields["shared_with"] = normalized
    # Out-of-band content/version drift: someone rolled the provider's live
    # pointer back to an older version, or edited the bytes directly. We do NOT
    # mirror it into KiroCrew's append-only history; instead surface a re-sync
    # prompt (the conflict banner's "Force re-sync" re-asserts KiroCrew's
    # current content as a new top version). KiroCrew stays authoritative.
    expected_dest_v = pub.version_map.get(str(pub.last_synced_kirocrew_version))
    cur_v = state.get("current_version")
    cur_sha = state.get("sha256")
    # Cloud STRICTLY ahead of our last sync is NOT drift-to-clobber: it's an
    # upstream-ahead edit (a collaborator with EDIT rights changed our cloud
    # copy) that the user can PULL into a new local snapshot — see
    # ``pull_upstream`` / ``upstream_status``. Only a NON-ahead mismatch (a
    # rollback to an older version, or an out-of-band edit at the SAME version
    # detected via sha) is genuine drift the force-re-sync banner addresses.
    cloud_ahead = isinstance(cur_v, int) and expected_dest_v is not None and cur_v > expected_dest_v
    drifted = not cloud_ahead and (
        (isinstance(cur_v, int) and expected_dest_v is not None and cur_v != expected_dest_v)
        or (
            isinstance(cur_sha, str)
            and bool(pub.last_pushed_sha256)
            and cur_sha != pub.last_pushed_sha256
        )
    )
    # A live CRDT doc has no version/sha drift to reconcile — the remote
    # owns the doc. Never surface a Force-resync banner for a LIVE publication.
    if pub.collab_mode == "live":
        drifted = False
    if drifted:
        cur_v_str = f"v{cur_v}" if isinstance(cur_v, int) else "an unknown version"
        expected_str = (
            f"v{expected_dest_v}" if expected_dest_v is not None else "an unknown version"
        )
        drift_msg = (
            f"{_DRIFT_PREFIX}: it is showing {cur_v_str} (KiroCrew published "
            f"{expected_str}). Force re-sync to re-publish KiroCrew's current version."
        )
        if pub.last_error != drift_msg:
            fields["last_error"] = drift_msg
    elif pub.last_error.startswith(_DRIFT_PREFIX):
        # A previously-flagged drift has been reconciled (e.g. a re-sync landed).
        fields["last_error"] = ""
    if not fields:
        return art
    logger.info("reconciled publication from destination: slug=%s fields=%s", slug, list(fields))
    return await asyncio.to_thread(lambda: store.update_publication(slug, **fields))


async def delete_for_artifact(art: Artifact) -> None:
    """Best-effort destination delete before a local artifact delete.

    Unlike ``unpublish`` this does NOT touch the local store (the artifact is
    about to be deleted entirely) and never raises.
    """
    if art.publication is None:
        return
    provider = _resolve_provider(art.publication.provider)
    if not provider.available():
        return
    try:
        await provider.unpublish(external_id=art.publication.artifact_id)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("delete_for_artifact failed for %s: %s", art.slug, exc)


# ── Bidirectional sync: pull + clone ─────────────────────────────────────────


def _kind_for_content_type(content_type: str) -> str:
    """Map an upstream content-type back to a KiroCrew artifact kind.

    Pulled/cloned artifacts arrive as *rendered* bytes — a widget's inner HTML
    is already wrapped in a standalone document, so HTML maps to ``html``, not
    ``widget`` (the inner-HTML form can't be recovered from the wrapped doc).
    Per-version ``kind`` recording (see ``ArtifactStore.update``) keeps a later
    revert to a pre-pull widget snapshot rendering as a widget.
    """
    ct = (content_type or "").lower()
    if "html" in ct:
        return "html"
    if "markdown" in ct:
        return "markdown"
    if "svg" in ct:
        return "svg"
    if "json" in ct:
        return "json"
    return "text"


# Content types we can safely pull into a local artifact as UTF-8 text. A binary
# upstream (image / PDF / Office doc) read as text would land as mojibake, so
# pull and clone refuse anything outside this allowlist and point the user back
# to the artifact registry.
_PULLABLE_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/html",
    "text/xml",
    "application/json",
    "application/xml",
    "image/svg+xml",
}


def _is_pullable_content_type(content_type: str) -> bool:
    """True when the upstream bytes are text we can render locally.

    Empty/unknown is treated as text (legacy publications stored no content-type).
    The text/* family and structured ``*+xml`` / ``*+json`` types are allowed;
    everything else (image/*, application/pdf, Office types, …) is refused so it
    is never read as UTF-8 and mangled.
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return True
    if ct in _PULLABLE_CONTENT_TYPES:
        return True
    return ct.startswith("text/") or ct.endswith("+xml") or ct.endswith("+json")


def _primary_source(art: Artifact) -> str | None:
    """Which upstream a default ('auto') pull/status reads from.

    Prefer my own cloud copy (``publication`` — the bidirectional collaborator
    case) when present, else the forked-from ``origin`` (``fork_metadata``). A
    forked-and-published artifact has BOTH; the publication is primary and the
    origin stays pullable via ``pull_upstream(source="origin")``.
    """
    if art.publication is not None:
        return "publication"
    if art.fork_metadata is not None:
        return "origin"
    return None


def _source_target(art: Artifact, source: str):
    """Resolve a source label → (provider_name, external_id, expected_cloud_v,
    is_publication), or ``None`` when that source isn't tracked."""
    if source == "publication" and art.publication is not None:
        pub = art.publication
        expected = pub.version_map.get(str(pub.last_synced_kirocrew_version))
        return (pub.provider, pub.artifact_id, expected, True)
    if source == "origin" and art.fork_metadata is not None:
        fm = art.fork_metadata
        # Resolve the origin against the provider the fork came from; legacy
        # records (pre multi-provider) carry no provider and fall back to the
        # default, preserving single-provider behavior.
        return (
            fm.upstream_provider or DEFAULT_PROVIDER,
            fm.upstream_artifact_id,
            fm.upstream_version,
            False,
        )
    return None


async def upstream_status(slug: str) -> dict[str, object]:
    """Cheap (metadata-only) check of whether a tracked upstream has changes to
    pull. Reports the PRIMARY source (my publication if present, else the fork
    origin). Best-effort: a provider/network failure reports ``tracked`` with
    the flags defaulted to False so opening an artifact never blocks or errors.
    """
    store = get_default_store()
    art = await asyncio.to_thread(store.get, slug)
    base: dict[str, object] = {
        "tracked": art.publication is not None or art.fork_metadata is not None,
        "source": _primary_source(art),
        "upstream_ahead": False,
        "local_ahead": False,
        "live_dirty": bool(art.live_dirty),
        "conflict": False,
        "cloud_version": None,
        "local_version": art.version,
    }
    source = base["source"]
    # File-backed artifacts participate fully: drift detection reads only
    # remote metadata/content (no local file access), and ``pull_upstream``
    # writes pulled content through to the backing file (the old
    # blanket suppression made a published working file silently push-only).
    if not isinstance(source, str):
        return base
    target = _source_target(art, source)
    if target is None:
        return base
    provider_name, ext_id, expected_v, is_pub = target
    try:
        # Best-effort contract: resolving the provider must not raise out of
        # here. In the public edition (empty registry) — or when a tracked
        # publication/fork_metadata survives from a companion edition (snapshot
        # restore) with no matching provider — ``_resolve_provider`` raises
        # ``PublishUnavailableError``; degrade to the base tracked payload so the
        # detail page's drift banner never turns a status probe into an error.
        provider = _resolve_provider(provider_name)
    except PublishError as exc:
        logger.warning("upstream_status: provider unavailable for %s: %s", slug, exc)
        return base
    if not provider.available():
        return base
    try:
        state = await provider.fetch_state(external_id=ext_id)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("upstream_status: fetch_state failed for %s: %s", slug, exc)
        return base
    cur_v = state.get("current_version") if state else None
    # LIVE (CRDT): snapshot_seq bumps on mere viewing (presence/reveal), so a
    # seq-based "upstream_ahead" produces phantom pull banners. Detect drift by
    # hashing the remote body and comparing to the baseline recorded at last
    # sync — remote-vs-remote, immune to both the provider's markdown canonicalization
    # and its view-triggered seq bumps. A CRDT never conflicts (edits merge).
    if is_pub and art.publication is not None and art.publication.collab_mode == "live":
        pub = art.publication
        remote_hash = await _live_remote_hash(provider, ext_id)
        baseline = pub.last_synced_remote_hash
        upstream_ahead = bool(baseline) and bool(remote_hash) and remote_hash != baseline
        base.update(
            {
                "upstream_ahead": bool(upstream_ahead),
                "local_ahead": bool(art.version > pub.last_synced_kirocrew_version),
                "conflict": False,
                "cloud_version": cur_v if isinstance(cur_v, int) else None,
            }
        )
        return base
    upstream_ahead = isinstance(cur_v, int) and expected_v is not None and cur_v > expected_v
    local_ahead = (
        is_pub
        and art.publication is not None
        and art.version > art.publication.last_synced_kirocrew_version
    )
    base.update(
        {
            "upstream_ahead": bool(upstream_ahead),
            "local_ahead": bool(local_ahead),
            "conflict": bool(upstream_ahead and local_ahead),
            "cloud_version": cur_v if isinstance(cur_v, int) else None,
        }
    )
    return base


async def pull_upstream(
    slug: str, *, source: str = "auto", actor: str = "user"
) -> dict[str, object]:
    """Pull an upstream-ahead version into a NEW local append-only snapshot.

    The unified pull half of bidirectional sync — works for a fork's ``origin``
    lineage AND my own ``publication`` (a collaborator edited my cloud copy).
    ``source`` selects which tracked upstream to pull (``auto`` = publication if
    present else origin). Rules:

    - Upstream strictly ahead → pull into a new local snapshot (prior versions
      stay in history). Never auto-republishes the pulled bytes
      (content-laundering law); an owned publication's NEXT explicit snapshot
      pushes normally.
    - NEVER refuses: unsaved working edits (``live_dirty``) are snapshotted as a
      LOCAL-ONLY, push-suppressed checkpoint FIRST (preserved in history), then
      the upstream lands as the next version. The checkpoint goes straight
      through the store, not the auto-pushing update endpoint, so preserving your
      edits can't upload them over the upstream being pulled (the auto-sync
      race). Pull never pushes; your next explicit snapshot does.
    - Non-text content types are refused up front (would be UTF-8 mojibake).
    - File-backed (``source_path``) artifacts participate fully: the pulled
      content is written through to the backing file (under the store's
      ``resolve()`` + ``is_sensitive_path`` write guards), so upstream edits
      reach the working file. The file's pre-pull bytes are preserved — as the
      ``live_dirty`` checkpoint when they drifted from the last snapshot, else
      in the existing snapshot history.

    Best-effort: never raises for provider failures.
    """
    store = get_default_store()
    art = await asyncio.to_thread(store.get, slug)
    if art.publication is None and art.fork_metadata is None:
        return {"pulled": False, "reason": "not tracked", "tracked": False}
    if source == "auto":
        source = _primary_source(art) or "publication"
    target = _source_target(art, source)
    if target is None:
        return {"pulled": False, "reason": f"no {source} upstream", "tracked": True}
    provider_name, ext_id, expected_v, is_pub = target
    provider = _resolve_provider(provider_name)
    if not provider.available():
        return {"pulled": False, "reason": "provider unavailable", "tracked": True}
    try:
        state = await provider.fetch_state(external_id=ext_id)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("pull_upstream: fetch_state failed for %s: %s", slug, exc)
        return {"pulled": False, "reason": "fetch_state failed", "tracked": True}
    cur_v = state.get("current_version") if state else None
    upstream_ahead = isinstance(cur_v, int) and expected_v is not None and cur_v > expected_v
    if not upstream_ahead:
        return {"pulled": False, "reason": "up to date", "source": source, "cloud_version": cur_v}
    # No hard conflict — we NEVER refuse a pull. Local edits are append-only
    # history, so pulling can't destroy them; it just adds a newer version on
    # top. If the live content has unsaved working edits (live_dirty), snapshot
    # them FIRST as a local-only checkpoint so they land in history, THEN pull
    # the upstream as the next version.
    #
    # This checkpoint goes straight through the store — NOT the auto-pushing
    # update endpoint — so it is push-suppressed even when auto_sync is on. That
    # closes the auto-sync race: snapshotting your working edits to preserve them
    # must never upload them over the very upstream you're about to pull. Pull
    # never pushes; your NEXT explicit snapshot (the reconciled result) does.
    preserved_version: int | None = None
    if art.live_dirty:
        try:
            checkpoint = await asyncio.to_thread(
                lambda: store.update(slug, snapshot=True, actor=actor)
            )
            preserved_version = checkpoint.version
        except Exception as exc:  # pragma: no cover — checkpoint must not 500
            logger.warning("pull_upstream: checkpoint failed for %s: %s", slug, exc)
            return {"pulled": False, "reason": "checkpoint failed", "source": source}
    pulled = await provider.fetch_content(external_id=ext_id)
    if not pulled or not pulled.get("content"):
        return {"pulled": False, "reason": "fetch_content unavailable", "source": source}
    # Content-type allowlist: pulled bytes are read as UTF-8 text, so a binary
    # upstream would arrive as mojibake. Refuse non-text up front (open it in
    # the publishing provider instead) rather than writing garbage into a new local version.
    if not _is_pullable_content_type(str(pulled.get("content_type") or "")):
        return {
            "pulled": False,
            "reason": f"unsupported content type for local pull — open it in {provider.display_name}",
            "source": source,
            "content_type": pulled.get("content_type"),
            "preserved_version": preserved_version,
        }
    # A widget published remotely is stored as a wrapped standalone document.
    # On pull we recover the original inner fragment from the wrapper's
    # sentinels: success keeps it a WIDGET (still inline-embeddable in chat, no
    # regression); failure (a foreign or pre-sentinel document) degrades it to
    # html, since keeping kind=="widget" on a full document would double-wrap it
    # on the next push. Non-widget kinds round-trip unchanged (pull_kind None).
    pulled_content = str(pulled["content"])
    pull_kind: str | None = None
    if art.kind == "widget":
        inner = unwrap_widget_html(pulled_content)
        if inner is not None:
            pulled_content = inner  # recovered fragment → stays a widget
        else:
            pull_kind = _kind_for_content_type(str(pulled.get("content_type") or ""))
    try:
        updated = await asyncio.to_thread(
            lambda: store.update(
                slug, content=pulled_content, kind=pull_kind, actor=actor, snapshot=True
            )
        )
    except Exception as exc:  # pragma: no cover — pull must not 500
        logger.warning("pull_upstream: write-back failed for %s: %s", slug, exc)
        return {"pulled": False, "reason": "write-back failed", "source": source}
    pcv = pulled.get("current_version")
    cloud_dest_v = pcv if isinstance(pcv, int) else (cur_v if isinstance(cur_v, int) else 0)
    if is_pub and updated.publication is not None:
        upub = updated.publication
        version_map = dict(upub.version_map)
        version_map[str(updated.version)] = cloud_dest_v
        extra_pull: dict[str, object] = {}
        if upub.collab_mode == "live":
            extra_pull["last_synced_remote_hash"] = hashlib.sha256(
                str(pulled.get("content") or "").encode("utf-8")
            ).hexdigest()
        prev_sha = upub.last_pushed_sha256
        await asyncio.to_thread(
            lambda: store.update_publication(
                slug,
                last_synced_kirocrew_version=updated.version,
                last_pushed_sha256=str(pulled.get("sha256") or prev_sha),
                version_map=version_map,
                last_error="",
                **extra_pull,
            )
        )
    elif not is_pub and updated.fork_metadata is not None:
        await asyncio.to_thread(
            lambda: store.update_fork_metadata(slug, upstream_version=cloud_dest_v)
        )
    logger.info(
        "pull_upstream: pulled cloud v%s into %s (local v%s, source=%s)",
        cur_v,
        slug,
        updated.version,
        source,
    )
    return {
        "pulled": True,
        "version": updated.version,
        "preserved_version": preserved_version,
        "source": source,
        "cloud_version": cur_v,
        "upstream_ahead": False,
    }


# Serializes clone so two concurrent clones of the same cloud artifact can't
# both pass the "not local yet" check and create duplicate local copies
# (find-then-create is otherwise a non-atomic check-then-act).
_clone_lock = asyncio.Lock()


async def clone_from_remote(artifact_id: str, *, provider_name: str = DEFAULT_PROVIDER) -> Artifact:
    """Clone a remote artifact into the local store as a bidirectional copy.

    "Clone locally": bring a remote artifact I can access down to this device.
    The local copy links to the SAME remote copy via a ``publication`` — I pull
    collaborators' edits down, and my snapshots push back up.

    ``auto_sync`` is the user's INTENT to keep the two copies in sync; a clone
    always turns it on (cloning is a deliberate two-way link), and it is never
    set or cleared by permission. Whether a given snapshot actually writes back
    is the publishing provider's call at push time: ``push_version`` attempts the
    push and the provider allows or denies it. So permission is evaluated live on
    every push — a grant or revoke over time is honoured automatically, with no
    permission verdict frozen at clone time. (A read of the remote's sharing can
    prove I *can* write, but never that I *can't*: edit rights may come via a
    group we can't resolve locally, so we never pre-disable a push from a read.)
    A denied push records a clear ``last_error`` and leaves the edits local;
    Fork is the path for a copy you intend to diverge from rather than sync with.
    Idempotent via ``find_by_artifact_id``.
    """
    store = get_default_store()
    existing = await asyncio.to_thread(
        lambda: store.find_by_artifact_id(artifact_id, provider=provider_name)
    )
    if existing is not None:
        return existing
    provider = _resolve_provider(provider_name)
    if not await provider.ensure_ready():
        raise PublishUnavailableError(provider.install_hint)
    pulled = await provider.fetch_content(external_id=artifact_id)
    if not pulled:
        raise PublishError(
            f"could not open artifact {artifact_id} — it may be too large to "
            f"open locally, or unavailable. Open it in {provider.display_name} instead."
        )
    # Content-type allowlist (same guard as pull): clone reads the bytes as
    # UTF-8 text, so a binary upstream would arrive as mojibake. Refuse non-text.
    if not _is_pullable_content_type(str(pulled.get("content_type") or "")):
        raise PublishError(
            f"artifact {artifact_id} is not a text artifact "
            f"(content-type {pulled.get('content_type')!r}); clone supports "
            f"HTML / markdown / text / SVG / JSON. Open it in {provider.display_name} instead."
        )
    owner = str(pulled.get("owner") or "")
    shared = [s for s in (pulled.get("shared_with") or []) if isinstance(s, str)]
    content = str(pulled.get("content") or "")
    kind = _kind_for_content_type(str(pulled.get("content_type") or ""))
    if kind == "html":
        inner = unwrap_widget_html(content)
        if inner is not None:
            content = inner
            kind = "widget"
    async with _clone_lock:
        existing = await asyncio.to_thread(
            lambda: store.find_by_artifact_id(artifact_id, provider=provider_name)
        )
        if existing is not None:
            return existing
        art = await asyncio.to_thread(
            lambda: store.create(
                name=str(pulled.get("title") or "Artifact"),
                content=content,
                kind=kind,
                source="import",
                description="",
                tags=[t for t in pulled.get("tags", []) if isinstance(t, str)],
            )
        )
        try:
            cloud_dest_v = int(pulled.get("current_version") or 1)
        except (TypeError, ValueError):
            cloud_dest_v = 1
        pub = ArtifactPublication(
            artifact_id=artifact_id,
            view_url=str(pulled.get("view_url") or ""),
            provider=provider.name,
            visibility=str(pulled.get("visibility") or "PRIVATE"),
            shared_with=shared,
            auto_sync=True,
            collab_mode=_collab_mode_for(provider),
            last_pushed_sha256=str(pulled.get("sha256") or ""),
            last_synced_kirocrew_version=art.version,
            version_map={str(art.version): cloud_dest_v},
            published_at=_now_iso(),
            published_by=owner,
            last_error="",
        )
        logger.info("clone_from_remote: %s as slug=%s", artifact_id, art.slug)
        if pub.collab_mode == "live":
            pub.last_synced_remote_hash = hashlib.sha256(
                str(pulled.get("content") or "").encode("utf-8")
            ).hexdigest()
        await asyncio.to_thread(store.set_publication, art.slug, pub)
        return await asyncio.to_thread(store.get, art.slug)


async def fork_from_remote(external_id: str, *, provider_name: str = DEFAULT_PROVIDER) -> Artifact:
    """Fork a remote artifact into the local store as an INDEPENDENT copy with
    pull-only lineage (``fork_metadata``).

    Provider-agnostic generalization of the original provider-specific fork handler.
    Unlike :func:`clone_from_remote`, a fork is NOT bound to the upstream for
    push — it is your own divergent artifact (``collab_mode`` stays the local
    default ``"mirror"``); ``pull_latest(source="origin")`` re-fetches the
    upstream as a pull-only update, and publishing it later mints a fresh
    ``publication``. Reads the upstream via ``fetch_content`` behind the same
    content-type allowlist + widget-unwrap as clone.
    """
    store = get_default_store()
    provider = _resolve_provider(provider_name)
    if not await provider.ensure_ready():
        raise PublishUnavailableError(provider.install_hint)
    pulled = await provider.fetch_content(external_id=external_id)
    if not pulled:
        raise PublishError(
            f"could not open artifact {external_id} — it may be too large to "
            f"open locally, or unavailable. Open it in {provider.display_name} instead."
        )
    if not _is_pullable_content_type(str(pulled.get("content_type") or "")):
        raise PublishError(
            f"artifact {external_id} is not a text artifact "
            f"(content-type {pulled.get('content_type')!r}); fork supports "
            f"HTML / markdown / text / SVG / JSON. Open it in {provider.display_name} instead."
        )
    content = str(pulled.get("content") or "")
    kind = _kind_for_content_type(str(pulled.get("content_type") or ""))
    if kind == "html":
        inner = unwrap_widget_html(content)
        if inner is not None:
            content = inner
            kind = "widget"
    owner = str(pulled.get("owner") or "")
    try:
        upstream_v = int(pulled.get("current_version") or 1)
    except (TypeError, ValueError):
        upstream_v = 1
    original_tags = [t for t in pulled.get("tags", []) if isinstance(t, str)]
    tags = original_tags + (["forked"] if "forked" not in original_tags else [])
    art = await asyncio.to_thread(
        lambda: store.create(
            name=str(pulled.get("title") or "Forked artifact"),
            content=content,
            kind=kind,
            source="import",
            description=(
                f"Forked from {owner or 'a teammate'}'s {provider.display_name} "
                f"artifact on {_now_iso()[:10]}"
            ),
            tags=tags,
        )
    )
    fm = ForkMetadata(
        upstream_artifact_id=external_id,
        upstream_url=str(pulled.get("view_url") or ""),
        upstream_owner=owner,
        upstream_version=upstream_v,
        forked_at=_now_iso(),
        upstream_provider=provider.name,
    )
    await asyncio.to_thread(store.set_fork_metadata, art.slug, fm)
    logger.info("fork_from_remote: %s (%s) as slug=%s", external_id, provider.name, art.slug)
    return await asyncio.to_thread(store.get, art.slug)


async def overwrite_upstream(slug: str) -> dict[str, object]:
    """Force the local content to become the remote's current version even when
    the remote moved ahead — WITHOUT pulling the remote's (possibly untrusted)
    bytes into the local store first.

    The provider's optimistic-concurrency guard normally rejects a push when the
    remote moved ahead. Overwrite resolves the remote's CURRENT token first, then
    force-pushes the local content with that token so the push lands as a NEW
    remote version carrying the local content. The superseded remote version
    (e.g. a collaborator's edit) STAYS in the remote's version history — the
    provider has no delete-version primitive — so local and remote histories are
    not byte-identical, but ``version_map`` keeps the local->remote mapping
    coherent. The remote bytes are never written into the local store.

    Best-effort: never raises for provider failures (records ``last_error``).
    """
    store = get_default_store()
    art = await asyncio.to_thread(store.get, slug)
    pub = art.publication
    if pub is None:
        return {
            "overwritten": False,
            "reason": "not a publication (only a push target can be overwritten)",
            "tracked": art.fork_metadata is not None,
        }
    if pub.collab_mode == "live":
        provider = _resolve_provider(pub.provider)
        if not provider.available():
            return {"overwritten": False, "reason": "provider unavailable", "tracked": True}
        await asyncio.to_thread(
            lambda: store.update_publication(slug, auto_sync=True, last_error="")
        )
        await push_version(await asyncio.to_thread(store.get, slug), force=True)
        refreshed = await asyncio.to_thread(store.get, slug)
        rpub = refreshed.publication
        if rpub is not None and rpub.last_error:
            return {"overwritten": False, "reason": rpub.last_error, "tracked": True}
        cloud_v = rpub.version_map.get(str(refreshed.version)) if rpub is not None else None
        logger.info("overwrite_upstream (live): pushed local v%s to %s", refreshed.version, slug)
        return {
            "overwritten": True,
            "local_version": refreshed.version,
            "cloud_version": cloud_v,
        }
    provider = _resolve_provider(pub.provider)
    if not provider.available():
        return {"overwritten": False, "reason": "provider unavailable", "tracked": True}
    try:
        state = await provider.fetch_state(external_id=pub.artifact_id)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("overwrite_upstream: fetch_state failed for %s: %s", slug, exc)
        return {"overwritten": False, "reason": "fetch_state failed", "tracked": True}
    cur_sha = state.get("sha256") if state else None
    await asyncio.to_thread(
        lambda: store.update_publication(
            slug,
            last_pushed_sha256=(
                str(cur_sha) if isinstance(cur_sha, str) and cur_sha else pub.last_pushed_sha256
            ),
            auto_sync=True,
            last_error="",
        )
    )
    await push_version(await asyncio.to_thread(store.get, slug), force=True)
    refreshed = await asyncio.to_thread(store.get, slug)
    rpub = refreshed.publication
    if rpub is not None and rpub.last_error:
        return {"overwritten": False, "reason": rpub.last_error, "tracked": True}
    cloud_v = rpub.version_map.get(str(refreshed.version)) if rpub is not None else None
    logger.info("overwrite_upstream: pushed local v%s over remote for %s", refreshed.version, slug)
    return {
        "overwritten": True,
        "local_version": refreshed.version,
        "cloud_version": cloud_v,
    }
