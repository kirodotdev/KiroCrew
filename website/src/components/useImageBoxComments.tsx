import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type Dispatch, type RefObject, type SetStateAction } from 'react'
import type { InlineComment } from './CommentOverlay'
import { ImageCommentComposer } from './ImageCommentComposer'
import { composerDraftStoreFor } from '../utils/composerDraftStore'
import { IMAGE_BOX_DRAFT_SLOT, boxBelongsToImage, boxRectOf, imageAnchorOf, imageDraftKeyOf, imageUseAt, parseImageBoxDraft, serializeImageBoxDraft, type ImageBoxPlacement, type ImageCommentHost, type ImageUse, type PlainRect } from '../utils/imageComments'

/**
 * Image box comments in the file viewer.
 *
 * A box drawn on a rendered image opens the shared comment box where the drag
 * ended, and submitting adds an ordinary pending comment that carries `image`,
 * so it lists, persists and submits with the text comments. A click on a box
 * flashes its row, as a click on highlighted text does.
 *
 * Two hooks, because `MarkdownPanel`'s draft guards, declared early, must see
 * and discard the box draft before the comment tooltips and the discard
 * confirm that the host uses exist. `useImageBoxDraft` holds the draft;
 * `useImageBoxHost` builds the host the images read and the composer.
 */

/** The image box being commented on, and the file and view it was drawn in. */
type BoxDraft = ImageBoxPlacement & Partial<ImageUse> & { filePath: string; fullscreen: boolean }

/** An image rendered now, by where it sits in the source (see `ImageCommentHost.register`). */
type ImageSpot = { src: string; line?: number; column?: number }

/**
 * The box draft and the text typed into its box. The text lives here, keyed by
 * the box, so a composer remount (the tab suspended, a view switch and back)
 * brings it back, and while it holds anything the tab is not clean, exactly as
 * for a text draft. One box per file is kept in the draft store as its
 * placement and typed text, so a slot switch brings the box and its text back.
 */
export function useImageBoxDraft(filePath: string, readOnly: boolean) {
  const store = useMemo(() => composerDraftStoreFor(`mc-image-box-draft:${filePath}`), [filePath])
  const [draft, setDraft] = useState<BoxDraft | null>(null)
  const textRef = useRef<{ key: string; text: string } | null>(null)
  // Whether this box's comment box is on screen. The host keeps it current
  // while the box is in front, and it keeps its last value while the box is
  // hidden (a background tab, the diff view). A box that came back with the
  // panel and has no comment box yet, because its image hasn't loaded, was
  // edited away or fails to load, guards nothing: no prompt on close and no
  // refused drag, as a text draft saved for a passage that isn't selected
  // guards nothing. Its image placing it, once loaded, makes it guard again.
  const showableRef = useRef(false)
  const hasText = useCallback(() => showableRef.current && !!textRef.current?.text.trim(), [])
  const discard = useCallback(() => {
    textRef.current = null
    showableRef.current = false
    // A read-only panel never touches the per-path store: the box saved there
    // belongs to the file tab that drew it.
    if (!readOnly) store.clear(IMAGE_BOX_DRAFT_SLOT, 0)
    setDraft(null)
  }, [store, readOnly])
  // A box with typed text that the panel was torn down under (a chat-slot
  // switch) comes back on the next mount, its composer placed by its image.
  // A read-only diff tab for the same path shows no composer, so it leaves it.
  // When the tab re-targets to another file in place, the old file's box is
  // left in that file's store and the new file's own box, if any, comes back.
  // A layout effect, so the old box stops guarding in the commit that changes
  // the file, before anything can ask the new file's tab to close.
  useLayoutEffect(() => {
    if (readOnly) return
    const saved = parseImageBoxDraft(store.read(IMAGE_BOX_DRAFT_SLOT, 0))
    setDraft(prev => {
      if (prev && prev.filePath === filePath) return prev
      textRef.current = null
      showableRef.current = false
      if (!saved) return null
      const { text, ...placement } = saved
      const restored = { ...placement, clientX: Number.NaN, clientY: Number.NaN, filePath, fullscreen: false }
      textRef.current = { key: imageDraftKeyOf(restored), text }
      return restored
    })
  }, [store, filePath, readOnly])
  return { store, draft, setDraft, textRef, showableRef, hasText, discard }
}

export type ImageBoxDraftState = ReturnType<typeof useImageBoxDraft>

export interface ImageBoxHostOptions {
  /** The panel can submit comments; without it no image is commentable. */
  enabled: boolean
  filePath: string
  fullscreen: boolean
  /** The tab is in front and shows the rendered document, not the editor. */
  shown: boolean
  diffViewShown: boolean
  comments: InlineComment[]
  setComments: Dispatch<SetStateAction<InlineComment[]>>
  scrollRef: RefObject<HTMLDivElement | null>
  confirmDiscard: () => Promise<boolean>
  flashRow: (row: HTMLElement) => void
  showTooltip: (text: string, clientX: number, clientY: number, box?: PlainRect) => void
  hideTooltip: () => void
}

