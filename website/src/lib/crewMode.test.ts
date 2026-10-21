import { afterEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_CREW_MODE_AGENT, crewModeAgent, crewModeView, isConductorAgent, isDefaultAgentName,
  memoryModeAllowsCrewMode, runsConductor, setCrewModeCatalog, withoutConductors, agentCycleTarget,
} from './crewMode'

afterEach(() => setCrewModeCatalog(undefined, undefined))

describe('crewMode', () => {
  it('runs on the shipped conductor until the catalog says otherwise', () => {
    expect(DEFAULT_CREW_MODE_AGENT).toBe('kirocrew-conductor')
    expect(crewModeAgent()).toBe('kirocrew-conductor')
  })

  it('swaps the conductor to the one the catalog names', () => {
    setCrewModeCatalog('better-conductor', ['kirocrew-conductor', 'kirocrew-team-lead'])
    expect(crewModeAgent()).toBe('better-conductor')
    // The configured agent is a conductor even when the family list omits it.
    expect(isConductorAgent('better-conductor')).toBe(true)
    expect(isConductorAgent('kirocrew-team-lead')).toBe(true)
    expect(isConductorAgent('kirocrew')).toBe(false)
  })

  it('never treats a crewmate as a conductor, whatever its name', () => {
    expect(runsConductor('kirocrew-conductor', 'template')).toBe(true)
    expect(runsConductor('kirocrew-conductor', undefined)).toBe(true)
    expect(runsConductor('kirocrew-conductor', 'member')).toBe(false)
  })

  it('leaves conductor templates out of a normal chat picker and keeps crewmates', () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor', 'kirocrew-pipeline-conductor'])
    const rows = [
      { name: 'kirocrew', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'template' },
      { name: 'kirocrew-pipeline-conductor', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'member' },
    ]
    expect(withoutConductors(rows)).toEqual([
      { name: 'kirocrew', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'member' },
    ])
  })

  it('keeps workers out of the person list and can show only roots', () => {
    const rows = [
      { key: 'a', crew_mode_root: true },
      { key: 'b', crew_root: 'a' },
      { key: 'c' },
    ]
    expect(crewModeView(rows).map(r => r.key)).toEqual(['a', 'c'])
    expect(crewModeView(rows, true).map(r => r.key)).toEqual(['a'])
  })

  it('treats the empty name and the configured default as the default agent', () => {
    expect(isDefaultAgentName('', 'default')).toBe(true)
    expect(isDefaultAgentName('default', 'default')).toBe(true)
    expect(isDefaultAgentName('custom-x', 'default')).toBe(false)
    // `kirocrew` is the default only while none is configured.
    expect(isDefaultAgentName('kirocrew', 'default')).toBe(false)
    expect(isDefaultAgentName('kirocrew', '')).toBe(true)
  })

  it('needs a Persistent chat, since an Incognito or Temporary one cannot open sessions', () => {
    expect(memoryModeAllowsCrewMode(undefined)).toBe(true)
    expect(memoryModeAllowsCrewMode('persistent')).toBe(true)
    expect(memoryModeAllowsCrewMode('incognito')).toBe(false)
    expect(memoryModeAllowsCrewMode('temporary')).toBe(false)
  })

  it('cycles a normal chat over the conductor-free list only', () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor', 'kirocrew-team-lead'])
    const agents = [
      { name: 'kirocrew', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'template' },
      { name: 'research', selection_kind: 'template' },
      { name: 'kirocrew-team-lead', selection_kind: 'template' },
    ]
    expect(agentCycleTarget(agents, 'kirocrew', 'template', 1)).toBe('research')
    // Wraps past the conductors in both directions.
    expect(agentCycleTarget(agents, 'research', 'template', 1)).toBe('kirocrew')
    expect(agentCycleTarget(agents, 'kirocrew', 'template', -1)).toBe('research')
    // A conductor chat has no picker, so the chord does nothing there.
    expect(agentCycleTarget(agents, 'kirocrew-conductor', 'template', 1)).toBeNull()
    expect(agentCycleTarget([], 'kirocrew', 'template', 1)).toBeNull()
  })
})
