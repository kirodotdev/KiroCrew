"""The ``standup`` and ``pr-watch`` footers count the items the work fold forgot.

Both pages close with "N item(s) on the board". The ``work`` fold keeps the open items
and the newest closed ones; every older closed item leaves ``items`` and is counted in
``work.forgotten`` (``crew_log/projection_work.py``). A footer that counts ``items``
alone therefore stops growing with the board on a long goal. ``goal-board`` already
adds ``forgotten.items`` to its own footer cell; these two pages now do the same.

The page scripts are RUN under ``node`` over a field bag here, because "the footer reads
7" is a claim about what executes, and a source pattern naming ``forgotten`` is met by a
page that never adds it. The standup COLUMNS are deliberately left on the listed items:
the page answers "what changed since you last looked", and a forgotten item closed
before the newest closed items the fold still keeps.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import TemplateManifest, load_template
from kiro_crew.subprocess_utf8 import UTF8_TEXT

BUILTIN = Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard_templates/builtin"

#: A DOM just wide enough to run either page's script to its end. Every element id and
#: every ``data-dashboard-field`` cell resolves to a recording element, so the probe can
#: read back what the script wrote into the ``items`` cell and the standup columns.
_DOM = r"""
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.className = ''; this.style = {}; this._t = '';
    this.title = ''; this.hidden = false; this.parentNode = null;
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  get textContent() { return this._t + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this._t = String(v); this.children = []; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c) { return this.appendChild(c); }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
  setAttribute() {}
  getAttribute() { return null; }
  removeAttribute() {}
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
const byId = {};
const cells = {};
const root = new El('body');
function cellFor(sel) {
  const m = /^\[data-dashboard-field="([^"]+)"\]$/.exec(sel);
  if (!m) return null;
  if (!cells[m[1]]) cells[m[1]] = new El('span');
  return cells[m[1]];
}
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.setTimeout = () => 0;
globalThis.document = {
  body: root,
  documentElement: new El('html'),
  querySelectorAll: () => [],
  querySelector: (sel) => cellFor(sel),
  getElementById: (id) => {
    if (!byId[id]) { byId[id] = new El('div'); root.appendChild(byId[id]); }
    return byId[id];
  },
  createElement: (tag) => new El(tag),
  createElementNS: (ns, tag) => new El(tag),
  createTextNode: (s) => { const n = new El('#text'); n.textContent = s; return n; },
};
"""

#: The footer cell, and for standup the four column header counts in page order.
_PROBE = r"""
const cols = byId.cols ? byId.cols.children.map((col) => col.children[0].children[1]._t) : null;
console.log(JSON.stringify({ items: cells.items ? cells.items._t : null, cols }));
"""

_ABSENT = object()


def _item(state: str, n: int, **extra: Any) -> dict[str, Any]:
    return {"item_id": f"it-{state}-{n}", "title": f"a {state} task", "state": state, **extra}


#: Three listed items: one accepted, one open, one open with a PR (pr-watch draws it).
_LISTED = [_item("accepted", 1), _item("open", 2), _item("open", 3, pr=42)]

#: Four closed items the fold counts in ``forgotten`` but does not list: three
#: accepted, one rejected.
_GONE = {"created": 7, "items": 4, "accepted": 3, "rejected": 1, "abandoned": 0, "in_round": 0}


def _run(page: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Run *page*'s own script over *fields* and return what it drew."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI builds the website, so node is present
        pytest.fail("node is required to run the page's script; these assertions cannot skip")
    html = (BUILTIN / page / "template.html").read_text(encoding="utf-8")
    script = html.split("<script>")[1].split("</script>")[0]
    program = "\n".join(
        (f"globalThis.kirocrew = {{ fields: {json.dumps(fields)} }};", _DOM, script, _PROBE)
    )
    done = subprocess.run(
        [node, "-"], input=program, capture_output=True, timeout=60, check=False, **UTF8_TEXT
    )
    assert done.returncode == 0, done.stderr
    return dict(json.loads(done.stdout.strip().splitlines()[-1]))


@pytest.fixture(scope="module")
def manifests() -> dict[str, TemplateManifest]:
    return {
        page: load_template(BUILTIN / page)[0] for page in ("standup", "pr-watch", "goal-board")
    }


@pytest.mark.parametrize("page", ["standup", "pr-watch"])
class TestTheFooterCountsTheWholeBoard:
    def test_it_binds_forgotten_the_way_goal_board_binds_it(
        self, page: str, manifests: dict[str, TemplateManifest]
    ) -> None:
        mine = manifests[page].fields.get("forgotten")
        assert mine is not None, f"{page} does not bind the work fold's forgotten counts"
        theirs = manifests["goal-board"].fields["forgotten"]
        assert (mine.type, mine.fold, mine.path, mine.agentic) == (
            theirs.type,
            theirs.fold,
            theirs.path,
            theirs.agentic,
        )

    def test_the_footer_adds_the_forgotten_items(self, page: str) -> None:
        drawn = _run(page, {"items": _LISTED, "forgotten": _GONE})
        assert drawn["items"] == "7", "3 listed + 4 forgotten are on the board"

    @pytest.mark.parametrize(
        "forgotten",
        [
            pytest.param(_ABSENT, id="absent"),
            pytest.param(None, id="null"),
            pytest.param("many", id="string"),
            pytest.param([3], id="array"),
            pytest.param({"items": -2}, id="negative"),
            pytest.param({"items": "5"}, id="numeric-string"),
        ],
    )
    def test_a_missing_or_odd_forgotten_reads_as_zero(self, page: str, forgotten: Any) -> None:
        fields: dict[str, Any] = {"items": _LISTED}
        if forgotten is not _ABSENT:
            fields["forgotten"] = forgotten
        assert _run(page, fields)["items"] == "3"


class TestTheStandupColumnsStayOnTheListedItems:
    """The columns answer "what changed since you last looked", so a forgotten item --
    closed before the newest closed items the fold keeps -- is not added to them."""

    def test_the_columns_do_not_change_with_forgotten(self) -> None:
        without = _run("standup", {"items": _LISTED})["cols"]
        with_gone = _run("standup", {"items": _LISTED, "forgotten": _GONE})["cols"]
        assert without == ["1", "0", "2", "0"]
        assert with_gone == without


#: The template version each page shipped at BEFORE it counted ``forgotten``.
#: A crewmate that adopted the page then holds a copy at this version, with the old
#: undercount frozen inside it (standup and pr-watch: the footer; flow: its tiles).
_PRE_FIX_VERSION = {"standup": 1, "pr-watch": 2, "flow": 1}


class TestAnAdoptedCopyOfTheOldFooterTurnsStale:
    """An adopted copy is frozen at adopt and turns ``stale`` only when the registry's
    version is HIGHER (``instance._state_of``). A fix to the page that does not raise
    the version reaches new adoptions only: every page adopted before it keeps the
    undercount while the frame still says ``live``. So each page must ship above the
    version its pre-fix copies carry.

    Driven through the real store: adopt the shipped page, then set the stored copy's
    template version back to the pre-fix one, which is exactly the record an earlier
    adoption left on disk.
    """

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, _floor_monkeypatch):
        _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        yield

    @pytest.mark.parametrize("page", sorted(_PRE_FIX_VERSION))
    def test_a_pre_fix_copy_reads_stale(self, page: str) -> None:
        from kiro_crew.dashboard_templates import instance

        slug = f"pre-fix-{page}"
        instance.stage_preview(slug, template_id=page)
        instance.apply_preview(slug, session_id="")
        record_path = instance.instance_dir(slug) / instance._RECORD_FILE
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["template"]["version"] = _PRE_FIX_VERSION[page]
        record_path.write_text(json.dumps(record), encoding="utf-8")

        copy = instance.read(slug)
        assert copy.template_version == _PRE_FIX_VERSION[page]
        assert copy.state == instance.STATE_STALE, (
            f"a {page} copy adopted before the forgotten-items fix reads "
            f"{copy.state!r}: the manifest version was not raised, so the old "
            "undercount keeps showing as live"
        )
