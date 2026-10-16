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

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.crew_log import bus as crew_log_bus
from kiro_crew.dashboard.handlers import artifacts as artifacts_handler
from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf
from kiro_crew.platform.context import redact_via_context


def _owner_write(store: Any, slug: str, **kwargs: Any) -> Any:
    """A dashboard content write under the record's CURRENT token.

    ``ArtifactStore.update`` refuses a kind or content write on a record whose kind is
    ``dashboard`` unless it names the read it was authorized against, and an absent
    token is a refusal -- see
    ``test_dashboard_package_owner_gate.TestAnUntokenedWriteCannotTakeADashboardOver``.
    The route reads the slug before every write and hands the store that token; a test
    playing the owner reads it here for the same reason.
    """
    token = store.get(slug, assign_token=True).content_token
    return store.update(slug, snapshot_token=token, **kwargs)


CREW = "atlas"
SLUG = "atlas"
OWNER = "U_OWNER"
PACKAGE_SLUG = "atlas-dashboard"

#: Above any revision the eager folder has already minted for the slot: the feed
#: subscribes with a baseline, and the cache drops an event that is not newer.
_REVISION_BASE = 2_000_000

PAGE = (
    '<p data-dashboard-field="goal"></p>'
    "<script>window.kirocrew.subscribe('goal', (v) => v);</script>"
)


def _package(bound_to: str = f"crewmate:{SLUG}", *, html: str = PAGE) -> dict[str, Any]:
    return dp.validate_package(
        {
            "kind": "dashboard",
            "bound_to": bound_to,
            "model": {
                "types": {
                    "goal": {
                        "type": "text",
                        "label": "Goal",
                        "source": {"fold": "work", "path": "conductor.goal"},
                    }
                }
            },
            "view": {"html": html},
            "theme": {"tokens": {}},
        }
    )


def _fake_cfg() -> Any:
    return SimpleNamespace(
        agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew", description="d", model="m1")},
        default_agent=CREW,
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
    )


#: The lifecycle event a PERSON's approval leaves on the record: a version cut with the
#: `user` actor. The render path serves the newest version carrying one, so a fixture
#: standing in for an APPLIED package has to carry it -- a stub without it models a
#: package whose owner agent has written pages nobody approved, which renders nothing.
APPLIED_VERSION = 4
APPLIED_EVENTS: list[dict[str, Any]] = [
    {"type": "iterated", "by": "agent", "version": 3},
    {"type": "edited", "by": "user", "version": APPLIED_VERSION},
]


class _Store:
    """The artifact store, narrowed to what the route reads: one APPLIED package.

    `get(slug)` answers the record (with its events) and `get(slug, version=N)` the
    bytes of that version, which is the pair the render path uses: the events say which
    version a person applied, and the versioned read is what it serves.
    """

    def __init__(self, content: str) -> None:
        self.content = content
        self.version_reads: list[int] = []

    def get(self, slug: str, *, version: int | None = None) -> Any:
        if slug != PACKAGE_SLUG:
            from kiro_crew.artifacts import ArtifactError

            raise ArtifactError(f"no artifact {slug!r}")
        if version is not None:
            from kiro_crew.artifacts import ArtifactError

            self.version_reads.append(version)
            if version != APPLIED_VERSION:
                raise ArtifactError(f"version {version} not found for {slug}")
        return SimpleNamespace(
            slug=slug,
            content=self.content,
            version=APPLIED_VERSION,
            kind="dashboard",
            events=list(APPLIED_EVENTS),
        )


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The route's app, with the frames it broadcasts recorded."""
    frames: list[tuple[str, Any]] = []
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    monkeypatch.setattr(
        state, "broadcast_ws_owners", lambda kind, data: frames.append((kind, data)), raising=False
    )
    app = web.Application()
    app["state"] = state
    store = _Store(dp.dump_package(_package()))
    monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
    # The slot the fold values are keyed by, resolved rather than guessed in
    # production; pinned here so the publish below names the same one.
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: SLUG)
    monkeypatch.setattr(md, "_write_session", lambda slug, member: "")
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, config=None: SLUG)
    yield SimpleNamespace(app=app, frames=frames, store=store)
    for page in list(app.get("member_dashboard_live_pages", {}).values()):
        page.disarm()


def _request(app: web.Application, method: str, path: str) -> web.Request:
    req = make_mocked_request(method, path, app=app, match_info={"slug": SLUG})
    req["app"] = ""
    req["user"] = OWNER
    return req


async def _call(handler: Any, req: web.Request) -> tuple[int, Any]:
    with patch.object(md.KiroCrewConfig, "load", return_value=_fake_cfg()):
        resp = await handler(req)
    return resp.status, json.loads(resp.body)


def _publish(fold: str, value: dict[str, Any], revision: int) -> None:
    crew_log_bus.publish(
        crew_log_bus.FOLD_ADVANCED,
        crew_log_bus.FoldAdvanced(
            scope=crew_log_bus.SCOPE_SLOT,
            key=SLUG,
            fold=fold,
            revision=_REVISION_BASE + revision,
            seq=revision,
            value=value,
        ),
    )


