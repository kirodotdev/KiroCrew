"""Validate ``plugin.json`` (Agent Plugins 1.0.0, §5).

The manifest schema is closed. An unknown top-level field and a non-object
``extensions`` are reported and ignored (§5.2, §8.1); every other violation
rejects the whole plugin. The schema is never fetched: only identifiers this
module lists are recognised.
"""

from __future__ import annotations

import os
import string
from typing import Any

from kiro_crew.agent_plugins.model import Issue, Manifest
from kiro_crew.agent_plugins.paths import resolves_within
from kiro_crew.user_json import loads_user_json

MANIFEST_FILE = "plugin.json"
SCHEMA_PREFIX = "https://agent-plugins.org/schemas/"
#: Supported Agent Plugins version -> its canonical manifest schema identifier.
MANIFEST_SCHEMAS = {"1.0.0": f"{SCHEMA_PREFIX}1.0.0/plugin.schema.json"}
#: Manifest schema identifier -> the matching ``mcp.json`` schema identifier.
MCP_SCHEMA_FOR = {
    MANIFEST_SCHEMAS["1.0.0"]: f"{SCHEMA_PREFIX}1.0.0/mcp.schema.json",
}

#: A manifest larger than this is refused unread; real manifests are a few KB.
MAX_MANIFEST_BYTES = 256 * 1024

_ALLOWED_FIELDS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)
_STRING_FIELDS = ("version", "description", "homepage", "repository", "license")
_AUTHOR_FIELDS = frozenset({"name", "email", "url"})
# §5.5: 1-64 lowercase alphanumerics, hyphens and periods; alphanumeric at both ends.
_NAME_ENDS = frozenset(string.ascii_lowercase + string.digits)
_NAME_CHARS = _NAME_ENDS | frozenset(".-")
_MAX_NAME_CHARS = 64


class ManifestError(Exception):
    """A fatal manifest violation; the message says which rule failed."""


class ManifestReadError(ManifestError):
    """``plugin.json`` could not be read on this host; no rule was broken."""


def is_valid_plugin_name(name: object) -> bool:
    """§5.5 name rule."""
    if not isinstance(name, str) or not 0 < len(name) <= _MAX_NAME_CHARS:
        return False
    if name[0] not in _NAME_ENDS or name[-1] not in _NAME_ENDS:
        return False
    if not all(char in _NAME_CHARS for char in name):
        return False
    return "--" not in name and ".." not in name


def load_manifest(root_real: str) -> tuple[Manifest, list[Issue]]:
    """Read and validate ``plugin.json`` under the resolved plugin root.

    Returns the manifest and its non-fatal issues. Raises :class:`ManifestError`
    for anything that rejects the plugin, and :class:`ManifestReadError` when the
    file could not be read. Bytes that are not UTF-8 are a rule the file breaks,
    like bad JSON: they are in the package, whichever host reads it.
    """
    path = os.path.join(root_real, MANIFEST_FILE)
    resolved = resolves_within(path, root_real)
    if resolved is None:
        raise ManifestError("plugin.json points outside the plugin folder")
    if not os.path.isfile(resolved):
        raise ManifestError("plugin.json is not a regular file")
    try:
        if os.path.getsize(resolved) > MAX_MANIFEST_BYTES:
            raise ManifestError("plugin.json is too large")
        with open(resolved, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ManifestReadError(f"plugin.json could not be read: {exc.__class__.__name__}") from exc
    except UnicodeDecodeError as exc:
        raise ManifestError("plugin.json is not UTF-8 text") from exc
    try:
        data = loads_user_json(text)
    except (ValueError, RecursionError) as exc:
        # RecursionError: nesting deeper than the parser's stack, well under the size cap.
        raise ManifestError("plugin.json is not valid JSON") from exc
    return parse_manifest(data)


def parse_manifest(data: Any) -> tuple[Manifest, list[Issue]]:
    """Validate an already-parsed manifest object."""
    if not isinstance(data, dict):
        raise ManifestError("plugin.json must hold a JSON object")
    issues: list[Issue] = []

    schema = data.get("$schema")
    if not isinstance(schema, str) or not schema:
        raise ManifestError("plugin.json is missing $schema")
    if schema not in MCP_SCHEMA_FOR:
        if schema.startswith(SCHEMA_PREFIX):
            raise ManifestError(f"unsupported Agent Plugins version: {schema}")
        raise ManifestError(f"$schema is not an Agent Plugins schema: {schema}")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError("plugin.json is missing name")
    if not is_valid_plugin_name(name):
        raise ManifestError(
            "name must be 1-64 lowercase letters, digits, hyphens or periods, "
            "start and end with a letter or digit, and not repeat - or ."
        )

    for key in sorted(set(data) - _ALLOWED_FIELDS):
        issues.append(Issue("manifest", f"unknown field ignored: {key}"))

    values: dict[str, Any] = {}
    for key in _STRING_FIELDS:
        if key in data:
            if not isinstance(data[key], str):
                raise ManifestError(f"{key} must be a string")
            values[key] = data[key]

    author = data.get("author")
    if "author" in data:
        if not isinstance(author, dict):
            raise ManifestError("author must be an object")
        extra = set(author) - _AUTHOR_FIELDS
        if extra:
            raise ManifestError(f"author has an unknown field: {sorted(extra)[0]}")
        if not all(isinstance(v, str) for v in author.values()):
            raise ManifestError("author fields must be strings")
        values["author"] = dict(author)

    keywords: tuple[str, ...] = ()
    if "keywords" in data:
        raw = data["keywords"]
        if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
            raise ManifestError("keywords must be a list of strings")
        keywords = tuple(raw)

    extensions: dict[str, object] = {}
    if "extensions" in data:
        raw_ext = data["extensions"]
        if isinstance(raw_ext, dict):
            extensions = dict(raw_ext)
        else:
            issues.append(Issue("manifest", "extensions is not an object, ignored"))

    return (
        Manifest(schema=schema, name=name, keywords=keywords, extensions=extensions, **values),
        issues,
    )
