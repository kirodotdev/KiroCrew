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
 * A slot's SHORT name, with the mint timestamp left off: `chat-<n>`.
 *
 * This is how a session is actually named in prose — the sidebar shows it, the
 * agent tools return it, and a person asked to say which session they mean says
 * "chat-1380", not "chat-1380-1789049480". The full key stays the identifier;
 * this is a nickname, and a nickname needs somewhere to be looked up.
 *
 * Anchored like the full grammar, for the same reason.
 */
const SESSION_SHORT_RE = /^chat-\d+$/

/**
 * Whether `raw` has the SHAPE of a short slot name, with no roster consulted.
 *
 * Shape alone is what says "this text is naming a session", which is a different
 * question from "that session is open". `MdAnchor` needs the first to decide
 * whether a `?sid=` link is a chat-session link at all: an unresolvable one must
 * be declined rather than navigate the browser to a dead session view, and a
 * short name is as recognisable as a full key there.
 */
export function isSessionShortName(raw: string): boolean {
  return SESSION_SHORT_RE.test(normalizeRunSessionKey(raw.trim()))
}

/**
 * Whether `raw` NAMES a session at all, in either spelling, with no roster
 * consulted.
 *
 * The union lives here rather than at the call site so it cannot drift: `MdAnchor`
 * uses it to decide that a `?sid=` link is a chat-session link and must be
 * declined rather than navigated (#9914), while `resolveSessionChip` asks the
 * roster whether that session is reachable. Spelled twice, a spelling added to the
 * resolver alone would slip past the anchor's decline gate and send a click back to
 * the dead session view.
 */
export function namesASession(raw: string): boolean {
  return sessionKeyFrom(raw) !== null || isSessionShortName(raw)
}

/**
 * The one open slot `raw` is the short name of, or null.
 *
 * `keys` is the live roster, and it is the SOLE authority — resolving a nickname
 * is a lookup, never a guess, so a name no open session answers to stays plain
 * text exactly as an unknown full key does.
 *
 * Refuses an AMBIGUOUS name outright. A slot number is reused across gateway
 * generations, so two open slots can share one number and differ only in the
 * timestamp; picking either would send a click to a session the reader did not
 * name. The full key is the disambiguator, and it already works.
 *
 * `writtenAtEpoch` is REQUIRED, and that is the point. It closes the collision
 * ACROSS TIME, which the ambiguity check above cannot see: when the older
 * generation's slot is closed only one match remains and it is the wrong one, so
 * an old transcript's `chat-1380` would resolve to whichever session later took
 * that number. Slot numbers really are reused -- measured on a real data home,
 * 1423 slots carried only 1399 distinct numbers -- so this is not a theoretical
 * hazard, and its failure is silent: the reader lands in a conversation they did
 * not name. The mint time is already the second half of every slot key, so a
 * candidate minted after the text naming it is skipped.
 *
 * Pass `undefined` where the caller does not know when the text was written, and
 * NO short name resolves. That is deliberate fail-closed: without a write time
 * there is nothing that distinguishes "the session this named" from "the session
 * that later took its number", and a chip must not guess. The FULL key is
 * unaffected on those surfaces, because it names its generation exactly.
 */
export function sessionKeyFromShort(
  raw: string,
  keys: Iterable<string>,
  writtenAtEpoch: number | undefined,
): string | null {
  if (writtenAtEpoch === undefined) return null
  const short = normalizeRunSessionKey(raw.trim())
  if (!SESSION_SHORT_RE.test(short)) return null
  // The separator is part of the prefix: without it `chat-138` also claims
  // `chat-1380-…`, which is a different session.
  const prefix = `${short}-`
  let found: string | null = null
  for (const key of keys) {
    // Prefix test FIRST, shape test only on a hit. Every roster entry but the
    // one being looked for fails this, and it is the cheap half of the pair —
    // measured at a 103-session roster, testing the shape of all 103 costs ~3x
    // what rejecting on the prefix does.
    const canonical = normalizeRunSessionKey(key)
    if (!canonical.startsWith(prefix)) continue
    if (!SESSION_KEY_RE.test(canonical)) continue
    if (mintEpoch(canonical) > writtenAtEpoch) continue
    if (found) return null
    found = canonical
  }
  return found
}

/** The mint time a slot key carries, in epoch seconds. */
function mintEpoch(key: string): number {
  return Number(key.slice(key.lastIndexOf('-') + 1))
}

/**
 * The session parameter a chat deep link carries, VERBATIM, or null.
 *
 * Split out of `sessionKeyFromChatHref` so a link and an inline chip resolve the
 * same spellings: the href gates (origin, and the message-anchor exclusion) are
 * about the URL, while which spellings name a session is the grammar above. A
 * caller that accepts more than one spelling needs the gates applied and the
 * grammar left to it.
 */
export function chatHrefSid(href: string): string | null {
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
  return chatDeepLinkSlot(`${url.pathname}${url.search}`) || null
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
  const sid = chatHrefSid(href)
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
