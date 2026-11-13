"""Which agents a parent's spec lets it spawn, and the roster a refusal or a hint lists.

The ``toolsSettings.subagent.availableAgents`` allowlists of a parent's spec at each scope
that holds one, the glob match an agent name is held to, and ``visible_agent_names``, the
one filter every roster shown to a model passes through.

This owner also works on its own, before ``kiro_crew.subagent`` is loaded: it imports for
real every name its functions read, from the module that defines each. A stdio tool server
names the agents a caller may spawn in a tool description it writes before it has loaded
the code that runs its tools, and that code is where the facade loads
(``kiro_crew.subagent_standalone``). Once the facade is loaded these same functions run on
its globals, like every owner's.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Container, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from kiro_crew.agent_discovery import (
    AgentsDirMemo,
    _kiro_agents_dir,
    _read_agent_spec,
    plain_markdown_document,
    read_agent_spec_strict,
)
from kiro_crew.agent_spec_format import is_markdown_spec, iter_agent_spec_files
from kiro_crew.config.paths import project_agents_dir
from kiro_crew.platform.context import redact_via_context
from kiro_crew.sel import sel
from kiro_crew.validation import _AGENT_NAME_RE, is_registered_agent_name

#: The facade's logger: a function here logs under one name whichever globals it runs on.
logger = logging.getLogger("kiro_crew.subagent")


#: Agent names a roster never suggests: the host default and the conductors are
#: reached by OMITTING ``agent``, not by naming one. Every roster inherits this
#: as :func:`visible_agent_names`' default ``exclude``, so no other module names
#: the set and it cannot drift when a reserved name appears.
UNADVERTISED_AGENTS = frozenset(
    {
        "kirocrew",
        "kirocrew-conductor",
        "kirocrew-pipeline-conductor",
        "kirocrew-security-conductor",
    }
)


def visible_agent_names(
    names: Iterable[str],
    *,
    exclude: Container[str] = UNADVERTISED_AGENTS,
    limit: int | None = None,
) -> tuple[list[str], int]:
    """Make a roster of agent names safe to render, and bound it.

    Returns ``(shown, withheld)``: the names that may be rendered, and how many
    the *limit* dropped (``0`` when nothing was dropped, so a caller appends its
    "+N more" only when there is a remainder to report).

    Three surfaces render this roster -- an unknown-agent refusal, the spawn
    tools' parameter descriptions, and ``spawn_list``'s output -- and all three
    come through the pipeline below rather than re-implementing it. Duplicated
    copies drift, so the SAFETY half lives here where a fourth surface cannot
    omit it:

    * **Grammar.** Every name must match the registered-agent grammar
      (``is_registered_agent_name``: the slot grammar or a published dotted
      template) before it is rendered. This is the load-bearing filter, not a tidiness check: an agent
      spec's ``name`` field is taken verbatim by
      ``agent_discovery._global_agent_info`` with no validation, so a spec can
      declare a name containing a newline plus instruction-shaped text -- which
      is pure ASCII, so an ``isascii`` check passes it -- and it would ride this
      string into a model's context. ``SPAWN_RUN_SCHEMA`` gates the ``agent``
      parameter on the same grammar, so a name that fails it could never have
      been dispatched anyway: offering it would advertise an unusable name.
    * **Redaction.** A grammar-valid name can still be credential-shaped (an
      AWS access key is pure alphanumerics), so each name goes through the
      canonical context-aware shim rather than being trusted.
    * **Bound.** Every rendered roster that reaches always-on context is capped,
      and the remainder is returned as a count instead of being silently lost.

    Order is the CALLER's: this returns names in the order it received them, so
    each surface keeps the presentation its own copy promises. *exclude* defaults
    to the reserved pair (reached by omitting ``agent``, never by naming one);
    ``spawn_list`` passes an empty set on purpose, because the two bounded
    rosters point at it as the surface that lists everything.
    """
    kept = [
        redact_via_context(n)
        for n in names
        if n and n not in exclude and is_registered_agent_name(n)
    ]
    if limit is None or len(kept) <= limit:
        return kept, 0
    return kept[:limit], len(kept) - limit


#: Bounds on a spec's ``toolsSettings.subagent.availableAgents`` list as
#: RETAINED by :func:`spawn_allowlist`: patterns beyond the count, and any
#: pattern longer than the character cap, are dropped (never matched, so the
#: overflow fails closed) and logged. The list is matched on the event loop on
#: every spawn, and the spec reader's only other ceiling is the file size cap.
#: 64 characters is the agent-name alphabet's own cap (``_AGENT_NAME_RE``,
#: ``_AGENT_GLOB_RE``); 256 patterns is far beyond any real roster.
_MAX_AVAILABLE_AGENTS_GLOBS = 256


_MAX_AVAILABLE_AGENTS_GLOB_CHARS = 64


def spawn_allowlist(spec: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The ``toolsSettings.subagent.availableAgents`` globs *spec* declares.

    This is kiro-cli's own key, defined for its built-in ``subagent`` tool: "glob
    patterns for agents this agent can spawn; omit to allow all". Kiro Crew's
    sub-agents come through ``spawn_run`` instead, so the same declaration is
    honoured at the spawn gate rather than silently ignored.

    Returns ``None`` when the key is OMITTED -- that is "allow all", and it is
    the only value under which the gate stays out of the way, so every spec that
    never wrote the key keeps the behaviour it had. A declared list is returned
    as written (non-string entries dropped), bounded to the first
    :data:`_MAX_AVAILABLE_AGENTS_GLOBS` patterns of at most
    :data:`_MAX_AVAILABLE_AGENTS_GLOB_CHARS` characters each -- the overflow is
    dropped and logged, which only narrows what is allowed. A declared value
    that is not a list is an EMPTY allowlist: the operator meant to restrict and
    the shape is wrong, so nothing is allowed rather than everything.

    ``trustedAgents``, the sibling key, is NOT an allowlist and is not read
    here: upstream it means "run these sub-agents without permission prompts",
    and reading it as a restriction would refuse spawns an operator never meant
    to forbid.
    """
    settings = spec.get("toolsSettings")
    if not isinstance(settings, Mapping):
        return None
    # The one key of the sub-agent alias group kiro-cli reads (``crew``,
    # ``agentCrew``, ``subagent``, ...), the same one the KAS projection reads.
    from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

    settings, _ = canonical_tools_settings(dict(settings))
    # The fold files every sub-agent spelling under `subagent`.
    subagent = settings.get("subagent")
    if not isinstance(subagent, Mapping) or "availableAgents" not in subagent:
        return None
    declared = subagent.get("availableAgents")
    if not isinstance(declared, (list, tuple)):
        return ()
    globs = tuple(entry for entry in declared if isinstance(entry, str) and entry)
    # Bound what is retained: the list is matched on the event loop on every
    # spawn (one ``fnmatchcase`` compile per pattern, then a regex pass and a
    # sort on the denial path), and the spec reader's only ceiling is the file
    # size cap, so a valid-size spec could carry hundreds of thousands of
    # patterns and stall the loop past the watchdog. Dropping entries only ever
    # NARROWS what is allowed, so the overflow fails closed: the first
    # ``_MAX_AVAILABLE_AGENTS_GLOBS`` patterns of at most
    # ``_MAX_AVAILABLE_AGENTS_GLOB_CHARS`` characters each are kept, the rest
    # are logged and refused.
    kept = tuple(
        g for g in globs[:_MAX_AVAILABLE_AGENTS_GLOBS] if len(g) <= _MAX_AVAILABLE_AGENTS_GLOB_CHARS
    )
    if len(kept) != len(globs):
        logger.warning(
            "availableAgents declares %d glob(s); keeping %d (limit %d patterns of at most %d "
            "chars each), the rest are not matched (fail-closed)",
            len(globs),
            len(kept),
            _MAX_AVAILABLE_AGENTS_GLOBS,
            _MAX_AVAILABLE_AGENTS_GLOB_CHARS,
        )
    return kept


