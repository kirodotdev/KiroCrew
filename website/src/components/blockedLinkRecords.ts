/**
 * The blocked-link records a reply carries, outside React's tree: their shape and
 * validation, how a rendered link's address is matched to one, the card ids, and
 * the hosts allowed from a card that is still open. The records are the server's
 * decision (`security/blocked_links.py`) about which links in the reply's text
 * are not rendered live; nothing here changes the text. `RedactionCards.tsx`
 * draws the chip and the card from them and re-exports these names.
 */
import { useSyncExternalStore } from 'react'

/** A stable rule id, as the backend's verdict reports it. */
export const RULE_RE = /^[a-z0-9_]{1,48}$/

export interface BlockedLink {
  /** The address exactly as the reply holds it: what a rendered link is matched against. */
  url: string
  domain: string
  rule: string
  query_chars: number
  /** Why the card offers no Open once or Copy, or null when it may. */
  withheld: 'credential' | 'length' | null
}

/** Strict host and rule shapes, mirroring the backend's own gates. */
const BLOCKED_LINK_HOST_RE =
  /^(?:[a-z0-9._-]{1,253}\.[a-z]{2,63}|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:.]{1,45}\])$/i
const BLOCKED_LINK_KEYS = ['url', 'domain', 'rule', 'query_chars', 'withheld']
const BLOCKED_LINK_URL_MAX = 8192

/**
 * The address a record may open, or null. Checked on the way in AND again at
 * the click: an http(s) address whose host is exactly the record's domain and
 * that carries no userinfo, so the host the card names is the host that opens.
 */
export function openableBlockedLinkUrl(url: unknown, domain: string): string | null {
  if (typeof url !== 'string' || url.length > BLOCKED_LINK_URL_MAX) return null
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return null
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null
  if (parsed.username !== '' || parsed.password !== '') return null
  if (parsed.hostname.toLowerCase() !== domain.toLowerCase()) return null
  return url
}


/**
 * Keep only well-formed blocked-link records, dropping a bad one individually.
 * The server computes them from the text it sends (`security/blocked_links.py`);
 * a record whose address the card could not open on its own host keeps its link
 * blocked and offers nothing to open.
 */
export function normalizeBlockedLinks(raw: unknown): BlockedLink[] {
  if (!Array.isArray(raw)) return []
  const out: BlockedLink[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const r = item as Record<string, unknown>
    const keys = Object.keys(r)
    if (keys.length !== BLOCKED_LINK_KEYS.length || !BLOCKED_LINK_KEYS.every(k => k in r)) continue
    const { url, domain, rule, query_chars: qc, withheld } = r
    if (typeof domain !== 'string' || !BLOCKED_LINK_HOST_RE.test(domain)) continue
    if (typeof rule !== 'string' || !RULE_RE.test(rule)) continue
    if (typeof qc !== 'number' || !Number.isInteger(qc) || qc < 0) continue
    if (typeof url !== 'string' || url === '') continue
    if (withheld !== null && withheld !== 'credential' && withheld !== 'length') continue
    const host = domain.toLowerCase()
    const openable = withheld === null && openableBlockedLinkUrl(url, host) !== null
    out.push({ url, domain: host, rule, query_chars: qc, withheld: openable ? null : (withheld ?? 'credential') })
  }
  return out
}

/** The distinct records for one domain, in first-appearance order. */
export function blockedLinksForDomain(records: readonly BlockedLink[], domain: string): BlockedLink[] {
  const seen = new Set<string>()
  const out: BlockedLink[] = []
  for (const r of records) {
    if (r.domain !== domain) continue
    const sig = JSON.stringify([r.rule, r.url, r.query_chars, r.withheld])
    if (seen.has(sig)) continue
    seen.add(sig)
    out.push(r)
  }
  return out
}

/** The ASCII punctuation a CommonMark backslash escapes. */
const ESCAPABLE = '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'
/** Whether *c* is an ASCII decimal digit. */
const isDigit = (c: string): boolean => c >= '0' && c <= '9'
/** Whether *c* is an ASCII hexadecimal digit. */
const isHexDigit = (c: string): boolean => isDigit(c) || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
/** Whether *c* may appear in a named character reference: an ASCII letter or digit. */
const isNameChar = (c: string): boolean => isDigit(c) || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')

/** The text a numeric reference body (`38`, `x26`) stands for, or null. */
function numericReference(body: string): string | null {
  const hex = body[0] === 'x' || body[0] === 'X'
  const digits = hex ? body.slice(1) : body
  const allowed = hex ? isHexDigit : isDigit
  if (digits === '' || digits.length > (hex ? 6 : 7) || ![...digits].every(allowed)) return null
  const code = Number.parseInt(digits, hex ? 16 : 10)
  // CommonMark reads NUL, a surrogate and anything past Unicode as U+FFFD.
  const valid = code > 0 && code <= 0x10ffff && (code < 0xd800 || code > 0xdfff)
  return String.fromCodePoint(valid ? code : 0xfffd)
}

/** The text a named reference (`&amp;`) stands for, or null for a name HTML does not define. */
function namedReference(reference: string): string | null {
  if (typeof DOMParser === 'undefined') return null
  // Only `&`, name characters and `;` reach the parser, so it decodes one reference and
  // nothing else; the document is never attached and runs nothing.
  const decoded = new DOMParser().parseFromString(reference, 'text/html').body.textContent ?? reference
  return decoded === reference ? null : decoded
}

/**
 * An address as the renderer reads a link destination: CommonMark decodes its
 * backslash escapes and its character references (`&amp;`, `&#38;`, `&#x26;`)
 * before the link reaches the matcher, so a record of the address as written is
 * matched in that form too.
 */
