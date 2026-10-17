/**
 * Your crew as a container task: the cloud panel's view for a launch on the
 * Fargate lane. A view of its own, parallel to the EC2 view in
 * DeployMyCrew.tsx, and deliberately not a branch inside it.
 *
 * WHY TWO VIEWS. The two lanes answer "is my crew still up" from different
 * sources. An EC2 launch registers its machine, so the Instances registry
 * (which a Settings teardown updates) is the truth for it, and the EC2 view
 * reads that. A Fargate launch registers nothing: its task serves a turn API
 * and no dashboard, so there is no token to mint and nothing for the registry
 * to hold, and the only source that can say whether the task still runs is
 * ECS itself. This view reads ECS through `GET /api/cloud/launch/{id}/task`
 * (one describe-tasks for exactly the ARN the launch recorded) and says what
 * THAT read said, and nothing the read did not say:
 *
 *   starting   the launch is still moving                 -> wait; the launch in Settings
 *   running    ECS reports RUNNING (or on the way up)      -> the task in the ECS console
 *   stopping   ECS reports it on the way down              -> wait
 *   stopped    ECS reports STOPPED, with ECS's own reason  -> Open Remote Crew in Settings
 *   missing    ECS no longer lists the task                -> Open Remote Crew in Settings
 *   unknown    the read did not complete                   -> Try again
 *   failed     the launch itself did not finish            -> Open Remote Crew in Settings
 *
 * A STATUS IS A READING AT AN INSTANT. Every ECS-derived sentence carries the
 * time of the read ("As of 16:32") and a Check again. A task on its way up or
 * down is re-read every few seconds until it settles; a settled one is not
 * polled. Nothing is asserted from the launch record alone: "running" is said
 * only on a RUNNING that ECS returned, "stopped" only on a STOPPED, "no longer
 * listed" only when ECS returned no task for the ARN, and a read that fails
 * says it could not read, never a state.
 *
 * WHAT IS DELIBERATELY ABSENT. No Session Manager target: a task is not a
 * managed instance, so the EC2 view's address row has no counterpart here. No
 * sign-in step: this lane's sign-in completes when the task starts, from a
 * secret. No lifetime countdown: the lane's TTL is enforced by a sweep that
 * runs at the next launch, so "stops at 18:00" is not a fact this page can
 * attest. No stop button: this panel never writes, on either lane.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, Copy, ExternalLink } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api, type LaunchJob, type LaunchTaskReport } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { fmtTime } from '../../i18n/format'
import { copyToClipboard } from '../../utils/clipboard'
import { launchIsInFlight } from '../../utils/remoteCrew'
import { AWS_REGION_RE, Stat, UNKNOWN, deployAge, deployProgress, deployStepLabel } from './deployShared'

/** How often a task on its way up or down is re-read. A settled task (running
 *  or stopped) is not polled: it changes only when something is done to it,
 *  and the reader has Check again for that moment. */
const TASK_POLL_MS = 4000

/** The query key for one launch's task read, under the launch-list key so an
 *  invalidation of the list can take the task reads with it. */
export const taskQueryKey = (jobId: string) => ['cloud', 'launches', jobId, 'task'] as const

/** What ECS said about the task, in the panel's words. */
export type TaskPhase = 'running' | 'starting' | 'stopping' | 'stopped' | 'missing' | 'other'

/**
 * ECS's lifecycle word for the task, folded to what the panel says. ECS's own
 * vocabulary (PROVISIONING, PENDING, ACTIVATING, RUNNING, DEACTIVATING,
 * STOPPING, DEPROVISIONING, STOPPED) is kept as the input; a word this panel
 * does not know is `other`, shown verbatim rather than rounded to the nearest
 * state it might mean. A task whose desiredStatus is STOPPED but whose
 * lastStatus has not caught up is `stopping`. No task in the report is
 * `missing`: ECS returned nothing for the ARN.
 */
export function taskPhase(report: Pick<LaunchTaskReport, 'task'>): TaskPhase {
  const task = report.task
  if (!task) return 'missing'
  const last = task.last_status.toUpperCase()
  // A stop ECS has accepted shows first in desiredStatus; lastStatus follows
  // seconds to a minute later. A task still RUNNING or starting with a STOPPED
  // desire is on its way down, and saying so keeps "running" from being the
  // last word a reader sees before "stopped".
  if (task.desired_status.toUpperCase() === 'STOPPED' && last !== 'STOPPED' && last !== 'DELETED') {
    return 'stopping'
  }
  switch (last) {
    case 'RUNNING':
      return 'running'
    case 'PROVISIONING':
    case 'PENDING':
    case 'ACTIVATING':
      return 'starting'
    case 'DEACTIVATING':
    case 'STOPPING':
    case 'DEPROVISIONING':
      return 'stopping'
    case 'STOPPED':
    case 'DELETED':
      return 'stopped'
    default:
      return 'other'
  }
}

