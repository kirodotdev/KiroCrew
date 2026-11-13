"""The path a ``file://`` declaration names, read one way by every reader."""

from __future__ import annotations

from kiro_crew.platform_compat import IS_WINDOWS

_SCHEME = "file://"


def is_windows_drive_uri(uri: str) -> bool:
    """Whether the ``file://`` *uri* is the ``file:///C:/x`` spelling, on Windows only.

    kiro-cli cuts only the scheme off a resource, so on Windows it is left with
    the rooted but drive-less ``/C:/x`` and does not load the file. Elsewhere
    ``/C:/x`` is an ordinary absolute path and this is ``False``.
    """
    text = uri[len(_SCHEME) :]
    drive = text[1:2]
    return (
        IS_WINDOWS
        and text[:1] == "/"
        and drive.isascii()
        and drive.isalpha()
        and text[2:4] in (":/", ":\\")
    )


def file_uri_path_text(uri: str) -> str:
    """The path text of a ``file://`` *uri*: the scheme cut off, nothing decoded or expanded.

    ``Path.as_uri()`` spells a Windows file ``file:///C:/x``. Windows reads the
    ``/C:/x`` left after the scheme as rooted but drive-less, so there the ``/``
    before the drive is dropped and the text is the absolute ``C:/x``.
    """
    text = uri[len(_SCHEME) :]
    return text[1:] if is_windows_drive_uri(uri) else text
