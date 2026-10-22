import { appPathname } from './basePath'

/** The chat route, spelled once. Two things key off it on the phone -- the
 *  single-bar header variant and the shell nav drawer's swipe gate -- and a
 *  drift between two spellings is exactly how "two drawers for one gesture"
 *  would come back. */
export const isChatRoute = (pathname: string) => pathname === '/chat' || pathname.startsWith('/chat/') || pathname === '/'

/**
 * Whether the dashboard is on the chat route right now, for "create a session,
 * then open chat" actions.
 *
 * Read from the live URL when called, not from a render: the create is async,
 * and the user may have moved pages before it resolves. On the chat page the
 * URL sync is the one writer of `?sid=` and already gives the new session its
 * single history entry. A `navigate('/chat')` there lands between that write
 * and the sync's next run, which pushes `?sid=` back on top, leaving entries
 * Back has to step through. Callers navigate only when this returns false.
 */
export function isOnChatRoute(): boolean {
  return isChatRoute(appPathname())
}
