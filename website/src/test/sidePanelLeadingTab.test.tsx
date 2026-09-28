/**
 * The host-owned LEADING tabs (`SidePanel.leadingTabs` + `usePanelTabs`'s
 * `leadingIds`): the Crewmates page's Notes / Work log / Dashboard.
 *
 * Contracts, each of which a plausible refactor breaks silently:
 *
 * 1. Placement and shape — they render AHEAD of the permanent pinned block, in
 *    the order given, as always-labelled chips with no close control, and none
 *    is a Reorder item.
 * 2. Focus — a fresh strip opens on the FIRST one (not on the first pinned view,
 *    which is what `syncPinned` would otherwise pick), a stored focus on any of
 *    them survives, a stored focus on an id the host no longer offers falls back
 *    to the first, and closing the last dynamic tab lands back on the first
 *    rather than on `null`.
 * 3. The panel's close control follows `onClose`: absent means permanent (no
 *    button), present means the button renders — the Crewmates page relies on
 *    the former for its docked column and the latter for its overlay.
 * 4. `hiddenViews` withdraws a view from BOTH the pinned block and the + menu —
 *    a host that cannot feed a view must not ship it empty.
 *
 * Bodies are stubbed as in `sidePanelPinnedAlwaysPresent.test.tsx`; only the
 * strip and the leading bodies are driven. Most cases pass ONE leading tab (a
 * host with a single one still works); the three-tab cases are grouped at the
 * end.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => <div data-testid="web-preview-body" /> }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
// One installed app contributes a panel tab, so the `'app'` withdrawal below
// has something to withhold. Only the descriptor hook is mocked; the pure
// helpers stay real (same shape as sidePanelAppTab.test.tsx).
vi.mock('../hooks/panelTabRegistry', async (orig) => {
  const actual = await orig<typeof import('../hooks/panelTabRegistry')>()
  return {
    ...actual,
    usePanelTabDescriptors: () => [{
      kind: 'app:pippin:browser' as const, appName: 'pippin', tabId: 'browser',
      title: 'Pippin', menuLabel: 'Open Pippin', menuDescription: 'Browse docs', icon: 'BookOpen', entry: 'panel.mjs',
    }],
  }
})

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { PINNED_VIEWS, usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'
import type { SidePanelLeadingTab, SidePanelWithholdable } from '../pages/chat/SidePanel'

const LEADING_ID = 'crew-notes'
/** Module constants, as the Crewmates page passes them: the id list is a
 *  dependency of the strip callbacks. */
const ONE_LEADING: readonly string[] = [LEADING_ID]
const THREE_IDS = ['crew-notes', 'crew-work-log', 'crew-dashboard'] as const
const THREE_LEADING: readonly string[] = THREE_IDS
const THREE_TITLES = ['Notes', 'Work log', 'Dashboard'] as const

/** Exposes the strip model so a case can act on it (open a view, close a tab)
 *  the way a host would, without reaching through the DOM for everything. */
let ctl: ReturnType<typeof usePanelTabs> | null = null
/** What the strip reported it SHOWS (the `onActiveTabChange` contract). */
let shown: string | null | undefined

interface HarnessProps {
  closable: boolean
  slot?: string
  hidden?: ReadonlySet<SidePanelWithholdable>
  /** Which leading ids the host offers this render (default: one). */
  leading?: readonly string[]
}

function leadingTabsFor(ids: readonly string[]): SidePanelLeadingTab[] {
  return ids.map(id => {
    const title = THREE_TITLES[THREE_IDS.indexOf(id as typeof THREE_IDS[number])]
    return {
      id,
      title,
      icon: <span data-testid={`leading-icon-${id}`} />,
      render: () => <div data-testid={`leading-body-${id}`}>{`radar ${title.toLowerCase()}`}</div>,
    }
  })
}

function Harness({ closable, slot = 'member-radar', hidden, leading = ONE_LEADING }: HarnessProps) {
  const tabsCtl = usePanelTabs(slot, undefined, { leadingIds: leading })
  ctl = tabsCtl
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot={slot}
      onFileSave={async () => {}}
      onClose={closable ? () => {} : undefined}
      canDockBottom={false}
      hiddenViews={hidden}
      onActiveTabChange={(id) => { shown = id }}
      leadingTabs={leadingTabsFor(leading)}
    />
  )
}

/** The single leading chip / body of the one-tab harness. */
const leadingChip = () => screen.getByTestId(`side-panel-leading-tab-${LEADING_ID}`)
const leadingBody = () => screen.getByTestId(`leading-body-${LEADING_ID}`)
const queryLeadingBody = () => screen.queryByTestId(`leading-body-${LEADING_ID}`)

