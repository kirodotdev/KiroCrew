"""``window.kirocrew.subscribe``: the frame contract's third way a value reaches a page.

The two that already exist are a text binding and a frozen snapshot, and neither lets a
page HEAR a change: a binding is filled by the host, and a snapshot is a value read once.
A page that draws a tree, a chart or a scene has to redraw when one field moves, and the
only signal it had was the generic :data:`~kiro_crew.dashboard_frame.PAGE_EVENT` -- which
says "something landed" and leaves the page to diff the whole read itself.

THE BOOTSTRAP IS EXECUTED HERE, not pattern-matched. Every other test of this module
asserts on the JS as a string, which is the right check for a containment rule (``is
textContent used, never innerHTML``) and the wrong one for a behaviour: "fires on first
load and on every push" is a claim about what runs, and a string holding the word
``subscribe`` is satisfied by code that never calls back. So the composed document's
scripts are run under a small DOM shim in Node, and
:func:`test_the_harness_can_see_a_subscriber_that_never_fires` breaks the bootstrap on
purpose and requires the probe to report the silence -- a harness that reports an absence
has to prove first that it can see one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import dashboard_frame

#: The DOM the bootstrap actually touches, and nothing else.
#:
#: Written out rather than taken from a headless browser because the surface under test
#: is four calls wide -- one element lookup, one query, one event registration and one
#: dispatch -- and a real engine would make the failure of this test a question about the
#: engine. The shim is deliberately dumb: it records, it does not interpret.
_SHIM = r"""
const listeners = {};
const dispatched = [];
globalThis.addEventListener = (type, fn) => {
  (listeners[type] = listeners[type] || []).push(fn);
};
globalThis.parent = { postMessage() {} };
globalThis.Event = class { constructor(type) { this.type = type; } };
const nodes = [];
globalThis.document = {
  readyState: 'complete',
  documentElement: { setAttribute() {}, lang: '' },
  getElementById: (id) => (id === 'kirocrew-dashboard-data' ? { textContent: ISLAND } : null),
  querySelectorAll: () => nodes,
};
globalThis.window = globalThis;
globalThis.dispatchEvent = (event) => {
  dispatched.push(event.type);
  for (const fn of listeners[event.type] || []) fn(event);
  return true;
};
globalThis.__push = (read) => {
  for (const fn of listeners.message || []) {
    fn({ source: globalThis.parent, data: { type: DATA_TYPE, read } });
  }
};
globalThis.__calls = [];
"""


def _island(read: dict[str, Any]) -> str:
    """The data island's text, as the composed document carries it."""
    return json.dumps(read, ensure_ascii=False)


