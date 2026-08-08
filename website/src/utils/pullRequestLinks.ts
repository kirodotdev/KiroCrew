import type { ChatMessage } from '../types'
import { safeSetItem } from './safeStorage'

export type PullRequestProvider = 'github' | 'gitlab'

/**
 * What a mentioned provider URL points at. 'change' is a pull request / merge
 * request; 'issue' is a GitHub issue / GitLab issue. Both kinds are extracted by
 * ONE scan and share one dedup map (a session routinely mentions both, and the
 * per-role cap must be a single budget); callers split the result by `kind` to
 * feed the Changes and Issues panels.
 */
export type SourceLinkKind = 'change' | 'issue'

export interface PullRequestLink {
  url: string
  provider: PullRequestProvider
  number: number
  repo: string
  kind: SourceLinkKind
}

/** Split one extraction result into its two kinds, preserving first-seen order. */
export function partitionSourceLinks(
  links: readonly PullRequestLink[],
): { changes: PullRequestLink[]; issues: PullRequestLink[] } {
  const changes: PullRequestLink[] = []
  const issues: PullRequestLink[] = []
  for (const link of links) (link.kind === 'issue' ? issues : changes).push(link)
  return { changes, issues }
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
 * Emit only links whose FIRST mention came from the agent. User-referenced links
 * are kept in the dedup map (so a later agent echo is still recognized as a
 * duplicate and skipped) but excluded here — they surface in the Files-tab
 * Resources list instead. Clean PullRequestLink objects are rebuilt so the
 * public shape never leaks the internal attribution field. Both kinds
 * (pull requests and issues) pass through unchanged; the caller splits them.
 */
function emitChangeSources(found: Map<string, AttributedLink>): PullRequestLink[] {
  const out: PullRequestLink[] = []
  for (const link of found.values()) {
    if (link.mentionedBy === 'user') continue
    out.push({
      url: link.url,
      provider: link.provider,
      number: link.number,
      repo: link.repo,
      kind: link.kind,
    })
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
// linear, no-backtracking shape.
const URL_CANDIDATE_RE = /https:\/\/[A-Za-z0-9!#$%&*+,.\/:;=?@_~-]+/g

/** Normalize the configured self-hosted GitLab hosts to a lookup set.
 * Entries arrive from dashboard config (already `host[:port]`), never from chat
 * content, and are matched exactly — the backend re-validates every URL before
 * a provider CLI runs, so this only decides which links become source tabs.
 * An explicit `:443` is dropped to match the URL API (and the backend), which
 * omits the default HTTPS port. */
export function gitlabHostSet(hosts: readonly string[] | undefined): ReadonlySet<string> {
  return new Set(
    (hosts ?? [])
      .map(host => host.trim().toLowerCase().replace(/:443$/, ''))
      .filter(Boolean),
  )
}

const NO_GITLAB_HOSTS: ReadonlySet<string> = new Set<string>()

/** GitLab path markers, in match order. Both a merge request and an issue live
 * under the project's `/-/` namespace and end in a numeric id, so the marker
 * that matches also decides the link's kind. */
const GITLAB_MARKERS: ReadonlyArray<{ marker: string; kind: SourceLinkKind }> = [
  { marker: '/-/merge_requests/', kind: 'change' },
  { marker: '/-/issues/', kind: 'issue' },
]

function gitlabLink(origin: string, path: string): PullRequestLink | null {
  for (const { marker, kind } of GITLAB_MARKERS) {
    const idx = path.lastIndexOf(marker)
    if (idx <= 0) continue
    const project = path.slice(1, idx)
    const number = path.slice(idx + marker.length)
    if (!project || !/^\d+$/.test(number)) continue
    return {
      url: `https://${origin}${path}`,
      provider: 'gitlab',
      number: Number(number),
      repo: project.split('/').at(-1) || project,
      kind,
    }
  }
  return null
}

/** GitHub's third path segment decides the kind: `/pull/12` vs `/issues/12`. */
function githubSegmentKind(segment: string): SourceLinkKind | null {
  // An explicit comparison, not an object-literal lookup: a Record index also
  // resolves INHERITED keys, so `/owner/repo/constructor/5` would survive the
  // falsy-kind guard and yield a link whose kind is neither 'change' nor
  // 'issue' -- routed to Changes, then rejected by the backend with a 400.
  if (segment === 'pull') return 'change'
  if (segment === 'issues') return 'issue'
  return null
}

function parseCandidate(
  raw: string,
  gitlabHosts: ReadonlySet<string> = NO_GITLAB_HOSTS,
  anyGitlabHost = false,
): PullRequestLink | null {
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
  // Strip a trailing dot (absolute-FQDN form) so the parsed host matches the
  // allowlist, whose entries are dot-normalized by the config loader; the two
  // sides must agree or a self-hosted MR link is silently dropped.
  const host = url.hostname
    .toLowerCase()
    .replace(/\.+$/, '')
    .replace(/^www\./, '')
  const path = url.pathname.replace(/\/+$/, '')
  if (host === 'github.com') {
    // [owner, repo, 'pull' | 'issues', number]
    const parts = path.split('/').filter(Boolean)
    if (parts.length !== 4 || !/^\d+$/.test(parts[3])) return null
    const kind = githubSegmentKind(parts[2])
    if (!kind) return null
    return {
      url: `https://github.com/${parts[0]}/${parts[1]}/${parts[2]}/${parts[3]}`,
      provider: 'github',
      number: Number(parts[3]),
      repo: parts[1],
      kind,
    }
  }
  if (host === 'gitlab.com') return gitlabLink('gitlab.com', path)
  // Self-managed instance: the exact host (with its port, when the URL carries
  // one) must be allowlisted. `www.` is not stripped here — the allowlist and
  // the backend both match the host as configured.
  const rawHost = url.hostname.toLowerCase().replace(/\.+$/, '')
  const hostWithPort = url.port ? `${rawHost}:${url.port}` : rawHost
  if (anyGitlabHost || gitlabHosts.has(hostWithPort)) return gitlabLink(hostWithPort, path)
  return null
}

/** True when a stored URL is already in canonical form.
 *
 * Used only for the persisted "seen sources" bookkeeping set, which is NOT an
 * authorization decision — whether a host may be loaded is decided at extraction
 * time and re-validated by the backend. Applying the allowlist here would drop
 * every self-hosted URL from the seen set, so after a reload those MRs would look
 * new again and reopen the Changes panel. */
function isCanonicalStoredUrl(value: string): boolean {
  return parseCandidate(value, NO_GITLAB_HOSTS, true)?.url === value
}

function linksInMessage(
  message: ChatMessage | undefined,
  gitlabHosts: ReadonlySet<string> = NO_GITLAB_HOSTS,
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
    const link = parseCandidate(match[0], gitlabHosts)
    if (!link || found.has(link.url)) continue
    found.set(link.url, { ...link, mentionedBy })
    if (found.size >= limit) break
  }
  return [...found.values()]
}

/** Parse ONE url into a source link, or null when it is not a pull request /
 *  merge request / issue on a permitted host.
 *
 *  Exposed for callers that hold a url but no transcript — the sidebar chips,
 *  whose links the BACKEND scanned out of the slot's messages. Going through the
 *  same parser as the transcript extractor means such a caller gets the identical
 *  canonical shape (canonicalised url, provider, number, repo, kind) instead of a
 *  second, drifting hand-rolled one. */
export function parseSourceLinkUrl(
  url: string,
  gitlabHosts: readonly string[] = [],
): PullRequestLink | null {
  return parseCandidate(url, gitlabHostSet(gitlabHosts))
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

export function extractPullRequestLinks(
  messages: ChatMessage[],
  gitlabHosts: readonly string[] = [],
): PullRequestLink[] {
  const hosts = gitlabHostSet(gitlabHosts)
  const found = new Map<string, AttributedLink>()
  for (const message of messages) {
    addLinks(found, linksInMessage(message, hosts))
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
  private hosts: ReadonlySet<string> = NO_GITLAB_HOSTS
  private hostsKey = ''

  update(
    slot: string | null,
    messages: ChatMessage[],
    gitlabHosts: readonly string[] = [],
  ): PullRequestLink[] {
    // An operator adding a self-managed host mid-session must retro-detect the
    // MRs already in the transcript, so a changed allowlist forces a full
    // rescan rather than only applying to future messages.
    const hostsKey = [...gitlabHostSet(gitlabHosts)].sort().join(',')
    if (hostsKey !== this.hostsKey) {
      this.hostsKey = hostsKey
      this.hosts = gitlabHostSet(gitlabHosts)
      this.rebuild(slot, messages)
      return this.result
    }
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
        addLinks(this.settled, linksInMessage(messages[index], this.hosts))
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
      addLinks(this.settled, linksInMessage(messages[index], this.hosts))
      if (roleCount(this.settled, 'agent') >= MAX_PULL_REQUEST_SOURCES) break
    }
    this.setTail(messages.at(-1))
    this.materialize()
  }

  private setTail(message: ChatMessage | undefined): void {
    this.tailTransient = message?.role === 'streaming' || message?.role === 'chunk'
    this.tail = linksInMessage(message, this.hosts)
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

/** Per-slot, per-kind links a sidebar chip explicitly revealed into the panel. */
export type RevealedSources = Record<string, Partial<Record<SourceLinkKind, PullRequestLink>>>

const REVEALED_SOURCE_PREFIX = 'mc-pr-source-revealed:'

/** `mc-pr-source-revealed:<kind>:<slot>` — kind first so the slot is the whole
 *  remainder and needs no escaping, exactly like `selectionStorageKey`. */
function revealedStorageKey(slot: string, kind: SourceLinkKind): string {
  return `${REVEALED_SOURCE_PREFIX}${kind}:${slot}`
}

function parseRevealedStorageKey(key: string): { slot: string; kind: SourceLinkKind } | null {
  if (!key.startsWith(REVEALED_SOURCE_PREFIX)) return null
  const rest = key.slice(REVEALED_SOURCE_PREFIX.length)
  const split = rest.indexOf(':')
  if (split <= 0) return null
  const kind = rest.slice(0, split)
  const slot = rest.slice(split + 1)
  if (!slot || slot.length > MAX_PERSISTED_SLOT_LENGTH) return null
  if (kind !== 'change' && kind !== 'issue') return null
  return { slot, kind }
}

interface StoredRevealed {
  slot: string
  kind: SourceLinkKind
  link: PullRequestLink
  at: number
}

/** Largest raw entry worth handing to JSON.parse — a url plus its `{u,t}` wrapper
 *  with room to spare. Bounds the parse itself rather than only rejecting the url
 *  afterwards. */
const MAX_STORED_REVEALED_BYTES = MAX_PERSISTED_SOURCE_URL_LENGTH + 128
/** Upper bound on a plausible recency stamp (2100-01-01Z). Storage is untrusted
 *  and `Number.isFinite` alone admits `Number.MAX_VALUE`; because
 *  `MAX_VALUE + 1 === MAX_VALUE`, such an entry could tie a genuine write and —
 *  being earlier in a stable sort — cap the genuine one out. An absolute bound is
 *  used rather than "not in the future" because a clock stepping BACKWARD is a
 *  real scenario (`commitRevealedSource` guards it), and a future-relative rule
 *  would make every previously-written real stamp look crafted. */
const MAX_PLAUSIBLE_STAMP_MS = 4102444800000
/** Highest stamp a genuine write may take. Strictly below the trusted ceiling so a
 *  crafted entry sitting AT the ceiling is demoted rather than tying — otherwise 32
 *  entries stamped exactly `MAX_PLAUSIBLE_STAMP_MS` would tie a clamped genuine
 *  write and, being earlier in a stable sort, keep it out of the read cap. */
const MAX_WRITABLE_STAMP_MS = MAX_PLAUSIBLE_STAMP_MS - 1

/**
 * Enumerate and validate every stored revealed link.
 *
 * ONE KEY PER FIELD, for the same reason the selection store next door uses one:
 * a popped-out session shares this localStorage, and a whole-map write publishes
 * this window's stale view of the slots it is not looking at — so the later write
 * would delete a sibling window's reveal, and the reload it was meant to survive
 * would silently swap the panel after all.
 *
 * Only the URL is stored, never the parsed shape: localStorage is untrusted, so
 * every entry is re-derived by the same parser that built it. The host allowlist
 * is deliberately NOT applied (same reasoning as `isCanonicalStoredUrl`): whether
 * a host may be loaded was decided at reveal time and is re-validated by the
 * backend, and applying it here would drop every self-hosted link because the
 * allowlist arrives asynchronously from dashboard config.
 */
function readStoredRevealed(): StoredRevealed[] {
  if (typeof localStorage === 'undefined') return []
  const out: StoredRevealed[] = []
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index)
      if (!key) continue
      const parsedKey = parseRevealedStorageKey(key)
      if (!parsedKey) continue
      const raw = localStorage.getItem(key)
      // Bound the parse, not just its result: an oversized value is rejected
      // before JSON.parse rather than after the url check.
      if (!raw || raw.length > MAX_STORED_REVEALED_BYTES) continue
      let value: unknown
      try {
        value = JSON.parse(raw)
      } catch {
        continue
      }
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue
      const { u, t } = value as { u?: unknown; t?: unknown }
      if (typeof u !== 'string' || !u || u.length > MAX_PERSISTED_SOURCE_URL_LENGTH) continue
      const link = parseCandidate(u, NO_GITLAB_HOSTS, true)
      // Re-derived, canonical, and its own parsed kind must agree with the key it
      // was filed under — a 'change' key holding an issue url would inject into
      // the panel the other kind owns.
      if (!link || link.url !== u || link.kind !== parsedKey.kind) continue
      // A stamp is trusted for RECENCY only within a plausible range; outside it
      // the entry keeps its link but forfeits recency and sorts oldest, so a
      // crafted stamp cannot displace a genuine reveal from the read cap.
      const trusted = typeof t === 'number'
        && Number.isFinite(t)
        && t >= 0
        && t < MAX_PLAUSIBLE_STAMP_MS
      out.push({ ...parsedKey, link, at: trusted ? (t as number) : 0 })
    }
  } catch {
    // Enumerating storage can throw in locked-down environments.
    return out
  }
  return out
}

/** Restore revealed links, capped on READ to the most recently written slots.
 *
 *  The cap is applied here and nothing deletes another slot's key, for the reason
 *  `loadSourceSelections` documents at length: a prune pass computes its doomed
 *  set from a walk, and a sibling window can refresh one of those slots before the
 *  removals run. */
export function loadRevealedSources(): RevealedSources {
  const stored = readStoredRevealed()
  const newest = new Map<string, number>()
  for (const entry of stored) {
    newest.set(entry.slot, Math.max(newest.get(entry.slot) ?? 0, entry.at))
  }
  const keep = new Set(
    [...newest.entries()].sort((a, b) => b[1] - a[1]).slice(0, MAX_PERSISTED_SOURCE_SLOTS).map(([slot]) => slot),
  )
  const out: RevealedSources = {}
  for (const entry of stored) {
    if (!keep.has(entry.slot)) continue
    out[entry.slot] = { ...out[entry.slot], [entry.kind]: entry.link }
  }
  return out
}

/** Persist ONE revealed link. Never touches another slot's or kind's key. */
export function commitRevealedSource(
  slot: string | null,
  kind: SourceLinkKind,
  url: string,
): boolean {
  if (!slot || slot.length > MAX_PERSISTED_SLOT_LENGTH) return false
  if (typeof localStorage === 'undefined') return false
  if (url.length > MAX_PERSISTED_SOURCE_URL_LENGTH || !isCanonicalStoredUrl(url)) return false
  // Never stamp below what is already stored: a clock stepping BACKWARD (an NTP
  // correction, a resumed VM) would otherwise sort a brand-new reveal below the
  // read cap. Mirrors `commitSourceSelection`.
  const newestAt = readStoredRevealed().reduce((max, entry) => Math.max(max, entry.at), 0)
  // Clamped BELOW the reader's trusted ceiling, so a write can never produce a
  // stamp its own reader would discard, and a crafted at-the-ceiling entry cannot
  // tie it.
  const at = Math.min(Math.max(Date.now(), newestAt + 1), MAX_WRITABLE_STAMP_MS)
  return safeSetItem(revealedStorageKey(slot, kind), JSON.stringify({ u: url, t: at }))
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
      if (isCanonicalStoredUrl(value)) seen.add(value)
    }
    if (!seen.size) continue
    remainingUrls -= seen.size
    restored.unshift([slot, seen])
  }
  return new Map(restored)
}

/* ── Per-slot selected source tab (which Change / Issue tab is focused) ────
 * The Changes and Issues panels each render one tab per detected url, and the
 * focused one is per chat slot. Kept HERE (persisted, keyed by slot) rather than
 * as a single component-local value so that leaving a session and returning —
 * including across a reload, where the panel tab strip itself rehydrates from
 * mc-panel-tabs:<slot> — restores the tab the user was reading instead of
 * snapping back to the first pull request in the transcript. Not stored on the
 * PanelTab itself: the selection must survive while the Changes tab does not yet
 * exist (the strip is created by SidePanel.syncPinned, which only runs while the
 * panel subtree is mounted), and patchTab silently no-ops on an absent tab.
 *
 * ONE KEY PER (slot, kind), never a shared blob. The dashboard can run several
 * chat windows against one origin (a popped-out session is a second document
 * sharing this localStorage), and localStorage offers no cross-document
 * atomicity: with a single blob, two windows reconciling at the same moment both
 * read it and the later write silently drops whatever the other had just added.
 * A one-field key makes every write a single scalar setItem with nothing to
 * merge, so two windows can only ever collide on the exact same field — which is
 * last-writer-wins by nature rather than collateral loss. usePanelTabs stores the
 * tab strip the same way, for the same reason ("Per-slot writes mean a GC'd slot
 * key is never resurrected by an unrelated slot's mutation").
 *
 * Recency for the slot cap therefore cannot come from key insertion order, so
 * each value carries the epoch millis it was written and the cap is applied on
 * read (and pruned after a write). */

const SOURCE_SELECTION_PREFIX = 'mc-pr-source-sel:'

/** True for a `storage` event key belonging to this store, so a window can tell
 *  a sibling's selection write from every other key on the origin. */
export function isSourceSelectionKey(key: string): boolean {
  return key.startsWith(SOURCE_SELECTION_PREFIX)
}

/** `mc-pr-source-sel:<kind>:<slot>` — kind first so the slot is the whole
 *  remainder and needs no escaping, whatever characters a slot key contains. */
function selectionStorageKey(slot: string, kind: SourceLinkKind): string {
  return `${SOURCE_SELECTION_PREFIX}${kind}:${slot}`
}

function parseSelectionStorageKey(key: string): { slot: string; kind: SourceLinkKind } | null {
  if (!isSourceSelectionKey(key)) return null
  const rest = key.slice(SOURCE_SELECTION_PREFIX.length)
  const split = rest.indexOf(':')
  if (split <= 0) return null
  const kind = rest.slice(0, split)
  const slot = rest.slice(split + 1)
  if (!slot || slot.length > MAX_PERSISTED_SLOT_LENGTH) return null
  if (kind !== 'change' && kind !== 'issue') return null
  return { slot, kind }
}

/** Focused source url per slot, split by kind (one Changes tab + one Issues
 *  tab per slot, each with its own selection). */
export type SourceSelections = Record<string, Partial<Record<SourceLinkKind, string>>>

/** Set one slot's selection for one kind, returning the SAME object when
 *  nothing changes so a React state update bails instead of re-rendering.
 *  Clearing ('' url) is a no-op when there is nothing stored for that slot, so
 *  the many sessions that never mention a pull request do not each accumulate
 *  an empty entry. In-memory only — durability goes through
 *  commitSourceSelection, which writes one field at a time. */
export function withSourceSelection(
  selections: SourceSelections,
  slot: string | null,
  kind: SourceLinkKind,
  url: string,
): SourceSelections {
  if (!slot) return selections
  const current = selections[slot]
  if ((current?.[kind] ?? '') === url) return selections
  if (!url && !current) return selections
  return { ...selections, [slot]: { ...current, [kind]: url } }
}

/** Read one slot's selection for one kind. '' when nothing is remembered. */
export function sourceSelection(
  selections: SourceSelections,
  slot: string | null,
  kind: SourceLinkKind,
): string {
  return (slot && selections[slot]?.[kind]) || ''
}

const SOURCE_KINDS: readonly SourceLinkKind[] = ['change', 'issue']

interface StoredSelection {
  slot: string
  kind: SourceLinkKind
  url: string
  /** Epoch millis of the write. Carries the recency the slot cap trims by, which
   *  independent keys cannot express as insertion order. */
  at: number
}

/** Enumerate and validate every stored field. localStorage is untrusted input,
 *  so a malformed key, a non-canonical url, or an unparseable value is skipped
 *  rather than trusted. A restored url is additionally compared against the live
 *  transcript before it selects anything, so this validation is bounding rather
 *  than authorization. */
function readStoredSelections(): StoredSelection[] {
  if (typeof localStorage === 'undefined') return []
  const out: StoredSelection[] = []
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index)
      if (!key) continue
      const parsedKey = parseSelectionStorageKey(key)
      if (!parsedKey) continue
      let value: unknown
      try {
        value = JSON.parse(localStorage.getItem(key) ?? 'null')
      } catch {
        continue
      }
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue
      const { u, t } = value as { u?: unknown; t?: unknown }
      if (typeof u !== 'string' || !u || u.length > MAX_PERSISTED_SOURCE_URL_LENGTH) continue
      if (!isCanonicalStoredUrl(u)) continue
      out.push({ ...parsedKey, url: u, at: typeof t === 'number' && Number.isFinite(t) ? t : 0 })
    }
  } catch {
    // Enumerating storage can throw in locked-down environments.
    return out
  }
  return out
}

