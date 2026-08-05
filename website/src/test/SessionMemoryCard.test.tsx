import { describe, it, expect } from 'vitest'

import {
  buildRows,
  fmtGb,
  fmtHostPct,
  fmtMb,
  fmtUptime,
  rowName,
  sessionChatPath,
  sparklineBars,
  VIEW_COLUMNS,
  VIEW_SORT,
} from '../pages/SessionMemoryCard'

type Sessions = Parameters<typeof buildRows>[0]
type Tasks = Parameters<typeof buildRows>[1]

const session = (over: Partial<Sessions[number]> = {}): Sessions[number] => ({
  key: 'dashboard:chat-1',
  title: 'A chat',
  slot_key: 'chat-1',
  untitled: false,
  agent: 'kirocrew',
  pid: 7,
  owns_runtime: true,
  prompts: 1,
  rss_mb: 100,
  procs: 3,
  mcp: 1,
  cpu_cores: 0.1,
  uptime_s: 60,
  ...over,
})

const task = (over: Partial<Tasks[number]> = {}): Tasks[number] => ({
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

describe('formatters', () => {
  // Exact values rather than a loose regex: a regex like /3.238.0/ leaves the
  // group separator unasserted (`.` matches anything), so it cannot tell
  // "3,238.0MB" from "3 238x0MB". These run under the fallback `en` locale.
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

describe('buildRows', () => {
  it('orders sessions by memory descending', () => {
    const rows = buildRows(
      [
        session({ key: 'dashboard:small', rss_mb: 100 }),
        session({ key: 'dashboard:big', rss_mb: 900 }),
      ],
      [],
    )
    expect(rows.map(r => r.id)).toEqual(['dashboard:big', 'dashboard:small'])
  })

  it('nests each task directly under its own parent session', () => {
    const rows = buildRows(
      [session({ key: 'dashboard:a', rss_mb: 900 }), session({ key: 'dashboard:b', rss_mb: 100 })],
      [task({ id: 't-b', parent: 'dashboard:b' }), task({ id: 't-a', parent: 'dashboard:a' })],
    )
    expect(rows.map(r => r.id)).toEqual(['dashboard:a', 't-a', 'dashboard:b', 't-b'])
  })

  it('sorts an unmeasured session last instead of treating it as the smallest', () => {
    // null is "not sampled yet", not 0 — a session still being measured must not
    // outrank a genuinely small one.
    const rows = buildRows(
      [session({ key: 'dashboard:unknown', rss_mb: null }), session({ key: 'dashboard:tiny', rss_mb: 1 })],
      [],
    )
    expect(rows.map(r => r.id)).toEqual(['dashboard:tiny', 'dashboard:unknown'])
  })

  it('marks a co-tenant of a multiplexed runtime as shared', () => {
    const [row] = buildRows([session({ owns_runtime: false })], [])
    expect(row.shared).toBe(true)
  })

  it('shows an unsampled task as unknown rather than zero', () => {
    const [, taskRow] = buildRows([session()], [task({ sampled: false, rss_mb: 0 })])
    expect(taskRow.rssMb).toBeNull()
    expect(taskRow.peakMb).toBeNull()
  })

  it('gives only sessions a destination — a task has no chat window of its own', () => {
    const [sessionRow, taskRow] = buildRows([session()], [task()])
    expect(sessionRow.href).toBe('/chat?sid=chat-1')
    expect(taskRow.href).toBeNull()
  })

  it('does not mutate the input array', () => {
    const input = [session({ key: 'dashboard:a', rss_mb: 1 }), session({ key: 'dashboard:b', rss_mb: 2 })]
    buildRows(input, [])
    expect(input.map(s => s.key)).toEqual(['dashboard:a', 'dashboard:b'])
  })
})

describe('buildRows sorting and filtering', () => {
  it('flips direction on the same column', () => {
    const s = [session({ key: 'dashboard:a', rss_mb: 100 }), session({ key: 'dashboard:b', rss_mb: 900 })]
    const desc = buildRows(s, [], { key: 'rssMb', desc: true }).map(r => r.id)
    const asc = buildRows(s, [], { key: 'rssMb', desc: false }).map(r => r.id)
    expect(desc).toEqual(['dashboard:b', 'dashboard:a'])
    expect(asc).toEqual(['dashboard:a', 'dashboard:b'])
  })

  it('keeps unmeasured rows last in BOTH directions', () => {
    // "Unknown" is not a small value: flipping the column must not promote an
    // unsampled session to the top, which a plain numeric compare would do.
    const s = [
      session({ key: 'dashboard:unknown', rss_mb: null }),
      session({ key: 'dashboard:small', rss_mb: 1 }),
    ]
    expect(buildRows(s, [], { key: 'rssMb', desc: true }).map(r => r.id))
      .toEqual(['dashboard:small', 'dashboard:unknown'])
    expect(buildRows(s, [], { key: 'rssMb', desc: false }).map(r => r.id))
      .toEqual(['dashboard:small', 'dashboard:unknown'])
  })

  it('sorts a text column alphabetically', () => {
    const s = [session({ key: 'dashboard:z', agent: 'zeta' }), session({ key: 'dashboard:a', agent: 'alpha' })]
    expect(buildRows(s, [], { key: 'agent', desc: false }).map(r => r.agent)).toEqual(['alpha', 'zeta'])
  })

  it('keeps a task welded under its parent regardless of the sort column', () => {
    // A task's number is only meaningful beside the session that owns it, so it
    // must never join the global sort and drift away from its parent.
    const s = [session({ key: 'dashboard:big', rss_mb: 900 }), session({ key: 'dashboard:small', rss_mb: 10 })]
    const t = [task({ id: 't1', parent: 'dashboard:small', rss_mb: 5, sampled: true })]
    const ids = buildRows(s, t, { key: 'rssMb', desc: true }).map(r => r.id)
    expect(ids).toEqual(['dashboard:big', 'dashboard:small', 't1'])
    expect(ids.indexOf('t1')).toBe(ids.indexOf('dashboard:small') + 1)
  })

  it('filters on title and on agent, and drops the orphaned tasks with the parent', () => {
    const s = [
      session({ key: 'dashboard:keep', title: 'Ported CR', agent: 'kirocrew' }),
      session({ key: 'dashboard:drop', title: 'Something else', agent: 'other' }),
    ]
    const t = [task({ id: 't-drop', parent: 'dashboard:drop', sampled: true })]
    expect(buildRows(s, t, { key: 'rssMb', desc: true }, 'ported').map(r => r.id)).toEqual(['dashboard:keep'])
    expect(buildRows(s, t, { key: 'rssMb', desc: true }, 'KIROCREW').map(r => r.id)).toEqual(['dashboard:keep'])
    // A hidden parent must not leave its task stranded at top level.
    expect(buildRows(s, t, { key: 'rssMb', desc: true }, 'ported').some(r => r.id === 't-drop')).toBe(false)
  })

  it('an empty filter changes nothing', () => {
    const s = [session({ key: 'dashboard:a' })]
    expect(buildRows(s, [], { key: 'rssMb', desc: true }, '   ').map(r => r.id)).toEqual(['dashboard:a'])
  })
})

describe('view modes', () => {
  it('opens each view sorted by its own subject', () => {
    // Switching to CPU must re-rank by CPU, otherwise the tab is cosmetic.
    expect(VIEW_SORT.memory).toBe('rssMb')
    expect(VIEW_SORT.cpu).toBe('cpuCores')
  })

  it('drops memory-only columns from the CPU view but keeps the identity ones', () => {
    expect(VIEW_COLUMNS.cpu).not.toContain('peakMb')
    expect(VIEW_COLUMNS.cpu).not.toContain('mcp')
    expect(VIEW_COLUMNS.cpu).toContain('cpuCores')
    expect(VIEW_COLUMNS.cpu).toContain('name')
    expect(VIEW_COLUMNS.cpu).toContain('pid')
  })

  it('flattens the Tasks view: tasks only, ranked globally, no parents', () => {
    const rows = buildRows(
      [session({ key: 'dashboard:chat-1', rss_mb: 900 })],
      [
        task({ id: 't-small', parent: 'dashboard:chat-1', rss_mb: 10 }),
        task({ id: 't-big', parent: 'dashboard:chat-1', rss_mb: 800 }),
      ],
      { key: 'rssMb', desc: true },
      '',
      'tasks',
    )
    expect(rows.every(r => r.kind === 'task')).toBe(true)
    // The 900MB session would outrank both tasks in a shared sort; its absence
    // is what proves the view is not just the memory view with a filter.
    expect(rows.map(r => r.id)).toEqual(['t-big', 't-small'])
  })
})

describe('disclosure', () => {
  it('marks only sessions that own tasks as expandable', () => {
    const rows = buildRows(
      [session({ key: 'dashboard:chat-1' }), session({ key: 'dashboard:chat-2', rss_mb: 50 })],
      [task({ id: 't1', parent: 'dashboard:chat-1' })],
      { key: 'rssMb', desc: true },
    )
    const byId = Object.fromEntries(rows.filter(r => r.kind === 'session').map(r => [r.id, r]))
    expect(byId['dashboard:chat-1'].hasTasks).toBe(true)
    expect(byId['dashboard:chat-2'].hasTasks).toBe(false)
  })

  it('hides a collapsed session\'s tasks while keeping the session itself', () => {
    const args = [
      [session({ key: 'dashboard:chat-1' })],
      [task({ id: 't1', parent: 'dashboard:chat-1' })],
      { key: 'rssMb' as const, desc: true },
      '',
      'memory' as const,
    ] as const
    const expanded = buildRows(...args, new Set<string>())
    const collapsed = buildRows(...args, new Set(['dashboard:chat-1']))
    expect(expanded.map(r => r.kind)).toEqual(['session', 'task'])
    expect(collapsed.map(r => r.kind)).toEqual(['session'])
    expect(collapsed[0].expanded).toBe(false)
  })
})

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

describe('fmtGb', () => {
  it('renders host-scale totals in GB with two decimals', () => {
    expect(fmtGb(126771.2)).toMatch(/123\.80/)
  })

  it('renders an em dash for an unsampled total', () => {
    expect(fmtGb(null)).toBe('—')
  })
})
