/**
 * Tests for the Mochi dashboard page's data shaping and its landing state.
 *
 * The plan-task switch is enumerated on purpose: a missed task type does not
 * throw, it renders a blank row or a raw JSON blob, so nothing but a per-type
 * assertion catches it.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  formatActivity,
  formatPlanTasks,
  formatWatchItems,
  statusTone,
  str,
} from '../dashboardData'

describe('dashboardData.str', () => {
  it('renders nullish as an em dash rather than "null"', () => {
    expect(str(null)).toBe('—')
    expect(str(undefined)).toBe('—')
  })

  it('stringifies objects instead of letting them reach JSX', () => {
    // A nested object in JSX is React error #31 — it takes the page down. The
    // plan is agent-authored JSON, so this is a live risk, not a formality.
    expect(str({ a: 1 })).toBe('{"a":1}')
  })

  it('passes numbers and booleans through', () => {
    expect(str(0)).toBe('0')
    expect(str(false)).toBe('false')
  })
})

describe('formatPlanTasks', () => {
  const at = '2026-07-30T10:30:00Z'

  it('drops tasks with no execute_after', () => {
    const rows = formatPlanTasks({
      tasks: [
        { type: 'notify', action: { message: 'timed' }, execute_after: at },
        { type: 'notify', action: { message: 'untimed' } },
      ],
    })
    expect(rows.map((r) => r.action)).toEqual(['timed'])
  })

  it('describes every task type the planner can emit', () => {
    const rows = formatPlanTasks({
      tasks: [
        { type: 'notify', action: { message: 'build failed' }, execute_after: at },
        { type: 'reminder', action: { title: 'stretch' }, execute_after: at },
        { type: 'mood', action: { value: 'happy' }, execute_after: at },
        { type: 'move', action: { behavior: 'peek_left' }, execute_after: at },
        { type: 'move', action: { x: 10, y: 20 }, execute_after: at },
        { type: 'watch', action: { label: 'CR-1' }, execute_after: at },
        { type: 'freestyle', action: { message: 'think' }, execute_after: at },
      ],
    })
    expect(rows.map((r) => r.action)).toEqual([
      'build failed',
      'stretch',
      'Mood → happy',
      'peek left',
      'wander',
      'CR-1',
      'think',
    ])
  })

  it('never renders a raw object for an unknown type with an opaque action', () => {
    const rows = formatPlanTasks({
      tasks: [{ type: 'brand_new', action: { nested: { deep: 1 } }, execute_after: at }],
    })
    // Falls back to the type name — readable, and crucially not [object Object].
    expect(rows[0].action).toBe('brand_new')
  })

  it('marks done tasks and tolerates a missing tasks array', () => {
    expect(formatPlanTasks({ tasks: [{ execute_after: at, done: true }] })[0].done).toBe(true)
    expect(formatPlanTasks(undefined)).toEqual([])
    expect(formatPlanTasks({ note: 'no plan yet' })).toEqual([])
  })
})

describe('formatActivity', () => {
  const entries = [
    { ts: '2026-07-30T08:00:00Z', type: 'notification', content: 'older' },
    { ts: '2026-07-30T09:00:00Z', type: 'memory', content: 'newer' },
    { ts: '2026-07-30T09:30:00Z', type: 'session_restore', content: 'noise' },
  ]

  it('sorts newest first and drops bookkeeping entries', () => {
    expect(formatActivity(entries).map((r) => r.content)).toEqual(['newer', 'older'])
  })

  it('leads with the narrative so the card shows the most current line', () => {
    const rows = formatActivity(entries, 'watching a build')
    expect(rows[0]).toMatchObject({ time: 'now', type: 'plan', content: 'watching a build' })
  })

  it('does not prepend a placeholder narrative', () => {
    expect(formatActivity(entries, '—')[0].content).toBe('newer')
    expect(formatActivity(entries, 'Active')[0].content).toBe('newer')
  })

  it('truncates long content', () => {
    const long = 'x'.repeat(400)
    const rows = formatActivity([{ ts: '2026-07-30T08:00:00Z', type: 'a', content: long }])
    expect(rows[0].content).toHaveLength(150)
  })
})

describe('formatWatchItems / statusTone', () => {
  it('falls back to a synthetic id so rows always have a stable key', () => {
    const rows = formatWatchItems([{ label: 'no id' } as never])
    expect(rows[0].id).toBe('wl-0')
  })

  it('tones terminal-good, failed and in-flight statuses apart', () => {
    expect(statusTone('watching')).toBe('ok')
    expect(statusTone('done')).toBe('ok')
    expect(statusTone('failed')).toBe('err')
    expect(statusTone('triggered')).toBe('warn')
  })

  it('keeps cancelled items listed and flagged, so the cancel is recoverable', () => {
    // Cancelling is one unconfirmed click on a row. Dropping the item made that
    // click unrecoverable for anyone without the desktop panel, so the row stays
    // and carries the flag the Reopen affordance keys off.
    const rows = formatWatchItems([
      { id: 'a', label: 'live', status: 'watching' },
      { id: 'b', label: 'oops', status: 'cancelled' },
    ] as never)
    expect(rows.map((r) => r.id)).toEqual(['a', 'b'])
    expect(rows.find((r) => r.id === 'b')?.cancelled).toBe(true)
    expect(rows.find((r) => r.id === 'a')?.cancelled).toBe(false)
  })

  it('still drops items that reached a natural end', () => {
    const rows = formatWatchItems([
      { id: 'd', label: 'finished', status: 'done' },
      { id: 'e', label: 'broke', status: 'failed' },
      { id: 'f', label: 'aged out', status: 'expired' },
    ] as never)
    expect(rows).toEqual([])
  })
})

// ── Landing state ───────────────────────────────────────────────────────────

function renderPage(Comp: React.ComponentType) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Comp />
    </QueryClientProvider>
  )
}

describe('MochiPage landing state', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('shows the sleeping landing with an Enable action when the app is disabled', async () => {
    // 403 is the backend guard's "disabled" answer — the page must read that as
    // an offerable state, not as an outage.
    ;(fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 403,
    })
    const { default: MochiPage } = await import('../MochiPage')
    renderPage(MochiPage)
    await waitFor(() => expect(screen.getByText('Mochi is sleeping')).toBeTruthy())
    expect(screen.getByRole('button', { name: /Enable Mochi/ })).toBeTruthy()
  })

  it('omits the Enable action while the runtime is merely starting', async () => {
    // 503 means already enabled, runtime not up yet — enabling again is a no-op
    // and would tell the user the wrong story about what is wrong.
    ;(fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 503,
    })
    const { default: MochiPage } = await import('../MochiPage')
    renderPage(MochiPage)
    await waitFor(() => expect(screen.getByText('Mochi is sleeping')).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Enable Mochi/ })).toBeNull()
  })
})
