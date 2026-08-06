import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { ArrowLeft, AlertTriangle, ExternalLink, GitFork, Loader2, User, MessageSquare } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { safeHttpUrl } from '../lib/safeUrl'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { THEME_VAR_NAMES, buildSrcdoc } from '../lib/widgetSrcdoc'
import { api } from '../api/client'
import { PageHeader, Card, Badge, Btn } from '../components/ui'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { CommentsSidebar } from '../components/CommentsSidebar'
import { CommentPopover } from '../components/CommentOverlay'
import { InlineCommentOverlay } from '../components/InlineCommentOverlay'
import { useCommentBridge, type IframeSelection } from '../hooks/useCommentBridge'
import type { ArtifactComment } from '../types'

import { i18nT } from '../i18n/t'
function readThemeVars(): Record<string, string> {
  if (typeof window === 'undefined' || typeof document === 'undefined') return {}
  const computed = getComputedStyle(document.documentElement)
  const out: Record<string, string> = {}
  for (const name of THEME_VAR_NAMES) {
    const v = sanitizeCssValue(computed.getPropertyValue(name))
    if (v) out[name] = v
  }
  return out
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
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const mdPreviewRef = useRef<HTMLDivElement>(null)
  const [popover, setPopover] = useState<{ x: number; y: number; quote: string; prefix?: string; suffix?: string; startOffset?: number; endOffset?: number } | null>(null)
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
  const meta = raw?.artifact ?? raw
  const art: RemoteArtifactDetail | undefined = raw
    ? { ...meta, content: raw.content ?? meta?.content, view_url: raw.view_url ?? meta?.view_url }
    : undefined
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

  // Writes go through useMutation (use-react-query guideline): errors aren't
  // swallowed and cache invalidation is centralized. Status-change + delete on
  // a shared artifact write straight through to the provider.
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
  const onAdd = useCallback((text: string) => { postMut.mutate({ text }) }, [postMut])
  const onReply = useCallback((parentId: string, text: string) => { replyMut.mutate({ parentId, text }) }, [replyMut])
  const onMarkReview = useCallback((id: string) => { markReviewMut.mutate(id) }, [markReviewMut])
  const onDelete = useCallback((id: string) => { deleteMut.mutate(id) }, [deleteMut])
  const noop = useCallback(() => {}, [])
  // Anchored commenting inside the HTML render: selection -> popover (posts
  // scope=shared with anchor), highlights + bidirectional scroll.
  const { scrollToAnchor } = useCommentBridge({
    iframeRef,
    comments,
    activeId: activeCommentId,
    onSelect: (sel: IframeSelection) => setPopover({ x: sel.x, y: sel.y, quote: sel.quote, prefix: sel.prefix, suffix: sel.suffix, startOffset: sel.startOffset, endOffset: sel.endOffset }),
    onHighlightClick: (id: string) => { setActiveCommentId(id); setFlashComment({ id, nonce: Date.now() }) },
  })
  useEffect(() => { if (iframeScrollTarget?.id) scrollToAnchor(iframeScrollTarget.id) }, [iframeScrollTarget, scrollToAnchor])
  const onAddAnchored = useCallback((text: string) => {
    if (!popover) return
    postMut.mutate({
      text,
      // The provider's create_comment REQUIRES start/end offsets + versionNumber
      // on the anchor; the remote post has no local store to fall back to, so an
      // incomplete anchor is rejected and the comment silently disappears.
      anchor: {
        quote: popover.quote,
        prefix: popover.prefix,
        suffix: popover.suffix,
        start_offset: popover.startOffset ?? 0,
        end_offset: popover.endOffset ?? (popover.startOffset ?? 0) + popover.quote.length,
        version_number: art?.current_version ?? 1,
      },
    })
    // Hand control back to the comment-driven default after a popover add:
    // reveal now, and clear the manual override so a later delete-all collapses.
    sidebarUserToggledRef.current = false
    setSidebarOpen(true)
    setPopover(null)
    window.getSelection()?.removeAllRanges()
  }, [popover, postMut, art])

  const handleFork = useCallback(async () => {
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
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!srcdoc) { setBlobUrl(null); return }
    const blob = new Blob([srcdoc], { type: 'text/html;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    setBlobUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [srcdoc])

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
  // the popover (posts scope=shared with the anchor). Mirrors the local page's
  // markdown selection path; the quote+prefix/suffix re-anchor the highlight.
  const handleMdMouseUp = useCallback(() => {
    if (!isMarkdown) return
    const sel = window.getSelection()
    const raw = sel?.toString() ?? ''
    if (!sel || sel.isCollapsed || !raw.trim()) return
    const root = mdPreviewRef.current
    if (!root || !sel.anchorNode || !root.contains(sel.anchorNode)) return
    const range = sel.getRangeAt(0)
    if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return
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
    const rect = range.getBoundingClientRect()
    const startOffset = idx
    const endOffset = idx + quote.length
    setPopover({ x: rect.left, y: rect.bottom, quote, prefix, suffix, startOffset, endOffset })
  }, [isMarkdown])

  if (detailQuery.isLoading) return <div className="p-6 text-muted">{i18nT('pages.remoteArtifactDetailPage.loading')}</div>
  if (detailQuery.error || !art) {
    const msg = detailQuery.error instanceof Error ? detailQuery.error.message : i18nT('pages.remoteArtifactDetailPage.failed_to_load_remote_artifact')
    return (
      <>
        <PageHeader title={i18nT('pages.remoteArtifactDetailPage.remote_artifact')} subtitle={externalId} />
        <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
          <Card>
            <div className="flex items-start gap-3">
              <AlertTriangle className="lucide-inline text-danger" />
              <div>
                <div className="text-sm text-danger font-medium">{i18nT('pages.remoteArtifactDetailPage.failed_to_load_remote_artifact')}</div>
                <div className="text-[13px] text-muted mt-1">{msg}</div>
              </div>
            </div>
            <div className="mt-3"><Btn onClick={() => navigate('/artifacts')}>{i18nT('pages.remoteArtifactDetailPage.back_to_library')}</Btn></div>
          </Card>
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
      <PageHeader title={title} subtitle={i18nT('pages.remoteArtifactDetailPage.remote_artifact_2', { provider })} />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="flex flex-wrap items-center gap-2 mb-4">
          <Btn onClick={() => navigate('/artifacts')} className="flex items-center gap-1">
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

        {art.summary && <div className="mb-3 text-sm text-muted italic">{art.summary}</div>}
        {forkError && (
          <div className="mb-3 px-3 py-2 rounded-md border border-danger/40 bg-danger-subtle text-[13px] text-danger">{forkError}</div>
        )}

        <div className="flex gap-4 items-start">
          <div className="flex-1 min-w-0">
            {isHtml ? (
              <div className="rounded-xl border border-border bg-card overflow-hidden" style={{ minHeight: 480 }}>
                {blobUrl ? (
                  <iframe
                    ref={iframeRef}
                    src={blobUrl}
                    sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
                    className="w-full border-none bg-card"
                    style={{ height: 'calc(100vh - 240px)', minHeight: 480 }}
                    title={i18nT('pages.remoteArtifactDetailPage.remote_artifact_3', { name: externalId })}
                  />
                ) : <div className="p-6 text-muted">{i18nT('pages.remoteArtifactDetailPage.rendering')}</div>}
              </div>
            ) : (
              <div ref={mdScrollerRef} className="relative rounded-xl border border-border bg-card overflow-auto p-5" style={{ minHeight: 480, height: 'calc(100vh - 240px)' }}>
                {isMarkdown
                  ? <div ref={mdPreviewRef} onMouseUp={handleMdMouseUp} className="msg-content text-sm leading-relaxed"><MarkdownRenderer content={art.content ?? ''} /></div>
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
            {popover && (
              <CommentPopover
                x={popover.x}
                y={popover.y}
                onSubmit={onAddAnchored}
                onCancel={() => { setPopover(null); window.getSelection()?.removeAllRanges() }}
              />
            )}
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
    </>
  )
}
