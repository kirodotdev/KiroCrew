"""The KiroCrew Governance Model — archetypes, ceiling, evaluator, loader.

This module is the **decoupled, extensible core** of the two-level governance
model (see ``docs/system-specs/modules/governance.md`` and the design doc).
Governance is defined at two levels resolved by a single rule — *the tightest
boundary wins*:

* **Level 1 — POLICY** (``GovernanceCeiling``): the enterprise security ceiling,
  loaded once at boot from a trust-root path the agent process does not own.
  Once present, the running app and its agent cannot weaken it.
* **Level 2 — PROFILE** (``Profile``): a per-surface / per-app / per-task scope
  that may only *narrow* what policy permits.

The effective permission for any item is ``policy ∩ profile``.

Design invariant — **four archetypes, one algebra each.**  Every governed
control is exactly one of ``ScopedRuleset`` (a named set, opt-in/opt-out),
``OrdinalControl`` (a value on a strictness scale), ``CapabilityGate`` (on/off +
named sub-sets) or ``ScopedMap`` (an allowlist of members each carrying a
posture).

**Why this is decoupled / reusable / extensible** (the explicit acceptance
criterion): the evaluator never branches on a scope *name*.  It composes and
queries controls purely through archetype polymorphism (every archetype exposes
``permits``/``query``), and the two domain-specific concerns that *do* vary —
how an item string matches a pattern (``_MATCHERS``) and how an ordinal's tiers
rank (``_ORDINAL_SCALES``) — live in **enforcer-owned registries**, never in the
policy/profile document.  Adding a new governed scope (a new MCP-server
grouping, a new chat transport, a new capability) is therefore a *data* change:
one row in ``SCOPE_CATALOG`` plus, at most, one matcher/scale registry entry.  No
change to ``resolve`` / ``compose`` / ``assert_governance_floor`` is ever
required — the test suite proves this by registering a synthetic scope and
resolving it with zero evaluator edits.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable

from kiro_crew.platform.context import PlatformCompositionError

logger = logging.getLogger(__name__)

# Where the enterprise security policy is read from.  Env wins so a managed
# fleet can point at a root-owned / read-only location without a package
# rebuild; the home file is the standalone operator's authoring location.  The
# companion-bundled resource (precedence step 2) is resolved separately by the
# caller that knows the active edition (see ``load_security_policy``).
_POLICY_ENV = "KIROCREW_SECURITY_POLICY"
_POLICY_HOME_PATH = Path.home() / ".kirocrew" / "security_policy.json"

# Schema version this loader understands.  A file declaring a different version
# fails closed rather than being parsed under guessed semantics.
POLICY_VERSION = 1

# Profile name schema (Appendix A): lowercase alnum + hyphen, alnum-initial.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# ScopedRuleset modes.
MODE_ALLOW = "allow"  # opt-in / closed set (deny-by-default)
MODE_DENY = "deny"  # opt-out / open set (allow-by-default)
_MODES = frozenset({MODE_ALLOW, MODE_DENY})


# ──────────────────────────────────────────────────────────────────────────
# Enforcer-owned registries (NEVER sourced from a policy/profile document)
# ──────────────────────────────────────────────────────────────────────────
# Keeping the strictness order and the match strategy in code — not in the
# governed files — is what makes the ceiling un-weakenable: a malicious profile
# cannot reorder "strict < off" to defeat a sandbox floor, nor redefine how a
# path glob matches.  Both registries are append-only by construction.

# Ordinal strictness scales, strictness-ASCENDING (loosest first → strictest
# last).  ``approval`` and ``sandbox`` are verified against the live enforcers
# (approval pipeline; ``sandbox.py`` level dirs off<standard<cc<strict).
_ORDINAL_SCALES: Dict[str, Tuple[str, ...]] = {
    "approval": ("yolo", "auto", "interactive"),
    "sandbox": ("off", "standard", "cc", "strict"),
}


def _match_identifier(item: str, pattern: str) -> bool:
    """Case-insensitive fnmatch for opaque identifiers (tools, apps, agents)."""
    return fnmatch.fnmatch(item.strip().casefold(), pattern.strip().casefold())


def _match_command(item: str, pattern: str) -> bool:
    """Case-sensitive fnmatch on a shell command body (deterministic per-OS).

    Uses ``fnmatchcase`` (not ``fnmatch``) so the decision does not depend on
    the host's filesystem case-folding — matching ``security``/``admission``.
    """
    return fnmatch.fnmatchcase(item, pattern)


def _match_path(item: str, pattern: str) -> bool:
    """Glob match for filesystem paths, expanding ``~`` / ``$HOME``.

    Case-sensitive (POSIX path semantics).  ``fnmatch``'s ``*`` already crosses
    directory separators, which is the documented (broad) behaviour for a
    deny-oriented read block; callers wanting precise segment semantics should
    prefer narrower patterns.

    Only the ITEM is normalized (``_norm_item``: expand → absolutize → lexically
    collapse ``.``/``..``); the PATTERN is only ``~``/``$VAR``-expanded and is
    matched verbatim.  Normalizing the item — never the pattern — does two jobs
    and avoids one trap:

    * a ``..`` traversal cannot satisfy an ALLOW-mode prefix:
      ``/home/u/ws/../.bashrc`` normalizes to ``/home/u/.bashrc`` and no longer
      fnmatches ``/home/u/ws/**`` (without this ``*`` spans the ``..`` and the
      write is wrongly PERMITTED though the OS resolves it outside the allow-list);
    * an agent-supplied RELATIVE item is anchored to an absolute form, so it can
      still match an absolute DENY glob (``../../etc/passwd`` cannot dodge
      ``/etc/**``) instead of silently failing to match and falling open;
    * the trap avoided: ``os.path.normpath`` must NOT touch the pattern — it treats
      ``*``/``**`` as ordinary segments and collapses an adjacent ``..`` against
      them (``/a/**/../b`` → ``/a/b``, silently dropping the ``**``), which would
      widen an allow / shrink a deny.  Matching the pattern verbatim preserves the
      operator's authored globs exactly.

    Normalization is purely lexical (no filesystem ``resolve()``), so it adds no
    I/O.  The relative-item anchor is the host process CWD (the same anchor the
    resolved ``is_sensitive_path`` keystone uses); it cannot perfectly reconstruct
    a backend's CWD, so that always-on keystone remains the authoritative,
    resolved block for the trust-root / credential dirs.

    LEXICAL-ONLY CONTRACT (no ``realpath``): matching does NOT resolve symlinks.
    A symlink that lexically sits inside an allow-prefix
    (``<allow>/link -> <secret>/key``) passes ``_match_path`` even though the OS
    write lands at ``<secret>/key`` outside the allow-list.  This is intentional:
    resolving would add filesystem I/O on every gate call and would refuse writes
    through symlinks an operator placed deliberately (a common workspace layout).
    The contract is therefore: **allow-mode prefixes are a lexical scoping aid,
    not a hardened sandbox against symlinks** — the resolved ``is_sensitive_path``
    keystone (which DOES ``resolve()``) is the authoritative guard for the
    trust-root / credential dirs, and an operator must not rely on an allow-mode
    prefix to confine writes in a directory that contains untrusted symlinks.
    See ``docs/system-specs/modules/governance.md`` → "Path matcher".
    """
    return fnmatch.fnmatchcase(_norm_item(item), _expand(pattern))


def _match_host(item: str, pattern: str) -> bool:
    """Case-insensitive fnmatch for network egress hosts / globs (DNS is CI)."""
    return fnmatch.fnmatch(item.strip().casefold(), pattern.strip().casefold())


def _match_mcp(item: str, pattern: str) -> bool:
    """Match an MCP reference where a server grant covers all its tools.

    ``item`` is the queried reference in canonical ``@server`` / ``@server/tool``
    form (convert a raw ``mcp__server__tool`` title with :func:`mcp_title_to_ref`
    first).  A ``@server`` pattern matches any ``@server/tool`` under it
    (server-level grant); ``@server/tool`` matches only itself.  A deny at either
    granularity is therefore final (handled by ``ScopedRuleset.permits`` in deny
    mode).  Comparison is case-insensitive on the server/tool identifiers.
    """
    it = item.strip().casefold()
    pat = pattern.strip().casefold()
    if it == pat:
        return True
    # A whole-server pattern (no '/') covers every tool under that server.
    if "/" not in pat:
        return it.startswith(pat + "/")
    return False


# Match-strategy registry.  A ScopedRuleset names its matcher; permits() looks
# it up here.  Adding a domain = (optionally) one new entry, not evaluator code.
_MATCHERS: Dict[str, Callable[[str, str], bool]] = {
    "identifier": _match_identifier,
    "command": _match_command,
    "path": _match_path,
    "host": _match_host,
    "mcp": _match_mcp,
}
_DEFAULT_MATCHER = "identifier"


def _expand(path_str: str) -> str:
    """Expand ``~`` and ``$VAR`` without touching the filesystem (no resolve())."""
    return os.path.expanduser(os.path.expandvars(path_str))


def _norm_item(path_str: str) -> str:
    """Expand, absolutize, then lexically collapse ``.``/``..`` in a queried item.

    Applied to the queried ITEM only (never a pattern — see ``_match_path``).
    Three steps, all purely lexical (no filesystem ``resolve()``, no I/O):

    1. ``_expand`` — ``~`` / ``$VAR`` substitution.
    2. ``os.path.abspath`` — anchor a relative path (e.g. ``../../etc/passwd`` or
       ``etc/passwd``) to the host CWD so it cannot dodge an absolute DENY glob by
       failing to match; ``abspath`` also runs ``normpath``, collapsing ``..``.
       An already-absolute path is unchanged except for the collapse.
    3. collapse a leading ``//`` to ``/`` — POSIX (4.13) leaves a path beginning
       with exactly two slashes implementation-defined, and ``normpath`` PRESERVES
       it (``normpath('//etc/passwd') == '//etc/passwd'``).  An item supplied as
       ``//etc/passwd`` would then not match a ``/etc/**`` deny even though the OS
       opens ``/etc/passwd`` — a deny bypass.  Collapse so the lexical form equals
       what the OS resolves.  A leading ``///+`` already normalizes
       to a single ``/``, so only the exact ``//`` case needs this.

    Collapsing ``..`` is what stops a traversal from satisfying an allow-prefix
    glob.  ``abspath`` cannot reconstruct an ACP backend's actual CWD, so it is a
    best-effort anchor; the resolved ``is_sensitive_path`` keystone remains the
    authoritative block for the trust-root / credential dirs.
    """
    p = os.path.abspath(_expand(path_str))
    if p.startswith("//") and not p.startswith("///"):
        p = p[1:]
    return p


def _reject_unknown_keys(d: Mapping[str, object], known: "set[str]", container: str) -> None:
    """Enforce ``additionalProperties:false`` on an archetype container.

    Raise ``PlatformCompositionError`` if *d* carries any key outside *known* so a
    typo is a validation error (the schema's documented intent) rather than a
    silently-dropped — and potentially dangerous — setting.
    """
    extra = set(d) - known
    if extra:
        raise PlatformCompositionError(
            f"{container} has unknown key(s) {sorted(extra)} (additionalProperties:false; "
            "a typo is rejected fail-closed)"
        )


_RUNNING_PREFIX = "Running: "
_READING_PREFIX = "Reading "


def classify_tool_title(tool_name: str) -> Tuple[Tuple[str, str], ...]:
    """Map a heterogeneous PreToolUse gate title to ``(scope, item)`` pairs.

    Returns ALL governed scope/item pairs the title must satisfy (the gate denies
    if ANY pair denies), or an empty tuple for a title the name gate does not
    govern.  Returning a tuple — rather than a single pair — closes the
    ambiguity in the UNPREFIXED case: different ACP backends deliver a bare title
    that may be either a command body OR a named tool, and a heuristic split
    misroutes one for the other (a single-token command escaping the commands
    ceiling, or a multi-word tool name mismatching the tools ceiling).  By
    checking BOTH scopes for an unprefixed title, neither ceiling is bypassed.

    Classification (deterministic, documented):

    * ``mcp__…`` → ``("mcp", "@server/tool")`` — the headline case.
    * ``Running: <cmd>`` → ``("commands", "<cmd>")``.
    * ``Reading <path>`` → ``("filesystem.read", "<path>")`` — kiro-cli's
      file-read tool title carries the path after the prefix, so the path
      ScopedRuleset (deny-oriented read block) applies at the name gate.  An
      ungoverned ``filesystem.read`` scope permits (the standalone default), so
      this is a no-op unless a policy/profile governs read paths.
    * unprefixed → BOTH ``("commands", <title>)`` and ``("tools", <title>)`` —
      whichever scope the operator actually governs applies; the other is a
      no-op (an ungoverned scope permits).

    Note on writes: kiro-cli's file-WRITE/edit title format is not a stable
    prefix the way ``Reading``/``Running:`` are, so ``filesystem.write`` is NOT
    classified from the title here — it is enforced from the real tool arguments
    (``raw_tool_params['path']`` + ``tool_kind == 'edit'``) via
    :func:`classify_tool_args`, which the gate consults alongside the title.
    """
    if tool_name.startswith("mcp__"):
        return (("mcp", mcp_title_to_ref(tool_name)),)
    if tool_name.startswith(_RUNNING_PREFIX):
        return (("commands", tool_name[len(_RUNNING_PREFIX) :]),)
    if tool_name.startswith(_READING_PREFIX):
        path = tool_name[len(_READING_PREFIX) :].strip()
        return (("filesystem.read", path),) if path else ()
    # Unprefixed + heterogeneous: evaluate against BOTH the commands and tools
    # ceilings so neither is bypassed regardless of which kind the title is.
    return (("commands", tool_name), ("tools", tool_name))


# ACP tool_kind values (acp/client.py): "execute" (Bash), "read" (fs_read),
# "edit" (fs_write/code), "fetch" (web_fetch).  The kind + raw params are the
# RELIABLE signal for path/URL scopes — the display title is backend-variable.
_KIND_READ = "read"
_KIND_EDIT = "edit"
_KIND_FETCH = "fetch"


def classify_tool_args(
    tool_kind: str, raw_params: Optional[Mapping[str, object]]
) -> Tuple[Tuple[str, str], ...]:
    """Map a tool's semantic ``kind`` + real arguments to ``(scope, item)`` pairs.

    This is the args-based companion to :func:`classify_tool_title`.  The display
    title is backend-variable and unreliable for extracting a path or URL, but the
    ACP event carries the tool's ``kind`` and ``raw_tool_params`` — the
    authoritative signal.  Used by the gate to enforce the path/host scopes that a
    title cannot carry:

    * ``kind == "edit"`` with a ``path`` → ``("filesystem.write", "<path>")``.
    * ``kind == "read"`` with a ``path`` → ``("filesystem.read", "<path>")``
      (redundant with the ``Reading`` title path, harmless — both must permit).
    * ``kind == "fetch"`` with a ``url`` → ``("network.egress", "<host>")`` —
      the host is extracted from the URL so the ``host`` matcher applies.

    **Empty/unknown ``kind`` fallback (the `kind` field is spec-OPTIONAL — some
    ACP backends omit it, so it arrives ``""``).**  When the kind is not one of
    the known fs/fetch kinds, we infer from the param SHAPE so an edit/fetch is
    still governed: a ``url``/``uri`` (and no shell ``command``) → egress; a
    ``path``/``file_path`` (and no shell ``command``) → BOTH read and write
    (we cannot tell read from write without the kind, so we apply both ceilings —
    an ungoverned one permits, so this only tightens and never misroutes a shell
    command, which carries ``command`` and is governed by the ``commands`` scope).

    Returns an empty tuple when the params carry no governed item (an ungoverned
    scope permits, so this only ever tightens).
    """
    if not raw_params or not isinstance(raw_params, Mapping):
        return ()
    pairs: list = []
    path = raw_params.get("path") or raw_params.get("file_path")
    url = raw_params.get("url") or raw_params.get("uri")
    has_command = bool(raw_params.get("command"))  # a shell tool → commands scope
    if tool_kind == _KIND_EDIT:
        if isinstance(path, str) and path:
            pairs.append(("filesystem.write", path))
    elif tool_kind == _KIND_READ:
        if isinstance(path, str) and path:
            pairs.append(("filesystem.read", path))
    elif tool_kind == _KIND_FETCH:
        if isinstance(url, str) and url:
            host = _url_host(url)
            if host:
                pairs.append(("network.egress", host))
    elif not has_command:
        # Unknown/empty kind and NOT a shell command: infer from param shape so
        # an edit/fetch is not silently ungoverned when the backend omits `kind`.
        if isinstance(url, str) and url:
            host = _url_host(url)
            if host:
                pairs.append(("network.egress", host))
        if isinstance(path, str) and path:
            # Can't distinguish read from write without the kind → apply both
            # ceilings (tightest-wins; an ungoverned scope permits).
            pairs.append(("filesystem.read", path))
            pairs.append(("filesystem.write", path))
    return tuple(pairs)


# Schemes that actually open a network connection to a host. A URL on any other
# scheme (mailto:, data:, javascript:, tel:, file:, blob:, …) contacts no host,
# so the egress gate must not derive a phantom host from it.
_NETWORK_SCHEMES = frozenset({"http", "https", "ws", "wss", "ftp", "ftps"})


def _looks_like_host(token: str) -> bool:
    """True if *token* looks like a network host (vs. a URI scheme word).

    A real ``host:port`` input (``example.com:8080``, ``localhost:3000``,
    ``127.0.0.1:9``) is mis-parsed by urlparse as ``scheme:path`` — the host
    becomes the "scheme".  A non-network URI (``tel:80``, ``gopher:1234``,
    ``mailto:443``) has a bare alphabetic-word scheme.  We distinguish by SHAPE
    rather than a denylist of every URI scheme: a hostname contains a ``.`` or a
    digit, or is exactly ``localhost``; a bare scheme word never does.  This is
    what stops a non-network scheme + numeric payload from yielding a phantom
    host without having to enumerate ``tel``/``gopher``/``sms``/… .
    """
    t = token.lower()
    return t == "localhost" or "." in t or any(c.isdigit() for c in t)


def _url_host(url: str) -> str:
    """Extract the host from a URL for the ``host`` matcher (no network I/O).

    Returns ``""`` for a URL that carries no network host, so a hostless URL is
    NOT mis-classified as egress to a phantom host the fetch would never contact.
    The cases, by shape of the input:

    * ``scheme://authority/…`` — a host only when ``scheme`` is a known NETWORK
      scheme (``http``/``https``/``ws``/``wss``/``ftp``/``ftps``).  ``file:///…``
      and any other scheme with an authority return ``""``.
    * no scheme (``host/path`` or ``//host/path``) — recover the authority via a
      ``//`` retry.
    * ``scheme:rest`` with NO ``://`` — EITHER a non-network URI
      (``mailto:``/``tel:``/``data:`` → no host) OR the scheme-less ``host:port``
      form that urlparse mis-reads as ``scheme:path`` (``example.com:8080`` →
      scheme ``example.com``, path ``8080``).  We retry as an authority ONLY when
      ``rest`` begins with a bare numeric port AND the parsed "scheme" looks like
      a host (``_looks_like_host``).  ``example.com:8080`` / ``localhost:3000`` →
      host; ``tel:80`` / ``mailto:443`` / ``gopher:1234`` → ``""`` (a bare-word
      URI scheme is never a hostname), so they cannot yield a phantom host.
    """
    from urllib.parse import urlparse

    try:
        s = url.strip()
        parsed = urlparse(s)
        netloc = parsed.netloc
        scheme = parsed.scheme.lower()
        if "://" in s:
            # Real scheme + authority: only a network scheme contacts a host.
            netloc = netloc if scheme in _NETWORK_SCHEMES else ""
        elif not scheme:
            # Scheme-less ``host/path`` (netloc empty → recover via ``//`` retry)
            # or protocol-relative ``//host/path`` (netloc already parsed → keep).
            netloc = netloc or urlparse("//" + s).netloc
        elif not netloc:
            # ``scheme:rest`` with no ``://``: the ``host:port`` form ONLY when
            # ``rest`` is a bare numeric port AND the parsed "scheme" looks like a
            # hostname.  ``tel:80``/``mailto:443``/``gopher:1234`` have a bare-word
            # scheme (not a host) so they stay hostless; ``example.com:8080`` /
            # ``localhost:3000`` have a host-shaped token and resolve.
            first_seg = parsed.path.split("/", 1)[0]
            host_port_form = first_seg.isdigit() and _looks_like_host(scheme)
            netloc = urlparse("//" + s).netloc if host_port_form else ""
    except Exception:
        return ""
    # Strip userinfo + port: ``user:pass@host:443`` → ``host``.
    host = netloc.rsplit("@", 1)[-1]
    if host.startswith("["):  # IPv6 literal [::1]:443
        return host[1 : host.index("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def mcp_title_to_ref(title: str) -> str:
    """Convert a gate tool title to the canonical ``@server`` / ``@server/tool``.

    The PreToolUse gate sees MCP tools as ``mcp__<server>__<tool>`` (the raw ACP
    title); the policy/profile grammar uses ``@server`` / ``@server/tool``.  This
    bridges the two so the ``mcp`` matcher operates on one canonical form.  A
    non-MCP title is returned unchanged.
    """
    if not title.startswith("mcp__"):
        return title
    rest = title[len("mcp__") :]
    # The kiro title format is ``mcp__<server>__<tool>`` where the SERVER name
    # may itself contain ``__`` (e.g. ``npm__playwright_mcp``) but the tool name
    # is a single identifier.  Split on the LAST ``__`` so the whole server name
    # is preserved — partition() (first ``__``) would mis-split a multi-segment
    # server and produce a ref that never matches a server-level mcp deny.
    server, sep, tool = rest.rpartition("__")
    return f"@{server}/{tool}" if sep else f"@{rest}"


def register_matcher(name: str, fn: Callable[[str, str], bool]) -> None:
    """Register an additional match strategy (extensibility seam, append-only).

    Raises if *name* is already registered with a different function so a typo
    cannot silently shadow a built-in matcher.
    """
    existing = _MATCHERS.get(name)
    if existing is not None and existing is not fn:
        raise ValueError(f"matcher {name!r} is already registered")
    _MATCHERS[name] = fn


# ──────────────────────────────────────────────────────────────────────────
# Decision record
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Decision:
    """The outcome of a governance query.

    ``rule`` and ``layer`` name *why* — used by the audit record (Phase 8) and
    by ``policy explain`` (Phase 10).  ``permitted`` is the only field callers
    must branch on.
    """

    permitted: bool
    reason: str
    rule: str = ""  # rule1-allow | rule1-deny | rule2-intersect | ordinal | gate | default
    layer: str = ""  # policy | profile | both | default


# A control that can answer "is this item permitted?" for ONE level.  Both
# ``ScopedRuleset`` and the composed ``_AndRuleset`` satisfy it — the evaluator
# only ever calls ``permits``, so it stays archetype-shape-agnostic.
@runtime_checkable
class RulesetLike(Protocol):
    def permits(self, item: str) -> Decision:  # pragma: no cover - protocol stub
        raise NotImplementedError


# ──────────────────────────────────────────────────────────────────────────
# Archetype 1 — ScopedRuleset
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScopedRuleset:
    """A set of named items the agent may use, defined opt-in or opt-out.

    ``mode == "allow"``: only ``allow`` is permitted (``deny`` ignored — Rule 1).
    ``mode == "deny"``: everything except ``deny`` is permitted.
    An absent/empty ``allow`` under allow-mode is the **empty set** (deny-all),
    never "unconstrained".
    """

    mode: str
    allow: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    matcher: str = _DEFAULT_MATCHER

    @staticmethod
    def from_dict(d: Mapping[str, object], *, matcher: str = _DEFAULT_MATCHER) -> "ScopedRuleset":
        # additionalProperties:false — a typo'd key (e.g. "deney" instead of
        # "deny") must be a validation error, NOT silently dropped.  The
        # dangerous case is a deny-list typo: it would otherwise become an empty
        # deny list = allow-everything.  Fail closed instead.
        _reject_unknown_keys(d, {"mode", "allow", "deny"}, "ScopedRuleset")
        mode = str(d.get("mode", "")).strip().lower()
        if mode not in _MODES:
            raise PlatformCompositionError(
                f"ScopedRuleset.mode must be 'allow' or 'deny', got {mode!r}"
            )
        if matcher not in _MATCHERS:
            raise PlatformCompositionError(f"unknown matcher {matcher!r} for ScopedRuleset")
        allow = _str_tuple(d.get("allow"))
        deny = _str_tuple(d.get("deny"))
        # Rule 1: in allow-mode the deny array is ignored — warn so an operator
        # who set both does not believe a dead deny entry is protecting anything.
        if mode == MODE_ALLOW and deny:
            logger.warning(
                "ScopedRuleset mode=allow ignores its deny=%r (Rule 1: allow beats deny); "
                "put hard bounds in policy, not a profile deny",
                list(deny),
            )
        return ScopedRuleset(mode=mode, allow=allow, deny=deny, matcher=matcher)

    def permits(self, item: str) -> Decision:
        """Rule 1 — within a single level."""
        match = _MATCHERS[self.matcher]
        if self.mode == MODE_ALLOW:
            ok = any(match(item, pat) for pat in self.allow)
            return Decision(
                ok,
                f"{item!r} {'in' if ok else 'not in'} allow-set",
                rule="rule1-allow",
            )
        # deny mode: permitted unless explicitly denied.
        hit = next((pat for pat in self.deny if match(item, pat)), None)
        if hit is not None:
            return Decision(False, f"{item!r} matches deny pattern {hit!r}", rule="rule1-deny")
        return Decision(True, f"{item!r} not denied", rule="rule1-deny")

    def compose(self, narrower: "RulesetLike") -> "RulesetLike":
        """Rule 2 / inheritance — intersect this (ceiling) with a *narrower* set.

        The result permits an item iff BOTH permit it.  Two deny-mode rulesets
        with the same matcher flatten faithfully to a single union ScopedRuleset;
        any other combination (allow∩allow, allow∩deny) cannot be flattened
        without losing glob semantics, so it is returned as an
        :class:`_AndRuleset` view that re-checks both at query time.  Callers
        treat the result as a :class:`RulesetLike`.
        """
        if (
            isinstance(narrower, ScopedRuleset)
            and self.mode == MODE_DENY
            and narrower.mode == MODE_DENY
            and self.matcher == narrower.matcher
        ):
            return ScopedRuleset(
                mode=MODE_DENY,
                deny=_dedup(self.deny + narrower.deny),  # union
                matcher=self.matcher,
            )
        return _AndRuleset(self, narrower)


@dataclass(frozen=True)
class _AndRuleset:
    """The AND of two rulesets; ``permits`` requires both to permit.

    Used when :meth:`ScopedRuleset.compose` cannot flatten to a single ruleset.
    Deliberately NOT a ``ScopedRuleset`` subclass — it satisfies
    :class:`RulesetLike` structurally, so the evaluator stays shape-agnostic.
    """

    ceiling: RulesetLike
    profile: RulesetLike

    def permits(self, item: str) -> Decision:
        c = self.ceiling.permits(item)
        if not c.permitted:
            return Decision(False, f"policy: {c.reason}", rule="rule2-intersect", layer="policy")
        p = self.profile.permits(item)
        if not p.permitted:
            return Decision(False, f"profile: {p.reason}", rule="rule2-intersect", layer="profile")
        return Decision(True, "permitted by both levels", rule="rule2-intersect", layer="both")


# ──────────────────────────────────────────────────────────────────────────
# Archetype 2 — OrdinalControl
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OrdinalControl:
    """A single value on an enforcer-owned strictness scale (e.g. sandbox tier).

    The scale name selects the order in ``_ORDINAL_SCALES``; the value itself
    must be a member of that scale.  Composition is strictest-of.
    """

    scale: str
    value: str

    def __post_init__(self) -> None:
        scale = _ORDINAL_SCALES.get(self.scale)
        if scale is None:
            raise PlatformCompositionError(f"unknown ordinal scale {self.scale!r}")
        if self.value not in scale:
            raise PlatformCompositionError(
                f"ordinal value {self.value!r} not in scale {self.scale!r} {scale}"
            )

    def rank(self) -> int:
        return _ORDINAL_SCALES[self.scale].index(self.value)

    def compose(self, narrower: "OrdinalControl") -> "OrdinalControl":
        """strictest-of(self, narrower).  A looser profile does NOT win here.

        Two layers enforce the ceiling: at BOOT, ``assert_governance_floor`` (wired
        via ``governance_profiles.assert_profiles_within_ceiling`` in
        ``bootstrap_context``) *rejects* a profile looser than the ceiling and
        aborts startup fail-closed; at RUNTIME, this method takes the stricter of
        the two so a stricter profile is honoured and a looser one can never
        downgrade the effective value (defense in depth even if a profile is
        hot-reloaded after boot).
        """
        if narrower.scale != self.scale:
            raise PlatformCompositionError(
                f"cannot compose ordinals on different scales: "
                f"{self.scale!r} vs {narrower.scale!r}"
            )
        return self if self.rank() >= narrower.rank() else narrower

    def is_at_least_as_strict_as(self, ceiling: "OrdinalControl") -> bool:
        return self.rank() >= ceiling.rank()


# ──────────────────────────────────────────────────────────────────────────
# Archetype 3 — CapabilityGate
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CapabilityGate:
    """An on/off capability, optionally bounded by named ScopedRuleset scopes.

    ``enabled`` composes by AND across levels (either side off → off).  Each
    entry in ``scopes`` is an orthogonal ruleset (e.g. spawn → {agents,
    cwd_roots}); all survive composition independently.
    """

    enabled: bool
    scopes: Mapping[str, RulesetLike] = field(default_factory=dict)

    @staticmethod
    def from_dict(
        d: Mapping[str, object],
        *,
        default_enabled: bool,
        scope_matchers: Optional[Mapping[str, str]] = None,
    ) -> "CapabilityGate":
        _reject_unknown_keys(d, {"enabled", "scopes"}, "CapabilityGate")
        raw_scopes = d.get("scopes") or {}
        if not isinstance(raw_scopes, dict):
            raise PlatformCompositionError("CapabilityGate.scopes must be an object")
        matchers = scope_matchers or {}
        scopes: Dict[str, RulesetLike] = {
            str(k): ScopedRuleset.from_dict(v, matcher=matchers.get(str(k), _DEFAULT_MATCHER))
            for k, v in raw_scopes.items()
            if isinstance(v, dict)
        }
        enabled = d.get("enabled")
        return CapabilityGate(
            enabled=bool(enabled) if enabled is not None else default_enabled,
            scopes=scopes,
        )

    def compose(self, narrower: "CapabilityGate") -> "CapabilityGate":
        merged: Dict[str, RulesetLike] = dict(self.scopes)
        for name, ruleset in narrower.scopes.items():
            base = merged.get(name)
            if isinstance(base, ScopedRuleset):
                merged[name] = base.compose(ruleset)
            elif base is not None:
                merged[name] = _AndRuleset(base, ruleset)
            else:
                merged[name] = ruleset
        return CapabilityGate(enabled=self.enabled and narrower.enabled, scopes=merged)

    def permits_scope_item(self, scope_name: str, item: str) -> Decision:
        if not self.enabled:
            return Decision(False, "capability disabled", rule="gate", layer="both")
        ruleset = self.scopes.get(scope_name)
        if ruleset is None:
            return Decision(True, f"scope {scope_name!r} unconstrained", rule="gate")
        return ruleset.permits(item)


# ──────────────────────────────────────────────────────────────────────────
# Archetype 4 — ScopedMap
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScopedMap:
    """An allowlist of members where each member carries its own posture.

    ``members`` is a ruleset over member ids (which transports).  ``posture`` is
    per-member and **policy-only**: a profile carries ``members`` only, so an app
    can never widen a member's identity ceiling.
    """

    members: RulesetLike
    # member id -> {leaf name -> ScopedRuleset}
    posture: Mapping[str, Mapping[str, ScopedRuleset]] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: Mapping[str, object], *, allow_posture: bool) -> "ScopedMap":
        _reject_unknown_keys(d, {"members", "posture"}, "ScopedMap")
        raw_members = d.get("members")
        if not isinstance(raw_members, dict):
            raise PlatformCompositionError("ScopedMap.members is required and must be an object")
        members = ScopedRuleset.from_dict(raw_members, matcher="identifier")
        raw_posture = d.get("posture")
        if raw_posture and not allow_posture:
            # Rule 6: posture is policy-only; a profile carrying it is rejected.
            raise PlatformCompositionError("ScopedMap.posture is policy-only; not allowed here")
        posture: Dict[str, Dict[str, ScopedRuleset]] = {}
        if isinstance(raw_posture, dict):
            for member, leaves in raw_posture.items():
                if not isinstance(leaves, dict):
                    continue
                # Rule 6: a posture key is valid only if its member id is admitted
                # by ``members`` — an orphan posture entry is dead config and a
                # likely typo, so fail closed rather than carry it silently.
                if not members.permits(str(member)).permitted:
                    raise PlatformCompositionError(
                        f"ScopedMap.posture key {member!r} is not admitted by members; "
                        "a posture entry must name an allowed member"
                    )
                posture[str(member)] = {
                    str(leaf): ScopedRuleset.from_dict(val, matcher="identifier")
                    for leaf, val in leaves.items()
                    if isinstance(val, dict)
                }
        return ScopedMap(members=members, posture=posture)

    def compose(self, narrower: "ScopedMap") -> "ScopedMap":
        # members intersect; posture is policy-only so the ceiling's wins.
        base = self.members
        composed = base.compose(narrower.members) if isinstance(base, ScopedRuleset) else base
        return ScopedMap(members=composed, posture=self.posture)

    def permits_member(self, member: str) -> Decision:
        return self.members.permits(member)

    def posture_permits(self, member: str, leaf: str, item: str) -> Decision:
        member_posture = self.posture.get(member)
        if not member_posture:
            return Decision(True, f"no posture for member {member!r}", rule="scopedmap")
        ruleset = member_posture.get(leaf)
        if ruleset is None:
            return Decision(True, f"no posture leaf {leaf!r} for {member!r}", rule="scopedmap")
        return ruleset.permits(item)


# ──────────────────────────────────────────────────────────────────────────
# Scope catalog — names → archetype kind + matcher/scale binding
# ──────────────────────────────────────────────────────────────────────────
# This is the ONLY place a scope name is associated with an archetype.  The
# evaluator iterates this catalog; it never hard-codes "if scope == 'tools'".
# Extending the model = adding a row here + (maybe) a matcher/scale entry.
RULESET = "ruleset"
ORDINAL = "ordinal"
CAPABILITY = "capability"
SCOPEDMAP = "scopedmap"


@dataclass(frozen=True)
class ScopeSpec:
    """Catalog entry describing how a named governed scope is parsed + composed."""

    kind: str
    matcher: str = _DEFAULT_MATCHER
    ordinal_scale: str = ""
    capability_default: bool = False  # policy-absence default for CapabilityGate
    # for CapabilityGate: scope-name -> matcher for its inner ScopedRulesets
    scope_matchers: Mapping[str, str] = field(default_factory=dict)


# Built-in catalog.  ``capability_default`` follows Validation rule 8.
SCOPE_CATALOG: Dict[str, ScopeSpec] = {
    "tools": ScopeSpec(RULESET, matcher="identifier"),
    "mcp": ScopeSpec(RULESET, matcher="mcp"),
    "apps": ScopeSpec(RULESET, matcher="identifier"),
    "commands": ScopeSpec(RULESET, matcher="command"),
    "filesystem.read": ScopeSpec(RULESET, matcher="path"),
    "filesystem.write": ScopeSpec(RULESET, matcher="path"),
    "folders.read": ScopeSpec(RULESET, matcher="path"),
    "folders.write": ScopeSpec(RULESET, matcher="path"),
    "network.egress": ScopeSpec(RULESET, matcher="host"),
    "channels": ScopeSpec(SCOPEDMAP),
    "approval_mode": ScopeSpec(ORDINAL, ordinal_scale="approval"),
    "sandbox.min_level": ScopeSpec(ORDINAL, ordinal_scale="sandbox"),
    # Capabilities (Validation rule 8 registered defaults):
    "capabilities.spawn": ScopeSpec(
        CAPABILITY, capability_default=True, scope_matchers={"agents": "identifier"}
    ),
    "capabilities.memory_writes": ScopeSpec(CAPABILITY, capability_default=True),
    "capabilities.script_hooks": ScopeSpec(CAPABILITY, capability_default=False),
    "capabilities.cron": ScopeSpec(CAPABILITY, capability_default=False),
    "capabilities.messaging": ScopeSpec(CAPABILITY, capability_default=False),
    # Publishing an artifact's bytes to an external destination is an
    # exfil/external-side-effect surface (like messaging), so it is opt-in
    # (capability_default=False): when a policy governs capabilities.* but omits
    # publish, publishing is denied.  When NO policy is present the standalone
    # default still permits everything.  The inner ``destinations`` ruleset
    # bounds WHICH publish-provider ids are allowed once the capability is on
    # (the direct analogue of capabilities.spawn's ``agents`` ruleset).  WHO
    # implements a destination is the orthogonal CPP PublishRegistry seam; this
    # gate only decides WHETHER + to WHERE.  Not a lever over ``git push`` (deny
    # floor) or ``network.egress`` (fetch host) — those are separate planes.
    "capabilities.publish": ScopeSpec(
        CAPABILITY, capability_default=False, scope_matchers={"destinations": "identifier"}
    ),
}


def register_scope(name: str, spec: ScopeSpec) -> None:
    """Register a new governed scope (extensibility seam).

    Raises on a conflicting redefinition so a typo cannot shadow a built-in.
    """
    existing = SCOPE_CATALOG.get(name)
    if existing is not None and existing != spec:
        raise ValueError(f"scope {name!r} already registered with a different spec")
    if spec.ordinal_scale and spec.ordinal_scale not in _ORDINAL_SCALES:
        raise ValueError(f"scope {name!r} references unknown ordinal scale {spec.ordinal_scale!r}")
    if spec.matcher not in _MATCHERS:
        raise ValueError(f"scope {name!r} references unknown matcher {spec.matcher!r}")
    SCOPE_CATALOG[name] = spec


# ──────────────────────────────────────────────────────────────────────────
# Ceiling + Profile carriers
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BootControls:
    """Policy-only boot constraints (not a governed scope; checked at startup)."""

    require_sandbox: bool = True
    allow_terminal: bool = False
    fail_closed: bool = True


@dataclass(frozen=True)
class GovernanceCeiling:
    """Level 1 — the enterprise security ceiling, frozen at boot.

    ``controls`` maps a catalog scope name to its parsed archetype value.  A
    scope absent from the map is *not governed* by policy (truth-table
    "not-governed" rows).  This generic mapping is what lets the evaluator stay
    scope-name-agnostic.
    """

    version: int
    boot: BootControls
    controls: Mapping[str, object]
    identity_issuer: str = ""
    identity_signature: str = ""

    def get(self, scope: str) -> Optional[object]:
        return self.controls.get(scope)


@dataclass(frozen=True)
class Bind:
    """What a profile applies to (discriminated)."""

    type: str  # surface | app | task
    id: str = ""


@dataclass(frozen=True)
class Profile:
    """Level 2 — a per-surface / per-app / per-task narrowing scope."""

    name: str
    bind: Optional[Bind] = None
    extends: str = ""
    controls: Mapping[str, object] = field(default_factory=dict)

    def get(self, scope: str) -> Optional[object]:
        return self.controls.get(scope)


def deny_all_profile(name: str = "_deny_all") -> Profile:
    """The most-restrictive built-in profile.

    An invalid profile falls back to this (Validation rule 5) — NOT to the
    policy ceiling.  Deny-all everywhere it can: every ruleset scope is an empty
    allow-set; every capability is disabled.
    """
    controls: Dict[str, object] = {
        scope: ScopedRuleset(mode=MODE_ALLOW, allow=(), matcher=spec.matcher)
        for scope, spec in SCOPE_CATALOG.items()
        # Skip alias scopes (folders.* → filesystem.*): the gate queries the
        # canonical target, so emitting the alias key too is dead config.
        if spec.kind == RULESET and scope not in _SCOPE_ALIASES
    }
    controls["channels"] = ScopedMap(members=ScopedRuleset(mode=MODE_ALLOW, allow=()))
    for scope, spec in SCOPE_CATALOG.items():
        if spec.kind == CAPABILITY:
            controls[scope] = CapabilityGate(enabled=False)
    return Profile(name=name, controls=controls)


# ──────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────────────
def _str_tuple(value: object) -> Tuple[str, ...]:
    """Coerce an untrusted JSON value into a tuple of strings (fail-safe)."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


def _dedup(items: Tuple[str, ...]) -> Tuple[str, ...]:
    seen: Dict[str, None] = {}
    for it in items:
        seen.setdefault(it, None)
    return tuple(seen)


def _parse_control(scope: str, spec: ScopeSpec, raw: object, *, is_policy: bool) -> object:
    """Parse one raw JSON control value into its archetype, per the catalog."""
    if not isinstance(raw, dict):
        if spec.kind == ORDINAL and isinstance(raw, str):
            return OrdinalControl(scale=spec.ordinal_scale, value=raw)
        raise PlatformCompositionError(f"scope {scope!r} must be an object")
    if spec.kind == RULESET:
        return ScopedRuleset.from_dict(raw, matcher=spec.matcher)
    if spec.kind == ORDINAL:
        # Accept {"value": ...} / {"min_level": ...} / {"mode": ...}; a bare
        # string value is handled above.
        value = raw.get("value") or raw.get("min_level") or raw.get("mode")
        if not isinstance(value, str):
            raise PlatformCompositionError(f"ordinal scope {scope!r} needs a string value")
        return OrdinalControl(scale=spec.ordinal_scale, value=value)
    if spec.kind == CAPABILITY:
        return CapabilityGate.from_dict(
            raw, default_enabled=spec.capability_default, scope_matchers=spec.scope_matchers
        )
    if spec.kind == SCOPEDMAP:
        return ScopedMap.from_dict(raw, allow_posture=is_policy)
    raise PlatformCompositionError(f"scope {scope!r} has unknown archetype {spec.kind!r}")


# Structural (non-governed) keys consumed by parse_policy/parse_profile, not as
# governed scopes.
_STRUCTURAL_KEYS = frozenset(
    {"version", "boot", "identity", "name", "bind", "extends", "description"}
)


# Top-level keys that are "key-open" namespaces: every child becomes a
# ``<key>.<child>`` catalog scope (the enforcer registry, not the loader, decides
# which are known — an unknown child fails closed).  ``capabilities`` is the
# built-in example; the companion can register e.g. ``vault.*`` and have it parse
# WITHOUT editing this loader (the extensibility contract).
_KEY_OPEN_NAMESPACES = frozenset({"capabilities"})

# ``sandbox`` carries the ordinal ``min_level`` plus non-governed boot flags
# (require_isolation, env_scrub_prefixes) kept raw under a reserved scope.
_SANDBOX_FLAGS_SCOPE = "sandbox._flags"

# Scope aliases: the Pippin doc names the profile's path scopes ``folders.read``/
# ``folders.write`` but the policy names them ``filesystem.read``/``.write`` (App.
# A.3 note + the worked example). They are the SAME path ceiling — both resolve
# through the ``path`` matcher and the gate queries ``filesystem.*`` — so a
# ``folders.*`` key is normalized to its ``filesystem.*`` scope at parse time.
# Without this, a profile's ``folders.write`` would land in a separate control
# key and silently fail to narrow the ``filesystem.write`` the gate evaluates.
_SCOPE_ALIASES: Dict[str, str] = {
    "folders.read": "filesystem.read",
    "folders.write": "filesystem.write",
}


def _parse_controls(data: Mapping[str, object], *, is_policy: bool) -> Dict[str, object]:
    """Flatten a policy/profile JSON body into the catalog's dotted-scope map.

    Data-driven against ``SCOPE_CATALOG`` so a newly ``register_scope``'d family
    parses with no loader edit (the extensibility contract):

    * a top-level key that is itself a flat catalog scope is taken directly;
    * a nested namespace (``<key>.<sub>`` exists in the catalog) descends
      generically — every child ``<sub>`` that resolves to a catalog scope is
      parsed, and an unknown child fails closed;
    * ``capabilities`` (and any registered key-open namespace) maps each child to
      ``<key>.<child>``, deferring the known/unknown decision to the catalog.

    An unknown governed key fails closed (tamper-evidence / Rule 8).
    """
    controls: Dict[str, object] = {}
    # Precompute, per top-level key, the set of dotted children in the catalog.
    nested_children: Dict[str, set] = {}
    for scope in SCOPE_CATALOG:
        head, sep, tail = scope.partition(".")
        if sep and not tail.startswith("_"):
            nested_children.setdefault(head, set()).add(tail)

    def take(scope: str, raw: object) -> None:
        # Normalize an alias (folders.* → filesystem.*) so it lands in the SAME
        # control key the gate queries, and a profile's folders.* actually narrows
        # the policy's filesystem.* ceiling.
        scope = _SCOPE_ALIASES.get(scope, scope)
        spec = SCOPE_CATALOG.get(scope)
        if spec is None:
            raise PlatformCompositionError(f"unknown governed scope {scope!r} (fail-closed)")
        if scope in controls:
            # Both folders.* and filesystem.* present (or a dup) → intersect so
            # neither silently wins; keeps the narrow-only invariant.
            existing = controls[scope]
            new = _parse_control(scope, spec, raw, is_policy=is_policy)
            controls[scope] = _compose_controls(existing, new)
        else:
            controls[scope] = _parse_control(scope, spec, raw, is_policy=is_policy)

    for key, raw in data.items():
        if key in _STRUCTURAL_KEYS:
            continue
        if key in SCOPE_CATALOG:
            # A flat top-level scope (e.g. tools, mcp, commands, channels).
            take(key, raw)
        elif key in _KEY_OPEN_NAMESPACES and isinstance(raw, dict):
            # Every child is a catalog scope; unknown child fails closed in take().
            for child, child_raw in raw.items():
                take(f"{key}.{child}", child_raw)
        elif key in nested_children and isinstance(raw, dict):
            # A fixed-child namespace (filesystem, folders, network, sandbox, …).
            for sub, sub_raw in raw.items():
                dotted = f"{key}.{sub}"
                if dotted in SCOPE_CATALOG:
                    take(dotted, sub_raw)
                elif key == "sandbox":
                    # Non-governed boot flags ride raw under a reserved scope.
                    controls.setdefault(_SANDBOX_FLAGS_SCOPE, {})  # type: ignore[arg-type]
                    controls[_SANDBOX_FLAGS_SCOPE][sub] = sub_raw  # type: ignore[index]
                else:
                    raise PlatformCompositionError(f"unknown governed key {dotted!r} (fail-closed)")
        else:
            raise PlatformCompositionError(f"unknown governed key {key!r} (fail-closed)")
    return controls


# ──────────────────────────────────────────────────────────────────────────
# Loader
# ──────────────────────────────────────────────────────────────────────────
def parse_policy(data: Mapping[str, object]) -> GovernanceCeiling:
    """Parse a policy mapping into a frozen ``GovernanceCeiling``.

    Fails closed (raises ``PlatformCompositionError``) on any structural problem
    so a malformed-but-present policy never silently degrades to ungoverned.
    """
    version = data.get("version")
    if version != POLICY_VERSION:
        raise PlatformCompositionError(
            f"security policy version {version!r} unsupported (expected {POLICY_VERSION})"
        )
    boot_raw = data.get("boot")
    if not isinstance(boot_raw, dict):
        raise PlatformCompositionError("security policy requires a 'boot' object")
    boot = BootControls(
        require_sandbox=bool(boot_raw.get("require_sandbox", True)),
        allow_terminal=bool(boot_raw.get("allow_terminal", False)),
        fail_closed=bool(boot_raw.get("fail_closed", True)),
    )
    controls = _parse_controls(data, is_policy=True)
    identity = data.get("identity") or {}
    issuer = str(identity.get("issuer", "")) if isinstance(identity, dict) else ""
    signature = str(identity.get("signature", "")) if isinstance(identity, dict) else ""
    return GovernanceCeiling(
        version=POLICY_VERSION,
        boot=boot,
        controls=controls,
        identity_issuer=issuer,
        identity_signature=signature,
    )


def parse_profile(data: Mapping[str, object]) -> Profile:
    """Parse a profile mapping into a frozen ``Profile`` (narrow-only).

    Raises ``PlatformCompositionError`` on a structural problem; callers that
    must not abort (the per-surface profile loader) catch this and fall back to
    :func:`deny_all_profile` (Validation rule 5).
    """
    name = str(data.get("name", "")).strip()
    if not name:
        raise PlatformCompositionError("profile requires a 'name'")
    # Schema: name MUST match ^[a-z0-9][a-z0-9-]*$ (Appendix A).  A non-conforming
    # name is a schema-invalid profile → the loader turns this raise into the
    # most-restrictive deny-all built-in (Validation rule 5), never the ceiling.
    if not _PROFILE_NAME_RE.match(name):
        raise PlatformCompositionError(f"profile name {name!r} must match ^[a-z0-9][a-z0-9-]*$")
    bind: Optional[Bind] = None
    raw_bind = data.get("bind")
    if isinstance(raw_bind, dict):
        btype = str(raw_bind.get("type", "")).strip()
        if btype not in ("surface", "app", "task"):
            raise PlatformCompositionError(
                f"profile bind.type must be surface|app|task, got {btype!r}"
            )
        bind = Bind(type=btype, id=str(raw_bind.get("id", "")))
    controls = _parse_controls(data, is_policy=False)
    return Profile(
        name=name,
        bind=bind,
        extends=str(data.get("extends", "")),
        controls=controls,
    )


def _read_json_file(path: Path) -> Dict[str, object]:
    """Read + JSON-parse a governance file.  Raises on any failure (fail-closed)."""
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise PlatformCompositionError(f"governance file {path} is not a JSON object")
    return data


def load_security_policy(
    *, bundled_loader: Optional[Callable[[], Optional[Mapping[str, object]]]] = None
) -> Optional[GovernanceCeiling]:
    """Load the enterprise security policy, or ``None`` for editable defaults.

    Precedence (first present wins):

    1. ``KIROCREW_SECURITY_POLICY`` env path — fleet hot-override, highest.
    2. ``bundled_loader()`` — the companion-bundled resource, supplied by the
       caller when the active edition is ``amazon`` (Phase 9 packages it via
       ``importlib.resources``).  The public core passes ``None`` here.
    3. ``~/.kirocrew/security_policy.json`` — standalone operator-authored.
    4. None → editable secure-defaults (standalone, ungoverned ceiling).

    A **present-but-unreadable / invalid** policy at the env or home path raises
    ``PlatformCompositionError`` (fail-closed to strictest), mirroring
    ``admission.load_admission_policy`` — a fleet that meant to enforce something
    must never silently fall open.  The bundled loader is trusted (same author,
    covered by the admission signature) so its parse errors also raise.
    """
    raw_env = os.environ.get(_POLICY_ENV, "").strip()
    if raw_env:
        path = Path(raw_env)
        try:
            data = _read_json_file(path)
        except Exception as exc:  # fail-closed: a fleet pointed here on purpose.
            raise PlatformCompositionError(
                f"security policy at {path} (from {_POLICY_ENV}) is unreadable: {exc}"
            ) from exc
        return parse_policy(data)

    if bundled_loader is not None:
        bundled = bundled_loader()
        if bundled is not None:
            return parse_policy(bundled)

    if _POLICY_HOME_PATH.exists():
        try:
            data = _read_json_file(_POLICY_HOME_PATH)
        except Exception as exc:
            raise PlatformCompositionError(
                f"security policy at {_POLICY_HOME_PATH} is unreadable: {exc}"
            ) from exc
        return parse_policy(data)

    return None


# ──────────────────────────────────────────────────────────────────────────
# Evaluator (Phase 4) — pure functions, no I/O
# ──────────────────────────────────────────────────────────────────────────
# The runtime resolution path.  It NEVER pre-merges policy and profile into one
# object; it queries each level independently and ANDs (Rule 2), so the engine
# is identical for every scope and never branches on a scope name.

_PERMIT_NOT_GOVERNED = Decision(True, "not governed", rule="default", layer="default")


def _query_level(control: object, scope: str, item: str) -> Decision:
    """Query ONE level's control for *item* in *scope* (Rule 1).

    A ``None`` control means the level does not govern this scope → permit
    (truth-table "not-governed" rows).  Dispatch is by archetype, not scope
    name.  ``scope`` is used only to locate the sub-leaf for the structured
    archetypes (capabilities.<cap>.<inner>, channels member).
    """
    if control is None:
        return _PERMIT_NOT_GOVERNED
    if isinstance(control, (ScopedRuleset, _AndRuleset)):
        return control.permits(item)
    if isinstance(control, CapabilityGate):
        # scope form "capabilities.<cap>" governs the gate itself; an item names
        # an inner scope element as "<inner>:<value>" (e.g. "agents:researcher").
        inner, sep, value = item.partition(":")
        if sep:
            return control.permits_scope_item(inner, value)
        return (
            Decision(True, "capability enabled", rule="gate")
            if control.enabled
            else Decision(False, "capability disabled", rule="gate")
        )
    if isinstance(control, ScopedMap):
        # item is a member id, or "member/leaf:value" for a posture query.
        member, sep, rest = item.partition("/")
        if not sep:
            return control.permits_member(item)
        leaf, _, value = rest.partition(":")
        return control.posture_permits(member, leaf, value)
    if isinstance(control, OrdinalControl):
        # Ordinals are not queried per-item; resolve_ordinal handles them.
        return Decision(True, "ordinal not item-queried", rule="ordinal")
    raise PlatformCompositionError(f"scope {scope!r} carries unknown control {type(control)!r}")


def resolve(
    ceiling: Optional[GovernanceCeiling],
    profile: Optional[Profile],
    scope: str,
    item: str,
) -> Decision:
    """Resolve whether *item* is permitted in *scope* — ``policy ∩ profile``.

    Rule 2: an item is effectively permitted only if BOTH levels permit it.
    Policy is the hard ceiling; the profile can only further restrict.  Either
    level being ``None`` (or not governing the scope) contributes a permit, so
    ``resolve(None, None, …)`` permits everything (the ungoverned standalone
    default) and a policy-deny is final regardless of the profile.
    """
    policy_control = ceiling.get(scope) if ceiling is not None else None
    policy_dec = _query_level(policy_control, scope, item)
    if not policy_dec.permitted:
        return Decision(
            False, f"policy denies: {policy_dec.reason}", rule=policy_dec.rule, layer="policy"
        )

    profile_control = profile.get(scope) if profile is not None else None
    profile_dec = _query_level(profile_control, scope, item)
    if not profile_dec.permitted:
        return Decision(
            False, f"profile denies: {profile_dec.reason}", rule=profile_dec.rule, layer="profile"
        )

    layer = (
        "both"
        if (policy_control is not None and profile_control is not None)
        else (
            "policy"
            if policy_control is not None
            else "profile" if profile_control is not None else "default"
        )
    )
    return Decision(True, "permitted", rule="rule2-intersect", layer=layer)


def gate_decision(
    ceiling: Optional[GovernanceCeiling],
    profile: Optional[Profile],
    tool_title: str,
    *,
    tool_kind: str = "",
    raw_params: Optional[Mapping[str, object]] = None,
) -> Decision:
    """Resolve a PreToolUse gate title against the governance ceiling ∩ profile.

    Classifies the (heterogeneous) title to its governed scope + item, then
    ``resolve``s it.  When ``tool_kind`` / ``raw_params`` are supplied (the ACP
    event carries them), the real arguments are ALSO classified
    (:func:`classify_tool_args`) so path/host scopes the title cannot carry —
    ``filesystem.write`` (edit path), ``network.egress`` (fetch host) — are
    enforced at the same gate.  A title/args pair the gate does not govern is
    permitted here — an ungoverned scope permits.  When BOTH levels are
    ungoverned the result permits (the standalone default), so a host with no
    policy + no profile behaves exactly as today.
    """
    pairs = list(classify_tool_title(tool_title))
    pairs.extend(classify_tool_args(tool_kind, raw_params))
    if not pairs:
        return Decision(True, "title not name-gate-governed", rule="default")
    # Deny if ANY governed scope the title/args map to denies it (the unprefixed
    # case maps to both commands+tools; an ungoverned scope permits, so this only
    # tightens).  Return the first denial for a precise audit reason.
    for scope, item in pairs:
        decision = resolve(ceiling, profile, scope, item)
        if not decision.permitted:
            return decision
    return Decision(True, "permitted by all mapped scopes", rule="rule2-intersect")


def resolve_ordinal(
    ceiling: Optional[GovernanceCeiling],
    profile: Optional[Profile],
    scope: str,
) -> Optional[OrdinalControl]:
    """Resolve the effective ordinal value = strictest-of(policy, profile).

    Returns ``None`` when neither level governs the ordinal (caller keeps its
    own default).  Used by Phase 7 to clamp ``approval_mode`` / ``sandbox`` —
    the result is a floor: the enforcer must run at least this strict.
    """
    pol = ceiling.get(scope) if ceiling is not None else None
    pro = profile.get(scope) if profile is not None else None
    if pol is not None and not isinstance(pol, OrdinalControl):
        raise PlatformCompositionError(f"scope {scope!r} policy value is not an ordinal")
    if pro is not None and not isinstance(pro, OrdinalControl):
        raise PlatformCompositionError(f"scope {scope!r} profile value is not an ordinal")
    if pol is not None and pro is not None:
        return pol.compose(pro)
    return pol or pro


def assert_governance_floor(
    ceiling: Optional[GovernanceCeiling], profile: Optional[Profile]
) -> None:
    """Boot-time guard: reject a profile that would *weaken* the ceiling.

    For every ORDINAL the ceiling governs, the profile (if it also governs it)
    must be **equal-or-stricter** — a looser profile value aborts boot
    (fail-closed).  Set-archetypes (ScopedRuleset/CapabilityGate/ScopedMap) can
    only ever narrow under ``resolve`` (intersection / AND), so they cannot
    weaken the ceiling by construction and need no per-item boot proof; the
    ordinal scales are the only place a profile could pick a *looser* value, so
    they are the floor check — mirroring ``assert_security_floor``'s role for the
    deny floor.

    A ``None`` ceiling (standalone, ungoverned) imposes no floor.
    """
    if ceiling is None or profile is None:
        return
    for scope, spec in SCOPE_CATALOG.items():
        if spec.kind != ORDINAL:
            continue
        pol = ceiling.get(scope)
        pro = profile.get(scope)
        if isinstance(pol, OrdinalControl) and isinstance(pro, OrdinalControl):
            if not pro.is_at_least_as_strict_as(pol):
                raise PlatformCompositionError(
                    f"governance floor violated: profile {profile.name!r} sets "
                    f"{scope}={pro.value!r} which is looser than policy ceiling "
                    f"{pol.value!r} (scale {spec.ordinal_scale!r})"
                )


def assert_governance_paths_protected() -> None:
    """Boot guard: the governance trust-root files must be on the sensitive floor.

    The keystone of "secure by default, not by mandate" is that the agent cannot
    WRITE the policy/profile files — enforced solely by their presence in
    ``security._SENSITIVE_HOME_DIRS`` (the shared read+write gate).
    ``assert_security_floor`` covers the deny-pattern floor but NOT the
    sensitive-path list, so this is an explicit, independent boot check: if a
    refactor ever drops these entries the integrity guarantee silently
    evaporates, so we fail closed at boot instead.
    """
    # Deferred import: keep ``security`` (heavy regex stack) off this module's
    # import path; only the boot check needs it.
    from kiro_crew import security

    required = (
        ".kirocrew/security_policy.json",
        ".kirocrew/profiles",
        ".kirocrew/admission_policy.json",
    )
    sensitive = set(security._SENSITIVE_HOME_DIRS)  # noqa: SLF001 — boot integrity check
    missing = [p for p in required if p not in sensitive]
    if missing:
        raise PlatformCompositionError(
            "governance integrity violated: trust-root paths missing from the "
            f"sensitive-path floor {missing!r}; the agent could rewrite its own "
            "ceiling. Restore them in security._SENSITIVE_HOME_DIRS."
        )


def compose_profiles(parent: Profile, child: Profile) -> Profile:
    """Inheritance — a child profile that ``extends`` *parent* may only narrow.

    Produces a merged ``Profile`` whose every control is ``parent ∘ child``
    (allow∩, deny∪, ordinal=stricter, gate=AND).  Used when a profile declares
    ``extends`` (Validation rule 4, monotonic narrowing).  The child's bind/name
    win; controls present in only one parent carry through unchanged.
    """
    merged: Dict[str, object] = dict(parent.controls)
    for scope, c_control in child.controls.items():
        p_control = merged.get(scope)
        if p_control is None:
            merged[scope] = c_control
            continue
        merged[scope] = _compose_controls(p_control, c_control)
    return Profile(name=child.name, bind=child.bind or parent.bind, extends="", controls=merged)


def _compose_controls(ceiling: object, narrower: object) -> object:
    """AND two controls of the same archetype (dispatch by type, not scope)."""
    if isinstance(ceiling, ScopedRuleset):
        return ceiling.compose(narrower)  # type: ignore[arg-type]
    if isinstance(ceiling, _AndRuleset):
        return _AndRuleset(ceiling, narrower)  # type: ignore[arg-type]
    if isinstance(ceiling, OrdinalControl) and isinstance(narrower, OrdinalControl):
        return ceiling.compose(narrower)
    if isinstance(ceiling, CapabilityGate) and isinstance(narrower, CapabilityGate):
        return ceiling.compose(narrower)
    if isinstance(ceiling, ScopedMap) and isinstance(narrower, ScopedMap):
        return ceiling.compose(narrower)
    # Archetype mismatch between parent and child for the same scope.
    raise PlatformCompositionError(
        f"cannot compose mismatched control types {type(ceiling)!r} / {type(narrower)!r}"
    )


__all__ = [
    "Decision",
    "RulesetLike",
    "ScopedRuleset",
    "OrdinalControl",
    "CapabilityGate",
    "ScopedMap",
    "ScopeSpec",
    "SCOPE_CATALOG",
    "GovernanceCeiling",
    "BootControls",
    "Profile",
    "Bind",
    "POLICY_VERSION",
    "MODE_ALLOW",
    "MODE_DENY",
    "register_scope",
    "register_matcher",
    "mcp_title_to_ref",
    "classify_tool_title",
    "classify_tool_args",
    "deny_all_profile",
    "parse_policy",
    "parse_profile",
    "load_security_policy",
    "resolve",
    "resolve_ordinal",
    "gate_decision",
    "assert_governance_floor",
    "assert_governance_paths_protected",
    "compose_profiles",
]
