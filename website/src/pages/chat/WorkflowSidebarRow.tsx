/**
 * WorkflowSidebarRow — expandable workflow-run card for the ActivityViewer
 * "Workflows" tab. Collapsed = compact summary (status + name + phase + last
 * log). Expanded = full hierarchical phase tree (via WorkflowRunTree)
 * plus a "View source" / Edit / Rerun panel (via WorkflowSourcePanel).
 *
 * Both surfaces (chat WorkflowProgressBar + this sidebar row) reuse the same
 * WorkflowRunTree + WorkflowSourcePanel + useRunSnapshot, so the tree and the
 * edit-rerun affordance behave identically.
 */
import { memo, useState } from 'react'
import { CheckCircle, AlertCircle, Loader as LoaderIcon, Workflow, ChevronDown } from 'lucide-react'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import WorkflowRunTree from '../../apps/workflows/WorkflowRunTree'
import WorkflowSourcePanel from '../../apps/workflows/WorkflowSourcePanel'
import { useRunSnapshot } from '../../apps/workflows/useRunSnapshot'

import { i18nT } from '../../i18n/t'
import { useI18nRevision } from '../../i18n/useI18nRevision'
export interface WfRunRow {
  run_id: string
  name?: string
  status: string
  event_count?: number
  phase?: string
  last_log?: string
  error?: string
  /** Originating chat session_key, used to scope a run to its own chat. */
  session_key?: string
}

const WorkflowSidebarRow = memo(function WorkflowSidebarRow({ row }: { row: WfRunRow }) {
  useI18nRevision()
  const [expanded, setExpanded] = useState(false)
  const { snapshot, error: snapshotError } = useRunSnapshot(row.run_id, {
    enabled: expanded,
  })

  const name = sanitizeLlmOutput((row.name || row.run_id).slice(0, 80))
  const phase = row.phase ? sanitizeLlmOutput(row.phase).slice(0, 40) : ''
  const lastLog = row.last_log ? sanitizeLlmOutput(row.last_log).slice(0, 200) : ''
  const errMsg = row.error ? sanitizeLlmOutput(row.error).slice(0, 200) : ''

  return (
    <div className="border border-border rounded text-[12px]">
      <button
        type="button"
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
        className="w-full px-3 py-2 hover:bg-bg-hover transition-colors text-left"
      >
        <div className="flex items-center gap-2">
          {row.status === 'running' ? (
            <LoaderIcon size={13} className="animate-spin text-accent shrink-0" />
          ) : row.status === 'finished' ? (
            <CheckCircle size={13} className="text-green-500 shrink-0" />
          ) : (
            <AlertCircle size={13} className="text-red-500 shrink-0" />
          )}
          <Workflow size={11} className="text-accent/70 shrink-0" />
          <span className="font-medium truncate">{name}</span>
          {row.status === 'running' && phase && (
            <span className="shrink-0 px-1.5 py-0.5 rounded bg-accent/10 border border-accent/20 text-[10px] text-accent">
              {phase}
            </span>
          )}
          <span className="ml-auto text-[10px] tabular-nums text-muted shrink-0">
            {row.status}
          </span>
          <ChevronDown
            size={12}
            className={`text-muted shrink-0 transition-transform ${expanded ? 'rotate-180' : ''}`}
          />
        </div>
        <div className="text-[10px] text-muted mt-0.5 font-mono truncate">
          {row.run_id} · {row.event_count ?? 0} {i18nT('pages.chat.workflowSidebarRow.events')}
        </div>
        {!expanded && row.status === 'running' && lastLog && (
          <div className="text-[11px] text-muted italic mt-0.5 truncate">
            {lastLog}
          </div>
        )}
        {!expanded && row.status === 'failed' && errMsg && (
          <div className="text-[11px] text-red-500 mt-0.5 truncate">{errMsg}</div>
        )}
      </button>

      {expanded && (
        <div className="px-3 pb-2 pt-1 border-t border-border flex flex-col gap-2">
          {snapshotError && (
            <div className="text-[11px] text-red-500 border border-red-500/30 rounded p-2">
              {i18nT('pages.chat.workflowSidebarRow.could_not_load_snapshot')} {sanitizeLlmOutput(snapshotError).slice(0, 200)}
            </div>
          )}
          <WorkflowRunTree
            events={snapshot?.events ?? []}
            status={
              (snapshot?.status as 'running' | 'finished' | 'failed' | 'cancelled' | undefined) ??
                (row.status as 'running' | 'finished' | 'failed' | 'cancelled' | undefined)
            }
            result={snapshot?.result}
            error={snapshot?.error ?? row.error ?? null}
          />
          <WorkflowSourcePanel
            run_id={row.run_id}
            source={snapshot ? snapshot.source ?? '' : null}
            sourceError={snapshotError}
          />
        </div>
      )}
    </div>
  )
})

export default WorkflowSidebarRow
