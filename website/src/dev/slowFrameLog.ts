/** Slow frame log -- an opt-in developer instrument that keeps, in this tab's
 *  memory, the slowest frames and the slowest interactions the page has had,
 *  with the script each frame spent its time in.
 *
 *  WHY THIS EXISTS. "It felt slow when I clicked" is not something a benchmark
 *  on another machine can answer. The browser already knows: a Long Animation
 *  Frame entry names the script that held the frame and how long it forced
 *  layout, and an Event Timing entry says how long an input waited, ran and
 *  took to paint. This reads both through ONE PerformanceObserver and keeps the
 *  worst of each, so the person who saw the stall can open Settings > Developer
 *  > Debug tools and read what caused it.
 *
 *  NOTHING LEAVES THE PAGE. The log is two bounded arrays in this module. There
 *  is no request, no storage write of the readings and no console output.
 *
 *  OFF MEANS OFF. With the flag off no observer exists and nothing is kept.
 *  Turning it on asks the browser for the entries it already buffered, so the
 *  log also shows what happened before the switch was flipped: every long
 *  frame, and the interactions of 104 ms and over.
 */

import { LOCKED_DEBUG_TOOLS, controlLocked } from '../utils/editionLocks'

const ENABLED_KEY = 'mc-slow-frame-log'
const ENABLED_EVENT = 'mc-slow-frame-log-changed'

/** Entries kept per list. The lists hold the slowest, not the newest: a stall
 *  from an hour ago is still there when the reader comes looking. */
const MAX_KEPT = 20
/** An interaction faster than this is not reported by the browser to us. 40 ms
 *  is the floor the Event Timing API allows above its 16 ms minimum that still
 *  keeps ordinary typing out of the callback. */
const SLOW_INTERACTION_FLOOR_MS = 40

export interface SlowFrameScript {
  /** How long the script ran, in ms. */
  duration: number
  /** What started it: a listener, a timer, a promise reaction. */
  invoker: string
  /** The function and file the browser attributes it to; '' when it gives none. */
  source: string
  /** Time the script spent forcing style and layout, in ms. */
  forcedLayout: number
}

export interface SlowFrame {
  /** `performance.now()` at the frame's start. */
  at: number
  duration: number
  /** The part of the frame that blocked input, in ms. */
  blocking: number
  /** The longest scripts of the frame, longest first. */
  scripts: SlowFrameScript[]
}

export interface SlowInteraction {
  at: number
  /** Input to next paint, in ms: what INP is made of. */
  duration: number
  /** The event type of the longest entry (`pointerdown`, `keydown`, ...). */
  name: string
  /** The element's tag and its accessible name, never the element itself. */
  target: string
  /** Wait before the handlers started, in ms. */
  inputDelay: number
  /** Time inside the handlers, in ms. */
  processing: number
  /** Handlers done to the frame on screen, in ms. */
  presentation: number
}

export interface SlowFrameSnapshot {
  enabled: boolean
  /** Whether this browser reports long animation frames at all. */
  framesSupported: boolean
  frames: readonly SlowFrame[]
  interactions: readonly SlowInteraction[]
}

interface LoafScript {
  duration: number
  invoker?: string
  sourceFunctionName?: string
  sourceURL?: string
  forcedStyleAndLayoutDuration?: number
}
interface LoafEntry extends PerformanceEntry {
  blockingDuration?: number
  scripts?: LoafScript[]
}
interface EventEntry extends PerformanceEntry {
  interactionId?: number
  processingStart: number
  processingEnd: number
  target?: Node | null
}

let enabled = false
let observer: PerformanceObserver | null = null
let frames: SlowFrame[] = []
let interactions: SlowInteraction[] = []
/** interactionId -> its row, so the several entries of one interaction fold into one. */
const byInteraction = new Map<number, SlowInteraction>()
let snapshot: SlowFrameSnapshot | null = null
const listeners = new Set<() => void>()

function readFlag(): boolean {
  // An LTS build hides Debug tools, and its instruments stay off (RFC D12).
  if (controlLocked(LOCKED_DEBUG_TOOLS)) return false
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem(ENABLED_KEY) === '1'
  } catch {
    // Storage blocked: a log nobody can turn on is the safe answer.
    return false
  }
}

function supports(type: string): boolean {
  return typeof PerformanceObserver !== 'undefined' && (PerformanceObserver.supportedEntryTypes ?? []).includes(type)
}

/** Keep `row` if it is among the `MAX_KEPT` slowest, slowest first. */
function keepSlowest<T extends { duration: number }>(list: T[], row: T): T[] {
  if (list.length >= MAX_KEPT && row.duration <= list[list.length - 1].duration) return list
  const next = [...list, row].sort((a, b) => b.duration - a.duration)
  if (next.length > MAX_KEPT) next.length = MAX_KEPT
  return next
}

function fileOf(url: string | undefined): string {
  if (!url) return ''
  const tail = url.split('?')[0].split('/').pop() ?? ''
  return tail
}

/** A module script's invoker is its whole URL; the file name says as much. */
function shortInvoker(invoker: string | undefined): string {
  if (!invoker) return ''
  return invoker.startsWith('http://') || invoker.startsWith('https://') ? fileOf(invoker) : invoker
}

