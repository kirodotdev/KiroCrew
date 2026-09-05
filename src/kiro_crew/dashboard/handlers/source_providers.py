"""Pull-request source data and owner-only review-thread mutation.

The browser sends a GitHub pull-request or GitLab merge-request URL. This
module validates the parsed host and path, then delegates authentication to a
validated absolute provider CLI. Credentials stay inside ``gh``/``glab`` and are
never returned to the browser. Credential-backed access is restricted to the
configured dashboard owner. Standalone local dashboards use their signed
bootstrap identity as the implicit owner when no channel owner is configured.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fnmatch
import hashlib
import itertools
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import PurePosixPath
from typing import Any, Protocol, TypedDict, TypeVar
from urllib.parse import quote, urlparse, urlunparse

import aiohttp
from aiohttp import web

from kiro_crew import github_runner, platform_compat
from kiro_crew.config.loader import KiroCrewConfig, config_dir, read_env_file_credential
from kiro_crew.dashboard.handlers._shared import read_capped_response

# Validation policy, well-known install dirs, and the strict-mode toggle are
# owned by the shared hardened runner (kiro_crew.github_runner) so every
# gh/glab-spawning surface applies exactly the same trust policy and never
# drifts. Re-exported under the historical private names because this module
# is their long-standing import location (issue_radar's glab resolution and
# the provider tests reach them here).
from kiro_crew.github_runner import GH_ENV_PASSTHROUGH as _GH_ENV_PASSTHROUGH
from kiro_crew.github_runner import (
    PROVIDER_EXECUTABLE_CANDIDATES as _PROVIDER_EXECUTABLE_CANDIDATES,
)
from kiro_crew.github_runner import STRICT_PROVIDER_BIN_ENV as _STRICT_PROVIDER_BIN_ENV
from kiro_crew.github_runner import (
    provider_executable_candidates,
)
from kiro_crew.github_runner import strict_provider_bins as _strict_provider_bins
from kiro_crew.github_runner import validate_provider_executable as _validate_provider_executable
from kiro_crew.history_search import register_search_ref_resolver as _register_search_ref_resolver
from kiro_crew.history_search import (
    reset_search_ref_resolver_for_tests as _reset_search_ref_resolver,
)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sandbox import (
    create_subprocess_limited,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
)
from kiro_crew.secrets import SecretVault
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

_MAX_URL_LENGTH = 2048
# Hard per-section limits enforced while draining provider stdout. Diff-bearing
# sections get more room than metadata/checks, but no subprocess may retain the
# old payload-sized allowance independently.
_METADATA_OUTPUT_BYTES = 1 * 1024 * 1024
_DISCUSSION_OUTPUT_BYTES = 2 * 1024 * 1024
_DIFF_OUTPUT_BYTES = 4 * 1024 * 1024
_CHECKS_OUTPUT_BYTES = 1 * 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024
# Bound the normalized aggregate returned to the browser. Reservations below
# conservatively cover raw bytes, decoded JSON, normalized copies, and Python
# object overhead while a complete direct fetch remains alive.
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_SECONDARY_PAGE_SIZE = 100
_COMMAND_TIMEOUT_SECS = 30
_CACHE_TTL_SECS = 30
# A merged pull request is terminal: nothing the chip renders moves again, and
# re-reading it on the open-PR cadence for as long as its chip stays in a
# sidebar was pure provider load — one `gh` subprocess per finished PR per
# minute from the chip loop alone, forever. A CLOSED one is nearly so, but it can
# be reopened and keeps accruing discussion, so it ages on a shorter clock.
# These govern the chip cache and the full payloads that have no cheaper
# revalidation (GitLab, registered plugins); a github.com full payload is
# instead REVALIDATED with a conditional GET at the open cadence whatever its
# lifecycle (see `_revalidate_pull_request`), so post-merge comments and a
# reopen reach the panel within one TTL for one rate-limit-free request.
# Mutation invalidation still drops these entries, the explicit refresh bypasses
# them, and the turn-boundary force still re-reads a CLOSED chip, never a merged
# one.
_TERMINAL_TTL_SECS = 6 * 60 * 60
_CLOSED_TTL_SECS = 60 * 60
# Ceiling on how long conditional revalidation may keep re-stamping one full
# payload without a full read behind it. The probes rest on GitHub moving the
# `issues/{n}` ETag for every rendered field, which the API does not promise for
# every mutation class; past this age one full read runs regardless of what the
# probes say, so a coverage gap degrades to bounded staleness, never unbounded.
_REVALIDATED_MAX_AGE_SECS = _TERMINAL_TTL_SECS
# Chip-vocabulary states (see ``_project_state``) that never move on their own.
_TERMINAL_CHIP_STATES = frozenset({"merged", "closed"})
_CACHE_MAX_ENTRIES = 32
_CACHE_MAX_BYTES = 48 * 1024 * 1024
_PROVIDER_CONCURRENCY = 4
# Bound direct full/check fetches by task count and retained-memory weight.
# Same-URL callers coalesce before admission; detached stale tasks keep their
# reservation until their underlying task actually completes.
_DIRECT_FETCH_PENDING_MAX = 16
_DIRECT_FETCH_MAX_RESERVED_BYTES = 128 * 1024 * 1024
# Measured worst case for one full fetch -- every command in the fanout at its
# declared output ceiling -- is a ~32MB allocation peak, and ~43MB projected if
# every ceiling is filled exactly (decode amplifies wire bytes by ~4.3x). 64MB
# is therefore a ~1.5x cover for raw bytes, decoded JSON, the normalized copy,
# and object overhead while a complete fetch remains alive. Two of these
# saturate the pool by design; a third caller WAITS for room rather than being
# refused (see _wait_for_direct_fetch_capacity), so the ceiling bounds memory
# without turning ordinary concurrent panel use into an error.
_FULL_FETCH_RESERVATION_BYTES = 64 * 1024 * 1024
_CHECKS_FETCH_RESERVATION_BYTES = 8 * 1024 * 1024
# How long a caller waits for admission room before giving up. Sized under the
# per-command timeout (_COMMAND_TIMEOUT_SECS) so a queued read cannot outlive the
# fetch it is queued behind by more than one command's worth of work.
_DIRECT_FETCH_WAIT_SECS = 20.0
# An issue payload is metadata plus comments -- no diffs, no check rollup -- so
# both its aggregate cache and its retained-memory lease sit well below the
# pull-request figures. TTL and entry count are shared with the PR cache.
_ISSUE_CACHE_MAX_BYTES = 16 * 1024 * 1024
_ISSUE_FETCH_RESERVATION_BYTES = 16 * 1024 * 1024
_PROVIDER_EXECUTABLE_OVERRIDES = {
    "gh": github_runner.GH_BIN_ENV,
    "glab": "KIROCREW_GLAB_BIN",
}
# Provider commands are absolute. Keep PATH deterministic only for trusted
# system helpers a provider may invoke; never inherit a workspace-controlled
# PATH or search it for gh/glab.
_PROVIDER_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Only variables needed to configure the provider CLI, reach its API, and use
# that provider's authentication cross this trust boundary. In particular,
# unrelated gateway/AWS/Slack credentials and arbitrary PATH entries are never
# inherited.
_PROVIDER_BASE_ENV_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
_PROVIDER_AUTH_ENV_KEYS = {
    # The gh set derives from the canonical union owned by the shared runner
    # (every key is gh-scoped auth/network/TLS config). GH_HOST passes through
    # it, but _run_json pins GH_HOST=github.com afterward, so the final env
    # cannot drift to a configured enterprise default — and for the same
    # reason the enterprise tokens are withheld: a github.com-pinned child can
    # never use them, so forwarding them is pure surplus credential surface.
    "gh": frozenset(_GH_ENV_PASSTHROUGH) - {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"},
    "glab": frozenset({"GLAB_CONFIG_DIR", "GITLAB_TOKEN"}),
}
# url -> (stored_at, serialized_size_bytes, normalized_payload)
_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
_CACHE_LOCK = LoopBoundLock()
_FULL_FETCH_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_FULL_FETCH_TASKS: dict[str, set[asyncio.Task[dict[str, Any]]]] = {}
_FULL_FETCH_GENERATIONS: dict[str, int] = {}
_CHECKS_FETCH_INFLIGHT: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}
# Issues get their own cache and inflight map rather than sharing the
# pull-request ones: the two live at different URLs but the same normalized-URL
# key space would still be shared, and a PR mutation's cache invalidation
# (_invalidate_pull_request_cache) must not evict issue payloads it knows
# nothing about. No generation map is needed -- this phase never mutates an
# issue, so there is no post-mutation write to order against.
_ISSUE_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
_ISSUE_CACHE_LOCK = LoopBoundLock()
_ISSUE_FETCH_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_ISSUE_FETCH_TASKS: dict[str, set[asyncio.Task[dict[str, Any]]]] = {}
_DIRECT_FETCH_RESERVATIONS: dict[asyncio.Task[Any], int] = {}
# Futures held by callers waiting for admission room. Woken when any reservation
# is released, so a request that arrives at a full pool queues instead of
# failing (see _wait_for_direct_fetch_capacity).
_DIRECT_FETCH_WAITERS: list[asyncio.Future[None]] = []
_provider_semaphore = asyncio.Semaphore(_PROVIDER_CONCURRENCY)
_SAFE_ERROR_RE = re.compile(r"\s+")
_PROVIDER_TOOL_NAME = "source_provider_cli"
logger = logging.getLogger(__name__)


class SourceProviderError(RuntimeError):
    """A provider CLI could not return the requested source data."""


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


def _sel():
    import kiro_crew.dashboard.handlers as _pkg  # circular import: package exports this module

    return _pkg.sel()


def _audit_provider_cli(
    executable: str,
    outcome: str,
    reason: str,
    *,
    critical: bool = False,
) -> None:
    """Emit a credential-free provider lifecycle event."""
    provider = executable if executable in {"gh", "glab"} else "unknown"
    try:
        _sel().log_tool_invocation(
            session_key="dashboard:source-provider",
            source="dashboard",
            tool_name=_PROVIDER_TOOL_NAME,
            tool_kind="provider_cli",
            outcome=outcome,
            downstream_service=provider,
            error=reason,
            metadata={"provider": provider, "reason": reason},
            critical=critical,
        )
    except Exception:
        if critical:
            raise
        logger.debug("SEL provider CLI audit failed", exc_info=True)


def _provider_setup_message(executable: str, override_name: str, last_error: str) -> str:
    """User-facing guidance when no acceptable provider CLI was found."""
    provider = "GitHub" if executable == "gh" else "GitLab"
    detail = f"\nLast check reported: {last_error}.\n" if last_error else ""
    if _strict_provider_bins():
        managed_dir = os.path.dirname(_PROVIDER_EXECUTABLE_CANDIDATES[executable][0])
        return (
            f"Can't load pull requests: {_STRICT_PROVIDER_BIN_ENV} is set, so this "
            f"host only accepts a root-owned {executable}.\n"
            "\n"
            f"  sudo mkdir -p {managed_dir}\n"
            f'  sudo cp "$(command -v {executable})" {managed_dir}/{executable}\n'
            f"  sudo chown -R root {managed_dir}\n"
            f"  sudo chmod 755 {managed_dir}/{executable}\n"
            f"{detail}"
            "\n"
            f"You won't have to sign in again -- your existing "
            f"`{executable} auth login` credentials are reused automatically.\n"
            "\n"
            f"Alternative: point {override_name} at an already-trusted, absolute "
            f"{executable} path, or unset {_STRICT_PROVIDER_BIN_ENV}."
        )
    return (
        f"Can't load pull requests: the {provider} CLI ({executable}) isn't "
        "available to the Kiro Crew gateway.\n"
        "\n"
        "Install it and sign in, then click Retry:\n"
        "\n"
        f"  brew install {executable}      # or your distro's package manager\n"
        f"  {executable} auth login\n"
        f"{detail}"
        "\n"
        f"Already installed? The gateway searches the standard install dirs plus "
        f"its own PATH and accepts your own {executable} -- Homebrew included. It "
        "still refuses one owned by another user, one that is world-writable, and "
        "one inside your project or workspace tree, since the agent can write "
        "there.\n"
        "\n"
        f"Alternative: point {override_name} at an absolute {executable} path."
    )


def _resolve_provider_executable(executable: str) -> str:
    """Resolve gh/glab: explicit override, well-known install dirs, then PATH."""
    if executable not in _PROVIDER_EXECUTABLE_CANDIDATES:
        raise SourceProviderError("unsupported provider command")
    override_name = _PROVIDER_EXECUTABLE_OVERRIDES[executable]
    override = os.environ.get(override_name)
    if override is not None:
        try:
            return _validate_provider_executable(override)
        except ValueError as exc:
            raise SourceProviderError(
                f"{override_name} is not a trusted executable: {exc}"
            ) from exc

    last_error = ""
    for candidate in provider_executable_candidates(executable):
        try:
            return _validate_provider_executable(candidate)
        except ValueError as exc:
            message = str(exc)
            # "does not exist" is noise on a host that simply lacks that dir;
            # keep the most informative rejection for the setup message.
            if message != "path does not exist":
                last_error = message
            continue
    raise SourceProviderError(_provider_setup_message(executable, override_name, last_error))


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


# --- Source-provider plugin seam --------------------------------------------
#
# The three built-in providers stay exactly as they are: their host checks run
# first in `parse_source_url`, their fetchers are dispatched by name, and none of
# the code below changes a byte of their behaviour. This registry is what lets a
# downstream edition add a FOURTH provider -- an internal code-review system --
# from its own composition root instead of shadowing this module on every
# upstream sync.
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

    The transcript search recognizes the built-in forge shapes itself (``#4411``,
    ``pull/4411``, a PR URL) and expands them to every spelling of the same item,
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
        _safe_error_text(
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
            _safe_error_text(str(exc), fallback=f"{plugin_id} is not configured")
        ) from exc
    except SourceCapacityError as exc:
        raise SourceCapacityError(_safe_error_text(str(exc), fallback="provider is busy")) from exc
    except SourceProviderError as exc:
        raise SourceProviderError(
            _safe_error_text(str(exc), fallback=f"the {plugin_id} source provider failed")
        ) from exc
    except ConfirmationRequired as exc:
        # A ``ValueError`` subclass with response semantics: it is what makes
        # `_owner_mutation_response` add ``confirmationRequired: True`` to the
        # 400 body, which is the client's only cue to offer the confirm-and-
        # retry affordance. Downcasting it to the parent arm below turns a
        # plugin's answerable refusal into a dead-end error, so it keeps its
        # type just as the ``SourceProviderError`` subclasses above keep
        # theirs. (Defined later in the module; an ``except`` clause is only
        # resolved when this context manager actually runs.)
        raise ConfirmationRequired(
            _safe_error_text(
                str(exc), fallback=f"the {plugin_id} source provider needs confirmation"
            )
        ) from exc
    except ValueError as exc:
        raise ValueError(
            _safe_error_text(str(exc), fallback=f"the {plugin_id} source provider refused")
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
        if not ref.url.startswith("https://") or len(ref.url) > _MAX_URL_LENGTH:
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


_GITLAB_HOSTS_TTL_SECS = 30.0
# Cached allowlist snapshots. Populated only by _load_source_link_settings()
# running in a worker thread, so every reader on the event loop is a pure dict
# lookup. GitLab and Jira allowlists come out of the SAME config read and share
# one TTL, lock, and generation counter: they change together (one config file)
# and consumers that memoize parse results (the per-slot sidebar source links)
# fold a single generation into their cache key either way.
_gitlab_hosts_snapshot: frozenset[str] = frozenset()
_jira_hosts_snapshot: frozenset[str] = frozenset()
# Whether the sidebar renders a session card's PR/issue chips at all
# (``dashboard.session_card_source_links``). Same read, same TTL, same
# generation as the allowlists above, for the same reason: it is another
# ``dashboard`` field off the same config file, and it is consumed by the same
# synchronous slot serialization that cannot read config itself.
#
# Starts TRUE where the allowlists start EMPTY, and the asymmetry is deliberate:
# an unknown host must fail CLOSED (do not recognize a link yet), but the chip
# strip predates this switch, so a cold snapshot must fail OPEN or every install
# would render no chips until the first refresh lands.
_session_card_chips_snapshot: bool = True
_gitlab_hosts_loaded_at = 0.0
# Bumped whenever either snapshot's CONTENT changes. Consumers that memoize a
# parse result (per-slot sidebar source links) fold this into their cache key so
# a later allowlist load invalidates decisions made against the cold snapshot.
_gitlab_hosts_generation = 0
_gitlab_hosts_lock = LoopBoundLock()


def gitlab_hosts_generation() -> int:
    """Monotonic counter identifying the current allowlist snapshot."""
    return _gitlab_hosts_generation


def _gitlab_hosts_fresh() -> bool:
    return bool(
        _gitlab_hosts_loaded_at
        and time.monotonic() - _gitlab_hosts_loaded_at < _GITLAB_HOSTS_TTL_SECS
    )


def _publish_provider_hosts(gitlab: frozenset[str], jira: frozenset[str]) -> None:
    """Install freshly loaded snapshots, bumping the generation on real change."""
    global _gitlab_hosts_snapshot, _jira_hosts_snapshot
    global _gitlab_hosts_loaded_at, _gitlab_hosts_generation
    if gitlab != _gitlab_hosts_snapshot or jira != _jira_hosts_snapshot:
        _gitlab_hosts_snapshot = gitlab
        _jira_hosts_snapshot = jira
        _gitlab_hosts_generation += 1
    _gitlab_hosts_loaded_at = time.monotonic()


def _publish_session_card_chips(enabled: bool) -> None:
    """Install the chip switch snapshot, bumping the SHARED generation on change.

    Its own publisher rather than a third argument to
    :func:`_publish_provider_hosts`, so a caller that only has hosts to install
    cannot silently reset the switch. The generation is shared on purpose: the
    owner websocket's refresh round pushes a fresh slots payload whenever it
    moves, which is what makes the chips appear or disappear without a reload.

    Callers outside the refresh must go through
    :func:`publish_session_card_chips_now`, which orders them against an
    in-flight load.
    """
    global _session_card_chips_snapshot, _gitlab_hosts_generation
    if enabled != _session_card_chips_snapshot:
        _session_card_chips_snapshot = enabled
        _gitlab_hosts_generation += 1


async def publish_session_card_chips_now(enabled: bool) -> None:
    """Install a just-WRITTEN chip switch value, ordered against the refresh.

    The switch has two writers the allowlists do not: the config PUT, which
    publishes at write time so the click is not stuck behind the TTL, and the
    refresh poll. Taking ``_gitlab_hosts_lock`` -- which
    :func:`ensure_gitlab_hosts_loaded` holds ACROSS its threaded load -- is what
    keeps them ordered: a poll already in flight is holding a reading from before
    the write, and publishing that after the write would resume the chips, and the
    credentialed polling behind them, for another full interval. Waiting for the
    lock means the write always lands last.

    The wait is bounded by that load, which is one config read.
    """
    async with _gitlab_hosts_lock:
        _publish_session_card_chips(enabled)


def session_card_source_links_enabled() -> bool:
    """Are the sidebar's per-card PR/issue chips switched on? Cache-only read.

    Safe to call from sync code on the event loop -- slot serialization and the
    check-refresh feeds do, once per slots push. It never touches the
    filesystem: the value arrives only from :func:`_load_source_link_settings`
    running in a worker thread, so an operator's edit takes effect within one TTL
    instead of stalling every push on a config read.
    """
    return _session_card_chips_snapshot


def _load_source_link_settings() -> tuple[frozenset[str], frozenset[str], bool]:
    """Read the source-link config: GitLab hosts, Jira hosts, chip switch.

    BLOCKING -- never on the loop. ``KiroCrewConfig.load()`` stats, reads,
    parses, and validates config files, so a slow or network-backed config
    directory would stall the sole event loop. Callers reach this only through
    :func:`ensure_gitlab_hosts_loaded`.

    One read for all three because they live in one file and are consumed by the
    same synchronous slot serialization; a second read would double the cost of
    every refresh round and let the two halves disagree within one round.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        dashboard = KiroCrewConfig.load().dashboard
        return (
            frozenset(dashboard.gitlab_hosts),
            frozenset(dashboard.jira_hosts),
            bool(dashboard.session_card_source_links),
        )
    except Exception:
        logger.debug("source-link settings unavailable", exc_info=True)
        # Hosts fail closed, the chip switch fails open: an unreadable config
        # must not blank a strip the user never asked to hide.
        return frozenset(), frozenset(), True


async def ensure_gitlab_hosts_loaded() -> frozenset[str]:
    """Refresh the cached allowlist off the event loop when the TTL has expired.

    Awaited by every async entry point before it validates a URL, so an operator
    adding an instance takes effect within one TTL without a gateway restart and
    without a config read ever running on the loop.

    The refresh is serialized: two concurrent loads could otherwise interleave so
    that a reader holding the PRE-revocation config installs its snapshot after
    the post-revocation one and resets the TTL, re-admitting a host the operator
    just removed for another full interval. The lock plus a post-acquire
    freshness recheck means exactly one load happens per expiry.
    """
    global _gitlab_hosts_snapshot, _gitlab_hosts_loaded_at
    if _gitlab_hosts_fresh():
        return _gitlab_hosts_snapshot
    async with _gitlab_hosts_lock:
        # Another waiter may have refreshed while this one queued.
        if _gitlab_hosts_fresh():
            return _gitlab_hosts_snapshot
        gitlab, jira, chips = await asyncio.to_thread(_load_source_link_settings)
        _publish_provider_hosts(gitlab, jira)
        # Safe to install unconditionally: this lock is held across the load
        # above, so a config write that raced it waits and publishes after us
        # (see publish_session_card_chips_now).
        _publish_session_card_chips(chips)
        return gitlab


def _allowed_gitlab_hosts() -> frozenset[str]:
    """Return the cached self-managed GitLab hosts without touching the filesystem.

    Safe to call from sync code on the event loop (URL parsing, slot
    serialization). The snapshot comes only from config -- never from the browser
    -- and is matched exactly, so neither a pasted URL nor a lookalike suffix can
    widen it. Before the first :func:`ensure_gitlab_hosts_loaded` completes the
    snapshot is empty, which fails closed (a self-managed URL is simply not
    recognized yet).
    """
    return _gitlab_hosts_snapshot


def _allowed_jira_hosts() -> frozenset[str]:
    """Return the cached self-hosted Jira hosts. Same discipline as GitLab.

    Loaded by the same :func:`ensure_gitlab_hosts_loaded` refresh (one config
    read covers both allowlists), so every entry point that awaits it before
    parsing has this snapshot warm too. Fails closed while cold.
    """
    return _jira_hosts_snapshot


# Path markers that identify a GitLab object, paired with the SourceRef kind
# they produce. Plain string literals matched with rfind -- deliberately not a
# regex alternation (see _parse_gitlab_path).
_GITLAB_PATH_MARKERS: tuple[tuple[str, str], ...] = (
    ("/-/merge_requests/", "change"),
    ("/-/issues/", "issue"),
)
# GitHub keeps issues and pull requests in one number space under two path
# segments. The captured segment is what derives the kind.
_GITHUB_PATH_RE = re.compile(r"/([^/]+)/([^/]+)/(pull|issues)/(\d+)", re.IGNORECASE)
_GITHUB_SEGMENT_KINDS = {"pull": "change", "issues": "issue"}


def _parse_gitlab_path(path: str) -> tuple[str, int, str]:
    """Split a GitLab MR/issue path into (project, number, kind) or raise ``ValueError``."""
    # String ops instead of a regex: the previous /(.+)/-/merge_requests/
    # pattern backtracked polynomially on adversarial paths. The
    # two markers are scanned independently and the RIGHTMOST valid one wins,
    # preserving the original ``rfind`` semantics (a project path that itself
    # contains the marker text is still split at the last occurrence) without
    # reintroducing an alternating pattern.
    best: tuple[str, int, str] | None = None
    best_idx = -1
    lowered = path.lower()
    for marker, kind in _GITLAB_PATH_MARKERS:
        idx = lowered.rfind(marker)
        if idx <= 0 or idx <= best_idx:
            continue
        project = path[1:idx]
        number_text = path[idx + len(marker) :]
        if not project or not number_text.isdigit():
            continue
        best_idx = idx
        best = (project, int(number_text), kind)
    if best is None:
        raise ValueError(
            "Expected a GitLab URL like https://gitlab.com/group/project/-/merge_requests/123 "
            "or https://gitlab.com/group/project/-/issues/123."
        )
    if any(segment in {"", ".", ".."} for segment in best[0].split("/")):
        raise ValueError("Invalid GitLab project path.")
    return best


def _gitlab_ref(host: str, path: str) -> SourceRef:
    """Build a GitLab ``SourceRef`` for an already-authorized host."""
    project, number, kind = _parse_gitlab_path(path)
    normalized = urlunparse(("https", host, path, "", "", ""))
    repo = project.rsplit("/", 1)[-1]
    owner = project.rsplit("/", 1)[0] if "/" in project else ""
    return SourceRef("gitlab", normalized, host, owner, repo, number, project=project, kind=kind)


# A Jira issue key: PROJECT-NUMBER where the project part starts with a letter,
# is uppercase alphanumeric, and Jira caps it at 10 characters. Mirrors the
# frontend's JIRA_KEY_RE in pullRequestLinks.ts -- the two parsers must agree so
# a chip the backend emits always re-parses on the frontend for the reveal.
_JIRA_KEY_RE = re.compile(r"[A-Z][A-Z0-9]{0,9}-\d+")


def _jira_ref(host: str, path: str) -> SourceRef:
    """Build a Jira ``SourceRef`` for an already-authorized host.

    Jira issues live at ``/browse/KEY-123``, possibly behind a context path
    (``/jira/browse/KEY-123`` on some Cloud tenants and Data Center installs).
    The prefix is preserved in the normalized URL so a self-hosted chip opens
    the real endpoint. The project key maps onto ``repo`` and the numeric tail
    onto ``number`` -- the same shape the frontend derives, so the sidebar chip
    label (``PROJ-123``) and the Issues panel identity agree end to end.
    """
    marker = "/browse/"
    browse_idx = path.find(marker)
    if browse_idx < 0:
        raise ValueError("Expected a Jira URL like https://org.atlassian.net/browse/PROJ-123.")
    # The key is the first segment after /browse/; deeper segments are Jira UI
    # state, not identity. Uppercase before validating -- Jira treats keys
    # case-insensitively and canonicalizing here keeps the dedup map in
    # state.py from splitting one issue across case variants.
    key = path[browse_idx + len(marker) :].split("/", 1)[0].upper()
    if not _JIRA_KEY_RE.fullmatch(key):
        raise ValueError("Expected a Jira URL like https://org.atlassian.net/browse/PROJ-123.")
    project_key, number_text = key.rsplit("-", 1)
    prefix = path[:browse_idx]
    normalized = urlunparse(("https", host, f"{prefix}{marker}{key}", "", "", ""))
    return SourceRef("jira", normalized, host, "", project_key, int(number_text), kind="issue")


def parse_source_url(raw_url: str) -> SourceRef:
    """Validate and normalize a supported pull/merge-request or issue URL.

    Public GitHub and gitlab.com are always accepted. A self-managed GitLab
    instance is accepted only when its exact ``host[:port]`` appears in the
    operator's ``dashboard.gitlab_hosts`` allowlist, so browser input can never
    choose which instance the credential-bearing CLI talks to.
    Exact parsed-host checks prevent URLs that merely mention a trusted host in
    their path, query, or userinfo from reaching a provider CLI.

    Issues and pull/merge requests share this one validator so both surfaces
    inherit the same host, scheme, and path guarantees. The returned
    ``SourceRef.kind`` says which namespace the number belongs to; every
    pull-request-only caller must gate on it via :func:`_require_change_ref`.
    """
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > _MAX_URL_LENGTH:
        raise ValueError("A pull-request URL is required.")
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Only HTTPS pull-request URLs without userinfo are supported.")
    # Strip a trailing dot so an absolute-FQDN URL (``gitlab.acme.internal.``)
    # matches the allowlist, whose entries are canonicalized the same way by the
    # config loader (:func:`_coerce_gitlab_hosts`). Without this the two sides
    # can never agree and the host is rejected (fails closed).
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/")

    if host in {"github.com", "www.github.com"}:
        match = _GITHUB_PATH_RE.fullmatch(path)
        if not match:
            raise ValueError(
                "Expected a GitHub URL like https://github.com/org/repo/pull/123 "
                "or https://github.com/org/repo/issues/123."
            )
        owner, repo, segment, number = match.groups()
        if owner in {".", ".."} or repo in {".", ".."}:
            raise ValueError("Invalid GitHub owner/repo path.")
        normalized = urlunparse(("https", "github.com", path, "", "", ""))
        return SourceRef(
            "github",
            normalized,
            "github.com",
            owner,
            repo,
            int(number),
            kind=_GITHUB_SEGMENT_KINDS[segment.lower()],
        )

    if host in {"gitlab.com", "www.gitlab.com"}:
        return _gitlab_ref("gitlab.com", path)

    # A self-managed instance may listen on a non-default port, so the allowlist
    # is matched against host and host:port -- an entry without a port does not
    # authorize an arbitrary port on the same host. An explicit :443 is treated
    # as absent, matching the browser URL API (which drops the default HTTPS
    # port) so the same URL resolves identically on both sides.
    port = parsed.port
    candidate = f"{host}:{port}" if port and port != 443 else host
    if host and candidate in _allowed_gitlab_hosts():
        return _gitlab_ref(candidate, path)

    # Jira: Atlassian Cloud (``*.atlassian.net``) is recognized automatically --
    # the suffix is Atlassian-operated, so it identifies the product the way
    # ``github.com`` does. Self-hosted Jira / Data Center requires an exact
    # entry in ``dashboard.jira_hosts``, the same allowlist discipline as
    # self-managed GitLab and checked AFTER it so a host an operator listed as
    # GitLab is never reinterpreted as Jira.
    is_cloud_jira = host.endswith(".atlassian.net") and len(host) > len(".atlassian.net")
    if host and (is_cloud_jira or candidate in _allowed_jira_hosts()):
        return _jira_ref(candidate, path)

    # Registered providers are consulted LAST, so no edition plugin can reinterpret
    # a built-in host or an operator-allowlisted one, and the three built-in
    # grammars keep exactly the precedence they had. The scheme/userinfo/length
    # checks above have already run, so a plugin never sees an unvalidated URL.
    registered = _parse_registered_source_url(raw_url)
    if registered is not None:
        return registered

    raise ValueError(
        "Only github.com pull requests and issues, gitlab.com merge requests and "
        "issues, merge requests or issues on a GitLab host listed in "
        "dashboard.gitlab_hosts, and Jira issues on *.atlassian.net or a host "
        "listed in dashboard.jira_hosts are supported."
    )


def _require_change_ref(ref: SourceRef) -> SourceRef:
    """Refuse an issue ref at a pull-request-only entry point.

    Issues and pull/merge requests now come out of the same validator, so every
    pre-existing caller would otherwise accept an issue URL. That is not merely
    a wrong-shaped read: on GitHub the two namespaces share one number counter,
    so ``.../issues/58`` would be handed to ``gh pr view`` and answer about pull
    request 58 -- a different object -- and on either provider an
    owner-authenticated mutation (resolve, auto-merge, mark-ready) would be
    aimed at whatever change carries that number. Fail closed with a
    ``ValueError``, which every caller already maps to a 400.
    """
    if ref.kind != "change":
        raise ValueError("This URL points at an issue, not a pull request or merge request.")
    return ref


def _safe_error_text(text: str, *, fallback: str = "provider command failed") -> str:
    """Strip credentials and exfiltration URLs from provider error prose.

    Split out from :func:`_safe_error` so a plugin-raised EXCEPTION gets the
    identical treatment a built-in's stderr gets. The two paths must not
    diverge: both end up verbatim in a client-visible response body.
    """
    text = text.strip()
    text = redact_exfiltration_urls(text)[0]
    text = redact_credentials(text)[0]
    text = _SAFE_ERROR_RE.sub(" ", text)
    return text[:600] or fallback


def _safe_error(stderr: bytes) -> str:
    return _safe_error_text(stderr.decode("utf-8", errors="replace"))


@dataclass(frozen=True)
class RepoRef:
    """A validated bare repository reference (host, owner, repo -- no number)."""

    provider: str
    host: str
    owner: str
    repo: str


# A GitHub username/org login: alphanumeric or single hyphens, 1-39 chars. Used
# to gate the per-login profile lookup so a malformed login from provider data
# can never widen the `gh api users/<login>` path (traversal / query injection).
_GH_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}")


def _strip_git_suffix(repo: str) -> str:
    return repo[:-4] if repo.endswith(".git") else repo


def parse_repo_url(raw_url: str) -> RepoRef:
    """Validate and normalize a bare repository URL (no pull/issue number).

    Reuses the exact host, scheme, and allowlist guarantees of
    :func:`parse_source_url`: HTTPS only, no userinfo, and a host that is
    github.com, gitlab.com, or an operator-allowlisted self-managed GitLab
    instance (via :func:`ensure_gitlab_hosts_loaded`). Fails closed on every
    other host so browser input can never point a credential-bearing CLI at an
    arbitrary server. A pull/issue URL (extra path segments) is refused rather
    than silently truncated to its owner/repo root.
    """
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > _MAX_URL_LENGTH:
        raise ValueError("A repository URL is required.")
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Only HTTPS repository URLs without userinfo are supported.")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/")
    segments = [segment for segment in PurePosixPath(path).parts if segment not in ("", "/")]

    if host in {"github.com", "www.github.com"}:
        # A repo root is exactly /owner/repo. A pull/issue/tree URL carries more
        # segments and is not a repository root -- refuse it.
        if len(segments) != 2:
            raise ValueError("Expected a GitHub repository URL like https://github.com/owner/repo.")
        owner, repo = segments[0], _strip_git_suffix(segments[1])
        if owner in {".", ".."} or repo in {".", ".."} or not repo:
            raise ValueError("Invalid GitHub owner/repo path.")
        return RepoRef("github", "github.com", owner, repo)

    # gitlab.com and allowlisted self-managed GitLab are recognized as valid git
    # hosts so parsing succeeds, but contributor fetching is GitHub-only in v1
    # (fetch_app_contributors returns [] for a non-github provider). Only the
    # host is authorized here; the project path is not deeply validated.
    port = parsed.port
    candidate = f"{host}:{port}" if port and port != 443 else host
    is_public_gitlab = host in {"gitlab.com", "www.gitlab.com"}
    if host and (is_public_gitlab or candidate in _allowed_gitlab_hosts()):
        if len(segments) < 2:
            raise ValueError(
                "Expected a GitLab repository URL like https://gitlab.com/group/project."
            )
        gitlab_host = "gitlab.com" if is_public_gitlab else candidate
        owner = "/".join(segments[:-1])
        return RepoRef("gitlab", gitlab_host, owner, _strip_git_suffix(segments[-1]))

    raise ValueError(
        "Only github.com and gitlab.com (or a GitLab host listed in "
        "dashboard.gitlab_hosts) repository URLs are supported."
    )


class _ProviderOutputTooLarge(RuntimeError):
    """A provider subprocess exceeded an output stream's byte limit."""


async def _read_stream_limited(stream: asyncio.StreamReader, limit: int, label: str) -> bytes:
    """Drain one subprocess pipe while enforcing a hard byte limit."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise _ProviderOutputTooLarge(f"provider {label} was too large")
        chunks.append(chunk)


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap a provider process tree after timeout, overflow, or cancellation.

    The reap is bounded and drains the pipes: this path is reached with the
    stdout/stderr readers already cancelled by ``wait_for``, so a killed child
    blocked writing into a full pipe -- or a surviving descendant still holding
    the pipes open -- would make a bare ``await proc.wait()`` hang the calling
    task forever.
    """
    await platform_compat.kill_and_reap(proc)


