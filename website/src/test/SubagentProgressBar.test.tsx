import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentPending, sseSubagentQueued, sseSubagentDone, sseSubagentTool, sseSubagentStalled } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnStopAll: vi.fn().mockResolvedValue({}),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import { api } from '../api/client'
import { OVERLAY_Z_MAX } from '../lib/themeDecorLayer'

const SLOT = 'test-slot'

/** Build a store with `running` running agents + optionally one pending agent, all in SLOT. */
function makeStore(running: string[], pending?: string) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  running.forEach(id => store.dispatch(sseSubagentSpawn({ slot: SLOT, id, task: `task ${id}`, agent: `agent-${id}` })))
  if (pending) store.dispatch(sseSubagentPending({ slot: SLOT, id: pending, task: `task ${pending}`, approval_id: `appr-${pending}` }))
  return store
}

function renderBar(store: ReturnType<typeof makeStore>) {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <SubagentProgressBar slot={SLOT} />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('SubagentProgressBar — in-chat stop controls', () => {
  beforeEach(() => vi.clearAllMocks())

  it('stops a single running agent from its per-row button (excludes pending from stop-all)', () => {
    // 1 running + 1 pending: only the running agent is stoppable.
    renderBar(makeStore(['a1'], 'p1'))
    // Header reflects the total active count (running + pending).
    // Exactly one per-row stop button (the running agent, not the pending one).
    const rowStops = screen.getAllByLabelText(/^Stop subagent/)
    expect(rowStops).toHaveLength(1)
    fireEvent.click(rowStops[0])
    expect(api.spawnDelete).toHaveBeenCalledTimes(1)
    expect(api.spawnDelete).toHaveBeenCalledWith('a1')
  })

  it('"Stop all" sends one session-scoped bulk stop for running work', async () => {
    renderBar(makeStore(['a1', 'a2'], 'p1'))
    fireEvent.click(screen.getByLabelText('Stop all'))
    await waitFor(() => expect(api.spawnStopAll).toHaveBeenCalledTimes(1))
    expect(api.spawnStopAll).toHaveBeenCalledWith(SLOT)
    expect(api.spawnDelete).not.toHaveBeenCalled()
  })

  it('labels the header stop control "Stop" (not "Stop all") when exactly one agent is stoppable', () => {
    renderBar(makeStore(['a1']))
    expect(screen.getByLabelText('Stop running subagent')).toBeInTheDocument()
    expect(screen.queryByLabelText('Stop all running subagents')).not.toBeInTheDocument()
  })

  it('excludes native (nested) kiro-cli subagents from the count and rows so it matches "spawned N"', async () => {
    // 2 top-level managed agents + 2 native:* nested agents surfaced from the
    // kiro-cli list_update. The chip must show only the 2 managed ones.
    renderBar(makeStore(['a1', 'a2', 'native:sess-x', 'native:sess-y']))
    // Running histogram counts managed only.
    expect(screen.getByTestId('subagent-running-count')).toHaveTextContent('2')
    // Exactly two rows, both managed; no native task rows.
    const rows = screen.getAllByTestId('subagent-row')
    expect(rows).toHaveLength(2)
    // "Stop all" delegates the managed running/queue scope to the backend;
    // native nested cards are not part of SubagentManager.
    fireEvent.click(screen.getByLabelText('Stop all'))
    await waitFor(() => expect(api.spawnStopAll).toHaveBeenCalledTimes(1))
    expect(api.spawnStopAll).toHaveBeenCalledWith(SLOT)
    expect(api.spawnDelete).not.toHaveBeenCalled()
  })

  it('renders no stop controls when every active agent is pending (stoppableCount === 0)', () => {
    renderBar(makeStore([], 'p1'))
    // The pending agent still shows in the header, but offers no stop affordance.
    expect(screen.queryByLabelText(/^Stop/)).toBeNull()
    expect(api.spawnDelete).not.toHaveBeenCalled()
  })
})

describe('SubagentProgressBar — queued / waiting count', () => {
  beforeEach(() => vi.clearAllMocks())

  it('mounts on a queued-only wave (no running agents yet) and shows the waiting count', () => {
    // Nothing has started (no subagent_spawn) — the wave is only queued.
    // The chip must still appear so the user gets an immediate signal.
    const store = makeStore([])
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 5 }))
    renderBar(store)
    const queued = screen.getByTestId('subagent-queued-count')
    expect(queued).toBeInTheDocument()
    expect(queued.textContent).toContain('5')
    // running count is present and zero
    expect(screen.getByTestId('subagent-running-count').textContent).toContain('0')
  })

  it('stays mounted across the staggered ramp when running momentarily hits zero but agents remain queued', () => {
    const store = makeStore(['a1'])
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 3 }))
    // The only running agent finishes, but 3 are still queued.
    store.dispatch(sseSubagentDone({ slot: SLOT, id: 'a1', elapsed: 2, outcome: 'completed' }))
    renderBar(store)
    // Chip is still present (did not unmount) and shows the waiting count.
    expect(screen.getByTestId('subagent-histogram')).toBeInTheDocument()
    expect(screen.getByTestId('subagent-queued-count').textContent).toContain('3')
  })

  it('offers Stop all for a queued-only wave and stops the queue by slot', async () => {
    const store = makeStore([])
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 3 }))
    renderBar(store)
    fireEvent.click(screen.getByLabelText('Stop all'))
    await waitFor(() => expect(api.spawnStopAll).toHaveBeenCalledTimes(1))
    expect(api.spawnStopAll).toHaveBeenCalledWith(SLOT)
  })

  it('hides the waiting segment once the queue drains to zero', () => {
    const store = makeStore(['a1'])
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 2 }))
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 0 }))
    renderBar(store)
    expect(screen.queryByTestId('subagent-queued-count')).toBeNull()
  })

  it('unmounts entirely when nothing is running and nothing is queued', () => {
    const store = makeStore([])
    const { container } = renderBar(store)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('SubagentProgressBar — overlay stacking', () => {
  beforeEach(() => vi.clearAllMocks())

  // Theme-experience overlays portal into the shell's decor slot, pinned at
  // OVERLAY_Z_MAX (lib/themeDecorLayer.ts) — the chip shares that stacking
  // context, so its z must sit strictly above the ceiling for no theme to paint
  // over an active wave. Read the constant rather than restating it: a restated
  // number is exactly the drift #7377 was about.
  it('elevates the wave chip above the theme-overlay ceiling (relative + z-[46])', () => {
    const { container } = renderBar(makeStore(['a1']))
    const wrapper = container.firstChild as HTMLElement
    expect(wrapper).toHaveClass('relative')
    const z = Number(/\bz-\[(\d+)\]/.exec(wrapper.className)?.[1])
    expect(z).toBe(46)
    expect(z).toBeGreaterThan(OVERLAY_Z_MAX)
  })
})

describe('sseSubagentQueued reducer', () => {
  function freshStore() {
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    store.dispatch(setActiveSlot(SLOT))
    return store
  }

  it('stores the queued count keyed by slot', () => {
    const store = freshStore()
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 4 }))
    expect(store.getState().chat.subagentQueued[SLOT]).toBe(4)
  })

  it('deletes the entry when count reaches zero (keeps the map clean)', () => {
    const store = freshStore()
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 4 }))
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 0 }))
    expect(store.getState().chat.subagentQueued[SLOT]).toBeUndefined()
  })

  it('clamps negative / garbage payloads to a non-negative integer', () => {
    const store = freshStore()
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: -3 as unknown as number }))
    expect(store.getState().chat.subagentQueued[SLOT]).toBeUndefined()
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 2.9 }))
    expect(store.getState().chat.subagentQueued[SLOT]).toBe(2)
  })
})

