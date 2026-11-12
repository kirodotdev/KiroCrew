import type { ChatMessage } from '../../types'

/**
 * The tags of the KAS project-hooks notice, mirroring `PROJECT_HOOKS_NOTICE_*` in
 * `src/kiro_crew/dashboard/chat_turn/turn_spec_hooks.py`. `off` offers Turn on;
 * `on` (the owner turned them on in Crew) and `kiro_cli` (they run because kiro-cli
 * trusts the folder) offer Turn off. Kept apart from the component so the renderer
 * table can match the row without loading the component (it is lazy-loaded).
 */
const SPEC_HOOKS_KIND = 'spec_hooks'
const STATES = new Set(['off', 'on', 'kiro_cli'])

export type ProjectHooksMeta = {
  kind?: unknown
  project_hooks?: unknown
  project_hooks_folder?: unknown
  project_hooks_commands?: unknown
  /** The gateway's own notice text for the state a successful click leads to. */
  project_hooks_saved_text?: unknown
}

/** The project-hooks notice row, matched on its tags and never on its words. */
export function isProjectHooksNotice(m: ChatMessage): boolean {
  if (m.role !== 'notice') return false
  const meta = m.meta as ProjectHooksMeta | undefined
  return (
    meta?.kind === SPEC_HOOKS_KIND &&
    typeof meta.project_hooks === 'string' &&
    STATES.has(meta.project_hooks) &&
    typeof meta.project_hooks_folder === 'string' &&
    meta.project_hooks_folder.length > 0
  )
}

/** The row's hooks: each event in plain words (`when`, the gateway's) and its
 *  command, keeping only well-formed entries. */
export function projectHookCommands(meta: ProjectHooksMeta): { when: string; command: string }[] {
  const raw = meta.project_hooks_commands
  if (!Array.isArray(raw)) return []
  return raw.flatMap((h) =>
    h && typeof h === 'object' && typeof h.command === 'string'
      ? [{ when: typeof h.when === 'string' ? h.when : String(h.event ?? ''), command: h.command }]
      : [],
  )
}
