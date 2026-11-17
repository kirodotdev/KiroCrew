import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ScheduleCrewmateRosterNotice, { type CrewmateRosterRead } from '../pages/ScheduleCrewmateRosterNotice'

// The Schedule page's Bot roster notice reads the shared members query. The
// page can open while that query is already failed and re-reading, so the
// notice's first render sees `pending` with no error.

const read = (over: Partial<CrewmateRosterRead> = {}): CrewmateRosterRead => ({
  isError: false,
  isPending: true,
  isFetching: true,
  errorUpdateCount: 1,
  error: null,
  refetch: vi.fn().mockResolvedValue(undefined),
  ...over,
})

const errorMessage = (e: unknown) => (e instanceof Error && e.message ? e.message : 'Failed')

describe('ScheduleCrewmateRosterNotice', () => {
  it('says a read is in progress, not a generic failure, when it mounts mid-read', () => {
    renderWithProviders(<ScheduleCrewmateRosterNotice read={read()} show errorMessage={errorMessage} />)
    const notice = screen.getByTestId('schedule-crewmate-roster-error')
    expect(notice).toHaveTextContent('Retrying…')
    expect(notice).not.toHaveTextContent('Failed')
    const retry = screen.getByRole('button', { name: 'Retry' })
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(retry).not.toBeDisabled()
  })

  it('shows the error it last saw while the next read runs', () => {
    const failed = read({ isError: true, isPending: false, isFetching: false, error: new Error('Members request failed (HTTP 500)') })
    const { rerender } = renderWithProviders(<ScheduleCrewmateRosterNotice read={failed} show errorMessage={errorMessage} />)
    rerender(<ScheduleCrewmateRosterNotice read={read()} show errorMessage={errorMessage} />)
    expect(screen.getByTestId('schedule-crewmate-roster-error')).toHaveTextContent('Members request failed (HTTP 500)')
  })

  it('renders nothing once the read has never failed or the page hides it', () => {
    const { rerender } = renderWithProviders(<ScheduleCrewmateRosterNotice read={read({ errorUpdateCount: 0 })} show errorMessage={errorMessage} />)
    expect(screen.queryByTestId('schedule-crewmate-roster-error')).toBeNull()
    rerender(<ScheduleCrewmateRosterNotice read={read({ isError: true, isPending: false })} show={false} errorMessage={errorMessage} />)
    expect(screen.queryByTestId('schedule-crewmate-roster-error')).toBeNull()
  })
})
