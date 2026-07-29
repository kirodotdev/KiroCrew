import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import ModelEffortDropdown from '../components/ModelEffortDropdown'
import { SETTINGS_DEFAULT_MODEL_ID } from '../hooks/useSettingHighlight'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

/**
 * The in-session model picker offers a link out to the global default-model
 * setting. Two things must hold: the link only appears when a call site opts in
 * (so pickers without a router are unaffected), and the id it deep-links to
 * still exists in the generated settings registry — otherwise the link lands on
 * Settings with no highlight.
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
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('ModelEffortDropdown — default-model link', () => {
  it('is absent when the call site passes no handler', () => {
    wrap(<ModelEffortDropdown {...baseProps} />)
    expect(screen.queryByText(/Set default for new sessions/)).toBeNull()
  })

  it('renders and fires when a handler is supplied', () => {
    const onSetDefault = vi.fn()
    wrap(<ModelEffortDropdown {...baseProps} onSetDefault={onSetDefault} />)
    const link = screen.getByText(/Set default for new sessions/)
    fireEvent.click(link)
    expect(onSetDefault).toHaveBeenCalledTimes(1)
  })

  it('coexists with the reasoning-effort footer', () => {
    wrap(<ModelEffortDropdown {...baseProps} hasEffort onSetDefault={vi.fn()} />)
    expect(screen.getByText('Reasoning')).toBeInTheDocument()
    expect(screen.getByText(/Set default for new sessions/)).toBeInTheDocument()
  })
})

describe('ModelEffortDropdown — effort footer value', () => {
  // The footer summarises what a turn WILL run at: a slot with no override
  // inherits the configured default, so showing a bare "Default" there hid the
  // real level (the reported symptom on a freshly created session). Scoped to
  // the footer row — the drill-in effort page is always mounted (off-screen)
  // and renders its own copy of the label.
  const footer = () => screen.getByRole('button', { name: /^Reasoning/ })

  it('shows the configured default when the slot carries no override', () => {
    wrap(<ModelEffortDropdown {...baseProps} hasEffort currentEffort="" defaultEffort="high" />)
    expect(footer()).toHaveTextContent('High')
  })

  it('shows the per-slot override when one is set', () => {
    wrap(<ModelEffortDropdown {...baseProps} hasEffort currentEffort="low" defaultEffort="high" />)
    expect(footer()).toHaveTextContent('Low')
    expect(footer()).not.toHaveTextContent('High')
  })

  it('falls back to "Default" when neither is set', () => {
    wrap(<ModelEffortDropdown {...baseProps} hasEffort currentEffort="" defaultEffort="" />)
    expect(footer()).toHaveTextContent('Default')
  })
})

describe('SETTINGS_DEFAULT_MODEL_ID', () => {
  it('resolves to a real entry in the generated settings registry', () => {
    // Registry ids derive from the setting's LABEL. If the "Default Model" row
    // is renamed without regenerating/updating this constant, the deep link
    // silently loses its highlight — fail here instead.
    const entry = SETTINGS_REGISTRY.find(e => e.id === SETTINGS_DEFAULT_MODEL_ID)
    expect(entry, `no registry entry for ${SETTINGS_DEFAULT_MODEL_ID}`).toBeDefined()
    expect(entry?.tab).toBe('chat')
  })
})
