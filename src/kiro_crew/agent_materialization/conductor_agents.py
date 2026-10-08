"""The conductor agent specs: one shape, four charters.

Four specs share one shape -- derived from the default template, no file-writing tool,
``execute_bash`` mounted but never auto-approved, and every MCP server mounted whole
but auto-approved verb by verb -- and differ in charter: the goal conductor (and its
deprecated ledger alias, which emits the same spec under the old name), the pipeline
conductor and the security conductor. Their prompts and the grant tuples each may
auto-approve are spec bytes and stay in :mod:`kiro_crew.agent`, beside the invariant
each is judged by; the installers here read them from there.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from kiro_crew import agent as agent_mod
from kiro_crew import agent_discovery
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME as _CONDUCTOR_AGENT_FILENAME
from kiro_crew.agent_files import (
    LEDGER_CONDUCTOR_AGENT_FILENAME as _LEDGER_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_files import (
    PIPELINE_CONDUCTOR_AGENT_FILENAME as _PIPELINE_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_files import (
    SECURITY_CONDUCTOR_AGENT_FILENAME as _SECURITY_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import auto_approve, managed_mcp


def _conductor_prompt(charter: str) -> str:
    """A conductor spec's prompt: its own *charter*, then the shared work-ledger
    protocol (``_CONDUCTOR_WORK_LEDGER_PROTOCOL``). Used by the conductors whose
    charter does not already state the ledger flow -- the pipeline and security
    conductors; ``_conductor_spec`` keeps the goal conductor's charter as is."""
    return charter + agent_mod._CONDUCTOR_WORK_LEDGER_PROTOCOL


