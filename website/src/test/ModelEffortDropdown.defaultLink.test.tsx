import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'
import ModelEffortDropdown from '../components/ModelEffortDropdown'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { api } from '../api/client'
import { SETTINGS_DEFAULT_MODEL_ID } from '../hooks/useSettingHighlight'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

// The nested ReasoningEffortDropdown persists slider picks over the wire
// (#5120); none of these tests touch the slider, but the stub keeps an
// accidental future interaction from hitting a real fetch.
vi.spyOn(api, 'chatSlotReasoningEffort').mockResolvedValue({ ok: true } as never)

/**
 * The in-session model picker carries two footer rows: an in-place "set as
 * default for <agent>" write, and a link out to the GLOBAL fallback setting.
 * Three things must hold: each row only appears when a call site opts in (so
 * pickers without a router / without an agent in scope are unaffected), the pin
 * row reports rather than re-writes when the agent already pins the active
 * model, and the id the link deep-links to still exists in the generated
 * settings registry — otherwise the link lands on Settings with no highlight.
 */

const baseProps = {
  anchorRect: { right: 400, top: 300 } as DOMRect,
  dropdownRef: React.createRef<HTMLDivElement>(),
  inputRef: React.createRef<HTMLInputElement>(),
  models: [{ name: 'auto', description: 'Default' }, { name: 'claude-opus-4.8' }],
  activeModel: 'auto',
  onSelectModel: vi.fn(),
  filter: '',
  setFilter: vi.fn(),
  onClose: vi.fn(),
  hasEffort: false,
  slot: 'dashboard:1',
  currentEffort: '',
  onListKeyDown: vi.fn(),
}

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The nested ReasoningEffortDropdown persists picks into the slot store
  // (#5120), so the tree needs the redux context even where a test never
  // touches the effort footer. A per-render store (not the app singleton)
  // keeps one test's persist from leaking into the next.
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
}

