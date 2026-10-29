/**
 * Mochi - Markdown rendering for chat messages
 *
 * The remark and rehype chains (depth-bounded; raw HTML admitted, then
 * sanitized), the element overrides that turn file paths into chips and local
 * image paths into images read over the app API, and `StreamingMarkdown`, which
 * closes an unfinished code fence while a reply is still streaming. One of the
 * owners `ChatPanel.tsx` composes: the panel and `ChatPanelBubble.tsx` render
 * through it, and `./ChatPanel` re-exports `fixStreamingFences`. Upstream Mochi
 * keeps this code in `src/renderer/ChatPanel.tsx`.
 */
import React from 'react'
import { Eye, File, Folder, PawPrint } from 'lucide-react'
import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { rehypeSanitize, rehypeStableRootKeys, remarkVerbatimUnknownTags } from '../../../../components/MarkdownRenderer'
import { capWhitespaceRuns, remarkBoundDepth, rehypeBoundRawDepth } from '../../../../utils/markdownDepthBound'
import { classifyPlatform } from '../../../../hooks/useGatewayPlatform'
import { WidgetFrame, parseWidgets, hasWidgets } from './WidgetFrame'
import { api } from '../mochiApi'
import { MochiCodeBlock } from '../../panel/MochiCodeBlock'
import { i18nT } from '../../../../i18n/t'
import ErrorNotice from '../../../../components/ErrorNotice'
import { useLanguageGeneration } from '../../../../i18n/useLanguageGeneration'
import { electronPlatform, isElectron } from '../../../../lib/electron'

const LocalImage: React.FC<{ path: string; onClickImage?: (src: string) => void }> = ({ path, onClickImage }) => {
  const [src, setSrc] = React.useState<string | null>(null)
  const [failed, setFailed] = React.useState(false)
  React.useEffect(() => {
    // Each path starts from the loading state, and a read that settles after
    // the path has moved on is dropped, so one path's bytes never paint over
    // another's.
    let cancelled = false
    setSrc(null)
    setFailed(false)
    const read = api?.readLocalImage?.(path)
    if (!read) { setFailed(true); return }
    read.then(
      (b64: string | null) => {
        if (cancelled) return
        if (b64) setSrc(`data:image/png;base64,${b64}`)
        else setFailed(true)
      },
      () => { if (!cancelled) setFailed(true) },
    )
    return () => { cancelled = true }
  }, [path])
  // A null read (missing file, refused path, non-2xx) is final: report it with
  // the path instead of a placeholder that would read as "still loading"
  // forever. Inline variant inside an inline-styled wrapper, as the panel's
  // send-error notice does: this panel receives core CSS variables only, so the
  // notice's Tailwind utilities resolve to nothing and the colour and size come
  // from the wrapper by inheritance.
  if (failed) {
    return (
      <span style={{ display: 'inline-flex', alignItems: 'center', color: 'var(--danger, #e5484d)', fontSize: 11 }}>
        {/* No hand-off: this image renders inside the Mochi chat panel, whose
            composer text and pending attachments live only in ChatPanel's local
            state, and the hand-off navigates away and would discard that
            unsent draft. */}
        <ErrorNotice
          variant="inline"
          message={`${i18nT('components.markdownRenderer.image_failed_to_load')}: ${path}`}
        />
      </span>
    )
  }
  if (!src) return <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>...</span>
  // Hands back the PATH, not `src` (the data: URL built from the bytes). Every
  // consumer wants to DO something with the file -- open it in the OS viewer --
  // and a data URL cannot be opened. Passing the rendered URL made each call
  // site look wired while doing nothing.
  //
  // A real <button>, not a clickable <img>: the transcript renders the image at
  // 200px, so opening the file at full size is a CONTROL, and only the native
  // element puts it in the tab order and fires it on Enter/Space. `inline-flex`
  // with zero chrome keeps the image inline and at exactly its own size, and the
  // button carries the accessible name so the image takes an empty alt rather
  // than being announced twice.
  return <button type="button" onClick={() => onClickImage?.(path)}
    aria-label={i18nT('apps.mochi.chatPanel.open_image')}
    style={{ display: 'inline-flex', padding: 0, border: 'none', background: 'none', cursor: 'zoom-in' }}>
    <img src={src} alt=""
      style={{ maxWidth: 200, maxHeight: 200, borderRadius: 4, marginTop: 4, cursor: 'zoom-in' }} />
  </button>
}

