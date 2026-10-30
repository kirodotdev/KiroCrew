/**
 * A turn's browsing as something to replay: frames on a clock, and the actions
 * the agent (or the person) took on that clock.
 *
 * Two sources, one shape (`Recording`):
 *   - the gateway's recording (`GET /api/browser/live/{session}/recordings/{turn}`),
 *     whose frames are JPEG URLs and whose events carry the op, the point and the
 *     drag path in frame pixels;
 *   - when the gateway keeps none (the route answers 404, or the read fails), the
 *     frames the card already holds in memory (`FrameRing`) and the step and URL
 *     changes the live store logged as they arrived.
 *
 * The wire is read loosely, field by field: an unknown field is ignored, a
 * missing one gets a default, and an event or frame that cannot be placed on the
 * clock is dropped rather than drawn in the wrong place. Pure functions here
 * (`readRecording`, `frameAt`, `activeEvent`, …) carry the player's arithmetic
 * so the tests can pin it without a DOM.
 */

import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { browserLiveFrameUrl } from '../api/client/browserLive'
import type { BrowserLiveTurn } from './useBrowserLive'
import type { DecodedBrowserFrame } from './useBrowserLive.frame'

export type ReplayOp = 'click' | 'type' | 'key' | 'drag' | 'scroll' | 'navigate' | 'person' | 'other'
const OPS: ReadonlySet<string> = new Set(['click', 'type', 'key', 'drag', 'scroll', 'navigate', 'person', 'other'])

/**
 * A quick ask's step (the gateway sends `actor: 'person'` with an `ask`
 * object; the card files it under its own op `person`): which kind, what
 * happened and how it ended, and the origin it was for. The gateway never
 * records the values; the card draws only these words, never the event's
 * own label or args.
 */
export interface ReplayPerson { kind: string; action: string; outcome: string; origin: string }

export interface ReplayPoint { x: number; y: number }

export interface ReplayEvent {
  id: string
  /** Epoch ms. */
  ts: number
  op: ReplayOp
  label: string
  args: Record<string, unknown>
  /** Frame pixels in the recording's `width` x `height` space, or null. */
  point: ReplayPoint | null
  path: ReplayPoint[] | null
  url: string
  status: 'ok' | 'error'
  actor: 'agent' | 'user'
  /** The frame size `point` and `path` are in, when the gateway stamped it (it can differ from the frame shown). */
  fw?: number
  fh?: number
  /** A move a loop op (`reflex`) made inside itself; the step list folds a run of them. */
  loop: boolean
  /** Set for `op: 'person'`. */
  person?: ReplayPerson
}

export interface ReplayFrame {
  i: number
  ts: number
  w: number
  h: number
  /** A URL the gateway serves, or a frame this page holds in memory. */
  src: string | DecodedBrowserFrame
}

export interface Recording {
  turnId: string
  startedTs: number
  endedTs: number | null
  live: boolean
  width: number
  height: number
  frames: ReplayFrame[]
  events: ReplayEvent[]
  source: 'server' | 'memory'
}

export interface RecordingSummary {
  turnId: string
  startedTs: number
  endedTs: number | null
  live: boolean
  frameCount: number
  eventCount: number
  startUrl: string
}

// ---- parsing ------------------------------------------------------------------

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const str = (v: unknown): string => (typeof v === 'string' ? v : '')

function readPoint(v: unknown): ReplayPoint | null {
  if (!v || typeof v !== 'object') return null
  const { x, y } = v as Record<string, unknown>
  const px = num(x)
  const py = num(y)
  return px === null || py === null ? null : { x: px, y: py }
}

function readPath(v: unknown): ReplayPoint[] | null {
  if (!Array.isArray(v)) return null
  const out = v.map(readPoint).filter((p): p is ReplayPoint => p !== null)
  return out.length >= 2 ? out : null
}

/** A person event's `ask`: four short words, nothing else. */
function readPerson(a: Record<string, unknown>): ReplayPerson {
  return { kind: str(a.kind).slice(0, 40), action: str(a.action).slice(0, 40), outcome: str(a.outcome).slice(0, 40), origin: str(a.origin).slice(0, 200) }
}

