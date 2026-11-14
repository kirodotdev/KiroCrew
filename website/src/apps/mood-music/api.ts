/**
 * Mood Music clients: the app's own storage routes, and the one chat slot the
 * page uses to talk to its DJ agent.
 *
 * The slot is created under the app's own mode (`seams.py`), so it stays off the
 * chat sidebar, and with `memory_mode: 'temporary'`, so feelings typed here are
 * not written into Kiro Crew memory: taste lives in the app's storage and is sent
 * with each request instead (`request.prompt.ts`). The slot is reopened before every
 * send (`ask`), because a send to a missing slot would recreate it as a plain chat.
 */
import { toApiError } from '../../api/apiError'
import { sendTurn } from '../../chat-core/transport/sendTurn'
import { i18nT } from '../../i18n/t'
import type { NewPick, PickSource } from './replyParse'

const BASE = '/api/apps/mood-music'
export const DJ_AGENT = 'mood-music-dj'
export const SLOT_NAME = 'mood-music'
const SLOT_MODE = 'mood-music'
const MEMORY_MODE = 'temporary'

export type Verdict = 'liked' | 'skipped' | ''

export interface Moment {
  id: string
  mood: string
  note: string
  tags: string[]
  songs: { title: string; artist: string }[]
  at: number
}

export interface QueuedPick {
  id: string
  title: string
  artist: string
  why: string
  tags: string[]
  source: PickSource
  video_id?: string
  url?: string
  feedback: Verdict
}

export interface FeedbackEntry {
  pick_id: string
  title: string
  artist: string
  tags: string[]
  verdict: Exclude<Verdict, ''>
  at: number
}

export interface MoodState {
  moments: Moment[]
  queue: QueuedPick[]
  feedback: FeedbackEntry[]
  last_source: string
}

export interface SlotMessage {
  role?: string
  type?: string
  content?: string
}

export interface SlotData {
  running?: boolean
  messages?: SlotMessage[]
}

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init })
  if (!r.ok) throw await toApiError(r)
  if (r.status === 204) return undefined as T
  return (await r.json()) as T
}

const send = <T>(method: string, path: string, body: unknown): Promise<T> =>
  jsonFetch<T>(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })

const openSlot = () =>
  send<{ key: string }>('POST', '/api/chat/slots', { name: SLOT_NAME, agent: DJ_AGENT, memory_mode: MEMORY_MODE, mode: SLOT_MODE })

export const moodApi = {
  state: () => jsonFetch<MoodState>(`${BASE}/state`),
  addMoment: (mood: string) => send<Moment>('POST', `${BASE}/moments`, { mood }),
  setQueue: (body: { source: string; moment_id?: string; mood_tags: string[]; picks: NewPick[] }) =>
    send<{ queue: QueuedPick[] }>('POST', `${BASE}/queue`, body),
  feedback: (pickId: string, verdict: Verdict) =>
    send<QueuedPick>('PUT', `${BASE}/queue/${encodeURIComponent(pickId)}/feedback`, { verdict }),

  openSlot,
  getSlot: (key: string) => jsonFetch<SlotData>(`/api/chat/slots/${encodeURIComponent(key)}`),
  /**
   * Send one turn; a refusal rejects, an unknown outcome resolves (the poll
   * settles it). The slot is (re)opened first under its own mode and memory
   * mode: it lives in memory only, and the send endpoint would recreate a
   * missing one as a plain persistent chat.
   */
  ask: async (message: string): Promise<string> => {
    const { key } = await openSlot()
    const receipt = await sendTurn({ message, slot: key })
    if (receipt.status === 'refused') throw new Error(receipt.reason || (i18nT('pages.chatPage.send_failed') as string))
    return key
  },
}

/** The newest assistant reply in a slot, or ''. */
export function latestReply(slot: SlotData | undefined): string {
  const msgs = slot?.messages ?? []
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    if (m.role === 'assistant' && typeof m.content === 'string' && m.content.trim()) return m.content
  }
  return ''
}
