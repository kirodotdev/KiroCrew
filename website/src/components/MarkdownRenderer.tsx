import React, { createContext, useContext, memo, useEffect, useMemo, useRef, useId, useCallback, useState } from 'react'
import Clickable from './Clickable'
import { HOVER_NONE_ACTION_BTN_CLS } from '../utils/touchActions'
import { getImageDims, rememberImageDims } from '../utils/imageDims'
import { Paperclip, X, Download, Plus, Minus, Search, Folder, Maximize2, Check } from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'
import ReactMarkdown from 'react-markdown'
import type { Components, ExtraProps } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkCjkFriendly from 'remark-cjk-friendly'
import remarkCjkFriendlyGfmStrikethrough from 'remark-cjk-friendly-gfm-strikethrough'
import remarkMath from 'remark-math'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import rehypeRaw from 'rehype-raw'
import rehypeKatex from 'rehype-katex'
import type { PluggableList } from 'unified'
import type { Root as HastRoot, RootContent, Element as HastElement, Text as HastText } from 'hast'

/** A hast node that owns a `children` array — either the document root or an
 *  element. Both accept `Element`/`Text` children, so our inserted glow/reveal
 *  spans are valid in either. */
type HastParent = HastRoot | HastElement

/** Splice replacement `<span>`/text nodes into a parent's children, replacing
 *  the single node at `index`. Root and Element have differently-typed children
 *  arrays (`RootContent[]` vs `ElementContent[]`) that both admit Element/Text,
 *  so this narrows on the parent kind to keep the splice type-safe. */
function spliceChildren(parent: HastParent, index: number, nodes: Array<HastElement | HastText>): void {
  if (parent.type === 'root') parent.children.splice(index, 1, ...nodes)
  else parent.children.splice(index, 1, ...nodes)
}
import '../utils/hljs'
import { api } from '../api/client'
import { useBlockAssembler, maskInlineCode } from '../hooks/useBlockAssembler'
import { usePathKind, type PathKind } from '../hooks/usePathKind'
import { useGatewayPlatform, type GatewayPlatform } from '../hooks/useGatewayPlatform'
import { fileIcon } from '../utils/fileIcons'
import { urlTransform, ALLOWED_PROTOCOLS, WINDOWS_ABS_PATH_RE, decodeLocalPath } from '../utils/urlTransform'
import { safeHttpUrl } from '../lib/safeUrl'
import { useLinkMeta, type LinkMeta } from '../lib/linkMeta'
import { LinkChip, LinkCard } from './LinkPreview'
import { parseSourceLinkUrl, forgeChipLabel, type PullRequestLink } from '../utils/pullRequestLinks'
import { JiraHostsCtx } from '../lib/jiraHosts'
import JiraLogo from './icons/JiraLogo'
import GithubLogo from './icons/GithubLogo'
import GitlabLogo from './icons/GitlabLogo'
import DiffBlock from './DiffBlock'
import EditableCodeBlock from './EditableCodeBlock'
import { SmoothResize } from './SmoothResize'
import type { ContentBlock } from '../types'

/** Extract the artifact slug from an `/artifacts/<slug>` href. Returns null
 *  when the href isn't an artifact route. Handles a leading origin, a trailing
 *  query/hash, and percent-encoded slugs (the agent emits an encoded slug
 *  matching the canonical full-page artifact URL). */
export function artifactSlugFromHref(href: string | null | undefined): string | null {
  if (!href) return null
  // Strip an optional origin so both relative (`/artifacts/x`) and absolute
  // (`http://host/artifacts/x`) forms resolve identically.
  let path = href
  try { path = new URL(href, 'http://x').pathname } catch { /* keep raw */ }
  const m = /^\/artifacts\/([^/?#]+)/.exec(path)
  if (!m) return null
  try { return decodeURIComponent(m[1]) } catch { return m[1] }
}

/**
 * Character-level shape of a local filesystem path: word chars, dot, dash, @,
 * ~, colon and space, separated by slashes. Anchored at both ends, so anything
 * carrying a URL scheme (`https://…`) or shell punctuation fails outright.
 *
 * Shape alone is NOT sufficient to linkify — see `isPathCandidate`.
 */
const PATH_SHAPE_RE = /^~?(?:\.{0,2}\/)?[\w.@~/ -]*\/[\w.@~: -]*[\w.]$/

/** A trailing `.ext` on the last segment, 1-8 chars — the only positive path
 *  signal available to a path that is neither rooted nor explicitly relative. */
const EXT_RE = /\.[A-Za-z0-9]{1,8}$/

/**
 * Could this inline-code text denote a local filesystem path?
 *
 * Deliberately a PRE-FILTER, not a decision. "Is `refs/heads/fix/foo` a path?"
 * is not a syntactic question — it is a filesystem question — so this only
 * decides whether spending a stat probe is worthwhile. The probe
 * (`usePathKind`) makes the actual call.
 *
 * Merely containing a slash is not enough: that matched git refs
 * (`refs/heads/…`, `origin/main`), repo slugs (`owner/repo`), MIME types
 * (`text/plain`), npm scopes (`@scope/pkg`) and dates (`2026/08/02`), every one
 * of which then rendered as a clickable "file" that could only ever 404. So a
 * candidate must carry a positive signal that it names a location:
 *
 *   - rooted (`/x`, `~/x`), or
 *   - explicitly relative (`./x`, `../x`), or
 *   - a file extension on the last segment (`src/main.py`).
 *
 * A bare two-segment identifier with no extension is rejected. Note the third
 * rule still admits `origin/feature/x.ts`; that is intentional — syntax cannot
 * settle it, and the stat probe will.
 */
export function isPathCandidate(s: string): boolean {
  if (!PATH_SHAPE_RE.test(s)) return false
  if (s.startsWith('/') || s.startsWith('~') || s.startsWith('./') || s.startsWith('../')) return true
  return EXT_RE.test(s.slice(s.lastIndexOf('/') + 1))
}

/**
 * A trailing source location: `:447`, or `:447:12` for line-and-column.
 *
 * Capped at 7 digits so a long digit run (a hash fragment, an id) is not read as
 * a line number, and so the captured value always parses to a safe integer.
 */
const LINE_REF_RE = /:(\d{1,7})(?:-(\d{1,7})|:\d{1,7})?$/

/**
 * Split a `file:line` / `file:line:col` reference into its path and line.
 *
 * Agents cite code the way compilers and stack traces do, so the location is
 * part of the token, and treating the whole token as a filename is what made
 * these chips inert: the stat probe asked the backend about
 * `…/_dispatch.py:447`, which does not exist, so the chip rendered as dead
 * text. Splitting first lets the probe ask about the file and the click carry
 * the line.
 *
 * Three shapes are accepted: a single line (`:447`), a line and column
 * (`:447:12`), and a RANGE (`:10-16`). The column is matched so it can be
 * consumed but is discarded — the reveal is line-granular, and pretending to a
 * column we then ignore would be a worse contract than not offering one. A
 * range, by contrast, IS honoured: the whole span is revealed and highlighted.
 *
 * Purely syntactic and therefore ambiguous: a file whose name genuinely ends in
 * `:12` splits into a path that does not exist. Callers resolve that by probing
 * the split path first and falling back to the unsplit text (see `InlineCode`),
 * rather than by guessing here.
 */
export function splitLineRef(s: string): { path: string; line?: number; endLine?: number } {
  const m = LINE_REF_RE.exec(s)
  if (!m) return { path: s }
  const line = Number(m[1])
  // `:0` is not a line — every editor numbers from 1 — so treat it as
  // part of the name rather than clamping it to 1 and jumping somewhere the
  // text never named.
  if (!line) return { path: s }
  const path = s.slice(0, m.index)
  const end = m[2] ? Number(m[2]) : undefined
  // A reversed or degenerate range (`:16-10`, `:10-0`, `:10-10`) carries no more
  // information than its start, so it collapses to a single line rather than
  // being silently swapped — guessing which end the author meant would be worse
  // than honouring the number they put first.
  if (end == null || end <= line) return { path, line }
  return { path, line, endLine: end }
}

/** Context providing the viewed file's directory path for resolving bare relative image paths. */
export const BasePathCtx = createContext<string | null>(null)

/**
 * When true, markdown images render as small previews (a compact thumbnail the
 * user can still click to open the full-size lightbox) instead of the default
 * large inline size. User-message ("sent prompt") rendering turns this on so
 * an attached screenshot doesn't dominate the bubble, while assistant/response
 * images keep the full inline size. Default false = full size.
 */
export const CompactImagesCtx = createContext<boolean>(false)

/**
 * A per-message token appended to local image URLs.
 *
 * `/api/file-raw?path=…` addresses a file by PATH, so every impression of a file
 * an agent rewrites across turns resolves to one URL — and a browser treats one
 * URL in one document as one resource. The second `<img>` is then served from the
 * in-document memory cache with no network request at all, so the new message
 * paints the OLD bytes. Measured in Chrome: without a distinct URL the edited
 * file is never re-fetched, and no HTTP cache header changes that — `ETag`,
 * `Cache-Control: no-cache` and even `no-store` are not consulted, because the
 * request is never made.
 *
 * Making the URL per-message gives each impression its own cache entry, so a new
 * message shows the current bytes while an earlier one keeps what it fetched.
 * Stable within a message, so re-renders and streaming do not re-request.
 */
export const ImageVersionCtx = createContext<string | null>(null)

/** The exact markdown string handed to ReactMarkdown, so components can map a
 *  node's source position back to the original text. ImgWithFallback uses it
 *  to see whether an image destination was `<…>`-wrapped — micromark strips
 *  the wrap and percent-encodes BOTH forms identically, so the parsed url
 *  alone cannot distinguish producer-encoded content from a legacy raw path
 *  that happens to contain `%XX` (which must be preserved verbatim). */
export const MdSourceCtx = createContext<string | null>(null)

/**
 * Per-consumer override for rendered markdown LINKS.
 *
 * A provider returns its own element for the hrefs it wants to own, or null to
 * fall through to the default anchor. Issue Radar uses it to render same-repo
 * issue/PR references as in-app affordances (dashed accent underline + hover
 * preview) without this module knowing anything about issues — and without any
 * consumer having to post-process React-owned DOM.
 *
 * Only the anchor is delegated; the surrounding markdown pipeline is untouched.
 */
export type LinkOverride = (link: { href: string; children: React.ReactNode }) => React.ReactNode | null
export const LinkOverrideCtx = createContext<LinkOverride | null>(null)

/**
 * Link-unfurl gate for the markdown subtree.
 *
 * `enabled` mirrors `cfg.dashboard.link_previews` (default OFF): the user has to
 * opt in before this machine will fetch a URL the model wrote.
 *
 * `live` means the block is STILL STREAMING. It is a hard, independent gate: a
 * URL in the streaming tail may be half-typed (`https://exa`), and resolving
 * that would send the model's in-progress text to a host nobody named. Nothing
 * is fetched while `live` is true — the chip/card simply appears once the block
 * settles.
 *
 * Both default to false, so any markdown rendered outside a provider (file
 * previews, artifact pages, app-embedded chat) keeps today's plain anchors.
 */
export interface LinkUnfurl {
  enabled: boolean
  live: boolean
}
export const LinkUnfurlCtx = createContext<LinkUnfurl>({ enabled: false, live: false })

/**
 * The href to unfurl, or null when the link must stay a plain anchor.
 *
 * Three exclusions, all deliberate:
 *  - non-http(s) (and Basic-auth userinfo) — `safeHttpUrl`. `artifact:`,
 *    `vscode:`, `mailto:`, `javascript:` and relative paths all fail here, so
 *    only an absolute web URL can ever reach the backend.
 *  - `/artifacts/<slug>` — an in-app artifact route, handled by the click
 *    interception below; unfurling it would fetch our own dashboard.
 *  - anything else same-origin — likewise an in-app dashboard route. There is no
 *    page title to show that the UI doesn't already know.
 */
export function unfurlableHref(href: string | null | undefined): string | null {
  if (!href || !safeHttpUrl(href)) return null
  if (artifactSlugFromHref(href)) return null
  try {
    if (new URL(href).origin === window.location.origin) return null
  } catch {
    return null
  }
  return href
}

/** Resolve the unfurl target for an href under the current gate. A hook (reads
 *  context), so it is called unconditionally by both link components. */
function useUnfurlHref(href: string | null | undefined): string | null {
  const { enabled, live } = useContext(LinkUnfurlCtx)
  if (!enabled || live) return null
  return unfurlableHref(href)
}

/**
 * The single `<a>` that is a paragraph's ONLY element child, or null.
 *
 * Whitespace-only text siblings are ignored (remark leaves a trailing newline
 * text node on `<p><a>…</a></p>`), but any real text, or a second element,
 * disqualifies the paragraph — that link is inline prose and gets a chip.
 * `text` is the anchor's own visible text, used only as the probe argument for a
 * `LinkOverrideCtx` provider.
 */
export function soleLinkInParagraph(node?: HastElement): { href: string; text: string } | null {
  if (!node?.children) return null
  let anchor: HastElement | null = null
  for (const child of node.children) {
    if (child.type === 'text') {
      if (child.value.trim()) return null
      continue
    }
    if (child.type !== 'element' || anchor || child.tagName !== 'a') return null
    anchor = child
  }
  const href = anchor?.properties?.href
  if (!anchor || typeof href !== 'string') return null
  const text = anchor.children
    .map((c) => (c.type === 'text' ? c.value : ''))
    .join('')
  return { href, text }
}

function isDarkTheme(): boolean {
  return (document.documentElement.getAttribute('data-theme') || '').includes('dark')
}

/**
 * mermaid, loaded on first use.
 *
 * mermaid plus its eager dependencies are ~90-130 KB gzip, and this module is
 * on the critical path (every chat message renders through it) while a
 * ```mermaid fence is rare. A static import therefore put the whole diagram
 * engine in the entry chunk for every user. `MermaidBlock` already renders
 * asynchronously inside an effect, so deferring the module costs nothing.
 *
 * The promise is cached at module scope so N diagram blocks share one load, and
 * `import()` itself is idempotent regardless.
 */
type MermaidApi = typeof import('mermaid')['default']

let mermaidLoad: Promise<MermaidApi> | null = null

function loadMermaid(): Promise<MermaidApi> {
  if (!mermaidLoad) mermaidLoad = import('mermaid').then(m => m.default)
  return mermaidLoad
}

function initMermaid(mermaid: MermaidApi): void {
  const dark = isDarkTheme()
  mermaid.initialize({
    startOnLoad: false,
    theme: dark ? 'dark' : 'default',
    themeVariables: dark ? {
      primaryColor: '#f59e32',
      primaryTextColor: '#e8e6e3',
      primaryBorderColor: '#3a3a3a',
      lineColor: '#888',
      secondaryColor: '#2a2a2a',
      tertiaryColor: '#1a1a1a',
    } : {
      primaryColor: '#f59e32',
      primaryTextColor: '#1a1a1a',
      primaryBorderColor: '#ccc',
      lineColor: '#666',
      secondaryColor: '#fff3e0',
      tertiaryColor: '#f5f5f5',
    },
    securityLevel: 'strict',
    fontFamily: 'inherit',
    // Throw on parse errors instead of injecting mermaid's error diagram into
    // a temp <div id="dmermaid-*"> on document.body. That temp node is leaked
    // when render() throws (cleanup only runs on success), so failed blocks
    // accumulated orphaned 512px error SVGs in the DOM. With this on, the
    // MermaidBlock .catch() shows a clean inline <pre> and nothing leaks.
    suppressErrorRendering: true,
  })
}

import { CodeBlock } from './CodeBlock'
import { ExcalidrawBlock } from './ExcalidrawBlock'
import DiagramLightbox from './DiagramLightbox'

/** Forward the `data-sourcepos` attribute from rehypeSourcepos onto the
 *  rendered element. Used in every MD_COMPONENTS override; returns an
 *  empty-valued attribute when sourcePos is disabled (React omits it from
 *  the DOM). */
const sp = (node?: HastElement) => {
  const v = node?.properties?.['data-sourcepos']
  return { 'data-sourcepos': typeof v === 'string' ? v : undefined }
}

const MermaidBlock = memo(function MermaidBlock({ code }: { code: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const id = useId().replace(/:/g, '_')
  const renderedRef = useRef('')
  // Rendered SVG markup, kept for the enlarge viewer. Empty until a successful
  // render and reset on failure, so the enlarge affordance only ever exists
  // for (and targets) the diagram currently on screen.
  const [svg, setSvg] = useState('')
  const [enlarged, setEnlarged] = useState(false)

  useEffect(() => {
    if (!ref.current || renderedRef.current === code) return
    renderedRef.current = code
    loadMermaid().then(mermaid => {
      // Re-initialized per render so a theme switch between two diagrams is
      // picked up; initialize() is cheap and idempotent.
      initMermaid(mermaid)
      return mermaid.render(`mermaid-${id}`, code)
    }).then(({ svg }) => {
      if (!ref.current) return
      const range = document.createRange()
      range.selectNodeContents(ref.current)
      range.deleteContents()
      ref.current.appendChild(range.createContextualFragment(svg))
      setSvg(svg)
    }).catch(() => {
      if (!ref.current) return
      const pre = document.createElement('pre')
      pre.className = 'text-danger text-[13px]'
      pre.textContent = code
      ref.current.textContent = ''
      ref.current.appendChild(pre)
      setSvg('')
      setEnlarged(false)
    })
  }, [code, id])

  return (
    <div className="relative group my-3">
      {/* Pointer convenience: clicking the rendered diagram opens the viewer.
          Keyboard and AT users reach the same viewer through the real button
          below — the same pairing the image lightbox uses (clickable <img>,
          focusable controls elsewhere). */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/click-events-have-key-events */}
      <figure
        className={`m-0 ${svg ? 'cursor-zoom-in' : ''}`}
        onClick={svg ? () => setEnlarged(true) : undefined}
      >
        <div ref={ref} className="flex justify-center overflow-x-auto min-h-[60px]" />
      </figure>
      {svg && (
        <button
          aria-label={i18nT('components.diagramLightbox.enlarge_diagram')}
          title={i18nT('components.diagramLightbox.enlarge_diagram')}
          className={`absolute top-1.5 right-1.5 p-1.5 rounded-md bg-bg-elevated/90 border border-border text-muted hover:text-text opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 transition-opacity cursor-pointer ${HOVER_NONE_ACTION_BTN_CLS}`}
          onClick={() => setEnlarged(true)}
        >
          <Maximize2 className="lucide-inline" aria-hidden="true" />
        </button>
      )}
      {enlarged && svg && <DiagramLightbox svg={svg} onClose={() => setEnlarged(false)} />}
    </div>
  )
})

/** Generate a URL-safe slug from heading children (handles nested elements) */
function textOf(node: React.ReactNode): string {
  if (typeof node === 'string') return node
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isElementWithProps(node)) {
    const props = node.props
    if (typeof props.alt === 'string') return props.alt
    if (props.children != null) return textOf(props.children)
  }
  return ''
}
/** Narrow a ReactNode to a ReactElement whose props may carry `alt`/`children`. */
function isElementWithProps(
  node: React.ReactNode,
): node is React.ReactElement<{ alt?: string; children?: React.ReactNode }> {
  return typeof node === 'object' && node !== null && 'props' in node
}
function slugify(children: React.ReactNode): string | undefined {
  const raw = textOf(children).toLowerCase().replace(/[^\w\s-]/g, '').replace(/\s+/g, '-').replace(/^-+|-+$/g, '')
  return raw || undefined
}

/** Default markdown anchor, unless a `LinkOverrideCtx` provider claims the href.
 *
 * Extracted from the inline `MD_COMPONENTS.a` so it can read context (it is a
 * component, so hooks are legal here). Only ALLOWED_PROTOCOLS links (editor
 * schemes) keep in-place navigation; everything else opens in a new tab. */
function MdAnchor({ node, href, children }: React.AnchorHTMLAttributes<HTMLAnchorElement> & ExtraProps) {
  const override = useContext(LinkOverrideCtx)
  const probeEnabled = useContext(PathProbeCtx)
  const actions = useContext(PathActionCtx)
  // The override is resolved FIRST and wins outright — Issue Radar's in-app
  // issue/PR affordance must keep beating a link preview. Feeding `null` into
  // the unfurl gate for a claimed href also means a claimed link is never
  // fetched, so the priority holds at the network boundary, not just visually.
  const claimed = href && override ? override({ href, children }) : null
  // Jira, GitHub, and GitLab issue / PR / MR URLs chip synchronously from the
  // URL alone (provider mark + reference) — no fetch, unlike the unfurl chip
  // below, so these chips render in user messages and with `link_previews`
  // off. Jira instances sit behind auth, so an unfurl of one can never
  // succeed; GitHub/GitLab pages unfurl fine but only in assistant messages
  // and only when the operator opted in, which left forge links as raw text
  // in most contexts (#2579). The parser matches hostnames EXACTLY
  // (`github.com` / `gitlab.com`, `www.` stripped) — a lookalike host such as
  // `evil-github.com.attacker.test` falls through to the plain anchor.
  // Self-hosted Jira instances come through `JiraHostsCtx` from the operator
  // allowlist. Forge chips additionally require `safeHttpUrl`: the chip keeps
  // the AUTHORED href (preserving e.g. `#issuecomment` fragments the parser's
  // canonical url drops), so a credential-smuggling `user:pass@github.com`
  // href must never be dressed up as a trusted-looking chip.
  const jiraHosts = useContext(JiraHostsCtx)
  const source = useMemo(() => {
    if (!href || claimed) return null
    const link = parseSourceLinkUrl(href, [], jiraHosts)
    if (!link) return null
    if (link.provider === 'jira') return link
    return safeHttpUrl(href) ? link : null
  }, [href, claimed, jiraHosts])
  // A chipped link is never handed to the unfurl gate — mirroring `claimed`,
  // so the no-fetch guarantee holds at the network boundary, not just visually.
  const target = useUnfurlHref(claimed || source ? null : href)
  const meta = useLinkMeta(target ?? undefined, target !== null)
  let localHref: string | null = null
  if (href?.startsWith('/')) {
    try {
      const decodedHref = decodeURIComponent(href)
      if (!decodedHref.startsWith('//')) localHref = decodedHref
    } catch { /* keep it a normal link */ }
  }
  const pathResolution = usePathResolution(
    localHref ?? '',
    probeEnabled
      && !claimed
      && !!localHref
      && !artifactSlugFromHref(localHref)
      && !!(actions.onFileOpen || actions.onFolderOpen),
  )
  const onPathClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    const plainPrimaryClick = e.button === 0 && !e.metaKey && !e.ctrlKey && !e.altKey
    if (pathResolution.probePending && plainPrimaryClick) {
      e.preventDefault()
      return
    }
    if (!pathResolution.candidate
      || (pathResolution.kind !== 'file' && pathResolution.kind !== 'dir')
      || !plainPrimaryClick) return
    e.preventDefault()
    activatePath(
      pathResolution.path,
      pathResolution.kind,
      e.shiftKey,
      actions,
      pathResolution.line,
      pathResolution.endLine,
    )
  }
  if (claimed) return <>{claimed}</>
  if (source?.provider === 'jira') {
    const jira = source
    return (
      <span className="group inline-flex max-w-full items-center gap-1 rounded-md border border-border/60 bg-accent/10 px-1.5 py-px align-baseline text-[13px] transition-colors hover:border-border hover:bg-accent/20 focus-within:border-border">
        <a
          href={jira.url}
          target="_blank"
          rel="noopener noreferrer"
          title={href}
          className="inline-flex min-w-0 items-center gap-1.5 text-text no-underline focus-ring"
        >
          <JiraLogo size={12} className="shrink-0" />
          <span className="truncate max-w-[24ch]">{`${jira.repo}-${jira.number}`}</span>
        </a>
      </span>
    )
  }
  const forgeLabel = source ? forgeChipLabel(source) : null
  if (source && forgeLabel) {
    return (
      <span className="group inline-flex max-w-full items-center gap-1 rounded-md border border-border/60 bg-accent/10 px-1.5 py-px align-baseline text-[13px] transition-colors hover:border-border hover:bg-accent/20 focus-within:border-border">
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          title={href}
          className="inline-flex min-w-0 items-center gap-1.5 text-text no-underline focus-ring"
        >
          {source.provider === 'github'
            ? <GithubLogo size={12} className="shrink-0" />
            : <GitlabLogo size={12} className="shrink-0" />}
          <span className="truncate max-w-[32ch]">{forgeLabel}</span>
        </a>
      </span>
    )
  }
  if (target && meta) return <LinkChip meta={meta} href={target}>{children}</LinkChip>
  let ext = false
  try { ext = !!href && ALLOWED_PROTOCOLS.has(new URL(href, 'http://x').protocol) } catch { /* not a URL */ }
  return (
    <a
      {...sp(node)}
      href={href}
      onClick={pathResolution.candidate ? onPathClick : undefined}
      {...(ext ? {} : { target: '_blank', rel: 'noopener noreferrer' })}
      className="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent"
    >
      {children}
    </a>
  )
}

/**
 * Whether inline-code chips may issue stat probes.
 *
 * False while a message streams. Mid-stream a path arrives one chunk at a time,
 * and the prefixes are themselves valid candidates — `/Users` is a real
 * directory on the way to `/Users/me/project/file.ts` — so probing every chunk
 * would burn requests and briefly render the wrong affordance before settling.
 * Chips stay inert until the text stops moving.
 */
const PathProbeCtx = createContext<boolean>(true)

/**
 * Where a confirmed path chip sends its activation.
 *
 * A context because `MD_COMPONENTS` is module-level — the `code` renderer cannot
 * receive MarkdownRenderer's props directly. Both handlers are optional: most of
 * the ~30 MarkdownRenderer call sites pass neither, and those fall back to the
 * OS file manager.
 */
type PathActions = { onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void; onFolderOpen?: (path: string) => void }
const PathActionCtx = createContext<PathActions>({})

type PathResolution = {
  candidate: boolean
  kind: PathKind | undefined
  path: string
  splitPath: string
  line: number | undefined
  endLine: number | undefined
  probePending: boolean
}

/** Resolve both legal readings of a location suffix before exposing an action.
 *
 * A literal filename such as `report.md:12` takes precedence over the inferred
 * `report.md` at line 12, so both Markdown forms use the same probe ordering.
 */
function usePathResolution(raw: string, probeEnabled: boolean): PathResolution {
  const { path: splitPath, line, endLine } = splitLineRef(raw)
  const candidate = probeEnabled && isPathCandidate(splitPath)
  const literalCandidate = candidate && line != null
  const splitKind = usePathKind(candidate ? splitPath : null)
  const literalKind = usePathKind(literalCandidate ? raw : null)
  const literalWins = literalKind === 'file' || literalKind === 'dir'

  return {
    candidate,
    kind: literalWins ? literalKind : splitKind,
    path: literalWins ? raw : splitPath,
    splitPath,
    line: literalWins ? undefined : line,
    endLine: literalWins ? undefined : endLine,
    probePending: (candidate && splitKind === undefined)
      || (literalCandidate && literalKind === undefined),
  }
}

/**
 * Act on a confirmed path chip.
 *
 * `reveal` is the shift-modifier / no-handler escape hatch: hand the path to the
 * OS file manager, which understands both files and directories.
 *
 * `line` (from a `file:447` chip) is passed to the file handler so it can scroll
 * to and flash that line. It is dropped on the two fallback routes on purpose:
 * `revealPath` selects a file in Finder/Explorer, which has no notion of a line,
 * and a directory does not have one either.
 */
function activatePath(path: string, kind: PathKind, reveal: boolean, actions: PathActions, line?: number, endLine?: number): void {
  if (reveal) { api.revealPath(path); return }
  if (kind === 'dir') {
    // No folder handler wired: fall back to the OS file manager rather than
    // silently doing nothing.
    if (actions.onFolderOpen) actions.onFolderOpen(path)
    else api.revealPath(path)
    return
  }
  if (!actions.onFileOpen) { api.revealPath(path); return }
  // Called with ONE argument when there is no line, not with an explicit
  // `undefined`: the handler is also the app's general-purpose file opener, and
  // an omitted argument keeps a chip click indistinguishable from every other
  // caller of it.
  if (line != null) actions.onFileOpen(path, endLine != null ? { line, endLine } : { line })
  else actions.onFileOpen(path)
}

const CHIP_BASE = 'bg-bg-elevated px-1.5 py-0.5 rounded text-accent text-sm font-mono'

/**
 * The chip's hover instruction, naming the application shift+click will actually
 * open.
 *
 * `api.revealPath` runs on the GATEWAY, so the host to name is that one — a
 * dashboard opened from a Mac against a Linux gateway must not promise Finder.
 * Anything we could not read (the `'gateway'` sentinel a non-owner gets, a failed
 * probe, Linux with no single file manager) takes the generic wording.
 *
 * Six whole sentences rather than one sentence with the label interpolated in:
 * the app name sits in a different case and position per language ("im
 * Dateimanager", "dans le gestionnaire de fichiers", "ファイルマネージャーに表示"),
 * which a placeholder cannot carry.
 */
function revealHintFor(isDir: boolean, platform: GatewayPlatform): string {
  if (isDir) {
    if (platform === 'darwin') return i18nT('components.markdownRenderer.click_to_browse_shift_click_to_reveal_in_finder')
    if (platform === 'windows') return i18nT('components.markdownRenderer.click_to_browse_shift_click_to_open_in_file_explorer')
    return i18nT('components.markdownRenderer.click_to_browse_shift_click_to_show_in_file_manager')
  }
  if (platform === 'darwin') return i18nT('components.markdownRenderer.click_to_open_shift_click_to_reveal_in_finder')
  if (platform === 'windows') return i18nT('components.markdownRenderer.click_to_open_shift_click_to_open_in_file_explorer')
  return i18nT('components.markdownRenderer.click_to_open_shift_click_to_show_in_file_manager')
}

/** Click-to-copy inline code chip for non-path spans (commands, env vars, IDs).
 *  Uses a brief "copied" feedback state and stays a plain inline `<code>` to
 *  preserve line-wrapping. The copied state shows a small check icon inline;
 *  the icon is `pointer-events-none` and purely decorative so it cannot steal
 *  the click or affect layout reflow. */
function CopyableCode({ className, safeProps, text, children }: {
  className: string
  safeProps: Record<string, unknown>
  text: string
  children: React.ReactNode
}) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])
  const handleCopy = (e: React.MouseEvent | React.KeyboardEvent) => {
    e.preventDefault()
    e.stopPropagation()
    copyToClipboard(text.trim())
    setCopied(true)
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setCopied(false), 1500)
  }
  return (
    <code
      className={`${className} cursor-pointer hover:underline`}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-element-to-interactive-role -- <code> is intentionally interactive (click-to-copy)
      role="button"
      tabIndex={0}
      onClick={handleCopy}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') handleCopy(e) }}
      title={copied
        ? i18nT('components.markdownRenderer.copied')
        : i18nT('components.markdownRenderer.click_to_copy')}
      {...safeProps}
    >
      {children}
      {copied && <Check size={12} aria-hidden="true" className="inline align-middle ml-0.5 opacity-70 pointer-events-none text-ok" />}
    </code>
  )
}

