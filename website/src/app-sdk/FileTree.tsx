/**
 * The dashboard's project file tree, offered to apps (`@kirocrew/app-sdk`).
 *
 * It is the same `pierre/PierreWorkspaceTreeImpl` the chat window's Files rail
 * and folder tab render: expandable folders, the changed-file colours, the
 * "limit reached" rows, and a local filter over the names it has listed. What
 * differs is the client behind it:
 *
 * - An EXTERNAL app gets a client built over its own scoped transport, once its
 *   manifest's `permissions.api` declares the tree read (`/api/project/tree`).
 *   Without it the tree renders a refusal and sends nothing. The git status read
 *   (`/api/project/git/status`) is a separate declaration: without it the rows
 *   are drawn uncoloured and no status request is sent. This grants nothing new:
 *   the scoped transport checks every request against the declaration, the same
 *   guardrail `useAppApi()` applies, and the gateway answers the tree read only
 *   for a project directory it knows, behind its sensitive-path refusals.
 * - A BUILTIN page is host code. It keeps the dashboard's client, exactly as it
 *   keeps the dashboard's query client (`appQueryClient.ts`).
 * - With no app identity the tree fails closed, like an external app with
 *   nothing declared, even under a scoped provider that declares the reads.
 *
 * The chat handoffs (Add to chat, drag to the composer, Upload files, New file)
 * are not offered: they write into the dashboard's composer and file dialogs,
 * which an app page does not have. An external app's tree also drops the row
 * menu's Download and the rows other installed apps contribute: each sends its
 * own request outside the tree's declared reads. Clicking a file hands its
 * absolute path to `onOpenFile`.
 *
 * The tree's runtime loads behind the lazy boundary in `pierre/tree.tsx`, so the
 * SDK barrel the dashboard shell loads for every app does not carry it.
 */
