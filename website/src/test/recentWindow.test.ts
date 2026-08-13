import { describe, it, expect } from 'vitest'
import {
  RECENT_UNIT_MS,
  RECENT_WINDOW_PRESETS,
  RECENT_TICK_MS,
  decomposeRecentWindow,
  formatRecentWindow,
  clampRecentAmount,
  customRecentWindowMs,
  recentTickIntervalMs,
  isWithinRecentWindow,
} from '../pages/recentWindow'

/**
 * Covers the pure window math behind the ChatSidebar "Recent" filter
 *. The component wiring lives in ChatSidebar.tsx, but the
 * decomposition / clamp / boundary logic is extracted here so it can be tested
 * without a full render, mirroring the sibling `unreadDrain` extraction.
 */
describe('recentWindow', () => {
  const { minutes, hours, days } = RECENT_UNIT_MS

  describe('decomposeRecentWindow', () => {
    it('picks the largest whole unit that divides the window evenly', () => {
      expect(decomposeRecentWindow(days)).toEqual({ value: 1, unit: 'days' })
      expect(decomposeRecentWindow(7 * days)).toEqual({ value: 7, unit: 'days' })
      expect(decomposeRecentWindow(6 * hours)).toEqual({ value: 6, unit: 'hours' })
      expect(decomposeRecentWindow(hours)).toEqual({ value: 1, unit: 'hours' })
      expect(decomposeRecentWindow(30 * minutes)).toEqual({ value: 30, unit: 'minutes' })
    })

    it('collapses 24 hours to 1 day (divisibility precedence)', () => {
      expect(decomposeRecentWindow(24 * hours)).toEqual({ value: 1, unit: 'days' })
    })

    it('round-trips every preset', () => {
      for (const preset of RECENT_WINDOW_PRESETS) {
        const { value, unit } = decomposeRecentWindow(preset.ms)
        expect(value * RECENT_UNIT_MS[unit]).toBe(preset.ms)
      }
    })

    it('falls back to minutes for a sub-minute window without returning 0', () => {
      expect(decomposeRecentWindow(30 * 1000)).toEqual({ value: 1, unit: 'minutes' })
    })
  })

  describe('formatRecentWindow', () => {
    it('renders a compact label per unit', () => {
      expect(formatRecentWindow(hours)).toBe('1h')
      expect(formatRecentWindow(6 * hours)).toBe('6h')
      expect(formatRecentWindow(days)).toBe('1d')
      expect(formatRecentWindow(7 * days)).toBe('7d')
      expect(formatRecentWindow(30 * minutes)).toBe('30m')
    })
  })

  describe('clampRecentAmount', () => {
    it('clamps empty / invalid / zero input to the minimum of 1', () => {
      expect(clampRecentAmount('')).toBe(1)
      expect(clampRecentAmount('0')).toBe(1)
      expect(clampRecentAmount('abc')).toBe(1)
      expect(clampRecentAmount(-5)).toBe(1)
    })

    it('clamps oversized input to the maximum of 9999', () => {
      expect(clampRecentAmount('10000')).toBe(9999)
      expect(clampRecentAmount(999999)).toBe(9999)
    })

    it('floors fractional input and keeps in-range values', () => {
      expect(clampRecentAmount('3.9')).toBe(3)
      expect(clampRecentAmount(42)).toBe(42)
    })
  })

  describe('customRecentWindowMs', () => {
    it('multiplies the clamped amount by the unit', () => {
      expect(customRecentWindowMs('2', 'hours')).toBe(2 * hours)
      expect(customRecentWindowMs('3', 'days')).toBe(3 * days)
      expect(customRecentWindowMs('90', 'minutes')).toBe(90 * minutes)
    })

    it('applies the 1..9999 clamp to empty / oversized amounts', () => {
      expect(customRecentWindowMs('', 'hours')).toBe(1 * hours)
      expect(customRecentWindowMs('100000', 'minutes')).toBe(9999 * minutes)
    })
  })

  describe('recentTickIntervalMs', () => {
    it('targets ~1/10th of the window', () => {
      expect(recentTickIntervalMs(10 * minutes)).toBe(minutes)
    })

    it('never ticks faster than every 30s', () => {
      expect(recentTickIntervalMs(minutes)).toBe(30_000)
    })

    it('never ticks slower than RECENT_TICK_MS', () => {
      expect(recentTickIntervalMs(7 * days)).toBe(RECENT_TICK_MS)
    })
  })

  describe('isWithinRecentWindow', () => {
    const now = 1_000_000_000_000

    it('is true just inside the window and false just outside', () => {
      const insideTs = new Date(now - (hours - 1000)).toISOString()
      const outsideTs = new Date(now - (hours + 1000)).toISOString()
      expect(isWithinRecentWindow(insideTs, now, hours)).toBe(true)
      expect(isWithinRecentWindow(outsideTs, now, hours)).toBe(false)
    })

    it('is true exactly on the boundary (<=)', () => {
      const boundaryTs = new Date(now - hours).toISOString()
      expect(isWithinRecentWindow(boundaryTs, now, hours)).toBe(true)
    })

    it('treats a missing or unparseable timestamp as not recent', () => {
      expect(isWithinRecentWindow(undefined, now, hours)).toBe(false)
      expect(isWithinRecentWindow('', now, hours)).toBe(false)
      expect(isWithinRecentWindow('not-a-date', now, hours)).toBe(false)
    })
  })
})
