import { describe, it, expect } from 'vitest'
import {
  PRESET_STATES, broadcastTargets, deliveryFor, matchesBroadcast, sendSteers, slotBroadcastStates,
  type BroadcastCriteria, type BroadcastSlot,
} from './broadcastTargets'
import { inferLane } from '../pages/chat/sessionLane'

const NOW = Date.parse('2026-10-15T06:00:00Z')
const fold = (id: string, parent_id?: string) => ({ id, name: id, order: 0, ...(parent_id ? { parent_id } : {}) })
const ctx = (loop: string[] = [], folders: ReturnType<typeof fold>[] = []) => ({
  folders, now: NOW,
  isCrewmate: (s: BroadcastSlot) => s.agent_kind === 'member',
  extrasFor: (s: BroadcastSlot) => ({ goalLoopActive: loop.includes(s.key) }),
})
const base = (over: Partial<BroadcastCriteria> = {}): BroadcastCriteria =>
  ({ preset: 'all', crewmates: 'include', states: [], ...over })

const idle: BroadcastSlot = { key: 'idle', running: false }
const busy: BroadcastSlot = { key: 'busy', running: true }
const crew: BroadcastSlot = { key: 'crew', running: true, agent_kind: 'member' }
const ask: BroadcastSlot = { key: 'ask', running: false, has_options: true }
const gated: BroadcastSlot = { key: 'gated', running: true, pending_approval: true }
const stalled: BroadcastSlot = { key: 'stalled', running: false, interrupted: true }
const peer: BroadcastSlot = { key: 'peer', running: true, peer_id: 'p1' }

describe('slotBroadcastStates', () => {
  it('uses the board lane for every row, so the two can never disagree', () => {
    for (const s of [idle, busy, ask, gated, stalled]) {
      expect(slotBroadcastStates(s).has(inferLane(s))).toBe(true)
    }
  })
  it('files an approval-blocked turn as needs_approval, not working', () => {
    const st = slotBroadcastStates(gated)
    expect(st.has('needs_approval')).toBe(true)
    expect(st.has('working')).toBe(false)
  })
  it('adds loop as a mode on top of the lane', () => {
    expect(slotBroadcastStates(busy, { goalLoopActive: true }).has('loop')).toBe(true)
    expect(slotBroadcastStates(busy).has('loop')).toBe(false)
  })
  it('reads the live signals outside the slot payload, as the board does', () => {
    // A parent whose sub-agent waits on an approval, and a running workflow.
    expect(slotBroadcastStates(idle, { subagentAwaiting: 1 }).has('needs_approval')).toBe(true)
    expect(slotBroadcastStates(idle, { workflowActive: true }).has('working')).toBe(true)
    expect(deliveryFor(idle, { workflowActive: true })).toBe('after-work')
  })
})

describe('matchesBroadcast presets', () => {
  const rows = [idle, busy, crew, ask, gated, stalled]
  it('loop reaches only looping rows', () => {
    expect(broadcastTargets(rows, base({ preset: 'loop' }), ctx(['idle', 'crew'])).map(s => s.key)).toEqual(['idle', 'crew'])
  })
  it('running reaches the working lane', () => {
    expect(broadcastTargets(rows, base({ preset: 'running' }), ctx()).map(s => s.key)).toEqual(['busy', 'crew'])
  })
  it('waiting reaches both lanes that owe the user something', () => {
    expect(broadcastTargets(rows, base({ preset: 'waiting' }), ctx()).map(s => s.key)).toEqual(['ask', 'gated', 'stalled'])
  })
  it('all reaches every local row', () => {
    expect(broadcastTargets(rows, base({ preset: 'all' }), ctx())).toHaveLength(rows.length)
    expect(PRESET_STATES.all).toHaveLength(5)
  })
  it('never reaches a peer-owned row', () => {
    expect(matchesBroadcast(peer, base(), ctx())).toBe(false)
  })
  it('never matches a relay archive, which refuses every send', () => {
    const archive: BroadcastSlot = { key: 'arch', running: false, executor: 'remote' }
    expect(matchesBroadcast(archive, base(), ctx())).toBe(false)
    expect(matchesBroadcast({ ...archive, executor: 'local' }, base(), ctx())).toBe(true)
  })
})

