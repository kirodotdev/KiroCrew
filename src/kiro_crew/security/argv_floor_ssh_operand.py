"""Operand self-target analysis for the ssh floor, split out of argv_floor_ssh.py.

Split out to keep argv_floor_ssh.py under the 2000-line cap. This module holds the
operand-position self-target reader (``_operand_targets_self``) and the brace /
arithmetic expansion and run-time hostname-substitution helpers it uses. It is a
leaf of argv_floor_ssh.py: argv_floor_ssh.py re-exports these names, so every caller
keeps importing them from there. Behaviour is identical to before the move.
"""

from __future__ import annotations

import contextvars
import re

from .argv_floor_hosts import _host_is_self
from .shell_normalizer import _cut_at_operator, _resolve_param_defaults

# Operand spellings that resolve to this host's name at run time, matched as
# TEXT inside a single argv token.  The substitution forms are substring
# matches ("$(hostname -f)" still hits); the variable forms are exact or
# dot-suffixed ("$hostname", "$hostname.example") so an unrelated variable
# like "$hostname_backup" is NOT read as this host.  The variable forms
# include the Windows spellings of the same name: PowerShell's
# "$env:COMPUTERNAME" (bare and braced) and cmd's "%COMPUTERNAME%".
_HOSTNAME_SUBSTITUTION_HINTS: tuple[str, ...] = ("$(hostname", "`hostname")

_HOSTNAME_VARIABLE_FORMS: tuple[str, ...] = (
    "$hostname",
    "${hostname}",
    "$env:computername",
    "${env:computername}",
    "%computername%",
)

_EXPANSION_DEFAULT_RE = re.compile(r"\$\{[^{}:]*:[-=+?]([^{}]+)\}")

_ARITH_INT_LITERAL_RE = re.compile(r"\$\(\(\s*(-?(?:0[xX][0-9a-fA-F]+|[0-9]+))\s*\)\)")

_BRACE_GROUP_RE = re.compile(r"(?<!\$)\{([^{}]*)\}")

_BRACE_RANGE_RE = re.compile(r"\A(-?[0-9]+|[A-Za-z])\.\.(-?[0-9]+|[A-Za-z])(?:\.\.(-?[0-9]+))?\Z")

_BRACE_EXPANSION_CAP = 256


def _arith_int_literal_repl(match: "re.Match[str]") -> str:
    """The decimal spelling bash prints for one arithmetic integer literal."""
    lit = match.group(1)
    magnitude = lit.lstrip("-")
    try:
        if magnitude.lower().startswith("0x"):
            value = int(lit, 16)
        elif magnitude.startswith("0") and len(magnitude) > 1:
            # bash reads a leading zero as octal; an invalid octal digit is a
            # bash error (no command runs), so the spelling is left alone.
            value = int(lit, 8)
        else:
            value = int(lit, 10)
        # ``str`` stays INSIDE the try: hex/octal ``int`` uses a
        # power-of-two base exempt from the interpreter's digit cap, so a
        # ~3600-digit hex literal converts -- but its decimal ``str`` IS
        # capped and would raise straight through ``is_denied``, aborting
        # the evaluation.  Keeping the original spelling instead composes
        # with the target-position rule into a fail-closed deny.
        return str(value)
    except ValueError:
        return match.group(0)


def _brace_alternatives(body: str) -> "list[str] | None":
    """The words one brace-group *body* expands to, or None when literal.

    A comma body is an alternation (empty alternatives included, as in
    ``{h,}``); a ``a..b`` / ``a..b..step`` body is a numeric or single-char
    range.  Anything else — ``{single}``, an empty body — is not a brace
    expansion in bash and expands to nothing here.
    """
    range_match = _BRACE_RANGE_RE.match(body)
    if range_match is not None:
        start_s, end_s, step_s = range_match.groups()
        if start_s.isalpha() != end_s.isalpha():
            return None
        try:
            step = abs(int(step_s)) if step_s else 1
            if step == 0:
                step = 1
            if start_s.isalpha():
                start, end = ord(start_s), ord(end_s)
                render = chr
            else:
                start, end = int(start_s), int(end_s)
                render = str  # type: ignore[assignment]
        except ValueError:
            # ``int()`` refuses digit strings past the interpreter's
            # conversion cap (~4300 digits); uncaught, that crashes the
            # gate.  An endpoint or step that large cannot name a single
            # legitimate target, so signal overflow exactly like the cap
            # check below -- the caller converts it to a fail-closed deny.
            return [""] * (_BRACE_EXPANSION_CAP + 1)
        if abs(end - start) // step + 1 > _BRACE_EXPANSION_CAP:
            # Signal overflow with an over-long list the caller's cap check
            # converts to a fail-closed deny.
            return [""] * (_BRACE_EXPANSION_CAP + 1)
        direction = 1 if start <= end else -1
        return [render(v) for v in range(start, end + direction, direction * step)]
    if "," in body:
        return body.split(",")
    return None


