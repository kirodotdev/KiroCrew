"""Which skills a KAS session may load: Crew's skill scope as ``skill`` deny rules.

kiro-cli (v2) and KAS (v3) build a session's skill catalog differently. On kiro-cli
Crew's native launch turns ``chat.disableInheritingDefaultResources`` on, so the
session loads only the skills its spec maps (or the inherited trees, served by Crew's
``skill_search``). KAS reads no
``cli.json``: it loads every skill under ``~/.kiro/skills`` and the workspace's
``.kiro/skills`` on top of the injected agent's ``resources`` (measured on kiro-cli
2.35.1: a spec mapping 3 skills ran with 45 on KAS and with 3 through Crew's v2 path).

The ``customAgents`` wire has no field that turns that off, but its ``permissions``
does reach the catalog: KAS drops every skill the policy denies under the ``skill``
capability, keyed on the skill's name, from the list its skill tool offers. So the
v2 scope is re-expressed as one deny rule: ``deny skill`` with the in-scope names as
``exclude``. Measured on the same spec, the rule took the session from 45 skills to
the spec's 3. A deny only narrows, so it needs no pass through the governance ceiling.

KAS ANDs ``exclude`` patterns onto every statement it compiles and does not split
them; its compiler records that a condition tree past roughly 114 patterns overflows
cedar-wasm's stack, and Crew bounds every rule it sends at
``kas_permissions._MAX_PATTERNS_PER_RULE`` (64) for that reason (measured live here
up to 35). Past that bound the rule is written the other way round: one ``deny`` per
out-of-scope name found in the trees KAS loads by default, which splits like any
``match`` list. That form cannot see a skill added to those trees after session
start, and a name with glob characters cannot be denied literally, so such a skill
stays offered.

The scope is read from the spec the session is projected with (``skill://``
entries, else the inherited trees for a spec that may inherit them) -- the rules
``agent_discovery.agent_skill_globs`` applies on kiro-cli -- plus every default-tree
skill the spec's own ``permissions`` allow by name: KAS loads those trees anyway,
and denying a skill the agent's author explicitly allows would override the
author. Skill files are read through ``hooks.safe_read_prefix``; a skill whose
opened file ``kas_prompt_paths.is_unsafe_prompt_path`` refuses is never copied.

KAS also refuses a SKILL.md kiro-cli loads (:func:`refused_skill_copies`): one with no
front matter, no ``name`` or ``description``, or front matter its YAML parser rejects,
and, in its default trees, a name outside ``[a-z0-9-]`` or a description over 1024
characters. kiro-cli falls back to the path and "No description available"; KAS drops
the skill without a word. For each in-scope skill KAS would drop, Crew writes a fixed
copy under the data home and adds it to the agent's ``resources`` as ``skill://``.

See ``docs/system-specs/modules/memory-skills-hooks.md`` (KAS skill scope).
"""

from __future__ import annotations

import functools
import glob
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew.acp.kas_permissions import _MAX_PATTERN_LENGTH, _MAX_PATTERNS_PER_RULE

logger = logging.getLogger(__name__)

#: KAS's capability for a skill (``CAPABILITY_BY_TOOL["disclose_context"]``).
SKILL_CAPABILITY = "skill"

#: How many skill files one scope resolution reads, at most.
_MAX_SKILL_FILES = 2048

#: How much of a skill file is read for its frontmatter.
_FRONTMATTER_BYTES = 64 * 1024

#: Characters KAS's matcher reads as a pattern; a name carrying one cannot be an
#: exact ``exclude`` entry, so it is left out (left denied) rather than widened.
_PATTERN_CHARS = frozenset("*?[]{}")


