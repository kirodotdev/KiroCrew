import { createContext } from 'react'

/**
 * Host hook for a session reference the renderer cannot resolve.
 *
 * The session roster a transcript resolves chips and `?sid=` links against is
 * the open tabs plus the closed sessions already listed under the sidebar's
 * "Older sessions". That list is fetched lazily — nothing shows it at mount —
 * so a transcript rendered on a fresh load can name a closed session the
 * roster has not heard of yet. When a well-formed session key misses the
 * roster, the renderer reports it here and the page decides whether to seed
 * the list. `null` means no host is listening (a renderer outside ChatPage),
 * and a miss is then simply a miss.
 *
 * Lives in its own module (not MarkdownRenderer) for the same reason as
 * `JiraHostsCtx`: many tests mock MarkdownRenderer down to a bare default
 * export, and the provider (ChatPage) must not couple its imports to that
 * module's export surface.
 */
export const SessionRosterMissCtx = createContext<((key: string) => void) | null>(null)
