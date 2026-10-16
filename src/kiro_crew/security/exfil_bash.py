"""The data-egress and reverse-shell command gate.

The exfiltration-specific subset of ``SUSPICIOUS_BASH_PATTERNS``, enforced at the
tool-invocation gate: command shapes that push local data out or open a shell to
a remote host. Every branch carries the id of the catalog rule it enforces, so an
operator opt-out of that rule skips it, and a ``-d @`` hit in a plain ``date`` or
inert-search command is let through by the deny tier's single-command eligibility
test.

:mod:`kiro_crew.security.exfil` is the import path of every name defined here.
"""

from __future__ import annotations

import fnmatch
import re

from .denied_rules import _INERT_SEARCH_VERBS, _exception_eligible
from .shell_commands import ShellWord as _ShellWord
from .shell_commands import read_commands as _read_commands
from .shell_normalizer import _matching_close_paren

# Data-egress / reverse-shell command shapes — the exfiltration-specific subset
# of SUSPICIOUS_BASH_PATTERNS. These are enforced at the
# tool-invocation gate (denied), unlike the full SUSPICIOUS_BASH_PATTERNS list
# which stays advisory: that list also carries destructive-but-local shapes
# (rm -rf, dd if=, chmod on system dirs, DROP TABLE) that a user may legitimately
# run in their own workspace, so hard-denying all of them at the gate would break
# ordinary use. This subset is narrowly the "push local data OUT / open a shell
# to a remote" shapes, where a hijacked-agent block is worth the rare false
# positive.
#
# Entries containing `*` are fnmatch globs (`*<pat>*`); the rest are
# case-insensitive substrings, so they fire regardless of intervening flags /
# token layout — `curl -d @f`, `curl -s -d @f`, `curl --data-binary @f` all
# match. The `@` sigil on curl body/upload flags means "read from a local file"
# (the tell-tale of egress); a bare `-d 'x=1'` inline body has no `@` and is not
# matched. curl long options accept BOTH ` @` and `=@` separators, so both are
# listed. `--data-raw` is deliberately EXCLUDED: it is the one --data variant
# that does NOT interpret a leading `@` as a file reference, so `--data-raw @x`
# posts the literal string `@x` (never reads a file) — including it would only
# add false positives. Multipart uploads use a glob (`-F *=@`) so ANY field name
# matches, not just a field literally named `file` (`curl -F x=@secret` exfils
# just as well).
_BASH_EXFIL_PATTERNS: list[str] = [
    "-d @",  # curl POST body read from a local file (space + `=` separators)
    "-d@",
    "-d=@",
    "--data @",
    "--data=@",
    "--data-binary @",
    "--data-binary=@",
    "--data-ascii @",
    "--data-ascii=@",
    "--data-urlencode @",  # also reads a local file when the value starts with @
    "--data-urlencode=@",
    "-F *=@",  # curl multipart file upload, any field name (glob)
    "--form *=@",
    "--upload-file",  # curl upload, long form
    "wget --post-file",  # wget file upload
    "/dev/tcp/",  # bash builtin reverse shell (>/dev/tcp/host/port)
    "/dev/udp/",
]

# Exfil shapes where whitespace or flag CASE around an operator matters, so a
# plain lowercased substring/glob would either miss a no-space variant or
# false-positive. Matched via regex against the ORIGINAL (non-lowercased)
# command. Each entry is (compiled pattern, human label).
_BASH_EXFIL_RES: list[tuple[re.Pattern[str], str]] = [
    # netcat reading a local file via input redirect — `nc host port < file` AND
    # `nc host port <file` (no space after `<`, a valid shell redirect that the
    # old `nc * < ` glob missed). `nc`/`ncat` is anchored at a word boundary so
    # `sync`/`func` etc. do not match. Case-insensitive (command name).
    (re.compile(r"(?:^|\s)nc(?:at)?\s+\S.*<", re.IGNORECASE), "nc/ncat file redirect"),
    # netcat reverse shell `nc -e <prog>` / `ncat -e <prog>`. `nc`/`ncat` is
    # anchored at a word boundary so `rsync -e ssh` (contains `nc -e`) and
    # `vnc -e` do NOT match; a plain substring `"nc -e"` false-positived on them.
    (re.compile(r"(?:^|\s)nc(?:at)?\s+-e\b", re.IGNORECASE), "nc/ncat reverse shell"),
    # curl upload short form `-T <file>` / `-Tfile` (no space). CASE-SENSITIVE
    # `-T`: curl's upload flag is uppercase, so this does NOT match lowercase long
    # options such as `--trace-time`. `-T` must begin at a word boundary.
    (re.compile(r"\bcurl\b.*(?:^|\s)-T\s*\S"), "curl -T upload"),
]


