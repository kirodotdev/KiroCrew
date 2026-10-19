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

#: Bash splits words on space and tab only; `str.split()` also splits on
#: Unicode spaces such as NBSP, which would misread `date<NBSP>x` as `date`.
_BASH_BLANKS_RE = re.compile(r"[ \t]+")


def _data_at_carveout_allows(command: str) -> bool:
    """A `-d @` hit in a benign `date`/`grep` command, not an upload: reuses
    `denied_rules._exception_eligible` (the blessed single-plain-command test)
    and requires a first word in `_CARVEOUT_VERBS`, which runs no subcommand."""
    if not _exception_eligible(command):
        return False
    words = _BASH_BLANKS_RE.split(command.strip(" \t"))
    return words[0] in _CARVEOUT_VERBS


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
    return None
