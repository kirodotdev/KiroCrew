import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { store } from '../store'

/**
 * The meetings LIST: three views behind one route (list / one meeting /
 * settings), the merge of "upcoming from the calendar" with "meetings this app
 * has touched", and the guarded delete. The merge is where the interesting
 * failure lives — a calendar event and a worked-on meeting for the same id are
 * ONE row, and the row's start time comes from the calendar even after the app
 * has recorded its own timestamps.
 *
 * `MeetingView` / `SettingsView` are probes: they have their own suites, and
 * this file is about which route the page hands them.
 */
const apiMocks = vi.hoisted(() => ({
  config: vi.fn(),
  calendar: vi.fn(),
  meetings: vi.fn(),
  syncCalendar: vi.fn(),
  deleteMeeting: vi.fn(),
}))

vi.mock('../apps/meetings/api', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/meetings/api')>()
  return { ...actual, meetingsApi: apiMocks }
})

vi.mock('../apps/meetings/MeetingView', () => ({
  default: ({ eventId, fallbackTitle, onBack, onOpenSettings }: {
    eventId: string
    fallbackTitle?: string
    onBack: () => void
    onOpenSettings: () => void
  }) => (
    <div>
      <span data-testid="meeting-id">{eventId}</span>
      <span data-testid="meeting-title">{fallbackTitle ?? 'zzz-none'}</span>
      <button onClick={onBack}>zzz-back</button>
      <button onClick={onOpenSettings}>zzz-open-settings</button>
    </div>
  ),
}))

vi.mock('../apps/meetings/SettingsView', () => ({
  default: ({ onBack }: { onBack: () => void }) => (
    <div>
      <span data-testid="settings">zzz-settings</span>
      <button onClick={onBack}>zzz-settings-back</button>
    </div>
  ),
}))

import MeetingsPage from '../apps/meetings/MeetingsPage'

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <Provider store={store}><MeetingsPage /></Provider>
      </QueryClientProvider>,
    ),
  }
}

function event(overrides: Record<string, unknown> = {}) {
  return {
    event_id: 'zzz-planning',
    title: 'zzz Planning',
    start: '2026-08-09T09:00:00Z',
    end: '2026-08-09T10:00:00Z',
    location: '',
    organizer: '',
    attendees: [],
    description: '',
    ...overrides,
  }
}

function meeting(overrides: Record<string, unknown> = {}) {
  return {
    event_id: 'zzz-retro',
    title: 'zzz Retro',
    status: 'ended',
    started_at: '2026-08-08T09:00:00Z',
    ended_at: '2026-08-08T10:00:00Z',
    ...overrides,
  }
}

/** The most recent notification title the page dispatched. */
function lastNotification(): string {
  const items = store.getState().notifications.items
  return items.length ? items[items.length - 1].title : ''
}

