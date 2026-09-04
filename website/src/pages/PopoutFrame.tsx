import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { registerPopout } from '../utils/chatPopout'
import { useAppSelector } from '../store'
import ChatPage from './ChatPage'

import { i18nT } from '../i18n/t'

/**
 * The window shell for a popped-out chat session (`/popout/chat/:slug?sid=…`).
 *
 * Renders the existing single-session chat (`ChatPage embedMode="chat"`) — a
 * separate JS context with its own WebSocket + store, so it streams live and
 * independently. On top it (1) registers as a live popout so the main
 * dashboard can track / focus / bring it back, and (2) mirrors the session
 * title into the OS window/taskbar. The "Return to main" control lives in the
 * chat title bar (rendered by ChatPage for `popout`), alongside where the main
 * window shows its pop-out control.
 */
export default function PopoutFrame() {
  const [params] = useSearchParams()
  const sid = params.get('sid') || params.get('slot') || ''
  const title = useAppSelector(s => s.dashboard.slots.find(x => x.key === sid)?.title)

  // Announce presence for `sid` and wire the heartbeat/control responder.
  useEffect(() => {
    if (!sid) return
    return registerPopout(sid)
  }, [sid])

  // Reflect the session in the window/taskbar title so multiple popouts are
  // distinguishable at the OS level.
  useEffect(() => {
    const label = title && title !== sid ? title : i18nT('pages.popoutFrame.session')
    document.title = i18nT('pages.popoutFrame.window_title', { label })
  }, [sid, title])

  return (
    <div className="h-screen w-screen overflow-hidden bg-bg flex flex-col relative">
      <div className="flex-1 min-h-0">
        <ChatPage embedded embedMode="chat" popout />
      </div>
    </div>
  )
}
