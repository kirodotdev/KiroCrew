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
 * The renderer is told a COUNT and nothing else — not even a timestamp. Paths,
 * filenames and exception codes stay in the main process (see `crashNoticeSummary` in
 * electron/crash-collector.js) — the reveal action is a main-process gesture
 * that names no path, so this component never holds one.
 */
import { useEffect, useState } from 'react'
import { AlertTriangle, FolderOpen, X } from 'lucide-react'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'

import { i18nT } from '../i18n/t'

/**
 * Bridge from electron/preload.js. Absent in a plain browser and in the PWA,
 * where there is no local disk holding crash artifacts to reveal — the banner
 * simply never renders, rather than offering an action that cannot work.
 */
type CrashReportsAPI = {
  get(): Promise<{ newCount: number }>
  reveal(): Promise<{ ok: boolean; error?: string }>
}
const crashReportsAPI = (): CrashReportsAPI | undefined =>
  (window as { crashReportsAPI?: CrashReportsAPI }).crashReportsAPI

export default function CrashReportNotice() {
  const [count, setCount] = useState(0)
  const [dismissed, setDismissed] = useState(false)
  const [revealError, setRevealError] = useState<string | null>(null)

  // The banner's ONE action must not fail into silence. `reveal()` reports its
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
      .then(summary => { if (alive) setCount(summary.newCount) })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  if (dismissed || count < 1) return null

  // Narrow-first, by wrapping rather than by hiding.
  //
  // The first shape of this banner was one horizontal row: icon, text, a
  // `shrink-0` reveal button, a `shrink-0` dismiss. At 320px those three fixed
  // items leave the text column about 50px wide, and the German and French
  // strings are the longest of the twelve catalogs — so the sentence carrying
  // the whole point of the notice is what gets crushed.
  //
  // `flex-wrap` -> `md:flex-nowrap` fixes it without a second copy of any
  // control. Narrow, the icon + text + dismiss share line one and the
  // full-width reveal button wraps to line two; at `md` everything is back on a
  // single line, with `order` putting the reveal button ahead of the dismiss
  // again. Deliberately NOT `md:hidden` + `hidden md:flex` pairs: two DOM nodes
  // for one action means `getByRole('button', { name })` matches twice, which is
  // a Playwright strict-mode violation, and it is also what the
  // `narrow-viewport-required` rule means by "hiding is not collapsing".
  return (
    <div
      role="status"
      className="mx-3 md:mx-6 mt-4 mb-2 bg-warn/10 border border-warn/30 rounded-lg p-3 md:p-4 animate-rise"
    >
      <div className="flex flex-wrap md:flex-nowrap items-start gap-3">
      <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5 order-1" />
      <div className="flex-1 basis-0 min-w-0 order-2">
        <div className="text-[13px] font-medium text-text">
          {i18nT('components.crashNotice.the_app_closed_unexpectedly')}
        </div>
        <div className="text-[13px] text-muted mt-1">
          {/* `n`, not `count`: i18next reads a `count` variable as a plural
              selector and would look for `_one`/`_other` variants of this key,
              which only exist for keys registered in i18n/pluralKeys.json. */}
          {i18nT('components.crashNotice.diagnostics_were_saved_locally', { n: count })}
        </div>
      </div>
      {/* Dismiss stays top-right at every width — that is where it is looked
          for — so narrow it ends line one and `md:order-4` moves it back to the
          far end once the reveal button rejoins the row. */}
      <button
        type="button"
        onClick={() => setDismissed(true)}
        aria-label={i18nT('components.crashNotice.dismiss')}
        className="shrink-0 text-muted hover:text-text p-1 rounded order-3 md:order-4"
      >
        <X size={14} />
      </button>
      <Btn
        onClick={() => { void revealCrashFolder() }}
        className="shrink-0 w-full md:w-auto justify-center order-4 md:order-3"
      >
        <FolderOpen size={14} /> {i18nT('components.crashNotice.show_diagnostics')}
      </Btn>
      </div>
      {/* Failure of the only action, on its OWN row beneath the controls — a
          sibling of the flex row, not a child of it, because that row is
          `md:flex-nowrap` and a full-width child there would not wrap, it would
          crush the text column instead. `askAgent` is on: the hand-off
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
    </div>
  )
}
