/**
 * How a worker-kit preview groups its rows: one group per kind of item, in a
 * fixed order, and within a group what is added, then changed, then removed,
 * each by name. The preview reads one group at a time instead of a mixed list.
 */
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import type { WorkerKitChange } from '../../api/crewCapabilities'
import { changeLabel } from './workerKitLabels'

export type WorkerKitGroupKey = 'prompt' | 'connections' | 'tools' | 'skills' | 'approvals' | 'off'

/** The groups in display order, each with the sections it holds. */
const GROUPS: { key: WorkerKitGroupKey; sections: WorkerKitChange['section'][]; heading: () => string }[] = [
  { key: 'prompt', sections: ['prompt'], heading: () => i18nT('crewWorkerKit.section_prompt') },
  { key: 'connections', sections: ['mcpServers'], heading: () => i18nT('crewWorkerKit.group_connections') },
  { key: 'tools', sections: ['tools'], heading: () => i18nT('crewWorkerKit.group_tool_access') },
  { key: 'skills', sections: ['resources', 'skills'], heading: () => i18nT('crewWorkerKit.group_skills') },
  { key: 'approvals', sections: ['allowedTools'], heading: () => i18nT('crewWorkerKit.group_approvals') },
  { key: 'off', sections: ['excludedTools', 'disabledTools', 'disabledServers'], heading: () => i18nT('crewWorkerKit.section_excluded') },
]

const CHANGE_ORDER: Record<WorkerKitChange['change'], number> = { added: 0, changed: 1, removed: 2 }

export interface WorkerKitGroup {
  key: WorkerKitGroupKey
  heading: string
  changes: WorkerKitChange[]
}

/** *changes* grouped for display; a group with no rows is left out. */
export function groupWorkerKitChanges(changes: WorkerKitChange[]): WorkerKitGroup[] {
  return GROUPS.map(group => ({
    key: group.key,
    heading: group.heading(),
    changes: changes
      .filter(c => group.sections.includes(c.section))
      .sort((a, b) =>
        CHANGE_ORDER[a.change] - CHANGE_ORDER[b.change]
        || compareText(changeLabel(a) ?? a.id, changeLabel(b) ?? b.id)),
  })).filter(group => group.changes.length > 0)
}
