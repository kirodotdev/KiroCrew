/**
 * A Crew run's own harness-native children (`native:<run id>:<child>`) draw one level
 * under that run's card; every other card keeps its rank.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage, sseSubagentSpawn } from '../store/chatSlice'
import { nestUnderRuns, runParentOf } from '../utils/subagentNesting'

const RUN = '0123456789abcdef'
const OTHER = 'fedcba9876543210'
const card = {}

describe('runParentOf', () => {
  it('names the run a run child card draws under', () => {
    const subs = { [RUN]: card }
    expect(runParentOf(`native:${RUN}:child-1`, subs)).toBe(RUN)
    expect(runParentOf(`native:${RUN}:wy57uompy:run-1:a`, subs)).toBe(RUN)
  })

  it('leaves a card whose run is not in the map at the top', () => {
    expect(runParentOf(`native:${RUN}:child-1`, {})).toBeUndefined()
  })

  it("keeps a chat's own native cards flat", () => {
    const subs = { wy57uompy: card, [RUN]: card }
    // A chat's Workflow agent names its task, which is not a run id.
    expect(runParentOf('native:wy57uompy:a03ccb341194641db', subs)).toBeUndefined()
    expect(runParentOf('native:child-0001', subs)).toBeUndefined()
    expect(runParentOf(RUN, subs)).toBeUndefined()
    expect(runParentOf(`native:${RUN}:`, subs)).toBeUndefined()
    expect(runParentOf('native:toString:x', {})).toBeUndefined()
  })
})

describe('nestUnderRuns', () => {
  it('puts each run child right after its run, in rank order, one level deep', () => {
    const ranked = [`native:${RUN}:b`, OTHER, 'native:child-0001', RUN, `native:${RUN}:a`]
    const subs = Object.fromEntries(ranked.map(id => [id, card]))
    const { ids, parentOf } = nestUnderRuns(ranked, subs)
    expect(ids).toEqual([OTHER, 'native:child-0001', RUN, `native:${RUN}:b`, `native:${RUN}:a`])
    expect([...parentOf]).toEqual([[`native:${RUN}:b`, RUN], [`native:${RUN}:a`, RUN]])
  })

  it('keeps an orphan at its own rank', () => {
    const ranked = [`native:${RUN}:a`, OTHER]
    const { ids, parentOf } = nestUnderRuns(ranked, { [ranked[0]]: card, [OTHER]: card })
    expect(ids).toEqual(ranked)
    expect(parentOf.size).toBe(0)
  })

  it('applies before the render cap, so a run keeps its children in the first 30', () => {
    const runs = Array.from({ length: 40 }, (_, i) => i.toString(16).padStart(16, '0'))
    const ranked = [...runs, `native:${runs[0]}:a`]
    const subs = Object.fromEntries(ranked.map(id => [id, card]))
    expect(nestUnderRuns(ranked, subs).ids.slice(0, 30)).toContain(`native:${runs[0]}:a`)
  })
})

describe('a stopped chat dims its own agents, not a run child', () => {
  it('leaves a run child undimmed when the Stop card settles', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, sseSubagentSpawn({ slot: 'A', id: RUN, agent: 'run' }))
    state = reducer(state, sseSubagentSpawn({ slot: 'A', id: `native:${RUN}:c1`, agent: 'child' }))
    state = reducer(state, sseChatMessage({
      slot: 'A', role: 'system', content: '', ts: '2026-11-07T00:00:00Z', kind: 'stop_event',
      meta: { kind: 'stop_event', id: 'stop-1', state: 'stopped' },
    } as Parameters<typeof sseChatMessage>[0]))
    expect(state.slotActivity.A.subagents[RUN].parentStopped).toBe(true)
    expect(state.slotActivity.A.subagents[`native:${RUN}:c1`].parentStopped).toBeUndefined()
  })
})
