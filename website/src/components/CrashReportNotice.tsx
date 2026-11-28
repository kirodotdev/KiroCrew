/**
 * CrashReportNotice — tells the user their app crashed, and where the evidence is.
 *
 * ## Why a banner and not a settings row
 *
 * The desktop app has captured crash artifacts for a while now (a Crashpad
 * minidump, and on macOS the OS's own `.ips` report) and never once mentioned
 * that either existed. The observed cost of that silence: a main-process crash
 * reaches us as "it closed by itself" days later, with the reporter guessing at
 * a cause while the artifacts sit unread on their disk. A settings row would not
 * have fixed it — nobody opens Settings to check whether they crashed. The
 * notice has to arrive unprompted, on the launch after the crash, which is the
 * one moment the user already knows something went wrong.
 *
 * ## Why it is dismissible and does not come back
 *
 * The banner is a report, not a task. Once seen it has done its job, and a
 * notice that reappears on every navigation is one the user learns to ignore —
 * including the next time, when it matters. Dismissal is per-session and
 * in-memory: main.js scans once per app session, so a new launch with no new
 * crash produces `newCount: 0` and nothing renders anyway.
 *
 * The renderer is told the count and when the newest crash happened, so a
 * weeks-old crash does not read like one that just occurred, plus, on a launch
 * whose Chromium file log is off, a cause code from a closed set (see
 * `initNativeLogging` in electron/native-logging.js) that the banner turns into
 * a translated sentence saying chromium.log is from an earlier session. Paths,
 * file names, module names and exception codes stay in the main process (see
 * `crashNoticeSummary` in electron/crash-collector.js) — the reveal action is a
 * main-process gesture that names no path, so this component never holds one.
 */
import { useEffect, useState } from 'react'
import { AlertTriangle, Bug, FolderOpen, X } from 'lucide-react'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import ReportProblemModal from './ReportProblemModal'

import { i18nT } from '../i18n/t'
import { fmtDateTime, toDate } from '../i18n/format'

/**
 * Bridge from electron/preload.js. Absent in a plain browser and in the PWA,
 * where there is no local disk holding crash artifacts to reveal — the banner
 * simply never renders, rather than offering an action that cannot work.
 */
const crashReportsAPI = (): CrashReportsAPI | undefined => window.crashReportsAPI

/** Waits before each re-ask of a refused `get()`, from the previous refusal. */
const REASK_DELAYS_MS = [15_000, 45_000, 120_000, 300_000, 600_000] as const

