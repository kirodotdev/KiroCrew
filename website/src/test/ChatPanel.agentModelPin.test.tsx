// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { kirocrewConfigMock, kirocrewAgentsMock, agentResolvedModelMock, updateKirocrewAgentMock } = vi.hoisted(() => ({
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ agent: { model: 'claude-opus-5.5', reasoning_effort: '' } })
  ),
  kirocrewAgentsMock: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'default' })),
  agentResolvedModelMock: vi.fn(() => Promise.resolve({ model: 'claude-opus-5.5', pinned: false })),
  updateKirocrewAgentMock: vi.fn(() => Promise.resolve({ ok: true })),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    kirocrewConfig: kirocrewConfigMock,
    kirocrewAgents: kirocrewAgentsMock,
    agentResolvedModel: agentResolvedModelMock,
    updateKirocrewAgent: updateKirocrewAgentMock,
    models: () => Promise.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'claude-opus-5.5', description: 'Opus 5.5' },
      { model_name: 'claude-opus-5', description: 'Opus 5' },
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
  return render(<MemoryRouter initialEntries={['/settings?tab=chat&sub=models']}><Provider store={createTestStore()}><QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider></Provider></MemoryRouter>)
}

/** The roster's view of the default agent: its own (member) pin, if any. */
const seedAgent = (model: string, extra: Record<string, unknown> = {}) =>
  kirocrewAgentsMock.mockImplementation(() => Promise.resolve({
    agents: [{ name: 'default', model, ...extra }],
    default_agent: 'default',
  }) as never)

/** The backend's verdict: the model a new chat on the default agent starts on. */
const seedResolved = (model: string) =>
  agentResolvedModelMock.mockImplementation(() => Promise.resolve({ model, pinned: false }) as never)

/** The Default Model select is rendered and has left its loading state, and
 *  both reads the notice depends on have answered. */
async function settled() {
  const trigger = await screen.findByRole('combobox', { name: 'Default Model' })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  await waitFor(() => expect(kirocrewAgentsMock).toHaveBeenCalled())
  await waitFor(() => expect(agentResolvedModelMock).toHaveBeenCalled())
}

/**
 * A new chat on the default agent starts on whatever the backend RESOLVES for
 * it (agent pin > template pin > this setting), so the select alone can look
 * saved while every new chat runs something else. The panel asks the resolver
 * and names the winner next to the select — with a way to hand the choice back
 * when the winner is the agent's own pin, and without one when its template
 * pins it (this panel cannot edit a template).
 */