function renderPanel(props: HarnessProps) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness {...props} />
      </Provider>
    </QueryClientProvider>,
  )
}

const chips = () => screen.getAllByRole('tab')
const nameOf = (el: HTMLElement) => el.getAttribute('aria-label') ?? el.textContent

describe('SidePanel leading tab', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs(); ctl = null; shown = undefined })

  it('renders first, ahead of the pinned block, labelled, with the host icon and no close control', () => {
    renderPanel({ closable: false })
    const rendered = chips()
    // Leading + the three pinned views, and nothing else on a fresh strip.
    expect(rendered).toHaveLength(1 + PINNED_VIEWS.length)
    expect(rendered[0]).toBe(leadingChip())
    // Labelled even while inactive: the label is visible text, not an aria-label.
    expect(rendered[0]).toHaveTextContent('Notes')
    expect(screen.getByTestId(`leading-icon-${LEADING_ID}`)).toBeInTheDocument()
    expect(rendered[0].querySelectorAll('button')).toHaveLength(0)
    expect(screen.getByTestId('side-panel-leading-tabs').contains(rendered[0])).toBe(true)
    // Not a Reorder item: the draggable list holds only the dynamic tabs.
    expect(screen.getByRole('tablist').contains(rendered[0])).toBe(false)
  })

  it('is the strip\'s default focus on a fresh bucket — its body shows, not the first pinned view', () => {
    renderPanel({ closable: false })
    expect(leadingChip()).toHaveAttribute('aria-selected', 'true')
    expect(leadingBody()).toHaveTextContent('radar notes')
    expect(screen.getByTestId('side-panel-leading-body')).toHaveAttribute('data-leading-id', LEADING_ID)
    expect(ctl?.activeId).toBe(LEADING_ID)
  })

  it('stays out of the + menu (permanent, not a view)', () => {
    renderPanel({ closable: false })
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    const menu = screen.getByRole('menu')
    expect(menu.querySelector('[role="menuitem"]')).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /^notes$/i })).toBeNull()
    // The per-chat Terminal IS offered — the whole menu model carries over.
    expect(screen.getByRole('menuitem', { name: 'Terminal' })).toBeTruthy()
  })

  it('closing the last dynamic tab returns focus to the leading tab, not to null', () => {
    renderPanel({ closable: false })
    act(() => { ctl!.openView('workflows') })
    expect(screen.getByRole('tab', { name: /Workflows/ })).toHaveAttribute('aria-selected', 'true')
    expect(queryLeadingBody()).toBeNull()
    act(() => { ctl!.closeTab('workflows') })
    // `closeTab` refocuses a neighbour first (the pinned Files view sits to the
    // left), so the fallback-to-leading arm is reached by emptying the bucket.
    // A pinned view remains, so this is the neighbour case: Files is focused.
    expect(ctl?.activeId).toBe('files')
    // Empty the bucket entirely and the leading tab is the resting focus.
    act(() => { ctl!.closeAll() })
    expect(ctl?.activeId).toBe(LEADING_ID)
  })

  it('renders no close control without onClose, and one with it', () => {
    const { unmount } = renderPanel({ closable: false })
    expect(screen.queryByRole('button', { name: 'Close panel' })).toBeNull()
    unmount()
    __resetPanelTabs()
    renderPanel({ closable: true })
    expect(screen.getByRole('button', { name: 'Close panel' })).toBeInTheDocument()
  })

  it('is per slot: a second slot\'s strip opens on its own leading tab while the first keeps its focus', () => {
    const first = renderPanel({ closable: false, slot: 'member-radar' })
    act(() => { ctl!.openView('workflows') })
    expect(ctl?.activeId).toBe('workflows')
    first.unmount()
    const second = renderPanel({ closable: false, slot: 'member-scout' })
    expect(ctl?.activeId).toBe(LEADING_ID)
    second.unmount()
    renderPanel({ closable: false, slot: 'member-radar' })
    expect(ctl?.activeId).toBe('workflows')
  })

  it('hiddenViews withdraws a view from the pinned block and the + menu alike', () => {
    renderPanel({ closable: false, hidden: new Set<SidePanelWithholdable>(['changes', 'pins', 'issues']) })
    // Pinned block: Changes is gone; Artifacts and Files remain, after the leading chip.
    expect(chips().map(nameOf)).toEqual(['Notes', 'Artifacts', 'Files'])
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    screen.getByRole('menu')
    expect(screen.queryByRole('menuitem', { name: 'Pins' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Issues' })).toBeNull()
    // Unrelated rows are untouched.
    expect(screen.getByRole('menuitem', { name: 'Links' })).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: 'Terminal' })).toBeTruthy()
    // App-contributed rows are NOT part of this withdrawal.
    expect(screen.getByRole('menuitem', { name: 'Open Pippin' })).toBeTruthy()
  })

  it("'terminal' and 'app' withdraw the per-chat Terminal and every app-contributed row", () => {
    renderPanel({ closable: false, hidden: new Set<SidePanelWithholdable>(['terminal', 'app']) })
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    screen.getByRole('menu')
    expect(screen.queryByRole('menuitem', { name: 'Terminal' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Open Pippin' })).toBeNull()
    // Chat views are untouched by those two switches.
    expect(screen.getByRole('menuitem', { name: 'Workflows' })).toBeTruthy()
  })

  it('a withheld kind ALREADY in the bucket is neither rendered nor focused, but stays stored', () => {
    // The chat page opened Pins and Workflows on this slot and left Pins focused;
    // a host that withholds Pins must not show it — chip or body — and the
    // stored focus on it falls back to the leading tab. Storage keeps the tab,
    // so the chat page finds it again.
    const first = renderPanel({ closable: false })
    act(() => { ctl!.openView('workflows'); ctl!.openView('pins') })
    expect(ctl?.activeId).toBe('pins')
    first.unmount()
    renderPanel({ closable: false, hidden: new Set<SidePanelWithholdable>(['pins']) })
    expect(screen.queryByRole('tab', { name: /Pins/ })).toBeNull()
    expect(screen.getByRole('tab', { name: /Workflows/ })).toBeInTheDocument()
    expect(leadingChip()).toHaveAttribute('aria-selected', 'true')
    expect(leadingBody()).toBeInTheDocument()
    // Stored, not deleted: the bucket still holds the Pins tab AND its focus —
    // a withdrawal can be temporary, so the store is never rewritten for it.
    expect(ctl?.tabs.some(t => t.id === 'pins')).toBe(true)
    expect(ctl?.activeId).toBe('pins')
    // The host learns what is actually shown through the callback instead.
    expect(shown).toBe(LEADING_ID)
  })

  it('a document tab follows its parent view: withholding Files withdraws a persisted file editor, Artifacts an artifact preview', () => {
    // A file opened on the chat page (kind 'file', not a ViewKind) is left
    // focused; the Members page then withholds every slot-bound view because
    // the slot is unconfirmed. A document tab that ignored the withdrawal would
    // stay on the strip AND stay active — a stale editor over a slot the
    // endpoint may refuse, in place of the leading tab the contract promises.
    const first = renderPanel({ closable: false })
    act(() => {
      ctl!.openArtifact({ slug: 'plan', kind: 'markdown' }, '# plan', 'member-radar')
      ctl!.openFile('/srv/notes.md', 'notes', 'member-radar')
    })
    expect(ctl?.activeId).toBe('file:/srv/notes.md')
    first.unmount()
    renderPanel({ closable: false, hidden: new Set<SidePanelWithholdable>(['files', 'artifacts']) })
    expect(screen.queryByRole('tab', { name: /notes\.md/ })).toBeNull()
    expect(screen.queryByRole('tab', { name: /plan/ })).toBeNull()
    expect(leadingChip()).toHaveAttribute('aria-selected', 'true')
    expect(leadingBody()).toBeInTheDocument()
    expect(shown).toBe(LEADING_ID)
    // Stored, not deleted — same contract as any withheld view.
    expect(ctl?.tabs.some(t => t.id === 'file:/srv/notes.md')).toBe(true)
    // Withholding Files alone leaves the artifact preview in place: the mapping
    // is per parent view, not a blanket "no documents".
    cleanup()
    renderPanel({ closable: false, hidden: new Set<SidePanelWithholdable>(['files']) })
    expect(screen.queryByRole('tab', { name: /notes\.md/ })).toBeNull()
    expect(screen.getByRole('tab', { name: /plan/ })).toBeInTheDocument()
  })

  it('a focus on the leading tab survives persistence: reload restores it, not the last stored tab', async () => {
    renderPanel({ closable: false })
    act(() => { ctl!.openView('workflows') })
    act(() => { ctl!.setActive(LEADING_ID) })
    expect(ctl?.activeId).toBe(LEADING_ID)
    // The bucket persists debounced (300ms); the serialised focus must still be
    // the leading id — it names no stored tab, but it is not a DROPPED tab.
    await new Promise(r => setTimeout(r, 400))
    const raw = localStorage.getItem('mc-panel-tabs:member-radar')
    expect(raw).toBeTruthy()
    expect(JSON.parse(raw as string).activeId).toBe(LEADING_ID)
  })

  it('a withheld body-owning tab (Browser) stays MOUNTED and hidden, not unmounted', () => {
    const first = renderPanel({ closable: false })
    act(() => { ctl!.openView('browser') })
    expect(screen.getByTestId('web-preview-body')).toBeInTheDocument()
    first.unmount()
    // Same slot, browser withheld: no chip, body still in the tree (hidden).
    renderPanel({ closable: false, hidden: new Set<SidePanelWithholdable>(['browser']) })
    expect(screen.queryByRole('tab', { name: /Browser/ })).toBeNull()
    expect(screen.getByTestId('web-preview-body')).toBeInTheDocument()
    expect(leadingBody()).toBeInTheDocument()
  })
})

describe('SidePanel leading tabs — three host tabs', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs(); ctl = null; shown = undefined })

  const chipFor = (id: string) => screen.getByTestId(`side-panel-leading-tab-${id}`)

  it('renders all three ahead of the pinned block, in order, every one labelled', () => {
    renderPanel({ closable: false, leading: THREE_LEADING })
    const rendered = chips()
    expect(rendered).toHaveLength(3 + PINNED_VIEWS.length)
    expect(rendered.slice(0, 3)).toEqual(THREE_IDS.map(chipFor))
    // Labels are visible text on inactive chips too — never icon-only.
    expect(rendered.slice(0, 3).map(c => c.textContent)).toEqual([...THREE_TITLES])
    for (const c of rendered.slice(0, 3)) {
      expect(c.querySelectorAll('button')).toHaveLength(0)
      expect(screen.getByRole('tablist').contains(c)).toBe(false)
    }
    // One group, and no second tablist nested in the strip.
    expect(screen.getAllByRole('tablist')).toHaveLength(1)
    expect(screen.getByTestId('side-panel-leading-tabs').querySelectorAll('[role="tab"]')).toHaveLength(3)
  })

  it('a fresh bucket focuses the FIRST leading tab and mounts only its body', () => {
    renderPanel({ closable: false, leading: THREE_LEADING })
    expect(chipFor('crew-notes')).toHaveAttribute('aria-selected', 'true')
    expect(chipFor('crew-work-log')).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByTestId('leading-body-crew-notes')).toBeInTheDocument()
    expect(screen.queryByTestId('leading-body-crew-work-log')).toBeNull()
    expect(screen.queryByTestId('leading-body-crew-dashboard')).toBeNull()
    expect(ctl?.activeId).toBe('crew-notes')
    expect(shown).toBe('crew-notes')
  })

  it('selecting a chip swaps the body; a stored focus on the third survives a remount', () => {
    const first = renderPanel({ closable: false, leading: THREE_LEADING })
    fireEvent.click(chipFor('crew-dashboard'))
    expect(chipFor('crew-dashboard')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('leading-body-crew-dashboard')).toHaveTextContent('radar dashboard')
    expect(screen.queryByTestId('leading-body-crew-notes')).toBeNull()
    expect(screen.getByTestId('side-panel-leading-body')).toHaveAttribute('data-leading-id', 'crew-dashboard')
    expect(ctl?.activeId).toBe('crew-dashboard')
    first.unmount()
    renderPanel({ closable: false, leading: THREE_LEADING })
    expect(chipFor('crew-dashboard')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('leading-body-crew-dashboard')).toBeInTheDocument()
    expect(shown).toBe('crew-dashboard')
  })

  it('a stored focus on an id the host no longer offers falls back to the first leading tab', () => {
    const first = renderPanel({ closable: false, leading: THREE_LEADING })
    fireEvent.click(chipFor('crew-dashboard'))
    expect(ctl?.activeId).toBe('crew-dashboard')
    first.unmount()
    // Same slot, but the host now offers only Notes: the stored id names
    // nothing on the strip, so the strip SHOWS the first leading tab. The
    // stored focus itself is left alone (a withdrawal can be temporary).
    renderPanel({ closable: false, leading: ONE_LEADING })
    expect(leadingChip()).toHaveAttribute('aria-selected', 'true')
    expect(leadingBody()).toBeInTheDocument()
    expect(shown).toBe(LEADING_ID)
    expect(screen.queryByTestId('side-panel-leading-tab-crew-dashboard')).toBeNull()
  })

  it('closing the last dynamic tab lands on the first leading tab, whichever was focused before', () => {
    renderPanel({ closable: false, leading: THREE_LEADING })
    fireEvent.click(chipFor('crew-work-log'))
    act(() => { ctl!.openView('workflows') })
    expect(ctl?.activeId).toBe('workflows')
    act(() => { ctl!.closeAll() })
    expect(ctl?.activeId).toBe('crew-notes')
    expect(chipFor('crew-notes')).toHaveAttribute('aria-selected', 'true')
  })
})