/**
 * Inline `code` span, upgraded to a click-to-open chip only once the backend has
 * confirmed the text names something that exists.
 *
 * The old behaviour linkified on regex match alone, which produced two bad
 * outcomes: a directory opened the file viewer and rendered "file not found"
 * (wrong — it exists), and non-paths that merely contain a slash (git refs,
 * repo slugs) became dead links. So the default is inverted here: plain text
 * unless proven otherwise.
 *
 * Binds its OWN click/key handlers rather than relying on delegation from the
 * container. That is what makes the affordance honest: the chip is the control
 * (`role="button"`, focusable, Enter/Space), the wrapper stays presentational,
 * and a `<code>` that arrives from raw HTML gets no handler at all — so a forged
 * chip cannot borrow the container's.
 */
function InlineCode({ children, ...props }: { children?: React.ReactNode } & Record<string, unknown>) {
  const codeStr = String(children).replace(/\n$/, '')
  const probeEnabled = useContext(PathProbeCtx)
  const actions = useContext(PathActionCtx)
  const gatewayPlatform = useGatewayPlatform()
  const raw = codeStr.trim()
  const pathResolution = usePathResolution(raw, probeEnabled)

  // `data-path` / `data-path-kind` describe a chip THIS component rendered, so
  // only it may set them. rehypeSanitize allowlists every `data-*` attribute
  // (isAllowedAttr: `k.startsWith('data')`), so raw HTML arrives here with a
  // forged pair intact; spreading it would publish attributes claiming a
  // backend-confirmed path that was never probed. Drop any inbound copy.
  const safeProps = Object.fromEntries(
    Object.entries(props).filter(([k]) => !k.toLowerCase().startsWith('data-path')),
  )

  if (pathResolution.probePending
    || (pathResolution.kind !== 'file' && pathResolution.kind !== 'dir')) {
    return <CopyableCode className={CHIP_BASE} safeProps={safeProps} text={codeStr}>{children}</CopyableCode>
  }
  const isDir = pathResolution.kind === 'dir'
  const { path, splitPath, kind, line: targetLine, endLine: targetEndLine } = pathResolution
  const revealHint = revealHintFor(isDir, gatewayPlatform)
  // A leading glyph is what makes "this is actionable" legible at rest. Without
  // one, a confirmed chip and an inert one differ only on hover, so a reader
  // cannot tell which paths the backend actually resolved. Files use the same
  // per-extension icon set as the Files tab and the folder browser, so a .md and
  // a .json chip are distinguishable — but rendered monochrome at the folder
  // glyph's weight, because inline in prose this is an affordance marker, not
  // decoration. Decorative either way: the path text carries the meaning.
  //
  // The glyph is an INLINE atom and the chip stays a plain inline box. Making the
  // chip `inline-flex` to align the glyph turned it atomic, so a long path could
  // no longer break across lines and overflowed its container instead — the
  // render gate caught this as layout/unbreakable-token on the artifacts surface.
  const Glyph = isDir ? Folder : fileIcon(path)
  /** stopPropagation keeps the container's artifact-link delegation from also
   *  firing for a click that this chip has already handled. */
  const act = (e: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean; preventDefault: () => void; stopPropagation: () => void }) => {
    e.preventDefault()
    e.stopPropagation()
    // Ctrl/Cmd+Click copies the path text rather than opening/revealing.
    if (e.ctrlKey || e.metaKey) { copyToClipboard(raw); return }
    activatePath(path, kind, e.shiftKey, actions, targetLine, targetEndLine)
  }
  return (
    <code
      className={`${CHIP_BASE} cursor-pointer hover:underline`}
      role="button"
      tabIndex={0}
      onClick={act}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') act(e) }}
      {...safeProps}
      data-path={path}
      data-path-kind={kind}
      data-path-line={targetLine}
      data-path-end-line={targetEndLine}
      // The resolved path leads the tooltip, not just the instruction. A native
      // tooltip paints in the browser's own layer, above page content, and any
      // element overlaying the chip must be pointer-events-none to let the click
      // reach it — so hovering always discloses the real target even when
      // surrounding markup visually covers the chip's text. It also shows a long
      // path in full when layout truncates it.
      //
      // `raw`, not `path`, so a `file:447` chip discloses the line it will jump
      // to. That keeps the disclosure honest without a second catalog string:
      // the location is already in the text the user is hovering.
      title={`${raw}\n${revealHint}\n${i18nT('components.markdownRenderer.ctrl_click_to_copy')}`}
    >
      <Glyph size={12} aria-hidden="true" className="inline align-middle mr-1 opacity-70" />
      {targetLine != null && raw.length > splitPath.length
        // Keep the location suffix atomic. A range is the case that actually
        // misleads: broken across lines, `…2026.md:10-` / `16` reads as a citation
        // ending at line 10 until the eye reaches the next line. The path itself
        // stays breakable, since that is what lets a long citation wrap at all.
        ? <>{splitPath}<span className="whitespace-nowrap">{raw.slice(splitPath.length)}</span></>
        : children}
    </code>
  )
}

/**
 * Default markdown paragraph — except when the paragraph IS a single link, in
 * which case the resolved link renders as a block card instead.
 *
 * Position is the whole selection rule: a link surrounded by prose is a chip
 * (see `MdAnchor`), a link standing alone is a card. `LinkCard` replaces the
 * `<p>` rather than nesting inside it, so the card is a block-level sibling of
 * the surrounding paragraphs.
 *
 * Jira issue URLs take a synchronous branch of the same rule, mirroring
 * `MdAnchor`'s chip: Jira instances sit behind auth, so the unfurl fetch can
 * never be relied on to produce a preview for them. The card is built from the
 * URL alone (provider mark, issue key, instance host) with NO request, and
 * recognition is the same allowlist-gated parse as the chip (`JiraHostsCtx`).
 * It obeys the same `enabled`/`live` gate as the fetched card, so ungated
 * surfaces (file previews, artifact pages, sourcePos mode) and streaming tails
 * keep today's inline chip.
 */
function MdParagraph({ node, children }: React.HTMLAttributes<HTMLParagraphElement> & ExtraProps) {
  const override = useContext(LinkOverrideCtx)
  const { enabled: cardsOn, live } = useContext(LinkUnfurlCtx)
  const jiraHosts = useContext(JiraHostsCtx)
  const sole = soleLinkInParagraph(node)
  const jira = useMemo(() => {
    if (!sole?.href || !cardsOn || live) return null
    const link = parseSourceLinkUrl(sole.href, [], jiraHosts)
    return link?.provider === 'jira' ? link : null
  }, [sole?.href, cardsOn, live, jiraHosts])
  // A recognized Jira link never reaches the unfurl machinery: its card is
  // synchronous, so handing the href on would only add a fetch whose result
  // is discarded.
  const target = useUnfurlHref(jira ? null : sole?.href)
  // Same priority rule as MdAnchor: a link the override owns stays an in-app
  // affordance inside an ordinary paragraph, never a card. The provider is a
  // pure render prop (Issue Radar's returns a RefLink element), and the probe
  // only runs when a card is otherwise on the table.
  const cardHref = jira ? sole?.href ?? null : target
  const claimed = !!(cardHref && override && override({ href: cardHref, children: sole?.text }))
  const unfurl = claimed ? null : target
  const meta = useLinkMeta(unfurl ?? undefined, unfurl !== null)
  if (jira && !claimed) {
    // `jira.url` (the parser's canonical form), NEVER `sole.href`: this branch
    // sits before the `safeHttpUrl()` rejection the unfurl path gets, so the
    // raw href could still carry Basic-auth userinfo. The canonical URL is
    // rebuilt from hostname+port alone — credentials cannot survive into it —
    // and it is the same target the inline chip's anchor already uses.
    return (
      <LinkCard
        meta={jiraCardMeta(jira)}
        href={jira.url}
        icon={<JiraLogo size={18} className="shrink-0" />}
      />
    )
  }
  if (unfurl && meta) return <LinkCard meta={meta} href={unfurl} />
  return <p {...sp(node)} className="my-1 leading-6">{children}</p>
}

/**
 * Synthetic `LinkMeta` for the Jira card, from the parsed URL alone: the issue
 * key is the title and the instance host is the domain — the same information
 * the inline chip carries, in card layout. No description on purpose: main has
 * no Jira issue fetch, and inventing one here would put this card behind auth.
 */
function jiraCardMeta(link: PullRequestLink): LinkMeta {
  let domain = ''
  try { domain = new URL(link.url).host } catch { /* unreachable: link.url came out of the parser */ }
  return {
    url: link.url,
    title: `${link.repo}-${link.number}`,
    description: '',
    siteName: '',
    domain,
    icon: '',
    iconDark: '',
    fetchedAt: 0,
  }
}