/** The recordings list, or [] for anything that is not one. */
export function readRecordingList(raw: unknown): RecordingSummary[] {
  const turns = raw && typeof raw === 'object' ? (raw as Record<string, unknown>).turns : null
  if (!Array.isArray(turns)) return []
  const out: RecordingSummary[] = []
  for (const t of turns) {
    if (!t || typeof t !== 'object') continue
    const d = t as Record<string, unknown>
    const turnId = str(d.turn_id)
    const startedTs = num(d.started_ts)
    if (!turnId || startedTs === null) continue
    out.push({
      turnId,
      startedTs,
      endedTs: num(d.ended_ts),
      live: d.live === true,
      frameCount: num(d.frame_count) ?? 0,
      eventCount: num(d.event_count) ?? 0,
      startUrl: str(d.start_url),
    })
  }
  return out
}

/** One recording, or null for a body that is not one. Frames and events end up sorted by time. */
export function readRecording(raw: unknown, sessionKey: string): Recording | null {
  if (!raw || typeof raw !== 'object') return null
  const d = raw as Record<string, unknown>
  const turnId = str(d.turn_id)
  if (!turnId) return null
  const frames: ReplayFrame[] = []
  for (const f of Array.isArray(d.frames) ? d.frames : []) {
    if (!f || typeof f !== 'object') continue
    const r = f as Record<string, unknown>
    const i = num(r.i)
    const ts = num(r.ts)
    if (i === null || ts === null || !Number.isInteger(i) || i < 0) continue
    frames.push({ i, ts, w: num(r.w) ?? 0, h: num(r.h) ?? 0, src: browserLiveFrameUrl(sessionKey, turnId, i) })
  }
  frames.sort((a, b) => a.ts - b.ts)
  const events: ReplayEvent[] = []
  let n = 0
  for (const e of Array.isArray(d.events) ? d.events : []) {
    if (!e || typeof e !== 'object') continue
    const r = e as Record<string, unknown>
    const ts = num(r.ts)
    if (ts === null) continue
    n += 1
    const args = r.args && typeof r.args === 'object' && !Array.isArray(r.args) ? r.args as Record<string, unknown> : {}
    const person = r.actor === 'person' && r.ask && typeof r.ask === 'object' ? readPerson(r.ask as Record<string, unknown>) : undefined
    const op: ReplayOp = person ? 'person' : typeof r.op === 'string' && OPS.has(r.op) ? (r.op as ReplayOp) : 'other'
    events.push({
      id: str(r.id) || `e${n}`,
      ts,
      op,
      // A person event carries no text of its own on screen: only its four words.
      label: person ? '' : str(r.label),
      args: person ? {} : args,
      ...(person ? { person } : {}),
      point: readPoint(r.point),
      path: readPath(r.path),
      url: str(r.url),
      status: r.status === 'error' ? 'error' : 'ok',
      actor: r.actor === 'user' || r.actor === 'person' ? 'user' : 'agent',
      loop: r.loop === true,
      ...(num(r.fw) && num(r.fh) ? { fw: num(r.fw) as number, fh: num(r.fh) as number } : {}),
    })
  }
  events.sort((a, b) => a.ts - b.ts)
  const last = frames[frames.length - 1]
  const startedTs = num(d.started_ts) ?? Math.min(frames[0]?.ts ?? Infinity, events[0]?.ts ?? Infinity)
  return {
    turnId,
    startedTs: Number.isFinite(startedTs) ? startedTs : 0,
    endedTs: num(d.ended_ts),
    live: d.live === true,
    width: num(d.width) ?? last?.w ?? 0,
    height: num(d.height) ?? last?.h ?? 0,
    frames,
    events,
    source: 'server',
  }
}

/** The recording the card can build from memory: its frame ring and its step log. */
export function recordingFromMemory(turn: BrowserLiveTurn): Recording {
  const ring = turn.ring.all()
  const frames: ReplayFrame[] = ring.map((f, i) => ({ i, ts: f.receivedAt, w: f.header.w, h: f.header.h, src: f }))
  const events: ReplayEvent[] = turn.log.map((e, i) => ({
    id: `m${i}`, ts: e.ts, op: e.op, label: e.label, args: {}, point: null, path: null,
    url: e.url, status: 'ok', actor: 'agent', loop: false,
  }))
  const last = ring[ring.length - 1]
  const starts = [turn.startedAt, frames[0]?.ts, events[0]?.ts].filter((v): v is number => typeof v === 'number')
  return {
    turnId: turn.turnId,
    startedTs: Math.min(...starts),
    endedTs: turn.endedAt,
    live: turn.endedAt === null,
    width: last?.header.w ?? 0,
    height: last?.header.h ?? 0,
    frames,
    events,
    source: 'memory',
  }
}

