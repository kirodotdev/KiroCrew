import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'

import AppNavStateIcon from './AppNavStateIcon'

/**
 * Which variant is VISIBLE is decided by index.css from the nav row's state, so
 * these cases pin what the component hands that CSS: the normal icon under
 * `.app-nav-icon-lit`, the inactive file under `.app-nav-icon-idle`, and the
 * normal icon alone whenever there is no usable inactive file.
 */
describe('AppNavStateIcon', () => {
  const normal = <svg data-testid="normal" />

  it('renders both variants, each under the class the CSS swaps on', () => {
    const { container, getByTestId } = render(
      <span className="app-icon-nav"><AppNavStateIcon icon={normal} inactiveSrc="/apps/demo/ui/idle.svg" /></span>,
    )
    expect(getByTestId('normal').parentElement).toHaveClass('app-nav-icon-lit')
    const idle = container.querySelector('img.app-nav-icon-idle')
    expect(idle).not.toBeNull()
    expect(idle).toHaveAttribute('src', '/apps/demo/ui/idle.svg')
    expect(idle).toHaveAttribute('alt', '')
  })

  it('renders the normal icon alone when no inactive file is declared', () => {
    const { container, getByTestId } = render(<AppNavStateIcon icon={normal} inactiveSrc="" />)
    expect(getByTestId('normal').parentElement).not.toHaveClass('app-nav-icon-lit')
    expect(container.querySelector('img')).toBeNull()
  })

  it('falls back to the normal icon alone when the inactive file fails to load', () => {
    // Otherwise the idle CSS would hide the normal icon and show a broken image
    // on every unselected row.
    const { container, getByTestId } = render(
      <AppNavStateIcon icon={normal} inactiveSrc="/apps/demo/ui/missing.svg" />,
    )
    fireEvent.error(container.querySelector('img.app-nav-icon-idle')!)
    expect(container.querySelector('img')).toBeNull()
    expect(getByTestId('normal').parentElement).not.toHaveClass('app-nav-icon-lit')
  })

  it('gives a new inactive file a fresh try after an earlier one failed', () => {
    const { container, rerender } = render(
      <AppNavStateIcon icon={normal} inactiveSrc="/apps/demo/ui/old.svg" />,
    )
    fireEvent.error(container.querySelector('img.app-nav-icon-idle')!)
    rerender(<AppNavStateIcon icon={normal} inactiveSrc="/apps/demo/ui/new.svg" />)
    expect(container.querySelector('img.app-nav-icon-idle')).toHaveAttribute('src', '/apps/demo/ui/new.svg')
  })
})
