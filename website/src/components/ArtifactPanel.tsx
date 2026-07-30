import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { ExternalLink, MessageSquare, MessageSquarePlus, Send, Loader2, Copy, Maximize2, Minimize2 } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import DetailPanel from './DetailPanel'
import Clickable from './Clickable'
import SelectionToolbar, { type SelectionAction } from './SelectionToolbar'
import { SendBtn } from './ui'
import { ArtifactBodyNative, ArtifactBodyIframe } from './ArtifactBody'
import { useFileArtifactComments } from './FileArtifactComments'
import { formatArtifactCommentsMessage } from './CommentOverlay'
import { copyToClipboard } from '../utils/clipboard'
import { api } from '../api/client'
import type { Artifact } from '../types'

import { i18nT } from '../i18n/t'
interface Props {
  slug: string
  /** Kind captured at open time; the live query overrides it once loaded. */
  kind: Artifact['kind']
  /** Content captured at open time; the live query overrides it once loaded. */
  content: string
  onClose: () => void
  /** Mirror of the local-file submit path: sends a formatted USER message to
   *  the chat session the panel was opened from (panel.slot). When omitted the
   *  submit-to-chat affordance is hidden (read-only embedding). */
  onSubmitComments?: (message: string) => void
  /** Render as a SidePanel tab body (fills parent, no resize handle/border). */
  embedded?: boolean
}

const BODY_HEIGHT_STYLE: React.CSSProperties = { height: '100%', minHeight: 0 }
// Non-fullscreen sidebar stacks below content (not beside it) and is
// height-capped so content stays the primary region in the narrow panel.
const STACKED_SIDEBAR_CLASS = 'w-full shrink-0 flex flex-col rounded-xl border border-border bg-card overflow-hidden'
const STACKED_SIDEBAR_STYLE: React.CSSProperties = { maxHeight: 280, minHeight: 0 }

/** Submit-to-chat bar with an optional "Add instruction" affordance. The
 *  free-form note is threaded through as the `extraPrompt` arg only when the
 *  toggle is open, and cleared after submit. */
