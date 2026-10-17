/**
 * The browser's own Back button asks the page on screen before it discards a
 * draft — and never pays for that with history the user owns.
 *
 * Back was the last exit nothing in the app could reach. `beforeunload` is
 * silent for it (the document never unloads), the gesture belongs to no
 * component so there is no click handler to wire, and `useBlocker` — the
 * mechanism built for exactly this — needs a data router the dashboard does not
 * mount. `NavigationBackGuard` closes it through the stack instead: while the
 * page publishes work at stake it keeps one duplicate history entry, so the
 * first Back lands on the page's own entry with the address unchanged and the
 * page still mounted, and the veto is asked there.
 *
 * These pin the parts that make that honest rather than decorative:
 *  - a declined confirm leaves the page MOUNTED WITH ITS TEXT, not merely the
 *    URL alone — and the NEXT Back is still caught, so refusing once does not
 *    spend the guard;
 *  - an accepted confirm actually leaves, in ONE press;
 *  - a page with nothing at stake is ABSENT from the stack — no duplicate entry,
 *    so Back means what it always meant;
 *  - the guard never TAKES anything: it does not push while the user holds a
 *    Forward branch, and it does not push at all until it can prove where the
 *    top of the stack is;
 *  - the same guard answers, so a page that saves its work stops being asked
 *    about — and one that dirties again is still defended;
 *  - a duplicate is never a destination: a press that lands on one carries
 *    through instead of appearing to do nothing.
 *
 * Uses a real `<BrowserRouter>` over jsdom's history rather than MemoryRouter,
 * because the whole subject is `popstate` and the browser stack — a memory
 * router never fires it. The pane is reached by CLICKING into it, as a user
 * does: the guard reads the top of the stack from a push, and a raw
 * `history.pushState` in a test fixture is not one.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { BrowserRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import SidePanelLayout, { useSidePanelLeaveGuard, type SidePanelTab } from '../components/SidePanelLayout'
import {
  NavigationLeaveGuardProvider,
  NavigationBackGuard,
  useMayLeaveForNavigation,
} from '../components/NavigationLeaveGuard'

// Flipped per describe block: the mobile back bar reaches history through
// `navigate(-1)`, which is the one in-app exit a duplicate entry can collide with.
const viewport = vi.hoisted(() => ({ mobile: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.mobile }))

const TABS: SidePanelTab[] = [
  { key: 'drafts', label: 'Drafts', icon: null },
  { key: 'other', label: 'Other', icon: null },
]

/** A pane shaped like PromptsTab's editor: the draft lives in component-local
 *  state, so an unmount is what destroys it. It publishes the same dirtiness
 *  twice — as the guard's answer and as the stake that arms Back. */
