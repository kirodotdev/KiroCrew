/**
 * Files and projects on the gateway host: worktree creation, recent
 * projects, directory, drive and file browsing, project git status/log/tree,
 * named workspaces, reveal/open, the file picker, diff, fuzzy search, path
 * completion, composer uploads and screenshots.
 */

import { relocateRequestUrl } from '../../lib/dashboardRuntime'
import { i18nT } from '../../i18n/t'
import { resizeImageForModel, type ResizeInfo } from '../../utils/resizeImage'
import type { ClientTransport } from './transport'

/** Response of POST /api/reveal. When the backend cannot drive a file manager
 *  (a remote or headless session) it degrades to a clipboard copy: `copy` is the
 *  path to write. The caller (`revealOrOpen`) writes the clipboard silently — the
 *  affordance that routed here already promised a copy. */
type RevealResult = { copy?: string }

/** Deadline for the whole-tree walks, `api.fileSearch` and `api.projectTree`. Its own literal,
 *  deliberately not an alias of the skills menu's: the two happen to agree today, and retuning
 *  that menu must not silently retune these walks. The tree read shares this bound rather than
 *  the listings' 10s because it walks the tree, which is what the figures below timed.
 *  This bounds an unbounded wait, it does not judge staleness — a reply for a
 *  superseded query cannot be shown as the answer to a newer one, since the query key
 *  carries `debouncedQuery` — so a merely slow walk is still worth waiting for.
 *
 *  It has to be, because the timeout path offers recovery: a bound under the honest walk time
 *  fails every attempt alike. The walk is ceilinged, not open-ended (`_WALK_MAX_DIRS_VISITED`
 *  20k dirs, `_WALK_MAX_SCAN_SCOPED` 50k entries), so its worst case follows those ceilings
 *  rather than repo size. Measured on trees that reach a ceiling: an 877k-dir tree 4.20s,
 *  `/var` 0.35s cold against 0.34s warm, `/usr` 0.17s. The worst is 28% of this budget, and
 *  cold-vs-warm moved 2%, so store latency dominates cache state — a store ~3.6x slower than
 *  that tree would exhaust 15s, and no tree size alone can.
 *
 *  Margin: this is the only wall-clock bound on either read — the backend's pool ceiling bounds
 *  how many probes can wedge, not how long a client waits (`_PATH_PROBE_EXEC_CEILING_SECS`). The
 *  search's own work is the ceilinged walk above, worst 4.20s, leaving ~10s of 15 for admission
 *  (2s at most, then a coded 503) and round trips. The tree read's git steps carry per-step kill
 *  switches (`rev-parse` 5s, `ls-files` 15s) that sum PAST this bound on purpose: a git wedged that
 *  long is what the bound is for, and the read rejects here at 15s into the Refresh notice.
 *  The walk figures are local-disk only; a network-mount sample, and a slow-store escape if the
 *  headroom does not hold there, are tracked in #11419 -- until then a timed-out search's Retry /
 *  Refresh re-enters this same bound, deliberately (see lib/withDeadline.ts on retry).
 *
 *  One constant rather than one per surface: a caller's `limit` only caps rows returned
 *  (the server truncates before responding), so a wider page is not a longer walk. */
export const FILE_SEARCH_TIMEOUT_MS = 15_000

/** Deadline for the picker/panel listing endpoints (`api.browseFiles`, `api.browseDirs`,
 *  `api.recentProjects`). Its own constant rather than the composer menus' 15s: a listing
 *  is a different endpoint on the same wedged gateway, so retuning the skills menu must
 *  not silently retune folder listings.
 *
 *  Shorter than the search's 15s because a listing is ONE `scandir` plus a per-entry directory
 *  test, so its cost tracks a single directory's ENTRY COUNT rather than tree size. Measured that
 *  way: the widest directory on the dev host, `/usr/share/man/man3` at 8,974 entries, listed in
 *  3.1ms cold against 3.0ms warm — 0.03% of this budget, so cache state is not the term and the
 *  bound tolerates a store some 3,200x slower per entry. Local storage only; no network mount was
 *  available to sample, which is the one case this figure does not cover.
 *
 *  Margin, in place of that sample: this is the only wall-clock bound on the read — the backend's
 *  pool ceiling bounds how many probes can wedge, not how long a client waits, by design. Its own
 *  per-request work is a 2s admission wait at most (then a coded 503, not silence) plus the
 *  millisecond listing above, so healthy work spends under a quarter of the 10s and the rest is
 *  round trips and store latency. A mount whose per-entry stats outrun that remainder rejects HERE
 *  at 10s into the notice (Retry / Refresh) path: the bounded refusal is the designed outcome, not a
 *  hang. No config knob, because a knob asks the user to know a number this comment exists to spare
 *  them. */
export const BROWSE_FILES_TIMEOUT_MS = 10_000

