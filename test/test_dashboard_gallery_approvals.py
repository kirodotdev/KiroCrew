"""The gallery's approvals page: Living UI on a Dashboard page.

The page says how many approvals wait on the reader, how many were asked for and
answered, and which ones wait. Motion happens on a change only: a count follows to its
new value, a newly arrived approval's row rises and fades with an envelope, and then the
page is still. There is no idle tour.

The counts are text bindings the host fills, so the page shows the right numbers before
any script moves them. The behaviour is checked by RUNNING the page's own script under
the host's real bootstrap in Node, with a fake frame clock, over values folded from real
``approval/requested`` and ``approval/decided`` entries. A string search could not tell
a count that follows from one that snaps.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import crew_log as lg
from kiro_crew import dashboard_frame
from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

GALLERY = (
    Path(mdf.__file__).resolve().parents[2]
    / "builtin_skills"
    / "dashboard"
    / "gallery"
    / "approvals"
    / "package.json"
)
#: The toolkit's own block extractor, the code that copies each block into its SKILL.md.
BLOCK_COPIER = Path(__file__).resolve().parents[1] / "scripts" / "copy_living_ui_blocks.py"
GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Any, _floor_monkeypatch: Any) -> Any:
    """The planted session logs go into a data home of their own."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture(scope="module")
def page() -> dict[str, Any]:
    """The stored gallery package, through the same gate a save would use."""
    return dp.parse_package(GALLERY.read_text(encoding="utf-8"))


_UNITS = itertools.count()


def _folded(*asks: str, answered: tuple[str, ...] = ()) -> dict[str, Any]:
    """The approvals fold over real entries: each id in *asks* asked, each in *answered* allowed."""
    unit = f"s-approvals-{next(_UNITS)}"
    handle = CrewLog.create(lg.KIND_SESSION, unit, owner="raymond", agent="kirocrew")
    for turn, approval_id in enumerate(asks, start=1):
        data = {"turn": turn, "approval_id": approval_id, "tool": f"tool-{approval_id}"}
        handle.append("approval/requested", {**data, "reason": f"why {approval_id}"}, src=GATEWAY)
    for approval_id in answered:
        data = {"turn": 1, "approval_id": approval_id, "decision": "allow", "by": "raymond"}
        handle.append("approval/decided", data, src=GATEWAY)
    return crew_log.fold_approvals(tuple(handle.iter_from(1)))


