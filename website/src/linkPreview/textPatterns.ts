/**
 * Plain-text link previews: `contributes.linkPreviews[].textPatterns`.
 *
 * An app may declare text shapes (a ticket key, an id) that open its card the way
 * a link does. Each entry is `{ pattern, flags?, id?, defaultOff? }`: a regex source
 * with its own flags (`g` is implied), and an optional id the app uses to switch the
 * pattern off (`ctx.setTextPatternsOff`). The core wraps each match in a hoverable
 * `<span data-link-preview-text>` (never a link); on hover the provider gets the
 * matched `text`.
 *
 * Safe by construction rather than by inspection. No static check can prove an
 * arbitrary regex cheap, so the patterns never run on the dashboard's thread:
 *
 * - Matching happens in a Web Worker (`textPatternWorker.ts`), one batch per render
 *   pass: every text node rendered in the same tick goes in one batch.
 * - Each pattern has its own hard time budget (`SCAN_BUDGET_MS`), re-armed when the
 *   worker posts `pattern-start` for it. On an overrun the worker is terminated,
 *   the pattern that used up its own budget is disabled for the rest of the
 *   session, the overrun is logged once, and the rest of the batch is retried on a
 *   fresh worker without it.
 * - A text node longer than `MAX_SCAN_SUBJECT` (2000 characters) is not scanned.
 *
 * Rendering never waits: a text node the worker has not answered for yet renders
 * as plain text, and the message re-renders with its spans when the answer comes.
 * The hover resolves a span from the worker's answer for that exact text, so a span
 * written in raw message HTML reaches only text a pattern actually matched.
 *
 * @module linkPreview/textPatterns
 */
import textPatternWorkerUrl from './textPatternWorker.ts?worker&url'
import { workerUrlWithBuildKey } from '../utils/workerCacheKey'
import { MAX_SCAN_SUBJECT, type FromWorker, type ScanHit, type ScanPattern, type ToWorker } from './textPatternScan'

/** Mirrors `MAX_LINK_PREVIEW_TEXT_PATTERNS` / `_LEN` in `apps/manifest_contributes.py`. */
export const MAX_TEXT_PATTERNS = 16
export const MAX_TEXT_PATTERN_LEN = 200
/** Hard budget for one pattern's run in the worker, re-armed at each `pattern-start`. */
export const SCAN_BUDGET_MS = 250
/** A worker that has not started a batch by then never will (it failed to load). */
export const WORKER_START_TIMEOUT_MS = 5000
/** Flags an entry may carry, besides the implied `g`. Mirrors `_TEXT_PATTERN_FLAGS_RE`. */
const FLAGS_RE = /^(?!.*(.).*\1)[giu]*$/
const STORE_PREFIX = 'kirocrew:link-preview-text-off:'
/** Answers kept. Past this no new text is scanned (it stays plain): clearing the
 *  cache instead would make every mounted message ask again, forever. */
export const MAX_CACHED_TEXTS = 20000

export interface RawTextPattern {
  pattern: string
  flags?: string
  id?: string
  defaultOff?: boolean
}

interface Declared {
  /** Provider key (`app:id`) that declared it. */
  owner: string
  appName: string
  id: string
  defaultOff: boolean
  source: string
  flags: string
}

export interface TextHit {
  start: number
  end: number
}

interface WorkerLike {
  postMessage(msg: ToWorker): void
  terminate(): void
  onmessage: ((e: MessageEvent<FromWorker>) => void) | null
}
type WorkerFactory = () => WorkerLike | null

function defaultWorker(): WorkerLike | null {
  if (typeof Worker === 'undefined') return null
  try {
    return new Worker(workerUrlWithBuildKey(textPatternWorkerUrl), { type: 'module' }) as unknown as WorkerLike
  } catch {
    return null
  }
}

let makeWorker: WorkerFactory = defaultWorker
let declared: Declared[] = []
let active: Declared[] = []
let appliedKey = ''
let generation = 0
const listeners = new Set<() => void>()
/** appName -> ids the app reported off (null: it never reported; use defaultOff). */
const reportedOff = new Map<string, string[] | null>()
/** `owner|id` of patterns an overrun disabled for this session. */
const disabled = new Set<string>()
let overrunLogged = false

/** text -> hits, for the active set. */
let answers = new Map<string, Array<TextHit & { owner: string }>>()
/** span text -> owner, for the hover. */
let spanOwners = new Map<string, string>()
let worker: WorkerLike | null = null
let queued = new Set<string>()
let flushScheduled = false
let inFlight: { batch: number; texts: string[]; patterns: Declared[]; running: number; started: boolean; timer: ReturnType<typeof setTimeout> } | null = null
/** The worker never started a batch: no spans for the rest of the session. */
let workerUnavailable = false
let batchSeq = 0