beforeEach(() => {
  vi.clearAllMocks()
  apiMocks.config.mockResolvedValue({ config: {} })
  apiMocks.calendar.mockResolvedValue({ events: [event()], provider: 'none', configured: false })
  apiMocks.meetings.mockResolvedValue({ meetings: [meeting()] })
  apiMocks.syncCalendar.mockResolvedValue({ count: 3 })
  apiMocks.deleteMeeting.mockResolvedValue(undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('MeetingsPage list', () => {
  it('merges calendar events with worked-on meetings, newest first', async () => {
    renderPage()
    expect(await screen.findByText('zzz Planning')).toBeInTheDocument()
    expect(screen.getByText('zzz Retro')).toBeInTheDocument()
    // Planning (Aug 9) sorts above Retro (Aug 8).
    const planning = screen.getByText('zzz Planning')
    const retro = screen.getByText('zzz Retro')
    expect(planning.compareDocumentPosition(retro) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
  })

  it('keeps the calendar start time for a meeting the app has also touched', async () => {
    apiMocks.calendar.mockResolvedValue({ events: [event({ event_id: 'zzz-retro', title: 'zzz Cal Retro' })], provider: 'none', configured: true })
    renderPage()
    // One row, not two: same id.
    expect(await screen.findByText('zzz Retro')).toBeInTheDocument()
    expect(screen.queryByText('zzz Cal Retro')).not.toBeInTheDocument()
    // …and it keeps the calendar's Aug 9 slot rather than the recorded Aug 8 one.
    expect(screen.getByText(/Aug 9/)).toBeInTheDocument()
  })

  it('falls back to the untitled label when neither side names the meeting', async () => {
    apiMocks.calendar.mockResolvedValue({ events: [], provider: 'none', configured: true })
    apiMocks.meetings.mockResolvedValue({ meetings: [meeting({ title: '' })] })
    renderPage()
    expect(await screen.findByText('Untitled meeting')).toBeInTheDocument()
  })

  it('renders no timestamp line for a missing or unparseable start', async () => {
    apiMocks.calendar.mockResolvedValue({ events: [], provider: 'none', configured: true })
    apiMocks.meetings.mockResolvedValue({
      meetings: [
        meeting({ event_id: 'zzz-a', title: 'zzz No Start', started_at: '' }),
        meeting({ event_id: 'zzz-b', title: 'zzz Bad Start', started_at: 'zzz-not-a-date' }),
      ],
    })
    renderPage()
    const noStart = (await screen.findByText('zzz No Start')).closest('[role="button"]')!
    expect(within(noStart as HTMLElement).queryByText(/·/)).not.toBeInTheDocument()
    const badStart = screen.getByText('zzz Bad Start').closest('[role="button"]')!
    expect(within(badStart as HTMLElement).queryByText(/·/)).not.toBeInTheDocument()
  })

  it('shows a badge per lifecycle state', async () => {
    apiMocks.calendar.mockResolvedValue({ events: [], provider: 'none', configured: true })
    apiMocks.meetings.mockResolvedValue({
      meetings: [
        meeting({ event_id: 'zzz-1', title: 'zzz Paused', status: 'paused' }),
        meeting({ event_id: 'zzz-2', title: 'zzz Reviewing', status: 'reviewing' }),
        meeting({ event_id: 'zzz-3', title: 'zzz Ended', status: 'ended' }),
      ],
    })
    renderPage()
    expect(await screen.findByText('Paused')).toBeInTheDocument()
    expect(screen.getByText('Reviewing')).toBeInTheDocument()
    expect(screen.getByText('Ended')).toBeInTheDocument()
  })

  it('filters by title', async () => {
    renderPage()
    await screen.findByText('zzz Planning')
    await userEvent.type(screen.getByLabelText(/Filter meetings/i), 'retro')
    expect(screen.getByText('zzz Retro')).toBeInTheDocument()
    expect(screen.queryByText('zzz Planning')).not.toBeInTheDocument()
  })

  it('offers a calendar hint when there is nothing to show', async () => {
    apiMocks.calendar.mockResolvedValue({ events: [], provider: 'none', configured: false })
    apiMocks.meetings.mockResolvedValue({ meetings: [] })
    const { unmount } = renderPage()
    expect(await screen.findByText(/Connect a calendar in settings/i)).toBeInTheDocument()
    unmount()

    apiMocks.calendar.mockResolvedValue({ events: [], provider: 'ics', configured: true })
    renderPage()
    expect(await screen.findByText(/Nothing on the calendar/i)).toBeInTheDocument()
  })
})

describe('MeetingsPage live banner', () => {
  it('offers a way back into a live meeting', async () => {
    apiMocks.meetings.mockResolvedValue({
      meetings: [meeting({ event_id: 'zzz-live', title: 'zzz Standup', status: 'active' })],
    })
    renderPage()
    expect(await screen.findByText('zzz Standup is live')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Return to the meeting/i }))
    expect(screen.getByTestId('meeting-id')).toHaveTextContent('zzz-live')
  })

  it('says a reviewing meeting is waiting on the action-item review', async () => {
    apiMocks.meetings.mockResolvedValue({
      meetings: [meeting({ event_id: 'zzz-rev', title: 'zzz Standup', status: 'reviewing' })],
    })
    renderPage()
    expect(await screen.findByText(/waiting on an action-item review/i)).toBeInTheDocument()
  })
})

describe('MeetingsPage routing', () => {
  it('opens a row, and comes back to a refreshed list', async () => {
    const { queryClient } = renderPage()
    await userEvent.click((await screen.findByText('zzz Retro')).closest('[role="button"]')!)
    expect(screen.getByTestId('meeting-id')).toHaveTextContent('zzz-retro')
    expect(screen.getByTestId('meeting-title')).toHaveTextContent('zzz Retro')

    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    await userEvent.click(screen.getByRole('button', { name: 'zzz-back' }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['meetings', 'list'] })
    expect(await screen.findByText('zzz Retro')).toBeInTheDocument()
  })

  it('reaches settings from the header and from inside a meeting, and back again', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /^Settings$/ }))
    expect(screen.getByTestId('settings')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'zzz-settings-back' }))
    await userEvent.click((await screen.findByText('zzz Retro')).closest('[role="button"]')!)
    await userEvent.click(screen.getByRole('button', { name: 'zzz-open-settings' }))
    expect(screen.getByTestId('settings')).toBeInTheDocument()
  })

  it('starts an ad-hoc meeting with a slug id needing no calendar', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /New meeting/i }))
    const id = screen.getByTestId('meeting-id').textContent ?? ''
    expect(id).toMatch(/^adhoc-\d{4}-\d{2}-\d{2}T[\d-]+Z?$/)
    // No ':' or '.' — the backend id charset is [A-Za-z0-9._-] minus those.
    expect(id).not.toMatch(/[:]/)
    expect(screen.getByTestId('meeting-title')).toHaveTextContent('Ad-hoc meeting')
  })
})

