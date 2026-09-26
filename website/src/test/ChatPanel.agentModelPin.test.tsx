// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { kirocrewConfigMock, kirocrewAgentsMock, agentResolvedModelMock, updateKirocrewAgentMock, patchConfigMock } = vi.hoisted(() => ({
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ agent: { model: 'claude-opus-5.5', reasoning_effort: '' } })
  ),
  kirocrewAgentsMock: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'default' })),
  agentResolvedModelMock: vi.fn(() => Promise.resolve({ model: 'claude-opus-5.5', pinned: false })),
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
      { model_name: 'claude-opus-5', description: 'Opus 5' },
      { model_name: 'claude-sonnet-5', description: 'Sonnet 5' },
    ]),
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

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<MemoryRouter initialEntries={['/settings?tab=chat&sub=models']}><Provider store={createTestStore()}><QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider></Provider></MemoryRouter>)
  return qc
}

/** The roster's view of the default agent: its own (member) pin, if any. */
const seedAgent = (model: string, extra: Record<string, unknown> = {}, name = 'default') =>
  kirocrewAgentsMock.mockImplementation(() => Promise.resolve({
    agents: [{ name, model, ...extra }],
    default_agent: name,
  }) as never)

/** The backend's verdict: the model a new chat on the default agent starts on,
 *  and whether that agent's own record pins it. */
const seedResolved = (model: string, pinned = false) =>
  agentResolvedModelMock.mockImplementation(() => Promise.resolve({ model, pinned }) as never)

