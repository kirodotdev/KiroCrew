/**
 * The artifact page below its header: the description, the upstream sync
 * banner the facade hands in, the cron warning, the page's error notices, the
 * publish panel, the artifact body beside the comments sidebar or the companion
 * chat column, the meta line, the thread popover and the activity timeline.
 */
import { memo, useMemo, type ComponentType, type ReactNode, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import WebAppArtifactCard from '../components/WebAppArtifactCard'
import { AlertCircle, AlertTriangle } from 'lucide-react'
import { Btn } from '../components/ui'
import SelectionToolbar from '../components/SelectionToolbar'
import { CommentsSidebar } from '../components/CommentsSidebar'
import { SubmitBar } from '../components/ArtifactSubmitBar'
import { ArtifactChatPanel } from '../components/ArtifactChatPanel'
import { CommentThreadPopover } from '../components/CommentThreadPopover'
// Artifact body renderers, extracted here so the chat side panel shares them.
import { ArtifactBodyNative, ArtifactBodyIframe, ArtifactBodyImage } from '../components/ArtifactBody'
import { PublishHub, publishNoticeKey } from '../components/PublishHub'
import type { Artifact, ArtifactEvent, ArtifactComment } from '../types'

import { i18nT } from '../i18n/t'
import { errMessage } from '../utils/thunkError'
import { fmtDateFields } from '../i18n/format'
import ErrorNotice from '../components/ErrorNotice'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import type { UseQueryResult } from '@tanstack/react-query'
import type {
  ArtifactDetailPageStateRead, ArtifactDetailPageDocumentRead,
} from './useArtifactDetailPageDocument'
import type { ArtifactDetailPageEditorRead } from './useArtifactDetailPageEditor'
import type {
  ArtifactDetailPageCommentsRead, ArtifactDetailPageComposerRead, ArtifactDetailPageThreadsRead,
} from './useArtifactDetailPageComments'
import type { ArtifactDetailPageCompanionRead } from './useArtifactDetailPageCompanion'
import type { ArtifactDetailPageMenuRead } from './ArtifactDetailPageHeader'
import {
  CodeCommentContext, codeFenceSpans, routeCodeComments, buildCodeCommentAnchor, postedCodeRange, type CodeCommentContextValue,
} from '../components/codeComments'
import type { PendingAnchor } from './useArtifactDetailPageComments'
import type { ExternalSelectionState } from '../hooks/useSelectionComposerAnchor'

/** Line-anchored code-block comments. Code fences render in Pierre's shadow
 *  DOM, which the light-DOM selection capture and the highlight overlay's text
 *  walk cannot reach, so code comments ride Pierre's own line-selection /
 *  gutter / annotation primitives instead (see codeComments.ts for the anchor
 *  convention and the routing rules). Returns the context a rendered CodeBlock
 *  consumes (null unless the markdown body is commentable) and the comments
 *  the light-DOM overlay should still paint. */
function useArtifactCodeComments({
  commentable, isMarkdown, sourceContent, durableComments, unreadRootIds, activeCommentId,
  openThreadHandler, stageSelection, lastPosted,
}: {
  commentable: boolean
  isMarkdown: boolean
  sourceContent: string
  durableComments: ArtifactComment[]
  unreadRootIds: Set<string>
  activeCommentId: string | null
  openThreadHandler: (id: string, rect?: { x: number; y: number; w: number; h: number }) => void
  stageSelection: (anchor: PendingAnchor, sel: ExternalSelectionState) => void
  /** The anchor of the last successful post, so its code range can clear. */
  lastPosted?: { startOffset: number; quote: string; seq: number } | null
}): { codeCommentCtx: CodeCommentContextValue | null; overlayComments: ArtifactComment[] } {
  // Routed for every markdown view, commentable or not: a code thread that a
  // fence does not claim falls to the light-DOM overlay, whose quote match
  // would paint an incidental prose copy of the code line (a historical
  // version shows no chips, but must not mis-highlight either).
  const codeSpans = useMemo(
    () => (isMarkdown ? codeFenceSpans(sourceContent) : []),
    [isMarkdown, sourceContent],
  )
  const codeRouting = useMemo(
    () => routeCodeComments(sourceContent, codeSpans, durableComments, unreadRootIds),
    [sourceContent, codeSpans, durableComments, unreadRootIds],
  )
  // Threads a fence claimed are excluded from the light-DOM overlay: a short
  // code quote (`}` and friends) could otherwise mis-highlight an incidental
  // prose occurrence of the same text.
  const overlayComments = useMemo(
    () => (codeRouting.claimedThreadIds.size === 0
      ? durableComments
      : durableComments.filter(c => !codeRouting.claimedThreadIds.has(c.thread_id))),
    [durableComments, codeRouting],
  )
  // The block and lines the last successful post was made on, if it was a
  // code range: that block clears its line selection.
  const postedFor = useMemo(
    () => (lastPosted ? postedCodeRange(sourceContent, codeSpans, lastPosted) : null),
    [lastPosted, sourceContent, codeSpans],
  )
  const codeCommentCtx = useMemo<CodeCommentContextValue | null>(() => {
    if (!commentable || !isMarkdown) return null
    return {
      annotationsFor: fenceStartLine => codeRouting.byFence.get(fenceStartLine) ?? [],
      activeId: activeCommentId,
      onActivate: openThreadHandler,
      postedFor,
      onCommentRange: ({ blockStartLine, start, end, x, y }) => {
        const span = codeSpans.find(s => s.contentStartLine === blockStartLine)
        if (!span) return
        const anchor = buildCodeCommentAnchor(sourceContent, span, start, end)
        if (!anchor) return
        // A stale prose selection would otherwise win over the staged code
        // anchor when the composer opens (it resolves the live DOM range first).
        window.getSelection()?.removeAllRanges()
        // Same composer + post path as a prose selection, opened through the
        // toolbar's external-selection channel. The offsets are SOURCE offsets
        // (the code-comment anchor convention): the post stores them verbatim,
        // and routeCodeComments' slice equality routes the comment back here.
        stageSelection(
          {
            anchor: anchor.quote, line: anchor.line, column: 1, prefix: anchor.prefix, suffix: anchor.suffix,
            startOffset: anchor.startOffset, endOffset: anchor.endOffset,
          },
          { text: anchor.quote, x, y, start: anchor.startOffset },
        )
      },
    }
  }, [commentable, isMarkdown, codeRouting, codeSpans, sourceContent, activeCommentId, openThreadHandler, stageSelection, postedFor])
  return { codeCommentCtx, overlayComments }
}

/** Mounts the code-comment context over the native body and hands it the
 *  comments the light-DOM overlay should paint. A component of its own so the
 *  hooks register here, not in the page view's hook sequence. */
function CodeCommentsScope({ children, ...args }: Parameters<typeof useArtifactCodeComments>[0] & {
  children: (overlayComments: ArtifactComment[]) => ReactNode
}) {
  const { codeCommentCtx, overlayComments } = useArtifactCodeComments(args)
  return <CodeCommentContext.Provider value={codeCommentCtx}>{children(overlayComments)}</CodeCommentContext.Provider>
}

/** Format an ISO timestamp into a short human-readable string for the
 * activity timeline ('5/25/26, 10:31 PM'). Falls back to the raw string
 * if Date parsing fails. */
function formatEventTs(ts: string): string {
  if (!ts) return '?'
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  return fmtDateFields(d, {
    year: '2-digit', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  })
}

/** Lifecycle activity timeline. Renders a chronological feed of
 * created/edited/iterated/referenced/reverted events from the artifact's
 * audit log. */
const ActivityTimeline = memo(function ActivityTimeline({
  events, navigateToSlot,
}: {
  events: ArtifactEvent[]
  navigateToSlot: (slotKey: string) => void
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  if (!events.length) {
    return (
      <div className="text-[12px] text-muted">{i18nT('pages.artifactDetailPage.no_lifecycle_events_yet')}</div>
    )
  }
  // Render newest first so the most recent activity is at the top.
  const ordered = [...events].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
  const verb = (t: ArtifactEvent['type'], md?: ArtifactEvent['metadata']) => {
    if (t === 'comment') {
      const action = typeof md?.action === 'string' ? md.action : ''
      return {
        deleted: i18nT('pages.artifactDetailPage.comment_removed'),
        reviewed: i18nT('pages.artifactDetailPage.comment_marked_for_review'),
        resolved: i18nT('pages.artifactDetailPage.comment_resolved'),
      }[action] ?? i18nT('pages.artifactDetailPage.comment')
    }
    return {
      created: i18nT('pages.artifactDetailPage.created'),
      edited: i18nT('pages.artifactDetailPage.edited'),
      iterated: i18nT('pages.artifactDetailPage.iterated'),
      referenced: i18nT('pages.artifactDetailPage.referenced'),
      reverted: i18nT('pages.artifactDetailPage.reverted'),
    }[t] ?? t
  }
  // Distinct hues per type so created/edited/iterated don't visually blur
  // together. reverted uses warn (orange) to flag its
  // 'undo-style' semantics; iterated uses info (cyan) so agent-driven
  // updates visually separate from user edits (accent/violet).
  const dot = (t: ArtifactEvent['type']) => ({
    created: 'var(--ok)',
    edited: 'var(--accent)',
    iterated: 'var(--info)',
    referenced: 'var(--muted)',
    reverted: 'var(--warn)',
    comment: 'var(--muted)',
  }[t] ?? 'var(--muted)')
  // Some session_id values are markers, not real chat slots — skip the
  // 'from session …' link for those so users don't get sent to the wrong
  // slot. The dashboard's browser client uses 'dashboard:ui' for every
  // request; cron jobs prefix with 'cron:'. Real slot keys never contain
  // a colon (they're random IDs).
  const isRealSlotKey = (sk?: string) =>
    !!sk && sk !== 'dashboard:ui' && !sk.startsWith('cron:') && !sk.startsWith('ui:')
  return (
    <ul className="space-y-1.5 m-0 p-0 list-none">
      {ordered.map((ev, i) => (
        <li key={i} className="flex items-start gap-2 text-[12px]">
          <span
            className="mt-1.5 inline-block w-1.5 h-1.5 rounded-full shrink-0"
            style={{ background: dot(ev.type) }}
            aria-hidden
          />
          <div className="flex-1 min-w-0">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-medium text-text">{verb(ev.type, ev.metadata)}</span>
              {ev.by && <span className="text-muted">{i18nT('pages.artifactDetailPage.by')} {ev.by}</span>}
              {ev.type === 'comment' ? null : ev.type === 'reverted' && ev.from_version != null ? (
                <span className="text-muted">{i18nT('pages.artifactDetailPage.v')}{ev.from_version} {i18nT('pages.artifactDetailPage.v_2')}{ev.version}</span>
              ) : (
                ev.version != null && <span className="text-muted">{i18nT('pages.artifactDetailPage.v_2')}{ev.version}</span>
              )}
              <span className="text-muted ml-auto">{formatEventTs(ev.ts)}</span>
            </div>
            {/* Comment events carry a snippet of the affected comment (and the
                agent's reason on deletes) so the timeline stays readable after
                the comment itself is gone. */}
            {ev.type === 'comment' && typeof ev.metadata?.comment_snippet === 'string' && ev.metadata.comment_snippet ? (
              <div className="text-[11px] text-muted mt-0.5 truncate" title={String(ev.metadata.comment_snippet)}>
                “{ev.metadata.comment_snippet}”
                {typeof ev.metadata.reason === 'string' && ev.metadata.reason ? ` — ${ev.metadata.reason}` : ''}
              </div>
            ) : null}
            {/* Source qualifier under the headline. For real chat slots this
                is a clickable link; for dashboard / cron / unknown markers
                it's plain muted text so users don't think it's actionable. */}
            {ev.session_id && isRealSlotKey(ev.session_id) ? (
              <button
                type="button"
                onClick={() => navigateToSlot(ev.session_id as string)}
                className="text-[11px] text-accent hover:underline cursor-pointer bg-transparent border-none p-0 mt-0.5"
                title={i18nT('pages.artifactDetailPage.open_session', { sessionId: ev.session_id })}
              >
                {i18nT('pages.artifactDetailPage.from_session')} {ev.session_id}
              </button>
            ) : ev.type === 'reverted' && ev.from_version != null ? (
              <span className="text-[11px] text-muted mt-0.5">
                {i18nT('pages.artifactDetailPage.content_copied_from_v')}{ev.from_version}
              </span>
            ) : ev.session_id === 'dashboard:ui' ? (
              <span className="text-[11px] text-muted mt-0.5">{i18nT('pages.artifactDetailPage.via_dashboard')}</span>
            ) : null}
          </div>
        </li>
      ))}
    </ul>
  )
})

/** Everything below the header. */
export function ArtifactDetailPageBody({
  stateRead, documentRead, editorRead, commentsRead, composerRead, companionRead, threadsRead, menuRead,
  artifact, commentsQuery, durableComments, displayCommentCount, commentCount, remoteSyncError, isMobile,
  panel, setPanel, narrowPanelOpen, sidebarUserToggledRef, toggleSidebar, openCompanionChat,
  UpstreamSyncBanner,
}: {
  stateRead: ArtifactDetailPageStateRead
  documentRead: ArtifactDetailPageDocumentRead
  editorRead: ArtifactDetailPageEditorRead
  commentsRead: ArtifactDetailPageCommentsRead
  composerRead: ArtifactDetailPageComposerRead
  companionRead: ArtifactDetailPageCompanionRead
  threadsRead: ArtifactDetailPageThreadsRead
  menuRead: ArtifactDetailPageMenuRead
  artifact: Artifact
  commentsQuery: UseQueryResult<{ comments: ArtifactComment[]; remote_sync_error?: string | null }>
  durableComments: ArtifactComment[]
  displayCommentCount: number
  commentCount: number
  remoteSyncError: string | null
  isMobile: boolean
  panel: 'none' | 'comments' | 'chat'
  setPanel: Dispatch<SetStateAction<'none' | 'comments' | 'chat'>>
  narrowPanelOpen: boolean
  sidebarUserToggledRef: MutableRefObject<boolean>
  toggleSidebar: () => void
  openCompanionChat: (opts?: { address?: boolean }) => Promise<void>
  UpstreamSyncBanner: ComponentType<{ artifact: Artifact; busy?: boolean; onPulled: () => void; onBeforeMutate?: () => Promise<void> }>
}) {
  const {
    slug, queryClient, selectedVersion, editing, previewDuringEdit, editedContent, setEditedContent, saving,
    saveError, saveConflict, contextError, setContextError, contextErrorIsRefresh, sendToSessionError,
    setSendToSessionError, showPublish, setShowPublish,
  } = stateRead
  const {
    iframeScrollTarget, previewRef, bodyRef, iframeBodyRef, detailQuery, eventsQuery, effectiveVersion,
    isCurrent, dirty, usesIframe, contentWidthStyle,
  } = documentRead
  const { reprobing, reprobeError, reprobeNotice, flushLiveEdits } = editorRead
  const {
    commentable, invalidateComments, commentActionError, setCommentActionError, addDocComment, replyComment,
    resolveComment, markReviewComment, reopenComment, removeComment, editComment,
  } = commentsRead
  const {
    selectionComposer, iframeSelection, onSidebarDraftDirtyChange, onThreadDraftDirtyChange,
    sidebarDiscardSignal, composerResetNonce, selectionActions, handleIframeSelect, stageIframeSelection,
    openThread, setOpenThread, lastPosted,
  } = composerRead
  const {
    sendNav, boundSlot, chatCreating, connected, newCompanionChat, pendingComments, pendingCommentIds,
    submittingComments, submitCommentsToChat, openChatFull,
  } = companionRead
  const {
    activeCommentId, bodyScrollNonce, unreadRootIds, openThreadHandler, activateFromSidebar,
  } = threadsRead
  const { copyError, setCopyError } = menuRead
  /** ONE definition, ONE mount point at the top of the chat column: the copy names the composer
   *  beneath it, so a page-top placement would put the instruction a column from what it means. */
  const contextNotice = (
    <>
      {/* No hand-off: this page holds a live editor buffer and an in-progress anchored comment
          selection -- the chat navigation unmounts this tree and discards both. */}
      <ErrorNotice
        message={contextError}
        title={i18nT(contextErrorIsRefresh
          ? 'pages.artifactDetailPage.chat_context_stale_notice_title'
          : 'pages.artifactDetailPage.chat_context_notice_title')}
        onDismiss={() => setContextError(null)}
        className="mb-3 animate-rise"
      />
    </>
  )

  // Cron-source warning shown only while editing — surface the foot-gun
  // (next cron run will create a newer version) without noisy chrome on
  // read-only views.
  const showCronWarning = editing && artifact.source === 'cron'
  return (
    <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
      {artifact.description && (
        <div className="mb-3 text-sm text-muted italic">{artifact.description}</div>
      )}

      {(artifact.fork_metadata || artifact.publication) && (
        <UpstreamSyncBanner artifact={artifact} busy={saving} onBeforeMutate={flushLiveEdits} onPulled={() => {
          queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
          queryClient.invalidateQueries({ queryKey: ['upstream-status', slug] })
        }} />
      )}

      {showCronWarning && (
        <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-warn/40 bg-warn-subtle text-[13px] text-warn-text">
          <AlertCircle size={14} className="lucide-inline shrink-0 mt-0.5" />
          <span>
            <strong>{i18nT('pages.artifactDetailPage.heads_up')}</strong> {i18nT('pages.artifactDetailPage.this_artifact_is_regenerated_by_a_cron_job_your')}
          </span>
        </div>
      )}

      {/* No hand-off: editor buffer editedContent (unsaved). `saveError` is set
          exactly when handleSave threw, so `dirty` is still true and the buffer
          was never persisted — a route change unmounts this page and it is
          gone. Every other nav-away on this page gates on
          `dirty && confirm(discard_unsaved_changes)`, and the deleted-artifact
          handler sets `saveError` INSTEAD of navigating precisely so the user
          can copy their work out. A one-click navigation off this surface would
          destroy it, and would bypass the beforeunload guard too. The conflict
          footer's link opens a NEW tab, so this page and its buffer stay put. */}
      <ErrorNotice
        message={saveError}
        title={saveConflict
          ? i18nT('pages.artifactDetailPage.save_conflict_title')
          : i18nT('pages.artifactDetailPage.save_failed')}
        footer={saveConflict && (
          <a
            href={`/artifacts/${encodeURIComponent(slug)}`}
            target="_blank"
            rel="noopener noreferrer"
            className="underline hover:no-underline"
          >
            {i18nT('pages.artifactDetailPage.save_conflict_open_newer_in_new_tab')}
          </a>
        )}
        className="mb-3"
      />

      {/* A failed "New session" hand-off. Not hidden in the trigger's tooltip:
          the menu has closed by the time the create rejects, so this is the
          only place the failure is readable. */}
      {/* No hand-off: comment draft (sidebar / selection composer text) */}
      <ErrorNotice
        message={sendToSessionError}
        title={i18nT('pages.artifactDetailPage.send_to_session_failed')}
        messagePlacement="below"
        onDismiss={() => setSendToSessionError(null)}
        className="mb-3"
      />

      {/* A failed Copy. */}
      {/* No hand-off: comment draft (sidebar / selection composer text) */}
      <ErrorNotice
        message={copyError}
        onDismiss={() => setCopyError(null)}
        className="mb-3"
      />

      {/* Read-only publication sync-error surface: keeps a persisted sync
          error visible (no controls) if a publishing provider is ever
          registered. Inert in the public edition, where the registry is empty
          and `artifact.publication` is always null. */}
      {/* No hand-off: editor buffer editedContent may be dirty */}
      <ErrorNotice
        title={i18nT('pages.artifactDetailPage.publication_sync_issue')}
        message={artifact.publication?.last_error}
        className="mb-3"
      />

      {/* Comments that failed to LOAD would otherwise read as "no comments"
          (the sidebar and the toolbar count both fall back to an empty list). */}
      {/* No hand-off: editor buffer editedContent may be dirty */}
      <ErrorNotice
        title={i18nT('pages.artifactDetailPage.comments_failed_to_load')}
        message={commentsQuery.error ? (errMessage(commentsQuery.error) || i18nT('components.errorBoundary.something_went_wrong')) : null}
        className="mb-3"
      />
      {/* No hand-off: comment draft (sidebar / selection composer text) */}
      <ErrorNotice
        title={i18nT('pages.artifactDetailPage.comment_action_failed')}
        message={commentActionError}
        onDismiss={() => setCommentActionError(null)}
        className="mb-3"
      />

      {/* Notice-only publication: the publish SUCCEEDED and the link is valid,
          it just is not reachable yet (e.g. CloudFront still rolling out). That
          is not a failure, so it renders as a neutral/warn line -- never the
          danger surface `last_error` drives. Suppressed when a real error is
          present, since that is the more important thing to show. */}
      {artifact.publication?.notice && !artifact.publication.last_error && (
        <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-warn/30 bg-warn-subtle text-[13px] text-warn-text">
          <AlertTriangle className="lucide-inline shrink-0 mt-0.5" />
          <span className="min-w-0 flex-1">{i18nT(publishNoticeKey({
            rolling_out: 'pages.artifactDetailPage.publication_still_rolling_out',
            distribution_disabled: 'pages.artifactDetailPage.publication_distribution_disabled',
            notice_generic: 'pages.artifactDetailPage.publication_notice_generic',
          }, artifact.publication.notice_code))}</span>
          {/* A notice is recorded once, at publish time, and the ordinary happy path
              never revisits it -- so a link that HAS since finished rolling out kept
              this banner forever. This asks the destination again and clears the notice
              only when the condition really cleared. User-triggered rather than polled:
              the answer costs a call to the destination, and a timer would either clear
              it without checking or hammer the destination on every visit. */}
          <Btn
            onClick={reprobeNotice}
            disabled={reprobing}
            aria-label={i18nT('pages.artifactDetailPage.recheck_publication_notice')}
          >
            {reprobing
              ? i18nT('pages.artifactDetailPage.rechecking')
              : i18nT('pages.artifactDetailPage.check_again')}
          </Btn>
          {/* Rendered here, beside the button that triggered it: a re-check failure used
              to be written to the editor's save-error channel, which both mislabelled it
              and put it far from the control the user just pressed.
              No hand-off: this panel sits on the artifact detail page alongside an
              editable buffer, and `askAgent` navigates away and destroys unsaved edits.
              A failed re-check is retryable in place by pressing the button again, so the
              hand-off would cost more than it could recover. */}
          {reprobeError && (
            <ErrorNotice message={reprobeError} className="mt-2" />
          )}
        </div>
      )}

      {/* Publish panel — toggled by the Publish toolbar button */}
      {showPublish && artifact.kind !== 'webapp' && artifact.kind !== 'image' && (
        <div className="mb-3">
          <PublishHub artifact={artifact} onClose={() => setShowPublish(false)} />
        </div>
      )}

      {/* One flex row for EVERY kind, so the right-hand panels are siblings of
          whatever body is rendered. The toolbar's chat and comments toggles
          render unconditionally, so scoping the row to the non-webapp branch
          would let a click on a webapp artifact create/activate a session with
          nowhere to display. */}
      <div className="flex gap-4 items-start">
        {/* An open panel owns the width while narrow, so the artifact body
            steps aside -- giving the panel `w-full` alone would still leave the
            two of them splitting 390px. Hidden rather than unmounted: the body
            holds scroll position and, for markdown, an in-progress anchored
            comment selection, and rotating a phone crosses the breakpoint. */}
        <div className={`flex-1 min-w-0 ${narrowPanelOpen ? 'hidden' : ''}`}>
          {artifact.kind === 'webapp' ? (
            <WebAppArtifactCard artifact={artifact} />
          ) : artifact.kind === 'image' ? (
            <ArtifactBodyImage artifact={artifact} slug={slug} />
          ) : usesIframe ? (
            <div ref={iframeBodyRef}>
              <ArtifactBodyIframe
                artifact={artifact}
                slug={slug}
                comments={durableComments}
                onSelect={handleIframeSelect}
                onOpenThread={(id: string, rect) => openThreadHandler(id, rect)}
                scrollToCommentId={iframeScrollTarget}
                activeId={activeCommentId}
                unreadRootIds={unreadRootIds}
              />
              {/* The frame's selections arrive through the bridge as
                  `iframeSelection`; the toolbar opens the same composer from
                  it, and from nothing else — the wrapper's own text (a
                  render-failure notice) is not part of the artifact. Gated
                  like the native body: a comment is stored against the
                  CURRENT artifact, so a historical snapshot takes none. */}
              {isCurrent && !editing && <SelectionToolbar key={`${slug}:${composerResetNonce}`} containerRef={iframeBodyRef} actions={selectionActions} composer={selectionComposer} externalSelection={iframeSelection} externalOnly suspended={narrowPanelOpen} />}
            </div>
          ) : (
            <div
              ref={bodyRef}
              className="relative"
              style={contentWidthStyle}
            >
              <CodeCommentsScope
                commentable={commentable} isMarkdown={artifact.kind === 'markdown'}
                sourceContent={artifact.content ?? ''} durableComments={durableComments}
                unreadRootIds={unreadRootIds} activeCommentId={activeCommentId}
                openThreadHandler={openThreadHandler} stageSelection={stageIframeSelection}
                lastPosted={lastPosted}
              >{overlayComments => (
                <ArtifactBodyNative
                  kind={artifact.kind}
                  content={editing ? editedContent : (artifact.content ?? '')}
                  // SVG shows preview AND source together while editing, so the
                  // preview toggle does not apply to it — and must not gate it:
                  // the toggle is hidden for SVG, so honoring a stale `true`
                  // here would strand the editor with no control to restore it.
                  editing={editing && (artifact.kind === 'svg' || !previewDuringEdit)}
                  onChange={setEditedContent}
                  previewRef={previewRef}
                  comments={overlayComments}
                  activeCommentId={activeCommentId}
                  scrollNonce={bodyScrollNonce}
                  onActivateComment={openThreadHandler}
                  unreadRootIds={unreadRootIds}
                />
              )}</CodeCommentsScope>
              {/* Selecting text opens the type-first comment box; mounted only
                  while an anchored comment can be made (current version, not
                  editing, a kind whose renderer attaches `previewRef`). It
                  listens on the preview itself, not the wrapper: the wrapper
                  also holds the inline-comment overlay's own text (gutter
                  counts), which no anchor can point at. */}
              {/* Keyed per artifact: the route element is reused across a
                  param-only navigation, and a toolbar that survived it would
                  submit the previous artifact's draft through this one's callbacks. */}
              {/* `externalSelection` carries a code-block line range (Pierre's
                  shadow DOM holds no light-DOM selection to read). */}
              {commentable && <SelectionToolbar key={`${slug}:${composerResetNonce}`} containerRef={previewRef} actions={selectionActions} composer={selectionComposer} externalSelection={iframeSelection} suspended={narrowPanelOpen} />}
            </div>
          )}
        </div>

        {/* Shared right-hand panel space: comments sidebar and companion chat
            panel are mutually exclusive flex siblings of the artifact body —
            icon-toggled, never overlays. The comment stack is durable,
            threaded, and works for ALL kinds (doc-level add for HTML/widget;
            anchored add for markdown/text via the selection composer above). */}
        {panel === 'comments' && (
          <CommentsSidebar
            comments={durableComments}
            onDraftDirtyChange={onSidebarDraftDirtyChange}
            discardSignal={sidebarDiscardSignal}
            loading={commentsQuery.isFetching}
            remoteSyncError={remoteSyncError}
            onAdd={addDocComment}
            onReply={replyComment}
            onResolve={resolveComment}
            onMarkReview={markReviewComment}
            onReopen={reopenComment}
            onDelete={removeComment}
            onRefresh={invalidateComments}
            onAskAgent={commentCount > 0 ? () => { void openCompanionChat({ address: true }) } : undefined}
            submitBar={pendingComments.length > 0 && boundSlot ? (
              <SubmitBar
                // A new artifact gets a fresh bar: a refusal notice and a
                // handed-back instruction belong to the artifact they were for.
                key={slug}
                count={pendingComments.length}
                submitting={submittingComments}
                onSubmit={submitCommentsToChat}
                connected={connected}
                stacked
              />
            ) : undefined}
            pendingSubmitIds={boundSlot ? pendingCommentIds : undefined}
            onClose={toggleSidebar}
            onCommentClick={activateFromSidebar}
            onEditComment={editComment}
            activeCommentId={activeCommentId}
          />
        )}
        {/* EVERY breakpoint, not just desktop: mobile HIDES the body while a panel is open, so a
            !isMobile gate here left a mobile chat with neither body nor panel -- a blank page. */}
        {panel === 'chat' && (
          <div className={`flex flex-col min-h-0 h-[calc(100vh-240px)] ${isMobile ? 'flex-1 min-w-0' : 'w-[480px] shrink-0'}`}>
            {/* IN THIS COLUMN, its only mount: the remedy names the composer below it, and a
                page-top banner puts that instruction a column away from the control it means. */}
            {contextNotice}
            <ArtifactChatPanel
              slotKey={boundSlot?.key ?? null}
              creating={chatCreating}
              onNewChat={() => { void newCompanionChat() }}
              onOpenFull={openChatFull}
              onClose={() => { sidebarUserToggledRef.current = true; setPanel('none') }}
            />
          </div>
        )}
      </div>

      <div className="mt-3 text-[12px] text-muted">
        {i18nT('pages.artifactDetailPage.created')} {artifact.created_at} {i18nT('pages.artifactDetailPage.updated')} {artifact.updated_at} {"\u00b7"}{' '}
        {/* "Live" reflects the always-current state. Numbered versions
            are historical snapshots — when one is selected, isCurrent is
            false (because the dropdown is non-Live). */}
        {selectedVersion === null
          ? i18nT('pages.artifactDetailPage.showing_live_v', { version: detailQuery.data?.version ?? '?' })
          : i18nT('pages.artifactDetailPage.showing_v_historical', { version: effectiveVersion })}
        {dirty && <span className="ml-2 text-warn">{i18nT('pages.artifactDetailPage.unsaved_changes')}</span>}
        {commentable && displayCommentCount === 0 && (
          <span className="ml-2 text-muted/80">{i18nT('pages.artifactDetailPage.tip_select_text_to_anchor_a_comment_or_use_the')} <strong>{i18nT('pages.artifactDetailPage.comments')}</strong> {i18nT('pages.artifactDetailPage.panel_to_add_one')}</span>
        )}
        {!commentable && !editing && isCurrent && (
          <span className="ml-2 text-muted/80">
            {i18nT('pages.artifactDetailPage.tip_use_the')} <strong>{i18nT('pages.artifactDetailPage.comments')}</strong> {i18nT('pages.artifactDetailPage.panel_to_comment')}
            {i18nT('pages.artifactDetailPage.or')} <strong>{i18nT('pages.artifactDetailPage.agent_chat')}</strong> {i18nT('pages.artifactDetailPage.to_chat_with_the_agent')}.
          </span>
        )}
      </div>

      {openThread && (
        <CommentThreadPopover
          comments={durableComments}
          rootId={openThread.rootId}
          rect={openThread.rect}
          onClose={() => setOpenThread(null)}
          onReply={replyComment}
          onResolve={resolveComment}
          onMarkReview={markReviewComment}
          onReopen={reopenComment}
          onDelete={removeComment}
          onEditComment={editComment}
          onDraftDirtyChange={onThreadDraftDirtyChange}
        />
      )}

      {/* Lifecycle event log + activity timeline. */}
      <div className="mt-6">
        <h3 className="text-[13px] font-semibold text-text-strong mb-2">{i18nT('pages.artifactDetailPage.activity')}</h3>
        {/* No hand-off: editor buffer editedContent may be dirty */}
        <ErrorNotice
          title={i18nT('pages.artifactDetailPage.activity_failed_to_load')}
          message={eventsQuery.error ? (errMessage(eventsQuery.error) || i18nT('components.errorBoundary.something_went_wrong')) : null}
          className="mb-2"
        />
        <ActivityTimeline
          events={eventsQuery.data?.events ?? []}
          navigateToSlot={(slotKey) => sendNav({ path: '/chat', slotKey })}
        />
      </div>
    </div>
  )
}
