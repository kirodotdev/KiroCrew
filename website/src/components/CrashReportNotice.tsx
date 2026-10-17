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
 * The renderer is told a COUNT and, only while Chromium's file log is off for
 * this launch, one cause code from a fixed set. Never a timestamp. Paths,
 * filenames and exception codes stay in the main process (see `crashNoticeSummary` in
 * electron/crash-collector.js) — the reveal action is a main-process gesture
 * that names no path, so this component never holds one.
 */
import { useEffect, useState } from 'react'
import { AlertTriangle, Bug, FolderOpen, X } from 'lucide-react'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import ReportProblemModal from './ReportProblemModal'

import { i18nT } from '../i18n/t'

/**
 * Bridge from electron/preload.js. Absent in a plain browser and in the PWA,
 * where there is no local disk holding crash artifacts to reveal — the banner
 * simply never renders, rather than offering an action that cannot work.
 */
const crashReportsAPI = (): CrashReportsAPI | undefined => window.crashReportsAPI

/**
 * The cause behind a kept-off file log, as one plain sentence.
 *
 * The codes are the closed set `NATIVE_LOG_OFF_REASONS` in
 * electron/native-logging.js, and none of them reaches the screen: a reporter
 * cannot act on `replace-failed`, but can pass on "could not be replaced". Each
 * case spells its catalog key in full so the key-reference gate can see it
 * (a built key would look dead). Anything outside the set reads as the
 * not-recorded sentence, the same fallback the main process applies.
 */
function nativeLogOffCause(reason: string): string {
  switch (reason) {
    case 'unsupported-fs': return i18nT('components.crashNotice.native_log_off_cause_unsupported_fs')
    case 'not-a-regular-file': return i18nT('components.crashNotice.native_log_off_cause_not_regular_file')
    case 'unreadable': return i18nT('components.crashNotice.native_log_off_cause_unreadable')
    case 'replace-failed': return i18nT('components.crashNotice.native_log_off_cause_replace_failed')
    case 'error': return i18nT('components.crashNotice.native_log_off_cause_error')
    case 'rotate-failed': return i18nT('components.crashNotice.native_log_off_cause_rotate_failed')
    default: return i18nT('components.crashNotice.native_log_off_cause_unknown')
  }
}

export default function CrashReportNotice() {
  const [count, setCount] = useState(0)
  const [nativeLogOff, setNativeLogOff] = useState<string | null>(null)
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
    // Swallowing the rejection is deliberate: the handler rejects a sender that
    // is not the local dashboard (a connection window pointed at a remote
    // gateway shares this preload), and that is a correct outcome, not an error
    // worth putting on screen.
    void api.get()
      .then(summary => {
        if (!alive) return
        // The log-off state first: the report dialog below is mounted once the
        // banner renders (`count` >= 1) and reads its starting note only then, so
        // the fact has to be in place before the count makes the banner appear.
        setNativeLogOff(summary.nativeLogOff ?? null)
        setCount(summary.newCount)
      })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  if (dismissed || count < 1) return null

  // What the report starts from: the count, and while the file sink is off the
  // fact and its cause as plain sentences, so the reporter does not retype what
  // the app already knows and a maintainer reads it in the report rather than in
  // a paraphrase. The fact is scoped to the session the report is made from, and
  // says a log file left by an earlier session may still cover the crash: the
  // rotation that failed leaves that file where it was, and this component cannot
  // tell whether the crashed launch had the sink on. No code or filename here.
  const reportNote = [
    i18nT('components.crashNotice.report_note', { n: count }),
    ...(nativeLogOff
      ? [i18nT('components.crashNotice.report_note_native_log_off'), nativeLogOffCause(nativeLogOff)]
      : []),
  ].join('\n')

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
          {/* The previous log could not be redacted or rotated, so the file sink
              stays off for this launch: a failed outcome, shown as an error.
              The text is the reporter's words, not the code's: the log from
              last time could not be moved out of the way, so no new one was
              started; the crash records are a different thing and unaffected;
              Report issue adds the fact to the report (`reportNote`, so nobody
              retypes it); then a "Technical
              detail" line carrying the cause as a plain sentence. It claims only
              what the main process knows,
              that the sink is off for THIS launch: the crash counted above
              happened in an earlier one, so nothing is said about whether the
              file on disk covers it. The raw code and the log's filename stay
              out of it, and the line goes to the agent with the hand-off.
              `askAgent` is on: this banner holds no unsaved input for the
              hand-off to lose, and the agent can look into the cause. `below`
              keeps the hand-off out of a side column, so the sentence keeps the
              banner's width at 320px. */}
          {nativeLogOff && (
            <ErrorNotice
              message={`${i18nT('components.crashNotice.native_log_off')}\n${nativeLogOffCause(nativeLogOff)}`}
              askAgent
              actionPlacement="below"
              className="mt-2"
              testId="crash-notice-native-log-off"
            />
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
        <Btn
          onClick={() => { void revealCrashFolder() }}
          className="w-full sm:w-auto justify-center"
        >
          <FolderOpen size={14} /> {i18nT('components.crashNotice.show_diagnostics')}
        </Btn>
        {/* The shared Report a Problem flow (bundle + pre-filled GitHub issue),
            started with what this banner holds: the count and, while the file
            sink is off, that fact and its cause (`reportNote`). The renderer is
            never told more (see the header), so the note names no path, filename
            or exception code. That bundle is the
            gateway's: it does not carry the desktop crash ledger, which is why
            the banner still asks the user to attach what "Show diagnostics"
            reveals, and why neither action is styled as the primary one. */}
        <Btn
          onClick={() => setReportOpen(true)}
          className="w-full sm:w-auto justify-center"
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
        initialNote={reportNote}
      />
    </div>
  )
}
