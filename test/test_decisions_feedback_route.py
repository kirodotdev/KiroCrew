"""The verdict route: who may call it, what a verdict may say, and that it APPENDS.

Three things are pinned, because each is a way a verdict route goes wrong.

Owner-only. The route WRITES the decision log, so an app token that reached it
could grow the file the operator reads and pollute the record with verdicts nobody
gave. Same gate as the consent pair in the same module.

Validated, not coerced. A row filed under a turn nobody can name, or carrying a
verdict outside the pair, makes every later count WRONG rather than incomplete --
so it is refused at the door.

Append-only, checked by reading the file. A verdict is a second event about a
turn, so two verdicts leave two rows with two timestamps and the decision row
they judge is byte-identical afterwards. That is what makes "when did they change
their mind" answerable, and it is asserted on the bytes rather than on the call.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers.decisions import api_decisions_feedback
from kiro_crew.decisions import log as log_mod


@pytest.fixture(autouse=True)
def _generous_append_deadline(monkeypatch):
    """Take the production append deadline off the critical path of these tests.

    The fixture rows here go through the real writer, whose 0.5s deadline exists to
    stop an observation occupying a caller on lock contention -- a bound worth
    keeping in production and worth not racing in a 24-worker suite, where losing
    it surfaces as an unexplained assertion two layers downstream.

    Raised, not removed: a genuinely stuck lock still fails, and pytest's own
    per-test timeout still bounds the run.
    """
    from kiro_crew import platform_log_append

    monkeypatch.setattr(platform_log_append, "_APPEND_TIMEOUT_SECONDS", 10.0)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the decision log directory into *tmp_path* and hand back the path."""
    directory = tmp_path / "decisions"
    monkeypatch.setattr(log_mod, "log_dir", lambda: directory)
    return directory


@pytest.fixture
def audit(monkeypatch):
    """Capture the SEL rows the handlers write."""
    import kiro_crew.dashboard.handlers as handlers_pkg

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: rows.append(kw)
    monkeypatch.setattr(handlers_pkg, "sel", lambda: fake)
    return rows


def _request(
    *, app: str = "", user: str = "owner-1", owner: str = "owner-1", body=None, query=None
):
    """A request shaped like a real DASHBOARD OWNER call (see test_decisions_consent.py)."""
    req = MagicMock()
    req.path = "/api/decisions/feedback"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.query = query or {}
    if isinstance(body, Exception):
        req.json = AsyncMock(side_effect=body)
    else:
        req.json = AsyncMock(return_value=body if body is not None else {})
    return req


def _body(**kw):
    base = {"turn_id": "turn-abc", "verdict": "right", "side": "jev"}
    base.update(kw)
    return base


def _rows(home):
    """Every row in today's day-file, in written order."""
    path = log_mod.log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestARefusedAppendIsNotASuccess:
    """The row IS the owner's answer, so a dropped one may not answer 200.

    Every other writer of this log is an observation and a dropped row is the
    contract. This one is a person's verdict, and the day-file ceiling is shared
    with the turn's own rows -- so on a busy sampled day the ceiling is reached by
    round traffic and a 200 would report a verdict that was never written.
    """

    @pytest.mark.asyncio
    async def test_a_full_day_file_answers_503_and_writes_nothing(self, home, audit, monkeypatch):
        monkeypatch.setattr(log_mod, "MAX_FILE_BYTES", 1)

        resp = await api_decisions_feedback(_request(body=_body()))

        assert resp.status == 503
        assert json.loads(resp.text)["code"] == "decisions_feedback_not_recorded"
        assert _rows(home) == [], "the refusal is real: nothing landed"

    @pytest.mark.asyncio
    async def test_the_refusal_audits_as_denied(self, home, audit, monkeypatch):
        """A refusal that audits as success is the same lie one layer down."""
        monkeypatch.setattr(log_mod, "MAX_FILE_BYTES", 1)

        await api_decisions_feedback(_request(body=_body()))

        assert audit, "the refusal must reach SEL"
        assert audit[-1]["outcome"] == "denied"
        assert audit[-1].get("error") == "not_recorded"

    @pytest.mark.asyncio
    async def test_an_unwritable_log_answers_503_rather_than_raising(
        self, home, audit, monkeypatch
    ):
        """A 503 is retryable, which is what the writer's own WARNING tells the owner."""
        monkeypatch.setattr(log_mod, "append", lambda row: False)

        resp = await api_decisions_feedback(_request(body=_body()))

        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_a_written_verdict_still_answers_200(self, home, audit):
        """The refusal path must not cost the ordinary one its success."""
        resp = await api_decisions_feedback(_request(body=_body()))

        assert resp.status == 200
        assert json.loads(resp.text) == {"ok": True}
        assert len(_rows(home)) == 1

    def test_the_writer_reports_whether_it_wrote(self, home, monkeypatch):
        """The return value the route reads, pinned at the writer itself."""
        assert log_mod.append({"ts": "t", "kind": "feedback"}) is True
        monkeypatch.setattr(log_mod, "MAX_FILE_BYTES", 1)
        assert log_mod.append({"ts": "t", "kind": "feedback"}) is False


