import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { ArrowLeft, ExternalLink, GitFork, Loader2, User, MessageSquare, RotateCw, Eye } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { safeHttpUrl } from '../lib/safeUrl'
import { buildSrcdoc, readThemeVars } from '../lib/widgetSrcdoc'
import { api } from '../api/client'
import { PageHeader, Card, Badge, Btn } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { CommentsSidebar } from '../components/CommentsSidebar'
import SelectionToolbar, { type SelectionAction } from '../components/SelectionToolbar'
import { useConfirm } from '../components/ConfirmDialog'
import { InlineCommentOverlay } from '../components/InlineCommentOverlay'
import { useCommentBridge, type IframeSelection } from '../hooks/useCommentBridge'
import { containedSelectionRange } from '../utils/selectionContainment'
import { paintAnnotationHighlight } from '../utils/annotationHighlight'
import { useSelectionComposerAnchor } from '../hooks/useSelectionComposerAnchor'
import type { ArtifactComment } from '../types'

import { i18nT } from '../i18n/t'
import { useSandboxDoc } from '../hooks/useSandboxDoc'
import { useSilentLoadWatch } from '../hooks/useSilentLoadWatch'

/** The selection an open comment composer annotates, resolved while it was
 *  still live. The provider's anchor schema needs the offsets, so a body
 *  selection always carries them and the bridge relays the frame's. */
interface PendingAnchor {
  quote: string
  prefix?: string
  suffix?: string
  startOffset?: number
  endOffset?: number
}

/** Shape of the `GET /api/remote-artifacts/{provider}/{external_id}` response.
 *  These are the provider `fetch_content` contract fields — flat **snake_case**
 *  (`content_type`, `current_version`, `view_url`, `owner`), forwarded verbatim
 *  through `_redact_remote_response` (which never renames keys). Read them as
 *  snake_case, matching `RemoteArtifactListing`/`RemoteArtifactCard`; a camelCase
 *  mismatch here leaves every field `undefined` (HTML/Markdown fall back to plain
 *  text, version defaults to 1, owner + "Open original" disappear). */
interface RemoteArtifactDetail {
  external_id?: string
  title?: string
  summary?: string
  owner?: string
  visibility?: string
  content_type?: string
  current_version?: number
  view_url?: string
  content?: string
  tags?: string[]
  /** Some providers may nest the metadata under "artifact" instead of returning
   *  it flat; flatten both shapes below so field reads resolve either way. */
  artifact?: RemoteArtifactDetail
}

/** Read-only detail view for a provider-hosted artifact the user is viewing
 *  but does NOT own locally (no fork). Renders the remote content and the same
 *  comment sidebar — comments post straight to the provider (scope=shared) and
 *  are TTL-cached server-side. Reached from the Shared/Public browse list. */
