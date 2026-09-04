"""Tests for the auto-triage-pipeline read-only fold.

Every test builds its OWN fixtures under tmp_path. Nothing here reads real data
under ~/.kirocrew: a fold test that depended on the live trail would pass or fail
on today's pipeline state, not on the fold's logic, so it would not be a test.

The assertions below are written to pin the REASON each guard exists, not merely a
happy path -- the docstrings in the module make specific factual claims (churn is
not skipped, distinct is not the event count, a brace pair inside a string must
not cause a bad cut, a retried item's spend lives across several slots) and each
of those claims gets a test that would fail if the claim stopped holding.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.apps.builtins.issue_radar.backend import pipeline_fold as fold
from kiro_crew.apps.builtins.issue_radar.backend import pipeline_routes as routes

# --------------------------------------------------------------------------
# Fixtures: everything resolves inside tmp_path.
# --------------------------------------------------------------------------


@pytest.fixture()
def tmp_root(tmp_path: Path) -> Path:
    """The workspace root that holds the audit log and dispatch queue."""
    return tmp_path


@pytest.fixture()
def wire_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Pin the three externally-resolved sources into tmp_path.

    ``root=`` covers the audit log and queue for L0/L1, but the issue cache and
    usage shards are resolved through imported helpers with no root parameter, so
    those two are redirected here. If any of these escaped tmp_path the test would
    be reading the developer's real machine.
    """
    cache_dir = tmp_path / "issue-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    usage_dir = tmp_path / "usage-tokens"
    usage_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(fold, "_issue_cache_dir", lambda owner, repo: cache_dir)
    monkeypatch.setattr(fold, "_usage_dir", lambda: usage_dir)
    # Also pin the workspace resolver so a code path that forgot to thread root=
    # cannot silently read the real workspace.
    monkeypatch.setattr(fold, "_workspace", lambda: tmp_path)
    return {"cache": cache_dir, "usage": usage_dir, "root": tmp_path}


#: The repository these tests exercise when the identity is not what they are
#: about. Spelled here rather than leaning on a production default, because the
#: fold deliberately has none: a reader that cannot name a repository gets a
#: TypeError instead of somebody else's shard.
TEST_REPO = "o/r"


def _write_log(root: Path, lines: list, repo: str | None = TEST_REPO) -> None:
    """Write the audit log. A str line is written verbatim (for torn/glued
    cases); a dict is serialized as one JSON object per line.

    A dict line gets ``repo`` stamped unless it already names one, because the
    production writers stamp it on EVERY event and a scoped fold admits only events
    that name the repository asked for. An unstamped fixture would therefore
    exercise a trail that no longer occurs, and would report empty counters for a
    reason unrelated to what these tests assert. Pass ``repo=None`` to build a
    deliberately pre-stamp line, which is what the exclusion tests do.
    """
    out = []
    for line in lines:
        if isinstance(line, str):
            out.append(line)
            continue
        if repo is not None and "repo" not in line:
            line = {**line, "repo": repo}
        out.append(json.dumps(line))
    fold.audit_log_path(root).write_text("\n".join(out) + "\n", encoding="utf-8")


def _write_queue(root: Path, entries: list[dict], repo: str = TEST_REPO) -> None:
    """Write one repository's queue shard."""
    fold.queue_path(repo=repo, root=root).write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def _write_usage(usage_dir: Path, day: str, rows: list[dict]) -> None:
    """Write usage shard rows, stamped as token records unless a row says otherwise.

    Real shard rows carry ``_type: "tokens"`` -- every reader in the owning usage
    module gates on it -- and the shard is a MIXED log. Fixtures that omitted the
    discriminator were describing a record shape that does not occur, which is why
    they kept passing while the fold was missing the filter. A row may still set
    ``_type`` explicitly to exercise the non-token case.
    """
    usage_dir.joinpath(f"{day}.jsonl").write_text(
        "\n".join(json.dumps({"_type": "tokens", **r}) for r in rows) + "\n",
        encoding="utf-8",
    )


def _step(fold_result: fold.PipelineFold, key: str) -> fold.StepCounts:
    for s in fold_result.steps:
        if s.key == key:
            return s
    raise AssertionError(f"no step {key}")


# --------------------------------------------------------------------------
# 1. Per-step entered/done/skipped/churn, churn counted separately from skipped.
# --------------------------------------------------------------------------


def test_churn_is_counted_separately_from_skipped(tmp_root: Path) -> None:
    """A review round is PROGRESS inside verify, not a decline. The module keeps
    churn and skipped in different buckets because "a gate ran" and "an item was
    declined" are opposite facts; conflating them makes a working step look like
    a rejecting one. verify has churn events but an empty skipped set, so churn
    must land in churn and leave skipped at zero."""
    _write_log(
        tmp_root,
        [
            {"event": "pr_opened", "issue": 1, "pr": 10},
            {"event": "review_round", "issue": 1},
            {"event": "review_round_fixed", "issue": 1},
            {"event": "push", "issue": 1},
            {"event": "gates_green", "issue": 1},
        ],
    )
    result = fold.fold_pipeline(root=tmp_root, repo=TEST_REPO)
    verify = _step(result, "verify")
    assert verify.entered == 1  # pr_opened admitted the item
    assert verify.churn == 4  # review_round, review_round_fixed, push, gates_green
    assert verify.skipped == 0  # none of those are declines
    assert verify.done == 0  # no pr_green yet


def test_entered_done_skipped_basic(tmp_root: Path) -> None:
    """Scan: scan=entered, label=done, dedupe=skipped. Pins that each event name
    routes to the bucket the step table assigns it."""
    _write_log(
        tmp_root,
        [
            {"event": "scan", "issue": 1},
            {"event": "scan", "issue": 2},
            {"event": "label", "issue": 1},
            {"event": "dedupe", "issue": 3},
        ],
    )
    scan = _step(fold.fold_pipeline(root=tmp_root, repo=TEST_REPO), "scan")
    assert scan.entered == 2
    assert scan.done == 1
    assert scan.skipped == 1
    assert scan.churn == 0


# --------------------------------------------------------------------------
# 2. in_flight = distinct entered minus observed-leaving.
# --------------------------------------------------------------------------


def test_in_flight_is_entered_minus_left(tmp_root: Path) -> None:
    """Three items enter scan; one gets a label (done => left), one a dedupe
    (skipped => left). Only the third has no observed exit, so in_flight is 1."""
    _write_log(
        tmp_root,
        [
            {"event": "scan", "issue": 1},
            {"event": "scan", "issue": 2},
            {"event": "scan", "issue": 3},
            {"event": "label", "issue": 1},
            {"event": "dedupe", "issue": 2},
        ],
    )
    scan = _step(fold.fold_pipeline(root=tmp_root, repo=TEST_REPO), "scan")
    assert scan.distinct_entered == 3
    assert scan.in_flight == 1  # only item 3 has not left


