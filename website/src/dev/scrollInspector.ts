/** Transcript scroll inspector -- an opt-in developer overlay that shows what the
 *  virtualizer is doing to the reader's scroll position, on the device where it
 *  is happening.
 *
 *  WHY THIS EXISTS. The transcript's positioning defects are only reproducible on
 *  a real phone, and the mechanisms that produce them are invisible in jsdom: the
 *  restore's settle loop reads live `getBoundingClientRect`, which is degenerate
 *  under a test renderer, so its whole body is structurally unreachable there.
 *  Four real defects were found by watching this overlay on a device and none of
 *  them could have been caught by a unit test -- an anchor key compared in the
 *  wrong vocabulary, a correction loop whose rAF was cancelled by the very
 *  re-render it triggered, a tolerance finer than the device pixel grid, and a
 *  loop aborting on the scroll events it caused itself. A phone has no console,
 *  so the readings have to be painted where the reader can photograph them.
 *
 *  OFF MEANS OFF. `enabled` is a module-level boolean read FIRST by every entry
 *  point, so a disabled inspector creates no element, arms no timer, allocates no
 *  strings, and retains nothing. The only residue is two idle event listeners
 *  registered once at module load, which is what makes the toggle take effect
 *  without a reload. Hot callers (a per-frame correction loop, a per-render
 *  counter) additionally guard their own argument construction with
 *  `inspectorOn()` so the disabled path does not even build the text.
 */

const ENABLED_KEY = 'mc-scroll-inspector'
const ENABLED_EVENT = 'mc-scroll-inspector-changed'
const POS_KEY = 'mc-scroll-inspector-pos'

/** Lines of event history kept on screen. Small on purpose: this is read from a
 *  phone screenshot, and a taller box covers the transcript it reports on. */
const MAX_LINES = 8
/** Live-reading cadence. Polled rather than driven by scroll events because the
 *  readings that matter most change while nobody is touching the screen -- a
 *  transcript growing under a still finger fires no scroll event at all. */
const TICK_MS = 250

let enabled = false
let host: HTMLDivElement | null = null
let liveEl: HTMLDivElement | null = null
let logEl: HTMLDivElement | null = null
let gripEl: HTMLDivElement | null = null
let ticker: number | null = null

const lines: string[] = []

/** STICKY readouts for the two decisions that answer "why did it open here", and
 *  that the 8-line window loses first because they happen at the START of a
 *  switch: whether the LEAVE saved or cleared the reading position (and the
 *  `stick`/at-bottom facts it decided on), and how the ENTRY resolved it.
 *
 *  Kept as one line each rather than by growing the log, because the log is read
 *  by aligning columns across lines and a taller box covers the transcript it is
 *  reporting on. A switch produces dozens of lines and these two are the ones a
 *  reader has to scroll back for -- which on a phone means they are gone. */
const STICKY_TAGS: Record<string, 'leave' | 'entry'> = {
  // Both branches, because which ONE ran is the question: `flush` decided from
  // `stick`/at-bottom whether to save or clear, while `skip` means the leave path
  // did neither -- a restore was still pending, so the outgoing position was
  // never recorded at all.
  'LEAVE.flush': 'leave',
  'LEAVE.skip': 'leave',
  'LEAVE': 'leave',
  'STORE.save': 'leave',
  'STORE.CLEAR': 'leave',
  'STORE.load': 'entry',
  'RESTORE.hold': 'entry',
  'RESTORE.OK': 'entry',
  'RESTORE.giveup': 'entry',
}
const sticky: { leave: string; entry: string } = { leave: '', entry: '' }
let watched: HTMLElement | null = null
let watchedRows = -1
let watchedMsgs = -1
let watchedTotal = -1

function readFlag(): boolean {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem(ENABLED_KEY) === '1'
  } catch {
    // Private mode / storage disabled: an inspector nobody can turn on is the
    // safe answer, never a crash on a path the product depends on.
    return false
  }
}

/** Whether the inspector is on. Callers in hot paths gate their own string
 *  building on this so a disabled inspector costs one boolean read. */
export function inspectorOn(): boolean {
  return enabled
}

// ---- position ----

