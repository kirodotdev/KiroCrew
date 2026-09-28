"""The registered-provider seam: how a downstream edition adds a source provider.

A plugin is consulted only after every built-in declined, and it is always
dispatched from inside the shared safety layers, never around them. The
registry here also answers the per-provider questions the chip scanner, the
chip label and transcript search ask of every provider, built-in or not.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from kiro_crew.dashboard.source_providers import LOGGER_NAME, contract, sanitize
from kiro_crew.dashboard.source_providers.contract import (
    ConfirmationRequired,
    SourceCapacityError,
    SourceProviderError,
    SourceProviderNotConfigured,
    SourceProviderPlugin,
    SourceRef,
)
from kiro_crew.history_search import register_search_ref_resolver as _register_search_ref_resolver
from kiro_crew.history_search import (
    reset_search_ref_resolver_for_tests as _reset_search_ref_resolver,
)

logger = logging.getLogger(LOGGER_NAME)


# --- Source-provider plugin seam --------------------------------------------
#
# The three built-in providers stay exactly as they are: their host checks run
# first in `parse_source_url`, their fetchers are dispatched by name, and none of
# the code below changes a byte of their behaviour. This registry is what lets a
# downstream edition add a FOURTH provider -- an internal code-review system --
# from its own composition root instead of shadowing the source-provider handler
# on every upstream sync.
#
# A plugin supplies only the two things it alone knows: how to recognize its URLs
# and how to fetch them. Everything that makes a provider read SAFE is shared and
# applies to a plugin identically, because the plugin is called from inside it:
#
#   * the full-payload and checks caches, their TTL, entry cap and byte cap;
#   * the direct-fetch admission reservations (so a plugin cannot outgrow the
#     gateway's concurrent-fetch memory ceiling);
#   * `_redact_provider_data` over every returned payload;
#   * `_MAX_PAYLOAD_BYTES` enforcement;
#   * owner-only gating and the SEL audit events on every API entry point;
#   * the coalescing of concurrent requests for one URL.
#
# A plugin therefore cannot opt out of redaction or the byte caps by construction
# -- it never sees the request, only a validated `SourceRef`.

# Chip labels are rendered in the sidebar and travel in the slots payload, so a
# plugin-supplied one is length-bounded. Generous next to `#123` / `PROJ-123`
# while ruling out a label that would blow up the payload.
_MAX_CHIP_LABEL_LENGTH = 64

# A provider id is embedded in payloads and compared across the frontend
# boundary, so it matches the frontend's `PROVIDER_ID_RE` exactly.
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Ids the core owns. A plugin may not claim one: `parse_source_url` checks the
# built-in hosts first, so a shadowing plugin would be dead for parsing yet live
# for fetching -- two layers disagreeing about one provider.
_BUILTIN_PROVIDER_IDS = frozenset({"github", "gitlab", "jira"})

_SOURCE_PROVIDER_PLUGINS: dict[str, SourceProviderPlugin] = {}


def register_source_provider(plugin: SourceProviderPlugin) -> None:
    """Register a source provider. Call once, at gateway start-up.

    Refuses a built-in id, a duplicate, a malformed id, and a plugin missing a
    required method -- loudly, with a ``ValueError``, because a registration that
    silently did nothing would leave the frontend descriptor live and every URL it
    claims answered with a 400 nobody can explain.
    """
    provider_id = getattr(plugin, "id", None)
    if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.match(provider_id):
        raise ValueError(f"source provider id {provider_id!r} must match {_PROVIDER_ID_RE.pattern}")
    if provider_id in _BUILTIN_PROVIDER_IDS:
        raise ValueError(f"source provider id {provider_id!r} is a built-in and cannot be replaced")
    if provider_id in _SOURCE_PROVIDER_PLUGINS:
        raise ValueError(f"source provider {provider_id!r} is already registered")
    for method in ("parse", "fetch_full", "fetch_checks", "setup_message"):
        if not callable(getattr(plugin, method, None)):
            raise ValueError(f"source provider {provider_id!r} is missing {method}()")
    _SOURCE_PROVIDER_PLUGINS[provider_id] = plugin
    # Publish the transcript-search seam DOWNWARD into core (dashboard -> core,
    # the allowed direction; core never reaches up into this module). Done here,
    # at registration, rather than from a route handler: `parse_search_query` is
    # also reached from paths that serve no HTTP -- the Discord title-only resume
    # gate and the `kirocrew memory search` CLI -- and a process that never ran a
    # dashboard route would then answer the SAME query differently, a divergence
    # that presents as flakiness rather than as a missing registration. Idempotent
    # by identity, so registering several providers consults one collector.
    _register_search_ref_resolver(source_search_ref)
    logger.info("registered source provider %s", provider_id)


def registered_source_provider(provider_id: str) -> SourceProviderPlugin | None:
    """The plugin owning a provider id, or None for a built-in / unknown one."""
    return _SOURCE_PROVIDER_PLUGINS.get(provider_id)


def reset_source_providers_for_tests() -> None:
    """Drop every registration. Test-only: the registry is module state."""
    _SOURCE_PROVIDER_PLUGINS.clear()
    # The search seam is module state in core, published from here, so reset it
    # with the registry it serves — otherwise a stale collector outlives the
    # plugins it reads and a later test observes a registered resolver it never
    # asked for.
    _reset_search_ref_resolver()


def source_search_ref(token: str) -> tuple[str, Sequence[str]] | None:
    """Spellings of the provider item a query *token* names, or None.

    The transcript search recognizes the built-in forge shapes itself (``#123``,
    ``pull/123``, a PR URL) and expands them to every spelling of the same item,
    so whichever form a transcript used is found. A registered provider whose ids
    look like ``REV-987654321`` matches none of those shapes, so WITHOUT this its
    ids degrade to plain literal needles: a query finds only the exact string it
    typed, and never a transcript that cited the same review by URL.

    A plugin contributes spellings through the optional ``search_ref()`` hook.

    The FIRST plugin to ANSWER wins, for every token shape. Several plugins may be
    registered, so the loop asks each in turn until one answers -- but nothing here
    adjudicates BETWEEN two real answers: two registrants holding a real item at the
    SAME token cannot arise in this repo, which registers no provider at all, so
    merging them would ship surface no code path can reach. It is additive if a
    second registrant appears.

    Nothing here judges an answer's SHAPE. Skipping a malformed one would only
    matter so a LATER plugin could still be asked, which is the same two-registrant
    scenario the merge above is declined for, so the collector hands the first
    answer through and lets the one normalizer decide what it is. A RAISE is
    different: it costs no ``alts`` to contain and one broken edition must not hide
    every later provider's items, so it is caught per provider here.

    Shape belongs to the search module's ``_provider_search_ref``, the single
    normalizer: casefolding, the dedup, the spelling cap and every shape check. Its
    guard also wraps this whole collector, because it IS the resolver core calls.
    """
    for plugin in _SOURCE_PROVIDER_PLUGINS.values():
        hook = getattr(plugin, "search_ref", None)
        if not callable(hook):
            continue
        try:
            found = hook(token)
        except Exception:
            # One broken edition must not hide every later provider's items, so the
            # raise is contained HERE as well as in the normalizer's own guard.
            logger.debug("source provider %s search_ref failed", plugin.id, exc_info=True)
            continue
        if found is None:
            continue
        return found
    return None


def source_link_path_markers() -> tuple[str, ...]:
    """Path substrings that make a URL worth handing to :func:`parse_source_url`.

    The sidebar chip scanner walks raw message text and cannot afford to parse
    every ``https://`` token it finds, so it prefilters on the built-in path
    markers. A registered provider whose URLs look like ``/reviews/CR-123``
    matches none of them, so WITHOUT this its chips would never appear -- the
    parser is never reached, and nothing reports why.

    A plugin contributes markers through the optional ``path_markers()`` hook.
    Bounded per plugin and validated, since a marker of ``"/"`` would defeat the
    prefilter it exists to be.
    """
    markers = ["/pull/", "/merge_requests/", "/issues/", "/browse/"]
    for plugin in _SOURCE_PROVIDER_PLUGINS.values():
        hook = getattr(plugin, "path_markers", None)
        if not callable(hook):
            continue
        try:
            extra = hook()
        except Exception:
            logger.debug("source provider %s path_markers failed", plugin.id, exc_info=True)
            continue
        if isinstance(extra, str) or not isinstance(extra, Iterable):
            continue
        for marker in itertools.islice(extra, _MAX_PLUGIN_PATH_MARKERS):
            # At least two characters beyond the leading slash: a bare "/" (or a
            # one-character marker) would admit essentially every URL and turn
            # the prefilter into a full parse of the whole transcript. The upper
            # bound keeps a runaway string out of the scanner's per-candidate
            # substring checks; no realistic path marker approaches it. islice
            # rather than list()[:n] so a generator-returning hook is consumed
            # only up to the cap instead of exhausted before slicing.
            if (
                isinstance(marker, str)
                and marker.startswith("/")
                and 3 <= len(marker) <= _MAX_PLUGIN_PATH_MARKER_LEN
            ):
                markers.append(marker)
    return tuple(dict.fromkeys(markers))


# Per-plugin ceiling on contributed prefilter markers -- enough for a provider
# with several URL shapes, small enough that the scanner's per-candidate cost
# stays bounded no matter how many providers register.
_MAX_PLUGIN_PATH_MARKERS = 8

# Ceiling on one marker's length: markers are substring-searched against every
# URL candidate in a transcript, so their size is part of the scanner's cost.
_MAX_PLUGIN_PATH_MARKER_LEN = 64


def source_ref_label(ref: SourceRef) -> str:
    """The provider's own short name for this object, as a chip renders it.

    Every provider names its objects differently -- GitHub writes ``#123``,
    GitLab writes ``!123`` for a merge request but ``#123`` for an issue, and
    Jira has no bare number at all: ``PROJ-123`` is the whole identifier, the
    number alone is meaningless outside its project.

    This belongs on the side that parsed the URL. The alternative -- shipping
    the components and letting the renderer reassemble them -- means the
    renderer has to know each provider's convention, which is knowledge it can
    only have about providers that already exist, and it made the payload carry
    Jira's project key purely so a template string could put it back together.

    Not a translated string: these are the provider's identifiers, not prose,
    and ``PROJ-123`` reads the same in every locale.

    A REGISTERED provider names its own objects through the optional
    :meth:`SourceProviderPlugin.chip_label` hook, for the same reason: an
    internal review system whose objects are ``CR-123`` cannot be spelled with
    any built-in's punctuation.

    An unrecognized provider falls to ``#number``, the most widely shared
    convention, rather than borrowing the punctuation of a specific vendor.
    """
    plugin = registered_source_provider(ref.provider)
    if plugin is not None:
        hook = getattr(plugin, "chip_label", None)
        if callable(hook):
            try:
                label = hook(ref)
            except Exception:
                logger.debug("source provider %s chip_label failed", ref.provider, exc_info=True)
            else:
                # A plugin label is rendered into a sidebar chip, so it is bounded
                # and type-checked rather than trusted; an unusable one degrades to
                # the neutral fallback instead of emitting a broken chip.
                if isinstance(label, str) and label and len(label) <= _MAX_CHIP_LABEL_LENGTH:
                    return label
    if ref.provider == "jira":
        return f"{ref.repo}-{ref.number}"
    if ref.provider == "gitlab" and ref.kind == "change":
        return f"!{ref.number}"
    return f"#{ref.number}"


def _plugin_for_change(ref: SourceRef) -> SourceProviderPlugin | None:
    """The plugin owning this ref, or None when a built-in path should run."""
    return _SOURCE_PROVIDER_PLUGINS.get(ref.provider)


def _plugin_setup_error(
    plugin: SourceProviderPlugin, exc: SourceProviderNotConfigured
) -> SourceProviderError:
    """Replace a plugin's not-configured signal with its own setup guidance."""
    try:
        message = plugin.setup_message()
    except Exception:
        logger.debug("source provider %s setup_message failed", plugin.id, exc_info=True)
        message = ""
    if not isinstance(message, str):
        message = ""
    # `setup_message()` is edition-authored operator guidance, but the `str(exc)`
    # fallback is plugin RUNTIME text, so the whole message goes through the same
    # redaction a built-in's stderr does rather than only the fallback branch.
    return SourceProviderError(
        sanitize._safe_error_text(
            message or str(exc),
            fallback=f"{plugin.id} is not configured.",
        )
    )


