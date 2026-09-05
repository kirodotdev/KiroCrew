"""Shared helpers used across handler submodules."""

from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
import logging
import os
import sys
import sysconfig
import time
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Callable, Iterable

import aiohttp
from aiohttp import web

from kiro_crew import extras, platform_compat
from kiro_crew.agent_discovery import (
    SKILL_URI_PREFIX,
    expand_skill_uri,
    skill_resource_uris,
)
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.state import VALID_MEMORY_MODES, DashboardState
from kiro_crew.dashboard.token_auth import (
    MAX_SESSION_TTL_SECS,
    _b64url_decode,
    required_peer_key_unverified,
)
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.messaging.privacy_mode import hydrate as _hydrate_conv_flags
from kiro_crew.messaging.privacy_mode import is_incognito as is_thread_incognito
from kiro_crew.messaging.privacy_mode import is_temporary as is_thread_temporary
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.skill_trust import is_project_trusted as _is_project_trusted
from kiro_crew.skills import skills_dir

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import CapabilityManager

logger = logging.getLogger(__name__)


def _redact_memory_field(val: object) -> object:
    """Redact credentials and exfiltration URLs from a memory field.

    Lives here (not in ``memory.py``) so handlers that ``memory.py`` itself
    imports from -- e.g. ``cron.py`` -- can share the chain without an import
    cycle.
    """
    if isinstance(val, (bytes, memoryview)):
        return None
    if isinstance(val, str):
        val, _ = redact_exfiltration_urls(val)
        val, _ = redact_credentials(val)
        return val
    if isinstance(val, list):
        return [_redact_memory_field(item) for item in val]
    if isinstance(val, dict):
        return {k: _redact_memory_field(v) for k, v in val.items()}
    return val


# Shared body cap for the small JSON-object endpoints that must bound the
# request BEFORE decoding (the strict-internal notification routes). Kept
# module-level and in one place so the security-relevant cap cannot drift
# between the two call sites (issue #490). 64 KB is generous — payload fields
# have their own caps.
_MAX_BODY_BYTES = 64 * 1024


