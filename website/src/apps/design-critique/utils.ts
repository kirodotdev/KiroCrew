import { HKEY, JOBKEY, LIVEKEY, LIVE_TTL_MS, SLOTSKEY } from './constants'
import type { Detected, DiscoveryScreen, Finding, Flow, HistoryEntry, Job, Report, ReportScreen, Scope, Screen, SlotData } from './types'

// What did the user hand us? Decide from the text they pasted.
// Accepted: screenshots, a Figma link, a GitHub/GitLab/Bitbucket repo, a local path,
// or a URL that is ALREADY serving (your dev server, a preview deploy, a live page).
// We never start a server ourselves — we only look at one that's already up.
export function detectKind(raw: string): Detected | null {
  const s = String(raw || '').trim()
  if (!s) return null
  if (/^https?:\/\/(www\.)?figma\.com\//i.test(s)) return { kind: 'figma', value: s }
  if (/^(https?:\/\/)?(github|gitlab|bitbucket)\.com\/[^/]+\/[^/]+/i.test(s)) return { kind: 'repo', value: s.replace(/^(?!https?:)/, 'https://') }
  if (/^https?:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?/i.test(s)) return { kind: 'url', value: s, local: true }
  if (/^localhost(:\d+)/i.test(s) || /^127\.0\.0\.1(:\d+)/.test(s)) return { kind: 'url', value: 'http://' + s, local: true }
  if (/^https?:\/\//i.test(s)) return { kind: 'url', value: s }
  // POSIX absolute / home / relative, plus Windows drive-letter (C:\ or C:/) and
  // UNC (\\host\share). Without the Windows forms a user on a supported platform
  // pasting C:\Users\me\app was told to "give me an absolute local path" — which
  // is what they had just given.
  if (/^(\/|~\/|\.\/)/.test(s) || /^[a-zA-Z]:[\\/]/.test(s) || /^\\\\[^\\]+\\/.test(s)) return { kind: 'local', value: s }
  return { kind: 'unknown', value: s }
}

// Said back to the user before they start, so "a URL" is never ambiguous.
export function recognise(det: Detected | null): { ok: boolean; text: string } | null {
  if (!det) return null
  switch (det.kind) {
    case 'figma': return { ok: true, text: 'Figma file — I’ll pull the frames.' }
    case 'repo': return { ok: true, text: 'Code repo — I’ll clone it and list the screens. Only pages that render without a server can be seen.' }
    case 'local': return { ok: true, text: 'Local code — I’ll list the screens. Only pages that render without a server can be seen.' }
    case 'url': return det.local
      ? { ok: true, text: 'Your dev server — it must be running right now. I’ll capture it live and measure real contrast and sizes.' }
      : { ok: true, text: 'A live/deployed page — I’ll capture it and measure real contrast and sizes.' }
    default: return { ok: false, text: 'Not something I recognise. Give me a Figma link, a GitHub repo, an absolute local path, or a URL that’s already serving.' }
  }
}

export function extractJson<T = Record<string, unknown>>(text: string | undefined): T | null {
  if (!text) return null
  const t = String(text).replace(/```json\s*/gi, '').replace(/```/g, '').trim()
  const a = t.indexOf('{'); const b = t.lastIndexOf('}')
  if (a === -1 || b === -1 || b < a) return null
  try { return JSON.parse(t.slice(a, b + 1)) as T } catch { return null }
}

export function lastAssistant(messages: SlotData['messages']): string {
  if (!Array.isArray(messages)) return ''
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]; const role = m.role || m.type
    if ((role === 'assistant' || role === 'msg msg-a') && (m.content || '').trim()) return m.content || ''
  }
  return ''
}

// Screen labels come from the model, so they can run long ("Upload prompt (empty
// state)"). Strip parentheticals and cap the length — the full text stays in the tooltip.
export function shortLabel(s: string | undefined): string {
  const t = String(s || '').replace(/\s*[([].*?[)\]]\s*/g, ' ').replace(/\s+/g, ' ').trim()
  if (!t) return 'Screen'
  return t.length > 18 ? t.slice(0, 17).trimEnd() + '…' : t
}

