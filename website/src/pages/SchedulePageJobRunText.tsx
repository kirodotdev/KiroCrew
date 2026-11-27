import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import type { CronJob } from '../types'
import { i18nT } from '../i18n/t'

/** The job dialog's last error or last output. The list sends a preview of a long
 * one; this reads them whole (re-read on each `crons` push), for the list row's run
 * only, never an earlier run's. */
export function JobRunText({ job }: { job: CronJob }) {
  const hasRunText = !!(job.last_result || job.last_error)
  const fullJob = useQuery({ queryKey: ['crons', 'job', job.id], queryFn: () => api.cron(job.id), enabled: hasRunText })
  const runText = hasRunText && fullJob.data && fullJob.data.last_run_ts === job.last_run_ts ? fullJob.data : job
  const lastError = runText.last_error
  const lastResult = runText.last_result

  return (
    <>
      {/* No hand-off: JobForm draft. Only while the list's preview stands in for the whole text. */}
      {hasRunText && fullJob.isError && runText === job && <ErrorNotice message={i18nT('pages.schedulePage.full_run_text_load_failed')} testId="schedule-job-full-text-error" />}
      {/* The run's persisted `last_error` is an error by origin (the job
          FAILED), so it takes the shared surface with the 'Last Error'
          label as its title. `whitespace-pre-wrap` on the notice body
          keeps the log's line structure; `font-mono` keeps it reading as
          output rather than prose.

          NOT gated on `script`: a `command` job fails the same way and
          carries the same `last_error`, and gating the only in-page
          surface on the job's TYPE left that reader with the row's
          `title=` tooltip alone -- invisible on touch, unreadable to a
          screen reader, and no hand-off. The failure decides the
          surface, never the shape of the job that produced it. */}
      {lastError && (
        <>
          {/* No hand-off: JobForm draft */}
          {/* A `last_error` is often a multi-frame Python traceback. The
              cap is viewport-relative (`min(45vh, 420px)`), not a fixed
              200px: a 200px window crushes a traceback to a few lines
              that scroll under the fold, so the one line naming the
              failure is the half the reader loses. 45vh uses the dialog's
              own `max-h-[86vh]` height, the 420px ceiling stops it
              dominating a tall screen, and it still scrolls internally
              for anything longer.

              `shrink-0` is load-bearing: this notice is a direct
              `overflow-y-auto` flex child of the column-flex DialogBody,
              which gives it an automatic `min-height: 0`, so the column
              squeezes it to its content's min-content height (~16px, one
              line) regardless of `max-height`. Measured in a running
              instance: without `shrink-0` the box renders 16px against a
              499px traceback; with it, the full height the cap allows. */}
          <ErrorNotice
            title={i18nT('pages.schedulePage.last_error')}
            message={lastError}
            className="shrink-0 max-h-[min(45vh,420px)] overflow-y-auto font-mono"
            testId="schedule-job-last-error"
          />
        </>
      )}
      {!lastError && lastResult && (
        <div className="flex flex-col gap-1.5">
          <div className="text-[12px] text-muted font-medium">{i18nT('pages.schedulePage.last_output')}</div>
          {/* Same viewport-relative cap as the Last Error notice above
              (#21061): a fixed 200px left most of a long output behind
              the inner scrollbar. */}
          <pre data-testid="schedule-job-last-output" className="text-[12px] font-mono whitespace-pre-wrap break-words rounded border px-2.5 py-2 shrink-0 max-h-[min(45vh,420px)] overflow-y-auto bg-bg-elevated border-border text-text">{lastResult}</pre>
        </div>
      )}
    </>
  )
}
