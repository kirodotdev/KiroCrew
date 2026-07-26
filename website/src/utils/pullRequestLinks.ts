import type { ChatMessage } from '../types'
import { safeSetItem } from './safeStorage'

export type PullRequestProvider = 'github' | 'gitlab'

export interface PullRequestLink {
  url: string
  provider: PullRequestProvider
  number: number
  repo: string
}

/**
 * First-mention attribution: whoever mentioned a PR FIRST owns its
 * classification. 'user' means the person referenced it for context (it belongs
 * in Resources, not Changes); 'agent' means the assistant / a tool / thinking
 * output surfaced or created it (a real Change). Because the dedup map keeps the
 * first occurrence of each URL, an agent later echoing a user-referenced PR
 * cannot reclassify it — the user's earlier mention stands.
 */
type MentionRole = 'agent' | 'user'
interface AttributedLink extends PullRequestLink {
  mentionedBy: MentionRole
}

/**
 * Emit only PRs whose FIRST mention came from the agent. User-referenced PRs are
 * kept in the dedup map (so a later agent echo is still recognized as a
 * duplicate and skipped) but excluded here — they surface in the Files-tab
 * Resources list instead. Clean PullRequestLink objects are rebuilt so the
 * public shape never leaks the internal attribution field.
 */
function emitChangeSources(found: Map<string, AttributedLink>): PullRequestLink[] {
  const out: PullRequestLink[] = []
  for (const link of found.values()) {
    if (link.mentionedBy === 'user') continue
    out.push({ url: link.url, provider: link.provider, number: link.number, repo: link.repo })
  }
  return out
}

export const MAX_PULL_REQUEST_SOURCES = 64

const SEEN_SOURCES_STORAGE_KEY = 'mc-pr-source-seen-v1'
const MAX_PERSISTED_SOURCE_SLOTS = 32
const MAX_PERSISTED_SOURCE_URLS = 512
const MAX_PERSISTED_SOURCE_URL_LENGTH = 2048
const MAX_PERSISTED_SLOT_LENGTH = 512