function loadPos(): { x: number; y: number } {
  try {
    const raw = localStorage.getItem(POS_KEY)
    if (raw) {
      const p = JSON.parse(raw) as { x?: unknown; y?: unknown }
      if (typeof p.x === 'number' && typeof p.y === 'number') return { x: p.x, y: p.y }
    }
  } catch { /* fall through to the default corner */ }
  return { x: 4, y: 48 }
}

function clampPos(x: number, y: number): { x: number; y: number } {
  // Keep a grabbable strip on screen: a box dragged off the edge and persisted
  // there would be unrecoverable without clearing storage.
  const maxX = Math.max(0, window.innerWidth - 40)
  const maxY = Math.max(0, window.innerHeight - 24)
  return { x: Math.min(Math.max(0, x), maxX), y: Math.min(Math.max(0, y), maxY) }
}

function applyPos(x: number, y: number): void {
  if (!host) return
  const p = clampPos(x, y)
  host.style.left = `${p.x}px`
  host.style.top = `${p.y}px`
}

function savePos(): void {
  if (!host) return
  try {
    localStorage.setItem(POS_KEY, JSON.stringify({ x: parseFloat(host.style.left) || 0, y: parseFloat(host.style.top) || 0 }))
  } catch { /* position is a convenience, never worth throwing over */ }
}

// ---- DOM ----

function ensureHost(): HTMLDivElement | null {
  if (!enabled) return null
  if (typeof document === 'undefined' || !document.body) return null
  if (host && host.isConnected) return host

  host = document.createElement('div')
  host.setAttribute('data-scroll-inspector', '1')
  // The BOX ignores pointers so it can never swallow a tap meant for the
  // transcript underneath; only the grip below opts back in. An overlay that
  // eats touches is worse than no overlay on the surface it is inspecting.
  host.style.cssText = [
    'position:fixed',
    'z-index:2147483647',
    // 350px fits a full log line without wrapping -- the readings are compared
    // against each other across lines, and a wrapped line breaks that alignment.
    'width:350px',
    'max-width:96vw',
    'pointer-events:none',
    'font:9px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace',
    'background:rgba(0,0,0,.84)',
    'color:#4ade80',
    'padding:0 6px 4px',
    'border-radius:4px',
    'white-space:pre',
    'overflow:hidden',
    'user-select:none',
    'touch-action:none',
  ].join(';')

  gripEl = document.createElement('div')
  gripEl.setAttribute('aria-hidden', 'true')
  gripEl.style.cssText = [
    'pointer-events:auto',
    'cursor:grab',
    'height:14px',
    'margin:0 -6px 2px',
    'display:flex',
    'align-items:center',
    'justify-content:center',
    'color:rgba(255,255,255,.45)',
    'font-size:11px',
    'letter-spacing:2px',
  ].join(';')
  gripEl.textContent = '⋯'
  attachDrag(gripEl)

  liveEl = document.createElement('div')
  liveEl.style.cssText = 'color:#fde047;font-size:11px;font-weight:700;margin-bottom:2px'
  logEl = document.createElement('div')

  host.appendChild(gripEl)
  host.appendChild(liveEl)
  host.appendChild(logEl)
  document.body.appendChild(host)

  const p = loadPos()
  host.style.left = `${p.x}px`
  host.style.top = `${p.y}px`
  applyPos(p.x, p.y)
  paint()
  return host
}

function attachDrag(handle: HTMLElement): void {
  let dragging = false
  let startX = 0
  let startY = 0
  let originX = 0
  let originY = 0

  handle.addEventListener('pointerdown', (e: PointerEvent) => {
    if (!host) return
    dragging = true
    startX = e.clientX
    startY = e.clientY
    originX = parseFloat(host.style.left) || 0
    originY = parseFloat(host.style.top) || 0
    handle.style.cursor = 'grabbing'
    // Capture so the drag survives the pointer leaving the 14px grip -- without
    // it a quick flick drops the box after a few pixels.
    try { handle.setPointerCapture(e.pointerId) } catch { /* not fatal */ }
    e.preventDefault()
  })
  handle.addEventListener('pointermove', (e: PointerEvent) => {
    if (!dragging) return
    applyPos(originX + (e.clientX - startX), originY + (e.clientY - startY))
    e.preventDefault()
  })
  const end = () => {
    if (!dragging) return
    dragging = false
    handle.style.cursor = 'grab'
    savePos()
  }
  handle.addEventListener('pointerup', end)
  handle.addEventListener('pointercancel', end)
}

