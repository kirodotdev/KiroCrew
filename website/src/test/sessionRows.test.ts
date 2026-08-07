import { describe, it, expect } from 'vitest'

import {
  buildTree,
  columnMaxima,
  fmtCredits,
  fmtGb,
  fmtHostPct,
  fmtMb,
  fmtTurns,
  fmtUptime,
  heatLevel,
  rowName,
  sessionChatPath,
  sparklineBars,
  type SessionPayloadRow,
  type TaskPayloadRow,
} from '../pages/system/sessionRows'

const session = (over: Partial<SessionPayloadRow> = {}): SessionPayloadRow => ({
  key: 'dashboard:chat-1',
  title: 'A chat',
  slot_key: 'chat-1',
  untitled: false,
  agent: 'kirocrew',
  channel: 'dashboard',
  pid: 7,
  owns_runtime: true,
  prompts: 1,
  rss_mb: 100,
  procs: 3,
  mcp: 1,
  cpu_cores: 0.1,
  uptime_s: 60,
  credits: 5.2,
  turns: 3,
  ...over,
})

const task = (over: Partial<TaskPayloadRow> = {}): TaskPayloadRow => ({
  id: 't1',
  task: 'a task',
  agent: 'kirocrew-research',
  parent: 'dashboard:chat-1',
  rss_mb: 50,
  peak_rss_mb: 60,
  cpu_cores: 0.05,
  started_at: Date.now() / 1000 - 30,
  shared: false,
  pid: 8,
  sampled: true,
  ...over,
})

// ── sessionChatPath ──

describe('sessionChatPath', () => {
  it('routes a dashboard session to its chat window by BARE slot key', () => {
    // ChatPage reads ?sid= and strips nothing, so the dashboard: prefix must not
    // travel with it.
    expect(sessionChatPath('dashboard:chat-69-1785905004')).toBe('/chat?sid=chat-69-1785905004')
  })

  it('encodes the slot key', () => {
    expect(sessionChatPath('dashboard:chat a/b')).toBe('/chat?sid=chat%20a%2Fb')
  })

  it.each(['_bg', 'slack:C123', 'cron:job-1'])('has no chat window for %s', key => {
    // These are real sessions with real memory but nothing to navigate to —
    // null keeps the row non-interactive instead of shipping a dead click.
    expect(sessionChatPath(key)).toBeNull()
  })

  it('rejects a bare prefix with no slot key', () => {
    expect(sessionChatPath('dashboard:')).toBeNull()
  })
})

// ── rowName ──

describe('rowName', () => {
  it('uses the resolved title', () => {
    expect(rowName(session({ title: 'Windows testing issue found' }))).toBe('Windows testing issue found')
  })

  it('appends the slot key when untitled, so rows stay distinguishable', () => {
    const name = rowName(session({ title: 'New Session…', untitled: true, slot_key: 'chat-70' }))
    expect(name).toBe('New Session… chat-70')
  })

  it('falls back to the key when there is no title at all', () => {
    expect(rowName(session({ title: '', key: 'dashboard:x' }))).toBe('dashboard:x')
  })
})

// ── formatters ──

describe('formatters', () => {
  it('renders megabytes with one decimal and a group separator', () => {
    expect(fmtMb(3238)).toBe('3,238.0MB')
    expect(fmtMb(572)).toBe('572.0MB')
  })

  it.each([null, undefined, NaN])('renders %s as an em dash rather than 0', v => {
    expect(fmtMb(v as number | null)).toBe('—')
  })

  it('takes a ratio for the host share, not a pre-multiplied percentage', () => {
    // A double-multiply bug would render 5,000% here.
    expect(fmtHostPct(1024, 2048)).toBe('50%')
    expect(fmtHostPct(3238, 126976)).toBe('2.55%')
  })

  it('has no host share without a host total', () => {
    expect(fmtHostPct(1024, null)).toBe('—')
  })

  it('renders a multi-day uptime coarsely', () => {
    expect(fmtUptime(2 * 86400 + 6 * 3600 + 41 * 60)).toBe('2d 6h 41m')
    expect(fmtUptime(9082)).toBe('2h 31m')
  })

  it('drops zero components', () => {
    // Load-bearing: without dropZero this same input renders "0d 0h 1m", so both
    // the exact form and the absence of a zero component discriminate.
    expect(fmtUptime(90)).toBe('1m')
    expect(fmtUptime(90)).not.toMatch(/0d/)
  })

  it('rejects a negative uptime', () => {
    expect(fmtUptime(-1)).toBe('—')
  })
})

