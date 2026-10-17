import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createTestStore } from './helpers'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const { mockChatSlotAgent, mockSendChat } = vi.hoisted(() => ({
  mockChatSlotAgent: vi.fn(),
  mockSendChat: vi.fn().mockResolvedValue({ ok: true }),
}))

// The REAL ApiError rides through the mock so a refusal is built exactly as
// `api.chatSlotAgent` rejects in production, not as a hand-rolled stand-in.
vi.mock('../api/client', async (importActual) => {
  const actual = await importActual<typeof import('../api/client')>()
  return {
    ApiError: actual.ApiError,
    api: new Proxy({ chatSlotAgent: mockChatSlotAgent, sendChat: mockSendChat }, {
      get: (t, prop) => {
        if (prop in t) return (t as Record<string, unknown>)[prop as string]
        return vi.fn().mockResolvedValue({})
      },
    }),
    SEARCH_MIN_CHARS: 2,
  }
})

import { ApiError } from '../api/client'
import { agentSwitchTarget, interceptSlashCommand, isInterceptedSlashCommand } from '../pages/chat/ChatInput'

const SLOT = 'chat-1'

const slot = (key: string): ChatSlot => ({
  key, title: key, messages: 1, running: false, mode: '', created: '', last_ts: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
  agent: 'kirocrew',
} as ChatSlot)

function storeWithSlot() {
  return createTestStore({
    dashboard: { slots: [slot(SLOT)] } as unknown as RootState['dashboard'],
  })
}

const slotAgent = (store: ReturnType<typeof createTestStore>) =>
  store.getState().dashboard.slots.find(s => s.key === SLOT)?.agent

describe('/agent <name> routes through Crew\'s agent selector', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockChatSlotAgent.mockImplementation(async (_slot: string, agent: string, kind?: string) =>
      ({ ok: true, agent, agent_kind: kind ?? '', workspace: 'default' }))
  })

  it('switches through the slot agent endpoint as a template pick, never as a turn', async () => {
    const store = storeWithSlot()
    const result = await interceptSlashCommand('/agent fable', SLOT, store.dispatch)
    expect(result).toEqual({ intercepted: true })
    // The picker's endpoint, with the namespace stated: an unknown name is then
    // refused (409) instead of committed with the default agent answering.
    expect(mockChatSlotAgent).toHaveBeenCalledWith(SLOT, 'fable', 'template', { announce: true })
    expect(mockSendChat).not.toHaveBeenCalled()
    // The store mirrors what the response named, as the picker's switch does.
    expect(slotAgent(store)).toBe('fable')
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('accepts kiro-cli built-ins and dotted template names', async () => {
    const store = storeWithSlot()
    for (const name of ['kiro_planner', 'team.reviewer']) {
      await interceptSlashCommand(`  /agent ${name}  `, SLOT, store.dispatch)
      expect(mockChatSlotAgent).toHaveBeenLastCalledWith(SLOT, name, 'template', { announce: true })
    }
  })

  it('leaves bare /agent, its subcommands and multi-word text to kiro-cli', async () => {
    const store = storeWithSlot()
    const passthrough = [
      '/agent', '/agent list', '/agent LIST', '/agent schema', '/agent create', '/agent create mine',
      '/agent set-default fable', '/agent fable please', '/agentx fable', 'switch /agent fable',
    ]
    for (const text of passthrough) {
      expect(agentSwitchTarget(text.trim())).toBeNull()
      expect(isInterceptedSlashCommand(text)).toBe(false)
      expect(await interceptSlashCommand(text, SLOT, store.dispatch)).toEqual({ intercepted: false })
    }
    expect(mockChatSlotAgent).not.toHaveBeenCalled()
  })

  it('keeps the sync predicate in lockstep with the interception', () => {
    expect(isInterceptedSlashCommand('/agent fable')).toBe(true)
    expect(isInterceptedSlashCommand('/agent kiro_default ')).toBe(true)
  })

  it('reports a refused switch on the shared agent-switch notice and marks it failed', async () => {
    mockChatSlotAgent.mockRejectedValueOnce(new ApiError(
      409,
      'the selected agent choice is not available',
      JSON.stringify({ error: 'the selected agent choice is not available', code: 'agent_choice_unavailable' }),
    ))
    const store = storeWithSlot()
    const result = await interceptSlashCommand('/agent fabel', SLOT, store.dispatch)
    expect(result).toEqual({
      intercepted: true, failed: true, stage: 'agent',
      error: 'the selected agent choice is not available',
    })
    // The caller owns the report (the composer's notice); no shell toast.
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
    expect(slotAgent(store)).toBe('kirocrew')
  })

  it('names the retry-later reason when a turn is in flight', async () => {
    mockChatSlotAgent.mockRejectedValueOnce(new ApiError(
      409,
      'a turn is in flight',
      JSON.stringify({ error: 'a turn is in flight', code: 'turn_in_flight' }),
    ))
    const store = storeWithSlot()
    const result = await interceptSlashCommand('/agent fable', SLOT, store.dispatch)
    expect(result).toMatchObject({ intercepted: true, failed: true, stage: 'agent' })
    const reason = result.intercepted && result.failed ? result.error ?? '' : ''
    expect(reason).not.toBe('a turn is in flight')
    expect(reason).toBeTruthy()
  })

  it('holds the pick for the next slot when the chat has none yet', async () => {
    const store = createTestStore()
    const onPendingAgent = vi.fn()
    const result = await interceptSlashCommand('/agent fable', null, store.dispatch, { onPendingAgent })
    expect(result).toEqual({ intercepted: true })
    expect(onPendingAgent).toHaveBeenCalledWith('fable')
    expect(mockChatSlotAgent).not.toHaveBeenCalled()
  })

  it('fails (keeping the composer) with no slot and no caller to hold the pick', async () => {
    const store = createTestStore()
    const result = await interceptSlashCommand('/agent fable', null, store.dispatch)
    expect(result).toEqual({ intercepted: true, failed: true, stage: 'agent' })
    expect(mockChatSlotAgent).not.toHaveBeenCalled()
  })
})