export default function CrashReportNotice() {
  const [count, setCount] = useState(0)
  const [lastCrashAt, setLastCrashAt] = useState<Date | null>(null)
  const [nativeLogOff, setNativeLogOff] = useState<NativeLogOffCause | null>(null)
  const [dismissed, setDismissed] = useState(false)
  const [revealError, setRevealError] = useState<string | null>(null)
  const [reportOpen, setReportOpen] = useState(false)

  // The reveal action must not fail into silence. `reveal()` reports its
  // outcome two ways — a rejection, and an explicit `{ ok: false }` (the log was
  // removed since the scan, or `shell.showItemInFolder` threw) — and the old
  // `.catch(() => {})` discarded both, leaving a dead button and a user who was
  // told to attach files they now cannot find. On either failure we surface the
  // fallback path (the log's fixed filename) through the shared ErrorNotice.
  // A rejection here is genuine: the banner only rendered because `get()`
  // resolved, so this IS the local dashboard, not the remote-sender case `get()`
  // deliberately swallows.
  const revealCrashFolder = async () => {
    setRevealError(null)
    const api = crashReportsAPI()
    if (!api) return
    try {
      const result = await api.reveal()
      if (!result?.ok) setRevealError(i18nT('components.crashNotice.reveal_failed'))
    } catch {
      setRevealError(i18nT('components.crashNotice.reveal_failed'))
    }
  }

  useEffect(() => {
    const api = crashReportsAPI()
    if (!api) return
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    // Swallowing the rejection is deliberate: the handler rejects a sender that
    // is not the local dashboard (a connection window pointed at a remote
    // gateway shares this preload), and that is a correct outcome, not an error
    // worth putting on screen. A refused ask runs no scan, and the shell's
    // port-owner check can refuse for minutes after launch, so it is re-asked.
    const ask = (attempt: number): void => void api.get()
      .then(summary => {
        if (!alive) return
        setCount(summary.newCount)
        // An older shell sends no time; a missing or malformed one hides the
        // line rather than printing the formatter's "—".
        setLastCrashAt(toDate(summary.lastCrashAt))
        setNativeLogOff(isNativeLogOffCause(summary.nativeLogOff) ? summary.nativeLogOff : null)
      })
      .catch(() => {
        const delay = REASK_DELAYS_MS[attempt]
        if (alive && delay !== undefined) timer = setTimeout(() => ask(attempt + 1), delay)
      })
    ask(0)
    return () => { alive = false; clearTimeout(timer) }
  }, [])

  if (dismissed || count < 1) return null

  const nativeLogOffMessage = nativeLogOff ? nativeLogOffText(nativeLogOff) : null

  // Two rows, so each holds at most two controls (the two-buttons-per-row
  // cap counts the icon-only dismiss too). Row one is the message: icon, text,
  // and the dismiss top-right, where it is looked for. Row two is the actions:
  // "Show diagnostics" and "Report issue". Narrow, the actions stack full-width
  // so the longest catalogs (German, French) never crush the sentence carrying
  // the point of the notice; from `sm` they sit side by side under the text.
  // One DOM node per action at every width — no `md:hidden` + `hidden md:flex`
  // pairs, which would make `getByRole('button', { name })` match twice (a
  // Playwright strict-mode violation; "hiding is not collapsing").
  return (
    <div
      role="status"
      className="mx-3 md:mx-6 mt-4 mb-2 bg-warn/10 border border-warn/30 rounded-lg p-3 md:p-4 animate-rise"
    >
      <div className="flex items-start gap-3">
        <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
        <div className="flex-1 basis-0 min-w-0">
          <div className="text-[13px] font-medium text-text">
            {i18nT('components.crashNotice.the_app_closed_unexpectedly')}
          </div>
          <div className="text-[13px] text-muted mt-1">
            {/* `n`, not `count`: i18next reads a `count` variable as a plural
                selector and would look for `_one`/`_other` variants of this key,
                which only exist for keys registered in i18n/pluralKeys.json. */}
            {i18nT('components.crashNotice.diagnostics_were_saved_locally', { n: count })}
          </div>
          {lastCrashAt && (
            <div className="text-[13px] text-muted mt-1" data-testid="crash-notice-latest">
              {i18nT('components.crashNotice.latest_crash', { date: fmtDateTime(lastCrashAt) })}
            </div>
          )}
          {/* Status about the attachment, not an error: the crash already
              happened and was recorded, and nothing the user asked for failed.
              The line only says the chromium.log they attach is from an earlier
              session; for most causes (`in_use`, `no_access`, `unreadable`,
              `other`) the user cannot act on it, so it informs in the muted
              register with no "Ask the agent"
              (docs/decisions/2026-11-07-the-crash-notice-says-the-file-log-is-off-as-a-muted-line.md),
              after the "Latest crash" line, as the 2026-11-14 crash notice entry orders them. */}
          {nativeLogOffMessage && (
            <div className="text-[12px] text-muted mt-1" data-testid="crash-notice-native-log-off">
              {nativeLogOffMessage}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={() => setDismissed(true)}
          aria-label={i18nT('components.crashNotice.dismiss')}
          className="shrink-0 text-muted hover:text-text p-1 rounded"
        >
          <X size={14} />
        </button>
      </div>
      <div className="mt-3 flex flex-col sm:flex-row gap-2 sm:pl-[30px]">
        {/* The stock Btn is transparent, so on the warn tint it has no edge until hovered. */}
        <Btn
          onClick={() => { void revealCrashFolder() }}
          className="w-full sm:w-auto justify-center border-border-strong bg-bg-elevated"
        >
          <FolderOpen size={14} /> {i18nT('components.crashNotice.show_diagnostics')}
        </Btn>
        {/* The shared Report a Problem flow (bundle + pre-filled GitHub issue),
            started with the crash count in the note. The renderer is never
            told a path, file name or exception code (see the header), so the
            note names none. That bundle is the
            gateway's: it does not carry the desktop crash ledger, which is why
            the banner still asks the user to attach what "Show diagnostics"
            reveals, and why neither action is styled as the primary one. */}
        <Btn
          onClick={() => setReportOpen(true)}
          className="w-full sm:w-auto justify-center border-border-strong bg-bg-elevated"
        >
          <Bug size={14} /> {i18nT('components.crashNotice.report_issue')}
        </Btn>
      </div>
      {/* Failure of the reveal action, on its OWN row beneath the actions.
          `askAgent` is on: the hand-off
          navigates away, and this banner holds no unsaved input to lose — exactly
          the crash-fallback case ErrorNotice documents for it. */}
      {revealError && (
        <ErrorNotice
          message={revealError}
          askAgent
          onDismiss={() => setRevealError(null)}
          className="mt-3"
          testId="crash-notice-reveal-error"
        />
      )}
      <ReportProblemModal
        open={reportOpen}
        onClose={() => setReportOpen(false)}
        initialNote={i18nT('components.crashNotice.report_note', { n: count })}
      />
    </div>
  )
}

type NativeLogOffCause = 'disk_full' | 'no_access' | 'in_use' | 'unreadable' | 'other'

const NATIVE_LOG_OFF_CAUSES: readonly NativeLogOffCause[] = ['disk_full', 'no_access', 'in_use', 'unreadable', 'other']

function isNativeLogOffCause(value: unknown): value is NativeLogOffCause {
  return typeof value === 'string' && (NATIVE_LOG_OFF_CAUSES as readonly string[]).includes(value)
}

// One whole sentence per cause, so each catalog phrases the cause in its own
// grammar instead of splicing a fragment into a frame.
function nativeLogOffText(cause: NativeLogOffCause): string {
  switch (cause) {
    case 'disk_full': return i18nT('components.crashNotice.native_log_off_disk_full')
    case 'no_access': return i18nT('components.crashNotice.native_log_off_no_access')
    case 'in_use': return i18nT('components.crashNotice.native_log_off_in_use')
    case 'unreadable': return i18nT('components.crashNotice.native_log_off_unreadable')
    default: return i18nT('components.crashNotice.native_log_off_other')
  }
}
