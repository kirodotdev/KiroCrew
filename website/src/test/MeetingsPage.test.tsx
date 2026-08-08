import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

import MeetingsPage from '../apps/meetings/MeetingsPage'

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
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <MeetingsPage />
      </Provider>
    </QueryClientProvider>,
  )
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
  it('places an X after the status and notes icon only on rows with local data', async () => {
    renderPage()

    const deleteButton = await screen.findByRole('button', { name: 'Delete Retrospective' })
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
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Retrospective' }))

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('Retrospective'))
    await waitFor(() => expect(apiMocks.deleteMeeting).toHaveBeenCalledWith('retro'))
  })

  it('does not delete when confirmation is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Retrospective' }))

    expect(apiMocks.deleteMeeting).not.toHaveBeenCalled()
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
