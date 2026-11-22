/**
 * Fire-and-forget usage counts for app pages: one `opened` per mount, and one
 * `foreground` span per stretch the page was visible.
 *
 * Sends only the app's name, where it was shown and a whole number of seconds.
 * The gateway resolves the public id and version itself (an app with no public
 * id is `private`), records `kirocrew.app.opened` / `kirocrew.app.foreground_time`
 * and answers 204; with telemetry consent off it records nothing. The route is
 * owner-only, so these count the owner's own views. A failure is swallowed here,
 * so reporting can never change what the page does.
 */

import { useEffect, useRef } from 'react'
import { api } from '../api/client'
import type { AppViewReport } from '../api/client/telemetry'
import { appPathname } from './basePath'
import { useLocalCrewHidden } from '../components/overlayRoot'
import { appPopoutRouteOf } from '../utils/appPopoutRoute'

/** A span shorter than this is a flicker, not a view, and is not sent. */
const MIN_SPAN_MS = 1000

export function reportAppView(report: AppViewReport): void {
  try {
    void Promise.resolve(api.reportAppView(report)).catch(() => {})
  } catch {
    // Never let the usage count affect the page.
  }
}

/** `popout` in an app pop-out window (`/popout/app/...`), else `page`. */
function viewSurface(): 'page' | 'popout' {
  return appPopoutRouteOf(appPathname()) ? 'popout' : 'page'
}

/**
 * Count *app*'s page: one `opened` when it mounts (a re-render sends nothing),
 * and one `foreground` span each time the page stops being visible: a hidden
 * tab, another Crew's pane in front of this one (`useLocalCrewHidden`, which
 * hides the page without unmounting it), a zero-size viewport (this dashboard in
 * another Crew's `display:none` pane frame, which leaves the document
 * `visible`), unmount or `pagehide`. Coming back
 * starts a new span and sends no second `opened`. `null` counts nothing.
 */
export function useAppViewMetrics(app: string | null): void {
  const crewHidden = useLocalCrewHidden()
  const crewHiddenRef = useRef(crewHidden)
  // The live span's controls, so the effect below can pause and resume it.
  const spanRef = useRef<{ check: () => void } | null>(null)
  // Declared first: on a render that changes both inputs, React runs every
  // cleanup, then the effects in order, so the span effect reads the new value.
  useEffect(() => {
    crewHiddenRef.current = crewHidden
    spanRef.current?.check()
  }, [crewHidden])
  useEffect(() => {
    if (!app) return
    const surface = viewSurface()
    reportAppView({ app, surface, event: 'opened' })
    // A frame hidden with `display:none` keeps `visibilityState` at `visible` but
    // lays out at 0x0 and fires `resize`, so an empty viewport counts as hidden.
    const shown = () =>
      document.visibilityState === 'visible' && !crewHiddenRef.current
      && window.innerWidth > 0 && window.innerHeight > 0
    // A monotonic clock, so a wall-clock change mid-span neither inflates nor drops it.
    let shownAt: number | null = shown() ? performance.now() : null
    const endSpan = () => {
      if (shownAt === null) return
      const ms = performance.now() - shownAt
      shownAt = null
      if (ms >= MIN_SPAN_MS) reportAppView({ app, surface, event: 'foreground', seconds: Math.floor(ms / 1000) })
    }
    const check = () => {
      if (!shown()) endSpan()
      else if (shownAt === null) shownAt = performance.now()
    }
    spanRef.current = { check }
    document.addEventListener('visibilitychange', check)
    window.addEventListener('resize', check)
    window.addEventListener('pagehide', endSpan)
    return () => {
      spanRef.current = null
      document.removeEventListener('visibilitychange', check)
      window.removeEventListener('resize', check)
      window.removeEventListener('pagehide', endSpan)
      endSpan()
    }
  }, [app])
}
