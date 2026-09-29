/**
 * A draft typed into a SidePanelLayout pane survives the exits that layout owns
 * (its rail, its mobile back bar) and a real document unload (`beforeunload`).
 * Neither reaches an IN-APP ROUTE CHANGE: the global sidebar replaces the whole
 * page, the document never unloads so `beforeunload` is silent, and the click
 * belongs to the app shell rather than to the layout. That was silent data loss
 * on the app's most-used navigation.
 *
 * `NavigationLeaveGuard` is the channel that closes it, and these pin the parts
 * that make it honest rather than decorative:
 *  - the veto is real — a declined confirm must leave the pane MOUNTED WITH ITS
 *    TEXT, not merely leave the URL alone;
 *  - a pane registers its dirtiness ONCE. It talks only to
 *    `useSidePanelLeaveGuard`; the layout forwards that same answer outward, so
 *    the sidebar asking is the pane answering;
 *  - the guard is re-read on every ask, so text typed after the registering
 *    render is visible to it;
 *  - a guard dies with the page that registered it, so a page the user already
 *    left cannot hold the sidebar hostage;
 *  - outside a provider the hooks degrade to "may leave", so a surface rendered
 *    standalone navigates instead of crashing.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import SidePanelLayout, { useSidePanelLeaveGuard, type SidePanelTab } from '../components/SidePanelLayout'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const TABS: SidePanelTab[] = [
  { key: 'drafts', label: 'Drafts', icon: null },
  { key: 'other', label: 'Other', icon: null },
]

/** A pane shaped like PromptsTab's editor: the draft lives in component-local
 *  state, so an unmount is what destroys it. */
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

function CapabilitiesLike() {
  return (
    <SidePanelLayout title="Capabilities" tabs={TABS}>
      {tab => <>
        {tab === 'drafts' && <DraftPane />}
        {tab !== 'drafts' && <div data-testid="plain">{tab}</div>}
      </>}
    </SidePanelLayout>
  )
}

/** The app shell's nav row, reduced to the one thing under test: it asks before
 *  it navigates, exactly as App.tsx's NavItem does. It lives OUTSIDE the layout,
 *  which is the whole reason the layout's own guard cannot answer for it. */
function SidebarRow({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate()
  const mayLeave = useMayLeaveForNavigation()
  return (
    <button onClick={() => { if (!mayLeave()) return; navigate(to) }}>{label}</button>
  )
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

function Dashboard() {
  return (
    <MemoryRouter initialEntries={['/capabilities']}>
      <SidebarRow to="/chat" label="Chat" />
      <SidebarRow to="/schedule" label="Schedule" />
      <Routes>
        <Route path="/capabilities" element={<CapabilitiesLike />} />
        <Route path="/chat" element={<div data-testid="page">chat</div>} />
        <Route path="/schedule" element={<div data-testid="page">schedule</div>} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>
  )
}

/** Provider on the outside, as main.tsx mounts it around the router. */
const renderDashboard = () =>
  render(<NavigationLeaveGuardProvider><Dashboard /></NavigationLeaveGuardProvider>)

const typeDraft = (value: string) =>
  fireEvent.change(screen.getByLabelText('draft'), { target: { value } })
const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }))
const draftValue = () => (screen.getByLabelText('draft') as HTMLInputElement).value
const loc = () => screen.getByTestId('loc').textContent

