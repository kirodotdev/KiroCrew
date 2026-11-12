import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor, within, act } from '@testing-library/react'
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
  mockUpdate.mockReset().mockResolvedValue({})
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
    expect(await screen.findByText('Bot messages')).toBeTruthy()
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

  it('keeps only the mute consequence under the Sources heading; what the section is sits in its tip', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    expect(screen.getByText('Muted notifications stay in history but never badge, sound, or banner.')).toBeTruthy()
    expect(screen.queryByText(/Mute notification sources or override their priority/)).toBeNull()
    expect(screen.getByTitle(/^Mute notification sources or override their priority\./)).toBeTruthy()
  })

  it('renders protected channels locked without controls', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByText('approval')).toBeTruthy())
    expect(screen.getByText('protected')).toBeTruthy()
    expect(screen.queryByRole('switch', { name: /system\.approval/ })).toBeNull()
  })

  it('explains when each system source fires, and leaves app rows without a tip', async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    const tips = screen.getAllByRole('button', { name: 'More information' })
    // Six system rows in the fixture, each with a tip, plus the Sources section's
    // own hint (#31472); the two app rows get none.
    expect(tips).toHaveLength(7)
    const cronRow = screen.getByText('cron').closest('div') as HTMLElement
    const cronTip = within(cronRow).getByRole('button', { name: 'More information' })
    expect(cronTip.getAttribute('title')).toBe(
      'When a scheduled job finishes or fails. If it keeps giving the same result or error, each repeat is still noted here, marked as a repeat.',
    )
    // Protected row: the tip sits after the lock and the protected text is unchanged.
    const approvalRow = screen.getByText('approval').closest('div') as HTMLElement
    expect(within(approvalRow).getByRole('button', { name: 'More information' }).getAttribute('title'))
      .toBe('When an agent is stopped until you approve a tool it wants to use.')
    expect(screen.getByText('Always interrupts — cannot be muted or lowered')).toBeTruthy()
    const appRow = screen.getByText('ticket-update').closest('div') as HTMLElement
    expect(within(appRow).queryByRole('button', { name: 'More information' })).toBeNull()
  })

  it('shows no tip for a system channel this build does not know', async () => {
    mockChannels.mockResolvedValue({ channels: [
      { channel: 'system.future', source: 'system', registered: true, default_priority: 'default', protected: false, settings: {} },
    ] })
    renderPanel()
    await waitFor(() => expect(screen.getByText('future')).toBeTruthy())
    const futureRow = screen.getByText('future').closest('div') as HTMLElement
    expect(within(futureRow).queryByRole('button', { name: 'More information' })).toBeNull()
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

  it('sends a second save of one channel only once the first is answered, so the later click is stored', async () => {
    // Two overlapping saves could reach the server in either order, and the
    // earlier click would then be the value it keeps.
    let answerFirst!: () => void
    mockUpdate.mockImplementationOnce(() => new Promise(resolve => { answerFirst = () => resolve({}) }))
    renderPanel()
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    const cron = screen.getByRole('switch', { name: /system\.cron/ })
    fireEvent.click(cron)
    await waitFor(() => expect(cron.getAttribute('aria-checked')).toBe('false'))
    fireEvent.click(cron)
    // The unmute shows at once; its save waits behind the mute's.
    await waitFor(() => expect(cron.getAttribute('aria-checked')).toBe('true'))
    expect(mockUpdate).toHaveBeenCalledTimes(1)
    await act(async () => { answerFirst() })
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(2))
    expect(mockUpdate.mock.calls.map(c => c[1])).toEqual([{ muted: true }, { muted: false }])
  })

  it('re-reads the list only once a channel\'s queued saves have all answered', async () => {
    // A read after the first answer would beat the second save to the server
    // and put the first click back on screen.
    let answered = 0
    const answers: (() => void)[] = []
    mockUpdate.mockImplementation(() => new Promise(resolve => { answers.push(() => { answered += 1; resolve({}) }) }))
    const answeredAtRead: number[] = []
    mockChannels.mockReset().mockResolvedValueOnce({ channels: CHANNELS })
      .mockImplementation(async () => { answeredAtRead.push(answered); return { channels: CHANNELS } })
    renderPanel()
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    const cron = screen.getByRole('switch', { name: /system\.cron/ })
    fireEvent.click(cron)
    await waitFor(() => expect(cron.getAttribute('aria-checked')).toBe('false'))
    fireEvent.click(cron)
    await waitFor(() => expect(cron.getAttribute('aria-checked')).toBe('true'))
    await act(async () => { answers[0]() })
    await waitFor(() => expect(answers).toHaveLength(2))
    await act(async () => { answers[1]() })
    await waitFor(() => expect(answeredAtRead.length).toBeGreaterThan(0))
    expect(answeredAtRead).toEqual([2])
  })

  it('re-reads the list after a save that lands while an earlier save\'s read is still out', async () => {
    // That earlier read may have been answered before the save landed, so only
    // a fresh read shows what the server keeps. The first save's read never
    // answers here, which keeps that save pending while the second one lands.
    mockChannels.mockReset().mockResolvedValueOnce({ channels: CHANNELS })
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockResolvedValue({ channels: CHANNELS })
    renderPanel()
    await waitFor(() => expect(screen.getByText('cron')).toBeTruthy())
    const cron = screen.getByRole('switch', { name: /system\.cron/ })
    fireEvent.click(cron)
    await waitFor(() => expect(cron.getAttribute('aria-checked')).toBe('false'))
    await waitFor(() => expect(mockChannels).toHaveBeenCalledTimes(2))
    fireEvent.click(cron)
    await waitFor(() => expect(mockChannels).toHaveBeenCalledTimes(3))
    expect(mockUpdate.mock.calls.map(c => c[1])).toEqual([{ muted: true }, { muted: false }])
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
    await waitFor(() => expect(cron.getAttribute('aria-checked')).toBe('true'))
    // The second save goes out only once the first is answered.
    refusals[0](new Error('first refused'))
    await waitFor(() => expect(refusals).toHaveLength(2))
    expect(mockUpdate.mock.calls.map(c => c[1])).toEqual([{ muted: true }, { muted: false }])
    refusals[1](new Error('second refused'))
    // Only the last save's settle asks for a refetch (which never answers
    // here), so the second read means both rollbacks have run. Both saves
    // failed, so the switch shows what the server holds: unmuted.
    await waitFor(() => expect(mockChannels).toHaveBeenCalledTimes(2))
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