def _run(bootstrap: str, read: dict[str, Any], page_js: str) -> list[Any]:
    """Run *bootstrap* then *page_js* over *read*; return what the page recorded.

    *page_js* is the page's own inline script -- the half a template author writes --
    and it pushes onto ``__calls``, so the assertion is about what the PAGE observed
    rather than about the bootstrap's internals.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI builds the website, so node is present
        pytest.skip("node is needed to execute the frame bootstrap")
    program = "\n".join(
        (
            f"const ISLAND = {json.dumps(_island(read))};",
            f"const DATA_TYPE = {json.dumps(dashboard_frame.DATA_MESSAGE_TYPE)};",
            _SHIM,
            bootstrap,
            page_js,
            "console.log(JSON.stringify(globalThis.__calls));",
        )
    )
    done = subprocess.run(
        [node, "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return list(json.loads(done.stdout.strip().splitlines()[-1]))


def _read(**over: Any) -> dict[str, Any]:
    fields = over.pop("fields", {"boards": 2, "owner": "atlas"})
    version = over.pop("version", 0)
    generation = over.pop("generation", "")
    payload = dashboard_frame.read_payload(fields, **over)
    # The host stamps every read it sends, so a harness that omitted it would test a
    # document the product never composes. The generation is stamped beside the version
    # for the same reason: it is what says whose counter that number came from.
    payload["version"] = version
    if generation:
        payload["generation"] = generation
    return payload


class TestSubscribeOnFirstLoad:
    def test_a_subscriber_is_called_with_the_value_the_island_carries(self) -> None:
        """FIRST LOAD, before any push.

        A page's inline script runs during parse, which is before the host has filled a
        single binding -- so a subscriber that only fired on a push would draw nothing on
        first paint and the reader would see an empty page over a log that has the
        numbers in it. Firing at subscribe time is what makes the callback the page's one
        entry point instead of a thing it needs a binding beside.
        """
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));",
        )
        assert calls == [2]

    def test_a_field_the_read_does_not_carry_fires_nothing(self) -> None:
        """An unresolved field is ABSENT, and absent is not a value.

        Calling back with ``undefined`` would hand the page a number to draw for a fold
        whose path did not resolve, and a page drawing it would show a blank where the
        host's own band says the data is older -- two different statements about one
        cell. The subscriber stays armed and fires when a push carries the field.
        """
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"owner": "atlas"}, missing=["boards"]),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));",
        )
        assert calls == []

    def test_subscribe_survives_the_refill_that_replaces_the_global(self) -> None:
        """The global is replaced wholesale on every push, so ``subscribe`` has to
        travel with it -- a page holding ``window.kirocrew.subscribe`` from first load
        and calling it after a push must not meet ``undefined``."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "__push({ fields: { boards: 9 }, agentic: [], seq: 2, stale: false, missing: [] });"
            "__calls.push(typeof window.kirocrew.subscribe);",
        )
        assert calls == ["function"]


class TestSubscribeOnEveryPush:
    def test_a_push_carrying_the_field_calls_back_with_the_new_value(self) -> None:
        """THE POINT OF THE WHOLE CONTRACT. The host pushes what moved; the page is told
        which field moved and to what, rather than being handed a new snapshot and left
        to diff it."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "__push({ fields: { boards: 3 }, agentic: [], seq: 2, stale: false, missing: [] });"
            "__push({ fields: { boards: 4 }, agentic: [], seq: 3, stale: false, missing: [] });",
        )
        assert calls == [2, 3, 4]

    def test_a_push_that_does_not_carry_the_field_is_silent_for_it(self) -> None:
        """A subscriber is per FIELD. Firing every subscriber on every push would make
        the callback another spelling of the generic page event, and a page redrawing a
        3D scene on a field it does not read is the cost a reader pays for that."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "__push({ fields: { owner: 'nyx' }, agentic: [], seq: 2, stale: false, missing: [] });",
        )
        assert calls == [2]

    def test_one_subscribers_throw_does_not_cost_the_others(self) -> None:
        """Two blocks on one page are two authors' work on a good day and one author's
        on a bad one. A throw in the first must not leave the second holding the value
        from before the push, because that page then shows two fields of different ages
        with nothing saying so."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', () => { throw new Error('x'); });"
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "__push({ fields: { boards: 7 }, agentic: [], seq: 2, stale: false, missing: [] });",
        )
        assert calls == [2, 7]

    def test_unsubscribing_stops_the_callbacks(self) -> None:
        """``subscribe`` returns its own disposer, so a block that is torn down takes
        its callback with it rather than drawing into a node that is gone."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "const off = window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "off();"
            "__push({ fields: { boards: 5 }, agentic: [], seq: 2, stale: false, missing: [] });",
        )
        assert calls == [2]

    def test_a_push_from_another_frame_calls_nothing_back(self) -> None:
        """The sender check the refill already applies, asserted through the new path.

        Every frame the dashboard embeds runs with ``allow-scripts``, so a sibling can
        reach ``parent.frames[i]`` and post here. A subscriber is a page's most trusted
        input -- it is what the page draws from -- so a value arriving from anything but
        the window that minted the page must not reach one.
        """
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "for (const fn of listeners.message || []) {"
            "  fn({ source: { other: true },"
            f"       data: {{ type: {json.dumps(dashboard_frame.DATA_MESSAGE_TYPE)},"
            "         read: { fields: { boards: 66 }, agentic: [], seq: 9,"
            "                 stale: false, missing: [] } } });"
            "}",
        )
        assert calls == [2]