def _skill_name(path: Path) -> str | None:
    """The skill's frontmatter ``name``, else its directory's name. KAS has no such
    fallback and drops a skill without a name; :func:`refused_skill_copies` gives
    that skill a copy KAS loads."""
    from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
    from kiro_crew.hooks import safe_read_prefix

    # A ``skill://`` path comes from a spec, which may point anywhere: the read
    # goes through the sensitive-path fence and refuses a credential store.
    raw = safe_read_prefix(str(path), _FRONTMATTER_BYTES)
    if raw is None:
        return None
    # ``utf-8-sig``, as every SKILL.md reader decodes: a BOM would hide the fence.
    head = raw.decode("utf-8-sig", errors="replace")
    name = (parse_frontmatter(head, SKILL_LOADER).get("name") or "").strip()
    if not name:
        name = path.parent.name if path.name == "SKILL.md" else path.stem
    return name or None


def _literal(name: str) -> bool:
    """Whether *name* can be a policy pattern that matches only itself."""
    return len(name) <= _MAX_PATTERN_LENGTH and not (_PATTERN_CHARS & set(name))


def skill_names(globs: list[str]) -> set[str]:
    """The names of the skill files *globs* match, bounded by :data:`_MAX_SKILL_FILES`."""
    names: set[str] = set()
    seen = 0
    for pattern in globs:
        for match in sorted(glob.glob(pattern, recursive=True)):
            path = Path(match)
            if not path.is_file():
                continue
            seen += 1
            if seen > _MAX_SKILL_FILES:
                logger.warning(
                    "KAS skill scope: more than %d skill files matched; the rest are left out",
                    _MAX_SKILL_FILES,
                )
                return names
            name = _skill_name(path)
            if name:
                names.add(name)
    return names


def _default_tree_globs(work_dir: str | Path | None) -> list[str]:
    """The skill trees KAS loads for every session, whatever the agent maps."""
    from kiro_crew.config import paths
    from kiro_crew.skills import project_skill_tree_present

    globs = [str(paths.kiro_home() / "skills" / "*" / "SKILL.md")]
    if work_dir and project_skill_tree_present(work_dir):
        globs.append(str(Path(work_dir) / ".kiro" / "skills" / "*" / "SKILL.md"))
    return globs


def _spec_globs(
    agent: str, spec: dict[str, Any], spec_path: Path, work_dir: Any
) -> list[str] | None:
    """The skill globs *spec* resolves to under ``agent_skill_globs``' rules, or
    ``None`` for an unmapped whole-catalog template.

    Its ``skill://`` entries, else the inherited trees for a spec that may inherit
    them (kiro-cli's rule, so inherited trees count); for the crewmate's conductor,
    its entries plus those trees.
    """
    from kiro_crew.agent_discovery import (
        WHOLE_CATALOG_TEMPLATES,
        can_inherit_skills,
        expand_skill_uri,
        inherited_skill_globs,
        mate_conductor_scope,
        skill_resource_uris,
    )

    globs = [
        g
        for uri in skill_resource_uris(spec)
        if (g := expand_skill_uri(uri, spec_path, project_dir=work_dir))
    ]
    if globs:
        # The crewmate conductor's mapping adds to the inherited trees, by the one
        # rule ``agent_skill_globs`` returns through.
        return mate_conductor_scope(agent, spec, globs, work_dir)
    if agent in WHOLE_CATALOG_TEMPLATES:
        return None
    return inherited_skill_globs(work_dir) if can_inherit_skills(spec) else []


