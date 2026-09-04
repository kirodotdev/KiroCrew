import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Check, ChevronDown, GitBranch, Loader2, RefreshCw, ShieldAlert, Wrench } from 'lucide-react'

import { Badge, Btn, Input, SendBtn } from './ui'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from './ui/dropdown-menu'
import SimpleSelect from './SimpleSelect'
import { useConfirm } from './ConfirmDialog'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'
import type {
  ReviewFixActionRequest,
  ReviewFixGroup,
  ReviewFixGroupState,
  ReviewFixMetadata,
  ReviewFixState,
  ReviewFixTaskResponse,
  ReviewFixValidation,
} from '../types'

export interface ReviewFixTaskTransport {
  status: (taskId: string) => Promise<ReviewFixTaskResponse>
  action: (taskId: string, input: ReviewFixActionRequest) => Promise<ReviewFixTaskResponse>
}

interface Props {
  taskId: string
  transport: ReviewFixTaskTransport
  onReviewAgain?: () => Promise<void> | void
  onOpenTaskRunner?: (taskId: string) => void
}

const ACTIVE_STATES = new Set([
  'planning', 'running', 'rereviewing',
])

const REVIEW_FIX_STATE_LABEL_KEYS = {
  draft: 'apps.codeReviewSage.reviewFix.taskPanel.states.draft',
  planning: 'apps.codeReviewSage.reviewFix.taskPanel.states.planning',
  awaiting_group_confirmation: 'apps.codeReviewSage.reviewFix.taskPanel.states.awaiting_group_confirmation',
  running: 'apps.codeReviewSage.reviewFix.taskPanel.states.running',
  awaiting_validation: 'apps.codeReviewSage.reviewFix.taskPanel.states.awaiting_validation',
  ready_to_apply: 'apps.codeReviewSage.reviewFix.taskPanel.states.ready_to_apply',
  awaiting_commit: 'apps.codeReviewSage.reviewFix.taskPanel.states.awaiting_commit',
  committed: 'apps.codeReviewSage.reviewFix.taskPanel.states.committed',
  awaiting_push: 'apps.codeReviewSage.reviewFix.taskPanel.states.awaiting_push',
  pushed: 'apps.codeReviewSage.reviewFix.taskPanel.states.pushed',
  rereviewing: 'apps.codeReviewSage.reviewFix.taskPanel.states.rereviewing',
  done: 'apps.codeReviewSage.reviewFix.taskPanel.states.done',
  paused: 'apps.codeReviewSage.reviewFix.taskPanel.states.paused',
  failed: 'apps.codeReviewSage.reviewFix.taskPanel.states.failed',
  blocked_model_resolution: 'apps.codeReviewSage.reviewFix.taskPanel.states.blocked_model_resolution',
  blocked_dirty_overlap: 'apps.codeReviewSage.reviewFix.taskPanel.states.blocked_dirty_overlap',
  blocked_validation: 'apps.codeReviewSage.reviewFix.taskPanel.states.blocked_validation',
} as const satisfies Record<ReviewFixState, string>

const REVIEW_FIX_GROUP_STATE_LABEL_KEYS = {
  proposed: 'apps.codeReviewSage.reviewFix.taskPanel.group_states.proposed',
  confirmed: 'apps.codeReviewSage.reviewFix.taskPanel.group_states.confirmed',
  executing: 'apps.codeReviewSage.reviewFix.taskPanel.group_states.executing',
  validating: 'apps.codeReviewSage.reviewFix.taskPanel.group_states.validating',
  ready_to_apply: 'apps.codeReviewSage.reviewFix.taskPanel.group_states.ready_to_apply',
  applied: 'apps.codeReviewSage.reviewFix.taskPanel.group_states.applied',
  committed: 'apps.codeReviewSage.reviewFix.taskPanel.group_states.committed',
} as const satisfies Record<ReviewFixGroupState, string>

function stateLabel(state: string): string {
  return i18nT(REVIEW_FIX_STATE_LABEL_KEYS[state as ReviewFixState] ?? REVIEW_FIX_STATE_LABEL_KEYS.failed)
}

function stateVariant(state: string): 'ok' | 'err' | 'warn' | 'aim' | 'muted' {
  if (state === 'done' || state === 'pushed' || state === 'committed') return 'ok'
  if (state.startsWith('blocked_') || state === 'failed') return 'err'
  if (state === 'running' || state === 'planning' || state === 'rereviewing') return 'aim'
  return 'warn'
}