describe('MeetingsPage calendar sync', () => {
  it('reports how many events came back and refreshes the calendar', async () => {
    const { queryClient } = renderPage()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    await userEvent.click(await screen.findByRole('button', { name: /Sync calendar/i }))
    await waitFor(() => expect(lastNotification()).toBe('Synced 3 events'))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['meetings', 'calendar'] })
  })

  it("reports the sync failure's own message", async () => {
    apiMocks.syncCalendar.mockRejectedValue(new Error('zzz calendar unreachable'))
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Sync calendar/i }))
    await waitFor(() => expect(lastNotification()).toBe('zzz calendar unreachable'))
  })

  it('falls back to a generic message when the failure carries none', async () => {
    apiMocks.syncCalendar.mockRejectedValue(new Error(''))
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Sync calendar/i }))
    await waitFor(() => expect(lastNotification()).toBe('Could not sync the calendar.'))
  })
})

describe('MeetingsPage delete', () => {
  it('asks before deleting, and a declined confirm deletes nothing', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Delete zzz Retro' }))
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('zzz Retro'))
    expect(apiMocks.deleteMeeting).not.toHaveBeenCalled()
  })

  it('deletes on confirm, drops that meeting from the cache, and says so', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { queryClient } = renderPage()
    const removeQueries = vi.spyOn(queryClient, 'removeQueries')
    await userEvent.click(await screen.findByRole('button', { name: 'Delete zzz Retro' }))
    await waitFor(() => expect(apiMocks.deleteMeeting).toHaveBeenCalledWith('zzz-retro'))
    await waitFor(() => expect(lastNotification()).toBe('Deleted “zzz Retro”'))
    expect(removeQueries).toHaveBeenCalledWith({ queryKey: ['meetings', 'zzz-retro'] })
  })

  it('shows a failed delete on the row it belongs to', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    apiMocks.deleteMeeting.mockRejectedValue(new Error('zzz still transcribing'))
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Delete zzz Retro' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('zzz still transcribing')
  })

  it('falls back to the generic message for a blank failure', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    apiMocks.deleteMeeting.mockRejectedValue(new Error('   '))
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Delete zzz Retro' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not delete this meeting.')
  })

  it('refuses to delete a meeting that has not ended', async () => {
    apiMocks.meetings.mockResolvedValue({
      meetings: [meeting({ event_id: 'zzz-live', title: 'zzz Standup', status: 'active' })],
    })
    renderPage()
    const guarded = await screen.findByRole('button', { name: 'End this meeting before deleting it' })
    expect(guarded).toBeDisabled()
  })
})