def scope_globs(
    agent: str,
    spec: dict[str, Any],
    *,
    agents_dir: Path,
    work_dir: str | Path | None,
) -> list[str] | None:
    """The skill globs this session loads on kiro-cli, or ``None`` when KAS needs no rule.

    ``None`` is the whole catalog (an unmapped template in ``WHOLE_CATALOG_TEMPLATES``:
    ``kirocrew``, ``kirocrew-worker`` and the crewmate's conductor and worker)
    or a scope that is exactly the default trees KAS loads by itself.

    The scope is read from *spec*, the spec this session is projected with, and
    from nothing else. Never re-resolved by name: a checkout spec that shadows a
    user-level name is not the one KAS runs (``harness/kas.resolve_projected_spec``).
    Never from the session key's skill-scope pin (``skill_scope_pin``) either: the
    gateway sets that pin per turn, after the session is built, so a session
    started for a turn that switched away from an app agent would still read the
    app's file. An app-bound session's projected spec IS its registered file
    (``apps.agent_launch.read_bound_app_spec``), and a crewmate worker's is already
    its merged session spec.
    """
    globs = _spec_globs(agent, spec, agents_dir / f"{agent}.json", work_dir)
    if globs is None or _inherits_default_trees(agent, spec, globs, work_dir):
        return None
    return [*globs, *_allowed_default_tree_skills(spec, work_dir)]


def _authored_skill_allows(spec: dict[str, Any]) -> set[str]:
    """The skill names *spec*'s own ``permissions`` allow by name.

    Only a plain ``{capability: skill, effect: allow, match: [...]}`` rule counts,
    and only its literal entries: a rule with any other key (a condition, an
    ``exclude``) or a pattern entry does not say which skills it means.
    """
    block = spec.get("permissions")
    rules = block.get("rules") if isinstance(block, dict) else None
    names: set[str] = set()
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or set(rule) - {"capability", "effect", "match"}:
            continue
        if rule.get("capability") != SKILL_CAPABILITY or rule.get("effect") != "allow":
            continue
        match = rule.get("match")
        if isinstance(match, list):
            names.update(m for m in match if isinstance(m, str) and _literal(m))
    return names


def _allowed_default_tree_skills(spec: dict[str, Any], work_dir: str | Path | None) -> list[str]:
    """The default-tree skill files whose name *spec*'s own ``permissions`` allow.

    KAS loads the default trees for every session, and the deny rule would refuse
    a skill there that the agent's author allowed by name; the author's allow is
    the narrower statement of intent, so such a skill stays in scope. Each path is
    escaped, since :func:`skill_names` reads the list as globs.
    """
    allowed = _authored_skill_allows(spec)
    if not allowed:
        return []
    paths: list[str] = []
    seen = 0
    for pattern in _default_tree_globs(work_dir):
        for match in sorted(glob.glob(pattern)):
            path = Path(match)
            if not path.is_file():
                continue
            seen += 1
            if seen > _MAX_SKILL_FILES:
                return paths
            if _skill_name(path) in allowed:
                paths.append(glob.escape(match))
    return paths


def _inherits_default_trees(
    agent: str, spec: dict[str, Any], globs: list[str], work_dir: str | Path | None
) -> bool:
    """Whether the scope IS the default trees, which KAS loads on its own.

    Asked on the trees, not on the glob list alone: with neither tree on disk an
    inheriting agent resolves to ``[]``, the same answer as an agent that may not
    inherit, and only the second is held to no skills.
    """
    from kiro_crew.agent_discovery import (
        WHOLE_CATALOG_TEMPLATES,
        can_inherit_skills,
        inherited_skill_globs,
        skill_resource_uris,
    )
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    if globs:
        return sorted(globs) == sorted(inherited_skill_globs(work_dir))
    return (
        agent not in WHOLE_CATALOG_TEMPLATES
        and not skill_resource_uris(spec)
        and can_inherit_skills(spec)
        and acp_driver.inherits_default_resources(work_dir)
    )


