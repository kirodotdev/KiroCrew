import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import SessionStorageScreen from '../pages/system/SessionStorageScreen'
import type { SessionStorageReport } from '../types'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

const cleanup = vi.fn()
const empty = vi.fn()
const restore = vi.fn()
let report: SessionStorageReport

vi.mock('../api/client', () => ({
  api: {
    sessionStorage: () => Promise.resolve(report),
    sessionStorageCleanup: (...args: unknown[]) => { cleanup(...args); return Promise.resolve({ sessions: 3, bytes: 10, remaining: 0, batch_id: 'b1' }) },
    sessionStorageEmpty: (...args: unknown[]) => { empty(...args); return Promise.resolve({ freed_bytes: 10 }) },
    sessionStorageRestore: (...args: unknown[]) => { restore(...args); return Promise.resolve({ restored: 3 }) },
  },
}))

function baseReport(over: Partial<SessionStorageReport> = {}): SessionStorageReport {
  return {
    total_bytes: 30_000_000_000,
    total_sessions: 164_723,
    active_sessions: 12,
    active_bytes: 670_000_000,
    reclaimable_sessions: 164_605,
    reclaimable_bytes: 27_760_000_000,
    reclaim_blocked_reason: '',
    buckets: [
      { label: 'under_7d', sessions: 1284, bytes: 710_000_000 },
      { label: '7_30d', sessions: 42_908, bytes: 10_600_000_000 },
      { label: '30_90d', sessions: 85_961, bytes: 14_400_000_000 },
      { label: 'over_90d', sessions: 34_570, bytes: 2_800_000_000 },
    ],
    trash: { bytes: 0, still_on_disk: true, instant: true, batches: [] },
    ...over,
  }
}

function staged(): SessionStorageReport {
  return baseReport({
    trash: {
      bytes: 17_200_000_000, still_on_disk: true, instant: true,
      batches: [{
        batch_id: '20260807T145200-abcd1234', created_at: 1, reason: 'policy',
        sessions: 134_169, bytes: 17_200_000_000,
      }],
    },
  })
}

describe('SessionStorageScreen', () => {
  beforeEach(() => {
    cleanup.mockClear(); empty.mockClear(); restore.mockClear()
    report = baseReport()
    // The confirm guard compares wall-clock instants, so the clock has to be
    // controllable for "same instant" and "a second later" to be expressible.
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows every age bucket the report returns', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Under 7 days')).toBeTruthy())
    expect(screen.getByText('7 – 30 days')).toBeTruthy()
    expect(screen.getByText('30 – 90 days')).toBeTruthy()
    expect(screen.getByText('Over 90 days')).toBeTruthy()
  })

  /** The payload has no per-store split, and the screen must not invent one. */
  it('never names the two stores it is built on', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Under 7 days')).toBeTruthy())
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/kiro-cli/i)
    expect(text).not.toMatch(/two stores/i)
    expect(text).not.toMatch(/transcript/i)
  })

  it('offers the reason instead of the action when reclaiming is blocked', async () => {
    report = baseReport({ reclaim_blocked_reason: 'This instance shares its store.' })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('This instance shares its store.')).toBeTruthy())
    const move = screen.getByRole('button', { name: /Move .* to Trash/ })
    expect((move as HTMLButtonElement).disabled).toBe(true)
  })

  it('stages with the selected threshold', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Under 7 days')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Move .* to Trash/ }))
    // 30 days is the default selection, and it must reach the API as 30 — not as
    // the label, and not as the bucket index.
    expect(cleanup).toHaveBeenCalledWith(30)
  })

  /**
   * Emptying is the only irreversible step, so one click must never destroy
   * anything: the first arms, the second commits.
   */
  it('requires two clicks to empty a batch', async () => {
    report = staged()
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('20260807T145200-abcd1234')).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))
    expect(empty).not.toHaveBeenCalled()

    // Past the arm window, so this is real consent rather than a double-click.
    vi.setSystemTime(Date.now() + 1000)
    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))
    expect(empty).toHaveBeenCalledWith(['20260807T145200-abcd1234'])
  })

  /**
   * The confirm replaces the arm button, so a fast double-click would otherwise
   * land its second click on a destructive button that appeared under a
   * stationary pointer. Two independent guards; this covers the timing one.
   */
  it('ignores a confirm that arrives inside the double-click window', async () => {
    report = staged()
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('20260807T145200-abcd1234')).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))
    // No clock advance: the same instant a real double-click would deliver.
    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))
    expect(empty).not.toHaveBeenCalled()
  })

  /** And this covers the layout one: Cancel takes the vacated slot, not the confirm. */
  it('puts Cancel where the arm button was, ahead of the confirm', async () => {
    report = staged()
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('20260807T145200-abcd1234')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))

    const labels = Array.from(document.querySelectorAll('button')).map(b => b.textContent ?? '')
    const cancelAt = labels.findIndex(t => /Cancel/.test(t))
    const confirmAt = labels.findIndex(t => /Delete forever/.test(t))
    expect(cancelAt).toBeGreaterThanOrEqual(0)
    expect(cancelAt).toBeLessThan(confirmAt)
  })

  it('restores a batch without arming anything', async () => {
    report = baseReport({
      trash: {
        bytes: 100, still_on_disk: true, instant: true,
        batches: [{ batch_id: 'b1', created_at: 1, reason: 'manual', sessions: 1, bytes: 100 }],
      },
    })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('b1')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Restore/ }))
    expect(restore).toHaveBeenCalledWith('b1')
  })
})