def agent_matches_allowlist(agent: str, allowlist: Iterable[str], *, app: str = "") -> bool:
    """kiro-cli's glob semantics for ``availableAgents``: case-sensitive fnmatch.

    *app* is the VERIFIED identity of the calling app (``execution.app``, bound
    by the SpawnSDK and re-proved by ``_validate_app_agent_ownership``), never a
    prefix parsed out of *agent*. Only when *agent* is that app's own
    materialized ``<app>--<name>`` is the bare ``<name>`` tried as well, because
    that is the name the app's spec lists and kiro-cli's own gate matches. A
    ``--`` in any other name is just part of the name: an installed
    ``rogue--reviewer`` does not satisfy a ``reviewer``-only allowlist.
    """
    candidates = {agent}
    if app and agent.startswith(f"{app}--"):
        candidates.add(agent[len(app) + 2 :])
    return any(
        fnmatch.fnmatchcase(candidate, pattern) for pattern in allowlist for candidate in candidates
    )


def parent_spawn_allowlists(
    parent_template: str,
    project_dirs: Sequence[str | Path] = (),
    *,
    parent_session_key: str = "",
) -> tuple[tuple[str, ...], ...] | None:
    """Every ``availableAgents`` list the specs naming *parent_template* declare.

    *project_dirs* are the directories the PARENT session's kiro-cli may be
    running in (:func:`parent_project_dirs`). For each,
    ``<project_dir>/.kiro/agents`` is resolved too, by the same rules and the
    same hardened reader under its own SEL label, and its lists are ADDED to the
    user-level ones: the target must satisfy every list from every scope. kiro-cli lets a project spec shadow a
    user-level one of the same name; the gate does not pick the winner, it
    applies both, so honouring the project scope can only narrow what a parent
    may spawn, never widen it. ``None`` from either scope is ``None``.

    *parent_session_key* is used only to audit the case this function cannot
    resolve: no project directory is known for the parent and no user-level spec
    names it, so a project-scope declaration may exist that is not honoured
    (:func:`_surface_unresolved_parent_spec`).

    Resolution follows :func:`kiro_crew.agent.agent_spec_path`: a spec whose
    DECLARED ``name`` is *parent_template* wins; ``<parent_template>.json`` (or
    ``.md``) is read only when no spec declares the name. Two specs declaring
    the same name are both kept -- the target must then satisfy each, which is
    tightest-wins rather than a guess at which one kiro-cli loaded.

    ``()`` when *parent_template* is empty, names no spec, or its spec omits the
    key: each of those is "no declaration to honour", the unchanged case.

    ``None`` means the answer is UNKNOWN and the caller must refuse: the
    directory exists but cannot be scanned (probed before the snapshot read,
    which folds a walk failure into "no specs"), or no readable spec declares
    the name while some spec file in the directory could not be read under the
    hardened reader (invalid JSON, oversized, a broken link, a symlink to a
    sensitive target). kiro-cli resolves a name by its DECLARED ``name`` first,
    under any filename, so the parent's declaration may be inside exactly the
    file that did not parse; the reader folds it into "no spec", and a
    restrictive declaration inside it would otherwise fail OPEN. A parent whose
    declaration IS readable is never refused by an unrelated broken file. The
    cost is that a parent with no user-level spec at all (a project-scope or
    edition agent) is refused too while any spec file in the directory is
    unreadable; the refusal names the file, and repairing or removing it
    restores admission. AppleDouble ``._`` sidecars, which the reader rejects by
    design, do not count.

    Reads the user-level agents directory with one fresh walk through the
    hardened reader (:func:`_scan_parent_spawn_allowlists`), memoized per
    directory in :data:`_PARENT_ALLOWLIST_MEMO` and pinned to the directory's
    :func:`kiro_crew.agent_discovery.agents_dir_revision` -- the fingerprint
    that refuses to pin whenever freshness cannot be proven. The catalog
    snapshots are NOT used: ``parsed_agent_specs`` revalidates on entry names
    and mtime only, so an mtime-preserving rewrite of a parent's spec
    (``cp -p``, ``rsync -t``) would serve its previous, permissive allowlist for
    ever; ``cached_agent_specs`` serves EMPTY rows on a cold cache, and an empty
    read here would admit a spawn the declaration forbids. It is a blocking
    read, so the event-loop entry points call it through ``asyncio.to_thread``
    (see :func:`parent_spawn_policy`) rather than from the gate. A parent living
    only in a project's ``.kiro/agents`` is not resolved here (its declaration
    is resolved only when *project_dirs* is given -- see :func:`parent_project_dirs`.
    """
    return _resolve_parent_spawn_allowlists(
        parent_template, project_dirs, parent_session_key=parent_session_key
    )[0]


