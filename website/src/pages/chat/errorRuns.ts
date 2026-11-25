/**
 * errorRuns — draw a run of identical failures as ONE card.
 *
 * A chat whose agent keeps failing the same way writes one red row per attempt,
 * so nine failed starts stacked nine identical cards, every older one still
 * reading as live advice (#38910). A run of rows carrying the same notice
 * (`errorNoticeRunKey`: the token and its values) draws one card, on the run's
 * FIRST row, so the card keeps its place while later attempts land: it says how
 * many times it happened and when the newest did, and lists every attempt's
 * time when expanded. The later rows draw nothing. The card offers Resume only
 * when the run's newest row is the chat's newest error, as that row would.
 *
 * Two attempts are consecutive when nothing but the Resume press that re-ran
 * the first lies between them: its `inject` row continues the same turn (the
 * way `sessionStartFailureStreak` reads it too), so it folds into the run. Any
 * other row ends the run, a different failure included. A Resume press after
 * the run's newest row stays drawn, since its attempt has not failed yet.
 *
 * Pure over `messages`, keyed by row REFERENCE and built once per array
 * identity (weakMapMemoize), like editRuns.
 */
import { createSelector } from '@reduxjs/toolkit'
import type { ChatMessage } from '../../types'
import { errorNoticeRunKey } from './gatewayNotice'
import { resolveInjectCard } from './RecoveryCard'

export interface ErrorRunIndex {
  /** A run's first row → every row of the run, oldest first. Runs of two or more only. */
  attemptsByLead: Map<ChatMessage, readonly ChatMessage[]>
  /** The later rows of every run, and the Resume presses between its rows: they draw nothing. */
  folded: Set<ChatMessage>
}

/** The `inject` row a Resume press lands as (RecoveryCard's `manual` kind). */
const isResumePress = (m: ChatMessage): boolean => m.role === 'inject' && resolveInjectCard(m)?.kind === 'manual'

function buildErrorRuns(messages: readonly ChatMessage[]): ErrorRunIndex {
  const index: ErrorRunIndex = { attemptsByLead: new Map(), folded: new Set() }
  let run: { key: string; attempts: ChatMessage[]; presses: ChatMessage[] } | null = null
  for (const m of messages) {
    if (run && isResumePress(m)) { run.presses.push(m); continue }
    const key = errorNoticeRunKey(m)
    if (run && key !== null && run.key === key) {
      for (const press of run.presses) index.folded.add(press)
      run.presses = []
      run.attempts.push(m)
      index.folded.add(m)
      index.attemptsByLead.set(run.attempts[0], run.attempts)
      continue
    }
    run = key !== null ? { key, attempts: [m], presses: [] } : null
  }
  return index
}

export const selectErrorRuns = createSelector([(messages: readonly ChatMessage[]) => messages], buildErrorRuns)