/** The wave chip is CHROME: prose and labels must follow the user's Font Family
 *  choice (`--font-body`), while the code-shaped fragments keep monospace
 *  explicitly. Tailwind's `font-mono` resolves to `var(--mono)`, which the Font
 *  Family setting never writes — so any `font-mono` on a prose element pins
 *  JetBrains Mono regardless of the setting, and that is what these assert
 *  against. The class is the observable here (jsdom applies no stylesheet), so
 *  each case checks the class on the SPECIFIC element that renders the text. */
describe('SubagentProgressBar — chrome follows the Font Family setting', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps prose chrome off font-mono while the tree glyph and counter keep it', () => {
    const store = makeStore(['a1'])
    store.dispatch(sseSubagentTool({ slot: SLOT, id: 'a1', tool: 'gh pr list --state all', tool_count: 5 }))
    const { container } = renderBar(store)

    // The task-preview row: prose, must inherit --font-body.
    const row = screen.getByLabelText(/^Open task a1 in subagents sidebar$/)
    expect(row.className).not.toContain('font-mono')

    // The histogram header's own container is prose/labels too.
    const header = screen.getByTestId('subagent-histogram').parentElement!
    expect(header.className).not.toContain('font-mono')

    // Box-drawing glyph: mono, so `├─` and `└─` keep one advance width.
    const glyph = [...container.querySelectorAll('span')].find(s => s.textContent === '└─')
    expect(glyph).toBeTruthy()
    expect(glyph!.className).toContain('font-mono')

    // Elapsed / tool counter: mono + tabular-nums so the column does not jitter.
    // Anchored match — the enclosing flex row's textContent also ENDS with this,
    // so an unanchored regex picks up the wrapper instead of the counter itself.
    const counter = [...container.querySelectorAll('span')].find(s => /^\d+s · 5 tools$/.test(s.textContent || ''))
    expect(counter).toBeTruthy()
    expect(counter!.className).toContain('font-mono')

    // The tool command IS code.
    const cmd = [...container.querySelectorAll('span')].find(s => s.textContent === '→ gh pr list --state all')
    expect(cmd).toBeTruthy()
    expect(cmd!.className).toContain('font-mono')
  })

  it('monospaces the tool name on the STALLED path too, matching the running path', () => {
    // Regression guard: the stalled line interpolates the same `lastTool` value
    // as the running line. Each path must monospace it itself, since the row
    // does not supply mono.
    const store = makeStore(['a1'])
    store.dispatch(sseSubagentTool({ slot: SLOT, id: 'a1', tool: 'npx vitest run', tool_count: 3 }))
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: 'a1', stalled: true }))
    const { container } = renderBar(store)

    // The tool name is now its OWN span, interpolated into a single translated
    // sentence rather than glued together with a hardcoded English " at " --
    // so the fragment holds the bare value, not the value plus its preposition.
    // The requirement being guarded is unchanged: this value renders mono.
    const toolFragment = [...container.querySelectorAll('span')]
      .find(s => s.textContent === 'npx vitest run')
    expect(toolFragment).toBeTruthy()
    expect(toolFragment!.className).toContain('font-mono')

    // The surrounding stalled prose is NOT monospaced.
    expect(toolFragment!.parentElement!.className).not.toContain('font-mono')
    // And the sanitised value is still what gets rendered — the whole line reads
    // as one sentence with exactly one space before `at`.
    expect(toolFragment!.parentElement!.textContent).toContain('at npx vitest run')
  })

  it('hedges the stalled copy and shows the IDLE span, not total elapsed', () => {
    // The watchdog observes an ABSENCE of stream events, which a slow silent
    // tool also produces — so the row must not assert a stall, and the number
    // beside the warning must be the idle span that justifies it (the row's
    // other figure, `elapsed`, is total runtime and is a different number).
    const store = makeStore(['a1'])
    store.dispatch(sseSubagentTool({ slot: SLOT, id: 'a1', tool: 'Reading retrieval.py:1', tool_count: 7 }))
    store.dispatch(sseSubagentStalled({
      slot: SLOT, id: 'a1', stalled: true, idle_secs: 196,
    }))
    const { container } = renderBar(store)

    const warn = [...container.querySelectorAll('span.text-warn')]
      .find(s => s.textContent?.includes('possibly stalled'))
    expect(warn).toBeTruthy()
    expect(warn!.textContent).toContain('no activity for 196s')
    // Hedged, not asserted: the bare "stalled ... — no activity" wording is gone.
    expect(warn!.textContent).not.toMatch(/(^|[^y] )stalled at/)
  })

  it('advances the idle figure while stalled instead of freezing it', async () => {
    // The backend emits `idle_secs` ONCE on the stalled transition. Rendering it
    // verbatim would freeze at that value beside the live elapsed counter, so a
    // subagent wedged for 20 minutes would still read "no activity for 196s" —
    // a milder form of the contradiction this row exists to remove.
    vi.useFakeTimers()
    try {
      const store = makeStore(['a1'])
      store.dispatch(sseSubagentTool({ slot: SLOT, id: 'a1', tool: 'sleep 600', tool_count: 1 }))
      store.dispatch(sseSubagentStalled({ slot: SLOT, id: 'a1', stalled: true, idle_secs: 196 }))
      const { container } = renderBar(store)

      const idleText = () => [...container.querySelectorAll('span.text-warn')]
        .find(s => s.textContent?.includes('possibly stalled'))?.textContent ?? ''
      expect(idleText()).toContain('no activity for 196s')

      // Two 1Hz ticks later the figure has moved with the clock.
      // Advance ONE tick per act() flush. The component's 1Hz tick is a toggle
      // (`setTick(n => 1 - n)`), so jumping two intervals inside a single flush
      // lands back on the original value and React bails out of the re-render —
      // an artifact of batching, not a product bug (real ticks arrive singly).
      await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
      expect(idleText()).toContain('no activity for 197s')
      await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
      expect(idleText()).toContain('no activity for 198s')
    } finally {
      vi.useRealTimers()
    }
  })

  it('falls back to the plain no-activity wording when no idle span was sent', () => {
    // Reconnect replays from a pre-upgrade gateway carry no idle_secs; the row
    // must still render hedged copy rather than a bare "196s"-shaped gap.
    const store = makeStore(['a1'])
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: 'a1', stalled: true }))
    const { container } = renderBar(store)
    const warn = [...container.querySelectorAll('span.text-warn')]
      .find(s => s.textContent?.includes('possibly stalled'))
    expect(warn).toBeTruthy()
    expect(warn!.textContent).toContain('no activity')
    expect(warn!.textContent).not.toContain('no activity for')
  })
})

