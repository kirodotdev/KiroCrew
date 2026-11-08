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


# the publish ordering contract
#
# ``_panel_record`` prefers the FILE over the fold. That is safe only because the
# route writes the file BEFORE the history append and skips the append when the
# write fails, so the file is never the staler record. The pins below hold that.


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

    Reversed, the drawer could serve a panel older than the one just published. Both
    spies delegate to the real callables and record only WHEN each ran.
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

    The call-level half; ``test_the_fold_never_holds_a_publish_the_file_lacks`` checks
    the fold's end state with the real emitter.
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

    Checked on both records' VALUES after a first publish lands: absence alone proves
    nothing, since ``read`` gives ``None`` for a malformed file and an empty log folds
    empty. The second write fails at ``atomic_write`` so the real ``publish`` lock,
    ownership check and write attempt all run.
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

    Handlers are read from the route's source, so a new handler that logs and falls
    through is caught. Ending in ``raise`` counts like ``return``.
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

    The emitter applies no ordering of its own, so one caller makes the pins above hold
    for the whole package. A second call site fails here until the order pin covers it.
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
