import { useEffect } from 'react'
import { Folder, X } from 'lucide-react'
import { InstantTip, useInstantTip } from '../InstantTip'
import { dispatchLightbox } from '../MarkdownRenderer'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { IMG_EXT, buildFileLabels } from '../../utils/fileTokens'
import type { ResizeInfo } from '../../utils/resizeImage'
import { i18nT } from '../../i18n/t'

/** Accent pill under a downscaled attachment chip. Hover (or focus) shows a
 *  styled tooltip with the resize details through the shared `InstantTip`
 *  (portal-rendered above the chip so the strip's overflow-x-auto can't clip
 *  it; see that module for the show/hide gesture semantics). */
function ResizeBadge({ resize }: { resize: ResizeInfo }) {
  // No click action here: the bubble is all a press shows, so a tap opens it.
  const { tip, tipHandlers, tipId } = useInstantTip({ openOnTap: true })
  return (
    <>
      {/* In flow under the thumbnail, not overlaid on it. The tile is a fixed
          64px square, while the widest catalog values need 105px (bn) and
          104px (de). Overlaid, that ends as one of two defects — an unbreakable
          Latin word spilling sideways onto the neighbouring chip, or a
          per-character-breaking script stacking down and covering the
          thumbnail. In flow, the chip is simply as wide as the wider of tile
          and pill, so each locale pays only its own width and the thumbnail is
          never covered in any of them. `whitespace-nowrap` is what makes the
          chip grow instead of the pill wrapping. */}
      <button
        type="button"
        aria-label={i18nT('components.chatInput.resized_to_fit_model_limits_2', { fromW: resize.fromW, fromH: resize.fromH, toW: resize.toW, toH: resize.toH })}
        className="px-1.5 py-[1px] rounded-full border-0 text-[10px] font-bold bg-accent text-accent-fg shadow-sm cursor-default whitespace-nowrap"
        {...tipHandlers}
      >{i18nT('components.chatInput.resized')}</button>
      <InstantTip tip={tip} tipId={tipId} className="w-max max-w-[calc(100vw-1rem)]">
        <div className="text-text">{i18nT('components.chatInput.resized_to_fit_model_limits')}</div>
        <div className="text-muted">{resize.fromW}×{resize.fromH} → {resize.toW}×{resize.toH}</div>
      </InstantTip>
    </>
  )
}

/** Stable default so an omitted `dirs` prop does not re-run the remeasure
 *  effect on every render (a fresh [] literal changes deps each time). */
const NO_DIRS: string[] = []

/** Staged attachments and folder references above the composer.
 *
 *  Every tile is a `role="group"` named by its FULL path. `title` shows that
 *  path on pointer hover only -- no browser opens a native tooltip on keyboard
 *  focus -- and a tile's visible text is the short label, so without the group
 *  name the path reaches nobody using assistive technology. A group's name IS
 *  announced when focus enters it, which is the reliable case and is what these
 *  tiles have: each one holds a focusable button.
 *
 *  The name also tells the per-tile controls apart: their labels are bare verbs
 *  ("Remove", "Remove folder"), so with several files staged a screen reader
 *  announces each one inside its own file's group instead of a row of identical
 *  buttons. */