def _conductor_shipped(
    core: tuple[str, ...], dashboard: tuple[str, ...], work: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """The grants a conductor installer ships: the shared builtins, its own *core*
    and *dashboard* verbs, then the base work-ledger verbs every conductor holds
    (``_CONDUCTOR_BASE_WORK_GRANTS``) followed by any extra *work* verbs of its own.

    The base work verbs are here, and not in each installer's arguments, because
    ``_conductor_mcp_servers`` mounts ``kirocrew-work`` on every conductor: a mount
    whose record verb prompts on every unattended patrol cycle is a ledger nobody
    writes to.
    """
    work_verbs = dict.fromkeys((*agent_mod._CONDUCTOR_BASE_WORK_GRANTS, *work))
    return ("session", "report", "tool_search", *core, *dashboard, *work_verbs)


def _conductor_mcp_servers(config: dict[str, Any]) -> dict[str, Any]:
    """The narrowed ``mcpServers`` map every conductor spec carries.

    ``kirocrew-core`` is inherited from ``build_agent_config``; ``kirocrew-dashboard``
    is hand-built here because it is the opt-in per-agent set (folder +
    session-control tools) that neither spec-writing loop emits, and a conductor
    granting it IS the explicit per-agent assignment that set requires.

    The two fields that are easy to forget are why this is a helper rather than
    three copies: without ``"type": "registry"`` a registry-mode client silently
    DROPS the entry, so the granted session-control tools never launch and the
    conductor's whole dispatch/patrol purpose is dead with no local error; and
    without the ``KIROCREW_HOME`` pin the shim reads the DEFAULT data home while the
    gateway runs under an override, so session control would act on a different
    session store than the one it reports on. Both helpers return empty on a
    default install, so the emitted spec is unchanged there.

    ``kirocrew-work`` is mounted on every conductor, unconditionally. The work
    ledger is where every conductor records the items it dispatches
    (``_CONDUCTOR_WORK_LEDGER_PROTOCOL``), and the dashboard's ``workstreams`` fold
    mints a board only from the ``work/recorded`` entries that ledger writes: a
    conductor without the mount runs a fleet its Dashboard tab cannot show. A
    conductor's own store (a pipeline queue, a security findings ledger) stays the
    detail behind the ledger row. The verbs every conductor auto-approves on the
    mount are ``_CONDUCTOR_BASE_WORK_GRANTS`` (``_conductor_shipped``).
    """
    mcp = config.get("mcpServers", {}) or {}
    core_entry = mcp.get("kirocrew-core")
    narrowed: dict[str, Any] = {}
    if core_entry:
        narrowed["kirocrew-core"] = core_entry
    dash_cmd, dash_args = agent_mod._kirocrew_mcp_invocation("mcp-dashboard")
    dash_entry: dict[str, Any] = {"command": dash_cmd, "args": dash_args}
    if managed_mcp._mcp_registry_mode():
        dash_entry["type"] = managed_mcp._MCP_REGISTRY_TYPE
    dash_env = managed_mcp._managed_mcp_env()
    if dash_env:
        dash_entry["env"] = dash_env
    narrowed["kirocrew-dashboard"] = dash_entry
    narrowed["kirocrew-work"] = managed_mcp._managed_opt_in_entry("mcp-work")
    return narrowed


# -- Every grant any release has shipped on the four conductor specs -------------
#
# THIS TABLE IS THE PROVENANCE. A generated conductor spec is rewritten on every
# ``rebuild_agent_config``, and the file on disk is the only place a user's own
# ``allowedTools`` entries live: nothing in the file says which entries the
# installer wrote and which the user added. The history does: an on-disk entry
# some release shipped on that spec is Crew's -- kept exactly when the current
# release ships it, under the ceiling -- and an entry no release ever shipped is
# the user's and survives. The one thing this cannot tell apart is a user who
# hand-adds a grant Crew once shipped and has since retired: that entry reads as
# Crew's and goes, loudly, and the tool it covered asks instead of being
# auto-approved -- the direction a safety list fails in.
#
# Reconstructed from every commit of the installers since the first conductor:
# the grant tuples and installer literals at each commit, resolved per spec (the
# pull request body of the change that introduced this table lists each grant
# with the change that shipped it and, where one did, the change that retired it).
# LITERAL on purpose -- not derived from the current tuples -- because the list's
# one job is to still name a grant after a release stops shipping it: derived
# from the current tuples, a retired grant would leave the history the moment it
# was retired and read as the user's, which is the very bug this module exists to
# close. A test pins that every grant the installers write today is listed here,
# so a NEW grant cannot land without being added; a retirement needs no step at
# all, which is the direction a safety list wants.
#
# The core verbs that replaced the bare ``@kirocrew-core`` on the goal conductor;
# the pipeline and security conductors shipped them from their first release,
# without ``select_crew`` (they route nothing).
_HISTORY_CORE_VERBS: frozenset[str] = frozenset(
    {
        "@kirocrew-core/monitor_start",
        "@kirocrew-core/monitor_update",
        "@kirocrew-core/autonudge_stop",
        "@kirocrew-core/wait",
        "@kirocrew-core/resource_status",
        "@kirocrew-core/list_sessions",
        "@kirocrew-core/session_ledger_read",
        "@kirocrew-core/session_ledger_record",
        "@kirocrew-core/skill_search",
        "@kirocrew-core/skill_fetch",
        "@kirocrew-core/send_message",
        "@kirocrew-core/send_notification",
        "@kirocrew-core/ask_question",
    }
)
# The dashboard verbs every conductor ships: the folder/session reads and creates,
# and ``session_status``. ``chat_folder_file_self`` is listed per spec below: the
# pipeline conductor has never shipped it.
_HISTORY_DASHBOARD_VERBS: frozenset[str] = frozenset(
    {
        "@kirocrew-dashboard/chat_folder_tree",
        "@kirocrew-dashboard/chat_folder_create",
        "@kirocrew-dashboard/session_create",
        "@kirocrew-dashboard/session_read_message",
        "@kirocrew-dashboard/session_status",
    }
)
# The work-ledger verbs the goal conductor and the ledger alias ship. The two
# ledger verbs were on the goal AND pipeline conductors for one release before the
# ledger flow moved to its own spec, and came back to the goal conductor with
# ``work_brief`` when it took the alias's spec over; they are the base every
# conductor ships; ``work_report`` is the
# WORKER's verb and no conductor has ever shipped it, so it is nobody's to drop.
_HISTORY_WORK_VERBS: frozenset[str] = frozenset(
    {
        "@kirocrew-work/work_ledger_read",
        "@kirocrew-work/work_ledger_record",
        "@kirocrew-work/work_brief",
    }
)
#: Spec name -> every ``allowedTools`` grant any release has shipped on it. Retired
#: today: the goal conductor's bare ``@kirocrew-core``. Beyond each spec's current
#: tuple the table holds exactly that entry (pinned by test), so an entry can join
#: it only as a grant some release is shown to have shipped. The pipeline
#: conductor's two work-ledger verbs were shipped for one release, retired, and are
#: shipped again as the base every conductor holds.
_SHIPPED_GRANT_HISTORY: dict[str, frozenset[str]] = {
    "kirocrew-conductor": (
        frozenset({"session", "report", "tool_search", "@kirocrew-core"})
        | _HISTORY_CORE_VERBS
        | {"@kirocrew-core/select_crew"}
        | _HISTORY_DASHBOARD_VERBS
        | {"@kirocrew-dashboard/chat_folder_file_self"}
        | _HISTORY_WORK_VERBS
        | {"@kirocrew-work/work_ledger_rebuild"}
    ),
    "kirocrew-ledger-conductor": (
        frozenset({"session", "report", "tool_search"})
        | _HISTORY_CORE_VERBS
        | {"@kirocrew-core/select_crew"}
        | _HISTORY_DASHBOARD_VERBS
        | {"@kirocrew-dashboard/chat_folder_file_self"}
        | _HISTORY_WORK_VERBS
        | {"@kirocrew-work/work_ledger_rebuild"}
    ),
    "kirocrew-pipeline-conductor": (
        frozenset({"session", "report", "tool_search"})
        | _HISTORY_CORE_VERBS
        | _HISTORY_DASHBOARD_VERBS
        | {"@kirocrew-work/work_ledger_read", "@kirocrew-work/work_ledger_record"}
    ),
    "kirocrew-security-conductor": (
        frozenset({"session", "report", "tool_search"})
        | _HISTORY_CORE_VERBS
        | _HISTORY_DASHBOARD_VERBS
        | {"@kirocrew-dashboard/chat_folder_file_self"}
        | {"@kirocrew-work/work_ledger_read", "@kirocrew-work/work_ledger_record"}
    ),
}

#: The characters that make an ``allowedTools`` entry a pattern rather than a name,
#: in kiro-cli's own glob dialect (``*``, ``?``, a ``[...]`` class).
_GLOB_METACHARACTERS = "*?["


def _is_wildcard(entry: str) -> bool:
    """Whether a user's ``allowedTools`` ENTRY is a pattern kiro-cli would expand.

    A pattern cannot be judged against the gate, and so is never carried forward.
    ``may_skip_gate`` judges a ref by its NAME: a builtin's floor and scopes are looked
    up by exact name, an ``@server`` ref by its server, and a pattern is in no table
    and names no server -- it passes as an unmapped name while kiro-cli expands it, on
    the one path that never reaches the gate, over the WHOLE tool namespace: every
    builtin and every verb of every mounted MCP server, in both spellings
    (``worker_agent._grant_reaches_excluded`` records that rule for ``*``). The
    builtins could be enumerated and judged one by one; the MCP verbs cannot -- a
    server's tools are discovered when kiro-cli connects to it, not at spec-write
    time -- so a ceiling withholding ``@kirocrew-dashboard/session_send`` by name
    would be bypassed by a kept ``session_*``, ``*_send`` or ``@*`` that no expansion
    here can see. The judgement the gate CAN make is of exact names, so those are the
    only entries offered to it: a shortcut the user wants for a tool is named as that
    tool. The rule is syntactic on purpose -- no tool name carries these characters,
    and a rule that reads them cannot be out-argued by a namespace it cannot read.
    """
    return any(ch in entry for ch in _GLOB_METACHARACTERS)


class _SpecUnusable(Exception):
    """The spec at the installer's path exists and cannot be read as a spec.

    ``replace`` says what the installer should do about it, and it follows the
    reader's own two failure classes (:func:`agent_discovery.read_agent_spec_strict`):

    ``True`` -- a path SHAPE identified before any read (a link planted at the
    spec's name, a path that resolves outside the agents directory or to a
    sensitive target, a dangling link, a second hard link on the spec's inode), or
    bytes that are not a spec and will not become one on a re-read (not JSON, not
    UTF-8, not an object, over the size cap, an ``allowedTools`` that is not a
    list). Nothing loadable is preserved by leaving such bytes where they are:
    kiro-cli cannot load them either, so a conductor dispatched by name would
    resolve to the DEFAULT agent -- a file-writing tool set with its own
    auto-approvals, in place of the deliberately file-tool-less conductor -- and a
    tightened governance ceiling could never be re-filtered onto the one
    ``allowedTools`` list that bypasses the PreToolUse gate. Writing a regular,
    loadable file over them is what restores both; the entries the file may have
    held could not be read and are named as lost. The hard-link alias is the one
    shape kiro-cli WOULD load: the hardened reader refuses a multiply-linked inode
    for good (no retry changes a link count), while kiro-cli reads the same bytes
    without that fence, so a spec left in place for that reason would be the one
    list a tightened ceiling never reaches. It is written over for the ceiling's
    sake, and safely: ``_atomic_json_write`` swaps the directory entry for a fresh
    inode, so the other name keeps its bytes untouched and nothing is written
    through the shared inode.

    ``False`` -- a read that failed for a reason the reader calls transient: a
    permission or I/O error, a descriptor limit, a refused open. The bytes behind
    it may be the user's intact spec and are never overwritten on a guess. The
    read is attempted a bounded few more times first (:func:`_spec_to_replace`),
    because the pause costs less than the hold does; a failure that outlasts them
    leaves the file exactly where it is, the install reports that it did not write
    it, and the next rebuild -- the gateway's hourly maintenance wake retries one
    the boot rebuild held (``agent.retry_held_conductor_specs``) -- reads it again.
    """

    def __init__(self, cause: str, *, replace: bool) -> None:
        super().__init__(cause)
        self.cause = cause
        self.replace = replace


#: Pauses between the attempts to read a conductor spec whose read failed for a
#: reason the reader calls transient (a plain ``OSError``: an I/O error, a stale
#: handle, a refused open), so the attempt count is one more than the pauses. A
#: second read a moment later clears most such failures, and a hold costs more
#: than the pause: the spec keeps the list the previous ceiling wrote until a
#: rebuild retries it. Bounded to a fraction of a second because the installers
#: run inside the boot rebuild, before the gateway serves, and only a spec that
#: cannot be read at all pays the whole of it.
_TRANSIENT_READ_BACKOFF_SECS: tuple[float, ...] = (0.05, 0.1)
_TRANSIENT_READ_ATTEMPTS = len(_TRANSIENT_READ_BACKOFF_SECS) + 1
_sleep = time.sleep


def _spec_to_replace(path: Path) -> dict[str, Any] | None:
    """The conductor spec on disk at *path*, ``None`` when there is none.

    One :func:`_spec_to_replace_once` attempt, repeated a bounded few times when
    the attempt failed in the reader's TRANSIENT class (``_SpecUnusable`` with
    ``replace=False``): the same read a moment later is what clears an I/O error
    or a stale handle, and holding the spec on the first one would leave the list
    the previous ceiling wrote on disk until something retries the rebuild. A
    path shape or bytes that can never be a spec (``replace=True``) are not
    retried -- a re-read changes neither. The last failure is raised with the
    attempt count in its cause, so the installer's warning says the spec was
    held after retries, not on one read.
    """
    agents_dir = agent_mod.kiro_agents_dir_path()
    attempts = _TRANSIENT_READ_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            return _spec_to_replace_once(path, agents_dir)
        except _SpecUnusable as exc:
            if exc.replace:
                raise
            if attempt == attempts:
                raise _SpecUnusable(f"{exc.cause}; {attempts} reads failed", replace=False) from exc
        _sleep(_TRANSIENT_READ_BACKOFF_SECS[attempt - 1])
    return None  # pragma: no cover -- the loop returns or raises


def _spec_to_replace_once(path: Path, agents_dir: Path) -> dict[str, Any] | None:
    """One attempt at :func:`_spec_to_replace`.

    Read through the hardened reader that keeps the failure class
    (:func:`agent_discovery.read_agent_spec_strict`), because this caller needs
    to know WHY a read failed: an absent file is a first install and carries
    nothing, a file that may read fine next time is left alone, and a file whose
    bytes can never be a spec is written over (:class:`_SpecUnusable`). The
    read-then-write-back fence every in-place spec rewriter applies
    (``_spec_path_is_safe``) applies here too, so a link at the spec's name is
    not followed into a copy-out -- asked through :func:`agent_mod._spec_path_shape_refusal`
    so that a probe which failed to read the metadata at all is told apart from a
    shape that is refused: the first is held like every other read that may
    succeed next time, because the bytes behind the name may be the user's intact
    spec; only the second is written over.
    """
    try:
        shape = agent_mod._spec_path_shape_refusal(path, agents_dir)
    except OSError as exc:
        raise _SpecUnusable(
            f"its metadata could not be read ({exc}); nothing about the file is known"
            " and its bytes may be your intact spec",
            replace=False,
        ) from exc
    if shape is not None:
        raise _SpecUnusable(shape, replace=True)
    # The link count is a shape, read from the inode's metadata before any byte
    # of it: a second hard link is permanent until someone unlinks it, so the
    # hardened reader's refusal of it is not a read that may succeed later -- and
    # kiro-cli, which does not apply that fence, would keep loading those bytes.
    try:
        links = os.lstat(path).st_nlink
    except FileNotFoundError:
        return None
    except OSError:
        links = 1  # the reader below classifies this failure
    if links > 1:
        raise _SpecUnusable(
            "a second hard link shares the spec's inode (the other name keeps its "
            "bytes; a spec kept as a single-link regular file carries its entries)",
            replace=True,
        )
    try:
        data = agent_discovery.read_agent_spec_strict(
            path, operation="conductor_spec_regeneration", source="unknown"
        )
    except FileNotFoundError:
        if os.path.lexists(path):
            raise _SpecUnusable("a dangling link stands at the spec's name", replace=True)
        return None
    except OSError as exc:
        # The reader's contract: a plain OSError is a read that MAY succeed on
        # retry (permissions, I/O, a refused open), not a verdict on the bytes.
        raise _SpecUnusable(str(exc), replace=False) from exc
    except ValueError as exc:
        # The reader's other class: the content is not a spec and a re-read will
        # not change that (a sensitive target, over the cap, not UTF-8, not JSON).
        raise _SpecUnusable(str(exc), replace=True) from exc
    if not isinstance(data, dict):
        raise _SpecUnusable("the document is not a JSON object", replace=True)
    if "allowedTools" in data and not isinstance(data["allowedTools"], list):
        raise _SpecUnusable("allowedTools is not a list", replace=True)
    return data


def _governed_grants(
    shipped: tuple[str, ...], *, name: str, filename: str, source: str, clean: bool
) -> list[str] | None:
    """The ``allowedTools`` list a conductor installer writes: the shipped grants, then
    the user's own entries on the spec it is about to replace, all under the ceiling.
    ``None`` when the spec on disk must not be replaced at all (below).

    Every ``rebuild_agent_config`` -- every gateway start, a provider switch, an MCP
    change -- re-runs the installers. An installer that rebuilt this list from its
    grant tuple alone and wrote the file without reading it would drop every
    approval the user had added, at the next start, silently, while
    ``toolsSettings`` carried over because nothing rewrites it. ``kirocrew.json``
    keeps the user's ``tools`` / ``allowedTools`` on an existing install (ADD-only,
    then the ceiling); this is that same merge, for the four conductor specs.

    Which entries on disk are the USER'S is the whole question, and the file cannot
    answer it: an entry is either the installer's or the user's and nothing in the
    JSON says which. The history of every grant any release has shipped on this
    spec (``_SHIPPED_GRANT_HISTORY``) answers it: an on-disk entry the history
    names is Crew's and is re-derived from *shipped* -- kept exactly when this
    release ships it -- and one it does not name is the user's and is carried
    forward. Without that history an add-only merge would keep every grant an
    earlier release shipped and this one deliberately does not -- the goal
    conductor's core grant is named verbs where an earlier release's was a bare
    ``@kirocrew-core``, and a narrowing like that reaches an existing install only
    if the installer can tell its own earlier output from the user's. The worker
    installer records the same objection and takes nothing but ``model`` from its
    previous file for want of such a record; here the history is the record, so a
    user's entry is kept and a retired grant still goes.

    The provenance contract, then: an entry on disk is the user's exactly when no
    release has ever shipped it on this spec. The history is kept in code rather
    than in a per-install record because it holds for ANY file at the spec's name
    -- one written by this release, by an earlier one (a downgrade and
    re-upgrade), by another install whose agents directory was copied here, or by
    a hand that edited an owned field -- with no second store to fall out of step
    with the spec, be torn by a failed write, or be hand-damaged into a wrong
    answer. What it costs is one case: a user who hand-adds a grant Crew once
    shipped and has since retired (the bare ``@kirocrew-core`` an early goal
    conductor shipped) loses it at the next start, named in the WARNING and the SEL feed, and the
    tool it covered asks instead of being auto-approved -- the drop fails closed.
    What the history cannot name is a grant a NEWER release ships: on an install
    downgraded past it, that grant reads as the user's until the re-upgrade --
    bounded to a grant some release of this project chose to ship, never to one
    it retired. ``clean`` keeps nothing: ``kirocrew setup --agent-only --clean``
    is the explicit reset, and it resets these specs as it resets
    ``kirocrew.json``.

    A spec that is present and cannot be read is not "no spec", and what happens
    to it follows the reader's own two failure classes (``_SpecUnusable``). Bytes
    that can never be a spec -- not JSON, not an object, an ``allowedTools`` that
    is not a list, a planted or dangling link, a path outside the agents directory
    -- are written over, because nothing loadable is preserved by keeping them:
    kiro-cli would not load that file either, a conductor dispatched by name would
    resolve to the DEFAULT agent and its file-writing tool set, and a tightened
    ceiling could never be re-filtered onto the list. A second hard link on the
    spec's inode is written over too, for the opposite reason: kiro-cli WOULD load
    those bytes while the hardened reader refuses them for good, so a spec left in
    place for that reason is the one list a tightened ceiling never reaches -- and
    the write swaps the directory entry, so the other name keeps its bytes. The
    WARNING names the cause and that no entry on the file was carried forward. A
    read that failed for a reason the reader calls transient (a permission or I/O
    error, a refused open) may stand before the user's intact spec, so that file
    is left exactly where it is (``None``), named in the WARNING, and the
    installer reports that it wrote nothing -- so a moved ceiling stays pending
    and is retried, rather than being marked projected onto a list that was never
    rewritten.

    Order: *shipped* first, as shipped, then the user's entries, so the spec reads
    as before for a user who added nothing and the tests that pin the shipped list
    stay literal. A shipped grant the user deleted comes back: narrowing a
    conductor is the governance ceiling's job (``POLICY ∩ PROFILE``), never a hand
    edit's, and that ceiling is applied here to the WHOLE merged list through
    ``_filter_auto_approve``, so a user's entry the ceiling forbids is withheld
    (and SEL-audited) exactly as a shipped one is -- the merge never widens the
    ceiling. One shape of the user's is never offered to it: a wildcard (``*``,
    ``fs_*``, ``session_*``, ``@*``, a character class) is a pattern the gate cannot
    judge -- it judges names, and kiro-cli would expand the pattern over every
    builtin and every mounted MCP verb, which no expansion at spec-write time can
    enumerate (:func:`_is_wildcard`) -- so it is dropped before the merge and the
    warning says to name tools exactly; an exact name or an exact ``@`` ref is judged
    by the gate as before. Nothing on the list moves silently: one WARNING names
    every on-disk entry this install did not carry forward, a drop the ceiling did
    not make is a revoked auto-approval and lands in the SEL feed as one (under its
    reason: a retired grant, or a wildcard), and an entry of the user's that IS
    carried forward lands there too, as a retained one -- the decision that an
    auto-approval stays past a rebuild is a permission decision, and the feed is
    where those are read.
    """
    on_disk: list[str] = []
    if not clean:
        path = agent_mod.kiro_agents_dir_path() / filename
        try:
            existing = _spec_to_replace(path)
        except _SpecUnusable as exc:
            agent_mod.logger.warning(
                "%s: the spec on disk could not be read as a spec (%s); %s",
                filename,
                exc.cause,
                (
                    "written afresh from the shipped grants, so no entry on it is carried "
                    "forward"
                    if exc.replace
                    else "left in place so your entries are not lost -- repair it, or run "
                    "`kirocrew setup --agent-only --clean` to rebuild it"
                ),
            )
            if not exc.replace:
                return None
            existing = None
        if existing is not None:
            # A non-string entry (a hand-edited file) is not a tool ref and would
            # crash the ceiling predicate: neither kept nor reported as dropped.
            on_disk = list(
                dict.fromkeys(
                    ref for ref in existing.get("allowedTools", []) if isinstance(ref, str)
                )
            )
    # Crew's entries are every grant any release has shipped on this spec: a
    # retired grant is Crew's and goes, and only an entry no release ever shipped
    # is the user's.
    crews = _SHIPPED_GRANT_HISTORY[name]
    users_own = [ref for ref in on_disk if ref not in crews]
    # A wildcard among the user's entries is a pattern, not an approval of anything
    # the gate can name: ``may_skip_gate`` judges by exact name and would pass it,
    # while kiro-cli expands it over every builtin and every mounted MCP verb --
    # and the verbs cannot be enumerated here to be judged one by one. Dropped
    # BEFORE the merge, so it is never offered to the ceiling as if it were an
    # approval of anything in particular, and named in the WARNING and the SEL
    # feed below with the one remedy: name the tools exactly.
    wildcards = [ref for ref in users_own if _is_wildcard(ref)]
    users_own = [ref for ref in users_own if ref not in wildcards]
    merged = (*shipped, *(ref for ref in users_own if ref not in shipped))
    granted = auto_approve._filter_auto_approve(merged, source=source)
    dropped = [ref for ref in on_disk if ref not in granted]
    retained = [ref for ref in users_own if ref in granted]
    if retained:
        # The user's own auto-approvals, carried past a rebuild: the decision that
        # a grant Kiro Crew did not ship stays in force is a permission decision
        # and belongs in the SEL feed beside the withholds and revocations below,
        # so an operator reading the feed sees every grant that outlived the
        # rebuild and on whose authority. Best-effort: the audit must not break
        # the install.
        try:
            agent_mod.sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_retained",
                outcome="ok",
                source=source,
                resources=(
                    f"{', '.join(retained)} auto-approval retained on {filename} "
                    "(your own entry, shipped by no release of Kiro Crew)"
                ),
            )
        except Exception:  # noqa: BLE001 — the audit must not break the install
            agent_mod.logger.debug(
                "SEL audit unavailable for retained conductor grant", exc_info=True
            )
    if not dropped:
        return granted
    wildcard_note = (
        f"; wildcard entries ({', '.join(wildcards)}) cannot be judged against the gate "
        "and are not carried -- name tools exactly"
        if wildcards
        else ""
    )
    agent_mod.logger.warning(
        "%s: regeneration dropped allowedTools entries %s (withheld by the governance "
        "ceiling, or shipped by an earlier release of Kiro Crew and no longer granted)%s",
        filename,
        ", ".join(dropped),
        wildcard_note,
    )
    # An entry the ceiling withheld was offered to it inside ``merged`` and is already
    # on the SEL feed from the filter; one that was never offered -- an earlier
    # release's grant this one does not ship, or a wildcard -- is an auto-approval
    # this install REVOKED, which is a permission decision and belongs in the same
    # feed, under its own reason.
    revoked = [ref for ref in dropped if ref not in merged and ref not in wildcards]
    if revoked:
        _audit_revoked(
            source,
            f"{', '.join(revoked)} auto-approval removed from {filename} "
            "(shipped by an earlier release and not by this one)",
        )
    for ref in wildcards:
        _audit_revoked(
            source,
            f"{ref} auto-approval removed from {filename} (a wildcard: it cannot be "
            "judged against the gate; name tools exactly)",
        )
    return granted