/**
 * Streaming-aware markdown renderer.
 * Ported from Kiro Crew's MarkdownRenderer — handles incomplete code fences
 * during streaming so partial responses render as proper markdown instead of
 * raw text.
 */
const StreamingMarkdown = React.memo<{ content: string }>(({ content }) => {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // The whitespace-run cap (see markdownDepthBound) is applied per TEXT
  // segment below, after widget extraction, so a widget body reaches
  // WidgetFrame byte-identical -- its <pre> indentation included.
  const cleaned = content.replace(/^\n+/, '')
  // If there's a complete widget in the stream, render it
  if (hasWidgets(cleaned)) {
    const segments = parseWidgets(cleaned)
    // Anything after the last complete widget may still be streaming
    const lastWidget = segments.findLastIndex(s => s.type === 'widget')
    return (
      <>
        {segments.map((seg, i) => {
          if (seg.type === 'widget') return <WidgetFrame key={i} html={seg.content} title={seg.title} />
          // Last text segment after final widget — still streaming
          if (i > lastWidget) {
            const stripped = seg.content.replace(/<mcwidget[\s\S]*$/, '')
            const prepared = fixStreamingFences(capWhitespaceRuns(stripped))
            return <React.Fragment key={i}>
              <Markdown remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{prepared}</Markdown>
              <span style={{ animation: 'blink 1s step-end infinite', display: 'inline-flex', verticalAlign: 'middle' }}><PawPrint size={11} /></span>
            </React.Fragment>
          }
          return <Markdown key={i} remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{capWhitespaceRuns(seg.content)}</Markdown>
        })}
      </>
    )
  }
  // Strip any partial/unclosed <mcwidget tag during streaming
  const stripped = cleaned.replace(/<mcwidget[\s\S]*$/, '')
  const prepared = fixStreamingFences(capWhitespaceRuns(stripped))
  return (
    <>
      <Markdown remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{prepared}</Markdown>
      <span style={{ animation: 'blink 1s step-end infinite', display: 'inline-flex', verticalAlign: 'middle' }}><PawPrint size={11} /></span>
    </>
  )
})

