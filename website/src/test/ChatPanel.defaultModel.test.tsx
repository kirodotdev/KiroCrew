// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks —
// use the same lightweight mock the SettingsSelect unit tests use so options
// are real role="option" nodes.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock, modelsMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } })
  ),
  modelsMock: vi.fn(() =>
    Promise.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'claude-opus-4.8', description: 'Opus' },
      { model_name: 'claude-haiku-4.5', description: 'Haiku' },
    ])
  ),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    kirocrewConfig: kirocrewConfigMock,
    models: modelsMock,
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const seed = (agent: Record<string, unknown>) =>
  kirocrewConfigMock.mockImplementation(() => Promise.resolve({ agent }) as never)

/** Open a SettingsSelect by label and return its option nodes.
 *  Waits for the control to leave its loading-disabled state first — the
 *  trigger exists (inert) while the config query is still in flight. */
async function openSelect(label: string) {
  const trigger = await screen.findByRole('combobox', { name: label })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  return screen.getAllByRole('option')
}

/** Assert a SettingsSelect is inert: it stays closed when clicked. */
async function expectSelectInert(label: string) {
  const trigger = await screen.findByRole('combobox', { name: label })
  await waitFor(() => expect(trigger).toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  expect(screen.queryAllByRole('option')).toHaveLength(0)
  return trigger
}

describe('ChatPanel — default model', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    seed({ model: 'auto', reasoning_effort: '' })
  })

  it('renders the Model section with both controls', async () => {
    wrap(<ChatPanel />)
    expect(await screen.findByText('Model')).toBeInTheDocument()
    expect(await screen.findByRole('combobox', { name: 'Fallback Model' })).toBeInTheDocument()
    expect(
      await screen.findByRole('combobox', { name: 'Default Reasoning Effort' })
    ).toBeInTheDocument()
  })

  it('lists the models the backend advertises', async () => {
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const opts = await openSelect('Fallback Model')
    const labels = opts.map(o => o.textContent)
    expect(labels).toContain('Default (auto)')
    expect(labels).toContain('claude-opus-4.8')
  })

  it('PATCHes agent.model on selection', async () => {
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect('Fallback Model')
    fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.model', 'claude-opus-4.8')
    )
  })

  it('shows the stored model in the trigger', async () => {
    seed({ model: 'claude-opus-4.8', reasoning_effort: '' })
    wrap(<ChatPanel />)
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Fallback Model' })).toHaveTextContent(
        'claude-opus-4.8'
      )
    )
  })

  it('keeps a stored model selectable when the backend no longer lists it', async () => {
    // A model that dropped off /api/models must stay in the option list —
    // otherwise the select shows a foreign value and a stray change event
    // would silently overwrite the user's stored choice.
    seed({ model: 'claude-opus-4.7-retired', reasoning_effort: '' })
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const opts = await openSelect('Fallback Model')
    expect(opts.map(o => o.textContent)).toContain('claude-opus-4.7-retired')
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('treats an empty stored model as the auto default', async () => {
    seed({ model: '', reasoning_effort: '' })
    wrap(<ChatPanel />)
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Fallback Model' })).toHaveTextContent(
        'Default (auto)'
      )
    )
  })

  it('surfaces an error banner when the write fails', async () => {
    patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect('Fallback Model')
    fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))
    expect(await screen.findByText(/Failed to save default model/)).toBeInTheDocument()
  })
})

describe('ChatPanel — default reasoning effort', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    seed({ model: 'claude-opus-4.8', reasoning_effort: '' })
  })

  it('offers the model-default sentinel plus every concrete level', async () => {
    wrap(<ChatPanel />)
    const opts = await openSelect('Default Reasoning Effort')
    expect(opts.map(o => o.textContent)).toEqual([
      'Model default',
      'Low',
      'Medium',
      'High',
      'Extra High',
      'Max',
    ])
  })

  it('PATCHes agent.reasoning_effort on selection', async () => {
    wrap(<ChatPanel />)
    await openSelect('Default Reasoning Effort')
    fireEvent.click(screen.getByRole('option', { name: 'Extra High' }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.reasoning_effort', 'xhigh')
    )
  })

  it('clears back to the model default with an empty value, not a sentinel', async () => {
    seed({ model: 'claude-opus-4.8', reasoning_effort: 'high' })
    wrap(<ChatPanel />)
    await openSelect('Default Reasoning Effort')
    fireEvent.click(screen.getByRole('option', { name: 'Model default' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.reasoning_effort', ''))
  })

  it('is inert when the default model cannot reason', async () => {
    // kiro-cli rejects effort on 'auto' and Haiku. The row stays visible but
    // inert with an explanatory hint, rather than vanishing.
    seed({ model: 'auto', reasoning_effort: '' })
    wrap(<ChatPanel />)
    await expectSelectInert('Default Reasoning Effort')
    expect(screen.getAllByTitle(/reasoning-capable/).length).toBeGreaterThan(0)
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('is inert for a non-reasoning concrete model too', async () => {
    seed({ model: 'claude-haiku-4.5', reasoning_effort: '' })
    wrap(<ChatPanel />)
    await expectSelectInert('Default Reasoning Effort')
    expect(screen.getAllByTitle(/reasoning-capable/).length).toBeGreaterThan(0)
  })

  it('surfaces an error banner when the write fails', async () => {
    patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
    wrap(<ChatPanel />)
    await openSelect('Default Reasoning Effort')
    fireEvent.click(screen.getByRole('option', { name: 'High' }))
    expect(await screen.findByText(/Failed to save default reasoning effort/)).toBeInTheDocument()
  })
})
