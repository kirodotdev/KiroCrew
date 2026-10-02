import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation } from '../../components/NavigationLeaveGuard'

/* The side panel is kept mounted while the Dashboard body holds an UNSENT
 * ANSWER, and released as soon as it does not.
 *
 * Why the page needs telling: a pending question's answer lives only in
 * `QuestionCard`'s own state and `CommandCenterPanel`'s draft bookkeeping. Nothing
 * outside that subtree can see it, so an unmount is the typed text being thrown
 * away and the page would never know. `CommandCenterPanel` therefore reports the
 * state up (`onDraftStateChange`) and the page holds its panel on that alone.
 *
 * Two earlier rules are pinned against here by their consequences, because each
 * looked reasonable and broke something a test had to catch:
 *
 *  - Hold whenever the Dashboard tab has been on screen. Dashboard is the landing
 *    tab, so that held the panel for every crewmate forever; it also kept every
 *    OTHER leading body alive behind the hidden panel, and a Schedules draft the
 *    person had explicitly DISCARDED came back on the next open.
 *  - Hold only while Dashboard is the ACTIVE tab. That fixed the Schedules case
 *    and lost the answer whenever someone typed, left the tab, and then closed
 *    the panel -- two ordinary steps.
 *
 * `CommandCenterPanel` is stubbed here so the report can be driven directly; its
 * own half (deriving the flag from a card's draft) is the command-centre tests'
 * business. The panel's shell is the real one, which is what the hold acts on.
 */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    sessionCrewLogProjections: vi.fn(() => Promise.resolve({ folds: {}, resolved: true, writesDrained: true })),
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
  },
}))

/* The stub exposes the report as two buttons, so a case can say "a draft exists"
 * exactly as a half-filled QuestionCard would. It also renders `publishedView`,
 * which is where the Dashboard's own frame lives: that frame is the second thing
 * in this body an unmount destroys, and it reports itself the same way. */
vi.mock('../chat/command-center/CommandCenterPanel', () => ({
  default: ({ onDraftStateChange, publishedView }: { onDraftStateChange?: (hasDraft: boolean) => void, publishedView?: { content: React.ReactNode } }) => (
    <div data-testid="command-center-stub">
      <button onClick={() => onDraftStateChange?.(true)}>stub-draft-on</button>
      <button onClick={() => onDraftStateChange?.(false)}>stub-draft-off</button>
      {publishedView?.content}
    </div>
  ),
}))
/* The real frame mints a single-use document over the network. Only its REPORT
 * matters to the page, so the stub is that report and nothing else. */
vi.mock('./CrewWebview', () => ({
  default: ({ onLiveFrameChange }: { onLiveFrameChange?: (live: boolean) => void }) => (
    <div data-testid="crew-webview-stub">
      <button onClick={() => onLiveFrameChange?.(true)}>stub-frame-live</button>
      <button onClick={() => onLiveFrameChange?.(false)}>stub-frame-gone</button>
    </div>
  ),
}))
vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))

import { api } from '../../api/client'
import MembersPage from './MembersPage'
import { PREVIEW_DASHBOARD } from '../../utils/previewFlags'

function setWindowWidth(px: number) {
  Object.defineProperty(window, 'innerWidth', { value: px, configurable: true, writable: true })
}

const row = () => ({
  name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
})

/** The panel's SHELL. It is kept mounted while hidden when something is held, so
 *  presence and visibility are different questions and only presence tells us
 *  whether the subtree (and the draft inside it) still exists. */
const shell = () => screen.queryByTestId('member-side-panel')
const closePanel = () => fireEvent.click(screen.getByRole('button', { name: /close panel/i }))
const draftOn = () => fireEvent.click(screen.getByRole('button', { name: 'stub-draft-on' }))
const draftOff = () => fireEvent.click(screen.getByRole('button', { name: 'stub-draft-off' }))
const frameLive = () => fireEvent.click(screen.getByRole('button', { name: 'stub-frame-live' }))
const frameGone = () => fireEvent.click(screen.getByRole('button', { name: 'stub-frame-gone' }))

/** Stands in for an app-shell navigation surface -- the sidebar, the palette, the
 *  identity pill -- which reaches the page only by asking its registered leave guards.
 *  Its own copy rather than an import, as the schedules suite keeps its own. */
