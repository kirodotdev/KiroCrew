"""Out-of-date agentic Dashboard fields (feature preview ``dashboard_stale_fields``).

A field the crewmate writes with ``dashboard_write`` carries the log's own write time
(the ``agentic`` fold's ``at``), but nothing compared it with an age limit: the page drew
a day-old value as current and the turn never asked for a new one. With the preview on:

1. an agentic field declares ``stale_after_s`` (template manifest or package model),
   24 hours by default, ``0`` for never stale;
2. the ``[DASHBOARD]`` turn block -- and a V1 crewmate's warm-turn notice -- names each
   field past its limit, and each agentic field never written;
3. the page read carries ``stale_after`` and the frame bootstrap marks each bound
   ELEMENT past it with how old it is and that the Bot is asked to update it,
   re-checking once a minute;
4. the Daily Goal Reminder and Heartbeat wakes carry the same line.

With the preview off every one of those surfaces is what it was before. Each test below
fails when its piece of the change is removed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from kiro_crew import dashboard_agentic, dashboard_frame
from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifacts import ArtifactValidationError
from kiro_crew.dashboard import goal_reminder
from kiro_crew.dashboard.handlers.member_dashboard_fields import package_manifest
from kiro_crew.dashboard_templates import instance
from kiro_crew.dashboard_templates.manifest import ManifestError, parse_manifest

SLUG = "code-reviewer"
MEMBER = "Code Reviewer"
DAY = 24 * 3600


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _manifest(fields: dict[str, Any]) -> Any:
    return parse_manifest(
        {
            "id": "stale-probe",
            "version": 1,
            "title": "Stale probe",
            "description": "a page for these cases",
            "source": "user",
            "fields": fields,
        }
    )


def _agentic(**extra: Any) -> dict[str, Any]:
    return {"type": "string", "source": {"agentic": True}, **extra}


class TestTheDeclaration:
    def test_a_template_field_declares_its_limit_and_zero_means_never(self) -> None:
        m = _manifest({"verdict": _agentic(stale_after_s=3600), "name": _agentic(stale_after_s=0)})
        assert m.fields["verdict"].stale_after_s == 3600
        assert m.fields["name"].stale_after_s == 0
        assert dashboard_agentic.stale_after_seconds(m.fields["verdict"]) == 3600

    def test_an_undeclared_field_takes_the_default_of_a_day(self) -> None:
        m = _manifest({"verdict": _agentic()})
        assert m.fields["verdict"].stale_after_s is None
        assert dashboard_agentic.stale_after_seconds(m.fields["verdict"]) == DAY

    @pytest.mark.parametrize("bad", [30, 31 * DAY, True, "3600", -1])
    def test_a_limit_out_of_range_is_refused(self, bad: Any) -> None:
        with pytest.raises(ManifestError, match="stale_after_s"):
            _manifest({"verdict": _agentic(stale_after_s=bad)})

    def test_a_fold_field_cannot_declare_one(self) -> None:
        with pytest.raises(ManifestError, match="only an agentic field declares stale_after_s"):
            _manifest(
                {
                    "count": {
                        "type": "number",
                        "source": {"fold": "work", "path": "summary.open"},
                        "stale_after_s": 3600,
                    }
                }
            )


def _package(types: dict[str, Any]) -> dict[str, Any]:
    html = "".join(f'<p data-dashboard-field="{name}"></p>' for name in types)
    return {
        "kind": "dashboard",
        "bound_to": "crewmate:mate",
        "model": {"types": types},
        "view": {"html": html},
        "theme": {"tokens": {"--panel-bg": "oklch(21% 0 0)"}},
    }


class TestThePackageDeclaration:
    """Most crewmates draw their own package page, which has no upkeep block, so the
    limit has to be declarable there too and reach the manifest the reads use."""

    def test_an_agentic_package_field_keeps_its_limit_through_the_translation(self) -> None:
        canonical = dp.validate_package(
            _package({"lane": {"type": "text", "source": {"agentic": True}, "stale_after_s": 7200}})
        )
        assert canonical["model"]["types"]["lane"]["stale_after_s"] == 7200
        assert package_manifest(canonical).fields["lane"].stale_after_s == 7200

    def test_a_fold_package_field_is_refused_one(self) -> None:
        with pytest.raises(ArtifactValidationError, match="only for a field the agent writes"):
            dp.validate_package(
                _package(
                    {
                        "open": {
                            "type": "number",
                            "source": {"fold": "work", "path": "summary.open_prs"},
                            "stale_after_s": 7200,
                        }
                    }
                )
            )

    def test_a_bad_package_limit_is_refused(self) -> None:
        # The template's own rule, word for word: not the generic unknown-key refusal.
        with pytest.raises(ArtifactValidationError, match=r"must be 0 \(never stale\)"):
            dp.validate_package(
                _package(
                    {"lane": {"type": "text", "source": {"agentic": True}, "stale_after_s": 5}}
                )
            )


class TestTheStaleLine:
    NOW = 2_000_000_000.0

    def _page(self) -> Any:
        return _manifest(
            {
                "verdict": _agentic(),
                "blockers": _agentic(),
                "fresh": _agentic(),
                "name": _agentic(stale_after_s=0),
            }
        )

    def test_names_a_field_past_its_limit_and_one_never_written(self) -> None:
        written = {
            "verdict": _iso(self.NOW - 39 * 3600),
            "fresh": _iso(self.NOW - 3600),
        }
        line = dashboard_agentic.stale_line(self._page(), written, self.NOW)
        assert line.startswith("Out of date on your page: ")
        assert "verdict (last written 1d ago, limit 1d)" in line
        assert "blockers (never written)" in line
        assert "fresh" not in line, "a field inside its limit is current"
        assert "name" not in line, "stale_after_s 0 never goes stale, written or not"
        assert "dashboard_write" in line

    def test_nothing_out_of_date_is_no_line(self) -> None:
        written = {name: _iso(self.NOW - 60) for name in ("verdict", "blockers", "fresh")}
        assert dashboard_agentic.stale_line(self._page(), written, self.NOW) == ""

    def test_more_than_three_are_counted_not_listed(self) -> None:
        m = _manifest({f"f{i}": _agentic() for i in range(5)})
        line = dashboard_agentic.stale_line(m, {}, self.NOW)
        assert "and 2 more" in line, line

    def test_stale_after_is_the_write_time_plus_the_limit(self) -> None:
        m = _manifest({"verdict": _agentic(stale_after_s=3600), "name": _agentic(stale_after_s=0)})
        written = {"verdict": _iso(self.NOW), "name": _iso(self.NOW)}
        assert dashboard_agentic.stale_after_map(m, written) == {"verdict": _iso(self.NOW + 3600)}


@pytest.fixture
def _member_page(tmp_path, _floor_monkeypatch):
    """A crewmate on ``project-report`` with the crew panel on, its fold reads stubbed."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(
        instance, "default_template_id", lambda _slug, _ids: "project-report"
    )
    _floor_monkeypatch.setattr(dashboard_agentic, "_crew_panel_on", lambda: True)
    _floor_monkeypatch.setattr(dashboard_agentic, "_member_slot", lambda _m, _s: "dm:slot")
    _floor_monkeypatch.setattr(dashboard_agentic, "_unresolved_fold_fields", lambda *_a: {})
    # Every agentic field written two days ago: all past the default day.
    old = _iso(time.time() - 2 * DAY)
    page = dashboard_agentic.page_in_force(SLUG)
    assert page is not None
    names = sorted(dashboard_agentic.agentic_fields(page[1]))
    assert names, "project-report has no agentic field; these cases guard nothing"
    _floor_monkeypatch.setattr(
        dashboard_agentic, "_agentic_written_at", lambda _slot: {n: old for n in names}
    )
    return _floor_monkeypatch


