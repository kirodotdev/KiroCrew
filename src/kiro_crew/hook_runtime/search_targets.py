"""The file-search deny target: the scope-only grammar a deny rule matches, its
percent encoding, the home-variable substitution and the lexical root
normalization.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _RECURSIVE_SEARCH_OPERATIONS,
        _SEARCH_DENY_ARG_KEYS,
        _SEARCH_DENY_ESCAPES,
        _SEARCH_DENY_PREFIX,
        _SEARCH_HOME_VARS,
        _SEARCH_PATH_FIELD,
    )


def _encode_search_field(value: str) -> str:
    """Percent-encode the characters a value could use to forge a field."""
    for raw, encoded in _SEARCH_DENY_ESCAPES:
        value = value.replace(raw, encoded)
    return "".join("%20" if ch.isspace() else ch for ch in value)


def _expand_home_vars(value: str) -> str:
    """Substitute only home-denoting variables; leave every other ``$VAR`` literal.

    NOT ``os.path.expandvars``, which dereferences ANY variable. That would turn the
    match target into a carrier for secret VALUES: the deny decision would depend on,
    and every downstream consumer of the target or of a rule hit (refusal text shown
    to the model, audit metadata, operator tooling) could then receive, whatever
    ``$NAME`` an agent chose to embed — a dereference the gate never needs, because
    only the home spellings have a value worth collapsing (the home path is what a
    home rule matches on anyway).

    An UNSET variable is left literal rather than substituted empty: turning
    ``$HOME/x`` into ``/x`` would claim a root-scope walk the tool never performs, and
    a rule matching that broader scope would deny the wrong thing.
    """
    for name in _SEARCH_HOME_VARS:
        expanded = os.environ.get(name)
        if not expanded:
            continue
        for spelling in (f"${name}", f"${{{name}}}", f"%{name}%"):
            value = value.replace(spelling, expanded)
    return value


def _normalize_search_path(value: str) -> str:
    """Canonicalize a search root so one spelling reaches a rule.

    ``~``, ``$HOME``, and ``.``/``..`` segments all name a tree a rule must be able
    to refuse under a single spelling — ``path="~"`` walks the home tree just as
    ``path="/data/op"`` does, and a rule anchored on the literal root matches
    only the latter. Mirrors what the sensitive-path keystone on this same gate
    already does with ``raw_params['path']``.

    Steps: expand the home variables and ``~``, ``normpath``, rewrite separators to
    ``/``, and collapse a leading ``//`` to ``/`` on POSIX. The separator rewrite keeps
    the emitted grammar OS-independent: ``normpath`` produces backslash separators on
    Windows, so a rule authored with ``/`` — the form the spec documents — would
    silently stop matching there, which fails OPEN. The collapse is POSIX-only
    because POSIX leaves a path beginning with exactly two slashes
    implementation-defined while on Windows a leading ``//`` is a UNC or
    extended-length root that must survive intact.

    Variable expansion is restricted to ``_SEARCH_HOME_VARS`` (see
    ``_expand_home_vars``) because this value is MODEL-AUTHORED and the resulting
    target is audited: expanding arbitrary variables would dereference a secret into
    a log. A non-home variable therefore stays literal, so no rule keyed on an
    absolute prefix matches it — an over-block/under-match in the same fail-safe
    direction as the relative-root decision below.

    DELIBERATELY NOT ``abspath``, which is where this diverges from
    ``governance._norm_item``: absolutizing resolves a relative root against the
    GATEWAY process cwd, which is not the cwd the tool runs in. That misattribution
    cuts both ways — a rule denying the tree actually walked is bypassed, and a rule
    naming the gateway's own tree falsely denies an unrelated search. Governance can
    absorb that because it is a policy intersection where an ungoverned scope
    permits; a hard deny cannot. A relative root therefore stays relative and no
    rule keyed on an absolute prefix matches it (see the residual limits).

    LEXICAL ONLY: no ``realpath``, so a symlink into a denied tree is not resolved.
    The resolved sensitive-path keystone remains the layer that does not depend on
    spelling.

    Never raises: this runs inside the permission gate, where an exception is a crash
    rather than a security decision. On any failure the raw value is returned for the
    caller to encode, which cannot forge a field.

    Only a BARE ``~`` (alone or followed by a separator) is expanded, NOT ``~name``,
    and the home directory comes from the ``_SEARCH_HOME_VARS`` environment values
    ONLY — ``os.path.expanduser`` is never called. Both of its lookup paths reach
    the account database through synchronous NSS calls that can stall the gateway
    event loop for seconds on LDAP-backed hosts: ``~name`` via ``pwd.getpwnam``
    (agent-controlled name), and bare ``~`` with ``HOME`` unset via
    ``pwd.getpwuid``. When no home variable is set the ``~`` stays literal — the
    same contract as an unexpanded ``$HOME`` — so the account database is never
    consulted at all.

    The expansion is built by CONCATENATION, never ``os.path.join``: join discards
    every earlier component when a later one is absolute, so ``~//etc`` (remainder
    ``/etc``) would come out as ``/etc`` — the gate would encode a root-scoped target
    while the search itself resolves under the real home, and a home-scoped deny rule
    would miss. Leading separators are stripped from the remainder instead, which is
    exactly what the shells and search tools this gate fronts do with ``~//etc``
    (``$HOME//etc`` == ``$HOME/etc``). The invariant across this whole branch: agent
    text is never dereferenced through the environment or account database beyond the
    fixed HOME spellings and the current user's own home, and once the home prefix is
    chosen nothing later in the string can displace it.
    """
    try:
        expanded = _expand_home_vars(value)
        if expanded == "~" or expanded.startswith(("~/", "~" + os.sep)):
            home = next((h for h in map(os.environ.get, _SEARCH_HOME_VARS) if h), "")
            if home:
                rest = expanded[1:].lstrip(os.sep + (os.altsep or ""))
                expanded = home + os.sep + rest if rest else home
        path = os.path.normpath(expanded)
    except (OSError, ValueError):
        return value
    path = path.replace(os.sep, "/")
    if os.altsep:
        path = path.replace(os.altsep, "/")
    if os.name != "nt" and path.startswith("//") and not path.startswith("///"):
        path = path[1:]
    return path


def _is_search_shaped(raw_params: Mapping) -> bool:
    """Whether these arguments describe a recursive search.

    A non-empty ``pattern`` string, or an ``operation`` naming a recursive walk that
    carries no pattern of its own.
    """
    pattern = raw_params.get("pattern")
    if isinstance(pattern, str) and pattern:
        return True
    operation = raw_params.get("operation")
    return isinstance(operation, str) and operation in _RECURSIVE_SEARCH_OPERATIONS


def _search_deny_target(raw_params: dict | None) -> str:
    """Synthesize a deny-matcher target from a file-search call's scope arguments.

    Both deny tiers match TEXT, and they are handed the display title plus — for a
    shell tool — the raw ``command``. A file-search builtin has neither: its title is
    LLM-authored prose that need not name a path, and it carries no ``command``, so the
    root it walks and whether that walk is depth-capped reach no deny rule. This target
    is what a rule matches instead: ``"<prefix> path=… max_depth=…"`` over the scope
    arguments present, or ``""`` when the arguments are not search-shaped.

    Identification is by ARGUMENT SHAPE, never the title, for the same reason the
    sensitive-path keystone reads ``raw_params['path']``: the arguments are what the
    tool runs with. A ``command`` means a shell tool, already covered by the raw-command
    target.

    Every emitted value is encoded so it cannot forge a field (``_encode_search_field``);
    without that, model-authored text disarms the very rule shape this mechanism exists
    to serve.

    Residual limits, deliberately not closed here — this is a defense-in-depth layer
    over the always-on sensitive-path keystone, not a complete sandbox:
      * The recursive-``operation`` set is enumerated, so a tool that walks a tree under
        some other argument shape produces no target.
      * Only singular path spellings are read; a call passing a ``paths``/``files``
        sequence, or omitting the root entirely to walk the cwd, emits no ``path``
        field and a path-keyed rule does not see it.
      * Path normalization is lexical (see ``_normalize_search_path``): a symlink into a
        denied tree is not resolved, and a RELATIVE root stays relative, so no rule keyed
        on an absolute prefix matches it.
      * What is being searched FOR is never expressible in a rule, only where.
    """
    if not isinstance(raw_params, Mapping) or raw_params.get("command"):
        return ""
    if not _is_search_shaped(raw_params):
        return ""
    fields = [_SEARCH_DENY_PREFIX]
    for canonical, spellings in _SEARCH_DENY_ARG_KEYS:
        for key in spellings:
            value = raw_params.get(key)
            # ``bool`` is an ``int`` subclass; a boolean depth is meaningless and would
            # emit a field no rule can match sensibly.
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                continue
            text = str(value)
            if not text:
                continue
            if canonical == _SEARCH_PATH_FIELD:
                text = _normalize_search_path(text)
            fields.append(f"{canonical}={_encode_search_field(text)}")
            break
    return " ".join(fields)
