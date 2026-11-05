/** A bot's Dashboard page saved as one HTML file that opens anywhere, offline.
 *
 * Built in the BROWSER, from the same document the tab shows: `buildSrcdoc` over the
 * page the gateway composed, with the reader's live theme tokens. The gateway has no
 * theme to read, so a gateway-built copy could only draw the default light theme; this
 * keeps the file looking like the tab and adds no second document builder.
 *
 * The page itself is already self-contained -- the frame's CSP allows only `data:`
 * fonts and images and `connect-src 'none'` -- so the one thing the tab loads by URL
 * is the same-origin Tailwind runtime. The file cannot fetch it (a `file:` document
 * has no dashboard origin, and a cookie-authenticating proxy refuses a request with
 * no cookie), so it is fetched here once and inlined in place of that `<script src>`.
 *
 * Every page is saved inside a wrapper document, as the `srcdoc` of one frame with
 * the tab's own sandbox: `allow-scripts` only for a page that may run script, and
 * the empty sandbox (every restriction) for a page the tab shows without script,
 * which is also built with `pageScripts: false`. The wrapper CSP's `default-src
 * 'none'` (the `frame-src` fallback) stops the frame navigating anywhere. Opened
 * from disk as a top-level document, a page could otherwise send what it shows to
 * any host by navigating: its script, or for a static page a nested frame's
 * `target=_top` link, which the tab's empty sandbox stops and a top-level file
 * would not. The sandbox also withholds the windows and downloads the tab's frame
 * withholds, and a nested frame inherits it.
 *
 * Links do not open from the saved file. The page runs with the same limits as in
 * the tab, where links leave through the dashboard's link bridge; a saved file has
 * no dashboard to hand them to. The frame's sandbox is what stops them: a static
 * page's empty sandbox runs no script (its link guard included), and a scripted
 * page's frame may neither open a window nor navigate. The gesture relays are left
 * out for the same reason: nothing is listening to them.
 */
import { AGENT_PAGE_SANDBOX, buildSrcdoc, resolveWidgetTheme } from '../../lib/widgetSrcdoc'
import { sanitizeCssValue } from '../../lib/cssSanitize'
import { TAILWIND_RUNTIME_PATH } from '../../lib/vendorPaths'
import { withBase } from '../../lib/basePath'

/** The grant set of the tab's frame for a page it shows without script: empty, every
 *  sandbox restriction applied (`CREW_DASHBOARD_SANDBOX_STATIC` in
 *  `CrewDynamicDashboard.tsx`, kept equal to it by this module's test). */
export const STATIC_PAGE_SANDBOX = ''

/** The most one saved page may be, in bytes. A page is up to 3 MiB and its data
 *  island up to 4 MiB (`dashboard_frame.MAX_PAGE_BYTES`, `sandbox_doc._MAX_BYTES`),
 *  plus the runtime and what `buildSrcdoc` adds, so a real page fits with room. */
export const MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024

/** Why a save did not produce a file. */
export type DownloadRefusal = 'too_large' | 'no_runtime'

export class DashboardDownloadError extends Error {
  constructor(readonly reason: DownloadRefusal) {
    super(reason)
    this.name = 'DashboardDownloadError'
  }
}

/** Characters a file system may refuse in a name. */
const UNSAFE_NAME_CHARS = new Set(['\\', '/', ':', '*', '?', '"', '<', '>', '|'])

/** The file name: the bot's name and the day, with anything a file system may
 *  refuse taken out. Each refused character and each run of white space becomes
 *  one space. */
export function downloadFileName(name: string, now: Date = new Date()): string {
  let safe = ''
  for (const ch of name) {
    const blank = ch.charCodeAt(0) < 0x20 || UNSAFE_NAME_CHARS.has(ch) || ch.trim() === ''
    if (!blank) safe += ch
    else if (safe && !safe.endsWith(' ')) safe += ' '
  }
  safe = safe.trim()
  // The reader's own day: `toISOString` is UTC, which dates an evening save west of
  // UTC tomorrow.
  const pad = (n: number) => String(n).padStart(2, '0')
  const day = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
  return `${safe || 'dashboard'} ${day}.html`
}

/** A dashboard read, as the data island and the push frames carry it. */
export type DashboardRead = Record<string, unknown>

