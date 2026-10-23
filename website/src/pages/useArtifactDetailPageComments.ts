/**
 * The artifact's comments: their reads and writes (`useArtifactDetailPageComments`),
 * the anchored-selection composer and the draft guards a hand-off asks first
 * (`useArtifactDetailPageComposer`), and each thread's read state and the active
 * comment (`useArtifactDetailPageThreads`). Which thread the floating popover
 * shows is the composer's state: its discard path closes the popover, and the
 * composer registers before the thread hook.
 *
 * The `ArtifactDetailPage` facade calls each hook here at the point its code
 * sits in the page's hook order, which `ArtifactDetailPage.owners.test.tsx`
 * pins: React runs a component's effects in registration order, so moving one
 * is a behaviour change even when every test of the moved code still passes.
 */
import { safeSetItem } from '../utils/safeStorage'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { type IframeSelection } from '../hooks/useCommentBridge'
import { api } from '../api/client'
import { type SelectionAction } from '../components/SelectionToolbar'
import { findCoords, resolveSourcePos } from '../components/MarkdownPanel'
import { containedSelectionRange } from '../utils/selectionContainment'
import { anchorFromRange } from '../utils/selectionAnchor'
import { paintAnnotationHighlight } from '../utils/annotationHighlight'
import { useSelectionComposerAnchor } from '../hooks/useSelectionComposerAnchor'
import { announceCommentsChanged, onCommentsChanged } from '../utils/artifactCommentsSync'
import type { ArtifactComment } from '../types'

import { i18nT } from '../i18n/t'
import { errMessage } from '../utils/thunkError'
import type {
  ArtifactDetailPageStateRead, ArtifactDetailPageDocumentRead,
} from './useArtifactDetailPageDocument'

/** The selection an open comment composer annotates, resolved while it was
 *  still live. `anchor` is the trimmed quote; line/column point at the source
 *  for the agent prompt; the offsets pin the highlight to this occurrence. */
export interface PendingAnchor {
  anchor: string
  line?: number
  column?: number
  prefix?: string
  suffix?: string
  startOffset?: number
  endOffset?: number
}

