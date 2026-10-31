/**
 * Code-intel's side-panel tab (`contributes.panelTabs[lsp]`): the gateway's
 * language-server pool (state, timing, last error and stderr, restart / stop),
 * the languages found under the chat's project directory, a workspace-symbol
 * search across every running server, a file tree per running server's project
 * root whose source files expand into their outline, and the app's shortcuts.
 *
 * Built by `website/vite.app-bundles.config.ts` into the Python package and
 * mounted by the host's `AppHost`, so it reaches the dashboard only through
 * `@kirocrew/app-sdk`: the scoped API for its own routes and the host's file
 * listing, and `useSidePanelHost()` to open a file in the panel beside it.
 */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, File, Folder, RefreshCw, RotateCw, Square } from 'lucide-react'
import { useAppApi, useAppInfo, useLanguageGeneration, useSidePanelHost } from '@kirocrew/app-sdk'
import { ErrorNotice, SearchInput, SimpleSelect } from '@kirocrew/app-sdk/ui'
import {
  codeIntelSupported,
  createCodeIntelApi,
  errorText,
  type CodeIntelApi,
  type LspProject,
  type LspServerEntry,
  type WorkspaceSymbol,
} from './api'
import { plural, tr } from './t'

/** How often the pool status refreshes while the tab is on screen. */
const SERVERS_POLL_MS = 5_000
/** Typing pause before a workspace-symbol search is sent. */
const SEARCH_DEBOUNCE_MS = 300

type OpenFile = (path: string, opts?: { line?: number }) => void

function pathBasename(path: string): string {
  const trimmed = path.replace(/\/+$/, '')
  const i = trimmed.lastIndexOf('/')
  return i >= 0 ? trimmed.slice(i + 1) : trimmed
}

/**
 * A project root as the tab shows it: relative to the chat's project directory
 * when it is inside it (so worktree copies of the same package read apart),
 * otherwise the full path.
 */
export function rootLabel(root: string, projectDir?: string): string {
  if (!projectDir) return root
  const base = projectDir.replace(/\/+$/, '')
  if (root === base) return pathBasename(root)
  return root.startsWith(base + '/') ? root.slice(base.length + 1) : root
}

/** A detected project's identity, as the search's project picker values it. */
const serverKey = (e: { server: string; root: string }): string => [e.server, e.root].join('\u0000')

function duration(secs: number | null): string {
  if (secs === null) return ''
  if (secs < 60) return tr('panel.seconds', { n: secs })
  const minutes = Math.round(secs / 60)
  if (minutes < 60) return tr('panel.minutes', { n: minutes })
  return tr('panel.hours', { n: Math.round(minutes / 60) })
}

function stateLabel(state: LspServerEntry['state'] | LspProject['state']): string {
  switch (state) {
    case 'ready': return tr('panel.state_ready')
    case 'starting': return tr('panel.state_starting')
    case 'failed': return tr('panel.state_failed')
    case 'exited': return tr('panel.state_exited')
    case 'stopped': return tr('panel.state_stopped')
  }
}

function stateTone(state: string): string {
  if (state === 'ready') return 'text-ok'
  if (state === 'failed' || state === 'exited') return 'text-danger'
  if (state === 'starting') return 'text-warn'
  return 'text-muted'
}

const ICON_BUTTON = 'flex items-center justify-center w-[24px] h-[24px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none disabled:opacity-40 disabled:cursor-default'

/** The app's client and the panel it sits in, for every row below the root. */
const PanelContext = createContext<{ api: CodeIntelApi; active: boolean; openFile?: OpenFile }>({
  api: null as unknown as CodeIntelApi,
  active: true,
})

/** The pool and detection answers; the only ones a pool operation changes. */
const POOL_KEYS = [['code-intel', 'servers'], ['code-intel', 'detect']] as const
function invalidatePool(queryClient: ReturnType<typeof useQueryClient>) {
  for (const queryKey of POOL_KEYS) void queryClient.invalidateQueries({ queryKey })
}

