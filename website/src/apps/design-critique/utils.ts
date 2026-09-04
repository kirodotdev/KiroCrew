import { HKEY, JOBKEY, LIVEKEY, LIVE_TTL_MS, SLOTSKEY } from './constants'
import { fmtRelative } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
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
//
// The text is the TAIL of that line only. The kind's own name is rendered
// separately, and localised, by `Composer.tsx`, so naming it again here would put
// an English noun in front of the localised one — which is also why the caller no
// longer has to strip a leading noun with a regex.
//
// Keys rather than literals: this runs per render, so the tail follows a language
// change the same way the noun does. Until now the noun was localised and the tail
// was not, which left one sentence in two languages.
export function recognise(det: Detected | null): { ok: boolean; text: string } | null {
  if (!det) return null
  switch (det.kind) {
    case 'figma': return { ok: true, text: i18nT('apps.designCritique.utils.i_ll_pull_the_frames') }
    case 'repo': return { ok: true, text: i18nT('apps.designCritique.utils.i_ll_clone_it_and_list_the_screens_only_pages_th') }
    case 'local': return { ok: true, text: i18nT('apps.designCritique.utils.i_ll_list_the_screens_only_pages_that_render_wit') }
    case 'url': return det.local
      ? { ok: true, text: i18nT('apps.designCritique.utils.it_must_be_running_right_now_i_ll_capture_it_liv') }
      : { ok: true, text: i18nT('apps.designCritique.utils.i_ll_capture_it_and_measure_real_contrast_and_si') }
    default: return { ok: false, text: i18nT('apps.designCritique.utils.not_something_i_recognise_give_me_a_figma_link_a') }
  }
}

/**
 * Every balanced `{…}` span in `t`, outermost only, in the order they appear.
 *
 * Quote- and escape-aware INSIDE an object, so a brace in a string value
 * (`"fix":"drop the {primary} token"`) does not move the depth counter and
 * cannot split a span.
 *
 * Outside one — at depth 0 — a quote is ignored on purpose. Prose is not JSON,
 * so its quotes do not pair: a reply opening `Here is "the report:` leaves one
 * unmatched quote, and tracking it there would put the scanner in string mode
 * for everything after, swallowing the report's own braces and emitting no span
 * at all. That is the failure this whole function exists to prevent, so string
 * mode begins only once an object is open.
 */
function braceSpans(t: string): string[] {
  const out: string[] = []
  let depth = 0, start = -1, inStr = false, esc = false
  for (let i = 0; i < t.length; i++) {
    const ch = t[i]
    if (inStr) {
      if (esc) { esc = false; continue }
      if (ch === '\\') { esc = true; continue }
      if (ch === '"') inStr = false
      continue
    }
    if (ch === '"') { if (depth > 0) inStr = true; continue }
    if (ch === '{') { if (depth === 0) start = i; depth++; continue }
    if (ch === '}' && depth > 0 && --depth === 0 && start >= 0) {
      out.push(t.slice(start, i + 1)); start = -1
    }
  }
  return out
}

/**
 * Pull the critic's JSON out of a reply.
 *
 * The contract asks for JSON and nothing else, and a reply that honours it
 * parses either way. But the tolerance this function already extends to the
 * usual model wrapping — a ```json fence, a line of prose either side — used to
 * break on a wrapper that merely CONTAINED a brace, because it sliced from the
 * first `{` to the last `}` and handed whatever lay between to `JSON.parse`.
 * A reply that closed with "tweak the `{primary}` token", or opened by naming
 * the `{schema}` it was given, therefore threw away a complete report and the
 * run ended on "not in a readable format".
 *
 * So consider every balanced span instead of one blind slice, and take the
 * largest that parses — the report is the substantive object in the reply, not
 * the aside next to it. This is strictly more tolerant than the slice: any text
 * the slice accepted still parses. A reply carrying no JSON at all still returns
 * null, which is the honest case the error message exists for.
 *
 * `accept` filters the candidates rather than vetoing the winner, and that
 * distinction is the whole point of taking it here. Judging only the largest
 * span and rejecting it outside this function would end the search at the first
 * unrecognised object, so a reply carrying a bigger unrelated object beside a
 * real report would lose the report — the same discard this function exists to
 * prevent. Rejection therefore continues to the next span.
 */
export function extractJson<T = Record<string, unknown>>(
  text: string | undefined,
  accept?: (value: unknown) => boolean,
): T | null {
  if (!text) return null
  const t = String(text).replace(/```json\s*/gi, '').replace(/```/g, '').trim()
  const spans = braceSpans(t).sort((a, b) => b.length - a.length)
  for (const s of spans) {
    let parsed: T
    try { parsed = JSON.parse(s) as T } catch { continue }
    if (!accept || accept(parsed)) return parsed
  }
  return null
}