function groupStateLabel(state: string): string {
  return i18nT(REVIEW_FIX_GROUP_STATE_LABEL_KEYS[state as ReviewFixGroupState] ?? REVIEW_FIX_GROUP_STATE_LABEL_KEYS.proposed)
}

function groupStateVariant(state: string): 'ok' | 'err' | 'warn' | 'aim' | 'muted' {
  if (state === 'ready_to_apply' || state === 'applied' || state === 'committed') return 'ok'
  if (state === 'executing' || state === 'validating') return 'aim'
  return state === 'proposed' ? 'muted' : 'warn'
}

function actionErrorCode(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'code' in error) {
    const code = (error as { code?: unknown }).code
    if (typeof code === 'string') return code
  }
  return ''
}

function actionErrorStatus(error: unknown): number {
  if (typeof error === 'object' && error !== null && 'status' in error) {
    const status = (error as { status?: unknown }).status
    if (typeof status === 'number') return status
  }
  return 0
}

function actionErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'message' in error) {
    const message = (error as { message?: unknown }).message
    if (typeof message === 'string') return message
  }
  return ''
}

function actionErrorCopy(error: unknown): string {
  const code = actionErrorCode(error)
  if (code === 'stale_revision' || code === 'revision_conflict' || code === 'stale_group_revision') {
    return i18nT('apps.codeReviewSage.reviewFix.taskPanel.stale_revision')
  }
  if (code === 'confirmation_required') return i18nT('apps.codeReviewSage.reviewFix.taskPanel.confirmation_required')
  // The transport's own errors (ApiError) keep the backend's prose in `message`
  // and the machine-readable code inside the raw body, not on the error. A
  // mapped code wins; a conflict the map does not know — a push whose preview
  // went stale, a transition the backend refused — still names itself instead
  // of collapsing into the generic action failure.
  if (actionErrorStatus(error) === 409) {
    const message = actionErrorMessage(error).trim()
    if (message) return message
  }
  return i18nT('apps.codeReviewSage.reviewFix.taskPanel.action_failed')
}

function passedKinds(group: ReviewFixGroup): Set<string> {
  return new Set(group.validation_runs.filter((run) => run.passed).map((run) => run.kind))
}

function validationSummary(group: ReviewFixGroup): string {
  const passed = passedKinds(group)
  const test = passed.has('test')
  const build = passed.has('build')
  if (test && build) return i18nT('apps.codeReviewSage.reviewFix.taskPanel.validation_both_passed')
  if (test) return i18nT('apps.codeReviewSage.reviewFix.taskPanel.validation_test_only')
  if (build) return i18nT('apps.codeReviewSage.reviewFix.taskPanel.validation_build_only')
  return i18nT('apps.codeReviewSage.reviewFix.taskPanel.validation_not_passed')
}

function validationRunLabel(run: ReviewFixValidation): string {
  return run.passed
    ? i18nT('apps.codeReviewSage.reviewFix.taskPanel.validation_passed', { kind: run.kind })
    : i18nT('apps.codeReviewSage.reviewFix.taskPanel.validation_failed', { kind: run.kind })
}

function shortSha(value: string): string {
  return value ? value.slice(0, 12) : '—'
}

function targetSummary(): string {
  return i18nT('apps.codeReviewSage.reviewFix.taskPanel.target_current_branch')
}

