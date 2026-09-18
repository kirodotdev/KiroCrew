"""The two on-disk forms of an agent spec, and the one parser for both.

kiro-cli reads an agent from ``~/.kiro/agents/<name>.json``. The v3 engine (KAS,
and Kiro IDE) also reads ``<name>.md``: YAML frontmatter carrying the same fields
as the JSON object, with the markdown body as the agent's system prompt. Both are
the same spec in a different serialization, so every scan of an agents directory
goes through this module rather than a bare ``glob("*.json")`` -- a scan that
sees only one form lists an agent kiro-cli would run, or projects onto KAS an
agent that is not there.

This is a leaf module on purpose: it imports nothing from ``kiro_crew``, so the
config loader, the MCP gateway rewriter and the discovery cache can all reach it
without an import cycle. It parses bytes it is handed and never opens a file --
the hardened read (size cap, sensitive-symlink refusal) stays with the caller.

``<name>.json`` beside ``<name>.md`` is one agent authored twice -- the JSON twin
is the workaround users kept while only JSON was read -- and the JSON wins:
the iterator drops the shadowed markdown file, so every consumer sees one
agent, and the roster warns so the author knows which file is live. Two
files of the SAME form declaring one name stay an ambiguity for the callers
that already refuse it.

A markdown spec is one file, so the body IS the prompt. When the body has
content it is the prompt even if the frontmatter also declares ``prompt``; a
frontmatter ``prompt`` is honoured only for a body-less file, so a spec that
points at a ``file://`` prompt still resolves. The frontmatter must be a
mapping; ``---`` opens it at byte 0 (after an optional UTF-8 BOM, which a
Windows editor adds and which would otherwise turn the whole document into
prose) and a line that is exactly ``---`` closes it, so a ``---junk`` line is
body text and configuration never leaks into the prompt.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

#: Suffixes an agents directory entry may carry, lower-case. ``.json`` is the
#: kiro-cli form; ``.md`` is the markdown form.
JSON_SUFFIX = ".json"
MARKDOWN_SUFFIX = ".md"
AGENT_SPEC_SUFFIXES: tuple[str, ...] = (JSON_SUFFIX, MARKDOWN_SUFFIX)

_FRONTMATTER_CLOSE_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


class _FrontmatterLoader(yaml.SafeLoader):  # type: ignore[misc]
    """``SafeLoader`` that yields a JSON-shaped TREE and nothing else.

    The parsed frontmatter is re-serialized as JSON on every consuming path
    (the KAS projection, the dashboard roster, the rewriter's overlay), so a
    document JSON cannot carry is not a quirk but a crash there. Two features of
    YAML have no JSON form and are removed at the loader rather than detected
    afterwards:

    * **Aliases** (``*name``) are refused at compose time; an ``&name`` anchor
      nothing references parses as its plain value, the same rule the other
      alias-refusing loaders in this package apply. An alias is a shared
      reference, and a shared reference is what makes a document a graph: a self-reference recurses forever, and a chain of
      shared containers expands exponentially when it is serialized -- which
      every consumer does. Without aliases the loaded value is a tree whose
      size is bounded by the file's, so the validation below is one linear
      walk with no identity tracking. An agent spec has no use for an alias
      that a repeated literal does not serve.
    * **Timestamps**: PyYAML's 1.1 resolver turns an unquoted ISO date
      (``args: [YYYY-MM-DD]``) into ``datetime.date``; kiro-cli's own YAML
      reader has no date type and reads the same scalar as the string the
      author typed, so the timestamp constructor is replaced to do the same.

    Anything else that is not JSON (an explicit ``!!binary`` / ``!!set``, a
    non-string key, an infinity) is rejected by :func:`_require_json_shape`
    after loading rather than mapped, since there is no faithful mapping.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.get_event()
            raise yaml.composer.ComposerError(
                None,
                None,
                f"aliases (*{event.anchor}) are not supported in agent frontmatter; "
                "repeat the value instead",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def _construct_timestamp_as_text(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return loader.construct_scalar(node)  # type: ignore[arg-type]


_FrontmatterLoader.add_constructor("tag:yaml.org,2002:timestamp", _construct_timestamp_as_text)


def _require_json_shape(value: Any, where: str) -> None:
    """Raise ``ValueError`` unless *value* is a finite JSON document.

    JSON values only: ``dict`` with ``str`` keys, ``list``, ``str``, ``bool``,
    ``int``, finite ``float``, ``None``. The loader admits no aliases, so *value*
    is a tree and one pass over it is linear in the file size; a depth the
    interpreter cannot recurse is caught by the caller. *where* names the
    offending key path in the error, since the author has to find it in a file
    whose other fields all parsed.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"frontmatter value at {where} is not a finite number")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"frontmatter key {key!r} at {where} is not a string; quote it")
            _require_json_shape(item, f"{where}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_shape(item, f"{where}[{index}]")
        return
    raise ValueError(
        f"frontmatter value at {where} is a YAML {type(value).__name__}, which has no JSON form"
    )


def spec_suffix(name: str | Path) -> str | None:
    """The spec suffix of *name* (``.json`` / ``.md``), or ``None`` for neither.

    Case-insensitive: a case-insensitive filesystem serves ``Foo.JSON`` to a
    ``glob("*.json")`` consumer, so the check here must match what a scan sees.
    """
    # Anything path-like with a ``.name`` is accepted, not only ``Path``: the
    # hardened reader is handed duck-typed paths on some platforms' probe gates.
    lowered = (name if isinstance(name, str) else name.name).lower()
    for suffix in AGENT_SPEC_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return None


def is_agent_spec_name(name: str) -> bool:
    """Whether a directory entry name is an agent spec of either form."""
    return spec_suffix(name) is not None


def is_markdown_spec(path: str | Path) -> bool:
    """Whether *path* is the markdown form."""
    return spec_suffix(path) == MARKDOWN_SUFFIX


def spec_stem(name: str) -> str:
    """The filename with its spec suffix removed; *name* unchanged otherwise."""
    suffix = spec_suffix(name)
    return name[: -len(suffix)] if suffix else name


def _has_json_twin(directory: Path, md: Path, json_stems: set[str]) -> bool:
    """Whether ``<md.stem>.json`` names an existing entry beside *md*.

    The stem set answers the exact-case twin. The filesystem answers the rest:
    on a case-insensitive filesystem (Windows, macOS by default) ``Foo.json``
    and ``foo.md`` are twins too -- their ``<stem>.json`` overlays would be ONE
    file there, so the second overlay written would hand one agent the other's
    MCP servers -- and the only authority on which names collide is the
    filesystem itself, asked for the name this file's overlay would take. On a
    case-sensitive filesystem the probe misses and the two stay distinct
    agents, which is what their distinct overlay files are.
    """
    if md.stem in json_stems:
        return True
    return (directory / f"{md.stem}{JSON_SUFFIX}").exists()


def _split_spec_files(directory: Path) -> tuple[list[Path], list[Path]]:
    """``(live, shadowed)``: every spec file, with ``<stem>.md`` beside ``<stem>.json`` set aside."""
    json_files = list(directory.glob(f"*{JSON_SUFFIX}"))
    json_stems = {p.stem for p in json_files}
    live = list(json_files)
    shadowed: list[Path] = []
    for path in directory.glob(f"*{MARKDOWN_SUFFIX}"):
        (shadowed if _has_json_twin(directory, path, json_stems) else live).append(path)
    return live, shadowed


def iter_agent_spec_files(directory: Path, *, ordered: bool = True) -> list[Path]:
    """Every live spec file in *directory*, both forms.

    A ``<stem>.md`` whose ``<stem>.json`` twin exists is NOT returned: the JSON
    wins (see the module docstring), and :func:`shadowed_markdown_specs` names
    the files this dropped. Propagates ``OSError`` from the directory walk
    exactly as ``Path.glob`` does, so a caller that already handles the
    JSON-only glob's failure handles this one unchanged. *ordered* sorts by full
    name so the order is stable across platforms; ``ordered=False`` keeps the
    directory's native order, JSON entries first, for the first-match resolvers
    that scan on the event loop and stop at the first hit.
    """
    live, _shadowed = _split_spec_files(directory)
    return sorted(live) if ordered else live


def shadowed_markdown_specs(directory: Path) -> list[Path]:
    """The ``<stem>.md`` files a ``<stem>.json`` twin hides, sorted; ``[]`` on a walk error."""
    try:
        _live, shadowed = _split_spec_files(directory)
    except OSError:
        return []
    return sorted(shadowed)


def agent_spec_candidates(directory: Path, name: str) -> list[Path]:
    """The direct-filename paths ``<name>.json`` and ``<name>.md``, existing or not.

    JSON first, so a caller that takes the first existing candidate applies the
    JSON-wins rule for a twin without restating it.
    """
    return [directory / f"{name}{suffix}" for suffix in AGENT_SPEC_SUFFIXES]


def split_markdown_spec(text: str) -> tuple[str, str] | None:
    """Split a markdown spec into ``(frontmatter_yaml, body)``.

    ``None`` when the document does not open with a frontmatter fence -- a plain
    markdown file dropped into the agents directory is not a spec and must not
    be listed as one. A fence with no closing line is also ``None``: the whole
    file would otherwise parse as YAML and a prompt would be mistaken for config.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return None
    first_newline = text.index("\n")
    close = _FRONTMATTER_CLOSE_RE.search(text, first_newline + 1)
    if close is None:
        return None
    frontmatter = text[first_newline + 1 : close.start()]
    body = text[close.end() :]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return frontmatter, body


def _load_frontmatter(text: str) -> Any:
    """Parse ONE YAML document with :class:`_FrontmatterLoader`.

    Driving the loader instance is what ``yaml.load`` does with an explicit
    ``Loader=``, so the parse is identical -- but the SafeLoader subclass is the
    only construction path here, with no ``yaml.load`` call whose safety a
    reader (or a scanner keyed on the call name) has to infer from the
    ``Loader=`` argument. The repo's ``test_yaml_safe_loading`` guard pins that.
    """
    loader = _FrontmatterLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def parse_markdown_spec(text: str) -> dict[str, Any]:
    """Parse a markdown spec into the same dict shape a JSON spec loads to.

    Raises ``ValueError`` -- the same class ``json.loads`` raises for a bad
    JSON spec -- when the fence is missing or unclosed, the frontmatter is not
    valid YAML, is valid YAML that is not a mapping, or holds a value JSON
    cannot carry (see :class:`_FrontmatterLoader`). The safe loader only: the
    agents directory is user-writable and shared with other tools.
    """
    parts = split_markdown_spec(text)
    if parts is None:
        raise ValueError("markdown agent spec has no closed '---' frontmatter fence")
    frontmatter, body = parts
    try:
        loaded = _load_frontmatter(frontmatter) if frontmatter.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"markdown agent spec frontmatter is not valid YAML: {exc}") from exc
    except RecursionError as exc:
        # PyYAML composes and constructs nested collections recursively, so a
        # frontmatter nested hundreds of levels deep (well within the size cap)
        # exhausts the interpreter stack. That is bad content, not a crash the
        # caller should see: same class as any other unparseable frontmatter.
        raise ValueError("markdown agent spec frontmatter is nested too deeply") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("markdown agent spec frontmatter is not a mapping")
    try:
        _require_json_shape(loaded, "frontmatter")
    except RecursionError as exc:
        raise ValueError("markdown agent spec frontmatter is nested too deeply") from exc
    except ValueError as exc:
        raise ValueError(f"markdown agent spec {exc}") from exc
    spec: dict[str, Any] = dict(loaded)
    tools = spec.get("tools")
    if isinstance(tools, str) and tools.strip() != "*":
        # The v3 loader accepts ``tools: read, write`` as a comma-separated
        # string as well as a YAML list; the JSON shape is the list, so the
        # string is normalized here and every consumer sees one shape. An
        # empty string means no tools, which the JSON form spells by omission.
        entries = [t.strip() for t in tools.split(",") if t.strip()]
        if entries:
            spec["tools"] = entries
        else:
            spec.pop("tools")
    if body.strip():
        # Leading blank lines are the gap authors leave after the fence, not
        # prompt text; trailing whitespace is kept as written.
        spec["prompt"] = body.lstrip("\r\n")
    return spec


def parse_agent_spec_text(text: str, path: str | Path) -> Any:
    """Parse *text* as the spec form *path*'s suffix names.

    Returns whatever the document holds -- callers reject a non-dict exactly as
    they did for JSON -- and raises ``ValueError`` for either form's syntax
    errors, so one ``except ValueError`` covers both. A path with neither
    suffix is parsed as JSON, the historical behaviour of every caller.
    """
    if is_markdown_spec(path):
        return parse_markdown_spec(text)
    return json.loads(text)


def parse_agent_spec_bytes(raw: bytes, path: str | Path) -> Any:
    """:func:`parse_agent_spec_text` over UTF-8 bytes; ``UnicodeDecodeError`` propagates."""
    return parse_agent_spec_text(raw.decode("utf-8"), path)
