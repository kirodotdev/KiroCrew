"""The gate's half of a KAS spec grant that holds only while Crew checks its denies.

``ToolCall.spec_denied_grant`` (:mod:`kiro_crew.platform.spec_denied_grant`)
carries grants a spec's ``deniedCommands`` / write ``deniedPaths`` held back, with
those denies. :func:`spec_denies_refusal` backs the ``spec-denies`` deny row: it refuses
a call a deny names, on every approval policy. :func:`denied_grant_applies` is
asked by the ``spec-builtin-grant`` row: it grants only a call a deny check
cleared, and treats every unknown (a command it cannot split, a target it cannot
read or resolve) as no grant, so the call asks. A ``web_fetch`` is refused only
when a plain-URL ``web_fetch.blocked`` entry definitely matches it, and granted by
a plain ``web_fetch.trusted`` URL, or by name when no blocked entry can reach it
(:func:`url_literal`, :func:`web_fetch_blocked_match`).
"""

from __future__ import annotations

import ipaddress
import os
import re
import string
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import unquote, unquote_plus

from kiro_crew.platform import allowed_commands, allowed_paths
from kiro_crew.platform.tool_paths import edit_target_candidates, is_edit_call

if TYPE_CHECKING:
    from kiro_crew.hooks import ToolCall
    from kiro_crew.platform.spec_denied_grant import SpecDeniedGrant

#: kiro-cli's shell and write names, as :func:`gate_tiers._spec_builtin_names` gives them.
_SHELL_NAMES = frozenset({"execute_bash", "execute_pwsh"})
_WRITE_NAMES = frozenset({"fs_write", "fs_append", "str_replace"})
_WEB_FETCH = "web_fetch"


def _path_denied(target: str, patterns: Sequence[str], cwd: str) -> bool | None:
    """Whether a deny in *patterns* covers the write *target*, or ``None`` (unknown).

    Read wider than :func:`gate_tiers._write_target_uncovered` reads an allowlist:
    the target is denied when ANY of its spellings (as written, normalized, or
    symlink-resolved) is covered, or covered by an entry whose directory part is
    resolved through a link. A relative target with no session directory, or a
    resolution that stalls or fails, is unknown.
    """
    from kiro_crew.security.paths_resolver import PathResolutionStalled, _resolved_forms_bounded

    expanded = os.path.expanduser(target)
    if not os.path.isabs(expanded):
        if not os.path.isabs(cwd):
            return None
        expanded = os.path.join(cwd, expanded)
    try:
        resolved = _resolved_forms_bounded(expanded)
    except PathResolutionStalled:
        return None
    if not resolved:
        return None
    forms = [os.path.normpath(expanded), *sorted(resolved)]
    every: list[str] = list(patterns)
    for pattern in patterns:
        prefix = allowed_paths.literal_prefix_dir(pattern)
        try:
            reals = _resolved_forms_bounded(prefix)
        except PathResolutionStalled:
            return None
        rest = pattern[len(prefix) :].lstrip("/")
        for real in reals:
            real = allowed_paths.to_glob_path(real)
            if real == prefix or not allowed_paths.is_absolute(real):
                continue
            twin = allowed_paths.anchor(allowed_paths.join(real, rest) if rest else real, "", "")
            if twin is not None:
                every.append(twin)
    return any(allowed_paths.covered(every, form) for form in forms)


def _write_denied(call: ToolCall, grant: SpecDeniedGrant) -> bool | None:
    """Whether a write deny covers any target of *call*, or ``None`` (unknown)."""
    candidates = edit_target_candidates(call.raw_params, call.diff_path, tool_kind=call.kind)
    if candidates.truncated or candidates.unanchored or not candidates:
        return None
    answers = [_path_denied(t, grant.denied_write_paths, grant.cwd) for t in candidates]
    if any(a is True for a in answers):
        return True
    return None if any(a is None for a in answers) else False


def _is_write(call: ToolCall, names: tuple[str, ...]) -> bool:
    return is_edit_call(call.kind, call.diff_path) or bool(_WRITE_NAMES.intersection(names))