def skill_scope_rules(
    globs: list[str] | None,
    *,
    work_dir: str | Path | None = None,
    also_allowed: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """The ``permissions`` rules that hold a KAS session to the skills *globs* match.

    ``None`` (:func:`scope_globs`: a whole-catalog template, or a scope that is the
    default trees KAS already loads) adds nothing. An empty scope denies every skill.
    *also_allowed* are names in scope that *globs* do not show: the copies
    :func:`refused_skill_copies` made.
    """
    if globs is None:
        return []
    names = sorted(n for n in skill_names(globs) | also_allowed if _literal(n))
    if not names:
        return [{"capability": SKILL_CAPABILITY, "effect": "deny"}]
    if len(names) <= _MAX_PATTERNS_PER_RULE:
        return [{"capability": SKILL_CAPABILITY, "exclude": names, "effect": "deny"}]
    in_scope = set(names)
    # Only literal names become ``match`` entries: KAS reads them as globs, so a
    # name like ``*`` would deny the in-scope skills too. Such a skill stays offered.
    outside = sorted(
        n for n in skill_names(_default_tree_globs(work_dir)) if n not in in_scope and _literal(n)
    )
    logger.info(
        "KAS skill scope maps %d skills, over the %d an exclude list may hold; "
        "denying the %d default-tree skills outside it by name instead",
        len(names),
        _MAX_PATTERNS_PER_RULE,
        len(outside),
    )
    step = _MAX_PATTERNS_PER_RULE
    return [
        {"capability": SKILL_CAPABILITY, "match": outside[i : i + step], "effect": "deny"}
        for i in range(0, len(outside), step)
    ]


#: How many skill names one skill-scope record repeats before it summarises.
_SKILL_SCOPE_AUDIT_NAMES = 40


def _skill_scope_summary(rules: tuple[dict[str, Any], ...]) -> str:
    """What the skill-scope denies keep and take away, in a bounded line."""
    kept: list[str] = []
    denied: list[str] = []
    deny_all = False
    for rule in rules:
        if rule.get("exclude"):
            kept.extend(rule["exclude"])
        elif rule.get("match"):
            denied.extend(rule["match"])
        else:
            deny_all = True

    def _names(names: list[str]) -> str:
        shown = ", ".join(names[:_SKILL_SCOPE_AUDIT_NAMES])
        more = len(names) - _SKILL_SCOPE_AUDIT_NAMES
        return f"{shown}, and {more} more" if more > 0 else shown

    if kept:
        return f"every skill denied except {len(kept)}: {_names(kept)}"
    if denied:
        return f"{len(denied)} default-tree skills denied by name: {_names(denied)}"
    return "every skill denied" if deny_all else "no skill rule"


# --- Skills KAS refuses (P0-26) -------------------------------------------------------

#: The characters KAS's ``isValidSkillName`` allows in a name, besides ``-``.
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_MAX_NAME_CHARS = 64
#: KAS's ``isValidSkillDescription`` bound, in UTF-16 units (JavaScript's length).
_MAX_DESCRIPTION_UNITS = 1024
#: kiro-cli's text for a skill with no description (``format_skill_hint``).
_NO_DESCRIPTION = "No description available"
#: The largest skill file Crew copies; KAS reads skill files whole.
# shortcut: a SKILL.md over 1 MiB gets no copy and stays dropped; raise if one is seen.
_MAX_COPY_BYTES = 1024 * 1024
#: Where the copies live, under the data home (``atomic_write`` makes it 0700).
COPY_DIR_NAME = "kas-skills"


class SkillCopy(NamedTuple):
    """A copy of a skill KAS would drop: the name KAS gives it and its ``skill://`` URI."""

    name: str
    uri: str


@functools.cache
def _kas_yaml_loader() -> type:
    """A YAML loader closer to js-yaml's: it refuses a duplicate key, and reads
    ``1e3`` as a number (YAML 1.2), as KAS does."""
    import yaml  # type: ignore[import-untyped]

    from kiro_crew.yaml_safe import NoAliasSafeLoader

    class _Loader(NoAliasSafeLoader):  # type: ignore[misc]
        def construct_mapping(self, node: Any, deep: bool = False) -> Any:
            seen: set[Any] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, "duplicated mapping key", key_node.start_mark
                    )
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    # PyYAML asks a resolver's pattern only for ``.match(value)``.
    _Loader.add_implicit_resolver("tag:yaml.org,2002:float", _Yaml12Float(), list("-+0123456789."))
    return _Loader


