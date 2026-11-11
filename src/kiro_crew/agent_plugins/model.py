"""Records the Agent Plugins loader produces.

Plain data only: discovery fills these in and nothing here touches the disk, so
every consumer (the MCP merge, the skill roots, the dashboard list) reads the
same verdict without re-validating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: ``invalid``: the plugin was rejected as a whole, so none of its components may
#: be used. ``not-a-plugin``: the directory has no ``plugin.json`` (a legacy
#: ``POWER.md`` power, for example). ``off``: a valid plugin the owner has not
#: turned on.
PluginState = Literal["invalid", "not-a-plugin", "off"]

#: Which part of a plugin an :class:`Issue` is about.
Component = Literal["plugin", "manifest", "skills", "mcp"]


@dataclass(frozen=True)
class Issue:
    """One problem the spec asks a client to report.

    ``entry`` names the skill directory or MCP server the issue is about, and is
    ``None`` when the issue covers a whole component. ``failed`` is true when the
    message reports a read that failed on this host (a permission error, say),
    and false when it reports a rule the package's files break: the dashboard
    shows the first as an error the agent can take over and the second as text.
    """

    component: Component
    message: str
    entry: str | None = None
    failed: bool = False


@dataclass(frozen=True)
class Manifest:
    """The portable fields of a valid ``plugin.json``."""

    schema: str
    name: str
    version: str | None = None
    description: str | None = None
    author: dict[str, str] | None = None
    homepage: str | None = None
    repository: str | None = None
    license: str | None = None
    keywords: tuple[str, ...] = ()
    #: Namespace -> raw value. Values are kept unvalidated: §8.1 forbids a client
    #: from validating a namespace it does not implement.
    extensions: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Skill:
    """A skill found under ``skills/`` whose ``SKILL.md`` passed validation."""

    name: str
    description: str
    #: Resolved path of the skill directory, inside the plugin root.
    path: str


@dataclass(frozen=True)
class StdioServer:
    """A valid ``stdio`` entry, as written: placeholders are NOT expanded here.

    Expansion needs the plugin's data directory and happens at launch, so the
    stored values stay exactly what the package declared.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    type: Literal["stdio"] = "stdio"


@dataclass(frozen=True)
class RemoteServer:
    """A valid ``streamable-http`` or ``sse`` entry."""

    name: str
    type: Literal["streamable-http", "sse"]
    url: str
    headers: dict[str, str] = field(default_factory=dict)


McpServer = StdioServer | RemoteServer


@dataclass(frozen=True)
class PluginRecord:
    """Everything discovery learned about one installed directory."""

    #: The name ``installed.json`` lists, which is also the directory name.
    name: str
    #: The directory as listed, before resolution.
    root: str
    state: PluginState
    #: Why the plugin is ``invalid`` or ``not-a-plugin``; ``None`` when ``off``.
    reason: str | None = None
    manifest: Manifest | None = None
    skills: tuple[Skill, ...] = ()
    servers: tuple[McpServer, ...] = ()
    issues: tuple[Issue, ...] = ()
    #: True when ``reason`` reports a failure to read or inspect the plugin, not
    #: a rule it breaks; the same split as :attr:`Issue.failed`.
    failed: bool = False


@dataclass(frozen=True)
class Discovery:
    """One pass over the powers home."""

    plugins: tuple[PluginRecord, ...] = ()
    #: Why ``installed.json`` could not be read, or ``None``. A failed read lists
    #: nothing, as a missing index does, so this is what tells the two apart.
    index_error: str | None = None
