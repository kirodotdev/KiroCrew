/**
 * `CrewCompanionPage` — the builtin dashboard page, driven through its real
 * request surface.
 *
 * A browser page cannot reach the companion's own 127.0.0.1 server, so every
 * read goes through the gateway proxy and every state the page has is a
 * consequence of those two reads succeeding or failing. The cases that matter
 * are therefore the unhappy ones: only ONE endpoint down (which must NOT render
 * "isn't running"), both down (which must, while keeping Memories from cache),
 * and each write failing (which must announce the real reason and re-read rather
 * than leave an optimistic edit standing).
 *
 * The transport is stubbed; the sections underneath are the real components, so
 * what is asserted is what a user would actually see.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { REMINDERS_PATH, STATS_PATH } from '../apps/crew-companion/constants'
import type { RemindersPayload, StatsPayload } from '../apps/crew-companion/types'

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('../apps/crew-companion/api', () => ({
  apiGet: (path: string) => apiGet(path),
  apiPost: (path: string, body?: unknown) => apiPost(path, body),
}))

const CrewCompanionPage = (await import('../apps/crew-companion/CrewCompanionPage')).default

function reminders(over: Partial<RemindersPayload> = {}): RemindersPayload {
  return {
    reminders: [
      { id: 'zz1', text: 'zz-one', fireAt: new Date(Date.now() + 600_000).toISOString(), recurrence: { everyMinutes: 60 } },
    ],
    breakNudgesEnabled: true,
    sessionNotificationsEnabled: false,
    breakReminderMins: 45,
    language: 'en',
    present: true,
    ...over,
  }
}

const stats: StatsPayload = {
  stats: {
    firstLaunch: '2031-01-01T00:00:00Z',
    streak: 3,
    companionSeconds: 7200,
    breathingSessions: 2,
    remindersCreated: 5,
    latestActiveTime: '23:00',
    earliestActiveTime: '07:00',
  },
  petName: 'zzpet',
  language: 'en',
}

/** Route the two reads independently, so one can fail while the other works. */
function routes({ rem, mem }: { rem?: unknown; mem?: unknown } = {}) {
  apiGet.mockImplementation((path: string) => {
    if (path === REMINDERS_PATH) {
      return rem instanceof Error ? Promise.reject(rem) : Promise.resolve(rem ?? reminders())
    }
    if (path === STATS_PATH) {
      return mem instanceof Error ? Promise.reject(mem) : Promise.resolve(mem ?? stats)
    }
    return Promise.reject(new Error(`zz-unexpected-path:${path}`))
  })
}

/** The live-region the page announces failed writes through. */
function notice() {
  return document.querySelector('[aria-live="polite"]')?.textContent ?? ''
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  apiPost.mockResolvedValue({})
  routes()
})

afterEach(() => {
  localStorage.clear()
})

describe('crew-companion/CrewCompanionPage — reads', () => {
  it('renders the live controls once both endpoints answer', async () => {
    render(<CrewCompanionPage />)
    await waitFor(() => expect(screen.getAllByRole('switch').length).toBe(2))
    expect(apiGet).toHaveBeenCalledWith(REMINDERS_PATH)
    expect(apiGet).toHaveBeenCalledWith(STATS_PATH)
    // Both endpoints up: no "isn't running" section.
    expect(document.querySelector('.cc-offline')).toBeNull()
  })

  it('stays live when only the reminders endpoint is down', async () => {
    // Keying "offline" off reminders alone would render "isn't running" over a
    // live companion whose stats path merely drifted.
    routes({ rem: new Error('zz-reminders-down') })
    render(<CrewCompanionPage />)
    await waitFor(() => expect(screen.getAllByRole('switch').length).toBe(2))
    expect(document.querySelector('.cc-offline')).toBeNull()
  })

  it('treats a malformed payload as unreachable', async () => {
    routes({ rem: { reminders: 'not-an-array' }, mem: {} })
    render(<CrewCompanionPage />)
    await waitFor(() => expect(document.querySelector('.cc-offline')).not.toBeNull())
  })

  it('shows the not-running state and the cached keepsake when both are down', async () => {
    localStorage.setItem('cc:lastStats', JSON.stringify(stats))
    routes({ rem: new Error('zz-down'), mem: new Error('zz-down') })
    render(<CrewCompanionPage />)
    await waitFor(() => expect(document.querySelector('.cc-offline')).not.toBeNull())
    // The controls are gone, but Memories persists from the cache.
    expect(screen.queryAllByRole('switch').length).toBe(0)
    expect(document.body.textContent).toContain('zzpet')
  })

  it('survives an unreadable cache with no keepsake at all', async () => {
    localStorage.setItem('cc:lastStats', '{ not json')
    routes({ rem: new Error('zz-down'), mem: new Error('zz-down') })
    render(<CrewCompanionPage />)
    await waitFor(() => expect(document.querySelector('.cc-offline')).not.toBeNull())
    expect(document.body.textContent).not.toContain('zzpet')
  })

  it('caches the last good stats for the next visit', async () => {
    render(<CrewCompanionPage />)
    await waitFor(() => expect(localStorage.getItem('cc:lastStats')).not.toBeNull())
    expect(JSON.parse(String(localStorage.getItem('cc:lastStats'))).petName).toBe('zzpet')
  })
})

