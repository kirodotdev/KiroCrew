// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { kirocrewConfigMock, kirocrewAgentsMock, agentResolvedModelMock, updateKirocrewAgentMock, patchConfigMock } = vi.hoisted(() => ({
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ agent: { model: 'claude-opus-5.5', reasoning_effort: 'medium' } })
  ),
  kirocrewAgentsMock: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'default' })),
  agentResolvedModelMock: vi.fn(() => Promise.resolve({
    model: 'claude-opus-5.5', pinned: false, reasoning_effort: 'medium', effort_pinned: false,
  })),
  updateKirocrewAgentMock: vi.fn(() => Promise.resolve({ ok: true })),
  patchConfigMock: vi.fn(() => Promise.resolve({})),
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
      { model_name: 'claude-haiku-5', description: 'Haiku 5' },
    ]),
    patchConfig: patchConfigMock,
    listApps: () => Promise.resolve([]),
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
  return qc
}

/** The roster's view of the default agent: its own effort pin, if any. */
const seedAgent = (reasoning_effort: string, name = 'default') =>
  kirocrewAgentsMock.mockImplementation(() => Promise.resolve({
    agents: [{ name, model: '', reasoning_effort }],
    default_agent: name,
  }) as never)

/** The backend's verdict on the effort a new chat on the default agent starts
 *  at, and whether the agent's own record pins it. */
const seedResolved = (
  reasoning_effort: string | undefined,
  effort_pinned = false,
  model = 'claude-opus-5.5',
) =>
  agentResolvedModelMock.mockImplementation(() => Promise.resolve({
    model, pinned: false,
    ...(reasoning_effort === undefined ? {} : { reasoning_effort, effort_pinned }),
  }) as never)

const CLEAR_BUTTON = { name: "Clear the agent's effort pin" }

/** The notice sits behind a chained query: config + roster load, the roster's
 *  success enables the resolver read, and only its answer renders the notice.
 *  Every wait on that chain gets this explicit deadline. */
const CHAIN = { timeout: 5000 }

async function settled() {
  const trigger = await screen.findByRole('combobox', { name: 'Default Reasoning Effort' }, CHAIN)
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'), CHAIN)
  await waitFor(() => expect(kirocrewAgentsMock).toHaveBeenCalled(), CHAIN)
  // Chain: roster success -> resolver read. Wait for the call, then for its
  // answer, so an absent notice below is the verdict, not a read in flight.
  await waitFor(() => expect(agentResolvedModelMock).toHaveBeenCalled(), CHAIN)
  await act(async () => { await agentResolvedModelMock.mock.results.at(-1)?.value })
}

/**
 * The effort counterpart of the Default Model pin notice
 * (ChatPanel.agentModelPin.test.tsx): a new chat on the default agent starts at
 * the agent's own effort pin, then the role-aware default, before this setting.
 * The panel reads the same resolver and names the winner under the select.
 */
