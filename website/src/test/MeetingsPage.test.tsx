import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Provider } from 'react-redux'

import { store } from '../store'

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

// Spy, not stub: real formatting still runs (rows render real text), while the
// all-day test can assert WHICH options reached the seam. The suite pins
// TZ=UTC, so the west-of-UTC day-shift is invisible behaviorally — the
// `timeZone: 'UTC'` pin is the fix, and the spy is the only way to prove it.
vi.mock('../i18n/format', { spy: true })

import MeetingsPage from '../apps/meetings/MeetingsPage'
import { fmtDateFields } from '../i18n/format'

const ENDED_MEETING = {
  event_id: 'retro',
  title: 'Retrospective',
  status: 'ended',
  started_at: '2026-08-08T09:00:00Z',
  ended_at: '2026-08-08T10:00:00Z',
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const result = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <MeetingsPage />
      </Provider>
    </QueryClientProvider>,
  )
  return { ...result, queryClient }
}

beforeEach(() => {
  vi.clearAllMocks()
  apiMocks.config.mockResolvedValue({ config: {} })
  apiMocks.calendar.mockResolvedValue({
    events: [{
      event_id: 'planning',
      title: 'Planning',
      start: '2026-08-09T09:00:00Z',
      end: '2026-08-09T10:00:00Z',
      all_day: false,
      location: '',
      organizer: '',
      attendees: [],
      description: '',
    }],
    provider: 'none',
    configured: false,
  })
  apiMocks.meetings.mockResolvedValue({ meetings: [ENDED_MEETING] })
  apiMocks.deleteMeeting.mockResolvedValue(undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('MeetingsPage deletion', () => {
  it('places a delete icon after the status and notes icon only on rows with local data', async () => {
    renderPage()

    const deleteButton = await screen.findByRole('button', { name: 'Delete Retrospective' })
    expect(deleteButton.querySelector('svg.lucide-trash-2')).toBeInTheDocument()
    const meetingRow = screen.getByText('Retrospective').closest('[role="button"]')
    expect(meetingRow).not.toBeNull()

    const status = within(meetingRow as HTMLElement).getByText('Ended')
    const notes = within(meetingRow as HTMLElement).getByLabelText('Has notes')
    expect(status.compareDocumentPosition(notes) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
    expect(notes.compareDocumentPosition(deleteButton) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)

    const scheduledRow = screen.getByText('Planning').closest('[role="button"]')
    expect(scheduledRow).not.toBeNull()
    expect(
      within(scheduledRow as HTMLElement).queryByRole('button', { name: 'Delete Planning' }),
    ).not.toBeInTheDocument()
  })

  it('confirms before deleting and sends the meeting id to the API', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { queryClient } = renderPage()
    queryClient.setQueryData(['meetings', 'retro', 'init'], { ok: true })
    queryClient.setQueryData(['meetings', 'retro', 'meta'], { meta: ENDED_MEETING })
    queryClient.setQueryData(['meetings', 'retro', 'outputs'], { outputs: {}, tasks: [] })

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Retrospective' }))

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('Retrospective'))
    await waitFor(() => expect(apiMocks.deleteMeeting).toHaveBeenCalledWith('retro'))
    await waitFor(() => {
      expect(queryClient.getQueryData(['meetings', 'retro', 'init'])).toBeUndefined()
      expect(queryClient.getQueryData(['meetings', 'retro', 'meta'])).toBeUndefined()
      expect(queryClient.getQueryData(['meetings', 'retro', 'outputs'])).toBeUndefined()
    })
  })

  it('does not delete when confirmation is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Retrospective' }))

    expect(apiMocks.deleteMeeting).not.toHaveBeenCalled()
  })

  it('uses a neutral loader while deletion is pending', async () => {
    let finishDelete: (() => void) | undefined
    apiMocks.deleteMeeting.mockImplementation(
      () => new Promise<void>(resolve => { finishDelete = resolve }),
    )
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderPage()

    const deleteButton = await screen.findByRole('button', { name: 'Delete Retrospective' })
    fireEvent.click(deleteButton)

    await waitFor(() => {
      expect(deleteButton.querySelector('svg.lucide-loader-circle')).toBeInTheDocument()
      expect(deleteButton.querySelector('svg.lucide-refresh-cw')).not.toBeInTheDocument()
    })
    await act(async () => finishDelete?.())
  })

  it('shows the backend failure beside the meeting that could not be deleted', async () => {
    apiMocks.deleteMeeting.mockRejectedValue(new Error('End the meeting before deleting it'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Retrospective' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'End the meeting before deleting it',
    )
    expect(screen.getByText('Retrospective')).toBeInTheDocument()
  })

  it('keeps the delete affordance visible but disabled for a live meeting', async () => {
    apiMocks.meetings.mockResolvedValue({
      meetings: [{ ...ENDED_MEETING, status: 'active' }],
    })
    renderPage()

    expect(
      await screen.findByRole('button', { name: 'End this meeting before deleting it' }),
    ).toBeDisabled()
  })
})

describe('MeetingsPage all-day rendering', () => {
  const ALL_DAY_EVENT = {
    event_id: 'offsite',
    title: 'Offsite',
    start: '2026-08-09T00:00:00Z',
    end: '2026-08-10T00:00:00Z',
    all_day: true,
    location: '',
    organizer: '',
    attendees: [],
    description: '',
  }

  beforeEach(() => {
    apiMocks.calendar.mockResolvedValue({
      events: [ALL_DAY_EVENT],
      provider: 'ics',
      configured: true,
    })
    apiMocks.meetings.mockResolvedValue({ meetings: [] })
  })

  it('renders an all-day event as a date only, with the fields read in UTC', async () => {
    renderPage()
    const row = (await screen.findByText('Offsite')).closest('[role="button"]') as HTMLElement

    // No time is displayed: an all-day event has no instant to show.
    expect(row.textContent).not.toMatch(/\d{1,2}:\d{2}/)

    // The visible result: the stored calendar date (Aug 9), not merely the
    // right option bag — a regression that keeps the pin but formats the wrong
    // Date would pass the seam assertions below.
    expect(row.textContent).toContain('Aug 9')

    // The date fields are pinned to UTC. `start` is a DATE ANCHOR (midnight
    // UTC), so reading it in the browser's zone would render Aug 8 for every
    // browser west of UTC. The suite runs under TZ=UTC where converted and
    // unconverted output coincide, so the pin is asserted at the seam.
    expect(vi.mocked(fmtDateFields)).toHaveBeenCalledWith(
      expect.any(Date),
      expect.objectContaining({ timeZone: 'UTC', day: 'numeric' }),
    )
    expect(vi.mocked(fmtDateFields)).not.toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ hour: '2-digit' }),
    )
  })

  it('keeps rendering the time for timed events', async () => {
    apiMocks.calendar.mockResolvedValue({
      events: [{ ...ALL_DAY_EVENT, all_day: false, start: '2026-08-09T09:30:00Z' }],
      provider: 'ics',
      configured: true,
    })
    renderPage()
    const row = (await screen.findByText('Offsite')).closest('[role="button"]') as HTMLElement
    expect(row.textContent).toMatch(/\d{1,2}:\d{2}/)
  })
})