const MD_COMPONENTS: Components = {
  code({ className, children, ...props }) {
    // Only a <code> inside a <pre> may render a block-level component here
    // (CodeBlock / MermaidBlock / ExcalidrawBlock are each rooted in a <div>).
    // rehypeMarkFencedCode stamps those with `data-fenced`; a bare <code> in
    // prose stays inline whatever class it carries, because a <div> inside the
    // enclosing <p> crashes React's reconciler. That comment carries the full
    // reasoning. `data-fenced` is destructured out so it never reaches the DOM.
    const { 'data-fenced': fenced, ...rest } = props as Record<string, unknown>
    if (fenced === undefined) return <InlineCode {...rest}>{children}</InlineCode>

    const match = /language-(\w+)/.exec(className || '')
    const lang = match?.[1]
    const codeStr = String(children).replace(/\n$/, '')

    if (lang === 'mermaid') return <MermaidBlock code={codeStr} />
    if (lang === 'excalidraw') return <ExcalidrawBlock code={codeStr} />

    return <CodeBlock code={codeStr} lang={lang} complete={true} />
  },
  pre({ children }) { return <>{children}</> },
  table({ node, children }) { return <div className="overflow-x-auto my-3"><table {...sp(node)} className="w-full border-collapse text-sm">{children}</table></div> },
  th({ node, children }) { return <th {...sp(node)} className="text-left text-muted text-[13px] font-medium px-3 py-2 border-b border-border bg-bg-elevated">{children}</th> },
  td({ node, children }) { return <td {...sp(node)} className="px-3 py-2 border-b border-border text-sm">{children}</td> },
  a: MdAnchor,
  blockquote({ node, children }) { return <blockquote {...sp(node)} className="border-l-[3px] border-accent pl-3 my-2 text-muted italic">{children}</blockquote> },
  hr({ node }) { return <hr {...sp(node)} className="border-border my-4" /> },
  h1({ node, children }) { const id = slugify(children); return <h1 {...sp(node)} id={id} className="text-xl font-bold mt-4 mb-2 text-text-strong">{children}</h1> },
  h2({ node, children }) { const id = slugify(children); return <h2 {...sp(node)} id={id} className="text-lg font-bold mt-3 mb-2 text-text-strong">{children}</h2> },
  h3({ node, children }) { const id = slugify(children); return <h3 {...sp(node)} id={id} className="text-base font-semibold mt-3 mb-1.5 text-text-strong">{children}</h3> },
  h4({ node, children }) { const id = slugify(children); return <h4 {...sp(node)} id={id} className="text-sm font-semibold mt-2 mb-1 text-text-strong">{children}</h4> },
  h5({ node, children }) { const id = slugify(children); return <h5 {...sp(node)} id={id} className="text-sm font-medium mt-2 mb-1 text-text-strong">{children}</h5> },
  h6({ node, children }) { const id = slugify(children); return <h6 {...sp(node)} id={id} className="text-[13px] font-medium mt-2 mb-1 text-muted">{children}</h6> },
  ul({ node, children, className }) { const isTasks = className?.includes('contains-task-list'); return <ul {...sp(node)} className={isTasks ? 'list-none pl-4 my-2 space-y-1' : 'list-disc pl-8 my-2 space-y-1 marker:text-muted'}>{children}</ul> },
  ol({ node, children, className }) { const isTasks = className?.includes('contains-task-list'); return <ol {...sp(node)} className={isTasks ? 'list-none pl-4 my-2 space-y-1' : 'list-decimal pl-8 my-2 space-y-1 marker:text-muted'}>{children}</ol> },
  li({ node, children, className }) {
    const isTask = className?.includes('task-list-item')
    if (!isTask) return <li {...sp(node)} className="text-sm leading-relaxed">{children}</li>
    // Task items use block flow, NOT flex. The previous `flex items-start` row
    // broke two ways: (1) an item containing a NESTED list (tasks.md shape)
    // laid the child <ul> out BESIDE the text; (2) any item long enough to
    // wrap turned each inline chunk (text node / code chip) into a separate
    // flex item, so text wrapped inside one chunk while siblings floated next
    // to it — and flex min-width:auto blocked wrapping entirely, forcing
    // horizontal scroll. Block flow + hanging indent (pl/-indent pair) keeps
    // the checkbox aligned with the first line and wrapped lines under the
    // text; nested lists reset the indent and drop below.
    //
    // `text-indent` is inherited, so a LOOSE task list (blank line between
    // items) needs care: remark-rehype wraps each item's content in <p> and
    // puts the checkbox inside the FIRST <p>. The first <p> should keep the
    // hanging indent, but every subsequent <p>/block would otherwise inherit
    // the -1.25rem and jut left into the checkbox gutter — hence the
    // `[&>p:not(:first-child)]:indent-0` reset. The checkbox margin/alignment
    // uses a descendant combinator (`[&_input…]`) rather than direct-child so
    // it also lands on the loose-mode checkbox nested inside that first <p>.
    return (
      <li
        {...sp(node)}
        className="text-sm leading-relaxed break-words pl-5 -indent-5 [&_input[type=checkbox]]:mr-1.5 [&_input[type=checkbox]]:align-middle [&>ul]:indent-0 [&>ol]:indent-0 [&>p:not(:first-child)]:indent-0 [&>ul]:mt-1 [&>ol]:mt-1"
      >
        {children}
      </li>
    )
  },
  p: MdParagraph,
  strong({ node, children }) { return <strong {...sp(node)} className="font-semibold text-text-strong">{children}</strong> },
  em({ node, children }) { return <em {...sp(node)} className="italic">{children}</em> },
  img: ImgWithFallback,
}

/** Markdown image with a React-rendered Paperclip fallback when the URL is
 *  broken. The fallback is React-rendered rather than a hand-built SVG swapped
 *  in via .replaceWith(), so it never mutates DOM React owns — which could
 *  otherwise trigger "removeChild on Node" reconciliation crashes. */
/** Style reserving a not-yet-loaded transcript image's EXACT display box.
 *
 * The loaded layout follows the replaced-element min/max rules, which
 * BACK-PROPAGATE a max-height cap into the width (a tall screenshot capped at
 * 60vh also narrows). Neither width/height attributes nor a bare aspect-ratio
 * reproduce that transfer — with either, max-height clamps the box's height
 * while the width stays at max-width, leaving the image letterboxed centered
 * inside a full-width border band. So spell the native resolution out:
 * width = min(natural, heightCap × ratio), the class's max-width still capping
 * on top; aspect-ratio derives the height. Same expression the loaded image
 * resolves to, so the reserve is invisible — same size, same left edge,
 * border hugging the image.
 */
export function reservedImageStyle(dims: { w: number; h: number }): React.CSSProperties {
  // NUMBERS only — the min()/calc()/aspect-ratio arithmetic lives in the
  // `.mc-img-reserve` rule (index.css), which is where a CSS value belongs and
  // keeps this component free of CSS-shaped string literals.
  return { '--mc-img-w': dims.w, '--mc-img-h': dims.h } as React.CSSProperties
}

/** Class pair applying `reservedImageStyle`'s custom properties: the shared
 *  reserve arithmetic plus the mode's height cap (see index.css). */
export function reservedImageClass(compact: boolean): string {
  return compact ? 'mc-img-reserve mc-img-reserve-compact' : 'mc-img-reserve'
}

function ImgWithFallback({
  node,
  src,
  alt,
  ...props
}: React.ImgHTMLAttributes<HTMLImageElement> & ExtraProps) {
  const [errored, setErrored] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const basePath = useContext(BasePathCtx)
  const compact = useContext(CompactImagesCtx)
  const version = useContext(ImageVersionCtx)
  const source = useContext(MdSourceCtx)
  if (!src) return null
  // A Windows drive/UNC path (`C:/…` — urlTransform passes it through for
  // image src) is as local as a POSIX `/…` path and must route to
  // /api/file-raw the same way; it must NOT take the basePath-relative branch
  // below, which is only for genuinely relative paths (issue #3497).
  const isWinAbs = WINDOWS_ABS_PATH_RE.test(src)
  const isLocal = src.startsWith('/') || src.startsWith('~') || src.startsWith('.') || isWinAbs
    || (basePath && !src.startsWith('http'))
  let url: string
  if (isLocal) {
    // micromark percent-encodes destinations in BOTH forms, so wrap-ness is
    // recovered from the source text at this node's position: only a
    // `<…>`-wrapped destination is producer-emitted (mdImageDest) and safe to
    // decode back to the on-disk path. An unwrapped one is legacy content —
    // a file literally named `photo%20copy.png` must stay verbatim, exactly
    // as it resolved before destinations were ever encoded. decodeLocalPath
    // keeps the raw form on malformed sequences and on decoded control
    // characters (a `%00` NUL would crash the backend's realpath).
    const start = node?.position?.start?.offset
    const end = node?.position?.end?.offset
    const wrapped = source != null && start != null && end != null
      && /\]\(\s*</.test(source.slice(start, end))
    const localPath = wrapped ? decodeLocalPath(src) : src
    if (basePath && !src.startsWith('/') && !src.startsWith('~') && !isWinAbs) {
      const resolved = basePath.replace(/\/[^/]*$/, '') + '/' + localPath
      url = `/api/file-raw?path=${encodeURIComponent(resolved)}`
    } else {
      url = `/api/file-raw?path=${encodeURIComponent(localPath)}`
    }
    // See ImageVersionCtx: without this every impression of a rewritten file
    // shares one cache entry and a new message renders the previous bytes. The
    // backend reads only `path`, so the extra parameter is inert server-side.
    if (version) url += `&v=${encodeURIComponent(version)}`
  } else {
    url = src
  }
  if (errored) {
    return (
      <span className="text-sm text-muted inline-flex items-center gap-1">
        <Paperclip size={14} aria-hidden="true" />
        {' ' + (alt || src)}
      </span>
    )
  }
  // SVGs authored with only a `viewBox` (no width/height) carry no intrinsic
  // size. Under the max-w/max-h-only CSS below they collapse to ~0px and look
  // missing — so uploading several SVGs appears to render only the ones that
  // happen to declare width/height. Give SVGs a definite width basis; the
  // viewBox aspect ratio then derives the height, clamped by max-h.
  const isSvg = /\.svg([?#]|$)/i.test(src)
  // Reserve vertical layout space BEFORE the bytes decode. A markdown image has
  // no intrinsic dimensions in the source, so without this it lays out at ~0px
  // until the network/decode completes, then snaps to its natural height —
  // shoving every sibling below it (still-streaming text, the next block) down
  // in one discrete jump. For a user reading a streaming message (or lazily
  // loading an image below the fold) that reads as a "flash". Holding a
  // min-height placeholder until `onLoad` reserves the space up front and
  // bounds the on-load shift; the placeholder is released once loaded so the
  // final layout is pixel-exact and history/completed images carry no floor.
  // The floor is a heuristic (markdown gives us no aspect ratio): 120px sits
  // below the common screenshot/diagram case (which then benefits) but above
  // small icons/badges — for a sub-120px raster image the on-load change is a
  // bounded (<=120px) collapse, an accepted residual since such images are
  // uncommon in markdown. SVGs already get a definite width basis (their viewBox
  // derives the height), so they need no placeholder. See
  // MarkdownRenderer.streamingImageShift.test.tsx.
  // Learned exact dimensions trump the heuristic floor: a transcript image
  // remounts every time the virtualized window scrolls back over it, and a
  // 120px floor under a 400-600px screenshot still realizes the difference as
  // a visible jump on every (re)load. Recording naturalWidth/Height on first
  // successful load (keyed by resolved URL, same mechanism as the artifact
  // gallery's thumbnails) lets every later mount reserve the real aspect box
  // via width/height attributes before any bytes arrive.
  const learned = !isSvg ? getImageDims(url) : undefined
  // The reserved box must resolve to EXACTLY the size the loaded image will
  // take, or the difference shows as a border wrapping empty space with the
  // image floated centered inside (object-contain letterboxing). The loaded
  // layout follows the replaced-element min/max rules, which BACK-PROPAGATE a
  // max-height cap into the width (a tall screenshot capped at 60vh also
  // narrows). Neither width/height attributes nor an explicit aspect-ratio
  // reproduce that transfer — with either, max-height clamps the box's height
  // while the width stays at max-width, leaving a wide letterboxed band. So
  // spell the native resolution out: width = min(natural, heightCap × ratio),
  // with the class's max-width still capping on top; aspect-ratio then derives
  // the height. Same expression the loaded image resolves to, so the reserve
  // is invisible — same size, same left edge, border hugging the image.
  const imgStyle: React.CSSProperties | undefined = isSvg
    ? { width: compact ? '240px' : '760px', height: 'auto' }
    : learned
      ? reservedImageStyle(learned)
      : (loaded ? undefined : { minHeight: '120px' })
  // Sent-prompt (user message) images render as a small preview so an attached
  // screenshot doesn't dominate the bubble; the lightbox still opens full size
  // on click. Response images keep the large inline size. See CompactImagesCtx.
  // The className stays inline in the JSX attribute (rather than hoisted to a
  // variable) so the i18n lint's className exemption still recognizes these as
  // class strings, not untranslated copy.
  return (
    <span className="block my-2">
      {/* The <img> is the lightbox trigger; dispatchLightbox needs the image
          element itself as currentTarget and the [data-lightbox-image] query
          relies on it being an <img>, so it can't be a <button>. Keyboard users
          reach the same lightbox via other focusable controls; a visible <img>
          preview is presentational here. */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions */}
      <img
        src={url} alt={alt || ''} loading="lazy"
        // Sent-prompt images align to the END edge, matching the bubble they
        // were sent from. `ms-auto` (logical, RTL-correct) sits on the IMG, never
        // on its wrapper: preflight makes <img> display:block so text-align is
        // inert here, and a shrink-to-fit wrapper makes the percentage in
        // `max-w-[min(100%,240px)]` resolve against its own content — silently
        // dropping the 240px cap and scattering mixed-width images. It reads
        // right only because the bubble shrink-wraps (`w-fit` in UserMessage):
        // inside a bubble stretched to its cap, moving the image to one edge
        // only moves the empty band to the other. The cap is a DEFINITE 240px,
        // not `min(100%,240px)`: a percentage max-width makes the image's
        // max-content contribution indefinite, so the bubble's `w-fit` falls
        // back to the full available width and the band never closes. 240px sits
        // below the bubble's own cap at every width the app supports, so the
        // percentage guard was redundant.
        className={`${learned && !isSvg ? reservedImageClass(compact) + ' ' : ''}${compact
          ? 'ms-auto max-w-[240px] max-h-[180px] object-contain rounded-md border border-border cursor-pointer hover:opacity-90 transition-opacity'
          : 'max-w-[min(100%,760px)] max-h-[60vh] object-contain rounded-md border border-border cursor-pointer hover:opacity-90 transition-opacity'}`}
        style={imgStyle}
        onClick={(e) => dispatchLightbox(e.currentTarget)}
        data-lightbox-image=""
        title={alt || src}
        onLoad={(e) => {
          const el = e.currentTarget
          if (el.naturalWidth > 0 && el.naturalHeight > 0) rememberImageDims(url, el.naturalWidth, el.naturalHeight)
          setLoaded(true)
        }}
        onError={() => setErrored(true)}
        {...props}
      />
    </span>
  )
}

// Disable single-$ inline math so currency strings like `$9.99` don't
// accidentally trigger KaTeX math parsing. With singleDollarTextMath=true (the
// default in remark-math v6), chat messages containing multiple dollar amounts
// get parsed as one giant math expression spanning the first $ to the last,
// which KaTeX then fails to render -- producing HTML that React cannot commit
// and crashing the whole dashboard with "DOMException: String contains an
// invalid character" during completeWork. Only $$...$$ display-math blocks
// are treated as math now; single $ is plain text.

/**
 * Rehype plugin: ALLOWLIST-based HTML sanitization of the HAST tree.
 * Unknown/unrecognized tags are converted to escaped text (renders literally)
 * rather than passed to React as elements -- prevents React error #290 crashes
 * from bare XML tags like `<dynamoDBClient>` in agent output.
 */
const ALLOWED_TAGS = new Set([
  // Block structure
  'div', 'span', 'p', 'br', 'hr',
  // Headings
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  // Lists
  'ul', 'ol', 'li',
  // Inline formatting
  'strong', 'b', 'em', 'i', 'del', 's', 'u', 'mark', 'small',
  'sup', 'sub', 'kbd', 'abbr', 'cite', 'q', 'var', 'samp',
  // Code
  'code', 'pre',
  // Links & media
  'a', 'img', 'picture', 'source', 'video', 'audio',
  // Tables
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
  // Semantic blocks
  'blockquote', 'details', 'summary', 'figure', 'figcaption',
  // Semantic HTML5 structure (remark-gfm emits <section> for footnotes)
  'section', 'article', 'header', 'footer', 'nav', 'aside', 'time',
  // Forms (only checkbox for GFM task lists -- further constrained below)
  'input',
  // Misc safe elements
  'dl', 'dt', 'dd', 'ruby', 'rt', 'rp', 'wbr',
  // SVG (inline diagrams)
  'svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'text', 'g', 'defs', 'use',
  'tspan', 'ellipse', 'lineargradient', 'radialgradient', 'stop', 'title', 'desc', 'clippath', 'marker',
  // Math (rehypeKatex pipeline -- pass through rehypeRaw)
  'math', 'inlinemath',
])
const DANGEROUS_PROTOCOLS = ['javascript:', 'data:', 'vbscript:']
const cleanUrl = (url: string) => url.replace(/[\x00-\x1f\x7f]/g, '').trim().toLowerCase()

/**
 * Attribute ALLOWLIST (replaces the former on-handler / protocol denylist).
 *
 * frontend-security: for an allowlisted element we now KEEP only the attributes
 * explicitly permitted for it and DROP everything else — so `style`,
 * `formaction`, `srcset`-on-the-wrong-tag, unknown `on*` handlers, etc. are all
 * removed by default rather than only the handful we remembered to block.
 *
 * Matching is case-insensitive because hast camelCases some property names
 * (`viewBox`, `colSpan`, `ariaHidden`, `data-*` → `dataSourcepos`); we always
 * compare on the lowercased key. `aria*`/`data*` prefixes are allowed wholesale
 * (inert, a11y/metadata only).
 */
const GLOBAL_ATTRS = new Set([
  'classname', 'class', 'id', 'title', 'dir', 'lang', 'role', 'align',
])
const TAG_ATTRS: Record<string, Set<string>> = {
  a: new Set(['href', 'name', 'target', 'rel']),
  img: new Set(['src', 'alt', 'width', 'height', 'loading']),
  input: new Set(['type', 'checked', 'disabled']),
  ol: new Set(['start', 'type', 'reversed']),
  li: new Set(['value']),
  td: new Set(['colspan', 'rowspan', 'headers']),
  th: new Set(['colspan', 'rowspan', 'scope', 'headers']),
  col: new Set(['span', 'width']),
  colgroup: new Set(['span', 'width']),
  source: new Set(['src', 'srcset', 'type', 'media', 'sizes']),
  video: new Set(['src', 'controls', 'width', 'height', 'poster', 'loop', 'muted', 'preload']),
  audio: new Set(['src', 'controls', 'loop', 'muted', 'preload']),
  details: new Set(['open']),
  time: new Set(['datetime']),
}
// SVG-family elements share a pool of inert presentation/geometry attributes.
const SVG_TAGS = new Set([
  'svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'text', 'g',
  'defs', 'use', 'tspan', 'ellipse', 'lineargradient', 'radialgradient', 'stop',
  'clippath', 'marker',
])
const SVG_ATTRS = new Set([
  'viewbox', 'xmlns', 'fill', 'stroke', 'strokewidth', 'strokelinecap',
  'strokelinejoin', 'strokedasharray', 'strokeopacity', 'fillopacity',
  'fillrule', 'cliprule', 'clippath', 'opacity', 'transform', 'd', 'points',
  'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'width',
  'height', 'offset', 'stopcolor', 'stopopacity', 'gradientunits',
  'gradienttransform', 'preserveaspectratio', 'markerwidth', 'markerheight',
  'refx', 'refy', 'orient',
])
/** True when `key` is a permitted attribute for element `tag` (both lowercased). */
function isAllowedAttr(tag: string, key: string): boolean {
  const k = key.toLowerCase()
  if (k.startsWith('aria') || k.startsWith('data')) return true
  if (GLOBAL_ATTRS.has(k)) return true
  if (TAG_ATTRS[tag]?.has(k)) return true
  if (SVG_TAGS.has(tag) && SVG_ATTRS.has(k)) return true
  return false
}

/** Elements that cannot have children per HTML spec (used by escapedNodeTree). */
const VOID_ELEMENTS = new Set(['img', 'br', 'hr', 'input', 'source', 'wbr', 'col'])

/** HAST element node shape (subset used by sanitize pipeline). */
interface HastNode {
  type: string
  tagName?: string
  value?: string
  properties?: Record<string, unknown>
  children?: HastNode[]
}

/** Tags never reconstructed — even as escaped text, faithful reconstruction of
 * executable elements is a liability. They collapse to an [unsupported:] marker. */
const UNSAFE_RECONSTRUCT_TAGS = new Set([
  'script', 'style', 'iframe', 'object', 'embed', 'form', 'link', 'meta', 'base', 'noscript',
])

const textNode = (value: string): HastNode => ({ type: 'text', value })

/** Convert a non-allowlisted element into a SAFE HAST element tree for display.
 *
 * frontend-security: no HTML string is ever materialized from untrusted content.
 * The node's source form is represented as a `<span class="escaped-tag">` whose
 * children are discrete TEXT fragments — the `<` / `>` delimiters live in their
 * own text nodes, separate from the tag/attribute content — so no single string
 * anywhere in the tree contains parseable markup, and React renders text nodes
 * safely by construction. Filters retained from the sanitizer: `on*` handler
 * attributes dropped, tag/attr names restricted to a safe charset, and
 * dangerous-protocol attribute values (javascript:/data:/vbscript:) dropped.
 */
function escapedNodeTree(node: HastNode): HastNode {
  const tag = (node.tagName ?? '').replace(/[^a-zA-Z0-9-]/g, '')
  const wrap = (children: HastNode[]): HastNode => ({
    type: 'element',
    tagName: 'span',
    properties: { className: ['escaped-tag'] },
    children,
  })
  if (UNSAFE_RECONSTRUCT_TAGS.has(tag.toLowerCase())) {
    return wrap([textNode(`[unsupported: ${tag}]`)])
  }
  const attrs = node.properties
    ? Object.entries(node.properties)
        .filter(([k]) => k !== 'className' && !/^on/i.test(k) && /^[a-zA-Z0-9_:-]+$/.test(k))
        .filter(([, v]) => typeof v !== 'string' || !DANGEROUS_PROTOCOLS.some(p => cleanUrl(v).startsWith(p)))
        .map(([k, v]) => (v === true ? k : `${k}="${String(v)}"`))
        .join(' ')
    : ''
  const children: HastNode[] = [textNode('<'), textNode(attrs ? `${tag} ${attrs}` : tag), textNode('>')]
  for (const c of node.children || []) {
    if (c.type === 'text') children.push(textNode(c.value ?? ''))
    else if (c.type === 'element') children.push(escapedNodeTree(c))
  }
  if (!VOID_ELEMENTS.has(tag)) {
    children.push(textNode('</'), textNode(tag), textNode('>'))
  }
  return wrap(children)
}

/**
 * Exported so every markdown surface in the product shares ONE sanitize policy.
 *
 * Any renderer that admits raw HTML (`rehype-raw`) needs this immediately after
 * it, and a second surface must never carry its own copy of the allowlist: the
 * policy is security-relevant, so a fork would silently drift out of step with
 * this one. The plugin is pure (no React, no styling), so a surface that cannot
 * reuse the component itself can still reuse the policy.
 */
export function rehypeSanitize() {
  return (tree: HastNode) => {
    const walk = (node: HastNode, parent: HastNode, index: number) => {
      // TS strict-null: HastNode.children is `HastNode[] | undefined`. Callers only
      // recurse into nodes whose children array they are iterating, so this cannot
      // happen for a well-formed HAST tree — guard defensively and move on.
      if (!parent.children) return index + 1
      if (node.type === 'element') {
        const tagLower = (node.tagName || '').toLowerCase()

        // Allowlist check: unknown tags become a safe element tree of text
        // fragments (no HTML string is ever built from untrusted content)
        if (!ALLOWED_TAGS.has(tagLower)) {
          parent.children.splice(index, 1, escapedNodeTree(node))
          return index + 1  // skip past the replacement (already safe)
        }

        // input: only allow GFM task-list checkboxes
        if (tagLower === 'input') {
          if (node.properties?.type === 'checkbox') {
            node.properties = { type: 'checkbox', checked: !!node.properties.checked, disabled: true }
          } else {
            parent.children.splice(index, 1)
            return index
          }
        }

        // Attribute ALLOWLIST: keep only attributes permitted for this element;
        // drop everything else (was: a denylist that stripped on*/protocol/srcdoc
        // and kept the rest). Retained URL-bearing attrs still get the
        // dangerous-protocol check below.
        if (node.properties) {
          for (const [key, val] of Object.entries(node.properties)) {
            if (!isAllowedAttr(tagLower, key)) {
              delete node.properties[key]
              continue
            }
            if (typeof val === 'string') {
              const cleaned = cleanUrl(val)
              if (DANGEROUS_PROTOCOLS.some(p => cleaned.startsWith(p))) {
                // Allow data:image/* on img src (inline base64 images)
                if (node.tagName === 'img' && key === 'src' && cleaned.startsWith('data:image/')) {
                  continue
                }
                delete node.properties[key]
              }
            }
          }
        }
      }
      if (node.children) {
        for (let i = 0; i < node.children.length; i++) {
          const result = walk(node.children[i], node, i)
          if (typeof result === 'number') i = result - 1  // re-check after splice
        }
      }
    }
    if (tree.children) {
      for (let i = 0; i < tree.children.length; i++) {
        const result = walk(tree.children[i], tree, i)
        if (typeof result === 'number') i = result - 1
      }
    }
  }
}

/** A whole mdast `html` node that is exactly ONE tag: `<x>`, `</x>`, `<x a b>`,
 * `<x/>`. Attribute values are quote-aware, so a value may itself contain `>`
 * (`<x a="b>c">`); without that, such a tag misses this test and falls to the
 * lossy escapedNodeTree() path. A bare attribute may hold `/` (`<x a/b>`) so
 * this accepts everything the previous blanket `[^>]*` did. The leading
 * `[a-zA-Z]` excludes comments (`<!-- -->`) and doctypes, which keep their
 * existing handling. */
const SINGLE_TAG_RE =
  /^<\/?([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^\s=>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*))?)*)\s*\/?>$/

