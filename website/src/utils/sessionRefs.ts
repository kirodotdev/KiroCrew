/**
 * Staged references to OTHER chat sessions — the "drag a session from the list
 * into the composer" gesture.
 *
 * A staged ref carries a LINK ONLY, never the referenced session's transcript.
 * That is a deliberate context-budget decision, not an unfinished shortcut:
 * across a real store, per-session user+assistant prose runs ~7.6k tokens at
 * the median and ~38k at p90 (and ~120k median once tool `meta` is counted).
 * Inlining that would spend a large slice of the RECEIVING session's window in
 * one turn, and pushing past the autocompact threshold would compact away the
 * very conversation the reference was meant to enrich. The receiving agent
 * resolves the link on demand instead, through a path that is already bounded,
 * redacted, and incognito-refusing server-side.
 *
 * Everything here is pure so the drag/compose wiring can be unit-tested without
 * a render.
 */
import { buildShareableUrl } from './shareUrl'

export interface SessionRef {
  /** Conversation-log key of the referenced session. */
  key: string
  /** Title snapshotted at stage time (the source may be renamed afterwards). */
  title: string
  /** Message count at stage time, for the chip's secondary line. */
  messages?: number
}

/** Max refs stageable on one message. A link costs ~50 tokens so the ceiling is
 *  about legibility, not budget: the chip strip is a single scrolling row. */
export const MAX_SESSION_REFS = 8

/**
 * Ceiling for a set being RECOVERED after a failed send, deliberately larger than
 * the staging cap.
 *
 * The two bounds answer different questions. `MAX_SESSION_REFS` limits what a user
 * may ADD (`addSessionRef` refuses past it, so nobody reaches this number by
 * staging). Recovery is not an add: it is giving back references the user already
 * staged and that were never delivered. Capping recovery at the staging bound made
 * a failed send silently discard some of them — losing a reference is worse than
 * a composer transiently showing more chips than a fresh compose could reach, and
 * the user can still see and remove every one of them.
 */
export const MAX_SESSION_REFS_RECOVERY = MAX_SESSION_REFS * 2

/**
 * Memory modes whose sessions may never be referenced from another session.
 * Mirrors the backend's `INCOGNITO_MEMORY_MODES` (history.py), documented there
 * as "never searchable/listable/summarizable".
 *
 * The link alone carries no transcript, and `get_chat_session` refuses these
 * modes server-side regardless — but a bare key still discloses that the
 * session exists and invites a read attempt, so the gesture is refused at the
 * source. This is the UX half of a guard whose enforcement half already exists.
 */
export const PRIVATE_MEMORY_MODES: readonly string[] = ['incognito', 'temporary']

export function isPrivateMemoryMode(mode: string | null | undefined): boolean {
  return !!mode && PRIVATE_MEMORY_MODES.includes(mode)
}

/** Why a session may not be referenced into the current chat, or `null` if it
 *  may be. */
export type SessionRefBlockReason = 'private' | 'self'

/**
 * The single decision behind both halves of the guard: what the drop zone
 * renders (invitation vs refusal) and what the drop handler does. Keeping it in
 * one pure function is what stops the affordance and the handler from drifting
 * apart — a refusal the user was shown but that the handler honours only by
 * accident is the failure mode this avoids.
 */
export function sessionRefBlockReason(opts: {
  key: string
  activeSlot?: string | null
  memoryMode?: string | null
}): SessionRefBlockReason | null {
  if (isPrivateMemoryMode(opts.memoryMode)) return 'private'
  // Referencing the session you are already in is a no-op that would read as a
  // bug: a chip pointing at the current chat.
  if (opts.activeSlot && opts.key === opts.activeSlot) return 'self'
  return null
}

/**
 * The link staged for a session — byte-identical to what the session menu's
 * "Copy link" action puts on the clipboard, because it is the same builder.
 *
 * Delegating rather than re-deriving `/chat?sid=…` is the point: one definition
 * of "a link to a session" means the composer, the clipboard, and the message
 * footer can never drift into three dialects that the receiving side has to
 * recognise separately.
 */
export function sessionRefUrl(ref: SessionRef): string {
  return buildShareableUrl(ref.key, ref.title)
}

/**
 * Reduce a title to text that is inert inside a markdown link label.
 *
 * Three characters classes are removed, each closing a vector measured against
 * the app's own `react-markdown` + `remark-gfm` pipeline (see the breakout tests):
 *
 *   `[` `]`  — an unescaped `]` terminates the label early, so the remainder
 *              renders as a second, attacker-chosen link.
 *   `\`      — a TRAILING backslash escapes the generated `]`, so the label never
 *              closes; GFM then discards our link entirely and renders only the
 *              autolink hidden in the label. Measured: one href, pointing at the
 *              attacker. This is the worst of the three — it does not add a link,
 *              it REPLACES ours.
 *   `<` `>`  — GFM autolinks `<https://…>` even while our link parses correctly,
 *              yielding a second href with no backslash involved at all.
 *
 * Parentheses are deliberately KEPT: with `]` gone they cannot terminate the
 * label, the probe confirms they stay inert, and legitimate titles contain them.
 * Newlines are collapsed so a title cannot span lines in the sent message.
 */
