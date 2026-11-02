/**
 * StatusNotice variants.
 *
 * `inline` is the compact in-card run (no bordered box) added for #18203, so a
 * gone approval can read as status inside a dense card the way `ErrorNotice`'s
 * inline variant reads as an error. `block` (the default) keeps its boxed shape,
 * so every pre-existing caller is unchanged. Both announce `role="status"`,
 * never `alert` — a StatusNotice is never a quieter error.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import StatusNotice from '../components/StatusNotice'

describe('StatusNotice variants', () => {
  it.each(['inline', 'block'] as const)('%s: announces role=status, never alert', (variant) => {
    render(<StatusNotice variant={variant} message="Nothing is waiting on it." testId="n" />)
    const notice = screen.getByTestId('n')
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice).toHaveTextContent('Nothing is waiting on it.')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('block (default) keeps the bordered box; inline drops it', () => {
    const { rerender } = render(<StatusNotice message="x" testId="n" />)
    expect(screen.getByTestId('n').className).toContain('border')
    rerender(<StatusNotice variant="inline" message="x" testId="n" />)
    expect(screen.getByTestId('n').className).not.toContain('border')
  })

  it('renders nothing for a falsy message', () => {
    const { container } = render(<StatusNotice variant="inline" message={null} testId="n" />)
    expect(container.firstChild).toBeNull()
  })
})