def _audit_revoked(source: str, resources: str) -> None:
    """One ``mcp_auto_approve_revoked`` SEL record. Best-effort: the audit must not
    break the install."""
    try:
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_auto_approve_revoked",
            outcome="ok",
            source=source,
            resources=resources,
        )
    except Exception:  # noqa: BLE001 — the audit must not break the install
        agent_mod.logger.debug("SEL audit unavailable for revoked conductor grant", exc_info=True)


def _conductor_shipped_grants() -> tuple[str, ...]:
    """The grants ``_conductor_spec`` ships, read once per install so the goal conductor
    and its ledger alias merge against the same tuple."""
    return _conductor_shipped(
        agent_mod._CONDUCTOR_CORE_GRANTS,
        agent_mod._CONDUCTOR_DASHBOARD_GRANTS,
        agent_mod._LEDGER_CONDUCTOR_WORK_GRANTS,
    )


def _conductor_spec(
    *, name: str, description: str, filename: str, source: str, clean: bool = False
) -> dict[str, Any] | None:
    """The conductor spec, emitted under *name* — one body, two filenames. ``None``
    when the spec on disk is the user's and unreadable, and must be left in place
    rather than replaced (``_governed_grants``).

    ``kirocrew-conductor`` and its deprecated alias
    ``kirocrew-ledger-conductor`` differ in ``name`` and ``description`` and in
    nothing else, and that is enforced here rather than trusted: two installers
    that each hand-built the same list are exactly where a grant lands on one
    spec and not the other, and the alias exists so an in-flight session keeps
    working — an alias that emits a DIFFERENT spec silently changes what that
    session can do. ``filename`` and ``source`` are the two per-installer
    values, and neither reaches the emitted JSON: ``filename`` names the KAS
    ``agent_id`` used in the derive's log line and the spec on disk whose user
    entries are carried forward, and ``source`` names the installer in the
    withheld-grant audit event. ``clean`` is the rebuild's own flag, passed
    through: a clean rebuild carries nothing forward (``_governed_grants``).

    The charter, and why each property is a property of the SPEC rather than of
    the prompt. Derived from the kirocrew agent (resolved MCP invocations,
    security hooks) and narrowed to what conducting needs: session control,
    core tools, the work ledger, and shell for the bundled acceptance
    evaluator — and **no tool that can write a file**, not ``fs_write`` and not
    ``code`` either, which governance classes under ``filesystem.write``
    because it writes files and can shell out. That is what makes "never does a
    work item's work itself" true against the tool list and not just against
    the prose.

    ``@kirocrew-core``, ``@kirocrew-dashboard`` and ``@kirocrew-work`` are all
    MOUNTED whole but auto-approved only verb by verb, via
    ``_CONDUCTOR_CORE_GRANTS``, ``_CONDUCTOR_DASHBOARD_GRANTS`` and
    ``_LEDGER_CONDUCTOR_WORK_GRANTS`` (see their comments for the per-verb
    reasoning). Both backends honour a per-tool reference, so the narrowing is
    real rather than cosmetic: kiro-cli's ``is_tool_in_allowlist`` checks
    ``@server`` and then ``@server/<tool>``, and ``allowed_tools_to_permissions``
    maps the same entry to an exact KAS ``server/tool`` resource match.

    The line the split follows is stated as an invariant on those tuples, not as
    a taste call: a granted verb may CREATE or READ, never MUTATE something that
    already exists and is not the conductor's own. Reads and creates are granted
    because the patrol loop is nudge-driven and must not block on an approval
    nobody is there to give. ``session_stop`` (discards a peer's in-flight turn),
    ``session_send`` (runs text as a peer's turn) and ``chat_folder_move_session``
    (writes a peer session's ``folder_id``) are withheld, because the conductor
    ingests untrusted content by design and the server-side gates bound which
    target is reachable, not what is done to it. ``work_report`` is withheld on
    the same rule: it writes into a PARENT's record, across a dispatch
    relationship.

    ``execute_bash`` is withheld for a different reason that is worth keeping
    distinct: ``allowedTools`` is name-scoped with no argument matching, so
    trusting the one bundled script cannot be told apart from trusting arbitrary
    shell. There is no per-argument form of that grant the way there is a
    per-tool form of the MCP one.

    The operating procedure ships as the ``goal-conductor`` builtin skill, NOT
    ``conductor``: that directory name belonged to the delegation skill the
    retired ``agent.conductor_skill`` flag generated, and install cleanup still
    removes a ``<skills>/conductor/SKILL.md`` whose bytes the generator wrote on
    old installs. Sharing the name would let that cleanup erase the packaged
    skill.
    """
    config = agent_mod.build_agent_config()
    config["name"] = name
    config["description"] = description
    # The charter already states the work-ledger flow in detail, so the shared
    # protocol block (``_conductor_prompt``) is not appended here.
    config["prompt"] = agent_mod._CONDUCTOR_SYSTEM_PROMPT
    config["tools"] = [
        "execute_bash",
        "fs_read",
        # ``web_fetch`` serves the charter's own worked example (reading an issue
        # list during triage). Deliberately NOT mounted: ``web_search`` (nothing
        # names it), ``grep``/``glob`` (``fs_read`` covers every read the charter
        # describes), and above all ``code`` — governance classes it under
        # ``filesystem.write`` because it "writes files AND can shell out", so
        # mounting it would make this spec's whole no-write property false.
        # An unused grant is surface the charter cannot account for.
        "web_fetch",
        "session",
        "report",
        # Load-bearing, not decoration: with MCP Tool Search active the
        # session-control specs are deferred, so the conductor cannot reach
        # ``session_create`` / ``chat_folder_*`` / ``monitor_start`` at all until
        # it loads them by id. Named in the prompt's tool inventory for that
        # reason, and auto-approved below so the load itself never prompts.
        "tool_search",
        "@kirocrew-core",
        "@kirocrew-dashboard",
        # Mounted whole, auto-approved verb by verb below: the worker half lives
        # on this server too, and a conductor has no reason to auto-approve a
        # tool whose only answer to it is a refusal.
        "@kirocrew-work",
    ]
    # ``allowedTools`` is the ONE path that never reaches the PreToolUse gate, so
    # every grant is filtered through the governance ceiling first — the same
    # predicate ``rebuild_agent_config`` applies to the primary spec's assembled
    # list, and the entry point ``may_skip_gate_now`` exists precisely so a new
    # writer cannot re-open the bypass by restating a literal. A governed ref
    # stays MOUNTED (it is still in ``tools``); it just prompts, and the gate
    # then applies the ceiling's per-tool rule with the real arguments.
    # ``tool_search`` is granted on the same rule as the dashboard verbs below:
    # it only READS a tool spec into context — it cannot act, touch workspace
    # state, or reach the machine — and it is bounded by the mounted catalog.
    # Withholding it made the ONE call that unblocks every deferred
    # session-control tool prompt first, so an unattended patrol cycle stalled
    # on the load rather than on the work. ``execute_bash`` stays withheld for
    # the reason recorded above it: ``allowedTools`` has no argument matching,
    # so trusting the one bundled script cannot be told apart from trusting
    # arbitrary shell. Withheld, not forbidden: these are the grants Crew SHIPS.
    # What the user approved on the spec on disk is carried forward beside them,
    # through the same ceiling (``_governed_grants``) -- or, when that spec is
    # theirs and cannot be read, left exactly where it is.
    granted = _governed_grants(
        _conductor_shipped_grants(), name=name, filename=filename, source=source, clean=clean
    )
    if granted is None:
        return None
    config["allowedTools"] = granted
    config["mcpServers"] = _conductor_mcp_servers(config)
    # Derive the KAS policy from the FILTERED grant list instead of restating it
    # as a literal: the rules come out byte-identical, a later edit to
    # ``allowedTools`` carries through, and a ceiling that strips a grant strips
    # its KAS rule with it (a hand-written ``kirocrew-core/*`` allow would have
    # survived the filter on the KAS backend). The shared writer version-gates it.
    auto_approve._write_derived_permissions(config, config["allowedTools"], filename)
    return config