async def _collect_process_output(
    proc: asyncio.subprocess.Process,
    executable: str,
    max_output_bytes: int,
) -> tuple[bytes, bytes]:
    """Read both pipes concurrently with hard limits and bounded lifetime."""
    if proc.stdout is None or proc.stderr is None:
        await _terminate_process(proc)
        raise SourceProviderError(f"{executable} did not expose provider output")
    tasks = [
        asyncio.create_task(_read_stream_limited(proc.stdout, max_output_bytes, "response")),
        asyncio.create_task(_read_stream_limited(proc.stderr, _MAX_ERROR_BYTES, "error output")),
        asyncio.create_task(proc.wait()),
    ]
    try:
        stdout, stderr, _ = await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=_COMMAND_TIMEOUT_SECS
        )
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise SourceProviderError(f"{executable} returned invalid provider output")
        return stdout, stderr
    except asyncio.TimeoutError as exc:
        await _terminate_process(proc)
        raise SourceProviderError(f"{executable} timed out reading the pull request") from exc
    except _ProviderOutputTooLarge as exc:
        await _terminate_process(proc)
        raise SourceProviderError(str(exc)) from exc
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _provider_failure_message(executable: str, stderr: bytes) -> str:
    """Redacted provider stderr, with the login hint appended for auth failures."""
    message = _safe_error(stderr)
    lowered = message.lower()
    if "unauthenticated" in lowered or "not logged in" in lowered or "authentication" in lowered:
        message = f"{message} Run `{executable} auth login`, then retry."
    return message


def _parse_json_success(executable: str) -> Callable[[int, bytes, bytes], Any]:
    """The ordinary provider contract: exit 0 and a JSON body, anything else fails."""

    def parse(returncode: int, stdout: bytes, stderr: bytes) -> Any:
        if returncode != 0:
            raise SourceProviderError(_provider_failure_message(executable, stderr))
        try:
            return json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceProviderError(f"{executable} returned invalid JSON") from exc

    return parse


@dataclass(frozen=True)
class _ConditionalRead:
    """One conditional REST GET: ``status`` is 200 or 304, ``etag`` the validator
    the server sent back (``""`` when it sent none). The body is deliberately
    not carried: the probes decide on status and validator alone and never
    compare bodies, so a 200 body is dead weight."""

    status: int
    etag: str


def _parse_conditional_get(executable: str) -> Callable[[int, bytes, bytes], _ConditionalRead]:
    """Parse ``gh api -i`` output for a conditional GET.

    ``-i`` puts the status line and response headers on stdout ahead of the
    body, which is the only way to read the refreshed ``ETag``. The exit code
    is NOT a usable signal here: ``gh`` treats every non-2xx status as a failure,
    so a ``304 Not Modified`` -- the whole point of the request -- exits 1 with
    ``gh: HTTP 304`` on stderr and an empty body. The status line decides
    instead, and only a status other than 200/304 is a provider error.
    """

    def parse(returncode: int, stdout: bytes, stderr: bytes) -> _ConditionalRead:
        text = stdout.decode("utf-8", errors="replace")
        head, sep, _body = text.partition("\r\n\r\n")
        if not sep:
            head, sep, _body = text.partition("\n\n")
        lines = head.splitlines()
        status_parts = lines[0].split() if lines else []
        try:
            status = int(status_parts[1]) if status_parts[0].upper().startswith("HTTP/") else 0
        except (IndexError, ValueError):
            status = 0
        if status not in (200, 304):
            raise SourceProviderError(_provider_failure_message(executable, stderr))
        etag = ""
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if colon and name.strip().lower() == "etag":
                etag = value.strip()
                break
        return _ConditionalRead(status, etag)

    return parse


async def _run_json(
    *argv: str,
    max_output_bytes: int = _METADATA_OUTPUT_BYTES,
    host: str = "",
) -> Any:
    """Run an allowlisted provider CLI expecting exit 0 and a JSON body.

    Thin wrapper over :func:`_run_provider`; every read and mutation that wants a
    plain JSON answer goes through here.
    """
    return await _run_provider(
        *argv,
        max_output_bytes=max_output_bytes,
        host=host,
        parse=_parse_json_success(argv[0] if argv else ""),
    )


async def _run_provider(
    *argv: str,
    max_output_bytes: int = _METADATA_OUTPUT_BYTES,
    host: str = "",
    parse: Callable[[int, bytes, bytes], Any],
) -> Any:
    """Run an allowlisted provider CLI with isolation, bounds, and SEL audit.

    ``parse`` turns ``(returncode, stdout, stderr)`` into the result and raises
    :class:`SourceProviderError` for a failed run; it runs inside the audited
    section, so a parse failure is recorded as ``failed/provider_error`` and only
    a parsed result reaches ``completed/success``.

    ``host`` is REQUIRED for ``glab`` and must already have passed
    :func:`parse_source_url`; it is re-checked here so a caller cannot reach an
    unauthorized instance even if a future code path forgets to validate, and an
    omitted host is refused rather than silently resolved to gitlab.com.
    """
    executable = argv[0] if argv else ""
    if max_output_bytes <= 0 or max_output_bytes > _DIFF_OUTPUT_BYTES:
        _audit_provider_cli(executable, "denied", "invalid_output_limit")
        raise SourceProviderError("invalid provider output limit")
    if executable not in {"gh", "glab"}:
        _audit_provider_cli(executable, "denied", "unsupported_provider")
        raise SourceProviderError("unsupported provider command")
    gitlab_host = "gitlab.com"
    if executable == "glab":
        # Required, not defaulted: a call site that forgets `host` would
        # otherwise silently target gitlab.com, so an allowlisted self-managed MR
        # could be read -- or mutated -- on the PUBLIC instance at the same
        # project/IID. Failing loudly makes that class of bug impossible to
        # introduce, including from future mutation endpoints.
        if not host:
            _audit_provider_cli(executable, "denied", "host_not_specified")
            raise SourceProviderError("a GitLab host is required for glab calls")
        if host not in {"gitlab.com", "www.gitlab.com"}:
            if host not in _allowed_gitlab_hosts():
                _audit_provider_cli(executable, "denied", "host_not_allowlisted")
                raise SourceProviderError("GitLab host is not allowlisted")
            gitlab_host = host
    # Windows is not refused here: it has no OS sandbox backend, so it reaches
    # the same no-backend policy a backend-less Linux host does, and
    # ``sandboxed_spawn_argv`` below owns that policy (fail closed unless the
    # operator set ``agent.sandbox_allow_unsandboxed_exec``). Every other bound
    # is platform-independent and still applies: the allowlisted executable, the
    # validated resolved path, the strict env allowlist with a pinned PATH, the
    # output cap, the timeout and the SEL audit.
    try:
        # Off the loop: resolution walks every candidate dir and stats the whole
        # parent chain of each hit (github_runner.validate_provider_executable),
        # and a miss re-walks all of PATH. The sidebar chip refresh reaches this
        # on a timer with no user present, so on the loop thread one slow
        # filesystem freezes every task -- including the liveness heartbeat --
        # until the loop watchdog kills the gateway and the supervisor respawns
        # into the same condition.
        resolved_executable = await asyncio.to_thread(_resolve_provider_executable, executable)
    except SourceProviderError:
        _audit_provider_cli(executable, "denied", "executable_untrusted")
        raise

    allowed_env_keys = _PROVIDER_BASE_ENV_KEYS | _PROVIDER_AUTH_ENV_KEYS[executable]
    if executable == "glab" and gitlab_host != "gitlab.com":
        # GITLAB_TOKEN is a single ambient credential with no host binding, so
        # forwarding it while GITLAB_HOST points at a self-managed instance would
        # send a gitlab.com PAT (and every permission it carries) to that server.
        # Self-managed hosts must therefore authenticate from the per-host entry
        # in glab's own config (reachable via GLAB_CONFIG_DIR), which is scoped to
        # the host it was created for.
        allowed_env_keys = allowed_env_keys - {"GITLAB_TOKEN"}
    # Matching follows the shared convention (exact on POSIX, case-folded on
    # Windows — see platform_compat.env_key_allowed) so the filter never
    # depends on the allowlist's casing agreeing with what os.environ yields.
    base_env = {
        key: value
        for key, value in os.environ.items()
        if platform_compat.env_key_allowed(key, allowed_env_keys)
    }
    base_env.update(
        {
            "GH_PAGER": "cat",
            "GLAB_PAGER": "cat",
            "NO_COLOR": "1",
            "PATH": _PROVIDER_SYSTEM_PATH,
        }
    )
    if executable == "gh":
        # All accepted GitHub URLs normalize to github.com. Pin bare API paths
        # to the same host instead of honoring a configured enterprise default.
        base_env["GH_HOST"] = "github.com"
    else:
        # Pin the CLI to the host parse_source_url authorized for this URL, so a
        # self-managed default in glab config can't redirect the bare API paths
        # to a different instance.
        base_env["GITLAB_HOST"] = gitlab_host

    cleanup_path: str | None = None
    invoked = False
    try:
        async with _provider_semaphore:
            try:
                wrapped_argv, env, cleanup_path = await sandboxed_spawn_argv_async(
                    [resolved_executable, *argv[1:]],
                    mode="standard",
                    env=base_env,
                    _prepare=sandboxed_spawn_argv,
                )
            except RuntimeError as exc:
                _audit_provider_cli(executable, "denied", "sandbox_rejected")
                raise SourceProviderError(f"{executable} could not start securely: {exc}") from exc
            audit_task = asyncio.create_task(
                asyncio.to_thread(
                    _audit_provider_cli,
                    executable,
                    "invoked",
                    "dispatch",
                    critical=True,
                )
            )
            try:
                await asyncio.shield(audit_task)
            except asyncio.CancelledError:
                # The worker thread cannot be cancelled once running. Wait for
                # it to settle so an on-disk invoked event is paired with the
                # outer request_cancelled terminal event before we re-raise.
                while not audit_task.done():
                    try:
                        await asyncio.shield(audit_task)
                    except asyncio.CancelledError:
                        continue
                if audit_task.exception() is None:
                    invoked = True
                raise
            except Exception as exc:
                raise SourceProviderError("provider audit unavailable") from exc
            invoked = True
            try:
                proc = await create_subprocess_limited(
                    *wrapped_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=platform_compat.IS_POSIX,
                    creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                )
            except OSError as exc:
                raise SourceProviderError(f"{executable} could not start") from exc
            stdout, stderr = await _collect_process_output(proc, executable, max_output_bytes)
        result = parse(proc.returncode if proc.returncode is not None else -1, stdout, stderr)
    except asyncio.CancelledError:
        if invoked:
            _audit_provider_cli(executable, "failed", "request_cancelled")
        raise
    except SourceProviderError:
        if invoked:
            _audit_provider_cli(executable, "failed", "provider_error")
        raise
    except Exception:
        if invoked:
            _audit_provider_cli(executable, "failed", "internal_error")
        raise
    finally:
        if cleanup_path:
            with contextlib.suppress(OSError):
                os.unlink(cleanup_path)
    _audit_provider_cli(executable, "completed", "success")
    return result


def _or_empty(value: Any) -> Any:
    """Coerce an already-recorded gather failure into an empty section."""
    if isinstance(value, BaseException):
        return []
    return value


def _mark_partial(partial_sections: list[str], section: str) -> None:
    """Add a partial-result section once while preserving display order."""
    if section not in partial_sections:
        partial_sections.append(section)


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce *value* to a dict, returning an empty dict for non-dict inputs."""
    return value if isinstance(value, dict) else {}


def _redact_provider_data(value: Any) -> Any:
    """Recursively redact secrets and suspicious URLs in provider-controlled data."""
    if isinstance(value, str):
        value = redact_exfiltration_urls(value)[0]
        return redact_credentials(value)[0]
    if isinstance(value, list):
        return [_redact_provider_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_provider_data(item) for key, item in value.items()}
    return value


def _payload_size_bytes(data: dict[str, Any]) -> int:
    """Return the compact UTF-8 JSON size used for response and cache bounds."""
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _author(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("login") or value.get("username") or value.get("name") or "")
    return str(value or "")


# --- Shared chip-status projection ------------------------------------------
#
# The sidebar chips and the detail panel derive the same {state, ci} chip
# projection from two different provider reads (a lightweight chip fetch and a
# full payload). The two caches are mutually invalidating, so the
# invariant "both projections agree" is load-bearing: any vocabulary drift turns
# a common steady state into a sustained cache ping-pong. To make drift
# structurally impossible rather than convention-enforced, BOTH paths route
# every raw provider value through the single functions below. Do not inline a
# second copy of this vocabulary anywhere.


def _rollup_ci(buckets: list[str]) -> str | None:
    """Roll per-check buckets up to a single chip CI value (or ``None``)."""
    if not buckets:
        return None
    if "failed" in buckets:
        return "failed"
    if "pending" in buckets:
        return "running"
    return "passed"


def _project_state(raw_state: str, *, draft: bool) -> str | None:
    """Map a provider PR/MR lifecycle state to the chip ``state`` vocabulary.

    GitHub reports OPEN/MERGED/CLOSED; GitLab opened/merged/closed/locked. A
    ``draft`` flag only means "draft" while the PR is still open — GitLab keeps
    ``draft: true`` on an MR closed while in draft, so the draft mapping must be
    gated on the open state or the two paths diverge (chip "draft" vs full
    "closed") and ping-pong forever.

    ``locked`` is GitLab's *transient* state while a merge is in progress — it
    is not a terminal lifecycle. Mapping it to ``closed`` painted a false
    "closed" glyph on an MR that is actually mid-merge and conflicted with the
    detail panel's own locked handling. Project nothing for it (both paths agree
    on "no lifecycle change") and let the next read resolve to merged/closed once
    GitLab settles.
    """
    state = raw_state.lower()
    if state in {"open", "opened"}:
        return "draft" if draft else "open"
    if state == "merged":
        return "merged"
    if state == "closed":
        return "closed"
    return None


def _chip_state(status: dict[str, str] | None) -> str:
    """The chip-vocabulary lifecycle of a cached chip status, ``""`` when unknown."""
    return str(status.get("state") or "") if status else ""


def _lifecycle_ttl(state: str) -> float:
    """Retention for a chip-vocabulary lifecycle: merged, closed, or anything else."""
    if state == "merged":
        return _TERMINAL_TTL_SECS
    if state == "closed":
        return _CLOSED_TTL_SECS
    return _CACHE_TTL_SECS


def _full_payload_ttl(payload: dict[str, Any]) -> float:
    """How long a cached full payload stays fresh WITHOUT revalidation, by the
    lifecycle it describes.

    Decided from the payload itself (through the same ``_project_state`` the chip
    projection uses) rather than from the chip cache, so the two caches cannot
    disagree about whether a URL is finished. A github.com payload past the open
    TTL is revalidated instead of served on this clock (``fetch_pull_request``).
    """
    state = _project_state(str(payload.get("state") or ""), draft=bool(payload.get("draft")))
    return _lifecycle_ttl(state or "")


def _chip_refresh_due(
    entry: tuple[float, dict[str, str] | None] | None, now: float, *, force: bool
) -> bool:
    """Whether a chip-cache entry has aged out of its TTL.

    ``force`` (a turn boundary) bypasses the TTL for every lifecycle except
    ``merged``: a PR the agent may have just reopened is worth a forced read, a
    merged one cannot change and would only spend a provider subprocess per turn
    for as long as its chip stays in the sidebar.
    """
    if entry is None:
        return True
    stamped_at, status = entry
    state = _chip_state(status)
    if state == "merged":
        return now - stamped_at >= _TERMINAL_TTL_SECS
    if force:
        return True
    ttl = _lifecycle_ttl(state) if state in _TERMINAL_CHIP_STATES else _CHECK_TTL_SECS
    return now - stamped_at >= ttl


def _gitlab_status_bucket(status: str) -> str:
    """Map a single GitLab *job* status to a check bucket for the Checks list.

    This is the per-job display vocabulary and is deliberately FAITHFUL: a job
    that failed buckets as ``failed`` even when it is ``allow_failure`` (GitLab
    shows such a job as failed-but-allowed, and hiding it as ``skipped`` made the
    Checks tab claim "all checks passed" while a job was red). The single CI
    glyph is NOT rolled up from these job buckets for GitLab — it comes from the
    pipeline aggregate via ``_gitlab_aggregate_ci`` — so a faithful failed job
    here never diverges the chip from the full-payload projection.
    """
    state = status.lower()
    if state in {"success", "passed"}:
        return "passed"
    if state in {"skipped", "manual"}:
        return "skipped"
    if state in {"failed", "canceled", "cancelled"}:
        return "failed"
    return "pending"


def _gitlab_aggregate_ci(status: str) -> str | None:
    """Project a GitLab *pipeline aggregate* status to the chip CI vocabulary.

    Single source of truth for the GitLab CI glyph, called by BOTH the chip path
    (which reads the pipeline aggregate directly) and the full-payload path (via
    ``ciStatus`` stamped by ``_fetch_gitlab``). Because both sides call this one
    function on the same aggregate value, the two projections cannot drift by
    construction — regardless of how the vocabulary is mapped below.

    The aggregate is authoritative and lossless for the glyph: GitLab folds
    ``allow_failure`` failures into a ``success`` aggregate, so an allowed
    failure correctly reads "passed" here while its job still shows failed in the
    Checks list. ``manual`` is a *blocking* gate (the pipeline is waiting on a
    manual action), so it maps to ``running`` — not ``passed`` — because work is
    still outstanding.
    """
    state = status.lower()
    if state in {"success", "passed"}:
        return "passed"
    if state in {"failed", "canceled", "cancelled"}:
        return "failed"
    if state == "skipped":
        # A wholly skipped pipeline has no failures and nothing outstanding.
        return "passed"
    if not state:
        return None
    # running / pending / created / scheduled / preparing / waiting_for_resource
    # and manual (a blocking manual gate is still outstanding work).
    return "running"


def _github_check(item: dict[str, Any]) -> dict[str, Any]:
    conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
    status = str(item.get("status") or "").upper()
    if status and status != "COMPLETED":
        bucket = "pending"
    elif conclusion in {"SUCCESS", "NEUTRAL"}:
        bucket = "passed"
    elif conclusion in {"SKIPPED", "STALE"}:
        bucket = "skipped"
    elif conclusion in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"}:
        bucket = "failed"
    else:
        bucket = "pending"
    return {
        "name": item.get("name") or item.get("context") or "Check",
        "workflow": item.get("workflowName") or "",
        "status": status,
        "conclusion": conclusion,
        "bucket": bucket,
        "url": item.get("detailsUrl") or item.get("targetUrl") or "",
        "startedAt": item.get("startedAt") or "",
        "completedAt": item.get("completedAt") or "",
    }


def _github_check_identity(
    item: dict[str, Any], check: dict[str, Any], index: int
) -> tuple[str, ...]:
    """The identity two rollup rows must share to be the same check.

    NOT the display name alone. ``workflow`` separates two workflows that publish
    a check with the same job name — including every matrix leg of an Actions
    job, since GitHub appends the matrix values to the check-run name even when
    the workflow sets an explicit ``name:`` (``Backend Tests (3.12, 4)``), so
    sibling shards never share an identity and one shard's failure can never be
    folded into another's success.

    The row KIND separates GitHub's two rollup shapes. ``__typename`` comes
    straight from the GraphQL union and is present on every row ``gh`` returns; a
    row without it (a hand-built dict) is classified from the fields each shape
    carries — ``status``/``conclusion`` are check-run-only and ``context`` is
    status-only. Do NOT discriminate on the absence of ``name``: a status row
    carrying both ``context`` and ``name`` would be read as a check-run and
    collide with a nameless one, which ``_github_check`` normalizes to the same
    ``"Check"`` placeholder.

    A check-run with NO workflow (published by an app outside Actions) is left
    deliberately UNCOLLAPSED — its per-row detail URL, else its position, joins
    the identity. Such rows are the one case this payload cannot adjudicate: the
    requested ``statusCheckRollup`` fields carry no check-suite or run-attempt
    id, so a superseded re-run is indistinguishable from a same-named check from
    a different app. Over-counting a re-run is a cosmetic miss; collapsing two
    apps would hide a real failure behind the other's later success, so the tie
    breaks toward never hiding red.
    """
    kind = str(item.get("__typename") or "")
    if not kind:
        status_shaped = "context" in item and not ("status" in item or "conclusion" in item)
        kind = "StatusContext" if status_shaped else "CheckRun"
    if kind == "CheckRun" and not check["workflow"]:
        return (kind, "", check["name"], check["url"] or f"#{index}")
    return (kind, check["workflow"], check["name"])


def _github_check_rank(check: dict[str, Any]) -> tuple[str, str]:
    """Recency key for two rows that share a check identity.

    ``startedAt`` leads: an OLDER run that finished must not outrank a NEWER one
    that is still going (no ``completedAt`` yet), which is exactly what comparing
    ``completedAt`` first would do — the panel would show a stale pass while its
    replacement was mid-flight. GitHub leaves ``startedAt`` null while a check-run
    is still QUEUED, so a started-less row that is still outstanding sorts above
    every timestamp instead of losing to the completed run it supersedes.
    """
    started = str(check.get("startedAt") or "")
    if not started and check.get("bucket") == "pending":
        return ("\uffff", "")
    return (started, str(check.get("completedAt") or ""))


def _github_checks(rollup: list[Any]) -> list[dict[str, Any]]:
    """Project GitHub's status-check rollup, keeping only the LATEST run per check.

    ``statusCheckRollup`` returns EVERY check-run recorded against the head sha,
    not one per check. The same workflow file can be dispatched twice for one sha
    (a push immediately followed by an edit event, say), producing two check
    suites whose jobs each contribute a row — and a concurrency group cancels the
    first suite, so the loser lands as ``CANCELLED``. Rendering the raw rollup
    therefore (a) inflated the totals the panel reports (observed 49 rows where
    GitHub's own UI counted 41) and (b) let a superseded ``CANCELLED`` row roll up
    to a red CI glyph on a pull request whose replacement run passed — a red that
    no amount of refreshing could clear, because the stale row is genuinely still
    in the provider payload.

    GitHub's UI collapses each check to its latest run; mirror that. Identity
    comes from ``_github_check_identity``, which is deliberately conservative:
    anything it cannot prove is the same check stays its own row, because
    over-counting is cosmetic while collapsing two distinct checks would hide a
    failure behind another's success.

    First-appearance order is preserved (dict insertion order survives value
    replacement) so a re-run does not reshuffle the list under the caller.
    """
    best: dict[tuple[str, ...], dict[str, Any]] = {}
    for index, item in enumerate(rollup):
        if not isinstance(item, dict):
            continue
        check = _github_check(item)
        identity = _github_check_identity(item, check, index)
        previous = best.get(identity)
        if previous is None or _github_check_rank(check) >= _github_check_rank(previous):
            best[identity] = check
    return list(best.values())


def _github_comment(item: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or item.get("databaseId") or ""),
        "kind": kind,
        "author": _author(item.get("author") or item.get("user")),
        "body": item.get("body") or "",
        "state": item.get("state") or "",
        "createdAt": item.get("createdAt")
        or item.get("submittedAt")
        or item.get("created_at")
        or "",
        "url": item.get("url") or item.get("html_url") or "",
        "path": item.get("path") or "",
        "line": item.get("line") or item.get("original_line"),
        "threadId": "",
        "resolvable": False,
        "resolved": False,
    }


_GITHUB_REVIEW_THREADS_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!)"
    "{repository(owner:$owner,name:$repo)"
    "{pullRequest(number:$number)"
    "{reviewThreads(first:100){nodes{id isResolved "
    "comments(first:10){nodes{databaseId}}}}}}}"
)


def _github_thread_ids(payload: Any) -> set[str]:
    """Return review-thread IDs scoped to the queried pull request."""
    if not isinstance(payload, dict):
        return set()
    try:
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return set()
    return {str(node["id"]) for node in _as_list(nodes) if node.get("id")}


def _github_thread_map(payload: Any) -> dict[str, dict[str, Any]]:
    """Map an inline comment databaseId to its review thread id and state."""
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, dict):
        return result
    try:
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return result
    for node in _as_list(nodes):
        thread_id = node.get("id")
        if not thread_id:
            continue
        is_resolved = bool(node.get("isResolved"))
        comments = node.get("comments")
        comment_nodes = comments.get("nodes") if isinstance(comments, dict) else []
        for comment in _as_list(comment_nodes):
            database_id = comment.get("databaseId")
            if database_id is None:
                continue
            result[str(database_id)] = {
                "threadId": str(thread_id),
                "resolved": is_resolved,
            }
    return result


# Both providers compute mergeability lazily: reading a pull request that has
# not been evaluated recently returns "not known yet" (GitHub ``UNKNOWN``,
# GitLab ``checking``/``unchecked``) *and* kicks off the computation, so the
# real answer is only available on a later read. A single read therefore reports
# a conflicting pull request as having no merge blocker at all — which is why
# the panel's conflict banner used to appear only once the user hit refresh.
# These bound a short re-read of the merge fields alone (not the whole fanout),
# issued concurrently with the secondary provider calls so most of the wait is
# absorbed by work the request was already doing.
_MERGE_STATE_REREADS = 2
_MERGE_STATE_REREAD_DELAY_SECS = 0.8
# The one normalized value that means "the provider has not answered yet". It is
# shared by both fields of the merge pair and by both providers.
_UNSETTLED_MERGE_STATE = "unknown"


def _merge_state_real(value: str) -> bool:
    """Whether one normalized merge field carries a real answer."""
    return bool(value) and value != _UNSETTLED_MERGE_STATE


def _merge_state_settled(mergeable: str, merge_state: str) -> bool:
    """Whether a normalized merge pair is a real answer, so no re-read is due.

    A pair is settled once **either** field is real. GitLab reports `need_rebase`
    and its branch-protection gates with ``mergeable == 'unknown'`` — the detail
    IS the answer there, so keying only on ``mergeable`` would re-read a state
    the provider had already settled and then discard it. A pair that is empty
    rather than unknown means the provider did not report the fields at all, so
    re-reading cannot settle it either.
    """
    if mergeable == _UNSETTLED_MERGE_STATE or merge_state == _UNSETTLED_MERGE_STATE:
        return _merge_state_real(mergeable) or _merge_state_real(merge_state)
    return True


_MERGE_STATE_FIELDS = ("mergeable", "mergeStateStatus")
# Lifecycle states for which a merge answer is still meaningful. Once a source is
# merged or closed the providers stop answering the merge pair at all, so a
# carried-forward value could never be cleared again.
_MERGE_STATE_LIVE_STATES = frozenset({"open", "draft"})


def _keep_known_merge_state(
    status: dict[str, str], previous: dict[str, str] | None
) -> dict[str, str]:
    """Carry a settled merge field forward when a fresh read has no answer yet.

    ``_record_merge_state`` omits a field the provider has not settled, on the
    principle that "still computing" must never be published as a real answer.
    That is necessary but not sufficient: every writer replaces the chip entry
    WHOLESALE, so an omitted field does not read as "no news" downstream — it
    erases whatever the previous entry had settled.

    That matters because an unsettled read is the COMMON case, not a rare one:
    both providers compute mergeability lazily, so a poll that arrives after the
    provider's evaluation lapsed returns ``unknown`` for a source whose conflict
    is already known. Without this carry-forward, such a poll drops the merge
    pair, which (a) removes it from the owner-gated sidebar payload that spreads
    the entry whole, and (b) reads as a CHANGED chip status, dropping the full
    payload and emitting a delta — whose refetch re-projects the real answer
    straight back into the cache. That is the repeating chip<->full transition
    ``_CHECK_FLAP_DAMP_THRESHOLD`` exists to contain, so the banner would survive
    only until the damper tripped and then go stale.

    Mirrors the same keep-known rule already applied to the ``ci`` glyph. A real
    answer always wins, including one that CHANGES the value, so this only ever
    fills a gap and cannot pin a stale verdict. Carry-forward stops once the
    source leaves an open state, where the pair is both meaningless and
    permanently unanswered.
    """
    if not previous:
        return status
    if status.get("state", "open") not in _MERGE_STATE_LIVE_STATES:
        return status
    carried = {
        field: previous[field]
        for field in _MERGE_STATE_FIELDS
        if field not in status and field in previous
    }
    return {**status, **carried} if carried else status


def _github_merge_state(details: dict[str, Any]) -> tuple[str, str]:
    """Normalize GitHub merge fields to (mergeable, mergeStateStatus).

    ``mergeable`` is one of ``mergeable`` / ``conflicting`` / ``unknown`` and
    ``mergeStateStatus`` is GitHub's merge-state vocabulary lowercased
    (``clean``, ``dirty``, ``behind``, ``blocked``, ``unstable``, ...).
    """
    raw_mergeable = str(details.get("mergeable") or "").upper()
    if raw_mergeable == "MERGEABLE":
        mergeable = "mergeable"
    elif raw_mergeable == "CONFLICTING":
        mergeable = "conflicting"
    else:
        mergeable = "unknown" if raw_mergeable else ""
    return mergeable, str(details.get("mergeStateStatus") or "").lower()


# GitLab detailed_merge_status values mapped onto the GitHub-style
# merge-state vocabulary the frontend renders.
_GITLAB_MERGE_STATE_MAP = {
    "mergeable": "clean",
    "conflict": "dirty",
    # need_rebase keeps its own value: on fast-forward-only projects a merge
    # commit cannot unblock the MR, so it must not be conflated with "behind".
    "need_rebase": "need_rebase",
    "ci_must_pass": "blocked",
    "ci_still_running": "unstable",
    "discussions_not_resolved": "blocked",
    "not_approved": "blocked",
    "blocked_status": "blocked",
    "external_status_checks": "blocked",
    "jira_association_missing": "blocked",
    "requested_changes": "blocked",
    "status_checks_must_pass": "blocked",
    "policies_denied": "blocked",
    "security_policy_violations": "blocked",
    "merge_request_blocked": "blocked",
    "draft_status": "draft",
}


def _gitlab_merge_state(details: dict[str, Any]) -> tuple[str, str]:
    """Normalize GitLab merge fields to (mergeable, mergeStateStatus).

    ``detailed_merge_status`` is authoritative; the deprecated legacy
    ``merge_status`` is consulted only when the detailed field is absent
    (it can be stale or coarse and must never override the detailed value).
    """
    detailed = str(details.get("detailed_merge_status") or "").lower()
    legacy = str(details.get("merge_status") or "").lower()
    if detailed:
        if detailed == "conflict":
            mergeable = "conflicting"
        elif detailed == "mergeable":
            mergeable = "mergeable"
        else:
            mergeable = "unknown"
        return mergeable, _GITLAB_MERGE_STATE_MAP.get(detailed, "unknown")
    if legacy == "cannot_be_merged":
        mergeable = "conflicting"
    elif legacy == "can_be_merged":
        mergeable = "mergeable"
    else:
        mergeable = "unknown" if legacy else ""
    return mergeable, ""


async def _github_settled_merge_state(ref: SourceRef, details: dict[str, Any]) -> tuple[str, str]:
    """Merge state for a GitHub PR, re-reading while it is still being computed.

    Re-reads only ``mergeable``/``mergeStateStatus``, at most
    ``_MERGE_STATE_REREADS`` times. A failed or still-unsettled re-read keeps the
    original value rather than raising: an unknown merge state degrades one
    banner, and must never fail the whole panel.
    """
    mergeable, merge_state = _github_merge_state(details)
    if _merge_state_settled(mergeable, merge_state):
        return mergeable, merge_state
    for _ in range(_MERGE_STATE_REREADS):
        await asyncio.sleep(_MERGE_STATE_REREAD_DELAY_SECS)
        try:
            data = await _run_json(
                "gh", "pr", "view", ref.url, "--json", "mergeable,mergeStateStatus"
            )
        except SourceProviderError:
            break
        if not isinstance(data, dict):
            break
        reread, reread_state = _github_merge_state(data)
        if _merge_state_settled(reread, reread_state):
            return reread, reread_state
    return mergeable, merge_state


async def _gitlab_settled_merge_state(
    ref: SourceRef, mr_api: str, details: dict[str, Any]
) -> tuple[str, str]:
    """Merge state for a GitLab MR, re-reading while it is still being computed.

    GitLab exposes ``detailed_merge_status`` only on the merge-request endpoint,
    so the re-read repeats that request and takes the merge fields from it. Same
    failure posture as the GitHub path: degrade to the original value.
    """
    mergeable, merge_state = _gitlab_merge_state(details)
    if _merge_state_settled(mergeable, merge_state):
        return mergeable, merge_state
    for _ in range(_MERGE_STATE_REREADS):
        await asyncio.sleep(_MERGE_STATE_REREAD_DELAY_SECS)
        try:
            data = await _run_json("glab", "api", mr_api)
        except SourceProviderError:
            break
        if not isinstance(data, dict):
            break
        reread, reread_state = _gitlab_merge_state(data)
        if _merge_state_settled(reread, reread_state):
            return reread, reread_state
    return mergeable, merge_state


async def _fetch_github(ref: SourceRef) -> dict[str, Any]:
    # `statusCheckRollup` is deliberately ABSENT from this field set: `gh`
    # resolves a `--json` field set atomically, so bundling the rollup (which
    # needs Checks read access that fine-grained tokens commonly lack) would
    # fail the whole panel read over the one section the token cannot see. The
    # rollup rides a separate degradable read below (#5115).
    fields = ",".join(
        [
            "additions",
            "author",
            "autoMergeRequest",
            "baseRefName",
            "body",
            "changedFiles",
            "comments",
            "commits",
            "deletions",
            "headRefName",
            "headRefOid",
            "isDraft",
            "mergeStateStatus",
            "mergeable",
            "mergedAt",
            "number",
            "reviews",
            "state",
            "title",
            "updatedAt",
            "url",
        ]
    )
    repo_api = f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
    details = await _run_json("gh", "pr", "view", ref.url, "--json", fields)
    if not isinstance(details, dict):
        raise SourceProviderError("GitHub returned an invalid pull-request payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    files_raw: Any
    review_comments_raw: Any
    review_threads_raw: Any
    merge_state_raw: Any
    rollup_raw: Any
    (
        files_raw,
        review_comments_raw,
        review_threads_raw,
        merge_state_raw,
        rollup_raw,
    ) = await asyncio.gather(
        _run_json(
            "gh",
            "api",
            f"{repo_api}/files?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DIFF_OUTPUT_BYTES,
        ),
        _run_json(
            "gh",
            "api",
            f"{repo_api}/comments?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={ref.owner}",
            "-f",
            f"repo={ref.repo}",
            "-F",
            f"number={ref.number}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        # Runs alongside the secondary calls so its re-read wait overlaps with
        # fetches this request was making anyway.
        _github_settled_merge_state(ref, details),
        _github_rollup_read(ref),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(files_raw, BaseException):
        _mark_partial(partial_sections, "files")
    if isinstance(review_comments_raw, BaseException) or isinstance(
        review_threads_raw, BaseException
    ):
        _mark_partial(partial_sections, "inline review comments")
    checks: list[dict[str, Any]] = []
    if isinstance(rollup_raw, BaseException):
        # The rollup is read separately from the core fields precisely so a
        # token without Checks read access (or a transient rollup failure)
        # costs the checks SECTION, never the panel. Name it in
        # `partialSections` so the empty list cannot read as "no checks": the
        # frontend banner surfaces the degraded section, and
        # `record_full_payload_status` keeps a known CI glyph alive while
        # `checks` is partial instead of erasing it.
        _mark_partial(partial_sections, "checks")
    else:
        rollup_checks, rollup_head = rollup_raw
        head_oid = str(details.get("headRefOid") or "")
        # A missing sha on either side DELIBERATELY fails open (accepts the
        # rollup): treating it as unverifiable would degrade every read where
        # the provider omits the field, which is worse than the narrow race
        # this guard exists for.
        if head_oid and rollup_head and rollup_head != head_oid:
            # The core read and the rollup read straddled a push: these checks
            # describe a different commit than the rest of the payload. Mark
            # the section unavailable rather than pin another head's CI to
            # this one; the next refresh re-pairs them.
            _mark_partial(partial_sections, "checks")
        else:
            checks = rollup_checks
    files = _or_empty(files_raw)
    review_comments = _or_empty(review_comments_raw)
    thread_map = _github_thread_map(_or_empty(review_threads_raw))
    file_rows = _as_list(files)
    review_comment_rows = _as_list(review_comments)
    changed_files = details.get("changedFiles")
    if (isinstance(changed_files, int) and changed_files > len(file_rows)) or (
        not isinstance(changed_files, int) and len(file_rows) >= _SECONDARY_PAGE_SIZE
    ):
        _mark_partial(partial_sections, "files")
    # GitHub's review-comment endpoint does not expose its total in the
    # primary payload. A full page means another page may exist.
    if len(review_comment_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "inline review comments")

    inline_comments = [_github_comment(item, "inline") for item in review_comment_rows]
    for comment in inline_comments:
        info = thread_map.get(comment["id"])
        if info:
            comment["threadId"] = info["threadId"]
            comment["resolved"] = info["resolved"]
            comment["resolvable"] = True

    comments = [
        *(_github_comment(item, "comment") for item in _as_list(details.get("comments"))),
        *(_github_comment(item, "review") for item in _as_list(details.get("reviews"))),
        *inline_comments,
    ]
    commits = []
    for item in _as_list(details.get("commits")):
        authors = _as_list(item.get("authors"))
        commits.append(
            {
                "sha": item.get("oid") or "",
                "title": item.get("messageHeadline") or "",
                "body": item.get("messageBody") or "",
                "author": _author(authors[0]) if authors else "",
                "date": item.get("committedDate") or item.get("authoredDate") or "",
                "url": (
                    f"https://github.com/{ref.owner}/{ref.repo}/commit/{item.get('oid')}"
                    if item.get("oid")
                    else ""
                ),
            }
        )

    normalized_files = []
    for item in _as_list(files):
        normalized_files.append(
            {
                "path": item.get("filename") or "",
                "status": item.get("status") or "modified",
                "additions": item.get("additions") or 0,
                "deletions": item.get("deletions") or 0,
                "patch": item.get("patch") or "",
            }
        )

    github_mergeable, github_merge_state = (
        merge_state_raw if isinstance(merge_state_raw, tuple) else _github_merge_state(details)
    )
    return {
        "provider": "github",
        # Identity comes from the VALIDATED ref, never the provider echo: the
        # browser submits this url back for refresh/resolve, so a compromised or
        # hostile instance echoing a different web_url could otherwise steer an
        # owner-authenticated mutation at an unrelated pull/merge request.
        "url": ref.url,
        "number": ref.number,
        "title": details.get("title") or "",
        "description": details.get("body") or "",
        "state": details.get("state") or "",
        "draft": bool(details.get("isDraft")),
        "mergedAt": details.get("mergedAt") or "",
        "mergeable": github_mergeable,
        "mergeStateStatus": github_merge_state,
        "autoMerge": bool(details.get("autoMergeRequest")),
        "updatedAt": details.get("updatedAt") or "",
        "headBranch": details.get("headRefName") or "",
        "baseBranch": details.get("baseRefName") or "",
        "headSha": details.get("headRefOid") or "",
        "author": _author(details.get("author")),
        "additions": details.get("additions") or 0,
        "deletions": details.get("deletions") or 0,
        "changedFiles": details.get("changedFiles") or len(normalized_files),
        "commits": commits,
        "checks": checks,
        "comments": comments,
        "files": normalized_files,
        "partialSections": partial_sections,
    }


def _gitlab_pipeline_as_check(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Represent a whole pipeline as a single check row.

    A pipeline standing in for its jobs must keep PIPELINE-level semantics: a
    ``manual`` pipeline is blocked on a required job, so it is marked as a
    required gate (``allow_failure: False``) rather than falling through to the
    job-level reading that treats a lone manual step as skipped -- which the
    frontend would then roll up as passed.
    """
    record = {**pipeline, "name": "Pipeline"}
    if str(pipeline.get("status") or "").lower() == "manual":
        record["allow_failure"] = False
    return _gitlab_check(record)


def _gitlab_check(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "").lower()
    bucket = _gitlab_status_bucket(status)
    if status == "manual" and item.get("allow_failure") is False:
        # A required manual job is an unsatisfied gate, not an optional step that
        # can be treated as skipped: rolling it up as passed would show green
        # while the pipeline is still blocked on a human action.
        bucket = "pending"
    return {
        "name": item.get("name") or "Job",
        "workflow": item.get("stage") or "",
        "status": status.upper(),
        "conclusion": status.upper(),
        "bucket": bucket,
        "url": item.get("web_url") or "",
        "startedAt": item.get("started_at") or "",
        "completedAt": item.get("finished_at") or "",
    }


async def _fetch_gitlab(ref: SourceRef) -> dict[str, Any]:
    project = quote(ref.project, safe="")
    mr_api = f"projects/{project}/merge_requests/{ref.number}"
    details = await _run_json("glab", "api", mr_api, host=ref.host)
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid merge-request payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    commits_raw: Any
    discussions_raw: Any
    changes_raw: Any
    pipelines_raw: Any
    merge_state_raw: Any
    commits_raw, discussions_raw, changes_raw, pipelines_raw, merge_state_raw = (
        await asyncio.gather(
            _run_json(
                "glab", "api", f"{mr_api}/commits?per_page={_SECONDARY_PAGE_SIZE}", host=ref.host
            ),
            _run_json(
                "glab",
                "api",
                f"{mr_api}/discussions?per_page={_SECONDARY_PAGE_SIZE}",
                max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
                host=ref.host,
            ),
            _run_json(
                "glab",
                "api",
                f"{mr_api}/changes",
                max_output_bytes=_DIFF_OUTPUT_BYTES,
                host=ref.host,
            ),
            _run_json("glab", "api", f"{mr_api}/pipelines?per_page=20", host=ref.host),
            # Runs alongside the secondary calls so its re-read wait overlaps
            # with fetches this request was making anyway.
            _gitlab_settled_merge_state(ref, mr_api, details),
            return_exceptions=True,
        )
    )
    partial_sections: list[str] = []
    for raw_value, section in (
        (commits_raw, "commits"),
        (discussions_raw, "review discussions"),
        (changes_raw, "files"),
        (pipelines_raw, "checks"),
    ):
        if isinstance(raw_value, BaseException):
            _mark_partial(partial_sections, section)
    commits = _or_empty(commits_raw)
    discussions = _or_empty(discussions_raw)
    changes = _or_empty(changes_raw)
    pipelines = _or_empty(pipelines_raw)
    commit_rows = _as_list(commits)
    discussion_rows = _as_list(discussions)
    if len(commit_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "commits")
    if len(discussion_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "review discussions")

    jobs: Any = []
    pipeline_rows = _as_list(pipelines)
    if pipeline_rows and pipeline_rows[0].get("id"):
        try:
            jobs = await _run_json(
                "glab",
                "api",
                f"projects/{project}/pipelines/{pipeline_rows[0]['id']}/jobs?per_page={_SECONDARY_PAGE_SIZE}",
                host=ref.host,
            )
        except SourceProviderError:
            _mark_partial(partial_sections, "checks")
            jobs = []
    if len(_as_list(jobs)) >= _SECONDARY_PAGE_SIZE:
        # A full page of jobs may be truncated — a failed job on a later page
        # would be invisible in this list. The CI glyph is projected from the
        # pipeline AGGREGATE (`ciStatus` below), which stays authoritative
        # regardless, but flag the Checks LIST as partial so the panel does not
        # imply it is exhaustive.
        _mark_partial(partial_sections, "checks")

    raw_changes = changes.get("changes") if isinstance(changes, dict) else []
    change_rows = _as_list(raw_changes)
    reported_change_count = str(details.get("changes_count") or "").rstrip("+")
    if (isinstance(changes, dict) and changes.get("overflow")) or (
        reported_change_count.isdigit() and int(reported_change_count) > len(change_rows)
    ):
        _mark_partial(partial_sections, "files")
    normalized_files = []
    for item in change_rows:
        if item.get("deleted_file"):
            status = "deleted"
        elif item.get("new_file"):
            status = "added"
        elif item.get("renamed_file"):
            status = "renamed"
        else:
            status = "modified"
        patch = item.get("diff") or ""
        normalized_files.append(
            {
                "path": item.get("new_path") or item.get("old_path") or "",
                "status": status,
                "additions": sum(
                    1
                    for line in patch.splitlines()
                    if line.startswith("+") and not line.startswith("+++")
                ),
                "deletions": sum(
                    1
                    for line in patch.splitlines()
                    if line.startswith("-") and not line.startswith("---")
                ),
                "patch": patch,
            }
        )

    gitlab_comments = []
    for discussion in _as_list(discussions):
        thread_id = str(discussion.get("id") or "")
        for note in _as_list(discussion.get("notes")):
            if note.get("system"):
                continue
            gitlab_comments.append(
                {
                    "id": str(note.get("id") or ""),
                    "kind": "comment",
                    "author": _author(note.get("author")),
                    "body": note.get("body") or "",
                    "state": "",
                    "createdAt": note.get("created_at") or "",
                    "url": "",
                    "path": "",
                    "line": None,
                    "threadId": thread_id,
                    "resolvable": bool(note.get("resolvable")),
                    "resolved": bool(note.get("resolved")),
                }
            )

    gitlab_mergeable, gitlab_merge_state = (
        merge_state_raw if isinstance(merge_state_raw, tuple) else _gitlab_merge_state(details)
    )
    gitlab_checks = [_gitlab_check(item) for item in _as_list(jobs)]
    # The single CI glyph is projected from the pipeline AGGREGATE (authoritative
    # and lossless — GitLab folds allow_failure into it and marks a blocking
    # manual gate), NOT rolled up from the per-job buckets, so a truncated /
    # empty / allow_failure job list can never diverge the glyph from the chip
    # path (which reads the same aggregate). `checks` below is a faithful
    # display list only.
    pipeline_status = str(pipeline_rows[0].get("status") or "") if pipeline_rows else ""
    gitlab_ci = _gitlab_aggregate_ci(pipeline_status)
    if not gitlab_checks and pipeline_rows and pipeline_status and "checks" not in partial_sections:
        # A pipeline EXISTS but its jobs have not materialized yet (freshly
        # created pipeline). Synthesize a single "Pipeline" row from the
        # aggregate so the Checks tab is not empty while jobs spin up — a
        # pipeline-less MR keeps `checks` empty. Display-only; the glyph comes
        # from `ciStatus`.
        gitlab_checks = [_gitlab_check({**pipeline_rows[0], "name": "Pipeline"})]
    payload: dict[str, Any] = {
        "provider": "gitlab",
        # See _fetch_github: identity is the validated ref, not the provider's
        # web_url/iid. This matters most for a self-managed instance, whose
        # responses are outside the trust boundary the allowlist establishes.
        "url": ref.url,
        "number": ref.number,
        "title": details.get("title") or "",
        "description": details.get("description") or "",
        "state": details.get("state") or "",
        "draft": bool(details.get("draft") or details.get("work_in_progress")),
        "mergedAt": details.get("merged_at") or "",
        "mergeable": gitlab_mergeable,
        "mergeStateStatus": gitlab_merge_state,
        "autoMerge": bool(details.get("merge_when_pipeline_succeeds")),
        "updatedAt": details.get("updated_at") or "",
        "headBranch": details.get("source_branch") or "",
        "baseBranch": details.get("target_branch") or "",
        "headSha": details.get("sha") or "",
        "author": _author(details.get("author")),
        "additions": sum(item["additions"] for item in normalized_files),
        "deletions": sum(item["deletions"] for item in normalized_files),
        "changedFiles": len(normalized_files),
        "commits": [
            {
                "sha": item.get("id") or item.get("short_id") or "",
                "title": item.get("title") or "",
                "body": item.get("message") or "",
                "author": item.get("author_name") or "",
                "date": item.get("created_at") or item.get("committed_date") or "",
                "url": item.get("web_url") or "",
            }
            for item in commit_rows
        ],
        "checks": gitlab_checks,
        "comments": gitlab_comments,
        "files": normalized_files,
        "partialSections": partial_sections,
    }
    if gitlab_ci is not None:
        # Authoritative aggregate CI for the glyph; consumed by
        # `status_from_full_payload` so the full-payload projection matches the
        # chip path (which reads the same aggregate) exactly.
        payload["ciStatus"] = gitlab_ci
    return payload


async def _github_rollup_read(ref: SourceRef) -> tuple[list[dict[str, Any]], str]:
    """Read the check rollup ALONE, paired with the head sha it was read at.

    ``gh pr view`` resolves a ``--json`` field set atomically: one unreadable
    field fails the whole read. ``statusCheckRollup`` needs Checks read access
    that fine-grained tokens commonly lack, so it must never share a field set
    with data the token IS authorized for (#5115) — every rollup consumer
    routes through this one isolated query instead of growing its own copy.
    ``headRefOid`` rides along (core pull-request data, readable whenever the
    PR itself is) so callers that pair this read with a separate core read can
    detect the two straddling a push and refuse to render another commit's
    checks.
    """
    data = await _run_json(
        "gh",
        "pr",
        "view",
        ref.url,
        "--json",
        "statusCheckRollup,headRefOid",
        max_output_bytes=_CHECKS_OUTPUT_BYTES,
    )
    if not isinstance(data, dict):
        raise SourceProviderError("GitHub returned an invalid checks payload")
    # The panel polls the checks endpoint while checks are pending and writes
    # the result straight over the full payload's `checks`, so every consumer
    # MUST collapse identically — an uncollapsed reply would re-inflate the
    # counts and resurrect a superseded CANCELLED failure on the first poll
    # after the panel opens.
    return (
        _github_checks(_as_list(data.get("statusCheckRollup"))),
        str(data.get("headRefOid") or ""),
    )


async def _fetch_github_checks(ref: SourceRef) -> list[dict[str, Any]]:
    checks, _head = await _github_rollup_read(ref)
    return checks


async def _fetch_gitlab_checks(ref: SourceRef) -> list[dict[str, Any]]:
    project = quote(ref.project, safe="")
    mr_api = f"projects/{project}/merge_requests/{ref.number}"
    pipelines = await _run_json(
        "glab",
        "api",
        f"{mr_api}/pipelines?per_page=1",
        max_output_bytes=_CHECKS_OUTPUT_BYTES,
        host=ref.host,
    )
    pipeline_rows = _as_list(pipelines)
    if not pipeline_rows:
        return []
    pipeline = pipeline_rows[0]
    pipeline_id = pipeline.get("id")
    if not pipeline_id:
        return [_gitlab_pipeline_as_check(pipeline)]
    jobs = await _run_json(
        "glab",
        "api",
        f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page={_SECONDARY_PAGE_SIZE}",
        max_output_bytes=_CHECKS_OUTPUT_BYTES,
        host=ref.host,
    )
    job_rows = _as_list(jobs)
    if not job_rows:
        return [_gitlab_pipeline_as_check(pipeline)]
    return [_gitlab_check(item) for item in job_rows]


async def _fetch_pull_request_checks_uncached(ref: SourceRef) -> list[dict[str, Any]]:
    plugin = _plugin_for_change(ref)
    if plugin is not None:
        try:
            with _plugin_errors(plugin.id):
                fetched = await plugin.fetch_checks(ref)
        except SourceProviderNotConfigured as exc:
            raise _plugin_setup_error(plugin, exc) from exc
    else:
        fetched = await (
            _fetch_github_checks(ref) if ref.provider == "github" else _fetch_gitlab_checks(ref)
        )
    checks = _redact_provider_data(fetched)
    if not isinstance(checks, list):
        raise SourceProviderError("provider returned an invalid checks payload")
    payload = {"checks": checks}
    if _payload_size_bytes(payload) > _MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider checks payload was too large")
    return checks


# --- Issues -----------------------------------------------------------------
#
# Issues reuse the pull-request transport wholesale (`_run_json` isolation,
# redaction, byte caps, the validated-ref identity rule) and add only their own
# normalization. They deliberately do NOT touch the chip-status cache: an issue
# has no CI or merge state, so `record_full_payload_status` is never called for
# one and `get_cached_check_status` is never consulted for one either.

# Contract order for the reaction counters, paired with GitHub's own REST keys.
_GITHUB_REACTION_KEYS: tuple[tuple[str, str], ...] = (
    ("plus1", "+1"),
    ("minus1", "-1"),
    ("laugh", "laugh"),
    ("hooray", "hooray"),
    ("confused", "confused"),
    ("heart", "heart"),
    ("rocket", "rocket"),
    ("eyes", "eyes"),
)


def _int_or_zero(value: Any) -> int:
    """Coerce a provider-supplied count to a non-negative int."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _safe_https_url(value: Any) -> str:
    """Keep only an https URL from provider-echoed link fields.

    Unlike the payload's own ``url`` (which comes from the validated ref), a
    linked change or comment permalink can only come from the provider, and it
    reaches an ``href`` in the browser. Restricting it to https drops
    ``javascript:``/``data:`` and any other scheme before it can be rendered as
    a link; a rejected value degrades to an empty string, which the frontend
    renders as plain text.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if text.lower().startswith("https://") else ""


def _issue_label(item: Any) -> dict[str, str]:
    """Normalize one label to the contract's ``{name, color, description}``.

    ``color`` is a BARE six-hex-digit string: GitHub already reports it that way
    and GitLab reports ``#rrggbb``, so the leading ``#`` is stripped here rather
    than left for the frontend to handle twice. GitLab also returns plain label
    NAMES unless ``with_labels_details`` is requested, so a bare string is
    accepted as a name-only label.
    """
    if isinstance(item, str):
        return {"name": item, "color": "", "description": ""}
    if not isinstance(item, dict):
        return {"name": "", "color": "", "description": ""}
    return {
        "name": str(item.get("name") or ""),
        "color": str(item.get("color") or "").lstrip("#"),
        "description": str(item.get("description") or ""),
    }


def _issue_labels(value: Any) -> list[dict[str, str]]:
    """Normalize a provider label list, tolerating GitLab's name-only form.

    ``_as_list`` cannot be reused here: it keeps only dict rows, which would
    silently drop every label from a GitLab reply that came back as bare
    strings. Nameless rows are dropped -- there is nothing to render.
    """
    if not isinstance(value, list):
        return []
    labels = [_issue_label(item) for item in value if isinstance(item, (str, dict))]
    return [label for label in labels if label["name"]]


def _issue_milestone(value: Any) -> dict[str, str] | None:
    """Normalize a milestone, or ``None`` when the issue has none."""
    if not isinstance(value, dict):
        return None
    return {
        "title": str(value.get("title") or ""),
        "state": str(value.get("state") or ""),
        # GitHub calls it due_on, GitLab due_date.
        "dueOn": str(value.get("due_on") or value.get("due_date") or ""),
    }


def _github_issue_reactions(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    reactions = {"total": _int_or_zero(value.get("total_count"))}
    for contract_key, github_key in _GITHUB_REACTION_KEYS:
        reactions[contract_key] = _int_or_zero(value.get(github_key))
    return reactions


def _gitlab_issue_reactions(details: dict[str, Any]) -> dict[str, int] | None:
    """Synthesize the reaction block from GitLab's up/down vote counters.

    GitLab's issue payload exposes only ``upvotes``/``downvotes``, not the full
    award-emoji breakdown, so the remaining counters stay zero rather than being
    fetched from a separate endpoint this phase does not need. ``total`` is the
    sum of what is actually known.
    """
    if "upvotes" not in details and "downvotes" not in details:
        return None
    plus1 = _int_or_zero(details.get("upvotes"))
    minus1 = _int_or_zero(details.get("downvotes"))
    reactions = {contract_key: 0 for contract_key, _ in _GITHUB_REACTION_KEYS}
    reactions["plus1"] = plus1
    reactions["minus1"] = minus1
    return {"total": plus1 + minus1, **reactions}


def _github_issue_comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or item.get("node_id") or ""),
        "author": _author(item.get("user") or item.get("author")),
        "body": str(item.get("body") or ""),
        "createdAt": str(item.get("created_at") or item.get("createdAt") or ""),
        "url": _safe_https_url(item.get("html_url") or item.get("url")),
    }


