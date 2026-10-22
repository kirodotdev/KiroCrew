"""End to end: a fold moves, and the field the page subscribed to arrives on the socket.

Every other test of this feature holds one piece still. This one drives the whole chain
the way the product does -- the real route, the real artifact store, the real crew-log
bus, the real owner gate, the real broadcast -- because the pieces were each correct
while nothing connected them, which is exactly what a unit test cannot see.

Four claims:

1. The route serves a ``kind="dashboard"`` package's own page, composed through
   ``dashboard_frame``, and arms the live page as it does so.
2. A fold advancing puts the SUBSCRIBED field on the owner's socket, and a key the
   model does not declare is not on it. The model is the allowlist, and this is the
   only test where "not pushed" is measured on the wire rather than in a dict.
3. A page that cannot place a patch asks for a whole read, and the route answers it.
4. A worker is refused at the real artifact write path, with the sentence that tells
   it what to do instead.

The person's lock outranking the package, the refusal of a package bound elsewhere, the
tree fold read through the route, what leaves the gateway for a page, and the attacks on
the armed page (a live-backed artifact, another kind of artifact, the refetch races, two
packages for one crewmate) are here too. The binding resolver, the slug and preview
rules, a bound package with nothing to serve, and which version is served are in
``test_member_dashboard_package_route.py``; the fixtures and fakes both files share live
in ``member_dashboard_package_route_helpers.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import string
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state
from member_dashboard_package_route_helpers import wired_fixture  # noqa: F401
from member_dashboard_package_route_helpers import (
    APPLIED_EVENTS,
    APPLIED_VERSION,
    CREW,
    OWNER,
    PACKAGE_SLUG,
    PAGE,
    PATH,
    SLUG,
    _call,
    _package,
    _publish,
    _request,
    _Store,
)

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.dashboard.handlers import artifacts as artifacts_handler
from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf
from kiro_crew.platform.context import redact_via_context


@pytest.mark.asyncio
async def test_the_route_serves_the_owners_own_page_through_the_frame(wired: Any) -> None:
    """The page is composed, not handed over raw: a data island, the bootstrap that
    freezes it onto ``window``, and the author's markup verbatim inside the host's
    body. Served under the key the tab already mounts."""
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200
    page = body["rendered_html"]
    assert PAGE in page, "the page is not placed verbatim"
    assert "kirocrew-dashboard-data" in page
    assert body["fields"] == ["goal"], "the body does not name the allowlist"
    assert body["instance_version"] == 4


@pytest.mark.asyncio
async def test_the_route_serves_a_script_as_written_and_still_masks_visible_text(
    wired: Any,
) -> None:
    """The scrub that runs before the page is framed must not rewrite the page's code.

    An 80-character palette in a script was replaced by ``[REDACTED: credential]``: the
    script still loaded, so nothing reported an error, and every lookup into the palette
    answered the wrong index. Composing the same page with ``dashboard_frame`` alone
    served it intact, which is why only the route shows it. A key-shaped run in the
    page's VISIBLE text is still masked, in the same response.
    """
    palette = string.ascii_lowercase + string.ascii_uppercase + string.digits + "#$%&*+-=?@^_~:;<>!"
    key = base64.b64encode(hashlib.sha256(b"kc-bare-0").digest()).decode()[:40]
    page = (
        '<p data-dashboard-field="goal"></p>'
        f"<script>var ART_CHARS = '{palette}'; window.kirocrew.subscribe('goal', (v) => v);</script>"
        f"<p>{key}</p>"
    )
    assert redact_via_context(page) != page, "the premise: the whole-page pass rewrites it"
    wired.store.content = dp.dump_package(_package(html=page))
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200
    rendered = body["rendered_html"]
    assert f"var ART_CHARS = '{palette}';" in rendered
    assert key not in rendered, "visible text kept a key-shaped run"
    assert "[REDACTED: credential]" in rendered


@pytest.mark.asyncio
async def test_the_persons_lock_outranks_the_crewmates_package(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE LOCK IS A CEILING OVER THE CREWMATE, so package selection cannot step over it.

    The lock exists so a crewmate cannot change which page the tab shows, and
    ``instance.read`` is the only reader of it. Selecting a bound package returned
    BEFORE that read, so writing its own ``kind="dashboard"`` artifact -- ordinary
    agent work -- moved the tab off the page the person pinned; with the package
    unapplied it moved it to the empty state. Nothing unusual is needed for that.

    Measured by what the route SERVES, not by a call count: the locked record is the
    answer, and the package page is not in it.
    """
    served: dict[str, Any] = {}

    async def _no_package(*_a: Any, **_k: Any) -> Any:
        served["package_served"] = True
        raise AssertionError("the package branch ran while the layout was locked")

    import dataclasses

    from kiro_crew.dashboard_templates import instance as instance_mod

    monkeypatch.setattr(md.instance, "is_locked", lambda slug: True)
    monkeypatch.setattr(md, "_serve_package", _no_package)
    # Stubbed beside `is_locked` so the two agree: the product reads the lock through
    # `instance.read` as well, and saying "locked" to one and not the other would
    # assert against a state no gateway reaches.
    real_read = instance_mod.read
    monkeypatch.setattr(
        md.instance,
        "read",
        lambda slug: dataclasses.replace(real_read(slug), structure_locked=True),
    )
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200, body
    assert "package_served" not in served
    assert PAGE not in (
        body.get("rendered_html") or ""
    ), "the mate's own page was served over the page the person locked"
    assert (
        body.get("structure_locked") is True
    ), "the body does not tell the tab the layout is locked"


