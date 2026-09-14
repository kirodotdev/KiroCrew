/**
 * The rendered sink for `reportActionFailure`, tested apart from any page.
 *
 * These behaviours were pinned against `ChatPage` while the notice lived there.
 * They moved with it: the sink is now mounted in the always-mounted shells, so a
 * page is the wrong place to assert them from. The mount itself is pinned by the
 * App-level suite, which renders the real route table at a non-chat path.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ActionFailureNotice from '../components/ActionFailureNotice'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { reportActionFailure, __resetActionFailureForTests } from '../utils/actionFailure'
import {
  consumeChatHandoff,
  installSoftNavigate,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'

const navigated: string[] = []

function RegisteredGuard({ guard }: { guard: () => boolean }) {
  useRegisterNavigationLeaveGuard(guard)
  return null
}

function renderNotice(guard?: () => boolean, route = '/settings') {
  return render(
    <NavigationLeaveGuardProvider>
      <MemoryRouter initialEntries={[route]}>
        {guard && <RegisteredGuard guard={guard} />}
        <ActionFailureNotice />
      </MemoryRouter>
    </NavigationLeaveGuardProvider>,
  )
}

describe('ActionFailureNotice', () => {
  beforeEach(() => {
    __resetActionFailureForTests()
    __resetErrorJournalForTests()
    __resetNavSeamForTests()
    sessionStorage.clear()
    navigated.length = 0
    installSoftNavigate(to => { navigated.push(to) })
  })

  afterEach(() => {
    __resetNavSeamForTests()
    vi.restoreAllMocks()
  })

  it('renders nothing until a write is rejected', () => {
    renderNotice()
    expect(screen.queryByTestId('session-action-error')).toBeNull()
  })

  it('shows a rejected write', () => {
    renderNotice()
    act(() => { reportActionFailure('Session reload failed.') })
    expect(screen.getByTestId('session-action-error').textContent).toContain('Session reload failed.')
  })

  it('names the session a rejected write reverted, not "this session"', () => {
    renderNotice()
    act(() => { reportActionFailure("Couldn't change this session's pin — the change was undone.", 'Research notes') })
    expect(screen.getByTestId('session-action-error').textContent)
      .toContain('Couldn’t update “Research notes”')
  })

  it('uses a neutral heading when a reporter names no session', () => {
    renderNotice()
    act(() => { reportActionFailure('Session reload failed.') })
    const text = screen.getByTestId('session-action-error').textContent ?? ''
    expect(text).toContain("Couldn’t complete that action")
    expect(text).not.toContain('Couldn’t update')
    expect(text).not.toContain('“”')
  })

  it('uses the neutral heading for a subjectless send without claiming a local update', () => {
    renderNotice()
    act(() => { reportActionFailure('The copy was not delivered — this session is unchanged.') })
    const text = screen.getByTestId('session-action-error').textContent ?? ''
    expect(text).toContain("Couldn’t complete that action")
    expect(text).toContain('The copy was not delivered — this session is unchanged.')
    expect(text).not.toContain('Couldn’t update')
  })

  it('dismissing the newest rejection shows the earlier one it covered', () => {
    renderNotice()
    act(() => { reportActionFailure('The color change was undone.', 'Research notes') })
    act(() => { reportActionFailure('The move was undone.', 'Trip planning') })
    expect(screen.getByTestId('session-action-error').textContent).toContain('The move was undone.')
    // Said on the banner, so the earlier failure taking its place is expected
    // rather than looking like a dismiss that did not work.
    expect(screen.getByTestId('session-action-error').textContent)
      .toContain('1 earlier failure is waiting — dismiss this one to see it.')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    const text = screen.getByTestId('session-action-error').textContent ?? ''
    expect(text).toContain('Couldn’t update “Research notes”')
    expect(text).toContain('The color change was undone.')
    expect(text).not.toContain('earlier failure')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('session-action-error')).toBeNull()
  })

  it('a reporter that changed nothing here leads with its own action, not "Couldn’t update"', () => {
    // A copy sent to another machine and a fork that created nothing both revert
    // no session, so the shared lead would assert a change that never happened.
    renderNotice()
    act(() => { reportActionFailure('No new session was created.', 'Research notes', undefined, 'Couldn’t duplicate “Research notes”') })
    const text = screen.getByTestId('session-action-error').textContent ?? ''
    expect(text).toContain('Couldn’t duplicate “Research notes”')
    expect(text).toContain('No new session was created.')
    expect(text).not.toContain('Couldn’t update')
  })

  it('the dismiss button takes down the shown failure', () => {
    renderNotice()
    act(() => { reportActionFailure('The mode change was undone.', 'Research notes') })
    expect(screen.getByTestId('session-action-error')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Dismiss'))
    expect(screen.queryByTestId('session-action-error')).toBeNull()
  })

  it('vetoes the hand-off before staging or navigating, then proceeds when allowed', () => {
    const guard = vi.fn(() => false)
    renderNotice(guard)
    act(() => { reportActionFailure('The pin change was undone.', 'Research notes') })

    fireEvent.click(screen.getByRole('button', { name: /ask the agent/i }))
    expect(guard).toHaveBeenCalledOnce()
    expect(navigated).toEqual([])
    expect(consumeChatHandoff()).toBeNull()

    guard.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: /ask the agent/i }))
    expect(guard).toHaveBeenCalledTimes(2)
    expect(navigated).toEqual(['/chat'])
    expect(consumeChatHandoff()).toContain('The pin change was undone.')
  })

  it('does not ask the leave guard when the hand-off is already on /chat', () => {
    const guard = vi.fn(() => false)
    renderNotice(guard, '/chat')
    act(() => { reportActionFailure('Session reload failed.') })

    fireEvent.click(screen.getByRole('button', { name: /ask the agent/i }))
    expect(guard).not.toHaveBeenCalled()
    expect(navigated).toEqual(['/chat'])
    expect(consumeChatHandoff()).toContain('Session reload failed.')
  })
})
