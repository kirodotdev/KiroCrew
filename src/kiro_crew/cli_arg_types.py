"""argparse ``type=`` callables for the count flags the CLI parsers share.

``type=int`` accepts any integer, and on these flags a negative or zero value is
not merely odd but silently wrong downstream: SQLite reads ``LIMIT -1`` as no
limit at all, ``sel_reader.recent`` answers ``limit <= 0`` with an empty list
that the command prints as "no events recorded", and ``journalctl -n -5`` /
``tail -n -5`` are rejected or reinterpreted by the tool. Raising
``ArgumentTypeError`` puts the refusal on argparse's own path, so the user gets
exit 2 with the option name before any command runs.

This is the one home for these types in the parser modules: a second copy would
reject the same bad count with different wording.
"""

from __future__ import annotations

import argparse


def _parse_int(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a whole number, got {raw!r}") from None


def non_negative_int(raw: str) -> int:
    """An integer of 0 or more: a line count where 0 means "none of the backlog"."""
    value = _parse_int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def positive_int(raw: str) -> int:
    """An integer of 1 or more: a count or number that starts at 1."""
    value = _parse_int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value
