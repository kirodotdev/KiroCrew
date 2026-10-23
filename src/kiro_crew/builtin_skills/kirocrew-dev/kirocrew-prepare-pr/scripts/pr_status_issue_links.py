"""Which issues a pull request body declares, and whether its closures are declared.

Loaded by ``pr_status.py``, which re-exports every name defined here; it is not run on
its own. It holds the explicit-trailer grammar (``Fixes #<n>``, ``owner/repo#<n>`` or an
issue URL), the Markdown masking that keeps code spans, fenced blocks and HTML comments
out of it, ``declared_issue_numbers`` (the entry point
``.github/scripts/issue_gate_refs.py`` reads through ``pr_status.py``) and
``closing_link_reason``, the advisory line the report prints. New work on how a body's
issue references are read goes here.

Standard library only, so the script stays runnable by a bare interpreter.
"""

import re

# A host closes an issue on merge ONLY for these verbs. "Related: #n", "Part of
# #n" and a bare "#n" render as links and close nothing, which is how finished
# work merges while its issue stays open forever.
_CLOSING_VERB = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
# A repository slug, in GitHub's own charset. Deliberately narrow so a stray
# path fragment cannot masquerade as a qualified reference.
_REPO_SLUG = r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
# The three reference targets the host actually resolves.
_ISSUE_TARGET = (
    r"(?:(?:" + _REPO_SLUG + r")?#\d+" r"|https?://[A-Za-z0-9.-]+/" + _REPO_SLUG + r"/issues/\d+)"
)
_CLOSING_REF = _CLOSING_VERB + r"[ \t]*:?[ \t]+" + _ISSUE_TARGET
# THE ACCEPTED EXPLICIT-TRAILER GRAMMAR, in full:
#
#   trailer := indent? bullet? ref (sep ref)* punct? html-comment?
#   ref     := verb ':'? sp target
#   verb    := close|closes|closed | fix|fixes|fixed | resolve|resolves|resolved
#   target  := '#123' | 'owner/repo#123' | 'https://host/owner/repo/issues/123'
#   sep     := ',' | ';' | 'and'
#
# Two properties are load-bearing.
#
# (1) The trailer must occupy the WHOLE visible line. An unanchored substring
# match also accepted prose that merely MENTIONS a past close -- "Fixed #123 in
# an earlier release; this PR only adds tests." -- and then told the author the
# keyword was fine and the NUMBER was wrong, the one reading that is never true
# for that line. A declaration is a trailer, not a mention. (Trailing
# whitespace, one sentence-ending '.'/';', a CR from a CRLF body, and a trailing
# HTML comment stay accepted, since none of them make the line prose.)
#
# (2) Qualified and URL targets are accepted. The host resolves them, so a body
# carrying one is NOT a body that forgot the verb. No reconciliation against the
# host's own repository identity is needed to accept them here: this classifier
# is only ever reached when the host resolved NOTHING, and the message it
# produces ("the verb is fine, check the reference") is correct whether the
# reference names this repository or another one.
#
# Every regex here runs against the MASKED body from
# ``_visible_markdown_prose``, never the raw one: a fenced block, an indented
# code block, an inline code span and an HTML comment are the surfaces GitHub
# itself does not resolve a closure from, and they are exactly where our own body
# template keeps its worked examples. Matching them would let a copied template
# read as a declaration the author never made.
#
# (3) INDENTATION IS CAPPED AT THREE COLUMNS, and the cap is the load-bearing
# half of that. Four columns is CommonMark's own code boundary, so this single
# bound refuses every code-indented example without the classifier needing to
# know which block type precedes it. Earlier revisions allowed any indent here
# and leaned entirely on the mask to tell code from prose, which required
# tracking whether a paragraph was open -- and that tracker was wrong for an ATX
# heading, a blockquote, a thematic break and a setext underline, each of which
# closes its block so the next indented line IS code. Enumerating block types
# converges on writing a Markdown parser; bounding the indent does not. A tab is
# refused for the same reason (it advances to the four-column stop).
#
# What the cap costs: a trailer indented four or more columns that GitHub WOULD
# resolve as lazy paragraph continuation is not credited as a declaration.
# That is the cheap direction -- it prints an advisory notice on an odd body,
# where the opposite error credits an EXAMPLE and silently suppresses a real
# unrelated-closure warning -- and a trailer is written as a whole line of its
# own, never as indented code.
_CLOSING_KW_RE = re.compile(
    r"^ {0,3}(?:[-*+][ \t]+)?"
    + _CLOSING_REF
    + r"(?:(?:[ \t]*[,;][ \t]*|[ \t]+and[ \t]+)"
    + _CLOSING_REF
    + r")*[ \t]*[.;]?[ \t]*(?:<!--.*?-->[ \t]*)?\r?$",
    re.IGNORECASE | re.MULTILINE,
)
# A resolved closure with no matching explicit trailer may come from prose or a
# manual link in the host UI, so every host-resolved issue number needs human
# confirmation unless the body DECLARES it. Declared numbers are read with the
# same grammar the accept path uses -- find each whole-line trailer with
# ``_CLOSING_KW_RE``, then read every reference on that line -- so one grammar
# governs both directions. Deriving them separately is what would report a
# legitimate trailer (bulleted, qualified ``owner/repo#n``, an issue URL, or
# several references on one line) as missing.
# The slug is CAPTURED, not merely matched: reconciling a host closure against
# a trailer on the number alone lets a stale ``Fixes other/repo#7`` vouch for a
# closure of THIS repository's own #7, which suppresses the very warning this
# function exists to print.
_CLOSING_REF_RE = re.compile(
    _CLOSING_VERB + r"[ \t]*:?[ \t]+"
    r"(?:(?P<slug>" + _REPO_SLUG + r")?#(?P<number>\d+)"
    r"|https?://[A-Za-z0-9.-]+/(?P<url_slug>" + _REPO_SLUG + r")"
    r"/issues/(?P<url_number>\d+))",
    re.IGNORECASE,
)
# Any issue-ish reference at all, to tell "forgot the verb" from
# "genuinely closes nothing". Mirrors the same three targets, so a qualified
# ref or an issue URL written without a verb is reported as a missing keyword
# rather than as a body with no issue link at all.
_BARE_REF_RE = re.compile(
    r"(?<![\w/])(?:" + _REPO_SLUG + r")?#\d+\b"
    r"|https?://[A-Za-z0-9.-]+/" + _REPO_SLUG + r"/issues/\d+\b",
    re.IGNORECASE,
)

