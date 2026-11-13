/**
 * A session's model picker lists the models of the harness the session runs on.
 *
 * Every picker read ONE list, `GET /api/models` for the configured backend. A
 * crewmate DM thread on Claude Code under a kiro-cli default therefore offered
 * kiro-cli's credit-priced catalog. The server now names the other harness
 * (`models_backend`), and the list for it is fetched and cached on its own key.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
    kirocrewConfig: vi.fn().mockResolvedValue({}),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], choices: [] }),
    setDefaultAgent: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'
import { markModelsDegraded, modelListRefetchInterval, modelsDegraded } from '../providers/modelListHealth'
import { useSessionRosters } from '../pages/chat/page/sessionRosters'
import { useComposerChips } from '../pages/chat/page/composerChips'
import type { ChatSlot } from '../types'

const KIRO_ROWS = [{ model_name: 'auto' }, { model_name: 'claude-fable-5.1', description: 'credits' }]
const CLAUDE_ROWS = [{ model_name: 'auto' }, { model_name: 'global.anthropic.claude-opus-5-5[1m]' }]
const models = api.models as unknown as ReturnType<typeof vi.fn>

/** The roster's list is fetched under the backend's own key once `modelsBackend` names it. */
const BACKEND_LIST = { timeout: 5000 }

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  markModelsDegraded('acp', false)
  markModelsDegraded('acp:claude', false)
  models.mockImplementation((backend?: string) => Promise.resolve(backend === 'claude' ? CLAUDE_ROWS : KIRO_ROWS))
})

describe('AcpAdapter.fetchAvailableModels(backend)', () => {
  it('asks for that backend and leaves the configured list\'s last-good cache alone', async () => {
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    const configured = localStorage.getItem('kc.acp.models.v1')

    const rows = await adapter.fetchAvailableModels('claude')

    expect(models).toHaveBeenLastCalledWith('claude')
    expect(rows.map(m => m.name)).toEqual(['auto', 'global.anthropic.claude-opus-5-5[1m]'])
    expect(localStorage.getItem('kc.acp.models.v1')).toBe(configured)
  })

  it('never serves the configured list\'s ids when that backend\'s fetch fails', async () => {
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    models.mockRejectedValue(new Error('503'))

    const rows = await adapter.fetchAvailableModels('claude')

    expect(rows.map(m => m.name)).toEqual(['auto'])
    expect(modelsDegraded('acp:claude')).toBe(true)
    expect(modelsDegraded('acp')).toBe(false)
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp', 'claude'] })).toBe(8_000)
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp'] })).toBe(false)
  })
})

describe('useSessionRosters', () => {
  function rosters(modelsBackend: string | undefined) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    return renderHook(() => useSessionRosters({
      activeSlot: 'member-helper',
      activeSlotProject: undefined,
      refreshTrigger: 0,
      slots: [],
      dispatch: vi.fn(),
      modelsBackend,
    }), { wrapper })
  }

  it('offers a claude session claude\'s models under a kiro-cli default', async () => {
    const { result } = rosters('claude')

    await waitFor(() => expect(result.current.effectiveModels.map(m => m.name)).toContain('global.anthropic.claude-opus-5-5[1m]'), BACKEND_LIST)
    expect(result.current.effectiveModels.map(m => m.name)).not.toContain('claude-fable-5.1')
  })

  it('keeps the configured list for a session on the configured backend', async () => {
    const { result } = rosters(undefined)

    await waitFor(() => expect(result.current.effectiveModels.map(m => m.name)).toContain('claude-fable-5.1'), BACKEND_LIST)
    expect(models).not.toHaveBeenCalledWith('claude')
  })

  it('says when the session\'s own list failed to load', async () => {
    // That list keeps no last-good copy, so a failed fetch leaves only Auto.
    models.mockImplementation((backend?: string) => (backend === 'claude'
      ? Promise.reject(new Error('Service Unavailable'))
      : Promise.resolve(KIRO_ROWS)))
    const { result } = rosters('claude')

    await waitFor(() => expect(result.current.ownModelsFailed).toBe(true), BACKEND_LIST)
  })
})

describe('useComposerChips', () => {
  it('keeps a claude session\'s pin on the chip while claude\'s own list is degraded', () => {
    // The adapter served the auto-only fallback for claude's list; the configured list is healthy.
    markModelsDegraded('acp:claude', true)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    const { result } = renderHook(() => useComposerChips({
      currentSlot: { key: 'member-helper', agent: 'helper', model: 'claude-opus-5-5' } as ChatSlot,
      defaultAgent: 'default',
      pendingAgent: '',
      installedAgents: [],
      provider: { id: 'acp', capabilities: { reasoningEffort: false }, resolveModel: vi.fn(), resolveDefaultEffort: vi.fn().mockResolvedValue('') } as never,
      availableModels: [{ name: 'auto', description: '' }],
      codexPairModels: false,
      selectionCapabilities: undefined,
      selectionCapabilitiesQ: { isError: false, data: { models_backend: 'claude' } },
      remoteCrew: { isRemote: false } as never,
      dispatch: vi.fn() as never,
      queryClient: qc,
      showActionError: vi.fn(),
    }), { wrapper })

    expect(result.current.shownModel).toBe('claude-opus-5-5')
  })
})
