"""A child's stderr tail is published only when neither spelling carries a secret.

The ring buffer stores one entry per stderr line, so a secret a harness
wrapped across two lines comes back with the join separator inside it, and the
bare-secret patterns (one contiguous 40-character run) do not match it.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.stderr_tail import STDERR_TAIL_MAX_CHARS, STDERR_WITHHELD, safe_stderr_tail

# The issue's fixture: a 40-character AWS secret access key, no prefix and no
# label the redactor anchors on, wrapped across two lines.
_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_WRAPPED = ["auth failed key=" + _KEY[:18], _KEY[18:] + " rejected"]


@pytest.mark.parametrize("sep", ["; ", " | ", "\n"])
def test_a_secret_wrapped_across_two_lines_is_withheld(sep):
    out = safe_stderr_tail(_WRAPPED, sep)
    assert out == STDERR_WITHHELD
    assert _KEY[:18] not in out
    assert _KEY[18:] not in out


def test_a_contiguous_secret_is_withheld_too():
    assert safe_stderr_tail([f"token {_KEY} rejected"], "; ") == STDERR_WITHHELD


def test_a_clean_tail_is_published_as_joined():
    lines = ["warming up", "Error: failed to create sandbox dir"]
    assert safe_stderr_tail(lines, "; ") == "warming up; Error: failed to create sandbox dir"


def test_nothing_to_publish_is_empty():
    assert safe_stderr_tail([], "; ") == ""
    assert safe_stderr_tail(["", "  "], "") == ""


def test_a_secret_straddling_the_cut_is_withheld():
    """The check runs before the cut, so a fragment past the window cannot leak."""
    line = f"token {_KEY} rejected"
    # Sized so the last STDERR_TAIL_MAX_CHARS characters start 10 characters
    # into the key: the window alone holds a 30-character run no pattern knows.
    rest = ("lorem ipsum " * 400)[: STDERR_TAIL_MAX_CHARS + len("token ") + 10 - len(line) - 2]
    lines = [line, rest]
    window = "; ".join(lines)[-STDERR_TAIL_MAX_CHARS:]
    assert window.startswith(_KEY[10:])
    assert safe_stderr_tail(lines, "; ") == STDERR_WITHHELD


def test_the_tail_is_bounded_and_keeps_the_end():
    lines = ["lorem ipsum dolor sit amet" for _ in range(200)] + ["the verdict"]
    out = safe_stderr_tail(lines, "; ")
    assert len(out) <= STDERR_TAIL_MAX_CHARS
    assert out.endswith("the verdict")


@pytest.mark.asyncio
async def test_process_exit_error_does_not_publish_a_wrapped_secret():
    client = AcpClient()
    client._cancelled = False
    client._buffer = MagicMock()
    client._buffer.__bool__ = lambda s: False
    client._process = MagicMock()
    client._process.returncode = 1
    client._process.stdout = MagicMock()
    client._process.stdout.readline = AsyncMock(return_value=b"")
    client._stderr_lines = deque(_WRAPPED, maxlen=20)
    client._stderr_task = MagicMock()
    client._stderr_task.done.return_value = True

    with pytest.raises(AcpError, match="ACP process exited") as exc_info:
        await client._read_message(timeout=1.0)

    message = str(exc_info.value)
    assert STDERR_WITHHELD in message
    assert _KEY[:18] not in message
    assert _KEY[18:] not in message
