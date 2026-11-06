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
 *  exactly as long as the old directory can still be in use. It clears as
 *  soon as the composer goes idle: from then on the chip names the project the
 *  next message will use, so nothing is pending any more. */
import { useState } from 'react'
import { Clock } from 'lucide-react'
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

export function useStagedProject(
  projectProp: string | undefined,
  isRunning: boolean,
  slotId: string | null | undefined,
  turnProject?: string | null,
): StagedProject {
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
  // The server's own reading of the running turn's directory wins once the
  // turn has claimed its session (#20352): a composer mounted after a reload
  // would otherwise capture the already-picked project as the start.
  if (typeof turnProject === 'string') start = turnProject
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
 *  hidden from assistive tech. Its text says the switch is still to come
 *  ("Starts next message"), so it reads without the tooltip. On a compact
 *  shelf the chip shows no folder name, and a clock glyph alone did not say
 *  what is waiting, so there the marker is the clock followed by the picked
 *  folder's name, truncated: still visible text, never a tooltip only. */
export function ProjectPendingBadge({ compact, project }: { compact: boolean; project: string }) {
  if (compact) {
    return (
      <span
        aria-hidden="true"
        className="inline-flex min-w-0 items-center gap-0.5 rounded px-1 py-px text-[10px] leading-4 font-medium text-accent bg-[color-mix(in_srgb,var(--accent)_14%,transparent)]"
        data-testid="composer-project-pending"
      >
        <Clock size={11} strokeWidth={2.25} className="shrink-0" data-testid="composer-project-pending-clock" />
        <span className="truncate max-w-[88px]">{folderName(project)}</span>
      </span>
    )
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