// ---- the player's arithmetic ------------------------------------------------------

/** The recording's length in ms: to its end, its last frame or its last event. */
export function durationOf(rec: Recording): number {
  const ends = [rec.endedTs, rec.frames[rec.frames.length - 1]?.ts, rec.events[rec.events.length - 1]?.ts]
    .filter((v): v is number => typeof v === 'number')
  const end = ends.length ? Math.max(...ends) : rec.startedTs
  return Math.max(0, end - rec.startedTs)
}

/** Index of the last item with `ts <= at`, or -1. `items` sorted by `ts`. */
export function lastAtOrBefore(items: readonly { ts: number }[], at: number): number {
  let lo = 0
  let hi = items.length - 1
  let ans = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (items[mid].ts <= at) { ans = mid; lo = mid + 1 } else hi = mid - 1
  }
  return ans
}

/** The frame showing at offset `t` ms: the last one at or before it, else the first. */
export function frameAt(rec: Recording, t: number): number {
  if (rec.frames.length === 0) return -1
  return Math.max(0, lastAtOrBefore(rec.frames, rec.startedTs + t))
}

/** How long an action stays drawn over the picture, in recording ms. */
export const OVERLAY_HOLD_MS = 1200

/** How far after an action a frame may be and still be that action's picture. */
export const ACTION_FRAME_LEAD_MS = 1000

/**
 * The frame to show while action `ev` is drawn over the picture. An action's
 * point is where it landed after the browser scrolled the target into view, so
 * the frame before the action can show the page at its old scroll position and
 * put the mark on the wrong element. The first frame at or after the action,
 * when it came within `ACTION_FRAME_LEAD_MS`, shows the page the point belongs
 * to; otherwise the frame at the play head.
 */
export function frameForAction(rec: Recording, t: number, ev: number): number {
  const at = frameAt(rec, t)
  const e = rec.events[ev]
  if (!e) return at
  const i = lastAtOrBefore(rec.frames, e.ts - 1) + 1
  const f = rec.frames[i]
  return f && f.ts - e.ts <= ACTION_FRAME_LEAD_MS && i > at ? i : at
}

/** The action drawn at offset `t`: the newest one in the last `OVERLAY_HOLD_MS`. */
export function activeEvent(rec: Recording, t: number, hold = OVERLAY_HOLD_MS): number {
  const i = lastAtOrBefore(rec.events, rec.startedTs + t)
  if (i < 0) return -1
  return rec.startedTs + t - rec.events[i].ts <= hold ? i : -1
}

/** The step the list highlights at `t`: the last one that has happened. */
export function currentStep(rec: Recording, t: number): number {
  return lastAtOrBefore(rec.events, rec.startedTs + t)
}

/** Offset of step `i`, clamped into the recording. */
export function offsetOf(rec: Recording, i: number): number {
  const e = rec.events[i]
  if (!e) return 0
  return Math.min(durationOf(rec), Math.max(0, e.ts - rec.startedTs))
}

/** Where the Next button goes: the first step strictly after `t`, or the end. */
export function nextOffset(rec: Recording, t: number): number {
  const at = rec.startedTs + t
  const e = rec.events.find(ev => ev.ts > at + 1)
  return e ? offsetOf(rec, rec.events.indexOf(e)) : durationOf(rec)
}

/**
 * Where Previous goes: the step before the current one when the play head sits
 * on (or within half a second of) a step, else the start of the current step,
 * the way a media player's previous-track button behaves.
 */
export function prevOffset(rec: Recording, t: number): number {
  const i = currentStep(rec, t)
  if (i < 0) return 0
  const here = offsetOf(rec, i)
  if (t - here > 500) return here
  return i > 0 ? offsetOf(rec, i - 1) : 0
}

/** A point in the recording's space as percentages of the picture box. */
export function toPercent(p: ReplayPoint, space: { w: number; h: number }): { left: number; top: number } | null {
  if (!(space.w > 0) || !(space.h > 0)) return null
  const clamp = (v: number) => Math.min(100, Math.max(0, v))
  return { left: clamp((p.x / space.w) * 100), top: clamp((p.y / space.h) * 100) }
}

