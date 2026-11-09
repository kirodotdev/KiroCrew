"""The channel ``TurnDriver``'s stream filter for a quoted session-directive marker."""

from __future__ import annotations

from kiro_crew import session_directive


class QuotedDirectiveFilter:
    """Drop each streamed line that is only a session-directive marker the reply quotes.

    The model reads a directive tool's result with its marker, so a reply that
    repeats the result carries the marker line too; the effect was applied from
    the tool result. A line that could still be one is held to its newline (or
    :meth:`flush`, which ``TurnDriver`` also calls at a tool call, so the held text
    stays ahead of the card) and dropped when
    ``session_directive.strip_quoted_markers`` would drop it; every other line
    passes through as it streams.
    """

    __slots__ = ("_held", "_passing")

    def __init__(self) -> None:
        self._held = ""
        self._passing = False

    def feed(self, chunk: str) -> str:
        out: list[str] = []
        while chunk:
            cut = chunk.find("\n") + 1 or len(chunk)
            piece, chunk = chunk[:cut], chunk[cut:]
            if self._passing:
                out.append(piece)
                self._passing = not piece.endswith("\n")
                continue
            self._held += piece
            if piece.endswith("\n"):
                out.append(self.flush())
            elif not session_directive.may_be_quoted_marker(self._held):
                out.append(self._held)
                self._held, self._passing = "", True
        return "".join(out)

    def flush(self) -> str:
        line, self._held, self._passing = self._held, "", False
        body = line.removesuffix("\n")
        return line if session_directive.strip_quoted_markers(body) == body else ""