def _digits(text: str, i: int) -> int:
    """The index after the ASCII digits that start at *i* in *text*."""
    while i < len(text) and "0" <= text[i] <= "9":
        i += 1
    return i


class _Yaml12Float:
    """YAML 1.2's plain float form, which PyYAML's 1.1 resolver misses (``1e3``):
    an optional sign, then ``.5``, ``5`` or ``5.`` / ``5.5``, then an optional
    exponent ``e`` / ``E``, optional sign, digits."""

    def match(self, value: str) -> bool:
        i = 1 if value[:1] in ("+", "-") else 0
        if value[i : i + 1] == ".":
            end = _digits(value, i + 1)
            if end == i + 1:
                return False
        else:
            end = _digits(value, i)
            if end == i:
                return False
            if value[end : end + 1] == ".":
                end = _digits(value, end + 1)
        if value[end : end + 1] in ("e", "E"):
            exp = end + 1 + (1 if value[end + 1 : end + 2] in ("+", "-") else 0)
            end = _digits(value, exp)
            if end == exp:
                return False
        return end == len(value)


def _only_comments(matter: str) -> bool:
    """KAS's ``isEmpty``: every line blank or a ``#`` comment with text after it."""
    for line in matter.split("\n"):
        stripped = line.lstrip()
        if stripped and not (stripped.startswith("#") and len(stripped) > 1):
            return False
    return True


def _load_one_yaml(matter: str) -> Any:
    """The one YAML document in *matter*; ``ValueError`` when it holds more."""
    loader = _kas_yaml_loader()(matter)
    try:
        data = loader.get_data() if loader.check_data() else None
        if loader.check_data():
            raise ValueError("expected a single document in the stream")
        return data
    finally:
        loader.dispose()


def _kas_front_matter(text: str) -> tuple[dict[str, Any] | None, str]:
    """KAS's reading of *text*: ``(front matter, body)``, the front matter ``None``
    when KAS finds none or refuses it.

    Follows ``utils/front-matter`` (``extractFrontMatter`` and ``parseFrontMatter``
    with ``SkillFrontMatterSchema``): an opening ``---`` not followed by a fourth
    ``-``, an optional language, a closing ``\\n---``; one YAML (or JSON) document
    that is a non-empty mapping whose ``name``, ``description``, ``license`` and
    ``compatibility``, when set, are strings and whose ``metadata`` is a mapping.
    The body is the text after the closing fence, or the whole text with none.
    """
    import yaml  # type: ignore[import-untyped]

    text = text.removeprefix("\ufeff")
    if not text.startswith("---") or text[3:4] == "-":
        return None, text
    rest = text[3:]
    newline = rest.find("\n")
    first = rest if newline == -1 else rest[:newline].removesuffix("\r")
    language = first.strip().lower()
    if language:
        rest = rest[len(first) :]
    close = rest.find("\n---")
    matter = rest if close == -1 else rest[:close]
    # No closing fence: KAS reads the whole text as front matter. The body is kept
    # whole for a copy, so a leading Markdown rule cannot erase the skill.
    body = text if close == -1 else rest[close + 4 :].removeprefix("\r").removeprefix("\n")
    if _only_comments(matter):
        return None, body
    try:
        if language == "json":
            data = json.loads(matter)
        elif language in ("", "yaml", "yml"):
            # shortcut: PyYAML reads YAML 1.1 (``yes`` is a bool) where js-yaml's
            # CORE schema reads 1.2, and aliases are refused here; such a skill gets
            # a copy KAS did not need. Swap in a 1.2 reader if one is reported.
            data = _load_one_yaml(matter)
        else:
            return None, body
    except (ValueError, TypeError, RecursionError, yaml.YAMLError):
        return None, body
    if not isinstance(data, dict) or not data:
        return None, body
    if any(
        k in data and not isinstance(data[k], str)
        for k in ("name", "description", "license", "compatibility")
    ) or ("metadata" in data and not isinstance(data["metadata"], dict)):
        return None, body
    return data, body


