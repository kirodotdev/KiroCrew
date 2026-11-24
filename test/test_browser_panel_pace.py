"""A panel's pace reads what the link carries, with pictures in flight (D117 M97, D123).

The link is modelled, with no socket and no clock: a round ``P`` (send to decoded
ack with no bytes on it) and a rate ``B``; one picture on the wire at a time; a
tab that decodes one picture at a time; the stream's own rules for pictures in
flight. The real ``Flight`` and ``PanelController`` are fed what ``PanelStream``
feeds them.
"""

from __future__ import annotations

import pytest

from kiro_crew.browser_harness.panel_pace import (
    PANEL_BYTES_PER_S,
    PANEL_FLIGHT_BYTES,
    PANEL_HOLD_TICKS,
    PANEL_IN_FLIGHT,
    PANEL_QUIET_TICKS,
    PANEL_STEP_BETWEEN,
    Flight,
    PanelController,
    in_flight,
    link_time,
)
from kiro_crew.browser_harness.screencast import FPS_STEPS, UP_HEADROOM

FLOOR_30 = FPS_STEPS.index(30)


def run(
    *,
    round_ms: float,
    nbytes: int,
    rate: float = float("inf"),
    decode: float = 0.0,
    seconds: int = 30,
    jump=None,
):
    """Aims after each second, and pictures acked in each second."""
    c, flight = PanelController(fps_floor=FLOOR_30), Flight()
    p = round_ms / 1000
    acks: list[tuple[float, int]] = []
    seq, next_send, wire_free, tab_free = 0, 0.0, 0.0, 0.0
    aims: list[int] = []
    got = [0] * seconds
    for ms in range(seconds * 1000):
        t = ms / 1000
        for at, to_ms in [jump] if jump and not isinstance(jump, list) else jump or []:
            if t >= at:
                p = to_ms / 1000
        acks.sort()
        while acks and acks[0][0] <= t + 1e-9:
            at, s = acks.pop(0)
            _, own = flight.acked(s, at)
            if own is not None:
                c.record(*own)
            got[min(seconds - 1, int(at))] += 1
        if ms % 1000 == 999:
            c.tick()
            aims.append(c.aim())
        if t + 1e-9 >= next_send:
            on = len(flight.pictures)
            # ``PanelStream._released``: under the controller's count, and within the socket's bytes.
            if on == 0 or (on < c.flight_cap() and flight.bytes() + nbytes <= PANEL_FLIGHT_BYTES):
                seq += 1
                flight.sent(seq, t, nbytes, measured=True)
                wire_free = max(t, wire_free) + nbytes / rate
                tab_free = max(wire_free + p / 2, tab_free) + decode
                acks.append((tab_free + p / 2, seq))
                next_send = t + 1.0 / c.aim()
    return aims, got


def changes_in_any_20s(aims: list[int]) -> int:
    most = 0
    for i in range(len(aims)):
        w = aims[i : i + 20]
        most = max(most, sum(1 for j in range(1, len(w)) if w[j] != w[j - 1]))
    return most


def longest_over(aims: list[int], carries: float) -> int:
    """The longest run of seconds with the aim over what is carried (the bottom step cannot go lower)."""
    run_, longest = 0, 0
    for a in aims:
        run_ = run_ + 1 if (a > FPS_STEPS[-1] and a > carries * 1.05) else 0
        longest = max(longest, run_)
    return longest


@pytest.mark.parametrize(("round_ms", "aim", "least"), [(94, 30, 29), (150, 24, 23.5)])
def test_a_far_round_on_a_link_with_room_gets_twenty_four_or_more(round_ms, aim, least):
    for nbytes in (30_000, 100_000):
        aims, got = run(round_ms=round_ms, nbytes=nbytes)
        assert set(aims[-10:]) == {aim}
        assert sum(got[-10:]) / 10 >= least
        assert min(got[-10:]) >= 22


def test_the_panels_own_step_of_24_is_climbed_to_from_20_and_left_for_30():
    # A round of 300 ms holds the aim at 10; at 150 ms four in flight give 26.7: the step of 24, not 20.
    aims, got = run(round_ms=300, nbytes=100_000, jump=(10, 150), seconds=50)
    assert aims[8] == 10
    assert set(aims[-15:]) == {PANEL_STEP_BETWEEN} and sum(got[-10:]) / 10 >= 23.5
    # The link gets near: the ladder's own 30.
    aims, _ = run(round_ms=150, nbytes=100_000, jump=(15, 60), seconds=40)
    assert aims[12] == PANEL_STEP_BETWEEN and set(aims[-10:]) == {30}