export function relTime(ts: number): string {
  const s = Math.max(1, Math.round((Date.now() - ts) / 1000))
  if (s < 60) return s + 's ago'
  const m = Math.round(s / 60); if (m < 60) return m + 'm ago'
  const hr = Math.round(m / 60); if (hr < 24) return hr + 'h ago'
  return Math.round(hr / 24) + 'd ago'
}

export const loadHistory = (): HistoryEntry[] => { try { return JSON.parse(localStorage.getItem(HKEY) || '[]') } catch { return [] } }
/**
 * Show an in-flight run in the critique list. Called when `+ New` backgrounds a
 * running critique: the run keeps going, so it needs a visible home rather than
 * disappearing until it happens to finish. Carries no report yet — `finishReport`
 * fills this same row in place so it never jumps position.
 */
export const beginPendingCritique = (slotKey: string, screens: Screen[]): HistoryEntry[] => {
  const cur = loadHistory()
  if (!slotKey || cur.some(e => e.slotKey === slotKey)) return cur
  const next: HistoryEntry[] = [{
    id: Date.now(),
    ts: Date.now(),
    slotKey,
    screens: screens || [],
    thumbUrl: screens && screens[0] ? screens[0].url : '',
    read: '',
    report: null,
    pending: true,
  }, ...cur]
  saveHistory(next)
  return next
}

/** Remove the placeholder for a run that failed, so no dead row is left behind. */
export const dropPendingCritique = (slotKey: string): HistoryEntry[] => {
  const cur = loadHistory()
  const next = cur.filter(e => !(e.slotKey === slotKey && e.pending))
  if (next.length !== cur.length) saveHistory(next)
  return next
}

export const saveHistory = (list: HistoryEntry[]): void => { try { localStorage.setItem(HKEY, JSON.stringify(list.slice(0, 24))) } catch { /* quota */ } }
/**
 * Job records are keyed by slotKey, because more than one critique can be in
 * flight at once: `+ New` deliberately does NOT cancel a running run, so a
 * single record meant starting a second critique overwrote the first one's
 * resume pointer — the first run then had nowhere to be resumed from and its
 * result was lost on navigation, even though the reaper was already sparing its
 * slot. Storing one record per slot keeps every in-flight run resumable.
 *
 * The value is read as an object but tolerates the older single-record shape
 * written by a previous build, so an upgrade in mid-run still resumes.
 */
export const loadJobs = (): Job[] => {
  try {
    const raw = JSON.parse(localStorage.getItem(JOBKEY) || 'null')
    if (!raw || typeof raw !== 'object') return []
    // Legacy: a bare Job was stored directly.
    if (typeof (raw as Job).slotKey === 'string') return [raw as Job]
    return Object.values(raw as Record<string, Job>)
      .filter((j): j is Job => !!j && typeof j === 'object' && typeof j.slotKey === 'string')
      .sort((a, b) => (b.ts || 0) - (a.ts || 0))
  } catch { return [] }
}

/** The most recent in-flight run — what the page resumes into on mount. */
export const loadJob = (): Job | null => loadJobs()[0] || null

export const saveJob = (j: Job): void => {
  if (!j || !j.slotKey) return
  try {
    const byslot: Record<string, Job> = {}
    for (const e of loadJobs()) byslot[e.slotKey] = e
    byslot[j.slotKey] = j
    localStorage.setItem(JOBKEY, JSON.stringify(byslot))
  } catch { /* quota */ }
}

/** Drop one run's record, or every record when called with no slot. */
export const clearJob = (slotKey?: string): void => {
  try {
    if (!slotKey) { localStorage.removeItem(JOBKEY); return }
    const byslot: Record<string, Job> = {}
    for (const e of loadJobs()) if (e.slotKey !== slotKey) byslot[e.slotKey] = e
    if (Object.keys(byslot).length) localStorage.setItem(JOBKEY, JSON.stringify(byslot))
    else localStorage.removeItem(JOBKEY)
  } catch { /* ignore */ }
}
export const loadSlots = (): string[] => { try { return JSON.parse(localStorage.getItem(SLOTSKEY) || '[]') } catch { return [] } }
export const saveSlots = (l: string[]): void => { try { localStorage.setItem(SLOTSKEY, JSON.stringify(l.slice(-20))) } catch { /* quota */ } }
export const trackSlot = (k: string): void => { if (k) saveSlots(loadSlots().filter(x => x !== k).concat([k])) }
export const untrackSlot = (k: string): void => { saveSlots(loadSlots().filter(x => x !== k)) }

