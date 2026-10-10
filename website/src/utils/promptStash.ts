/**
 * Prompt stash: park the composer draft and bring it back later.
 *
 * Cmd/Ctrl+S in the composer moves the current draft (its text plus the
 * collapsed paste blocks its `[ Paste #N ]` tokens point at) onto a small
 * per-session stack and clears the composer. The same chord on an EMPTY
 * composer brings the most recent entry back. Each draft has its own
 * localStorage key so same-session tabs never overwrite a shared array.
 *
 * Scope is per chat session, not global: a draft is written for one
 * conversation, and restoring it into another would send it to the wrong agent.
 *
 * At the cap (`PROMPT_STASH_MAX`) a further stash is REFUSED and the draft stays
 * in the composer. Nothing is ever dropped to make room, because the entry that
 * would be dropped is text the user deliberately kept. For the same reason the
 * store has no expiry and no cross-session eviction (unlike the draft stores):
 * an entry leaves only when the user restores or deletes it. A write that the
 * browser refuses (quota, disabled storage) is reported to the caller, which
 * keeps the draft in the composer.
 *
 * The same rule bounds the store's SIZE: `PROMPT_STASH_MAX_BYTES` caps the
 * serialized bytes of every `mc-prompt-stash:*` entry across all slots, and a
 * write that would cross it is REFUSED, never made room for by evicting another
 * entry. The stash keys sit outside `safeStorage`'s reclaim tiers on purpose
 * (kept drafts are never silently dropped), so without this budget a few
 * multi-MB pastes would eat the ~1 MB of shared localStorage headroom that
 * `draftConstants.ts` reserves for the uncapped draft stores, and other
 * sessions' unsent drafts would then fail to save.
 *
 * Not to be confused with `queuedSendStash` in `useQueuedMessageActions`, which
 * is an internal in-memory record of queued sends. This module never reads or
 * writes it.
 */
import type { PasteBlock } from './pasteTokens'
import { sanitizePasteBlocks } from './chatPasteDrafts'
import { safeRemoveItem, safeSetItem } from './safeStorage'
import { eventKeyToken } from '../lib/shortcutRegistry'

/** Prefix of the per-entry keys for one chat slot. The trailing separator keeps
 *  `chat-1` from matching `chat-10`. */
export const PROMPT_STASH_KEY = 'mc-prompt-stash'
export const promptStashKey = (slot: string): string => `${PROMPT_STASH_KEY}:${slot}:`
/** Most drafts one session can hold. Small on purpose: this is a place to park
 *  a prompt for a moment, not a notes store. */
export const PROMPT_STASH_MAX = 10

/** Most serialized bytes (key + value, as `String.length` code units, the same
 *  measure `slotDraftStore` uses) that every `mc-prompt-stash:*` entry across
 *  ALL slots may hold together. A write that would cross it is refused; nothing
 *  is evicted. Sized well inside the ~1 MB that `draftConstants.ts` leaves for
 *  the uncapped localStorage siblings, so a full stash cannot starve them. */
export const PROMPT_STASH_MAX_BYTES = 512 * 1024

export interface PromptStashEntry {
  id: string
  text: string
  blocks: PasteBlock[]
  /** Stash time, used only to restore in insertion order. */
  t: number
}

/** Serialize every stash mutation across tabs and slots. The byte budget is
 *  origin-wide, so all mutations share one Web Lock; contention is negligible
 *  because stash actions are user-initiated. Older browsers and test
 *  environments without Web Locks keep the existing same-tab behaviour. */
export function withPromptStashLock<T>(criticalSection: (locked: boolean) => T | Promise<T>): Promise<T> {
  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks
  if (!locks) return Promise.resolve(criticalSection(false))
  // Keep lock availability explicit inside the mutation. The no-lock fallback
  // needs post-write ownership and byte-budget checks that the serialized path
  // does not.
  return locks.request(PROMPT_STASH_KEY, () => criticalSection(true)) as Promise<T>
}

/** The keyboard fields the chord test reads. A structural subset of
 *  `KeyboardEvent`, so tests can pass a plain object. */
export interface StashChordEvent {
  key: string
  /** Physical key (`KeyS`). Optional so tests can pass a plain object; the
   *  registry token falls back to `key` when it is absent. */
  code?: string
  metaKey: boolean
  ctrlKey: boolean
  altKey: boolean
  shiftKey: boolean
  isComposing?: boolean
}