function versionOf(read: DashboardRead): number {
  return typeof read.version === 'number' ? read.version : 0
}

function generationOf(read: DashboardRead): string {
  return typeof read.generation === 'string' ? read.generation : ''
}

/** The read a page holds after *arrived* is posted to a page holding *held*.
 *
 *  Mirrors the frame bootstrap's refill listener (`_BOOTSTRAP_JS` in
 *  `dashboard_frame.py`), so the saved file carries what the tab shows: a superseded
 *  full read and an out-of-order patch are dropped (the page asks for a whole read
 *  to recover from a rejected patch), and an in-order patch keeps the fields it
 *  did not move. */
export function applyPushedRead(held: DashboardRead, arrived: DashboardRead): DashboardRead {
  const version = versionOf(arrived)
  const generation = generationOf(arrived)
  const sameGeneration = generation === generationOf(held)
  if (arrived.patch !== true) {
    return sameGeneration && version < versionOf(held) ? held : arrived
  }
  if (!sameGeneration || version !== versionOf(held) + 1) return held
  const asObject = (v: unknown) => (v && typeof v === 'object' ? (v as DashboardRead) : {})
  return {
    fields: { ...asObject(held.fields), ...asObject(arrived.fields) },
    agentic: arrived.agentic || [],
    seq: typeof arrived.seq === 'number' ? arrived.seq : 0,
    missing: arrived.missing || [],
    written_at: arrived.written_at || {},
    locale: typeof arrived.locale === 'string' ? arrived.locale : held.locale,
    version,
    generation: typeof arrived.generation === 'string' ? arrived.generation : held.generation,
    // Kept from the held read: a patch carries the builder's defaults for these.
    subject: held.subject,
    last_user_at: held.last_user_at,
  }
}

/** The id `dashboard_frame.compose_body` gives the composed page's data island. */
const ISLAND_ID = 'kirocrew-dashboard-data'

/** `dashboard_frame.escape_json_for_html`: each of these as JSON's own `\u` escape,
 *  so the JSON is the same and the HTML parser sees nothing it would act on. */
const JSON_HTML_UNSAFE = new Set(['<', '>', '&', '\u2028', '\u2029'])
/** A backslash and `u`, spelled by code point so it reads as no string. */
const JSON_ESCAPE = String.fromCharCode(0x5c, 0x75)

function escapeJsonForHtml(blob: string): string {
  let out = ''
  for (const ch of blob) {
    out += JSON_HTML_UNSAFE.has(ch) ? JSON_ESCAPE + ch.charCodeAt(0).toString(16).padStart(4, '0') : ch
  }
  return out
}

/** *html* with its data island holding *read*. The frame bootstrap fills every
 *  binding from the island when the document loads, so this is all a page that runs
 *  script needs to open with these values. Unchanged when no island is found.
 *
 *  Parsed in a detached document (nothing in it runs) the way `buildSrcdoc` parses a
 *  page, through `createContextualFragment`, and the island found by its id, so the
 *  result does not depend on how the composer spells the tag. `buildSrcdoc` parses
 *  and serializes the page the same way, so this adds no new rewriting. */
export function withIslandRead(html: string, read: DashboardRead): string {
  const doc = document.implementation.createHTMLDocument('')
  const range = doc.createRange()
  range.selectNodeContents(doc.body)
  const fragment = range.createContextualFragment(html)
  const island = fragment.getElementById(ISLAND_ID)
  if (!island) return html
  island.textContent = escapeJsonForHtml(JSON.stringify(read))
  doc.body.replaceChildren(fragment)
  return doc.body.innerHTML
}

export interface DashboardDownloadInput {
  /** The composed page the tab is showing (`shown.html`). */
  html: string
  themeVars: Record<string, string>
  mode: 'dark' | 'light'
  /** Whether the tab lets this page run its own script. */
  pageScripts: boolean
}

