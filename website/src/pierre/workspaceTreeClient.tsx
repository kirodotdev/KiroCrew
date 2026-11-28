/**
 * The reads the shared workspace tree makes, as one injectable client.
 *
 * `PierreWorkspaceTreeImpl` lists a project through this client, so the same
 * tree can be hosted by an app (`app-sdk/FileTree.tsx`) with a client built over
 * that app's scoped transport.
 *
 * With no provider in context the tree uses the dashboard's own client: a
 * dashboard surface never provides one.
 */
import { createContext, useContext, type ReactNode } from 'react'
import { api } from '../api/client'
import type { ProjectTreePayload } from '../api/client/files'

export type ProjectGitStatusPayload = Awaited<ReturnType<typeof api.projectGitStatus>>

export interface WorkspaceTreeClient {
  /** One level of the project (`/api/project/tree`), or of its folder `dir`. */
  tree(projectDir: string, dir?: string): Promise<ProjectTreePayload>
  /** The project's working-tree status (`/api/project/git/status`), which colours
   *  changed rows. Null when the host cannot read it: rows are drawn uncoloured. */
  gitStatus: ((projectDir: string) => Promise<ProjectGitStatusPayload>) | null
  /** Whether the tree offers the dashboard's own actions: the row menu's Download
   *  and the rows installed apps contribute, each sending its own request
   *  (`/api/file-download`, the contributing app's endpoint), not a tree read; and
   *  its notices' agent hand-off, which navigates to the chat and would unmount an
   *  app page with its unsaved state. */
  hostFileActions: boolean
}

/** The dashboard's client. Each member calls through `api` at call time, so a
 *  test that mocks the client module sees each call with its own arguments. */
export const DASHBOARD_WORKSPACE_TREE_CLIENT: WorkspaceTreeClient = {
  tree: (...args) => api.projectTree(...args),
  gitStatus: (...args) => api.projectGitStatus(...args),
  hostFileActions: true,
}

const WorkspaceTreeClientContext = createContext<WorkspaceTreeClient | null>(null)

export function WorkspaceTreeClientProvider({ client, children }: { client: WorkspaceTreeClient; children: ReactNode }) {
  return <WorkspaceTreeClientContext.Provider value={client}>{children}</WorkspaceTreeClientContext.Provider>
}

/** The client the nearest host provided, else the dashboard's own. */
export function useWorkspaceTreeClient(): WorkspaceTreeClient {
  return useContext(WorkspaceTreeClientContext) ?? DASHBOARD_WORKSPACE_TREE_CLIENT
}
