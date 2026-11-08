// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { i18nT } from '../i18n/t'
import { MemoryRouter } from 'react-router-dom'
import OwnerOnlyGate from './OwnerOnlyGate'
import ErrorBoundary from './ErrorBoundary'
import { ApiError } from '../api/apiError'

const guest = vi.hoisted(() => ({ value: true, error: null as string | null, retry: vi.fn() }))
vi.mock('../hooks/sharedCrew/useSharedCrew', () => ({ useIsGuest: () => guest.value, useGuestIdentityError: () => guest.error, useRetryGuestIdentity: () => guest.retry }))

function at(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <OwnerOnlyGate><div data-testid="page">page</div></OwnerOnlyGate>
    </MemoryRouter>,
  )
}

afterEach(() => { cleanup(); guest.value = true; guest.error = null; guest.retry.mockClear() })

describe('OwnerOnlyGate', () => {
  it('shows a failed identity read instead of passing it as "not a guest"', () => {
    guest.value = false
    guest.error = 'Bad gateway'
    at('/chat')
    const notice = screen.getByTestId('guest-identity-error')
    // Said in words, with the server's answer kept on hover, not shown raw.
    expect(notice.textContent).toContain(i18nT('sharedCrew.identity_check_failed'))
    expect(notice.textContent).not.toContain('Bad gateway')
    expect(notice.querySelector('[title="Bad gateway"]')).not.toBeNull()
    expect(screen.getByTestId('page')).toBeTruthy()
    fireEvent.click(screen.getByTestId('guest-identity-retry'))
    expect(guest.retry).toHaveBeenCalledOnce()
  })

  it('shows the Owner-only state to a guest on an owner-only page', () => {
    at('/settings/people')
    expect(screen.getByTestId('owner-only')).toBeTruthy()
    expect(screen.queryByTestId('page')).toBeNull()
  })

  it('links back to the chat', () => {
    at('/settings')
    expect(screen.getByTestId('owner-only-back').getAttribute('href')).toBe('/chat')
  })

  it('leaves a guest on an in-crew page alone', () => {
    at('/chat/abc')
    expect(screen.getByTestId('page')).toBeTruthy()
    expect(screen.queryByTestId('owner-only')).toBeNull()
  })

  it('never blocks the owner', () => {
    guest.value = false
    at('/settings')
    expect(screen.getByTestId('page')).toBeTruthy()
  })

  it('renders the same state when a page throws a 403 owner_only', () => {
    const Throws = () => { throw new ApiError(403, 'forbidden', JSON.stringify({ code: 'owner_only' })) }
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<ErrorBoundary><Throws /></ErrorBoundary>)
    expect(screen.getByTestId('owner-only')).toBeTruthy()
  })
})
