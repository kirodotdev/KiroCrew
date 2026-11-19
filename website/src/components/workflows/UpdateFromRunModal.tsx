/**
 * UpdateFromRunModal — append a finished run's exact source to a saved workflow
 * as its next revision, from the chat run card or the Workflows Runs panel.
 *
 * The server suggests the target from the run's lineage or name; the owner can
 * pick another. The dialog shows revision N → N+1 and the server's diff, and
 * Apply sends back the revision and source digest it showed. A 409 (the workflow
 * gained a revision, or the run's source changed) reloads the preview so the
 * owner reviews the new diff before applying again.
 *
 * Backend contract:
 *   GET  /api/workflows/runs/{id}/update-definition?workflow= -> WorkflowRunUpdatePreview
 *   POST /api/workflows/runs/{id}/update-definition           -> { ok, definition }
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, GitCommitHorizontal } from 'lucide-react'
import { api, type WorkflowDefinition } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import Modal from '../Modal'
import ErrorNotice from '../ErrorNotice'
import SimpleSelect from '../SimpleSelect'
import { Btn } from '../ui'
import { ScriptDiff } from '../../cards/CardChanges'
import { i18nT } from '../../i18n/t'

/** The refusals that mean "what you reviewed is out of date", not "this failed". */
const STALE_CODES: ReadonlySet<string> = new Set([
  'workflow_definition_conflict',
  'workflow_run_source_changed',
])

/** The first of `refs` the read does not list in `d.shadowed_refs`, so a request
 *  by it reaches `d`; null when none does. */
function reachingRef(d: WorkflowDefinition, refs = [d.slug, d.id]): string | null {
  return refs.find(ref => !!ref && !d.shadowed_refs?.includes(ref)) ?? null
}

export default function UpdateFromRunModal({
  runId,
  onClose,
  onUpdated,
}: {
  runId: string
  onClose: () => void
  onUpdated: (definition: WorkflowDefinition) => void
}) {
  const queryClient = useQueryClient()
  // The ref the owner picked; '' leaves the choice to the server's suggestion.
  const [picked, setPicked] = useState('')
  const definitions = useQuery({
    queryKey: ['workflow-definitions'],
    queryFn: () => api.workflowDefinitions(),
  })
  const preview = useQuery({
    queryKey: ['workflow-run-update-preview', runId, picked],
    queryFn: () => api.previewWorkflowRunUpdate(runId, picked),
    staleTime: 0,
  })
  const target = preview.data?.target ?? null
  // Only a ref that reaches its record is offered: a shadowed one resolves to
  // another workflow than the label names.
  const choices = (definitions.data?.definitions ?? []).filter(d => reachingRef(d) !== null)
  const targetRecord = target ? choices.find(d => d.id === target.id) : undefined
  const pickedRef = targetRecord ? reachingRef(targetRecord) : null
  // Apply prefers the id, which a rename does not move; expected_id pins the write
  // to the previewed workflow, so a ref that reaches another one since answers 409.
  const targetRef = targetRecord ? reachingRef(targetRecord, [targetRecord.id, targetRecord.slug]) : null
  const apply = useMutation({
    mutationFn: () =>
      api.updateWorkflowRunDefinition(runId, {
        workflow: targetRef!,
        expected_id: target!.id,
        expected_revision: target!.revision,
        expected_source_sha256: preview.data!.run_source_sha256,
      }),
    onSuccess: async result => {
      onUpdated(result.definition)
      await queryClient.invalidateQueries({ queryKey: ['workflow-definitions'] })
    },
    onError: async err => {
      if (STALE_CODES.has(apiErrorCode(err))) await preview.refetch()
    },
  })
  const stale = STALE_CODES.has(apiErrorCode(apply.error))
  // A failed preview refresh keeps the last good data; it must not be applied.
  // Until the definitions list loads, the ref that reaches the target is unknown.
  const blocked =
    !target ||
    !targetRef ||
    preview.data?.unchanged ||
    preview.data?.format_mismatch ||
    !!preview.error ||
    !definitions.isSuccess
  const failure = (stale ? null : apply.error) ?? preview.error ?? definitions.error
  const failureText = failure
    ? failure instanceof Error && failure.message
      ? failure.message
      : i18nT('pages.overview.workflowLibrary.request_failed')
    : null

  return (
    <Modal
      open
      onClose={onClose}
      title={i18nT('pages.chat.workflowRunCard.update_title')}
      maxWidth={760}
      // A pending Apply's failure shows only in this dialog, so it stays open.
      dismissDisabled={apply.isPending}
      footer={
        <>
          <Btn onClick={onClose} disabled={apply.isPending}>
            {i18nT('pages.chat.workflowRunCard.cancel')}
          </Btn>
          <Btn
            primary
            onClick={() => apply.mutate()}
            disabled={!!blocked || apply.isPending || preview.isFetching}
          >
            {apply.isPending ? (
              <Loader2 className="lucide-inline animate-spin" />
            ) : (
              <GitCommitHorizontal className="lucide-inline" />
            )}
            {i18nT('pages.chat.workflowRunCard.update_apply')}
          </Btn>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-[13px] text-muted">{i18nT('pages.chat.workflowRunCard.update_help')}</p>
        <div className="block text-[12px] text-muted">
          <span className="block mb-1">{i18nT('pages.chat.workflowRunCard.update_target')}</span>
          <SimpleSelect
            aria-label={i18nT('pages.chat.workflowRunCard.update_target')}
            options={choices.map(d => reachingRef(d)!)}
            optionLabels={choices.map(d => d.name || d.slug)}
            value={picked || pickedRef || ''}
            triggerFallback={i18nT('pages.chat.workflowRunCard.update_pick')}
            onChange={ref => {
              apply.reset()
              setPicked(ref)
            }}
            disabled={apply.isPending}
            className="w-full"
          />
        </div>
        {preview.isLoading ? <Loader2 className="lucide-inline animate-spin text-muted" /> : null}
        {target ? (
          <p data-testid="workflow-update-target" className="text-[13px] font-medium text-text">
            {target.name || target.slug}
          </p>
        ) : null}
        {target ? (
          <p data-testid="workflow-update-revision" className="text-[12px] text-muted font-mono">
            {i18nT('pages.chat.workflowRunCard.update_revision', { from: target.revision, to: target.revision + 1 })}
          </p>
        ) : null}
        {/* Blocking impact (the default): a 409 is a pressed Apply that wrote nothing.
            The dialog holds no draft (the diff is re-read from the server), so a
            hand-off loses nothing. */}
        <ErrorNotice
          variant="inline"
          askAgent
          testId="workflow-update-stale"
          message={stale ? i18nT('pages.chat.workflowRunCard.update_stale') : null}
        />
        {preview.data?.unchanged ? (
          <p className="text-[12px] text-muted">{i18nT('pages.chat.workflowRunCard.update_unchanged')}</p>
        ) : preview.data?.format_mismatch ? (
          <p className="text-[12px] text-muted">{i18nT('pages.chat.workflowRunCard.update_format_mismatch')}</p>
        ) : preview.data?.diff ? (
          <ScriptDiff diff={preview.data.diff} />
        ) : null}
        <ErrorNotice variant="inline" askAgent testId="workflow-update-error" message={failureText} />
      </div>
    </Modal>
  )
}
