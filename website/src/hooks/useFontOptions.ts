import { useCallback, useEffect, useState } from 'react'

import {
  detectInstalledFonts,
  isLocalFontAccessSupported,
  queryLocalMonospaceFonts,
} from '../utils/fontDetect'

/* ── Installed monospace families for the terminal font picker ──────────────
 *
 * Two detection layers, because neither alone is enough. The candidate probe
 * (`detectInstalledFonts`) needs no permission and works in every browser, but
 * can only confirm names it is handed. The Local Font Access API returns the real
 * font book, but exists only in Chromium, only in a secure context, and only
 * behind a permission prompt — so it is offered as an action the user takes, not
 * something that fires on mount.
 *
 * The probe runs in an effect rather than during render: its first call makes the
 * browser resolve every candidate name, which is work that does not belong in a
 * paint. Until it lands the picker shows the default row and free text, which is
 * exactly the state a browser with no measurable canvas stays in. */

export interface FontOptionsState {
  /** Installed monospace families, probe order then font-book order. */
  families: string[]
  /** Whether the full-enumeration action is worth offering at all. */
  accessSupported: boolean
  /**
   * Outcome of the last `enumerate()` run, for the caller to render next to the
   * action that triggered it.
   *
   * Every terminal state reports, `added` included: the list growing is not
   * feedback the user can rely on, because the filter that made them run the
   * action can also hide every family it added, leaving the popup unchanged after
   * a permission grant.
   */
  lastResult: 'idle' | 'checking' | 'denied' | 'added' | 'none'
  /** Ask the browser for the whole font book. MUST run from a user gesture. */
  enumerate: () => void
}

export function useFontOptions(): FontOptionsState {
  const [families, setFamilies] = useState<string[]>([])
  const [lastResult, setLastResult] = useState<FontOptionsState['lastResult']>('idle')
  const [accessSupported] = useState(() => isLocalFontAccessSupported())

  useEffect(() => {
    let cancelled = false
    const probe = () => {
      if (!cancelled) setFamilies(prev => (prev.length ? prev : detectInstalledFonts()))
    }
    // Wait for web fonts before measuring. The dashboard loads its own monospace
    // face, which resolves AFTER first paint, so a probe that runs immediately
    // measures it as missing and the list silently omits the family the terminal
    // already ships with — and whether it lands at all comes down to load timing.
    // `document.fonts` is absent in the test environment, where measurement is
    // stubbed anyway.
    const fonts = document.fonts
    if (fonts) void fonts.ready.then(probe)
    else probe()
    return () => { cancelled = true }
  }, [])

  const enumerate = useCallback(() => {
    // Set before awaiting: where permission was already granted the query
    // resolves without a prompt and the list may not change at all, so the click
    // would otherwise have no visible effect while it ran.
    setLastResult('checking')
    void queryLocalMonospaceFonts().then(result => {
      if (!result.ok) {
        // `unsupported` cannot reach here (the action is not offered), so this is
        // a block or a dismissed prompt: say so and keep the probed list.
        setLastResult('denied')
        return
      }
      // Union, probed names first: the font book is authoritative about what
      // exists, but the probe order puts the families a terminal user is most
      // likely to want at the top of an alphabetical list of hundreds.
      setFamilies(prev => {
        const seen = new Set(prev)
        const added = result.families.filter(name => !seen.has(name))
        setLastResult(added.length ? 'added' : 'none')
        return added.length ? [...prev, ...added] : prev
      })
    })
  }, [])

  return { families, accessSupported, lastResult, enumerate }
}