/** Tag name of a single-tag html node, or undefined when it is not one. */
function singleTagName(value: string): string | undefined {
  return SINGLE_TAG_RE.exec(value)?.[1]?.toLowerCase()
}

/** Showable verbatim. Executable tags keep their `[unsupported: x]` marker; every
 * other unknown tag diverts, because a text node is inert wherever it lands. */
function divertibleTag(tag: string): boolean {
  return !UNSAFE_RECONSTRUCT_TAGS.has(tag)
}

/** Index of the sibling that closes `tag`, tracking same-tag nesting; -1 if unclosed. */
function matchingCloseIndex(kids: MdastNode[], start: number, tag: string): number {
  let depth = 0
  for (let j = start + 1; j < kids.length; j++) {
    const k = kids[j]
    if (k.type !== 'html' || typeof k.value !== 'string') continue
    if (singleTagName(k.value) !== tag) continue
    if (k.value.startsWith('</')) {
      if (depth === 0) return j
      depth--
    } else if (!k.value.endsWith('/>')) depth++
  }
  return -1
}

/** Render non-allowlisted single tags VERBATIM instead of reconstructing them.
 *
 * Runs at the remark (mdast) stage, before rehypeRaw reaches the HTML parser. An
 * mdast `html` node's `value` IS the author's original source substring, so
 * converting it to `text` reproduces exactly what was typed: original case,
 * original spacing, and no closing tag the author never wrote.
 *
 * Deliberately narrow — two things keep existing escapedNodeTree() handling:
 * multi-tag raw HTML blocks, and UNSAFE_RECONSTRUCT_TAGS (script/style/iframe
 * still collapse to `[unsupported: x]`). Everything else diverts, including a
 * tag whose attribute value is a dangerous protocol — see frontend-security.
 *
 * Exported so every markdown surface that admits raw HTML shares this pass; a
 * surface wiring rehypeSanitize without it keeps the lossy reconstruction.
 *
 * frontend-security: the tag never becomes an element and never reaches the HTML
 * parser — it ends up a text node, which React escapes on render, so the React
 * #290 guard still holds.
 */
export function remarkVerbatimUnknownTags() {
  return (tree: MdastNode) => {
    const walk = (node: MdastNode) => {
      const kids = node.children
      if (!kids) return
      for (let i = 0; i < kids.length; i++) {
        const child = kids[i]
        if (child.type === 'html' && typeof child.value === 'string') {
          const tag = singleTagName(child.value)
          if (tag && !ALLOWED_TAGS.has(tag) && divertibleTag(tag)) {
            const paired = child.value.startsWith('</') || child.value.endsWith('/>')
              ? -1
              : matchingCloseIndex(kids, i, tag)
            if (paired > i) {
              // A closed container: divert the whole span, so allowlisted tags
              // inside it stay literal instead of rendering as live elements.
              for (let j = i; j <= paired; j++) {
                const k = kids[j]
                if (k.type !== 'html' || typeof k.value !== 'string') continue
                const kt = singleTagName(k.value)
                if (kt && divertibleTag(kt)) k.type = 'text'
              }
            } else {
              // Verbatim source text — no HTML string is built or re-parsed.
              child.type = 'text'
            }
          }
        }
        walk(child)
      }
    }
    walk(tree)
  }
}

// CommonMark has a known emphasis defect (commonmark/commonmark-spec#650): a
// closing `**` is only right-flanking when it is NOT preceded by punctuation, or
// IS followed by whitespace/punctuation. `**中文（带括号）。**这句` fails both —
// preceded by `。`, followed by the letter `这` — so it renders as literal
// asterisks. English prose sidesteps this by putting a space after the `**`; CJK
// cannot, because a space there is visibly wrong.
//
// `remark-cjk-friendly` implements the CJK-friendly flanking amendment. ORDER IS
// LOAD-BEARING: it must run BEFORE remark-gfm (it changes how emphasis
// delimiters are classified), and the strikethrough companion AFTER, because it
// extends gfm's own `~~` construct.
const REMARK_PLUGINS: PluggableList = [
  remarkCjkFriendly,
  remarkGfm,
  remarkCjkFriendlyGfmStrikethrough,
  [remarkMath, { singleDollarTextMath: false }],
  remarkVerbatimUnknownTags,
]

/**
 * HTML block-level elements that cannot legally nest inside `<p>`. When
 * `rehype-raw` parses raw HTML embedded in markdown, it may produce a HAST tree
 * with a block element inside a `<p>` (e.g. `<p><div>…</div></p>`). The
 * browser's HTML parser auto-corrects this by closing the `<p>` before the
 * block element, moving the block out — but React's VDOM still thinks the block
 * is inside the `<p>`. On the next reconciliation React tries to `removeChild`
 * from `<p>`, the node is no longer there, and we get:
 *   "Failed to execute 'removeChild' on 'Node': The node to be removed is not
 *    a child of this node."
 *
 * This plugin mirrors the browser's correction at the HAST level so React's
 * tree matches reality from the first render.
 */
const BLOCK_ELEMENTS = new Set([
  'address', 'article', 'aside', 'blockquote', 'details', 'dialog', 'dd',
  'div', 'dl', 'dt', 'fieldset', 'figcaption', 'figure', 'footer', 'form',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hgroup', 'hr', 'li',
  'main', 'nav', 'ol', 'p', 'pre', 'section', 'table', 'ul',
])

/**
 * Stamps `data-fenced` on every `<code>` that is the child of a `<pre>`.
 *
 * `MD_COMPONENTS.code` renders a block-level component for a code element that
 * carries a class (`CodeBlock`, `MermaidBlock` and `ExcalidrawBlock` are each
 * rooted in a `<div>`). Real fenced blocks never reach it — `useBlockAssembler`
 * segments those out of the source and `BlockRenderer` draws them directly — so
 * the only classed code elements arriving here come from raw HTML in prose.
 * `<pre><code class="language-js">` is the legitimate shape: the `<pre>` is
 * block-level, so `rehypeUnwrapBlocks` hoists it clear of any surrounding `<p>`
 * and the block renders as a sibling.
 *
 * A BARE `<code class="language-js">` mid-sentence is not. The sanitizer
 * allowlists `class` globally (see GLOBAL_ATTRS), so it survives, keeps its
 * class, and renders a `<div>` inside the enclosing `<p>`. The browser hoists
 * that `<div>` out of the `<p>`, React's VDOM does not follow, and the next
 * reconciliation throws:
 *   "Failed to execute 'removeChild' on 'Node': The node to be removed is not
 *    a child of this node."
 *
 * `rehypeUnwrapBlocks` cannot catch this, because it decides block-ness from the
 * HAST tag name and `code` is inline there — the block only appears in what the
 * component renders. Marking the genuinely fenced ones lets the override keep
 * inline code inline whatever class it carries, which is also what the source
 * asked for.
 */
function rehypeMarkFencedCode() {
  return (tree: HastRoot) => {
    const walk = (node: HastRoot | HastElement) => {
      if (!node.children) return
      const isPre = node.type === 'element' && node.tagName === 'pre'
      for (const child of node.children) {
        if (child.type !== 'element') continue
        // The marker is ours to set and no one else's. `isAllowedAttr` admits
        // every `data-*`, so raw HTML in the message can carry its own
        // `data-fenced` — and an inline `<code data-fenced class="language-js">`
        // would then claim block rendering and reintroduce the very crash this
        // plugin exists to prevent.
        //
        // Deleting a fixed key spelling is not enough. The HTML parser
        // lowercases attribute names, so `dataFenced` arrives as the hast
        // property `datafenced`, which the JSX serializer still hands to the
        // component as `data-fenced`. Strip by NORMALIZED form so every casing
        // and dash placement that can reach the override as the marker is
        // removed here.
        if (child.properties) {
          for (const key of Object.keys(child.properties)) {
            if (key.toLowerCase().replace(/-/g, '') === 'datafenced') {
              delete child.properties[key]
            }
          }
        }
        if (isPre && child.tagName === 'code') {
          child.properties = { ...(child.properties ?? {}), 'data-fenced': '' }
        }
        walk(child)
      }
    }
    walk(tree)
  }
}

function rehypeUnwrapBlocks() {
  return (tree: HastRoot) => {
    const walk = (parent: HastRoot | HastElement) => {
      if (!parent.children) return
      for (let i = 0; i < parent.children.length; i++) {
        const child = parent.children[i]
        if (child.type === 'element') walk(child)
      }
      // Only `<p>` elements need unwrapping (that's the only element the
      // browser auto-closes when it encounters a block child).
      if (parent.type !== 'element' || parent.tagName !== 'p') return
      const hasBlock = parent.children.some(
        c => c.type === 'element' && BLOCK_ELEMENTS.has(c.tagName),
      )
      if (!hasBlock) return

      // Split: children before a block go into a <p>, the block becomes a
      // sibling, children after go into the next iteration's bucket. We
      // rebuild the parent's slot in-place by replacing it in the grandparent.
      // Since we're walking depth-first and only mutate the CURRENT parent's
      // children list at the grandparent level, we handle this by returning
      // replacement nodes and letting the outer walk splice them.
      const replacement: RootContent[] = []
      let bucket: RootContent[] = []
      const flushBucket = () => {
        // Only emit a <p> wrapper if the bucket has non-whitespace content.
        const hasContent = bucket.some(n =>
          n.type === 'element' || (n.type === 'text' && n.value.trim()),
        )
        if (hasContent) {
          replacement.push({
            type: 'element',
            tagName: 'p',
            properties: { ...(parent as HastElement).properties },
            children: bucket as HastElement['children'],
            // Preserve source position so rehypeSourcepos can stamp
            // data-sourcepos on the synthesized wrappers (needed for
            // inline-comment anchoring).
            position: (parent as HastElement).position,
          })
        }
        bucket = []
      }
      for (const child of parent.children) {
        if (child.type === 'element' && BLOCK_ELEMENTS.has(child.tagName)) {
          flushBucket()
          replacement.push(child as RootContent)
        } else {
          bucket.push(child as RootContent)
        }
      }
      flushBucket()
      // Stash the replacement so the caller can splice it.
      ;(parent as HastElement & { _unwrapReplacement?: RootContent[] })._unwrapReplacement = replacement
    }

    // Two-pass: first walk marks <p> elements that need splitting, then we
    // splice replacements into their parents top-down. A single pass that
    // mutates children while iterating would skip indices.
    const splice = (node: HastRoot | HastElement) => {
      if (!node.children) return
      let i = 0
      while (i < node.children.length) {
        const child = node.children[i]
        if (child.type === 'element') splice(child)
        const rep = (child as HastElement & { _unwrapReplacement?: RootContent[] })._unwrapReplacement
        if (rep) {
          delete (child as HastElement & { _unwrapReplacement?: RootContent[] })._unwrapReplacement
          ;(node.children as RootContent[]).splice(i, 1, ...rep)
          i += rep.length
        } else {
          i++
        }
      }
    }

    walk(tree)
    splice(tree)
  }
}

const REHYPE_PLUGINS: PluggableList = [[rehypeRaw, { passThrough: ['math', 'inlineMath'] }], rehypeMarkFencedCode, rehypeUnwrapBlocks, rehypeSanitize, rehypeKatex]

// Matches one source line break plus any leading tabs/spaces, so a trailing
// space before the break doesn't survive as its own text node. Mirrors the
// pattern used by the `remark-breaks` package.
const SOFT_BREAK_RE = /[\t ]*(?:\r?\n|\r)/g

/**
 * remark plugin: turn soft line breaks (a lone source newline inside a
 * paragraph, which CommonMark otherwise collapses to a space) into hard breaks
 * (mdast `break` → <br>). This is an inlined equivalent of the `remark-breaks`
 * package, kept local to avoid adding a runtime dependency.
 *
 * Opt-in via MarkdownRenderer's `softBreaks` prop and used ONLY for user
 * messages: the chat input lets people press Shift+Enter for a newline, so
 * those breaks must survive rendering. Assistant/LLM markdown keeps standard
 * CommonMark soft-break-collapse.
 *
 * Operates on `text` nodes only, so fenced code, inline code, math, and raw
 * HTML (whose content lives in `.value`, not `.children`) are untouched, and
 * blank-line block separators — already parsed as distinct blocks — are not
 * affected, so lists and paragraphs keep their normal block spacing. That is
 * what lets user messages drop container-level `white-space: pre-wrap`, which
 * had made react-markdown's inter-block newline text nodes render as literal
 * blank lines and inflated list/paragraph gaps.
 */
