import { memo, useEffect, useMemo, useRef, useState } from 'react'
import DOMPurify from 'dompurify'
import { Download, Image as ImageIcon, ImageOff } from 'lucide-react'
import hljs from '../utils/hljs'
import { useTheme } from '../hooks/useTheme'
import { useCommentBridge, type IframeSelection } from '../hooks/useCommentBridge'
import { InlineCommentOverlay } from './InlineCommentOverlay'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { THEME_VAR_NAMES, buildSrcdoc } from '../lib/widgetSrcdoc'
import { ContentRenderer, langFor, wrapCode } from './ContentRenderer'
import type { FileType } from './FileRenderers'
import type { Artifact, ArtifactComment } from '../types'

import { i18nT } from '../i18n/t'
// Shared renderer for the full-page route and the chat side-panel Artifacts
// tab (markdown/text/json/svg natively via ContentRenderer; widget/html via
// the sandboxed iframe).

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

/** Map an artifact `kind` to the FileType the ContentRenderer expects.
 * Only used for non-iframe kinds (widget/html still go through the iframe). */
export function fileTypeForKind(kind: Artifact['kind']): FileType {
  switch (kind) {
    case 'markdown': return 'markdown'
    case 'json':     return 'json'
    case 'svg':      return 'svg'
    case 'text':     return 'code'
    // image is served as raw bytes from its asset URL, not rendered from
    // content — it never flows through ContentRenderer, but map it explicitly
    // so a stray call can't misroute image bytes into the markdown path.
    case 'image':    return 'image'
    // widget / html shouldn't reach here, but fall back to markdown rather
    // than throwing — keeps the page survivable for unexpected enum values.
    default:         return 'markdown'
  }
}

/** Pseudo-extension used when rendering text artifacts as code. */
export function extForKind(kind: Artifact['kind']): string {
  switch (kind) {
    case 'json': return '.json'
    case 'svg':  return '.svg'
    case 'text': return '.txt'
    default:     return '.md'
  }
}

/** Whether this artifact kind supports inline editing.
 * Widget / html are agent-managed (raw HTML editing has too many edge cases —
 * see design discussion); markdown / text / json / svg are
 * editable text formats. */
export function isEditableKind(kind: Artifact['kind']): boolean {
  return kind === 'markdown' || kind === 'text' || kind === 'json' || kind === 'svg'
}

/** Renders a non-iframe artifact body — markdown / text / json / svg.
 * Theme vars are inherited naturally because we're not in a sandboxed
 * iframe; nothing to inject. When `editing` is true, swaps the preview
 * for a Monaco code editor. The `previewRef` is owned by the parent so
 * the detail page / side panel can attach selection-to-comment handlers
 * above it. */
