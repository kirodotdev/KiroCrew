/**
 * The crew editor's Model pane offers a REASONING EFFORT pin, not just a model.
 *
 * A crew could pin the expensive model and then not say how hard it should think,
 * so the two halves of one decision lived on different surfaces (the model on the
 * crew, the effort on a chat slot the crew may not even have — a scheduled or
 * webhook-woken crew has no slot at all).
 *
 * The control is gated on the model the crew will actually run on, the same way
 * the chat picker is: offering a level on a model the backend drops it for is a
 * control that silently does nothing. The one exception is a pin already stored
 * on such a model — the select stays so it can be cleared without first putting
 * the old model back.
 *
 * SimpleSelect is stubbed with a plain listbox for the reason documented at
 * length in CrewEditorSelect.test.tsx: Radix commits discrete events through
 * `flushSync`, which React refuses inside Testing Library's `act()`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition',
    'variants', 'whileHover', 'whileTap', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const cache = new Map<string, unknown>()
  return {
    motion: new Proxy({}, {
      get: (_t, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

const mockApi = vi.hoisted(() => ({
  kirocrewAgents: vi.fn(),
  agentsInstalled: vi.fn(),
  workspaces: vi.fn(),
  kirocrewConfig: vi.fn(),
  createWorkspace: vi.fn(),
  createKirocrewAgent: vi.fn(),
  updateKirocrewAgent: vi.fn(),
  deleteKirocrewAgent: vi.fn(),
  agentResolvedModel: vi.fn(),
  setDefaultAgent: vi.fn(),
  createChatSlot: vi.fn(),
  models: vi.fn(),
  acpBackends: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('../components/SimpleSelect', () => ({
  default: ({
    options, value, onChange, optionLabels, 'aria-label': ariaLabel,
  }: {
    options: string[]
    value: string
    onChange: (v: string) => void
    optionLabels?: string[]
    'aria-label'?: string
  }) => (
    <div>
      <button
        type="button"
        role="combobox"
        aria-label={ariaLabel}
        aria-expanded={false}
        // Required alongside aria-expanded for the role; the stub is a plain
        // listbox, so the options it controls are its own siblings.
        aria-controls={`${ariaLabel}-options`}
      >
        {optionLabels?.[options.indexOf(value)] ?? value}
      </button>
      <div id={`${ariaLabel}-options`}>
        {options.map((o, i) => (
          <button
            key={o}
            type="button"
            role="option"
            aria-selected={o === value}
            // The effort select's inherit choice is the EMPTY string, which is a
            // real value here (clear the pin), so it needs a name to be clickable.
            onClick={() => onChange(o)}
          >
            {optionLabels?.[i] ?? o}
          </button>
        ))}
      </div>
    </div>
  ),
}))

import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

const REVIEWER = {
  name: 'reviewer',
  kiro_agent: 'reviewer-agent',
  workspace: 'default',
  memory_store: 'default',
  model: 'claude-opus-5',
  reasoning_effort: 'max',
  inherited_acp_backend: '',
}

function renderPage(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MemoryRouter>
          <KiroCrewAgentsPage />
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

/** Render the roster with one crew shaped by `crew`, then open its Model pane. */
async function openModelPane(crew: Record<string, unknown>, queryClient?: QueryClient): Promise<HTMLElement> {
  mockApi.kirocrewAgents.mockResolvedValue({
    agents: [{ ...REVIEWER, ...crew }],
    default_agent: 'kirocrew',
  })
  renderPage(queryClient)
  await waitFor(() => expect(screen.getAllByTestId('crew-card')).toHaveLength(1))
  fireEvent.click(screen.getByRole('button', { name: 'Edit agent reviewer' }))
  const sheet = await screen.findByRole('dialog', { name: 'Edit agent reviewer' })
  fireEvent.click(within(sheet).getByTestId('crew-rail-model'))
  return sheet
}

/** The one select labelled `label`, scoped so a shared option label (both the
 *  model and the effort select offer "Inherited") cannot resolve ambiguously.
 *  The stub renders a trigger and its options as siblings in one wrapper. */
function selectScope(sheet: HTMLElement, label: string) {
  const trigger = within(sheet).getByRole('combobox', { name: label })
  return within(trigger.parentElement as HTMLElement)
}

beforeEach(() => {
  Object.values(mockApi).forEach(mock => mock.mockReset())
  mockApi.agentsInstalled.mockResolvedValue([{ name: 'reviewer-agent' }])
  mockApi.workspaces.mockResolvedValue({ workspaces: [{ name: 'default' }] })
  mockApi.kirocrewConfig.mockResolvedValue({ memory_stores: { default: {} } })
  mockApi.agentResolvedModel.mockResolvedValue({
    model: 'claude-opus-5',
    pinned: true,
    kiro_agent: 'reviewer-agent',
    reasoning_effort: 'max',
    effort_pinned: true,
  })
  mockApi.models.mockResolvedValue([{ model_name: 'claude-opus-5' }, { model_name: 'claude-haiku-4.5' }])
  mockApi.acpBackends.mockResolvedValue({ backends: [
    { id: '', policy_id: 'kiro', selectable: true },
    { id: 'codex', policy_id: 'codex', selectable: true },
    { id: 'claude', policy_id: 'claude', selectable: true },
    { id: 'kas', policy_id: 'kas', selectable: true },
  ] })
  mockApi.createKirocrewAgent.mockResolvedValue({})
  mockApi.updateKirocrewAgent.mockResolvedValue({})
  mockApi.deleteKirocrewAgent.mockResolvedValue({})
  mockApi.setDefaultAgent.mockResolvedValue({})
})

