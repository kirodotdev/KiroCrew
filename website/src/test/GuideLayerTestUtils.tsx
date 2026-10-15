/**
 * The fixtures the GuideLayer*.test.tsx suites share: the guide record and
 * its claimed form, the MCP add-form action, the registered target with a
 * real-looking box, the location probe, the tracker's tick and waitFor, the
 * wall clock the tests start at and the gateway's progress transitions. No
 * module state: the state a test reassigns (pending, onWrite, qc, calls)
 * cannot be an import, so each test file declares it, with the helpers that
 * read or assign it (renderGuide, writes, startCrewmate) and the hooks that
 * reset it.
 */
import { vi } from 'vitest'
import { act, waitFor as rtlWaitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { TAB_ID } from '../api/tabId'
import { type Guide } from '../api/guide'
import { resolveGuideActions } from '../guide/guideActions'
import { GUIDE_TRACK_TICK_MS } from '../guide/useGuideStepTracker'
import { marks } from './guideTargets'

export async function tickTracker(ticks = 1) {
  await act(async () => { await vi.advanceTimersByTimeAsync(GUIDE_TRACK_TICK_MS * ticks) })
}
export const waitFor: typeof rtlWaitFor = (callback, options) => rtlWaitFor(async () => {
  await act(async () => { await vi.advanceTimersByTimeAsync(GUIDE_TRACK_TICK_MS) })
  return callback()
}, options)


export type Call = { path: string; body: Record<string, unknown>; headers: Headers }

export const guide = (over: Partial<Guide> = {}): Guide => ({
  guide_id: 'g1',
  slot_key: 'slot-A',
  status: 'offered',
  revision: 1,
  owner_tab: null,
  action_index: 0,
  step_index: 0,
  actions: [{ id: 'crewmate.create', params: { name: 'radar', goal: 'watch the build' } }],
  reason: 'You asked for a mate.',
  expires_at: null,
  lease_expires_at: null,
  ...over,
})

export const claimed = (g: Guide): Guide => ({ ...g, status: 'active', owner_tab: TAB_ID, revision: g.revision + 1 })

/** A UI-only action of several steps (the MCP add form): the fixture for a
 *  step that completes when the page moves on to a later registered control. */
export const MCP_OPEN = { id: 'mcp.open_add', params: {} }

export function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc" data-granted={(loc.state as { leaveGranted?: boolean } | null)?.leaveGranted ? '1' : '0'}>{loc.pathname + loc.search}</div>
}

/** A registered control with a real-looking box (happy-dom lays nothing out). */
export function Target({ anchor, rect = { top: 300, left: 100, width: 80, height: 30 }, testId, onClick }: {
  anchor?: string
  rect?: { top: number; left: number; width: number; height: number }
  testId?: string
  onClick?: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testId ?? `target-${anchor ?? 'none'}`} {...marks({ anchor: anchor }, (el) => {
        if (!el) return
        el.getBoundingClientRect = () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height, x: rect.left, y: rect.top, toJSON: () => ({}) }) as DOMRect
      })}
    >
      target
    </button>
  )
}

/** The wall clock a test that moves time past a bound starts at. */
export const T0 = Date.UTC(2026, 0, 15, 12)

/** The gateway's progress transitions, including recovery in place. */
export const gatewayProgress = (current: Guide, b: Record<string, unknown>): Guide => {
  const next = { ...current, revision: current.revision + 1 }
  if (b.outcome === 'observed') {
    const resolved = resolveGuideActions(current.actions)
    const steps = resolved.ok ? resolved.actions[current.action_index]?.steps.length ?? 0 : 0
    const step = (b.step_index as number) + 1
    return step < steps
      ? { ...next, status: 'active', step_index: step }
      : { ...next, status: 'active', action_index: current.action_index + 1, step_index: 0 }
  }
  if (b.outcome === 'target_missing') return { ...next, status: 'target_missing' }
  if (current.status !== 'target_missing') return current
  const resume = typeof b.resume_step_index === 'number' ? { step_index: b.resume_step_index } : {}
  return { ...next, status: 'active', ...resume }
}