export default function ReviewFixTaskPanel({
  taskId, transport, onReviewAgain, onOpenTaskRunner,
}: Props) {
  const { confirm, confirmDialog } = useConfirm()
  const query = useQuery({
    queryKey: ['review-fix-task', taskId],
    queryFn: () => transport.status(taskId),
    refetchInterval: (current) => ACTIVE_STATES.has(current.state.data?.state ?? '') ? 2500 : false,
  })
  const task = query.data
  const metadata = task?.review_fix ?? null
  const [groupDraft, setGroupDraft] = useState<Record<string, string>>({})
  const [groupDraftRevision, setGroupDraftRevision] = useState<number | null>(null)
  const [modelChoice, setModelChoice] = useState('')
  const [modelRevision, setModelRevision] = useState<number | null>(null)
  const [validationCommands, setValidationCommands] = useState<Record<string, { test: string; build: string }>>({})
  const [commitMessages, setCommitMessages] = useState<Record<string, string>>({})
  const [reviewAgainPending, setReviewAgainPending] = useState(false)

  useEffect(() => {
    if (!metadata || metadata.revision === groupDraftRevision) return
    const next: Record<string, string> = {}
    for (const group of metadata.groups) {
      for (const key of group.finding_keys) next[key] = group.group_id
    }
    setGroupDraft(next)
    setGroupDraftRevision(metadata.revision)
  }, [metadata, groupDraftRevision])

  useEffect(() => {
    if (!metadata || metadata.revision === modelRevision) return
    setModelChoice(metadata.model.requested_model)
    setModelRevision(metadata.revision)
  }, [metadata, modelRevision])

  const action = useMutation({
    mutationFn: (input: ReviewFixActionRequest) => transport.action(taskId, input),
    onSuccess: () => { void query.refetch() },
  })

  const groupPayload = useMemo(() => {
    if (!metadata) return []
    return metadata.groups
      .map((group) => ({
        group_id: group.group_id,
        finding_keys: metadata.finding_snapshots
          .map((finding) => finding.key)
          .filter((key) => (groupDraft[key] ?? '') === group.group_id),
        hard: group.hard,
      }))
      .filter((group) => group.finding_keys.length > 0)
  }, [groupDraft, metadata])

  const sendAction = (name: string, extras: Partial<ReviewFixActionRequest> = {}) => {
    if (!task || !metadata || action.isPending) return
    action.mutate({
      action: name,
      expected_revision: task.revision,
      target_fingerprint: metadata.target.dirty_fingerprint,
      confirmed: true,
      ...extras,
    })
  }

  const sendGroupAction = (
    name: string,
    group: ReviewFixGroup,
    extras: Partial<ReviewFixActionRequest> = {},
  ) => {
    sendAction(name, {
      group_id: group.group_id,
      expected_group_revision: group.revision,
      ...extras,
    })
  }

  const updateCommand = (groupId: string, kind: 'test' | 'build', value: string) => {
    setValidationCommands((current) => ({
      ...current,
      [groupId]: { test: current[groupId]?.test ?? '', build: current[groupId]?.build ?? '', [kind]: value },
    }))
  }

  const runValidation = (group: ReviewFixGroup) => {
    const commands = validationCommands[group.group_id] ?? { test: '', build: '' }
    const testCommand = commands.test.trim().split(/\s+/).filter(Boolean)
    const buildCommand = commands.build.trim().split(/\s+/).filter(Boolean)
    if (testCommand.length === 0 || buildCommand.length === 0) return
    sendGroupAction('validate_group', group, {
      test_command: testCommand,
      build_command: buildCommand,
    })
  }

  const applyGroup = (group: ReviewFixGroup) => {
    const passed = passedKinds(group)
    if (!passed.has('test') || !passed.has('build')) return
    sendGroupAction('apply_group', group, {
      confirmation_intent: 'apply_review_fix_group',
    })
  }

  const commitGroup = (group: ReviewFixGroup) => {
    const message = (commitMessages[group.group_id] ?? '').trim()
    if (!message) return
    sendGroupAction('commit_group', group, {
      commit_message: message,
      confirmation_intent: 'commit_review_fix_group',
    })
  }

  const reviewAgain = async () => {
    if (!onReviewAgain || !task || !metadata || reviewAgainPending) return
    setReviewAgainPending(true)
    try {
      await onReviewAgain()
      await query.refetch()
    } finally {
      setReviewAgainPending(false)
    }
  }

  // The two irreversible actions ask first. The dialog resolves an arbitrary
  // time later, so the revision sent is the one on screen when the user
  // confirmed — a task that moved since answers 409 and the query refetches.
  const discardCandidate = async () => {
    if (!task || !metadata) return
    const ok = await confirm({
      title: i18nT('apps.codeReviewSage.reviewFix.taskPanel.confirm_discard_title'),
      body: i18nT('apps.codeReviewSage.reviewFix.taskPanel.confirm_discard_body'),
      confirmLabel: i18nT('apps.codeReviewSage.reviewFix.taskPanel.discard_candidate'),
    })
    if (!ok) return
    sendAction('discard_candidate', { confirmation_intent: 'discard_review_fix_candidate' })
  }

  const pushGroup = async (group: ReviewFixGroup) => {
    if (!task || !metadata) return
    const branch = metadata.git.confirmed_branch || metadata.git.proposed_branch || metadata.git.candidate_branch
    const ok = await confirm({
      title: i18nT('apps.codeReviewSage.reviewFix.taskPanel.confirm_push_title', {
        remote: metadata.git.remote,
        branch,
      }),
      body: i18nT('apps.codeReviewSage.reviewFix.taskPanel.confirm_push_body', { remote: metadata.git.remote }),
      confirmLabel: i18nT('apps.codeReviewSage.reviewFix.taskPanel.push'),
    })
    if (!ok) return
    sendGroupAction('push', group, { confirmation_intent: 'push_review_fix_group' })
  }

  if (query.isLoading) {
    return <div className="flex items-center gap-2 py-5 text-muted text-sm"><Loader2 size={15} className="animate-spin" />{i18nT('apps.codeReviewSage.reviewFix.taskPanel.loading')}</div>
  }
  if (query.isError || !task) {
    return <ErrorNotice message={i18nT('apps.codeReviewSage.reviewFix.taskPanel.load_failed')} askAgent testId="review-fix-task-load-error" />
  }
  if (!metadata) {
    return <ErrorNotice message={i18nT('apps.codeReviewSage.reviewFix.taskPanel.missing_metadata')} askAgent testId="review-fix-task-metadata-error" />
  }

  const isGrouping = task.state === 'awaiting_group_confirmation'
  const isValidation = task.state === 'awaiting_validation' || task.state === 'blocked_validation'
  const isApplyPhase = task.state === 'ready_to_apply'
  const isCommitPhase = task.state === 'awaiting_commit'
  const isPushPhase = task.state === 'awaiting_push'
  const canResolveModel = task.state === 'blocked_model_resolution' || !metadata.model.resolved_model_id

  return (
    <section className="rounded-lg border border-border bg-card p-4 flex flex-col gap-4" aria-labelledby="review-fix-task-title">
      <header className="flex flex-wrap items-start gap-3">
        <Wrench size={18} className="lucide-inline text-accent mt-0.5" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="review-fix-task-title" className="text-[14px] font-semibold text-text-strong">
              {i18nT('apps.codeReviewSage.reviewFix.taskPanel.title')}
            </h2>
            <Badge variant={stateVariant(task.state)}>{stateLabel(task.state)}</Badge>
            <span className="font-mono text-[11px] text-muted">{i18nT('apps.codeReviewSage.reviewFix.taskPanel.revision', { revision: task.revision })}</span>
          </div>
          <div className="mt-1 text-[12px] text-muted break-all">{task.task_id}</div>
        </div>
        {onOpenTaskRunner && (
          <Btn type="button" onClick={() => onOpenTaskRunner(task.task_id)}>
            {i18nT('apps.codeReviewSage.reviewFix.taskPanel.open_task_runner')}
          </Btn>
        )}
      </header>

      <div className="grid gap-3 md:grid-cols-2">
        <InfoBlock label={i18nT('apps.codeReviewSage.reviewFix.taskPanel.target')}>
          <div>{targetSummary()}</div>
          <code className="break-all">{metadata.target.target_path}</code>
          <div>{metadata.target.branch_name || metadata.target.target_ref || '—'}</div>
          <div className="font-mono">{shortSha(metadata.target.head_sha)}</div>
        </InfoBlock>
        <InfoBlock label={i18nT('apps.codeReviewSage.reviewFix.taskPanel.candidate')}>
          <code className="break-all">{metadata.git.candidate_worktree_path || '—'}</code>
          <div>{metadata.git.candidate_branch || metadata.git.candidate_ref || '—'}</div>
          <div className="font-mono">{shortSha(metadata.git.candidate_ref)}</div>
        </InfoBlock>
      </div>

      {(task.state === 'blocked_dirty_overlap') && (
        <div className="rounded-md border border-danger/40 bg-danger-subtle px-3 py-2 text-[12.5px] text-danger flex gap-2">
          <ShieldAlert size={15} className="lucide-inline mt-0.5 shrink-0" aria-hidden="true" />
          <div>
            <div className="font-semibold">{stateLabel(task.state)}</div>
            <div className="mt-1">{i18nT('apps.codeReviewSage.reviewFix.taskPanel.blocked_target_description')}</div>
            {metadata.target.tracked_paths.length > 0 && (
              <code className="mt-1 block break-words">{metadata.target.tracked_paths.join(', ')}</code>
            )}
          </div>
        </div>
      )}

      {canResolveModel && (
        <div className="rounded-md border border-warn/40 bg-warn-subtle p-3 flex flex-wrap items-end gap-2">
          <div className="flex min-w-[220px] flex-1 flex-col gap-1 text-[12px] font-semibold text-muted">
            {i18nT('apps.codeReviewSage.reviewFix.taskPanel.model_to_resolve')}
            {metadata.model.advertised_model_ids.length > 0 ? (
              <SimpleSelect
                options={metadata.model.advertised_model_ids}
                value={modelChoice}
                onChange={setModelChoice}
                disabled={action.isPending}
                className="font-mono text-[12px]"
                aria-label={i18nT('apps.codeReviewSage.reviewFix.taskPanel.model_to_resolve')}
              />
            ) : (
              <Input value={modelChoice} onChange={(event) => setModelChoice(event.target.value)} aria-label={i18nT('apps.codeReviewSage.reviewFix.taskPanel.model_to_resolve')} className="font-mono text-[12px]" />
            )}
          </div>
          <SendBtn disabled={action.isPending || !modelChoice.trim()} onClick={() => sendAction('resolve_model', { model: modelChoice.trim() })}>
            {action.isPending ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <RefreshCw size={14} className="lucide-inline" aria-hidden="true" />}
            {i18nT('apps.codeReviewSage.reviewFix.taskPanel.resolve_model')}
          </SendBtn>
        </div>
      )}

      <section className="flex flex-col gap-2" aria-labelledby="review-fix-groups-title">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id="review-fix-groups-title" className="text-[13px] font-semibold text-text-strong">
            {i18nT('apps.codeReviewSage.reviewFix.taskPanel.groups')}
          </h3>
          {metadata.groups.some((group) => group.hard) && (
            <span className="text-[11.5px] text-muted">{i18nT('apps.codeReviewSage.reviewFix.taskPanel.hard_groups_locked')}</span>
          )}
          <span className="flex-1" />
          {isGrouping && (
            <>
              <SendBtn
                disabled={action.isPending || groupPayload.length === 0}
                onClick={() => sendAction('edit_soft_grouping', { groups: groupPayload })}
              >
                {i18nT('apps.codeReviewSage.reviewFix.taskPanel.save_grouping')}
              </SendBtn>
              <Btn type="button" onClick={() => sendAction('confirm_grouping')} disabled={action.isPending}>
                <Check size={13} className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.codeReviewSage.reviewFix.taskPanel.confirm_grouping')}
              </Btn>
            </>
          )}
        </div>
        <div className="flex flex-col gap-2">
          {metadata.groups.map((group) => (
            <GroupCard
              key={group.group_id}
              group={group}
              metadata={metadata}
              isGrouping={isGrouping}
              groupDraft={groupDraft}
              onGroupDraftChange={(key, value) => setGroupDraft((current) => ({ ...current, [key]: value }))}
              validationCommands={validationCommands[group.group_id] ?? { test: '', build: '' }}
              onCommandChange={(kind, value) => updateCommand(group.group_id, kind, value)}
              onValidate={() => runValidation(group)}
              isValidation={isValidation}
              isApplyPhase={isApplyPhase}
              isCommitPhase={isCommitPhase}
              isPushPhase={isPushPhase}
              taskCommitted={task.state === 'committed'}
              commitMessage={commitMessages[group.group_id] ?? ''}
              onCommitMessageChange={(value) => setCommitMessages((current) => ({ ...current, [group.group_id]: value }))}
              onApply={() => applyGroup(group)}
              onCommit={() => commitGroup(group)}
              onPushPreview={() => sendGroupAction('push_preview', group)}
              onPush={() => void pushGroup(group)}
              onCapturePatch={() => sendGroupAction('capture_group_patch', group)}
              disabled={action.isPending}
            />
          ))}
        </div>
      </section>

      <footer className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        {task.state === 'paused' ? (
          <Btn type="button" onClick={() => sendAction('resume')} disabled={action.isPending}>{i18nT('apps.codeReviewSage.reviewFix.taskPanel.resume')}</Btn>
        ) : isGrouping && metadata.groups.length > 0 && metadata.groups.every((group) => group.state === 'confirmed') ? (
          <Btn type="button" onClick={() => sendAction('resume')} disabled={action.isPending}>{i18nT('apps.codeReviewSage.reviewFix.taskPanel.start_execution')}</Btn>
        ) : task.state === 'running' ? (
          <Btn type="button" onClick={() => sendAction('pause')} disabled={action.isPending}>{i18nT('apps.codeReviewSage.reviewFix.taskPanel.pause')}</Btn>
        ) : null}
        {(task.state === 'failed' || task.state.startsWith('blocked_')) && (
          <Btn type="button" onClick={() => sendAction('retry')} disabled={action.isPending}>{i18nT('apps.codeReviewSage.reviewFix.taskPanel.retry')}</Btn>
        )}
        {task.state === 'pushed' && onReviewAgain && (
          <SendBtn disabled={reviewAgainPending || action.isPending} onClick={() => void reviewAgain()}>
            {reviewAgainPending ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <RefreshCw size={14} className="lucide-inline" aria-hidden="true" />}
            {i18nT('apps.codeReviewSage.reviewFix.taskPanel.review_again')}
          </SendBtn>
        )}
        <span className="flex-1" />
        {task.state !== 'done' && task.state !== 'running' && (
          <Btn
            type="button"
            danger
            onClick={() => void discardCandidate()}
            disabled={action.isPending}
          >
            {i18nT('apps.codeReviewSage.reviewFix.taskPanel.discard_candidate')}
          </Btn>
        )}
      </footer>

      <ErrorNotice
        variant="inline"
        message={action.error ? actionErrorCopy(action.error) : null}
        askAgent
        testId="review-fix-action-error"
      />
      {confirmDialog}
    </section>
  )
}