# Issue-link classification must see the same Markdown surface GitHub treats as
# prose. Mask ignored contexts character-for-character so MULTILINE anchors and
# word boundaries keep their source positions. Fences allow up to three leading
# spaces; a backtick fence's info string cannot itself contain a backtick.
#
# Markdown's OTHER code block -- four or more columns of indentation -- is NOT
# tracked here, deliberately. Recognising it requires knowing whether a paragraph
# is open, which is a Markdown parser's job: an approximation of that state was
# wrong for an ATX heading, a blockquote, a thematic break and a setext underline,
# each of which closes its block so the next indented line IS code. The bound in
# ``_CLOSING_KW_RE`` covers every one of those shapes without consulting block
# state, so the state machine is not needed and is not here.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def _fence_opening(line):
    match = _FENCE_OPEN_RE.match(line)
    if not match:
        return None
    fence = match.group("fence")
    if fence[0] == "`" and "`" in match.group("info"):
        return None
    return fence[0], len(fence)


def _is_closing_fence(line, fence_char, opening_length):
    candidate = line[:-1] if line.endswith("\r") else line
    indent = 0
    while indent < len(candidate) and candidate[indent] == " ":
        indent += 1
    if indent > 3:
        return False
    end = indent
    while end < len(candidate) and candidate[end] == fence_char:
        end += 1
    return end - indent >= opening_length and not candidate[end:].strip(" \t")


