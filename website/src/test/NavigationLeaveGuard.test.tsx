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
import { render, screen, cleanup, fireEvent, waitFor, act } from '@testing-library/react'
import { BrowserRouter, MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import SidePanelLayout, { useSidePanelLeaveGuard, type SidePanelTab } from '../components/SidePanelLayout'
import { NavigationLeaveGuardProvider, useGuardedLeave, useRegisterNavigationLeaveGuard, useClaimNavigationExitAttempt } from '../components/NavigationLeaveGuard'
import { useMayLeaveAsPromise } from './useMayLeaveAsPromise'
import { deferred } from './helpers'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const TABS: SidePanelTab[] = [
  { key: 'drafts', label: 'Drafts', icon: null },
  { key: 'other', label: 'Other', icon: null },
  { key: 'third', label: 'Third', icon: null },
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
 *  it navigates, exactly as navRow.tsx's NavItem does. It lives OUTSIDE the layout,
 *  which is the whole reason the layout's own guard cannot answer for it. */
function SidebarRow({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate()
  const mayLeave = useMayLeaveAsPromise()
  const claimExitAttempt = useClaimNavigationExitAttempt()
  return (
    <button onClick={() => {
      void (async () => {
        const attempt = claimExitAttempt()
        if (!attempt) return
        try {
          if (!(await mayLeave()) || !attempt.isSourceCurrent()) return
          navigate(to)
        } finally {
          attempt.end()
        }
      })()
    }}>{label}</button>
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

function AsyncExitSource({ ask }: { ask: () => Promise<boolean> }) {
  const navigate = useNavigate()
  useRegisterNavigationLeaveGuard(ask)
  return <>
    <SidebarRow to="/chat" label="Chat" />
    <SidebarRow to="/schedule" label="Schedule" />
    <button onClick={() => navigate('/schedule')}>Force schedule</button>
  </>
}

function AsyncExitDashboard({ ask }: { ask: () => Promise<boolean> }) {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/capabilities" element={<AsyncExitSource ask={ask} />} />
        <Route path="/chat" element={<div data-testid="page">chat</div>} />
        <Route path="/schedule" element={<div data-testid="page">schedule</div>} />
      </Routes>
      <LocationProbe />
    </BrowserRouter>
  )
}

function AsyncTabPane({ ask }: { ask: () => Promise<boolean> }) {
  useSidePanelLeaveGuard(ask)
  return <div data-testid="async-tab-pane">drafts</div>
}

function AsyncTabsDashboard({ ask }: { ask: () => Promise<boolean> }) {
  return (
    <MemoryRouter initialEntries={['/capabilities']}>
      <SidePanelLayout title="Capabilities" tabs={TABS}>
        {tab => tab === 'drafts' ? <AsyncTabPane ask={ask} /> : <div data-testid="plain">{tab}</div>}
      </SidePanelLayout>
      <LocationProbe />
    </MemoryRouter>
  )
}

function SameUrlContinuation({ ask, perform }: { ask: () => boolean; perform: () => void }) {
  const leave = useGuardedLeave()
  useRegisterNavigationLeaveGuard(ask)
  return <button onClick={() => leave(perform, '/capabilities')}>Continue</button>
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

  it('does not ask when the page has nothing at stake', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    click('Chat')
    expect(confirmSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByTestId('page').textContent).toBe('chat'))
  })

  it('continues a same-URL action without asking the leave guard', () => {
    const ask = vi.fn(() => false)
    const perform = vi.fn()
    render(
      <NavigationLeaveGuardProvider>
        <MemoryRouter initialEntries={['/capabilities']}>
          <SameUrlContinuation ask={ask} perform={perform} />
        </MemoryRouter>
      </NavigationLeaveGuardProvider>,
    )

    click('Continue')
    expect(perform).toHaveBeenCalledOnce()
    expect(ask).not.toHaveBeenCalled()
  })

  it('keeps the first of two rapid nav-row attempts and drops the later one', async () => {
    window.history.replaceState(null, '', '/capabilities')
    const answer = deferred<boolean>()
    const ask = vi.fn(() => answer.promise)
    render(<NavigationLeaveGuardProvider><AsyncExitDashboard ask={ask} /></NavigationLeaveGuardProvider>)
    click('Chat')
    click('Schedule')
    await waitFor(() => expect(ask).toHaveBeenCalledTimes(1))
    await act(async () => { answer.resolve(true) })
    await waitFor(() => expect(loc()).toBe('/chat'))
  })

  it('abandons a pending route target when its source route changes', async () => {
    window.history.replaceState(null, '', '/capabilities')
    const answer = deferred<boolean>()
    const ask = vi.fn(() => answer.promise)
    render(<NavigationLeaveGuardProvider><AsyncExitDashboard ask={ask} /></NavigationLeaveGuardProvider>)
    click('Chat')
    await waitFor(() => expect(ask).toHaveBeenCalledTimes(1))
    click('Force schedule')
    await waitFor(() => expect(loc()).toBe('/schedule'))
    await act(async () => { answer.resolve(true) })
    expect(loc()).toBe('/schedule')
  })

  it('skips a guard that unregistered while an earlier answer was pending', async () => {
    window.history.replaceState(null, '', '/capabilities')
    const answer = deferred<boolean>()
    const first = vi.fn(() => answer.promise)
    const second = vi.fn(() => false)
    function Second() { useRegisterNavigationLeaveGuard(second); return null }
    function Host() {
      const [showSecond, setShowSecond] = React.useState(true)
      return <>
        <AsyncExitSource ask={first} />
        {showSecond && <Second />}
        <button onClick={() => setShowSecond(false)}>Close second</button>
      </>
    }
    render(
      <NavigationLeaveGuardProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/capabilities" element={<Host />} />
            <Route path="/chat" element={<div data-testid="page">chat</div>} />
          </Routes>
          <LocationProbe />
        </BrowserRouter>
      </NavigationLeaveGuardProvider>,
    )
    click('Chat')
    await waitFor(() => expect(first).toHaveBeenCalledTimes(1))
    click('Close second')
    await act(async () => { answer.resolve(true) })
    await waitFor(() => expect(loc()).toBe('/chat'))
    expect(second).not.toHaveBeenCalled()
  })

  it('treats a guard that throws as "stay" and frees the slot for the next exit', async () => {
    window.history.replaceState(null, '', '/capabilities')
    let throws = true
    const ask = vi.fn((): boolean => { if (throws) throw new Error('guard failed'); return true })
    function Source() {
      useRegisterNavigationLeaveGuard(ask)
      const leave = useGuardedLeave()
      const navigate = useNavigate()
      return <button onClick={() => leave(() => navigate('/chat'))}>Leave</button>
    }
    render(
      <NavigationLeaveGuardProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/capabilities" element={<Source />} />
            <Route path="/chat" element={<div data-testid="page">chat</div>} />
          </Routes>
          <LocationProbe />
        </BrowserRouter>
      </NavigationLeaveGuardProvider>,
    )
    click('Leave')
    expect(loc()).toBe('/capabilities')
    throws = false
    click('Leave')
    await waitFor(() => expect(loc()).toBe('/chat'))
  })

  it('keeps the first of two rapid tab attempts and drops the later tab', async () => {
    const answer = deferred<boolean>()
    const ask = vi.fn(() => answer.promise)
    render(<NavigationLeaveGuardProvider><AsyncTabsDashboard ask={ask} /></NavigationLeaveGuardProvider>)
    click('Other')
    click('Third')
    await waitFor(() => expect(ask).toHaveBeenCalledTimes(1))
    await act(async () => { answer.resolve(true) })
    await waitFor(() => expect(screen.getByTestId('plain').textContent).toBe('other'))
    expect(loc()).toBe('/capabilities?tab=other')
  })

  it('runs a shell exit in the same click when a side panel has nothing at stake', () => {
    const perform = vi.fn()
    function ShellExit() {
      const leave = useGuardedLeave()
      return <button onClick={() => leave(perform)}>Shell exit</button>
    }
    function SyncPane() { useSidePanelLeaveGuard(() => true); return <div>pane</div> }
    render(
      <NavigationLeaveGuardProvider>
        <MemoryRouter initialEntries={['/capabilities']}>
          <ShellExit />
          <SidePanelLayout title="Capabilities" tabs={TABS}>
            {tab => tab === 'drafts' ? <SyncPane /> : <div>{tab}</div>}
          </SidePanelLayout>
        </MemoryRouter>
      </NavigationLeaveGuardProvider>,
    )
    click('Shell exit')
    expect(perform).toHaveBeenCalledOnce()
  })

  it('frees the exit slot when a pane guard throws on a tab switch', async () => {
    let throws = true
    const ask = vi.fn((): boolean => { if (throws) throw new Error('guard failed'); return true })
    function ThrowingPane() { useSidePanelLeaveGuard(ask); return <div data-testid="async-tab-pane">drafts</div> }
    render(
      <NavigationLeaveGuardProvider>
        <MemoryRouter initialEntries={['/capabilities']}>
          <SidePanelLayout title="Capabilities" tabs={TABS}>
            {tab => tab === 'drafts' ? <ThrowingPane /> : <div data-testid="plain">{tab}</div>}
          </SidePanelLayout>
          <LocationProbe />
        </MemoryRouter>
      </NavigationLeaveGuardProvider>,
    )
    click('Other')
    expect(screen.getByTestId('async-tab-pane')).toBeInTheDocument()
    throws = false
    click('Other')
    await waitFor(() => expect(screen.getByTestId('plain').textContent).toBe('other'))
  })

  it('keeps the page mounted with its draft when the confirm is declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    typeDraft('half-written prompt')
    click('Chat')
    await waitFor(() => expect(confirmSpy).toHaveBeenCalled())
    // Not just "the URL is unchanged" but "the text is still there": a veto that
    // stopped the URL write while the page unmounted anyway would pass a
    // URL-only assertion and still lose the draft.
    expect(draftValue()).toBe('half-written prompt')
    expect(screen.queryByTestId('page')).toBeNull()
    expect(loc()).toBe('/capabilities')
  })

  it('navigates once the confirm is accepted', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    typeDraft('half-written prompt')
    click('Chat')
    await waitFor(() => expect(screen.getByTestId('page').textContent).toBe('chat'))
    expect(loc()).toBe('/chat')
  })

  it('reads the current draft, not the one from the registering render', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    typeDraft('typed then thrown away')
    typeDraft('')
    click('Chat')
    expect(confirmSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByTestId('page').textContent).toBe('chat'))
  })

  it('drops the guard when the page that registered it is gone', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    typeDraft('half-written prompt')
    click('Chat')
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('page').textContent).toBe('chat'))
    // Capabilities is unmounted; navigating on from Chat must not consult the
    // dead guard, whose closure still holds the abandoned text.
    confirmSpy.mockClear()
    click('Schedule')
    expect(confirmSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(loc()).toBe('/schedule'))
  })

  it('lets a pane still switch tabs inside the page it never left', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    typeDraft('half-written prompt')
    // The rail is the layout's own exit and keeps asking through the same guard:
    // forwarding it outward must not consume or shadow the in-layout ask.
    click('Other')
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('plain').textContent).toBe('other'))
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

    it('asks EVERY registered guard, so neither surface\'s draft is left unprotected', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
      renderBoth()
      // The second surface registered last. With one slot this draft was the only one
      // asked about and the first pane's text went silently.
      typeDraft('half-written prompt')
      click('Chat')
      await waitFor(() => expect(confirmSpy).toHaveBeenCalledWith('Discard unsaved changes?'))
      expect(draftValue()).toBe('half-written prompt')
      expect(loc()).toBe('/capabilities')
    })

    it('stops at the first refusal rather than asking about a second draft too', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
      renderBoth()
      typeDraft('half-written prompt')
      typeSecond('and a second one')
      click('Chat')
      // A user who has already said "stay" must not then be asked about a draft for a
      // navigation that is no longer going to happen.
      await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
      expect(loc()).toBe('/capabilities')
    })

    // The stake's own two-publisher rule — one surface going clean must not disarm the
    // other one's still-typed draft — is not pinned here. Its only reader is
    // `NavigationBackGuard`, which subscribes imperatively against real `window.history`
    // and is inert under this file's MemoryRouter, and a probe hook that existed solely
    // for a test to read the boolean is a public API with no production caller. The rule
    // is stated at `publishStake` instead; what a test can still see is the page arming
    // the browser prompt off the same flag (`MembersPage.schedules.test.tsx`).

    it('needs BOTH to allow before it leaves', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
      renderBoth()
      typeDraft('half-written prompt')
      typeSecond('and a second one')
      click('Chat')
      await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(screen.getByTestId('page').textContent).toBe('chat'))
    })
  })

  it('degrades to "may leave" with no provider, rather than crashing', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<Dashboard />)
    typeDraft('half-written prompt')
    click('Chat')
    // No channel to publish into and none to ask: the sidebar navigates. Pinned
    // because the layout is rendered standalone in other tests and embedded
    // surfaces, where a hook that assumed a provider would throw.
    expect(confirmSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByTestId('page').textContent).toBe('chat'))
  })
})