def _install_conductor_agent(*, clean: bool = False) -> bool:
    """Generate and install the kirocrew-conductor agent config.

    THE conductor: it owns a goal, and it tracks that goal in the work ledger.
    The ledger flow shipped on a separate ``kirocrew-ledger-conductor`` spec
    first so that migrating every existing conductor user was a decision and not
    a side effect, and the decision has now been taken — the flow ran end to end
    (7 items across 3 rounds, each acceptance settled by the evaluator rather
    than by a transcript read), so it is what this spec emits.
    ``kirocrew-ledger-conductor`` stays for one release as a deprecated alias
    emitting this same spec under its old name, because an in-flight session
    names its agent by string and a deleted name is a broken session.

    Every property ``_conductor_spec`` argues for holds here, and the swap did
    not relax one of them: no file-writing tool at all, ``@kirocrew-core`` /
    ``@kirocrew-dashboard`` / ``@kirocrew-work`` mounted whole and auto-approved
    verb by verb, ``execute_bash`` mounted and never auto-approved BY CREW, and
    the KAS policy derived from the FILTERED grant list rather than restated.
    The user's own auto-approve entries on the installed file are carried
    forward under the ceiling (``_governed_grants``); ``clean`` is the rebuild's
    flag and drops them, as it drops every customization of ``kirocrew.json``.

    Returns whether the spec was written. ``False`` is the one non-raising path
    that leaves the file untouched -- a spec on disk the installer could not read
    for a reason that may clear on retry -- and the rebuild reads it so a moved
    governance ceiling is not marked projected onto a list that was never
    rewritten (``rebuild_agent_config``).
    """
    config = _conductor_spec(
        name="kirocrew-conductor",
        description=(
            "Owns a long-horizon goal and tracks it in the work ledger: "
            "decomposes it into items, dispatches one session per item, reads "
            "their reported status as data rather than as a transcript, "
            "verifies claims with the acceptance evaluator, and decides each "
            "next round. Never does the work itself."
        ),
        filename=_CONDUCTOR_AGENT_FILENAME,
        source="_install_conductor_agent",
        clean=clean,
    )
    if config is None:
        return False
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _CONDUCTOR_AGENT_FILENAME
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed conductor agent config: %s", path)
    return True


