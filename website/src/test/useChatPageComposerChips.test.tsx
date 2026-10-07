/**
 * The composer chips' model pin write (pages/chat/page/composerChips.ts): the
 * model picker's "set as this agent's default" row, driven as a hook.
 *
 * The dropdown closes the moment the row is clicked, so the outcome has to be
 * visible elsewhere:
 *  - success refreshes the dashboard and invalidates every resolved-model query,
 *    so a slot showing an inherited model picks the new pin up without a reload;
 *  - failure is said on the page (the action-error notice, titled) AND as a
 *    critical agent notification, both naming the agent and the server's message.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const apiMock = vi.hoisted(() => ({ updateKirocrewAgent: vi.fn(), projectGit: vi.fn(), kirocrewConfig: vi.fn() }))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, ...apiMock } }
})

import { useComposerChips } from '../pages/chat/page/composerChips'
import { triggerRefresh } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

type Opts = Parameters<typeof useComposerChips>[0]

function harness() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  const provider = {
    id: 'test-provider',
    capabilities: { reasoningEffort: false },
    resolveModel: vi.fn().mockResolvedValue('m-1'),
    resolveDefaultEffort: vi.fn().mockResolvedValue(''),
  }
  const opts: Opts = {
    currentSlot: { key: 'slot-a', agent: 'builder', model: 'm-2' } as ChatSlot,
    defaultAgent: 'default',
    pendingAgent: '',
    installedAgents: [],
    provider: provider as never,
    availableModels: [],
    codexPairModels: false,
    selectionCapabilities: undefined,
    selectionCapabilitiesQ: { isError: false },
    remoteCrew: { isRemote: false } as never,
    dispatch: vi.fn() as never,
    queryClient,
    showActionError: vi.fn(),
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  const hook = renderHook((p: Opts) => useComposerChips(p), { initialProps: opts, wrapper })
  return { opts, hook, invalidate }
}

beforeEach(() => {
  apiMock.updateKirocrewAgent.mockReset()
  apiMock.projectGit.mockReset()
})

describe('pinning the shown model to the agent', () => {
  it('writes agents.<name>.model, then refreshes and invalidates resolved models', async () => {
    apiMock.updateKirocrewAgent.mockResolvedValue({})
    const { opts, hook, invalidate } = harness()
    expect(hook.result.current._modelPinAgent).toBe('builder')
    act(() => { hook.result.current.pinModelToAgentMut.mutate({ agent: 'builder', model: 'm-2' }) })
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['resolved-model'] }))
    expect(apiMock.updateKirocrewAgent).toHaveBeenCalledWith('builder', { model: 'm-2' })
    expect(opts.dispatch).toHaveBeenCalledWith(triggerRefresh())
    expect(opts.showActionError).not.toHaveBeenCalled()
  })

  it('says a failed write on the page and as a critical notification', async () => {
    apiMock.updateKirocrewAgent.mockRejectedValue(new Error('config.json is read-only'))
    const { opts, hook } = harness()
    act(() => { hook.result.current.pinModelToAgentMut.mutate({ agent: 'builder', model: 'm-2' }) })
    await waitFor(() => expect(opts.showActionError).toHaveBeenCalledTimes(1))
    const [body, title] = (opts.showActionError as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(body).toBe('builder: config.json is read-only')
    expect(title).toMatch(/./)
    const note = (opts.dispatch as ReturnType<typeof vi.fn>).mock.calls.map(c => c[0]).find(a => a.type?.endsWith('addNotification'))
    expect(note.payload).toMatchObject({ kind: 'agent', priority: 'critical', title, body })
    expect(typeof note.payload.ts).toBe('string')
  })
})

describe('effort control gate on the capability read', () => {
  it('keeps effort available while the read 404s for a not-yet-registered slot (#14817)', () => {
    const { opts, hook } = harness()
    const base: Opts = {
      ...opts,
      provider: { ...(opts.provider as object), capabilities: { reasoningEffort: true } } as never,
      selectionCapabilities: { known: true, effort_supported: true, effort_levels: ['low', 'high'] } as never,
    }
    const notFound = Object.assign(new Error('not found'), { status: 404 })
    hook.rerender({ ...base, selectionCapabilitiesQ: { isError: true, error: notFound } })
    expect(hook.result.current.effortSupported).toBe(true)
    const peerDown = Object.assign(new Error('peer unavailable'), { status: 503 })
    hook.rerender({ ...base, selectionCapabilitiesQ: { isError: true, error: peerDown } })
    expect(hook.result.current.effortSupported).toBe(false)
  })
})

describe('what the live session reports running', () => {
  // A claude session pinned to Fable whose harness ran Opus: the chip must name
  // what ran, not the pick, and the effort the adapter reported, not the config.
  function liveHarness(slot: Partial<ChatSlot>) {
    const h = harness()
    const provider = {
      id: 'test-provider',
      capabilities: { reasoningEffort: true },
      resolveModel: vi.fn().mockResolvedValue('claude-fable-5.1'),
      resolveDefaultEffort: vi.fn().mockResolvedValue('max'),
    }
    h.hook.rerender({
      ...h.opts,
      provider: provider as never,
      currentSlot: { key: 'slot-a', agent: 'builder', model: '', ...slot } as ChatSlot,
    })
    return h
  }

  it('names the model the harness reported over the selection and the resolved default', async () => {
    apiMock.kirocrewConfig.mockResolvedValue({ agent: { model: 'claude-fable-5.1' } })
    const { hook } = liveHarness({
      served_model: 'global.anthropic.claude-fable-5[1m]',
      turn_model: 'global.anthropic.claude-opus-5[1m]',
      served_effort: '',
    })
    await waitFor(() => expect(hook.result.current.chipModel).toBe('global.anthropic.claude-opus-5[1m]'))
    // The picker keeps highlighting the selection; the chip's title names it.
    expect(hook.result.current.shownModel).toBe('global.anthropic.claude-fable-5[1m]')
    expect(hook.result.current.modelSelected).toBe('global.anthropic.claude-fable-5[1m]')
    // The adapter reported its own default, so the configured `max` is not shown.
    expect(hook.result.current.effectiveEffort).toBe('')
  })

  it('keeps the configured prediction until a session reports', async () => {
    apiMock.kirocrewConfig.mockResolvedValue({ agent: { model: 'claude-fable-5.1' } })
    const { hook } = liveHarness({})
    await waitFor(() => expect(hook.result.current.chipModel).toBe('claude-fable-5.1'))
    await waitFor(() => expect(hook.result.current.effectiveEffort).toBe('max'))
    expect(hook.result.current.modelSelected).toBe('')
  })
})
