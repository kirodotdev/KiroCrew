//
// Pure data shaping behind the Overview "Today" card. These are the rules a
// reader of the card relies on without seeing them: which sessions count as
// today's, that a live slot outranks its archived twin, that incognito and
// temporary sessions never appear, how folders are ordered, and how a day file
// splits into entries.
//
import { describe, it, expect } from 'vitest'
import type { ChatSlot } from '../types'
import {
  groupByFolder,
  localDateKey,
  parseHistoryEntries,
  todaySessions,
} from '../pages/overview/todayActivity'

// 14:30 local on the test day; the two comparisons below stay inside the day.
const now = new Date(2026, 8, 15, 14, 30, 0)
const at = (h: number, m = 0) => new Date(2026, 8, 15, h, m, 0)
const iso = (d: Date) => d.toISOString()
const epoch = (d: Date) => Math.floor(d.getTime() / 1000)
const yesterday = new Date(2026, 8, 14, 23, 59, 0)

function slot(partial: Partial<ChatSlot> & { key: string }): ChatSlot {
  return { messages: 0, running: false, ...partial } as ChatSlot
}

describe('localDateKey', () => {
  it('uses local date parts, never the UTC date', () => {
    expect(localDateKey(now)).toBe('2026-09-15')
    expect(localDateKey(new Date(2026, 0, 5, 0, 30))).toBe('2026-01-05')
  })
})

describe('todaySessions', () => {
  it('keeps live slots and archived rows active today, newest first', () => {
    const out = todaySessions(
      [
        slot({ key: 'live-a', title: 'A', last_turn_ts: iso(at(9)), messages: 12 }),
        slot({ key: 'live-old', title: 'Old', last_turn_ts: iso(yesterday) }),
      ],
      [
        { key: 'closed-b', title: 'B', modified: epoch(at(11)) },
        { key: 'closed-old', title: 'Stale', modified: epoch(yesterday) },
      ],
      now,
    )
    expect(out.map(s => s.key)).toEqual(['closed-b', 'live-a'])
    expect(out[1]).toMatchObject({ live: true, messages: 12 })
    // An archived row's message count is a size estimate, so it is not carried.
    expect(out[0].messages).toBeUndefined()
  })

  it('lets the live slot win over an archived row with the same key', () => {
    const out = todaySessions(
      [slot({ key: 'same', title: 'Live title', last_turn_ts: iso(at(10)), running: true })],
      [{ key: 'same', title: 'Archived title', modified: epoch(at(8)) }],
      now,
    )
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ title: 'Live title', live: true, running: true })
  })

  it('counts a slot still running a turn that started yesterday as active now', () => {
    // `last_turn_ts` moves only when a prompt arrives or a turn ends, so an
    // overnight turn still streaming at 14:30 carries yesterday's stamp.
    const out = todaySessions(
      [
        slot({ key: 'overnight', title: 'Overnight loop', last_turn_ts: iso(yesterday), running: true }),
        slot({ key: 'idle-old', title: 'Idle since yesterday', last_turn_ts: iso(yesterday), running: false }),
        slot({ key: 'live-a', title: 'A', last_turn_ts: iso(at(9)) }),
      ],
      [],
      now,
    )
    expect(out.map(s => s.key)).toEqual(['overnight', 'live-a'])
    expect(out[0].activity).toBe(epoch(now))
  })

  it('never lists incognito or temporary sessions', () => {
    const out = todaySessions(
      [
        slot({ key: 'inc', last_turn_ts: iso(at(10)), memory_mode: 'incognito' }),
        slot({ key: 'tmp', last_turn_ts: iso(at(10)), memory_mode: 'temporary' }),
        slot({ key: 'ok', last_turn_ts: iso(at(10)), memory_mode: 'persistent' }),
      ],
      [{ key: 'inc-row', modified: epoch(at(9)), memory_mode: 'incognito' }],
      now,
    )
    expect(out.map(s => s.key)).toEqual(['ok'])
  })

  it('falls back to the key when a session has no title', () => {
    const out = todaySessions([slot({ key: 'chat-7', last_ts: iso(at(12)) })], [], now)
    expect(out[0].title).toBe('chat-7')
  })
})

describe('groupByFolder', () => {
  const folders = [{ id: 'f1', name: 'Website', order: 0 }, { id: 'f2', name: 'Planner', order: 1 }]
  const sessions = todaySessions(
    [
      slot({ key: 'a', last_turn_ts: iso(at(13)), folder_id: 'f2' }),
      slot({ key: 'b', last_turn_ts: iso(at(12)) }),
      slot({ key: 'c', last_turn_ts: iso(at(11)), folder_id: 'f1' }),
      slot({ key: 'd', last_turn_ts: iso(at(10)), folder_id: 'gone' }),
    ],
    [],
    now,
  )

  it('orders folders by their newest session and puts the unfiled group last', () => {
    const groups = groupByFolder(sessions, folders)
    expect(groups.map(g => g.name ?? '(unfiled)')).toEqual(['Planner', 'Website', '(unfiled)'])
    expect(groups[0].sessions.map(s => s.key)).toEqual(['a'])
  })

  it('treats a folder id that names no folder as unfiled', () => {
    const groups = groupByFolder(sessions, folders)
    const unfiled = groups.find(g => g.folder_id === '')
    expect(unfiled?.sessions.map(s => s.key)).toEqual(['b', 'd'])
  })
})

describe('parseHistoryEntries', () => {
  it('splits the day file into timestamped entries, newest first', () => {
    const content = [
      '# 2026-09-15',
      '',
      '#### 07:58 PDT',
      'Answered the cron timezone question.',
      '',
      '#### 09:12 PDT',
      'Rebased the Bedrock KB PR.',
      'Gates green.',
      '',
      '#### 10:00 PDT',
      '',
    ].join('\n')
    expect(parseHistoryEntries(content)).toEqual([
      { time: '09:12 PDT', text: 'Rebased the Bedrock KB PR.\nGates green.' },
      { time: '07:58 PDT', text: 'Answered the cron timezone question.' },
    ])
  })

  it('returns nothing for an empty or header-only file', () => {
    expect(parseHistoryEntries('')).toEqual([])
    expect(parseHistoryEntries('# 2026-09-15\n')).toEqual([])
  })
})
