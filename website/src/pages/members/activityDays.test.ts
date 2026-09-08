import { describe, expect, it } from 'vitest'
import { activityDayLabel, floorCountText, groupActivityDays, projectLabel } from './activityDays'

/** Local midnight `daysAgo` days back, plus `hours`, as epoch seconds. */
function at(daysAgo: number, hours: number): number {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  d.setDate(d.getDate() - daysAgo)
  return d.getTime() / 1000 + hours * 3600
}

describe('groupActivityDays', () => {
  it('folds entries into local calendar days, newest day first, entries oldest first', () => {
    const days = groupActivityDays(
      [
        { ts: at(0, 9), via: 'chat', project: '/a' },
        { ts: at(2, 8), via: 'chat', project: '/a' },
        { ts: at(0, 12), via: 'select_crew', project: '/b' },
        { ts: at(2, 14), via: 'chat', project: '/b' },
        { ts: at(2, 11), via: 'chat', project: '/a' },
      ],
      false,
    )
    expect(days.map((d) => d.dayStart)).toEqual([at(0, 0), at(2, 0)])
    expect(days[0]).toMatchObject({ chats: 1, routed: 1, projects: ['/a', '/b'], isFloor: false })
    expect(days[1]).toMatchObject({ chats: 3, routed: 0, projects: ['/a', '/b'] })
    expect(days[1].entries.map((e) => e.ts)).toEqual([at(2, 8), at(2, 11), at(2, 14)])
  })

  it('marks only the day holding the oldest returned entry as a floor when the server capped', () => {
    const entries = [
      { ts: at(0, 9), via: 'chat' },
      { ts: at(1, 9), via: 'chat' },
      { ts: at(1, 7), via: 'chat' },
    ]
    expect(groupActivityDays(entries, false).map((d) => d.isFloor)).toEqual([false, false])
    expect(groupActivityDays(entries, true).map((d) => d.isFloor)).toEqual([false, true])
  })

  it('returns nothing for an empty log', () => {
    expect(groupActivityDays([], true)).toEqual([])
  })
})

describe('activityDayLabel', () => {
  it('words the nearest days relatively and older ones by weekday or date', () => {
    const now = new Date(2026, 8, 8, 21, 30) // Tue Sep 8 2026, local
    const midnight = (daysAgo: number) => {
      const d = new Date(now)
      d.setHours(0, 0, 0, 0)
      d.setDate(d.getDate() - daysAgo)
      return d.getTime() / 1000
    }
    expect(activityDayLabel(midnight(0), now)).toMatch(/today/i)
    expect(activityDayLabel(midnight(1), now)).toMatch(/yesterday/i)
    expect(activityDayLabel(midnight(2), now)).toMatch(/2 days ago/i)
    // Within the week: a weekday name, not a relative phrase.
    expect(activityDayLabel(midnight(4), now)).toMatch(/^Fri/)
    // Older: month + day.
    expect(activityDayLabel(midnight(9), now)).toMatch(/Aug 30/)
  })
})

describe('floorCountText', () => {
  it('qualifies the interpolated number and leaves the rest of the phrase alone', () => {
    expect(floorCountText('3 chats', 3, 2)).toBe('2+ chats')
    // A floor of one is rendered from the plural phrase (shown = 2) so it reads "1+ chats".
    expect(floorCountText('2 chats', 2, 1)).toBe('1+ chats')
    expect(floorCountText('对话 13 次', 13, 12)).toBe('对话 12+ 次')
    expect(floorCountText('no digits', 4, 3)).toBe('no digits')
  })
})

describe('projectLabel', () => {
  it('keeps the last path segment, tolerating trailing separators and Windows paths', () => {
    expect(projectLabel('/local/home/u/.kiro/crew/workspace')).toBe('workspace')
    expect(projectLabel('/srv/kirocrew/')).toBe('kirocrew')
    expect(projectLabel('C:\\work\\Reports')).toBe('Reports')
    expect(projectLabel('plain')).toBe('plain')
  })
})
