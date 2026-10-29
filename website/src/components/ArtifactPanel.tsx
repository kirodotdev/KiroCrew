import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { OverlayPortal } from './overlayRoot'
import { useQuery } from '@tanstack/react-query'
import { Component, Copy, ExternalLink, MessageSquare, Loader2, Maximize2, Minimize2 } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import DetailPanel from './DetailPanel'
import Clickable from './Clickable'
import SelectionToolbar, { type SelectionAction } from './SelectionToolbar'
import { isTouchDevice } from '../utils/isTouchDevice'
import { SubmitBar, type SubmitVerdict } from './ArtifactSubmitBar'
import ErrorNotice from './ErrorNotice'
import { ArtifactBodyNative, ArtifactBodyIframe, ArtifactBodyImage } from './ArtifactBody'
import { useFileArtifactComments } from './FileArtifactComments'
import { proseOverlayComments } from './codeComments'
import { useConfirm } from './ConfirmDialog'
import { formatArtifactCommentsMessage } from './CommentOverlay'
import { copyToClipboard } from '../utils/clipboard'
import { api } from '../api/client'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { useArtifactLiveReload } from '../hooks/useArtifactLiveReload'
import { useSentCommentIds } from '../hooks/useSentCommentIds'
import type { Artifact } from '../types'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { isEditableTarget } from '../utils/editableTarget'
import { useLocalCrewHidden } from './overlayRoot'
import { overlayCaptionClearancePx } from '../lib/electron'
interface Props {
  slug: string
  /** Kind captured at open time; the live query overrides it once loaded. */
  kind: Artifact['kind']
  /** Content captured at open time; the live query overrides it once loaded. */
  content: string
  onClose: () => void
  /** Is this panel the tab the user can see? A host that keeps background tabs
   *  mounted and merely hides them — and keeps the whole panel mounted through
   *  a close — has several live panels at once, each binding document-level
   *  Escape. Without this, an artifact tab that is off screen closes itself.
   *  Hosts that mount a single panel can leave this unset. */
  active?: boolean
  /** Mirror of the local-file submit path: sends a formatted USER message to
   *  the chat session the panel was opened from (panel.slot). When omitted the
   *  submit-to-chat affordance is hidden (read-only embedding). The return is
   *  an optional delivery verdict: an explicit `false` (or a promise of one)
   *  means the message was NOT delivered, so the batch must stay pending; any
   *  other return counts as delivered. */
  onSubmitComments?: (message: string, count?: number) => void | boolean | Promise<void | boolean>
  /** Gateway connection flag. Gates the submit-to-chat affordance (mirrors
   *  ChatInput's Send gating) so a batch submit can't fire while the chat
   *  send path would silently refuse it. Defaults true for embeddings
   *  without a chat send path. */
  connected?: boolean
  /** Render as a SidePanel tab body (fills parent, no resize handle/border). */
  embedded?: boolean
  /** Stable cross-remount identity (slot + tab id) for the embedded body's
   *  scroll position — a chat-slot switch unmounts the whole tab body, and
   *  this is what lets the document come back where the user left it (see
   *  `useScrollMemory`). Omitted by hosts without that lifecycle. */
  scrollMemoryKey?: string
}

const BODY_HEIGHT_STYLE: React.CSSProperties = { height: '100%', minHeight: 0 }

