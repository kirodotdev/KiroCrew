"""The KAS projection's record of grants Crew honours once it checks a spec's denies.

``kas_agents.to_client_custom_agent`` keeps a shell or write grant at the prompt
when the spec also sets ``deniedCommands`` or a write ``deniedPaths``
(``kas_permissions.withhold_answered_under_authored_rules``). When every entry of
that deny list is one Crew reads the way kiro-cli does, this module records the
grants the deny alone held back under :data:`CREW_SPEC_DENIES_KEY`, together with
the list, and the session handle reads them back as one
:class:`~kiro_crew.platform.spec_denied_grant.SpecDeniedGrant`. See that module for
how the gate uses it. Any other key beside a deny (``fallbackAction``, an
authored ``ask``) keeps the grant withheld, as before; a boolean shell
``autoAllowReadonly`` does not, and ``denyByDefault`` withholds only the whole-tool
shell grant, never the ``allowedCommands`` beside it.
"""

from __future__ import annotations

import json
import os
import string
from typing import Any, cast

from kiro_crew.acp.kas_grant_only_settings import without_grant_only_settings
from kiro_crew.acp.kas_permissions import (
    _MAX_PATTERN_LENGTH,
    _SHELL_SETTINGS_KEYS,
    _WRITE_SETTINGS_KEYS,
    CREW_SPEC_DENIES_KEY,
    CREW_UNJUDGED_DENIES_KEY,
    _chunked_rules,
    _url_glob,
    shell_denies_carried,
    shell_deny_carried,
    shell_deny_globs,
    withhold_answered_under_authored_rules,
)
from kiro_crew.platform import allowed_commands, allowed_paths
from kiro_crew.platform.spec_denied_gate import url_literal
from kiro_crew.platform.spec_denied_grant import SpecDeniedGrant
from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

#: The built-ins a checked deny may lift: the ones whose calls the gate can test
#: against it (a shell call's command, a write's targets). ``code`` both edits and
#: runs commands, so it stays withheld.
_LIFTABLE: frozenset[str] = frozenset(
    {"execute_bash", "execute_pwsh", "fs_write", "fs_append", "str_replace"}
)

#: Bound on each recorded deny list, like the allowlists'.
_MAX_DENIES = 64

#: The capabilities whose rule reaches a ``web_fetch`` call.
_WEB_FETCH_CAPABILITIES = frozenset({"web_fetch", "builtin", "all"})


def _web_fetch_entries(settings: dict[str, Any], field: str, read: Any) -> tuple[list[str], bool]:
    """``(readable, complete)``: the *field* entries *read* accepts, deduplicated and
    bounded, and whether that is every entry. An absent field is complete."""
    if field not in settings:
        return [], True
    entries = settings[field]
    if not isinstance(entries, list):
        return [], False
    complete = all(isinstance(r, str) and read(r) is not None for r in entries)
    readable = list(dict.fromkeys(r for r in entries if isinstance(r, str) and read(r) is not None))
    if len(readable) > _MAX_DENIES:
        return readable[:_MAX_DENIES], False
    return readable, complete


def trusted_past_bound(tools_settings: Any) -> list[tuple[int, str]]:
    """``(position as written, entry)`` of each distinct ``web_fetch.trusted`` entry
    past the :data:`_MAX_DENIES` the gate reads, in order, when the bound is all that
    keeps the list from being read whole: every entry is one :func:`url_literal`
    reads. ``[]`` otherwise. An entry written twice keeps its first place."""
    settings = tools_settings.get("web_fetch") if isinstance(tools_settings, dict) else None
    entries = settings.get("trusted") if isinstance(settings, dict) else None
    if not isinstance(entries, list):
        return []
    if not all(isinstance(r, str) and url_literal(r) is not None for r in entries):
        return []
    placed: dict[str, int] = {}
    for at, raw in enumerate(entries, 1):
        placed.setdefault(raw, at)
    return [(at, raw) for raw, at in list(placed.items())[_MAX_DENIES:]]