@pytest.mark.asyncio
async def test_a_locked_pinned_package_tells_the_tab_it_is_locked(wired: Any) -> None:
    """A pinned package page carries the person's lock, as a template page does.

    The Profile switch and the Dashboard's lock badge read ``structure_locked`` from
    this body; a package page that left it out read as unlocked after a refetch.
    """
    from kiro_crew.dashboard_templates import instance as instance_mod

    instance_mod.apply_package(
        SLUG,
        package_slug=PACKAGE_SLUG,
        package_version=APPLIED_VERSION,
        content=dp.dump_package(_package()),
    )
    instance_mod.set_structure_lock(SLUG, True)

    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))

    assert status == 200, body
    assert PAGE in body["rendered_html"], "the pinned package is the page in force"
    assert body.get("structure_locked") is True


@pytest.mark.asyncio
async def test_an_unlocked_crewmate_still_gets_its_package(wired: Any) -> None:
    """The limit: the lock is the only thing that outranks the package, so a crewmate
    nobody locked is unaffected -- which is every crewmate today."""
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200
    assert PAGE in body["rendered_html"]


def _page_at_the_ceiling() -> str:
    """A page of exactly the frame's ceiling: :data:`PAGE` plus an inlined script."""
    from kiro_crew import dashboard_frame

    head, tail = PAGE + "<script>", "</script>"
    return head + "x" * (dashboard_frame.MAX_PAGE_BYTES - len(head) - len(tail)) + tail


def _store_page(wired: Any, page: str) -> None:
    """Make *page* the applied package's page, through the package's own validator."""
    raw = json.loads(dp.dump_package(_package()))
    raw["view"] = {"html": page}
    wired.store.content = dp.dump_package(dp.validate_package(raw))


@pytest.mark.asyncio
async def test_a_page_at_the_frames_ceiling_is_served_through_the_frame(wired: Any) -> None:
    """The biggest page a package may hold is drawn, not refused at the frame."""
    page = _page_at_the_ceiling()
    _store_page(wired, page)
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200, body
    assert body["html"] == page
    assert page in body["rendered_html"]


