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
 *  next message will use, so nothing is pending any more. A queued message
 *  that starts the next turn straight away also takes the pick, so a new
 *  turn opening (`turnKey`) clears it the same way. */
import { useRef } from 'react'
import { Clock } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { pathBasename, stripTrailingSeparators } from '../../utils/pathBasename'
import { opensRunTurn } from '../../pages/chat/groupDisplayItems'
import type { ChatMessage } from '../../types'

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

/** Names the turn a chat is on: the `ts` of its last turn-opening row. A
 *  queued message that starts right after the reply ends opens a new turn
 *  without the composer ever going idle, and the project switch applies to
 *  that turn, so the composer needs this to re-take its start project. A
 *  steer, or a send still parked behind the running turn, belongs to the turn
 *  already running and does not count. Older history loaded above the
 *  transcript leaves the last opener unchanged. */
export function stagedProjectTurnKey(messages: readonly ChatMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (opensRunTurn(m)) return `${m.role}:${m.ts ?? i}`
  }
  return ''
}

interface Baseline { start: string; turnKey: string | undefined }

export function useStagedProject(
  projectProp: string | undefined,
  isRunning: boolean,
  slotId: string | null | undefined,
  turnKey?: string,
  projectLocked = false,
  turnProject?: string | null,
): StagedProject {
  const project = projectProp || ''
  // The start project of each busy chat, keyed by slot. The composer is not
  // remounted per chat, so leaving a busy chat and coming back must restore
  // that chat's own start project: taking the current project then would
  // adopt the pending pick as the start and hide it. Taken on the first busy
  // render for a chat (during render, so the first busy frame is right),
  // re-taken when a new turn opens (`turnKey`), and dropped once the chat is
  // seen idle, so its next busy window starts fresh. While the chip is locked
  // (`projectLocked`: sub-agents still attached) the server keeps the project
  // reset deferred, so a turn a sub-agent completion opens still runs in the
  // start project: the held start survives that turn-key change.
  const baselines = useRef(new Map<string, Baseline>())
  const key = slotId ?? ''
  let start = project
  if (!isRunning) {
    baselines.current.delete(key)
  } else {
    let held = baselines.current.get(key)
    if (!held || (held.turnKey !== turnKey && !projectLocked)) {
      held = { start: project, turnKey }
      baselines.current.set(key, held)
    } else held.turnKey = turnKey
    // The server's own reading of the running turn's directory wins once the
    // turn has claimed its session (#20352): a composer mounted after a
    // reload would otherwise capture the already-picked project as the start.
    // It is kept for the rest of the busy window, because the server stops
    // naming it when the reply ends while attached sub-agents keep the
    // composer busy and the old directory in use.
    if (typeof turnProject === 'string') held.start = turnProject
    start = held.start
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
