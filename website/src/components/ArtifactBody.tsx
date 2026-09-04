import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { Download, Eye, Image as ImageIcon, ImageOff, RotateCw } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { useSandboxDoc } from '../hooks/useSandboxDoc'
import { useScrollMemory } from '../hooks/useScrollMemory'
import { useCommentBridge, type IframeSelection } from '../hooks/useCommentBridge'
import { InlineCommentOverlay } from './InlineCommentOverlay'
import { Btn } from './ui'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { THEME_VAR_NAMES, buildSrcdoc } from '../lib/widgetSrcdoc'
import {
  widgetHeightKey, getWidgetHeight, setWidgetHeight, estimateWidgetHeight,
  clampFrameHeight,
} from '../utils/widgetHeights'
import { ContentRenderer, langFor, wrapCode } from './ContentRenderer'
import type { FileType } from './FileRenderers'
import type { Artifact, ArtifactComment } from '../types'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
// Shared renderer for the full-page route and the chat side-panel Artifacts
// tab (markdown/text/json/svg natively via ContentRenderer; widget/html via
// the sandboxed iframe).

/** Key space for measured detail-frame heights. Heights are only comparable
 * between frames laid out at the same width, and the full-page detail frame is
 * neither the chat/panel width (the empty space) nor the thumbnail's fixed base
 * width — so it keeps its own space rather than reading and polluting theirs. */
const DETAIL_HEIGHT_SPACE = 'artifact-detail'

/** Height of the placeholder / failure box shown when there is NO document to
 * size against. Deliberately not a floor on a rendered document: a floor is
 * what put a short artifact in a taller frame and forced a nested scroll on a
 * phone. */
const NO_DOCUMENT_BOX_HEIGHT = 480

/** How long after a frame reports `load` its document has to report a height
 * before the surface offers a retry.
 *
 * The document URL is single-use — the gateway spends it on the first GET — so a
 * navigation the ENGINE starts on its own (memory pressure, a back/forward cache
 * eviction) re-requests a spent URL and lands the frame on a 404 page. That
 * fires `load` like any other navigation, so without this the user is left with
 * a silent empty box and no affordance, while `failed` stays false because the
 * mint itself succeeded. Every document this surface builds carries the injected
 * height reporter, and the reporter re-posts unconditionally after its own
 * window `load` (see HEIGHT_REPORTER_BODY) precisely so that a report racing
 * ahead of the load event -- layout settles before images finish -- cannot be
 * the last one; silence past this window therefore means the frame is showing
 * something that is not ours.
 *
 * Deliberately NOT an automatic re-mint: a second `load` also happens when a
 * link inside an artifact navigates the frame, and silently pulling the reader
 * back would fight an action they took. Offering the re-mint is the right
 * response to either cause — in both, the frame has stopped showing the artifact
 * — which is why the window re-arms on EVERY load rather than only on a new url.
 * The two causes cannot be told apart from outside the opaque sandbox, so the
 * notice for this state is cause-neutral ("no longer showing" / "Show artifact")
 * rather than the `failed` state's failure claim. */
const DOC_REPORT_GRACE_MS = 3000

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
 * for a Pierre code editor. The `previewRef` is owned by the parent so
 * the detail page / side panel can attach selection-to-comment handlers
 * above it. */