function LeaveProbe() {
  const mayLeave = useMayLeaveForNavigation()
  const [answer, setAnswer] = useState('')
  return (
    <button type="button" data-testid="leave-probe" onClick={() => setAnswer(String(mayLeave()))}>
      {answer}
    </button>
  )
}
const askToLeave = () => {
  fireEvent.click(screen.getByTestId('leave-probe'))
  return screen.getByTestId('leave-probe').textContent
}
/** Does the page hold the document open right now? The `beforeunload` listener and the
 *  published navigation stake arm off ONE flag, and this is the half a test can see. */
const holdsDocument = () => !window.dispatchEvent(new Event('beforeunload', { cancelable: true }))

async function openCrewmate(name = 'oncall', alsoRoster: string[] = []) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
    members: [name, ...alsoRoster].map(n => ({ ...row(), name: n, slug: n, slot_key: `member-${n}` })),
    default_agent: 'kirocrew',
  })
  // Echo the requested slug: a fixed answer reports the first crewmate for every one of
  // them, which the page reads as a slug collision and renders instead of the thread.
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
    Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: false }),
  )
  renderWithProviders(
    <NavigationLeaveGuardProvider>
      <MembersPage />
      <LeaveProbe />
    </NavigationLeaveGuardProvider>,
  )
  fireEvent.click(await screen.findByText(name))
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent(`member-${name}`))
  return screen.findByTestId('command-center-stub')
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  // These tests exercise the Dashboard tab, a Feature Preview (default off).
  localStorage.setItem(PREVIEW_DASHBOARD, '1')
  __resetPanelTabs()
  setWindowWidth(1440)
})

describe('MembersPage: the side panel is held by an unsent Dashboard answer', () => {
  it('no draft reported: closing the panel releases it, so its open/close motion runs', async () => {
    await openCrewmate()
    closePanel()
    await waitFor(() => expect(shell()).toBeNull())
  })

  it('a reported draft holds the panel across a tab switch AND a close -- the sequence that lost the answer', async () => {
    await openCrewmate()
    act(() => { draftOn() })
    // Leave the Dashboard, then close: the subtree must still be there.
    fireEvent.click(screen.getByTestId('side-panel-leading-tab-crew-work-log'))
    await screen.findByTestId('member-work-log')
    closePanel()
    await waitFor(() => expect(shell()).not.toBeVisible())
    expect(shell()).toBeInTheDocument()
  })

  it('the answer sent, the hold goes with it', async () => {    await openCrewmate()
    act(() => { draftOn() })
    closePanel()
    await waitFor(() => expect(shell()).not.toBeVisible())
    expect(shell()).toBeInTheDocument()
    // Re-open so the stub is reachable, report the draft gone, close again.
    fireEvent.click(screen.getByTestId('member-panel-toggle'))
    await screen.findByTestId('command-center-stub')
    act(() => { draftOff() })
    closePanel()
    await waitFor(() => expect(shell()).toBeNull())
  })
})

/* The same hold, for the other thing in this body that cannot survive a remount.
 *
 * An expanded published Dashboard holds a minted document whose URL is SINGLE-USE,
 * so a re-created iframe requests a spent one and renders blank. `CrewWebview`
 * keeps the frame mounted across a collapse for that reason, but that rule reaches
 * only as far as the component: whether the PANEL SUBTREE lives is this page's
 * decision, and the page cannot see the flag. So the frame reports it, and the hold
 * covers it on the same terms as an unsent answer.
 *
 * Driven here by the stub's report rather than by a real expand, because the
 * page's half is the whole of what is pinned: given the report, does the subtree
 * survive the exits that would otherwise take it. */
describe('MembersPage: the side panel is held by a live published Dashboard frame', () => {
  it('a live frame with NO draft holds the panel across a close -- the case a draft-only hold drops', async () => {
    await openCrewmate()
    act(() => { frameLive() })
    closePanel()
    await waitFor(() => expect(shell()).not.toBeVisible())
    expect(shell()).toBeInTheDocument()
  })

  it('the frame reported gone, the hold goes with it', async () => {
    await openCrewmate()
    act(() => { frameLive() })
    act(() => { frameGone() })
    closePanel()
    await waitFor(() => expect(shell()).toBeNull())
  })

  it('a live frame survives leaving the Dashboard tab and then closing', async () => {
    await openCrewmate()
    act(() => { frameLive() })
    fireEvent.click(screen.getByTestId('side-panel-leading-tab-crew-work-log'))
    await screen.findByTestId('member-work-log')
    closePanel()
    await waitFor(() => expect(shell()).not.toBeVisible())
    expect(shell()).toBeInTheDocument()
  })
})

