/**
 * The composer dock view (pages/chat/page/ComposerDockView.tsx), rendered
 * directly with its children stubbed and their props captured: the actions the
 * dock wires into the composer, the knowledge picker, the follow-up card, the
 * agent and model pop-ups, the project picker's AWS footer and an app's
 * session control.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, screen, fireEvent, waitFor } from '@testing-library/react'
import { forwardRef, type ReactNode } from 'react'

type Captured = Record<string, (...args: never[]) => unknown> & Record<string, unknown>
const seen = vi.hoisted(() => ({} as Record<string, Captured | null>))

vi.mock('../components/AgentDropdownList', () => ({
  default: () => null,
  isCrewmateChat: () => false,
  DefaultAgentRow: () => <button type="button" data-testid="default-agent-footer">default agent</button>,
  defaultWriteKind: () => 'agent',
  ManageAgentsFooter: (p: Captured) => { seen.manageAgents = p; return <button type="button" data-testid="manage-agents-footer">manage agents</button> },
}))
vi.mock('../components/ProjectPicker', () => ({ default: (p: Captured & { footer?: ReactNode }) => { seen.projectPicker = p; return <>{p.footer}</> } }))
vi.mock('../components/ProjectAwsProfile', () => ({ ProjectAwsProfileSection: (p: Captured) => { seen.awsProfile = p; return null } }))
// The model picker's props are captured; a case that sets `realModelPicker.on` also
// draws the real picker, to see which session switches the dock's props give it.
const realModelPicker = vi.hoisted(() => ({ on: false }))
vi.mock('../components/ModelEffortDropdown', async (orig) => {
  const { default: RealModelEffortDropdown } = await orig<typeof import('../components/ModelEffortDropdown')>()
  return {
    default: (p: Captured) => {
      seen.modelDropdown = p
      return realModelPicker.on ? <RealModelEffortDropdown {...(p as unknown as Parameters<typeof RealModelEffortDropdown>[0])} /> : null
    },
  }
})
vi.mock('../components/ChatInput', () => ({ default: (p: Captured) => { seen.chatInput = p; return null } }))
vi.mock('../components/SessionControlHost', () => ({ default: (p: Captured) => { seen.sessionControl = p; return null } }))
vi.mock('../components/ErrorNotice', () => ({ default: () => null }))
vi.mock('../components/KiroSignInPrompt', () => ({ default: () => null }))
vi.mock('../components/FollowUpCard', () => ({ default: (p: Captured) => { seen.followUp = p; return null } }))
vi.mock('../components/PendingQuestionCard', () => ({ default: () => null }))
vi.mock('../components/FlyingQuote', () => ({ default: () => null }))
vi.mock('../components/QueueStack', () => ({ default: () => null, SubagentDeliveryProgress: () => null }))
vi.mock('../components/MemoryModeChip', () => ({ MemoryModeChip: (p: Captured) => { seen.memoryChip = p; return null } }))
// The dock is driven by props here; the Crew Mode root read is a store/query hook.
vi.mock('../components/crew/useCrewRoot', () => ({ useCrewRoot: () => ({ rootClosed: false, rootTitle: '', workers: undefined, failed: false }) }))
vi.mock('../chat-core/composer/Composer', () => ({ useComposerDraftText: () => null, Composer: forwardRef(({ children }: { children: ReactNode }, _ref) => <>{children}</>) }))
vi.mock('../composerActions/useComposerActionItems', () => ({ useComposerActionItems: () => [] }))
vi.mock('./chat/McpLoadingBar', () => ({ default: () => null }))
vi.mock('./chat/SubagentProgressBar', () => ({ default: () => null }))
vi.mock('./chat/TaskProgressBar', () => ({ default: () => null }))
vi.mock('./chat/WorkflowProgressBar', () => ({ default: () => null }))
vi.mock('./chat/KnowledgePicker', () => ({ KnowledgePicker: (p: Captured) => { seen.knowledge = p; return null } }))
vi.mock('../app-sdk/ChatScrollChrome', async (orig) => ({ ...(await orig<typeof import('../app-sdk/ChatScrollChrome')>()), JumpToBottomButton: () => null }))
// The dock reads where the chat runs (a react-query read); here it runs on this machine.
const runsHere = vi.hoisted(() => ({ value: true as boolean | undefined }))
vi.mock('../components/sessionEnvironmentHooks', async (orig) => ({
  ...(await orig<typeof import('../components/sessionEnvironmentHooks')>()),
  useSlotRunsHere: () => runsHere.value,
}))

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ComposerDockView from './chat/page/ComposerDockView'
import { CLEARANCE_GAP_PX, TOP_OVERLAY_ATTR, TOP_OVERLAY_GROWS, growingTopOverlays, overlayInset, topOverlays } from './chat/browserLiveClearance'
import { dismissFollowupItem } from '../store/chatSlice'
import { i18nT } from '../i18n/t'

const rect = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 800, width: 800, height: bottom - top, x: 0, y: top, toJSON: () => ({}) }) as DOMRect

type DockProps = Parameters<typeof ComposerDockView>[0]

function dockProps(over: Partial<DockProps> = {}): DockProps {
  return {
    activeSlot: 'slot-a', currentSlot: { key: 'slot-a', project: '/p', folder_id: 'f1' } as never, dispatch: vi.fn() as never,
    navigate: vi.fn() as never, messages: [], embedded: false, knowledgeFetch: { results: [], loading: false, error: null } as never,
    historySuggestions: [], sessionControls: [], composerAbove: null as never, queuedMessages: [] as never,
    agentDropdown: false, modelDropdown: false, projectPickerOpen: false, openSessionControl: null, followUpOptions: [],
    pasteSlice: {} as never, composerChips: {} as never, chatConfig: {} as never, messageQuote: { pendingQuote: null, clearQuote: vi.fn() } as never,
    dropTargetProps: {} as never, followUpPickedRef: { current: new Set() } as never, effectiveAgents: [] as never,
    hiddenModelsQ: { isError: false, refetch: vi.fn() } as never, modelPickerReturnsFocusRef: { current: false } as never,
    provider: { id: 'acp', capabilities: {}, getContextWindow: () => undefined } as never, refusedPress: null as never,
    ...over,
  } as DockProps
}

beforeEach(() => { for (const k of Object.keys(seen)) seen[k] = null; runsHere.value = true; realModelPicker.on = false })

describe('ComposerDockView', () => {
  it.each([
    [true, 'local'],
    [false, 'remote'],
    [undefined, 'pending'],
  ] as const)('sends ! commands where the chat runs (runs here: %s -> %s)', (here, target) => {
    runsHere.value = here
    render(<ComposerDockView {...dockProps()} />)
    expect(seen.chatInput!.terminalCommands).toBe(target)
  })

  it('dismisses the prefill hint from the composer', () => {
    const setPrefillHint = vi.fn()
    render(<ComposerDockView {...dockProps({ setPrefillHint })} />)
    act(() => { (seen.chatInput!.onDismissHint as () => void)() })
    expect(setPrefillHint).toHaveBeenCalledWith(false)
  })

  it('injects or skips knowledge results', () => {
    const inject = vi.fn()
    const clearResults = vi.fn()
    render(<ComposerDockView {...dockProps({ knowledgeFetch: { results: [{ id: 'r1' }], loading: false, error: null, inject, clearResults } as never })} />)
    const picked = [{ id: 'r1' }]
    act(() => { (seen.knowledge!.onInject as (s: unknown) => void)(picked) })
    expect(inject).toHaveBeenCalledWith(picked)
    act(() => { (seen.knowledge!.onSkip as () => void)() })
    expect(clearResults).toHaveBeenCalled()
  })

  it('skips one follow-up item on its own card', () => {
    const p = dockProps({ pendingFollowup: { items: [{ prompt: 'x' }], ts: 42 } as never })
    render(<ComposerDockView {...p} />)
    act(() => { (seen.followUp!.onSkip as (i: number) => void)(0) })
    expect(p.dispatch).toHaveBeenCalledWith(dismissFollowupItem({ slot: 'slot-a', index: 0, ts: 42 }))
  })

  it('closes the agent pop-up on the way to the crews page', () => {
    const setAgentDropdown = vi.fn()
    const p = dockProps({ agentDropdown: true, agentBtnRect: { top: 0, left: 0, width: 10, height: 10 } as never, setAgentDropdown, filteredAgents: [] as never })
    render(<ComposerDockView {...p} />)
    act(() => { (seen.manageAgents!.onManage as () => void)() })
    expect(setAgentDropdown).toHaveBeenCalledWith(false)
    expect(p.navigate).toHaveBeenCalledWith('/capabilities?tab=crews')
  })

  it('scrolls the complete top-down agent menu inside its cap, so both footer controls stay reachable', () => {
    const bottom = window.innerHeight - 200
    render(<ComposerDockView {...dockProps({
      topDown: true,
      agentDropdown: true,
      agentBtnRect: { top: bottom - 24, bottom, left: 24, width: 96, height: 24 } as never,
      filteredAgents: [] as never,
      activeAgentName: 'kirocrew',
      defaultAgent: 'kirocrew',
      installedAgents: [] as never,
    })} />)
    const menu = screen.getByRole('dialog')
    const list = screen.getByRole('listbox')

    expect(menu.style.top).toBe(`${bottom + 4}px`)
    expect(menu.style.bottom).toBe('')
    expect(menu.style.maxHeight).toBe(`${200 - 4 - 8}px`)
    expect(menu.className.split(/\s+/)).toContain('overflow-y-auto')
    expect(list.className.split(/\s+/)).toContain('overflow-y-auto')
    expect(list.style.minHeight).toBe('96px')
    expect(menu.contains(screen.getByTestId('default-agent-footer'))).toBe(true)
    expect(menu.contains(screen.getByTestId('manage-agents-footer'))).toBe(true)
  })

  it('falls back above when the top-down agent menu has too little room below', () => {
    const bottom = window.innerHeight - 20
    render(<ComposerDockView {...dockProps({
      topDown: true,
      agentDropdown: true,
      agentBtnRect: { top: bottom - 24, bottom, left: 24, width: 96, height: 24 } as never,
      filteredAgents: [] as never,
      activeAgentName: 'kirocrew',
      defaultAgent: 'kirocrew',
      installedAgents: [] as never,
    })} />)
    const menu = screen.getByRole('dialog')
    const list = screen.getByRole('listbox')

    expect(menu.style.top).toBe('')
    expect(menu.style.bottom).toBe('48px')
    expect(menu.style.maxHeight).toBe('')
    expect(menu.className.split(/\s+/)).toContain('overflow-y-auto')
    expect(list.className.split(/\s+/)).toContain('overflow-y-auto')
    expect(list.style.minHeight).toBe('96px')
    expect(menu.contains(screen.getByTestId('default-agent-footer'))).toBe(true)
    expect(menu.contains(screen.getByTestId('manage-agents-footer'))).toBe(true)
  })

  it('keeps the main agent menu placement and list styles outside top-down layout', () => {
    render(<ComposerDockView {...dockProps({
      agentDropdown: true,
      agentBtnRect: { top: 400, bottom: 424, left: 24, width: 96, height: 24 } as never,
      filteredAgents: [] as never,
      activeAgentName: 'kirocrew',
      defaultAgent: 'kirocrew',
      installedAgents: [] as never,
    })} />)
    const menu = screen.getByRole('dialog')
    const list = screen.getByRole('listbox')

    expect(menu.style.bottom).toBe(`${window.innerHeight - 400 + 4}px`)
    expect(menu.style.top).toBe('')
    expect(menu.style.maxHeight).toBe('')
    expect(list.className).toBe('overflow-y-auto max-h-[280px]')
  })

  // Top-down floats the dock over the transcript's TOP edge, under the title
  // band. The browser card's header clearance and frame fit
  // (browserLiveClearance) measure only marked top overlays, so the dock must
  // carry the marker there, or a taken-over card's header stays under it.
  it('marks the top-down dock root as a transcript top overlay, so the browser card clears its bottom edge', () => {
    render(<ComposerDockView {...dockProps({ topDown: true, dockTop: 40 })} />)
    const dock = screen.getByTestId('composer-dock-root')
    expect(dock).toHaveAttribute(TOP_OVERLAY_ATTR, TOP_OVERLAY_GROWS)
    expect(growingTopOverlays()).toContain(dock)
    expect(topOverlays()).toContain(dock)
    const scroller = document.createElement('div')
    document.body.appendChild(scroller)
    vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue(rect(0, 800))
    vi.spyOn(dock, 'getBoundingClientRect').mockReturnValue(rect(40, 220))
    // The dock's bottom edge (220) plus the gap is what the card may not use.
    expect(overlayInset(scroller)).toBe(220 + CLEARANCE_GAP_PX)
    scroller.remove()
  })

  // Top-down, the dock grows downward from `dockTop`. Without a cap a long
  // status stack plus the composer pushes the Send row below the pane edge.
  it('caps the top-down dock to the height under dockTop; only the status stack shrinks and scrolls', () => {
    render(<ComposerDockView {...dockProps({ topDown: true, dockTop: 40 })} />)
    const dock = screen.getByTestId('composer-dock-root')
    const stack = screen.getByTestId('composer-status-stack')
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex', 'flex-col']))
    expect(dock.style.maxHeight).toBe('calc(100% - 40px)')
    expect(stack.className.split(/\s+/)).toEqual(expect.arrayContaining(['max-h-[50svh]', 'overflow-y-auto']))
    const composerRow = stack.nextElementSibling as HTMLElement
    expect(stack.contains(composerRow)).toBe(false)
  })

  it.each([[false], [undefined]] as const)('leaves the bottom dock uncapped (topDown %s)', (topDown) => {
    render(<ComposerDockView {...dockProps(topDown === undefined ? {} : { topDown })} />)
    const dock = screen.getByTestId('composer-dock-root')
    expect(dock.className.split(/\s+/)).not.toContain('flex-col')
    expect(dock.style.maxHeight).toBe('')
  })

  // The bottom dock covers the transcript's bottom edge. As a top overlay its
  // bottom (the pane's bottom) would push every card out of view.
  it.each([[false], [undefined]] as const)('leaves the bottom dock root unmarked (topDown %s)', (topDown) => {
    render(<ComposerDockView {...dockProps(topDown === undefined ? {} : { topDown })} />)
    const dock = screen.getByTestId('composer-dock-root')
    expect(dock).not.toHaveAttribute(TOP_OVERLAY_ATTR)
    expect(topOverlays()).not.toContain(dock)
    const scroller = document.createElement('div')
    document.body.appendChild(scroller)
    vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue(rect(0, 800))
    vi.spyOn(dock, 'getBoundingClientRect').mockReturnValue(rect(620, 800))
    expect(overlayInset(scroller)).toBe(0)
    scroller.remove()
  })

  it.each([[true, true], [false, false], [undefined, false]] as const)('opens the memory-mode popover below the chip only in top-down layout (topDown %s)', (topDown, openBelow) => {
    render(<ComposerDockView {...dockProps({ showComposerMemoryChip: true, ...(topDown === undefined ? {} : { topDown }) })} />)
    expect(seen.memoryChip!.openBelow).toBe(openBelow)
  })

  it('closes the model pop-up, retries the visibility read, and pins the slot model to its agent', () => {
    const setModelDropdown = vi.fn()
    const refetch = vi.fn()
    const mutate = vi.fn()
    render(<ComposerDockView {...dockProps({
      modelDropdown: true, modelBtnRect: { top: 0, left: 0, width: 10, height: 10 } as never, setModelDropdown,
      hiddenModelsQ: { isError: true, refetch } as never, pinModelToAgentMut: { mutate } as never,
      _modelPinAgent: 'helper', _modelPinActive: 'auto', modelPickerConfigured: true,
    })} />)
    act(() => { (seen.modelDropdown!.onClose as () => void)() })
    expect(setModelDropdown).toHaveBeenCalledWith(false)
    act(() => { (seen.modelDropdown!.onRetryModelVisibility as () => void)() })
    expect(refetch).toHaveBeenCalled()
    act(() => { (seen.modelDropdown!.onPinToAgent as () => void)() })
    // 'auto' is the display fallback, never a pin: the pin writes the empty "no pin" model.
    expect(mutate).toHaveBeenCalledWith({ agent: 'helper', model: '' })
  })

  it.each([
    [{ fastMode: false }, true],
    [{}, false],
  ])("draws the model picker's Fast mode switch only while the slot's read carries it (%o)", (over, shown) => {
    realModelPicker.on = true
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ComposerDockView {...dockProps({ modelDropdown: true, modelBtnRect: { top: 0, left: 0, width: 10, height: 10 } as never, filteredModels: [], ...over })} />
      </QueryClientProvider>,
    )
    const toggle = screen.queryByRole('switch', { name: i18nT('components.fastModeToggle.label') })
    expect(toggle !== null).toBe(shown)
    if (toggle) expect(toggle.getAttribute('aria-disabled')).toBeNull()
  })

  it("draws a Fast mode switch the harness says cannot be turned on disabled, with the harness's reason", () => {
    realModelPicker.on = true
    const reason = 'disabled by environment configuration'
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ComposerDockView {...dockProps({
          modelDropdown: true, modelBtnRect: { top: 0, left: 0, width: 10, height: 10 } as never, filteredModels: [],
          fastMode: false, fastUnavailable: reason,
        })} />
      </QueryClientProvider>,
    )
    const toggle = screen.getByRole('switch', { name: i18nT('components.fastModeToggle.label') })
    expect(toggle.getAttribute('aria-disabled')).toBe('true')
    expect(document.getElementById(toggle.getAttribute('aria-describedby') ?? '')?.textContent)
      .toBe(i18nT('components.fastModeToggle.unavailable', { reason }))
  })

  it('closes the project picker when its AWS profile footer hands off', () => {
    const setProjectPickerOpen = vi.fn()
    render(<ComposerDockView {...dockProps({ _slotProject: '/p', projectPickerOpen: true, setProjectPickerOpen })} />)
    act(() => { (seen.awsProfile!.onHandoff as () => void)() })
    expect(setProjectPickerOpen).toHaveBeenCalledWith(false)
  })

  it("closes an app's session control and refreshes the control statuses", () => {
    const setOpenSessionControl = vi.fn()
    const refreshSessionControlStatuses = vi.fn()
    render(<ComposerDockView {...dockProps({
      openSessionControl: { key: 'c1', slot: 'slot-a' }, sessionControls: [{ key: 'c1' }] as never,
      setOpenSessionControl, refreshSessionControlStatuses,
    })} />)
    act(() => { (seen.sessionControl!.onClose as () => void)() })
    expect(setOpenSessionControl).toHaveBeenCalledWith(null)
    expect(refreshSessionControlStatuses).toHaveBeenCalled()
  })

  it('labels an untitled history suggestion "Untitled session" and resumes its key (#20877)', () => {
    const handleResumeSession = vi.fn()
    render(<ComposerDockView {...dockProps({
      showHistorySuggestions: true,
      handleResumeSession,
      historySuggestions: [
        { key: 'chat-9-1785440411', title: '' },
        { key: 'chat-8-1785440400', title: 'chat-8-1785440400' },
        { key: 's-named', title: 'Named work' },
      ] as never,
    })} />)
    const options = screen.getAllByRole('option')
    // The name line never shows the key; an untitled row's second line does,
    // so two untitled rows stay distinct.
    expect(options.map(o => o.querySelector('.text-\\[13px\\]')?.textContent)).toEqual(['Untitled session', 'Untitled session', 'Named work'])
    expect(options.map(o => o.querySelector('.text-\\[11px\\]')?.textContent ?? null)).toEqual(['chat-9-1785440411', 'chat-8-1785440400', null])

    // Mouse and keyboard resume still send the key and the old `title || key`.
    fireEvent.mouseDown(options[0])
    expect(handleResumeSession).toHaveBeenLastCalledWith('chat-9-1785440411', 'chat-9-1785440411')
    fireEvent.keyDown(options[1], { key: 'Enter' })
    expect(handleResumeSession).toHaveBeenLastCalledWith('chat-8-1785440400', 'chat-8-1785440400')
  })

  it('hands the transcript quote jump to the composer, so the staged card jumps like a sent one (#34335)', () => {
    const onJumpToQuote = vi.fn()
    render(<ComposerDockView {...dockProps({ onJumpToQuote })} />)
    const quote = { role: 'user' as const, text: 'q', ts: 't1' }
    act(() => { (seen.chatInput!.onJumpToQuote as (q: typeof quote) => void)(quote) })
    expect(onJumpToQuote).toHaveBeenCalledWith(quote)
  })
})

// happy-dom has no layout, so a real ResizeObserver never fires. This one
// records what it observes, and a test fires it for one target.
class FakeResizeObserver {
  static all: FakeResizeObserver[] = []
  readonly targets = new Set<Element>()
  constructor(readonly cb: ResizeObserverCallback) { FakeResizeObserver.all.push(this) }
  observe(t: Element) { this.targets.add(t) }
  unobserve(t: Element) { this.targets.delete(t) }
  disconnect() { this.targets.clear() }
}
const observed = (t: Element) => FakeResizeObserver.all.some(o => o.targets.has(t))
const fireResize = (t: Element) => { for (const o of FakeResizeObserver.all) if (o.targets.has(t)) o.cb([], o as unknown as ResizeObserver) }
/** The box's content height (`scroll`) and box height (`client`), read live. */
function stubHeights(el: HTMLElement, h: { scroll: number; client: number }) {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => h.scroll })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => h.client })
}

