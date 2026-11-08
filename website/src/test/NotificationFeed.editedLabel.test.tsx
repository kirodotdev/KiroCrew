import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed from '../components/notifications/NotificationFeed'
import { fmtRelativeMinute, fmtTime } from '../components/notifications/notifMeta'
import type { RootState } from '../store'
import type { Notification } from '../types'
import '../i18n/all'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
  },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

// Every note lands in the "Today" group: the clock sits 30 s after the latest.
const NOW = '2026-07-23T10:01:30Z'
const REPLACED_TS = '2026-07-23T10:01:00Z'

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

const mkN = (over: Partial<Notification>): Notification => ({
  kind: 'system', source: 'system', ts: '2026-07-23T10:00:00Z', title: 'Note', body: 'body', acked: false, ...over,
})

function rowOf(title: string): HTMLElement {
  const row = screen.getByText(title).closest('[data-notif-row]')
  if (!(row instanceof HTMLElement)) throw new Error(`no feed row holds "${title}"`)
  return row
}

/** The bell shows a relative time, the page a clock time. */
const timeText = (variant: 'panel' | 'mac', ts: string) => (variant === 'mac' ? fmtRelativeMinute(ts) : fmtTime(ts))

/** The "edited" label's own line: it must hold just the label, then the time. */
function expectOnTimeLine(row: HTMLElement, variant: 'panel' | 'mac', ts: string) {
  const label = within(row).getByText('edited')
  const time = within(row).getByText(timeText(variant, ts))
  expect(Array.from(label.parentElement?.children ?? [])).toEqual([label, time])
}

describe.each(['panel', 'mac'] as const)('NotificationFeed (%s): a keyed replacement', variant => {
  it('says it was edited on the time line, and an unkeyed note does not', () => {
    const store = createTestStore({
      notifications: {
        items: [
          mkN({ ts: REPLACED_TS, title: 'Build status', key: 'build', replaced: true }),
          mkN({ ts: '2026-07-23T10:00:00Z', title: 'Plain note' }),
        ],
      } as RootState['notifications'],
    })
    renderWithProviders(<NotificationFeed variant={variant} selectedTs={null} onSelect={() => {}} />, { store })
    expectOnTimeLine(rowOf('Build status'), variant, REPLACED_TS)
    expect(within(rowOf('Plain note')).queryByText('edited')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open edited notification: Build status' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open notification: Plain note' })).toBeTruthy()
  })
})

describe('NotificationFeed (mac): a keyed replacement heading a collapsed deck', () => {
  it('says it was edited on the time line, apart from the stack count', () => {
    const store = createTestStore({
      notifications: {
        // The store holds notes oldest first, so the replacement is the deck's head.
        items: [
          mkN({ ts: '2026-07-23T10:00:00Z', channel: 'system.agent', title: 'Plain note' }),
          mkN({ ts: REPLACED_TS, channel: 'system.agent', title: 'Build status', key: 'build', replaced: true }),
        ],
      } as RootState['notifications'],
    })
    renderWithProviders(<NotificationFeed variant="mac" selectedTs={null} onSelect={() => {}} />, { store })
    const head = rowOf('Build status')
    const count = within(head).getByText('2')
    // The label's line is not the column that holds the count.
    expect(within(head).getByText('edited').parentElement?.contains(count)).toBe(false)
    expectOnTimeLine(head, 'mac', REPLACED_TS)
  })
})