describe('in-app navigation leave guard', () => {
  beforeEach(() => { sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('does not ask when the page has nothing at stake', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    click('Chat')
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.getByTestId('page').textContent).toBe('chat')
  })

  it('keeps the page mounted with its draft when the confirm is declined', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    typeDraft('half-written prompt')
    click('Chat')
    expect(confirmSpy).toHaveBeenCalled()
    // Not just "the URL is unchanged" but "the text is still there": a veto that
    // stopped the URL write while the page unmounted anyway would pass a
    // URL-only assertion and still lose the draft.
    expect(draftValue()).toBe('half-written prompt')
    expect(screen.queryByTestId('page')).toBeNull()
    expect(loc()).toBe('/capabilities')
  })

  it('navigates once the confirm is accepted', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    typeDraft('half-written prompt')
    click('Chat')
    expect(screen.getByTestId('page').textContent).toBe('chat')
    expect(loc()).toBe('/chat')
  })

  it('reads the current draft, not the one from the registering render', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    typeDraft('typed then thrown away')
    typeDraft('')
    click('Chat')
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.getByTestId('page').textContent).toBe('chat')
  })

  it('drops the guard when the page that registered it is gone', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    typeDraft('half-written prompt')
    click('Chat')
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    // Capabilities is unmounted; navigating on from Chat must not consult the
    // dead guard, whose closure still holds the abandoned text.
    confirmSpy.mockClear()
    click('Schedule')
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(loc()).toBe('/schedule')
  })

  it('lets a pane still switch tabs inside the page it never left', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    typeDraft('half-written prompt')
    // The rail is the layout's own exit and keeps asking through the same guard:
    // forwarding it outward must not consume or shadow the in-layout ask.
    click('Other')
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('plain').textContent).toBe('other')
    expect(loc()).toBe('/capabilities?tab=other')
  })

  describe('two surfaces on one page', () => {
    /* One page can hold two drafts -- the Crewmates page holds the New crewmate
     * dialog and the side panel's Schedules create form -- and the channel used to
     * keep ONE slot, so the second registrant silently replaced the first and only
     * the last-mounted draft was still protected. */
    function SecondDraft() {
      const [text, setText] = React.useState('')
      useRegisterNavigationLeaveGuard(() => !text || confirm('Discard the second draft?'))
      return (
        <input
          aria-label="second"
          value={text}
          onChange={e => setText((e.target as HTMLInputElement).value)}
        />
      )
    }
    const renderBoth = () => render(
      <NavigationLeaveGuardProvider>
        <Dashboard />
        <SecondDraft />
      </NavigationLeaveGuardProvider>,
    )
    const typeSecond = (value: string) =>
      fireEvent.change(screen.getByLabelText('second'), { target: { value } })

    it('asks EVERY registered guard, so neither surface\'s draft is left unprotected', () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
      renderBoth()
      // The second surface registered last. With one slot this draft was the only one
      // asked about and the first pane's text went silently.
      typeDraft('half-written prompt')
      click('Chat')
      expect(confirmSpy).toHaveBeenCalledWith('Discard unsaved changes?')
      expect(draftValue()).toBe('half-written prompt')
      expect(loc()).toBe('/capabilities')
    })

    it('stops at the first refusal rather than asking about a second draft too', () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
      renderBoth()
      typeDraft('half-written prompt')
      typeSecond('and a second one')
      click('Chat')
      // A user who has already said "stay" must not then be asked about a draft for a
      // navigation that is no longer going to happen.
      expect(confirmSpy).toHaveBeenCalledTimes(1)
      expect(loc()).toBe('/capabilities')
    })

    // The stake's own two-publisher rule — one surface going clean must not disarm the
    // other one's still-typed draft — is not pinned here. Its only reader is
    // `NavigationBackGuard`, which subscribes imperatively against real `window.history`
    // and is inert under this file's MemoryRouter, and a probe hook that existed solely
    // for a test to read the boolean is a public API with no production caller. The rule
    // is stated at `publishStake` instead; what a test can still see is the page arming
    // the browser prompt off the same flag (`MembersPage.schedules.test.tsx`).

    it('needs BOTH to allow before it leaves', () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
      renderBoth()
      typeDraft('half-written prompt')
      typeSecond('and a second one')
      click('Chat')
      expect(confirmSpy).toHaveBeenCalledTimes(2)
      expect(screen.getByTestId('page').textContent).toBe('chat')
    })
  })

  it('degrades to "may leave" with no provider, rather than crashing', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<Dashboard />)
    typeDraft('half-written prompt')
    click('Chat')
    // No channel to publish into and none to ask: the sidebar navigates. Pinned
    // because the layout is rendered standalone in other tests and embedded
    // surfaces, where a hook that assumed a provider would throw.
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.getByTestId('page').textContent).toBe('chat')
  })
})