/** The comment reads and writes, and the callbacks the sidebar and the thread popover call. */
export function useArtifactDetailPageComments({
  stateRead, documentRead, durableComments,
}: {
  stateRead: ArtifactDetailPageStateRead
  documentRead: ArtifactDetailPageDocumentRead
  durableComments: ArtifactComment[]
}) {
  const { slug, queryClient, editing } = stateRead
  const { previewRef, isCurrent, artifact } = documentRead
  // ── Inline-comment handlers ──────────────────────────────────────────────
  // Comments only make sense for kinds where text→source coords resolve
  // cleanly: markdown (via data-sourcepos) and text (rendered === source).
  // JSON / SVG selection produces noisy anchors; revisit when there's a real
  // user demand.
  // Anchored (selection-driven) comment creation: select text in the rendered
  // body to pin a comment to that exact span. The anchor (quote + prefix/suffix
  // + rendered-text offsets + version) is persisted server-side and surfaced to
  // the agent via artifact_get_comments, so a comment is a durable, located
  // instruction rather than a free-floating note. Doc-level comments via the
  // CommentsSidebar remain available for all kinds.
  // markdown AND text: both now render behind a `previewRef` (ContentRenderer
  // attaches it to the markdown DOM and to the <pre> used for text), so a
  // selection has a root to map back to source in either. Do not add a kind here
  // without confirming its renderer attaches the ref, or the composer opens
  // with an anchor that is only the quote.
  const commentable = !!artifact && !editing && isCurrent && (
    artifact.kind === 'markdown' || artifact.kind === 'text'
  )
  const isMarkdown = artifact?.kind === 'markdown'
  const sourceContent = artifact?.content ?? ''

  /** The live DOM selection inside the rendered body as a durable anchor, or
   *  null when there is none (collapsed, blank, or outside the preview). */
  const resolveSelectionAnchor = useCallback((highlightOwner: object): PendingAnchor | null => {
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null
    const root = previewRef.current
    if (!root) return null
    // The SAME containment predicate the toolbar opened the composer with, so
    // a selection it accepted is never rejected here (a triple-click on the
    // last block ends at a boundary point OUTSIDE the preview); the returned
    // range is clamped to the preview, so the offsets below are measured in it.
    const range = containedSelectionRange(sel.getRangeAt(0), root)
    if (!range) return null
    const derived = anchorFromRange(root, range)
    if (!derived) return null
    const raw = range.toString()
    const anchor = derived.quote
    // For markdown, walk the rendered DOM to map (anchorNode, offset) back to
    // (line, col) in the source via data-sourcepos. For text artifacts the
    // rendered text equals the source so findCoords is exact.
    const coords = isMarkdown
      ? (resolveSourcePos(range, root, sourceContent) ?? findCoords(sourceContent, raw) ?? findCoords(sourceContent, anchor))
      : (findCoords(sourceContent, raw) ?? findCoords(sourceContent, anchor))
    // Rendered-text offset of the selection start, in the same space the
    // highlighter's indexTextNodes/rangeForAnchor use — pins the highlight to
    // THIS occurrence when the quote repeats (line/col drive the agent prompt;
    // the offset drives the visual anchor).
    // The box is about to take focus and collapse the selection: paint the
    // passage so the reader can still see what the open box is attached to.
    paintAnnotationHighlight(highlightOwner, range)
    // prefix/suffix ride along: `rangeForAnchor` scores a repeated quote by its
    // neighbours, so a comment on the second "beta" re-anchors to the second.
    return { anchor, line: coords?.line, column: coords?.column, prefix: derived.prefix, suffix: derived.suffix, startOffset: derived.startOffset, endOffset: derived.endOffset }
  }, [isMarkdown, sourceContent, previewRef])

  const invalidateComments = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['artifact-comments', slug] })
  }, [queryClient, slug])
  // Last failed comment write (post/reply/resolve/review/reopen/delete/edit).
  // Rendered via ErrorNotice near the top of the page; cleared by the next
  // successful write or by dismissal.
  const [commentActionError, setCommentActionError] = useState<string | null>(null)
  // Per document, like the edit state reset in useArtifactDetailPageDocument.ts: the route element is reused
  // across artifacts, and a refused-after-close notice names a passage of the
  // artifact it was typed on -- carried into the next one it would point at
  // text that is not there (the draft itself waits under that artifact's key).
  useEffect(() => { setCommentActionError(null) }, [slug])

  // Cross-window mirroring: a popout and the main window are separate JS
  // contexts with separate query caches, so a comment posted in one wouldn't
  // show in the other until staleness (~30s). Announce every local mutation
  // and refetch on announcements from other windows — comments mirror
  // immediately in both directions.
  const invalidateAndAnnounce = useCallback(() => {
    setCommentActionError(null)
    invalidateComments()
    announceCommentsChanged(slug)
  }, [invalidateComments, slug])
  useEffect(() => {
    if (!slug) return
    return onCommentsChanged(slug, invalidateComments)
  }, [slug, invalidateComments])

  // Writes go through useMutation (use-react-query guideline): errors surface
  // through `commentActionError` (rendered as an ErrorNotice) and cache
  // invalidation is centralized. Errors invalidate locally only (safety-net
  // refetch) — a failed mutation didn't change server state, so there's
  // nothing for other windows to sync.
  const onMutErr = useCallback((e: unknown) => {
    setCommentActionError((errMessage(e) || i18nT('components.errorBoundary.something_went_wrong')))
    invalidateComments()
  }, [invalidateComments])
  const postCommentMut = useMutation({
    mutationFn: (vars: { text: string; scope?: string; anchor?: object }) => api.postArtifactComment(slug, vars),
    onSuccess: invalidateAndAnnounce, onError: onMutErr,
  })
  // The composer's own post: its refusal is reported INSIDE the box (text kept,
  // retry offered), so it does not also raise the page's failure banner — one
  // failure, one notice, and the raw server reason stays out of the page.
  const postAnchoredMut = useMutation({
    mutationFn: (vars: { text: string; scope?: string; anchor?: object }) => api.postArtifactComment(slug, vars),
    onSuccess: invalidateAndAnnounce, onError: invalidateComments,
  })
  const replyCommentMut = useMutation({
    mutationFn: (vars: { parentId: string; text: string }) => api.replyArtifactComment(slug, vars.parentId, { text: vars.text }),
    onSuccess: (_d: unknown, vars: { parentId: string; text: string }) => {
      // The reply itself succeeded — announce it immediately so other windows
      // mirror it, regardless of what the follow-up reopen does.
      invalidateAndAnnounce()
      // Replying to a resolved thread auto-reopens it. A second
      // announce picks up the status change; a reopen failure only refetches
      // locally (the reply was already announced above).
      const parent = durableComments.find(c => c.id === vars.parentId)
      if (parent && parent.status === 'resolved') {
        api.reopenComment(slug, vars.parentId).then(invalidateAndAnnounce).catch(onMutErr)
      }
    },
    onError: onMutErr,
  })
  const resolveCommentMut = useMutation({ mutationFn: (id: string) => api.resolveComment(slug, id), onSuccess: invalidateAndAnnounce, onError: onMutErr })
  const markReviewCommentMut = useMutation({ mutationFn: (id: string) => api.markCommentReview(slug, id), onSuccess: invalidateAndAnnounce, onError: onMutErr })
  const reopenCommentMut = useMutation({ mutationFn: (id: string) => api.reopenComment(slug, id), onSuccess: invalidateAndAnnounce, onError: onMutErr })
  const removeCommentMut = useMutation({ mutationFn: (id: string) => api.deleteArtifactComment(slug, id), onSuccess: invalidateAndAnnounce, onError: onMutErr })
  const editCommentMut = useMutation({ mutationFn: (vars: { id: string; text: string }) => api.editArtifactComment(slug, vars.id, { text: vars.text }), onSuccess: invalidateAndAnnounce, onError: onMutErr })

  // Doc-level add (from the sidebar) — works for ALL kinds, including
  // HTML/widget where in-iframe text selection isn't reachable.
  const addDocComment = useCallback((text: string) => {
    // A DOCUMENT-level comment needs no text selection, so unlike the anchored
    // path it can be left on an empty document -- and it must still count as
    postCommentMut.mutate({ text, scope: 'private' })
  }, [postCommentMut])

  const replyComment = useCallback((parentId: string, text: string) => {
    replyCommentMut.mutate({ parentId, text })
  }, [replyCommentMut])

  const resolveComment = useCallback((id: string) => { resolveCommentMut.mutate(id) }, [resolveCommentMut])

  const markReviewComment = useCallback((id: string) => { markReviewCommentMut.mutate(id) }, [markReviewCommentMut])

  const reopenComment = useCallback((id: string) => { reopenCommentMut.mutate(id) }, [reopenCommentMut])

  const removeComment = useCallback((id: string) => { removeCommentMut.mutate(id) }, [removeCommentMut])
  const editComment = useCallback((id: string, text: string) => { editCommentMut.mutate({ id, text }) }, [editCommentMut])

  return {
    commentable, resolveSelectionAnchor, invalidateComments, commentActionError, setCommentActionError,
    postAnchoredMut, addDocComment, replyComment, resolveComment, markReviewComment, reopenComment,
    removeComment, editComment,
  }
}