function paint(): void {
  if (!logEl) return
  logEl.textContent = lines.join('\n')
}

function teardown(): void {
  if (ticker !== null) {
    clearInterval(ticker)
    ticker = null
  }
  if (host && host.isConnected) host.remove()
  host = null
  liveEl = null
  logEl = null
  gripEl = null
  lines.length = 0
  sticky.leave = ''
  sticky.entry = ''
  watched = null
  watchedRows = -1
  watchedMsgs = -1
  watchedTotal = -1
}

// ---- public feed ----

/** Append one event line. Newest at the bottom; the buffer is a ring. */
export function devLog(tag: string, detail: string): void {
  if (!enabled) return
  const t = new Date()
  const ts =
    `${String(t.getMinutes()).padStart(2, '0')}:` +
    `${String(t.getSeconds()).padStart(2, '0')}.` +
    `${Math.floor(t.getMilliseconds() / 100)}`
  const slot = STICKY_TAGS[tag]
  if (slot) sticky[slot] = `${tag} ${detail}`
  lines.push(`${ts} ${tag} ${detail}`)
  while (lines.length > MAX_LINES) lines.shift()
  if (!ensureHost()) return
  paint()
}

/** Register the scroller whose geometry the live block reads, plus its row count. */
export function devWatchScroller(el: HTMLElement, rows?: number): void {
  if (!enabled) return
  watched = el
  if (typeof rows === 'number') watchedRows = rows
  if (!ensureHost()) return
  if (ticker === null && typeof window !== 'undefined') {
    ticker = window.setInterval(tick, TICK_MS)
  }
}

/** Loaded MESSAGE count and the server's total. Distinct from row count: a row
 *  groups a whole turn, so rows alone cannot say whether history is arriving. */
export function devWatchMessages(loaded: number, serverTotal: number): void {
  if (!enabled) return
  watchedMsgs = loaded
  watchedTotal = serverTotal
}

function tick(): void {
  const w = watched
  if (!w || !ensureHost() || !liveEl) return
  const dist = w.scrollHeight - w.clientHeight - w.scrollTop
  liveEl.textContent =
    `to-end ${Math.round(dist)}px  rows=${watchedRows}  msgs=${watchedMsgs}/${watchedTotal < 0 ? '?' : watchedTotal}` +
    `\ny=${Math.round(w.scrollTop)} h=${Math.round(w.scrollHeight)} v=${Math.round(w.clientHeight)}` +
    `  h/n=${watchedRows > 0 ? Math.round(w.scrollHeight / watchedRows) : '-'}` +
    (sticky.leave ? `\nLEFT  ${sticky.leave}` : '') +
    (sticky.entry ? `\nENTER ${sticky.entry}` : '')
}

/** A persisted anchor key shown so its VOCABULARY is legible: the stable row id
 *  carries an `a-` prefix, the per-render key does not, and printing only the
 *  tail hides exactly the part that tells them apart. */
export function keyShape(k: string): string {
  return `${k.slice(0, 2)}\u2026${k.slice(-6)}`
}

/** Last 4 chars of a session id -- enough to tell two tabs apart on screen. */
export function shortId(id: string | null | undefined): string {
  if (!id) return '-'
  return id.length <= 4 ? id : id.slice(-4)
}

// ---- gate ----

export function setInspectorEnabled(on: boolean): void {
  if (on === enabled) return
  enabled = on
  if (!on) teardown()
  // Turning it ON deliberately does not build the overlay here: it appears with
  // the first reading, so an enabled inspector on a surface that reports nothing
  // stays invisible instead of hanging an empty box over the page.
}

if (typeof window !== 'undefined') {
  enabled = readFlag()
  window.addEventListener(ENABLED_EVENT, (e) => {
    const detail = (e as CustomEvent<unknown>).detail
    setInspectorEnabled(typeof detail === 'boolean' ? detail : readFlag())
  })
  // Another tab toggling it should not leave this one running.
  window.addEventListener('storage', (e) => {
    if (e.key === ENABLED_KEY) setInspectorEnabled(readFlag())
  })
}

export const INSPECTOR_KEYS = { ENABLED_KEY, ENABLED_EVENT, POS_KEY } as const
