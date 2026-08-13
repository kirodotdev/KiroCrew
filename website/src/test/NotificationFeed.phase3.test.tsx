import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed, { SEEN_CHANNELS_STORAGE_KEY } from '../components/notifications/NotificationFeed'
import type { RootState } from '../store'
import type { Notification } from '../types'

const mockUpdateChannelSettings = vi.fn().mockResolvedValue({})

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    updateNotificationChannelSettings: (...args: unknown[]) => mockUpdateChannelSettings(...args),
  },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const mkN = (over: Partial<Notification>): Notification => ({
  kind: 'cron', ts: '2026-07-23T10:00:00Z', title: 'Note', body: 'body', acked: false, ...over,
})

function stateWith(notifs: Notification[]): Partial<RootState> {
  return { notifications: { items: notifs } as RootState['notifications'] }
}

function renderFeed(notifs: Notification[]) {
  const store = createTestStore(stateWith(notifs))
  return renderWithProviders(
    <NotificationFeed selectedTs={null} onSelect={() => {}} />, { store },
  )
}

beforeEach(() => {
  localStorage.clear()
  mockUpdateChannelSettings.mockClear()
})

describe('NotificationFeed Phase 3: silenced (muted-channel) rows', () => {
  it('hides silenced rows by default and reveals them via the Muted chip', () => {
    renderFeed([
      mkN({ ts: '1', title: 'Visible note' }),
      mkN({ ts: '2', title: 'Muted note', silenced: true, priority: 'passive' }),
    ])
    expect(screen.getByText('Visible note')).toBeTruthy()
    expect(screen.queryByText('Muted note')).toBeNull()

    const chip = screen.getByRole('button', { name: /Muted \(1\)/ })
    expect(chip.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(chip)
    expect(screen.getByText('Muted note')).toBeTruthy()
    fireEvent.click(chip)
    expect(screen.queryByText('Muted note')).toBeNull()
  })

  it('does not render the Muted chip when nothing is silenced', () => {
    renderFeed([mkN({ ts: '1', title: 'Visible note' })])
    expect(screen.queryByRole('button', { name: /Muted \(/ })).toBeNull()
  })
})

describe('NotificationFeed Phase 3: priority tiers', () => {
  it('marks unacked critical rows with a danger dot instead of the accent dot', () => {
    const { container } = renderFeed([mkN({ ts: '1', title: 'Approval needed', kind: 'approval', priority: 'critical' })])
    const dot = container.querySelector('[data-priority="critical"]')
    expect(dot).toBeTruthy()
    expect(dot!.className).toContain('bg-danger')
    expect(dot!.className).not.toContain('bg-accent')
  })

  it('unacked default rows keep the accent dot', () => {
    const { container } = renderFeed([mkN({ ts: '1', title: 'Digest ready' })])
    const dot = container.querySelector('[data-priority="default"]')
    expect(dot).toBeTruthy()
    expect(dot!.className).toContain('bg-accent')
  })

  it('acked critical rows show no dot', () => {
    const { container } = renderFeed([mkN({ ts: '1', title: 'Approval needed', priority: 'critical', acked: true })])
    expect(container.querySelector('[data-priority]')).toBeNull()
  })
})

describe('NotificationFeed Phase 3: keep/mute prompt', () => {
  const appNote = mkN({
    ts: '1', title: 'Ticket escalated', kind: 'agent',
    source: 'oncall-radar', channel: 'oncall-radar.ticket-update',
  })

  it('prompts on the first notification from a new app channel', () => {
    renderFeed([appNote])
    expect(screen.getByText(/Keep receiving these\?/)).toBeTruthy()
    expect(screen.getByText('oncall-radar / ticket-update')).toBeTruthy()
  })

  it('never prompts for system-source notifications', () => {
    renderFeed([mkN({ ts: '1', source: 'system', channel: 'system.cron' })])
    expect(screen.queryByText(/Keep receiving these\?/)).toBeNull()
  })

  it('Keep dismisses the prompt and persists the decision', () => {
    renderFeed([appNote])
    fireEvent.click(screen.getByRole('button', { name: 'Keep' }))
    expect(screen.queryByText(/Keep receiving these\?/)).toBeNull()
    expect(JSON.parse(localStorage.getItem(SEEN_CHANNELS_STORAGE_KEY) || '[]'))
      .toContain('oncall-radar.ticket-update')
    expect(mockUpdateChannelSettings).not.toHaveBeenCalled()
  })

  it('Mute channel calls the settings API and dismisses', () => {
    renderFeed([appNote])
    fireEvent.click(screen.getByRole('button', { name: 'Mute channel' }))
    expect(screen.queryByText(/Keep receiving these\?/)).toBeNull()
    expect(mockUpdateChannelSettings).toHaveBeenCalledWith('oncall-radar.ticket-update', { muted: true })
  })

  it('does not prompt again for an already-decided channel', () => {
    localStorage.setItem(SEEN_CHANNELS_STORAGE_KEY, JSON.stringify(['oncall-radar.ticket-update']))
    renderFeed([appNote])
    expect(screen.queryByText(/Keep receiving these\?/)).toBeNull()
  })
})
