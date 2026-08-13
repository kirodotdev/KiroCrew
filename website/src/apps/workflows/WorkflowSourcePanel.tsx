/**
 * WorkflowSourcePanel — view-source / edit / rerun affordance for a
 * dynamic-workflow run, shared by the chat WorkflowProgressBar and the
 * ActivityViewer Workflows sidebar.
 *
 * Behavior:
 *   - "View source" toggle reveals the authored Python script (read-only by
 *     default). Source is fetched lazily by the parent and passed in.
 *   - "Edit" flips the textarea to editable; "Rerun with edits" POSTs
 *     `{source}` to `/api/workflows/runs/{run_id}/rerun`. On 200 we show the
 *     new run_id; on 400 we surface validation errors inline.
 *
 * The panel is self-contained for its rerun POST so both surfaces get
 * identical behavior. Source-fetch is done by the parent (since both surfaces
 * already fetch the full run snapshot to drive the tree).
 */
import { memo, useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { FileCode, Pencil, Play, ChevronRight } from 'lucide-react'
import { sanitizeLlmOutput } from '../../utils/sanitize'

import { i18nT } from '../../i18n/t'
const CORE_API_BASE = '/api/workflows'

interface RerunOk {
  run_id: string
  edited?: boolean
  replayed_before?: number
}

interface RerunErr {
  error: string
  errors?: string[]
}

export interface WorkflowSourcePanelProps {
  run_id: string
  /** Authored workflow Python source. `null` while loading, `''` when fetch
   *  has completed but the snapshot did not include source. */
  source: string | null
  /** Optional fetch error to surface above the textarea. */
  sourceError?: string | null
}

const WorkflowSourcePanel = memo(function WorkflowSourcePanel({
  run_id,
  source,
  sourceError,
}: WorkflowSourcePanelProps) {
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<string>('')
  const [errors, setErrors] = useState<string[] | null>(null)
  const [topError, setTopError] = useState<string | null>(null)
  const [newRunId, setNewRunId] = useState<string | null>(null)

  // Reset draft when the upstream source changes (e.g. snapshot refetch).
  useEffect(() => {
    if (source != null) setDraft(source)
  }, [source])

  // Rerun-with-edits POST → useMutation. The endpoint distinguishes a 400
  // (validation errors, shown inline) from other failures, so the mutationFn
  // throws a typed marker we branch on in onError.
  const rerunMutation = useMutation({
    mutationFn: async (src: string) => {
      const r = await fetch(
        `${CORE_API_BASE}/runs/${encodeURIComponent(run_id)}/rerun`,
        {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source: src }),
        },
      )
      const body = (await r.json().catch(() => null)) as RerunOk | RerunErr | null
      if (r.ok && body && 'run_id' in body) return body
      // Reject with status + body so onError can show validation vs generic.
      throw { status: r.status, body }
    },
    onMutate: () => {
      setErrors(null)
      setTopError(null)
      setNewRunId(null)
    },
    onSuccess: data => {
      setNewRunId(data.run_id)
      setEditing(false)
    },
    onError: (err: unknown) => {
      const e = err as { status?: number; body?: RerunErr } | Error
      if (e instanceof Error) {
        setTopError(e.message)
      } else if (e.status === 400 && e.body && 'errors' in e.body) {
        setErrors(e.body.errors ?? [e.body.error || i18nT('apps.workflows.workflowSourcePanel.invalid_script')])
      } else {
        setTopError(i18nT('apps.workflows.workflowSourcePanel.rerun_failed', { status: e.status ?? 'error' }))
      }
    },
  })
  const submitting = rerunMutation.isPending
  const rerun = () => rerunMutation.mutate(draft)

  return (
    <div className="border border-border rounded">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] font-medium hover:bg-bg-hover transition-colors"
        aria-expanded={open}
      >
        <ChevronRight
          size={12}
          className={`text-muted shrink-0 transition-transform ${open ? 'rotate-90' : ''}`}
        />
        <FileCode size={12} className="text-muted shrink-0" />
        <span className="text-left">{i18nT('apps.workflows.workflowSourcePanel.view_source')}</span>
        <span className="ml-auto text-[10px] text-muted">
          {source == null ? i18nT('apps.workflows.workflowSourcePanel.loading') : `${source.split('\n').length} lines`}
        </span>
      </button>
      {open && (
        <div className="p-2 flex flex-col gap-2">
          {sourceError && (
            <div className="text-[11px] text-red-500 border border-red-500/30 rounded p-2">
              {i18nT('apps.workflows.workflowSourcePanel.could_not_load_source')} {sanitizeLlmOutput(sourceError).slice(0, 200)}
            </div>
          )}
          {source != null && source === '' && !sourceError && (
            <div className="text-[11px] text-muted italic">
              {i18nT('apps.workflows.workflowSourcePanel.no_source_captured_for_this_run')}
            </div>
          )}
          {source != null && source !== '' && (
            <textarea
              value={editing ? draft : source}
              onChange={e => setDraft(e.target.value)}
              readOnly={!editing}
              spellCheck={false}
              className={`font-mono text-[11px] leading-relaxed h-48 p-2 rounded border border-border bg-card resize-y ${editing ? '' : 'opacity-90'}`}
              aria-label={i18nT('apps.workflows.workflowSourcePanel.workflow_source_for', { runId: run_id })}
            />
          )}

          {errors && errors.length > 0 && (
            <div className="text-[11px] text-red-500 border border-red-500/30 rounded p-2">
              <div className="font-medium mb-1">{i18nT('apps.workflows.workflowSourcePanel.invalid_fix_before_rerun')}</div>
              <ul className="list-disc pl-4">
                {errors.map((e, i) => (
                  <li key={i}>{sanitizeLlmOutput(e).slice(0, 200)}</li>
                ))}
              </ul>
            </div>
          )}
          {topError && (
            <div className="text-[11px] text-red-500 border border-red-500/30 rounded p-2">
              {sanitizeLlmOutput(topError).slice(0, 200)}
            </div>
          )}
          {newRunId && (
            <div className="text-[11px] text-green-500 border border-green-500/30 rounded p-2 font-mono">
              {i18nT('apps.workflows.workflowSourcePanel.started_run')} {sanitizeLlmOutput(newRunId).slice(0, 80)}
            </div>
          )}

          <div className="flex items-center gap-2">
            {!editing ? (
              <button
                type="button"
                onClick={() => setEditing(true)}
                disabled={source == null || source === ''}
                className="flex items-center gap-1 px-2 py-1 text-[11px] rounded border border-border disabled:opacity-50"
              >
                <Pencil size={11} /> {i18nT('apps.workflows.workflowSourcePanel.edit')}
              </button>
            ) : (
              <>
                <button
                  type="button"
                  onClick={rerun}
                  disabled={submitting}
                  className="flex items-center gap-1 px-2 py-1 text-[11px] rounded bg-accent text-accent-fg disabled:opacity-50"
                >
                  <Play size={11} /> {submitting ? i18nT('apps.workflows.workflowSourcePanel.rerunning') : i18nT('apps.workflows.workflowSourcePanel.rerun_with_edits')}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setEditing(false)
                    if (source != null) setDraft(source)
                    setErrors(null)
                    setTopError(null)
                  }}
                  disabled={submitting}
                  className="px-2 py-1 text-[11px] rounded border border-border disabled:opacity-50"
                >
                  {i18nT('apps.workflows.workflowSourcePanel.cancel')}
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
})

export default WorkflowSourcePanel