#: :func:`parent_spawn_allowlists`'s answer together with the spec files that
#: made it unknown, as :func:`_describe_unreadable_specs` describes them
#: (``()`` for every answer that is not ``None``). The descriptions travel with
#: the answer, in the :data:`ParentSpawnPolicy` the gate receives, so a
#: refusal names the files of its own resolution and no other session's.
_ParentSpawnResolution = tuple["tuple[tuple[str, ...], ...] | None", "tuple[str, ...]"]


def _resolve_parent_spawn_allowlists(
    parent_template: str,
    project_dirs: Sequence[str | Path] = (),
    *,
    parent_session_key: str = "",
) -> _ParentSpawnResolution:
    """:func:`parent_spawn_allowlists`, plus the unreadable files behind a ``None``."""
    # ``match``, not ``fullmatch``: the pattern is anchored, and the roster
    # ratchet reserves the ``fullmatch`` spelling for ``visible_agent_names``.
    if not parent_template or not _AGENT_NAME_RE.match(parent_template):
        return (), ()
    user_lists, user_found, unreadable = _scope_spawn_allowlists(
        _kiro_agents_dir(), parent_template, _PARENT_ALLOWLIST_MEMO, "spawn_available_agents"
    )
    project_dirs = tuple(dict.fromkeys(str(d) for d in project_dirs if d))
    if not project_dirs:
        if user_lists is not None and not user_found and parent_session_key:
            _surface_unresolved_parent_spec(parent_template, parent_session_key)
        return user_lists, unreadable
    if user_lists is None:
        return None, unreadable
    lists = user_lists
    for directory in project_dirs:
        project_lists, _project_found, unreadable = _scope_spawn_allowlists(
            project_agents_dir(directory),
            parent_template,
            _PARENT_PROJECT_ALLOWLIST_MEMO,
            "spawn_available_agents_project",
        )
        if project_lists is None:
            return None, unreadable
        # Every scope applies (tightest-wins): an added list can only refuse more.
        lists = lists + project_lists
    return lists, ()


