import { describe, it, expect } from 'vitest'
import { unwrapSkills } from '../lib/skillsPayload'

const ROWS = [{ key: 'a', name: 'a' }, { key: 'b', name: 'b' }]

describe('unwrapSkills', () => {
  it('passes a bare array through as unscoped', () => {
    expect(unwrapSkills(ROWS)).toEqual({ items: ROWS, agentScoped: false, scopedAgent: '' })
  })

  it('unwraps a scoped envelope, including an EMPTY one', () => {
    expect(unwrapSkills({ skills: ROWS, agent_scoped: true, agent: 'writer' }))
      .toEqual({ items: ROWS, agentScoped: true, scopedAgent: 'writer' })
    expect(unwrapSkills({ skills: [], agent_scoped: true, agent: 'writer' }))
      .toEqual({ items: [], agentScoped: true, scopedAgent: 'writer' })
  })

  it('treats an envelope without the flag as unscoped data', () => {
    expect(unwrapSkills({ skills: ROWS })).toEqual({ items: ROWS, agentScoped: false, scopedAgent: '' })
  })

  it('refuses the scope when the envelope flags it WITHOUT naming the agent', () => {
    // Scoped copy interpolates the name; "Scoped to agent " is worse than
    // no cue, so a malformed envelope degrades to unscoped.
    expect(unwrapSkills({ skills: ROWS, agent_scoped: true })).toEqual({
      items: ROWS, agentScoped: false, scopedAgent: '',
    })
    expect(unwrapSkills({ skills: ROWS, agent_scoped: true, agent: '' })).toEqual({
      items: ROWS, agentScoped: false, scopedAgent: '',
    })
  })

  it('degrades undefined and malformed payloads to an empty unscoped list', () => {
    expect(unwrapSkills(undefined)).toEqual({ items: [], agentScoped: false, scopedAgent: '' })
    expect(unwrapSkills({ skills: null } as never)).toEqual({ items: [], agentScoped: false, scopedAgent: '' })
    expect(unwrapSkills({} as never)).toEqual({ items: [], agentScoped: false, scopedAgent: '' })
  })
})
