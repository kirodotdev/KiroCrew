import { describe, it, expect } from 'vitest'
import { memberThreadTitle } from '../lib/memberThreadTitle'

const AGENTS = [
  { name: 'lead', display_name: 'Team Lead', selection_kind: 'member' as const },
  { name: 'plain', selection_kind: 'member' as const },
  { name: 'lead-tpl', display_name: 'Not a mate', selection_kind: 'template' as const },
]

describe('memberThreadTitle', () => {
  it("names a mate's own thread after its display name", () => {
    expect(memberThreadTitle({ mode: 'member', agent: 'lead' }, AGENTS)).toBe('Team Lead')
  })

  it('falls back to the mate name when it has no display name', () => {
    expect(memberThreadTitle({ mode: 'member', agent: 'plain' }, AGENTS)).toBe('plain')
  })

  it('falls back to the mate name while the roster does not list it', () => {
    expect(memberThreadTitle({ mode: 'member', agent: 'unlisted' }, AGENTS)).toBe('unlisted')
    expect(memberThreadTitle({ mode: 'member', agent: 'lead' }, [])).toBe('lead')
  })

  it('ignores a template row that shares the mate name', () => {
    expect(memberThreadTitle({ mode: 'member', agent: 'lead-tpl' }, AGENTS)).toBe('lead-tpl')
  })

  it('leaves every other session to its own title', () => {
    expect(memberThreadTitle({ mode: 'chat', agent: 'lead' }, AGENTS)).toBeNull()
    expect(memberThreadTitle({ agent: 'lead' }, AGENTS)).toBeNull()
    expect(memberThreadTitle({ mode: 'member' }, AGENTS)).toBeNull()
    expect(memberThreadTitle({ mode: 'member', agent: '' }, AGENTS)).toBeNull()
  })
})
