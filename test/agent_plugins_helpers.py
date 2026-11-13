"""Builders for Agent Plugins test fixtures on a tmp_path powers home."""

from __future__ import annotations

import json
import os
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.testing.links import make_dir_link

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def install(home: Path, name: str, manifest: dict | str | None = None, **files: str) -> Path:
    """Create ``installed/<name>/`` with *manifest* and *files*, and list it.

    *manifest* defaults to a minimal valid ``plugin.json`` named *name*. A dict
    is written as JSON, a non-empty string as raw text, and ``""`` writes no
    ``plugin.json`` at all. Each keyword in *files* is a path relative to the
    plugin root, with ``__`` standing for ``/``.
    """
    root = home / "installed" / name
    root.mkdir(parents=True, exist_ok=True)
    if manifest is None:
        manifest = {"$schema": PLUGIN_SCHEMA, "name": name}
    if manifest != "":
        text = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (root / "plugin.json").write_text(text, encoding="utf-8")
    for rel, content in files.items():
        path = root / rel.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    index = home / "installed.json"
    data = json.loads(index.read_text()) if index.exists() else {"installedPowers": []}
    if {"name": name} not in data["installedPowers"]:
        data["installedPowers"].append({"name": name})
    index.write_text(json.dumps(data), encoding="utf-8")
    return root


def mcp(servers: dict, schema: str = MCP_SCHEMA) -> str:
    return json.dumps({"$schema": schema, "mcpServers": servers})


def skill(name: str, description: str = "Does a thing.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\nBody.\n"


def symlink(link: Path, target: str | Path, *, directory: bool = False) -> None:
    """Make a link at *link* to *target*, read relative to *link*'s folder.

    A directory link goes through ``make_dir_link``: a junction on Windows, which
    needs no privilege, so the containment tests run there too. A junction holds
    an absolute path, so a relative *target* is resolved for it first. A file
    link is a real symlink and never skips here: a test that makes one is listed
    in ``test/requires-real-symlinks.txt``, and the root conftest skips it only
    on a host whose probe cannot make a symlink.
    """
    if directory:
        dest = Path(target)
        if platform_compat.IS_WINDOWS:
            dest = (link.parent / dest).resolve()
        make_dir_link(link, dest)
        return
    os.symlink(target, link)
