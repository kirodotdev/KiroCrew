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
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
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
            key={o || 'inherit'}
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
}

function renderPage() {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
async function openModelPane(crew: Record<string, unknown>): Promise<HTMLElement> {
  mockApi.kirocrewAgents.mockResolvedValue({
    agents: [{ ...REVIEWER, ...crew }],
    default_agent: 'kirocrew',
  })
  renderPage()
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
  vi.clearAllMocks()
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
  mockApi.createKirocrewAgent.mockResolvedValue({})
  mockApi.updateKirocrewAgent.mockResolvedValue({})
  mockApi.deleteKirocrewAgent.mockResolvedValue({})
  mockApi.setDefaultAgent.mockResolvedValue({})
})

describe('crew editor — reasoning effort pin', () => {
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
  })
})
