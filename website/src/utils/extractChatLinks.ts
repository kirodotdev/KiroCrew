import type { ChatMessage } from '../types'

export type LinkType = 'cr' | 'issue' | 'other'

export interface ExtractedLink {
  url: string
  type: LinkType
  label: string
  msgIdx: number
  fromMarkdown?: boolean
}

/** Classify a URL into a known type (generic git/PR, provider issue, or
 *  everything else). Issues are checked FIRST: they are a narrower shape than
 *  the PR pattern and must not be mislabelled "PR" in the Resources list. */
function classifyUrl(url: string): LinkType {
  // GitHub `/issues/5` and GitLab `/-/issues/5` share one trailing shape.
  if (/\/(?:issues)\/\d+/i.test(url)) return 'issue'
  // Generic pull request / code review detection (GitHub, GitLab, Bitbucket, etc.)
  if (/\/(?:pull|pull-requests|merge_requests)\/\d+/i.test(url) || /\/reviews\/[\w-]+/i.test(url)) return 'cr'
  return 'other'
}

/** Generate a fallback label from URL structure */
function fallbackLabel(url: string, type: LinkType): string {
  switch (type) {
    case 'cr': {
      const m = url.match(/\/(?:pull|pull-requests|merge_requests)\/(\d+)/i) || url.match(/\/reviews\/([\w-]+)/i)
      return m ? `#${m[1]}` : 'PR'
    }
    case 'issue': {
      const m = url.match(/\/issues\/(\d+)/i)
      return m ? `#${m[1]}` : 'Issue'
    }
    default: { try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url.slice(0, 30) } }
  }
}

/** Normalize URL for dedup (strip trailing slash) */
function normalizeUrl(url: string): string {
  return url.replace(/\/+$/, '')
}

/** Check if a URL is already seen (normalized exact match) */
function isDuplicate(url: string, seen: Set<string>): boolean {
  return seen.has(normalizeUrl(url))
}

// Match markdown links: [text](url)
const MD_LINK_RE = /\[([^\]]{1,100})\]\((https?:\/\/[^)]+)\)/g
// Match bare URLs not inside markdown link syntax (strip trailing punctuation)
const BARE_URL_RE = /https?:\/\/[\w][\w.-]*\.[a-z]{2,}(?:[\w/._?&#=-]*[\w/&#=-])?/g

export function extractChatLinks(messages: ChatMessage[]): ExtractedLink[] {
  const seen = new Set<string>()
  const links: ExtractedLink[] = []

  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i]
    if (msg.role !== 'user' && msg.role !== 'assistant') continue
    const text = msg.content || ''

    // Pass 1: Extract markdown links [label](url) — best labels
    MD_LINK_RE.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = MD_LINK_RE.exec(text)) !== null) {
      const [, rawLabel, url] = m
      if (isDuplicate(url, seen)) continue
      seen.add(normalizeUrl(url))
      const type = classifyUrl(url)
      const label = rawLabel.replace(/\*+/g, '').replace(/`/g, '').trim()
      links.push({ url, type, label: label.length >= 3 ? label.slice(0, 60) : fallbackLabel(url, type), msgIdx: i, fromMarkdown: true })
    }

    // Pass 2: Extract bare URLs with nearby context
    BARE_URL_RE.lastIndex = 0
    while ((m = BARE_URL_RE.exec(text)) !== null) {
      const url = m[0]
      if (isDuplicate(url, seen)) continue
      seen.add(normalizeUrl(url))
      const type = classifyUrl(url)
      links.push({ url, type, label: fallbackLabel(url, type), msgIdx: i })
    }
  }

  return links
}

/**
 * Canonical identity for a resource link, used to collapse the Resources list.
 *
 * `extractChatLinks` intentionally keeps distinct URLs distinct (so a link to a
 * specific PR diff/comment stays navigable), but the Resources panel should show
 * each underlying resource only ONCE. CR/PR links that point at the same request
 * (e.g. `.../pull/274`, `.../pull/274/files`, `.../pull/274#issuecomment-1`)
 * collapse to a single canonical key; so do issue links that point at the same
 * issue (`.../issues/5`, `.../issues/5#issuecomment-9`). Every other link dedups
 * on its trailing-slash-normalized URL.
 */
export function resourceIdentity(link: ExtractedLink): string {
  return resourceKey(link.url)
}

// Canonical request base for a CR/PR or issue URL — the part that identifies the
// resource itself, minus any sub-path/query/fragment. Single source of truth.
// The issue arm matters for the
// side panel: the Issues tab keys its rich entries on the canonical issue url,
// so a fragment-bearing mention must collapse to the same key or the link would
// show up twice — once with a panel, once in Resources.
const CR_BASE_RE = [
  /^(https?:\/\/[^/]+\/.*?\/(?:pull|pull-requests|merge_requests)\/\d+)/i,
  /^(https?:\/\/[^/]+\/.*?\/issues\/\d+)/i,
  /^(https?:\/\/[^/]+\/.*?\/reviews\/[\w-]+)/i,
]

/**
 * Lowercase only the scheme + authority (case-insensitive per RFC 3986) and
 * strip a trailing slash. The path/query/fragment are left case-INTACT because
 * they ARE case-sensitive — `/Docs` and `/docs` are different resources and must
 * not collapse into one Resources row.
 */
function normalizeAuthorityCase(url: string): string {
  const m = url.match(/^(https?:\/\/[^/]+)(.*)$/i)
  const normalized = m ? m[1].toLowerCase() + m[2] : url
  return normalizeUrl(normalized)
}

/**
 * Canonical dedup key for a resource URL (works for both extracted links and
 * Changes-tab source URLs, so both sides compare on the same identity). CR/PR
 * URLs collapse to their request base; all other URLs key on their
 * authority-normalized, trailing-slash-stripped form.
 */
export function resourceKey(url: string): string {
  for (const re of CR_BASE_RE) {
    const m = url.match(re)
    if (m) return normalizeAuthorityCase(m[1])
  }
  return normalizeAuthorityCase(url)
}

/**
 * Collapse duplicate resources so the Resources list never repeats an entry.
 * Keeps the first occurrence of each `resourceIdentity` — first wins because
 * markdown links (best labels) are extracted before bare URLs.
 */
export function dedupResourceLinks(links: ExtractedLink[]): ExtractedLink[] {
  const seen = new Set<string>()
  const out: ExtractedLink[] = []
  for (const link of links) {
    const key = resourceIdentity(link)
    if (seen.has(key)) continue
    seen.add(key)
    out.push(link)
  }
  return out
}
