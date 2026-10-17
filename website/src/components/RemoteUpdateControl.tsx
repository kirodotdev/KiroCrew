/**
 * "Update" for a remote crew that runs an older version: one click arms it, a
 * second (the confirmation, naming the crew) starts it.
 *
 * The click calls `POST /api/instances/{id}/update`, which runs the crew's own
 * `kirocrew update`, then its restart, then reads the crew's version back, as
 * a background job; this control polls `GET` on the same path and shows the
 * job's phase and its outcome: a new version, the same version (the crew's
 * updater installed nothing), no answer yet, or the remote's redacted error,
 * each with what to do next. Nothing runs without the confirming click, and
 * the gateway refuses a second job while one runs.
 *
 * Every instance of this control for the same crew (its Remote Crew row, the
 * post-update offer) reads the same job query, so a job started in one place
 * shows its progress in the other.
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDownCircle, CheckCircle, Info, RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import type { RemoteUpdateJob } from '../types'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { INSTANCE_VERSIONS_KEY, useInstanceVersions } from '../hooks/useInstanceVersions'
import { errMessage } from '../utils/thunkError'
import { i18nT } from '../i18n/t'

/** How often a running job is re-read. An update takes a minute or more, so a
 *  few seconds of lag on the phase costs nothing. */
const JOB_POLL_MS = 3000

/** How often the versions are re-read after an unverified outcome, until the
 *  crew answers with a version other than the one it had before. */
const UNVERIFIED_POLL_MS = 15_000

/** Every state of the control shares this root. It never grows past its parent,
 *  and its text may break inside a word, because a crew name is free text: an
 *  unbroken 128-character name would otherwise widen the confirm row past a
 *  phone screen and push Cancel out of view. */
const ROOT = 'inline-flex flex-col gap-1.5 items-start min-w-0 max-w-full [overflow-wrap:anywhere]'

export const remoteUpdateJobKey = (instanceId: string) => ['instances', 'update-job', instanceId] as const

/** The job query every control and row for one crew shares. */
export function useRemoteUpdateJob(instanceId: string, enabled = true) {
  return useQuery({
    queryKey: remoteUpdateJobKey(instanceId),
    queryFn: () => api.remoteUpdateStatus(instanceId),
    enabled,
    retry: false,
    refetchInterval: q => (q.state.data?.job?.state === 'running' ? JOB_POLL_MS : false),
  })
}

/** A job that ended with the crew back up (whatever version it came back on). */
export function isSettledOutcome(job: RemoteUpdateJob | null | undefined): boolean {
  return job?.state === 'succeeded' || job?.state === 'unchanged' || job?.state === 'unverified'
}

function progressLabel(job: RemoteUpdateJob | null, name: string): string {
  if (job?.phase === 'restarting') return i18nT('components.remoteUpdate.restarting', { name })
  if (job?.phase === 'verifying') return i18nT('components.remoteUpdate.verifying', { name })
  return i18nT('components.remoteUpdate.updating', { name })
}

