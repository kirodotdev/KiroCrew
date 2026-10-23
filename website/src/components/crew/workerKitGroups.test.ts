/**
 * workerKitGroups — the preview's rows in a fixed kind order, each kind's rows
 * added, then changed, then removed, each by the name the row shows.
 */
import { describe, it, expect } from 'vitest'
import type { WorkerKitChange } from '../../api/crewCapabilities'
import { groupWorkerKitChanges } from './workerKitGroups'

const row = (section: WorkerKitChange['section'], id: string, change: WorkerKitChange['change']): WorkerKitChange =>
  ({ section, id, change })

describe('groupWorkerKitChanges', () => {
  it('puts every section in its group, in the fixed order, and leaves out empty groups', () => {
    const groups = groupWorkerKitChanges([
      row('disabledServers', 'kirocrew-core', 'added'),
      row('excludedTools', 'execute_bash', 'added'),
      row('disabledTools', '@kirocrew-core/cron_add', 'removed'),
      row('allowedTools', '@jira/search', 'added'),
      row('skills', 'notes', 'added'),
      row('resources', 'skill:///x/release-notes/SKILL.md', 'removed'),
      row('tools', '@jira', 'added'),
      row('mcpServers', 'jira', 'added'),
      row('prompt', 'prompt', 'changed'),
    ])
    expect(groups.map(g => [g.key, g.changes.length])).toEqual([
      ['prompt', 1], ['connections', 1], ['tools', 1], ['skills', 2], ['approvals', 1], ['off', 3],
    ])
    expect(groups.map(g => g.heading)).toEqual([
      'Instructions', 'Connections', 'Tool access', 'Skills', 'Auto-approvals', 'Turned off',
    ])
  })

  it('orders a group added, changed, removed, then by the shown name', () => {
    const [skills] = groupWorkerKitChanges([
      row('resources', 'skill:///x/zeta/SKILL.md', 'removed'),
      row('skills', 'beta', 'added'),
      row('resources', 'skill:///x/alpha/SKILL.md', 'removed'),
      row('skills', 'gamma', 'changed'),
      row('resources', 'skill:///x/alpha-two/SKILL.md', 'added'),
    ])
    expect(skills.changes.map(c => [c.change, c.id])).toEqual([
      ['added', 'skill:///x/alpha-two/SKILL.md'],
      ['added', 'beta'],
      ['changed', 'gamma'],
      ['removed', 'skill:///x/alpha/SKILL.md'],
      ['removed', 'skill:///x/zeta/SKILL.md'],
    ])
  })

  it('has no group for no changes', () => {
    expect(groupWorkerKitChanges([])).toEqual([])
  })
})
