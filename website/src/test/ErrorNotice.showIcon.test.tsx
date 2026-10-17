/**
 * ErrorNotice `showIcon`.
 *
 * A notice nested in a row that already marks the failure with its own status
 * icon (the Remote Crew pre-flight's check rows) turns its triangle off, so the
 * problem is not drawn twice. The default keeps every existing consumer's icon,
 * and turning it off keeps the alert role: the icon is decorative only.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'

const ICON = '.lucide-triangle-alert'

describe('ErrorNotice showIcon', () => {
  it.each(['inline', 'block'] as const)('%s: draws the triangle by default', (variant) => {
    render(<ErrorNotice variant={variant} message="Save failed" testId="n" noHandoffReason="unit test" />)
    expect(screen.getByTestId('n').querySelector(ICON)).not.toBeNull()
  })

  it.each(['inline', 'block'] as const)('%s: showIcon={false} drops the triangle and keeps the alert', (variant) => {
    render(<ErrorNotice variant={variant} message="Save failed" testId="n" showIcon={false} noHandoffReason="unit test" />)
    const notice = screen.getByTestId('n')
    expect(notice.querySelector(ICON)).toBeNull()
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Save failed')
  })
})