@pytest.mark.asyncio
async def test_the_page_is_scrubbed_and_framed_off_the_event_loop(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both steps read the whole page, which takes hundreds of milliseconds near the
    ceiling: on the loop, that pause lands on every other session's chat."""
    import threading

    loop_thread = threading.current_thread()
    calls: list[tuple[str, bool]] = []
    scrub, frame = md._template_text_safe, mdf.render_package_page

    def _scrub(text: str) -> str:
        calls.append(("scrub", threading.current_thread() is loop_thread))
        return scrub(text)

    def _frame(*args: Any, **kwargs: Any) -> str:
        calls.append(("frame", threading.current_thread() is loop_thread))
        return frame(*args, **kwargs)

    monkeypatch.setattr(md, "_template_text_safe", _scrub)
    monkeypatch.setattr(mdf, "render_package_page", _frame)
    status, _body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200
    assert {name for name, _ in calls} == {"scrub", "frame"}
    assert not [name for name, on_loop in calls if on_loop], calls


@pytest.mark.asyncio
async def test_the_route_arms_the_live_page(wired: Any) -> None:
    await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    pages = wired.app["member_dashboard_live_pages"]
    assert (SLUG, PACKAGE_SLUG) in pages
    assert pages[(SLUG, PACKAGE_SLUG)].folds() == frozenset({"work"})


@pytest.mark.asyncio
async def test_a_fold_puts_the_subscribed_field_on_the_owners_socket(wired: Any) -> None:
    """THE WHOLE CHAIN. Nothing in this test calls the controller: the route arms it,
    the bus carries a real fold event, and the frame is read off the broadcast."""
    await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    wired.frames.clear()
    _publish("work", {"conductor": {"goal": "ship the host"}, "omitted": 0}, 1)
    assert wired.frames, "a fold moved and nothing reached the socket"
    kind, data = wired.frames[-1]
    assert kind == mdf.FIELD_FRAME
    assert data["slug"] == SLUG
    assert data["read"]["fields"]["goal"] == "ship the host"
    assert data["read"]["patch"] is True


@pytest.mark.asyncio
async def test_a_key_the_model_does_not_declare_is_not_on_the_wire(wired: Any) -> None:
    """Measured on the BROADCAST, which is the only place it counts. A work ledger's
    fold value carries every item and every worker's report; a page binding one field
    of it must not be handed the rest, and the model is what says so."""
    await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    wired.frames.clear()
    _publish(
        "work",
        {"conductor": {"goal": "ship the host"}, "items": ["a secret item"], "omitted": 3},
        1,
    )
    sent = json.dumps(wired.frames[-1][1])
    assert "a secret item" not in sent
    assert set(wired.frames[-1][1]["read"]["fields"]) == {"goal"}


@pytest.mark.asyncio
async def test_a_fold_that_moves_without_the_field_changing_sends_nothing(wired: Any) -> None:
    await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    _publish("work", {"conductor": {"goal": "ship the host"}}, 1)
    wired.frames.clear()
    _publish("work", {"conductor": {"goal": "ship the host"}, "items": ["new"]}, 2)
    assert wired.frames == []


@pytest.mark.asyncio
async def test_each_frame_steps_the_version_by_one(wired: Any) -> None:
    """What the page checks a patch against. A counter that skipped would make every
    page ask for a full read on every push."""
    await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    wired.frames.clear()
    for n, goal in enumerate(("one", "two", "three"), start=1):
        _publish("work", {"conductor": {"goal": goal}}, n)
    versions = [data["read"]["version"] for _kind, data in wired.frames]
    assert versions == list(range(versions[0], versions[0] + len(versions)))


@pytest.mark.asyncio
async def test_the_refetch_route_answers_with_the_whole_read(wired: Any) -> None:
    await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    _publish("work", {"conductor": {"goal": "ship the host"}}, 1)
    status, body = await _call(
        md.api_member_dashboard_refetch,
        _request(
            wired.app,
            "POST",
            f"/api/members/{SLUG}/dashboard/refetch?member={CREW}&dashboard={PACKAGE_SLUG}",
        ),
    )
    assert status == 200
    assert body["read"]["fields"]["goal"] == "ship the host"
    assert "patch" not in body["read"], "a full read must replace, not merge"


@pytest.mark.asyncio
async def test_the_refetch_route_says_so_when_nothing_is_armed(wired: Any) -> None:
    status, body = await _call(
        md.api_member_dashboard_refetch,
        _request(
            wired.app,
            "POST",
            f"/api/members/{SLUG}/dashboard/refetch?member={CREW}&dashboard={PACKAGE_SLUG}",
        ),
    )
    assert status == 409
    assert body["code"] == "no_live_page"


@pytest.mark.asyncio
async def test_a_package_bound_elsewhere_is_not_served_here(wired: Any) -> None:
    """A page carries the binding's own fold values, so rendering one bound elsewhere
    would show one crewmate's work under another's name."""
    wired.store.content = dp.dump_package(_package(bound_to="crewmate:nyx"))
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 403
    assert body["code"] == "dashboard_not_bound"


@pytest.mark.asyncio
async def test_a_non_owner_is_refused_before_the_package_is_read(wired: Any) -> None:
    req = _request(wired.app, "GET", PATH)
    req["user"] = "123456789"

    def _no_read() -> Any:
        raise AssertionError("a refused caller must not reach the artifact store")

    with patch("kiro_crew.artifacts.get_default_store", side_effect=_no_read):
        status, body = await _call(md.api_member_dashboard, req)
    assert status == 403
    assert body["code"] == "owner_only"


class TestTheWorkerIsRefusedAtTheRealWritePath:
    """The owner gate on the path a worker's ``artifact_update`` actually takes."""

    @staticmethod
    def _roster(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
        """The roster the gate counts claimants in.

        The gate authorizes only when the binding's slug names EXACTLY ONE crewmate,
        so a test that patched the slug function alone left that count reading
        whatever roster the machine happened to hold.
        """
        from kiro_crew.config import loader as loader_mod

        monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, config=None: name.lower())
        monkeypatch.setattr(
            loader_mod.KiroCrewConfig,
            "load",
            staticmethod(lambda *a, **k: SimpleNamespace(agents=dict.fromkeys(names, None))),
        )

    def _request(self, app: web.Application, crew: str) -> web.Request:
        req = make_mocked_request(
            "PATCH",
            f"/api/artifacts/{PACKAGE_SLUG}",
            headers={"X-Internal-Secret": "s", "X-Session-Key": "chat-9-17911"},
            app=app,
            match_info={"slug": PACKAGE_SLUG},
        )
        app["state"].sessions = SimpleNamespace(get_agent_selection=lambda key: ("member", crew))
        return req

    def test_a_worker_is_refused_with_the_sentence_that_says_what_to_do(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._roster(monkeypatch, "atlas", "nyx")
        monkeypatch.setattr(mdf, "writing_caller", lambda request: ("nyx", "chat-9-17911"))
        said = mdf.package_write_refusal(
            self._request(wired.app, "nyx"), dp.dump_package(_package())
        )
        assert said, "a worker may write another crewmate's dashboard package"
        assert "atlas" in said
        assert "work_report" in said

    def test_the_owner_is_not_refused(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self._roster(monkeypatch, "atlas", "nyx")
        monkeypatch.setattr(mdf, "writing_caller", lambda request: ("atlas", "chat-1-1790"))
        assert (
            mdf.package_write_refusal(
                self._request(wired.app, "atlas"), dp.dump_package(_package())
            )
            == ""
        )

    def test_a_document_that_is_not_a_package_is_not_gated(self, wired: Any) -> None:
        """Asked on every artifact write, so refusing a prose document for having no
        binding would break every other kind."""
        req = self._request(wired.app, "nyx")
        assert mdf.package_write_refusal(req, "# just a note") == ""
        assert mdf.package_write_refusal(req, None) == ""

    def test_the_update_route_calls_the_gate(self) -> None:
        """The gate is only a gate if the route asks it. Pinned on the SOURCE of the
        three write paths, because a route that stopped asking would leave every test
        above passing."""
        # The routes and the gate live in the facade's owner modules. The gate's own
        # module is read last, so every other module's text sits in the head below.
        facade = Path(artifacts_handler.__file__)
        parts = [facade] + sorted(
            (facade.parent / "artifacts_parts").glob("*.py"),
            key=lambda path: path.stem == "dashboard_package_gate",
        )
        source = "".join(path.read_text(encoding="utf-8") for path in parts)
        # Counted as CALLS, excluding the accessor's own definition line, because one
        # call wraps its arguments across lines and a single-spelling count misses it.
        calls = source.count("_package_write_refusal(") - source.count(
            "def _package_write_refusal("
        )
        assert calls == 3, (
            "expected the gate on all three write paths -- api_artifact_update, the "
            "create that lands on an existing slug, and the fresh create. Whichever "
            "one goes unasked is the spelling a worker would use"
        )
        # Reached through ONE deferred accessor, so the controller stays off the boot
        # path: `dashboard.server` imports this module before the socket binds.
        assert "def _package_write_refusal(" in source
        head = source[: source.index("def _package_write_refusal(")]
        assert (
            "member_dashboard_fields" not in head
        ), "the controller is imported at module scope, which puts it on the boot path"


# --------------------------------------------------------------------------- #
# The tree fold, through the production route. This is what makes
# `read_tree_projection` reachable at all: it has no bus scope, so nothing can push
# it, and the controller reads it on arm and on refetch.
# --------------------------------------------------------------------------- #

GALLERY = (
    Path(mdf.__file__).resolve().parents[2]
    / "builtin_skills"
    / "dashboard"
    / "gallery"
    / "dispatch-tree"
    / "package.json"
)


def board(key: str, goal: str, depth: int, entries: int, on_cycle: bool = False) -> dict[str, Any]:
    """One row in the shape ``_worktree_render`` emits, not a shape this test invented.

    Inventing it is exactly what hid the gallery page reading ``slot_key`` / ``on_cycle``
    / ``entries`` off a fold that emits ``board`` / ``cycle`` / ``items``: the stub
    satisfied the test and nothing compared it to the renderer.
    """
    return {
        "board": key,
        "parent": None,
        "parent_item": None,
        "cycle": on_cycle,
        "depth": depth,
        "goal": goal,
        "items": [{"id": f"it_{n}"} for n in range(entries)],
        "generation": "",
    }


def _tree_value(goal: str, depth: int = 0) -> dict[str, Any]:
    """One rendered ``worktree`` projection, in the fold's own declared shape."""
    return {
        "boards": [board("chat-1-1790", goal, depth, 2)],
        "roots": ["chat-1-1790"],
        "cycles": 0,
        "unresolved": 0,
        "dropped": 0,
        "goal_chars": len(goal),
        "limit": 64,
        "owner_limit": 8,
        "root": SLUG,
    }


@pytest.fixture()
def tree_wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The route, serving the GALLERY package, with the tree read intercepted.

    The gallery page is loaded off disk rather than rebuilt here: it is the artifact a
    reader is told to copy, so a test that served a hand-written stand-in would prove
    the route works for a package nobody has.

    ``read_tree_projection`` itself is stubbed because its real walk needs a fleet of
    on-disk logs; what this pins is that the ROUTE calls it, with the root it should,
    and puts its value on the page. :mod:`test_crew_log_worktree_read` owns the walk.
    """
    frames: list[tuple[str, Any]] = []
    calls: list[tuple[str, str]] = []
    value = {"v": _tree_value("ship the host")}
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    monkeypatch.setattr(
        state, "broadcast_ws_owners", lambda kind, data: frames.append((kind, data)), raising=False
    )
    app = web.Application()
    app["state"] = state
    store = _Store(GALLERY.read_text(encoding="utf-8"))
    monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: SLUG)
    monkeypatch.setattr(md, "_write_session", lambda slug, member: "")
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, config=None: SLUG)
    # The gallery package ships bound to the example crewmate; this route serves the
    # crewmate it is bound to, so the binding is the thing a copier changes first.
    store.content = store.content.replace('"crewmate:example"', f'"crewmate:{SLUG}"')

    def _read_tree(root: str, name: str) -> Any:
        calls.append((root, name))
        return SimpleNamespace(value=value["v"], seq=1)

    monkeypatch.setattr("kiro_crew.crew_log.projection.read_tree_projection", _read_tree)
    yield SimpleNamespace(app=app, frames=frames, calls=calls, value=value, store=store)
    for page in list(app.get("member_dashboard_live_pages", {}).values()):
        page.disarm()


TREE_PATH = f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={PACKAGE_SLUG}"


@pytest.mark.asyncio
async def test_the_route_reads_the_tree_fold_for_this_slot(tree_wired: Any) -> None:
    """THE PRODUCTION CALLER of ``read_tree_projection``. Asked with the page's own slot
    as the root, because a tree walks what the record says from one root outward -- the
    boards that root's units bound, and the boards those conduct, to closure."""
    status, _body = await _call(md.api_member_dashboard, _request(tree_wired.app, "GET", TREE_PATH))
    assert status == 200
    assert (SLUG, "worktree") in tree_wired.calls


@pytest.mark.asyncio
async def test_the_tree_value_is_on_the_page_the_route_serves(tree_wired: Any) -> None:
    status, body = await _call(md.api_member_dashboard, _request(tree_wired.app, "GET", TREE_PATH))
    assert status == 200
    island = body["rendered_html"].split('id="kirocrew-dashboard-data">')[1].split("</script>")[0]
    read = json.loads(island.replace("\\u003c", "<").replace("\\u003e", ">"))
    assert read["fields"]["boards"][0]["goal"] == "ship the host"
    assert read["fields"]["roots"] == ["chat-1-1790"]


@pytest.mark.asyncio
async def test_a_tree_bound_page_does_not_fly_the_stale_band(tree_wired: Any) -> None:
    """The feed reports a tree binding unavailable, because it has no scope to key it
    by. If the controller left those fields in ``missing`` the page would raise the band
    permanently -- over values that are current -- and a band a reader learns to ignore
    is worse than no band."""
    _status, body = await _call(md.api_member_dashboard, _request(tree_wired.app, "GET", TREE_PATH))
    island = body["rendered_html"].split('id="kirocrew-dashboard-data">')[1].split("</script>")[0]
    read = json.loads(island.replace("\\u003c", "<").replace("\\u003e", ">"))
    assert read["stale"] is False
    assert "boards" not in read["missing"]


@pytest.mark.asyncio
async def test_a_refetch_re_reads_the_tree(tree_wired: Any) -> None:
    """The only moment a tree value moves. Nothing can push one, so a refetch is not
    merely the gap remedy here -- it is the whole update path for this fold."""
    await _call(md.api_member_dashboard, _request(tree_wired.app, "GET", TREE_PATH))
    tree_wired.value["v"] = _tree_value("ship the gallery", depth=1)
    _status, body = await _call(
        md.api_member_dashboard_refetch,
        _request(
            tree_wired.app,
            "POST",
            f"/api/members/{SLUG}/dashboard/refetch?member={CREW}&dashboard={PACKAGE_SLUG}",
        ),
    )
    assert body["read"]["fields"]["boards"][0]["goal"] == "ship the gallery"
    assert tree_wired.calls.count((SLUG, "worktree")) == 2


@pytest.mark.asyncio
async def test_a_child_board_dispatched_after_arm_reaches_the_open_page(tree_wired: Any) -> None:
    """THE GOAL'S OWN CLAIM, end to end: the page renders the tree a fleet forms, and a
    board dispatched while somebody is looking at it arrives on that same open page.

    Distinct from :func:`test_a_refetch_re_reads_the_tree`, which swaps the whole value
    and asks whether the read moved. What is in question here is the FLEET growing: a
    conductor dispatches one more worker, so a child appears UNDER an existing board,
    and the page a reader already has open shows it with its parent link intact.

    It arrives through the refetch because a tree has no bus scope to push on -- that is
    the design, and this is the test that says the resulting update path actually
    carries a new board rather than only a changed field. The page is not re-armed: the
    same LivePage that served the first paint delivers it, which is what makes this
    about the open tab instead of about a reload.
    """
    status, first = await _call(md.api_member_dashboard, _request(tree_wired.app, "GET", TREE_PATH))
    assert status == 200
    island = first["rendered_html"].split('id="kirocrew-dashboard-data">')[1].split("</script>")[0]
    before = json.loads(island.replace("\\u003c", "<").replace("\\u003e", ">"))
    assert [b["board"] for b in before["fields"]["boards"]] == ["chat-1-1790"]
    armed = dict(tree_wired.app["member_dashboard_live_pages"])
    assert len(armed) == 1, "no page was armed, so this test would prove nothing"

    # The fleet grows: the root board's conductor dispatches one more worker, and that
    # worker's own board hangs off the board that dispatched it.
    child = board("chat-2-1791", "draw the tree", depth=1, entries=1)
    child["parent"] = "chat-1-1790"
    child["parent_item"] = "it_0"
    grown = dict(tree_wired.value["v"])
    grown["boards"] = [*grown["boards"], child]
    tree_wired.value["v"] = grown

    _status, body = await _call(
        md.api_member_dashboard_refetch,
        _request(
            tree_wired.app,
            "POST",
            f"/api/members/{SLUG}/dashboard/refetch?member={CREW}&dashboard={PACKAGE_SLUG}",
        ),
    )
    boards = body["read"]["fields"]["boards"]
    assert [b["board"] for b in boards] == ["chat-1-1790", "chat-2-1791"], boards
    appended = boards[-1]
    assert appended["parent"] == "chat-1-1790", "the child lost the board that dispatched it"
    assert appended["depth"] == 1
    assert appended["goal"] == "draw the tree"
    # The SAME armed page delivered it. A new one would mean the reader had reloaded,
    # which is the thing this test exists to rule out.
    assert dict(tree_wired.app["member_dashboard_live_pages"]) == armed


@pytest.mark.asyncio
async def test_a_work_ledger_push_still_rides_beside_the_tree(tree_wired: Any) -> None:
    """The gallery page binds BOTH: the tree for its shape and the work ledger for the
    counts. One fold having no event must not cost the other its push."""
    await _call(md.api_member_dashboard, _request(tree_wired.app, "GET", TREE_PATH))
    tree_wired.frames.clear()
    _publish("work", {"items": [{"id": "it_1"}, {"id": "it_2"}]}, 1)
    assert tree_wired.frames, "the work fold moved and nothing reached the socket"
    assert len(tree_wired.frames[-1][1]["read"]["fields"]["work_items"]) == 2


@pytest.mark.asyncio
async def test_the_gallery_pages_own_subscriber_receives_the_tree(tree_wired: Any) -> None:
    """END TO END, through the page's own script.

    The route reads the tree, composes the document, and the gallery page's own
    ``window.kirocrew.subscribe('boards', draw)`` is called with the boards that came
    out of ``read_tree_projection``. Everything between is the product's: the island,
    the bootstrap, the subscriber registry.

    The document's scripts are EXECUTED in Node under the same DOM shim
    ``test_dashboard_frame_subscribe`` uses, because the claim is about what runs. A
    page that composed correctly and never called its own callback renders an empty
    tree, and no assertion on the html would see it.
    """
    from test_dashboard_frame_subscribe import _run

    _status, body = await _call(md.api_member_dashboard, _request(tree_wired.app, "GET", TREE_PATH))
    island = body["rendered_html"].split('id="kirocrew-dashboard-data">')[1].split("</script>")[0]
    read = json.loads(island.replace("\\u003c", "<").replace("\\u003e", ">"))
    # The page's own script is replayed verbatim out of the stored package, so what is
    # exercised is the gallery artifact rather than a line retyped in this test.
    page_js = (
        dp.parse_package(tree_wired.store.content)["view"]["html"]
        .split("<script>")[1]
        .split("</script>")[0]
    )
    calls = _run(
        __import__("kiro_crew.dashboard_frame", fromlist=["x"])._BOOTSTRAP_JS,
        read,
        # The shim answers only for the data island, so the page's own two elements and
        # `createElement` are supplied here, RECORDING instead of building: what this
        # asserts is that the callback fired with the tree the route read, which is the
        # link the ruling is about. The row markup itself is pinned by the gallery test.
        "const node = () => ({"
        "  setAttribute() {}, appendChild(c) { if (c && c.text !== undefined)"
        "    __calls.push(c.text); }, style: {}, hidden: false,"
        "  set textContent(v) { this.text = v; }, get textContent() { return ''; },"
        "});"
        "const owned = { nodes: node(), empty: node() };"
        "const island = globalThis.document.getElementById;"
        "globalThis.document.getElementById = (id) => owned[id] || island(id);"
        "globalThis.document.createElement = () => node();" + page_js,
    )
    assert "ship the host" in calls, (
        "the gallery page's subscriber never received the tree the route read, so the "
        "page renders an empty tree over a fleet the record knows about"
    )
    assert "chat-1-1790" in calls


# --------------------------------------------------------------------------- #
# What leaves the gateway for a page. The wildcard `postMessage` target is
# unavoidable for an opaque-origin frame, so the two properties that make it
# safe are pinned here rather than argued in a comment.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_frame_carries_only_the_fields_the_model_declares(tree_wired: Any) -> None:
    """The wildcard target means the payload itself is the boundary.

    `'*'` is the only target that can reach an opaque origin, so the host cannot narrow
    WHO receives a frame -- it can only narrow WHAT is in one. Every key of every frame
    must therefore be a field the package's own model declares, and a fold's other
    members must never ride along.
    """
    await _call(md.api_member_dashboard, _request(tree_wired.app, "GET", TREE_PATH))
    allowed = set(dp.subscribed_fields(dp.parse_package(tree_wired.store.content)))
    tree_wired.frames.clear()
    _publish(
        "work",
        {"items": [{"id": "it_1"}], "conductor": {"goal": "secret goal"}, "omitted": 4},
        1,
    )
    assert tree_wired.frames, "the fold moved and nothing was sent"
    for _kind, data in tree_wired.frames:
        extra = set(data["read"]["fields"]) - allowed
        assert not extra, f"a frame carried {sorted(extra)}, which the model does not declare"
    sent = json.dumps([d for _k, d in tree_wired.frames])
    assert "secret goal" not in sent, "a fold member the model omits reached the wire"


def test_the_tab_ignores_a_message_from_any_other_window() -> None:
    """The other half of the wildcard's safety: the host accepts nothing back from a
    window that is not this frame's own.

    Every frame the dashboard embeds runs with `allow-scripts`, so a sibling can reach
    `parent.frames[i]` and post. The tab's listener compares `event.source` against the
    frame's `contentWindow` before reading anything, and the frame's own bootstrap makes
    the mirror-image check against `parent`.
    """
    tab = (
        Path(mdf.__file__).resolve().parents[4]
        / "website"
        / "src"
        / "pages"
        / "members"
        / "CrewDynamicDashboard.tsx"
    )
    if not tab.exists():  # pragma: no cover - a source checkout always has it
        pytest.skip("the website source is not in this build")
    text = tab.read_text(encoding="utf-8")
    assert (
        "event.source !== win" in text
    ), "the tab reads a posted message without checking which window sent it"
    # ONE SUPPRESSION PER CALL, each carrying the reason -- not a file-level disable,
    # which would also cover a call added later for a different window. Counted against
    # the calls rather than pinned to a number, so a new post has to bring its own
    # suppression and no count here goes stale when one does.
    posts = text.count("postMessage(")
    assert posts, "the tab posts nothing, so this test is guarding none of it"
    assert (
        text.count("nosemgrep: javascript.browser.security.wildcard-postmessage") == posts
    ), f"{posts} postMessage call(s) and a different number of per-call suppressions"
    # The DECLARATION, not the file's prose. The file explains at length that it
    # withholds same-origin access, so a text search for that token matches the
    # explanation and passes whatever the sandbox actually grants.
    # The grant is defined once, in lib/crewDashboardSandbox.ts, and the tab imports it.
    grant = (tab.parents[2] / "lib" / "crewDashboardSandbox.ts").read_text(encoding="utf-8")
    assert "import { CREW_DASHBOARD_SANDBOX } from '../../lib/crewDashboardSandbox'" in text
    assert "export const CREW_DASHBOARD_SANDBOX = 'allow-scripts'" in grant, (
        "the frame's sandbox is no longer exactly allow-scripts; same-origin access "
        "would give the page the dashboard's own storage and cookies, and a non-opaque "
        "origin would also make the wildcard postMessage target a real choice"
    )


# --------------------------------------------------------------------------- #
# The two fenced findings on 6ec89205e5. Each asserts the ATTACK FAILS, not
# merely that a guard exists, because a guard that is present and reachable
# around is the shape both of these had.
# --------------------------------------------------------------------------- #


class _Linked:
    """An artifact whose bytes come from a FILE: a live source pointer.

    `source_path` set with `source_copy_only` false is the store's own spelling for
    "reads serve the file's current bytes". Nothing in the artifact API gates those
    bytes, so the write gate never sees them.
    """

    def __init__(self, content: str, *, kind: str = "dashboard") -> None:
        self.slug = PACKAGE_SLUG
        self.content = content
        self.version = 2
        self.kind = kind
        self.source_path = "/tmp/anywhere/page.json"
        self.source_root = "/tmp/anywhere"
        self.source_copy_only = False


@pytest.mark.asyncio
async def test_a_live_backed_artifact_is_not_armed_even_bound_to_this_crewmate(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ATTACK: a worker edits the FILE behind a linked artifact into a package bound
    to this crewmate, then the owner opens the route.

    The write gate cannot have seen those bytes -- they were never submitted through the
    artifact API -- so if the route parses and arms whatever the store returns, it runs
    worker-authored script against this crewmate's own fold values. The refusal must
    land before the parse.
    """
    linked = _Linked(dp.dump_package(_package()))
    monkeypatch.setattr(
        "kiro_crew.artifacts.get_default_store", lambda: SimpleNamespace(get=lambda slug: linked)
    )

    def _never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a live-backed artifact reached the package parser")

    monkeypatch.setattr("kiro_crew.artifact_store.dashboard_package.parse_package", _never)
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 409
    assert body["code"] == "dashboard_live_source"
    assert not wired.app.get("member_dashboard_live_pages"), "a page was armed anyway"


@pytest.mark.asyncio
async def test_an_artifact_of_another_kind_is_not_parsed_as_a_package(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record's own kind decides, not the bytes. A document whose text happens to
    parse as a package is still not a dashboard."""
    note = SimpleNamespace(
        slug=PACKAGE_SLUG, content=dp.dump_package(_package()), version=1, kind="note"
    )
    monkeypatch.setattr(
        "kiro_crew.artifacts.get_default_store", lambda: SimpleNamespace(get=lambda slug: note)
    )
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 409
    assert body["code"] == "dashboard_not_a_package"


@pytest.mark.asyncio
async def test_a_copied_artifact_with_provenance_is_still_served(wired: Any) -> None:
    """`source_copy_only` means the artifact owns its bytes and the path is provenance.
    Refusing those too would turn every promoted-then-copied package away."""
    copied = _Linked(dp.dump_package(_package()))
    copied.source_copy_only = True
    wired.store.get = lambda slug: copied  # type: ignore[method-assign]
    status, _body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200


def _tree_package() -> dict[str, Any]:
    """A package binding a TREE fold, which is the only kind a refetch re-reads."""
    return dp.validate_package(
        {
            "kind": "dashboard",
            "bound_to": f"crewmate:{SLUG}",
            "model": {
                "types": {
                    "roots": {
                        "type": "array",
                        "label": "Roots",
                        "source": {"fold": "worktree", "path": "roots"},
                    }
                }
            },
            # Its own markup: the view may only bind names the model declares, and
            # this model declares the tree field rather than the shared page's.
            "view": {
                "html": '<p data-dashboard-field="roots"></p>'
                "<script>window.kirocrew.subscribe('roots', (v) => v);</script>"
            },
            "theme": {"tokens": {}},
        }
    )


def test_two_refetches_cannot_publish_an_older_tree_under_a_newer_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE SAME ATTACK ON THE DATA, one read further out.

    A tree has no bus scope, so a refetch is the only thing that moves it -- and a page
    asks for one on every gap it sees, so two can be in flight at once. With the tree
    read outside the page's lock, the OLDER of the two can finish last, replace the
    cache with what it saw, and then take the next version. The page accepts that as
    the latest and draws a tree from before the newer one, with `stale: false` beside
    it: no gap to detect, because the numbers are consecutive.

    Driven deterministically: the first read parks inside the projection call, the
    second refetch runs to completion meanwhile, and the assertion is on what the
    coordinator could SEE -- whether the page's lock was free while that read was open.
    """
    import threading

    frames: list[dict[str, Any]] = []
    live = mdf.LivePage(SLUG, SLUG, _tree_package(), frames.append)
    reads: list[str] = []
    inside = threading.Event()
    released = threading.Event()
    probed = threading.Event()
    held_at_probe: list[bool] = []

    def _read_tree(root: str, name: str) -> Any:
        # Call 1 is ``arm``'s own read, which answers at once -- the page has to come
        # up before anything can refetch. Call 2 is the first refetch's, and it PARKS
        # inside the projection, which is the window the second refetch runs in. Later
        # calls answer with the newer value.
        reads.append(name)
        if len(reads) == 2:
            inside.set()
            assert released.wait(timeout=10), "the coordinator never released the read"
            return SimpleNamespace(value=_tree_value("older"), seq=1)
        return SimpleNamespace(value=_tree_value("newer"), seq=2)

    monkeypatch.setattr("kiro_crew.crew_log.projection.read_tree_projection", _read_tree)
    live.arm()
    try:

        def _second_refetch() -> None:
            assert inside.wait(timeout=10), "the first read never reached its window"
            # AFTER the probe, so the probe has no competitor. This refetch takes the
            # same lock, so a probe racing it could read ITS hold as the parked read's
            # and report "held" in the very case this test exists to catch.
            assert probed.wait(timeout=10), "the coordinator never probed"
            live.refetch()

        def _coordinate() -> None:
            # Holds no lock of this page, so progress never depends on a clock. What it
            # RECORDS is the discriminator: with the tree read inside the page's lock,
            # no probe can take that lock while the read is open.
            #
            # ONE non-blocking attempt, with no retry and nothing to wait on. The window
            # is held open by this thread: the parked read does not return until
            # `released` is set below, and the second refetch does not start until
            # `probed` is. So at this instant the lock is held by the parked read or by
            # nobody, and which one it is IS the answer -- a poll would be asking the
            # same decided question repeatedly on a clock.
            assert inside.wait(timeout=10), "the first read never reached its window"
            took_it = live._lock.acquire(blocking=False)
            if took_it:
                live._lock.release()
            held_at_probe.append(not took_it)
            probed.set()
            released.set()

        second = threading.Thread(target=_second_refetch, name="second-refetch")
        coordinator = threading.Thread(target=_coordinate, name="release")
        second.start()
        coordinator.start()
        first = live.refetch()
        second.join(timeout=10)
        coordinator.join(timeout=10)
        assert not second.is_alive(), "the second refetch did not finish"
        assert not coordinator.is_alive(), "the coordinator did not finish"

        assert held_at_probe == [True], (
            "the page's lock was free while a tree read was open, so the read is not "
            "inside it -- the window an older tree wins in"
        )
        # And the invariant the page depends on, whatever the interleaving: the highest
        # version sent carries the newest tree.
        everything = [(f["version"], f["fields"].get("roots")) for f in frames]
        everything.append((first["version"], first["fields"].get("roots")))
        _highest, newest = max(everything, key=lambda pair: pair[0])
        assert newest == ["chat-1-1790"], f"the highest version carries {newest!r}"
    finally:
        live.disarm()


def test_a_refetch_cannot_publish_a_stale_snapshot_under_a_newer_version() -> None:
    """THE ATTACK ON THE DATA, which is a lost update rather than an intruder.

    A fold advances between a refetch's value capture and the moment it takes a version.
    The patch carrying the NEWER value goes out first; the refetch then stamps the OLDER
    snapshot with the next version, and the page accepts it as the latest and silently
    goes backwards -- with no gap for it to detect, because the numbers are consecutive.

    Driven deterministically: a second thread advances the fold while the refetch is
    inside its own read, and the assertion is on the ORDER of what was sent.
    """
    import threading

    frames: list[dict[str, Any]] = []
    live = mdf.LivePage(SLUG, SLUG, _package(), frames.append)
    live.arm()
    try:
        _publish("work", {"conductor": {"goal": "first"}}, 1)
        inside = threading.Event()
        released = threading.Event()
        published = threading.Event()
        probed = threading.Event()
        held_at_probe: list[bool] = []
        real_values = live._values

        def _slow_values() -> Any:
            out = real_values()
            inside.set()
            assert released.wait(timeout=10), "the coordinator never released the read"
            return out

        live._values = _slow_values  # type: ignore[method-assign]

        def _advance() -> None:
            # WAITS FOR THE WINDOW. Publishing on thread start instead made this test
            # pass against the broken code: the fold often moved before the read, so
            # the refetch legitimately carried the newer value and the stale-snapshot
            # branch was never taken.
            assert inside.wait(timeout=10), "the read never reached its window"
            # And waits for the PROBE, because `_on_fold` takes the same lock: a publish
            # racing the probe could be read as the parked read's hold and report "held"
            # in the very case this test exists to catch. The window is still open --
            # the coordinator sets `released` only after this is allowed to run.
            assert probed.wait(timeout=10), "the coordinator never probed"
            _publish("work", {"conductor": {"goal": "second"}}, 2)
            published.set()

        def _coordinate() -> None:
            # Released by a thread holding no lock of this page, so progress never
            # depends on a timeout. What it RECORDS is the discriminator, and it ASKS THE
            # LOCK rather than inferring from a clock: with the read inside the page's
            # lock, the read is parked holding it and no probe can take it; with the read
            # outside, the lock is free and the probe takes it.
            #
            # ONE non-blocking attempt, with no retry and nothing to wait on. Every other
            # party is parked at this instant -- the read on `released`, the publisher on
            # `probed` -- so the lock is held by the parked read or by nobody, and which
            # one it is IS the answer. A poll would be asking a decided question again on
            # a clock, which passes on a fast runner and fails on a slow one.
            assert inside.wait(timeout=10), "the read never reached its window"
            took_it = live._lock.acquire(blocking=False)
            if took_it:
                live._lock.release()
            held_at_probe.append(not took_it)
            probed.set()
            released.set()

        racer = threading.Thread(target=_advance, name="fold-advance")
        coordinator = threading.Thread(target=_coordinate, name="release")
        racer.start()
        coordinator.start()
        whole = live.refetch()
        racer.join(timeout=10)
        coordinator.join(timeout=10)
        assert not racer.is_alive(), "the publishing thread did not finish"
        assert not coordinator.is_alive(), "the coordinator did not finish"
        live._values = real_values  # type: ignore[method-assign]

        assert held_at_probe == [True], (
            "the page's lock was free while the refetch sat mid-read, which means the "
            "read is not inside it -- the window a stale snapshot wins in"
        )
        # And the invariant the page depends on: the highest version sent carries the
        # newest value. Asserted whatever the interleaving, so this holds even if the
        # ordering above ever changes.
        everything = [(f["version"], f["fields"].get("goal")) for f in frames]
        everything.append((whole["version"], whole["fields"].get("goal")))
        _highest, newest_value = max(everything, key=lambda pair: pair[0])
        assert newest_value == "second", (
            f"the highest version sent carries {newest_value!r}, so the page is told an "
            "older value is the latest and goes backwards with no gap to detect"
        )
    finally:
        live.disarm()


@pytest.mark.asyncio
async def test_two_packages_for_one_crewmate_do_not_feed_each_other(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One crewmate, two packages open: each frame names its own, and arming the second
    does not disarm the first.

    The socket is per connection, so every frame reaches every tab the owner has open.
    Keyed by crewmate alone, the second GET disarmed the first page and both tabs then
    read one counter -- so a tab applied patches computed for a layout it was not
    rendering, and its refetch answered with the other package's whole read.
    """
    second = "atlas-dashboard-2"
    store = {PACKAGE_SLUG: _package(), second: _package()}
    monkeypatch.setattr(
        "kiro_crew.artifacts.get_default_store",
        lambda: SimpleNamespace(
            get=lambda slug, version=None: SimpleNamespace(
                slug=slug,
                content=dp.dump_package(store[slug]),
                version=APPLIED_VERSION,
                kind="dashboard",
                events=list(APPLIED_EVENTS),
            )
        ),
    )
    for which in (PACKAGE_SLUG, second):
        status, _body = await _call(
            md.api_member_dashboard,
            _request(
                wired.app, "GET", f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={which}"
            ),
        )
        assert status == 200
    pages = wired.app["member_dashboard_live_pages"]
    assert set(pages) == {
        (SLUG, PACKAGE_SLUG),
        (SLUG, second),
    }, "the live pages are not keyed by package, so one crewmate cannot hold two"

    wired.frames.clear()
    _publish("work", {"conductor": {"goal": "moved"}}, 1)
    named = {data["dashboard"] for _kind, data in wired.frames}
    assert named == {PACKAGE_SLUG, second}, (
        f"frames named {sorted(named)}: a frame that does not name its package cannot "
        "be dropped by a tab showing the other one"
    )


@pytest.mark.asyncio
async def test_a_refetch_must_name_the_package_it_wants(wired: Any) -> None:
    """Answering an unnamed refetch with whatever is armed would replace one page's
    values with another page's read wholesale."""
    await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    status, body = await _call(
        md.api_member_dashboard_refetch,
        _request(wired.app, "POST", f"/api/members/{SLUG}/dashboard/refetch?member={CREW}"),
    )
    assert status == 400
    assert body["code"] == "missing_dashboard"
    status, body = await _call(
        md.api_member_dashboard_refetch,
        _request(
            wired.app,
            "POST",
            f"/api/members/{SLUG}/dashboard/refetch?member={CREW}&dashboard={PACKAGE_SLUG}",
        ),
    )
    assert status == 200
    assert body["dashboard"] == PACKAGE_SLUG
