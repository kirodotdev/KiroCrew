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
import hmac
import json
import logging
import math
import os
import re
import threading
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    Callable,
    Dict,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from kiro_crew.config.paths import config_dir
from kiro_crew.platform.admission import (
    canonical_signing_bytes,
    hmac_signature,
    policy_trust_root_path,
    read_policy_trust_root,
)
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_health import mark_governance_incident
from kiro_crew.platform.tool_paths import TARGET_PATH_KEYS, target_paths
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Where the enterprise security policy is read from.  Env wins so a managed
# fleet can point at a root-owned / read-only location without a package
# rebuild; the home file is the standalone operator's authoring location.  The
# companion-bundled resource (precedence step 2) is resolved separately by the
# caller that knows the active edition (see ``load_security_policy``).
_POLICY_ENV = "KIROCREW_SECURITY_POLICY"
_POLICY_HOME_LEAF = "security_policy.json"

# Duplicated from ``policy_distribution.POLICY_URL_ENV`` on purpose.  This module
# is the trust root and must not import the fetch engine at module load — that
# would pull urllib onto every import of the governance evaluator, and the engine
# imports back from here.  Naming the variable is not a behaviour, so the copy has
# nothing to drift into; ``test_governance_distribution.py`` pins them equal.
#: Ceiling on any ``distribution`` duration, in seconds. ``threading.TIMEOUT_MAX`` is what the
#: platform can wait for, and both consumers of these values hit it: ``Event.wait`` in the
#: refresher and the socket timeout in a fetch each raise OverflowError above it. ~292 years, so
#: it bounds typos rather than intentions.
MAX_DURATION_SECS = threading.TIMEOUT_MAX

_POLICY_DISTRIBUTION_URL_ENV = "KIROCREW_POLICY_URL"
#: Named here only so the declared-source refusal can point at it; the engine owns it.
_POLICY_DISTRIBUTION_HEADERS_ENV = "KIROCREW_POLICY_HEADERS"

# Duplicated from ``policy_distribution.CACHE_DIR_LEAF`` for the same reason, and
# pinned equal by the same test.  Named here because
# ``assert_governance_paths_protected`` is a BOOT check that must not import the
# fetch engine to know what it is asserting.
_POLICY_CACHE_LEAF = "policy_cache"

# Also duplicated from ``policy_distribution``, and pinned by the same test.  Named
# here because the tier's ENTRY condition has to know about cache-only mode: such a
# child is deliberately given no source, so a source-only gate would skip the tier.
_POLICY_DISTRIBUTION_CACHE_ONLY_ENV = "KIROCREW_POLICY_CACHE_ONLY"


def _policy_home_path() -> Path:
    """Resolve the standalone security-policy path lazily.

    Deferred (not a module-level ``config_dir()`` capture) so importing this
    trust-root module never fires ``config_dir()`` — and thus the one-time
    data-home migration — as an import side effect. The migration must happen
    only at the single chosen point (``ensure_data_home()`` in the CLI prologue);
    the platform layer is documented as side-effect-free load-bearing infra.
    Callers (and tests) go through this accessor rather than a captured constant.
    """
    return config_dir() / _POLICY_HOME_LEAF


# Schema version this loader understands.  A file declaring a different version
# fails closed rather than being parsed under guessed semantics.
POLICY_VERSION = 1

# Provenance of the loaded ceiling — what the loader actually PROVED, not what the
# document claims.  Reported verbatim by ``kirocrew policy show`` so an operator
# can tell an established issuer from a decorative one.
SIGNATURE_VERIFIED = "verified"  # signature present AND checked against a trust key
SIGNATURE_UNVERIFIED = "unverified"  # signature present but not proven (no key / bad sig)
SIGNATURE_UNSIGNED = "unsigned"  # no identity.signature in the document
SIGNATURE_UNCHECKED = "unchecked"  # parsed outside the loader (no trust root consulted)

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


CU_CLASS_OBSERVE = "observe"
CU_CLASS_MUTATE = "mutate"
CU_CLASS_POINTER = "pointer"
CU_CLASS_KEYBOARD = "keyboard"
CU_CLASS_TEXT_ENTRY = "text_entry"
CU_CLASS_CONTROL = "control"

# The MCP tool-name prefix every computer-use tool carries
# (``computer_click``…).  Stripped before a class lookup so ONE vocabulary works
# in a policy document whichever form the operator writes: ``click`` and
# ``computer_click`` are the same action, and a fleet that authored against the
# governance spec's bare names does not silently stop matching if a tool is
# renamed with/without the prefix.
_CU_ACTION_TOOL_PREFIX = "computer_"

# Enforcer-owned computer-use action -> semantic classes.  In CODE, never in a
# policy/profile document — the same invariant as ``_ORDINAL_SCALES``: a malicious
# profile must not be able to re-label ``type_text`` as an observation and slip
# past a read-only ceiling.  Append-only by construction; keys are the
# PREFIX-STRIPPED action names (see ``_cu_action_key``).
_CU_ACTION_CLASSES: Dict[str, Tuple[str, ...]] = {
    # Observation only — no input is synthesized into any window.
    "list_apps": (CU_CLASS_OBSERVE,),
    "get_state": (CU_CLASS_OBSERVE,),
    # Control plane: drops KiroCrew's OWN cached snapshots and touches no other
    # application, so it is neither observe nor mutate.  A read-only ceiling that
    # wants the model to be able to release its cache explicitly must therefore
    # allow ``["@observe", "@control"]`` (or leave ``end_turn`` ungated at the
    # dispatcher, which is legitimate — it is internal state, not automation).
    "end_turn": (CU_CLASS_CONTROL,),
    # Mutators — each synthesizes input into another application's window.
    "click": (CU_CLASS_MUTATE, CU_CLASS_POINTER),
    # A drag is pointer-only by nature: there is no accessibility action that
    # expresses "sweep from here to there", so unlike ``click`` it has no
    # element-addressed form and an ``@pointer`` deny removes it entirely.
    "drag": (CU_CLASS_MUTATE, CU_CLASS_POINTER),
    "scroll": (CU_CLASS_MUTATE, CU_CLASS_POINTER),
    "perform_action": (CU_CLASS_MUTATE, CU_CLASS_POINTER),
    "type_text": (CU_CLASS_MUTATE, CU_CLASS_KEYBOARD, CU_CLASS_TEXT_ENTRY),
    "press_key": (CU_CLASS_MUTATE, CU_CLASS_KEYBOARD),
    "set_value": (CU_CLASS_MUTATE, CU_CLASS_TEXT_ENTRY),
    # Reserved vocabulary: names the governance spec's copy-pasteable policies
    # use and names a v2 may ship.  Carried here so a fleet policy authored
    # against them is classified correctly the day the tool lands, rather than
    # silently falling to the ("mutate",) unknown default and breaking an
    # ``@observe`` allow-list.  Classifying a not-yet-shipped name is inert.
    "get_app_state": (CU_CLASS_OBSERVE,),
    "perform_secondary_action": (CU_CLASS_MUTATE, CU_CLASS_POINTER),
}
# An action absent from the table (a 10th tool a later build or a companion adds)
# is treated as MUTATING — the fail-closed default in BOTH directions: it can
# never satisfy an ``@observe`` allow-list, AND it IS caught by an ``@mutate``
# deny-list.
_CU_UNKNOWN_ACTION_CLASSES: Tuple[str, ...] = (CU_CLASS_MUTATE,)


def _cu_action_key(name: str) -> str:
    """Normalize an action name/pattern to its class-table key.

    Casefolds and strips the ``computer_`` MCP tool-name prefix so the raw title
    suffix (``computer_click``) and the bare action a policy document names
    (``click``) resolve to the same row.  Applied to the PATTERN too, so
    ``deny: ["computer_press_key"]`` and ``deny: ["press_key"]`` are equivalent
    and neither is a silent no-op.
    """
    key = name.strip().casefold()
    return key[len(_CU_ACTION_TOOL_PREFIX) :] if key.startswith(_CU_ACTION_TOOL_PREFIX) else key


def computer_use_action_classes(action: str) -> Tuple[str, ...]:
    """Public read of the code-owned class table for one computer-use *action*.

    The single source of truth every enforcement site must derive from — the
    Plane-B gate's "is this action mutating?" test reads THIS, never a private
    copy (the ``test_floor_derives_rank_from_ssot_not_private_table`` precedent).
    An unregistered action returns the fail-closed
    :data:`_CU_UNKNOWN_ACTION_CLASSES` (``("mutate",)``), so a tool added without
    a table row is treated as mutating everywhere at once.
    """
    return _CU_ACTION_CLASSES.get(_cu_action_key(action), _CU_UNKNOWN_ACTION_CLASSES)


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

# The computer-use MCP server key and the raw ACP title prefix its tools arrive
# under.  Named here (not in the feature package) because ``classify_tool_title``
# is the ONE evaluator-adjacent function that must know the prefix, and this
# module must not import a feature package (boot order: the policy loader runs
# before any feature import).
CU_MCP_SERVER = "kirocrew-computer"
_CU_TITLE_PREFIX = f"mcp__{CU_MCP_SERVER}__"


def is_computer_use_title(tool_name: str) -> bool:
    """True when *tool_name* is a computer-use MCP tool title.

    Shared by ``hooks.on_tool_call``'s approval clamp and read-only pair so the
    prefix test lives in exactly one place.
    """
    return tool_name.startswith(_CU_TITLE_PREFIX)


def computer_use_action_from_title(tool_name: str) -> str:
    """The bare action name behind a computer-use MCP title, or ``""``.

    ``mcp__kirocrew-computer__computer_click`` -> ``computer_click``.  Returns
    ``""`` for any other title so a caller can branch without a second prefix
    test.
    """
    return tool_name[len(_CU_TITLE_PREFIX) :] if is_computer_use_title(tool_name) else ""


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

# The accepted path spellings are OWNED by the shared low-level walk
# (``kiro_crew.platform.tool_paths.TARGET_PATH_KEYS``) so the governance
# intersection plane and the hooks sensitive-path keystone can never drift on
# which keys count as a target. Aliased here under the historic name for local
# readers; do NOT redefine the tuple.
_PATH_ARG_KEYS = TARGET_PATH_KEYS

# Synthetic, never-permittable item emitted for a filesystem scope when the
# bounded path walk TRUNCATED (see the truncation branch in
# ``classify_tool_args``).  It is deliberately not a real path: it contains a NUL
# byte and glob metacharacters, so no operator allow-list pattern
# (``allow: ['~/project/**']``) or literal can ever match it, while a deny-mode
# ceiling that governs the scope still binds on it.  The marker text names why it
# exists so a denial audit is self-explanatory.
_TRUNCATED_SCAN_ITEM = "\x00<governance:path-scan-truncated-unverifiable>"


def _tool_arg_paths(raw_params: Mapping[str, object]) -> Tuple[Tuple[str, ...], bool]:
    """Return every distinct, non-empty path carried under a supported alias,
    at ANY nesting depth, plus whether the bounded scan was TRUNCATED.

    Tool backends use all three spellings, sometimes in the same payload, and a
    batch-shaped tool buries its real targets inside an array argument (e.g.
    ``{"operations": [{"mode": "Line", "path": …}]}``).  Every value must be
    governed at every depth: choosing the first truthy alias would let a benign
    ``path`` mask a sensitive ``filePath`` (and a truthy non-string value could
    mask every later alias entirely), and reading only the TOP level would let a
    nested path escape the operator ceiling entirely — the reported bypass.

    Delegates to the shared, iterative, bounded walk so the extraction is
    IDENTICAL to the hooks keystone's (single source of truth).  The second
    element is the walk's ``truncated`` flag: the caller must not treat a
    truncated scan as "no governed target present" — see the truncation branch in
    :func:`classify_tool_args`.
    """
    found = target_paths(raw_params)
    return tuple(found), found.truncated


