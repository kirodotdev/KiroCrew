import { safeSetItem } from '../utils/safeStorage'
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ArtifactComment, CommentAnchor } from '../types'
import { CommentsSidebar } from './CommentsSidebar'
import { InlineCommentOverlay } from './InlineCommentOverlay'
import { CommentThreadPopover } from './CommentThreadPopover'
import type { SelectionComposer } from './SelectionToolbar'
import { containedSelectionRange } from '../utils/selectionContainment'
import { paintAnnotationHighlight } from '../utils/annotationHighlight'
import { useSelectionComposerAnchor } from '../hooks/useSelectionComposerAnchor'
import ErrorNotice from './ErrorNotice'
import { errMessage } from '../utils/thunkError'
import { i18nT } from '../i18n/t'

/** The selection an open composer annotates, resolved while it was still live. */
interface PendingAnchor {
  quote: string
  prefix?: string
  suffix?: string
  startOffset?: number
  endOffset?: number
}

/**
 * Durable artifact-comment layer for a NON-iframe (markdown / text) body,
 * keyed by an artifact slug, packaged as a hook that returns three *placed*
 * render nodes so the file viewer can slot them into its own layout:
 *   - `overlay`  -> mount INSIDE the positioned scroll container (it positions
 *                   highlight rects + gutter bubbles in content coords);
 *   - `sidebar`  -> mount beside the content (chronological feed);
 *   - `popovers` -> mount anywhere (the fixed-positioned thread popover and the
 *                   error notices the sidebar shows when it is closed).
 *
 * This is the SAME experience as the artifact detail page (overlay highlights,
 * gutter bubbles, floating thread popover, chronological sidebar) — the file
 * viewer uses it for file-backed artifacts. Comments persist in the artifact
 * store and are never dumped to chat.
 *
 * `selectionComposer` is the type-first annotation input for the viewer's
 * `SelectionToolbar`: selecting text opens the comment box at once, `onOpen`
 * resolves the anchor while the DOM selection is still live, and `onSubmit`
 * posts the anchored comment. For an iframe body the bridge's selection arrives
 * through `onIframeSelect`, which stores its anchor and hands the toolbar an
 * `iframeSelection` to open the same box from.
 *
 * Pass `slug=null` to make the hook inert (no query, empty nodes) so callers
 * can invoke it unconditionally for non-artifact files.
 */