describe('crew-companion/CrewCompanionPage — writes', () => {
  it('moves the switch immediately and clears an earlier failure on success', async () => {
    render(<CrewCompanionPage />)
    await waitFor(() => expect(screen.getAllByRole('switch').length).toBe(2))
    const [breaks] = screen.getAllByRole('switch')
    expect(breaks.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(breaks)
    // Optimistic: the poll is up to POLL_MS away.
    expect(breaks.getAttribute('aria-checked')).toBe('false')
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith(`${REMINDERS_PATH}/config`, { breakNudgesEnabled: false }),
    )
    expect(notice()).toBe('')
  })

  it('announces a failed config write and re-reads the truth', async () => {
    render(<CrewCompanionPage />)
    await waitFor(() => expect(screen.getAllByRole('switch').length).toBe(2))
    apiPost.mockRejectedValue(new Error('zz-config-broke'))
    const before = apiGet.mock.calls.length
    fireEvent.click(screen.getAllByRole('switch')[0])
    await waitFor(() => expect(notice()).toContain('zz-config-broke'))
    expect(apiGet.mock.calls.length).toBeGreaterThan(before)
  })

  it('adds a reminder and clears the draft only once it landed', async () => {
    render(<CrewCompanionPage />)
    await waitFor(() => expect(screen.getAllByRole('switch').length).toBe(2))
    const input = document.querySelector('.cc-add-input') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'zzz in 10 minutes' } })
    fireEvent.submit(document.querySelector('.cc-add') as HTMLFormElement)
    await waitFor(() => expect(input.value).toBe(''))
    expect(apiPost).toHaveBeenCalledWith(
      `${REMINDERS_PATH}/add`,
      expect.objectContaining({ text: expect.stringContaining('zzz') }),
    )
  })

  it('keeps the draft and says why when the add fails', async () => {
    render(<CrewCompanionPage />)
    await waitFor(() => expect(screen.getAllByRole('switch').length).toBe(2))
    apiPost.mockRejectedValue(new Error('zz-add-broke'))
    const input = document.querySelector('.cc-add-input') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'zzz in 10 minutes' } })
    fireEvent.submit(document.querySelector('.cc-add') as HTMLFormElement)
    await waitFor(() => expect(notice()).toContain('zz-add-broke'))
    expect(input.value).toBe('zzz in 10 minutes')
  })

  it('skips a recurring reminder, and announces a failed skip', async () => {
    render(<CrewCompanionPage />)
    await waitFor(() => expect(document.querySelector('.cc-icon-btn.is-remove')).not.toBeNull())
    const skip = Array.from(document.querySelectorAll('.cc-icon-btn')).find(
      (b) => !b.classList.contains('is-remove'),
    ) as HTMLButtonElement
    fireEvent.click(skip)
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(`${REMINDERS_PATH}/skip`, { id: 'zz1' }))
    expect(notice()).toBe('')

    apiPost.mockRejectedValue(new Error('zz-skip-broke'))
    fireEvent.click(skip)
    await waitFor(() => expect(notice()).toContain('zz-skip-broke'))
  })

  it('removes the row at once, and puts it back by re-reading when the write fails', async () => {
    render(<CrewCompanionPage />)
    // `.cc-row` is shared with the Memories rows, so count the reminder-only
    // remove control instead.
    const rows = () => document.querySelectorAll('.cc-icon-btn.is-remove').length
    await waitFor(() => expect(rows()).toBe(1))
    apiPost.mockRejectedValue(new Error('zz-remove-broke'))
    fireEvent.click(document.querySelector('.cc-icon-btn.is-remove') as HTMLButtonElement)
    // Optimistic removal — the row goes now, not on the next poll.
    expect(rows()).toBe(0)
    await waitFor(() => expect(notice()).toContain('zz-remove-broke'))
    // The re-read restores it, because the delete never happened.
    await waitFor(() => expect(rows()).toBe(1))
  })
})

describe('crew-companion/CrewCompanionPage — bringing the companion back', () => {
  async function offlinePage() {
    routes({ rem: new Error('zz-down'), mem: new Error('zz-down') })
    render(<CrewCompanionPage />)
    await waitFor(() => expect(document.querySelector('.cc-cta')).not.toBeNull())
    return document.querySelector('.cc-cta') as HTMLButtonElement
  }

  it('asks for the panel when the app is merely closed', async () => {
    const cta = await offlinePage()
    fireEvent.click(cta)
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith('/api/apps/crew-companion/window', { target: 'panel' }),
    )
    expect(notice()).toBe('')
  })

  it('enables the app first and then re-sends the open it was asked for', async () => {
    const cta = await offlinePage()
    // The first open fails (app switched off), enable succeeds, the retry lands.
    let opens = 0
    apiPost.mockImplementation((path: string) => {
      if (path === '/api/apps/crew-companion/window') {
        opens += 1
        return opens === 1 ? Promise.reject(new Error('zz-disabled')) : Promise.resolve({})
      }
      return Promise.resolve({})
    })
    fireEvent.click(cta)
    await waitFor(() => expect(opens).toBe(2))
    expect(apiPost).toHaveBeenCalledWith('/api/apps/crew-companion/enable', {})
    expect(notice()).toBe('')
  })

  it('reports the real reason when even enabling fails', async () => {
    const cta = await offlinePage()
    apiPost.mockImplementation((path: string) =>
      path === '/api/apps/crew-companion/enable'
        ? Promise.reject(new Error('zz-enable-broke'))
        : Promise.reject(new Error('zz-open-broke')),
    )
    fireEvent.click(cta)
    // The notice carries the failure, not a piece of unrelated guidance prose.
    await waitFor(() => expect(notice()).toContain('zz-enable-broke'))
  })
})
