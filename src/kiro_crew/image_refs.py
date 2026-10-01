"""Local image references in text: the path grammar, and history scrubbing.

A LEAF module, for the same reason :mod:`kiro_crew.imaging` is one: two callers
need this and only one of them may import the ACP package.

* :func:`strip_image_refs` neutralizes a reference in REPLAYED history, and is
  called from ``kiro_crew.context`` -- application code, which the
  agent-sdk-boundary gate forbids from importing ``kiro_crew.acp`` at all
  (``scripts/check_agent_sdk_boundary.py``; there is deliberately no inline
  opt-out). The path grammar below is what it reads.
* :func:`~kiro_crew.acp.prompt_blocks.build_prompt_blocks` re-exports
  :data:`STRIPPED_IMAGE_MARKER` for its callers and writes the OPPOSITE marker,
  ``[image: <name>]``. It reads no path grammar: an image block is built only
  from the structured attachment list the receiving channel supplies
  (:mod:`kiro_crew.prompt_attachments`), never from a path found in the text,
  because a path in text is a mention -- a ledger snapshot line, a nudge body,
  a quoted reply -- and re-inlining a mention on every automation cycle grows a
  request until the backend rejects it.

Keeping the grammar next to the scrubber matters because the scrubber's
guarantee is stated against the shapes a history row can hold, and the two
markers must stay distinct: one says "this picture is attached to this very
request", the other "this picture was not carried into this context".

IMPORT RULE, and it is load-bearing rather than stylistic: module scope reaches
nothing that reaches ``kiro_crew.acp``. ``prompt_blocks`` imports this module at
ITS module scope, so anything imported here that leads back to the ACP package
closes a cycle -- and it closes silently, because the order that breaks is the
one where this module is FIRST into the cluster, which no test importing
``prompt_blocks`` or ``context`` ever exercises. ``kiro_crew.messaging`` is
exactly such a path: its ``__init__`` pulls ``driver`` -> ``acp.types`` ->
``acp/__init__`` -> ``acp.client`` -> ``prompt_blocks`` -> back into this module
half-built, raising ``ImportError`` on ``_PATH_RE``. So the two scanners that
live under ``kiro_crew.messaging`` are imported where they are USED, and
``test_replay_image_refs`` pins a cold ``import kiro_crew.image_refs`` in a
subprocess so the rule cannot regress unnoticed. ``kiro_crew.widget_parse``
reaches no ACP module and stays at module scope.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
import tempfile

from kiro_crew.widget_parse import mask_inline_code

logger = logging.getLogger(__name__)

# Absolute paths ending in a supported raster suffix.
#
# Four properties are load-bearing:
#
# 1. The quantifier is non-greedy. A greedy `+` swallows the separator between
#    two paths, so "/tmp/a.png and /tmp/b.png" matched as ONE span ending at the
#    final ".png" -- not a file, so every image in a multi-image message was
#    dropped.
#
# 2. The character class holds HORIZONTAL whitespace only, and a lookbehind
#    forbids starting inside a URL or another path. With `\s` (which includes
#    "\n") a leading URL chained across the newline into the appended path:
#    `slack/events.py` emits "<user text>\n<image path>", so
#
#        see https://example.com/docs\n/tmp/a.png
#
#    matched as "//example.com/docs\n/tmp/a.png" -- one nonexistent path. Any
#    Slack message containing a link therefore lost its image. The `(?<![\w:/])`
#    guard rejects the "/" inside "https://" as a start position, which also
#    stops a URL that merely ends in ".png" from being probed as a local file.
#
# 3. An atomic path plus tail guards rejects longer names without swallowing a
#    separate path; a later separator across symbols marks a directory component.
#    With no later separator or image suffix, a glued non-ASCII run reads as
#    prose because nothing distinguishes it from a directory name mentioned alone.
#    Two paths glued by non-ASCII text with no space or punctuation between them
#    ("/tmp/a.png和/tmp/b.png") therefore read as ONE token: the builder inlines
#    nothing for it and the replay scrubber replaces it with one marker; a space
#    or a fullwidth comma between them keeps two pictures.
#
# 4. A later suffix in the same whitespace- and parenthesis-free run belongs
#    to this path; stopping early could attach a different picture at its prefix.
_SUFFIX_GROUP = r"(?:png|jpg|jpeg|gif|webp|bmp)"

# Bounds the path body and every lookahead scan. Space is legal inside a path,
# so without a bound a message of spaced fragments (" /a /a /a ... .png~") would
# be re-walked from every start before the tail guard refuses it; a path longer
# than this stays text.
_MAX_PATH_SCAN_CHARS = 512

#: Space and tab only -- NEVER `\s`. See note 2 above.
_PATH_CHARS = r"[\w./@~ \t()\-]"

# Scan through symbols so unsupported path characters cannot hide later separators.
# Punctuation ends a token: ASCII, Latin-1, General and Supplemental Punctuation,
# the arrow blocks, and the CJK/fullwidth punctuation sub-ranges. Whitespace and
# parentheses are the one deliberate overlap with the path bodies (a quoted path
# may hold them); no other break character is a path character, so letters and
# digits of every script continue a token. NUL is the scrubber's stand-in for
# masked code, so a code span ends a token too.
_TOKEN_BREAK = (
    r"\s\x00!\"#$%&'()*+,:;<=>?\[\]^`{|}"
    r"\u00a1\u00a7\u00ab\u00b6\u00b7\u00bb\u00bf"
    r"\u2010-\u2027\u2030-\u205e\u2190-\u21ff\u27f0-\u27ff\u2900-\u297f"
    r"\u2e00-\u2e2e\u2e30-\u2e7f"
    r"\u3000-\u3004\u3008-\u3020\u302a-\u3030\u3037\u303d-\u303f\u30fb"
    r"\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
)
_RUN_CHARS = rf"[^{_TOKEN_BREAK}]"

#: A path begins at the text start or after a token break, never mid-token:
#: rules out "https://host/...", a "/" already inside a longer path, and the
#: second of two paths glued by a symbol -- which would otherwise be inlined
#: alone while the first is dropped without a word.
_NOT_MID_TOKEN = rf"(?<![\w:/])(?<![^{_TOKEN_BREAK}])"
#: A longer name begins where a word character follows the suffix, directly or
#: after a joiner. A period before a CAPITAL letter is the one exception: file
#: extensions are lowercase and sentences start upper, so `/tmp/a.png.Then`
#: is a picture followed by prose, while `/tmp/a.png.backup` is another file
#: (the class opts out of the pattern's IGNORECASE so the case still counts).
_NOT_LONGER_NAME = (
    rf"(?![A-Za-z0-9_~]|[/@\-][A-Za-z0-9_]|(?-i:\.[a-z0-9_])"
    rf"|{_RUN_CHARS}{{0,{_MAX_PATH_SCAN_CHARS}}}?/)"
)
_NO_LATER_SUFFIX = rf"(?!{_RUN_CHARS}{{0,{_MAX_PATH_SCAN_CHARS}}}?\.{_SUFFIX_GROUP})"

_POSIX_PATH_RE = re.compile(
    rf"{_NOT_MID_TOKEN}((?>/{_PATH_CHARS}{{1,{_MAX_PATH_SCAN_CHARS}}}?"
    rf"\.{_SUFFIX_GROUP}{_NO_LATER_SUFFIX}))"
    rf"{_NOT_LONGER_NAME}",
    re.IGNORECASE,
)

# Windows absolute paths: a drive letter ("C:\...", "C:/...") or a UNC share
# ("\\\\host\\share\\..."). Temp attachments land in %LOCALAPPDATA%\Temp and
# dashboard uploads in %USERPROFILE%\.kiro\crew\uploads, so on Windows the
# POSIX grammar matched NOTHING and every image stayed prose -- then the temp
# file was deleted at end of turn, leaving a dead reference.
#
# Platform-gated rather than merged into one pattern: backslash and ":" are
# legal in POSIX filenames, so accepting Windows shapes everywhere makes prose
# like `the path C:\docs\logo.png is an example` a candidate -- and on Linux a
# file with that literal name can exist in the CWD, which would inline a file
# the user only mentioned. Matching the host's own grammar keeps that impossible.
#
# The UNC alternative accepts both separators after the leading pair
# (``\\host\share\...`` and ``//host/share/...``): the dashboard composer
# serializes image attachments with forward slashes (a markdown destination
# cannot carry raw backslashes -- CommonMark eats ``\`` before punctuation),
# and Windows file APIs accept the forward-slash form verbatim. The leading
# pair likewise accepts ``//``; ``(?<![\w:/])`` guards it from matching inside
# a URL's ``://``.
# ``~`` is load-bearing on Windows and was the bug this class was missing: a
# runner's ``%TEMP%`` resolves to the 8.3 SHORT name of its profile
# (``C:\Users\RUNNER~1\AppData\Local\Temp\...`` on GitHub Actions), and a user
# can equally be ``Admini~1``. Without the tilde the non-greedy ``+?`` could not
# cross it, so the whole path failed to match and every Windows attachment under
# such a temp dir stayed prose -- then its temp file was swept at end of turn,
# leaving a dead reference. The POSIX class has always held ``~``; this keeps the
# two grammars symmetric on the one character a real Windows temp path needs.
_WINDOWS_PATH_CHARS = r"[\w\\/.@~ \t()\-]"
# A drive colon glued into a token continues it: without this the colon ends
# the run and a second absolute path glued on by prose hides from both guards,
# so the first picture is attached alone where the POSIX grammar attaches none.
# Only a letter GLUED to the token counts -- preceded by a character that neither
# ends a token nor spells a word: after whitespace the drive begins its own path,
# and "https:" is a scheme, not a drive, so neither can carry a body from a
# non-image path through prose into a later picture.
_WINDOWS_DRIVE_COLON = rf"(?<=[^{_TOKEN_BREAK}A-Za-z0-9_][A-Za-z]):(?=[\\/])"
_WINDOWS_RUN_CHARS = rf"(?:{_WINDOWS_DRIVE_COLON}|{_RUN_CHARS})"
_WINDOWS_PATH_BODY = rf"(?:{_WINDOWS_DRIVE_COLON}|{_WINDOWS_PATH_CHARS})"
_WINDOWS_NOT_LONGER_NAME = (
    rf"(?![A-Za-z0-9_~]|[\\/@\-][A-Za-z0-9_]|(?-i:\.[a-z0-9_])"
    rf"|{_WINDOWS_RUN_CHARS}{{0,{_MAX_PATH_SCAN_CHARS}}}?[\\/])"
)
_WINDOWS_NO_LATER_SUFFIX = (
    rf"(?!{_WINDOWS_RUN_CHARS}{{0,{_MAX_PATH_SCAN_CHARS}}}?\.{_SUFFIX_GROUP})"
)
_WINDOWS_PATH_RE = re.compile(
    rf"(?<![\w:])(?:(?<![\w:/]))(?<![^{_TOKEN_BREAK}])((?>(?:[A-Za-z]:[\\/]"
    rf"|[\\/]{{2}}[^\\/:*?\"<>|\r\n]{{1,{_MAX_PATH_SCAN_CHARS}}}[\\/])"
    rf"{_WINDOWS_PATH_BODY}{{1,{_MAX_PATH_SCAN_CHARS}}}?\.{_SUFFIX_GROUP}"
    rf"{_WINDOWS_NO_LATER_SUFFIX}))"
    rf"{_WINDOWS_NOT_LONGER_NAME}",
    re.IGNORECASE,
)

_PATH_RE = _WINDOWS_PATH_RE if os.name == "nt" else _POSIX_PATH_RE


#: Stands in for a local image reference in text that is NOT the current turn.
#:
#: It deliberately carries neither the path nor the alt text.
#:
#: The path is the harmful half. The builder reads no path out of the text
#: (image blocks come only from the channel's structured attachment list), so
#: a path left in a replayed row is one of two things: a file that is gone -- a
#: swept temp upload, a pruned attachment -- or one the model cannot open,
#: sitting in the prose verbatim next to the assistant's own earlier
#: description of what it showed. Were the builder ever to inline a replayed
#: path, a picture an earlier compaction already dropped would come back at
#: full byte cost on every cold start, with the surrounding markdown mangled
#: into ``![alt]([image: name])``; the marker forecloses both readings.
#:
#: The alt text goes too, because a caption is indistinguishable from a
#: description: a model handed ``![the login error](...)`` with no picture has
#: prose asserting what the picture showed, which is the behaviour being fixed.
#:
#: Distinct from ``[image: <name>]``, which ``build_prompt_blocks`` writes to
#: mean the OPPOSITE -- that the picture is attached to this very request.
STRIPPED_IMAGE_MARKER = "[image not carried into this context]"

#: Cheap "could either grammar match at all" pre-test. Every row of every
#: history build pays this, so the two real scans below must not run unless a
#: raster suffix is present somewhere in the row.
_ANY_IMAGE_SUFFIX_RE = re.compile(rf"\.{_SUFFIX_GROUP}", re.IGNORECASE)

#: A bare attachment path STANDS ALONE: it opens the row, or follows whitespace
#: or an opening delimiter. ``_PATH_RE``'s own ``(?<![\w:/])`` guard only
#: forbids starting mid-token, which still admits a path embedded in a URL
#: query -- ``?src=/tmp/a.png`` is preceded by ``=``, which that guard permits.
#:
#: The builder never needed the tighter guard: it rewrites only a KNOWN
#: attachment path, after reading the file, so an unreadable URL-embedded path
#: was always left exactly as written. A substitution has no such condition,
#: and rewriting the inside of a URL is corruption rather than scrubbing. The
#: consequence is stated in :func:`strip_image_refs`.
#:
#: The opening delimiters are shared with :data:`_OPENERS_RE` so the two cannot
#: drift into a delimiter one of them treats as prose and the other as syntax.
_OPENING_DELIMS = r"(\[<\"'"
_STANDALONE_LEAD_RE = re.compile(rf"[\s{_OPENING_DELIMS}]")

#: What may precede a path that is still ALONE on its line: indentation, the
#: list, task-list and quote markers a reply sets a file list in, and Slack's
#: own bullet. A marker with no space after it is still rejected, one step
#: earlier, by :data:`_STANDALONE_LEAD_RE`: a path must follow whitespace or an
#: opening delimiter, and that guard is base behaviour this pass does not widen.
_LINE_LEAD_RE = re.compile(r"[ \t]*(?:(?:[-*+\u2022]|\d{1,9}[.)])[ \t]+|>[ \t]*|\[[ xX]\][ \t]+)*")

#: What may FOLLOW it and still leave it alone on its line: sentence
#: punctuation, then blanks. A word after it means the line is prose.
_LINE_TAIL_RE = re.compile(r"[.,;:!?]*[ \t\r]*")

#: The file name a channel's attachment writer mints, which is what lets a span
#: alone on its line vouch for itself. Every channel ingests through
#: ``messaging.attachments._make_temp`` -- a bare ``tempfile.mkstemp``: the
#: default prefix, eight characters of ``[a-z0-9_]``, and the image suffix
#: (kept, or replaced by the sniffed type's) -- and appends the result on its
#: own line. That name holds no whitespace, so a channel path carries a space
#: only where its temp DIRECTORY does (a Windows profile named ``John Smith``).
#: Being alone on a line is not enough by itself: ``/var/log/app has the broken
#: logo.png.`` is a whole line a person types, and it ends in a word, not in
#: this name.
_CHANNEL_ATTACHMENT_NAME_RE = re.compile(
    rf"{re.escape(tempfile.gettempprefix())}[a-z0-9_]{{8}}\.{_SUFFIX_GROUP}",
    re.IGNORECASE,
)

#: A markdown link destination in ANGLE form, which is how CommonMark spells a
#: destination holding a space -- and the only delimiter shape that vouches for
#: a spaced span. The bare ``](a b)`` form does not: this repo's own reader ends
#: such a destination at the first space (``outbound_files.md_destination``), so
#: the span is not one path to the code that consumes it. Nor does a quote pair:
#: ``"/var/log/app has the broken logo.png"`` is one sentence a person types, so
#: reading quotes as proof of a single path deletes it -- the defect this pass
#: exists to stop, not a shape to trade for coverage.
_LINK_DEST_OPEN = "](<"
_LINK_DEST_CLOSE = ">)"

#: The delimiters a path can be written inside, which therefore sit between a
#: span's last whitespace and the path itself rather than being part of it.
_OPENERS_RE = re.compile(rf"[{_OPENING_DELIMS}]*")


def _spaced_span_is_one_path(
    text: str, start: int, end: int, line_starts: list[int], lead_ends: dict[int, int]
) -> bool:
    """Whether *text*[start:end], which holds a space or tab, is ONE path by its shape.

    ``_PATH_CHARS`` admits horizontal whitespace because a real attachment name
    can carry it (``Screen Shot 2024.png``). Read with no "was a file actually
    read" gate in front of it, the class turns prose into a path: in ``check
    /var/log/app and tell me why logo.png is broken`` it spans from ``/var`` to
    ``.png``. So the shape alone vouches for a spaced span only where nothing
    else can be meant: it is what a channel appends -- alone on its line AND
    ending in the name the channel writer mints
    (:data:`_CHANNEL_ATTACHMENT_NAME_RE`) -- or it is a markdown link's
    angle-form destination, which holds nothing but a path.

    *line_starts* is every line's start offset, so both line bounds come from a
    bisection rather than a scan over a long single-line row. *lead_ends* caches,
    per line index, where the line's greedy :data:`_LINE_LEAD_RE` match ends:
    the lead check is a ``fullmatch`` from the line's start, so without the
    cache every candidate on a line re-reads that line's lead -- quadratic on a
    row with a long lead and many spaced spans. A lead that ends BEFORE *start*
    cannot fullmatch up to it (the greedy match is the longest the grammar
    admits from that line start, and every shorter match ends at or before it),
    so that one comparison settles every candidate but the one the lead
    actually reaches.
    """
    after = bisect.bisect_right(line_starts, start)
    line_start = line_starts[after - 1]
    # The next line's start, minus its newline -- never a forward scan, which on
    # a long single-line row costs as much as the whole strip.
    line_end = line_starts[after] - 1 if after < len(line_starts) else len(text)
    # The name first: it is the narrowest test, and prose fails it.
    name_start = max(text.rfind("/", start, end), text.rfind("\\", start, end)) + 1
    if _CHANNEL_ATTACHMENT_NAME_RE.fullmatch(text, name_start, end):
        lead_end = lead_ends.get(after)
        if lead_end is None:
            lead = _LINE_LEAD_RE.match(text, line_start)
            # The lead grammar admits the empty string, so the match never misses.
            lead_end = lead_ends[after] = lead.end() if lead is not None else line_start
        if (
            start <= lead_end
            and _LINE_LEAD_RE.fullmatch(text, line_start, start)
            and _LINE_TAIL_RE.fullmatch(text, end, line_end)
        ):
            return True
    return (
        text[start - len(_LINK_DEST_OPEN) : start] == _LINK_DEST_OPEN
        and text[end : end + len(_LINK_DEST_CLOSE)] == _LINK_DEST_CLOSE
    )


#: Stands in for a masked character. Outside ``_PATH_CHARS`` and
#: ``_WINDOWS_PATH_CHARS`` and inside ``_TOKEN_BREAK``, so it ends a candidate
#: and the run after one, does not start one, and does not stop the path right
#: after it from starting one.
#: The one place it can still sit INSIDE a candidate is the UNC host segment of
#: ``_WINDOWS_PATH_RE``, a negated class that admits it -- as it admitted the
#: space the mask wrote before -- so a code span inside a ``\\host\share``
#: prefix is not a boundary on Windows.
_MASKED = "\x00"


def _mask_code_spans(text: str, iter_fence_spans) -> str:
    """*text* with fenced blocks and inline code blanked, length preserved.

    Offsets from a scan of the result therefore index straight into *text*.
    Newlines are kept so the per-line inline pass still sees the real line
    structure. Both span rules are borrowed rather than re-spelled --
    ``iter_fence_spans`` is the whole-text view of the splitter's own fence
    machine, and ``mask_inline_code`` is the shared port of the frontend's
    balanced-backtick rule -- because a second spelling of either diverges on
    the next CommonMark fix.

    The fence scanner arrives as an argument because it cannot be imported at
    this module's scope (see the IMPORT RULE) and its caller already pays that
    deferred import once per call.
    """
    chars = list(text)
    for start, end in iter_fence_spans(text):
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = _MASKED
    fenced = "".join(chars)
    if "`" not in fenced:
        # Only a backtick run can make the inline pass change a character, and
        # the fence pass already wrote the sentinel, so there is nothing to
        # remap -- which is the common row, on a path that runs per history row.
        return fenced
    masked = "\n".join(mask_inline_code(line) for line in fenced.split("\n"))
    # ``mask_inline_code`` is the shared port and blanks with a SPACE, which
    # ``_PATH_CHARS`` admits -- so a candidate would run straight through a code
    # span and out the other side. Every character the mask changed becomes the
    # sentinel instead, which no path class holds, so code ends a candidate here.
    return "".join(_MASKED if m != o and m == " " else m for m, o in zip(masked, text, strict=True))


def _bare_path_spans(text: str) -> list[tuple[int, int]]:
    """Spans of *text* holding a standalone local image path, in order."""
    # Deferred: see this module's IMPORT RULE. kiro_crew.messaging.__init__
    # reaches kiro_crew.acp.types, and prompt_blocks imports this module at its
    # own module scope, so importing it above would close that cycle.
    from kiro_crew.messaging.outbound_files import is_remote_destination
    from kiro_crew.messaging.split import iter_fence_spans

    try:
        masked = _mask_code_spans(text, iter_fence_spans)
    except Exception:  # pragma: no cover - defensive: a scan must never break a turn
        logger.debug("image refs: code-span mask failed", exc_info=True)
        return []
    spans: list[tuple[int, int]] = []
    line_starts: list[int] | None = None
    # Per line index, where that line's greedy lead match ends: filled on the
    # first spaced candidate a line holds and read by every later one, so a
    # long lead is scanned once per line rather than once per candidate.
    lead_ends: dict[int, int] = {}
    for match in _PATH_RE.finditer(masked):
        start, end = match.span(1)
        spaced = max(masked.rfind(" ", start, end), masked.rfind("\t", start, end)) + 1
        if spaced:
            # The span's last token can be a path on its own -- "check /var/log
            # and /tmp/a.png" spans both -- and then THAT is the reference. It
            # has to be the whole token, bar an opening delimiter the token is
            # written inside ("why (/tmp/a.png) is broken"); a match further in
            # is only the tail of a longer name ("photos (1)/img.png" matches
            # from "/img.png"), and the full span stays the candidate. Any
            # sub-match ends where the span does: the grammar is non-greedy, so
            # the span's own suffix is the only one it holds.
            last = _PATH_RE.search(masked, spaced, end)
            if last is not None and _OPENERS_RE.fullmatch(masked, spaced, last.start(1)):
                start, spaced = last.start(1), 0
        if start > 0 and not _STANDALONE_LEAD_RE.match(text[start - 1]):
            continue
        raw = text[start:end]
        # A protocol-relative URL ("//cdn/x.png") is a path shape to both
        # grammars -- `_POSIX_PATH_RE` because it opens with "/", and
        # `_WINDOWS_PATH_RE` because "//" also spells a UNC share. Whether a
        # given "//" destination is remote is exactly the question
        # `is_remote_destination` exists to answer (a roaming profile's own
        # UNC attachment is local; an arbitrary share or URL is not), and
        # `iter_local_refs` already answers it through that predicate. Calling
        # the SAME predicate here makes the two passes agree by construction
        # rather than by coincidence, which is what the "remote references are
        # left alone" contract above actually requires -- testing the
        # `REMOTE_PREFIXES` tuple directly is the bug its own docstring warns
        # against, reading a stored UNC attachment as a remote URL. The
        # directions still match the builder's: a destination the predicate
        # calls remote is left in place (nothing answers `is_file()` for it),
        # and one it calls local is stripped here, the way the builder marks a
        # local attachment it inlined out of the current turn.
        if is_remote_destination(raw):
            continue
        if not spaced:
            spans.append((start, end))
            continue
        if line_starts is None:
            line_starts = [0, *(m.end() for m in re.finditer("\n", text))]
        # Whitespace with no shape to vouch for it: prose keeps every word.
        if _spaced_span_is_one_path(text, start, end, line_starts, lead_ends):
            spans.append((start, end))
    return spans


def strip_image_refs(text: str) -> str:
    """*text* with every local image reference replaced by a content-free marker.

    The counterpart of :func:`~kiro_crew.acp.prompt_blocks.build_prompt_blocks`
    for text that is replayed or recalled HISTORY rather than the current
    request: the builder marks a picture that IS attached to this request, this
    function marks one that is not. A
    history row names a picture that belonged to an earlier turn, and the two
    ways that reference can be read are both wrong (see
    :data:`STRIPPED_IMAGE_MARKER`). Replacing it with a marker is the "fully
    removed" half of the only two honest options, since a text vehicle cannot
    carry bytes.

    Both shapes a row can hold are covered, in the order that keeps them from
    overlapping: markdown ``![alt](dest)`` first, via the same
    :func:`~kiro_crew.messaging.outbound_files.iter_local_refs` scan the
    attachment store uses -- which is what the dashboard persists -- and then
    bare paths, which is what a Slack or Telegram inbound message appends.
    Doing markdown first means the second pass never sees a destination that
    was already inside a link.

    The bare-path pass is ``_PATH_RE``, the grammar of the paths a channel used
    to append (and a Slack or Telegram inbound message still appends, for agent
    tools), narrowed by two conditions a substitution needs because it has no
    "was a file actually read" gate in front of it:

    * code is masked (:func:`_mask_code_spans`), so a fenced or inline-code
      path is documentation and stays readable;
    * the path must stand alone (:data:`_STANDALONE_LEAD_RE`), so a path inside
      a URL query is left as part of its URL.

    Those two are corruption when rewritten, which is strictly worse than the
    residue of not rewriting them: a URL-embedded path naming a file that still
    exists stays in the replayed row as text -- the builder reads no path out of
    the text, so it is never inlined from there. Narrowing here does not change
    that behaviour in either direction.

    One more shape is read conservatively, because the grammar cannot settle it
    and the scrubber has no file to ask. A span holding a space or tab may be a
    spaced file name (``Screen Shot 2024.png``) or a stretch of prose (``check
    /var/log/app and tell me why logo.png is broken``). Its last token is tried
    as a path on its own first, and otherwise its shape has to vouch for it
    (:func:`_spaced_span_is_one_path`: a channel's own attachment line, or a
    markdown link's angle-form destination). The residue is every other spaced
    path -- a ``Screen Shot 2024.png`` a person typed, even alone on its line --
    which keeps its text: settling it needs either a filesystem probe -- a
    blocking call on the event loop, and the data home behind it can be a
    network share -- or a guess at the span's interior, whose own failure mode
    is deleting the prose this function exists to keep.

    Remote and ``data:`` references are left alone, matching
    ``iter_local_refs``: neither is a local path, and a URL stays usable to a
    tool-capable agent. That agreement is enforced rather than assumed -- the
    bare-path pass calls the same ``is_remote_destination`` predicate, because
    a protocol-relative ``//cdn/x.png`` is a path shape to BOTH grammars and
    only the predicate can tell a genuine URL from a stored UNC attachment on a
    roaming profile's share.

    Two residues remain, both inherited. ``_PATH_RE`` is platform-gated, so a
    bare Windows path in a transcript transferred to a POSIX host is not matched
    -- it is text on either host, and the markdown shape is matched on both.
    And escaped ``\\![x](...)`` markup and 4-space-indented code are not treated
    as code here, so a genuine absolute path inside one is replaced by the
    marker, on a per-build copy, with the on-disk row untouched.

    NOT for a caller with its own image handling. ``chat_title._title_text``
    keeps an ESCAPED or code-quoted ``![x](...)`` readable in a title prompt
    (``test_prompt_preserves_escaped_and_code_quoted_markdown_images``). A
    code-quoted one survives both passes here, but the bare-path pass replaces
    the destination inside an escaped one, deliberately, so that path strips
    image references its own way and does not call this.

    Reads no files and mutates nothing: it returns a new string. Every rule
    above is lexical, so a history build makes no filesystem call at all. That
    matters because this runs inline on the event loop, while the builder's
    own probes are deliberately offloaded (``acp.client`` runs it through
    ``asyncio.to_thread``): resolving the data home here would put a
    ``Path.resolve()`` on a user-set ``KIROCREW_HOME`` -- a network share on a
    roaming profile -- in front of every history row.
    """
    if not isinstance(text, str) or not text or not _ANY_IMAGE_SUFFIX_RE.search(text):
        return text
    # Deferred for the same reason as in _mask_code_spans: see the IMPORT RULE.
    from kiro_crew.messaging.outbound_files import iter_local_refs

    out = text
    try:
        refs = iter_local_refs(out)
    except Exception:  # pragma: no cover - defensive: a scan must never break a turn
        logger.debug("image refs: reference scan failed", exc_info=True)
        refs = []
    # Right-to-left, so an earlier reference's span stays valid after a later
    # one has been replaced -- the same order the attachment store rewrites in.
    for ref in reversed(refs):
        out = out[: ref.start] + STRIPPED_IMAGE_MARKER + out[ref.end :]
    for start, end in reversed(_bare_path_spans(out)):
        out = out[:start] + STRIPPED_IMAGE_MARKER + out[end:]
    return out