export function useFileArtifactComments({
  slug, previewRef, scrollRef, usesIframe = false, sidebarClassName, sidebarStyle,
  sidebarDefaultOpen = true, confirmDiscardDraft,
}: {
  slug: string | null
  previewRef: React.RefObject<HTMLDivElement | null>
  scrollRef: React.RefObject<HTMLElement | null>
  /** When the body is a sandboxed iframe (widget/html), selections and thread
   *  opens come from the in-iframe bridge rather than DOM text selection.
   *  Defaults false. */
  usesIframe?: boolean
  /** Initial sidebar open state. Defaults true; the chat side panel passes
   *  false to keep comments collapsed and content primary. */
  sidebarDefaultOpen?: boolean
  /** Override the `sidebar` `<aside>` sizing. Omitted → the full-page default. */
  sidebarClassName?: string
  sidebarStyle?: React.CSSProperties
  /** Asked before Escape / ✕ discards a typed comment draft; resolve `true` to
   *  discard. The host owns the dialog so the wording matches its other
   *  discard prompts. Omit to discard without asking. */
  confirmDiscardDraft?: () => Promise<boolean>
}): {
  overlay: ReactNode
  popovers: ReactNode
  sidebar: ReactNode
  /** Pass to the viewer's `SelectionToolbar` as `composer`. */
  selectionComposer: SelectionComposer
  /** Whether the composer is open at all — the host's own Escape handler
   *  stands down while it is (the toolbar closes the box on Escape itself, and
   *  the box need not hold focus). */
  isComposerOpen: () => boolean
  /** Whether the open composer holds unsaved text — the host asks before an
   *  action of its own (full screen, close) would unmount the toolbar. */
  hasComposerDraft: () => boolean
  /** After the host's own guard confirmed a discard: drop the persisted copy
   *  of THAT passage's draft, so it does not resurface on the next open. */
  clearComposerDraftSlot: () => void
  /** Pass to the same toolbar as `externalSelection`: the latest in-iframe
   *  selection, for a body the DOM toolbar cannot see into. Null otherwise. */
  iframeSelection: { text: string; x: number; y: number; start?: number } | null
  toggleSidebar: () => void
  sidebarOpen: boolean
  commentCount: number
  /** Raw durable comments (for callers that render their own body, e.g. the
   *  multi-kind side-panel Artifacts tab) and the building-block state the
   *  body needs to position overlays / sync the active thread. */
  comments: ArtifactComment[]
  activeCommentId: string | null
  scrollNonce: number
  unreadRootIds: Set<string>
  /** Activate a thread (= open its popover + mark read). Pass to the body. */
  activateComment: (id: string) => void
  /** In-iframe text selection → open the composer at the given rect. */
  onIframeSelect: (sel: { x: number; y: number; quote: string; prefix?: string; suffix?: string; startOffset?: number }) => void
  /** In-iframe highlight click → open the thread popover. */
  onIframeOpenThread: (id: string, rect?: { x: number; y: number; w: number; h: number }) => void
  /** Drives the iframe bridge to scroll a comment's anchor into view. */
  iframeScrollTarget: { id: string; nonce: number } | null
} {
  const qc = useQueryClient()
  const commentsQuery = useQuery<{ comments: ArtifactComment[]; remote_sync_error?: string | null }>({
    queryKey: ['artifact-comments', slug],
    queryFn: () => api.artifactComments(slug as string),
    enabled: !!slug,
    staleTime: 30_000,
  })
  // Memoized because it is the dep of rootIdOf / unreadRootIds / markThreadRead:
  // the `[]` fallback (no slug, or the query not yet resolved) is a fresh array
  // each render, which would rebuild all three — and every consumer memo keyed
  // on them — on every render. React Query keeps `data` referentially stable
  // between refetches that resolve deep-equal, so this changes only on real data.
  const durableComments = useMemo(
    () => (slug ? (commentsQuery.data?.comments ?? []) : []),
    [slug, commentsQuery.data?.comments],
  )
  const remoteSyncError = commentsQuery.data?.remote_sync_error ?? null
  const invalidate = useCallback(() => {
    if (slug) qc.invalidateQueries({ queryKey: ['artifact-comments', slug] })
  }, [qc, slug])

  // ── read/unread (localStorage, per artifact) ──
  const readKey = `mc-cmt-read:${slug ?? ''}`
  const [readIds, setReadIds] = useState<Set<string>>(new Set())
  useEffect(() => {
    try { setReadIds(new Set(JSON.parse(localStorage.getItem(readKey) || '[]'))) }
    catch { setReadIds(new Set()) }
  }, [readKey])
  const rootIdOf = useCallback(
    (c: ArtifactComment) => (c.parent_id && durableComments.some(x => x.id === c.parent_id) ? c.parent_id : c.id),
    [durableComments],
  )
  const unreadRootIds = useMemo(() => {
    const s = new Set<string>()
    for (const c of durableComments) if (!readIds.has(c.id)) s.add(rootIdOf(c))
    return s
  }, [durableComments, readIds, rootIdOf])
  const markThreadRead = useCallback((rootId: string) => {
    const ids = durableComments.filter(c => c.id === rootId || c.parent_id === rootId).map(c => c.id)
    setReadIds(prev => {
      const next = new Set(prev)
      ids.forEach(i => next.add(i))
      try { safeSetItem(readKey, JSON.stringify([...next])) } catch { /* quota */ }
      return next
    })
  }, [durableComments, readKey])

  // ── active comment + thread popover ──
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null)
  const [bodyScrollNonce, setBodyScrollNonce] = useState(0)
  const [openThread, setOpenThread] = useState<{ rootId: string; rect?: { x: number; y: number; w: number; h: number } } | null>(null)
  // For iframe (widget/html) bodies: drives the bridge to scroll a comment's
  // anchor highlight into view, mirroring the full-page detail route.
  const [iframeScrollTarget, setIframeScrollTarget] = useState<{ id: string; nonce: number } | null>(null)
  const openThreadHandler = useCallback((id: string, rect?: { x: number; y: number; w: number; h: number }) => {
    setActiveCommentId(id); markThreadRead(id); setOpenThread({ rootId: id, rect })
  }, [markThreadRead])
  const activateFromSidebar = useCallback((id: string) => {
    setActiveCommentId(id); markThreadRead(id)
    if (usesIframe) {
      // The bridge scrolls the iframe, then posts the anchor rect → onOpenThread.
      setIframeScrollTarget({ id, nonce: Date.now() })
    } else {
      setBodyScrollNonce(n => n + 1); setOpenThread({ rootId: id })
    }
  }, [markThreadRead, usesIframe])

  // ── sidebar open/collapse ──
  const [sidebarOpen, setSidebarOpen] = useState(sidebarDefaultOpen)
  const toggleSidebar = useCallback(() => setSidebarOpen(v => !v), [])

  // ── anchored create (the toolbar's type-first composer) ──
  // The pending/staged anchors, the external selection, the highlight token and
  // the draft guard live in the shared wiring below; this host contributes how
  // the live DOM selection inside `previewRef` becomes a durable anchor.
  const resolveSelectionAnchor = useCallback((highlightOwner: object): PendingAnchor | null => {
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null
    const root = previewRef.current
    if (!root) return null
    // The SAME containment predicate the toolbar opened the composer with, so
    // a selection it accepted is never rejected here: a triple-click on the
    // preview's last block ends at a boundary point OUTSIDE the preview, and
    // judging the raw endpoints would leave the comment with a quote-only
    // anchor. The returned range is clamped to the preview, so every offset
    // below is measured inside it.
    const range = containedSelectionRange(sel.getRangeAt(0), root)
    if (!range) return null
    const raw = range.toString()
    if (!raw.trim()) return null
    const quote = raw.trim()
    // Derive the selection's real offset from the Range, NOT full.indexOf(quote):
    // indexOf finds the FIRST occurrence, so selecting a later repeat of the same
    // text would store prefix/suffix for the wrong spot and mis-anchor the
    // highlight. Use Range.toString() for both the full text and the
    // pre-selection slice so the offset space is consistent — innerText inserts
    // block newlines that Range.toString omits, and the matcher works off
    // textContent (which Range.toString mirrors).
    const fullRange = document.createRange()
    fullRange.selectNodeContents(root)
    const full = fullRange.toString()
    const preRange = document.createRange()
    preRange.setStart(root, 0)
    preRange.setEnd(range.startContainer, range.startOffset)
    const idx = preRange.toString().length + (raw.length - raw.trimStart().length)
    const prefix = full.slice(Math.max(0, idx - 32), idx)
    const suffix = full.slice(idx + quote.length, idx + quote.length + 32)
    // The box is about to take focus and collapse the selection: paint the
    // passage so the reader can still see what the open box is attached to.
    paintAnnotationHighlight(highlightOwner, range)
    // Persist the rendered-text offset (`idx`) so the highlighter can re-anchor
    // to THIS occurrence rather than the first match of the quote.
    return { quote, prefix, suffix, startOffset: idx, endOffset: idx + quote.length }
  }, [previewRef])

  // ── mutations (all hit the durable artifact comment store) ──
  // Writes go through useMutation so errors aren't silently swallowed and cache
  // invalidation is centralized (use-react-query guideline). The mutations are
  // only reachable when slug is non-null (the returned nodes are null otherwise).
  // A rejected write is ALSO kept as a message: invalidating alone re-reads the
  // list, which silently drops the user's text with no explanation. The
  // sidebar renders it beside the composer (no hand-off there — see the sidebar).
  const [mutationError, setMutationError] = useState<string | null>(null)
  const onMutErr = useCallback((e: unknown) => {
    setMutationError(errMessage(e) || i18nT('components.commentsSidebar.comment_change_failed'))
    invalidate()
  }, [invalidate])
  const clearMutationError = useCallback(() => setMutationError(null), [])
  const loadError = commentsQuery.isError
    ? (errMessage(commentsQuery.error) || i18nT('components.commentsSidebar.comments_load_failed'))
    : null
  const postMut = useMutation({
    mutationFn: (vars: { text: string; scope?: string; anchor?: object }) =>
      api.postArtifactComment(slug as string, vars),
    onSuccess: invalidate, onError: onMutErr,
  })
  const replyMut = useMutation({
    mutationFn: (vars: { parentId: string; text: string }) =>
      api.replyArtifactComment(slug as string, vars.parentId, { text: vars.text }),
    onSuccess: (_d: unknown, vars: { parentId: string; text: string }) => {
      // Replying to a resolved thread auto-reopens it.
      const parent = durableComments.find(c => c.id === vars.parentId)
      if (parent && parent.status === 'resolved') {
        api.reopenComment(slug as string, vars.parentId).then(invalidate).catch(onMutErr)
      } else {
        invalidate()
      }
    },
    onError: onMutErr,
  })
  const resolveMut = useMutation({ mutationFn: (id: string) => api.resolveComment(slug as string, id), onSuccess: invalidate, onError: onMutErr })
  const markReviewMut = useMutation({ mutationFn: (id: string) => api.markCommentReview(slug as string, id), onSuccess: invalidate, onError: onMutErr })
  const reopenMut = useMutation({ mutationFn: (id: string) => api.reopenComment(slug as string, id), onSuccess: invalidate, onError: onMutErr })
  const removeMut = useMutation({ mutationFn: (id: string) => api.deleteArtifactComment(slug as string, id), onSuccess: invalidate, onError: onMutErr })
  const editMut = useMutation({ mutationFn: (v: { id: string; text: string }) => api.editArtifactComment(slug as string, v.id, { text: v.text }), onSuccess: invalidate, onError: onMutErr })

  const submitAnchored = useCallback((text: string, pending: PendingAnchor): Promise<boolean> => {
    if (!slug) return Promise.resolve(false)
    const anchor: CommentAnchor = { quote: pending.quote, prefix: pending.prefix, suffix: pending.suffix }
    // Only the native text-selection path computes an offset; iframe selections
    // (no startOffset) omit it and keep the prefix/suffix anchor.
    if (pending.startOffset != null) {
      anchor.start_offset = pending.startOffset
      anchor.end_offset = pending.endOffset ?? pending.startOffset + pending.quote.length
    }
    // Resolve, never throw: the toolbar keeps the typed text (and its persisted
    // draft) on `false`, and `onMutErr` has already put the failure beside the
    // sidebar's composer.
    return postMut.mutateAsync({ text, scope: 'private', anchor }).then(() => true, () => false)
  }, [slug, postMut])
  const quoteOf = useCallback((a: PendingAnchor) => a.quote, [])
  const quoteOnly = useCallback((quote: string): PendingAnchor => ({ quote }), [])
  // Where the draft lives between teardowns the toolbar cannot guard — a
  // chat-slot switch replaces the whole side panel. Per artifact, per passage.
  const {
    selectionComposer, iframeSelection, stageIframeSelection, isComposerOpen, hasComposerDraft, clearComposerDraftSlot,
  } = useSelectionComposerAnchor<PendingAnchor>({
    resolveDomAnchor: resolveSelectionAnchor, quoteOf, quoteOnly, submit: submitAnchored,
    draftKey: `mc-artifact-composer-draft:${slug ?? ''}`, confirmDiscard: confirmDiscardDraft,
  })
  // In-iframe text selection (widget/html via the bridge): stage the
  // iframe-derived anchor, then ask the toolbar to open the composer at the
  // supplied viewport rect. Offsets are deliberately dropped — they are in the
  // iframe body's text space, not the one the parent highlighter reads — so the
  // comment re-anchors by quote + prefix/suffix inside the frame.
  const onIframeSelect = useCallback((sel: { x: number; y: number; quote: string; prefix?: string; suffix?: string; startOffset?: number }) => {
    // The frame's offset is not stored on the anchor (wrong text space for the
    // parent highlighter) but it IS a stable passage key for the draft slot.
    stageIframeSelection({ quote: sel.quote, prefix: sel.prefix, suffix: sel.suffix }, { text: sel.quote, x: sel.x, y: sel.y, start: sel.startOffset })
  }, [stageIframeSelection])
  const addDoc = useCallback((text: string) => { if (slug) postMut.mutate({ text, scope: 'private' }) }, [slug, postMut])
  const reply = useCallback((parentId: string, text: string) => { if (slug) replyMut.mutate({ parentId, text }) }, [slug, replyMut])
  const resolve = useCallback((id: string) => { if (slug) resolveMut.mutate(id) }, [slug, resolveMut])
  const markReview = useCallback((id: string) => { if (slug) markReviewMut.mutate(id) }, [slug, markReviewMut])
  const reopen = useCallback((id: string) => { if (slug) reopenMut.mutate(id) }, [slug, reopenMut])
  const remove = useCallback((id: string) => { if (slug) removeMut.mutate(id) }, [slug, removeMut])
  const editComment = useCallback((id: string, text: string) => { if (slug) editMut.mutate({ id, text }) }, [slug, editMut])

  const overlay: ReactNode = slug ? (
    <InlineCommentOverlay
      scrollRef={scrollRef}
      textRef={previewRef}
      comments={durableComments}
      activeId={activeCommentId}
      scrollNonce={bodyScrollNonce}
      unreadRootIds={unreadRootIds}
      onActivate={openThreadHandler}
    />
  ) : null

  const popovers: ReactNode = slug ? (
    <>
      {/* Writes also originate from the toolbar composer (anchored add) and the
          thread popover (reply / resolve), which stay reachable while the
          sidebar is CLOSED — so a
          rejected write, and equally a rejected comments READ (the overlay then
          shows zero comments), must not wait for a sidebar that is not mounted.
          When the sidebar is open it owns both notices (beside the composer);
          when it is closed they land here, in the node the host always mounts.
          No hand-off: the thread reply / comment draft is unsaved. */}
      {!sidebarOpen && (loadError || mutationError) && (
        <div className="fixed bottom-safe-offset-4 right-safe-offset-4 z-[60] max-w-[420px] flex flex-col gap-2">
          <ErrorNotice
            testId="artifact-comments-load-error"
            message={loadError}
          />
          <ErrorNotice
            testId="artifact-comments-mutation-error"
            message={mutationError}
            onDismiss={clearMutationError}
          />
        </div>
      )}
      {openThread && (
        <CommentThreadPopover
          comments={durableComments}
          rootId={openThread.rootId}
          rect={openThread.rect}
          onClose={() => setOpenThread(null)}
          onReply={reply}
          onResolve={resolve}
          onMarkReview={markReview}
          onReopen={reopen}
          onDelete={remove}
          onEditComment={editComment}
        />
      )}
    </>
  ) : null

  const sidebar: ReactNode = slug && sidebarOpen ? (
    <CommentsSidebar
      comments={durableComments}
      loading={commentsQuery.isFetching}
      remoteSyncError={remoteSyncError}
      loadError={loadError}
      mutationError={mutationError}
      onDismissMutationError={clearMutationError}
      onAdd={addDoc}
      onReply={reply}
      onResolve={resolve}
      onMarkReview={markReview}
      onReopen={reopen}
      onDelete={remove}
      onRefresh={invalidate}
      onClose={toggleSidebar}
      onCommentClick={activateFromSidebar}
      onEditComment={editComment}
      activeCommentId={activeCommentId}
      containerClassName={sidebarClassName}
      containerStyle={sidebarStyle}
    />
  ) : null

  return {
    overlay, popovers, sidebar, selectionComposer, isComposerOpen, hasComposerDraft, clearComposerDraftSlot, iframeSelection, toggleSidebar, sidebarOpen,
    commentCount: durableComments.length,
    comments: durableComments,
    activeCommentId,
    scrollNonce: bodyScrollNonce,
    unreadRootIds,
    activateComment: openThreadHandler,
    onIframeSelect,
    onIframeOpenThread: openThreadHandler,
    iframeScrollTarget,
  }
}
