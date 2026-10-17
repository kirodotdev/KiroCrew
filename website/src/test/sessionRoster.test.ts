import { describe, it, expect } from 'vitest'
import { sessionTitleRoster } from '../utils/sessionRoster'

/* The roster a session chip may be offered against.
 *
 * Two pages build it now -- ChatPage for its own transcript, the Members page
 * for a crewmate DM whose chips navigate to ChatPage -- so the rule lives in one
 * place. What it has to get right is the narrowing: the map is a claim that
 * clicking lands somewhere, and the destination is the unified chat view, so a
 * slot that view cannot render must not be in it. */

const slot = (key: string, extra: Record<string, string> = {}) => ({ key, ...extra })

describe('sessionTitleRoster', () => {
  it('keeps the surfaces the chat page renders: the default one and orchestrator', () => {
    const roster = sessionTitleRoster([
      slot('chat-1-1700000000', { title: 'Fix the pagination bug' }),
      slot('chat-2-1700000001', { title: 'Orchestrate the release', surface: 'orchestrator' }),
    ])
    expect([...roster.keys()]).toEqual(['chat-1-1700000000', 'chat-2-1700000001'])
    expect(roster.get('chat-2-1700000001')).toBe('Orchestrate the release')
  })

  it('drops a slot the destination cannot render, so no chip points at a switch that clears itself', () => {
    const roster = sessionTitleRoster([
      slot('chat-1-1700000000', { title: 'Keep me' }),
      slot('member-radar', { title: 'Radar', mode: 'member' }),
      slot('app-spec-builder', { title: 'Spec builder', surface: 'app' }),
      slot('dash-1', { title: 'Dashboard', surface: 'dashboard' }),
    ])
    expect([...roster.keys()]).toEqual(['chat-1-1700000000'])
  })

  it('reads `surface` first and falls back to `mode`, the way the slot rows are shaped', () => {
    // A row carrying BOTH: `surface` decides. An empty surface beside a
    // member mode is the shape a migrated row has, and it stays admitted --
    // the fallback only applies when `surface` is absent entirely.
    expect([...sessionTitleRoster([slot('a', { surface: '', mode: 'member' })]).keys()]).toEqual(['a'])
    expect([...sessionTitleRoster([slot('b', { mode: 'member' })]).keys()]).toEqual([])
    expect([...sessionTitleRoster([slot('c', { surface: 'orchestrator', mode: 'member' })]).keys()]).toEqual(['c'])
  })

  it('titles an untitled slot with its own key, so the tooltip is never blank', () => {
    const roster = sessionTitleRoster([slot('chat-9-1700000009'), slot('chat-8-1700000008', { title: '' })])
    expect(roster.get('chat-9-1700000009')).toBe('chat-9-1700000009')
    expect(roster.get('chat-8-1700000008')).toBe('chat-8-1700000008')
  })

  it('answers an empty roster for no slots -- a caller that KNOWS there is nothing open', () => {
    // Distinct from withholding the map, which is how a caller says it does not
    // know yet (see markdown/contexts.ts). This builder only ever makes the
    // first claim; withholding is the host's decision, not this function's.
    expect(sessionTitleRoster([]).size).toBe(0)
  })
})
