/**
 * Isolated capture entry for the Context tab reading pushed frames instead of polling.
 *
 * WHY ISOLATED: the trigger is a `session_projection` frame on the dashboard socket,
 * and a full SPA needs a gateway, a live socket and a session with turns. Here the
 * REAL tab (`ContextBreakdownTab`) and the REAL frame handler (`onUsageFrame`, the
 * one `useWebSocket` calls) run unchanged; only the REST endpoint is answered by a
 * stub that counts its reads and grows by one turn per read. The strip above the
 * tab shows that count, so a frame can prove "one frame, one read".
 *
 * The page exposes `window.__ctx` so the capture script drives the scenes:
 * `frame(revision, unit?)` delivers a usage frame, `reconnect()` does what the
 * socket's open handler does on a reopen. Theme: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { useSyncExternalStore } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import { newQueryClient } from '../src/api/queryClient'
import { ContextBreakdownTab, type ContextTrace } from '../src/pages/ContextBreakdownPanel'
import {
  onUsageFrame,
  rereadAllContextTraces,
  resetContextTraceRefresh,
} from '../src/hooks/useWebSocket'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'chat-demo'
const BLOCKS = [
  { your_message: 900, critical_rules: 4200, lessons: 3100, memory: 1800, skill_index: 2400, surface: 300 },
  { your_message: 1400, critical_rules: 4200, lessons: 3100, semantic_memory: 2600, surface: 300 },
  { your_message: 600, critical_rules: 4200, lessons: 3100, loaded_skill: 7200, surface: 300 },
  { your_message: 2100, critical_rules: 4200, lessons: 3100, episodic_memory: 1900, surface: 300 },
  { your_message: 800, critical_rules: 4200, lessons: 3100, task_facts: 1200, surface: 300 },
]

/** What the strip shows. A tiny store so the strip re-renders on every change. */
const log = { reads: 0, last: 'mounted', listeners: new Set<() => void>() }
function note(last: string) {
  log.last = last
  for (const fn of log.listeners) fn()
}
function useLog() {
  return useSyncExternalStore(
    fn => { log.listeners.add(fn); return () => log.listeners.delete(fn) },
    () => `${log.reads}|${log.last}`,
  )
}

function traceWith(turns: number): ContextTrace {
  const rows = Array.from({ length: turns }, (_, i) => {
    const blocks = BLOCKS[i % BLOCKS.length]
    return {
      ts: `2026-10-04T05:${String(10 + i).padStart(2, '0')}:00Z`,
      phase: i === 0 ? 'session_start' : 'turn',
      blocks,
      total_chars: Object.values(blocks).reduce((a, b) => a + b, 0),
      context_used: 18_000 + i * 2_500,
      context_window: 200_000,
      model: 'claude-opus',
      ordinal: i + 1,
    }
  })
  const totals: Record<string, number> = {}
  for (const row of rows) for (const [k, v] of Object.entries(row.blocks)) totals[k] = (totals[k] ?? 0) + v
  return {
    slot: SLOT, turns: rows, totals,
    injected_chars: Object.values(totals).reduce((a, b) => a + b, 0) - (totals.your_message ?? 0),
    user_chars: totals.your_message ?? 0,
    peak_context_used: 18_000 + (turns - 1) * 2_500, context_window: 200_000, window_days: 14,
  }
}

// The REST stub: each read is counted and the session has grown by one turn.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/telemetry/context-trace')) {
    log.reads += 1
    note(log.last)
    return Promise.resolve(new Response(JSON.stringify(traceWith(Math.min(log.reads + 1, 5))), {
      status: 200, headers: { 'content-type': 'application/json' },
    }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = newQueryClient()

declare global {
  interface Window {
    __ctx: { frame: (revision: number, unit?: string) => string; reconnect: () => void; reads: () => number; mark: (text: string) => void }
  }
}
window.__ctx = {
  frame(revision, unit = 'unit-1') {
    const outcome = onUsageFrame(queryClient, {
      unit, slot: `dashboard:${SLOT}`, name: 'usage', seq: revision, value: {}, revision,
    })
    note(`usage frame rev ${revision} (${unit}) → ${outcome}`)
    return outcome
  },
  reconnect() {
    resetContextTraceRefresh()
    rereadAllContextTraces(queryClient)
    note('socket reopened (gateway restart) → re-read')
  },
  reads: () => log.reads,
  mark: (text: string) => note(text),
}

function Strip() {
  const [reads, last] = useLog().split('|')
  return (
    <div data-testid="capture-strip" className="font-mono text-[12px] px-3 py-2 mb-3 rounded-lg border border-border bg-card">
      <span data-testid="reads">REST reads: {reads}</span>
      <span className="text-muted"> · last event: </span>
      <span data-testid="last">{last}</span>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <div data-capture-root style={{ background: 'var(--bg)', color: 'var(--text)', padding: 16, width: 520 }}>
      <Strip />
      <div style={{ height: 620 }}>
        <ContextBreakdownTab slot={SLOT} />
      </div>
    </div>
  </QueryClientProvider>,
)
