/** The project chip's staged-switch state (#20350).
 *
 *  A project picked while the composer is busy updates `project` at once (the
 *  chip names the next turn's project), but the reply already running keeps
 *  its start-of-turn directory. `useStagedProject` holds that start project
 *  and says whether a pick is still waiting to take effect, so the chip can
 *  show it as visible text instead of only in its tooltip, which touch input
 *  cannot reach.
 *
 *  `isRunning` is the composer's busy flag, which stays true while attached
 *  sub-agents run after the reply itself ends. That is the window in which the
 *  server's deferred session reset stays armed, so the pending state lasts
 *  exactly as long as the old directory can still be in use, and clears only
 *  once the composer is idle and the next turn starts in the picked project. */
import { useState } from 'react'
import { i18nT } from '../../i18n/t'
import { pathBasename, stripTrailingSeparators } from '../../utils/pathBasename'

const folderName = (path: string) => pathBasename(stripTrailingSeparators(path)) || path

export interface StagedProject {
  /** The project the running reply started in, or '' when it had none. */
  turnStartProject: string
  /** True while a pick made during this busy window has not taken effect. */
  pending: boolean
  /** Tooltip / accessible-name lines for a busy composer; '' when idle. */
  midTurnNote: string
  /** The pending line alone (announced and named on the chip); '' when nothing is pending. */
  pendingNote: string
}

export function useStagedProject(projectProp: string | undefined, isRunning: boolean, slotId: string | null | undefined): StagedProject {
  const project = projectProp || ''
  // Captured when the composer turns busy, and adjusted during render rather
  // than in an effect so the first busy frame is right. Re-taken when the
  // composer moves to another chat as well: it is not remounted per chat, so
  // going from one busy chat to another keeps `isRunning` true and would
  // otherwise compare against the previous chat's project.
  const [turnStartProject, setTurnStartProject] = useState(project)
  const [sawRunning, setSawRunning] = useState(isRunning)
  const [sawSlot, setSawSlot] = useState(slotId)
  let start = turnStartProject
  if (slotId !== sawSlot) {
    setSawSlot(slotId)
    setSawRunning(isRunning)
    setTurnStartProject(project)
    start = project
  } else if (isRunning !== sawRunning) {
    setSawRunning(isRunning)
    if (isRunning) {
      setTurnStartProject(project)
      start = project
    }
  }
  const pending = isRunning && !!project && stripTrailingSeparators(project) !== stripTrailingSeparators(start || '')
  if (!isRunning) return { turnStartProject: start, pending: false, midTurnNote: '', pendingNote: '' }
  const startName = start ? folderName(start) : i18nT('components.chatInput.project')
  const pendingNote = pending ? i18nT('components.chatInput.project_switch_pending', { project: folderName(project) }) : ''
  const midTurnNote = [
    pendingNote,
    i18nT('components.chatInput.project_switch_applies_next_turn', { project: startName }),
    i18nT('components.chatInput.project_switch_links_follow_pick'),
  ].filter(Boolean).join('\n')
  return { turnStartProject: start, pending, midTurnNote, pendingNote }
}

/** Visible marker inside the project chip while a staged pick waits. The chip's
 *  accessible name already carries the full pending line, so the marker is
 *  hidden from assistive tech; on a compact shelf it shrinks to a dot. */
export function ProjectPendingBadge({ compact }: { compact: boolean }) {
  if (compact) {
    return <span aria-hidden="true" className="shrink-0 w-1.5 h-1.5 rounded-full bg-accent" data-testid="composer-project-pending" />
  }
  return (
    <span
      aria-hidden="true"
      className="shrink-0 rounded px-1 py-px text-[10px] leading-4 font-medium text-accent bg-[color-mix(in_srgb,var(--accent)_14%,transparent)]"
      data-testid="composer-project-pending"
    >
      {i18nT('components.chatInput.project_pending_badge')}
    </span>
  )
}
