import type { NavIntent } from './popoutController'
import { handoffToChat, notifyChatHandoffs } from './errorReport'
import { safeSetSessionItem } from './safeStorage'
import type { ChatLaunchOptions } from '../app-sdk'

/**
 * Shared composer-prefill + navigation-intent helpers.
 *
 * `PREFILL_STORAGE_KEY` is the sessionStorage channel ChatPage's slot-restore
 * effect honors: `{ slotKey, prompt, ts }` seeds the composer when the slot
 * becomes active (30s TTL). It's exported from here so non-page modules (the
 * popout nav-intent applier below) can write it without importing a page
 * component. ChatPage re-exports it for its existing importers.
 */
export const PREFILL_STORAGE_KEY = 'kirocrew_prefill'

/** Seed the composer prefill for a slot. Returns whether sessionStorage accepted it. */
export function writePrefill(slotKey: string, prompt: string): boolean {
  return safeSetSessionItem(PREFILL_STORAGE_KEY, JSON.stringify({ slotKey, prompt, ts: Date.now() }))
}

/** `window.__mc_chat_launch`: the slot the App SDK's `openChat` writes and ChatPage's launch intake reads. */
type ChatLaunchWindow = Window & { __mc_chat_launch?: ChatLaunchOptions & { ts: number } }

/**
 * Take the chat launch intent staged on THIS window, if a fresh one is there,
 * and clear it. Fresh by the launch intake's own rule (ten seconds): a stale
 * one is a launch some earlier page already gave up on, not this navigation's.
 */
export function claimChatLaunch(): ChatLaunchOptions | undefined {
  const launchWindow = window as ChatLaunchWindow
  const staged = launchWindow.__mc_chat_launch
  if (!staged) return undefined
  delete launchWindow.__mc_chat_launch
  if (Date.now() - (staged.ts ?? 0) > 10_000) return undefined
  const { ts: _ts, ...launch } = staged
  return launch
}

/** A store hand-off waiting for its staged router navigation to commit. */
export interface DeferredPendingInput {
  text: string
  fromKey: string
  /** The destination whose commit releases the prompt. */
  to: string
  /** The session the prompt is for; the releasing commit must show it active. */
  slotKey: string | null
  ts: number
}

export const DEFERRED_PENDING_INPUT_TTL_MS = 10_000

type DeferredPendingInputCommit = {
  key: string
  href: string
  activeSlot: string | null
}

function normalizeDashboardHref(href: string): { href: string; pathname: string } | null {
  try {
    const url = new URL(href, 'http://dashboard.invalid')
    return { href: `${url.pathname}${url.search}`, pathname: url.pathname }
  } catch {
    return null
  }
}

/**
 * Take a fresh staged pending input only on its own commit: the committed path
 * and query equal the destination, or, for a session-bound chat destination,
 * ChatPage's `/chat[/<slug>]?sid=<slotKey>` rewrite (legacy `?slot=` too)
 * batched into the same transition. The named session must also be active, so
 * a later switch cannot seed another draft.
 */
export function takeDeferredPendingInput(
  stash: DeferredPendingInput | null,
  committed: DeferredPendingInputCommit,
  now: number,
): string | null {
  if (!stash || committed.key === stash.fromKey) return null
  if (now - stash.ts > DEFERRED_PENDING_INPUT_TTL_MS) return null

  const destination = normalizeDashboardHref(stash.to)
  const location = normalizeDashboardHref(committed.href)
  if (!destination || !location) return null

  const onChatDestination = destination.pathname === '/chat'
    || destination.pathname.startsWith('/chat/')
  const ownCommit = location.href === destination.href
    || (Boolean(stash.slotKey)
      && onChatDestination
      && chatDeepLinkSlot(location.href) === stash.slotKey)
  if (!ownCommit) return null
  if (stash.slotKey !== null && committed.activeSlot !== stash.slotKey) return null
  return stash.text
}

/**
 * Stage a forwarded chat launch intent on THIS window, where ChatPage's launch
 * intake reads it. A fresh `ts`: the hand-off between windows must not eat into
 * the intake's ten-second freshness window.
 */
export function writeChatLaunch(launch: ChatLaunchOptions): void {
  ;(window as ChatLaunchWindow).__mc_chat_launch = { ...launch, ts: Date.now() }
}

/**
 * The chat session a dashboard path names, or `''` when it names none.
 *
 * `/chat?sid=<slotKey>` is the dashboard's session deep link — the System page's
 * session rows, Telemetry's conversation links and the app SDK all build it — and
 * `?slot=` is the legacy spelling ChatPage still reads. A `/chat/<slug>` prefix is
 * decorative (ChatPage writes it from the session title), so it is accepted too.
 *
 * Used to tell a session deep link apart from an ordinary route, because the two
 * cannot be honoured the same way: `navigate()` alone only selects a session while
 * ChatPage is MOUNTING, so a link followed while /chat is already open lands on
 * the page with the previous session still selected.
 */