/** The tag and accessible name of the nearest named element at or above `node`:
 *  a press usually lands on the icon inside a button, and `svg` names nothing. */
function describeTarget(node: Node | null | undefined): string {
  if (!node || !(node instanceof Element)) return ''
  const named = node.closest('[aria-label], [data-testid]') ?? node
  const label = named.getAttribute('aria-label') ?? named.getAttribute('data-testid') ?? ''
  return label ? `${named.tagName.toLowerCase()} [${label.slice(0, 60)}]` : named.tagName.toLowerCase()
}

function addFrame(e: LoafEntry): void {
  const scripts = (e.scripts ?? [])
    .map((s): SlowFrameScript => ({
      duration: Math.round(s.duration),
      invoker: shortInvoker(s.invoker),
      source: [s.sourceFunctionName, fileOf(s.sourceURL)].filter(Boolean).join(' '),
      forcedLayout: Math.round(s.forcedStyleAndLayoutDuration ?? 0),
    }))
    .sort((a, b) => b.duration - a.duration)
    .slice(0, 3)
  frames = keepSlowest(frames, {
    at: Math.round(e.startTime),
    duration: Math.round(e.duration),
    blocking: Math.round(e.blockingDuration ?? 0),
    scripts,
  })
}

function addInteraction(e: EventEntry): void {
  const id = e.interactionId
  if (!id) return
  const known = byInteraction.get(id)
  // One interaction is several entries (pointerdown, pointerup, click). Its
  // duration is the longest of them, which is what INP counts.
  if (known && known.duration >= e.duration) return
  const row: SlowInteraction = {
    at: Math.round(e.startTime),
    duration: Math.round(e.duration),
    name: e.name,
    target: describeTarget(e.target) || known?.target || '',
    inputDelay: Math.round(e.processingStart - e.startTime),
    processing: Math.round(e.processingEnd - e.processingStart),
    // The browser rounds `duration` to 8 ms and not `processingEnd`, so a paint
    // right after the handlers can come out below zero.
    presentation: Math.max(0, Math.round(e.startTime + e.duration - e.processingEnd)),
  }
  if (known) interactions = interactions.filter(r => r !== known)
  byInteraction.set(id, row)
  interactions = keepSlowest(interactions, row)
  // Forget ids that fell out of the kept list, so the map stays bounded too.
  if (byInteraction.size > MAX_KEPT * 4) {
    const kept = new Set(interactions)
    for (const [key, value] of byInteraction) if (!kept.has(value)) byInteraction.delete(key)
  }
}

function onEntries(list: PerformanceObserverEntryList): void {
  for (const e of list.getEntries()) {
    if (e.entryType === 'long-animation-frame') addFrame(e as LoafEntry)
    else if (e.entryType === 'event') addInteraction(e as EventEntry)
  }
  publish()
}

function publish(): void {
  snapshot = null
  for (const l of [...listeners]) l()
}

function start(): void {
  if (observer || typeof PerformanceObserver === 'undefined') return
  const next = new PerformanceObserver(onEntries)
  try {
    // One observer, two entry types. `buffered` hands over what the browser
    // kept from before this call, so the log is not empty on the first look.
    if (supports('long-animation-frame')) next.observe({ type: 'long-animation-frame', buffered: true })
    if (supports('event')) next.observe({ type: 'event', buffered: true, durationThreshold: SLOW_INTERACTION_FLOOR_MS } as PerformanceObserverInit)
  } catch {
    // An engine that names a type it then refuses: keep whatever did register.
  }
  observer = next
}

function stop(): void {
  observer?.disconnect()
  observer = null
  frames = []
  interactions = []
  byInteraction.clear()
}

export function setSlowFrameLogEnabled(on: boolean): void {
  if (on === enabled) return
  enabled = on
  if (on) start()
  else stop()
  publish()
}

/** The current readings. The same object until something changes, so it can
 *  back `useSyncExternalStore`. */
export function slowFrameSnapshot(): SlowFrameSnapshot {
  snapshot ??= { enabled, framesSupported: supports('long-animation-frame'), frames, interactions }
  return snapshot
}

export function subscribeSlowFrameLog(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** Test seam: back to off, with nothing kept and nobody listening. */
export function __resetSlowFrameLogForTests(): void {
  stop()
  enabled = false
  listeners.clear()
  snapshot = null
}

if (typeof window !== 'undefined') {
  setSlowFrameLogEnabled(readFlag())
  window.addEventListener(ENABLED_EVENT, (e) => {
    const detail = (e as CustomEvent<unknown>).detail
    setSlowFrameLogEnabled(typeof detail === 'boolean' ? detail && !controlLocked(LOCKED_DEBUG_TOOLS) : readFlag())
  })
  // Another tab toggling it should not leave this one recording. A null key is
  // that tab clearing all of storage, the flag with it.
  window.addEventListener('storage', (e) => {
    if (e.key === ENABLED_KEY || e.key === null) setSlowFrameLogEnabled(readFlag())
  })
}

export const SLOW_FRAME_LOG_KEYS = { ENABLED_KEY, ENABLED_EVENT } as const
