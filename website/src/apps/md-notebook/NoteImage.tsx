/**
 * Rendered `![alt](src)` image for the Notes preview.
 *
 * A component rather than a bare `<img>` because a note's image is the one
 * inline span that can fail after it renders: the file was renamed in the
 * vault, the remote host is unreachable, the path points outside what the
 * dashboard will serve. When that happens the block falls back to the alt text
 * (or the file name, when the note gave no alt), so the reader still learns
 * what was meant to be there instead of facing a broken frame. That mirrors
 * what the chat renderer does for the same failure.
 *
 * The image is capped at the width of its container rather than sized in
 * pixels, so it follows the reading column and the full-width mode instead of
 * overflowing the measure. Height is capped at 60vh, like the chat renderer, so
 * a tall phone screenshot or whiteboard photo stays one screenful instead of
 * pushing the rest of the note several viewports down.
 *
 * A min-height floor holds the block's space until the bytes decode. Markdown
 * carries no dimensions, so without it the image lays out at nothing and then
 * snaps to its natural height: in this app that shift lands under a click that
 * means "edit the line I am pointing at", so it would open the wrong block's
 * editor. The floor is released on load, which keeps the final layout exact.
 * SVGs get a definite width basis instead, because one authored with only a
 * `viewBox` has no intrinsic size and would collapse under a max-width cap.
 *
 * No click handler: in this app a click means "edit the source of this line",
 * and the surrounding block already carries that gesture. Swallowing the click
 * to open a lightbox would make the image the only span in a note you cannot
 * click to edit.
 *
 * The failed/loaded state belongs to ONE source. An Obsidian embed keeps its
 * positional React key while its `src` changes underneath it: the first paint
 * resolves the embed by convention (attachment folder, then the note's folder)
 * before the attachment index has loaded, and the by-name lookup may move it
 * to another folder a moment later. If the provisional URL 404s first, a state
 * that outlived the source would pin the block on the failure fallback and
 * never request the corrected one. So each flag remembers the source it was
 * observed for, and a new source starts clean without remounting anything.
 */
import { ImageOff } from 'lucide-react'
import { useState } from 'react'

/** File name of a source, used as the fallback label when alt is empty. */
function srcLabel(src: string): string {
  const clean = src.split(/[?#]/)[0]
  const slash = Math.max(clean.lastIndexOf('/'), clean.lastIndexOf('\\'))
  return (slash === -1 ? clean : clean.slice(slash + 1)) || src
}

export function NoteImage({
  src,
  alt,
  rawSrc,
  width,
  reason,
}: {
  src: string | null
  alt: string
  rawSrc: string
  /** Requested display width in CSS pixels (an Obsidian `![[file|720]]`), still capped by the column. */
  width?: number
  /**
   * Why the image is not shown, when the reason is not "no such file". Rendered
   * as visible text beside the file name (and repeated as the tooltip), so a
   * reader whose file IS in the vault (twice) learns why it did not render
   * instead of going looking for a missing one -- on touch, by keyboard and
   * through a screen reader, none of which ever see a hover title.
   */
  reason?: string
}) {
  // The source each event was observed for, not a bare boolean: a flag that
  // outlived its source would keep a corrected `src` from ever being requested
  // (see the note above). Comparing against the current `src` retires the old
  // observation the moment the source moves, with no effect and no remount.
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null)
  const failed = src !== null && failedSrc === src
  const loaded = src !== null && loadedSrc === src
  if (src === null || failed) {
    // The glyph, not the muted colour, is what says "this was an image": alt
    // text alone reads as an ordinary sentence, so a renamed or refused file
    // would look like prose the author wrote. Same signal the chat renderer
    // gives for the same failure. Both failures share this one presentation:
    // a source the app refuses up front and a file that disappears after the
    // note was written are the same event to the reader.
    return (
      <>
        <span
          title={reason}
          style={{ color: 'var(--muted)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
        >
          <ImageOff size={14} aria-hidden="true" />
          {alt || srcLabel(rawSrc)}
        </span>
        {reason === undefined ? null : (
          <span style={{ color: 'var(--muted)', fontStyle: 'italic', fontSize: '0.9em', marginLeft: '4px' }}>
            ({reason})
          </span>
        )}
      </>
    )
  }
  const isSvg = /\.svg([?#]|$)/i.test(rawSrc)
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- `onLoad`/`onError` are resource events (release the layout floor, swap in the fallback), not gestures; the image deliberately carries no click, per the note above
    <img
      src={src}
      alt={alt}
      onLoad={() => setLoadedSrc(src)}
      onError={() => setFailedSrc(src)}
      style={{
        maxWidth: '100%',
        maxHeight: '60vh',
        objectFit: 'contain',
        ...(isSvg ? { width: '100%', height: 'auto' } : { height: loaded ? 'auto' : undefined, minHeight: loaded ? undefined : '120px' }),
        // After the SVG basis so an authored width wins over it; `maxWidth`
        // above still keeps a wide request inside the reading column.
        ...(width === undefined ? null : { width: `${width}px` }),
        display: 'block',
        margin: '4px 0',
        borderRadius: '4px',
      }}
    />
  )
}
