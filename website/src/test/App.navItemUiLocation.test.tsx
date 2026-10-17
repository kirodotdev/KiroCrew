/**
 * NavItem forwards a registered find_ui location to the row a person taps.
 *
 * The phone nav drawer's Search row (`shell.menu-search`) is a NavItem, and the
 * index generator only proves the `{...uiLocation(id)}` spread on <NavItem>.
 * NavItem takes a fixed prop list, so the marker reaches the DOM only through
 * its typed `data-ui-location` prop; this pins that it does, on the
 * role=button row and nowhere else, and that a row without one carries none.
 * Listed in FORWARDING_PROVEN (src/uiLocations/uiIndex.test.ts).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { unregisteredMarkers } from './guideTargets'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { NavItem } from '../App'
import { UI_LOCATION_ATTR, uiLocation } from '../uiLocations/uiLocation'

vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

// The forwarding proof: every marker the render drew is registered by its ref.
afterEach(() => { expect(unregisteredMarkers()).toEqual([]) })

describe('NavItem and registered UI locations', () => {
  it('puts the marker on the row element a person taps', () => {
    renderWithProviders(
      <NavItem
        path="#"
        label="Search sessions, files, and commands"
        icon={null}
        active={false}
        collapsed={false}
        onClickOverride={() => {}}
        {...uiLocation('shell.menu-search')}
      />,
    )
    const marked = document.querySelectorAll(`[${UI_LOCATION_ATTR}]`)
    expect(marked).toHaveLength(1)
    expect(marked[0].getAttribute(UI_LOCATION_ATTR)).toBe('shell.menu-search')
    expect(marked[0].getAttribute('role')).toBe('button')
    expect(marked[0]).toBe(screen.getByRole('button', { name: /Search sessions, files, and commands/ }))
  })

  it('adds no marker to a row that has none', () => {
    renderWithProviders(<NavItem path="/schedule" label="Schedule" icon={null} active={false} collapsed={false} />)
    expect(document.querySelector(`[${UI_LOCATION_ATTR}]`)).toBeNull()
  })
})