export function FilePreviewStrip({ files, dirs = NO_DIRS, resizedInfo, onRemove, onRemoveDir, rootRef }: { files: string[]; dirs?: string[]; resizedInfo?: Record<string, ResizeInfo>; onRemove?: (path: string) => void; onRemoveDir?: (path: string) => void; rootRef?: (node: HTMLDivElement | null) => void }) {
  const [attachScroller, edges, remeasure] = useScrollEdges<HTMLDivElement>()
  // Chips are added and removed while the strip stays mounted (a paste, a
  // remove), and the scroller keeps its own box through those changes, so the
  // ResizeObserver never fires and no scroll event lands. Without this the cue
  // goes stale: dark over a row that now fits, or absent over one that clips.
  useEffect(() => { remeasure() }, [files, dirs, remeasure])
  const imgs = files.filter(p => IMG_EXT.test(p))
  const nonImgs = files.filter(p => !IMG_EXT.test(p))
  if (!imgs.length && !nonImgs.length && !dirs.length) return null
  return (
    // The wrapper exists for the edge cues: absolutely-positioned children of
    // the scroller itself would travel with the scrolled content, so the fades
    // anchor to a non-scrolling parent, same shape as the sibling strips.
    <div className="relative" ref={rootRef}>
      {/* items-start, not items-end: a chip carrying a resize pill is taller than a
          plain one, and bottom-alignment would spend that difference staggering the
          THUMBNAILS (the thing being compared) instead of letting the pills hang. */}
      <div ref={attachScroller} data-testid="preview-strip" className="flex gap-2 px-4 py-2 border-t border-border bg-chrome/50 overflow-x-auto items-start" data-image-scope="">
      {imgs.map((path, i) => {
        const src = `/api/file-raw?path=${encodeURIComponent(path)}`
        const resize = resizedInfo?.[path]
        return (
          <div key={path} role="group" aria-label={path} className="group/preview shrink-0 flex flex-col items-start gap-0.5" title={path}>
            {/* The corner controls anchor to the IMAGE, not to the chip: the chip
                is as wide as the wider of tile and resize pill, so a locale
                whose pill is wider than the 64px tile (de: 104px pill) would
                otherwise strand the remove button 40px out in the empty space
                beside the thumbnail it removes. */}
            <div className="relative">
            <span className="absolute -top-1.5 -left-1.5 w-5 h-5 rounded-full bg-accent text-accent-fg text-[10px] font-bold flex items-center justify-center z-10">{i + 1}</span>
            <button
              type="button"
              aria-label={i18nT('components.chatInput.open_preview_of', { name: path.split('/').pop() })}
              className="block cursor-pointer"
              onClick={(e) => { const img = e.currentTarget.querySelector('img'); if (img) dispatchLightbox(img) }}
            >
              {/* Fixed 64×64 square tile: every image chip is the same size, so
                  a phone screenshot (31px at intrinsic ratio) is as recognisable
                  as a landscape shot, and the strip's row stays uniform.
                  object-cover center-crops instead of letterboxing — the full
                  image is one click away in the lightbox, so the tile only has
                  to be identifiable, not complete. bg-bg-hover backs
                  transparent PNGs so the border reads as a tile rather than a
                  see-through frame. */}
              {/* The listener refreshes the scroll cue; the image is inside the
                  actual preview button and is not itself interactive. */}
              {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
              <img src={src} alt={path} className="w-16 h-16 rounded border border-border object-cover bg-bg-hover hover:opacity-80 transition-opacity"
                data-lightbox-image=""
                // The tile's box is fixed, but chips mount before their bytes
                // arrive and remove/add churns the strip's scrollWidth without
                // resizing the scroller's own box — no ResizeObserver fires and
                // no scroll lands, so this load signal still refreshes the cue.
                onLoad={remeasure} />
            </button>
            {onRemove && (
              <button
                aria-label={i18nT('components.chatInput.remove')}
                className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-danger text-white text-[12px] flex items-center justify-center opacity-0 group-hover/preview:opacity-100 transition-opacity cursor-pointer"
                onClick={() => onRemove(path)} title={i18nT('components.chatInput.remove')}
              ><X className="lucide-inline" /></button>
            )}
            </div>
            {resize && <ResizeBadge resize={resize} />}
          </div>
        )
      })}
      {nonImgs.map(path => (
        <div key={path} role="group" aria-label={path} title={path} className="relative group/preview shrink-0 flex items-center gap-1.5 px-2 py-1 rounded border border-border bg-bg-hover text-[12px] text-text">
          <span>{path.split('/').pop()}</span>
          {onRemove && (
            <button className="text-muted hover:text-danger cursor-pointer bg-transparent border-none p-0" onClick={() => onRemove(path)} title={i18nT('components.chatInput.remove')} aria-label={i18nT('components.chatInput.remove')}><X size={12} /></button>
          )}
        </div>
      ))}
      {/* Folder references: a path handed to the agent, not an upload. No
          /api/file-raw thumbnail is fetched — there is no content to preview.
          Labels are basename-first and widen by parent segments on collision
          (shared buildFileLabels rule), so two staged `pages/` folders from
          different parents stay tellable apart. */}
      {(() => {
        // buildFileLabels splits on `/` only, so normalize Windows separators
        // for label computation; keys and tooltips keep the original rel.
        const normDir = (d: string) => d.replace(/\\/g, '/').replace(/\/+$/, '')
        const dirLabels = buildFileLabels(dirs.map(normDir))
        return dirs.map(path => (
        <div
          key={path}
          data-dir-chip=""
          role="group"
          aria-label={path}
          title={path}
          className="relative group/preview shrink-0 flex items-center gap-1.5 px-2 py-1 rounded border border-border bg-bg-hover text-[12px] text-text"
        >
          <Folder size={12} aria-label={i18nT('components.filePickerMenu.folder')} className="shrink-0 lucide-inline" />
          <span>{(dirLabels.get(normDir(path)) || path) + '/'}</span>
          {onRemoveDir && (
            <button aria-label={i18nT('components.filePickerMenu.remove_folder')} className="text-muted hover:text-danger cursor-pointer bg-transparent border-none p-0" onClick={() => onRemoveDir(path)} title={i18nT('components.filePickerMenu.remove_folder')}><X size={12} /></button>
          )}
        </div>
        ))
      })()}
      </div>
      {/* Edge cues, same treatment as the sibling strips (SidePanelLayout's
          tab strip, FollowUpBar's scroll row): a gradient says content
          continues past the clipped edge, because the overlay scrollbar on
          macOS/iOS leaves no visible sign while idle. from-bg-elevated matches
          the composer surface the strip sits on. z-10 keeps the fade above the
          chips' own z-10 badges; pointer-events-none keeps those interactive. */}
      {edges.left && (
        <div aria-hidden="true" data-testid="preview-strip-cue-left" className="pointer-events-none absolute left-0 top-px bottom-0 w-6 z-10 bg-gradient-to-r from-bg-elevated to-transparent" />
      )}
      {edges.right && (
        <div aria-hidden="true" data-testid="preview-strip-cue-right" className="pointer-events-none absolute right-0 top-px bottom-0 w-6 z-10 bg-gradient-to-l from-bg-elevated to-transparent" />
      )}
    </div>
  )
}