export type ArtifactDetailPageCommentsRead = ReturnType<typeof useArtifactDetailPageComments>

/** The anchored-selection composer, the draft guards a send-to-session hand-off asks first, and which thread the popover shows. */
export function useArtifactDetailPageComposer({
  stateRead, documentRead, commentsRead, submitAnchored, popout,
}: {
  stateRead: ArtifactDetailPageStateRead
  documentRead: ArtifactDetailPageDocumentRead
  commentsRead: ArtifactDetailPageCommentsRead
  submitAnchored: (text: string, pending: PendingAnchor) => Promise<boolean>
  popout: boolean
}) {
  const { slug, confirm, editing } = stateRead
  const { isCurrent } = documentRead
  const { resolveSelectionAnchor, setCommentActionError } = commentsRead
  // Counts prompts actually shown, so a caller of `guardCommentDraft` can tell
  // "the user agreed to discard" from "the hook went ahead without asking"
  // (a composer post already in flight).
  const discardPromptsRef = useRef(0)
  const confirmDiscardDraft = useCallback(() => {
    discardPromptsRef.current += 1
    return confirm({
      title: i18nT('components.markdownPanel.discard_unsaved_comment'),
      confirmLabel: i18nT('components.markdownPanel.discard_comment_button'),
      // The composer it guards is a body portal at z-[9999]; the prompt must
      // take the layer above it or the box swallows clicks on its buttons.
      layer: 'top',
    })
  }, [confirm])
  const quoteOf = useCallback((a: PendingAnchor) => a.anchor, [])
  const quoteOnly = useCallback((anchor: string): PendingAnchor => ({ anchor }), [])
  // A composer post refused after its box was closed: the page's comment
  // failure banner says so (the draft waits in the store).
  const onRefusedAfterClose = useCallback((quote: string) => {
    setCommentActionError(i18nT('components.selectionToolbar.comment_post_failed_closed', { quote }))
  }, [setCommentActionError])
  // The shared composer wiring (pending/staged anchors, external selection,
  // highlight, draft mirror + guard). The draft store is per artifact, per
  // passage — the same record the side panel uses for this artifact, so a draft
  // started in one surface comes back in the other. Edit mode and a version
  // switch unmount the toolbar (and the box with it), so both run
  // `guardCommentDraft` first — in the draft's own words, since "unsaved
  // changes" would read as file edits at risk.
  // The anchor of the last post that SUCCEEDED, bumped per post: a code block
  // clears the line range that post was made on (see useArtifactCodeComments).
  // Taken from the post itself, so a draft the composer kept, a refused post,
  // or someone else's comment never counts.
  const [lastPosted, setLastPosted] = useState<{ startOffset: number; quote: string; seq: number } | null>(null)
  const submitAndRecord = useCallback((text: string, pending: PendingAnchor) => submitAnchored(text, pending).then(ok => {
    if (ok && pending.startOffset != null && pending.anchor) {
      const { startOffset, anchor: quote } = pending
      setLastPosted(p => ({ startOffset, quote, seq: (p?.seq ?? 0) + 1 }))
    }
    return ok
  }), [submitAnchored])
  const {
    selectionComposer, iframeSelection, stageIframeSelection, clearSelectionState, guardCommentDraft, hasCommentDraft,
  } = useSelectionComposerAnchor<PendingAnchor>({
    resolveDomAnchor: resolveSelectionAnchor, quoteOf, quoteOnly, submit: submitAndRecord,
    draftKey: `mc-artifact-composer-draft:${slug}`, confirmDiscard: confirmDiscardDraft, onRefusedAfterClose,
  })
  // Navigating between artifacts drops any anchor of the departing document.
  useEffect(() => { clearSelectionState() }, [slug, clearSelectionState])

  // The comments sidebar's own composers (add box, reply, in-place edit) hold
  // local state the selection-composer guard cannot see. A send-to-session
  // hand-off navigates away and unmounts them, so it asks about these too. A
  // popout does not navigate (sendNav forwards the intent to a main window and
  // stays on the artifact), so there is nothing to lose and nothing to ask.
  //
  // A confirmed discard takes effect AT ONCE: the sidebar's boxes reset and the
  // selection toolbar remounts (closing its box; the hook already cleared the
  // stored draft). So any draft present after a slow create was typed since,
  // and is asked about with no bookkeeping about which drafts were seen. The
  // cost, accepted by the owner: if the create then fails, the discarded draft
  // is gone even though the page stayed.
  const sidebarDraftDirtyRef = useRef(false)
  const onSidebarDraftDirtyChange = useCallback((dirty: boolean) => { sidebarDraftDirtyRef.current = dirty }, [])
  // The floating thread popover's reply and edit boxes count the same way; a
  // confirmed discard closes the popover, which drops its text.
  const threadDraftDirtyRef = useRef(false)
  const onThreadDraftDirtyChange = useCallback((dirty: boolean) => { threadDraftDirtyRef.current = dirty }, [])
  const [sidebarDiscardSignal, setSidebarDiscardSignal] = useState(0)
  const [composerResetNonce, setComposerResetNonce] = useState(0)
  // The remount lands on the next commit; until then the hook still reports
  // the discarded box as dirty, which must not count as a new draft.
  const composerDiscardPendingRef = useRef(false)
  useEffect(() => { composerDiscardPendingRef.current = false }, [composerResetNonce])
  const discardDraftsBeforeLeaving = useCallback((): Promise<boolean> => new Promise((resolve) => {
    const sidebar = sidebarDraftDirtyRef.current || threadDraftDirtyRef.current
    const composer = hasCommentDraft() && !composerDiscardPendingRef.current
    const discardSidebar = () => {
      if (!sidebar) return
      if (sidebarDraftDirtyRef.current) {
        sidebarDraftDirtyRef.current = false
        setSidebarDiscardSignal((n) => n + 1)
      }
      if (threadDraftDirtyRef.current) {
        threadDraftDirtyRef.current = false
        setOpenThread(null)
      }
    }
    if (composer) {
      // One prompt covers both: the hook asks and clears the composer's slot.
      let ok = false
      const promptsBefore = discardPromptsRef.current
      void guardCommentDraft(() => { ok = true }).then(async () => {
        if (!ok) { resolve(false); return }
        if (discardPromptsRef.current !== promptsBefore) {
          composerDiscardPendingRef.current = true
          // Clear the iframe selection too: the remounted toolbar would
          // otherwise reopen an empty box over the discarded passage.
          clearSelectionState()
          setComposerResetNonce((n) => n + 1)
          discardSidebar()
          resolve(true)
          return
        }
        // The hook went ahead without asking (its post is already saving), so
        // nothing has agreed to the sidebar's draft yet.
        if (sidebar && !(await confirmDiscardDraft())) { resolve(false); return }
        discardSidebar()
        resolve(true)
      })
    } else if (sidebar) {
      void confirmDiscardDraft().then((ok) => { if (ok) discardSidebar(); resolve(ok) })
    } else {
      resolve(true)
    }
  }), [confirmDiscardDraft, guardCommentDraft, hasCommentDraft, clearSelectionState])
  const guardSendToSession = useCallback((proceed: (recheck: (go: () => void) => void) => void | Promise<void>) => {
    if (popout) { void proceed((go) => go()); return }
    void (async () => {
      if (!(await discardDraftsBeforeLeaving())) return
      void proceed((go) => { void discardDraftsBeforeLeaving().then((ok) => { if (ok) go() }) })
    })()
  }, [popout, discardDraftsBeforeLeaving])
  // No row action beside the composer: the box already carries Add comment and
  // Close, and a third control would break the two-per-row cap. Copying the
  // selection is the composer's own Cmd/Ctrl+C while its input is empty.
  const selectionActions: SelectionAction[] = useMemo(() => [], [])
  // In-iframe text selection (widget/html via the bridge): stage the
  // iframe-derived anchor, then ask the toolbar to open the composer at the
  // supplied viewport rect. Offsets are dropped on purpose — they are in the
  // iframe body's text space, not the parent highlighter's — so the comment
  // re-anchors by quote + prefix/suffix inside the frame.
  const handleIframeSelect = useCallback((sel: IframeSelection) => {
    // The bridge relays selections on a historical snapshot too; a comment is
    // stored against the CURRENT artifact, so those are not annotatable.
    if (editing || !isCurrent) return
    // The frame's offset keys the draft slot (a stable passage id), though the
    // anchor itself omits it (wrong text space for the parent highlighter).
    stageIframeSelection({ anchor: sel.quote, prefix: sel.prefix, suffix: sel.suffix }, { text: sel.quote, x: sel.x, y: sel.y, start: sel.startOffset })
  }, [editing, isCurrent, stageIframeSelection])

  // Opening a thread (bubble/highlight click, or the iframe bridge) → activate,
  // mark read, and open the floating thread popover. Markdown finds its anchor
  // via data-mc-cid; the iframe passes a viewport rect.
  const [openThread, setOpenThread] = useState<{ rootId: string; rect?: { x: number; y: number; w: number; h: number } } | null>(null)

  return {
    selectionComposer, iframeSelection, clearSelectionState, guardCommentDraft, onSidebarDraftDirtyChange,
    onThreadDraftDirtyChange, sidebarDiscardSignal, composerResetNonce, guardSendToSession, selectionActions,
    handleIframeSelect, stageIframeSelection, openThread, setOpenThread, lastPosted,
  }
}