export default function RemoteUpdateControl({ instanceId, name }: { instanceId: string; name: string }) {
  const queryClient = useQueryClient()
  const [armed, setArmed] = useState(false)
  const jobQ = useRemoteUpdateJob(instanceId)
  const start = useMutation({
    mutationFn: () => api.updateRemoteInstance(instanceId),
    onSuccess: r => queryClient.setQueryData(remoteUpdateJobKey(instanceId), { job: r.job }),
    // A refusal can mean a job already runs (started from the other surface):
    // re-read it, so its progress replaces the refusal.
    onError: () => void queryClient.invalidateQueries({ queryKey: remoteUpdateJobKey(instanceId), exact: true }),
    onSettled: () => setArmed(false),
  })
  const job = jobQ.data?.job ?? null
  const settled = isSettledOutcome(job)
  // Once a job runs, a refusal shown before it no longer describes anything.
  const { reset: resetStart } = start
  const jobRunning = job?.state === 'running'
  useEffect(() => {
    if (jobRunning) resetStart()
  }, [jobRunning, resetStart])
  // An unverified outcome is settled by the crew itself once it reconnects:
  // keep re-reading the versions until it answers with a different one.
  const unverified = job?.state === 'unverified'
  const before = job?.version_before ?? ''
  const versionsQ = useInstanceVersions(unverified, data => {
    const now = data?.instances.find(r => r.id === instanceId)?.version
    return now && now !== before ? false : UNVERIFIED_POLL_MS
  })
  const versionNow = versionsQ.data?.instances.find(r => r.id === instanceId)?.version ?? ''
  const confirmedLater = unverified && !!versionNow && versionNow !== before
  // A crew that is back up may report a new version, so the Behind badge
  // re-reads rather than waiting out its stale time.
  useEffect(() => {
    if (settled) void queryClient.invalidateQueries({ queryKey: INSTANCE_VERSIONS_KEY, exact: true })
  }, [settled, queryClient])

  // A failed status read is shown beside whatever the cached job says, so a
  // stale progress or outcome never stands in for a read that did not happen.
  const statusNotice = jobQ.error ? (
    <>
      {/* No hand-off: this control sits on the Remote Crew panel, whose add
          and edit crew forms hold unsaved values, and in the post-update
          offer, which floats over whatever page is open. */}
      <ErrorNotice
        variant="inline"
        className="max-w-[420px]"
        message={i18nT('components.remoteUpdate.status_read_failed', { name, error: errMessage(jobQ.error) })}
      />
    </>
  ) : null

  if (start.isPending || job?.state === 'running') {
    return (
      <span className={ROOT} data-testid="remote-update-control">
        <Btn disabled aria-live="polite" className="max-w-full text-left">
          <RefreshCw className="lucide-inline animate-spin shrink-0" aria-hidden />{' '}
          <span className="min-w-0">{progressLabel(job, name)}</span>
        </Btn>
        {statusNotice}
      </span>
    )
  }
  if (job?.state === 'succeeded' || confirmedLater) {
    const version = confirmedLater ? versionNow : job?.version_after
    return (
      <span className={ROOT} data-testid="remote-update-control">
        <span role="status" className="inline-flex items-start gap-1.5 text-[12px] text-ok max-w-full">
          <CheckCircle className="lucide-inline shrink-0 mt-0.5" aria-hidden />{' '}
          <span className="min-w-0">
            {version
              ? i18nT('components.remoteUpdate.succeeded', { name, version })
              : i18nT('components.remoteUpdate.succeeded_unknown', { name })}
          </span>
        </span>
        {statusNotice}
      </span>
    )
  }
  if (unverified) {
    return (
      <span className={ROOT} data-testid="remote-update-control">
        <span role="status" className="inline-flex items-start gap-1.5 text-[12px] text-muted max-w-[420px]">
          <Info className="lucide-inline shrink-0 mt-0.5" aria-hidden /> {i18nT('components.remoteUpdate.unverified', { name })}
        </span>
        {statusNotice}
      </span>
    )
  }
  const restartFailed = job?.state === 'failed' && job.phase === 'restarting'
  const failure = start.error
    ? errMessage(start.error)
    : jobQ.error
      ? i18nT('components.remoteUpdate.status_read_failed', { name, error: errMessage(jobQ.error) })
      : job?.state === 'failed'
        ? restartFailed
          ? i18nT('components.remoteUpdate.failed_restart', { name, error: job.error || '?' })
          : i18nT('components.remoteUpdate.failed_update', { name, error: job.error || '?' })
        : ''
  // A failure this control could not tie to the remote (a refusal from this
  // gateway, or the status read itself) gets no remote next step.
  const localFailure = !!start.error || !!jobQ.error
  // A finished job that did not land is retried with the same action; the
  // label says so, since the crew already ran it once.
  const again = job?.state === 'failed' || job?.state === 'unchanged'
  return (
    <span className={ROOT} data-testid="remote-update-control">
      {job?.state === 'unchanged' && (
        <span role="status" className="inline-flex items-start gap-1.5 text-[12px] text-muted max-w-[420px]">
          <Info className="lucide-inline shrink-0 mt-0.5" aria-hidden />
          {i18nT('components.remoteUpdate.unchanged', { name, version: job.version_after || job.version_before })}
        </span>
      )}
      {failure && (
        <>
          {/* No hand-off: this control sits on the Remote Crew panel, whose add
              and edit crew forms hold unsaved values, and in the post-update
              offer, which floats over whatever page is open, including a chat
              composer with an unsent draft. A hand-off navigates away from both. */}
          <ErrorNotice variant="inline" className="max-w-[420px]" message={failure} />
          {/* A refusal from this gateway (chained, headless, already
              running) or a failed status read says what it is itself; the
              next step is for a remote that ran and failed. */}
          {!localFailure && (
            <span className="text-[12px] text-muted max-w-[420px]">
              {restartFailed
                ? i18nT('components.remoteUpdate.failed_restart_next_step', {
                  name,
                  // The CLI command, from the catalog entry the instances panel shows it with.
                  command: i18nT('pages.settings.instancesPanel.kirocrew_restart'),
                })
                : i18nT('components.remoteUpdate.failed_next_step', { name })}
            </span>
          )}
        </>
      )}
      {armed ? (
        <span className="flex flex-col gap-1 min-w-0 max-w-full">
          <span className="text-[12px] text-muted">{i18nT('components.remoteUpdate.confirm_hint', { name })}</span>
          <span data-testid="remote-update-confirm-row" className="flex flex-wrap items-center gap-2 min-w-0 max-w-full">
            <Btn primary className="max-w-full text-left" onClick={() => start.mutate()}>
              <ArrowDownCircle className="lucide-inline shrink-0" aria-hidden />{' '}
              <span className="min-w-0">{i18nT('components.remoteUpdate.confirm', { name })}</span>
            </Btn>
            <Btn onClick={() => setArmed(false)}>{i18nT('components.remoteUpdate.cancel')}</Btn>
          </span>
        </span>
      ) : (
        <Btn onClick={() => setArmed(true)} aria-label={i18nT('components.remoteUpdate.update_aria', { name })}>
          <ArrowDownCircle className="lucide-inline" aria-hidden />{' '}
          {again ? i18nT('components.remoteUpdate.try_again') : i18nT('components.remoteUpdate.update')}
        </Btn>
      )}
    </span>
  )
}
