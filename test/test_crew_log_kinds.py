"""The layering: what is the envelope, what belongs to one kind, who may write it.

``test_crew_log_core.py`` covers the format. This file covers the SPLIT -- the
matrix of which ``src`` each kind accepts, the guest rules that authorization
hangs off ``src``, and the two crew contracts' envelope half. It is a matrix
rather than a set of examples because the hole it closes is a shared source list
that let one kind's emitter write into the other's file: a gap in a matrix is
visible, a missing example is not.

The `data` half of the crew contracts -- ``crew/dispatch`` requiring ``target``,
``crew/report`` requiring ``ref`` -- is per-type validation, which lives in the
type registry rather than here. Until that module exists, those requirements are
spec text (``docs/system-specs/modules/crew-log-core.md`` section 4b) and this file
asserts only what the envelope itself can hold.
"""

from __future__ import annotations

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import Ledger, LedgerError, Ref
from kiro_crew.crew_log.schema import check_ownership, require_src

CREW = "qa"
SESSION = "s-7f3a"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _crew(unit_id: str = CREW) -> Ledger:
    return Ledger.create(lg.KIND_CREW, unit_id)


def _session(unit_id: str = SESSION) -> Ledger:
    return Ledger.create(lg.KIND_SESSION, unit_id, owner=CREW, agent="kirocrew")


def _code(excinfo) -> str:
    return excinfo.value.code


# --- the src matrix, both kinds, every documented form ---------------------

#: Every ``src`` spelling the format knows, against both kinds. ``None`` means
#: the kind refuses it with ``bad_src``.
SRC_MATRIX: tuple[tuple[str, bool, bool], ...] = (
    # src,             crew accepts, session accepts
    ("gateway", True, True),
    ("acp", False, True),
    ("dashboard", True, False),
    ("patrol", True, False),
    ("crew:qa", True, False),
    ("app:radar", True, False),
    ("session:s-7f3a", False, False),
)


@pytest.mark.parametrize(("src", "crew_ok", "session_ok"), SRC_MATRIX)
def test_each_kind_accepts_only_its_own_emitters(src, crew_ok, session_ok):
    for kind, accepted in ((lg.KIND_CREW, crew_ok), (lg.KIND_SESSION, session_ok)):
        if accepted:
            assert require_src(src, kind=kind) == src
        else:
            with pytest.raises(LedgerError) as exc:
                require_src(src, kind=kind)
            assert _code(exc) == lg.CODE_BAD_SRC
            assert exc.value.field == "src"


def test_the_matrix_covers_every_documented_source():
    """The matrix is the rule's whole surface, so a new source cannot skip it."""
    listed = {src for src, _crew_ok, _session_ok in SRC_MATRIX}
    assert lg.FIXED_SOURCES <= listed
    for prefixes in lg.KIND_SOURCE_PREFIXES.values():
        for prefix in prefixes:
            assert any(src.startswith(prefix) for src in listed), prefix


def test_a_patrol_cannot_write_into_a_session_log():
    # The concrete hole a shared source list leaves open: a patrol has nothing
    # to say inside one session's own turn history, and src is what a reader
    # attributes an entry to.
    session = _session()
    with pytest.raises(LedgerError) as exc:
        session.append("turn/started", {"turn": 1}, src="patrol")
    assert _code(exc) == lg.CODE_BAD_SRC


def test_an_acp_runtime_cannot_write_into_a_crew_ledger():
    crew = _crew()
    with pytest.raises(LedgerError) as exc:
        crew.append("activity/tick", {}, src="acp")
    assert _code(exc) == lg.CODE_BAD_SRC


def test_require_src_demands_the_kind_that_selects_the_rule():
    """``kind`` is keyword-only and required, so no caller gets a default rule."""
    with pytest.raises(TypeError):
        require_src("gateway")  # type: ignore[call-arg]


def test_an_unknown_kind_is_refused_before_any_source_list_is_indexed():
    with pytest.raises(LedgerError) as exc:
        require_src("gateway", kind="swarm")
    assert _code(exc) == lg.CODE_BAD_KIND