def test_the_step_of_24_stands_on_a_round_that_gives_25():
    # A link of 150 ms is a round of 160 ms with the tab's decode in it: four in flight give 25.
    aims, got = run(round_ms=300, nbytes=100_000, jump=(3, 160), seconds=60)
    assert set(aims[-25:]) == {PANEL_STEP_BETWEEN}
    assert sum(got[-10:]) / 10 >= 23.5 and min(got[-10:]) >= 22
    # A round that gives under 24 does not hold it: 20.
    aims, _ = run(round_ms=300, nbytes=100_000, jump=(3, 180), seconds=60)
    assert set(aims[-25:]) == {20}


def test_a_second_climb_waits_until_the_aim_stood_for_twenty_seconds():
    # The link gets better twice: at 12 s (the step of 24) and at 40 s (the ladder's 30).
    aims, _ = run(round_ms=300, nbytes=100_000, jump=[(12, 150), (40, 60)], seconds=80)
    # The aim came down in the stream's first two seconds; the first climb waits for twenty quiet ones.
    assert set(aims[2:12]) == {10}
    first = next(i for i, a in enumerate(aims) if i > 11 and a == PANEL_STEP_BETWEEN)
    assert first >= PANEL_QUIET_TICKS - 1
    second = next(i for i, a in enumerate(aims) if i > first and a == 30)
    assert second - first >= PANEL_QUIET_TICKS - 1
    assert changes_in_any_20s(aims[5:]) <= 2


def test_the_step_of_24_is_paced_as_a_longer_period_on_the_ladders_30():
    c = PanelController(fps_floor=FLOOR_30)
    assert c.aim() == 30 and c.period_floor() == 0.0
    c._last = (100_000.0, 100_000 * 26.7 * UP_HEADROOM)
    c.fps_step = FPS_STEPS.index(20)
    c.wake()
    assert c.aim() == PANEL_STEP_BETWEEN and FPS_STEPS[c.fps_step] == 30
    assert c.period_floor() == 1 / PANEL_STEP_BETWEEN
    # 260 kB pictures: 24 a second would be over the bytes a panel may take (6,240 kB), so it stays at 20.
    c = PanelController(fps_floor=FLOOR_30)
    c._last = (260_000.0, 260_000 * 26.7 * UP_HEADROOM)
    c.fps_step = FPS_STEPS.index(20)
    c.wake()
    assert c.aim() == 20 and c.period_floor() == 0.0


def test_a_near_round_still_reaches_the_panels_top():
    aims, _ = run(round_ms=20, nbytes=100_000)
    assert set(aims[-10:]) == {30}


def test_how_many_go_at_once_follows_their_size():
    assert PANEL_IN_FLIGHT == 4
    assert [in_flight(n) for n in (10_000, 160_000, 160_001, 213_000, 320_000, 320_001)] == [
        4,
        4,
        3,
        3,
        2,
        1,
    ]
    assert in_flight(0) == 4 and in_flight(PANEL_FLIGHT_BYTES * 3) == 1
    # A picture of 200 kB went alone and got 10 a second at 94 ms; three at a time it gets 30 (6,000 kB a second).
    aims, got = run(round_ms=94, nbytes=200_000)
    assert set(aims[-10:]) == {30} and sum(got[-10:]) / 10 >= 29


def test_a_thin_link_that_carries_a_step_is_aimed_at_it_and_no_further():
    rate = 300_000
    for nbytes in (10_000, 15_000, 30_000):
        aims, got = run(round_ms=94, nbytes=nbytes, rate=rate, seconds=60)
        carries = rate / nbytes
        # After the first seconds the aim is over what the link carries for 2 s at most at a time.
        assert longest_over(aims[5:], carries) <= 2, (nbytes, aims)
        assert sum(got[-10:]) * nbytes / 10 <= rate
    # A picture the link cannot carry five times a second: the lowest step, as before.
    aims, _ = run(round_ms=94, nbytes=100_000, rate=rate)
    assert set(aims) == {FPS_STEPS[-1]}


@pytest.mark.parametrize(
    ("case", "carries"),
    [
        # The reviewer's two (REVIEW-48 Part 1): a link and a decode that carry a rate between two steps.
        (dict(round_ms=50, nbytes=45_000, rate=300_000), 300 / 45),
        (dict(round_ms=20, nbytes=100_000, decode=0.06), 1 / 0.06),
        # PAGES-1: four in flight on a link of 1,000 kB a second.
        (dict(round_ms=94, nbytes=100_000, rate=1_000_000), 10),
    ],
)
def test_the_aim_is_steady_where_the_link_carries_less_than_a_step(case, carries):
    aims, got = run(seconds=90, **case)
    # At most two changes of the aim in any 20 s, and never over what is carried for more than 2 s.
    assert changes_in_any_20s(aims[5:]) <= 2, aims
    assert longest_over(aims[5:], carries) <= 2, aims
    # It stands on the fastest step that is carried.
    best = max(f for f in FPS_STEPS if f <= carries * 1.05)
    assert max(set(aims[10:]), key=aims[10:].count) == best
    assert sum(got[-30:]) / 30 >= best - 0.3


