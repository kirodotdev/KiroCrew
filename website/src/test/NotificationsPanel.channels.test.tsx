import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import { __resetForTests } from '../hooks/useNotificationSound'
import type { NotificationChannel } from '../types'

/** The channels section reads/writes through React Query, so the panel needs
 *  a client. `retry: false` so the "API fails" case settles in one round.
 *  Rendered on the Sources rail item, which is where the channels list lives. */
function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter initialEntries={['/settings?tab=notifications&sub=sources']}><QueryClientProvider client={qc}><NotificationsPanel /></QueryClientProvider></MemoryRouter>)
}

const CHANNELS: NotificationChannel[] = [
  { channel: 'system.approval', source: 'system', registered: true, default_priority: 'critical', protected: true, settings: {} },
  { channel: 'system.cron', source: 'system', registered: true, default_priority: 'default', protected: false, settings: {} },
  { channel: 'system.heartbeat', source: 'system', registered: true, default_priority: 'passive', protected: false, settings: { muted: true } },
  { channel: 'system.member', source: 'system', registered: true, default_priority: 'passive', protected: false, settings: {} },
  { channel: 'system.apps', source: 'system', registered: true, default_priority: 'default', protected: false, settings: {} },
  { channel: 'system.turn', source: 'system', registered: true, default_priority: 'default', protected: false, settings: {} },
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
    renderPanel()
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    expect(screen.getByText('system')).toBeTruthy()
    expect(screen.getByText('oncall-radar')).toBeTruthy()
    // Channel labels drop the source prefix
    expect(screen.getByText('ticket-update')).toBeTruthy()
    expect(screen.queryByText('oncall-radar.ticket-update')).toBeNull()
  })

  it('names the crewmate-message channel in words, not by its id', async () => {
    renderPanel()
    expect(await screen.findByText('Mate messages')).toBeTruthy()
    expect(screen.queryByText('member')).toBeNull()
  })

  it('names the app-update channel in words, so muting it reads as what it does', async () => {
    // The bare id "apps" reads as "every app's notifications"; muting it only
    // silences the "a newer version is published" notes.
    renderPanel()
    expect(await screen.findByText('App updates')).toBeTruthy()
    expect(screen.queryByText('apps')).toBeNull()
  })

  it('names the dead-turn channel in words, not by its "turn" id', async () => {
    // The bare id "turn" reads as the "turn" sound category (conversation
    // handoffs); the label says failure so a user knows what muting costs.
    renderPanel()
    expect(await screen.findByText('Chat errors')).toBeTruthy()
    expect(screen.queryByText('turn')).toBeNull()
  })

  it('renders protected channels locked without controls', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByText('approval')).toBeTruthy())
    expect(screen.getByText('protected')).toBeTruthy()
    expect(screen.queryByRole('switch', { name: /system\.approval/ })).toBeNull()
  })

  it('marks unregistered channels as retained', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByText('sync-status')).toBeTruthy())
    expect(screen.getByText(/setting retained/)).toBeTruthy()
  })

  it('mute toggle PUTs the setting', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    const toggle = screen.getByRole('switch', { name: /system\.cron/ })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(toggle)
    // useMutation invokes mutationFn after its async onMutate, not synchronously.
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('system.cron', { muted: true }))
  })

  it('muted channel toggle reads off and unmutes on click', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByText('heartbeat')).toBeTruthy())
    const toggle = screen.getByRole('switch', { name: /system\.heartbeat/ })
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(toggle)
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('system.heartbeat', { muted: false }))
  })

  it('a refused mute rolls back only its own channel, not a mute made while it was in flight', async () => {
    // The first read serves the list; later reads never answer, so the cache
    // shows what the rollback left rather than a refetch.
    mockChannels.mockReset().mockResolvedValueOnce({ channels: CHANNELS }).mockImplementation(() => new Promise(() => {}))
    let refuse!: (e: Error) => void
    mockUpdate.mockImplementation((channel: string) =>
      channel === 'system.cron' ? new Promise((_, reject) => { refuse = reject }) : Promise.resolve({}))
    renderPanel()
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    const cron = screen.getByRole('switch', { name: /system\.cron/ })
    const member = screen.getByRole('switch', { name: /system\.member/ })
    fireEvent.click(cron)
    fireEvent.click(member)
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('system.member', { muted: true }))
    expect(member.getAttribute('aria-checked')).toBe('false')
    refuse(new Error('cron refused'))
    await waitFor(() => expect(cron.getAttribute('aria-checked')).toBe('true'))
    expect(member.getAttribute('aria-checked')).toBe('false')
  })

  it('two refused mutes of one channel roll back to the stored value, not to the first optimistic one', async () => {
    mockChannels.mockReset().mockResolvedValueOnce({ channels: CHANNELS }).mockImplementation(() => new Promise(() => {}))
    const refusals: ((e: Error) => void)[] = []
    mockUpdate.mockImplementation(() => new Promise((_, reject) => { refusals.push(reject) }))
    renderPanel()
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    const cron = screen.getByRole('switch', { name: /system\.cron/ })
    fireEvent.click(cron)
    // The first mute shows before the second click, which then unmutes.
    await waitFor(() => expect(cron.getAttribute('aria-checked')).toBe('false'))
    fireEvent.click(cron)
    await waitFor(() => expect(refusals).toHaveLength(2))
    expect(mockUpdate.mock.calls.map(c => c[1])).toEqual([{ muted: true }, { muted: false }])
    refusals[0](new Error('first refused'))
    refusals[1](new Error('second refused'))
    // Each failure's settle asks for a refetch (which never answers here), so
    // the third read means both rollbacks have run. Both saves failed, so the
    // switch shows what the server holds: unmuted.
    await waitFor(() => expect(mockChannels).toHaveBeenCalledTimes(3))
    expect(cron.getAttribute('aria-checked')).toBe('true')
  })

  it('hides the section when the channels API fails', async () => {
    mockChannels.mockRejectedValue(new Error('boom'))
    renderPanel()
    await waitFor(() => expect(screen.getByText('Failed to load channels')).toBeTruthy())
    // Sound section still renders
    expect(screen.getByText('Sound')).toBeTruthy()
  })

})
