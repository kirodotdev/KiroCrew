"""Declared and observed facts about a server's use of the project folder.

Roots keep a name per chat only when every source agrees. A vault reference in
any declaration, an edition listing, or an effective ``not_used`` answer keeps
the usual question and approval path. Aliases join these facts; saved launch
bindings use exact names and only the agent specs in the source directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Mapping

from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway import hazards
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger(__name__)

SOURCE_CREW = "crew"
SOURCE_EDITION = "edition"


def edition_folder_free() -> frozenset[str]:
    """Server names the edition declares folder-free; older editions declare none."""
    from kiro_crew.platform.context import current_context, safe_context_call

    def _read() -> frozenset[str]:
        reader = getattr(current_context().mcp_tooling, "folder_free_mcp_servers", None)
        names = reader() if callable(reader) else ()
        return frozenset(n for n in names if isinstance(n, str) and n)

    return safe_context_call(
        _read,
        fallback_factory=frozenset,
        log_message="folder_free_mcp_servers lookup failed; declaring no server folder-free",
    )


def folder_free_source(
    name: str,
    edition: frozenset[str] | None = None,
    stored_answer: str | None = None,
    *,
    asked_roots: bool = False,
) -> str | None:
    """A fact that clears the question; roots or ``uses_cwd`` contradict the edition."""
    if name in KIROCREW_BIN_MCP_SERVERS:
        return SOURCE_CREW
    if stored_answer == "uses_cwd" or asked_roots:
        return None
    if roots_observed(name, edition_folder_free() if edition is None else edition):
        return SOURCE_EDITION
    return None


def roots_observed(name: str, observed_names: Collection[str]) -> bool:
    """Whether *name* or its canonical alias matches a recorded name."""
    if name in observed_names:
        return True
    alias = mcp_server_alias(name)
    return any(mcp_server_alias(observed) == alias for observed in observed_names)


def asks_for_roots(observed: Mapping[str, tuple[str, ...]], name: str) -> bool:
    """Whether the hazard ledger holds a roots request by *name* or its alias."""
    return roots_observed(
        name,
        frozenset(n for n, facts in observed.items() if hazards.HAZARD_ROOTS_REQUESTED in facts),
    )


def roots_keeps_per_chat(
    *,
    asked_roots: bool,
    any_vault_declaration: bool,
    edition_listed: bool,
    stored_answer: str | None,
) -> bool:
    """Apply roots only when no source disagrees with keeping this name per chat."""
    return (
        asked_roots
        and not any_vault_declaration
        and not edition_listed
        and stored_answer != "not_used"
    )


def effective_answer(
    name: str, answers: Mapping[str, str], bindings: Mapping[str, str], current_launch: str | None
) -> str | None:
    """Prefer the exact name's valid answer; otherwise consult launch-bound aliases."""
    from kiro_crew.config.integration_sections import effective_cwd_answer

    exact = effective_cwd_answer(answers.get(name), bindings.get(name), current_launch)
    if exact in {"not_used", "uses_cwd"}:
        return exact
    found = {
        effective_cwd_answer(answer, bindings.get(key), current_launch)
        for key, answer in answers.items()
        if roots_observed(name, (key,))
    }
    if "not_used" in found:
        return "not_used"
    if "uses_cwd" in found:
        return "uses_cwd"
    return None


@dataclass
class FolderDeclarations:
    """Value-free facts from the same source fleet for every roots reader."""

    vault: set[str] = field(default_factory=set)
    launches: dict[str, set[str]] = field(default_factory=dict)
    complete: bool = True

    def launch(self, name: str) -> str | None:
        from kiro_crew.mcp_gateway.launch_approval import serialize_identities

        ids = self.launches.get(name)
        return serialize_identities(ids) if ids else None

    def per_chat(
        self,
        names: Collection[str],
        observed: Collection[str],
        edition: frozenset[str],
        answers: Mapping[str, str],
        bindings: Mapping[str, str],
    ) -> frozenset[str]:
        """One name-wide decision; an unreadable source cannot establish agreement."""
        if not self.complete:
            return frozenset()
        return frozenset(
            name
            for name in names
            if roots_keeps_per_chat(
                asked_roots=roots_observed(name, observed),
                any_vault_declaration=roots_observed(name, self.vault),
                edition_listed=roots_observed(name, edition),
                stored_answer=effective_answer(name, answers, bindings, self.launch(name)),
            )
        )


