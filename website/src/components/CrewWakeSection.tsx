/**
 * The "what wakes this crew" section of the crew editor.
 *
 * Distinct from the Routing section's `triggers` field directly above it: that
 * field decides when the orchestrator PICKS this crew for a task a human already
 * started, while everything listed here starts a turn with no human present.
 * Users conflate the two, so the section carries a one-line disambiguator.
 *
 * Only clock triggers are listed. Webhook tokens carry their own crew binding
 * and are the webhook pane's answer (CrewWebhookSection), not a second row kind
 * here; a dashboard nudge loop is keyed by slot, not by crew, so listing it
 * would still be inventing an attribution the backend cannot answer.
 */
import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Clock, Pause, Play, Zap, ExternalLink, AlarmClockOff, TriangleAlert } from 'lucide-react'
import { api } from '../api/client'
import { Badge, Btn, IconButton, Skeleton } from './ui'
import { timeAgo } from '../utils/timeAgo'
import { fmtRelative } from '../i18n/format'
import type { CronJob } from '../types'
import { useCronActions } from '../hooks/useCronActions'
import { wakesCrew, crewWakeQueryKey } from './crew/wakesCrew'

import { i18nT } from '../i18n/t'

function WakeRow({ job, onChanged }: { job: CronJob; onChanged: () => void }) {
  const { running, runNow, toggleEnabled, actionError } = useCronActions(onChanged)
  const isRunning = running.has(job.id) || !!job.is_running

  const last = job.last_run_ts ? timeAgo(job.last_run_ts) : null
  const next = job.enabled && job.next_run_ts ? fmtRelative(job.next_run_ts) : null
  const pauseLabel = job.enabled
    ? i18nT('components.crewWakeSection.pause_named', { name: job.name })
    : i18nT('components.crewWakeSection.resume_named', { name: job.name })
  const runLabel = i18nT('components.crewWakeSection.run_named_now', { name: job.name })
  // A paused job cannot be run, matching the Schedule page. Its own copy says
  // why, so the disabled control is not silent about the reason.
  const runTitle = job.enabled ? runLabel : i18nT('pages.schedulePage.resume_to_run')
  const rowError = actionError?.id === job.id ? actionError.msg : ''

  return (
    <div className="border-t border-border py-2 first:border-t-0" data-testid="wake-row">
      {/* Narrow-first: a 320px dialog cannot fit the badges, the schedule and two
          controls on one line, and the editor clips rather than scrolls. The two
          wrappers become `display: contents` at `sm`, so the wide layout is the
          same single flex row it was without them. */}
      <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-3">
        <div className="flex w-full items-center gap-2 sm:contents">
          <Badge variant="muted" className="shrink-0 font-mono">
            <Clock className="lucide-inline" aria-hidden="true" />
            {i18nT('components.crewWakeSection.schedule')}
          </Badge>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[12.5px] text-text-strong">{job.name}</div>
            {(last || next) && (
              <div className="text-[10.5px] text-muted">
                {[last, next].filter(Boolean).join(' · ')}
              </div>
            )}
          </div>
        </div>
        <div className="flex w-full items-center gap-2 sm:contents">
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted sm:w-24 sm:flex-none" title={job.schedule}>
            {job.schedule}
          </span>
          <Badge variant={isRunning ? 'aim' : job.enabled ? 'ok' : 'muted'} className="shrink-0">
            {isRunning
              ? i18nT('components.crewWakeSection.running')
              : job.enabled
                ? i18nT('components.crewWakeSection.active')
                : i18nT('components.crewWakeSection.paused')}
          </Badge>
          <div className="flex shrink-0 gap-1">
            <IconButton
              aria-label={pauseLabel}
              title={pauseLabel}
              onClick={() => toggleEnabled(job.id, !job.enabled)}
            >
              {job.enabled
                ? <Pause className="lucide-inline" aria-hidden="true" />
                : <Play className="lucide-inline" aria-hidden="true" />}
            </IconButton>
            <IconButton
              aria-label={runLabel}
              title={runTitle}
              disabled={!job.enabled || isRunning}
              onClick={() => runNow(job.id)}
            >
              <Zap className="lucide-inline" aria-hidden="true" />
            </IconButton>
          </div>
        </div>
      </div>
      {rowError && (
        <div className="mt-1 pl-1 text-[11px] text-danger" role="alert">{rowError}</div>
      )}
    </div>
  )
}

export default function CrewWakeSection({ crew, isDefaultCrew }: { crew: string; isDefaultCrew: boolean }) {
  const navigate = useNavigate()
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: crewWakeQueryKey(crew),
    queryFn: () => api.crons(),
  })
  const jobs: CronJob[] = (data?.jobs || []).filter((j: CronJob) => wakesCrew(j, crew, isDefaultCrew))
  const onChanged = useCallback(() => { void refetch() }, [refetch])

  // A failed fetch leaves `jobs` empty, which would otherwise render the
  // affirmative "nothing wakes this crew" — a false statement about the crew
  // rather than a report about the request. Absence of an answer and an answer
  // of "none" are different things and must not render the same.
  const body = isLoading
    ? <Skeleton className="h-12" />
    : isError
      ? (
        <div className="flex items-center gap-2 rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted" role="alert">
          <TriangleAlert className="lucide-inline shrink-0" aria-hidden="true" />
          {i18nT('components.crewWakeSection.could_not_load_this_crew_s_schedules_so_what_wak')}
        </div>
      )
      : jobs.length === 0
        ? (
          <div className="flex items-center gap-2 rounded-md border border-border bg-bg-accent px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
            <AlarmClockOff className="lucide-inline shrink-0" aria-hidden="true" />
            {i18nT('components.crewWakeSection.no_schedules_run_this_crew_automatically')}
          </div>
        )
        : <div>{jobs.map(j => <WakeRow key={j.id} job={j} onChanged={onChanged} />)}</div>

  return (
    <section className="flex flex-col gap-3" data-testid="crew-wake-section">
      <div className="flex items-center gap-2">
        <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{i18nT('components.crewWakeSection.what_wakes_this_crew')}</h3>
        <Btn className="ml-auto" onClick={() => navigate('/schedule')}>
          <ExternalLink className="lucide-inline" aria-hidden="true" />
          {i18nT('components.crewWakeSection.open_schedule')}
        </Btn>
      </div>
      <p className="m-0 text-[11.5px] leading-relaxed text-muted">{i18nT('components.crewWakeSection.schedules_that_run_this_crew_without_you_asking')}</p>
      {body}
    </section>
  )
}