#: How many unreadable files a refusal names, and how long each error may be.
_MAX_UNREADABLE_IN_ERROR = 5


_MAX_UNREADABLE_ERROR_CHARS = 200


#: ``(allowlists, found, unreadable)`` for one agents directory: *allowlists* as
#: :func:`parent_spawn_allowlists` answers it for that directory alone, *found*
#: whether any readable spec there resolved the parent's name at all, and
#: *unreadable* the spec files that made the answer unknown, as
#: :func:`_describe_unreadable_specs` describes them (``()`` for every other
#: answer). The memo keeps this answer, which is why *unreadable* is bounded.
_ScopeAllowlists = tuple["tuple[tuple[str, ...], ...] | None", bool, "tuple[str, ...]"]


def _scope_spawn_allowlists(
    agents_dir: Path,
    parent_template: str,
    memo: "AgentsDirMemo[_ScopeAllowlists]",
    operation: str,
) -> _ScopeAllowlists:
    """Resolve *parent_template* in one agents directory (user or project scope)."""
    # Probe the walk first: a directory that is absent declares nothing
    # (nothing could have loaded the parent from it either), while one that
    # exists but cannot be scanned -- unreadable, or the path replaced by a
    # file -- is UNKNOWN, never "no declaration" (allow all).
    try:
        with os.scandir(agents_dir):
            pass
    except FileNotFoundError:
        return (), False, ()
    except OSError as exc:
        logger.warning("agents directory %s is not scannable (%s); refusing spawn", agents_dir, exc)
        return None, False, ()
    try:
        from kiro_crew.agent_spec_pins import pinned_memo_key, spec_pins

        # A pin change is a miss: the pin chooses which claimant's list applies.
        # One snapshot keys and scans, so a concurrent pin write cannot cross them.
        pins = spec_pins()
        return memo.get(
            agents_dir,
            pinned_memo_key(parent_template, pins),
            lambda: _scan_parent_spawn_allowlists(
                agents_dir, parent_template, operation, pins=pins
            ),
        )
    except OSError as exc:  # the directory walk itself failed: an unknown answer
        logger.warning("agents directory %s is not scannable (%s); refusing spawn", agents_dir, exc)
        return None, False, ()


#: Parent templates already warned about by :func:`_surface_unresolved_parent_spec`
#: in this process, so the log line is said once per name rather than per spawn.
#: Bounded: past the cap nothing more is recorded and the warning keeps firing,
#: which is noisier, never quieter.
_UNRESOLVED_PARENT_WARNED: set[str] = set()


_UNRESOLVED_PARENT_WARNED_MAX = 256


