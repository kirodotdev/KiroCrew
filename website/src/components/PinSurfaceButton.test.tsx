/**
 * The header pin control that promotes the sub-item you are viewing.
 *
 * It derives its subject from the URL rather than from a prop, which is what
 * keeps `SidePanelLayout` untouched — so the contract worth pinning is mostly
 * about WHICH url yields a control, and about the cap refusal being visible
 * rather than silent.
 *
 * The button is located by role, and state is read from `aria-pressed` and
 * `disabled` rather than from label text: asserting on the label would couple
 * these cases to catalog wording, and a control located BY the label it is
 * about can pass by finding some other element rendering the same string.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import { PinSurfaceButton } from './PinSurfaceButton'
import { NAV_PINNED_KEY, NAV_PINNED_LIMIT, readNavPinned } from '../lib/navPinned'
import { getPinnableSurfaces } from '../surfaces/registry'
import '../surfaces/builtins'

function renderAt(url: string, props: { defaultTab?: string } = {}) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <PinSurfaceButton {...props} />
    </MemoryRouter>,
  )
}

const button = () => screen.queryByRole('button')

beforeEach(() => {
  localStorage.clear()
})

describe('PinSurfaceButton subject resolution', () => {
  it('renders a control on a url that names a pinnable sub-item', () => {
    // Control for the three absence cases below: if this did not render, those
    // would pass without testing anything.
    renderAt('/capabilities?tab=steering')
    expect(button()).not.toBeNull()
  })

  it('renders nothing on an unrelated page', () => {
    renderAt('/chat')
    expect(button()).toBeNull()
  })

  it('renders nothing with no tab named and NO default supplied', () => {
    // The original version of this case asserted the absence for the whole
    // param-less URL, reasoning that SidePanelLayout "shows its first tab before
    // writing the param, so there is no sub-item to attribute a pin to yet".
    // That premise was wrong: on desktop the param-less URL IS the first tab,
    // permanently -- SidePanelLayout deletes the param when you pick that tab and
    // never writes it back -- so the control was missing on the landing view of
    // /capabilities rather than briefly. What remains true is narrower and is what
    // this case now pins: with no default supplied by the host, the control must
    // stay inert instead of guessing a tab.
    renderAt('/capabilities')
    expect(button()).toBeNull()
  })

  it('renders nothing for a tab that is not registered as pinnable', () => {
    renderAt('/capabilities?tab=not-a-real-tab')
    expect(button()).toBeNull()
  })

  it('names the right subject, so two tabs do not share one control', () => {
    const { unmount } = renderAt('/capabilities?tab=steering')
    fireEvent.click(button() as HTMLElement)
    expect(readNavPinned()).toEqual(new Set(['capabilities-steering']))
    unmount()

    renderAt('/capabilities?tab=skills')
    fireEvent.click(button() as HTMLElement)
    expect(readNavPinned()).toEqual(new Set(['capabilities-steering', 'capabilities-skills']))
  })
})

describe('PinSurfaceButton toggling', () => {
  it('pins on click and reports pressed state', () => {
    renderAt('/capabilities?tab=steering')
    expect(button()).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(button() as HTMLElement)
    expect(readNavPinned().has('capabilities-steering')).toBe(true)
    expect(button()).toHaveAttribute('aria-pressed', 'true')
  })

  it('unpins on a second click', () => {
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(['capabilities-steering']))
    renderAt('/capabilities?tab=steering')
    expect(button()).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(button() as HTMLElement)
    expect(readNavPinned().has('capabilities-steering')).toBe(false)
    expect(button()).toHaveAttribute('aria-pressed', 'false')
  })

  it('carries a non-empty accessible name in both states', () => {
    renderAt('/capabilities?tab=steering')
    const unpinnedName = button()?.getAttribute('aria-label')
    expect(unpinnedName).toBeTruthy()
    fireEvent.click(button() as HTMLElement)
    const pinnedName = button()?.getAttribute('aria-label')
    expect(pinnedName).toBeTruthy()
    // The two states must not announce identically, or a screen reader user
    // cannot tell a pin from an unpin.
    expect(pinnedName).not.toBe(unpinnedName)
  })
})

describe('PinSurfaceButton at the cap', () => {
  /** Fill the pinned set with ids that are NOT the subject under test. */
  function fillCapExcluding(subject: string) {
    const others = getPinnableSurfaces().map(s => s.navId).filter(id => id !== subject)
    expect(others.length).toBeGreaterThanOrEqual(NAV_PINNED_LIMIT)
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(others.slice(0, NAV_PINNED_LIMIT)))
  }

  it('disables the control for an unpinned sub-item, rather than dropping the click', () => {
    fillCapExcluding('capabilities-steering')
    renderAt('/capabilities?tab=steering')
    expect(button()).toBeDisabled()
    const before = readNavPinned()
    fireEvent.click(button() as HTMLElement)
    expect(readNavPinned()).toEqual(before)
  })

  it('keeps the control live for an ALREADY pinned sub-item, so the cap is escapable', () => {
    // The regression this guards: gating on size alone would disable every
    // control at the cap, and a user who filled it could never unpin anything.
    const ids = getPinnableSurfaces().map(s => s.navId).slice(0, NAV_PINNED_LIMIT)
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(ids))
    renderAt(`/capabilities?tab=${ids[0].replace('capabilities-', '')}`)
    expect(button()).not.toBeDisabled()
    fireEvent.click(button() as HTMLElement)
    expect(readNavPinned().has(ids[0])).toBe(false)
  })
  it('offers the DEFAULT tab when the URL carries no tab param', () => {
    // SidePanelLayout expresses "first tab" as the absence of ?tab= on desktop,
    // so /capabilities lands on Crews with no param. Matching the raw param alone
    // found no surface and the control vanished on the most prominent sub-item --
    // the one tab the feature most needs to cover. Reported by review, not by
    // this suite: every other case here passes an explicit ?tab=.
    renderAt('/capabilities', { defaultTab: 'crews' })

    const btn = screen.getByTestId('pin-surface-button')
    expect(btn).not.toBeNull()
    expect(btn.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(btn)
    expect(readNavPinned().has('capabilities-crews')).toBe(true)
  })

  it('at the cap the label states the limit instead of promising the pin', () => {
    // A disabled control whose tooltip still reads "Pin X to the sidebar" promises
    // the action it refuses, and a blind read of the at-cap frame confirmed a
    // reader takes it for a live pin. Asserting the label is the only observable:
    // a title attribute never appears in a screenshot.
    localStorage.setItem(
      NAV_PINNED_KEY,
      JSON.stringify(['capabilities-crews', 'capabilities-templates', 'capabilities-skills', 'capabilities-mcp', 'capabilities-knowledge']),
    )
    renderAt('/capabilities?tab=steering')

    const btn = screen.getByTestId('pin-surface-button')
    expect(btn).toBeDisabled()
    const label = btn.getAttribute('title') ?? ''
    expect(label).toContain(String(NAV_PINNED_LIMIT))
    expect(label).not.toMatch(/^Pin /)
    expect(btn.getAttribute('aria-label')).toBe(label)
  })

})