# Which catalog rule each always-on exfil branch enforces, so a denial maps back
# to a rule id and an operator opt-out is honoured. Patterns/labels absent from
# these maps stay unconditional.
_BASH_EXFIL_RULE_BY_PATTERN: dict[str, str] = {
    "-d @": "data-exfil-curl-file-body",
    "-d@": "data-exfil-curl-file-body",
    "-d=@": "data-exfil-curl-file-body",
    "--data @": "data-exfil-curl-file-body",
    "--data=@": "data-exfil-curl-file-body",
    "--data-binary @": "data-exfil-curl-file-body",
    "--data-binary=@": "data-exfil-curl-file-body",
    "--data-ascii @": "data-exfil-curl-file-body",
    "--data-ascii=@": "data-exfil-curl-file-body",
    "--data-urlencode @": "data-exfil-curl-file-body",
    "--data-urlencode=@": "data-exfil-curl-file-body",
    "-F *=@": "data-exfil-curl-multipart-upload",
    "--form *=@": "data-exfil-curl-multipart-upload",
    "--upload-file": "data-exfil-curl-upload",
    "wget --post-file": "data-exfil-wget-post-file",
    "/dev/tcp/": "reverse-shell-devtcp",
    "/dev/udp/": "reverse-shell-devtcp",
}

# A single regex can span more than one catalog row, so this maps to a TUPLE. The
# gate attributes each MATCH to one of those rows and honours that row's own
# toggle — see _exfil_rule_id_for_match.
_BASH_EXFIL_RULE_BY_LABEL: dict[str, tuple[str, ...]] = {
    "nc/ncat file redirect": ("data-exfil-nc-file-redirect",),
    "nc/ncat reverse shell": ("reverse-shell-nc", "reverse-shell-ncat"),
    "curl -T upload": ("data-exfil-curl-upload",),
}

#: For a label whose regex spans several catalog rows, the token that identifies
#: WHICH row a given match belongs to. Ordered longest-first so ``ncat`` is tested
#: before ``nc`` — the reverse would classify every ``ncat`` hit as ``nc``.
_BASH_EXFIL_ROW_DISCRIMINATORS: dict[str, tuple[tuple[str, str], ...]] = {
    "nc/ncat reverse shell": (("ncat", "reverse-shell-ncat"), ("nc", "reverse-shell-nc")),
}


def _exfil_rule_id_for_match(label: str, matched: str, rule_ids: tuple[str, ...]) -> str:
    """The catalog row a single exfil match belongs to.

    One regex can cover more than one row, and the operator toggles rows, not
    regexes — so a match has to be attributed before its toggle can be honoured.
    Falls back to the label's first row when nothing discriminates, which keeps the
    single-row labels (the common case) on their existing behaviour and never
    returns an id outside ``rule_ids``.
    """
    low = matched.lower()
    for token, rid in _BASH_EXFIL_ROW_DISCRIMINATORS.get(label, ()):
        if token in low and rid in rule_ids:
            return rid
    return rule_ids[0]


# `-d @` is a bare substring, so it also fires on benign commands that egress
# nothing (GNU `date` converting an epoch, `grep` for the literal `-d @`). The
# denial stays on the text — fail-closed, never anchored to curl, since no text
# matcher models bash. The carve-out relaxes it only for a command the
# deny-exception gate judges a single plain command whose first word runs no
# subcommand, so a `-d @` in it cannot reach an HTTP client.
_DATA_EXFIL_FILE_BODY_RULE = "data-exfil-curl-file-body"

#: `date` plus the inert-search verbs. `date` is local, not added to
#: `_INERT_SEARCH_VERBS`, which must not grow the `rm -rf /` deny-exception.
_CARVEOUT_VERBS = frozenset((*_INERT_SEARCH_VERBS, "date"))


def _data_at_carveout_allows(command: str) -> bool:
    """A `-d @` hit in a benign `date`/`grep` command, not an upload: reuses
    `denied_rules._exception_eligible` (the blessed single-plain-command test)
    and requires a first word in `_CARVEOUT_VERBS`, which runs no subcommand.

    The first word is the shared reader's RAW word, split at live space and tab
    only (never NBSP), with its quotes kept: `'date'` is not the verb `date`."""
    if not _exception_eligible(command):
        return False
    words = _read_commands(command)[0].words
    return bool(words) and words[0].raw in _CARVEOUT_VERBS