def _surface_unresolved_parent_spec(parent_template: str, parent_session_key: str) -> None:
    """Say that a parent's ``availableAgents`` may be unhonoured, loudly.

    Reached when no project directory is known for the parent session and no
    user-level spec names its template: the parent may be a project-scope agent
    whose declaration this gate cannot see. The spawn keeps today's admission
    (there is nothing readable to refuse on), and the gap is logged at WARNING
    once per name and audited to the SEL on every occurrence.
    """
    shown = redact_via_context(parent_template)
    if parent_template not in _UNRESOLVED_PARENT_WARNED:
        if len(_UNRESOLVED_PARENT_WARNED) < _UNRESOLVED_PARENT_WARNED_MAX:
            _UNRESOLVED_PARENT_WARNED.add(parent_template)
        logger.warning(
            "parent agent %r has no user-level spec and the parent session's project "
            "directory is unknown, so a project-scope "
            "toolsSettings.subagent.availableAgents declaration (if any) is not honoured "
            "for this spawn",
            shown,
        )
    try:
        sel().log_tool_invocation(
            session_key=parent_session_key,
            source="subagent",
            tool_name="spawn_run",
            outcome="parent_spec_unresolved",
            metadata={"parent_agent": shown, "scope": "user", "project_dir": "unknown"},
        )
    except Exception:  # noqa: BLE001 - the audit is best-effort; the warning above stands
        logger.debug("SEL audit failed for unresolved parent spec", exc_info=True)


#: The gate's answers, one set per agents directory, each pinned to the
#: directory's :func:`kiro_crew.agent_discovery.agents_dir_revision`
#: (entry names, mtime AND ctime, size, inode, mode, link count, the in-process
#: spec generation, and a digest of the bytes of anything younger than the racy
#: window). The catalog snapshot
#: (``parsed_agent_specs``) revalidates on names and mtime alone, so a rewrite
#: that keeps the mtime -- ``cp -p``, ``rsync -t``, a restore -- would serve a
#: PERMISSIVE allowlist for ever after the operator tightened it; a security
#: gate cannot be answered from it. The revision refuses to pin (``None``)
#: whenever freshness cannot be proven, and the walk then runs uncached. Its
#: own instance, not shared with the KAS projection's or the tool-policy read's:
#: the three reads carry different SEL ``operation`` labels.
_PARENT_ALLOWLIST_MEMO: AgentsDirMemo[_ScopeAllowlists] = AgentsDirMemo()


#: The same, for a parent session's ``<project>/.kiro/agents``: its own
#: instance because its reads carry their own SEL ``operation`` label.
_PARENT_PROJECT_ALLOWLIST_MEMO: AgentsDirMemo[_ScopeAllowlists] = AgentsDirMemo()


