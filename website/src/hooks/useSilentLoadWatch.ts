import { useCallback, useEffect, useState } from 'react'

/** How long a sandboxed frame may stay unloaded after its url lands before the
 *  surface treats the load as SILENT.
 *
 *  A silent load is the case `useSandboxDoc.failed` does NOT cover: the mint
 *  succeeded (a url is in hand) but the frame's `load` event never fires, so a
 *  surface that reveals only on load stays blank forever with no notice and no
 *  recovery. The value matches the window ArtifactBody has used since the
 *  affordance was introduced there; this hook is that same affordance lifted
 *  out so the three sibling frames (WidgetFrame, ArtifactThumbs,
 *  RemoteArtifactDetailPage) get it without a fourth copy of the timer. */
export const SILENT_LOAD_GRACE_MS = 3000

/** Watch a sandbox-doc frame for a load that never arrives.
 *
 *  One hook rather than the same effect in four components — the exact reason
 *  useSandboxDoc gives for itself: the arming rule has a non-obvious edge that
 *  is easy to get wrong when copied. The timer must arm against the CURRENT
 *  url and be cleared the instant that url reports load; arming on a re-mint
 *  before the new document has had any chance to load would fire a false
 *  silent verdict on every theme change or content refetch.
 *
 *  Usage: pass the minted `url` (null while a mint is in flight) and call
 *  `onLoaded` from the frame's own `load` handler (or a ref-bound listener).
 *  `silent` turns true only if the grace window elapses with no load for the
 *  url that is currently in hand. It resets whenever the url changes, so a
 *  re-mint gets a fresh window rather than inheriting the previous verdict.
 */
export function useSilentLoadWatch(
  url: string | null | undefined,
): {
  /** The current url landed but never reported load within the grace window. */
  silent: boolean
  /** Call from the frame's `load` handler: clears the window for this url. */
  onLoaded: () => void
} {
  const [silent, setSilent] = useState(false)
  // The url the last load belonged to. The timer arms only while this trails
  // the current url — i.e. the current document has not yet loaded. On a
  // re-mint to a new url this no longer matches, so the window re-arms; on a
  // React no-op re-mint to the SAME url string it still matches, so no false
  // window opens for a document that is already showing.
  const [loadedUrl, setLoadedUrl] = useState<string | null>(null)

  // A new url starts the observation over, in the same commit that re-arms
  // below, so the previous document's verdict never leaks onto the new one.
  useEffect(() => {
    setSilent(false)
  }, [url])

  useEffect(() => {
    if (!url || loadedUrl === url) return
    const timer = setTimeout(() => setSilent(true), SILENT_LOAD_GRACE_MS)
    return () => clearTimeout(timer)
  }, [url, loadedUrl])

  const onLoaded = useCallback(() => {
    setLoadedUrl(url ?? null)
    setSilent(false)
  }, [url])

  return { silent, onLoaded }
}
