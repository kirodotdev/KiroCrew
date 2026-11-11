"""Ratchet: a capped read of a file goes through ``bounded_read``, never ``read(cap + 1)``.

``stream.read(cap + 1)`` and ``os.read(fd, cap + 1)`` make CPython allocate
``cap + 1`` before the first byte arrives, so a 4 KB file read under a 50 MB cap
costs a 50 MB allocation: 14 ms on a Windows host against 0.03 ms on Linux,
paid by every guarded read. :mod:`kiro_crew.bounded_read` returns the same
bytes without ever asking for the cap, and it is the one spelling the package
uses for a file, a descriptor or an archive member, so a new capped read has a
helper to call and a reviewer has one shape to look for.

A site is a call to ``.read`` or ``.read1`` with an argument of the form
``<anything> + 1``. Two things are not sites. An awaited read is an asyncio or
aiohttp stream, which hands back what is buffered and allocates nothing up
front. Built-in apps are leaving the package for their own repositories and
take no new import from it, so their tree is not scanned.

Two tables hold the plain spelling that stays, each with exact counts, so a
site added to a listed file fails and one removed records the new number in
the same change. ``_STANDALONE`` files run where the package cannot be
imported. ``_RESPONSE_READS`` are reads of an HTTP response body: the standard
library clips such a request to the declared length or to the next chunk, so
the cap is not allocated, and their bound is a wire contract of its own.

A limit computed on an earlier line (``limit = cap + 1`` then ``read(limit)``)
is not seen; the shape pinned here is the one every site in the tree used.

Modelled on ``test_http_server_bind_ratchet.py`` (an exact recorded count,
detector proven on fixtures first) and scanned through ``source_corpus`` like
``test_dir_fd_create_race_ratchet.py``.
"""

from __future__ import annotations

import ast
from collections import Counter

import pytest
from source_corpus import parsed_candidates, src_root

#: One worker owns the whole-tree scan.
pytestmark = pytest.mark.xdist_group(name="tree_scan_bounded_read_ratchet")

_READS = frozenset({"read", "read1"})

#: Trees this gate does not police, as path fragments under ``kiro_crew``.
_UNSCANNED = ("_vendor/", "apps/builtins/")

#: Files that cannot import the package, with the sites each one holds and why.
_STANDALONE: dict[str, tuple[int, str]] = {
    "__init__.py": (1, "the package root imports nothing from the package"),
    "agent_sdk/gate_extensions/strands/kiro_crew_strands_gate.py": (
        1,
        "runs as a sealed copy inside the harness process, outside the package",
    ),
    "remote_session/workspace_tools.py": (
        6,
        "a stdlib-only script copied to the remote guest",
    ),
}

#: Reads of an HTTP response body, with the sites each file holds.
_RESPONSE_READS: dict[str, int] = {
    "acp/kas_bundle.py": 1,
    "apps/official_catalog.py": 1,
    "cli_server_stop.py": 1,
    "cli_server_update.py": 1,
    "dashboard/handlers/kiro_usage_api.py": 1,
    "dashboard/tailnet_localapi.py": 1,
    "decisions/impl_sagemaker.py": 1,
    "embeddings_ollama.py": 1,
    "feature_videos_manifest.py": 1,
    "monitoring/bitbucket_pull_request.py": 1,
    "platform/enterprise_policy_fetch.py": 1,
    "platform/policy_distribution_source.py": 1,
    "pod/runtime_client.py": 2,
}


def _is_one_past(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Add)
        and any(
            isinstance(side, ast.Constant) and type(side.value) is int and side.value == 1
            for side in (node.left, node.right)
        )
    )


