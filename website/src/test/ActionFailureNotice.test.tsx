/**
 * The rendered sink for `reportActionFailure`, tested apart from any page.
 *
 * These behaviours were pinned against `ChatPage` while the notice lived there.
 * They moved with it: the sink is now mounted in the always-mounted shells, so a
 * page is the wrong place to assert them from. The mount itself is pinned by the
 * App-level suite, which renders the real route table at a non-chat path.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
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
})
