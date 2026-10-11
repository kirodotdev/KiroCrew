import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { SelectionComposer } from '../components/SelectionToolbar'
import { composerDraftStoreFor } from '../utils/composerDraftStore'
import { clearAnnotationHighlight } from '../utils/annotationHighlight'

/** The in-iframe selection handed to the toolbar as `externalSelection`. */
export interface ExternalSelectionState { text: string; x: number; y: number; start?: number }

/**
 * The one composer wiring every artifact host shares, parameterized by what
 * actually differs between them: how the live DOM selection becomes an anchor,
 * and what a submit does with it.
 *
 * Owns, for the host:
 * - the PENDING anchor (written only in `onOpen`, when the toolbar has just
 *   accepted THIS selection) and the STAGED bridge anchor (a selection inside
 *   an iframe body, promoted to pending only if the toolbar opens for that same
 *   text — the toolbar refuses to re-target a box holding a typed draft, so
 *   writing pending straight from the bridge would submit that draft against
 *   the passage selected AFTER it was typed);
 * - the `externalSelection` the toolbar opens the box from for iframe bodies;
 * - the stand-in highlight token (focus in the input collapses the real
 *   selection; the host's resolver paints under this owner, the wiring clears);
 * - the draft mirror (`onDraftChange`), the per-host durable draft store, and
 *   the guard the host runs before its own actions would unmount the toolbar.
 *
 * `A` is the host's anchor record: `quoteOf` reads the selected text back out
 * of it (the bridge-promotion match) and `quoteOnly` builds the fallback when
 * neither a live range nor a matching staged anchor exists.
 */
/** Longest passage a one-line notice quotes back; longer ones are clipped with an ellipsis. */
const NOTICE_QUOTE_MAX = 48
const clipQuote = (quote: string): string => {
  const flat = quote.replace(/\s+/g, ' ').trim()
  return flat.length > NOTICE_QUOTE_MAX ? `${flat.slice(0, NOTICE_QUOTE_MAX - 1)}…` : flat
}

