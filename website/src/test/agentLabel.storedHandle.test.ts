/**
 * A picker stores the crew's IDENTITY (`member_id`), so every surface that
 * renders a STORED agent value (Schedule's Agent column, a webhook token's
 * detail, a channel role) must resolve it back to the display name through
 * the roster it holds. Before this, a crew picked as "Crew Program Manager"
 * reappeared as `crew-program-manager` on each of those surfaces.
 */
import { describe, expect, it } from 'vitest'
import { agentDisplayLabel, agentOrDefaultLabel, crewHandle, isHandleOf } from '../utils/agentLabel'

const roster = [
  { name: 'Crew Program Manager', member_id: 'crew-program-manager', display_name: 'Crew Program Manager' },
  // A project-scope row: no id, the name is the handle it stores.
  { name: 'linter' },
]

describe('stored agent handles render as display names', () => {
  it('a stored member_id resolves to the crew display name', () => {
    expect(agentDisplayLabel('crew-program-manager', roster)).toBe('Crew Program Manager')
    expect(agentOrDefaultLabel('crew-program-manager', 'kirocrew', roster)).toBe('Crew Program Manager')
  })

  it('a stored name (older record, project row) still resolves', () => {
    expect(agentDisplayLabel('Crew Program Manager', roster)).toBe('Crew Program Manager')
    expect(agentDisplayLabel('linter', roster)).toBe('linter')
  })

  it('a value the roster does not list renders verbatim, never blank', () => {
    expect(agentDisplayLabel('kirocrew', roster)).toBe('kirocrew')
    expect(agentDisplayLabel('kirocrew')).toBe('kirocrew')
    expect(agentOrDefaultLabel('kirocrew', 'x')).toBe('kirocrew')
  })

  it('the picker stores the id and matches either handle', () => {
    expect(crewHandle(roster[0])).toBe('crew-program-manager')
    expect(crewHandle(roster[1])).toBe('linter')
    expect(isHandleOf(roster[0], 'crew-program-manager')).toBe(true)
    expect(isHandleOf(roster[0], 'Crew Program Manager')).toBe(true)
    expect(isHandleOf(roster[1], 'crew-program-manager')).toBe(false)
  })
})