/** Whether a phase is still moving, and so worth re-reading without being asked. */
export function taskPhaseIsTransitional(phase: TaskPhase): boolean {
  return phase === 'starting' || phase === 'stopping'
}

/**
 * The ECS console page for one task, or undefined when any part is missing or
 * the region is not a region id (it becomes a hostname). Built through the URL
 * API from one whole-URL literal; the cluster and task id are path segments
 * and are encoded as such.
 */
export function ecsTaskConsoleUrl(
  region: string | undefined,
  cluster: string | undefined,
  taskId: string | undefined,
): string | undefined {
  if (!region || !AWS_REGION_RE.test(region) || !cluster || !taskId) return undefined
  const url = new URL('https://console.aws.amazon.com/ecs/v2/clusters/')
  url.hostname = [region, url.hostname].join('.')
  url.pathname = `/ecs/v2/clusters/${encodeURIComponent(cluster)}/tasks/${encodeURIComponent(taskId)}`
  url.searchParams.set('region', region)
  return url.toString()
}

/**
 * The task's identity as ECS names it: cluster and task id, with the full ARN
 * one Copy away. The id stays selectable text so a reader whose clipboard is
 * denied can still read it, and a failed copy says so through the shared error
 * surface rather than leaving the button silent.
 */
function TaskRow({
  cluster,
  taskId,
  arn,
  onHandoff,
}: {
  cluster: string
  taskId: string
  arn: string
  onHandoff: () => void
}) {
  const { t } = useTranslation()
  const [done, setDone] = useState(false)
  const [failed, setFailed] = useState(false)
  return (
    <div className="flex flex-col gap-1.5" data-testid="deploy-task-row">
      <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px]">
        <span className="text-muted">{t('pages.membersPage.deploy_task_cluster')}</span>
        <code className="min-w-0 break-all text-text" data-testid="deploy-task-cluster">{cluster || UNKNOWN}</code>
        <span className="text-muted">{t('pages.membersPage.deploy_task_id')}</span>
        <code className="min-w-0 break-all text-text" data-testid="deploy-task-id">{taskId || UNKNOWN}</code>
      </div>
      <div className="flex items-center gap-2">
        <code
          className="flex-1 min-w-0 break-all text-[11px] text-muted bg-bg-hover rounded px-2 py-1"
          data-testid="deploy-task-arn"
        >
          {arn}
        </code>
        <Btn
          onClick={() => {
            // The guarded helper, not `navigator.clipboard`: it falls back to
            // `execCommand` on plain HTTP and never rejects, so the boolean is
            // the whole outcome and the tick is gated on it.
            void copyToClipboard(arn).then((ok) => {
              setDone(ok)
              setFailed(!ok)
            })
          }}
          data-testid="deploy-task-copy"
        >
          {done ? <Check size={14} /> : <Copy size={14} />}
          {done ? t('pages.membersPage.deploy_copied') : t('pages.membersPage.deploy_copy')}
        </Btn>
      </div>
      {failed && (
        <ErrorNotice
          variant="inline"
          message={t('pages.membersPage.deploy_copy_failed')}
          askAgent
          onHandoff={onHandoff}
          testId="deploy-task-copy-error"
        />
      )}
    </div>
  )
}

/**
 * "As of 16:32 · Check again": the instant the status was read, and the way to
 * read it again. Every ECS-derived sentence sits above one of these, because a
 * status without its instant reads as a standing fact.
 */
function ReadAt({
  readAt,
  onCheck,
  checking,
}: {
  readAt: number
  onCheck: () => void
  checking: boolean
}) {
  const { t } = useTranslation()
  return (
    <div className="text-[11.5px] text-muted flex items-center justify-center gap-2" data-testid="deploy-task-read-at">
      <span>{t('pages.membersPage.deploy_task_read_at', { time: fmtTime(readAt * 1000) })}</span>
      <span aria-hidden="true">{'\u00b7'}</span>
      <button
        type="button"
        onClick={onCheck}
        disabled={checking}
        className="bg-transparent border-0 p-0 text-[11.5px] text-accent hover:underline cursor-pointer disabled:opacity-60"
        data-testid="deploy-task-check"
      >
        {t('pages.membersPage.deploy_task_check_again')}
      </button>
    </div>
  )
}