const keyOf = (d: Declared) => `${d.owner}|${d.id}`

function readOff(appName: string): string[] | null {
  if (reportedOff.has(appName)) return reportedOff.get(appName) ?? null
  let ids: string[] | null = null
  try {
    const raw = window.localStorage?.getItem(STORE_PREFIX + appName)
    const parsed = raw ? (JSON.parse(raw) as unknown) : null
    if (Array.isArray(parsed)) ids = parsed.filter((x): x is string => typeof x === 'string')
  } catch {
    ids = null
  }
  reportedOff.set(appName, ids)
  return ids
}

function notify(): void {
  generation += 1
  for (const l of listeners) l()
}

function stopWorker(): void {
  if (inFlight) clearTimeout(inFlight.timer)
  inFlight = null
  try { worker?.terminate() } catch { /* already gone */ }
  worker = null
}

/** Recompute the active set; every cached answer belonged to the old one. */
function recompute(): void {
  active = declared.filter((d) => {
    if (disabled.has(keyOf(d))) return false
    const off = readOff(d.appName)
    return !(off ? off.includes(d.id) : d.defaultOff)
  })
  answers = new Map()
  spanOwners = new Map()
  queued = new Set()
  stopWorker()
  notify()
}

/** Within the bounds and compiles with its flags. Compiling runs nothing; whether
 *  it matches the empty string is the worker's to find out (`scanTexts`), since
 *  that test runs the pattern. */
function usable(source: string, flags: string): boolean {
  if (!source || source.length > MAX_TEXT_PATTERN_LEN || !FLAGS_RE.test(flags)) return false
  try {
    void new RegExp(source, flags)
    return true
  } catch {
    return false
  }
}

/**
 * Replace the declared set from the resolved providers. Idempotent for an unchanged
 * set; a change bumps the generation so rendered messages re-parse.
 */
export function setLinkPreviewTextPatterns(
  providers: ReadonlyArray<{ key: string; appName: string; textPatterns: readonly RawTextPattern[] }>,
): void {
  const key = JSON.stringify(providers.map((p) => [p.key, p.textPatterns]))
  if (key === appliedKey) return
  appliedKey = key
  const next: Declared[] = []
  for (const p of providers) {
    p.textPatterns.slice(0, MAX_TEXT_PATTERNS).forEach((tp, i) => {
      const flags = tp.flags ?? ''
      if (!usable(tp.pattern, flags)) return
      next.push({ owner: p.key, appName: p.appName, id: tp.id || String(i), defaultOff: tp.defaultOff === true, source: tp.pattern, flags })
    })
  }
  declared = next
  recompute()
}

/**
 * The app's own switch (`ctx.setTextPatternsOff`): which of its pattern ids are off
 * now. Remembered across page loads, so the first paint after a reload already
 * matches the app's settings although its module only loads on the first hover.
 */
export function setTextPatternsOff(appName: string, ids: readonly string[]): void {
  const clean = [...new Set(ids.filter((x) => typeof x === 'string'))].sort()
  const prev = readOff(appName)
  if (prev && JSON.stringify(prev) === JSON.stringify(clean)) return
  reportedOff.set(appName, clean)
  try {
    window.localStorage?.setItem(STORE_PREFIX + appName, JSON.stringify(clean))
  } catch {
    // storage full or blocked: this page load still honours it
  }
  recompute()
}

export function hasTextPatterns(): boolean {
  return active.length > 0
}