// Cheap URL candidate scan; each candidate is then parsed with the URL API and
// validated with linear string ops. This is an ALLOWLIST of URL-safe ASCII
// characters (RFC 3986 unreserved + gen-/sub-delims + '%'), minus the bracket
// and quote characters we deliberately treat as delimiters -- ()[]{}"' -- so a
// PR wrapped in parens or a markdown link still parses. Because it is an
// allowlist, the scan stops at the first byte that cannot appear in a URL,
// including every CJK ideograph and fullwidth punctuation mark. A URL packed
// against CJK text with no ASCII space -- routine in Chinese/Japanese/Korean
// messages, e.g. a fullwidth "(" placed right after the PR number -- therefore
// no longer swallows the trailing text and fails the numeric-tail check.
// extractChatLinks.ts uses the same allowlist approach for its bare-URL scan.
// Still a single greedy character class (no lazy quantifier), preserving the
// linear, no-backtracking shape CodeQL flagged on the old backend parser.
const URL_CANDIDATE_RE = /https:\/\/[A-Za-z0-9!#$%&*+,.\/:;=?@_~-]+/g

function parseCandidate(raw: string): PullRequestLink | null {
  // Trim trailing punctuation and markdown emphasis (**bold**, *italic*,
  // `code`, _underscore_, ~~strike~~) that the candidate scan may have
  // swallowed — agent messages routinely wrap PR URLs in emphasis, and a
  // trailing "**" makes the numeric tail check fail silently. Safe for
  // this parser: a valid PR/MR URL always ends in a numeric component, so
  // these characters can never be part of a legitimate link tail.
  const cleaned = raw.replace(/[.,!?;:*_~`]+$/, '')
  let url: URL
  try {
    url = new URL(cleaned)
  } catch {
    return null
  }
  const host = url.hostname.toLowerCase().replace(/^www\./, '')
  const path = url.pathname.replace(/\/+$/, '')
  if (host === 'github.com') {
    const parts = path.split('/').filter(Boolean) // [owner, repo, 'pull', number]
    if (parts.length !== 4 || parts[2] !== 'pull' || !/^\d+$/.test(parts[3])) return null
    return {
      url: `https://github.com/${parts[0]}/${parts[1]}/pull/${parts[3]}`,
      provider: 'github',
      number: Number(parts[3]),
      repo: parts[1],
    }
  }
  if (host === 'gitlab.com') {
    const marker = '/-/merge_requests/'
    const idx = path.lastIndexOf(marker)
    if (idx <= 0) return null
    const project = path.slice(1, idx)
    const number = path.slice(idx + marker.length)
    if (!project || !/^\d+$/.test(number)) return null
    return {
      url: `https://gitlab.com${path}`,
      provider: 'gitlab',
      number: Number(number),
      repo: project.split('/').at(-1) || project,
    }
  }
  return null
}

function linksInMessage(
  message: ChatMessage | undefined,
  limit = MAX_PULL_REQUEST_SOURCES,
): AttributedLink[] {
  if (message?.role === 'streaming' || message?.role === 'chunk' || limit <= 0) return []
  // Non-transient roles other than 'user' (assistant, tool, thinking, …) are all
  // agent output — a PR URL in a tool result (e.g. `gh pr create`) is as much an
  // agent-surfaced Change as one the assistant types.
  const mentionedBy: MentionRole = message?.role === 'user' ? 'user' : 'agent'
  const found = new Map<string, AttributedLink>()
  const rawContent = message?.content
  const content = typeof rawContent === 'string' ? rawContent : ''
  URL_CANDIDATE_RE.lastIndex = 0
  for (const match of content.matchAll(URL_CANDIDATE_RE)) {
    const link = parseCandidate(match[0])
    if (!link || found.has(link.url)) continue
    found.set(link.url, { ...link, mentionedBy })
    if (found.size >= limit) break
  }
  return [...found.values()]
}

function roleCount(found: Map<string, AttributedLink>, role: MentionRole): number {
  let n = 0
  for (const link of found.values()) if (link.mentionedBy === role) n += 1
  return n
}

function addLinks(
  found: Map<string, AttributedLink>,
  links: AttributedLink[],
): void {
  for (const link of links) {
    if (found.has(link.url)) continue
    // Cap each role INDEPENDENTLY. The emitted Change sources are the agent
    // links, so counting user-referenced links against a single shared limit
    // let a flood of pasted PRs exhaust the budget and starve every later
    // agent-created PR out of the Changes tab. User links must still be
    // retained (bounded) so a later agent echo of a user-first PR stays
    // classified as a Resource — hence a per-role cap rather than dropping them.
    if (roleCount(found, link.mentionedBy) >= MAX_PULL_REQUEST_SOURCES) continue
    found.set(link.url, link)
  }
}

export function extractPullRequestLinks(messages: ChatMessage[]): PullRequestLink[] {
  const found = new Map<string, AttributedLink>()
  for (const message of messages) {
    addLinks(found, linksInMessage(message))
    // Once MAX agent sources are captured, no further message can add an emitted
    // Change source, so stop scanning (user links past this point are moot).
    if (roleCount(found, 'agent') >= MAX_PULL_REQUEST_SOURCES) break
  }
  return emitChangeSources(found)
}

function sameMessagePrefix(
  previous: ChatMessage[],
  next: ChatMessage[],
  length: number,
): boolean {
  for (let index = 0; index < length; index += 1) {
    if (previous[index] !== next[index]) return false
  }
  return true
}

/**
 * Incremental per-slot link index. Durable prefixes remain settled while the
 * changing tail is rescanned. Every chunk/streaming message stays transient
 * regardless of position, so appended tool/thinking events cannot prematurely
 * publish a numeric URL that an earlier stream is still extending.
 */
export class PullRequestLinkIndex {
  private slot: string | null = null
  private messages: ChatMessage[] = []
  private settled = new Map<string, AttributedLink>()
  private tail: AttributedLink[] = []
  private tailTransient = false
  private result: PullRequestLink[] = []

  update(slot: string | null, messages: ChatMessage[]): PullRequestLink[] {
    if (slot !== this.slot) {
      this.rebuild(slot, messages)
      return this.result
    }
    if (messages === this.messages) return this.result

    const previous = this.messages
    const previousLength = previous.length
    const nextLength = messages.length
    const appended = nextLength > previousLength
      && sameMessagePrefix(previous, messages, previousLength)
    const tailOnlyChanged = nextLength === previousLength
      && nextLength > 0
      && messages[nextLength - 1] !== previous[previousLength - 1]
      && sameMessagePrefix(previous, messages, nextLength - 1)

    if (appended) {
      if (!this.tailTransient) addLinks(this.settled, this.tail)
      for (let index = previousLength; index < nextLength - 1; index += 1) {
        addLinks(this.settled, linksInMessage(messages[index]))
        if (roleCount(this.settled, 'agent') >= MAX_PULL_REQUEST_SOURCES) break
      }
      this.setTail(messages[nextLength - 1])
      this.messages = messages
      this.materialize()
    } else if (tailOnlyChanged) {
      this.setTail(messages[nextLength - 1])
      this.messages = messages
      this.materialize()
    } else {
      this.rebuild(slot, messages)
    }
    return this.result
  }

  private rebuild(slot: string | null, messages: ChatMessage[]): void {
    this.slot = slot
    this.messages = messages
    this.settled = new Map()
    for (let index = 0; index < Math.max(0, messages.length - 1); index += 1) {
      addLinks(this.settled, linksInMessage(messages[index]))
      if (roleCount(this.settled, 'agent') >= MAX_PULL_REQUEST_SOURCES) break
    }
    this.setTail(messages.at(-1))
    this.materialize()
  }

  private setTail(message: ChatMessage | undefined): void {
    this.tailTransient = message?.role === 'streaming' || message?.role === 'chunk'
    this.tail = linksInMessage(message)
  }

  private materialize(): void {
    const found = new Map(this.settled)
    addLinks(found, this.tail)
    this.result = emitChangeSources(found)
  }
}

/** Record source URLs per slot and report only links that slot has never seen. */
export function recordNewPullRequestLinks(
  seenBySlot: Map<string, Set<string>>,
  slot: string | null,
  links: PullRequestLink[],
): boolean {
  if (!slot) return false
  const seen = seenBySlot.get(slot) ?? new Set<string>()
  let hasNew = false
  for (const link of links) {
    if (seen.has(link.url)) continue
    if (seen.size >= MAX_PULL_REQUEST_SOURCES) break
    seen.add(link.url)
    hasNew = true
  }
  seenBySlot.delete(slot)
  seenBySlot.set(slot, seen)
  return hasNew
}

/**
 * Restore the bounded seen-source index used to distinguish live discovery
 * from historical transcript hydration after ChatPage remounts or reloads.
 * localStorage is untrusted input, so malformed slots and non-canonical URLs
 * are ignored instead of entering source-selection state.
 */
export function loadSeenPullRequestLinks(): Map<string, Set<string>> {
  if (typeof localStorage === 'undefined') return new Map()
  let parsed: unknown
  try {
    parsed = JSON.parse(localStorage.getItem(SEEN_SOURCES_STORAGE_KEY) || '[]')
  } catch {
    return new Map()
  }
  if (!Array.isArray(parsed)) return new Map()

  const restored: Array<[string, Set<string>]> = []
  let remainingUrls = MAX_PERSISTED_SOURCE_URLS
  for (
    let index = parsed.length - 1;
    index >= 0 && restored.length < MAX_PERSISTED_SOURCE_SLOTS && remainingUrls > 0;
    index -= 1
  ) {
    const entry = parsed[index]
    if (!Array.isArray(entry) || entry.length !== 2) continue
    const [slot, urls] = entry
    if (
      typeof slot !== 'string'
      || !slot
      || slot.length > MAX_PERSISTED_SLOT_LENGTH
      || !Array.isArray(urls)
    ) continue

    const seen = new Set<string>()
    for (const value of urls) {
      if (
        typeof value !== 'string'
        || value.length > MAX_PERSISTED_SOURCE_URL_LENGTH
        || seen.size >= MAX_PULL_REQUEST_SOURCES
        || seen.size >= remainingUrls
      ) continue
      const source = parseCandidate(value)
      if (source?.url === value) seen.add(value)
    }
    if (!seen.size) continue
    remainingUrls -= seen.size
    restored.unshift([slot, seen])
  }
  return new Map(restored)
}

/** Persist recent per-slot seen sources without allowing unbounded growth. */
export function persistSeenPullRequestLinks(
  seenBySlot: Map<string, Set<string>>,
): boolean {
  const persisted: Array<[string, string[]]> = []
  const entries = [...seenBySlot.entries()]
  let remainingUrls = MAX_PERSISTED_SOURCE_URLS
  for (
    let index = entries.length - 1;
    index >= 0 && persisted.length < MAX_PERSISTED_SOURCE_SLOTS && remainingUrls > 0;
    index -= 1
  ) {
    const [slot, seen] = entries[index]
    if (!slot || slot.length > MAX_PERSISTED_SLOT_LENGTH) continue
    const urls: string[] = []
    for (const value of seen) {
      if (
        value.length > MAX_PERSISTED_SOURCE_URL_LENGTH
        || urls.length >= MAX_PULL_REQUEST_SOURCES
        || urls.length >= remainingUrls
      ) continue
      const source = parseCandidate(value)
      if (source?.url === value) urls.push(value)
    }
    if (!urls.length) continue
    remainingUrls -= urls.length
    persisted.unshift([slot, urls])
  }
  return safeSetItem(SEEN_SOURCES_STORAGE_KEY, JSON.stringify(persisted))
}
