import type { CrewTeam } from './client'
import { withSavedTeam } from './teamsQuery'

/* The team dialog writes the route's answer into the cached team list before
 * the refetch it starts. The store keeps a crewmate on one team, so a save
 * that adds a crewmate to a team also took it OFF its old team in the same
 * write; the cached list must say the same, or a failed refetch leaves the
 * crewmate on both teams and the roster keeps showing the old one. */
describe('withSavedTeam', () => {
  const triage: CrewTeam = { id: 'aaaaaaaaaaaa', name: 'Triage', members: ['oncall', 'scribe'] }
  const release: CrewTeam = { id: 'bbbbbbbbbbbb', name: 'Release', members: ['fixer'] }

  it('takes a moved crewmate off every other cached team, in place', () => {
    const saved: CrewTeam = { ...release, members: ['fixer', 'scribe'] }
    expect(withSavedTeam([triage, release], saved)).toEqual([
      { ...triage, members: ['oncall'] },
      saved,
    ])
  })

  it('a brand-new team detaches its members before it is appended', () => {
    const saved: CrewTeam = { id: 'cccccccccccc', name: 'Docs', members: ['oncall'] }
    expect(withSavedTeam([triage, release], saved)).toEqual([
      { ...triage, members: ['scribe'] },
      release,
      saved,
    ])
  })

  it('leaves an untouched team as the same object and an empty cache empty', () => {
    const saved: CrewTeam = { ...triage, name: 'Intake' }
    const out = withSavedTeam([triage, release], saved)
    expect(out?.[0]).toEqual(saved)
    expect(out?.[1]).toBe(release)
    expect(withSavedTeam(undefined, saved)).toBeUndefined()
  })
})
