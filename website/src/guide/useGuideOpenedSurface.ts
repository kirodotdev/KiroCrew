/**
 * Close a surface a guide opened once that guide is called off, but only when
 * the user never touched it.
 *
 * A guide's action can open a form over the page (the Crewmates page's creation
 * flow over the member chat). When the guide is then cancelled or expires, the
 * form stays up and covers the chat that shows the guide's result line. Closing
 * it is right only while it is PRISTINE: a form the user typed into or moved
 * forward is their work, and closing it is never the guide's call. A guide that
 * COMPLETED leaves the surface alone too: the surface shows its own success.
 *
 * `markOpened()` is called by the surface when it opens; it records the guide
 * this tab is walking through *actionId*, if any. Opened any other way (the
 * user's own click), nothing is recorded and nothing is ever closed.
 */
import { useCallback, useEffect, useRef } from 'react'
import { useGuide } from './GuideContext'

export function useGuideOpenedSurface(
  actionId: string,
  { open, isPristine, close }: { open: boolean; isPristine: () => boolean; close: () => void },
): () => void {
  const ctx = useGuide()
  const ctxRef = useRef(ctx)
  ctxRef.current = ctx
  const openerRef = useRef<string | null>(null)
  const latest = useRef({ isPristine, close })
  latest.current = { isPristine, close }

  const markOpened = useCallback(() => {
    const view = ctxRef.current?.view
    openerRef.current = view?.ownedHere && view.action?.id === actionId ? view.guide.guide_id : null
  }, [actionId])

  // Closed by the user (or by its own success): no longer the guide's surface.
  // Only an open -> closed edge clears it; the surface may mark before it has
  // finished opening.
  const wasOpen = useRef(open)
  useEffect(() => {
    if (wasOpen.current && !open) openerRef.current = null
    wasOpen.current = open
  }, [open])

  const guides = ctx?.guides
  useEffect(() => {
    const opener = openerRef.current
    if (!opener || !guides) return
    const g = guides.find(x => x.guide_id === opener)
    if (!g) return
    // Ended because its save went through: the surface is showing what was
    // made (the ready step), so it stays, exactly as for a completed guide.
    if (g.status === 'completed' || (g.status === 'cancelled' && g.reason === 'saved_without_guide')) {
      openerRef.current = null
      return
    }
    if (g.status !== 'cancelled' && g.status !== 'expired') return
    openerRef.current = null
    if (open && latest.current.isPristine()) latest.current.close()
  }, [guides, open])

  return markOpened
}