describe('crew editor — reasoning effort pin', () => {
  it.each<[string | null, string]>([
    [null, 'Inherited (Kiro CLI)'],
    ['', 'Kiro CLI'],
    ['claude', 'Claude Code'],
  ])('keeps draft pins when reselecting the current backend %s', async (backend, label) => {
    const sheet = await openModelPane({ acp_backend: backend })
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: label }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('High')
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', { avatar: {}, reasoning_effort: 'high' },
    ))
  })

  it.each<[string | null, string]>([
    [null, 'Inherited (Kiro CLI)'],
    ['', 'Kiro CLI'],
    ['claude', 'Claude Code'],
  ])('restores opening pins after switching away and back to %s', async (backend, label) => {
    const sheet = await openModelPane({ acp_backend: backend })
    mockApi.models.mockResolvedValue([{ model_name: 'codex-test-model' }])
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'codex' }))
    await waitFor(() =>
      expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent('Inherited'),
    )
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: label }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
    expect(within(sheet).getByText('Save changes')).toBeDisabled()

    fireEvent.click(within(sheet).getByTestId('crew-rail-routing'))
    fireEvent.change(within(sheet).getByRole('textbox', { name: 'Triggers' }), { target: { value: 'review' } })
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', { avatar: {}, triggers: 'review' },
    ))
  })

  it.each([
    ['', 'Kiro CLI'],
    ['claude', 'Claude Code'],
  ])('keeps worker pins when making the inherited effective backend %s explicit', async (backend, label) => {
    mockApi.kirocrewConfig.mockResolvedValue({
      memory_stores: { default: {} },
      agent: { acp_backend: backend, member_acp_backend: backend === '' ? 'claude' : '' },
    })
    mockApi.models.mockImplementation(async (requestedBackend?: string) =>
      requestedBackend === backend
        ? [{ model_name: REVIEWER.model }, { model_name: 'worker-catalog-model' }]
        : [{ model_name: 'member-catalog-model' }])
    const sheet = await openModelPane({ crewmate: false, inherited_acp_backend: backend })
    await selectScope(sheet, 'Edit default model').findByRole('option', { name: 'worker-catalog-model' })
    expect(selectScope(sheet, 'Edit default model').queryByRole('option', { name: 'member-catalog-model' })).not.toBeInTheDocument()
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: label }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({
        acp_backend: backend,
        model: REVIEWER.model,
        reasoning_effort: REVIEWER.reasoning_effort,
      }),
    ))
  })

  it('uses the inherited member DM catalog and treats explicit Kiro as a backend change', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({
      memory_stores: { default: {} },
      agent: { acp_backend: '', member_acp_backend: 'claude' },
    })
    mockApi.models.mockImplementation(async (backend?: string) =>
      backend === 'claude'
        ? [{ model_name: REVIEWER.model }, { model_name: 'member-catalog-model' }]
        : [{ model_name: 'kiro-test-model' }])
    const sheet = await openModelPane({ crewmate: true, inherited_acp_backend: 'claude' })
    await selectScope(sheet, 'Edit default model').findByRole('option', { name: 'member-catalog-model' })
    expect(selectScope(sheet, 'Edit default model').queryByRole('option', { name: 'kiro-test-model' })).not.toBeInTheDocument()
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Claude Code' }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('High')

    // The installation default is Kiro, but this editor's inherited route is Claude.
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Inherited (Claude Code)' }))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Kiro CLI' }))
    await selectScope(sheet, 'Edit default model').findByRole('option', { name: 'kiro-test-model' })
    await waitFor(() =>
      expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent('Inherited'),
    )
    expect(mockApi.models).toHaveBeenCalledWith('')
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: '', model: 'auto', reasoning_effort: '' }),
    ))
  })

  it.each([false, true])('uses the roster route without recomputing it from config or crewmate=%s', async crewmate => {
    mockApi.kirocrewConfig.mockResolvedValue({
      agent: { acp_backend: '', member_acp_backend: '' },
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const sheet = await openModelPane({ crewmate, inherited_acp_backend: 'claude' }, queryClient)
    await waitFor(() => expect(mockApi.models).toHaveBeenCalledWith('claude'))
    expect(mockApi.models).not.toHaveBeenCalledWith('')
    expect(within(sheet).getByRole('combobox', { name: 'AI app' })).toHaveTextContent('Inherited (Claude Code)')

    // A roster refresh cannot change the opening route underneath unsaved pins.
    await act(async () => {
      queryClient.setQueryData(['kirocrew-agents'], {
        agents: [{ ...REVIEWER, inherited_acp_backend: '', crewmate }],
        default_agent: 'kirocrew',
      })
    })
    expect(within(sheet).getByRole('combobox', { name: 'AI app' })).toHaveTextContent('Inherited (Claude Code)')
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Claude Code' }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
  })

  it('leaves an absent roster route unknown while allowing an explicit Kiro selection', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({
      agent: { acp_backend: 'claude', member_acp_backend: 'claude' },
    })
    const sheet = await openModelPane({ inherited_acp_backend: undefined })
    expect(mockApi.models).not.toHaveBeenCalled()
    expect(within(sheet).getByRole('combobox', { name: 'AI app' })).toHaveTextContent(/^Inherited$/)
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Kiro CLI' }))
    await waitFor(() => expect(mockApi.models).toHaveBeenCalledWith(''))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
  })

  it('keeps compatible draft pins after the target backend catalog arrives', async () => {
    // A saved pin may be absent from the old catalog but valid on the target.
    mockApi.models.mockResolvedValue([{ model_name: 'kiro-other-model' }])
    const sheet = await openModelPane({ acp_backend: '' })
    await selectScope(sheet, 'Edit default model').findByRole('option', { name: 'kiro-other-model' })
    let finishModels!: (models: { model_name: string }[]) => void
    mockApi.models.mockImplementation(() => new Promise(resolve => { finishModels = resolve }))
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Claude Code' }))
    await waitFor(() => expect(mockApi.models).toHaveBeenCalledWith('claude'))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('High')
    await act(async () => { finishModels([{ model_name: REVIEWER.model }]) })
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: 'claude', model: REVIEWER.model, reasoning_effort: 'high' }),
    ))
  })

  it.each([
    ['', 'claude-opus-4.8', 'claude', 'Claude Code', 'global.anthropic.claude-opus-4-8[1m]'],
    ['', 'opus-4.8-1m', 'claude', 'Claude Code', 'global.anthropic.claude-opus-4-8[1m]'],
    ['claude', 'global.anthropic.claude-opus-4-8[1m]', '', 'Kiro CLI', 'claude-opus-4.8'],
    ['', 'claude-opus-4.5', 'claude', 'Claude Code', 'global.anthropic.claude-opus-4-8'],
    ['claude', 'global.anthropic.claude-opus-4-8', '', 'Kiro CLI', 'claude-opus-4.5'],
  ])('migrates %s pin %s to the equivalent advertised spelling on %s', async (openingBackend, pin, targetBackend, targetLabel, advertised) => {
    mockApi.models.mockImplementation(async (backend: string) =>
      [{ model_name: backend === openingBackend ? pin : advertised }])
    const sheet = await openModelPane({ acp_backend: openingBackend, model: pin })
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: targetLabel }))
    await waitFor(() =>
      expect(within(sheet).getByRole('combobox', { name: 'Edit default model' }).textContent).toBe(advertised),
    )
    expect(mockApi.models).toHaveBeenCalledWith(targetBackend)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('High')
    expect(within(sheet).getByRole('status')).toBeEmptyDOMElement()

    // The advertised spelling belongs to the draft, not the opening snapshot.
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', {
      name: openingBackend === '' ? 'Kiro CLI' : 'Claude Code',
    }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' }).textContent).toBe(pin)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
    expect(within(sheet).getByText('Save changes')).toBeDisabled()
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: targetLabel }))
    await waitFor(() =>
      expect(within(sheet).getByRole('combobox', { name: 'Edit default model' }).textContent).toBe(advertised),
    )
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: targetBackend, model: advertised, reasoning_effort: 'high' }),
    ))
  })

  it('prefers an exact advertised pin over an earlier registry-equivalent row', async () => {
    const pin = 'opus-4.8-1m'
    mockApi.models.mockImplementation(async (backend: string) => backend === 'claude'
      ? [{ model_name: 'global.anthropic.claude-opus-4-8[1m]' }, { model_name: pin }]
      : [{ model_name: pin }])
    const sheet = await openModelPane({ acp_backend: '', model: pin })
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Claude Code' }))
    await selectScope(sheet, 'Edit default model').findByRole('option', { name: 'global.anthropic.claude-opus-4-8[1m]' })
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' }).textContent).toBe(pin)
    expect(within(sheet).getByRole('status')).toBeEmptyDOMElement()
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: 'claude', model: pin, reasoning_effort: 'max' }),
    ))
  })

  it.each([
    // The dotted registry id is 1M; the dashed id is a distinct 200K model.
    ['', 'claude-opus-4.8', 'claude', 'Claude Code', 'claude-opus-4-8'],
    ['claude', 'claude-opus-4-8', '', 'Kiro CLI', 'claude-opus-4.8'],
    ['', 'custom-model.v1', 'codex', 'codex', 'custom-model-v1'],
    ['', 'custom-model-v1', 'codex', 'codex', 'custom-model.v1'],
  ])('keeps distinct %s pin %s separate from %s catalog model %s', async (openingBackend, pin, targetBackend, targetLabel, advertised) => {
    mockApi.models.mockImplementation(async (backend: string) =>
      [{ model_name: backend === openingBackend ? pin : advertised }])
    const sheet = await openModelPane({ acp_backend: openingBackend, model: pin })
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: targetLabel }))
    await waitFor(() => expect(within(sheet).getByRole('status')).toHaveTextContent(
      `“${pin}” is not available in ${targetLabel}. Model and reasoning effort now inherit their defaults.`,
    ))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent('Inherited')
    expect(within(sheet).queryByRole('combobox', { name: 'Edit reasoning effort' })).not.toBeInTheDocument()
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: targetBackend, model: 'auto', reasoning_effort: '' }),
    ))
  })

  it('keeps pins when the target backend has no advertised models to check', async () => {
    const sheet = await openModelPane({ acp_backend: '' })
    mockApi.models.mockResolvedValue([])
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'codex' }))
    await waitFor(() => expect(mockApi.models).toHaveBeenCalledWith('codex'))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: 'codex', model: REVIEWER.model, reasoning_effort: 'max' }),
    ))
  })

  it.each([
    ['codex-test-model', 'auto', ''],
    [REVIEWER.model, REVIEWER.model, 'high'],
  ])('validates pins when an auto-only target catalog later advertises %s', async (advertisedModel, savedModel, savedEffort) => {
    const sheet = await openModelPane({ acp_backend: '' })
    await within(sheet).findByText(`Using ${REVIEWER.model}`)
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    vi.useFakeTimers()
    try {
      mockApi.models.mockResolvedValue([{ model_name: 'auto' }])
      fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'codex' }))
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(mockApi.models).toHaveBeenLastCalledWith('codex')
      expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
      expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('High')
      expect(within(sheet).getByRole('status')).toBeEmptyDOMElement()

      const attempts = mockApi.models.mock.calls.length
      mockApi.models.mockResolvedValue([{ model_name: advertisedModel }])
      // Exercise the real hook's next poll after a successful auto-only response.
      await act(async () => { await vi.advanceTimersByTimeAsync(8_000) })
      expect(mockApi.models).toHaveBeenCalledTimes(attempts + 1)
      expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(
        savedModel === 'auto' ? 'Inherited' : savedModel,
      )
      expect(within(sheet).queryByText(`Using ${REVIEWER.model}`)).not.toBeInTheDocument()
      expect(within(sheet).queryByText(/Thinking at/)).not.toBeInTheDocument()
      if (savedEffort) {
        expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('High')
        expect(within(sheet).getByRole('status')).toBeEmptyDOMElement()
      } else {
        expect(within(sheet).queryByRole('combobox', { name: 'Edit reasoning effort' })).not.toBeInTheDocument()
        expect(within(sheet).getByRole('status')).toHaveTextContent(
          `“${REVIEWER.model}” is not available in codex. Model and reasoning effort now inherit their defaults.`,
        )
      }
      await act(async () => {
        fireEvent.click(within(sheet).getByText('Save changes'))
        await vi.advanceTimersByTimeAsync(1)
      })
      expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
        'reviewer', expect.objectContaining({ acp_backend: 'codex', model: savedModel, reasoning_effort: savedEffort }),
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears only an incompatible effort when the target backend serves the model', async () => {
    const sheet = await openModelPane({ acp_backend: '', model: 'claude-haiku-4.5' })
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Claude Code' }))
    await waitFor(() =>
      expect(within(sheet).queryByRole('combobox', { name: 'Edit reasoning effort' })).not.toBeInTheDocument(),
    )
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent('claude-haiku-4.5')
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: 'claude', model: 'claude-haiku-4.5', reasoning_effort: '' }),
    ))
  })

  it('waits for the selected backend catalog before clearing incompatible pins', async () => {
    const sheet = await openModelPane({ acp_backend: '' })
    let finishModels!: (models: { model_name: string }[]) => void
    mockApi.models.mockImplementation(() => new Promise(resolve => { finishModels = resolve }))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'codex' }))
    await waitFor(() => expect(mockApi.models).toHaveBeenCalledWith('codex'))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    await act(async () => { finishModels([{ model_name: 'codex-test-model' }]) })
    await waitFor(() =>
      expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent('Inherited'),
    )
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: 'codex', model: 'auto', reasoning_effort: '' }),
    ))
  })

  it('ignores a late catalog response after returning to the opening backend', async () => {
    const sheet = await openModelPane({ acp_backend: '' })
    let finishModels!: (models: { model_name: string }[]) => void
    mockApi.models.mockImplementation(() => new Promise(resolve => { finishModels = resolve }))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'codex' }))
    await waitFor(() => expect(mockApi.models).toHaveBeenCalledWith('codex'))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Kiro CLI' }))
    await act(async () => { finishModels([{ model_name: 'codex-test-model' }]) })
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
    expect(within(sheet).getByRole('status')).toBeEmptyDOMElement()
    expect(within(sheet).getByText('Save changes')).toBeDisabled()
  })

  it.each(['another app', 'opening app', 'model choice', 'reopen'])('clears the reset notice after %s', async action => {
    let sheet = await openModelPane({ acp_backend: '' })
    mockApi.models.mockResolvedValue([{ model_name: 'codex-test-model' }])
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'codex' }))
    await waitFor(() => expect(within(sheet).getByRole('status')).toHaveTextContent(
      `“${REVIEWER.model}” is not available in codex.`,
    ))
    expect(within(sheet).getByRole('status')).toHaveAttribute('aria-live', 'polite')
    if (action === 'reopen') {
      fireEvent.click(within(sheet).getByText('Save changes'))
      await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit agent reviewer' })).not.toBeInTheDocument())
      fireEvent.click(screen.getByRole('button', { name: 'Edit agent reviewer' }))
      sheet = await screen.findByRole('dialog', { name: 'Edit agent reviewer' })
      fireEvent.click(within(sheet).getByTestId('crew-rail-model'))
    } else if (action === 'model choice') {
      fireEvent.click(selectScope(sheet, 'Edit default model').getByRole('option', { name: 'codex-test-model' }))
    } else {
      fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', {
        name: action === 'opening app' ? 'Kiro CLI' : 'Claude Code',
      }))
    }
    expect(within(sheet).getByRole('status')).toBeEmptyDOMElement()
  })

  it('names a later reset using the current app after an earlier notice was cleared', async () => {
    const sheet = await openModelPane({ acp_backend: '' })
    mockApi.models.mockResolvedValue([{ model_name: 'codex-test-model' }])
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'codex' }))
    await waitFor(() => expect(within(sheet).getByRole('status')).toHaveTextContent('not available in codex'))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Kiro CLI' }))
    let finishModels!: (models: { model_name: string }[]) => void
    mockApi.models.mockImplementation(() => new Promise(resolve => { finishModels = resolve }))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Claude Code' }))
    await waitFor(() => expect(mockApi.models).toHaveBeenCalledWith('claude'))
    expect(within(sheet).getByRole('status')).toBeEmptyDOMElement()
    await act(async () => { finishModels([{ model_name: 'claude-test-model' }]) })
    await waitFor(() => expect(within(sheet).getByRole('status')).toHaveTextContent(
      `“${REVIEWER.model}” is not available in Claude Code. Model and reasoning effort now inherit their defaults.`,
    ))
  })

  it('keeps validation pending through a target model-list error and explains the reset after retry', async () => {
    const sheet = await openModelPane({ acp_backend: '' })
    mockApi.models.mockRejectedValue(new Error('raw target catalog error'))
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Claude Code' }))
    await within(sheet).findByText('Could not load models for Claude Code.')
    expect(within(sheet).queryByText('raw target catalog error')).not.toBeInTheDocument()
    expect(within(sheet).getByRole('status')).toBeEmptyDOMElement()
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
    mockApi.models.mockResolvedValue([{ model_name: 'claude-test-model' }])
    fireEvent.click(within(sheet).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(within(sheet).getByRole('status')).toHaveTextContent(
      `“${REVIEWER.model}” is not available in Claude Code. Model and reasoning effort now inherit their defaults.`,
    ))
    expect(within(sheet).queryByText('Could not load models for Claude Code.')).not.toBeInTheDocument()
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: 'claude', model: 'auto', reasoning_effort: '' }),
    ))
  })

  it('uses developer backend names and explains when a change takes effect', async () => {
    const sheet = await openModelPane({})
    const backend = selectScope(sheet, 'AI app')
    for (const label of ['Inherited (Kiro CLI)', 'Kiro CLI', 'Claude Code', 'KAS (kiro-agent)']) {
      expect(backend.getByRole('option', { name: label })).toBeInTheDocument()
    }
    expect(within(sheet).getByText(/The software that runs this member’s tasks/)).toHaveTextContent(
      'New sessions use the selected app; sessions already open keep the app they started with.',
    )
  })

  it.each([
    ['backend', 'Could not load AI apps.'],
    ['models', 'Could not load models for Kiro CLI.'],
  ])('offers an in-place retry for %s errors without losing the draft', async (resource, message) => {
    const failedApi = resource === 'backend' ? mockApi.acpBackends : mockApi.models
    const retryResult = resource === 'backend'
      ? { backends: [{ id: '', policy_id: 'kiro', selectable: true }] }
      : [{ model_name: REVIEWER.model }]
    failedApi.mockRejectedValue(new Error('raw transport detail'))
    const sheet = await openModelPane({ acp_backend: '' })
    const notice = await within(sheet).findByText(message)
    expect(notice.closest('[role="alert"]')).toBeInTheDocument()
    expect(within(sheet).getByRole('combobox', { name: 'AI app' })).toHaveTextContent(/^Kiro CLI$/)
    expect(within(sheet).queryByText('raw transport detail')).not.toBeInTheDocument()
    expect(within(sheet).queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    const attempts = failedApi.mock.calls.length
    failedApi.mockResolvedValue(retryResult)
    fireEvent.click(within(sheet).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(failedApi).toHaveBeenCalledTimes(attempts + 1))
    await waitFor(() => expect(within(sheet).queryByText(message)).not.toBeInTheDocument())
    expect(within(sheet).getByRole('combobox', { name: 'AI app' })).toHaveTextContent(/^Kiro CLI$/)
    expect(selectScope(sheet, 'AI app').getAllByRole('option', { name: 'Kiro CLI' })).toHaveLength(1)
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent(REVIEWER.model)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('High')
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', { avatar: {}, reasoning_effort: 'high' },
    ))
  })

  it('switches model catalogs and saves the backend with cleared incompatible pins', async () => {
    const sheet = await openModelPane({})
    mockApi.models.mockImplementation(async (backend?: string) =>
      [{ model_name: backend === 'codex' ? 'codex-test-model' : 'kiro-test-model' }])
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'codex' }))
    await waitFor(() => expect(mockApi.models).toHaveBeenCalledWith('codex'))
    await within(sheet).findByRole('option', { name: 'codex-test-model' })
    expect(within(sheet).queryByRole('option', { name: 'claude-opus-5' })).not.toBeInTheDocument()
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: 'codex', model: 'auto', reasoning_effort: '' }),
    ))
  })

  it('shows the stored level on a model that reasons', async () => {
    const sheet = await openModelPane({})
    const select = within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })
    expect(select).toHaveTextContent('Max')
  })

  it('reports where the effort resolves from, not just which level', async () => {
    // The pane already did this for model. Without the same readout for effort a
    // user cannot tell "this crew pins max" from "your global default is max",
    // which are different facts with different blast radius.
    const sheet = await openModelPane({})
    const line = await waitFor(() => within(sheet).getByText(/Thinking at Max/))
    expect(line.parentElement).toHaveTextContent('chosen for this agent')
  })

  it.each([
    ['AI app', 'Claude Code', 'Kiro CLI'],
    ['Edit default model', 'claude-haiku-4.5', REVIEWER.model],
    ['Edit reasoning effort', 'High', 'Max'],
  ])('hides the saved readout while %s is dirty and restores it on return', async (label, changed, opening) => {
    const sheet = await openModelPane({ acp_backend: '' })
    await within(sheet).findByText(`Using ${REVIEWER.model}`)
    expect(within(sheet).getByText('Thinking at Max')).toBeInTheDocument()
    fireEvent.click(selectScope(sheet, label).getByRole('option', { name: opening }))
    expect(within(sheet).getByText(`Using ${REVIEWER.model}`)).toBeInTheDocument()

    fireEvent.click(selectScope(sheet, label).getByRole('option', { name: changed }))
    expect(within(sheet).queryByText(`Using ${REVIEWER.model}`)).not.toBeInTheDocument()
    expect(within(sheet).queryByText(/Thinking at/)).not.toBeInTheDocument()
    fireEvent.click(selectScope(sheet, label).getByRole('option', { name: opening }))
    expect(within(sheet).getByText(`Using ${REVIEWER.model}`)).toBeInTheDocument()
    expect(within(sheet).getByText('Thinking at Max')).toBeInTheDocument()

    // An unrelated unsaved pane does not make the execution readout stale.
    fireEvent.click(within(sheet).getByTestId('crew-rail-routing'))
    fireEvent.change(within(sheet).getByRole('textbox', { name: 'Triggers' }), { target: { value: 'review' } })
    fireEvent.click(within(sheet).getByTestId('crew-rail-model'))
    expect(within(sheet).getByText(`Using ${REVIEWER.model}`)).toBeInTheDocument()
  })

  it('restores the saved readout with the new resolution after saving and reopening', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const sheet = await openModelPane({ acp_backend: '' }, queryClient)
    await within(sheet).findByText(`Using ${REVIEWER.model}`)
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    expect(within(sheet).queryByText(/Thinking at/)).not.toBeInTheDocument()
    const savedRoster = {
      agents: [{ ...REVIEWER, acp_backend: '', reasoning_effort: 'high' }],
      default_agent: 'kirocrew',
    }
    mockApi.kirocrewAgents.mockResolvedValue(savedRoster)
    mockApi.agentResolvedModel.mockResolvedValue({
      model: REVIEWER.model, pinned: true, reasoning_effort: 'high', effort_pinned: true,
    })
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit agent reviewer' })).not.toBeInTheDocument())
    await waitFor(() => expect(queryClient.getQueryData(['kirocrew-agents'])).toEqual(savedRoster))
    fireEvent.click(screen.getByRole('button', { name: 'Edit agent reviewer' }))
    const reopened = await screen.findByRole('dialog', { name: 'Edit agent reviewer' })
    fireEvent.click(within(reopened).getByTestId('crew-rail-model'))
    await within(reopened).findByText('Thinking at High')
    expect(within(reopened).getByText(`Using ${REVIEWER.model}`)).toBeInTheDocument()
    expect(within(reopened).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('High')
    expect(within(reopened).getByText('Save changes')).toBeDisabled()
  })

  it('sends the level on save', async () => {
    const sheet = await openModelPane({})
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    fireEvent.click(within(sheet).getByText('Save changes'))

    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    const [, body] = mockApi.updateKirocrewAgent.mock.calls[0]
    expect(body.reasoning_effort).toBe('high')
  })

  it('sends an empty string when the pin is cleared, so clearing is a real write', async () => {
    // A skipped field would make clearing impossible: the server only writes the
    // keys the body carries.
    const sheet = await openModelPane({})
    fireEvent.click(
      selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'Inherited' }),
    )
    fireEvent.click(within(sheet).getByText('Save changes'))

    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    const [, body] = mockApi.updateKirocrewAgent.mock.calls[0]
    expect(body.reasoning_effort).toBe('')
  })

  it('hides the control on a model that takes no effort', async () => {
    mockApi.agentResolvedModel.mockResolvedValue({
      model: 'claude-haiku-4.5',
      pinned: true,
      kiro_agent: 'reviewer-agent',
      reasoning_effort: '',
      effort_pinned: false,
    })
    const sheet = await openModelPane({ model: 'claude-haiku-4.5', reasoning_effort: '' })

    expect(
      within(sheet).queryByRole('combobox', { name: 'Edit reasoning effort' }),
    ).not.toBeInTheDocument()
    // And no resolution readout either — there is nothing to resolve.
    expect(within(sheet).queryByText(/Thinking at/)).not.toBeInTheDocument()
    // But it SAYS so: an absent control with no explanation is the complaint
    // this feature started from.
    expect(
      within(sheet).getByText(/claude-haiku-4\.5 takes no reasoning effort/),
    ).toBeInTheDocument()
  })

  it('explains that a model must be chosen before an effort can be', async () => {
    // The common shape of a fresh crew: it pins no model, and nothing else pins
    // one either, so the backend chooses and no level can be applied. Silently
    // hiding the control here is what would make the feature look missing in the
    // very configuration most crews start in.
    mockApi.agentResolvedModel.mockResolvedValue({
      model: '',
      pinned: false,
      kiro_agent: 'reviewer-agent',
      reasoning_effort: '',
      effort_pinned: false,
    })
    const sheet = await openModelPane({ model: '', reasoning_effort: '' })

    expect(
      within(sheet).queryByRole('combobox', { name: 'Edit reasoning effort' }),
    ).not.toBeInTheDocument()
    expect(
      within(sheet).getByText(/No reasoning effort until a model is chosen/),
    ).toBeInTheDocument()
  })

  it.each([
    ['Inherited', 'No reasoning effort until a model is chosen: pick one above, or set a default.'],
    ['claude-haiku-4.5', 'claude-haiku-4.5 takes no reasoning effort, so there is none to set.'],
  ])('explains hidden effort for an unsaved %s model selection', async (model, explanation) => {
    mockApi.agentResolvedModel.mockResolvedValue({
      model: REVIEWER.model, pinned: true, reasoning_effort: '', effort_pinned: false,
    })
    const sheet = await openModelPane({ reasoning_effort: '' })
    await within(sheet).findByText(`Using ${REVIEWER.model}`)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Inherited')
    expect(within(sheet).queryByText(explanation)).not.toBeInTheDocument()

    fireEvent.click(selectScope(sheet, 'Edit default model').getByRole('option', { name: model }))
    expect(within(sheet).queryByRole('combobox', { name: 'Edit reasoning effort' })).not.toBeInTheDocument()
    expect(within(sheet).queryByText(/^Using /)).not.toBeInTheDocument()
    expect(within(sheet).queryByText(/^Thinking at /)).not.toBeInTheDocument()
    expect(within(sheet).getAllByText(explanation)).toHaveLength(1)
    expect(within(sheet).queryByText(/This effort is ignored|does not take a reasoning effort/)).not.toBeInTheDocument()

    fireEvent.click(selectScope(sheet, 'Edit default model').getByRole('option', { name: REVIEWER.model }))
    expect(within(sheet).queryByText(explanation)).not.toBeInTheDocument()
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Inherited')
    expect(within(sheet).getByText(`Using ${REVIEWER.model}`)).toBeInTheDocument()
    expect(within(sheet).getByText('Save changes')).toBeDisabled()

    // A compatible explicit model still offers effort in a dirty app draft.
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Kiro CLI' }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Inherited')
    expect(within(sheet).queryByText(/^Using /)).not.toBeInTheDocument()
    expect(within(sheet).queryByText(/No reasoning effort until|takes no reasoning effort/)).not.toBeInTheDocument()
  })

  it.each(['', 'max'])('does not carry inherited effort capability across AI apps (saved effort: %s)', async effort => {
    mockApi.agentResolvedModel.mockResolvedValue({
      model: REVIEWER.model, pinned: false, reasoning_effort: effort, effort_pinned: !!effort,
    })
    mockApi.models.mockImplementation(async (backend: string) =>
      [{ model_name: backend === 'codex' ? 'codex-test-model' : REVIEWER.model }])
    const sheet = await openModelPane({ acp_backend: null, model: '', reasoning_effort: effort })
    await within(sheet).findByText(`Using ${REVIEWER.model}`)
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toBeInTheDocument()

    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'codex' }))
    await selectScope(sheet, 'Edit default model').findByRole('option', { name: 'codex-test-model' })
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent('Inherited')
    if (effort) {
      // A saved effort remains clearable, but the old model cannot justify it.
      expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
      expect(within(sheet).getByText(/This effort is ignored until a model is chosen/)).toBeInTheDocument()
      expect(within(sheet).queryByText(/No reasoning effort until a model is chosen/)).not.toBeInTheDocument()
      fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'Inherited' }))
    }
    expect(within(sheet).queryByRole('combobox', { name: 'Edit reasoning effort' })).not.toBeInTheDocument()
    expect(within(sheet).getAllByText(/No reasoning effort until a model is chosen/)).toHaveLength(1)
    expect(within(sheet).queryByText(/^Using /)).not.toBeInTheDocument()
    expect(within(sheet).queryByText(/This effort is ignored until a model is chosen/)).not.toBeInTheDocument()

    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: 'Inherited (Kiro CLI)' }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent(effort ? 'Max' : 'Inherited')
    expect(within(sheet).queryByText(/This effort is ignored until a model is chosen/)).not.toBeInTheDocument()
    expect(within(sheet).queryByText(/No reasoning effort until a model is chosen/)).not.toBeInTheDocument()
    expect(within(sheet).getByText(`Using ${REVIEWER.model}`)).toBeInTheDocument()
    expect(within(sheet).getByText('Save changes')).toBeDisabled()
  })

  it.each<[string, string | null, string, string, string | null]>([
    ['', null, 'Inherited (Kiro CLI)', 'Kiro CLI', ''],
    ['', '', 'Kiro CLI', 'Inherited (Kiro CLI)', null],
    ['claude', null, 'Inherited (Claude Code)', 'Claude Code', 'claude'],
    ['claude', 'claude', 'Claude Code', 'Inherited (Claude Code)', null],
  ])('does not reuse inherited effort across same-backend selections: %s from %s', async (inheritedBackend, openingBackend, openingLabel, targetLabel, targetBackend) => {
    mockApi.kirocrewConfig.mockResolvedValue({
      agent: { acp_backend: '', member_acp_backend: inheritedBackend },
    })
    mockApi.agentResolvedModel.mockResolvedValue({
      model: REVIEWER.model, pinned: false, reasoning_effort: 'max', effort_pinned: true,
    })
    const sheet = await openModelPane({
      acp_backend: openingBackend, inherited_acp_backend: inheritedBackend,
      model: '', reasoning_effort: 'max',
    })
    await within(sheet).findByText(`Using ${REVIEWER.model}`)
    // An effort-only edit remains in the saved model's context.
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'Inherited' }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Inherited')
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'Max' }))

    // The effective app is unchanged, but an explicit foreign app can drop
    // template/global model inheritance. Do not infer that rule in the editor.
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: targetLabel }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent('Inherited')
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
    expect(within(sheet).getByText(/This effort is ignored until a model is chosen/)).toBeInTheDocument()
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'Inherited' }))
    expect(within(sheet).queryByRole('combobox', { name: 'Edit reasoning effort' })).not.toBeInTheDocument()

    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: openingLabel }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Max')
    expect(within(sheet).queryByText(/This effort is ignored until a model is chosen/)).not.toBeInTheDocument()
    expect(within(sheet).getByText('Save changes')).toBeDisabled()
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: targetLabel }))
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'Inherited' }))
    fireEvent.click(within(sheet).getByText('Save changes'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'reviewer', expect.objectContaining({ acp_backend: targetBackend, model: 'auto', reasoning_effort: '' }),
    ))
  })

  it.each<[string | null, string, string]>([
    [null, 'Inherited', 'Kiro CLI'],
    ['', 'Kiro CLI', 'Inherited'],
  ])('keeps inherited effort in the opening context but does not equate unknown and explicit routes from %s', async (openingBackend, openingLabel, targetLabel) => {
    mockApi.agentResolvedModel.mockResolvedValue({
      model: REVIEWER.model, pinned: false, reasoning_effort: '', effort_pinned: false,
    })
    const sheet = await openModelPane({
      acp_backend: openingBackend, inherited_acp_backend: undefined, model: '', reasoning_effort: '',
    })
    await within(sheet).findByText(`Using ${REVIEWER.model}`)
    // Older rosters omit the route. Their saved resolution still applies until
    // the context changes, including while an effort-only draft is being edited.
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'High' }))
    fireEvent.click(selectScope(sheet, 'Edit reasoning effort').getByRole('option', { name: 'Inherited' }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Inherited')
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: targetLabel }))
    expect(within(sheet).queryByRole('combobox', { name: 'Edit reasoning effort' })).not.toBeInTheDocument()
    fireEvent.click(selectScope(sheet, 'AI app').getByRole('option', { name: openingLabel }))
    expect(within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' })).toHaveTextContent('Inherited')
    expect(within(sheet).getByText('Save changes')).toBeDisabled()
  })

  it('keeps the control, and says the pin is ignored, when a stored pin cannot apply', async () => {
    // Reachable by pinning a level and then switching the model: hiding the
    // select outright would strand the value with no way to clear it, and saying
    // nothing would leave a stored level that never takes effect looking active.
    mockApi.agentResolvedModel.mockResolvedValue({
      model: 'claude-haiku-4.5',
      pinned: true,
      kiro_agent: 'reviewer-agent',
      reasoning_effort: 'max',
      effort_pinned: true,
    })
    const sheet = await openModelPane({ model: 'claude-haiku-4.5', reasoning_effort: 'max' })

    expect(
      within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' }),
    ).toBeInTheDocument()
    expect(
      within(sheet).getByText(/claude-haiku-4\.5 does not take a reasoning effort/),
    ).toBeInTheDocument()
    // The warning owns this state; the readout must not repeat it in weaker words
    // ("there is none to set" would also be false — one IS set, it is ignored).
    expect(
      within(sheet).queryByText(/takes no reasoning effort, so there is none to set/),
    ).not.toBeInTheDocument()
  })

  it('does not claim "Inherited" is a model that takes no effort', async () => {
    // Reachable by pinning an effort and then clearing the model: the stranded-pin
    // warning used to substitute the "Inherited" LABEL for {{model}}, producing a
    // sentence that names no model and states nothing true.
    mockApi.agentResolvedModel.mockResolvedValue({
      model: '',
      pinned: false,
      kiro_agent: 'reviewer-agent',
      reasoning_effort: 'max',
      effort_pinned: true,
    })
    const sheet = await openModelPane({ model: '', reasoning_effort: 'max' })

    // The select stays, so the stranded pin can still be cleared.
    expect(
      within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' }),
    ).toBeInTheDocument()
    expect(
      within(sheet).getByText(/This effort is ignored until a model is chosen/),
    ).toBeInTheDocument()
    expect(within(sheet).queryByText(/Inherited does not take/)).not.toBeInTheDocument()
  })

  it('stops trusting the saved resolution once the model pin is cleared', async () => {
    // `resolved` describes the SAVED state. A crew that pins claude-opus-5 resolves
    // to it, so switching the select to Inherited and reading resolved.model would
    // keep offering an effort control on the strength of a model the crew is about
    // to stop using -- and the level would be dropped at spawn if the inherit chain
    // lands somewhere that takes none. Until the write happens nothing here can
    // know, so the pane must report unresolved.
    const sheet = await openModelPane({})
    expect(
      within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' }),
    ).toBeInTheDocument()

    fireEvent.click(selectScope(sheet, 'Edit default model').getByRole('option', { name: 'Inherited' }))

    await waitFor(() =>
      expect(
        within(sheet).getByText(/This effort is ignored until a model is chosen/),
      ).toBeInTheDocument(),
    )
    // Not the "that model takes none" sentence: no model is named, because none
    // is known.
    expect(within(sheet).queryByText(/claude-opus-5 does not take/)).not.toBeInTheDocument()
  })

  it('follows the PENDING model pick, not the saved one', async () => {
    // The gate has to move with the select: choosing Haiku and only then being
    // told the effort no longer applies is one save too late.
    const sheet = await openModelPane({})
    expect(
      within(sheet).getByRole('combobox', { name: 'Edit reasoning effort' }),
    ).toBeInTheDocument()

    const modelPane = selectScope(sheet, 'Edit default model')
    fireEvent.click(modelPane.getByRole('option', { name: 'claude-haiku-4.5' }))

    await waitFor(() =>
      expect(
        within(sheet).getByText(/claude-haiku-4\.5 does not take a reasoning effort/),
      ).toBeInTheDocument(),
    )
    expect(within(sheet).queryByText(/takes no reasoning effort, so there is none to set/)).not.toBeInTheDocument()
  })
})
