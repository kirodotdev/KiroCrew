import { useEffect, useState } from 'react'

/**
 * Animated collapsible for unknown-height content (folder bodies).
 *
 * Uses the CSS grid `1fr`/`0fr` trick so the body can animate to its intrinsic
 * height without measuring it, and keeps its children MOUNTED while closed:
 * `sessionRowNav.ts` walks the rows of a collapsed folder to carry keyboard
 * navigation past it, so unmounting them (or `display: none`) is not available.
 *
 * Two properties are needed for the closed state, and they cannot both apply at
 * the same moment:
 *
 *   - `visibility: hidden` stops the closed rows PAINTING. It flips with `open`,
 *     so the shrinking track reads as empty space closing rather than rows
 *     sliding away.
 *   - `content-visibility: hidden` stops them occupying LAYOUT. Without it the
 *     clipped rows still contribute scrollable overflow to the nearest scroll
 *     container, which is what let a collapsed folder leave ~3000px of dead
 *     scroll height under the sidebar's session list.
 *
 * `content-visibility` is therefore DEFERRED to the end of the collapse: applied
 * at once it would zero the subtree's height before the track could animate away
 * from `1fr`, and the folder would snap shut instead of closing. Opening is the
 * mirror image — layout is restored first, so the track has something to animate
 * toward. A folder that mounts already closed suppresses immediately, so a
 * freshly rendered list neither animates nor reserves height.
 *
 * This lives in one place deliberately: it used to be copied per call site, and
 * the copy kept the layout defect after the original was fixed.
 */

/** Collapse duration. Shared by the CSS transition and the deferral timer so
 *  the two cannot drift — a timer shorter than the transition would snap. */
export const FOLDER_BODY_COLLAPSE_MS = 150

export function FolderBody({
  open,
  padding = '2px',
  children,
}: {
  open: boolean
  /** Applied while open; the closed state always collapses padding to 0. */
  padding?: string
  children?: React.ReactNode
}) {
  // Mount state, not an effect result: a body that starts closed must already be
  // suppressed on its first paint rather than reserving height for one frame.
  const [layoutSuppressed, setLayoutSuppressed] = useState(!open)

  useEffect(() => {
    if (open) {
      // Restore layout before the track animates open, otherwise `1fr` resolves
      // against a zero-height subtree and the expand snaps too.
      setLayoutSuppressed(false)
      return
    }
    const timer = setTimeout(() => setLayoutSuppressed(true), FOLDER_BODY_COLLAPSE_MS)
    return () => clearTimeout(timer)
  }, [open])

  return (
    <div
      aria-hidden={!open}
      // @ts-expect-error inert is a valid HTML attribute but TS types may lag
      inert={!open ? '' : undefined}
      style={{
        display: 'grid',
        gridTemplateRows: open ? '1fr' : '0fr',
        transition: `grid-template-rows ${FOLDER_BODY_COLLAPSE_MS}ms ease-out`,
      }}
    >
      <div style={{
        overflow: 'hidden',
        visibility: open ? 'visible' : 'hidden',
        contentVisibility: layoutSuppressed ? 'hidden' : 'visible',
        padding: open ? padding : 0,
      }}>{children}</div>
    </div>
  )
}

export default FolderBody
