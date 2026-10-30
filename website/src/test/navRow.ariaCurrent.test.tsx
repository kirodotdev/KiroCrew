/**
 * The main rail paints the current page's row with a class, which assistive
 * technology cannot see, so a screen reader heard every row as the same button.
 * A route row that is active now carries `aria-current="page"`. A row that toggles
 * a surface (`pressed` set) keeps `aria-pressed` and never claims to be a page.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, cleanup } from '@testing-library/react'
import { renderWithProviders } from './helpers'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

import { NavItem } from '../shell/nav/navRow'

function renderRow(props: { label: string; active: boolean; pressed?: boolean }) {
  return renderWithProviders(
    <NavItem
      path="/sessions"
      label={props.label}
      icon={null}
      active={props.active}
      pressed={props.pressed}
      collapsed={false}
    />,
    { route: '/sessions' },
  )
}

const row = (label: string) => screen.getByRole('button', { name: label })

describe('main rail: the current page row says so', () => {
  afterEach(() => cleanup())

  it('marks an active route row with aria-current="page"', () => {
    renderRow({ label: 'Sessions', active: true })
    expect(row('Sessions')).toHaveAttribute('aria-current', 'page')
  })

  it('marks no inactive route row', () => {
    renderRow({ label: 'Schedule', active: false })
    expect(row('Schedule')).not.toHaveAttribute('aria-current')
  })

  it('leaves an active toggle row to aria-pressed', () => {
    renderRow({ label: 'Terminal', active: true, pressed: true })
    expect(row('Terminal')).toHaveAttribute('aria-pressed', 'true')
    expect(row('Terminal')).not.toHaveAttribute('aria-current')
  })
})