export const ArtifactBodyNative = memo(function ArtifactBodyNative({
  kind, content, editing, onChange, previewRef,
  comments, activeCommentId, scrollNonce, onActivateComment, unreadRootIds,
  heightStyle, flush, scrollMemoryKey,
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
  /** Cross-remount scroll identity (slot + tab id) — see `useScrollMemory`.
   *  Passed only by the side panel's EMBEDDED body: a chat-slot switch
   *  unmounts that instance, and this brings the document back where the
   *  user left it. The full-page route and the fullscreen overlay omit it
   *  (different lifecycles, and a second instance sharing the key would
   *  fight the first over recording). */
  scrollMemoryKey?: string
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const fileType = fileTypeForKind(kind)
  const ext = extForKind(kind)
  const isRichType = fileType === 'json' || fileType === 'svg' || fileType === 'html' || fileType === 'image' || fileType === 'csv' || fileType === 'pdf'
  const isMarkdown = fileType === 'markdown'
  const lang = langFor(ext)
  const scrollerRef = useRef<HTMLDivElement>(null)
  // This div is the REAL scroll container for natively-rendered artifacts —
  // the panel's outer wrapper never overflows (measured in the #5701 capture
  // harness), so the memory must live here to observe anything.
  const scrollMemory = useScrollMemory(scrollMemoryKey, scrollerRef, content !== '')
  const displayContent = isMarkdown ? content : wrapCode(content, ext)
  // Comment overlay for every natively-rendered body that has a previewRef —
  // markdown (rendered DOM) AND the code path (text/json/svg). Widgets/HTML use
  // the iframe bridge instead.
  const showOverlay = !editing && !!onActivateComment && (comments?.length ?? 0) > 0
  return (
    <div
      ref={scrollerRef}
      onScroll={scrollMemory.onScroll}
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
          onChange={onChange}
          previewRef={previewRef}
          displayContent={displayContent}
          isMarkdown={isMarkdown}
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
   *  Does not affect the frame's height, which follows its document. */
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const { theme, colorTheme, themeVersion } = useTheme()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const iframeRef = useRef<HTMLIFrameElement>(null)
  // Set once the current document reports its own height, which is the signal
  // that the frame is showing a document THIS surface built rather than an error
  // page — see DOC_REPORT_GRACE_MS.
  const reportedRef = useRef(false)
  const loadedUrlRef = useRef<string | null>(null)
  const [docSilent, setDocSilent] = useState(false)
  const [loadNonce, setLoadNonce] = useState(0)
  // The frame is sized to its document's OWN height, never to a fixed box.
  //
  // A frame taller than its content is not merely cosmetic: iOS WebKit leaves
  // such a frame unpainted, so a short artifact rendered as an empty box on
  // iPhone while the SAME document was fine in the gallery — whose thumbnail
  // frame is always shorter than its content — and fine on desktop, which
  // paints either way. Measured on the reporting device: content 385/465px in a
  // 573px frame was blank; 617/800px in the same frame rendered. Sizing to the
  // reported height removes that condition rather than working around it, and
  // it also drops the nested scroll, so a phone scrolls the page instead of a
  // pane inside the page.
  const heightKey = useMemo(
    () => widgetHeightKey(artifact.content ?? '', DETAIL_HEIGHT_SPACE),
    [artifact.content],
  )
  const [frameHeight, setFrameHeight] = useState(
    () => clampFrameHeight(getWidgetHeight(heightKey) ?? estimateWidgetHeight(DETAIL_HEIGHT_SPACE)),
  )
  // A different artifact adopts its own measured height when there is one. With
  // no sample for it, the current height is kept until the document reports —
  // reverting to the space's median would be a visible jump in exchange for
  // nothing, since the real value is one message away.
  useEffect(() => {
    const cached = getWidgetHeight(heightKey)
    if (cached != null) setFrameHeight(clampFrameHeight(cached))
  }, [heightKey])
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      // Source-checked: the document is built from model- or user-authored HTML
      // and its scripts can postMessage the parent directly, so a report is only
      // honored from THIS frame's own window.
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return
      if (e.data?.type !== 'mc-widget-height') return
      const raw = e.data.height
      if (typeof raw !== 'number' || !Number.isFinite(raw)) return
      // Applied as reported. The reporter already defers a SHRINK in-document
      // (a reload or a JIT reflow briefly measures smaller before the content
      // settles), so re-deferring here would only add a second delay to a value
      // that has already waited.
      // Bounded at both ends, and bounded again when read back from the cache.
      // Not a threat model — ordinary CSS (`min-height` in viewport units) makes a
      // document's height depend on the frame's viewport, and a self-sizing frame
      // then feeds its own measurement. See clampFrameHeight for the measurements.
      const next = clampFrameHeight(raw)
      reportedRef.current = true
      setDocSilent(false)
      setFrameHeight(prev => (prev === next ? prev : next))
      setWidgetHeight(heightKey, next)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [heightKey])
  const srcdoc = useMemo(
    () => artifact.content
      ? buildSrcdoc({
        html: artifact.content, themeVars, mode: theme, enableComments: true,
        includeHeightReporter: true,
      })
      : null,
    [artifact.content, themeVars, theme],
  )
  // One shared hook rather than this effect in four components: the previous
  // document survives both an in-flight and a failed re-mint, and `failed`
  // clears on a successful settle (or when the srcdoc goes away) — `pending`
  // acknowledges the click.
  // See hooks/useSandboxDoc.ts for why each rule
  // exists.
  const { url: blobUrl, failed, pending, retry } = useSandboxDoc(srcdoc)
  // A new document starts the observation over. Declared before the arming
  // effect below so a url change clears the previous document's verdict in the
  // same commit that re-arms.
  useEffect(() => {
    reportedRef.current = false
    setDocSilent(false)
  }, [blobUrl])
  useEffect(() => {
    // Only arm for a load that belongs to the CURRENT url: on a re-mint the
    // nonce still carries the previous document's load, and arming on that would
    // start the window before the new document has had a chance to load at all.
    if (!blobUrl || loadedUrlRef.current !== blobUrl) return
    if (reportedRef.current) return
    const timer = setTimeout(() => {
      if (!reportedRef.current) setDocSilent(true)
    }, DOC_REPORT_GRACE_MS)
    return () => clearTimeout(timer)
  }, [loadNonce, blobUrl])
  // Visibility is gated on the FIRST document only, and deliberately NOT reset
  // when a new url lands. A re-mint (theme settle, content refetch, retry)
  // navigates the frame again, and re-hiding on every new url leaves the frame
  // blank until the next `load` fires — a document that is already rendering
  // must not be taken away from the reader to cover a swap it cannot see. A
  // brief engine canvas during that swap is the lesser cost.
  const [everLoaded, setEverLoaded] = useState(false)
  // Bridge: push anchored highlights into the iframe, surface in-iframe text
  // selections (-> popover) and highlight clicks (-> flash the sidebar row).
  const { scrollToAnchor, onIframeLoad } = useCommentBridge({
    iframeRef, comments: comments ?? [], onSelect, onOpenThread, activeId, unreadRootIds,
  })
  useEffect(() => {
    if (scrollToCommentId?.id) scrollToAnchor(scrollToCommentId.id)
  }, [scrollToCommentId, scrollToAnchor])
  return (
    // The side panel's heightStyle still wins outright — that surface fits a
    // fixed pane and scrolls inside it. On the full page the wrapper takes no
    // height of its own: the frame it contains is exactly its document's height,
    // so a floor here would put a short artifact in a taller frame and bring
    // back the nested scroll on a phone. The placeholder/failure box is the one
    // case with no document to size against.
    <div
      className="relative rounded-xl border border-border bg-card overflow-hidden"
      style={heightStyle ?? {
        ...(blobUrl ? {} : { minHeight: NO_DOCUMENT_BOX_HEIGHT }),
        ...previewStyle,
      }}
    >
      {/* Two shapes for the same notice, because the two situations read very
          differently. With a document still showing, the notice OVERLAYS it: a
          failed re-mint must not displace what the user is reading. With no
          document at all, the same overlay is a thin strip along the top of an
          empty box, which reads as a broken page rather than a failed load — so
          it centres instead.

          Two COPIES too, because only one of the states is a known failure.
          `failed` means the mint itself failed, so asserting a render failure is
          accurate and the action is honestly a retry. `docSilent` means the frame
          navigated to something that is not ours, and from outside the opaque
          sandbox an engine renavigation onto a spent url (a failure) is
          indistinguishable from the reader following a link inside the artifact
          (deliberate). The copy must not assert a failure the surface cannot
          verify, and the action — the same re-mint — is labeled by what it does
          for the user (bring the artifact back), not as a "retry" of an error
          they may not have had. `failed` wins when both are set: a known failed
          mint is the more specific diagnosis. */}
      {(failed || docSilent) && (
        <div
          className={
            blobUrl
              ? 'absolute top-0 left-0 right-0 z-10 px-6 py-3 flex flex-wrap items-center gap-3 text-text bg-bg-elevated/95 border-b border-border'
              : 'absolute inset-0 z-10 flex flex-wrap items-center justify-center gap-3 text-text'
          }
        >
          {/* The live region is the text span, not the container: a region
              holding the button would re-announce the control's name as status
              prose on every state flip (implicit aria-atomic). While a re-mint
              is in flight it carries the existing "Rendering…" string so a
              screen-reader user who pressed the action hears that something
              happened — the visual disabled state alone is silent to AT. */}
          <span role="status" className="min-w-0">
            {i18nT(pending
              ? 'components.artifactBody.rendering'
              : failed
                ? 'components.artifactBody.could_not_render'
                : 'components.artifactBody.no_longer_showing')}
          </span>
          {/* The click is acknowledged by DISABLING the button, never by
              clearing `docSilent`: a re-mint can resolve with the same url
              string (a React no-op — no new `load`, so nothing would ever
              re-arm the silence window) or hang, and a notice cleared at click
              time would leave the reader on a dead frame with no affordance at
              all. The glyph branches with the copy: a reload arrow asserts the
              same failure the docSilent string just stopped claiming. Btn (the
              design-system button), not a raw `btn btn-sm` button: that class
              has no CSS behind it, and this control is the only recovery
              affordance the reader has — it must look pressable. */}
          <Btn
            disabled={pending}
            onClick={retry}
          >
            {failed ? <RotateCw className="lucide-inline" /> : <Eye className="lucide-inline" />}
            {i18nT(failed
              ? 'components.artifactBody.retry'
              : 'components.artifactBody.show_artifact')}
          </Btn>
        </div>
      )}
      {blobUrl ? (
        <>
          {/* The frame is TRANSPARENT until its FIRST document reports load, with
              a themed panel underneath. Swapping the placeholder for the iframe
              the moment the URL arrives shows the browser's own canvas for the
              length of the document fetch — and some engines paint that canvas
              WHITE regardless of this element's background, which reads as a flash
              on every open. WidgetFrame already fades in on load for this reason;
              this is the same guard on the artifact frame. It covers the first
              load ONLY — see `everLoaded` for why a re-mint must not re-hide a
              document that is already rendering. */}
          {!everLoaded && (
            <div aria-hidden className="absolute inset-0 bg-card" />
          )}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onLoad is a document-load lifecycle handler (it arms the silent-document watch and reveals the frame), not a user interaction; the frame's own content is what a keyboard reaches, and nothing here can be triggered from one */}
          <iframe
            ref={iframeRef}
            src={blobUrl}
            onLoad={() => {
              loadedUrlRef.current = blobUrl
              // Every load restarts the observation, not just a new url. The case
              // this exists for is the engine renavigating a SPENT url after a
              // successful render (a back/forward-cache eviction), so keeping a
              // previous document's report would skip the window exactly when it
              // is needed and leave a 404 in the frame with no affordance.
              reportedRef.current = false
              setDocSilent(false)
              setLoadNonce(n => n + 1)
              setEverLoaded(true)
              onIframeLoad?.()
            }}
            sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
            className="w-full border-none bg-card"
            style={{
              ...(heightStyle ?? { height: frameHeight }),
              // iOS WebKit skips this frame's FIRST paint: measured on the
              // reporting device, the document loaded, its injected scripts ran,
              // it reported a correct 385px layout height, and it sat in a
              // visible 385px frame — painting nothing. Four unrelated
              // invalidations applied AFTER that state each made it appear (a
              // 1px resize, a transform toggle, an opacity flip, a display
              // toggle), so the engine had laid the document out and simply
              // never rasterized it. Promoting the frame to its own compositing
              // layer is the one remedy that needs no timing: the other three
              // have to be fired after load, and anything scheduled off a load
              // event is a race on a slow connection.
              transform: 'translateZ(0)',
              // `color-scheme` is cheap insurance on top: it tells the engine
              // which base canvas to paint for the embedded document before that
              // document's own CSS is parsed.
              colorScheme: theme,
              opacity: everLoaded ? 1 : 0,
              transition: 'opacity .12s ease',
            }}
            title={i18nT('components.artifactBody.artifact', { slug })}
          />
        </>
      ) : (
        !failed && (
          <div className="p-6 text-muted">{i18nT('components.artifactBody.rendering')}</div>
        )
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
 * plus a Download control pointing at the same URL. No editor, no iframe —
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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
          // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle handler (degrade to the "could not be loaded" notice), not a user interaction; there is nothing here for a keyboard to reach
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
