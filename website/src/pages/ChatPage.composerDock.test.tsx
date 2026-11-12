/**
 * The composer dock view (pages/chat/page/ComposerDockView.tsx), rendered
 * directly with its children stubbed and their props captured: the actions the
 * dock wires into the composer, the knowledge picker, the follow-up card, the
 * agent and model pop-ups, the project picker's AWS footer and an app's
 * session control.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, screen, fireEvent } from '@testing-library/react'
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
vi.mock('../components/ModelEffortDropdown', () => ({ default: (p: Captured) => { seen.modelDropdown = p; return null } }))
vi.mock('../components/ChatInput', () => ({ default: (p: Captured) => { seen.chatInput = p; return null } }))
vi.mock('../components/SessionControlHost', () => ({ default: (p: Captured) => { seen.sessionControl = p; return null } }))
vi.mock('../components/ErrorNotice', () => ({ default: () => null }))
vi.mock('../components/FollowUpCard', () => ({ default: (p: Captured) => { seen.followUp = p; return null } }))
vi.mock('../components/PendingQuestionCard', () => ({ default: () => null }))
vi.mock('../components/FlyingQuote', () => ({ default: () => null }))
vi.mock('../components/QueueStack', () => ({ default: () => null, SubagentDeliveryProgress: () => null }))
vi.mock('../components/MemoryModeChip', () => ({ MemoryModeChip: (p: Captured) => { seen.memoryChip = p; return null } }))
// The dock is driven by props here; the Crew Mode root read is a store/query hook.
vi.mock('../components/crew/useCrewRoot', () => ({ useCrewRoot: () => ({ rootClosed: false, rootTitle: '', workers: undefined, failed: false }) }))
vi.mock('../chat-core/composer/Composer', () => ({ useComposerDraftText: () => null, Composer: forwardRef(({ children }: { children: ReactNode }, _ref) => <>{children}</>) }))
vi.mock('../composerActions/useComposerActionItems', () => ({ useComposerActionItems: () => [] }))
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

import ComposerDockView from './chat/page/ComposerDockView'
import { CLEARANCE_GAP_PX, TOP_OVERLAY_ATTR, TOP_OVERLAY_GROWS, growingTopOverlays, overlayInset, topOverlays } from './chat/browserLiveClearance'
import { dismissFollowupItem } from '../store/chatSlice'

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

beforeEach(() => { for (const k of Object.keys(seen)) seen[k] = null; runsHere.value = true })

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

  it('scrolls the complete top-down agent menu when its cap is smaller than both footer controls', () => {
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

    expect(menu.style.top).toBe(`${bottom + 4}px`)
    expect(menu.style.bottom).toBe('')
    expect(menu.style.maxHeight).toBe('8px')
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
