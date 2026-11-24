/**
 * Whether a path names a page of the dashboard that a panel may show live.
 *
 * The gateway decides (`browser_harness/panel_page.py` holds the rules that count), and it has two ways. With its
 * guard off, which is how it starts, any page of the dashboard opens but the few of `SHUT_SHAPES`. With its guard
 * on (`browser.xr_pages_guarded`) only the pages of `LIVE_SHAPES` open. A page is opened by one rule in both ways, so
 * `livePath` is the wider rule: it spares the gateway an open that it refuses in both ways. A page that only the
 * guard refuses is asked for, and the panel goes when the gateway says no. Kept apart from `streamSource.ts` so a
 * test can read it without the renderer.
 */

/**
 * The patterns of `PATH_SHAPES` in `panel_page.py`, one for one: the pages that open in both ways. The plus's own
 * list is made of these. A test reads them from there and compares.
 */
export const LIVE_SHAPES: readonly string[] = [
  '/chat(/[A-Za-z0-9_.:-]{1,128})?',
  '/members',
  '/notifications',
  '/sessions',
  '/session-dashboards',
  '/overview',
  '/crew-board',
  '/schedule',
  '/projects(/[A-Za-z0-9_.:~-]{1,128}){0,6}',
  '/tasks',
  '/artifacts(/(?!deploy$)[A-Za-z0-9_.-]{1,128})?',
  '/logs',
  '/settings(/(?!(people|connections|instances)(/|$))[a-z0-9-]{1,64}(/[a-z0-9-]{1,64}){0,2})?',
]

const LIVE = LIVE_SHAPES.map(shape => new RegExp(`^(?:${shape})$`))

/** The patterns of `SHUT_SHAPES` in `panel_page.py`, one for one: what opens in neither way. A test compares. */
export const SHUT_SHAPES: readonly string[] = [
  '/xr(/.*)?',
  '/deploy(/.*)?',
  '/artifacts/(deploy|remote)(/.*)?',
  '/settings/(people|instances)(/.*)?',
  '/instances(/.*)?',
]

const SHUT = SHUT_SHAPES.map(shape => new RegExp(`^(?:${shape})$`))

/** A dot segment is a path the browser rewrites into another one. */
const dots = (path: string): boolean => path.split('/').some(segment => segment === '.' || segment === '..')

/**
 * A path as the dashboard's router reads it: decoded once, in lower case, one slash between two segments and none
 * at the end. So `/XR`, `/xr/` and `/%78r` are all the XR page. Null for a path that cannot be decoded.
 */
function asRouted(path: string): string | null {
  let plain: string
  try { plain = decodeURIComponent(path) } catch { return null }
  return plain.toLowerCase().replace(/\/+/g, '/').replace(/\/+$/, '') || '/'
}

/**
 * Whether a page stays out of XR: the XR page itself, and the pages that publish, admit people or link other
 * machines (`SHUT_SHAPES`). A page that the XR tab draws itself (`domSource.ts`) is not let go to one of them.
 */
export function staysOut(path: string): boolean {
  const routed = asRouted(path)
  return routed === null || dots(routed) || SHUT.some(shape => shape.test(routed))
}

/** Whether a path is one of the list that opens in both ways. */
export const listedPath = (path: string): boolean => path.length <= 300 && !dots(path) && LIVE.some(shape => shape.test(path))

/** The Bots page with one Bot named, as the gateway spells it (`panel_page.allowed_open`). */
export const MEMBER_PAGE = '/members?member='
/** A name with no line break, no tab and no other control character, and no space at either end. */
const namesABot = (name: string): boolean => name.length > 0 && name.length <= 256 && name === name.trim() && ![...name].some(ch => ch.charCodeAt(0) < 32 || ch.charCodeAt(0) === 127)

/**
 * The page of one Bot: the Bots page with that Bot's name as its one query, every reserved character encoded. The
 * Bots page with no name shows whichever Bot the browser showed last, so a panel that is opened for a Bot names it.
 * The spelling is the gateway's own, so that an address this page opened and one the gateway tells it are the same
 * string: `encodeURIComponent` leaves five characters that the gateway encodes.
 */
export function memberPath(name: string): string | null {
  if (!namesABot(name)) return null
  return MEMBER_PAGE + encodeURIComponent(name).replace(/[!'()*]/g, ch => `%${ch.charCodeAt(0).toString(16).toUpperCase()}`)
}

/** The chat page with one session named, as the dashboard's own session link spells it. */
export const CHAT_SESSION = '/chat?sid='
const SESSION_KEY = /^[A-Za-z0-9_.:-]{1,128}$/

/**
 * The page of one chat session: the chat page with that session's key as its one query. The chat page with no key
 * shows whichever session that browser showed last, so a panel that is opened for a session names it. The gateway
 * opens it while its guard is off (`panel_page.py`, `_unguarded_open`); with the guard on it refuses, and the panel goes.
 */
export const chatSessionPath = (key: string): string | null => (SESSION_KEY.test(key) ? CHAT_SESSION + key : null)

/** A path of the list, the page of one Bot or of one chat session, in the one spelling that is kept and compared; null for anything else. */
export function livePath(path: string | null | undefined): string | null {
  if (!path || path.length > 2000) return null
  // Exactly one pair, of a key's own characters: a second one, or a fragment, is another address.
  if (path.startsWith(CHAT_SESSION)) return chatSessionPath(path.slice(CHAT_SESSION.length))
  if (path.startsWith(MEMBER_PAGE)) {
    // Exactly one pair: a second one, or a fragment, is another address.
    const value = path.slice(MEMBER_PAGE.length)
    if (/[&#=?;]/.test(value)) return null
    try { return memberPath(decodeURIComponent(value.replace(/\+/g, ' '))) } catch { return null }
  }
  // A path and nothing after it, in the characters a browser writes an address in.
  if (path.length > 300 || !/^\/[\x21-\x7e]*$/.test(path) || /[?#]/.test(path) || dots(path)) return null
  // A second slash or a backslash after the first names another host to a browser (`//host/x`): not this origin.
  if (/^\/[/\\]/.test(path)) return null
  const routed = asRouted(path)
  if (routed === null || dots(routed) || SHUT.some(shape => shape.test(routed))) return null
  return path
}