export default function RemoteArtifactDetailPage() {
  const { provider = '', externalId = '' } = useParams<{ provider: string; externalId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { theme, colorTheme, themeVersion } = useTheme()
  // Collapsed by default; auto-reveals once the artifact has comments (effect
  // below). Session-only — deliberately not persisted, so an empty comment
  // panel never stays open on a dashboard/infographic.
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const sidebarUserToggledRef = useRef(false)
  const [forking, setForking] = useState(false)
  const [forkError, setForkError] = useState('')
  const { confirm, confirmDialog } = useConfirm()
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const mdPreviewRef = useRef<HTMLDivElement>(null)
  const iframeBodyRef = useRef<HTMLDivElement>(null)
  const [flashComment, setFlashComment] = useState<{ id: string; nonce: number } | null>(null)
  const [iframeScrollTarget, setIframeScrollTarget] = useState<{ id: string; nonce: number } | null>(null)
  const mdScrollerRef = useRef<HTMLDivElement>(null)
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null)
  const [mdScrollNonce, setMdScrollNonce] = useState(0)

  const detailQuery = useQuery<RemoteArtifactDetail>({
    queryKey: ['remote-artifact', provider, externalId],
    queryFn: () => api.remoteArtifactDetail(provider, externalId),
    enabled: !!externalId,
  })
  const commentsQuery = useQuery<{ comments: ArtifactComment[]; remote_sync_error?: string | null }>({
    queryKey: ['remote-artifact-comments', provider, externalId],
    queryFn: () => api.remoteArtifactComments(provider, externalId),
    enabled: !!externalId,
    staleTime: 30_000,
  })

  const raw = detailQuery.data
  // The detail response may nest metadata under `artifact`, with content +
  // view_url at the top level. Flatten so content_type (and title/owner/version)
  // resolve — otherwise isHtml is always false and the page renders raw source
  // instead of the iframe.
  // Memoized because this object is the dep of `onAddAnchored` (it reads
  // `current_version` for the anchor) and, via `art?.content`, of the srcdoc
  // memo: rebuilt every render it would rebuild both, and a new srcdoc string
  // remounts the sandboxed iframe. React Query keeps `data` referentially stable
  // between refetches that resolve deep-equal, so this changes only on real data.
  const art: RemoteArtifactDetail | undefined = useMemo(() => {
    if (!raw) return undefined
    const meta = raw.artifact ?? raw
    return { ...meta, content: raw.content ?? meta.content, view_url: raw.view_url ?? meta.view_url }
  }, [raw])
  const comments = commentsQuery.data?.comments ?? []
  const remoteSyncError = commentsQuery.data?.remote_sync_error ?? null

  const invalidateComments = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['remote-artifact-comments', provider, externalId] })
  }, [queryClient, provider, externalId])

  const toggleSidebar = useCallback(() => {
    sidebarUserToggledRef.current = true
    setSidebarOpen(v => !v)
  }, [])
  // Auto-reveal the sidebar when the artifact has comments; collapse when none.
  // Skipped once the user takes manual control via the toggle. Navigating to a
  // different remote artifact (this component is reused across the route)
  // clears that override so each artifact gets the comment-driven default.
  const sidebarNavRef = useRef(externalId)
  useEffect(() => {
    if (sidebarNavRef.current !== externalId) {
      sidebarNavRef.current = externalId
      sidebarUserToggledRef.current = false
    }
    if (sidebarUserToggledRef.current) return
    setSidebarOpen(comments.length > 0)
  }, [externalId, comments.length])

  // Writes go through useMutation (use-react-query guideline): cache
  // invalidation is centralized, and each mutation's `error` is rendered below
  // (`commentWriteError`) so a refused post/reply/status change/delete is
  // reported rather than just re-fetched. Status-change + delete on a shared
  // artifact write straight through to the provider.
  const postMut = useMutation({
    mutationFn: (vars: { text: string; anchor?: object }) =>
      api.postRemoteArtifactComment(provider, externalId, vars),
    onSuccess: invalidateComments, onError: invalidateComments,
  })
  const replyMut = useMutation({
    mutationFn: (vars: { parentId: string; text: string }) =>
      api.replyRemoteArtifactComment(provider, externalId, vars.parentId, { text: vars.text }),
    onSuccess: invalidateComments, onError: invalidateComments,
  })
  const markReviewMut = useMutation({
    mutationFn: (id: string) => api.markReviewRemoteComment(provider, externalId, id),
    onSuccess: invalidateComments, onError: invalidateComments,
  })
  const deleteMut = useMutation({
    mutationFn: (id: string) => api.deleteRemoteComment(provider, externalId, id),
    onSuccess: invalidateComments, onError: invalidateComments,
  })
  // The most recent refused write. react-query clears a mutation's error on its
  // next `mutate`, and Dismiss resets all four, so a stale failure cannot linger
  // past the user's next attempt.
  const commentWriteError = postMut.error?.message
    ?? replyMut.error?.message
    ?? markReviewMut.error?.message
    ?? deleteMut.error?.message
    ?? null
  const dismissCommentWriteError = useCallback(() => {
    postMut.reset(); replyMut.reset(); markReviewMut.reset(); deleteMut.reset()
  }, [postMut, replyMut, markReviewMut, deleteMut])
  const onAdd = useCallback((text: string) => { postMut.mutate({ text }) }, [postMut])
  const onReply = useCallback((parentId: string, text: string) => { replyMut.mutate({ parentId, text }) }, [replyMut])
  const onMarkReview = useCallback((id: string) => { markReviewMut.mutate(id) }, [markReviewMut])
  const onDelete = useCallback((id: string) => { deleteMut.mutate(id) }, [deleteMut])
  const noop = useCallback(() => {}, [])
  // Anchored commenting inside the HTML render: the bridge relays a selection
  // (with the frame's offsets), the toolbar opens the composer from it, and the
  // post goes out scope=shared with the anchor; highlights + bidirectional scroll.
  const { scrollToAnchor } = useCommentBridge({
    iframeRef,
    comments,
    activeId: activeCommentId,
    onSelect: (sel: IframeSelection) => {
      stageIframeSelection({ quote: sel.quote, prefix: sel.prefix, suffix: sel.suffix, startOffset: sel.startOffset, endOffset: sel.endOffset }, { text: sel.quote, x: sel.x, y: sel.y, start: sel.startOffset })
    },
    onHighlightClick: (id: string) => { setActiveCommentId(id); setFlashComment({ id, nonce: Date.now() }) },
  })
  useEffect(() => { if (iframeScrollTarget?.id) scrollToAnchor(iframeScrollTarget.id) }, [iframeScrollTarget, scrollToAnchor])
  const submitAnchored = useCallback((text: string, pending: PendingAnchor): Promise<boolean> => {
    // Resolve, never throw: on `false` the toolbar keeps the typed text and its
    // persisted draft for a retry; the panel reveal is a reaction to a STORED
    // comment, so it waits for success.
    return postMut.mutateAsync({
      text,
      // The provider's create_comment REQUIRES start/end offsets + versionNumber
      // on the anchor; the remote post has no local store to fall back to, so an
      // incomplete anchor is rejected and the comment silently disappears.
      anchor: {
        quote: pending.quote,
        prefix: pending.prefix,
        suffix: pending.suffix,
        start_offset: pending.startOffset ?? 0,
        end_offset: pending.endOffset ?? (pending.startOffset ?? 0) + pending.quote.length,
        version_number: art?.current_version ?? 1,
      },
    }).then(() => {
      // Hand control back to the comment-driven default after an anchored add:
      // reveal now, and clear the manual override so a later delete-all collapses.
      sidebarUserToggledRef.current = false
      setSidebarOpen(true)
      return true
    }, () => false)
  }, [postMut, art])

  const forkNow = useCallback(async () => {
    setForking(true)
    setForkError('')
    try {
      const res = await api.forkRemoteArtifact(provider, externalId)
      if (res.error) setForkError(res.error)
      else navigate(`/artifacts/${encodeURIComponent(res.slug)}`)
    } catch (e: unknown) {
      setForkError(e instanceof Error ? e.message : i18nT('pages.remoteArtifactDetailPage.fork_failed'))
    } finally {
      setForking(false)
    }
  }, [provider, externalId, navigate])

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const ctype = (art?.content_type ?? '').toLowerCase()
  const isHtml = ctype.includes('html')
  const isMarkdown = ctype.includes('markdown') || ctype.includes('text/md')
  const srcdoc = useMemo(
    () => (isHtml && art?.content ? buildSrcdoc({ html: art.content, themeVars, mode: theme, enableComments: true }) : null),
    [isHtml, art?.content, themeVars, theme],
  )
  // A gateway-served document, not a `blob:` URL — the same reason the artifact
  // and widget frames moved: some WebKit-based in-app browsers refuse a blob
  // load outright and can take the whole page down with it.
  const { url: blobUrl, failed, pending, retry } = useSandboxDoc(srcdoc)
  // A mint can succeed while the frame never fires `load` — a visible-but-blank
  // frame here, since this surface has no opacity gate. `silent` overlays the
  // same notice + retry the mint-`failed` branch uses so the blank is explained
  // and recoverable. Same watch ArtifactBody / WidgetFrame use; keep the four
  // in step.
  const { silent: loadSilent, onLoaded: onFrameLoaded } = useSilentLoadWatch(blobUrl)

  // Anchored-comment highlights for the remote markdown body use the SAME
  // DOM-rect overlay as the local artifact page (InlineCommentOverlay), so
  // markdown highlighting is one reliable mechanism everywhere; the HTML path
  // uses the iframe bridge above. Clicking a highlight/bubble flashes the
  // matching sidebar row.
  const onMarkdownActivate = useCallback((id: string) => {
    setActiveCommentId(id)
    setFlashComment({ id, nonce: Date.now() })
  }, [])

  // Anchored-comment create on the remote markdown body: a text selection opens
  // the toolbar's composer, whose `onOpen` resolves the anchor here while the
  // selection is still live (posts scope=shared with the anchor). Mirrors the
  // local page's markdown selection path; the quote+prefix/suffix re-anchor the
  // highlight.
  const resolveMdSelectionAnchor = useCallback((highlightOwner: object): PendingAnchor | null => {
    if (!isMarkdown) return null
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null
    const root = mdPreviewRef.current
    if (!root) return null
    // The SAME containment predicate the toolbar opened the composer with, so
    // a selection it accepted is never rejected here — a triple-click on the
    // last paragraph ends at a boundary point OUTSIDE the preview, and a
    // rejection would post a quote-only anchor pinned at offset 0 to the
    // provider. The returned range is clamped to the preview.
    const range = containedSelectionRange(sel.getRangeAt(0), root)
    if (!range) return null
    const raw = range.toString()
    if (!raw.trim()) return null
    const quote = raw.trim()
    // Derive the selection's real offset from the Range, NOT full.indexOf(quote):
    // indexOf finds the FIRST occurrence, so selecting a later repeat would store
    // prefix/suffix (and the start/end offsets sent to the provider) for the
    // wrong spot and mis-anchor the highlight. Range.toString() for both the full
    // text and the pre-selection slice keeps the offset space consistent and
    // matches the textContent the highlighter searches.
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
    return { quote, prefix, suffix, startOffset: idx, endOffset: idx + quote.length }
  }, [isMarkdown])

  const confirmDiscardDraft = useCallback(() => confirm({
    title: i18nT('components.markdownPanel.discard_unsaved_comment'),
    confirmLabel: i18nT('components.markdownPanel.discard_comment_button'),
  }), [confirm])
  const quoteOf = useCallback((a: PendingAnchor) => a.quote, [])
  const quoteOnly = useCallback((quote: string): PendingAnchor => ({ quote }), [])
  // The shared composer wiring (pending/staged anchors, external selection,
  // highlight, draft mirror + guard). The draft store is per remote artifact,
  // per passage, so Back / Fork / a reload bring a typed comment back when its
  // passage is selected again; Back and Fork ask before leaving a draft behind.
  const {
    selectionComposer, iframeSelection, stageIframeSelection, guardCommentDraft,
  } = useSelectionComposerAnchor<PendingAnchor>({
    resolveDomAnchor: resolveMdSelectionAnchor, quoteOf, quoteOnly, submit: submitAnchored,
    draftKey: `mc-remote-artifact-composer-draft:${provider}/${externalId}`, confirmDiscard: confirmDiscardDraft,
  })
  const handleFork = useCallback(() => { void guardCommentDraft(() => { void forkNow() }) }, [guardCommentDraft, forkNow])
  const goBack = useCallback(() => { void guardCommentDraft(() => navigate('/artifacts')) }, [guardCommentDraft, navigate])
  // No row action beside the composer: the box already carries Add comment and
  // Close, and a third control would break the two-per-row cap. Copying the
  // selection is the composer's own Cmd/Ctrl+C while its input is empty.
  const selectionActions: SelectionAction[] = useMemo(() => [], [])

  if (detailQuery.isLoading) return <div className="p-6 text-muted">{i18nT('pages.remoteArtifactDetailPage.loading')}</div>
  if (detailQuery.isError || !art) {
    const failed = detailQuery.isError
    const msg = detailQuery.error instanceof Error ? detailQuery.error.message : i18nT('pages.remoteArtifactDetailPage.failed_to_load_remote_artifact')
    return (
      <>
        <div className="sticky top-0 z-10 bg-bg border-b border-border">
          <PageHeader title={i18nT('pages.remoteArtifactDetailPage.remote_artifact')} subtitle={externalId} />
          <div className="px-4 md:px-6 py-2 flex flex-wrap items-center gap-2">
            <Btn onClick={() => navigate('/artifacts')} className="flex items-center gap-1">
              <ArrowLeft size={13} /> {i18nT('pages.remoteArtifactDetailPage.back')}
            </Btn>
          </div>
        </div>
        <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
          {failed ? (
            <Card>
              {/* askAgent on: the detail read rejected, so nothing else rendered —
                  no comment draft exists on this branch to lose. */}
              <ErrorNotice
                title={i18nT('pages.remoteArtifactDetailPage.failed_to_load_remote_artifact')}
                message={msg}
                askAgent
                testId="remote-artifact-detail-error"
              />
              <div className="mt-3"><Btn onClick={() => navigate('/artifacts')}>{i18nT('pages.remoteArtifactDetailPage.back_to_library')}</Btn></div>
            </Card>
          ) : (
            // The provider answered but had no artifact under this id: an empty
            // state, not a failure — the same plain note the local detail page uses.
            <Card>
              <div className="text-sm text-muted">{i18nT('pages.artifactDetailPage.not_found')}</div>
              <div className="mt-3"><Btn onClick={() => navigate('/artifacts')}>{i18nT('pages.remoteArtifactDetailPage.back_to_library')}</Btn></div>
            </Card>
          )}
        </div>
      </>
    )
  }

  const title = art.title || externalId
  // The external "open original" link comes from a provider response, so
  // validate the scheme (http/https only) before rendering it — a
  // malicious/compromised provider could otherwise supply a
  // javascript:/file:/data: URL that executes or launches on click.
  const safeArtifactUrl = safeHttpUrl(art.view_url ?? '')

  return (
    <>
      <div className="sticky top-0 z-10 bg-bg border-b border-border">
        <PageHeader title={title} subtitle={i18nT('pages.remoteArtifactDetailPage.remote_artifact_2', { provider })} />
        <div className="px-4 md:px-6 py-2 flex flex-wrap items-center gap-2">
          <Btn onClick={goBack} className="flex items-center gap-1">
            <ArrowLeft size={13} /> {i18nT('pages.remoteArtifactDetailPage.back')}
          </Btn>
          {art.visibility && <Badge variant="ok">{art.visibility}</Badge>}
          {art.owner && (
            <span className="inline-flex items-center gap-1 text-[12px] text-muted">
              <User size={12} /> {art.owner}
            </span>
          )}
          {art.current_version != null && <span className="text-[12px] text-muted">{i18nT('pages.remoteArtifactDetailPage.v')}{art.current_version}</span>}
          {(art.tags ?? []).map(t => (
            <span key={t} className="text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted">{t}</span>
          ))}
          <span className="ml-auto flex items-center gap-2">
            <Btn
              type="button"
              onClick={toggleSidebar}
              className={sidebarOpen ? 'border-accent text-accent bg-accent-subtle' : ''}
              title={sidebarOpen ? i18nT('pages.remoteArtifactDetailPage.hide_comments') : i18nT('pages.remoteArtifactDetailPage.show_comments')}
              aria-pressed={sidebarOpen}
            >
              <MessageSquare size={13} /> {i18nT('pages.remoteArtifactDetailPage.comments')}
              {comments.length > 0 && <span className="ml-0.5 px-1 rounded bg-accent/20 text-[10px]">{comments.length}</span>}
            </Btn>
            {safeArtifactUrl && (
              <Btn
                type="button"
                onClick={() => window.open(safeArtifactUrl, '_blank', 'noopener,noreferrer')}
                title={i18nT('pages.remoteArtifactDetailPage.open_the_original_on_the_remote_provider')}
              >
                <ExternalLink size={13} /> {i18nT('pages.remoteArtifactDetailPage.open_original')}
              </Btn>
            )}
            <Btn
              type="button"
              primary
              onClick={handleFork}
              disabled={forking}
              title={i18nT('pages.remoteArtifactDetailPage.fork_into_your_local_artifacts_editable_copy')}
            >
              {forking ? <Loader2 size={13} className="animate-spin" /> : <GitFork size={13} />} {i18nT('pages.remoteArtifactDetailPage.fork')}
            </Btn>
          </span>
        </div>
      </div>
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">

        {art.summary && <div className="mb-3 text-sm text-muted italic">{art.summary}</div>}
        {forkError && (
          /* No hand-off: the comments sidebar's draft shares this page —
             navigating away would discard an in-progress comment. */
          <ErrorNotice message={forkError} className="mb-3" />
        )}
        {commentsQuery.isError && (
          /* No hand-off: the comments sidebar's comment draft (and an open
             anchored-comment composer) share this page — navigating away would
             discard an in-progress comment. */
          <ErrorNotice
            message={commentsQuery.error?.message}
            className="mb-3"
            testId="remote-artifact-comments-error"
          />
        )}
        {commentWriteError && (
          /* No hand-off: the comments sidebar's comment draft is exactly what a
             refused post/reply leaves behind — navigating away would discard it. */
          <ErrorNotice
            message={commentWriteError}
            onDismiss={dismissCommentWriteError}
            className="mb-3"
            testId="remote-artifact-comment-write-error"
          />
        )}

        <div className="flex gap-4 items-start">
          <div className="flex-1 min-w-0">
            {isHtml ? (
              <div ref={iframeBodyRef} className="relative rounded-xl border border-border bg-card overflow-hidden" style={{ minHeight: 480 }}>
                {blobUrl ? (
                  /* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onLoad is a frame-load lifecycle handler (it clears the silent-load watch), not a user interaction; the frame's own content is what a keyboard reaches, and nothing here can be triggered from one */
                  <iframe
                    ref={iframeRef}
                    src={blobUrl}
                    onLoad={onFrameLoaded}
                    sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
                    // NO clipboard-write delegation here, deliberately. These frames host
                    // agent-generated HTML whose scripts run on load, so a delegated
                    // permission would let one overwrite the user's clipboard with no Copy
                    // action at all. Copying still works: lib/widgetSrcdoc.ts injects an
                    // execCommand fallback that a real button press satisfies and a
                    // gesture-less on-load script does not.
                    className="w-full border-none bg-card"
                    style={{
                      height: 'calc(100vh - 240px)',
                      minHeight: 480,
                      // See ArtifactBody's frame: a laid-out document whose first
                      // paint is skipped shows an empty box, and promoting the
                      // frame to its own compositing layer is the remedy that
                      // needs no post-load timing. The remote detail frame was
                      // left out when the local one was promoted.
                      transform: 'translateZ(0)',
                    }}
                    title={i18nT('pages.remoteArtifactDetailPage.remote_artifact_3', { name: externalId })}
                  />
                ) : failed ? (
                  <div className="p-6 flex items-center gap-3 text-text">
                    <span>{i18nT('components.artifactBody.could_not_render')}</span>
                    <Btn onClick={retry} disabled={pending} className="flex items-center gap-1">
                      <RotateCw className="lucide-inline" />
                      {i18nT('components.artifactBody.retry')}
                    </Btn>
                  </div>
                ) : <div className="p-6 text-muted">{i18nT('pages.remoteArtifactDetailPage.rendering')}</div>}
                {/* Recovery overlay for a frame that is STILL mounted (blobUrl
                    present). Two states can strand a mounted frame with no
                    in-flow recovery, because the ternary above picks <iframe>
                    whenever blobUrl is set and so never reaches its own `failed`
                    branch while a (possibly spent) url survives:
                      - loadSilent: the mint succeeded but the frame never fired
                        `load` -- a blank box. Cause-neutral copy + Eye/"Show
                        artifact" (a frame that did not report is not a proven
                        failure).
                      - failed: a re-mint (e.g. taking "Show artifact") rejected
                        while the previous url stayed in place (see useSandboxDoc),
                        so blobUrl is still truthy and `failed` is now true. That
                        is a known read failure -- could_not_render + RotateCw/
                        "Retry". Without this the overlay's old `!failed` guard hid
                        it AND the ternary kept showing the spent iframe, leaving
                        no notice and no recovery at all.
                    `failed` wins when both hold: a known failed mint is the more
                    specific diagnosis. */}
                {blobUrl && (failed || loadSilent) && (
                  <div className="absolute top-0 left-0 right-0 z-10 p-6 flex flex-wrap items-center gap-3 text-text bg-bg-elevated/95 border-b border-border">
                    {failed && !pending ? (
                      // A rejected mint is a known read failure, rendered through
                      // ErrorNotice per the errors-use-error-notice rule. The
                      // RotateCw + "Retry" Btn below is the recovery.
                      // No hand-off: the comments sidebar's draft (and an open
                      // anchored-comment composer) share this page - the hand-off
                      // navigates to the chat and unmounts the whole page, not
                      // just this frame, so it would discard an in-progress
                      // comment. The three sibling ErrorNotices on this page keep
                      // the hand-off off for the same reason.
                      <ErrorNotice
                        variant="inline"
                        testId="remote-artifact-silent-mint-error"
                        className="min-w-0"
                        message={i18nT('components.artifactBody.could_not_render')}
                      />
                    ) : (
                      // Silent load (or a re-mint in flight): a frame that did
                      // not report, NOT a proven failure, so cause-neutral copy
                      // in a live region and an Eye action labeled by what it
                      // does. An ErrorNotice here would assert a failure the
                      // surface cannot verify.
                      <span role="status" className="min-w-0">
                        {i18nT(pending
                          ? 'components.artifactBody.rendering'
                          : 'components.artifactBody.no_longer_showing')}
                      </span>
                    )}
                    <Btn onClick={retry} disabled={pending} className="flex items-center gap-1">
                      {failed ? <RotateCw className="lucide-inline" /> : <Eye className="lucide-inline" />}
                      {i18nT(failed
                        ? 'components.artifactBody.retry'
                        : 'components.artifactBody.show_artifact')}
                    </Btn>
                  </div>
                )}
              </div>
            ) : (
              <div ref={mdScrollerRef} className="relative rounded-xl border border-border bg-card overflow-auto p-5" style={{ minHeight: 480, height: 'calc(100vh - 240px)' }}>
                {isMarkdown
                  ? <div ref={mdPreviewRef} className="msg-content text-sm leading-relaxed"><MarkdownRenderer content={art.content ?? ''} /></div>
                  : <pre className="text-[13px] text-text whitespace-pre-wrap break-words font-mono">{art.content ?? ''}</pre>}
                {isMarkdown && comments.length > 0 && (
                  <InlineCommentOverlay
                    scrollRef={mdScrollerRef}
                    textRef={mdPreviewRef}
                    comments={comments}
                    activeId={activeCommentId}
                    scrollNonce={mdScrollNonce}
                    onActivate={onMarkdownActivate}
                  />
                )}
              </div>
            )}
            {/* One composer for both bodies: a markdown selection opens it from
                the DOM, an HTML selection through the bridge as `iframeSelection`
                only (the frame's wrapper holds render-failure copy of its own).
                A plain-source body has neither, so it gets no toolbar. */}
            {isHtml && <SelectionToolbar containerRef={iframeBodyRef} actions={selectionActions} composer={selectionComposer} externalSelection={iframeSelection} externalOnly />}
            {isMarkdown && <SelectionToolbar containerRef={mdPreviewRef} actions={selectionActions} composer={selectionComposer} />}
          </div>

          {sidebarOpen && (
            <CommentsSidebar
              comments={comments}
              loading={commentsQuery.isFetching}
              remoteSyncError={remoteSyncError}
              onAdd={onAdd}
              onReply={onReply}
              onResolve={noop}
              onMarkReview={onMarkReview}
              onDelete={onDelete}
              onRefresh={invalidateComments}
              onClose={toggleSidebar}
              onCommentClick={isHtml ? (id: string) => { setActiveCommentId(id); setIframeScrollTarget({ id, nonce: Date.now() }) } : (id: string) => { setActiveCommentId(id); setMdScrollNonce(n => n + 1) }}
              activeCommentId={activeCommentId}
              flashCommentId={flashComment}
              hideResolve
              hideDelete
            />
          )}
        </div>
      </div>
      {confirmDialog}
    </>
  )
}
