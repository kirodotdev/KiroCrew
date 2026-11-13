"""session.md's compaction member lists match the sets they describe.

The context-compaction entry in ``docs/system-specs/modules/session.md`` names the
members of ``ACP_BACKENDS_COMPACT`` in prose, then names the ones that finish the
whole compaction inside the prompt turn (``ACP_BACKENDS_INLINE_COMPACTION``). A
harness that joins a set keeps its old absence in the spec unless something reads
the two together, so this does.

The member sentence names the harness-managed members
(``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION``) after a ``plus``, because their
threshold behavior is described in their own bullet further down.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.agent_sdk.backends_turns import (
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_HARNESS_MANAGED_COMPACTION,
    ACP_BACKENDS_INLINE_COMPACTION,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = _REPO_ROOT / "docs" / "system-specs" / "modules" / "session.md"

#: The name the spec uses for a backend id, where it is not the id itself.
_DOC_NAME = {"": "kiro-cli", "kas": "KAS"}

_MEMBERS = re.compile(r"`ACP_BACKENDS_COMPACT` — (?P<sentence>[^.]+)\. They divide")
_INLINE = re.compile(r"while (?P<inline>[^.]+?) finish the whole compaction inside the prompt turn")


def _names(phrase: str) -> set[str]:
    return {part.strip() for part in re.split(r",|\band\b", phrase) if part.strip()}


def _doc_names(backends: frozenset[str]) -> set[str]:
    return {_DOC_NAME.get(backend, backend) for backend in backends}


def _spec() -> str:
    return " ".join(_SPEC.read_text(encoding="utf-8").split())


def test_the_member_list_is_the_compaction_set() -> None:
    match = _MEMBERS.search(_spec())
    assert match, "session.md no longer carries the ACP_BACKENDS_COMPACT member list"
    own, _, rest = match["sentence"].partition(", plus ")
    managed = rest.split(",")[0]
    assert _names(own) == _doc_names(ACP_BACKENDS_COMPACT - ACP_BACKENDS_HARNESS_MANAGED_COMPACTION)
    assert _names(managed) == _doc_names(ACP_BACKENDS_HARNESS_MANAGED_COMPACTION)


def test_the_inline_list_is_the_inline_compaction_set() -> None:
    match = _INLINE.search(_spec())
    assert match, "session.md no longer names the members that compact inside the turn"
    assert _names(match["inline"]) == _doc_names(ACP_BACKENDS_INLINE_COMPACTION)
