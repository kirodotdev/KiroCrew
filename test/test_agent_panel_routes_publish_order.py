"""The crew-webview publish ordering contract: the file is written before the history entry is
appended, and no refused or failed publish appends one. Also the drawer read's docked-height
opt-in.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_panel_routes_helpers import (  # noqa: F401 -- fixtures are used by name
    CREW,
    SLUG,
    _client,
    _crew_log_on,
    _folded,
    vetted_fixture,
)

from kiro_crew import agent_panel
from kiro_crew.crew_log import emit as crew_log_emit

pytestmark = pytest.mark.asyncio


# ------------------------------------------------- the publish ordering contract
#
# ``_panel_record`` prefers the FILE over the fold, and its docstring states the
# one thing that makes that selection safe: the publish route writes the file
# BEFORE it appends the history entry, and returns without appending if that write
# fails. So every publish is in the file while only the ones whose append landed
# are in the fold, and the file can never be the staler of the two. The five pins
# below are that sentence made executable -- the order, the call-level fail-closed
# branch, its end state on the fold, every refusal branch, and the size of the
# caller population it holds for.


def _publish_handler_ast():
    """The publish route's function definition, parsed from its own source file.

    Read off disk rather than through ``inspect.getsource`` so the enclosing
    module's import graph is irrelevant to a structural assertion about it.
    """
    import ast
    from pathlib import Path

    import kiro_crew

    path = Path(kiro_crew.__file__).parent / "dashboard" / "handlers" / "agent_panel_publish.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_agent_panel_publish":
            return ast, node
    raise AssertionError("api_agent_panel_publish is not defined in the handler module")


async def test_the_file_is_written_before_the_history_is_appended(vetted, monkeypatch):
    """The file write completes BEFORE the history append begins.

    This is the order ``_panel_record``'s file-wins selection rests on. Reversed,
    the fold could hold a cycle the file does not, and the drawer would serve a
    panel older than the one the publish just wrote while reporting success -- a
    viewer cannot tell a stale dashboard from a current one.

    Both spies delegate to the real callables, so the publish under test really
    writes and really appends; what is recorded is only WHEN each happened.
    """
    order: list[str] = []
    real_publish = agent_panel.publish
    real_append = crew_log_emit.on_panel_published

    def _spy_publish(*a: Any, **kw: Any):
        record = real_publish(*a, **kw)
        # Recorded AFTER the real write returns, so the marker means "the file is
        # on disk" rather than "the write was attempted".
        order.append("file")
        return record

    def _spy_append(*a: Any, **kw: Any):
        order.append("append")
        return real_append(*a, **kw)

    monkeypatch.setattr(agent_panel, "publish", _spy_publish)
    monkeypatch.setattr(crew_log_emit, "on_panel_published", _spy_append)

    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}, "title": "fleet"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()

    assert order == ["file", "append"], (
        f"the publish route ran {order}; the file must be written before the history "
        "is appended, because _panel_record prefers the file on the strength of that order"
    )


async def test_a_failed_file_write_appends_no_history_row(vetted, monkeypatch):
    """A file write that fails never reaches the history append at all.

    The CALL-level half of the guarantee: the emitter is not invoked. Its
    end-state half -- that the fold holds no record either -- is measured by
    ``test_the_fold_never_holds_a_publish_the_file_lacks``, which runs the real
    emitter and so can observe a fold that a spy would leave empty whatever the
    route did.
    """
    appended: list[tuple[Any, ...]] = []
    real_append = crew_log_emit.on_panel_published

    def _refuse_write(*_a: Any, **_kw: Any):
        raise OSError("the record could not be written")

    def _spy_append(*a: Any, **kw: Any) -> bool:
        appended.append(a)
        # Delegates rather than answering True, so the spy adds an observation
        # instead of replacing the emitter's effect. A recording-only spy makes
        # every downstream read of the fold answer empty for its own reason.
        return real_append(*a, **kw)

    monkeypatch.setattr(agent_panel, "publish", _refuse_write)
    monkeypatch.setattr(crew_log_emit, "on_panel_published", _spy_append)

    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}, "title": "fleet"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "panel_write_failed"

    assert appended == [], "a publish whose file write failed still appended a history row"


async def test_the_fold_never_holds_a_publish_the_file_lacks(vetted, monkeypatch):
    """END STATE: the fold never gets AHEAD of the file.

    The property ``_panel_record``'s file-wins branch is written to be free of, and
    it is asserted on the two records themselves rather than on a call count, so a
    writer that reaches the log by any route -- not only through the emitter this
    file spies on elsewhere -- is still caught.

    A first publish is allowed to land, so both halves compare VALUES. Asserting
    absence after a single failed publish would prove nothing on either side:
    ``read`` answers ``None`` for a malformed file as readily as for a missing one,
    so it cannot see a partial record, and an empty fold is what an empty log looks
    like anyway.

    The second publish is refused at the WRITE SEAM, so the real ``publish`` runs
    its lock, its ownership check and its write attempt. Refusing ``publish``
    itself would exercise none of that -- it is the record's only writer, so the
    file would be untouched by construction rather than by the code under test.
    """
    async with _client() as c:
        first = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 1}, "title": "landed"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert first.status == 200, await first.text()

    def _refuse_write(*_a: Any, **_kw: Any):
        raise OSError("the record could not be written")

    monkeypatch.setattr(agent_panel, "atomic_write", _refuse_write)

    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 2}, "title": "refused"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 503, await resp.text()

    stored = agent_panel.read(SLUG)
    assert stored is not None, "the first publish left no file to compare against"
    assert stored["data"] == {"cycle": 1}, "the refused write changed the stored record"

    folded = _folded()
    assert folded is not None, "the first publish recorded no history"
    assert folded["data"] == {"cycle": 1}, "the fold holds a publish the file never received"
    assert folded["publishes"] == 1, "the fold counted a publish the file never received"


async def test_every_refused_publish_returns_before_the_history_append():
    """EVERY handler of the publish write cannot fall through to the append.

    Enumerated from the route's own source rather than listed here, because the
    recurrence is a refusal branch nobody has written yet: a handler added below
    the existing ones, logging and then falling through, would append a history
    row for a publish that never reached the file. Asserting over the handlers
    that ARE there covers the next one by construction.

    A handler ending in ``raise`` satisfies this as squarely as one ending in
    ``return``: neither reaches the append.
    """
    ast, handler = _publish_handler_ast()

    blocks = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Try)
        and any(
            isinstance(inner, ast.Attribute) and inner.attr == "publish"
            for stmt in node.body
            for inner in ast.walk(stmt)
        )
    ]
    assert len(blocks) == 1, (
        f"found {len(blocks)} try blocks calling agent_panel.publish; the pin below "
        "reads the one that guards the file write"
    )

    fell_through = [
        ast.unparse(h.type) if h.type is not None else "bare except"
        for h in blocks[0].handlers
        if not isinstance(h.body[-1], (ast.Return, ast.Raise))
    ]
    assert not fell_through, (
        f"{fell_through} handle a failed panel write without returning or raising, so a "
        "publish the file never received can still reach the history append"
    )
    assert blocks[0].handlers, "the publish write is unguarded, so a failure cannot be refused"


async def test_the_history_append_has_exactly_one_call_site():
    """``on_panel_published`` is called from exactly one place in the package.

    The emitter is public and applies no ordering rule of its own, so the write
    order lives entirely in its caller. One caller is what makes the pins above a
    statement about the whole package rather than about one route.

    This cannot check a NEW caller's ordering -- it makes one impossible to add
    silently. A second call site fails here, and extending the order pin to cover
    it is what clears the failure.
    """
    import ast

    import source_corpus

    root = source_corpus.src_root()
    # Only files whose TEXT holds the identifier are parsed. `ast` cannot produce a
    # call to a name the source does not contain, so the rest of the package cannot
    # contribute a site, and parsing it all is the cost `source_corpus` exists to
    # remove -- its corpus read is cached once per module rather than per test.
    candidates = list(source_corpus.parsed_candidates(require_all=("on_panel_published",)))
    sites: list[str] = []
    for path, _text, tree in candidates:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                name = ""
            if name == "on_panel_published":
                # The MODULE, not the line: a line number turns every unrelated edit
                # above the call into a failure of this pin. The COUNT is kept beside
                # it so a second caller inside this same module is caught too.
                #
                # ``as_posix``, not ``str``: on a Windows shard the native rendering
                # is backslash-separated and would never equal the literal below.
                sites.append(path.relative_to(root).as_posix())

    # Said out loud so a reader sees the scan reached something. A filter that matched
    # nothing would leave ``sites`` empty, and an empty list is also what a package
    # with no emitter at all looks like -- this pin must not read those two alike.
    assert candidates, "the corpus filter matched no file, so this pin measured nothing"

    assert sites == ["dashboard/handlers/agent_panel_publish.py"], (
        f"on_panel_published is called from {sites}; the publish order is the caller's "
        "to keep, so every call site needs the ordering pins in this file extended to it"
    )


async def test_the_drawer_read_reports_the_templates_docked_opt_in(vetted):
    """A template that carries the docked marker is reported with its height, and
    one that does not reports ``None`` -- which keeps the drawer's zero-mint
    native summary."""
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    (over / "compact.html").write_text(
        "<!--kirocrew:docked height=180-->" + agent_panel.DATA_MARKER, encoding="utf-8"
    )
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        plain = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert plain["panel"]["docked_height"] is None

        await c.post(
            "/api/agent-panel/publish",
            json={"template": "compact", "data": {"cycle": 48}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        opted = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert opted["panel"]["docked_height"] == 180
