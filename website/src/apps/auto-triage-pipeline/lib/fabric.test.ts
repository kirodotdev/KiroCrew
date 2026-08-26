import { describe, it, expect } from 'vitest'
import type { CrewFabricItem, ConnectedRepo, RepoRef } from '../api'
import {
  SPINE_PHASES, EXIT_PHASES, spineIndex, isKnownPhase, phaseClass, tsMs,
  foldItem, columnOccupancy, dwellSeconds, formatDwell, laneDwells, openDwellSeconds,
  exitToken, queueSummary,
  laneTimelineRows, SLOW_DWELL_SECONDS, phaseCaption, phaseHeader, dashboardCells,
  EDITING_SLOT_CAP, selectRepo, repoOrderKey,
} from './fabric'

// A stable clock for the dwell assertions.
const T = (h: number, m = 0, s = 0) =>
  new Date(Date.UTC(2026, 0, 1, h, m, s)).toISOString()
const MS = (h: number, m = 0, s = 0) => Date.UTC(2026, 0, 1, h, m, s)

/** Minimal item builder — only the fields the fold reads. */
function item(partial: Partial<CrewFabricItem> & Pick<CrewFabricItem, 'phase' | 'timeline'>): CrewFabricItem {
  return {
    number: 1,
    crew_id: 'c_test',
    title: 'a work item',
    next: '',
    pr_number: null,
    exit: null,
    reopens: 0,
    ...partial,
  }
}

describe('phase geometry', () => {
  it('SPINE_PHASES holds only on-spine phases in lifecycle order, resolved last', () => {
    expect(SPINE_PHASES).toEqual([
      'selected', 'claimed', 'investigating', 'implementing',
      'awaiting-ci', 'addressing-review', 'awaiting-merge', 'resolved',
    ])
    // resolved is the ONLY terminal on the spine
    expect(SPINE_PHASES[SPINE_PHASES.length - 1]).toBe('resolved')
  })

  it('the four other terminals and awaiting-reply are EXITS, never columns', () => {
    for (const p of ['skipped', 'yielded', 'handed-back', 'preempted', 'awaiting-reply']) {
      expect(EXIT_PHASES[p]).toBe(true)
      expect(spineIndex(p)).toBe(-1)
    }
    // resolved is NOT an exit
    expect(EXIT_PHASES['resolved']).toBeUndefined()
  })

  it('spineIndex maps on-spine phases to ascending columns and off-spine to -1', () => {
    expect(spineIndex('selected')).toBe(0)
    expect(spineIndex('implementing')).toBe(3)
    expect(spineIndex('resolved')).toBe(7)
    expect(spineIndex('yielded')).toBe(-1)
    expect(spineIndex('not-a-phase')).toBe(-1)
  })

  it('isKnownPhase accepts enum members and rejects strangers', () => {
    expect(isKnownPhase('implementing')).toBe(true)
    expect(isKnownPhase('yielded')).toBe(true)
    expect(isKnownPhase('teleporting')).toBe(false)
  })

  it('phaseClass is keyed off the live phase', () => {
    expect(phaseClass('implementing')).toBe('edit')
    expect(phaseClass('addressing-review')).toBe('edit')
    expect(phaseClass('awaiting-ci')).toBe('wait')
    expect(phaseClass('claimed')).toBe('wait')
    expect(phaseClass('awaiting-reply')).toBe('reply')
    expect(phaseClass('yielded')).toBe('exit')
    expect(phaseClass('skipped')).toBe('exit')
    expect(phaseClass('resolved')).toBe('done')
  })

  it('exitToken returns the stable catalog key for each off-spine phase', () => {
    expect(exitToken('skipped')).toBe('exit_skipped')
    expect(exitToken('handed-back')).toBe('exit_handed_back')
    expect(exitToken('awaiting-reply')).toBe('exit_await_reply')
    // an unknown phase gets no key — the view falls back to the raw phase
    expect(exitToken('teleporting')).toBe('')
  })
})

describe('tsMs', () => {
  it('parses an ISO timestamp and is null-tolerant', () => {
    expect(tsMs(T(2))).toBe(MS(2))
    expect(tsMs(null)).toBeNull()
    expect(tsMs(undefined)).toBeNull()
    expect(tsMs('not a date')).toBeNull()
  })
})