function remarkSoftBreaks() {
  const visit = (node: { type?: string; value?: string; children?: unknown[] }) => {
    if (!node || !Array.isArray(node.children)) return
    const out: unknown[] = []
    for (const raw of node.children) {
      const child = raw as { type?: string; value?: string; children?: unknown[] }
      if (child.type === 'text' && typeof child.value === 'string' && /[\r\n]/.test(child.value)) {
        const value = child.value
        let start = 0
        SOFT_BREAK_RE.lastIndex = 0
        let match: RegExpExecArray | null
        while ((match = SOFT_BREAK_RE.exec(value))) {
          if (match.index > start) out.push({ type: 'text', value: value.slice(start, match.index) })
          out.push({ type: 'break' })
          start = match.index + match[0].length
        }
        if (start < value.length) out.push({ type: 'text', value: value.slice(start) })
      } else {
        visit(child)
        out.push(child)
      }
    }
    // A break ADJACENT to an image is redundant and inflates spacing: the
    // image renders as its own block (span.block.my-2), so the line break is
    // already implied — the <br> would add an empty line box (~one
    // line-height) AND keep the neighbouring margins from collapsing,
    // turning the intended 8px gap between two attached screenshots into
    // ~37px. Text-to-text breaks (Shift+Enter prose) are untouched.
    const isImage = (n: unknown): boolean => (n as { type?: string })?.type === 'image'
    node.children = out.filter((n, i) => {
      if ((n as { type?: string })?.type !== 'break') return true
      return !(isImage(out[i - 1]) || isImage(out[i + 1]))
    })
  }
  return (tree: unknown) => visit(tree as { children?: unknown[] })
}

// User-message variant: base remark chain plus soft-break → hard-break.
const REMARK_PLUGINS_WITH_BREAKS: PluggableList = [...REMARK_PLUGINS, remarkSoftBreaks]

/**
 * Rehype plugin that copies each hast element's source `position` onto a
 * `data-sourcepos` HTML attribute in CommonMark format `startLine:startCol-endLine:endCol`.
 * Used by the inline-commenting flow to map selection DOM → source coordinates.
 * Replaces the deprecated `sourcePos` option removed in react-markdown v10.
 */
function rehypeSourcepos() {
  return (tree: HastRoot) => {
    const walk = (node: HastRoot | RootContent) => {
      if (node.type === 'element' && node.position?.start) {
        const s = node.position.start, e = node.position.end ?? s
        node.properties = node.properties || {}
        node.properties['data-sourcepos'] = `${s.line}:${s.column}-${e.line}:${e.column}`
      }
      if ('children' in node && node.children) for (const c of node.children) walk(c)
    }
    walk(tree)
  }
}
const REHYPE_PLUGINS_WITH_SOURCEPOS: PluggableList = [[rehypeRaw, { passThrough: ['math', 'inlineMath'] }], rehypeMarkFencedCode, rehypeUnwrapBlocks, rehypeSanitize, rehypeKatex, rehypeSourcepos]
// NOTE: remark plugin config is shared via REMARK_PLUGINS above (singleDollarTextMath:
// false). The sourcepos variant only differs in the rehype chain.

/** Number of trailing characters glowed while a message streams. */
const GLOW_TAIL_CHARS = 30

/**
 * Rehype plugin: wrap the message's trailing text in a
 * `<span class="streaming-glow">` so the newest streamed words shimmer.
 *
 * Operates on the parsed HAST tree (not the markdown source and not the live
 * DOM), so it: (a) never builds a raw HTML string with LLM content — the span
 * is a real element node react-markdown renders as a React `<span>`; (b) never
 * bisects a markdown token — by this stage `**bold**` is already a `<strong>`
 * element, so splitting the last *text* node is always safe; (c) doesn't mutate
 * React-owned DOM, so it can't cause reconciliation crashes.
 *
 * Glows the whole last text node when it's short, else its last GLOW_TAIL_CHARS
 * on a space boundary (never mid-word). Skips text inside code/pre.
 */
function rehypeStreamingGlow(options?: { tailChars?: number }) {
  const tailChars = options?.tailChars ?? GLOW_TAIL_CHARS
  return (tree: HastRoot) => {
    // Collect every eligible text node (non-whitespace, not inside code/pre);
    // the streaming tail is the last one. Using an array (rather than a
    // closure-mutated `let`) keeps TypeScript's control-flow narrowing happy.
    const candidates: { parent: HastParent; index: number; value: string }[] = []
    const walk = (node: RootContent, parent: HastParent, index: number, inCode: boolean) => {
      if (node.type === 'text') {
        if (!inCode && node.value && node.value.trim()) {
          candidates.push({ parent, index, value: node.value })
        }
        return
      }
      const code = inCode || (node.type === 'element' && (node.tagName === 'code' || node.tagName === 'pre'))
      if ('children' in node && node.children) {
        for (let i = 0; i < node.children.length; i++) walk(node.children[i], node, i, code)
      }
    }
    for (let i = 0; i < tree.children.length; i++) walk(tree.children[i], tree, i, false)
    const target = candidates[candidates.length - 1]
    if (!target) return
    const { parent, index, value } = target
    let cut: number
    if (value.length <= tailChars) {
      cut = 0
    } else {
      const sp = value.lastIndexOf(' ', value.length - tailChars)
      cut = sp > 0 ? sp : value.length - tailChars
    }
    const before = value.slice(0, cut)
    const tail = value.slice(cut)
    if (!tail.trim()) return
    const span: HastElement = {
      type: 'element',
      tagName: 'span',
      properties: { className: ['streaming-glow'] },
      children: [{ type: 'text', value: tail }],
    }
    const beforeNode: HastText = { type: 'text', value: before }
    spliceChildren(parent, index, before ? [beforeNode, span] : [span])
  }
}

/** Split a text run into individual characters for per-char animation. */
const REVEAL_CHAR_RE = /[\s\S]/g

/** How many trailing characters of the streaming tail carry the reveal fade.
 *  Only this growing EDGE is sub-opaque; text that has settled behind it is
 *  left as plain, fully-opaque text nodes. Sized to comfortably cover the
 *  smooth buffer's per-frame reveal wave (MAX_CPS burst) so genuinely-new text
 *  still materializes over several frames. */
const REVEAL_FADE_CHARS = 32
/** Opacity of the newest (tip) character; older chars ramp linearly to 1 across
 *  REVEAL_FADE_CHARS. Kept well above 0 so a mid-stream PAUSE never leaves the
 *  trailing words hard to read — the reveal is a gentle materialization, not a
 *  fade-from-invisible. */
const REVEAL_MIN_OPACITY = 0.6

/** How long the rendered content must sit unchanged before the reveal edge is
 *  settled to full opacity. `--ft-o` is POSITIONAL, so only the tip advancing
 *  raises a character's opacity. This matters for exactly one case: a stream
 *  that PAUSES mid-turn (the gap while the model composes tool arguments), where
 *  `streaming` is still true and nothing advances the tip, leaving the last
 *  REVEAL_FADE_CHARS characters pinned as low as REVEAL_MIN_OPACITY for the
 *  whole pause. A FINISHED stream is already self-healing and needs nothing:
 *  rehypeStreamingReveal is only in the pipeline while `glow` is set, and
 *  `glow` follows `isStreaming`, so the spans are dropped on the next re-parse.
 *  Do not "simplify" this into `animOn = !!smooth && streaming` — that only
 *  covers the self-healing case and cannot cover a pause, where streaming is
 *  true by definition. */
const REVEAL_IDLE_SETTLE_MS = 500

/** Opacity for a character `d` positions back from the streaming tip (d=0 is
 *  the newest char). Deliberately a pure function of POSITION, not of mount
 *  time — this is the streaming-flash fix. react-markdown re-parses the whole
 *  tail every frame, and when a newly-revealed char COMPLETES a markdown token
 *  (inline `code`, **bold**, a [link], a heading/list marker, …) the subtree
 *  restructures, so React unmounts/remounts the `.ft-word` spans for text that
 *  was ALREADY on screen. A mount-triggered CSS keyframe (like `ft-char-fade`)
 *  would re-run on every such remount → a visible flash, right at the active
 *  edge where the eye is. With position-derived opacity a
 *  remounted span re-appears at the IDENTICAL opacity, so it cannot re-fade;
 *  only the tip advancing changes a char's opacity, giving a smooth
 *  materialization. Confirmed by src/test/streamingFlashRepro.test.tsx. */
function revealOpacity(d: number): number {
  if (d >= REVEAL_FADE_CHARS - 1) return 1
  const o = REVEAL_MIN_OPACITY + (1 - REVEAL_MIN_OPACITY) * (d / (REVEAL_FADE_CHARS - 1))
  return Math.round(o * 100) / 100
}

/**
 * Rehype plugin: wrap the streaming tail's TRAILING EDGE in `<span
 * class="ft-word" style="--ft-o:…">` so each character carries a
 * position-derived opacity (see revealOpacity). Only the last
 * REVEAL_FADE_CHARS characters are wrapped; text that has settled behind the
 * edge stays as plain, fully-opaque text nodes.
 *
 * Text inside `code`/`pre` (rendered by the code components) and
 * `.streaming-glow` is skipped. Atomic block components (fenced code, widgets,
 * mermaid, diffs) are separate non-text blocks and are not faded here.
 *
 * The reveal is driven by CSS opacity that is a pure function of each char's
 * distance to the tip — NOT a mount-triggered animation — so react-markdown's
 * per-frame re-parse (which remounts edge spans whenever a markdown token
 * completes) can never re-fire the fade on already-visible text. That
 * remount-immunity is the streaming-flash fix. This plugin runs AFTER
 * rehypeSanitize in the pipeline, so the inline `--ft-o` style it adds is not
 * stripped by the attribute allowlist. On stream end the plugin drops out and
 * the tail reverts to plain text (clean for selection/copy).
 */
function rehypeStreamingReveal() {
  return (tree: HastRoot) => {
    const candidates: { parent: HastParent; index: number; value: string }[] = []
    const walk = (node: RootContent, parent: HastParent, index: number, skip: boolean) => {
      if (node.type === 'text') {
        if (!skip && node.value && node.value.trim()) {
          candidates.push({ parent, index, value: node.value })
        }
        return
      }
      const cls = node.type === 'element' ? node.properties?.className : undefined
      const isGlow = Array.isArray(cls) && cls.includes('streaming-glow')
      // Skip text inside `pre` (fenced code/diff render via their own
      // components) and the glow window. Inline `code` is NOT skipped so it
      // char-fades like the surrounding prose — fenced blocks are separate
      // non-markdown blocks, so any `code` reached here is inline.
      const next = skip || isGlow || (node.type === 'element' && node.tagName === 'pre')
      if ('children' in node && node.children) {
        for (let i = 0; i < node.children.length; i++) walk(node.children[i], node, i, next)
      }
    }
    for (let i = 0; i < tree.children.length; i++) walk(tree.children[i], tree, i, false)
    if (candidates.length === 0) return
    // Wrap only the trailing REVEAL_FADE_CHARS characters, walking candidates
    // from the last (deepest in document order) backward and spending a shared
    // budget. Everything before the edge is left as-is (plain text). `fromEnd`
    // tracks how many wrapped chars lie AFTER the current candidate so each
    // span gets an opacity derived from its distance to the streaming tip.
    let budget = REVEAL_FADE_CHARS
    let fromEnd = 0
    for (let c = candidates.length - 1; c >= 0 && budget > 0; c--) {
      const { parent, index, value } = candidates[c]
      // Keep the leading (settled) portion of the boundary node as a plain text
      // node; only wrap its trailing chars. A char-exact cut is fine because
      // opacity is continuous — the boundary char lands at ~1.0, matching the
      // adjacent plain text, so there is no visible seam.
      const cut = value.length > budget ? value.length - budget : 0
      budget -= (value.length - cut)
      const head = value.slice(0, cut)
      const tail = value.slice(cut)
      const tokens = tail.match(REVEAL_CHAR_RE)
      if (!tokens || tokens.length === 0) continue
      // tokens are in document order; the last token of the last candidate is
      // the tip. distance-from-tip for tokens[i] = fromEnd + (last - i).
      const spans: Array<HastElement | HastText> = tokens.map((tok, i) => ({
        type: 'element',
        tagName: 'span',
        properties: { className: ['ft-word'], style: `--ft-o:${revealOpacity(fromEnd + (tokens.length - 1 - i))}` },
        children: [{ type: 'text', value: tok }],
      }))
      fromEnd += tokens.length
      // Splice highest index first (candidates ascend in document order, so
      // walking c downward gives descending indices within a shared parent),
      // keeping earlier candidates' indices valid.
      spliceChildren(parent, index, head ? [{ type: 'text', value: head } as HastText, ...spans] : spans)
    }
  }
}

/**
 * Rehype plugin: append an inline blinking caret (`<span class="streaming-caret">`)
 * immediately after the message's LAST trailing text node, so it sits inline at
 * the end of the streamed text (on the same line as the final word) rather than
 * on a new line below the block.
 *
 * Runs only while streaming (added under MarkdownBlock's `glow` gate, which is
 * true only for the last markdown block), so exactly one caret is injected. The
 * caret is a childless element node — the glow/reveal plugins that run after it
 * only touch text nodes, so it is left untouched and the trailing text still
 * gets its shimmer/fade. On stream end the plugin drops out and the caret
 * disappears with no leftover node (clean for selection/copy).
 *
 * Falls back to appending at the tree root only when there is no eligible text
 * yet (e.g. the block is pure code) — a rare edge where a new-line caret is
 * acceptable.
 */
function rehypeStreamingCaret() {
  return (tree: HastRoot) => {
    const candidates: { parent: HastParent; index: number }[] = []
    const walk = (node: RootContent, parent: HastParent, index: number) => {
      if (node.type === 'text') {
        if (node.value && node.value.trim()) candidates.push({ parent, index })
        return
      }
      // Block code: exclude entirely — the caret never belongs inside a fenced
      // code/diff block.
      if (node.type === 'element' && node.tagName === 'pre') return
      // Inline code: record the <code> element itself as a candidate at the
      // PARENT level (and don't recurse into its text children), so the caret
      // lands AFTER the inline code, not before it. Without this, a message
      // ending in `` `code` `` would splice the caret ahead of the <code>.
      if (node.type === 'element' && node.tagName === 'code') { candidates.push({ parent, index }); return }
      if (node.type === 'element' && node.children) {
        for (let i = 0; i < node.children.length; i++) walk(node.children[i], node, i)
      }
    }
    if (tree.children) {
      for (let i = 0; i < tree.children.length; i++) walk(tree.children[i], tree, i)
    }
    const caret: HastElement = {
      type: 'element',
      tagName: 'span',
      properties: { className: ['streaming-caret'], 'aria-hidden': 'true' },
      children: [],
    }
    const target = candidates[candidates.length - 1]
    if (target) {
      // Insert as the next sibling of the last visible node (text run or inline
      // <code>) so it renders inline right after the final content. Narrow on
      // the parent kind (RootContent[] vs ElementContent[]) to keep the insert
      // type-safe — spliceChildren removes a node, so it can't do an insert.
      if (target.parent.type === 'root') target.parent.children.splice(target.index + 1, 0, caret)
      else target.parent.children.splice(target.index + 1, 0, caret)
    } else if (tree.children) {
      tree.children.push(caret)
    }
  }
}

// ── CJK autolink boundaries ────────────────────────────────────────────────
//
// GFM's autolink-literal extension ends a bare `https://…` run only at ASCII
// whitespace or `<`. CJK punctuation written directly after a URL — the way
// Chinese and Japanese prose actually writes it, with no space — is therefore
// swallowed INTO the href:
//
//   （https://example.com/pull/1，`abc`）：`ready`
//   -> href="https://example.com/pull/1%EF%BC%8C%60abc%60…"
//
// The wrong href is the smaller half of the damage. The run also eats the
// OPENING backtick of the code span that follows, which shifts every later
// backtick pairing in the paragraph by one: prose renders as inline code and
// real code renders with literal backticks. One missing space corrupts the
// rest of the message.
//
// The same swallow takes the CLOSING `**` of a bold-wrapped URL, the shape
// `**https://…**（revision 1）` that CJK prose writes with no space between the
// emphasis and the punctuation after it. GFM trims a TRAILING `*`, so this only
// breaks when a non-space follows: the `**` stops being a delimiter, the opening
// `**` renders as two literal asterisks, and every later `**` in the paragraph
// re-pairs against the wrong partner.
//
// This has to be fixed at the SOURCE level, not on the mdast: re-splitting the
// link node after the fact cannot restore the code-span pairing, because the
// pairing is decided while micromark tokenizes the whole paragraph. So force
// the boundary before parsing by re-emitting the URL head as an angle autolink
// `<url>`, which has an explicit end and renders identically.
//
// The cut is EVIDENCE-BASED, not character-based — see cjkCutIndex and
// strongDelimCutIndex. CJK punctuation reaches real URLs raw
// (`…/wiki/苹果（公司）`), so cutting on the character alone would break links
// that render correctly today.
//
// Which regions are off-limits is read off remark's OWN parse (see
// autolinkLiteralSpans) rather than a hand-rolled scanner: only a real GFM
// autolink-literal node is ever touched, so code, existing links, raw HTML and
// math are excluded by construction instead of by a mask that has to re-derive
// every CommonMark block and inline rule correctly.
//
// Scope: only `http(s)://` runs. Scheme-less `www.` literals have the same flaw
// but cannot be closed with `<…>` (angle autolinks require a scheme).

// Punctuation classes. CJK punctuation is NOT by itself proof that a URL ended:
// real page titles contain it, and they reach the URL raw —
// `https://zh.wikipedia.org/wiki/苹果（公司）`, `https://zh.wikipedia.org/wiki/我，机器人`,
// `https://ja.wikipedia.org/wiki/モーニング娘。`. Cutting on the character alone
// would break links that render correctly today, so a cut needs EVIDENCE.
const CJK_PUNCT_RE =
  /[\u00b7\u2018\u2019\u201c\u201d\u2026\u3000-\u303f\u30fb\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65]/
