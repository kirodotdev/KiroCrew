/**
 * ErrorNotice `noHandoffReason`: the fourth hand-off state, for a notice whose
 * hand-off would open an agent that cannot run.
 *
 * The type forbids combining it with `askAgent` (pinned at compile time in
 * `src/components/ErrorNotice.handoffTypePins.tsx`). These tests pin the
 * runtime side: the reason is never rendered, no hand-off renders in either
 * variant, and a notice without the reason keeps the hand-off exactly as before.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'
import { ErrorNoticeHandoffTypePins } from '../components/ErrorNotice.handoffTypePins'

const REASON = 'every agent runs as this lapsed sign-in'
const ASK = /ask the agent/i

describe('ErrorNotice noHandoffReason', () => {
  for (const variant of ['block', 'inline'] as const) {
    it(`${variant}: renders the error with no hand-off and never shows the reason`, () => {
      render(<ErrorNotice variant={variant} message="Kiro sign-in expired" noHandoffReason={REASON} testId="n" />)
      const notice = screen.getByTestId('n')
      expect(notice.getAttribute('role')).toBe('alert')
      expect(notice.textContent).toContain('Kiro sign-in expired')
      expect(notice.textContent).not.toContain(REASON)
      expect(notice.outerHTML).not.toContain(REASON)
      expect(screen.queryByRole('button', { name: ASK })).toBeNull()
    })

    it(`${variant}: without the reason, askAgent still renders the hand-off`, () => {
      render(<ErrorNotice variant={variant} message="Could not load sessions" askAgent />)
      expect(screen.getByRole('button', { name: ASK })).toBeTruthy()
    })
  }

  it('the compile-time pin component renders every combination as an alert', () => {
    // The pins are checked by `tsc`, not here; rendering them keeps the file
    // exercised and shows the reason prop alone never adds a hand-off.
    render(<ErrorNoticeHandoffTypePins />)
    expect(screen.getAllByRole('alert')).toHaveLength(7)
    expect(screen.queryAllByRole('button', { name: ASK })).toHaveLength(2)
  })
})