def _web_fetch_lists(tools_settings: Any) -> tuple[list[str], bool, list[str], bool]:
    """``(blocked, blocked_complete, trusted, trusted_complete)`` of the canonical
    ``web_fetch`` key. ``trusted`` is empty unless ``blocked`` is complete: a grant
    is never made past a ``blocked`` entry Crew cannot read."""
    settings = tools_settings.get("web_fetch") if isinstance(tools_settings, dict) else None
    if not isinstance(settings, dict):
        return [], True, [], True
    blocked, blocked_complete = _web_fetch_entries(settings, "blocked", url_literal)
    trusted, trusted_complete = _web_fetch_entries(settings, "trusted", url_literal)
    if not blocked_complete:
        return blocked, False, [], False
    return blocked, True, trusted, trusted_complete


def _authored_web_fetch_restricted(authored_permissions: Any) -> bool:
    """Whether the spec's own ``permissions`` block restricts ``web_fetch``: an
    ``ask`` or ``deny`` reaching it (any scope, meta capabilities included), read
    raw like ``kas_permissions.authored_restricted_capabilities``, so a rule the
    merge drops still counts; or a block the merge refuses whole, or one this
    cannot read. Crew's gate then grants no ``web_fetch``, so the author's rule
    keeps the prompt it had on main."""
    raw = authored_permissions
    if raw is None:
        return False
    from kiro_crew.acp.kas_permissions import projection_accepts

    if not isinstance(raw, dict) or set(raw) - {"rules", "policies"}:
        return True
    rules = raw.get("rules", [])
    if raw.get("policies") not in (None, []) or not isinstance(rules, list):
        return True
    if not projection_accepts(raw):
        return True
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("capability"), str):
            return True
        if rule.get("effect") != "allow" and rule["capability"] in _WEB_FETCH_CAPABILITIES:
            return True
    return False


def web_fetch_gate(tools_settings: Any, authored_permissions: Any = None) -> dict[str, Any]:
    """The ``web_fetch`` part of the :data:`CREW_SPEC_DENIES_KEY` record.

    kiro-cli matches ``web_fetch.trusted`` against the whole URL; KAS carries no
    allow for it, so the fetch asks. On a fetch that reaches Crew's gate,
    ``spec-builtin-grant`` grants one a trusted entry names as a plain URL or a
    plain URL plus ``.*`` (:func:`url_literal`), only when every
    ``web_fetch.blocked`` entry has that shape too and none matches. An ``ask`` or
    ``deny`` in *authored_permissions* reaching ``web_fetch``, or a block the merge
    refuses, drops the grant (:func:`_authored_web_fetch_restricted`).

    The readable blocked entries travel too, and ``spec-denies`` refuses a fetch
    one definitely matches (``spec_denied_gate.web_fetch_blocked_match``); with no
    trusted grant they travel alone when an entry is one the gate refuses
    (:func:`gate_refused_blocked`). Empty when the gate has nothing to judge.
    """
    blocked, complete, trusted, _ = _web_fetch_lists(tools_settings)
    if trusted and complete and not _authored_web_fetch_restricted(authored_permissions):
        return {
            "webFetchBlocked": blocked,
            "webFetchBlockedComplete": complete,
            "webFetchTrusted": trusted,
        }
    if not gate_refused_blocked(tools_settings):
        return {}
    return {"webFetchBlocked": blocked, "webFetchBlockedComplete": complete, "webFetchTrusted": []}


def gate_refused_blocked(tools_settings: Any) -> list[str]:
    """The ``web_fetch.blocked`` entries KAS cannot carry as a host deny that Crew's
    gate refuses itself: each one :func:`url_literal` reads among the entries the
    record carries (:func:`_web_fetch_lists`), as written, that can match an
    ``http(s)`` URL (:func:`_blocked_ask_host`; any other stays as on main). ``[]`` while
    ``agent.kas_fail_closed_web_fetch_blocked`` is on: its KAS deny over ``*``
    already refuses every fetch. One reader for the record, the asks
    (:func:`with_blocked_host_asks`) and the notice, so they cannot disagree."""
    from kiro_crew.acp import kas_unread_commands

    def carried(raw: str) -> bool:
        glob = _url_glob(raw.strip())
        return glob is not None and len(glob) <= _MAX_PATTERN_LENGTH

    blocked = _web_fetch_lists(tools_settings)[0]
    uncarried = [
        raw for raw in blocked if raw.strip() and not carried(raw) and _blocked_ask_host(raw) != ""
    ]
    if not uncarried or kas_unread_commands.fails_closed_on_uncarried_blocked():
        return []
    return uncarried