describe('ChatPanel — default agent model pin notice', () => {
  beforeEach(() => {
    updateKirocrewAgentMock.mockClear()
    kirocrewAgentsMock.mockClear()
    agentResolvedModelMock.mockClear()
    seedResolved('claude-opus-5.5')
  })

  it('names the agent pin when the resolved model is the agent\'s own pin', async () => {
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5')
    wrap()
    await settled()
    const notice = await screen.findByTestId('agent-model-pin-notice')
    expect(notice).toHaveTextContent(
      'New chats on the default agent use its own pinned model claude-opus-5, not this setting.'
    )
    expect(screen.getByRole('button', { name: "Remove the agent's pin" })).toBeInTheDocument()
  })

  it('asks the resolver for the configured default agent', async () => {
    wrap()
    await settled()
    expect(agentResolvedModelMock).toHaveBeenCalledWith('')
  })

  it('uses the agent display name when one is set', async () => {
    seedAgent('claude-opus-5', { display_name: 'Kiro' })
    seedResolved('claude-opus-5')
    wrap()
    await settled()
    expect(await screen.findByTestId('agent-model-pin-notice')).toHaveTextContent('New chats on the Kiro agent use')
  })

  it('names the template pin, with no button, when the agent itself inherits', async () => {
    seedAgent('')
    seedResolved('claude-opus-5')
    wrap()
    await settled()
    const notice = await screen.findByTestId('agent-model-pin-notice')
    expect(notice).toHaveTextContent(
      "New chats on the default agent use claude-opus-5, pinned by the agent's template, not this setting."
    )
    expect(screen.queryByRole('button', { name: "Remove the agent's pin" })).toBeNull()
  })

  it('is hidden when the default agent resolves to the global default', async () => {
    seedAgent('')
    seedResolved('claude-opus-5.5')
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-model-pin-notice')).toBeNull()
  })

  it('is hidden when the resolved model is the global default under another spelling', async () => {
    // `claude-opus-4.8`, `opus` and the provider id `claude-opus-4-8[1m]` are
    // one registry entry; a raw string compare would notice here.
    kirocrewConfigMock.mockImplementationOnce(() =>
      Promise.resolve({ agent: { model: 'claude-opus-4.8', reasoning_effort: '' } }) as never
    )
    seedAgent('opus')
    seedResolved('claude-opus-4-8[1m]')
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-model-pin-notice')).toBeNull()
  })

  it('is hidden when the global default is Auto', async () => {
    // On Auto the setting itself defers to the agent config, so whatever
    // resolves is what the hint already promised.
    kirocrewConfigMock.mockImplementationOnce(() =>
      Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } }) as never
    )
    seedAgent('')
    seedResolved('claude-opus-5')
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-model-pin-notice')).toBeNull()
  })

  it('ignores a roster pin the backend did not honour', async () => {
    // A member pin the active harness cannot claim is skipped by the resolver;
    // the roster still carries it, but no new chat will use it.
    seedAgent('gpt-5')
    seedResolved('claude-opus-5.5')
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-model-pin-notice')).toBeNull()
  })

  it('reports a failed resolver read and retries it in place', async () => {
    agentResolvedModelMock.mockImplementationOnce(() => Promise.reject(new Error('502')) as never)
    seedAgent('claude-opus-5')
    wrap()
    await settled()
    expect(await screen.findByText('Failed to load config.')).toBeInTheDocument()
    const calls = agentResolvedModelMock.mock.calls.length
    fireEvent.click(screen.getAllByRole('button', { name: 'Retry' })[0])
    await waitFor(() => expect(agentResolvedModelMock.mock.calls.length).toBeGreaterThan(calls))
    await waitFor(() => expect(screen.queryByText('Failed to load config.')).toBeNull())
  })

  it('reports a failed roster read and retries it in place', async () => {
    kirocrewAgentsMock.mockImplementationOnce(() => Promise.reject(new Error('502')) as never)
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5')
    wrap()
    await settled()
    expect(await screen.findByText('Failed to load config.')).toBeInTheDocument()
    const calls = kirocrewAgentsMock.mock.calls.length
    fireEvent.click(screen.getAllByRole('button', { name: 'Retry' })[0])
    await waitFor(() => expect(kirocrewAgentsMock.mock.calls.length).toBeGreaterThan(calls))
    expect(await screen.findByTestId('agent-model-pin-notice')).toBeInTheDocument()
  })

  it('removes the agent pin so the global default applies again', async () => {
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5')
    wrap()
    await settled()
    fireEvent.click(await screen.findByRole('button', { name: "Remove the agent's pin" }))
    await waitFor(() =>
      expect(updateKirocrewAgentMock).toHaveBeenCalledWith('default', { model: '' })
    )
  })

  it('re-asks the resolver after the global default is saved', async () => {
    seedAgent('')
    seedResolved('claude-opus-5.5')
    wrap()
    await settled()
    const before = agentResolvedModelMock.mock.calls.length
    fireEvent.click(await screen.findByRole('combobox', { name: 'Default Model' }))
    fireEvent.click(screen.getAllByRole('option', { name: 'claude-opus-5' }).filter(o => !o.closest('nav'))[0])
    await waitFor(() => expect(agentResolvedModelMock.mock.calls.length).toBeGreaterThan(before))
  })
})
