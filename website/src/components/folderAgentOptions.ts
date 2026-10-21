import { crewDisplayName } from './AgentSelector'
import type { SearchableSelectOption } from './SearchableSelect'
import { i18nT } from '../i18n/t'

/** One row of the roster the Default-agent picker lists. */
export interface FolderAgentRow {
  name: string
  display_name?: string
  selection_kind?: 'member' | 'template'
  kiro_agent?: unknown
}

/**
 * The picker's agent rows. Every row stores `name`: a member's config key or a
 * template's name, the two values the create route binds without guessing. A
 * member reads under its label, as it does in the chat agent picker, and still
 * matches its key in the filter. When two rows read the same, each label says
 * which kind it is, in the list and in the closed picker, so a member labelled
 * like a template is never picked as that template, or the other way round, by
 * mistake.
 */
export function folderAgentOptions(agents: readonly FolderAgentRow[]): SearchableSelectOption[] {
  const labelOf = (a: FolderAgentRow) => (a.selection_kind === 'member' ? crewDisplayName(a) : a.name)
  const counts = new Map<string, number>()
  for (const a of agents) counts.set(labelOf(a), (counts.get(labelOf(a)) ?? 0) + 1)
  return agents.map(a => {
    const label = labelOf(a)
    const kindLabel = a.selection_kind === 'member'
      ? i18nT('components.agentDropdownList.group_members')
      : i18nT('components.agentDropdownList.group_templates')
    // In the label itself, not a sublabel: the closed picker shows only the
    // label, and an exact typed name then matches the row's value alone.
    const shown = (counts.get(label) ?? 0) > 1 && a.selection_kind ? `${label} · ${kindLabel}` : label
    return {
      value: a.name,
      label: shown,
      ...(shown !== a.name ? { keywords: a.name } : {}),
    }
  })
}