function destinationDecoded(url: string): string {
  let out = ''
  let i = 0
  while (i < url.length) {
    const c = url[i]
    if (c === '\\' && i + 1 < url.length && ESCAPABLE.includes(url[i + 1])) {
      out += url[i + 1]
      i += 2
      continue
    }
    if (c === '&') {
      const end = url.indexOf(';', i + 1)
      const body = end > i + 1 && end - i <= 33 ? url.slice(i + 1, end) : ''
      const decoded =
        body[0] === '#'
          ? numericReference(body.slice(1))
          : body !== '' && [...body].every(isNameChar)
            ? namedReference(url.slice(i, end + 1))
            : null
      if (decoded !== null) {
        out += decoded
        i = end + 1
        continue
      }
    }
    out += c
    i += 1
  }
  return out
}

/** The comparable forms of an address: as written, as the renderer decodes a
 *  destination, as the browser parses each, and each with its escapes decoded,
 *  so a link the renderer decoded or escaped again still meets the record of
 *  the address the reply holds. */
function addressForms(url: string): string[] {
  const forms = new Set([url, destinationDecoded(url)])
  for (const form of [...forms]) {
    try {
      forms.add(new URL(form).href)
    } catch {
      // Not an absolute address: compared as written.
    }
  }
  for (const form of [...forms]) {
    try {
      forms.add(decodeURIComponent(form))
    } catch {
      // A stray escape: compared as written.
    }
  }
  return [...forms]
}

/** What may follow an address in prose and still be the end of its link. */
const LINK_END = '.,;:!?)]}>\'"*_~'

/**
 * The record a rendered link (an anchor's href, a bare address the renderer
 * linked, an image's src) stands for, or null when it may render live. A link
 * matches a record when it holds the record's address, or when it is that
 * address cut short only by the punctuation that ends a sentence, so the
 * renderer and the server reading a link's end differently never leaves it live.
 */
export function blockedLinkFor(href: unknown, records: readonly BlockedLink[]): BlockedLink | null {
  if (typeof href !== 'string' || href === '' || records.length === 0) return null
  const links = addressForms(href)
  for (const record of records) {
    for (const want of addressForms(record.url)) {
      for (const have of links) {
        if (have.includes(want)) return record
        if (want.startsWith(have) && [...want.slice(have.length)].every(c => LINK_END.includes(c))) return record
      }
    }
  }
  return null
}

// ── hosts allowed from an open card ─────────────────────────────────────

/**
 * A host allowed from a card that is still open. Allowing reloads the reply,
 * and the server then serves it without this host's records, so its links
 * render live and the card would lose what it renders from. The hold
 * keeps those records, and the "Allowed · Undo" outcome, for this one reply
 * until its card closes (a reply with no message time keeps it while mounted).
 */
export interface AllowHold {
  workspace: string
  records: readonly BlockedLink[]
  outcome: 'allowed' | 'undone'
  /** The card the host was allowed from. The reloaded reply reopens that
   *  card id; when no block lists it, no card shows. */
  cardId: string
  /** The card entry Allow was pressed from, the one that shows the outcome. */
  entry: number
}

/**
 * The id of a blocked-link card. A card opens under the block holding the
 * chip that opened it, so the id names that block as well as the host: one
 * host blocked in four paragraphs has four possible cards, and one click
 * opens one of them. The block part is `<rendered block>.<top-level block>`.
 * Allow swaps an inline chip for the inline link it stood for, so the block
 * keeps its place across the reload and the held card reopens under it.
 */
export function blockedLinkCardId(domain: string, block: string): string {
  return `rx-link-${domain}~${block}`
}

/** The host a blocked-link card id names (a host never contains `~`). */
export function blockedLinkCardDomain(id: string): string {
  const rest = id.slice('rx-link-'.length)
  const cut = rest.indexOf('~')
  return cut < 0 ? rest : rest.slice(0, cut)
}

export const EMPTY_HOLDS: ReadonlyMap<string, AllowHold> = new Map()
let allowHolds: ReadonlyMap<string, ReadonlyMap<string, AllowHold>> = new Map()
const holdListeners = new Set<() => void>()

function subscribeHolds(cb: () => void): () => void {
  holdListeners.add(cb)
  return () => { holdListeners.delete(cb) }
}

/** Set or clear the hold for one domain of one reply. */
export function setAllowHold(replyKey: string, domain: string, hold: AllowHold | null): void {
  const cur = allowHolds.get(replyKey) ?? EMPTY_HOLDS
  if (hold === null && !cur.has(domain)) return
  const next = new Map(cur)
  if (hold === null) next.delete(domain)
  else next.set(domain, hold)
  const all = new Map(allowHolds)
  if (next.size) all.set(replyKey, next)
  else all.delete(replyKey)
  allowHolds = all
  holdListeners.forEach(l => l())
}

/** The holds of one reply, by domain, outside React (a mount's first state). */
export function allowHoldsOf(replyKey: string): ReadonlyMap<string, AllowHold> | undefined {
  return allowHolds.get(replyKey)
}

/** The holds of one reply, by domain. */
export function useAllowHolds(replyKey: string | undefined): ReadonlyMap<string, AllowHold> {
  return useSyncExternalStore(subscribeHolds, () => (replyKey && allowHolds.get(replyKey)) || EMPTY_HOLDS)
}

/** The held domain an anchor points at (matched with and without its port),
 *  or null. */
export function heldAnchorDomain(href: unknown, held: ReadonlySet<string>): string | null {
  if (typeof href !== 'string' || held.size === 0) return null
  try {
    const u = new URL(href)
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null
    for (const d of [u.host.toLowerCase(), u.hostname.toLowerCase()]) if (held.has(d)) return d
    return null
  } catch {
    return null
  }
}
