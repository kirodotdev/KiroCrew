"""The ssh self-target floor: an ssh-family connection back into this same host is refused.

The agent's shell runs inside the sandbox and sshd does not, so a connection whose
target is this machine re-enters it outside every control here. The floor reads an
ssh-family command line (``ssh``, ``scp``, ``sftp``, ``rsync``, a binary copied under a
new name, and the remote-shell values those verbs exec) in operand position -- behind
options, redirects, a ``user@`` prefix, URI schemes, quoting, brace and arithmetic
expansions, same-line assignments and one-level function calls -- and asks
``argv_floor_hosts`` whether each destination names this host. The option tables, the
quoted-separator masking that keeps a quoted ``;`` from ending the walk, and the
section comment naming the floor's residuals live here too. A new ssh-family spelling
or option goes here.

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`). This module reads the frame walk
in ``argv_floor_self`` and the host verdicts in ``argv_floor_hosts``, both below it.
"""

from __future__ import annotations

import fnmatch
import re

# The reader names a test instruments on the facade are reached as attributes of
# the reader module rather than bound here at import: the facade mirrors a patch
# onto the owning module's namespace, so a name bound into THIS namespace would
# keep resolving the unpatched object and the instrument would count nothing.
from . import shell_normalizer as _shell_normalizer
from .argv_floor_hosts import _GLOB_CHARS, _host_is_self
from .argv_floor_self import _self_token_frames, _static_substitution_output
from .shell_normalizer import (
    _argv_programs,
    _cut_at_operator,
    _data_consumer_exempt,
    _decode_shell_quoted_literals,
    _ends_argv,
    _program_basename,
    _resolve_param_defaults,
    _strip_redirect,
    _substitution_depth_delta,
    _xargs_here_string_rebuild,
)

# ── Sandbox-escape floor (ssh back into this same host) ──
# The agent's shell runs inside a sandbox; sshd does not.  A connection whose
# TARGET is this same machine re-enters it outside every control in this
# module, so the floor resolves the target in OPERAND POSITION — behind
# options (``ssh -p 22 localhost``), redirects, a ``user@`` prefix, URI
# schemes, bracketed/bare IPv6 and numeric IPv4 literals, and quoting the
# raw-text regex cannot see through — and refuses it when it names this host.
#
# Named residuals, fail-open BY DESIGN (failing closed on any of these would
# deny every legitimate remote ssh, which this box's workflows depend on):
# config-level routing that needs ssh's own config resolution (``-F`` files,
# ssh_config Host aliases), and a substitution that only resolves to this
# host at run time (``$(some-command)``).  The literal
# ``hostname``-substitution spellings and ``-o hostname=``/``-o proxyjump=``
# values ARE checked below, as text, and a ``-o proxycommand=`` value is
# checked twice: the floor recurses on it as a command line AND
# scans it for a LITERAL self endpoint (``nc 127.0.0.1 22`` relays the
# session to the local sshd with no ssh verb for the recursion to see) --
# a resolvable ALIAS inside such a transport stays in the residual class.
# The alias fail-open above is scoped: a DOTLESS alias is answered from the
# hosts file same-call (open) with async revalidation; a DOTTED alias that
# resolves is classified by its addresses; a DOTTED name whose lookup FAILS
# is refused per call, deliberately -- answering a failed lookup open would
# let whoever controls resolution mint an allow by suppressing it, the
# same class the verdict cache refuses to latch.
#
# The strictly larger residual is OUT OF GATE SCOPE by the module's own
# doctrine (a script body is never a gate subject): interpreter/script-file
# indirection (``bash escape.sh``, ``python -c`` + subprocess or paramiko)
# and non-ssh-family clients (``git clone ssh://localhost/…``, autossh) reach
# the unsandboxed sshd without an ssh-family command line ever crossing this
# floor.  The sandbox has no network namespace, so loopback:22 stays
# reachable from inside; this floor is the interim tier, and the fix of
# record is an OS-level network fence in the sandbox (tracked follow-up:
# see the sandbox-escape residuals note in docs/system-specs/modules/security.md).


# Operand spellings that resolve to this host's name at run time, matched as
# TEXT inside a single argv token.  The substitution forms are substring
# matches ("$(hostname -f)" still hits); the variable forms are exact or
# dot-suffixed ("$hostname", "$hostname.example") so an unrelated variable
# like "$hostname_backup" is NOT read as this host.
_HOSTNAME_SUBSTITUTION_HINTS: tuple[str, ...] = ("$(hostname", "`hostname")


_HOSTNAME_VARIABLE_FORMS: tuple[str, ...] = ("$hostname", "${hostname}")


# A FLAT command substitution -- no nested substitution characters in the
# body -- is the only shape ``_static_substitution_output`` can decide
# statically.  Both spellings are matched; group 1 xor group 2 carries the
# body.
_FLAT_SUBSTITUTION_RE = re.compile(r"\$\(([^()`$]*)\)|`([^`()$]*)`")


def _glob_can_name_ssh_verb(probe: str) -> bool:
    """True when a glob word in *probe* can expand to an ssh-family program."""
    for word in probe.split():
        if not _GLOB_CHARS.intersection(word):
            continue
        base = word.rsplit("/", 1)[-1]
        for verb in _SSH_FAMILY_VERBS:
            if fnmatch.fnmatchcase(verb, base) or fnmatch.fnmatchcase(verb + ".exe", base):
                return True
    return False


# ``${VAR:-word}`` / ``${VAR:=word}`` / ``${VAR:+word}`` — bash substitutes the
# embedded WORD before exec, so a self-host hiding in the default IS the
# destination when the variable is unset (``ssh "${TARGET:-localhost}"``).
# The word is statically visible, so it is checked; a bare ``$VAR`` whose
# value only exists at run time remains the documented fail-open residual.
_EXPANSION_DEFAULT_RE = re.compile(r"\$\{[^{}:]*:[-=+?]([^{}]+)\}")


_SSH_FAMILY_VERBS: frozenset[str] = frozenset({"ssh", "scp", "sftp", "rsync"})


# Verbs that place a copy (or a link) of their SOURCE operand at their
# DESTINATION operand.  A destination whose source is an ssh-family binary
# IS that binary under a new name, so the walk binds it as the verb for the
# rest of the line.
_BINARY_COPY_VERBS: frozenset[str] = frozenset({"cp", "mv", "ln", "install"})


# A leading environment-assignment word (``FOO=bar cmd``) keeps the NEXT
# token in program position.  The walk reads lowercased text.
_ENV_ASSIGN_PREFIX_RE = re.compile(r"[a-z_][a-z0-9_]*=")


# rsync execs TWO env-var command values FROM HERE: ``RSYNC_RSH`` (its remote
# shell, exactly as ``-e``/``--rsh``) and ``RSYNC_CONNECT_PROG`` (its
# daemon-mode proxy for reaching ``rsync://`` / ``host::module``
# destinations).  A LEADING assignment (``RSYNC_RSH='ssh localhost' rsync …``)
# rides BEFORE the verb, where the operand walk in ``_is_ssh_to_self`` never
# sees it.  The captured value is a command line rsync execs FROM HERE, so it
# recurses the floor.  Tokens reach the walk lowercased; IGNORECASE keeps the
# match robust regardless.  The remote-shell env vars of OTHER verbs
# (``GIT_SSH_COMMAND`` and the like) stay a documented residual, deliberately
# out of this floor's scope.
_RSYNC_RSH_ASSIGN_RE = re.compile(r"^(rsync_rsh|rsync_connect_prog)=(.+)$", re.IGNORECASE)


