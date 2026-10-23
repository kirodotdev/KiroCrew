/**
 * Pin Summary (`dashboard.side_panel_pin_summary`) in the side panel: Summary
 * leads the pinned block, cannot be closed, leaves the + menu, and is the tab a
 * PLAIN show lands on (the panel opened again, a chat switch). A show where
 * something asked for a tab keeps it, the tab already shown included; an
 * automatic open neither stops nor later undoes the landing, and a remount that
 * is not a new show lands nothing. The chat page reports the strip it shows with
 * `notePanelShown`, which ends a show through `notePanelStoppedShowing`; these
 * tests call that directly. Off while session summaries are off and on a
 * host that withholds Summary. Until a chat's summary read answers, the strip
 * keeps the block it has, and the landing waits for a read that says on.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
import { api } from '../api/client'

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
 *  strict "summaries on" read resolves from the cache with no request.
 *  `summariesOn: null` caches nothing, so each read goes to `api.sessionSummary`. */
function renderPanel(props: HarnessProps = {}, summariesOn: boolean | null = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  if (summariesOn !== null) for (const slot of ['slot-a', 'slot-b']) queryClient.setQueryData(['session-summary', slot], { enabled: summariesOn })
  const store = createTestStore()
  const view = (p: HarnessProps) => (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}><Harness {...p} /></Provider>
    </QueryClientProvider>
  )
  const r = render(view(props))
  return { rerender: (p: HarnessProps) => r.rerender(view({ ...props, ...p })), unmount: r.unmount }
}

/** Holds every summary read in flight until the test answers it, as for a chat
 *  whose read is not cached (garbage-collected, or a reload: the strip is
 *  kept, the query is not). */
function holdSummaryRead(): (enabled: boolean) => void {
  let answer!: (enabled: boolean) => void
  const read = new Promise<{ enabled: boolean }>(resolve => { answer = enabled => resolve({ enabled }) })
  const spy = vi.spyOn(api, 'sessionSummary').mockReturnValue(read as never)
  onTestFinished(() => { spy.mockRestore() })
  return answer
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

  it('keeps the next landing available after closed-panel Git discovery', () => {
    const { rerender } = renderPanel()
    closeAndReopen(rerender, () => { ctl.openView('git', { automatic: true }) })
    expect(ctl.tabs.map(tab => tab.id)).toContain('git')
    expect(ctl.activeId).toBe('summary')
  })

  it.each([
    ['Git', 'git'],
    ['Changes', 'changes'],
  ] as const)('keeps Summary focused when an automatic %s open follows its landing', (label, kind) => {
    renderPanel()
    expect(activeChip()?.getAttribute('aria-label')).toBe('Summary')
    act(() => { ctl.openView(kind, { automatic: true }) })
    expect(chipNames()).toContain(label)
    expect(ctl.activeId).toBe('summary')
  })

  it('keeps the Summary landing through a same-show remount before discovery', () => {
    const { rerender } = renderPanel()
    rerender({ panelHidden: true })
    rerender({ panelHidden: false })
    act(() => { ctl.openView('git', { automatic: true }) })
    expect(ctl.activeId).toBe('summary')
  })

  it('lets automatic Git focus after another tab was chosen', () => {
    renderPanel()
    act(() => { ctl.setActive('files') })
    act(() => { ctl.openView('git', { automatic: true }) })
    expect(ctl.activeId).toBe('git')
  })

  it('lets automatic Git focus after Summary was explicitly chosen', () => {
    renderPanel()
    act(() => { ctl.setActive('summary') })
    act(() => { ctl.openView('git', { automatic: true }) })
    expect(ctl.activeId).toBe('git')
  })

  it('lets automatic Git focus when this show had no Summary landing', () => {
    const { rerender } = renderPanel()
    closeAndReopen(rerender, () => { ctl.setActive('summary') })
    expect(ctl.activeId).toBe('summary')
    act(() => { ctl.openView('git', { automatic: true }) })
    expect(ctl.activeId).toBe('git')
  })

  it('keeps automatic Git focus behavior when Pin Summary is off', () => {
    renderPanel({ pinSummary: false })
    act(() => { ctl.openView('git', { automatic: true }) })
    expect(ctl.activeId).toBe('git')
  })

  it('stops protecting Summary focus when Pin Summary is turned off', () => {
    const { rerender } = renderPanel()
    expect(activeChip()?.getAttribute('aria-label')).toBe('Summary')
    rerender({ pinSummary: false })
    act(() => { ctl.openView('git', { automatic: true }) })
    expect(ctl.activeId).toBe('git')
  })

  it('lets an explicit Git open replace the Summary landing focus', () => {
    renderPanel()
    expect(activeChip()?.getAttribute('aria-label')).toBe('Summary')
    act(() => { ctl.openView('git') })
    expect(ctl.activeId).toBe('git')
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

  describe('while the chat\'s summary read is in flight', () => {
    /** A strip a known-on read pinned Summary in, shown again with no cached
     *  read. Returns the read's answer. */
    function pinnedStripWithReadInFlight(beforeRemount?: () => void) {
      renderPanel().unmount()
      if (beforeRemount) act(beforeRemount)
      const answer = holdSummaryRead()
      renderPanel({}, null)
      return answer
    }

    it('keeps Summary first and not closable, and writes no reordered strip', () => {
      pinnedStripWithReadInFlight()
      expect(chipNames().slice(0, 4)).toEqual(['Summary', 'Changes', 'Artifacts', 'Files'])
      expect(within(chip('Summary')).queryByRole('button', { name: 'Close tab' })).toBeNull()
      expect(ctl.tabs.map(t => t.id).slice(0, 4)).toEqual(['summary', 'changes', 'artifacts', 'files'])
    })

    it('unpins Summary when the read answers off', async () => {
      const answer = pinnedStripWithReadInFlight()
      await act(async () => { answer(false) })
      const summary = () => screen.getAllByRole('tab').find(el => el.textContent?.includes('Summary'))
      await waitFor(() => { expect(within(summary()!).getByRole('button', { name: 'Close tab' })).toBeTruthy() })
      expect(chipNames().slice(0, 3)).toEqual(['Changes', 'Artifacts', 'Files'])
    })

    it('lands on Summary only once the read answers on, and keeps the strip as it is', async () => {
      const answer = pinnedStripWithReadInFlight(() => {
        // Files is focused, then the panel stops showing the strip, so the
        // next show is a plain one.
        ctl.setActive('files')
        notePanelStoppedShowing('slot-a')
      })
      expect(activeChip()?.getAttribute('aria-label')).toBe('Files')
      await act(async () => { answer(true) })
      // The landing is the sign the answer reached the panel.
      await waitFor(() => { expect(activeChip()?.getAttribute('aria-label')).toBe('Summary') })
      expect(chipNames().slice(0, 4)).toEqual(['Summary', 'Changes', 'Artifacts', 'Files'])
      expect(within(chip('Summary')).queryByRole('button', { name: 'Close tab' })).toBeNull()
    })
  })
})
