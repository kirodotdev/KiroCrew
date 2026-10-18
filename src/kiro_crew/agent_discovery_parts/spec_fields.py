"""The discovery record, and what one agent spec's fields read as.

``AgentInfo`` with the two discovery scopes and the test for Kiro Crew's own generated
specs, then the field readers: the string and model fields, the skill resources and the
``skill://`` URIs they name, and the welcome message a new chat shows.

:mod:`kiro_crew.agent_discovery` stays the import path and the patch surface; see
:mod:`kiro_crew.agent_discovery_parts`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.agent_spec_format import is_app_agent_launch_name, is_native_skill_alias_name
from kiro_crew.dashboard.side_readonly_spec import is_readonly_spec_description

# Discovery scopes. A ``project`` agent comes from the session's own checkout and
# SHADOWS a ``global`` agent of the same name, mirroring kiro-cli: it resolves
# ``--agent`` against ``$PWD/.kiro/agents`` before ``~/.kiro/agents``. Kiro Crew
# spawns kiro-cli with the session's project dir as cwd, so the shadowing is a
# property of the backend rather than a policy choice here — surfacing the losing
# entry as separately selectable would advertise an agent that cannot be reached.
SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"


@dataclass
class AgentInfo:
    """Metadata for an installed kiro-cli agent."""

    name: str
    filename: str
    description: str
    model: str
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    source: str = "builtin"  # "kirocrew" | "package" | "builtin"
    package: str = ""  # AIM package name
    scope: str = SCOPE_GLOBAL  # "global" | "project"
    # Display-only provenance, deliberately NOT folded into ``source``: that field
    # also gates agent auto-creation and delete-blocking, so widening it to cover
    # the helper specs would silently change both.
    kirocrew_owned: bool = False
    # Fork lineage from the agent_state sidecar: set when this template is one
    # crew's private copy of another (blueprint semantics). ``private_to`` also
    # gates the sync loop's agent auto-creation — a private copy is not a
    # standalone template deserving its own agent.
    forked_from: str = ""
    private_to: str = ""
    # True when the row is a markdown spec AND no JSON spec claims its name in
    # any scope kiro-cli resolves (``agent.markdown_spec_for_agent``'s rule).
    # Hosts outside ``ACP_BACKENDS_MARKDOWN_AGENT_SPECS`` cannot load it, so a
    # picker can say so before a spawn is refused. Display-only: no spawn path
    # reads it.
    markdown_only: bool = False

    def __post_init__(self) -> None:
        """Make the annotations above TRUE, at every construction site.

        ``~/.kiro/agents`` is a SHARED directory: ACP adapters and IDE plugins
        drop their own specs there and do not all spell every field as a plain
        string (observed: ``"model": {"id": "anthropic:claude-opus-4-8"}``, and
        bare ``null``). ``to_dict()`` is what ``/api/agents/installed``
        serialises, so any such value reached the dashboard verbatim; rendered as
        a JSX child it threw React error #31 ("Objects are not valid as a React
        child") and put the WHOLE Agent Templates tab into the error boundary —
        every other agent's row with it.

        The enforcement lives HERE rather than in per-field calls at each caller
        because the fields are rendered bare in several places (`{a.name}`,
        `{a.package}`, `<SourceBadge source={a.source}>`, `a.filename.startsWith`)
        and there are two construction sites, one of them an out-of-tree edition
        seam. A per-field fix at one caller only *looks* complete: the next
        foreign spelling, or the next field someone renders, reopens the same
        whole-tab crash. A constructor invariant cannot be forgotten.

        Fallbacks are per field because they are not interchangeable: ``model``
        defers to ``"auto"`` (see :func:`spec_model` — the same "non-string means
        no pin" rule the execution path applies), ``source`` to its
        ``"builtin"`` default, and the free-text fields to empty.
        """
        for name, fallback in (
            ("name", ""),
            ("filename", ""),
            ("description", ""),
            ("model", _DEFER_MODEL),
            ("source", "builtin"),
            ("package", ""),
            ("scope", SCOPE_GLOBAL),
            ("forked_from", ""),
            ("private_to", ""),
        ):
            if not isinstance(getattr(self, name), str):
                setattr(self, name, fallback)
        # ``list[str]`` is equally load-bearing: these elements are rendered as
        # skill/server chips. Drop the unusable ones rather than the whole list,
        # so a spec with one bad entry keeps the rest.
        self.skills = [s for s in self.skills if isinstance(s, str)]
        self.mcp_servers = [s for s in self.mcp_servers if isinstance(s, str)]
        # The out-of-tree edition seam passes this through from a raw row, and a
        # truthy non-bool would render as a provenance claim nothing verified.
        self.kirocrew_owned = self.kirocrew_owned is True
        self.markdown_only = self.markdown_only is True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_internal_agent_spec(info: object) -> bool:
    """Whether a discovery row is one of Kiro Crew's own generated specs.

    Two kinds are published into the user-level agents directory, the only place
    kiro-cli can load them from, and neither is a sub-agent:

    * the derived read-only spec a side turn runs under
      (``dashboard.side_readonly_spec``, ``<agent>--readonly``), identified by
      the owner marker its ``description`` opens with -- the same marker
      publication uses to tell its own file from a user's, so a hand-authored
      agent that merely ends in ``--readonly`` stays spawnable;
    * a skill-view alias (``kirocrew-skill-view-*``), which discovery already
      leaves out of the roster; matched here too so a row built some other way
      cannot put one back. An app launch copy (``kirocrew-app-launch-*``,
      ``apps.agent_launch``) is matched the same way.

    A roster that offers either sends the model straight into a spawn that
    fails: the read-only spec is written per side turn, after the shared
    kiro-cli listed its agents, so the child never advertises it. Reads fields
    with ``getattr`` because the rosters' tests and edition seams hand in rows
    that are not full ``AgentInfo`` objects.
    """
    if is_readonly_spec_description(getattr(info, "description", None)):
        return True
    return any(
        isinstance(value, str)
        and (is_native_skill_alias_name(value) or is_app_agent_launch_name(value))
        for value in (getattr(info, "filename", None), getattr(info, "name", None))
    )


SKILL_URI_PREFIX = "skill://"
#: The Crew tool that loads a skill a bounded directory lists.
_SKILL_SEARCH_TOOL = "@kirocrew-core/skill_search"

# Kiro Crew-only spec convention, predating ``.kiro/agents/`` and still used by
# projects driven from Slack. kiro-cli does NOT read this location, so a name
# declared only here is NOT dispatchable: offering it as an agent would hand
# kiro-cli a mode it cannot activate. It is therefore excluded from discovery by
# default and included only for Slack's own listing/resolution, which predates
# this scope — see :func:`project_agent_files`.
AGENT_SPEC_SUFFIX = ".agent-spec.json"


# The "no pin, defer to the tier below" spelling. Mirrors
# ``config.loader.DEFAULT_MODEL``; duplicated as a literal rather than imported
# so this module keeps its leaf-level import graph (``config.paths`` only).
_DEFER_MODEL = "auto"


def spec_str(data: dict[str, Any], key: str, default: str = "") -> str:
    """Read a raw spec field as a ``str``, for callers that are NOT an AgentInfo.

    ``AgentInfo.__post_init__`` is what enforces the type contract for the
    dataclass; this is its counterpart for the two places that hand spec fields
    to the dashboard WITHOUT going through it:

    - ``api_agent_detail``, which returns ``{**data, ...}`` — the raw on-disk spec
      — so the detail panel receives whatever the file happened to contain.
    - ``list_agents``, for ``name``, where the useful fallback is the file's own
      stem rather than the generic empty string.

    ``~/.kiro/agents`` is a SHARED directory: other tools (ACP adapters, IDE
    plugins) drop their own specs there and do not all spell every field as a
    plain string. Observed in the wild: ``"model": {"id":
    "anthropic:claude-opus-4-8"}``, and a bare ``null``. Rendered as a React
    child, either throws error #31 and blanks the whole Agent Templates tab.
    """
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def spec_model(data: dict[str, Any]) -> str:
    """The ``model`` of an agent spec, coerced to the ``str`` AgentInfo declares.

    A non-string is treated as "no pin" (``"auto"``), which is exactly the rule
    :func:`config.loader.normalize_agent_model` already applies to the same file
    on the EXECUTION path. Keeping both sides on that rule is deliberate: the
    resolver would collapse a structured model to "defer" regardless, so
    extracting ``id`` here would make the displayed chip disagree with the model
    actually used — and ``anthropic:claude-opus-4-8`` is a provider-prefixed id
    kiro-cli would reject anyway, turning a display bug into a spawn failure.

    It also leaked into ``subagent.py``'s spawn kwargs as a ``--model`` argument.
    """
    return spec_str(data, "model", _DEFER_MODEL)


def _builder_mcp_skills(data: dict[str, Any]) -> list[str]:
    """Extract skill names from builder-mcp args (--skill-name-filter)."""
    mcp = data.get("mcpServers") or {}
    if not isinstance(mcp, dict):
        return []
    bm = mcp.get("builder-mcp") or {}
    if not isinstance(bm, dict):
        return []
    args = bm.get("args") or []
    skills: list[str] = []
    for i, arg in enumerate(args):
        if arg == "--skill-name-filter" and i + 1 < len(args):
            skills.extend(s.strip() for s in args[i + 1].split(",") if s.strip())
    return skills


def skill_resource_uris(data: dict[str, Any]) -> list[str]:
    """Return the ``skill://`` entries of an agent spec's ``resources``, in order.

    These are the kiro-cli-native mapping of skills to an agent: kiro-cli loads
    every ``SKILL.md`` matched by these URIs when the agent is spawned with
    ``--agent``. Non-``skill://`` resources (``file://`` steering globs and
    friends) are user-owned and deliberately excluded.
    """
    resources = data.get("resources") or []
    if not isinstance(resources, list):
        return []
    return [r for r in resources if isinstance(r, str) and r.startswith(SKILL_URI_PREFIX)]


def _skills_from_resources(data: dict[str, Any]) -> list[str]:
    """Derive display names for the skills an agent maps via ``skill://``.

    Lexical only (no filesystem access) so :func:`list_agents` stays a
    stat+parse operation: a skill directory is named by the path segment
    directly above ``SKILL.md``, which is the skill's name in every supported
    layout. A wildcard segment (``skill://~/.kiro/skills/*/SKILL.md`` — "every
    skill in this root") has no single name, so the pattern is surfaced
    verbatim rather than silently dropped.
    """
    names: list[str] = []
    for uri in skill_resource_uris(data):
        parts = [p for p in uri[len(SKILL_URI_PREFIX) :].split(sep="/") if p]
        if not parts:
            continue
        # Trailing SKILL.md (or any *.md) is the file, not the skill name.
        if parts[-1].lower().endswith(".md"):
            parts.pop()
        if parts:
            names.append(parts[-1])
    return names


def _extract_skills(data: dict[str, Any]) -> list[str]:
    """All skills mapped to an agent, de-duplicated, order-preserving.

    Two independent mapping mechanisms are unioned:

    1. ``skill://`` entries in ``resources`` — the kiro-cli-native mapping
       (what the dashboard's Agent Templates editor writes).
    2. ``builder-mcp --skill-name-filter`` args — an edition-specific
       convention that predates the ``resources`` support.
    """
    seen: set[str] = set()
    out: list[str] = []
    for name in (*_skills_from_resources(data), *_builder_mcp_skills(data)):
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def expand_skill_uri(
    uri: str, agent_path: Path, *, project_dir: str | Path | None = None
) -> str | None:
    """Expand a ``skill://`` resource URI into an fnmatch glob over real paths.

    kiro-cli accepts ``skill://~/.kiro/skills/*/SKILL.md`` (global),
    ``skill:///abs/path/SKILL.md`` (absolute), and
    ``skill://.kiro/skills/*/SKILL.md`` (workspace-relative to the cwd at
    session start).

    A workspace-relative URI resolves against *project_dir* when the caller
    supplies one — every path that resolves skills for a prompt passes the
    session's own project, which is the cwd kiro-cli is launched in. With no
    project supplied it falls back to the project root inferred from
    *agent_path*: for the ``<project>/.kiro/agents/foo.json`` layout that is three
    levels up (``foo.json`` -> ``agents`` -> ``.kiro`` -> ``<project>``), so
    appending the ``.kiro/``-prefixed glob yields ``<project>/.kiro/...`` without
    doubling the ``.kiro`` segment. Both are best-effort: the cwd kiro-cli
    actually uses may differ.

    Returns ``None`` for anything that is not a ``skill://`` URI.
    """
    if not uri.startswith(SKILL_URI_PREFIX):
        return None
    raw = uri[len(SKILL_URI_PREFIX) :]
    if raw.startswith("~/"):
        return str(Path.home() / raw[2:])
    if raw.startswith("/") or Path(raw).is_absolute():
        return raw
    base = Path(project_dir) if project_dir else agent_path.parent.parent.parent
    return str(base / raw)


def skill_uri_for_path(skill_md: Path) -> str:
    """Render a ``SKILL.md`` path as a ``skill://`` resource URI.

    The forward direction of :func:`expand_skill_uri`, and the one renderer both
    writers that MINT a mapping call -- the Agent Templates editor
    (``dashboard.handlers._shared.skill_uri_for_key``) and the builtin-agent
    installers (``agent_materialization.skill_resources``) -- so a mapping written
    by either expands back to the same path.

    A path under ``$HOME`` is emitted in ``~/`` form: kiro-cli expands it, and it
    keeps the written spec portable across machines and home directories. Anything
    else is emitted absolute, the form :func:`expand_skill_uri` returns unchanged.

    ``agent._relocated_skill_uri`` renders a ``skill://`` URI too and deliberately
    does NOT come here: it REPOINTS an existing mapping at a relocated skill and
    keeps whichever form that mapping was already written in, so a ``~/`` entry a
    person authored stays portable instead of being rewritten absolute. Minting and
    repointing are different rules, which is why there are two.
    """
    try:
        rel_home = skill_md.relative_to(Path.home())
    except ValueError:
        return f"{SKILL_URI_PREFIX}{skill_md.as_posix()}"
    return f"{SKILL_URI_PREFIX}~/{rel_home.as_posix()}"


#: Ceiling on a ``welcomeMessage`` rendered into a chat transcript. The field is
#: authored in a user-writable, tool-shared directory, so its length is not a
#: trusted quantity: an unbounded value would be persisted into the slot window
#: and re-broadcast to every open tab on each restore. Truncated rather than
#: refused — a long hint is still the author's intent, and dropping it silently
#: reproduces exactly the "accepted but invisible" behaviour this reader exists
#: to remove.
WELCOME_MESSAGE_MAX_CHARS = 2000


def spec_welcome_message(data: dict[str, Any]) -> str:
    """The display-ready ``welcomeMessage`` of a parsed agent spec, or ``""``.

    Coerced through :func:`spec_str` for the reason documented there: this key
    is read from ``~/.kiro/agents``, a directory other tools also write, so a
    structured or ``null`` value is "absent" rather than an error. Surrounding
    whitespace is stripped and a whitespace-only value collapses to ``""``, so
    a blank hint renders nothing instead of an empty bubble.

    Truncated at :data:`WELCOME_MESSAGE_MAX_CHARS` with an ellipsis, so the
    caller can append the result without re-checking its size.
    """
    text = spec_str(data, "welcomeMessage").strip()
    if len(text) > WELCOME_MESSAGE_MAX_CHARS:
        # The ellipsis is part of the budget, not an addition to it: the ceiling
        # is what callers are promised, so a result of cap+1 would break the one
        # guarantee this function makes.
        text = text[: WELCOME_MESSAGE_MAX_CHARS - 1].rstrip() + "\u2026"
    return text