function Section({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="border-b border-border">
      <div className="flex items-center gap-2 px-3 h-[32px]">
        <h3 className="text-[11px] font-semibold uppercase tracking-wide text-muted m-0 flex-1">{title}</h3>
        {action}
      </div>
      <div className="pb-2">{children}</div>
    </section>
  )
}

/** One running (or failed) server: state, timings, and its restart / stop controls. */
function ServerRow({ entry, projectDir }: { entry: LspServerEntry; projectDir?: string }) {
  const { api } = useContext(PanelContext)
  const queryClient = useQueryClient()
  const act = useMutation({
    mutationFn: (op: 'restart' | 'stop') =>
      op === 'restart' ? api.restart(entry.server, entry.root) : api.stop(entry.server, entry.root),
    onSettled: () => invalidatePool(queryClient),
  })
  const timing = entry.state === 'ready'
    ? tr('panel.ready_timing', { uptime: duration(entry.uptimeSecs), stops: duration(entry.shutdownInSecs) })
    : entry.state === 'failed' && entry.retryInSecs !== null
      ? tr('panel.retry_in', { time: duration(entry.retryInSecs) })
      : duration(entry.ageSecs)
  const hasDetail = !!(entry.error || entry.stderr || entry.command)
  return (
    <li className="px-3 py-1.5 list-none">
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-[11px] font-mono px-1.5 rounded bg-bg-hover shrink-0">{entry.server}</span>
        <span className="text-[12px] font-medium truncate flex-1" title={entry.root}>{pathBasename(entry.root)}</span>
        <span className={`text-[11px] shrink-0 ${stateTone(entry.state)}`}>{stateLabel(entry.state)}</span>
        <button
          type="button"
          className={ICON_BUTTON}
          disabled={act.isPending}
          onClick={() => act.mutate('restart')}
          title={tr('panel.restart')}
          aria-label={tr('panel.restart_server', { server: entry.server, root: entry.root })}
        ><RotateCw size={13} /></button>
        <button
          type="button"
          className={ICON_BUTTON}
          disabled={act.isPending}
          onClick={() => act.mutate('stop')}
          title={tr('panel.stop')}
          aria-label={tr('panel.stop_server', { server: entry.server, root: entry.root })}
        ><Square size={12} /></button>
      </div>
      <div className="text-[11px] text-muted pl-1 mt-0.5 break-all">{rootLabel(entry.root, projectDir)}</div>
      {entry.folders.length > 0 && (
        <div className="text-[11px] text-muted pl-1 break-all" title={entry.folders.join('\n')}>
          {tr('panel.with_projects', { names: entry.folders.map(pathBasename).join(', ') })}
        </div>
      )}
      <div className="text-[11px] text-muted pl-1">
        {entry.pid !== null ? tr('panel.with_pid', { timing, pid: entry.pid }) : timing}
      </div>
      {act.isError && <div className="text-[11px] text-danger pl-1">{errorText(act.error)}</div>}
      {entry.notices.length > 0 && (
        <ul className="m-0 mt-1 pl-5 text-[11px]">
          {entry.notices.map((n, i) => (
            <li key={i} className={`break-words ${n.level === 'error' ? 'text-danger' : 'text-warn-text'}`}>{n.message}</li>
          ))}
        </ul>
      )}
      {hasDetail && (
        <details className="mt-1 pl-1">
          <summary className="text-[11px] text-muted cursor-pointer">{tr('panel.details')}</summary>
          {entry.command && <div className="text-[11px] font-mono break-all mt-1">{entry.command}</div>}
          <div className="text-[11px] font-mono break-all text-muted">{entry.root}</div>
          {entry.error && <div className="text-[11px] text-danger mt-1 break-words">{entry.error}</div>}
          {entry.stderr && (
            <pre className="text-[10.5px] font-mono whitespace-pre-wrap break-all bg-bg-hover rounded p-2 mt-1 max-h-[240px] overflow-auto">{entry.stderr}</pre>
          )}
        </details>
      )}
    </li>
  )
}