def test_the_session_emitters_own_sources_are_all_accepted():
    """No behaviour change for the writes that exist: the emitter's own constants."""
    from kiro_crew.crew_log import emit

    for src in (emit._SRC_ACP, emit._SRC_GATEWAY):
        assert require_src(src, kind=lg.KIND_SESSION) == src


# --- guest authorization hangs off src ------------------------------------


def test_a_crew_guest_writes_the_crew_kinds_built_in_domains():
    crew = _crew()
    for entry_type in ("item/phase", "activity/record", "crew/finding"):
        assert crew.append(entry_type, {}, src="crew:qa").src == "crew:qa"


def test_a_crew_guest_is_refused_by_a_session_log():
    session = _session()
    with pytest.raises(LedgerError) as exc:
        session.append("turn/started", {"turn": 1}, src="crew:qa")
    assert _code(exc) == lg.CODE_BAD_SRC


def test_an_app_guest_is_confined_to_its_own_type_namespace():
    crew = _crew()
    assert crew.append("app:radar/scan", {}, src="app:radar").type == "app:radar/scan"
    for borrowed in ("member/joined", "crew/report", "app:other/scan"):
        with pytest.raises(LedgerError) as exc:
            crew.append(borrowed, {}, src="app:radar")
        assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION


def test_an_app_guest_is_refused_by_a_session_log():
    session = _session()
    with pytest.raises(LedgerError) as exc:
        session.append("app:radar/scan", {}, src="app:radar")
    assert _code(exc) == lg.CODE_BAD_SRC


def test_a_type_never_carries_the_writers_identity():
    """``crew:<name>/<action>`` is malformed: the writer is named by ``src``."""
    for spelled in ("crew:qa/report", "crew:qa/dispatch"):
        with pytest.raises(LedgerError) as exc:
            check_ownership(lg.KIND_CREW, spelled, "crew:qa")
        assert _code(exc) == lg.CODE_BAD_TYPE
        assert exc.value.field == "type"


def test_one_report_type_serves_every_child():
    # The point of moving identity into src: two children write the same type
    # into one parent crew log, and the entries are told apart by src.
    crew = _crew()
    evidence = Ref(lg.KIND_SESSION, SESSION, 1)
    first = crew.append(
        "crew/report", {"item": "i-1", "status": "done"}, src="crew:qa", ref=evidence
    )
    second = crew.append(
        "crew/report", {"item": "i-1", "status": "blocked"}, src="crew:docs", ref=evidence
    )
    assert first.type == second.type == "crew/report"
    assert {first.src, second.src} == {"crew:qa", "crew:docs"}


# --- the crew contracts: the envelope half --------------------------------


def test_a_report_may_cite_a_session_segment_which_is_the_one_cross_kind_bridge():
    crew = _crew()
    dispatch = crew.append(
        "crew/dispatch",
        {"item": "pr-4127", "target": {"kind": "session", "slot": "dashboard:3"}},
        src="gateway",
    )
    report = crew.append(
        "crew/report",
        {"item": "pr-4127", "status": "done", "credits": 0.21},
        src="crew:qa",
        thread=dispatch.seq,
        ref=Ref(lg.KIND_SESSION, SESSION, 40, 96),
    )
    assert report.thread == dispatch.seq
    assert report.ref is not None and report.ref.unit == lg.KIND_SESSION


def test_a_session_log_may_cite_a_crew_segment_too():
    """``ref`` is kind-independent: it is the envelope's, not one kind's."""
    _crew()
    session = _session()
    entry = session.append(
        "turn/started",
        {"turn": 1, "actor": "user", "depth": 0},
        src="gateway",
        ref=Ref(lg.KIND_CREW, CREW, 1),
    )
    assert entry.ref is not None and entry.ref.unit == lg.KIND_CREW


def test_both_dispatch_types_are_owned_by_the_crew_kind_and_by_no_other():
    for entry_type in ("crew/dispatch", "crew/report"):
        check_ownership(lg.KIND_CREW, entry_type, "gateway")
        with pytest.raises(LedgerError) as exc:
            check_ownership(lg.KIND_SESSION, entry_type, "gateway")
        assert _code(exc) == lg.CODE_EVENT_TYPE_NOT_OWNED
