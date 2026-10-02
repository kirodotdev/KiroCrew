/**
 * The composer's control row (approval-mode picker)
 * scrolls horizontally at narrow widths, and on macOS/iOS the overlay
 * scrollbar leaves no idle trace, so the row reads as complete while controls
 * sit off-screen. The cue is the shared `useScrollEdges` measurement painting
 * a gradient over the clipped edge — the same treatment the sibling strips
 * (FollowUpBar's scroll row, SidePanelLayout's tab strip) ship.
 *
 * These tests pin the wiring, and each names what reverting it breaks:
 *
 *   - a row that fits shows no cue (revert symptom: a permanent fade lies
 *     that a control is hidden),
 *   - a clipped row cues the hidden side only (revert symptom: no signal at
 *     all — the original defect),
 *   - the cues follow the row as it scrolls (needs the hook's scroll
 *     listener, not a one-shot read),
 *   - a control appearing remeasures without any scroll or resize event
 *     (needs the remeasure effect keyed on the row's prop-driven content; the
 *     scroller keeps its own box when children change, so no observer fires).
 *
 * jsdom does no layout, so scroll geometry is stubbed — the stub is what makes
 * the derivation testable, mirroring FollowUpBar.scrollEdges.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { normalizeAutomationRecord } from '../monitoring/automation'
import { api } from '../api/client'
import { structuredMonitorLoop } from './monitorFixtures'

const voiceInput = vi.hoisted(() => ({ onVoiceToggle: undefined as (() => void) | undefined }))
vi.mock('../chat-core/composer/Composer', async importOriginal => ({
  ...await importOriginal<typeof import('../chat-core/composer/Composer')>(),
  useComposerVoiceSlice: () => ({ inputProps: voiceInput }),
}))

/** `hidden` px of content beyond the right edge, `scrolled` px already past the left. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(320)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(320 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

const leftCue = () => screen.queryByTestId('control-row-cue-left')
const rightCue = () => screen.queryByTestId('control-row-cue-right')
const controlRow = () => screen.getByTestId('composer-control-row')

describe('ChatInput control-row scroll-edge cues', () => {
  beforeEach(() => {
    voiceInput.onVoiceToggle = undefined
    localStorage.removeItem('mc-input-height')
    localStorage.removeItem('mc-composer-collapsed')
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => {
    vi.restoreAllMocks()
    cleanup()
    localStorage.removeItem('mc-input-height')
    localStorage.removeItem('mc-composer-collapsed')
  })

  it('shows no cue when every control fits', () => {
    stubGeometry({ hidden: 0 })
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(leftCue()).toBeNull()
    expect(rightCue()).toBeNull()
  })

  it('cues only the side hiding content when the row overflows', () => {
    stubGeometry({ hidden: 240 })
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(rightCue()).toBeTruthy()
    // The cue is paint, not surface: it sits over the edge controls, so
    // letting it catch clicks would put a dead zone on the picker underneath,
    // and it must stay silent to assistive tech.
    expect(rightCue()).toHaveClass('pointer-events-none')
    expect(rightCue()).toHaveAttribute('aria-hidden', 'true')
    // Nothing is hidden to the left at offset 0; a cue there would point at
    // content that does not exist.
    expect(leftCue()).toBeNull()
  })

  it('follows the row as it scrolls', () => {
    stubGeometry({ hidden: 240 })
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(leftCue()).toBeNull()

    // Scrolled to the far end: the hidden side flips.
    stubGeometry({ hidden: 240, scrolled: 240 })
    fireEvent.scroll(controlRow())
    expect(leftCue()).toBeTruthy()
    expect(rightCue()).toBeNull()
  })

  it('remeasures when a control appears, without any scroll or resize event', () => {
    // The row fits, then the approval-mode picker mounts (the slot's mode
    // arrives) and the content now clips. The scroller's own box never
    // changed, so neither the ResizeObserver nor a scroll event reports it —
    // only the remeasure keyed on the row's prop-driven content can update
    // the cue. Reverting that effect leaves this row cue-less.
    stubGeometry({ hidden: 0 })
    const { rerender } = renderWithProviders(<ChatInput {...defaultProps} />)
    expect(rightCue()).toBeNull()

    vi.restoreAllMocks()
    stubGeometry({ hidden: 240 })
    rerender(<ChatInput {...defaultProps} approvalMode="default" />)
    expect(rightCue()).toBeTruthy()
  })

  it.each([0, undefined])('keeps suggestion details and its explicit action above the input (generation %s)', async generation => {
    const wire = {
      id: 'goal-1', slot_key: 'chat-1', active: false, config_generation: generation,
      goal: { objective: 'Check keyboard focus', criteria: [], progress: '', status: 'suggested', evidence: [] },
    }
    const resume = vi.spyOn(api, 'autonudgeResume').mockResolvedValue({ loop: wire })
    const refresh = vi.spyOn(api, 'autonudgeForSlot').mockResolvedValue({ enabled: true, loop: wire })
    const onAutomationClick = vi.fn()
    const onAutomationChange = vi.fn()
    renderWithProviders(<ChatInput
      {...defaultProps}
      slotId="chat-1"
      automation={normalizeAutomationRecord(wire)}
      onAutomationClick={onAutomationClick}
      onAutomationChange={onAutomationChange}
    />)
    const trigger = await screen.findByRole('button', { name: 'Keep working until this is verified?: Check keyboard focus' })
    const row = screen.getByTestId('composer-automation-row')
    const action = within(row).getByRole('button', { name: generation === undefined ? 'Refresh status' : 'Start' })
    expect(within(row).getAllByRole('button')).toEqual([trigger, action])
    expect(trigger).toBeVisible()
    expect(action).toBeVisible()
    expect(controlRow().contains(row)).toBe(false)
    expect(controlRow().querySelector('[data-goal-suggestion]')).toBeNull()
    expect(row.compareDocumentPosition(screen.getByRole('textbox', { name: 'Message input' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    fireEvent.click(trigger)
    expect(onAutomationClick).toHaveBeenCalledWith(true)
    expect(resume).not.toHaveBeenCalled()
    expect(refresh).not.toHaveBeenCalled()
    fireEvent.click(action)
    await waitFor(() => expect(onAutomationChange).toHaveBeenCalledTimes(1))
    if (generation === undefined) {
      expect(refresh).toHaveBeenCalledExactlyOnceWith('chat-1')
      expect(resume).not.toHaveBeenCalled()
    } else {
      expect(resume).toHaveBeenCalledExactlyOnceWith('goal-1', generation)
      expect(refresh).not.toHaveBeenCalled()
    }
  })

  it('keeps automation outside the saved 93px input box while sharing its draft and collapse owner', async () => {
    localStorage.setItem('mc-input-height', '93')
    const props = {
      ...defaultProps, value: 'Keep this draft', slotId: 'chat-1', collapsible: true,
      onChange: vi.fn(), onAutomationClick: vi.fn(), onUploadFiles: vi.fn(),
    }
    const suggested = normalizeAutomationRecord({
      id: 'goal-1', slot_key: 'chat-1', active: false, config_generation: 0,
      goal: { objective: 'Check keyboard focus', criteria: [], progress: '', status: 'suggested', evidence: [] },
    })
    const { rerender, unmount } = renderWithProviders(<ChatInput {...props} automation={null} />)
    const trigger = await screen.findByRole('button', { name: 'Set a goal' })
    const row = screen.getByTestId('composer-automation-row')
    const wrapper = screen.getByTestId('input-wrapper')
    expect(wrapper.style.height).toBe('93px')
    expect(wrapper.contains(row)).toBe(false)
    expect(row.parentElement).toBe(wrapper.parentElement)
    expect(screen.getByTestId('composer-dock')).toContainElement(row)
    expect(wrapper).toContainElement(screen.getByRole('textbox', { name: 'Message input' }))
    expect(wrapper).toContainElement(controlRow())
    expect(wrapper).toContainElement(screen.getByRole('button', { name: 'Send' }))
    expect(wrapper).toContainElement(screen.getByRole('button', { name: 'Add files & options' }))

    rerender(<ChatInput {...props} automation={suggested} />)
    expect(screen.getAllByTestId('composer-automation-row')).toEqual([row])
    expect(within(row).getAllByRole('button')).toEqual([
      trigger, within(row).getByRole('button', { name: 'Start' }),
    ])
    expect(wrapper.style.height).toBe('93px')
    expect(localStorage.getItem('mc-input-height')).toBe('93')
    expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveValue(props.value)

    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
    fireEvent.click(screen.getByTestId('composer-collapse-row'))
    expect(await screen.findByTestId('composer-collapsed-bar')).toHaveTextContent(props.value)
    expect(localStorage.getItem('mc-composer-collapsed')).toBe('1')
    // A fresh collapsed mount avoids waiting for an exit animation in the test DOM.
    unmount()
    renderWithProviders(<ChatInput {...props} automation={suggested} />)
    expect(screen.queryByTestId('composer-automation-row')).toBeNull()
    expect(screen.queryByTestId('input-wrapper')).toBeNull()
    fireEvent.click(screen.getByTestId('composer-collapsed-bar'))
    await screen.findByRole('button', { name: 'Start' })
    const restoredRow = screen.getByTestId('composer-automation-row')
    const restoredWrapper = screen.getByTestId('input-wrapper')
    expect(screen.getAllByTestId('composer-automation-row')).toEqual([restoredRow])
    expect(restoredWrapper.contains(restoredRow)).toBe(false)
    expect(restoredRow.parentElement).toBe(restoredWrapper.parentElement)
    expect(restoredWrapper).toContainElement(controlRow())
    expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveValue(props.value)
    expect(props.onChange).not.toHaveBeenCalled()
    expect(localStorage.getItem('mc-input-height')).toBe('93')
  })

  it('keeps manual and watch entry in the same row while bottom controls remain reachable', async () => {
    const onVoiceToggle = vi.fn()
    voiceInput.onVoiceToggle = onVoiceToggle
    const props = {
      ...defaultProps, value: 'Keep this draft', slotId: 'chat-1',
      onAutomationClick: vi.fn(), onUploadFiles: vi.fn(),
      onSend: vi.fn(), approvalMode: 'default',
    }
    const { rerender } = renderWithProviders(<ChatInput {...props} automation={null} />)
    const trigger = await screen.findByRole('button', { name: 'Set a goal' })
    const row = screen.getByTestId('composer-automation-row')
    const upload = screen.getByRole('button', { name: 'Add files & options' })
    const approval = within(controlRow()).getByRole('button', { name: /Approval mode:/ })
    const voice = screen.getByRole('button', { name: 'Voice input' })
    const send = screen.getByRole('button', { name: 'Send' })
    expect(row.contains(trigger)).toBe(true)
    for (const automation of [
      normalizeAutomationRecord({ id: 'manual-1', slot_key: 'chat-1', active: true, cycle_count: 1, max_cycles: 5 }),
      normalizeAutomationRecord(structuredMonitorLoop()),
      ...(['suggested', 'working'] as const).map(status => normalizeAutomationRecord({
        id: 'goal-1', slot_key: 'chat-1', active: status === 'working',
        goal: { objective: 'Check keyboard focus', criteria: [], progress: '', status, evidence: [] },
      })),
      null,
    ]) {
      rerender(<ChatInput {...props} automation={automation} />)
      expect(screen.getByTestId('composer-automation-row')).toBe(row)
      const [entry] = within(row).getAllByRole('button')
      expect(entry).toBeVisible()
      expect(controlRow().contains(entry)).toBe(false)
      fireEvent.click(entry)
      expect(props.onAutomationClick).toHaveBeenLastCalledWith(true)
      for (const control of [upload, approval, voice, send]) {
        expect(control).toBeVisible()
        expect(control).toBeEnabled()
        expect(row.contains(control)).toBe(false)
      }
      expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveValue('Keep this draft')
    }
    fireEvent.click(send)
    expect(props.onSend).toHaveBeenCalledTimes(1)
    fireEvent.click(voice)
    expect(onVoiceToggle).toHaveBeenCalledTimes(1)
    fireEvent.click(upload)
    expect(await screen.findByRole('button', { name: /Sketch/ })).toBeVisible()
  })

  it('keeps the focused automation trigger and open goal details mounted as goal state changes', async () => {
    const props = { ...defaultProps, slotId: 'chat-1', onAutomationClick: vi.fn() }
    const { rerender } = renderWithProviders(<ChatInput {...props} automation={null} />)
    const trigger = await screen.findByRole('button', { name: 'Set a goal' })
    const row = screen.getByTestId('composer-automation-row')
    trigger.focus()
    for (const [status, label, active] of [
      ['suggested', 'Keep working until this is verified?', false],
      ['working', 'Working toward your goal', true],
      ['paused', 'Goal paused', false],
      ['needs_input', 'Needs your input', false],
      ['complete', 'Goal achieved', false],
    ] as const) {
      const automation = normalizeAutomationRecord({
        id: 'goal-1', slot_key: 'chat-1', active, config_generation: 0,
        goal: { objective: 'Check keyboard focus', criteria: [], progress: '', status, evidence: [] },
      })
      rerender(<ChatInput {...props} automation={automation} />)
      expect(await screen.findByRole('button', { name: `${label}: Check keyboard focus` })).toBe(trigger)
      expect(document.activeElement).toBe(trigger)
      expect(screen.getByTestId('composer-automation-row')).toBe(row)
      expect(row.contains(trigger)).toBe(true)
      expect(controlRow().contains(trigger)).toBe(false)
    }

    const goal = { objective: 'Check keyboard focus', criteria: [], progress: '', evidence: [] }
    const renderGoal = (status: string, active: boolean) => rerender(<ChatInput {...props} automationOpen automation={normalizeAutomationRecord({
      id: 'goal-1', slot_key: 'chat-1', active, config_generation: 0, goal: { ...goal, status },
    })} />)
    renderGoal('suggested', false)
    const details = await screen.findByRole('dialog', { name: goal.objective })
    trigger.focus()
    for (const [status, active] of [['working', true], ['paused', false], ['complete', false]] as const) {
      renderGoal(status, active)
      expect(screen.getByRole('dialog', { name: goal.objective })).toBe(details)
      expect(document.activeElement).toBe(trigger)
      expect(row.contains(trigger)).toBe(true)
    }
  })
})