function SubmitBar({ count, submitting, onSubmit, bleed = false }: {
  count: number; submitting: boolean; onSubmit: (extraPrompt?: string) => void
  /** Bleed to the panel edges (non-fullscreen, inside the negative-margin
   *  content wrapper). Fullscreen uses its own padding, so omit it there. */
  bleed?: boolean
}) {
  const [extraPrompt, setExtraPrompt] = useState('')
  const [showExtraPrompt, setShowExtraPrompt] = useState(false)
  const extraPromptRef = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { if (showExtraPrompt) extraPromptRef.current?.focus() }, [showExtraPrompt])
  const submit = useCallback(() => {
    onSubmit(showExtraPrompt ? extraPrompt : undefined)
    setExtraPrompt('')
    setShowExtraPrompt(false)
  }, [onSubmit, showExtraPrompt, extraPrompt])
  return (
    <div className={`border-t border-border bg-chrome px-3 py-2 ${bleed ? '-mb-4 -ml-4' : 'rounded-md border-x border-b'}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[13px] text-text truncate">
            {count} {i18nT('components.artifactPanel.comment')}{count === 1 ? '' : 's'} {i18nT('components.artifactPanel.to_send_to_this_chat')}
          </span>
          <button
            type="button"
            aria-label={i18nT('components.artifactPanel.toggle_additional_instruction')}
            aria-pressed={showExtraPrompt}
            onClick={() => setShowExtraPrompt(v => !v)}
            className={`flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[11px] font-medium border cursor-pointer transition-all shrink-0 ${showExtraPrompt ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
          ><MessageSquarePlus className="lucide-inline" /> {i18nT('components.artifactPanel.add_instruction')}</button>
        </div>
        <SendBtn onClick={submit} disabled={submitting}>
          {i18nT('components.artifactPanel.submit')} <Send className="lucide-inline" />
        </SendBtn>
      </div>
      {showExtraPrompt && (
        <textarea
          ref={extraPromptRef}
          aria-label={i18nT('components.artifactPanel.additional_instruction')}
          placeholder={i18nT('components.artifactPanel.optional_overall_feedback_or_an_extra_instructio')}
          value={extraPrompt}
          onChange={e => setExtraPrompt(e.target.value)}
          rows={2}
          className="mt-2 w-full bg-bg-elevated border border-border rounded-md px-2.5 py-1.5 text-text text-[13px] font-body outline-none resize-none focus-ring leading-[18px]"
        />
      )}
    </div>
  )
}

/**
 * Side-panel Artifacts tab. Reuses the shared artifact body
 * (`ArtifactBodyNative` / `ArtifactBodyIframe`) and the durable-comment
 * store/UI (`useFileArtifactComments`) from the full-page route, mirroring the
 * file panel's layout (inline overlay non-fullscreen; portal overlay with the
 * full `CommentsSidebar` in fullscreen).
 *
 * The one behavioral difference from the full-page route: "Submit to chat"
 * sends a formatted USER message to the originating chat session via
 * `onSubmitComments` (the local-file user-message path) rather than the
 * full-page `iterateWithAgent` navigate — and only for human comments.
 */
export default memo(function ArtifactPanel({ slug, kind, content, onClose, onSubmitComments, embedded }: Props) {
  const navigate = useNavigate()
  const previewRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const fsPreviewRef = useRef<HTMLDivElement>(null)
  const fsScrollRef = useRef<HTMLDivElement>(null)
  const [fullscreen, setFullscreen] = useState(false)

  // Live artifact — authoritative for kind/name/content once loaded. Seeded
  // with what handleArtifactOpen captured so the panel renders immediately.
  const detailQuery = useQuery<Artifact>({
    queryKey: ['artifact', slug],
    queryFn: () => api.artifact(slug),
    enabled: !!slug,
    staleTime: 10_000,
  })
  const artifact = detailQuery.data
  const effectiveKind = artifact?.kind ?? kind
  const effectiveContent = artifact?.content ?? content
  const name = artifact?.name ?? slug
  const usesIframe = effectiveKind === 'widget' || effectiveKind === 'html'
  // The panel opens synchronously without awaiting the fetch, so only show the
  // loading state when we genuinely have nothing yet (no seed and the shared
  // query is still in flight) — otherwise the seed renders and never flashes.
  const isHydrating = detailQuery.isLoading && !artifact && !content
  const loadFailed = detailQuery.isError && !artifact && !content

  // Two instances (non-fullscreen body / fullscreen body) read the SAME durable
  // comments via the shared query cache; only local UI state (sidebar open,
  // active thread) is per-instance.
  const fa = useFileArtifactComments({
    slug, previewRef, scrollRef, usesIframe,
    sidebarDefaultOpen: false,
    sidebarClassName: STACKED_SIDEBAR_CLASS,
    sidebarStyle: STACKED_SIDEBAR_STYLE,
  })
  const faFull = useFileArtifactComments({ slug, previewRef: fsPreviewRef, scrollRef: fsScrollRef, usesIframe })
  // The active comment layer for the visible surface.
  const active = fullscreen ? faFull : fa

  // Selection → anchored comment, reusing the active layer's create popover.
  const handleCommentAction = useCallback(() => {
    active.requestAnchoredComment()
    window.getSelection()?.removeAllRanges()
  }, [active])
  const handleCopyAction = useCallback((text: string) => { if (text) copyToClipboard(text) }, [])
  const selectionActions: SelectionAction[] = useMemo(() => [
    { id: 'comment', icon: <MessageSquarePlus size={12} />, label: 'Comment', onClick: handleCommentAction },
    // Icon (not a text "Copy" span, which rendered as "Copy Copy" beside the label).
    { id: 'copy', icon: <Copy size={12} />, label: 'Copy', onClick: handleCopyAction },
  ], [handleCommentAction, handleCopyAction])

  // Human-only: agent comments are filtered out here AND defensively inside
  // formatArtifactCommentsMessage (which applies the hardened esc()).
  const humanComments = useMemo(
    () => fa.comments.filter(c => !c.is_agent),
    [fa.comments],
  )
  const [submitting, setSubmitting] = useState(false)
  // Tracks the "submitting" reset timer so it can be cancelled on unmount —
  // otherwise a late firing calls setSubmitting on an unmounted component,
  // which under jsdom teardown throws "window is not defined" (the timer
  // outlives the test environment under --coverage timing).
  const submitResetTimer = useRef<ReturnType<typeof setTimeout>>()
  const submitToChat = useCallback((extraPrompt?: string) => {
    if (!onSubmitComments || humanComments.length === 0) return
    setSubmitting(true)
    try {
      onSubmitComments(formatArtifactCommentsMessage(slug, name, humanComments, extraPrompt))
    } finally {
      // Brief guard against double-fire.
      clearTimeout(submitResetTimer.current)
      submitResetTimer.current = setTimeout(() => setSubmitting(false), 400)
    }
  }, [onSubmitComments, humanComments, slug, name])
  useEffect(() => () => clearTimeout(submitResetTimer.current), [])

  // Esc closes fullscreen first, then the panel; lock body scroll while the
  // fullscreen overlay is open — mirrors MarkdownPanel.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // Don't hijack Esc while the user is in an editable field (e.g. the
      // add-instruction textarea) — let the field handle it instead of
      // closing/exiting the panel out from under them.
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement)?.isContentEditable) return
      if (fullscreen) setFullscreen(false); else onClose()
    }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [fullscreen, onClose])
  useEffect(() => {
    if (!fullscreen) return
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = '' }
  }, [fullscreen])

  // Submit-to-chat is the side-panel analog of the detail page's companion
  // chat. Only rendered when the host actually supplies a submit channel.
  const showSubmitBar = !!onSubmitComments && humanComments.length > 0

  const renderBody = (
    bodyScrollRef: React.RefObject<HTMLDivElement>,
    bodyPreviewRef: React.RefObject<HTMLDivElement>,
    layer: typeof fa,
  ) => (
    <div ref={bodyScrollRef} className="relative h-full overflow-auto pr-2">
      {isHydrating ? (
        <div className="h-full flex flex-col items-center justify-center gap-3 text-muted" aria-busy="true">
          <Loader2 size={20} className="animate-spin" />
          <span className="text-[13px]">{i18nT('components.artifactPanel.loading_artifact')}</span>
        </div>
      ) : loadFailed ? (
        <div className="h-full flex items-center justify-center px-6 text-center text-[13px] text-danger">
          {i18nT('components.artifactPanel.couldn_t_load_this_artifact_it_may_have_been_del')}
        </div>
      ) : usesIframe && artifact ? (
        <ArtifactBodyIframe
          artifact={artifact}
          slug={slug}
          comments={layer.comments}
          onSelect={layer.onIframeSelect}
          onOpenThread={layer.onIframeOpenThread}
          scrollToCommentId={layer.iframeScrollTarget}
          activeId={layer.activeCommentId}
          unreadRootIds={layer.unreadRootIds}
          heightStyle={BODY_HEIGHT_STYLE}
        />
      ) : (
        <ArtifactBodyNative
          kind={effectiveKind}
          content={effectiveContent}
          editing={false}
          onChange={() => { /* read-only in the side panel */ }}
          previewRef={bodyPreviewRef}
          comments={layer.comments}
          heightStyle={BODY_HEIGHT_STYLE}
          onActivateComment={layer.activateComment}
          activeCommentId={layer.activeCommentId}
          scrollNonce={layer.scrollNonce}
          unreadRootIds={layer.unreadRootIds}
        />
      )}
    </div>
  )

  // Show/hide comments toggle — mirrors the artifact detail page's control.
  const commentsToggle = (open: boolean, onToggle: () => void) => (
    <button
      className={`p-1.5 rounded-md border cursor-pointer transition-all ${open ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
      onClick={onToggle}
      title={open ? 'Hide comments' : 'Show comments'}
      aria-label={open ? 'Hide comments' : 'Show comments'}
      aria-pressed={open}
    >
      {/* leading-none collapses the icon and count pill's line-box to the 14px
          icon height, so this button matches its bare-icon p-1.5 siblings. */}
      <span className="inline-flex items-center gap-1 leading-none">
        <MessageSquare size={14} />
        {active.commentCount > 0 && (
          <span className="ml-0.5 px-1 rounded bg-accent/20 text-[10px] leading-none tabular-nums">{active.commentCount}</span>
        )}
      </span>
    </button>
  )

  return (
    <>
    <DetailPanel
      embedded={embedded}
      title={
        <span className="flex items-center gap-2">
          <span className="truncate">{name}</span>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent font-medium shrink-0 uppercase">{effectiveKind}</span>
        </span>
      }
      onClose={onClose}
      initialWidth={480}
      minWidth={420}
      storageKey="mc-panel-width"
      headerActions={
        <>
          {commentsToggle(fa.sidebarOpen, fa.toggleSidebar)}
          <button
            className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
            onClick={() => setFullscreen(true)}
            title={i18nT('components.artifactPanel.full_screen')}
            aria-label={i18nT('components.artifactPanel.full_screen')}
          ><Maximize2 size={14} /></button>
          <button
            className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
            onClick={() => navigate(`/artifacts/${encodeURIComponent(slug)}`)}
            title={i18nT('components.artifactPanel.open_full_artifact_page')}
            aria-label={i18nT('components.artifactPanel.open_full_artifact_page')}
          ><ExternalLink size={14} /></button>
        </>
      }
      footer={
        <Clickable
          className="flex items-center gap-2 text-[11px] text-muted font-mono truncate cursor-pointer hover:text-text transition-colors"
          title={i18nT('components.artifactPanel.click_to_copy_slug')}
          onClick={() => copyToClipboard(slug)}
        >
          {i18nT('components.artifactPanel.artifacts')}{slug}
        </Clickable>
      }
    >
      <div className="flex-1 overflow-hidden -mx-5 -my-4 py-4 flex flex-col pl-4 pr-0 min-h-0">
        <div className="relative flex-1 min-w-0 min-h-0">
          {renderBody(scrollRef, previewRef, fa)}
        </div>
        {/* Sidebar stacks below content (height-capped) so content stays primary. */}
        {fa.sidebarOpen && (
          <div className="mt-3 pr-2 shrink-0">{fa.sidebar}</div>
        )}
        {showSubmitBar && (
          <SubmitBar count={humanComments.length} submitting={submitting} onSubmit={submitToChat} bleed />
        )}
      </div>
      {!usesIframe && !fullscreen && <SelectionToolbar containerRef={scrollRef} actions={selectionActions} />}
      {!fullscreen && fa.popovers}
    </DetailPanel>
    {fullscreen && createPortal(
      <div className="fixed inset-0 z-[9999] bg-bg flex flex-col" role="dialog" aria-modal="true" aria-label={i18nT('components.artifactPanel.full_screen_artifact_preview')}
        ref={el => { if (el && !el.dataset.focused) { el.dataset.focused = '1'; const first = el.querySelector<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])'); first?.focus() } }}
        onKeyDown={e => { if (e.key === 'Tab') { const focusable = e.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])'); if (focusable.length === 0) return; const first = focusable[0], last = focusable[focusable.length - 1]; if (e.shiftKey) { if (document.activeElement === first) { e.preventDefault(); last.focus() } } else { if (document.activeElement === last) { e.preventDefault(); first.focus() } } } }}>
        {/* Header — pl-20 clears macOS traffic-light buttons */}
        <div className="flex items-center justify-between pl-20 pr-6 h-12 shrink-0 border-b border-border">
          <span className="flex items-center gap-2 min-w-0">
            <span className="text-base font-semibold text-text-strong truncate">{name}</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent font-medium shrink-0 uppercase">{effectiveKind}</span>
          </span>
          <div className="flex items-center gap-1.5">
            {commentsToggle(faFull.sidebarOpen, faFull.toggleSidebar)}
            <button
              className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
              onClick={() => navigate(`/artifacts/${encodeURIComponent(slug)}`)}
              title={i18nT('components.artifactPanel.open_full_artifact_page')}
              aria-label={i18nT('components.artifactPanel.open_full_artifact_page')}
            ><ExternalLink size={14} /></button>
            <button className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all" onClick={() => setFullscreen(false)} title={i18nT('components.artifactPanel.exit_full_screen_esc')} aria-label={i18nT('components.artifactPanel.exit_full_screen')}><Minimize2 size={14} /></button>
          </div>
        </div>
        <div className="relative flex-1 overflow-hidden min-h-0 px-16 py-4">
          <div className="flex gap-4 items-stretch h-full">
            <div className="relative flex-1 min-w-0 min-h-0">
              {renderBody(fsScrollRef, fsPreviewRef, faFull)}
            </div>
            {faFull.sidebarOpen && faFull.sidebar}
          </div>
        </div>
        {!usesIframe && <SelectionToolbar containerRef={fsScrollRef} actions={selectionActions} />}
        {faFull.popovers}
        {showSubmitBar && (
          <div className="shrink-0 px-16 pb-3">
            <SubmitBar count={humanComments.length} submitting={submitting} onSubmit={submitToChat} />
          </div>
        )}
        <Clickable className="shrink-0 flex items-center px-16 h-6 text-[11px] text-muted font-mono truncate cursor-pointer hover:text-text transition-colors" title={i18nT('components.artifactPanel.click_to_copy_slug')} onClick={() => copyToClipboard(slug)}>{i18nT('components.artifactPanel.artifacts')}{slug}</Clickable>
      </div>,
      document.body
    )}
    </>
  )
})