// Top-down, the card box above the composer is a `dock-inert` scroll box: it
// passes input through, so its own scrollbar is a hit target only while it
// carries the overflow mark (`.dock-inert[data-overflowing="true"]`).
describe('ComposerDockView: the top-down card box marks its overflow', () => {
  let realResizeObserver: typeof ResizeObserver
  beforeEach(() => {
    realResizeObserver = globalThis.ResizeObserver
    FakeResizeObserver.all = []
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
  })
  afterEach(() => { globalThis.ResizeObserver = realResizeObserver })

  const followup = { pendingFollowup: { items: [{ prompt: 'x' }], ts: 1 } as never }

  it('marks the box while it overflows, also when only a card grows inside the capped box', async () => {
    const view = render(<ComposerDockView {...dockProps({ topDown: true, dockTop: 40, ...followup })} />)
    const precards = screen.getByTestId('composer-precards')
    const card = precards.firstElementChild as HTMLElement
    const h = { scroll: 100, client: 100 }
    stubHeights(precards, h)
    fireResize(precards)
    expect(precards.dataset.overflowing).toBe('false')
    // At the cap the box keeps its height, so only the card reports the growth.
    h.scroll = 180
    fireResize(card)
    expect(precards.dataset.overflowing).toBe('true')
    h.scroll = 100
    fireResize(card)
    expect(precards.dataset.overflowing).toBe('false')
    // A card that mounts resizes nothing already observed: the mount re-marks.
    h.scroll = 160
    view.rerender(<ComposerDockView {...dockProps({ topDown: true, dockTop: 40, ...followup, refusedPress: { action: 'continue', message: 'refused' } as never })} />)
    const refused = screen.getByTestId('refused-press-error')
    await waitFor(() => expect(precards.dataset.overflowing).toBe('true'))
    expect(observed(refused)).toBe(true)
    // So does a card that unmounts.
    h.scroll = 100
    view.rerender(<ComposerDockView {...dockProps({ topDown: true, dockTop: 40, ...followup })} />)
    await waitFor(() => expect(precards.dataset.overflowing).toBe('false'))
    expect(observed(refused)).toBe(false)
    view.unmount()
    expect(observed(precards)).toBe(false)
    expect(observed(card)).toBe(false)
  })

  it.each([[false], [undefined]] as const)('leaves the bottom card box unobserved and unmarked (topDown %s)', (topDown) => {
    render(<ComposerDockView {...dockProps({ ...(topDown === undefined ? {} : { topDown }), ...followup })} />)
    const precards = screen.getByTestId('composer-precards')
    expect(precards.className).toBe('contents')
    expect(observed(precards)).toBe(false)
    expect(precards).not.toHaveAttribute('data-overflowing')
  })
})