def test_a_failed_climb_is_tried_again_only_after_its_hold_and_then_twice_as_late():
    aims, _ = run(round_ms=20, nbytes=100_000, decode=0.06, seconds=150)
    climbs = [i for i in range(11, len(aims)) if aims[i] > aims[i - 1]]
    assert len(climbs) >= 2
    gaps = [b - a for a, b in zip(climbs, climbs[1:])]
    assert gaps[0] >= PANEL_HOLD_TICKS
    assert all(later >= earlier for earlier, later in zip(gaps, gaps[1:]))


def test_one_picture_goes_at_a_time_for_the_second_after_a_failed_climb():
    c = PanelController(fps_floor=FLOOR_30)
    assert c.flight_cap() == PANEL_IN_FLIGHT
    c.fps_step = FPS_STEPS.index(15)
    c._note_climb(FPS_STEPS.index(10))  # a climb from 10 to 15
    # A second of pictures that stood in line: 100 kB each, 0.1 s behind the one before.
    for _ in range(5):
        c.record(100_000, 0.6, 0.1)
    c.tick()
    assert FPS_STEPS[c.fps_step] == 10 and c.flight_cap() == 1
    c.tick()
    assert c.flight_cap() == PANEL_IN_FLIGHT


def test_two_pictures_go_at_a_time_on_the_lowest_step():
    # The aim cannot go under 5: on a link that carries less, more in flight only makes each picture older.
    c = PanelController(fps_floor=FLOOR_30)
    c.fps_step = len(FPS_STEPS) - 1
    assert c.flight_cap() == 2
    aims, got = run(round_ms=150, nbytes=100_000, rate=300_000)
    assert set(aims[5:]) == {5} and sum(got[-10:]) / 10 <= 3.1


def test_a_panel_never_asks_for_more_bytes_a_second_than_its_bound():
    assert PANEL_BYTES_PER_S == 6_000_000
    # 200 kB pictures on a near link with room: 30 a second are 6,000 kB a second, the bound itself.
    aims, got = run(round_ms=20, nbytes=200_000)
    assert set(aims[-10:]) == {30}
    assert sum(got[-10:]) / 10 * 200_000 <= PANEL_BYTES_PER_S
    # 270 kB pictures: 30 a second would be 8,100 kB; two go at a time, and 20 a second are 5,400 kB.
    aims, got = run(round_ms=20, nbytes=270_000)
    assert set(aims[-10:]) == {20}
    assert sum(got[-10:]) / 10 * 270_000 <= PANEL_BYTES_PER_S
    # A hand on the page wakes the pace to the fastest step the link carries: still under the bound.
    c = PanelController(fps_floor=FLOOR_30)
    c._last = (270_000.0, 50_000_000.0)
    c.fps_step = FPS_STEPS.index(5)
    c.wake()
    assert FPS_STEPS[c.fps_step] == 20


def test_a_round_that_grows_brings_the_aim_down_within_two_seconds():
    # A round of 600 ms gives under 7 a second with four in flight.
    aims, _ = run(round_ms=30, nbytes=100_000, jump=(15, 600))
    assert aims[13] == 30
    assert max(aims[15:17]) <= 10


def test_the_link_time_is_the_wait_in_line_or_the_round_shared_by_the_pictures_in_flight():
    # A link with room: no wait in line, so the round shared by those in flight, less the headroom.
    assert link_time(100_000, 0.094, 0.094, 0.094) == 0.094 / (PANEL_IN_FLIGHT * UP_HEADROOM)
    # A full link: the picture stood in line for its own time on the wire.
    assert link_time(100_000, 0.6, 0.33, 0.25) == 0.33
    # A picture too big for a second one in flight goes alone.
    big = PANEL_FLIGHT_BYTES // 2 + 1
    assert link_time(big, 0.094, 0.094, 0.094) == 0.094 / UP_HEADROOM


def test_a_picture_no_ack_came_for_counts_its_whole_delivery():
    c = PanelController(fps_floor=FLOOR_30)
    c.record(100_000, 2.0)
    assert c.window.delivery_s == 2.0