#: The characters of a host an ``ask`` is scoped to; any other asks unscoped.
_ASK_HOST_CHARS = frozenset(string.ascii_lowercase + string.digits + "._-")


def _blocked_ask_host(raw: str) -> str | None:
    """The host glob a KAS ``web_fetch`` ``ask`` for the blocked entry *raw* is
    scoped to, read with string work like ``kas_permissions._url_glob``: the
    literal's authority up to the first ``/``, ``?`` or ``#``, port dropped,
    lower-cased, with a trailing ``*`` when a prefix entry ends inside the host.

    ``""`` when the entry needs no ask: another scheme's, or one with no ``://``
    that cannot start an ``http(s)`` URL, never matches one. ``None`` when it
    names no clear host (empty, userinfo, an address literal, another character,
    past the pattern bound): the caller then asks for every fetch."""
    entry = url_literal(raw)
    if entry is None:
        return None
    literal, prefix = entry
    scheme, sep, rest = literal.partition("://")
    if not sep:
        starts_url = prefix and any(s.startswith(literal) for s in ("http://", "https://"))
        return None if starts_url else ""
    if scheme.lower() not in ("http", "https"):
        return ""
    cut = min((i for i in map(rest.find, "/?#") if i >= 0), default=len(rest))
    authority = rest[:cut]
    if "@" in authority or "[" in authority:
        return None
    host, colon, _port = authority.partition(":")
    host = host.lower()
    if prefix and cut == len(rest) and not colon:
        host += "*"
    name = host.removesuffix("*")
    if not name or not _ASK_HOST_CHARS.issuperset(name) or len(host) > _MAX_PATTERN_LENGTH:
        return None
    return host


def with_blocked_host_asks(policy: Any, tools_settings: Any) -> Any:
    """*policy* with a KAS ``web_fetch`` ``ask`` scoped to the host of each
    :func:`gate_refused_blocked` entry (:func:`_blocked_ask_host`), or one unscoped
    ``ask`` when an entry names no clear host. KAS resolves deny over ask over
    allow and matches a ``web_fetch`` rule on the URL's host, so a fetch to one of
    those hosts reaches Crew's gate as a permission request; any other fetch is
    left as it was. *policy* unchanged when no entry needs one."""
    hosts = [_blocked_ask_host(raw) for raw in gate_refused_blocked(tools_settings)]
    if any(host is None for host in hosts):
        asks: list[dict[str, Any]] = [{"capability": "web_fetch", "effect": "ask"}]
    else:
        asks = _chunked_rules("web_fetch", "ask", list(dict.fromkeys(h for h in hosts if h)))
    if not asks:
        return policy
    block = dict(policy) if isinstance(policy, dict) else {}
    block["rules"] = [*(block.get("rules") or []), *asks]
    return block


def _auto_approves_web_fetch(policy: Any) -> bool:
    """Whether *policy* runs a ``web_fetch`` unprompted on any host: an unscoped
    ``allow`` reaching it and no unscoped ``ask`` or ``deny`` (a hook's ``ask``, a
    kept authored rule) holding it back."""
    rules = policy.get("rules") if isinstance(policy, dict) else None
    if not isinstance(rules, list) or any(v for k, v in policy.items() if k != "rules"):
        return False
    unscoped = [
        r
        for r in rules
        if isinstance(r, dict)
        and r.get("capability") in _WEB_FETCH_CAPABILITIES
        and "match" not in r
        and "exclude" not in r
    ]
    effects = {r.get("effect") for r in unscoped}
    return "allow" in effects and not effects & {"ask", "deny"}


