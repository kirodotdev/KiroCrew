/**
 * Pure event-stream → view-model helpers shared by the Workflows page, the
 * chat WorkflowProgressBar (expanded tree), and the ActivityViewer Workflows
 * sidebar. Extracted from WorkflowsPage so all three surfaces fold the same
 * `workflow_run_event` stream into the same phase tree + budget snapshot.
 *
 * Pure / non-mutating. Unit-tested in src/test/WorkflowsPage.test.ts via the
 * re-exports in WorkflowsPage.tsx.
 */

export interface WfEvent {
  run_id: string
  seq: number
  ts: string
  type: string
  /** Unvalidated wire payload: the backend sends a different set of fields per
   *  `type`, and nothing here parses it, so every read is asserted at the point
   *  the `type` check has already picked the shape. */
  data: Record<string, unknown>
}

export interface AgentRow {
  agent_id: string
  label?: string
  last_tool?: string
  ok?: boolean
}

export interface PhaseGroup {
  title: string
  agents: AgentRow[]
}

/** Fold a run event stream into ordered phases each holding their agent rows. */
export function groupByPhase(events: WfEvent[]): PhaseGroup[] {
  const phases: PhaseGroup[] = []
  const byId = new Map<string, AgentRow>()
  let current = ''
  const ensure = (title: string): PhaseGroup => {
    let p = phases.find(x => x.title === title)
    if (!p) { p = { title, agents: [] }; phases.push(p) }
    return p
  }
  for (const e of events) {
    if (e.type === 'phase_started') {
      current = (e.data.title as string | undefined) || ''
      ensure(current)
    } else if (e.type === 'agent_started') {
      // Asserted, not coerced: the backend sends a different field set per `type`
      // and nothing here validates it, so these reads carry exactly the trust the
      // `any` did. Narrowing at runtime would change what a malformed event
      // renders, which a lint pass has no business deciding.
      const row: AgentRow = { agent_id: e.data.agent_id as string, label: e.data.label as string | undefined }
      byId.set(e.data.agent_id as string, row)
      ensure((e.data.phase as string | undefined) ?? current).agents.push(row)
    } else if (e.type === 'agent_progress') {
      const row = byId.get(e.data.agent_id as string)
      if (row) row.last_tool = e.data.last_tool as string | undefined
    } else if (e.type === 'agent_finished') {
      const row = byId.get(e.data.agent_id as string)
      if (row) row.ok = !!e.data.ok
    }
  }
  return phases
}

/**
 * Whether a run's originating `session_key` belongs to a given chat slot.
 *
 * A chat-launched run is tagged with the session_key the gateway resolves for the
 * slot, which is the slot's history key: ``dashboard:<slotKey>`` (see
 * ``_history_key_for`` on the backend). So a run belongs to slot ``chat-1-123``
 * when its session_key is ``dashboard:chat-1-123`` (or, defensively, ends with the
 * slot key). A run with NO session_key (UI-launched, no chat link) belongs to no
 * chat slot and is shown only in the standalone Workflows tab — never pinned to a
 * chat. This is what keeps an active run in ITS chat, not every open chat.
 */
export function runBelongsToSlot(sessionKey: string | undefined | null, slotKey: string | undefined | null): boolean {
  if (!sessionKey || !slotKey) return false
  if (sessionKey === slotKey) return true
  if (sessionKey === `dashboard:${slotKey}`) return true
  // Tolerate a leading "dashboard:" / "dashboard_" prefix on either side.
  return normalizeRunSessionKey(sessionKey) === normalizeRunSessionKey(slotKey)
}

/** Canonical form of a run's `session_key` — and of a slot key — for
 *  cross-referencing the two without pairwise `runBelongsToSlot` scans: a map
 *  of runs keyed by `normalizeRunSessionKey(session_key)` is looked up with
 *  `normalizeRunSessionKey(slotKey)` and matches exactly the pairs
 *  `runBelongsToSlot` accepts. Strips one leading "dashboard:" (the history
 *  key the gateway tags chat-launched runs with) or "dashboard_" (persisted
 *  key form) prefix. */
export function normalizeRunSessionKey(s: string): string {
  return s.replace(/^dashboard[:_]/, '')
}

/** Latest budget snapshot from the stream, if any. */
export function latestBudget(events: WfEvent[]): { spent: number; total: number | null } | null {
  let out: { spent: number; total: number | null } | null = null
  for (const e of events) {
    if (e.type === 'run_started') out = { spent: 0, total: (e.data.budget_total as number | null | undefined) ?? null }
    else if (e.type === 'budget_update') {
      const prevTotal: number | null = out ? out.total : null
      out = { spent: e.data.spent as number, total: prevTotal }
    }
  }
  return out
}
