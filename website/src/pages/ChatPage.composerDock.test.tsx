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
  isCrewmateChat: () => true,
  DefaultAgentRow: () => null,
  defaultWriteKind: () => 'agent',
  ManageAgentsFooter: (p: Captured) => { seen.manageAgents = p; return null },
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
vi.mock('../components/MemoryModeChip', () => ({ MemoryModeChip: () => null }))
vi.mock('../chat-core/composer/Composer', () => ({ Composer: forwardRef(({ children }: { children: ReactNode }, _ref) => <>{children}</>) }))
vi.mock('./chat/SubagentProgressBar', () => ({ default: () => null }))
vi.mock('./chat/TaskProgressBar', () => ({ default: () => null }))
vi.mock('./chat/WorkflowProgressBar', () => ({ default: () => null }))
vi.mock('./chat/KnowledgePicker', () => ({ KnowledgePicker: (p: Captured) => { seen.knowledge = p; return null } }))
vi.mock('../app-sdk/ChatScrollChrome', async (orig) => ({ ...(await orig<typeof import('../app-sdk/ChatScrollChrome')>()), JumpToBottomButton: () => null }))

import ComposerDockView from './chat/page/ComposerDockView'
import { dismissFollowupItem } from '../store/chatSlice'

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

beforeEach(() => { for (const k of Object.keys(seen)) seen[k] = null })

describe('ComposerDockView', () => {
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
})
