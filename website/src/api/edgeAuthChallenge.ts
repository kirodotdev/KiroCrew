/**
 * Recognise an interposed proxy's sign-in page on a failed API call.
 *
 * A gate in front of the gateway answers a lapsed session with its own HTML page and
 * no `Location` header, so the refusal arrives looking like a plain 403 whose body is
 * markup. That is the silent failure this module ends: the caller words a message off
 * the outcome below, and the reader signs in themselves.
 *
 * Nothing here acts on the document. No URL from the body is ever read as a
 * destination either -- only whether the page offers a way in at all, which is what
 * tells a lapsed session from a firewall block page.
 */
import { looksLikeHtmlDocument } from './apiError'
import { safeHttpUrl } from '../lib/safeUrl'
import { isEmbeddedPane } from '../lib/embedded'

/** The statuses an auth refusal arrives with. 403 is the one measured; 401 is the
 *  canonical challenge status, and the HTML-document test is what excludes ours. */
const CHALLENGE_STATUSES: ReadonlySet<number> = new Set([401, 403])

/** How many `href` candidates to weigh before calling the page a block page. */
const MAX_HREF_CANDIDATES = 20

/** How much of the body to scan. A gate's sign-in anchor is in the first
 *  screenful; the rest is a hostile body's budget to waste. */
const MAX_SCAN_BYTES = 64 * 1024

/** Longest run of spaces read inside a start tag before giving up on it. */
const MAX_ATTR_GAP = 64

const isSpace = (c: string | undefined): boolean =>
  c === ' ' || c === '\t' || c === '\n' || c === '\r' || c === '\f'

/** An attribute-name character, so `<a` can be told from `<area` and `<abbr`. */
const isNameChar = (c: string | undefined): boolean =>
  c !== undefined && /[A-Za-z0-9:_.-]/.test(c)

/** Index after a BOUNDED run of spaces, or -1 when the run is longer than any
 *  real tag — the bound is what keeps this from being re-walked per position. */
function afterSpaces(s: string, from: number): number {
  let i = from
  while (i < s.length && isSpace(s[i])) {
    if (i - from >= MAX_ATTR_GAP) return -1
    i++
  }
  return i
}

/**
 * The `href` value in one anchor start tag's attribute text, or null.
 *
 * Hand-scanned rather than matched, so no step can backtrack: every position
 * costs a fixed number of comparisons and the one unbounded read (the closing
 * quote) is paid at most once per tag. Requires whitespace before the name, so
 * `data-href` is not an `href`, and requires a non-empty value.
 */
function hrefInTag(attrs: string): string | null {
  for (let i = 0; i < attrs.length; i++) {
    if (!isSpace(attrs[i])) continue
    if (attrs.slice(i + 1, i + 5).toLowerCase() !== 'href') continue
    let j = afterSpaces(attrs, i + 5)
    if (j < 0 || attrs[j] !== '=') continue
    j = afterSpaces(attrs, j + 1)
    if (j < 0) continue
    const quote = attrs[j]
    if (quote !== '"' && quote !== "'") continue
    const close = attrs.indexOf(quote, j + 1)
    if (close < 0) return null
    if (close <= j + 1) continue
    return attrs.slice(j + 1, close)
  }
  return null
}

/**
 * Every anchor's `href`, in source order, read in one pass.
 *
 * Scoped to the anchor start tag because a challenge page's `<head>` routinely
 * carries a `<link rel=stylesheet href>` or `<base href>` before the sign-in
 * anchor, and an attribute-wide match would navigate to the stylesheet.
 *
 * An earlier revision expressed this as `/<a\b[^>]*?\shref…/g`. That `[^>]*?`
 * re-expanded to end-of-string at EVERY `<a` when the body carried unterminated
 * tags, which is quadratic — and this runs on every failed response, from a body
 * the far side authored. Here each character is visited a bounded number of
 * times, and an unterminated tag ends the scan instead of restarting it.
 */
function* anchorHrefs(body: string): Generator<string> {
  for (const attrs of startTags(body, 'a')) {
    const href = hrefInTag(attrs)
    if (href !== null) yield href
  }
}