def _sites(tree: ast.AST) -> list[int]:
    """Line numbers of the capped reads in *tree* that bypass the helper."""
    awaited: set[int] = set()
    reads: list[ast.Call] = []
    # One walk: an ``Await`` is visited before the call it wraps, but the filter
    # runs after the walk so it does not depend on that order.
    for node in ast.walk(tree):
        if isinstance(node, ast.Await):
            awaited.add(id(node.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _READS
        ):
            reads.append(node)
    return [
        call.lineno
        for call in reads
        if id(call) not in awaited
        and any(
            _is_one_past(argument)
            for argument in (*call.args, *(keyword.value for keyword in call.keywords))
        )
    ]


def _lines(source: str) -> list[int]:
    return _sites(ast.parse(source))


def test_the_detector_sees_every_spelling_of_a_capped_read() -> None:
    assert _lines("data = fh.read(cap + 1)\n") == [1]
    assert _lines("data = handle.read(1 + MAX_BYTES)\n") == [1]
    assert _lines("raw = os.read(fd, _LEASE_MAX_BYTES + 1)\n") == [1]
    assert _lines("raw = resp.read1(limit + 1)\n") == [1]
    assert _lines("x = fh.read(\n    A + B + 1,\n)\n") == [1]
    assert _lines("x = fh.read(size=cap + 1)\n") == [1]
    assert _lines("ok = fh.read(len(data) + 1) == data\n") == [1]


def test_the_detector_leaves_other_reads_alone() -> None:
    # The helper itself, a plain or fixed-size read, and arithmetic that is not
    # "one past a cap".
    assert _lines("data = bounded_read(fh, cap + 1)\n") == []
    assert _lines("raw = bounded_read_fd(fd, cap + 1)\n") == []
    assert _lines("data = fh.read()\n") == []
    assert _lines("data = fh.read(4096)\n") == []
    assert _lines("data = fh.read(cap - 1)\n") == []
    assert _lines("data = fh.read(cap + 2)\n") == []
    assert _lines("data = fh.read(cap + True)\n") == []
    assert _lines("data = fh.read(cap + 1.0)\n") == []
    # An async stream read returns what is buffered.
    assert _lines("async def f(r):\n    return await r.content.read(cap + 1)\n") == []
    # The shape in a comment or a string is not a call.
    assert _lines('note = "fh.read(cap + 1)"  # fh.read(cap + 1)\n') == []


def _tree_sites() -> Counter[str]:
    root = src_root()
    counts: Counter[str] = Counter()
    # Every site spells the attribute name, the operator and the digit of an
    # integer literal equal to one, so a file without all three holds none.
    for path, _text, tree in parsed_candidates(
        require_all=("read", "+", "1"), skip_syntax_errors=False
    ):
        rel = path.relative_to(root).as_posix()
        if any(part in rel for part in _UNSCANNED):
            continue
        found = len(_sites(tree))
        if found:
            counts[rel] = found
    return counts


def test_every_capped_read_of_a_file_goes_through_the_helper() -> None:
    counts = _tree_sites()
    recorded = {rel: count for rel, (count, _why) in _STANDALONE.items()} | _RESPONSE_READS

    strays = {rel: n for rel, n in counts.items() if rel not in recorded}
    assert not strays, (
        "read(cap + 1) allocates the cap before it reads, so a small file costs "
        "what the cap costs. Use kiro_crew.bounded_read.bounded_read(stream, cap + 1), "
        "or bounded_read_fd(fd, cap + 1) for a raw descriptor: same bytes, same "
        f"length check afterwards. Sites: {sorted(strays.items())}"
    )

    held = {rel: counts.get(rel, 0) for rel in recorded}
    changed = {rel: n for rel, n in held.items() if n != recorded[rel]}
    assert not changed, (
        "a file listed in _STANDALONE or _RESPONSE_READS holds a different number "
        "of read(cap + 1) sites than recorded. A new site there needs a reason; "
        "if you REMOVED one, lower its number (or delete its row at zero). "
        f"Found: {changed}"
    )


def test_the_two_tables_name_different_files() -> None:
    assert not set(_STANDALONE) & set(_RESPONSE_READS)
    assert all(count > 0 for count, _why in _STANDALONE.values())
    assert all(count > 0 for count in _RESPONSE_READS.values())


def test_the_scan_reaches_the_helper_s_own_callers() -> None:
    # A scan that silently stopped matching would report zero strays, which reads
    # exactly like success: the converted readers must still be in the corpus.
    root = src_root()
    seen = {
        path.relative_to(root).as_posix()
        for path, _text, _tree in parsed_candidates(require_all=("bounded_read(",))
    }
    assert "hook_runtime/safe_reads.py" in seen
    assert "jsonl_util.py" in seen
