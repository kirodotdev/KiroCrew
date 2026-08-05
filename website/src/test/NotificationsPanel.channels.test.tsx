import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor } from '@testing-library/react'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import { __resetForTests } from '../hooks/useNotificationSound'
import type { NotificationChannel } from '../types'

const CHANNELS: NotificationChannel[] = [
  { channel: 'system.approval', source: 'system', registered: true, default_priority: 'critical', protected: true, settings: {} },
  { channel: 'system.cron', source: 'system', registered: true, default_priority: 'default', protected: false, settings: {} },
  { channel: 'system.heartbeat', source: 'system', registered: true, default_priority: 'passive', protected: false, settings: { muted: true } },
  { channel: 'oncall-radar.ticket-update', source: 'oncall-radar', registered: true, default_priority: 'default', protected: false, settings: { priority: 'critical' } },
  { channel: 'oncall-radar.sync-status', source: 'oncall-radar', registered: false, default_priority: null, protected: false, settings: { muted: true } },
]

const mockChannels = vi.fn()
const mockUpdate = vi.fn().mockResolvedValue({})

vi.mock('../api/client', () => ({
  api: {
    notificationChannels: (...args: unknown[]) => mockChannels(...args),
    updateNotificationChannelSettings: (...args: unknown[]) => mockUpdate(...args),
  },
}))

beforeEach(() => {
  localStorage.clear()
  __resetForTests()
  mockChannels.mockReset().mockResolvedValue({ channels: CHANNELS })
  mockUpdate.mockClear()
  ;(window as unknown as { AudioContext: unknown }).AudioContext = vi.fn(() => ({
    state: 'running', currentTime: 0, destination: {},
    resume: vi.fn(() => Promise.resolve()),
    createOscillator: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), type: '', frequency: { value: 0 }, onended: null })),
    createGain: vi.fn(() => ({ gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() }, connect: vi.fn(), disconnect: vi.fn() })),
  }))
})

describe('NotificationsPanel channels section', () => {
  it('groups channels by source with system first', async () => {
    render(<NotificationsPanel />)
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    expect(screen.getByText('system')).toBeTruthy()
    expect(screen.getByText('oncall-radar')).toBeTruthy()
    // Channel labels drop the source prefix
    expect(screen.getByText('ticket-update')).toBeTruthy()
    expect(screen.queryByText('oncall-radar.ticket-update')).toBeNull()
  })

  it('renders protected channels locked without controls', async () => {
    render(<NotificationsPanel />)
    await waitFor(() => expect(screen.getByText('approval')).toBeTruthy())
    expect(screen.getByText('protected')).toBeTruthy()
    expect(screen.queryByRole('switch', { name: /system\.approval/ })).toBeNull()
  })

  it('marks unregistered channels as retained', async () => {
    render(<NotificationsPanel />)
    await waitFor(() => expect(screen.getByText('sync-status')).toBeTruthy())
    expect(screen.getByText(/setting retained/)).toBeTruthy()
  })

  it('mute toggle PUTs the setting', async () => {
    render(<NotificationsPanel />)
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    const toggle = screen.getByRole('switch', { name: /system\.cron/ })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(toggle)
    expect(mockUpdate).toHaveBeenCalledWith('system.cron', { muted: true })
  })

  it('muted channel toggle reads off and unmutes on click', async () => {
    render(<NotificationsPanel />)
    await waitFor(() => expect(screen.getByText('heartbeat')).toBeTruthy())
    const toggle = screen.getByRole('switch', { name: /system\.heartbeat/ })
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(toggle)
    expect(mockUpdate).toHaveBeenCalledWith('system.heartbeat', { muted: false })
  })

  it('hides the section when the channels API fails', async () => {
    mockChannels.mockRejectedValue(new Error('boom'))
    render(<NotificationsPanel />)
    await waitFor(() => expect(screen.getByText('Failed to load channels')).toBeTruthy())
    // Sound section still renders
    expect(screen.getByText('Sound')).toBeTruthy()
  })

})