/** Slots ordered newest-write-first. A slot's recency is its most recent field,
 *  so the two kinds of one session are capped together rather than competing. */
function slotsByRecency(stored: readonly StoredSelection[]): string[] {
  const newest = new Map<string, number>()
  for (const entry of stored) {
    newest.set(entry.slot, Math.max(newest.get(entry.slot) ?? 0, entry.at))
  }
  return [...newest.entries()].sort((a, b) => b[1] - a[1]).map(([slot]) => slot)
}

/** Restore the per-slot focused-tab selections, capped to the most recently
 *  written slots.
 *
 *  The cap is applied HERE, on read, and nothing deletes another slot's key.
 *  Deleting by snapshot cannot be made safe across documents: the set of doomed
 *  slots is computed from a walk, and a sibling window can refresh one of them
 *  before the removals run — so a "prune the tail" pass can delete a selection
 *  that just became live. (Re-reading each key immediately before removing it
 *  only shrinks that window, which is the same reasoning that made the old
 *  single-blob read-modify-write unsafe.) The only removal this store performs is
 *  the one the calling document owns: clearing its own field.
 *
 *  The cost is that keys accumulate — two at most per session that ever had a
 *  selection, roughly a hundred bytes each. usePanelTabs takes the same trade for
 *  the tab strip (no cap; a key is removed only when its own bucket empties), and
 *  this is orders of magnitude smaller than the per-session `vc_heights_*` caches
 *  that safeStorage's reclaim tiers exist for. */