def _fields(page: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    """The fields the page is sent, read off a folded value by the model's own paths."""
    types = page["model"]["types"]
    return {name: value[spec["source"]["path"]] for name, spec in types.items()}


#: The DOM the bootstrap and the page touch, a frame clock that only moves when a test
#: says so, and an IntersectionObserver that reports the stage in view at load.
_SHIM = r"""
let clock = 0;
let frames = new Map();
let nextFrame = 1;
globalThis.requestAnimationFrame = (cb) => {
  const id = nextFrame++;
  frames.set(id, cb);
  return id;
};
globalThis.cancelAnimationFrame = (id) => { frames.delete(id); };
globalThis.matchMedia = () => ({
  matches: REDUCED, addEventListener() {}, removeEventListener() {},
});
const observers = [];
globalThis.IntersectionObserver = class {
  constructor(cb) { this.cb = cb; observers.push(this); }
  observe(el) { this.el = el; }
  disconnect() {}
};
class Element {
  constructor(tag, attrs = {}, text = '') {
    this.attrs = { ...attrs };
    this.hidden = 'hidden' in this.attrs;
    this.kids = [];
    this.own = text;
    const props = {};
    this.style = { props, setProperty: (name, value) => { props[name] = value; } };
  }
  appendChild(kid) { this.kids.push(kid); return kid; }
  get textContent() {
    return this.kids.length ? this.kids.map((k) => k.textContent).join(' ') : this.own;
  }
  set textContent(value) { this.kids = []; this.own = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
}
const byId = {};
for (const spec of ELEMENTS) byId[spec.attrs.id] = new Element(spec.tag, spec.attrs, spec.text);
const listeners = {};
globalThis.window = globalThis;
globalThis.addEventListener = (type, fn) => {
  (listeners[type] = listeners[type] || []).push(fn);
};
globalThis.dispatchEvent = (event) => {
  for (const fn of listeners[event.type] || []) fn(event);
  return true;
};
globalThis.Event = class { constructor(type) { this.type = type; } };
globalThis.parent = { postMessage() {} };
const island = { textContent: ISLAND };
globalThis.document = {
  readyState: 'loading',
  documentElement: { setAttribute() {}, lang: '' },
  getElementById: (id) => (id === 'kirocrew-dashboard-data' ? island : byId[id] || null),
  querySelectorAll: (selector) => (selector === '[data-dashboard-field]'
    ? Object.values(byId).filter((el) => el.getAttribute('data-dashboard-field') !== null)
    : []),
  createElement: (tag) => new Element(tag),
};
let version = 0;
const send = (read) => {
  const event = { source: globalThis.parent, data: { type: DATA_TYPE, read } };
  for (const fn of listeners.message || []) fn(event);
};
const base = (fields) => ({
  fields, agentic: [], seq: 0, stale: false, missing: [], written_at: {}, locale: 'en',
  generation: 'g',
});
// A patch as the controller sends one: what moved, one version on.
const push = (fields) => { version += 1; send({ ...base(fields), version, patch: true }); };
// A whole read at the version the page holds, as a refill or a refetch answer sends one.
const full = (fields) => send({ ...base(fields), version });
// The end of parse, then the observer's first report that the stage is in view.
const load = () => {
  document.readyState = 'interactive';
  for (const fn of listeners.DOMContentLoaded || []) fn();
  for (const o of observers) o.cb([{ isIntersecting: true, target: o.el }]);
};
// One frame, 1/60 s on. What it returns is how many frames the page still asks for.
const frame = () => {
  clock += 1000 / 60;
  const due = [...frames.values()];
  frames = new Map();
  for (const cb of due) cb(clock);
  return frames.size;
};
const asleep = () => frames.size === 0;
const text = (id) => byId[id].textContent;
const rows = () => byId.asks.kids.map((row) => ({
  text: row.textContent, glow: Number(row.style.props['--glow'] || 0),
}));
const brightest = () => Math.max(0, ...rows().map((row) => row.glow));
// Frames until the page is still, at most ten seconds of them, with look() after each.
const run = (look) => {
  const seen = [];
  for (let i = 0; i < 600; i++) {
    const left = frame();
    seen.push(look());
    if (!left) break;
  }
  return seen;
};
const out = [];
"""


class _ElementsWithIds(HTMLParser):
    """Every element of the page's markup that has an id: its tag, its attributes, and the
    text it holds before its first child or its end tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[dict[str, Any]] = []
        self._open: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        named = {name: value or "" for name, value in attrs}
        self._open = {"tag": tag, "attrs": named, "text": ""} if "id" in named else None
        if self._open is not None:
            self.found.append(self._open)

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._open["text"] += data

    def handle_endtag(self, tag: str) -> None:
        self._open = None


def _elements(markup: str) -> list[dict[str, Any]]:
    parser = _ElementsWithIds()
    parser.feed(markup)
    parser.close()
    return [{**element, "text": element["text"].strip()} for element in parser.found]


def _drive(page: dict[str, Any], fields: dict[str, Any], scenario: str, **data: Any) -> list[Any]:
    """Load the page over *fields*, run *scenario*, and return what it put in ``out``.

    *data* is handed to the scenario as constants. ``REDUCED=True`` among them turns
    reduced motion on for the whole run.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI builds the website, so node is present
        pytest.skip("node is needed to run the page's own script")
    html = page["view"]["html"]
    island = {**dashboard_frame.read_payload(fields), "version": 0, "generation": "g"}
    constants = {"REDUCED": False, **data}
    program = "\n".join(
        (
            f"const ISLAND = {json.dumps(json.dumps(island))};",
            f"const ELEMENTS = {json.dumps(_elements(html.split('<style>')[0]))};",
            f"const DATA_TYPE = {json.dumps(dashboard_frame.DATA_MESSAGE_TYPE)};",
            *(f"const {name} = {json.dumps(value)};" for name, value in constants.items()),
            _SHIM,
            dashboard_frame._BOOTSTRAP_JS,
            html.split("<script>")[1].split("</script>")[0],
            "load();",
            scenario,
            "console.log(JSON.stringify(out));",
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


def _whole(seen: list[str]) -> list[int]:
    assert all(re.fullmatch(r"\d+", shown) for shown in seen), f"a frame showed {seen}"
    return [int(shown) for shown in seen]


# --------------------------------------------------------------------------- #
# The package and its bindings
# --------------------------------------------------------------------------- #


def test_the_gallery_page_is_a_package_the_write_gate_accepts(page: dict[str, Any]) -> None:
    assert dp.canonical_package_content(json.dumps(page))
    assert len(page["view"]["html"].encode("utf-8")) < dp.MAX_VIEW_HTML_BYTES
    assert set(dp.subscribed_fields(page).values()) == {
        "fold:approvals.pending",
        "fold:approvals.requested",
        "fold:approvals.decided",
        "fold:approvals.pending_requests",
    }


def test_every_path_it_binds_is_a_field_the_fold_really_carries(page: dict[str, Any]) -> None:
    value = _folded("a1", "a2", answered=("a1",))
    for name, spec in page["model"]["types"].items():
        assert spec["source"]["path"] in value, f"{name} reads a path the approvals fold lacks"


def test_every_row_key_the_page_reads_is_one_a_real_pending_request_carries(
    page: dict[str, Any],
) -> None:
    """Keys taken off a folded row, so a name only this file believes in cannot pass."""
    emitted = set(_folded("a1")["pending_requests"][0])
    read = set(re.findall(r"\bask\.([a-z_]+)", page["view"]["html"]))
    assert read == {"approval_id", "tool", "reason"}
    assert read <= emitted, f"the page reads {sorted(read - emitted)}, which no row carries"


def test_the_copied_toolkit_blocks_are_the_toolkits_own(page: dict[str, Any]) -> None:
    """Agents copy this page, so it carries the toolkit's current blocks, each byte for byte
    as the toolkit's own extractor reads it, licence notice included, and nothing else: no
    idle tour, and no timer that could wake a still page. When a block changes in
    ``toolkit/living-ui.js``, copy it into this page again, unchanged."""
    copier = load_skill_script("copy_living_ui_blocks_for_the_gallery", BLOCK_COPIER)
    toolkit = dict(copier.read_blocks(copier.TOOLKIT.read_text(encoding="utf-8")))
    html = page["view"]["html"]
    for helper in ("loop", "follow", "burst"):
        assert toolkit[helper] in html, (
            f"the page's {helper} block is not the one in toolkit/living-ui.js: copy that "
            "block into the page's script again, unchanged"
        )
    assert html.count("// Adapted from anatomy by Ryan (MIT License)") == 3
    for absent in ("LivingUI.tour", "setInterval", "setTimeout"):
        assert absent not in html, f"the page carries {absent}"


# --------------------------------------------------------------------------- #
# Still first: what the page shows before, or without, its script
# --------------------------------------------------------------------------- #


def test_every_count_is_a_cell_the_host_fills_without_the_script(page: dict[str, Any]) -> None:
    fields = _fields(page, _folded("a1", "a2", "a3", answered=("a3",)))
    shown = dashboard_frame.fill_bindings_as_text(page["view"]["html"], {"fields": fields})
    for name, count in (("pending", 2), ("requested", 3), ("decided", 1)):
        assert f'data-dashboard-field="{name}">{count}</span>' in shown


def test_what_only_the_script_draws_ships_hidden(page: dict[str, Any]) -> None:
    """The list and its empty line are claims only the script can make true."""
    html = page["view"]["html"]
    assert '<div id="waiting" hidden>' in html
    assert '<p id="none" class="none" hidden>Nothing right now</p>' in html
    notice = html.split("<noscript>")[1].split("</noscript>")[0]
    assert "scripts off" in notice and "Ask whoever administers" in notice


# --------------------------------------------------------------------------- #
# Motion on change only
# --------------------------------------------------------------------------- #


def test_a_count_follows_to_its_new_value_and_the_page_is_still_again(
    page: dict[str, Any],
) -> None:
    calls = _drive(
        page,
        _fields(page, _folded("a1", "a2")),
        """
        run(() => 0);
        out.push(asleep(), text('pending'));
        push({ pending: 7 });
        out.push(text('pending'), run(() => text('pending')), asleep());
        """,
    )
    still, loaded, at_push, seen, settled = calls
    assert still and loaded == "2", "the loaded page moved, or showed the wrong count"
    assert at_push == "2", "the count snapped to its target on the push"
    shown = _whole(seen)
    assert shown == sorted(shown) and max(shown) == 7, f"it passed its target: {shown}"
    assert {3, 4, 5, 6} <= set(shown), f"it skipped the numbers between: {shown}"
    assert shown.index(7) == len(shown) - 1, "the loop ran on after the count landed"
    assert settled


def test_a_count_that_goes_down_moves_down_and_not_past(page: dict[str, Any]) -> None:
    seven = _fields(page, _folded("a1", "a2", "a3", "a4", "a5", "a6", "a7"))
    calls = _drive(page, seven, "push({ pending: 3 }); out.push(run(() => text('pending')));")
    shown = _whole(calls[0])
    assert shown == sorted(shown, reverse=True) and min(shown) == 3 and shown[-1] == 3
    assert {4, 5, 6} <= set(shown), f"it skipped the numbers between: {shown}"


def test_a_new_approval_lights_its_row_and_nothing_else_does(page: dict[str, Any]) -> None:
    """New means absent from the LAST list: not the first list, not the same list again
    (as a whole read or as a patch), and not a list that lost a row."""
    both = _fields(page, _folded("a1", "a2"))
    calls = _drive(
        page,
        both,
        """
        out.push(Math.max(0, ...run(brightest)));
        full(BOTH);
        out.push(Math.max(0, ...run(brightest)));
        push({ pending_requests: BOTH.pending_requests });
        out.push(Math.max(0, ...run(brightest)));
        push({ pending_requests: ONE });
        out.push(Math.max(0, ...run(brightest)));
        push({ pending_requests: NEW });
        out.push(run(rows), asleep());
        """,
        BOTH=both,
        ONE=_folded("a1", "a2", answered=("a2",))["pending_requests"],
        NEW=_folded("a1", "a2", "a3", answered=("a2",))["pending_requests"],
    )
    *quiet, seen, settled = calls
    assert quiet == [0, 0, 0, 0], f"a row lit on a list with nothing new in it: {quiet}"
    a3 = [next(r["glow"] for r in frame if r["text"].startswith("tool-a3")) for frame in seen]
    a1 = [next(r["glow"] for r in frame if r["text"].startswith("tool-a1")) for frame in seen]
    assert a3[0] < 0.5 < max(a3), f"the envelope did not rise from a flat start: {a3}"
    assert a3[-1] == 0 and settled, "the envelope did not fade back to a still page"
    assert max(a1) == 0, "a row already listed lit up"


def test_several_approvals_in_one_push_each_light(page: dict[str, Any]) -> None:
    calls = _drive(
        page,
        _fields(page, _folded("a1")),
        "push({ pending_requests: MANY }); out.push(run(rows));",
        MANY=_folded("a1", "b1", "b2", "b3")["pending_requests"],
    )
    peak: dict[str, float] = {}
    for frame in calls[0]:
        for row in frame:
            peak[row["text"]] = max(peak.get(row["text"], 0.0), row["glow"])
    assert peak["tool-a1 why a1"] == 0
    assert all(peak[f"tool-{b} why {b}"] > 0.5 for b in ("b1", "b2", "b3")), peak


def test_two_pushes_close_together_follow_the_newer_value(page: dict[str, Any]) -> None:
    calls = _drive(
        page,
        _fields(page, _folded("a1", "a2")),
        """
        push({ pending: 9, pending_requests: FIRST });
        frame(); frame();
        push({ pending: 4, pending_requests: SECOND });
        out.push(run(() => [text('pending'), brightest()]), rows().length);
        """,
        FIRST=_folded("a1", "a2", "b1")["pending_requests"],
        SECOND=_folded("a1", "a2", "b1", "b2")["pending_requests"],
    )
    seen, listed = calls
    shown = _whole([pair[0] for pair in seen])
    assert shown == sorted(shown) and max(shown) == 4 and shown[-1] == 4, shown
    assert max(pair[1] for pair in seen) > 0.5 and listed == 4


def test_reduced_motion_jumps_every_count_and_lights_nothing(page: dict[str, Any]) -> None:
    calls = _drive(
        page,
        _fields(page, _folded("a1", "a2")),
        """
        run(() => 0);
        push({ pending: 6, requested: 9, pending_requests: NEW });
        out.push([text('pending'), text('requested')]);
        out.push(run(() => [text('pending'), brightest()]), rows().length, asleep());
        """,
        NEW=_folded("a1", "a2", "a3")["pending_requests"],
        REDUCED=True,
    )
    at_push, seen, listed, settled = calls
    assert at_push == ["6", "9"], "a count moved instead of jumping under reduced motion"
    assert all(pair == ["6", 0] for pair in seen), f"something moved or lit: {seen}"
    assert listed == 3 and settled, "the page stopped updating under reduced motion"


# --------------------------------------------------------------------------- #
# The inputs beside the ordinary ones
# --------------------------------------------------------------------------- #


def test_an_empty_list_says_nothing_right_now(page: dict[str, Any]) -> None:
    calls = _drive(
        page,
        _fields(page, _folded()),
        "out.push(text('pending'), byId.none.hidden, byId.waiting.hidden, rows().length);",
    )
    assert calls == ["0", False, False, 0]


def test_a_null_or_missing_field_leaves_the_hosts_text(page: dict[str, Any]) -> None:
    """A null count shows the host's dash and does not move, and a null list shows no
    list. A later count is shown where it is, since there was no number to move from. The
    first list the page sees is a first list even after a null, so nothing in it lights,
    and a list after a null that followed a list is compared with that list. A field a
    whole read leaves out keeps the text it had."""
    calls = _drive(
        page,
        {"pending": None, "requested": None, "decided": 3, "pending_requests": None},
        """
        out.push(text('pending'), byId.waiting.hidden);
        push({ pending: 4, pending_requests: LIST });
        out.push(text('pending'), byId.waiting.hidden, Math.max(0, ...run(brightest)));
        full({ pending: 4, pending_requests: LIST });
        out.push(text('requested'), text('decided'), rows().length);
        push({ decided: null, pending_requests: null });
        out.push(run(() => text('decided')), byId.waiting.hidden);
        push({ pending_requests: MORE });
        out.push(run(rows));
        """,
        LIST=_folded("a1", "a2")["pending_requests"],
        MORE=_folded("a1", "a2", "a3")["pending_requests"],
    )
    assert calls[:2] == ["\u2014", True]
    assert calls[2:5] == ["4", False, 0]
    assert calls[5:8] == ["\u2014", "3", 2], "a field the read left out lost its last text"
    assert set(calls[8]) == {"\u2014"}, f"a count that turned null moved: {calls[8]}"
    assert calls[9] is True, "a list that turned null still shows its old rows"
    peak = {row["text"][:7]: 0.0 for row in calls[10][0]}
    for frame in calls[10]:
        for row in frame:
            peak[row["text"][:7]] = max(peak[row["text"][:7]], row["glow"])
    assert peak == {"tool-a1": 0, "tool-a2": 0, "tool-a3": peak["tool-a3"]}, peak
    assert peak["tool-a3"] > 0.5, "the one new approval after the null did not light"


# --------------------------------------------------------------------------- #
# The check dashboard_save runs
# --------------------------------------------------------------------------- #


def test_the_offline_check_dashboard_save_runs_finds_no_script_error(
    page: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run where this host has a browser the check may start, as the save would."""
    from kiro_crew import dashboard_verify, platform_compat

    if not platform_compat.IS_LINUX:
        pytest.skip("the page check runs on Linux only")
    monkeypatch.setattr(dashboard_verify, "find_launcher_python", lambda: sys.executable)
    monkeypatch.setattr(platform_compat, "host_available_mib", lambda: 1 << 20)
    read = dashboard_frame.read_payload(_fields(page, _folded("a1", "a2", answered=("a1",))))
    verdict = dashboard_verify.verify_page(page["view"]["html"], read, model=page["model"])
    if verdict.status in (dashboard_verify.STATUS_SKIPPED, dashboard_verify.STATUS_TIMED_OUT):
        pytest.skip(f"no usable browser here: {verdict.status}: {verdict.reason}")
    assert verdict.status == dashboard_verify.STATUS_PASSED, verdict.wire()
    assert verdict.errors == ()