export const ArtifactBodyNative = memo(function ArtifactBodyNative({
  kind, content, editing, onChange, previewRef,
  comments, activeCommentId, scrollNonce, onActivateComment, unreadRootIds,
  heightStyle, flush,
}: {
  kind: Artifact['kind']
  content: string
  editing: boolean
  onChange: (v: string) => void
  previewRef: React.RefObject<HTMLDivElement | null>
  comments?: ArtifactComment[]
  activeCommentId?: string | null
  scrollNonce?: number
  onActivateComment?: (id: string) => void
  unreadRootIds?: Set<string>
  /** Override the body height/min-height (the side panel fills its
   *  flex container instead of the full-page `calc(100vh - 240px)`). */
  heightStyle?: React.CSSProperties
  /** Drop the card chrome (border + rounding + reading padding) and render
   *  edge-to-edge, matching how `MarkdownPanel` renders a FILE in the chat side
   *  panel. The full-page route keeps the card, because there the artifact is a
   *  document floating on the page background and the border is what bounds it;
   *  inside the panel that same border is a redundant box drawn just inside the
   *  panel's own border, and it made a markdown artifact look nothing like the
   *  markdown file rendered by the tab next to it. Also forwarded to
   *  `ContentRenderer`, whose non-markdown paths draw a second border of their
   *  own unless told to run flush. */
  flush?: boolean
}) {
  const fileType = fileTypeForKind(kind)
  const ext = extForKind(kind)
  const isRichType = fileType === 'json' || fileType === 'svg' || fileType === 'html' || fileType === 'image' || fileType === 'csv' || fileType === 'pdf'
  const isMarkdown = fileType === 'markdown'
  const lang = langFor(ext)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const displayContent = isMarkdown ? content : wrapCode(content, ext)
  const highlightedHtml = useMemo(() => {
    if (isMarkdown || editing || isRichType) return ''
    try { return DOMPurify.sanitize(hljs.highlight(content, { language: lang }).value) + '\n' }
    catch { return DOMPurify.sanitize(hljs.highlightAuto(content).value) + '\n' }
  }, [content, lang, isMarkdown, editing, isRichType])
  // Comment overlay for every natively-rendered body that has a previewRef —
  // markdown (rendered DOM) AND the <pre> path (text/json/svg). Widgets/HTML use
  // the iframe bridge instead.
  const showOverlay = !editing && !!onActivateComment && (comments?.length ?? 0) > 0
  return (
    <div
      ref={scrollerRef}
      className={`relative overflow-auto ${flush ? '' : 'rounded-xl border border-border bg-card'}`}
      style={heightStyle ?? { minHeight: 480, height: 'calc(100vh - 240px)' }}
    >
      <div className={flush ? 'h-full' : 'p-5 h-full'}>
        <ContentRenderer
          isRichType={isRichType}
          fileType={fileType}
          content={content}
          editing={editing}
          lang={lang}
          lineNums={true}
          wordWrap={true}
          autocomplete={false}
          onChange={onChange}
          previewRef={previewRef}
          displayContent={displayContent}
          isMarkdown={isMarkdown}
          highlightedHtml={highlightedHtml}
          flush={flush}
          markdownClassName="msg-content text-sm leading-relaxed"
        />
      </div>
      {showOverlay && (
        <InlineCommentOverlay
          scrollRef={scrollerRef}
          textRef={previewRef}
          comments={comments ?? []}
          activeId={activeCommentId ?? null}
          scrollNonce={scrollNonce}
          unreadRootIds={unreadRootIds}
          onActivate={onActivateComment!}
        />
      )}
    </div>
  )
})

/** Renders widget / html artifacts in a sandboxed iframe with theme-var
 * injection. */