PATH = f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={PACKAGE_SLUG}"


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
async def test_an_unlocked_crewmate_still_gets_its_package(wired: Any) -> None:
    """The limit: the lock is the only thing that outranks the package, so a crewmate
    nobody locked is unaffected -- which is every crewmate today."""
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200
    assert PAGE in body["rendered_html"]


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
        source = Path(artifacts_handler.__file__).read_text(encoding="utf-8")
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
    assert "export const CREW_DASHBOARD_SANDBOX = 'allow-scripts'" in text, (
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


# --------------------------------------------------------------------------- #
# The resolver that makes the package page reachable at all. Without it nothing
# in the product knew a package's slug, so the route, the push and the refetch
# were unreachable code -- which is what First Principles blocked on.
# --------------------------------------------------------------------------- #


def _record(slug: str, package: dict[str, Any], **over: Any) -> Any:
    #: APPLIED by default, like `_Store`: a record carrying only agent-actor events
    #: models a package nobody approved, and the render path draws nothing for it.
    return SimpleNamespace(
        slug=slug,
        content=dp.dump_package(package),
        version=APPLIED_VERSION,
        kind="dashboard",
        events=list(over.get("events", APPLIED_EVENTS)),
        source_path=over.get("source_path", ""),
        source_copy_only=over.get("source_copy_only", False),
    )


@pytest.fixture()
def library(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The artifact library, listed by kind the way the resolver lists it."""
    held: dict[str, Any] = {}

    def _list(kind: str | None = None, **_k: Any) -> list[Any]:
        # METADATA ONLY, which is what the real store answers with: `list()` is built
        # from meta.json and leaves `content` None, because the bytes live in the
        # artifact's own file and only `get()` loads them. A fake that carries content
        # here is how a resolver reading the listed record passes every test while
        # resolving nothing in production.
        return [
            SimpleNamespace(**{**vars(r), "content": None})
            for r in held.values()
            if kind in (None, "dashboard")
        ]

    monkeypatch.setattr(
        "kiro_crew.artifacts.get_default_store",
        lambda: SimpleNamespace(list=_list, get=lambda s, version=None: held[s]),
    )
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, config=None: SLUG)
    return held


class TestTheBindingResolver:
    """``resolve_bound_slug``, the ONE lookup that answers "which package is bound here".

    These pin behaviour this route needs and the shared function now owns. They were
    written against a private copy in this handler; the copy is gone and the behaviour
    moved into the shared function, so the tests moved with it rather than being deleted
    alongside it -- a deleted test is how a protection quietly stops being one.

    The route's own wrapper (`md._bound_package_slug`) maps every refusal to "", because
    a read that draws a page answers an unprovable binding with the page the crewmate
    already has. Both layers are asserted: the function refuses, and the route falls back.
    """

    def _resolved(self, member: str = CREW) -> Any:
        from kiro_crew.artifact_store.dashboard_package import resolve_bound_slug

        return resolve_bound_slug(f"crewmate:{SLUG}")

    def test_it_finds_the_package_bound_to_this_crewmate(self, library: Any) -> None:
        library[PACKAGE_SLUG] = _record(PACKAGE_SLUG, _package())
        assert self._resolved() == PACKAGE_SLUG
        assert md._bound_package_slug(CREW) == PACKAGE_SLUG

    def test_a_package_bound_elsewhere_is_not_this_crewmates(self, library: Any) -> None:
        library["other"] = _record("other", _package(bound_to="crewmate:nyx"))
        assert self._resolved() is None
        assert md._bound_package_slug(CREW) == ""

    def test_two_packages_claiming_one_crewmate_are_refused(self, library: Any) -> None:
        """THE PROTECTION A PLAIN DEDUP WOULD COST, which is why it moved into the
        shared function rather than going with the copy.

        Returning the first match is arbitrary -- the listing order is the store's, not
        a statement about which package is live -- and a reader cannot tell an arbitrary
        pick from a correct one, so a relisted library silently swaps which page a
        crewmate shows. Both are bound to the same crewmate, so nothing shows a
        stranger's work; what is lost is determinism, and the honest answer is that the
        binding is ambiguous.
        """
        from kiro_crew.artifact_store.dashboard_package import (
            BindingAmbiguous,
            BindingLookupIncomplete,
        )

        library["a"] = _record("a", _package())
        library["b"] = _record("b", _package())
        with pytest.raises(BindingAmbiguous) as refused:
            self._resolved()
        # Both named, because the remedy is to delete or rebind one of them.
        assert "'a'" in str(refused.value) and "'b'" in str(refused.value)
        # The SUBCLASS is the no-caller-change half: every existing handler for an
        # unprovable binding catches this one too.
        assert isinstance(refused.value, BindingLookupIncomplete)
        assert md._bound_package_slug(CREW) == ""

    def test_an_unreadable_record_after_a_match_leaves_uniqueness_unproven(
        self, library: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same fact as ambiguity, seen from the other side: the record that could
        not be read may be the SECOND claimant, so the match is not proven unique."""
        from kiro_crew.artifact_store.dashboard_package import BindingLookupIncomplete
        from kiro_crew.artifacts import ArtifactError

        library[PACKAGE_SLUG] = _record(PACKAGE_SLUG, _package())
        library["unreadable"] = _record("unreadable", _package())
        real = library.copy()

        def _get(slug: str, version: int | None = None) -> Any:
            if slug == "unreadable":
                raise ArtifactError("cannot open")
            return real[slug]

        store = SimpleNamespace(
            list=lambda kind=None, **_k: [
                SimpleNamespace(**{**vars(r), "content": None}) for r in real.values()
            ],
            get=_get,
        )
        monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
        with pytest.raises(BindingLookupIncomplete) as refused:
            self._resolved()
        assert "only one bound to" in str(refused.value)
        assert md._bound_package_slug(CREW) == ""

    def test_a_live_backed_record_is_not_a_candidate(self, library: Any) -> None:
        """Its bytes are a file's current contents, which nothing in the artifact API
        gates, so it must not be considered this crewmate's page -- not even to the
        extent of claiming the binding, which would take it from the real one."""
        library[PACKAGE_SLUG] = _record(PACKAGE_SLUG, _package(), source_path="/tmp/p.json")
        assert self._resolved() is None
        assert md._bound_package_slug(CREW) == ""

    def test_a_copied_record_with_provenance_still_counts(self, library: Any) -> None:
        library[PACKAGE_SLUG] = _record(
            PACKAGE_SLUG, _package(), source_path="/tmp/p.json", source_copy_only=True
        )
        assert self._resolved() == PACKAGE_SLUG
        assert md._bound_package_slug(CREW) == PACKAGE_SLUG

    def test_a_live_backed_duplicate_does_not_make_a_binding_ambiguous(self, library: Any) -> None:
        """Why the live-backed skip has to precede the ambiguity check: an ungated
        writer could otherwise point a second record at this binding and turn a
        resolvable dashboard into a refused one."""
        library[PACKAGE_SLUG] = _record(PACKAGE_SLUG, _package())
        library["live"] = _record("live", _package(), source_path="/tmp/p.json")
        assert self._resolved() == PACKAGE_SLUG

    def test_an_empty_library_answers_absent_rather_than_guessing(self, library: Any) -> None:
        assert self._resolved() is None
        assert md._bound_package_slug(CREW) == ""

    def test_a_store_that_raises_answers_empty_at_the_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The route's own wrapper, which is what keeps a broken library from costing a
        crewmate its tab rather than only its package page."""

        def _boom() -> Any:
            raise OSError("the library is unreadable")

        monkeypatch.setattr("kiro_crew.artifacts.get_default_store", _boom)
        assert md._bound_package_slug(CREW) == ""

    def test_it_resolves_against_the_REAL_store_not_a_fake_that_carries_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE FIXTURE'S PREMISE, measured rather than assumed.

        Every other test here hands the resolver a fake library, and a fake is free to
        put content on a listed record. The real store does not: `list()` reads
        meta.json and `content` comes back None, so a resolver that parsed the listed
        record finds nothing to parse and every crewmate silently falls back to the
        template page.

        Written against a real `ArtifactStore` on disk for that reason -- it is the only
        thing that can tell the two apart.
        """
        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "artifacts")
        store.create(name="page", content=dp.dump_package(_package()), kind="dashboard")
        listed = store.list(kind="dashboard")
        assert len(listed) == 1
        assert listed[0].content is None, (
            "the real store now carries content on a listed record, so the resolver's "
            "read-by-slug is no longer load-bearing -- re-check this before simplifying it"
        )
        monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
        monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, config=None: SLUG)
        assert self._resolved() == listed[0].slug
        assert md._bound_package_slug(CREW) == listed[0].slug

    def test_there_is_only_one_binding_lookup_in_the_product(self) -> None:
        """The subtraction FP asked for, pinned so it stays subtracted: one function
        answers "which package is bound here", and nothing re-implements it."""
        root = Path(mdf.__file__).resolve().parents[4]
        hits = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "bound_package_slug" in path.read_text(encoding="utf-8", errors="ignore")
        )
        assert hits == [
            # The route's own thin wrapper, which only maps the refusals to "".
            "src/kiro_crew/dashboard/handlers/member_dashboard.py",
            # The two suites that name the wrapper to assert its fallback.
            "test/test_artifact_dashboard_package.py",
            "test/test_member_dashboard_package_route.py",
        ], f"a second binding lookup appeared: {hits}"


