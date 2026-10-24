/**
 * Behavioural coverage for the multiplexed dashboard socket: the frame router's
 * activity, automation and re-broadcast arms -- follow-up and folder cards,
 * subagent and workflow activity, the automation map, update progress, the
 * window re-broadcasts, source-status deltas, and how frames are scheduled.
 * The frame-router setup repeats
 * UseWebSocketCoverage.test.tsx's (keep the copies identical); the fakes are in
 * UseWebSocketCoverageTestUtils.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { store as globalStore } from '../store'
import chatReducer, { PANE_HYDRATE_LIMIT, setActiveSlot, clearMessages, sseChatMessage, sseAutomation } from '../store/chatSlice'
import type { ChatSlot } from '../types'
import { structuredMonitorLoop } from './monitorFixtures'
import { normalizeAutomationRecord } from '../monitoring/automation'
import { AUTONUDGE_LOOPS_QUERY_KEY } from '../components/autoNudgeLoop'
import { ACTIVE, BACKGROUND, WS_INSTANCES, MockWebSocket, MockAudio } from './UseWebSocketCoverageTestUtils'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error { status = 500 },
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    answerQuestion: vi.fn().mockResolvedValue({ ok: true }),
    voiceSynthesize: vi.fn().mockResolvedValue({ ok: true }),
    voiceCancel: vi.fn().mockResolvedValue({ ok: true }),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

describe('useWebSocket frame router', () => {
  let testStore: ReturnType<typeof createTestStore>
  let testQueryClient: QueryClient
  let rafCbs: FrameRequestCallback[]
  let originalCreateObjectUrl: typeof URL.createObjectURL
  let originalRevokeObjectUrl: typeof URL.revokeObjectURL

  const slotFixture = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
    key, title: key, agent: 'kirocrew', ...extra,
  } as ChatSlot)

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    MockAudio.instances.length = 0
    MockAudio.playResult = () => Promise.resolve()
    rafCbs = []
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    // The production tree dispatches and reads through the same singleton
    // store. This harness intentionally dispatches through a Provider test
    // store, so mirror only production-marked coordinator rows into the
    // singleton read side; unmarked runner rows remain explicit per test.
    const mirroredCoordinatorRows = new Set<string>()
    testStore.subscribe(() => {
      const state = testStore.getState().chat
      const bySlot: Array<[string | null, typeof state.messages]> = [
        [state.activeSlot, state.messages],
        ...Object.entries(state.slotMessages),
      ]
      for (const [slot, messages] of bySlot) {
        if (!slot) continue
        for (const message of messages ?? []) {
          const id = message.meta?.approval_id
          if (message.role !== 'permission'
            || message.meta?.registry !== 'coordinator'
            || typeof id !== 'string') continue
          const key = `${slot}\u0000${id}\u0000${message.ts ?? ''}`
          if (mirroredCoordinatorRows.has(key)) continue
          mirroredCoordinatorRows.add(key)
          globalStore.dispatch(sseChatMessage({
            slot,
            role: message.role,
            content: message.content,
            ts: message.ts,
            cls: message.cls,
            meta: message.meta,
          }))
        }
      }
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCbs.push(cb); return rafCbs.length })
    originalCreateObjectUrl = URL.createObjectURL
    originalRevokeObjectUrl = URL.revokeObjectURL
    let blobSeq = 0
    URL.createObjectURL = vi.fn(() => `blob:voice-${++blobSeq}`)
    URL.revokeObjectURL = vi.fn()
    // Read paths resolve against the singleton store, not the Provider store.
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    URL.createObjectURL = originalCreateObjectUrl
    URL.revokeObjectURL = originalRevokeObjectUrl
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    testQueryClient = qc
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mount() {
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => {
      ws.simulateOpen()
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot: ACTIVE, request_id: 'test-voice' } }))
    })
    return { ...hook, ws }
  }

  const chat = () => testStore.getState().chat
  const dash = () => testStore.getState().dashboard

  it('keeps only the renderable fields of a follow-up card', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'followup_card',
        data: {
          slot: ACTIVE,
          ts: 99,
          items: [
            { title: 'Add tests', prompt: 'write tests', description: 'raise coverage', branch: 'test/cov', extra: 'dropped' },
            { title: 'no prompt' },
            null,
          ],
        },
      })
    })
    const card = chat().followups[ACTIVE]
    expect(card?.ts).toBe(99)
    expect(card?.items).toEqual([
      { title: 'Add tests', description: 'raise coverage', prompt: 'write tests', branch: 'test/cov' },
    ])
  })

  it('drops a follow-up frame with no renderable item', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'followup_card', data: { slot: ACTIVE, items: 'not an array' } }) })
    expect(chat().followups[ACTIVE]).toBeUndefined()
  })

  it('accepts a complete folder suggestion and refuses a half-filled one', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'slot_folder_suggestion',
        data: { slot: ACTIVE, folder_id: 'f1', folder_name: 'Reviews', breadcrumb: 'Work / Reviews', ts: 12 },
      })
    })
    expect(chat().folderSuggestions[ACTIVE]).toEqual({
      folderId: 'f1', folderName: 'Reviews', breadcrumb: 'Work / Reviews', ts: 12, turns: 0,
    })

    act(() => {
      ws.simulateMessage({ type: 'slot_folder_suggestion', data: { slot: BACKGROUND, folder_id: 'f2' } })
    })
    expect(chat().folderSuggestions[BACKGROUND]).toBeUndefined()
  })

  it('records a plain activity event without disturbing the model catalog', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'activity_event', data: { slot: ACTIVE, kind: 'session', text: 'warm turn' } })
    })
    expect(chat().toolLog.some(e => e.type === 'session')).toBe(true)
  })

  it('threads the whole subagent lifecycle into one card', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'subagent_spawn', data: { slot: ACTIVE, id: 'ag-1', task: 'Read specs', agent: 'kirocrew' } })
      ws.simulateMessage({ type: 'subagent_queued', data: { slot: ACTIVE, queued: 3 } })
    })
    expect(chat().subagents['ag-1']?.status).toBe('running')
    expect(chat().subagentQueued[ACTIVE]).toBe(3)

    act(() => {
      ws.simulateMessage({ type: 'subagent_chunk', data: { slot: ACTIVE, id: 'ag-1', text: 'partial ' } })
      ws.simulateMessage({ type: 'subagent_tool', data: { slot: ACTIVE, id: 'ag-1', tool: 'fs_read', tool_count: 2 } })
    })
    // Subagent chunks are now buffered and flushed per animation frame (PR #5945).
    act(() => { const pending = rafCbs; rafCbs = []; pending.forEach(cb => cb(0)) })
    expect(chat().subagents['ag-1']?.streaming).toBe('partial ')
    expect(chat().subagents['ag-1']?.lastTool).toBe('fs_read')

    act(() => {
      ws.simulateMessage({ type: 'subagent_stalled', data: { slot: ACTIVE, id: 'ag-1', stalled: true, idle_secs: 90 } })
    })
    expect(chat().subagents['ag-1']?.stalled).toBe(true)

    act(() => {
      ws.simulateMessage({ type: 'subagent_retrying', data: { slot: ACTIVE, id: 'ag-1', attempt: 2 } })
      // The legacy alias routes into the same reducer.
      ws.simulateMessage({ type: 'subagent_recovering', data: { slot: ACTIVE, id: 'ag-1', attempt: 3 } })
    })
    expect(chat().subagents['ag-1']?.retrying).toBe(true)
    expect(chat().subagents['ag-1']?.stalled).toBe(false)

    act(() => {
      ws.simulateMessage({ type: 'subagent_done', data: { slot: ACTIVE, id: 'ag-1', elapsed: 12, outcome: 'completed' } })
    })
    expect(chat().subagents['ag-1']?.status).toBe('done')
    expect(chat().subagents['ag-1']?.elapsed).toBe(12)
  })

  it('fans a coalesced batch of updates and chunks into the per-agent cards', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'subagent_spawn', data: { slot: ACTIVE, id: 'ag-a', task: 'A', agent: 'w' } })
      ws.simulateMessage({ type: 'subagent_spawn', data: { slot: ACTIVE, id: 'ag-b', task: 'B', agent: 'w' } })
      ws.simulateMessage({
        type: 'subagent_batch_update',
        data: { updates: [{ id: 'ag-a', slot: ACTIVE, tool: 'grep', tool_count: 4 }, { id: 'ag-b', slot: ACTIVE, stalled: true }] },
      })
      ws.simulateMessage({
        type: 'subagent_batch_chunks',
        data: { chunks: [{ id: 'ag-a', slot: ACTIVE, text: 'aa' }, { id: 'ag-b', slot: ACTIVE, text: 'bb' }] },
      })
    })
    expect(chat().subagents['ag-a']?.lastTool).toBe('grep')
    expect(chat().subagents['ag-a']?.streaming).toBe('aa')
    expect(chat().subagents['ag-b']?.stalled).toBe(true)
    expect(chat().subagents['ag-b']?.streaming).toBe('bb')
  })

  it('replays a collapsed reconnect snapshot batch through both reducers', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'subagent_snapshot_batch',
        data: {
          items: [
            { type: 'subagent_snapshot', data: { id: 'ag-live', slot: ACTIVE, task: 'T', agent: 'w', streaming: '', last_tool: '', started: 1 } },
            { type: 'subagent_done', data: { slot: ACTIVE, id: 'ag-fin', elapsed: 4, task: 'T2', agent: 'w' } },
            { type: 'unknown_kind', data: {} },
          ],
        },
      })
    })
    expect(chat().subagents['ag-live']?.status).toBe('running')
    expect(chat().subagents['ag-fin']?.status).toBe('done')
  })

  it('accepts the wave lifecycle markers and the heartbeat as no-ops', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'spawn_batch_started', data: { slot: ACTIVE, size: 4 } })
      ws.simulateMessage({ type: 'batch_finished', data: { slot: ACTIVE } })
      ws.simulateMessage({ type: 'heartbeat', data: {} })
      ws.simulateMessage({ type: 'a_type_this_client_has_never_heard_of', data: {} })
    })
    expect(chat().subagents).toEqual({})
    expect(dash().connected).toBe(true)
  })

  it('re-imports a dev-mode app bundle on an app_reload frame', () => {
    const { ws } = mount()
    const seen: string[] = []
    const listener = (e: Event) => { seen.push((e as CustomEvent).detail?.app) }
    window.addEventListener('mc:app-reload', listener)
    try {
      act(() => { ws.simulateMessage({ type: 'app_reload', data: { app: 'dev-fleet' } }) })
      expect(seen).toEqual(['dev-fleet'])
    } finally {
      window.removeEventListener('mc:app-reload', listener)
    }
  })

  it('tracks a dynamic-workflow run and a side-conversation result', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'workflow_run_event',
        data: { run_id: 'run-1', seq: 1, type: 'phase', data: { name: 'discover', phase: 'discover' } },
      })
      ws.simulateMessage({
        type: 'chat.side_result',
        data: { slot: ACTIVE, run_id: 'run-1', role: 'assistant', content: 'side answer', final: true },
      })
    })
    expect(chat().workflowRuns['run-1']).toBeDefined()
    expect(chat().slotSide[ACTIVE]?.messages.some(m => m.content === 'side answer')).toBe(true)
  })

  it('records context usage and a plain status line', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'context_usage', data: { slot: ACTIVE, pct: 42, used_tokens: 4200, window_tokens: 10000 } })
      ws.simulateMessage({ type: 'chat_status', data: { slot: ACTIVE, status: 'Compacting…' } })
    })
    // The status line lands with the frame's stream flush.
    act(() => { const pending = rafCbs; rafCbs = []; pending.forEach(cb => cb(0)) })
    expect(chat().slotContextPct[ACTIVE]).toBe(42)
    expect(chat().slotStatusDetail[ACTIVE]).toMatchObject({ kind: 'thinking', label: 'Compacting…' })

    // A status frame with no text is ignored rather than clearing the detail.
    act(() => { ws.simulateMessage({ type: 'chat_status', data: { slot: ACTIVE } }) })
    act(() => { const pending = rafCbs; rafCbs = []; pending.forEach(cb => cb(0)) })
    expect(chat().slotStatusDetail[ACTIVE]).toMatchObject({ kind: 'thinking', label: 'Compacting…' })
  })

  it('re-reads the transcript when a variant switch names a slot', () => {
    const { ws } = mount()
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockClear()
    act(() => { ws.simulateMessage({ type: 'chat_variant_switch', data: { slot: ACTIVE } }) })
    // An empty view asks for the floor-sized page, never the whole transcript.
    expect(api.chatSlotDetail).toHaveBeenCalledWith(ACTIVE, PANE_HYDRATE_LIMIT)
  })

  it('chimes once when a turn completes', () => {
    const { ws } = mount()
    const heard: string[] = []
    const listener = (e: Event) => { heard.push((e as CustomEvent).detail?.kind) }
    window.addEventListener('mc-notification', listener)
    try {
      act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
      expect(heard).toEqual(['turn'])
      expect(chat().slotStatusDetail[ACTIVE]?.kind).toBe('idle')
    } finally {
      window.removeEventListener('mc-notification', listener)
    }
  })

  it('normalizes an autonudge frame into the authoritative automation map', () => {
    const { ws } = mount()
    const loop = {
      id: 'loop-1', slot_key: ACTIVE, message: 'go', idle_secs: 60,
      active: true, cycle_count: 4, max_cycles: 24, last_fire_ts: 0,
    }
    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'fired', slot: ACTIVE, loop },
      })
    })
    expect(chat().automations[ACTIVE]).toMatchObject({
      kind: 'legacy_goal_loop', cycleCount: 4, maxCycles: 24,
    })

    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'removed', slot: ACTIVE, loop },
      })
    })
    expect(chat().automations[ACTIVE]).toBeUndefined()
  })

  it('refreshes the full loop registry when a removal frame arrives', () => {
    const { ws } = mount()
    const invalidate = vi.spyOn(testQueryClient, 'invalidateQueries')
    invalidate.mockClear()

    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'removed', slot: ACTIVE },
      })
    })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
  })

  it('removes a structured monitor when its websocket tombstone arrives', () => {
    const { ws } = mount()
    const loop = { ...structuredMonitorLoop(), slot_key: ACTIVE }
    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'updated', slot: ACTIVE, loop },
      })
    })
    expect(chat().automations[ACTIVE]).toMatchObject({ kind: 'structured_monitor' })
    expect(testQueryClient.getQueryData(['session-automation', ACTIVE]))
      .toMatchObject({ kind: 'structured_monitor' })

    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'removed', slot: ACTIVE, loop },
      })
    })
    expect(chat().automations[ACTIVE]).toBeUndefined()
  })

  it('tombstones the cached automation before a removal refetch can fail', () => {
    const { ws } = mount()
    const loop = { ...structuredMonitorLoop(), slot_key: ACTIVE }
    testQueryClient.setQueryData(['session-automation', ACTIVE], loop)

    act(() => {
      ws.simulateMessage({
        type: 'autonudge_state',
        data: { event: 'removed', slot: ACTIVE, loop },
      })
    })

    expect(testQueryClient.getQueryData(['session-automation', ACTIVE])).toBeNull()
    expect(chat().automations[ACTIVE]).toBeUndefined()
  })

  it('tombstones a cached automation omitted by a complete reconnect seed', async () => {
    const stale = normalizeAutomationRecord({
      ...structuredMonitorLoop(),
      slot_key: ACTIVE,
    })!
    testStore.dispatch(sseAutomation(stale))
    renderHook(() => useWebSocket(), { wrapper })
    testQueryClient.setQueryData(['session-automation', ACTIVE], stale)

    await act(async () => {
      WS_INSTANCES[0].simulateOpen()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(chat().automations[ACTIVE]).toBeUndefined()
    expect(testQueryClient.getQueryData(['session-automation', ACTIVE])).toBeNull()
  })

  it('ignores an autonudge frame with no slot', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'autonudge_state', data: { event: 'fired' } }) })
    expect(chat().automations).toEqual({})
  })

  it('shows update progress and clears it on the done step', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'update_progress', data: { step: 'download', detail: '40%' } }) })
    expect(dash().updateProgress).toEqual({ step: 'download', detail: '40%' })

    act(() => { ws.simulateMessage({ type: 'update_progress', data: { step: 'done', detail: '' } }) })
    expect(dash().updateProgress).toBeNull()
  })

  it('bumps the refresh trigger for a session restart and a refine frame', () => {
    const { ws } = mount()
    const before = dash().refreshTrigger
    act(() => {
      ws.simulateMessage({ type: 'sessions_restarting', data: { status: 'restarting' } })
      ws.simulateMessage({ type: 'refine', data: {} })
    })
    expect(dash().refreshTrigger).toBe(before + 2)
  })

  it('tracks aggregate subagent status and per-agent text', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'subagent_status', data: { slot: ACTIVE, running: 2, agents: [] } })
      ws.simulateMessage({ type: 'subagent_text', data: { slot: ACTIVE, id: 'ag-1', text: 'progress' } })
      // Neither arm dispatches without the identifying fields.
      ws.simulateMessage({ type: 'subagent_status', data: { running: 5 } })
      ws.simulateMessage({ type: 'subagent_text', data: { slot: ACTIVE, text: 'no id' } })
    })
    expect(dash().subagentRunning[ACTIVE]).toBe(2)
    expect(dash().subagentText[ACTIVE]?.['ag-1']).toBe('progress')
  })

  it('re-broadcasts every channel frame on one window event', () => {
    const { ws } = mount()
    const kinds: string[] = []
    const listener = (e: Event) => { kinds.push((e as CustomEvent).detail?.type) }
    window.addEventListener('kirocrew-channel', listener)
    try {
      act(() => {
        for (const type of [
          'channel_message', 'channel_agent_status', 'channel_created',
          'channel_closed', 'channel_agent_joined', 'channel_agent_left',
        ]) ws.simulateMessage({ type, data: { channel: 'c1' } })
      })
      expect(kinds).toEqual([
        'channel_message', 'channel_agent_status', 'channel_created',
        'channel_closed', 'channel_agent_joined', 'channel_agent_left',
      ])
    } finally {
      window.removeEventListener('kirocrew-channel', listener)
    }
  })

  it('re-broadcasts tool_call so the Browser panel can open on a browse', () => {
    // ChatPage listens for this to auto-open the panel when a shell call turns out
    // to be `playwright-cli`. Without the re-broadcast the listener is live code
    // that can never fire, which is exactly how the previous frame-based trigger
    // broke when its producer was removed.
    const { ws } = mount()
    const seen: unknown[] = []
    const onTool = (e: Event) => { seen.push((e as CustomEvent).detail) }
    window.addEventListener('kirocrew-tool-call', onTool)
    try {
      act(() => {
        ws.simulateMessage({
          type: 'tool_call',
          data: {
            slot: ACTIVE, tool: 'execute_bash', kind: 'tool', purpose: 'open a page',
            input_preview: 'playwright-cli open https://example.com', is_shell: true,
          },
        })
      })
      expect(seen).toHaveLength(1)
      expect((seen[0] as { input_preview: string }).input_preview).toContain('playwright-cli')
    } finally {
      window.removeEventListener('kirocrew-tool-call', onTool)
    }
  })

  it('re-broadcasts cron history and the computer-use mirror frame', () => {
    const { ws } = mount()
    const seen: string[] = []
    const push = (name: string) => () => { seen.push(name) }
    const onCron = push('cron')
    const onBrowser = push('browser')
    const onComputer = push('computer')
    window.addEventListener('cron_history', onCron)
    window.addEventListener('kirocrew-browser-frame', onBrowser)
    window.addEventListener('kirocrew-computer-use-frame', onComputer)
    try {
      act(() => {
        ws.simulateMessage({ type: 'cron_history', data: { job: 'j1' } })
        // No producer sends browser frames: the Browser panel frames the
        // Playwright CLI's own dashboard, so a frame arriving here would be a
        // message from a component that no longer exists.
        ws.simulateMessage({ type: 'browser_frame', data: { image: 'x' } })
        ws.simulateMessage({ type: 'computer_use_frame', data: { image: 'y' } })
      })
      expect(seen).toEqual(['cron', 'computer'])
    } finally {
      window.removeEventListener('cron_history', onCron)
      window.removeEventListener('kirocrew-browser-frame', onBrowser)
      window.removeEventListener('kirocrew-computer-use-frame', onComputer)
    }
  })

  it('patches the sidebar chip and the cached batch from a source_status delta', () => {
    const { ws } = mount()
    const url = 'https://github.com/o/r/pull/1'
    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [slotFixture(ACTIVE, { source_links: [{ url, state: 'open', ci: 'running' }] } as Partial<ChatSlot>)],
      })
    })
    act(() => {
      ws.simulateMessage({ type: 'source_status', data: { url, state: 'merged', ci: 'passed' } })
    })
    expect(dash().slots[0].source_links?.[0].state).toBe('merged')
    expect(dash().slots[0].source_links?.[0].ci).toBe('passed')

    // A delta that carries no usable url is dropped before any cache work.
    act(() => { ws.simulateMessage({ type: 'source_status', data: {} }) })
    expect(dash().slots[0].source_links?.[0].state).toBe('merged')
  })

  it('survives a notification listener that throws, on every arm that fires one', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const passThrough = window.dispatchEvent.bind(window)
    const dispatchSpy = vi.spyOn(window, 'dispatchEvent').mockImplementation((event: Event) => {
      if (event.type === 'mc-notification') throw new Error('listener exploded')
      return passThrough(event)
    })
    try {
      const { ws } = mount()
      act(() => {
        ws.simulateMessage({ type: 'notification', data: { kind: 'info', title: 'Boom', ts: '1' } })
        ws.simulateMessage({ type: 'approval', data: { id: 'ap-boom', slot: ACTIVE, tool: 'execute_bash', ts: 2 } })
        ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } })
      })
      // Each arm swallowed the listener failure and completed its real work.
      expect(warn).toHaveBeenCalledTimes(3)
      expect(testStore.getState().notifications.items).toHaveLength(2)
      expect(chat().slotStatusDetail[ACTIVE]?.kind).toBe('idle')
    } finally {
      dispatchSpy.mockRestore()
      warn.mockRestore()
    }
  })

  it('cancels the scheduled frame when a finalizing frame flushes synchronously', () => {
    const cancelSpy = vi.fn()
    vi.stubGlobal('cancelAnimationFrame', cancelSpy)
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: ACTIVE, content: 'buffered', seq: 1 } }) })
    expect(rafCbs).toHaveLength(1)

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(cancelSpy).toHaveBeenCalledWith(1)
    expect(chat().messages.find(m => m.role === 'assistant')?.content).toBe('buffered')
  })

  it('falls back to a timer when the environment has no animation frames', () => {
    vi.stubGlobal('requestAnimationFrame', undefined)
    vi.stubGlobal('cancelAnimationFrame', undefined)
    vi.useFakeTimers()
    try {
      const { ws } = mount()
      act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: ACTIVE, content: 'timed', seq: 1 } }) })
      expect(chat().messages.find(m => m.role === 'streaming')).toBeUndefined()
      act(() => { vi.advanceTimersByTime(16) })
      expect(chat().messages.find(m => m.role === 'streaming')?.content).toBe('timed')
    } finally {
      vi.useRealTimers()
    }
  })
})
