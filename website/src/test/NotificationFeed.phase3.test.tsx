import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed, { SEEN_CHANNELS_STORAGE_KEY } from '../components/notifications/NotificationFeed'
import type { RootState } from '../store'
import { addNotification } from '../store/notificationsSlice'
import type { Notification } from '../types'
import { approvalNotificationBody } from '../lib/approvalNotificationBody'

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
  it('hides silenced rows by default and reveals them via the Show muted chip', () => {
    renderFeed([
      mkN({ ts: '1', title: 'Visible note' }),
      mkN({ ts: '2', title: 'Muted note', silenced: true, priority: 'passive' }),
    ])
    expect(screen.getByText('Visible note')).toBeTruthy()
    expect(screen.queryByText('Muted note')).toBeNull()

    const chip = screen.getByRole('button', { name: /Show muted \(1\)/ })
    expect(chip.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(chip)
    expect(screen.getByText('Muted note')).toBeTruthy()
    fireEvent.click(chip)
    expect(screen.queryByText('Muted note')).toBeNull()
  })

  // The label used to read "Muted (1)" in both states, which named the rows'
  // state instead of the press's effect: a first-time user could not tell
  // whether pressing reveals muted rows or mutes something. Each state must name
  // the action the press performs.
  it('names the action, so the label flips with the disclosure state', () => {
    renderFeed([
      mkN({ ts: '1', title: 'Visible note' }),
      mkN({ ts: '2', title: 'Muted note', silenced: true, priority: 'passive' }),
    ])
    const chip = screen.getByRole('button', { name: /muted \(1\)/i })
    expect(chip.textContent).toContain('Show muted (1)')
    expect(chip.textContent).not.toContain('Hide')

    fireEvent.click(chip)
    expect(chip.textContent).toContain('Hide muted (1)')
    expect(chip.textContent).not.toContain('Show')

    fireEvent.click(chip)
    expect(chip.textContent).toContain('Show muted (1)')
  })

  it('does not render the Muted chip when nothing is silenced', () => {
    renderFeed([mkN({ ts: '1', title: 'Visible note' })])
    expect(screen.queryByRole('button', { name: /muted \(/i })).toBeNull()
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

  it('Mute channel calls the settings API and dismisses once it lands', async () => {
    renderFeed([appNote])
    fireEvent.click(screen.getByRole('button', { name: 'Mute channel' }))
    expect(mockUpdateChannelSettings).toHaveBeenCalledWith('oncall-radar.ticket-update', { muted: true })
    await waitFor(() => expect(screen.queryByText(/Keep receiving these\?/)).toBeNull())
    expect(JSON.parse(localStorage.getItem(SEEN_CHANNELS_STORAGE_KEY) || '[]'))
      .toContain('oncall-radar.ticket-update')
  })

  it('keeps the prompt and the channel undecided while the mute is in flight', () => {
    mockUpdateChannelSettings.mockReturnValueOnce(new Promise(() => {}))
    renderFeed([appNote])
    fireEvent.click(screen.getByRole('button', { name: 'Mute channel' }))
    expect(screen.getByText(/Keep receiving these\?/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Mute channel' }) as HTMLButtonElement).disabled).toBe(true)
    // Keep waits too, so a second answer cannot race the pending mute.
    expect((screen.getByRole('button', { name: 'Keep' }) as HTMLButtonElement).disabled).toBe(true)
    expect(localStorage.getItem(SEEN_CHANNELS_STORAGE_KEY)).toBeNull()
  })

  it('a failed mute keeps the prompt, shows an error, and a retry can land', async () => {
    mockUpdateChannelSettings.mockRejectedValueOnce(new Error('503'))
    renderFeed([appNote])
    fireEvent.click(screen.getByRole('button', { name: 'Mute channel' }))
    expect(await screen.findByText("Couldn't mute this channel. Try again.")).toBeTruthy()
    expect(screen.getByText(/Keep receiving these\?/)).toBeTruthy()
    expect(localStorage.getItem(SEEN_CHANNELS_STORAGE_KEY)).toBeNull()

    const retry = screen.getByRole('button', { name: 'Mute channel' }) as HTMLButtonElement
    expect(retry.disabled).toBe(false)
    fireEvent.click(retry)
    await waitFor(() => expect(screen.queryByText(/Keep receiving these\?/)).toBeNull())
    expect(screen.queryByText("Couldn't mute this channel. Try again.")).toBeNull()
    expect(mockUpdateChannelSettings).toHaveBeenCalledTimes(2)
    expect(JSON.parse(localStorage.getItem(SEEN_CHANNELS_STORAGE_KEY) || '[]'))
      .toContain('oncall-radar.ticket-update')
  })

  // Two new app channels: A's note is older, B's is newer.
  const noteA = mkN({ ts: '2026-07-23T10:00:00Z', title: 'Alpha note', kind: 'agent', source: 'app-a', channel: 'app-a.events' })
  const noteB = mkN({ ts: '2026-07-23T11:00:00Z', title: 'Beta note', kind: 'agent', source: 'app-b', channel: 'app-b.events' })
  const promptFor = (label: string) => screen.queryAllByText(label).length > 0

  it("another channel's mute does not unlock a pending one", () => {
    mockUpdateChannelSettings
      .mockReturnValueOnce(new Promise(() => {}))
      .mockReturnValueOnce(new Promise(() => {}))
    const store = createTestStore(stateWith([noteA]))
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Mute channel' }))
    // B arrives while A's save is pending; both prompts are shown.
    act(() => { store.dispatch(addNotification(noteB)) })
    const muteButtons = () => screen.getAllByRole('button', { name: 'Mute channel' }) as HTMLButtonElement[]
    expect(muteButtons()).toHaveLength(2)
    // Mute B too; A's Keep and Mute must stay locked.
    fireEvent.click(muteButtons().find(b => !b.disabled)!)
    const keeps = screen.getAllByRole('button', { name: 'Keep' }) as HTMLButtonElement[]
    expect(keeps.every(b => b.disabled)).toBe(true)
    expect(muteButtons().every(b => b.disabled)).toBe(true)
  })

  it("a newer channel's note does not hide a failed mute", async () => {
    let fail: (e: unknown) => void = () => {}
    mockUpdateChannelSettings.mockReturnValueOnce(new Promise((_, rej) => { fail = rej }))
    const store = createTestStore(stateWith([noteA]))
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} />, { store })
    expect(promptFor('app-a / events')).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Mute channel' }))
    act(() => { store.dispatch(addNotification(noteB)) })
    await act(async () => { fail(new Error('503')) })
    expect(await screen.findByText("Couldn't mute this channel. Try again.")).toBeTruthy()
    expect(promptFor('app-a / events')).toBe(true)
    expect(promptFor('app-b / events')).toBe(true)
    // Keep on B leaves A's error and retry in place.
    const keepB = screen.getAllByRole('button', { name: 'Keep' })[0]
    fireEvent.click(keepB)
    expect(promptFor('app-b / events')).toBe(false)
    expect(promptFor('app-a / events')).toBe(true)
    expect(screen.getByText("Couldn't mute this channel. Try again.")).toBeTruthy()
  })

  it('records a mute that lands after the feed unmounted', async () => {
    let land: (v: unknown) => void = () => {}
    mockUpdateChannelSettings.mockReturnValueOnce(new Promise(r => { land = r }))
    const { unmount } = renderFeed([appNote])
    fireEvent.click(screen.getByRole('button', { name: 'Mute channel' }))
    unmount()
    land({})
    await waitFor(() => expect(JSON.parse(localStorage.getItem(SEEN_CHANNELS_STORAGE_KEY) || '[]'))
      .toContain('oncall-radar.ticket-update'))
  })

  it('does not prompt again for an already-decided channel', () => {
    localStorage.setItem(SEEN_CHANNELS_STORAGE_KEY, JSON.stringify(['oncall-radar.ticket-update']))
    renderFeed([appNote])
    expect(screen.queryByText(/Keep receiving these\?/)).toBeNull()
  })
})

describe('NotificationFeed approval excerpts', () => {
  it.each(['panel', 'mac'] as const)('spends the %s excerpt budget on content, not fences', variant => {
    const command = 'echo ```; rm -rf *cache*; echo ' + 'x'.repeat(160)
    // Not an approval: every approval row renders its whole body, so the
    // excerpt path is exercised on another kind carrying the same fenced body.
    const store = createTestStore(stateWith([
      mkN({ kind: 'cron', body: approvalNotificationBody('agent', command) }),
    ]))
    const { container } = renderWithProviders(
      <NotificationFeed selectedTs={null} onSelect={() => {}} variant={variant} />, { store },
    )
    const limit = variant === 'mac' ? 140 : 80
    const preview = `Source: agent · ${command}`.slice(0, limit)
    expect(screen.getByText(preview).textContent).toBe(preview)
    // The command's own triple run stays; neither four-backtick wrapper leaks.
    expect(container.textContent).not.toContain('````')
    expect(preview).toHaveLength(limit)
  })
})