def _scan_parent_spawn_allowlists(
    agents_dir: Path,
    parent_template: str,
    operation: str = "spawn_available_agents",
    *,
    pins: Mapping[str, str] | None = None,
) -> _ScopeAllowlists:
    """One fresh walk of *agents_dir* for :func:`parent_spawn_allowlists`.

    Answers ``(allowlists, found, unreadable)``; *found* is whether a readable
    spec resolved the parent's name (by declared ``name`` or filename stem), and
    *unreadable* describes the files that made ``allowlists`` ``None``.

    Every spec file goes through the hardened reader under the gate's own SEL
    labels (*operation* tells the user scope from the project scope), and a file the reader refuses is kept as UNREADABLE rather than
    folded into "no spec" -- except a markdown file with no opening frontmatter
    fence, which is not a spec at all (:func:`plain_markdown_document`) and is
    skipped like the AppleDouble sidecar. Propagates ``OSError`` from the
    directory walk.
    """
    rows: list[tuple[dict[str, Any], Path]] = []
    unreadable: list[Path] = []
    for path in iter_agent_spec_files(agents_dir):
        if path.name.startswith("._"):
            # AppleDouble sidecar: rejected by design, not by failure.
            continue
        # Literal labels at each call, so the reader's label ratchet attributes both.
        if operation == "spawn_available_agents_project":
            data = _read_agent_spec(
                path, operation="spawn_available_agents_project", source="subagent"
            )
        else:
            data = _read_agent_spec(path, operation="spawn_available_agents", source="subagent")
        if data is None:
            if is_markdown_spec(path) and plain_markdown_document(path):
                # No opening frontmatter fence: a README or a shared prompt
                # fragment, not a spec. It cannot carry a ``name`` kiro-cli
                # would resolve, so it can hide no declaration and the
                # fail-closed rule for an unreadable spec does not reach it.
                # A FENCED document that fails to parse announced itself as a
                # spec and stays UNREADABLE.
                continue
            unreadable.append(path)
        else:
            rows.append((data, path))
    from kiro_crew import agent_spec_pins

    declared_rows = [(data, path) for data, path in rows if data.get("name") == parent_template]
    if operation == "spawn_available_agents_project":
        # ``agent.spec_pins`` chooses among the USER-level agents directory only;
        # every project claimant's allowlist stays in the intersection.
        decision, pin = "none", None
    else:
        decision, pin = agent_spec_pins.pin_decision(
            parent_template, [path.name for _data, path in declared_rows], pins
        )
    if decision == "broken":
        # The resolver refuses this parent's spec, so its allowlist is UNKNOWN: no
        # other claimant's list, and no filename fallback, stands in for it.
        logger.warning(
            "parent agent %r is pinned to %r, which no spec declaring it is; "
            "refusing spawn (fail-closed)",
            parent_template,
            pin,
        )
        return None, False, ()
    # The parent session runs the pinned claimant when ``agent.spec_pins`` chooses
    # one, so its allowlist is the one that applies. Any other case keeps every
    # claimant, the strictest reading.
    declared = [data for data, path in declared_rows if decision != "use" or path.name == pin]
    if not declared:
        # No readable spec declares the name. kiro-cli resolves a name by the
        # DECLARED ``name`` first, so the declaration may sit in a spec file the
        # hardened reader refused -- invalid JSON, oversized, a broken link, a
        # symlink to a sensitive target -- under ANY filename, not only
        # ``<parent_template>.json``. Such a file is known to exist and cannot
        # be read, so the answer is UNKNOWN: a gate does not fail open on a
        # document it could not open. Only when every spec-shaped file parsed
        # is "no readable spec declares the name" the same fact as "no spec
        # declares the name", and the direct-filename fallback below applies.
        if unreadable:
            logger.warning(
                "parent agent %r has no readable spec while %s could not be read; "
                "refusing spawn (fail-closed)",
                parent_template,
                ", ".join(repr(path.name) for path in unreadable),
            )
            scope = "project" if operation == "spawn_available_agents_project" else "user"
            return None, False, _describe_unreadable_specs(unreadable, scope, operation)
        declared = [data for data, path in rows if path.stem == parent_template]
    lists = (spawn_allowlist(data) for data in declared)
    return tuple(allowlist for allowlist in lists if allowlist is not None), bool(declared), ()


def _describe_unreadable_specs(
    paths: Sequence[Path], scope: str, operation: str
) -> tuple[str, ...]:
    """At most :data:`_MAX_UNREADABLE_IN_ERROR` descriptions of *paths*, in order.

    When more files were unreadable, the last description ends ``(+N more)``
    with the count of the files left out. The answer is bounded here, before
    the memo keeps it, and only the files it names are read a second time.
    """
    named = [
        _describe_unreadable_spec(path, scope, operation)
        for path in paths[:_MAX_UNREADABLE_IN_ERROR]
    ]
    withheld = len(paths) - len(named)
    if named and withheld > 0:
        named[-1] += f" (+{withheld} more)"
    return tuple(named)


def _describe_unreadable_spec(path: Path, scope: str, operation: str) -> str:
    """``'<file name>' (<scope> scope): <error>`` for a spec the hardened reader refused.

    The error comes from a second read through
    :func:`kiro_crew.agent_discovery.read_agent_spec_strict`, which keeps the
    failure class the listing reader folds into ``None``. A link is not read
    again: its refusal (a broken link, a sensitive target) is already audited,
    and a second read would audit it twice. Only the first line of the error is
    kept, bounded, because the refusal is one line of text.
    """
    if path.is_symlink():
        reason = "a link the agent spec reader refused"
    else:
        try:
            # Literal labels at each call, so the reader's label ratchet attributes both.
            if operation == "spawn_available_agents_project":
                read_agent_spec_strict(
                    path, operation="spawn_available_agents_project", source="subagent"
                )
            else:
                read_agent_spec_strict(path, operation="spawn_available_agents", source="subagent")
            reason = "not an agent spec object"
        except (OSError, ValueError) as exc:
            reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    if len(reason) > _MAX_UNREADABLE_ERROR_CHARS:
        reason = reason[: _MAX_UNREADABLE_ERROR_CHARS - 3] + "..."
    return f"{path.name!r} ({scope} scope): {reason}"
