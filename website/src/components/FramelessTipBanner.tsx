/**
 * FramelessTipBanner — one-time onboarding hint for Linux Wayland/CSD users.
 *
 * The Electron shell detects at boot whether the session is Wayland with
 * client-side decorations (typical GNOME/Wayland setup). In that mode there is
 * NO native window frame drawn by the OS, so the native application menu bar
 * has no place to render — File / View / Content Text Size / Connection all become
 * unreachable via keyboard or click. The user cannot know this without
 * poking at the config file.
 *
 * This banner surfaces on the first launch under that detection, offering
 * one primary action (write the `linuxFrameless: false` override, prompt for
 * restart) and one dismissal ("Don't remind me" — writes framelessTipShown
 * and never re-appears).
 *
 * Renders nothing outside Electron (browser tab: no `framePrefsAPI`), outside
 * Linux, outside Wayland, or after prior dismissal. Mounts once at App root
 * next to MigrationCheck; each render is a fresh state check against the
 * shell — no local state duplication.
 */
import { useEffect, useState } from 'react'
import { AlertCircle, X } from 'lucide-react'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'

interface FramePrefsState {
  platform: string
  isFrameless: boolean
  isWayland: boolean
  frameDecisionReason: string
  tipShown: {
    frameless: boolean
    menuBarF10: boolean
  }
}

interface FramePrefsAPI {
  getState: () => Promise<FramePrefsState>
  markTipShown: (kind: 'frameless' | 'menuBarF10') => Promise<{ ok: boolean }>
  enableFrames: () => Promise<{ restartRequired: boolean }>
  showMenuBarNow: () => Promise<{ ok: boolean }>
  restartApp: () => Promise<{ ok: boolean }>
}

declare global {
  interface Window {
    framePrefsAPI?: FramePrefsAPI
  }
}

type BannerPhase = 'hidden' | 'visible' | 'restart-prompt' | 'dismissed'

