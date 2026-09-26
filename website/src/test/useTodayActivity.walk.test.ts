//
// The closed-session page walk behind the Overview "Today" card. One fixed
// page would silently truncate the count on a busy day; the walk reads
// newest-first pages until one ends before today, the endpoint reports no
// more rows, or the page bound stops it.
//
import { describe, it, expect, vi } from 'vitest'
import { fetchClosedRowsForToday } from '../pages/overview/useTodayActivity'
import { reachesBeforeToday, type ClosedSessionRow } from '../pages/overview/todayActivity'

const now = new Date(2026, 8, 15, 14, 30, 0)
const epoch = (d: Date) => Math.floor(d.getTime() / 1000)
const today = epoch(new Date(2026, 8, 15, 9, 0, 0))
const yesterday = epoch(new Date(2026, 8, 14, 23, 59, 0))

function rows(n: number, modified: number, prefix = 'k'): ClosedSessionRow[] {
  return Array.from({ length: n }, (_, i) => ({ key: `${prefix}${i}`, modified }))
}

describe('reachesBeforeToday', () => {
  it('is true only when the last row of a page predates today', () => {
    expect(reachesBeforeToday([...rows(2, today), ...rows(1, yesterday)], now)).toBe(true)
    expect(reachesBeforeToday(rows(3, today), now)).toBe(false)
    expect(reachesBeforeToday([], now)).toBe(false)
  })
})

describe('fetchClosedRowsForToday', () => {
  it('reads a second page when the first is all today and more rows exist', async () => {
    const list = vi.fn()
      .mockResolvedValueOnce({ sessions: rows(200, today, 'a'), has_more: true })
      .mockResolvedValueOnce({ sessions: [...rows(5, today, 'b'), ...rows(3, yesterday, 'c')], has_more: true })
    const out = await fetchClosedRowsForToday(now, list)
    expect(out).toHaveLength(208)
    // The second page ends before today, so the third is never requested.
    expect(list).toHaveBeenCalledTimes(2)
    expect(list).toHaveBeenNthCalledWith(1, 200, 0, false, true)
    expect(list).toHaveBeenNthCalledWith(2, 200, 200, false, true)
  })

  it('stops when the endpoint reports no more rows', async () => {
    const list = vi.fn().mockResolvedValueOnce({ sessions: rows(3, today), has_more: false })
    expect(await fetchClosedRowsForToday(now, list)).toHaveLength(3)
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('accepts a bare row array and treats it as the only page', async () => {
    const list = vi.fn().mockResolvedValueOnce(rows(4, today))
    expect(await fetchClosedRowsForToday(now, list)).toHaveLength(4)
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('stops at the page bound even when every row is still today', async () => {
    const list = vi.fn().mockResolvedValue({ sessions: rows(200, today), has_more: true })
    const out = await fetchClosedRowsForToday(now, list)
    expect(list).toHaveBeenCalledTimes(10)
    expect(out).toHaveLength(2000)
  })
})