describe('foldItem — plain forward run', () => {
  const lane = foldItem(item({
    number: 5109,
    pr_number: 5144,
    phase: 'awaiting-merge',
    timeline: [
      { phase: 'claimed', at: T(2, 34) },
      { phase: 'implementing', at: T(2, 35) },
      { phase: 'awaiting-ci', at: T(3, 8) },
      { phase: 'awaiting-merge', at: T(3, 48) },
    ],
  }))

  it('reaches each on-spine column once, ascending', () => {
    expect(lane.reach).toEqual([1, 3, 4, 6]) // claimed, implementing, awaiting-ci, awaiting-merge
  })

  it('puts the head at the LIVE phase', () => {
    expect(lane.head).toBe(spineIndex('awaiting-merge')) // 6
    expect(lane.cls).toBe('wait')
  })

  it('records no loops, no reopens, no exit', () => {
    expect(lane.loops).toEqual([])
    expect(lane.reopens).toBe(0)
    expect(lane.exit).toBeNull()
  })

  it('carries the PR number and prev column', () => {
    expect(lane.prNumber).toBe(5144)
    expect(lane.prevIdx).toBe(spineIndex('awaiting-ci')) // 4 — the step before the head
  })
})

describe('foldItem — title and next are distinct fields', () => {
  it('carries the real title through, and next under its OWN name', () => {
    const lane = foldItem(item({
      phase: 'claimed',
      title: 'pr_status rollup degrades to red on a torn cache',
      next: 'add the Windows branch to _safe_chmod',
      timeline: [{ phase: 'claimed', at: T(2, 34) }],
    }))
    expect(lane.title).toBe('pr_status rollup degrades to red on a torn cache')
    // next is the crew's resumable INTENT — never conflated with the title.
    expect(lane.next).toBe('add the Windows branch to _safe_chmod')
  })

  it('an empty title (number the caches never saw) does NOT fall back to next', () => {
    const lane = foldItem(item({
      phase: 'claimed',
      title: '',
      next: 'not a title',
      timeline: [{ phase: 'claimed', at: T(2, 34) }],
    }))
    expect(lane.title).toBe('')
    expect(lane.next).toBe('not a title')
  })

  it('a payload omitting next folds to an empty next, not undefined', () => {
    const lane = foldItem({
      number: 7, crew_id: 'c_x', title: 'x', pr_number: null,
      exit: null, reopens: 0, phase: 'claimed', timeline: [{ phase: 'claimed', at: T(2, 34) }],
    } as unknown as CrewFabricItem)
    expect(lane.next).toBe('')
  })
})

describe('foldItem — review round-trip (decision 3: head is live, not max index)', () => {
  // implementing → awaiting-ci → addressing-review → awaiting-ci.
  // The FURTHEST column reached is addressing-review (5), but the item RE-ENTERED
  // awaiting-ci, so the head must be awaiting-ci (4) and a loop must be recorded
  // from addressing-review (5) back to awaiting-ci (4).
  const lane = foldItem(item({
    phase: 'awaiting-ci',
    timeline: [
      { phase: 'claimed', at: T(1) },
      { phase: 'implementing', at: T(2) },
      { phase: 'awaiting-ci', at: T(3) },
      { phase: 'addressing-review', at: T(4) },
      { phase: 'awaiting-ci', at: T(5) },
    ],
  }))

  it('reaches every distinct column including the one past the head', () => {
    expect(lane.reach).toEqual([1, 3, 4, 5]) // claimed, implementing, awaiting-ci, addressing-review
  })

  it('keeps the head at the live phase, LEFT of the furthest column', () => {
    expect(lane.head).toBe(spineIndex('awaiting-ci')) // 4, not 5
  })

  it('records the return trace from the further column back to the head', () => {
    expect(lane.loops).toEqual([{ from: spineIndex('addressing-review'), to: spineIndex('awaiting-ci') }])
    expect(lane.reopens).toBe(0) // a round-trip is not a reopen (no exit between)
  })

  it('prevIdx is the distinct column before the head in time (addressing-review)', () => {
    expect(lane.prevIdx).toBe(spineIndex('addressing-review')) // 5
  })
})

