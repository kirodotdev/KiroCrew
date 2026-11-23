"""kiro-cli ``toolsSettings`` translated into a KAS ``permissions`` block.

Split out of ``kas_permissions`` by size; every name here stays importable from
there, where every caller has always read it. A test patches
``kas_permissions.fails_closed_on_uncarried_blocked``, so this module calls that
name through ``kas_permissions`` at run time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew.acp import kas_permissions
from kiro_crew.acp.kas_deny_alternatives import deny_alternatives
from kiro_crew.acp.kas_deny_cli_forms import take_cli_written
from kiro_crew.acp.kas_permissions import (
    _GLOB_METACHARACTERS,
    _MAX_ALLOWED_COMMANDS,
    _MAX_PATTERN_LENGTH,
    _MAX_PATTERNS_PER_RULE,
    _MAX_RULES,
    _paths_not_all_carried,
    _usable_allowed_commands,
    _write_falls_back_to_deny,
    allowed_command_pattern,
)
from kiro_crew.acp.kas_spec_grants import READ_SETTINGS_KEYS
from kiro_crew.acp.kas_subagent_rules import _agent_glob, _available_agents_rules
from kiro_crew.platform.tools_settings_aliases import _TOOLS_SETTINGS_FAMILY

# --- kiro-cli ``toolsSettings`` -> KAS ``permissions`` -------------------------
#
# KAS declares ``toolsSettings`` a CLI-only field and replaces it with
# ``permissions``; its migration guide maps each field onto a capability rule.
# The translation below follows that guide, with two limits of its own:
#
# * Only what can be said faithfully is translated. A kiro-cli regex becomes a
#   glob only when it is a literal plus ``.*`` (a deny's group alternation first
#   split per branch, ``kas_deny_alternatives``); anything else is reported,
#   never guessed at, because a guessed deny can silently stop matching.
# * No ALLOW is emitted. The gap being closed is lost restrictions; an allow
#   left behind only means the call keeps prompting, and shell/filesystem allows
#   are a family Crew never auto-approves here (:data:`WITHHELD_CAPABILITIES`).
#   Every allow key is reported as not carried instead.
#
# The block this produces is fed to :func:`merge_user_permissions` like an
# authored one, so its denies travel as written.

#: Characters that keep a regex from being read as a literal. ``*`` is here too:
#: outside ``.*`` it is a quantifier on the character before it.
_REGEX_METACHARACTERS = frozenset(".^$*+?()[]{}|\\")

#: Shell redirect operators. KAS matches a shell rule against the command's
#: words, and a redirect and its target are not among them, so a deny written
#: to match one never sees it.
_REDIRECT_CHARACTERS = frozenset("<>")


def _regex_to_glob(pattern: str) -> str | None:
    """A kiro-cli regex (anchored ``\\A..\\z``) as a KAS glob, or ``None``.

    Only ``.*`` and backslash-escaped punctuation are understood; any other regex
    syntax returns ``None`` so the caller reports it instead of guessing. A
    literal character KAS would read as glob syntax (``*?[]{}!``) has no glob
    spelling, so it returns ``None`` too. A space at either end is kept, as kiro-cli.
    """
    body = pattern
    for prefix in ("\\A", "^"):
        body = body.removeprefix(prefix)
    for suffix in ("\\z", "$"):
        if body.endswith(suffix) and not body.endswith("\\" + suffix):
            body = body[: -len(suffix)]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if body.startswith(".*", i):
            out.append("*")
            i += 2
        elif ch == "\\" and i + 1 < len(body) and not body[i + 1].isalnum():
            if body[i + 1] in _GLOB_METACHARACTERS:
                return None
            out.append(body[i + 1])
            i += 2
        elif ch in _REGEX_METACHARACTERS or ch in _GLOB_METACHARACTERS:
            return None
        else:
            out.append(ch)
            i += 1
    return "".join(out) or None


def _url_glob(raw: str) -> str | None:
    """A ``web_fetch.blocked`` entry as a KAS host glob, or ``None``. KAS matches
    web_fetch rules against the URL's host, so a pattern that names a scheme or a
    path can never match there."""
    glob = _regex_to_glob(raw)
    return glob if glob is not None and not set(glob) & set("/:") else None


def _patterns(value: Any) -> list[str]:
    return (
        [p.strip() for p in value if isinstance(p, str) and p.strip()]
        if isinstance(value, list)
        else []
    )


def _chunked_rules(
    capability: str,
    effect: str,
    patterns: list[str],
    *,
    exclude: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One rule per :data:`_MAX_PATTERNS_PER_RULE` patterns, so no rule is refused for size.

    *exclude*, when given, is carried whole on every part.
    """
    step = _MAX_PATTERNS_PER_RULE
    narrowing = {} if exclude is None else {"exclude": list(exclude)}
    return [
        {"capability": capability, "match": patterns[i : i + step], **narrowing, "effect": effect}
        for i in range(0, len(patterns), step)
    ]


