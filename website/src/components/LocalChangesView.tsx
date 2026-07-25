import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ChevronDown,
  ChevronRight,
  GitBranch,
  Pen,
} from 'lucide-react'
import { api } from '../api/client'
import DiffView from './UnifiedDiffView'
import { colorForExt, fileIcon } from '../utils/fileIcons'

/** Poll cadence for the local worktree scan while the view is visible. Cheap
 *  on the backend (bounded `git status` per repo) but not free — keep lazy. */
const LOCAL_CHANGES_POLL_MS = 10_000

/** Collapsed repo roots — MODULE scope, because SidePanel unmounts inactive
 *  views on tab switches, which would reset component state. In-memory only
 *  (resets on reload), mirroring the panel-tab store's approach to
 *  survive-the-unmount UI state. */
const collapsedRepoRoots = new Set<string>()

/** Test-only: clear the module-level collapse state between tests. */
export function __resetLocalChangesUi(): void {
  collapsedRepoRoots.clear()
}

/** Bare file name followed by its de-emphasized directory path (VS Code
 *  style: "Deep.tsx  src/components"). The dir shrinks/truncates first so the
 *  name stays readable in narrow panels. Shared by the Local rows and the PR
 *  panel's changed-file rows. */
export function FileNameWithPath({ rel, title }: { rel: string; title?: string }) {
  const slash = rel.lastIndexOf('/')
  const dir = slash === -1 ? '' : rel.slice(0, slash)
  const name = slash === -1 ? rel : rel.slice(slash + 1)
  return (
    <span className="flex items-baseline gap-1.5 min-w-0 text-[13px]" title={title ?? rel}>
      <span className="text-text truncate max-w-full">{name}</span>
      {dir && <span className="text-muted/70 text-[11px] truncate shrink min-w-0">{dir}</span>}
    </span>
  )
}

type GitChangeFile = { path: string; rel: string; status: string; staged: boolean; additions?: number; deletions?: number }
type GitChangesRepo = { root: string; name: string; branch: string; files: GitChangeFile[] }

/** Compact per-status letter badge (VS Code style): letter + color instead of
 *  the long porcelain word. The full word stays on hover (title) and for
 *  screen readers (aria-label). `!` marks conflicts — `C` is taken by copied. */
const STATUS_BADGE: Record<string, { letter: string; tone: string }> = {
  modified: { letter: 'M', tone: 'text-accent' },
  added: { letter: 'A', tone: 'text-ok' },
  untracked: { letter: 'U', tone: 'text-ok' },
  deleted: { letter: 'D', tone: 'text-danger' },
  renamed: { letter: 'R', tone: 'text-accent' },
  copied: { letter: 'C', tone: 'text-accent' },
  conflicted: { letter: '!', tone: 'text-danger' },
}

/** Working-tree changes for git repos under the chat's project dir — the body
 *  of the Changes panel's ever-present Local tab. The repo/file list comes
 *  from GET /api/git-changes; per-file diffs are fetched lazily on expand
 *  through the existing /api/file-diff endpoint and rendered with the same
 *  DiffView the PR file rows use. Each row carries hover-revealed actions: a
 *  chevron reflecting the inline-diff state and an open-in-editor button that
 *  opens the file as a native document tab (full file, edit + save); clicking
 *  anywhere on the row toggles the inline diff. */
