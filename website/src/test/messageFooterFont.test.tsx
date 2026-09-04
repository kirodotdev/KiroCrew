import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'
import { fmtMessageTime, fmtMessageTimeFull } from '../pages/chat/messageTime'

/**
 * The message footer is CHROME, so it must follow Settings → Display → Font
 * Family. Tailwind's `font-mono` resolves to `var(--mono)` — a token that
 * setting never writes — so any hardcoded `font-mono` here silently overrode the
 * user's choice and pinned JetBrains Mono, which has no CJK coverage, under a
 * date string that a zh/ja dashboard renders WITH CJK characters.
 *
 * `tabular-nums` is the part that must SURVIVE: fixed-width digits were the real
 * reason mono looked right here, and `font-variant-numeric` delivers them in a
 * proportional face too.
 */
describe('message footer follows the Font Family setting', () => {
  it('does not pin the turn-stats line to font-mono', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 59_000, credits: 1.98 }} />)
    const stats = screen.getByTestId('turn-stats')
    expect(stats.className).not.toContain('font-mono')
    // Guards against asserting on an empty node: the line still renders.
    expect(stats).toHaveTextContent('1.98 credits')
    expect(stats).toHaveTextContent('59s')
  })

  it('keeps tabular-nums on the turn-stats line so digits stay fixed-width', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 59_000, credits: 1.98 }} />)
    expect(screen.getByTestId('turn-stats').className).toContain('tabular-nums')
  })

  it('does not pin the timestamp to font-mono, and titles it with the full date', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} timestamp="Aug 6, 05:52 PM" timestampTitle="Thursday, August 6, 2026, 05:52 PM" />)
    const stamp = screen.getByText('Aug 6, 05:52 PM')
    expect(stamp.className).not.toContain('font-mono')
    expect(stamp.className).toContain('tabular-nums')
    expect(stamp).toHaveAttribute('title', 'Thursday, August 6, 2026, 05:52 PM')
  })

  it('does not pin the variant counter to font-mono either', () => {
    // The counter shares the hover row with the timestamp, so fixing only the
    // timestamp would leave half of one row ignoring the Font Family setting.
    render(<AssistantMessage
      content="v2" isStreaming={false} slotRunning={false}
      variants={[{ content: 'v1' }, { content: 'v2' }, { content: 'v3' }]}
      variantIdx={1}
    />)
    const counter = screen.getByText('2/3')
    expect(counter.className).not.toContain('font-mono')
    expect(counter.className).toContain('tabular-nums')
  })
})

/**
 * The year is elided for the current year and KEPT for any other, so a
 * scrolled-back or imported message can never be misread as this year's. The
 * clock is faked rather than asserting against `new Date()` so the "current
 * year" branch is pinned to a value the test controls.
 */
describe('fmtMessageTime — year elision', () => {
  afterEach(() => { vi.useRealTimers() })

  const withNow = (iso: string, fn: () => void) => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(iso))
    try { fn() } finally { vi.useRealTimers() }
  }

  it('omits the year for a message from the current year', () => {
    withNow('2026-08-06T12:00:00.000Z', () => {
      const out = fmtMessageTime('2026-08-06T17:52:00.000Z')
      expect(out).not.toContain('2026')
      // The rest of the fields survive the elision.
      expect(out).toMatch(/Aug/)
      expect(out).toMatch(/\d{1,2}:\d{2}/)
    })
  })

  it('keeps the year for a message from an earlier year', () => {
    withNow('2026-08-06T12:00:00.000Z', () => {
      expect(fmtMessageTime('2025-11-02T17:52:00.000Z')).toContain('2025')
    })
  })

  it('keeps the year for a message dated in a LATER year (clock skew)', () => {
    withNow('2026-08-06T12:00:00.000Z', () => {
      expect(fmtMessageTime('2027-01-04T09:00:00.000Z')).toContain('2027')
    })
  })

  it('returns empty for a missing or unparseable timestamp', () => {
    expect(fmtMessageTime(undefined)).toBe('')
    expect(fmtMessageTime('')).toBe('')
    expect(fmtMessageTime('not a date')).toBe('')
    expect(fmtMessageTimeFull(undefined)).toBe('')
  })

  it('always spells the year out in the hover title, even for the current year', () => {
    withNow('2026-08-06T12:00:00.000Z', () => {
      const full = fmtMessageTimeFull('2026-08-06T17:52:00.000Z')
      expect(full).toContain('2026')
      // The elided display and the full title disagree — that is the point.
      expect(full).not.toBe(fmtMessageTime('2026-08-06T17:52:00.000Z'))
    })
  })
})