export function loadSourceSelections(): SourceSelections {
  const stored = readStoredSelections()
  const keep = new Set(slotsByRecency(stored).slice(0, MAX_PERSISTED_SOURCE_SLOTS))
  const out: SourceSelections = {}
  for (const entry of stored) {
    if (!keep.has(entry.slot)) continue
    out[entry.slot] = { ...out[entry.slot], [entry.kind]: entry.url }
  }
  return out
}

/**
 * Outcome of a durable write. `unchanged` and `failed` are deliberately
 * distinct: both mean "storage was not written", but only `failed` means storage
 * now DISAGREES with what the caller intended, which is what
 * `adoptSourceSelections` needs to know to avoid adopting a stale value back
 * over a live selection.
 */
export type CommitOutcome = 'persisted' | 'unchanged' | 'failed'

/**
 * Write ONE (slot, kind) through to its OWN storage key.
 *
 * There is deliberately nothing to merge here: the value is a scalar under a key
 * no other field shares, so this is a single setItem with no read-modify-write
 * for a concurrent window to interleave with. That is what makes several chat
 * windows on one origin safe — see the store's header comment. Two windows can
 * still race the exact same field, which is last-writer-wins by nature; what
 * cannot happen any more is one window's write dropping a field it never touched.
 *
 * NOT free to call speculatively. It enumerates storage to clamp the stamp, and
 * a valid url is always written through (see the recency note below), so callers
 * must skip it when they know nothing changed — the reconciliation effects gate
 * their clear on there being a selection to clear rather than calling this on
 * every render. `unchanged` therefore means "no write was attempted at all"
 * (no slot, an over-long or non-canonical url, or a clear with nothing stored),
 * never "the value already matched".
 */