const CLEAR_BUTTON = { name: 'Clear its pin' }

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
    patchConfigMock.mockClear()
    patchConfigMock.mockImplementation(() => Promise.resolve({}) as never)
    kirocrewAgentsMock.mockClear()
    agentResolvedModelMock.mockClear()
    seedResolved('claude-opus-5.5')
  })

  it('names the agent pin when the resolved model is the agent\'s own pin', async () => {
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5', true)
    wrap()
    await settled()
    const notice = await screen.findByTestId('agent-model-pin-notice')
    expect(notice).toHaveTextContent(
      'New chats on the default agent use its own pinned model claude-opus-5, not this setting. You can pin a model again later from the chat model picker.'
    )
    // The button says what it does and names no successor model: clearing hands
    // new chats to the next tier down, which may be the agent's template rather
    // than the global default, and the panel cannot tell which in advance.
    const button = screen.getByRole('button', CLEAR_BUTTON)
    expect(button).toBeEnabled()
    expect(button).not.toHaveTextContent('claude-opus-5.5')
  })

  it('asks the resolver for the roster\'s default agent, by name, once the roster has named it', async () => {
    seedAgent('claude-opus-5', {}, 'oncall')
    seedResolved('claude-opus-5', true)
    wrap()
    await settled()
    // Never an unnamed ask: that would let the server answer for a different
    // agent than the one the clear button writes to.
    expect(agentResolvedModelMock).not.toHaveBeenCalledWith('')
    expect(agentResolvedModelMock).toHaveBeenCalledWith('oncall')
    expect(agentResolvedModelMock.mock.invocationCallOrder[0]).toBeGreaterThan(
      kirocrewAgentsMock.mock.invocationCallOrder[0]
    )
    fireEvent.click(await screen.findByRole('button', CLEAR_BUTTON))
    await waitFor(() =>
      expect(updateKirocrewAgentMock).toHaveBeenCalledWith('oncall', { model: '' })
    )
  })

  it('holds the clear button while the roster or the resolver is being re-read', async () => {
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5', true)
    const qc = wrap()
    await settled()
    const button = await screen.findByRole('button', CLEAR_BUTTON)
    expect(button).toBeEnabled()
    // A roster re-read that has not answered yet: the default agent may be
    // about to change under the button, so it waits.
    let release!: () => void
    kirocrewAgentsMock.mockImplementationOnce(() => new Promise(resolve => {
      release = () => resolve({ agents: [{ name: 'default', model: 'claude-opus-5' }], default_agent: 'default' })
    }) as never)
    qc.refetchQueries({ queryKey: ['kirocrew-agents'] })
    await waitFor(() => expect(button).toBeDisabled())
    release()
    await waitFor(() => expect(button).toBeEnabled())
    // The same for the resolver.
    agentResolvedModelMock.mockImplementationOnce(() => new Promise(resolve => {
      release = () => resolve({ model: 'claude-opus-5', pinned: true })
    }) as never)
    qc.refetchQueries({ queryKey: ['resolved-model'] })
    await waitFor(() => expect(button).toBeDisabled())
    release()
    await waitFor(() => expect(button).toBeEnabled())
  })

  it('names the agent the way the chat model picker does, by its name, not its display name', async () => {
    // The picker row this notice points at ("Pinned for <agent>") uses the raw
    // agent name; the two must agree or the reader cannot match them up.
    seedAgent('claude-opus-5', { display_name: 'Kiro' })
    seedResolved('claude-opus-5', true)
    wrap()
    await settled()
    const notice = await screen.findByTestId('agent-model-pin-notice')
    expect(notice).toHaveTextContent('New chats on the default agent use')
    expect(notice).not.toHaveTextContent('Kiro')
  })

  it('holds the clear button while a new global default is still being saved', async () => {
    // Clearing the pin hands the agent to the global default; if that default is
    // mid-save and the save is then rejected, the pin is already gone and the
    // agent lands on neither model. So the button waits for the save to settle.
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5', true)
    wrap()
    await settled()
    const button = await screen.findByRole('button', CLEAR_BUTTON)
    expect(button).toBeEnabled()
    let release!: () => void
    patchConfigMock.mockImplementationOnce(() => new Promise(resolve => {
      release = () => resolve({})
    }) as never)
    fireEvent.click(await screen.findByRole('combobox', { name: 'Default Model' }))
    fireEvent.click(screen.getAllByRole('option', { name: 'claude-sonnet-5' }).filter(o => !o.closest('nav'))[0])
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.model', 'claude-sonnet-5'))
    // The notice still shows (the pin still beats the new pick), but the button
    // waits for the save to land.
    const pending = await screen.findByRole('button', CLEAR_BUTTON)
    expect(pending).toBeDisabled()
    expect(updateKirocrewAgentMock).not.toHaveBeenCalled()
    release()
    await waitFor(() => expect(pending).toBeEnabled())
  })

  it('points at the chat model picker, with no button, when the agent itself inherits', async () => {
    // The resolver can land here from the agent's template OR from the backend
    // default when the global model is out of the agent's scope, so the notice
    // names both tiers rather than picking one, says why there is nothing to
    // clear, and names the one control that CAN fix it.
    seedAgent('')
    seedResolved('claude-opus-5', false)
    wrap()
    await settled()
    const notice = await screen.findByTestId('agent-model-pin-notice')
    expect(notice).toHaveTextContent(
      'New chats on the default agent use claude-opus-5, which comes from its agent template or the backend default, not this setting. This agent does not pin a model itself, so there is nothing to clear here. To choose one, open the chat model picker and pin a model to this agent.'
    )
    expect(screen.queryByRole('button', CLEAR_BUTTON)).toBeNull()
  })

  it('trusts the resolver, not the roster, on whose pin answered', async () => {
    // The roster carries the same model, but the resolver says the agent's own
    // record does not pin it (the roster is a separate, possibly stale read).
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5', false)
    wrap()
    await settled()
    expect(await screen.findByTestId('agent-model-pin-notice')).toHaveTextContent('nothing to clear here')
    expect(screen.queryByRole('button', CLEAR_BUTTON)).toBeNull()
  })

  it('shows nothing, and offers no clear, when the agent carries a pin the backend skipped', async () => {
    // The record pins a model the active backend cannot use, so the resolver
    // skipped it and answered from a lower tier. Clearing would erase a pin that
    // is kept on purpose for the other backend, and naming it as the source
    // would be wrong.
    seedAgent('gpt-5')
    seedResolved('claude-opus-5', true)
    wrap()
    await settled()
    expect(screen.queryByTestId('agent-model-pin-notice')).toBeNull()
    expect(screen.queryByRole('button', { name: /Clear its pin/ })).toBeNull()
  })

  it('holds the clear button when a re-read fails and only the old answer is left', async () => {
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5', true)
    const qc = wrap()
    await settled()
    const button = await screen.findByRole('button', CLEAR_BUTTON)
    expect(button).toBeEnabled()
    kirocrewAgentsMock.mockImplementationOnce(() => Promise.reject(new Error('502')) as never)
    await qc.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    expect(await screen.findByText('Failed to load config.')).toBeInTheDocument()
    expect(screen.getByRole('button', CLEAR_BUTTON)).toBeDisabled()
    expect(updateKirocrewAgentMock).not.toHaveBeenCalled()
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
    seedResolved('claude-opus-5', true)
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
    seedResolved('claude-opus-5', true)
    wrap()
    await waitFor(() => expect(kirocrewAgentsMock).toHaveBeenCalled())
    expect(await screen.findByText('Failed to load config.')).toBeInTheDocument()
    // The resolver waits for the roster to name the agent, so it has not been
    // asked yet — and there is no agent name it could have been asked for.
    expect(agentResolvedModelMock).not.toHaveBeenCalled()
    const calls = kirocrewAgentsMock.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(kirocrewAgentsMock.mock.calls.length).toBeGreaterThan(calls))
    expect(await screen.findByTestId('agent-model-pin-notice')).toBeInTheDocument()
    expect(agentResolvedModelMock).toHaveBeenCalledWith('default')
  })

  it('folds two failed reads into one banner whose Retry re-asks both', async () => {
    kirocrewConfigMock.mockImplementationOnce(() => Promise.reject(new Error('502')) as never)
    kirocrewAgentsMock.mockImplementationOnce(() => Promise.reject(new Error('502')) as never)
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5', true)
    wrap()
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    await waitFor(() => expect(kirocrewAgentsMock).toHaveBeenCalled())
    expect(await screen.findByText('Failed to load config.')).toBeInTheDocument()
    // One notice, one Retry — not a row per failed read saying the same thing.
    expect(screen.getAllByText('Failed to load config.')).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(1)
    const cfgCalls = kirocrewConfigMock.mock.calls.length
    const rosterCalls = kirocrewAgentsMock.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(kirocrewConfigMock.mock.calls.length).toBeGreaterThan(cfgCalls))
    await waitFor(() => expect(kirocrewAgentsMock.mock.calls.length).toBeGreaterThan(rosterCalls))
    await waitFor(() => expect(screen.queryByText('Failed to load config.')).toBeNull())
    expect(await screen.findByTestId('agent-model-pin-notice')).toBeInTheDocument()
  })

  it('removes the agent pin so the global default applies again', async () => {
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5', true)
    wrap()
    await settled()
    fireEvent.click(await screen.findByRole('button', CLEAR_BUTTON))
    await waitFor(() =>
      expect(updateKirocrewAgentMock).toHaveBeenCalledWith('default', { model: '' })
    )
  })

  it('reports a rejected clear beside the button, with the server reason, and clears it on retry', async () => {
    updateKirocrewAgentMock.mockImplementationOnce(() => Promise.reject(new Error('agent is read-only')) as never)
    seedAgent('claude-opus-5')
    seedResolved('claude-opus-5', true)
    wrap()
    await settled()
    const notice = await screen.findByTestId('agent-model-pin-notice')
    fireEvent.click(screen.getByRole('button', CLEAR_BUTTON))
    const err = await screen.findByTestId('agent-model-pin-clear-error')
    expect(err).toHaveAttribute('role', 'alert')
    expect(err).toHaveTextContent("Couldn't clear the agent's pinned model (agent is read-only). Try again.")
    // Inside the notice row, not the panel's top banner.
    expect(notice).toContainElement(err)
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    // The button is the retry: a second attempt resets the error.
    fireEvent.click(screen.getByRole('button', CLEAR_BUTTON))
    await waitFor(() => expect(updateKirocrewAgentMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('agent-model-pin-clear-error')).toBeNull())
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