def web_fetch_regranted(tools_settings: Any, authored_permissions: Any, policy: Any) -> bool:
    """Whether the record lifts ``web_fetch`` by name (its ``builtins``): the
    asks of :func:`with_blocked_host_asks` are all that hold a fetch back. *policy*
    is the projected ``permissions`` before those asks; it must auto-approve the
    fetch (:func:`_auto_approves_web_fetch`), the spec's own block must not
    restrict ``web_fetch`` (:func:`_authored_web_fetch_restricted`), and the ceiling
    must let the gate grant it. The gate then grants only a URL clear of every
    readable blocked entry (``spec_denied_gate.denied_grant_applies``)."""
    if not gate_refused_blocked(tools_settings) or not _auto_approves_web_fetch(policy):
        return False
    if _authored_web_fetch_restricted(authored_permissions):
        return False
    from kiro_crew.platform.governance import may_grant_builtin_at_gate_now

    return may_grant_builtin_at_gate_now("web_fetch")


def web_fetch_carried(tools_settings: Any, authored_permissions: Any) -> frozenset[str]:
    """The ``web_fetch.<key>`` paths Crew's gate applies for *tools_settings*, for the
    session-start notice: ``web_fetch.trusted`` when every blocked and trusted entry
    is readable, the spec's own block does not restrict ``web_fetch`` and the
    ceiling lets the gate grant it. A trusted list past the bound still counts: the
    gate grants its first entries, and the notice names the rest
    (:func:`trusted_past_bound`). ``web_fetch.blocked`` when every entry KAS cannot
    carry is one the gate refuses (:func:`gate_refused_blocked`); otherwise the
    notice names each entry neither applies
    (``kas_unread_commands._uncarried_web_fetch_blocked``).
    """
    from kiro_crew.acp.kas_unread_commands import _uncarried_web_fetch_blocked

    carried: set[str] = set()
    if gate_refused_blocked(tools_settings) and not _uncarried_web_fetch_blocked(tools_settings):
        carried.add("web_fetch.blocked")
    _blocked, complete, trusted, trusted_complete = _web_fetch_lists(tools_settings)
    trusted_complete = trusted_complete or bool(trusted_past_bound(tools_settings))
    if not (trusted and complete and trusted_complete):
        return frozenset(carried)
    if _authored_web_fetch_restricted(authored_permissions):
        return frozenset(carried)
    from kiro_crew.platform.governance import may_grant_builtin_at_gate_now

    if may_grant_builtin_at_gate_now("web_fetch"):
        carried.add("web_fetch.trusted")
    return frozenset(carried)


def _checkable(tools_settings: Any, keys: tuple[str, ...], field: str, read: Any) -> list[str]:
    """Every entry of *field* on *keys*, when *read* accepts each one; else ``[]``.

    ``[]`` when no key sets the field, when any value is not a list of strings, when
    any entry, blank-looking ones included, is outside what *read* accepts, or past
    the bound: then the
    deny is not lifted and keeps its grant at the prompt (fail toward ask).
    """
    if not isinstance(tools_settings, dict):
        return []
    out: list[str] = []
    for key in keys:
        settings = tools_settings.get(key)
        if not isinstance(settings, dict) or field not in settings:
            continue
        entries = settings[field]
        if not isinstance(entries, list):
            return []
        for raw in entries:
            # Kept verbatim: a blank-looking entry such as " " is a real path or
            # command to kiro-cli, so it is checked like any other, never dropped.
            if not isinstance(raw, str) or read(raw) is None:
                return []
            out.append(raw)
    out = list(dict.fromkeys(out))
    return out if len(out) <= _MAX_DENIES else []


def _refusable_deny(raw: object) -> bool:
    """Whether the ``spec-denies`` row can refuse what the ``deniedCommands`` entry
    *raw* names: Crew reads it as wide as kiro-cli (``allowed_commands.parse_deny``)
    and, with an optional ``^`` / ``\\A`` and ``$`` / ``\\z`` anchor taken off, it
    holds no character a command the row judges never holds
    (``allowed_commands.UNGRANTABLE_COMMAND_CHARS``: a redirect, a separator, a
    substitution). The row judges a plain command and each command of a chain, so
    an entry naming one of those characters could never match. One reader for the
    record and the notice, so they cannot disagree."""
    if not isinstance(raw, str) or allowed_commands.parse_deny(raw) is None:
        return False
    body = raw.removeprefix("\\A") if raw.startswith("\\A") else raw.removeprefix("^")
    if body.endswith("\\z") and not body.endswith("\\\\z"):
        body = body[:-2]
    elif body.endswith("$") and not body.endswith("\\$"):
        body = body[:-1]
    return not allowed_commands.UNGRANTABLE_COMMAND_CHARS.intersection(body)