#: Deprecated agent-spec name -> the current spec that replaced it.
#:
#: One row per installed alias. ``kirocrew doctor`` reads this table to warn
#: any config surface that persists an agent name -- a cron job, a crew
#: binding, a chat slot -- while the old name still resolves, so deleting the
#: alias later breaks nobody silently with ``Mode not found`` at dispatch
#: time. A row is deleted together with its alias installer, never before:
#: the doctor notice is the precondition for the deletion (see
#: ``docs/request-for-change/rfc-conductor-work-ledger.md``, "What retired
#: means for the name").
DEPRECATED_AGENT_SPECS: dict[str, str] = {
    "kirocrew-ledger-conductor": "kirocrew-conductor",
}


def _install_ledger_conductor_agent(*, clean: bool = False) -> bool:
    """Install the deprecated ``kirocrew-ledger-conductor`` alias spec.

    The ledger flow is ``kirocrew-conductor`` now, and this name is kept for one
    release because it is a public, user-facing string: it is what a running
    session records as its agent, what a seed prompt names for a second-level
    conductor, and what an operator typed into a cron. Deleting it in the same
    release as the swap would break those in place, so the name still resolves
    and emits the SAME spec — see ``_conductor_spec``, which both installers
    call so the two cannot drift. Its file is its own, so the user entries it
    carries forward and the record it leaves are keyed by the alias name.

    Removed next release; nothing new should name it.
    """
    config = _conductor_spec(
        name="kirocrew-ledger-conductor",
        description=(
            "Deprecated alias of kirocrew-conductor (removed next release). "
            "Owns a long-horizon goal and tracks it in the work ledger: "
            "decomposes it into items, dispatches one session per item, reads "
            "their reported status as data rather than as a transcript, "
            "verifies claims with the acceptance evaluator, and decides each "
            "next round. Never does the work itself."
        ),
        filename=_LEDGER_CONDUCTOR_AGENT_FILENAME,
        source="_install_ledger_conductor_agent",
        clean=clean,
    )
    if config is None:
        return False
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _LEDGER_CONDUCTOR_AGENT_FILENAME
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed ledger-conductor alias agent config: %s", path)
    return True