def shell_denies_match(patterns: tuple[str, ...], command: str | None) -> bool | None:
    """The refusal row's shell judgement: match, definite no-match, or unknown.

    A redirect or unsplittable command is unknown. A ``#`` or ``[[`` in a chain
    can change which pieces execute, so those chains are unknown too. A match
    wins over an unknown piece, preserving the ordinary session's refusal.
    """
    pieces = allowed_commands.chain_commands(command) or ()
    if not pieces or (len(pieces) > 1 and ("#" in str(command) or "[[" in str(command))):
        return None
    unknown = False
    for piece in pieces:
        answer = allowed_commands.denied_match(patterns, piece, word_bounded=True)
        if answer is True:
            return True
        unknown = unknown or answer is None
    return None if unknown else False


def spec_denies_refusal(call: ToolCall, names: tuple[str, ...]) -> str | None:
    """Why the ``spec-denies`` row refuses *call*, or ``None``: its spec's checked
    ``deniedCommands`` or write ``deniedPaths`` names it. A ``deny_policy``: the
    refusal is the spec's state, as for ``spec-shell-allowlist``.

    A chain is judged one command at a time (``allowed_commands.chain_commands``)
    and refused when a deny names any of them. A command that does not split there
    (a quote, ``#`` or ``[[`` in a chain, a redirect, ``$(...)``, a backtick)
    is not refused here."""
    grant = call.spec_denied_grant
    if grant is None or call.mcp_server:
        return None
    prefix = "Blocked by this agent's spec: "
    if grant.denied_commands and call.is_shell and _SHELL_NAMES.intersection(names):
        if shell_denies_match(grant.denied_commands, call.command) is True:
            return prefix + "the command matches its deniedCommands"
    if grant.denied_write_paths and _is_write(call, names):
        if _write_denied(call, grant) is True:
            return prefix + "the write target is in its deniedPaths"
    if grant.web_fetch_blocked and _WEB_FETCH in names:
        if web_fetch_blocked_match(call, grant) is True:
            return prefix + "the URL matches its web_fetch.blocked"
    return None


#: Characters that make a kiro-cli regex more than a literal.
_URL_REGEX_SYNTAX = frozenset(".^$*+?()[]{}|\\")


def url_literal(raw: object) -> tuple[str, bool] | None:
    """A ``web_fetch.blocked`` / ``trusted`` entry as ``(literal, is_prefix)``, or
    ``None`` when it is not a plain URL or a plain URL followed by ``.*``.

    kiro-cli anchors an entry with ``^`` and ``$`` unless it already starts or ends
    with one, then matches the whole URL. Read here with string work only, never
    as a regex: an optional leading ``^`` or ``\\A``, an optional trailing ``$`` or
    ``\\z``, then literal characters (a backslash before ASCII punctuation is that
    character), and at most a final ``.*``. ``\\<`` / ``\\>`` (word boundaries in
    Rust) and a trailing ``\\$`` (which kiro-cli leaves unanchored) are refused.
    """
    if not isinstance(raw, str) or not raw:
        return None
    body = raw.removeprefix("\\A") if raw.startswith("\\A") else raw.removeprefix("^")
    if body.endswith("\\$"):
        return None
    if body.endswith("\\z") and not body.endswith("\\\\z"):
        body = body[:-2]
    elif body.endswith("$"):
        body = body[:-1]
    prefix = body.endswith(".*") and not body.endswith("\\.*")
    if prefix:
        body = body[:-2]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if not (nxt.isascii() and not nxt.isalnum() and nxt.isprintable()) or nxt in "<> ":
                return None
            out.append(nxt)
            i += 2
        elif ch in _URL_REGEX_SYNTAX:
            return None
        else:
            out.append(ch)
            i += 1
    return "".join(out), prefix


def _url_matches(entry: tuple[str, bool], url: str) -> bool:
    literal, prefix = entry
    return url.startswith(literal) if prefix else url == literal


_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_HEX = frozenset(string.hexdigits)
_HOST_NAME = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*")
_DEFAULT_PORTS = {"http": "80", "https": "443"}
_URL_SEP = "/"  # a URL path separator, never a file path one


#: How a server may read a run of ``/`` in the path: kept, collapsed before dot
#: segments are removed, or collapsed after (the fetch client removes them first).
_COLLAPSE_MODES = ("", "before", "after")


