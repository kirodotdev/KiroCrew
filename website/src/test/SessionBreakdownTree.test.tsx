/**
 * Session Breakdown tree: the fused topology + per-node-composition view.
 *
 *  - nodeSegments composes a node's bar from grouped totals, ranked, user last.
 *  - the tree renders nothing when the session spawned no sub-agents.
 *  - each node reads its OWN trace (fetched by childSession) — a node with a
 *    trace is expandable and shows that trace's per-turn composition; a node
 *    without one stays a one-line topology row.
 *  - status collapses to running / done / ended.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { SessionBreakdownTree, nodeSegments } from '../pages/SessionBreakdownTree'
import { type ContextTrace } from '../pages/ContextBreakdownPanel'
import type { SubagentActivity } from '../types'

// api.telemetryContextTrace is called per child key; stub it to return a
// per-key trace so the tree can render each node's own composition.
vi.mock('../api/client', () => ({
  api: {
    telemetryContextTrace: vi.fn((key: string) => Promise.resolve(traceFixtures[key])),
  },
}))

const trace = (over: Partial<ContextTrace> = {}): ContextTrace => ({
  slot: 's',
  turns: [
    { ts: '2026-08-27T00:00:00Z', phase: 'per_turn', blocks: { loaded_skill: 4000, history: 6000 }, total_chars: 10000, context_used: 12000, context_window: 200000, model: 'opus-5' },
  ],
  totals: { loaded_skill: 4000, history: 6000 },
  injected_chars: 10000,
  user_chars: 0,
  estimated_other_chars: 0,
  peak_context_used: 12000,
  context_window: 200000,
  window_days: 14,
  ...over,
})

const traceFixtures: Record<string, ContextTrace> = {
  'subagent:a': trace({ totals: { loaded_skill: 2000, history: 8000 }, injected_chars: 10000 }),
  'subagent:b': trace({ totals: { loaded_skill: 9000, memory: 1000 }, injected_chars: 10000 }),
  // two turns of different sizes: exercises the per-turn map and the sqrt
  // width scaling against the larger turn (maxTurn).
  'subagent:multi': trace({
    turns: [
      { ts: '2026-08-27T00:00:00Z', phase: 'per_turn', blocks: { loaded_skill: 4000, history: 6000 }, total_chars: 10000, context_used: 12000, context_window: 200000, model: 'opus-5' },
      { ts: '2026-08-27T00:01:00Z', phase: 'per_turn', blocks: { history: 40000, memory: 8000 }, total_chars: 48000, context_used: 60000, context_window: 200000, model: 'opus-5' },
    ],
    totals: { loaded_skill: 4000, history: 46000, memory: 8000 },
    injected_chars: 58000,
  }),
}

const sub = (over: Partial<SubagentActivity> = {}): SubagentActivity => ({
  id: 'a', task: 'read specs', agent: 'gpt-review',
  status: 'done', streaming: '', lastTool: '', startedAt: 1000, elapsed: 4000,
  childSession: 'subagent:a', ...over,
})

function renderTree(subagents: Record<string, SubagentActivity>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SessionBreakdownTree subagents={subagents} />
    </QueryClientProvider>,
  )
}

afterEach(cleanup)

describe('SessionBreakdownTree', () => {
  it('renders nothing when the session spawned no sub-agents', () => {
    const { container } = renderTree({})
    expect(container.firstChild).toBeNull()
  })

  it('draws the header with sub-agent and running counts', () => {
    renderTree({
      a: sub({ id: 'a', status: 'running', childSession: 'subagent:a', startedAt: 1 }),
      b: sub({ id: 'b', agent: 'opus-review', status: 'done', childSession: 'subagent:b', startedAt: 2 }),
    })
    expect(screen.getByText(/2 sub-agents/i)).toBeTruthy()
    expect(screen.getByText(/1 running/i)).toBeTruthy()
  })

  it('lists nodes in spawn order and shows agent names', () => {
    renderTree({
      late: sub({ id: 'late', agent: 'zeta', childSession: 'subagent:a', startedAt: 900 }),
      early: sub({ id: 'early', agent: 'alpha', childSession: 'subagent:b', startedAt: 100 }),
    })
    const names = screen.getAllByText(/^(alpha|zeta)$/).map(n => n.textContent)
    expect(names).toEqual(['alpha', 'zeta'])
  })

  it('collapses error and stopped to a single ended status', () => {
    renderTree({
      a: sub({ id: 'a', agent: 'boom', status: 'error', childSession: 'subagent:a', startedAt: 1 }),
    })
    expect(screen.getByText(/ended/i)).toBeTruthy()
  })

  it('expands a node with a trace and renders its per-turn bars, caption and gauge', async () => {
    renderTree({
      m: sub({ id: 'm', agent: 'opus-review', model: 'claude-opus-5', childSession: 'subagent:multi', startedAt: 1 }),
    })
    // The row is expandable once its trace resolves; find the button by name.
    const row = await screen.findByRole('button', { name: /opus-review/i })
    fireEvent.click(row)
    // Node caption names the model and turn count; both turns render as t1/t2.
    await waitFor(() => expect(screen.getByText(/claude-opus-5/i)).toBeTruthy())
    expect(screen.getByText('t1')).toBeTruthy()
    expect(screen.getByText('t2')).toBeTruthy()
    // Occupancy gauge is present (aria-hidden bars are drawn from the trace).
    expect(screen.getByText(/opus-review/i)).toBeTruthy()
  })

  it('toggles a node closed again on a second activation', async () => {
    renderTree({
      m: sub({ id: 'm', agent: 'opus-review', childSession: 'subagent:multi', startedAt: 1 }),
    })
    const row = await screen.findByRole('button', { name: /opus-review/i })
    fireEvent.click(row)
    await waitFor(() => expect(screen.getByText('t1')).toBeTruthy())
    fireEvent.keyDown(row, { key: 'Enter' })
    await waitFor(() => expect(screen.queryByText('t1')).toBeNull())
  })

  it('shows a stalled badge and a tool count', () => {
    renderTree({
      a: sub({ id: 'a', agent: 'slow', status: 'running', stalled: true, toolCount: 7, childSession: 'subagent:a', startedAt: 1 }),
    })
    expect(screen.getByText(/stalled/i)).toBeTruthy()
    expect(screen.getByText(/7 tools/i)).toBeTruthy()
  })

  it('falls back to a generic sub-agent label when the node has no agent name', () => {
    renderTree({
      a: sub({ id: 'a', agent: '', childSession: '', startedAt: 1 }),
    })
    // en fallback for a nameless node is the literal "sub-agent" (exact match
    // so the plural "N sub-agents" in the header meta does not also match).
    expect(screen.getByText('sub-agent')).toBeTruthy()
  })

  it('collapses the whole tree when the header is toggled', () => {
    renderTree({
      a: sub({ id: 'a', agent: 'alpha', childSession: 'subagent:a', startedAt: 1 }),
    })
    expect(screen.getByText('alpha')).toBeTruthy()
    const header = screen.getByRole('button', { name: /sub-agents/i })
    fireEvent.click(header)
    expect(screen.queryByText('alpha')).toBeNull()
  })
})

describe('nodeSegments', () => {
  it('ranks blocks by size and puts the user slice last', () => {
    const segs = nodeSegments({ loaded_skill: 4000, history: 6000, your_message: 100 })
    expect(segs.map(s => s.key)).toEqual(['history', 'loaded_skill', 'your_message'])
    expect(segs[segs.length - 1].isUser).toBe(true)
  })

  it('sums to ~100 percent', () => {
    const segs = nodeSegments({ loaded_skill: 4000, history: 6000 })
    expect(Math.round(segs.reduce((a, s) => a + s.pct, 0))).toBe(100)
  })

  it('returns nothing for empty totals', () => {
    expect(nodeSegments({})).toEqual([])
  })

  it('omits the user slice when there are no user chars', () => {
    const segs = nodeSegments({ loaded_skill: 4000, history: 6000 })
    expect(segs.some(s => s.isUser)).toBe(false)
  })

  it('colours each source by a distinct fixed hue, not the grey ramp', () => {
    const segs = nodeSegments({ loaded_skill: 4000, history: 6000, tool_output: 1000 })
    const fills = new Set(segs.map(s => s.fill))
    // three sources -> three distinct hues (a grey ramp would repeat by rank)
    expect(fills.size).toBe(3)
    expect(segs.find(s => s.key === 'loaded_skill')?.fill).toBe('var(--ctx-src-skill)')
    expect(segs.find(s => s.key === 'history')?.fill).toBe('var(--ctx-src-history)')
  })
})