def _utf16_len(text: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def _cut_utf16(text: str, units: int) -> str:
    used = 0
    for i, c in enumerate(text):
        used += 2 if ord(c) > 0xFFFF else 1
        if used > units:
            return text[:i]
    return text


def _kas_loads(front: dict[str, Any] | None, *, native: bool) -> bool:
    """Whether KAS loads a skill with *front* matter: as a ``skill://`` resource
    (``resource-resolver.readSkill``) or, *native*, from a default tree
    (``validateSkillMd``, which also checks the name and description)."""
    if front is None or not front.get("name") or not front.get("description"):
        return False
    if not native:
        return True
    name, description = front["name"], front["description"]
    return _valid_kas_name(name) and _utf16_len(description) <= _MAX_DESCRIPTION_UNITS


def _valid_kas_name(name: str) -> bool:
    """KAS's ``isValidSkillName``: 1-64 of ``a-z0-9-``, no edge or double hyphen."""
    return (
        0 < len(name) <= _MAX_NAME_CHARS
        and all(c in _NAME_CHARS or c == "-" for c in name)
        and not name.startswith("-")
        and not name.endswith("-")
        and "--" not in name
    )


def _kas_name(raw: str) -> str:
    """*raw* as a name KAS accepts, or ``""``: each run of other characters is one ``-``."""
    out: list[str] = []
    for c in raw.lower():
        if c in _NAME_CHARS:
            out.append(c)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")[:_MAX_NAME_CHARS].rstrip("-")


def _yaml_quoted(text: str) -> str:
    """*text* as a YAML double-quoted string, which every YAML reader reads as a
    string (a bare ``1e3`` is a number to KAS). JSON's escapes are YAML's; the
    characters YAML may not hold raw are escaped too."""
    out: list[str] = []
    for c in json.dumps(text, ensure_ascii=False):
        code = ord(c)
        if code == 0x7F or (0x80 <= code <= 0x9F and code != 0x85) or code in (0xFFFE, 0xFFFF):
            out.append(f"\\u{code:04x}")
        else:
            out.append(c)
    return "".join(out)


def _restricted_by_author(spec: dict[str, Any], names: set[str]) -> bool:
    """Whether the spec's own ``permissions`` deny or ask for a skill under one of
    *names*. Such a rule does not match a copy's new name, so that skill gets no
    copy."""
    from kiro_crew.acp.kas_permissions import META_CAPABILITY_EXPANSION

    block = spec.get("permissions")
    rules = block.get("rules") if isinstance(block, dict) else None
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("effect") not in ("deny", "ask"):
            continue
        capability = rule.get("capability")
        if not isinstance(capability, str):
            continue
        if SKILL_CAPABILITY not in META_CAPABILITY_EXPANSION.get(capability, (capability,)):
            continue
        match = rule.get("match")
        # No ``match``, or an empty one, is every skill to KAS (``isUnconditionalMatch``).
        listed = [p for p in match if isinstance(p, str)] if isinstance(match, list) else []
        patterns = listed or ["*"]
        exclude = rule.get("exclude")
        excluded = [p for p in exclude if isinstance(p, str)] if isinstance(exclude, list) else []
        for name in names:
            if any(_rule_matches(name, p) for p in patterns) and not any(
                _cedar_like(name, p) for p in excluded
            ):
                return True
    return False


def _cedar_like(text: str, pattern: str) -> bool:
    """KAS's skill-name match (``policy/cedar-compiler.ts``, ``globToCedarLike``):
    Cedar ``like``, where ``*`` is any run of characters and every other
    character, ``?`` and ``[`` included, is itself."""
    parts = pattern.split("*")
    if len(parts) == 1:
        return text == pattern
    head, *middle, tail = parts
    if len(text) < len(head) + len(tail) or not text.startswith(head) or not text.endswith(tail):
        return False
    at, end = len(head), len(text) - len(tail)
    for part in middle:
        found = text.find(part, at, end)
        if found == -1:
            return False
        at = found + len(part)
    return True


def _rule_matches(text: str, pattern: str) -> bool:
    """A ``match`` entry as KAS compiles it: ``like``, and for a pattern ending in
    `` *`` also the bare prefix (``matchConditionsForPattern``)."""
    bare = pattern[:-2] if pattern.endswith(" *") else ""
    return _cedar_like(text, pattern) or (bool(bare) and "*" not in bare and text == bare)


def _kiro_cli_names(text: str) -> set[str]:
    """The ``name`` kiro-cli reads from *text*, whatever KAS makes of it.

    ``format_skill_hint``: front matter opens with a ``---`` line and closes at
    the next line that trims to ``---``; the last line starting ``name:`` wins,
    trimmed. The unquoted form is added too, so a rule on either reaches it.
    """
    text = text.removeprefix("\ufeff")
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return set()
    lines = text.splitlines()
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if closing is None:
        return set()
    names = [line[len("name:") :].strip() for line in lines[1:closing] if line.startswith("name:")]
    if not names or not names[-1]:
        return set()
    name = names[-1]
    quoted = len(name) > 1 and name[0] == name[-1] and name[0] in "\"'"
    return {name, name[1:-1].strip() if quoted else name}


def _free_name(base: str, taken: set[str]) -> str:
    """*base*, or *base* with the first ``-N`` suffix no skill in *taken* uses."""
    name, n = base, 1
    while name in taken:
        n += 1
        suffix = f"-{n}"
        name = base[: _MAX_NAME_CHARS - len(suffix)].rstrip("-") + suffix
    return name


def _copy_text(path: Path, front: dict[str, Any] | None, body: str, name: str) -> str:
    """The copy of the skill at *path*: valid front matter, a note, the original body."""
    description = (front or {}).get("description") or ""
    if not description:
        first = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), "")
        description = first or _NO_DESCRIPTION
    description = _cut_utf16(description, _MAX_DESCRIPTION_UNITS).strip() or _NO_DESCRIPTION
    head = f"name: {_yaml_quoted(name)}\ndescription: {_yaml_quoted(description)}\n"
    note = (
        f"_Kiro Crew copied this skill from `{path}` so KAS can load it. "
        f"Relative paths in it are relative to `{path.parent}`._"
    )
    return f"---\n{head}---\n{note}\n\n{body}"


