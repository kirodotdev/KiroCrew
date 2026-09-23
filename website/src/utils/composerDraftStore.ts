import { safeSetSessionItem } from './safeStorage'

/**
 * Durable home for a selection composer's typed-but-unsent comment, keyed per
 * host document and per PASSAGE (offset + anchor text), so two half-written
 * comments on different passages of one document coexist and neither can
 * overwrite or clear the other. This is the `SelectionComposer.draftStore`
 * contract: the toolbar writes on every keystroke and reads back when the box
 * next opens over the same passage.
 *
 * Why it exists: a chat-slot switch replaces the whole side panel, a teardown
 * the toolbar cannot guard, so without this the draft is gone with the panel.
 *
 * Two copies, merged per passage with memory winning. sessionStorage is the
 * one that should survive the switch but not a browser session; the module
 * map is the fallback when sessionStorage refuses (a full quota, legacy
 * private modes) — it still outlives the panel, since a slot switch unmounts
 * the panel, not the page. Every operation swallows storage errors: nothing
 * here may throw out of a keystroke handler.
 */
export interface ComposerDraftStore {
  read: (anchor: string, start: number) => string | null
  write: (text: string, anchor: string, start: number) => void
  clear: (anchor: string, start: number) => void
}

type Slots = Record<string, string>

/** In-memory twin of the sessionStorage record, per document key. */
const composerDraftMemory = new Map<string, string>()

const slotKey = (anchor: string, start: number) => `${start}|${anchor}`

function parse(raw: string | null): Slots {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object') return {}
    const out: Slots = {}
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) if (typeof v === 'string') out[k] = v
    return out
  } catch { return {} }
}

/**
 * The store for one host document. `key` must be unique per document across
 * hosts (the file viewer uses its file path, the artifact hosts their slug),
 * since the record is shared by every panel showing that document.
 */
export function composerDraftStoreFor(key: string): ComposerDraftStore {
  // `save` always writes memory and writes sessionStorage only when that
  // succeeds, so after a quota rejection the memory copy is the newer one for
  // the slots it holds, while sessionStorage still carries slots from before
  // this page load.
  const load = (): Slots => {
    let fromSession: Slots = {}
    try { fromSession = parse(window.sessionStorage.getItem(key)) } catch { /* unavailable */ }
    return { ...fromSession, ...parse(composerDraftMemory.get(key) ?? null) }
  }
  const save = (slots: Slots) => {
    if (Object.keys(slots).length === 0) {
      composerDraftMemory.delete(key)
      try { window.sessionStorage.removeItem(key) } catch { /* unavailable */ }
      return
    }
    const raw = JSON.stringify(slots)
    composerDraftMemory.set(key, raw)
    // Through the helper so a full or denied store can never raise on the
    // render path; the in-memory copy above is what actually serves this tab.
    safeSetSessionItem(key, raw)
  }
  return {
    read: (anchor, start) => load()[slotKey(anchor, start)] ?? null,
    write: (text, anchor, start) => { const slots = load(); slots[slotKey(anchor, start)] = text; save(slots) },
    clear: (anchor, start) => { const slots = load(); delete slots[slotKey(anchor, start)]; save(slots) },
  }
}
