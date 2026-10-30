/**
 * The name a worker-kit preview row shows for the item it changes. A row's `id`
 * is the value the crewmate's file stores: a server's key, a skill's resource
 * URI (`skill:///…/<name>/SKILL.md`) or an approval (`@server/tool`). The row
 * shows the skill's name and the tool and server names; the stored value stays
 * in the row's title.
 */
import { i18nT } from '../../i18n/t'
import type { WorkerKitChange } from '../../api/crewCapabilities'

const SCHEME_RE = /^[a-z][a-z0-9+.-]*:\/\//i
const SKILL_FILE_RE = /\/SKILL\.md$/i
const GLOB_RE = /[*?[\]{}]/

/** A skill's name from its resource URI or path: the folder holding SKILL.md. */
export function skillLabel(id: string): string {
  const path = id.replace(SCHEME_RE, '').replace(SKILL_FILE_RE, '').replace(/\/+$/, '')
  const parts = path.split('/').filter(Boolean)
  const globAt = parts.findIndex(p => GLOB_RE.test(p))
  if (globAt >= 0) {
    // A pattern names every skill under a folder, not one skill.
    const folder = parts[globAt - 1]
    return folder ? i18nT('crewWorkerKit.skills_in_folder', { folder }) : id
  }
  const last = parts[parts.length - 1]
  return last ? last.replace(/\.md$/i, '') : id
}

/** An approval's tool and server: `@server/tool`, `@server` for all its tools. */
export function approvalLabel(id: string): string {
  const m = /^@([^/]+)(?:\/(.+))?$/.exec(id)
  if (!m) return id
  const [, server, tool] = m
  if (!tool || tool === '*') return i18nT('crewWorkerKit.approval_every_tool', { server })
  return i18nT('crewWorkerKit.approval_tool_on_server', { tool, server })
}

/** A tool selector's reach: `@server/tool`, `@server` for all its tools. Worded
 *  as what the worker can use, so it never reads like an auto-approval row. */
export function toolAccessLabel(id: string): string {
  const m = /^@([^/]+)(?:\/(.+))?$/.exec(id)
  if (!m) return id
  const [, server, tool] = m
  if (!tool || tool === '*') return i18nT('crewWorkerKit.tool_access_every_tool', { server })
  return i18nT('crewWorkerKit.tool_access_tool_on_server', { tool, server })
}

/** What a preview row names, or `null` for the instructions row, which names nothing. */
export function changeLabel(change: Pick<WorkerKitChange, 'section' | 'id'>): string | null {
  switch (change.section) {
    case 'prompt':
      return null
    case 'resources':
    case 'skills':
      return skillLabel(change.id)
    case 'tools':
      return toolAccessLabel(change.id)
    case 'allowedTools':
    case 'excludedTools':
    case 'disabledTools':
      return approvalLabel(change.id)
    default:
      return change.id
  }
}