def _mask_html_comments(line, in_comment):
    masked = list(line)
    cursor = 0
    if in_comment:
        comment_end = line.find("-->")
        if comment_end < 0:
            return " " * len(line), True
        cursor = comment_end + 3
        masked[:cursor] = " " * cursor

    while True:
        comment_start = line.find("<!--", cursor)
        if comment_start < 0:
            return "".join(masked), False
        comment_end = line.find("-->", comment_start + 4)
        if comment_end < 0:
            masked[comment_start:] = " " * (len(line) - comment_start)
            return "".join(masked), True
        cursor = comment_end + 3
        masked[comment_start:cursor] = " " * (cursor - comment_start)


def _mask_inline_code(text):
    runs = []
    cursor = 0
    while cursor < len(text):
        start = text.find("`", cursor)
        if start < 0:
            break
        end = start + 1
        while end < len(text) and text[end] == "`":
            end += 1
        runs.append((start, end))
        cursor = end

    next_equal: list[int | None] = [None] * len(runs)
    next_by_length: dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        run_length = runs[index][1] - runs[index][0]
        next_equal[index] = next_by_length.get(run_length)
        next_by_length[run_length] = index

    masked = list(text)
    index = 0
    while index < len(runs):
        closing_index = next_equal[index]
        if closing_index is None:
            index += 1
            continue
        start = runs[index][0]
        end = runs[closing_index][1]
        for offset in range(start, end):
            if masked[offset] not in "\r\n":
                masked[offset] = " "
        index = closing_index + 1
    return "".join(masked)


def _visible_markdown_prose(body):
    """Mask fenced/code-span/comment examples while preserving line offsets."""
    body = body or ""
    visible_lines = []
    fence_char = None
    fence_length = 0
    in_comment = False

    for line in body.split("\n"):
        if fence_char is not None:
            if _is_closing_fence(line, fence_char, fence_length):
                fence_char = None
                fence_length = 0
            visible_lines.append(" " * len(line))
            continue

        # A real fence opener wins over comment-looking text in its info
        # string. While inside a comment, masking happens first so a fence-like
        # example cannot alter fence state.
        opening = None if in_comment else _fence_opening(line)
        if opening is not None:
            fence_char, fence_length = opening
            visible_lines.append(" " * len(line))
            continue

        masked_line, in_comment = _mask_html_comments(line, in_comment)
        opening = None if in_comment else _fence_opening(masked_line)
        if opening is not None:
            fence_char, fence_length = opening
            visible_lines.append(" " * len(line))
            continue
        visible_lines.append(masked_line)

    return _mask_inline_code("\n".join(visible_lines))


# GitHub exposes ``Issue.number`` as a positive GraphQL Int. Normalize the
# host value and trailer text without converting untrusted digit strings to a
# Python int: Python 3.10 accepts arbitrarily long strings while 3.11+ raises,
# and readiness must behave identically on every supported interpreter.
_GITHUB_ISSUE_NUMBER_MAX = 2_147_483_647
_GITHUB_ISSUE_NUMBER_MAX_TEXT = "2147483647"