describe('foldItem — reopen after an exit', () => {
  // claimed → implementing → yielded → implementing (reopened) → awaiting-ci.
  const lane = foldItem(item({
    phase: 'awaiting-ci',
    reopens: 1, // payload authority
    timeline: [
      { phase: 'claimed', at: T(2) },
      { phase: 'implementing', at: T(3) },
      { phase: 'yielded', at: T(3, 5) },
      { phase: 'implementing', at: T(5) },
      { phase: 'awaiting-ci', at: T(6) },
    ],
  }))

  it('does not draw an exit stub — the lane came back on-spine', () => {
    expect(lane.exit).toBeNull()
    expect(lane.head).toBe(spineIndex('awaiting-ci'))
    expect(lane.cls).toBe('wait')
  })

  it('honours the payload reopen count', () => {
    expect(lane.reopens).toBe(1)
  })

  it('re-entering implementing produces a loop, not a new column', () => {
    expect(lane.reach).toEqual([1, 3, 4]) // claimed, implementing, awaiting-ci
    expect(lane.loops.some((l) => l.to === spineIndex('implementing'))).toBe(true)
  })

  it('derives reopens from the walk when the payload omits the count', () => {
    const derived = foldItem(item({
      phase: 'awaiting-ci',
      reopens: 0,
      timeline: [
        { phase: 'implementing', at: T(3) },
        { phase: 'yielded', at: T(3, 5) },
        { phase: 'implementing', at: T(5) },
        { phase: 'awaiting-ci', at: T(6) },
      ],
    }))
    expect(derived.reopens).toBe(1)
  })
})

describe('foldItem — off-spine exit (decision 2: stub, not a column)', () => {
  const lane = foldItem(item({
    number: 3664,
    phase: 'skipped',
    exit: { phase: 'skipped', at: T(2, 38, 21) },
    timeline: [
      { phase: 'claimed', at: T(2, 38, 19) },
      { phase: 'skipped', at: T(2, 38, 21) },
    ],
  }))

  it('has no head column — the item is off the spine', () => {
    expect(lane.head).toBe(-1)
    expect(lane.cls).toBe('exit')
  })

  it('reached only the on-spine columns; skipped is NOT one of them', () => {
    expect(lane.reach).toEqual([spineIndex('claimed')]) // [1]
    expect(lane.reach).not.toContain(spineIndex('skipped')) // -1 anyway
  })

  it('draws an exit stub off the furthest reached column', () => {
    expect(lane.exit).toEqual({ phase: 'skipped', token: 'exit_skipped', atColumn: spineIndex('claimed'), atMs: 1767235101000 })
  })

  it('falls back to the live phase for the exit when the payload omits exit', () => {
    const l = foldItem(item({
      phase: 'yielded',
      exit: null,
      timeline: [
        { phase: 'claimed', at: T(2) },
        { phase: 'yielded', at: T(2, 5) },
      ],
    }))
    expect(l.exit?.phase).toBe('yielded')
    expect(l.exit?.token).toBe('exit_yielded')
  })

  it('an exited lane has no running clock', () => {
    expect(lane.currentSince).toBeNull()
    expect(openDwellSeconds(lane, MS(9))).toBeNull()
  })
})

describe('foldItem — dwell (L2)', () => {
  it('per-column dwell is measured between first entries, keyed by destination', () => {
    const lane = foldItem(item({
      phase: 'awaiting-merge',
      timeline: [
        { phase: 'claimed', at: T(2, 0) },
        { phase: 'implementing', at: T(2, 30) },   // +30m into implementing
        { phase: 'awaiting-ci', at: T(3, 0) },     // +30m into awaiting-ci
        { phase: 'awaiting-merge', at: T(3, 40) },  // +40m into awaiting-merge
      ],
    }))
    const dwells = laneDwells(lane)
    expect(dwells).toEqual([
      { toColumn: spineIndex('implementing'), seconds: 30 * 60 },
      { toColumn: spineIndex('awaiting-ci'), seconds: 30 * 60 },
      { toColumn: spineIndex('awaiting-merge'), seconds: 40 * 60 },
    ])
  })

  it('open dwell is measured from the MOST RECENT entry into the current phase (decision 3)', () => {
    // awaiting-ci entered at T(3), left, then RE-entered at T(5). An open dwell
    // to T(6) must be 1h (from T(5)), NOT 3h (from the first T(3) entry).
    const lane = foldItem(item({
      phase: 'awaiting-ci',
      timeline: [
        { phase: 'implementing', at: T(2) },
        { phase: 'awaiting-ci', at: T(3) },
        { phase: 'addressing-review', at: T(4) },
        { phase: 'awaiting-ci', at: T(5) },
      ],
    }))
    expect(lane.currentSince).toBe(MS(5))
    expect(openDwellSeconds(lane, MS(6))).toBe(3600) // 1h, not 3h
  })

  it('a legacy timeline line with no `at` degrades to no dwell rather than NaN', () => {
    const lane = foldItem(item({
      phase: 'awaiting-ci',
      timeline: [
        { phase: 'claimed' },                 // no `at` — legacy line
        { phase: 'implementing', at: T(2) },
        { phase: 'awaiting-ci', at: T(3) },
      ],
    }))
    expect(laneDwells(lane)).toEqual([{ toColumn: spineIndex('awaiting-ci'), seconds: 3600 }])
    // reach is still complete (L0/L1 survive a missing timestamp)
    expect(lane.reach).toEqual([1, 3, 4])
  })

  it('dwellSeconds and formatDwell', () => {
    expect(dwellSeconds(MS(1), MS(2))).toBe(3600)
    expect(dwellSeconds(null, MS(2))).toBeNull()
    expect(dwellSeconds(MS(2), null)).toBeNull()
    // formatDwell returns the structured amount + CLDR unit; the view localizes
    // the rendering (45s / 12m / 3.2h in en). The threshold ladder is what is
    // pinned here.
    expect(formatDwell(45)).toEqual({ value: 45, unit: 'second' })
    expect(formatDwell(12 * 60)).toEqual({ value: 12, unit: 'minute' })
    expect(formatDwell(Math.round(3.2 * 3600))).toEqual({ value: 3.2, unit: 'hour' })
  })
})