export function commitSourceSelection(
  slot: string | null,
  kind: SourceLinkKind,
  url: string,
): CommitOutcome {
  if (!slot || slot.length > MAX_PERSISTED_SLOT_LENGTH) return 'unchanged'
  if (typeof localStorage === 'undefined') return 'unchanged'
  const key = selectionStorageKey(slot, kind)
  const stored = readStoredSelections()
  const current = stored.find(entry => entry.slot === slot && entry.kind === kind)?.url ?? ''

  if (!url) {
    if (!current) return 'unchanged'
    try {
      localStorage.removeItem(key)
    } catch {
      return 'failed'
    }
    return 'persisted'
  }
  // Re-selecting the SAME url is deliberately NOT a no-op: it rewrites the entry
  // to refresh recency. The read cap hides all but the most recent slots, so a
  // slot that has aged out is excluded on load and the panel shows the fallback.
  // If re-picking the value already stored there did nothing, the user's click
  // could never bring that slot back inside the cap and the tab would reset on
  // every reload — the exact failure this store exists to prevent.
  if (url.length > MAX_PERSISTED_SOURCE_URL_LENGTH || !isCanonicalStoredUrl(url)) return 'unchanged'
  // Never stamp below what is already stored. A clock that steps BACKWARD (an
  // NTP correction, a resumed VM, a user setting the date) would otherwise put a
  // brand-new selection at the bottom of the recency order, where the read cap
  // hides it behind 32 older slots and the tab resets on the next reload.
  const newest = stored.reduce((max, entry) => Math.max(max, entry.at), 0)
  const at = Math.max(Date.now(), newest + 1)
  if (!safeSetItem(key, JSON.stringify({ u: url, t: at }))) return 'failed'
  return 'persisted'
}

