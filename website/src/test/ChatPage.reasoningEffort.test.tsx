/**
 * Tests for reasoning effort button in ChatInput.
 * Tests the ChatInput component directly to avoid ChatPage's complex dependencies.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'

vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

import ChatInput, { REASONING_EFFORT_PROVIDERS, EFFORT_LABEL_KEY, modelSupportsEffort } from '../components/ChatInput'
import ReasoningEffortDropdown from '../components/ReasoningEffortDropdown'
import { pendingSlotSwitchTarget, performSlotSwitch, SWITCH_CONFIRM_TIMEOUT_MS } from '../lib/slotSwitch'

beforeEach(() => { vi.clearAllMocks() })

function renderInput(props: Partial<Parameters<typeof ChatInput>[0]> = {}) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: { dashboard: { slots: [], unreadSlots: [], refreshTrigger: 0, subagentRunning: {}, subagentDetails: {}, subagentText: {} } as RootState['dashboard'], chat: { activeSlot: null, messages: [], slotRunning: false, toolLog: [], activityOpen: false } as RootState['chat'], notifications: { items: [] } as RootState['notifications'] },
  })
  const defaults = {
    value: '',
    onChange: vi.fn(),
    onSend: vi.fn(),
    providerId: 'acp',
    reasoningEffort: 'high',
    onReasoningEffortClick: vi.fn(),
    modelName: 'claude-opus-4.7',
    onModelClick: vi.fn(),
  }
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Provider store={store}><ChatInput {...defaults} {...props} /></Provider></QueryClientProvider>)
}

describe('ChatInput reasoning effort button', () => {
  it('renders effort button with current level for claude_code provider', () => {
    renderInput()
    expect(screen.getByText('High')).toBeInTheDocument()
  })

  it('does not render effort button when capability is off (prop undefined)', () => {
    renderInput({ onReasoningEffortClick: undefined })
    expect(screen.queryByText('High')).not.toBeInTheDocument()
  })


  it('calls onModelClick with rect on click (reasoning effort merged into model button)', () => {
    const onModelClick = vi.fn()
    renderInput({ onModelClick })
    fireEvent.click(screen.getByTitle('Model: claude-opus-4.7'))
    expect(onModelClick).toHaveBeenCalledTimes(1)
    expect(onModelClick.mock.calls[0][0]).toHaveProperty('x')
  })

  it('shows disabled state when running', () => {
    renderInput({ isRunning: true })
    const btn = screen.getByTitle('Stop the current response to switch model')
    expect(btn).toBeDisabled()
  })

  it('EFFORT_LABEL_KEY covers all valid values incl xhigh', () => {
    expect(EFFORT_LABEL_KEY['']).toBeDefined()
    expect(EFFORT_LABEL_KEY['low']).toBeDefined()
    expect(EFFORT_LABEL_KEY['medium']).toBeDefined()
    expect(EFFORT_LABEL_KEY['high']).toBeDefined()
    expect(EFFORT_LABEL_KEY['xhigh']).toBeDefined()
    expect(EFFORT_LABEL_KEY['max']).toBeDefined()
  })

  it('REASONING_EFFORT_PROVIDERS is acp-only (kiro-cli is the sole provider)', () => {
    expect(REASONING_EFFORT_PROVIDERS.has('acp')).toBe(true)
    expect(REASONING_EFFORT_PROVIDERS.has('claude_code')).toBe(false)
  })

  it('modelSupportsEffort gates per-model (Fable/Opus/Sonnet/GPT-5.x)', () => {
    // Capable: Fable/Opus/Sonnet in either naming convention.
    expect(modelSupportsEffort('claude-fable-5')).toBe(true)
    expect(modelSupportsEffort('global.anthropic.claude-fable-5[1m]')).toBe(true)
    expect(modelSupportsEffort('claude-opus-4.7')).toBe(true)
    expect(modelSupportsEffort('claude-sonnet-4.6')).toBe(true)
    expect(modelSupportsEffort('global.anthropic.claude-opus-4-8[1m]')).toBe(true)
    // Capable: GPT-5.x (kiro applies effort to GPT models too).
    expect(modelSupportsEffort('gpt-5.6-sol')).toBe(true)
    expect(modelSupportsEffort('gpt-5.6-luna')).toBe(true)
    // Not capable: haiku, auto, empty/undefined, other third-party.
    expect(modelSupportsEffort('claude-haiku-4.5')).toBe(false)
    expect(modelSupportsEffort('auto')).toBe(false)
    expect(modelSupportsEffort('')).toBe(false)
    expect(modelSupportsEffort(undefined)).toBe(false)
    expect(modelSupportsEffort('deepseek-3.2')).toBe(false)
    expect(modelSupportsEffort('minimax-m2.5')).toBe(false)
    expect(modelSupportsEffort('glm-5')).toBe(false)
  })
})

const mockApi = vi.hoisted(() => ({ chatSlotReasoningEffort: vi.fn().mockResolvedValue({ ok: true }), effortLevels: vi.fn().mockResolvedValue(['low', 'medium', 'high', 'xhigh', 'max']) }))
vi.mock('../api/client', () => ({ api: mockApi, SEARCH_MIN_CHARS: 2 }))

function renderDropdown(props: Partial<Parameters<typeof ReasoningEffortDropdown>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const defaults = { slot: 's1', currentEffort: 'high', onClose: vi.fn() }
  // The dropdown writes the slot's effort into the store on persist success
  // (#5120), so the harness carries a real store seeded with the slot row.
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: { slots: [{ key: defaults.slot, messages: 0, running: false, reasoning_effort: defaults.currentEffort }], unreadSlots: [], refreshTrigger: 0, subagentRunning: {}, subagentDetails: {}, subagentText: {} } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  return Object.assign(render(<QueryClientProvider client={qc}><Provider store={store}><ReasoningEffortDropdown {...defaults} {...props} /></Provider></QueryClientProvider>), { store })
}

describe('ReasoningEffortDropdown', () => {
  beforeEach(() => { mockApi.effortLevels.mockClear(); mockApi.chatSlotReasoningEffort.mockClear() })

  it('renders a slider over the concrete levels with the current value', async () => {
    renderDropdown()
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    // 5 concrete levels (low..max) -> index range 0..4, current 'high' = index 2.
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuemax')).toBe('4'))
    expect(slider.getAttribute('aria-valuemin')).toBe('0')
    expect(slider.getAttribute('aria-valuenow')).toBe('2')
    expect(slider.getAttribute('aria-valuetext')).toBe('High')
  })

  it('persists the level for the slot when stepped up', async () => {
    renderDropdown()
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuemax')).toBe('4'))
    // high (index 2) -> ArrowRight -> xhigh (index 3)
    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'xhigh'))
  })

  it('writes the persisted level into the slot row on API success (#5120)', async () => {
    // The local optimistic state masks staleness in THIS popover, but the
    // STORE is what the Alt+Shift effort cycle steps from — the persist must
    // write it. No websocket exists in this harness, so the row can only
    // move if the persist path writes it. The response echoes the STORED
    // value; the mock answers {ok} with no reasoning_effort, exercising the
    // requested-level fallback.
    const { store } = renderDropdown()
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuemax')).toBe('4'))
    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'xhigh'))
    await vi.waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 's1')?.reasoning_effort).toBe('xhigh'))
  })

  it('stages the pick synchronously so a cycle press inside the debounce window sees it (#5120)', async () => {
    // The persist is debounced 150ms (one write per drag). Without staging,
    // an Alt+Shift+D press right after a dropdown pick reads a base that
    // ignores the pick and re-selects it instead of advancing past it. The
    // staged target must be visible IMMEDIATELY after the pick, before any
    // wire call fires.
    renderDropdown()
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuemax')).toBe('4'))
    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    // Synchronous: no debounce flush, no API call yet — the cycle shortcuts'
    // base accessor already reports the pick as the newest intent.
    expect(pendingSlotSwitchTarget('reasoning_effort', 's1')).toBe('xhigh')
    expect(mockApi.chatSlotReasoningEffort).not.toHaveBeenCalled()
    // The debounced persist then goes to the wire as usual.
    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'xhigh'))
  })

  it('discards the debounced persist when a newer request supersedes the pick (#5120)', async () => {
    // GPT round-5 scenario: dropdown picks 'xhigh', a cycle shortcut fires
    // its own request within the 150ms debounce. The shortcut's request
    // begins immediately (clearing the stage), so when the timer fires the
    // pick is no longer the newest intent — persisting it then would make
    // the STALE pick the newest request and win the adjudication, reverting
    // the user's newer choice. The timer must discard it.
    vi.useFakeTimers()
    try {
      renderDropdown()
      await vi.waitFor(() => expect(screen.getByRole('slider', { name: 'Reasoning effort' }).getAttribute('aria-valuemax')).toBe('4'))
      const slider = screen.getByRole('slider', { name: 'Reasoning effort' })
      fireEvent.keyDown(slider, { key: 'ArrowRight' })
      expect(pendingSlotSwitchTarget('reasoning_effort', 's1')).toBe('xhigh')
      // A competing request (the cycle shortcut's) begins inside the window.
      const race = performSlotSwitch('reasoning_effort', 's1', 'max',
        async () => 'max', () => {})
      // The pick is no longer the newest intent...
      expect(pendingSlotSwitchTarget('reasoning_effort', 's1')).toBe('max')
      // ...so the debounce firing must NOT put 'xhigh' on the wire.
      await vi.advanceTimersByTimeAsync(200)
      expect(mockApi.chatSlotReasoningEffort).not.toHaveBeenCalledWith('s1', 'xhigh')
      await race
    } finally {
      vi.useRealTimers()
    }
  })

  it('leaves the slot row untouched when the persist call fails (#5120)', async () => {
    mockApi.chatSlotReasoningEffort.mockRejectedValueOnce(new Error('boom'))
    const { store } = renderDropdown()
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuemax')).toBe('4'))
    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalled())
    // The failed pick changed nothing server-side; the store keeps the
    // pre-pick value (the popover's own optimistic label is local-only).
    expect(store.getState().dashboard.slots.find(s => s.key === 's1')?.reasoning_effort).toBe('high')
  })

  it('rolls the optimistic control back and announces the newest persist failure', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    mockApi.chatSlotReasoningEffort.mockRejectedValueOnce(new Error('effort rejected'))
    const { store } = renderDropdown()
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuemax')).toBe('4'))

    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    expect(slider.getAttribute('aria-valuetext')).toBe('Extra High')

    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'xhigh'))
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuetext')).toBe('High'))
    expect(store.getState().chat.agentSwitchNotice?.message).toBe('effort rejected')
  })

  it('rolls back and announces when the current target times out while still in flight', async () => {
    vi.useFakeTimers()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    let resolveRequest!: (value: { reasoning_effort: string }) => void
    mockApi.chatSlotReasoningEffort.mockImplementationOnce(() => new Promise((resolve) => { resolveRequest = resolve }))
    try {
      const { store } = renderDropdown()
      await vi.waitFor(() => expect(screen.getByRole('slider', { name: 'Reasoning effort' }).getAttribute('aria-valuemax')).toBe('4'))
      const slider = screen.getByRole('slider', { name: 'Reasoning effort' })

      fireEvent.keyDown(slider, { key: 'ArrowRight' })
      expect(slider.getAttribute('aria-valuetext')).toBe('Extra High')

      await vi.advanceTimersByTimeAsync(150)
      expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'xhigh')
      expect(pendingSlotSwitchTarget('reasoning_effort', 's1')).toBe('xhigh')

      await vi.advanceTimersByTimeAsync(SWITCH_CONFIRM_TIMEOUT_MS)
      await vi.waitFor(() => expect(slider.getAttribute('aria-valuetext')).toBe('High'))
      await vi.waitFor(() => expect(store.getState().chat.agentSwitchNotice?.message).toBe(
        'Switch not confirmed yet — it will apply if the connection recovers.',
      ))
    } finally {
      resolveRequest({ reasoning_effort: 'xhigh' })
      await vi.advanceTimersByTimeAsync(0)
      vi.useRealTimers()
    }
  })

  it('announces a failed pending write flushed while the dropdown closes', async () => {
    mockApi.chatSlotReasoningEffort.mockRejectedValueOnce(new Error('close flush rejected'))
    const { store, unmount } = renderDropdown()
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuemax')).toBe('4'))

    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    unmount()

    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'xhigh'))
    await vi.waitFor(() => expect(store.getState().chat.agentSwitchNotice?.message).toBe('close flush rejected'))
  })

  it('does not let an older failure roll back or warn over a newer pick', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    let rejectFirst!: (error: Error) => void
    mockApi.chatSlotReasoningEffort
      .mockImplementationOnce(() => new Promise((_, reject) => { rejectFirst = reject }))
      .mockResolvedValueOnce({ reasoning_effort: 'max' })
    const { store } = renderDropdown()
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuemax')).toBe('4'))

    fireEvent.keyDown(slider, { key: 'ArrowRight' }) // high -> xhigh
    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'xhigh'))
    fireEvent.keyDown(slider, { key: 'ArrowRight' }) // newer intent: max
    expect(slider.getAttribute('aria-valuetext')).toBe('Max')
    rejectFirst(new Error('superseded failure'))

    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'max'))
    expect(slider.getAttribute('aria-valuetext')).toBe('Max')
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('reflects the active level as the slider value', async () => {
    renderDropdown({ currentEffort: 'max' })
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuetext')).toBe('Max'))
  })

  it('renders one tick mark per level boundary', async () => {
    const { container } = renderDropdown()
    await screen.findByRole('slider', { name: 'Reasoning effort' })
    // 5 concrete levels -> 4 segments -> 5 tick marks
    await vi.waitFor(() => expect(container.querySelectorAll('[aria-hidden] > span').length).toBe(5))
  })

  it('always shows the current effort even when absent from the reported list', async () => {
    // Slot is on 'xhigh' but this model only reports low/medium/high.
    mockApi.effortLevels.mockResolvedValueOnce(['low', 'medium', 'high'])
    renderDropdown({ currentEffort: 'xhigh' })
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    // concrete = [low, medium, high, xhigh] -> xhigh is the last index (3).
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuetext')).toBe('Extra High'))
    expect(slider.getAttribute('aria-valuenow')).toBe('3')
  })

  it('fetches effort levels scoped to the slot', async () => {
    renderDropdown({ slot: 'slot-xyz' })
    await vi.waitFor(() => expect(mockApi.effortLevels).toHaveBeenCalledWith('slot-xyz'))
  })

  it('drops the "default" string from the concrete level set', async () => {
    mockApi.effortLevels.mockResolvedValueOnce(['default', 'high', 'low', 'max', 'medium', 'xhigh'])
    renderDropdown()
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    // 5 concrete levels -> max index 4 (no stray 'default'/'' notch).
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuemax')).toBe('4'))
  })

  it('"Use model default" toggle reflects the empty effort and disables the slider', async () => {
    renderDropdown({ currentEffort: '' })
    const toggle = await screen.findByRole('switch', { name: 'Use model default' })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    const slider = screen.getByRole('slider', { name: 'Reasoning effort' })
    expect(slider.getAttribute('aria-disabled')).toBe('true')
  })

  it('toggling default on persists the empty sentinel; off persists a concrete level', async () => {
    // Start explicit ('high') -> toggle on -> persists ''.
    renderDropdown({ currentEffort: 'high' })
    const toggle = await screen.findByRole('switch', { name: 'Use model default' })
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(toggle)
    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', ''))
  })

  it('toggling default off persists the slider level (not empty)', async () => {
    renderDropdown({ currentEffort: '' })
    const toggle = await screen.findByRole('switch', { name: 'Use model default' })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(toggle)
    // default idx for an unset slot is 'high' (index 2 of low..max).
    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'high'))
  })

  // With a Settings default configured, the no-override state names the
  // configured value ("Default · High") rather than a bare "Default", which
  // would read as "the model decides" and hide the value the turn runs at.
  it('names the inherited value when the slot has no override', async () => {
    renderDropdown({ currentEffort: '', defaultEffort: 'high' })
    await screen.findByRole('slider', { name: 'Reasoning effort' })
    expect(screen.getByText('Default · High')).toBeInTheDocument()
  })

  it('labels the toggle for the configured default, not the model default', async () => {
    renderDropdown({ currentEffort: '', defaultEffort: 'high' })
    const toggle = await screen.findByRole('switch', { name: 'Use configured default' })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByRole('switch', { name: 'Use model default' })).not.toBeInTheDocument()
  })

  it('keeps the bare "Default" wording when no default is configured', async () => {
    renderDropdown({ currentEffort: '', defaultEffort: '' })
    await screen.findByRole('slider', { name: 'Reasoning effort' })
    expect(screen.getByText('Default')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Use model default' })).toBeInTheDocument()
  })

  it('an explicit per-slot override still outranks the configured default', async () => {
    renderDropdown({ currentEffort: 'low', defaultEffort: 'max' })
    const slider = await screen.findByRole('slider', { name: 'Reasoning effort' })
    await vi.waitFor(() => expect(slider.getAttribute('aria-valuetext')).toBe('Low'))
    expect(screen.queryByText(/Default/)).not.toBeInTheDocument()
  })
})