/** A language found in the project: where its server would run, and why it is not running. */
function ProjectRow({ project, projectDir }: { project: LspProject; projectDir?: string }) {
  const { api } = useContext(PanelContext)
  const queryClient = useQueryClient()
  const start = useMutation({
    mutationFn: () => api.start(project.server, project.root),
    onSettled: () => invalidatePool(queryClient),
  })
  let note: string
  if (project.refused.length > 0) note = tr('panel.refused', { reason: project.refused[0] })
  else if (!project.available) note = tr('panel.not_installed', { commands: project.install.join(', ') })
  else note = tr('panel.files', { n: project.files })
  return (
    <li className="px-3 py-1.5 list-none">
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-[11px] font-mono px-1.5 rounded bg-bg-hover shrink-0">{project.server}</span>
        <span className="text-[12px] font-medium truncate flex-1" title={project.root}>{pathBasename(project.root)}</span>
        <span className={`text-[11px] shrink-0 ${project.available ? stateTone(project.state) : 'text-warn'}`}>
          {project.available ? stateLabel(project.state) : tr('panel.missing')}
        </span>
        {project.available && project.state === 'stopped' && (
          <button
            type="button"
            className="text-[11px] px-2 h-[22px] rounded-md border border-border bg-transparent hover:bg-bg-hover cursor-pointer disabled:opacity-40"
            disabled={start.isPending}
            onClick={() => start.mutate()}
            aria-label={tr('panel.start_server', { server: project.server, root: project.root })}
          >{tr('panel.start')}</button>
        )}
      </div>
      <div className="text-[11px] text-muted pl-1 mt-0.5 break-all">{rootLabel(project.root, projectDir)}</div>
      <div className={`text-[11px] pl-1 break-words ${project.available ? 'text-muted' : 'text-warn'}`}>{note}</div>
      {start.isError && <div className="text-[11px] text-danger pl-1">{errorText(start.error)}</div>}
    </li>
  )
}