function sameSelections(a: SourceSelections, b: SourceSelections): boolean {
  const slots = new Set([...Object.keys(a), ...Object.keys(b)])
  for (const slot of slots) {
    for (const kind of SOURCE_KINDS) {
      if ((a[slot]?.[kind] ?? '') !== (b[slot]?.[kind] ?? '')) return false
    }
  }
  return true
}

/**
 * Re-read storage and fold another window's writes into this window's map.
 *
 * Meant for the `storage` event, which fires in every OTHER document on the
 * origin when one of them writes — so a window learns about a sibling's change
 * instead of carrying its mount-time view until reload. Storage is the merged
 * truth for slots this window is not showing (every window commits through
 * `commitSourceSelection`), so those are taken wholesale.
 *
 * The ACTIVE slot is the one that needs a rule, because it is the only slot this
 * window's own reconciliation also writes to. It is merged per KIND, and a stored
 * value replaces this window's own only when both hold:
 *
 *   - It is in `available` — the urls this window's transcript currently offers
 *     for that kind. Two windows can show the same session with DIFFERENT
 *     transcripts (one has received a newer message mentioning another pull
 *     request). Adopting a url this window has no tab for makes its own
 *     reconciliation immediately overwrite the sibling's choice, discarding
 *     whichever selection the user actually made. (Whether that exchange also
 *     repeats depends on how the two transcripts diverged; the selection loss
 *     does not.) Refusing an unusable value ends it: this window keeps its own,
 *     its reconciliation sees nothing to change, and it writes nothing.
 *   - That kind is not in `unpersisted[slot]` — the fields whose own write storage
 *     REFUSED (`safeSetItem` returns false once nothing reclaimable is left).
 *     Storage then holds an older url (or none) for a field the user has already
 *     moved, and without this the next sibling event would quietly revert it. The
 *     ledger is honored for EVERY slot, not just the active one: a refused write
 *     is equally lost whether or not the user happens to be looking at that
 *     session right now.
 *
 * Returns the SAME object when nothing differs, so a state update bails.
 */