export default function LocalChangesView({ projectDir, onFileOpen }: {
  projectDir?: string
  /** Open a path as a native file document tab (threaded from ChatPage). */
  onFileOpen?: (path: string) => void
}) {
  const query = useQuery({
    queryKey: ['git-changes', projectDir],
    queryFn: () => api.gitChanges(projectDir!),
    enabled: !!projectDir,
    refetchInterval: LOCAL_CHANGES_POLL_MS,
    refetchOnWindowFocus: false,
  })

  if (!projectDir) {
    return <LocalChangesEmpty>Pick a project directory for this chat to see its uncommitted git changes.</LocalChangesEmpty>
  }
  if (query.isLoading) {
    return <LocalChangesEmpty>Scanning for git repositories…</LocalChangesEmpty>
  }
  if (query.isError) {
    return <LocalChangesEmpty>{`Could not scan ${projectDir} for changes.`}</LocalChangesEmpty>
  }
  const repos = query.data?.repos ?? []
  const totalFiles = repos.reduce((n, r) => n + r.files.length, 0)

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex-1 min-h-0 overflow-y-auto scrollbar-overlay">
        {repos.length === 0 && (
          <LocalChangesEmpty>No git repository found under this project directory.</LocalChangesEmpty>
        )}
        {repos.length > 0 && totalFiles === 0 && (
          <LocalChangesEmpty>
            {`Working tree clean — no local changes${repos[0].branch ? ` on ${repos.map(r => r.branch).filter(Boolean).join(', ')}` : ''}.`}
          </LocalChangesEmpty>
        )}
        {repos.filter(repo => repo.files.length > 0).map(repo => (
          <LocalRepoSection key={repo.root} repo={repo} onFileOpen={onFileOpen} />
        ))}
      </div>
    </div>
  )
}

function LocalChangesEmpty({ children }: { children: string }) {
  return <div className="px-4 py-6 text-[12px] text-muted text-center">{children}</div>
}

function LocalRepoSection({ repo, onFileOpen }: { repo: GitChangesRepo; onFileOpen?: (path: string) => void }) {
  const [collapsed, setCollapsed] = useState(() => collapsedRepoRoots.has(repo.root))
  const toggleCollapsed = () => {
    setCollapsed(value => {
      const next = !value
      if (next) collapsedRepoRoots.add(repo.root)
      else collapsedRepoRoots.delete(repo.root)
      return next
    })
  }
  return (
    <div>
      {/* Repo header row: an inset pill (narrower than the panel, rounded,
          tinted on hover only). Files sit under it with a slight indent.
          Not sticky: a floating pill with transparent margins looks odd with
          rows scrolling through the gaps. Click collapses the repo's files. */}
      <div
        role="button"
        tabIndex={0}
        aria-expanded={!collapsed}
        onClick={toggleCollapsed}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCollapsed() } }}
        className="mx-1.5 mt-1.5 flex items-center gap-2 px-2.5 py-2 rounded-md hover:bg-bg-elevated cursor-pointer transition-colors"
      >
        {collapsed
          ? <ChevronRight size={13} className="shrink-0 text-muted" />
          : <ChevronDown size={13} className="shrink-0 text-muted" />}
        <span className="text-[13px] font-semibold text-muted-strong truncate">{repo.name}</span>
        {repo.branch && (
          <span className="flex items-center gap-1 text-[11px] text-muted shrink-0 min-w-0">
            <GitBranch size={11} className="shrink-0" />
            <span className="truncate max-w-[140px]">{repo.branch}</span>
          </span>
        )}
        <span className="ml-auto text-[11px] text-muted shrink-0">
          {repo.files.length} {repo.files.length === 1 ? 'file' : 'files'}
        </span>
      </div>
      {!collapsed && (
        <div>
          {repo.files.map(file => <LocalChangeRow key={file.path} file={file} onFileOpen={onFileOpen} />)}
        </div>
      )}
    </div>
  )
}