async def read_bounded_json(
    request: web.Request,
    max_bytes: int | None = _MAX_BODY_BYTES,
    *,
    allow_absent: bool = False,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Read and parse a JSON *object* request body, capped at *max_bytes*.

    Returns ``(body, None)`` on success, or ``(None, error_response)`` when the
    caller should return early. This owns the parse-and-shape guard for the
    endpoints routed through it: ``await request.json()`` happily returns a
    list, string, or number for a body that is valid JSON but not an object, and
    a handler that then calls ``.get()`` on the result turns a client mistake
    into a 500 (issue #5587).

    NOT yet the dashboard's only such guard. Four siblings survive and diverge:
    ``handlers_channel._json_object`` (same ``invalid_json``/``body_not_object``
    codes, but raises ``HTTPBadRequest`` instead of returning the response),
    ``handlers/hooks.py::_json_object`` (``default_empty=True`` collapses a
    MALFORMED body to defaults -- the defect this issue fixed in
    ``api_memory_promote``), ``handlers/session_storage.py::_json_body``
    (deliberately different: an empty body is legitimate there, and it
    documents why), and ``handlers/artifacts.py::_read_json_body`` (raises
    ``ArtifactValidationError``, carries its own cap). Folding or narrowing each
    is tracked on issue #5587 alongside the remaining handler sweep -- claiming
    one owner before that is done would be a claim the tree does not support.

    The cap is enforced BEFORE decoding: a Content-Length precheck rejects an
    oversized declared body, and the stream is then read incrementally so a
    chunked body (which carries no Content-Length) cannot buffer past
    ``max_bytes + one chunk`` on the event-loop thread. That bound is the point
    of the helper for the strict-internal notification routes (issue #490).

    ``max_bytes=None`` reads the body whole with no pre-decode ceiling, for the
    endpoints that have no principled byte limit today (a knowledge bundle
    import has no defensible maximum size). It is deliberately explicit rather
    than the default: an endpoint opting out of the cap should say so at the
    call site, and giving one of those endpoints a real ceiling later is then a
    one-argument change here instead of a re-plumb.

    Which one a converting caller wants is a real choice, not a default to
    inherit: take the cap when the body is a fixed set of control fields (an
    identifier, a flag, a number), and ``None`` only when the body legitimately
    carries user content of unbounded size (file contents, an export, a fetched
    document). Note that switching a site TO the cap also moves it off
    ``request.json()`` onto the streaming read, so that handler's unit tests
    must feed ``content``/``content_length`` rather than mocking ``json``.

    *allow_absent* treats a request with no readable body as an empty object,
    for endpoints whose fields all have defaults. A body that is *present but
    malformed* is still a 400 -- "the client sent nothing" and "the client sent
    garbage" are different facts, and only the first one can be defaulted.

    Decoding matches ``request.json()`` on both paths -- ``decode(charset or
    utf-8)`` then ``loads`` -- so the two differ only in whether the read is
    bounded, and the declared ``charset=`` is honoured either way. The uncapped
    path calls ``request.json()`` itself rather than reimplementing it, which is
    what makes converting a ``try: await request.json()`` site a drop-in: no
    handler and no test harness sees a different read.

    The catch is narrowed to the three client-input failures -- ``ValueError``
    (which covers ``json.JSONDecodeError`` and ``UnicodeDecodeError``),
    ``LookupError`` (an unknown ``charset=`` codec), and ``RecursionError`` (a
    deeply nested document blowing the parser's stack). Transport failures (a
    disconnect mid-body, a read timeout) deliberately propagate: they are not a
    client JSON mistake and keep their 500 status class.
    """
    if allow_absent and not request.can_read_body:
        return {}, None
    if max_bytes is None:
        try:
            body = await request.json()
        except (LookupError, RecursionError, ValueError):
            return None, web.json_response(
                {"error": "invalid JSON", "code": "invalid_json"}, status=400
            )
    else:
        if request.content_length and request.content_length > max_bytes:
            return None, web.json_response(
                {"error": "payload too large", "code": "payload_too_large"}, status=413
            )
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.content.iter_chunked(8192):
            received += len(chunk)
            if received > max_bytes:
                return None, web.json_response(
                    {"error": "payload too large", "code": "payload_too_large"}, status=413
                )
            chunks.append(chunk)
        try:
            body = json.loads(b"".join(chunks).decode(request.charset or "utf-8"))
        except (LookupError, RecursionError, ValueError):
            return None, web.json_response(
                {"error": "invalid JSON", "code": "invalid_json"}, status=400
            )
    if not isinstance(body, dict):
        return None, web.json_response(
            {"error": "body must be a JSON object", "code": "body_not_object"}, status=400
        )
    return body, None


# Chunk size for draining an OUTBOUND HTTP response to EOF. Matches the
# bounded-read shape in ``mcp_providers.official._fetch_json``: large enough
# that a typical body arrives in a handful of iterations, small enough that
# the over-cap check fires long before an oversized body is buffered whole.
_RESPONSE_READ_CHUNK_BYTES = 64 * 1024


async def read_capped_response(resp: "aiohttp.ClientResponse", cap: int) -> bytes:
    """Read *resp*'s body to EOF, returning at most ``cap + 1`` bytes.

    A single ``StreamReader.read(n)`` resolves as soon as ANY bytes are
    buffered -- on a chunked response (no Content-Length) that is the first
    buffered chunk, so the caller silently works on a truncated body. This
    drains ``iter_chunked`` chunks until EOF, enforcing the cap against the
    ACCUMULATED total: reading stops as soon as the total exceeds *cap*, so a
    hostile oversized body is refused mid-stream rather than buffered whole.
    The return is clamped to ``cap + 1`` bytes so callers keep the established
    over-cap sentinel (``len(body) > cap`` means "exceeded the cap"), while a
    body of exactly *cap* bytes is still delivered complete.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(_RESPONSE_READ_CHUNK_BYTES):
        chunks.append(chunk)
        total += len(chunk)
        if total > cap:
            break
    return b"".join(chunks)[: cap + 1]


def _audit_admission(surface: str, resource: str, allowed: bool, error: str = "") -> None:
    """Record an external-access verdict in the security event log.

    BOTH outcomes are logged, not just denials. An admission is the security-
    relevant event here: "this deployment queried a public registry" and "this
    deployment provisioned cloud infrastructure" are exactly what an operator who
    restricted these surfaces needs to be able to prove afterwards, and a log that
    only carries denials cannot answer whether the permitted path was ever taken.

    Raises on failure — deliberately NOT best-effort, unlike most SEL call sites.
    An access grant that cannot be recorded is an unaccountable grant, so the
    caller converts a failed audit into a denial rather than proceeding unlogged.

    ``critical=True`` is what makes that possible. The default path QUEUES the
    event and swallows a write failure internally, so an exception handler around
    this call would never fire and the "fail closed" claim would be empty; the
    critical path writes synchronously and raises on a filesystem failure.
    """
    from kiro_crew.sel import sel as _sel  # circular import: sel imports config

    _sel().log_api_access(
        caller="system",
        operation=f"external_access:{surface}",
        outcome="allowed" if allowed else "denied",
        source="agent",
        resources=resource,
        error=error,
        critical=True,
    )


def _admits(surface: str, resource: str, probe: "Callable[[], bool]") -> bool:
    """Ask the composed policy one admission question, audited either way.

    Denies on a transient adapter failure rather than admitting. The only way to
    reach that fallback is for a COMPOSED policy to raise — a managed deployment
    whose intent was to restrict something — so admitting there would hand back
    the exact access the operator disabled. The public default cannot raise, so an
    ordinary install is unaffected, and ``PlatformCompositionError`` still
    propagates per the CPP fail-closed invariant.

    A FAILED AUDIT ALSO DENIES. If the verdict cannot be written to the security
    event log — an unwritable or corrupt SEL key — then proceeding would grant
    external access with no accountability record, which is the one thing this
    seam exists to make provable. Denying is the conservative direction: the
    operator loses a registry browser or a deploy button and gets a logged error,
    rather than silently gaining unaudited egress.

    SYNCHRONOUS BY DESIGN, and callers on the event loop must run it in a worker
    thread. SEL initialization does blocking filesystem work (trust-dir creation,
    key validation, and on Windows an owner-only DACL), so calling this inline
    from a coroutine would stall every request.
    """
    from kiro_crew.platform.context import safe_context_call

    failed: list[str] = []

    def _fallback() -> bool:
        failed.append("policy_error")
        return False

    allowed = safe_context_call(
        probe,
        fallback_factory=_fallback,
        log_message=f"external-access check failed for {surface} {resource!r}; denying",
    )
    try:
        _audit_admission(surface, resource, allowed, error="policy_error" if failed else "")
    except Exception:
        logger.error(
            "external-access verdict for %s %r could not be audited; denying",
            surface,
            resource,
            exc_info=True,
        )
        return False
    return allowed


def admits_registry(kind: str, name: str, api_base: str) -> bool:
    """Whether the composed platform admits an external discovery registry.

    The single call point for the registry half of the ``external_access`` seam, so
    both catalogs ask the question identically instead of each re-deriving the
    fail-closed idiom — the reason ``safe_context_call`` is centralized is that a
    hand-rolled ``except Exception`` at a call site silently swallows
    ``PlatformCompositionError``.
    """
    from kiro_crew.platform.context import current_context

    return _admits(
        f"registry:{kind}",
        api_base,
        lambda: current_context().external_access.admits_registry(kind, name, api_base),
    )


def admits_cloud_deployment(target: str = "aws") -> bool:
    """Whether the composed platform admits provisioning in a cloud account.

    Consulted by the deploy surface: a denied deployment reports itself disabled
    and refuses every mutating request.
    """
    from kiro_crew.platform.context import current_context

    return _admits(
        "cloud_deployment",
        target,
        lambda: current_context().external_access.admits_cloud_deployment(target),
    )


def _capability_manager() -> "CapabilityManager":
    """The edition's external capability manager (CPP seam).

    Lives in the shared layer (not a leaf handler) so every consumer —
    ``agents.py`` handlers, ``mcp.py`` uninstall, and the skill/prompt listers
    here — imports it DOWNWARD with no circular dependency. Operations-based: the
    edition owns its CLI grammar, output parsing, and error translation. Fails
    closed to an unavailable ``DefaultCapabilityManager`` so ``/api/capability/*``
    degrade to 503 rather than crashing.

    The returned manager is ALREADY LIVENESS-bounded: the context wraps every
    ``CapabilityManager`` in ``BoundedCapabilityManager`` at composition time
    (``PlatformContext.__post_init__``), so the ``asyncio.wait_for`` mutation
    bound is inherited by every reader of ``current_context().capability_manager``
    — not just callers who route through this accessor. The fallback
    ``DefaultCapabilityManager`` is bound here too so a context-lookup failure
    degrades to a wrapped (still unavailable) manager, keeping the return type
    uniform.
    """
    from kiro_crew.platform.capability_bound import bind_capability_manager
    from kiro_crew.platform.context import current_context, safe_context_call
    from kiro_crew.platform.defaults import DefaultCapabilityManager

    return safe_context_call(
        lambda: current_context().capability_manager,
        fallback_factory=lambda: bind_capability_manager(DefaultCapabilityManager()),
        log_message="capability_manager lookup failed; treating as unavailable",
    )


def _get_memory(state: DashboardState):
    """Get MemoryStore from context_builder, or create standalone."""
    if state.context_builder:
        return state.context_builder.memory
    # Fallback: create standalone MemoryStore
    if not hasattr(state, "_standalone_memory"):
        from kiro_crew.memory import MemoryStore

        mem = MemoryStore()
        mem.init()
        state._standalone_memory = mem  # type: ignore[attr-defined]
    return state._standalone_memory  # type: ignore[attr-defined]


def _get_active_workspace(state: DashboardState) -> str:
    """Return the workspace of the most recently active chat slot, or 'default'."""
    slots = getattr(state, "_slots", {})
    if slots:
        # Pick the slot with the most messages (most active)
        best = max(slots.values(), key=lambda s: s.total_messages, default=None)
        if best and best.workspace and best.workspace != "default":
            return best.workspace
    return "default"


def _get_lessons(state: DashboardState, workspace: str | None = None):
    """Get LessonStore for a workspace. Falls back to global."""
    ws = workspace or _get_active_workspace(state)
    if ws != "default" and state.context_builder:
        return state.context_builder.get_lessons_for(ws)
    return state.lessons


def _get_skills(state: DashboardState):
    """Get SkillsLoader from context_builder, or create standalone."""
    if state.context_builder:
        return state.context_builder.skills
    if not hasattr(state, "_standalone_skills"):
        from kiro_crew.skills import SkillsLoader

        skills = SkillsLoader(install_builtins=False)
        state._standalone_skills = skills  # type: ignore[attr-defined]
    return state._standalone_skills  # type: ignore[attr-defined]


def _edition_skill_roots() -> list[Path]:
    """Return edition-contributed SKILL.md source roots (CPP seam).

    Reads ``McpToolingProvider.extra_skills()`` fail-closed through
    ``safe_context_call`` (public Default: ``[]``), so on a vanilla OSS install
    there are no roots to discover and the edition skill helpers below
    return "nothing found" rather than globbing a hardcoded home-dir tree.
    Deferred import (sel.py pattern) so this module never imports the platform
    package at module load.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    roots: list[Path] = safe_context_call(
        lambda: list(current_context().mcp_tooling.extra_skills()),
        fallback_factory=list,
        log_message="extra_skills lookup failed; using none",
    )
    return [Path(r) for r in roots]


def _canonical_skill_roots() -> list[Path]:
    """Skill roots the CORE owns and keys under its own prefixes.

    ``extra_skills()`` legitimately advertises some of these — the data home and
    ``~/.kiro/skills`` — so the loader indexes them. They must not ALSO be
    searched or keyed as ``package/``, or one file gets two identities and a
    ``package/<name>`` request can be answered with the user's own editable skill.

    State-free on purpose, so every consumer gets the exclusion by default;
    ``<project>/.kiro/skills`` needs a chat slot and is added by the caller that
    has one.
    """
    out: list[Path] = [Path.home() / ".kiro" / "skills", skills_dir()]
    try:
        # ``resolve()``, not just ``expanduser()``: a RELATIVE extra_paths entry
        # would otherwise key the catalog by a relative root, and the persisted
        # ``skill://`` URI would then resolve against whatever cwd the next
        # kiro-cli session starts in — silently loading a different skill, or
        # none. A skill root must be a stable absolute location.
        out.extend(Path(p).expanduser().resolve() for p in KiroCrewConfig.load().skills.extra_paths)
    except Exception:
        logger.debug("failed to load extra skill paths from config", exc_info=True)
    return out


def _resolved_set(paths: Iterable[Path]) -> set[Path]:
    """Resolved forms of *paths*, skipping any that cannot be resolved.

    ``Path.resolve()`` raises ``RuntimeError`` (not ``OSError``) on a symlink
    loop, so both are caught: an unresolvable root simply does not participate in
    identity comparisons.
    """
    out: set[Path] = set()
    for p in paths:
        try:
            out.add(p.resolve())
        except (OSError, RuntimeError):
            continue
    return out


def _edition_package_roots(canonical: set[Path] | None = None) -> list[Path]:
    """Edition roots that are genuinely ``package/`` territory.

    The single source of truth for "which advertised roots are package roots",
    shared by key enumeration and path resolution — if those two disagree, the
    catalog offers a key the resolver refuses, or worse resolves to a file the
    catalog never listed.

    An unresolvable root is KEPT: it cannot be compared for identity, and
    dropping it would silently remove a root that is otherwise served.
    """
    owned = set(canonical) if canonical is not None else _resolved_set(_canonical_skill_roots())
    out: list[Path] = []
    for root in _edition_skill_roots():
        try:
            resolved = root.resolve()
        except (OSError, RuntimeError):
            out.append(root)
            continue
        if resolved in owned:
            continue
        owned.add(resolved)
        out.append(root)
    return out


# Key prefix for a skill contributed by an installed edition/package bundle.
# Read by key enumeration, path resolution, and the detail endpoint, so none of
# the three can drift on the one string that decides which grammar a key is read
# under. The guard against a fourth site respelling it is a test, not this comment.
PACKAGE_KEY_PREFIX = "package/"

# Separates an optional root qualifier from the relative path in a ``package/``
# key: ``package/<qualifier>:<rel>``. A qualifier is a DERIVED IDENTITY — a digest
# of the holding root's canonical path, e.g. ``05c564ec5e9e4b7a8c1d2e3f4a5b6c7d`` — and NOT itself a
# path segment of any root, so nothing may test it as one. Deriving it from the root
# ALONE is what makes one relative path bundled by several packages addressable
# while keeping the key stable: it cannot be re-spelled by installing or removing an
# unrelated bundle, and NO replacement re-derives its qualifier — not at a different path,
# nor at the SAME one (the digest folds dev, inode and ctime). Resolution RE-DERIVES with the
# same function enumeration used, so the two sides cannot disagree about a key.
#
# ``:`` and not ``-``: the route grammar reserves a lone ``-`` segment as the
# separator before the ``tree``/``file`` verbs (``/api/skills/{name}/-/tree``),
# so a ``-`` segment inside a key would make ``package/Pkg/-/tree`` ambiguous
# between a detail GET and a tree GET. ``:`` is a legal path character (RFC 3986
# pchar) needing no escaping.
#
# It is NOT absent from the keys the core emits today: ``:`` is legal in a POSIX
# directory name, so a skill directory literally named ``foo:bar`` already
# enumerated and resolved before this grammar existed. The separator is RESERVED
# anyway, and that is the whole of the grammar: a key carrying it has exactly ONE
# reading, the qualified one. A literal ``foo:bar`` skill is therefore omitted
# from the catalog and 404s on open — :func:`_resolve_skill_root` has no verbatim
# fallback. Reserving it is what keeps a key's MEANING independent of which roots
# happen to be installed; under a dual reading, uninstalling a root re-pointed an
# existing key at a different package's skill. A key without the separator still
# takes exactly the pre-existing code path.
_SKILL_KEY_QUALIFIER_SEP = ":"

# The characters read as PATTERN syntax rather than as a name — by ``Path.glob`` when a
# key's remainder is resolved, and by ``fnmatch`` when a ``skill://`` URI is inverted. One
# spelling for both, because it is one rule: a pattern names a SET, which no single
# catalog key can address. ``]`` is absent deliberately: it is only special INSIDE a
# ``[`` class, so refusing it alone would reject an ordinary directory name for no gain.
_GLOB_CHARS = ("*", "?", "[")


def _split_package_skill_key(pkg_rel: str) -> tuple[str | None, str]:
    """Split a ``package/`` key remainder into ``(qualifier, relative_path)``.

    ``None`` for the qualifier means the key is unqualified and resolves exactly
    as it always has. A qualifier is only recognised when both halves are
    non-empty, so a stray leading or trailing separator degrades to "no
    qualifier" rather than to an empty glob pattern.
    """
    if _SKILL_KEY_QUALIFIER_SEP not in pkg_rel:
        return None, pkg_rel
    qualifier, _, rel = pkg_rel.partition(_SKILL_KEY_QUALIFIER_SEP)
    if not qualifier or not rel:
        return None, pkg_rel
    return qualifier, rel


def _names_a_relative_path(name: str) -> bool:
    """Whether *name* is a relative pattern under a root on the current host.

    ``Path.glob`` raises ``NotImplementedError: Non-relative patterns are
    unsupported`` on a non-relative pattern, and NON-RELATIVE means carrying a drive
    OR a root — not merely being absolute. A qualified key is caller-supplied, so an
    unvalidated remainder would surface that as a 500 rather than a 404.

    Validation uses HOST semantics, because enumeration and resolution run on the
    SAME host and the binding contract is between those two: every catalogued key
    must resolve. Asking ``ntpath`` unconditionally broke that. ``ntpath.splitdrive``
    treats ANY single character before ``:`` as a drive, so ``a:b`` was refused
    everywhere — while on POSIX ``a:b`` is a legal directory name that the enumerator
    happily catalogues, leaving the pair catalogued-but-unresolvable. That is not the
    fail-closed direction; it is the phantom row. On Windows such a directory cannot
    exist, so refusing it there costs nothing and keeps the glob from raising.

    Host semantics is also interpreter-stable, which was the reason the flavour's
    ``.drive`` was avoided: pathlib only adopted the permissive drive rule in 3.12, so
    ``PureWindowsPath("~:x").drive`` is empty on 3.10/3.11 and ``"~:"`` on 3.12.
    ``os.path.splitdrive`` is ``posixpath``'s (always no drive) or ``ntpath``'s (the
    permissive rule, measured identical on 3.10-3.12) — never the version-dependent
    one.

    A component carrying ANY glob metacharacter — ``*``, ``?`` or ``[`` — is refused,
    for the two different reasons they are wrong. ``**`` EMBEDDED in a component
    (``***``, ``a**b``) is an INVALID pattern and ``Path.glob`` raises ``ValueError:
    Invalid pattern: '**' can only be an entire path component`` — the same
    500-instead-of-404 this predicate exists to stop, just a different exception type
    than the non-relative case above. Every other spelling is VALID but is a WILDCARD,
    so the key would not name a directory: ``**`` matches at any depth, ignoring
    ``_SKILL_NEST_DEPTH``, while ``*``, ``?`` and ``[`` match SIBLINGS — so one key
    resolves to whichever skill happens to match, or to several, which is the
    one-key-two-skills divergence the reserved separator exists to prevent.

    Refusing them cannot strand a row, because :func:`_merge_package_walks` holds every
    walked rel to this SAME predicate and omits what it refuses. An earlier revision
    admitted a single ``*`` or ``?`` precisely to avoid that phantom row, when only
    separator-carrying rels were omitted; the enumerator mirror removed the tradeoff, so
    the narrow admission is no longer needed and the ambiguity is no longer worth
    carrying. The declared cost is the same class as the separator's: a skill directory
    named with a glob metacharacter is invisible in the catalog and 404s on open — and
    the same now holds for a name CONTAINING ``..``, which the resolver's own first gate
    refuses as a substring (see the inline note below).
    """
    if not name:
        return False
    if os.path.splitdrive(name)[0]:
        return False
    if ".." in name:
        # SUBSTRING, deliberately, and not ``".." in host.parts``: the resolver's own
        # first gate is ``".." in name``, so a component merely CONTAINING ``..``
        # (``foo..bar``, ``pkg..v2``) passes a parts test — its only part is
        # ``"foo..bar"``, which is not ``".."`` — while the resolver refuses the key
        # whole. That pair is catalogued-but-unresolvable: the phantom row this grammar
        # exists to remove, reintroduced by the two sides testing traversal differently.
        # Matching the resolver is the fail-CLOSED direction; loosening the resolver to
        # a parts test instead would relax a pre-existing traversal guard shared with
        # the ``kiro-user/`` and ``kiro-workspace/`` territories, which is not this
        # change's to weaken. The declared cost is the same class as the separator's: a
        # skill directory whose name contains ``..`` is invisible in the catalog.
        return False
    host = PurePath(name)
    if host.root or host.drive:
        return False
    if any(c in part for part in host.parts for c in _GLOB_CHARS):
        return False
    # ``Path.glob`` recurses per pattern component, so a deep enough remainder raises
    # RecursionError inside the resolver; the enumerator cannot mint one this deep anyway.
    if len(host.parts) > _SKILL_NEST_DEPTH:
        return False
    return True


def _resolve_package_skill_path(
    name: str, canonical: set[Path] | None = None, qualifier: str | None = None
) -> Path | None:
    """Find SKILL.md for an edition-contributed skill by its key remainder.

    Searched over the ``package/`` territory of the edition skill roots
    (:func:`_edition_package_roots`) — NOT every advertised root. A root the core
    already keys as ``kiro-user/`` or unprefixed is excluded, so a
    ``package/<name>`` request can never be answered with the user's own editable
    skill; *canonical* lets a caller that knows the active project add
    ``<project>/.kiro/skills`` to that exclusion.

    Two layouts are supported, in precedence order:

    1. ``<root>/<name>/SKILL.md`` — *name* is the path relative to the root, which
       is how a row keyed ``package/<rel>`` addresses its file.
    2. ``<root>/<pkg>/<name>/SKILL.md`` — *name* is a leaf under some package
       directory, for an edition that keys rows by leaf.

    An exact relative-path hit wins over a nested leaf hit. Within a tier, two
    DISTINCT files matching is a genuine ambiguity — the same relative path
    bundled by two packages — so it returns ``None`` and logs instead of picking
    one. Serving an arbitrary one of the two looks completely successful and shows
    the wrong skill's content, which is the failure mode worth being loud about.

    *qualifier* is how a caller resolves that ambiguity, and it is the CANONICAL
    catalog-derived value for the wanted copy's root — the identity digest
    :func:`_root_identity_token` derives — not any segment that root happens to carry. So for
    roots ``packages/PkgA/eventId-1/skills`` and ``packages/PkgB/eventId-2/skills`` the
    qualifiers are the two roots' digests, and neither ``package/PkgA:<rel>`` nor
    ``package/eventId-1:<rel>`` resolves even though both name a unique segment of one
    root: the catalogue never offers those spellings, and answering one would be
    answering a key ``/tree`` does not list.

    That narrowing is deliberate and is the invariant this grammar exists to restore
    (see :func:`_merge_package_walks`): every key enumeration EMITS resolves, so a
    qualified key resolves to the copy the catalogue would list it for, or to nothing.
    The converse does not hold — resolution also accepts a leaf-name key through its
    nested tier — so this is one direction, not an inverse.
    Accepting any carried segment instead is what let a stale key bind to a DIFFERENT
    bundle — uninstall the root a qualifier was derived from, install another that also
    carries that segment, and the same key silently served the new bundle's content.

    Selection still applies to ROOTS and never to assembled candidate paths, because a
    candidate's segments cannot tell a root segment from a relative-path segment: testing
    candidates would let a qualifier match inside another root's rel and serve that
    bundle's file. A root that is not among the copies of this rel yields nothing, so
    ``package/PkgB:shared-skill`` does not answer with ``PkgA``'s copy when only ``PkgA``
    bundles that path.

    The core still needs no root-to-package mapping: the qualifier is derived from the
    root's own canonical path, and that knowledge belongs to whichever edition installs
    the roots. A qualifier that is not a digest any installed root yields narrows to no
    roots rather than to all of them, so an unrecognised one answers nothing.

    A qualified key is answered with the candidate whose identity was CHECKED, and never
    by re-globbing the root the check selected. Re-globbing reopened the rebind twice
    over. Once through TIME: the digest is taken over a root's device, inode and creation
    time, so a second read of that path can land on a REPLACEMENT installed since, and
    the key would then serve the replacement's file under the replaced bundle's identity.
    Once through TIER: the qualifier is derived over the exact-relative-path glob alone,
    so the shared loop's nested-leaf tier can only ever offer a file that was NOT in the
    checked set — reachable exactly when the re-glob finds nothing, which is what a
    replacement produces. Re-CHECKING the identity after the glob closes neither: it
    leaves the same window open between the recheck and the read, and says nothing about
    a file the nested tier admitted from outside the derivation set. Fewer or more than
    one match refuses rather than guessing, so a digest collision cannot pick a winner.
    """
    exact: list[tuple[Path, Path]] = []
    nested: list[tuple[Path, Path]] = []
    if not _names_a_relative_path(name):
        # A name that cannot be a relative path under a root names nothing, and
        # handing it to ``glob`` would raise rather than 404 (see the predicate).
        return None
    if _SKILL_KEY_QUALIFIER_SEP in name:
        # The remainder still carries the RESERVED separator, so this key is not a
        # well-formed ``<qualifier>:<rel>`` pair: either a half-empty qualifier
        # (``package/:foo``, which :func:`_split_package_skill_key` degrades to an
        # unqualified key whose rel keeps the colon) or a rel with a second separator
        # (``package/A:B:C`` splits once, leaving rel ``B:C``). Enumeration OMITS any
        # rel carrying the separator, so admitting one here would resolve a
        # colon-named skill the catalogue never listed — the same one-key-two-readings
        # divergence the reservation exists to remove, reached through the rel half.
        return None
    roots = _edition_package_roots(canonical)
    if qualifier is not None:
        # RE-DERIVE the qualifier per root and require equality, rather than testing
        # membership. Membership (``qualifier in root.parts``) accepts ANY root that
        # happens to carry a segment of that name, which is not the same thing as the
        # root the key was minted for: uninstall the root a qualifier was derived from,
        # install a different one that also carries that segment, and the stale key
        # binds to the new bundle and serves its content. Re-deriving cannot do that,
        # because :func:`_root_identity_token` is a digest of the root's own canonical path
        # and no other root produces it — a stale key fails to resolve instead.
        #
        # Deriving with the SAME function enumeration uses makes resolution its exact
        # inverse: a qualified key resolves to the copy the catalogue would list it
        # for, or to nothing.
        # ONE collision-set computation, shared with the enumerator's fold rather than
        # spelled again here -- see :func:`_package_collision`. Both of its ``None``
        # cases refuse alike: fewer than two distinct copies means enumeration minted
        # the UNQUALIFIED key and never offered a qualified spelling for this rel, and
        # an all-or-nothing failure means it omitted the rel whole. Answering either
        # anyway is what let a stale qualifier reach a root it was never minted for.
        copies, qualifiers = _package_collision(
            [(root, hit) for root in roots for hit in root.glob(f"{name}/SKILL.md")]
        )
        if qualifiers is None:
            return None
        # Answer with the candidate whose identity was CHECKED, rather than re-globbing the
        # matched root: see the two rebinds this closes, above.
        matched = [
            (root, skill_md)
            for (root, skill_md), q in zip(copies, qualifiers, strict=True)
            if q == qualifier
        ]
        if len(matched) != 1:
            return None
        return matched[0][1]
    for root in roots:
        exact.extend((root, hit) for hit in root.glob(f"{name}/SKILL.md"))
        nested.extend((root, hit) for hit in root.glob(f"*/{name}/SKILL.md"))
    for tier, label in ((exact, "relative path"), (nested, "leaf name")):
        # One entry per distinct resolved FILE, shared with the enumerator's fold rather
        # than spelled twice: two implementations of "collapse aliases, preserve order,
        # skip the unreadable" diverge on the next exception-handling fix.
        candidates = _dedupe_entries(tier)
        if len(candidates) == 1:
            return candidates[0][1]
        if candidates:
            logger.warning(
                "edition skill %r matches %d distinct files by %s (%s); refusing "
                "to guess — the package/<path> key cannot address more than one",
                name,
                len(candidates),
                label,
                ", ".join(sorted(str(f) for _root, f in candidates)),
            )
            return None
    return None


def active_project_state(state: DashboardState, session_key: str = "") -> tuple[Path | None, str]:
    """Resolve the workspace project AND why it is absent when it is.

    Returns ``(project, state)`` where *state* is one of:

    * ``"set"`` — *project* is a real directory and workspace-scoped resources
      resolve against it;
    * ``"none"`` — no open chat slot names a project at all;
    * ``"ambiguous"`` — two or more slots name DIFFERENT projects and
      *session_key* did not single one out, so there is no defensible answer.

    :func:`active_project_dir` collapses the last two to ``None``, which is the
    right call for a resolver but not for a UI: "you have no project" and "your
    open chats disagree" need different words and different remedies, and a
    caller that cannot tell them apart has to guess. Callers that only need the
    path should keep using :func:`active_project_dir`.
    """
    project = _resolve_active_project(state, session_key)
    if project is not None:
        return project, "set"
    slots = getattr(state, "_slots", {}) or {}
    distinct = {str(p) for p in (_slot_project(s) for s in slots.values()) if p is not None}
    return None, "ambiguous" if len(distinct) > 1 else "none"


def _slot_project(slot: Any) -> Path | None:
    """The project a chat slot is bound to, if any.

    ``project_dir`` is accepted alongside ``project`` for slot-like objects that
    expose that name instead.
    """
    pd = getattr(slot, "project", None) or getattr(slot, "project_dir", None)
    if isinstance(pd, Path):
        return pd
    if isinstance(pd, str) and pd:
        return Path(pd)
    return None


def active_project_dir(state: DashboardState, session_key: str = "") -> Path | None:
    """Return the project directory that workspace-scoped resources resolve against.

    Workspace-scoped resources (``<project>/.kiro/skills``,
    ``<project>/.kiro/steering``) live under the directory the agent actually
    runs in, which the dashboard stores per chat slot as ``_ChatSlot.project``
    (set by ``PUT /api/chat/slots/{slot}/project`` and the ``set_project`` MCP
    tool).  ``project_dir`` is accepted as a fallback for slot-like objects that
    expose that name instead.

    Resolution is deterministic, in this order:

    1. the slot named by *session_key*, when it has a project;
    2. otherwise the single project shared by every slot that has one;
    3. otherwise ``None``.

    Step 3 matters for mutations: with two chats open on different projects
    there is no defensible "active" project for a settings page, and silently
    picking the first-inserted slot would create, overwrite or delete files in
    the wrong project.  Failing closed makes the caller surface the ambiguity
    instead — :func:`active_project_state` reports which of the two "no answer"
    cases produced the ``None``.

    Step 2 is what makes this the WRONG helper for a per-chat resource. It
    answers for a chat that has no project of its own, so a caller that must
    agree with what one chat will actually load — the skills catalog, and the
    consent grant that admits those skills — would resolve a directory that chat
    is not bound to. Those callers use :func:`requesting_slot_project` instead.
    Reach for this one only when the resource really is global.
    """
    return _resolve_active_project(state, session_key)


def requesting_slot_project(state: DashboardState, session_key: str = "") -> Path | None:
    """The project bound to THIS chat slot, with no cross-slot fallback.

    :func:`active_project_dir` answers "which project should a global surface
    act on", and falls back to the single project shared by the open slots.
    This answers the narrower question the skills loader asks: "which project
    is THIS chat bound to". ``SkillsLoader`` resolves project skills from
    ``_ChatSlot.project`` verbatim, so a caller that must agree with what the
    loader will actually load -- the catalog, and the consent grant that admits
    it -- has to ask the same question, not the broader one.

    Returns ``None`` when this slot has no project, which is a meaningful
    answer: there is no directory for this chat to list, trust, or load from.
    """
    slots = getattr(state, "_slots", {}) or {}
    if not session_key:
        return None
    slot_name = session_key.split(":", 1)[-1] if ":" in session_key else session_key
    slot = slots.get(slot_name)
    if slot is None:
        return None
    return _slot_project(slot)


def _resolve_active_project(state: DashboardState, session_key: str) -> Path | None:
    """The three-step resolution shared by the two public accessors."""
    slots = getattr(state, "_slots", {}) or {}

    if session_key:
        slot_name = session_key.split(":", 1)[-1] if ":" in session_key else session_key
        slot = slots.get(slot_name)
        if slot is not None:
            scoped = _slot_project(slot)
            if scoped is not None:
                return scoped
    distinct: dict[str, Path] = {}
    for slot in slots.values():
        proj = _slot_project(slot)
        if proj is not None:
            distinct[str(proj)] = proj
    if len(distinct) == 1:
        return next(iter(distinct.values()))
    return None


# ── Kiro-cli native skills (~/.kiro/skills/, <project>/.kiro/skills/) ──


# Maximum SKILL.md content we'll read just to extract frontmatter description.
_KIRO_SKILL_FRONTMATTER_BYTES = 4096


def _kiro_skill_roots(project_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Return ``(label, path)`` pairs for the open-standard skill locations.

    label is one of: ``kiro-user``, ``kiro-workspace``.  Used as the
    ``source`` field on listed skills so the UI can show provenance.
    """
    out: list[tuple[str, Path]] = []
    user_dir = Path.home() / ".kiro" / "skills"
    if user_dir.is_dir() and not is_sensitive_path(str(user_dir)):
        out.append(("kiro-user", user_dir))
    if project_dir:
        ws_dir = project_dir / ".kiro" / "skills"
        if ws_dir.is_dir() and not is_sensitive_path(str(ws_dir)):
            out.append(("kiro-workspace", ws_dir))
    return out


def _parse_skill_description(skill_md: Path) -> tuple[str, bool]:
    """Cheap frontmatter parse — return (description, always)."""
    # Gate on the resolved target before reading: a SKILL.md inside an
    # otherwise-trusted skills root may itself be a symlink to a sensitive
    # credential file (e.g. ~/.kiro/skills/evil/SKILL.md → ~/.aws/credentials).
    # Checking the root dir is not enough — individual files must be checked.
    try:
        resolved_md = skill_md.resolve(strict=True)
    except OSError:
        return "", False
    if is_sensitive_path(str(resolved_md)):
        return "", False
    try:
        with resolved_md.open("rb") as f:
            head = f.read(_KIRO_SKILL_FRONTMATTER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return "", False
    if not head.startswith("---"):
        return "", False
    end = head.find("\n---", 3)
    if end < 0:
        return "", False
    desc = ""
    always = False
    for line in head[3:end].splitlines():
        line = line.strip()
        if line.startswith("description:"):
            desc = line.split(":", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("always:"):
            val = line.split(":", 1)[1].strip().lower()
            always = val == "true"
    return desc, always


def list_kiro_skills(project_dir: Path | None = None) -> list[dict[str, Any]]:
    """List skills from kiro-cli's open-standard locations.

    Each entry has the same shape as a SkillsLoader entry plus a
    ``source`` of ``kiro-user`` or ``kiro-workspace``.  Read-only —
    edits are not routed back here (kiro-cli owns these directories).
    """
    out: list[dict[str, Any]] = []
    for source, root in _kiro_skill_roots(project_dir):
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            desc, always = _parse_skill_description(skill_md)
            out.append(
                {
                    "key": f"{source}/{entry.name}",
                    "name": entry.name,
                    "description": desc,
                    "path": str(skill_md),
                    "dir": str(entry),
                    "always": always,
                    "source": source,
                }
            )
    return out


# ── loaded_by_agents resolution ──


def _agent_dirs() -> list[Path]:
    """Return existing agent JSON directories (global + workspace)."""
    out: list[Path] = []
    user = kiro_agents_dir()
    if user.is_dir():
        out.append(user)
    return out


def _expand_resource_uri(uri: str, agent_path: Path) -> str | None:
    """Strip ``skill://`` and resolve ``~`` / workspace-relative paths.

    Thin alias for :func:`kiro_crew.agent_discovery.expand_skill_uri` — the
    single implementation, shared with the session-context skill filter so the
    dashboard's ``loaded_by_agents`` annotation and the runtime injection agree
    on what a given URI matches.

    Returns a glob pattern usable with fnmatch, or None if not a skill URI.
    """
    return expand_skill_uri(uri, agent_path)


def _agent_loads_skill(agent_json: dict[str, Any], agent_path: Path, skill_md: Path) -> bool:
    """Return True if *agent_json*'s ``resources`` would load *skill_md*.

    One-off helper (single skill vs single agent). For annotating *many*
    skills against *many* agents, prefer :func:`_expand_agent_globs` +
    :func:`_agents_loading_skill` so each agent's globs are expanded once
    instead of once per skill.
    """
    resources = agent_json.get("resources") or []
    if not isinstance(resources, list):
        return False
    target = str(skill_md)
    for res in resources:
        if not isinstance(res, str):
            continue
        glob = _expand_resource_uri(res, agent_path)
        if glob and fnmatch.fnmatch(target, glob):
            return True
    return False


def _expand_agent_globs(
    parsed_agents: list[tuple[str, dict[str, Any], Path]],
) -> list[tuple[str, list[str]]]:
    """Pre-expand every agent's ``skill://`` resources into fnmatch globs ONCE.

    Returns ``(agent_name, [glob, ...])`` pairs. The glob for a resource
    depends only on ``(uri, agent_path)`` — NOT on the skill being matched —
    so expanding here (O(agents × resources)) and reusing the result across
    all skills avoids re-running :func:`_expand_resource_uri` once per
    (skill, agent, resource), which on a large catalog is the dominant cost.
    Agents with no skill:// resources are dropped (they can match nothing).
    """
    expanded: list[tuple[str, list[str]]] = []
    for name, data, agent_path in parsed_agents:
        resources = data.get("resources") or []
        if not isinstance(resources, list):
            continue
        globs = [
            g
            for res in resources
            if isinstance(res, str)
            for g in (_expand_resource_uri(res, agent_path),)
            if g
        ]
        if globs:
            expanded.append((name, globs))
    return expanded


def _agents_loading_skill(
    skill_md: Path, expanded_agents: list[tuple[str, list[str]]]
) -> list[str]:
    """Return names of agents whose pre-expanded globs match *skill_md*."""
    target = str(skill_md)
    return [
        name for name, globs in expanded_agents if any(fnmatch.fnmatch(target, g) for g in globs)
    ]


def _load_parsed_agents() -> list[tuple[str, dict[str, Any], Path]]:
    """Read every agent JSON ONCE, returning ``(name, data, agent_path)``.

    Hoisted out of the per-skill loop so ``api_skills`` parses each agent
    file exactly once per request instead of once per skill — turning an
    O(skills × agents) read/parse blowup into O(agents). Best-effort: macOS
    AppleDouble sidecars ("._foo.json"), unreadable/invalid agents, and
    sensitive-path symlinks are skipped (a symlink under ~/.kiro/agents/
    could otherwise point at a credential file renamed ``*.json``).
    """
    parsed: list[tuple[str, dict[str, Any], Path]] = []
    for agents_dir in _agent_dirs():
        try:
            agent_files = sorted(agents_dir.glob("*.json"))
        except OSError:
            # An unreadable agents dir (e.g. PermissionError) must degrade to
            # "no agents" rather than propagate and 500 the whole response.
            continue
        for agent_path in agent_files:
            if agent_path.name.startswith("._"):
                continue
            try:
                resolved = agent_path.resolve(strict=True)
            except OSError:
                continue
            if is_sensitive_path(str(resolved)):
                continue
            try:
                data = json.loads(resolved.read_text(encoding="utf-8"))
            # ValueError covers both json.JSONDecodeError and
            # UnicodeDecodeError (a non-UTF-8 file must not 500 the API).
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            name = data.get("name") or agent_path.stem
            parsed.append((str(name), data, agent_path))
    return parsed


def _resolve_loaded_by_agents(
    skill_md: Path,
    parsed_agents: list[tuple[str, dict[str, Any], Path]] | None = None,
) -> list[str]:
    """Return list of agent names whose ``resources`` glob matches *skill_md*.

    Pass *parsed_agents* (from :func:`_load_parsed_agents`) to reuse a single
    agent parse across many skills; omit it for a one-off lookup (parses
    agents inline). Empty list means no agent loads this skill via
    ``skill://`` URIs (it may still be loaded via KiroCrew text-injection or
    an external MCP server).
    """
    agents = parsed_agents if parsed_agents is not None else _load_parsed_agents()
    out: list[str] = []
    for name, data, agent_path in agents:
        if _agent_loads_skill(data, agent_path, skill_md):
            out.append(name)
    return out


def annotate_skills_with_agents(skills: list[dict[str, Any]]) -> None:
    """Annotate each skill dict in-place with ``loaded_by_agents``.

    Parses the agent JSONs ONCE and pre-expands each agent's ``skill://``
    globs ONCE, then matches every skill against that in-memory set —
    O(agents × resources) expansion + O(skills × globs) matching, instead of
    re-expanding every agent glob per skill. Synchronous and filesystem-heavy
    (the parse walks ~/.kiro/agents) — callers on the asyncio event loop MUST
    run this off the loop. Per-skill failures isolate to an empty list (the
    documented default) rather than blanking the whole response.
    """
    expanded = _expand_agent_globs(_load_parsed_agents())
    for s in skills:
        path = s.get("path") or ""
        if not path:
            s["loaded_by_agents"] = []
            continue
        try:
            s["loaded_by_agents"] = _agents_loading_skill(Path(path), expanded)
        except Exception:
            s["loaded_by_agents"] = []


def collect_skills_blocking(
    skills_loader: Any,
    package_skills: list[dict[str, Any]],
    project_dir: Path | None,
) -> list[dict[str, Any]]:
    """Gather + annotate the full skill catalog. Runs ALL blocking FS work.

    This is the synchronous core behind ``GET /api/skills``. It performs
    every filesystem-heavy step in one call so the caller can offload the
    whole thing to a thread via ``run_in_executor``. ``list_skills()`` (os.walk +
    per-file frontmatter reads), ``list_kiro_skills()`` (per-skill resolve +
    read), and the confined project catalog are filesystem-heavy enough to
    stall the event loop past the loop-stall watchdog on large catalogs, so
    they run in the thread too rather than inline.

    Steps, in the same order the handler used inline:

    1. ``skills_loader.list_skills()`` — kirocrew skills (default source).
    2. ``package_skills`` — edition/package skills already fetched (structured
       rows) from ``CapabilityManager.list_skills()``; the manager owns their
       parsing, so nothing is parsed here.
    3. Global open-standard kiro-cli skills plus project rows from the loader's
       confined no-follow catalog.
    4. ``annotate_skills_with_agents(...)`` — ``loaded_by_agents`` per skill.

    The capability-manager fetch is intentionally NOT done here (it is async);
    the caller awaits it and hands us the structured rows.
    """
    result: list[dict[str, Any]] = skills_loader.list_skills()
    for s in result:
        s.setdefault("source", "kirocrew")
    _warn_skills_outside_roots(package_skills)
    result.extend(package_skills)
    # The legacy scanner is valid for the operator-owned global Kiro directory,
    # but it resolves and reads project link targets before containment can be
    # checked. Never pass the project to it: pre-consent project rows must come
    # from the loader's confined no-follow enumeration below.
    workspace_rows = list_kiro_skills()
    if project_dir is not None:
        # A workspace row is LISTABLE without consent but only USABLE with it:
        # $token expansion and context injection both resolve through
        # SkillsLoader, which gates the project root on the operator's grant.
        # Marking the row lets the picker offer that consent instead of handing
        # back a token that silently expands to nothing.
        trusted = _is_project_trusted(project_dir)

        # The loader's containment-only catalog IS the definition of what
        # consent could make loadable. It intentionally bypasses trust
        # enforcement so genuine untrusted rows remain visible, while its
        # confined no-follow read keeps linked targets untouched.
        try:
            project_rows = skills_loader.catalog_project_skills(project_dir)
        except Exception:  # noqa: BLE001 — a listing must not die on enumeration
            logger.warning("skills catalog: enumeration failed; listing no workspace rows")
            project_rows = []
        for row in project_rows:
            row["key"] = f"kiro-workspace/{row.get('key', '')}"
            row["source"] = "kiro-workspace"
            row["trusted"] = trusted
        workspace_rows.extend(project_rows)
    result.extend(workspace_rows)
    annotate_skills_with_agents(result)
    return result


def _warn_skills_outside_roots(package_skills: list[dict[str, Any]]) -> None:
    """Log loudly for any ``CapabilityManager.list_skills()`` row whose path
    falls outside every ``McpToolingProvider.extra_skills()`` root.

    Enforces (at runtime, not just in the interface docstring) the containment
    invariant the two Protocols share: the skill browser
    (``/api/skills/package/<name>/tree`` + detail) resolves a skill's on-disk
    path by searching those roots, so a listed row outside them lists in
    ``/api/skills`` but 404s on tree/detail. An edition that satisfies both
    seams independently can violate this; a loud warning turns an otherwise
    silent, hard-to-diagnose 404 into an actionable log line. No-op in OSS
    (``list_skills()`` returns ``[]``, so ``package_skills`` is empty).
    """
    if not package_skills:
        return
    roots = _edition_skill_roots()
    if not roots:
        return
    resolved_roots = []
    for r in roots:
        try:
            resolved_roots.append(r.resolve())
        except OSError:
            continue
    for row in package_skills:
        raw = row.get("dir") or row.get("path")
        if not raw:
            continue
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if not any(p == root or root in p.parents for root in resolved_roots):
            logger.warning(
                "skill %r (path %s) is outside every extra_skills() root %s — it "
                "will list in /api/skills but 404 on tree/detail (CapabilityManager."
                "list_skills / McpToolingProvider.extra_skills containment invariant)",
                row.get("name") or row.get("key"),
                raw,
                [str(r) for r in resolved_roots],
            )


# ── Skill directory browser (tree + file content) ──


# Hard caps to keep the API responsive and bounded.
SKILL_TREE_MAX_ENTRIES = 500
SKILL_FILE_MAX_BYTES = 1_048_576  # 1 MiB


def _resolve_skill_root(name: str, state: DashboardState, session_key: str = "") -> Path | None:
    """Return the absolute skill directory for *name*, or None.

    Accepts the same nested-name scheme used by the existing skill API:
    - ``foo`` → ``~/.kiro/crew/skills/foo``
    - ``utils/tiny-url`` → ``~/.kiro/crew/skills/utils/tiny-url``
    - ``package/<skill>`` → resolved via _resolve_package_skill_path() lookup
    - ``package/<qualifier>:<skill>`` → same, narrowed to the root whose CANONICAL
      qualifier equals ``<qualifier>`` — the identity digest :func:`_root_identity_token`
      derives for that root, not a segment the root happens to carry, so a
      unique-but-non-canonical segment does not resolve (see
      :func:`_split_package_skill_key`)
    - ``kiro-user/<skill>`` → ``~/.kiro/skills/<skill>``
    - ``kiro-workspace/<skill>`` → ``<project>/.kiro/skills/<skill>``

    *session_key* scopes ``kiro-workspace/`` to the requesting chat slot's
    project. Without it, resolution falls back to the single project shared by
    every slot — and fails closed to ``None`` when open slots disagree, since
    guessing could read the wrong checkout (#2457).

    The returned path is always under one of the allowed roots — paths
    that try to escape via ``..`` or symlinks are rejected.
    """
    if not name or ".." in name or name.startswith("/"):
        return None
    if name.startswith("kiro-user/"):
        rel = name[len("kiro-user/") :]
        root = Path.home() / ".kiro" / "skills"
    elif name.startswith("kiro-workspace/"):
        rel = name[len("kiro-workspace/") :]
        # NOT trust-gated, deliberately: reading a SKILL.md is how the operator
        # decides whether to grant trust in the first place, so requiring the
        # grant to view the file would make the consent decision blind. The
        # boundary that matters -- an unconsented project skill never reaching the
        # agent's context -- is enforced in SkillsLoader. Uses the permissive
        # resolver so the documented keyless single-project fallback and the
        # #2457 two-project behaviour stay as they are.
        proj = active_project_dir(state, session_key)
        if proj is None:
            return None
        root = proj / ".kiro" / "skills"
    elif name.startswith(PACKAGE_KEY_PREFIX):
        # Locate via existing helper (sync version). The active project's
        # ``.kiro/skills`` joins the canonical exclusion here because this caller
        # is the one that knows the chat slot.
        pkg_rel = name[len(PACKAGE_KEY_PREFIX) :]
        qualifier, rel = _split_package_skill_key(pkg_rel)
        canonical = _resolved_set(_canonical_skill_roots())
        proj = active_project_dir(state, session_key)
        if proj is not None:
            canonical |= _resolved_set([proj / ".kiro" / "skills"])
        # The separator is RESERVED: a ``package/`` key carrying it is always the
        # qualified reading, and one that no longer resolves 404s. There is no
        # verbatim retry of the whole remainder, because the remainder of a qualified
        # key has exactly the same shape as a colon-carrying literal path — so a
        # retry cannot tell "PkgA is gone" from "a skill is named PkgA:shared-skill",
        # and would answer a stale key with whatever OTHER root holds a directory of
        # that literal name, serving another bundle's content under this key.
        #
        # Deciding by the installed ROOT SET was the earlier attempt at that, and it
        # made a key's MEANING depend on which roots happen to exist: uninstalling a
        # root, or installing an unrelated one whose path carries the segment, would
        # silently flip an existing key between the two readings. Reserving the
        # separator removes the ambiguity at its source rather than adjudicating it.
        # The enumerator omits colon-carrying paths for the same reason, so both
        # halves of the API agree.
        path = _resolve_package_skill_path(rel, canonical, qualifier)
        if not path:
            return None
        candidate = path.parent
        if is_sensitive_path(str(candidate)):
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        # Re-check the *resolved* target — a symlink within the package path could
        # point at a sensitive location that the unresolved check missed
        # (consistent with the kirocrew/kiro branches below).
        if is_sensitive_path(str(resolved)):
            return None
        return resolved
    else:
        # ``kirocrew`` skills live under the active config home, which honors
        # KIROCREW_HOME (e.g. isolated dev gateways).  Hardcoding
        # ``~/.kirocrew`` here would 404 every skill in a KIROCREW_HOME-isolated
        # deployment even though SkillsLoader (the GET /api/skills source)
        # resolves them correctly.
        rel = name
        # Reject empty, traversal, absolute, and home-expansion inputs before
        # any filesystem probing. pathlib collapses ``Path(root) / "/etc"`` to
        # ``/etc`` (absolute RHS overrides the base), so an un-rejected absolute
        # or ``~`` prefix would let _probe() run is_dir() on arbitrary paths
        # before the containment check.
        if not rel or ".." in rel or rel.startswith("/") or rel.startswith("~"):
            return None
        # Root precedence must match SkillsLoader.load_skill(): kirocrew ->
        # user extra_paths -> edition skill roots (lowest). Otherwise the tree
        # endpoint could display a different directory than load_skill() reads.
        roots = [skills_dir()]
        try:
            roots.extend(Path(p).expanduser() for p in KiroCrewConfig.load().skills.extra_paths)
        except Exception:
            logger.debug("failed to load extra skill paths from config", exc_info=True)
        roots.extend(_edition_skill_roots())

        def _probe(r: Path) -> bool:
            try:
                return (r / rel).is_dir()
            except OSError:
                return False

        root = next((r for r in roots if _probe(r)), skills_dir())
    candidate = root / rel
    if not candidate.is_dir():
        return None
    if is_sensitive_path(str(candidate)):
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    # Containment + symlink policy.  Skills can be nested under category
    # directories (``utils/multi-badger`` → ``<root>/utils/multi-badger``),
    # and a skill directory itself may be a symlink (an edition may install
    # symlink ``~/.kiro/skills/<name>`` to ``~/.agents/skills/<name>``).  We
    # therefore require the candidate's *parent* directory to resolve to a
    # location at or under the trusted root — which permits the leaf to be a
    # symlink while still rejecting a symlinked *intermediate* directory that
    # would let ``a/b`` escape the tree.  The resolved target is then checked
    # against the sensitive-path list as a final guard.
    try:
        parent_resolved = candidate.parent.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except OSError:
        return None
    if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
        return None
    if is_sensitive_path(str(resolved)):
        return None
    return resolved


# ── Agent-template skill mapping (skill:// resources <-> catalog keys) ──


# Upper bound on how many skills one agent template may map. Each mapped skill
# is a full SKILL.md that kiro-cli loads into the agent's context, so an
# unbounded list is a context-exhaustion footgun, not a feature.
MAX_AGENT_SKILLS = 100


def _skill_key_roots(state: DashboardState, session_key: str = "") -> list[tuple[str, Path]]:
    """``(key_prefix, root)`` pairs for every location skills are keyed from.

    Mirrors :func:`_resolve_skill_root`'s roots, in the same precedence order,
    so an enumerated key names the same directory that function would resolve.
    Roots that cannot exist in this deployment (no active project dir, no
    edition roots) are omitted. *session_key* scopes the ``kiro-workspace/``
    root to the requesting chat slot's project, exactly as
    :func:`_resolve_skill_root` does — the two MUST agree or an enumerated key
    would not resolve (#2457).
    """
    out: list[tuple[str, Path]] = [("kiro-user/", Path.home() / ".kiro" / "skills")]
    proj = active_project_dir(state, session_key)
    if proj is not None:
        out.append(("kiro-workspace/", proj / ".kiro" / "skills"))
    out.extend(("", root) for root in _canonical_skill_roots()[1:])
    # ``package/`` covers only the edition roots the core does not already key
    # above — via the same helper the resolver uses, so enumeration and
    # resolution cannot drift apart. A key the catalog offers must be one the
    # resolver accepts, and vice versa.
    canonical = _resolved_set(root for _prefix, root in out)
    out.extend((PACKAGE_KEY_PREFIX, root) for root in _edition_package_roots(canonical))
    return out


# Skills may sit under category directories (``utils/tiny-url``). Bound the
# enumeration walk so a deep or pathological tree cannot turn one PATCH into an
# unbounded filesystem crawl. Three levels covers every layout in use.
_SKILL_NEST_DEPTH = 3


def _resolver_answers_catalog_key(key: str, *, refuse_leading_tilde: bool) -> bool:
    """Whether :func:`_resolve_skill_root` can answer *key* — the ONE place this is decided.

    Tested on the minted KEY, the exact string resolution receives, so the two sides
    cannot drift. Each rule carries the resolver's own scope: ``..`` anywhere is refused
    in every territory, that gate being reached before any prefix is dispatched; a leading
    ``~`` only when *refuse_leading_tilde*, since the unprefixed branch alone refuses it
    and the others join the rel literally. Passed as data because the package walk walks
    with an empty prefix deliberately, to compare two roots' rels before either is a key.
    """
    if ".." in key:
        return False
    if refuse_leading_tilde and key.startswith("~"):
        return False
    return True


def _collect_skills_under(
    directory: Path,
    root: Path,
    root_resolved: Path,
    prefix: str,
    out: dict[str, Path],
    depth: int,
    refuse_leading_tilde: bool = False,
) -> None:
    """Add every ``<dir>/SKILL.md`` at or under *directory* to *out*.

    Containment mirrors :func:`_resolve_skill_root`: a candidate's *parent* must
    resolve at or under the trusted root, which permits the skill directory
    itself to be a symlink (an edition may install a skill by symlinking
    ``~/.kiro/skills/<name>`` to elsewhere) while still rejecting a symlinked
    *intermediate* directory that would let ``a/b`` escape the tree. Sensitive
    paths are rejected before and after symlink resolution.
    """
    if depth <= 0:
        return
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if is_sensitive_path(str(entry)):
            continue
        try:
            parent_resolved = entry.parent.resolve(strict=True)
        except OSError:
            continue
        if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
            continue
        skill_md = entry / "SKILL.md"
        if skill_md.is_file():
            try:
                target = skill_md.resolve(strict=True)
            except OSError:
                continue
            if is_sensitive_path(str(target)):
                continue
            key = prefix + entry.relative_to(root).as_posix()
            if not _resolver_answers_catalog_key(key, refuse_leading_tilde=refuse_leading_tilde):
                continue
            # First root wins, matching _skill_key_roots precedence.
            out.setdefault(key, skill_md)
        else:
            _collect_skills_under(
                entry, root, root_resolved, prefix, out, depth - 1, refuse_leading_tilde
            )


def enumerate_skill_catalog(state: DashboardState, session_key: str = "") -> dict[str, Path]:
    """Map every discoverable catalog key to its ``SKILL.md`` path.

    Built by **enumerating** the skill roots, never by joining a caller-supplied
    string onto one. That is the security property this function exists for: the
    only paths the agent-template editor can ever hand to the filesystem or
    write into an agent spec are paths this walk discovered, so a hostile or
    traversing key (``../../.ssh``, an absolute path, a ``~`` prefix) can do
    nothing but miss a dict lookup. Allowlist by enumeration rather than
    validate-then-join — it also removes the tainted-path dataflow that
    validate-then-join leaves for static analysis to flag.

    *session_key* only selects which project's ``kiro-workspace/`` root joins
    the walk (see :func:`_skill_key_roots`); it never widens the enumeration
    property above. Results are computed per call — nothing is cached — so a
    per-session root cannot leak into another session's catalog.

    It is additionally the single source of truth for BOTH directions of the
    key <-> URI mapping, so they cannot disagree: a mapping written against a
    symlinked skill directory inverts back to the same key it was written from.

    ``package/`` roots are walked separately and folded in by
    :func:`_merge_package_walks`, because whether a relative path needs a
    qualifier is only knowable once every package root has been walked.
    """
    catalog: dict[str, Path] = {}
    package_walks: list[tuple[Path, dict[str, Path]]] = []
    for prefix, root in _skill_key_roots(state, session_key):
        if is_sensitive_path(str(root)) or not root.is_dir():
            continue
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            continue
        if prefix == PACKAGE_KEY_PREFIX:
            # Walked unprefixed into its own dict so the relative paths of two
            # roots can be compared before either becomes a key.
            found: dict[str, Path] = {}
            _collect_skills_under(root, root, root_resolved, "", found, _SKILL_NEST_DEPTH)
            package_walks.append((root, found))
            continue
        _collect_skills_under(
            root,
            root,
            root_resolved,
            prefix,
            catalog,
            _SKILL_NEST_DEPTH,
            # Only the unprefixed branch of the resolver refuses a leading ``~``; the
            # prefixed territories join the rel literally and resolve such a name.
            refuse_leading_tilde=(prefix == ""),
        )
    # A core row whose own relative path is literally ``package/<rel>`` keys into the
    # reserved prefix, but detail and tree now route a ``package/`` key exclusively to
    # the package roots, so nothing will ever reach it. Prune before the merge, or the
    # catalogue offers a key the resolver cannot answer.
    #
    # The ABSOLUTE path is logged, not just the key: the file stays on disk and drops
    # out of the catalog, so this warning is the only surface naming where it is — and it
    # is the only REMEDIATION surface too, because ``api_skill_detail`` refuses every
    # mutating verb on a ``package/`` key unconditionally, stranded or not. An operator
    # removes the file by hand, at the path this line names.
    for reserved in [k for k in catalog if k.startswith(PACKAGE_KEY_PREFIX)]:
        logger.warning(
            "core skill %r keys into the reserved %r prefix, which only package "
            "roots can answer; omitting it — its SKILL.md remains on disk at %s. "
            "Rename or remove that directory to reclaim the key: no API verb can, "
            "because every mutating verb refuses a reserved-prefix key",
            reserved,
            PACKAGE_KEY_PREFIX,
            catalog[reserved],
        )
        del catalog[reserved]
    _merge_package_walks(package_walks, catalog)
    return catalog


# Wide enough that finding a second root with the same qualifier is not a search anyone
# can run: a narrow digest is GROUND, not merely collided with by accident.
_ROOT_IDENTITY_DIGEST_BYTES = 16


def _root_identity_token(root: Path) -> str | None:
    """The qualifier for *root*: a stable, collision-resistant token for its identity.

    A qualifier answers one question — WHICH of several roots bundling the same relative
    path does this key mean — so the only thing it carries is *root*'s own identity, and
    it must be an identity rather than a distinguishing path segment. A segment is chosen
    against whichever roots collide at derivation time, so a root that is REPLACED
    (uninstalled, and a different root installed that still carries the same segment,
    e.g. ``<...>/A/skills`` giving way to ``<...>/A/v2/skills``) would re-derive the very
    same qualifier. A key an editor still holds would then resolve to a DIFFERENT file,
    and because the agent-config write path (:func:`apply_skill_mapping`) resolves keys
    against a FRESH catalog at write time, it would persist a ``skill://`` URI for a
    skill the user never selected — silent, and durable in the agent's config.

    This token closes that, and both of its halves matter. It is derived from the root's
    own CANONICAL path plus that directory's device, inode and INODE CREATION TIME, so it
    is the same for the same root no matter what else is installed or removed alongside
    it: a key stays valid across an unrelated bundle install, and the editor's held key
    and the agent config's persisted URI keep meaning what they meant. The inode is what
    makes it an identity rather than an ADDRESS: the path alone re-derives when one bundle
    is uninstalled and another is installed at the SAME path, so a held key would resolve
    to the replacement's file and the write path would persist a skill nobody selected.
    The creation time is what makes that identity NON-REUSABLE, and it is load-bearing
    rather than belt-and-braces: an inode number is a reusable resource, so a replacement
    directory at the same path can be handed the identical ``st_ino`` — ordinary
    ext-family behaviour on a normal uninstall/reinstall — and dev:ino alone would then
    re-derive the replaced bundle's own qualifier. ``st_ctime_ns`` cannot be recycled
    with it, because it records the moment THIS inode was created and the clock does not
    run backwards. Per-instance, that stale key fails to resolve, which the write path
    turns into a whole-request rejection. **Residual, recorded rather than implied:** a
    filesystem's timestamp granularity is coarse (measured ~20ms on xfs), so an
    uninstall and reinstall completing inside ONE granule AND handed the recycled inode
    re-derives the replaced key after all. Closing that needs a creation time the
    platform will not give us — Linux exposes no ``st_birthtime`` through ``os.stat``
    (verified: absent from ``os.stat_result`` on this interpreter) — so the narrowed
    window is what this basis buys, not its elimination.

    The cost is that the identity tracks the root's own top-level contents: adding or
    removing a skill directory in a root moves that directory's ``st_ctime_ns`` and so
    re-spells every qualified key in it (a write further down, inside a skill, does not).
    That is a mutation of the bundle itself rather than an unrelated install, and the
    re-enumerate-only contract already tolerates it — as it tolerates re-materialising the
    same bundle through a remount or a restore. The cost of re-spelling is a 404, never
    another bundle's bytes. A root that does not exist keeps a path-only identity, because
    a qualifier must be derivable for every path the OS can name — not a way back to the
    rebind, since minting requires copies on disk, so a minted digest always carried an
    inode and a later path-only one cannot match it. That last claim is only as strong as
    the digest is wide: a narrow one can be GROUND against, so an install path could be
    chosen to collide with a key already minted for another root.
    :data:`_ROOT_IDENTITY_DIGEST_BYTES` is the bound that makes the search infeasible
    rather than merely unlikely.

    Lowercase hex is also already key-safe: it carries neither
    :data:`_SKILL_KEY_QUALIFIER_SEP` nor a glob metacharacter nor a traversal element, so
    the key round-trips without a per-candidate filter. What it gives up is legibility —
    ``package/a1b2c3d4e5f67890abcdef1234567890:tool`` does not say which bundle it means. The omission
    warnings carry the absolute path instead, which is the surface a reader needs, and a
    resolved row still shows its own file.

    Canonical and not the advertised path, so an edition that advertises one root
    through a symlink alias keeps ONE identity. ``blake2b`` and not ``hash()``, which
    is salted per process and would mint a different key on every restart. A root that
    does not canonicalise yields ``None`` and the caller omits the path: identity that
    cannot be established fails closed.
    """
    try:
        resolved = root.resolve()
        basis = str(resolved)
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        st: os.stat_result | None = resolved.stat()
    except (OSError, ValueError):
        st = None
    # ``os.fsencode`` and NOT ``basis.encode("utf-8")``: on POSIX a path is bytes, so a
    # byte the encoding cannot decode arrives as a lone surrogate that utf-8 refuses.
    material = os.fsencode(basis)
    if st is not None:
        # ``st_ctime_ns`` is the non-reusable part: an inode number can be recycled, the
        # moment a replacement inode was created cannot.
        material += b"\x00" + f"{st.st_dev}:{st.st_ino}:{st.st_ctime_ns}".encode("ascii")
    return hashlib.blake2b(material, digest_size=_ROOT_IDENTITY_DIGEST_BYTES).hexdigest()


def _dedupe_entries(entries: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    """One ``(root, file)`` pair per DISTINCT file, CONTAINED by its root, in order.

    The single dedupe for both sides of the grammar: the enumerator's collision fold and
    the resolver's tier loop. One skill is routinely reachable through two roots — an
    edition may advertise both a directory and a symlink into it — and that is NOT an
    ambiguity; only distinct FILES are. Qualified keys are emitted per distinct COPY, so
    a third root holding a symlink to another root's copy is a genuine collision by
    count yet the same file: iterating raw entries would give it its own key pointing at
    a file another key already names, two catalog rows for one skill.

    **Containment is enforced here, on the CANONICAL forms of both sides.** ``Path.glob``
    matches a symlinked directory's dirent and yields a path that is LEXICALLY under the
    root while resolving anywhere on the filesystem, so a skill directory — or any
    intermediate directory on the way to it — that is a symlink pointing outside the root
    would otherwise hand the detail endpoint a file outside the package territory
    entirely. A prefix test on the unresolved path cannot see that, and neither can the
    glob result itself; only ``resolve()`` on both sides can. That makes this the one
    place the check belongs: all three call sites (both resolver tiers and the fold) pass
    through it, so enumeration and resolution refuse the same entries by construction
    rather than by two rules kept in step by hand — an escaping entry omitted from only
    one side would be a catalogued row that 404s on open, or a resolvable key ``/tree``
    never lists.

    ``Path.resolve()`` raises ``RuntimeError`` (not ``OSError``) on a symlink loop, and a
    looping ``SKILL.md`` IS yielded by ``glob`` because a literal pattern matches the
    dirent without following it. Catching only ``OSError`` would turn that into a 500 on
    a browser-triggered request, so an unresolvable entry is skipped instead: it cannot
    be read anyway. An unresolvable ROOT fails the same way — closed, not open, since
    containment cannot be established against a root that does not canonicalise.
    """
    kept: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    canonical_roots: dict[Path, Path | None] = {}
    for root, skill_md in entries:
        if root not in canonical_roots:
            try:
                canonical_roots[root] = root.resolve()
            except (OSError, RuntimeError):
                canonical_roots[root] = None
        canonical_root = canonical_roots[root]
        try:
            identity = skill_md.resolve()
        except (OSError, RuntimeError):
            # Unreadable anyway — see the symlink-loop paragraph above.
            continue
        if canonical_root is None or not identity.is_relative_to(canonical_root):
            # Escapes the root it was found under. Logged with both absolute paths
            # because the row simply will not appear, and this line is the only
            # remediation surface an edition author gets.
            logger.warning(
                "edition skill %s resolves to %s, outside its root %s — omitting it; "
                "a package skill directory may not symlink out of its own root",
                skill_md,
                identity,
                canonical_root if canonical_root is not None else root,
            )
            continue
        if identity in seen:
            continue
        seen.add(identity)
        kept.append((root, skill_md))
    return kept


def _package_collision(
    entries: list[tuple[Path, Path]],
) -> tuple[list[tuple[Path, Path]], list[str] | None]:
    """The ONE collision-set computation, shared by enumeration and resolution.

    Returns ``(copies, qualifiers)`` for a single walked rel:

    * *copies* is the deduplicated ``(root, file)`` list — one entry per DISTINCT
      file, contained by its root (see :func:`_dedupe_entries`).
    * *qualifiers* is one qualifier per entry of *copies*, in the same order, or
      ``None`` when this rel has no addressable qualified spelling at all.

    ``None`` covers BOTH of the ways that happens, because both mean the catalogue
    offers no qualified key: fewer than two distinct copies (so the rel is keyed
    unqualified), or a collision in which some root does not canonicalise and so yields
    no identity. Callers distinguish the two by ``len(copies)``, which is what lets
    enumeration mint the unqualified key in the first case and omit in the second, while
    resolution refuses in both.

    The all-or-nothing shape is kept as a FAIL-CLOSED backstop rather than a rule that
    fires in normal operation. Since :func:`_root_identity_token` is a digest of the root's
    own canonical path, every root that canonicalises yields one, and two distinct roots
    cannot yield the same one — so neither the missing-qualifier nor the duplicate branch
    is reachable except when the filesystem refuses to canonicalise a root.

    **Both sides call this rather than each computing it**, and that is the point: every
    key the catalog EMITS must resolve to the file it was emitted for, so any difference
    in how the two build this set is a phantom row — a key ``/tree`` lists and ``detail``
    404s. That is the direction this shares, and it is the whole of it: resolution is
    deliberately WIDER than enumeration, because the resolver also accepts a leaf-name
    key through its nested tier, so a rel the catalog lists at its full relative path
    stays reachable under the bare leaf. Two copies of the rule agreeing today is not
    even the narrow guarantee; it is a pair that drifts on the next edit to either one.
    One implementation cannot disagree with itself.
    """
    copies = _dedupe_entries(entries)
    if len(copies) < 2:
        return copies, None
    roots = [root for root, _skill_md in copies]
    qualifiers = [_root_identity_token(root) for root in roots]
    if any(q is None for q in qualifiers) or len(set(qualifiers)) != len(qualifiers):
        return copies, None
    # Every element is a str once none is None; narrowed for the caller's benefit.
    return copies, [q for q in qualifiers if q is not None]


def _omitted_paths(entries: Iterable[tuple[Path, Path]]) -> str:
    """Absolute ``SKILL.md`` paths of *entries*, sorted, for an omission warning.

    Every omission warning owes the reader the ABSOLUTE path, because the row is simply
    absent afterwards and the key alone is relative to a root the reader cannot infer —
    so the log line is the only remediation surface there is. Two of the warnings named
    just the relative key, which told an operator that something was dropped without
    telling them where to find it.

    The FILE and not its root: a root can bundle several skills, and the operator's next
    action is on the one directory this key named.
    """
    return ", ".join(sorted(str(skill_md) for _root, skill_md in entries))


def _merge_package_walks(
    walks: list[tuple[Path, dict[str, Path]]], catalog: dict[str, Path]
) -> None:
    """Fold per-root ``package/`` walks into *catalog*, qualifying collisions.

    A relative path found in one root only — or found in several that all reach
    the SAME file through a symlink — keeps the plain ``package/<rel>`` key it has
    always had. A relative path backed by two DISTINCT files is what today's
    single key cannot address: whichever root won ``setdefault`` produced a key
    the resolver then refused, so the entry was a phantom in the editor. Those
    become one qualified key per copy instead.

    A relative path is OMITTED and logged only when the collision has no addressable
    qualified spelling at all: a root that does not canonicalise yields no identity, so
    :func:`_package_collision` returns no qualifiers. That is a fail-closed backstop
    rather than a normal outcome, since every root that canonicalises has a digest.
    Omitting keeps the documented invariant intact in the ONE direction it claims —
    every key this walk offers is one :func:`_resolve_skill_root` accepts — which is what
    lets a caller treat an enumerated key as routable without re-checking it. The
    converse is deliberately NOT claimed: resolution is wider, because it also accepts a
    leaf-name key through its nested tier, so a key this walk never offered can still
    resolve. Read as two-way, that reads like a defect; it is the intended asymmetry.

    A relative path that itself contains the qualifier separator is omitted for the
    same reason: its key is indistinguishable from a qualified one, so the resolver
    reads it as a ``<qualifier>:<rel>`` pair rather than the path meant. The resolver
    agrees rather than guessing — it does not retry the remainder verbatim, because
    the remainder of a genuine qualified key has the same shape, and a stale qualified
    key would then be answered by whatever root happened to hold a directory of that
    literal name. So such a path is not addressable under this grammar at all.

    A qualified key is written unconditionally. This helper does NOT re-check whether
    the key is already taken, because it cannot be: the caller prunes every
    ``package/``-prefixed key before folding, which settles a core-versus-package
    contest, and the fold cannot contend with itself because a qualifier never carries
    the separator. That precondition is the caller's and is asserted against the
    caller.
    """
    owners: dict[str, list[tuple[Path, Path]]] = {}
    for root, found in walks:
        for rel, skill_md in found.items():
            owners.setdefault(rel, []).append((root, skill_md))
    for rel, entries in owners.items():
        if _SKILL_KEY_QUALIFIER_SEP in rel:
            logger.warning(
                "edition skill %r carries the reserved key qualifier separator %r, so "
                "its key would be read as a qualified key naming another path; "
                "omitting it — the affected SKILL.md files remain on disk at %s",
                rel,
                _SKILL_KEY_QUALIFIER_SEP,
                _omitted_paths(entries),
            )
            continue
        if not _names_a_relative_path(rel):
            # The SAME predicate the resolver applies, for the same reason the separator
            # check above exists: a rel the resolver refuses would be catalogued and
            # then 404 on open — the phantom row this grammar removes. A directory
            # literally named ``**`` is legal on POSIX and IS walked, so without this it
            # is the one reachable divergence left (an anchor, a drive or a traversal
            # element cannot be a dirent, and a colon-carrying name is already omitted
            # above). Derived qualifiers are held to this predicate too, so holding the
            # rel to it makes the rule uniform rather than a special case.
            logger.warning(
                "edition skill %r is not a relative path the resolver accepts (a glob "
                "wildcard, an anchor or a traversal element), so its key would be "
                "catalogued and unresolvable; omitting it — the affected SKILL.md files "
                "remain on disk at %s",
                rel,
                _omitted_paths(entries),
            )
            continue
        entries, qualifiers = _package_collision(entries)
        if not entries:
            continue
        if qualifiers is None:
            if len(entries) < 2:
                catalog.setdefault(PACKAGE_KEY_PREFIX + rel, entries[0][1])
                continue
            logger.warning(
                "edition skill %r is bundled by %d roots that share every path "
                "segment; omitting it — no key could address one copy. The affected "
                "SKILL.md files remain on disk at %s",
                rel,
                len(entries),
                _omitted_paths(entries),
            )
            continue
        for qualifier, (_root, skill_md) in zip(qualifiers, entries, strict=True):
            key = f"{PACKAGE_KEY_PREFIX}{qualifier}{_SKILL_KEY_QUALIFIER_SEP}{rel}"
            # Assigned unconditionally: no key written here can already be present.
            # Every one carries ``PACKAGE_KEY_PREFIX``, and the sole caller prunes every
            # prefixed key out of *catalog* immediately before folding, so a
            # core-versus-package contest for a qualified key is already settled. Nor
            # can this fold contend with itself: a qualifier is lowercase hex, so it
            # never carries the separator and a key splits back to exactly one
            # ``(qualifier, rel)`` pair, and within one *rel* the qualifiers are distinct
            # because each is a digest of a distinct root. The precondition belongs to
            # the caller and is asserted there by
            # ``test_the_fold_is_handed_a_catalog_free_of_reserved_prefix_keys``.
            catalog[key] = skill_md


def _skill_uri_for_path(skill_md: Path) -> str:
    """Render a discovered ``SKILL.md`` path as a ``skill://`` resource URI.

    Paths under ``$HOME`` are emitted in ``~/`` form: kiro-cli expands it, and
    it keeps the written agent spec portable across machines and home dirs.
    """
    try:
        rel_home = skill_md.relative_to(Path.home())
    except ValueError:
        return f"{SKILL_URI_PREFIX}{skill_md.as_posix()}"
    return f"{SKILL_URI_PREFIX}~/{rel_home.as_posix()}"


def skill_key_for_uri(
    uri: str,
    agent_path: Path,
    state: DashboardState,
    catalog: dict[str, Path] | None = None,
    session_key: str = "",
) -> str | None:
    """Invert a ``skill://`` resource URI back to a catalog key, or ``None``.

    ``None`` means "not editable through the catalog" — a wildcard pattern, or a
    path that no enumerated skill accounts for (a hand-authored URI, or a skill
    that has since been deleted). Callers preserve those verbatim instead of
    rewriting or dropping them.

    Pass *catalog* (from :func:`enumerate_skill_catalog`) to reuse one walk
    across many URIs.
    """
    if any(c in uri for c in _GLOB_CHARS):
        return None
    expanded = expand_skill_uri(uri, agent_path)
    if not expanded:
        return None
    entries = catalog if catalog is not None else enumerate_skill_catalog(state, session_key)
    wanted = Path(expanded)
    for key, path in entries.items():
        if path == wanted:
            return key
    # Fall back to comparing resolved targets so a URI written against a
    # symlinked skill directory (or against its target) still inverts.
    try:
        target = wanted.resolve(strict=True)
    except OSError:
        return None
    for key, path in entries.items():
        try:
            if path.resolve(strict=True) == target:
                return key
        except OSError:
            continue
    return None


def skill_uri_for_key(
    key: str,
    state: DashboardState,
    catalog: dict[str, Path] | None = None,
    session_key: str = "",
) -> str | None:
    """Resolve a catalog key to the ``skill://`` URI for its ``SKILL.md``.

    A miss returns ``None`` — the key names no discoverable skill. Because the
    lookup goes through :func:`enumerate_skill_catalog` rather than joining
    *key* onto a root, an arbitrary caller-supplied key can never widen an
    agent's resources beyond the enumerated skill trees.

    Pass *catalog* to reuse one walk across many keys.
    """
    entries = catalog if catalog is not None else enumerate_skill_catalog(state, session_key)
    skill_md = entries.get(key)
    if skill_md is None:
        return None
    return _skill_uri_for_path(skill_md)


def agent_skill_views(
    data: dict[str, Any], agent_path: Path, state: DashboardState, session_key: str = ""
) -> tuple[list[str], list[str]]:
    """``(catalog_keys, unmanaged_uris)`` for *data*, from ONE catalog walk.

    The two views partition the agent's ``skill://`` resources: keys the editor
    owns and can rewrite, and URIs it cannot express (wildcards, or paths no
    enumerated skill accounts for) which are shown read-only and preserved on
    every write. Both are order-preserving; keys are de-duplicated.

    Filesystem-heavy (it enumerates the skill roots) — callers on the asyncio
    event loop MUST run this off the loop.
    """
    catalog = enumerate_skill_catalog(state, session_key)
    keys: list[str] = []
    unmanaged: list[str] = []
    seen: set[str] = set()
    for uri in skill_resource_uris(data):
        key = skill_key_for_uri(uri, agent_path, state, catalog)
        if key is None:
            unmanaged.append(uri)
        elif key not in seen:
            seen.add(key)
            keys.append(key)
    return keys, unmanaged


def agent_skill_keys(
    data: dict[str, Any], agent_path: Path, state: DashboardState, session_key: str = ""
) -> list[str]:
    """Catalog keys for the skills *data* maps, de-duplicated, order-preserving.

    Only catalog-resolvable entries are returned — this is the set the Agent
    Templates editor owns and can rewrite. Wildcard / hand-authored URIs are
    excluded here and reported separately by :func:`agent_unmanaged_skill_uris`.
    """
    return agent_skill_views(data, agent_path, state, session_key)[0]


def agent_unmanaged_skill_uris(
    data: dict[str, Any], agent_path: Path, state: DashboardState, session_key: str = ""
) -> list[str]:
    """``skill://`` URIs that the catalog editor cannot express, in order.

    Wildcards, and paths no enumerated skill accounts for. Surfaced read-only in
    the UI and preserved on every write so editing an agent through the dashboard
    never silently drops a hand-authored mapping.
    """
    return agent_skill_views(data, agent_path, state, session_key)[1]


def apply_skill_mapping(
    data: dict[str, Any],
    agent_path: Path,
    state: DashboardState,
    keys: list[str],
    session_key: str = "",
) -> tuple[list[str], list[str]]:
    """Rewrite *data*'s ``skill://`` resources to *keys*, in place.

    Returns ``(applied_keys, unknown_keys)``. Nothing is written when
    *unknown_keys* is non-empty — the caller rejects the whole request so a
    typo'd key can never partially apply.

    Invariants:

    * Non-``skill://`` resources (``file://`` steering globs) keep their
      original relative order and are never touched.
    * Unmanaged ``skill://`` URIs (wildcards, hand-authored paths) are preserved.
    * The managed set is fully replaced, so removing a key removes the mapping.
    """
    applied: list[str] = []
    unknown: list[str] = []
    uris: list[str] = []
    seen: set[str] = set()
    # One enumeration for the whole write: every key resolved and every existing
    # URI inverted against the SAME snapshot, so a concurrent skill add/remove
    # cannot make the two halves disagree mid-request.
    catalog = enumerate_skill_catalog(state, session_key)
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        uri = skill_uri_for_key(key, state, catalog)
        if uri is None:
            unknown.append(key)
            continue
        applied.append(key)
        uris.append(uri)
    if unknown:
        return applied, unknown

    resources = data.get("resources") or []
    if not isinstance(resources, list):
        resources = []
    kept = [
        r
        for r in resources
        if not (isinstance(r, str) and r.startswith(SKILL_URI_PREFIX))
        or skill_key_for_uri(r, agent_path, state, catalog) is None
    ]
    merged = kept + [u for u in uris if u not in kept]
    if merged:
        data["resources"] = merged
    else:
        # An empty list is meaningful to kiro-cli (it suppresses the shipped
        # steering defaults that _refresh_dynamic_fields only re-seeds when the
        # key is absent/empty), and an agent with nothing mapped should fall
        # back to those defaults — so drop the key instead of writing [].
        data.pop("resources", None)
    return applied, unknown


def list_skill_tree(skill_root: Path) -> list[dict[str, Any]]:
    """Return a flat list of files under *skill_root*, capped at SKILL_TREE_MAX_ENTRIES.

    Each entry: ``{path: relative-from-root, type: "file"|"dir", size: int}``.
    Sensitive paths are filtered out.  Symlinks are resolved; entries whose
    real path escapes *skill_root* are omitted.
    """
    out: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(skill_root, followlinks=False):
        # Stable order — reproducible across runs / tests.
        dirnames.sort()
        filenames.sort()
        for d in list(dirnames):
            full = Path(dirpath) / d
            if is_sensitive_path(str(full)):
                dirnames.remove(d)
                continue
            rel = full.relative_to(skill_root).as_posix()
            out.append({"path": rel, "type": "dir", "size": 0})
            if len(out) >= SKILL_TREE_MAX_ENTRIES:
                return out
        for f in filenames:
            full = Path(dirpath) / f
            if is_sensitive_path(str(full)):
                continue
            try:
                if full.is_symlink():
                    real = full.resolve(strict=True)
                    real.relative_to(skill_root.resolve(strict=True))
                    if is_sensitive_path(str(real)):
                        continue
                stat = full.stat()
            except (OSError, ValueError):
                continue
            rel = full.relative_to(skill_root).as_posix()
            out.append({"path": rel, "type": "file", "size": int(stat.st_size)})
            if len(out) >= SKILL_TREE_MAX_ENTRIES:
                return out
    return out


def read_skill_file(skill_root: Path, rel_path: str) -> tuple[str, str | None]:
    """Read ``skill_root/rel_path`` with safety + size guards.

    Returns ``(content, error)``.  ``error`` is non-empty when access is
    denied, the file is too big, or it doesn't exist.
    """
    if not rel_path or ".." in rel_path.split("/") or rel_path.startswith("/"):
        return "", "invalid path"
    target = skill_root / rel_path
    try:
        resolved = target.resolve(strict=True)
        skill_resolved = skill_root.resolve(strict=True)
        resolved.relative_to(skill_resolved)
    except (OSError, ValueError):
        return "", "not found"
    if is_sensitive_path(str(resolved)):
        return "", "access denied"
    if not resolved.is_file():
        return "", "not a file"
    try:
        size = resolved.stat().st_size
    except OSError:
        return "", "stat failed"
    if size > SKILL_FILE_MAX_BYTES:
        return "", f"file too large ({size} bytes; cap {SKILL_FILE_MAX_BYTES})"
    try:
        return resolved.read_text(encoding="utf-8", errors="replace"), None
    except OSError:
        return "", "read failed"


def _read_session_key(request: "Any") -> str:
    """Read and normalize the ``X-Session-Key`` header for authz comparisons.

    Strips surrounding whitespace so the authorization gate matches the
    canonical stored key form and the routing endpoints (which already
    ``.strip()``). A trailing space / stray whitespace must not let a
    restricted or read-blocked session slip past the restricted-key set or the
    slot lookup (CWE-178/180 — inconsistent normalization in an auth context).
    """
    return request.headers.get("X-Session-Key", "").strip()


def _caller_bounds(request: web.Request) -> tuple[dict[str, str], int]:
    """Read the caller's own session bounds from the token that authenticated it.

    Shared by every handler that mints a NEW credential on the authority of an
    existing dashboard session (the mobile login link and the tailnet QR mint),
    so the two mint surfaces cannot drift apart on the invariant: the minted
    credential must never out-scope the session authorizing it.

    Returns ``(carried_claims, ttl_ceiling_seconds)``. ``ttl_ceiling`` is ``0``
    when the caller has no lifetime left to lend, which the handler refuses
    rather than minting against. Claims are carried, never re-derived: ``boot``
    copied verbatim (same rule as the link→session exchange in ``token_auth``),
    ``no_refresh`` copied so the recipient session never grows a refresh chain,
    and the remaining ``session_exp`` becomes the TTL ceiling so a short-lived
    caller cannot mint a longer-lived credential. ``require_peer`` and its
    signed ``peer_key`` move as one inseparable device bound. Fail-closed on an
    unreadable payload: a caller whose bounds cannot be established gets a
    bounded (no-refresh, default-TTL-capped) link rather than an unbounded one.

    **Read the credential the middleware VALIDATED, not a re-extracted one.**
    Only that credential has a verified signature; the other one was never
    checked. ``token_auth`` publishes it as ``request["auth_token"]`` for
    exactly this reason: its own extraction prefers ``?token=`` but falls back
    to the session cookie when the query token is invalid, so re-deriving with
    a fixed query-then-cookie order could pick the credential that was NOT
    validated — letting a request that authenticated with a bounded cookie have
    its bounds read from an unverified, attacker-settable query token, dropping
    ``no_refresh`` and raising the TTL ceiling to the full maximum, which is
    precisely the ceiling-escape this function exists to prevent. When no
    credential was published (a surface that authenticated by another means),
    the mint is bounded fail-closed the same way an unreadable payload is.

    **A non-positive remaining lifetime is never rounded up.** Clamping it to a
    floor of one second would let a caller whose own session has just run out
    mint a link that outlives it, and the exchange the recipient performs starts
    a fresh window — so repeating the mint would walk the expiry forward
    indefinitely from a session that should already be dead. Report ``0`` and
    let the caller be refused.
    """
    published = request.get("auth_token", "")
    token = published if isinstance(published, str) else ""
    carried: dict[str, str] = {}
    ttl_ceiling = MAX_SESSION_TTL_SECS
    if not token:
        # Authenticated without a readable token (unexpected on this surface):
        # fail closed by bounding the mint rather than trusting it.
        return {"no_refresh": "1"}, ttl_ceiling
    try:
        data = json.loads(_b64url_decode(token.split(".", 1)[0]))
        boot = str(data.get("boot", ""))
        if boot:
            carried["boot"] = boot
        if str(data.get("no_refresh", "")) == "1":
            carried["no_refresh"] = "1"
        if str(data.get("require_peer", "")) == "1":
            carried["require_peer"] = "1"
            # Middleware refuses a claimless require_peer cookie, so the
            # fallback is unreachable on a real authenticated request. Keep it
            # fail-closed for direct test doubles or future alternate auth:
            # an impossible key mints an unusable child instead of widening it.
            carried["peer_key"] = required_peer_key_unverified(token) or "unverified"
        session_exp = float(data.get("session_exp", 0.0))
        if session_exp:
            remaining = int(session_exp - time.time())
            if remaining <= 0:
                return carried, 0
            ttl_ceiling = min(ttl_ceiling, remaining)
    except Exception:
        return {"no_refresh": "1"}, ttl_ceiling
    return carried, ttl_ceiling


def _is_restricted_session(state: DashboardState, request: "Any") -> bool:
    """Check if request comes from an ephemeral (incognito) or temporary (guest) session.

    Reads X-Session-Key header (set by browser and MCP subprocesses).
    Returns True if the session should be blocked from memory operations.
    """
    sk = _read_session_key(request)
    if not sk:
        return False
    if sk == "dashboard:ui":
        return False
    if sk in state._restricted_keys:
        return True
    slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
    slot = state._slots.get(slot_name)
    if slot and slot.is_restricted:
        return True
    if is_channel_session_key(sk):
        # Restore the DURABLE flags before consulting the in-memory maps. The
        # privacy trackers are process-local and are only populated by
        # ``privacy_mode.hydrate`` on an INBOUND channel message, so a turn that
        # no inbound message drove — a cron with session="origin", a
        # webhook-resumed session, a monitor/autonudge re-injection, a subagent —
        # reaches this gate with empty maps after a gateway restart even though
        # the user's !incognito is on disk. Calling the canonical restore (rather
        # than reading the SessionMap directly) keeps one source of truth and
        # self-heals the process-local view. Idempotent and allocation-free for
        # unflagged keys.
        #
        # Namespace-agnostic on purpose. A ``startswith("slack:")`` test made this
        # branch structurally unreachable for every other channel, so a
        # ``telegram:{agent}:direct:{user}`` session the user marked incognito
        # could never enter it and the ~30 dashboard mutations gated on this
        # predicate stayed open for it.
        _hydrate_conv_flags(state.sessions, sk)
        if is_thread_temporary(sk) or is_thread_incognito(sk):
            return True
    # NOTE: deliberately no disk fallback for an absent slot. This predicate is
    # a SYNC helper with ~49 call sites reachable from async handlers, so reading
    # the persisted mode here would put blocking file I/O on the event loop
    # (AUTOSDE ``no-blocking-call-on-event-loop``). The archived-session recovery
    # is done off-loop instead, by the one caller that needs it —
    # ``api_lessons_create`` — via ``_probe_persisted_session``.
    return False


def _blocks_reads_session(state: DashboardState, request: "Any") -> bool:
    """Check if request comes from a temporary session that blocks memory reads."""
    sk = _read_session_key(request)
    if not sk or sk == "dashboard:ui":
        return False
    slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
    slot = state._slots.get(slot_name)
    if slot and slot.blocks_reads:
        return True
    if is_channel_session_key(sk):
        # Same durable-flag restore, and the same namespace-agnostic reach, as
        # _is_restricted_session: a temporary conversation whose flags this
        # process never hydrated must not serve reads, on any channel.
        _hydrate_conv_flags(state.sessions, sk)
        if is_thread_temporary(sk):
            return True
    # NOTE: deliberately no disk fallback for an absent slot. This predicate is
    # a SYNC helper with ~49 call sites reachable from async handlers, so reading
    # the persisted mode here would put blocking file I/O on the event loop
    # (AUTOSDE ``no-blocking-call-on-event-loop``). The archived-session recovery
    # is done off-loop instead, by the one caller that needs it —
    # ``api_lessons_create`` — via ``_probe_persisted_session``.
    return False


# Byte ceiling for the session-metadata head read. The metadata line is a small
# JSON object (a few hundred bytes); 64 KiB is generous headroom while keeping an
# enormous or adversarial first line from being pulled into memory.
_METADATA_HEAD_MAX_BYTES = 64 * 1024


def _persisted_session_paths(slot_name: str) -> list["Path"]:
    """Every existing session transcript that *slot_name* could name.

    Returns more than one entry only when the key is genuinely ambiguous — see
    :func:`_probe_persisted_session`, which treats that as unknown rather than
    picking a winner.
    """
    if (
        not slot_name
        or "/" in slot_name
        or "\\" in slot_name
        or "\x00" in slot_name
        or ":" in slot_name
        or slot_name.startswith(".")
    ):
        # Defence-in-depth against path traversal; ``KIROCREW_SESSION_KEY``
        # normally has no path separators, but ``X-Session-Key`` is
        # attacker-controlled in principle even behind the secret
        # middleware. Reject forward slash (Linux/macOS) and backslash
        # (Windows) path separators, null bytes that can truncate C-level
        # path parsing, and leading dots that could target hidden
        # per-directory files outside the intended session namespace.
        #
        # The colon is rejected for Windows, where it is not an ordinary
        # character: ``WindowsPath("…/sessions") / "D:foo.jsonl"`` evaluates to
        # ``D:foo.jsonl`` — a DRIVE-RELATIVE path that silently escapes the
        # sessions directory entirely (verified; POSIX joins it literally and is
        # unaffected). It also spells an NTFS alternate data stream
        # (``file:stream``). A dashboard slot key never contains a colon: the
        # transport prefix is stripped by the caller before this point, and
        # ``_normalize_slot_key`` folds the key to ``[\\w\\-.]`` anyway.
        return []
    sess_dir = config_dir() / "sessions"
    if not sess_dir.exists():
        return []
    # Match the resolution order used by slack/interactions.py when
    # linking Slack threads to existing sessions: bare stem first, then
    # the ``dashboard_`` prefix fallback for dashboard slots. Cron sessions
    # persist under different names: ``history._safe_key`` folds ``:`` to
    # ``_``, so ``cron:{id}`` writes ``cron_{id}.jsonl`` and its linked
    # dashboard slot ``dashboard:cron-{id}`` writes ``dashboard_cron-{id}.jsonl``.
    # Probe those too so an idle-evicted cron session is recognised rather
    # than misclassified as forged.
    candidates = [sess_dir / f"{slot_name}.jsonl"]
    if not slot_name.startswith("dashboard_"):
        candidates.append(sess_dir / f"dashboard_{slot_name}.jsonl")
    candidates.append(sess_dir / f"cron_{slot_name}.jsonl")
    candidates.append(sess_dir / f"dashboard_cron-{slot_name}.jsonl")
    return [p for p in candidates if p.exists()]


def _persisted_session_path(slot_name: str) -> "Path | None":
    """First existing transcript for *slot_name*, or None.

    Existence only. When more than one candidate exists the answer is still
    "yes, a session exists" — which is all the establish-vs-forged check needs.
    Anything making an AUTHORIZATION decision must use
    :func:`_probe_persisted_session`, which refuses to guess between them.
    """
    matches = _persisted_session_paths(slot_name)
    return matches[0] if matches else None


def _session_has_persisted_history(slot_name: str) -> bool:
    """Return True iff the slot has a JSONL file in the data home's sessions/.

    A positive signal that the session was previously **established** — i.e.
    that the key belongs to a real session rather than being forged or stale.
    It says nothing about the session's ``memory_mode``: every mode writes its
    transcript to disk (``_save_slot_to_history`` has no ``memory_mode`` gate,
    by design, so incognito/temporary tabs still survive a reload). Callers
    gating *memory writes* must therefore consult
    :func:`_persisted_session_memory_mode` as well — file existence alone is
    not evidence that writes are permitted.

    Used by ``api_lessons_create`` (in ``handlers/cron.py``) to distinguish
    between:

    * A legitimate MCP subprocess whose in-memory slot was evicted by the
      idle-sweep loop (``session.py``'s 30-minute timeout) or archived by a
      tab close. The subprocess still holds the original
      ``KIROCREW_SESSION_KEY`` env var, so it keeps sending the same
      ``X-Session-Key``, but ``state._slots`` has moved on. Without this
      check such calls return HTTP 400 ``unknown session`` even though the
      user is actively typing in the thread.

    * A forged or stale key from a context that never had a real session
      backing it — which should continue to be rejected.

    Only checks existence, not contents. Authentication of the caller is
    still enforced by the ``X-Internal-Secret`` middleware upstream; this
    check only governs the *established vs forged* distinction.
    """
    return _persisted_session_path(slot_name) is not None


def _persisted_session_memory_mode(slot_name: str) -> str | None:
    """Return the ``memory_mode`` recorded in *slot_name*'s session metadata.

    Three distinct outcomes, and the distinction IS the security property:

    * ``"persistent"`` / ``"incognito"`` / ``"temporary"`` — read from the
      metadata line. A metadata line that parses but carries no ``memory_mode``
      is reported as ``"persistent"``: the field postdates the feature, so a
      valid header without it is genuinely a legacy persistent session.
    * ``None`` — **unknown**. No file, or no parseable metadata object as the
      first line. Callers gating memory writes MUST deny on ``None`` rather
      than treat it as persistent. Denying is safe: ``ConversationLog.append``
      writes the metadata line when it creates the file, before any message is
      appended, so a session file whose first line is not metadata was not
      produced by a normal session and is no evidence that writes are allowed.

    This is the recovery path for a session whose in-memory state is gone but
    whose transcript is still on disk. Both in-memory signals a restricted
    session normally carries are dropped when a tab is archived
    (``api_chat_slot_close`` removes the slot from ``state._slots`` *and*
    discards its key from ``state._restricted_keys``), while the transcript —
    including its ``memory_mode`` marker — persists. Without reading that
    marker back, an archived incognito session whose MCP subprocess is still
    alive presents as an ordinary established session and its memory writes
    are allowed.

    Only the FIRST line is consulted, and only ``_METADATA_HEAD_MAX_BYTES`` of
    it: a later ``_type: metadata`` object is message content, not the header,
    and must not be able to redefine the mode. Byte-bounding keeps an enormous
    or adversarial first line from pinning memory.

    Blocking file I/O — call from a worker thread, never on the event loop
    (AUTOSDE ``no-blocking-call-on-event-loop``); prefer
    :func:`_probe_persisted_session`. Deliberately uncached: a cache would need
    invalidation on every mode change and could itself go stale, which is the
    exact failure class this closes.
    """
    path = _persisted_session_path(slot_name)
    if path is None:
        return None
    return _read_memory_mode(path)


def _read_memory_mode(path: "Path") -> str | None:
    """Read the ``memory_mode`` out of *path*'s metadata line. See above."""
    try:
        with open(path, "rb") as f:
            head = f.read(_METADATA_HEAD_MAX_BYTES)
    except OSError:
        return None
    first, _sep, _rest = head.partition(b"\n")
    try:
        d = json.loads(first.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("_type") != "metadata":
        return None
    mode = d.get("memory_mode")
    if mode is None:
        # Valid header, field absent -> legacy persistent session.
        return "persistent"
    if not isinstance(mode, str):
        return None
    # Allowlist, not normalize-and-hope: an unrecognised value must read as
    # unknown so the caller fails closed. Case/whitespace matter because the
    # comparison downstream is set membership — `"incognito "` would lower() to
    # itself, miss INCOGNITO_MEMORY_MODES, and be treated as unrestricted. The
    # API validates this field on the way in, but a hand-edited or partially
    # written transcript is not bound by that.
    normalized = mode.strip().lower()
    if normalized not in VALID_MEMORY_MODES:
        return None
    return normalized


async def require_owner_dashboard_request(
    request: web.Request, operation: str
) -> web.Response | None:
    """Owner gate shared across dashboard handler modules.

    Returns ``None`` when the caller IS the dashboard owner, allowing the
    request to proceed.  Otherwise audits the denial via SEL (an enqueue —
    the singleton is warmed at startup, see ``sel.warm_sel_singleton``),
    checks for a stale pre-owner bootstrap subject (relabelling the denial
    to a 401), and falls back to a 403 with the standard ``owner_only`` code.

    Imports ``is_owner_dashboard_request`` and ``stale_owner_session_response``
    inside the function body to avoid a circular import: ``source_providers``
    imports chat-state helpers that reach back into sibling handler modules.
    """
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
    )

    if is_owner_dashboard_request(request):
        return None

    # SEL is warmed at gateway startup (sel.warm_sel_singleton), so this
    # ``log_api_access`` only enqueues to the writer thread — no thread hop
    # needed (#8608). Guarded because a FAILED warm leaves construction to
    # retry here and possibly raise.
    caller = str(request.get("user") or "unknown")
    try:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="non_owner_block",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for non-owner %s failed", operation, exc_info=True)

    # Deny decision made above; only the response label changes for a signed
    # pre-owner bootstrap subject (see stale_owner_session_response).
    return _owner_denial_response(request)


def _owner_denial_response(
    request: web.Request,
    error_message: str = "owner authorization required",
    error_code: str = "owner_only",
) -> web.Response:
    """Stale-session relabel + 403 denial -- the tail of every owner gate.

    Synchronous: ``stale_owner_session_response`` is a pure predicate over
    request attributes, so no I/O is involved.  Domain-specific wrappers that
    perform their own SEL/audit logging before reaching the denial response can
    call this directly instead of going through the full async
    ``require_owner_dashboard_request`` helper.

    Imports ``stale_owner_session_response`` inside the function body to avoid
    a circular import (same reason as the async helper above).
    """
    from kiro_crew.dashboard.handlers.source_providers import (
        stale_owner_session_response,
    )

    stale = stale_owner_session_response(request)
    if stale is not None:
        return stale
    return web.json_response(
        {"error": error_message, "code": error_code},
        status=403,
    )


def _probe_persisted_session(slot_name: str) -> tuple[bool, str | None]:
    """``(file_exists, memory_mode_or_None)`` for *slot_name*.

    Refuses to guess when the key is **ambiguous**. ``slot_name`` reaches this
    function with its transport namespace already stripped
    (``sk.split(":", 1)[-1]``), so one stem can match several real transcripts —
    e.g. a legacy Slack thread at ``<ts>.jsonl`` and an archived dashboard slot
    named after that same ts at ``dashboard_<ts>.jsonl``. Taking the first
    candidate would let a *persistent* file answer for an *incognito* session and
    permit the write. Existence stays true (a session really does exist), but the
    mode is reported as ``None`` = unknown, which the caller denies on.

    Blocking I/O: hand this to a worker thread from an async caller
    (``await asyncio.to_thread(_probe_persisted_session, slot_name)``). It is a
    single composed call so one thread hop covers the whole probe.
    """
    matches = _persisted_session_paths(slot_name)
    if not matches:
        return False, None
    if len(matches) > 1:
        return True, None
    return True, _read_memory_mode(matches[0])


# ── Optional-extra install advice ──
# Two handler modules need these: `core` for the [voice] extra behind
# Speech-to-Text, and `messaging` for the per-channel SDK extras ([feishu] ->
# lark-oapi, [teams] -> PyJWT, [whatsapp] -> neonize). They live here rather than
# in either one so neither handler module has to import the other.


def _pip_install_channel_available() -> bool:
    """True when ``<gateway python> -m pip install`` can plausibly succeed.

    Three environments make that command a guaranteed dead end, and surfacing
    it there recreates the press-and-nothing-changes failure this surface
    exists to avoid:

    - the desktop app's bundled interpreter (see
      :func:`platform_compat.is_bundled_interpreter`): pip may exist, but a
      pip install writes into the code-signed bundle — breaking launches and
      updates — and is discarded on every app update;
    - an interpreter without the ``pip`` module (uv tool installs, some
      pipx layouts);
    - a PEP 668 externally-managed interpreter (distro/brew pythons), where
      pip refuses to install. Checked only outside a venv: inside one, pip
      works and deliberately ignores the marker, so a venv returns True.

    Touches the filesystem (``find_spec``, then the marker file), so call it
    from a worker thread on an async path.
    """
    if platform_compat.is_bundled_interpreter():
        return False
    if importlib.util.find_spec("pip") is None:
        return False
    # PEP 668 applies to the environment pip would install into. Inside a venv
    # pip deliberately ignores the marker, and `sysconfig.get_path("stdlib")`
    # resolves to the BASE interpreter's directory — where distro/brew pythons
    # place it — so checking it from a venv would misfire on the recommended
    # install layout (venv on a Debian/Ubuntu/Homebrew python).
    if sys.prefix != sys.base_prefix:
        return True
    return not (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists()


def pip_extra_install_command(extra: str) -> str:
    """The command that installs *extra*'s dependencies into THIS gateway's python.

    Thin wrapper over :func:`kiro_crew.extras.pip_install_command`, which owns
    the two things that make this string correct: it names the extra's real
    distributions rather than ``kirocrew[extra]`` (this project is not on any
    index, so that form cannot resolve for anyone), and it spells out the
    interpreter so the install cannot land in a different environment than the
    one that has to import it.

    Empty for an extra this build does not declare -- callers already treat an
    empty command as "no install channel" and show the unsupported notice.
    """
    return extras.pip_install_command(extra)