export function createFilesEndpoints({ post, put, del, j, checkSessionExpired, withJournaledDeadline }: ClientTransport) {
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
    recentProjects: () => withJournaledDeadline(BROWSE_FILES_TIMEOUT_MS, undefined, '/api/recent-projects', s => fetch(relocateRequestUrl('/api/recent-projects'), { signal: s }).then(j)) as Promise<{ dirs: string[] }>,
    // Bounded HERE, not per initiator: react-query dedupes on the key, so the weakest
    // initiator would decide the bound.
    browseDirs: (path?: string) => withJournaledDeadline(BROWSE_FILES_TIMEOUT_MS, undefined, '/api/browse-dirs', s => fetch(relocateRequestUrl('/api/browse-dirs' + (path ? '?path=' + encodeURIComponent(path) : '')), { signal: s }).then(j)) as Promise<{ path: string; parent: string; dirs: { name: string; path: string }[] }>,
    /** Windows only: the mounted drive roots, as the virtual level above every `X:\`. `path` is `""`: this listing is not a directory. */
    browseDrives: () => withJournaledDeadline(BROWSE_FILES_TIMEOUT_MS, undefined, '/api/browse-dirs', s => fetch(relocateRequestUrl('/api/browse-dirs?drives=1'), { signal: s }).then(j)) as Promise<{ path: string; parent: string; dirs: { name: string; path: string }[] }>,
    browseFiles: (path?: string, signal?: AbortSignal) => withJournaledDeadline(BROWSE_FILES_TIMEOUT_MS, signal, '/api/browse-files', s => fetch(relocateRequestUrl('/api/browse-files' + (path ? '?path=' + encodeURIComponent(path) : '')), { signal: s }).then(j)) as Promise<{ path: string; parent: string; dirs: { name: string; path: string; mtime: number }[]; files: { name: string; path: string; mtime: number }[] }>,
    projectGit: (path: string) => fetch(relocateRequestUrl('/api/project/git?path=' + encodeURIComponent(path))).then(j) as Promise<{ path: string; repo: boolean; repoRoot?: string; branch?: string; detached?: boolean; head?: string }>,
    projectGitStatus: (path: string) => fetch(relocateRequestUrl('/api/project/git/status?path=' + encodeURIComponent(path))).then(j) as Promise<{ repo: boolean; repoRoot?: string; branch?: string; ahead?: number; behind?: number; truncated?: boolean; files: { path: string; status: string; staged: boolean; additions?: number; deletions?: number }[] }>,
    projectGitLog: (path: string, limit = 20) => fetch(relocateRequestUrl('/api/project/git/log?path=' + encodeURIComponent(path) + '&limit=' + limit)).then(j) as Promise<{ repo: boolean; commits: { sha: string; message: string; author: string; date: string; isHead: boolean }[] }>,
    projectTree: (path: string) => withJournaledDeadline(FILE_SEARCH_TIMEOUT_MS, undefined, '/api/project/tree', s =>
      fetch(relocateRequestUrl('/api/project/tree?path=' + encodeURIComponent(path)), { signal: s }).then(j)) as Promise<{ root: string; paths: string[]; directories?: string[]; repo: boolean; truncated?: boolean; truncatedDirectories?: string[]; hiddenOnlyDirectories?: string[]; unreadableDirectories?: string[]; linkedDirectories?: string[] }>,
    workspaces: () => fetch(relocateRequestUrl('/api/workspaces')).then(j),
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
    fileDiff: (path: string) => fetch(relocateRequestUrl('/api/file-diff?path=' + encodeURIComponent(path))).then(j) as Promise<{ diff: string; original: string; status?: 'clean' | 'modified' | 'untracked' | 'not_git' | 'error' }>,
    /** Fuzzy file search for @-mention picker. `kind` distinguishes folder hits from files.
     *  `kinds` narrows the result set server-side — 'files' or 'dirs'; omitted returns both.
     *  Filtering server-side rather than dropping unwanted hits here matters because the
     *  backend caps results BEFORE the response, so a client-side filter would silently
     *  shrink an already-capped list. `limit` raises the server's result cap (default 15);
     *  the server clamps it to a fixed ceiling, so a large value cannot amplify the walk.
     *
     *  Bounded HERE, not per initiator: react-query dedupes on the key, so the
     *  weakest initiator would otherwise decide whether the promise is bounded —
     *  and a future caller would arrive unbounded by default. */
    fileSearch: (q: string, project?: string, signal?: AbortSignal, kinds?: 'files' | 'dirs', limit?: number) => {
      const p = new URLSearchParams({ q })
      if (project) p.set('project', project)
      if (kinds) p.set('kinds', kinds)
      if (limit) p.set('limit', String(limit))
      return withJournaledDeadline(FILE_SEARCH_TIMEOUT_MS, signal, '/api/file-search', s =>
        fetch(relocateRequestUrl(`/api/file-search?${p}`), { signal: s }).then(j)) as Promise<{ results: Array<{ path: string; name: string; size: number; mtime: number; kind?: 'file' | 'dir' }>; root: string }>
    },
    /** One directory level of a project, for the composer's `./` path completion.
     *  `dir` is the literal prefix typed (`./`, `../src/`) and `q` the partial entry
     *  name; the server resolves `dir` under the named project and answers an empty
     *  set for anything that leaves the project root. */
    pathComplete: (project: string, dir: string, q: string, signal?: AbortSignal) => {
      const p = new URLSearchParams({ path: project, dir })
      if (q) p.set('q', q)
      return fetch(relocateRequestUrl(`/api/path-complete?${p}`), signal ? { signal } : undefined).then(j) as Promise<{ results: Array<{ path: string; name: string; size: number; mtime: number; kind?: 'file' | 'dir' }>; root: string; outside?: boolean }>
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
      const res = await fetch(relocateRequestUrl('/api/upload/file'), { method: 'POST', body: fd, ...(signal ? { signal } : {}) })
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
