"""Install and attribute the managed ``kirocrew-dashboard-author`` agent spec.

A dispatched, worker-shaped agent that authors ONE dashboard template and lands it as a
pull request -- NOT the default-mirroring worker and NOT a conductor, so it is built from
its own shipped charter rather than mirrored from the default spec. Its install is
attributed by an installer-recorded ownership digest in the ``agent_state`` sidecar, so a
user-placed file at the once-user-creatable stem (or another owned agent's spec renamed
onto it) is never overwritten.

A registered owner of the :mod:`kiro_crew.agent_materialization` package: callers reach its
public names through the facade (``kiro_crew.agent.<name>``), the one import path every owner
is read through.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kiro_crew import agent as agent_mod
from kiro_crew import agent_state
from kiro_crew.agent_files import (
    DASHBOARD_AUTHOR_AGENT_FILENAME as _DASHBOARD_AUTHOR_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import auto_approve, skill_resources
from kiro_crew.agent_spec_format import parse_markdown_spec

#: The shipped ``dashboard-template`` skill spec -- the ONE authoritative copy of the
#: dashboard author's charter. The installer reads its ``prompt`` and ``description`` from
#: here through :func:`parse_markdown_spec` rather than from a Python constant held equal
#: to it by nothing. ``parent.parent`` is the ``kiro_crew`` package root (this module is
#: ``kiro_crew/agent_materialization/dashboard_author_agent.py``).
_DASHBOARD_AUTHOR_SPEC_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "builtin_skills"
    / "kirocrew-dev"
    / "dashboard-template"
    / "agent-spec.md"
)

#: The agent NAME the dashboard-author spec declares. The install writes it as the spec's
#: ``name`` and records ownership under it; the governance loop recognises an owned write by
#: this name (:func:`kiro_crew.agent_state.get_managed_digest`), not by the name itself.
_MANAGED_OWNED_NAME = "kirocrew-dashboard-author"


def _foreign_dashboard_author_spec_reason(spec: dict[str, Any] | None) -> str | None:
    """Why *spec*, read from the owned dashboard-author path, is not this installer's own
    managed write, or ``None`` when it REPRODUCES the installer-recorded ownership digest
    (so the installer may rewrite it).

    PROVENANCE by an installer-recorded digest in the ``agent_state`` sidecar, NOT by
    content marks. The dashboard-author stem was a user-creatable template name before it
    became owned, so a user could hand-place a ``kirocrew-dashboard-author.json`` -- and,
    as review found, so could a copy of ANOTHER owned agent (``kirocrew``, a conductor)
    renamed onto the stem, which would forge any mark chosen from the managed spec's own
    shape (its name, its ``kirocrew-core`` reference, even a narrowed server map). Content
    marks cannot tell such a duplicate apart from the managed spec. The digest can: it is
    recorded by the installer AFTER it lands a write (:func:`_install_dashboard_author_agent`
    calls :func:`kiro_crew.agent_state.set_managed_digest`), so only a file whose exact
    bytes reproduce that recorded value is confirmed as this installer's own last write.

    ``None`` -- "ours to rewrite" -- means exactly: *spec* is a dict whose canonical bytes
    (:func:`kiro_crew.agent_state.spec_digest`) equal the digest the installer recorded for
    this owned name. Everything else is foreign and left untouched:

    * NO digest is recorded (``get_managed_digest`` is ``None``) -- the installer never
      wrote here, so there is nothing to confirm and the present file is a user artefact;
    * *spec* is absent / unreadable / not a dict (the caller passes ``None``) -- a file
      whose provenance cannot even be parsed is never read as ours;
    * *spec*'s bytes do not reproduce the recorded digest -- it has been hand-edited, or it
      is a different agent's spec renamed onto the stem; either way not our last write.

    A crash between the write and the digest record leaves no digest, which fails CLOSED:
    the next rebuild sees "no ownership recorded" and declines rather than overwriting a
    file it cannot attribute. The install gate (:func:`_present_spec_blocks_install`)
    still refuses up front on an unreadable or symlinked file, so a broken file is covered
    there too.
    """
    if not isinstance(spec, dict):
        return None
    if not agent_state.managed_digest_matches(
        _MANAGED_OWNED_NAME, agent_state.spec_digest(spec), strict=True
    ):
        return "its bytes do not reproduce the installer-recorded ownership digest"
    return None


def _managed_dashboard_author_install_lands(path: Path) -> bool:
    """Would the managed install actually WRITE at *path* -- i.e. is the stem owned by a
    live installer that re-filters its grants every rebuild?

    True when :func:`_write_dashboard_author_spec` would land. The ``.json`` must not be
    blocked (absent, or it reproduces the installer-recorded ownership digest). A ``.md``
    sibling blocks only a FIRST-TIME install (an unconfirmed ``.json``, which the install
    must not write beside a user markdown spec); once the ``.json`` is CONFIRMED ours, the
    install refreshes it in place despite the sibling, so the fork-governance loop may treat
    it as owned (an owned writer re-filters its grants every rebuild). Mirrors the install
    gate exactly, so the two never disagree about whether this stem has a live writer.
    """
    if _present_spec_blocks_install(path) is not None:
        return False
    current_json = agent_mod._read_spec_capped(path) if path.is_file() else None
    json_confirmed = isinstance(current_json, dict) and _is_confirmed_managed_dashboard_author(
        current_json
    )
    if not json_confirmed:
        if _present_spec_blocks_install(path.parent / (path.stem + ".md")) is not None:
            return False
    return True


def _is_confirmed_managed_dashboard_author(spec: dict[str, Any] | None) -> bool:
    """Does *spec* POSITIVELY confirm as this installer's own managed dashboard-author?

    The fail-closed companion to :func:`_foreign_dashboard_author_spec_reason`, for the
    callers that must never strip a user's guards or skip filtering a user's approvals: the
    fork refresh, the capability-parent check and the governance filter. Those ask "is this
    confirmed OURS?", so an ABSENT, unreadable or non-dict spec (``None``) is confirmed as
    NOTHING -- it is treated as a user file (never refreshed, never guard-stripped, still
    filtered), NOT as "ours to rewrite". The install gate keeps using the reason helper,
    where a broken file IS replaceable so one truncated write cannot wedge the install
    forever; here the opposite default is the safe one.

    Confirmation is the installer-recorded ownership digest: *spec*'s canonical bytes
    reproduce the value :func:`kiro_crew.agent_state.get_managed_digest` holds for this
    owned name. No digest recorded, or bytes that do not reproduce it, is NOT confirmed.
    """
    if not isinstance(spec, dict):
        return False
    return _foreign_dashboard_author_spec_reason(spec) is None


def _install_dashboard_author_agent() -> None:
    """Generate and install the kirocrew-dashboard-author agent config.

    A dispatched, worker-shaped agent, but NOT the default-mirroring worker: it carries
    its own charter and its own tool surface, so it is built from scratch the way each
    conductor is rather than mirrored from the default spec. It authors ONE dashboard
    template and lands it as a pull request, so it mounts the two capabilities that work
    needs -- ``fs_write`` and ``execute_bash`` -- and nothing that would make it a
    conductor or a reporting worker: no ``session`` verb, no ``@kirocrew-work`` mount, no
    ``@kirocrew-dashboard`` publication surface, no ``code`` (governance classes it under
    ``filesystem.write``, a second path to two capabilities already mounted).

    ``fs_write`` and ``execute_bash`` are MOUNTED but never auto-approved, the line
    ``kirocrew-conductor`` draws and for the same reason: ``allowedTools`` has no argument
    matching, so a blanket write or shell grant cannot be told apart from "write anywhere"
    / "run anything", and the author's whole safety story is that a human reads its diff
    before it lands. Every auto-approved entry only READS or recalls -- the frontmatter
    ``allowedTools`` of the shipped ``agent-spec.md`` (``fs_read``, ``tool_search`` and the
    four read-only ``@kirocrew-core`` verbs) minus ``fs_read`` -- which is what makes
    granting it on the one path that never reaches the PreToolUse gate safe.

    Derived from the kirocrew agent so it inherits the resolved ``@kirocrew-core``
    invocation and the security hooks, then narrowed: the ``mcpServers`` map keeps only
    ``kirocrew-core`` (the one server any grant below names), and the KAS policy is
    derived from the FILTERED grant list rather than restated, so a ceiling that strips a
    grant strips its KAS rule with it. The shared writer version-gates it.

    The ``prompt``, ``description``, ``tools`` and ``allowedTools`` are ALL read from the
    shipped ``dashboard-template`` skill's ``agent-spec.md`` -- the one authoritative copy
    of the charter, which :func:`parse_markdown_spec` already parses from the package --
    rather than restated in Python literals that nothing holds equal to it. The
    frontmatter ``allowedTools`` is the SOURCE's authored auto-approve intent; the install
    drops ``fs_read`` from it (the one omission with a named reason below) and re-filters
    the rest through the governance ceiling, so what lands is the governed surface rather
    than a copy.
    """
    spec_source = parse_markdown_spec(_DASHBOARD_AUTHOR_SPEC_PATH.read_text(encoding="utf-8"))
    config = agent_mod.build_agent_config()
    config["name"] = _MANAGED_OWNED_NAME
    config["description"] = spec_source["description"]
    config["prompt"] = spec_source["prompt"]
    config["tools"] = list(spec_source["tools"])
    # ``allowedTools`` is the ONE path that never reaches the PreToolUse gate, so every
    # grant is filtered through the governance ceiling first -- a governed ref stays
    # MOUNTED (it is still in ``tools``) and simply prompts. The source frontmatter's
    # ``allowedTools`` is the authored auto-approve intent; we drop ``fs_read`` from it
    # because the ceiling withholds it from every derived spec's auto-approve list, so it
    # reaches the gate like ``fs_write`` and ``execute_bash`` do, and offering it only to
    # have it stripped would emit a withheld-audit event on every rebuild for a grant that
    # was never going to land. The rest is derived from the parsed spec rather than
    # restated, so the one authored list stays the single source of truth.
    config["allowedTools"] = auto_approve._filter_auto_approve(
        tuple(g for g in spec_source["allowedTools"] if g != "fs_read"),
        source="_install_dashboard_author_agent",
    )
    # Narrowed to the one server any grant above names. ``build_agent_config`` mounts the
    # whole managed set; a template is not published at run time, so the publication and
    # session-control servers are surface this charter cannot account for.
    mcp = config.get("mcpServers", {}) or {}
    core_entry = mcp.get("kirocrew-core")
    config["mcpServers"] = {"kirocrew-core": core_entry} if core_entry else {}
    # The KAS policy is derived at the write, by the shared tail
    # (``_write_dashboard_author_spec``), from the FILTERED grant list, so a ceiling that
    # strips a grant strips its KAS rule with it.
    agents_dir = agent_mod.kiro_agents_dir_path()
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / _DASHBOARD_AUTHOR_AGENT_FILENAME
    # The whole read -> attribute -> write runs under ``agents_spec_lock``, the template-spec
    # writer lock every other read-modify-writer of this directory holds (the worker
    # installer, the reset path, the fork refresh, the dashboard PATCH). Without it two
    # overlapping rebuilds could interleave their reads and writes. Holding the lock makes
    # the ownership attribution and the write one atomic step, as the sibling worker
    # installer does.
    with agent_mod.agents_spec_lock(agents_dir):
        # Preserve the two AUTHORIZED, persisted user settings a rebuild would otherwise
        # discard, taken ONLY from a file this installer confirms is its own prior managed
        # write (so a user file at the stem contributes nothing): an explicit ``model`` pin
        # (``model_managed`` False, the dashboard PATCH / reset-model contract) and the user
        # ``resources`` skill mappings. Everything else is regenerated from the shipped spec
        # and the governance ceiling, so a grant the ceiling strips still goes. Read once,
        # under the lock, so the carry-over cannot race the write.
        existing = agent_mod._read_spec_capped(path)
        removed_all_skills = False
        if isinstance(existing, dict) and _is_confirmed_managed_dashboard_author(existing):
            if agent_state.get_model_managed(_MANAGED_OWNED_NAME) is False:
                pinned = existing.get("model")
                if isinstance(pinned, str) and pinned.strip():
                    config["model"] = pinned
            # Mirror the confirmed spec's user skill mappings EXACTLY. "resources" PRESENT
            # (even an empty list) is the user's saved selection and is carried as-is; the
            # empty case matters -- an explicit remove-all-skills PATCH pops the key to [],
            # and carrying only a non-empty list would let the build's inherited default
            # (deep-merged from agent.json) restore the mapping the user removed. "resources"
            # ABSENT means the user holds no selection, so any inherited default is dropped.
            if "resources" in existing:
                carried = existing["resources"]
                if isinstance(carried, list):
                    config["resources"] = carried
                    # An explicit remove-all is the one selection the charter mapping
                    # below does not touch. "No skills" is a thing a person can SAY
                    # here -- the key is present and empty -- and it is pinned, so the
                    # mapping respects it and the spec keeps no skill at all.
                    removed_all_skills = not carried
            else:
                config.pop("resources", None)
        if not removed_all_skills:
            # The one skill the shipped charter names ("load the ``dashboard-template``
            # skill before you write anything"). AFTER the carry-over above, and by
            # MERGE: a person's saved selection keeps its entries and their order, and
            # only the missing charter skill is appended. Dropping just that one entry
            # from a non-empty list is therefore not expressible: a list without it
            # reads the same as one saved before the mapping existed, and re-adding it
            # is what carries the fix to everyone who already has a selection.
            skill_resources.map_prompt_skills(config, "kirocrew-dev/dashboard-template")
        _write_dashboard_author_spec(path, config)


def _write_dashboard_author_spec(path: Path, config: dict) -> None:
    """Write the managed spec, attributing any file already at *path* by the INSTALLER-RECORDED
    OWNERSHIP DIGEST. Caller holds ``agents_spec_lock``.

    POSITIVE-CONFIRMATION by the digest recorded in the ``agent_state`` sidecar
    (:func:`_foreign_dashboard_author_spec_reason`): the spec is written only when the path
    is FREE, or the file there reproduces the digest the installer recorded for its last
    managed write, i.e. our own prior landed write. A user who hand-placed a spec at this
    once-user-creatable stem -- or a copy of another owned agent renamed onto it -- is left
    untouched, because its bytes do not reproduce the recorded digest. The digest is recorded
    AFTER this write (below), under this same lock, so the record and the file move together.

    Three branches, exactly:

    * path ABSENT -> install (nothing to lose).
    * path PRESENT and the file reproduces the recorded ownership digest -> refresh is
      allowed (that digest IS the record that this is our own prior write).
    * path PRESENT not reproducing the digest (no digest recorded, hand-edited, or a
      different agent's spec renamed on), OR the file cannot be read -> leave it untouched
      and log once. We never overwrite a file we cannot positively confirm is our own write.

    BOTH candidate forms are checked before the name is claimed: kiro-cli reads
    ``<stem>.json`` and the v3 engine also reads ``<stem>.md``, and a ``.json`` written
    beside a user's ``.md`` would SHADOW that markdown spec. So a user ``.md`` at the stem
    blocks the install exactly as an unconfirmed ``.json`` does -- the installer never
    shadows a spec it did not write.
    """
    agents_dir = path.parent
    stem = path.stem
    md_candidate = agents_dir / (stem + ".md")
    # Is the ``.json`` already our confirmed managed write? Decide this FIRST, because it
    # changes whether a ``.md`` sibling may block. A ``.md`` at the stem must block a
    # FIRST-TIME install (writing the ``.json`` would shadow the user's markdown spec), but
    # it must NOT freeze an already-confirmed managed ``.json``: refusing to refresh that
    # ``.json`` would leave its auto-approvals live after the ceiling revoked them, since a
    # non-fork managed spec has no other writer to re-filter it. So a confirmed ``.json`` is
    # refreshed in place and the sibling ``.md`` is left untouched.
    current_json = agent_mod._read_spec_capped(path) if path.is_file() else None
    json_confirmed = isinstance(current_json, dict) and _is_confirmed_managed_dashboard_author(
        current_json
    )
    json_blocked = _present_spec_blocks_install(path)
    if json_blocked is not None:
        agent_mod.logger.warning(
            "Not installing the managed %s: %s. Leaving it untouched; remove or rename "
            "that file to let the managed agent install.",
            _DASHBOARD_AUTHOR_AGENT_FILENAME,
            json_blocked,
        )
        return
    # The ``.md`` sibling blocks only a first-time install (unconfirmed ``.json``). Once the
    # ``.json`` is confirmed ours, the ``.md`` does not stop the refresh.
    if not json_confirmed:
        md_blocked = _present_spec_blocks_install(md_candidate)
        if md_blocked is not None:
            agent_mod.logger.warning(
                "Not installing the managed %s: %s. Leaving it untouched; remove or rename "
                "that file to let the managed agent install.",
                _DASHBOARD_AUTHOR_AGENT_FILENAME,
                md_blocked,
            )
            return
    # Every candidate is free, or the ``.json`` reproduces the installer-recorded ownership
    # digest (so a refresh is allowed). Two-phase digest commit so a crash never leaves the
    # bytes and the record out of step: record the NEW bytes' digest as PENDING first, then
    # replace the file (``_atomic_json_write`` -- a tmp-file + atomic replace), then finalize.
    # At every instant the file reproduces either the old finalized digest or the pending
    # one, so a later rebuild always re-confirms and re-filters instead of freezing.
    #
    # The pending digest is taken INSIDE the shared writer, after its one governance
    # pass and before the replace, so it is the digest of exactly the bytes that land:
    # a ceiling that moves while this runs can change those bytes, never split them
    # from their record.
    current_digest = (
        agent_state.spec_digest(current_json) if isinstance(current_json, dict) else None
    )

    def _record_pending(governed: dict) -> None:
        agent_state.begin_managed_write(
            _MANAGED_OWNED_NAME, agent_state.spec_digest(governed), current=current_digest
        )

    auto_approve.write_governed_spec(
        path, config, source="_install_dashboard_author_agent", before_write=_record_pending
    )
    agent_state.finalize_managed_write(_MANAGED_OWNED_NAME)
    agent_mod.logger.info("Installed dashboard-author agent config: %s", path)


def _present_spec_blocks_install(candidate: Path) -> str | None:
    """Why a file already at *candidate* (a ``.json`` or ``.md`` at the dashboard-author
    stem) must block the managed install, or ``None`` when it does not.

    ``None`` means: the candidate is ABSENT, or it is a ``.json`` that reproduces the
    installer-recorded ownership digest (our own prior write, which the refresh may
    overwrite). A reason string means leave everything untouched:

    * a symlink / non-regular file -> never follow or clobber;
    * a read failure (oversized, non-UTF-8, unparseable) -> provenance unconfirmable;
    * a file that does not reproduce the recorded digest -> a user artefact or another
      agent's spec (its ``.json`` is not our write; ANY ``.md`` is a user spec, since the
      installer only ever writes ``.json``, so even a ``.md`` that reproduced the digest
      would still be the user's and is left untouched).

    The digest-reproducing ``.json`` refresh case is the ONE overwrite the installer makes;
    everything else is protected.
    """
    if not os.path.lexists(candidate):
        return None
    if candidate.is_symlink() or not candidate.is_file():
        return f"{candidate} is not a regular file"
    current = agent_mod._read_spec_capped(candidate)
    if current is None:
        return f"the file at {candidate} could not be read, so its provenance cannot be confirmed"
    # The installer only ever writes the ``.json`` form, so a ``.md`` is never its own write:
    # a ``.md`` is always a user spec and must be left untouched. Only a ``.json`` may be
    # confirmed as ours and refreshed.
    if candidate.suffix.lower() != ".json":
        return f"a user markdown spec exists at {candidate}"
    reason = _foreign_dashboard_author_spec_reason(current)
    if reason is not None:
        return f"a file at {candidate} is not this installer's own ({reason})"
    return None
