import { describe, expect, it } from 'vitest'
import { harnessManagedNoteBackend } from './autoCompact'

describe('harnessManagedNoteBackend', () => {
  it('names the harness while the session follows the global', () => {
    expect(harnessManagedNoteBackend({ pct: null, agent_pct: null, harness_managed: 'kas' })).toBe('kas')
  })

  it('hides the note once the slider sets an override', () => {
    expect(harnessManagedNoteBackend({ pct: 20, agent_pct: null, harness_managed: 'kas' })).toBeNull()
  })

  it('hides the note when the agent declares a default', () => {
    expect(harnessManagedNoteBackend({ pct: null, agent_pct: 30, harness_managed: 'kas' })).toBeNull()
  })

  it('shows nothing for a backend Crew compacts', () => {
    expect(harnessManagedNoteBackend({ pct: null, agent_pct: null, harness_managed: null })).toBeNull()
    expect(harnessManagedNoteBackend(null)).toBeNull()
  })
})