# --------------------------------------------------------------------------
# 3. distinct vs event counts under rework.
# --------------------------------------------------------------------------


def test_distinct_entered_differs_from_event_count_under_rework(tmp_root: Path) -> None:
    """One item enters implement TWICE (start, then resume after a dead slot).
    The event count is WORK PERFORMED (2 starts = 2 units), the distinct count is
    THROUGHPUT (still 1 item). The module keeps them separate precisely so rework
    is neither hidden nor double-counted; assert 2 vs 1."""
    _write_log(
        tmp_root,
        [
            {"event": "implement_start", "issue": 7},
            {"event": "implement_resume", "issue": 7},
        ],
    )
    impl = _step(fold.fold_pipeline(root=tmp_root, repo=TEST_REPO), "implement")
    assert impl.entered == 2  # two entry events
    assert impl.distinct_entered == 1  # one distinct item


# --------------------------------------------------------------------------
# 4. Field-based outcome discriminator on triage.
# --------------------------------------------------------------------------


def test_triage_classification_discriminates_in_flight_vs_exit(tmp_root: Path) -> None:
    """Triage logs ONE `triage` event per item and records the routing in
    details.classification. `auto-fixable` continues into dispatch (stays in
    flight); any other value hands the item to a human (an EXIT). Written to FAIL
    if the discriminator is ignored: if the field were not read, both items would
    look entered-and-never-left and in_flight would be 2, so asserting in_flight
    == 1 breaks the moment the classification stops being consulted."""
    _write_log(
        tmp_root,
        [
            {"event": "triage", "issue": 1, "details": {"classification": "auto-fixable"}},
            {"event": "triage", "issue": 2, "details": {"classification": "needs-human"}},
        ],
    )
    triage = _step(fold.fold_pipeline(root=tmp_root, repo=TEST_REPO), "triage")
    assert triage.entered == 2  # both logged a triage event
    assert triage.in_flight == 1  # only the auto-fixable one is still in flight
    routed = {r["outcome"]: r["count"] for r in triage.to_dict()["routed"]}
    assert routed == {"auto-fixable": 1, "needs-human": 1}


# --------------------------------------------------------------------------
# 5. Glued records.
# --------------------------------------------------------------------------


def test_glued_records_both_recovered(tmp_root: Path) -> None:
    """A writer that omits its trailing newline concatenates two well-formed
    objects as `}{` on one line. Both are real state transitions; dropping the
    line loses one. Assert both are folded (scan entered=2)."""
    glued = json.dumps({"event": "scan", "issue": 1, "repo": TEST_REPO}) + json.dumps(
        {"event": "scan", "issue": 2, "repo": TEST_REPO}
    )
    _write_log(tmp_root, [glued])
    scan = _step(fold.fold_pipeline(root=tmp_root, repo=TEST_REPO), "scan")
    assert scan.entered == 2
    assert fold.fold_pipeline(root=tmp_root, repo=TEST_REPO).unparseable == 0


def test_glued_pair_with_brace_pair_inside_string_still_splits(tmp_root: Path) -> None:
    """The nastier case raw_decode exists for: the FIRST object contains the
    literal `}{` INSIDE a string value. A naive split on the `}{` text would cut
    in the middle of that string and corrupt both halves. raw_decode tracks where
    each value actually ends, so the cut lands between objects. Assert both
    recovered and the title round-trips intact through the fold's L1 view."""
    obj1 = {"event": "pr_opened", "issue": 11, "pr": 21, "title": "fix }{ crash", "repo": TEST_REPO}
    obj2 = {"event": "scan", "issue": 12, "repo": TEST_REPO}
    glued = json.dumps(obj1) + json.dumps(obj2)
    _write_log(tmp_root, [glued])
    result = fold.fold_pipeline(root=tmp_root, repo=TEST_REPO)
    assert result.unparseable == 0
    assert _step(result, "verify").entered == 1  # pr_opened for issue 11
    assert _step(result, "scan").entered == 1  # scan for issue 12
    # And the string that contained `}{` survived undamaged: split at the wrong
    # place would have produced invalid JSON and lost the record entirely.
    pieces, complete = fold._split_glued(glued)
    assert complete is True
    assert len(pieces) == 2
    assert json.loads(pieces[0])["title"] == "fix }{ crash"


# --------------------------------------------------------------------------
# 6. A malformed line increments unparseable and does not abort.
# --------------------------------------------------------------------------


def test_malformed_line_counts_unparseable_and_fold_continues(tmp_root: Path) -> None:
    """A torn tail is the expected state of a log another process is appending
    to. A genuinely broken line must bump `unparseable` and NOT abort: the good
    events on either side of it must still be folded."""
    _write_log(
        tmp_root,
        [
            {"event": "scan", "issue": 1},
            "{ this is not json at all ",
            {"event": "scan", "issue": 2},
        ],
    )
    result = fold.fold_pipeline(root=tmp_root, repo=TEST_REPO)
    assert result.unparseable == 1
    assert _step(result, "scan").entered == 2  # both good scans survived


# --------------------------------------------------------------------------
# 7. Hostile item numbers.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "12a",  # non-decimal
        "-5",  # negative (str form; strip/isdecimal reject the sign)
        "1" * (fold.MAX_ITEM_DIGITS + 1),  # more digits than the ceiling
        True,  # bool is an int subclass but not an item number
        "\u2460",  # circled one: isdigit True, isdecimal False, int() raises
        "",  # empty
        None,  # absent
        0,  # zero is not a valid item number
        -5,  # negative int
    ],
)
def test_hostile_item_numbers_refused_without_raising(raw) -> None:
    """_bounded_int must reject every hostile shape by returning None, never by
    raising -- it guards a value that arrives from an external log. The unicode
    digit case is the pointed one: the module uses isdecimal not isdigit
    specifically because "\u2460".isdigit() is True while int("\u2460") raises,
    so isdigit would pass the guard and blow up in the conversion it protects."""
    assert fold._bounded_int(raw) is None


def test_unicode_digit_is_decimal_distinction_holds() -> None:
    """Pin the exact property the module's docstring relies on, so a future edit
    that swaps isdecimal->isdigit is caught here even if _bounded_int changed."""
    assert "\u2460".isdigit() is True
    assert "\u2460".isdecimal() is False


def test_bounded_int_accepts_a_valid_number() -> None:
    """The refusals above are only meaningful if the happy path works."""
    assert fold._bounded_int("5327") == 5327
    assert fold._bounded_int(42) == 42


# --------------------------------------------------------------------------
# 8. Control chars / ANSI escapes rendered inert by _printable.
# --------------------------------------------------------------------------


