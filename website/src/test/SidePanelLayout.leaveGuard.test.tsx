/**
 * SidePanelLayout's panes are mounted conditionally (`{tab === '<key>' && <Tab
 * />}`), so a rail click UNMOUNTS the pane and takes its component-local state
 * with it. A pane holding a draft the user typed cannot notice that on its own:
 * React fires nothing before an unmount that a confirm could answer, and the
 * click belongs to the shell. `useSidePanelLeaveGuard` is how the pane gets a
 * veto.
 *
 * What these pin, beyond "a guard is consulted":
 *  - the veto is real — a declined confirm must leave the pane MOUNTED WITH ITS
 *    TEXT, not merely leave the URL alone;
 *  - clicking the tab already shown never asks, because nothing unmounts (a
 *    confirm the user did not earn teaches them to click through the one that
 *    matters);
 *  - the guard is re-read on every invocation, so a draft typed after the
 *    registering render is visible to it — registering the closure itself would
 *    pin the first render's empty draft and silently lose exactly the text this
 *    exists to protect;
 *  - a guard dies with its registrant, so an unmounted pane cannot hold the
 *    rail hostage.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import SidePanelLayout, { useSidePanelLeaveGuard, type SidePanelTab } from '../components/SidePanelLayout'

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const TABS: SidePanelTab[] = [
  { key: 'drafts', label: 'Drafts', icon: null },
  { key: 'other', label: 'Other', icon: null },
  { key: 'third', label: 'Third', icon: null },
]

/** A pane shaped like the real ones: the draft lives in its own useState, so an
 *  unmount is what destroys it. Asks only when something is actually typed. */
function DraftPane() {
  const [draft, setDraft] = React.useState('')
  useSidePanelLeaveGuard(() => !draft || confirm('Discard unsaved changes?'))
  return (
    <input
      aria-label="draft"
      value={draft}
      onChange={e => setDraft((e.target as HTMLInputElement).value)}
    />
  )
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

function renderPage(url = '/capabilities') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <SidePanelLayout title="Capabilities" tabs={TABS}>
        {tab => <>
          {tab === 'drafts' && <DraftPane />}
          {tab !== 'drafts' && <div data-testid="plain">{tab}</div>}
        </>}
      </SidePanelLayout>
      <LocationProbe />
    </MemoryRouter>,
  )
}

const typeDraft = (value: string) =>
  fireEvent.change(screen.getByLabelText('draft'), { target: { value } })
const clickTab = (name: string) => fireEvent.click(screen.getByRole('button', { name }))
const draftValue = () => (screen.getByLabelText('draft') as HTMLInputElement).value
const loc = () => screen.getByTestId('loc').textContent

describe('SidePanelLayout leave guard', () => {
  beforeEach(() => { mobile = false; sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('does not ask when the pane has nothing at stake', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage()
    clickTab('Other')
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.getByTestId('plain').textContent).toBe('other')
  })

  it('keeps the pane mounted with its draft when the confirm is declined', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage()
    typeDraft('half-written prompt')
    clickTab('Other')
    expect(confirmSpy).toHaveBeenCalled()
    // The whole point: not just "the URL is unchanged" but "the text is still
    // there". A guard that vetoed the param write while the pane unmounted
    // anyway would pass a URL-only assertion and still lose the draft.
    expect(draftValue()).toBe('half-written prompt')
    expect(screen.queryByTestId('plain')).toBeNull()
    expect(loc()).toBe('/capabilities')
  })

  it('switches once the confirm is accepted', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderPage()
    typeDraft('half-written prompt')
    clickTab('Other')
    expect(screen.getByTestId('plain').textContent).toBe('other')
    expect(loc()).toBe('/capabilities?tab=other')
  })

  it('never asks when the clicked tab is the one already shown', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage()
    typeDraft('half-written prompt')
    clickTab('Drafts')
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(draftValue()).toBe('half-written prompt')
  })

  it('reads the current draft, not the one from the registering render', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage()
    typeDraft('typed then thrown away')
    typeDraft('')
    clickTab('Other')
    // Emptied again: there is nothing to lose, so no ask — and the switch runs.
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.getByTestId('plain').textContent).toBe('other')
  })

  it('drops the guard when its pane unmounts', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderPage()
    typeDraft('half-written prompt')
    clickTab('Other')
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    // The draft pane is gone; a switch between two plain tabs must not consult
    // the dead guard's closure, which still closes over the abandoned text.
    confirmSpy.mockClear()
    clickTab('Third')
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.getByTestId('plain').textContent).toBe('third')
  })

  it('guards the mobile back bar, which unmounts the pane just as a switch does', () => {
    mobile = true
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage('/capabilities?tab=drafts')
    typeDraft('half-written prompt')
    fireEvent.click(screen.getByRole('button', { name: /Capabilities/ }))
    expect(confirmSpy).toHaveBeenCalled()
    expect(draftValue()).toBe('half-written prompt')
    expect(loc()).toBe('/capabilities?tab=drafts')
  })
})
