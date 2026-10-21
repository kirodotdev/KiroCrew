"""A crewmate's notes: the briefing file it keeps for its future self.

The text injected as the crewmate's ``[CURRENT ASSIGNMENT]`` section lives in
``members/<slug>/briefing.md`` (:data:`kiro_crew.members.BRIEFING_FILE_NAME`).
A crewmate's chat runs on the conductor, which has no file-writing tool, so it
keeps the file through ``save_notes`` (the ``kirocrew-mate`` server), and the
gateway writes it here. The read side is ``members.read_member_briefing``.

The directory is agent-writable, so the write uses the same open-time controls
the read does: the members root is resolved once, the member's own directory
component is appended lexically, and the leaf is opened with ``O_NOFOLLOW``
under the pinned parent, so a symlink anywhere on the path is refused rather
than followed. Platforms without those controls (Windows) cannot read a
briefing either, so the write refuses there too.
"""

from __future__ import annotations

import os

from kiro_crew import members
from kiro_crew.pinned_fs import PinnedPathRefusal, open_in_pinned_parent


class NotesError(Exception):
    """A notes write the gateway refuses, with a stable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def write_member_notes(slug: str, text: str) -> int:
    """Replace the member's briefing with *text*; return the characters written.

    *text* is the whole new briefing (stripped); an empty one clears it. Longer
    than :data:`members.MEMBER_BRIEFING_MAX_CHARS` is refused, because only that
    much is ever injected. Blocking file IO; callers run it off the loop.
    """
    if not isinstance(text, str):
        raise NotesError("invalid_notes", "notes are text")
    text = text.strip()
    if len(text) > members.MEMBER_BRIEFING_MAX_CHARS:
        raise NotesError(
            "notes_too_long",
            f"notes are at most {members.MEMBER_BRIEFING_MAX_CHARS} characters",
        )
    if not members.member_briefing_supported():
        raise NotesError("notes_unsupported", "notes are not available on this platform")
    members.validate_slug(slug)
    root = members.members_root().resolve()
    parent = root / slug
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        # The member's own directory, created without following a link at its name.
        try:
            os.mkdir(parent, 0o700)
        except FileExistsError:
            pass
        fd = open_in_pinned_parent(
            str(parent),
            members.BRIEFING_FILE_NAME,
            flags=os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
            mode=0o600,
            what="member briefing",
        )
    except (PinnedPathRefusal, OSError) as exc:
        raise NotesError("notes_unwritable", "the notes file could not be opened") from exc
    try:
        data = (text + "\n").encode("utf-8") if text else b""
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except OSError as exc:
        raise NotesError("notes_unwritable", "the notes could not be written") from exc
    finally:
        os.close(fd)
    return len(text)
