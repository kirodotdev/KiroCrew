import { useEffect, useMemo, memo, useState } from 'react'
import { Workflow, Loader2, CheckCircle2, AlertCircle, ChevronDown } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../../store'
import { clearWorkflowRun } from '../../store/chatSlice'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { WorkflowRunProgress } from '../../store/chatSlice'
import WorkflowRunTree from '../../apps/workflows/WorkflowRunTree'
import WorkflowSourcePanel from '../../apps/workflows/WorkflowSourcePanel'
import { useRunSnapshot } from '../../apps/workflows/useRunSnapshot'
import { runBelongsToSlot } from '../../apps/workflows/runModel'

import { i18nT } from '../../i18n/t'
import { useI18nRevision } from '../../i18n/useI18nRevision'
const EMPTY_RUNS: Record<string, WorkflowRunProgress> = {}
// How long a finished/failed/cancelled run lingers before being dropped.
const TERMINAL_LINGER_MS = 4000

/** Compact dynamic-workflow run indicator above the chat input. Mirrors
 *  SubagentProgressBar's style and placement: shows the run name, current
 *  phase, and the most recent narrator log line for any RUNNING workflow.
 *  Briefly shows ✓/✗ on terminal states, then drops the entry.
 *
 *  Each row is expandable: clicking toggles a hierarchical phase tree
 *  (powered by <WorkflowRunTree>) plus a "View source" / Edit / Rerun panel.
 *  The full run snapshot (with events + source) is fetched on expand and
 *  refreshed every ~2s while the run is still running. */
const WorkflowProgressBar = memo(function WorkflowProgressBar({ slot }: { slot: string | null }) {
  useI18nRevision()
  const dispatch = useAppDispatch()
  const runs = useAppSelector(s => s.chat.workflowRuns ?? EMPTY_RUNS)
  // Only show runs launched FROM this chat session — a run sticks to the chat
  // that triggered it, not every open chat. Runs without a session_key
  // (UI-launched) belong to no chat and only appear in the Workflows tab.
  const list = useMemo(
    () => Object.values(runs).filter(r => runBelongsToSlot(r.sessionKey, slot)),
    [runs, slot],
  )
  const visible = list.filter(r => r.status === 'running' || r.status === 'finished' || r.status === 'failed' || r.status === 'cancelled')

  // Drop terminal runs after a brief linger so users get a glance at ✓/✗.
  const [terminalSeenAt, setTerminalSeenAt] = useState<Record<string, number>>({})
  useEffect(() => {
    const now = Date.now()
    const next: Record<string, number> = {}
    let changed = false
    for (const r of list) {
      if (r.status !== 'running') {
        next[r.run_id] = terminalSeenAt[r.run_id] ?? now
        if (terminalSeenAt[r.run_id] == null) changed = true
      }
    }
    // Drop entries no longer present
    for (const k of Object.keys(terminalSeenAt)) if (!(k in next)) changed = true
    if (changed) setTerminalSeenAt(next)
  }, [list, terminalSeenAt])

  useEffect(() => {
    const ids = Object.keys(terminalSeenAt)
    if (ids.length === 0) return
    const t = setInterval(() => {
      const now = Date.now()
      ids.forEach(id => {
        if (now - (terminalSeenAt[id] ?? 0) >= TERMINAL_LINGER_MS) {
          dispatch(clearWorkflowRun(id))
        }
      })
    }, 500)
    return () => clearInterval(t)
  }, [terminalSeenAt, dispatch])

  // Track which runs the user has expanded. Keyed by run_id.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const toggle = (run_id: string) =>
    setExpanded(s => ({ ...s, [run_id]: !s[run_id] }))

  if (visible.length === 0) return null

  return (
    <div className="px-5 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="mb-1 rounded-md bg-accent/10 border border-accent/20 animate-slide-up overflow-hidden">
        {visible.map(r => (
          <ExpandableRunRow
            key={r.run_id}
            run={r}
            expanded={!!expanded[r.run_id]}
            onToggle={() => toggle(r.run_id)}
          />
        ))}
      </div>
    </div>
  )
})

/** A single expandable run row. Collapsed state is the original one-liner;
 *  expanded state fetches the full snapshot and renders the phase tree +
 *  view-source/edit/rerun panel. */
function ExpandableRunRow({
  run,
  expanded,
  onToggle,
}: {
  run: WorkflowRunProgress
  expanded: boolean
  onToggle: () => void
}) {
  const name = sanitizeLlmOutput((run.name || run.run_id).slice(0, 60))
  const phase = sanitizeLlmOutput((run.phase || '').slice(0, 40))
  const lastLog = sanitizeLlmOutput((run.lastLog || '').slice(0, 100))
  const errMsg = sanitizeLlmOutput((run.error || '').slice(0, 100))

  // Fetch full snapshot only while expanded; poll while still running.
  const { snapshot, error: snapshotError } = useRunSnapshot(run.run_id, {
    enabled: expanded,
  })

  return (
    <div className="border-b border-accent/10 last:border-b-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="w-full px-3 py-1.5 text-[13px] font-mono flex items-start gap-2 hover:bg-accent/5 transition-colors text-left"
      >
        <span className="shrink-0 mt-0.5">
          {run.status === 'running' && <Loader2 size={14} className="text-accent animate-spin" />}
          {run.status === 'finished' && <CheckCircle2 size={14} className="text-green-500" />}
          {(run.status === 'failed' || run.status === 'cancelled') && <AlertCircle size={14} className="text-danger" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <Workflow size={12} className="text-accent/70 shrink-0" />
            <span className="truncate text-text-strong font-medium">{name}</span>
            {phase && run.status === 'running' && (
              <span className="shrink-0 text-accent/80 italic">· {phase}…</span>
            )}
          </div>
          {run.status === 'running' && lastLog && (
            <div className="text-muted truncate italic text-[12px]">{lastLog}</div>
          )}
          {run.status === 'failed' && errMsg && (
            <div className="text-danger truncate text-[12px]">{errMsg}</div>
          )}
        </div>
        <ChevronDown
          size={14}
          className={`text-muted shrink-0 mt-0.5 transition-transform ${expanded ? 'rotate-180' : ''}`}
        />
      </button>

      {expanded && (
        <div className="px-3 pb-2 pt-1 flex flex-col gap-2">
          {snapshotError && (
            <div className="text-[11px] text-red-500 border border-red-500/30 rounded p-2">
              {i18nT('pages.chat.workflowProgressBar.could_not_load_run_snapshot')} {sanitizeLlmOutput(snapshotError).slice(0, 200)}
            </div>
          )}
          <WorkflowRunTree
            events={snapshot?.events ?? []}
            status={
              (snapshot?.status as 'running' | 'finished' | 'failed' | 'cancelled' | undefined) ??
                run.status
            }
            result={snapshot?.result}
            error={snapshot?.error ?? run.error ?? null}
          />
          <WorkflowSourcePanel
            run_id={run.run_id}
            source={snapshot ? snapshot.source ?? '' : null}
            sourceError={snapshotError}
          />
        </div>
      )}
    </div>
  )
}

export default WorkflowProgressBar