/** For `useSyncExternalStore`: bumps whenever spans would render differently. */
export function subscribeTextPatterns(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
export function textPatternGeneration(): number {
  return generation
}

function onWorkerStuck(): void {
  if (!inFlight || inFlight.started) return
  workerUnavailable = true
  // eslint-disable-next-line no-console -- logged once; plain-text previews are off for this page load
  console.warn('[link-preview] the text pattern worker did not start; plain-text previews are off for this page')
  stopWorker()
  queued = new Set()
}

function onOverrun(): void {
  const f = inFlight
  if (!f) return
  const culprit = f.patterns[f.running]
  if (culprit) disabled.add(keyOf(culprit))
  if (!overrunLogged) {
    overrunLogged = true
    // eslint-disable-next-line no-console -- logged once per session; the pattern stays off
    console.warn(`[link-preview] a text pattern of ${culprit?.owner ?? 'an app'} ran past its time budget and is off for this session`)
  }
  const retry = f.texts
  stopWorker()
  active = active.filter((d) => !disabled.has(keyOf(d)))
  // The disabled pattern may own hits already cached (and drawn) from earlier
  // batches, and a span's owner is the first pattern that takes its text, so the
  // remaining set can give the same text another owner. Drop every cached answer
  // and owner and let the messages re-render: they ask again and are rescanned
  // with what is left. The set only shrinks, so this ends.
  answers = new Map()
  spanOwners = new Map()
  for (const t of retry) queued.add(t)
  notify()
  schedule()
}

function onAnswer(e: MessageEvent<FromWorker>): void {
  const msg = e.data
  const f = inFlight
  if (!f || msg.batch !== f.batch) return
  if (msg.type === 'pattern-start') {
    // Each pattern gets its own budget, re-armed as it starts: an overrun is the
    // pattern that used up its own time, not whichever one the batch reached
    // when a shared timer fired. A cold worker start is no pattern's time.
    f.running = msg.i
    f.started = true
    clearTimeout(f.timer)
    f.timer = setTimeout(onOverrun, SCAN_BUDGET_MS)
    return
  }
  clearTimeout(f.timer)
  inFlight = null
  // A pattern that matches the empty string is off for the session, quietly: it
  // was never drawn, so nothing changes on screen.
  const rejected = (msg.rejected ?? []).map((i) => f.patterns[i]).filter((d): d is Declared => Boolean(d))
  if (rejected.length) {
    for (const d of rejected) disabled.add(keyOf(d))
    active = active.filter((d) => !disabled.has(keyOf(d)))
  }
  f.texts.forEach((text, t) => {
    const hits = (msg.hits[t] ?? []).map((h: ScanHit) => ({ start: h.start, end: h.end, owner: f.patterns[h.pattern]?.owner ?? '' }))
    answers.set(text, hits)
    // The worker named each hit's owner from its text alone, so this is the same
    // for a given text in every message.
    for (const h of hits) spanOwners.set(text.slice(h.start, h.end), h.owner)
  })
  notify()
  if (queued.size) schedule()
}

function flush(): void {
  flushScheduled = false
  if (inFlight || !queued.size || !active.length || workerUnavailable) return
  if (!worker) {
    worker = makeWorker()
    if (!worker) { queued = new Set(); return }
    worker.onmessage = onAnswer
  }
  const texts = [...queued]
  queued = new Set()
  const patterns = active.slice()
  const batch = ++batchSeq
  inFlight = { batch, texts, patterns, running: 0, started: false, timer: setTimeout(onWorkerStuck, WORKER_START_TIMEOUT_MS) }
  const scanPatterns: ScanPattern[] = patterns.map((p) => ({ source: p.source, flags: p.flags }))
  worker.postMessage({ type: 'scan', batch, patterns: scanPatterns, texts })
}

function schedule(): void {
  if (flushScheduled) return
  flushScheduled = true
  queueMicrotask(flush)
}

/**
 * Hits for one text node: the worker's answer, or none yet (the text is queued and
 * the message re-renders when the answer comes). Never runs a pattern here.
 */
export function textPatternHits(value: string): TextHit[] {
  if (!active.length || !value || value.length > MAX_SCAN_SUBJECT) return []
  const known = answers.get(value)
  if (known) return known
  if (!inFlight?.texts.includes(value) && !queued.has(value)) {
    if (answers.size + queued.size + (inFlight?.texts.length ?? 0) >= MAX_CACHED_TEXTS) return []
    queued.add(value)
    schedule()
  }
  return []
}

/** The owning provider for a span's text, from the worker's answers. Null when none. */
export function resolveTextPreview(text: string): { owner: string } | null {
  const owner = spanOwners.get(text)
  return owner && active.some((d) => d.owner === owner) ? { owner } : null
}

/** Test-only: replace how the worker is made (vitest has no `Worker`). */
export function setTextPatternWorkerFactoryForTest(factory?: WorkerFactory): void {
  stopWorker()
  makeWorker = factory ?? defaultWorker
}

/** Test-only. */
export function resetTextPatternsForTest(): void {
  stopWorker()
  declared = []
  active = []
  appliedKey = ''
  answers = new Map()
  spanOwners = new Map()
  queued = new Set()
  flushScheduled = false
  disabled.clear()
  overrunLogged = false
  workerUnavailable = false
  reportedOff.clear()
  try {
    for (let i = window.localStorage.length - 1; i >= 0; i--) {
      const k = window.localStorage.key(i)
      if (k?.startsWith(STORE_PREFIX)) window.localStorage.removeItem(k)
    }
  } catch {
    // no storage in this environment
  }
  generation += 1
}