describe('ModelEffortDropdown — visible models shortcut', () => {
  it('reports a visibility-config read failure and retries in place', () => {
    const onRetryModelVisibility = vi.fn()
    wrap(
      <ModelEffortDropdown
        {...baseProps}
        modelVisibilityError
        onRetryModelVisibility={onRetryModelVisibility}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load dashboard config.')
    expect(screen.queryByRole('button', { name: 'Ask the agent' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetryModelVisibility).toHaveBeenCalledOnce()
  })

  it('is optional and opens management from between the list and effort controls', () => {
    const onManageModels = vi.fn()
    wrap(<ModelEffortDropdown {...baseProps} hasEffort onManageModels={onManageModels} />)
    const button = screen.getByRole('button', { name: 'Manage visible models' })
    expect(screen.getByRole('listbox').compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(button.compareDocumentPosition(screen.getByRole('slider')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(button)
    expect(onManageModels).toHaveBeenCalledTimes(1)
    expect(SETTINGS_REGISTRY.some(entry => entry.configKey === 'dashboard.model_picker_hidden_models')).toBe(true)
  })

  it('stays absent after its caller marks configuration complete', () => {
    wrap(<ModelEffortDropdown {...baseProps} />)
    expect(screen.queryByRole('button', { name: 'Manage visible models' })).not.toBeInTheDocument()
  })

  it('places the first-use shortcut between models and effort in keyboard order', async () => {
    const onListKeyDown = vi.fn()
    wrap(
      <ModelEffortDropdown
        {...baseProps}
        hasEffort
        onManageModels={vi.fn()}
        onListKeyDown={onListKeyDown}
      />,
    )
    const user = userEvent.setup()
    const input = screen.getByPlaceholderText('Type to filter…')
    const manage = screen.getByRole('button', { name: 'Manage visible models' })
    const slider = screen.getByRole('slider', { name: 'Reasoning effort' })
    input.focus()
    await user.tab()
    expect(manage).toHaveFocus()
    await user.tab()
    expect(slider).toHaveFocus()

    const options = screen.getAllByRole('option')
    const last = options[options.length - 1]
    last.focus()
    fireEvent.keyDown(last, { key: 'ArrowDown' })
    expect(manage).toHaveFocus()
    fireEvent.keyDown(manage, { key: 'ArrowDown' })
    expect(slider).toHaveFocus()
    fireEvent.keyDown(manage, { key: 'ArrowUp' })
    expect(last).toHaveFocus()
    expect(onListKeyDown).not.toHaveBeenCalled()
  })
})

describe('ModelEffortDropdown — global fallback link', () => {
  it('is absent when the call site passes no handler', () => {
    wrap(<ModelEffortDropdown {...baseProps} />)
    expect(screen.queryByText(/Global default for new sessions/)).toBeNull()
  })

  it('renders and fires when a handler is supplied', () => {
    const onSetDefault = vi.fn()
    wrap(<ModelEffortDropdown {...baseProps} onSetDefault={onSetDefault} />)
    const link = screen.getByText(/Global default for new sessions/)
    fireEvent.click(link)
    expect(onSetDefault).toHaveBeenCalledTimes(1)
  })

  it('coexists with the reasoning-effort footer', () => {
    wrap(<ModelEffortDropdown {...baseProps} hasEffort onSetDefault={vi.fn()} />)
    expect(screen.getByText('Effort')).toBeInTheDocument()
    expect(screen.getByText(/Global default for new sessions/)).toBeInTheDocument()
  })
})

describe('ModelEffortDropdown — per-agent default row', () => {
  // The primary way to set a default: writes the agent's own model in place,
  // without a trip to Settings. Named for the agent so it is unambiguous which
  // scope is being changed.
  it('is absent without a handler', () => {
    wrap(<ModelEffortDropdown {...baseProps} agentName="oncall" />)
    expect(screen.queryByText(/default model for oncall/i)).toBeNull()
  })

  it('is absent without an agent in scope', () => {
    wrap(<ModelEffortDropdown {...baseProps} onPinToAgent={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /as default model for/ })).toBeNull()
  })

  it('names the agent and fires the in-place write', () => {
    const onPinToAgent = vi.fn()
    wrap(<ModelEffortDropdown {...baseProps} agentName="oncall" onPinToAgent={onPinToAgent} />)
    // Queried by ACCESSIBLE NAME, not getByText: the row interpolates the model
    // id and the agent name through <Trans> so each lands in its own mono
    // <span>, and getByText joins only an element's DIRECT text nodes — it
    // cannot see across that split. The accessible name is built from the whole
    // subtree, so it also asserts what a screen reader announces.
    fireEvent.click(screen.getByRole('button', { name: 'Set as default model for oncall' }))
    expect(onPinToAgent).toHaveBeenCalledTimes(1)
  })

  it('reports state instead of offering a no-op write when already pinned', () => {
    const onPinToAgent = vi.fn()
    wrap(
      <ModelEffortDropdown
        {...baseProps}
        agentName="oncall"
        pinnedToAgent
        onPinToAgent={onPinToAgent}
      />
    )
    const row = screen.getByRole('button', { name: 'Default model for oncall' })
    expect(screen.queryByRole('button', { name: 'Set as default model for oncall' })).toBeNull()
    fireEvent.click(row)
    expect(onPinToAgent).not.toHaveBeenCalled()
  })

  it('coexists with both other footer rows', () => {
    wrap(
      <ModelEffortDropdown
        {...baseProps}
        hasEffort
        agentName="oncall"
        onPinToAgent={vi.fn()}
        onSetDefault={vi.fn()}
      />
    )
    expect(screen.getByText('Effort')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Set as default model for oncall' })).toBeInTheDocument()
    expect(screen.getByText(/Global default for new sessions/)).toBeInTheDocument()
  })
})

describe('ModelEffortDropdown — inline effort', () => {
  it('caps the picker to the viewport and leaves the model list as the flexible scroller', () => {
    const innerHeight = window.innerHeight
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 320 })
    try {
      wrap(
        <ModelEffortDropdown
          {...baseProps}
          anchorRect={{ ...baseProps.anchorRect, top: 300 } as DOMRect}
          models={Array.from({ length: 20 }, (_, index) => ({ name: `model-${index}` }))}
          hasEffort
          onManageModels={vi.fn()}
          onSetDefault={vi.fn()}
        />,
      )
      expect(screen.getByRole('dialog')).toHaveStyle({ maxHeight: '288px' })
      expect(screen.getByRole('dialog')).toHaveClass('flex', 'flex-col', 'overflow-hidden')
      expect(screen.getByRole('listbox')).toHaveClass('min-h-0', 'flex-1', 'overflow-y-auto')
    } finally {
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: innerHeight })
    }
  })

  it('shows the configured default when the slot carries no override', () => {
    wrap(<ModelEffortDropdown {...baseProps} hasEffort currentEffort="" defaultEffort="high" />)
    expect(screen.getByText('Default · High')).toBeInTheDocument()
  })

  it('shows the per-slot override when one is set', () => {
    wrap(<ModelEffortDropdown {...baseProps} hasEffort currentEffort="low" defaultEffort="high" />)
    expect(screen.getByText('Low')).toBeInTheDocument()
  })

  it('falls back to "Default" when neither is set', () => {
    wrap(<ModelEffortDropdown {...baseProps} hasEffort currentEffort="" defaultEffort="" />)
    expect(screen.getAllByText('Default').length).toBeGreaterThan(0)
  })

  it('stays on one page with no drill-in chevron or back row and keeps model search', () => {
    wrap(<ModelEffortDropdown {...baseProps} hasEffort />)
    expect(screen.getByPlaceholderText('Type to filter…')).toBeInTheDocument()
    expect(screen.queryByText('Models')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Reasoning/ })).toBeNull()
  })

  it('leaves slider arrow keys to the inline reasoning control', () => {
    const onListKeyDown = vi.fn()
    wrap(<ModelEffortDropdown {...baseProps} hasEffort currentEffort="high" onListKeyDown={onListKeyDown} />)
    fireEvent.keyDown(screen.getByRole('slider', { name: 'Reasoning effort' }), { key: 'ArrowRight' })
    expect(onListKeyDown).not.toHaveBeenCalled()
  })

  it('tabs from the filter into the inline slider without closing the picker', async () => {
    const onListKeyDown = vi.fn()
    wrap(<ModelEffortDropdown {...baseProps} hasEffort onListKeyDown={onListKeyDown} />)
    const user = userEvent.setup()
    const input = screen.getByPlaceholderText('Type to filter…')
    input.focus()
    await user.tab()
    expect(screen.getByRole('slider', { name: 'Reasoning effort' })).toHaveFocus()
    expect(onListKeyDown).not.toHaveBeenCalled()
  })

  it('moves ArrowDown from the last model into the inline slider', () => {
    const onListKeyDown = vi.fn()
    wrap(<ModelEffortDropdown {...baseProps} hasEffort onListKeyDown={onListKeyDown} />)
    const options = screen.getAllByRole('option')
    const last = options[options.length - 1]
    last.focus()
    fireEvent.keyDown(last, { key: 'ArrowDown' })
    expect(screen.getByRole('slider', { name: 'Reasoning effort' })).toHaveFocus()
    expect(onListKeyDown).not.toHaveBeenCalled()
  })
})

