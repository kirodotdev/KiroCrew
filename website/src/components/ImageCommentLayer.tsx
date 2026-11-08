import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { OverlayPortal, useLocalCrewHidden } from './overlayRoot'
import { MessageSquarePlus } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { fmtNumber, fmtPercent } from '../i18n/format'
import { dispatchLightbox } from './markdown/Lightbox'
import { WHOLE_IMAGE, boxPositionStyle, isWholeImage, dragToImageBox, pictureRectOf, type ImageBox, type ImageBoxRect, type ImageCommentHost } from '../utils/imageComments'

interface Box { left: number; top: number; width: number; height: number }

/** How far the pointer moves before a press on the image becomes a drag. Below
 *  it the press is a click, which opens the lightbox (or, on a box, reveals its
 *  comment) as it always has. */
export const DRAG_THRESHOLD_PX = 4
/** The smallest box a drag leaves, on each side, in screen pixels. A narrower
 *  sliver is a slip, not a region, and is dropped. */
export const MIN_BOX_PX = 6
/** How long a release's click is swallowed: the click lands in the same task
 *  as the release, so the next tick is enough. */
export const RELEASE_CLICK_MS = 0

/** A box's accessible name. */
function boxLabel(box: ImageBox): string {
  return i18nT('components.imageComments.box_label', { number: fmtNumber(box.number), x: fmtPercent(box.x), y: fmtPercent(box.y) })
}

/**
 * Where a composer opened without a pointer hangs, in viewport coordinates:
 * at the middle of the box's bottom edge, so it opens below the picture rather
 * than over it. The keyboard control and a restored draft both use it. A bottom
 * past the window is held inside it, where the composer flips up into view.
 */
function belowBox(rect: { left: number; top: number; width: number; height: number }, b: ImageBoxRect): [number, number] {
  const bottom = rect.top + (b.y + b.h) * rect.height
  return [rect.left + (b.x + b.w / 2) * rect.width, Math.max(0, Math.min(bottom, window.innerHeight))]
}

/**
 * Box comment overlay for one rendered markdown image: the numbered boxes of
 * its existing comments, and the drag that draws a new one.
 *
 * It works the way commenting on text does. Dragging across the image draws a
 * box, and letting go opens the comment composer there; a drag that starts on
 * an existing box draws a new box too. A plain click still opens the lightbox,
 * and a click on a box reveals that box's comment in the pending list, as a
 * click on highlighted text does. There is no button on the image: the only
 * control, for readers who cannot drag, appears when the image gets keyboard
 * focus and comments on the whole image. Touch keeps its scrolling, so a touch
 * press never draws.
 *
 * The overlay sits over the image as an absolutely positioned sibling sized to
 * the image's box, so boxes positioned by percentage stay on their region as
 * the image resizes. It renders no text nodes (a box's number is CSS generated
 * content): the preview's find, selection offsets and comment highlights all
 * walk the text of this subtree, and a digit here would shift every offset
 * after the image.
 */
