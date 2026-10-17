/** Live dynamic-workflow runs (`workflowRuns`): the event fold, the monotonic
 *  reconcile against the authoritative run list, and the sidebar's
 *  per-session activity selectors. */
import { createSelector, type PayloadAction } from '@reduxjs/toolkit'
import type { RootState } from '../index'
import type { WorkflowRunSummary } from '../../types'
import { normalizeRunSessionKey } from '../../apps/workflows/runModel'
import type { ChatState, WorkflowRunProgress } from './state'
import { isUnsafeKey, safeKey, workflowText } from './wire'

/** The statuses a run has ENDED in. Spelled once so the reducer, the reconcile
 *  and any surface deciding "is this still live?" cannot drift apart — and so an
 *  unrecognised status from a newer backend reads as "not terminal / unknown"
 *  rather than accidentally matching. */
export const WORKFLOW_TERMINAL_STATUSES = ['finished', 'failed', 'cancelled'] as const

export function isTerminalWorkflowStatus(status: string | undefined | null): boolean {
  return !!status && (WORKFLOW_TERMINAL_STATUSES as readonly string[]).includes(status)
}

/** Live dynamic-workflow activity per originating session, keyed by the
 *  NORMALIZED session key (`normalizeRunSessionKey`), so a slot looks itself
 *  up with the same normalization `runBelongsToSlot` applies — this replaces
 *  a per-slot scan over every run. Values carry the count of running runs
 *  plus the RAW name/phase of the first matching run in insertion order;
 *  they are agent-authored wire strings, so rendering sanitizes at the edge.
 *  Memoized on `workflowRuns` identity, which lets a session row read its one
 *  key with `shallowEqual` and ignore every other run's events. */
export const selectSidebarWorkflowActive = createSelector(
  [(state: RootState) => state.chat.workflowRuns],
  (workflowRuns) => {
    // Null prototype: the accumulator is indexed by a normalized session key
    // from the wire, and on a `{}` literal a key like "__proto__" would READ
    // Object.prototype as a truthy existing entry and then mutate it —
    // corrupting every object in the page. Object.create(null) makes such a
    // key an ordinary own property. (Same threat model as the goalLoops
    // safeKey normalization.)
    const active: Record<string, { count: number; name: string; phase: string }> = Object.create(null)
    for (const r of Object.values(workflowRuns ?? {})) {
      // A run with NO sessionKey is UI-launched (no chat link) and belongs to
      // no slot — the same exclusion runBelongsToSlot encodes.
      if (r.status !== 'running' || !r.sessionKey) continue
      const key = normalizeRunSessionKey(r.sessionKey)
      const cur = active[key]
      if (cur) cur.count += 1
      else active[key] = { count: 1, name: r.name || r.run_id, phase: r.phase || '' }
    }
    return active
  },
)

/** Just the keys of `selectSidebarWorkflowActive` — the sidebar shell's
 *  presence signal (the In-progress filter and the board's state lanes need
 *  "which sessions have a live run", never the label). Subscribed with
 *  `shallowEqual`, it re-renders the shell only when the SET of
 *  workflow-active sessions changes, not on every phase/progress event. */
export const selectSidebarWorkflowActiveKeys = createSelector(
  [selectSidebarWorkflowActive],
  (active) => Object.keys(active),
)

