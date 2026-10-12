"""Redaction for a Side Chat reply that tool rows split into text rows.

Owned by ``dashboard/handlers/side.py``, which draws the rows; see
``docs/system-specs/modules/side.md`` for the wire and the mechanism.
"""

from __future__ import annotations

import os
import re

from kiro_crew.security import StreamRedactor
from kiro_crew.security import redact
from kiro_crew.security import redact as _batch_redact

# The stream redactor holds back one trailing credential-class run, plus the
# shapes its own anchors know (a Bearer header, a ``token=`` parameter, a JWT).
# Every pattern the batch pass knows is matched inside one such run except
# three, whose match waits for text the model may write much later and spans
# bytes outside that class: a key block needs its footer (or body lines the
# truncated-block form knows) and has newlines inside; a URL is judged whole (a
# password needs the ``@`` that ends it, an exfiltration URL its payload) and
# its path or query may hold a ``!`` or a ``$``; a labelled value (an AWS field,
# ``aws_secret_access_key: ``; an ``Authorization: Bearer `` header) is folded
# with its label into one tag, and the space after the label commits the label
# alone (the stream redactor's own Bearer anchor lets go of the header once a
# byte outside its value class, a quote, follows). Text inside an open one is
# held back here too, from the header, the scheme or the label, until the
# pattern can be judged: a key block until its footer, a URL until the
# whitespace that ends it, a label until its value's first byte.
_KEY_BLOCK_HEADER_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_KEY_BLOCK_FOOTER_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_OPEN_URL_RE = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://\S*\Z")
_OPEN_LABEL_RE = re.compile(
    r"(?:(?:SecretAccessKey|aws_secret_access_key|SessionToken|aws_session_token"
    r"|AccessKeyId|aws_access_key_id)[\"']?\s*(?:[:=]\s*[\"']?)?"
    r"|(?i:Authorization)[\"']?\s*(?:[:=]\s*[\"']?(?:(?i:Bearer)\s*)?)?)\Z"
)

# Up to this many committed characters every commit gets its own whole-text
# pass. Past it a pass waits for ``committed // 32`` new characters (at most
# ``_PASS_STEP_MAX``), so a long reply costs a bounded number of passes, each
# over the reply so far, instead of one per chunk.
_PASS_EVERY_COMMIT_BELOW = 8_192
_PASS_STEP_MAX = 1_024


def _open_credential_start(text: str) -> int | None:
    """Offset of a key block, URL or value label ``text`` opens and does not
    close, else ``None``. Everything from it must wait for the text that
    decides it."""
    starts = []
    for header in _KEY_BLOCK_HEADER_RE.finditer(text):
        if not _KEY_BLOCK_FOOTER_RE.search(text, header.end()):
            starts.append(header.start())  # the first header with no footer after it
            break
    for pattern in (_OPEN_URL_RE, _OPEN_LABEL_RE):
        match = pattern.search(text)
        if match is not None:
            starts.append(match.start())
    return min(starts) if starts else None