function InfoBlock({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border bg-bg-elevated px-3 py-2 text-[12px] text-muted">
      <div className="mb-1 text-[11.5px] font-semibold uppercase tracking-wide text-muted">{label}</div>
      <div className="flex flex-col gap-0.5">{children}</div>
    </div>
  )
}

interface GroupCardProps {
  group: ReviewFixGroup
  metadata: ReviewFixMetadata
  isGrouping: boolean
  groupDraft: Record<string, string>
  onGroupDraftChange: (key: string, value: string) => void
  validationCommands: { test: string; build: string }
  onCommandChange: (kind: 'test' | 'build', value: string) => void
  onValidate: () => void
  isValidation: boolean
  isApplyPhase: boolean
  isCommitPhase: boolean
  isPushPhase: boolean
  taskCommitted: boolean
  commitMessage: string
  onCommitMessageChange: (value: string) => void
  onApply: () => void
  onCommit: () => void
  onPushPreview: () => void
  onPush: () => void
  onCapturePatch: () => void
  disabled: boolean
}

function GroupCard({
  group, metadata, isGrouping, groupDraft, onGroupDraftChange, validationCommands, onCommandChange,
  onValidate, isValidation, isApplyPhase, isCommitPhase, isPushPhase, taskCommitted, commitMessage,
  onCommitMessageChange, onApply, onCommit, onPushPreview, onPush, onCapturePatch, disabled,
}: GroupCardProps) {
  const [open, setOpen] = useState(true)
  const findingByKey = useMemo(
    () => new Map(metadata.finding_snapshots.map((finding) => [finding.key, finding])),
    [metadata.finding_snapshots],
  )
  const passed = passedKinds(group)
  const canApply = group.state === 'ready_to_apply' && passed.has('test') && passed.has('build')
  const canValidate = isValidation && group.state !== 'committed' && group.state !== 'applied'
  // The group's diff is final the moment the group is committed, so a preview
  // makes sense from there on — during the whole task-committed window (where
  // the action block used to disappear entirely) and once awaiting_push.
  const canPreviewPush = group.state === 'committed' && (taskCommitted || isPushPhase)
  const moreActions: Array<{ key: string; label: string; icon?: JSX.Element; onSelect: () => void }> = []
  // Refresh is a tertiary affordance, so it lives in the overflow menu —
  // except during awaiting_push, where preview + push already fill the row
  // (AUTOSDE max-two-buttons-per-row) and the committed diff is final anyway.
  // Gate on the GROUP's lifecycle, not on hasDiff: capture_group_patch does
  // not set diff_path/patch_path (only apply does), so hasDiff is always
  // false before Apply and the old gate hid the ONLY re-capture entry point
  // in exactly the pre-Apply phases where the backend accepts a re-capture
  // (fix_tasks.py capture-accept matrix: AWAITING_VALIDATION /
  // BLOCKED_VALIDATION). Once applied or committed the captured patch is
  // pinned into artifacts, so refresh is no longer legitimate.
  if (!isPushPhase && group.state !== 'applied' && group.state !== 'committed') {
    moreActions.push({
      key: 'refresh_diff',
      label: i18nT('apps.codeReviewSage.reviewFix.taskPanel.refresh_diff'),
      icon: <GitBranch size={13} className="lucide-inline text-accent" aria-hidden="true" />,
      onSelect: onCapturePatch,
    })
  }
  const groupOptions = metadata.groups.map((item) => item.group_id)
  const groupLabels = metadata.groups.map((item) => item.hard
    ? `${item.group_id} · ${i18nT('apps.codeReviewSage.reviewFix.taskPanel.hard')}`
    : item.group_id)

  return (
    <article className="rounded-md border border-border bg-bg-elevated">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left cursor-pointer"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
      >
        <ChevronDown size={14} className={`lucide-inline transition-transform ${open ? '' : '-rotate-90'}`} aria-hidden="true" />
        <span className="font-mono text-[12px] font-semibold text-text-strong">{group.group_id}</span>
        <Badge variant={groupStateVariant(group.state)}>{groupStateLabel(group.state)}</Badge>
        <span className="text-[11.5px] text-muted">{group.finding_keys.length} {i18nT('apps.codeReviewSage.reviewFix.taskPanel.findings')}</span>
        <span className="flex-1" />
        <span className="font-mono text-[11px] text-muted">r{group.revision}</span>
      </button>
      {open && (
        <div className="border-t border-border px-3 py-3 flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            {group.finding_keys.map((key) => {
              const finding = findingByKey.get(key)
              if (!finding) return null
              return (
                <div key={key} className="flex flex-wrap items-start gap-2 rounded border border-border bg-card px-2 py-1.5">
                  <div className="min-w-0 flex-1">
                    <div className="text-[12px] text-text-strong break-words">{finding.title || key}</div>
                    {finding.file_path && <code className="text-[11px] text-muted break-all">{finding.file_path}{finding.line == null ? '' : `:${finding.line}`}</code>}
                  </div>
                  {isGrouping ? (
                    <SimpleSelect
                      options={groupOptions}
                      optionLabels={groupLabels}
                      value={groupDraft[key] ?? group.group_id}
                      onChange={(value) => onGroupDraftChange(key, value)}
                      disabled={group.hard || disabled}
                      aria-label={i18nT('apps.codeReviewSage.reviewFix.taskPanel.group_for_finding')}
                      className="text-[11.5px]"
                    />
                  ) : (
                    <span className="font-mono text-[11px] text-muted">{group.hard ? i18nT('apps.codeReviewSage.reviewFix.taskPanel.hard') : i18nT('apps.codeReviewSage.reviewFix.taskPanel.soft')}</span>
                  )}
                </div>
              )
            })}
          </div>

          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11.5px] text-muted">
            <span>{i18nT('apps.codeReviewSage.reviewFix.taskPanel.affected_files')}: {group.affected_files.join(', ') || '—'}</span>
            <span>{i18nT('apps.codeReviewSage.reviewFix.taskPanel.validation_status')}: {validationSummary(group)}</span>
          </div>

          {(isValidation || isApplyPhase) && group.state !== 'applied' && group.state !== 'committed' && (
            <div className="grid gap-2 md:grid-cols-2">
              <div className="flex flex-col gap-1 text-[11.5px] font-semibold text-muted">
                {i18nT('apps.codeReviewSage.reviewFix.taskPanel.test_command')}
                <Input value={validationCommands.test} onChange={(event) => onCommandChange('test', event.target.value)} aria-label={i18nT('apps.codeReviewSage.reviewFix.taskPanel.test_command')} placeholder={i18nT('apps.codeReviewSage.reviewFix.taskPanel.command_placeholder')} disabled={!isValidation || disabled} className="font-mono text-[11.5px]" />
              </div>
              <div className="flex flex-col gap-1 text-[11.5px] font-semibold text-muted">
                {i18nT('apps.codeReviewSage.reviewFix.taskPanel.build_command')}
                <Input value={validationCommands.build} onChange={(event) => onCommandChange('build', event.target.value)} aria-label={i18nT('apps.codeReviewSage.reviewFix.taskPanel.build_command')} placeholder={i18nT('apps.codeReviewSage.reviewFix.taskPanel.command_placeholder')} disabled={!isValidation || disabled} className="font-mono text-[11.5px]" />
              </div>
            </div>
          )}

          {group.validation_runs.length > 0 && (
            <div className="flex flex-col gap-1 rounded border border-border bg-card px-2.5 py-2">
              {group.validation_runs.map((run) => (
                <div key={run.validation_id} className="flex items-center gap-2 text-[11.5px]">
                  <Badge variant={run.passed ? 'ok' : 'err'}>{run.passed ? <Check size={11} aria-hidden="true" /> : <ShieldAlert size={11} aria-hidden="true" />}{validationRunLabel(run)}</Badge>
                  <code className="min-w-0 truncate text-muted">{run.command.join(' ')}</code>
                  {run.artifact_path && <code className="ml-auto truncate text-muted">{run.artifact_path}</code>}
                </div>
              ))}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            {canValidate && (
              <SendBtn onClick={onValidate} disabled={disabled || !validationCommands.test.trim() || !validationCommands.build.trim()}>
                <Check size={14} className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.codeReviewSage.reviewFix.taskPanel.run_validation')}
              </SendBtn>
            )}
            {/* Preview is available from the moment the group is committed
                (the diff on disk is final) — not only once the whole task
                reaches awaiting_push, where it used to vanish. Push stays
                push-phase-only: the backend refuses it before an approved
                preview exists. Refresh is a tertiary affordance, so it lives
                in the overflow menu and the row holds at most two buttons
                (AUTOSDE max-two-buttons-per-row). */}
            {canPreviewPush && (
              <>
                <Btn type="button" onClick={onPushPreview} disabled={disabled}>{i18nT('apps.codeReviewSage.reviewFix.taskPanel.push_preview')}</Btn>
                {isPushPhase && (
                  <SendBtn onClick={onPush} disabled={disabled}>{i18nT('apps.codeReviewSage.reviewFix.taskPanel.push')}</SendBtn>
                )}
              </>
            )}
            {moreActions.length > 0 && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Btn type="button" disabled={disabled} aria-label={i18nT('apps.codeReviewSage.reviewFix.taskPanel.more_actions')}>
                    <ChevronDown size={14} aria-hidden="true" />
                  </Btn>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-[180px]">
                  {moreActions.map((item) => (
                    <DropdownMenuItem key={item.key} onSelect={item.onSelect} disabled={disabled}>
                      {item.icon}
                      <span>{item.label}</span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            {isApplyPhase && (
              <SendBtn onClick={onApply} disabled={disabled || !canApply}>
                <Check size={14} className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.codeReviewSage.reviewFix.taskPanel.apply')}
              </SendBtn>
            )}
            {isCommitPhase && group.state === 'applied' && (
              <div className="flex flex-wrap items-end gap-2 w-full">
                <div className="flex min-w-[240px] flex-1 flex-col gap-1 text-[11.5px] font-semibold text-muted">
                  {i18nT('apps.codeReviewSage.reviewFix.taskPanel.commit_message')}
                  <Input value={commitMessage} onChange={(event) => onCommitMessageChange(event.currentTarget.value)} aria-label={i18nT('apps.codeReviewSage.reviewFix.taskPanel.commit_message')} placeholder={i18nT('apps.codeReviewSage.reviewFix.taskPanel.commit_message_placeholder')} disabled={disabled} />
                </div>
                <SendBtn onClick={onCommit} disabled={disabled || !commitMessage.trim()}>{i18nT('apps.codeReviewSage.reviewFix.taskPanel.commit')}</SendBtn>
              </div>
            )}
          </div>
        </div>
      )}
    </article>
  )
}
