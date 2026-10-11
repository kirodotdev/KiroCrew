// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks —
// use the same lightweight mock the SettingsSelect unit tests use so options
// are real role="option" nodes.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))

import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock, modelsMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } })
  ),
  // `display_name` is what Qoder sends; the adapter maps it to `label` only
  // when it differs from the id, so the first and third rows carry no label.
  modelsMock: vi.fn(() =>
    Promise.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'qfmodel', display_name: 'Qwen3.8-Flash', description: '', advertised_models_exclusive: true },
      { model_name: 'claude-opus-4.8', description: 'Opus' },
    ])
  ),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more', model_picker_hidden_models: [], model_picker_configured: false }),
    kirocrewConfig: kirocrewConfigMock,
    models: modelsMock,
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
    featureVideoStatus: () => Promise.resolve({
      enabled: true, download_enabled: false, release: 'r1',
      cached: 0, total: 0, downloading: null,
    }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

function wrap(ui: React.ReactElement, sub = 'models') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter initialEntries={[`/settings?tab=chat&sub=${sub}`]}><Provider store={createTestStore()}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider></MemoryRouter>)
}

const seed = (agent: Record<string, unknown>) =>
  kirocrewConfigMock.mockImplementation(() => Promise.resolve({ agent }) as never)

/** Open a SettingsSelect by label and return its option nodes. */
async function openSelect(label: string) {
  const trigger = await screen.findByRole('combobox', { name: label })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  return screen.getAllByRole('option').filter(o => !o.closest('nav'))
}

describe('ChatPanel — model labels', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    seed({ model: 'claude-opus-4.8', reasoning_effort: '' })
  })

  it('shows the backend label in the default-model options and trigger', async () => {
    seed({ model: 'qfmodel', reasoning_effort: '' })
    wrap(<ChatPanel />)
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Default Model' })).toHaveTextContent('Qwen3.8-Flash')
    )
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const opts = await openSelect('Default Model')
    const labels = opts.map(o => o.textContent)
    expect(labels).toContain('Qwen3.8-Flash')
    expect(labels).not.toContain('qfmodel')
  })

  it('stores the id, not the label, when the labelled model is picked', async () => {
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect('Default Model')
    fireEvent.click(screen.getByRole('option', { name: 'Qwen3.8-Flash' }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.model', 'qfmodel')
    )
  })

  it('keeps showing the id for a row without a label', async () => {
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const opts = await openSelect('Default Model')
    expect(opts.map(o => o.textContent)).toContain('claude-opus-4.8')
  })

  it('labels the selectable-models rows and still finds a row by its id', async () => {
    wrap(<ChatPanel />)
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(trigger).not.toBeDisabled())
    fireEvent.click(trigger)
    expect(screen.getByRole('checkbox', { name: 'Qwen3.8-Flash' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'claude-opus-4.8' })).toBeInTheDocument()

    fireEvent.change(screen.getByRole('textbox', { name: 'Search models…' }), { target: { value: 'qfmodel' } })
    const rows = document.querySelectorAll('[data-multi-select-option]')
    expect(rows).toHaveLength(1)
    expect(rows[0]).toHaveTextContent('Qwen3.8-Flash')
  })
})