@contextlib.contextmanager
def _plugin_errors(plugin_id: str) -> Iterator[None]:
    """Redact the message of any exception a plugin raises out of a dispatch.

    The seam's whole claim is that a plugin "cannot opt out" of the shared
    hardening because it is dispatched from inside it. `_redact_provider_data`
    delivers that for the RETURNED payload, but an exception took a second route
    to the client that skipped every scrubber: `SourceProviderError` reaches the
    503 body verbatim and `ValueError` reaches the 400 body verbatim, so a
    plugin whose backend embedded a token or a presigned URL in its failure text
    published it. A built-in never could — every built-in failure path already
    runs its provider's stderr through `_safe_error`.

    `SourceProviderNotConfigured` is redacted here too, keeping its own type:
    the fetch callers catch it and substitute the plugin's setup guidance (see
    `_plugin_setup_error`), but the mutation hooks have no such substitution, so
    an unredacted pass-through published the raw not-configured message in the
    503 body on exactly that path.

    The exception TYPE is preserved so each caller's own handling, and the
    status code each maps to, are unchanged; only the message is scrubbed.

    Deliberately NOT ``except Exception``: an unlisted type (a plugin's bare
    ``RuntimeError``, ``KeyError``, its own class) propagates to a generic 500
    whose body carries no exception text, so there is nothing to scrub on that
    route. That safety lives in the response handlers only writing
    ``SourceProviderError`` / ``ValueError`` text into client-visible bodies --
    anyone widening a handler to render other exception text must widen this
    boundary in the same change.
    """
    try:
        yield
    except SourceProviderNotConfigured as exc:
        raise SourceProviderNotConfigured(
            sanitize._safe_error_text(str(exc), fallback=f"{plugin_id} is not configured")
        ) from exc
    except SourceCapacityError as exc:
        raise SourceCapacityError(
            sanitize._safe_error_text(str(exc), fallback="provider is busy")
        ) from exc
    except SourceProviderError as exc:
        raise SourceProviderError(
            sanitize._safe_error_text(str(exc), fallback=f"the {plugin_id} source provider failed")
        ) from exc
    except ConfirmationRequired as exc:
        # A ``ValueError`` subclass with response semantics: it is what makes
        # `_owner_mutation_response` add ``confirmationRequired: True`` to the
        # 400 body, which is the client's only cue to offer the confirm-and-
        # retry affordance. Downcasting it to the parent arm below turns a
        # plugin's answerable refusal into a dead-end error, so it keeps its
        # type just as the ``SourceProviderError`` subclasses above keep
        # theirs.
        raise ConfirmationRequired(
            sanitize._safe_error_text(
                str(exc), fallback=f"the {plugin_id} source provider needs confirmation"
            )
        ) from exc
    except ValueError as exc:
        raise ValueError(
            sanitize._safe_error_text(str(exc), fallback=f"the {plugin_id} source provider refused")
        ) from exc