export function useSelectionComposerAnchor<A>({
  resolveDomAnchor, quoteOf, quoteOnly, submit, draftKey, confirmDiscard, onRefusedAfterClose,
}: {
  /** The live DOM selection as an anchor, or null when there is none the host
   *  accepts. Runs while the selection is still live; paint the stand-in
   *  highlight under `highlightOwner` here. */
  resolveDomAnchor: (highlightOwner: object) => A | null
  quoteOf: (anchor: A) => string
  quoteOnly: (text: string) => A
  /** The typed comment plus the anchor it annotates. A host whose store can
   *  refuse returns a promise resolving to whether the comment was stored; the
   *  wiring clears the selection state and the highlight only on success, so a
   *  retry posts against the same anchor. A void return clears at once. */
  submit: (comment: string, anchor: A) => void | Promise<boolean>
  /** Storage key for the durable draft (per artifact; per passage inside). */
  draftKey: string
  /** Asked before a typed draft is discarded; resolve `true` to discard. Omit
   *  to discard without asking. */
  confirmDiscard?: () => Promise<boolean>
  /** A post the store refused AFTER its box was closed (Escape/✕ mid-flight, a
   *  guarded transition): the box cannot show the refusal, so the host does,
   *  on its own failure surface. The draft is still in the store for the next
   *  open over the same passage. */
  /** Receives the annotated passage (clipped for a one-line notice), so the
   *  notice can name the text to select again instead of "the same text". */
  onRefusedAfterClose?: (quote: string) => void
}): {
  /** Pass to the `SelectionToolbar` as `composer`. */
  selectionComposer: SelectionComposer
  /** Pass to the same toolbar as `externalSelection`. */
  iframeSelection: ExternalSelectionState | null
  /** A selection inside an iframe body: stage its anchor and ask the toolbar
   *  to open the box at the supplied rect. */
  stageIframeSelection: (anchor: A, sel: ExternalSelectionState) => void
  /** Drop every pending/staged anchor, the external selection and the paint —
   *  what a host does when its body stops being commentable (edit mode, a
   *  historical version). */
  clearSelectionState: () => void
  /** Whether the box is open (between `onOpen` and its close/submit). A host
   *  with a document-level Escape handler must stand down while it is: the
   *  toolbar closes the box on Escape itself, and the box need not hold focus
   *  (a touch or Shift+Arrow open leaves the caret elsewhere). */
  isComposerOpen: () => boolean
  /** Run `proceed` unless an unsaved draft would be lost, in which case ask
   *  first (via `confirmDiscard`); a confirmed discard clears the slot too.
   *  Without `confirmDiscard` the draft is dropped without asking. */
  guardCommentDraft: (proceed: () => void) => Promise<void>
  /** Whether the composer box currently holds unsaved text. */
  hasCommentDraft: () => boolean
} {
  const pendingAnchorRef = useRef<A | null>(null)
  const stagedIframeAnchorRef = useRef<A | null>(null)
  const [iframeSelection, setIframeSelection] = useState<ExternalSelectionState | null>(null)
  const highlightOwnerRef = useRef<object>({})
  const composerOpenRef = useRef(false)
  const isComposerOpen = useCallback(() => composerOpenRef.current, [])
  const hasCommentDraft = useCallback(() => composerDraftRef.current, [])

  // Bumped whenever the pending anchor changes hands (a new open, a clear), so
  // a post that settles late — after the user moved on to another passage or
  // another artifact through the same host — cannot clear THAT anchor.
  const generationRef = useRef(0)
  const clearSelectionState = useCallback(() => {
    generationRef.current += 1
    composerOpenRef.current = false
    pendingAnchorRef.current = null
    stagedIframeAnchorRef.current = null
    setIframeSelection(null)
    clearAnnotationHighlight(highlightOwnerRef.current)
  }, [])
  // A host torn down with its box open (slot switch) must not leave its paint.
  useEffect(() => () => clearAnnotationHighlight(highlightOwnerRef.current), [])

  const stageIframeSelection = useCallback((anchor: A, sel: ExternalSelectionState) => {
    stagedIframeAnchorRef.current = anchor
    setIframeSelection(sel)
  }, [])

  // `onOpen` runs BEFORE focus moves into the input, while the DOM selection is
  // still live — the one moment the anchor can be resolved from it, and the one
  // moment the pending anchor is written.
  const handleComposerOpen = useCallback((text: string) => {
    generationRef.current += 1
    composerOpenRef.current = true
    const fromDom = resolveDomAnchor(highlightOwnerRef.current)
    const staged = stagedIframeAnchorRef.current
    stagedIframeAnchorRef.current = null
    if (fromDom) pendingAnchorRef.current = fromDom
    else if (staged && quoteOf(staged) === text) pendingAnchorRef.current = staged
    else pendingAnchorRef.current = quoteOnly(text)
    // No DOM range (the bridge path): the frame keeps its own selection visible.
    if (!fromDom) clearAnnotationHighlight(highlightOwnerRef.current)
  }, [resolveDomAnchor, quoteOf, quoteOnly])
  // Whether the host has a post in flight. A guard run meanwhile must not
  // discard under it — a confirmed discard clears the durable slot, and a
  // refusal arriving after it would find nothing left to keep — so it proceeds
  // WITHOUT asking and without touching the slot: the slot is what makes the
  // teardown safe (a late success clears it, a late refusal leaves the text
  // for the next open over the same passage). Nothing waits on the network.
  // A COUNT, not a flag: a box closed mid-flight abandons its post but the
  // post is still outstanding, and a second box may post meanwhile — the
  // first flight settling must not read as "nothing in flight" while the
  // second is still out, or a guard run then would discard the second's draft.
  const inFlightRef = useRef(0)
  const handleComposerSubmit = useCallback((comment: string): void | Promise<boolean> => {
    const pending = pendingAnchorRef.current
    if (!pending) return
    const generation = generationRef.current
    const outcome = submit(comment, pending)
    if (outcome && typeof (outcome as Promise<boolean>).then === 'function') {
      inFlightRef.current += 1
      return (outcome as Promise<boolean>).then(ok => ok, () => false).then(ok => {
        inFlightRef.current -= 1
        // Clear only the anchor this post was made against: a slow post on one
        // passage (or one artifact, the host being reused across a param-only
        // navigation) must not null the anchor of the box open on the next.
        if (ok && generationRef.current === generation) clearSelectionState()
        // Refused with its box gone (closed mid-flight, or the host moved on):
        // nothing on screen says so unless the host does.
        if (!ok && generationRef.current !== generation) onRefusedAfterClose?.(clipQuote(quoteOf(pending)))
        return ok
      })
    }
    clearSelectionState()
  }, [submit, clearSelectionState, onRefusedAfterClose, quoteOf])

  // The draft mirror: whether the box holds unsaved text and which passage it
  // belongs to, so a discard confirmed by one of the host's own guards clears
  // that slot alone.
  const composerDraftRef = useRef(false)
  const composerDraftPassageRef = useRef<{ anchor: string; start: number } | null>(null)
  const handleComposerDraftChange = useCallback((hasDraft: boolean, passage: { anchor: string; start: number } | null) => {
    composerDraftRef.current = hasDraft
    composerDraftPassageRef.current = hasDraft ? passage : null
  }, [])
  const composerDraftStore = useMemo(() => composerDraftStoreFor(draftKey), [draftKey])
  const clearComposerDraftSlot = useCallback(() => {
    const p = composerDraftPassageRef.current
    if (p) composerDraftStore.clear(p.anchor, p.start)
    composerDraftPassageRef.current = null
  }, [composerDraftStore])
  const guardCommentDraft = useCallback(async (proceed: () => void) => {
    // A post in flight: the text is already persisted and the flight's own
    // settle decides the slot (see `inFlightRef`), so leave without asking.
    if (inFlightRef.current > 0) { proceed(); return }
    if (composerDraftRef.current) {
      if (confirmDiscard && !(await confirmDiscard())) return
      clearComposerDraftSlot()
    }
    proceed()
  }, [confirmDiscard, clearComposerDraftSlot])

  const selectionComposer: SelectionComposer = useMemo(() => ({
    onOpen: handleComposerOpen,
    onSubmit: handleComposerSubmit,
    onClose: clearSelectionState,
    onDraftChange: handleComposerDraftChange,
    confirmDiscard,
    draftStore: composerDraftStore,
  }), [handleComposerOpen, handleComposerSubmit, clearSelectionState, handleComposerDraftChange, confirmDiscard, composerDraftStore])

  return {
    selectionComposer, iframeSelection, stageIframeSelection, clearSelectionState,
    isComposerOpen, guardCommentDraft, hasCommentDraft,
  }
}
