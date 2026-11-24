/**
 * The picture a panel keeps while a corner is held and until a picture at the new size has stood (`Panel.tsx`, D84 1,
 * D96 5). The source writes no picture into it (`PageSource.keepPicture`), and it is let go when the panel lets it
 * go, when the panel goes, or when its source changes (pages put away, a room switch, M59).
 */
import { useCallback, useEffect, useRef } from 'react'
import type { Texture } from 'three'
import type { PageSource } from './source'

export function useKeptPicture(source: PageSource) {
  const kept = useRef<Texture | null>(null)
  /** Keep the picture drawn now; one kept before is let go. */
  const keep = useCallback(() => { kept.current = source.keepPicture?.() ?? source.frame().texture }, [source])
  /** Let it go: the live picture is drawn from here, and the source may free the kept texture. */
  const drop = useCallback(() => { kept.current = null; source.letGoPicture?.() }, [source])
  // The panel goes, or its source is another: the old source's kept picture goes with it.
  useEffect(() => () => { if (kept.current) drop() }, [drop])
  return { kept, keep, drop }
}
