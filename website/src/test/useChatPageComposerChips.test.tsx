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

function harness(over: Partial<Opts> = {}) {
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
    installedAgents: [{ name: 'builder', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', selection_kind: 'member' }],
    provider: provider as never,
    availableModels: [],
    codexPairModels: false,
    selectionCapabilities: undefined,
    selectionCapabilitiesQ: { isError: false },
    dispatch: vi.fn() as never,
    queryClient,
    showActionError: vi.fn(),
    ...over,
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

describe('which agent the pin row may target (#25099)', () => {
  const templateRow = { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', selection_kind: 'template' as const }

  it('offers no target on a template-bound session, so the row is withheld', () => {
    const { hook } = harness({
      currentSlot: { key: 'slot-t', agent: 'kirocrew', agent_kind: 'template', model: 'm-2' } as ChatSlot,
      installedAgents: [templateRow],
    })
    expect(hook.result.current._modelPinAgent).toBe('')
    expect(hook.result.current._modelPinPinned).toBe(false)
  })

  it('offers no target when the slot kind is unset but the name is only a template', () => {
    const { hook } = harness({
      currentSlot: { key: 'slot-t', agent: 'kirocrew', model: 'm-2' } as ChatSlot,
      installedAgents: [templateRow],
    })
    expect(hook.result.current._modelPinAgent).toBe('')
  })

  it('offers no target for a template-bound slot even when a crew agent shares its name', () => {
    const { hook } = harness({
      currentSlot: { key: 'slot-t', agent: 'kirocrew', agent_kind: 'template', model: 'm-2' } as ChatSlot,
      installedAgents: [{ ...templateRow, selection_kind: 'member' }],
    })
    expect(hook.result.current._modelPinAgent).toBe('')
  })

  it('keeps the crew agent as the target on a member session', () => {
    const { hook } = harness({
      currentSlot: { key: 'slot-m', agent: 'builder', agent_kind: 'member', model: 'm-2' } as ChatSlot,
    })
    expect(hook.result.current._modelPinAgent).toBe('builder')
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

describe('the inherited model a slot shows', () => {
  it('asks for the owning app, so an app session names its own model setting', async () => {
    const { opts, hook } = harness()
    const provider = opts.provider as unknown as { resolveModel: ReturnType<typeof vi.fn> }
    hook.rerender({ ...opts, currentSlot: { key: 'slot-b', agent: '', app: 'demo-app' } as ChatSlot })
    await waitFor(() => expect(provider.resolveModel).toHaveBeenCalledWith('default', 'demo-app'))
  })

  it('asks for no app on a slot no app owns', async () => {
    const { opts, hook } = harness()
    const provider = opts.provider as unknown as { resolveModel: ReturnType<typeof vi.fn> }
    hook.rerender({ ...opts, currentSlot: { key: 'slot-c', agent: 'builder' } as ChatSlot })
    await waitFor(() => expect(provider.resolveModel).toHaveBeenCalledWith('builder', undefined))
  })
})