/** Cmd+S (macOS) / Ctrl+S (elsewhere), with no Shift or Alt. Either modifier is
 *  accepted on every platform, matching the other Cmd/Ctrl chords in the
 *  dashboard. Shift+Cmd+S is left alone so it keeps its "save as" meaning, and a
 *  keystroke that is part of an IME composition is never claimed.
 *
 *  The key is matched POSITIONALLY through the shortcut registry's
 *  `eventKeyToken` (`e.code` first, `e.key` only without a code), the same way
 *  the registry's own `stash-prompt` entry resolves. On a Cyrillic or Greek
 *  layout Ctrl+physical-S carries `key: 'ы'` but `code: 'KeyS'`; comparing the
 *  glyph would leave the chord dead there and let the browser's save dialog
 *  through. */
export function isStashChord(e: StashChordEvent): boolean {
  if (e.isComposing) return false
  if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return false
  return eventKeyToken({ code: e.code ?? '', key: e.key }) === 's'
}

/** A draft that is only whitespace, with no paste blocks, has nothing worth
 *  keeping. */
export function isDraftEmpty(text: string, blocks: readonly PasteBlock[]): boolean {
  return blocks.length === 0 && text.trim() === ''
}

export type StashPlan = 'stash' | 'full' | 'restore' | 'none'

/** What the chord does, given the composer and the stack. A non-empty draft is
 *  stashed (or refused at the cap); an empty one restores the latest entry, or
 *  does nothing when the stack is empty. */
export function planStashChord(draftEmpty: boolean, count: number): StashPlan {
  if (!draftEmpty) return count >= PROMPT_STASH_MAX ? 'full' : 'stash'
  return count > 0 ? 'restore' : 'none'
}

let idCounter = 0
/** Unique across tabs even when two stash actions share the same millisecond:
 *  time, a per-tab counter and random bits. Not `crypto.randomUUID`, which is
 *  missing on a dashboard served over plain http from a LAN address. */
export function makeStashEntry(text: string, blocks: readonly PasteBlock[], now = Date.now()): PromptStashEntry {
  idCounter += 1
  const rand = Math.random().toString(36).slice(2, 10)
  return {
    id: `stash-${now.toString(36)}-${idCounter.toString(36).padStart(8, '0')}-${rand}`,
    text,
    blocks: blocks.map(b => ({ ...b })),
    t: now,
  }
}

function sanitizeEntry(v: unknown, id: string): PromptStashEntry | null {
  if (!v || typeof v !== 'object' || !id) return null
  const e = v as Record<string, unknown>
  if (typeof e.text !== 'string' || typeof e.t !== 'number' || !Number.isFinite(e.t)) return null
  const blocks = e.blocks === undefined ? [] : sanitizePasteBlocks(e.blocks) ?? []
  if (isDraftEmpty(e.text, blocks)) return null
  return { id, text: e.text, blocks, t: e.t }
}

function storedEntry(entry: PromptStashEntry): Omit<PromptStashEntry, 'id'> {
  return { text: entry.text, blocks: entry.blocks, t: entry.t }
}

function entryKey(slot: string, id: string): string {
  return `${promptStashKey(slot)}${id}`
}

/** The stack for one chat slot, oldest first. Corrupt entries are ignored.
 *  A concurrent add may briefly exceed the cap; do not truncate here because
 *  that would hide a draft the user deliberately kept. */
export function loadPromptStash(slot: string): PromptStashEntry[] {
  const prefix = promptStashKey(slot)
  const out: PromptStashEntry[] = []
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (!key?.startsWith(prefix)) continue
      const raw = localStorage.getItem(key)
      if (!raw) continue
      try {
        const clean = sanitizeEntry(JSON.parse(raw), key.slice(prefix.length))
        if (clean) out.push(clean)
      } catch {
        // One corrupt entry must not hide the other independently stored drafts.
      }
    }
  } catch {
    return []
  }
  return out.sort((a, b) => a.t - b.t || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
}

/** Serialized size of the whole stash: key + value code units of every
 *  `mc-prompt-stash:*` entry in every slot. Unreadable storage counts as empty;
 *  the write that follows then fails on its own and is reported. */