// ── fmtGb ──

describe('fmtGb', () => {
  it('renders host-scale totals in GB with two decimals', () => {
    expect(fmtGb(126771.2)).toMatch(/123\.80/)
  })

  it('renders an em dash for an unsampled total', () => {
    expect(fmtGb(null)).toBe('—')
  })
})

// ── sparklineBars ──

describe('sparklineBars', () => {
  it('scales against the host total, not the series maximum', () => {
    // Same series, different ceiling: a self-scaled trace would peg the peak to
    // full height in both cases and make a small blip look like saturation.
    expect(sparklineBars([{ t: 1, mb: 1024 }, { t: 2, mb: 2048 }], 4096)).toEqual([25, 50])
  })

  it('clamps a series that exceeds the ceiling instead of overflowing the box', () => {
    expect(sparklineBars([{ t: 1, mb: 10 }, { t: 2, mb: 999 }], 100)).toEqual([10, 100])
  })

  it('returns nothing for a series too short to show a trend', () => {
    expect(sparklineBars([], 100)).toEqual([])
    expect(sparklineBars([{ t: 1, mb: 5 }], 100)).toEqual([])
  })
})

// ── buildTree ──

describe('buildTree', () => {
  it('nests each task under the right parent via subRows', () => {
    // Tasks must be accessible through the parent session, not as top-level rows,
    // because TanStack expand/collapse operates on subRows.
    const rows = buildTree(
      [session({ key: 'dashboard:a' }), session({ key: 'dashboard:b', rss_mb: 200 })],
      [task({ id: 't-b', parent: 'dashboard:b' }), task({ id: 't-a', parent: 'dashboard:a' })],
    )
    const rowA = rows.find(r => r.id === 'dashboard:a')!
    const rowB = rows.find(r => r.id === 'dashboard:b')!
    expect(rowA.subRows).toHaveLength(1)
    expect(rowA.subRows![0].id).toBe('t-a')
    expect(rowB.subRows).toHaveLength(1)
    expect(rowB.subRows![0].id).toBe('t-b')
  })

  it('omits subRows entirely when a session has no tasks', () => {
    // An empty array would make TanStack treat the row as expandable (canExpand),
    // which renders a meaningless disclosure chevron on a leaf row.
    const rows = buildTree([session({ key: 'dashboard:solo' })], [])
    expect(rows[0]).not.toHaveProperty('subRows')
  })

  it('assigns channel "subagent" to tasks regardless of parent channel', () => {
    // A task was spawned by the agent, not the user — attributing the parent's
    // channel would misreport origin when grouping by channel.
    const rows = buildTree(
      [session({ key: 'dashboard:a', channel: 'slack' })],
      [task({ id: 't1', parent: 'dashboard:a' })],
    )
    expect(rows[0].subRows![0].channel).toBe('subagent')
  })

  it('shows unsampled task fields as null', () => {
    // A task that has not been measured yet must read "—" in the UI, not "0".
    const rows = buildTree(
      [session()],
      [task({ sampled: false, rss_mb: 0, peak_rss_mb: 0, cpu_cores: 0 })],
    )
    const t = rows[0].subRows![0]
    expect(t.rssMb).toBeNull()
    expect(t.peakMb).toBeNull()
    expect(t.cpuCores).toBeNull()
  })

  it('marks a co-tenant of a multiplexed runtime as shared', () => {
    const rows = buildTree([session({ owns_runtime: false })], [])
    expect(rows[0].shared).toBe(true)
  })

  it('gives only sessions a destination — a task has no chat window', () => {
    const rows = buildTree([session()], [task()])
    expect(rows[0].href).toBe('/chat?sid=chat-1')
    expect(rows[0].subRows![0].href).toBeNull()
  })

  it('does not mutate the input arrays', () => {
    const sessions = [session({ key: 'dashboard:a' }), session({ key: 'dashboard:b' })]
    const tasks = [task({ id: 't1', parent: 'dashboard:a' })]
    const sessionsSnapshot = [...sessions.map(s => s.key)]
    const tasksSnapshot = [...tasks.map(t => t.id)]
    buildTree(sessions, tasks)
    expect(sessions.map(s => s.key)).toEqual(sessionsSnapshot)
    expect(tasks.map(t => t.id)).toEqual(tasksSnapshot)
  })

  it("preserves input order (sorting is the table's job)", () => {
    // buildTree must NOT sort — imposing an order would fight the table's
    // SortingState and make the first paint disagree with user-chosen order.
    const rows = buildTree(
      [session({ key: 'dashboard:z', rss_mb: 1 }), session({ key: 'dashboard:a', rss_mb: 999 })],
      [],
    )
    expect(rows.map(r => r.id)).toEqual(['dashboard:z', 'dashboard:a'])
  })

  it('keeps a parentless task visible as a top-level row', () => {
    // The footer reports `tasks.length` under "Task sessions". Dropping a task
    // whose parent matches no session would count it there and hide it from the
    // table -- one quantity, two numbers, which is the defect this page exists
    // to remove. An empty or stale parent key happens for real (an app-spawned
    // task, or a task outliving its session).
    const rows = buildTree(
      [session({ key: 'dashboard:a' })],
      [
        task({ id: 't-child', parent: 'dashboard:a' }),
        task({ id: 't-orphan', parent: '' }),
        task({ id: 't-stale', parent: 'dashboard:gone' }),
      ],
    )

    const taskRowCount = rows.reduce(
      (n, r) => n + (r.kind === 'task' ? 1 : 0) + (r.subRows?.length ?? 0),
      0,
    )
    // The invariant the footer depends on: every task in the payload is
    // reachable in the tree.
    expect(taskRowCount).toBe(3)

    const topLevel = rows.filter(r => r.kind === 'task').map(r => r.id)
    expect(topLevel).toContain('t-orphan')
    expect(topLevel).toContain('t-stale')
    // The matched one stays nested under its session, not promoted.
    expect(rows.find(r => r.id === 'dashboard:a')?.subRows?.map(r => r.id)).toEqual(['t-child'])
  })
})