# Sentinel bytes standing in for an ``_ends_argv`` boundary character that was
# QUOTED or backslash-escaped in the raw source, so it is literal DATA rather
# than a command separator.  ``_self_tokens`` runs shlex, which resolves quotes
# BEFORE the operand walk in ``_is_ssh_to_self`` runs, so by token time a quoted
# ``a;b`` is indistinguishable from a glued unquoted operator ``a;b`` -- yet the
# first is one operand and the second is a command boundary.  The distinguishing
# information exists only in the SOURCE, before tokenization, so the raw text is
# scanned there and every data separator is rewritten to a sentinel that
# ``_ends_argv`` does not treat as a boundary.  A real, unquoted separator keeps
# its character and still ends the walk.  The table covers EVERY character that
# can make ``_ends_argv`` end the walk (``;`` ``|`` ``&`` newline ``#`` ``(``
# ``{``) -- covering only a subset would let a quoted spelling of the missing
# one hide a self-host operand behind a fake boundary (``scp '&' localhost:/x``
# stops the walk at ``&`` with the target unexamined).  Control bytes are used
# because a real shell command never contains them.
_QUOTED_SEP_SENTINELS = {
    ";": "\x00",
    "|": "\x01",
    "&": "\x02",
    "\n": "\x03",
    "#": "\x04",
    "(": "\x05",
    "{": "\x06",
}


def _mask_quoted_separators(text: str) -> str:
    """Rewrite quoted/escaped ``_ends_argv`` boundary chars in *text* to sentinels.

    Single-pass quote-state scan over the RAW source:

    * inside single quotes -- boundary chars are literal data, so they become
      sentinels; a single quote takes no escapes, so a backslash inside is an
      ordinary character.
    * inside double quotes -- boundary chars are literal data, so they become
      sentinels; a backslash does not change a separator's literalness, so each
      character is handled on its own.
    * outside quotes -- a backslash escapes the next character, so an escaped
      boundary char is data and becomes a sentinel (the backslash is kept so
      shlex still de-escapes downstream, and an escaped quote never opens a
      context); the one exception is backslash-newline, a line continuation,
      handled at the escape branch.
    * every other character is copied verbatim; an unterminated quote leaves the
      rest of the string quoted, matching how bash reads it.

    Idempotent for this floor's purposes: a sentinel byte already present in
    hostile input is left in place, and ``_unmask_separators`` later turns it
    into a real separator -- which only ends the walk EARLIER, widening the deny
    (the safe direction).  The mask feeds ``_self_tokens`` (shlex) and recursive
    floor calls, both of which discard quotes anyway, so it never changes the
    resolved tokens beyond neutralizing the boundary ambiguity it exists to
    close.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is None:
            if ch == "\\" and i + 1 < n:
                # Escaped char: an escaped separator is data (mask it); keep the
                # backslash so shlex de-escapes downstream, and consume the next
                # char here so an escaped quote never opens a quote context.
                # EXCEPTION: backslash-newline is a LINE CONTINUATION -- bash
                # (and posix shlex) glue the surrounding text into one token
                # (``local\<newline>host`` resolves to ``localhost``), so it is
                # neither data nor a separator; masking it would corrupt the
                # glued token and un-match a continued self-host spelling.
                nxt = text[i + 1]
                out.append(ch)
                if nxt == "\n":
                    out.append(nxt)
                else:
                    out.append(_QUOTED_SEP_SENTINELS.get(nxt, nxt))
                i += 2
                continue
            if ch in ("'", '"'):
                quote = ch
            out.append(ch)
        elif quote == '"' and ch == "\\" and i + 1 < n and text[i + 1] in '"\\$`':
            # Inside DOUBLE quotes bash honors ``\`` only before ``"  \  $  ` ``
            # ``\"`` is a literal quote, NOT a close.  Copy both bytes
            # verbatim and skip the next char so the escaped quote never ends the
            # quote context -- otherwise ``scp "a\";b" localhost:/x`` would exit
            # the quote at ``\"`` and read the following ``;`` as a real
            # separator, ending the operand walk before the self-host target.
            # Single quotes honor no escape, so this is scoped to ``"``.
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        elif ch == quote:
            quote = None
            out.append(ch)
        else:
            # Inside a quote: a separator is literal data; everything else
            # (backslash included) is copied verbatim.
            out.append(_QUOTED_SEP_SENTINELS.get(ch, ch))
        i += 1
    return "".join(out)


def _unmask_separators(text: str) -> str:
    """Reverse :func:`_mask_quoted_separators` -- restore the boundary chars."""
    for sep, sentinel in _QUOTED_SEP_SENTINELS.items():
        text = text.replace(sentinel, sep)
    return text


# An EMPTY command substitution expands to nothing, so ``s$()sh`` runs ``ssh``
# and ``local$()host`` resolves to ``localhost`` -- the same glue-evasion the
# self-kill floor names for ``p$()kill``, but aimed at the verb gate and at the
# operand.  Collapsing these in the SOURCE text (before the gate and before
# tokenization) makes both the substring gate and every resolved operand read
# the spliced-out spelling.  Only ``$()``/backticks are matched: an empty
# ``${}`` is a bash syntax error, not an expansion, so it never splices.
_EMPTY_EXPANSION_RE = re.compile(r"\$\(\s*\)|`\s*`")


# ssh options whose VALUE is a forward/bind spec or a login name, not a
# destination this process connects to from here (lowered text collapses
# ``-L``/``-l`` and ``-W``/``-w``): ``-L``/``-R``/``-D``/``-W`` forward specs
# name listen addresses and far-side hops, ``-b`` a local bind address, and
# ``-l`` a login name.  Every value except ``-R``'s is exempt from the
# fail-closed operand check — ``ssh -L 127.0.0.1:8080:db:5432 far-host`` is
# the recommended way to reach a remote service and must stay allowed.  The
# ``-R`` destination is dialed FROM HERE, so its spec IS checked
# (``_remote_forward_value_targets_self``); ``-J`` deliberately stays checked
# too: the jump connection originates from THIS machine.
_SSH_FORWARD_OPT_LETTERS: frozenset[str] = frozenset("lrdwb")


# Static resolutions for ``_operand_targets_self``.  A ``$((…))``
# carrying ONE integer literal is normalized to the decimal spelling bash
# prints (hex and bash-octal included); anything else inside ``$((…))`` is
# left alone — an in-floor expression evaluator is itself attack surface, and
# a variable-carrying expression is runtime state (documented residual).
_ARITH_INT_LITERAL_RE = re.compile(r"\$\(\(\s*(-?(?:0[xX][0-9a-fA-F]+|[0-9]+))\s*\)\)")


# A brace group with a comma alternation or a ``..`` range is a brace
# expansion; ``{single}`` is literal in bash and stays untouched, and the
# lookbehind keeps ``${…}`` parameter expansions out.
_BRACE_GROUP_RE = re.compile(r"(?<!\$)\{([^{}]*)\}")


_BRACE_RANGE_RE = re.compile(r"\A(-?[0-9]+|[A-Za-z])\.\.(-?[0-9]+|[A-Za-z])(?:\.\.(-?[0-9]+))?\Z")


# ponytail: fan-out ceiling.  Expansion stops at 256 generated words and the
# caller DENIES (fail-closed) — a wider product in a connection operand is
# pathological, and enumerating it here would be its own DoS.
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


def _operand_targets_self(operand: str, *, host_position: bool = True) -> bool:
    """True if *operand* names THIS machine as a connection target.

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
    # A glued shell operator (``ssh localhost;true`` hands the operand
    # ``localhost;true``) is never part of a hostname, so consult the
    # operator-cut spelling too -- the same ``(token, _cut_at_operator(token))``
    # idiom the git-push floor uses.  This only WIDENS the deny: the cut token
    # has no operator left, so the recursive call cuts nothing and does not
    # recurse again, and a bare ``$VAR`` (no operator to cut) is unaffected.
    cut = _cut_at_operator(t)
    if cut != t and _operand_targets_self(cut, host_position=host_position):
        return True
    for hint in _HOSTNAME_SUBSTITUTION_HINTS:
        if hint in t:
            return True
    for var in _HOSTNAME_VARIABLE_FORMS:
        if t == var or t.startswith(var + ".") or t.startswith(var + ":"):
            return True
    # An expansion's embedded default is the destination when the variable is
    # unset — check every ``${…:-word}``-style word (recursion terminates:
    # the word is strictly shorter than the operand).  Kept alongside the
    # whole-operand resolution below because it is WIDER on mangled glue
    # (``foo${X:-localhost}bar``): the embedded word alone is self even when
    # the resolved whole is not.
    for match in _EXPANSION_DEFAULT_RE.finditer(t):
        if _operand_targets_self(match.group(1), host_position=host_position):
            return True
    # Bash substitutes a parameter default INTO the surrounding
    # word (``local${KC_UNSET:-host}`` -> ``localhost``), so the whole
    # operand is resolved and re-checked — checking each default word in
    # isolation misses the reconstruction.  ``_resolve_param_defaults``
    # substitutes every ``${VAR<op>literal}`` with its literal to a fixpoint
    # (nesting and colon-less operators included); the recursive call sees a
    # string with no such form left, so it does not recurse here again.
    resolved = _resolve_param_defaults(t)
    if resolved != t and _operand_targets_self(resolved, host_position=host_position):
        return True
    # A single integer literal inside arithmetic expansion is
    # printed decimally by bash (``$((0x7f))`` -> ``127``), gluing into the
    # surrounding word.  Only the literal spellings are normalized — an
    # expression or a variable stays unresolved (run-time residual, see the
    # docstring) — and the substituted string carries no ``$((…))`` literal,
    # so the recursion is a single level.
    normalized = _ARITH_INT_LITERAL_RE.sub(_arith_int_literal_repl, t)
    if normalized != t and _operand_targets_self(normalized, host_position=host_position):
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
        if choice != t and _operand_targets_self(choice, host_position=host_position):
            return True
    scheme_seen = False
    for scheme in ("ssh://", "scp://", "sftp://", "rsync://"):
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
    if _host_is_self(t, dns_fallback=host_position or scheme_seen):
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
            if _host_is_self(t[len(pre_colon.rsplit("@", 1)[0]) + 1 :]):
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
        if _host_is_self(tail, dns_fallback=host_position or scheme_seen):
            return True
        tail_host = tail.partition(":")[0]
        if tail_host != tail and _host_is_self(
            tail_host, dns_fallback=host_position or scheme_seen
        ):
            return True
    return _host_is_self(host, dns_fallback=host_position or scheme_seen or host != t)