/**
 * Does this parsed object look like a critique report, rather than a fragment of
 * one or something else that happens to share a key name?
 *
 * Needed because a JSON object is not self-identifying. A stream cut short right
 * before a nested object leaves a continuation row that parses PERFECTLY on its
 * own — split the schema before `{"severity":…}` and the suffix is a complete,
 * valid finding object — so "it parsed" cannot mean "it is the report". Accepting
 * one would be worse than the error it replaced: `normalizeReport` coerces the
 * fragment into a report with nothing in it, `finishReport` writes that blank
 * critique to history, and `endRun` then deletes the slot holding the real one.
 *
 * The test is TYPED, not a key-presence check, and that distinction carries the
 * guarantee. A key name alone is cheap for unrelated JSON to satisfy — an object
 * holding `findings` as a long STRING passes a presence test, and being longer
 * than the real report it would then be preferred over it, losing the critique to
 * exactly the blank-render path above. So each field must arrive as the type the
 * UI renders it as, which is the same shape `normalizeReport` coerces to
 * downstream: prose as a string, lists as arrays, the tally as an object.
 *
 * Any ONE correctly-typed field is enough. A real report carries several, while a
 * sparse one should still render rather than be discarded — requiring a full
 * schema here would reintroduce the original bug in a stricter disguise.
 */
export function looksLikeReport(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const v = value as Record<string, unknown>
  const isText = (k: string): boolean => typeof v[k] === 'string'
  const isList = (k: string): boolean => Array.isArray(v[k])
  const isObj = (k: string): boolean =>
    !!v[k] && typeof v[k] === 'object' && !Array.isArray(v[k])
  return isText('overallRead') || isText('health') || isObj('tally')
    || isList('screens') || isList('findings') || isList('keep') || isList('couldNotSee')
}

/**
 * The report from a finished turn, wherever in that turn it landed.
 *
 * One turn is not one message. The runner finalizes a segment per text block
 * (`_flush_segment`), so text either side of a tool group arrives as two
 * `assistant` rows, and a stream interrupted by a transient backend error is
 * persisted as a partial row plus a continuation row — the model is told to
 * carry on where it stopped, which for this prompt means resuming mid-JSON.
 * Reading only the newest row therefore discarded a report the critic really
 * did produce: a trailing remark after the JSON, or either half of a split one,
 * failed to parse and the run ended on "not in a readable format".
 *
 * So try each assistant row newest-first, then the rows joined in order, which
 * is what reassembles a split reply. Order matters: newest-first keeps the last
 * report authoritative when a turn somehow produced two, and the join is only
 * reached when no single row carries one.
 *
 * `accept` is what makes that order safe. Without it the newest-first pass is a
 * trap on exactly the split it exists to repair — a continuation beginning at a
 * nested object parses alone, and would be taken for the whole report before the
 * join ever ran. It is handed to `extractJson` rather than applied to its result,
 * so an unrecognised candidate is skipped WITHIN a row's span search and the next
 * span still gets its turn; vetoing the result here would abandon a row whose
 * report merely sat behind a larger object.
 */
export function jsonFromMessages<T = Record<string, unknown>>(
  messages: SlotData['messages'],
  accept?: (value: unknown) => boolean,
): T | null {
  if (!Array.isArray(messages)) return null
  const rows = messages
    .filter(m => { const role = m.role || m.type; return role === 'assistant' || role === 'msg msg-a' })
    .map(m => m.content || '')
    .filter(c => !!c.trim())
  for (let i = rows.length - 1; i >= 0; i--) {
    const p = extractJson<T>(rows[i], accept)
    if (p) return p
  }
  return rows.length > 1 ? extractJson<T>(rows.join(''), accept) : null
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

/**
 * Relative age of a critique run, through the shared formatting seam.
 *
 * Uses narrow `fmtRelative` rather than gluing a unit onto a number (`s + 's ago'`),
 * which is untranslatable and is what `src/i18n/unitLiterals.test.ts` gates.
 * `fmtRelative` is byte-identical in English for seconds, minutes, hours and
 * multi-day gaps; the two deliberate deltas documented on the seam apply here too
 * (a sub-second age reads `now` rather than `1s ago`, and exactly one day back reads
 * `yesterday`), and it truncates rather than rounds so an age never reads as further
 * in the past than it is.
 */
export function relTime(ts: number): string {
  return fmtRelative(ts)
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
 * single shared record would let starting a second critique overwrite the first
 * one's resume pointer — leaving the first run with nowhere to resume from and
 * its result lost on navigation, even though the reaper spares its slot. Storing
 * one record per slot keeps every in-flight run resumable.
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
 * field is only *probably* an array. Guarding each render site one at a time is
 * fragile — the same bug class recurs (`steps` arriving as `"1"`, `keep` /
 * `couldNotSee` / `rules` as non-arrays), and the failure is worse than a blank
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
  // Built field by field on purpose: a `...raw` spread lets any scalar the model
  // invented survive unchecked, which is how a field like `framework` or `note`
  // can stay un-normalised while screens and flows are guarded. Constructing the
  // object explicitly means an unvalidated field cannot reach a React child at all —
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
      // Every field the picker renders, not a subset: an object in `group` crashes
      // the route exactly like one in `label` would. `canSee`
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
 * A single persisted job record (JOBKEY) can only spare one slot, which is wrong
 * as soon as two critiques overlap — which the UI allows on purpose: `+ New`
 * deliberately does NOT cancel a running critique, it lets it finish into History.
 * Start A, start B, navigate away, and a single-slot reaper would delete A's slot
 * along with its result. This list tracks every in-flight slot so the reaper
 * spares all of them.
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