/** The host the rendered images read, and the composer for the active draft. */
export function useImageBoxHost(box: ImageBoxDraftState, o: ImageBoxHostOptions) {
  const { draft, setDraft, textRef, showableRef, hasText, discard, store } = box
  const { enabled, filePath, fullscreen, shown, diffViewShown, comments, setComments, scrollRef, confirmDiscard, flashRow, showTooltip, hideTooltip } = o
  // Bumped when a drag is refused because the box on screen holds typed text,
  // so that box is brought back to the reader instead of the drag vanishing.
  const [reveal, setReveal] = useState(0)
  // Every image rendered now, by where it sits in the source, loaded or not. A
  // saved box finds its image by `src` among these, the way a text comment finds
  // its passage by its text, so an edit above the image does not detach it.
  const [imageSpots, setImageSpots] = useState<readonly ImageSpot[]>([])
  const registerImageSpot = useCallback((src: string, line: number | undefined, column: number | undefined) => {
    const entry = { src, line, column }
    setImageSpots(prev => [...prev, entry])
    return () => setImageSpots(prev => prev.filter(e => e !== entry))
  }, [])
  // The draft remembers the file and view it was drawn in and goes inert when
  // either changes, the editor or the diff view opens, or the tab goes to the
  // background (the composer is portaled, so it would otherwise float over the
  // tab in front), since the image it points at is no longer on screen.
  const active = draft && shown && draft.filePath === filePath && draft.fullscreen === fullscreen && !diffViewShown ? draft : null
  // The diff view unmounts the image, and a background tab unmounts the
  // composer while the panel keeps its scroll, so a box draft comes back from
  // either the way it comes back after a slot switch: unplaced, and placed
  // again by its image, rather than at a spot on screen that may have moved.
  useEffect(() => {
    if (diffViewShown || !shown) setDraft(d => (d && Number.isFinite(d.clientX) ? { ...d, clientX: Number.NaN, clientY: Number.NaN } : d))
  }, [diffViewShown, shown, setDraft])
  // While the box is in front, it guards exactly while its comment box is
  // placed on screen; while it is hidden it keeps what it was. A new box
  // re-reads it (`discard` cleared it), so `active` is a dependency too.
  const showable = active ? Number.isFinite(active.clientX) : null
  useLayoutEffect(() => {
    if (showable != null) showableRef.current = showable
  }, [showable, active, showableRef])
  const host: ImageCommentHost | null = useMemo(() => enabled ? {
    boxesFor: (src, line, column) => {
      const spots = imageSpots.filter(s => s.src === src)
      return comments.flatMap((c, i) => (c.image && boxBelongsToImage({ src: c.image.src, line: c.line, column: c.column, nth: c.image.nth, uses: c.image.uses }, src, { line, column }, spots)
        ? [{ id: c.id, number: i + 1, ...boxRectOf(c.image), text: c.text }]
        : []))
    },
    register: registerImageSpot,
    draft: active && !Number.isFinite(active.clientX) ? { ...active, unplaced: true } : active,
    ownsDraft: (src, line, column) => {
      const d = active
      return !!d && boxBelongsToImage({ src: d.src, line: d.line, column: d.column, nth: d.nth, uses: d.uses }, src, { line, column }, imageSpots.filter(s => s.src === src))
    },
    placeDraft: (clientX, clientY) => setDraft(d => (d && !Number.isFinite(d.clientX) ? { ...d, clientX, clientY } : d)),
    onPlace: placement => {
      // A typed comment keeps the box it was written for, as a typed text
      // comment keeps its passage: a new drag does not re-target it. The box
      // that kept it is brought back on screen and focused, so the drag
      // doesn't just vanish. A box with no comment box on screen (see
      // `showableRef`) holds nothing, and the new box replaces it along with
      // whatever was typed in it.
      if (hasText()) { setReveal(n => n + 1); return }
      discard()
      const use = imageUseAt({ line: placement.line, column: placement.column }, imageSpots.filter(s => s.src === placement.src))
      setDraft({ ...placement, ...(use ?? {}), filePath, fullscreen })
    },
    onActivate: id => {
      hideTooltip()
      const row = document.querySelector(`[data-comment-id="${id}"]`) as HTMLElement | null
      if (row) flashRow(row)
    },
    onHover: (text, clientX, clientY, rect) => {
      if (text == null) hideTooltip()
      else showTooltip(text, clientX, clientY, rect)
    },
  } : null, [enabled, comments, imageSpots, registerImageSpot, active, setDraft, hasText, discard, filePath, fullscreen, flashRow, hideTooltip, showTooltip])
  const submit = useCallback((text: string) => {
    const d = active
    if (!d) return
    const newComment: InlineComment = {
      id: Math.random().toString(36).substring(2),
      anchor: imageAnchorOf(d.alt, d.src),
      text,
      line: d.line,
      column: d.column,
      image: { src: d.src, ...(d.path ? { path: d.path } : {}), ...boxRectOf(d), ...(d.nth != null && d.uses != null ? { nth: d.nth, uses: d.uses } : {}) },
    }
    setComments(prev => [...prev, newComment])
    discard()
  }, [active, discard, setComments])
  // A new box is a new comment: its key remounts the composer, and only text
  // kept for THIS box comes back into it.
  const draftKey = active ? imageDraftKeyOf(active) : null
  const keepText = useCallback((text: string) => {
    if (!draftKey || !active) return
    textRef.current = { key: draftKey, text }
    if (text.trim()) store.write(serializeImageBoxDraft(active, text), IMAGE_BOX_DRAFT_SLOT, 0)
    else store.clear(IMAGE_BOX_DRAFT_SLOT, 0)
  }, [draftKey, active, textRef, store])
  const popover = active && draftKey && Number.isFinite(active.clientX) ? (
    <ImageCommentComposer
      key={draftKey}
      x={active.clientX}
      y={active.clientY}
      scrollRef={scrollRef}
      onSubmit={submit}
      onCancel={discard}
      initialText={textRef.current?.key === draftKey ? textRef.current.text : ''}
      onTextChange={keepText}
      confirmDiscard={confirmDiscard}
      reveal={reveal}
    />
  ) : null
  return { host, popover }
}