/** Workspace-symbol search over every running server. */
function SymbolSearch() {
  const { api, active, openFile } = useContext(PanelContext)
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  useEffect(() => {
    const id = setTimeout(() => setDebounced(query.trim()), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(id)
  }, [query])
  const search = useQuery({
    queryKey: ['code-intel', 'workspace-symbols-all', debounced],
    queryFn: () => api.workspaceSymbolsAll(debounced),
    enabled: active && debounced.length > 0,
    retry: false,
    staleTime: 10_000,
  })
  // A library class is written out only when opened.
  const openClass = useMutation({
    mutationFn: async (sym: WorkspaceSymbol) => (await api.openClass(sym.server!, sym.root!, sym.uri!)).path,
    onSuccess: (path, sym) => openFile?.(path, { line: sym.line }),
  })
  const open = (sym: WorkspaceSymbol) => {
    if (sym.path) openFile?.(sym.path, { line: sym.line })
    else if (sym.uri && sym.server && sym.root) openClass.mutate(sym)
  }
  const answer = search.data
  return (
    <div className="px-3">
      <SearchInput
        type="search"
        className="w-full"
        value={query}
        onChange={e => setQuery(e.target.value)}
        placeholder={tr('panel.search_placeholder')}
        aria-label={tr('panel.search_placeholder')}
      />
      {search.isFetching && <div className="text-[11px] text-muted mt-1">{tr('panel.searching')}</div>}
      {search.isError && <div className="text-[11px] text-danger mt-1">{errorText(search.error)}</div>}
      {openClass.isError && <div className="text-[11px] text-danger mt-1">{errorText(openClass.error)}</div>}
      {answer && !search.isFetching && (
        <>
          {answer.servers === 0 && <div className="text-[11px] text-muted mt-1">{tr('panel.search_none_running')}</div>}
          {answer.servers > 0 && answer.symbols.length === 0 && <div className="text-[11px] text-muted mt-1">{tr('panel.no_symbols')}</div>}
          <ul className="m-0 p-0 mt-1 max-h-[320px] overflow-auto">
            {answer.symbols.map((sym, i) => {
              const where = sym.path
                ? `${pathBasename(sym.path)}:${sym.line}`
                : tr('panel.library')
              return (
                <li key={`${i}:${sym.path || sym.uri}:${sym.line}`} className="list-none">
                  <button
                    type="button"
                    className="w-full text-left flex items-center gap-2 px-1 py-0.5 rounded hover:bg-bg-hover bg-transparent border-none cursor-pointer min-w-0"
                    onClick={() => open(sym)}
                    title={sym.path ? `${sym.path}:${sym.line}` : sym.container}
                  >
                    <span className="text-[10px] text-muted w-[64px] shrink-0 truncate">{sym.kind}</span>
                    <span className="text-[12px] truncate">{sym.name}</span>
                    <span className="text-[11px] text-muted truncate flex-1">{sym.container || pathBasename(sym.path)}</span>
                    {answer.servers > 1 && sym.server && <span className="text-[10px] text-muted shrink-0">{sym.server}</span>}
                    <span className="text-[11px] text-muted shrink-0">{where}</span>
                  </button>
                </li>
              )
            })}
          </ul>
          {answer.truncated && <div className="text-[11px] text-muted">{tr('panel.truncated')}</div>}
          {answer.external > 0 && <div className="text-[11px] text-muted">{plural('external', answer.external)}</div>}
          {answer.errors.map(e => (
            <div key={`${e.server}:${e.root}`} className="text-[11px] text-danger break-words">
              {tr('panel.search_server_failed', { server: e.server, path: pathBasename(e.root), error: e.error })}
            </div>
          ))}
        </>
      )}
    </div>
  )
}

/** A source file in the tree: expands into the server's outline of it. */
function FileOutline({ path, depth }: { path: string; depth: number }) {
  const { api, active, openFile } = useContext(PanelContext)
  const [open, setOpen] = useState(false)
  const supported = codeIntelSupported(path)
  const outline = useQuery({
    queryKey: ['code-intel', 'outline', path],
    queryFn: () => api.symbols(path),
    enabled: active && open && supported,
    retry: false,
    staleTime: 30_000,
  })
  const pad = { paddingLeft: 8 + depth * 12 }
  return (
    <li className="list-none">
      <div className="flex items-center gap-1 h-[22px] hover:bg-bg-hover rounded min-w-0" style={pad}>
        {supported
          ? (
            <button
              type="button"
              className="w-[16px] h-[16px] flex items-center justify-center bg-transparent border-none p-0 cursor-pointer text-muted"
              onClick={() => setOpen(v => !v)}
              aria-expanded={open}
              aria-label={tr(open ? 'panel.hide_outline' : 'panel.show_outline', { name: pathBasename(path) })}
            >{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</button>
          )
          : <span className="w-[16px] shrink-0" />}
        <File size={13} className="text-muted shrink-0" />
        <button
          type="button"
          className="text-[12px] truncate text-left bg-transparent border-none p-0 cursor-pointer flex-1 min-w-0"
          onClick={() => openFile?.(path)}
          title={path}
        >{pathBasename(path)}</button>
      </div>
      {open && (
        <div style={{ paddingLeft: 8 + (depth + 1) * 12 + 16 }}>
          {outline.isFetching && <div className="text-[11px] text-muted">{tr('panel.loading')}</div>}
          {outline.isError && <div className="text-[11px] text-danger break-words">{errorText(outline.error)}</div>}
          {outline.data && outline.data.symbols.length === 0 && <div className="text-[11px] text-muted">{tr('panel.no_symbols')}</div>}
          {outline.data && (
            <ul className="m-0 p-0">
              {outline.data.symbols.map((sym, i) => (
                <li key={`${i}:${sym.line}:${sym.name}`} className="list-none">
                  <button
                    type="button"
                    className="w-full text-left flex items-center gap-2 h-[20px] rounded hover:bg-bg-hover bg-transparent border-none cursor-pointer min-w-0"
                    style={{ paddingLeft: sym.depth * 12 }}
                    onClick={() => openFile?.(path, { line: sym.line })}
                    title={`${sym.name} · ${sym.line}`}
                  >
                    <span className="text-[10px] text-muted w-[56px] shrink-0 truncate">{sym.kind}</span>
                    <span className="text-[12px] truncate">{sym.name}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  )
}

/** A directory in the tree, listed on first expand. */
function DirNode({ path, name, depth, initiallyOpen }: {
  path: string; name: string; depth: number; initiallyOpen?: boolean
}) {
  const { api, active } = useContext(PanelContext)
  const [open, setOpen] = useState(!!initiallyOpen)
  const listing = useQuery({
    queryKey: ['code-intel', 'browse-files', path],
    queryFn: ({ signal }) => api.browseFiles(path, signal),
    enabled: active && open,
    retry: false,
    staleTime: 5_000,
  })
  const pad = { paddingLeft: 8 + depth * 12 }
  return (
    <li className="list-none">
      <button
        type="button"
        className="w-full flex items-center gap-1 h-[22px] hover:bg-bg-hover rounded bg-transparent border-none cursor-pointer text-left min-w-0"
        style={pad}
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        title={path}
      >
        <span className="w-[16px] flex items-center justify-center text-muted">{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
        <Folder size={13} className="text-muted shrink-0" />
        <span className="text-[12px] truncate">{name}</span>
      </button>
      {open && (
        <ul className="m-0 p-0">
          {listing.isLoading && <li className="list-none text-[11px] text-muted" style={{ paddingLeft: pad.paddingLeft + 28 }}>{tr('panel.loading')}</li>}
          {listing.isError && (
            <li className="list-none text-[11px] text-danger break-words" style={{ paddingLeft: pad.paddingLeft + 28 }}>
              {tr('panel.list_failed')}
              {listing.error instanceof Error && listing.error.message ? ` ${listing.error.message}` : ''}
            </li>
          )}
          {listing.data?.dirs.filter(d => !d.name.startsWith('.')).map(d => (
            <DirNode key={d.path} path={d.path} name={d.name} depth={depth + 1} />
          ))}
          {listing.data?.files.map(f => (
            <FileOutline key={f.path} path={f.path} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  )
}

/** Where the app's chords live: Settings shows the ones in force, since the reader may have changed them. */
function Shortcuts() {
  return <div className="px-3 text-[11px] text-muted">{tr('panel.shortcuts_in_settings')}</div>
}

export default function CodeIntelPanel() {
  useLanguageGeneration()
  const sdk = useAppApi()
  const api = useMemo(() => createCodeIntelApi(sdk), [sdk])
  const info = useAppInfo()
  const active = info.active !== false
  const host = useSidePanelHost()
  const projectDir = host?.projectDir
  const openFile = host?.openFile
  const queryClient = useQueryClient()
  const servers = useQuery({
    queryKey: ['code-intel', 'servers'],
    queryFn: () => api.servers(),
    enabled: active,
    refetchInterval: active ? SERVERS_POLL_MS : false,
    retry: false,
  })
  const detect = useQuery({
    queryKey: ['code-intel', 'detect', projectDir],
    queryFn: () => api.detect(projectDir!),
    enabled: active && !!projectDir,
    retry: false,
    staleTime: 60_000,
  })
  const refresh = () => invalidatePool(queryClient)
  const pool = servers.data?.servers ?? []
  const projects = detect.data?.projects ?? []
  // Symbol search asks every running server; the outline shows one, chosen
  // here: its project root, and the projects it opened beside it.
  const live = pool.filter(e => e.state === 'ready' || e.state === 'starting')
  // Failed and exited servers, listed with the detected languages not running.
  const inactive = pool.filter(e => e.state !== 'ready' && e.state !== 'starting')
  const [chosen, setChosen] = useState('')
  const target = live.find(e => serverKey(e) === chosen) ?? live[0]
  const listed = new Set(inactive.map(serverKey))
  const notRunning = projects.filter(p => (!p.available || p.state === 'stopped' || p.state === 'failed') && !listed.has(serverKey(p)))
  const ctx = useMemo(() => ({ api, active, openFile }), [api, active, openFile])
  return (
    <PanelContext.Provider value={ctx}>
    <div className="h-full overflow-auto text-text" data-testid="code-intel-panel">
      <Section title={tr('panel.symbol_search')}>
        <SymbolSearch />
      </Section>
      <Section title={tr('panel.outline')}>
        {servers.data && !target && (
          <div className="px-3 text-[12px] text-muted">{tr('panel.outline_empty')}</div>
        )}
        {target && live.length > 1 && (
          <div className="px-3 pb-1">
            <SimpleSelect
              className="w-full"
              options={live.map(serverKey)}
              optionLabels={live.map(e => tr('panel.project_option', { server: e.server, path: rootLabel(e.root, projectDir) }))}
              value={serverKey(target)}
              onChange={setChosen}
              aria-label={tr('panel.active_server')}
            />
          </div>
        )}
        {target && (
          <ul className="m-0 p-0">
            {[target.root, ...target.folders].map((root, i) => (
              <DirNode
                key={`${serverKey(target)}:${root}`}
                path={root}
                name={rootLabel(root, projectDir)}
                depth={0}
                initiallyOpen={i === 0}
              />
            ))}
          </ul>
        )}
      </Section>
      <Section
        title={tr('panel.servers_running')}
        action={(
          <button type="button" className={ICON_BUTTON} onClick={refresh} title={tr('panel.refresh')} aria-label={tr('panel.refresh')}>
            <RefreshCw size={13} className={servers.isFetching || detect.isFetching ? 'animate-spin motion-reduce:animate-none' : ''} />
          </button>
        )}
      >
        {servers.isError && <ErrorNotice className="mx-3" message={errorText(servers.error)} />}
        {servers.data && live.length === 0 && <div className="px-3 text-[12px] text-muted">{tr('panel.none_running')}</div>}
        <ul className="m-0 p-0">{live.map(entry => <ServerRow key={serverKey(entry)} entry={entry} projectDir={projectDir} />)}</ul>
        {servers.data && (
          <div className="px-3 text-[11px] text-muted">
            {tr('panel.pool_limits', { max: servers.data.maxServers, idle: duration(servers.data.idleShutdownSecs) })}
          </div>
        )}
      </Section>
      <Section title={tr('panel.servers_other')}>
        <ul className="m-0 p-0">{inactive.map(entry => <ServerRow key={serverKey(entry)} entry={entry} projectDir={projectDir} />)}</ul>
        {!projectDir && <div className="px-3 text-[12px] text-muted">{tr('panel.no_project')}</div>}
        {projectDir && (
          <>
            {detect.isLoading && <div className="px-3 text-[12px] text-muted">{tr('panel.scanning')}</div>}
            {detect.isError && <ErrorNotice className="mx-3" message={errorText(detect.error)} />}
            {detect.data && projects.length === 0 && inactive.length === 0 && <div className="px-3 text-[12px] text-muted">{tr('panel.no_languages')}</div>}
            {detect.data && projects.length > 0 && notRunning.length === 0 && inactive.length === 0 && (
              <div className="px-3 text-[12px] text-muted">{tr('panel.all_running')}</div>
            )}
            <ul className="m-0 p-0">{notRunning.map(p => <ProjectRow key={`${p.server}:${p.root}`} project={p} projectDir={projectDir} />)}</ul>
            {detect.data?.truncated && <div className="px-3 text-[11px] text-muted">{tr('panel.scan_truncated')}</div>}
          </>
        )}
      </Section>
      <Section title={tr('panel.shortcuts')}>
        <Shortcuts />
      </Section>
    </div>
    </PanelContext.Provider>
  )
}
