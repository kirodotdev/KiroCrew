"""Local image references in text: the path grammar, and history scrubbing.

A LEAF module, for the same reason :mod:`kiro_crew.imaging` is one: two callers
need this and only one of them may import the ACP package.

* :func:`~kiro_crew.acp.prompt_blocks.build_prompt_blocks` turns a path in the
  CURRENT request into a real image block, and reads the grammar below to find
  one.
* :func:`strip_image_refs` neutralizes a reference in REPLAYED history, and is
  called from ``kiro_crew.context`` -- application code, which the
  agent-sdk-boundary gate forbids from importing ``kiro_crew.acp`` at all
  (``scripts/check_agent_sdk_boundary.py``; there is deliberately no inline
  opt-out).

Keeping one grammar for both matters more than which module hosts it: the
scrubber's whole guarantee is stated against what the builder would inline, so a
second copy of the pattern would be a path one of them leaves for the other.
``prompt_blocks`` re-exports the names it and its tests already use.

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

import logging
import os
import re

from kiro_crew.widget_parse import mask_inline_code

logger = logging.getLogger(__name__)

# Absolute paths ending in a supported raster suffix.
#
# Two properties are load-bearing, and BOTH were learned from real defects:
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
_SUFFIX_GROUP = r"(?:png|jpg|jpeg|gif|webp|bmp)"

#: Space and tab only -- NEVER `\s`. See note 2 above.
_PATH_CHARS = r"[\w./@~ \t()\-]"

#: Must not begin mid-token: rules out "https://host/..." and a "/" that is
#: already part of a longer path.
_NOT_MID_TOKEN = r"(?<![\w:/])"

_POSIX_PATH_RE = re.compile(
    rf"{_NOT_MID_TOKEN}(/{_PATH_CHARS}+?\.{_SUFFIX_GROUP})",
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
_WINDOWS_PATH_CHARS = r"[\w\\/.@ \t()\-]"
_WINDOWS_PATH_RE = re.compile(
    rf"(?<![\w:])(?:(?<![\w:/]))((?:[A-Za-z]:[\\/]|[\\/]{{2}}[^\\/:*?\"<>|\r\n]+[\\/])"
    rf"{_WINDOWS_PATH_CHARS}+?\.{_SUFFIX_GROUP})",
    re.IGNORECASE,
)

_PATH_RE = _WINDOWS_PATH_RE if os.name == "nt" else _POSIX_PATH_RE


#: Stands in for a local image reference in text that is NOT the current turn.
#:
#: It deliberately carries neither the path nor the alt text.
#:
#: The path is the harmful half twice over. When the file is still readable,
#: ``build_prompt_blocks`` re-inlines it -- so a picture an earlier compaction
#: already dropped comes back at full byte cost, on this turn and on every later
#: cold start, and the surrounding markdown is left mangled into
#: ``![alt]([image: name])`` because the substitution rewrites the destination
#: inside the link. When the file is absent -- a swept temp upload, a pruned
#: attachment, a sensitive-path refusal, an oversize or an undecodable file,
#: which are five separate skip branches in that builder -- no image block is
#: emitted at all and the path is left in the text verbatim.
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
#: ``build_prompt_blocks`` can afford the looser guard because its rewrite is
#: CONDITIONAL: it edits the text only after it has actually read a file, so an
#: unreadable URL-embedded path is left exactly as written. A substitution has
#: no such condition, and rewriting the inside of a URL is corruption rather
#: than scrubbing. The consequence is stated in :func:`strip_image_refs`.
_STANDALONE_LEAD_RE = re.compile(r"[\s(\[<\"']")


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
                chars[i] = " "
    return "\n".join(mask_inline_code(line) for line in "".join(chars).split("\n"))


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
    for match in _PATH_RE.finditer(masked):
        start = match.start(1)
        if start > 0 and not _STANDALONE_LEAD_RE.match(text[start - 1]):
            continue
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
        # and one it calls local is stripped here exactly as the builder would
        # inline it out of the current turn.
        if is_remote_destination(match.group(1)):
            continue
        spans.append((start, match.end(1)))
    return spans


def strip_image_refs(text: str) -> str:
    """*text* with every local image reference replaced by a content-free marker.

    The inverse of :func:`~kiro_crew.acp.prompt_blocks.build_prompt_blocks`, for
    text that is replayed or recalled HISTORY rather than the current request: a
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

    The bare-path pass is ``_PATH_RE`` -- the same pattern the builder reads, so
    the two cannot drift into a path this function leaves behind for that one to
    pick up -- narrowed by two conditions the builder does not need, because its
    rewrite happens only after a file was actually read while a substitution has
    no such condition:

    * code is masked (:func:`_mask_code_spans`), so a fenced or inline-code
      path is documentation and stays readable;
    * the path must stand alone (:data:`_STANDALONE_LEAD_RE`), so a path inside
      a URL query is left as part of its URL.

    Those two are corruption when rewritten, which is strictly worse than the
    residue of not rewriting them: a URL-embedded path naming a file that still
    exists can still be inlined out of a replayed row, exactly as it would be
    out of the current turn's text without this function. Narrowing here does
    not change that behaviour in either direction.

    Remote and ``data:`` references are left alone, matching
    ``iter_local_refs``: neither is a local path, so neither is inlined and a
    URL stays usable to a tool-capable agent. That agreement is enforced rather
    than assumed -- the bare-path pass calls the same ``is_remote_destination``
    predicate, because a protocol-relative ``//cdn/x.png`` is a path shape to
    BOTH grammars and only the predicate can tell a genuine URL from a stored
    UNC attachment on a roaming profile's share.

    Two residues remain, both inherited and both narrower than the builder's own
    behaviour rather than wider. ``_PATH_RE`` is platform-gated, so a bare
    Windows path in a transcript transferred to a POSIX host is not matched --
    it is not inlined there either, and the markdown shape is matched on both
    hosts. And escaped ``\\![x](...)`` markup and 4-space-indented code are not
    treated as code here, so a genuine absolute path inside one is replaced by
    the marker; the builder rewrites those same spans to ``[image: <name>]``
    whenever the file is readable, so this is that established rewrite extended
    to the unreadable case, on a per-build copy, with the on-disk row untouched.

    Reads no files and mutates nothing: it returns a new string.
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
