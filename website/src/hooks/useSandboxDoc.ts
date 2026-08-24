import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'

/** Mint a gateway-served document URL for model-authored HTML shown in an iframe.
 *
 * Every surface that renders artifact or widget HTML goes through here rather
 * than building a `blob:` URL: some WebKit-based in-app browsers refuse a blob
 * load outright ("invalid url or response") and can take the whole page down
 * with it, and a sandboxed `srcdoc` frame blank-renders on WebKit. A plain
 * https document is the only form observed to load on every surface.
 *
 * One hook rather than the same effect in four components, because the state
 * machine has three non-obvious rules that were each got wrong when copied:
 *
 * - The PREVIOUS url survives an in-flight mint. Clearing it first flashes an
 *   open document out to a placeholder on every theme change, since a theme
 *   change rebuilds the html and costs a round trip.
 * - The previous url also survives a FAILED mint. A transient blip while the
 *   user is reading must not replace a document that is rendering fine; the
 *   caller shows the failure notice alongside it instead.
 * - `failed` clears when a retry STARTS, so the surface visibly acknowledges
 *   the click instead of staying pixel-identical until the attempt lands.
 */
export function useSandboxDoc(srcdoc: string | null | undefined): {
  /** The minted document URL, or null before the first one lands. */
  url: string | null
  /** The last mint attempt failed. `url` may still hold a working document. */
  failed: boolean
  /** Mint again. Required for recovery: the URL is single-use server-side, so
   *  re-rendering a spent one recovers nothing. */
  retry: () => void
} {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!srcdoc) {
      setUrl(null)
      setFailed(false)
      return
    }
    let alive = true
    setFailed(false)
    api
      .sandboxDocUrl(srcdoc)
      .then((r) => {
        if (!alive) return
        setUrl(r.url)
        setFailed(false)
      })
      .catch(() => {
        if (!alive) return
        // The previous url is deliberately left in place — see the contract above.
        setFailed(true)
      })
    return () => {
      alive = false
    }
  }, [srcdoc, attempt])

  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { url, failed, retry }
}