/** The scroll direction an event names, from whichever spelling its args use. */
export function scrollDirection(args: Record<string, unknown>): 'up' | 'down' | 'left' | 'right' {
  const pick = (...keys: string[]) => {
    for (const k of keys) { const v = num(args[k]); if (v !== null) return v }
    return 0
  }
  // Only delta spellings: a scroll's `x`/`y` are where the wheel turned, not how far.
  const dy = pick('dy', 'delta_y', 'deltaY')
  const dx = pick('dx', 'delta_x', 'deltaX')
  if (Math.abs(dx) > Math.abs(dy)) return dx < 0 ? 'left' : 'right'
  return dy < 0 ? 'up' : 'down'
}

export const SPEEDS = [1, 2, 4] as const
export type ReplaySpeed = typeof SPEEDS[number]

// ---- data hooks ---------------------------------------------------------------------

export const recordingsQueryKey = (sessionKey: string) => ['browser-live', 'recordings', sessionKey] as const
export const recordingQueryKey = (sessionKey: string, turnId: string) => ['browser-live', 'recording', sessionKey, turnId] as const

/** How often a recording still being written is read again. */
export const LIVE_RECORDING_REFRESH_MS = 2000

/**
 * The last recorded frame of one turn, as an image URL, or null. The done card's
 * thumbnail when this page holds no frame of the turn: the card was rebuilt from
 * an event after a reload or a reconnect (frames live only in memory), or every
 * frame of the turn reached the gateway's recorder while no card was showing.
 */
export function useRecordedThumb(sessionKey: string, turnId: string, enabled: boolean): string | null {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!enabled) return
    const ctl = new AbortController()
    api.browserLiveRecordings(sessionKey, ctl.signal)
      .then((raw) => {
        const hit = raw === null ? undefined : readRecordingList(raw).find(r => r.turnId === turnId)
        setUrl(hit && hit.frameCount > 0 ? browserLiveFrameUrl(sessionKey, turnId, hit.frameCount - 1) : null)
      })
      .catch(() => { /* no recording: the globe stays */ })
    return () => ctl.abort()
  }, [sessionKey, turnId, enabled])
  return enabled ? url : null
}

/** The session's recorded turns, or null when the gateway keeps none. */
export function useRecordingList(sessionKey: string | null, enabled: boolean) {
  return useQuery({
    queryKey: recordingsQueryKey(sessionKey ?? ''),
    queryFn: async ({ signal }) => {
      const raw = await api.browserLiveRecordings(sessionKey as string, signal)
      return raw === null ? null : readRecordingList(raw)
    },
    enabled: enabled && !!sessionKey,
    staleTime: 5_000,
    retry: false,
  })
}

/**
 * The recording a replay plays: the gateway's when it has one with frames, else
 * the card's memory. `memory` is the live store's record for the same turn.
 */
export function useRecording(sessionKey: string | null, turnId: string | null, memory: BrowserLiveTurn | null, enabled: boolean): {
  recording: Recording | null
  loading: boolean
  error: Error | null
} {
  const q = useQuery({
    queryKey: recordingQueryKey(sessionKey ?? '', turnId ?? ''),
    queryFn: async ({ signal }) => {
      const raw = await api.browserLiveRecording(sessionKey as string, turnId as string, signal)
      return raw === null ? null : readRecording(raw, sessionKey as string)
    },
    enabled: enabled && !!sessionKey && !!turnId,
    staleTime: 1_000,
    retry: false,
    refetchInterval: (query) => (query.state.data?.live ? LIVE_RECORDING_REFRESH_MS : false),
  })
  const ringLength = memory?.ring.length ?? 0
  // The ring grows in place, so its length is part of the key.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- ringLength stands for the ring's contents
  const fromMemory = useMemo(() => (memory ? recordingFromMemory(memory) : null), [memory, ringLength])
  return { ...pickRecording(q.data ?? null, fromMemory, q.isLoading), error: q.error }
}

/**
 * Which recording the view shows: the gateway's when it has frames; the card's
 * own when it has frames and the gateway has none yet; else the gateway's steps
 * alone (a turn whose pictures were all dropped for an unknown mask).
 */
export function pickRecording(server: Recording | null, memory: Recording | null, loading: boolean): { recording: Recording | null; loading: boolean } {
  if (server && server.frames.length > 0) return { recording: server, loading: false }
  // Wait for the gateway's first answer before falling back, so the view does not
  // play the card's frames for a moment and then jump to the recording.
  if (loading) return { recording: null, loading: true }
  if (memory && memory.frames.length > 0) return { recording: memory, loading: false }
  if (server && server.events.length > 0) return { recording: server, loading: false }
  return { recording: memory, loading: false }
}