/**
 * Foreground for text sitting on a solid severity colour.
 *
 * The severity palette runs from a dark red to a light yellow, so no single
 * hardcoded foreground stays legible across it: white on the Minor yellow
 * (#e2c541) measures about 1.9:1, well under the 4.5:1 WCAG 1.4.3 floor that
 * the 11px badge and pin text needs. Pick per-colour from relative luminance.
 */
export function readableOn(hex: string): string {
  const m = /^#?([0-9a-fA-F]{6})$/.exec(String(hex).trim())
  if (!m) return '#fff'
  const n = parseInt(m[1], 16)
  const lin = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const c = v / 255
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)
  })
  const lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
  // Contrast against black vs against white; take whichever is higher.
  return (lum + 0.05) / 0.05 > 1.05 / (lum + 0.05) ? '#12141a' : '#fff'
}

/**
 * Coerce a model-supplied report into the shape the UI renders.
 *
 * `extractJson<Report>` is an unchecked cast over model output, so every array
 * field is only *probably* an array. Guarding each render site one at a time has
 * already cost two rounds of the same bug class (`steps` arriving as `"1"`, then
 * `keep` / `couldNotSee` / `rules`), and the failure is worse than a blank
 * section: the throw happens during render, the route-level ErrorBoundary
 * replaces the page, and because the entry is written to history first, reopening
 * that critique crashes every time.
 *
 * So normalise once, here, at the boundary. Anything that should be a list and
 * is not becomes an empty list; a wrong-shaped field costs a missing section
 * instead of an unrecoverable page.
 */
export function normalizeReport(raw: Report | null): Report | null {
  if (!raw || typeof raw !== 'object') return null
  // Element types matter, not just the container. `keep: [{text: '…'}]` passes an
  // Array.isArray check and then React throws on rendering an object as a child —
  // and because the entry is written to history first, that crash repeats on
  // every reopen. So each list is filtered to the kind it actually renders as.
  const strings = (v: unknown): string[] =>
    (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && !!x.trim()) : [])
  const objects = <T,>(v: unknown): T[] =>
    (Array.isArray(v) ? v.filter(x => !!x && typeof x === 'object' && !Array.isArray(x)) as T[] : [])
  const numbers = (v: unknown): number[] =>
    (Array.isArray(v) ? v.filter((x): x is number => typeof x === 'number' && Number.isFinite(x)) : [])
  // Anything rendered as a React child must be a string or absent. A number is
  // stringified (harmless and probably meant); an object or array is dropped,
  // because React throws on it rather than degrading.
  const text = (v: unknown): string | undefined =>
    (typeof v === 'string' ? v : typeof v === 'number' && Number.isFinite(v) ? String(v) : undefined)
  return {
    ...raw,
    overallRead: text(raw.overallRead),
    health: text(raw.health),
    screens: objects<ReportScreen>(raw.screens).map(sc => ({
      ...sc, label: text(sc?.label), path: text((sc as { path?: unknown })?.path),
    })) as ReportScreen[],
    keep: strings(raw.keep),
    couldNotSee: strings(raw.couldNotSee),
    findings: objects<Finding>(raw.findings).map(f => ({
      ...f,
      title: text(f?.title) || '',
      category: text(f?.category),
      location: text(f?.location),
      evidence: text(f?.evidence),
      fix: text(f?.fix),
      steps: numbers(f?.steps),
      rules: strings(f?.rules),
    })) as Finding[],
  }
}

/**
 * Coerce a model-supplied discovery payload into the shape the picker renders.
 *
 * Same class as normalizeReport, one layer out: guarding `Array.isArray(flows)`
 * proves the container is a list but says nothing about its ELEMENTS, so a reply
 * of `flows:[null]` still crashed when the picker read `f.screenIds`. Filter to
 * real objects and make every nested list a list, at the one place the payload
 * enters the app.
 */
