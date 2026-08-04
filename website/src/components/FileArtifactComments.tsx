import { safeSetItem } from '../utils/safeStorage'
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ArtifactComment, CommentAnchor } from '../types'
import { CommentsSidebar } from './CommentsSidebar'
import { InlineCommentOverlay } from './InlineCommentOverlay'
import { CommentThreadPopover } from './CommentThreadPopover'
import { CommentPopover } from './CommentOverlay'

/**
 * Durable artifact-comment layer for a NON-iframe (markdown / text) body,
 * keyed by an artifact slug, packaged as a hook that returns three *placed*
 * render nodes so the file viewer can slot them into its own layout:
 *   - `overlay`  -> mount INSIDE the positioned scroll container (it positions
 *                   highlight rects + gutter bubbles in content coords);
 *   - `sidebar`  -> mount beside the content (chronological feed);
 *   - `popovers` -> mount anywhere (fixed-positioned create + thread popovers).
 *
 * This is the SAME experience as the artifact detail page (overlay highlights,
 * gutter bubbles, floating thread popover, chronological sidebar) — the file
 * viewer uses it for file-backed artifacts. Comments persist in the artifact
 * store and are never dumped to chat.
 *
 * `requestAnchoredComment()` opens the create popover for the current text
 * selection — call it from the viewer's existing "Comment" selection action so
 * there's no competing auto-popover.
 *
 * Pass `slug=null` to make the hook inert (no query, empty nodes) so callers
 * can invoke it unconditionally for non-artifact files.
 */
export function useFileArtifactComments({
  slug, previewRef, scrollRef, usesIframe = false, sidebarClassName, sidebarStyle,
  sidebarDefaultOpen = true,
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
}): {
  overlay: ReactNode
  popovers: ReactNode
  sidebar: ReactNode
  requestAnchoredComment: () => void
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
  /** In-iframe text selection → open the create popover at the given rect. */
  onIframeSelect: (sel: { x: number; y: number; quote: string; prefix?: string; suffix?: string }) => void
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
  const durableComments = slug ? (commentsQuery.data?.comments ?? []) : []
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

  // ── anchored-create popover (driven by the viewer's "Comment" action) ──
  const [popover, setPopover] = useState<{ x: number; y: number; quote: string; prefix?: string; suffix?: string; startOffset?: number; endOffset?: number } | null>(null)
  const requestAnchoredComment = useCallback(() => {
    const sel = window.getSelection()
    const raw = sel?.toString() ?? ''
    if (!sel || sel.isCollapsed || !raw.trim()) return
    const root = previewRef.current
    if (!root || !sel.anchorNode || !root.contains(sel.anchorNode)) return
    const range = sel.getRangeAt(0)
    if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return
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
    const rect = range.getBoundingClientRect()
    // Persist the rendered-text offset (`idx`) so the highlighter can re-anchor
    // to THIS occurrence rather than the first match of the quote.
    setPopover({ x: rect.left, y: rect.bottom, quote, prefix, suffix, startOffset: idx, endOffset: idx + quote.length })
  }, [previewRef])

  // In-iframe text selection (widget/html via the bridge) → open the create
  // popover at the supplied viewport rect with the iframe-derived anchor.
  const onIframeSelect = useCallback((sel: { x: number; y: number; quote: string; prefix?: string; suffix?: string }) => {
    setPopover({ x: sel.x, y: sel.y, quote: sel.quote, prefix: sel.prefix, suffix: sel.suffix })
  }, [])

  // ── mutations (all hit the durable artifact comment store) ──
  // Writes go through useMutation so errors aren't silently swallowed and cache
  // invalidation is centralized (use-react-query guideline). The mutations are
  // only reachable when slug is non-null (the returned nodes are null otherwise).
  const onMutErr = useCallback(() => invalidate(), [invalidate])
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

  const addAnchored = useCallback((text: string) => {
    if (!popover || !slug) return
    const anchor: CommentAnchor = { quote: popover.quote, prefix: popover.prefix, suffix: popover.suffix }
    // Only the native text-selection path computes an offset; iframe selections
    // (no startOffset) omit it and keep the prefix/suffix anchor.
    if (popover.startOffset != null) {
      anchor.start_offset = popover.startOffset
      anchor.end_offset = popover.endOffset ?? popover.startOffset + popover.quote.length
    }
    postMut.mutate({ text, scope: 'private', anchor })
    setPopover(null); window.getSelection()?.removeAllRanges()
  }, [popover, slug, postMut])
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
      {popover && (
        <CommentPopover
          x={popover.x}
          y={popover.y}
          onSubmit={addAnchored}
          onCancel={() => { setPopover(null); window.getSelection()?.removeAllRanges() }}
        />
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
    overlay, popovers, sidebar, requestAnchoredComment, toggleSidebar, sidebarOpen,
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