const CJK_OPEN_BRACKETS = '\u3008\u300a\u300c\u300e\u3010\u3014\u3016\u3018\u301a\uff08\uff3b\uff5b\uff5f\uff62'
const CJK_CLOSE_BRACKETS = '\u3009\u300b\u300d\u300f\u3011\u3015\u3017\u3019\u301b\uff09\uff3d\uff5d\uff60\uff63'
// Sentence-ending CJK punctuation. These are NEVER treated as a URL boundary,
// because real page titles end in them and reach the URL raw —
// `…/wiki/モーニング娘。`, `…/wiki/魔法先生ネギま！`, `…/wiki/そして誰もいなくなった…`.
// A separator like `，` or `、` does not end a title, so it stays eligible.
const CJK_SENTENCE_ENDERS = '\u3002\uff0e\uff01\uff1f\u2026\uff61'
// The one character that makes markdown do something AND cannot appear in a
// raw-written URL. RFC 3986 excludes the backtick, so browsers percent-encode
// it — while `*`, `[` and `]` are all legal and common in query strings
// (`?q=foo，*test`, `?filter[name]=x`), so they are NOT evidence. The backtick
// is also the character whose loss does the real damage: the run eats an opening
// code-span delimiter and every later backtick pairing in the paragraph shifts.
const MD_ACTIVE_RE = /`/

// Strong-emphasis delimiters. A single `*` or `_` is NOT included: both are legal
// in a URL and common in query strings (`?q=foo，*test`, `?a=b_c`), so an unpaired
// one before the URL is too weak to act on. A DOUBLED delimiter carries the
// structural evidence instead — see strongDelimCutIndex.
const STRONG_DELIMS = ['**', '__']
const STRONG_DELIM_RE = /\*\*|__/

// CommonMark's character classes for delimiter flanking. Unicode-aware on
// purpose: the text this runs on is CJK prose, where the neighbour of a `**` is
// routinely a fullwidth punctuation mark (`：`, `）`) that ASCII classes miss —
// and misclassifying a neighbour flips whether a run can open emphasis.
const UNICODE_WS_RE = /[\s\p{Zs}]/u
const UNICODE_PUNCT_RE = /[\p{P}\p{S}]/u
/**
 * East Asian WIDE or FULLWIDTH characters — ideographs, kana, hangul, and the
 * fullwidth forms that carry CJK punctuation (`：（），。`). Ambiguous-width marks
 * (`·`, `…`, curly quotes) are deliberately absent: the renderer's amendment
 * treats those as ordinary punctuation, so this class must too.
 */
const CJK_WIDE_RE =
  /[\u1100-\u115f\u2e80-\ua4cf\ua960-\ua97f\uac00-\ud7a3\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff00-\uff60\uffe0-\uffe6]|[\u{20000}-\u{3fffd}]/u

// Where a bare URL may START, and the run GFM's tokenizer would take from there
// (everything up to ASCII whitespace or `<`). Only needed for a SECOND URL
// inside one autolink node's own run.
const URL_START_RE = /https?:\/\//g
const URL_RUN_AT_RE = /^https?:\/\/[^\s<]*/

// GFM only autolinks a host containing a dot, and neither of the last two
// labels may contain `_`. Wrapping a run GFM would NOT have linked would CREATE
// a link the author never wrote, so the head has to clear the same bar.
const AUTOLINKABLE_HOST_RE = /^https?:\/\/([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+)(?::\d+)?(?:[/?#]|$)/

// Which regions are off-limits (code, existing links, raw HTML, math) is NOT
// decided by hand-rolled scanning — it is read off remark's own parse of the
// source. Anything inside a fence, an indented block, an inline-code span
// (including a multi-line one), an existing link/image, an angle autolink, a raw
// HTML tag, or a math span simply never becomes an autolink-literal node, so it
// is unreachable here by construction. Built from the SAME `REMARK_PLUGINS` the
// render pipeline uses, so a future plugin addition cannot make the span-finder
// and the renderer disagree about what a link is.
const AUTOLINK_PARSER = unified().use(remarkParse).use(REMARK_PLUGINS).freeze()

type MdastNode = {
  type: string
  url?: string
  value?: string
  children?: MdastNode[]
  position?: { start: { offset?: number }; end: { offset?: number } }
}

// Node types whose source text is not prose. A bracket inside one of them is
// part of a URL, a code sample, a tag or a formula — never the `（` that wraps a
// following URL — so they are excluded from the bracket-balance prefix.
const NON_PROSE_TYPES = new Set([
  'inlineCode',
  'code',
  'link',
  'linkReference',
  'image',
  'imageReference',
  'html',
  'math',
  'inlineMath',
  'definition',
  'footnoteDefinition',
])

/**
 * Source offsets of every GFM autolink LITERAL in `content` — a bare
 * `http(s)://…` run that remark turned into a link on its own — plus a mask
 * marking every character that belongs to a non-prose node.
 *
 * Excludes `<https://…>` angle autolinks and `[text](url)` links from the
 * literal list, which already carry explicit boundaries: both can also satisfy
 * `text === url`, so the test is on the source text at the node's start, not on
 * the node shape alone.
 */
function autolinkLiteralSpans(content: string): {
  literals: Array<[number, number]>
  nonProse: Uint8Array
} {
  let tree: MdastNode
  try {
    tree = AUTOLINK_PARSER.parse(content) as unknown as MdastNode
  } catch {
    // A parse failure must not take the message down with it — the unfixed
    // render is strictly better than no render.
    return { literals: [], nonProse: new Uint8Array(content.length) }
  }
  const literals: Array<[number, number]> = []
  const nonProse = new Uint8Array(content.length)
  const visit = (node: MdastNode): void => {
    const start = node.position?.start.offset
    const end = node.position?.end.offset
    const positioned = typeof start === 'number' && typeof end === 'number'
    if (node.type === 'link' && positioned) {
      const text =
        node.children?.length === 1 && node.children[0].type === 'text' ? node.children[0].value : undefined
      if (text !== undefined && text === node.url && /^https?:\/\//.test(content.slice(start, end))) {
        // Deliberately NOT masked here. A greedy literal run can hold several
        // URLs with real prose between them (`（…/1）和【…/2】` is ONE run), and
        // that prose is where the second URL's bracket context lives. The caller
        // masks each URL's own characters as it consumes them instead.
        literals.push([start as number, end as number])
        return
      }
    }
    if (positioned && NON_PROSE_TYPES.has(node.type)) nonProse.fill(1, start, end)
    for (const child of node.children ?? []) visit(child)
  }
  visit(tree)
  return { literals, nonProse }
}

/**
 * Index in `run` where the URL demonstrably stops, or -1 when there is no
 * evidence that it does. `prefix` is the source text before the URL on its own
 * line — it decides whether a closing bracket had an opener to close.
 *
 * Two things count as evidence:
 *
 *  1. A CJK closing bracket that closes an opener SURROUNDING the URL — one left
 *     unclosed in `prefix` and not opened inside the run. This is GFM's own ASCII
 *     paren-balancing rule, generalised. `（https://x.com/a）` and
 *     `（见 https://x.com/a）` cut; `https://x.com/苹果（公司）` does not (the
 *     opener is inside the URL), and neither does `https://x.com/search?q=foo）`
 *     (nothing to close, so the bracket is plausibly part of the query).
 *  2. A SEPARATOR-class CJK punctuation mark IMMEDIATELY followed by a BACKTICK.
 *     That is the destructive case (the run eats an opening code-span delimiter
 *     and shifts every later pairing) and the backtick is the one character that
 *     cannot appear in a raw-written URL. `…/2137，`96ed647b`）` cuts.
 *
 *     Sentence-enders (`。．！？…｡`) are EXCLUDED from this rule: real page titles
 *     end in them and reach the URL raw, so `…/wiki/モーニング娘。`紹介`` must not
 *     cut. Separators like `，`、`、`、`；`、`：` do not end titles, so they stay
 *     eligible. `…/wiki/我，机器人`简介`` is still safe for a different reason —
 *     its comma is followed by more title, not by the backtick.
 *
 * Deliberately NOT covered: `…/pull/1，然后` — a bare CJK sentence continuing
 * off a URL with no space and no markup. It is character-for-character
 * indistinguishable from a legitimate `…/wiki/我，机器人`, so it keeps today's
 * behaviour rather than risking a correct link.
 */
function cjkCutIndex(run: string, prefix: string): number {
  // Openers left unclosed before the URL, per bracket type: only those have
  // something for a closer inside the run to close.
  const pending = new Map<string, number>()
  for (const ch of prefix) {
    const open = CJK_OPEN_BRACKETS.indexOf(ch)
    if (open >= 0) {
      pending.set(ch, (pending.get(ch) ?? 0) + 1)
      continue
    }
    const close = CJK_CLOSE_BRACKETS.indexOf(ch)
    if (close >= 0) {
      const opener = CJK_OPEN_BRACKETS[close]
      const n = pending.get(opener) ?? 0
      if (n > 0) pending.set(opener, n - 1)
    }
  }
  let depth = 0
  for (let i = 1; i < run.length; i++) {
    const ch = run[i]
    if (CJK_OPEN_BRACKETS.includes(ch)) {
      depth++
      continue
    }
    const close = CJK_CLOSE_BRACKETS.indexOf(ch)
    if (close >= 0) {
      if (depth > 0) {
        depth--
        continue
      }
      if ((pending.get(CJK_OPEN_BRACKETS[close]) ?? 0) > 0) return i
      // No opener to close — the bracket is plausibly part of the URL itself.
      continue
    }
    if (depth > 0 || !CJK_PUNCT_RE.test(ch)) continue
    // Sentence-enders are never a boundary — a real title can end in one. This
    // also means a mixed run like `。，` cuts at the `，`, leaving the `。` inside
    // the URL, because the loop reaches the separator on a later iteration.
    if (CJK_SENTENCE_ENDERS.includes(ch)) continue
    // Walk the contiguous punctuation run — `、，` before a backtick is one
    // boundary, not two — and require the evidence to sit directly after it.
    let end = i
    while (end < run.length && CJK_PUNCT_RE.test(run[end])) end++
    if (end < run.length && MD_ACTIVE_RE.test(run[end])) return i
  }
  return -1
}

/**
 * Whether a delimiter run with `before`/`after` as its neighbours can OPEN
 * and/or CLOSE emphasis, per CommonMark's flanking rules. Callers pass `' '` for
 * start/end of line, which the spec treats as whitespace.
 *
 * This is the load-bearing distinction: a textual count of `**`/`__` cannot tell
 * a real delimiter from a run GFM renders literally. An intraword `__`
 * (`report__final.pdf`) can neither open nor close, and a `**` with whitespace on
 * both sides (`a ** b`) is not flanking at all — treating either as a delimiter
 * truncates a URL that renders correctly today.
 */
function flankingFor(
  before: string,
  after: string,
  ch: string,
): { canOpen: boolean; canClose: boolean } {
  const wsBefore = UNICODE_WS_RE.test(before)
  const wsAfter = UNICODE_WS_RE.test(after)
  // The CJK-friendly amendment that `remark-cjk-friendly` implements — and this
  // pass must measure the SAME grammar the renderer runs — classifies a wide or
  // fullwidth character as CJK rather than as punctuation, so CJK punctuation no
  // longer blocks emphasis, and admits a CJK neighbour where CommonMark admits
  // only whitespace or punctuation. Without this, `**中文。**` reads as two
  // openers here while the renderer pairs it as one closed strong.
  const cjkBefore = CJK_WIDE_RE.test(before)
  const cjkAfter = CJK_WIDE_RE.test(after)
  const punctBefore = UNICODE_PUNCT_RE.test(before) && !cjkBefore
  const punctAfter = UNICODE_PUNCT_RE.test(after) && !cjkAfter
  const leftFlanking = !wsAfter && (!punctAfter || wsBefore || punctBefore || cjkBefore)
  const rightFlanking = !wsBefore && (!punctBefore || wsAfter || punctAfter || cjkAfter)
  // `_` additionally cannot do intraword emphasis; `*` can. That extra condition tests
  // punct-or-whitespace in the RAW sense, where CJK punctuation counts — the amendment
  // above excludes wide characters from the punctuation class used for FLANKING only.
  // Reusing the amended class here would leave a fullwidth `：` reading as neither
  // punctuation nor whitespace, so `：__url__` would look intraword and open nothing.
  if (ch === '_') {
    const rawBefore = wsBefore || UNICODE_PUNCT_RE.test(before)
    const rawAfter = wsAfter || UNICODE_PUNCT_RE.test(after)
    return {
      canOpen: leftFlanking && (!rightFlanking || rawBefore),
      canClose: rightFlanking && (!leftFlanking || rawAfter),
    }
  }
  return { canOpen: leftFlanking, canClose: rightFlanking }
}

/** Flanking for the run at `[start, end)` of `line`. */
function delimFlanking(
  line: string,
  start: number,
  end: number,
  ch: string,
): { canOpen: boolean; canClose: boolean } {
  return flankingFor(
    start > 0 ? line[start - 1] : ' ',
    end < line.length ? line[end] : ' ',
    ch,
  )
}

/**
 * Whether the character at `at` is backslash-escaped, i.e. preceded by an ODD
 * number of backslashes. `\**` is a literal asterisk followed by a lone `*` and
 * cannot be a strong delimiter, while `\\**` escapes the backslash itself and
 * leaves the `**` intact. Parity is what tells those apart.
 */
function isEscapedAt(line: string, at: number): boolean {
  let n = 0
  while (at - n - 1 >= 0 && line[at - n - 1] === '\\') n++
  return n % 2 === 1
}

/**
 * Whether the delimiter run `line[at, end)` sits between two ordinary word
 * characters — neither side whitespace, punctuation, nor CJK. Start of line and
 * end of line count as whitespace, so they are never intraword.
 */
function isIntrawordAt(line: string, at: number, end: number): boolean {
  const before = at > 0 ? line[at - 1] : ' '
  const after = end < line.length ? line[end] : ' '
  const plain = (c: string) =>
    !UNICODE_WS_RE.test(c) && !UNICODE_PUNCT_RE.test(c) && !CJK_WIDE_RE.test(c)
  return plain(before) && plain(after)
}

/**
 * Whether `line[0, upTo)` leaves a strong-emphasis opener OPEN — i.e. the author
 * was still inside a `**`/`__` when the URL started.
 *
 * CommonMark consumes delimiter CHARACTERS, not whole runs, so this counts
 * characters: an unambiguous opener adds its length, an unambiguous closer takes
 * back up to that many, and a strong opener is open when at least two characters
 * are still unmatched. Counting runs instead would call `**foo*` a pending strong
 * opener, when the lone `*` has in fact eaten one of the two and CommonMark renders
 * `*<em>foo</em>` — no strong opener survives to wrap the URL.
 *
 * A run that could be either an opener or a closer (`a**b`) makes the whole line
 * inconclusive, because a count that guesses can be wrong in both directions. A run
 * GFM would render literally — an intraword `__`, or a `**` with whitespace on both
 * sides — is not flanking at all and contributes nothing, which is why a lone `*`
 * used as prose (`2 * 3 = 6`) does not disturb the count.
 */
function hasPendingStrongOpener(line: string, upTo: number, delim: string): boolean {
  const ch = delim[0]
  let open = 0
  let i = 0
  while (i < upTo) {
    if (line[i] !== ch) {
      i++
      continue
    }
    let end = i
    while (end < line.length && line[end] === ch) end++
    if (end > upTo) break
    // An escaped first character is a literal, so the delimiter run effectively
    // starts one character later: `\**` carries no delimiter at all, `\***` carries
    // one. Flanking is then measured from that later start, whose left neighbour is
    // the literal asterisk — punctuation, which is what it renders as.
    const from = isEscapedAt(line, i) ? i + 1 : i
    if (end > from) {
      const { canOpen, canClose } = delimFlanking(line, from, end, ch)
      // An INTRAWORD run — a word character on both sides, no whitespace, no
      // punctuation, no CJK — is the shape every counter-example to this rule has
      // used (`report__final.pdf`, `?q=foo**-bar`). CommonMark lets `*` pair there,
      // but the renderer leaves such a line literal, so claiming to know the pairing
      // is how a working URL gets truncated. Treat the line as inconclusive.
      if (canOpen && canClose && isIntrawordAt(line, from, end)) return false
      // A run that can do both otherwise is the ordinary shape in CJK prose (`：**`,
      // `。**`). The renderer resolves it the way a delimiter stack does: close an
      // opener when one is waiting, otherwise open.
      if (canClose && open >= delim.length) open -= Math.min(end - from, open)
      else if (canOpen) open += end - from
    }
    i = end
  }
  return open >= delim.length
}

/**
 * Index in `run` of a strong-emphasis delimiter the author wrote to CLOSE an
 * opener that sits before the URL, or -1 when there is none.
 *
 * This evidence is structural rather than lexical: it does not claim to know
 * where the URL ended, it observes that the current reading is one no author
 * writes — a delimiter PAIR wrapping the URL, whose closing half GFM has
 * swallowed into the href. Leaving it there costs the emphasis its delimiter, so
 * the opener degrades to two literal asterisks and every later `**` in the
 * paragraph re-pairs against the wrong partner.
 *
 * BOTH ends must be real, and both are judged against the text the author wrote:
 * the prefix must leave an opener open (hasPendingStrongOpener) AND the candidate
 * inside the run must itself be a legitimate closer. Checking only the opener is
 * not enough — `__See https://example.com/a__b for details__` opens a real `__`
 * and then hits an INTRAWORD `__` in the path, which closes nothing, so cutting
 * there truncates a correct link and the emphasis stays open anyway.
 *
 * The candidate must also be followed by CJK PUNCTUATION — the same class the two
 * rules above already act on. Flanking cannot carry this: in `?q=foo**-bar` the
 * `**` has a word character before it and punctuation after it, which is exactly
 * the shape of a real closer before punctuation, and a candidate followed by a
 * fullwidth mark is right-flanking by construction (a run holds no whitespace, and
 * the mark itself is punctuation), so a closer check on it can never refuse
 * anything. The requirement is deliberately narrower than "any non-ASCII": an
 * ideograph is legal mid-path (`?q=a**中文`), so treating one as a boundary would
 * truncate a working URL.
 *
 * Consequences worth knowing, both accepted:
 *  - An all-ASCII paragraph is never cut, and neither is `**url**已合并` (an
 *    ideograph, not punctuation, follows the delimiter). This pass carries `Cjk` in
 *    its name; the shape it is for is `**url**（…` / `**url**，…`.
 *  - A URL whose path genuinely carries a fullwidth mark straight after a `**`
 *    (`…/wiki/苹果**（公司）`) would be cut short. That is the SAME residual risk
 *    rules 1 and 2 already accept, on the same character class.
 *
 * A trailing delimiter never reaches here: GFM trims a trailing `*`/`_` off the
 * autolink literal, so `**https://x.com/a**` — which renders correctly today —
 * yields a node whose source stops at `a`, with no delimiter inside the run.
 */
function strongDelimCutIndex(run: string, prefix: string, suffix: string): number {
  // Flanking is decided by a delimiter's NEIGHBOURS, so the prefix alone is not
  // enough context: the character after a prefix-terminal `**` is the URL's
  // first character.
  const line = prefix + run
  let best = -1
  for (const delim of STRONG_DELIMS) {
    if (!hasPendingStrongOpener(line, prefix.length, delim)) continue
    // If the prose after the URL still has a delimiter available to close that
    // opener, the author's pair spans the URL and the `**` inside it is part of the
    // URL. Cutting there would truncate the href AND orphan the real closer.
    if (hasUnmatchedCloserAfter(suffix, delim)) continue
    for (let at = run.indexOf(delim); at > 0; at = run.indexOf(delim, at + 1)) {
      // An escaped delimiter closes nothing, so it is no evidence of a boundary.
      // Skipping rather than adjusting is enough here: the next iteration starts one
      // character later, which is exactly the run `\***` leaves behind.
      if (isEscapedAt(line, prefix.length + at)) continue
      const after = at + delim.length < run.length ? run[at + delim.length] : ' '
      if (!CJK_PUNCT_RE.test(after)) continue
      if (best < 0 || at < best) best = at
      break
    }
  }
  return best
}

/**
 * The earliest boundary any evidence rule can prove, or -1. Rules are
 * independent: each one alone is enough, and the shortest URL among them is the
 * conservative choice.
 */
function earliestCut(run: string, prefix: string, suffix: string): number {
  const cuts = [cjkCutIndex(run, prefix), strongDelimCutIndex(run, prefix, suffix)].filter(
    (i) => i > 0,
  )
  return cuts.length > 0 ? Math.min(...cuts) : -1
}

function isAutolinkableHost(head: string): boolean {
  const m = AUTOLINKABLE_HOST_RE.exec(head)
  if (!m) return false
  // GFM: `_` is not allowed in either of the last two domain labels.
  return m[1].split('.').slice(-2).every((label) => !label.includes('_'))
}

/**
 * Drop the trailing characters GFM strips from an autolink literal but an angle
 * autolink would keep, so `…/1.，`b`` links `…/1` and leaves `.` as prose.
 */
function trimGfmAutolinkTail(s: string): string {
  let out = s
  for (let guard = 0; guard < s.length; guard++) {
    const next = out.replace(/[?!.,:*_~]+$/, '')
    if (next.endsWith(')')) {
      const open = (next.match(/\(/g) ?? []).length
      const close = (next.match(/\)/g) ?? []).length
      // GFM keeps a `)` that closes a `(` from inside the URL itself.
      if (close > open) {
        out = next.slice(0, -1)
        continue
      }
    }
    if (next === out) return out
    out = next
  }
  return out
}

/**
 * The PROSE text before `at` on its own line, with every non-prose character
 * blanked out. Only this text can supply the opener a closing bracket inside the
 * URL closes — a `（` sitting in an earlier URL's query string, a code sample or
 * an HTML attribute is not bracket context for the URL that follows.
 *
 * Line-scoped on purpose: a paragraph-wide scan would be less conservative, and
 * a cut is the risky direction.
 */
function prosePrefix(content: string, nonProse: Uint8Array, at: number): string {
  const lineStart = content.lastIndexOf('\n', at - 1) + 1
  let out = ''
  for (let i = lineStart; i < at; i++) out += nonProse[i] ? ' ' : content[i]
  return out
}

