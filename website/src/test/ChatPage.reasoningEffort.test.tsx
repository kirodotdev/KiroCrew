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
  return render(<QueryClientProvider client={qc}><ReasoningEffortDropdown {...defaults} {...props} /></QueryClientProvider>)
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

  // Regression: with a Settings default configured, the no-override state was
  // labelled a bare "Default", which read as "the model decides" and hid the
  // configured value the turn would actually run at.
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
