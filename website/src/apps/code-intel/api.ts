/**
 * Code-intel's calls, over the permission-scoped `AppApi` the host hands the tab.
 *
 * Only the Language servers tab calls these: the editor shortcuts are
 * `contributes.editorBindings`, which core dispatches to the app's `binding`
 * route itself. Everything here is a `POST /api/apps/code-intel/pool` with an
 * `op`, plus the host's own directory listing for the outline's tree.
 */
import { activeLocale, type AppApi, type AppApiError } from '@kirocrew/app-sdk'
import { tr } from './t'

const POOL = '/api/apps/code-intel/pool'

export interface ServerNotice {
  level: 'error' | 'warning'
  message: string
}

export interface CodeSymbol {
  name: string
  kind: string
  detail: string
  container: string
  /** Nesting depth in the outline, 0 at the top. */
  depth: number
  /** 1-based. */
  line: number
  character: number
}

export interface CodeSymbolsResponse {
  symbols: CodeSymbol[]
  truncated: boolean
  server: string
  notices?: ServerNotice[]
  hint?: string
}

export interface LspServerEntry {
  server: string
  root: string
  /** Projects opened beside the root because its classpath references them. */
  folders: string[]
  state: 'starting' | 'ready' | 'failed' | 'exited'
  /** The start failure, empty otherwise. */
  error: string
  /** The server's last stderr output, credentials redacted. */
  stderr: string
  pid: number | null
  command: string
  /** Seconds since the server answered `initialize`; null until it has. */
  uptimeSecs: number | null
  /** Seconds since the server was spawned (or, for a failed start, since it failed). */
  ageSecs: number
  idleSecs: number | null
  /** Seconds until the idle shutdown stops it, if nothing uses it. */
  shutdownInSecs: number | null
  /** For a failed start: seconds until a request may try again. */
  retryInSecs: number | null
  /** Problems the server reported, and why it may not see the project. */
  notices: ServerNotice[]
}

export interface LspServersResponse {
  servers: LspServerEntry[]
  maxServers: number
  idleShutdownSecs: number
}

export interface LspProject {
  server: string
  root: string
  /** Files of this language under that root (within the walk's bounds). */
  files: number
  available: boolean
  command: string
  /** Executable names that would serve it, when none is installed. */
  install: string[]
  /** Installed candidates refused by the provenance check, with the reason. */
  refused: string[]
  state: 'stopped' | 'starting' | 'ready' | 'failed'
}

export interface LspDetectResponse {
  directory: string
  projects: LspProject[]
  /** The walk stopped at its bound; the list may be incomplete. */
  truncated: boolean
}

export interface WorkspaceSymbol {
  name: string
  kind: string
  container: string
  /** Empty for a library class not written out yet: open it with `openClass`. */
  path: string
  /** A library class's `jdt://` URI, when `path` is empty. */
  uri?: string
  /** In a merged search: the server and root that found it. */
  server?: string
  root?: string
  /** 1-based. */
  line: number
  character: number
}

export interface MergedSymbolsResponse {
  symbols: WorkspaceSymbol[]
  truncated: boolean
  external: number
  hidden: number
  /** How many running servers were asked. */
  servers: number
  /** Servers that failed to answer; the others' symbols are still listed. */
  errors: Array<{ server: string; root: string; error: string }>
}

export interface DirListing {
  path: string
  parent: string
  dirs: { name: string; path: string; mtime: number }[]
  files: { name: string; path: string; mtime: number }[]
}

/** File extensions the backend has a language-server family for (`servers.py`). */
const SUPPORTED_EXTS = new Set([
  '.py', '.pyi', '.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.go', '.rs',
  '.java', '.kt', '.kts', '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.rb',
])

export function codeIntelSupported(filePath: string): boolean {
  const dot = filePath.lastIndexOf('.')
  return dot >= 0 && SUPPORTED_EXTS.has(filePath.slice(dot).toLowerCase())
}

/**
 * The reader's sentence for a failed call. The backend already wrote one in the
 * dashboard's language (`error`); a 404 means the gateway never mounted this app's
 * routes; a response with no JSON body falls back to the generic failure.
 */
export function errorText(err: unknown): string {
  const failure = err as Partial<AppApiError> | null
  let parsed: { error?: unknown; code?: unknown } = {}
  if (typeof failure?.body === 'string') {
    try { parsed = JSON.parse(failure.body) ?? {} } catch { /* not JSON */ }
  }
  // The app's own 404s carry a `code`; the gateway's answer for an unmounted route does not.
  if (failure?.status === 404 && !parsed.code) return tr('backend_not_loaded')
  if (typeof parsed.error === 'string' && parsed.error) return parsed.error
  return tr('failed')
}

export function createCodeIntelApi(api: AppApi) {
  // `lang` is the dashboard's resolved UI language, for the sentences the backend
  // writes (a failed start, a refused server).
  const pool = <T,>(op: string, body: Record<string, unknown> = {}) => api.post<T>(POOL, { op, ...body, lang: activeLocale() })
  return {
    servers: () => pool<LspServersResponse>('servers'),
    detect: (path: string) => pool<LspDetectResponse>('detect', { path }),
    symbols: (path: string) => pool<CodeSymbolsResponse>('symbols', { path }),
    /** `workspace/symbol` on every running server, merged. Starts nothing. */
    workspaceSymbolsAll: (query: string) => pool<MergedSymbolsResponse>('workspace_symbols_all', { query }),
    /** Write a library class out from the server that found it; its file path. */
    openClass: (server: string, root: string, uri: string) =>
      pool<{ path: string }>('open_class', { server, root, uri }),
    start: (server: string, root: string) => pool<{ ok: boolean }>('start', { server, root }),
    restart: (server: string, root: string) => pool<{ ok: boolean }>('restart', { server, root }),
    stop: (server: string, root: string) => pool<{ ok: boolean }>('stop', { server, root }),
    browseFiles: (path: string, signal?: AbortSignal) =>
      api.get<DirListing>(`/api/browse-files?path=${encodeURIComponent(path)}`, { signal }),
  }
}

export type CodeIntelApi = ReturnType<typeof createCodeIntelApi>