def _ssh_family_verb(token: str) -> "str | None":
    """The ssh-family verb *token* invokes, or None.

    Path-qualified (``/usr/bin/ssh``) and Windows (``ssh.exe``) spellings
    resolve to the bare verb.  A glob basename that CAN expand to one
    (``s?h``, see ``_GLOB_CHARS``) resolves to the verb it can name.
    """
    base = _program_basename(_strip_redirect(token.strip("\"'")))
    if base.endswith(".exe"):
        base = base[: -len(".exe")]
    if base in _SSH_FAMILY_VERBS:
        return base
    if _GLOB_CHARS.intersection(base):
        for verb in _SSH_FAMILY_VERBS:
            if fnmatch.fnmatchcase(verb, base) or fnmatch.fnmatchcase(verb + ".exe", base):
                return verb
    return None


# ssh_config keywords that SET the destination.  ``-o hostname=…`` rewrites
# the host the positional operand merely aliases, and ``-o proxyjump=…``
# opens a connection of its own — so a self value in either is a self
# connection regardless of the operand.  Generic ``opt=value`` spellings
# (rsync ``--exclude=localhost``) name data, not a destination.
_SSH_ROUTING_OPTION_KEYS: tuple[str, ...] = ("hostname", "proxyjump")


# ssh_config keywords whose VALUE is a command line ssh runs LOCALLY, not a
# host.  ``-o proxycommand="ssh localhost x"`` and ``-o localcommand=…`` both
# exec their value on THIS machine, so a value that itself opens a channel to
# this host is a self connection; ``-o knownhostscommand=…`` likewise runs its
# value locally (to print known_hosts lines), so it is scanned the same way.
# scp/sftp forward ``-o`` to ssh, so the same hole reaches them.  The value is
# checked by recursing the whole floor on it.
_SSH_COMMAND_OPTION_KEYS: tuple[str, ...] = ("proxycommand", "localcommand", "knownhostscommand")


# OpenSSH's valueless short flags, case-folded (the floor lowercases the whole
# command line once at entry).  getopt lets these BUNDLE in front of an option
# letter -- ``-voHostname=x`` is ``-v`` + ``-o Hostname=x`` -- so a glued
# ``o``/``j`` is still the option letter after this prefix.  Value-taking
# letters (``-l``, ``-p``, ...) are NOT here: they consume the rest of the
# token as their argument.  A folded twin whose uppercase takes a value
# (``-Q``, ``-M``) can only ADD a denial of a spelling ssh itself rejects,
# never an allow.
_SSH_VALUELESS_SHORT_FLAGS = "1246acfgkmnqstvxy"


# Case-folded option letters that CONSUME the next token as their value, PER
# VERB.  The walk itself stays table-free and fail-closed -- these
# sets only decide whether the token AFTER an option keeps HOST POSITION and
# whether it consumes the ssh/sftp positional slot.  The source is case-folded,
# so where upper and lower case disagree on valueness the letter is IN the
# set, i.e. treated as VALUE-TAKING: a value token mistaken for the positional
# CONSUMES the slot and the real host after it is never checked at all
# (``sftp -R 64 localhost``), while a host token mistaken for a value leaves
# the slot pending, so every later token still gets the full checks --
# over-checking is this floor's safe direction.  (Treating such a letter as
# valueless and betting that "the DNS layer still covers a host after it" does
# not survive the consumption path.)  scp/rsync have no positional slot in
# this walk, so they need no entry.
_VERB_VALUE_TAKING_OPT_LETTERS: "dict[str, frozenset[str]]" = {
    "ssh": frozenset("bcdefijlmopqrsw"),
    "sftp": frozenset("bcdfijloprsx"),
}


# Folded letters whose two cases DISAGREE on valueness: the letter
# stays in the value set above (so the slot is not consumed by mistake), but
# the swallowed token ALSO keeps full host-position checks — both candidate
# destinations are over-checked, this floor's safe direction.  ssh: c/C, f/F,
# m/M, q/Q, s/S.  sftp: c/C, f/F, p/P, r/R.
_VERB_FOLD_AMBIGUOUS_OPT_LETTERS: "dict[str, frozenset[str]]" = {
    "ssh": frozenset("cfmqs"),
    "sftp": frozenset("cfpr"),
}