export const ArtifactBodyIframe = memo(function ArtifactBodyIframe({
  artifact, slug, previewStyle, comments, onSelect, onOpenThread, scrollToCommentId, activeId, unreadRootIds,
  heightStyle,
}: {
  artifact: Artifact
  slug: string
  /** Reading-width constraint applied to the full-page wrapper (max-width).
   *  Merged with the default min-height; does not replace the iframe height. */
  previewStyle?: React.CSSProperties
  comments?: ArtifactComment[]
  onSelect?: (sel: IframeSelection) => void
  onOpenThread?: (commentId: string, rect?: { x: number; y: number; w: number; h: number }) => void
  scrollToCommentId?: { id: string; nonce: number } | null
  activeId?: string | null
  unreadRootIds?: Set<string>
  /** Override the iframe height (side-panel fit). */
  heightStyle?: React.CSSProperties
}) {
  const { theme, colorTheme, themeVersion } = useTheme()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const srcdoc = useMemo(
    () => artifact.content ? buildSrcdoc({ html: artifact.content, themeVars, mode: theme, enableComments: true }) : null,
    [artifact.content, themeVars, theme],
  )
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  useEffect(() => {
    // Clear the stale blob URL when srcdoc goes falsy (e.g. artifact.content
    // empties while the panel is open); the cleanup below revokes the old URL,
    // so leaving blobUrl set would point the iframe at a dead blob.
    if (!srcdoc) {
      setBlobUrl(null)
      return
    }
    const blob = new Blob([srcdoc], { type: 'text/html;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    setBlobUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [srcdoc])
  // Bridge: push anchored highlights into the iframe, surface in-iframe text
  // selections (-> popover) and highlight clicks (-> flash the sidebar row).
  const { scrollToAnchor, onIframeLoad } = useCommentBridge({
    iframeRef, comments: comments ?? [], onSelect, onOpenThread, activeId, unreadRootIds,
  })
  useEffect(() => {
    if (scrollToCommentId?.id) scrollToAnchor(scrollToCommentId.id)
  }, [scrollToCommentId, scrollToAnchor])
  return (
    // Honor the side-panel's heightStyle on the wrapper too (mirrors
    // ArtifactBodyNative); otherwise the hardcoded minHeight forces 480px and
    // overflows the panel's flex container. Falls back to the 480 default
    // merged with the full-page reading-width previewStyle (max-width).
    <div className="rounded-xl border border-border bg-card overflow-hidden" style={heightStyle ?? { minHeight: 480, ...previewStyle }}>
      {blobUrl ? (
        <iframe
          ref={iframeRef}
          src={blobUrl}
          onLoad={onIframeLoad}
          sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
          className="w-full border-none bg-card"
          style={heightStyle ?? { height: 'calc(100vh - 240px)', minHeight: 480 }}
          title={i18nT('components.artifactBody.artifact', { slug })}
        />
      ) : (
        <div className="p-6 text-muted">{i18nT('components.artifactBody.rendering')}</div>
      )}
    </div>
  )
})

/** The URL the image bytes stream from. The server sets Content-Type, so the
 * browser renders it directly in an <img> / downloads it via an anchor — the
 * artifact JSON never carries base64. Slugs are already URL-safe, but encode
 * defensively so an unexpected character can't break the path. */
export function artifactAssetUrl(slug: string): string {
  return `/api/artifacts/${encodeURIComponent(slug)}/asset`
}

/** Renders an image artifact: the picture itself streamed from the asset URL,
 * plus a Download control pointing at the same URL. No Monaco, no iframe —
 * image is not an editable text kind, so it never reaches ArtifactBodyNative /
 * ArtifactBodyIframe. Reads the optional `image` metadata defensively: `alt`
 * drives the accessible description (falling back to the artifact name), and
 * `width`/`height` set the intrinsic aspect ratio so the layout doesn't jump
 * before the bytes arrive. Download names the file from `original_filename`,
 * or a slug + extension when that is absent. */
export const ArtifactBodyImage = memo(function ArtifactBodyImage({
  artifact, slug, heightStyle,
}: {
  artifact: Artifact
  slug: string
  /** Override the body height/min-height (side-panel fit), mirroring the other
   *  bodies. Falls back to the full-page reading height. */
  heightStyle?: React.CSSProperties
}) {
  const url = artifactAssetUrl(slug)
  const alt = artifact.image?.alt || artifact.name
  const downloadName =
    artifact.image?.original_filename || `${slug}.${artifact.image?.ext || 'png'}`
  // The asset endpoint can legitimately fail (missing sidecar, unreadable file,
  // a mime the read path refuses). Without this the user gets the browser's
  // broken-image glyph and no explanation on an otherwise healthy page.
  const [failed, setFailed] = useState(false)
  return (
    <div
      className="rounded-xl border border-border bg-card overflow-hidden flex flex-col"
      style={heightStyle ?? { minHeight: 480, height: 'calc(100vh - 240px)' }}
    >
      <div className="flex-1 min-h-0 flex items-center justify-center overflow-auto p-4">
        {failed ? (
          <div className="flex flex-col items-center gap-2 text-center">
            <ImageOff size={24} className="text-muted" aria-hidden="true" />
            <span className="text-sm text-muted">
              {i18nT('components.artifactBody.image_could_not_be_loaded')}
            </span>
          </div>
        ) : (
          <img
            src={url}
            alt={alt}
            width={artifact.image?.width}
            height={artifact.image?.height}
            loading="lazy"
            className="max-w-full max-h-full object-contain"
            draggable={false}
            onError={() => setFailed(true)}
          />
        )}
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-2 text-sm text-muted">
        <span className="flex items-center gap-1.5 min-w-0">
          <ImageIcon size={14} className="shrink-0" />
          <span className="truncate">{downloadName}</span>
        </span>
        <a
          href={url}
          download={downloadName}
          className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-text hover:border-border-strong transition-colors shrink-0"
        >
          <Download size={14} /> {i18nT('pages.artifactDetailPage.download')}
        </a>
      </div>
    </div>
  )
})
