/**
 * PPTX Maker — typed API client.
 *
 * The backend runs IN the gateway, so every call is a same-origin request that
 * the dashboard's session cookie authenticates — no app-sdk provider, no proxy
 * prefix. This mirrors how the other in-gateway builtin pages talk to their
 * routes (see `apps/issue-radar/api.ts`).
 *
 * Deck artifact URLs come back from the server as `preview/...` paths relative to
 * this base, never as filesystem paths, so `artifactUrl()` is the only place they
 * are turned into something fetchable.
 */

export const API_BASE = '/api/apps/pptx-maker'

// ── wire types ──────────────────────────────────────────────────────────────

export interface DeckSummary {
  deckId: string
  name: string
  slideCount: number
  thumbnailUrl: string | null
  pptxUrl: string | null
  brief: string
}

export interface SlideRef {
  slug: string
  previewUrl: string | null
  composeUrl: string | null
}

/** Which deliverable tabs a deck currently has. Keys appear as they are written. */
export interface DeckSpecs {
  brief?: string
  outline?: string
  artDirection?: string
}

export interface DeckDetail {
  deckId: string
  name: string
  defsUrl: string | null
  pptxUrl: string | null
  dirPath: string
  pptxPath: string | null
  specs: DeckSpecs
  /** Per-deliverable mtime — diffed across polls to follow the newest change. */
  updatedAt: Record<string, number>
  slides: SlideRef[]
}

export interface EngineStatus {
  ready: boolean
  clone: boolean
  venv: boolean
  pinnedTag: string
  provision: { state: 'idle' | 'running' | 'done' | 'error'; log: string; elapsed: number }
}

export interface DepsStatus {
  labels: Record<string, string>
  present: Record<string, boolean>
  /** True for a tool KiroCrew installed itself, rather than one found on PATH. */
  managed: Record<string, boolean>
  missing: string[]
  /**
   * Per-tool install command for the ones the app deliberately cannot install
   * (LibreOffice — a system package). Display only; the user runs it.
   */
  hints: Record<string, string>
}

export interface AssetsStatus {
  sources: string[]
  provisioned: Record<string, boolean>
  ready: boolean
  tag: string
  state: 'idle' | 'running' | 'done' | 'error'
  log: string
  elapsed: number
  perSource: Record<string, string>
}

export interface StyleEntry {
  name: string
  source?: string
  pinned?: boolean
  /** Standalone HTML of the style's first slide, for the list thumbnail. */
  coverHtml?: string
}

export interface TemplateEntry {
  name: string
  source?: string
  description?: string
  layout_count?: number
  theme_colors?: Record<string, string>
  fonts?: { halfwidth?: string; fullwidth?: string }
}

/** One slide's render payload, as the engine's compose step writes it. */
export interface ComposePayload {
  version: number
  viewBox: string
  bgSvg?: string
  bgFill?: string
  components: ComposeComponent[]
}

export interface ComposeComponent {
  svg: string
  class?: string
  text?: string
  changed?: boolean
  bbox?: { x: number; y: number; w: number; h: number }
}

export interface ComposeDefs {
  defs?: string
}

// ── transport ───────────────────────────────────────────────────────────────

class PptxMakerError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'PptxMakerError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { credentials: 'same-origin', ...init })
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const body = await res.json()
      if (body && typeof body.error === 'string') message = body.error
    } catch {
      // Non-JSON error body — the status line is all we have.
    }
    throw new PptxMakerError(message, res.status)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

/**
 * Turn a server-returned relative artifact path into a fetchable URL.
 *
 * The server never hands out absolute paths, so this is the single place a deck
 * artifact reference becomes a request.
 */
export function artifactUrl(relative: string): string {
  return `${API_BASE}/${relative.replace(/^\/+/, '')}`
}

/** Fetch a deck artifact as text (a spec document or a style board). */
export async function fetchArtifactText(relative: string): Promise<string> {
  const res = await fetch(artifactUrl(relative), { credentials: 'same-origin' })
  if (!res.ok) throw new PptxMakerError(`HTTP ${res.status}`, res.status)
  return res.text()
}

/** Fetch a deck artifact as JSON (a compose payload or the shared defs). */
export async function fetchArtifactJson<T>(relative: string): Promise<T> {
  const res = await fetch(artifactUrl(relative), { credentials: 'same-origin' })
  if (!res.ok) throw new PptxMakerError(`HTTP ${res.status}`, res.status)
  return (await res.json()) as T
}

export const pptxMakerApi = {
  engine: () => request<EngineStatus>('/engine'),
  provisionEngine: () => request<{ state: string }>('/engine/provision', { method: 'POST' }),
  deps: () => request<DepsStatus>('/deps'),
  assets: () => request<AssetsStatus>('/assets'),
  provisionAssets: (force = false) =>
    request<{ state: string }>(`/assets/provision${force ? '?force=true' : ''}`, {
      method: 'POST',
    }),

  config: () => request<{ deckRoot: string; default: string }>('/config'),
  setDeckRoot: (deckRoot: string) =>
    request<{ saved: boolean; deckRoot: string }>('/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ deckRoot }),
    }),

  decks: () => request<{ decks: DeckSummary[] }>('/decks'),
  deck: (deckId: string) => request<DeckDetail>(`/deck?id=${encodeURIComponent(deckId)}`),

  styles: () => request<{ styles: StyleEntry[] }>('/styles'),
  style: (name: string) =>
    request<{ name: string; fullHtml: string }>(`/style?name=${encodeURIComponent(name)}`),
  importStyle: (name: string, html: string | Blob) =>
    request<{ imported: string }>(`/styles/import?name=${encodeURIComponent(name)}`, {
      method: 'POST',
      body: html,
    }),
  renameStyle: (name: string, to: string) =>
    postJson<{ renamed: { from: string; to: string } }>('/styles/rename', { name, to }),
  pinStyle: (name: string, pinned: boolean) =>
    postJson<{ pinnedStyles: string[] }>('/styles/pin', { name, pinned }),
  deleteStyle: (name: string) =>
    request<{ deleted: string }>(`/styles?name=${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),

  templates: () => request<{ templates: TemplateEntry[] }>('/templates'),
  importTemplate: (name: string, file: Blob, description = '') =>
    request<{ imported: string; metadata: Record<string, unknown> }>(
      `/templates/import?name=${encodeURIComponent(name)}&description=${encodeURIComponent(description)}`,
      { method: 'POST', body: file },
    ),
  renameTemplate: (name: string, to: string) =>
    postJson<{ renamed: { from: string; to: string } }>('/templates/rename', { name, to }),
  deleteTemplate: (name: string) =>
    request<{ deleted: string }>(`/templates?name=${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
}

export { PptxMakerError }
