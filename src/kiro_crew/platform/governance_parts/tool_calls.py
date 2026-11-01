"""What one tool call touches, as governed (scope, item) pairs.

The code-owned computer-use action classes (``_CU_ACTION_CLASSES``,
``computer_use_action_classes``), the classification of a gate tool title
(``classify_tool_title``, ``mcp_title_to_ref``) and of a builtin call's arguments
(``classify_tool_args``). The evaluator asks these which scopes a call is judged under.
A new way of reading a tool call's target goes here.

Served through :mod:`kiro_crew.platform.governance`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping, Optional, Tuple

from kiro_crew.platform.governance_parts.matching import _url_host
from kiro_crew.platform.guest_paths import (
    GuestPath,
    guest_also_local,
    guest_root,
    guest_unverifiable,
)
from kiro_crew.platform.tool_paths import (
    TARGET_PATH_KEYS,
    edit_target_candidates,
    is_edit_call,
    target_paths,
)

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
    # Control plane: drops Kiro Crew's OWN cached snapshots and touches no other
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

#: Same never-permittable construction, for a file-EDIT whose diff content block
#: names a path that is still RELATIVE after ``~``/env expansion.  Such a path
#: resolves against the gateway process CWD, not the agent workspace, so no
#: allow-mode confinement can be verified against it.  Emitting the marker means
#: an operator ceiling that governs ``filesystem.write`` DENIES the unverifiable
#: edit, while an ungoverned scope still permits (standalone default preserved).
_UNANCHORED_TARGET_ITEM = "\x00<governance:edit-target-unanchored-unverifiable>"

#: Same never-permittable construction, for a remote-workspace target whose
#: spelling another reader could resolve to a different file (a ``$``, a ``~``
#: segment, a backslash) or that cannot be anchored in the guest. It is emitted
#: BESIDE the literal guest path, not instead of it: a deny is still checked against
#: the path the guest opens, and an allow-mode confinement additionally refuses.
_GUEST_UNVERIFIABLE_ITEM = "\x00<governance:guest-target-unverifiable>"


#: Same never-permittable construction, for a scope a proven host built-in is
#: judged in (``gate_decision``'s ``own_scopes``) whose arguments name no target
#: there: a pathless ``grep`` searches a directory the gate cannot see. An
#: allow-mode confinement refuses it; a deny-mode or ungoverned scope permits it.
#: Absolute on purpose: the path matcher anchors a relative item at the gateway's
#: working directory, which would put a relative marker inside a workspace
#: allow-prefix whenever the gateway runs there.
_NO_TARGET_ITEM = "/\x00<governance:own-scope-target-absent-unverifiable>"


def _governed_targets(
    paths: Iterable[str], root: Optional[str], also_local: bool = False
) -> Tuple[str, ...]:
    """The governed items for *paths*: unchanged for a local call (``root`` None).

    For a remote-workspace call each path becomes a :class:`GuestPath` anchored in
    the guest workdir *root*, matched with POSIX rules, and any one
    :func:`guest_unverifiable` adds :data:`_GUEST_UNVERIFIABLE_ITEM` once. With
    *also_local* each plain path is kept beside its guest twin, so a call the
    builder could not place for certain must pass every rule under both readings.
    """
    paths = tuple(paths)
    if root is None:
        return paths
    items: list = [GuestPath(path, root) for path in paths]
    if any(guest_unverifiable(item) for item in items):
        items.append(_GUEST_UNVERIFIABLE_ITEM)
    if also_local:
        items[:0] = paths
    return tuple(items)


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
    tool_kind: str,
    raw_params: Optional[Mapping[str, object]],
    diff_path: str = "",
) -> Tuple[Tuple[str, str], ...]:
    """Map a tool's semantic ``kind`` + real arguments to ``(scope, item)`` pairs.

    This is the args-based companion to :func:`classify_tool_title`.  The display
    title is backend-variable and unreliable for extracting a path or URL, but the
    ACP event carries the tool's ``kind`` and ``raw_tool_params`` — the
    authoritative signal.  Used by the gate to enforce the path/host scopes that a
    title cannot carry:

    * ``kind == "edit"`` → ``("filesystem.write", "<path>")`` for every path in
      the edit's judged target set: the UNION of the params' path spellings and
      *diff_path*, the path the tool call's diff content block named
      (:func:`kiro_crew.platform.tool_paths.edit_target_candidates`, the same
      single source the hooks edit gate consumes).  A backend may stream trusted
      params that carry no path key and name the file only in that block, so
      classifying the params alone would hand an ALLOW-mode ``filesystem.write``
      confinement a pathless edit — the write it exists to confine would never
      be asked.  A *diff_path* still relative after ``~``/env expansion emits
      ``_UNANCHORED_TARGET_ITEM`` (see below) instead of a path.
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

    **Unanchored diff path — the same needle, threaded the same way.**  A
    relative diff-block path resolves against the process CWD, so its membership
    in an allow-list cannot be established; ``edit_target_candidates`` withholds
    it and sets ``unanchored``, and this plane emits
    ``("filesystem.write", _UNANCHORED_TARGET_ITEM)``: a governed
    ``filesystem.write`` scope denies the unverifiable edit, an ungoverned one
    permits.  (The hooks edit gate additionally hard-denies the unanchored shape
    outright for its callers — this marker is the governance plane's own
    fail-safe reading, not the primary deny.)

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

    **Remote-workspace targets.**  When *raw_params* are a remote workspace's
    call arguments (:class:`~kiro_crew.platform.guest_paths.GuestPathParams`,
    tagged by ``build_permission_event`` for a call the runtime that attached the
    workspace identified), every path target is emitted as a
    :class:`~kiro_crew.platform.guest_paths.GuestPath`, which the ``path``
    matcher normalizes with POSIX rules in the guest workdir rather than this
    gateway's ``os.path`` -- so a ``/workspace/app/**`` rule binds the guest's
    file on a Windows gateway too. A target the guest would read literally but
    another reader might not (``$``, a ``~`` segment, a backslash) or that
    cannot be anchored also emits ``_GUEST_UNVERIFIABLE_ITEM``: the literal path
    still meets every deny, and an allow-mode confinement refuses the call.
    """
    params = raw_params if raw_params and isinstance(raw_params, Mapping) else None
    # A remote workspace's call names guest files: each target is minted as a
    # GuestPath so the path matcher reads it with POSIX rules (see
    # ``_governed_targets``). ``None`` for every other call, which keeps its
    # targets as the plain strings the gateway's own rules match.
    root = guest_root(raw_params)
    also_local = guest_also_local(raw_params)
    if is_edit_call(tool_kind, diff_path):
        # The edit's judged target set is the params∪diff-block union, from the
        # same helper both edit gates consume — a diff-only edit (params carry
        # no path key, or no params at all) is classified by the diff block's
        # path rather than reaching an ALLOW-mode confinement pathless. The
        # route is ``is_edit_call``: a diff content block is write-plane
        # evidence whatever the spec-optional ``kind`` field says, so a
        # kindless (or mislabelled) call carrying one is classified here too.
        candidates = edit_target_candidates(params, diff_path, tool_kind=tool_kind)
        targets = _governed_targets(candidates, root, also_local)
        edit_pairs: list = [("filesystem.write", path) for path in targets]
        if candidates.truncated:
            edit_pairs.append(("filesystem.write", _TRUNCATED_SCAN_ITEM))
        if candidates.unanchored:
            edit_pairs.append(("filesystem.write", _UNANCHORED_TARGET_ITEM))
        if tool_kind != _KIND_EDIT:
            # A kindless call routed here by its diff block also keeps the
            # read pairs the shape-inference fallback applies to kindless
            # paths — additive only, so no call loses a pair.
            for path in targets:
                edit_pairs.append(("filesystem.read", path))
        # ADDITIVE for every other classified dimension too: routing a call
        # here because its frame carried a diff block must never DROP a pair
        # the pre-route classification would have emitted. A call that also
        # carries a ``url`` param keeps its ``network.egress`` pair, so a
        # governed egress ceiling still binds it (a fetch-kind or kindless
        # call cannot shed egress governance by arriving with a diff block).
        if params is not None:
            edit_url = params.get("url") or params.get("uri")
            if isinstance(edit_url, str) and edit_url:
                edit_host = _url_host(edit_url)
                if edit_host:
                    edit_pairs.append(("network.egress", edit_host))
        return tuple(edit_pairs)
    if params is None:
        return ()
    pairs: list = []
    found, paths_truncated = _tool_arg_paths(params)
    paths = _governed_targets(found, root, also_local)
    url = params.get("url") or params.get("uri")
    has_command = bool(params.get("command"))  # a shell tool → commands scope
    if tool_kind == _KIND_READ:
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