def test_printable_neutralizes_control_and_ansi(tmp_root: Path) -> None:
    """Event names and titles reach a terminal and a browser, so a control byte or
    an ANSI escape must not survive. Each unsafe character is REPLACED, which
    neutralizes it without escaping the rest of the string."""
    ansi = "\x1b[31mred\x1b[0m\r\n"
    rendered = fold._printable(ansi)
    assert "\x1b" not in rendered  # no live escape byte
    assert "\r" not in rendered and "\n" not in rendered
    assert "\ufffd" in rendered  # the unsafe characters were replaced, not dropped
    assert "red" in rendered  # and the readable part survived


def test_printable_keeps_legitimate_non_latin_text(tmp_root: Path) -> None:
    """Non-ASCII is NOT a threat and must not be mangled.

    An earlier version used ``ascii()``, which neutralizes escapes and also turns
    every non-Latin character into a ``\\uXXXX`` sequence -- so an operator could not
    read the title of their own issue. Pinned because the safe-looking fix (escape
    everything) is the one that shipped.
    """
    for text in (
        "\u4e2d\u6587\u6807\u9898",
        "\u0440\u0443\u0441\u0441\u043a\u0438\u0439",
        "caf\u00e9",
        "\u65e5\u672c\u8a9e",
    ):
        rendered = fold._printable(text)
        assert rendered == text, f"mangled legitimate text: {text!r} -> {rendered!r}"


def test_printable_neutralizes_bidi_overrides(tmp_root: Path) -> None:
    """A bidi override can visually reorder a line, so it is unsafe even though it
    is neither a control byte nor an escape."""
    rendered = fold._printable("safe\u202ereversed")
    assert "\u202e" not in rendered
    assert "safe" in rendered


def test_printable_control_chars_inert_in_unmapped_event(tmp_root: Path) -> None:
    """An unknown event carrying control chars is surfaced in unmappedEvents; its
    name must be neutralized before it lands there, or the raw escape reaches the
    operator's screen."""
    _write_log(tmp_root, [{"event": "weird\x1b[2Jevent", "issue": 1}])
    result = fold.fold_pipeline(root=tmp_root, repo=TEST_REPO)
    names = [u["event"] for u in result.to_dict()["unmappedEvents"]]
    assert any("\ufffd" in n for n in names)
    assert all("\x1b" not in n for n in names)


# --------------------------------------------------------------------------
# 9. Oversized log raises FoldError with no absolute path.
# --------------------------------------------------------------------------


