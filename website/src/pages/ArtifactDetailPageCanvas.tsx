/**
 * A canvas artifact's own page: the board fills everything under the app's top bar,
 * and the page's controls float over it as one pill in the top-left corner, as in
 * FigJam.
 *
 * The pill holds two groups: Back and the title (click to rename) with the save state,
 * then the version, the agent chat and a menu. The menu holds Star, Comments, Tags,
 * Activity, Snapshot, Copy, Send to a session and Download; on a past version it leads
 * with Revert instead of Star and Tags. The page's failure notices and the generic page's
 * publication status float under the pill. Tags open in a popover under the pill; comments,
 * activity and the agent chat each open as the one full-height column on the right.
 *
 * The pill and the page's notices sit outside the lazily loaded board, so Back, the version,
 * the menu and the chat stay while its chunk loads, fails to load or throws; the board
 * reports its save state up and renders its own notices into a slot under the pill.
 *
 * Narrow: the controls' layer stops short of the board's top-right corner, which the
 * editor's zoom control holds, and the title truncates; on a phone an open column takes
 * the whole width and the board steps aside.
 *
 * Everything this page reads (the record, versions, rename, star, the menu's actions)
 * is the generic page's own state, passed in, so the two pages cannot disagree about
 * an artifact.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { ArrowLeft, Camera, Check, ChevronDown, Copy, Download, History, MessageSquare, MoreHorizontal, RotateCcw, Sparkles, Star, Tag, Upload, X } from 'lucide-react'
import { Input } from '../components/ui'
import { ArtifactChatPanel } from '../components/ArtifactChatPanel'
import { CommentsSidebar } from '../components/CommentsSidebar'
import { SubmitBar } from '../components/ArtifactSubmitBar'
import { ArtifactTagChips } from '../components/ArtifactTagChips'
import { Popover, PopoverAnchor, PopoverContent } from '../components/ui/popover'
import ErrorNotice from '../components/ErrorNotice'
import type { ArtifactDetailPageCompanionRead } from './useArtifactDetailPageCompanion'
import HoverTip from '../components/HoverTip'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '../components/ui/dropdown-menu'
import { ArtifactSendToSessionSubmenu } from '../components/ArtifactSendToSession'
import { ArtifactPopoutMenuItems } from '../components/ArtifactPopoutMenuItems'
import { PublishHub } from '../components/PublishHub'
import { CanvasArtifactBody } from '../components/canvas/lazyCanvas'
import { discardCanvas, flushCanvas } from '../components/canvas/canvasPending'
import { FLOAT_SHADOW } from '../components/canvas/canvasPaint'
import { fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { errMessage } from '../utils/thunkError'
import type { Artifact, ArtifactComment } from '../types'
import {
  ActivityTimeline, ArtifactPublicationNotices, ArtifactUpstreamSync, type UpstreamSyncBannerComponent,
} from './ArtifactDetailPageBody'
import type { ArtifactDetailPageDocumentRead, ArtifactDetailPageStateRead } from './useArtifactDetailPageDocument'
import type { ArtifactDetailPageEditorRead } from './useArtifactDetailPageEditor'
import type {
  ArtifactDetailPageCommentsRead, ArtifactDetailPageComposerRead, ArtifactDetailPageThreadsRead,
} from './useArtifactDetailPageComments'
import type { ArtifactDetailPageMenuRead } from './ArtifactDetailPageHeader'

const ICON_BTN = 'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-text hover:bg-bg-hover disabled:opacity-40'

interface Props {
  artifact: Artifact
  popout: boolean
  stateRead: ArtifactDetailPageStateRead
  documentRead: ArtifactDetailPageDocumentRead
  editorRead: ArtifactDetailPageEditorRead
  composerRead: ArtifactDetailPageComposerRead
  menuRead: ArtifactDetailPageMenuRead
  companionRead: ArtifactDetailPageCompanionRead
  isMobile: boolean
  panel: 'none' | 'comments' | 'chat'
  setPanel: Dispatch<SetStateAction<'none' | 'comments' | 'chat'>>
  sidebarUserToggledRef: MutableRefObject<boolean>
  openCompanionChat: (opts?: { address?: boolean }) => Promise<void>
  handleRevert: () => Promise<void>
  commentsRead: ArtifactDetailPageCommentsRead
  threadsRead: ArtifactDetailPageThreadsRead
  commentsQuery: UseQueryResult<{ comments: ArtifactComment[]; remote_sync_error?: string | null }>
  durableComments: ArtifactComment[]
  commentCount: number
  remoteSyncError: string | null
  UpstreamSyncBanner: UpstreamSyncBannerComponent
}

/** The column on the right, when one is open. The chat is the generic page's `panel`;
 *  comments and activity are this page's own, so the generic page's auto-reveal of the
 *  comments panel (written for a document beside it) never takes width from the board. */