describe('ChatPanel — default agent reasoning-effort pin notice', () => {
  beforeEach(() => {
    updateKirocrewAgentMock.mockClear()
    updateKirocrewAgentMock.mockImplementation(() => Promise.resolve({ ok: true }) as never)
    patchConfigMock.mockClear()
    patchConfigMock.mockImplementation(() => Promise.resolve({}) as never)
    kirocrewAgentsMock.mockClear()
    agentResolvedModelMock.mockClear()
    seedAgent('')
    seedResolved('medium')
  })

  it('names the agent\'s own effort pin and offers to clear it', async () => {
    seedAgent('max')
    seedResolved('max', true)
    wrap()
    await settled()
    const notice = await screen.findByTestId('agent-effort-pin-notice', {}, CHAIN)
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice).toHaveTextContent(
      'New chats on the default agent use its own pinned reasoning effort Max, not this setting. You can pin an effort again later under Customize → Crewmates.'
    )
    expect(screen.getByRole('button', CLEAR_BUTTON)).toBeEnabled()
    // The model notice is a separate row and stays hidden: the model matches.
    expect(screen.queryByTestId('agent-model-pin-notice')).toBeNull()
  })

  it('clears only the effort pin, on the roster\'s default agent', async () => {
    seedAgent('max', 'oncall')
    seedResolved('max', true)
    wrap()
    await settled()
    expect(agentResolvedModelMock).toHaveBeenCalledWith('oncall')
    const before = agentResolvedModelMock.mock.calls.length
    fireEvent.click(await screen.findByRole('button', CLEAR_BUTTON, CHAIN))
    await waitFor(
      () => expect(updateKirocrewAgentMock).toHaveBeenCalledWith('oncall', { reasoning_effort: '' }),
      CHAIN,
    )
    // The model pin is not touched, and the resolver is asked again.
    expect(updateKirocrewAgentMock).not.toHaveBeenCalledWith('oncall', expect.objectContaining({ model: '' }))
    await waitFor(() => expect(agentResolvedModelMock.mock.calls.length).toBeGreaterThan(before), CHAIN)
  })

  it('names the Background Effort row, with no button, when the agent pins nothing', async () => {
    // An unpinned agent bound to a background worker takes the Background role
    // effort, which this page sets a few rows down.
    kirocrewConfigMock.mockImplementationOnce(() => Promise.resolve({
      agent: { model: 'claude-opus-5.5', reasoning_effort: 'medium', role_efforts: { background: 'low' } },
    }) as never)
    seedResolved('low', false)
    wrap()
    await settled()
    const notice = await screen.findByTestId('agent-effort-pin-notice', {}, CHAIN)
    expect(notice).toHaveTextContent(
      'New chats on the default agent use Low, not this setting. The agent runs as a background worker, so it takes the Background Effort setting below. To change it, set a different Background Effort.'
    )
    expect(screen.queryByRole('button', CLEAR_BUTTON)).toBeNull()
  })

  it('names a pin that replaces Model default too', async () => {
    // '' is a real choice ("Model default"), unlike the model's Auto.
    kirocrewConfigMock.mockImplementationOnce(() =>
      Promise.resolve({ agent: { model: 'claude-opus-5.5', reasoning_effort: '' } }) as never
    )
    seedAgent('high')
    seedResolved('high', true)
    wrap()
    await settled()
    expect(await screen.findByTestId('agent-effort-pin-notice', {}, CHAIN)).toHaveTextContent('its own pinned reasoning effort High')
  })

  it('labels a resolved model default by the select\'s own word for it', async () => {
    seedResolved('', false)
    wrap()
    await settled()
    expect(await screen.findByTestId('agent-effort-pin-notice', {}, CHAIN)).toHaveTextContent('use Model default, not this setting. The agent runs as a background worker')
  })

  it('names no source it cannot vouch for when an unpinned answer is not the Background value', async () => {
    // Background Effort is unset (''), yet the resolver answers Low with no
    // agent pin: the panel cannot say where that came from, so it says nothing.
    seedResolved('low', false)
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-effort-pin-notice')).toBeNull()
  })

  it('is hidden when the agent resolves to this setting', async () => {
    seedResolved('medium', false)
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-effort-pin-notice')).toBeNull()
  })

  it('is hidden when the gateway predates the effort fields', async () => {
    seedResolved(undefined)
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-effort-pin-notice')).toBeNull()
  })

  it('is hidden when the agent starts on a model that takes no effort', async () => {
    seedAgent('max')
    seedResolved('max', true, 'claude-haiku-5')
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-effort-pin-notice')).toBeNull()
  })

  it('offers no clear when the roster does not carry the pin the resolver named', async () => {
    // The roster is a separate read; erasing a pin it does not show could erase
    // the wrong value, so the button waits for the two to agree.
    seedAgent('high')
    seedResolved('max', true)
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-effort-pin-notice')).toBeNull()
    expect(screen.queryByRole('button', CLEAR_BUTTON)).toBeNull()
  })

  it('holds the clear button while a new default effort is still being saved', async () => {
    seedAgent('max')
    seedResolved('max', true)
    wrap()
    await settled()
    const button = await screen.findByRole('button', CLEAR_BUTTON, CHAIN)
    expect(button).toBeEnabled()
    let release!: () => void
    patchConfigMock.mockImplementationOnce(() => new Promise(resolve => {
      release = () => resolve({})
    }) as never)
    fireEvent.click(screen.getByRole('combobox', { name: 'Default Reasoning Effort' }))
    fireEvent.click(screen.getAllByRole('option', { name: 'High' }).filter(o => !o.closest('nav'))[0])
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.reasoning_effort', 'high'), CHAIN)
    await waitFor(() => expect(screen.getByRole('button', CLEAR_BUTTON)).toBeDisabled(), CHAIN)
    expect(updateKirocrewAgentMock).not.toHaveBeenCalled()
    release()
    await waitFor(() => expect(screen.getByRole('button', CLEAR_BUTTON)).toBeEnabled(), CHAIN)
  })

  it('re-asks the resolver after the default effort is saved', async () => {
    wrap()
    await settled()
    const before = agentResolvedModelMock.mock.calls.length
    fireEvent.click(screen.getByRole('combobox', { name: 'Default Reasoning Effort' }))
    fireEvent.click(screen.getAllByRole('option', { name: 'High' }).filter(o => !o.closest('nav'))[0])
    await waitFor(() => expect(agentResolvedModelMock.mock.calls.length).toBeGreaterThan(before), CHAIN)
  })

  it('re-asks the resolver after the Background effort is saved', async () => {
    wrap()
    await settled()
    const before = agentResolvedModelMock.mock.calls.length
    fireEvent.click(screen.getByRole('combobox', { name: 'Background Effort' }))
    fireEvent.click(screen.getAllByRole('option', { name: 'High' }).filter(o => !o.closest('nav'))[0])
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.role_efforts.background', 'high'), CHAIN)
    await waitFor(() => expect(agentResolvedModelMock.mock.calls.length).toBeGreaterThan(before), CHAIN)
  })

  it('reports a rejected clear beside the button and clears it on retry', async () => {
    updateKirocrewAgentMock.mockImplementationOnce(() => Promise.reject(new Error('agent is read-only')) as never)
    seedAgent('max')
    seedResolved('max', true)
    wrap()
    await settled()
    const notice = await screen.findByTestId('agent-effort-pin-notice', {}, CHAIN)
    fireEvent.click(screen.getByRole('button', CLEAR_BUTTON))
    const err = await screen.findByTestId('agent-effort-pin-clear-error', {}, CHAIN)
    expect(err).toHaveAttribute('role', 'alert')
    expect(err).toHaveTextContent("Couldn't clear the agent's pinned reasoning effort (agent is read-only). Try again.")
    expect(notice).toContainElement(err)
    fireEvent.click(screen.getByRole('button', CLEAR_BUTTON))
    await waitFor(() => expect(updateKirocrewAgentMock).toHaveBeenCalledTimes(2), CHAIN)
    await waitFor(() => expect(screen.queryByTestId('agent-effort-pin-clear-error')).toBeNull(), CHAIN)
  })

  it('holds the clear button when a re-read fails and only the old answer is left', async () => {
    seedAgent('max')
    seedResolved('max', true)
    const qc = wrap()
    await settled()
    expect(await screen.findByRole('button', CLEAR_BUTTON, CHAIN)).toBeEnabled()
    agentResolvedModelMock.mockImplementationOnce(() => Promise.reject(new Error('502')) as never)
    await qc.invalidateQueries({ queryKey: ['resolved-model'] })
    expect(await screen.findByText('Failed to load config.', {}, CHAIN)).toBeInTheDocument()
    expect(screen.getByRole('button', CLEAR_BUTTON)).toBeDisabled()
  })
})
