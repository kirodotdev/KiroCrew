// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { kirocrewConfigMock, kirocrewAgentsMock, agentResolvedModelMock } = vi.hoisted(() => ({
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ agent: { model: 'claude-opus-5.5', reasoning_effort: '' } })
  ),
  kirocrewAgentsMock: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'default' })),
  agentResolvedModelMock: vi.fn(() => Promise.resolve({ model: 'claude-opus-5.5', pinned: false })),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    kirocrewConfig: kirocrewConfigMock,
    kirocrewAgents: kirocrewAgentsMock,
    agentResolvedModel: agentResolvedModelMock,
    updateKirocrewAgent: () => Promise.resolve({ ok: true }),
    // `display_name` is what Qoder sends; the adapter maps it to `label`.
    models: () => Promise.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'qfmodel', display_name: 'Qwen3.8-Flash', description: '', advertised_models_exclusive: true },
      { model_name: 'claude-opus-5.5', description: 'Opus 5.5' },
    ]),
    patchConfig: () => Promise.resolve({}),
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

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<MemoryRouter initialEntries={['/settings?tab=chat&sub=models']}><Provider store={createTestStore()}><QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider></Provider></MemoryRouter>)
}

/** The roster's view of the default agent: its own pin. */
const seedAgent = (model: string) =>
  kirocrewAgentsMock.mockImplementation(() => Promise.resolve({
    agents: [{ name: 'default', model }],
    default_agent: 'default',
  }) as never)

/** The backend's verdict: the model a new chat on the default agent starts on. */
const seedResolved = (model: string) =>
  agentResolvedModelMock.mockImplementation(() => Promise.resolve({ model, pinned: true }) as never)

describe('ChatPanel — pin notice model label', () => {
  beforeEach(() => {
    kirocrewAgentsMock.mockClear()
    agentResolvedModelMock.mockClear()
  })

  it('shows the backend label for the pinned model and keeps the id in the title', async () => {
    seedAgent('qfmodel')
    seedResolved('qfmodel')
    wrap()
    // The notice depends on the agent roster, its resolved-model query, and the
    // separate model-label list; chain-query waits get an explicit bound.
    const notice = await screen.findByTestId('agent-model-pin-notice', undefined, { timeout: 5000 })
    await waitFor(() => expect(notice).toHaveTextContent('its own pinned model Qwen3.8-Flash'), { timeout: 5000 })
    expect(notice.querySelector('[title="qfmodel"]')).toHaveTextContent('Qwen3.8-Flash')
    expect(notice).not.toHaveTextContent('qfmodel')
  })

  it('shows the raw id when the pinned model is not in the advertised list', async () => {
    seedAgent('retired-model')
    seedResolved('retired-model')
    wrap()
    // Same roster -> resolved-model -> model-list chain as the labelled case.
    const notice = await screen.findByTestId('agent-model-pin-notice', undefined, { timeout: 5000 })
    await waitFor(() => expect(notice).toHaveTextContent('its own pinned model retired-model'), { timeout: 5000 })
    expect(notice.querySelector('[title="retired-model"]')).toHaveTextContent('retired-model')
  })
})