/** `prosePrefix`'s mirror: the prose from `at` to the end of that line. */
function proseSuffix(content: string, nonProse: Uint8Array, at: number): string {
  let lineEnd = content.indexOf('\n', at)
  if (lineEnd < 0) lineEnd = content.length
  let out = ''
  for (let i = at; i < lineEnd; i++) out += nonProse[i] ? ' ' : content[i]
  return out
}

/**
 * Whether the prose AFTER the URL still offers a delimiter that could close the
 * opener waiting from before it — i.e. the author's pair is `**prose … prose**`
 * and the `**` inside the URL is part of the URL.
 *
 * Parity is the whole point, and it is what makes this usable where a plain
 * "is there another `**` later" test is not: in
 * `已建好：**url**（revision 1），说明见 **文档**。` the two trailing delimiters pair
 * with EACH OTHER, so none is left over for the opener, and the boundary inside
 * the run really is the only reading that closes it. In `**See url**（x） for
 * details**` the single trailing delimiter has no partner, so it is the closer and
 * the run's `**` belongs to the URL.
 */
function hasUnmatchedCloserAfter(suffix: string, delim: string): boolean {
  const ch = delim[0]
  let open = 0
  let i = 0
  while (i < suffix.length) {
    if (suffix[i] !== ch) {
      i++
      continue
    }
    let end = i
    while (end < suffix.length && suffix[end] === ch) end++
    const from = isEscapedAt(suffix, i) ? i + 1 : i
    if (end - from >= delim.length) {
      const { canOpen, canClose } = delimFlanking(suffix, from, end, ch)
      // Nothing local is waiting, so a closer here can only be closing the opener
      // that sits before the URL.
      if (canClose && open < delim.length) return true
      if (canClose) open -= Math.min(end - from, open)
      else if (canOpen) open += end - from
    }
    i = end
  }
  return false
}

/**
 * Close a bare `http(s)://` run whose boundary is provable — CJK punctuation
 * that could not be part of the URL, or a strong-emphasis delimiter swallowed
 * out of the surrounding markup — by re-emitting its head as an angle autolink.
 * Returns `content` unchanged when there is no such evidence.
 *
 * NOT safe to run when `data-sourcepos` is in play: it inserts two characters
 * per fixed URL, which shifts every later column on that line and would
 * mis-anchor an inline comment. Callers gate on that (see MarkdownBlock).
 */
export function fixCjkAutolinkBoundaries(content: string): string {
  if (!content.includes('://')) return content
  if (!CJK_PUNCT_RE.test(content) && !STRONG_DELIM_RE.test(content)) return content
  const { literals, nonProse } = autolinkLiteralSpans(content)
  const inserts: Array<[number, string]> = []
  for (const [start, end] of literals) {
    // Everything of this node already accounted for. A `https://` nested in the
    // URL's own path (`?u=https://…`) must not be cut separately — that would
    // corrupt the outer URL and emit out-of-order inserts.
    let consumedTo = start
    URL_START_RE.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = URL_START_RE.exec(content.slice(start, end))) !== null) {
      const at = start + m.index
      if (at < consumedTo) continue
      const run = URL_RUN_AT_RE.exec(content.slice(at, end))?.[0] ?? ''
      const cut = earliestCut(
        run,
        prosePrefix(content, nonProse, at),
        proseSuffix(content, nonProse, at + run.length),
      )
      if (cut < 0) {
        // The whole run is one URL — mask it, so a bracket in its query string
        // cannot pose as prose context for a later URL.
        nonProse.fill(1, at, at + run.length)
        consumedTo = at + run.length
        continue
      }
      const head = trimGfmAutolinkTail(run.slice(0, cut))
      if (!isAutolinkableHost(head)) {
        nonProse.fill(1, at, at + run.length)
        consumedTo = at + run.length
        continue
      }
      inserts.push([at, '<'], [at + head.length, '>'])
      // Resume right after the head: a second URL inside the same autolink node
      // (`（https://a/1）和【https://b/2】` is ONE run) still needs its own
      // boundary. Only the head just consumed is masked — the text between the
      // two URLs is real prose, and it is where the next bracket's opener lives.
      nonProse.fill(1, at, at + head.length)
      consumedTo = at + head.length
    }
  }
  if (inserts.length === 0) return content
  let out = ''
  let pos = 0
  for (const [at, ch] of inserts) {
    out += content.slice(pos, at) + ch
    pos = at
  }
  return out + content.slice(pos)
}

export function fixCodeFences(s: string): string {
  // Escape bare "N." lines so markdown doesn't render them as ordered lists.
  // CommonMark: 0-3 leading spaces = list item, 4+ = indented code block.
  // Tracks backtick and tilde fences with length matching per CommonMark spec.
  let inFence = false
  let fenceMarker = ''
  s = s.replace(/^( {0,3}(```+|~~~+)[\w+#-]*.*|( {0,3}\d+)\.([ \t\r]*))$/gm, (match, _, fence, num, trail) => {
    if (fence) {
      if (!inFence) { inFence = true; fenceMarker = fence }
      else if (
        fence[0] === fenceMarker[0] &&
        fence.length >= fenceMarker.length &&
        /^[ \t\r]*$/.test(match.slice(match.indexOf(fence) + fence.length))
      ) { inFence = false }
      return match
    }
    if (inFence || num === undefined) return match
    return num + '\\.' + trail
  })
  // Ensure blank line before opening fences that are glued to preceding text
  s = s.replace(/([^\n])(\n?)(```\w*\n)/g, (_, pre, nl, fence) =>
    nl ? pre + nl + fence : pre + '\n\n' + fence
  )
  // Split closing fences glued to trailing text: ```358KB → ```\n358KB
  // Preserves valid opening fences (```diff, ```json5, ```c++) via negative lookahead
  s = s.replace(/^(```)(?![a-zA-Z][\w+#-]*\s*$)(.+)$/gm, '$1\n$2')
  // Split opening fences glued to uppercase text
  s = s.replace(/```([A-Z])/g, '```\n$1')
  return s
}

const MCWIDGET_STRIP_RE = /<mcwidget[\s\S]*?<\/mcwidget>|<mcwidget[\s\S]*$/g

// Anthropic tool-use protocol markup occasionally leaks into the visible
// text stream (model emits a literal `<tool_use>...</tool_use>` block alongside
// the real ACP tool call). The wrapper element is unknown to the markdown
// renderer, so the JSON body — including its escaped `\n` literals — collapses
// into a single unbroken paragraph, fragmenting the surrounding markdown.
// Mirror MCWIDGET_STRIP_RE: catch complete tag pairs and unclosed openers
// (mid-stream).
const TOOL_USE_STRIP_RE = /<tool_use[\s\S]*?<\/tool_use>|<tool_use[\s\S]*$/g

/**
 * Strip stray protocol tags (`<mcwidget>`, `<tool_use>`) that leak through to
 * a markdown block during streaming transitions, while preserving any tag
 * mentions that appear inside inline-code spans (e.g. when the agent is
 * documenting the syntax).
 *
 * Builds a per-line inline-code mask, runs the strip regex against the masked
 * text to find ranges, then splices those ranges out of the original content.
 * Mask preserves offsets so match indices are valid against the original.
 *
 * `openMarker` is a fast-path substring check to skip work when the tag is
 * not present at all. `stripRe` is the actual matcher; it must be a global
 * regex with sticky-safe semantics (advance lastIndex on zero-length match).
 */
function stripStrayTags(content: string, openMarker: string, stripRe: RegExp): string {
  if (!content.includes(openMarker)) return content
  const masked = content.split('\n').map(l => maskInlineCode(l)).join('\n')
  if (!masked.includes(openMarker)) return content
  const ranges: Array<[number, number]> = []
  stripRe.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = stripRe.exec(masked)) !== null) {
    ranges.push([m.index, m.index + m[0].length])
    if (m[0].length === 0) stripRe.lastIndex++
  }
  if (ranges.length === 0) return content
  let out = ''
  let pos = 0
  for (const [start, end] of ranges) {
    out += content.slice(pos, start)
    pos = end
  }
  out += content.slice(pos)
  return out
}

const stripStrayWidgetTags = (content: string) => stripStrayTags(content, '<mcwidget', MCWIDGET_STRIP_RE)
const stripStrayToolUseTags = (content: string) => stripStrayTags(content, '<tool_use', TOOL_USE_STRIP_RE)

// A GFM table delimiter row, e.g. `| --- | :--: |` or `---|---`. remark-gfm
// only promotes the preceding header line to a <table> once this row is present.
const TABLE_DELIM_RE = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/

/**
 * While STREAMING, withhold an incomplete trailing table so it never paints as
 * literal pipe text that later reflows into a <table>.
 *
 * remark-gfm needs BOTH a header row and a `|---|` delimiter row to recognize a
 * table. Mid-stream the header arrives first and renders as a <p> containing
 * literal "| A | B |"; when the delimiter row streams in, that paragraph
 * RESTRUCTURES into a bordered table — a visible structural snap of
 * already-shown content (see MarkdownRenderer.streamingTableSnap.test.tsx).
 * This defers the trailing header run (mirroring how an incomplete fenced code
 * block is held) until the delimiter arrives, so the transition the user sees
 * is the standard "content appears", not "paragraph morphs into a table".
 *
 * Scoped narrowly to avoid hiding ordinary prose: only a run of trailing
 * non-blank lines whose FIRST line is a bordered table header (starts with `|`)
 * is a candidate, and only when that run does NOT yet contain a delimiter row
 * (a `---` row that actually carries a `|`). A run that already has such a
 * delimiter is a real (possibly still growing) table and is left to render.
 *
 * Scoping choices (both close known edge cases):
 *  - Require the first line to START with `|`. A looser "≥2 pipes" test also
 *    matched ordinary prose (e.g. a line with an inline `` `cmd | grep | wc` ``)
 *    and would withhold that whole paragraph for the rest of the stream. Models
 *    emit bordered tables (`| a | b |`), so start-with-`|` keeps the real case
 *    while excluding prose; a borderless table simply isn't deferred (it never
 *    regressed anything — it just renders as before).
 *  - The delimiter must contain a `|`. A bare `---` is a thematic break / setext
 *    underline, NOT a GFM table delimiter (which needs matching pipe-separated
 *    cells), so counting it as "already a table" would wrongly skip deferral and
 *    let the snap happen.
 */
function deferIncompleteStreamingTable(content: string): string {
  const lines = content.split('\n')
  let start = lines.length
  while (start > 0 && lines[start - 1].trim() !== '') start--
  if (start >= lines.length) return content // trailing blank line / nothing to defer
  const run = lines.slice(start)
  if (!/^\s*\|/.test(run[0])) return content // not a bordered table header
  // A real GFM delimiter row carries at least one pipe; a bare `---` does not.
  if (run.some((l) => l.includes('|') && TABLE_DELIM_RE.test(l))) return content
  return lines.slice(0, start).join('\n')
}

const MarkdownBlock = memo(function MarkdownBlock({ content, sourcePos, startLine, glow, smooth, softBreaks, live, unfurl }: { content: string; sourcePos?: boolean; startLine?: number; glow?: boolean; smooth?: boolean; softBreaks?: boolean; live?: boolean; unfurl?: boolean }) {
  // Declared before the early return below — Rules of Hooks.
  //
  // `sourcePos` force-disables unfurl: the inline-commenting flow maps a DOM
  // selection back to source coordinates through `data-sourcepos`, and a card
  // REPLACES the `<p>` that carries it, so a standalone link would become an
  // uncommentable hole. The two are mutually exclusive in practice today (only
  // the chat transcript enables previews, and it renders without sourcepos) —
  // this makes that a guarantee instead of a coincidence.
  const unfurlCtx = useMemo<LinkUnfurl>(
    () => ({ enabled: !!unfurl && !sourcePos, live: !!live }),
    [unfurl, sourcePos, live],
  )
  // Strip any <mcwidget> or <tool_use> tags that leak through during
  // streaming transitions or when the agent emits protocol markup as text.
  // Both passes preserve mentions inside inline-code spans.
  let clean = stripStrayToolUseTags(stripStrayWidgetTags(content))
  // `glow` marks the live streaming tail block: while streaming, hold back an
  // incomplete trailing table so it doesn't paint as pipe text then snap into a
  // <table> when the delimiter row arrives.
  if (glow) clean = deferIncompleteStreamingTable(clean)
  if (!clean.trim()) return null
  const baseRehype = sourcePos ? REHYPE_PLUGINS_WITH_SOURCEPOS : REHYPE_PLUGINS
  // Streaming tail block only (see MarkdownRenderer's `glow` prop):
  //   - in immediate mode: append the glow plugin for trailing-word shimmer;
  //   - in smooth mode: append the reveal plugin for per-char fade entrance.
  let rehypePlugins: PluggableList = baseRehype
  if (glow) {
    const tail: PluggableList = []
    // Inline caret first, so the glow/reveal plugins still see (and animate)
    // the trailing text node that the caret is inserted after.
    tail.push(rehypeStreamingCaret)
    if (!smooth) tail.push([rehypeStreamingGlow, { tailChars: GLOW_TAIL_CHARS }])
    if (smooth) tail.push(rehypeStreamingReveal)
    rehypePlugins = [...baseRehype, ...tail]
  }
  // `fixCodeFences` runs FIRST: its later passes CREATE code blocks the raw
  // source did not have (blank line before a fence glued to preceding text,
  // splitting a closing fence glued to trailing text). Rewriting boundaries
  // before that would judge such a region as prose and leave a literal `<…>`
  // inside what ends up displayed as code.
  //
  // `sourcePos` mode maps a DOM selection back to source coordinates through
  // `data-sourcepos` for inline commenting. fixCjkAutolinkBoundaries inserts two
  // characters per fixed URL, which shifts every later column on that line and
  // would anchor a comment to the wrong occurrence — so that surface keeps the
  // unfixed (but coordinate-accurate) render.
  const fenced = fixCodeFences(clean)
  const prepared = sourcePos ? fenced : fixCjkAutolinkBoundaries(fenced)
  const md = (
    <MdSourceCtx.Provider value={prepared}>
      <ReactMarkdown remarkPlugins={softBreaks ? REMARK_PLUGINS_WITH_BREAKS : REMARK_PLUGINS} rehypePlugins={rehypePlugins} urlTransform={urlTransform} components={MD_COMPONENTS}>
        {prepared}
      </ReactMarkdown>
    </MdSourceCtx.Provider>
  )
  const body = sourcePos ? <div data-block-start={startLine ?? 1}>{md}</div> : md
  // The provider carries no DOM node, so sourcepos / lightbox scoping upstream
  // is unaffected. It is the only way MdAnchor / MdParagraph — which react-markdown
  // instantiates deep inside its own tree — can see the gate.
  return <LinkUnfurlCtx.Provider value={unfurlCtx}>{body}</LinkUnfurlCtx.Provider>
})

import WidgetFrame from './WidgetFrame'
import WidgetPlaceholder from './WidgetPlaceholder'

import { i18nT } from '../i18n/t'
/** Try to extract a file path from chat text immediately preceding a diff
 * block. Tools sometimes emit "Created /path/to/file:" or "Modified ..."
 * before a bare diff with no +++/--- headers; this hint lets DiffBlock's
 * Open file button work in those cases.
 */
function extractPathHintFromText(text: string | undefined): string | undefined {
  if (!text) return undefined
  // Last non-empty line before the diff is the most likely carrier of
  // "Created /path:" or "Edited /path:" — scan a few lines back rather
  // than the whole block, to keep this cheap and avoid false positives.
  const lines = text.trimEnd().split('\n').slice(-5)
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim().replace(/[:.,]+$/, '')
    if (!line) continue
    // Patterns we accept:
    //   Created /abs/path
    //   Modified /abs/path
    //   Wrote /abs/path
    //   Updated /abs/path
    //   /abs/path        (bare absolute path)
    //   ~/relative/path  (home-relative)
    //   `/abs/path`      (backtick-wrapped)
    const stripped = line.replace(/^`|`$/g, '')
    const m = /(?:Created|Modified|Wrote|Updated|Edited|Saved|File|Path)?\s*[:\s]?\s*`?(\/[^\s`]+|~\/[^\s`]+)`?/i.exec(stripped)
    if (m && m[1]) return m[1]
  }
  return undefined
}

function BlockRenderer({ block, prevBlock, onFileOpen, sourcePos, messageTs, widgetIndex, slotKey, glow, smooth, softBreaks, live, unfurl }: { block: ContentBlock; prevBlock?: ContentBlock; onFileOpen?: (path: string) => void; sourcePos?: boolean; messageTs?: string; widgetIndex?: number; slotKey?: string; glow?: boolean; smooth?: boolean; softBreaks?: boolean; live?: boolean; unfurl?: boolean }) {
  switch (block.type) {
    case 'diff': {
      const pathHint = prevBlock?.type === 'markdown'
        ? extractPathHintFromText(prevBlock.content)
        : undefined
      const node = <DiffBlock code={block.content} complete={block.complete} onFileOpen={onFileOpen} pathHint={pathHint} streaming={!!smooth && !block.complete} />
      // Smooth mode: wrap so the block height eases as lines arrive. The wrapper
      // is mounted for the whole message lifecycle (smooth is constant) so the
      // child never remounts when streaming flips to complete.
      return smooth ? <SmoothResize enabled={!block.complete}>{node}</SmoothResize> : node
    }
    case 'mermaid':
      return block.complete ? <MermaidBlock code={block.content} /> : (
        <div className="my-2 p-3 bg-bg-elevated border border-border rounded-md text-muted text-[12px] italic animate-pulse">{i18nT('components.markdownRenderer.generating_diagram')}</div>
      )
    case 'excalidraw':
      // Held back until the fence closes: a half-streamed scene is invalid JSON,
      // so attempting to draw it would only flash the raw-source fallback.
      return block.complete ? <ExcalidrawBlock code={block.content} /> : (
        <div className="my-2 p-3 bg-bg-elevated border border-border rounded-md text-muted text-[12px] italic animate-pulse">{i18nT('components.markdownRenderer.generating_diagram')}</div>
      )
    case 'code': {
      const node = <EditableCodeBlock code={block.content} lang={block.language} complete={block.complete} />
      // Height-grow only — streaming code renders as one plain <pre> text node
      // so per-line content animation isn't applied here.
      return smooth ? <SmoothResize enabled={!block.complete}>{node}</SmoothResize> : node
    }
    case 'widget':
      return block.complete
        ? <WidgetFrame html={block.content} title={block.language} slug={block.slug} messageTs={messageTs} widgetIndex={widgetIndex} slotKey={slotKey} />
        : <WidgetPlaceholder title={block.language} />
    case 'markdown':
      // `live` = this block is the streaming tail (see MarkdownRenderer). ORed
      // with the block's own `complete` flag so a provisional block is treated
      // as live too, whatever produced it.
      return <MarkdownBlock content={block.content} sourcePos={sourcePos} startLine={block.startLine} glow={glow} smooth={smooth} softBreaks={softBreaks} live={!block.complete || !!live} unfurl={unfurl} />
  }
}