export const workflowReducers = {
  /** Fold a single dynamic-workflow run event into workflowRuns. */
  sseWorkflowEvent(state: ChatState, action: PayloadAction<{ run_id: string; session_key?: string; seq?: number; ts?: number; type: string; data?: Record<string, unknown> }>) {
    const { run_id, type, data, session_key } = action.payload
    if (isUnsafeKey(run_id)) return
    if (!run_id) return
    const d = (data || {}) as Record<string, unknown>
    const cur = state.workflowRuns[run_id] ?? {
      run_id, name: '', phase: '', lastLog: '', status: 'running' as const,
    }
    if (session_key && !cur.sessionKey) cur.sessionKey = session_key
    switch (type) {
      case 'run_started':
        cur.name = workflowText(d.name) || cur.name || run_id
        cur.status = 'running'
        break
      case 'phase_started':
        cur.phase = workflowText(d.title) || cur.phase
        break
      case 'log': {
        const msg = workflowText(d.message)
        if (msg) cur.lastLog = msg
        break
      }
      case 'run_finished':
        cur.status = 'finished'
        break
      case 'run_failed':
        cur.status = 'failed'
        cur.error = workflowText(d.error) || cur.error
        break
      case 'run_cancelled':
        cur.status = 'cancelled'
        break
      default:
        break
    }
    state.workflowRuns[safeKey(run_id)] = cur
  },
  clearWorkflowRun(state: ChatState, action: PayloadAction<string>) {
    delete state.workflowRuns[action.payload]
  },
  /** Fold the AUTHORITATIVE run list (`GET /api/workflows/runs`) into
   *  `workflowRuns`, correcting rows the live event stream could not.
   *
   *  `workflow_run_event` frames are one-shot and never replayed, so a client
   *  that was closed, asleep, or disconnected when a run ended holds an entry
   *  frozen at `running` forever: the spinner keeps spinning, the phase and log
   *  lines keep rendering as live, and the terminal-linger cleanup — which only
   *  tracks entries that have reached a terminal status — never arms to drop it.
   *  A gateway restart is the same case from the other side: the registry marks
   *  a run that was still running as failed (interrupted), and only this read
   *  carries that to a tab that stayed open across the restart.
   *
   *  The merge is deliberately MONOTONIC, because the snapshot is a point-in-time
   *  read that races the live stream (frames can land while the request is in
   *  flight) and a workflow status only ever moves one way, running → terminal:
   *   - a local entry already TERMINAL is never touched — the snapshot cannot be
   *     newer than the frame that ended it, so "re-opening" it could only undo
   *     truth the client already has;
   *   - a running local entry is only ever advanced to terminal, never rewound;
   *   - progress fields (`phase`, `lastLog`) are filled only when EMPTY, since a
   *     live frame's value is newer than any value this response carries;
   *   - a row absent locally is SEEDED only while it is still running — that is
   *     the reload / late-join case (nothing else seeds this slice, so a run
   *     started before the tab opened is otherwise invisible). A terminal row is
   *     never resurrected: the run is over and re-adding it would show a wall of
   *     ✓ rows above the composer on every reconnect.
   *   - an unrecognised status is not evidence and is skipped entirely, so a
   *     future backend state cannot silently clear a spinner or seed a row.
   *
   *  A failed request must NOT reach here at all: an absent list means the
   *  authority could not be read, not that no runs exist. Callers pass only a
   *  real `runs` array (see `syncWorkflowRuns` in useWebSocket).
   *
   *  Absence from a SUCCESSFUL response is likewise not evidence: the registry
   *  evicts old runs (200 by default), so a long-lived entry can legitimately
   *  drop out of the list. Such an entry is left alone rather than guessed at.
   */
  reconcileWorkflowRuns(state: ChatState, action: PayloadAction<WorkflowRunSummary[]>) {
    for (const row of action.payload ?? []) {
      const runId = row?.run_id
      if (typeof runId !== 'string' || !runId || isUnsafeKey(runId)) continue
      const status = row.status
      const terminal = isTerminalWorkflowStatus(status)
      if (!terminal && status !== 'running') continue  // unknown status: no evidence
      const key = safeKey(runId)
      const cur = state.workflowRuns[key]
      if (!cur) {
        if (terminal) continue  // over and gone — never resurrect
        state.workflowRuns[key] = {
          run_id: runId,
          name: workflowText(row.name) || runId,
          phase: workflowText(row.phase),
          lastLog: workflowText(row.last_log),
          status: 'running',
          sessionKey: workflowText(row.session_key) || undefined,
        }
        continue
      }
      if (cur.status !== 'running') continue  // terminal locally: one-way, done
      if (!cur.name) cur.name = workflowText(row.name) || cur.name
      if (!cur.sessionKey && workflowText(row.session_key)) cur.sessionKey = workflowText(row.session_key)
      if (!terminal) {
        // Still running per the authority — the live stream owns progress, so
        // only fill what this client never received.
        if (!cur.phase && workflowText(row.phase)) cur.phase = workflowText(row.phase)
        if (!cur.lastLog && workflowText(row.last_log)) cur.lastLog = workflowText(row.last_log)
        continue
      }
      cur.status = status as WorkflowRunProgress['status']
      if (workflowText(row.error)) cur.error = workflowText(row.error)
    }
  },
}
