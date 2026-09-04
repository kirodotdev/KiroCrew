// Thin fetch wrapper for the Papyrus backend.
//
// The routes are registered directly on the main gateway's aiohttp Application
// (see kiro_crew/apps/builtins/papyrus/backend/routes.py:register_routes), so the
// base path is /api/apps/papyrus — matching issue-radar and code-review-sage, NOT
// the /apps/{name}/api reverse-proxy prefix used by apps that run as a separate
// child process.
//
// This is a BUILTIN dashboard page rendered inside the main React tree, so every
// request is a same-origin fetch carrying the dashboard's session cookie. The
// app-sdk hooks are deliberately not used — they require <AppApiProvider>, which
// only wraps standalone/installed apps via AppHost.

const API = '/api/apps/papyrus'

/** Endpoint path for the compiled PDF. */
const PDF_PATH = '/pdf'

/** One row of the project list. */
export interface Project {
  name: string
  /** Main-document mtime, in epoch SECONDS (Python's st_mtime). */
  modified: number
  has_pdf: boolean
}

export interface ProjectDetail {
  name: string
  main_file: string
  files: string[]
  has_pdf: boolean
}

/** One parsed compiler message. `line` is null when the log gave no line. */
export interface Diagnostic {
  level: 'error' | 'warning' | 'typesetting'
  message: string
  line: number | null
  file: string | null
}

export interface CompileResult {
  ok: boolean
  log: string
  errors: Diagnostic[]
  duration_ms: number
}

export interface GitStatus {
  is_git: boolean
  branch?: string
  dirty?: boolean
  has_remote?: boolean
  ahead?: number
  behind?: number
  changes?: string[]
  recent_commits?: string[]
  /** Set when the status probe itself failed, so the toolbar can stay quiet. */
  error?: string
}

/** Live state of the managed-compiler provisioning job. */
export interface ManagedCompilerJob {
  state: 'idle' | 'downloading' | 'verifying' | 'installing' | 'done' | 'error'
  error: string
  attempt: number
  bytes_downloaded: number
  bytes_total: number
  elapsed: number
}

/**
 * The app's own digest-pinned Tectonic install — the reason a stock machine can
 * compile at all. `supported` is false where no pinned build exists (32-bit
 * Linux, BSD, Windows-on-ARM); there the UI keeps the manual-install advice.
 */
export interface ManagedCompiler {
  supported: boolean
  installed: boolean
  /** Upstream release tag the pinned digests were computed from. */
  release: string
  version: string
  job: ManagedCompilerJob
}

export interface Health {
  status: string
  /** Basename of the compiler found on the host, or '' when there is none. */
  compiler: string
  git: boolean
  managed: ManagedCompiler
}

/** Thrown by every call below, carrying the server's own message when it sent one. */
export class PapyrusApiError extends Error {
  status: number
  /** Raw git/compiler output, when the endpoint returned any. */
  output: string

  constructor(message: string, status: number, output = '') {
    super(message)
    this.name = 'PapyrusApiError'
    this.status = status
    this.output = output
  }
}

interface ErrorBody {
  error?: string
  output?: string
}

/** Prefer the backend's own `{error}`/`reason` over a bare "HTTP 500". */
async function toError(r: Response): Promise<PapyrusApiError> {
  let body: ErrorBody | null = null
  try {
    body = (await r.json()) as ErrorBody
  } catch {
    body = null
  }
  return new PapyrusApiError(
    body?.error || r.statusText || `HTTP ${r.status}`,
    r.status,
    body?.output || '',
  )
}

async function get<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  const q = new URLSearchParams(params)
  const suffix = q.toString() ? `?${q.toString()}` : ''
  const r = await fetch(`${API}${path}${suffix}`, { credentials: 'same-origin' })
  if (!r.ok) throw await toError(r)
  return r.json() as Promise<T>
}

async function send<T>(method: string, path: string, body: unknown): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw await toError(r)
  return r.json() as Promise<T>
}

async function del<T>(path: string, params: Record<string, string>): Promise<T> {
  const q = new URLSearchParams(params)
  const r = await fetch(`${API}${path}?${q.toString()}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  })
  if (!r.ok) throw await toError(r)
  return r.json() as Promise<T>
}

/**
 * Compile is the one call that legitimately takes tens of seconds (a paper with a
 * bibliography needs four compiler passes), so it does NOT go through the shared
 * `send` error path: a non-ok response here is a FAILED COMPILE with a parseable
 * log, not a transport error, and throwing would discard exactly the output the
 * user needs to fix their document.
 */
async function compile(name: string): Promise<CompileResult> {
  const r = await fetch(`${API}/compile`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  const data = (await r.json().catch(() => null)) as CompileResult | ErrorBody | null
  if (data && 'ok' in data) return data as CompileResult
  // A transport/authorization failure with no compile payload — surface it as a
  // failed compile whose "log" is the error, so one code path renders both.
  const message = (data as ErrorBody | null)?.error || r.statusText || `HTTP ${r.status}`
  return { ok: false, log: message, errors: [], duration_ms: 0 }
}

/** URL of the compiled PDF, cache-busted so a recompile is never served stale. */
export function pdfUrl(name: string, version: number): string {
  const q = new URLSearchParams({ name, v: String(version) })
  return `${API}${PDF_PATH}?${q.toString()}`
}

export const papyrusApi = {
  health: () => get<Health>('/health'),

  /**
   * Start the managed-compiler install. Answers 202 the moment the background job
   * starts, so the caller polls `/health` for `managed.job` rather than awaiting
   * the ~10-22MB transfer on this request.
   */
  provisionCompiler: () => send<{ ok: boolean; state: string }>('POST', '/compiler/provision', {}),

  listProjects: () => get<{ projects: Project[] }>('/projects'),
  createProject: (name: string) => send<{ name: string; main_file: string }>('POST', '/projects', { name }),
  cloneProject: (url: string, name?: string) =>
    send<{ name: string; main_file: string }>('POST', '/projects/clone', { url, name: name || '' }),
  getProject: (name: string) => get<ProjectDetail>('/project', { name }),
  deleteProject: (name: string) => del<{ ok: boolean }>('/project', { name }),

  listFiles: (name: string) => get<{ files: string[] }>('/files', { name }),
  readFile: (name: string, path: string) => get<{ path: string; content: string }>('/file', { name, path }),
  saveFile: (name: string, path: string, content: string) =>
    send<{ ok: boolean; path: string }>('PUT', '/file', { name, path, content }),
  createFile: (name: string, path: string) =>
    send<{ ok: boolean; path: string }>('POST', '/file', { name, path, content: '' }),
  deleteFile: (name: string, path: string) => del<{ ok: boolean }>('/file', { name, path }),
  setMainFile: (name: string, path: string) =>
    send<{ ok: boolean; main_file: string }>('PUT', '/main', { name, path }),

  compile,

  gitStatus: (name: string) => get<GitStatus>('/git', { name }),
  gitCommit: (name: string, message: string) =>
    send<{ ok: boolean; output: string }>('POST', '/git/commit', { name, message }),
  gitPush: (name: string) => send<{ ok: boolean; output: string }>('POST', '/git/push', { name }),
  gitPull: (name: string) =>
    send<{ ok: boolean; output: string; stashed: boolean }>('POST', '/git/pull', { name }),
}