/** The saved document as a Blob. *fetchText* reads the runtime; tests pass their own. */
export async function buildDashboardDownload(
  input: DashboardDownloadInput,
  fetchText: (url: string) => Promise<string> = defaultFetchText,
): Promise<Blob> {
  const runtimeUrl = window.location.origin + withBase(TAILWIND_RUNTIME_PATH)
  const runtimeJs = await fetchText(runtimeUrl)
  if (!runtimeJs) throw new DashboardDownloadError('no_runtime')
  // The builder writes the runtime inline itself, under the nonce it minted, so
  // nothing has to be found in its output afterwards.
  const page = buildSrcdoc({
    html: input.html,
    themeVars: input.themeVars,
    mode: input.mode,
    offlineScripts: true,
    pageScripts: input.pageScripts,
    inlineRuntime: runtimeJs,
  })
  const file = sandboxedWrapper(page, input)
  const blob = new Blob([file], { type: 'text/html' })
  if (blob.size > MAX_DOWNLOAD_BYTES) throw new DashboardDownloadError('too_large')
  return blob
}

/** The policy of the CSP `<meta>` `buildSrcdoc` always writes in *page*'s head. */
function pagePolicy(page: string): string {
  const metas = new DOMParser().parseFromString(page, 'text/html').head.querySelectorAll('meta')
  const csp = Array.from(metas).find((meta) => meta.getAttribute('http-equiv') === 'Content-Security-Policy')
  return csp?.getAttribute('content') ?? ''
}

/** *declarations* as one inline `style` value. */
function inlineStyle(declarations: Record<string, string>): string {
  return Object.entries(declarations)
    .filter(([, value]) => value)
    .map(([name, value]) => `${name}:${value}`)
    .join(';')
}

/** *page* as the `srcdoc` of a frame sandboxed as the tab's frame is, filling a
 *  wrapper painted in the page's own background so opening it shows no flash.
 *  Built with DOM APIs, so the serializer escapes the attribute.
 *
 *  The wrapper's policy is the page's own. A `srcdoc` document inherits its
 *  parent's CSP and enforces it beside its own, so a wrapper policy that closed
 *  script would stop the page's inline script; the page's policy grants the page
 *  nothing new. The wrapper holds no script. Its `default-src 'none'` is the
 *  `frame-src` fallback: the `srcdoc` frame is not fetched, so it still loads, and
 *  every navigation of that frame (`https:`, `data:`, a meta refresh) is refused. */
function sandboxedWrapper(page: string, input: DashboardDownloadInput): string {
  const policy = pagePolicy(page)
  // Never save a wrapper without a policy: it would let the frame navigate. This is
  // a document not in the shape `buildSrcdoc` writes.
  if (!policy) throw new DashboardDownloadError('no_runtime')
  // The theme `buildSrcdoc` drew the page in, light-canvas fallback included.
  const { themeVars, mode } = resolveWidgetTheme(input.html, input.themeVars, input.mode)
  const doc = document.implementation.createHTMLDocument('')
  const charset = doc.createElement('meta')
  charset.setAttribute('charset', 'utf-8')
  const csp = doc.createElement('meta')
  csp.setAttribute('http-equiv', 'Content-Security-Policy')
  csp.setAttribute('content', policy)
  const viewport = doc.createElement('meta')
  viewport.setAttribute('name', 'viewport')
  viewport.setAttribute('content', 'width=device-width, initial-scale=1')
  doc.head.replaceChildren(charset, csp, viewport)
  const fill = { margin: '0', height: '100%', overflow: 'hidden' }
  doc.documentElement.setAttribute(
    'style',
    inlineStyle({ ...fill, 'color-scheme': mode, background: sanitizeCssValue(themeVars['--bg']) }),
  )
  doc.body.setAttribute('style', inlineStyle(fill))
  const frame = doc.createElement('iframe')
  frame.setAttribute('sandbox', input.pageScripts ? AGENT_PAGE_SANDBOX : STATIC_PAGE_SANDBOX)
  frame.setAttribute('style', inlineStyle({ display: 'block', border: '0', width: '100%', height: '100%' }))
  frame.setAttribute('srcdoc', page)
  doc.body.replaceChildren(frame)
  // Built as a node rather than read from `doc.doctype`, which not every DOM gives a
  // document made by `createHTMLDocument`; without it the wrapper opens in quirks mode.
  const doctype = new XMLSerializer().serializeToString(document.implementation.createDocumentType('html', '', ''))
  return doctype + doc.documentElement.outerHTML
}

async function defaultFetchText(url: string): Promise<string> {
  const res = await fetch(url, { credentials: 'same-origin' })
  if (!res.ok) return ''
  return res.text()
}
