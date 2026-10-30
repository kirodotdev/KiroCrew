// The Subagents section names what a model or effort pin costs: each pinned
// subagent starts its own runtime instead of sharing the chat's.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { Provider } from 'react-redux'

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    kirocrewConfig: () => Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } }),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
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
import { createTestStore } from './helpers'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter initialEntries={['/settings?tab=chat&sub=models']}><Provider store={createTestStore()}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider></MemoryRouter>)
}

describe('ChatPanel — subagent pin cost', () => {
  it('names the per-subagent runtime cost under the Subagents heading', async () => {
    wrap(<ChatPanel />)
    const note = await screen.findByTestId('subagent-pin-runtime-cost')
    expect(note).toHaveTextContent('runs each subagent in its own process')
    expect(note).toHaveTextContent('0.55 GB')
    // It sits in the Subagents section, after that section's heading.
    const heading = screen.getByRole('heading', { name: 'Subagents' })
    expect(heading.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