describe('columnOccupancy (L1 badges)', () => {
  const lanes = [
    foldItem(item({ number: 1, phase: 'implementing', timeline: [{ phase: 'implementing', at: T(2) }] })),
    foldItem(item({ number: 2, phase: 'awaiting-ci', timeline: [{ phase: 'awaiting-ci', at: T(2) }] })),
    foldItem(item({ number: 3, phase: 'awaiting-ci', timeline: [{ phase: 'awaiting-ci', at: T(2) }] })),
    foldItem(item({ number: 4, phase: 'resolved', timeline: [{ phase: 'resolved', at: T(2) }] })),
    foldItem(item({ number: 5, phase: 'yielded', timeline: [{ phase: 'claimed', at: T(1) }, { phase: 'yielded', at: T(2) }] })),
  ]
  const occ = columnOccupancy(lanes)

  it('counts live lanes per column and flags editing', () => {
    expect(occ.get(spineIndex('implementing'))).toEqual({ total: 1, editing: 1 })
    expect(occ.get(spineIndex('awaiting-ci'))).toEqual({ total: 2, editing: 0 })
  })

  it('excludes done and exited lanes from occupancy', () => {
    expect(occ.get(spineIndex('resolved'))).toBeUndefined()
    expect([...occ.values()].reduce((a, b) => a + b.total, 0)).toBe(3)
  })
})

describe('queueSummary — the aggregate an operator scans first', () => {
  const lanes = [
    foldItem(item({ number: 1, phase: 'implementing', timeline: [{ phase: 'implementing', at: T(2) }] })),
    foldItem(item({ number: 2, phase: 'awaiting-ci', timeline: [{ phase: 'awaiting-ci', at: T(1) }] })),
    foldItem(item({ number: 3, phase: 'awaiting-ci', timeline: [{ phase: 'awaiting-ci', at: T(5) }] })),
    foldItem(item({ number: 4, phase: 'resolved', timeline: [{ phase: 'resolved', at: T(2) }] })),
    foldItem(item({
      number: 5, phase: 'awaiting-merge', reopens: 2,
      timeline: [{ phase: 'implementing', at: T(1) }, { phase: 'awaiting-merge', at: T(3) }],
    })),
  ]
  const summary = queueSummary(lanes, MS(6))

  it('counts only live lanes and flags editing', () => {
    expect(summary.live).toBe(4) // excludes the resolved lane
    expect(summary.editing).toBe(1) // lane 1 (implementing)
  })

  it('tallies live lanes per phase', () => {
    expect(summary.perPhase.get('awaiting-ci')).toBe(2)
    expect(summary.perPhase.get('implementing')).toBe(1)
    expect(summary.perPhase.get('awaiting-merge')).toBe(1)
    expect(summary.perPhase.get('resolved')).toBeUndefined() // done lanes are not "in the queue"
  })

  it('sums reopens across all lanes as the escalation count', () => {
    expect(summary.reopens).toBe(2)
  })

  it('names the single longest-waiting live lane', () => {
    // lane 2 entered awaiting-ci at T(1); at T(6) that is 5h — the longest.
    expect(summary.longestWait).toEqual({ number: 2, phase: 'awaiting-ci', seconds: 5 * 3600 })
  })

  it('has no longest wait when nothing is running', () => {
    const s = queueSummary(
      [foldItem(item({ number: 9, phase: 'resolved', timeline: [{ phase: 'resolved', at: T(2) }] }))],
      MS(6),
    )
    expect(s.live).toBe(0)
    expect(s.longestWait).toBeNull()
  })
})

