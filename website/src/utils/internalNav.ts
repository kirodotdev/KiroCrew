import { appPathname } from '../lib/basePath'
import { chatDeepLinkSlot } from './navIntent'
import { sessionKeyFrom } from './sessionKeys'

/**
 * In-app navigation for links that name a page of this dashboard.
 *
 * A link in a chat message or in an agent-written page (a crewmate's Dashboard
 * tab, an artifact, a widget) used to open every dashboard path in a new browser
 * tab, and in the desktop app in a second window. A link this module accepts
 * opens in place instead, through the router and the page-leave check, like a
 * sidebar click.
 *
 * Acceptance is an explicit list of destinations, each with the query
 * parameters it may carry. A link that names anything else, or carries any other
 * parameter, is refused here and keeps opening the way it did before, so a link
 * cannot reach a page action through a query string this list never named. The
 * path that is navigated is rebuilt from the parsed fields, never the authored
 * text.
 */

/** Longest query value carried over, in characters. */
const MAX_PARAM_LENGTH = 512

/** A path segment the rebuilt path may contain. */
const SEGMENT_RE = /^[A-Za-z0-9._~-]{1,128}$/

/** A fragment the rebuilt path may keep (a heading or setting anchor). */
const HASH_RE = /^#[A-Za-z0-9._-]{1,128}$/

interface Destination {
  /** Matches the parsed path segments (after the leading `/`). */
  matches: (segments: readonly string[]) => boolean
  /** The query parameters this destination may carry. */
  params: readonly string[]
}

const exactly = (...want: string[]) => (segments: readonly string[]) =>
  segments.length === want.length && want.every((s, i) => segments[i] === s)

/**
 * The dashboard pages a link may open in place. Popout and embed routes, app
 * pages and redirect-only aliases are absent on purpose: they either belong to
 * another window or host content this list cannot vouch for.
 */
const DESTINATIONS: readonly Destination[] = [
  { matches: exactly('schedule'), params: ['job'] },
  // Any settings sub-page: the settings router owns which ones exist.
  { matches: (s) => s[0] === 'settings' && s.length <= 4, params: ['tab', 'sub', 'section', 'highlight', 'channel'] },
  { matches: exactly('capabilities'), params: ['tab'] },
  { matches: exactly('members'), params: ['name'] },
  { matches: exactly('sessions'), params: [] },
  { matches: exactly('session-dashboards'), params: [] },
  { matches: exactly('notifications'), params: [] },
  { matches: exactly('crew-board'), params: [] },
  { matches: exactly('logs'), params: [] },
  { matches: exactly('apps'), params: [] },
  { matches: exactly('apps', 'library'), params: [] },
  { matches: exactly('artifacts'), params: ['folder'] },
  { matches: (s) => s.length === 2 && s[0] === 'artifacts' && s[1] !== 'remote' && s[1] !== 'deploy', params: [] },
]

function isChatPath(segments: readonly string[]): boolean {
  return segments[0] === 'chat' && segments.length <= 2
}

/**
 * The in-app path `href` names, rebuilt from its parsed fields, or null when it
 * is not a link this module opens in place.
 *
 * `origins` lists the origins that count as this dashboard besides the page's
 * own: the configured `dashboard.url` and the tunnel's public origin, each an
 * exact origin. A relative href resolves against the page's own origin. A chat
 * link must name a session by its full key (`/chat?sid=<key>`); a short name
 * needs the session roster to resolve, which the chat renderer's own session
 * links already have.
 */
export function resolveInternalNav(href: string | null | undefined, origins: readonly string[] = []): string | null {
  if (!href) return null
  const own = globalThis.location?.origin
  if (!own || own === 'null') return null
  let url: URL
  try {
    url = new URL(href.trim(), own)
  } catch {
    return null
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  if (url.username || url.password) return null
  if (url.origin !== own && !origins.includes(url.origin)) return null
  // A sub-path build (`BASE_PATH`) serves every page under its base, and the DOM
  // shim writes links there, so the route is read with the base removed. The
  // path handed back is the router's own, which has no base.
  const pathname = appPathname(url.pathname)
  let segments: string[]
  try {
    segments = pathname.split('/').slice(1).map(decodeURIComponent)
  } catch {
    return null
  }
  if (segments.length > 0 && segments[segments.length - 1] === '') segments.pop()
  if (segments.length === 0 || !segments.every((s) => SEGMENT_RE.test(s) && s !== '.' && s !== '..')) return null
  if (url.hash && !HASH_RE.test(url.hash)) return null

  if (isChatPath(segments)) {
    for (const name of url.searchParams.keys()) if (name !== 'sid' && name !== 'slot') return null
    const named = chatDeepLinkSlot(`${pathname}${url.search}`)
    if (!named) return url.search ? null : '/chat'
    const key = sessionKeyFrom(named)
    return key ? `/chat?${new URLSearchParams({ sid: key })}` : null
  }

  const destination = DESTINATIONS.find((d) => d.matches(segments))
  if (!destination) return null
  const query = new URLSearchParams()
  for (const [name, value] of url.searchParams) {
    if (!destination.params.includes(name) || query.has(name)) return null
    if (value.length > MAX_PARAM_LENGTH || /\p{Cc}/u.test(value)) return null
    query.set(name, value)
  }
  const path = `/${segments.map(encodeURIComponent).join('/')}`
  const search = query.toString()
  return `${path}${search ? `?${search}` : ''}${url.hash}`
}

/** Origin of a configured URL, or null when it has none worth trusting. */
export function originOf(raw: unknown): string | null {
  if (typeof raw !== 'string' || !raw.trim()) return null
  try {
    const url = new URL(raw.trim())
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
    return url.origin
  } catch {
    return null
  }
}

type InternalNavHandler = { origins: readonly string[]; open: (path: string) => void }

let handler: InternalNavHandler | null = null

/**
 * Install the main window's in-app navigator. The app shell installs it; popout
 * and embed windows do not, so a link there keeps opening the way it always has.
 * Returns the uninstall, which only removes this same registration.
 */
export function setInternalNavHandler(next: InternalNavHandler): () => void {
  handler = next
  return () => {
    if (handler === next) handler = null
  }
}

/** Whether `openInternalLink(href)` would open it in place, without opening it. */
export function wouldOpenInternalLink(href: string | null | undefined): boolean {
  return handler !== null && resolveInternalNav(href, handler.origins) !== null
}

/**
 * Open `href` in place when it names a page this window can show. Returns true
 * when the link was handed to the navigator (the caller then cancels the
 * browser's own open), false when it was not, so the caller keeps its existing
 * behaviour.
 */
export function openInternalLink(href: string | null | undefined): boolean {
  const current = handler
  if (!current) return false
  const path = resolveInternalNav(href, current.origins)
  if (!path) return false
  current.open(path)
  return true
}
