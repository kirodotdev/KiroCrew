/**
 * Where the side panel's remembered size lives.
 *
 * The panel's width (right dock) and height (bottom dock) are remembered PER
 * CHAT, keyed by the slot the panel is showing. Two chats parked on different
 * views want different sizes: a chat left on Git reads well narrow and a chat
 * left on Browser wants most of the window, so ONE shared number re-imposes the
 * last drag on every other chat and the user re-drags the handle on every
 * switch.
 *
 * The size follows the CHAT, never the tab. Switching tabs inside one chat
 * leaves the panel exactly where it is, whatever the tab kinds: a panel that
 * moves its own edge while the user is only changing what it shows reads as a
 * jump nobody asked for. The size changes only when the slot changes, and the
 * panel eases that move on its own open/close curve (see sidePanelMount.ts).
 *
 * Every drag end also writes the bare base key, so a chat opened for the first
 * time starts at the size last dragged anywhere, which is what a new chat got
 * before sizes were per chat, and an install that already had a size upgrades
 * with nothing reset (see `loadSidePanelDim`).
 */
import { safeSetItem } from '../../utils/safeStorage'

/** Base key for the right-dock width. `sidePanelDimKey` appends the slot; the
 *  bare key is the size last dragged in any chat, written at every drag end and
 *  read as the seed for a chat that has no size of its own. */
export const SIDE_PANEL_WIDTH_KEY = 'mc-side-panel-width'
/** Base key for the bottom-dock height. Separate from width so flipping dock
 *  orientation restores each orientation's own last size. */
export const SIDE_PANEL_HEIGHT_KEY = 'mc-side-panel-height'

/**
 * Storage key for one chat's size: `<base>:<slot>`, or the bare base key when
 * the slot is empty.
 *
 * A host whose thread is not confirmed yet passes an empty slot (the Members
 * page until its thread POST answers, the chat page before a slot exists). A
 * key suffixed with nothing would be a phantom chat no slot ever reads again,
 * so such a host reads and writes the bare key alone.
 */
export function sidePanelDimKey(base: string, slot: string): string {
  // One key per chat, collected with that chat's other per-session keys by
  // `utils/storageGc.ts` (both prefixes are in SESSION_PREFIXES), on delete
  // and by the boot-time orphan sweep. The bare base key is never collected.
  return slot ? `${base}:${slot}` : base
}

/**
 * The keys a size is stored under, in read order: the chat's own key, then the
 * bare base key. One entry for an empty slot, where the two are the same key.
 */
function sidePanelDimKeys(base: string, slot: string): string[] {
  return slot ? [sidePanelDimKey(base, slot), base] : [base]
}

/**
 * Stored size for a chat, or `fallback`.
 *
 * Reads the chat's own key, then the bare base key, then `fallback`. The bare
 * key is what every drag end also writes (`saveSidePanelDim`), so a chat with
 * no size of its own opens at the size last dragged anywhere, and an install
 * upgrading from one size for the whole panel starts every chat at the size it
 * already had rather than at the built-in default.
 *
 * A value below `min` is ignored rather than clamped up: it can only come from
 * a hand-edited or stale entry, and the caller's floor is the real minimum.
 */
export function loadSidePanelDim(
  { base, slot, min, fallback }: { base: string; slot: string; min: number; fallback: number },
): number {
  for (const key of sidePanelDimKeys(base, slot)) {
    const v = parseInt(localStorage.getItem(key) || '', 10)
    if (!isNaN(v) && v >= min) return v
  }
  return fallback
}

/**
 * Persist a dragged size under the chat's own key AND the bare base key.
 *
 * The second write is what makes a brand-new chat open at the size the user
 * last chose anywhere instead of at the built-in default, and it keeps the bare
 * key current for an install that later runs a build reading only that key. An
 * empty slot writes the bare key once and no per-slot key.
 */
export function saveSidePanelDim(
  { base, slot, value }: { base: string; slot: string; value: number },
): void {
  for (const key of sidePanelDimKeys(base, slot)) safeSetItem(key, String(value))
}

/**
 * A chat's in-memory size from a slot-keyed map, or `undefined` when that chat
 * has none yet.
 *
 * Own keys only. A slot key is user-supplied and the gateway keeps names like
 * `__proto__` and `constructor` intact, so a plain index would hand back
 * `Object.prototype` or `Object` for them. That value is not nullish, so the
 * `?? loadSidePanelDim(...)` fallback never runs, the clamp turns it into `NaN`,
 * and a drag then saves `NaN` to the bare key every other chat seeds from.
 */
export function ownDim(map: Record<string, number>, slot: string): number | undefined {
  return Object.prototype.hasOwnProperty.call(map, slot) ? map[slot] : undefined
}
