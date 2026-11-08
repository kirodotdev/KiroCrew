import { useState } from 'react'
import type { EditDrafts } from './CrewDashboardEditLayer'

/** Dashboard edit mode's session: whether Edit is on, and the layer's unsent drafts. */
export interface EditSession {
  editing: boolean
  setEditing: (on: boolean) => void
  drafts: EditDrafts
  /** Done: Edit off and the next session starts with no drafts. */
  finish: () => void
}

/** One Edit session per *forKey* (a Bot's page). It is held by whoever outlives the
 *  dashboard document: the Dashboard tab remounts that document when it swaps the live
 *  page for a staged one (and back), and a session held inside it would start empty
 *  each time, losing the unsent request, correction value and reason. */
export function useEditSession(forKey: string): EditSession {
  const fresh = () => ({ for: forKey, editing: false, drafts: new Map<string, unknown>() })
  const [state, setState] = useState(fresh)
  let current = state
  if (state.for !== forKey) { current = fresh(); setState(current) }
  return {
    editing: current.editing,
    setEditing: on => setState(prev => ({ ...prev, editing: on })),
    drafts: current.drafts,
    finish: () => setState(prev => ({ for: prev.for, editing: false, drafts: new Map() })),
  }
}