def _resource_globs(spec: dict[str, Any], spec_path: Path, work_dir: Any) -> list[str]:
    """The paths of *spec*'s ``skill://`` entries, which KAS reads as resources."""
    from kiro_crew.agent_discovery import expand_skill_uri, skill_resource_uris

    return [
        g
        for uri in skill_resource_uris(spec)
        if (g := expand_skill_uri(uri, spec_path, project_dir=work_dir))
    ]


def _matched_files(globs: list[str]) -> list[Path]:
    """The files *globs* match, at most :data:`_MAX_SKILL_FILES`; the ones past
    that bound are counted and named in one warning."""
    files: list[Path] = []
    seen: set[Path] = set()
    left_out = 0
    for pattern in globs:
        for match in sorted(glob.glob(pattern, recursive=True)):
            path = Path(match)
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if len(files) < _MAX_SKILL_FILES:
                files.append(path)
            else:
                left_out += 1
    if left_out:
        logger.warning(
            "KAS skill copies: %d skill files matched past the %d read; they get no copy",
            left_out,
            _MAX_SKILL_FILES,
        )
    return files


def _unsafe_to_copy(opened: str) -> bool:
    """Whether the kernel path of an opened skill file is one a prompt may not carry."""
    from kiro_crew.acp.kas_prompt_paths import is_unsafe_prompt_path

    return is_unsafe_prompt_path(Path(opened))