def classify_tool_args(
    tool_kind: str, raw_params: Optional[Mapping[str, object]]
) -> Tuple[Tuple[str, str], ...]:
    """Map a tool's semantic ``kind`` + real arguments to ``(scope, item)`` pairs.

    This is the args-based companion to :func:`classify_tool_title`.  The display
    title is backend-variable and unreliable for extracting a path or URL, but the
    ACP event carries the tool's ``kind`` and ``raw_tool_params`` — the
    authoritative signal.  Used by the gate to enforce the path/host scopes that a
    title cannot carry:

    * ``kind == "edit"`` with a path argument →
      ``("filesystem.write", "<path>")``.
    * ``kind == "read"`` with a path argument →
      ``("filesystem.read", "<path>")``
      (redundant with the ``Reading`` title path, harmless — both must permit).
    * ``kind == "fetch"`` with a ``url`` → ``("network.egress", "<host>")`` —
      the host is extracted from the URL so the ``host`` matcher applies.

    **Empty/unknown ``kind`` fallback (the `kind` field is spec-OPTIONAL — some
    ACP backends omit it, so it arrives ``""``).**  When the kind is not one of
    the known fs/fetch kinds, we infer from the param SHAPE so an edit/fetch is
    still governed: a ``url``/``uri`` (and no shell ``command``) → egress; a
    ``path``/``file_path``/``filePath`` (and no shell ``command``) → BOTH read and write
    (we cannot tell read from write without the kind, so we apply both ceilings —
    an ungoverned one permits, so this only tightens and never misroutes a shell
    command, which carries ``command`` and is governed by the ``commands`` scope).

    Returns an empty tuple when the params carry no governed item (an ungoverned
    scope permits, so this only ever tightens).

    **Truncated scan (open-question-2 policy on this permit-by-default plane).**
    The shared path walk is bounded (``_TARGET_PATH_MAX_PATHS`` /
    ``_TARGET_PATH_MAX_NODES``); a payload padded past those caps returns a
    ``truncated`` result whose path list may be INCOMPLETE.  On this plane a
    truncated scan that yielded no path must NOT silently reach the caller's
    permit-by-default ``if not pairs`` branch — that is exactly the fail-open the
    reported bypass exploits (bury the governed path past 10_000 nodes to escape
    the operator ceiling).  The keystone in ``hooks`` fails SAFE by hard-denying
    any truncated scan, but this plane is permit-by-default and must NOT
    blanket-deny an UNGOVERNED standalone host that happens to send a huge
    payload.  So we thread the needle (issue option (c)): on truncation we emit
    the filesystem scope(s) the ``tool_kind`` implies against a synthetic,
    never-permittable item (``_TRUNCATED_SCAN_ITEM``).  Result: an operator
    ceiling that governs that filesystem scope DENIES the unverifiable call
    (closing the fail-open), while an ungoverned scope still permits it (the
    standalone default is preserved, and no unrelated scope is touched).  The
    truncation is auditable via the synthetic item text in the denial reason.
    Rejected alternatives: (a) permit as before = keep the fail-open; (b) deny the
    whole call unconditionally = over-blocks ungoverned hosts and every unrelated
    scope.

    Precise semantics of the marker against the two ruleset modes (both correct):
    a PREFIX-BOUNDED ALLOW-mode ceiling (``allow: ['~/workspace/**']`` — confine
    to a workspace, the exact profile the reported bypass targets) does NOT match
    the synthetic item, so the truncated call is DENIED — the fail-open is closed.
    (A CATCH-ALL ALLOW pattern — ``**`` / ``/**`` / ``*`` — does match the marker
    because fnmatch ``*`` crosses separators, so it permits the truncated scan;
    that is not a bypass, since such a ceiling confines nothing and is
    unconstrained anyway.)  A DENY-mode
    ceiling that blocks only specific paths (``deny: ['~/secrets/**']``) permits
    the marker, because a targeted deny is not a general confinement and a partial
    scan cannot prove the buried path hit that one pattern; the always-on,
    resolved ``is_sensitive_path`` keystone in ``hooks`` (which hard-denies ANY
    truncated scan) remains the authoritative guard for the sensitive tiers there.
    """
    if not raw_params or not isinstance(raw_params, Mapping):
        return ()
    pairs: list = []
    paths, paths_truncated = _tool_arg_paths(raw_params)
    url = raw_params.get("url") or raw_params.get("uri")
    has_command = bool(raw_params.get("command"))  # a shell tool → commands scope
    if tool_kind == _KIND_EDIT:
        for path in paths:
            pairs.append(("filesystem.write", path))
        if paths_truncated:
            pairs.append(("filesystem.write", _TRUNCATED_SCAN_ITEM))
    elif tool_kind == _KIND_READ:
        for path in paths:
            pairs.append(("filesystem.read", path))
        if paths_truncated:
            pairs.append(("filesystem.read", _TRUNCATED_SCAN_ITEM))
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
        for path in paths:
            # Can't distinguish read from write without the kind → apply both
            # ceilings (tightest-wins; an ungoverned scope permits).
            pairs.append(("filesystem.read", path))
            pairs.append(("filesystem.write", path))
        if paths_truncated:
            # Unknown kind → we cannot tell read from write, so a truncated scan
            # must consult BOTH filesystem ceilings against the never-permittable
            # marker (see the truncated-scan policy in the docstring).
            pairs.append(("filesystem.read", _TRUNCATED_SCAN_ITEM))
            pairs.append(("filesystem.write", _TRUNCATED_SCAN_ITEM))
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
    # The governed item this outcome is ABOUT. One gate query can carry several
    # identities for a single call (a prose title plus a trusted tool name plus an
    # MCP reference), so a denial has to say WHICH one it denied or the audit
    # record names the wrong subject. Empty when the caller asked about one item
    # and already knows it.
    item: str = ""


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
        if "enabled" not in d:
            enabled_flag = default_enabled
        else:
            enabled = d["enabled"]
            if not isinstance(enabled, bool):
                raise PlatformCompositionError(
                    f"CapabilityGate.enabled must be a boolean, got {enabled!r}"
                )
            enabled_flag = enabled
        return CapabilityGate(
            enabled=enabled_flag,
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

# SCOPE_CATALOG key whose deny-mode deny list is force-pinned (un-opt-out-able).
COMMANDS_SCOPE = "commands"


@dataclass(frozen=True)
class ScopeSpec:
    """Catalog entry describing how a named governed scope is parsed + composed."""

    kind: str
    matcher: str = _DEFAULT_MATCHER
    ordinal_scale: str = ""
    capability_default: bool = False  # see the CAPABILITY-DEFAULT CONTRACT note below
    # for CapabilityGate: scope-name -> matcher for its inner ScopedRulesets
    scope_matchers: Mapping[str, str] = field(default_factory=dict)


# ── CAPABILITY-DEFAULT CONTRACT (read before touching any capability_default) ──
#
# ``capability_default`` supplies ``enabled`` when the capability's KEY IS PRESENT
# in the document but its ``enabled`` field is omitted, e.g.
#
#     "capabilities": {"publish": {"scopes": {"destinations": {...}}}}
#
# which configures publish's destinations without stating whether publish is on,
# and therefore resolves to this default.  That is the ONLY case it covers.
#
# It does NOT apply when the key is absent.  ``_parse_controls`` materializes only
# the children physically present in the document, so an unnamed capability is an
# absent scope, and ``resolve`` PERMITS an absent scope (``_PERMIT_NOT_GOVERNED``)
# exactly as it does for an unnamed ``mcp``, ``commands`` or ``network.egress``.
# Omission means ungoverned-and-permitted for every archetype; capabilities are
# not a special case, and making them one would put a namespace-specific rule in
# a loader whose whole contract is that a newly registered scope family parses
# without a loader edit.
#
# Consequence for authors, and the reason a governed fleet should enumerate all of
# them: a policy cannot deny a capability by leaving it out.  ``kirocrew policy
# validate`` reports a partially-governed ``capabilities`` block so the gap is
# visible instead of implied.
#
# Verified 2026-08-11 by executing parse_policy + resolve; several comments below
# previously described the absent-key case and were wrong.

# Built-in catalog.
#
# APPEND-ONLY CONTRACT: add a row, or retire one — never RENAME a scope in place.
# Two things depend on it. A policy naming a retired scope fails closed at boot, so
# a rename is a visible migration on the ceiling; and a PROFILE naming an
# unregistered ``capabilities.*`` child that declares ``enabled: true`` is
# deliberately TOLERATED (skipped + recorded on ``Profile.unknown_scopes``, see
# ``_parse_controls``) so a data home shared with an edition that registers extra
# rows still loads. A rename would land in that tolerant path and silently stop
# governing, instead of surfacing. (A renamed row declared ``enabled: false`` still
# fails closed — the asymmetry is documented on ``_parse_controls``.)
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
    # Admin control over the auto-approve (YOLO) durations a user may select.
    # Members are duration selections. Two are security-relevant: "permanent"
    # (the never-expiring grant an operator DECLARES via
    # ``agent.dangerouslySkipPermissions``, re-established every startup) and
    # "until_shutdown" (an ad-hoc grant with no timed expiry, cleared on restart).
    # Deny-by-default is open (standalone permits it), and an enterprise POLICY
    # can remove it, e.g.
    #   {"yolo_duration": {"mode": "deny", "deny": ["permanent", "until_shutdown"]}}
    # which forces a declared grant back onto the ordinary ad-hoc TTL. Consulted
    # by kiro_crew.safety_override at the activation seam, against the HOST
    # profile and fail-closed.
    "yolo_duration": ScopeSpec(RULESET, matcher="identifier"),
    # Which ACP harness a deployment may select (``agent.acp_backend``).
    #
    # Distinct from the selectable-backend REGISTRY in ``kiro_crew.acp_backends``:
    # that answers "what can this BUILD serve", which is a capability fact and is
    # not governable. This row answers "what may THIS DEPLOYMENT select", so a
    # managed fleet can qualify one harness and bound the rest.
    #
    # ADDITIVE over a floor, which is the semantics decision #6622 was blocked on:
    #   {"agent_backend": {"mode": "allow", "allow": ["claude"]}}
    # means "ALSO allow claude", not "only claude" — ``kiro`` stays selectable
    # because ``acp_backends.GOVERNANCE_FLOOR_BACKEND`` is never submitted to this
    # scope at all. The alternative reading (exclusive) can empty the set, and an
    # install with no startable harness cannot be recovered from the dashboard,
    # since the trust-root policy is the one file it may not write.
    #
    # Members are POLICY ids, not the code's spelling: ``kiro`` / ``kas`` /
    # ``claude`` (see ``acp_backends.POLICY_ID_BY_BACKEND``) — the kiro backend is
    # the empty string internally, which no identifier matcher can carry.
    #
    # Consulted by ``kiro_crew.agent_backend_governance`` at exactly ONE place: it
    # recomputes the ``acp_backends`` registry, from ``bootstrap_context`` at boot and
    # from ``policy_distribution.apply_ceiling`` on every runtime ceiling install.
    # Deliberately NOT consulted at provider construction — harness-parity H13 forbids
    # the Kiro construction path gaining a conditional in service of an adapter, and a
    # test asserts ``create_provider_factory`` contains no governance call. The
    # existing single gate ``resolve_selected_backend`` reads the narrowed registry, so
    # it degrades a denied persisted value with no second check.
    "agent_backend": ScopeSpec(RULESET, matcher="identifier"),
    # Capabilities (registered defaults — see the CAPABILITY-DEFAULT CONTRACT above):
    "capabilities.spawn": ScopeSpec(
        CAPABILITY, capability_default=True, scope_matchers={"agents": "identifier"}
    ),
    "capabilities.memory_writes": ScopeSpec(CAPABILITY, capability_default=True),
    # Web browsing (the ``browser`` MCP tool driving the native panel, and the
    # playwright-cli fallback it points at) is a governable egress surface: an
    # enterprise policy that denies it must stop the agent browsing on the
    # native path too, not only the shell ``commands`` path playwright-cli sits
    # behind. Default True (on) — like memory_writes, an ordinary capability
    # that is available unless a policy explicitly sets
    # ``{"capabilities": {"browse": {"enabled": false}}}``.
    "capabilities.browse": ScopeSpec(CAPABILITY, capability_default=True),
    "capabilities.script_hooks": ScopeSpec(CAPABILITY, capability_default=False),
    "capabilities.cron": ScopeSpec(CAPABILITY, capability_default=False),
    "capabilities.messaging": ScopeSpec(CAPABILITY, capability_default=False),
    # Agent workload identity + Gateway MCP (opt-in, like messaging/publish).
    # Inner ``posture`` is policy data (``workload`` | ``login``), not a second
    # scope and not an evaluator input. An ``enabled: true`` document with a
    # missing or unknown posture fails closed — treated as disabled, or
    # boot-abort when ``boot.fail_closed``. Data row only; CONTRACT_VERSION
    # and the evaluator are untouched.
    "capabilities.agentcore": ScopeSpec(CAPABILITY, capability_default=False),
    # Publishing an artifact's bytes to an external destination is an
    # exfil/external-side-effect surface (like messaging), so it is opt-in
    # (capability_default=False): a policy that names ``publish`` while omitting
    # ``enabled`` — typically to configure its destinations — resolves to denied.
    # Note this does NOT make omission of the whole key deny: an unnamed publish
    # is ungoverned and permitted (see the CAPABILITY-DEFAULT CONTRACT above).
    # The inner ``destinations`` ruleset
    # bounds WHICH publish-provider ids are allowed once the capability is on
    # (the direct analogue of capabilities.spawn's ``agents`` ruleset).  WHO
    # implements a destination is the orthogonal CPP PublishRegistry seam; this
    # gate only decides WHETHER + to WHERE.  Not a lever over ``git push`` (deny
    # floor) or ``network.egress`` (fetch host) — those are separate planes.
    "capabilities.publish": ScopeSpec(
        CAPABILITY, capability_default=False, scope_matchers={"destinations": "identifier"}
    ),
    # Installed theme packs may ship a persona.md whose (consent-gated) text is
    # injected into the agent's FIRST turn — third-party text entering the
    # model's context (mirrors capabilities.publish being a governable external
    # surface). Making it a capability row lets an enterprise POLICY
    # force-disable persona injection wholesale. Default True: naming the row
    # without ``enabled`` keeps personas working (this is a tone-only surface; the deny floor +
    # PreToolUse gate still bind every action the persona could ask for), while
    # a governing policy can pin it off. Data row only — CONTRACT_VERSION and
    # the evaluator are untouched (per spec).
    "capabilities.theme_persona": ScopeSpec(CAPABILITY, capability_default=True),
    # Installing a theme pack fetches third-party content server-side (a local
    # dir move or a ``git clone`` from a remote URL), stores it, and serves its
    # sandboxed JS + assets into the operator's dashboard — a content-ingestion
    # surface that, like capabilities.publish, an enterprise POLICY must be able
    # to close. Making install a capability row lets a managed-fleet ceiling
    # ban pack installation wholesale (consulted in ``api_themes_install``).
    # Default True: naming the row without ``enabled`` keeps install working for the
    # standalone single-user owner; a governing policy can pin it off. Data row only —
    # CONTRACT_VERSION and the evaluator are untouched (mirrors theme_persona).
    "capabilities.theme_install": ScopeSpec(CAPABILITY, capability_default=True),
    # The anonymous heartbeat (``beacon.py``) and official-app install receipt
    # (``apps/install_receipt.py``) are the repo's only default-on egress family.
    # Both use fixed anonymous payloads and the same effective consent gate. A
    # managed fleet frequently may not egress to a vendor endpoint at all — so
    # unlike the user-facing Settings toggle (which the agent and the operator can
    # both flip), an enterprise POLICY needs to pin it off in a way the running app
    # cannot undo. This row is what makes that possible: it is read from the
    # trust-root ``security_policy.json``, which sits in
    # ``security._SENSITIVE_HOME_DIRS``, so the agent can neither read nor rewrite
    # its own ceiling.
    #
    # Default True: naming the row without ``enabled`` keeps the documented default-on
    # behavior for the
    # standalone user, who still has the toggle, the CLI, and the env var. Consulted
    # at THREE chokepoints, because any one alone would be a half-control:
    #   * ``beacon.should_send`` — blocks the send itself (the actual egress);
    #   * the dashboard config PATCH allowlist — refuses a re-enable write with 403,
    #     so a pinned host cannot be left storing ``beacon_enabled: true`` and
    #     showing a toggle that does nothing;
    #   * ``kirocrew telemetry enable`` — exits 1 without writing, for the same
    #     reason as the PATCH refusal on a headless host.
    # Writing ``false`` stays allowed at both write chokepoints (tightest-wins).
    # POLICY LAYER ONLY: ``beacon.is_governance_pinned_off`` requires
    # ``layer == "policy"``, so a Level-2 profile pinning this row does NOT suppress
    # the beacon — the probe is process-wide and carries no session, so a
    # per-surface profile is not the question it answers.
    # Data row only — CONTRACT_VERSION and the evaluator are untouched (mirrors
    # theme_install).
    "capabilities.telemetry": ScopeSpec(CAPABILITY, capability_default=True),
    # Auto-deriving the dashboard's OWN tailnet origin (``dashboard/tailnet.py``,
    # RFC §4). When ``dashboard.tailscale.enabled`` is on, the gateway shells out
    # to the local ``tailscale`` daemon at startup and, on success, puts the
    # resulting MagicDNS name into the CSRF origin allowlist and the
    # DNS-rebinding ``Host`` barrier. A managed fleet frequently may not do
    # either half: some hosts must not invoke the tailnet CLI at all, and — the
    # sharper case — an origin set that only a fleet-controlled ``dashboard.url``
    # is allowed to widen must not be widenable by a name the app derived for
    # itself. The operator-facing switch is reachable from ``config.json``, the
    # ``config.local.json`` overlay and the CLI, so exactly like
    # ``capabilities.telemetry`` it is a control the running app (and its agent)
    # can lift; this row is the one it cannot, because it is read from the
    # trust-root ``security_policy.json``, which sits in
    # ``security._SENSITIVE_HOME_DIRS`` — the agent can neither read nor rewrite
    # its own ceiling.
    #
    # Default True: naming this row without ``enabled`` must keep TODAY's behaviour
    # byte-for-byte. The feature is already off by default in config, so a default
    # of False would deny tailnet derivation for a policy that mentioned the row
    # only to configure something about it. (An unnamed row is ungoverned and
    # permitted regardless of this default — see the CAPABILITY-DEFAULT CONTRACT
    # above.) An enterprise that wants it off says so.
    # Consulted at THREE chokepoints, because any one alone is a half-control:
    #   * ``tailnet.resolve_tailnet_host`` — returns "" so no origin is derived
    #     and the daemon is not even consulted (the action itself);
    #   * the dashboard config PATCH allowlist — refuses an ENABLING write with
    #     403, so a pinned host cannot be left storing ``enabled: true`` behind a
    #     control that does nothing;
    #   * ``kirocrew config set dashboard.tailscale.enabled true`` — exits 1
    #     without writing, for the same reason on a headless host.
    # Writing ``false`` stays allowed at both write chokepoints (tightest-wins).
    # POLICY LAYER ONLY: ``tailnet.is_governance_pinned_off`` requires
    # ``layer == "policy"``, so a Level-2 profile pinning this row does NOT
    # suppress the derivation — the probe runs once at gateway startup, is
    # process-wide and carries no session, so a per-surface profile is not the
    # question it answers.
    # Data row only — CONTRACT_VERSION and the evaluator are untouched (mirrors
    # telemetry).
    "capabilities.tailnet_origin": ScopeSpec(CAPABILITY, capability_default=True),
    # "Connect your phone": minting a live mobile session credential (tailnet QR
    # or one-time login link) is an auth-surface widening an enterprise POLICY
    # must be able to close wholesale or narrow per method. The ``methods``
    # ruleset binds on the MobileConnectMethod ids the CPP seam contributes
    # (mirrors capabilities.publish's ``destinations``); WHO implements a method
    # is the orthogonal MobileConnectProvider seam — this gate only decides
    # WHETHER + WHICH. Default True: naming the row without ``enabled`` keeps
    # the personal-install pair working; a governing policy can pin it off.
    # Enforced fail-closed at the methods listing AND at each mint endpoint
    # (the filtered list is presentation, never the control). Data row only —
    # CONTRACT_VERSION and the evaluator are untouched (mirrors publish).
    "capabilities.mobile_connect": ScopeSpec(
        CAPABILITY, capability_default=True, scope_matchers={"methods": "identifier"}
    ),
    # "Share as image": the dashboard turns an assistant reply into a branded
    # PNG card and offers a prefilled post to X / LinkedIn. The card itself is
    # rendered and exported in the browser (no upload — copy / download stay
    # local), but the intent buttons hand the reply's caption text to a
    # third-party site in a URL, so the feature is an egress path for agent
    # output that a managed fleet may forbid wholesale. There is no server-side
    # share action to refuse; the control is the dashboard entry, and the
    # dashboard learns whether to draw it from ``GET /api/dashboard/config``
    # (``social_share_enabled``), which resolves this row server-side so the
    # frontend never guesses. Default True: naming the row without ``enabled``
    # keeps the entry for the standalone user; a governing ceiling — policy or
    # a profile bound to the dashboard surface — withdraws it
    # (``dashboard/social_share.py``: evaluated on the pinned ``dashboard:ui``
    # surface through ``vet_and_audit``, fail-closed, mirroring the
    # mobile_connect listing). Data row only — CONTRACT_VERSION and the
    # evaluator are untouched.
    "capabilities.social_share": ScopeSpec(CAPABILITY, capability_default=True),
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


def _version_tuple(value: str) -> Tuple[int, ...]:
    """Parse a version's numeric core into a comparable tuple.

    The pre-release/build suffix is split off the WHOLE string before splitting
    on dots, because this project's CI stamps a dot INSIDE the pre-release
    (``0.2.0-nightly.20260728t184500``, ``0.3.0-insider.2``) — splitting
    per-component would leave ``nightly.20260728t184500`` as its own component
    and read every nightly as version ``0``.
    """
    core = re.split(r"[-+]", str(value).strip(), maxsplit=1)[0]
    try:
        return tuple(int(chunk) for chunk in core.split("."))
    except ValueError:
        return ()


@dataclass(frozen=True)
class UpdatePins:
    """Policy-only update pins: WHERE new code comes from, and the MINIMUM version.

    **Not a governed scope, deliberately.** Every archetype answers "is X
    permitted?"; a remote URL and a version number are *values the core
    consumes*, not permission decisions. So they ride outside ``controls`` — no
    ``SCOPE_CATALOG`` row, no matcher, no evaluator change, no new archetype.

    Read from the trust-root ``security_policy.json``, which sits in
    ``security._SENSITIVE_HOME_DIRS`` — that keystone (the agent can neither read
    nor write its own ceiling) is what makes these enterprise-*pinnable* where a
    ``config.json`` field or an env var would only be a suggestion.

    **Policy-only: rejected in a Level-2 profile** (see :func:`parse_profile`).
    Profiles are narrow-only, and there is no "narrower" version of pointing
    somewhere else — a per-app profile that could redirect the update source
    would be privilege escalation.
    """

    # The git remote new code may come from, as an fnmatch glob so one pin can
    # cover a mirror set. Empty = unpinned (the standalone default: any remote).
    source: str = ""
    # The minimum version this fleet may run. Empty = no floor.
    min_version: str = ""
    # The operator-authored shell commands for the command update provider. Their
    # PRESENCE is what enables the provider (there is no mechanism selector): when
    # either is set, resolve_provider() returns a CommandProvider. They live HERE
    # (in the keystone-protected security_policy.json the agent cannot write),
    # never in config.json, because a command provider executes unsandboxed code
    # as the gateway — placing its commands in an agent-writable file would let a
    # prompt-injected agent run arbitrary code. config.json has no path into this
    # seam at all.
    check_command: str = ""
    apply_command: str = ""
    # Per-platform command overrides, keyed by "{sys.platform}-{machine}"
    # (e.g. "linux-x86_64", "darwin-arm64"). Falls back to the top-level
    # check_command/apply_command when the current platform has no override.
    platform_commands: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def permits_source(self, url: str) -> bool:
        """Does *url* satisfy the source pin? An empty pin permits anything.

        An empty *url* (the checkout has no resolvable remote) DENIES when a pin
        exists: an admin's pin must not be satisfied by "we could not tell". So
        does a ``.``/``..`` component — ``*`` spans separators, so
        ``/srv/approved/../evil.git`` would otherwise match ``/srv/approved/*``.

        ``fnmatchcase``, not ``fnmatch``: the latter normcases via
        ``os.path.normcase``, a no-op on POSIX but lowercasing on Windows — the
        same pin must not be case-sensitive on one OS and not the other.
        """
        if not self.source:
            return True
        candidate = url.strip()
        if not candidate or any(
            part in (".", "..") for part in candidate.replace("\\", "/").split("/")
        ):
            return False
        return fnmatch.fnmatchcase(candidate, self.source.strip())

    def meets_min_version(self, version: str) -> bool:
        """Is *version* at or above the floor? An empty floor is always met.

        An unparseable floor imposes none (a typo must not brick a fleet); an
        unparseable *version* is treated as below the floor, which makes the host
        take the update rather than sit on a build it cannot identify.
        """
        floor = _version_tuple(self.min_version) if self.min_version else ()
        if not floor:
            return True
        current = _version_tuple(version)
        if not current:
            return False
        width = max(len(current), len(floor))
        return current + (0,) * (width - len(current)) >= floor + (0,) * (width - len(floor))

    @staticmethod
    def from_dict(d: Mapping[str, object]) -> "UpdatePins":
        _reject_unknown_keys(
            d,
            {
                "source",
                "min_version",
                "check_command",
                "apply_command",
                "platform_commands",
            },
            "updates",
        )
        for key in ("source", "min_version", "check_command", "apply_command"):
            # `str(raw or "")` would coerce `"source": false` to "" — silently
            # UNPINNING a policy that plainly meant to pin. Fail closed instead.
            if d.get(key) is not None and not isinstance(d[key], str):
                raise PlatformCompositionError(f"updates.{key} must be a string")
        raw_platform = d.get("platform_commands")
        platform_commands: dict[str, dict[str, str]] = {}
        if raw_platform is not None:
            if not isinstance(raw_platform, Mapping):
                raise PlatformCompositionError("updates.platform_commands must be a mapping")
            for plat_key, plat_val in raw_platform.items():
                if not isinstance(plat_key, str) or not isinstance(plat_val, Mapping):
                    raise PlatformCompositionError(
                        "updates.platform_commands entries must map a platform string "
                        "to a command mapping"
                    )
                entry: dict[str, str] = {}
                for cmd_key, cmd_val in plat_val.items():
                    if cmd_key not in ("check_command", "apply_command"):
                        raise PlatformCompositionError(
                            f"updates.platform_commands.{plat_key} has unknown key {cmd_key!r}"
                        )
                    if not isinstance(cmd_val, str):
                        raise PlatformCompositionError(
                            f"updates.platform_commands.{plat_key}.{cmd_key} must be a string"
                        )
                    # Stripped like source/min_version above: a whitespace-only
                    # command is truthy, so it would pass the presence check that
                    # SELECTS the provider, and `sh -c "   "` exits 0. That reads
                    # as a successful update, so the gateway restarts, the version
                    # has not changed, the check still reports an update, and the
                    # unattended path loops. Normalising here means absent and
                    # blank are the same thing everywhere downstream.
                    entry[cmd_key] = cmd_val.strip()
                platform_commands[plat_key] = entry
        return UpdatePins(
            source=str(d.get("source") or "").strip(),
            min_version=str(d.get("min_version") or "").strip(),
            check_command=str(d.get("check_command") or "").strip(),
            apply_command=str(d.get("apply_command") or "").strip(),
            platform_commands=platform_commands,
        )


def active_update_pins() -> UpdatePins:
    """The boot-frozen ceiling's update pins — empty (unpinned) when ungoverned.

    The single read point for the three update call sites, so they cannot drift.
    Returns unpinned on any error: a governance glitch must not block an update,
    because refusing one strands a host on a build that may need a patch.
    """
    try:
        from kiro_crew.platform.context import current_context

        pins = getattr(current_context().governance, "updates", None)
    except Exception:
        logger.debug("update pins unavailable; treating as unpinned", exc_info=True)
        return UpdatePins()
    return pins if isinstance(pins, UpdatePins) else UpdatePins()


# ``distribution.on_unavailable`` dispositions.  Two values, because there are only
# two questions: may this host run when the central ceiling cannot be established,
# and may it run on a cache older than the fleet's staleness bound.
UNAVAILABLE_FAIL_CLOSED = "fail_closed"  # abort boot / refuse the stale cache
UNAVAILABLE_DEGRADE = "degrade"  # fall through to the next tier / serve stale + audit
_UNAVAILABLE_DISPOSITIONS = frozenset({UNAVAILABLE_FAIL_CLOSED, UNAVAILABLE_DEGRADE})

#: Floor on ``refresh_interval_secs``.  A central endpoint serves the whole fleet,
#: so a typo'd `1` would turn every host into a polling loop against the admin's
#: own control plane.  A value below this is raised to it (with a warning) rather
#: than rejected: refusing would brick a fleet over a number that has a safe
#: reading, which is the opposite of what a distribution channel is for.
MIN_REFRESH_INTERVAL_SECS = 60

#: Default per-request timeout when the policy names none.  Bounded because this
#: runs on the boot path on a cold cache: an endpoint that accepts a connection
#: and never answers must not hang startup indefinitely.
DEFAULT_FETCH_TIMEOUT_SECS = 10.0

#: Hard ceiling on a fetched document.  A hostile or misconfigured endpoint must
#: not be able to OOM boot by answering with an unbounded body.
MAX_POLICY_BYTES = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class PolicyDistribution:
    """Policy-only central-distribution pins: WHERE the ceiling itself comes from.

    This is how an enterprise IT admin owns one document and has every machine in
    the fleet follow it: the admin publishes ``security_policy.json`` to a central
    location, and each host fetches it, caches the last-known-good copy, and
    re-fetches on an interval so a pushed change lands without a restart or a
    redeploy.  ``kiro_crew.platform.policy_distribution`` is the engine; this
    dataclass is only the parsed declaration.

    **Not a governed scope**, for the same reason :class:`UpdatePins` is not: every
    archetype answers "is X permitted?", while a URL and an interval are *values
    the core consumes*.  So it rides outside ``controls`` — no ``SCOPE_CATALOG``
    row, no matcher, no evaluator change.

    **Policy-only: rejected in a Level-2 profile** (see :func:`parse_profile`).  A
    profile is narrow-only and there is no narrower version of pointing somewhere
    else.  A per-app profile that could redirect where the ceiling is fetched from
    would not be a narrowing — it would be a total replacement of the enforcement
    document, which is the widest escalation in this model.

    **Provenance is not configured here either.**  There is no
    ``require_signature`` key: a document must not be the authority on whether it has
    to be authentic, which is the reason ``_policy_trust_settings`` already gives for
    keeping the flag out of ``security_policy.json`` — "an attacker rewriting the
    policy would simply clear it".  Mandating a verified signature is
    ``require_policy_signature`` in the operator-controlled admission policy, which is
    on the keystone and which a fetched document cannot reach; this tier honours it
    like every other tier.

    **No credentials live here.**  ``headers`` is deliberately absent: a document
    published to the whole fleet must not carry a per-machine secret, and this one
    is additionally copied into a local cache and reported on by the read-only
    policy viewer.  A request credential is per-machine configuration and comes
    from the ``KIROCREW_POLICY_HEADERS`` environment variable instead — the same
    channel that already carries ``KIROCREW_SECURITY_POLICY``.
    """

    #: The URL the ceiling is fetched from.  Empty = no central distribution (the
    #: default, so an existing policy is byte-identical in behaviour).  The scheme
    #: must be one a fetcher is registered for; the built-ins are ``https``,
    #: ``file``, and ``http`` restricted to loopback hosts.
    source: str = ""
    #: Seconds between background re-fetches.  0 = fetch at boot only, which is
    #: still centrally-managed but not "on the fly".  Raised to
    #: :data:`MIN_REFRESH_INTERVAL_SECS` when set lower.
    refresh_interval_secs: int = 0
    #: Per-request timeout.  0 = :data:`DEFAULT_FETCH_TIMEOUT_SECS`.
    timeout_secs: float = 0.0
    #: How old the cached copy may be before it stops being an acceptable answer.
    #: 0 = no bound (a reachable-once host keeps running forever on that copy).
    #: A positive value is the fleet's staleness ceiling: past it, the disposition
    #: below decides whether the host refuses to run or runs and reports.
    max_cache_age_secs: int = 0
    #: What to do when the central ceiling cannot be established — a cold cache and
    #: an unreachable source, or a cache past ``max_cache_age_secs``.
    #: :data:`UNAVAILABLE_FAIL_CLOSED` (the default) refuses; a fleet that pointed
    #: a host at a central ceiling meant that ceiling to bind, so "we could not
    #: tell" must not read as "run unbounded".  :data:`UNAVAILABLE_DEGRADE` falls
    #: through to the next precedence tier instead, recording a governance
    #: incident — for a fleet that would rather have a working host it can see is
    #: degraded than a host that will not start.
    on_unavailable: str = UNAVAILABLE_FAIL_CLOSED

    @property
    def enabled(self) -> bool:
        """Is central distribution configured at all?"""
        return bool(self.source)

    def effective_timeout(self) -> float:
        return self.timeout_secs if self.timeout_secs > 0 else DEFAULT_FETCH_TIMEOUT_SECS

    def effective_refresh_interval(self) -> int:
        """The refresh interval actually used, clamped to the polling floor."""
        if self.refresh_interval_secs <= 0:
            return 0
        return max(self.refresh_interval_secs, MIN_REFRESH_INTERVAL_SECS)

    def cache_too_old(self, age_secs: float) -> bool:
        """Has a cached copy of *age_secs* exceeded the fleet's staleness bound?

        A negative age (a clock that moved backwards between the write and the
        read) reads as fresh rather than as infinitely stale: a host must not
        refuse to boot because NTP stepped its clock.
        """
        if self.max_cache_age_secs <= 0:
            return False
        return age_secs > self.max_cache_age_secs

    @staticmethod
    def from_dict(d: Mapping[str, object]) -> "PolicyDistribution":
        _reject_unknown_keys(
            d,
            {
                "source",
                "refresh_interval_secs",
                "timeout_secs",
                "max_cache_age_secs",
                "on_unavailable",
            },
            "distribution",
        )
        raw_source = d.get("source")
        if raw_source is not None and not isinstance(raw_source, str):
            # `str(raw or "")` would coerce `"source": false` to "", silently
            # DISABLING central distribution on a policy that plainly meant to
            # configure it. Fail closed instead, exactly as ``updates`` does.
            raise PlatformCompositionError("distribution.source must be a string")
        source = str(raw_source or "").strip()
        # A DECLARED source may not carry a credential, and the two shapes that do are
        # userinfo and a query string. This block ends up in the policy cache VERBATIM --
        # the document has to be byte-identical for its signature to verify -- and that
        # file is readable by an app backend, which is arbitrary third-party code. So a
        # `https://user:pass@host/p.json` or a pre-signed `?X-Amz-Signature=...` placed
        # here would be published to every host and then handed to every app.
        #
        # This is the rule the module docstring already states -- the per-machine request
        # credential travels in `KIROCREW_POLICY_HEADERS`, which "a published document must
        # not" carry -- made enforceable rather than advisory. The ENVIRONMENT channel is
        # deliberately unrestricted: that is where a pre-signed URL belongs, it is set by
        # whatever provisions the host, and it never lands in the document.
        if source:
            try:
                parsed = urllib.parse.urlsplit(source)
            except ValueError as exc:
                # ``urlsplit`` raises on a malformed bracketed host ("https://[::1"), and a
                # policy is CONFIGURATION: an operator who mistyped the address must get a
                # composition error naming the key, not an uncaught ValueError traceback out
                # of boot. Refusing here also keeps a source that cannot be parsed from
                # reaching the engine at all.
                raise PlatformCompositionError(
                    f"distribution.source is not a parseable URL: {exc}"
                ) from exc
            # ``.port`` is a LAZILY parsed property, so the ``urlsplit`` guard above does not
            # cover it: it raises for a non-numeric or out-of-range port. A policy is
            # configuration, so that is a composition error naming the key, not a traceback.
            try:
                parsed.port
            except ValueError as exc:
                raise PlatformCompositionError(
                    f"distribution.source has an unusable port: {exc}"
                ) from exc
            offending = "userinfo" if (parsed.username or parsed.password) else ""
            if not offending and parsed.query:
                offending = "a query string"
            if offending:
                raise PlatformCompositionError(
                    f"distribution.source carries {offending}, which a published policy "
                    "must not: this document is cached verbatim and is readable by app "
                    f"backends. Put the address in {_POLICY_DISTRIBUTION_URL_ENV} and any "
                    f"credential in {_POLICY_DISTRIBUTION_HEADERS_ENV}, which stay "
                    "per-machine."
                )

        numbers: dict[str, float] = {}
        for key, allow_float in (
            ("refresh_interval_secs", False),
            ("timeout_secs", True),
            ("max_cache_age_secs", False),
        ):
            raw = d.get(key)
            if raw is None:
                numbers[key] = 0.0
                continue
            # bool is an int subclass, and `"refresh_interval_secs": true` would
            # otherwise parse as 1 second — a fleet-wide poll storm from a typo.
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise PlatformCompositionError(f"distribution.{key} must be a number")
            # NaN and infinity parse as JSON numbers in Python's decoder, and every
            # comparison below is FALSE for NaN — so it would slip past the range and
            # whole-number checks and then raise an uncaught ValueError at int().
            #
            # Only a FLOAT is asked: ``math.isfinite`` converts its argument to a float
            # first, so a JSON integer of 310 digits — which is a perfectly ordinary thing
            # for a typo to produce — raises OverflowError inside the very check meant to
            # reject it. An int has no non-finite values to screen for, and the range check
            # below handles it exactly.
            if isinstance(raw, float) and not math.isfinite(raw):
                raise PlatformCompositionError(f"distribution.{key} must be a finite number")
            if raw < 0:
                raise PlatformCompositionError(f"distribution.{key} must not be negative")
            if not allow_float and raw != int(raw):
                raise PlatformCompositionError(f"distribution.{key} must be a whole number")
            # Bounded by ``threading.TIMEOUT_MAX``, which is what the platform can actually
            # WAIT for: the refresher passes the interval to ``Event.wait`` and the fetch
            # passes the timeout to a socket, and both raise OverflowError above it —
            # silently killing the poller thread, so the host simply stops receiving policy
            # updates. Not an invented policy limit: it is ~292 years, so no duration anyone
            # means comes near it, and every value that does is a typo.
            if raw > MAX_DURATION_SECS:
                raise PlatformCompositionError(
                    f"distribution.{key} must not exceed {int(MAX_DURATION_SECS)} seconds "
                    "(the longest interval this platform can wait for)"
                )
            # Stored WITHOUT a float round-trip, for the same reason ``isfinite`` is not
            # asked of an int: ``float()`` on a 310-digit integer raises OverflowError, and
            # doing that here would move the crash three lines down rather than fix it.
            # Every consumer either takes ``int()`` of this or accepts a float, and an int
            # satisfies both.
            numbers[key] = raw

        raw_disposition = d.get("on_unavailable")
        if raw_disposition is None:
            disposition = UNAVAILABLE_FAIL_CLOSED
        elif not isinstance(raw_disposition, str):
            raise PlatformCompositionError("distribution.on_unavailable must be a string")
        else:
            disposition = raw_disposition.strip()
            if disposition not in _UNAVAILABLE_DISPOSITIONS:
                raise PlatformCompositionError(
                    f"distribution.on_unavailable {raw_disposition!r} must be one of "
                    f"{sorted(_UNAVAILABLE_DISPOSITIONS)}"
                )

        if (
            not source
            and not os.environ.get(_POLICY_DISTRIBUTION_URL_ENV, "").strip()
            and any(
                (
                    numbers["refresh_interval_secs"],
                    numbers["timeout_secs"],
                    numbers["max_cache_age_secs"],
                    raw_disposition is not None,
                )
            )
        ):
            # A block that tunes a fetch it never configures is a policy whose
            # author believed central distribution was on. Silently ignoring it is
            # how a fleet ends up ungoverned while its policy file reads as managed.
            #
            # Unless the ENVIRONMENT supplies the source, which is the ordinary split:
            # whatever provisions the host owns the address (and any credential in it),
            # while the fleet publishes the cadence and the staleness bound in the
            # document. Such a block is not inert at all, and refusing it here aborted
            # boot on exactly the configuration the two-channel design intends.
            raise PlatformCompositionError(
                "distribution declares settings but no 'source'; central policy "
                f"distribution would be inert (fail-closed). Set 'source', or "
                f"{_POLICY_DISTRIBUTION_URL_ENV} if the address is per-machine."
            )

        return PolicyDistribution(
            source=source,
            refresh_interval_secs=int(numbers["refresh_interval_secs"]),
            timeout_secs=numbers["timeout_secs"],
            max_cache_age_secs=int(numbers["max_cache_age_secs"]),
            on_unavailable=disposition,
        )


def active_policy_distribution() -> PolicyDistribution:
    """The installed ceiling's distribution pins — empty when ungoverned.

    The single read point so the refresher, the CLI and the policy viewer cannot
    drift.  Returns an unconfigured value on any error, and deliberately reads the
    INSTALLED context rather than resolving one: this is called from a background
    refresh thread, where composing a context as a side effect of asking "where do
    I fetch from" would be a boot-order surprise.
    """
    try:
        from kiro_crew.platform.context import installed_context

        ctx = installed_context()
        pins = getattr(ctx.governance, "distribution", None) if ctx is not None else None
    except Exception:
        logger.debug("policy distribution pins unavailable", exc_info=True)
        return PolicyDistribution()
    return pins if isinstance(pins, PolicyDistribution) else PolicyDistribution()


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
    # Outcome of the load-time signature check (:func:`load_security_policy`).
    # ``parse_policy`` alone cannot fill this in — it has the document but not the
    # trust root — so a directly-parsed ceiling carries the honest
    # ``SIGNATURE_UNCHECKED``, never a flattering default.
    signature_state: str = "unchecked"
    # Policy-only update pins (outside ``controls`` — see UpdatePins).
    updates: UpdatePins = field(default_factory=UpdatePins)
    # Policy-only central-distribution pins (outside ``controls`` — see
    # PolicyDistribution). Where THIS document is fetched from, so an enterprise
    # admin owns one file and the fleet follows it.
    distribution: PolicyDistribution = field(default_factory=PolicyDistribution)
    # Optional operator-declared fallback profile (policy top-level ``fallback``).
    # When a per-surface profile FILE is unusable (unreadable, unparseable, or a
    # broken ``extends``), the loader substitutes THIS profile instead of the
    # most-restrictive deny-all. Absent (the default) keeps the deny-all fallback,
    # so an operator who declares nothing is unchanged (fail-closed). A declared
    # fallback is still intersected with this ceiling, so it can only ever narrow
    # it — it trades strict fail-closed for keeping the unlisted planes available.
    fallback_profile: "Optional[Profile]" = None
    # Policy-only composed posture for ``capabilities.agentcore``
    # (``workload`` | ``login``). Not a CapabilityGate field — that type stays
    # ``enabled`` + ``scopes``. Read through :func:`agentcore_posture`, which
    # returns ``None`` when the capability is off, omitted, or fail-closed
    # disabled. A profile cannot compose a different posture onto this field
    # (policy-wins).
    agentcore_identity_posture: Optional[str] = None
    # Policy-only Gateway MCP URL (``capabilities.agentcore.gateway_url``).
    # Empty when omitted or the capability is off. Not a CapabilityGate field.
    # A profile cannot carry this key. Read through
    # :func:`agentcore_gateway_url`.
    agentcore_gateway_url: str = ""
    # Policy-only workload identity name (``capabilities.agentcore.workload_name``).
    # Empty when omitted — runtime then uses env or a later default. A profile
    # cannot carry this key. Read through :func:`agentcore_workload_name`.
    agentcore_workload_name: str = ""

    def get(self, scope: str) -> Optional[object]:
        return self.controls.get(scope)

    def signature_summary(self) -> str:
        """One-line, operator-facing description of the ceiling's provenance.

        Exists so every surface (CLI, and any future viewer) renders the SAME
        vocabulary instead of each re-deriving "is this issuer meaningful?" from
        the raw fields — the mistake this state machine replaces was printing an
        issuer that no check had ever established.
        """
        if self.signature_state == SIGNATURE_VERIFIED:
            return f"signed and verified (issuer: {self.identity_issuer})"
        if self.signature_state == SIGNATURE_UNVERIFIED:
            issuer = self.identity_issuer or "unnamed issuer"
            return f"signed but UNVERIFIED — issuer {issuer!r} is unproven (advisory)"
        if self.signature_state == SIGNATURE_UNSIGNED:
            return "unsigned (integrity rests on filesystem permissions)"
        return "signature not checked (parsed without a trust root)"

    def pinned_command_patterns(self) -> Tuple[str, ...]:
        """Force-deny command patterns pinned by this ceiling's ``commands`` scope.

        Enterprise lock: these patterns are un-opt-out-able. hooks/security union
        them into the effective denied set so a user cannot disable a built-in
        rule the fleet policy pinned. Returns ``()`` when the policy does not
        govern ``commands`` or governs it in allow (allowlist) mode.
        """
        return _command_deny_patterns(self.controls.get(COMMANDS_SCOPE))


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
    #: Capability-family keys this profile declared that THIS build does not know
    #: (see the key-open tolerance note in ``_parse_controls``). Diagnostic only —
    #: an unknown key governs nothing here, so it is recorded rather than enforced.
    #: Defaults to ``()`` so every existing constructor and ``replace()`` call keeps
    #: working unchanged.
    unknown_scopes: Tuple[str, ...] = ()

    def get(self, scope: str) -> Optional[object]:
        return self.controls.get(scope)

    def pinned_command_patterns(self) -> Tuple[str, ...]:
        """Force-deny command patterns pinned by this profile's ``commands`` scope."""
        return _command_deny_patterns(self.controls.get(COMMANDS_SCOPE))


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


def _command_deny_patterns(control: object) -> Tuple[str, ...]:
    """Extract force-deny command patterns from a parsed ``commands`` control.

    A DENY-mode ScopedRuleset's ``deny`` tuple IS the set of force-pins: patterns
    that must remain denied regardless of user opt-out. An ALLOW-mode ruleset is
    an allowlist (deny-by-default) enforced by ``gate_decision`` and CANNOT be
    projected as a deny-pattern union, so it returns ``()`` (with a debug log).
    Any non-ScopedRuleset control (e.g. an ``_AndRuleset`` that can only arise
    from an allow-mode combination) also returns ``()``.
    """
    if isinstance(control, ScopedRuleset) and control.mode == MODE_DENY:
        return control.deny
    if control is not None:
        logger.debug("commands control %r yields no force-deny pins", type(control))
    return ()


_AGENTCORE_SCOPE = "capabilities.agentcore"
_AGENTCORE_POSTURES = frozenset({"workload", "login"})
_AGENTCORE_POLICY_ONLY = frozenset({"posture", "gateway_url", "workload_name"})


def _capability_raw_for_gate(
    scope: str, raw: Mapping[str, object], *, is_policy: bool
) -> Mapping[str, object]:
    """Drop inner policy data that is not a CapabilityGate field.

    ``capabilities.agentcore.posture``, ``gateway_url``, and
    ``workload_name`` are policy data, not a second scope and not an
    evaluator input. Strip them on a policy document so
    ``CapabilityGate.from_dict`` stays ``additionalProperties: false``.
    A profile (or policy fallback body) that carries either key is
    rejected — Rule 6, same fail-closed raise as ``ScopedMap.posture``.
    """
    if scope != _AGENTCORE_SCOPE:
        return raw
    extra = _AGENTCORE_POLICY_ONLY.intersection(raw)
    if not extra:
        return raw
    if not is_policy:
        name = "posture" if "posture" in extra else next(iter(extra))
        raise PlatformCompositionError(
            f"capabilities.agentcore.{name} is policy-only; not allowed on a profile"
        )
    return {key: value for key, value in raw.items() if key not in _AGENTCORE_POLICY_ONLY}


def _apply_agentcore_posture(
    data: Mapping[str, object],
    controls: Dict[str, object],
    boot: "BootControls",
) -> Optional[str]:
    """Validate agentcore posture and return the value to persist on the ceiling.

    Missing or unknown ``posture`` with ``enabled: true`` aborts when
    ``boot.fail_closed``; otherwise the row is treated as disabled. Disabled
    (or unnamed) rows do not require a posture and yield ``None``.
    """
    raw_caps = data.get("capabilities")
    if not isinstance(raw_caps, dict):
        return None
    raw = raw_caps.get("agentcore")
    if not isinstance(raw, dict):
        return None
    control = controls.get(_AGENTCORE_SCOPE)
    if not isinstance(control, CapabilityGate) or not control.enabled:
        return None
    posture = raw.get("posture")
    if isinstance(posture, str) and posture in _AGENTCORE_POSTURES:
        return posture
    reason = (
        f"capabilities.agentcore is enabled but posture is {posture!r}; "
        "expected 'workload' or 'login'"
    )
    if boot.fail_closed:
        raise PlatformCompositionError(reason)
    controls[_AGENTCORE_SCOPE] = CapabilityGate(enabled=False, scopes=control.scopes)
    return None


def agentcore_posture(ceiling: Optional[GovernanceCeiling]) -> Optional[str]:
    """Return the policy posture if ``capabilities.agentcore`` is enabled.

    ``\"workload\"`` or ``\"login\"`` when the ceiling enables the capability
    with a known posture. ``None`` when there is no ceiling, the capability is
    omitted, disabled, or fail-closed-disabled. The single reader later work
    must use — do not re-parse raw policy JSON for this field.
    """
    if ceiling is None:
        return None
    stored = ceiling.agentcore_identity_posture
    if stored not in _AGENTCORE_POSTURES:
        return None
    control = ceiling.controls.get(_AGENTCORE_SCOPE)
    if not isinstance(control, CapabilityGate) or not control.enabled:
        return None
    return stored


def _apply_agentcore_gateway_url(
    data: Mapping[str, object],
    controls: Dict[str, object],
    boot: "BootControls",
) -> str:
    """Validate and return the policy Gateway URL, or empty.

    Missing / empty is legal (the crew may still name a posture and add
    the URL later). A present but unusable value aborts when
    ``boot.fail_closed``; otherwise it is ignored.
    """
    from kiro_crew.platform.agentcore_schema import normalize_agentcore_gateway_url

    raw_caps = data.get("capabilities")
    if not isinstance(raw_caps, dict):
        return ""
    raw = raw_caps.get("agentcore")
    if not isinstance(raw, dict):
        return ""
    control = controls.get(_AGENTCORE_SCOPE)
    if not isinstance(control, CapabilityGate) or not control.enabled:
        return ""
    value = raw.get("gateway_url")
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        reason = "capabilities.agentcore.gateway_url must be a string"
        if boot.fail_closed:
            raise PlatformCompositionError(reason)
        return ""
    try:
        return normalize_agentcore_gateway_url(value)
    except ValueError as exc:
        if boot.fail_closed:
            raise PlatformCompositionError(str(exc)) from exc
        return ""


def agentcore_gateway_url(ceiling: Optional[GovernanceCeiling]) -> str:
    """Return the policy Gateway URL if ``capabilities.agentcore`` is enabled."""
    if ceiling is None or agentcore_posture(ceiling) is None:
        return ""
    return str(ceiling.agentcore_gateway_url or "")


def _apply_agentcore_workload_name(
    data: Mapping[str, object],
    controls: Dict[str, object],
    boot: "BootControls",
) -> str:
    """Validate and return the policy workload name, or empty."""
    from kiro_crew.platform.agentcore_schema import normalize_agentcore_workload_name

    raw_caps = data.get("capabilities")
    if not isinstance(raw_caps, dict):
        return ""
    raw = raw_caps.get("agentcore")
    if not isinstance(raw, dict):
        return ""
    control = controls.get(_AGENTCORE_SCOPE)
    if not isinstance(control, CapabilityGate) or not control.enabled:
        return ""
    value = raw.get("workload_name")
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        reason = "capabilities.agentcore.workload_name must be a string"
        if boot.fail_closed:
            raise PlatformCompositionError(reason)
        return ""
    try:
        return normalize_agentcore_workload_name(value)
    except ValueError as exc:
        if boot.fail_closed:
            raise PlatformCompositionError(str(exc)) from exc
        return ""


def agentcore_workload_name(ceiling: Optional[GovernanceCeiling]) -> str:
    """Return the policy workload name if ``capabilities.agentcore`` is enabled."""
    if ceiling is None or agentcore_posture(ceiling) is None:
        return ""
    return str(ceiling.agentcore_workload_name or "")


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
        gate_raw: Mapping[str, object] = _capability_raw_for_gate(scope, raw, is_policy=is_policy)
        return CapabilityGate.from_dict(
            gate_raw, default_enabled=spec.capability_default, scope_matchers=spec.scope_matchers
        )
    if spec.kind == SCOPEDMAP:
        return ScopedMap.from_dict(raw, allow_posture=is_policy)
    raise PlatformCompositionError(f"scope {scope!r} has unknown archetype {spec.kind!r}")


# Structural (non-governed) keys consumed by parse_policy/parse_profile, not as
# governed scopes.
_STRUCTURAL_KEYS = frozenset(
    {
        "version",
        "boot",
        "identity",
        "name",
        "bind",
        "extends",
        "description",
        "updates",
        "distribution",
        "fallback",
    }
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

# The EXHAUSTIVE set of non-governed ``sandbox`` boot flags that may ride raw
# under ``_SANDBOX_FLAGS_SCOPE``.  Anything else under ``sandbox`` fails closed
# like every other fixed-child namespace (filesystem, folders, network).
#
# Why an allowlist rather than blanket tolerance: ``sandbox.min_level`` is the
# ordinal floor with the widest blast radius in the catalog, and the reserved
# scope is WRITE-ONLY — nothing reads it back.  Blanket tolerance therefore
# turned a one-character typo into silent total loss of the floor:
# ``{"sandbox": {"min_levl": "strict"}}`` parsed clean, reported OK from
# ``kirocrew policy validate``, rendered as a governed scope in
# ``kirocrew policy show``, and left ``sandbox.min_level`` absent — so
# ``sandbox._governance_sandbox_floor()`` read "ungoverned" and clamped nothing.
# Green validation with zero enforcement is precisely the failure class this
# model exists to prevent, so an unrecognized child must be loud.
#
# Why not reject EVERY non-``min_level`` child: these two names are documented
# in-code above as reserved, and a deployed policy may already carry one.
# Rejecting them would abort boot on a policy that parses today, converting a
# diagnostic gap into an availability regression.  Keeping the two known names
# accepted costs nothing (they are inert either way) and confines the new
# strictness to names nobody has been told are valid.
_SANDBOX_RESERVED_FLAGS = frozenset({"require_isolation", "env_scrub_prefixes"})

# Scope aliases: the provider doc names the profile's path scopes ``folders.read``/
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


def _parse_controls(
    data: Mapping[str, object],
    *,
    is_policy: bool,
    unknown_out: Optional[List[str]] = None,
    profile_name: str = "",
) -> Dict[str, object]:
    """Flatten a policy/profile JSON body into the catalog's dotted-scope map.

    Data-driven against ``SCOPE_CATALOG`` so a newly ``register_scope``'d family
    parses with no loader edit (the extensibility contract):

    * a top-level key that is itself a flat catalog scope is taken directly;
    * a nested namespace (``<key>.<sub>`` exists in the catalog) descends
      generically — every child ``<sub>`` that resolves to a catalog scope is
      parsed, and an unknown child fails closed;
    * ``capabilities`` (and any registered key-open namespace) maps each child to
      ``<key>.<child>``, deferring the known/unknown decision to the catalog.

    An unknown governed key fails closed (tamper-evidence / Rule 8), with ONE
    narrow exception: see ``unknown_out``.

    ``unknown_out`` opts into KEY-OPEN TOLERANCE and is passed ONLY by
    :func:`parse_profile`. When present, an unknown child of a key-open namespace
    (``capabilities.*``) does not invalidate the profile — but ONLY in the
    ASYMMETRIC case where the child is a dict whose ``enabled`` is exactly
    ``True``. Such a child is skipped, warned about, and appended here for the
    caller to record on ``Profile.unknown_scopes``. Every KNOWN sibling in the same
    block still parses and is still enforced.

    Every OTHER unknown child — ``enabled: false``, ``enabled`` absent, a non-dict
    value, anything malformed — RAISES exactly as it did before the tolerance
    existed, so the loader degrades the whole profile to its bind-preserving
    deny-all fallback.

    Why the rule is asymmetric, and why it is profile-only:

    * a profile is **narrow-only**, and the intersection is performed by
      :func:`resolve` (rule-2 intersect of ceiling ∘ profile), so an unknown
      ``enabled: true`` cannot change ANY decision in ANY build: if the scope is
      unregistered there is nothing to decide, and if a later build registers it
      the profile merely declines to narrow it. Skipping it therefore loses
      nothing, and that holds even when the key is a typo;
    * an unknown NARROWING (``enabled: false``, or a malformed child whose intent
      cannot be read) is indistinguishable from a typo'd narrowing of a CORE
      capability — ``{"spwan": {"enabled": false}}`` reads exactly like a failed
      attempt to disable ``spawn``. Honoring the operator's intent requires
      failing closed there: the loud deny-all fallback surfaces the typo, whereas
      tolerating it would silently grant what the operator tried to deny;
    * the POLICY/ceiling path keeps raising (``unknown_out`` stays ``None``), so
      tamper-evidence on the ceiling is unchanged (Rule 8). The policy's optional
      top-level ``fallback`` object parses with ``is_policy=False`` but no
      ``unknown_out``, so it fails closed at boot;
    * unknown **top-level** governed families still fail closed either way — only
      children inside an already-known key-open family are eligible.

    The concrete defect this fixes: the internal edition ``register_scope``s extra
    ``capabilities.*`` rows and seeds a profile naming them with ``enabled: true``;
    a public edition reading the SAME data home rejects that whole profile and
    degrades the surface to deny-all. Cross-edition data-home sharing is supported,
    so an inert declaration must not be fatal.

    Accepted residual: a typo'd capability name carrying ``enabled: true`` is
    tolerated rather than surfaced. That is inert by the first bullet above (it
    could not have changed a decision), so the cost is a missed diagnostic, not a
    permission change. ``Profile.unknown_scopes`` is what surfaces it.

    This relies on ``SCOPE_CATALOG`` being APPEND-ONLY (a scope is added or
    retired, never renamed in place) — otherwise a renamed key declared
    ``enabled: true`` would be silently tolerated instead of surfacing as the
    migration it is.
    """
    controls: Dict[str, object] = {}
    # Precompute, per top-level key, the set of dotted children in the catalog.
    nested_children: Dict[str, set] = {}
    for scope in SCOPE_CATALOG:
        head, sep, tail = scope.partition(".")
        if sep and not tail.startswith("_"):
            nested_children.setdefault(head, set()).add(tail)

    def take(scope: str, raw: object, *, tolerate_unknown: bool = False) -> None:
        # Normalize an alias (folders.* → filesystem.*) so it lands in the SAME
        # control key the gate queries, and a profile's folders.* actually narrows
        # the policy's filesystem.* ceiling.
        scope = _SCOPE_ALIASES.get(scope, scope)
        spec = SCOPE_CATALOG.get(scope)
        if spec is None:
            # ASYMMETRIC TOLERANCE (profiles only, key-open families only): an
            # unknown child whose payload is EXACTLY ``{"enabled": true}`` is
            # inert — the intersection lives in resolve() and a profile is
            # narrow-only, so declining to narrow an unregistered scope cannot
            # change any decision in any build. The payload must be the exact
            # one-key dict: a capability payload can carry inner narrowing
            # rulesets (``spawn`` has ``agents``, ``publish`` has
            # ``destinations``), so ``{"enabled": true, "agents": {...}}`` is
            # an ENABLE-plus-NARROWING and skipping it would drop the inner
            # narrowing — that shape, any other extra key, ``enabled: false``,
            # a truthy-but-not-True enabled (``1``, ``"true"``), or a non-dict
            # is indistinguishable from a typo'd narrowing of a core
            # capability, so it must fail closed: honoring the operator's
            # intent means the loud deny-all fallback rather than silently
            # granting what they tried to deny. Key-set equality plus an ``is
            # True`` identity check (never ``==``, which admits ``1``) is what
            # makes the inertness proof airtight.
            if (
                tolerate_unknown
                and unknown_out is not None
                and isinstance(raw, dict)
                and set(raw.keys()) == {"enabled"}
                and raw["enabled"] is True
            ):
                logger.warning(
                    "profile %r declares capability scope %r which this build does not "
                    "register; skipping it because it is declared enabled=true, which "
                    "cannot change any decision (a profile only narrows, and the "
                    "intersection is applied in resolve). Known capabilities in this "
                    "profile are still enforced. If this name is a typo, correct it — "
                    "the same key with enabled=false would instead fail closed.",
                    profile_name or "<unnamed>",
                    scope,
                )
                unknown_out.append(scope)
                return
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
            # Every child is a catalog scope; an unknown child fails closed in
            # take() for a POLICY, and is tolerated + recorded for a PROFILE.
            tolerate = not is_policy and unknown_out is not None
            for child, child_raw in raw.items():
                take(f"{key}.{child}", child_raw, tolerate_unknown=tolerate)
        elif key in nested_children and isinstance(raw, dict):
            # A fixed-child namespace (filesystem, folders, network, sandbox, …).
            for sub, sub_raw in raw.items():
                dotted = f"{key}.{sub}"
                if dotted in SCOPE_CATALOG:
                    take(dotted, sub_raw)
                elif key == "sandbox" and sub in _SANDBOX_RESERVED_FLAGS:
                    # A KNOWN non-governed boot flag rides raw under a reserved
                    # scope.  The membership test is what keeps a typo'd
                    # ``min_level`` from being swallowed here instead of
                    # reaching the fail-closed raise below.
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
def parse_policy(
    data: Mapping[str, object], *, signature_state: str = SIGNATURE_UNCHECKED
) -> GovernanceCeiling:
    """Parse a policy mapping into a frozen ``GovernanceCeiling``.

    Fails closed (raises ``PlatformCompositionError``) on any structural problem
    so a malformed-but-present policy never silently degrades to ungoverned.

    ``signature_state`` is supplied by :func:`load_security_policy`, which is the
    only caller that has the trust root.  It defaults to ``SIGNATURE_UNCHECKED``
    (not ``SIGNATURE_UNSIGNED``) because a direct ``parse_policy`` call has PROVEN
    nothing about provenance — reporting "unsigned" would conflate "we looked and
    found no signature" with "nobody looked".
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
    composed_posture = _apply_agentcore_posture(data, controls, boot)
    composed_gateway_url = _apply_agentcore_gateway_url(data, controls, boot)
    composed_workload_name = _apply_agentcore_workload_name(data, controls, boot)
    identity = data.get("identity") or {}
    issuer = str(identity.get("issuer", "")) if isinstance(identity, dict) else ""
    signature = str(identity.get("signature", "")) if isinstance(identity, dict) else ""
    raw_updates = data.get("updates")
    if raw_updates is not None and not isinstance(raw_updates, dict):
        raise PlatformCompositionError("security policy 'updates' must be an object")
    raw_distribution = data.get("distribution")
    if raw_distribution is not None and not isinstance(raw_distribution, dict):
        raise PlatformCompositionError("security policy 'distribution' must be an object")
    # Optional operator-declared fallback profile (see GovernanceCeiling.fallback_profile).
    # Parsed as a narrow-only PROFILE (is_policy=False): resolve() intersects it with
    # this ceiling like any per-surface profile when a profile FILE is unusable, so it
    # can only narrow the ceiling. Unknown scopes inside it fail closed at boot, on
    # BOTH sides of the key-open asymmetry: this call passes no ``unknown_out``, so a
    # ``capabilities.*`` child this build does not register raises here even when it
    # declares ``enabled: true`` — the case a real profile FILE tolerates. The policy
    # document is the tamper-evidence surface (Rule 8), so it gets no tolerance.
    raw_fallback = data.get("fallback")
    fallback_profile: "Optional[Profile]" = None
    if raw_fallback is not None:
        if not isinstance(raw_fallback, dict):
            raise PlatformCompositionError("security policy 'fallback' must be an object")
        # The fallback payload is a controls-only profile body. EVERY structural
        # key (name/bind/extends/updates/fallback/…) is skipped by _parse_controls,
        # so a payload like {"extends": "lockdown"} would silently lose its intent
        # and the fallback would resolve to ceiling-permitted controls instead of the
        # intended restriction. Reject any structural key so such a fallback fails
        # closed at boot rather than vanishing.
        stray = sorted(_STRUCTURAL_KEYS & raw_fallback.keys())
        if stray:
            raise PlatformCompositionError(
                f"security policy 'fallback' may not contain structural key(s) {stray} — "
                "it is a controls-only profile body"
            )
        fallback_profile = Profile(
            name="_fallback", controls=_parse_controls(raw_fallback, is_policy=False)
        )
    return GovernanceCeiling(
        version=POLICY_VERSION,
        boot=boot,
        controls=controls,
        identity_issuer=issuer,
        identity_signature=signature,
        signature_state=signature_state,
        updates=UpdatePins.from_dict(raw_updates or {}),
        distribution=PolicyDistribution.from_dict(raw_distribution or {}),
        fallback_profile=fallback_profile,
        agentcore_identity_posture=composed_posture,
        agentcore_gateway_url=composed_gateway_url,
        agentcore_workload_name=composed_workload_name,
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
    # ``updates`` is POLICY-ONLY. It is in _STRUCTURAL_KEYS (so _parse_controls
    # skips it rather than failing as an unknown scope), which would make a
    # profile's copy silently inert — so reject it loudly here instead. A profile
    # is narrow-only and there is no narrower version of pointing somewhere else:
    # a per-app profile redirecting the update source would be escalation.
    if "updates" in data:
        raise PlatformCompositionError(
            "profiles may not set 'updates' — update pins are policy-only "
            "(a profile redirecting the update source would be privilege escalation)"
        )
    # ``distribution`` is POLICY-ONLY for a stronger version of the same reason,
    # and it is in _STRUCTURAL_KEYS for the same mechanical one (so _parse_controls
    # skips it rather than failing as an unknown scope, which would make a
    # profile's copy silently inert). Redirecting where the CEILING is fetched from
    # is not a narrowing at all — it replaces the whole enforcement document, so a
    # profile that could do it would be the widest escalation in this model.
    if "distribution" in data:
        raise PlatformCompositionError(
            "profiles may not set 'distribution' — central policy distribution is "
            "policy-only (a profile redirecting where the ceiling is fetched from "
            "would replace the enforcement document, not narrow it)"
        )
    # ``fallback`` is POLICY-ONLY for the same reason ``updates`` is. It is in
    # _STRUCTURAL_KEYS (so parse_policy's _parse_controls skips it rather than
    # rejecting it as an unknown scope), which would make a profile's copy silently
    # inert — a validation hole. A profile IS a per-surface narrowing; there is no
    # narrower "what an unusable profile falls back to", and a profile declaring its
    # own fallback would be nonsensical. Reject it loudly here (the loader turns this
    # into the deny-all fallback via Validation rule 5).
    if "fallback" in data:
        raise PlatformCompositionError(
            "profiles may not set 'fallback' — the unusable-profile fallback is "
            "policy-only (a profile declaring its own fallback is meaningless)"
        )
    bind: Optional[Bind] = None
    raw_bind = data.get("bind")
    if isinstance(raw_bind, dict):
        btype = str(raw_bind.get("type", "")).strip()
        if btype not in ("surface", "app", "task"):
            raise PlatformCompositionError(
                f"profile bind.type must be surface|app|task, got {btype!r}"
            )
        bind = Bind(type=btype, id=str(raw_bind.get("id", "")))
    # Key-open tolerance is opted into HERE and nowhere else (by passing
    # ``unknown_out``): an unknown ``capabilities.*`` child declared
    # ``enabled: true`` is inert, so it does not cost a cross-edition data home its
    # whole profile. Any other unknown child still raises. See _parse_controls.
    unknown: List[str] = []
    controls = _parse_controls(data, is_policy=False, unknown_out=unknown, profile_name=name)
    return Profile(
        name=name,
        bind=bind,
        extends=str(data.get("extends", "")),
        controls=controls,
        unknown_scopes=tuple(unknown),
    )


def _read_json_file(path: Path) -> Dict[str, object]:
    """Read + JSON-parse a governance file.  Raises on any failure (fail-closed)."""
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise PlatformCompositionError(f"governance file {path} is not a JSON object")
    return data


def policy_signing_payload(data: Mapping[str, object]) -> bytes:
    """Canonical bytes an ``identity.signature`` covers, for the policy *document*.

    Routed through ``admission.canonical_signing_bytes`` — the SAME
    sorted-keys/compact-separators/UTF-8 canonicalization
    ``PluginManifest.signing_payload`` uses — so the two trust roots cannot drift
    apart, and a reviewer verifies consistency by reading one function.

    Coverage is the **whole document minus ``identity.signature``** (the signature
    cannot cover itself; ``identity.issuer`` IS covered, so an attacker cannot
    re-label a validly-signed policy as coming from a different issuer).  Signing
    the raw document rather than a projection of the parsed
    :class:`GovernanceCeiling` is deliberate and does two things a projection
    could not:

    * it covers keys THIS build does not know — a companion-registered scope, or a
      future schema addition — so stripping or editing one is still detected,
      whereas a projection would silently omit it; and
    * it keeps the payload scope-name-agnostic, honoring the module's core
      invariant that adding a scope is a ``SCOPE_CATALOG`` data change and never
      an edit to shared machinery.

    Because coverage is byte-canonical over the parsed JSON (not over the file's
    literal bytes), re-indenting or reordering keys does NOT break a signature,
    while changing any value or adding/removing any key does.
    """
    body = {k: v for k, v in data.items() if k != "identity"}
    identity = data.get("identity")
    if isinstance(identity, dict):
        # Preserve every identity field EXCEPT the signature, so issuer (and any
        # future field such as an expiry or key id) is inside the signed payload.
        rest = {k: v for k, v in identity.items() if k != "signature"}
        if rest:
            body["identity"] = rest
    elif identity is not None:
        # A non-object identity is a schema error parse_policy will reject; keep it
        # inside the payload rather than silently dropping it from coverage.
        body["identity"] = identity
    return canonical_signing_bytes(body)


def _policy_signature_state(
    data: Mapping[str, object], trust_keys: Mapping[str, str]
) -> Tuple[str, str]:
    """Classify a policy document's signature.  Returns ``(state, detail)``.

    Pure and I/O-free: the caller supplies the trust keys, so this is directly
    unit-testable and the loader keeps the single responsibility of deciding what
    to DO with the verdict.  Mirrors ``admission._signature_valid`` — HMAC-SHA256
    over the canonical payload, compared with ``hmac.compare_digest`` — with the
    key selected by ``identity.issuer`` instead of a plugin ``publisher``.
    """
    identity = data.get("identity")
    identity_map: Mapping[str, object] = identity if isinstance(identity, dict) else {}
    signature = str(identity_map.get("signature", "")).strip()
    issuer = str(identity_map.get("issuer", "")).strip()
    if not signature:
        return SIGNATURE_UNSIGNED, "no identity.signature"
    if not issuer:
        # A signature with no issuer names no key, so nothing can verify it.
        return SIGNATURE_UNVERIFIED, "identity.signature present but identity.issuer is empty"
    secret = trust_keys.get(issuer)
    if not secret:
        return SIGNATURE_UNVERIFIED, f"no trust key for issuer {issuer!r}"
    expected = hmac_signature(secret, policy_signing_payload(data))
    # Compare BYTES, not str: ``hmac.compare_digest`` raises TypeError on a str
    # containing any non-ASCII character, and a policy file's signature is
    # attacker- or paste-controlled text.  A raised TypeError here is not a
    # denial — it escapes ``load_security_policy`` as a plain (non-
    # ``PlatformCompositionError``) exception, so the boot handler does not treat
    # it as fatal and the later ``safe_context_call`` degrades to the no-ceiling
    # fallback: a tampered policy would yield an UNGOVERNED host even with
    # ``require_policy_signature`` on, inverting the flag. Encoding both sides
    # keeps every malformed signature an ordinary UNVERIFIED verdict.
    if hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8")):
        return SIGNATURE_VERIFIED, f"issuer {issuer!r}"
    return SIGNATURE_UNVERIFIED, f"signature does not match trust key for issuer {issuer!r}"


def _policy_trust_settings() -> Tuple[bool, Dict[str, str]]:
    """Read the operator-controlled trust root: ``(require_policy_signature, keys)``.

    Sourced from the **admission policy** (``KIROCREW_ADMISSION_POLICY`` env path,
    else ``<data home>/admission_policy.json``) rather than from a new key store,
    for three reasons:

    * a document must not be the authority on whether it has to be authentic — a
      ``require_signature`` flag INSIDE ``security_policy.json`` would be
      self-referential, and an attacker rewriting the policy would simply clear
      it;
    * the admission policy is already the fleet-controlled trust root of this
      package, already carries ``trust_keys``, and is already on the
      ``is_sensitive_path`` keystone (the agent can neither read nor write it), so
      it inherits every protection the plugin trust root has; and
    * one key store means an operator provisions keys once.

    Reads via ``admission.read_policy_trust_root`` — the SIDE-EFFECT-FREE reader —
    not ``load_admission_policy``: the latter records the dashboard admission
    posture and emits a critical ``governance_degraded`` SEL on an absent policy,
    which is correct once per process at boot and wrong here, because this runs on
    a repeating path (``gatewayd`` re-loads the security policy per app call) and
    would flip the governance indicator to degraded on every one.

    Never raises, and an unreadable admission policy yields no keys and a
    ``False`` opt-in: an admission-policy problem is already handled loudly and
    fail-closed in admission's own domain, and it must not additionally make the
    security ceiling unloadable through a second path.
    """
    try:
        adm = read_policy_trust_root()
        return bool(adm.require_policy_signature), dict(adm.trust_keys)
    except Exception:
        logger.debug("policy trust settings unavailable", exc_info=True)
        return False, {}


def _policy_signature_required() -> bool:
    """True when the admission policy explicitly opted in.

    A trust root that is absent, unreadable, or not a JSON object reads as **no
    opt-in**, and that is deliberate rather than a gap.  An attacker who can write
    ``admission_policy.json`` is explicitly out of this feature's threat model (see
    the threat-model note in ``docs/system-specs/modules/governance.md``) — such a
    process would simply set the flag to ``false``, which is well-formed JSON, so
    fail-closing on a *malformed* file only catches a clumsy version of an attack
    the design already concedes.  What a corrupt trust root actually indicates in
    practice is a non-atomic fleet push or a hand-edit typo — a reliability event —
    and the useful response to that is to log loudly and behave predictably, which
    ``read_policy_trust_root`` already does.  ``kirocrew doctor`` surfaces it.

    Never raises.
    """
    try:
        data = json.loads(policy_trust_root_path().read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(isinstance(data, dict) and data.get("require_policy_signature", False))


def _audit_policy_signature(state: str, detail: str, path_label: str) -> None:
    """Best-effort audit of the policy signature verdict.  Never raises.

    A ``verified`` outcome is a debug-level non-event; the interesting records are
    an unverified signature (someone TRIED to assert provenance and it did not
    hold) and the fail-closed trip.  Mirrors ``admission._audit_admission_fail_closed``
    /``_verify_seed_integrity``: SEL for the durable trail, and the process-global
    health mark only for the genuine fail-closed case so a merely-advisory
    mismatch does not flip the dashboard to degraded on every boot.
    """
    if state == SIGNATURE_VERIFIED:
        logger.debug("security policy signature verified (%s)", detail)
        return
    try:

        sel().log_api_access(
            caller="_host",
            operation="security_policy_signature",
            outcome=state,
            source="startup",
            # WHICH policy and WHY both live here rather than in the log line below.
            resources=path_label,
            error=detail,
        )
    except Exception:
        logger.debug("policy signature SEL emit unavailable", exc_info=True)
    if state == SIGNATURE_UNVERIFIED:
        # Neither the label nor the reason is interpolated. Both carry text this process
        # did not author — the label is whatever the caller was resolving (for the fetch
        # tier a URL that may itself be a credential) and the reason names the issuer the
        # DOCUMENT claimed — and the log ring is served by ``GET /api/logs``, which the
        # agent's own browser tooling can drive. The SEL record above is on the keystone
        # and is not reachable that way, so it is where an audit reason belongs; this line
        # exists so the state is visible in an operator's console.
        logger.warning(
            "the security policy carries an UNVERIFIED signature; treating the ceiling as "
            "unauthenticated. The security_policy_signature audit record names which "
            "policy and why."
        )


def _verify_policy_signature(data: Mapping[str, object], *, source: str) -> str:
    """Compute and audit the signature state for a policy document.

    **Computes the verdict; does not enforce it.**  Enforcement is
    :func:`assert_policy_signature_satisfied`, which runs once on the FINAL
    composed ceiling.  The split matters because ``load_security_policy`` walks a
    precedence chain (env → companion bundle → operator home) and runs more than
    once per boot with different arguments: the core's loader-less pass falls
    through to the HOME tier and would raise on it, even when the edition's later
    pass would have selected a correctly-signed BUNDLE that outranks it.  A tier
    the final ceiling never came from must not be able to abort boot, so every
    load-time verdict here is preliminary and only the surviving one is judged.

    Returns the state to record on the ceiling.  Never raises.
    """
    _, trust_keys = _policy_trust_settings()
    state, detail = _policy_signature_state(data, trust_keys)
    _audit_policy_signature(state, detail, source)
    return state


def _mark_policy_signature_incident(detail: str) -> None:
    """Best-effort: record the fail-closed trip on the governance health signal."""
    try:

        mark_governance_incident("failed_closed", detail=f"security_policy_signature:{detail}")
    except Exception:
        logger.debug("governance health mark unavailable", exc_info=True)


def load_security_policy(
    *, bundled_loader: Optional[Callable[[], Optional[Mapping[str, object]]]] = None
) -> Optional[GovernanceCeiling]:
    """Load the enterprise security policy, or ``None`` for editable defaults.

    Precedence (first present wins):

    1. ``KIROCREW_SECURITY_POLICY`` env path — fleet hot-override, highest.
    2. **the centrally distributed document** — fetched from ``KIROCREW_POLICY_URL``
       or from the ``distribution.source`` a lower tier declares, served from the
       last-known-good cache when the endpoint is unreachable.  See
       :mod:`kiro_crew.platform.policy_distribution`.  Tier 1 stays above it so an
       operator recovering from a bad central push has a channel that outranks the
       thing that broke; tiers 3 and 4 sit below because they are what the fetched
       document replaces.
    3. ``bundled_loader()`` — the companion-bundled resource, supplied by the
       caller when the active edition is ``amazon`` (Phase 9 packages it via
       ``importlib.resources``).  The public core passes ``None`` here.
    4. ``~/.kiro/crew/security_policy.json`` — standalone operator-authored.
    5. None → editable secure-defaults (standalone, ungoverned ceiling).

    A **present-but-unreadable / invalid** policy at the env or home path raises
    ``PlatformCompositionError`` (fail-closed to strictest), mirroring
    ``admission.load_admission_policy`` — a fleet that meant to enforce something
    must never silently fall open.  The bundled loader is trusted (same author,
    covered by the admission signature) so its parse errors also raise.

    **Signature verification (``identity.signature``).**  The two FILE tiers (1
    and 3) carry a detached signature over the canonical document
    (:func:`policy_signing_payload`), verified against a trust key the
    *operator-controlled admission policy* holds — never a key the security policy
    supplies about itself.  The verdict is recorded on
    ``GovernanceCeiling.signature_state`` and is **advisory by default**: an
    unsigned or unverifiable policy still loads and still governs, so every
    existing standalone install and every existing policy file keeps working
    byte-for-byte unchanged.  Setting ``require_policy_signature`` in the
    admission policy makes it mandatory, and a failure then raises
    ``PlatformCompositionError`` (boot aborts) like every other fail-closed check
    in this module.

    **All three tiers are verified — none is exempt.**  The plugin-admission
    manifest signature covers only the manifest fields (name / publisher /
    version / capabilities — ``admission.PluginManifest.signing_payload``), NOT
    the bytes of the packaged ``security_policy.json``, so "covered by admission"
    never actually protected the bundled resource: a tampered bundled policy would
    have loaded unchecked.  So the bundled tier passes ``signable=True`` like the
    file tiers.  When ``require_policy_signature`` is OFF (the default, and what
    the ``amazon`` edition ships) verification is advisory at every tier and an
    unsigned bundled policy still loads — so existing installs are unchanged; when
    it is ON, an edition signs its bundled policy like any other governed tier.
    A **missing** policy does not satisfy the requirement either: with a genuine
    opt-in and no policy at any tier, boot aborts rather than running ungoverned.
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
        state = _verify_policy_signature(data, source=str(path))
        return parse_policy(data, signature_state=state)

    bundled = bundled_loader() if bundled_loader is not None else None

    home_path = _policy_home_path()
    home_data: Optional[Dict[str, object]] = None
    home_error: Optional[Exception] = None
    if home_path.exists():
        # Read here rather than at the home tier below because the CENTRAL tier
        # needs to see whether a lower tier declares a ``distribution`` source.
        # The read is captured, not acted on: an unreadable home file still raises
        # with the same message at the same point in precedence, one tier down.
        try:
            home_data = _read_json_file(home_path)
        except Exception as exc:
            home_error = exc

    # Tier 2 — the centrally distributed ceiling. The source comes from the env
    # (the fleet lever) or from the ``distribution`` block a lower tier declares
    # (self-refresh), preferring the bundled declaration over the home one so the
    # peek follows the same precedence the tiers themselves do. Returns None when
    # distribution is not configured, or when it is configured, could not be
    # established, and the policy chose to degrade; raises under the fail-closed
    # default. Fully inert — not even an import — when nothing declares a source.
    # ``_POLICY_DISTRIBUTION_CACHE_ONLY_ENV`` is in this condition because a
    # cache-only child (an app backend) is given NO source by design — the tier is
    # what reads the cache the gateway wrote, so gating entry on a source would skip
    # it and drop that child to a local or absent ceiling: exactly the looser-ceiling
    # failure cache-only mode exists to prevent.
    declared = _declared_distribution(bundled) or _declared_distribution(home_data)
    if (
        declared is not None
        or os.environ.get(_POLICY_DISTRIBUTION_URL_ENV, "").strip()
        or os.environ.get(_POLICY_DISTRIBUTION_CACHE_ONLY_ENV, "").strip()
    ):
        from kiro_crew.platform.policy_distribution import load_distributed_policy

        distributed = load_distributed_policy(declared)
        if distributed is not None:
            return distributed

    if bundled is not None:
        # The bundled tier is NOT exempt from a fleet that opted into
        # require_policy_signature. The plugin-admission manifest signature
        # covers only name/publisher/version/capabilities
        # (admission.PluginManifest.signing_payload), NOT the packaged
        # security_policy.json bytes, so "covered by admission" did not in
        # fact protect the resource — a tampered bundled policy would have
        # loaded unchecked. When require is OFF this is advisory exactly like
        # the file tiers (an unsigned bundled policy still loads), so the
        # standalone/default and the Amazon edition (which sets no require)
        # are unaffected; when require is ON the edition must sign its
        # bundled policy like any other governed tier.
        state = _verify_policy_signature(bundled, source="companion-bundled resource")
        return parse_policy(bundled, signature_state=state)

    if home_error is not None:
        raise PlatformCompositionError(
            f"security policy at {home_path} is unreadable: {home_error}"
        ) from home_error
    if home_data is not None:
        state = _verify_policy_signature(home_data, source=str(home_path))
        return parse_policy(home_data, signature_state=state)

    # No policy at env, bundled, OR home tier → ungoverned (editable defaults).
    #
    # This function deliberately does NOT refuse boot here, because it cannot tell
    # whether it is the LAST word on the question. ``load_security_policy()`` is
    # called more than once per boot with different arguments: the core calls it
    # with NO bundled_loader first (``bootstrap.build_default_context``) and a
    # companion edition re-invokes it WITH its loader afterwards. "No policy at any
    # tier" is therefore only decidable once composition has finished, so the
    # fail-closed refusal lives in :func:`assert_policy_signature_satisfied`, which
    # boot calls on the FINAL context alongside the other governance floor gates.
    return None


def _declared_distribution(
    data: Optional[Mapping[str, object]],
) -> Optional[PolicyDistribution]:
    """Peek a lower tier's ``distribution`` block, for the central tier's source.

    Returns ``None`` when *data* is absent, or declares no source AND the environment
    supplies none either — so the central tier stays completely inert, not even imported,
    on every install that does not use it.

    The environment half of that condition is load-bearing, not defensive. A block with
    settings and no ``source`` is legitimate when ``KIROCREW_POLICY_URL`` supplies the
    address: that is the ordinary two-channel split, where whatever provisions the host owns
    the address while the fleet publishes the cadence, the staleness bound and — the one
    that bites — ``on_unavailable``. Discarding such a declaration parsed it and then threw
    it away, so a fleet that had chosen ``degrade`` silently got the ``fail_closed`` default
    and aborted startup on the first outage. ``resolve_distribution`` overlays the
    environment onto whatever is returned here, so returning the settings is what lets the
    two channels combine at all.

    Deliberately does **not** validate the rest of the document.  This is a peek at
    one key, ahead of the tier that will parse the whole thing: a policy whose
    OTHER keys are malformed must still fail at its own tier with its own message,
    not here with a confusing one about distribution.  A malformed ``distribution``
    block itself does raise, because that block is what this function exists to
    read and a fleet that mistyped where its ceiling comes from must not silently
    get no central distribution at all.
    """
    if not isinstance(data, Mapping):
        return None
    raw = data.get("distribution")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PlatformCompositionError("security policy 'distribution' must be an object")
    declared = PolicyDistribution.from_dict(raw)
    if declared.enabled:
        return declared
    # Source-less, so only worth carrying when the environment names the address the
    # settings are for. ``from_dict`` already refuses a block with settings and no source
    # from either channel, which is the case that means "the author believed distribution
    # was on"; anything left here is an empty block, which is inert by any reading.
    if os.environ.get(_POLICY_DISTRIBUTION_URL_ENV, "").strip():
        return declared
    return None


def assert_policy_signature_satisfied(ceiling: Optional[GovernanceCeiling]) -> None:
    """Enforce ``require_policy_signature`` against a FINAL, selected ceiling.

    The single enforcement point for the opt-in.  :func:`_verify_policy_signature`
    only *computes* each tier's verdict at load time; this judges the one that
    actually survived precedence, and it judges both failure shapes:

    * a ceiling whose ``signature_state`` is not ``verified`` — someone either did
      not sign it or signed it with a key this trust root does not hold; and
    * **no ceiling at all** — absence must not satisfy a mandated signature, or a
      fleet that lost or never shipped its ``security_policy.json`` runs with no
      ceiling whatsoever, the exact failure the flag exists to prevent.

    Enforcing here rather than in the loader is what makes tier precedence work.
    ``load_security_policy`` walks env → companion bundle → operator home and runs
    more than once per boot with different arguments (the core with no
    ``bundled_loader``, an edition with one).  A raise inside it fires on whichever
    tier that particular pass happened to reach: the core's loader-less pass falls
    through to an unsigned HOME file and would abort even when the edition's later
    pass selects a correctly-signed BUNDLE that outranks it.  Only the composed
    result knows which tier won, so only the composed result can be judged.

    Callers: boot (on ``ctx.governance``) and every non-boot consumer that re-loads
    the policy on a live path.  With the flag off (the default, and what the
    ``amazon`` edition ships) this is a no-op — an unsigned policy still loads and
    still governs, which is the compatibility contract.
    """
    if ceiling is not None and ceiling.signature_state == SIGNATURE_VERIFIED:
        return
    if not _policy_signature_required():
        return
    if ceiling is None:
        _mark_policy_signature_incident("no-policy:require_policy_signature set")
        raise PlatformCompositionError(
            "require_policy_signature is set in the admission policy but no "
            "security policy is present at any tier (env, bundled, or home); "
            "boot is refused (fail-closed) rather than running ungoverned. "
            "Install a signed security_policy.json, or clear "
            "require_policy_signature to return to advisory verification."
        )
    _mark_policy_signature_incident(f"{ceiling.signature_state}:require set")
    raise PlatformCompositionError(
        f"the active security policy's signature is {ceiling.signature_state!r}, not "
        "'verified'; require_policy_signature is set in the admission policy, so "
        "boot is refused (fail-closed). Sign the policy with the trust key for its "
        "identity.issuer, or clear require_policy_signature to return to advisory "
        "verification."
    )


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
    mcp_ref: str = "",
    extra_titles: Tuple[str, ...] = (),
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

    ``mcp_ref`` supplies an ALREADY-canonical ``@server`` / ``@server/tool``
    reference for a caller that holds the server and tool as separate trusted
    fields; it is checked in the ``mcp`` scope alongside anything the title maps
    to.  Taking the reference directly is what makes such a call exact: the
    ``mcp__<server>__<tool>`` title form is read by splitting on the LAST ``__``,
    so it cannot represent a tool name containing ``__``, and a caller holding
    the untangled fields must not be made to encode them into a form that loses
    the distinction.

    ``extra_titles`` carries any further NON-model-authored names the caller holds
    for the SAME call (the trusted ``_meta.kiro.toolName`` beside a prose display
    title). They belong in this one call rather than a second one: a separate call
    re-resolves the active profile, so a hot reload between the two could answer
    each question from a different snapshot and permit a tool that both complete
    profiles deny. Empty entries are ignored.
    """
    pairs = list(classify_tool_title(tool_title))
    # Additional NON-model-authored titles the caller holds for the same call --
    # e.g. the trusted ``_meta.kiro.toolName`` beside an LLM-authored display
    # title. They are classified here, in ONE decision, rather than asked as
    # separate calls: each separate call would resolve the active profile again,
    # so a profile hot-reloaded mid-call could serve a DIFFERENT snapshot to each
    # question and a tool denied by both complete profiles could be permitted by
    # every individual lookup. One snapshot, every target, deny if any denies.
    # Empty entries are skipped: an empty title classifies to the unprefixed
    # scopes as a real queryable item (``('tools', '')``), not a no-op, so it
    # could match a rule it has nothing to do with.
    for extra in extra_titles:
        if extra:
            pairs.extend(classify_tool_title(extra))
    pairs.extend(classify_tool_args(tool_kind, raw_params))
    if mcp_ref:
        pairs.append(("mcp", mcp_ref))
    # Order-preserving dedupe -- a caller whose title already equals its trusted
    # identity must not pay the same resolve twice.
    pairs = list(dict.fromkeys(pairs))
    if not pairs:
        return Decision(True, "title not name-gate-governed", rule="default")
    # Deny if ANY governed scope the title/args map to denies it (the unprefixed
    # case maps to both commands+tools; an ungoverned scope permits, so this only
    # tightens).  Return the first denial for a precise audit reason.
    for scope, item in pairs:
        decision = resolve(ceiling, profile, scope, item)
        if not decision.permitted:
            # Name the identity that actually denied: with several targets in one
            # query the caller cannot infer it, and an audit naming the prose
            # title instead of the trusted tool name is a misleading record.
            return replace(decision, item=item)
    return Decision(True, "permitted by all mapped scopes", rule="rule2-intersect")


def resolve_pinned_commands(
    ceiling: Optional[GovernanceCeiling],
    profile: Optional[Profile] = None,
) -> Tuple[str, ...]:
    """Effective force-pinned command patterns = ceiling pins ∪ profile pins.

    Deny-mode denies compose by UNION (tightest-wins), so the effective pin set
    is the order-preserving, de-duplicated union of the ceiling's and the active
    profile's ``commands`` deny patterns. hooks.py calls this and unions the
    result into security.py's effective denied set so an enterprise/policy pin
    (and a bound profile's command deny) overrides the user's per-rule opt-out.
    Returns ``()`` for an ungoverned standalone host (ceiling and profile both
    None or neither governing ``commands``) — no behavior change there.
    """
    pins: Tuple[str, ...] = ()
    if ceiling is not None:
        pins = pins + ceiling.pinned_command_patterns()
    if profile is not None:
        pins = pins + profile.pinned_command_patterns()
    return _dedup(pins)


#: Host builtin tool name -> the governed scopes its calls are evaluated under.
#:
#: This is the POLICY-WRITE-TIME counterpart to :func:`classify_tool_args`. The
#: gate classifies a builtin from the real call (``tool_kind`` + ``raw_params``),
#: because that is the only reliable source for a path or a URL — but deciding
#: whether a tool may be put on an auto-approve list happens BEFORE any call
#: exists, so the only thing available is the name. Hence a name map, and hence
#: it lives here next to ``SCOPE_CATALOG`` rather than in a caller: a new governed
#: builtin is a data change in this file, which is the rule AGENTS.md states for
#: scopes. A parallel copy in an app-layer module silently re-opened the
#: auto-approve shortcut for anything the copy had not heard of.
BUILTIN_TOOL_SCOPES: Dict[str, Tuple[str, ...]] = {
    # `code` is the edit/LSP/AST-rewrite tool: it writes files AND can shell out
    # (format/build), and it is itself a governed tool. Mapping it to all three
    # scopes means a ceiling that constrains filesystem.write, commands, OR the
    # tool allow-list withholds its blanket auto-approve, so its edits go through
    # the PreToolUse gate. Missing entirely, it defaulted to auto-approved and a
    # `filesystem.write` ceiling could not reach it — unrestricted edits with no
    # permission request.
    "code": ("commands", "tools", "filesystem.write"),
    "execute_bash": ("commands",),
    "fs_read": ("filesystem.read",),
    "fs_write": ("filesystem.write",),
    "glob": ("filesystem.read",),
    "grep": ("filesystem.read",),
    "web_fetch": ("network.egress",),
    "web_search": ("network.egress",),
}

# The scopes whose enforcement is an ALWAYS-ON, ceiling-independent floor applied
# at the PreToolUse gate: sensitive-path blocking for filesystem tools and
# denied-command rules for shell tools. A builtin mapping to any of these must
# never receive a blanket auto-approve (which would skip that floor) — see
# may_skip_gate. `network.egress` is deliberately excluded: it is ceiling-only,
# not an always-on floor, so network builtins may still auto-approve absent a
# ceiling.
_FLOOR_GATE_SCOPES: frozenset[str] = frozenset({"filesystem.read", "filesystem.write", "commands"})


def _ceiling_mentions_mcp_server(ceiling: Optional[GovernanceCeiling], server: str) -> bool:
    """Whether the ``mcp`` ruleset names this server at ANY granularity.

    Reads the ruleset's own patterns rather than probing, because a probe cannot
    see a per-tool rule: ``@srv/delete`` matches only itself, so a sentinel tool
    sails past it. Matches ``@server`` and ``@server/<tool>``, case-insensitively,
    in both the allow and the deny list — an ALLOW-mode ruleset that lists only
    some of a server's tools is also an opinion, and auto-approving the whole
    server would grant the rest.

    A pattern scan deliberately does NOT answer "is this scope governed" — it
    answers "is THIS SERVER named". The empty-allowlist case (allow-mode with no
    patterns = deny-all) produces no patterns to match and is caught by the
    server-level ``gate_decision`` probe in :func:`may_skip_gate` BEFORE this
    runs; see ``_ruleset_has_an_opinion`` for why counting patterns is the wrong
    test for the scope-level question. Do not "fix" this function to return True
    on an empty ruleset: that would tax every server for a rule about one.
    """
    if ceiling is None:
        return False
    ruleset = ceiling.get("mcp")
    if ruleset is None:
        return False
    prefix = f"@{server}".casefold()
    patterns = tuple(getattr(ruleset, "allow", ()) or ()) + tuple(
        getattr(ruleset, "deny", ()) or ()
    )
    for pattern in patterns:
        candidate = str(pattern).strip().casefold()
        if candidate == prefix or candidate.startswith(prefix + "/"):
            return True
    return False


def _ruleset_has_an_opinion(rules: object) -> bool:
    """Whether a scope's ruleset constrains anything at all.

    Counting patterns is NOT the test, and getting that wrong inverts the answer
    in the strictest case there is: under ``mode="allow"`` an EMPTY allowlist is
    the empty set — deny-all — so a ruleset with no patterns is MAXIMALLY
    governed, while a pattern-count check reads it as "no opinion" and hands out
    the auto-approve exemption. See :class:`ScopedRuleset`.

    * allow-mode → always an opinion (an empty allowlist denies everything).
    * deny-mode with patterns → an opinion.
    * deny-mode with no patterns → genuinely permits everything; no opinion.
    """
    if rules is None:
        return False
    mode = str(getattr(rules, "mode", "") or "").strip().lower()
    if mode == MODE_ALLOW:
        return True
    if mode == MODE_DENY:
        return bool(tuple(getattr(rules, "deny", ()) or ()))
    # An unrecognized shape is not proof of safety — treat it as an opinion so
    # the call goes through the gate rather than skipping it.
    return True


def may_skip_gate(ref: str, ceiling: Optional[GovernanceCeiling]) -> bool:
    """Whether an auto-approve entry may bypass the PreToolUse gate.

    The ONE predicate every writer of an ``allowedTools`` list must consult.
    Auto-approve is the single path that never reaches ``hooks.on_tool_call``, so
    an entry written without asking this question is a tool the ceiling can no
    longer refuse — which is exactly the guarantee the governance docs make. Two
    independent writers produce those lists (app-agent materialization and the
    host agent's shared-MCP sync); closing the bypass in one of them protects one
    agent and leaves the other with the hole, so both call this.

    ``ref`` is what actually appears in such a list:

    * ``@server`` / ``@server/tool`` — an MCP reference.
    * a bare tool name (``fs_read``, ``web_fetch``) — a host builtin.

    Coarser than the gate ON PURPOSE. Auto-approve has only whole-server (and
    whole-tool) granularity, while the ceiling may speak per tool, per path or
    per host — arguments that do not exist yet at write time. So the question here
    is only "could the ceiling ever have something to say about this", and if it
    could, the shortcut is declined and the real decision is left to the gate,
    which has the real arguments.

    Returns True (keep the entry) for an ungoverned host, so nothing changes
    without a ceiling — EXCEPT a BUILTIN whose always-on floor (sensitive-path /
    denied-command) is enforced at the gate, which is withheld even with no
    ceiling (see the floor check below). That exception is builtin-only on
    purpose: the sensitive-path / denied-command floor runs at the gate against
    the builtin ``fs_read`` / ``fs_write`` / ``execute_bash`` / ``code`` tool
    arguments. An MCP ``@server`` is a separate process whose file/command access
    the builtin floor never inspects, so on an ungoverned host an ``@server``
    keeps its grant and ONLY a ceiling's argument-derived scopes
    (``filesystem.*`` / ``network.egress``) constrain it — there is no
    ceiling-independent floor to bypass for a server. Returns False on an
    evaluator ERROR: not being able to tell cannot mean "skip the control",
    because there is no later checkpoint on that path — the cost of asking is one
    permission prompt, and no capability is lost.
    """
    # Defensive: a non-string ref (a malformed, hand-edited config) cannot be a
    # valid tool/@server reference — treat it as "do not auto-approve" rather
    # than crash on ref.startswith() below. The list writers also discard such
    # entries, but this keeps the predicate safe for any caller.
    if not isinstance(ref, str):
        return False
    # A builtin whose MANDATORY, ceiling-INDEPENDENT floor lives at the
    # PreToolUse gate must NEVER be auto-approved: auto-approve is the one path
    # that skips hooks.on_tool_call, and that floor (sensitive-path blocking for
    # filesystem tools — ~/.ssh, ~/.aws, ...; denied-command rules for shell
    # tools) is always-on and un-disableable. Skipping it would expose
    # credentials/run denied commands even with NO enterprise ceiling present.
    # Network-only builtins (web_fetch/web_search) carry no such floor, and
    # read-only file tools still auto-approve via hooks AFTER the floor runs — so
    # this only closes the bypass, it does not add prompts.
    if not ref.startswith("@") and _FLOOR_GATE_SCOPES.intersection(
        BUILTIN_TOOL_SCOPES.get(ref, ())
    ):
        return False
    if ceiling is None:
        return True
    try:
        if ref.startswith("@"):
            server = ref[1:].split("/", 1)[0]
            if not server:
                return False
            # Server-level first: the gate sees MCP tools as
            # ``mcp__<server>__<tool>``, so a sentinel classifies to
            # ``@server/probe`` and any server-level pattern (including a
            # wildcard, which a literal scan would miss) covers it.
            if not gate_decision(ceiling, None, f"mcp__{server}__probe").permitted:
                return False
            # Then per-tool rules, which the sentinel cannot see.
            if _ceiling_mentions_mcp_server(ceiling, server):
                return False
            # An MCP tool can also read/write files or reach the network, and the
            # gate enforces those scopes from the REAL call arguments
            # (classify_tool_args: the edit path, the fetch host) — arguments the
            # server-name probe cannot carry. Auto-approving the server skips that
            # gate, so if the ceiling has an opinion on ANY argument-derived scope,
            # withhold and let the gate apply it against the real path/host.
            arg_scopes = ("filesystem.read", "filesystem.write", "network.egress")
            if any(_ruleset_has_an_opinion(ceiling.get(_s)) for _s in arg_scopes):
                return False
            return True

        # The generic `tools` scope governs tool NAMES, so it applies to EVERY
        # builtin — including one absent from the capability map. Without this an
        # unmapped tool (introspect / session / report / any future builtin)
        # returned True unconditionally and its shipped `allowedTools` grant
        # bypassed a `tools`-scope ceiling (e.g. tools.deny=["report"]). The
        # capability scopes it DOES map to add path/host granularity on top.
        scopes = ("tools",) + tuple(BUILTIN_TOOL_SCOPES.get(ref, ()))
        return not any(_ruleset_has_an_opinion(ceiling.get(scope)) for scope in scopes)
    except Exception:  # noqa: BLE001 — see the fail-closed note above
        logger.warning("ceiling probe failed for %r; not auto-approving", ref, exc_info=True)
        return False


def strip_ungoverned_auto_approve(servers: Mapping[str, object]) -> Dict[str, object]:
    """Return ``servers`` with a ceiling-governed ``autoApprove`` removed.

    ``autoApprove`` is the OTHER route to the exemption ``allowedTools`` grants,
    and a more direct one: kiro-cli approves an autoApproved MCP tool locally and
    emits no permission request, so ``hooks.on_tool_call`` never runs for it.
    ``agent.py``'s managed-server block states the rule for the servers KiroCrew
    ships ("DELIBERATELY NO autoApprove KEY, and none may ever be added"); this
    applies it to every other source.

    A MAP-level helper on purpose. The key can arrive from an app manifest, a
    per-agent MCP policy, a managed server spec, or an imported config from
    another tool — and each fix that covered only the reported source left the
    others open. Both writers of an agent config (the host's ``install_agent``
    and app-agent materialization in ``apps/bridges.py``) run their final server
    map through here, so a new source is covered the moment it lands in that map
    rather than needing its own patch.

    Only the key is dropped, never the server: the tools stay available and go
    through the approval gate, which is where a per-tool ceiling rule is applied.
    Unchanged on an ungoverned host.
    """
    out: Dict[str, object] = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict) or "autoApprove" not in spec:
            out[name] = spec
            continue
        if may_skip_gate_now(f"@{name}"):
            out[name] = spec
            continue
        trimmed = dict(spec)
        trimmed.pop("autoApprove", None)
        logger.info(
            "Dropped autoApprove from MCP server %s: the governance ceiling "
            "constrains it, so its tools go through the approval gate",
            name,
        )
        # Revoking a gate exemption is a permission DECISION — the allowedTools
        # writers emit this same SEL event, so a silent pop here would be the one
        # withhold path with no audit trail. Best-effort; never break a rebuild.
        try:
            sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="strip_ungoverned_auto_approve",
                resources=(
                    f"@{name} autoApprove removed (governance ceiling); "
                    "calls go through the approval gate"
                ),
            )
        except Exception:  # noqa: BLE001 — audit must not break the filter
            logger.debug("SEL audit unavailable for autoApprove strip", exc_info=True)
        out[name] = trimmed
    return out


def may_skip_gate_now(ref: str) -> bool:
    """:func:`may_skip_gate` against the CURRENTLY installed ceiling.

    The entry point every ``allowedTools`` writer should call: it folds in the one
    piece each of them would otherwise re-implement — resolving the ceiling — and
    it fails CLOSED. There are five such writers (the host agent's shared-MCP
    sync, app-agent materialization, two dashboard MCP-enable paths and doctor's
    auto-fix), and every one of them independently produces the single state the
    PreToolUse gate can never see. A per-caller copy is how they diverged: the
    first fix closed one and left the other four open.

    An unreadable ceiling is deliberately NOT treated like an absent one. ``None``
    from ``current_context()`` means "ungoverned host, keep every grant"; an
    exception means "there may be a ceiling and we cannot read it", and answering
    "skip the gate" to that is the bypass itself.
    """
    try:
        from kiro_crew.platform.context import current_context

        ceiling = getattr(current_context(), "governance", None)
    except Exception:  # pragma: no cover - defensive
        logger.warning("cannot resolve the governance ceiling; not auto-approving %r", ref)
        return False
    if not may_skip_gate(ref, ceiling):
        return False
    # The POLICY ceiling permits (or is absent) — but governance is
    # ``policy ∩ profile``, and a Level-2 PROFILE can govern this ref even with NO
    # ceiling (a per-app profile denying ``@srv/delete``). A static allowedTools
    # writer has no surface to resolve the single active profile, so withhold the
    # shortcut if ANY configured profile could govern the ref; the runtime gate
    # then applies the specific one with the real arguments.
    try:
        from kiro_crew.platform.governance_profiles import any_configured_profile_governs

        if any_configured_profile_governs(ref):
            return False
    except Exception:  # pragma: no cover - defensive; cannot tell -> withhold
        logger.warning("cannot evaluate profiles for %r; not auto-approving", ref)
        return False
    return True


def sanitize_agent_config_governance(config: MutableMapping[str, object]) -> None:
    """In-place: strip ceiling-governed auto-approve grants from a full agent
    config about to be written to ``kirocrew.json``.

    THE filter for writers that persist the WHOLE config object (the dashboard
    ``PUT /api/agent/config`` handler, the browser-setup Playwright convergence)
    rather than mutating one ``allowedTools`` entry at a time. Those writers
    escaped the per-ref writer inventory precisely because they assign
    ``config["allowedTools"] = [...]`` / ``config["mcpServers"] = {...}`` wholesale
    — so a governed ``@denied`` ref or a governed server's ``autoApprove`` written
    through them restored the very bypass the per-ref writers close. Every
    whole-config writer MUST call this immediately before it persists, so no
    future writer can reopen the surface.

    Drops non-string and ceiling-governed ``allowedTools`` entries (same rule and
    fail-closed semantics as ``may_skip_gate_now``) and removes ``autoApprove``
    from any governed MCP server (via ``strip_ungoverned_auto_approve``). ``tools``
    (mount, not auto-approve) is left intact. A no-op on an ungoverned host.
    """
    allowed = config.get("allowedTools")
    if isinstance(allowed, list):
        kept: list[str] = []
        withheld: list[str] = []
        for ref in allowed:
            if not isinstance(ref, str):
                continue  # non-string junk is not a valid ref — drop silently
            (kept if may_skip_gate_now(ref) else withheld).append(ref)
        config["allowedTools"] = kept
        if withheld:
            # Withholding a grant is a permission DECISION — every other
            # allowedTools writer emits this event, so a silent drop here would
            # be the one withhold path with no audit trail. Best-effort.
            try:
                sel().log_api_access(
                    caller="system",
                    operation="mcp_auto_approve_withheld",
                    outcome="ok",
                    source="sanitize_agent_config_governance",
                    resources=(
                        f"{', '.join(withheld)} removed from allowedTools "
                        "(governance ceiling); calls go through the approval gate"
                    ),
                )
            except Exception:  # noqa: BLE001 — audit must not break the write
                logger.debug("SEL audit unavailable for config sanitize", exc_info=True)
    servers = config.get("mcpServers")
    if isinstance(servers, dict):
        config["mcpServers"] = strip_ungoverned_auto_approve(servers)


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

    # The data home moved to ~/.kiro/crew; check the CURRENT home's trust-root
    # paths are gated (the legacy ~/.kirocrew forms remain in the keystone too,
    # but a fresh install only ever writes the new home, so that is what the boot
    # integrity check must assert).
    required = (
        ".kiro/crew/security_policy.json",
        ".kiro/crew/profiles",
        ".kiro/crew/admission_policy.json",
        # Denied-command opt-out ceiling — the agent must not be able to write
        # its own deny opt-out state (would let it disable the deny gate).
        ".kiro/crew/denied_commands.json",
        # The centrally-distributed ceiling's cache. On this list for the same
        # reason as the policy itself and then one more: the cached metadata records
        # the source the copy came from, and the loader trusts that record when
        # deciding whether the cache is this host's last-known-good. An agent that
        # could write here would not need to touch ``security_policy.json`` to
        # replace its own ceiling — it would publish itself one, with provenance.
        f".kiro/crew/{_POLICY_CACHE_LEAF}",
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
    # Union the diagnostic records so an ``extends`` chain does not lose the fact
    # that a link in it named a capability this build cannot govern.
    return Profile(
        name=child.name,
        bind=child.bind or parent.bind,
        extends="",
        controls=merged,
        unknown_scopes=_dedup(parent.unknown_scopes + child.unknown_scopes),
    )


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
    "UpdatePins",
    "active_update_pins",
    "PolicyDistribution",
    "active_policy_distribution",
    "UNAVAILABLE_FAIL_CLOSED",
    "UNAVAILABLE_DEGRADE",
    "MIN_REFRESH_INTERVAL_SECS",
    "DEFAULT_FETCH_TIMEOUT_SECS",
    "MAX_POLICY_BYTES",
    "Profile",
    "Bind",
    "POLICY_VERSION",
    "MODE_ALLOW",
    "MODE_DENY",
    "COMMANDS_SCOPE",
    "SIGNATURE_VERIFIED",
    "SIGNATURE_UNVERIFIED",
    "SIGNATURE_UNSIGNED",
    "SIGNATURE_UNCHECKED",
    "policy_signing_payload",
    "CU_MCP_SERVER",
    "CU_CLASS_OBSERVE",
    "CU_CLASS_MUTATE",
    "CU_CLASS_POINTER",
    "CU_CLASS_KEYBOARD",
    "CU_CLASS_TEXT_ENTRY",
    "CU_CLASS_CONTROL",
    "computer_use_action_classes",
    "computer_use_action_from_title",
    "is_computer_use_title",
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
    "resolve_pinned_commands",
    "resolve_ordinal",
    "gate_decision",
    "assert_governance_floor",
    "assert_governance_paths_protected",
    "assert_policy_signature_satisfied",
    "compose_profiles",
]
