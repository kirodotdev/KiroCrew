/** An Ultra Mode root row's one state word, drawn so Running and Waiting read
 *  apart at a glance and without colour: Running is a solid dot and a heavier
 *  word, Waiting a hollow ring and the muted word. The marks are the row's own
 *  11px glyph size (the session count's bubble), and they never move, so a
 *  row the person is not looking at costs no frames. */
import { Circle } from 'lucide-react'
import { i18nT } from '../../i18n/t'

export type CrewStateKind = 'needs_you' | 'running' | 'paused' | 'stalled' | 'waiting' | 'done'

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
