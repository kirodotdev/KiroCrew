/**
 * Files and projects on the gateway host: worktree creation, recent
 * projects, directory, drive and file browsing, project git status/log/tree,
 * named workspaces, reveal/open, the file picker, diff, fuzzy search, path
 * completion, composer uploads and screenshots.
 */

import { i18nT } from '../../i18n/t'
import { resizeImageForModel, type ResizeInfo } from '../../utils/resizeImage'
import type { ClientTransport } from './transport'

/** Response of POST /api/reveal. When the backend cannot drive a file manager
 *  (a remote or headless session) it degrades to a clipboard copy: `copy` is the
 *  path to write. The caller (`revealOrOpen`) writes the clipboard silently — the
 *  affordance that routed here already promised a copy. */
type RevealResult = { copy?: string }

export function createFilesEndpoints({ post, put, del, j, checkSessionExpired }: ClientTransport) {
  const projects = {
    // Follow-up card: create a sibling git worktree of `repo` on a new `branch`.
    // Resolves with the created path, or rejects with the server's message
    // (branch/dir already exists, not a git repo, git unavailable).
    createWorktree: (repo: string, branch: string) =>
      post('/api/worktree/create', { repo, branch }).then(j) as Promise<{
        ok?: boolean
        path?: string
        branch?: string
        base?: string
        error?: string
      }>,
    recentProjects: () => fetch('/api/recent-projects').then(j) as Promise<{ dirs: string[] }>,
    browseDirs: (path?: string) => fetch('/api/browse-dirs' + (path ? '?path=' + encodeURIComponent(path) : '')).then(j) as Promise<{ path: string; parent: string; dirs: { name: string; path: string }[] }>,
    /** Windows only: the mounted drive roots, as the virtual level above every `X:\`. `path` is `""`: this listing is not a directory. */
    browseDrives: () => fetch('/api/browse-dirs?drives=1').then(j) as Promise<{ path: string; parent: string; dirs: { name: string; path: string }[] }>,
    browseFiles: (path?: string) => fetch('/api/browse-files' + (path ? '?path=' + encodeURIComponent(path) : '')).then(j) as Promise<{ path: string; parent: string; dirs: { name: string; path: string; mtime: number }[]; files: { name: string; path: string; mtime: number }[] }>,
    projectGit: (path: string) => fetch('/api/project/git?path=' + encodeURIComponent(path)).then(j) as Promise<{ path: string; repo: boolean; repoRoot?: string; branch?: string; detached?: boolean; head?: string }>,
    projectGitStatus: (path: string) => fetch('/api/project/git/status?path=' + encodeURIComponent(path)).then(j) as Promise<{ repo: boolean; repoRoot?: string; branch?: string; ahead?: number; behind?: number; truncated?: boolean; files: { path: string; status: string; staged: boolean; additions?: number; deletions?: number }[] }>,
    projectGitLog: (path: string, limit = 20) => fetch('/api/project/git/log?path=' + encodeURIComponent(path) + '&limit=' + limit).then(j) as Promise<{ repo: boolean; commits: { sha: string; message: string; author: string; date: string; isHead: boolean }[] }>,
    projectTree: (path: string) => fetch('/api/project/tree?path=' + encodeURIComponent(path)).then(j) as Promise<{ root: string; paths: string[]; directories?: string[]; repo: boolean; truncated?: boolean; truncatedDirectories?: string[]; hiddenOnlyDirectories?: string[]; unreadableDirectories?: string[]; linkedDirectories?: string[] }>,
    workspaces: () => fetch('/api/workspaces').then(j),
    createWorkspace: (body: object) => post('/api/workspaces', body).then(j),
    updateWorkspace: (name: string, body: object) =>
      put('/api/workspaces/' + encodeURIComponent(name), body).then(j),
    deleteWorkspace: (name: string) =>
      del('/api/workspaces/' + encodeURIComponent(name)).then(j),
  }

  const reveal = {
    // `action` mirrors the backend's own two modes: 'reveal' selects the path in
    // the OS file manager (the default every existing caller relies on), 'open'
    // hands a regular file to its default application. Headless hosts have
    // neither, so the backend answers with `copy` and the path goes to the
    // clipboard instead of the call silently doing nothing.
    // This is a SIDE-EFFECT-FREE transport call: it neither writes the clipboard
    // nor shows a dialog. The single caller `revealOrOpen` owns the clipboard copy,
    // so the degrade is presented in exactly one place instead of in the transport
    // layer where a dialog is a surprise.
    revealPath: (path: string, action: 'open' | 'reveal' = 'reveal') =>
      post('/api/reveal', { path, action }).then(j) as Promise<RevealResult>,
  }

  const fileOps = {
    pickFiles: () => post('/api/upload').then(j) as Promise<{ paths: string[] }>,
    fileDiff: (path: string) => fetch('/api/file-diff?path=' + encodeURIComponent(path)).then(j) as Promise<{ diff: string; original: string; status?: 'clean' | 'modified' | 'untracked' | 'not_git' | 'error' }>,
    /** Fuzzy file search for @-mention picker. `kind` distinguishes folder hits from files.
     *  `kinds` narrows the result set server-side — 'files' or 'dirs'; omitted returns both.
     *  Filtering server-side rather than dropping unwanted hits here matters because the
     *  backend caps results BEFORE the response, so a client-side filter would silently
     *  shrink an already-capped list. `limit` raises the server's result cap (default 15);
     *  the server clamps it to a fixed ceiling, so a large value cannot amplify the walk. */
    fileSearch: (q: string, project?: string, signal?: AbortSignal, kinds?: 'files' | 'dirs', limit?: number) => {
      const p = new URLSearchParams({ q })
      if (project) p.set('project', project)
      if (kinds) p.set('kinds', kinds)
      if (limit) p.set('limit', String(limit))
      return fetch(`/api/file-search?${p}`, signal ? { signal } : undefined).then(j) as Promise<{ results: Array<{ path: string; name: string; size: number; mtime: number; kind?: 'file' | 'dir' }>; root: string }>
    },
    /** One directory level of a project, for the composer's `./` path completion.
     *  `dir` is the literal prefix typed (`./`, `../src/`) and `q` the partial entry
     *  name; the server resolves `dir` under the named project and answers an empty
     *  set for anything that leaves the project root. */
    pathComplete: (project: string, dir: string, q: string, signal?: AbortSignal) => {
      const p = new URLSearchParams({ path: project, dir })
      if (q) p.set('q', q)
      return fetch(`/api/path-complete?${p}`, signal ? { signal } : undefined).then(j) as Promise<{ results: Array<{ path: string; name: string; size: number; mtime: number; kind?: 'file' | 'dir' }>; root: string; outside?: boolean }>
    },
    /** Upload files via browser File API (cross-platform).
     *  `signal` lets the composer abort an upload still in flight: the
     *  request dies client-side and the server unlinks its partials through
     *  the disconnect path the upload handler already has. */
    uploadFiles: async (files: File[], signal?: AbortSignal) => {
      // Downscale oversized images client-side so they fit the model's image
      // limits before they ever reach the server (see resizeImage.ts).
      const prepared = await Promise.all(files.map(f => resizeImageForModel(f)))
      const resized = prepared.map(p => p.info).filter((i): i is ResizeInfo => i !== null)
      const fd = new FormData()
      prepared.forEach(p => fd.append('file', p.file))
      const res = await fetch('/api/upload/file', { method: 'POST', body: fd, ...(signal ? { signal } : {}) })
      checkSessionExpired(res)
      let body: { paths?: unknown; error?: string }
      try { body = await res.json() } catch { body = {} }
      if (!res.ok) return { paths: [] as string[], error: body.error || res.statusText, resized, resizedByPath: {} as Record<string, ResizeInfo> }
      if (!Array.isArray(body.paths)) return { paths: [] as string[], error: i18nT('api.client.unexpected_server_response'), resized, resizedByPath: {} as Record<string, ResizeInfo> }
      // The server appends one path per multipart 'file' part in order, so
      // paths[i] is prepared[i]'s stored location — zip them to key resize
      // details by the exact server path the attachment chip renders from.
      const paths = body.paths as string[]
      const resizedByPath: Record<string, ResizeInfo> = {}
      prepared.forEach((p, i) => { if (p.info && paths[i]) resizedByPath[paths[i]] = p.info })
      return { ...(body as { paths: string[]; error?: string }), resized, resizedByPath }
    },
    screenshot: () => post('/api/screenshot').then(j) as Promise<{ path: string }>,
  }

  return { projects, reveal, fileOps }
}