// Non-fullscreen sidebar stacks below content (not beside it) and is
// height-capped so content stays the primary region in the narrow panel.
const STACKED_SIDEBAR_CLASS = 'w-full shrink-0 flex flex-col rounded-xl border border-border bg-card overflow-hidden'
const STACKED_SIDEBAR_STYLE: React.CSSProperties = { maxHeight: 280, minHeight: 0 }

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
export default memo(function ArtifactPanel({ slug, kind, content, onClose, active: visible = true, onSubmitComments, connected = true, embedded, scrollMemoryKey }: Props) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const navigate = useNavigate()
  const previewRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const fsPreviewRef = useRef<HTMLDivElement>(null)
  const fsScrollRef = useRef<HTMLDivElement>(null)
  const [fullscreen, setFullscreen] = useState(false)
  // True while a remote Crew covers the local dashboard this panel lives in.
  const crewHidden = useLocalCrewHidden()
  // Shared IME latch for the full-screen preview's Tab trap: a Tab that lands
  // during an IME composition (or its post-`compositionend` window) is
  // choosing a candidate, not leaving the field, so the trap must decline it
  // instead of yanking focus and aborting the composition
  // (`useDialogFocusTrap` is the reference consumer of the same seam).
  const fsImeLatch = useDocumentImeLatch(fullscreen)
  // Read at render time: `.embedded-win-inset` toggles at runtime.
  const captionTop = overlayCaptionClearancePx()

  // Live artifact — authoritative for kind/name/content once loaded. Seeded
  // with what handleArtifactOpen captured so the panel renders immediately.
  const detailQuery = useQuery<Artifact>({
    queryKey: ['artifact', slug],
    queryFn: () => api.artifact(slug),
    enabled: !!slug,
    staleTime: 10_000,
  })
  const artifact = detailQuery.data
  // File-backed artifacts: an agent rewriting the backing file never passes
  // through a handler, so the artifact_update WS event does not fire for it.
  // Watch the live pointer while this tab is the visible one and refetch
  // through the shared cache; a hidden tab holds no stream and catches up
  // when it is shown again.
  useArtifactLiveReload(slug, artifact?.source_path, visible)
  const effectiveKind = artifact?.kind ?? kind
  const effectiveContent = artifact?.content ?? content
  const name = artifact?.name ?? slug
  const usesIframe = effectiveKind === 'widget' || effectiveKind === 'html'
  // Where a selection can become an anchored comment: an iframe body (through
  // the bridge) and the two native kinds whose renderer attaches the preview
  // ref the anchor resolver maps a selection against (the same two the
  // artifact page allows). `ArtifactBodyNative` renders json / svg / image
  // through rich viewers that attach no ref, so a toolbar over those could
  // never open — it is not mounted at all rather than mounted dead.
  const canAnchorComments = usesIframe || effectiveKind === 'markdown' || effectiveKind === 'text'
  // The panel opens synchronously without awaiting the fetch, so only show the
  // loading state when we genuinely have nothing yet (no seed and the shared
  // query is still in flight) — otherwise the seed renders and never flashes.
  const isHydrating = detailQuery.isLoading && !artifact && !content
  const loadFailed = detailQuery.isError && !artifact && !content

  // Escape / ✕ on a typed comment draft, and the panel's own full-screen and
  // close actions (each unmounts the toolbar the draft lives in), ask first —
  // in the draft's own words, since "unsaved changes" would read as file edits.
  const { confirm, confirmDialog } = useConfirm()
  const confirmDiscardDraft = useCallback(() => confirm({
    title: i18nT('components.markdownPanel.discard_unsaved_comment'),
    confirmLabel: i18nT('components.markdownPanel.discard_comment_button'),
    // Raised from inside the full-screen shell too (Exit full screen over a
    // draft): that shell is an opaque body portal at z-[9999], so the prompt
    // must take the layer above it or the button looks dead.
    layer: 'top',
  }), [confirm])

  // ── submitted-to-chat tracking ── (shared with the standalone artifact page)
  const { sentIds, markSent } = useSentCommentIds(slug)
  // Handed to the comment layers so each sidebar row the Submit bar counts is
  // marked; only when this host has a Submit, since otherwise there is no bar
  // for a marker to answer to.
  const submitSentIds = onSubmitComments ? sentIds : undefined

  // Two instances (non-fullscreen body / fullscreen body) read the SAME durable
  // comments via the shared query cache; only local UI state (sidebar open,
  // active thread) is per-instance.
  const fa = useFileArtifactComments({
    slug, previewRef, scrollRef, usesIframe,
    sidebarDefaultOpen: false,
    sidebarClassName: STACKED_SIDEBAR_CLASS,
    sidebarStyle: STACKED_SIDEBAR_STYLE,
    confirmDiscardDraft,
    submitSentIds,
  })
  const faFull = useFileArtifactComments({ slug, previewRef: fsPreviewRef, scrollRef: fsScrollRef, usesIframe, confirmDiscardDraft, submitSentIds })
  // Code-block threads (source-offset anchors, see codeComments.ts) show no
  // chips here, and the prose overlay must not paint them on a prose copy of
  // the code line: each layer's overlay gets its comments without them.
  const isMarkdownBody = effectiveKind === 'markdown'
  const faProse = useMemo(
    () => (isMarkdownBody ? proseOverlayComments(effectiveContent ?? '', fa.comments) : fa.comments),
    [isMarkdownBody, effectiveContent, fa.comments],
  )
  const faFullProse = useMemo(
    () => (isMarkdownBody ? proseOverlayComments(effectiveContent ?? '', faFull.comments) : faFull.comments),
    [isMarkdownBody, effectiveContent, faFull.comments],
  )
  // The active comment layer for the visible surface.
  const active = fullscreen ? faFull : fa
  /** Run `proceed` unless the visible layer's comment draft would be lost, in
   *  which case ask first (the layer's own guard: it knows the passage whose
   *  persisted copy a confirmed discard must drop). */
  const guardDraft = active.guardCommentDraft
  const enterFullscreen = useCallback(() => { void guardDraft(() => setFullscreen(true)) }, [guardDraft])
  const exitFullscreen = useCallback(() => { void guardDraft(() => setFullscreen(false)) }, [guardDraft])
  const requestClose = useCallback(() => { void guardDraft(onClose) }, [guardDraft, onClose])

  // Selecting text opens the layer's type-first comment composer (see
  // `useFileArtifactComments().selectionComposer`). No row action beside it:
  // the box already carries Add comment and Close, and a third control would
  // break the two-per-row cap. Copying the selection is the composer's own
  // Cmd/Ctrl+C while its input is empty.
  const selectionActions: SelectionAction[] = useMemo(() => [], [])
  // The kinds no anchor resolver can map (json, svg, …) get no composer — but
  // a pointer device keeps the Copy row the panel shipped before it. One
  // control, so the row is inside the two-per-row cap on its own.
  const copyOnlyActions: SelectionAction[] = useMemo(() => [
    { id: 'copy', icon: <Copy size={12} />, label: i18nT('components.selectionToolbar.copy'), onClick: (text: string) => copyToClipboard(text) },
  ], [])
  // On a touch device the platform draws its own handles and Copy callout
  // around the selection, and a row floated there took the taps meant for the
  // handles. Copy is the callout's job there, so the copy-only row is not
  // mounted at all (the same call the chat reply makes for its Copy).
  const showCopyOnlyToolbar = !canAnchorComments && !isTouchDevice()

  // Pending = forwarding-eligible AND human-authored AND not yet submitted,
  // as the comment layer worked it out from `submitSentIds` (the same list its
  // sidebar marks). Agent comments are also filtered defensively inside
  // formatArtifactCommentsMessage (hardened esc()).
  const pendingComments = fa.pendingSubmit
  const [submitting, setSubmitting] = useState(false)
  // Tracks the "submitting" reset timer so it can be cancelled on unmount —
  // otherwise a late firing calls setSubmitting on an unmounted component,
  // which under jsdom teardown throws "window is not defined" (the timer
  // outlives the test environment under --coverage timing).
  const submitResetTimer = useRef<ReturnType<typeof setTimeout>>()
  const submitToChat = useCallback((extraPrompt?: string): Promise<SubmitVerdict> => {
    // Bail while offline: the chat send path silently refuses messages in
    // that state, so firing the fake "submitting" spinner would just mislead.
    // The Submit button is disabled offline too — this is the backstop.
    // Nothing sent: `false`, so the bar hands the typed instruction back.
    if (!connected || !onSubmitComments || pendingComments.length === 0) return Promise.resolve(false)
    setSubmitting(true)
    const batch = pendingComments
    // The send itself fires synchronously; only the MARKING waits for the
    // host's delivery verdict. An explicit `false` (or a throw/rejection)
    // means the chat send was refused, so the batch stays pending and Submit
    // re-offers it — sent ids are append-only, so marking an undelivered
    // batch would silently drop it with no re-surface path. A void-returning
    // host counts as delivered (embeddings without a verdict keep the
    // clear-on-submit behavior).
    let verdict: void | boolean | Promise<void | boolean>
    try {
      verdict = onSubmitComments(formatArtifactCommentsMessage(slug, name, batch, extraPrompt), batch.length)
    } catch {
      verdict = false
    }
    // The verdict goes back to the bar so a refusal hands the instruction
    // back; the chat host has already reported the failure itself.
    return Promise.resolve(verdict)
      .then(delivered => {
        if (delivered === false) return false
        markSent(batch)
        return true
      })
      .catch(() => false /* undelivered — keep the batch pending */)
      .finally(() => {
        // Brief guard against double-fire, released after the verdict settles.
        clearTimeout(submitResetTimer.current)
        submitResetTimer.current = setTimeout(() => setSubmitting(false), 400)
      })
  }, [connected, onSubmitComments, pendingComments, slug, name, markSent])
  useEffect(() => () => clearTimeout(submitResetTimer.current), [])

  // Esc closes fullscreen first, then the panel; lock body scroll while the
  // fullscreen overlay is open — mirrors MarkdownPanel.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // A background tab, or any tab in a closed-but-mounted panel, is still
      // listening — closing it would dismiss an artifact that is nowhere on
      // screen. Aliased from the `active` prop: a local `active` in this file
      // already names the fullscreen icon. While a remote Crew is showing, the
      // whole local dashboard is off screen too.
      if (!visible || crewHidden) return
      // Don't hijack Esc while the user is in an editable field (e.g. the
      // add-instruction textarea) — let the field handle it instead of
      // closing/exiting the panel out from under them.
      if (isEditableTarget(e)) return
      // An open annotation box owns Escape: the toolbar closes it (and hands the
      // selection back) on its own, and the box need not hold focus (a touch or
      // Shift+Arrow open leaves the caret elsewhere) — the panel must not ALSO
      // close or exit full screen out from under it. Mirrors MarkdownPanel.
      if (active.isComposerOpen()) return
      if (fullscreen) exitFullscreen(); else requestClose()
    }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [visible, fullscreen, exitFullscreen, requestClose, active, crewHidden])
  useEffect(() => {
    if (!fullscreen) return
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = '' }
  }, [fullscreen])

  // Submit-to-chat is the side-panel analog of the detail page's companion
  // chat. Only rendered when the host actually supplies a submit channel.
  const showSubmitBar = !!onSubmitComments && pendingComments.length > 0

  // `flush` drops the native body's card chrome so a markdown artifact in the
  // side panel looks like a markdown FILE in the side panel — same edge-to-edge
  // text, padded once by DetailPanel's own `px-5 py-4` instead of twice by a
  // nested bordered card. Fullscreen keeps the card: there the artifact floats
  // on a full-viewport backdrop and the border is what bounds the document.
  const renderBody = (
    bodyScrollRef: React.RefObject<HTMLDivElement>,
    bodyPreviewRef: React.RefObject<HTMLDivElement>,
    layer: typeof fa,
    flush = false,
    // Embedded body only: cross-remount scroll identity, forwarded to
    // ArtifactBodyNative (whose inner div is the real scroll container).
    // The fullscreen instance omits it so two live instances never share a
    // key. Iframe kinds scroll inside their sandbox — deliberately out of
    // scope (#5701).
    bodyScrollMemoryKey?: string,
  ) => (
    <div ref={bodyScrollRef} className="relative h-full overflow-auto pr-2">
      {isHydrating ? (
        <div className="h-full flex flex-col items-center justify-center gap-3 text-muted" aria-busy="true">
          <Loader2 size={20} className="animate-spin" />
          <span className="text-[13px]">{i18nT('components.artifactPanel.loading_artifact')}</span>
        </div>
      ) : loadFailed ? (
        <div className="h-full flex items-center justify-center px-6">
          {/* Read failure before anything loaded — nothing in the panel to lose, so the hand-off is on. */}
          <ErrorNotice
            askAgent
            testId="artifact-panel-load-error"
            message={i18nT('components.artifactPanel.couldn_t_load_this_artifact_it_may_have_been_del')}
          />
        </div>
      ) : effectiveKind === 'image' && artifact ? (
        <ArtifactBodyImage
          artifact={artifact}
          slug={slug}
          heightStyle={BODY_HEIGHT_STYLE}
        />
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
          comments={layer === fa ? faProse : faFullProse}
          heightStyle={BODY_HEIGHT_STYLE}
          onActivateComment={layer.activateComment}
          activeCommentId={layer.activeCommentId}
          scrollNonce={layer.scrollNonce}
          unreadRootIds={layer.unreadRootIds}
          flush={flush}
          scrollMemoryKey={bodyScrollMemoryKey}
        />
      )}
    </div>
  )

  // Show/hide comments toggle — mirrors the artifact detail page's control.
  const commentsToggle = (open: boolean, onToggle: () => void) => (
    <button
      className={`p-1.5 rounded-md border cursor-pointer transition-all ${open ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
      onClick={onToggle}
      title={open ? i18nT('components.artifactPanel.hide_comments') : i18nT('components.artifactPanel.show_comments')}
      aria-label={open ? i18nT('components.artifactPanel.hide_comments') : i18nT('components.artifactPanel.show_comments')}
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
      icon={<Component size={14} className="text-accent shrink-0" />}
      title={<span className="truncate">{name}</span>}
      onClose={requestClose}
      initialWidth={480}
      minWidth={420}
      storageKey="mc-panel-width"
      headerActions={
        <>
          {commentsToggle(fa.sidebarOpen, fa.toggleSidebar)}
          <button
            className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
            onClick={enterFullscreen}
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
          {renderBody(scrollRef, previewRef, fa, true, scrollMemoryKey)}
        </div>
        {/* Sidebar stacks below content (height-capped) so content stays primary. */}
        {fa.sidebarOpen && (
          <div className="mt-3 pr-2 shrink-0">{fa.sidebar}</div>
        )}
        {showSubmitBar && (
          <SubmitBar key={slug} count={pendingComments.length} submitting={submitting} onSubmit={submitToChat} connected={connected} hostReportsRefusal bleed />
        )}
      </div>
      {/* Listens on the preview itself, not the scroll box around it, so the
          composer only opens over text the anchor resolver can map. For an
          iframe body the preview ref is never attached — the frame's
          selections arrive through the bridge as `iframeSelection` instead and
          `externalOnly` keeps the toolbar's own selection check off — so the
          scroll box stands in as the container: it is what the box's placement
          clamps to, and an unattached ref would let a right-half bridge
          selection open the box over the comments sidebar. Suspended with the
          tab so a hidden panel's open box does not stay on screen. */}
      {!fullscreen && canAnchorComments && <SelectionToolbar containerRef={usesIframe ? scrollRef : previewRef} actions={selectionActions} composer={fa.selectionComposer} externalSelection={fa.iframeSelection} externalOnly={usesIframe} suspended={!visible} />}
      {!fullscreen && showCopyOnlyToolbar && <SelectionToolbar containerRef={scrollRef} actions={copyOnlyActions} suspended={!visible} />}
      {!fullscreen && fa.popovers}
    </DetailPanel>
    {fullscreen && <OverlayPortal>{
      // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- `dialog` is a structural role, and this onKeyDown is the modal focus trap the house a11y contract requires, not an activation: it only redirects a boundary Tab back inside. The operable controls are the buttons and the artifact body within.
      <div className="fixed inset-0 z-[9999] bg-bg flex flex-col p-safe" role="dialog" aria-modal="true" aria-label={i18nT('components.artifactPanel.full_screen_artifact_preview')}
        ref={el => { if (el && !el.dataset.focused) { el.dataset.focused = '1'; const first = el.querySelector<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])'); first?.focus() } }}
        onKeyDown={e => {
          if (e.key !== 'Tab') return
          const focusable = e.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])')
          if (focusable.length === 0) return
          const first = focusable[0], last = focusable[focusable.length - 1]
          const wrapsBackward = e.shiftKey && document.activeElement === first
          const wrapsForward = !e.shiftKey && document.activeElement === last
          // A mid-dialog Tab is the browser's to move, and not the trap's to
          // claim. A boundary Tab the IME owns must not cycle focus — the user
          // is choosing a candidate, not leaving the field — so `claimKey`
          // (native-event contract in useImeGuard.ts) runs before the
          // preventDefault() and focus move.
          if (!wrapsBackward && !wrapsForward) return
          // `claimSyntheticKey` owns BOTH halves of a decline: the native
          // event (which document/window listeners see) and React's own
          // propagation flag (which it walks when dispatching to component
          // ancestors), so a declined Tab cannot trigger an ancestor's
          // keyboard handling.
          if (!fsImeLatch.claimSyntheticKey(e)) return
          e.preventDefault()
          ;(wrapsBackward ? last : first).focus()
        }}>
        {/* Header — pl-20 clears macOS traffic-light buttons; the marginTop drops
            it below the Windows/Linux caption band (#24784). */}
        <div data-testid="artifact-panel-fullscreen-header" className="flex items-center justify-between pl-20 pr-6 h-12 shrink-0 border-b border-border"
          style={captionTop > 0 ? { marginTop: `${captionTop}px` } : undefined}>
          <span className="flex items-center gap-2 min-w-0">
            <Component size={14} className="text-accent shrink-0" />
            <span className="text-base font-semibold text-text-strong truncate">{name}</span>
          </span>
          <div className="flex items-center gap-1.5">
            {commentsToggle(faFull.sidebarOpen, faFull.toggleSidebar)}
            <button
              className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
              onClick={() => navigate(`/artifacts/${encodeURIComponent(slug)}`)}
              title={i18nT('components.artifactPanel.open_full_artifact_page')}
              aria-label={i18nT('components.artifactPanel.open_full_artifact_page')}
            ><ExternalLink size={14} /></button>
            <button className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all" onClick={exitFullscreen} title={i18nT('components.artifactPanel.exit_full_screen_esc')} aria-label={i18nT('components.artifactPanel.exit_full_screen')}><Minimize2 size={14} /></button>
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
        {canAnchorComments && <SelectionToolbar containerRef={usesIframe ? fsScrollRef : fsPreviewRef} actions={selectionActions} composer={faFull.selectionComposer} externalSelection={faFull.iframeSelection} externalOnly={usesIframe} suspended={!visible} />}
        {showCopyOnlyToolbar && <SelectionToolbar containerRef={fsScrollRef} actions={copyOnlyActions} suspended={!visible} />}
        {faFull.popovers}
        {showSubmitBar && (
          <div className="shrink-0 px-16 pb-3">
            <SubmitBar key={slug} count={pendingComments.length} submitting={submitting} onSubmit={submitToChat} connected={connected} hostReportsRefusal />
          </div>
        )}
        <Clickable className="shrink-0 flex items-center px-16 h-6 text-[11px] text-muted font-mono truncate cursor-pointer hover:text-text transition-colors" title={i18nT('components.artifactPanel.click_to_copy_slug')} onClick={() => copyToClipboard(slug)}>{i18nT('components.artifactPanel.artifacts')}{slug}</Clickable>
      </div>
    }</OverlayPortal>}
    {confirmDialog}
    </>
  )
})
