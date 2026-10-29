/**
 * ErrorNotice `announce`.
 *
 * Three surfaces report a per-slot write failure in place AND through the shared
 * floating agent-switch toast, which carries the identical string. Both rendered
 * `role="alert"`, so a screen reader read the same failure out twice. The in-place
 * copy drops its live region: it is never the sole surface, so it stays readable
 * while the toast does the announcing. `showIcon` is the visual half of the same
 * concern and deliberately keeps the role.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'

describe('ErrorNotice announce', () => {
  it.each(['inline', 'block'] as const)('%s: announces assertively by default', (variant) => {
    render(<ErrorNotice variant={variant} message="Save failed" testId="n" noHandoffReason="unit test" />)
    expect(screen.getByTestId('n')).toHaveAttribute('role', 'alert')
  })

  it.each(['inline', 'block'] as const)('%s: announce={false} drops the live region but keeps the text readable', (variant) => {
    render(<ErrorNotice variant={variant} message="Save failed" testId="n" announce={false} noHandoffReason="unit test" />)
    const notice = screen.getByTestId('n')
    expect(notice.getAttribute('role')).toBeNull()
    expect(notice).toHaveTextContent('Save failed')
  })

  it('announce={false} leaves no live region anywhere in the notice, so nothing re-announces', () => {
    const { container } = render(
      <ErrorNotice message="Save failed" testId="n" announce={false} noHandoffReason="unit test" />,
    )
    expect(container.querySelectorAll('[role="alert"], [role="status"]').length).toBe(0)
  })
})