export default memo(function MarkdownRenderer({ content, streaming = false, onFileOpen, onFolderOpen, onArtifactOpen, rawMode = false, sourcePos = false, messageTs, slotKey, glow = false, smooth, softBreaks = false, compactImages = false, linkPreviews = false }: { content: string; streaming?: boolean; onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void; onFolderOpen?: (path: string) => void; onArtifactOpen?: (slug: string) => void; rawMode?: boolean; sourcePos?: boolean; messageTs?: string; slotKey?: string; glow?: boolean; smooth?: boolean; softBreaks?: boolean; compactImages?: boolean; linkPreviews?: boolean }) {
  const blocks = useBlockAssembler(content, streaming)

  /** Chip activation lives on the chip itself (see InlineCode); this handler is
   *  only the artifact-link delegation it has always been. */
  const handleClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const el = e.target as HTMLElement
    // e.target may be an inline child of the `/artifacts/<slug>` anchor (e.g.
    // <em>/<code>), so walk up with closest(). preventDefault stops the
    // relative href from navigating full-page instead of opening the panel.
    if (onArtifactOpen && !e.shiftKey) {
      const anchor = el.closest('a[href^="/artifacts/"]') as HTMLAnchorElement | null
      if (anchor) {
        const slug = artifactSlugFromHref(anchor.getAttribute('href'))
        if (slug) {
          e.preventDefault()
          onArtifactOpen(slug)
          return
        }
      }
    }
  }, [onArtifactOpen])

  /** Stable identity so every chip in a long transcript doesn't re-render when
   *  this component does. */
  const pathActions = useMemo<PathActions>(() => ({ onFileOpen, onFolderOpen }), [onFileOpen, onFolderOpen])

  // Pre-compute the widget index for each widget block (0-based ordinal of
  // widgets within this message). WidgetFrame uses (messageTs, widgetIndex)
  // to derive a stable slug when the agent didn't emit an explicit one, so
  // bookmark state survives refreshes and prevents save→refresh duplicates.
  // Memoized so each BlockRenderer gets a stable widgetIndex reference
  // between renders, so it doesn't defeat memo() if anyone later wraps
  // BlockRenderer.
  //
  // Must run before any conditional return — Rules of Hooks. (rawMode flips
  // via a settings toggle which usually re-mounts this component anyway,
  // but we keep hook order strict for safety.)
  const widgetIndices = useMemo(() => {
    const out: number[] = new Array(blocks.length).fill(-1)
    let n = 0
    for (let i = 0; i < blocks.length; i++) {
      if (blocks[i].type === 'widget') { out[i] = n; n++ }
    }
    return out
  }, [blocks])

  // Index of the last markdown block — the streaming tail that gets the glow
  // (only when `glow` is set). -1 if the message ends in a non-markdown block.
  const lastMarkdownIdx = useMemo(() => {
    for (let i = blocks.length - 1; i >= 0; i--) if (blocks[i].type === 'markdown') return i
    return -1
  }, [blocks])

  // Settle the reveal edge once the content stops changing, and NEVER un-settle
  // it. One-way is the whole point: `.ft-word` spans persist across chunks
  // (hast-util-to-jsx-runtime keys element children by per-parent ordinal, and
  // `--ft-o` is a function of the span's slot, not of the character), so
  // REMOVING `.ft-idle` would transition the entire 32-character edge from the
  // settled 1 back down to `--ft-o` — an inverse of the fade-in #697 built, in
  // the same pixels. Pre-paint clearing cannot avoid that either, because a
  // transition starts from the previously COMPUTED style, not the last painted
  // frame. Making the settle one-way removes the downward transition by
  // construction: a character's opacity only ever rises.
  //
  // The cost is deliberate: after the first stall the rest of that row renders
  // at full opacity with no reveal. A latched class is harmless once streaming
  // ends — the spans only exist while `glow` is set, and the class's effect is
  // full opacity, which is the correct end state anyway.
  //
  // Skipped entirely when `smooth` is off: `.ft-idle` is inert there, and this
  // component has ~15 non-streaming call sites.
  const [revealIdle, setRevealIdle] = useState(false)
  useEffect(() => {
    if (!smooth || revealIdle) return
    const t = setTimeout(() => setRevealIdle(true), REVEAL_IDLE_SETTLE_MS)
    return () => clearTimeout(t)
  }, [content, smooth, revealIdle])

  if (rawMode) {
    return <pre className="text-[13px] font-mono whitespace-pre-wrap break-words leading-relaxed text-muted">{content}</pre>
  }

  // Root class drives the per-char entrance keyframe (.ft-word descendants
  // only exist in the streaming tail block, so this is inert otherwise).
  // ft-streaming scopes the animation to live streaming so history/scroll
  // re-mounts don't re-fade.
  const animOn = !!smooth
  const animClass = animOn ? ' ft-anim-smooth' : ''
  // `ft-idle` is folded into streamClass rather than interpolated separately so
  // the root element below stays byte-identical to base. The repo's
  // accessible-interactive-elements rule greps ADDED lines for a non-role div or
  // span carrying a click handler (check-added: true), so merely re-touching that
  // line trips a WCAG-affordance gate even though this change adds no affordance
  // -- the element and its handler are untouched.
  const streamClass =
    (animOn && streaming ? ' ft-streaming' : '') +
    (animOn && revealIdle ? ' ft-idle' : '')

  return (
    // Presentational content wrapper for rendered markdown blocks. The onClick is
    // pure event delegation for `/artifacts/<slug>` links only — path chips bind
    // their own handlers (see InlineCode), so this wrapper is not an interactive
    // control and carries no role.
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
    <div className={`group${animClass}${streamClass}`} onClick={handleClick} data-image-scope="">
      {/* PathProbeCtx: suppress path stat probes while the message is still
          streaming, so partial paths ('/Users' en route to '/Users/me/x.ts')
          neither burn requests nor flash the wrong affordance.
          PathActionCtx: where a confirmed chip sends its click — MD_COMPONENTS is
          module-level, so the renderer cannot pass these down as props. */}
      <PathProbeCtx.Provider value={!streaming}>
      <PathActionCtx.Provider value={pathActions}>
      {/* CompactImagesCtx: user-message ("sent prompt") callers pass compactImages
          so their attached images render as small previews. The provider wraps the
          blocks here (a context Provider renders no DOM node, so data-image-scope /
          lightbox scoping on the div above is unaffected) and lives in this module
          so a caller that mocks it in tests never needs to re-export the context. */}
      <CompactImagesCtx.Provider value={compactImages}>
      {/* ImageVersionCtx: scopes local image URLs to this message so an agent
          rewriting one file across turns is not served the previous bytes from
          the in-document resource cache. */}
      <ImageVersionCtx.Provider value={messageTs ?? null}>
        {blocks.map((block, i) => (
          // Key on startLine (stable across streaming) instead of block.type, so
          // a code -> diff reclassification mid-stream doesn't unmount the
          // in-progress component. Falls back to index for blocks without a
          // startLine (e.g. extracted widgets). The "idx-" prefix avoids
          // collision with real startLine numbers.
          <BlockRenderer
            key={block.startLine != null ? `line-${block.startLine}` : `idx-${i}`}
            block={block} prevBlock={blocks[i - 1]} onFileOpen={onFileOpen} sourcePos={sourcePos}
            messageTs={messageTs}
            widgetIndex={widgetIndices[i] >= 0 ? widgetIndices[i] : undefined}
            slotKey={slotKey}
            glow={glow && i === lastMarkdownIdx}
            // Same gate `glow` uses — the last markdown block of a streaming
            // message IS the live tail. Reusing it means the unfurl suppression
            // and the shimmer can never disagree about which block is still
            // being typed. `streaming` rather than `glow` because a caller may
            // render a streaming transcript without asking for the shimmer.
            live={streaming && i === lastMarkdownIdx}
            unfurl={linkPreviews}
            smooth={smooth}
            softBreaks={softBreaks}
          />
        ))}
      </ImageVersionCtx.Provider>
      </CompactImagesCtx.Provider>
      </PathActionCtx.Provider>
      </PathProbeCtx.Provider>
    </div>
  )
})

type LightboxImage = { src: string; alt: string }
type LightboxDetail = { images: LightboxImage[]; index: number }

/** Lightbox zoom (enlarge) bounds. `1` is fit-to-screen; each step scales the
 *  fit box up so the image can overflow the viewport and be panned via the
 *  scrollable overlay. */
const LIGHTBOX_ZOOM_MIN = 1
const LIGHTBOX_ZOOM_MAX = 5
const LIGHTBOX_ZOOM_STEP = 0.5

/** True when a keyboard event originates from an editable element, so global
 *  printable-key shortcuts (like the lightbox 'd' download) don't hijack typing. */
function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || typeof el.tagName !== 'string') return false
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable === true
}

/** Derive a download filename for a lightbox image. Local images are served
 *  as `/api/file-raw?path=<abs>`, so prefer the basename of that path; for
 *  other URLs fall back to the pathname basename, then the alt text. */
function lightboxFilename(image: LightboxImage): string {
  try {
    const u = new URL(image.src, window.location.href)
    const p = u.searchParams.get('path')
    const fromPath = p ? p.split(/[\\/]/).pop() : ''
    if (fromPath) return fromPath
    const fromName = u.pathname.split('/').pop()
    if (fromName && fromName.includes('.')) return decodeURIComponent(fromName)
  } catch {
    // image.src is not a parseable URL (e.g. a bare data: payload) -- fall through.
  }
  const altName = (image.alt || '').trim().replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '')
  return altName || 'image'
}

/** Download the given lightbox image to the user's machine. Fetches the
 *  already-served bytes (same-origin for /api/file-raw, or data:/blob:) into a
 *  blob and triggers a browser download. If the fetch is blocked (e.g. a
 *  cross-origin remote image with no CORS), falls back to opening the image in
 *  a new tab so the user can save it manually. */
async function downloadLightboxImage(image: LightboxImage): Promise<void> {
  const name = lightboxFilename(image)
  try {
    const res = await fetch(image.src)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    const objUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objUrl
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(objUrl), 1000)
  } catch {
    window.open(image.src, '_blank', 'noopener,noreferrer')
  }
}

/** Build the lightbox payload for an image click. The set is "all images
 *  inside the nearest [data-image-scope] ancestor"; for markdown messages
 *  that's a MarkdownRenderer instance (one per chat message), and for the
 *  chat-input thumbnail strip it's the strip's outer div. */
export function dispatchLightbox(target: HTMLImageElement): void {
  const scope = target.closest('[data-image-scope]') as HTMLElement | null
  let detail: LightboxDetail = { images: [{ src: target.src, alt: target.alt }], index: 0 }
  if (scope) {
    const els = Array.from(scope.querySelectorAll<HTMLImageElement>('img[data-lightbox-image]'))
    if (els.length > 0) {
      detail = {
        images: els.map(el => ({ src: el.src, alt: el.alt })),
        index: Math.max(0, els.indexOf(target)),
      }
    }
  }
  window.dispatchEvent(new CustomEvent('lightbox', { detail }))
}

/** Lightbox overlay -- mount once in the app, listens for 'lightbox' custom
 *  events. Escape closes; ArrowLeft/ArrowRight navigate within the image set
 *  (clamped at the ends). Accepts both the structured { images, index }
 *  payload and the legacy { src, alt } single-image shape. */
export function Lightbox() {
  const [state, setState] = useState<LightboxDetail | null>(null)
  // Zoom (enlarge) factor for the current image. 1 = fit-to-screen; larger
  // values scale the fit box up so the image overflows into the scrollable
  // overlay. Reset to 1 whenever the shown image changes (see effect below).
  const [zoom, setZoom] = useState(1)
  const zoomIn = useCallback(() => setZoom(z => Math.min(LIGHTBOX_ZOOM_MAX, +(z + LIGHTBOX_ZOOM_STEP).toFixed(2))), [])
  const zoomOut = useCallback(() => setZoom(z => Math.max(LIGHTBOX_ZOOM_MIN, +(z - LIGHTBOX_ZOOM_STEP).toFixed(2))), [])
  // Pan offset (px) for dragging an enlarged image around. Only meaningful when
  // zoom > 1. The image element itself is the drag surface; a small movement
  // threshold distinguishes a pan-drag from a click (which steps the zoom).
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const imgRef = useRef<HTMLImageElement>(null)
  const dragRef = useRef({ startX: 0, startY: 0, baseX: 0, baseY: 0, moved: 0, active: false, dragging: false })
  const [dragging, setDragging] = useState(false)
  // Live zoom for the pointer/clamp closures (avoids stale-closure math in the
  // fire-and-forget pointer handlers and the post-layout re-clamp effect).
  const zoomRef = useRef(zoom)
  zoomRef.current = zoom
  // Clamp a candidate pan so the image can't be flung entirely off-screen. Zoom
  // is applied as a CSS `scale()` transform, so the *visual* size is the layout
  // box (offsetWidth/Height) times the current zoom; travel is allowed up to
  // half that overflow beyond the viewport.
  const clampPan = useCallback((x: number, y: number) => {
    const el = imgRef.current
    if (!el) return { x, y }
    const z = zoomRef.current
    const maxX = Math.max(0, (el.offsetWidth * z - window.innerWidth) / 2)
    const maxY = Math.max(0, (el.offsetHeight * z - window.innerHeight) / 2)
    return { x: Math.min(maxX, Math.max(-maxX, x)), y: Math.min(maxY, Math.max(-maxY, y)) }
  }, [])
  // End a drag on either pointerup OR pointercancel (touch/pen interrupted, or
  // capture lost) so `active`/`dragging` never latch on with no contact held.
  const endDrag = useCallback((e: React.PointerEvent<HTMLImageElement>) => {
    const d = dragRef.current
    if (d.active) { try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* no capture */ } }
    d.active = false
    if (d.dragging) { d.dragging = false; setDragging(false) }
  }, [])
  // Keep a fresh ref so the global keydown handler (subscribed once per open)
  // can read the current image for the download shortcut without a stale closure.
  const stateRef = useRef<LightboxDetail | null>(null)
  stateRef.current = state
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as Partial<LightboxDetail> & Partial<LightboxImage> | undefined
      if (!detail) { setState(null); return }
      if (Array.isArray(detail.images) && detail.images.length > 0) {
        const raw = Number.isInteger(detail.index) ? (detail.index as number) : 0
        const idx = Math.max(0, Math.min(raw, detail.images.length - 1))
        setState({ images: detail.images, index: idx })
      } else if (typeof detail.src === 'string') {
        setState({ images: [{ src: detail.src, alt: detail.alt || '' }], index: 0 })
      }
    }
    window.addEventListener('lightbox', handler)
    return () => window.removeEventListener('lightbox', handler)
  }, [])
  const isOpen = state !== null
  // Reset the zoom whenever the lightbox opens/closes or the shown image
  // changes, so each image starts fit-to-screen rather than inheriting the
  // previous one's zoom.
  useEffect(() => { setZoom(LIGHTBOX_ZOOM_MIN) }, [isOpen, state?.index])
  // On any zoom change, recentre at fit and otherwise re-clamp the existing pan
  // to the new (smaller/larger) bounds — zooming out must not strand the image
  // off-screen. Runs post-layout, so offsetWidth already reflects the new box.
  useEffect(() => { setPan(p => (zoom <= LIGHTBOX_ZOOM_MIN ? { x: 0, y: 0 } : clampPan(p.x, p.y))) }, [zoom, clampPan])
  useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setState(null)
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault()
        setState(s => (s && s.index > 0 ? { ...s, index: s.index - 1 } : s))
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        setState(s => (s && s.index < s.images.length - 1 ? { ...s, index: s.index + 1 } : s))
      } else if ((e.key === '+' || e.key === '=') && !isEditableTarget(e.target) && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        zoomIn()
      } else if ((e.key === '-' || e.key === '_') && !isEditableTarget(e.target) && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        zoomOut()
      } else if (e.key === '0' && !isEditableTarget(e.target) && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        setZoom(LIGHTBOX_ZOOM_MIN)
      } else if ((e.key === 'd' || e.key === 'D') && !isEditableTarget(e.target)) {
        e.preventDefault()
        const cur = stateRef.current
        if (cur) void downloadLightboxImage(cur.images[cur.index])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isOpen, zoomIn, zoomOut])
  if (!state) return null
  const img = state.images[state.index]
  const zoomed = zoom > LIGHTBOX_ZOOM_MIN
  return (
    <Clickable className="fixed inset-0 z-[9999] bg-black/80 flex items-center justify-center overflow-hidden cursor-pointer" onClick={() => setState(null)}>
      {/* Inner wrapper centres the image; when enlarged, the image is dragged
          around via a translate transform (see pointer handlers) rather than
          scrollbars — a flex-centred overflow container can't scroll to its
          hidden top/left edges, so drag-to-pan is the reliable mechanism. */}
      <div className="flex items-center justify-center w-full h-full">
        {/* The image is a drag surface for panning when zoomed; zoom itself
            lives in the toolbar + keyboard. A plain click only stops the
            backdrop-close from firing (clicking the image should not dismiss
            the viewer). Escape / the toolbar buttons are the keyboard paths,
            so this presentational <img> needs no key handler. */}
        {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions */}
        <img
          ref={imgRef}
          src={img.src}
          alt={img.alt}
          draggable={false}
          className={`select-none object-contain rounded-lg shadow-2xl ${dragging ? '' : 'transition-transform duration-150'} ${zoomed ? (dragging ? 'cursor-grabbing' : 'cursor-grab') : 'cursor-default'}`}
          style={{ maxWidth: '90vw', maxHeight: '90vh', transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`, transformOrigin: 'center' }}
          onDragStart={e => e.preventDefault()}
          onPointerDown={e => {
            if (zoom <= LIGHTBOX_ZOOM_MIN) return // nothing to pan at fit
            e.preventDefault()
            try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* unsupported */ }
            dragRef.current = { startX: e.clientX, startY: e.clientY, baseX: pan.x, baseY: pan.y, moved: 0, active: true, dragging: false }
          }}
          onPointerMove={e => {
            const d = dragRef.current
            if (!d.active) return
            const dx = e.clientX - d.startX
            const dy = e.clientY - d.startY
            d.moved = Math.max(d.moved, Math.hypot(dx, dy))
            if (d.moved > 4 && !d.dragging) { d.dragging = true; setDragging(true) }
            setPan(clampPan(d.baseX + dx, d.baseY + dy))
          }}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onClick={e => { e.stopPropagation() }}
        />
      </div>
      {/* Control cluster sits on its own translucent, blurred pill so the
          white icons stay legible even when a light/enlarged image is panned
          up behind the toolbar. */}
      <div className="fixed top-4 right-4 flex items-center gap-0.5 rounded-full bg-black/60 backdrop-blur-md ring-1 ring-white/15 shadow-lg px-1 py-1">
        {/* Zoom segment: − / reset (magnifier) / + always visible as a group. */}
        <button
          aria-label={i18nT('components.markdownRenderer.zoom_out')}
          title={i18nT('components.markdownRenderer.zoom_out')}
          disabled={zoom <= LIGHTBOX_ZOOM_MIN}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors disabled:opacity-40 disabled:hover:bg-transparent"
          onClick={(e) => { e.stopPropagation(); zoomOut() }}
        >
          <Minus className="lucide-inline" aria-hidden="true" />
        </button>
        <button
          aria-label={i18nT('components.markdownRenderer.reset_zoom')}
          title={i18nT('components.markdownRenderer.reset_zoom')}
          disabled={zoom <= LIGHTBOX_ZOOM_MIN}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors disabled:opacity-40 disabled:hover:bg-transparent"
          onClick={(e) => { e.stopPropagation(); setZoom(LIGHTBOX_ZOOM_MIN) }}
        >
          <Search className="lucide-inline" aria-hidden="true" />
        </button>
        <button
          aria-label={i18nT('components.markdownRenderer.zoom_in')}
          title={i18nT('components.markdownRenderer.zoom_in')}
          disabled={zoom >= LIGHTBOX_ZOOM_MAX}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors disabled:opacity-40 disabled:hover:bg-transparent"
          onClick={(e) => { e.stopPropagation(); zoomIn() }}
        >
          <Plus className="lucide-inline" aria-hidden="true" />
        </button>
        <span className="w-px h-5 bg-white/20 mx-0.5" aria-hidden="true" />
        <button
          aria-label={i18nT('components.markdownRenderer.download_image')}
          title={i18nT('components.markdownRenderer.download_d')}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors"
          onClick={(e) => { e.stopPropagation(); void downloadLightboxImage(img) }}
        >
          <Download className="lucide-inline" aria-hidden="true" />
        </button>
        <button
          aria-label={i18nT('components.markdownRenderer.close')}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors"
          onClick={() => setState(null)}
        >
          <X className="lucide-inline" aria-hidden="true" />
        </button>
      </div>
    </Clickable>
  )
}