/**
 * Each start tag of *name*, as its attribute text, in one bounded pass.
 *
 * The single place this module walks a body the far side authored. Kept generic
 * because the second reader of that body arrived later as a regex and reintroduced
 * exactly the quadratic hazard this walk exists to avoid: `[^>]+` re-expanded to
 * end-of-string at every `<meta` in a body of unterminated tags, and on the full
 * body rather than the capped prefix. Here a name is compared over a fixed width,
 * an unterminated tag ENDS the scan, and `i` always advances.
 */
function* startTags(body: string, name: string): Generator<string> {
  const limit = Math.min(body.length, MAX_SCAN_BYTES)
  let i = 0
  while (i < limit) {
    const lt = body.indexOf('<', i)
    if (lt < 0 || lt >= limit) return
    const from = lt + 1
    if (body.slice(from, from + name.length).toLowerCase() !== name) {
      i = from
      continue
    }
    const attrsFrom = from + name.length
    // So `<a` is not read out of `<area`, nor `<meta` out of `<metadata`.
    if (isNameChar(body[attrsFrom])) { i = attrsFrom; continue }
    const gt = body.indexOf('>', attrsFrom)
    if (gt < 0) return
    yield body.slice(attrsFrom, gt)
    i = gt + 1
  }
}

/**
 * Whether this failed response is a proxy's sign-in page. All three signals are
 * required; any one of them alone also matches a gateway denial.
 *
 * 401 is accepted beside the measured 403 because it is the CANONICAL status for an
 * auth challenge, and an oauth2-proxy-style gate answering it was otherwise left in
 * the silent failure this module exists to end. The gateway's own 401 is unaffected:
 * it answers JSON, and an HTML DOCUMENT body is still required here.
 */