def _github_linked_changes(timeline: Any) -> list[dict[str, Any]]:
    """Cross-referenced PULL REQUESTS from an issue's timeline.

    GitHub records "this was mentioned from X" as a ``cross-referenced`` event
    whose ``source.issue`` is the mentioning item. Issues and pull requests are
    the same REST object type, distinguished only by the presence of a
    ``pull_request`` sub-object, so filtering on that key is what keeps a plain
    issue-to-issue mention out of the linked-changes list. Duplicates are folded
    because one pull request can cross-reference an issue repeatedly.
    """
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in _as_list(timeline):
        if str(event.get("event") or "") != "cross-referenced":
            continue
        origin = event.get("source")
        item = origin.get("issue") if isinstance(origin, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("pull_request"), dict):
            continue
        url = _safe_https_url(item.get("html_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        changes.append(
            {
                "provider": "github",
                "url": url,
                "number": _int_or_zero(item.get("number")),
                "title": str(item.get("title") or ""),
                "state": str(item.get("state") or "").lower(),
            }
        )
    return changes


def _gitlab_linked_changes(related: Any) -> list[dict[str, Any]]:
    """Normalize GitLab's ``related_merge_requests`` reply to linked changes."""
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _as_list(related):
        url = _safe_https_url(item.get("web_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        changes.append(
            {
                "provider": "gitlab",
                "url": url,
                "number": _int_or_zero(item.get("iid") or item.get("id")),
                "title": str(item.get("title") or ""),
                # GitLab says "opened"; the contract's state field is free-form
                # text but both providers should read the same in the panel.
                "state": "open" if item.get("state") == "opened" else str(item.get("state") or ""),
            }
        )
    return changes


async def _fetch_github_issue(ref: SourceRef) -> dict[str, Any]:
    issue_api = f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}"
    details = await _run_json("gh", "api", issue_api)
    if not isinstance(details, dict):
        raise SourceProviderError("GitHub returned an invalid issue payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    comments_raw: Any
    timeline_raw: Any
    comments_raw, timeline_raw = await asyncio.gather(
        _run_json(
            "gh",
            "api",
            f"{issue_api}/comments?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        _run_json(
            "gh",
            "api",
            f"{issue_api}/timeline?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(comments_raw, BaseException):
        _mark_partial(partial_sections, "comments")
    if isinstance(timeline_raw, BaseException):
        _mark_partial(partial_sections, "linked changes")
    comment_rows = _as_list(_or_empty(comments_raw))
    comment_count = _int_or_zero(details.get("comments"))
    if len(comment_rows) >= _SECONDARY_PAGE_SIZE or comment_count > len(comment_rows):
        _mark_partial(partial_sections, "comments")
    timeline = _or_empty(timeline_raw)
    if len(_as_list(timeline)) >= _SECONDARY_PAGE_SIZE:
        # A full page may be truncated, so a cross-reference on a later page
        # would be missing from the linked-changes list.
        _mark_partial(partial_sections, "linked changes")

    return {
        "provider": "github",
        # Identity comes from the VALIDATED ref, never the provider echo: the
        # browser submits this url back for refresh, so a hostile or compromised
        # instance echoing a different html_url could otherwise steer a
        # credential-backed read at an unrelated repository or object.
        "url": ref.url,
        "number": ref.number,
        "title": str(details.get("title") or ""),
        "description": str(details.get("body") or ""),
        "state": str(details.get("state") or "").lower(),
        "stateReason": str(details.get("state_reason") or ""),
        "author": _author(details.get("user")),
        "createdAt": str(details.get("created_at") or ""),
        "updatedAt": str(details.get("updated_at") or ""),
        "closedAt": str(details.get("closed_at") or ""),
        "closedBy": _author(details.get("closed_by")),
        "labels": _issue_labels(details.get("labels")),
        "assignees": [
            name for name in (_author(item) for item in _as_list(details.get("assignees"))) if name
        ],
        "milestone": _issue_milestone(details.get("milestone")),
        "commentCount": comment_count,
        "locked": bool(details.get("locked")),
        "reactions": _github_issue_reactions(details.get("reactions")),
        "comments": [_github_issue_comment(item) for item in comment_rows],
        "linkedChanges": _github_linked_changes(timeline),
        "partialSections": partial_sections,
    }


async def _fetch_gitlab_issue(ref: SourceRef) -> dict[str, Any]:
    project = quote(ref.project, safe="")
    issue_api = f"projects/{project}/issues/{ref.number}"
    # with_labels_details upgrades `labels` from bare names to objects carrying
    # the colour the panel renders; without it every label would be colourless.
    details = await _run_json("glab", "api", f"{issue_api}?with_labels_details=true", host=ref.host)
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid issue payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    notes_raw: Any
    related_raw: Any
    notes_raw, related_raw = await asyncio.gather(
        _run_json(
            "glab",
            "api",
            f"{issue_api}/notes?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
            host=ref.host,
        ),
        _run_json(
            "glab",
            "api",
            f"{issue_api}/related_merge_requests",
            host=ref.host,
        ),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(notes_raw, BaseException):
        _mark_partial(partial_sections, "comments")
    if isinstance(related_raw, BaseException):
        _mark_partial(partial_sections, "linked changes")
    note_rows = _as_list(_or_empty(notes_raw))
    if len(note_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "comments")

    comments = []
    for note in note_rows:
        if note.get("system"):
            # Label/milestone/state churn, not discussion.
            continue
        note_id = str(note.get("id") or "")
        comments.append(
            {
                "id": note_id,
                "author": _author(note.get("author")),
                "body": str(note.get("body") or ""),
                "createdAt": str(note.get("created_at") or ""),
                # GitLab notes carry no permalink of their own, so anchor off the
                # VALIDATED ref url rather than any provider-echoed link.
                "url": f"{ref.url}#note_{note_id}" if note_id else "",
            }
        )
    reported_comment_count = _int_or_zero(details.get("user_notes_count"))
    if reported_comment_count > len(comments) and "comments" not in partial_sections:
        _mark_partial(partial_sections, "comments")

    return {
        "provider": "gitlab",
        # See _fetch_github_issue: identity is the validated ref, not the
        # provider's web_url/iid. This matters most for a self-managed instance,
        # whose responses are outside the trust boundary the allowlist sets.
        "url": ref.url,
        "number": ref.number,
        "title": str(details.get("title") or ""),
        "description": str(details.get("description") or ""),
        # GitLab says "opened"; the contract's vocabulary is open/closed.
        "state": "open" if details.get("state") == "opened" else str(details.get("state") or ""),
        # GitLab has no equivalent of GitHub's state_reason.
        "stateReason": "",
        "author": _author(details.get("author")),
        "createdAt": str(details.get("created_at") or ""),
        "updatedAt": str(details.get("updated_at") or ""),
        "closedAt": str(details.get("closed_at") or ""),
        "closedBy": _author(details.get("closed_by")),
        "labels": _issue_labels(details.get("labels")),
        "assignees": [
            name for name in (_author(item) for item in _as_list(details.get("assignees"))) if name
        ],
        "milestone": _issue_milestone(details.get("milestone")),
        "commentCount": reported_comment_count or len(comments),
        "locked": bool(details.get("discussion_locked")),
        "reactions": _gitlab_issue_reactions(details),
        "comments": comments,
        "linkedChanges": _gitlab_linked_changes(_or_empty(related_raw)),
        "partialSections": partial_sections,
    }


# ── Jira issue fetching ────────────────────────────────────────────────────────

_JIRA_FETCH_TIMEOUT = 15  # seconds per HTTP call
_JIRA_MAX_COMMENTS = 50


def _get_jira_auth(host: str) -> tuple[str, str] | None:
    """Return (email, token) for *host* from config + vault/.env, or None.

    Host and email come from config.json (non-sensitive metadata). The token is
    resolved from the encrypted vault first (successor store, populated by
    ``kirocrew secrets import``), falling back to the protected .env file /
    environment for installs that have not migrated — following the same
    credential isolation pattern as Slack/Discord/Telegram tokens, never stored
    in the agent-readable config.json.

    Raises ValueError on config load failures so callers can distinguish
    "config is broken" from "no credentials configured" (None).
    """
    try:
        # Snapshot the process-environment value of JIRA_API_TOKEN BEFORE
        # KiroCrewConfig.load() / load_credentials() runs.  load_credentials()
        # calls os.environ.setdefault(CRED_JIRA_API_TOKEN, ...) which seeds the
        # .env global into os.environ when no real env override is present.
        # Reading os.environ["JIRA_API_TOKEN"] AFTER that call would treat a
        # merely-seeded .env value as a "live env override", causing the
        # single-host global branch to use the .env global instead of the vault
        # for a host that has its OWN per-host token in the vault.
        # Capturing the value here — before any setdefault — means only a real
        # operator-set env var (present before this call) counts as an override.
        _env_global_override = os.environ.get("JIRA_API_TOKEN")
        cfg = KiroCrewConfig.load()
        entries = cfg.dashboard.jira_auth
        # Token is resolved from .env / environment, not config.json
        creds = cfg.load_credentials()
    except Exception as exc:
        raise ValueError(f"jira_config_error: Could not load Jira configuration: {exc}") from exc
    normalized = host.lower().removesuffix(":443")
    for entry in entries:
        entry_host = entry.host.strip().lower().removesuffix(":443")
        if entry_host == normalized:
            # Per-host token: JIRA_TOKEN_<host_key> takes precedence.
            # Global JIRA_API_TOKEN fallback is only permitted when a single
            # host is configured — prevents cross-host credential leakage.
            # Injective host-to-key: hex-encode the normalized host to avoid
            # collisions (e.g. jira-a.x.com vs jira.a-x.com).
            host_key = entry_host.encode().hex().upper()
            per_host_name = f"JIRA_TOKEN_{host_key}"
            # Resolution order: the encrypted vault first (the successor store,
            # populated by `kirocrew secrets import`), then the legacy .env /
            # environment value so existing installs keep working unchanged.
            #
            # EXCEPTION for the global `JIRA_API_TOKEN`: a nonempty PROCESS-
            # ENVIRONMENT value overrides even the vault. `load_credentials`
            # overlays `os.environ` over the .env for this key, so a live env
            # var is the effective credential at runtime — and `kirocrew secrets
            # import` deliberately SKIPS migrating the key while such an override
            # is set, precisely so it does not get pinned into the vault. But a
            # vault entry written by an EARLIER migration (before the override
            # existed) would otherwise be read vault-first and silently shadow
            # that override. Consulting the env override before the global vault
            # entry keeps the migrate-skip and the resolve-order consistent.
            # Per-host `JIRA_TOKEN_<HEX>` keys are NOT env-overlaid, so they are
            # unaffected and keep their vault-first order.
            #
            # We use `_env_global_override` (captured BEFORE load_credentials
            # ran) rather than a fresh os.environ read so that a value merely
            # seeded by load_credentials' setdefault — which is NOT a real
            # operator override — cannot masquerade as one here.
            #
            # HOWEVER: `GatewayOrchestrator.__init__` calls `load_credentials()`
            # at startup, which seeds the `.env` global into `os.environ` via
            # `setdefault` BEFORE any request handler runs. A subsequent call to
            # `_get_jira_auth` would then capture that `.env`-seeded value as
            # `_env_global_override`, indistinguishable from a real operator
            # override. Fix: after ruling out secret refs, compare the captured
            # env value against the current `.env` file value — an equal value
            # came from `.env` (stale, do not override the vault), a different
            # value means the operator set a distinct override at runtime (treat
            # as authoritative). `read_env_file_credential` blocks on I/O but
            # `_get_jira_auth` is called via `asyncio.to_thread` so that is safe.
            token = _resolve_jira_token_from_vault(per_host_name)
            if not token and len(entries) == 1:
                # A `secret://` value is a vault REFERENCE, not a raw token.
                # After `secrets import --apply` the `.env` line becomes
                # `JIRA_API_TOKEN=secret://JIRA_API_TOKEN`, and `load_credentials`
                # propagates that into os.environ (and `creds`) via setdefault.
                # So an env/creds value that is a `secret://` ref must NOT be
                # used as the token — fall through to the vault. Only a real,
                # non-ref env value that DIFFERS from the `.env` file counts as
                # a genuine live override that beats the global vault entry.
                _env_file_val = read_env_file_credential("JIRA_API_TOKEN")
                _is_genuine_override = (
                    _env_global_override
                    and not _is_secret_ref(_env_global_override)
                    and _env_global_override != _env_file_val
                )
                if _is_genuine_override:
                    # _is_genuine_override is truthy only when _env_global_override
                    # is a non-empty str, so `or ""` is dead in practice — it only
                    # narrows str | None -> str for the type checker.
                    token = _env_global_override or ""
                else:
                    token = _resolve_jira_token_from_vault("JIRA_API_TOKEN")
            if not token:
                _c = creds.get(per_host_name, "")
                token = _c if not _is_secret_ref(_c) else ""
            if not token and len(entries) == 1:
                _c = creds.get("JIRA_API_TOKEN", "")
                token = _c if not _is_secret_ref(_c) else ""
            if not token:
                return None
            return (entry.email or "", token)
    return None


def _is_secret_ref(value: str) -> bool:
    """True if *value* is a ``secret://`` vault reference rather than a raw token.

    After ``secrets import --apply`` the ``.env`` line for a migrated key becomes
    ``KEY=secret://KEY``, and ``load_credentials`` propagates that string into
    both ``os.environ`` and the returned creds dict. Such a value is a POINTER
    to the vault, not a usable credential, so the resolver must treat it as
    "look in the vault" and never hand it to Jira as the token.
    """
    return value.startswith("secret://")


def _resolve_jira_token_from_vault(name: str) -> str:
    """Return the vault secret *name*, or ``""`` if absent/unavailable.

    Best-effort: a missing vault, missing entry, or read error all yield the
    empty string so the caller falls back to the legacy .env / environment
    value rather than failing.
    """
    try:
        secret = SecretVault(config_dir()).get(name)
    except Exception:
        return ""
    return secret.reveal() if secret is not None else ""


def _jira_is_cloud(host: str) -> bool:
    """True if host is an Atlassian Cloud instance."""
    return host.lower().endswith(".atlassian.net")


_ADF_MAX_DEPTH = 64

# ADF node types that occupy a line of their own. Everything else is treated as
# an inline run, so an unknown node still contributes its text rather than
# vanishing.
_ADF_BLOCK_TYPES = frozenset(
    {
        "doc",
        "paragraph",
        "heading",
        "codeBlock",
        "blockquote",
        "panel",
        "rule",
        "bulletList",
        "orderedList",
        "taskList",
        "table",
        "expand",
        "nestedExpand",
        "layoutSection",
        "layoutColumn",
        "bodiedExtension",
        "decisionList",
        "mediaSingle",
        "mediaGroup",
        "blockCard",
        "embedCard",
    }
)

# Containers that only GROUP other blocks. Markdown has no columns, so the
# grouping flattens to its children in document order -- but the children must
# still render as BLOCKS. Reaching the inline path instead concatenates them:
# a two-column layout of a paragraph and a heading rendered as `firstsecond`,
# with the separator and the `##` both gone. Measured on each type below.
_ADF_BLOCK_CONTAINER_TYPES = frozenset(
    {
        "layoutSection",
        "layoutColumn",
        "bodiedExtension",
        "decisionList",
        "mediaSingle",
        "mediaGroup",
    }
)

# Block-level cards carry their URL in an attribute and have no content, so the
# inline container fallthrough rendered them as the empty string -- the URL was
# lost outright, which is the same unrecoverable loss this change exists to fix.
_ADF_BLOCK_CARD_TYPES = frozenset({"blockCard", "embedCard"})

# List types, which follow their sibling block without a blank line so the
# nested list stays part of the same (tight) list item.
_ADF_LIST_TYPES = frozenset({"bulletList", "orderedList", "taskList"})

# Inline markdown/HTML syntax openers. The panel renders a source's
# ``description`` and every comment ``body`` through MarkdownRenderer
# (react-markdown + remark-gfm + rehypeRaw), so ADF *text* that merely looks
# like markup would otherwise be re-parsed as markup: a literal ``**`` in a
# Jira description would turn bold, a literal ``<b>`` would be eaten by the
# HTML sanitizer, and a literal ``&copy;`` would be decoded to a copyright sign.
# ``!`` earns its place for a different reason: this converter emits real ``[``
# for a link, a mention card and a media node, so a literal ``!`` landing
# immediately before one would splice into image syntax and the panel would
# auto-fetch a provider-controlled URL -- the beacon the media-as-link form
# exists to avoid. `$` is there because the same renderer runs remark-math, so a
# literal `$$x$$` in a Jira description would render as KaTeX rather than as the
# characters someone typed. Backslash-escaping these keeps ADF text literal,
# leaving the marks and block types below as the only things that become real
# markdown. Every character here is ASCII punctuation, which CommonMark says may
# always be backslash-escaped.
_MD_INLINE_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]<>|~&!$"})

# A text run that OPENS a line can also start a *block* construct the inline set
# above does not cover (``# heading``, ``- item``, ``1. item``, and a line of
# ``=`` or ``-`` that makes the line ABOVE it a setext heading). Only paragraph
# and list-item text is passed through this: a heading's own ``#`` prefix
# already claims its line, and list markers are added by the list renderer after
# its items are rendered.
_MD_BLOCK_LEAD_RE = re.compile(r"^([ \t]*)(?:([-+#=])|(\d{1,9})([.)]))", re.MULTILINE)

# The characters `_URL_RE` will not cross that can nonetheless appear INSIDE a
# URL. Its path/query class is `[^\s)\"'>]*`, so any of these ends the match and
# puts the rest of the URL -- including its whole query -- outside every
# exfiltration check that follows.
#
# Whitespace is deliberately NOT here even though the class excludes it. A space
# genuinely ends a URL: the renderer's own autolinker stops there too, so text
# after it is prose rather than part of a fetchable address. Encoding it treated
# the two as one URL and destroyed benign content -- `see <url>?id=7 <40-char
# sha> for detail` collapsed to `see%20[REDACTED: suspicious URL ...]`, losing
# every word after the URL.
#
# This table is a SHADOW of that scanner's terminator set and exists only while
# the scanner carries the bug. Retire it when #7611 lands rather than keeping
# both: two copies of one set drift, and the copy that matters is the scanner's.
_URL_SCAN_ESCAPES = str.maketrans(
    {
        '"': "%22",
        "'": "%27",
        "(": "%28",
        ")": "%29",
        ">": "%3E",
    }
)

# A URL as any consumer delimits one: it runs to the first whitespace. Terminator
# encoding is applied only INSIDE these spans so surrounding prose keeps its own
# punctuation.
_URL_SPAN_RE = re.compile(r"https?://\S*", re.IGNORECASE)

# The largest ordered-list start CommonMark accepts. Ten digits is not a list
# marker, so a longer number renders as a literal paragraph.
_MD_MAX_LIST_START = 999999999

# One highlighter token, and nothing that could leave the fence's own line. The
# anchors are deliberately absent: `$` also matches just before a TRAILING
# newline, so `re.match` accepted `"python\n"` and the fence emitted a blank
# first line inside the block. `fullmatch` is the check that means what this
# comment says.
_MD_CODE_LANGUAGE_RE = re.compile(r"[A-Za-z0-9+#._-]{1,32}")


def _md_redact_untruncated(text: str, *, single_url: bool = False) -> str:
    """Redact *text* with the URL scan able to see whole URLs.

    `_redact_provider_data` inherits `_URL_RE`, whose path/query class stops at
    whitespace, ``)``, ``"``, ``'`` or ``>``. A URL carrying any of them is
    scanned only as far as that character, so its query -- the part that would
    carry exfiltrated data -- is never inspected, and `_exfil_url_warning`
    returns clean on a truncated match with no ``?`` left in it. Scanning a form
    with those characters percent-encoded is what lets the checks see all of it.

    Every place this converter emits provider text goes through here, because
    the gap is per-CALL-SITE, not per-node-type: when only the link destination
    was covered, an expand title, a mention label, an inline card and a media URL
    each still leaked a high-entropy query, measured one by one.

    The rule is to scan the form that will actually be EMITTED, which is why
    whitespace is handled differently per site rather than uniformly:

    * ``single_url`` -- a link DESTINATION is one address, and the angle-bracket
      form emits it with whitespace percent-encoded, so a space does NOT end it.
      The scan encodes whitespace too, or it would stop where the emitted URL
      does not.
    * otherwise -- in prose a space really does end the URL. Verified against the
      renderer: for `https://host/a?data= <blob>` the emitted anchor's href is
      `https://host/a?data=`, so following text cannot ride along in a fetchable
      address. Encoding whitespace here treated a URL and the next word as one
      address: a URL followed by a commit SHA scanned as a query carrying the SHA,
      the entropy heuristic fired, and the WHOLE paragraph was replaced.

    When nothing is found the ORIGINAL text comes back, so no encoding is ever
    visible in the ordinary case. When something IS found the redacted encoded
    form is returned: the marker replaces the URL wholesale, and a stray percent
    escape beside a redaction marker is a better outcome than emitting a URL that
    was only scanned up to its first parenthesis.
    """
    if single_url:
        scanned = re.sub(r"\s+", "%20", text).translate(_URL_SCAN_ESCAPES)
    else:
        scanned = _URL_SPAN_RE.sub(lambda m: m.group(0).translate(_URL_SCAN_ESCAPES), text)
    redacted = str(_redact_provider_data(scanned))
    return text if redacted == scanned else redacted


def _md_escape_inline(text: str) -> str:
    """Backslash-escape the markdown syntax characters in literal ADF text."""
    return text.translate(_MD_INLINE_ESCAPE)


def _adf_attr_label(value: Any) -> str:
    """Prepare an ADF *attribute* string for emission as an inline label.

    Redact, collapse whitespace, then escape -- in that order, and all three for
    the same reason.

    An attribute is a label, not prose. Where a text node's own newline is
    content, a newline inside an attribute would end the construct the attribute
    sits in and let the remainder become document structure, so whitespace is
    collapsed first. Escaping then keeps the rest literal -- and because escaping
    inserts a backslash, it would hide a credential from the payload-level
    ``_redact_provider_data`` pass that runs afterwards, so redaction has to
    happen here, before the backslash lands.

    Doing it here rather than in a pre-pass over the whole tree is what keeps the
    work bounded: this runs inside the converter's own depth-capped traversal,
    while ``_redact_provider_data`` recurses without a cap and raises
    ``RecursionError`` on a document a few hundred levels deep.
    """
    collapsed = re.sub(r"\s+", " ", _md_redact_untruncated(str(value or ""))).strip()
    return _md_escape_inline(collapsed)


def _md_code_language(value: Any) -> str:
    """The fence info string for an ADF code block's ``language`` attribute.

    A fence's info string runs to the end of its line, so a newline-bearing (or
    merely space-bearing) attribute would close the fence early and turn
    provider-controlled text into real document structure. Only a single
    highlighter token is admitted: letters, digits, and the punctuation real
    language names carry (``c++``, ``c#``, ``objective-c``, ``asp.net``,
    ``shell_session``). Anything else drops to a bare fence, which costs syntax
    highlighting and nothing else.
    """
    language = str(value or "")
    return language if _MD_CODE_LANGUAGE_RE.fullmatch(language) else ""


def _md_escape_block_leads(text: str) -> str:
    """Escape a line-leading ``-``/``+``/``#``/``1.`` so it stays literal text.

    The backslash goes before the punctuation, never before the digit: ``\\1`` is
    not a valid CommonMark escape and would render as a visible backslash.
    """

    def _sub(match: re.Match[str]) -> str:
        if match.group(2):
            return f"{match.group(1)}\\{match.group(2)}"
        return f"{match.group(1)}{match.group(3)}\\{match.group(4)}"

    return _MD_BLOCK_LEAD_RE.sub(_sub, text)


def _md_backtick_fence(text: str, minimum: int) -> str:
    """A backtick fence long enough to survive the backticks inside *text*."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(minimum, longest + 1)


def _md_inline_code(text: str) -> str:
    """Wrap *text* in an inline code span, unescaped (code spans are literal).

    CommonMark cannot open a span whose content starts or ends with a backtick,
    and it strips one leading and one trailing character from a span whose
    content both begins and ends with a space or newline (unless the content is
    nothing but whitespace, which is left alone). One space of padding -- which
    that same rule then removes -- is what keeps such content intact.

    A line break inside the content is collapsed to a space FIRST. A code span is
    inline, so it cannot span a paragraph: a newline in the content ends the
    enclosing paragraph and everything after it is parsed as fresh markdown --
    outside the fence, and so past :func:`_md_escape_inline`,
    :func:`_md_link_target` and the redaction gate. Collapsed rather than emitted
    as a fenced block, because a block would change the document structure at
    every call site while the escape is what actually has to hold.
    """
    text = re.sub(r"\r\n?|\n", " ", text)
    fence = _md_backtick_fence(text, 1)
    first, last = text[:1], text[-1:]
    edge_stripped = first in (" ", "\n") and last in (" ", "\n") and text.strip() != ""
    pad = " " if text.startswith("`") or text.endswith("`") or edge_stripped else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _md_link_target(url: str) -> str | None:
    """Render *url* as a markdown link destination, or None to omit the link.

    The bare form covers the common case; the angle-bracket form takes over when
    the URL carries whitespace or parentheses, which would otherwise end the
    destination early and spill the rest of the URL into the document.

    None means this URL must not become a destination at all. The old plain-text
    walker dropped every href, so a provider-controlled destination reaching the
    payload is new here, and the payload-level scan cannot be relied on to clean
    one up afterwards: ``_URL_RE``'s path group excludes ``)``, so a URL with a
    ``)`` in its path is matched only up to that point. Everything past it --
    including the whole query -- is then outside every check that follows, and
    ``_exfil_url_warning`` returns clean on a truncated match with no ``?`` left
    in it. Measured: a high-entropy query blob is redacted without the paren and
    NOT redacted with it, and no credential-pattern pass covers a bare blob.

    So the scan here is run against a form with every character that terminates
    that match percent-encoded -- not just the parenthesis, which was the one
    member of the class this gate originally covered. The encoded form is only
    ever used to DECIDE: a URL that passes is emitted exactly as it arrived.
    Encoding a handful of characters cannot trip the heavy-encoding rule, which
    needs 20 consecutive octets, and a URL pathological enough to reach that is
    dropped rather than leaked. A link that fails is dropped rather than emitted
    partly redacted; the label still carries the text, so nothing silently
    vanishes.

    This shields the hrefs THIS converter emits. The truncation itself is in the
    shared scanner and every other caller still has it, so it is filed as #7611
    rather than left recorded only here; fixing a shared security regex is a
    different blast radius than a Jira rendering change.
    """
    if not url:
        return None
    if _md_redact_untruncated(url, single_url=True) != url:
        return None
    if not re.search(r"[\s()<>]", url):
        return url
    inner = re.sub(r"\s+", "%20", url).replace("<", "%3C").replace(">", "%3E")
    return f"<{inner}>"


def _md_one_line(text: str) -> str:
    """Fold *text* onto a single line, collapsing ONLY line breaks.

    A heading and a GFM table cell each occupy exactly one line, but the repeated
    spaces and tabs inside one are content, not layout: a code span's whitespace
    is literal by definition, and the padding that protects its boundary spaces
    would be eaten by a blanket whitespace collapse. Only a newline has to go,
    and a code span's own padding sits inside its backticks where no newline can
    reach it.

    Split on the line breaks rather than matched with a regex. The pattern this
    replaces made the leading whitespace run and the newline anchor compete for
    the same characters, so on a long newline-FREE whitespace run -- content a
    provider controls, bounded only by the 8MiB fetch cap -- the engine retried
    every split of that run at every starting offset before failing. A split,
    strip and join reads each character a fixed number of times. The collapsed
    set is unchanged: a maximal whitespace run containing at least one line break
    becomes one space, and the whitespace class strip uses is the one the pattern
    matched.
    """
    return " ".join(seg for seg in (line.strip() for line in text.split("\n")) if seg)


def _md_guard_line_expansion(text: str, per_line: int) -> None:
    """Refuse a per-line expansion that would blow the payload ceiling.

    Marking or indenting adds *per_line* characters to EVERY line, and a provider
    controls both numbers. Newlines embedded in a single text node cost about
    three bytes of payload each, while sixty levels of nesting adds a hundred and
    twenty characters to every one of them, so a document well inside the 8MiB
    fetch cap can project past three hundred MiB of output. The payload gate runs
    only AFTER conversion, so without this check the allocation happens first:
    measured before it, a 2.3MiB payload rendered 93MiB of markdown with a 224MiB
    peak, and the 8MiB cap extrapolates to roughly 780MiB.

    Raising here matches how an oversized response is already refused, and it
    lives inside the two expanders rather than at their call sites so no new
    caller can forget it.
    """
    if len(text) + per_line * (text.count("\n") + 1) > _MAX_PAYLOAD_BYTES:
        raise SourceProviderError("Jira issue content is too large to render.")


def _md_prefix_lines(text: str, prefix: str) -> str:
    """Prefix every line of *text*, keeping blank lines inside the same block."""
    _md_guard_line_expansion(text, len(prefix))
    stripped = prefix.rstrip()
    return "\n".join(prefix + line if line else stripped for line in text.split("\n"))


def _md_hang_indent(body: str, marker: str) -> str:
    """Put *marker* on the first line and align continuation lines under it."""
    if not body:
        return ""
    _md_guard_line_expansion(body, len(marker))
    pad = " " * len(marker)
    head, *rest = body.split("\n")
    lines = [marker + head]
    lines.extend(pad + line if line else "" for line in rest)
    return "\n".join(lines)


def _adf_to_markdown(node: Any, *, _depth: int = 0) -> str:
    """Convert an Atlassian Document Format tree to markdown.

    ADF is the JSON document model Jira Cloud v3 returns for rich-text fields
    (descriptions and comment bodies). The panel renders those fields through
    MarkdownRenderer, and every other provider puts real markdown in the same
    payload field (a GitHub issue ``body``, a GitLab ``description``), so
    emitting markdown here restores headings, lists, link URLs, code fences and
    tables that a plain-text walk would drop.

    Traversal is depth-limited (max 64 levels) to prevent stack exhaustion from a
    malformed or maliciously deep document, and literal text is escaped so a
    description cannot smuggle markup into the panel.

    Best-effort by design: ADF tables may carry merged cells and nested blocks
    that GFM cannot express (rendered as a flat approximation), and a ``media``
    node without a public ``url`` attribute has no fetchable address, so it
    contributes nothing.
    """
    if _depth > _ADF_MAX_DEPTH or not isinstance(node, dict):
        return ""
    if str(node.get("type") or "") in _ADF_BLOCK_TYPES:
        return _adf_block_to_markdown(node, _depth=_depth)
    return _adf_inline_to_markdown(node, _depth=_depth)


def _adf_block_to_markdown(node: dict[str, Any], *, _depth: int) -> str:
    """Render one ADF block node. Only called for a type in _ADF_BLOCK_TYPES."""
    node_type = str(node.get("type") or "")
    attrs = _as_dict(node.get("attrs"))
    if node_type == "doc":
        return _adf_join_blocks(node, _depth=_depth)
    if node_type in _ADF_BLOCK_CONTAINER_TYPES:
        return _adf_join_blocks(node, _depth=_depth)
    if node_type in _ADF_BLOCK_CARD_TYPES:
        return _adf_url_link(str(attrs.get("url") or ""))
    if node_type == "paragraph":
        return _md_escape_block_leads(_adf_inline_run(node, _depth=_depth))
    if node_type == "heading":
        level = min(max(_int_or_zero(attrs.get("level")) or 1, 1), 6)
        # A heading occupies one line, and only its first line carries the `#`
        # prefix. A hardBreak inside it would push the rest onto a second line
        # where a leading `-` or `#` is neither inline- nor block-lead-escaped and
        # would render as a spurious list or heading.
        text = _md_one_line(_adf_inline_run(node, _depth=_depth))
        return f"{'#' * level} {text}" if text else ""
    if node_type == "codeBlock":
        body = _adf_plain_text(node, _depth=_depth)
        fence = _md_backtick_fence(body, 3)
        language = _md_code_language(attrs.get("language"))
        # The newline before the closing fence SEPARATES the body from it, so a
        # body that already ends with one does not need another: adding it
        # unconditionally turned a source ending in `\n` into content ending in
        # `\n\n`, and an empty body into a block holding one blank line.
        if not body:
            return f"{fence}{language}\n{fence}"
        separator = "" if body.endswith("\n") else "\n"
        return f"{fence}{language}\n{body}{separator}{fence}"
    if node_type in ("blockquote", "panel"):
        # An ADF panel (info/note/warning) has no markdown equivalent; a
        # blockquote keeps it visually set apart from the surrounding prose.
        #
        # A chain of single-child quotes is collapsed and prefixed ONCE. Marking
        # at every level re-copies text the level below already marked, which is
        # quadratic in the nesting depth for the number of lines it carries: on a
        # 6.7MB document nested 60 deep that measured 2.57s of blocking work
        # against 0.47s for the same content unnested. Each collapsed level still
        # consumes depth, so the traversal cap applies exactly as before.
        marks = 1
        inner_node = node
        while True:
            only = _as_list(inner_node.get("content"))
            if len(only) == 1 and str(only[0].get("type") or "") in ("blockquote", "panel"):
                inner_node = only[0]
                marks += 1
                _depth += 1
            else:
                break
        inner = _adf_join_blocks(inner_node, _depth=_depth)
        return _md_prefix_lines(inner, "> " * marks) if inner else ""
    if node_type == "rule":
        return "---"
    if node_type in ("bulletList", "orderedList"):
        return _adf_list_to_markdown(node, _depth=_depth, ordered=node_type == "orderedList")
    if node_type == "taskList":
        return _adf_task_list_to_markdown(node, _depth=_depth)
    if node_type == "table":
        return _adf_table_to_markdown(node, _depth=_depth)
    # expand / nestedExpand: a collapsed section, whose title is the only part
    # markdown cannot express as a container.
    title = _adf_attr_label(attrs.get("title"))
    inner = _adf_join_blocks(node, _depth=_depth)
    return "\n\n".join(part for part in (f"**{title}**" if title else "", inner) if part)


def _adf_inline_to_markdown(node: Any, *, _depth: int, _scanned: bool = False) -> str:
    """Render one ADF inline node, recursing into an unrecognised container."""
    if _depth > _ADF_MAX_DEPTH or not isinstance(node, dict):
        return ""
    node_type = str(node.get("type") or "")
    attrs = _as_dict(node.get("attrs"))
    if node_type == "text":
        return _adf_apply_marks(str(node.get("text") or ""), _as_list(node.get("marks")))
    if node_type == "hardBreak":
        # Two trailing spaces: the panel renders with CommonMark soft-break
        # collapse, so a bare newline would become a space.
        return "  \n"
    if node_type == "mention":
        name = _adf_attr_label(attrs.get("text") or attrs.get("id"))
        if not name:
            return ""
        return name if name.startswith("@") else f"@{name}"
    if node_type == "emoji":
        return _adf_attr_label(attrs.get("text") or attrs.get("shortName"))
    if node_type == "inlineCard":
        return _adf_url_link(str(attrs.get("url") or ""))
    if node_type == "media":
        # A link, not an image: the URL stays recoverable (the loss this fix is
        # about) without the panel auto-fetching a provider-controlled address
        # the moment someone opens the issue.
        return _adf_url_link(str(attrs.get("url") or ""), _adf_attr_label(attrs.get("alt")))
    # An unrecognised inline container contributes only its children, so it gets
    # the same span treatment they would get at the top level -- otherwise two
    # equally marked text nodes one level down still emit `**a****b**`. The
    # ancestor's scan already covered this subtree, so it is not repeated.
    return _adf_inline_sequence(_as_list(node.get("content")), _depth=_depth + 1, _scanned=_scanned)


_ADF_EMPHASIS_MARKS = frozenset({"strong", "em"})


def _adf_emphasis_item(node: dict[str, Any], *, _depth: int) -> tuple[str, frozenset[str]]:
    """Split one inline node into (text, marks that can be factored).

    Only ``strong`` and ``em`` are factorable, because they are the only two
    marks that share a delimiter CHARACTER and so the only two whose delimiter
    runs can merge with a neighbour's. A node carrying anything else -- a code
    mark, strike, a link -- is rendered whole and treated as opaque: its own
    backtick, tilde or bracket sits at the boundary and keeps the asterisks
    apart, which was verified per pair rather than assumed.
    """
    text = str(node.get("text") or "")
    kinds = {str(mark.get("type") or "") for mark in _as_list(node.get("marks"))}
    if str(node.get("type") or "") != "text" or not text or not kinds <= _ADF_EMPHASIS_MARKS:
        return _adf_inline_to_markdown(node, _depth=_depth, _scanned=True), frozenset()
    return _md_escape_inline(text), frozenset(kinds)


def _md_wrap_emphasis(mark: str, inner: str, before: str, after: str) -> str:
    """Wrap *inner* for one emphasis mark with a delimiter that survives.

    ``before`` and ``after`` are the single characters on either side, not the
    surrounding text: only the adjacent character decides either rule, and
    passing the accumulated output instead made the emitter quadratic.

    ``strong`` has only ``**``, and an asterisk on the INSIDE of it is fine: a
    closing ``***`` resolves correctly. ``em`` needs both of its spellings,
    because neither works everywhere -- ``*`` is re-lexed when it touches
    another asterisk run, and ``_`` will not open or close INTRAWORD. When
    neither is safe at both ends the mark is dropped and the text kept: that
    loses an italic, where emitting the delimiter anyway would show it as
    content and lose the italic as well.
    """
    if not inner:
        return ""
    if mark == "strong":
        return f"**{inner}**"
    if before != "*" and after != "*":
        return f"*{inner}*"
    if not before.isalnum() and not after.isalnum():
        return f"_{inner}_"
    return inner


def _md_emit_emphasis(items: list[tuple[str, frozenset[str]]], *, before: str = "") -> str:
    """Emit a run of (text, marks) items, factoring a shared mark out ONCE.

    Wrapping each node independently is what corrupts overlapping marks. A
    strong node, then strong+em, then em emitted ``**a*****b****c*``, which a
    CommonMark parser reads as strong(a), a LITERAL ``***b***``, then em(c): the
    delimiters become visible text and the middle node loses both its marks.
    Emitting a mark shared by neighbours ONCE, around all of them, is what keeps
    the runs unambiguous -- ``**a*b***_c_`` parses as intended.

    The scan is greedy from the left, taking the mark that spans the longest run
    at each position. A different factorisation can occasionally keep a mark this
    one drops; greedy is chosen because its fallback is lossy, never wrong.

    ``before`` is the one character preceding this run. Recursion is bounded by
    the number of factorable marks -- each level removes one, so it is at most
    two deep and needs no depth guard of its own.
    """
    out: list[str] = []
    last = before
    index = 0
    while index < len(items):
        text, marks = items[index]
        if not marks:
            out.append(text)
            last = text[-1:] or last
            index += 1
            continue
        best_mark, best_end = "", index
        for mark in ("strong", "em"):
            if mark not in marks:
                continue
            end = index
            while end < len(items) and mark in items[end][1]:
                end += 1
            if end > best_end:
                best_mark, best_end = mark, end
        inner = _md_emit_emphasis(
            [(t, ms - {best_mark}) for t, ms in items[index:best_end]], before=last
        )
        if best_end < len(items):
            next_text, next_marks = items[best_end]
            # A marked neighbour opens with a delimiter, which is punctuation for
            # the underscore rule and an asterisk for the run-merging one.
            following = "*" if next_marks else next_text[:1]
        else:
            following = ""
        piece = _md_wrap_emphasis(best_mark, inner, last, following)
        out.append(piece)
        last = piece[-1:] or last
        index = best_end
    return "".join(out)


def _adf_apply_marks(text: str, marks: list[dict[str, Any]]) -> str:
    """Wrap literal *text* in the markdown for each ADF mark, innermost first.

    A ``code`` mark is exclusive: a code span is literal by definition, so the
    emphasis marks are not applied inside one and the text is not escaped.
    Empty text takes no mark wrapping at all, since a bare ``****`` or ``` `` ```
    would render as those literal characters rather than as nothing.
    """
    kinds = {str(mark.get("type") or "") for mark in marks}
    if not text:
        out = ""
    elif "code" in kinds:
        out = _md_inline_code(text)
    else:
        out = _md_escape_inline(text)
        if "strong" in kinds:
            out = f"**{out}**"
        if "em" in kinds:
            # Asterisk, not underscore: CommonMark refuses to open or close an
            # underscore emphasis INTRAWORD, so an italic node between two plain
            # ones would render as `a_b_c` with the underscores visible and the
            # italic lost. Asterisk has no such restriction, and `***x***` still
            # nests correctly when a strong mark wraps the same text.
            out = f"*{out}*"
        if "strike" in kinds:
            out = f"~~{out}~~"
    for mark in marks:
        if str(mark.get("type") or "") != "link":
            continue
        href = str(_as_dict(mark.get("attrs")).get("href") or "")
        if href:
            target = _md_link_target(href)
            if target:
                out = f"[{out or _md_escape_inline(href)}]({target})"
            else:
                # No destination, but keep the text: the label is what the reader
                # was shown, and the href is the part that failed the scan.
                out = out or _adf_attr_label(href)
        break
    return out


def _adf_inline_run(node: dict[str, Any], *, _depth: int) -> str:
    """Concatenate a block's inline children into one line of markdown."""
    return _adf_inline_sequence(_as_list(node.get("content")), _depth=_depth + 1).strip()


def _adf_inline_sequence(
    children: list[dict[str, Any]], *, _depth: int, _scanned: bool = False
) -> str:
    """Render a run of inline nodes.

    Redaction is checked ONCE, over the whole run, against the plain-text
    rendition a seamless walk would produce -- every node's own text in order,
    with no markup between any of it. That string is exactly what the
    payload-level ``_redact_provider_data`` pass used to see, so checking it is
    what preserves a catch this converter would otherwise break: escaping puts a
    backslash inside ``ghp_``, and marks put delimiters between the halves of a
    secret split across siblings, so a credential contiguous in the old output is
    not contiguous in this one.

    Checking the WHOLE run rather than some span of it is deliberate. Any
    narrower boundary has to answer "which nodes contribute text seamlessly", and
    that question kept having a wider answer than the last one -- a bold sibling,
    then an unrecognised container, then a mention or emoji label, each of which
    contributes text with no delimiter of its own. The run has no such boundary
    to get wrong.

    When the check fires the run is emitted as that redacted string: it loses its
    formatting, but no node's text is lost with it.

    ``_scanned`` says an ancestor already scanned this subtree and found it clean.
    That scan covered every descendant's text, since ``_adf_plain_text`` recurses,
    so re-scanning inside a nested container is provably redundant -- and it is
    not free: rescanning at each level is depth-times-text work, which measured
    7.0s for 1MiB under 60 unrecognised containers and 27.8s for 4MiB.
    """
    if not _scanned:
        plain = "".join(_adf_plain_text(child, _depth=_depth) for child in children)
        redacted = _md_redact_untruncated(plain)
        if redacted != plain:
            return _md_escape_inline(redacted)
    return _md_emit_emphasis(
        [_adf_emphasis_item(node, _depth=_depth) for node in _adf_merge_marked_text(children)]
    )


def _adf_mark_key(node: dict[str, Any]) -> list[tuple[str, str]]:
    """An order-insensitive signature for a text node's marks."""
    return sorted(
        (str(mark.get("type") or ""), json.dumps(_as_dict(mark.get("attrs")), sort_keys=True))
        for mark in _as_list(node.get("marks"))
    )


def _adf_merge_marked_text(span: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge neighbouring text nodes whose marks are identical.

    A hardBreak between two text nodes stops the merge, since the break has to
    survive between them.

    Each run's text is collected in a list and joined ONCE at the end. Rebuilding
    the accumulator as ``previous + current`` per node is superlinear -- measured
    at 0.26s for 100k adjacent nodes, 0.75s for 200k and 1.48s for 300k, and 300k
    single-character text nodes fit inside the 8MiB fetch cap -- so a provider
    could buy seconds of synchronous work on the event loop. Only the traversal
    DEPTH is capped; nothing bounds a node's breadth.
    """
    merged: list[dict[str, Any]] = []
    runs: list[list[str]] = []
    previous_key: list[tuple[str, str]] | None = None
    for node in span:
        is_text = str(node.get("type") or "") == "text"
        key = _adf_mark_key(node) if is_text else None
        if merged and is_text and previous_key is not None and key == previous_key:
            runs[-1].append(str(node.get("text") or ""))
            continue
        merged.append(node)
        runs.append([str(node.get("text") or "")] if is_text else [])
        previous_key = key
    return [{**node, "text": "".join(run)} if run else node for node, run in zip(merged, runs)]


def _adf_plain_text(node: Any, *, _depth: int) -> str:
    """The plain text a node contributes, with no markup of any kind.

    This is the rendition the old plain-text walk produced, and it serves two
    callers for the same reason -- both want the characters, not the markup: a
    code block's literal body, and the redaction gate in
    ``_adf_inline_sequence``, which has to see what the payload-level redactor
    used to see.

    A label-bearing node contributes its label WITHOUT the markup this converter
    would wrap it in -- a mention's bare name, not ``@name``; a card's URL, not
    ``[url](url)``. That is deliberate: the gate must never see less contiguity
    than the rendered output has, and dropping the prefix can only make it see
    more, which errs toward redacting.
    """
    if _depth > _ADF_MAX_DEPTH or not isinstance(node, dict):
        return ""
    node_type = str(node.get("type") or "")
    attrs = _as_dict(node.get("attrs"))
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        return str(attrs.get("text") or attrs.get("id") or "")
    if node_type == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if node_type == "media":
        # The ALT before the URL, because the alt is what the reader is shown and
        # what can JOIN a neighbour's text. When the URL fails the destination
        # scan the link is dropped and the alt is emitted with no brackets around
        # it, so a credential split across a text node and an alt becomes one
        # contiguous token -- measured, with the backslash from escaping `ghp_`
        # then defeating the payload pass. Returning the URL here made the gate
        # blind to exactly that. The URL is the fallback for a media node with no
        # alt, which is still emitted as the label.
        return str(attrs.get("alt") or attrs.get("url") or "")
    if node_type == "inlineCard":
        # A card has no alt: its label IS the redacted URL, so the URL is what
        # the gate must see.
        return str(attrs.get("url") or "")
    return "".join(
        _adf_plain_text(child, _depth=_depth + 1) for child in _as_list(node.get("content"))
    )


def _adf_url_link(url: str, label: str = "") -> str:
    """Render a provider URL as a link, or as its label alone if the URL fails.

    The single place a provider URL becomes a markdown destination, so the scan
    in `_md_link_target` cannot be skipped by adding another node type that
    carries a URL. `_adf_attr_label` is the matching chokepoint for attribute
    text; between them, no provider attribute reaches the document unscanned.
    """
    if not url:
        return ""
    text = label or _adf_attr_label(url)
    target = _md_link_target(url)
    return f"[{text}]({target})" if target else text


def _adf_join_blocks(node: dict[str, Any], *, _depth: int) -> str:
    """Render a container's children as markdown blocks, blank-line separated."""
    rendered = (
        _adf_to_markdown(child, _depth=_depth + 1) for child in _as_list(node.get("content"))
    )
    return "\n\n".join(block for block in rendered if block)


def _adf_item_body(item: dict[str, Any], *, _depth: int) -> str:
    """Render one list item, which may mix inline text with nested blocks."""
    blocks: list[tuple[str, bool]] = []
    run: list[dict[str, Any]] = []

    def flush() -> None:
        # Through _adf_inline_sequence, so an item's own inline children get the
        # same split-credential guarantee a paragraph's do.
        text = _adf_inline_sequence(list(run), _depth=_depth + 1).strip()
        run.clear()
        if text:
            blocks.append((_md_escape_block_leads(text), False))

    for child in _as_list(item.get("content")):
        child_type = str(child.get("type") or "")
        if child_type in _ADF_BLOCK_TYPES:
            flush()
            # Through _adf_to_markdown, never straight to the block renderer: a
            # nested list would otherwise re-enter its own renderer past the
            # depth cap and exhaust the stack on a deeply nested document.
            rendered = _adf_to_markdown(child, _depth=_depth + 1)
            if rendered:
                blocks.append((rendered, child_type in _ADF_LIST_TYPES))
        else:
            run.append(child)
    flush()

    if not blocks:
        return ""
    parts = [blocks[0][0]]
    for text, is_list in blocks[1:]:
        parts.append("\n" if is_list else "\n\n")
        parts.append(text)
    return "".join(parts)


def _adf_list_to_markdown(node: dict[str, Any], *, _depth: int, ordered: bool) -> str:
    """Render a bullet or ordered list, honouring an explicit start number."""
    items = _as_list(node.get("content"))
    raw_order = _as_dict(node.get("attrs")).get("order")
    # `or 1` collapsed an explicit `order: 0`, which ADF allows and CommonMark
    # honours as `<ol start="0">`. Only an absent, non-integer or negative value
    # falls back to 1. `bool` is excluded because it is an `int` in Python and
    # `order: true` is not a start number.
    start = (
        raw_order
        if isinstance(raw_order, int) and not isinstance(raw_order, bool) and raw_order >= 0
        else 1
    )
    # More than nine digits is not a list start at all: CommonMark renders
    # `1000000000. a` as a PARAGRAPH, so an out-of-range order would turn the
    # whole list into literal text carrying visible numbers. The LAST item's
    # marker is the one that has to fit, since a start of 999999999 overflows on
    # its second item.
    if start + max(len(items) - 1, 0) > _MD_MAX_LIST_START:
        start = 1
    lines: list[str] = []
    for index, item in enumerate(items):
        marker = f"{start + index}. " if ordered else "- "
        rendered = _md_hang_indent(_adf_item_body(item, _depth=_depth + 1), marker)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def _adf_task_list_to_markdown(node: dict[str, Any], *, _depth: int) -> str:
    """Render an ADF task list as a GFM checklist."""
    lines: list[str] = []
    for item in _as_list(node.get("content")):
        state = str(_as_dict(item.get("attrs")).get("state") or "").upper()
        marker = "- [x] " if state == "DONE" else "- [ ] "
        rendered = _md_hang_indent(_adf_item_body(item, _depth=_depth + 1), marker)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def _adf_table_to_markdown(node: dict[str, Any], *, _depth: int) -> str:
    """Render an ADF table as a GFM table, using its first row as the header.

    GFM requires a header row and cannot express merged cells or block content
    inside a cell, so cell text is flattened to a single line and any
    colspan/rowspan is ignored.

    Each BODY row emits its own cells and nothing more, because GFM inserts empty
    cells for a row shorter than the header. The HEADER and separator are widened
    to the widest row, because the other direction is not symmetric: GFM fixes the
    table's width at the header and a row with MORE cells than the header has the
    excess dropped -- the text is gone, not wrapped. Widening two lines is linear
    in the width; padding every row is what made this quadratic before, since a
    table with one wide row and many narrow ones emitted rows x width cells for
    the handful it actually carried, and a provider controls both numbers.
    """
    rows: list[list[str]] = []
    for row in _as_list(node.get("content")):
        if str(row.get("type") or "") != "tableRow":
            continue
        cells = [
            _adf_cell_text(cell, _depth=_depth + 1)
            for cell in _as_list(row.get("content"))
            if str(cell.get("type") or "") in ("tableHeader", "tableCell")
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    header, *body = rows
    width = max(len(row) for row in rows)
    lines = [
        "| " + " | ".join(header + [""] * (width - len(header))) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _adf_cell_text(cell: dict[str, Any], *, _depth: int) -> str:
    """Flatten one table cell to a single line (a GFM cell cannot wrap).

    Only line breaks are folded: repeated spaces and tabs survive, because a code
    span's whitespace is literal and a blanket collapse would silently rewrite
    ``a  b`` as ``a b``.

    Any pipe still unescaped after rendering is escaped here. A text pipe is
    already escaped by ``_md_escape_inline``, but a code span is emitted
    literally by definition, so ``a|b`` inside one would split the cell in two.
    GFM honours ``\\|`` inside a code span for exactly this case. The one thing
    this cannot express is a literal backslash-pipe pair inside a code span in a
    table, which GFM has no spelling for.
    """
    text = _md_one_line(_adf_join_blocks(cell, _depth=_depth))
    return re.sub(r"(?<!\\)\|", r"\\|", text)


def _jira_linked_changes(fields: dict[str, Any], base_url: str) -> list[dict[str, Any]]:
    """Parse Jira issuelinks into the linkedChanges format.

    Jira issue links have an inward and outward side.  Each link object
    contains either an ``inwardIssue`` or ``outwardIssue`` (never both).
    We normalise both directions into a flat list with the relationship
    type visible to the user.
    """
    raw_links = _as_list(fields.get("issuelinks"))
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in raw_links:
        if not isinstance(link, dict):
            continue
        link_type = _as_dict(link.get("type"))
        # Determine direction and extract the linked issue object
        if isinstance(link.get("outwardIssue"), dict):
            linked_issue = link["outwardIssue"]
            relation = str(link_type.get("outward") or "")
        elif isinstance(link.get("inwardIssue"), dict):
            linked_issue = link["inwardIssue"]
            relation = str(link_type.get("inward") or "")
        else:
            continue
        issue_key = str(linked_issue.get("key") or "")
        if not issue_key or issue_key in seen:
            continue
        seen.add(issue_key)
        # Derive browse URL from base_url
        url = f"{base_url}/browse/{issue_key}"
        # Extract state from statusCategory
        status_obj = _as_dict(
            linked_issue.get("fields", {}).get("status")
            if isinstance(linked_issue.get("fields"), dict)
            else {}
        )
        status_cat = _as_dict(status_obj.get("statusCategory"))
        cat_key = str(status_cat.get("key") or "").lower()
        state = "closed" if cat_key == "done" else "open"
        # Extract issue number (numeric portion after the dash)
        parts = issue_key.rsplit("-", 1)
        number = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
        # Summary for title
        linked_fields = (
            linked_issue.get("fields") if isinstance(linked_issue.get("fields"), dict) else {}
        )
        title = str(linked_fields.get("summary") or "") if isinstance(linked_fields, dict) else ""
        changes.append(
            {
                "provider": "jira",
                "url": url,
                "number": number,
                "title": title or issue_key,
                "state": state,
                "relation": relation,
                "issueKey": issue_key,
            }
        )
    return changes


def _jira_fix_versions(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the fix versions that can populate the milestone slot.

    A version is usable only when it is an object carrying a non-empty
    ``name``: the Issue panel gates the milestone chip on the object being
    truthy, so a nameless version would render an icon with no text.  A
    malformed leading entry therefore does not hide a usable one behind it.
    """
    usable: list[dict[str, Any]] = []
    for version in _as_list(fields.get("fixVersions")):
        if not isinstance(version, dict):
            continue
        if str(version.get("name") or "").strip():
            usable.append(version)
    return usable


def _jira_version_is_done(version: dict[str, Any]) -> bool:
    """A released or archived version no longer takes new work."""
    return bool(version.get("released")) or bool(version.get("archived"))


def _jira_pick_fix_version(fix_versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the fix version that fills the one-slot milestone contract.

    The Issue panel's milestone chip renders only the version's name, with no
    released/archived signal, so surfacing a shipped release reads as if it
    were the pending one (issue #7595).  Prefer the first version that is
    neither released nor archived; when every version has shipped, fall back
    to the first usable entry so the ticket still shows a release rather than
    dropping to no milestone at all.
    """
    pending = [v for v in fix_versions if not _jira_version_is_done(v)]
    return (pending or fix_versions)[0] if fix_versions else None


def _jira_fix_version_milestone(version: dict[str, Any]) -> dict[str, str]:
    """Map one Jira fix version onto the ``IssueMilestone`` contract.

    Jira has no milestones; a fix version is the release a ticket is
    scheduled for, which is the same thing the panel's milestone chip
    communicates.  ``name`` becomes the title and ``releaseDate`` (ISO, unlike
    the locale-formatted ``userReleaseDate``) becomes ``dueOn``.

    ``state`` has only GitHub's two values to choose from.  A released version
    is done, and an archived one no longer takes work, so both map to
    ``closed`` and everything else stays ``open``.
    """
    released = _jira_version_is_done(version)
    return {
        "title": str(version.get("name") or "").strip(),
        "state": "closed" if released else "open",
        "dueOn": str(version.get("releaseDate") or ""),
    }


async def _fetch_jira_issue(ref: SourceRef) -> dict[str, Any]:
    """Fetch a Jira issue via the REST API using configured credentials.

    Raises ValueError with a machine-readable prefix when no credentials are
    configured (frontend uses this to show the link-out fallback).
    """
    # Offload config I/O to a thread — KiroCrewConfig.load() is synchronous
    # (stats, reads, json.loads, jsonschema.validate) and must never run on
    # the event loop. Same discipline as _load_source_link_settings in this file.
    auth_pair = await asyncio.to_thread(_get_jira_auth, ref.host)
    if auth_pair is None:
        host_key = ref.host.lower().removesuffix(":443").encode().hex().upper()
        raise ValueError(
            "jira_no_credentials: No Jira credentials configured for "
            f"{ref.host}. Add a jira_auth entry to config.json and set "
            f"JIRA_API_TOKEN (or JIRA_TOKEN_{host_key} for multi-host) "
            "in your .env file."
        )
    email, token = auth_pair
    is_cloud = _jira_is_cloud(ref.host)
    # Cloud uses API v3 (ADF description); Server/DC uses v2 (wiki/text).
    api_version = "3" if is_cloud else "2"
    issue_key = f"{ref.owner}-{ref.number}" if ref.owner else f"{ref.repo}-{ref.number}"
    # Preserve the context path prefix from the validated URL (e.g. /jira in
    # https://corp.example/jira/browse/PROJ-123) so Server/DC instances
    # behind a reverse proxy reach the correct REST endpoint.
    parsed = urlparse(ref.url)
    browse_idx = parsed.path.find("/browse/")
    context_path = parsed.path[:browse_idx] if browse_idx > 0 else ""
    base_url = f"https://{ref.host}{context_path}"
    issue_url = (
        f"{base_url}/rest/api/{api_version}/issue/{issue_key}"
        f"?fields=summary,status,issuetype,assignee,description,labels,"
        f"comment,priority,reporter,created,updated,resolution,resolutiondate,"
        f"issuelinks,fixVersions"
    )

    # Build auth header
    headers: dict[str, str] = {"Accept": "application/json"}
    if is_cloud and email:
        # Basic auth: email:token
        cred = base64.b64encode(f"{email}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    else:
        # Bearer auth (PAT) for Server/DC
        headers["Authorization"] = f"Bearer {token}"

    timeout = aiohttp.ClientTimeout(total=_JIRA_FETCH_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(issue_url, allow_redirects=False) as resp:
                if resp.status == 401:
                    raise SourceProviderError(
                        "Jira authentication failed. For Cloud, verify both email and API token in "
                        "dashboard.jira_auth settings."
                    )
                if resp.status == 403:
                    raise SourceProviderError(
                        "Jira access denied. The configured token may lack "
                        "permission to read this issue."
                    )
                if resp.status == 404:
                    raise SourceProviderError(f"Jira issue {issue_key} not found on {ref.host}.")
                if resp.status != 200:
                    raise SourceProviderError(f"Jira returned HTTP {resp.status} for {issue_key}.")
                # Bound response size to prevent memory exhaustion from an
                # oversized or malicious payload before JSON decoding. Streamed
                # to EOF: a single read(n) resolves on the first buffered chunk
                # of a chunked response and would hand json.loads a truncated
                # document.
                body = await read_capped_response(resp, _MAX_PAYLOAD_BYTES)
                if len(body) > _MAX_PAYLOAD_BYTES:
                    raise SourceProviderError(
                        f"Jira response for {issue_key} exceeds the size limit."
                    )
                try:
                    data = json.loads(body)
                except RecursionError:
                    raise SourceProviderError(
                        f"Jira response for {issue_key} is too deeply nested."
                    )
    except SourceProviderError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise SourceProviderError(
            f"Could not reach Jira at {ref.host}: {type(exc).__name__}"
        ) from exc
    except ValueError as exc:
        raise SourceProviderError(
            f"Jira returned an unparseable response for {issue_key}."
        ) from exc

    if not isinstance(data, dict):
        raise SourceProviderError("Jira returned an invalid issue payload")

    fields = data.get("fields")
    if not isinstance(fields, dict):
        fields = {}

    # Extract description
    raw_desc = fields.get("description")
    if isinstance(raw_desc, dict):
        # ADF (Cloud v3). The converter redacts internally, where it escapes:
        # `_adf_inline_sequence` for a run's text and `_adf_attr_label` for an
        # attribute. Both run inside its depth-capped traversal, so no unbounded
        # pre-pass walks a provider-controlled tree.
        description = _adf_to_markdown(raw_desc).strip()
    elif isinstance(raw_desc, str):
        # Plain text or wiki markup (Server v2)
        description = raw_desc
    else:
        description = ""

    # Extract status — use statusCategory.key which is locale-proof and
    # canonical ("done", "new", "indeterminate") rather than the English name.
    status_obj = _as_dict(fields.get("status"))
    status_category = _as_dict(status_obj.get("statusCategory"))
    category_key = str(status_category.get("key") or "").lower()
    state = "closed" if category_key == "done" else "open"

    # Resolution as state reason
    resolution = fields.get("resolution")
    state_reason = ""
    if isinstance(resolution, dict):
        state_reason = str(resolution.get("name") or "")

    # Reporter/author
    reporter = _as_dict(fields.get("reporter"))
    author = str(reporter.get("displayName") or reporter.get("name") or "")

    # Assignee
    assignee_obj = fields.get("assignee")
    assignees: list[str] = []
    if isinstance(assignee_obj, dict):
        name = str(assignee_obj.get("displayName") or assignee_obj.get("name") or "")
        if name:
            assignees.append(name)

    # Labels
    _raw_labels = fields.get("labels")
    raw_labels: list[Any] = _raw_labels if isinstance(_raw_labels, list) else []
    labels = [{"name": str(lbl), "color": "", "description": ""} for lbl in raw_labels if lbl]

    # Priority as a pseudo-label (Jira has no label colors)
    priority_obj = fields.get("priority")
    if isinstance(priority_obj, dict):
        pname = str(priority_obj.get("name") or "")
        if pname:
            labels.insert(0, {"name": f"Priority: {pname}", "color": "", "description": ""})

    # Issue type as a pseudo-label
    issuetype_obj = fields.get("issuetype")
    if isinstance(issuetype_obj, dict):
        tname = str(issuetype_obj.get("name") or "")
        if tname:
            labels.insert(0, {"name": tname, "color": "0052cc", "description": ""})

    # Comments
    comment_obj = fields.get("comment") or {}
    comment_list = _as_list(comment_obj.get("comments") if isinstance(comment_obj, dict) else [])
    partial_sections: list[str] = []
    total_comments = (
        _int_or_zero(comment_obj.get("total"))
        if isinstance(comment_obj, dict)
        else len(comment_list)
    )
    if total_comments > len(comment_list):
        _mark_partial(partial_sections, "comments")

    comments = []
    for c in comment_list[:_JIRA_MAX_COMMENTS]:
        c_author = _as_dict(c.get("author"))
        c_body_raw = c.get("body")
        if isinstance(c_body_raw, dict):
            c_body = _adf_to_markdown(c_body_raw).strip()
        elif isinstance(c_body_raw, str):
            c_body = c_body_raw
        else:
            c_body = ""
        comments.append(
            {
                "id": str(c.get("id") or ""),
                "author": str(c_author.get("displayName") or c_author.get("name") or ""),
                "body": c_body,
                "createdAt": str(c.get("created") or ""),
                "url": "",  # Jira comments have no standalone permalink
            }
        )

    # Fix versions -> the milestone slot. The contract holds exactly one, so a
    # ticket scheduled for several releases surfaces the pending one (falling
    # back to the first when all have shipped) and declares the rest partial
    # rather than dropping them silently.
    fix_versions = _jira_fix_versions(fields)
    chosen = _jira_pick_fix_version(fix_versions)
    milestone = _jira_fix_version_milestone(chosen) if chosen else None
    if len(fix_versions) > 1:
        _mark_partial(partial_sections, "fix versions")

    return {
        "provider": "jira",
        "url": ref.url,
        "number": ref.number,
        "title": str(fields.get("summary") or ""),
        "description": description,
        "state": state,
        "stateReason": state_reason,
        "author": author,
        "createdAt": str(fields.get("created") or ""),
        "updatedAt": str(fields.get("updated") or ""),
        "closedAt": str(fields.get("resolutiondate") or ""),
        "closedBy": "",  # Jira does not expose who resolved
        "labels": labels,
        "assignees": assignees,
        "milestone": milestone,  # Jira's Fix Version is its milestone equivalent
        "commentCount": total_comments,
        "locked": False,  # Jira has no issue locking concept
        "reactions": None,  # Jira has no reactions
        "comments": comments,
        "linkedChanges": _jira_linked_changes(fields, base_url),
        "partialSections": partial_sections,
    }


_T = TypeVar("_T")


def _finish_inflight(store: dict[str, asyncio.Task[_T]], url: str, task: asyncio.Task[_T]) -> None:
    """Drop a completed shared fetch and consume orphaned exceptions."""
    if store.get(url) is task:
        store.pop(url, None)
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()


def _direct_fetch_tasks() -> set[asyncio.Task[Any]]:
    """Snapshot unique direct full/issue/check tasks, including detached stale work.

    Issue fetches are counted here so their reservations are real: the pending
    cap and the retained-byte ceiling are computed from this set, so a task
    absent from it would hold a lease nothing ever reads.
    """
    tasks: set[asyncio.Task[Any]] = set(_CHECKS_FETCH_INFLIGHT.values())
    for full_tasks in _FULL_FETCH_TASKS.values():
        tasks.update(full_tasks)
    for issue_tasks in _ISSUE_FETCH_TASKS.values():
        tasks.update(issue_tasks)
    return tasks


def _direct_fetch_capacity_free(reservation_bytes: int) -> bool:
    """Whether a lease of ``reservation_bytes`` fits under both ceilings now."""
    tasks = _direct_fetch_tasks()
    reserved = sum(
        amount
        for task, amount in _DIRECT_FETCH_RESERVATIONS.items()
        if task in tasks and not task.done()
    )
    return (
        len(tasks) < _DIRECT_FETCH_PENDING_MAX
        and reservation_bytes <= _DIRECT_FETCH_MAX_RESERVED_BYTES - reserved
    )


async def _wait_for_direct_fetch_capacity(deadline: float) -> bool:
    """Sleep until a reservation is released or ``deadline`` passes.

    Returns True if a release was observed and the caller should re-check
    capacity, False if the wait budget is spent.

    MUST NOT be awaited while holding ``_CACHE_LOCK`` or ``_ISSUE_CACHE_LOCK``:
    an in-flight fetch takes the same lock to write its result, so waiting for it
    to finish while holding that lock would deadlock. Callers therefore release
    the lock, wait here, then re-acquire and re-check.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _DIRECT_FETCH_WAITERS.append(waiter)
    try:
        await asyncio.wait_for(waiter, timeout=remaining)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        if waiter in _DIRECT_FETCH_WAITERS:
            _DIRECT_FETCH_WAITERS.remove(waiter)


def _wake_direct_fetch_waiters() -> None:
    """Wake every waiter after a release; each re-checks its own ceiling.

    All waiters are woken rather than just the first: leases differ in size, so
    the head of the queue is not necessarily the one that now fits, and a
    freed lease that only satisfies a later waiter must not stall behind it.
    """
    waiters = list(_DIRECT_FETCH_WAITERS)
    _DIRECT_FETCH_WAITERS.clear()
    for waiter in waiters:
        if not waiter.done():
            waiter.set_result(None)


def _capacity_exhausted_error() -> SourceCapacityError:
    return SourceCapacityError("Too many source requests are pending; retry shortly.")


def _reserve_direct_fetch(task: asyncio.Task[Any], reservation_bytes: int) -> None:
    """Hold a conservative retained-byte lease until the task terminates."""
    _DIRECT_FETCH_RESERVATIONS[task] = reservation_bytes

    def release(done: asyncio.Task[Any]) -> None:
        _DIRECT_FETCH_RESERVATIONS.pop(done, None)
        # The lease is gone, so a queued caller may now fit. Scheduled on the loop
        # rather than woken inline: this runs during task teardown, and deferring
        # by one loop iteration means the waiter re-checks capacity after the task
        # is fully settled rather than mid-completion.
        asyncio.get_running_loop().call_soon(_wake_direct_fetch_waiters)

    task.add_done_callback(release)


async def fetch_pull_request_checks(raw_url: str) -> list[dict[str, Any]]:
    """Fetch current CI checks, coalescing concurrent requests for one URL."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    deadline = time.monotonic() + _DIRECT_FETCH_WAIT_SECS
    while True:
        task = _CHECKS_FETCH_INFLIGHT.get(ref.url)
        if task is not None:
            break
        if _direct_fetch_capacity_free(_CHECKS_FETCH_RESERVATION_BYTES):
            task = asyncio.create_task(_fetch_pull_request_checks_uncached(ref))
            _CHECKS_FETCH_INFLIGHT[ref.url] = task
            _reserve_direct_fetch(task, _CHECKS_FETCH_RESERVATION_BYTES)

            def finish_checks(done: asyncio.Task[list[dict[str, Any]]]) -> None:
                _finish_inflight(_CHECKS_FETCH_INFLIGHT, ref.url, done)

            task.add_done_callback(finish_checks)
            break
        if not await _wait_for_direct_fetch_capacity(deadline):
            raise _capacity_exhausted_error()
    return await asyncio.shield(task)


async def _fetch_pull_request_uncached(
    ref: SourceRef, generation: int, *, refresh: bool = False
) -> dict[str, Any]:
    # A registered plugin is dispatched from HERE, inside the shared layer, so it
    # inherits redaction, the byte cap, the cache write, the generation guard and
    # the chip-status projection without being able to opt out of any of them.
    plugin = _plugin_for_change(ref)
    fetched: SourceChangePayload | dict[str, Any]
    if plugin is not None:
        try:
            with _plugin_errors(plugin.id):
                fetched = await plugin.fetch_full(ref, refresh=refresh)
        except SourceProviderNotConfigured as exc:
            raise _plugin_setup_error(plugin, exc) from exc
    elif ref.provider == "github":
        fetched = await _fetch_github(ref)
    else:
        fetched = await _fetch_gitlab(ref)
    data = _redact_provider_data(fetched)
    if not isinstance(data, dict):
        raise SourceProviderError("provider returned an invalid pull-request payload")
    payload_size = _payload_size_bytes(data)
    if payload_size > _MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider pull-request payload was too large")

    async with _CACHE_LOCK:
        if _FULL_FETCH_GENERATIONS.get(ref.url, 0) != generation:
            # A successful mutation invalidated this generation while provider
            # I/O was running. Return its result to existing waiters, but never
            # let pre-mutation data overwrite the post-mutation cache.
            return data
        now = time.monotonic()
        # Sweep expired entries on write, then cap by both recency count and
        # aggregate serialized weight. A PR combines several provider commands,
        # so per-command pipe limits alone do not bound retained cache memory.
        # Each entry ages by its own lifecycle-derived TTL (`_full_payload_ttl`).
        for key in [
            key
            for key, (stored_at, _, payload) in _CACHE.items()
            if now - stored_at >= _full_payload_ttl(payload)
        ]:
            del _CACHE[key]
        _CACHE[ref.url] = (now, payload_size, data)
        while (
            len(_CACHE) > _CACHE_MAX_ENTRIES
            or sum(entry[1] for entry in _CACHE.values()) > _CACHE_MAX_BYTES
        ):
            del _CACHE[min(_CACHE, key=lambda key: _CACHE[key][0])]
        # One provider read, both surfaces: project this payload onto the chip
        # cache so the sidebar cannot keep rendering an older lifecycle than the
        # detail panel it was just fetched for. Kept INSIDE the lock, in the same
        # transaction as the generation check and the `_CACHE` write, so a
        # provider mutation cannot land between the passing generation check and
        # this projection and republish pre-mutation status into the chip cache
        # (which would emit a stale `source_status` delta the full-cache
        # invalidation cannot undo). `record_full_payload_status` is synchronous
        # and never re-acquires `_CACHE_LOCK`, so running it here cannot deadlock.
        record_full_payload_status(ref.url, data)
    return data


# ── Conditional revalidation of an expired GitHub payload ────────────────────
# An expired full payload used to mean the whole provider fanout again (the core
# `gh pr view`, the files, review-comment and rollup reads, and the merge-state
# re-reads) even when nothing about the pull request had moved. GitHub's REST
# API honours `If-None-Match`, and an authenticated `304 Not Modified` is free on
# the primary rate limit, so an expired github.com payload is first REVALIDATED
# with small conditional GETs; only when one reports a change does the fanout
# run. Together the probes cover what the panel renders:
#   * `issues/{n}` -- its ETag moves with the pull request's `updated_at`, i.e.
#     title/body/labels/lifecycle (a reopen included), a push (synchronize),
#     reviews, and comments. `pulls/{n}` is deliberately NOT the probe: it
#     embeds the base/head repository objects whose live counters (open issues,
#     stars, pushed_at) change its ETag on a busy repository without the pull
#     request changing.
#   * `commits/{head_sha}/check-runs` and `commits/{head_sha}/status` -- CI
#     hangs off the commit, not the pull request, so `updated_at` never moves
#     for it; these two ETags do. Both are needed: check runs and legacy commit
#     statuses are separate resources, and `statusCheckRollup` renders both.
#     Skipped for a merged or closed pull request, whose CI the panel and the
#     chip no longer track.
# The merge pair (`mergeable`/`mergeStateStatus`) is recomputed lazily by GitHub
# and moves no validator; it is carried by the chip refresh, which drops the
# full payload on a changed merge pair (see the chip <-> full protocol).
# GitHub's GraphQL API -- what `gh pr view` speaks -- has no conditional
# requests at all, which is why the probes are REST.
#
# Strictly 304-only: a probe that answers 200 (including the first probe of a
# URL, which has no validator to send and only LEARNS the ETags) or that fails
# is "unknown", and unknown runs the fanout. Nothing is ever judged unchanged by
# comparing bodies. An explicit refresh, a mutation invalidation and any
# non-GitHub provider (GitLab, a registered plugin) skip the probes entirely.


@dataclass(frozen=True)
class _Revalidator:
    """The validators learned for one pull request URL. ``head_sha`` scopes the
    two commit-level ETags: a push moves the head, and the old commit's
    check-runs ETag would still answer 304 for a commit nobody looks at any
    more."""

    issue_etag: str
    checks_etag: str
    status_etag: str
    head_sha: str
    learned_at: float
    # When the payload these validators vouch for was last read in full. An
    # all-304 carries it forward unchanged; only a full read resets it, and
    # `_revalidate_pull_request` forces one once it is `_REVALIDATED_MAX_AGE_SECS`
    # old. ``None`` (validators built without a read behind them) is never
    # aged out.
    read_at: float | None = None


_REVALIDATORS: dict[str, _Revalidator] = {}
_REVALIDATORS_MAX = 512
# Check-runs are paged; the ETag is per exact URL, so the page size is part of
# the key and must not drift between probes.
_REVALIDATE_CHECK_RUNS_PAGE = 100


def _trim_revalidators() -> None:
    while len(_REVALIDATORS) > _REVALIDATORS_MAX:
        del _REVALIDATORS[min(_REVALIDATORS, key=lambda key: _REVALIDATORS[key].learned_at)]


async def _gh_conditional_get(
    path: str, etag: str, *, max_output_bytes: int = _METADATA_OUTPUT_BYTES
) -> _ConditionalRead:
    """One `gh api` GET carrying ``If-None-Match`` when a validator is known."""
    argv = ["gh", "api", path, "-i"]
    if etag:
        argv += ["-H", f"If-None-Match: {etag}"]
    return await _run_provider(
        *argv, max_output_bytes=max_output_bytes, parse=_parse_conditional_get("gh")
    )


def _payload_is_terminal(payload: dict[str, Any]) -> bool:
    state = _project_state(str(payload.get("state") or ""), draft=bool(payload.get("draft")))
    return state in _TERMINAL_CHIP_STATES


@dataclass(frozen=True)
class _ProbeOutcome:
    """``unchanged`` when every probe answered 304. ``learned`` carries the
    validators the probes returned, or ``None`` when a probe failed or the
    payload had no head to probe. On an all-304 they are already committed;
    on a 200 they describe a payload the cache does NOT hold yet, so the
    caller commits them only once the full read that follows has succeeded
    -- committed early, a fanout that then failed would leave the pre-change
    payload paired with post-change validators, and every later probe would
    answer 304 against it and re-stamp the stale payload as current."""

    unchanged: bool
    learned: _Revalidator | None


_PROBE_UNKNOWN = _ProbeOutcome(False, None)


def _commit_revalidator(url: str, learned: _Revalidator | None) -> None:
    if learned is None:
        return
    _REVALIDATORS[url] = learned
    _trim_revalidators()


async def _probe_github_payload(ref: SourceRef, payload: dict[str, Any]) -> _ProbeOutcome:
    """Ask GitHub whether the pull request ``payload`` describes has moved.

    Any probe failure is an "unknown" (not unchanged, nothing learned): the
    fanout that follows is the same read that would have run without probing,
    so a failing probe costs at most the probes themselves and can never hide
    a change.
    """
    head_sha = str(payload.get("headSha") or "")
    if not head_sha:
        return _PROBE_UNKNOWN
    known = _REVALIDATORS.get(ref.url)
    same_head = known is not None and known.head_sha == head_sha
    issue_etag = known.issue_etag if known else ""
    checks_etag = known.checks_etag if known and same_head else ""
    status_etag = known.status_etag if known and same_head else ""
    repo_api = f"repos/{quote(ref.owner)}/{quote(ref.repo)}"
    commit_api = f"{repo_api}/commits/{quote(head_sha)}"
    probes = [_gh_conditional_get(f"{repo_api}/issues/{ref.number}", issue_etag)]
    if not _payload_is_terminal(payload):
        probes.append(
            _gh_conditional_get(
                f"{commit_api}/check-runs?per_page={_REVALIDATE_CHECK_RUNS_PAGE}",
                checks_etag,
                max_output_bytes=_CHECKS_OUTPUT_BYTES,
            )
        )
        probes.append(
            _gh_conditional_get(
                f"{commit_api}/status", status_etag, max_output_bytes=_CHECKS_OUTPUT_BYTES
            )
        )
    try:
        reads = await asyncio.gather(*probes)
    except SourceProviderError as exc:
        logger.info("source revalidation probe failed; reading in full: %s", exc)
        return _PROBE_UNKNOWN
    issue = reads[0]
    checks = reads[1] if len(reads) > 1 else None
    status = reads[2] if len(reads) > 2 else None
    learned = _Revalidator(
        issue_etag=issue.etag or issue_etag,
        checks_etag=(checks.etag if checks else "") or checks_etag,
        status_etag=(status.etag if status else "") or status_etag,
        head_sha=head_sha,
        learned_at=time.monotonic(),
        read_at=known.read_at if known else None,
    )
    unchanged = all(read.status == 304 for read in reads)
    if unchanged:
        # A 304 vouches for the payload the cache already holds, so the
        # (re-confirmed) validators are safe to keep right away.
        _commit_revalidator(ref.url, learned)
    return _ProbeOutcome(unchanged, learned)


def _revalidation_applies(ref: SourceRef) -> bool:
    """Only a github.com pull request served by the built-in fetcher is probed;
    GitLab and registered plugins have no conditional read here."""
    return ref.provider == "github" and _plugin_for_change(ref) is None


async def _revalidate_pull_request(
    ref: SourceRef, generation: int, cached: tuple[float, int, dict[str, Any]]
) -> dict[str, Any]:
    """Serve the expired ``cached`` payload if the probes say it is current,
    else fall through to the full read. Runs under the same inflight slot and
    memory reservation as a full fetch, because it may become one."""
    _, size, payload = cached
    known = _REVALIDATORS.get(ref.url)
    if (
        known is not None
        and known.read_at is not None
        and time.monotonic() - known.read_at >= _REVALIDATED_MAX_AGE_SECS
    ):
        # Ceiling reached: read in full without asking, and forget the
        # validators so the next cycle learns a fresh set against this read
        # (kept, they would pin `read_at` and force every later cycle too).
        _REVALIDATORS.pop(ref.url, None)
        return await _fetch_pull_request_uncached(ref, generation)
    outcome = await _probe_github_payload(ref, payload)
    if outcome.unchanged:
        async with _CACHE_LOCK:
            # Re-stamp only the entry the probes vouched for: a mutation that
            # landed meanwhile has advanced the generation and dropped it, and a
            # concurrent write may have replaced it. Either way the caller still
            # gets the payload the probes confirmed current.
            if (
                _FULL_FETCH_GENERATIONS.get(ref.url, 0) == generation
                and _CACHE.get(ref.url) is cached
            ):
                _CACHE[ref.url] = (time.monotonic(), size, payload)
        return payload
    fresh = await _fetch_pull_request_uncached(ref, generation)
    # Only now does the cache hold a payload at least as new as the validators
    # describe (see _ProbeOutcome); a fanout that raised leaves the old ones.
    if outcome.learned is not None:
        _commit_revalidator(ref.url, replace(outcome.learned, read_at=time.monotonic()))
    return fresh


async def fetch_pull_request(raw_url: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch a PR/MR, sharing one provider fanout per normalized URL."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    now = time.monotonic()
    deadline = now + _DIRECT_FETCH_WAIT_SECS
    while True:
        async with _CACHE_LOCK:
            cached = _CACHE.get(ref.url)
            revalidate = cached is not None and not refresh and _revalidation_applies(ref)
            if cached and not refresh:
                age = time.monotonic() - cached[0]
                # Inside the open TTL every provider serves the entry as is. Past
                # it, a github.com entry is REVALIDATED below whatever its
                # lifecycle (the probe is one rate-limit-free request); a
                # provider with no conditional read keeps serving a finished
                # payload on its lifecycle clock instead.
                if age < _CACHE_TTL_SECS or (not revalidate and age < _full_payload_ttl(cached[2])):
                    return cached[2]
            task = _FULL_FETCH_INFLIGHT.get(ref.url)
            if task is not None:
                break
            if _direct_fetch_capacity_free(_FULL_FETCH_RESERVATION_BYTES):
                generation = _FULL_FETCH_GENERATIONS.get(ref.url, 0)
                if revalidate and cached is not None:
                    task = asyncio.create_task(_revalidate_pull_request(ref, generation, cached))
                else:
                    task = asyncio.create_task(
                        _fetch_pull_request_uncached(ref, generation, refresh=refresh)
                    )
                _FULL_FETCH_INFLIGHT[ref.url] = task
                _FULL_FETCH_TASKS.setdefault(ref.url, set()).add(task)
                _reserve_direct_fetch(task, _FULL_FETCH_RESERVATION_BYTES)

                def finish_full_fetch(done: asyncio.Task[dict[str, Any]]) -> None:
                    _finish_inflight(_FULL_FETCH_INFLIGHT, ref.url, done)
                    active = _FULL_FETCH_TASKS.get(ref.url)
                    if active is None:
                        return
                    active.discard(done)
                    if not active:
                        _FULL_FETCH_TASKS.pop(ref.url, None)
                        _FULL_FETCH_GENERATIONS.pop(ref.url, None)

                task.add_done_callback(finish_full_fetch)
                break
        # Outside the lock on purpose: the fetches being waited on take
        # _CACHE_LOCK themselves to write their result, so waiting under it would
        # deadlock. Re-checks the cache and the inflight map on wake, since
        # either may have been satisfied by whoever just finished.
        if not await _wait_for_direct_fetch_capacity(deadline):
            raise _capacity_exhausted_error()
    # Shield the shared fetch so one disconnected browser cannot cancel work
    # still awaited by another request for the same URL.
    return await asyncio.shield(task)


async def _fetch_issue_uncached(ref: SourceRef) -> dict[str, Any]:
    if ref.provider == "github":
        fetched = await _fetch_github_issue(ref)
    elif ref.provider == "gitlab":
        fetched = await _fetch_gitlab_issue(ref)
    elif ref.provider == "jira":
        fetched = await _fetch_jira_issue(ref)
    else:
        raise SourceProviderError(f"unsupported issue provider: {ref.provider}")
    data = _redact_provider_data(fetched)
    if not isinstance(data, dict):
        raise SourceProviderError("provider returned an invalid issue payload")
    payload_size = _payload_size_bytes(data)
    if payload_size > _MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider issue payload was too large")

    async with _ISSUE_CACHE_LOCK:
        now = time.monotonic()
        # Sweep expired entries on write, then cap by both recency count and
        # aggregate serialized weight -- an issue combines several provider
        # commands, so per-command pipe limits alone do not bound retained
        # cache memory. Deliberately NOT paired with `record_full_payload_status`:
        # an issue has no chip status, so projecting one would publish a
        # meaningless {ci, state} for a URL the sidebar never asks about.
        for key in [
            key
            for key, (stored_at, _, _) in _ISSUE_CACHE.items()
            if now - stored_at >= _CACHE_TTL_SECS
        ]:
            del _ISSUE_CACHE[key]
        _ISSUE_CACHE[ref.url] = (now, payload_size, data)
        while (
            len(_ISSUE_CACHE) > _CACHE_MAX_ENTRIES
            or sum(entry[1] for entry in _ISSUE_CACHE.values()) > _ISSUE_CACHE_MAX_BYTES
        ):
            del _ISSUE_CACHE[min(_ISSUE_CACHE, key=lambda key: _ISSUE_CACHE[key][0])]
    return data


async def fetch_issue(raw_url: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch an issue, sharing one provider fanout per normalized URL."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = parse_source_url(raw_url)
    if ref.kind != "issue":
        raise ValueError("This URL points at a pull request or merge request, not an issue.")
    # Jira issues require configured credentials. When none are available, the
    # ValueError propagates to the frontend which shows the "Open in Jira"
    # link-out fallback (same as the zero-config default before #2361).
    now = time.monotonic()
    deadline = now + _DIRECT_FETCH_WAIT_SECS
    while True:
        async with _ISSUE_CACHE_LOCK:
            cached = _ISSUE_CACHE.get(ref.url)
            if not refresh and cached and time.monotonic() - cached[0] < _CACHE_TTL_SECS:
                return cached[2]
            task = _ISSUE_FETCH_INFLIGHT.get(ref.url)
            if task is not None:
                break
            if _direct_fetch_capacity_free(_ISSUE_FETCH_RESERVATION_BYTES):
                task = asyncio.create_task(_fetch_issue_uncached(ref))
                _ISSUE_FETCH_INFLIGHT[ref.url] = task
                _ISSUE_FETCH_TASKS.setdefault(ref.url, set()).add(task)
                _reserve_direct_fetch(task, _ISSUE_FETCH_RESERVATION_BYTES)

                def finish_issue_fetch(done: asyncio.Task[dict[str, Any]]) -> None:
                    _finish_inflight(_ISSUE_FETCH_INFLIGHT, ref.url, done)
                    active = _ISSUE_FETCH_TASKS.get(ref.url)
                    if active is None:
                        return
                    active.discard(done)
                    if not active:
                        _ISSUE_FETCH_TASKS.pop(ref.url, None)

                task.add_done_callback(finish_issue_fetch)
                break
        # Outside the lock: see the matching note in fetch_pull_request.
        if not await _wait_for_direct_fetch_capacity(deadline):
            raise _capacity_exhausted_error()
    # Shield the shared fetch so one disconnected browser cannot cancel work
    # still awaited by another request for the same URL.
    return await asyncio.shield(task)


_CONTRIBUTORS_TTL_SECS = 6 * 60 * 60
_CONTRIBUTORS_MAX = 6
_contributors_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_contributors_lock = LoopBoundLock()
_contributors_inflight: dict[str, asyncio.Task[list[dict[str, str]]]] = {}


async def _fetch_github_contributors(ref: RepoRef, key: str) -> list[dict[str, str]]:
    raw = await _run_json(
        "gh",
        "api",
        f"repos/{ref.owner}/{ref.repo}/contributors?per_page={_CONTRIBUTORS_MAX}&anon=false",
    )
    contributors: list[dict[str, str]] = []
    for row in _as_list(raw)[:_CONTRIBUTORS_MAX]:
        login = str(row.get("login") or "").strip()
        if not login:
            continue
        # The display name needs a second lookup, but only when the login is a
        # safe GitHub handle -- an unexpected value never reaches the API path.
        name = login
        if _GH_LOGIN_RE.fullmatch(login):
            profile = _as_dict(await _run_json("gh", "api", f"users/{login}"))
            name = str(profile.get("name") or "").strip() or login
        contributors.append(
            {
                "login": login,
                "name": name,
                "avatarUrl": str(row.get("avatar_url") or ""),
                "profileUrl": f"https://github.com/{login}",
            }
        )
    # Names and avatar URLs are provider-controlled: redact secrets/exfil URLs
    # before they are cached or returned. The client renders them as text/<img>.
    contributors = _redact_provider_data(contributors)
    async with _contributors_lock:
        now = time.monotonic()
        stale_keys = [
            k for k, (at, _) in _contributors_cache.items() if now - at >= _CONTRIBUTORS_TTL_SECS
        ]
        for stale in stale_keys:
            del _contributors_cache[stale]
        _contributors_cache[key] = (now, contributors)
        while len(_contributors_cache) > _CACHE_MAX_ENTRIES:
            oldest = min(_contributors_cache, key=lambda k: _contributors_cache[k][0])
            del _contributors_cache[oldest]
    return contributors


async def fetch_app_contributors(url: str, *, refresh: bool = False) -> list[dict[str, str]]:
    """Return an app source repo's top contributors (GitHub only, v1).

    Each entry is ``{login, name, avatarUrl, profileUrl}`` -- ``name`` falls back
    to the login when the GitHub profile has no display name. Capped at six by
    commit count (the provider's default ordering). A non-github host returns
    ``[]`` (not an error) so the caller can simply hide the row; an unparseable
    or non-allowlisted URL raises ``ValueError`` (mapped to 400).
    """
    # Refresh the self-managed GitLab allowlist off the loop before parse_repo_url
    # reads the cached snapshot, mirroring fetch_pull_request.
    await ensure_gitlab_hosts_loaded()
    ref = parse_repo_url(url)
    if ref.provider != "github":
        return []
    key = f"{ref.host}/{ref.owner}/{ref.repo}"
    async with _contributors_lock:
        cached = _contributors_cache.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < _CONTRIBUTORS_TTL_SECS:
            return cached[1]
        task = _contributors_inflight.get(key)
        if task is None:
            task = asyncio.create_task(_fetch_github_contributors(ref, key))
            _contributors_inflight[key] = task
            task.add_done_callback(lambda done: _finish_inflight(_contributors_inflight, key, done))
    # Shield the shared fetch so one disconnected browser cannot cancel work
    # another concurrent view is still awaiting for the same repo.
    return await asyncio.shield(task)


def _provider_error_response(
    request: web.Request, operation: str, exc: SourceProviderError
) -> web.Response:
    """Audit and answer a failed provider read.

    Capacity pressure is reported under its own code and audit reason rather than
    as a generic provider error: nothing failed, the gateway was holding its
    concurrent-fetch memory ceiling. The code is what lets the client retry this
    one case instead of presenting it as a dead end, while a real provider error
    (auth, missing PR, malformed payload) still fails immediately.

    The audit event is emitted here rather than at the call sites so the reason
    cannot drift from the code the caller receives -- the two are one decision.
    Owning the ``json_response`` here also keeps the error-code contract scan
    honest: a helper that returned only the body would leave every call site
    passing an opaque variable (see test/test_error_code_contract.py).
    """
    if isinstance(exc, SourceCapacityError):
        code, reason = "source_busy", "capacity_exhausted"
    else:
        code, reason = "provider_error", "provider_error"
    _audit_source_api(request, operation, "failed", reason)
    return web.json_response({"error": str(exc), "code": code}, status=503)


async def api_pull_request_source(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request`` with ``{url, refresh?}``."""
    denied = _authorize_owner_request(
        request, "source.pull_request.read", allow_local_no_owner=True
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.read", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await fetch_pull_request(
            str(body.get("url") or ""), refresh=bool(body.get("refresh"))
        )
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.read", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.pull_request.read", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, "source.pull_request.read", exc)
    _audit_source_api(request, "source.pull_request.read", "completed")
    return web.json_response(data)


async def api_issue_source(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/issue`` with ``{url, refresh?}``.

    Same authorization, audit, and error mapping as
    :func:`api_pull_request_source` -- an issue read is credential-backed
    provider data too, so it is gated on the dashboard owner identically.
    """
    denied = _authorize_owner_request(request, "source.issue.read", allow_local_no_owner=True)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.issue.read", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await fetch_issue(str(body.get("url") or ""), refresh=bool(body.get("refresh")))
    except asyncio.CancelledError:
        _audit_source_api(request, "source.issue.read", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("jira_no_credentials:"):
            code = "jira_no_credentials"
        elif msg.startswith("jira_config_error:"):
            code = "jira_config_error"
        else:
            code = "invalid_request"
        _audit_source_api(request, "source.issue.read", "failed", code)
        return web.json_response({"error": msg, "code": code}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, "source.issue.read", exc)
    _audit_source_api(request, "source.issue.read", "completed")
    return web.json_response(data)


async def api_app_contributors(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/contributors`` with ``{url, refresh?}``.

    Same authorization, audit, and error mapping as
    :func:`api_pull_request_source`: contributor data is credential-backed
    provider data, so it is gated on the dashboard owner identically.
    """
    denied = _authorize_owner_request(
        request, "source.contributors.read", allow_local_no_owner=True
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.contributors.read", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        contributors = await fetch_app_contributors(
            str(body.get("url") or ""), refresh=bool(body.get("refresh"))
        )
    except asyncio.CancelledError:
        _audit_source_api(request, "source.contributors.read", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.contributors.read", "failed", "invalid_request")
        return web.json_response({"error": str(exc), "code": "invalid_request"}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, "source.contributors.read", exc)
    _audit_source_api(request, "source.contributors.read", "completed")
    return web.json_response({"contributors": contributors})


async def api_pull_request_checks(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/checks`` with ``{url}``."""
    denied = _authorize_owner_request(
        request, "source.pull_request.checks", allow_local_no_owner=True
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.checks", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        checks = await fetch_pull_request_checks(str(body.get("url") or ""))
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.checks", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.pull_request.checks", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, "source.pull_request.checks", exc)
    _audit_source_api(request, "source.pull_request.checks", "completed")
    return web.json_response({"checks": checks})


# Upper bound on URLs accepted per status request. Matches the Changes tab's
# own source cap (website/src/utils/pullRequestLinks.ts MAX_PULL_REQUEST_SOURCES)
# so one request covers a full strip, and caps the parse work for a hostile body.
STATUS_URLS_MAX = 64


async def api_pull_request_status(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/status`` with ``{urls: [...]}``.

    Returns the *cached* lightweight ``{ci, state}`` for each URL and kicks a
    bounded background refresh for stale entries — the same cache and pacing the
    sidebar chips use. Never blocks on a provider call: unknown URLs simply come
    back absent and appear on a later poll.
    """
    denied = _authorize_owner_request(
        request, "source.pull_request.status", allow_local_no_owner=True
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.status", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_urls = body.get("urls")
    if not isinstance(raw_urls, list):
        _audit_source_api(request, "source.pull_request.status", "failed", "invalid_request")
        return web.json_response({"error": "A list of pull-request URLs is required."}, status=400)
    try:
        await ensure_gitlab_hosts_loaded()
    except asyncio.CancelledError:
        # This is a direct await in the handler (unlike the read/checks/resolve
        # endpoints, which reach ensure() through a provider helper wrapped in
        # the cancellation guard below). A cancellation here would otherwise
        # unwind past the terminal ``completed`` audit, leaving an authorized
        # status attempt absent from the tamper-evident SEL chain. Pair it with
        # ``failed/request_cancelled`` — matching the body-parse guard above —
        # then re-raise.
        _audit_source_api(request, "source.pull_request.status", "failed", "request_cancelled")
        raise
    canonical: list[str] = []
    for value in raw_urls[:STATUS_URLS_MAX]:
        if not isinstance(value, str):
            continue
        try:
            # An issue URL reaching here would be scheduled for a chip refresh
            # and answered from the pull-request namespace. `_require_change_ref`
            # raises ValueError, which this loop already treats as "not a
            # supported source URL" and skips.
            ref = _require_change_ref(parse_source_url(value))
        except ValueError:
            continue
        if ref.url not in canonical:
            canonical.append(ref.url)
    statuses = {
        url: status for url in canonical if (status := get_cached_check_status(url)) is not None
    }
    refreshing = schedule_check_refresh(canonical)
    _audit_source_api(request, "source.pull_request.status", "completed")
    # ``refreshing`` lets the client re-poll shortly instead of on TTL pacing, so
    # a state change lands within seconds of the background refresh rather than up
    # to one extra TTL later. ``ttlSecs`` is the server's own cache TTL, so the
    # client's steady-state pacing tracks it instead of hardcoding a copy.
    return web.json_response(
        {
            "statuses": statuses,
            "refreshing": refreshing,
            "ttlSecs": CHECK_STATUS_TTL_SECS,
        }
    )


_GITHUB_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_=+-]{1,128}$")
_GITLAB_THREAD_ID_RE = re.compile(r"^[A-Fa-f0-9]{1,128}$")

_GITHUB_RESOLVE_MUTATION = (
    "mutation($threadId:ID!){resolveReviewThread(input:{threadId:$threadId})"
    "{thread{isResolved}}}"
)

_GITHUB_UNRESOLVE_MUTATION = (
    "mutation($threadId:ID!){unresolveReviewThread(input:{threadId:$threadId})"
    "{thread{isResolved}}}"
)

_GITHUB_THREAD_REPLY_MUTATION = (
    "mutation($threadId:ID!,$body:String!)"
    "{addPullRequestReviewThreadReply"
    "(input:{pullRequestReviewThreadId:$threadId,body:$body})"
    "{comment{id}}}"
)

# Comment bodies are user text passed as a single CLI argument (argv, never a
# shell string), so the only real risk is size. GitHub rejects bodies past 65536
# characters anyway, so refusing here turns a provider error into a clear local
# one and bounds the argument.
_MAX_COMMENT_CHARS = 65536


def _validated_comment_body(body: str) -> str:
    """Return a comment body that is safe and worth sending.

    Empty bodies are refused rather than posted: an accidental empty comment is
    visible to everyone on the pull request and cannot be removed from here.
    """
    text = (body or "").strip()
    if not text:
        raise ValueError("A comment body is required.")
    if len(text) > _MAX_COMMENT_CHARS:
        raise ValueError(f"A comment body must be at most {_MAX_COMMENT_CHARS} characters.")
    return text


# Node ids are provider-issued, but they are interpolated into a CLI argument,
# so they get the same shape check as review-thread ids before dispatch.
_GITHUB_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_=+-]{1,128}$")

_GITHUB_PULL_REQUEST_NODE_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo)"
    "{squashMergeAllowed mergeCommitAllowed rebaseMergeAllowed"
    " pullRequest(number:$number){id isDraft state autoMergeRequest{enabledAt}}}}"
)

_GITHUB_AUTO_MERGE_MUTATION = (
    "mutation($pullRequestId:ID!,$mergeMethod:PullRequestMergeMethod!)"
    "{enablePullRequestAutoMerge(input:{pullRequestId:$pullRequestId,mergeMethod:$mergeMethod})"
    "{pullRequest{autoMergeRequest{enabledAt}}}}"
)

_GITHUB_READY_MUTATION = (
    "mutation($pullRequestId:ID!)"
    "{markPullRequestReadyForReview(input:{pullRequestId:$pullRequestId})"
    "{pullRequest{isDraft}}}"
)

# GitHub merge methods in the order this dashboard prefers them, gated on what
# the repository actually allows: enabling auto-merge with a disallowed method
# fails, and the repository's allow-list is the only machine-readable signal
# GitHub exposes (there is no "default method" field in the API).
_GITHUB_MERGE_METHODS: tuple[tuple[str, str], ...] = (
    ("squashMergeAllowed", "SQUASH"),
    ("mergeCommitAllowed", "MERGE"),
    ("rebaseMergeAllowed", "REBASE"),
)

# GitLab stores draft state as a title prefix, but exposes a dedicated
# mutation that performs the transition itself. Using it keeps the prefix
# grammar (Draft:/[WIP]/...) the provider's problem and avoids a read-modify-
# write of the title, which would clobber a concurrent retitle and could
# mangle titles that merely start with a draft-like word ("Drafting widgets").
_GITLAB_SET_DRAFT_MUTATION = (
    "mutation($projectPath:ID!,$iid:String!,$draft:Boolean!)"
    "{mergeRequestSetDraft(input:{projectPath:$projectPath,iid:$iid,draft:$draft})"
    "{errors mergeRequest{draft}}}"
)


def _raise_on_graphql_errors(payload: Any, message: str) -> None:
    """Raise when a GraphQL response carries errors instead of a mutation result.

    GraphQL reports refusals in the body with HTTP 200, so a provider CLI that
    only fails on transport errors would let a rejected mutation look like a
    success. Both the transport-level ``errors`` array and the per-mutation
    ``errors`` field are checked, since GitLab uses the latter.
    """
    if not isinstance(payload, dict):
        raise SourceProviderError(message)
    if payload.get("errors"):
        raise SourceProviderError(message)
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    for result in data.values():
        if isinstance(result, dict) and result.get("errors"):
            raise SourceProviderError(message)


def _github_repository_node(payload: Any) -> dict[str, Any]:
    """Extract the repository node from a GraphQL pull-request node response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    repository = data.get("repository") if isinstance(data, dict) else None
    return repository if isinstance(repository, dict) else {}


async def _github_pull_request_node(ref: SourceRef) -> tuple[str, dict[str, Any]]:
    """Return the pull request's validated node id plus its repository node."""
    payload = await _run_json(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_GITHUB_PULL_REQUEST_NODE_QUERY}",
        "-f",
        f"owner={ref.owner}",
        "-f",
        f"repo={ref.repo}",
        "-F",
        f"number={ref.number}",
    )
    repository = _github_repository_node(payload)
    pull_request = repository.get("pullRequest")
    pull_request = pull_request if isinstance(pull_request, dict) else {}
    node_id = str(pull_request.get("id") or "")
    if not _GITHUB_NODE_ID_RE.fullmatch(node_id):
        raise SourceProviderError("GitHub did not return a usable pull-request id")
    return node_id, repository


async def _invalidate_full_payload_cache(url: str) -> None:
    """Supersede the FULL pull-request payload cache and its in-flight fetch.

    Deliberately does NOT touch the lightweight chip-status cache. A caller that
    has just written a fresh chip status (the changed-status path in
    ``_refresh_check_status``) must drop only the now-stale full payload behind
    the detail panel, not the chip entry it just produced — invalidating the
    chip here would pop that entry and bump its generation, spuriously
    re-judging the next refresh as "changed" and spinning the very
    mutual-invalidation loop this projection exists to avoid.
    """
    async with _CACHE_LOCK:
        _CACHE.pop(url, None)
        if _FULL_FETCH_TASKS.get(url):
            _FULL_FETCH_GENERATIONS[url] = _FULL_FETCH_GENERATIONS.get(url, 0) + 1
        else:
            _FULL_FETCH_GENERATIONS.pop(url, None)
        _FULL_FETCH_INFLIGHT.pop(url, None)


async def _invalidate_pull_request_cache(url: str) -> None:
    """Supersede cached and in-flight data before a provider mutation."""
    await _invalidate_full_payload_cache(url)
    _invalidate_check_status(url)


async def _github_thread_ref(raw_url: str, thread_id: str) -> SourceRef:
    """Validate a thread id AND prove it belongs to the pull request in the url.

    The ownership check is the security control: the thread id arrives from the
    browser, and without it an owner-authenticated mutation could be steered at a
    thread on an unrelated pull request. Shared by reply/unresolve so no future
    call site can skip it. Registered providers never reach it -- both callers
    dispatch a plugin ref to its own hook first -- so the plugin branch below is
    a fail-closed backstop for a future call site that forgets that dispatch,
    not a live path.
    """
    await ensure_gitlab_hosts_loaded()
    # The docstring above promises this is the one place reply/unresolve
    # cannot skip, so the kind check belongs here too — not only in the callers
    # that happen to repeat it.
    ref = _require_change_ref(parse_source_url(raw_url))
    if _plugin_for_change(ref) is not None:
        raise ValueError(
            f"Review-thread operations are not supported by the '{ref.provider}' source provider."
        )
    if ref.provider != "github":
        raise ValueError("Replying to review threads is only supported on GitHub so far.")
    if not _GITHUB_THREAD_ID_RE.fullmatch(thread_id or ""):
        raise ValueError("A valid thread id is required.")
    threads = await _run_json(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_GITHUB_REVIEW_THREADS_QUERY}",
        "-f",
        f"owner={ref.owner}",
        "-f",
        f"repo={ref.repo}",
        "-F",
        f"number={ref.number}",
    )
    if thread_id not in _github_thread_ids(threads):
        raise ValueError("Review thread does not belong to this pull request.")
    return ref


async def reply_to_review_thread(raw_url: str, thread_id: str, body: str) -> None:
    """Post a reply into an existing review thread."""
    text = _validated_comment_body(body)
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    hook = _require_plugin_hook(ref, "reply_to_thread", "Replying to review threads")
    if hook is not None:
        # The thread id is the plugin's own vocabulary: ownership and shape
        # validation are the hook's job, exactly as on the resolve path.
        await _invalidate_pull_request_cache(ref.url)
        with _plugin_errors(ref.provider):
            await hook(ref, thread_id, text)
        return
    ref = await _github_thread_ref(raw_url, thread_id)
    # Invalidate before dispatch, matching resolve: once the provider call
    # starts its remote result is uncertain under cancellation, so a stale
    # generation must already be unable to satisfy the post-write refresh.
    await _invalidate_pull_request_cache(ref.url)
    payload = await _run_json(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_GITHUB_THREAD_REPLY_MUTATION}",
        "-f",
        f"threadId={thread_id}",
        "-f",
        f"body={text}",
    )
    _raise_on_graphql_errors(payload, "could not post the reply")


async def unresolve_pull_request_thread(raw_url: str, thread_id: str) -> None:
    """Reopen a resolved review thread."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    # Reopen is the same provider capability as resolve, so it dispatches to the
    # same hook with `resolved=False` -- one hook, one capability flag
    # (`resolveThreads`), no way for a plugin to support one direction only.
    hook = _require_plugin_hook(ref, "resolve_thread", "Reopening review threads")
    if hook is not None:
        await _invalidate_pull_request_cache(ref.url)
        with _plugin_errors(ref.provider):
            await hook(ref, thread_id, resolved=False)
        return
    ref = await _github_thread_ref(raw_url, thread_id)
    await _invalidate_pull_request_cache(ref.url)
    payload = await _run_json(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_GITHUB_UNRESOLVE_MUTATION}",
        "-f",
        f"threadId={thread_id}",
    )
    _raise_on_graphql_errors(payload, "could not reopen the thread")


async def comment_on_pull_request(raw_url: str, body: str) -> None:
    """Post a top-level comment on the pull request itself (not a thread)."""
    text = _validated_comment_body(body)
    await ensure_gitlab_hosts_loaded()
    # Issue refs are refused here for the same reason the thread mutations refuse
    # them: this posts to /issues/{number}/comments, and on GitHub issues and pull
    # requests share one number counter, so an issue URL would publish a comment
    # on an unrelated issue that happens to carry the PR's number.
    ref = _require_change_ref(parse_source_url(raw_url))
    hook = _require_plugin_hook(ref, "comment", "Commenting")
    if hook is not None:
        await _invalidate_pull_request_cache(ref.url)
        with _plugin_errors(ref.provider):
            await hook(ref, text)
        return
    if ref.provider != "github":
        raise ValueError("Commenting is only supported on GitHub so far.")
    await _invalidate_pull_request_cache(ref.url)
    # Issue comments, because a pull request's conversation timeline IS its issue
    # timeline; the review-comment endpoints require a diff position.
    await _run_json(
        "gh",
        "api",
        "-X",
        "POST",
        f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments",
        "-f",
        f"body={text}",
    )


async def resolve_pull_request_thread(raw_url: str, thread_id: str) -> None:
    """Resolve a review thread after conservatively invalidating cached data."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    hook = _require_plugin_hook(ref, "resolve_thread", "Resolving review threads")
    if hook is not None:
        await _invalidate_pull_request_cache(ref.url)
        with _plugin_errors(ref.provider):
            await hook(ref, thread_id, resolved=True)
        return
    thread_pattern = _GITHUB_THREAD_ID_RE if ref.provider == "github" else _GITLAB_THREAD_ID_RE
    if not thread_pattern.fullmatch(thread_id or ""):
        raise ValueError("A valid thread id is required.")
    if ref.provider == "github":
        threads = await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={ref.owner}",
            "-f",
            f"repo={ref.repo}",
            "-F",
            f"number={ref.number}",
        )
        if thread_id not in _github_thread_ids(threads):
            raise ValueError("Review thread does not belong to this pull request.")
        # Invalidate before dispatch. Once the provider call starts its remote
        # result is uncertain under cancellation, so stale generations must
        # already be unable to refill or satisfy a post-mutation refresh.
        await _invalidate_pull_request_cache(ref.url)
        await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_RESOLVE_MUTATION}",
            "-f",
            f"threadId={thread_id}",
        )
    else:
        project = quote(ref.project, safe="")
        await _invalidate_pull_request_cache(ref.url)
        await _run_json(
            "glab",
            "api",
            "-X",
            "PUT",
            f"projects/{project}/merge_requests/{ref.number}/discussions/{thread_id}",
            "-f",
            "resolved=true",
            host=ref.host,
        )


async def _gitlab_merge_request(ref: SourceRef) -> dict[str, Any]:
    """Read a merge request so a mutation can refuse inapplicable requests."""
    project = quote(ref.project, safe="")
    details = await _run_json(
        "glab", "api", f"projects/{project}/merge_requests/{ref.number}", host=ref.host
    )
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid merge-request payload")
    return details


def _gitlab_is_draft(details: dict[str, Any]) -> bool:
    """Report draft state, tolerating the legacy ``work_in_progress`` field."""
    return bool(details.get("draft") or details.get("work_in_progress"))


# GitLab pipeline statuses that still have to finish. While one of these is the
# head pipeline's status, merge_when_pipeline_succeeds genuinely defers the
# merge; outside them there is nothing left to wait for and the same call
# merges right away.
_GITLAB_PENDING_PIPELINE_STATUSES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled", "manual"}
)


def _gitlab_has_pending_pipeline(details: dict[str, Any]) -> bool:
    """Report whether a pipeline would actually gate the merge."""
    pipeline = details.get("head_pipeline") or details.get("pipeline")
    if not isinstance(pipeline, dict):
        return False
    return str(pipeline.get("status") or "").lower() in _GITLAB_PENDING_PIPELINE_STATUSES


class ConfirmationRequired(ValueError):
    """A mutation refused because the caller has not acknowledged its effect.

    Distinct from an ordinary rejection so the response can carry a machine-
    readable marker: the request is not malformed and is not permanently
    refused, it is waiting on an acknowledgement the client can only make
    meaningfully once the server has told it what is actually at stake.
    """


async def enable_pull_request_auto_merge(
    raw_url: str, *, confirm_immediate_merge: bool = False
) -> str:
    """Enable auto-merge (merge once requirements pass) and return the method.

    Both providers are read first so an inapplicable request is refused before
    anything is dispatched. GitHub has a real auto-merge switch and refuses a
    draft or already-armed pull request. GitLab has none: its equivalent is a
    merge call flagged ``merge_when_pipeline_succeeds``, which merges
    **immediately** when no pipeline is pending. That makes the GitLab path a
    merge authorization, so when nothing would gate the merge the caller must
    pass ``confirm_immediate_merge`` to acknowledge it. The refusal is raised as
    ``ConfirmationRequired`` so a client can discover the hazard from the server
    rather than pre-emptively asserting consent: the acknowledgement is only
    ever sent in answer to this specific refusal, which keeps the guard live for
    the dashboard instead of degrading it into a constant.
    """
    # Warm the allowlist BEFORE parsing: a self-managed URL is validated against
    # the cached snapshot, so a cold mutation would otherwise be rejected as an
    # unsupported host (400) even though the operator authorized it.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    hook = _require_plugin_hook(ref, "enable_auto_merge", "Auto-merge")
    if hook is not None:
        await _invalidate_pull_request_cache(ref.url)
        with _plugin_errors(ref.provider):
            method = await hook(ref, confirm_immediate_merge=confirm_immediate_merge)
        # The contract is the merge METHOD as a string; a plugin returning
        # anything else degrades to "" rather than leaking a foreign shape into
        # the response the dashboard renders.
        return method if isinstance(method, str) else ""
    if ref.provider == "github":
        node_id, repository = await _github_pull_request_node(ref)
        pull_request = repository.get("pullRequest")
        pull_request = pull_request if isinstance(pull_request, dict) else {}
        if pull_request.get("isDraft"):
            raise ValueError(
                "GitHub cannot enable auto-merge on a draft pull request. "
                "Mark it ready for review first."
            )
        if pull_request.get("autoMergeRequest"):
            raise ValueError("Auto-merge is already enabled for this pull request.")
        method = next(
            (
                graphql_method
                for field, graphql_method in _GITHUB_MERGE_METHODS
                if repository.get(field)
            ),
            "",
        )
        if not method:
            raise ValueError("This repository does not allow any merge method.")
        # Invalidate before dispatch: once the provider call starts its remote
        # result is uncertain under cancellation, so stale generations must
        # already be unable to refill or satisfy a post-mutation refresh.
        await _invalidate_pull_request_cache(ref.url)
        payload = await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_AUTO_MERGE_MUTATION}",
            "-f",
            f"pullRequestId={node_id}",
            "-f",
            f"mergeMethod={method}",
        )
        _raise_on_graphql_errors(payload, "GitHub refused to enable auto-merge")
        return method.lower()
    details = await _gitlab_merge_request(ref)
    if _gitlab_is_draft(details):
        raise ValueError("GitLab cannot arm a draft merge request. Mark it ready for review first.")
    if details.get("merge_when_pipeline_succeeds"):
        raise ValueError("Auto-merge is already enabled for this merge request.")
    if not _gitlab_has_pending_pipeline(details) and not confirm_immediate_merge:
        raise ConfirmationRequired(
            "No pipeline is pending, so GitLab would merge this merge request "
            "immediately. Confirm the merge to proceed."
        )
    project = quote(ref.project, safe="")
    await _invalidate_pull_request_cache(ref.url)
    await _run_json(
        "glab",
        "api",
        "-X",
        "PUT",
        f"projects/{project}/merge_requests/{ref.number}/merge",
        "-f",
        "merge_when_pipeline_succeeds=true",
        host=ref.host,
    )
    return "pipeline"


async def mark_pull_request_ready(raw_url: str) -> None:
    """Take a draft pull/merge request out of draft state.

    Both providers expose a dedicated transition, so neither path rewrites the
    title: GitLab's draft prefix grammar stays the provider's concern and a
    concurrent retitle cannot be clobbered by this call.
    """
    # Warm the allowlist BEFORE parsing: a self-managed URL is validated against
    # the cached snapshot, so a cold mutation would otherwise be rejected as an
    # unsupported host (400) even though the operator authorized it.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    hook = _require_plugin_hook(ref, "mark_ready", "Marking a change ready for review")
    if hook is not None:
        await _invalidate_pull_request_cache(ref.url)
        with _plugin_errors(ref.provider):
            await hook(ref)
        return
    if ref.provider == "github":
        node_id, repository = await _github_pull_request_node(ref)
        pull_request = repository.get("pullRequest")
        pull_request = pull_request if isinstance(pull_request, dict) else {}
        if not pull_request.get("isDraft"):
            raise ValueError("This pull request is already ready for review.")
        await _invalidate_pull_request_cache(ref.url)
        payload = await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_READY_MUTATION}",
            "-f",
            f"pullRequestId={node_id}",
        )
        _raise_on_graphql_errors(payload, "GitHub refused to mark the pull request ready")
        return
    details = await _gitlab_merge_request(ref)
    if not _gitlab_is_draft(details):
        raise ValueError("This merge request is already ready for review.")
    await _invalidate_pull_request_cache(ref.url)
    payload = await _run_json(
        "glab",
        "api",
        "graphql",
        "-f",
        f"query={_GITLAB_SET_DRAFT_MUTATION}",
        "-f",
        f"projectPath={ref.project}",
        "-f",
        f"iid={ref.number}",
        "-F",
        "draft=false",
        host=ref.host,
    )
    _raise_on_graphql_errors(payload, "GitLab refused to mark the merge request ready")


# The three verdicts GitHub accepts when submitting a pending review. DISMISS and
# the other review endpoints are deliberately absent: this path exists to publish a
# draft the caller has read, not to act on reviews someone else submitted.
_REVIEW_SUBMIT_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES", "COMMENT"})
# The verdicts that GATE a merge, and therefore the ones whose attachment to a
# specific commit is load-bearing. A COMMENT review carries no verdict, so a head
# that moves under it costs nothing but misplaced line anchors.
_REVIEW_GATING_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES"})
# GitHub review ids are positive integers. Bounded and anchored because the value
# is interpolated into the REST path.
_GITHUB_REVIEW_ID_RE = re.compile(r"[1-9][0-9]{0,19}")


def _flatten_paginated(payload: Any) -> list[dict[str, Any]]:
    """Flatten a ``gh api --paginate --slurp`` result into one list of objects.

    ``--slurp`` returns an array of per-page arrays, but a single-page result from
    a caller without the flag is already flat, so both shapes are accepted rather
    than assuming one. Non-dict members are dropped: a malformed page must not
    smuggle a value past the scans that read these lists.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return out
    for item in payload:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, list):
            out.extend(p for p in item if isinstance(p, dict))
    return out


async def _github_pending_review(ref: SourceRef) -> dict[str, Any]:
    """Return the caller's own unsubmitted review on ``ref``, or empty fields.

    GitHub scopes a PENDING review to its author and permits only one per user
    per pull request, so the single PENDING entry this returns is necessarily the
    authenticated user's own draft -- there is no way to observe, or therefore to
    publish, someone else's.

    Beyond the body, two facts decide whether the draft may be published at all,
    so they are read here rather than re-derived at submit time:

    ``stale`` -- the draft's ``commit_id`` is not the pull request's live head.
    Publishing a verdict then attributes a review to code that was never read, and
    on a repository without stale-approval dismissal a stale ``APPROVE`` counts as
    a live approval of unreviewed code.

    ``contentRedacted`` -- redaction ALTERS the draft's own text (body or any of
    its inline comments). Submitting publishes GitHub's stored draft, not the
    redacted copy this returns, so a draft quoting a credential would be shown
    redacted here and published verbatim there. The mismatch is reported so the
    publish path can refuse rather than silently leak.
    """
    raw = await _run_json(
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews",
    )
    if not isinstance(raw, list):
        raise SourceProviderError("GitHub returned an invalid reviews payload")
    # Paginated: a pull request with more reviews than one page can carry would
    # otherwise hide the PENDING entry past the first 30 and read back as "no
    # draft" -- the same page-one blindness that made the comment scan unsafe.
    reviews = _flatten_paginated(raw)
    for entry in reviews:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("state") or "").upper() != "PENDING":
            continue
        review_id = str(entry.get("id") or "")
        raw_body = str(entry.get("body") or "")
        state = await _github_pull_request_state(ref)
        head_sha = str(state["headSha"])
        draft_sha = str(entry.get("commit_id") or "")
        texts = [raw_body]
        comments: list[dict[str, Any]] = []
        if review_id:
            comments = await _github_pending_review_comments(ref, review_id)
            texts.extend(str(c.get("body") or "") for c in comments)
        # The body is provider-controlled text on its way to the dashboard, so it
        # goes through the same redaction chokepoint as every other provider read
        # (fetch_pull_request, fetch_pull_request_checks). A hand-written draft can
        # quote a credential as easily as a comment can.
        return {
            "reviewId": review_id,
            "body": str(_redact_provider_data(raw_body)),
            # The inline comments are part of what a verdict publishes, and
            # `contentDigest` binds them, so they have to be RETURNED as well or the
            # digest would certify text the reader never saw -- consent to a body
            # standing in for consent to comments hidden behind it. Redacted the same
            # way and on the same chokepoint as the body.
            "comments": [
                {
                    "path": str(_redact_provider_data(str(c.get("path") or ""))),
                    "line": c.get("line"),
                    "body": str(_redact_provider_data(str(c.get("body") or ""))),
                }
                for c in comments
            ],
            "commitId": draft_sha,
            "headSha": head_sha,
            # Unknown draft or head sha counts as stale: fail closed rather than
            # treat an unanswerable question as "current".
            "stale": not (draft_sha and head_sha and draft_sha == head_sha),
            "contentRedacted": any(_redact_provider_data(t) != t for t in texts),
            "autoMergeArmed": bool(state["autoMergeArmed"]),
            "contentDigest": _review_content_digest(raw_body, comments),
            "staleDismissalEnabled": await _github_stale_dismissal_enabled(
                ref, str(state["baseRef"])
            ),
        }
    return {
        "reviewId": "",
        "body": "",
        "comments": [],
        "commitId": "",
        "headSha": "",
        "stale": False,
        "contentRedacted": False,
        "autoMergeArmed": False,
        "contentDigest": "",
        "staleDismissalEnabled": False,
    }


async def _github_pull_request_state(ref: SourceRef) -> dict[str, Any]:
    """The live facts a publish decision needs: head sha, and whether auto-merge is armed.

    One fetch for both, because they are read together on every publish path and the
    object carries them both.

    Fetches the object and reads the fields in Python rather than passing
    ``--jq .head.sha``: ``gh``'s jq output for a string is a BARE token, and
    ``_run_json`` feeds every response to ``json.loads``, which rejects it — so the
    jq form turned every read of this value into a 503.
    """
    payload = await _run_json(
        "gh",
        "api",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}",
    )
    if not isinstance(payload, dict):
        raise SourceProviderError("GitHub returned an invalid pull-request payload")
    head = payload.get("head")
    sha = str((head or {}).get("sha") or "").strip() if isinstance(head, dict) else ""
    # `auto_merge` is null when disarmed and an object when armed. Anything else
    # counts as armed: an unrecognised shape must not read as "safe".
    auto = payload.get("auto_merge")
    base = payload.get("base")
    base_ref = str((base or {}).get("ref") or "") if isinstance(base, dict) else ""
    return {
        "headSha": sha,
        "autoMergeArmed": auto is not None,
        "baseRef": base_ref,
    }


async def _github_stale_dismissal_enabled(ref: SourceRef, base_ref: str) -> bool:
    """Whether the base branch dismisses approvals when new commits land.

    This is the setting that makes a stale approval HARMLESS: with it on, GitHub
    itself dismisses the approval the moment the head moves, so an approval can
    never authorize a merge of code nobody reviewed -- no matter when auto-merge is
    armed or when the force-push lands relative to our own checks.

    Fail CLOSED: when NEITHER read can confirm the setting -- no protection at all,
    no permission on either surface, any error -- this returns False, which withholds
    APPROVE rather than assuming the branch is safe.

    Two reads, because they need different privileges. `GET /branches/{ref}/protection`
    is admin-only, so a non-admin reviewer on a properly protected repository would be
    refused APPROVE forever and sent back to github.com -- the context switch this
    feature removes. GraphQL's `branchProtectionRules` answers the same question at a
    lower privilege, so it is tried when REST cannot answer. The safety property is
    unchanged: only an explicit `true` from one of them opens the verdict.
    """
    if not base_ref:
        return False
    if await _github_rest_dismisses_stale(ref, base_ref):
        return True
    return await _github_graphql_dismisses_stale(ref, base_ref)


async def _github_rest_dismisses_stale(ref: SourceRef, base_ref: str) -> bool:
    """The admin-only REST read of the base branch's protection block."""
    try:
        payload = await _run_json(
            "gh",
            "api",
            f"repos/{ref.owner}/{ref.repo}/branches/{quote(base_ref, safe='')}/protection",
        )
    except Exception:
        logger.debug("branch protection unreadable via REST for %s", ref.url, exc_info=True)
        return False
    if not isinstance(payload, dict):
        return False
    reviews = payload.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        return False
    return reviews.get("dismiss_stale_reviews") is True


async def _github_graphql_dismisses_stale(ref: SourceRef, base_ref: str) -> bool:
    """The lower-privilege GraphQL read, used when REST cannot answer.

    A rule's `pattern` may be a glob (`releases/*`), so the branch is matched with
    fnmatch rather than by equality. Only a rule that BOTH matches this branch and has
    `dismissesStaleReviews` true counts -- a non-matching rule says nothing about the
    branch being merged into.
    """
    query = (
        "query($owner:String!,$name:String!){"
        " repository(owner:$owner,name:$name){"
        " branchProtectionRules(first:100){"
        " nodes{ pattern dismissesStaleReviews } } } }"
    )
    try:
        payload = await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={ref.owner}",
            "-F",
            f"name={ref.repo}",
        )
    except Exception:
        logger.debug("branch protection unreadable via GraphQL for %s", ref.url, exc_info=True)
        return False
    nodes = (((payload or {}).get("data") or {}).get("repository") or {}).get(
        "branchProtectionRules"
    ) or {}
    for rule in nodes.get("nodes") or []:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        if _branch_pattern_matches(pattern, base_ref) and rule.get("dismissesStaleReviews") is True:
            return True
    return False


def _branch_pattern_matches(pattern: str, ref: str) -> bool:
    """Does a GitHub branch-protection ``pattern`` cover branch ``ref``?

    GitHub matches these patterns **per path segment**: a single ``*`` never
    crosses a ``/`` and ``**`` is the only wildcard that spans segments. Python's
    :func:`fnmatch.fnmatch` has no pathname mode -- its ``*`` swallows separators
    -- so ``releases/*`` would match ``releases/1/2`` here while GitHub's rule
    does not cover that branch at all.

    That difference is not cosmetic: this predicate is what opens the APPROVE
    verdict, so a pattern that appears to match a branch it does not actually
    protect is a fail-OPEN -- a stale approval could survive a head change on a
    branch carrying no stale-dismissal rule. Compare segment by segment.

    Case-sensitive by design (``fnmatchcase``): branch names are, and plain
    ``fnmatch`` would fold case on macOS and Windows.
    """
    if not pattern or not ref:
        return False
    return _segments_match(pattern.split("/"), ref.split("/"))


def _segments_match(pat: list[str], seg: list[str]) -> bool:
    """Segment-wise glob match, where ``**`` consumes zero or more segments."""
    if not pat:
        return not seg
    if pat[0] == "**":
        return any(_segments_match(pat[1:], seg[i:]) for i in range(len(seg) + 1))
    if not seg:
        return False
    if not fnmatch.fnmatchcase(seg[0], pat[0]):
        return False
    return _segments_match(pat[1:], seg[1:])


async def _github_pull_request_head_sha(ref: SourceRef) -> str:
    """The pull request's live head sha, used to detect a stale draft review."""
    return str((await _github_pull_request_state(ref))["headSha"])


async def _github_pending_review_comments(ref: SourceRef, review_id: str) -> list[dict[str, Any]]:
    """A pending review's inline comments, ACROSS ALL PAGES.

    Needed because submission publishes every comment GitHub holds for the draft,
    not just the body this app can see -- so a credential hiding in an inline
    comment of a hand-written draft has to be detectable too, and the content
    digest has to cover them. Pagination is not optional here: the endpoint returns
    30 per page, so an unpaginated scan clears a draft whose 31st comment carries
    the credential and then publishes it.
    """
    raw = await _run_json(
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/comments",
    )
    return _flatten_paginated(raw)


def _review_content_digest(body: str, comments: list[dict[str, Any]]) -> str:
    """Stable digest of everything submitting this draft would publish.

    The review id identifies the review OBJECT, not its contents: GitHub lets a
    pending review's body be edited and its inline comments be added or removed
    under the same id. So an id match alone cannot prove the caller read what is
    about to go out -- this digest can, and the submit path requires the one the
    caller was shown.

    Sorted by comment id (stable and unique) so a re-ordered read of identical
    content yields the same digest, while an edited body, a moved line, or an
    added/removed comment all change it. Hashes the RAW text, because raw text is
    what GitHub publishes.
    """
    payload: list[dict[str, Any]] = [
        {
            "id": str(c.get("id") or ""),
            "path": str(c.get("path") or ""),
            "line": c.get("line"),
            "body": str(c.get("body") or ""),
        }
        for c in comments
    ]
    payload.sort(key=lambda c: str(c["id"]))
    blob = json.dumps({"body": body, "comments": payload}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def pull_request_pending_review(raw_url: str) -> dict[str, Any]:
    """Read the caller's unsubmitted draft review, so a client can show it.

    Separate from the submit call because publishing is only safe when the caller
    has been shown the exact draft it is about to publish: the id returned here is
    what :func:`submit_pull_request_review` requires back.
    """
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    if ref.provider != "github":
        raise ValueError("Draft reviews can only be read on GitHub pull requests.")
    return await _github_pending_review(ref)


async def submit_pull_request_review(
    raw_url: str, review_id: str, event: str, content_digest: str
) -> dict[str, Any]:
    """Publish an existing pending review with a verdict.

    ``review_id`` is required rather than resolved implicitly: a pending review may
    equally be one the human started by hand in the provider UI, so submitting
    whatever draft happens to exist could publish an unfinished review the caller
    never saw. Requiring the id the caller was shown -- and re-checking that it is
    still the pending one -- makes that impossible and turns a concurrent
    submit-or-replace into a rejection instead of a surprise.
    """
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    if ref.provider != "github":
        raise ValueError("Draft reviews can only be published on GitHub pull requests.")
    normalized = (event or "").strip().upper()
    if normalized not in _REVIEW_SUBMIT_EVENTS:
        raise ValueError("event must be APPROVE, REQUEST_CHANGES, or COMMENT.")
    if not _GITHUB_REVIEW_ID_RE.fullmatch(review_id or ""):
        raise ValueError("A valid review id is required.")
    pending = await _github_pending_review(ref)
    if pending.get("reviewId") != review_id:
        raise ValueError(
            "This draft review is no longer pending -- it was already submitted or "
            "replaced. Reload the pull request and try again."
        )
    # The id identifies the review OBJECT; GitHub lets its body be edited and its
    # inline comments change under the same id. So the id alone cannot prove the
    # caller read what is about to be published -- the digest of the current
    # content must match the one the caller was shown. REQUIRED, never optional:
    # an omitted digest that skipped the comparison would be a one-parameter
    # bypass of this whole guard.
    # What this endpoint does NOT check: whether the pending draft belongs to the
    # caller's own review RUN. It enforces identity of the DRAFT (`reviewId` must be
    # the pending one) and identity of its CONTENT (the digest), which is everything
    # visible from here -- run provenance lives in Sage's run records, which this
    # provider-level handler has no access to. Sage's publish bar checks it before
    # offering a verdict; a different caller publishing "the pending draft" is
    # publishing what it read, bound to what it read, and gets no run-coherence
    # guarantee from this endpoint.
    if not content_digest:
        raise ValueError(
            "A contentDigest is required: publishing must be bound to the exact "
            "draft contents that were displayed."
        )
    if content_digest != pending.get("contentDigest"):
        raise ValueError(
            "This draft changed after it was displayed -- its body or inline comments "
            "are no longer what you read. Reload the pull request and review it again."
        )
    # Submission publishes the draft GitHub stored, NOT the redacted copy the read
    # returned. So when redaction alters the draft's own text the two disagree: the
    # dashboard shows `[REDACTED]` while the published review would carry the
    # secret verbatim. Refuse -- a leak the user was shown as redacted is worse
    # than no publish button.
    if pending.get("contentRedacted"):
        raise ValueError(
            "This draft contains content that must be redacted (a credential or an "
            "unsafe URL), and publishing would post the original text. Edit or "
            "discard the draft on GitHub instead."
        )
    # A draft written against an older head reviewed code that is no longer there.
    # For APPROVE that is the dangerous case -- a repository without stale-approval
    # dismissal counts it as a live approval of unreviewed code -- but a verdict of
    # any kind, and inline comments anchored to vanished lines, are all wrong on a
    # moved head, so every event is refused rather than carving out an exception.
    if pending.get("stale"):
        raise ValueError(
            "This draft was written against an earlier commit and the pull request "
            "has moved since. Re-review the current head before publishing."
        )
    # The stale check above and the submit below cannot be atomic — GitHub's
    # submit-review API takes no expected-head parameter — so a force-push in that
    # window leaves a verdict on an unreviewed head. The post-submit dismissal
    # further down repairs that, EXCEPT when auto-merge is armed: then the approval
    # satisfies branch protection and GitHub can merge before the dismissal lands,
    # and nothing repairs a merge. Refuse APPROVE for exactly that combination
    # rather than removing the verdict outright.
    # Every remaining variant of the stale-approval race -- auto-merge armed before
    # OR after this check, a force-push landing inside the submit round trip, a
    # manual merge in that same window -- needs ONE precondition to do harm: the base
    # branch must NOT dismiss approvals when new commits land. With
    # `dismiss_stale_reviews` on, GitHub retracts the approval itself the moment the
    # head moves, so a stale approval can never authorize unreviewed code and the
    # timing of our own checks stops mattering. Requiring it closes the chain instead
    # of narrowing it, and it fails closed: unreadable protection (no admin rights,
    # no protection at all) withholds APPROVE.
    if normalized == "APPROVE" and not pending.get("staleDismissalEnabled"):
        raise ValueError(
            "Approve is unavailable because this pull request's base branch does not "
            "dismiss approvals when new commits are pushed (or its protection is not "
            "readable from here). Without that, an approval published now could "
            'outlive the commit it reviewed. Enable "Dismiss stale pull request '
            'approvals when new commits are pushed" on the base branch, or approve '
            "on GitHub."
        )
    if normalized == "APPROVE" and pending.get("autoMergeArmed"):
        raise ValueError(
            "Auto-merge is armed on this pull request, so an approval could merge it "
            "before a stale-head check could take the approval back. Disarm "
            "auto-merge to approve from here, or approve on GitHub where you can see "
            "the head you are approving."
        )
    # Invalidate before dispatch: once the provider call starts its remote result
    # is uncertain under cancellation, so a stale generation must already be unable
    # to refill or satisfy a post-mutation refresh.
    await _invalidate_pull_request_cache(ref.url)
    validated_head = str(pending.get("headSha") or "")
    await _run_json(
        "gh",
        "api",
        "-X",
        "POST",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/events",
        "-f",
        f"event={normalized}",
    )
    # GitHub's submit-review API takes no expected-head parameter, so the check
    # above and this call cannot be one atomic operation: a force-push landing in
    # between would leave a verdict attached to a head nobody reviewed. Re-read the
    # head and, for a GATING verdict, dismiss what we just published rather than
    # leave a silent stale approval. This is a compensating action, not atomicity --
    # it turns an invisible window into a visible, self-reverting one.
    if normalized in _REVIEW_GATING_EVENTS:
        landed_head = await _github_pull_request_head_sha(ref)
        if landed_head and validated_head and landed_head != validated_head:
            dismissed = await _github_dismiss_review(ref, review_id)
            await _invalidate_pull_request_cache(ref.url)
            if dismissed:
                raise SourceProviderError(
                    "The pull request head moved while this review was being "
                    f"published, so the {normalized} was dismissed again. Re-review "
                    "the new head."
                )
            raise SourceProviderError(
                "The pull request head moved while this review was being published, "
                f"and the resulting {normalized} could NOT be dismissed "
                "automatically. Dismiss it on GitHub: it applies to a commit that "
                "was not reviewed."
            )
    return {"submitted": True, "event": normalized}


async def _github_dismiss_review(ref: SourceRef, review_id: str) -> bool:
    """Dismiss a just-published review whose head moved under it. Never raises.

    Best-effort by design: the caller reports a different, louder error when this
    fails, because an undismissable stale approval is exactly the state a human has
    to know about.
    """
    try:
        await _run_json(
            "gh",
            "api",
            "-X",
            "PUT",
            f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/dismissals",
            "-f",
            "message=Dismissed automatically: the pull request head changed while "
            "this review was being published, so it applied to unreviewed code.",
            "-f",
            "event=DISMISS",
        )
        return True
    except Exception:
        logger.warning("could not dismiss a stale review on %s", ref.url, exc_info=True)
        return False


_LOCAL_DASHBOARD_OWNER_SUBJECTS = frozenset({"local-app", "local-startup"})

# The one owner-gate denial that gets a machine-readable label of its own. A
# token subject is fixed at mint time as ``owner_id or <bootstrap subject>``,
# and every refresh re-mints from the INCOMING subject, so a session signed in
# before ``KIROCREW_OWNER_ID`` was configured carries `local-app` /
# `local-startup` for its whole life. Once an owner exists, the gate denies
# that subject — correctly — but a generic ``403 forbidden`` gives the user no
# way to tell "sign in again" apart from any other authorization failure.
STALE_OWNER_SESSION_CODE = "stale_session_reauth"

# The no-owner mutation denial, labeled only for signed machine-local dashboard
# sessions (see ``_authorize_owner_request``). Reads pass for those subjects, so
# the panel renders live mutation buttons whose clicks would otherwise dead-end
# in a generic 403; the code lets the client say what to configure instead.
OWNER_NOT_CONFIGURED_CODE = "owner_not_configured"


def stale_owner_session_response(request: web.Request) -> web.Response | None:
    """The distinct denial label for a signed pre-owner bootstrap session.

    Called strictly AFTER an owner-gate deny decision has been made: it never
    grants, widens, or re-orders access — it only chooses the response body for
    a request that is already refused. Returns the ``401 stale_session_reauth``
    body when the denied caller is a SIGNED dashboard-user bootstrap subject
    while an owner is configured, and ``None`` for every other denied caller,
    who keeps the call site's existing generic response. The discriminator is
    reserved for already-authenticated callers on purpose: an unsigned, absent,
    or app-token caller must not learn which denial class it hit.

    401 rather than 403 because re-authentication is the remedy — the caller's
    credential is stale, not merely under-privileged. Only a fresh sign-in (a
    newly minted token, whose subject is derived from the now-configured owner)
    clears it; a token refresh cannot, since refresh preserves the subject.
    """
    caller = str(request.get("user") or "")
    if request.get("app") != "":
        # App tokens keep their generic denial, and an absent app claim means
        # the middleware never authenticated this caller as a dashboard user.
        return None
    if caller not in _LOCAL_DASHBOARD_OWNER_SUBJECTS:
        return None
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    if not owner_id:
        return None
    return web.json_response(
        {
            "error": "this session predates the configured owner; sign in again",
            "code": STALE_OWNER_SESSION_CODE,
        },
        status=401,
    )


def is_owner_dashboard_request(request: web.Request) -> bool:
    """Return whether request has a configured or implicit local owner identity."""
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    if "app" not in request or request["app"] != "" or not caller:
        return False
    if owner_id:
        return caller == owner_id
    return caller in _LOCAL_DASHBOARD_OWNER_SUBJECTS


def _audit_source_api(
    request: web.Request,
    operation: str,
    outcome: str,
    error: str = "",
) -> None:
    """Best-effort source API audit without sensitive request or provider data."""
    caller = str(request.get("user") or "anonymous")
    try:
        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="dashboard",
            error=error,
        )
    except Exception:
        logger.debug("SEL source API audit failed", exc_info=True)


def _authorize_owner_request(
    request: web.Request, operation: str, *, allow_local_no_owner: bool = False
) -> web.Response | None:
    """Require an explicit dashboard-user claim matching the configured owner.

    When no owner is configured, read-only operations may allow either signed
    standalone-local bootstrap identity. Mutations remain owner-only. Once an
    owner is configured, every operation requires an exact owner match.
    """
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    if not owner_id:
        is_local_dashboard = request.get("app") == "" and caller in _LOCAL_DASHBOARD_OWNER_SUBJECTS
        if allow_local_no_owner and is_local_dashboard:
            return None
        _audit_source_api(request, operation, "denied", "owner_not_configured")
        if is_local_dashboard:
            # The one caller class that could legitimately reach this refusal
            # from the UI: a signed machine-local dashboard session whose reads
            # already succeeded, clicking a mutation button. A generic
            # ``forbidden`` reads as a dead end, so name the remedy with a
            # machine-readable code the client can translate into guidance.
            # The discriminator stays reserved for signed local subjects — an
            # unsigned, absent, or app-token caller must not learn which
            # denial class it hit (same rule as ``stale_owner_session_response``).
            return web.json_response(
                {
                    "error": (
                        "this action needs a configured owner, which Kiro Crew"
                        " identifies by Slack member ID; set 'Owner Slack member"
                        " ID' in Settings → Channels → Slack, restart the"
                        " gateway, then sign in again"
                    ),
                    "code": OWNER_NOT_CONFIGURED_CODE,
                },
                status=403,
            )
        return web.json_response({"error": "forbidden"}, status=403)
    if "app" not in request or request["app"] != "":
        _audit_source_api(request, operation, "denied", "app_token_not_allowed")
        return web.json_response({"error": "forbidden"}, status=403)
    if not caller:
        _audit_source_api(request, operation, "denied", "non_owner")
        return web.json_response({"error": "forbidden"}, status=403)
    if caller != owner_id:
        _audit_source_api(request, operation, "denied", "non_owner")
        # Deny decision made above; the helper only relabels the response for a
        # signed pre-owner bootstrap subject. Every other caller stays generic.
        stale = stale_owner_session_response(request)
        if stale is not None:
            return stale
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def api_pull_request_resolve(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/resolve`` mutation.

    Credential-backed provider access requires an explicit dashboard-user claim.
    Configured installations require an exact owner match. Standalone local
    installations accept only signed local bootstrap subjects. App tokens and
    missing auth claims fail closed.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await resolve_pull_request_thread(
            str(body.get("url") or ""), str(body.get("threadId") or "")
        )
        return {"resolved": True}

    return await _owner_mutation_response(request, "source.pull_request.resolve", action)


async def api_pull_request_unresolve(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/unresolve`` mutation.

    The counterpart to resolve: a thread closed by mistake, or reopened because
    the fix did not hold, has to be recoverable from the same surface.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await unresolve_pull_request_thread(
            str(body.get("url") or ""), str(body.get("threadId") or "")
        )
        return {"resolved": False}

    return await _owner_mutation_response(request, "source.pull_request.unresolve", action)


async def api_pull_request_reply(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/reply`` mutation.

    Posts a reply into an existing review thread under the dashboard owner's
    provider identity. Same auth, audit, and cache-invalidation contract as
    resolve, plus the thread-ownership proof that keeps a browser-supplied thread
    id from reaching an unrelated pull request.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await reply_to_review_thread(
            str(body.get("url") or ""),
            str(body.get("threadId") or ""),
            str(body.get("body") or ""),
        )
        return {"posted": True}

    return await _owner_mutation_response(request, "source.pull_request.reply", action)


async def api_pull_request_comment(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/comment`` mutation.

    A top-level comment on the pull request conversation, for the case that is
    not a reply to anyone's line.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await comment_on_pull_request(str(body.get("url") or ""), str(body.get("body") or ""))
        return {"posted": True}

    return await _owner_mutation_response(request, "source.pull_request.comment", action)


async def _owner_mutation_response(
    request: web.Request,
    operation: str,
    action: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> web.Response:
    """Run one owner-only provider mutation with shared auth, audit, and errors.

    Every provider mutation shares the same contract: an explicit owner claim,
    a JSON body, and terminal audit events that distinguish a client disconnect
    (remote outcome unknown) from a rejected request or a provider failure.
    """
    denied = _authorize_owner_request(request, operation)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        payload = await action(body)
    except asyncio.CancelledError:
        # The provider may have accepted the mutation before the client
        # disconnected, so record the uncertain outcome and preserve task
        # cancellation for aiohttp's shutdown/disconnect handling.
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, operation, "failed", "invalid_request")
        rejection: dict[str, Any] = {"error": str(exc)}
        if isinstance(exc, ConfirmationRequired):
            # Marks the refusal as answerable: the client may retry with the
            # acknowledgement, and only this reply justifies sending it.
            rejection["confirmationRequired"] = True
        return web.json_response(rejection, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, operation, exc)
    except Exception:
        _audit_source_api(request, operation, "failed", "internal_error")
        raise
    _audit_source_api(request, operation, "completed")
    return web.json_response(payload)


async def api_pull_request_auto_merge(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/auto-merge`` mutation.

    Authorizes the provider to merge the pull request once its requirements
    pass. Same credential boundary as the resolve mutation.

    ``confirmImmediateMerge`` must be a real JSON boolean. Coercing it with
    ``bool()`` would let any truthy value -- notably the string ``"false"`` --
    read as consent, so a malformed client would silently satisfy the very guard
    that stands between it and an immediate merge.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        confirm = body.get("confirmImmediateMerge", False)
        if confirm is not True and confirm is not False:
            raise ValueError("confirmImmediateMerge must be true or false.")
        method = await enable_pull_request_auto_merge(
            str(body.get("url") or ""),
            confirm_immediate_merge=confirm,
        )
        return {"autoMerge": True, "mergeMethod": method}

    return await _owner_mutation_response(request, "source.pull_request.auto_merge", action)


async def api_pull_request_ready(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/ready`` mutation.

    Takes the pull/merge request out of draft. Same credential boundary as the
    resolve mutation.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await mark_pull_request_ready(str(body.get("url") or ""))
        return {"ready": True}

    return await _owner_mutation_response(request, "source.pull_request.ready", action)


async def api_pull_request_pending_review(request: web.Request) -> web.Response:
    """POST ``/api/source/pull-request/pending-review`` with ``{url}``.

    A read, gated like :func:`api_pull_request_source` rather than like the
    mutations: it returns the same class of credential-backed provider data, so a
    stricter gate here would hide the draft on installations that can already read
    the pull request itself.
    """
    operation = "source.pull_request.pending_review"
    denied = _authorize_owner_request(request, operation, allow_local_no_owner=True)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await pull_request_pending_review(str(body.get("url") or ""))
    except asyncio.CancelledError:
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, operation, "failed", "invalid_request")
        return web.json_response({"error": str(exc), "code": "invalid_request"}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, operation, exc)
    _audit_source_api(request, operation, "completed")
    return web.json_response(data)


async def api_pull_request_submit_review(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/submit-review`` mutation.

    Publishes a pending review the caller has already been shown. Same credential
    boundary as the resolve mutation, and the strictest of the provider writes in
    consequence: submitting is irreversible and visible to everyone on the pull
    request.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        return await submit_pull_request_review(
            str(body.get("url") or ""),
            str(body.get("reviewId") or ""),
            str(body.get("event") or ""),
            str(body.get("contentDigest") or ""),
        )

    return await _owner_mutation_response(request, "source.pull_request.submit_review", action)


# ── Lightweight CI check status for sidebar chips ────────────────────────────
# Separate from the full PR cache: chips poll with the slots list, so this
# path must never block a slots response. Reads are served from this cache;
# refreshes are fire-and-forget with inflight dedup and a bounded map.

_CHECK_TTL_SECS = 60
# Public alias for periodic drivers (the owner-WS refresh loop) that pace
# their wakeups to the cache TTL. Sleeping exactly one TTL between rounds
# means each round finds the previous round's entries just expired — one
# provider fetch per URL per TTL, no wasted wakeups.
CHECK_STATUS_TTL_SECS = _CHECK_TTL_SECS
# The dashboard caps live slots at 500. Keeping one tiny status entry per slot
# avoids eviction churn when a large workspace is open.
_CHECK_CACHE_MAX = 512
# Bound both running and semaphore-waiting tasks. Overflow URLs receive a cache
# timestamp with no status, which backs them off for one TTL instead of creating
# a new task on every slots request.
_CHECK_PENDING_MAX = 16
# Public alias for periodic drivers that need to know the per-round admission
# cap so they can rotate which URLs they submit first across rounds (fair
# scheduling when the number of stale chips exceeds the cap).
CHECK_STATUS_PENDING_MAX = _CHECK_PENDING_MAX
_CHECK_UPDATE_DEBOUNCE_SECS = 0.1
# Bound concurrent gh/glab refresh operations so a cold cache across many
# sessions can't spawn a burst of provider subprocesses at once. TTL + inflight
# dedup handle rate and duplication; this caps instantaneous concurrency.
_CHECK_CONCURRENCY = 4
# These globals are loop-affine: the dashboard creates and mutates them only
# from its single asyncio event loop. They are not thread-safe by design.
_check_semaphore = asyncio.Semaphore(_CHECK_CONCURRENCY)
_check_cache: dict[str, tuple[float, dict[str, str] | None]] = {}
_check_inflight: set[str] = set()
# Bumped when a mutation supersedes a URL's status. A refresh that started
# before the bump must not write its now-stale result back into the cache,
# which would otherwise restore the pre-mutation state for up to one TTL.
_check_generations: dict[str, int] = {}
_CHECK_TASKS: set[asyncio.Task] = set()  # keep strong refs until done
_CheckUpdateCallback = Callable[[], None]
_check_update_callbacks: set[_CheckUpdateCallback] = set()
_check_update_handle: asyncio.TimerHandle | None = None
# An agent turn that touched a pull request (opened it, pushed, merged, drove a
# review round) is the highest-signal moment to re-read it, so the turn-boundary
# hook bypasses the TTL instead of waiting out the periodic rotation. The floor
# bounds a rapid multi-turn session to one forced provider read per URL per
# interval; every other caller stays on plain TTL pacing.
_CHECK_FORCE_MIN_INTERVAL_SECS = 10.0
_check_forced_at: dict[str, float] = {}
# URLs for which a turn-boundary forced refresh arrived while a (possibly
# pre-turn, now-stale) chip fetch was already in flight. The in-flight fetch is
# NOT floor-stamped (it may return pre-turn data), and when it completes
# ``_refresh_check_status`` issues exactly one follow-up forced read so the
# post-turn state is actually observed instead of waiting out the TTL.
_check_force_pending: set[str] = set()
# Status-delta sinks receive {"url", "ci"?, "state"?} whenever a URL's cached
# chip status CHANGES, so owner dashboards can invalidate the matching
# pull-request detail query immediately instead of waiting out a poll interval.
# Status is credential-backed, so sinks must be owner-scoped.
_StatusDeltaSink = Callable[[dict[str, str]], None]
_status_delta_sinks: set[_StatusDeltaSink] = set()
# Structural loop-breaker for the chip <-> full-payload mutual-invalidation
# protocol. The two caches project a provider's raw state independently and are
# kept equivalent only by convention ("keep the two in step"). Should they ever
# disagree on a URL's vocabulary (a bug), the chip refresh observes the SAME
# changed transition every TTL cycle — chip re-projects value A, the client's
# full refetch re-projects value B back into the cache — so the invalidation
# protocol would spawn a provider subprocess per URL per cycle indefinitely,
# silently. Rather than trust the two projections to stay identical as
# GitHub/GitLab vocabularies evolve, cap the blast radius: once a URL repeats an
# identical (previous -> new) changed-transition past this threshold, stop
# driving the loop for it and log loudly, so a divergence degrades to a stale
# glyph instead of an unbounded polling loop. A genuinely changing PR produces
# DISTINCT transitions, which resets the counter, so it is never damped.
_CHECK_FLAP_DAMP_THRESHOLD = 3
_check_flap: dict[str, tuple[tuple[str, str], int]] = {}
_check_flap_damped: set[str] = set()


def _status_sig(status: dict[str, str] | None) -> str:
    """Stable, order-independent signature of a chip status for flap detection."""
    if not status:
        return ""
    return "|".join(f"{key}={status[key]}" for key in sorted(status))


def _note_check_flap(url: str, previous: dict[str, str] | None, status: dict[str, str]) -> bool:
    """Track repeated identical changed-transitions; return True to damp the loop.

    See ``_check_flap`` above. The chip refresh calls this on every *changed*
    transition it is about to act on. When the same (previous -> new) transition
    recurs past ``_CHECK_FLAP_DAMP_THRESHOLD`` times in a row for one URL, the
    two projections are flapping and the caller must stop invalidating the full
    payload for it. Any different transition (a real state change) resets the
    counter and clears the damp.
    """
    transition = (_status_sig(previous), _status_sig(status))
    last, count = _check_flap.get(url, (None, 0))
    if transition == last:
        count += 1
    else:
        count = 1
        _check_flap_damped.discard(url)
    _check_flap[url] = (transition, count)
    if count >= _CHECK_FLAP_DAMP_THRESHOLD:
        if url not in _check_flap_damped:
            _check_flap_damped.add(url)
            logger.warning(
                "source-status: suppressing full-payload invalidation for %s — chip "
                "status keeps flapping (%s -> %s) every refresh, which means the chip "
                "and full-payload projections disagree on vocabulary (a bug); the chip "
                "glyph may now be stale until the two projections are reconciled",
                url,
                transition[0] or "<none>",
                transition[1] or "<none>",
            )
        return True
    return False


def _clear_check_flap(url: str) -> None:
    """Reset a URL's flap tracker after an authoritative full-payload write.

    Called by ``record_full_payload_status`` when the full fetch changes the
    cached status, so an interleaved full write is not misread as part of a
    repeating chip transition (which would otherwise falsely damp real churn).
    """
    _check_flap.pop(url, None)
    _check_flap_damped.discard(url)


# ── Repository visibility (public vs private) ────────────────────────────────
# Chip status (state / CI rollup) is credential-backed provider data, so it is
# sent only to the owner connection by default. But for a PUBLIC repository that
# same lifecycle state is world-visible on the provider's website, so withholding
# it from a legitimate authenticated dashboard user buys no confidentiality — it
# only removes the chip's most useful signal (is this PR merged / closed / green).
#
# This cache lets the status gate admit a public-repo link for a dashboard-user
# connection while keeping PRIVATE repos strictly owner-only. It is keyed by
# ``provider|host|owner|repo`` (not by PR URL): visibility is a property of the
# repository, and one repo backs many PR chips — so a per-repo entry, refreshed
# on the SAME cadence as the chip status it gates, costs at most one extra
# provider read per repo per TTL, shared across every chip on it.
#
# Fails CLOSED: until a repo is positively known public, ``is_repo_public``
# returns None and the gate treats it as owner-only. A provider read that errors
# or is unauthorized never flips a repo to public.
#
# TTL == the CHECK TTL, deliberately: the status a public flag authorizes is
# refreshed every ``_CHECK_TTL_SECS`` and ``schedule_visibility_refresh`` runs
# on the SAME calls, so visibility is never more than one refresh cycle staler
# than the status it gates. A repo that flips public->private therefore stops
# authorizing non-owner status within ~one check TTL (not an hour): the paired
# status refresh re-reads visibility, the flip is observed, and a stale entry
# fails closed the moment it crosses this TTL. This bounds the private-status
# exposure to a single short refresh window rather than a long one.
_VISIBILITY_TTL_SECS = _CHECK_TTL_SECS
_VISIBILITY_CACHE_MAX = 512
# provider|host|owner|repo -> (fetched_monotonic, is_public | None)
_visibility_cache: dict[str, tuple[float, bool | None]] = {}
_visibility_inflight: set[str] = set()
# Per-key counter bumped every time a force=True refresh fails a cached-public
# entry closed. ``_refresh_repo_visibility`` captures this at start and refuses
# to write a positive (public) result if the generation changed while it was
# fetching — i.e. a public->private force-invalidation landed mid-flight — so a
# stale in-flight read can never RESTORE public across a flip (GPT #6789
# round-14). The next refresh reconfirms from a fail-closed baseline.
_visibility_force_gen: dict[str, int] = {}
_VISIBILITY_TASKS: set[asyncio.Task] = set()
# Jira has no public-repo concept and its status is credential-gated regardless,
# so visibility is only meaningful for change providers.
_VISIBILITY_PROVIDERS = frozenset({"github", "gitlab"})


def _visibility_key(ref: SourceRef) -> str:
    return f"{ref.provider}|{ref.host}|{ref.owner}|{ref.repo}"


def _trim_visibility_cache() -> None:
    while len(_visibility_cache) > _VISIBILITY_CACHE_MAX:
        del _visibility_cache[min(_visibility_cache, key=lambda k: _visibility_cache[k][0])]


def is_repo_public(url: str) -> bool | None:
    """Whether the repo behind a source URL is known PUBLIC.

    Returns True (known public), False (known private), or None (not yet
    fetched / unknown / STALE / not a change provider). The status gate treats
    anything other than True as owner-only, so an unfetched, errored, or stale
    repo never leaks private status to a non-owner. Never blocks — reads the
    cache only.

    A cache entry older than ``_VISIBILITY_TTL_SECS`` is treated as STALE and
    returns None: a repo that flipped public->private while its visibility
    refresh kept failing must not keep authorizing status forever. The exposure
    is bounded to one TTL from the last SUCCESSFUL read (``_refresh_repo_visibility``
    never resets the timestamp on a failed read), after which this fails closed.
    """
    try:
        ref = parse_source_url(url)
    except Exception:
        return None
    if ref.provider not in _VISIBILITY_PROVIDERS:
        return None
    entry = _visibility_cache.get(_visibility_key(ref))
    if not entry:
        return None
    fetched_at, value = entry
    if time.monotonic() - fetched_at >= _VISIBILITY_TTL_SECS:
        return None
    return value


async def _fetch_repo_visibility(ref: SourceRef) -> bool | None:
    """Read a repo's public/private flag via the provider CLI. None on failure."""
    try:
        if ref.provider == "github":
            # isPrivate is False for BOTH public AND internal (GitHub Enterprise)
            # repos, but an internal repo is visible only to enterprise members —
            # NOT anonymously — so classifying it public would leak credential-
            # backed status to a non-owner (GPT #6789). Read `visibility` and
            # require exactly "public" (mirrors the GitLab "public"-only gate);
            # "internal"/"private" → owner-only. isPrivate is kept only as a
            # belt-and-braces private check.
            data = await _run_json(
                "gh",
                "repo",
                "view",
                f"{ref.owner}/{ref.repo}",
                "--json",
                "isPrivate,visibility",
            )
            if not isinstance(data, dict):
                return None
            if data.get("isPrivate") is True:
                return False
            vis = data.get("visibility")
            if isinstance(vis, str):
                return vis.lower() == "public"
            return None
        if ref.provider == "gitlab":
            # GitLab exposes repository visibility as public/internal/private.
            # Only "public" is world-readable without a credential; "internal"
            # is visible to authenticated instance members, which is NOT the
            # same as anonymous-public, so it stays owner-only.
            #
            # But a PUBLIC project can still restrict individual features:
            # merge_requests_access_level / builds_access_level can be "private"
            # (members only) or "disabled" even when the project is public, so a
            # credentialed refresh would otherwise surface member-only MR/CI
            # status to a non-owner (GPT #6789). Require the project to be public
            # AND both feature levels to be "enabled" (available at the project's
            # public visibility, i.e. anonymously readable) before treating the
            # PR/MR lifecycle + CI status as public. GitHub has no such per-
            # feature split — a public repo's PRs and checks are public.
            #
            # quote(ref.project, safe="") — NOT f"{owner}%2F{repo}": a subgroup
            # project path (group/subgroup/repo) has interior slashes that must
            # all be percent-encoded, and owner/repo drops the subgroup segment
            # entirely. Mirrors every other glab-api call site.
            project = quote(ref.project, safe="")
            data = await _run_json("glab", "api", f"projects/{project}", host=ref.host)
            if not isinstance(data, dict) or not isinstance(data.get("visibility"), str):
                return None
            if data["visibility"] != "public":
                return False
            # "enabled" = available at the project's (public) visibility level;
            # "private"/"disabled" restrict the feature to members. Missing keys
            # fail closed (owner-only) rather than assuming anonymous access.
            mr_level = data.get("merge_requests_access_level")
            ci_level = data.get("builds_access_level")
            # public_jobs (a.k.a. "Public pipelines") is a SEPARATE gate: when
            # False, a public project with builds_access_level "enabled" still
            # hides pipeline/job status from non-members, so a credentialed
            # refresh would leak private CI state to a non-owner (GPT #6789).
            # Require it True (missing → fail closed) before treating CI status
            # as anonymously public.
            public_jobs = data.get("public_jobs")
            return mr_level == "enabled" and ci_level == "enabled" and public_jobs is True
    except Exception:
        return None
    return None


async def _refresh_repo_visibility(
    ref: SourceRef,
    on_update: _CheckUpdateCallback | None = None,
    *,
    prev_public_override: bool | None = None,
) -> None:
    key = _visibility_key(ref)
    prev_entry = _visibility_cache.get(key)
    # Snapshot the force-invalidation generation at start. If a force=True
    # refresh fails this key closed WHILE we are fetching (generation bumps), our
    # read is stale w.r.t. that public->private flip, so we must NOT write back a
    # positive result that would restore ``public`` — we leave the fail-closed
    # unknown standing and let the next refresh reconfirm (GPT #6789 round-14).
    start_gen = _visibility_force_gen.get(key, 0)
    # The RENDERED gate value before this refresh: True only if a fresh public
    # entry exists (mirrors ``is_repo_public``'s TTL check). A change in this
    # boolean is exactly when a chip appears or disappears for a non-owner.
    #
    # ``prev_public_override`` carries the rendered-public value captured BEFORE
    # a forced pre-invalidation clobbered the cache entry to unknown. Without it,
    # the force path would read its own just-written (now, None) as prev_public
    # =False, so a genuine public->private transition compares False==False and
    # fires no update — leaving connected non-owners on the stale public chip
    # indefinitely (GPT #6789). The override restores the true baseline so the
    # hide-the-chip update is queued.
    if prev_public_override is not None:
        prev_public = prev_public_override
    else:
        prev_public = (
            bool(prev_entry[1]) and (time.monotonic() - prev_entry[0]) < _VISIBILITY_TTL_SECS
            if prev_entry
            else False
        )
    try:
        async with _check_semaphore:
            public = await _fetch_repo_visibility(ref)
    except Exception:
        public = None
    finally:
        _visibility_inflight.discard(key)
    prev = _visibility_cache.get(key)
    if public is not None and _visibility_force_gen.get(key, 0) != start_gen:
        # A force=True invalidation (public->private flip) landed while we were
        # fetching. Our positive read predates the flip, so restoring ``public``
        # here would re-open the leak the force path just closed. Discard the
        # stale positive: leave the fail-closed entry as-is (or record unknown)
        # so is_repo_public stays None until a post-flip refresh reconfirms.
        if prev is None:
            _visibility_cache[key] = (time.monotonic(), None)
    elif public is not None:
        # A positive read is authoritative: store the fresh value and reset the
        # TTL clock. This is the ONLY path that may mark a repo public.
        _visibility_cache[key] = (time.monotonic(), public)
    elif prev is not None:
        # Failed read. Keep the prior value but DO NOT reset the timestamp, so a
        # persistently-failing refresh cannot extend a stale ``public`` past its
        # TTL: it ages out from its last SUCCESSFUL read and ``is_repo_public``
        # then returns None (fail closed). This is the public->private +
        # visibility-read-fails hole — leaving the old timestamp bounds the
        # exposure to one TTL rather than forever.
        _visibility_cache[key] = (prev[0], prev[1])
    else:
        # Never successfully read: record an unknown so repeated cold failures
        # do not re-spawn a fetch every slots push (still returns None).
        _visibility_cache[key] = (time.monotonic(), None)
    _trim_visibility_cache()
    # Notify only when the RENDERED public flag flipped: a cold->public repo now
    # shows its chip status, and a public->private (or aged-out) repo hides it.
    # Without this a fresh visibility read never re-serialized the sidebar, so a
    # chip could stay bare until an unrelated push (GPT #6789).
    new_entry = _visibility_cache.get(key)
    new_public = bool(new_entry and new_entry[1] is True)
    if on_update is not None and new_public != prev_public:
        _queue_check_update(on_update)


def schedule_visibility_refresh(
    urls: list[str], on_update: _CheckUpdateCallback | None = None, *, force: bool = False
) -> None:
    """Kick bounded background visibility reads for repos not freshly cached.

    Fire-and-forget with inflight dedup, mirroring ``schedule_check_refresh``.
    One entry per repo (deduped by visibility key), TTL-paced, so a large
    workspace of PRs on a handful of repos costs a handful of reads per repo per
    TTL.

    ``on_update`` is invoked (debounced) whenever a repo's RENDERED public flag
    flips, so the sidebar re-serializes when a chip should appear or disappear.
    ``force`` bypasses the TTL freshness check for callers that know the repo's
    status just moved (the turn-boundary refresh), so visibility is revalidated
    in lockstep with the forced status read rather than lagging it.

    On the ``force`` path the cached PUBLIC flag is invalidated SYNCHRONOUSLY
    before the refresh task is spawned: the forced status read and the
    visibility read run as concurrent tasks, and if status finished first it
    could otherwise broadcast fresh (now-private) status against a still-cached-
    public visibility entry (a non-owner private-status leak). Dropping the entry
    to unknown up front makes ``is_repo_public`` fail closed for the whole
    in-flight window; the refresh restores ``public`` only on a positive
    reconfirmation, and its ``on_update`` re-serializes when it does.
    """
    now = time.monotonic()
    seen: set[str] = set()
    for url in dict.fromkeys(urls):
        try:
            ref = parse_source_url(url)
        except Exception:
            continue
        if ref.provider not in _VISIBILITY_PROVIDERS:
            continue
        key = _visibility_key(ref)
        if key in seen:
            continue
        seen.add(key)
        entry = _visibility_cache.get(key)
        if not force and entry and now - entry[0] < _VISIBILITY_TTL_SECS:
            continue
        prev_public_override: bool | None = None
        if force:
            # Bump the force generation on EVERY forced refresh, BEFORE the
            # inflight-dedup return below and regardless of the current cache
            # value. A forced refresh means "the status just moved, revalidate
            # now"; any visibility read already in flight (which may have started
            # before a public->private flip) must be treated as stale and
            # refused write-back. Gating this bump on "currently public" was a
            # hole: an entry already dropped to unknown (e.g. a first force
            # landed, then a second arrives while the pre-privacy fetch is still
            # in flight) would skip the bump, and that in-flight positive read
            # could then restore ``public`` (GPT #6789 round-15).
            _visibility_force_gen[key] = _visibility_force_gen.get(key, 0) + 1
            if entry is not None and entry[1] is True:
                # Capture the TRUE rendered-public baseline BEFORE clobbering, so
                # the refresh's on_update comparison measures the flip against
                # what non-owners currently see (public), not the unknown we are
                # about to write. Otherwise a public->private transition compares
                # False==False and never hides the chip (GPT #6789).
                prev_public_override = (now - entry[0]) < _VISIBILITY_TTL_SECS
                # Synchronously fail the entry closed so ``is_repo_public``
                # returns None for the whole in-flight window; the refresh
                # restores True only on a positive reconfirmation. Only clobber
                # when currently public — an already-unknown entry is already
                # fail-closed, and the generation bump above covers the stale
                # in-flight read either way.
                _visibility_cache[key] = (now, None)
        if key in _visibility_inflight:
            # A refresh is already running for this repo. We have already failed
            # a cached-public entry closed above on the force path, so the
            # in-flight result can only ever restore ``public`` via a positive
            # reconfirmation (never leave a stale public flag standing); dedup
            # the redundant spawn.
            continue
        _visibility_inflight.add(key)
        task = asyncio.get_running_loop().create_task(
            _refresh_repo_visibility(ref, on_update, prev_public_override=prev_public_override)
        )
        _VISIBILITY_TASKS.add(task)
        task.add_done_callback(_VISIBILITY_TASKS.discard)


def get_cached_check_status(url: str) -> dict[str, str] | None:
    """Cached status for a PR url: {"ci": ..., "state": ..., "mergeable": ...}.

    Every key is present only when known. ``mergeable``/``mergeStateStatus`` are
    omitted while the provider is still computing mergeability, so a client must
    treat their absence as "no news" rather than "nothing blocks the merge".
    Returns None until the first background refresh completes.
    """
    entry = _check_cache.get(url)
    return entry[1] if entry else None


def _trim_check_cache() -> None:
    while len(_check_cache) > _CHECK_CACHE_MAX:
        del _check_cache[min(_check_cache, key=lambda key: _check_cache[key][0])]
    if len(_check_generations) > _CHECK_CACHE_MAX:
        for url in [
            url
            for url in _check_generations
            if url not in _check_cache and url not in _check_inflight
        ]:
            del _check_generations[url]
    while len(_check_forced_at) > _CHECK_CACHE_MAX:
        del _check_forced_at[min(_check_forced_at, key=lambda key: _check_forced_at[key])]
    # Flap-tracking state is only meaningful while a URL is live in the cache;
    # drop entries for evicted URLs so these maps cannot outgrow the cache.
    if len(_check_flap) > _CHECK_CACHE_MAX:
        for stale in [key for key in _check_flap if key not in _check_cache]:
            _check_flap.pop(stale, None)
            _check_flap_damped.discard(stale)
    # Follow-up-force intent only matters while a fetch is actually in flight;
    # drop any stragglers for URLs no longer inflight so the set stays bounded.
    if len(_check_force_pending) > _CHECK_CACHE_MAX:
        for stale in [key for key in _check_force_pending if key not in _check_inflight]:
            _check_force_pending.discard(stale)


def _invalidate_check_status(url: str) -> None:
    """Drop a URL's cached chip status and supersede any in-flight refresh.

    The sidebar/source-strip chips read a separate, shorter-lived cache from the
    full pull-request payload, so a mutation that only busts the full cache
    would leave the chips showing pre-mutation state until their TTL expired.
    """
    _check_cache.pop(url, None)
    _check_generations[url] = _check_generations.get(url, 0) + 1
    _trim_check_cache()


def register_status_delta_sink(sink: _StatusDeltaSink) -> None:
    """Receive ``{"url", "ci"?, "state"?}`` whenever a chip status changes.

    Idempotent: registering the same bound method twice keeps one sink. Sinks
    must be owner-scoped — chip status is credential-backed provider data.
    """
    _status_delta_sinks.add(sink)


def unregister_status_delta_sink(sink: _StatusDeltaSink) -> None:
    _status_delta_sinks.discard(sink)


def _emit_status_delta(url: str, status: dict[str, str], origin: str) -> None:
    """Fan a changed status out to every registered sink, best-effort.

    ``origin`` records where the change was observed — ``"chip"`` (the
    lightweight refresh path) or ``"detail"`` (a full fetch's write-through) —
    and is **diagnostic only**: the client invalidates the detail payload for
    EVERY changed delta regardless of origin. It must, because a ``"detail"``
    delta is produced by the single window whose full fetch ran; only that
    window received the fresh HTTP payload, so the other owner windows (whose
    detail query is ``staleTime: Infinity``) would otherwise keep rendering the
    pre-change lifecycle. The initiating window's resulting refetch is harmless:
    ``record_full_payload_status`` only runs in the *uncached* fetch path, so
    the refetch hits the warm 30s cache and emits no further delta (no loop).
    The field is retained on the wire for diagnostics and possible future
    requester-aware routing; no consumer branches on it today.
    """
    if not _status_delta_sinks:
        return
    delta = {"url": url, "origin": origin, **status}
    for sink in tuple(_status_delta_sinks):
        with contextlib.suppress(Exception):
            sink(delta)


def _record_merge_state(result: dict[str, str], mergeable: str, merge_state: str) -> None:
    """Add the merge fields to a chip-status entry, each only once it is real.

    An unanswered field is left out entirely rather than written as ``unknown``:
    the chip cache is a short-TTL hint the client compares against its loaded
    pull-request payload, and "still computing" must not read as a disagreement
    with a real answer the payload already has. The two fields are recorded
    independently because GitLab settles `need_rebase` and its branch-protection
    gates in the detail field while ``mergeable`` stays ``unknown`` — dropping the
    detail because its sibling is unknown would leave exactly those banners
    invisible to the poll.
    """
    if _merge_state_real(mergeable):
        result["mergeable"] = mergeable
    if _merge_state_real(merge_state):
        result["mergeStateStatus"] = merge_state


def status_from_full_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    """Derive the lightweight chip status from a FULL pull-request payload.

    The sidebar chips and the detail panel must not read two independent caches
    with different TTLs, or they could each be "fresh" and still disagree. The
    full fetch is strictly richer than the chip fetch, so it write-throughs into
    the chip cache via this projection — one provider read, one truth, both
    surfaces. Shares the SAME projection helpers as ``_fetch_check_status``:
    ``_project_state`` for lifecycle, and — for CI — GitLab's authoritative
    ``ciStatus`` aggregate (stamped by ``_fetch_gitlab`` via
    ``_gitlab_aggregate_ci``, the same value the chip path reads) or, for GitHub,
    ``_rollup_ci`` over the ``statusCheckRollup`` buckets. Because both paths
    resolve the CI glyph from the identical aggregate, they cannot drift.
    """
    if not isinstance(payload, dict):
        return None
    result: dict[str, str] = {}
    # GitLab stamps an authoritative aggregate CI (`ciStatus`) — the same value
    # the chip path reads straight from the pipeline aggregate — so prefer it and
    # never roll up GitLab's faithful per-job buckets (which would count an
    # allow_failure red job, or miss a truncated one, and diverge from the chip).
    # GitHub has no separate aggregate: its `statusCheckRollup` buckets ARE the
    # aggregate, so fall back to rolling them up.
    ci_status = payload.get("ciStatus")
    if isinstance(ci_status, str) and ci_status:
        result["ci"] = ci_status
    else:
        buckets = [str(check.get("bucket") or "") for check in _as_list(payload.get("checks"))]
        ci = _rollup_ci(buckets)
        if ci is not None:
            result["ci"] = ci
    state = _project_state(str(payload.get("state") or ""), draft=bool(payload.get("draft")))
    if state is not None:
        result["state"] = state
    # The merge pair must be projected here too, not just by the chip read. If the
    # write-through omitted it, every full fetch would rewrite the chip entry
    # WITHOUT the fields the chip read had recorded, so the next chip refresh
    # would see a "change" and drop the full payload, which would write-through
    # and strip them again — the exact repeating chip↔full transition the flap
    # damper below exists to contain, spun by nothing but a projection gap.
    _record_merge_state(
        result,
        str(payload.get("mergeable") or ""),
        str(payload.get("mergeStateStatus") or ""),
    )
    return result or None


def record_full_payload_status(url: str, payload: dict[str, Any]) -> None:
    """Publish a full fetch's lifecycle/CI projection into the chip cache.

    Keeps the sidebar chips in lockstep with whatever the detail panel just
    rendered, and emits a delta so other owner windows converge too. Never
    invalidates the full cache — the caller just stored it.
    """
    status = status_from_full_payload(payload)
    if status is None:
        return
    previous = _check_cache.get(url)
    # A degraded full payload (a provider's secondary pipelines/jobs call failed,
    # so ``checks`` came back empty and is flagged in ``partialSections``) omits
    # the ``ci`` projection. Mirror ``_refresh_check_status``'s keep-known-status
    # rule: never let a transient partial fetch erase a CI glyph the chip cache
    # already knows, or the write-through would recreate the very chip/panel
    # divergence this projection exists to prevent. Only carry the field over
    # when ``checks`` is explicitly partial — a genuinely empty checks section
    # (no CI configured) must still be allowed to clear a stale glyph.
    if (
        "ci" not in status
        and previous
        and previous[1]
        and "ci" in previous[1]
        and "checks" in (payload.get("partialSections") or [])
    ):
        status = {**status, "ci": previous[1]["ci"]}
    # Never let a lazily-unsettled merge read erase a settled one. A first full
    # fetch commonly returns `unknown` (that is the bug this module's re-reads
    # address), so without this the write-through would strip a conflict the chip
    # cache already knew.
    status = _keep_known_merge_state(status, previous[1] if previous else None)
    _check_cache[url] = (time.monotonic(), status)
    _trim_check_cache()
    if previous is None or previous[1] != status:
        # A full-payload write is an independent, authoritative status change —
        # NOT another instance of the chip re-projecting the same value. Reset
        # this URL's flap tracker so the chip refresh's consecutive-transition
        # counter does not mistake "chip A→B, full B→C, chip C→B ..." for a
        # single repeating A→B loop and falsely damp legitimate CI churn (e.g.
        # three real re-runs of the same job).
        _clear_check_flap(url)
        # Lockstep visibility revalidation (GPT #6789): the full-payload writer
        # is a SECOND authoritative status writer alongside _refresh_check_status.
        # A public->private change whose owner detail fetch refreshes status here
        # would otherwise be served to a non-owner against a still-cached-public
        # visibility flag. force=True bypasses the visibility TTL and synchronously
        # fails a cached-public entry closed for the in-flight window (restoring
        # public only on positive reconfirmation), closing the same window the
        # chip-refresh path already guards. Bounded to real status transitions.
        with contextlib.suppress(Exception):
            schedule_visibility_refresh([url], force=True)
        _emit_status_delta(url, status, "detail")


def _flush_check_updates() -> None:
    """Coalesce completed refreshes into one slots broadcast per event-loop tick."""
    global _check_update_handle
    callbacks = tuple(_check_update_callbacks)
    _check_update_callbacks.clear()
    _check_update_handle = None
    for callback in callbacks:
        with contextlib.suppress(Exception):
            callback()


def _queue_check_update(callback: _CheckUpdateCallback) -> None:
    global _check_update_handle
    _check_update_callbacks.add(callback)
    if _check_update_handle is None:
        _check_update_handle = asyncio.get_running_loop().call_later(
            _CHECK_UPDATE_DEBOUNCE_SECS, _flush_check_updates
        )


def schedule_check_refresh(
    urls: list[str], on_update: _CheckUpdateCallback | None = None, *, force: bool = False
) -> list[str]:
    """Kick bounded background refreshes for stale URLs without blocking.

    Returns the URLs whose value is expected to change shortly — the ones this
    call started plus the ones a prior call already has in flight. Callers that
    serve the cache to a client (the source-strip status endpoint) use it to tell
    the client "poll again soon" instead of leaving it on TTL pacing, which would
    surface a just-refreshed state up to one extra TTL late. URLs deferred by the
    pending-work cap are deliberately excluded: they were backed off for a TTL,
    so nothing is coming sooner.

    ``force`` skips the TTL check for event-driven callers that know the remote
    state just moved (see ``request_check_refresh_now``). The pending cap and
    inflight dedup still apply, so a forced round can never outgrow a paced one.
    A finished PR ages by ``_TERMINAL_TTL_SECS`` instead of the open-PR TTL, and a
    MERGED one is not even force-read (``_chip_refresh_due``).
    """
    now = time.monotonic()
    refreshing: list[str] = []
    for url in dict.fromkeys(urls):
        entry = _check_cache.get(url)
        if not _chip_refresh_due(entry, now, force=force):
            continue
        if url in _check_inflight:
            refreshing.append(url)
            continue
        if len(_check_inflight) >= _CHECK_PENDING_MAX:
            # A paced (TTL) caller over the cap was backed off for a full TTL, so
            # renew its timestamp to stop the slots endpoint re-attempting every
            # poll. A forced caller (turn boundary) must instead stay eligible:
            # renewing the timestamp here would push the periodic sweep out by a
            # TTL and make the chip STALER in exactly the contention case force
            # exists for (more PR-linked slots than the pending cap).
            if not force:
                _check_cache[url] = (now, entry[1] if entry else None)
                _trim_check_cache()
            continue
        _check_inflight.add(url)
        refreshing.append(url)
        task = asyncio.get_running_loop().create_task(_refresh_check_status(url, on_update))
        _CHECK_TASKS.add(task)
        task.add_done_callback(_CHECK_TASKS.discard)
    return refreshing


def request_check_refresh_now(
    urls: list[str], on_update: _CheckUpdateCallback | None = None
) -> list[str]:
    """TTL-bypassing refresh for event-driven callers (agent turn boundaries).

    A finished agent turn is the moment a PR most likely changed, so waiting out
    the 60s chip TTL — or the periodic loop's rotation, which with more PR-linked
    slots than ``CHECK_STATUS_PENDING_MAX`` can take minutes — leaves both the
    chips and the detail panel visibly behind reality. Each URL is floored to one
    forced read per ``_CHECK_FORCE_MIN_INTERVAL_SECS`` so a burst of short turns
    cannot turn into a burst of provider subprocesses; URLs inside the floor fall
    back to normal TTL pacing rather than being dropped.
    """
    now = time.monotonic()
    eligible: list[str] = []
    paced: list[str] = []
    for url in dict.fromkeys(urls):
        last = _check_forced_at.get(url)
        if last is not None and now - last < _CHECK_FORCE_MIN_INTERVAL_SECS:
            paced.append(url)
            continue
        eligible.append(url)
    inflight_before = set(_check_inflight)
    refreshing = schedule_check_refresh(eligible, on_update, force=True)
    # Distinguish URLs this call actually STARTED from ones a prior fetch already
    # had in flight. `schedule_check_refresh` returns both, but only the started
    # ones did a fresh post-turn read.
    started = [url for url in refreshing if url not in inflight_before]
    already = [url for url in refreshing if url in inflight_before]
    # Burn the once-per-interval force allowance ONLY for URLs actually STARTED.
    # A URL deferred by the pending cap never ran, and an already-in-flight URL's
    # fetch may have started BEFORE this turn's final push landed — stamping
    # either as "just forced" would satisfy the floor with pre-turn data and lock
    # out the corrective read for CHECK_FORCE_MIN_INTERVAL.
    for url in started:
        _check_forced_at[url] = now
    # For URLs whose in-flight fetch predates this turn boundary, request exactly
    # one follow-up forced read on completion (see `_refresh_check_status`), so a
    # stale pre-turn result cannot pin the chip for a full TTL.
    for url in already:
        _check_force_pending.add(url)
    _trim_check_cache()
    if paced:
        refreshing.extend(schedule_check_refresh(paced, on_update))
    return refreshing


# Internal marker `_fetch_check_status` sets when the core chip read succeeded
# but the isolated rollup read alone failed (or described a different head).
# `_refresh_check_status` — the sole consumer — POPS it before the status is
# cached or compared, so only the documented chip keys
# ({state, ci, mergeable, mergeStateStatus}) ever reach slot serialization.
# It exists because "rollup unavailable" and "rollup empty" are otherwise the
# same absent `ci` key, and only the former may keep a previously known glyph.
_CHIP_CI_UNAVAILABLE = "ciUnavailable"


async def _refresh_check_status(url: str, on_update: _CheckUpdateCallback | None = None) -> None:
    previous = _check_cache.get(url)
    generation = _check_generations.get(url, 0)
    try:
        async with _check_semaphore:
            status = await _fetch_check_status(url)
    except Exception:
        status = None
    finally:
        _check_inflight.discard(url)
        if url in _check_force_pending:
            # A turn-boundary force arrived while THIS (possibly pre-turn) fetch
            # was in flight. Its result may predate the turn's final push, so
            # issue exactly one follow-up forced read now that the URL is free.
            # The follow-up starts fresh (not in flight), so it is not re-queued
            # here — at most one follow-up per in-flight collision, bounded.
            _check_force_pending.discard(url)
            with contextlib.suppress(Exception):
                schedule_check_refresh([url], on_update, force=True)
    if _check_generations.get(url, 0) != generation:
        # A mutation superseded this URL while the fetch was in flight, so the
        # result describes the pre-mutation state. Drop it rather than let it
        # overwrite the invalidated entry.
        return
    ci_unavailable = False
    if status is not None:
        # Strip the internal marker BEFORE the status is cached or compared:
        # cache entries feed owner slot serialization, which must only ever
        # carry the documented chip keys.
        ci_unavailable = bool(status.pop(_CHIP_CI_UNAVAILABLE, None))
        if not status:
            status = None
    # A transient provider failure must not erase a known status. It still
    # refreshes the timestamp so repeated slots requests respect the TTL.
    if status is None and previous:
        status = previous[1]
    elif (
        ci_unavailable
        and status is not None
        and "ci" not in status
        and previous
        and previous[1]
        and "ci" in previous[1]
    ):
        # The CI portion ALONE was unavailable this round (rollup read failed
        # or straddled a push) while the authorized core fields survived.
        # Mirror `record_full_payload_status`'s keep-known rule for a partial
        # `checks` section: a degraded read must not erase a glyph the cache
        # already knows — but a SUCCESSFUL rollup with zero checks (no marker)
        # must still be allowed to clear a stale one.
        status = {**status, "ci": previous[1]["ci"]}
    # Re-read the cache AFTER the provider await. The turn-boundary design makes
    # a concurrent full fetch the COMMON case: on `chat_done` the client
    # invalidates the detail payload (starting a full fetch) at the same moment
    # `refresh_slot_source_status` forces a chip refresh for the same URL. If the
    # full fetch resolved first it already wrote the fresh projection into
    # `_check_cache` via `record_full_payload_status`. Comparing against the
    # stale pre-await `previous` would then let this (possibly older) chip read
    # overwrite the newer value, spuriously judge "changed", drop the just-stored
    # full payload, and emit a redundant delta — roughly doubling provider cost
    # on every status-changing turn boundary and briefly broadcasting the wrong
    # status. `_check_inflight` dedups concurrent chip refreshes, so the only
    # writer that can land here is `record_full_payload_status`; when it did,
    # defer to it entirely rather than clobber the richer full-payload projection.
    latest = _check_cache.get(url)
    if latest is not previous:
        return
    if status is not None:
        # Same keep-known rule as the full-payload writer: an unsettled merge read
        # must not erase a settled one, or this refresh would strip the pair,
        # judge itself "changed", and drive the invalidation loop the flap damper
        # below contains.
        status = _keep_known_merge_state(status, previous[1] if previous else None)
    _check_cache[url] = (time.monotonic(), status)
    _trim_check_cache()
    changed = status is not None and (previous is None or previous[1] != status)
    if not changed:
        return
    assert status is not None  # narrowed by `changed`
    # Lockstep visibility revalidation (GPT #6789) — FIRST, before flap handling
    # and before the first ``await``. A status refresh can land a freshly-fetched
    # (possibly now-private) status while this URL's visibility entry is still
    # within its TTL, so ``is_repo_public`` would authorize the new status
    # against a stale-fresh public flag. ``schedule_visibility_refresh(force=True)``
    # SYNCHRONOUSLY fails a cached-public entry closed for the in-flight window
    # (it pre-invalidates before spawning the refresh task), so it must run
    # before any ``await`` yields the event loop and before the flap path's
    # early return — otherwise a concurrent slots push (or the flap path, which
    # returns without reaching the old call site) could observe the newly-cached
    # private status against an un-invalidated public flag. Bounded to real
    # status transitions only.
    with contextlib.suppress(Exception):
        schedule_visibility_refresh([url], on_update, force=True)
    # Structural loop-breaker: if this URL keeps repeating the identical chip
    # transition every refresh, the chip and full-payload projections disagree
    # on vocabulary and the mutual-invalidation protocol below would spin a
    # provider-polling loop forever. Cap the blast radius — still update the chip
    # cache and re-serialize the sidebar, but stop driving the full-payload
    # invalidation + delta that closes the loop, so the divergence degrades to a
    # stale glyph instead of an unbounded loop.
    if _note_check_flap(url, previous[1] if previous else None, status):
        if on_update:
            _queue_check_update(on_update)
        return
    # The chip cache just learned the PR moved, so the full payload behind the
    # detail panel is known-stale. Drop ONLY the full payload (rather than let it
    # live out its own TTL) and tell owner dashboards, so the panel and the chip
    # can never render two different lifecycles for the same PR. We must not
    # invalidate the chip cache here: this refresh just wrote the fresh chip
    # entry, and clearing it (as the mutation-path `_invalidate_pull_request_cache`
    # does) would bump the chip generation and re-judge the next refresh as
    # "changed", spinning the mutual-invalidation loop.
    with contextlib.suppress(Exception):
        await _invalidate_full_payload_cache(url)
    _emit_status_delta(url, status, "chip")
    if on_update:
        _queue_check_update(on_update)


async def _fetch_check_status(url: str) -> dict[str, str] | None:
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    # Belt-and-braces: the callers that feed this path already drop issue links
    # (see DashboardState.source_link_urls), but a chip refresh is what reaches
    # `gh pr view`, so refuse an issue URL here too rather than rely on every
    # future scheduling site remembering to filter.
    ref = _require_change_ref(parse_source_url(url))
    result: dict[str, str] = {}
    # A registered provider projects its own chip status. Optional: a plugin
    # without the hook simply contributes no {ci, state} glyph, which renders as a
    # plain chip -- strictly better than falling into the GitLab branch and
    # running `glab` against a host it knows nothing about.
    plugin = _plugin_for_change(ref)
    if plugin is not None:
        hook = getattr(plugin, "fetch_check_status", None)
        if not callable(hook):
            return None
        try:
            with _plugin_errors(plugin.id):
                status = await hook(ref)
        except SourceProviderNotConfigured as exc:
            raise _plugin_setup_error(plugin, exc) from exc
        if not isinstance(status, dict):
            return None
        # Same redaction and key discipline as a built-in read: only the two
        # fields the chip renders survive, and each must be a short string.
        projected = _redact_provider_data(
            {
                key: value
                for key, value in status.items()
                if key in {"ci", "state"} and isinstance(value, str) and len(value) <= 32
            }
        )
        return projected or None
    if ref.provider == "github":
        # The rollup is read separately from the core fields (#5115): `gh`
        # resolves a `--json` field set atomically, so bundling
        # `statusCheckRollup` here made a token without Checks read access lose
        # the state/draft/merge data it WAS authorized to read. The two reads
        # run concurrently; only the core read is load-bearing.
        data_raw, rollup_raw = await asyncio.gather(
            _run_json(
                "gh",
                "pr",
                "view",
                ref.url,
                "--json",
                "state,isDraft,mergeable,mergeStateStatus,headRefOid",
            ),
            _github_rollup_read(ref),
            return_exceptions=True,
        )
        if isinstance(data_raw, BaseException):
            raise data_raw
        data = data_raw
        if not isinstance(data, dict):
            return None
        if isinstance(rollup_raw, BaseException):
            # Core data survives; flag the CI portion unavailable so the cache
            # writer can keep a previously known glyph instead of erasing it.
            result[_CHIP_CI_UNAVAILABLE] = "1"
        else:
            checks, rollup_head = rollup_raw
            head_oid = str(data.get("headRefOid") or "")
            # A missing sha on either side deliberately fails open, same as
            # the full-payload guard: unverifiable must not mean unavailable.
            if head_oid and rollup_head and rollup_head != head_oid:
                # The two reads straddled a push — this rollup describes a
                # different commit. Treat it as unavailable rather than paint
                # another head's CI on this one; the next refresh re-pairs.
                result[_CHIP_CI_UNAVAILABLE] = "1"
            else:
                # Same projection AND the same latest-run collapsing as the
                # full payload (`_github_checks`, applied inside
                # `_github_rollup_read`), so the chip glyph cannot disagree
                # with the panel's own rollup — a superseded CANCELLED row must
                # not paint either red.
                buckets = [check["bucket"] for check in checks]
                ci = _rollup_ci(buckets)
                if ci is not None:
                    result["ci"] = ci
        raw_state = str(data.get("state") or "").upper()
        state = _project_state(raw_state, draft=bool(data.get("isDraft")))
        if state is not None:
            result["state"] = state
        _record_merge_state(result, *_github_merge_state(data))
        return result or None
    project = quote(ref.project, safe="")
    details = await _run_json(
        "glab", "api", f"projects/{project}/merge_requests/{ref.number}", host=ref.host
    )
    head_status = ""
    if isinstance(details, dict):
        # Same {state} vocabulary as the full-payload path via `_project_state`:
        # GitLab keeps `draft: true` on an MR closed while still in draft, so the
        # draft mapping is gated on the open state and `locked` folds into
        # `closed`. A mismatch here would ping-pong under the mutual
        # invalidation (chip "draft" ≠ cached "closed", drop, refetch, repeat).
        state = _project_state(
            str(details.get("state") or ""),
            draft=bool(details.get("draft") or details.get("work_in_progress")),
        )
        if state is not None:
            result["state"] = state
        _record_merge_state(result, *_gitlab_merge_state(details))
        # head_pipeline is the MR's own HEAD pipeline and ships with this same
        # payload, so the common case needs one provider call like GitHub does.
        head = details.get("head_pipeline")
        if isinstance(head, dict):
            head_status = str(head.get("status") or "").lower()
    if head_status:
        status = head_status
    else:
        pipelines = await _run_json(
            "glab",
            "api",
            f"projects/{project}/merge_requests/{ref.number}/pipelines?per_page=1",
            host=ref.host,
        )
        rows = _as_list(pipelines)
        status = str(rows[0].get("status") or "").lower() if rows else ""
    if status:
        # Project the pipeline AGGREGATE through the SAME helper the full-payload
        # path uses (`_gitlab_aggregate_ci`, consumed there via `ciStatus`), so
        # the chip glyph and the panel glyph cannot drift. The aggregate already
        # folds allow_failure into `success` and marks a blocking manual gate, so
        # it is the authoritative, lossless source for the single CI glyph.
        ci = _gitlab_aggregate_ci(status)
        if ci is not None:
            result["ci"] = ci
    return result or None