/* The exits above are the ones the mount hold ANSWERS: a tab switch and a panel close
 * leave the subtree standing, so the draft is in no danger and nothing is asked. The
 * cases below are the ones it cannot answer, because they destroy the subtree under it --
 * the `CommandCenterPanel` is keyed on the crewmate and the page belongs to a route. Each
 * asks the same question the Schedules create form already asks at that exit; before
 * this, typing an answer and clicking another crewmate in the same roster discarded it in
 * silence, with no persistence and no recovery path. */
describe('MembersPage: exits that destroy the subtree ask about an unsent answer', () => {
  it('asks before a crewmate switch discards the answer, and switches once it does', async () => {
    await openCrewmate('oncall', ['scribe'])
    act(() => { draftOn() })

    // Refused: still on the same crewmate, and the subtree that holds the answer is there.
    fireEvent.click(screen.getByText('scribe'))
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    expect(screen.getByTestId('command-center-stub')).toBeInTheDocument()

    // Confirmed: the switch lands.
    fireEvent.click(screen.getByText('scribe'))
    const ask2 = await screen.findByRole('dialog')
    fireEvent.click(within(ask2).getByRole('button', { name: /Discard/i }))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-scribe'))
  })

  it('asks before the narrow-window Back clears the crewmate over the answer', async () => {
    // Clearing the member param unmounts the panel subtree. This exit replaces rather
    // than pushes, and a replace raises no `popstate`, so neither the published stake nor
    // `NavigationBackGuard` can see it -- the ask has to be here. The button is
    // `md:hidden`, which is CSS: it is in the DOM at any width, so the handler is
    // reachable without narrowing the window and losing the overlay panel's mount.
    await openCrewmate()
    act(() => { draftOn() })

    fireEvent.click(screen.getByTestId('member-back'))
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('command-center-stub')).toBeInTheDocument()
  })

  it('vetoes leaving the route while the answer is unsent, and lets a clean page through', async () => {
    // The leave registry is synchronous, so this exit uses `window.confirm` -- the same
    // path the Schedules draft takes. Every app-shell surface that asks reaches it,
    // including the header identity pill.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await openCrewmate()
      // Clean: allowed out with no prompt at all.
      expect(askToLeave()).toBe('true')
      expect(confirmSpy).not.toHaveBeenCalled()

      act(() => { draftOn() })
      expect(askToLeave()).toBe('false')
      expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/lose the answer/i))
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('holds a reload and the navigation stake while the answer is unsent', async () => {
    // A reload is not a route change, so the guard above never sees it, and
    // `NavigationBackGuard` arms off the published STAKE rather than off the guard. Both
    // come off one flag and the unload is the half a test can observe.
    await openCrewmate()
    expect(holdsDocument()).toBe(false)
    act(() => { draftOn() })
    await waitFor(() => expect(holdsDocument()).toBe(true))
    act(() => { draftOff() })
    await waitFor(() => expect(holdsDocument()).toBe(false))
  })

  it('a tab switch, the chord and a panel close still ask NOTHING -- the hold covers them', async () => {
    // The anti-regression for the obvious shortcut: `schedAtStakeRef` and `schedGuardRef`
    // are one line from answering for the Dashboard draft too, and then every one of
    // these gestures raises a dialog about an answer that the mount hold is keeping
    // alive either way. The chord is the exit that proves it -- it is the tab-level pair's
    // own caller, where a tab switch away from Dashboard consults nothing (the strip's
    // `onBeforeLeave` belongs to the Schedules tab alone).
    const confirmSpy = vi.spyOn(window, 'confirm')
    try {
      await openCrewmate()
      act(() => { draftOn() })
      fireEvent.click(screen.getByTestId('side-panel-leading-tab-crew-work-log'))
      await screen.findByTestId('member-work-log')
      act(() => { window.dispatchEvent(new Event('toggle-activity-panel')) })
      await waitFor(() => expect(shell()).not.toBeVisible())
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(confirmSpy).not.toHaveBeenCalled()
      // Re-open and close from the panel's own control: same answer.
      fireEvent.click(screen.getByTestId('member-panel-toggle'))
      await screen.findByTestId('command-center-stub')
      closePanel()
      await waitFor(() => expect(shell()).not.toBeVisible())
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(confirmSpy).not.toHaveBeenCalled()
      // And the subtree is still standing, which is why none of them had to ask.
      expect(shell()).toBeInTheDocument()
    } finally {
      confirmSpy.mockRestore()
    }
  })
})