describe('foldItem — forward compatibility', () => {
  it('ignores an unknown phase in the timeline rather than crashing', () => {
    const lane = foldItem(item({
      phase: 'awaiting-ci',
      // @ts-expect-error deliberately feeding a phase this client does not know
      timeline: [
        { phase: 'claimed', at: T(2) },
        { phase: 'teleporting', at: T(2, 30) }, // unknown — skipped
        { phase: 'awaiting-ci', at: T(3) },
      ],
    }))
    expect(lane.reach).toEqual([spineIndex('claimed'), spineIndex('awaiting-ci')])
    expect(lane.head).toBe(spineIndex('awaiting-ci'))
  })

  it('tolerates a missing timeline array', () => {
    // @ts-expect-error deliberately omitting timeline to exercise the guard
    const lane = foldItem(item({ phase: 'claimed', timeline: undefined }))
    expect(lane.reach).toEqual([])
    expect(lane.head).toBe(spineIndex('claimed'))
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// WAVE 2 — new pure helpers for the faithful drawing's hover card and the queue
// dashboard, plus the two MUTATION-VERIFIED rules the whole view lives on.
// ─────────────────────────────────────────────────────────────────────────────

describe('laneTimelineRows — the hover card full phase table', () => {
  it('gives each row the time spent IN its own phase, leaving the last one open', () => {
    const lane = foldItem(item({
      phase: 'awaiting-ci',
      timeline: [
        { phase: 'claimed', at: T(2, 0) },          // in claimed for 30m
        { phase: 'implementing', at: T(2, 30) },    // in implementing for 10m
        { phase: 'yielded', at: T(2, 40) },         // off-spine, still a ROW; 2h here
        { phase: 'implementing', at: T(4, 40) },    // in implementing again for 20m
        { phase: 'awaiting-ci', at: T(5, 0) },      // still there -- clock open
      ],
    }))
    const rows = laneTimelineRows(lane)
    // one row per timeline entry — the fold's column collapse does NOT apply here
    expect(rows.map((r) => r.phase)).toEqual([
      'claimed', 'implementing', 'yielded', 'implementing', 'awaiting-ci',
    ])
    // A row's dwell is measured FORWARD to the next stop, so it answers "how long was
    // the item in THIS phase" -- the question the DWELL header asks. Measuring
    // backwards put each gap on the row the item had just moved INTO, so `claimed`
    // read as null while the 30 minutes spent there appeared beside `implementing`.
    expect(rows[0].dwellSeconds).toBe(30 * 60)
    expect(rows[1].dwellSeconds).toBe(10 * 60)
    expect(rows[2].dwellSeconds).toBe(2 * 3600)
    expect(rows[3].dwellSeconds).toBe(20 * 60)
    // The last stop has no successor: its clock is still running, and the open dwell
    // is reported separately rather than guessed from `now` in here.
    expect(rows[4].dwellSeconds).toBeNull()
  })

  it('flags a step over an hour as slow, and only such steps', () => {
    const lane = foldItem(item({
      phase: 'awaiting-ci',
      timeline: [
        { phase: 'implementing', at: T(1, 0) },
        { phase: 'awaiting-ci', at: T(1, 59) },     // 59m — under the hour
        { phase: 'awaiting-ci', at: T(3, 0) },      // +61m — over the hour
      ],
    }))
    const rows = laneTimelineRows(lane)
    expect(SLOW_DWELL_SECONDS).toBe(3600)
    // The flag now sits on the phase that HELD the time: implementing kept the item
    // 59m (under the hour), the first awaiting-ci row kept it 61m (over).
    expect(rows[0].slow).toBe(false) // implementing, 59m
    expect(rows[1].slow).toBe(true)  // awaiting-ci, 61m
    expect(rows[2].dwellSeconds).toBeNull() // last stop, clock still open
  })

  it('a legacy line with no `at` degrades to a null dwell, not NaN', () => {
    const lane = foldItem(item({
      phase: 'awaiting-ci',
      timeline: [
        { phase: 'claimed' },                 // legacy, no `at`
        { phase: 'awaiting-ci', at: T(3) },
      ],
    }))
    const rows = laneTimelineRows(lane)
    expect(rows[0].atMs).toBeNull()
    expect(rows[1].dwellSeconds).toBeNull() // prior step had no timestamp
    expect(rows[1].slow).toBe(false)
  })
})

describe('phaseCaption / phaseHeader — station text lives in one pure place', () => {
  it('returns the caption catalog key for every on-spine phase and blanks an unknown one', () => {
    expect(phaseCaption('implementing')).toBe('caption_implementing')
    expect(phaseCaption('awaiting-merge')).toBe('caption_awaiting_merge')
    expect(phaseCaption('resolved')).toBe('caption_resolved')
    expect(phaseCaption('teleporting')).toBe('')
  })

  it('returns the header catalog key for the abbreviated column names', () => {
    expect(phaseHeader('awaiting-ci')).toBe('header_awaiting_ci')
    expect(phaseHeader('addressing-review')).toBe('header_addressing_review')
    expect(phaseHeader('awaiting-merge')).toBe('header_awaiting_merge')
    expect(phaseHeader('investigating')).toBe('header_investigating')
    expect(phaseHeader('teleporting')).toBe('')
  })
})

describe('queueSummary — exits and resolved counts (dashboard)', () => {
  const lanes = [
    foldItem(item({ number: 1, phase: 'implementing', timeline: [{ phase: 'implementing', at: T(2) }] })),
    foldItem(item({ number: 2, phase: 'resolved', timeline: [{ phase: 'resolved', at: T(2) }] })),
    foldItem(item({
      number: 3, phase: 'skipped', exit: { phase: 'skipped', at: T(2) },
      timeline: [{ phase: 'claimed', at: T(1) }, { phase: 'skipped', at: T(2) }],
    })),
    foldItem(item({
      number: 4, phase: 'awaiting-reply', exit: { phase: 'awaiting-reply', at: T(3) },
      timeline: [{ phase: 'claimed', at: T(1) }, { phase: 'awaiting-reply', at: T(3) }],
    })),
  ]
  const summary = queueSummary(lanes, MS(6))

  it('counts off-spine endings (incl. awaiting-reply) as exits', () => {
    expect(summary.exits).toBe(2) // skipped + awaiting-reply
  })

  it('counts resolved lanes separately as throughput', () => {
    expect(summary.resolved).toBe(1)
  })

  it('an exit or a resolved lane is not in the live queue', () => {
    expect(summary.live).toBe(1) // only the implementing lane
    expect(summary.editing).toBe(1)
  })
})

describe('dashboardCells — per-phase instrument row aligned to the spine columns', () => {
  const lanes = [
    foldItem(item({ number: 1, phase: 'awaiting-ci', timeline: [{ phase: 'awaiting-ci', at: T(2) }] })),
    foldItem(item({ number: 2, phase: 'awaiting-ci', timeline: [{ phase: 'awaiting-ci', at: T(2) }] })),
    foldItem(item({ number: 3, phase: 'implementing', timeline: [{ phase: 'implementing', at: T(2) }] })),
  ]
  const cells = dashboardCells(queueSummary(lanes, MS(6)))

  it('emits one cell per spine phase, in column order', () => {
    expect(cells.map((c) => c.phase)).toEqual([...SPINE_PHASES])
  })

  it('carries the live count and the editing flag per phase', () => {
    const ci = cells.find((c) => c.phase === 'awaiting-ci')!
    const impl = cells.find((c) => c.phase === 'implementing')!
    expect(ci.count).toBe(2)
    expect(ci.editing).toBe(false)
    expect(impl.count).toBe(1)
    expect(impl.editing).toBe(true)
  })

  it('marks the single busiest phase (earliest column on a tie)', () => {
    const busiest = cells.filter((c) => c.busiest)
    expect(busiest).toHaveLength(1)
    expect(busiest[0].phase).toBe('awaiting-ci') // 2 lanes, the max
  })

  it('marks nothing busiest when the queue is empty', () => {
    const empty = dashboardCells(queueSummary([], MS(6)))
    expect(empty.every((c) => c.count === 0)).toBe(true)
    expect(empty.some((c) => c.busiest)).toBe(false)
  })

  it('the editing slot cap mirrors the store invariant (at most one editing)', () => {
    expect(EDITING_SLOT_CAP).toBe(1)
  })
})

// ── MUTATION-VERIFY: the two rules the view lives or dies on ──
//
// These two describes each hold (a) the assertion that PROVES the rule and (b) a
// commented "mutant" — the exact wrong implementation the rule guards against.
// The wave-2 report records the observed result of pasting each mutant into the
// production code (fabric.ts) and watching THESE tests redden, then restoring.

describe('MUTATION-VERIFY rule 3 — head is the LIVE phase, not the max column', () => {
  // A review round-trip: implementing → awaiting-ci → addressing-review → awaiting-ci.
  // The furthest column reached is addressing-review (5); the LIVE phase is
  // awaiting-ci (4). The head MUST be 4.
  const lane = foldItem(item({
    phase: 'awaiting-ci',
    timeline: [
      { phase: 'implementing', at: T(2) },
      { phase: 'awaiting-ci', at: T(3) },
      { phase: 'addressing-review', at: T(4) },
      { phase: 'awaiting-ci', at: T(5) },
    ],
  }))

  it('places the head LEFT of the furthest column reached', () => {
    expect(lane.reach).toContain(spineIndex('addressing-review')) // 5 was reached
    expect(lane.head).toBe(spineIndex('awaiting-ci'))             // but the head is 4
    expect(lane.head).toBeLessThan(spineIndex('addressing-review'))
  })

  // MUTANT (foldItem): `const head = offSpine ? -1 : Math.max(...reach)`.
  // With that, head would be spineIndex('addressing-review') === 5 and the first
  // two assertions above go RED. Observed and restored — see the report.
})

describe('MUTATION-VERIFY rule 2 — an off-spine phase is an EXIT, never a column', () => {
  const lane = foldItem(item({
    number: 3664,
    phase: 'skipped',
    exit: { phase: 'skipped', at: T(2, 38, 21) },
    timeline: [
      { phase: 'claimed', at: T(2, 38, 19) },
      { phase: 'skipped', at: T(2, 38, 21) },
    ],
  }))

  it('gives skipped no column and draws it as a stub off the furthest column', () => {
    expect(spineIndex('skipped')).toBe(-1)                 // not a column at all
    expect(lane.reach).toEqual([spineIndex('claimed')])    // only claimed is a column
    expect(lane.head).toBe(-1)                             // off the spine
    expect(lane.exit).toEqual({ phase: 'skipped', token: 'exit_skipped', atColumn: spineIndex('claimed'), atMs: 1767235101000 })
  })

  // MUTANT (fabric.ts EXIT_PHASES): delete the `'skipped': true` entry so skipped
  // is treated as on-spine. `spineIndex('skipped')` is still -1 (SPINE_PHASES has
  // no skipped), so the fold would push head/reach off a -1 column and BOTH the
  // reach and the exit assertions above go RED (exit becomes null, reach unchanged
  // but head no longer -1 via a different path). Observed and restored.
})

// ─────────────────────────────────────────────────────────────────────────────
// REPO RESOLUTION — the pure decision behind "which repo does a first-ever visit
// show". localStorage is a REMEMBERED PREFERENCE; the connected list is the source
// of truth. These four cases are the whole contract.
// ─────────────────────────────────────────────────────────────────────────────

/** A connected-repo builder — only the fields selectRepo reads. */
function conn(owner: string, repo: string, extra: Partial<ConnectedRepo> = {}): ConnectedRepo {
  return { owner, repo, ...extra }
}

describe('repoOrderKey — a stable, total order so the default repo is not "by chance"', () => {
  it('keys by provider:host/owner/repo, lower-cased, defaulting a legacy record to public GitHub', () => {
    expect(repoOrderKey(conn('Acme', 'Web'))).toBe('github:github.com/acme/web')
    expect(repoOrderKey(conn('grp', 'proj', { provider: 'gitlab', host: 'gitlab.example.com' })))
      .toBe('gitlab:gitlab.example.com/grp/proj')
  })

  it('sorts independently of the incoming (config insertion) order', () => {
    const a = [conn('zed', 'z'), conn('acme', 'a'), conn('mid', 'm')]
    const sorted = [...a].sort((x, y) => repoOrderKey(x).localeCompare(repoOrderKey(y)))
    expect(sorted.map((r) => r.repo)).toEqual(['a', 'm', 'z'])
  })
})

describe('selectRepo — the four contract cases', () => {
  const connected: ConnectedRepo[] = [
    // deliberately NOT in stable order, to prove the fallback does not pick repos[0]
    conn('zeta', 'zulu'),
    conn('acme', 'web', { provider: 'github', host: 'github.com' }),
    conn('mid', 'svc', { provider: 'gitlab', host: 'gitlab.example.com' }),
  ]

  it('1. preference present AND still connected → that repo (identity-matched)', () => {
    const pref: RepoRef = { owner: 'zeta', repo: 'zulu' }
    const got = selectRepo(pref, connected)
    expect(got).toEqual({ owner: 'zeta', repo: 'zulu', provider: 'github', host: 'github.com' })
  })

  it('1b. identity is part of the match — same owner/repo on a different host is NOT the preference', () => {
    const pref: RepoRef = { owner: 'mid', repo: 'svc' } // defaults to github/github.com
    const got = selectRepo(pref, connected)
    // 'mid/svc' is connected only as a gitlab project, so the github preference is
    // stale → falls back to the stable-first repo, not the gitlab mid/svc.
    expect(got).toEqual({ owner: 'acme', repo: 'web', provider: 'github', host: 'github.com' })
  })

  it('2. preference present but NO LONGER connected → first connected by stable order', () => {
    const pref: RepoRef = { owner: 'gone', repo: 'deleted' }
    const got = selectRepo(pref, connected)
    // stable-first of the three is acme/web (github:github.com/acme/web)
    expect(got).toEqual({ owner: 'acme', repo: 'web', provider: 'github', host: 'github.com' })
  })

  it('3. preference absent, repos available → first connected by stable order', () => {
    const got = selectRepo(null, connected)
    expect(got).toEqual({ owner: 'acme', repo: 'web', provider: 'github', host: 'github.com' })
  })

  it('4. no repos connected at all → null (the genuine empty state)', () => {
    expect(selectRepo({ owner: 'acme', repo: 'web' }, [])).toBeNull()
    expect(selectRepo(null, [])).toBeNull()
  })

  it('never returns a repo outside the connected list, regardless of preference', () => {
    const got = selectRepo({ owner: 'ghost', repo: 'x' }, connected)
    const keys = connected.map(repoOrderKey)
    expect(got).not.toBeNull()
    expect(keys).toContain(repoOrderKey(got as ConnectedRepo))
  })
})

// MUTATION-VERIFY (case 2, "stale preference falls back"): break selectRepo by
// returning the matched preference verbatim without validating it against the
// connected list — e.g. replace the body with `return preference ?? …`. Then case
// 2 ("gone/deleted" preference) would return { owner:'gone', repo:'deleted' }, a
// repo that is NOT connected, and BOTH the "case 2" assertion and the
// "never returns a repo outside the connected list" assertion go RED. Observed
// going red on that mutation and restored — see the report.

describe('editing slot is bounded per crew, not globally', () => {
  // The store scopes the cap to one crew (`_editing_item` takes `crew_id`, and its
  // refusal names that crew), so two crews each editing one item is legal and routine.
  // Folding the TOTAL against a cap of 1 turned normal two-crew work into a red
  // violation -- a false fault, which is worse than no signal at all.
  const editingLane = (number: number, crewId: string): FabricLane => ({
    ...foldItem(
      {
        number,
        crew_id: crewId,
        phase: 'implementing',
        timeline: [{ phase: 'implementing', at: '2026-01-01T00:00:00Z' }],
      } as never,
      0,
    ),
  })

  it('two crews editing one item each is NOT over the cap', () => {
    const s = queueSummary([editingLane(1, 'crew-a'), editingLane(2, 'crew-b')], 0)
    expect(s.editing).toBe(2)
    expect(s.editingMaxPerCrew).toBe(1)
    expect(s.editingMaxPerCrew > EDITING_SLOT_CAP).toBe(false)
  })

  it('one crew editing two items IS over the cap', () => {
    const s = queueSummary([editingLane(1, 'crew-a'), editingLane(2, 'crew-a')], 0)
    expect(s.editing).toBe(2)
    expect(s.editingMaxPerCrew).toBe(2)
    expect(s.editingMaxPerCrew > EDITING_SLOT_CAP).toBe(true)
  })
})
