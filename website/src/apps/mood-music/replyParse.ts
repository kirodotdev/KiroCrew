/**
 * Read the DJ agent's `mood-music` block out of a reply.
 *
 * The agent found its links on web pages, so the block is untrusted: each pick
 * is checked here with `links.ts` and reduced to the fields the page draws, a
 * bad pick is dropped, and lengths are cut to the backend's limits. Text is only
 * ever rendered as React text, never as HTML.
 */
import { bilibiliUrl, validVideoId, youtubeVideoId } from './links'

export type PickSource = 'youtube' | 'bilibili'

/** A pick as the page sends it to `POST /queue`. */
export interface NewPick {
  title: string
  artist: string
  why: string
  tags: string[]
  source: PickSource
  video_id?: string
  url?: string
}

export interface DjReply {
  question: string
  moodTags: string[]
  picks: NewPick[]
  /** Picks the block named that failed a check. */
  dropped: number
}

const FENCE_RE = /```mood-music[^\n]*\n([\s\S]*?)```/g
const MAX_PICKS = 12

function text(value: unknown, limit: number): string {
  return typeof value === 'string' ? value.trim().slice(0, limit) : ''
}

function tags(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .filter((t): t is string => typeof t === 'string' && t.trim().length > 0)
    .map(t => t.trim().slice(0, 40))
    .slice(0, 8)
}

function pick(raw: unknown): NewPick | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const title = text(r.title, 200)
  if (!title) return null
  const base = { title, artist: text(r.artist, 200), why: text(r.why, 400), tags: tags(r.tags) }
  if (r.source === 'youtube') {
    const id = r.video_id !== undefined ? validVideoId(r.video_id) : youtubeVideoId(r.url)
    return id ? { ...base, source: 'youtube', video_id: id } : null
  }
  if (r.source === 'bilibili') {
    const url = bilibiliUrl(r.url)
    return url ? { ...base, source: 'bilibili', url } : null
  }
  return null
}

/** The LAST `mood-music` block in *content*, or null when there is none that parses. */
export function parseDjReply(content: string): DjReply | null {
  const blocks = [...content.matchAll(FENCE_RE)]
  const last = blocks[blocks.length - 1]
  if (!last) return null
  let data: unknown
  try {
    data = JSON.parse(last[1])
  } catch {
    return null
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const d = data as Record<string, unknown>
  const rawPicks = Array.isArray(d.picks) ? d.picks.slice(0, MAX_PICKS) : []
  const picks = rawPicks.map(pick).filter((p): p is NewPick => p !== null)
  return {
    question: text(d.question, 300),
    moodTags: tags(d.mood_tags),
    picks,
    dropped: rawPicks.length - picks.length,
  }
}

/** A short stable id for a reply, so the page queues each reply once. */
export function replySource(content: string): string {
  let h = 0
  for (let i = 0; i < content.length; i++) h = ((h << 5) - h + content.charCodeAt(i)) | 0
  return `r${(h >>> 0).toString(36)}-${content.length}`
}