def canonical_url(text: str, *, prefix: bool = False, collapse: str = "") -> str | None:
    """*text* as an ``http(s)`` fetch resolves it, or ``None`` when that is unsure.

    Lower-case scheme and host, no trailing host dot, no default port, ``%XX`` of an
    unreserved character decoded (other escapes upper-cased), dot segments removed,
    an empty path read as ``/``, no fragment. With *collapse* ``"before"`` or
    ``"after"``, runs of ``/`` in the path are read as one before or after dot
    segments are removed, as a server that collapses them may read it. Refused:
    userinfo, non-ASCII, whitespace, a backslash, a bad escape or port, a numeric
    host that is not a plain dotted quad, and (outside *prefix*) a ``[...]`` address
    literal. With *prefix*, *text* is the start of a URL (a ``blocked`` entry before
    its ``.*``); its form may only be wider, so a cut escape or a final ``.``/``..``
    segment is refused rather than guessed, and a cut decimal host (``http://10.``)
    is kept as written.
    """
    if not text.isascii() or any(ch <= " " or ch in "\\\x7f" for ch in text):
        return None
    scheme, sep, rest = text.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in _DEFAULT_PORTS:
        return None
    cut = min((i for i in map(rest.find, "/?#") if i >= 0), default=len(rest))
    authority, tail = rest[:cut], rest[cut:]
    if "@" in authority or "%" in authority:
        return None
    if authority.startswith("["):
        # An address literal is never granted; a blocked one is compared in its
        # compressed form.
        inner, close, port = authority[1:].partition("]")
        if not prefix or not close or (port and not port.startswith(":")):
            return None
        try:
            host = f"[{ipaddress.IPv6Address(inner).compressed}]"
        except ValueError:
            return None
        port = port[1:]
    else:
        host, _, port = authority.partition(":")
        host = host.lower().removesuffix(".")
        if not _HOST_NAME.fullmatch(host) and not (prefix and host == ""):
            return None
        last = host.rsplit(".", 1)[-1]
        if last.isdigit() or last.startswith("0x"):
            # A cut prefix host with fewer than four parts (`http://127.1`) can be
            # a short address spelling as well as the start of a longer one.
            whole = not prefix or cut < len(rest)
            if not whole and host.count(".") != 3:
                # The written start of a decimal dotted quad that ends on a dot
                # (`http://10.`) still names every granted URL under it: a granted
                # numeric host is a plain quad. Without the dot (`http://127.1`) it
                # may be a whole short spelling; an octal or hex label names
                # another address.
                labels = authority.removesuffix(".").split(".")
                if (
                    port
                    or not authority.endswith(".")
                    or not all(lab.isdigit() and str(int(lab)) == lab for lab in labels)
                ):
                    return None
                return f"{scheme}://{authority}"
            try:
                if str(ipaddress.IPv4Address(host)) != host:
                    return None
            except ValueError:
                return None
    if port and not (port.isdigit() and int(port) <= 65535):
        return None
    port = str(int(port)) if port else ""
    netloc = host if port in ("", _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    tail = tail.partition("#")[0]  # a fetch never sends the fragment
    out: list[str] = []
    i = 0
    while i < len(tail):
        if tail[i] != "%":
            out.append(tail[i])
            i += 1
            continue
        code = tail[i + 1 : i + 3]
        if len(code) != 2 or not _HEX.issuperset(code):
            return None
        ch = chr(int(code, 16))
        out.append(ch if ch in _UNRESERVED else "%" + code.upper())
        i += 3
    tail = "".join(out)
    end = tail.find("?") if "?" in tail else len(tail)
    path, after = tail[:end], tail[end:]
    if collapse == "before":
        path = re.sub("/{2,}", "/", path)
    segments = path.split(_URL_SEP)[1:]
    if prefix and not after and segments and segments[-1] in (".", ".."):
        return None
    kept: list[str] = []
    for seg in segments:
        if seg == "..":
            if kept:
                kept.pop()
        elif seg != ".":
            kept.append(seg)
    if segments and segments[-1] in (".", ".."):
        kept.append("")
    if path or not prefix or after:
        path = "/" + "/".join(kept)
    if collapse == "after":
        path = re.sub("/{2,}", "/", path)
    return f"{scheme}://{netloc}{path}{after}"


def _blocked_hits(entry: tuple[str, bool] | None, urls: Sequence[str]) -> bool:
    """Whether a ``blocked`` entry withholds the grant: unread, or matching any of
    the URL's *urls* forms, read as written or canonical (with or without empty
    path segments collapsed)."""
    if entry is None:
        return True
    literal, prefix = entry
    scheme, sep, _ = literal.partition("://")
    if sep and scheme.lower() not in _DEFAULT_PORTS:
        return False  # another scheme's entry never names an http(s) URL
    forms = [canonical_url(literal, prefix=prefix, collapse=c) for c in _COLLAPSE_MODES]
    read = [form for form in forms if form is not None]
    if len(read) != len(_COLLAPSE_MODES):
        return True
    literals = [literal, *read, *(text for form in read for text in _query_decoded(form))]
    return any(_url_matches((lit, prefix), u) for lit in literals for u in urls)


def _escape_unsafe(canonical: str) -> bool:
    """Whether a canonical URL keeps an escape a server may read as another URL:
    any escape in the path (``%2F``), one of a non-ASCII byte anywhere, or an
    escaped ``%`` (``%2573``, which a second decode reads as ``s``)."""
    path = canonical.partition("?")[0]
    codes = [int(code[:2], 16) for code in canonical.split("%")[1:]]
    return "%" in path or any(code >= 0x80 or code == 0x25 for code in codes)


def _query_decoded(canonical: str) -> tuple[str, ...]:
    """*canonical* with its query fully unescaped, as a server's query parser reads
    it (``+`` kept, and ``+`` read as a space); a URL and a ``blocked`` entry are both
    compared in these forms too, so they can only withhold a grant."""
    head, sep, query = canonical.partition("?")
    if not sep:
        return ()
    return tuple(f"{head}{sep}{decode(query)}" for decode in (unquote, unquote_plus))


def _fetch_url(call: ToolCall) -> str | None:
    """The ``web_fetch`` call's ``url`` as written, or ``None`` when it is not a
    non-empty string."""
    url = call.raw_params.get("url") if isinstance(call.raw_params, dict) else None
    return url if isinstance(url, str) and url else None


def _clear_forms(url: str) -> tuple[list[str], list[str]] | None:
    """``(canonical forms, every form)`` of *url* a ``blocked`` entry is compared
    with, or ``None`` when the URL is unsure: a control character (Rust's ``.`` does
    not match a newline), no canonical form in some collapse mode, or an escape
    :func:`_escape_unsafe` names. *every form* is the URL as written, its canonical
    forms and their query-decoded forms."""
    if any(ch < " " or ch == "\x7f" for ch in url):
        return None
    canonicals = [canonical_url(url, collapse=c) for c in _COLLAPSE_MODES]
    if any(form is None or _escape_unsafe(form) for form in canonicals):
        return None
    forms = [form for form in canonicals if form is not None]
    return forms, [url, *forms, *(text for form in forms for text in _query_decoded(form))]


def _kiro_cli_matches(entry: tuple[str, bool], url: str) -> bool:
    """kiro-cli's own anchored, case-sensitive whole-URL test of one entry: a
    prefix entry's ``.*`` does not cross a newline."""
    literal, prefix = entry
    if prefix:
        return url.startswith(literal) and "\n" not in url[len(literal) :]
    return url == literal


def _definitely_blocked(entry: tuple[str, bool], url: str) -> bool:
    """Whether the read ``blocked`` *entry* matches *url* as kiro-cli reads it, or in
    the single canonical form of both with no ``/`` collapse. An entry with a ``#``,
    or a prefix cut before its path (``https://evil\\..*``), is compared as written
    only: its canonical form drops the fragment, a trailing host dot or a default
    port, so it would name more URLs than the entry does."""
    if _kiro_cli_matches(entry, url):
        return True
    literal, prefix = entry
    if "#" in literal or (prefix and _URL_SEP not in literal.partition("://")[2]):
        return False
    form = canonical_url(literal, prefix=prefix, collapse="")
    target = canonical_url(url, collapse="")
    return form is not None and target is not None and _url_matches((form, prefix), target)


def web_fetch_blocked_match(call: ToolCall, grant: SpecDeniedGrant) -> bool | None:
    """The one judge of a ``web_fetch`` against the record's ``blocked`` entries.

    ``True`` (the ``spec-denies`` row refuses): an entry :func:`url_literal` reads
    matches the URL by :func:`_definitely_blocked`. ``False`` (clear, so the by-name
    grant may answer): the URL has every :func:`_clear_forms` form and no entry hits
    it in any of them (:func:`_blocked_hits`, which counts an unsure entry as a hit).
    ``None`` otherwise: the call keeps asking. The collapse modes, the
    query-decoded forms and an entry with no canonical form only ever withhold."""
    url = _fetch_url(call)
    if url is None:
        return None
    blocked = [url_literal(raw) for raw in grant.web_fetch_blocked]
    if any(entry is not None and _definitely_blocked(entry, url) for entry in blocked):
        return True
    read = _clear_forms(url)
    named = [entry for entry in blocked if entry is None or _may_start_http_url(entry)]
    if read is None or any(_blocked_hits(entry, read[1]) for entry in named):
        return None
    return False


def _may_start_http_url(entry: tuple[str, bool]) -> bool:
    """Whether a read ``blocked`` entry can match an ``http(s)`` URL at all: one with
    no ``://`` (a host entry KAS already denies) only when it is a prefix of one."""
    literal, prefix = entry
    if "://" in literal:
        return True
    low = literal.lower()
    return prefix and ("http://".startswith(low) or "https://".startswith(low))


def _web_fetch_granted(call: ToolCall, grant: SpecDeniedGrant) -> bool:
    """Whether a ``web_fetch`` call is granted: only by a ``trusted`` entry that is a
    plain URL (equal) or a plain URL plus ``.*`` (prefix), with every ``blocked``
    entry of that shape and not matching. Anything else keeps the prompt KAS asked
    for. A URL with a control character is never granted: Rust's ``.`` does not
    match a newline, so a prefix test would read the entry wider than kiro-cli.

    The URL is also compared in its :func:`canonical_url` form, so a spelling the
    fetch resolves elsewhere (an escape, a case, a default port, a dot segment,
    userinfo) neither slips past ``blocked`` nor reaches out of ``trusted``. An empty
    path segment is read each way a server may read it (kept, or collapsed before
    or after dot segments), and the
    query also fully unescaped for ``blocked``. A URL with no canonical form, or one
    that keeps an escape :func:`_escape_unsafe` names, is never granted."""
    if not grant.web_fetch_blocked_complete or not grant.web_fetch_trusted:
        return False
    url = _fetch_url(call)
    read = _clear_forms(url) if url is not None else None
    if url is None or read is None:
        return False
    forms, seen = read
    blocked = [url_literal(raw) for raw in grant.web_fetch_blocked]
    trusted = [url_literal(raw) for raw in grant.web_fetch_trusted]
    if any(_blocked_hits(entry, seen) for entry in blocked):
        return False
    return any(
        entry is not None
        and _url_matches(entry, url)
        and (
            not entry[1]
            or all(
                _canonical_prefix_holds(entry[0], form, collapse)
                for form, collapse in zip(forms, _COLLAPSE_MODES, strict=True)
            )
        )
        for entry in trusted
    )


def _canonical_prefix_holds(literal: str, canonical: str, collapse: str = "") -> bool:
    """Whether a ``trusted`` prefix still holds for the canonical URL (read with the
    same *collapse*); a prefix cut before ``://`` (``.*``, ``http``) is compared as
    written."""
    form = canonical_url(literal, prefix=True, collapse=collapse) if "://" in literal else literal
    if form is not None and form.endswith(".") and "/" not in form.partition("://")[2]:
        return False  # a cut host (`http://10.`) also starts a DNS name
    return form is not None and canonical.startswith(form)


def denied_grant_applies(call: ToolCall, names: tuple[str, ...]) -> bool:
    """Whether *call*'s ``spec_denied_grant`` grants it: a name it lifted (or a
    command one of its ``allowedCommands`` matches), and every deny that applies to
    the call checked and clear. Anything unknown answers False."""
    grant = call.spec_denied_grant
    if grant is None or call.mcp_server:
        return False
    shell = call.is_shell and bool(_SHELL_NAMES.intersection(names))
    by_name = bool(grant.builtins.intersection(names))
    if _WEB_FETCH in names:
        # By name only when the URL is clear of every readable blocked entry; the
        # projection lifts it only where its asks alone held the fetch back.
        return _web_fetch_granted(call, grant) or (
            _WEB_FETCH in grant.builtins and web_fetch_blocked_match(call, grant) is False
        )
    by_command = (
        shell
        and allowed_commands.plain_command(call.command)
        and allowed_commands.matches_any(grant.commands, str(call.command))
    )
    if not (by_name or by_command):
        return False
    if _SHELL_NAMES.intersection(names) and not shell:
        return False
    if shell and grant.denied_commands:
        if allowed_commands.denied_match(grant.denied_commands, call.command) is not False:
            return False
    if _is_write(call, names) and grant.denied_write_paths:
        if _write_denied(call, grant) is not False:
            return False
    return True
