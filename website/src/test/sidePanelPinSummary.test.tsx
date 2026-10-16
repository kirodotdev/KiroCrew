/**
 * Pin Summary (`dashboard.side_panel_pin_summary`) in the side panel: Summary
 * leads the pinned block, cannot be closed, leaves the + menu, and is the tab a
 * PLAIN show lands on (the panel opened again, a chat switch). A show where
 * something asked for a tab keeps it, the tab already shown included; an
 * automatic open does not stop the landing, and a remount that is not a new show
 * lands nothing. The chat page marks a show's end with `notePanelStoppedShowing`,
 * which these tests call directly. Off while session summaries are off and on a
 * host that withholds Summary.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

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
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel, { type SidePanelWithholdable } from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs, notePanelStoppedShowing } from '../hooks/usePanelTabs'

type HarnessProps = { slot?: string; pinSummary?: boolean; panelHidden?: boolean; hiddenViews?: ReadonlySet<SidePanelWithholdable> }
let ctl: ReturnType<typeof usePanelTabs>

function Harness({ slot = 'slot-a', pinSummary = true, panelHidden = false, hiddenViews }: HarnessProps) {
  const tabsCtl = usePanelTabs(slot)
  ctl = tabsCtl
  return (
    <SidePanel
      tabsCtl={tabsCtl} slot={slot} pinSummary={pinSummary} panelHidden={panelHidden} hiddenViews={hiddenViews}
      onFileSave={async () => {}} onClose={() => {}}
    />
  )
}

/** Renders with session summaries answered for both test slots, so the
 *  strict "summaries on" read resolves from the cache with no request. */
function renderPanel(props: HarnessProps = {}, summariesOn = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  for (const slot of ['slot-a', 'slot-b']) queryClient.setQueryData(['session-summary', slot], { enabled: summariesOn })
  const store = createTestStore()
  const view = (p: HarnessProps) => (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}><Harness {...p} /></Provider>
    </QueryClientProvider>
  )
  const r = render(view(props))
  return { rerender: (p: HarnessProps) => r.rerender(view({ ...props, ...p })) }
}

const chipNames = () => screen.getAllByRole('tab').map(el => el.getAttribute('aria-label') ?? el.textContent)
const activeChip = () => screen.getAllByRole('tab').find(el => el.getAttribute('aria-selected') === 'true')
const chip = (name: string) => screen.getByRole('tab', { name })
/** Close the panel the way the chat page does (it hides, and the chat page
 *  notes the stop), run `whileClosed`, then open it again. */
function closeAndReopen(rerender: (p: HarnessProps) => void, whileClosed?: () => void) {
  rerender({ panelHidden: true })
  act(() => { notePanelStoppedShowing('slot-a') })
  if (whileClosed) act(whileClosed)
  rerender({ panelHidden: false })
}

beforeEach(() => { localStorage.clear(); __resetPanelTabs() })

describe('Pin Summary', () => {
  it('pins Summary first, ahead of Changes, Artifacts and Files, with no close control', () => {
    renderPanel()
    expect(chipNames().slice(0, 4)).toEqual(['Summary', 'Changes', 'Artifacts', 'Files'])
    expect(within(chip('Summary')).queryByRole('button', { name: 'Close tab' })).toBeNull()
    // The strip opts into the four-chip narrow rule (pinnedViews.css).
    expect(document.querySelector('.side-panel-strip-summary-pinned')).not.toBeNull()
  })

  it('no longer offers Summary in the + menu', () => {
    renderPanel()
    act(() => {
      fireEvent.pointerDown(screen.getByRole('button', { name: 'Open side panel tab' }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    })
    expect(screen.getByRole('menu')).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: 'Summary' })).toBeNull()
  })

  it('lands on Summary each time the panel is shown again with nothing else chosen', () => {
    const { rerender } = renderPanel()
    expect(activeChip()?.getAttribute('aria-label')).toBe('Summary')
    act(() => { fireEvent.click(chip('Files')) })
    expect(activeChip()?.getAttribute('aria-label')).toBe('Files')
    closeAndReopen(rerender)
    expect(activeChip()?.getAttribute('aria-label')).toBe('Summary')
  })

  it('keeps the tab an open brought with it', () => {
    const { rerender } = renderPanel()
    closeAndReopen(rerender, () => {
      // A sub-agent card, a file chip, an app render: something asked for a
      // tab while the panel was closed.
      ctl.openView('workflows')
    })
    expect(activeChip()?.textContent).toContain('Workflows')
  })

  it('keeps the tab when the open asks for the tab already shown', () => {
    const { rerender } = renderPanel()
    act(() => { fireEvent.click(chip('Files')) })
    // A file chip for the file the closed panel already shows: the focus does
    // not move, but the ask still decides the tab.
    closeAndReopen(rerender, () => { ctl.setActive('files') })
    expect(activeChip()?.getAttribute('aria-label')).toBe('Files')
  })

  it('still lands on Summary after an automatic open', () => {
    const { rerender } = renderPanel()
    // A Git or new-link tab created while the panel was closed.
    closeAndReopen(rerender, () => { ctl.openView('issues', { automatic: true }) })
    expect(activeChip()?.getAttribute('aria-label')).toBe('Summary')
  })

  it('keeps the tab through a remount that is not a new show', () => {
    const { rerender } = renderPanel()
    act(() => { fireEvent.click(chip('Files')) })
    // The find pane opens and closes: the panel hides and returns, but the chat
    // page notes no stop, because the panel never closed.
    rerender({ panelHidden: true })
    rerender({ panelHidden: false })
    expect(activeChip()?.getAttribute('aria-label')).toBe('Files')
  })

  it('lands on Summary when switching chats, in a new chat and back in a visited one', () => {
    const { rerender } = renderPanel()
    act(() => { fireEvent.click(chip('Files')) })
    act(() => { notePanelStoppedShowing('slot-a') })
    rerender({ slot: 'slot-b' })
    expect(activeChip()?.getAttribute('aria-label')).toBe('Summary')
    act(() => { notePanelStoppedShowing('slot-b') })
    rerender({ slot: 'slot-a' })
    expect(activeChip()?.getAttribute('aria-label')).toBe('Summary')
  })

  it('pins nothing while session summaries are off', () => {
    renderPanel({}, false)
    expect(chipNames()).not.toContain('Summary')
  })

  it('pins nothing on a host that withholds Summary', () => {
    renderPanel({ hiddenViews: new Set<SidePanelWithholdable>(['summary']) })
    expect(chipNames()).not.toContain('Summary')
  })

  it('leaves Summary as an ordinary, closable tab when turned off', () => {
    const { rerender } = renderPanel()
    rerender({ pinSummary: false })
    expect(chipNames().slice(0, 3)).toEqual(['Changes', 'Artifacts', 'Files'])
    const summary = screen.getAllByRole('tab').find(el => el.textContent?.includes('Summary'))
    expect(summary).toBeTruthy()
    expect(within(summary!).getByRole('button', { name: 'Close tab' })).toBeTruthy()
  })

  it('changes nothing while off', () => {
    renderPanel({ pinSummary: false })
    expect(chipNames()).toEqual(['Changes', 'Artifacts', 'Files'])
    expect(activeChip()?.getAttribute('aria-label')).toBe('Changes')
    expect(document.querySelector('.side-panel-strip-summary-pinned')).toBeNull()
  })
})
