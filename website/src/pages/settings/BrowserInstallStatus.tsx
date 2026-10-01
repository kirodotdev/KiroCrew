import { useEffect, useState } from 'react'
import { CheckCircle2, Loader2 } from 'lucide-react'

import type { BrowserEngine, BrowserInstallJob } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtDuration } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import {
  activityEngine,
  activityIsCliSetup,
  elapsedSeconds,
  type InstallActivity,
} from './browserInstallState'

/** Catalog KEY per engine, as full literal keys so the key checks resolve them.
 *  Keys rather than strings: a module-level `i18nT()` would freeze the boot
 *  language. */
export const ENGINE_LABEL_KEY: Record<BrowserEngine, string> = {
  chromium: 'pages.settings.browserPanel.engine_chromium',
  firefox: 'pages.settings.browserPanel.engine_firefox',
  webkit: 'pages.settings.browserPanel.engine_webkit',
}

/** Stage names the gateway reports. A stage this build does not know renders no
 *  step line rather than a raw identifier. */
const STAGE_KEY: Record<string, string> = {
  preparing: 'pages.settings.browserPanel.stage_preparing',
  installing_cli: 'pages.settings.browserPanel.stage_installing_cli',
  downloading_browser: 'pages.settings.browserPanel.stage_downloading_browser',
  installing_skills: 'pages.settings.browserPanel.stage_installing_skills',
  finishing: 'pages.settings.browserPanel.stage_finishing',
}

/** Whole seconds as `1m 5s` / `12s` in the active locale. */
function durationText(seconds: number): string {
  const total = Math.floor(seconds)
  return fmtDuration(
    [
      [Math.floor(total / 60), 'minute'],
      [total % 60, 'second'],
    ],
    { dropZero: true },
  )
}

export const engineLabel = (engine: BrowserEngine): string => i18nT(ENGINE_LABEL_KEY[engine])

/** Headline for an operation that is still running. */
function runningHeadline(activity: InstallActivity): string {
  const engine = activityEngine(activity)
  if (engine) return i18nT('pages.settings.browserPanel.op_engine_download', { engine: engineLabel(engine) })
  if (activityIsCliSetup(activity)) return i18nT('pages.settings.browserPanel.op_cli_setup')
  // Not reason_busy_generic: that string is also every blocked row's reason, and the
  // headline repeating it word for word reads as the same message printed twice.
  return i18nT('pages.settings.browserPanel.op_busy_generic')
}

/** Headline for a finished operation, named by what it was and how it ended. */
function finishedHeadline(job: BrowserInstallJob): string {
  const engine = job.kind === 'engine_download' ? job.engine : null
  if (job.kind === 'cli_setup') {
    if (job.status === 'succeeded') return i18nT('pages.settings.browserPanel.done_cli')
    if (job.status === 'interrupted') return i18nT('pages.settings.browserPanel.interrupted_cli')
    return i18nT('pages.settings.browserPanel.failed_cli')
  }
  if (engine) {
    const name = engineLabel(engine)
    if (job.status === 'succeeded') return i18nT('pages.settings.browserPanel.done_engine', { engine: name })
    if (job.status === 'interrupted') return i18nT('pages.settings.browserPanel.interrupted_engine', { engine: name })
    return i18nT('pages.settings.browserPanel.failed_engine', { engine: name })
  }
  if (job.status === 'succeeded') return i18nT('pages.settings.browserPanel.outcome_succeeded')
  if (job.status === 'interrupted') return i18nT('pages.settings.browserPanel.outcome_interrupted')
  return i18nT('pages.settings.browserPanel.outcome_failed')
}

/**
 * The one place install progress and outcome are shown.
 *
 * Rendered by the panel outside every `installed` branch, so the CLI binary
 * appearing halfway through setup cannot hide the browser and skills stages that
 * are still running. There is no percentage or ETA: the installer reports stages,
 * not bytes, and a number it did not measure would be invented.
 */