def _require_plugin_hook(ref: SourceRef, name: str, action: str) -> Any:
    """Resolve a plugin mutation hook, or None when the caller owns the built-ins.

    Returns None for a built-in provider so the existing code path continues
    untouched. For a REGISTERED provider it either returns the hook or raises the
    ``ValueError`` every mutation endpoint already maps to a 400 -- so an
    unimplemented mutation reads as "this provider does not support it" rather
    than falling into a GitHub-only branch and reporting the wrong reason.
    """
    plugin = _plugin_for_change(ref)
    if plugin is None:
        return None
    hook = getattr(plugin, name, None)
    if not callable(hook):
        raise ValueError(f"{action} is not supported by the '{ref.provider}' source provider.")
    return hook


def _parse_registered_source_url(raw_url: str) -> SourceRef | None:
    """Consult every registered plugin, in registration order.

    A plugin that raises is skipped rather than allowed to break URL validation
    for every provider; a ref that does not match its own plugin's id, or is not
    a normalized ``https`` URL, is refused -- it would otherwise become a cache
    key and an audit subject the gateway cannot re-derive.
    """
    for plugin in _SOURCE_PROVIDER_PLUGINS.values():
        try:
            ref = plugin.parse(raw_url)
        except Exception:
            logger.debug("source provider %s parse failed", plugin.id, exc_info=True)
            continue
        if ref is None:
            continue
        if not isinstance(ref, SourceRef) or ref.provider != plugin.id:
            logger.warning("source provider %s returned a foreign ref; ignoring", plugin.id)
            continue
        if not ref.url.startswith("https://") or len(ref.url) > contract._MAX_URL_LENGTH:
            logger.warning("source provider %s returned a non-https ref; ignoring", plugin.id)
            continue
        # Change refs only: the issue fetch pipeline is built-in-only, so an
        # admitted plugin issue ref would render a chip whose panel can only
        # 400. Widening this is additive if a plugin issue path ever exists.
        if ref.kind != "change" or not isinstance(ref.number, int):
            logger.warning("source provider %s returned a malformed ref; ignoring", plugin.id)
            continue
        return ref
    return None