export function promptStashBytes(): number {
  const prefix = `${PROMPT_STASH_KEY}:`
  let total = 0
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (!key?.startsWith(prefix)) continue
      total += key.length + (localStorage.getItem(key)?.length ?? 0)
    }
  } catch {
    return 0
  }
  return total
}

export interface StashWriteOptions {
  /** Id of an entry in the same slot that this write replaces (a restored
   *  draft's stored copy, removed right after the write succeeds). Its bytes
   *  are not counted against the budget. */
  releasing?: string | null
}

/** Serialized size of one stored entry, or 0 when it is absent or unreadable. */
export function storedEntryBytes(slot: string, id: string): number {
  const key = entryKey(slot, id)
  try {
    const raw = localStorage.getItem(key)
    return raw === null ? 0 : key.length + raw.length
  } catch {
    return 0
  }
}

/** Whether writing `entry` for `slot` keeps the whole stash within
 *  `PROMPT_STASH_MAX_BYTES`. The hook asks this before `addStashEntry` so a
 *  refusal for size gets its own message; `addStashEntry` enforces it too. */
export function stashWriteFits(slot: string, entry: PromptStashEntry, options: StashWriteOptions = {}): boolean {
  const { releasing = null } = options
  const added = entryKey(slot, entry.id).length + JSON.stringify(storedEntry(entry)).length
  const released = releasing ? storedEntryBytes(slot, releasing) : 0
  return promptStashBytes() - released + added <= PROMPT_STASH_MAX_BYTES
}

/** Persist one independent entry and verify that exact entry can be read back.
 *  A write that would push the whole stash past `PROMPT_STASH_MAX_BYTES` is
 *  refused before touching storage. */
export function addStashEntry(slot: string, entry: PromptStashEntry, options: StashWriteOptions = {}): boolean {
  const clean = sanitizeEntry(entry, entry.id)
  if (!clean) return false
  if (!stashWriteFits(slot, clean, options)) return false
  const key = entryKey(slot, clean.id)
  const encoded = JSON.stringify(storedEntry(clean))
  if (!safeSetItem(key, encoded)) return false
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return false
    const roundTrip = sanitizeEntry(JSON.parse(raw), clean.id)
    return roundTrip !== null && JSON.stringify(storedEntry(roundTrip)) === encoded
  } catch {
    return false
  }
}

/** The storage keys of every stash entry one slot holds right now, read under
 *  the stash lock. A permanent delete takes this BEFORE it asks the gateway, so
 *  the clear that follows a confirmed delete removes exactly these entries: a
 *  draft stashed afterwards, in a session reopened under the same slot key
 *  while the DELETE was in flight (a member thread reuses its key), is not the
 *  deleted session's and stays. */
export function snapshotPromptStash(slot: string): Promise<string[]> {
  return withPromptStashLock(() => {
    const prefix = promptStashKey(slot)
    const keys: string[] = []
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i)
        if (key?.startsWith(prefix)) keys.push(key)
      }
    } catch {
      // Storage enumeration is best effort; an unread entry simply stays.
    }
    return keys
  })
}

/** Remove the entries a `snapshotPromptStash` call listed, under the stash
 *  lock. Keys that are already gone are a no-op. */
export function removePromptStashKeys(keys: readonly string[]): Promise<void> {
  return withPromptStashLock(() => {
    for (const key of keys) safeRemoveItem(key)
  })
}

/** The storage keys of every stash entry held by slots OTHER than `slot`.
 *  Synchronous, so a caller already inside `withPromptStashLock` can take it
 *  without re-entering the lock. A refusal at the byte budget offers to clear
 *  exactly these: entries left by a session that was deleted somewhere this tab
 *  never saw (another tab, the CLI, retention) still count toward the budget,
 *  and nothing else in the product can free them. The client never decides
 *  which slots are gone; the user confirms the clear after seeing the count. */
export function otherSlotsStashKeys(slot: string): string[] {
  const all = `${PROMPT_STASH_KEY}:`
  const own = promptStashKey(slot)
  const keys: string[] = []
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key?.startsWith(all) && !key.startsWith(own)) keys.push(key)
    }
  } catch {
    // Unreadable storage offers nothing to clear.
  }
  return keys
}

/** Remove one independent entry and report whether it is now absent. */
export function removeStashEntry(slot: string, id: string): boolean {
  const key = entryKey(slot, id)
  try {
    localStorage.removeItem(key)
    return localStorage.getItem(key) === null
  } catch {
    return false
  }
}