export function isEdgeAuthChallenge(
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
 * Does this page offer somewhere to SIGN IN — as opposed to merely somewhere to go?
 *
 * Accepting any safe http(s) URL was the bug: a firewall page's privacy or status
 * link satisfied it, so the caller was told a session had lapsed and stopped
 * retrying something no sign-in would have fixed. Hence a link that looks like
 * authentication, or a page that sends itself onward.
 *
 * Deliberately NOT origin-fenced: the measured gate's own link is cross-origin.
 * Nothing here is followed, so this answers only whether a way in is offered — a
 * different question from where it leads.
 */
export function hasSignInAffordance(body: string, base: string): boolean {
  let seen = 0
  for (const href of anchorHrefs(body)) {
    if (++seen > MAX_HREF_CANDIDATES) break
    try {
      const url = new URL(href, base)
      // Still required: the scheme gate rejects `javascript:` and credential userinfo.
      if (!safeHttpUrl(url.href)) continue
      if (namesAuthentication(url)) return true
    } catch { continue }
  }
  return redirectsItself(body)
}

/**
 * Words that appear in a sign-in route and not in a footer link.
 *
 * Matched on the PATH only. Deliberately excludes weaker candidates that a block
 * page's own links carry — `account`, `session`, `identity`, `help`, `support` —
 * because a false positive here is the whole defect: it reads a firewall block as a
 * lapsed session and stops retries that would have succeeded.
 */
const AUTH_PATH_WORDS = [
  'login', 'log-in', 'logon', 'signin', 'sign-in', 'sign_in',
  'auth', 'sso', 'oauth', 'openid', 'saml', 'idp',
]

/**
 * Query parameters a gate uses to carry where you were going.
 *
 * A challenge wants to send you back afterwards, so it names the refused request.
 * An ordinary footer link has no reason to.
 */
const RETURN_PARAMS = [
  'redirect', 'redirect_uri', 'redirect_url', 'rd', 'return', 'returnto',
  'return_to', 'next', 'continue', 'target',
]

/** Does this URL look like a way to authenticate, rather than just a way out? */
function namesAuthentication(url: URL): boolean {
  // Both halves: a gate's link is often an opaque path on an identity host, and
  // often the reverse. A block page's own links are neither.
  const where = `${url.hostname}${url.pathname}`.toLowerCase()
  for (const word of AUTH_PATH_WORDS) {
    if (where.includes(word)) return true
  }
  // A link that carries the refused request back is a challenge's shape even when
  // the route itself is opaque — a bare `/` with `?rd=…`, say.
  for (const param of RETURN_PARAMS) {
    if (url.searchParams.has(param)) return true
  }
  return false
}

/**
 * Does the page send itself onward without an anchor to click?
 *
 * A meta refresh or a scripted assignment is a way in that carries no `<a href>`, so
 * requiring an anchor reported such a challenge as a block page — naming a firewall
 * where the honest answer was a lapsed session. Only the PRESENCE of a redirect is
 * read; the destination is deliberately never parsed, because nothing in the body is
 * ever followed. Reloading the top document is what lets that redirect run for real.
 */
function redirectsItself(body: string): boolean {
  // No slice here: each scanner below applies MAX_SCAN_BYTES itself, so the cap has
  // exactly one home per scanner and a test can hold each of them to it.
  for (const attrs of startTags(body, 'meta')) {
    if (declaresRefresh(attrs)) return true
  }
  return assignsLocation(body)
}

/** Does this `<meta>`'s attribute text say `http-equiv=refresh`? */
function declaresRefresh(attrs: string): boolean {
  for (let i = 0; i < attrs.length; i++) {
    if (!isSpace(attrs[i])) continue
    if (attrs.slice(i + 1, i + 11).toLowerCase() !== 'http-equiv') continue
    let j = afterSpaces(attrs, i + 11)
    if (j < 0 || attrs[j] !== '=') continue
    j = afterSpaces(attrs, j + 1)
    if (j < 0) continue
    const quoted = attrs[j] === '"' || attrs[j] === "'"
    const from = quoted ? j + 1 : j
    // Fixed-width read. An unquoted value is legal, and the regex accepted it too.
    if (attrs.slice(from, from + 7).toLowerCase() === 'refresh') return true
  }
  return false
}

/**
 * Is position *k* the `=` of a real assignment?
 *
 * `location == here` and `location === here` are COMPARISONS, and `location => …`
 * is an arrow parameter; all three read a redirect out of code that navigates
 * nowhere. That mislabels a block page as a lapsed session, which is the same
 * misfire the affordance words above exist to prevent.
 */
const isAssignmentAt = (s: string, k: number): boolean =>
  s[k] === '=' && s[k + 1] !== '=' && s[k + 1] !== '>'

/** Does a script send the document onward by assigning `location`? */
function assignsLocation(body: string): boolean {
  const limit = Math.min(body.length, MAX_SCAN_BYTES)
  const lower = body.slice(0, limit).toLowerCase()
  const WORD = 'location'
  for (let i = lower.indexOf(WORD); i >= 0; i = lower.indexOf(WORD, i + 1)) {
    // An IDENTIFIER boundary, so `allocation =` is not a redirect while
    // `window.location` is. `isNameChar` counts `.`, so it cannot answer this.
    if (isIdentChar(lower[i - 1])) continue
    let j = afterSpaces(lower, i + WORD.length)
    if (j < 0) continue
    if (isAssignmentAt(lower, j)) return true
    if (lower[j] !== '.') continue
    j = afterSpaces(lower, j + 1)
    if (j < 0) continue
    for (const prop of ['href', 'replace', 'assign']) {
      if (lower.slice(j, j + prop.length) !== prop) continue
      const after = afterSpaces(lower, j + prop.length)
      if (after >= 0 && (isAssignmentAt(lower, after) || lower[after] === '(')) return true
    }
  }
  return false
}

const isIdentChar = (c: string | undefined): boolean =>
  c !== undefined && /[A-Za-z0-9_$]/.test(c)

/** What the response turned out to be; the caller words its message off this. */
export type ChallengeOutcome = 'expired' | 'framed' | 'no-signin'

/**
 * Name an interposed gate's challenge on a failed response, or null when this
 * response is not one.
 *
 * Only ADDS a name; the caller's `ApiError` is thrown either way.
 */
export function noteEdgeAuthChallenge(
  status: number,
  contentType: string | null,
  body: string,
): ChallengeOutcome | null {
  if (!isEdgeAuthChallenge(status, contentType, body)) return null
  if (typeof window === 'undefined') return 'expired'
  // A block page is an HTML 403 too. With nowhere to sign in, name the refusal
  // rather than diagnosing a lapse that may not have happened.
  if (!hasSignInAffordance(body, window.location.href)) return 'no-signin'
  // A framed document cannot complete the proxy's sign-in nested inside itself, so
  // its message has to send the reader elsewhere rather than to this one.
  if (isEmbeddedPane()) return 'framed'
  return 'expired'
}
