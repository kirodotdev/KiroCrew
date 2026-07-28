import { describe, it, expect } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import SubagentRunCard, { extractSpawnRunLaunch, isSpawnRunTool } from '../pages/chat/SubagentRunCard'
import type { RootState } from '../store'
import type { ChatMessage, SubagentActivity } from '../types'

type ChatState = RootState['chat']

const SLOT = 'chat-1'

/** Mirrors the real spawn_run tool result shape produced by mcp_core.py. */
const SPAWN_OUTPUT = [
  'Spawned 3 subagent(s). Results will arrive as completion events:',
  '  1713e7d0 (kirocrew): INVESTIGATION ONLY -- trace the backend signals',
  '  5c15adde (kirocrew): INVESTIGATION ONLY -- trace the sidebar data flow',
  '  aa5da49b (kirocrew): INVESTIGATION ONLY -- trace the chat transcript cards',
  '',
  '⚠️ END YOUR TURN NOW — do no further work this turn.',
].join('\n')

function spawnToolMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'tool',
    content: '🔧 spawn_run',
    cls: '',
    meta: { tool_call_id: 'tc_spawn', input: '{}', output: SPAWN_OUTPUT },
    ...overrides,
  }
}

function agent(id: string, status: SubagentActivity['status']): SubagentActivity {
  return {
    id, task: 't', agent: 'kirocrew', status, streaming: '', lastTool: '',
    startedAt: Date.now(), elapsed: 0, toolCount: 0, stalled: false,
  } as SubagentActivity
}

describe('SubagentRunCard detection helpers', () => {
  it('extracts every accepted agent id from a spawn_run result', () => {
    const launch = extractSpawnRunLaunch(spawnToolMsg())
    expect(launch).not.toBeNull()
    expect(launch!.ids).toEqual(['1713e7d0', '5c15adde', 'aa5da49b'])
    expect(launch!.announced).toBe(3)
  })

  it('is stateful-regex safe — repeated calls return the same ids', () => {
    // The agent-line regex is /g and module-scoped; a stale lastIndex would
    // make the second call silently drop leading ids.
    const first = extractSpawnRunLaunch(spawnToolMsg())!.ids
    const second = extractSpawnRunLaunch(spawnToolMsg())!.ids
    expect(second).toEqual(first)
  })

  it('returns null for a non-spawn tool message', () => {
    const msg: ChatMessage = { role: 'tool', content: '🔧 Running: echo hi', cls: '', meta: { output: 'hi' } }
    expect(extractSpawnRunLaunch(msg)).toBeNull()
    expect(isSpawnRunTool(msg)).toBe(false)
  })

  it('returns null when the launch output has not arrived yet', () => {
    expect(extractSpawnRunLaunch(spawnToolMsg({ meta: { tool_call_id: 'tc_spawn' } }))).toBeNull()
  })

  it('still detects a launch whose per-agent lines are absent', () => {
    const msg = spawnToolMsg({ meta: { output: 'Spawned 1 subagent(s). Monitor results via polling:' } })
    const launch = extractSpawnRunLaunch(msg)
    expect(launch).not.toBeNull()
    expect(launch!.ids).toEqual([])
    expect(launch!.announced).toBe(1)
  })

  it('isSpawnRunTool is true only for the tool role', () => {
    expect(isSpawnRunTool(spawnToolMsg())).toBe(true)
    // The same text quoted in an assistant message must not render a card.
    expect(isSpawnRunTool(spawnToolMsg({ role: 'assistant' }))).toBe(false)
  })
})

describe('SubagentRunCard rendering', () => {
  const launch = { ids: ['a1', 'a2', 'a3'], announced: 3 }

  it('reports running agents from the live slice', () => {
    const store = createTestStore({
      chat: {
        activeSlot: SLOT,
        subagents: { a1: agent('a1', 'running'), a2: agent('a2', 'tool'), a3: agent('a3', 'done') },
        subagentQueued: {},
      } as unknown as ChatState,
    })
    renderWithProviders(<SubagentRunCard launch={launch} slot={SLOT} />, { store })
    expect(screen.getByText('2 agents running')).toBeTruthy()
  })

  it('surfaces queued agents that have not started yet', () => {
    // The regression this card exists for: a wave accepted but still behind the
    // concurrency cap has NO per-agent entries, so a card keyed only on
    // `subagents` would read as idle.
    const store = createTestStore({
      chat: { activeSlot: SLOT, subagents: {}, subagentQueued: { [SLOT]: 3 } } as unknown as ChatState,
    })
    renderWithProviders(<SubagentRunCard launch={launch} slot={SLOT} />, { store })
    expect(screen.getByTestId('subagent-card-queued').textContent).toContain('3 waiting')
    // "0 agents running" is technically true and useless for a fully-queued wave.
    expect(screen.getByText('3 agents queued')).toBeTruthy()
    expect(screen.queryByText('0 agents running')).toBeNull()
  })

  it('reads a background slot from slotActivity, not the active map', () => {
    const store = createTestStore({
      chat: {
        activeSlot: 'chat-other',
        subagents: {},
        slotActivity: { [SLOT]: { toolLog: [], subagents: { a1: agent('a1', 'running') } } },
        subagentQueued: {},
      } as unknown as ChatState,
    })
    renderWithProviders(<SubagentRunCard launch={launch} slot={SLOT} />, { store })
    expect(screen.getByText('1 agent running')).toBeTruthy()
  })

  it('shows a finished summary once the wave settles', () => {
    const store = createTestStore({
      chat: {
        activeSlot: SLOT,
        subagents: { a1: agent('a1', 'done'), a2: agent('a2', 'done'), a3: agent('a3', 'error') },
        subagentQueued: {},
      } as unknown as ChatState,
    })
    renderWithProviders(<SubagentRunCard launch={launch} slot={SLOT} />, { store })
    expect(screen.getByText('3 agents finished')).toBeTruthy()
  })

  it('clicking the card opens the Subagents panel on this wave', () => {
    const store = createTestStore({
      chat: { activeSlot: SLOT, subagents: { a1: agent('a1', 'running') }, subagentQueued: {} } as unknown as ChatState,
    })
    renderWithProviders(<SubagentRunCard launch={launch} slot={SLOT} />, { store })
    fireEvent.click(screen.getByTestId('subagent-run-card'))
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('subagents')
    expect(store.getState().chat.selectedSubagentId).toBe('a1')
  })

  it('a settled wave does not claim ANOTHER wave\u2019s queue depth', () => {
    // chat.subagentQueued is keyed by slot, not by launch: a second spawn_run
    // wave queueing behind the cap must not make this already-finished card
    // report "3 agents queued" and a "3 waiting" chip for agents that are not
    // its own. Settled therefore outranks queued in both label and chip.
    const store = createTestStore({
      chat: {
        activeSlot: SLOT,
        subagents: { a1: agent('a1', 'done'), a2: agent('a2', 'done'), a3: agent('a3', 'done') },
        subagentQueued: { [SLOT]: 3 },
      } as unknown as ChatState,
    })
    renderWithProviders(<SubagentRunCard launch={launch} slot={SLOT} />, { store })
    expect(screen.getByText('3 agents finished')).toBeTruthy()
    expect(screen.queryByText('3 agents queued')).toBeNull()
    expect(screen.queryByTestId('subagent-card-queued')).toBeNull()
  })
})