class _SegmentRedactor:
    """Redact a side reply that tool rows split into text rows.

    The main chat settles its one row with ``redact()`` over the whole reply.
    A tool row splits the side reply into rows, and a row cannot be redacted
    from its own text alone: a pattern that spans the split (a key header above
    the row, its base64 body below; ``postgres://user:pass`` above, ``@host``
    below) matches only with both halves in one text. So one pass over the
    whole reply so far decides every frame, and a row's text is what that pass
    produces past the rows above it.

    The :class:`StreamRedactor` inside says WHEN the pass may run: it withholds
    a trailing credential-class run the way the main chat's live stream does
    (``chat_runner._wsred``), and ``_open_credential_start`` extends that hold
    over the two shapes whose judgment waits for later text, so the text a pass
    runs over never ends inside a credential the batch patterns know. The
    stream redactor's own output is not drawn; the live view is the whole-text
    pass over the committed text, so a commit the pass rewrites (a key body
    committed under its header) never reaches the wire. The run still held when
    a tool row lands is neither flushed nor drawn: the text after the call
    decides it.

    The pass runs over the reply as written, with one substitution. A value the
    stream redactor DROPS (a credential still open at its hold-back ceiling,
    ``_STREAM_HOLDBACK_JWT_MAX``, or a complete ``token=`` value its cut
    bisects: it emits its tag and swallows the bytes, holding nothing) is a
    value the batch patterns need not see whole, so a pass over the bytes as
    written would hand them on (an unsigned JWT's claims, a bearer token's
    head). In the text the passes see (``_view``), the dropped bytes are the
    tag they were traded for; see ``_take``. The stored reply is the pass over
    that text too, so a dropped value stays dropped.

    What a frame can still carry is what ``redact()`` leaves in the reply as
    written so far. A row above a tool row is settled once and cannot be taken
    back, so the holds above keep everything a later byte could turn into a
    credential out of it; a key block the reply never closes is held to the
    end and judged there by the batch pass on what the reply has (the
    truncated-block form of the pattern), which is stricter than the main
    chat's live stream, which draws such a body as it comes.
    """

    __slots__ = (
        "_wsred",
        "_committed_through",
        "_view",
        "_len",
        "_wrote",
        "_passed",
        "_rows",
        "_settled",
        "_streamed",
    )

    def __init__(self) -> None:
        # The stream redactor commits through the batch function it is given;
        # ``_take`` reads what it committed off this hook.
        self._committed_through: list[tuple[str, str]] = []
        self._wsred = StreamRedactor(self._batch)
        self._view: list[str] = []  # the reply as the passes see it (``_take``)
        self._len = 0  # len("".join(_view))
        self._wrote = False  # whether the model wrote any text
        self._passed = 0  # committed length at the last live pass
        self._rows = 0  # tool rows drawn so far
        self._settled = ""  # the pass over the reply up to the row above
        self._streamed = ""  # live deltas drawn for the current row

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def wrote(self) -> bool:
        return self._wrote

    def _batch(self, text: str) -> str:
        """What the stream redactor commits through: the batch function it
        would use on its own (its commits are not the whole-text pass, which
        is ``redact`` above and may be stubbed in tests), recorded."""
        out = _batch_redact(text)
        self._committed_through.append((text, out))
        return out

    def _take(self, text: str) -> str:
        """Feed a delta to the stream redactor and keep ``_view`` in step with
        it; return what the stream redactor emitted (``""`` while it holds).

        ``_view`` holds the bytes the stream redactor has consumed from its
        buffer (the run it still holds is not in it yet), as written, except
        the bytes it DROPPED, which stand in the view as what it emitted for
        them. What it emits in a feed is the batch redaction of each run it
        commits (through ``_batch``, which records the run and its redaction)
        with its own tag where it drops a value: the feed that arms a drop
        emits the committed prefix's redaction and the tag for the value to the
        buffer's end (or for a complete token value its cut bisects), a feed
        that continues a drop emits nothing (or the tag again at its bound),
        and the feed that ends a drop swallows the run and commits the rest of
        the delta as usual. So the view takes each committed run as written in
        place of its redaction, and whatever else was emitted as it is. The
        rule is on what the redactor did, not on which path did it, so a path
        this docstring does not list is covered too.
        """
        self._committed_through.clear()
        out = self._wsred.feed(text)
        take = ""
        pos = 0
        for committed, redacted in self._committed_through:
            # The stream redactor emits each commit's redaction in order, with
            # only its own tags between them. A commit that cannot be found
            # where the previous one ended means that contract changed under
            # this code; failing the turn is safer than splicing text that is
            # not the reply (the handler reports the turn as interrupted).
            at = out.find(redacted, pos)
            if at < 0:
                raise RuntimeError("StreamRedactor emitted a commit out of order")
            take += out[pos:at] + committed
            pos = at + len(redacted)
        take += out[pos:]
        self._view.append(take)
        self._len += len(take)
        return out

    def _committed(self) -> str:
        """The text a pass may run over: the view (which never ends inside the
        run the stream redactor holds) minus an open key block or URL."""
        committed = "".join(self._view)
        cut = _open_credential_start(committed)
        return committed if cut is None else committed[:cut]

    def _share(self, whole: str) -> str:
        """The current row's share of a pass over the reply: what it produces
        past the rows above. Those rows were settled by a pass over a prefix of
        the same text. A later pass agrees with it except where the batch
        redactor's judgment of that prefix changed with the text after it (a
        header name folded into the tag of the value that followed the call),
        so the share starts where the two passes stop agreeing: the rewrite is
        drawn in this row, and the row above keeps what it was settled with."""
        if whole.startswith(self._settled):
            return whole[len(self._settled) :]
        return whole[len(os.path.commonprefix((whole, self._settled))) :]

    def feed(self, text: str) -> str:
        """Take a delta; return the part safe to draw now (possibly empty).

        A live pass is one ``redact()`` over the committed reply (about 1.4 ms
        per 10k characters, measured), paid on the event loop like the main
        chat's per-flush redaction. Past ``_PASS_EVERY_COMMIT_BELOW`` committed
        characters the pass waits for a step of new text (see ``_pass_step``):
        a 64k-character reply costs about 60 passes past that point, not one
        per chunk.
        """
        self._wrote = self._wrote or bool(text)
        if not self._take(text):
            return ""
        if self._len - self._passed < self._pass_step():
            return ""
        self._passed = self._len
        expected = self._share(redact(self._committed()))
        if not expected.startswith(self._streamed):
            # The pass rewrote text this row already shows. A delta cannot fix
            # that; the frame that settles the row replaces it.
            return ""
        out = expected[len(self._streamed) :]
        self._streamed = expected
        return out

    def _pass_step(self) -> int:
        """New committed characters a live pass waits for."""
        if self._passed < _PASS_EVERY_COMMIT_BELOW:
            return 1
        return min(_PASS_STEP_MAX, self._passed // 32)

    def close_row(self) -> str | None:
        """A tool row lands: settle the current row, start the next.

        Returns the row's text when it differs from the live deltas drawn for
        it (``None`` otherwise, nothing to send). The client draws every plain
        delta as sent and replaces the row only on a settle or terminal frame
        (``store/chat/side.ts``), so the deltas drawn are exactly ``_streamed``.
        """
        self._passed = self._len
        whole = redact(self._committed())
        row_text = self._share(whole)
        result = row_text if row_text != self._streamed else None
        self._settled = whole
        self._streamed = ""
        self._rows += 1
        return result

    def reply(self) -> str:
        """The whole reply redacted, as stored: the pass over the view plus the
        run the stream redactor still holds, nothing held back (a key block or
        URI the reply left open is judged by the batch pass on what the reply
        has, as the main chat's terminal frame judges it)."""
        return redact("".join(self._view) + self._wsred.held)

    def finish(self) -> str:
        """The terminal frame's text: ``reply()`` past the rows above."""
        return self._share(self.reply())