function DraftPane() {
  const [draft, setDraft] = React.useState('')
  useSidePanelLeaveGuard(() => !draft || confirm('Discard unsaved changes?'), !!draft)
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

/** A control that REPLACES the current entry at the same address, which is what
 *  the layout's own writes do (the tab sync, and `backToRoot`'s cold-deep-link
 *  branch). Stands in for them without dragging a second page into this harness. */
function ReplaceHere() {
  const navigate = useNavigate()
  const loc = useLocation()
  return (
    <button onClick={() => navigate(loc.pathname + loc.search, { replace: true })}>
      Rewrite this entry
    </button>
  )
}

/** The app shell's nav row, reduced to the one thing that matters here: it asks
 *  before it navigates, exactly as App.tsx's NavItem does. */
function SidebarRow({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate()
  const mayLeave = useMayLeaveForNavigation()
  return <button onClick={() => { if (!mayLeave()) return; navigate(to) }}>{label}</button>
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

const renderDashboard = () =>
  render(
    <NavigationLeaveGuardProvider>
      <BrowserRouter>
        <NavigationBackGuard />
        <SidebarRow to="/capabilities" label="Go to Capabilities" />
        <SidebarRow to="/chat" label="Chat" />
        <ReplaceHere />
        <Routes>
          <Route path="/capabilities" element={<CapabilitiesLike />} />
          <Route path="/chat" element={<div data-testid="page">chat</div>} />
          <Route path="/schedule" element={<div data-testid="page">schedule</div>} />
        </Routes>
        <LocationProbe />
      </BrowserRouter>
    </NavigationLeaveGuardProvider>,
  )

const click = (name: string | RegExp) => fireEvent.click(screen.getByRole('button', { name }))
const typeDraft = (value: string) =>
  fireEvent.change(screen.getByLabelText('draft'), { target: { value } })
const draftValue = () => (screen.getByLabelText('draft') as HTMLInputElement).value
const loc = () => screen.getByTestId('loc').textContent

/** The page the user arrived on, so Back has somewhere real to go. */
const startAtSchedule = () => { window.history.replaceState(null, '', '/schedule') }

/** Enter the drafting page the way a user does — a sidebar click, which is a real
 *  router PUSH. That push is also what tells the guard where the top of the stack
 *  is; without one (a cold load, a reload) it deliberately stays disarmed. */
const enterCapabilities = async () => {
  click('Go to Capabilities')
  await waitFor(() => expect(screen.getByLabelText('draft')).toBeInTheDocument())
}

/** jsdom applies `back()` in a task, and the guard answers inside the resulting
 *  `popstate` — so every assertion about Back has to be awaited. */
const pressBack = async (settle: () => void) => {
  window.history.back()
  await waitFor(settle)
}

describe('browser Back navigation leave guard', () => {
  beforeEach(() => { sessionStorage.clear(); startAtSchedule() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('does not touch the history stack for a page with nothing at stake', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterCapabilities()
    // A clean page must be ABSENT from the stack, not merely quiet: with a
    // duplicate pushed regardless, this Back would land on it and appear to do
    // nothing at all.
    await pressBack(() => expect(loc()).toBe('/schedule'))
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('keeps the page mounted with its draft when the confirm is declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterCapabilities()
    typeDraft('half-written prompt')
    await pressBack(() => expect(confirmSpy).toHaveBeenCalled())
    // Not just "the URL is unchanged" but "the text is still there": a veto that
    // let the page unmount would pass a URL-only assertion and still lose the
    // draft.
    expect(draftValue()).toBe('half-written prompt')
    expect(loc()).toBe('/capabilities')
    expect(screen.queryByTestId('page')).toBeNull()
  })

  it('still catches the next Back after one refusal', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterCapabilities()
    typeDraft('half-written prompt')
    await pressBack(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    // Refusing consumes the duplicate. Without a fresh one the second press walks
    // straight out of the page, which is the same silent loss with one extra
    // click in front of it.
    await pressBack(() => expect(confirmSpy).toHaveBeenCalledTimes(2))
    expect(draftValue()).toBe('half-written prompt')
    expect(loc()).toBe('/capabilities')
  })

  it('leaves on a single press once the confirm is accepted', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    await enterCapabilities()
    typeDraft('half-written prompt')
    // ONE press: the duplicate absorbed the user's pop, so the guard owes the
    // real one. Leaving that out would make Back need two presses on every dirty
    // page — the first would read as a Back that did nothing.
    await pressBack(() => expect(loc()).toBe('/schedule'))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(screen.queryByLabelText('draft')).toBeNull()
  })

  it('stops asking once the work is gone', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterCapabilities()
    typeDraft('typed then thrown away')
    typeDraft('')
    await pressBack(() => expect(loc()).toBe('/schedule'))
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('still guards a draft that was dirtied, undone, and dirtied again', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterCapabilities()
    // Type a character and undo it — the shape of a real edit session. The
    // duplicate minted for the first keystroke must be REUSED, not popped: a
    // popped one becomes a stale entry above us, which the guard's own
    // truncation test then reads as the user's Forward branch, and it could
    // never arm again. The pane would be dirty with Back wide open.
    typeDraft('a')
    typeDraft('')
    typeDraft('half-written prompt')
    await pressBack(() => expect(confirmSpy).toHaveBeenCalled())
    expect(draftValue()).toBe('half-written prompt')
    expect(loc()).toBe('/capabilities')
  })

  it('re-arms over its own duplicate after the user walks back onto the page', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterCapabilities()
    // Dirty, clear, Back, Forward, dirty again — the sequence that leaves the
    // guard's own duplicate sitting one entry ABOVE the page. A guard that has
    // forgotten it owns that entry cannot tell it from a Forward branch the user
    // built, so its truncation test refuses to push and the re-dirtied editor is
    // left with Back wide open. It is ours, so it is replaceable.
    typeDraft('first thoughts')
    typeDraft('')
    await pressBack(() => expect(loc()).toBe('/schedule'))
    window.history.forward()
    await waitFor(() => expect(screen.getByLabelText('draft')).toBeInTheDocument())
    typeDraft('half-written prompt')
    await pressBack(() => expect(confirmSpy).toHaveBeenCalled())
    expect(draftValue()).toBe('half-written prompt')
    expect(loc()).toBe('/capabilities')
  })

  it('re-arms when a replace-write overwrites its duplicate under it', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterCapabilities()
    typeDraft('half-written prompt')
    // A replace overwrites the STATE of the entry it lands on, so the duplicate
    // stops being the guard's own while the guard is still standing on it. Left
    // unnoticed, the page believes it is guarded and Back walks straight out of it
    // — and the stake only re-publishes when it changes, so nothing else notices.
    click('Rewrite this entry')
    await waitFor(() => expect(screen.getByLabelText('draft')).toBeInTheDocument())
    await pressBack(() => expect(confirmSpy).toHaveBeenCalled())
    expect(draftValue()).toBe('half-written prompt')
    expect(loc()).toBe('/capabilities')
  })

  it('does not spend the user\'s Forward branch on a keystroke', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterCapabilities()
    // Build a Forward branch the way a user does: go somewhere, come back.
    click('Chat')
    await waitFor(() => expect(loc()).toBe('/chat'))
    await pressBack(() => expect(loc()).toBe('/capabilities'))
    const lengthBeforeTyping = window.history.length
    typeDraft('half-written prompt')
    // A push truncates everything above it, and this one would fire on a
    // KEYSTROKE — so arming here would destroy /chat, which the user can never
    // get back. The guard stays out of the stack instead: Back is unguarded on
    // this entry (a gap), but nothing is lost.
    expect(window.history.length).toBe(lengthBeforeTyping)
    window.history.forward()
    await waitFor(() => expect(loc()).toBe('/chat'))
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('stays out of the stack entirely until it knows where the top is', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    // Mounted straight onto the drafting page with no push behind it — a cold
    // deep link, or a reload after pressing Back. The platform reports only a
    // TOTAL entry count, so with nothing to calibrate against the guard cannot
    // tell an entry below the app from a Forward branch above it. It refuses to
    // push rather than risk truncating one.
    window.history.replaceState(null, '', '/capabilities')
    renderDashboard()
    await waitFor(() => expect(screen.getByLabelText('draft')).toBeInTheDocument())
    const lengthBeforeTyping = window.history.length
    typeDraft('half-written prompt')
    expect(window.history.length).toBe(lengthBeforeTyping)
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('does not spend a press on a duplicate left behind by a confirmed exit', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    await enterCapabilities()
    typeDraft('half-written prompt')
    // Accepting an in-app exit buries the duplicate under the new page.
    click('Chat')
    await waitFor(() => expect(loc()).toBe('/chat'))
    // Two presses must reach the page before Capabilities. The buried duplicate
    // shows the SAME address as the real entry, so without skipping it the second
    // press would still be sitting on /capabilities — a Back that visibly did
    // nothing.
    await pressBack(() => expect(loc()).toBe('/capabilities'))
    await pressBack(() => expect(loc()).toBe('/schedule'))
  })

  it('does not drag the user back to a page they confirmed leaving', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    await enterCapabilities()
    typeDraft('half-written prompt')
    click('Chat')
    await waitFor(() => expect(loc()).toBe('/chat'))
    expect(screen.getByTestId('page').textContent).toBe('chat')
  })

  it('stands down for a pop that lands PAST the trap', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterCapabilities()
    typeDraft('half-written prompt')
    // A long-press Back menu or history.go(-n) skips the duplicate entirely, so
    // the page is already unmounted by the time the guard hears about it.
    // Correlating any pop with its own duplicate would confirm a draft that is
    // already gone and then pop one entry further.
    window.history.go(-2)
    await waitFor(() => expect(loc()).toBe('/schedule'))
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('degrades to plain Back with no provider, rather than crashing', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(
      <BrowserRouter>
        <NavigationBackGuard />
        <SidebarRow to="/capabilities" label="Go to Capabilities" />
        <Routes>
          <Route path="/capabilities" element={<CapabilitiesLike />} />
          <Route path="/schedule" element={<div data-testid="page">schedule</div>} />
        </Routes>
        <LocationProbe />
      </BrowserRouter>,
    )
    await enterCapabilities()
    typeDraft('half-written prompt')
    // No channel to arm from and none to ask. Pinned because the layout is
    // rendered standalone in other tests and in embedded surfaces, where a guard
    // that assumed a provider would throw on mount.
    await pressBack(() => expect(loc()).toBe('/schedule'))
    expect(confirmSpy).not.toHaveBeenCalled()
  })
})

/**
 * The mobile back bar is the in-app exit that reaches history directly: with a
 * pushed drill-in entry under it, `SidePanelLayout.backToRoot` pops via
 * `navigate(-1)`. A duplicate sitting on top of that entry is what makes this
 * worth pinning — an earlier revision copied the drill-in's `SUBNAV_PUSH_STATE`
 * marker onto the duplicate, so the pop consumed it instead, landed on the
 * identical address, and read as a back bar that did nothing while asking a
 * second time.
 */
describe('browser Back guard alongside the mobile back bar', () => {
  beforeEach(() => { viewport.mobile = true; sessionStorage.clear(); startAtSchedule() })
  afterEach(() => { viewport.mobile = false; vi.restoreAllMocks(); cleanup() })

  /** Mobile opens at the root list; drilling in is what mounts the pane. */
  const enterAndDrillIn = async () => {
    click('Go to Capabilities')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Drafts' })).toBeInTheDocument())
    click('Drafts')
    await waitFor(() => expect(screen.getByLabelText('draft')).toBeInTheDocument())
  }
  /** The layout's own back control, named for the page. The sidebar row is
   *  labelled 'Go to Capabilities' so the two are addressable apart. */
  const backBar = () => click('Capabilities')

  it('asks once and reaches the root list when the back bar is tapped', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    await enterAndDrillIn()
    typeDraft('half-written prompt')
    backBar()
    // ONE confirm: the back bar already asked through the pane's own guard, and
    // the duplicate must not turn that into a second question.
    await waitFor(() => expect(screen.queryByLabelText('draft')).toBeNull())
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    // The root list, not the address it was already at.
    expect(loc()).toBe('/capabilities')
  })

  it('leaves a Back after the accepted back-bar landing on the real entry', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    await enterAndDrillIn()
    typeDraft('half-written prompt')
    backBar()
    await waitFor(() => expect(loc()).toBe('/capabilities'))
    // The back bar took the replace branch, so the entry it rewrote is the one the
    // guard had minted — it is a real root-list destination now. Treating it as an
    // invisible duplicate would carry this press PAST the drill-in entry the user
    // is actually going back to.
    await pressBack(() => expect(loc()).toBe('/capabilities?tab=drafts'))
  })

  it('keeps the drafted pane when the back bar ask is declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await enterAndDrillIn()
    typeDraft('half-written prompt')
    backBar()
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(draftValue()).toBe('half-written prompt')
    // And the browser's own Back is still trapped afterwards.
    await pressBack(() => expect(confirmSpy).toHaveBeenCalledTimes(2))
    expect(draftValue()).toBe('half-written prompt')
  })
})