def _normalize_issue_number(value):
    """Return canonical decimal text for a GitHub issue number, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if not 0 < value <= _GITHUB_ISSUE_NUMBER_MAX:
            return None
        return str(value)
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdigit()
        or len(value) > len(_GITHUB_ISSUE_NUMBER_MAX_TEXT)
    ):
        return None
    normalized = value.lstrip("0")
    if not normalized:
        return None
    if (
        len(normalized) == len(_GITHUB_ISSUE_NUMBER_MAX_TEXT)
        and normalized > _GITHUB_ISSUE_NUMBER_MAX_TEXT
    ):
        return None
    return normalized


def _issue_number_sort_key(number):
    return (len(number), number)


def _normalize_repo_key(value):
    """Return canonical case-folded ``owner/repo`` text, else None.

    ``owner/repo`` is a GitHub API slug, NOT a filesystem path: the ``/`` is the
    API's own literal separator, so ``os.path.join`` would be wrong here (it
    emits a backslash on Windows). Built with ``format`` rather than ``+`` so
    the portability scanner's path-assembly rule does not read it as one.

    GitHub owner and repository names are case-insensitive, so the comparison
    must be too: otherwise ``Fixes KiroDotDev/KiroCrew#7`` would read as naming
    a different repository from the host's own ``kirodotdev/KiroCrew`` and the
    trailer would stop covering the closure it plainly declares.
    """
    if not isinstance(value, str):
        return None
    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name:
        return None
    return "{}/{}".format(owner.lower(), name.lower())


def _host_ref_repo_key(ref):
    """Return the case-folded ``owner/repo`` a host closing reference names."""
    if not isinstance(ref, dict):
        return None
    repository = ref.get("repository")
    if not isinstance(repository, dict):
        return None
    owner = repository.get("owner")
    login = owner.get("login") if isinstance(owner, dict) else None
    name = repository.get("name")
    if not isinstance(login, str) or not isinstance(name, str):
        return None
    return _normalize_repo_key("{}/{}".format(login, name))


def _undeclared_closing_numbers(declared, closing_refs):
    """Return (resolved numbers, numbers the body does not declare, well_formed).

    Accounting is per REPOSITORY, not per number, and the asymmetry between the
    two kinds of declaration is the whole point:

    * A QUALIFIED declaration (``Fixes owner/repo#7``, an issue URL, or a bare
      ``#7`` when the caller supplied the PR's own repository) covers every host
      reference naming that same repository.
    * A WILDCARD declaration -- a bare ``#7`` whose repository cannot be
      resolved because the caller passed none -- covers exactly ONE reference.
      One unqualified trailer cannot honestly vouch for two closures of the same
      number in two different repositories, so the second is reported.

    A host reference carrying no ``repository`` object is covered by any
    declaration of its number: nothing there can be reconciled in either
    direction, and inventing a mismatch would fire this advisory on a body that
    is fine.

    Deliberately absent: a "the same number resolved twice" notice. An earlier
    revision had one, and once matching became repository-aware it fired on a
    fully correct body -- ``Fixes #7`` plus ``Fixes other/repo#7`` with both
    closures resolved and both matched. Genuine ambiguity is already the
    undeclared case above, so the branch was redundant as well as wrong.
    """
    known: dict = {}
    wildcards: dict = {}
    for repo_key, number in declared:
        if repo_key is None:
            wildcards[number] = wildcards.get(number, 0) + 1
        else:
            known.setdefault(number, set()).add(repo_key)

    numbers = []
    undeclared = set()
    well_formed = True
    for ref in closing_refs:
        try:
            value = ref["number"]
        except (KeyError, TypeError):
            well_formed = False
            continue
        number = _normalize_issue_number(value)
        if number is None:
            well_formed = False
            continue
        numbers.append(number)
        host_repo = _host_ref_repo_key(ref)
        if host_repo is not None and host_repo in known.get(number, ()):
            continue
        if wildcards.get(number):
            wildcards[number] -= 1
            continue
        if host_repo is None and known.get(number):
            continue
        undeclared.add(number)
    return numbers, undeclared, well_formed


def _declared_closing_numbers(visible_body, base_repo=None):
    """Return (declared ``(repo_key, number)`` pairs, well_formed).

    ``repo_key`` is the case-folded ``owner/repo`` the trailer names explicitly,
    else *base_repo* for an unqualified ``#<n>`` (which can only mean the PR's
    own repository), else ``None`` when neither is known.

    ``well_formed`` is False when a trailer names a number the host could never
    have issued (out of GitHub's positive-Int32 range, or all zeros), which is
    reported rather than silently dropped: a body that declares a closure we
    cannot line up against the host's answer still needs a human to confirm the
    closures that DID resolve are the intended ones.
    """
    base_key = _normalize_repo_key(base_repo)
    declared = set()
    well_formed = True
    for line in _CLOSING_KW_RE.finditer(visible_body):
        for ref in _CLOSING_REF_RE.finditer(line.group(0)):
            number = _normalize_issue_number(ref.group("number") or ref.group("url_number"))
            if number is None:
                well_formed = False
                continue
            slug = ref.group("slug") or ref.group("url_slug")
            declared.add((_normalize_repo_key(slug) or base_key, number))
    return declared, well_formed


# The DECLARATION grammar: what a PR body says it is for. A declaration is a
# closing verb (the host auto-closes the issue on merge) OR a non-closing
# ``Refs`` / ``Part of`` (the issue stays open: partial work, a follow-up of
# several), each followed by one of the three issue targets. It STARTS a
# visible line (three columns of indent at most, an optional bullet) and what
# follows the reference on that line is free. Everything ``_CLOSING_KW_RE``
# refuses for being code or quotation -- four-column indent, a ``>``
# blockquote, a mid-sentence mention -- this refuses too, since none of them
# can start a line this way; what it accepts that ``_CLOSING_KW_RE`` does not
# is a trailer followed by prose (``Fixes #7 (the Windows half)``) and the
# non-closing verbs, which GitHub renders as links and never resolves as a
# closure -- the NOTICE path has no use for them, a gate that asks "which issue
# is this work for" does. Every verb is read at a word start: the line-start
# regex consumes the bullet/indent for the first one, and the lookbehind in
# ``_DECLARING_REF_RE`` guards the rest of the line, so ``unresolved: #2`` in
# the free tail is not ``resolved #2``.
_DECLARING_VERB = r"(?:" + _CLOSING_VERB[3:-1] + r"|refs?|part of)"
_DECLARING_LINE_START_RE = re.compile(
    r"^ {0,3}(?:[-*+][ \t]+)?(?=" + _DECLARING_VERB + r"[ \t]*:?[ \t]+)",
    re.IGNORECASE | re.MULTILINE,
)
# A URL target counts only on the github.com host: GitHub renders nothing from
# ``https://example.com/.../issues/N`` and a gate that credited it would be
# crediting a link to nowhere. (``_ISSUE_TARGET`` for the NOTICE path keeps
# any host, because that classifier only explains a body the host already
# resolved to nothing and its message is right either way.)
_DECLARING_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])" + _DECLARING_VERB + r"[ \t]*:?[ \t]+"
    r"(?:(?P<slug>" + _REPO_SLUG + r")?#(?P<number>\d+)"
    r"|https?://(?:www\.)?github\.com/(?P<url_slug>" + _REPO_SLUG + r")"
    r"/issues/(?P<url_number>\d+))",
    re.IGNORECASE,
)


def declared_issue_numbers(body, repo):
    """Return (numbers declared for ``repo``, well_formed) -- the public grammar.

    The ONE entry point other surfaces use to read which issues a PR body
    declares it is for (`.github/scripts/issue_gate_refs.py`, the Issue Gate's
    adapter, is its caller), so the masking and the reference grammar are never
    re-derived elsewhere: a semantic change here reaches every caller, where a
    caller rewrapping a private pattern drifts from it unnoticed.

    ``body`` is masked with ``_visible_markdown_prose`` first, then read with
    ``_DECLARING_LINE_START_RE`` / ``_DECLARING_REF_RE`` (stated above): a
    declaration starts a line and the rest of that line is free, every reference
    on it is read. A closing verb and a non-closing ``Refs`` / ``Part of`` both
    declare; only the former closes on merge. This is a different question from
    the one ``closing_link_reason`` answers (why the HOST resolved no closure),
    which keeps its own whole-line, closing-verbs-only classifier.

    Only references naming ``repo`` are returned (an unqualified ``#<n>`` can
    only mean the PR's own repository). ``well_formed`` is False when a
    reference names a number the host could never have issued, which the
    caller reports rather than drops. Numbers are canonical decimal strings,
    sorted numerically.
    """
    visible = _visible_markdown_prose(body)
    repo_key = _normalize_repo_key(repo)
    numbers = set()
    well_formed = True
    for start in _DECLARING_LINE_START_RE.finditer(visible):
        line_end = visible.find("\n", start.end())
        line = visible[start.end() : line_end if line_end >= 0 else len(visible)]
        for ref in _DECLARING_REF_RE.finditer(line):
            number = _normalize_issue_number(ref.group("number") or ref.group("url_number"))
            if number is None:
                well_formed = False
                continue
            slug = ref.group("slug") or ref.group("url_slug")
            if (_normalize_repo_key(slug) or repo_key) == repo_key:
                numbers.add(number)
    return sorted(numbers, key=_issue_number_sort_key), well_formed


def closing_link_reason(body, closing_refs, repo=None):
    """Return an advisory issue-link reason, else None.

    ADVISORY ONLY — the caller prints this, it never changes the exit code. An
    issue-less PR is legitimate, and a green PR should not be held on
    bookkeeping.

    ``closing_refs`` is the host's OWN resolution of the body (the
    ``closingIssuesReferences`` field), so it is the truth about what will
    actually close. The body regexes classify *why* it resolved to nothing --
    which is what makes the message actionable -- and, when it DID resolve
    something, whether every resolved issue is backed by an explicit trailer or
    whether a closure needs human confirmation. The accepted explicit-trailer
    grammar is stated in full above ``_CLOSING_KW_RE``: a whole visible line
    carrying one or more ``<verb> <target>`` references, where a target may be
    ``#123``, ``owner/repo#123`` or an issue URL.

    ``repo`` is the viewed PR's own ``owner/name`` when the caller knows it. It
    is what an unqualified ``#<n>`` trailer resolves to, so supplying it lets a
    resolved closure be matched by REPOSITORY AND NUMBER rather than by number
    alone. Optional, and omitting it only widens the match.
    """
    body = body or ""
    visible_body = _visible_markdown_prose(body)
    if closing_refs:
        declared, trailers_well_formed = _declared_closing_numbers(visible_body, repo)
        _resolved, missing_numbers, refs_well_formed = _undeclared_closing_numbers(
            declared, closing_refs
        )
        if refs_well_formed and trailers_well_formed and not missing_numbers:
            return None
        if not trailers_well_formed:
            return (
                "body has a malformed explicit closing trailer "
                "(confirm every closure is intentional; use one valid "
                "'Fixes #<n>' trailer per issue)"
            )
        if not refs_well_formed:
            return (
                "host returned a malformed closing issue reference "
                "(confirm every closure is intentional)"
            )
        missing_text = (
            " matching "
            + ", ".join(
                "#{}".format(number)
                for number in sorted(missing_numbers, key=_issue_number_sort_key)
            )
            if missing_numbers
            else " matching every host-resolved closure"
        )
        return (
            "host will close an issue but the body has no explicit closing trailer"
            + missing_text
            + " (confirm every closure is intentional; add one 'Fixes #<n>' "
            "trailer per issue)"
        )
    if _CLOSING_KW_RE.search(visible_body):
        # A visible verb is present but the host resolved nothing: the number,
        # repository target, or issue state does not form a live closure. A code
        # fence is not a candidate explanation -- fenced text is masked
        # before this runs, so it cannot reach here in the first place.
        return (
            "body has a closing keyword but the host resolved no issue "
            "(check the number, repository, and issue state)"
        )
    if _BARE_REF_RE.search(visible_body):
        return (
            "body references an issue with no closing keyword - use "
            "'Fixes #<n>' so it closes on merge (the Issue Gate lane also accepts "
            "'Part of #<n>' for partial work)"
        )
    return (
        "no issue link - add 'Fixes #<n>' (or 'Part of #<n>') for a triaged issue; "
        "the Issue Gate lane fails the PR without one once it is enabled"
    )