class TestTheBindingsStayTextOnly:
    def test_a_subscribed_value_is_never_written_as_markup(self) -> None:
        """The one containment rule of this module, re-asserted for the field that now
        also reaches a callback: a value can be drawn by the page's own script, and the
        HOST still only ever writes it as a text node.

        ``subscribe`` hands the page a string and what the page does with it is the
        page's business. What must not change is the host's half -- so the bootstrap
        carries no ``innerHTML``, no ``insertAdjacentHTML`` and no ``outerHTML``, in the
        fill path or in the delivery path.
        """
        js = dashboard_frame._BOOTSTRAP_JS
        assert "textContent = text(" in js, "the host no longer fills bindings as text"
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in js, f"the bootstrap reaches {sink}, so a value can become markup"

    def test_a_value_holding_markup_reaches_a_subscriber_as_that_string(self) -> None:
        """A fold value holds whatever the log recorded, including an issue title with a
        tag in it. The callback gets the STRING -- not a parsed node, and not an escaped
        rendering the page would have to undo."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": "<img src=x onerror=alert(1)>"}),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));",
        )
        assert calls == ["<img src=x onerror=alert(1)>"]


def test_the_harness_can_see_a_subscriber_that_never_fires() -> None:
    """THE POSITIVE CONTROL for every ``assert calls == []`` above.

    The bootstrap is mutated so delivery is dropped, and this test requires the probe to
    come back empty. Without it, a shim that silently failed to register the page script
    at all would make every "fires" test pass vacuously and every "is silent" test pass
    for the wrong reason -- and the suite would report a contract nothing implements.
    """
    broken = dashboard_frame._BOOTSTRAP_JS.replace("entry.cb(", "(function () {})(")
    assert broken != dashboard_frame._BOOTSTRAP_JS, (
        "the mutation matched nothing, so this control proves nothing: the delivery call "
        "has been renamed and this test has to follow it"
    )
    calls = _run(
        broken,
        _read(),
        "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
        "__push({ fields: { boards: 3 }, agentic: [], seq: 2, stale: false, missing: [] });",
    )
    assert calls == [], "the harness reports callbacks a bootstrap that cannot deliver made"


def test_the_contract_is_documented_where_a_page_author_reads_it() -> None:
    """A page author reads the module docstring, not the bootstrap. A third way for a
    value to reach a page that is only discoverable by reading minified-looking JS inside
    an f-string is a contract nobody composes against."""
    doc = Path(dashboard_frame.__file__).read_text(encoding="utf-8")
    head = doc[: doc.index('"""', doc.index('"""') + 3)]
    assert "subscribe" in head, (
        "the module docstring lists the ways a value reaches a page and does not mention "
        "subscribe, so the only description of it is the code"
    )


def _posted(bootstrap: str, read: dict[str, Any], page_js: str) -> list[Any]:
    """What the page posted to its parent. The refetch request is a message, not a call."""
    return _run(
        bootstrap,
        read,
        "globalThis.parent.postMessage = (m) => __calls.push(m);\n" + page_js,
    )


class TestAPatchCarriesOnlyWhatMoved:
    """The controller pushes the fields that changed and nothing else, which makes the
    frame responsible for the half a partial push cannot carry.

    Without a merge here, a push of one field would leave ``window.kirocrew.fields``
    holding that field alone -- so a chart reading a second field would find
    ``undefined`` and the host's own fill would mark every other cell missing. A page
    would then go blank one field at a time, each push, with no state saying why.
    """

    def test_a_patch_keeps_the_fields_it_does_not_carry(self) -> None:
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2, "owner": "atlas"}),
            "__push({ patch: true, version: 1, fields: { boards: 5 }, agentic: [],"
            "         seq: 2, stale: false, missing: [] });"
            "__calls.push(window.kirocrew.fields.owner, window.kirocrew.fields.boards);",
        )
        assert calls == ["atlas", 5]

    def test_a_patch_fires_only_the_subscribers_for_the_fields_it_carries(self) -> None:
        """A page redrawing a 3D scene on a field it does not read is the cost a reader
        pays for a callback that fires on every push."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2, "owner": "atlas"}),
            "window.kirocrew.subscribe('owner', (v) => __calls.push(['owner', v]));"
            "window.kirocrew.subscribe('boards', (v) => __calls.push(['boards', v]));"
            "__push({ patch: true, version: 1, fields: { boards: 5 }, agentic: [],"
            "         seq: 2, stale: false, missing: [] });",
        )
        assert calls == [["owner", "atlas"], ["boards", 2], ["boards", 5]]

    def test_a_full_read_replaces_rather_than_merges(self) -> None:
        """A full read is the answer to a refetch, so it IS the whole truth: merging it
        would keep a field the host has since dropped from the model and leave the page
        drawing a value nothing will ever push again."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2, "owner": "atlas"}),
            "__push({ version: 7, fields: { boards: 5 }, agentic: [], seq: 2,"
            "         stale: false, missing: [] });"
            "__calls.push(window.kirocrew.fields.owner === undefined,"
            "             window.kirocrew.fields.boards);",
        )
        assert calls == [True, 5]


class TestAVersionGapRefetches:
    def test_a_patch_that_skips_a_version_asks_for_a_full_read(self) -> None:
        """A page that applied a patch it cannot place in order is a page showing one
        field from after a change and the rest from before it, with nothing saying so.
        The gap is the only thing that detects a dropped frame, so it asks for the whole
        read rather than guessing at the hole."""
        posted = _posted(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}),
            "__push({ patch: true, version: 1, fields: { boards: 3 }, agentic: [],"
            "         seq: 2, stale: false, missing: [] });"
            "__push({ patch: true, version: 4, fields: { boards: 9 }, agentic: [],"
            "         seq: 5, stale: false, missing: [] });",
        )
        assert [m["type"] for m in posted] == [dashboard_frame.REFETCH_MESSAGE_TYPE]
        assert posted[0]["held"] == 1, "the host is not told which version the page holds"

    def test_the_skipped_patch_is_not_applied(self) -> None:
        """Asking for a refetch and applying the patch anyway would be the same wrong
        page, reached one round trip later."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}),
            "__push({ patch: true, version: 1, fields: { boards: 3 }, agentic: [],"
            "         seq: 2, stale: false, missing: [] });"
            "__push({ patch: true, version: 4, fields: { boards: 9 }, agentic: [],"
            "         seq: 5, stale: false, missing: [] });"
            "__calls.push(window.kirocrew.fields.boards);",
        )
        assert calls == [3]

    def test_consecutive_patches_apply_without_asking(self) -> None:
        posted = _posted(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}),
            "for (const v of [1, 2, 3]) {"
            "  __push({ patch: true, version: v, fields: { boards: v }, agentic: [],"
            "           seq: v, stale: false, missing: [] });"
            "}",
        )
        assert posted == []

    def test_a_patch_after_a_gateway_restart_asks_for_a_whole_read(self) -> None:
        """A restart restarts the host's counter, so the first patch after one is not
        the page's next version. There is no exemption for that: a patch computed
        against a read this page never held must not be applied, and the refetch costs
        one round trip to replace the whole read instead."""
        posted = _posted(
            dashboard_frame._BOOTSTRAP_JS,
            # The page was composed when the host's counter stood at 9.
            _read(fields={"boards": 2}, version=9),
            # The gateway restarted, so its counter is back near one.
            "__push({ patch: true, version: 1, fields: { boards: 3 }, agentic: [],"
            "         seq: 2, stale: false, missing: [] });",
        )
        assert [m["type"] for m in posted] == [dashboard_frame.REFETCH_MESSAGE_TYPE]
        assert posted[0]["held"] == 9

    def test_the_page_holds_the_version_the_island_carried(self) -> None:
        """So the first patch is checked against the read the document was composed
        from, rather than against nothing."""
        posted = _posted(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}, version=9),
            "__push({ patch: true, version: 10, fields: { boards: 3 }, agentic: [],"
            "         seq: 2, stale: false, missing: [] });",
        )
        assert posted == []

    def test_a_full_read_resets_the_version_the_page_holds(self) -> None:
        """So the patch after a refetch is checked against the read that answered it."""
        posted = _posted(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}),
            "__push({ version: 9, fields: { boards: 3 }, agentic: [], seq: 2,"
            "         stale: false, missing: [] });"
            "__push({ patch: true, version: 10, fields: { boards: 4 }, agentic: [],"
            "         seq: 3, stale: false, missing: [] });",
        )
        assert posted == []