export function sanitizeRefTitle(title: string, max = 80): string {
  const flat = title.replace(/\s+/g, ' ').replace(/[[\]\\<>]/g, '').trim()
  if (!flat) return ''
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat
}

/** The markdown link appended to the outgoing message for one staged ref. */
export function formatSessionRefLink(ref: SessionRef): string {
  const url = sessionRefUrl(ref)
  // BOTH candidates are sanitized. The key is not implicitly safe: it survives
  // a round-trip through the draft store, and `sanitizeSessionRefs` only demands
  // a non-empty string — so a corrupt entry can carry `x](https://evil/)`, whose
  // unescaped `]` would close this link early and render the remainder as a
  // second, attacker-chosen link. Sanitizing the title and then falling back to
  // a raw key would have defeated the very guard the title needs.
  //
  // Final fallback is the URL, which is bracket-free by construction: the slug
  // is `[a-z0-9-]` only (toSlug) and URLSearchParams percent-encodes the rest.
  const label = sanitizeRefTitle(ref.title || '') || sanitizeRefTitle(ref.key) || url
  return `[${label}](${url})`
}

/**
 * Append staged links to the outgoing message. Returns `text` unchanged when
 * nothing is staged, so a message with no refs is byte-identical to before this
 * feature existed.
 */
export function appendSessionRefLinks(text: string, refs: SessionRef[]): string {
  if (!refs.length) return text
  const links = refs.map(r => formatSessionRefLink(r)).join('\n')
  // filter(Boolean) drops an empty typed message so a links-only send has no
  // leading blank line. Joined rather than interpolated: the separator is
  // message structure, not copy, and keeping it out of a template literal keeps
  // this line off the i18n gate's radar honestly instead of by exemption.
  return [text, links].filter(Boolean).join('\n\n')
}

/**
 * Stage a ref. Duplicates and overflow past `MAX_SESSION_REFS` are no-ops that
 * return the SAME array reference, so callers can skip the state update (and
 * the re-render) on a redundant drop.
 */
export function addSessionRef(refs: SessionRef[], ref: SessionRef): SessionRef[] {
  if (!ref.key) return refs
  if (refs.some(r => r.key === ref.key)) return refs
  if (refs.length >= MAX_SESSION_REFS) return refs
  return [...refs, ref]
}

/** Unstage a ref. Returns the same array reference when the key wasn't staged. */
export function removeSessionRef(refs: SessionRef[], key: string): SessionRef[] {
  const next = refs.filter(r => r.key !== key)
  return next.length === refs.length ? refs : next
}

/**
 * Merge refs being restored after a failed send back into whatever is staged now.
 *
 * `keep` wins on a key collision, so a reference the user staged WHILE the send
 * was in flight is never clobbered by the one coming back. Shared by both
 * failure paths (create-failure and transport-failure) so they cannot drift into
 * two different merge rules.
 *
 * Capped at `MAX_SESSION_REFS_RECOVERY`, not the staging cap. A recovery set is
 * references the user already staged and that were never delivered, so trimming it
 * to the staging bound silently discarded some: with a full set sent and another
 * staged during the in-flight window, the originals fell off. `addSessionRef` still
 * refuses to ADD past `MAX_SESSION_REFS`, so this ceiling is only ever reached by
 * giving something back, never by staging.
 *
 * `keep` is taken first, so the newly staged references — the ones the user can
 * currently see — are the ones that survive if the recovery ceiling is ever hit.
 */
export function mergeSessionRefs(keep: SessionRef[], incoming: SessionRef[]): SessionRef[] {
  if (!incoming.length) return keep
  const held = new Set(keep.map(r => r.key))
  const merged = [...keep, ...incoming.filter(r => !held.has(r.key))]
  return merged.length > MAX_SESSION_REFS_RECOVERY
    ? merged.slice(0, MAX_SESSION_REFS_RECOVERY)
    : merged
}

/**
 * Sanitizer for the per-slot draft store: drops malformed entries, de-dupes by
 * key, enforces the cap, and returns `null` for "nothing worth storing" (the
 * contract `createSlotDraftStore` expects).
 *
 * Bounded by `MAX_SESSION_REFS_RECOVERY`, not the staging cap, so the store can
 * hold a set handed back by `mergeSessionRefs` after a failed send. Truncating at
 * the staging bound here would silently drop references the merge deliberately
 * preserved, and they would vanish on the next slot switch — live state and the
 * persisted draft have to agree.
 */
export function sanitizeSessionRefs(v: unknown): SessionRef[] | null {
  if (!Array.isArray(v)) return null
  const out: SessionRef[] = []
  for (const item of v) {
    if (!item || typeof item !== 'object') continue
    const r = item as Record<string, unknown>
    if (typeof r.key !== 'string' || !r.key) continue
    if (out.some(x => x.key === r.key)) continue
    out.push({
      key: r.key,
      title: typeof r.title === 'string' ? r.title : r.key,
      messages: typeof r.messages === 'number' && Number.isFinite(r.messages) && r.messages >= 0
        ? r.messages
        : undefined,
    })
    if (out.length >= MAX_SESSION_REFS_RECOVERY) break
  }
  return out.length ? out : null
}