export function ImageCommentLayer({ host, imgRef, src, path, alt, line, column }: {
  host: ImageCommentHost
  imgRef: React.RefObject<HTMLImageElement | null>
  /** The image destination exactly as written in the markdown. */
  src: string
  /** The file the preview resolved `src` to and loaded, decoded, for the agent
   *  payload. Falls back to `src` when absent. */
  path?: string
  alt?: string
  line?: number
  column?: number
}) {
  const [box, setBox] = useState<Box | null>(null)
  /** The box under a drag in progress, as fractions of the image. */
  const [live, setLive] = useState<ImageBoxRect | null>(null)
  const layerRef = useRef<HTMLSpanElement>(null)
  const keyboardRef = useRef<HTMLButtonElement>(null)
  const placedFromKeyboardRef = useRef(false)

  // The placement callback changes with every host render; the drag listeners
  // are bound once per image and read it through a ref.
  const placeRef = useRef<(rect: ImageBoxRect, clientX: number, clientY: number) => void>(() => {})
  useEffect(() => {
    placeRef.current = (rect, clientX, clientY) => {
      host.onPlace({ src, ...(path ? { path } : {}), alt, line, column, ...rect, clientX, clientY })
    }
  }, [host, src, path, alt, line, column])
  // The hover popover, read the same way; and whether a drag is under way, when
  // a box under the pointer is being drawn over rather than read.
  const hoverRef = useRef(host.onHover)
  useEffect(() => { hoverRef.current = host.onHover }, [host])
  const draggingRef = useRef(false)
  // While a box has keyboard focus its comment is shown beside it, and a scroll
  // or resize moves the box out from under a popover placed once, so the
  // popover follows it until the box loses focus or the layer goes.
  const unfollowRef = useRef<(() => void) | null>(null)
  const followFocusedBox = useCallback((el: HTMLElement, text: string) => {
    unfollowRef.current?.()
    const show = () => {
      // Not while a drag draws a new box across it, as for the pointer.
      if (draggingRef.current) return
      const r = el.getBoundingClientRect()
      hoverRef.current(text, r.left + r.width / 2, r.top + r.height / 2, { left: r.left, top: r.top, width: r.width, height: r.height })
    }
    show()
    window.addEventListener('scroll', show, { capture: true, passive: true })
    window.addEventListener('resize', show)
    unfollowRef.current = () => {
      window.removeEventListener('scroll', show, { capture: true })
      window.removeEventListener('resize', show)
      unfollowRef.current = null
    }
  }, [])
  useEffect(() => () => unfollowRef.current?.(), [])

  // The layer mirrors the picture's box in the wrapper's coordinates: the
  // image's box, less any band object-contain leaves around the picture. The
  // wrapper is the image's offsetParent, so offset* are already in the layer's
  // space, and scrolling moves the wrapper and the layer together. The image moves
  // inside the wrapper when the wrapper is resized (an end-aligned image slides
  // with its edge), so the wrapper is observed along with the image. A move
  // that resizes neither is not reported by ResizeObserver and is not handled.
  useLayoutEffect(() => {
    const img = imgRef.current
    if (!img) return
    const measure = () => {
      const next = pictureRectOf(img, true)
      setBox(prev => (prev && prev.left === next.left && prev.top === next.top && prev.width === next.width && prev.height === next.height ? prev : next))
    }
    measure()
    img.addEventListener('load', measure)
    const wrapper = img.offsetParent ?? img.parentElement
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    if (ro) {
      ro.observe(img)
      if (wrapper) ro.observe(wrapper)
    } else {
      window.addEventListener('resize', measure)
    }
    return () => {
      img.removeEventListener('load', measure)
      if (ro) ro.disconnect()
      else window.removeEventListener('resize', measure)
    }
  }, [imgRef])

  // Ends the drag in flight, if any. A Crew switch mid-drag ends it: the hidden
  // pane stops painting, not listening, so its window Escape listener would
  // otherwise claim a key pressed on the Crew in front.
  const endDragRef = useRef<() => void>(() => {})
  const crewHidden = useLocalCrewHidden()
  useEffect(() => { if (crewHidden) endDragRef.current() }, [crewHidden])

  // The drag. Bound natively on the wrapper that holds both the image and this
  // layer, so a press on the image or on one of its boxes starts it. Movement
  // and release are read off the window, so a drag that leaves the image still
  // ends, clamped to its edge.
  useEffect(() => {
    const img = imgRef.current
    const wrapper = img?.parentElement
    if (!img || !wrapper) return
    /** Drag state: where it started, as fractions of the image so a scroll
     *  mid-drag cannot shift it, and whether it has passed the threshold. */
    let drag: { pointerId: number; startX: number; startY: number; fx: number; fy: number; moved: boolean } | null = null
    let swallowTimer: number | null = null

    const startsDrag = (t: EventTarget | null) => t === img
      || (t instanceof Element && !!layerRef.current?.contains(t) && !!t.closest('[data-image-box]'))
    const current = (clientX: number, clientY: number) => {
      const rect = pictureRectOf(img)
      const from = { x: rect.left + drag!.fx * rect.width, y: rect.top + drag!.fy * rect.height }
      return { rect, next: dragToImageBox(from, { x: clientX, y: clientY }, rect) }
    }
    // A drag ends in a mouseup over the image or a box, and the browser follows
    // it with a click: that click must not open the lightbox or reveal a box.
    const swallowClick = (e: MouseEvent) => {
      e.preventDefault()
      e.stopPropagation()
      stopSwallowing()
    }
    const stopSwallowing = () => {
      window.removeEventListener('click', swallowClick, true)
      if (swallowTimer != null) { window.clearTimeout(swallowTimer); swallowTimer = null }
    }
    const swallowNextClick = () => {
      stopSwallowing()
      window.addEventListener('click', swallowClick, true)
      // The click lands in the same task as the release; a release elsewhere
      // has no click, so the listener must not outlive it.
      swallowTimer = window.setTimeout(stopSwallowing, RELEASE_CLICK_MS)
    }
    // A drag dropped by Escape still has its button held, and the click its
    // release makes is the drag's, not a click on the image: swallowed when
    // that pointer is released, however long after the Escape.
    let awaitRelease: ((e: PointerEvent) => void) | null = null
    const stopAwaitingRelease = () => {
      if (!awaitRelease) return
      window.removeEventListener('pointerup', awaitRelease, true)
      window.removeEventListener('pointercancel', awaitRelease, true)
      awaitRelease = null
    }
    const swallowClickOnRelease = (pointerId: number) => {
      stopAwaitingRelease()
      awaitRelease = (e: PointerEvent) => {
        if (e.pointerId !== pointerId) return
        stopAwaitingRelease()
        if (e.type === 'pointerup') swallowNextClick()
      }
      window.addEventListener('pointerup', awaitRelease, true)
      window.addEventListener('pointercancel', awaitRelease, true)
    }
    // The image keeps its pointer cursor, the cue that a click enlarges it,
    // until a press passes the drag threshold. From then until the drag ends
    // the page shows a crosshair, over the image and off it.
    const drawingCursor = (on: boolean) => {
      img.style.cursor = on ? 'crosshair' : ''
      document.body.style.cursor = on ? 'crosshair' : ''
    }
    const end = () => {
      if (drag?.moved) drawingCursor(false)
      drag = null
      draggingRef.current = false
      setLive(null)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onCancel)
      window.removeEventListener('keydown', onKey, true)
    }
    endDragRef.current = end
    const onMove = (e: PointerEvent) => {
      if (!drag || e.pointerId !== drag.pointerId) return
      if (!drag.moved) {
        if (Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) < DRAG_THRESHOLD_PX) return
        drag.moved = true
        drawingCursor(true)
      }
      setLive(current(e.clientX, e.clientY).next)
    }
    const onUp = (e: PointerEvent) => {
      if (!drag || e.pointerId !== drag.pointerId) return
      if (!drag.moved) { end(); return }
      const { rect, next } = current(e.clientX, e.clientY)
      end()
      swallowNextClick()
      if (!next || next.w * rect.width < MIN_BOX_PX || next.h * rect.height < MIN_BOX_PX) return
      placedFromKeyboardRef.current = false
      placeRef.current(next, e.clientX, e.clientY)
    }
    const onCancel = (e: PointerEvent) => {
      if (drag && e.pointerId === drag.pointerId) end()
    }
    // Escape drops the box being drawn. Claimed in the capture phase, before the
    // panel's own document-level Escape, which would otherwise close the file.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing || !drag) return
      e.preventDefault()
      e.stopPropagation()
      const { moved, pointerId } = drag
      end()
      if (moved) swallowClickOnRelease(pointerId)
    }
    const onDown = (e: PointerEvent) => {
      if (drag || e.button !== 0 || e.pointerType === 'touch' || !startsDrag(e.target)) return
      const rect = pictureRectOf(img)
      if (!(rect.width > 0) || !(rect.height > 0)) return
      const clamp = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v)
      drag = {
        pointerId: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        fx: clamp((e.clientX - rect.left) / rect.width),
        fy: clamp((e.clientY - rect.top) / rect.height),
        moved: false,
      }
      draggingRef.current = true
      hoverRef.current(null, e.clientX, e.clientY)
      window.addEventListener('pointermove', onMove)
      window.addEventListener('pointerup', onUp)
      window.addEventListener('pointercancel', onCancel)
      window.addEventListener('keydown', onKey, true)
    }
    // The press's defaults, a text selection and the browser dragging the image
    // out as a file, belong to the mousedown. Refused there rather than on the
    // pointerdown: a refused pointerdown suppresses the mousedown itself, which
    // every press-outside-to-close handler in the app listens for.
    const onMouseDown = (e: MouseEvent) => {
      if (e.button === 0 && startsDrag(e.target)) e.preventDefault()
    }
    const onDragStart = (e: DragEvent) => {
      if (startsDrag(e.target)) e.preventDefault()
    }
    wrapper.addEventListener('pointerdown', onDown)
    wrapper.addEventListener('mousedown', onMouseDown)
    wrapper.addEventListener('dragstart', onDragStart)
    return () => {
      wrapper.removeEventListener('pointerdown', onDown)
      wrapper.removeEventListener('mousedown', onMouseDown)
      wrapper.removeEventListener('dragstart', onDragStart)
      end()
      stopSwallowing()
      stopAwaitingRelease()
    }
  }, [imgRef])

  // Closing the composer this image's box opened (Escape, the close button, or
  // adding the comment) unmounts the input that held focus. When the box came
  // from the keyboard control, hand focus back to it rather than lose it to the
  // page; a box drawn with the pointer leaves focus where the reader left it.
  const ownsDraft = !!host.draft && host.ownsDraft(src, line, column)
  const ownedDraftRef = useRef(false)
  useEffect(() => {
    const had = ownedDraftRef.current
    ownedDraftRef.current = ownsDraft
    if (!had || ownsDraft || !placedFromKeyboardRef.current) return
    const active = document.activeElement
    if (active && active !== document.body) return
    keyboardRef.current?.focus({ preventScroll: true })
  }, [ownsDraft])

  const descIdBase = useId()

  // A draft restored after the panel was torn down opens its composer where
  // the keyboard control opens one: below the box (see belowBox).
  const unplacedDraft = ownsDraft && host.draft?.unplaced ? host.draft : null
  const placeDraft = host.placeDraft
  useEffect(() => {
    if (!unplacedDraft || !placeDraft) return
    const rect = imgRef.current ? pictureRectOf(imgRef.current) : undefined
    if (!rect || !(rect.width > 0) || !(rect.height > 0)) return
    placeDraft(...belowBox(rect, unplacedDraft))
  }, [unplacedDraft, placeDraft, imgRef, box])

  const commentOnWholeImage = useCallback((e: React.MouseEvent<HTMLButtonElement>) => {
    e.preventDefault()
    e.stopPropagation()
    const rect = imgRef.current ? pictureRectOf(imgRef.current) : undefined
    if (!rect || !(rect.width > 0) || !(rect.height > 0)) return
    placedFromKeyboardRef.current = true
    placeRef.current(WHOLE_IMAGE, ...belowBox(rect, WHOLE_IMAGE))
  }, [imgRef])

  if (!box || box.width <= 0 || box.height <= 0) return null
  const boxes = host.boxesFor(src, line, column)
  const draft = ownsDraft ? host.draft : null
  const wholeImageLabel = i18nT('components.imageComments.comment_on_image')

  return (
    <span
      ref={layerRef}
      data-image-comment-layer=""
      className="pointer-events-none absolute z-[1]"
      style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
    >
      {boxes.map(b => (
        <button
          key={b.id}
          type="button"
          data-image-box=""
          data-box-number={b.number}
          data-box-label={fmtNumber(b.number)}
          aria-label={boxLabel(b)}
          aria-describedby={`${descIdBase}-${b.id}`}
          style={boxPositionStyle(b)}
          className={`pointer-events-auto absolute ${live ? 'cursor-crosshair' : 'cursor-pointer'} rounded-sm border-2 border-accent bg-accent/10 transition-colors hover:bg-accent/20 focus-ring before:absolute before:-left-2.5 before:-top-2.5 before:flex before:size-5 before:items-center before:justify-center before:rounded-full before:border-2 before:border-bg before:bg-accent before:text-[10px] before:font-semibold before:leading-none before:text-accent-fg before:shadow-md before:content-[attr(data-box-label)]`}
          // The comment shows beside the box, so it doesn't cover the region
          // it's about; not while a drag draws a new box across this one.
          onPointerMove={e => {
            if (e.pointerType === 'touch' || draggingRef.current) return
            const r = e.currentTarget.getBoundingClientRect()
            host.onHover(b.text, e.clientX, e.clientY, { left: r.left, top: r.top, width: r.width, height: r.height })
          }}
          onPointerLeave={e => host.onHover(null, e.clientX, e.clientY)}
          // A keyboard reader sees the comment the same way, beside the box.
          // A click focuses too, and the pointer already shows it there.
          onFocus={e => {
            if (!e.currentTarget.matches(':focus-visible')) return
            followFocusedBox(e.currentTarget, b.text)
          }}
          onBlur={e => {
            if (!unfollowRef.current) return
            unfollowRef.current()
            const r = e.currentTarget.getBoundingClientRect()
            host.onHover(null, r.left + r.width / 2, r.top + r.height / 2)
          }}
          onClick={e => {
            e.preventDefault()
            e.stopPropagation()
            // A whole-image box covers the picture, so a pointer click on it
            // still enlarges the image, as a click outside a smaller box does.
            // Keyboard activation (no click count) reveals its comment.
            if (e.detail > 0 && isWholeImage(b) && imgRef.current) { dispatchLightbox(imgRef.current); return }
            host.onActivate(b.id)
          }}
        />
      ))}
      {/* Each box's comment is its description. aria-description isn't
          supported on a button, and text in the layer would shift find and
          selection offsets, so the text sits hidden outside the document. */}
      {boxes.length > 0 && typeof document !== 'undefined' && (
        <OverlayPortal>
          <div hidden data-image-box-descriptions="">
            {boxes.map(b => <span key={b.id} id={`${descIdBase}-${b.id}`}>{b.text}</span>)}
          </div>
        </OverlayPortal>
      )}
      {draft && (
        <span
          aria-hidden="true"
          data-image-box-draft=""
          style={boxPositionStyle(draft)}
          className="absolute animate-pulse rounded-sm border-2 border-accent bg-accent/20 motion-reduce:animate-none"
        />
      )}
      {live && (
        <span
          aria-hidden="true"
          data-image-box-live=""
          style={boxPositionStyle(live)}
          className="absolute rounded-sm border-2 border-dashed border-accent bg-accent/10"
        />
      )}
      {/* For readers who cannot drag: invisible and out of the pointer's way
          until it has keyboard focus, so the image itself carries no button.
          Its words are CSS generated content from data-label, like a box's
          number, so the layer still adds no text to the document. */}
      <button
        ref={keyboardRef}
        type="button"
        aria-label={wholeImageLabel}
        data-label={wholeImageLabel}
        onClick={commentOnWholeImage}
        className="pointer-events-none absolute end-1.5 top-1.5 inline-flex items-center gap-1 rounded-md border border-border bg-card px-1.5 py-1 text-xs text-text opacity-0 shadow-sm focus-ring focus-visible:pointer-events-auto focus-visible:opacity-100 after:content-[attr(data-label)]"
      >
        <MessageSquarePlus size={14} aria-hidden="true" />
      </button>
    </span>
  )
}
