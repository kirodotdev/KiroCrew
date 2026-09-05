import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { SESSION_LANES, inferLane, type LaneSlotFields } from '../pages/chat/sessionLane'

/** The two properties the board depends on, asserted directly rather than
 *  inferred from the rule list: a card that matches no lane vanishes, and a card
 *  that matches two is rendered twice and double-counted. */
describe('inferLane exhaustiveness and exclusivity', () => {
  const FIELDS: (keyof LaneSlotFields)[] = [
    'pending_approval', 'needs_input', 'has_options', 'interrupted',
    'running', 'orchestrating', 'subagents_running',
  ]

  it('assigns every combination of state flags to exactly one known lane', () => {
    const keys = SESSION_LANES.map(l => l.key)
    // 2^7 flag combinations × sub-agent-approval present/absent.
    for (let mask = 0; mask < 1 << FIELDS.length; mask++) {
      for (const subagentAwaiting of [0, 2]) {
        const slot: LaneSlotFields = {}
        FIELDS.forEach((f, i) => { if (mask & (1 << i)) slot[f] = true })
        const lane = inferLane(slot, { subagentAwaiting })
        expect(keys, `mask ${mask}`).toContain(lane)
        expect(keys.filter(k => k === lane)).toHaveLength(1)
      }
    }
  })

  it('files a session with no state at all under idle, never nowhere', () => {
    expect(inferLane({})).toBe('idle')
  })
})

describe('inferLane precedence', () => {
  it('ranks an owed approval above a running turn', () => {
    // A blocking approval leaves `running` true. Ranking working first would
    // render the row as a spinner and bury the decision the board exists for.
    expect(inferLane({ pending_approval: true, running: true })).toBe('needs_approval')
  })

  it('ranks a sub-agent approval above the parent looking idle', () => {
    // The parent is not running anything, so without the extras it would sort
    // to idle — the least urgent lane — while a click is owed.
    expect(inferLane({}, { subagentAwaiting: 1 })).toBe('needs_approval')
  })

  it('ignores a zero sub-agent approval count', () => {
    expect(inferLane({ running: true }, { subagentAwaiting: 0 })).toBe('working')
  })

  it('ranks an unanswered question above a running turn', () => {
    expect(inferLane({ needs_input: true, running: true })).toBe('waiting')
  })

  it('treats an options card as waiting on the user', () => {
    expect(inferLane({ has_options: true })).toBe('waiting')
  })

  it('treats an interrupted turn as waiting, not idle', () => {
    // The session sits dead until the user resumes it; idle would imply nothing
    // is owed.
    expect(inferLane({ interrupted: true })).toBe('waiting')
  })

  it('ranks live snapshot subagents above stale interruption', () => {
    // Reconnect can hydrate the slot summary before detailed child activity.
    // The board must agree with the sidebar that live work supersedes the old turn.
    expect(inferLane({ interrupted: true, subagents_running: true })).toBe('working')
  })

  it('ranks an approval above a question', () => {
    expect(inferLane({ pending_approval: true, needs_input: true })).toBe('needs_approval')
  })
})

describe('inferLane working signals', () => {
  it('counts a plain running turn', () => {
    expect(inferLane({ running: true })).toBe('working')
  })

  it('counts autopilot stage execution', () => {
    expect(inferLane({ orchestrating: true })).toBe('working')
  })

  it('counts sub-agents running under an idle parent', () => {
    expect(inferLane({ subagents_running: true })).toBe('working')
  })

  it('counts a queued prompt behind a finished turn', () => {
    expect(inferLane({ queue_depth: 1 })).toBe('working')
  })

  it('counts an armed monitor loop between cycles', () => {
    // The loop wakes the session on its own, so it is working rather than idle
    // even in the gap where no turn is in flight.
    expect(inferLane({}, { goalLoopActive: true })).toBe('working')
  })

  it('keeps an interrupted armed loop waiting until live work resumes', () => {
    expect(inferLane({ interrupted: true }, { goalLoopActive: true })).toBe('waiting')
  })

  it('counts a dynamic workflow running against an otherwise-idle slot', () => {
    // A workflow is tracked in the store, not on the slot payload, so `running`
    // is false while it executes. Without it the board files the session as Idle
    // while its own row renders a workflow spinner.
    expect(inferLane({ running: false }, { workflowActive: true })).toBe('working')
  })

  it.each([
    ['orchestration', { orchestrating: true }, {}],
    ['queued work', { queue_depth: 1 }, {}],
    ['dynamic workflow', {}, { workflowActive: true }],
    ['detailed subagents', {}, { detailedSubagentsRunning: true }],
  ])('ranks %s above stale interruption', (_name, slot, extras) => {
    expect(inferLane({ interrupted: true, ...slot }, extras)).toBe('working')
  })

  it('does not treat absent live work as work', () => {
    expect(inferLane({}, { workflowActive: false, goalLoopActive: false })).toBe('idle')
  })

  it('does not treat a zero queue depth as work', () => {
    expect(inferLane({ queue_depth: 0 })).toBe('idle')
  })
})

describe('SESSION_LANES', () => {
  it('orders the lanes most-urgent first', () => {
    expect(SESSION_LANES.map(l => l.key)).toEqual(['needs_approval', 'waiting', 'working', 'idle'])
  })

  it('ends on idle so the fallback lane is the rightmost column', () => {
    expect(SESSION_LANES[SESSION_LANES.length - 1].key).toBe('idle')
  })

  it('gives every lane a distinct key and a label key', () => {
    const keys = SESSION_LANES.map(l => l.key)
    expect(new Set(keys).size).toBe(keys.length)
    for (const lane of SESSION_LANES) {
      expect(lane.labelKey).toMatch(/^pages\.chatSidebar\.lane_/)
      expect(lane.color).toMatch(/^var\(--/)
    }
  })

  it('names only CSS custom properties the theme actually defines', () => {
    // A `var(--x)` that no theme block defines resolves to nothing: the lane's
    // accent dot renders transparent and its label silently inherits. That is
    // invisible to a type-check and to every behavioural test, and it shipped
    // once -- the idle lane used `--text-muted`, which is defined nowhere.
    // Resolved from the vitest root (website/), since import.meta.url is not a
    // file: URL under this config.
    const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')
    const defined = new Set(
      [...css.matchAll(/(--[a-z0-9-]+)\s*:/gi)].map(m => m[1].toLowerCase()),
    )
    expect(defined.size).toBeGreaterThan(20) // guard against a bad read
    for (const lane of SESSION_LANES) {
      const token = /^var\((--[a-z0-9-]+)\)$/i.exec(lane.color)?.[1]?.toLowerCase()
      expect(token, `${lane.key} colour is not a plain var() token: ${lane.color}`).toBeTruthy()
      expect(defined, `${lane.key} uses undefined token ${token}`).toContain(token)
    }
  })
})
