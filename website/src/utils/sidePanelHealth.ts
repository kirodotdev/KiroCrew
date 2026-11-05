/**
 * Is the chat page's side panel really on screen while the store says it is
 * open?
 *
 * The header's open button is shown only while `activityOpen` is false, so a
 * panel that is "open" but paints nothing leaves the person with no panel and
 * no way to open one until a reload. That state was reported on the macOS
 * desktop app and was not reproduced: this is the safeguard, plus the
 * diagnostics that should name the cause the next time it happens.
 *
 * `findSidePanelFaults` reads the DOM once and names what is wrong;
 * `sidePanelDiag` records the same fields `diag.js` asked a person to collect
 * by hand. `useSidePanelHealth` (hooks) runs them after an open and heals.
 */

/** What can leave an open side panel off the screen. */
export type SidePanelFault =
  /** The shell's `#activity-bar-slot` the panel portals into has left the document. */
  | 'slot-detached'
  /** No panel column was rendered at all. */
  | 'no-column'
  /** The column or the panel inside it has no width or no height. */
  | 'zero-size'
  /** An ancestor of the column is `display: none` / `visibility: hidden`
   *  while the chat itself is on screen. */
  | 'hidden-ancestor'
  /** A trackpad slide is still set after the panel should have docked. */
  | 'slide-stuck'
  /** The desktop app's native browser view may be left over the panel rect
   *  while the Browser tab is not the one in front. */
  | 'native-cover'

export const SIDE_PANEL_COLUMN_SELECTOR = '[data-testid="side-panel-column"]'
export const SIDE_PANEL_INLINE_SELECTOR = '[data-testid="side-panel-inline"]'

/** The first ancestor-or-self that hides `el`, or null. Stops at `<html>`. */
export function hiddenAncestorOf(el: Element | null): Element | null {
  for (let node = el; node && node !== document.documentElement; node = node.parentElement) {
    const cs = getComputedStyle(node)
    if (cs.display === 'none' || cs.visibility === 'hidden') return node
  }
  return null
}

export interface SidePanelProbe {
  /** The portal target the page holds (null: the inline fallback renders). */
  slot: HTMLElement | null
  /** The chat pane, to tell a hidden panel from a hidden page. */
  chatPane: Element | null
  /** The trackpad slide flag (chatTrackpadSwipe). */
  deskSlide: boolean
  /** The desktop bridge is present, a Browser tab exists in this chat's strip,
   *  and it is not the tab in front. */
  nativeSuspect: boolean
}

/** The column the panel renders into: inside the slot, else the inline one. */
export function sidePanelColumn(slot: HTMLElement | null): Element | null {
  return slot ? slot.querySelector(SIDE_PANEL_COLUMN_SELECTOR) : document.querySelector(SIDE_PANEL_INLINE_SELECTOR)
}

/**
 * Name every reason the side panel is not on screen. Empty when it is, and
 * also when the chat itself is not on screen (a hidden pane shows neither the
 * panel nor the header button, so nothing is out of step).
 */
export function findSidePanelFaults({ slot, chatPane, deskSlide, nativeSuspect }: SidePanelProbe): SidePanelFault[] {
  if (chatPane && (!chatPane.isConnected || hiddenAncestorOf(chatPane))) return []
  const faults: SidePanelFault[] = []
  if (slot && !slot.isConnected) faults.push('slot-detached')
  const column = sidePanelColumn(slot)
  if (!column) {
    if (!faults.length) faults.push('no-column')
  } else {
    const panel = column.firstElementChild
    const boxes = [column, panel].map(el => el?.getBoundingClientRect())
    if (boxes.some(b => !b || b.width < 1 || b.height < 1)) faults.push('zero-size')
    if (hiddenAncestorOf(column)) faults.push('hidden-ancestor')
  }
  if (deskSlide) faults.push('slide-stuck')
  if (nativeSuspect) faults.push('native-cover')
  return faults
}

type Box = { x: number; y: number; w: number; h: number; display: string; visibility: string; opacity: string; style: string | null }
const box = (el: Element | null | undefined): Box | null => {
  if (!el) return null
  const b = el.getBoundingClientRect()
  const cs = getComputedStyle(el)
  return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height), display: cs.display, visibility: cs.visibility, opacity: cs.opacity, style: el.getAttribute('style') }
}
const describe = (el: Element | null): string | null =>
  el ? `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${el.getAttribute('data-testid') ? `[${el.getAttribute('data-testid')}]` : ''}` : null

/** Storage that shapes the panel. Sizes, dock and open flags verbatim; a
 *  tab strip only as its tab kinds and front tab, never its titles or URLs. */
function panelStorage(): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (!k) continue
      if (k.startsWith('mc-panel-tabs:')) {
        try {
          const b = JSON.parse(localStorage.getItem(k) ?? 'null') as { tabs?: { kind?: unknown }[]; activeId?: unknown } | null
          out[k] = { kinds: (b?.tabs ?? []).map(t => String(t?.kind)), active: String(b?.activeId) }
        } catch { out[k] = 'unparsable' }
      } else if (/^mc-(side-panel|activity-open|preview-crew)/.test(k)) {
        out[k] = String(localStorage.getItem(k)).slice(0, 40)
      }
    }
  } catch { /* storage can be locked down */ }
  return out
}

/** The fields `diag.js` collected by hand, for the console line. */
export function sidePanelDiag({ slot, faults }: { slot: HTMLElement | null; faults: SidePanelFault[] }) {
  const column = sidePanelColumn(slot)
  const panel = column?.firstElementChild ?? null
  let hitAtPanelCenter: { el: string | null; inPanel: boolean; under: string | null } | null = null
  if (panel && typeof document.elementFromPoint === 'function') {
    const b = panel.getBoundingClientRect()
    const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2)
    hitAtPanelCenter = hit ? { el: describe(hit), inPanel: panel.contains(hit), under: describe(hit.closest('[data-testid]')) } : null
  }
  const liveSlot = document.getElementById('activity-bar-slot')
  return {
    faults,
    win: [window.innerWidth, window.innerHeight],
    slot: box(slot),
    slotInDoc: slot ? slot.isConnected : null,
    liveSlotIsHeld: slot ? liveSlot === slot : null,
    col: box(column),
    panel: box(panel),
    hitAtPanelCenter,
    hiddenAncestor: describe(hiddenAncestorOf(column)),
    browserAPI: typeof window.browserAPI,
    sliding: document.documentElement.hasAttribute('data-panel-sliding'),
    storage: panelStorage(),
  }
}