export default function FramelessTipBanner() {
  const [phase, setPhase] = useState<BannerPhase>('hidden')
  // A user-facing action on this banner CAN fail (the preload's IPC handler
  // rejects, the config write throws, `app.relaunch` fails). Each of those
  // outcomes needs a diagnostic + agent hand-off — the alternative is a dead
  // button and a user with no recovery path. `getState` failures at mount are
  // also surfaced: unlike CrashReportNotice's swallow-with-justification (which
  // knows the reject-path is a correct remote-sender refusal), our getState is
  // Linux-only local and a reject genuinely means something broke.
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const api = window.framePrefsAPI
    if (!api) return // browser tab or missing preload — never show

    let cancelled = false
    api.getState().then((state) => {
      if (cancelled) return
      // Show ONLY on the exact triple: Linux + Wayland + not previously
      // dismissed. Any other combination is silent. A dismissal is
      // permanent — the user asked us not to remind them.
      if (
        state.platform === 'linux' &&
        state.isWayland &&
        state.isFrameless &&
        !state.tipShown.frameless
      ) {
        setPhase('visible')
      }
    }).catch((err: Error) => {
      // A real failure: preload is present (we passed the `!api` check) but
      // the getState IPC rejected. Surface it — this is the discoverability
      // path for the user's own desktop, so a silent no-op leaves them with
      // no way to reach the menu at all.
      if (!cancelled) setError(err?.message || i18nT('components.framelessTipBanner.title'))
    })

    return () => { cancelled = true }
  }, [])

  const handleEnable = async () => {
    setError(null)
    const api = window.framePrefsAPI
    if (!api) return
    try {
      // Write the config. The shell auto-marks the tip as shown so we do not
      // fire it again on the next boot (the user has already acted). Move
      // straight to the restart-prompt phase.
      await api.enableFrames()
      setPhase('restart-prompt')
    } catch (err) {
      setError((err as Error)?.message || i18nT('components.framelessTipBanner.enable'))
    }
  }

  const handleRestartNow = async () => {
    setError(null)
    const api = window.framePrefsAPI
    if (!api) return
    try {
      await api.restartApp()
      // If restartApp returns (rare — the process is quitting), keep the
      // banner hidden so we don't flash back to visible during teardown.
      setPhase('dismissed')
    } catch (err) {
      setError((err as Error)?.message || i18nT('components.framelessTipBanner.restart'))
    }
  }

  const handleRestartLater = () => {
    // User chose to defer restart. Config is already written; next boot
    // picks up the framed decoration. Banner stays dismissed for this
    // session.
    setPhase('dismissed')
  }

  const handleDismiss = async () => {
    setError(null)
    const api = window.framePrefsAPI
    if (!api) return
    try {
      await api.markTipShown('frameless')
      setPhase('dismissed')
    } catch (err) {
      setError((err as Error)?.message || i18nT('components.framelessTipBanner.dismiss'))
    }
  }

  if (phase === 'hidden' || phase === 'dismissed') {
    // GPT 5.6 blocker on 16d827598: if the mount-time getState() rejected
    // with a real error, `setError` fires but `phase` stays 'hidden', so
    // the phase-null-return below would swallow the diagnostic. Render an
    // error-only shell here — the tip content stays hidden, but a caught
    // IPC failure surfaces through ErrorNotice with the agent hand-off.
    if (error) {
      return (
        <div className="mx-6 mt-4 mb-2">
          <ErrorNotice message={error} askAgent />
        </div>
      )
    }
    return null
  }

  if (phase === 'restart-prompt') {
    return (
      // Narrow-first: at 320px the fixed icon + two buttons crush the text
      // column. `flex-wrap` puts icon+text on one row and lets the buttons
      // wrap to a full-width second row; `md:flex-nowrap` keeps everything
      // on one line at desktop widths. Same pattern CrashReportNotice uses.
      <div className="mx-6 mt-4 mb-2 bg-accent/10 border border-accent/30 rounded-lg p-4 flex flex-wrap md:flex-nowrap items-start gap-3 animate-rise">
        <AlertCircle size={18} className="text-accent shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0 basis-full md:basis-auto">
          <div className="text-[13px] font-medium text-text">
            {i18nT('components.framelessTipBanner.restartTitle')}
          </div>
          <div className="text-[13px] text-muted mt-1">
            {i18nT('components.framelessTipBanner.restartBody')}
          </div>
        </div>
        <Btn primary onClick={handleRestartNow} className="shrink-0 basis-full md:basis-auto">
          {i18nT('components.framelessTipBanner.restart')}
        </Btn>
        <Btn onClick={handleRestartLater} className="shrink-0 basis-full md:basis-auto">
          {i18nT('components.framelessTipBanner.later')}
        </Btn>
        {/* askAgent true: the banner is a stateless onboarding hint — no draft
            input in this subtree that a chat navigation would destroy. */}
        <ErrorNotice message={error} askAgent className="basis-full mt-2" />
      </div>
    )
  }

  // phase === 'visible'
  return (
    <div className="mx-6 mt-4 mb-2 bg-warn/10 border border-warn/30 rounded-lg p-4 flex flex-wrap md:flex-nowrap items-start gap-3 animate-rise">
      <AlertCircle size={18} className="text-warn shrink-0 mt-0.5" />
      <div className="flex-1 min-w-0 basis-full md:basis-auto">
        <div className="text-[13px] font-medium text-text">
          {i18nT('components.framelessTipBanner.title')}
        </div>
        <div className="text-[13px] text-muted mt-1">
          {i18nT('components.framelessTipBanner.body')}
        </div>
      </div>
      <Btn primary onClick={handleEnable} className="shrink-0 basis-full md:basis-auto">
        {i18nT('components.framelessTipBanner.enable')}
      </Btn>
      <Btn onClick={handleDismiss} className="shrink-0 basis-full md:basis-auto">
        <X size={14} /> {i18nT('components.framelessTipBanner.dismiss')}
      </Btn>
      {/* askAgent true: read-only onboarding hint, no draft state at risk. */}
      <ErrorNotice message={error} askAgent className="basis-full mt-2" />
    </div>
  )
}
