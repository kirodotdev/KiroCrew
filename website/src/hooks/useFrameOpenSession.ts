import { useCallback, useEffect, useRef, type RefObject } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { apiErrorCode } from '../api/apiError'
import type { CrewBoardOpenDashboard } from '../api/crewBoard'
import { useMayLeaveForNavigation } from '../components/NavigationLeaveGuard'
import { useAppDispatch } from '../store'
import { switchSlot } from '../store/chatSlice'
import { applyNavIntentInMain } from '../utils/navIntent'
import { hasGesture } from './useFrameOpenLink'

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
 * - the person just clicked or pressed a key IN that frame (`hasGesture`).
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
 * Honour open-session messages from the given frames. `open` and `gesture` are injectable
 * for tests only; by default the session opens the way every in-app session link does
 * (`applyNavIntentInMain`: leave check, slot switch, then `/chat`).
 */
export function useFrameOpenSession(
  frame: RefObject<HTMLIFrameElement | null>,
  dashboard: CrewBoardOpenDashboard | null,
  inject: {
    open?: (slot: string) => void
    gesture?: (frame: HTMLIFrameElement) => boolean
  } = {},
): void {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const mayLeave = useMayLeaveForNavigation()
  const openInMain = useCallback(
    (slot: string) =>
      applyNavIntentInMain(
        // `path` carries no query string, and ChatPage writes `?sid=` back itself once
        // the session is active.
        { path: '/chat', slotKey: slot },
        {
          navigate,
          switchSlot: (key) => {
            void dispatch(switchSlot({ key, announceOnMissing: true }))
          },
          mayLeave,
        },
      ),
    [navigate, dispatch, mayLeave],
  )

  const frameRef = useRef(frame)
  frameRef.current = frame
  const dashboardRef = useRef(dashboard)
  dashboardRef.current = dashboard
  const openRef = useRef(inject.open ?? openInMain)
  openRef.current = inject.open ?? openInMain
  const gestureRef = useRef(inject.gesture ?? hasGesture)
  gestureRef.current = inject.gesture ?? hasGesture
  /** One request at a time: a second click while the first is out would navigate twice. */
  const pending = useRef(false)
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
      if (!asked || pending.current) return
      pending.current = true
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
      api
        .crewBoardOpen(asked, itemId)
        .then((result) => {
          if (!mounted.current) return
          if (!result || typeof result.slot !== 'string' || !result.slot) {
            answer('open_failed')
            return
          }
          answer(null)
          openRef.current(result.slot)
        })
        .catch((err: unknown) => {
          answer(apiErrorCode(err) || 'open_failed')
        })
        .finally(() => {
          pending.current = false
        })
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
}