def _proxyjump_value_targets_self(value: str) -> bool:
    """True if any hop in a ProxyJump chain names this host.

    A ProxyJump value is a COMMA-SEPARATED chain (``localhost,far``); ssh dials
    the first hop directly from HERE, so a self-host anywhere in the chain is a
    self dial.  Each hop is checked (fail-closed): over-blocking a later hop is
    the safe direction, and it keeps this simple.  The WHOLE value is checked
    too (round-10): a brace alternation's own comma (``local{h,}ost``) is torn
    by the hop split, and only the unsplit spelling resolves back to the word
    bash glues together.
    """
    if _operand_targets_self(value):
        return True
    return any(_operand_targets_self(hop) for hop in value.split(","))


def _remote_forward_value_targets_self(value: str) -> bool:
    """True if a remote-forward (``-R`` / RemoteForward) spec dials THIS host.

    ``-R`` is the one forward whose DESTINATION is dialed FROM HERE: the far
    sshd listens, and each accepted connection is handed back for THIS client
    to connect to ``host:hostport`` locally -- so ``-R 2222:localhost:22``
    gives remote users the local unsandboxed sshd.  Branch table for
    ``_SSH_FORWARD_OPT_LETTERS`` (the others stay exempt): ``-L``/``-D``
    destinations are dialed from the FAR side (its loopback is the far
    machine), ``-w`` names tun devices, ``-b`` a local source bind -- none
    dials a destination from here.  Spec shapes, colon-split OUTSIDE
    brackets: ``[bind:]port:host:hostport`` (3-4 fields) checks the host
    field as a connection target (DNS-classified like any host value);
    ``[bind:]port`` (1-2 fields) is reverse-SOCKS, where the REMOTE chooses
    every local dial destination -- loopback included -- so it fails closed.
    The config spelling ``RemoteForward listen dest`` is whitespace-joined
    onto ``:`` first, which reduces it to the same shape.
    """
    spec = ":".join(value.strip().strip("\"'").split())
    fields: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(spec):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            fields.append(spec[start:i])
            start = i + 1
    fields.append(spec[start:])
    if len(fields) <= 2:
        return True
    dest = fields[-2].strip().strip("[]")
    return bool(dest) and _operand_targets_self(dest)


def _command_value_names_self_endpoint(value: str) -> bool:
    """True if a ProxyCommand-style value names a LITERAL self endpoint.

    The value is a local command line whose network endpoint decides where
    the outer session lands (round-26): ``nc 127.0.0.1 22`` relays the whole
    session to the local sshd with no ssh-family verb for the recursion to
    see.  Each whitespace word is checked as a connection operand, and its
    bracketed groups and separator-split fragments are checked LITERALLY --
    fragments never resolve DNS (``dns_fallback=False``), so option words
    like ``-connect`` or socat's ``TCP`` address prefix cannot fail closed,
    and a resolvable alias inside a transport (``nc myalias 22``) stays the
    documented alias residual the interim tier does not close.
    """
    for word in value.split():
        stripped = word.strip("\"'")
        if not stripped or stripped.startswith("-"):
            continue
        if _operand_targets_self(stripped, host_position=False):
            return True
        for group in re.findall(r"\[([^\]]*)\]", stripped):
            if _host_is_self(group, dns_fallback=False):
                return True
        without_brackets = re.sub(r"\[[^\]]*\]", "", stripped)
        for frag in re.split(r"[:=,]", without_brackets):
            if frag and _host_is_self(frag, dns_fallback=False):
                return True
    return False


def _routing_option_key_value_targets_self(key: str, value: str) -> bool:
    """True if ssh option *key* routes *value* to this host.

    ``-o`` may be GLUED to the keyword (``-ohostname=…``), so a leading ``o`` in
    front of a longer name is the option letter, not part of the keyword.
    ProxyJump is a comma-chain; ProxyCommand/LocalCommand values are local
    command lines that recurse the floor (the value is strictly shorter than the
    token, so the recursion terminates); Hostname is a single host.
    """
    if key.startswith("o") and len(key) > 1:
        key = key[1:]
    if key == "proxyjump":
        return _proxyjump_value_targets_self(value)
    if key == "remoteforward":
        return _remote_forward_value_targets_self(value)
    if key in _SSH_ROUTING_OPTION_KEYS:  # "hostname" (proxyjump handled above)
        return _operand_targets_self(value)
    if key in _SSH_COMMAND_OPTION_KEYS:
        return _is_ssh_to_self(value) or _command_value_names_self_endpoint(value)
    return False


def _routing_option_value_targets_self(token: str, *, value_slot: bool) -> bool:
    """True if *token* carries a routing option value naming this host.

    Covers the ``key=value`` spelling, the config-style WHITESPACE spelling
    OpenSSH equally accepts (``-o "Hostname localhost"`` resolves to the same
    routing -- verified against ``ssh -G``), and the attached jump-host form
    (``-Jlocalhost``, ``-Jlocalhost,far``, which opens a connection of its own).
    The whitespace form is only read in a VALUE SLOT (an option's argument, or
    attached to the option itself): in plain operand position a two-word token
    is remote command data (``ssh far 'hostname localhost'``), not routing.
    """
    head, eq, value = token.partition("=")
    if eq:
        key = head.lstrip("-")
        if token.startswith("-"):
            # getopt bundles valueless flags in FRONT of the option letter:
            # ``-voHostname=x`` is ``-v`` + ``-o Hostname=x``.
            key = key.lstrip(_SSH_VALUELESS_SHORT_FLAGS)
        if _routing_option_key_value_targets_self(key, value):
            return True
    elif value_slot:
        parts = token.strip().split(None, 1)
        if len(parts) == 2 and _routing_option_key_value_targets_self(
            parts[0].lstrip("-"), parts[1]
        ):
            return True
    if token.startswith("-"):
        bare = token.lstrip("-").lstrip(_SSH_VALUELESS_SHORT_FLAGS)
        if len(bare) > 1 and bare[0] == "j" and _proxyjump_value_targets_self(bare[1:]):
            return True
    return False


# Same-line literal shell assignments.  bash substitutes ``$a`` /
# ``${a}`` from an assignment earlier on the SAME line before exec, so
# ``a=s; ${a}sh localhost`` runs ``ssh``.  Only literal, separator-free values
# are modeled (optionally quoted); a value carrying ``$``, a backtick, spaces,
# or a command separator stays unresolved -- the documented run-time residual.
# Resolution is position-aware: a reference takes the LATEST assignment whose
# text precedes it, exactly like bash, so ``${a}sh; a=s`` does not resolve.
_LINE_ASSIGNMENT_RE = re.compile(
    r"(?:\A|[;&|`(\s])([a-z_][a-z0-9_]*)=([\"']?)([a-z0-9._:@/-]*)\2(?=\Z|[)\s;&|])"
)


_VAR_REFERENCE_RE = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}|\$([a-z_][a-z0-9_]*)")


