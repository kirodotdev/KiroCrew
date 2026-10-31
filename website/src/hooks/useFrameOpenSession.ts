import { useCallback, useEffect, useRef, type RefObject } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { apiErrorCode } from '../api/apiError'
import type { CrewBoardOpenDashboard } from '../api/crewBoard'
import { useMayLeaveForNavigation } from '../components/NavigationLeaveGuard'
import { useAppDispatch } from '../store'
import { switchSlot } from '../store/chatSlice'
import { applyNavIntentInMain } from '../utils/navIntent'
import { hasGesture } from './useFrameOpenLink'
import { tiedToFrame } from './useFrameLinkBridge'

/**
 * A sandboxed org-chart page asking its host to open the session behind one worker card.
 *
 * The page posts `{type: 'kirocrew-dashboard:open-session', item_id}` and nothing else.
 * It holds no session key and no board it could name: the fold masks the key, and a board
 * alias is minted per render. So the host asks the gateway to find the item in the
 * dashboard this frame shows (`api.crewBoardOpen`), gets the worker's slot key back, and
 * navigates there itself. The key reaches this hook's JavaScript only. It is never posted
 * into the frame and never appears in the document.
 *
 * The message is honoured only when ALL of these hold, like the pull-request bridge in
 * `useFrameOpenLink`:
 *
 * - it came from one of THIS host's own frames (`event.source`);
 * - `item_id` is a non-empty string (the gateway route checks its shape and refuses a bad id);
 * - the person just clicked or pressed a key IN that frame (`hasGesture`);
 * - the dashboard can tie that click to the frame (`tiedToFrame`): a page that focuses itself
 *   after the owner typed or clicked elsewhere in the host passes `hasGesture` alone, so it
 *   could switch the owner's session without a card click. An untied request opens nothing and
 *   the page is told `open_untied`, which it draws as a note asking for another click on the button.
 *
 * The switch goes through `applyNavIntentInMain`, which asks the app shell's leave guard first. A
 * Questions panel holding an unsent answer registers one, so this action asks before it throws
 * the answer away, like the sidebar and every other exit that asks.
 *
 * A popout or embed window cannot follow the key: its router re-pins any other route to the
 * window's own frame, so a `/chat` navigation would land back on the session it already
 * shows while the page was told the open worked. There the host asks nothing and answers
 * `open_unavailable`, which the page draws as a note telling the person to use the main window.
 *
 * The host answers the page with `{type: 'kirocrew-dashboard:open-session-result', item_id, code}`:
 * `code` is `null` once the gateway found the session, or the gateway's refusal code (the worker
 * closed, the id is on two boards, a stale card). The page draws it on the card that asked. The
 * answer carries an item id and a code and never a session key.
 */
export const OPEN_SESSION_MESSAGE_TYPE = 'kirocrew-dashboard:open-session'

/**
 * The item id an open-session message asks for, or null when the message is not one this host honours.
 * The shape of the id is not checked here: the gateway route refuses a bad id, and its refusal code
 * is posted back to the page like any other.
 */
export function openSessionItemOf(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null
  const msg = data as { type?: unknown; item_id?: unknown }
  if (msg.type !== OPEN_SESSION_MESSAGE_TYPE || typeof msg.item_id !== 'string') return null
  return msg.item_id ? msg.item_id : null
}

/** What the host posts back into the frame once the gateway has answered. */
export const OPEN_SESSION_RESULT_TYPE = 'kirocrew-dashboard:open-session-result'

/**
 * Honour open-session messages from the given frames. `open`, `gesture` and `tied` are injectable
 * for tests only; by default the session opens the way every in-app session link does
 * (`applyNavIntentInMain`: leave check, slot switch, then `/chat`).
 */
export function useFrameOpenSession(
  frame: RefObject<HTMLIFrameElement | null>,
  dashboard: CrewBoardOpenDashboard | null,
  inject: {
    open?: (slot: string) => boolean | void
    gesture?: (frame: HTMLIFrameElement) => boolean
    tied?: (frame: HTMLIFrameElement) => boolean
  } = {},
): void {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const dispatch = useAppDispatch()
  const mayLeave = useMayLeaveForNavigation()
  const openInMain = useCallback(
    (slot: string): boolean => {
      // The leave guard decides FIRST, once, so the page is told what actually happened. The same
      // answer is handed on, because `applyNavIntentInMain` would otherwise ask again and a guard
      // that prompts would prompt twice.
      const allowed = mayLeave()
      applyNavIntentInMain(
        // `path` carries no query string, and ChatPage writes `?sid=` back itself once
        // the session is active.
        { path: '/chat', slotKey: slot },
        {
          navigate,
          switchSlot: (key) => {
            void dispatch(switchSlot({ key, announceOnMissing: true, gesture: true }))
          },
          mayLeave: () => allowed,
        },
      )
      return allowed
    },
    [navigate, dispatch, mayLeave],
  )

  const frameRef = useRef(frame)
  frameRef.current = frame
  const dashboardRef = useRef(dashboard)
  dashboardRef.current = dashboard
  const openRef = useRef(inject.open ?? openInMain)
  openRef.current = inject.open ?? openInMain
  const sharedWindowRef = useRef(false)
  sharedWindowRef.current = pathname.startsWith('/popout/') || pathname.startsWith('/embed/')
  const gestureRef = useRef(inject.gesture ?? hasGesture)
  gestureRef.current = inject.gesture ?? hasGesture
  const tiedRef = useRef(inject.tied ?? tiedToFrame)
  tiedRef.current = inject.tied ?? tiedToFrame
  /** The item whose lookup is out. One request at a time: a second click would navigate twice. */
  const pending = useRef<string | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (!event.source) return
      const mine = frameRef.current.current
      if (!mine || mine.contentWindow !== event.source) return
      const itemId = openSessionItemOf(event.data)
      if (!itemId || !gestureRef.current(mine)) return
      const asked = dashboardRef.current
      if (!asked) return
      const askedBy = mine.contentWindow
      /** Tell the page how its request ended, if the frame it asked from is still the one mounted. */
      const answer = (code: string | null) => {
        if (!mounted.current || frameRef.current.current?.contentWindow !== askedBy) return
        // '*' because this frame is sandboxed without allow-same-origin, so its origin is opaque
        // and cannot be named; the post reaches this one frame's window and carries an item id
        // and a refusal code, never a session key.
        // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
        askedBy?.postMessage({ type: OPEN_SESSION_RESULT_TYPE, item_id: itemId, code }, '*')
      }
      if (sharedWindowRef.current) {
        answer('open_unavailable')
        return
      }
      if (!tiedRef.current(mine)) {
        // A click this host cannot tie to the frame (a Tab in from the host, input elsewhere a moment
        // ago) opens nothing, and the page is told so it can ask for a fresh click. No lookup is made.
        answer('open_untied')
        return
      }
      if (pending.current !== null) {
        // The same card again is a double click and is dropped; another card is told to wait.
        if (pending.current !== itemId) answer('open_busy')
        return
      }
      pending.current = itemId
      api
        .crewBoardOpen(asked, itemId)
        .then((result) => {
          if (!mounted.current) return
          if (!result || typeof result.slot !== 'string' || !result.slot) {
            answer('open_failed')
            return
          }
          // Success is posted only once the switch happened. A guard that kept the person here
          // (an unsent answer they chose to keep) is `open_declined`, not a success that went nowhere.
          const opened = openRef.current(result.slot)
          answer(opened === false ? 'open_declined' : null)
        })
        .catch((err: unknown) => {
          answer(apiErrorCode(err) || 'open_failed')
        })
        .finally(() => {
          pending.current = null
        })
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
}