describe('mate mode', () => {
  const rows = [busy, crew]
  it('exclude drops mate chats', () => {
    expect(broadcastTargets(rows, base({ crewmates: 'exclude' }), ctx()).map(s => s.key)).toEqual(['busy'])
  })
  it('only keeps mate chats', () => {
    expect(broadcastTargets(rows, base({ crewmates: 'only' }), ctx()).map(s => s.key)).toEqual(['crew'])
  })
  it('include keeps both', () => {
    expect(broadcastTargets(rows, base({ crewmates: 'include' }), ctx())).toHaveLength(2)
  })
  it('lets the caller recognise a kind-less mate chat', () => {
    const kindless: BroadcastSlot = { key: 'k', running: true, agent: 'astra' }
    const c = { ...ctx(), isCrewmate: (s: BroadcastSlot) => s.agent === 'astra' }
    expect(matchesBroadcast(kindless, base({ crewmates: 'exclude' }), c)).toBe(false)
    expect(matchesBroadcast(kindless, base({ crewmates: 'only' }), c)).toBe(true)
  })
})

describe('custom criteria', () => {
  const custom = (over: Partial<BroadcastCriteria>) => base({ preset: 'custom', states: ['working', 'idle'], ...over })
  it('matches no row when no state is picked', () => {
    expect(matchesBroadcast(busy, custom({ states: [] }), ctx())).toBe(false)
  })
  it('matches any picked state', () => {
    expect(broadcastTargets([idle, busy, ask], custom({}), ctx()).map(s => s.key)).toEqual(['idle', 'busy'])
  })
  it('includes sub-folders of the picked folder', () => {
    const folders = [fold('f'), fold('g', 'f'), fold('h', 'g'), fold('x')]
    const rows: BroadcastSlot[] = [{ ...busy, key: 'in', folder_id: 'h' }, { ...busy, key: 'out', folder_id: 'x' }, { ...busy, key: 'none' }]
    expect(broadcastTargets(rows, custom({ folderId: 'f' }), ctx([], folders)).map(s => s.key)).toEqual(['in'])
  })
  it('requires the tag, the pin and recent activity when asked', () => {
    const rows: BroadcastSlot[] = [
      { ...busy, key: 'ok', tags: ['t'], pinned: true, last_ts: '2026-10-15T05:00:00Z' },
      { ...busy, key: 'old', tags: ['t'], pinned: true, last_ts: '2026-10-13T05:00:00Z' },
      { ...busy, key: 'unpinned', tags: ['t'], last_ts: '2026-10-15T05:00:00Z' },
      { ...busy, key: 'untagged', pinned: true, last_ts: '2026-10-15T05:00:00Z' },
      { ...busy, key: 'nots', tags: ['t'], pinned: true },
    ]
    const c = custom({ tagId: 't', pinnedOnly: true, activeWithinMs: 24 * 3600_000 })
    expect(broadcastTargets(rows, c, ctx()).map(s => s.key)).toEqual(['ok'])
  })
  it('still applies the mate mode', () => {
    expect(matchesBroadcast(crew, custom({ crewmates: 'exclude' }), ctx())).toBe(false)
  })
})

describe('deliveryFor', () => {
  it('steers a live row and starts a new turn on an idle one', () => {
    expect(deliveryFor(busy)).toBe('steer')
    expect(deliveryFor(idle)).toBe('new')
    expect(deliveryFor(ask)).toBe('new')
  })
  it('says a session whose turn ended but whose sub-agents or queue are live holds the message', () => {
    expect(deliveryFor({ key: 's', running: false, subagents_running: true })).toBe('after-work')
    expect(deliveryFor({ key: 'q', running: false, queue_depth: 2 })).toBe('after-work')
  })
  it('steers only into a session\'s own running turn', () => {
    expect(sendSteers(busy)).toBe(true)
    expect(sendSteers({ key: 's', running: false, subagents_running: true })).toBe(false)
    expect(sendSteers(idle)).toBe(false)
  })
  it('says a turn parked on the user takes the message only after the answer', () => {
    expect(deliveryFor(gated)).toBe('after-answer')
    expect(deliveryFor({ key: 'q', running: true, needs_input: true })).toBe('after-answer')
  })
})