def _resolve_line_assignments(text: str) -> str:
    """Substitute ``$var``/``${var}`` from literal same-line assignments."""
    assignments: "list[tuple[int, str, str]]" = []
    for m in _LINE_ASSIGNMENT_RE.finditer(text):
        assignments.append((m.end(), m.group(1), m.group(3)))
    if not assignments:
        return text

    def _substitute(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        value: "str | None" = None
        for end, assigned, assigned_value in assignments:
            if end <= match.start() and assigned == name:
                value = assigned_value
        return match.group(0) if value is None else value

    return _VAR_REFERENCE_RE.sub(_substitute, text)


# One-level function-call argument binding.  A same-line function
# definition whose body dials a positional parameter (``f(){ ssh "$1" id; };
# f localhost``) is an ordinary evasion: the literal call argument IS the
# destination.  Each (definition, later call) pair is bound and the bound body
# recursed through the floor.  One level only -- a function calling another
# function is the documented residual -- and both fan-outs are capped.
_FUNCTION_DEF_RE = re.compile(
    # ``name() { … }``, ``function name() { … }``, and bash's parenthesis-free
    # ``function name { … }`` keyword form all define the same
    # function.  The body group matches BALANCED braces one level deep:
    # a non-greedy ``.*?`` stopped at the ``}`` inside ``${1}``,
    # truncating the body and losing the dial.  The two alternatives are
    # disjoint on their first character, so the group is backtracking-safe;
    # a body nesting braces two deep stays unmatched -- same one-level scope
    # as the binder itself (a function calling a function is the documented
    # residual).
    r"(?:function\s+([a-z_][a-z0-9_]*)\s*(?:\(\s*\))?|([a-z_][a-z0-9_]*)\s*\(\s*\))\s*"
    r"\{((?:[^{}]|\{[^{}]*\})*)\}",
    re.DOTALL,
)


_FUNCTION_BIND_CAP = 8


# A call still INVOKES the function behind leading assignment words
# (``x=1 f localhost``) and the invocation keywords that run their operand as
# a command (``time f``, ``! f``, ``if f; then``).  ``command``/``exec``/
# ``nohup`` are NOT here: ``command`` bypasses function lookup and the other
# two exec real binaries, so none of them reaches the function body.
_CALL_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*\Z")


_CALL_WRAPPER_WORDS: frozenset[str] = frozenset(
    {"time", "!", "if", "elif", "while", "until", "then", "else", "do", "{", "("}
)


def _function_call_binds_self(text: str) -> bool:
    """True if a bound (def, call) pair opens an ssh channel to this host."""
    # The caller hands over masked text: a quoted ``{`` rides as a sentinel
    # byte while its closing ``}`` stays literal, which breaks the DEF
    # regex's balanced-brace body capture on ``"${10}"``.  Restore ONLY the
    # parameter-expansion opener -- ``${`` is an expansion, never a command
    # boundary -- so quoted braced positionals capture and bind.
    text = text.replace("$\x06", "${")
    for def_count, m in enumerate(_FUNCTION_DEF_RE.finditer(text)):
        if def_count >= _FUNCTION_BIND_CAP:
            # Past the cap the binder cannot resolve what a definition does,
            # so it fails CLOSED: a 9th definition is deniable padding, not a
            # command shape the floor can clear.
            return True
        name, body = m.group(1) or m.group(2), m.group(3)
        # The caller hands over masked text (quoted separators AND quoted
        # braces ride as mask bytes), so a quoted ``"${10}"`` reads as
        # ``"$\x0610}"`` here.  Unmask the captured body before binding or
        # the braced replacements never match.
        body = _unmask_separators(body)
        if "$" not in body:
            continue  # nothing to bind
        call_count = 0
        for segment in re.split(r"[;&|\n]", text[m.end() :]):
            words = segment.split()
            # Skip leading assignment words and invocation keywords
            # -- both still invoke the function.  Any OTHER leading word means
            # the name is an argument (``echo f localhost``), not a call.
            idx = 0
            while idx < len(words) and (
                words[idx] in _CALL_WRAPPER_WORDS
                or _CALL_ASSIGNMENT_RE.fullmatch(words[idx]) is not None
            ):
                idx += 1
            if idx >= len(words) or words[idx] != name:
                continue
            call_count += 1
            if call_count > _FUNCTION_BIND_CAP:
                # Same fail-closed rule as the definition cap: the 9th call
                # of one name is unresolvable padding, so deny.
                return True
            args = [w.strip("\"'") for w in words[idx + 1 :]]
            bound = body
            joined = " ".join(args)
            for star in ('"$@"', "$@", '"$*"', "$*"):
                bound = bound.replace(star, joined)
            # Braced ``${N}`` has no single-digit ceiling in bash, so it
            # binds for EVERY supplied argument; unbraced ``$N`` reads one
            # digit (``$10`` is ``${1}0``), so it stays capped at nine.
            for i, arg in enumerate(args, start=1):
                bound = bound.replace('"${%d}"' % i, arg)
                bound = bound.replace("${%d}" % i, arg)
                if i <= 9:
                    bound = bound.replace('"$%d"' % i, arg)
                    bound = bound.replace("$%d" % i, arg)
            if bound != body and _is_ssh_to_self(bound):
                return True
    return False


def _is_ssh_to_self(text_lower: str) -> bool:
    """True if *text_lower* opens an ssh/scp/sftp/rsync channel to THIS host.

    Structural for the same reason the other floors are: the target hides
    behind options, redirects, quoting, and prefixes.  The argv walk is
    FAIL-CLOSED about option grammar: EVERY token that could be an operand —
    including one sitting where an option's value would go — is checked
    against the self-host set.  The per-verb value-taking table
    (``_VERB_VALUE_TAKING_OPT_LETTERS``) never exempts a token from that
    check; it only decides whether the token after an option keeps HOST
    POSITION (the DNS-layer gate) and whether it consumes the ssh/sftp
    positional slot, with collisions resolved toward value-taking — the
    over-checking direction (a valueless-flag table exempting the target
    itself is the shape that mis-consumed ``scp -r localhost:…``).  A
    non-forwarding option value (a port, a cipher) never names this host, so
    the check costs nothing; a self-host hiding in value position is denied.
    The one carved-out value class is the forward/bind specs
    (``_SSH_FORWARD_OPT_LETTERS``): ``ssh -L 127.0.0.1:8080:db:5432
    far-host`` names a local LISTEN address, not a destination, and must
    stay allowed.
    ssh/sftp consume their FIRST unshadowed operand as the positional host
    (later operands are the remote command, where a word like "localhost" is
    data, not a destination); scp/rsync accept a target in any operand
    position.  ``opt=value`` tokens (``-ohostname=localhost``,
    ``-o proxyjump=localhost``) have their value checked too.  Redirections
    are stepped over the way bash removes them from argv.  Same argv-boundary
    discipline as the self-kill floor: the walk is substitution-aware and
    stops at this command's own separator.

    Two evasions are neutralized before the walk.  Empty command substitutions
    (``s$()sh``, ``local$()host``) expand to nothing, so they are collapsed out
    of the source text first -- otherwise they hide the verb from the substring
    gate and the self-host from an operand.  Quote and backslash splices
    (``ss""h``, ``s\\sh``) are rejoined by shlex in the tokens but still hide the
    verb from the raw-substring gate, so the gate probes a splice-stripped copy.
    Two value classes are command lines this host runs LOCALLY rather than
    hosts, and both recurse this floor on the value: ssh's
    ``-o proxycommand=``/``-o localcommand=`` (in
    ``_routing_option_value_targets_self``) and rsync's ``-e``/``--rsh``.
    rsync also honors a leading ``RSYNC_RSH=`` environment assignment as the
    same selector -- handled in the walk below; the wider remote-shell env-var
    family (``GIT_SSH_COMMAND`` and the like) is a documented residual, as are
    the shell-builtin export spellings (``declare -x``/``typeset -x``), which
    are not leading-assignment syntax and so are not modeled by the walk.
    """
    # Collapse empty expansions in the SOURCE so the gate and tokenization both
    # read the spliced-out spelling (``s$()sh localhost`` -> ``ssh localhost``).
    text_lower = _EMPTY_EXPANSION_RE.sub("", text_lower)

    # A flat command substitution whose output is statically decidable
    # (``$(printf localhost)``, backtick ``echo localhost``) IS its output by
    # the time the kernel sees the command line, so splice that output into
    # the source before the probe and the walk -- the resolved self-host is
    # then an ordinary operand, glued spellings (``local$(printf host)``)
    # included.  ``_static_substitution_output`` resolves only echo/printf
    # literals; every other body keeps its original text (the ``$(hostname``
    # hints below read it) and stays the documented run-time residual.  A
    # single-quoted spelling resolves too -- an over-approximation toward
    # deny, accepted for a deny floor.
    def _splice_static_substitution(match: "re.Match[str]") -> str:
        body = match.group(1) if match.group(1) is not None else match.group(2)
        out = _static_substitution_output(body)
        return match.group(0) if out == "\x00" else out

    text_lower = _FLAT_SUBSTITUTION_RE.sub(_splice_static_substitution, text_lower)
    # Substitute ``$a``/``${a}`` from literal same-line assignments
    # (``a=s; ${a}sh localhost`` -> ``ssh localhost``) so neither the verb nor
    # the operand can be spliced from statically-known text.  Values are
    # separator-free by construction, so the substitution cannot fabricate a
    # command boundary the masking below would misread.
    text_lower = _resolve_line_assignments(text_lower)
    # Mask every ``;``/``|`` that is QUOTED or backslash-escaped in the source to
    # a sentinel BEFORE tokenization, so a quoted separator surviving into a
    # shlex-dequoted token (``scp 'a;b' localhost:/x``) is not read as a
    # command boundary by ``_ends_argv`` -- which would end the walk before the
    # real target and let the connection through.  It is restored only at the
    # faithful operand/routing checks below; recursion sites receive the masked
    # text unchanged (the mask is idempotent -- their quotes are already gone).
    text_lower = _mask_quoted_separators(text_lower)
    # Quote/backslash splices survive into the tokens (shlex rejoins them) but
    # defeat this raw-substring gate, so probe a copy with them stripped out.
    # Bash decodes ANSI-C quoting ($'\x73\x73\x68' -> ssh)
    # before exec, and the operand walk's tokenizer resolves it too -- so the
    # gate guarding that walk probes the DECODED text, or a verb spelling the
    # walk would deny slips past the gate the walk never gets to correct.
    probe = _decode_shell_quoted_literals(text_lower)
    probe = probe.replace('"', "").replace("'", "").replace("\\", "")
    # bash substitutes ``${VAR:-word}`` defaults before exec, so the verb can
    # be spliced from statically-known text (``s${U:-s}h`` -> ``ssh``).
    # Resolve the same forms the operand walk resolves; a bare ``$VAR`` whose
    # value exists only at run time stays the documented fail-open residual.
    probe = _resolve_param_defaults(probe)
    if not any(verb in probe for verb in _SSH_FAMILY_VERBS) and not _glob_can_name_ssh_verb(probe):
        return False
    # Bind one level of function-call arguments (``f(){ ssh "$1"
    # id; }; f localhost``) and recurse on the bound body -- the literal call
    # argument is the destination the walk below cannot see through ``$1``.
    if _function_call_binds_self(text_lower):
        return True
    # A self-targeting ``RSYNC_RSH`` set in one frame is inherited by
    # LATER frames (an exported value, or a command-scoped prefix on a command
    # that spawns a nested ``sh -c`` payload), where the per-frame pending/export
    # walk below -- which re-initialises inside each frame -- cannot see it.
    # This function-scope latch carries that fact forward; it is set at the END
    # of a frame (so the same-frame ``;``-clear semantics are unchanged
    # for the frame that owns the assignment) and never cleared.
    # Over-approximation, accepted for a deny floor: once set, the latch also
    # covers a textually-later SIBLING nested payload; a contrived same-frame
    # ``RSYNC_RSH=self cmd; rsync other:/ .`` stays allowed by the ``;``-clear
    # rule because the latch is consulted only in LATER frames.
    rsync_rsh_carried_self = False
    # Destination paths a same-line cp/mv/ln/install gave to
    # an ssh-family binary, bound to the verb they now carry.  Shared across
    # frames (a copy staged in a wrapper payload reaches its siblings); a
    # copy staged in an EARLIER command line is the documented residual.
    bound_program_verbs: "dict[str, str]" = {}
    for tokens in _self_token_frames(text_lower):
        programs = _argv_programs(tokens)
        # A leading ``RSYNC_RSH=<cmd>`` environment assignment selects rsync's
        # remote shell exactly like ``-e``/``--rsh``, but rides BEFORE the verb
        # so the operand walk below never sees it.  When rsync runs live, that
        # value is a local command line that recurses the floor.  Bash applies a
        # leading assignment to the WHOLE simple command that follows -- the
        # command word and every child it execs, wrappers of any shape
        # (``env``/``command``/``timeout`` …) included -- so an ordinary word
        # does NOT drop the pending value; only a command separator ends that
        # simple command and clears it (tracked substitution-aware, so a ``;``
        # inside ``$( … )`` does not).  ``export`` makes the value persist for
        # the rest of the line, so an exported value is remembered separately
        # and survives separators.
        rsync_rsh_pending: dict[str, str] = {}
        rsync_rsh_exported: dict[str, str] = {}
        # Names declared for export BEFORE any value exists (the POSIX
        # ``export VAR; VAR=value`` order): a later assignment to a
        # marked name is exported the moment it lands.  Never cleared by
        # separators.
        rsync_rsh_export_marked: set[str] = set()
        # The last plain assignment seen in this frame, PER NAME (the two
        # rsync env vars are independent — one shared slot would let a later
        # far assignment overwrite an earlier self value), wherever it
        # appeared: a shell variable persists for the rest of the line even
        # after the simple command it prefixed ends, so a later bare
        # ``export RSYNC_RSH`` (the POSIX ``VAR=value; export VAR`` two-step)
        # promotes THAT name's value into the environment.  Never cleared by
        # separators.
        rsync_rsh_assigned: dict[str, str] = {}
        prev_stripped_tok: "str | None" = None
        cmd_start = True  # the next token sits in program position
        outer_depth = 0
        xargs_prefix_index: "int | None" = None  # a bare ``xargs`` in this simple command
        # Once per FRAME, not once per verb token: see ``_is_credential_mint``.
        disqualified: "bool | None" = None
        for i, token in enumerate(tokens):
            verb = _ssh_family_verb(token)
            if verb is None and bound_program_verbs:
                stripped_prog = token.strip("\"'")
                verb = bound_program_verbs.get(stripped_prog) or bound_program_verbs.get(
                    _program_basename(stripped_prog)
                )
            if verb is None:
                stripped_tok = token.strip("\"'")
                assign = _RSYNC_RSH_ASSIGN_RE.match(stripped_tok)
                if assign is not None:
                    env_name = assign.group(1)
                    value = assign.group(2).strip("\"'")
                    rsync_rsh_pending[env_name] = value
                    rsync_rsh_assigned[env_name] = value
                    # A preceding ``export`` makes the assignment persist into
                    # every later segment's environment for real, so remember it
                    # where no separator clears it.
                    if prev_stripped_tok == "export" or env_name in rsync_rsh_export_marked:
                        rsync_rsh_exported[env_name] = value
                else:
                    exported_name = stripped_tok.rstrip(";&")
                    if prev_stripped_tok == "export" and exported_name in (
                        "rsync_rsh",
                        "rsync_connect_prog",
                    ):
                        # Bare ``export RSYNC_RSH`` (or ``RSYNC_CONNECT_PROG``)
                        # after an earlier plain assignment: THAT name's stored
                        # value enters the environment.  The walk reads lowered
                        # text, and a glued separator (``export RSYNC_RSH;``)
                        # rides on the name token.  Declared BEFORE any value
                        # (export-first), the name is marked so the later
                        # assignment exports itself.
                        rsync_rsh_export_marked.add(exported_name)
                        if exported_name in rsync_rsh_assigned:
                            rsync_rsh_exported[exported_name] = rsync_rsh_assigned[exported_name]
                    outer_depth += _substitution_depth_delta(token)
                    if outer_depth <= 0 and _ends_argv(token):
                        # A command separator ends the simple command the
                        # leading assignment prefixed; pending does not cross it.
                        rsync_rsh_pending.clear()
                        xargs_prefix_index = None
                    outer_depth = max(outer_depth, 0)
                # ``xargs`` turns its stdin into argv for the
                # program it launches -- remember a bare ``xargs`` in this
                # simple command (any position: wrappers keep it off program
                # position), so a later verb + here-string is judged as the
                # command xargs actually runs.
                if _program_basename(stripped_tok) == "xargs":
                    xargs_prefix_index = i
                # A program-position cp/mv/ln/install whose
                # source operand is an ssh-family binary binds its DESTINATION
                # operand as that verb for the rest of the line, so
                # ``cp /usr/bin/ssh /tmp/x && /tmp/x localhost`` cannot shed
                # the floor by shedding the basename.  Statically decidable,
                # exactly like the same-line assignment/function binders.
                if cmd_start and _program_basename(stripped_tok) in _BINARY_COPY_VERBS:
                    operands: "list[str]" = []
                    for peek in tokens[i + 1 :]:
                        peeked = peek.strip("\"'")
                        if _ends_argv(peek):
                            # A separator can ride glued on the last operand
                            # (``/tmp/y;``) -- keep the de-glued word, then
                            # stop at the boundary.
                            deglued = peeked.rstrip(";&|\n")
                            if deglued and not deglued.startswith("-"):
                                operands.append(deglued)
                            break
                        if peeked.startswith("-"):
                            continue
                        operands.append(peeked)
                    if len(operands) >= 2:
                        for source in operands[:-1]:
                            bound_verb = _ssh_family_verb(source)
                            if bound_verb is not None:
                                dest = operands[-1]
                                bound_program_verbs[dest] = bound_verb
                                bound_program_verbs[_program_basename(dest)] = bound_verb
                                break
                cmd_start = _ends_argv(token) or (
                    cmd_start and _ENV_ASSIGN_PREFIX_RE.match(stripped_tok) is not None
                )
                prev_stripped_tok = stripped_tok
                continue
            # A verb starts a fresh simple command, so the ``export`` adjacency
            # run ends here.
            prev_stripped_tok = None
            # ``echo ssh localhost`` prints two words; it connects to nothing.
            if disqualified is None:
                disqualified = _shell_normalizer._data_consumer_command_disqualified(tokens)
            if _data_consumer_exempt(i, token, programs, tokens, command_disqualified=disqualified):
                continue
            # Launched through xargs, the verb's REAL argv
            # arrives on stdin -- and a here-string puts that stdin in the
            # source text, so ``xargs ssh <<< localhost`` runs
            # ``ssh localhost``.  Rebuild that command and judge it like a
            # directly-typed one; the rebuild drops the xargs word and the
            # here-string pair, so the recursion walks strictly fewer tokens
            # and terminates.
            if xargs_prefix_index is not None and xargs_prefix_index < i:
                rebuilt = _xargs_here_string_rebuild(verb, tokens, xargs_prefix_index, i)
                if rebuilt is not None and _is_ssh_to_self(rebuilt):
                    return True
            # rsync execs a pending ``RSYNC_RSH`` / ``RSYNC_CONNECT_PROG``
            # value as its remote shell (or daemon proxy) FROM HERE, so a
            # value that opens a channel to this host is a self dial.
            # An unexported value applies only to this simple command; an
            # exported one persists, so fall back to it when nothing is pending.
            if verb == "rsync":
                for env_name in ("rsync_rsh", "rsync_connect_prog"):
                    env_val = rsync_rsh_pending.get(env_name, rsync_rsh_exported.get(env_name))
                    if env_val is not None and _is_ssh_to_self(env_val):
                        return True
                # With neither a pending nor an exported value in THIS
                # frame, fall back to a self-targeting value carried from an
                # EARLIER frame (inherited into this nested payload).
                if not rsync_rsh_pending and not rsync_rsh_exported and rsync_rsh_carried_self:
                    return True
            positional_pending = verb in ("ssh", "sftp")
            option_shadow = False  # previous token was an option that may take a value
            value_shadow = False  # previous token was an option that CONSUMES a value
            value_shadow_ambiguous = False  # ...but its folded letter is case-ambiguous
            forward_value_pending = False  # previous token was a forward/bind option
            forward_value_letter = ""  # which forward letter armed it (``r`` is checked)
            redirect_target_pending = False  # previous token was a detached redirect op
            rsh_value_pending = False  # previous token was rsync -e/--rsh (value is a local cmd)
            proxyjump_value_pending = False  # previous token was a detached -J (value = hop chain)
            opts_terminated = False  # an exact ``--`` ended option parsing (POSIX)
            depth = 0
            for arg in tokens[i + 1 :]:
                stripped = arg.strip("\"'")
                # Classify BEFORE testing whether the token ends the argv
                # (same order as the self-kill floor): a quoted remote payload
                # may contain separator characters, and for scp/rsync a
                # target can legally follow it.
                if redirect_target_pending:
                    # The filename after a detached ``>``/``2>``/``<`` — bash
                    # removes both words from argv before exec.
                    redirect_target_pending = False
                elif rsh_value_pending:
                    # The detached value of rsync ``-e``/``--rsh``: a
                    # remote-shell command line rsync execs FROM HERE, so
                    # ``-e 'ssh localhost'`` opens a local ssh into this host.
                    # Recurse the floor on the value (strictly shorter than the
                    # whole command, so this terminates).  Checked first so the
                    # value is consumed whatever it looks like.
                    rsh_value_pending = False
                    option_shadow = False
                    value_shadow = False
                    if _is_ssh_to_self(stripped):
                        return True
                elif proxyjump_value_pending:
                    # The detached value of ``-J``: a ProxyJump hop
                    # chain whose FIRST hop is dialed from here -- comma-split
                    # it exactly like the attached ``-Jvalue`` spelling, which
                    # ``_routing_option_value_targets_self`` already covers.
                    proxyjump_value_pending = False
                    option_shadow = False
                    value_shadow = False
                    if _proxyjump_value_targets_self(_unmask_separators(stripped)):
                        return True
                elif ">" in stripped or "<" in stripped:
                    # A redirection construct.  The part before the operator is
                    # an ordinary word when non-numeric
                    # (``localhost>/dev/null``); a bare or fd-prefixed operator
                    # (``>``, ``2>``) also consumes the NEXT token as its
                    # target, unless the target is attached (``>/dev/null``,
                    # ``2>&1``).
                    remainder = _strip_redirect(stripped)
                    if remainder and not remainder.isdigit():
                        checkable = positional_pending or verb in ("scp", "rsync")
                        # Host position = the unshadowed ssh/sftp positional
                        # slot; an option value or an scp/rsync file operand
                        # is not one (its ``host:path`` colon form re-enables
                        # the DNS layer inside the check).
                        if checkable and _operand_targets_self(
                            _unmask_separators(remainder),
                            host_position=positional_pending and not value_shadow,
                        ):
                            return True
                        if not value_shadow:
                            # After a VALUELESS flag the token IS
                            # the positional destination -- consuming the slot
                            # keeps a later dotted remote-command argument
                            # out of host position.
                            positional_pending = False
                    elif stripped.endswith((">", "<")):
                        redirect_target_pending = True
                    option_shadow = False
                    value_shadow = False
                elif stripped == "--" and not opts_terminated:
                    # Exact ``--`` is the POSIX option
                    # TERMINATOR -- everything after it is an operand.
                    # Classified as a long option, ``value_shadow`` swallowed
                    # the NEXT token out of host position, so a DNS-classified
                    # self alias after ``--`` was never checked.
                    opts_terminated = True
                    option_shadow = False
                    value_shadow = False
                elif not opts_terminated and stripped.startswith("-") and len(stripped) > 1:
                    # An option.  Its attached ``=value`` is checked only for
                    # the ROUTING options ssh resolves a destination from
                    # (``-ohostname=localhost``, ``-oproxyjump=localhost``) —
                    # a generic ``--opt=value`` (rsync ``--exclude=localhost``)
                    # names data, not a destination.
                    if _routing_option_value_targets_self(
                        _unmask_separators(stripped), value_slot=True
                    ):
                        return True
                    # rsync runs its ``-e``/``--rsh`` value as the remote-shell
                    # command FROM HERE, so ``-e 'ssh localhost'`` execs a local
                    # ssh into this host: the value is a command line, not a
                    # host, and must recurse this floor.  It rides in-token for
                    # ``--rsh=…`` and for a single-dash bundle that reaches
                    # ``e`` with letters after it (``-e'ssh …'`` tokenizes to
                    # ``-essh …``); a bare ``--rsh`` or a bundle ENDING in ``e``
                    # (``-ave``) takes the NEXT token via ``rsh_value_pending``.
                    # Only exactly ``rsh`` among double-dash options consumes a
                    # value, so ``--exclude=localhost`` stays data.
                    if verb == "rsync":
                        bare_opt = stripped.lstrip("-")
                        if stripped == "--rsh":
                            rsh_value_pending = True
                        elif stripped.startswith("--rsh="):
                            if _is_ssh_to_self(stripped.partition("=")[2]):
                                return True
                        elif not stripped.startswith("--") and "e" in bare_opt:
                            attached = bare_opt.partition("e")[2]
                            if attached and _is_ssh_to_self(attached):
                                return True
                            if not attached:
                                rsh_value_pending = True
                    option_shadow = True
                    bare = stripped.lstrip("-")
                    # Only an option whose (bundle-final) letter
                    # takes a value for THIS verb swallows the next token out
                    # of host position; ``ssh -v self.example`` keeps its
                    # destination DNS-checked, ``sftp -R 64 localhost`` keeps
                    # its positional slot pending past the consumed ``64``.
                    # Double-dash options keep the conservative value
                    # assumption.
                    value_shadow = stripped.startswith("--") or (
                        bool(bare)
                        and bare[-1] in _VERB_VALUE_TAKING_OPT_LETTERS.get(verb, frozenset())
                    )
                    # A folded letter whose two cases disagree on
                    # valueness keeps the swallowed token host-checkable.
                    value_shadow_ambiguous = (
                        not stripped.startswith("--")
                        and bool(bare)
                        and bare[-1] in _VERB_FOLD_AMBIGUOUS_OPT_LETTERS.get(verb, frozenset())
                    )
                    # Forward/bind exemption is ssh-ONLY: scp/rsync have no
                    # forward options, and their `-r`/`-l` are valueless
                    # flags -- mis-classifying them as value-taking would hide
                    # the target of `scp -r localhost:…`.  Bundle-final
                    # spellings arm like the ``-4J`` precedent below, and the
                    # LETTER is recorded because ``-R`` is not exempt like its
                    # siblings (see ``_remote_forward_value_targets_self``).
                    forward_value_pending = (
                        verb == "ssh"
                        and not stripped.startswith("--")
                        and bool(bare)
                        and bare[-1] in _SSH_FORWARD_OPT_LETTERS
                    )
                    forward_value_letter = bare[-1] if forward_value_pending else ""
                    # The GLUED remote-forward spelling carries its spec in the
                    # same token (``-R2222:localhost:22``, ``-R2222``); ssh has
                    # no valueless ``-r``, so within the ssh verb a leading
                    # ``r`` with a spec-shaped remainder is the forward.  A
                    # valueless bundle prefix (``-vR2222:…``) is stripped
                    # first, the same treatment ``-o``/``-J`` get.
                    fwd_bare = bare.lstrip(_SSH_VALUELESS_SHORT_FLAGS)
                    if (
                        verb == "ssh"
                        and not stripped.startswith("--")
                        and len(fwd_bare) > 1
                        and fwd_bare[0] == "r"
                        and (":" in fwd_bare[1:] or fwd_bare[1:].isdigit())
                        and _remote_forward_value_targets_self(fwd_bare[1:])
                    ):
                        return True
                    # A DETACHED ``-J`` -- or a flag bundle ending in
                    # the jump letter (``-4J``) -- takes the NEXT token as its
                    # ProxyJump hop chain.  The attached spelling (``-Jhost``)
                    # is handled by ``_routing_option_value_targets_self``.
                    if not stripped.startswith("--") and bare.endswith("j"):
                        proxyjump_value_pending = True
                        forward_value_pending = False
                        forward_value_letter = ""
                elif forward_value_pending:
                    # The value of a forward/bind option (see
                    # ``_SSH_FORWARD_OPT_LETTERS``) names a listen address or
                    # a far-side hop for every letter EXCEPT ``r``: a
                    # remote-forward destination is dialed FROM HERE, so its
                    # spec is checked instead of exempted (the branch table
                    # lives on ``_remote_forward_value_targets_self``).
                    if forward_value_letter == "r" and _remote_forward_value_targets_self(
                        _unmask_separators(stripped)
                    ):
                        return True
                    forward_value_pending = False
                    forward_value_letter = ""
                    option_shadow = False
                    value_shadow = False
                else:
                    # An operand, or the value of the preceding option.  Check
                    # it either way (fail-closed — see the docstring); only an
                    # UNSHADOWED operand consumes the ssh/sftp positional slot.
                    checkable = positional_pending or verb in ("scp", "rsync")
                    if checkable and _operand_targets_self(
                        _unmask_separators(stripped),
                        host_position=positional_pending
                        and (not value_shadow or value_shadow_ambiguous),
                    ):
                        return True
                    if _routing_option_value_targets_self(
                        _unmask_separators(stripped), value_slot=option_shadow
                    ):
                        return True
                    if not value_shadow:
                        # Same consumption rule as the redirect
                        # branch above.
                        positional_pending = False
                    option_shadow = False
                    value_shadow = False
                    value_shadow_ambiguous = False
                depth += _substitution_depth_delta(arg)
                if depth <= 0 and _ends_argv(arg):
                    break
                depth = max(depth, 0)
        # End of frame: a leading RSYNC_RSH selector that SURVIVED to here was
        # consumed by the frame's command (which may spawn a nested payload),
        # and an exported one persists for the whole line -- either way, if it
        # is self-targeting, later frames inherit it.  Latch it now (never
        # cleared) so a nested ``sh -c 'rsync ...'`` frame denies.  Same
        # precedence as the in-walk rsync check: pending, else exported.
        for env_name in ("rsync_rsh", "rsync_connect_prog"):
            carried_now = rsync_rsh_pending.get(env_name, rsync_rsh_exported.get(env_name))
            if carried_now is not None and _is_ssh_to_self(carried_now):
                rsync_rsh_carried_self = True
    return False