import { useContext, useLayoutEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'
import { FILE_SEARCH_TIMEOUT_MS, foldLazyDirectories, projectTreeUrl, type ProjectTreePayload } from '../api/client/files'
import { withDeadline } from '../lib/withDeadline'
import { RETRYABLE_SEARCH_CAUSES, searchErrorCause, TREE_FAILURE_KEYS } from '../lib/searchErrorCause'
import { PierreWorkspaceTree, TreeSkeleton } from '../pierre/tree'
import {
  DASHBOARD_WORKSPACE_TREE_CLIENT,
  WorkspaceTreeClientProvider,
  type ProjectGitStatusPayload,
  type WorkspaceTreeClient,
} from '../pierre/workspaceTreeClient'
import { reportForError } from '../utils/errorReport'
import { gitFilterRefusalCause, gitFilterRefusalCopyKey, isGitFilterRefusal } from '../utils/gitStatusError'
import { i18nT } from '../i18n/t'
import { useAppIdentity } from './identity'
import { AppSdkContext, apiPathAllowed, type AppApi } from './scopedApi'

/** The endpoint behind each client member. */
export const FILE_TREE_PATHS = {
  tree: '/api/project/tree',
  gitStatus: '/api/project/git/status',
} as const

const NO_PATHS: readonly string[] = []

/** The client for these declared paths, or null when the tree read is not
 *  declared. Each request is the one the dashboard's own client sends, under the
 *  same deadline. Exported for tests. */
export function scopedWorkspaceTreeClient(api: AppApi, allowedPaths: readonly string[]): WorkspaceTreeClient | null {
  const P = FILE_TREE_PATHS
  if (!apiPathAllowed(allowedPaths, P.tree)) return null
  return {
    tree: (projectDir, dir) =>
      withDeadline(FILE_SEARCH_TIMEOUT_MS, undefined, signal =>
        api.get<ProjectTreePayload>(projectTreeUrl(projectDir, dir), { signal })).then(foldLazyDirectories),
    gitStatus: apiPathAllowed(allowedPaths, P.gitStatus)
      ? projectDir => api.get<ProjectGitStatusPayload>(P.gitStatus + '?path=' + encodeURIComponent(projectDir))
      : null,
    hostFileActions: false,
  }
}

export interface AppFileTreeProps {
  /** The project directory to list. The gateway lists only a directory it knows
   *  as a project (a chat's project folder); any other answers "not found". */
  projectDir: string
  /** Called with a file's ABSOLUTE path when the user opens it. */
  onOpenFile?: (path: string) => void
  /** Absolute path of the file the page shows, echoed as the tree's selection. */
  selectedPath?: string | null
  /** Filters the rows to names matching it; empty or null shows every row. */
  searchQuery?: string | null
}

/** The tree, or the notice for a failed read in its place. The read is the
 *  tree's own query (same key, same client), so this costs no extra request.
 *  Neither notice here offers the agent hand-off: it leaves the app's page, which
 *  may hold edits the page has not saved (as `ProjectPicker`'s refusal). */
function TreeBody({ client, projectDir, onOpenFile, selectedPath, searchQuery, active }: AppFileTreeProps & { client: WorkspaceTreeClient; active: boolean }) {
  // The opened file: the last one handed to `onOpenFile`, or the file the page
  // itself moved `selectedPath` to. `onOpenFile` fires only when the tree reports
  // a file other than this one, so a selection the tree reports again (a listing
  // refresh that keeps it, the second notification of a click on a filtered
  // row, a re-click) is no open. Read and written synchronously: those repeats
  // can arrive before a re-render.
  const lastOpened = useRef<string | null>(null)
  const q = useQuery({
    queryKey: ['project-tree', projectDir],
    queryFn: () => client.tree(projectDir),
    // A hidden host (`info.active` false) pauses the reads, as the tree's own do.
    enabled: !!projectDir && active,
    retry: false,
    staleTime: 10_000,
  })
  // A page that shows another file of the project moves the opened file: the
  // tree selects that row and drops the others. A layout effect runs after the
  // tree's own, so a notification the tree sends for the previous file in that
  // same commit still matches and stays silent. Nothing else moves it: a file
  // outside the project, or a cleared `selectedPath`, leaves the tree's
  // selection as it was (limit: re-picking that still-selected file is no
  // change until another file is opened).
  const root = q.data?.root
  useLayoutEffect(() => {
    if (selectedPath && root && selectedPath.startsWith(`${root}/`)) lastOpened.current = selectedPath
  }, [selectedPath, root])
  if (q.isError) {
    const cause = searchErrorCause(q.error)
    return (
      // No hand-off: the hosting app's page may hold an unsaved draft of its own.
      <ErrorNotice
        testId="app-file-tree-error"
        message={i18nT(TREE_FAILURE_KEYS[cause])}
        report={reportForError(q.error)}
        actions={RETRYABLE_SEARCH_CAUSES.has(cause) ? (
          <Btn type="button" onClick={() => { void q.refetch() }}>
            {i18nT('components.workspaceTree.folder_listing_retry')}
          </Btn>
        ) : undefined}
      />
    )
  }
  if (!q.data) return <TreeSkeleton />
  return (
    // Fills its host whether the host is a flex column or a sized block; the
    // tree itself sizes as a flex item (`flex: 1; height: 100%`).
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <StatusNotice client={client} projectDir={projectDir} />
      <div className="flex min-h-0 flex-1 flex-col">
        <PierreWorkspaceTree
          projectDir={projectDir}
          onFileOpen={path => {
            if (path === lastOpened.current) return
            lastOpened.current = path
            onOpenFile?.(path)
          }}
          selectedPath={selectedPath}
          searchQuery={searchQuery || null}
          active={active}
        />
      </div>
    </div>
  )
}

/** Why the changed-file colours are missing when the status read failed. The
 *  tree draws that failure only in its changed-files mode, which the export
 *  never uses, so without this an app that declared the read would lose its
 *  colours with no word why. A disabled observer of the tree's own status
 *  query: it reads that query's state and never sends a request. */
function StatusNotice({ client, projectDir }: { client: WorkspaceTreeClient; projectDir: string }) {
  const status = useQuery({
    queryKey: ['git-status', projectDir],
    queryFn: () => client.gitStatus!(projectDir),
    enabled: false,
  })
  if (!client.gitStatus || !status.error) return null
  const err = status.error
  return (
    // No hand-off: the hosting app's page may hold an unsaved draft of its own.
    <ErrorNotice
      variant="inline"
      impact="partial"
      className="mx-2 mt-1.5 whitespace-normal"
      testId="app-file-tree-status-error"
      message={isGitFilterRefusal(err)
        ? i18nT(gitFilterRefusalCopyKey(gitFilterRefusalCause(err)))
        : i18nT('components.workspaceTree.status_failed')}
      report={reportForError(err)}
    />
  )
}

export default function FileTree(props: AppFileTreeProps) {
  const identity = useAppIdentity()
  const sdk = useContext(AppSdkContext)
  const builtin = identity?.origin === 'builtin'
  // Read off the context directly, not through useAppApi(), which throws with
  // no provider: a surface with no provider must fail CLOSED.
  const allowedPaths = sdk?.info.permissions.api ?? NO_PATHS
  const api = sdk?.api
  // No identity is the refusal even under a scoped provider: only a host
  // publishes one, so a provider without it is not an app page.
  const client = useMemo(
    () => (builtin ? DASHBOARD_WORKSPACE_TREE_CLIENT : identity && api ? scopedWorkspaceTreeClient(api, allowedPaths) : null),
    [builtin, identity, api, allowedPaths],
  )
  if (!client) {
    return (
      // No hand-off: the hosting app's page may hold an unsaved draft of its own.
      <ErrorNotice
        variant="inline"
        testId="app-file-tree-not-declared"
        message={i18nT('appSdk.fileTree.not_declared', { path: FILE_TREE_PATHS.tree })}
      />
    )
  }
  if (!props.projectDir) return null
  return (
    <WorkspaceTreeClientProvider client={client}>
      {/* Keyed on the project: the tree's model, its loaded folders and its
          refusals belong to one project, so a new one starts fresh. */}
      <TreeBody key={props.projectDir} {...props} client={client} active={sdk?.info.active !== false} />
    </WorkspaceTreeClientProvider>
  )
}