export function BrowserInstallStatus({
  activity,
  finished,
  legacyError,
  receivedAt,
  statusError,
  allowHandoff,
}: {
  activity: InstallActivity | null
  finished: BrowserInstallJob | null
  /** `last_error` from a gateway that sends no job, shown when nothing runs. */
  legacyError: string | null
  /** When the status carrying `activity` arrived, to advance `elapsed_s`. */
  receivedAt: number
  /** The latest status read failed, so what is shown may be out of date. */
  statusError: string | null
  /** False while an unsaved token draft is on the panel: the hand-off navigates
   *  away and would discard it. */
  allowHandoff: boolean
}) {
  const running = activity !== null
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) return undefined
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(id)
  }, [running])

  const shownFinished = running ? null : finished
  const showLegacyError = !running && !finished && Boolean(legacyError)
  if (!running && !shownFinished && !showLegacyError && !statusError) return null

  const elapsed = elapsedSeconds(activity, shownFinished, receivedAt, now)
  const stage =
    activity?.source === 'job' ? activity.job.stage : activity?.source === 'pending' ? 'preparing' : null
  const failed = shownFinished && shownFinished.status !== 'succeeded'

  return (
    <section
      aria-label={i18nT('pages.settings.browserPanel.status_region_label')}
      className="border border-border rounded-md px-3 py-2.5 mb-4 flex flex-col gap-1.5"
      data-testid="browser-install-status"
    >
      {statusError && (
        /* No hand-off: the panel may hold an unsaved extension token draft. */
        <ErrorNotice
          variant="inline"
          title={i18nT('pages.settings.browserPanel.status_unavailable')}
          message={statusError}
          messagePlacement="below"
        />
      )}
      {running && (
        <>
          <div role="status" className="flex items-start gap-2">
            <Loader2 size={16} className="text-accent shrink-0 mt-[2px] animate-spin" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium">{runningHeadline(activity)}</div>
              {stage && STAGE_KEY[stage] && (
                <div className="text-[13px] text-muted">
                  {i18nT('pages.settings.browserPanel.stage_line', { stage: i18nT(STAGE_KEY[stage]) })}
                </div>
              )}
            </div>
          </div>
          {/* Outside the live region: a once-a-second tick announced to a screen
              reader would drown out the stage changes that matter. */}
          {elapsed !== null && (
            <div className="text-[13px] text-muted pl-6" data-testid="browser-install-elapsed">
              {i18nT('pages.settings.browserPanel.elapsed_running', { duration: durationText(elapsed) })}
            </div>
          )}
          <div className="text-[13px] text-muted pl-6">
            {i18nT('pages.settings.browserPanel.install_takes_a_while')}
          </div>
        </>
      )}
      {shownFinished && !failed && (
        <div role="status" className="flex items-start gap-2">
          <CheckCircle2 size={16} className="text-ok shrink-0 mt-[2px]" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium">{finishedHeadline(shownFinished)}</div>
            {elapsed !== null && (
              <div className="text-[13px] text-muted">
                {i18nT('pages.settings.browserPanel.elapsed_done', { duration: durationText(elapsed) })}
              </div>
            )}
          </div>
        </div>
      )}
      {shownFinished && failed && (
        /* The detail is the installer's own output, redacted by the gateway: the
           useful failures (a registry login, a blocked download, missing OS
           libraries) are only actionable if the operator can read what it said.
           The hand-off turns an error a user cannot fix alone into a chat, and is
           withheld while a token draft would be lost to the navigation. */
        <ErrorNotice
          title={finishedHeadline(shownFinished)}
          message={shownFinished.error_detail || i18nT('pages.settings.browserPanel.failure_no_detail')}
          messagePlacement="below"
          messageClassName="font-mono whitespace-pre-wrap break-words"
          askAgent={allowHandoff}
        />
      )}
      {showLegacyError && (
        /* Same trade as a job failure: hand-off only when no token draft exists. */
        <ErrorNotice message={legacyError} askAgent={allowHandoff} />
      )}
    </section>
  )
}
