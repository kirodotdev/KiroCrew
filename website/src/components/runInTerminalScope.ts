import { createContext } from 'react'

/**
 * Which host page owns the "Run in terminal" buttons beneath it. A host
 * (ChatPage, MembersPage) provides the scope its useRunInTerminalBridge
 * returned, RunInTerminalBtn stamps it on the request, and only that host
 * answers. Null outside every host.
 */
export const RunInTerminalScope = createContext<string | null>(null)