def uncarried_denied_commands(tools_settings: Any, policy: Any) -> list[str]:
    """The ``deniedCommands`` entries the gate refuses on its own, or ``[]``.

    Empty when every entry reaches *policy* as a KAS deny rule
    (``kas_permissions.shell_denies_carried``). Otherwise every entry Crew reads as
    wide as kiro-cli and the row can judge (:func:`_refusable_deny`), so a command
    one names is refused on every approval policy, not only where KAS asks. Any
    other entry refuses nothing, as on main; the session-start notice names it
    (``kas_agents.spec_keys_without_carrier``). *tools_settings* has one key per
    kiro-cli alias group.
    """
    if not isinstance(tools_settings, dict) or shell_denies_carried(tools_settings, policy):
        return []
    out: list[str] = []
    for key in _SHELL_SETTINGS_KEYS:
        settings = tools_settings.get(key)
        entries = settings.get("deniedCommands") if isinstance(settings, dict) else None
        for raw in entries if isinstance(entries, list) else ():
            if _refusable_deny(raw):
                out.append(raw)
    return list(dict.fromkeys(out))[:_MAX_DENIES]


def denied_command_items(tools_settings: Any, policy: Any = None) -> list[dict[str, Any]]:
    """Uncarried denies with raw values and classification separate from display text.

    ``whole`` names a malformed list; ``past_bound`` distinguishes a readable
    entry beyond the record limit from one the gate can actually check.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(tools_settings, dict):
        return out
    globs = shell_deny_globs(policy)
    kept: set[str] = set()
    for key in _SHELL_SETTINGS_KEYS:
        settings = tools_settings.get(key)
        if not isinstance(settings, dict) or "deniedCommands" not in settings:
            continue
        entries = settings["deniedCommands"]
        if not isinstance(entries, list):
            out.append(
                dict(
                    key=f"{key}.deniedCommands",
                    entry=entries,
                    conditional=False,
                    past_bound=False,
                    whole=True,
                )
            )
            continue
        for raw in entries:
            readable = _refusable_deny(raw)
            past = readable and raw not in kept and len(kept) >= _MAX_DENIES
            if readable and not past:
                kept.add(raw)
            if not (isinstance(raw, str) and shell_deny_carried(raw, globs)):
                out.append(
                    dict(
                        key=f"{key}.deniedCommands",
                        entry=raw,
                        conditional=readable and not past,
                        past_bound=past,
                        whole=False,
                    )
                )
    return out


def deny_entry_text(item: dict[str, Any]) -> str:
    """The authored entry for display, or its key when the whole list is malformed."""
    if item["whole"]:
        return str(item["key"])
    raw = item["entry"]
    return raw if isinstance(raw, str) and raw.isprintable() else json.dumps(raw)


def denied_command_notice(item: dict[str, Any]) -> str:
    """Format one structured deny for display; callers never parse this text."""
    if item["whole"]:
        return str(item["key"])
    if item["past_bound"]:
        suffix = f" (past the first {_MAX_DENIES})"
    elif item["conditional"]:
        suffix = GATE_JUDGED_MARK
    else:
        suffix = ""
    return f"{item['key']}: {deny_entry_text(item)}{suffix}"


def unread_denied_commands(tools_settings: Any, policy: Any = None) -> list[str]:
    """Display-only compatibility view of :func:`denied_command_items`."""
    return [denied_command_notice(item) for item in denied_command_items(tools_settings, policy)]


#: Marks a notice item the gate refuses, one command of a chain at a time; a
#: command that does not split there (``spec_denied_gate.spec_denies_refusal``)
#: is not judged.
GATE_JUDGED_MARK = " (refused by Kiro Crew's gate)"


def _without(tools_settings: Any, keys: tuple[str, ...], field: str) -> Any:
    """*tools_settings* without *field* on *keys*; an entry left empty goes too."""
    if not isinstance(tools_settings, dict):
        return tools_settings
    out = dict(tools_settings)
    for key in keys:
        entry = out.get(key)
        if isinstance(entry, dict) and field in entry:
            rest = {k: v for k, v in entry.items() if k != field}
            if rest:
                out[key] = rest
            else:
                del out[key]
    return out


def spec_denies_record(
    spec: dict[str, Any],
    *,
    candidates: frozenset[str],
    answered: frozenset[str],
    policy: Any,
    commands: list[str],
    commands_answered: bool,
) -> dict[str, Any] | None:
    """The :data:`CREW_SPEC_DENIES_KEY` value for *spec*, or ``None``.

    *candidates* are the built-ins the spec grants that the ceiling allows,
    *answered* those already answered unconditionally, *policy* the merged
    ``permissions`` BEFORE the translated ``toolsSettings`` (whose denies are the
    ones lifted here; an authored rule still counts), *commands* the usable
    ``allowedCommands`` and *commands_answered* whether they are already answered.
    """
    # One key per kiro-cli alias group, the one kiro-cli itself keeps: a deny under
    # a spelling kiro-cli discards is not one it enforces.
    tools_settings, _ = canonical_tools_settings(spec.get("toolsSettings"))
    denied_commands = _checkable(
        tools_settings, _SHELL_SETTINGS_KEYS, "deniedCommands", allowed_commands.parse_deny
    )
    denied_paths = _checkable(
        tools_settings, _WRITE_SETTINGS_KEYS, "deniedPaths", allowed_paths.parse
    )
    if not (denied_commands or denied_paths):
        return None
    lifted_settings = without_grant_only_settings(tools_settings)
    if denied_commands:
        lifted_settings = _without(lifted_settings, _SHELL_SETTINGS_KEYS, "deniedCommands")
    if denied_paths:
        lifted_settings = _without(lifted_settings, _WRITE_SETTINGS_KEYS, "deniedPaths")
    lifted = withhold_answered_under_authored_rules(
        candidates,
        policy,
        authored_permissions=spec.get("permissions"),
        tools_settings=lifted_settings,
    )
    builtins = sorted((lifted & candidates & _LIFTABLE) - answered)
    # The `allowedCommands` answer's own reading: `denyByDefault` refuses only a
    # command no entry matches, so it holds no matched one back.
    from kiro_crew.acp.kas_agents import _allowed_commands_withheld

    lifted_commands = (
        commands
        if commands
        and not commands_answered
        and not _allowed_commands_withheld(spec, policy, lifted_settings)
        else []
    )
    if not (builtins or lifted_commands):
        return None
    return {
        "builtins": builtins,
        "commands": list(lifted_commands),
        "deniedCommands": denied_commands,
        "deniedPaths": denied_paths,
    }


def commands_lifted_by_denies(spec: dict[str, Any]) -> bool:
    """Whether *spec*'s ``allowedCommands`` are answered once its denies are
    checked, read with no merged policy, for the session-start notice."""
    from kiro_crew.acp.kas_permissions import crew_answered_commands

    record = spec_denies_record(
        spec,
        candidates=frozenset(),
        answered=frozenset(),
        policy=None,
        commands=crew_answered_commands(
            canonical_tools_settings(spec.get("toolsSettings"))[0], spec.get("permissions")
        ),
        commands_answered=False,
    )
    return bool(record and record["commands"])


def _entry_spec_denies(entry: Any, cwd: str) -> SpecDeniedGrant | None:
    """One projected entry's record as a :class:`SpecDeniedGrant`, anchored at *cwd*.

    ``None`` when the entry has none, or when any part of it is malformed: no
    grant, so the call keeps asking.
    """
    raw = entry.get(CREW_SPEC_DENIES_KEY) if isinstance(entry, dict) else None
    if not isinstance(raw, dict):
        return None
    fields = ("builtins", "commands", "deniedCommands", "deniedPaths")
    values = [raw.get(f) for f in fields]
    if not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in values):
        return None
    builtins, commands, denied_commands, denied_paths = cast(list[list[str]], values)
    web_blocked = raw.get("webFetchBlocked", [])
    web_trusted = raw.get("webFetchTrusted", [])
    web_complete = raw.get("webFetchBlockedComplete", True)
    if not (
        all(
            isinstance(v, list) and all(isinstance(x, str) for x in v)
            for v in (web_blocked, web_trusted)
        )
        and isinstance(web_complete, bool)
    ):
        return None
    from kiro_crew.acp.kas_write_allowlist import anchor_write_allowlist

    per_entry = [anchor_write_allowlist([path], cwd) for path in dict.fromkeys(denied_paths)]
    if not all(per_entry):
        # An entry that cannot be anchored here (a relative one with no session
        # directory) checks nothing, so nothing it held back is granted.
        return None
    anchored = tuple(dict.fromkeys(glob for globs in per_entry for glob in globs))
    return SpecDeniedGrant(
        builtins=frozenset(builtins),
        commands=tuple(commands),
        denied_commands=tuple(denied_commands),
        denied_write_paths=anchored,
        cwd=cwd if os.path.isabs(cwd) else "",
        web_fetch_blocked=tuple(web_blocked),
        web_fetch_blocked_complete=web_complete,
        web_fetch_trusted=tuple(web_trusted),
    )


def projected_spec_denies(custom_agents: Any, agent_id: str, cwd: str) -> SpecDeniedGrant | None:
    """*agent_id*'s registered entry's record, matched like
    ``kas_agents.projected_crew_answered``."""
    from kiro_crew.acp.kas_agents import _registered_entry

    return _entry_spec_denies(_registered_entry(custom_agents, agent_id), cwd)


def switched_spec_denies(custom_agents: Any, mode_id: str, cwd: str) -> SpecDeniedGrant | None:
    """That record once a KAS mode switch moves the session to *mode_id*."""
    from kiro_crew.acp.kas_agents import _switched_entry

    return _entry_spec_denies(_switched_entry(custom_agents, mode_id), cwd)


def unjudged_denied_commands(
    tools_settings: Any, policy: Any = None, *, conditional: bool = False
) -> list[str]:
    """Authored denies the gate cannot check, for the Add-abilities notice.

    With *conditional*, the gate-only entries instead: the gate checks those only
    in a command ``spec_denied_gate.shell_denies_match`` can split, so a Bot's
    worker holds every other command that is not read-only for them.
    """
    settings, _dropped = canonical_tools_settings(tools_settings)
    return list(
        dict.fromkeys(
            deny_entry_text(item)
            for item in denied_command_items(settings, policy)
            if item["conditional"] is conditional
        )
    )


def with_unjudged_denies(
    custom_agents: list[dict[str, Any]], agent_id: str, spec: dict[str, Any]
) -> list[dict[str, Any]]:
    """Record structured denies for the Bot worker, never on the KAS wire.

    Conditional entries use the refusal row's shared judge; other entries hold
    every non-read-only command. Authored text never carries classification.
    """
    out: list[dict[str, Any]] = []
    for entry in custom_agents:
        if isinstance(entry, dict) and entry.get("id") == agent_id:
            settings, _ = canonical_tools_settings(spec.get("toolsSettings"))
            unjudged = denied_command_items(settings, entry.get("permissions"))
            if unjudged:
                entry = {**entry, CREW_UNJUDGED_DENIES_KEY: unjudged}
        out.append(entry)
    return out


def _entry_unjudged_denies(entry: Any) -> tuple[dict[str, Any] | str, ...]:
    """One projected entry's :data:`CREW_UNJUDGED_DENIES_KEY` list; a malformed one
    is read as one unjudged entry, so the gate still holds the shell."""
    if not isinstance(entry, dict) or CREW_UNJUDGED_DENIES_KEY not in entry:
        return ()
    from kiro_crew.agent_sdk.kas_tool_view import worker_deny_records

    return worker_deny_records(entry[CREW_UNJUDGED_DENIES_KEY])


def projected_unjudged_denies(
    custom_agents: Any, agent_id: str
) -> tuple[dict[str, Any] | str, ...]:
    """*agent_id*'s registered entry's unjudged denies, matched like
    :func:`projected_spec_denies`."""
    from kiro_crew.acp.kas_agents import _registered_entry

    return _entry_unjudged_denies(_registered_entry(custom_agents, agent_id))


def switched_unjudged_denies(custom_agents: Any, mode_id: str) -> tuple[dict[str, Any] | str, ...]:
    """Those once a KAS mode switch moves the session to *mode_id*."""
    from kiro_crew.acp.kas_agents import _switched_entry

    return _entry_unjudged_denies(_switched_entry(custom_agents, mode_id))