export function chatDeepLinkSlot(path: string): string {
  let url: URL
  try {
    // A relative path needs some base to parse against; only the path and query
    // are ever read back off it.
    url = new URL(path, 'http://dashboard.invalid')
  } catch {
    return ''
  }
  const onChat = url.pathname === '/chat' || url.pathname.startsWith('/chat/')
  if (!onChat) return ''
  return url.searchParams.get('sid') || url.searchParams.get('slot') || ''
}

/**
 * Perform a navigation intent forwarded from a popout window, in THIS main
 * dashboard window. Order matters: the prefill must be in sessionStorage
 * before the slot switch + route change so ChatPage's slot-restore effect
 * finds it when the target slot activates.
 *
 * `mayLeave` is the page-on-screen leave check (`useMayLeaveForNavigation`).
 * An intent that leaves the page asks it first, like every wired in-app exit,
 * and a veto drops the whole intent: no prefill is staged and no slot switches
 * for a navigation the user refused.
 */
export function applyNavIntentInMain(
  intent: NavIntent,
  deps: {
    navigate: (path: string) => void
    switchSlot: (slotKey: string) => void
    // Required, so every caller decides whether its exit asks the page first.
    mayLeave: () => boolean
    /**
     * Append `text` to the chat composer this window already shows for
     * `slotKey`, returning false when no such composer is on screen. A hand-off
     * to the slot already shown cannot go through the sessionStorage prefill:
     * activating an already-active slot does not re-run the slot-restore effect,
     * so the seed would sit unread for its 30s TTL and then overwrite whatever
     * the user typed on their next switch back.
     */
    appendToOpenComposer?: (slotKey: string, text: string) => boolean
    /**
     * Stage `text` for `to` and `slotKey` so it reaches THIS window's store
     * only after the navigation this function issues next has committed. Redux
     * renders synchronously while BrowserRouter commits its location in a
     * transition; publishing immediately lets a mounted ChatPage consume the
     * prompt under the outgoing query, overwrite the active session's draft,
     * and lose the destination's launch flags. The caller publishes the text
     * only on the commit of `to` (or ChatPage's own `?sid=` rewrite of it for
     * `slotKey`, with that session active) and drops it on any other commit.
     */
    setPendingInput?: (text: string, to: string, slotKey: string | null) => void
  },
): void {
  // Best-effort raise — a channel-delivered intent has no user activation, so
  // browsers may veto this; the opener-focus on the popout side is the
  // reliable path and this is just the assist for the claimed-but-not-opener
  // main. Raised before the leave check so its confirm shows on a window the
  // user can see.
  try { window.focus() } catch { /* vetoed — non-fatal */ }
  const prefill = intent.prefill
  if (prefill?.append !== undefined && intent.path === '/chat'
    && intent.slotKey === prefill.slotKey && deps.appendToOpenComposer?.(prefill.slotKey, prefill.append)) {
    return
  }
  if (!deps.mayLeave()) return
  let destination = `${intent.path}${intent.search ?? ''}`
  if (prefill) writePrefill(prefill.slotKey, prefill.prompt)
  if (intent.pendingInput) {
    deps.setPendingInput?.(intent.pendingInput, destination, intent.slotKey ?? null)
  }
  if (intent.errorHandoffs?.length) {
    // The forwarding window already spent its queue. If this window cannot
    // re-stage it, use ChatPage's fresh-session launch (`?new=1&prefill=`) so
    // the diagnostic seeds only the session that launch creates; publishing
    // through pendingInput would let usePendingInputIntake overwrite an open
    // session's unsent draft. "Ask the agent" always targets /chat
    // (sendErrorToChat navigates there after staging, and the pop-out's hard
    // exit hands off /chat), so a prompt paired with any other destination is
    // stale, and dropping it is safer than writing it over a retained draft.
    if (handoffToChat(intent.errorHandoffs)) notifyChatHandoffs()
    else if (intent.path === '/chat' || intent.path.startsWith('/chat/')) {
      const params = new URLSearchParams(intent.search ?? '')
      params.delete('sid')
      params.delete('slot')
      params.set('new', '1')
      params.set('prefill', intent.errorHandoffs.join('\n\n'))
      destination = `${intent.path}?${params}`
    }
  }
  if (intent.chatLaunch) writeChatLaunch(intent.chatLaunch)
  if (intent.slotKey) deps.switchSlot(intent.slotKey)
  deps.navigate(destination)
}