@dataclass(frozen=True)
class ProjectRouting:
    """Which servers get one backend per project (``daemon.project_scope``).

    A server known to be folder-free (Kiro Crew's own, the edition's list, or a
    ``not_used`` answer for its current launch) keeps the one shared backend.
    Every other server is treated as reading its folder and gets a per-project
    stub, unless its name is in *dont_share* (an operator's ``stub_overrides``
    ``false``), a declaration names a vault secret, or a source could not be read.
    """

    edition: frozenset[str]
    answers: Mapping[str, str]
    bindings: Mapping[str, str]
    observed_roots: frozenset[str]
    dont_share: frozenset[str]
    declarations: FolderDeclarations

    def per_project(self, name: str) -> bool:
        if not self.declarations.complete:
            return False
        if roots_observed(name, self.dont_share) or roots_observed(name, self.declarations.vault):
            return False
        answer = effective_answer(name, self.answers, self.bindings, self.declarations.launch(name))
        if answer == "not_used":
            return False
        return (
            folder_free_source(
                name,
                self.edition,
                answer,
                asked_roots=roots_observed(name, self.observed_roots),
            )
            is None
        )

    def folder_free(self, name: str) -> bool:
        """Whether a fact says *name* can run in one folder for all chats."""
        answer = effective_answer(name, self.answers, self.bindings, self.declarations.launch(name))
        if answer == "not_used":
            return True
        return (
            folder_free_source(
                name,
                self.edition,
                answer,
                asked_roots=roots_observed(name, self.observed_roots),
            )
            is not None
        )

    def fingerprint(self) -> dict[str, Any]:
        """The restart-only inputs a cached overlay was routed by.

        *observed_roots* is left out: it moves while the gateway runs, so the
        rewrite fingerprints it on its own, among the inputs a refresh may change.
        """
        return {
            "edition": sorted(self.edition),
            "answers": sorted(self.answers.items()),
            "bindings": sorted(self.bindings.items()),
            "dont_share": sorted(self.dont_share),
            "complete": self.declarations.complete,
        }


def read_declarations(source_dir: Path, mate_source_dir: Path | None = None) -> FolderDeclarations:
    """Read agent, settings and admitted crewmate declarations. BLOCKING, values discarded."""
    from kiro_crew.agent_discovery import read_agent_spec_strict
    from kiro_crew.agent_spec_format import iter_agent_spec_files_strict
    from kiro_crew.mcp_gateway.hashing import hash_command
    from kiro_crew.mcp_gateway.mate_overlays import list_mate_sources, with_conductor_source
    from kiro_crew.mcp_gateway.secret_uri import secret_reference_keys
    from kiro_crew.user_json import loads_user_json

    facts = FolderDeclarations()

    def collect(spec: Any, *, binding: bool = False) -> None:
        servers = spec.get("mcpServers") if isinstance(spec, dict) else None
        if not isinstance(servers, dict):
            return
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            if not binding:
                if secret_reference_keys(entry.get("env")):
                    facts.vault.add(mcp_server_alias(name))
                continue
            command, args = entry.get("command"), entry.get("args")
            if isinstance(command, str) and command:
                argv = [str(a) for a in args] if isinstance(args, list) else []
                facts.launches.setdefault(name, set()).add(hash_command(command, argv))

    try:
        try:
            agent_paths = iter_agent_spec_files_strict(source_dir)
        except FileNotFoundError:
            agent_paths = []
        paths = with_conductor_source(list(agent_paths), mate_source_dir)
        if mate_source_dir is not None:
            mates = list_mate_sources(mate_source_dir)
            facts.complete = mates.complete
            paths.extend(mates.files)
        for sources, binding in ((agent_paths, True), (paths, False)):
            for path in sources:
                try:
                    collect(
                        read_agent_spec_strict(
                            path, operation="mcp_folder_facts", source="unknown"
                        ),
                        binding=binding,
                    )
                except (OSError, ValueError):
                    # The rewriter and the row scan skip an invalid or unreadable
                    # spec too, so one bad file leaves every other launch readable.
                    continue
        settings = source_dir.parent / "settings" / "mcp.json"
        try:
            collect(loads_user_json(settings.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except ValueError:
            pass  # Invalid settings inject no servers, as in the rewriter.
    except OSError:
        facts.complete = False
        logger.warning("MCP folder facts could not read every source; keeping normal approvals")
    return facts


def roots_requesting(socket_path: str | Path = "") -> frozenset[str]:
    """Every name the gateway saw ask for roots. BLOCKING file read."""
    from kiro_crew.mcp_gateway.rewriter_overlay import records_dir

    try:
        observed = hazards.load_ledger(records_dir(socket_path)).as_dict()
    except OSError:
        return frozenset()
    return frozenset(n for n, facts in observed.items() if hazards.HAZARD_ROOTS_REQUESTED in facts)