def tools_settings_to_permissions(
    tools_settings: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Translate a kiro-cli ``toolsSettings`` object into a KAS ``permissions`` block.

    Returns ``(block, not_carried)``: the block (``None`` when no rule came out)
    and the ``<tool>.<key>`` paths that have no faithful rule, so the caller can
    name exactly what this session runs without.
    """
    if not isinstance(tools_settings, dict):
        return None, []
    rules: list[dict[str, Any]] = []
    lost: list[str] = []
    star_subtrees: list[tuple[str, str, list[str]]] = []
    # Allows go in last (below), so an over-long list can never sink the denies.
    allow_groups: list[tuple[str, list[dict[str, Any]]]] = []

    def converted(where: str, values: Any, convert: Callable[[str], str | None], keep=False):
        out: list[str] = []
        for raw in [v for v in values if v] if keep else _patterns(values):
            glob = convert(raw)
            # An over-long pattern would make the merge refuse the WHOLE block,
            # so it is dropped and reported here instead.
            if glob is None or len(glob) > _MAX_PATTERN_LENGTH:
                lost.append(where)
            else:
                out.append(glob)
        return list(dict.fromkeys(out))

    for tool in sorted(tools_settings, key=str):
        settings = tools_settings[tool]
        family = _TOOLS_SETTINGS_FAMILY.get(tool) if isinstance(tool, str) else None
        if not isinstance(settings, dict):
            if settings:
                lost.append(str(tool))
            continue
        if family is None:
            lost.extend(f"{tool}.{key}" for key in sorted(settings, key=str) if settings[key])
            continue
        # Only shapes run live on KAS are carried: the plain deny lists, and the
        # `subagent` rules below (a deny with an `exclude`, and a name-scoped
        # allow). The deny-all-but shapes on a command or a path, where
        # `exclude` has not been run, are enforced by Crew's gate instead: the
        # shell's `denyByDefault` and the write tool's `fallbackAction: "deny"`
        # (below).
        handled: set[str] = set()

        if family == "shell":
            handled.add("deniedCommands")
            # An alternation is split into one glob per branch, as written, as kiro-cli does.
            entries = _patterns(settings.get("deniedCommands"))
            # Three entries travel as kiro-cli writes them, which is not what their
            # regex names, so the key is named as not carried too.
            cli_written, entries = take_cli_written(entries)
            split = [alt for raw in entries for alt in deny_alternatives(raw)]
            denied = converted(f"{tool}.deniedCommands", split, _regex_to_glob, keep=True)
            if cli_written:
                denied = list(dict.fromkeys([*denied, *cli_written]))
                lost.append(f"{tool}.deniedCommands")
            rules.extend(_chunked_rules("shell", "deny", denied))
            # kiro-cli matches the regex over the whole command line; KAS matches
            # the glob over the command's words, which do not include a redirect
            # or its target. The deny still travels, since it does stop
            # the character inside a word, but the entry is named as not carried.
            if any(set(glob) & _REDIRECT_CHARACTERS for glob in denied):
                lost.append(f"{tool}.deniedCommands")
            # No rule either: Crew's gate refuses every shell call the spec's
            # usable `allowedCommands` do not match (`spec_shell_allowlist`).
            handled.add("denyByDefault")
            # No rule: Crew answers the shell requests these match itself, at
            # the gate (`crew_answered_commands`). Only an entry it cannot use
            # is named.
            handled.add("allowedCommands")
            allowed = settings.get("allowedCommands")
            # Named when an entry is unusable, and when the bound cuts the list:
            # those commands keep prompting. The notice then names each such entry
            # in place of the key (`name_unreadable_entries`).
            if allowed and (
                not isinstance(allowed, list)
                or any(allowed_command_pattern(p) is None for p in allowed)
                or len(_usable_allowed_commands(tools_settings)) > _MAX_ALLOWED_COMMANDS
            ):
                lost.append(f"{tool}.allowedCommands")
            # No rule: Crew's read-only row answers a read-only command, also
            # under `denyByDefault` (`kas_spec_grants`). A non-boolean is named.
            if isinstance(settings.get("autoAllowReadonly"), bool):
                handled.add("autoAllowReadonly")
        elif family in ("read", "write"):
            capability = "fs_read" if family == "read" else "fs_write"
            handled.add("deniedPaths")
            # kiro-cli globs and KAS's filesystem globs share one syntax. A path
            # not already ending in a wildcard also gets a `/**` twin, so a bare
            # directory denies what is inside it, not just the directory entry.
            # Denying a file's non-existent subtree is harmless: wider, never looser.
            # A path ending in one `*` gets the twin too: kiro-cli's globset `*`
            # crosses `/` and KAS's minimatch `*` stops at it, so `/etc/*` alone
            # would leave `/etc/a/b` open on KAS. `/etc/*/**` closes it.
            denied_paths = converted(f"{tool}.deniedPaths", settings.get("deniedPaths"), str)
            fits = [p for p in denied_paths if len(p) + 3 <= _MAX_PATTERN_LENGTH]
            subtrees = [f"{p.rstrip('/')}/**" for p in fits if not p.endswith("*")]
            denied_paths = list(dict.fromkeys(denied_paths + subtrees))
            rules.extend(_chunked_rules(capability, "deny", denied_paths))
            if tool in READ_SETTINGS_KEYS:
                # No rule: Crew's gate answers a covered read (`crew_answered_read_paths`).
                handled.add("allowedPaths")
                if _paths_not_all_carried(settings.get("allowedPaths")):
                    lost.append(f"{tool}.allowedPaths")
            # Kept apart so they never cost the denies above their place: they
            # travel only when the whole block still fits (below).
            star_twins = [
                f"{p}/**"
                for p in fits
                if p.endswith("*")
                and not p.endswith(("**", "\\*"))
                and f"{p}/**" not in denied_paths
            ]
            if star_twins:
                star_subtrees.append((f"{tool}.deniedPaths", capability, star_twins))
            if family == "write":
                # `interactive` is the default: nothing is lost by leaving it out.
                # `deny` travels as no rule: Crew's gate refuses every write the
                # usable `allowedPaths` do not cover (`spec_write_allowlist`).
                handled.add("fallbackAction")
                # No rule: Crew's gate answers a covered write (`crew_answered_paths`).
                # Named when an entry is unusable.
                handled.add("allowedPaths")
                if _paths_not_all_carried(settings.get("allowedPaths")):
                    lost.append(f"{tool}.allowedPaths")
                if settings.get("fallbackAction") not in (
                    None,
                    "",
                    "interactive",
                ) and not _write_falls_back_to_deny(settings):
                    lost.append(f"{tool}.fallbackAction")
        elif family == "web_fetch":
            # KAS matches a host only, so a `blocked` regex is carried here as a host deny
            # and `trusted` (an allow) is named. A `blocked` entry with no host deny is
            # named unless the gate refuses it (`kas_spec_denies.gate_refused_blocked`);
            # with `agent.kas_fail_closed_web_fetch_blocked` on, one deny over "*" takes
            # the key's place and refuses every fetch. Crew's gate grants a plain-URL
            # `trusted` entry itself (`kas_spec_denies.web_fetch_gate`); the session
            # notice drops it then, and a crewmate's policy check reads only this side.
            handled.add("blocked")
            blocked = converted(f"{tool}.blocked", settings.get("blocked"), _url_glob)
            if f"{tool}.blocked" in lost and kas_permissions.fails_closed_on_uncarried_blocked():
                blocked = ["*"]
            rules.extend(_chunked_rules("web_fetch", "deny", blocked))
        elif family == "subagent":
            # KAS's `subagent` capability matches the sub-agent's name (its consent
            # names it as the resource), and it is the shape KAS itself writes for
            # trusted sub-agents (`{capability: "subagent", match: [ids], effect:
            # "allow"}`). Run live on kiro-cli 2.35.1: a deny over "*"
            # with the target in `exclude` let the call reach its permission
            # request, the same deny with another name excluded refused it, and
            # an allow naming it ran it unprompted.
            handled.update({"availableAgents", "trustedAgents"})
            if "availableAgents" in settings:
                rules.extend(
                    _available_agents_rules(
                        f"{tool}.availableAgents", settings["availableAgents"], lost
                    )
                )
            # An allow, so the merge puts it to the governance ceiling (the refs
            # that name the sub-agent tool) and drops it when the ceiling refuses.
            trusted = converted(f"{tool}.trustedAgents", settings.get("trustedAgents"), _agent_glob)
            allows = _chunked_rules("subagent", "allow", trusted)
            allow_groups.append((f"{tool}.trustedAgents", allows))

        lost.extend(
            f"{tool}.{key}"
            for key in sorted(settings, key=str)
            if key not in handled and settings[key]
        )

    if len(rules) > _MAX_RULES:
        return None, sorted(set(lost) | {"(all: too many rules)"})
    # The trailing-`*` subtree twins widen denies that already travel. Over the
    # rule bound they would sink the WHOLE block, so they are left out and the
    # key is named instead: the originals still deny what KAS's `*` reaches.
    for where, capability, twins in star_subtrees:
        extra = _chunked_rules(capability, "deny", twins)
        if len(rules) + len(extra) > _MAX_RULES:
            lost.append(where)
        else:
            rules.extend(extra)
    # Each key's allows whole, or named and left prompting: every deny still travels.
    for where, allows in allow_groups:
        if len(rules) + len(allows) > _MAX_RULES:
            lost.append(where)
        else:
            rules.extend(allows)
    return ({"rules": rules} if rules else None), sorted(set(lost))