def test_oversized_log_raises_folderror_without_absolute_path(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log past MAX_LOG_BYTES is refused rather than read into memory. The
    error message must NOT leak the absolute path: it is surfaced to an HTTP
    client, and a path discloses the host's directory layout. Shrink the ceiling
    instead of writing 64MB so the test is cheap."""
    monkeypatch.setattr(fold, "MAX_LOG_BYTES", 32)
    _write_log(tmp_root, [{"event": "scan", "issue": 1, "pad": "x" * 200}])
    with pytest.raises(fold.FoldError) as exc:
        fold.fold_pipeline(root=tmp_root, repo=TEST_REPO)
    message = str(exc.value)
    assert str(tmp_root) not in message  # no absolute path leaked
    assert str(fold.audit_log_path(tmp_root)) not in message
    assert "ceiling" in message  # it is the size guard that fired


# --------------------------------------------------------------------------
# 10. PR extraction: top-level pr, details.pr, /pull/<n> URL; None for prose.
# --------------------------------------------------------------------------


def test_pr_extraction_top_level() -> None:
    assert fold._extract_pr({"event": "pr_opened", "pr": 5600}) == 5600


def test_pr_extraction_details_pr() -> None:
    assert fold._extract_pr({"event": "cleanup", "details": {"pr": 4857}}) == 4857


def test_pr_extraction_pull_url_anywhere() -> None:
    rec = {"event": "pr_green", "details": {"url": "https://github.com/o/KiroCrew/pull/5600"}}
    assert fold._extract_pr(rec) == 5600


def test_pr_extraction_rejects_free_prose() -> None:
    """Free prose like 'PR 5327 head abc' must yield None. This is deliberate:
    the same prose field also says things like 'rebased over PR 5191', so a loose
    number-grab would produce a confidently WRONG link. Only a structured field
    or a real /pull/ URL is trusted. Assert None for both prose shapes."""
    assert fold._extract_pr({"event": "note", "details": {"text": "PR 5327 head abc"}}) is None
    assert fold._extract_pr({"event": "note", "details": {"text": "rebased over PR 5191"}}) is None


# --------------------------------------------------------------------------
# 11. L2 sums usage across current slot AND every previous_slots key.
# --------------------------------------------------------------------------


def test_l2_sums_current_and_previous_slots(
    wire_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried item's spend lives across several sessions: the dispatcher
    retires a dead slot into previous_slots and opens a new one. Reporting only
    the current slot under-counts exactly the retried (expensive) items -- the
    real trail showed a 21x gap. Fixture: current slot small, previous slot large;
    assert BOTH sessions appear and both totals are present."""
    root = wire_sources["root"]
    _write_queue(
        root,
        [{"issue": 42, "slot": "slot-current", "previous_slots": ["slot-old"]}],
    )
    _write_usage(
        wire_sources["usage"],
        "2026-08-24",
        [
            {"slot": "slot-current", "credits": 187, "cost": 1.0},
            {"slot": "slot-old", "credits": 4059, "cost": 20.0},
        ],
    )
    sessions = fold.list_item_sessions(42, repo=TEST_REPO, root=root)
    by_slot = {s.slot: s for s in sessions}
    assert set(by_slot) == {"slot-current", "slot-old"}
    assert by_slot["slot-current"].credits == 187
    assert by_slot["slot-old"].credits == 4059  # the large previous slot IS summed
    assert by_slot["slot-current"].current is True
    assert by_slot["slot-old"].current is False


# --------------------------------------------------------------------------
# 12. A string/null/NaN/inf usage field must not poison a sum.
# --------------------------------------------------------------------------


def test_bad_usage_fields_do_not_poison_sum(wire_sources: dict[str, Path]) -> None:
    """The total is shown as money and turns; one row with a NaN would make the
    whole column unreadable. _number coerces string/null/NaN/inf to 0.0 so the
    good rows still sum. Fixture mixes a clean 10 with a string, null, NaN and
    inf, and asserts the sum is exactly 10 (finite, not NaN)."""
    root = wire_sources["root"]
    _write_queue(root, [{"issue": 1, "slot": "s1", "previous_slots": []}])
    # NaN/inf cannot be written by json.dumps with allow_nan=False, but the live
    # writer uses default json which DOES emit NaN/Infinity, and json.loads reads
    # them back -- so this is the real on-disk shape.
    lines = [
        json.dumps({"_type": "tokens", "slot": "s1", "credits": 10.0}),
        json.dumps({"_type": "tokens", "slot": "s1", "credits": "not-a-number"}),
        json.dumps({"_type": "tokens", "slot": "s1", "credits": None}),
        '{"_type": "tokens", "slot": "s1", "credits": NaN}',
        '{"_type": "tokens", "slot": "s1", "credits": Infinity}',
    ]
    wire_sources["usage"].joinpath("2026-08-24.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    sessions = fold.list_item_sessions(1, repo=TEST_REPO, root=root)
    assert len(sessions) == 1
    credits = sessions[0].credits
    assert credits == 10.0  # only the clean row contributed
    assert credits == credits  # not NaN


def test_non_token_records_do_not_pollute_sums(wire_sources: dict[str, Path]) -> None:
    """The usage shard is a MIXED log, and any record carrying a `slot` field would
    otherwise add its numbers to that session's totals. Every reader in the owning
    usage module gates on `_type == "tokens"` at each of its read sites; this fold
    omitted the gate. Credits is the headline figure of the L2 table, so a polluted
    sum is the wrong answer to the one question the table exists to answer. Fixture:
    one token row worth 10 plus a non-token row worth 999 on the same slot."""
    root = wire_sources["root"]
    _write_queue(root, [{"issue": 7, "slot": "s7", "previous_slots": []}])
    lines = [
        json.dumps({"_type": "tokens", "slot": "s7", "credits": 10.0, "turns": 1}),
        json.dumps({"_type": "something_else", "slot": "s7", "credits": 999.0, "turns": 50}),
    ]
    wire_sources["usage"].joinpath("2026-08-24.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    sessions = fold.list_item_sessions(7, repo=TEST_REPO, root=root)
    assert len(sessions) == 1
    assert sessions[0].credits == 10.0  # the non-token 999 is NOT counted
    # `rows` is the honest turn count (one row per turn), so the ignored record must
    # not inflate it either.
    assert sessions[0].rows == 1


def test_scalar_cached_author_is_kept(wire_sources: dict[str, Path]) -> None:
    """`author` is cached as a scalar login, not a list. `_names` returned [] for a
    bare string -- while its own docstring claimed to accept a scalar -- so a cached
    author rendered as "Not cached": our own defect displayed as missing data, the
    same shape as reading the cache's wrapper instead of its `detail`."""
    assert fold._names("octocat") == ["octocat"]
    assert fold._names("") == []  # an empty login is still absent
    # The list and dict shapes keep working.
    assert fold._names(["a", {"login": "b"}]) == ["a", "b"]
    assert fold._names({"login": "solo"}) == ["solo"]
    assert fold._names(None) == []
    assert fold._names(42) == []


def test_absent_comment_count_is_null_not_zero(wire_sources: dict[str, Path]) -> None:
    """ "We never cached this issue" and "this issue has no comments" are different
    facts, and only one of them is ours to assert. The neighbouring labels/assignees
    already render as "Not cached"; comments defaulted to 0 and so claimed an
    uncached issue had no discussion. A present, genuine zero must still survive."""
    assert fold._comment_count(None) is None
    assert fold._comment_count("7") is None  # a string is not an answer
    assert fold._comment_count(True) is None  # bool is not a count
    assert fold._comment_count(0) == 0  # a real zero SURVIVES
    assert fold._comment_count(4) == 4
    assert fold._comment_count(["a", "b", "c"]) == 3


def test_usage_beyond_the_old_thirty_shard_cap_is_still_summed(
    wire_sources: dict[str, Path],
) -> None:
    """The L2 total is billed as the spend summed ACROSS RETRIES, and an item can be
    reworked for weeks. Reading only the 30 newest shards silently turned that into
    "spend in the recent window", under-reporting exactly the long-running items whose
    cost matters most -- and the window's own comment justified itself with a
    retention claim a real installation contradicted (37 shards on disk).

    The fixture must EXCEED the old cap or it proves nothing: 40 shards, with the
    spend on the OLDEST one, so a 30-newest read drops it."""
    root = wire_sources["root"]
    _write_queue(root, [{"issue": 9, "slot": "s9", "previous_slots": []}])
    # 40 daily shards, newest 2026-08-24 backwards. Only the oldest carries credits,
    # so the assertion below fails for any read that truncates the tail.
    day = date(2026, 8, 24)
    for i in range(40):
        shard_day = (day - timedelta(days=i)).isoformat()
        rows = [{"slot": "s9", "credits": 100.0}] if i == 39 else [{"slot": "s9", "credits": 0.0}]
        _write_usage(wire_sources["usage"], shard_day, rows)
    sessions = fold.list_item_sessions(9, repo=TEST_REPO, root=root)
    assert len(sessions) == 1
    assert sessions[0].credits == 100.0  # the 40th-newest shard IS counted


def test_deeply_nested_record_does_not_crash_the_fold() -> None:
    """CPython's JSON decoder recurses per nesting level, so a deeply nested value
    raises RecursionError, not a JSON error. `_iter_jsonl` guarded its own
    `json.loads` but called `_split_glued` OUTSIDE that guard, so a RecursionError
    from the recovery path escaped and turned one hostile line into an HTTP 500 --
    on a log this walk is built to tolerate line by line.

    Driven through `_iter_jsonl`, the real entry point, rather than the helper: the
    escape only existed because of how the two are wired together."""
    deep = "[" * 20000 + "]" * 20000
    # Glued so the recovery path is the one that runs, which is where it escaped.
    line = '{"event": "scan", "issue": 1}' + deep
    out = list(fold._iter_jsonl(line + "\n"))
    # It must REPORT the bad line rather than raise.
    assert out, "the walk yielded nothing instead of reporting the line"
    assert any(ok is False for _rec, ok in out)


def test_unreadable_shard_raises_rather_than_under_reporting(
    wire_sources: dict[str, Path],
) -> None:
    """An oversized shard used to be skipped, which dropped that day's rows from a
    figure presented as the LIFETIME total -- the operator saw a smaller number with
    nothing saying it was partial. That is the same defect as the removed 30-shard
    window arriving by another route, and this feature refuses the trade everywhere
    else. Fixture: a good shard plus one past the byte ceiling; the call must FAIL
    rather than return the good shard's total alone."""
    root = wire_sources["root"]
    _write_queue(root, [{"issue": 11, "slot": "s11", "previous_slots": []}])
    _write_usage(wire_sources["usage"], "2026-08-24", [{"slot": "s11", "credits": 7.0}])
    # One shard deliberately past the read ceiling.
    big = wire_sources["usage"] / "2026-08-23.jsonl"
    big.write_text("x" * (fold.MAX_LOG_BYTES + 1024), encoding="utf-8")
    with pytest.raises(fold.FoldError):
        fold.list_item_sessions(11, repo=TEST_REPO, root=root)


def test_printable_redacts_credentials_but_keeps_readable_titles() -> None:
    """Issue titles are NOT our text -- anyone can open an issue on the forge and
    write anything in the title, and this fold hands them to a route that renders
    them in the dashboard. That makes this a dashboard-bound sink for
    attacker-controlled text, owing the same redaction every other such sink in this
    codebase applies. It was skipping the shared helper entirely.

    The other half of the contract still has to hold: redaction must not undo the
    reason `ascii()` was removed, so a Chinese or Cyrillic title stays readable."""
    secret = "ghp_" + "A" * 36
    out = fold._printable(f"crash when token {secret} is set", 200)
    assert secret not in out, "a credential-shaped token reached dashboard-bound text"

    # Legitimate non-Latin titles survive intact -- the threat is control and bidi
    # characters and credentials, not non-Latin scripts.
    for title in (
        "\u4fee\u590d\u5d29\u6e83",
        "\u0438\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c",
    ):
        assert fold._printable(title, 200) == title

    # Control and bidi-override characters are still neutralized.
    assert "\x1b" not in fold._printable("a\x1b[31mred", 200)
    assert "\u202e" not in fold._printable("a\u202eb", 200)


def test_reaped_item_leaves_the_working_step(wire_sources: dict[str, Path]) -> None:
    """A cleanup enter has to EXIT the working step, not merely enter the cleanup one.

    Without that, an item that started implementing and was then reaped had no
    transition out of Implement: it stayed in that step's in-flight count while the
    cleanup step also counted it, so a retired item sat in the live backlog and was
    double-counted across two steps. Measured on the real trail before the fix: 6 of
    the 27 items Implement reported as in-flight had a cleanup as their last event.

    Asserted for dispatch and verify too. Both measured zero reaped items on the real
    trail, so this is the guard that keeps the sibling gaps from reappearing -- fixing
    only the step the finding named is what would invite the identical finding next
    round."""
    root = wire_sources["root"]
    events = [
        # queued but reaped before starting -> must not sit in dispatch
        {"event": "implement_queued", "issue": 1},
        {"event": "cleanup", "issue": 1},
        # started then reaped -> must not sit in implement
        {"event": "implement_queued", "issue": 2},
        {"event": "implement_start", "issue": 2},
        {"event": "cleanup", "issue": 2},
        # PR opened then reaped before green -> must not sit in verify
        {"event": "implement_queued", "issue": 3},
        {"event": "implement_start", "issue": 3},
        {"event": "pr_opened", "issue": 3, "pr": 99},
        {"event": "cleanup_sweep", "issue": 3},
    ]
    _write_log(root, events)
    fold_result = fold.fold_pipeline(root=root, repo=TEST_REPO)
    assert _step(fold_result, "dispatch").in_flight == 0
    assert _step(fold_result, "implement").in_flight == 0
    assert _step(fold_result, "verify").in_flight == 0
    # They ARE in cleanup -- the point is one step at a time, not none.
    assert _step(fold_result, "cleanup").in_flight == 3


# --------------------------------------------------------------------------
# 13. populated_columns names only columns non-zero somewhere.
# --------------------------------------------------------------------------


def test_populated_columns_omits_all_zero_column() -> None:
    """A column that is structurally zero on every row must be omitted, or the
    table prints a row of zeros next to a real credit total and invites the
    reader to think the work was free. Build rows where credits is non-zero but
    cost is zero everywhere; assert credits is named and cost is not."""
    rows = [
        fold.SessionRow(slot="a", credits=100.0, cost=0.0),
        fold.SessionRow(slot="b", credits=50.0, cost=0.0),
    ]
    cols = fold.populated_columns(rows)
    assert "credits" in cols
    assert "cost" not in cols  # all-zero column omitted


def test_populated_columns_includes_column_nonzero_on_one_row() -> None:
    """Non-zero SOMEWHERE (not everywhere) is the rule: one row carrying cost is
    enough to keep the column."""
    rows = [
        fold.SessionRow(slot="a", credits=100.0, cost=0.0),
        fold.SessionRow(slot="b", credits=50.0, cost=3.5),
    ]
    assert "cost" in fold.populated_columns(rows)


# --------------------------------------------------------------------------
# 14. list_item_sessions for an item with no queue entry returns [].
# --------------------------------------------------------------------------


def test_item_with_no_queue_entry_returns_empty(wire_sources: dict[str, Path]) -> None:
    """An item that never opened a session (early batch steps open none) has no
    queue entry. That is a real answer -- empty list -- not an error. Assert [] and
    that it does not raise."""
    root = wire_sources["root"]
    _write_queue(root, [{"issue": 99, "slot": "s", "previous_slots": []}])
    assert fold.list_item_sessions(1, repo=TEST_REPO, root=root) == []


# --------------------------------------------------------------------------
# 15. list_step_items with an unknown step raises FoldError.
# --------------------------------------------------------------------------


def test_unknown_step_raises_folderror(wire_sources: dict[str, Path]) -> None:
    """An unknown step key is a caller error, and the fold refuses it with
    FoldError rather than returning a silently-empty list that would look like a
    real but idle step."""
    root = wire_sources["root"]
    _write_log(root, [{"event": "scan", "issue": 1}])
    with pytest.raises(fold.FoldError):
        fold.list_step_items("does-not-exist", owner="o", repo="r", root=root)


def test_known_step_lists_resident_item(wire_sources: dict[str, Path]) -> None:
    """The unknown-step refusal is only meaningful if a known step returns rows.
    An item that entered scan and has not left is resident; its title comes from
    the issue cache we wired into tmp_path.

    The event carries its repository because the production writers stamp one on
    every event and a scoped read admits only matching events -- an unstamped line
    would return no rows for a reason this test is not about."""
    root = wire_sources["root"]
    _write_log(root, [{"event": "scan", "issue": 3, "repo": "o/r"}])
    wire_sources["cache"].joinpath("issue-3.json").write_text(
        json.dumps({"title": "a real title", "author": {"login": "octocat"}}),
        encoding="utf-8",
    )
    rows = fold.list_step_items("scan", owner="o", repo="r", root=root)
    assert [r.number for r in rows] == [3]
    assert rows[0].title == "a real title"
    assert rows[0].author == "octocat"


# --------------------------------------------------------------------------
# 16. No write path: register_routes mounts only GET/HEAD.
# --------------------------------------------------------------------------


def test_register_routes_mounts_only_read_methods() -> None:
    """The app is a window, never a hand on the pipeline. aiohttp's add_get also
    registers HEAD; NOTHING else may be mounted. Assert every registered method
    is GET or HEAD and there are exactly three GET routes -- a POST/PATCH/DELETE
    slipping in would be a write path the whole design forbids."""
    from aiohttp import web

    app = web.Application()
    routes.register_routes(app)
    methods = sorted({r.method for r in app.router.routes()})
    assert set(methods) <= {"GET", "HEAD"}
    get_paths = sorted(r.resource.canonical for r in app.router.routes() if r.method == "GET")
    assert get_paths == [
        f"{routes.PREFIX}/item/sessions",
        f"{routes.PREFIX}/overview",
        f"{routes.PREFIX}/step",
    ]
    assert "POST" not in methods and "PUT" not in methods and "DELETE" not in methods


# ── the repository dimension ──────────────────────────────────────────────────
# The scheduled jobs stamp `repo` on every event they write. Everything written
# before that does not carry one and cannot be back-filled, so the load-bearing
# rule is what happens to those: they belong to EVERY repository's fold, never to
# none. A reader that dropped them would delete the entire pre-stamp history from
# a filtered view, which reads as "this pipeline never ran".


def _mixed_trail(root: Path) -> None:
    """A trail shaped like the real one: pre-stamp events plus two repositories."""
    _write_log(
        root,
        [
            # pre-stamp: no `repo` key at all
            {"ts": "2026-08-01T00:00:00Z", "event": "triage", "issue": 1},
            {"ts": "2026-08-01T00:01:00Z", "event": "implement_queued", "issue": 1},
            # stamped, repository A
            {"ts": "2026-08-02T00:00:00Z", "event": "triage", "issue": 2, "repo": "acme/alpha"},
            {
                "ts": "2026-08-02T00:01:00Z",
                "event": "implement_queued",
                "issue": 2,
                "repo": "acme/alpha",
            },
            # stamped, repository B
            {"ts": "2026-08-03T00:00:00Z", "event": "triage", "issue": 3, "repo": "acme/beta"},
        ],
        repo=None,  # these fixtures carry pre-stamp rows ON PURPOSE
    )


def test_the_census_stays_whole_trail_on_a_SCOPED_fold(tmp_path: Path) -> None:
    """The census is disclosure, so narrowing the view must not narrow it.

    Read off a fold that IS narrowed, which is the form that proves the property:
    the census resisted the narrowing. Asserting it through a fold covering
    everything would only show that an unnarrowed call reports everything, and no
    such call exists -- the repository is required, because there is no honest
    default for which one a reader means.
    """
    _mixed_trail(tmp_path)
    result = fold.fold_pipeline(root=tmp_path, repo=TEST_REPO).to_dict()
    assert result["totalEvents"] == 5
    assert result["unattributedEvents"] == 2


def test_two_repositories_sharing_a_long_prefix_stay_DISTINCT(tmp_path: Path) -> None:
    """The census counts identity, so redaction must not decide what is one repository.

    `_printable` truncates at 120 characters. Using it as the dict KEY merged any two
    repositories whose names share a 120-character prefix into a single entry: their
    counts combined, the list collapsed to one row, and the view's "more than one
    repository" condition went false -- so a board mixing two repositories reported
    itself as unmixed. That is the disclosure this whole change exists to make,
    silently defeated by its own redaction.

    Counting on the raw value and redacting in `to_dict()` keeps both properties: the
    identity is exact and nothing unredacted reaches the dashboard. Note the two rows
    below RENDER identically after truncation -- which is honest, and unlike a merge it
    still tells the operator there are two.
    """
    shared = "acme/" + ("x" * 130)
    a, b = shared + "-alpha", shared + "-beta"
    assert a[:120] == b[:120], "fixture must actually collide under truncation"
    _write_log(
        tmp_path,
        [
            {"ts": "2026-08-02T00:00:00Z", "event": "triage", "issue": 1, "repo": a},
            {"ts": "2026-08-02T00:01:00Z", "event": "triage", "issue": 2, "repo": a},
            {"ts": "2026-08-02T00:02:00Z", "event": "triage", "issue": 3, "repo": b},
        ],
    )

    census = fold.fold_pipeline(root=tmp_path, repo=TEST_REPO).to_dict()["repos"]
    assert len(census) == 2, f"two repositories collapsed into {len(census)} row(s)"
    assert sorted(row["count"] for row in census) == [1, 2]
    # And nothing left unredacted: every emitted name is within the printable bound.
    assert all(len(row["repo"]) <= 120 for row in census)


def test_the_census_key_is_REDACTED(tmp_path: Path) -> None:
    """A census key reaches the dashboard, so it is redacted like every other string.

    The trail is written by agent-driven jobs, so a repository value is
    attacker-reachable text exactly as an issue title is. This field was the one
    string the fold handed its routes WITHOUT `_printable`, which also made the
    module's redaction-sink registration claim more than the code did.
    """
    secret = "acme/x-aws_secret_access_key=AKIAIOSFODNN7EXAMPLE"
    _write_log(
        tmp_path,
        [
            {"ts": "2026-08-02T00:00:00Z", "event": "triage", "issue": 7, "repo": secret},
            {"ts": "2026-08-02T00:01:00Z", "event": "triage", "issue": 8, "repo": "acme/alpha"},
        ],
    )

    census = fold.fold_pipeline(root=tmp_path, repo=TEST_REPO).to_dict()["repos"]
    keys = {row["repo"] for row in census}

    # The raw credential-shaped value never reaches the payload...
    assert secret not in keys
    assert not any("AKIAIOSFODNN7EXAMPLE" in k for k in keys)
    # ...and the clean sibling is untouched, so redaction is not blanket mangling.
    assert "acme/alpha" in keys


def test_the_repository_census_counts_every_repository_the_trail_names(tmp_path: Path) -> None:
    """`repos` answers "what is in here", which is disclosure rather than scoping.

    It is the measurement that makes the missing repository dimension visible: an
    operator can see the trail mixes two repositories even though no view can yet
    separate them.
    """
    _mixed_trail(tmp_path)
    census = fold.fold_pipeline(root=tmp_path, repo=TEST_REPO).to_dict()["repos"]
    assert {e["repo"]: e["count"] for e in census} == {"acme/alpha": 2, "acme/beta": 1}


def test_an_empty_or_non_string_repo_on_an_event_reads_as_unattributed(tmp_path: Path) -> None:
    """A malformed stamp degrades to "no repository", never to a census row.

    Treating `repo: ""` or `repo: 5` as a value would attribute the event to a
    repository named by junk, and a view rendering the census would grow a phantom
    row.
    """
    _write_log(
        tmp_path,
        [
            {"ts": "2026-08-02T00:00:00Z", "event": "triage", "issue": 1, "repo": ""},
            {"ts": "2026-08-02T00:01:00Z", "event": "triage", "issue": 2, "repo": 5},
            {"ts": "2026-08-02T00:02:00Z", "event": "triage", "issue": 3, "repo": None},
        ],
    )
    result = fold.fold_pipeline(root=tmp_path, repo=TEST_REPO).to_dict()
    assert result["unattributedEvents"] == 3
    assert result["repos"] == []


def test_l1_filters_items_by_repository(tmp_path: Path) -> None:
    """Filtering is only sound while the dispatch queue is sharded per repository.

    That condition is the whole reason this assertion is safe to make, so it is
    stated rather than assumed. With the queue one file keyed on the issue number
    alone, a filter narrows the ROWS without narrowing the JOINS: each surviving row
    is enriched from whichever repository's entry happens to occupy that number, so
    the view reads as scoped while showing another repository's metadata, sessions
    and costs. Worse than an unscoped list, because it looks precise.

    The queue is one file per repository, so the join is as narrow as the filter and
    the scope is real. Should it ever become a single shared file again, this test is
    the one that must change with it rather than be left quietly passing.
    """
    _write_log(
        tmp_path,
        [
            {"ts": "2026-08-02T00:00:00Z", "event": "triage", "issue": 11, "repo": "acme/alpha"},
            {"ts": "2026-08-02T00:01:00Z", "event": "triage", "issue": 22, "repo": "acme/beta"},
        ],
    )
    _write_queue(tmp_path, [], repo="acme/alpha")
    rows = fold.list_step_items("triage", owner="acme", repo="alpha", root=tmp_path)
    assert sorted(r.number for r in rows) == [11], "acme/beta's item leaked into acme/alpha"

    _write_queue(tmp_path, [], repo="acme/beta")
    other = fold.list_step_items("triage", owner="acme", repo="beta", root=tmp_path)
    assert sorted(r.number for r in other) == [22]

    # A repository the trail never names has no items of its own -- and picks up
    # no one else's.
    _write_queue(tmp_path, [], repo="nobody/nothing")
    none = fold.list_step_items("triage", owner="nobody", repo="nothing", root=tmp_path)
    assert [r.number for r in none] == []


def test_l1_excludes_a_pre_stamp_item_rather_than_dressing_it_in_another_repo(
    tmp_path: Path,
) -> None:
    """A pre-stamp item appears on no scoped listing, and that is not mere strictness.

    Admitting it would look like the humble reading -- the fold cannot tell whose it
    was, and losing history seems worse than showing it twice -- but admission is not
    where it ends. An admitted row is enriched by its issue NUMBER against the
    SELECTED repository's issue cache and queue shard, so the item renders with
    another repository's title, labels, assignees, slot and credit costs: the precise
    misattribution this scoping exists to remove, re-entering through the one door
    left open. Absent beats invented, so it is excluded and disclosed by
    ``unattributedEvents`` instead.

    The fixture makes the fabrication concrete: this repository really does own a
    #7, with a different title, so a regression does not merely show an extra row
    -- it shows that row wearing this repository's identity.
    """
    _write_log(
        tmp_path,
        [{"ts": "2026-08-01T00:00:00Z", "event": "triage", "issue": 7}],
        repo=None,  # the unstamped event IS the subject here
    )
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "issue-7.json").write_text(
        json.dumps({"title": "acme/alpha's own issue 7", "author": {"login": "someone"}}),
        encoding="utf-8",
    )
    with mock.patch.object(fold, "_issue_cache_dir", lambda owner, repo: cache):
        for owner, repo in (("acme", "alpha"), ("nobody", "nothing")):
            _write_queue(tmp_path, [], repo=f"{owner}/{repo}")
            rows = fold.list_step_items("triage", owner=owner, repo=repo, root=tmp_path)
            assert [
                r.number for r in rows
            ] == [], f"{owner}/{repo} showed an unattributable item as its own"

    # The counterpart, so this is EXCLUSION-WITH-DISCLOSURE rather than a silent drop.
    # There is no unscoped fold to compare against any more -- the repository is
    # required -- so the disclosure has to come off a scoped fold, which is exactly
    # where a reader needs it: the item is absent from the rows, and the census still
    # says one event could not be placed.
    scoped = fold.fold_pipeline(root=tmp_path, repo="acme/alpha")
    assert scoped.unattributed == 1
    triage = next(s for s in scoped.steps if s.key == "triage")
    assert triage.entered == 0, "an unattributable event is not this repository's throughput"


def test_the_census_still_discloses_what_the_scoped_view_dropped(tmp_path: Path) -> None:
    """Excluding is only honest if the view still ADMITS to the exclusion.

    Otherwise a scoped board reads as complete while silently omitting events, and
    a user comparing two boards could not tell that neither accounts for the
    pre-stamp history. ``unattributedEvents`` is written above the admission gate
    for exactly this reason, so it survives the narrowing that drops the rows.
    """
    _write_log(
        tmp_path,
        [
            {"ts": "2026-08-02T00:00:00Z", "event": "triage", "issue": 11, "repo": "acme/alpha"},
            {"ts": "2026-08-02T00:01:00Z", "event": "triage", "issue": 33},
            {"ts": "2026-08-02T00:02:00Z", "event": "triage", "issue": 34},
        ],
        repo=None,  # these fixtures carry pre-stamp rows ON PURPOSE
    )
    scoped = fold.fold_pipeline(repo="acme/alpha", root=tmp_path)
    triage = next(s for s in scoped.steps if s.key == "triage")
    assert triage.entered == 1, "the scoped counter must not include unattributable events"
    assert scoped.unattributed == 2, "but the view must still say how many it could not place"
    assert scoped.total_events == 3, "and the census keeps describing the whole trail"


def test_l0_narrows_the_step_counters_but_not_the_census(tmp_path: Path) -> None:
    """The two halves of a scoped fold, and why they differ.

    Step counters are the VIEW and must narrow: a board scoped to one repository
    that counted another's throughput would be lying about its own numbers.

    ``repos`` / ``unattributedEvents`` / ``totalEvents`` are DISCLOSURE and must
    NOT narrow: a scoped board still has to be able to say which repositories the
    trail contains and how much of it cannot be attributed. Narrowing the census
    would hide exactly the mixture the census exists to report -- and the view
    would then look clean because it had been filtered, which is the failure this
    whole dimension was added to avoid.
    """
    _write_log(
        tmp_path,
        [
            {"ts": "2026-08-02T00:00:00Z", "event": "triage", "issue": 11, "repo": "acme/alpha"},
            {"ts": "2026-08-02T00:01:00Z", "event": "triage", "issue": 12, "repo": "acme/alpha"},
            {"ts": "2026-08-02T00:02:00Z", "event": "triage", "issue": 22, "repo": "acme/beta"},
            {"ts": "2026-08-02T00:03:00Z", "event": "triage", "issue": 33},
        ],
        repo=None,  # these fixtures carry pre-stamp rows ON PURPOSE
    )
    alpha = fold.fold_pipeline(root=tmp_path, repo="acme/alpha").to_dict()
    beta = fold.fold_pipeline(root=tmp_path, repo="acme/beta").to_dict()

    def entered(payload: dict) -> int:
        return next(s["entered"] for s in payload["steps"] if s["key"] == "triage")

    # Each side counts only its own. The unattributed event is in NEITHER: a step
    # counter feeds a board whose rows get enriched from that repository, so an event
    # naming no repository cannot be presented as either one's throughput.
    assert entered(alpha) == 2
    assert entered(beta) == 1

    # And the census is IDENTICAL from both sides -- which is the sharper form of the
    # property. Compared against an unscoped fold it only showed that an unnarrowed
    # call reports everything; compared between two narrowed calls it shows the census
    # does not move when the view does, whichever repository you asked about.
    assert alpha["repos"] == beta["repos"]
    assert alpha["unattributedEvents"] == beta["unattributedEvents"] == 1
    assert alpha["totalEvents"] == beta["totalEvents"] == 4


def test_l2_reads_the_named_repositorys_queue_shard(tmp_path: Path) -> None:
    """The costliest misattribution the shard prevents.

    Issue numbers are per-repository, so two repositories can both have #500.
    Before the queue was sharded, this lookup keyed on the number alone and would
    return whichever entry happened to occupy it -- handing back another
    repository's slots, and therefore another repository's sessions and CREDIT
    COSTS, under the name of the item that was asked for.
    """
    _write_log(tmp_path, [{"ts": "2026-08-02T00:00:00Z", "event": "triage", "issue": 500}])
    _write_queue(tmp_path, [{"issue": 500, "slot": "chat-alpha"}], repo="acme/alpha")
    _write_queue(tmp_path, [{"issue": 500, "slot": "chat-beta"}], repo="acme/beta")

    alpha = fold.list_item_sessions(500, root=tmp_path, repo="acme/alpha")
    beta = fold.list_item_sessions(500, root=tmp_path, repo="acme/beta")
    assert [r.slot for r in alpha] == ["chat-alpha"]
    assert [r.slot for r in beta] == ["chat-beta"]


def test_the_shard_name_matches_what_the_writers_produce(tmp_path: Path) -> None:
    """A cross-repository contract with nothing else binding the two sides.

    The scheduled jobs that write this queue live in another repository and name
    the file with their own slug implementation. A drift here does not raise --
    the fold opens a path nothing ever wrote and reports an empty queue -- so the
    name is pinned as a literal on this side too.
    """
    got = fold.queue_path(repo="kirodotdev/KiroCrew", root=tmp_path).name
    assert got == "gh-autofix-dispatch-queue.kirodotdev__KiroCrew.jsonl"
    assert fold._repo_slug("Weird Name/Repo!") == "Weird-Name__Repo-"
    # ASCII-explicit, so it cannot diverge from the writers' shell-side slug on a
    # non-ASCII name the way str.isalnum() would.
    assert fold._repo_slug("café/naïve") == "caf-__na-ve"


def test_two_repositories_never_resolve_to_one_shard(tmp_path: Path) -> None:
    a = fold.queue_path(repo="acme/alpha", root=tmp_path)
    b = fold.queue_path(repo="other/alpha", root=tmp_path)
    assert a != b and a.name != b.name


def test_the_shard_path_stays_inside_its_root(tmp_path: Path) -> None:
    """A repository name is untrusted config; it must not escape the directory."""
    for hostile in ("../../etc/passwd", "a/../../b", "/absolute/evil", "..", "."):
        p = fold.queue_path(repo=hostile, root=tmp_path)
        assert p.parent == tmp_path, hostile
        assert p.resolve().parent == tmp_path.resolve(), hostile


# --------------------------------------------------------------------------
# The pre-sharding queue. Reading a shard that the deploy has not created yet
# is the one case where "no entries" is a false statement rather than a true one.
# --------------------------------------------------------------------------


def test_the_legacy_queue_name_matches_what_the_installer_renames(tmp_path: Path) -> None:
    """The other half of the cross-repository contract, pinned as a literal.

    The pipeline's installer renames THIS name to the shard. If the two sides
    disagreed, the guard below would never fire and the empty board it exists to
    prevent would come back silently -- the same failure mode as a drifted shard
    name, in the check meant to catch it.
    """
    assert fold.legacy_queue_path(tmp_path).name == "gh-autofix-dispatch-queue.jsonl"
    assert fold.legacy_queue_path(tmp_path).parent == tmp_path


def test_an_unmigrated_queue_is_refused_rather_than_read_as_empty(tmp_path: Path) -> None:
    """Entries under the old name must not surface as a repository with no work."""
    fold.legacy_queue_path(tmp_path).write_text(
        json.dumps({"issue": 42, "slot": "chat-1"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(fold.QueueMigrationPending):
        fold._read_queue(repo=TEST_REPO, root=tmp_path)


def test_the_refusal_is_not_reported_as_a_bad_request(tmp_path: Path) -> None:
    """It subclasses FoldError, but the routes must tell the two apart.

    A caller that catches only FoldError still gets the visible path; a caller
    that knows the difference can say the queue needs migrating instead of
    blaming the step or item number the user asked for.
    """
    assert issubclass(fold.QueueMigrationPending, fold.FoldError)


def test_a_present_shard_is_read_even_while_the_old_file_lingers(tmp_path: Path) -> None:
    """Post-migration truth wins.

    An installer that copied instead of renaming, or an operator who kept the old
    file, must not make a working board refuse. Only an ABSENT shard is ambiguous.
    """
    fold.legacy_queue_path(tmp_path).write_text(
        json.dumps({"issue": 42, "slot": "stale"}) + "\n", encoding="utf-8"
    )
    fold.queue_path(repo=TEST_REPO, root=tmp_path).write_text(
        json.dumps({"issue": 7, "slot": "current"}) + "\n", encoding="utf-8"
    )
    got = fold._read_queue(repo=TEST_REPO, root=tmp_path)
    assert set(got) == {7}, "the shard is the current truth, not the pre-sharding file"


def test_a_queue_that_never_existed_is_still_simply_empty(tmp_path: Path) -> None:
    """The guard must not turn a genuinely new repository into an error.

    With neither file present nothing has been queued yet, and an empty queue is
    the honest answer -- this is the common case on a freshly connected repository.
    """
    assert fold._read_queue(repo=TEST_REPO, root=tmp_path) == {}
