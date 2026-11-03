import { useEffect, useLayoutEffect, useState } from 'react'
import { safeGetSessionItem, safeSetSessionItem } from '../../utils/safeStorage'

/**
 * The MCP Servers changes staged for the next Apply, kept for the browser tab
 * (#25962). Opening a chat unmounts the Customize page, so without this a trip
 * to a chat and back dropped every scope toggle, tool toggle and staged
 * uninstall that had not been applied yet.
 *
 * `sessionStorage`, not `localStorage`: a staged change belongs to the tab that
 * made it, and a second tab must not inherit an uninstall it never chose.
 */
export const MCP_STAGED_CHANGES_KEY = 'kirocrew:mcp-servers-staged-changes'

export type StagedScopeChange = { scopes?: Partial<Record<string, boolean>>; uninstall?: boolean }
export type StagedChanges = {
  pending: Record<string, StagedScopeChange>
  pendingTools: Record<string, Record<string, boolean>>
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

function boolMap(v: unknown): Record<string, boolean> | null {
  if (!isRecord(v)) return null
  const out: Record<string, boolean> = {}
  for (const [k, b] of Object.entries(v)) if (typeof b === 'boolean') out[k] = b
  return Object.keys(out).length > 0 ? out : null
}

/** The saved changes, or null when nothing usable is stored. Malformed entries are dropped. */
export function loadStagedChanges(): StagedChanges | null {
  let saved: unknown
  try {
    saved = JSON.parse(safeGetSessionItem(MCP_STAGED_CHANGES_KEY) || 'null')
  } catch {
    return null
  }
  if (!isRecord(saved)) return null
  const pending: StagedChanges['pending'] = {}
  if (isRecord(saved.pending)) {
    for (const [name, raw] of Object.entries(saved.pending)) {
      if (!isRecord(raw)) continue
      if (raw.uninstall === true) { pending[name] = { uninstall: true }; continue }
      const scopes = boolMap(raw.scopes)
      if (scopes) pending[name] = { scopes }
    }
  }
  const pendingTools: StagedChanges['pendingTools'] = {}
  if (isRecord(saved.pendingTools)) {
    for (const [name, raw] of Object.entries(saved.pendingTools)) {
      const tools = boolMap(raw)
      if (tools) pendingTools[name] = tools
    }
  }
  if (Object.keys(pending).length === 0 && Object.keys(pendingTools).length === 0) return null
  return { pending, pendingTools }
}

/**
 * Restore the staged changes once on mount, then keep the stored copy in step.
 *
 * The restore runs in a layout effect so the restored rows are what first
 * paints. Saving waits for that restore to commit: the first render still holds
 * the empty initial state, and writing it would erase what is being restored.
 */
export function useStagedChangesMemory(current: StagedChanges, restore: (saved: StagedChanges) => void) {
  const [restored, setRestored] = useState(false)
  useLayoutEffect(() => {
    const saved = loadStagedChanges()
    if (saved) restore(saved)
    setRestored(true)
    // Mount only: `restore` is a fresh closure every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const { pending, pendingTools } = current
  useEffect(() => {
    if (!restored) return
    safeSetSessionItem(MCP_STAGED_CHANGES_KEY, JSON.stringify({ pending, pendingTools }))
  }, [restored, pending, pendingTools])
}