def _preview(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(dashboard_agentic, "stale_fields_enabled", lambda: on)


@pytest.mark.usefixtures("_member_page")
class TestTheTurn:
    def test_preview_off_leaves_the_block_as_it_was(self, _member_page) -> None:
        _preview(_member_page, False)
        block = dashboard_agentic.turn_block(SLUG, MEMBER)
        assert block.startswith("[DASHBOARD]") and "Out of date" not in block, block

    def test_preview_on_names_the_out_of_date_fields_before_the_upkeep_pointer(
        self, _member_page
    ) -> None:
        _preview(_member_page, True)
        lines = dashboard_agentic.turn_block(SLUG, MEMBER).splitlines()
        stale = [i for i, line in enumerate(lines) if line.startswith("Out of date on your page")]
        assert len(stale) == 1, lines
        assert lines[-1].startswith("Upkeep:") and stale[0] == len(lines) - 2, lines

    def test_a_cold_fold_is_never_reported_as_never_written(self, _member_page) -> None:
        """No warm read (a restart): no claim, rather than every field 'never written'."""
        _preview(_member_page, True)
        _member_page.setattr(dashboard_agentic, "_agentic_written_at", lambda _slot: None)
        assert "Out of date" not in dashboard_agentic.turn_block(SLUG, MEMBER)

    def test_a_v1_warm_turn_carries_the_line_too(self, _member_page) -> None:
        _preview(_member_page, False)
        assert dashboard_agentic.unresolved_notice(SLUG, MEMBER) == ""
        _preview(_member_page, True)
        notice = dashboard_agentic.unresolved_notice(SLUG, MEMBER)
        assert notice.startswith("[DASHBOARD]\nOut of date on your page"), notice

    def test_the_check_in_wakes_get_the_same_line(self, _member_page) -> None:
        _preview(_member_page, False)
        assert goal_reminder.stale_line_for(SLUG, MEMBER) == ""
        _preview(_member_page, True)
        assert goal_reminder.stale_line_for(SLUG, MEMBER).startswith("Out of date on your page")


class TestTheHeartbeat:
    REC: dict[str, Any] = {
        "goals": [{"id": "g1", "text": "Ship it"}],
        "key_results": [],
        "schedules": {"heartbeat": True},
    }

    def test_a_stale_line_is_work_that_comes_before_the_nothing_to_do_rule(self) -> None:
        plain = goal_reminder.heartbeat_prompt(self.REC, has_session_tools=True)
        stale = "Out of date on your page: verdict (never written)."
        with_line = goal_reminder.heartbeat_prompt(self.REC, has_session_tools=True, stale=stale)
        assert stale not in plain
        assert with_line.index(stale) < with_line.index("call nothing_to_do at once")
        assert goal_reminder.HEARTBEAT_STALE_COUNTS in with_line
        assert with_line.replace(f"{stale} {goal_reminder.HEARTBEAT_STALE_COUNTS}\n\n", "") == plain


class TestThePayload:
    def test_the_map_rides_the_read_only_when_given(self) -> None:
        """Preview off (``None``) is today's read, key for key; on, the map rides it."""
        assert "stale_after" not in dashboard_frame.read_payload({"a": 1})
        got = dashboard_frame.read_payload({"a": 1}, stale_after={"a": "2030-01-01T00:00:00+00:00"})
        assert got["stale_after"] == {"a": "2030-01-01T00:00:00+00:00"}

    def test_page_stale_after_is_none_with_the_preview_off(self, _floor_monkeypatch) -> None:
        m = _manifest({"verdict": _agentic()})
        written = {"verdict": _iso(time.time())}
        _preview(_floor_monkeypatch, False)
        assert dashboard_agentic.page_stale_after(m, written) is None
        _preview(_floor_monkeypatch, True)
        assert set(dashboard_agentic.page_stale_after(m, written) or {}) == {"verdict"}


#: Bound elements with real attribute storage, a fake clock and a captured interval,
#: so a mark that only appears after time passes can be driven without waiting.
_SHIM = r"""
let NOW = START;
Date.now = () => NOW;
const listeners = {};
globalThis.addEventListener = (type, fn) => {
  (listeners[type] = listeners[type] || []).push(fn);
};
globalThis.parent = { postMessage() {} };
globalThis.Event = class { constructor(type) { this.type = type; } };
globalThis.dispatchEvent = () => true;
let tick = null;
globalThis.setInterval = (fn) => { tick = fn; return 1; };
globalThis.clearInterval = () => { tick = null; };
function el(name) {
  const attrs = { 'data-dashboard-field': name };
  return {
    attrs, textContent: '',
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    setAttribute: (k, v) => { attrs[k] = String(v); },
    removeAttribute: (k) => { delete attrs[k]; },
  };
}
const nodes = NAMES.map(el);
globalThis.document = {
  readyState: 'complete',
  documentElement: { setAttribute() {}, lang: '' },
  getElementById: (id) => (id === 'kirocrew-dashboard-data' ? { textContent: ISLAND } : null),
  querySelectorAll: () => nodes,
};
globalThis.window = globalThis;
const marks = () => Object.fromEntries(
  nodes.map((n) => [n.attrs['data-dashboard-field'], n.attrs['data-dashboard-out-of-date-label'] || ''])
);
"""


def _run_frame(read: dict[str, Any], names: list[str], start_ms: int, after: str) -> Any:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI builds the website, so node is present
        pytest.skip("node is needed to execute the frame bootstrap")
    program = "\n".join(
        (
            f"const ISLAND = {json.dumps(json.dumps(read))};",
            f"const NAMES = {json.dumps(names)};",
            f"const START = {start_ms};",
            _SHIM,
            dashboard_frame._BOOTSTRAP_JS,
            after,
        )
    )
    done = subprocess.run(
        [node, "--input-type=module"],
        input=program,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,  # flake-ok: a hang bound on one node run, not a wait for an event
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


class TestTheFrameMark:
    START = 1_900_000_000_000

    def _read(self, **over: Any) -> dict[str, Any]:
        return dashboard_frame.read_payload(
            {"verdict": "on track", "count": 3, "blockers": "none"},
            agentic=["verdict", "blockers"],
            **over,
        )

    def test_each_bound_element_past_its_time_is_marked_and_only_it(self) -> None:
        past = _iso(self.START / 1000 - 60)
        future = _iso(self.START / 1000 + 3600)
        got = _run_frame(
            self._read(stale_after={"verdict": past, "blockers": future}),
            ["verdict", "count", "blockers", "verdict"],
            self.START,
            "console.log(JSON.stringify("
            "[marks(), nodes.map(n => n.attrs['data-dashboard-out-of-date'] || '')]));",
        )
        labels, flags = got
        # No write time in this read: the label still says what happens next.
        assert labels["verdict"] == "may be out of date · the Bot is asked to update it"
        assert flags == ["true", "", "", "true"], "per element: both verdict nodes, nothing else"

    def test_the_label_says_how_old_the_value_is(self) -> None:
        got = _run_frame(
            self._read(
                stale_after={"verdict": _iso(self.START / 1000 - 60)},
                written_at={"verdict": _iso(self.START / 1000 - 2 * DAY)},
            ),
            ["verdict"],
            self.START,
            "console.log(JSON.stringify(marks()));",
        )
        assert got["verdict"] == "updated 2 days ago · the Bot is asked to update it"

    def test_a_field_turns_stale_on_the_minute_tick_with_no_push(self) -> None:
        soon = _iso(self.START / 1000 + 30)
        got = _run_frame(
            self._read(
                stale_after={"verdict": soon},
                written_at={"verdict": _iso(self.START / 1000 + 30 - DAY)},
            ),
            ["verdict"],
            self.START,
            "const before = marks().verdict; NOW += 60000; tick && tick();"
            "console.log(JSON.stringify([before, marks().verdict, tick === null]));",
        )
        # Marked on the tick, and the timer stays: the shown age keeps growing.
        assert got == ["", "updated yesterday · the Bot is asked to update it", False]

    def test_a_missing_cell_keeps_only_its_own_mark(self) -> None:
        past = _iso(self.START / 1000 - 60)
        got = _run_frame(
            self._read(stale_after={"verdict": past}, missing=["verdict"]),
            ["verdict"],
            self.START,
            "console.log(JSON.stringify(nodes[0].attrs));",
        )
        assert "data-dashboard-out-of-date" not in got
        assert got.get("data-dashboard-missing") == "true"

    def test_the_label_follows_the_reader_language(self) -> None:
        past = _iso(self.START / 1000 - 60)
        got = _run_frame(
            self._read(
                stale_after={"verdict": past},
                written_at={"verdict": _iso(self.START / 1000 - 2 * DAY)},
                locale="zh-CN",
            ),
            ["verdict"],
            self.START,
            "console.log(JSON.stringify(marks()));",
        )
        # The browser's own Intl words: zh says "the day before yesterday" for 2 days.
        assert got["verdict"] == "更新于前天 · 已提醒 Bot 更新"

    def test_preview_off_marks_nothing_and_starts_no_timer(self) -> None:
        """No ``stale_after`` (preview off): no mark and no timer -- and the same page
        with the map does start the timer, so this is not a harness that sees none."""
        script = "console.log(JSON.stringify([marks(), tick === null]));"
        off = _run_frame(self._read(), ["verdict", "blockers"], self.START, script)
        assert off == [{"verdict": "", "blockers": ""}, True]
        later = _iso(self.START / 1000 + 3600)
        on = _run_frame(self._read(stale_after={"verdict": later}), ["verdict"], self.START, script)
        assert on == [{"verdict": ""}, False], "a mark still due keeps the minute timer"


class TestThePageServedWithoutScript:
    """A page served with no script (an egress allowlist withholds scripts) is filled on
    the server, so the server draws the same label the bootstrap would."""

    PAGE = '<p data-dashboard-field="verdict"></p><p data-dashboard-field="blockers"></p>'

    def _fill(self, **over: Any) -> str:
        read = dashboard_frame.read_payload(
            {"verdict": "On track", "blockers": "none"}, agentic=["verdict", "blockers"], **over
        )
        return dashboard_frame.fill_bindings_as_text(self.PAGE, read)

    def test_a_field_past_its_time_is_labelled_in_the_static_fill(self) -> None:
        past, later = _iso(time.time() - 60), _iso(time.time() + 3600)
        html = self._fill(stale_after={"verdict": past, "blockers": later}, locale="zh-CN")
        assert html.count('data-dashboard-out-of-date="true"') == 1, html
        assert (
            '<p data-dashboard-field="verdict" data-dashboard-out-of-date="true" '
            'data-dashboard-out-of-date-label="可能已过期 · 已提醒 Bot 更新">On track</p>'
        ) in html

    def test_no_map_or_a_missing_field_gets_no_label(self) -> None:
        past = _iso(time.time() - 60)
        assert "out-of-date" not in self._fill()
        assert "out-of-date" not in self._fill(stale_after={"verdict": past}, missing=["verdict"])


class TestTheLivePackagePage:
    """A package page's frames are built on the fold worker, which does no I/O: the
    preview is read once when the page arms, and every frame after carries the map."""

    def _live(self, frames: list[Any]) -> Any:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        package = dp.validate_package(
            _package({"lane": {"type": "text", "source": {"agentic": True}}})
        )
        return mdf.LivePage("stale-live", "stale-live", package, frames.append)

    def _write_lane(self, at: str) -> None:
        from kiro_crew.crew_log import bus as crew_log_bus

        crew_log_bus.publish(
            crew_log_bus.FOLD_ADVANCED,
            crew_log_bus.FoldAdvanced(
                scope=crew_log_bus.SCOPE_SLOT,
                key="stale-live",
                fold="agentic",
                revision=3_000_001,
                seq=1,
                value={"fields": {"lane": {"value": "green", "type": "string", "at": at}}},
            ),
        )

    def test_frames_carry_stale_after_only_when_armed_with_the_preview(
        self, _floor_monkeypatch
    ) -> None:
        at = _iso(time.time() - 2 * DAY)
        for on in (False, True):
            _preview(_floor_monkeypatch, on)
            frames: list[Any] = []
            live = self._live(frames)
            first = live.arm()
            try:
                assert ("stale_after" in first) is on, first
                # Turned off after arming: the frames keep what the page armed with,
                # because the fold worker never reads config.
                _preview(_floor_monkeypatch, not on)
                self._write_lane(at)
                pushed = [f for f in frames if "lane" in f.get("fields", {})]
                assert pushed, frames
                if on:
                    assert pushed[-1]["stale_after"] == {"lane": _iso(_stamp(at) + DAY)}
                else:
                    assert "stale_after" not in pushed[-1]
            finally:
                live.disarm()


def _stamp(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


class TestTheFieldsRead:
    def test_rows_say_how_long_each_value_stays_current_only_with_the_preview(
        self, _floor_monkeypatch
    ) -> None:
        m = _manifest({"verdict": _agentic(stale_after_s=3600)})
        inst = dashboard_agentic.Instance(manifest=m, instance_version=1)
        written = {"verdict": _iso(time.time() - 7200)}
        _preview(_floor_monkeypatch, False)
        off = dashboard_agentic.fields_for_agent(inst, values={"verdict": "x"}, written_at=written)
        assert "stale" not in off["fields"][0]
        _preview(_floor_monkeypatch, True)
        on = dashboard_agentic.fields_for_agent(inst, values={"verdict": "x"}, written_at=written)
        row = on["fields"][0]
        assert row["stale"] is True and row["stale_after_s"] == 3600 and "stale_after" in row