/** Ensure blank line before fences glued to text, and close any unclosed fence. */
export function fixStreamingFences(s: string): string {
  // The info string may be a tag plus an optional `{...}` attribute block,
  // matching the dashboard's fixCodeFences: a ``` run followed by more words
  // is a mid-sentence mention, not a fence.
  s = s.replace(/([^\n])(\n?)(```(?:[ \t]*[^`\s]+)?(?:[ \t]+\{[^`\n]*\})?[ \t\r]*\n)/g, (_, pre, nl, fence) =>
    nl ? pre + nl + fence : pre + '\n\n' + fence
  )
  // If there's an odd number of ``` fences, the last one is unclosed — close it
  const fenceCount = (s.match(/^```/gm) || []).length
  if (fenceCount % 2 === 1) s += '\n```'
  return s
}

// `remarkBoundDepth` first: it bounds the parsed tree's depth inside parse(),
// ahead of remark-gfm's recursive post-parse transform. Shared with the
// core renderer, for the same reason the sanitizer is: this panel parses
// the same untrusted message content through the same kind of pipeline.
const MD_REMARK = [remarkBoundDepth, remarkGfm, remarkVerbatimUnknownTags]
/**
 * Raw HTML must be ADMITTED, then SANITIZED — in that order.
 *
 * Without `rehype-raw`, react-markdown leaves an `html` node unhandled and its
 * source text is emitted verbatim, so a model writing `<br>` (the only way to
 * break a line inside a GFM table cell) put a literal "<br>" in the bubble.
 * The sanitizer is the core's, imported rather than copied: admitting raw HTML
 * is exactly the point where a second, drifting allowlist would become a hole.
 */
// ``rehypeStableRootKeys`` goes LAST, after ``rehypeSanitize``, and the order is
// load-bearing rather than cosmetic: the sanitizer keeps only allowlisted
// attributes, and ``style`` is on neither the global list nor any list for
// ``div``. Ahead of it the wrapper would lose ``display: contents`` and become a
// real layout box around every block, which is a visible regression that the
// keys it stabilises would not reveal.
const MD_REHYPE = [rehypeBoundRawDepth, rehypeRaw, rehypeSanitize, rehypeStableRootKeys]

/**
 * Typed against react-markdown's own `Components`, so each override receives the
 * real props of the tag it replaces (the same contract the dashboard's
 * MarkdownRenderer uses) instead of an `any` that hides a misspelled prop.
 */
const mdComponents: Components = {
  // react-markdown's defaultUrlTransform rewrites a destination whose scheme is
  // outside its allowlist (and an empty `[x]()` destination) to href="". An
  // anchor with an empty href still paints as a live link and its "Copy Link
  // Address" resolves to the current page URL, so a refused destination renders
  // as inert text instead -- same degradation as md-notebook's Preview.
  //
  // On the anchor path, `href` and the children are restated after the spread --
  // both already arrive in `p`, so this is the same anchor at runtime -- because
  // an <a> whose href is only ever supplied by a spread is indistinguishable
  // from a bare <a onClick>: it is not focusable and Enter does not fire it, and
  // neither a reader nor the linter can tell it apart from a real link.
  a: (p) => p.href ? <a {...p} href={p.href} style={{ color: 'var(--accent)', textDecoration: 'none', cursor: 'pointer' }}
    onMouseEnter={(e) => (e.currentTarget.style.textDecoration = 'underline')}
    onMouseLeave={(e) => (e.currentTarget.style.textDecoration = 'none')}
    onClick={(e) => { e.preventDefault(); const href = p.href; if (href) api?.openExternal?.(href) }}>{p.children}</a>
    : <span>{p.children}</span>,
  table: (p) => <table style={{ borderCollapse: 'collapse', fontSize: 11, width: '100%', margin: '4px 0' }} {...p} />,
  th: (p) => <th style={{ border: '1px solid var(--border)', padding: '3px 6px', textAlign: 'left', fontWeight: 600 }} {...p} />,
  td: (p) => <td style={{ border: '1px solid var(--border)', padding: '3px 6px' }} {...p} />,
  code: (p) => {
    // Whole class token, not its leading `\w+` run: `language-error-report`
    // labels as `error-report`, not `error` (same rule as MarkdownRenderer).
    const match = /language-(\S+)/.exec(p.className || '')
    if (match) {
      return <MochiCodeBlock lang={match[1]} code={String(p.children).replace(/\n$/, '')} />
    }
    // Detect inline code that looks like a file path
    const text = String(p.children)
    if (/^(?:\/(?:Users|home|tmp|var|etc)\/[^\s]+|[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_.-]+)+)\.(?:md|txt|json|yaml|yml|toml|py|ts|js|sh|cfg|conf|log|csv)$/.test(text)) {
      return <FileChip path={text} />
    }
    return <code style={{ background: 'var(--bg-input)', borderRadius: 3, padding: '1px 4px', fontSize: 11 }} {...p} />
  },
  pre: (p) => <>{p.children}</>,
  p: (p) => {
    // Detect file paths in paragraph text and render as FileChip
    const children = React.Children.toArray(p.children)
    const processed = children.map((child, i) => {
      if (typeof child !== 'string') return child
      // Match absolute file paths (not inside backticks — those go through code component)
      const fileRe = /((?:\/(?:Users|home|tmp|var|etc)\/[^\s,;:'"<>()]+|[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_.-]+)+)\.(?:md|txt|json|yaml|yml|toml|py|ts|js|sh|cfg|conf|log|csv))/g
      const parts: (string | React.ReactElement)[] = []
      let lastIdx = 0
      let match: RegExpExecArray | null
      while ((match = fileRe.exec(child)) !== null) {
        if (match.index > lastIdx) parts.push(child.slice(lastIdx, match.index))
        parts.push(<FileChip key={`fc-${i}-${match.index}`} path={match[1]} />)
        lastIdx = match.index + match[0].length
      }
      if (lastIdx < child.length) parts.push(child.slice(lastIdx))
      return parts.length > 1 ? <React.Fragment key={i}>{parts}</React.Fragment> : child
    })
    return <p style={{ margin: '2px 0' }}>{processed}</p>
  },
  ul: (p) => <ul style={{ margin: '2px 0', paddingLeft: 16 }} {...p} />,
  ol: (p) => <ol style={{ margin: '2px 0', paddingLeft: 16 }} {...p} />,
  li: (p) => <li style={{ marginBottom: 1 }} {...p} />,
  img: (p) => {
    const src = p.src
    // Any LOCAL FILE path goes through LocalImage, which reads the bytes over the
    // app API and renders a data URL. A bare `<img src="/Users/…/x.png">` cannot
    // work in a page: the browser resolves it against the gateway origin and 404s.
    //
    // This used to be gated on `.kiro/crew/screenshots/` only, so every UPLOADED
    // image — which the route stores under `uploads/`, and which is how dropped
    // files and now crops are referenced — rendered as a broken image. The test
    // is the shape of the path, not one directory.
    //
    // `startsWith('/')` alone is not enough: gateway-relative URLs like
    // `/assets/logo.png` also start with a slash and must stay ordinary <img>.
    if (isLocalFilePath(src)) {
      // `src` (the path), NOT the data URL LocalImage renders: the OS viewer
      // opens a file, and openLightbox rejects data: URLs for that reason.
      return <LocalImage path={src} onClickImage={() => api?.openLightbox?.(src)} />
    }
    // The markdown author's own alt text, defaulting to '' — an image written
    // without one is decorative, and an explicit empty alt is what tells a screen
    // reader to skip it instead of falling back to reading out the URL.
    return <img {...p} alt={p.alt ?? ''} style={{ maxWidth: '100%', borderRadius: 4, marginTop: 4, cursor: 'zoom-in' }} />
  },
}

/**
 * Does this src name a file on THIS machine (rather than a URL the page can load)?
 *
 * Mirrors the absolute-path roots the paragraph renderer already matches for file
 * chips, plus the app's own data home, so a path under `screenshots/` or
 * `uploads/` is recognised wherever the data home lives.
 */
function isLocalFilePath(src: unknown): src is string {
  if (typeof src !== 'string' || !src.startsWith('/')) return false
  return /^\/(?:Users|home|tmp|var|private|etc)\//.test(src) || src.includes('.kiro/crew/')
}

/** Inline file chip — shows shortened path with preview + reveal buttons */
const FileChip: React.FC<{ path: string }> = ({ path: filePath }) => {
  const parts = filePath.split('/')
  const short = parts.length > 3 ? `…/${parts.slice(-2).join('/')}` : parts.slice(-2).join('/')
  // The SHELL's platform, not the gateway's: `revealFile` is an IPC send Mochi's
  // Electron main process handles, so that host owns which application opens. A
  // browser tab has no shell to report one — and no shell to reveal anything
  // either — so it takes the generic wording.
  const hostPlatform = classifyPlatform(electronPlatform())
  const revealLabel = hostPlatform === 'darwin'
    ? i18nT('apps.mochi.chatPanel.open_in_finder')
    : hostPlatform === 'windows'
      ? i18nT('apps.mochi.chatPanel.open_in_file_explorer')
      : i18nT('apps.mochi.chatPanel.show_in_file_manager')
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      background: 'var(--bg-input)', border: '1px solid var(--border)',
      borderRadius: 6, padding: '2px 6px 2px 5px', margin: '2px 0',
      fontSize: 11, lineHeight: 1.3, verticalAlign: 'middle',
    }}>
      <File size={11} color="var(--text-muted)" />
      {/* Preview and reveal both delegate to the shell bridge (window.mochi),
          published only by the Electron preload — in a browser tab the calls
          are silent no-ops, so the dead controls are withheld rather than
          rendered. Inside the shell the reveal label names that host's own file
          manager, since the shell is what performs the reveal. */}
      {isElectron ? (
        <>
          <span style={{ color: 'var(--text)', cursor: 'pointer', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
            title={filePath}
            role="button"
            tabIndex={0}
            onClick={() => api?.previewFile?.(filePath)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); api?.previewFile?.(filePath) } }}
          >{short}</span>
          <button onClick={() => api?.previewFile?.(filePath)} title={i18nT('apps.mochi.chatPanel.preview')} aria-label={i18nT('apps.mochi.chatPanel.preview')} style={{
            background: 'none', border: 'none', padding: '1px', cursor: 'pointer',
            color: 'var(--text-muted)', display: 'flex', alignItems: 'center',
            transition: 'color 0.15s',
          }}
            onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--accent)')}
            onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--text-muted)')}
          ><Eye size={11} /></button>
          <button onClick={() => api?.revealFile?.(filePath)} title={revealLabel} aria-label={revealLabel} style={{
            background: 'none', border: 'none', padding: '1px', cursor: 'pointer',
            color: 'var(--text-muted)', display: 'flex', alignItems: 'center',
            transition: 'color 0.15s',
          }}
            onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--accent)')}
            onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--text-muted)')}
          ><Folder size={11} /></button>
        </>
      ) : (
        <span style={{ color: 'var(--text)', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
          title={filePath}
        >{short}</span>
      )}
    </span>
  )
}

// `ChatPanel.tsx` renders `StreamingMarkdown`; `ChatPanelBubble.tsx` renders
// settled messages through the same chains, overrides and local-image reader.
export { LocalImage, MD_REHYPE, MD_REMARK, mdComponents, StreamingMarkdown }
