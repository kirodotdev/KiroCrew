// Guide app — data access, types, and language-aware field resolution.
//
// The base path is /api/apps/guide (same convention as every builtin). The
// backend's search ranking is shared with the MCP tools, so this stays a thin
// client over one source of truth.

const API = '/api/apps/guide'

/** A string, or a platform-variant dict ({default, macos, windows, …}). */
export type Variant = string | Record<string, string>

export interface Step {
  t?: Variant
  t_zh?: string
  do?: Variant
  do_zh?: string
  cmd?: Variant
  expect?: Variant
}

export interface EntrySummary {
  id: string
  title?: string
  symptom?: string
  trust?: string
  fix?: string
}

export interface EntryDetail extends EntrySummary {
  title_zh?: string
  symptom_zh?: string
  trust_note?: string
  trust_note_zh?: string
  kind?: string
  platform?: string[] | string
  topic?: string[] | string
  steps?: Step[]
  if_stuck?: { text?: string; note?: string; note_zh?: string; next?: string | null }
  crew_prompt?: string
  keywords?: string[]
  community?: boolean
  community_body?: string
  community_body_zh?: string
  community_author?: string
  community_permalink?: string
  community_date?: string
  sources?: { label?: string; url?: string }[]
}

export interface GuideIndex {
  ids: string[]
  platforms: string[]
  topics: string[]
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'same-origin' })
  if (!res.ok) throw new Error(String(res.status))
  return (await res.json()) as T
}

export function fetchEntries(
  q: string,
  opts: { platform?: string; topic?: string; limit?: number } = {},
): Promise<{ entries: EntrySummary[] }> {
  const p = new URLSearchParams()
  if (q) p.set('q', q)
  if (opts.platform) p.set('platform', opts.platform)
  if (opts.topic) p.set('topic', opts.topic)
  p.set('limit', String(opts.limit ?? 25))
  return getJson(`${API}/entries?${p.toString()}`)
}

export function fetchEntry(id: string): Promise<EntryDetail> {
  return getJson(`${API}/entries/${encodeURIComponent(id)}`)
}

export function fetchIndex(): Promise<GuideIndex> {
  return getJson(`${API}/index`)
}

/** True when the dashboard language is Chinese (prefer `_zh` sibling fields). */
export function isZh(lang: string | undefined): boolean {
  return !!lang && lang.toLowerCase().startsWith('zh')
}

/** A string, or the resolved value of a platform-variant dict (prefer `default`). */
export function resolveVariant(value: Variant | undefined): string {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object') {
    if (typeof value.default === 'string' && value.default.trim()) return value.default
    for (const v of Object.values(value)) {
      if (typeof v === 'string' && v.trim()) return v
    }
  }
  return ''
}

/**
 * Language-aware pick: in zh, use the Chinese value when it is a non-empty
 * string, else fall back to the (English) base value. Mirrors the reference
 * renderer's `L()`, so English is never blanked where it exists. Callers pass
 * both siblings explicitly — no dynamic `_zh` key is composed at runtime.
 */
export function pickL(en: Variant | undefined, zh: unknown, lang: string): string {
  if (isZh(lang) && typeof zh === 'string' && zh) return zh
  return resolveVariant(en)
}

/**
 * A step's `do` is resolved directly (not via {@link pick}): an intentionally
 * emptied `do_zh` must render nothing, not be rescued by the English fallback.
 */
export function pickStepDo(step: Step, lang: string): string {
  if (isZh(lang) && Object.prototype.hasOwnProperty.call(step, 'do_zh')) {
    return resolveVariant(step.do_zh)
  }
  return resolveVariant(step.do)
}
