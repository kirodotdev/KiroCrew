/** An Ultra Mode root row's one state word, drawn so Running and Waiting read
 *  apart at a glance and without colour: Running is a solid dot and a heavier
 *  word, Waiting a hollow ring and the muted word. The marks are the row's own
 *  11px glyph size (the session count's bubble), and they never move, so a
 *  row the person is not looking at costs no frames. */
import { Circle } from 'lucide-react'
import { i18nT } from '../../i18n/t'

export type CrewStateKind = 'needs_you' | 'running' | 'paused' | 'stalled' | 'quiet' | 'waiting' | 'done'

export interface CrewStateStyle {
  /** Catalog key of the word. */
  wordKey: string
  /** Classes on the word. */
  tone: string
  /** The glyph in front of the word: a solid dot, a hollow ring, or none. */
  mark: 'filled' | 'ring' | null
}

export const CREW_STATE_STYLE: Record<CrewStateKind, CrewStateStyle> = {
  needs_you: { wordKey: 'components.crewMode.ultra_needs_you', tone: 'crew-tone-warn font-medium', mark: null },
  running: { wordKey: 'commandCenter.running', tone: 'text-text font-semibold', mark: 'filled' },
  // A stalled goal loop is a failure, not live work.
  stalled: { wordKey: 'components.crewMode.ultra_paused', tone: 'text-danger font-medium', mark: null },
  paused: { wordKey: 'components.crewMode.ultra_paused', tone: 'crew-tone-warn', mark: null },
  // Open ledger work gone quiet, its next step not taken (`lib/crewWork.ts`).
  quiet: { wordKey: 'components.crewMode.ultra_row_stalled', tone: 'crew-tone-warn font-medium', mark: null },
  waiting: { wordKey: 'commandCenter.worker_group_waiting', tone: 'text-muted', mark: 'ring' },
  done: { wordKey: 'commandCenter.worker_group_done', tone: 'crew-tone-ok', mark: null },
}

/** The dot wears the working count's green; the ring the waiting count's muted tone. */
const MARK_TONE = { filled: 'text-[var(--ok)]', ring: 'crew-tone-muted' } as const

export function crewStateWord(kind: CrewStateKind): string {
  return i18nT(CREW_STATE_STYLE[kind].wordKey)
}

export function CrewStateWord({ kind, title, className = '' }: { kind: CrewStateKind; title?: string; className?: string }) {
  const { tone, mark } = CREW_STATE_STYLE[kind]
  return (
    <span className={`shrink-0 inline-flex items-center gap-1 whitespace-nowrap text-[12px] ${tone} ${className}`} data-testid="crew-root-state" data-state={kind} title={title || undefined}>
      {mark && (
        <Circle size={11} aria-hidden="true" data-testid="crew-root-state-mark" data-mark={mark}
          className={`inline-block shrink-0 ${MARK_TONE[mark]}`} fill={mark === 'filled' ? 'currentColor' : 'none'} />
      )}
      {crewStateWord(kind)}
    </span>
  )
}

/** The Ultra Mode row's own state as one short word, from the row-state branch
 *  that won: what the person owes reads "Needs you", live work "Running", an
 *  interrupted turn "Paused", queued work "Waiting". The branch's own line
 *  stays in the tooltip. */
export interface CrewRowOwnState { kind: CrewStateKind; word: string; title: string }

const CREW_NEEDS_YOU_KEYS = new Set(['pending_approval', 'subagent_awaiting', 'crew_approval', 'needs_input', 'loop_waiting'])
const CREW_RUNNING_KEYS = new Set(['running', 'goal_loop', 'subagents', 'snapshot_live_work', 'workflow', 'structured_monitor_action'])
const CREW_WAITING_KEYS = new Set(['subagents_waiting', 'structured_monitor_passive', 'crew_waiting'])

/** `working`: one of the crew's sessions is mid-turn (or the root's own live work).
 *  A branch that only says the root itself is idle -- a passive patrol, queued
 *  children -- then reads "Running": the crew is, and the board shows it so. */
function crewRowOwnKind(key: string, loopStalled: boolean, working: boolean): CrewStateKind | null {
  // A stalled goal loop is a failure, not live work.
  if (key === 'goal_loop' && loopStalled) return 'stalled'
  if (CREW_NEEDS_YOU_KEYS.has(key)) return 'needs_you'
  if (CREW_RUNNING_KEYS.has(key)) return 'running'
  if (key === 'interrupted') return 'paused'
  // Open ledger work that has gone quiet: nothing failed, nothing runs.
  if (key === 'crew_stalled') return 'quiet'
  if (CREW_WAITING_KEYS.has(key) && working) return 'running'
  // Queued children and a passive monitor are unfinished, never "Done".
  if (CREW_WAITING_KEYS.has(key)) return 'waiting'
  return null
}

export function crewRowOwnState(key: string | undefined, title: string, loopStalled = false, working = false): CrewRowOwnState | null {
  const kind = key ? crewRowOwnKind(key, loopStalled, working) : null
  return kind ? { kind, word: crewStateWord(kind), title } : null
}
