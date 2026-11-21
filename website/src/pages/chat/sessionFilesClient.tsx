/**
 * The reads the chat's "Edited" list makes, as one injectable client.
 *
 * `SessionFilesList` reads a session's edited files, and the recorded texts of
 * one edit, through this client, so the same list can be hosted by an app
 * (`app-sdk/SessionChanges.tsx`) with a client built over that app's scoped
 * transport.
 *
 * With no provider in context the list uses the dashboard's own client: a
 * dashboard surface never provides one.
 */
import { createContext, useContext, type ReactNode } from 'react'
import { api } from '../../api/client'
import type { SessionFileDiffPayload, SessionFilesPayload } from '../../api/client/files'

export interface SessionFilesClient {
  sessionFiles(slot: string): Promise<SessionFilesPayload>
  sessionFileDiff(slot: string, path: string): Promise<SessionFileDiffPayload>
}

/** The dashboard's client. Each member calls through `api` at call time, so a
 *  test that mocks the client module sees each call with its own arguments. */
export const DASHBOARD_SESSION_FILES_CLIENT: SessionFilesClient = {
  sessionFiles: (...args) => api.sessionFiles(...args),
  sessionFileDiff: (...args) => api.sessionFileDiff(...args),
}

const SessionFilesClientContext = createContext<SessionFilesClient | null>(null)

export function SessionFilesClientProvider({ client, children }: { client: SessionFilesClient; children: ReactNode }) {
  return <SessionFilesClientContext.Provider value={client}>{children}</SessionFilesClientContext.Provider>
}

/** The client the nearest host provided, else the dashboard's own. */
export function useSessionFilesClient(): SessionFilesClient {
  return useContext(SessionFilesClientContext) ?? DASHBOARD_SESSION_FILES_CLIENT
}
