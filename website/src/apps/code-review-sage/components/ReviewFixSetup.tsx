import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { Wrench } from 'lucide-react'

import { Btn, Input, SendBtn, Badge } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { i18nT } from '../../../i18n/t'
import { sageApi, SageApiError } from '../api'
import type {
  ReviewFixFindingSnapshot,
  ReviewFixTaskResponse,
} from '../lib/types'
import ReviewModelPicker from './ReviewModelPicker'

interface Props {
  findings: ReviewFixFindingSnapshot[]
  reviewRunId: string
  prUrl: string
  sourceHeadSha: string
  model: string
  onModelChange: (model: string) => void
  onCreated: (response: ReviewFixTaskResponse) => void
  onClose: () => void
}

function errorCopy(error: unknown): string {
  const code = error instanceof SageApiError ? error.code : ''
  switch (code) {
    case 'target_required':
      return i18nT('apps.codeReviewSage.reviewFix.target_path_required')
    case 'target_denied':
      return i18nT('apps.codeReviewSage.reviewFix.target_path_not_allowed')
    case 'findings_required':
      return i18nT('apps.codeReviewSage.reviewFix.no_eligible_findings_selected')
    case 'blocked_model_resolution':
      return i18nT('apps.codeReviewSage.reviewFix.model_resolution_blocked')
    default:
      return i18nT('apps.codeReviewSage.reviewFix.creation_failed')
  }
}

export default function ReviewFixSetup({
  findings, reviewRunId, prUrl, sourceHeadSha, model, onModelChange, onCreated, onClose,
}: Props) {
  const [targetPath, setTargetPath] = useState('')
  const create = useMutation({
    mutationFn: () => sageApi.createFixTask({
      target_path: targetPath.trim(),
      findings,
      review_run_id: reviewRunId,
      pr_url: prUrl,
      source_head_sha: sourceHeadSha,
      target_mode: 'current_branch',
      model,
    }),
    onSuccess: onCreated,
  })

  return (
    <section
      className="rounded-lg border border-accent/40 bg-accent-subtle/40 p-4 flex flex-col gap-3"
      aria-labelledby="review-fix-setup-title"
    >
      <div className="flex items-start gap-3">
        <Wrench size={18} className="lucide-inline text-accent mt-0.5" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <h2 id="review-fix-setup-title" className="text-[14px] font-semibold text-text-strong">
            {i18nT('apps.codeReviewSage.reviewFix.setup_title')}
          </h2>
          <p className="mt-1 text-[12.5px] leading-relaxed text-muted">
            {i18nT('apps.codeReviewSage.reviewFix.setup_description')}
          </p>
        </div>
        <Btn type="button" onClick={onClose} aria-label={i18nT('apps.codeReviewSage.reviewFix.close_setup')}>
          {i18nT('apps.codeReviewSage.reviewFix.cancel')}
        </Btn>
      </div>

      <div className="flex flex-col gap-2">
        <div className="text-[12px] font-semibold text-muted">
          {i18nT('apps.codeReviewSage.reviewFix.selected_findings', { count: findings.length })}
        </div>
        <div className="flex flex-col gap-1.5 max-h-40 overflow-y-auto">
          {findings.map((finding) => (
            <div key={finding.key} className="flex items-start gap-2 rounded-md border border-border bg-card px-2.5 py-2">
              <Badge variant={finding.severity === 'red' ? 'err' : 'warn'}>{finding.severity}</Badge>
              <div className="min-w-0 flex-1">
                <div className="text-[12.5px] text-text-strong break-words">{finding.title}</div>
                {finding.file_path && (
                  <div className="font-mono text-[11px] text-muted truncate">
                    {finding.file_path}{finding.line == null ? '' : `:${finding.line}`}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="flex flex-col gap-1.5 text-[12px] font-semibold text-muted">
        {i18nT('apps.codeReviewSage.reviewFix.target_repository')}
        <Input
          aria-label={i18nT('apps.codeReviewSage.reviewFix.target_repository')}
          value={targetPath}
          onChange={(event) => setTargetPath(event.target.value)}
          placeholder={i18nT('apps.codeReviewSage.reviewFix.target_repository_placeholder')}
          disabled={create.isPending}
          className="font-mono text-[12.5px]"
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <ReviewModelPicker value={model} onChange={onModelChange} disabled={create.isPending} />
        <span className="text-[11.5px] text-muted">
          {i18nT('apps.codeReviewSage.reviewFix.model_pinned_before_execution')}
        </span>
        <span className="flex-1" />
        <SendBtn
          onClick={() => create.mutate()}
          disabled={create.isPending || !targetPath.trim() || findings.length === 0}
        >
          <Wrench className="lucide-inline" />
          {create.isPending
            ? i18nT('apps.codeReviewSage.reviewFix.creating_task')
            : i18nT('apps.codeReviewSage.reviewFix.create_fix_task')}
        </SendBtn>
      </div>
      {/* askAgent stays off: the form still holds the unsaved draft (target
          path + finding selection), and the hand-off would navigate away and
          destroy it — ErrorNotice's documented opt-out case. */}
      <ErrorNotice
        message={create.error ? errorCopy(create.error) : null}
        testId="review-fix-setup-error"
      />
    </section>
  )
}
