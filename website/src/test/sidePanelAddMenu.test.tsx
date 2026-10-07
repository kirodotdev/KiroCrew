/**
 * The side panel's "+" tab menu runs on the shared shadcn/Radix dropdown.
 *
 * Guards the two things the previous hand-rolled menu owned by hand and that a
 * regression would silently take away: the menu opens from the trigger with its
 * items exposed under the WAI-ARIA menu roles, and selecting an item opens that
 * view as a tab. Escape covers the dismissal path Radix now owns instead of the
 * document-level mousedown listener this replaced.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

// Heavy tab bodies — none of them are what this test drives.
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
// Terminal off / Developer Mode off: the menu then lists exactly the views the
// assertions below name, with no environment-dependent extras.
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

// Disk preflight for a clean file tab's close batch (SidePanel.handleCloseTabs).
// Default: every file is present on disk, so a clean tab needs no confirmation;
// a test overrides this to make a file read 404 (gone) or fail.
const fileReadMock = vi.fn(async (_path: string) => ({ ok: true, status: 200, text: '', binary: false, truncated: false, redacted: false, lossy: false }))
vi.mock('../utils/fileReadQuery', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/fileReadQuery')>()),
  fetchFileRead: (path: string) => fileReadMock(path),
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel, { newMenuSections, NEW_MENU_LABEL_KEY } from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'
import { PREVIEW_DASHBOARD, setPreviewFlag } from '../utils/previewFlags'
import { LONG_PRESS_MS } from '../hooks/useLongPressReorder'

let panelController: ReturnType<typeof usePanelTabs>

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  panelController = tabsCtl
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
}

const openMenu = () => act(() => {
  fireEvent.pointerDown(
    screen.getByRole('button', { name: 'Open side panel tab' }),
    { button: 0, ctrlKey: false, pointerType: 'mouse' },
  )
})

describe('side panel + menu (shadcn dropdown)', () => {
  beforeEach(() => { localStorage.clear() })

  it('opens as an ARIA menu with the view items', () => {
    renderPanel()
    expect(screen.queryByRole('menu')).toBeNull()
    openMenu()
    expect(screen.getByRole('menu')).toBeTruthy()
    for (const label of ['Pins', 'Issues', 'Subagents', 'Workflows', 'Side Chat', 'Browser']) {
      expect(screen.getByRole('menuitem', { name: label })).toBeTruthy()
    }
    // The permanently pinned views are always in the strip already, so they must
    // never be offered here.
    expect(screen.queryByRole('menuitem', { name: 'Files' })).toBeNull()
    // Diagnostics are behind Developer Mode, which this harness has off.
    expect(screen.queryByRole('menuitem', { name: 'Logs' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Context breakdown' })).toBeNull()
  })

  it('opens the picked view as a tab', () => {
    renderPanel()
    openMenu()
    act(() => { fireEvent.click(screen.getByRole('menuitem', { name: 'Workflows' })) })
    expect(screen.getByRole('tab', { name: /Workflows/ })).toBeTruthy()
  })

  it('dismisses on Escape', () => {
    renderPanel()
    openMenu()
    act(() => { fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' }) })
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('renders one separator between the groups and none at the edges', () => {
    renderPanel()
    openMenu()
    const menu = screen.getByRole('menu')
    const kids = Array.from(menu.children)
    const seps = kids.filter(el => el.getAttribute('role') === 'separator')
    // Developer Mode is off in this harness, so the whole diagnostics group is
    // gone and only two groups survive: session output, then Side + Browser
    // (Terminal is disabled too). Two groups, one rule.
    expect(seps).toHaveLength(1)
    expect(kids[0].getAttribute('role')).toBe('menuitem')
    expect(kids[kids.length - 1].getAttribute('role')).toBe('menuitem')
    // Rules separate groups, so no two are adjacent.
    const roles = kids.map(el => el.getAttribute('role'))
    expect(roles.join(' ')).not.toContain('separator separator')
  })
})

describe('side panel Dashboard view behind the Dynamic Dashboard preview', () => {
  beforeEach(() => { localStorage.clear() })

  it('offers no Dashboard entry and withholds a persisted Dashboard tab while the preview is off', () => {
    // A tab the user opened before the flag went off (or that the store still
    // holds) must not stay on the strip: the withdrawal is the same one a host
    // applies, so it covers the bucket, not just the menu.
    const Seeded = () => {
      const tabsCtl = usePanelTabs('slot-a')
      if (!tabsCtl.tabs.some(t => t.kind === 'command-center')) tabsCtl.openView('command-center')
      return <SidePanel tabsCtl={tabsCtl} slot="slot-a" onFileSave={async () => {}} onClose={() => {}} />
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <Provider store={createTestStore()}>
          <Seeded />
        </Provider>
      </QueryClientProvider>,
    )
    expect(screen.queryByRole('tab', { name: /Dashboard/ })).toBeNull()
    expect(screen.queryByTestId('command-center-panel')).toBeNull()
    openMenu()
    expect(screen.queryByRole('menuitem', { name: 'Dashboard' })).toBeNull()
    // Every other session-output row is still there: only this view is gated.
    expect(screen.getByRole('menuitem', { name: 'Pins' })).toBeTruthy()
  })

  it('offers the Dashboard entry, and opens it as a tab, once the preview is on', () => {
    localStorage.setItem(PREVIEW_DASHBOARD, '1')
    renderPanel()
    openMenu()
    act(() => { fireEvent.click(screen.getByRole('menuitem', { name: 'Dashboard' })) })
    expect(screen.getByRole('tab', { name: /Dashboard/ })).toBeTruthy()
  })

  it('brings a withheld Dashboard tab back in the same tick the toggle flips on', () => {
    const Seeded = () => {
      const tabsCtl = usePanelTabs('slot-a')
      if (!tabsCtl.tabs.some(t => t.kind === 'command-center')) tabsCtl.openView('command-center')
      return <SidePanel tabsCtl={tabsCtl} slot="slot-a" onFileSave={async () => {}} onClose={() => {}} />
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <Provider store={createTestStore()}>
          <Seeded />
        </Provider>
      </QueryClientProvider>,
    )
    expect(screen.queryByRole('tab', { name: /Dashboard/ })).toBeNull()
    act(() => { setPreviewFlag(PREVIEW_DASHBOARD, true) })
    expect(screen.getByRole('tab', { name: /Dashboard/ })).toBeTruthy()
  })
})

describe('newMenuSections', () => {
  const kinds = (o: { devMode: boolean; terminalEnabled: boolean; summaryEnabled?: boolean }) =>
    newMenuSections({ summaryEnabled: true, ...o }).map(g => g.items.map(i => i.kind))

  it('partitions every catalogued view exactly once', () => {
    // Both gates open, so nothing is filtered but the auto-pinned views. Any
    // view added to NEW_MENU_LABEL_KEY without being placed in a group — or
    // placed in two — fails here instead of quietly vanishing from the menu.
    const flat = kinds({ devMode: true, terminalEnabled: true }).flat()
    const pinned = ['changes', 'files', 'artifacts']
    const catalogued = Object.keys(NEW_MENU_LABEL_KEY).filter(k => !pinned.includes(k))
    expect([...flat].sort()).toEqual([...catalogued].sort())
    expect(new Set(flat).size).toBe(flat.length)
  })

  it('hides Summary while session summaries are disabled', () => {
    // The feature is opt-in and its settings toggle ships separately, so
    // advertising the row while the flag is false sends every reader to a panel
    // that says it is off and offers no way to change that.
    const flat = kinds({ devMode: true, terminalEnabled: true, summaryEnabled: false }).flat()
    expect(flat).not.toContain('summary')
    // Only that row goes — its group still carries the rest, so the group is not
    // dropped and nothing else is collateral.
    expect(kinds({ devMode: true, terminalEnabled: true, summaryEnabled: false })[0])
      .toEqual(['command-center', 'pins', 'issues', 'links', 'subagents', 'workflows', 'git'])
  })

  it('keeps each group id fixed however the gates fall', () => {
    // The group id is the menu's React key. If it moved when a gate resolved,
    // React would remount the group and detach the row under the user's cursor
    // — `summaryEnabled` in particular starts undefined and flips when its
    // request lands, i.e. potentially mid-click. So the id a group reports must
    // depend only on its declaration, never on which rows survived.
    const idsFor = (o: { devMode: boolean; terminalEnabled: boolean; summaryEnabled: boolean }) =>
      newMenuSections(o).map(g => g.id)

    // Gating a row must not touch its group's id.
    expect(idsFor({ devMode: true, terminalEnabled: true, summaryEnabled: false }))
      .toEqual(idsFor({ devMode: true, terminalEnabled: true, summaryEnabled: true }))

    // Dropping a whole group must not renumber the survivors: with Developer
    // Mode off the diagnostics group disappears, and the two that remain keep
    // the ids they had.
    expect(idsFor({ devMode: true, terminalEnabled: true, summaryEnabled: true }))
      .toEqual(['session-output', 'workspaces', 'diagnostics'])
    expect(idsFor({ devMode: false, terminalEnabled: true, summaryEnabled: true }))
      .toEqual(['session-output', 'workspaces'])

    // And ids stay unique, or two groups would collide on one key.
    for (const devMode of [false, true]) {
      for (const summaryEnabled of [false, true]) {
        const ids = idsFor({ devMode, terminalEnabled: true, summaryEnabled })
        expect(new Set(ids).size).toBe(ids.length)
      }
    }
  })

  it('groups by session output, workspaces, then diagnostics', () => {
    expect(kinds({ devMode: true, terminalEnabled: true })).toEqual([
      ['command-center', 'summary', 'pins', 'issues', 'links', 'subagents', 'workflows', 'git'],
      ['side', 'browser', 'terminal'],
      ['logs', 'context', 'crewlog'],
    ])
  })

  it('drops a group the gates emptied instead of leaving a stray separator', () => {
    // Developer Mode off empties the diagnostics group entirely — the case the
    // empty-group filter exists for. No returned group is ever empty, under any
    // gate combination.
    for (const devMode of [false, true]) {
      for (const terminalEnabled of [false, true]) {
        for (const group of newMenuSections({ devMode, terminalEnabled, summaryEnabled: true })) {
          expect(group.items.length).toBeGreaterThan(0)
        }
      }
    }
    // Both gates closed: diagnostics gone outright, Terminal dropped from
    // Workspaces — two groups, not three with a hole.
    expect(kinds({ devMode: false, terminalEnabled: false })).toEqual([
      ['command-center', 'summary', 'pins', 'issues', 'links', 'subagents', 'workflows', 'git'],
      ['side', 'browser'],
    ])
    // Terminal back, diagnostics still gated.
    expect(kinds({ devMode: false, terminalEnabled: true })).toEqual([
      ['command-center', 'summary', 'pins', 'issues', 'links', 'subagents', 'workflows', 'git'],
      ['side', 'browser', 'terminal'],
    ])
  })
})


describe('workspace tab close menu', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs(); fileReadMock.mockResolvedValue({ ok: true, status: 200, text: '', binary: false, truncated: false, redacted: false, lossy: false }) })
  it('closes tabs to the right and preserves fixed views', async () => {
    renderPanel()
    act(() => {
      panelController.openView('issues')
      panelController.openView('browser')
      panelController.openView('workflows')
    })
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Browser/ }), { clientX: 50, clientY: 50 })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close tabs to the right' }))
    await waitFor(() => expect(screen.queryByRole('tab', { name: /Workflows/ })).toBeNull())
    expect(screen.getByRole('tab', { name: /Browser/ })).toBeTruthy()
    expect(screen.getByRole('tab', { name: /Issues/ })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Files' })).toBeTruthy()
  })

  it('closes other tabs and keeps the requested tab and fixed views', async () => {
    renderPanel()
    act(() => {
      panelController.openView('issues')
      panelController.openView('browser')
    })
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Issues/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close other tabs' }))
    await waitFor(() => expect(screen.queryByRole('tab', { name: /Browser/ })).toBeNull())
    expect(screen.getByRole('tab', { name: /Issues/ })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Files' })).toBeTruthy()
  })

  it('asks before bulk close discards file edits, and cancel retains every tab', async () => {
    renderPanel()
    act(() => {
      panelController.openFile('/tmp/draft.md', 'saved')
      panelController.patchTab('file:/tmp/draft.md', { content: 'unsaved' })
      panelController.openView('browser')
    })
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Browser/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))
    expect(await screen.findByText('Discard unsaved changes?')).toBeTruthy()
    // The dialog names what is lost rather than asking blind.
    expect(within(screen.getByRole('dialog')).getByText('draft.md')).toHaveAttribute('title', '/tmp/draft.md')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('tab', { name: /draft.md/ })).toBeTruthy()
    expect(screen.getByRole('tab', { name: /Browser/ })).toBeTruthy()
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Browser/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByRole('tab', { name: /draft.md/ })).toBeNull())
    expect(screen.queryByRole('tab', { name: /Browser/ })).toBeNull()
    expect(screen.getByRole('tab', { name: 'Files' })).toBeTruthy()
  })

  it('the single × closes a dirty tab directly, without a confirmation', async () => {
    // The frozen Goal says the single × is unchanged: it closes one tab the way
    // it did on main, so even a dirty file goes straight through handleCloseTab.
    // Only the close MENU items run through the batch path that confirms.
    renderPanel()
    act(() => {
      panelController.openFile('/tmp/draft.md', 'saved')
      panelController.patchTab('file:/tmp/draft.md', { content: 'unsaved' })
    })
    fireEvent.click(screen.getByRole('button', { name: 'Close tab' }))
    await waitFor(() => expect(screen.queryByRole('tab', { name: /draft.md/ })).toBeNull())
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('asks before a bulk close discards a clean file whose disk copy is gone', async () => {
    // The disk no longer holds this file, so the clean buffer is its only copy.
    fileReadMock.mockResolvedValue({ ok: false, status: 404, text: '', binary: false, truncated: false, redacted: false, lossy: false })
    renderPanel()
    act(() => {
      panelController.openFile('/tmp/gone.md', 'saved contents')  // clean: content === savedContent
      panelController.openView('browser')
    })
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Browser/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))
    // The batch does not close the clean tab silently — it names it as a last copy.
    expect(await screen.findByText('Discard unsaved changes?')).toBeTruthy()
    expect(within(screen.getByRole('dialog')).getByText('gone.md')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('tab', { name: /gone.md/ })).toBeTruthy()
  })

  it('confirms a partial (truncated) file whose disk copy is gone before closing', async () => {
    // A truncated buffer is still the only surviving prefix once the disk copy
    // is gone, so the preflight must not skip it.
    fileReadMock.mockResolvedValue({ ok: false, status: 404, text: '', binary: false, truncated: false, redacted: false, lossy: false })
    renderPanel()
    act(() => {
      panelController.openFile('/tmp/truncated.log', 'first 10k bytes', null, { partial: true })
      panelController.openView('browser')
    })
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Browser/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))
    expect(await screen.findByText('Discard unsaved changes?')).toBeTruthy()
    expect(within(screen.getByRole('dialog')).getByText('truncated.log')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('tab', { name: /truncated.log/ })).toBeTruthy()
  })

  it('re-evaluates a file edited during the disk preflight instead of closing a stale snapshot', async () => {
    // The preflight reads the disk asynchronously; a buffer the user edits
    // during that round trip must be named as unsaved, not closed on the clean
    // pre-read snapshot.
    let release: (v: { ok: boolean; status: number; text: string; binary: boolean; truncated: boolean; redacted: boolean; lossy: boolean }) => void
    fileReadMock.mockImplementation(() => new Promise(res => { release = res }))
    renderPanel()
    act(() => {
      panelController.openFile('/tmp/edited.md', 'saved contents')  // clean at snapshot time
      panelController.openView('browser')
    })
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Browser/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))
    // While the read is pending, the user edits the file — it is now dirty.
    act(() => { panelController.patchTab('file:/tmp/edited.md', { content: 'edited while closing' }) })
    // Let the (present-on-disk) read resolve; the batch must now see the edit.
    act(() => { release!({ ok: true, status: 200, text: '', binary: false, truncated: false, redacted: false, lossy: false }) })
    expect(await screen.findByText('Discard unsaved changes?')).toBeTruthy()
    expect(within(screen.getByRole('dialog')).getByText('edited.md')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('tab', { name: /edited.md/ })).toBeTruthy()
  })

  it('closes a clean file silently when its disk copy is still present', async () => {
    // Default mock: the file is on disk, so its clean buffer is not a last copy.
    renderPanel()
    act(() => {
      panelController.openFile('/tmp/present.md', 'saved contents')
      panelController.openView('browser')
    })
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Browser/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))
    // No confirmation: nothing unsaved and the file is safe on disk.
    await waitFor(() => expect(screen.queryByRole('tab', { name: /present.md/ })).toBeNull())
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('names the combined action when a bulk close both discards edits and stops shells', async () => {
    renderPanel()
    act(() => {
      panelController.openFile('/tmp/draft.md', 'saved')
      panelController.patchTab('file:/tmp/draft.md', { content: 'unsaved' })
      panelController.openTerminal()
      panelController.openTerminal()
      panelController.openView('browser')
    })
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Browser/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))
    // One dialog states both losses...
    expect(await screen.findByText('Discard unsaved changes?')).toBeTruthy()
    expect(within(screen.getByRole('dialog')).getByText('draft.md')).toBeTruthy()
    expect(within(screen.getByRole('dialog')).getByText('2 terminals will close and their running shells will stop.')).toBeTruthy()
    // ...and the confirm button names BOTH, not just the discard, since clicking
    // it also stops the shells and that cannot be undone.
    expect(screen.getByRole('button', { name: 'Discard and close terminals' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Discard changes' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Discard and close terminals' }))
    await waitFor(() => expect(screen.queryByRole('tab', { name: /draft.md/ })).toBeNull())
    expect(screen.queryByRole('tab', { name: /Terminal/ })).toBeNull()
  })

  it('counts the shells a bulk close would stop before closing them', async () => {
    renderPanel()
    act(() => { panelController.openTerminal() })
    act(() => { panelController.openTerminal() })
    act(() => { panelController.openView('browser') })
    expect(screen.getAllByRole('tab', { name: /Terminal/ })).toHaveLength(2)
    fireEvent.contextMenu(screen.getByRole('tab', { name: /Browser/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))
    expect(await screen.findByText('2 terminals will close and their running shells will stop.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close terminals' }))
    await waitFor(() => expect(screen.queryByRole('tab', { name: /Terminal/ })).toBeNull())
    expect(screen.queryByRole('tab', { name: /Browser/ })).toBeNull()
  })

  // Same hold as the terminal strip: lifting in place opens the menu, and the
  // Radix trigger's own 700ms touch timer stays silent in between.
  it('opens on a touch hold that lifts in place', () => {
    renderPanel()
    act(() => {
      panelController.openView('issues')
      panelController.openView('browser')
    })
    vi.useFakeTimers()
    try {
      fireEvent.pointerDown(screen.getByRole('tab', { name: /Browser/ }), { pointerType: 'touch', button: 0, clientX: 50, clientY: 50 })
      act(() => { vi.advanceTimersByTime(LONG_PRESS_MS + 700) })
      expect(screen.queryByRole('menu')).toBeNull()
      fireEvent.pointerUp(window, { clientX: 50, clientY: 50 })
      expect(screen.getByRole('menuitem', { name: 'Close other tabs' })).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })
})
