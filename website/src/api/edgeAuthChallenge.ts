/**
 * Recognise an interposed gate's refusal on a failed API call.
 *
 * A gate in front of the gateway answers a lapsed session with its own HTML page and
 * no `Location` header, so the refusal arrives looking like a plain 403 whose body is
 * markup. That is the silent failure this module ends: the caller words a message off
 * the outcome below, and the reader signs in themselves.
 *
 * It deliberately does NOT try to tell a lapsed session from a firewall block. An
 * earlier revision did, scanning the body for anchors, form actions, password fields,
 * meta refreshes and `location=` assignments, and review found FOUR separate classes
 * of page it read backwards -- `/authors/jane` as a sign-in link, `/oauth2/start` as
 * not one, a form-rendered gate as a block, and a geo-block's visible `Location = US`
 * text as a redirect. Each fix was one more spelling on a hand-maintained list read
 * against a body the far side authors. The message below is honest for both cases and
 * names the same action either way, so the distinction bought wording it did not need
 * and a confidence it could not support.
 *
 * Nothing here reads the body's contents at all now: only that it IS a document.
 */
import { i18nT } from '../i18n/t'
import { isEmbeddedPane } from '../lib/embedded'

/**
 * Anchored at the start, so a body that merely MENTIONS markup is not a page.
 * `<!doctype` needs the trailing space; `<html` accepts a space or the close, so
 * `<htmlish>` is not matched.
 */
const HTML_DOCUMENT_START = /^<(?:!doctype\s|html[\s>])/i

/**
 * Is this response body an HTML PAGE rather than an error message?
 *
 * `api/apiError` drops such a body rather than rendering it, and this module treats it
 * on a 401/403 as an interposed gate's page. One definition is what stops those two
 * drifting into recognising different things. It lives here rather than in a module of
 * its own because the import between them runs one way -- `apiError` imports this file
 * and nothing here imports `apiError` -- so there is no cycle for a third module to break.
 */
export const looksLikeHtmlDocument = (body: string): boolean =>
  HTML_DOCUMENT_START.test(body.trim())

/** The statuses an auth refusal arrives with. 403 is the one measured; 401 is the
 *  canonical challenge status, and the HTML-document test is what excludes ours. */
const CHALLENGE_STATUSES: ReadonlySet<number> = new Set([401, 403])

/**
 * Whether this failed response is an interposed gate's page rather than the gateway's.
 *
 * All three signals are required; any one alone also matches a gateway denial. 401 is
 * accepted beside the measured 403 because it is the CANONICAL status for an auth
 * challenge. The gateway's own 401 is unaffected: its sinks answer JSON or plain text,
 * and an HTML DOCUMENT body is still required here.
 */
function isEdgeAuthChallenge(
  status: number,
  contentType: string | null,
  body: string,
): boolean {
  if (!CHALLENGE_STATUSES.has(status)) return false
  const type = (contentType ?? '').split(';', 1)[0].trim().toLowerCase()
  if (type !== 'text/html' && type !== 'application/xhtml+xml') return false
  return looksLikeHtmlDocument(body)
}

/**
 * What the response turned out to be; the caller words its message off this.
 *
 * `framed` is separate because it rests on a fact about THIS document rather than on a
 * reading of the far side's: a nested pane cannot complete a sign-in inside itself, so
 * "reload this tab" is the one remedy that is wrong there.
 */
export type ChallengeOutcome = 'challenged' | 'framed'

/**
 * Name an interposed gate's refusal on a failed response, or null when this response
 * is not one.
 *
 * Only ADDS a name; the caller's `ApiError` is thrown either way.
 */
export function noteEdgeAuthChallenge(
  status: number,
  contentType: string | null,
  body: string,
): ChallengeOutcome | null {
  if (!isEdgeAuthChallenge(status, contentType, body)) return null
  if (typeof window === 'undefined') return 'challenged'
  return isEmbeddedPane() ? 'framed' : 'challenged'
}

/**
 * The address each framed message was produced with, newest last.
 *
 * Provenance has to be REMEMBERED rather than re-derived. Re-producing the message and
 * comparing strings looked equivalent and was not: a language switch remounts the tree
 * and re-evaluates every `i18nT()` (see `i18n/t`), so a message worded in one locale is
 * compared against the same key rendered in another, the equality fails, and the link
 * silently disappears -- reinstating the hand-transcription it exists to remove.
 *
 * Bounded because it is keyed on produced strings: a session yields one per locale per
 * origin, and the oldest is dropped past the cap rather than held forever.
 */
const ADDRESS_BY_MESSAGE = new Map<string, string>()
const MAX_REMEMBERED_MESSAGES = 8

function rememberAddress(message: string, origin: string): void {
  ADDRESS_BY_MESSAGE.delete(message)
  ADDRESS_BY_MESSAGE.set(message, origin)
  while (ADDRESS_BY_MESSAGE.size > MAX_REMEMBERED_MESSAGES) {
    const oldest = ADDRESS_BY_MESSAGE.keys().next()
    if (oldest.done) break
    ADDRESS_BY_MESSAGE.delete(oldest.value)
  }
}

/**
 * The address this message names, or null if it is not a message that names one.
 *
 * Answers from what was PRODUCED, so it is immune to the current locale and returns the
 * address the text actually carries rather than whatever the document's origin is now.
 * A message the far side authored is never in the map, so it can never become a link.
 */
export function addressNamedByMessage(message: string): string | null {
  return ADDRESS_BY_MESSAGE.get(message) ?? null
}

/** Test-only: forget produced messages so one case cannot see another's. */
export function __resetAddressProvenanceForTests(): void {
  ADDRESS_BY_MESSAGE.clear()
}

/**
 * The message for an outcome, or null when the response was not a challenge.
 *
 * One mapping, used by both error factories. `api/client` and `api/apiError` each
 * build an `ApiError` for the same refusal, and a second copy of this would let the
 * dashboard and the app bundles word the identical failure differently.
 *
 * Both strings offer the lapse as a CONDITION the reader can settle in one action,
 * rather than as a diagnosis this module is in no position to make.
 */
export function edgeChallengeMessage(outcome: ChallengeOutcome | null): string | null {
  if (outcome === null) return null
  if (outcome === 'framed') {
    // Origin only, and the pane's own: its address bar holds the OUTER dashboard's URL,
    // and an iframe src can carry query parameters. The address is remembered so the
    // render surface can offer it as a control rather than text to retype.
    let origin = ''
    try {
      origin = typeof window === 'undefined' ? '' : window.location.origin
    } catch {
      origin = ''
    }
    const message = i18nT('api.client.proxy_challenge_framed', { origin })
    if (origin && origin !== 'null') rememberAddress(message, origin)
    return message
  }
  return i18nT('api.client.proxy_challenge_reload')
}