class TestAnOlderFullReadCannotReplaceANewerOne:
    """Two gaps can be in flight at once, and their answers can arrive in either order.

    A full read REPLACES, so the older answer puts back every value the newer one
    already moved past -- and it reports `stale: false` while doing it, so the page
    presents a number from the past as the current one with nothing saying otherwise.
    The version alone cannot decide it: an armed page's counter starts again in the
    next one, so a lower number from a re-armed page is NEWER. The generation says
    which counter a number belongs to.
    """

    def test_a_reply_that_arrived_late_is_dropped(self) -> None:
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}, generation="gen-a"),
            # Two refetches answered out of order: version 7 lands, then version 5.
            "__push({ version: 7, generation: 'gen-a', fields: { boards: 7 },"
            "         agentic: [], seq: 7, stale: false, missing: [] });"
            "__push({ version: 5, generation: 'gen-a', fields: { boards: 5 },"
            "         agentic: [], seq: 5, stale: false, missing: [] });"
            "__calls.push(window.kirocrew.fields.boards);",
        )
        assert calls == [7], "the older full read replaced the newer one's values"

    def test_a_subscriber_is_not_called_for_the_dropped_reply(self) -> None:
        """A page that DRAWS would otherwise redraw itself from the stale answer, which
        is the same wrong picture whether or not the frozen read moved."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}, generation="gen-a"),
            "window.kirocrew.subscribe('boards', function (v) { __calls.push(v); });"
            "__push({ version: 7, generation: 'gen-a', fields: { boards: 7 },"
            "         agentic: [], seq: 7, stale: false, missing: [] });"
            "__push({ version: 5, generation: 'gen-a', fields: { boards: 5 },"
            "         agentic: [], seq: 5, stale: false, missing: [] });",
        )
        assert calls == [2, 7]

    def test_a_lower_version_from_a_RE_ARMED_page_is_still_applied(self) -> None:
        """The fence is why this case needs saying: the host replaced the live page, so
        its first read carries version 1 against a tab holding 7. Fencing on the number
        alone would leave that tab unable to accept its own recovery, forever."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}, generation="gen-a"),
            "__push({ version: 7, generation: 'gen-a', fields: { boards: 7 },"
            "         agentic: [], seq: 7, stale: false, missing: [] });"
            "__push({ version: 1, generation: 'gen-b', fields: { boards: 1 },"
            "         agentic: [], seq: 1, stale: false, missing: [] });"
            "__calls.push(window.kirocrew.fields.boards);",
        )
        assert calls == [1], "a re-armed page's own read was refused as stale"

    def test_the_patch_after_a_dropped_reply_is_checked_against_what_the_page_KEPT(
        self,
    ) -> None:
        """The dropped answer must not move `held` either: the page holds 7, so the
        patch that belongs to it applies and no refetch is asked for."""
        posted = _posted(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}, generation="gen-a"),
            "__push({ version: 7, generation: 'gen-a', fields: { boards: 7 },"
            "         agentic: [], seq: 7, stale: false, missing: [] });"
            "__push({ version: 5, generation: 'gen-a', fields: { boards: 5 },"
            "         agentic: [], seq: 5, stale: false, missing: [] });"
            "__push({ patch: true, version: 8, generation: 'gen-a',"
            "         fields: { boards: 8 }, agentic: [], seq: 8, stale: false,"
            "         missing: [] });",
        )
        assert posted == [], "the page asked for a read it had already been given"

    def test_a_patch_from_a_REPLACEMENT_page_asks_for_a_whole_read(self) -> None:
        """The host can replace the live page under an open tab: a second tab's load
        disarms the old page and arms a new one, and the new one's counter starts again.
        So its patch 2 looks like the next one to a tab holding 1 -- and merging it
        would mix fields computed from two different reads, which is exactly what the
        gap check exists to refuse."""
        posted = _posted(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}, version=1, generation="gen-a"),
            "__push({ patch: true, version: 2, generation: 'gen-b',"
            "         fields: { boards: 9 }, agentic: [], seq: 2, stale: false,"
            "         missing: [] });",
        )
        assert [m["type"] for m in posted] == [dashboard_frame.REFETCH_MESSAGE_TYPE]
        assert posted[0]["held"] == 1

    def test_the_replacement_patch_is_not_applied(self) -> None:
        """Asking for the read and merging anyway would be the same mixed page, reached
        one round trip later."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}, version=1, generation="gen-a"),
            "__push({ patch: true, version: 2, generation: 'gen-b',"
            "         fields: { boards: 9 }, agentic: [], seq: 2, stale: false,"
            "         missing: [] });"
            "__calls.push(window.kirocrew.fields.boards);",
        )
        assert calls == [2]

    def test_a_patch_from_the_SAME_generation_still_applies(self) -> None:
        """The condition is about whose counter the number came from, and nothing else:
        the ordinary push must not start asking for a read it does not need."""
        posted = _posted(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}, version=1, generation="gen-a"),
            "__push({ patch: true, version: 2, generation: 'gen-a',"
            "         fields: { boards: 9 }, agentic: [], seq: 2, stale: false,"
            "         missing: [] });",
        )
        assert posted == []

    def test_a_patch_is_not_fenced_by_the_version_it_carries(self) -> None:
        """An out-of-order PATCH is the gap check's business: it asks for a whole read
        rather than being dropped, because a patch nobody answers leaves the page
        showing one field from after a change and the rest from before it."""
        posted = _posted(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": 2}, version=7, generation="gen-a"),
            "__push({ patch: true, version: 5, generation: 'gen-a',"
            "         fields: { boards: 5 }, agentic: [], seq: 5, stale: false,"
            "         missing: [] });",
        )
        assert [m["type"] for m in posted] == [dashboard_frame.REFETCH_MESSAGE_TYPE]


def test_the_refetch_type_matches_the_tab_that_answers_it() -> None:
    """The page posts this and the browser tab is what turns it into a full read.

    Mirrored across two languages, so a drift is silent in the worst way: the page
    would ask forever and the tab would never hear it, leaving a dashboard applying
    no patches and showing no error -- indistinguishable from a quiet crew.
    """
    # `parents[2]` is the repository root: this module sits at `src/kiro_crew/`, so
    # `parents[1]` is `src` and the path below it never exists -- which made this pin
    # skip itself every run while reading as green.
    tab = (
        Path(dashboard_frame.__file__).resolve().parents[2]
        / "website"
        / "src"
        / "pages"
        / "members"
        / "CrewDynamicDashboard.tsx"
    )
    # FAILS rather than skips. A cross-language pin that cannot find one of its two
    # sides has not been satisfied, and a skip is how it went unnoticed before.
    assert tab.exists(), f"the tab this pin compares against is not at {tab}"
    text = tab.read_text(encoding="utf-8")
    assert f"'{dashboard_frame.REFETCH_MESSAGE_TYPE}'" in text, (
        "the tab does not carry the refetch type the frame posts, so a page that "
        "cannot place a patch asks for a read nothing answers"
    )
    assert "REFETCH_MESSAGE_TYPE" in text


def test_the_bootstrap_posts_the_type_the_constant_names() -> None:
    """The literal inside the script and the exported name, pinned to each other."""
    assert f'"{dashboard_frame.REFETCH_MESSAGE_TYPE}"' in dashboard_frame._BOOTSTRAP_JS


class TestSubscribeCountsAsAPageThatRefills:
    """A page that draws through ``subscribe`` is one a refill reaches.

    The beacon's ``refill`` flag is what lets the host post new values into the open
    document instead of loading a new one. The probe only watched ``addEventListener``,
    and ``subscribe`` never touches it, so every such page was re-minted on every read
    and the frame was swapped each time the data moved.
    """

    #: The bootstrap and then the probe, in the order the composed document runs them.
    _SCRIPTS = "\n".join((dashboard_frame._BOOTSTRAP_JS, dashboard_frame._REFILL_PROBE_JS))
    _FLAG = f"window[{dashboard_frame._REFILL_FLAG!r}]"

    def _flag_after(self, page_js: str) -> Any:
        calls = _run(self._SCRIPTS, _read(), f"{page_js}\n__calls.push({self._FLAG});")
        return calls[-1]

    def test_a_page_that_subscribes_reports_that_it_refills(self) -> None:
        assert self._flag_after("window.kirocrew.subscribe('boards', () => {});") is True

    def test_a_page_that_never_listens_still_reports_that_it_does_not(self) -> None:
        """The flag must stay false for a page that draws once, or the host would post
        values into a document that cannot show them."""
        assert self._flag_after("") is False

    def test_a_call_that_registers_nothing_does_not_count(self) -> None:
        """``subscribe`` returns early for a bad name or callback, so no subscriber
        exists and nothing on the page would redraw."""
        assert self._flag_after("window.kirocrew.subscribe('boards', 'not a function');") is False


def test_a_patch_naming_a_field_missing_dims_the_cell_it_carried_forward() -> None:
    """A patch merges the fields it did not move, so the old value is still in
    ``fields``. With no band, ``missing`` is the only thing that can dim it."""
    calls = _run(
        dashboard_frame._BOOTSTRAP_JS,
        _read(),
        "const cell = { attrs: {}, textContent: '',"
        "  getAttribute(k) { return k === 'data-dashboard-field' ? 'boards' : this.attrs[k]; },"
        "  setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };"
        "nodes.push(cell);"
        "__push({ patch: true, version: 1, fields: { boards: 5 }, missing: [] });"
        "__calls.push([cell.textContent, cell.attrs['data-dashboard-missing'] || null]);"
        "__push({ patch: true, version: 2, fields: {}, missing: ['boards'] });"
        "__calls.push([cell.textContent, cell.attrs['data-dashboard-missing'] || null,"
        "  window.kirocrew.fields.boards]);",
    )
    assert calls == [["5", None], ["5", "true", 5]], calls


def test_a_field_named_constructor_is_not_dimmed_while_it_resolves() -> None:
    """The missing set is looked up as own keys, so an inherited property such as
    ``constructor`` never reads as missing."""
    calls = _run(
        dashboard_frame._BOOTSTRAP_JS,
        _read(fields={"constructor": 1}),
        "const cell = { attrs: {}, textContent: '',"
        "  getAttribute(k) { return k === 'data-dashboard-field' ? 'constructor' : this.attrs[k]; },"
        "  setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };"
        "nodes.push(cell);"
        "__push({ patch: true, version: 1, fields: { constructor: 7 }, missing: [] });"
        "__calls.push([cell.textContent, cell.attrs['data-dashboard-missing'] || null]);",
    )
    assert calls == [["7", None]], calls