class TestFeedbackAuth:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"app": "some-app"},
            {"user": "someone-else"},
            {"user": ""},
        ],
    )
    async def test_only_the_dashboard_owner_may_record_a_verdict(self, home, audit, kwargs):
        resp = await api_decisions_feedback(_request(body=_body(), **kwargs))

        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "dashboard_owner_required"
        assert _rows(home) == []

    @pytest.mark.asyncio
    async def test_a_refusal_is_audited_as_denied(self, home, audit):
        await api_decisions_feedback(_request(body=_body(), app="some-app"))

        assert [r["outcome"] for r in audit] == ["denied"]


class TestFeedbackSchema:
    @pytest.mark.asyncio
    async def test_a_valid_verdict_is_accepted(self, home, audit):
        resp = await api_decisions_feedback(_request(body=_body()))

        assert resp.status == 200
        assert json.loads(resp.text) == {"ok": True}

    @pytest.mark.asyncio
    async def test_a_cleared_verdict_is_a_real_value(self, home, audit):
        """``null`` means the person took their verdict back, which the log must say."""
        resp = await api_decisions_feedback(_request(body=_body(verdict=None)))

        assert resp.status == 200
        assert _rows(home)[0]["verdict"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"verdict": "right", "side": "jev"},  # no turn
            {"turn_id": "", "verdict": "right", "side": "jev"},
            {"turn_id": "   ", "verdict": "right", "side": "jev"},
            {"turn_id": 7, "verdict": "right", "side": "jev"},
            {"turn_id": "t", "verdict": "maybe", "side": "jev"},
            {"turn_id": "t", "verdict": True, "side": "jev"},
            {"turn_id": "t", "verdict": "right"},  # no side
            {"turn_id": "t", "verdict": "right", "side": "both"},
            {"turn_id": "t", "verdict": "right", "side": None},
            {"turn_id": "t", "side": "jev"},  # no verdict key at all
            [],
            "nope",
        ],
    )
    async def test_an_unusable_body_is_refused_and_writes_nothing(self, home, audit, body):
        resp = await api_decisions_feedback(_request(body=body))

        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "decisions_feedback_invalid_body"
        assert _rows(home) == []

    @pytest.mark.asyncio
    async def test_an_omitted_verdict_is_refused_where_a_written_null_is_taken(self, home, audit):
        """Absent and ``null`` differ by one key and must not mean the same thing.

        ``null`` is the retract, so reading an omitted key as one would append an
        event nobody sent off a body that merely lost the field. Both bodies are
        sent here, in one test, because the pair is the claim: only the written
        ``null`` reaches the log.
        """
        omitted = await api_decisions_feedback(
            _request(body={"turn_id": "turn-abc", "side": "jev"})
        )

        assert omitted.status == 400
        assert json.loads(omitted.text)["code"] == "decisions_feedback_invalid_body"
        assert _rows(home) == []

        written = await api_decisions_feedback(
            _request(body={"turn_id": "turn-abc", "verdict": None, "side": "jev"})
        )

        assert written.status == 200
        assert [row["verdict"] for row in _rows(home)] == [None]

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_refused(self, home, audit):
        resp = await api_decisions_feedback(_request(body=ValueError("bad json")))

        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_json"
        assert _rows(home) == []

    @pytest.mark.asyncio
    async def test_the_row_carries_exactly_the_five_fields_the_reader_folds(self, home, audit):
        await api_decisions_feedback(_request(body=_body(side="baseline", verdict="wrong")))

        row = _rows(home)[0]
        assert set(row) == {"ts", "kind", "turn_id", "verdict", "side"}
        assert row["kind"] == "feedback"
        assert row["turn_id"] == "turn-abc"
        assert row["verdict"] == "wrong"
        assert row["side"] == "baseline"

    @pytest.mark.asyncio
    async def test_an_overlong_turn_id_is_bounded(self, home, audit):
        """One malformed caller must not write an unbounded line into a shared file."""
        await api_decisions_feedback(_request(body=_body(turn_id="t" * 5000)))

        assert len(_rows(home)[0]["turn_id"]) == log_mod._MAX_TURN_ID_CHARS


class TestAppendOnly:
    @pytest.mark.asyncio
    async def test_a_changed_mind_leaves_two_rows_not_one_edit(self, home, audit):
        await api_decisions_feedback(_request(body=_body(verdict="right")))
        await api_decisions_feedback(_request(body=_body(verdict="wrong")))

        rows = _rows(home)
        assert [r["verdict"] for r in rows] == ["right", "wrong"]

    @pytest.mark.asyncio
    async def test_a_decision_row_already_in_the_file_is_untouched(self, home, audit):
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append(log_mod.build_row(point="skills.select", session_key="s", latency_ms=12))
        before = log_mod.log_path().read_bytes()

        await api_decisions_feedback(_request(body=_body()))

        after = log_mod.log_path().read_bytes()
        assert after.startswith(before)
        assert len(_rows(home)) == 2

    @pytest.mark.asyncio
    async def test_the_verdict_is_audited_with_its_side(self, home, audit):
        await api_decisions_feedback(_request(body=_body(side="baseline")))

        allowed = [r for r in audit if r["outcome"] == "allowed"]
        assert len(allowed) == 1
        assert "side=baseline" in allowed[0]["resources"]
