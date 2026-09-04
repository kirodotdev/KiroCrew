/**
 * Recognising a dashboard chat session by its identifier: text → slot key, for
 * the two shapes the key arrives in.
 *
 * No React and no framework imports, so both call sites in `MarkdownRenderer`
 * share ONE grammar and cannot drift — a key accepted as an inline chip is
 * accepted as a link, or neither is. The single ambient read is `location.origin`,
 * which is what decides same-origin, and it has an off-DOM fallback.
 */

import { normalizeRunSessionKey } from '../apps/workflows/runModel'
import { chatDeepLinkSlot } from './navIntent'

/** Base for resolving a relative href when there is no document to resolve
 *  against, and the origin such an href then lands on. */
const RELATIVE_BASE = 'http://localhost'

/**
 * The origin an href has to resolve to: this document's own, so the app's own
 * absolute links match. Falls back off-DOM (node, SSR), where none exists.
 */
function sessionOrigin(): string {
  const origin = globalThis.location?.origin
  return origin && origin !== 'null' ? origin : RELATIVE_BASE
}

/**
 * A slot key as the backend mints it: `chat-<n>-<unix-ts>`.
 *
 * The `dashboard:` / `dashboard_` prefixes are NOT spelled here. They are
 * stripped by `normalizeRunSessionKey`, the pre-existing owner of that grammar,
 * so the two cannot disagree — a second copy here already did, accepting
 * `dashboard_` (the persisted key form) while refusing `dashboard:` (the history
 * key the gateway itself mints).
 *
 * Anchored at both ends: a key is the WHOLE span, never a substring of it.
 * Matching loosely would turn any prose mentioning a key into a chip whose text
 * and target disagree.
 */
const SESSION_KEY_RE = /^chat-\d+-\d+$/

/** The slot key `raw` names, or null. Surrounding whitespace is trimmed; nothing
 *  else about the span is tolerated. */
export function sessionKeyFrom(raw: string): string | null {
  const key = normalizeRunSessionKey(raw.trim())
  return SESSION_KEY_RE.test(key) ? key : null
}

/**
 * The slot key a chat deep link points at, or null.
 *
 * Any href resolving to THIS origin, root-relative or absolute: the app's own
 * share link is minted as `${location.origin}/chat?sid=…`, so a pasted
 * Copy-link is the common shape rather than the exotic one. The parsed origin is
 * the sole authority, never a prefix test — `//host` is protocol-relative and
 * `/\host` is too under the WHATWG rule reading `\` as `/`, and `MdAnchor`
 * decodes `%5C` into that same backslash, so one check covers every shape.
 *
 * The path and session-parameter grammar is `chatDeepLinkSlot`'s, not respelled
 * here: it already owns which paths count and that `?slot=` aliases `?sid=`, so
 * a future alias or path shape lands in one place. This adds the two things it
 * has no opinion on — the origin gate above and the key grammar below.
 */
export function sessionKeyFromChatHref(href: string): string | null {
  const origin = sessionOrigin()
  let url: URL
  try {
    url = new URL(href, origin)
  } catch {
    return null
  }
  if (url.origin !== origin) return null
  // `ChatPage` reads `msg`/`mid` once at mount, so switching in place drops the
  // target. Left to the plain anchor, a fresh mount honours it.
  if (url.searchParams.has('msg') || url.searchParams.has('mid')) return null
  const sid = chatDeepLinkSlot(`${url.pathname}${url.search}`)
  return sid ? sessionKeyFrom(sid) : null
}

/**
 * `href` with its session parameter rewritten to the canonical slot `key`.
 *
 * An authored link may carry a spelling `?sid=` cannot resolve (`dashboard_…`) or
 * the legacy `?slot=` alias. A modified click — Cmd, Ctrl, middle — is handed to
 * the browser deliberately, so the attribute itself has to be the canonical
 * target or that click opens a session that fails to load. Path and any other
 * query parameters are preserved; only the session parameter is normalised, and
 * an absolute same-origin href comes back root-relative so the click stays in
 * the app rather than reloading it.
 */
export function canonicalChatHref(href: string, key: string): string {
  const url = new URL(href, sessionOrigin())
  url.searchParams.delete('slot')
  url.searchParams.set('sid', key)
  return `${url.pathname}${url.search}`
}