def _install_pipeline_conductor_agent(*, clean: bool = False) -> bool:
    """Generate and install the kirocrew-pipeline-conductor agent config.

    Follows ``_install_conductor_agent`` above deliberately — one standalone
    installer per generated agent is the file's established pattern — and
    keeps every property that installer's docstring argues for: derived from
    the kirocrew agent, **no dedicated file-writing tool** (neither ``fs_write``
    nor ``code``), ``@kirocrew-dashboard`` and ``@kirocrew-work`` mounted whole but
    auto-approved only verb by verb, ``execute_bash`` mounted but never
    auto-approved (``allowedTools`` has no argument matching, so trusting the two
    bundled skill scripts cannot be told apart from trusting arbitrary shell), and
    the KAS policy derived from the FILTERED grant list. Each dispatched item is a
    work-ledger row; the queue and ``decisions.md`` stay its detail. Where the two agents
    differ is charter, not mechanics: this one supervises a repository
    pipeline's worker fleet (probe / verify / intervene / adjudicate / govern)
    per the ``pipeline-conductor`` builtin skill, rather than decomposing a
    free-form goal.
    """
    config = agent_mod.build_agent_config()
    config["name"] = "kirocrew-pipeline-conductor"
    config["description"] = (
        "Runs one repository pipeline as a supervised fleet: picks up queued "
        "work items, dispatches one worker session per item, probes and "
        "verifies them, intervenes on stalls, adjudicates blocked items, and "
        "governs host resources and per-item credit budgets. Never does a "
        "work item's work itself."
    )
    config["prompt"] = _conductor_prompt(agent_mod._PIPELINE_CONDUCTOR_SYSTEM_PROMPT)
    config["tools"] = [
        "execute_bash",
        "fs_read",
        "web_fetch",
        "session",
        "report",
        "tool_search",
        "@kirocrew-core",
        "@kirocrew-dashboard",
        "@kirocrew-work",
    ]
    shipped = _conductor_shipped(
        agent_mod._PIPELINE_CONDUCTOR_CORE_GRANTS,
        agent_mod._PIPELINE_CONDUCTOR_DASHBOARD_GRANTS,
    )
    granted = _governed_grants(
        shipped,
        name="kirocrew-pipeline-conductor",
        filename=_PIPELINE_CONDUCTOR_AGENT_FILENAME,
        source="_install_pipeline_conductor_agent",
        clean=clean,
    )
    if granted is None:
        return False
    config["allowedTools"] = granted
    config["mcpServers"] = _conductor_mcp_servers(config)
    # Same derive-don't-restate rationale as the conductor above; the shared
    # writer version-gates it.
    auto_approve._write_derived_permissions(
        config, config["allowedTools"], _PIPELINE_CONDUCTOR_AGENT_FILENAME
    )
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _PIPELINE_CONDUCTOR_AGENT_FILENAME
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed pipeline-conductor agent config: %s", path)
    return True


