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
import { render, screen, cleanup, fireEvent, within, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import SidePanelLayout, { useSidePanelLeaveGuard, type SidePanelTab } from '../components/SidePanelLayout'

import { useConfirm } from '../components/ConfirmDialog'

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
  const { confirm, confirmDialog } = useConfirm()
  useSidePanelLeaveGuard(() => !draft || confirm({ title: 'Discard unsaved changes?', confirmLabel: 'Discard changes' }))
  return <>
    <input
      aria-label="draft"
      value={draft}
      onChange={e => setDraft((e.target as HTMLInputElement).value)}
    />
    {confirmDialog}
  </>
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
  const settleDiscard = async (confirmed: boolean) => {
    const dialog = await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })
    fireEvent.click(within(dialog).getByRole('button', { name: confirmed ? 'Discard changes' : 'Cancel' }))
  }

  beforeEach(() => { mobile = false; sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('does not ask when the pane has nothing at stake', async () => {
    renderPage()
    clickTab('Other')
    await waitFor(() => expect(screen.getByTestId('plain').textContent).toBe('other'))
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).toBeNull()
  })

  it('keeps the pane mounted with its draft when the confirm is declined', async () => {
    renderPage()
    typeDraft('half-written prompt')
    clickTab('Other')
    await settleDiscard(false)
    expect(draftValue()).toBe('half-written prompt')
    expect(screen.queryByTestId('plain')).toBeNull()
    expect(loc()).toBe('/capabilities')
  })

  it('switches once the confirm is accepted', async () => {
    renderPage()
    typeDraft('half-written prompt')
    clickTab('Other')
    await settleDiscard(true)
    await waitFor(() => expect(screen.getByTestId('plain').textContent).toBe('other'))
    expect(loc()).toBe('/capabilities?tab=other')
  })

  it('never asks when the clicked tab is the one already shown', () => {
    renderPage()
    typeDraft('half-written prompt')
    clickTab('Drafts')
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).toBeNull()
    expect(draftValue()).toBe('half-written prompt')
  })

  it('reads the current draft, not the one from the registering render', async () => {
    renderPage()
    typeDraft('typed then thrown away')
    typeDraft('')
    clickTab('Other')
    await waitFor(() => expect(screen.getByTestId('plain').textContent).toBe('other'))
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).toBeNull()
  })

  it('drops the guard when its pane unmounts', async () => {
    renderPage()
    typeDraft('half-written prompt')
    clickTab('Other')
    await settleDiscard(true)
    await waitFor(() => expect(screen.getByTestId('plain').textContent).toBe('other'))
    clickTab('Third')
    await waitFor(() => expect(screen.getByTestId('plain').textContent).toBe('third'))
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).toBeNull()
  })

  it('guards the mobile back bar, which unmounts the pane just as a switch does', async () => {
    mobile = true
    renderPage('/capabilities?tab=drafts')
    typeDraft('half-written prompt')
    fireEvent.click(screen.getByRole('button', { name: /Capabilities/ }))
    await settleDiscard(false)
    expect(draftValue()).toBe('half-written prompt')
    expect(loc()).toBe('/capabilities?tab=drafts')
  })
})
