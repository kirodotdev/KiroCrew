"""A link planted at a dump's name is never read as that dump.

The dumps directory is not agent-fenced, so a ``loopstall-*.txt`` there can be a
link to any file. This runs wherever the runner can create a link, Windows
included, where the plain no-follow open flag does not exist.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.dashboard import crash_dump_store
from kiro_crew.dashboard.crash_dump_store import DUMP_PREFIX, DUMP_SUFFIX

# Past the four header lines a dump reader skips, so a followed link would surface it.
_FOREIGN = 'one\ntwo\nthree\nfour\n  File "elsewhere.py", line 7 in not_a_dump_frame\n'


@requires_symlinks
def test_a_dump_named_link_to_another_file_is_never_read(tmp_path: Path) -> None:
    dumps_dir = tmp_path / "crash-dumps"
    dumps_dir.mkdir()
    target = tmp_path / "elsewhere.txt"
    target.write_text(_FOREIGN, encoding="utf-8")
    # What a reader that followed the link would surface.
    assert crash_dump_store.dump_replay_lines(target)[0]
    assert crash_dump_store.dump_first_stack_lines(target)
    link = dumps_dir / f"{DUMP_PREFIX}20260717T080000Z{DUMP_SUFFIX}"
    link.symlink_to(target)

    with pytest.raises(OSError) as refused:
        crash_dump_store._read_dump_head(link)
    assert refused.value.errno == errno.ELOOP
    assert crash_dump_store.dump_replay_lines(link) == ([], False)
    assert crash_dump_store.dump_first_stack_lines(link) == []