def _install_security_conductor_agent(*, clean: bool = False) -> bool:
    """Generate and install the kirocrew-security-conductor agent config.

    A third standalone installer, following ``_install_pipeline_conductor_agent``
    above for the same reason that one follows ``_install_conductor_agent`` — one
    installer per generated agent is this file's established pattern — and
    keeping every property those docstrings argue for: derived from the kirocrew
    agent, **no dedicated file-writing tool** (neither ``fs_write`` nor ``code``,
    which governance classes under ``filesystem.write``), ``@kirocrew-core`` and
    ``@kirocrew-dashboard`` mounted whole but auto-approved only verb by verb,
    ``execute_bash`` mounted but never auto-approved (``allowedTools`` has no
    argument matching, so trusting the skill's bundled scripts cannot be told
    apart from trusting arbitrary shell), and the KAS policy derived from the
    FILTERED grant list.

    Those properties carry more weight here than on either sibling, which is the
    charter difference: this agent's own children probe a security fence, so what
    it ingests on an unattended cycle is hostile by assumption. "Never touches the
    target itself" therefore has to hold as a spec property when nobody is at the
    keyboard, and the two human gates the prompt names (active testing beyond a
    local proof of concept, and any fixer dispatch) are what the withheld
    ``session_send`` / ``spawn_run`` / ``execute_bash`` grants make expensive to
    skip rather than merely discouraged.

    The grant tuples are the pipeline conductor's, REUSED rather than copied. The
    derivation the goal conductor's comment describes — the union of this prompt's
    own "Your tools:" inventory and the skill's real call sites, filtered to what
    registers on each server — lands on exactly that set here: patrol lifecycle,
    reads, the agent's own ledger, and owner reporting, with no ``select_crew``
    (this conductor routes nothing). A third byte-identical copy would be
    duplication whose later divergence nothing could detect, and reuse across
    agents is already this file's practice, and ``_filter_auto_approve`` plus
    ``_conductor_mcp_servers`` are the same argument applied one level down.

    ``@kirocrew-work`` is mounted with the base work-ledger grants, as on every
    conductor (``_conductor_mcp_servers``): each auditor, verifier and fixer is a
    ledger item. Findings, severity and evidence stay in the
    ``security-conductor`` skill's ledger scripts as the detail behind that row.
    """
    config = agent_mod.build_agent_config()
    config["name"] = "kirocrew-security-conductor"
    config["description"] = (
        "Runs one security audit as a supervised fleet: decomposes a target "
        "into attack surfaces, dispatches one auditor session per surface and "
        "an independent verifier per finding, adjudicates severity, and gates "
        "any fix behind a human yes. Never touches the target itself."
    )
    config["prompt"] = _conductor_prompt(agent_mod._SECURITY_CONDUCTOR_SYSTEM_PROMPT)
    config["tools"] = [
        "execute_bash",
        "fs_read",
        "web_fetch",
        "session",
        "report",
        "tool_search",
        "@kirocrew-core",
        "@kirocrew-dashboard",
        "@kirocrew-work",
    ]
    shipped = _conductor_shipped(
        agent_mod._PIPELINE_CONDUCTOR_CORE_GRANTS,
        agent_mod._SECURITY_CONDUCTOR_DASHBOARD_GRANTS,
    )
    granted = _governed_grants(
        shipped,
        name="kirocrew-security-conductor",
        filename=_SECURITY_CONDUCTOR_AGENT_FILENAME,
        source="_install_security_conductor_agent",
        clean=clean,
    )
    if granted is None:
        return False
    config["allowedTools"] = granted
    config["mcpServers"] = _conductor_mcp_servers(config)
    # Derived from the FILTERED grant list rather than restated, so a ceiling
    # that strips a grant strips its KAS rule with it; the shared writer
    # version-gates it.
    auto_approve._write_derived_permissions(
        config, config["allowedTools"], _SECURITY_CONDUCTOR_AGENT_FILENAME
    )
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _SECURITY_CONDUCTOR_AGENT_FILENAME
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed security-conductor agent config: %s", path)
    return True