export function adoptSourceSelections(
  previous: SourceSelections,
  activeSlot: string | null,
  available: Partial<Record<SourceLinkKind, readonly string[]>> = {},
  unpersisted: Record<string, Partial<Record<SourceLinkKind, boolean>>> = {},
): SourceSelections {
  const stored = loadSourceSelections()
  const next: SourceSelections = { ...stored }
  // Slots this window holds a refused write for, plus the active slot (whose
  // transcript gates adoption). Everything else is taken from storage as-is.
  const reconcile = new Set([...Object.keys(unpersisted), ...(activeSlot ? [activeSlot] : [])])
  for (const slot of reconcile) {
    const incoming = stored[slot]
    const own = previous[slot]
    const refused = unpersisted[slot] ?? {}
    const merged: Partial<Record<SourceLinkKind, string>> = {}
    for (const kind of SOURCE_KINDS) {
      const candidate = incoming?.[kind] ?? ''
      // The transcript check applies only to the slot on screen — it is the one
      // whose reconciliation would answer an unusable value with a write.
      const showable = slot !== activeSlot || (available[kind] ?? []).includes(candidate)
      const usable = Boolean(candidate) && !refused[kind] && showable
      const value = usable ? candidate : (own?.[kind] ?? '')
      if (value) merged[kind] = value
    }
    if (Object.keys(merged).length) next[slot] = merged
    else delete next[slot]
  }
  return sameSelections(previous, next) ? previous : next
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
      if (isCanonicalStoredUrl(value)) urls.push(value)
    }
    if (!urls.length) continue
    remainingUrls -= urls.length
    persisted.unshift([slot, urls])
  }
  return safeSetItem(SEEN_SOURCES_STORAGE_KEY, JSON.stringify(persisted))
}