@pytest.mark.asyncio
async def test_the_route_arms_a_live_page_through_the_resolver(
    wired: Any, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE CALLER First Principles asked for, end to end.

    An ordinary dashboard GET with NO `?dashboard=` -- which is what the tab sends --
    resolves the crewmate's binding, serves the package page, and arms the live page.
    Before this, every one of those was unreachable.
    """
    library[PACKAGE_SLUG] = _record(PACKAGE_SLUG, _package())
    status, body = await _call(
        md.api_member_dashboard,
        _request(wired.app, "GET", f"/api/members/{SLUG}/dashboard?member={CREW}"),
    )
    assert status == 200
    assert body["dashboard"] == PACKAGE_SLUG, "the body does not name the package it served"
    assert PAGE in body["rendered_html"], "the package's own page was not served"
    pages = wired.app["member_dashboard_live_pages"]
    assert (SLUG, PACKAGE_SLUG) in pages, "no live page was armed"
    # And it pushes: the whole point of arming one.
    wired.frames.clear()
    _publish("work", {"conductor": {"goal": "reachable"}}, 1)
    assert wired.frames, "the armed page pushed nothing"
    assert wired.frames[-1][1]["dashboard"] == PACKAGE_SLUG


@pytest.mark.asyncio
async def test_a_crewmate_with_no_package_still_gets_its_template_page(
    wired: Any, library: Any
) -> None:
    """The fall-through that keeps every crewmate working: an unresolved binding means
    the template instance, which is the behaviour on the base."""
    status, _body = await _call(
        md.api_member_dashboard,
        _request(wired.app, "GET", f"/api/members/{SLUG}/dashboard?member={CREW}"),
    )
    assert status == 200
    assert not wired.app.get("member_dashboard_live_pages"), "a page was armed with no package"


# --------------------------------------------------------------------------
# 5. the slug is a name too, and the armed pages are bounded
# --------------------------------------------------------------------------

#: Artifact slugs that are legal names AND strings the egress redactor rewrites. An
#: artifact slug is ``[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?``, which forbids ``_`` and
#: upper case -- so a `ghp_` token and an AWS key cannot be slugs at all, and these two
#: are the whole overlap: a Slack bot token and a GitLab PAT.
#:
#: ASSEMBLED FROM PARTS, never written out: as a literal the first one is a shape
#: GitHub's own push protection refuses, and a test fixture is not worth an exemption on
#: a secret-scanning rule. Nothing is lost, because each test asserts at runtime that
#: the redactor really rewrites the value it was handed -- the fixture's whole premise is
#: measured rather than spelled.
_CREDENTIAL_SLUGS = [
    "-".join(("xoxb", "1" * 12, "a" * 24)),
    "glpat-" + "a" * 20,
]


class _StoreAt:
    """One package under the slug the test names, with every read recorded."""

    def __init__(self, slug: str, content: str) -> None:
        self.slug = slug
        self.content = content
        self.reads: list[str] = []

    def _record(self) -> Any:
        return SimpleNamespace(
            slug=self.slug,
            content=self.content,
            version=APPLIED_VERSION,
            kind="dashboard",
            events=list(APPLIED_EVENTS),
        )

    def get(self, slug: str, *, version: int | None = None) -> Any:
        self.reads.append(slug)
        if slug != self.slug:
            from kiro_crew.artifacts import ArtifactError

            raise ArtifactError(f"no artifact {slug!r}")
        return self._record()

    def list(self, kind: str | None = None, **_k: Any) -> list[Any]:
        return [self._record()]


class _StoreAny:
    """One applied package, served under whatever slug is asked for."""

    def __init__(self, content: str) -> None:
        self.content = content

    def get(self, slug: str, *, version: int | None = None) -> Any:
        return SimpleNamespace(
            slug=slug,
            content=self.content,
            version=APPLIED_VERSION,
            kind="dashboard",
            events=list(APPLIED_EVENTS),
        )


@pytest.mark.parametrize("bad", _CREDENTIAL_SLUGS)
@pytest.mark.asyncio
async def test_a_credential_shaped_slug_is_refused_before_the_package_is_read(
    wired: Any, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A slug travels further than any other name on this surface: the body carries it
    twice, every pushed frame carries it, and the refetch answers with it.

    The premise is asserted rather than assumed -- the shape has to be one the redactor
    actually rewrites, or this test passes while measuring nothing. A package DOES
    exist under the slug, which is what makes the refusal the subject: without it the
    route reads that package and serves a 200 naming it.

    Refused rather than redacted, because the page, the push and the refetch JOIN on
    this string: rewriting it would make the three disagree.
    """
    assert (
        redact_via_context(bad) != bad
    ), "this fixture is not a credential shape, so the test proves nothing"
    store = _StoreAt(bad, dp.dump_package(_package()))
    monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
    status, body = await _call(
        md.api_member_dashboard,
        _request(
            wired.app,
            "GET",
            f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={bad}",
        ),
    )
    assert status == 409
    assert body["code"] == "dashboard_slug_refused"
    assert bad not in json.dumps(body), "the refusal hands the credential back"
    assert store.reads == [], "the package was read before its slug was judged"
    assert not wired.app.get("member_dashboard_live_pages"), "a page was armed anyway"


@pytest.mark.asyncio
async def test_a_clean_slug_the_caller_names_is_still_served(wired: Any) -> None:
    """The guard answers a question about this one string and nothing else: an ordinary
    package name is unaffected."""
    status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200
    assert body["dashboard"] == PACKAGE_SLUG


@pytest.mark.asyncio
async def test_a_bound_package_so_named_costs_the_page_and_not_the_tab(
    wired: Any, library: Any
) -> None:
    """NOBODY ASKED for this package -- the slug came from the crewmate's own binding.

    So it is treated as an unresolved binding, which is the template page every
    crewmate has on this branch, rather than as the 409 a caller who named a slug
    gets. A credential pasted into an artifact's NAME costs its owner the package
    page; it does not cost them the tab.
    """
    bad = _CREDENTIAL_SLUGS[0]
    library[bad] = _record(bad, _package())
    status, body = await _call(
        md.api_member_dashboard,
        _request(wired.app, "GET", f"/api/members/{SLUG}/dashboard?member={CREW}"),
    )
    assert status == 200, "the crewmate lost its whole tab, not just its package page"
    assert body.get("dashboard", "") == "", "the body names the package it refused"
    assert bad not in json.dumps(body)
    assert not wired.app.get("member_dashboard_live_pages"), "a page was armed anyway"


@pytest.mark.asyncio
async def test_a_page_beyond_the_cap_evicts_the_least_recently_armed_one(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each armed page holds a bus subscription per fold its model reads, and nothing
    disarms one when its tab closes -- a browser cannot say so.

    Measured on the BUS rather than on the map: the evicted page is gone from the map
    either way, and what matters is that its subscriptions went with it.
    """
    monkeypatch.setattr(md, "_MAX_LIVE_PAGES", 2)
    monkeypatch.setattr(
        "kiro_crew.artifacts.get_default_store",
        lambda: _StoreAny(dp.dump_package(_package())),
    )
    for name in ("one", "two", "three"):
        status, _body = await _call(
            md.api_member_dashboard,
            _request(
                wired.app,
                "GET",
                f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={name}",
            ),
        )
        assert status == 200
    pages = wired.app["member_dashboard_live_pages"]
    assert sorted(key[1] for key in pages) == [
        "three",
        "two",
    ], "the cap did not hold, or it evicted a page newer than the oldest one"
    wired.frames.clear()
    _publish("work", {"conductor": {"goal": "after the eviction"}}, 1)
    assert wired.frames, "the pages left behind push nothing, so this measures nothing"
    pushed = {frame[1]["dashboard"] for frame in wired.frames}
    assert "one" not in pushed, "the evicted page is still subscribed to the bus"


@pytest.mark.asyncio
async def test_the_page_this_request_armed_is_never_the_one_evicted(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap below one would otherwise disarm the page whose read is being answered,
    and the response would carry a first paint nothing can ever patch."""
    monkeypatch.setattr(md, "_MAX_LIVE_PAGES", 0)
    status, _body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
    assert status == 200
    assert list(wired.app["member_dashboard_live_pages"]) == [(SLUG, PACKAGE_SLUG)]


#: The preview read as the tab actually sends it: `instance.preview_url` names no
#: package, so the binding is what would otherwise fill one in.
PREVIEW_PATH = f"/api/members/{SLUG}/dashboard?member={CREW}&preview=1"

#: The ordinary live read with no `dashboard=`, which is what the binding answers for.
NO_SLUG_PATH = f"/api/members/{SLUG}/dashboard?member={CREW}"


class _ListingStore(_Store):
    """``_Store`` plus the listing the BINDING lookup needs.

    The plain stub answers only `get`, so `bound_package_slug` fails its scan and
    returns "" -- which is the same answer the fix produces and would make these tests
    pass over the defect. Here the binding really resolves, so a preview read that
    still reaches the package branch serves a page.
    """

    def list(self, kind: str = "") -> list[Any]:
        return [
            SimpleNamespace(
                slug=PACKAGE_SLUG,
                kind="dashboard",
                content=None,
                source_path="",
                source_copy_only=False,
            )
        ]


@pytest.fixture()
def bound(wired: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """``wired`` with the crewmate's binding actually resolvable."""
    store = _ListingStore(dp.dump_package(_package()))
    monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
    return SimpleNamespace(app=wired.app, frames=wired.frames, store=store)


@pytest.mark.asyncio
async def test_the_binding_resolves_in_this_fixture(bound: Any) -> None:
    """The control. Without it, every test below passes whenever the lookup is broken
    for an unrelated reason, which is exactly how the defect survived."""
    assert md._bound_package_slug(CREW) == PACKAGE_SLUG
    status, body = await _call(md.api_member_dashboard, _request(bound.app, "GET", NO_SLUG_PATH))
    assert status == 200
    assert body["dashboard"] == PACKAGE_SLUG, "the binding is not reaching the route"


@pytest.mark.asyncio
async def test_a_preview_read_does_not_serve_the_bound_package(bound: Any) -> None:
    """A PREVIEW SHOWS THE STAGED PAGE OR NOTHING -- never the live one.

    `preview=1` exists for one purpose: show a person the page they are about to say
    yes to. The tab's preview link carries no `dashboard=`, so a binding resolved ahead
    of this branch serves a crewmate with a bound package its LIVE package page under a
    panel labelled "not applied yet" -- the person then approves a page they never saw,
    which is the one thing this route must not do.

    Nothing is staged here, so the honest answer is `no_preview`. What matters is that
    it is not 200 with the package's page in it.
    """
    status, body = await _call(md.api_member_dashboard, _request(bound.app, "GET", PREVIEW_PATH))
    assert status == 404, body
    assert body["code"] == "no_preview"


@pytest.mark.asyncio
async def test_a_preview_read_arms_no_live_page(bound: Any) -> None:
    """The other half of the same defect. Serving the package also ARMED it, so a
    preview re-pointed the push machinery at the live page and the open live tab's own
    version counter jumped -- gaps the real page can only recover from with a refetch.
    """
    await _call(md.api_member_dashboard, _request(bound.app, "GET", PREVIEW_PATH))
    assert not bound.app.get("member_dashboard_live_pages")


@pytest.mark.asyncio
async def test_a_preview_read_does_not_even_look_the_binding_up(bound: Any) -> None:
    """Measured as a call count, because the fix is an ORDER and an order is what a
    later edit undoes silently: resolving the binding and then declining to use it
    would still pay a library listing on every preview, and would leave the package
    branch one `if` away from running again.
    """
    looked: list[str] = []
    original = md._bound_package_slug

    def _counted(member: str) -> str:
        looked.append(member)
        return original(member)

    with patch.object(md, "_bound_package_slug", _counted):
        await _call(md.api_member_dashboard, _request(bound.app, "GET", PREVIEW_PATH))
    assert looked == []


@pytest.mark.asyncio
async def test_a_package_named_explicitly_still_wins_over_preview(bound: Any) -> None:
    """The caller that NAMES a package asked for that package's page, and the tab never
    sends both. Kept working so the fix is "a preview resolves no binding", not "a
    preview cannot reach a package", which would make the two query parameters fight.
    """
    path = f"{PATH}&preview=1"
    status, body = await _call(md.api_member_dashboard, _request(bound.app, "GET", path))
    assert status == 200
    assert body["dashboard"] == PACKAGE_SLUG


class _VanishingStore:
    """The bound package is there when the BINDING is resolved and gone when the page
    is read.

    The reachable shape of "a bound package with nothing to serve". A stored record
    always carries a version from the moment `create` makes it, so a record with no
    version does not exist; what does happen is that the record goes away between the
    listing the binding is resolved from and the read that would serve it -- a delete,
    a prune, or a library pointing at a record this process cannot open.

    Modelled by letting the first `get` succeed (the resolver's) and refusing every
    later one (the route's), which is exactly that ordering.
    """

    def __init__(self, content: str) -> None:
        self.content = content
        self.gets: list[str] = []

    def list(self, kind: str = "") -> list[Any]:
        return [
            SimpleNamespace(
                slug=PACKAGE_SLUG,
                kind="dashboard",
                content=None,
                source_path="",
                source_copy_only=False,
            )
        ]

    def get(self, slug: str) -> Any:
        from kiro_crew.artifacts import ArtifactError

        self.gets.append(slug)
        if len(self.gets) == 1 and slug == PACKAGE_SLUG:
            return SimpleNamespace(slug=slug, content=self.content, version=1, kind="dashboard")
        raise ArtifactError(f"no artifact {slug!r}")


class TestABoundPackageWithNothingToServe:
    """A BOUND PACKAGE WITH NO VERSION RENDERS NOTHING AT ALL.

    `rfc-crewmate-dynamic-dashboard.md`'s decision 3 puts it in those words: naming a
    package and publishing one are different acts, and "a bound package with no applied
    version renders nothing at all". The binding answers WHICH package a
    reader would see; whether there is yet anything to see is a separate question.

    Three answers are therefore wrong here, and each was reachable: an error page (the
    route's own 404, which the tab draws as a failure the reader could clear), the
    DEFAULT TEMPLATE (the fallback the no-package path takes, which would show a
    stranger's page under this crewmate's name), and the last page that happened to
    load. The right answer is the frame's own empty state.

    The caller that NAMES a slug still gets told -- it asked a question and a 404 is the
    answer. Only the binding-sourced case renders empty, because nobody asked.
    """

    @pytest.fixture()
    def unbacked(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        store = _VanishingStore(dp.dump_package(_package()))
        monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
        return SimpleNamespace(app=wired.app, store=store)

    @pytest.mark.asyncio
    async def test_the_binding_really_resolves_before_the_record_goes(self, unbacked: Any) -> None:
        """The control. Without it an empty answer could mean the LOOKUP failed, which
        has its own pinned behaviour (fail closed to the template page) and would make
        every assertion below pass over the case this class is about."""
        assert md._bound_package_slug(CREW) == PACKAGE_SLUG
        assert len(unbacked.store.gets) == 1

    @pytest.mark.asyncio
    async def test_it_renders_nothing_rather_than_an_error_page(self, unbacked: Any) -> None:
        status, body = await _call(
            md.api_member_dashboard, _request(unbacked.app, "GET", NO_SLUG_PATH)
        )
        assert status == 200, body
        assert body["state"] == "empty"
        assert not body.get("rendered_html"), "something was composed for a package with no version"
        assert not body.get("html")
        assert body["instance_version"] == 0

    @pytest.mark.asyncio
    async def test_it_does_not_fall_back_to_the_default_template(self, unbacked: Any) -> None:
        """The failure mode worth naming: the no-package path serves the shipped default
        so that every crewmate has a page. Reaching it HERE would draw a page that is
        not this crewmate's under this crewmate's name, and `instance_version` 0 would
        make it look like an ordinary fresh crewmate rather than a binding with nothing
        behind it."""
        _, body = await _call(md.api_member_dashboard, _request(unbacked.app, "GET", NO_SLUG_PATH))
        assert not body.get("template", {}).get("id"), body.get("template")
        assert "no applied version" in body.get("state_reason", "")

    @pytest.mark.asyncio
    async def test_it_arms_no_live_page(self, unbacked: Any) -> None:
        """Nothing is drawn, so nothing subscribes. An armed page here would push fields
        into a frame holding no document."""
        await _call(md.api_member_dashboard, _request(unbacked.app, "GET", NO_SLUG_PATH))
        assert not unbacked.app.get("member_dashboard_live_pages")

    @pytest.mark.asyncio
    async def test_a_caller_that_named_the_slug_is_still_told(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It asked for one package by name, so the honest answer is that there is no
        such package -- not an empty page it did not ask for.

        The split the route already uses for a slug it refuses: the caller that NAMED
        one gets a code, and a slug that came from the crewmate's own binding does not,
        because nobody asked.
        """
        store = _VanishingStore(dp.dump_package(_package()))
        store.gets.append("spent")  # the route's read is never this store's first
        monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
        status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
        assert status == 404, body
        assert body["code"] == "dashboard_not_found"


class TestAnApprovalDoesNotAuthorizeAReplacement:
    """THE APPROVAL AND ITS BYTES ARE TWO READS, so the record can be swapped between.

    ``_serve_package`` reads the record to find the version a person applied, then
    fetches that version's bytes by SLUG AND NUMBER in a second call. A slug is
    reusable and a version number restarts at 1, so a delete-and-recreate in between
    answers the second read with a different artifact's version N -- whatever its own
    agent wrote -- served under an approval a person gave to the record that is gone.

    ``created_at`` is minted once per record and never rewritten, so comparing it
    across the two reads is what makes them one record. The window is small; the
    comparison is one tuple.
    """

    @pytest.mark.asyncio
    async def test_a_recreated_record_renders_nothing(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = wired.store
        real_get = store.get
        reads: list[int | None] = []

        def _swapped(slug: str, *, version: int | None = None) -> Any:
            reads.append(version)
            art = real_get(slug, version=version)
            # The FIRST read is the record the approval is taken from; the versioned
            # read that follows is answered by the record that replaced it, which is a
            # different `created_at` under the same slug and the same number.
            stamp = "2026-10-12T00:00:00Z" if version is None else "2026-10-12T09:99:99Z"
            return SimpleNamespace(
                **(
                    {**art.__dict__, "created_at": stamp}
                    if hasattr(art, "__dict__")
                    else {"created_at": stamp}
                )
            )

        monkeypatch.setattr(store, "get", _swapped)
        status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
        assert status == 200, body
        assert reads == [
            None,
            APPLIED_VERSION,
        ], "the render path did not make the two reads this test is about"
        assert not body.get("rendered_html"), (
            "a page from the record that REPLACED the approved one was rendered under "
            "that approval"
        )
        assert body.get("state") == "empty"

    @pytest.mark.asyncio
    async def test_the_same_record_still_renders(self, wired: Any) -> None:
        """The limit: one record read twice is one record, so the ordinary page is
        unaffected by the comparison."""
        status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
        assert status == 200
        assert PAGE in body["rendered_html"]


class TestTheRenderPathServesAnAppliedVersion:
    """THE GATE, measured on a REAL store so the events are the ones the product writes.

    The render path must serve a page a PERSON put there. The marker already exists on
    main and needs no new field: a version is cut only by
    ``ArtifactStore.update(..., snapshot=True)``, each cut appends a lifecycle event
    carrying ``by=actor`` (``artifacts_writes.update``), and that actor is derived from HOW
    the request authenticated -- ``actor = "agent" if is_mcp else "user"`` where
    ``is_mcp`` is the presence of the ``X-Internal-Secret`` header
    (``dashboard/handlers/artifacts.py:1960``). So:

        applied version := the newest version whose event has ``by == "user"``

    Keyed on ``by`` and never on ``type``: ``event_type`` is settable from the request
    body, so an agent can write ``type="edited"``; ``by`` comes from the auth path and
    the body cannot reach it.

    Written against a real `ArtifactStore` because a fake is free to put any event on a
    record, and the claim under test is about the events the store itself writes.
    """

    @pytest.fixture()
    def live(self, wired: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "artifacts")
        art = store.create(name="page", content=dp.dump_package(_package()), kind="dashboard")
        monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
        path = f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={art.slug}"
        return SimpleNamespace(app=wired.app, store=store, slug=art.slug, path=path)

    def _agent_writes(self, live: Any, marker: str) -> None:
        """An agent iteration, exactly as the MCP path performs it: a snapshot whose
        actor is `agent`, which is what `X-Internal-Secret` being present produces."""
        page = _package()
        page["view"]["html"] = f'<p data-dashboard-field="goal">{marker}</p>'
        _owner_write(
            live.store, live.slug, content=dp.dump_package(page), snapshot=True, actor="agent"
        )

    def _person_applies(self, live: Any, marker: str) -> None:
        """A person's approval: a snapshot cut with the `user` actor, which is what a
        request with no `X-Internal-Secret` header produces."""
        page = _package()
        page["view"]["html"] = f'<p data-dashboard-field="goal">{marker}</p>'
        _owner_write(
            live.store, live.slug, content=dp.dump_package(page), snapshot=True, actor="user"
        )

    def test_the_store_really_tags_the_actor_on_the_event(self, live: Any) -> None:
        """The control for the whole class. If the store stopped recording `by`, every
        assertion below would pass by reading nothing."""
        self._agent_writes(live, "one")
        art = live.store.get(live.slug)
        cut = [e for e in art.events if e.get("version") and e.get("by")]
        assert cut, f"no version-cutting event carries an actor: {art.events}"
        assert cut[-1]["by"] == "agent"
        self._person_applies(live, "two")
        art = live.store.get(live.slug)
        assert [e for e in art.events if e.get("by") == "user" and e.get("version")]

    @pytest.mark.asyncio
    async def test_an_agent_only_package_renders_nothing(self, live: Any) -> None:
        """(a) The agent writes three times and no person has approved anything, so
        there is no applied version and the frame draws its empty state."""
        for marker in ("one", "two", "three"):
            self._agent_writes(live, marker)
        status, body = await _call(md.api_member_dashboard, _request(live.app, "GET", live.path))
        assert status == 200, body
        assert body["state"] == "empty"
        assert not body.get("rendered_html")
        assert body["instance_version"] == 0

    @pytest.mark.asyncio
    async def test_the_version_a_person_cut_is_the_one_served(self, live: Any) -> None:
        """(b) A person approves, and THAT page renders."""
        self._agent_writes(live, "agent-draft")
        self._person_applies(live, "approved")
        status, body = await _call(md.api_member_dashboard, _request(live.app, "GET", live.path))
        assert status == 200, body
        assert body["state"] == "live"
        assert "approved" in body["rendered_html"]
        assert "agent-draft" not in body["rendered_html"]

    @pytest.mark.asyncio
    async def test_a_later_agent_write_does_not_replace_it(self, live: Any) -> None:
        """(c) THE ONE THAT PINS THE GATE.

        The agent writes again AFTER the approval. Its bytes are the record's current
        content, which a route reading `get(slug)` serves; the applied version is older
        and is what must render instead. Without this, (a) and (b) both pass over a
        route that simply serves the latest write whenever any approval exists.
        """
        self._person_applies(live, "approved")
        self._agent_writes(live, "written-after")
        current = live.store.get(live.slug)
        assert "written-after" in (current.content or ""), (
            "the premise failed: the agent's write is not the record's current content, "
            "so this test is not measuring the gate"
        )
        status, body = await _call(md.api_member_dashboard, _request(live.app, "GET", live.path))
        assert status == 200, body
        assert "approved" in body["rendered_html"]
        assert (
            "written-after" not in body["rendered_html"]
        ), "the route served the agent's newer bytes over the version a person applied"

    @pytest.mark.asyncio
    async def test_the_body_names_the_applied_version_not_the_newest(self, live: Any) -> None:
        """The frame's patch counter and the body's labels have to name the page that is
        actually on screen, or the first push is checked against a version the reader
        never received."""
        self._person_applies(live, "approved")
        self._agent_writes(live, "written-after")
        applied = [
            e["version"]
            for e in live.store.get(live.slug).events
            if e.get("by") == "user" and e.get("version")
        ][-1]
        _, body = await _call(md.api_member_dashboard, _request(live.app, "GET", live.path))
        assert body["instance_version"] == applied
        assert body["instance_version"] < live.store.get(live.slug).version

    @pytest.mark.asyncio
    async def test_an_agent_cannot_spell_its_way_to_an_applied_version(self, live: Any) -> None:
        """WHY THE MARKER IS `by` AND NOT `type`.

        `event_type` is settable from the request body, so an agent can make its own
        iteration carry `type="edited"` -- the spelling a person's save would use. The
        actor comes from the auth path instead, so the event still reads `by="agent"`
        and the page still does not render.
        """
        page = _package()
        page["view"]["html"] = '<p data-dashboard-field="goal">dressed-up</p>'
        _owner_write(
            live.store,
            live.slug,
            content=dp.dump_package(page),
            snapshot=True,
            actor="agent",
            event_type="edited",
        )
        dressed = [e for e in live.store.get(live.slug).events if e.get("type") == "edited"]
        assert (
            dressed and dressed[-1]["by"] == "agent"
        ), "the premise failed: the store let a body-settable type change the actor"
        status, body = await _call(md.api_member_dashboard, _request(live.app, "GET", live.path))
        assert status == 200
        assert body["state"] == "empty"
        assert not body.get("rendered_html")


class TestAnAnnotationIsNotAnApproval:
    """READING A PACKAGE IS NOT APPROVING IT.

    The applied marker takes the newest version a person CUT. The trap is that ``by`` is
    stamped on every lifecycle event, not only on the ones that cut a version, and two
    of them are things a person does while merely looking:

    * ``referenced`` -- the chat panel posts one every time a person opens an artifact
      in a slot, and the store stamps it with ``version=meta.version``;
    * ``comment`` -- resolving or adding a comment does the same.

    Both arrive from the browser, so both carry ``by="user"`` and the version the AGENT
    last wrote. A marker keyed on the actor alone therefore reads ordinary reading as
    approval, and the agent's unapproved draft renders with script. That is not a corner
    case: opening the page is the commonest thing a person does with it.

    So the marker takes BOTH halves -- a user actor AND an event type that cut a version.
    The actor is what an agent cannot forge; the type is what separates a cut from a
    glance. Keeping only the actor was the defect; keeping only the type would let an
    agent spell its own save ``edited``, which
    ``test_an_agent_cannot_spell_its_way_to_an_applied_version`` pins.
    """

    @pytest.fixture()
    def live(self, wired: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "artifacts")
        art = store.create(name="page", content=dp.dump_package(_package()), kind="dashboard")
        # The agent's own draft, over MCP: a version cut with the agent actor.
        page = _package()
        page["view"]["html"] = '<p data-dashboard-field="goal">agent-draft</p>'
        _owner_write(store, art.slug, content=dp.dump_package(page), snapshot=True, actor="agent")
        monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
        path = f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={art.slug}"
        return SimpleNamespace(app=wired.app, store=store, slug=art.slug, path=path)

    async def _rendered(self, live: Any) -> Any:
        status, body = await _call(md.api_member_dashboard, _request(live.app, "GET", live.path))
        assert status == 200, body
        return body

    @pytest.mark.asyncio
    async def test_a_person_opening_the_page_does_not_apply_it(self, live: Any) -> None:
        """The chat panel's impression, which is what `recordArtifactReference` sends."""
        live.store.record_impression(live.slug, by="user", session_id="chat-1")
        stamped = [e for e in live.store.get(live.slug).events if e.get("type") == "referenced"]
        assert stamped and stamped[-1]["by"] == "user", (
            "the premise failed: the impression is not stamped with a user actor, so "
            "this test is not measuring the trap"
        )
        assert (
            stamped[-1]["version"] == live.store.get(live.slug).version
        ), "the premise failed: the impression does not name the current version"
        body = await self._rendered(live)
        assert body["state"] == "empty"
        assert not body.get("rendered_html")

    @pytest.mark.asyncio
    async def test_a_person_commenting_does_not_apply_it(self, live: Any) -> None:
        live.store.record_comment_event(
            live.slug, action="resolved", by="user", session_id="chat-1"
        )
        stamped = [e for e in live.store.get(live.slug).events if e.get("type") == "comment"]
        assert stamped and stamped[-1]["by"] == "user", "the premise failed"
        body = await self._rendered(live)
        assert body["state"] == "empty"
        assert not body.get("rendered_html")

    @pytest.mark.asyncio
    async def test_an_annotation_does_not_promote_a_newer_agent_draft(self, live: Any) -> None:
        """The same trap with an approval already in place, which is the damaging shape:
        a person applied page one, the agent wrote page two, and the person then merely
        OPENED the package. Page one must still be what renders."""
        page = _package()
        page["view"]["html"] = '<p data-dashboard-field="goal">approved</p>'
        _owner_write(
            live.store, live.slug, content=dp.dump_package(page), snapshot=True, actor="user"
        )
        newer = _package()
        newer["view"]["html"] = '<p data-dashboard-field="goal">written-after</p>'
        _owner_write(
            live.store, live.slug, content=dp.dump_package(newer), snapshot=True, actor="agent"
        )
        live.store.record_impression(live.slug, by="user", session_id="chat-1")
        body = await self._rendered(live)
        assert "approved" in body["rendered_html"]
        assert "written-after" not in body["rendered_html"]


class TestTheAppRefusalIsWhatKeepsTheMarkerHonest:
    """THE MARKER DEPENDS ON THE APP REFUSAL, end to end.

    ``by == "user"`` is assigned to a request that carries no ``X-Internal-Secret``, and
    an app backend is proxied with that header STRIPPED -- so an app's artifact write
    would be stamped ``by="user"`` and would apply its own page. What stops it is
    ``member_dashboard_fields.package_write_refusal``, which identifies an app
    POSITIVELY from ``request["app"]`` and refuses before the person's exemption is
    reached.

    The refusal has its own unit tests and the marker has its own; NOTHING tied the two
    together, so moving the app check below the person exemption would leave every one of
    them green while an app quietly became a person. This is that tie: an app writes a
    dashboard package and the page renders nothing, because no approval exists.
    """

    @pytest.mark.asyncio
    async def test_an_app_write_never_becomes_an_applied_version(
        self, wired: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.artifacts import ArtifactStore
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        store = ArtifactStore(root=tmp_path / "artifacts")
        art = store.create(name="page", content=dp.dump_package(_package()), kind="dashboard")
        monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)

        page = _package()
        page["view"]["html"] = '<p data-dashboard-field="goal">app-wrote-this</p>'
        content = dp.dump_package(page)

        # THE WRITE, through the gate the artifact routes ask. An app token is what
        # `request["app"]` carries, and it is the only thing this request differs by.
        app_request = make_mocked_request("PATCH", f"/api/artifacts/{art.slug}")
        app_request["app"] = "some-app"
        app_request["user"] = OWNER
        refusal = fields.package_write_refusal(app_request, content)
        assert refusal, "an app was allowed to write a dashboard package"
        assert "an app may not write a dashboard package" in refusal

        # The refusal is what keeps the store free of an app-authored version. Were the
        # check moved below the person's exemption, the write would land stamped
        # `by="user"` -- so the assertion that matters is the page, not the sentence.
        status, body = await _call(
            md.api_member_dashboard,
            _request(
                wired.app,
                "GET",
                f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={art.slug}",
            ),
        )
        assert status == 200, body
        assert body["state"] == "empty"
        assert not body.get("rendered_html")
        assert "app-wrote-this" not in json.dumps(body)

    def test_the_app_check_runs_before_the_person_exemption(self) -> None:
        """The ORDER, read off the source, because it is the mechanism and an order is
        what a later edit undoes without changing any line's meaning. An app is
        identified positively by its id STRING in `request["app"]`; the person is
        whoever is left."""
        import inspect

        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        body = inspect.getsource(fields.package_write_refusal)
        app_check = body.index("if isinstance(app_id, str) and app_id:")
        # The person's exemption is the branch that returns no refusal for a caller the
        # app check did not claim.
        assert app_check < body.index("binding_refusal("), (
            "the app refusal no longer precedes the binding check, so an app can reach "
            "the person's path and its write would be stamped by='user'"
        )


def test_a_create_cannot_name_itself_the_person(tmp_path: Path) -> None:
    """THE ONE EVENT WHOSE ACTOR THE REQUEST BODY REACHES, and why it is still closed.

    `ArtifactStore.create` stamps the `created` event's `by` from its `source`, and the
    create route honours `body["source"]` as given ("Honor an explicitly-supplied source
    (MCP tool / import path)"). That would be a way to mint an approval for one's own
    first version -- except `source` is validated against a closed label set, and no
    label is `user`.

    Pinned on the real store rather than reasoned about, and pinned as the LABEL SET
    rather than as one rejected string: the applied marker leaves `created` out because
    it can never carry a user actor, so a `user` label added to that set later must fail
    HERE, where the reason is written down, rather than silently widen the gate.
    """
    from kiro_crew.artifacts import ArtifactStore, ArtifactValidationError

    store = ArtifactStore(root=tmp_path / "artifacts")
    with pytest.raises(ArtifactValidationError) as refused:
        store.create(
            name="page", content=dp.dump_package(_package()), kind="dashboard", source="user"
        )
    assert "invalid source 'user'" in str(refused.value)
    # The whole set, so a new label is read here before it reaches the marker.
    allowed = {
        "chat",
        "cli",
        "cron",
        "dashboard",
        "import",
        "manual",
        "slack",
        "subagent",
        "task-runner",
        "unknown",
    }
    for label in allowed:
        assert label != "user"
    art = store.create(
        name="page2", content=dp.dump_package(_package()), kind="dashboard", source="dashboard"
    )
    created = [e for e in store.get(art.slug).events if e.get("type") == "created"]
    assert created and created[-1]["by"] != "user", (
        "a create stamped a person as its actor, which would mint an approval for the "
        "creator's own first version"
    )


@pytest.mark.asyncio
async def test_a_package_a_person_created_is_not_applied_by_the_create_alone(
    wired: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consequence of leaving `created` out, stated as behaviour: the dashboard's own
    create does not make a page live. A person approves a page by saving it, which is an
    `edited` cut whose actor the auth path decides."""
    from kiro_crew.artifacts import ArtifactStore

    store = ArtifactStore(root=tmp_path / "artifacts")
    page = _package()
    page["view"]["html"] = '<p data-dashboard-field="goal">created-only</p>'
    art = store.create(
        name="page", content=dp.dump_package(page), kind="dashboard", source="dashboard"
    )
    monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
    status, body = await _call(
        md.api_member_dashboard,
        _request(
            wired.app, "GET", f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={art.slug}"
        ),
    )
    assert status == 200, body
    assert body["state"] == "empty"
    assert "created-only" not in json.dumps(body)


class TestTheApprovingTypeSetIsAnAllowlist:
    """THE SET ITSELF, guarded -- because the finding that produced it was a set that
    admitted too much, and the next annotation type someone adds is the same shape of
    mistake.

    `_VERSION_CUTTING_EVENTS` is an ALLOWLIST: `applied_package_version` skips any type
    not in it, so a lifecycle type added to the store tomorrow is excluded until someone
    decides otherwise. A denylist of annotations would admit it silently, which is
    exactly how `referenced` came to count as an approval.

    These tests pin the three premises the set rests on, each against the module that
    owns it rather than against a copy here, so a change to any of them fails with the
    reason written next to it.
    """

    def test_every_approving_type_is_a_real_lifecycle_type(self) -> None:
        """A typo in the set would silently never match, and the gate would then refuse
        every page -- failing closed, but for a reason nobody could find."""
        from kiro_crew.artifact_store.records import ALLOWED_EVENT_TYPES

        assert md._VERSION_CUTTING_EVENTS <= ALLOWED_EVENT_TYPES, (
            f"the approving set names types the store does not emit: "
            f"{sorted(md._VERSION_CUTTING_EVENTS - ALLOWED_EVENT_TYPES)}"
        )

    def test_a_new_lifecycle_type_is_excluded_until_someone_decides(self) -> None:
        """The allowlist's whole point, stated as the thing a reader checks: every type
        the store can emit is either approving ON PURPOSE or excluded, and this test
        lists the exclusions with the reason each one is out.

        A type added to the store's vocabulary appears in neither list and fails here,
        which is the prompt to decide rather than to inherit.
        """
        from kiro_crew.artifact_store.records import ALLOWED_EVENT_TYPES

        excluded_on_purpose = {
            # An agent's own version cut. Excluded by the actor too, named here so the
            # set reads as a decision rather than an omission.
            "iterated",
            # Annotations. Both are stamped with the record's CURRENT version, so
            # counting either makes reading a page approve whatever it happens to show.
            "referenced",
            "comment",
            # Cannot carry `by == "user"` at all -- see the test below.
            "created",
        }
        decided = md._VERSION_CUTTING_EVENTS | excluded_on_purpose
        assert decided == ALLOWED_EVENT_TYPES, (
            "a lifecycle type is neither approving nor excluded on purpose: "
            f"{sorted(ALLOWED_EVENT_TYPES - decided)}. Decide it here, and if it is an "
            "annotation leave it OUT -- the set is an allowlist for that reason"
        )

    def test_a_created_event_can_never_carry_the_person_as_its_actor(self) -> None:
        """WHY `created` IS OUT, and it is not the reason one might assume.

        `create` stamps the event's actor from its `source` (`by=source if source !=
        "chat" else "agent"`), and `source` is validated against `ALLOWED_SOURCES` --
        which has no `user` member. So a `created` event's actor is one of those labels
        or `agent`, and never the person. Putting `created` in the approving set would
        add a rule that matches nothing while telling the next reader that creating a
        page approves it.

        Pinned on the real constant: if `user` ever joins `ALLOWED_SOURCES`, this fails
        and the decision gets made again with the new fact in hand.
        """
        from kiro_crew.artifact_store.rules import ALLOWED_SOURCES

        assert "user" not in ALLOWED_SOURCES, (
            "a create can now name the person as its source, so a `created` event can "
            "carry by=='user' -- re-decide whether it belongs in the approving set, and "
            "check whether a person's create can carry bytes an agent supplied"
        )
        assert "created" not in md._VERSION_CUTTING_EVENTS

    def test_the_marker_needs_both_fields_not_either(self) -> None:
        """The conjunction, asserted directly on the function rather than through a
        route: one field short in either direction answers zero.

        An agent can set the TYPE (``event_type`` is body-settable) but not the actor; an
        annotation carries the ACTOR but not an approving type. Each field closes the
        hole the other cannot reach, which is why both are required.
        """
        approving = sorted(md._VERSION_CUTTING_EVENTS)[0]
        # Right type, wrong actor: the agent's own cut, however it is spelled.
        assert (
            md.applied_package_version(
                SimpleNamespace(events=[{"type": approving, "by": "agent", "version": 7}])
            )
            == 0
        )
        # Right actor, wrong type: a person reading the page.
        assert (
            md.applied_package_version(
                SimpleNamespace(events=[{"type": "referenced", "by": "user", "version": 7}])
            )
            == 0
        )
        # Both: an approval.
        assert (
            md.applied_package_version(
                SimpleNamespace(events=[{"type": approving, "by": "user", "version": 7}])
            )
            == 7
        )
        # The NEWEST approval wins, and a later annotation does not move it.
        assert (
            md.applied_package_version(
                SimpleNamespace(
                    events=[
                        {"type": approving, "by": "user", "version": 3},
                        {"type": approving, "by": "user", "version": 5},
                        {"type": "referenced", "by": "user", "version": 9},
                    ]
                )
            )
            == 5
        )


class TestTheGenericSnapshotButtonIsNotAnApproval:
    """A PERSON TIDYING VERSION HISTORY DOES NOT PUBLISH THE AGENT'S PAGE.

    The artifact detail page and the markdown panel both offer a generic Snapshot
    control that sends `updateArtifact(slug, { snapshot: true })` with NO content. On
    the generic path that versions the record's current bytes and stamps the event
    `by="user"`, `type="edited"` -- which is exactly the shape this marker approves on.
    A person pressing it to keep a restore point would then be publishing whatever the
    agent wrote last, with nothing on screen saying so.

    It does not happen, and the reason is a guard in the store rather than anything this
    handler does: for `kind="dashboard"` a content-less update sets `snapshot = False`
    ("No content, so no layout change, so no version -- this also disarms the explicit
    Snapshot path below, which would otherwise re-version the live package unchanged").

    So this class pins the PREMISE the marker leans on. If that guard is ever removed,
    the marker silently starts treating a history button as an approval, and these tests
    are what fail instead.
    """

    @pytest.fixture()
    def drafted(self, wired: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "artifacts")
        art = store.create(
            name="page", content=dp.dump_package(_package()), kind="dashboard", source="dashboard"
        )
        draft = _package()
        draft["view"]["html"] = '<p data-dashboard-field="goal">agent-draft</p>'
        _owner_write(store, art.slug, content=dp.dump_package(draft), snapshot=True, actor="agent")
        monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
        path = f"/api/members/{SLUG}/dashboard?member={CREW}&dashboard={art.slug}"
        return SimpleNamespace(app=wired.app, store=store, slug=art.slug, path=path)

    def test_a_content_less_snapshot_cuts_no_version(self, drafted: Any) -> None:
        """The store-level fact the rest of this class rests on, asserted first so a
        failure here names the guard rather than the page."""
        before = drafted.store.get(drafted.slug).version
        versions_before = drafted.store.list_versions(drafted.slug)
        drafted.store.update(drafted.slug, snapshot=True, actor="user")
        after = drafted.store.get(drafted.slug)
        assert after.version == before, (
            "a content-less snapshot now cuts a version for a dashboard package, so the "
            "generic Snapshot button can mint an approval -- the marker needs an event "
            "only an apply writes"
        )
        assert drafted.store.list_versions(drafted.slug) == versions_before
        assert not [
            e for e in after.events if e.get("by") == "user" and e.get("version")
        ], f"a user-actor version event appeared: {after.events}"

    @pytest.mark.asyncio
    async def test_the_page_still_renders_nothing_after_it(self, drafted: Any) -> None:
        """The behaviour a reader cares about, through the real route."""
        drafted.store.update(drafted.slug, snapshot=True, actor="user")
        status, body = await _call(
            md.api_member_dashboard, _request(drafted.app, "GET", drafted.path)
        )
        assert status == 200, body
        assert body["state"] == "empty"
        assert not body.get("rendered_html")
        assert "agent-draft" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_a_person_writing_the_page_themselves_does_apply_it(self, drafted: Any) -> None:
        """The limit of the rule, so it does not read as "a person can never apply". A
        person who SUBMITS bytes has approved those bytes, and that is the act the
        marker is looking for."""
        theirs = _package()
        theirs["view"]["html"] = '<p data-dashboard-field="goal">person-approved</p>'
        _owner_write(
            drafted.store,
            drafted.slug,
            content=dp.dump_package(theirs),
            snapshot=True,
            actor="user",
        )
        status, body = await _call(
            md.api_member_dashboard, _request(drafted.app, "GET", drafted.path)
        )
        assert status == 200, body
        assert "person-approved" in body["rendered_html"]
        assert "agent-draft" not in body["rendered_html"]


class TestTheScriptGrantIsAudited:
    """THE GRANT IS AN AUTHORIZATION DECISION, so it leaves a record.

    It decides whether a page a crewmate wrote may run script against that crewmate's
    own fold values, and the repository's backend security controls require a decision
    of that kind to be auditable. The row goes through the shared governance helper
    (``sel().log_governance_decision``), the one every other permission check in the
    product uses, rather than a second emitter with its own vocabulary.

    DEDUPED ON ``(crewmate, package, version, decision)`` because the tab POLLS: a
    fallback refetch every few minutes plus two WS frames re-decide this with the same
    answer for as long as a dashboard is open, and a row per read would turn an
    authorization log into a page-view log and push the rows that matter out of any
    practical retention window. A change in any part of that tuple is exactly when
    there is something new to record.
    """

    @pytest.fixture
    def audited(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """Capture the governance rows the route writes, and forget past decisions."""
        rows: list[dict[str, Any]] = []

        class _Sel:
            def log_governance_decision(self, **kw: Any) -> None:
                rows.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        monkeypatch.setattr(md, "_grant_audited", {})
        return rows

    @pytest.mark.asyncio
    async def test_the_first_open_writes_one_row(self, wired: Any, audited: Any) -> None:
        status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
        assert status == 200, body
        assert len(audited) == 1, audited
        row = audited[0]
        # The page the decision was made FOR, which is the half only the route knows:
        # a row naming the crewmate alone cannot be traced to the bytes that ran.
        assert row["item"] == f"{CREW}/{PACKAGE_SLUG}@{APPLIED_VERSION}"
        assert row["scope"] == "network.egress"
        # The vocabulary the shared helper pins, so a log query for allowed decisions
        # does not miss these rows alone.
        assert row["outcome"] in ("allowed", "denied")
        assert row["outcome"] == ("allowed" if body["scripts"] else "denied")
        # The key the profile was actually resolved under, not the bare slot.
        assert row["session_key"] == md.governance_session_key(SLUG)
        assert row["reason"], "the row says only the verdict, so it cannot be acted on"

    @pytest.mark.asyncio
    async def test_an_identical_second_read_writes_nothing(self, wired: Any, audited: Any) -> None:
        """The polling case. Nothing about the decision changed, so there is nothing to
        record -- and recording it anyway is what fills the log."""
        await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
        assert len(audited) == 1
        for _ in range(4):
            status, _body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
            assert status == 200
        assert len(audited) == 1, f"a read with the same decision wrote a row: {audited}"

    @pytest.mark.asyncio
    async def test_a_profile_that_flips_the_decision_writes_another(
        self, wired: Any, audited: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The change worth recording. An operator edits a profile and the same page
        stops running script: that is a new decision about the same bytes, and a dedupe
        keyed on the page alone would hide it."""
        from kiro_crew.platform import governance as gov
        from kiro_crew.platform import governance_profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "resolve_active_scope", lambda *a, **k: None)
        _status, first = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
        assert len(audited) == 1

        restricting = gov.Profile(
            name="dashboard",
            controls={
                "network.egress": gov.ScopedRuleset(
                    mode=gov.MODE_ALLOW, allow=("api.github.com",), deny=(), matcher="host"
                )
            },
        )
        monkeypatch.setattr(profiles_mod, "resolve_active_scope", lambda *a, **k: restricting)
        _status, second = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))

        assert first["scripts"] is True and second["scripts"] is False, (first, second)
        assert len(audited) == 2, audited
        assert [r["outcome"] for r in audited] == ["allowed", "denied"]
        # The profile is named as the layer that narrowed it, so the remedy is readable
        # off the row: this is a profile edit, not a ceiling or an unreadable read.
        assert audited[1]["layer"] == "profile"

    @pytest.mark.asyncio
    async def test_an_audit_that_cannot_be_written_still_serves_the_grant(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BEST EFFORT, in the direction that matters: the grant is decided by policy
        and profile, and it cannot change because a log write failed. The route must
        answer the same `scripts` it would have answered with a healthy log, and must
        not turn the failure into an error for the reader."""
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(md, "_grant_audited", {})
        healthy_status, healthy = await _call(
            md.api_member_dashboard, _request(wired.app, "GET", PATH)
        )

        def _broken() -> Any:
            raise RuntimeError("security_events.jsonl is not writable")

        monkeypatch.setattr(sel_mod, "sel", _broken)
        monkeypatch.setattr(md, "_grant_audited", {})
        status, body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))

        assert status == healthy_status == 200, body
        assert body["scripts"] == healthy["scripts"]
        # The page itself is served, not an error body. Compared by the markup the
        # package declares rather than byte-for-byte: a composed document carries a
        # per-mint generation, so equal bytes is not a property any two reads have.
        assert PAGE in body["rendered_html"]
        assert body["state"] == healthy["state"]

    @pytest.mark.asyncio
    async def test_a_failed_write_is_not_retried_on_every_poll(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken log must not become a retry storm. The decision is remembered
        BEFORE the write is attempted, so a log that cannot be written costs one lost
        row rather than one failed write per poll for as long as the tab is open."""
        import kiro_crew.sel as sel_mod

        attempts: list[int] = []

        def _broken() -> Any:
            attempts.append(1)
            raise RuntimeError("security_events.jsonl is not writable")

        monkeypatch.setattr(sel_mod, "sel", _broken)
        monkeypatch.setattr(md, "_grant_audited", {})
        for _ in range(5):
            status, _body = await _call(md.api_member_dashboard, _request(wired.app, "GET", PATH))
            assert status == 200
        assert len(attempts) == 1, f"the failed write was retried on every read: {attempts}"