# gh reads a request body from a LOCAL FILE through flags the substring table
# above does not name: `gh api --input <file>`, `--field k=@<file>`, and
# `gh release create|edit --notes-file <file>` (`-F <file>`). They are matched on
# shell words, and only where `gh` is a command's first word, so text that only
# mentions a flag (`echo …`, `grep …`, a later command's own `--input`) is left
# alone. Stdin (`-`, `k=@-`) names no file and stays allowed. `-f`/`--raw-field`
# never reads a file.
_GH_FILE_BODY_RULE = "data-exfil-gh-file-body"
_GH_FILE_BODY_SUBCOMMANDS = frozenset({"api", "repo", "release"})
#: gh global flags that take the next word as their value.
_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
#: Per subcommand, the gh options whose next word is a plain value (gh's own
#: flag tables; a short letter means different things per subcommand: `-d` is
#: `repo --description` but `release --draft`).
_GH_VALUE_OPTIONS: "dict[str, frozenset[str]]" = {
    "api": _GH_VALUE_FLAGS
    | frozenset(
        {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-q", "--jq"}
        | {"-t", "--template", "-p", "--preview", "--cache"}
    ),
    "repo": _GH_VALUE_FLAGS
    | frozenset(
        {"-d", "--description", "-h", "--homepage", "--add-topic", "--remove-topic"}
        | {"--default-branch", "--visibility", "-g", "--gitignore", "-l", "--license"}
        | {"-t", "--team", "-p", "--template", "-s", "--source", "-r", "--remote"}
        | {"-u", "--upstream-remote-name", "--fork-name", "-b", "--branch"}
    ),
    "release": _GH_VALUE_FLAGS
    | frozenset(
        {"-t", "--title", "-n", "--notes", "--target", "--tag", "--notes-start-tag"}
        | {"--discussion-category", "-L", "--limit", "-O", "--output", "--pattern"}
        | {"-D", "--dir", "-A", "--archive", "-q", "--jq", "-T", "--template"}
    ),
}
#: Bash reserved words that may stand before a command's program word
#: (`{ gh …; }`, `then gh …`, `do gh …`, `! gh …`, `time gh …`).
_LEADING_WORDS = frozenset({"{", "!", "if", "then", "else", "elif", "while", "until", "do", "time"})


def _word_text(word: "_ShellWord") -> str:
    """*word* as its program receives it: quotes, escapes and `\\<newline>` removed,
    and the `$` of `$'…'` / `$"…"` dropped. Nothing is expanded."""
    chars: "list[str]" = []
    state, dollar = 0, False
    for step in word.steps:
        if step.state != state:  # an opening or closing quote
            if dollar and state == 0:
                chars.pop()
            state = step.state
        elif step.text != "\\\n":
            chars.append(step.char)
        dollar = step.active and step.text == "$"
    return "".join(chars)


def _gh_argvs(command: str) -> "list[list[str]]":
    """Each of *command*'s commands as the words its program receives.

    Read by :func:`shell_commands.read_commands` with ``heredoc_bodies``: a
    redirection (a leading `2>/dev/null` included) is never a word, a here-document
    body is skipped and an unquoted one adds only its substitutions' commands, and
    every `$(…)` or backtick body is read once.
    """
    argvs = [[_word_text(w) for w in c.argv] for c in _read_commands(command, heredoc_bodies=True)]
    if _heredoc_unclear(command):
        argvs.extend(_flat_argvs(command))
    return argvs


_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([^\s;&|()<>'\"]*)\2")


def _heredoc_unclear(command: str) -> bool:
    """A `<<` the reader may misread, so the matcher fails closed: one after an unclosed
    `((`/`$((`, an unquoted-tag here-document inside a `$(…)`/backtick span, or a span
    whose computed close falls inside a here-document body (a lone `)` in the data).
    A quoted-tag body inside a span the reader closes after it is data."""
    closes: "dict[int, int]" = {}
    for doc in _HEREDOC_RE.finditer(command):
        at, before = doc.start(), command[: doc.start()]
        head = command[at + 2 : at + 258].replace("\\\n", "")  # live line joins
        word = re.match(r"-?[ \t]*([^\s;&|()<>]*)", head).group(1)  # type: ignore[union-attr]
        if not re.fullmatch(r"[\w.-]+|'[\w.-]*'|\"[\w.-]*\"", word):
            return True  # `$'…'`, `$"…"`, backslash or mixed quoting
        line = command.find("\n", doc.end()) + 1
        tabs = r"\t*" if doc.group(1) else ""  # only `<<-` strips leading tabs
        term = re.search("^" + tabs + re.escape(word) + "$", command[line:], re.M) if line else None
        if (
            word[0] not in "'\""
            and line
            and "\\\n" in command[line : line + (term.start() if term else len(command))]
        ):
            return True  # bash joins lines in an unquoted body
        if before.count("((") > before.count("))"):
            return True
        for opener in re.finditer(r"\$\(|`", before):
            start = opener.end()
            if start not in closes:  # each span's close is computed once per command
                tick = opener.group() == "`"
                closes[start] = (
                    command.find("`", start)
                    if tick
                    else start + _matching_close_paren(command[start:], 0)[0] - 1
                )
            close = closes[start]
            if 0 <= close < at:
                continue  # that span closed before this here-document
            line = command.find("\n", doc.end()) + 1
            end = re.search("^" + tabs + re.escape(doc.group(3)) + "$", command[line:], re.M)
            if not doc.group(2) or not line or end is None or close < line + end.end():
                return True
    return False


def _flat_argvs(command: str) -> "list[list[str]]":
    """The whole command as commands: split at every `;&|()`, backtick and newline, quotes
    and backslashes dropped, so no span or body hides a program word."""
    text = re.sub(r"[\\'\"]", "", command)  # then drop redirections and their targets
    text = re.sub(r"\d*(?:&>>?|[<>]+&?)[ \t]*[^\s;&|()`<>]*", " ", text)
    return [piece.split() for piece in re.split(r"[;&|()`\n]", text)]


def _is_assignment(word: str) -> bool:
    name, eq, _value = word.partition("=")
    return bool(eq) and name.isidentifier()


def _names_stdin(value: str) -> bool:
    """*value* reads no file on disk: empty (a process substitution `<( … )` is a
    redirection, so it leaves no word), `-`, `/dev/stdin` or `/dev/fd/N`."""
    return value in ("", "-", "/dev/stdin") or value.startswith("/dev/fd/")


def _gh_field_reads_file(spec: str) -> bool:
    """A gh field spec `k=@<file>`; `k=@-` and the other stdin names are not files."""
    _key, sep, value = spec.partition("=")
    return bool(sep) and value.startswith("@") and not _names_stdin(value[1:])


def _gh_short_cluster(word: str, value_options: "frozenset[str]") -> "tuple[str, bool]":
    """Read a short-flag cluster the way gh's flag parser does: booleans, then at
    most one flag that takes the rest of the word or, when none is left, the next
    word (`-dt TITLE`, `-tTITLE`). Returns the `-F…` part still to check (or
    ``""``) and whether the next word is a value. Any letter that is not a known
    value option counts as a boolean, so `-dF notes.md` still reaches `-F`."""
    for ci in range(1, len(word)):
        flag, rest = "-" + word[ci], word[ci + 1 :]
        if flag == "-F":
            return "-F" + rest, False
        if flag in value_options:
            return "", not rest
    return "", False


def _gh_release_action(args: "list[str]", value_options: "frozenset[str]") -> str:
    """The word after `release` that names its action (`create`, `download`, …)."""
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in value_options
        else:
            return word
    return ""


def _may_run_gh(command: str) -> bool:
    """A cheap test before :func:`_gh_file_body_flag`: `gh` appears once quotes,
    backslashes and newlines are removed, so `g\\h` and `g''h` (both run `gh`) pass."""
    return "gh" in re.sub(r"[\\'\"\n]", "", command.lower())


def _gh_file_body_flag(command: str) -> str:
    """The gh flag in *command* that sends a local file, or ``""``."""
    for words in _gh_argvs(command):
        i = 0
        while i < len(words) and (_is_assignment(words[i]) or words[i] in _LEADING_WORDS):
            i += 1
        if i >= len(words) or words[i].rsplit("/", 1)[-1] != "gh":
            continue
        args = words[i + 1 :]
        # Global flags may sit before the subcommand.
        j = 0
        while j < len(args):
            w = args[j]
            if w.startswith("-"):
                j += 2 if w in _GH_VALUE_FLAGS else 1  # a value flag (`-R o/r`) takes the next word
            elif j and args[j - 1].startswith("-") and "=" not in args[j - 1]:
                if w in _GH_FILE_BODY_SUBCOMMANDS:
                    break
                j += 1  # the value of a flag this walk does not know
            else:
                break
        if j >= len(args) or args[j] not in _GH_FILE_BODY_SUBCOMMANDS:
            continue
        release = args[j] == "release"
        value_options = _GH_VALUE_OPTIONS[args[j]]
        if release and _gh_release_action(args[j + 1 :], value_options) == "download":
            value_options = value_options | {"-p"}  # `--pattern` here, `--prerelease` on create
        # Global flags count (`gh --input f api …` gives api the flag); `-F` on
        # release means `--notes-file` only after the subcommand.
        # A known option's value (`--title '--input=x'`) is data, not a flag.
        skip = False
        for k, word in enumerate(args):
            if skip:
                skip = False
                continue
            skip = word in value_options
            if len(word) > 2 and word[0] == "-" and word[1] not in "-F":
                word, skip = _gh_short_cluster(word, value_options)
                if not word:
                    continue
            if release and k <= j and word.startswith("-F"):
                continue
            nxt = args[k + 1] if k + 1 < len(args) else ""
            name, eq, value = word.partition("=")
            if word.startswith("-F") and not word.startswith("--"):
                name, value = "-F", word[2:].removeprefix("=") or nxt
                skip = word == "-F"
            elif not eq:
                value = nxt
            if name in ("--input", "--notes-file", "--field"):
                skip = not eq
            if name == "--input" and not _names_stdin(value):
                return "--input"
            if release and name in ("--notes-file", "-F") and not _names_stdin(value):
                return "--notes-file"
            if not release and name in ("--field", "-F") and _gh_field_reads_file(value):
                return "--field"
    return ""


def audit_bash_exfiltration(
    command: str, *, enabled_ids: "frozenset[str] | None" = None
) -> str | None:
    """Return a denial reason if *command* matches a data-egress / reverse-shell
    shape that must be blocked at the tool-invocation gate, else None.

    Scoped to _BASH_EXFIL_PATTERNS / _BASH_EXFIL_RES (exfil/reverse-shell only) so
    it can be wired into the deny path in ``hooks.on_tool_call`` without blocking
    benign local commands. The broader :func:`audit_bash_command` stays advisory.

    Every branch carries the id of the catalog rule it enforces, so *enabled_ids*
    lets the caller honour an operator opt-out: a branch whose rule the operator
    disabled is skipped. ``None`` (the default) means ALL enabled — fail-closed,
    which is what keeps the callers that hold no effective set (cron command
    vetting, computer-use input vetting) at full strength without a change.
    """
    lower = command.lower()

    def _on(rule_id: str) -> bool:
        return enabled_ids is None or rule_id in enabled_ids

    carveout_allows: bool | None = None
    for pattern in _BASH_EXFIL_PATTERNS:
        rule_id = _BASH_EXFIL_RULE_BY_PATTERN.get(pattern, "")
        if rule_id and not _on(rule_id):
            continue
        pat = pattern.lower()
        if "*" in pat:
            if fnmatch.fnmatch(lower, f"*{pat}*"):
                return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
        elif pat in lower:
            # Fail-closed denial, except an inert `date`/`grep` command (computed
            # once per command and reused).
            if rule_id == _DATA_EXFIL_FILE_BODY_RULE:
                if carveout_allows is None:
                    carveout_allows = _data_at_carveout_allows(command)
                if carveout_allows:
                    continue
            return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
    for rx, label in _BASH_EXFIL_RES:
        rule_ids = _BASH_EXFIL_RULE_BY_LABEL.get(label, ())
        if not rule_ids:
            if rx.search(command):
                return f"Blocked: command matches data-exfiltration pattern ({label})"
            continue
        # A label can span more than one catalog row (one regex covers both the nc
        # and ncat rules). Denying while EITHER is enabled defeats the operator:
        # switching `reverse-shell-nc` off left `nc` blocked by its sibling. So
        # resolve each MATCH to the row it actually belongs to and honour that
        # row's own toggle. Every match is examined, not just the first, because a
        # command can carry both spellings and the leading one may be the disabled
        # row while the other is still enforced.
        for m in rx.finditer(command):
            matched_id = _exfil_rule_id_for_match(label, m.group(0), rule_ids)
            if _on(matched_id):
                return f"Blocked: command matches data-exfiltration pattern ({label})"
        continue
    if _on(_GH_FILE_BODY_RULE) and _may_run_gh(command):
        flag = _gh_file_body_flag(command)
        if flag:
            return f"Blocked: command matches data-exfiltration pattern (gh {flag} from file)"
    return None
