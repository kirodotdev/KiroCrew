import { i18nT } from '../../i18n/t'
/** Kanban board TypeScript types — mirrors the backend data model. */

export type TaskStatus = 'backlog' | 'todo' | 'running' | 'done' | 'failed'

export interface ExecutionRecord {
  id: string
  started_at: number
  ended_at?: number | null
  session_key?: string | null
  result?: 'succeeded' | 'failed' | 'cancelled' | null
  error?: string | null
}

export interface TaskRecord {
  id: string
  title: string
  description: string
  prompt: string
  status: TaskStatus
  created_at: number
  updated_at: number
  executions: ExecutionRecord[]
  tags: string[]
  priority: 'low' | 'medium' | 'high'
  /** True while a background job is still generating the title + description. */
  refining?: boolean
}

export interface ColumnDef {
  id: TaskStatus
  color: string       // Tailwind color class prefix
  bgSubtle: string    // Tailwind bg-subtle class
  textColor: string   // Tailwind text color class
}

export const COLUMNS: ColumnDef[] = [
  { id: 'backlog', color: 'muted', bgSubtle: 'bg-bg-hover', textColor: 'text-muted' },
  { id: 'todo', color: 'info', bgSubtle: 'bg-info-subtle', textColor: 'text-info' },
  { id: 'running', color: 'warn', bgSubtle: 'bg-warn-subtle', textColor: 'text-warn' },
  { id: 'done', color: 'ok', bgSubtle: 'bg-ok-subtle', textColor: 'text-ok' },
  { id: 'failed', color: 'danger', bgSubtle: 'bg-danger-subtle', textColor: 'text-danger' },
]

/** Statuses a user can manually drag to */
export const MANUAL_DROP_TARGETS: TaskStatus[] = ['backlog', 'todo', 'done', 'failed']

// Lane labels resolve on every call. A module-level i18nT() would be evaluated
// at import and freeze the label in whichever language happened to be active
// then, leaving the board untranslated after a language switch.
export function laneLabel(id: TaskStatus): string {
  switch (id) {
    case 'backlog': return i18nT('apps.kanban.lanes.backlog')
    case 'todo': return i18nT('apps.kanban.lanes.todo')
    case 'running': return i18nT('apps.kanban.lanes.running')
    case 'done': return i18nT('apps.kanban.lanes.done')
    case 'failed': return i18nT('apps.kanban.lanes.failed')
  }
}