// ── columnMaxima ──

describe('columnMaxima', () => {
  it('walks into subRows to find the true maximum', () => {
    // A task can be larger than its parent session; ignoring subRows would
    // understate the heat ceiling and overflow the tint.
    const rows = buildTree(
      [session({ key: 'dashboard:a', rss_mb: 100, cpu_cores: 0.5 })],
      [task({ id: 't1', parent: 'dashboard:a', rss_mb: 200, cpu_cores: 1.2, sampled: true })],
    )
    const max = columnMaxima(rows)
    expect(max.rssMb).toBe(200)
    expect(max.cpuCores).toBe(1.2)
  })

  it('returns null when nothing is sampled', () => {
    // No samples means no ceiling — a null max disables the heat tint entirely
    // rather than dividing by zero.
    const rows = buildTree(
      [session({ key: 'dashboard:a', rss_mb: null, cpu_cores: null })],
      [],
    )
    const max = columnMaxima(rows)
    expect(max.rssMb).toBeNull()
    expect(max.cpuCores).toBeNull()
  })

  it('ignores null values in unsampled tasks', () => {
    const rows = buildTree(
      [session({ key: 'dashboard:a', rss_mb: 50, cpu_cores: 0.3 })],
      [task({ id: 't1', parent: 'dashboard:a', sampled: false, rss_mb: 0, cpu_cores: 0 })],
    )
    const max = columnMaxima(rows)
    expect(max.rssMb).toBe(50)
    expect(max.cpuCores).toBe(0.3)
  })
})

