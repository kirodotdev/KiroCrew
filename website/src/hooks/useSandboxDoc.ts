import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'

/** How long a mint may stay pending before the pending flag is released.
 *
 * `pending` disables the caller's recovery button, and the mint POST is a bare
 * fetch with no timeout — for a RE-MINT attempted from an already-mounted
 * notice, a wedged gateway (request accepted, response never written) would
 * otherwise leave that notice's only affordance disabled for the life of the
 * mount. (A wedged FIRST mint shows no notice at all — `failed` never turns on
 * — so the ceiling has nothing to restore there; that gap predates `pending`.)
 * When the ceiling releases the flag the attempt may still settle later; both
 * settle paths are idempotent, so a late arrival is harmless. */
const MINT_PENDING_CEILING_MS = 15_000

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
 * - `failed` clears on a successful settle, or when `srcdoc` goes away (which
 *   tears the whole state down) — never at retry start. Clearing at start
 *   flips a caller's failure notice to whatever its
 *   other states show (or unmounts it) before anything about the frame has
 *   changed — the accessible name of the control the user just pressed must
 *   not change mid-flight. `pending` is what acknowledges the click.
 */
export function useSandboxDoc(srcdoc: string | null | undefined): {
  /** The minted document URL, or null before the first one lands. */
  url: string | null
  /** The last mint attempt failed. `url` may still hold a working document. */
  failed: boolean
  /** A mint is in flight. Callers use this to disable their recovery action:
   *  a re-mint can resolve with the SAME url string (a React no-op that fires
   *  no new `load`), so a caller that hides its notice at click time can end up
   *  with no affordance at all if nothing observable changes. Disabling on
   *  `pending` acknowledges the click without removing the notice. */
  pending: boolean
  /** Mint again. Required for recovery: the URL is single-use server-side, so
   *  re-rendering a spent one recovers nothing. */
  retry: () => void
} {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const [pending, setPending] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!srcdoc) {
      setUrl(null)
      setFailed(false)
      setPending(false)
      return
    }
    let alive = true
    // Deliberately NOT setFailed(false) here: failed clears only on a
    // successful settle. See the third rule in the header comment.
    setPending(true)
    // A wedged POST would otherwise pin `pending` (and the caller's disabled
    // recovery button) for the life of the mount. Releasing the flag does not
    // abort the attempt; the settle paths below remain valid if it lands late.
    const ceiling = setTimeout(() => {
      if (alive) setPending(false)
    }, MINT_PENDING_CEILING_MS)
    api
      .sandboxDocUrl(srcdoc)
      .then((r) => {
        if (!alive) return
        clearTimeout(ceiling)
        setUrl(r.url)
        setFailed(false)
        setPending(false)
      })
      .catch(() => {
        if (!alive) return
        clearTimeout(ceiling)
        // The previous url is deliberately left in place — see the contract above.
        setFailed(true)
        setPending(false)
      })
    return () => {
      alive = false
      clearTimeout(ceiling)
    }
  }, [srcdoc, attempt])

  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { url, failed, pending, retry }
}
