"""The hire gallery's catalog: every template a crew member can be hired from.

Three sources, one shape (design step 6, *Crew Member = Custom Agent + Wrapper
Layer*): the job cards enabled installed apps offer in their manifest's
``crew.templates`` (:mod:`kiro_crew.member_templates`), the agent files this
package ships (built-ins), and the user's own agent files under the agents
directory. A member's private copy is never a template (it is one colleague's
definition, not a posting), and an app's materialized agent is offered only
through its card -- the card is what carries the role, the duty and the face.

Everything here is a READ: the catalog is assembled from the manifests, the
agent files and the crewmate ENROLLMENT records, and answers what the gallery
renders plus the exact ``source`` body a hire of each card sends. A card's
``hired_as`` is the crewmates hired from it -- the enrolled, active roster
whose record names the card's template -- never a row that merely binds the
card's file (a session agent is not a crewmate). Blocking file IO throughout:
call off the loop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from kiro_crew import member_templates
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.agent_discovery import AgentInfo, _extract_skills, _mcp_server_names, list_agents
from kiro_crew.apps.manager import get_app_manifest, list_apps
from kiro_crew.apps.manifest import CREW_CATEGORIES, CrewTemplate
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.member_identity import effective_display_name

logger = logging.getLogger(__name__)

#: The gallery never offers these as templates: the two files Kiro Crew's own
#: setup writes are the assistant the ``default`` member already IS.
_ASSISTANT_SOURCES = frozenset({"kirocrew"})

_WORD_BREAK_RE = re.compile(r"[-_.]+")


@dataclass
class TemplateCard:
    """One gallery card, whatever its origin. ``source`` is the hire body's
    ``source`` -- what ``POST /api/members`` takes to hire from this card."""

    id: str
    origin: str  # "app" | "builtin" | "local"
    source: dict[str, str]
    role: str
    duty: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = "other"
    starter_prompts: list[dict[str, str]] = field(default_factory=list)
    avatar: dict[str, Any] = field(default_factory=dict)
    publisher: str = ""
    version: str = ""
    #: The template's agent as the member would copy it: its name, and what
    #: the definition brings along (skills, MCP servers).
    agent: str = ""
    capabilities: list[dict[str, str]] = field(default_factory=list)
    #: The crewmates hired from this card and still enrolled, each
    #: ``{"id", "display_name"}`` in roster order: the id opens the DM, the
    #: name is what the gallery's "Chat with" / picker say.
    hired_as: list[dict[str, str]] = field(default_factory=list)
    #: False when the card is listed but a hire would be refused right now
    #: (the app's agent is not materialized, its spec unreadable...); the
    #: refusal's code says why, in the hire's own vocabulary.
    hireable: bool = True
    unavailable_code: str = ""
    unavailable_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "origin": self.origin,
            "source": dict(self.source),
            "role": self.role,
            "duty": self.duty,
            "description": self.description,
            "tags": list(self.tags),
            "category": self.category if self.category in CREW_CATEGORIES else "other",
            "starter_prompts": [dict(s) for s in self.starter_prompts],
            "avatar": dict(self.avatar) if self.avatar else None,
            "publisher": self.publisher,
            "version": self.version,
            "agent": self.agent,
            "capabilities": [dict(c) for c in self.capabilities],
            "hired_as": [dict(m) for m in self.hired_as],
            "hireable": self.hireable,
            "unavailable_code": self.unavailable_code,
            "unavailable_reason": self.unavailable_reason,
        }


def _first_sentence(text: str) -> str:
    """The one-line duty a card without one falls back to: its description's
    first sentence, so the card face never reads empty beside a described role."""
    text = " ".join(text.split())
    if not text:
        return ""
    head = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return head if len(head) <= 160 else head[:157].rstrip() + "..."


def humanize_agent_name(name: str) -> str:
    """``pipeline-conductor`` -> ``Pipeline Conductor``: the role a file with
    no card is offered under. Already-cased words keep their case."""
    words = [w for w in _WORD_BREAK_RE.split(name) if w]
    return " ".join(w if any(c.isupper() for c in w[1:]) else w[:1].upper() + w[1:] for w in words)


def capabilities_of(
    spec: dict[str, Any] | None, info: AgentInfo | None = None
) -> list[dict[str, str]]:
    """What a definition brings along, for the card's *Built-in capabilities*:
    its skills and its MCP servers, each once, skills first."""
    skills: list[str] = []
    servers: list[str] = []
    if info is not None:
        skills = list(info.skills)
        servers = list(info.mcp_servers)
    elif isinstance(spec, dict):
        skills = _extract_skills(spec)
        servers = _mcp_server_names(spec)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, names in (("skill", skills), ("mcp", servers)):
        for name in names:
            if isinstance(name, str) and name and (kind, name) not in seen:
                seen.add((kind, name))
                out.append({"kind": kind, "name": name})
    return out


Enrolled = Mapping[str, Mapping[str, Any]]


def _hired_from(cfg: KiroCrewConfig, enrolled: Enrolled, ref: str) -> list[dict[str, str]]:
    """The crewmates hired from the template *ref* (``app/agent`` for a store
    card, the file's agent name for a local one): the ENROLLED rows whose
    record names it, with the display name the roster shows. Retired members
    have no record; a row bound to the file without a record is a session
    agent, not a hire."""
    out: list[dict[str, str]] = []
    for name, row in cfg.agents.items():
        record = enrolled.get(name)
        if record is None or record.get("template") != ref:
            continue
        out.append({"id": name, "display_name": effective_display_name(name, row.display_name)})
    return out


def _app_cards(
    cfg: KiroCrewConfig, enrolled: Enrolled, apps: list[dict[str, Any]]
) -> list[TemplateCard]:
    cards: list[TemplateCard] = []
    for app in apps:
        name = app.get("name")
        if not isinstance(name, str) or not name or not app.get("enabled", False):
            continue
        manifest = get_app_manifest(name)
        if manifest is None or not manifest.crew.templates:
            continue
        publisher = manifest.displayName or name
        for card in manifest.crew.templates:
            cards.append(
                _app_card(
                    cfg,
                    enrolled,
                    name,
                    publisher,
                    str(app.get("version") or manifest.version),
                    card,
                )
            )
    return cards


def _app_card(
    cfg: KiroCrewConfig,
    enrolled: Enrolled,
    app: str,
    publisher: str,
    version: str,
    card: CrewTemplate,
) -> TemplateCard:
    out = TemplateCard(
        id=f"app:{app}/{card.agent}",
        origin="app",
        source={"kind": "store", "app": app, "agent": card.agent},
        role=card.role,
        duty=card.duty or _first_sentence(card.description),
        description=card.description,
        tags=list(card.tags),
        category=card.scenario,
        starter_prompts=[dict(s) for s in card.starter_prompts],
        avatar=card.member_avatar,
        publisher=publisher,
        version=version,
    )
    try:
        template = member_templates.resolve_store_template(app, card.agent)
    except member_templates.TemplateUnavailable as exc:
        out.hireable = False
        out.unavailable_code = exc.code
        out.unavailable_reason = str(exc)
        # The crewmates already hired from this template are the owner's,
        # whatever the template's state today: a card that cannot be hired
        # from still offers "Chat with" / the picker. Attributed by the
        # canonical ref -- the refusal's own when the shipped spec was read
        # (the declared name is known), else the card's file stem, which is
        # the declared name in every case but a spec that renames itself.
        ref = exc.ref or member_templates.template_ref(app, Path(card.agent).stem)
        out.hired_as = _hired_from(cfg, enrolled, ref)
        return out
    out.agent = template.agent_name
    out.version = template.version or version
    out.capabilities = capabilities_of(template.materialized_spec)
    out.hired_as = _hired_from(cfg, enrolled, template.ref)
    return out


def _file_cards(
    cfg: KiroCrewConfig, enrolled: Enrolled, agents: list[AgentInfo]
) -> list[TemplateCard]:
    cards: list[TemplateCard] = []
    for info in agents:
        if info.private_to or info.scope != "global":
            continue  # a member's own copy, or a project checkout's file
        if info.source in _ASSISTANT_SOURCES or info.source in ("app", "package"):
            # The assistant is the default member; an app's agent is offered
            # through its card; a package's agent is the package's to offer.
            continue
        origin = "builtin" if info.source == "builtin" else "local"
        cards.append(
            TemplateCard(
                id=f"{origin}:{info.name}",
                origin=origin,
                source={"kind": "local", "agent": info.name},
                role=humanize_agent_name(info.name),
                duty=info.description,
                description=info.description,
                publisher="Kiro Crew" if origin == "builtin" else "",
                agent=info.name,
                capabilities=capabilities_of(None, info),
                hired_as=_hired_from(cfg, enrolled, info.name),
            )
        )
    return cards


def enrolled_records(cfg: KiroCrewConfig) -> dict[str, dict[str, Any]]:
    """``{id: enrollment record}`` for the rows on the roster -- the same
    predicate ``GET /api/members`` applies (``enrolled_member_ids``), read
    lenient: an unreadable sidecar attributes no hire to any card, the way it
    lists no member. The predicate is the record module's own
    (``agent_state.enrolled_member_ids``), the one the roster route wraps."""
    from kiro_crew import agent_state

    ids = agent_state.enrolled_member_ids(cfg.agents)
    if not ids:
        return {}
    try:
        records = agent_state.all_crewmate_records()
    except (OSError, ValueError):
        return {}
    return {name: records[name] for name in ids if name in records}


def build_catalog(
    cfg: KiroCrewConfig | None = None, *, enrolled: Enrolled | None = None
) -> list[TemplateCard]:
    """Every card the gallery lists: app cards first (in app order), then the
    built-ins, then the user's files, each group by name. *enrolled* is the
    roster's enrollment records (:func:`enrolled_records` when omitted)."""
    cfg = cfg if cfg is not None else KiroCrewConfig.load()
    if enrolled is None:
        enrolled = enrolled_records(cfg)
    cards = _app_cards(cfg, enrolled, list_apps())
    files = _file_cards(cfg, enrolled, list(list_agents(agents_dir=kiro_agents_dir_path())))
    files.sort(key=lambda c: (c.origin != "builtin", c.role.lower()))
    return cards + files
