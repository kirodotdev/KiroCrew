"""A crewmate's notes: the briefing file it keeps for its future self.

``members/<slug>/briefing.md``, written by the gateway for ``save_notes``, so a
crewmate keeps its notes whatever its template mounts. The directory is agent-writable, so the write
pins the parent and replaces the leaf with ``O_NOFOLLOW``; a symlink is refused
and a failed write keeps the old notes. Refused where those controls are absent.
"""

from __future__ import annotations

import os
import stat

from kiro_crew import members
from kiro_crew.atomic_write import atomic_write_at
from kiro_crew.member_folder import open_member_folder


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
    try:
        # ``members`` and the member's folder are opened once and held, so no name is
        # looked up again between the check and the write.
        with open_member_folder(slug) as folder:
            dir_fd = folder.fd
            if dir_fd is None:
                raise NotesError("notes_unwritable", "the notes file could not be opened")
            try:
                leaf = os.stat(members.BRIEFING_FILE_NAME, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                leaf = None
            except OSError as exc:
                raise NotesError("notes_unwritable", "the notes file could not be opened") from exc
            if leaf is not None and not stat.S_ISREG(leaf.st_mode):
                # A symlink (or anything not a plain file) at the leaf is refused, as the
                # read side refuses it, rather than silently replaced.
                raise NotesError("notes_unwritable", "the notes file is not a plain file")
            try:
                # A temporary beside the briefing, renamed over it: a failed write leaves
                # the previous notes as they were, never a truncated file.
                atomic_write_at(
                    dir_fd,
                    members.BRIEFING_FILE_NAME,
                    (text + "\n") if text else "",
                    fsync=True,
                    mode=0o600,
                )
            except OSError as exc:
                raise NotesError("notes_unwritable", "the notes could not be written") from exc
    except members.MemberSlugError as exc:
        raise NotesError("notes_unwritable", "the notes file could not be opened") from exc
    return len(text)
