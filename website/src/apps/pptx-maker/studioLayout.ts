/**
 * Geometry of the studio's chat column — the chat that drives a deck, docked
 * beside the deck it is building so the preview stays in view while the agent
 * writes the brief, outline, art direction and slides.
 *
 * Kept in its own module (like `pages/projectsLayout.ts`) so the persisted keys
 * and bounds live in one place and the page reads them through the shared
 * `lib/columnWidth` loaders.
 */
import { loadColumnWidth } from '../../lib/columnWidth'
import { safeGetItem, safeRemoveItem, safeSetItem } from '../../utils/safeStorage'

export const CHAT_WIDTH_KEY = 'kc:pptx-maker:chat-width'

/** Wide enough for the composer's toolbar row without wrapping. */
export const MIN_CHAT_WIDTH = 360
export const DEFAULT_CHAT_WIDTH = 440
/** Past this the preview gets narrower than the chat, which is the wrong way
 *  round for a page whose point is watching the deck. */
export const MAX_CHAT_WIDTH = 720

export function loadChatWidth(): number {
  return loadColumnWidth(CHAT_WIDTH_KEY, MIN_CHAT_WIDTH, MAX_CHAT_WIDTH, DEFAULT_CHAT_WIDTH)
}

/**
 * The studio chat this page last docked, so leaving for another app or the
 * main chat and coming back restores it instead of stranding the session.
 *
 * `open` records whether the user left it docked: the page comes back the way
 * it was left — an open chat is docked from the first render, a chat closed
 * with ✕ stays closed and is offered back from the Decks card header.
 */
export const STUDIO_CHAT_KEY = 'kc:pptx-maker:studio-chat'

export interface StudioChat {
  slot: string
  open: boolean
  /** Last title seen for the session, so the way back can be named before the
   *  session list has loaded. */
  title?: string
}

/** Slot keys are short gateway-minted ids; anything else is not ours. */
const SLOT_KEY_RE = /^[A-Za-z0-9._:-]{1,200}$/

export function loadStudioChat(): StudioChat | null {
  const raw = safeGetItem(STUDIO_CHAT_KEY)
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    const { slot, open, title } = parsed as { slot?: unknown; open?: unknown; title?: unknown }
    if (typeof slot !== 'string' || !SLOT_KEY_RE.test(slot) || typeof open !== 'boolean') return null
    return typeof title === 'string' && title ? { slot, open, title: title.slice(0, 200) } : { slot, open }
  } catch {
    // A corrupt entry means "nothing to restore", never a broken page.
    return null
  }
}

export function saveStudioChat(chat: StudioChat): void {
  safeSetItem(STUDIO_CHAT_KEY, JSON.stringify(chat))
}

/** Forget the remembered chat — its session was deleted or archived. */
export function clearStudioChat(): void {
  safeRemoveItem(STUDIO_CHAT_KEY)
}

/**
 * The deck a studio chat is building: the first deck that appeared after the
 * chat started, newest first. Deck ids start with the engine's
 * `YYYYMMDD-HHMM` stamp, so the list is already newest-first.
 *
 * Returns `null` until one appears — the chat may still be interviewing.
 */
export function deckStartedSince(deckIds: readonly string[], known: ReadonlySet<string>): string | null {
  for (const id of deckIds) {
    if (!known.has(id)) return id
  }
  return null
}
