"""One agent definition file as the parts of a ``.kirobot`` bundle.

An agent definition file is Markdown: a front matter block, then the prompt
(the markdown agent spec form :mod:`kiro_crew.agent_spec_format` reads). With
``dashboard.import_agent_files`` on, the Bot import card takes one in place of a
``.kirobot`` zip (:func:`kiro_crew.crewmate_bundle.read_crewmate_file`).

The mapping is fixed: ``name``, ``description`` and ``model`` become the
manifest's name, description and model hint (a hint the card shows, never
applies), and the body becomes the Bot's rules. Nothing else is carried: a
``tools`` list, hooks, MCP servers and any other field are dropped, so
``agent.json`` is empty and a card raised from the file has no grant. The parts
then go through :func:`kiro_crew.crewmate_bundle.crewmate_import_params` like a
zip's, so every rule of the card runs on them, and the owner's press on the
card is still the only thing that creates the Bot.
"""

from __future__ import annotations

import hashlib
from typing import Any

from kiro_crew import agent_spec_format
from kiro_crew.crewmate_bundle import CREWMATE_FORMAT, JSON_MAX_BYTES, CrewmateFile, _refuse

#: Where the card says the Bot came from.
SENDER_LABEL = "an agent file"


def opens_agent_file(data: bytes) -> bool:
    """Whether *data* opens like an agent definition file: a front matter fence.

    Only the first bytes are read; a byte that is not UTF-8 there fails the test.
    """
    head = bytes(data[:8]).decode("utf-8", errors="replace")
    return agent_spec_format.opens_frontmatter_fence(head)


def read_agent_file(data: bytes) -> CrewmateFile:
    """The bundle parts of one agent definition file. Raises ``CardCatalogError``."""
    if len(data) > JSON_MAX_BYTES:
        raise _refuse("too_large", "an agent file is over 64 KiB")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        raise _refuse("invalid_text", "the agent file is not UTF-8 text") from None
    try:
        spec = agent_spec_format.parse_markdown_spec(text)
    except ValueError:
        raise _refuse("invalid_bundle", "the agent file's front matter cannot be read") from None
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise _refuse("invalid_bundle", "the agent file has no name")
    manifest: dict[str, Any] = {
        "format": CREWMATE_FORMAT,
        "formatVersion": 1,
        "name": name,
        "sender": {"label": SENDER_LABEL},
    }
    for key, field in (("description", "description"), ("model", "modelHint")):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            manifest[field] = value.strip()
    bundle: dict[str, Any] = {"crewmate": manifest, "agent": {}}
    # The body only: a front matter ``prompt`` (a file:// pointer) is not rules.
    parts = agent_spec_format.split_markdown_spec(text)
    body = parts[1].lstrip("\r\n") if parts else ""
    if body.strip():
        # A rules field holds no CR; a Windows editor's line ends are not content.
        bundle["rules"] = {"rules": body.replace("\r\n", "\n")}
    return CrewmateFile(bundle=bundle, sha256=hashlib.sha256(bytes(data)).hexdigest())
