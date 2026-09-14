/**
 * The rendered sink for `reportActionFailure`, tested apart from any page.
 *
 * These behaviours were pinned against `ChatPage` while the notice lived there.
 * They moved with it: the sink is now mounted in the always-mounted shells, so a
 * page is the wrong place to assert them from. The mount itself is pinned by the
 * App-level suite, which renders the real route table at a non-chat path.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import ActionFailureNotice from '../components/ActionFailureNotice'
import { reportActionFailure, __resetActionFailureForTests } from '../utils/actionFailure'

describe('ActionFailureNotice', () => {
  beforeEach(() => { __resetActionFailureForTests() })

  it('renders nothing until a write is rejected', () => {
    render(<ActionFailureNotice />)
    expect(screen.queryByTestId('session-action-error')).toBeNull()
  })

  it('shows a rejected write', () => {
    render(<ActionFailureNotice />)
    act(() => { reportActionFailure('Session reload failed.') })
    expect(screen.getByTestId('session-action-error').textContent).toContain('Session reload failed.')
  })

  it('names the session a rejected write reverted, not "this session"', () => {
    render(<ActionFailureNotice />)
    act(() => { reportActionFailure("Couldn't change this session's pin — the change was undone.", 'Research notes') })
    expect(screen.getByTestId('session-action-error').textContent).toContain('Research notes')
  })

  it('carries no heading when a reporter names no session', () => {
    render(<ActionFailureNotice />)
    act(() => { reportActionFailure('Session reload failed.') })
    expect(screen.getByTestId('session-action-error').textContent).not.toContain('Couldn’t update')
  })

  it('a reporter that changed nothing here leads with its own action, not "Couldn’t update"', () => {
    // A copy sent to another machine and a fork that created nothing both revert
    // no session, so the shared lead would assert a change that never happened.
    render(<ActionFailureNotice />)
    act(() => { reportActionFailure('No new session was created.', 'Research notes', undefined, 'Couldn’t duplicate “Research notes”') })
    const text = screen.getByTestId('session-action-error').textContent ?? ''
    expect(text).toContain('Couldn’t duplicate “Research notes”')
    expect(text).toContain('No new session was created.')
    expect(text).not.toContain('Couldn’t update')
  })

  it('the dismiss button takes down a failure a write reported under its key', () => {
    // The clear a success calls is keyed; the button's is not, and it is bound
    // straight to an onClick — so it must not read the click event as a key.
    render(<ActionFailureNotice />)
    act(() => { reportActionFailure('The mode change was undone.', 'Research notes', undefined, undefined, { actionKey: 'mode:chat-a' }) })
    expect(screen.getByTestId('session-action-error')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Dismiss'))
    expect(screen.queryByTestId('session-action-error')).toBeNull()
  })
})