def _brace_expansions(token: str) -> "list[str] | None":
    """Every word bash brace expansion produces for *token*; None on overflow.

    Groups are expanded left-to-right to a fixpoint (each rewrite removes one
    brace pair, so this terminates); the returned words carry no expandable
    group.  A product beyond ``_BRACE_EXPANSION_CAP`` returns None and the
    caller fails closed.
    """
    words = [token]
    changed = True
    while changed:
        changed = False
        next_words: list[str] = []
        for word in words:
            expanded = False
            for group in _BRACE_GROUP_RE.finditer(word):
                alternatives = _brace_alternatives(group.group(1))
                if alternatives is None:
                    continue
                head, tail = word[: group.start()], word[group.end() :]
                next_words.extend(head + alt + tail for alt in alternatives)
                expanded = True
                changed = True
                break
            if not expanded:
                next_words.append(word)
            if len(next_words) > _BRACE_EXPANSION_CAP:
                return None
        words = next_words
    return words


_LITERAL_HOSTS = contextvars.ContextVar("ssh_floor_literal_hosts", default=False)

# Set while the git floor reads a remote word (``_git_remote_targets_self``). A git
# word is also a refspec or a remote name, so a Windows own-name counts there only
# in clear transport syntax: a URL authority or after real ``user@`` userinfo.
_GIT_WORD = contextvars.ContextVar("ssh_floor_git_word", default=False)


_URI_SCHEMES: tuple[str, ...] = ("ssh://", "scp://", "sftp://", "rsync://")


def _windows_name_is_host(t: str, *, host_position: bool, file_operand: bool) -> bool:
    """True if a Windows own-name spelling sits where *t* names a host.

    It counts whole (``$env:computername``), after userinfo (``user@``, and
    after the LAST ``@`` when userinfo holds a colon: ``user:pw@``), as a URI
    authority (``ssh://[user@]host[:port]``), before a path colon, or dotted in
    host position (``$env:computername.corp``).  A dotted word elsewhere is a
    local file (``%computername%.log``); an ``@`` after a ``:`` or ``/`` is
    remote-path data (``backup:/logs/a@%computername%.log``); and a
    *file_operand* (an scp/rsync file, a git or tar remote word) with no host
    colon is a local path or a name (``scp -r %computername% far:``).  In a git
    word (``_GIT_WORD``) only clear transport syntax counts: a URL authority or
    scp-like ``user@host:path``, never a refspec or a remote name.
    """
    # PowerShell's ``$env:`` colon is part of the variable, never a host colon.
    if file_operand and ":" not in t.replace("env:", ""):
        return False
    git_word = _GIT_WORD.get()
    spellings = [] if git_word else [t]
    user, at, rest = t.partition("@")
    if at and not {":", "/"} & set(user.replace("env:", "")):
        spellings.append(rest)
    if at and "/" not in t and not file_operand and not git_word:
        # Only an ssh/sftp destination reads its host after the LAST ``@``; a
        # file operand's host ends at its first colon (``far:job@x`` is far).
        spellings.append(t.rsplit("@", 1)[1])
    authority = ""
    for scheme in _URI_SCHEMES:
        if t.startswith(scheme):
            authority = t[len(scheme) :].split("/", 1)[0].rsplit("@", 1)[-1]
    for var in _HOSTNAME_VARIABLE_FORMS:
        if "hostname" in var:
            continue
        if authority.startswith(var) and authority[len(var) : len(var) + 1] in ("", ":", "."):
            return True
        for spelling in spellings:
            if not spelling.startswith(var):
                continue
            tail = spelling[len(var) :]
            if (tail == "" and not git_word) or tail.startswith(":"):
                return True
            if tail.startswith(".") and (host_position or ":" in tail):
                return True
    return False


