import { createContext } from 'react'

/**
 * Which host page owns the "Run in terminal" buttons beneath it. A host
 * (ChatPage, MembersPage) provides the scope its useRunInTerminalBridge
 * returned, RunInTerminalBtn stamps it on the request, and only that host
 * answers. Null outside every host.
 */
export const RunInTerminalScope = createContext<string | null>(null)

/** The slot whose own transcript handed over the command. Terminal execution
 *  scope also covers side panels; only the main transcript opts into reporting. */
export const RunInTerminalOriginSlot = createContext<string | null>(null)