// ── fmtCredits ──

describe('fmtCredits', () => {
  it('renders a cumulative total with 1 decimal place', () => {
    expect(fmtCredits(18.4)).toBe('18.4')
    expect(fmtCredits(0.9)).toBe('0.9')
  })

  it('renders an em dash for null (not measured, NOT zero)', () => {
    expect(fmtCredits(null)).toBe('—')
    expect(fmtCredits(undefined)).toBe('—')
  })

  it('renders an em dash for NaN', () => {
    expect(fmtCredits(NaN)).toBe('—')
  })

  it('renders zero as a number, not as an em dash', () => {
    // Zero credits is a measured fact, null is unmeasured — they are distinct.
    expect(fmtCredits(0)).not.toBe('—')
    expect(fmtCredits(0)).toBe('0.0')
  })
})

// ── fmtTurns ──

describe('fmtTurns', () => {
  it('renders a turn count', () => {
    expect(fmtTurns(7)).toBe('7')
    expect(fmtTurns(100)).toBe('100')
  })

  it('renders an em dash for null (not measured, NOT zero)', () => {
    expect(fmtTurns(null)).toBe('—')
    expect(fmtTurns(undefined)).toBe('—')
  })

  it('renders zero turns as "0", not as an em dash', () => {
    // Distinguishes "measured zero" from "not measured"
    expect(fmtTurns(0)).toBe('0')
  })
})

// ── heatLevel ──

describe('heatLevel', () => {
  it('returns 0 when max is null', () => {
    expect(heatLevel(100, null)).toBe(0)
  })

  it('returns 0 when max is 0 (avoids division by zero)', () => {
    expect(heatLevel(0, 0)).toBe(0)
  })

  it('is safe with a negative max', () => {
    expect(heatLevel(5, -10)).toBe(0)
  })

  it('returns 0 when value is null', () => {
    expect(heatLevel(null, 100)).toBe(0)
  })

  it('returns 1 at the 0.1 boundary', () => {
    // share = 10/100 = 0.10, boundary is >= 0.1
    expect(heatLevel(10, 100)).toBe(1)
  })

  it('returns 1 just below the 0.33 boundary', () => {
    expect(heatLevel(32, 100)).toBe(1)
  })

  it('returns 2 at the 0.33 boundary', () => {
    expect(heatLevel(33, 100)).toBe(2)
  })

  it('returns 2 just below the 0.66 boundary', () => {
    expect(heatLevel(65, 100)).toBe(2)
  })

  it('returns 3 at the 0.66 boundary', () => {
    expect(heatLevel(66, 100)).toBe(3)
  })

  it('returns 3 at the maximum (share = 1.0)', () => {
    expect(heatLevel(100, 100)).toBe(3)
  })

  it('returns 0 for a very small share', () => {
    expect(heatLevel(1, 100)).toBe(0)
  })
})

// ── credits and turns in buildTree ──

describe('buildTree credits and turns', () => {
  it('carries credits and turns from the session payload', () => {
    const rows = buildTree([session({ credits: 24.9, turns: 11 })], [])
    expect(rows[0].credits).toBe(24.9)
    expect(rows[0].turns).toBe(11)
  })

  it('maps null credits/turns from the payload as null, not zero', () => {
    const rows = buildTree([session({ credits: null, turns: null })], [])
    expect(rows[0].credits).toBeNull()
    expect(rows[0].turns).toBeNull()
  })

  it('tasks always have null credits and turns (not measured per-task)', () => {
    const rows = buildTree([session()], [task()])
    expect(rows[0].subRows![0].credits).toBeNull()
    expect(rows[0].subRows![0].turns).toBeNull()
  })
})