type Column = 'chat' | 'comments' | 'activity' | 'publish'
type OwnColumn = Exclude<Column, 'chat'>

const COLUMN_HEAD = 'flex shrink-0 items-center gap-1 border-b border-border px-2 py-1.5'

/** Mounted beside the board's load-failure notice, so the page knows that notice is showing. */
function ReportBoardFailure({ onFailed }: { onFailed: (failed: boolean) => void }): ReactNode {
  useEffect(() => {
    onFailed(true)
    return () => onFailed(false)
  }, [onFailed])
  return null
}

export default function ArtifactDetailPageCanvas({
  artifact, popout, stateRead, documentRead, editorRead, composerRead, menuRead, companionRead,
  isMobile, panel, setPanel, sidebarUserToggledRef, openCompanionChat, handleRevert, commentsRead, threadsRead,
  commentsQuery, durableComments, commentCount, remoteSyncError, UpstreamSyncBanner,
}: Props) {
  const {
    slug, navigate, confirm, renaming, nameDraft, setNameDraft, selectedVersion, setSelectedVersion, saving, ime,
    contextError, setContextError, contextErrorIsRefresh, saveError, saveConflict, sendToSessionError,
    setSendToSessionError,
  } = stateRead
  const {
    boundSlot, chatCreating, newCompanionChat, openChatFull, sendNav, pendingComments, pendingCommentIds,
    submittingComments, submitCommentsToChat, connected,
  } = companionRead
  const chatOpen = panel === 'chat'
  const [own, setOwn] = useState<OwnColumn | null>(null)
  const [tagsOpen, setTagsOpen] = useState(false)
  // Set when Tags is picked, read once the menu has finished closing: the menu hands focus
  // back to its trigger as it closes, and a popover already open would take that focus
  // move as an outside interaction and close at once.
  const tagsPickedRef = useRef(false)
  // The board's save state, or why it is read-only, for the pill; null until it has loaded.
  const [status, setStatus] = useState<string | null>(null)
  // Where the board puts its own notices: under the pill and the page's notices.
  const [boardNotices, setBoardNotices] = useState<HTMLDivElement | null>(null)
  // While the board's chunk failed, its failure notice takes the board's place in the column;
  // the controls then sit above it in the flow instead of floating over it.
  const [boardFailed, setBoardFailed] = useState(false)
  // Opening the chat (from the pill or anywhere else) replaces this page's own column.
  useEffect(() => { if (panel === 'chat') setOwn(null) }, [panel])
  const column: Column | null = chatOpen ? 'chat' : own
  /** Toggles comments, activity or publish, closing the chat if it holds the column. */
  const toggleOwn = (next: OwnColumn) => {
    if (chatOpen) { sidebarUserToggledRef.current = true; setPanel('none') }
    setOwn(cur => (cur === next && !chatOpen ? null : next))
  }
  // On a phone an open column owns the width; a board squeezed beside it would be nothing.
  const narrowColumnOpen = isMobile && column !== null
  const {
    invalidateComments, commentActionError, setCommentActionError, addDocComment, replyComment, resolveComment,
    markReviewComment, reopenComment, removeComment, editComment,
  } = commentsRead
  const { activeCommentId, activateFromSidebar } = threadsRead
  const { onSidebarDraftDirtyChange, sidebarDiscardSignal } = composerRead
  // The selection-composer guard does not see the sidebar's local add/reply/edit
  // drafts. Keep a dirty sidebar mounted across ALL column changes, including an
  // async submit opening chat, rather than guarding only the pill's entry points.
  const [sidebarDraftDirty, setSidebarDraftDirty] = useState(false)
  const reportSidebarDraft = useCallback((dirty: boolean) => {
    setSidebarDraftDirty(dirty)
    onSidebarDraftDirtyChange(dirty)
  }, [onSidebarDraftDirtyChange])
  const { addTag, removeTag } = editorRead
  const { versions, isCurrent, eventsQuery, versionsQuery } = documentRead
  const { pinning, togglePin, titleButtonRef, startRenaming, commitRename, cancelRename, endEditing, handleSnapshotLive } = editorRead
  const { guardCommentDraft } = composerRead
  const {
    copyStatus, copyError, setCopyError, handleCopyContent, copyLabel, moreOpen, setMoreOpen, sendToSession, downloadAsHtml,
  } = menuRead

  const versionsDesc = versions.slice().reverse()
  const versionOptions = ['live', ...versionsDesc.map(String)]
  const versionValue = selectedVersion === null ? 'live' : String(selectedVersion)
  const versionLabels = [
    i18nT('pages.artifactDetailPage.live'),
    ...versionsDesc.map(v => i18nT('pages.artifactDetailPage.version_label', { version: fmtNumber(v, { useGrouping: false }) })),
  ]

  /** Leaves or switches only after the waiting save landed, or the person agreed to drop it. */
  const mayDrop = async () => {
    const saved = await flushCanvas(slug)
    if (saved) return true
    const ok = await confirm({
      title: i18nT('pages.artifactDetailPage.discard_unsaved_changes'),
      confirmLabel: i18nT('pages.artifactDetailPage.discard_changes_button'),
    })
    if (ok) discardCanvas(slug)
    return ok
  }

  return (
    <div className="flex min-h-0 flex-1" data-testid="canvas-page">
      {/* Hidden rather than unmounted while a phone's chat is open: the board holds a
          waiting autosave and the view position, and rotating a phone crosses the breakpoint. */}
      <div className={`relative flex min-h-0 min-w-0 flex-1 flex-col ${narrowColumnOpen ? 'hidden' : ''}`} data-testid="canvas-page-board">
        {/* The page's controls, over the board's top-left corner and above its layers. The
            layer ends 5.5rem from the right, so on any board width it never covers the
            editor's zoom control (right-3, a 56px button in p-1.5 = 5rem) or takes its taps.
            A failed board renders no editor, so the controls then take the full width. */}
        <div data-testid="canvas-page-controls" className={`pointer-events-none flex flex-col items-start gap-2 ${boardFailed ? 'm-3' : 'absolute left-3 right-[5.5rem] top-3'}`} style={{ zIndex: 10 }}>
            {/* Only the pill and each notice take pointer events: this column sizes to its widest
                notice, and its empty area and gaps beside the pill are board the person can draw on. */}
            <div data-testid="canvas-page-controls-column" className="pointer-events-none flex max-w-full flex-col items-start gap-2">
              <Popover open={tagsOpen} onOpenChange={setTagsOpen}>
              <PopoverAnchor asChild>
              <div data-testid="canvas-page-pill" className="pointer-events-auto flex max-w-full min-w-0 items-center gap-1 rounded-2xl bg-bg-elevated p-1.5 text-text" style={{ boxShadow: FLOAT_SHADOW }}>
                {!popout && (
                  <HoverTip label={i18nT('pages.artifactDetailPage.back')}>
                    <button type="button" className={ICON_BTN} aria-label={i18nT('pages.artifactDetailPage.back')} data-testid="canvas-page-back"
                      onClick={async () => { if (await mayDrop()) navigate('/artifacts') }}>
                      <ArrowLeft size={16} />
                    </button>
                  </HoverTip>
                )}
                {renaming ? (
                  <Input
                    autoFocus
                    value={nameDraft}
                    aria-label={i18nT('pages.artifactDetailPage.artifact_name')}
                    onChange={e => setNameDraft(e.target.value)}
                    {...ime.bindEnter({ onEnter: () => void commitRename(true), onEscape: cancelRename, onBlur: () => void commitRename(false) })}
                    className="h-8 w-56 px-2 text-[14px] font-semibold"
                  />
                ) : (
                  // min-w-0 down to the button, through HoverTip's own wrapper, so a long title truncates.
                  <span className="flex min-w-0 [&>span]:min-w-0">
                    <HoverTip label={i18nT('pages.artifactDetailPage.rename_this_artifact')}>
                      <button ref={titleButtonRef} type="button" onClick={startRenaming} data-testid="canvas-page-title"
                        className="min-w-0 max-w-[18rem] truncate rounded-lg px-2 py-1 text-left text-[14px] font-semibold text-text-strong hover:bg-bg-hover">
                        {artifact.name}
                      </button>
                    </HoverTip>
                  </span>
                )}
                {/* Yields first when the pill runs out of room. */}
                {status && <span className="min-w-0 shrink-[4] truncate px-1 text-[11px] text-muted" aria-live="polite" data-testid="canvas-save-status">{status}</span>}
                <span aria-hidden className="mx-1 h-5 w-px shrink-0 bg-border" />
                {/* A plain button opening a menu, not a select: the pill's flat look is this page's
                    own styling, and the shared select primitives own theirs. */}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button type="button" disabled={saving} aria-label={i18nT('pages.artifactDetailPage.version')}
                      data-testid="canvas-page-version"
                      className="flex h-8 shrink-0 items-center gap-1 rounded-lg px-2 text-[12px] text-text hover:bg-bg-hover disabled:opacity-40">
                      <span>{versionLabels[versionOptions.indexOf(versionValue)] ?? versionLabels[0]}</span>
                      <ChevronDown size={13} className="text-muted" aria-hidden="true" />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" className="min-w-[160px] max-h-[min(360px,var(--radix-dropdown-menu-content-available-height))] overflow-y-auto">
                    <DropdownMenuRadioGroup
                      value={versionValue}
                      onValueChange={async raw => {
                        if (raw === versionValue || !(await mayDrop())) return
                        await guardCommentDraft(() => {
                          endEditing()
                          setSelectedVersion(raw === 'live' ? null : parseInt(raw, 10))
                        })
                      }}
                    >
                      {versionOptions.map((v, i) => (
                        <DropdownMenuRadioItem key={v} value={v}>{versionLabels[i]}</DropdownMenuRadioItem>
                      ))}
                    </DropdownMenuRadioGroup>
                  </DropdownMenuContent>
                </DropdownMenu>
                <HoverTip label={i18nT('pages.artifactDetailPage.chat_with_the_agent_about_this_artifact')}>
                  <button type="button" data-testid="canvas-page-chat"
                    className={`${ICON_BTN} ${chatOpen ? 'bg-accent-subtle text-accent' : ''}`}
                    aria-label={i18nT('pages.artifactDetailPage.toggle_agent_chat')} aria-pressed={chatOpen}
                    onClick={() => { void openCompanionChat() }}>
                    <Sparkles size={15} />
                  </button>
                </HoverTip>
                <DropdownMenu open={moreOpen} onOpenChange={setMoreOpen}>
                  <DropdownMenuTrigger asChild>
                    <button type="button" className={ICON_BTN} aria-label={i18nT('pages.artifactDetailPage.more_actions')} data-testid="canvas-page-more">
                      <MoreHorizontal size={16} />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" className="min-w-[220px]"
                    onCloseAutoFocus={e => {
                      if (!tagsPickedRef.current) return
                      tagsPickedRef.current = false
                      e.preventDefault()
                      setTagsOpen(true)
                    }}>
                    {/* A past version is read-only; Revert writes it as the new live version. The
                        generic page shows this as a button beside its picker; here it leads the
                        menu so the pill keeps two buttons. handleRevert asks first. */}
                    {!isCurrent && (
                      <>
                        <DropdownMenuItem disabled={saving} onSelect={() => { void handleRevert() }} data-testid="canvas-page-menu-revert">
                          <RotateCcw size={13} className="shrink-0 text-warn" aria-hidden="true" />
                          <span>{i18nT('pages.artifactDetailPage.revert_to_v', { version: selectedVersion })}</span>
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                      </>
                    )}
                    {/* Only on the current version, as on the generic page: a snapshot does not
                        carry the live record's `pinned`. */}
                    {isCurrent && (
                      <DropdownMenuItem disabled={pinning} onSelect={() => { void togglePin() }}>
                        <Star size={13} className={`shrink-0 ${artifact.pinned ? 'fill-current text-warn' : 'text-muted'}`} aria-hidden="true" />
                        <span>{artifact.pinned ? i18nT('pages.artifactsPage.remove_star_from_artifact') : i18nT('pages.artifactsPage.star_artifact')}</span>
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuItem onSelect={() => toggleOwn('comments')} data-testid="canvas-page-menu-comments">
                      <MessageSquare size={13} className="shrink-0 text-muted" aria-hidden="true" />
                      <span>{column === 'comments' ? i18nT('pages.artifactDetailPage.hide_comments') : i18nT('pages.artifactDetailPage.show_comments')}</span>
                    </DropdownMenuItem>
                    {isCurrent && (
                      <DropdownMenuItem onSelect={() => { tagsPickedRef.current = true }} data-testid="canvas-page-menu-tags">
                        <Tag size={13} className="shrink-0 text-muted" aria-hidden="true" />
                        <span>{i18nT('pages.artifactsPage.tags')}</span>
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuItem onSelect={() => toggleOwn('activity')} data-testid="canvas-page-menu-activity">
                      <History size={13} className="shrink-0 text-muted" aria-hidden="true" />
                      <span>{i18nT('pages.artifactDetailPage.activity')}</span>
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    {isCurrent && artifact.live_dirty && (
                      <DropdownMenuItem disabled={saving} onSelect={handleSnapshotLive}>
                        <Camera size={13} className="shrink-0 text-muted" aria-hidden="true" />
                        <span>{i18nT('pages.artifactDetailPage.snapshot')}</span>
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuItem onSelect={e => { e.preventDefault(); handleCopyContent() }}>
                      {copyStatus === 'copied'
                        ? <Check size={13} className="shrink-0 text-ok" aria-hidden="true" />
                        : <Copy size={13} className="shrink-0 text-muted" aria-hidden="true" />}
                      <span aria-live="polite">{copyLabel}</span>
                    </DropdownMenuItem>
                    <ArtifactSendToSessionSubmenu state={sendToSession} />
                    {!popout && <ArtifactPopoutMenuItems slug={slug} name={artifact.name} />}
                    <DropdownMenuSeparator />
                    {/* The generic page's one publish surface, opened in this page's side column. */}
                    <DropdownMenuItem role="menuitemcheckbox" aria-checked={column === 'publish'} onSelect={() => toggleOwn('publish')} data-testid="canvas-page-menu-publish">
                      <Upload size={13} className={`shrink-0 ${column === 'publish' ? 'text-accent' : 'text-muted'}`} aria-hidden="true" />
                      <span>{i18nT('pages.artifactDetailPage.publish')}</span>
                      {column === 'publish' && <Check size={13} className="ml-auto shrink-0 text-accent" aria-hidden="true" />}
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={downloadAsHtml}>
                      <Download size={13} className="shrink-0 text-muted" aria-hidden="true" />
                      <span>{i18nT('pages.artifactDetailPage.download')}</span>
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
              </PopoverAnchor>
              {/* The tags of the record on screen; only the current version can change them. */}
              <PopoverContent align="start" className="w-[min(320px,calc(100vw-1.5rem))]" data-testid="canvas-page-tags">
                <div className="mb-2 text-[12px] font-semibold text-text-strong">{i18nT('pages.artifactsPage.tags')}</div>
                <div className="flex flex-wrap items-center gap-2">
                  <ArtifactTagChips tags={artifact.tags} addTag={addTag} removeTag={removeTag} stateRead={stateRead} />
                </div>
              </PopoverContent>
              </Popover>
              <div data-testid="canvas-page-notices" className="flex w-full max-w-[520px] flex-col gap-2 empty:hidden [&>*]:pointer-events-auto">
                {/* The generic page's publication status, as it shows it. Before a pull, overwrite or
                    snapshot replaces the live content, the board's waiting autosave goes out; one that
                    fails throws, so the banner stops and shows the error instead of acting on the
                    stored content. No hand-off: the board may hold a waiting autosave and the
                    comments column a draft. */}
                <ArtifactUpstreamSync artifact={artifact} stateRead={stateRead} UpstreamSyncBanner={UpstreamSyncBanner}
                  onBeforeMutate={async () => {
                    if (!(await flushCanvas(slug))) throw new Error(i18nT('components.canvas.save_failed_title'))
                  }} />
                <ArtifactPublicationNotices artifact={artifact} editorRead={editorRead} />
                {/* No hand-off: the board may hold a waiting autosave not yet saved anywhere. */}
                <ErrorNotice
                  variant="inline"
                  title={i18nT('pages.artifactDetailPage.versions_failed_to_load')}
                  message={versionsQuery.error ? (errMessage(versionsQuery.error) || i18nT('components.errorBoundary.something_went_wrong')) : null}
                />
                {/* A failed star, rename, Snapshot or the deleted-artifact notice.
                    No hand-off: the board may hold a waiting autosave not yet saved anywhere,
                    and the hand-off navigates away from it. */}
                <ErrorNotice
                  message={saveError}
                  title={saveConflict
                    ? i18nT('pages.artifactDetailPage.save_conflict_title')
                    : i18nT('pages.artifactDetailPage.save_failed')}
                />
                {/* A failed "New session" hand-off; the menu has closed by the time it rejects.
                    No hand-off: the board may hold a waiting autosave not yet saved anywhere. */}
                <ErrorNotice
                  message={sendToSessionError}
                  title={i18nT('pages.artifactDetailPage.send_to_session_failed')}
                  messagePlacement="below"
                  onDismiss={() => setSendToSessionError(null)}
                />
                {/* A failed Copy or Download.
                    No hand-off: the board may hold a waiting autosave not yet saved anywhere. */}
                <ErrorNotice message={copyError} onDismiss={() => setCopyError(null)} />
              </div>
            </div>
            <div ref={setBoardNotices} data-testid="canvas-page-board-notices" className="w-full max-w-[520px] empty:hidden [&>*]:pointer-events-auto" />
        </div>
        <CanvasArtifactBody
          key={slug}
          artifact={artifact}
          isCurrent={isCurrent}
          onStatus={setStatus}
          noticeSlot={boardNotices}
          keepOnFailure={<ReportBoardFailure onFailed={setBoardFailed} />}
        />
      </div>
      {/* One full-height column on the right: the chat, comments or activity. The board shrinks
          beside it; on a phone the column takes the whole width and its close button brings the
          board back. The column sets its own width; this wrapper only docks it flush against the
          board's edge, so a panel's card border and rounding give way to one left border. */}
      {(column !== null || sidebarDraftDirty) && (
        <div hidden={column === null} className={`${column === null ? 'hidden' : 'flex'} min-h-0 flex-col border-border bg-card [&>aside]:rounded-none [&>aside]:border-0 ${narrowColumnOpen ? 'min-w-0 flex-1' : 'shrink-0 border-l'}`} data-testid="canvas-page-column">
          {column === 'chat' && (
            <div className="flex min-h-0 flex-1 flex-col [&>aside]:rounded-none [&>aside]:border-0" data-testid="canvas-page-chat-panel">
              {/* No hand-off: the board may hold a waiting autosave and the comments column a draft; the hand-off navigates away from both. */}
              <ErrorNotice
                message={contextError}
                title={i18nT(contextErrorIsRefresh
                  ? 'pages.artifactDetailPage.chat_context_stale_notice_title'
                  : 'pages.artifactDetailPage.chat_context_notice_title')}
                onDismiss={() => setContextError(null)}
                className="m-2 animate-rise"
              />
              <ArtifactChatPanel
                slotKey={chatCreating ? null : boundSlot?.key ?? null}
                creating={chatCreating}
                onNewChat={() => { void newCompanionChat() }}
                onOpenFull={openChatFull}
                onClose={() => { sidebarUserToggledRef.current = true; setPanel('none') }}
              />
            </div>
          )}
          {(column === 'comments' || sidebarDraftDirty) && (
            <div hidden={column !== 'comments'} className={`${column === 'comments' ? 'flex' : 'hidden'} min-h-0 flex-1 flex-col ${narrowColumnOpen ? '' : 'w-[340px]'}`} data-testid="canvas-page-comments">
              {/* No hand-off: a comment draft and the board's waiting autosave live on this page. */}
              <ErrorNotice
                title={i18nT('pages.artifactDetailPage.comments_failed_to_load')}
                message={commentsQuery.error ? (errMessage(commentsQuery.error) || i18nT('components.errorBoundary.something_went_wrong')) : null}
                className="m-2"
              />
              {/* No hand-off: a comment draft and the board's waiting autosave live on this page. */}
              <ErrorNotice
                title={i18nT('pages.artifactDetailPage.comment_action_failed')}
                message={commentActionError}
                onDismiss={() => setCommentActionError(null)}
                className="m-2"
              />
              {/* A canvas has no text to anchor to, so every comment here is on the whole artifact. */}
              <CommentsSidebar
                comments={durableComments}
                onDraftDirtyChange={reportSidebarDraft}
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
                    key={slug}
                    count={pendingComments.length}
                    submitting={submittingComments}
                    onSubmit={submitCommentsToChat}
                    connected={connected}
                    stacked
                  />
                ) : undefined}
                pendingSubmitIds={boundSlot ? pendingCommentIds : undefined}
                onClose={() => setOwn(null)}
                onCommentClick={activateFromSidebar}
                onEditComment={editComment}
                activeCommentId={activeCommentId}
                containerClassName="flex min-h-0 flex-1 flex-col overflow-hidden bg-card"
                containerStyle={{}}
              />
            </div>
          )}
          {column === 'activity' && (
            <aside className={`flex min-h-0 flex-1 flex-col ${narrowColumnOpen ? '' : 'w-[340px]'}`} data-testid="canvas-page-activity"
              aria-label={i18nT('pages.artifactDetailPage.activity')}>
              <div className={COLUMN_HEAD}>
                <History size={13} className="shrink-0 text-muted" aria-hidden="true" />
                <span className="flex-1 truncate text-[12px] font-medium text-text">{i18nT('pages.artifactDetailPage.activity')}</span>
                <button type="button" onClick={() => setOwn(null)} aria-label={i18nT('app.close')}
                  className="rounded p-1 text-muted hover:bg-bg-hover hover:text-text">
                  <X size={13} />
                </button>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto p-3">
                {/* No hand-off: the board may hold a waiting autosave not yet saved anywhere. */}
                <ErrorNotice
                  title={i18nT('pages.artifactDetailPage.activity_failed_to_load')}
                  message={eventsQuery.error ? (errMessage(eventsQuery.error) || i18nT('components.errorBoundary.something_went_wrong')) : null}
                  className="mb-2"
                />
                <ActivityTimeline events={eventsQuery.data?.events ?? []} navigateToSlot={(slotKey) => sendNav({ path: '/chat', slotKey })} />
              </div>
            </aside>
          )}
          {column === 'publish' && (
            <div className={`min-h-0 flex-1 overflow-y-auto p-3 ${narrowColumnOpen ? '' : 'w-[340px]'}`} data-testid="canvas-page-publish">
              <PublishHub artifact={artifact} onClose={() => setOwn(null)} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}