def _operand_targets_self(
    operand: str,
    *,
    host_position: bool = True,
    dns: bool = True,
    match_globs: bool = True,
    file_operand: bool = False,
) -> bool:
    """True if *operand* names THIS machine as a connection target.

    ``file_operand=True`` marks an scp/rsync file operand or a git/tar remote
    word, which names no host unless it carries a ``host:`` colon (applied to
    the Windows own-name spellings only, see ``_windows_name_is_host``).

    ``dns=False`` passes ``dns_fallback=False`` to every host check (no
    hosts-file or DNS alias verdict, no unread-address-table window), for a
    word that is not itself the destination (see
    ``_command_value_names_self_endpoint``).  ``match_globs=False`` passes
    the same to every host check, for an operand of a bare glob verb (see
    ``_is_ssh_to_self``).

    Resolves the host part the way the ssh family reads an operand: an
    optional ``ssh://``/``scp://``/``sftp://``/``rsync://`` scheme (authority
    isolated before any path), an optional ``user@`` prefix stripped from the
    HOST part only (an ``@`` in a remote PATH is not userinfo), a bracketed
    IPv6 literal, a bare IPv6/numeric IP literal, and a ``host:path`` colon
    form.  A bare word equal to a self-host name also counts: for ssh/sftp it
    IS the positional target, and for scp/rsync a local file named exactly
    ``localhost`` is not worth carving an allowance for (over-blocking is the
    safer direction, and the catalog pattern must stay a SUBSET of this
    predicate — see ``test_retained_pattern_is_a_subset_of_its_predicate``).

    Every statically-derivable expansion/gluing form is resolved over the
    WHOLE operand and the resolved spelling re-checked (round-10 invariant):
    parameter defaults (``local${U:-host}`` -> ``localhost``), brace
    expansion (``local{h,}ost``), and single-integer arithmetic literals
    (``$((0x7f)).0.0.1``).  Each resolution only WIDENS the deny.  Forms
    whose produced text depends on runtime state — a bare ``$VAR`` value,
    command substitution with output (``$(get-host)``), arithmetic beyond
    one literal (``$((i+1))``) — are the documented run-time residual class:
    emulating them needs an in-floor evaluator that is itself attack
    surface, and their statically-visible parts are already checked (the
    embedded defaults here, the ``$(hostname`` hints, the empty-substitution
    collapse in ``_is_ssh_to_self``).
    """
    t = operand.strip().strip("\"'")
    if not t or t.startswith("-"):
        return False
    dns = dns and not _LITERAL_HOSTS.get()
    # A glued shell operator (``ssh localhost;true`` hands the operand
    # ``localhost;true``) is never part of a hostname, so consult the
    # operator-cut spelling too -- the same ``(token, _cut_at_operator(token))``
    # idiom the git-push floor uses.  This only WIDENS the deny: the cut token
    # has no operator left, so the recursive call cuts nothing and does not
    # recurse again, and a bare ``$VAR`` (no operator to cut) is unaffected.
    cut = _cut_at_operator(t)
    if cut != t and _operand_targets_self(
        cut,
        host_position=host_position,
        dns=dns,
        match_globs=match_globs,
        file_operand=file_operand,
    ):
        return True
    for hint in _HOSTNAME_SUBSTITUTION_HINTS:
        if hint in t:
            return True
    for var in _HOSTNAME_VARIABLE_FORMS:
        if "hostname" in var and (t == var or t.startswith(var + ".") or t.startswith(var + ":")):
            return True
    if _windows_name_is_host(t, host_position=host_position, file_operand=file_operand):
        return True
    # An expansion's embedded default is the destination when the variable is
    # unset — check every ``${…:-word}``-style word (recursion terminates:
    # the word is strictly shorter than the operand).  Kept alongside the
    # whole-operand resolution below because it is WIDER on mangled glue
    # (``foo${X:-localhost}bar``): the embedded word alone is self even when
    # the resolved whole is not.
    for match in _EXPANSION_DEFAULT_RE.finditer(t):
        if _operand_targets_self(
            match.group(1),
            host_position=host_position,
            dns=dns,
            match_globs=match_globs,
            file_operand=file_operand,
        ):
            return True
    # Bash substitutes a parameter default INTO the surrounding
    # word (``local${KC_UNSET:-host}`` -> ``localhost``), so the whole
    # operand is resolved and re-checked — checking each default word in
    # isolation misses the reconstruction.  ``_resolve_param_defaults``
    # substitutes every ``${VAR<op>literal}`` with its literal to a fixpoint
    # (nesting and colon-less operators included); the recursive call sees a
    # string with no such form left, so it does not recurse here again.
    resolved = _resolve_param_defaults(t)
    if resolved != t and _operand_targets_self(
        resolved,
        host_position=host_position,
        dns=dns,
        match_globs=match_globs,
        file_operand=file_operand,
    ):
        return True
    # A single integer literal inside arithmetic expansion is
    # printed decimally by bash (``$((0x7f))`` -> ``127``), gluing into the
    # surrounding word.  Only the literal spellings are normalized — an
    # expression or a variable stays unresolved (run-time residual, see the
    # docstring) — and the substituted string carries no ``$((…))`` literal,
    # so the recursion is a single level.
    normalized = _ARITH_INT_LITERAL_RE.sub(_arith_int_literal_repl, t)
    if normalized != t and _operand_targets_self(
        normalized,
        host_position=host_position,
        dns=dns,
        match_globs=match_globs,
        file_operand=file_operand,
    ):
        return True
    # An arithmetic expansion that SURVIVED normalization is an
    # expression (``$((0+1))``) whose produced text only the shell knows.
    # In a connection-target position that unknown glues into the host
    # (``127.0.0.$((0+1))`` -> ``127.0.0.1``), so fail closed.  For the
    # ``host:path`` colon form only the PRE-COLON host part can name this
    # machine: arithmetic after the colon glues into a remote
    # filename (``far:/backups/part$((i)).tar``), so it stays allowed.
    if "$((" in normalized and (
        host_position or (":" in normalized and "$((" in normalized.split(":", 1)[0])
    ):
        return True
    # Brace expansion splices each alternative into the
    # surrounding word BEFORE every other expansion (``local{h,}ost`` ->
    # ``localhost``), so each choice is re-checked.  ``None`` means the
    # fan-out exceeded the cap: fail closed — no legitimate single-target
    # connection operand needs a >256-way product, and over-blocking is this
    # floor's safe direction.
    choices = _brace_expansions(t)
    if choices is None:
        return True
    for choice in choices:
        if choice != t and _operand_targets_self(
            choice,
            host_position=host_position,
            dns=dns,
            match_globs=match_globs,
            file_operand=file_operand,
        ):
            return True
    scheme_seen = False
    for scheme in _URI_SCHEMES:
        if t.startswith(scheme):
            # Isolate the URI authority before any path, so an ``@`` or ``:``
            # in the remote path cannot masquerade as userinfo or a port.
            t = t[len(scheme) :].split("/", 1)[0]
            scheme_seen = True
            break
    # A bare loopback/IP literal is a host even when it CONTAINS colons
    # (``::1``, ``::ffff:127.0.0.1``): check the whole token before
    # colon-splitting, which would read an IPv6 literal as an empty host.
    # The DNS layer applies to the WHOLE token only when the token sits in
    # host position (or a scheme marked it as an authority) -- a plain word
    # here may be a local filename.
    if _host_is_self(
        t, dns_fallback=dns and (host_position or scheme_seen), match_globs=match_globs
    ):
        return True
    if t.startswith("[") or "@[" in t:
        # Bracketed IPv6, optionally behind userinfo: user@[::1]:path
        host = t.split("[", 1)[1].partition("]")[0]
    else:
        # host[:path] first, THEN userinfo — in that order, so an ``@`` in
        # the path part (``localhost:/tmp/a@b``) is never taken as userinfo.
        pre_colon = t.partition(":")[0]
        if "@" in pre_colon:
            # An ``@`` BEFORE the first colon is real userinfo.  Check the
            # whole remainder after it as a bare host first: ``user@::1``
            # names ``::1``, which the plain colon split below would misread
            # as an empty host.  Userinfo marks the remainder as a host.
            after_userinfo = t[len(pre_colon.rsplit("@", 1)[0]) + 1 :]
            if _host_is_self(after_userinfo, dns_fallback=dns, match_globs=match_globs):
                return True
        host = pre_colon
        if "@" in host:
            host = host.rsplit("@", 1)[1]
    # An isolated host part (a scheme authority, a bracketed literal, a
    # ``host:path`` prefix, a userinfo remainder) IS a host wherever the
    # operand sits, so the DNS layer applies; an operand isolation did not
    # shorten is a bare word -- a host only in host position.
    if "@" in t and (scheme_seen or "/" not in t):
        # Userinfo may CONTAIN a colon (``user:pass@host``): the colon-first
        # split above then reads the host as ``user``.  OpenSSH resolves the
        # destination AFTER the LAST ``@`` -- in a URI authority (its path is
        # already split off) and in the plain ``[user@]host`` form (which has
        # no path).  A token with a ``/`` outside a scheme keeps the colon-first
        # order: its ``@`` is path data (``far:/backup/a@b``).
        tail = t.rsplit("@", 1)[1]
        tail_dns = dns and (host_position or scheme_seen)
        if _host_is_self(tail, dns_fallback=tail_dns, match_globs=match_globs):
            return True
        tail_host = tail.partition(":")[0]
        if tail_host != tail and _host_is_self(
            tail_host, dns_fallback=tail_dns, match_globs=match_globs
        ):
            return True
    return _host_is_self(
        host,
        dns_fallback=dns and (host_position or scheme_seen or host != t),
        match_globs=match_globs,
    )
