/**
 * The in-app notification banner's preferences and its arrival gate.
 *
 * Two per-device preferences live here, both in localStorage for the same
 * reason the sound and chat-complete settings beside them do: whether a banner
 * should interrupt is a property of the screen the user is looking at, not of
 * the gateway. Both keys degrade to their default on a corrupt or half-written
 * value rather than to a surprise state.
 *
 * The gate (`shouldBannerNote`) is a pure predicate so the suppression rules
 * are one testable list rather than a chain of early returns spread over the
 * component. The socket layer owns the one rule that cannot be decided here —
 * a reconnect catch-up replay — by simply not dispatching the live event.
 */
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { isSilencedNote } from '../store/notificationsSlice'
import type { Notification } from '../types'

/** localStorage key holding the banner opt-out. Absent means ON: the banner is
 *  the default surface for a live notification, and only an explicit `'0'`
 *  turns it off. */
export const BANNER_ENABLED_KEY = 'mc-notification-banner'

/** Same-window signal that the banner preference changed, so a mounted banner
 *  honours the flip immediately (a DOM `storage` event never fires in the
 *  window that wrote the value). */
export const MC_BANNER_SETTING_CHANGED_EVENT = 'mc-notification-banner-changed' as const

export function loadBannerEnabled(): boolean {
  return safeGetItem(BANNER_ENABLED_KEY) !== '0'
}

/** Persist the preference and announce it to this window. Returns whether the
 *  write landed; the event fires only then, so no listener adopts a value that
 *  vanishes on reload. */
export function saveBannerEnabled(on: boolean): boolean {
  const ok = safeSetItem(BANNER_ENABLED_KEY, on ? '1' : '0')
  if (ok) {
    try {
      window.dispatchEvent(new CustomEvent(MC_BANNER_SETTING_CHANGED_EVENT, { detail: { enabled: on } }))
    } catch {
      /* a listener threw; the persisted value is still the truth */
    }
  }
  return ok
}

/** localStorage key recording that the user pressed "Not now" on the bell
 *  popover's system-notification hint. The hint is a one-time nudge: a
 *  permanent dismissal, never a snooze. */
export const PERMISSION_HINT_DISMISSED_KEY = 'mc-notification-permission-hint-dismissed'

export function loadPermissionHintDismissed(): boolean {
  return safeGetItem(PERMISSION_HINT_DISMISSED_KEY) === '1'
}

export function savePermissionHintDismissed(): boolean {
  return safeSetItem(PERMISSION_HINT_DISMISSED_KEY, '1')
}

/** Auto-hide delay for a default-priority banner. Critical banners never
 *  auto-hide. */
export const BANNER_AUTO_HIDE_MS = 6000

/** How many pending banners the deck peeks behind the top card. */
export const BANNER_DECK_DEPTH = 2

/** How many cards the expanded list shows before folding into "+N more". */
export const BANNER_EXPANDED_MAX = 4

/** The chat surfaces, where `chat.activeSlot` is the conversation on screen.
 *  The ONE spelling of that set: the socket layer's `isChatSurfaceVisible`
 *  (useWebSocket.ts) applies it to `window.location`, this gate to the router's
 *  pathname. */
export function isChatPath(pathname: string): boolean {
  return pathname === '/' || pathname === '/chat' || pathname.startsWith('/chat/')
    || pathname.startsWith('/popout/chat') || pathname.startsWith('/embed/chat')
}

export interface BannerGateContext {
  /** The banner preference; OFF suppresses every note. */
  enabled: boolean
  /** The bell popover is on screen (open or still animating shut). The note
   *  is already visible there, so a banner would say the same thing twice. */
  popoverOpen: boolean
  /** Current route. */
  pathname: string
  /** `chat.activeSlot` — the conversation a chat surface is showing. */
  activeSlot: string | null | undefined
  /** `document.hasFocus() && !document.hidden`: the user can see the view. */
  windowFocused: boolean
}

/**
 * True when *n* describes something already on the user's screen. A note for
 * the active chat while the window is focused is a fact the transcript shows;
 * a note whose deep link IS the current route likewise. Re-announcing either
 * is noise, so the gate refuses it. Away from the window nothing is "on
 * screen", and the note banners.
 */
export function targetsCurrentView(n: Notification, ctx: Pick<BannerGateContext, 'pathname' | 'activeSlot' | 'windowFocused'>): boolean {
  if (!ctx.windowFocused) return false
  if (n.slot && ctx.activeSlot && n.slot === ctx.activeSlot && isChatPath(ctx.pathname)) return true
  if (n.url) {
    const path = n.url.split(/[?#]/)[0]
    if (path === ctx.pathname) return true
  }
  return false
}

/**
 * The full arrival gate, in the order the checks read: the preference, the
 * note's own attention tier, the surfaces that already show it, and the view
 * it describes. Every `false` here is a note that stays in the bell (unread
 * dot and all) without a card.
 */
export function shouldBannerNote(n: Notification, ctx: BannerGateContext): boolean {
  if (!ctx.enabled) return false
  if (isSilencedNote(n)) return false
  if (ctx.popoverOpen) return false
  if (ctx.pathname === '/notifications' || ctx.pathname.startsWith('/notifications/')) return false
  if (targetsCurrentView(n, ctx)) return false
  return true
}