function LocalChangeRow({ file, onFileOpen }: { file: GitChangeFile; onFileOpen?: (path: string) => void }) {
  const [open, setOpen] = useState(false)
  // Same file-type icon map the Files tab uses (utils/fileIcons) — replaces
  // the per-row chevron; the expanded diff below the row shows open state.
  const Icon = fileIcon(file.path)
  const iconColor = colorForExt(file.path)
  // Lazy: the diff is only fetched when the row is expanded. The queryKey
  // carries a change FINGERPRINT (status/staged/counts from the 10s list
  // poll), so an open diff refetches when the underlying file changes instead
  // of drifting from the counts shown beside it. Deleted files fetch too —
  // the endpoint serves their deletion patch from HEAD.
  const diffQuery = useQuery({
    queryKey: ['file-diff', file.path, file.status, file.staged, file.additions, file.deletions],
    queryFn: () => api.fileDiff(file.path),
    enabled: open,
    staleTime: LOCAL_CHANGES_POLL_MS,
    refetchOnWindowFocus: false,
  })
  return (
    <div>
      {/* The row is a div-with-button-role (not a <button>) so the
          open-in-editor action can be its own nested real button: it opens the
          file as a native document tab, everything else toggles the inline
          diff. Same pattern as the Files tab's FileTile. Keyboard: Enter/Space
          on the row toggles; the guard on e.target keeps the nested button's
          own activation from double-firing. */}
      {/* The row looks IDENTICAL open or closed (inset rounded hover pill).
          Stickiness comes from this wrapper: full-width and opaque (bg-bg) so
          diff text can't scroll through the row or its side gutters while
          pinned. The wrapper's containing block is the row+diff div, so the
          row un-sticks exactly when its own diff scrolls past. -top-px hides
          the sub-pixel hairline that peeks above a pinned sticky element —
          the 1px clipped above the container edge is the row's own padding,
          so no margin/padding compensation is needed (and none exists to
          overlap the previous sibling's divider). */}
      <div className={open ? 'sticky -top-px z-10 bg-bg' : undefined}>
        <div
          role="button"
          tabIndex={0}
          aria-expanded={open}
          onClick={() => setOpen(value => !value)}
          onKeyDown={e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); setOpen(value => !value) } }}
          className="group flex items-center gap-2 px-3 py-2 mx-1.5 rounded-md text-left cursor-pointer hover:bg-bg-elevated transition-colors"
        >
        <Icon size={13} className={`${iconColor} shrink-0`} />
        {/* Name + path are plain text; the two ACTION icons sit right after
            them: a chevron reflecting the inline-diff state (row click
            toggles) and an explicit open-in-editor button. */}
        <FileNameWithPath rel={file.rel} title={file.path} />
        {/* Action icons reveal on row hover, or on KEYBOARD focus only
            (focus-visible): plain focus-within kept them lit after a mouse
            click expanded/collapsed the row, since the click leaves DOM focus
            on it. The chevron stays visible while expanded so open state
            remains legible. */}
        {open
          ? <ChevronDown size={13} className="shrink-0 text-muted" />
          : <ChevronRight size={13} className="shrink-0 text-muted opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 transition-opacity" />}
        {onFileOpen && (
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onFileOpen(file.path) }}
            className="shrink-0 flex items-center justify-center w-[18px] h-[18px] rounded bg-transparent border-none p-0 cursor-pointer text-muted hover:text-accent opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 focus-visible:opacity-100 transition-opacity"
            title={`Open ${file.rel} in editor`}
            aria-label={`Open ${file.rel} in editor`}
          >
            <Pen size={12} />
          </button>
        )}
        <span className="flex-1 min-w-0" />
        {/* `staged` is intentionally not surfaced — no staging/commit actions
            exist in this view yet; the counts + status letter carry the signal. */}
        {(file.additions !== undefined || file.deletions !== undefined) && (
          <span className="text-[11px] font-mono shrink-0">
            <span className="text-ok">+{file.additions ?? 0}</span> <span className="text-danger">-{file.deletions ?? 0}</span>
          </span>
        )}
        <span
          title={file.status}
          aria-label={file.status}
          className={`text-[11px] font-mono font-bold w-3 text-center shrink-0 ${STATUS_BADGE[file.status]?.tone || 'text-muted'}`}
        >
          {STATUS_BADGE[file.status]?.letter || '?'}
        </span>
        </div>
      </div>
      {open && (
        // Frame the diff with dividers on BOTH sides so the row above and the
        // next file's row below both stand apart from the diff body.
        <div className="overflow-x-auto border-t border-b border-border">
          {diffQuery.isLoading ? (
            <div className="px-3 py-3 text-[11px] text-muted">Loading diff…</div>
          ) : diffQuery.data?.diff ? (
            <DiffView patch={diffQuery.data.diff} path={file.path} />
          ) : (
            <div className="px-3 py-3 text-[12px] text-muted">No diff available for this file.</div>
          )}
        </div>
      )}
    </div>
  )
}