describe('SubagentProgressBar — collapse toggle', () => {
  beforeEach(() => { vi.clearAllMocks(); try { localStorage.clear() } catch { /* not available in this env */ } })

  it('defaults to expanded, showing the agent list', () => {
    renderBar(makeStore(['a1', 'a2']))
    // Open state offers the collapse affordance.
    expect(screen.getByLabelText('Collapse subagent list')).toBeInTheDocument()
    // The list container is not hidden.
    const listContainer = screen.getAllByTestId('subagent-row')[0].parentElement!
    expect(listContainer.className).not.toContain('hidden')
  })

  it('collapsing hides the list while keeping the count + Stop all in the header', () => {
    renderBar(makeStore(['a1', 'a2']))
    fireEvent.click(screen.getByLabelText('Collapse subagent list'))
    // Affordance flips to expand.
    expect(screen.getByLabelText('Expand subagent list')).toBeInTheDocument()
    // The list container is now hidden (rows stay in the DOM, just not shown).
    const listContainer = screen.getAllByTestId('subagent-row')[0].parentElement!
    expect(listContainer.className).toContain('hidden')
    // Header still carries the running count and the stop-all control.
    expect(screen.getByTestId('subagent-running-count')).toHaveTextContent('2')
    expect(screen.getByLabelText('Stop all')).toBeInTheDocument()
  })

  it('lets "Stop all" work while collapsed', async () => {
    renderBar(makeStore(['a1', 'a2']))
    fireEvent.click(screen.getByLabelText('Collapse subagent list'))
    fireEvent.click(screen.getByLabelText('Stop all'))
    await waitFor(() => expect(api.spawnStopAll).toHaveBeenCalledTimes(1))
    expect(api.spawnStopAll).toHaveBeenCalledWith(SLOT)
  })
})