export function normalizeScope(raw: Partial<Scope> | null | undefined): Scope | null {
  if (!raw || typeof raw !== 'object') return null
  const objects = <T,>(v: unknown): T[] =>
    (Array.isArray(v) ? v.filter(x => x && typeof x === 'object') as T[] : [])
  const strings = (v: unknown): string[] =>
    (Array.isArray(v) ? v.filter(x => typeof x === 'string') as string[] : [])
  const text = (v: unknown): string | undefined =>
    (typeof v === 'string' ? v : typeof v === 'number' && Number.isFinite(v) ? String(v) : undefined)
  // Built field by field on purpose: a `...raw` spread let any scalar the model
  // invented survive unchecked, which is how `framework` and `note` stayed
  // un-normalised after screens and flows were fixed. Constructing the object
  // explicitly means an unvalidated field cannot reach a React child at all —
  // the class is closed by construction rather than one field at a time. Adding
  // a field to Scope now requires deciding how it is coerced.
  const blocked = raw.blocked && typeof raw.blocked === 'object' && !Array.isArray(raw.blocked)
    ? { ...raw.blocked, reason: text(raw.blocked.reason) || '', detail: text(raw.blocked.detail) }
    : null
  return {
    framework: text(raw.framework),
    note: text(raw.note),
    blocked: blocked as Scope['blocked'],
    screens: objects<DiscoveryScreen>(raw.screens)
      .filter(s => typeof s.id === 'string' && !!s.id)
      // Every field the picker renders, not a subset: `group` was missed once and
      // an object there crashes the route exactly like `label` would. `canSee`
      // is coerced because a model string "false" is truthy and would silently
      // flip a screen to visible.
      .map(s => ({
        id: s.id,
        label: text(s.label) || s.id,
        ref: text(s.ref),
        group: text(s.group),
        why: text(s.why),
        canSee: typeof s.canSee === 'boolean' ? s.canSee : undefined,
      })) as DiscoveryScreen[],
    flows: objects<Flow>(raw.flows).map(f => ({
      label: text(f.label),
      why: text(f.why),
      screenIds: strings(f.screenIds),
    })) as Flow[],
    cannotSee: strings((raw as { cannotSee?: unknown }).cannotSee),
  }
}

/**
 * Slots with a run in flight, so the mount-time reaper can spare all of them.
 *
 * Only one job record is persisted (JOBKEY), so the reaper used to spare exactly
 * one slot. That is wrong as soon as two critiques overlap, which the UI allows
 * on purpose — `+ New` deliberately does NOT cancel a running critique, it lets
 * it finish into History. Start A, start B, navigate away, and the reaper deleted
 * A's slot along with its result.
 *
 * Entries are timestamped and expire, because a tab closed mid-run would
 * otherwise mark a slot live forever and defeat the reaper it is exempt from.
 */
interface LiveRun { k: string; ts: number }

const readLive = (): LiveRun[] => {
  try {
    const raw = JSON.parse(localStorage.getItem(LIVEKEY) || '[]')
    if (!Array.isArray(raw)) return []
    const now = Date.now()
    return raw.filter((r): r is LiveRun =>
      !!r && typeof r === 'object' && typeof r.k === 'string' && !!r.k
      && now - (typeof r.ts === 'number' ? r.ts : 0) < LIVE_TTL_MS)
  } catch { return [] }
}

const writeLive = (l: LiveRun[]): void => {
  try { localStorage.setItem(LIVEKEY, JSON.stringify(l.slice(-20))) } catch { /* quota */ }
}

/** Slot keys whose run is still in flight (expired entries dropped). */
export const loadLive = (): string[] => readLive().map(r => r.k)

export const markLive = (k: string): void => {
  if (!k) return
  writeLive(readLive().filter(r => r.k !== k).concat([{ k, ts: Date.now() }]))
}

export const unmarkLive = (k: string): void => {
  if (!k) return
  writeLive(readLive().filter(r => r.k !== k))
}