export type ArtifactDetailPageComposerRead = ReturnType<typeof useArtifactDetailPageComposer>

/** Each thread's read state, the active comment, and opening a thread from a highlight or the sidebar. */
export function useArtifactDetailPageThreads({
  stateRead, documentRead, composerRead, durableComments,
}: {
  stateRead: ArtifactDetailPageStateRead
  documentRead: ArtifactDetailPageDocumentRead
  composerRead: ArtifactDetailPageComposerRead
  durableComments: ArtifactComment[]
}) {
  const { slug } = stateRead
  const { setIframeScrollTarget, usesIframe } = documentRead
  const { setOpenThread } = composerRead
  // Persistent active comment; NO transitory flash.
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null)
  const [bodyScrollNonce, setBodyScrollNonce] = useState(0)
  // Read/unread tracking: a per-artifact set of seen comment ids
  // in localStorage; a thread is unread if any of its comments is unseen.
  const readKey = `mc-cmt-read:${slug}`
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

  const openThreadHandler = useCallback((id: string, rect?: { x: number; y: number; w: number; h: number }) => {
    setActiveCommentId(id)
    markThreadRead(id)
    setOpenThread({ rootId: id, rect })
  }, [markThreadRead, setOpenThread])
  // Sidebar comment clicked → activate, scroll the doc to the anchor, open popover.
  const activateFromSidebar = useCallback((id: string) => {
    setActiveCommentId(id)
    markThreadRead(id)
    if (usesIframe) {
      // The bridge scrolls the iframe, then posts the anchor rect → onOpenThread
      // opens the popover over the iframe.
      setIframeScrollTarget({ id, nonce: Date.now() })
    } else {
      setBodyScrollNonce(n => n + 1)
      setOpenThread({ rootId: id })
    }
  }, [markThreadRead, usesIframe, setIframeScrollTarget, setOpenThread])

  return {
    activeCommentId, bodyScrollNonce, unreadRootIds, openThreadHandler, activateFromSidebar,
  }
}

export type ArtifactDetailPageThreadsRead = ReturnType<typeof useArtifactDetailPageThreads>