/**
 * The view. `job` is the newest launch and is on the Fargate lane (the dialog
 * decides that); `open` gates the task read the same way the dialog gates the
 * launch read, so a closed panel reads nothing.
 */
export default function FargateCrewView({
  job,
  open,
  onClose,
  goToSettings,
}: {
  job: LaunchJob
  open: boolean
  onClose: () => void
  goToSettings: () => void
}) {
  const { t } = useTranslation()
  // Only a finished launch has a task to read; a moving or failed one is
  // described from the record, which is all there is.
  const readable = job.status === 'done' && !!job.instance_id
  const q = useQuery({
    queryKey: taskQueryKey(job.id),
    queryFn: () => api.cloudLaunchTask(job.id),
    enabled: open && readable,
    // One retry, not the default three with backoff: a reader waiting on
    // "could not read" should see it in seconds, and Try again is theirs.
    retry: 1,
    refetchInterval: (query) => {
      const data = query.state.data
      return data && taskPhaseIsTransitional(taskPhase(data)) ? TASK_POLL_MS : false
    },
  })
  const region = job.region || UNKNOWN

  if (!readable && launchIsInFlight(job.status)) {
    return (
      <div className="flex flex-col items-center gap-1 text-center" data-testid="deploy-task-state-starting">
        <p className="m-0 text-[14px] font-medium">{t('pages.membersPage.deploy_task_state_starting')}</p>
        {/* "Step 2 of 4" says how far; the step's own name says what step 2
            is, in the gateway's wording. This lane's sign-in step completes at
            once (the task starts from a secret), so the counter moves through
            it without waiting on anyone. */}
        <div className="text-[12px] text-text" data-testid="deploy-progress">
          {t('pages.membersPage.deploy_step_progress', deployProgress(job))}
          {deployStepLabel(job) && (
            <span className="text-muted" data-testid="deploy-step-label">
              {' \u00b7 '}
              {deployStepLabel(job)}
            </span>
          )}
        </div>
        <div className="text-[11.5px] text-muted" data-testid="deploy-close-hint">
          {t('pages.membersPage.deploy_close_hint')}
        </div>
        {/* The launch runs in the gateway; the Settings page lists it with
            its Cancel. A door, not a button, so the state still reads as
            "wait". */}
        <button
          type="button"
          onClick={goToSettings}
          className="bg-transparent border-0 p-0 text-[11.5px] text-accent hover:underline cursor-pointer"
          data-testid="deploy-action-manage"
        >
          {t('pages.membersPage.deploy_action_manage')}
        </button>
      </div>
    )
  }

  if (!readable) {
    // Failed or cancelled before a task was recorded (or `done` with no ARN,
    // which no gateway writes): the gateway's own sentence, and the way back
    // to the set-up flow.
    return (
      <div className="flex flex-col items-center gap-3 text-center" data-testid="deploy-task-state-failed">
        <p className="m-0 text-[14px] font-medium">{t('pages.membersPage.deploy_task_state_failed')}</p>
        {job.error && (
          <ErrorNotice
            message={job.error}
            askAgent
            onHandoff={onClose}
            messageClassName="font-mono text-left"
            className="w-full"
            testId="deploy-launch-error"
          />
        )}
        <Btn primary onClick={goToSettings} data-testid="deploy-action-deploy">
          {t('pages.membersPage.deploy_action_deploy')}
        </Btn>
        <div className="text-[11.5px] text-muted" data-testid="deploy-action-hint">
          {t('pages.membersPage.deploy_action_hint')}
        </div>
      </div>
    )
  }

  if (q.isPending) {
    return (
      <div className="text-[12px] text-muted text-center" data-testid="deploy-task-loading">
        {t('pages.membersPage.deploy_task_loading')}
      </div>
    )
  }

  if (q.isError) {
    // The read did not complete. Said as exactly that: not "stopped", not
    // "gone", not "running". The gateway's error names the cause (an AWS
    // refusal, a lane no longer configured here, a Windows host) and the
    // agent can take it from there.
    const message = q.error instanceof Error && q.error.message ? q.error.message : String(q.error)
    return (
      <div className="flex flex-col items-center gap-2 text-center" data-testid="deploy-task-state-unknown">
        <p className="m-0 text-[14px] font-medium">{t('pages.membersPage.deploy_task_state_unknown')}</p>
        <ErrorNotice
          message={message}
          askAgent
          onHandoff={onClose}
          messageClassName="font-mono text-left"
          className="w-full"
          testId="deploy-task-error"
        />
        <Btn onClick={() => { void q.refetch() }} data-testid="deploy-task-retry">
          {t('pages.membersPage.deploy_retry')}
        </Btn>
      </div>
    )
  }

  const report = q.data
  const phase = taskPhase(report)
  const task = report.task
  const consoleUrl = task ? ecsTaskConsoleUrl(job.region, task.cluster, task.task_id) : undefined
  const vars = { region, status: task?.last_status || UNKNOWN }
  const headline = (() => {
    switch (phase) {
      case 'running':
        return t('pages.membersPage.deploy_task_state_running', vars)
      case 'starting':
        return t('pages.membersPage.deploy_task_state_task_starting', vars)
      case 'stopping':
        return t('pages.membersPage.deploy_task_state_stopping', vars)
      case 'stopped':
        return t('pages.membersPage.deploy_task_state_stopped', vars)
      case 'missing':
        return t('pages.membersPage.deploy_task_state_missing', vars)
      default:
        return t('pages.membersPage.deploy_task_state_other', vars)
    }
  })()

  return (
    <div className="flex flex-col gap-3" data-testid={`deploy-task-state-${phase}`}>
      <p className="m-0 text-[14px] font-medium text-center">{headline}</p>

      {/* ECS's own sentence for a stop, verbatim: the only source that knows
          why, in the words it chose. Absent when ECS gave none. */}
      {phase === 'stopped' && task && (
        <div className="text-[12px] text-center text-text" data-testid="deploy-task-stopped">
          {task.stopped_at != null && (
            <div>{t('pages.membersPage.deploy_task_stopped_when', { age: deployAge(task.stopped_at) })}</div>
          )}
          {task.stopped_reason && (
            <div className="font-mono text-[11px] text-muted break-words" data-testid="deploy-task-stopped-reason">
              {task.stopped_reason}
            </div>
          )}
        </div>
      )}

      {/* Honest counters only: both come off the launch record, the same two
          the EC2 view shows. Not on a stopped or unlisted task: "since deploy"
          beside "has stopped" reads as an uptime it never was. */}
      {(phase === 'running' || phase === 'starting' || phase === 'stopping' || phase === 'other') && (
        <div className="grid grid-cols-2 gap-2" data-testid="deploy-stats">
          <Stat
            value={deployAge(job.created_at)}
            label={t('pages.membersPage.deploy_stat_since')}
            testid="deploy-stat-since"
          />
          <Stat value={job.region || UNKNOWN} label={t('pages.membersPage.deploy_stat_region')} testid="deploy-stat-region" />
        </div>
      )}

      {task && (
        <TaskRow cluster={task.cluster} taskId={task.task_id} arn={task.task_arn} onHandoff={onClose} />
      )}

      <ReadAt
        readAt={report.read_at}
        onCheck={() => { void q.refetch() }}
        checking={q.isFetching}
      />
      {/* Check again stays in every read state, stopped and unlisted included:
          a stopped task changes once more (ECS drops it from the list after
          about an hour), an unlisted one is worth re-verifying, and a reader
          who wants to re-check should not have to close and reopen the panel. */}

      {/* Where the task lives: the ECS console page for exactly this task, in a
          new tab. The glyph and the underline say the link leaves the app. */}
      {consoleUrl && (
        <div className="text-[11.5px] text-center" data-testid="deploy-task-console">
          <a
            href={consoleUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-accent underline"
            data-testid="deploy-task-console-link"
          >
            {t('pages.membersPage.deploy_task_console')}
            <ExternalLink size={11} aria-hidden="true" />
          </a>
        </div>
      )}

      {/* A task that has stopped or that ECS no longer lists leaves one thing
          to do, the same as with no launch at all: the set-up flow. */}
      {(phase === 'stopped' || phase === 'missing') && (
        <div className="flex flex-col items-center gap-3 text-center">
          <Btn primary onClick={goToSettings} data-testid="deploy-action-deploy">
            {t('pages.membersPage.deploy_action_deploy')}
          </Btn>
          <div className="text-[11.5px] text-muted" data-testid="deploy-action-hint">
            {t('pages.membersPage.deploy_action_hint')}
          </div>
        </div>
      )}
    </div>
  )
}