def refused_skill_copies(
    agent: str,
    spec: dict[str, Any],
    globs: list[str] | None,
    *,
    agents_dir: Path,
    work_dir: str | Path | None,
) -> list[SkillCopy]:
    """Copies of the in-scope skills KAS would drop, written for it to load.

    In scope is what *globs* (:func:`scope_globs`) match, or every default-tree
    skill when *globs* is ``None``. A skill is dropped when no way KAS reaches it
    loads it: the default trees (strict check) and the spec's ``skill://`` entries,
    forwarded as they are (front matter, name and description only). The copy gets
    a name KAS accepts, unused by any skill KAS loads, and a description: the
    original's, else the first line of the body, else kiro-cli's
    "No description available", cut to 1024. Each copy is rewritten at every
    session start, so it follows the original. A skill that cannot be read or
    written gets no copy and stays dropped, as before.
    """
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.config import paths
    from kiro_crew.hooks import safe_read_prefix

    native = set(_matched_files(_default_tree_globs(work_dir)))
    resource = set(_matched_files(_resource_globs(spec, agents_dir / f"{agent}.json", work_dir)))
    in_scope = sorted(native) if globs is None else _matched_files(globs)
    taken: set[str] = set()
    refused: list[tuple[Path, dict[str, Any] | None, str, str]] = []
    for path in sorted(native | resource | set(in_scope)):
        # A copy's text reaches the model, as a file:// prompt does, so the
        # file actually opened must pass the prompt fence too.
        raw = safe_read_prefix(str(path), _MAX_COPY_BYTES + 1, refuse=_unsafe_to_copy)
        if raw is None:
            continue
        text = raw.decode("utf-8", errors="replace")
        front, body = _kas_front_matter(text)
        loads = (path in native and _kas_loads(front, native=True)) or (
            path in resource and _kas_loads(front, native=False)
        )
        if loads:
            taken.add(str((front or {})["name"]))
        elif path in in_scope and len(raw) <= _MAX_COPY_BYTES:
            refused.append((path, front, body, text))
    copies: list[SkillCopy] = []
    root = paths.data_home() / COPY_DIR_NAME
    for path, front, body, text in refused:
        # Every name a rule may have been written against: kiro-cli's (read
        # whether or not KAS accepts the front matter), the folder, the path.
        authored = {str(path), path.parent.name if path.name == "SKILL.md" else path.stem}
        authored |= _kiro_cli_names(text)
        if isinstance((front or {}).get("name"), str):
            authored.add(front["name"].strip())  # type: ignore[index]
        if _restricted_by_author(spec, authored):
            logger.info("KAS skill %s is denied or asked under its own name; no copy", path)
            continue
        base = (
            _kas_name(str((front or {}).get("name") or ""))
            or _kas_name(path.parent.name if path.name == "SKILL.md" else path.stem)
            or "skill"
        )
        name = _free_name(base, taken)
        # Keyed by the name too: another session may give the same skill another
        # name, and must not rewrite this session's copy.
        key = f"{path.resolve()}\0{name}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        target = root / digest / "SKILL.md"
        try:
            # ``restrict_to_owner`` refuses a link planted in the parent chain, so
            # the gateway never writes where a sandboxed agent pointed it.
            atomic_write(
                target, _copy_text(path, front, body, name), newline="", restrict_to_owner=True
            )
        except OSError:
            logger.warning("KAS skill copy of %s could not be written", path, exc_info=True)
            continue
        taken.add(name)
        copies.append(SkillCopy(name, f"skill://{target}"))
    if copies:
        logger.info(
            "KAS would drop %d skill(s) kiro-cli loads; loading fixed copies: %s",
            len(copies),
            ", ".join(c.name for c in copies),
        )
    return copies
