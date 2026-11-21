/**
 * The chat's list of the files a session changed, with each change's diff,
 * offered to apps (`@kirocrew/app-sdk`).
 *
 * The list is the same `pages/chat/SessionFilesList` the chat window's Files
 * tab shows under "Edited", and the diff is the same `components/DiffPanel`
 * its diff tab renders. Picking a file shows the change this session recorded
 * to it beside the list. What differs is the client behind them:
 *
 * - An EXTERNAL app gets a client built over its own scoped transport. The
 *   list needs `/api/project/session-files` in the manifest's
 *   `permissions.api`, and the diff needs `/api/project/session-file-diff`.
 *   The two are sibling paths, not parent and child, so neither declaration
 *   covers the other: without the list's path the component renders a refusal
 *   and sends nothing, and without the diff's path picking a file renders a
 *   refusal in the diff's place and sends nothing. This grants nothing new:
 *   the scoped transport still checks every request against the declaration,
 *   the same guardrail `useAppApi()` applies.
 * - A BUILTIN page is host code. It keeps the dashboard's client, exactly as it
 *   keeps the dashboard's query client (`appQueryClient.ts`).
 * - With no app identity at all the component fails closed, like an external
 *   app with nothing declared.
 *
 * The list's "open the file" and "browse the project" hand-offs are not
 * offered: they open tabs in the chat window's side panel, which an app page
 * does not have. Picking a row shows its diff instead.
 *
 * The list and the diff load behind one lazy boundary: this module is part of
 * the SDK barrel the dashboard shell loads up front for every app, while they
 * are only needed once a page shows a session's changes.
 */
import { lazy, Suspense, useContext, useMemo } from 'react'
import type { SessionFileDiffPayload, SessionFilesPayload } from '../api/client/files'
import ErrorNotice from '../components/ErrorNotice'
import { ContentSkeleton } from '../components/ui'
import { SessionFilesClientProvider, type SessionFilesClient } from '../pages/chat/sessionFilesClient'
import { i18nT } from '../i18n/t'
import { useAppIdentity } from './identity'
import { AppSdkContext, apiPathAllowed, type AppApi } from './scopedApi'

const SessionChangesBody = lazy(() => import('./SessionChangesBody'))

/** The endpoint behind each client member. Siblings: declaring one does not
 *  declare the other. */
export const SESSION_CHANGES_PATHS = {
  sessionFiles: '/api/project/session-files',
  sessionFileDiff: '/api/project/session-file-diff',
} as const

const NO_PATHS: readonly string[] = []

/** The client over an app's scoped transport. Each request is the one the
 *  dashboard's own client sends; the transport refuses, without sending, any
 *  path the app did not declare. Exported for tests. */
export function scopedSessionFilesClient(api: AppApi): SessionFilesClient {
  const P = SESSION_CHANGES_PATHS
  return {
    sessionFiles: slot => api.get<SessionFilesPayload>(`${P.sessionFiles}?slot=${encodeURIComponent(slot)}`),
    sessionFileDiff: (slot, path) => api.get<SessionFileDiffPayload>(
      `${P.sessionFileDiff}?slot=${encodeURIComponent(slot)}&path=${encodeURIComponent(path)}`,
    ),
  }
}

export interface AppSessionChangesProps {
  /** The chat session whose changes are shown, the key `ChatEmbed` takes. */
  slotKey: string
  /** The session's project folder: a row names its folder relative to it.
   *  Without it, each row names its absolute folder. */
  projectDir?: string
}

/** The refusal for an undeclared read. The list's fills the component's area
 *  on its own; the diff's names the one file, since the list beside it shows. */
function refusal(key: 'not_declared' | 'diff_not_declared', path: string, testId: string) {
  const notice = (
    <ErrorNotice
      variant="inline"
      className="whitespace-normal max-w-full"
      testId={testId}
      message={key === 'diff_not_declared'
        ? i18nT('appSdk.sessionChanges.diff_not_declared', { path })
        : i18nT('appSdk.sessionChanges.not_declared', { path })}
      askAgent
      // Says what the agent does here, so it does not read as a second
      // person to fix what only the app's developer can change.
      askAgentLabel={i18nT('appSdk.sessionChanges.ask_agent')}
    />
  )
  if (key === 'diff_not_declared') return notice
  return <div className="w-full h-full min-h-0 flex items-center justify-center p-4">{notice}</div>
}

export default function SessionChanges({ slotKey, projectDir = '' }: AppSessionChangesProps) {
  const identity = useAppIdentity()
  const sdk = useContext(AppSdkContext)
  const builtin = identity?.origin === 'builtin'
  // Read off the context directly, not through useAppApi(), which throws with
  // no provider: a surface with no provider must fail CLOSED.
  const allowedPaths = sdk?.info.permissions.api ?? NO_PATHS
  const api = sdk?.api
  const client = useMemo(() => (builtin || !api ? null : scopedSessionFilesClient(api)), [builtin, api])
  // A side-panel tab stays mounted while hidden; it stops polling then.
  const active = sdk?.info.active !== false
  const listAllowed = builtin || (!!client && apiPathAllowed(allowedPaths, SESSION_CHANGES_PATHS.sessionFiles))
  const diffAllowed = builtin || (!!client && apiPathAllowed(allowedPaths, SESSION_CHANGES_PATHS.sessionFileDiff))
  // No identity is the refusal even under a scoped provider: only a host
  // surface that named its app may read a chat's files.
  if (!identity || !listAllowed) return refusal('not_declared', SESSION_CHANGES_PATHS.sessionFiles, 'app-session-changes-not-declared')
  const body = (
    <Suspense fallback={<ContentSkeleton rows={6} />}>
      <SessionChangesBody
        slot={slotKey}
        projectDir={projectDir}
        active={active}
        diffRefusal={diffAllowed ? null : refusal('diff_not_declared', SESSION_CHANGES_PATHS.sessionFileDiff, 'app-session-changes-diff-not-declared')}
      />
    </Suspense>
  )
  return client ? <SessionFilesClientProvider client={client}>{body}</SessionFilesClientProvider> : body
}
