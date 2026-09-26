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
export function useSelectionComposerAnchor<A>({
  resolveDomAnchor, quoteOf, quoteOnly, submit, draftKey, confirmDiscard,
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
  /** Whether the open box holds unsaved text. */
  hasComposerDraft: () => boolean
  /** After a discard the host confirmed itself: drop THAT passage's persisted
   *  copy so it does not resurface on the next open. */
  clearComposerDraftSlot: () => void
  /** Run `proceed` unless an unsaved draft would be lost, in which case ask
   *  first (via `confirmDiscard`); a confirmed discard clears the slot too.
   *  Without `confirmDiscard` the draft is dropped without asking. */
  guardCommentDraft: (proceed: () => void) => Promise<void>
} {
  const pendingAnchorRef = useRef<A | null>(null)
  const stagedIframeAnchorRef = useRef<A | null>(null)
  const [iframeSelection, setIframeSelection] = useState<ExternalSelectionState | null>(null)
  const highlightOwnerRef = useRef<object>({})
  const composerOpenRef = useRef(false)
  const isComposerOpen = useCallback(() => composerOpenRef.current, [])

  const clearSelectionState = useCallback(() => {
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
  const handleComposerSubmit = useCallback((comment: string): void | Promise<boolean> => {
    const pending = pendingAnchorRef.current
    if (!pending) return
    const outcome = submit(comment, pending)
    if (outcome && typeof (outcome as Promise<boolean>).then === 'function') {
      return (outcome as Promise<boolean>).then(ok => ok, () => false).then(ok => {
        // The refused comment is still in the box: keep its anchor for the retry.
        if (ok) clearSelectionState()
        return ok
      })
    }
    clearSelectionState()
  }, [submit, clearSelectionState])

  // The draft mirror: whether the box holds unsaved text and which passage it
  // belongs to, so a discard confirmed by one of the host's own guards clears
  // that slot alone.
  const composerDraftRef = useRef(false)
  const composerDraftPassageRef = useRef<{ anchor: string; start: number } | null>(null)
  const handleComposerDraftChange = useCallback((hasDraft: boolean, passage: { anchor: string; start: number } | null) => {
    composerDraftRef.current = hasDraft
    composerDraftPassageRef.current = hasDraft ? passage : null
  }, [])
  const hasComposerDraft = useCallback(() => composerDraftRef.current, [])
  const composerDraftStore = useMemo(() => composerDraftStoreFor(draftKey), [draftKey])
  const clearComposerDraftSlot = useCallback(() => {
    const p = composerDraftPassageRef.current
    if (p) composerDraftStore.clear(p.anchor, p.start)
    composerDraftPassageRef.current = null
  }, [composerDraftStore])
  const guardCommentDraft = useCallback(async (proceed: () => void) => {
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
    isComposerOpen, hasComposerDraft, clearComposerDraftSlot, guardCommentDraft,
  }
}