describe('SETTINGS_DEFAULT_MODEL_ID', () => {
  it('resolves to a real entry in the generated settings registry', () => {
    // Registry ids derive from the setting's LABEL. If the default-model row
    // is renamed without regenerating/updating this constant, the deep link
    // silently loses its highlight — fail here instead.
    const entry = SETTINGS_REGISTRY.find(e => e.id === SETTINGS_DEFAULT_MODEL_ID)
    expect(entry, `no registry entry for ${SETTINGS_DEFAULT_MODEL_ID}`).toBeDefined()
    expect(entry?.tab).toBe('chat')
  })
})
/**
 * The other half of the same classification: the pin row is a SENTENCE with two
 * interpolated identifiers. Prose follows the Font Family setting; the model id
 * and the agent name are verbatim identifiers and stay monospace — which is also
 * what disambiguates "Set claude-opus-5 as default model for default", where the
 * trailing word is an agent NAME and not the English adjective.
 */
describe('pin row monospaces only its two identifiers', () => {
  function pinRow(props: Record<string, unknown> = {}) {
    wrap(<ModelEffortDropdown
      {...baseProps}
      agentName="oncall"
      pinModelName="claude-opus-5"
      onPinToAgent={vi.fn()}
      {...props}
    />)
    return screen.getByRole('button', { name: /default model for/i })
  }

  it('puts the model id and the agent name in mono, and nothing else', () => {
    const row = pinRow()
    const mono = Array.from(row.querySelectorAll('.font-mono')).map(e => e.textContent)
    expect(mono).toEqual(['claude-opus-5', 'oncall'])
    // The sentence around them must NOT be mono, or the whole row would ignore
    // the Font Family setting again.
    expect(row.querySelector('span')?.className).not.toContain('font-mono')
    expect(row.textContent).toBe('Set claude-opus-5 as default model for oncall')
  })

  it('monospaces the agent name in the already-pinned state too', () => {
    const row = pinRow({ pinnedToAgent: true })
    expect(Array.from(row.querySelectorAll('.font-mono')).map(e => e.textContent)).toEqual(['oncall'])
  })

  it('monospaces the model id in the unavailable state too', () => {
    // Same value reaches this branch, so styling only the other two states would
    // leave one row rendering a bare id in prose type.
    const row = wrap(<ModelEffortDropdown
      {...baseProps} agentName="oncall" pinModelName="claude-opus-5"
      onPinToAgent={vi.fn()} pinModelUnavailable
    />) && screen.getByRole('button', { name: /claude-opus-5/ })
    expect(Array.from(row.querySelectorAll('.font-mono')).map(e => e.textContent)).toEqual(['claude-opus-5'])
  })

  it('wraps instead of truncating so a long locale keeps the agent name', () => {
    // Both identifiers sit at the ends of the sentence, so an ellipsis removes
    // exactly the part the label exists to communicate. es/fr/it need 8-14 more
    // characters than English for "default model", so this is load-bearing.
    const label = pinRow().querySelector('span')
    expect(label?.className).toContain('min-w-0')
    expect(label?.className).not.toContain('truncate')
  })
})
