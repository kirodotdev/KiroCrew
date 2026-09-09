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
  // Addressable so a width guard can measure THIS block. Reading the host's
  // textContent instead joins the last live line to the first log line with no
  // separator, which reads as one over-long line that does not exist.
  liveEl.setAttribute('data-scroll-inspector-live', '')
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
  corr = null
  watched = null
  watchedRows = -1
  watchedMsgs = -1
  watchedTotal = -1
}

// ---- public feed ----

/** Durable tally of who loaded older pages, keyed by the producer's own label.
 *
 *  The event log is a short ring, so by the time a reader notices the loaded
 *  count has run away, the lines naming WHICH producer did it have already
 *  scrolled off — leaving a screenshot that proves the count and nothing about
 *  its cause. A count survives the ring, and the to-top reading AT EACH FIRE is
 *  what tells the producers apart: the near-top walk can only fire within a few
 *  viewports of the head, while the pinned/deep-link jump pages from anywhere,
 *  so a large `max` is by itself an attribution. */
const olderTally = new Map<string, { n: number; lastVp: number; maxVp: number }>()

/** Older-page latency, split into the two halves that have different owners.
 *
 *  "Loading is too slow" is unactionable until it says WHICH half. `fetch` is
 *  the request — the backend's work plus the wire. `paint` is everything after
 *  the data is in the store until the page is really on screen: reducer,
 *  regroup, render, and the measurement that replaces estimated heights with
 *  real ones. The reader experiences the end of the SECOND one, so a single
 *  round-trip number can hide an 11s wait behind a 200ms paint, or the reverse.
 *
 *  Kept as last + worst rather than a log line: the event ring holds ~8 lines,
 *  so after a long scroll the numbers that mattered have scrolled off, which is
 *  exactly how the first runaway lost its own evidence. */
let olderLat: { fetchMs: number; paintMs: number; worstMs: number; n: number } | null = null
/** Set by the paging thunk. Reports the REQUEST only: the deliberate landing hold
 *  it used to be split against is gone, so there is no second span to tell it
 *  apart from. Reinstating a hold means re-adding a second number here, which is
 *  a visible edit rather than a constant nobody notices. */
let olderSpans: { netMs: number } | null = null

export function devOlderSpans(netMs: number): void {
  if (!enabled) return
  olderSpans = { netMs }
  if (ensureHost()) paint()
}

export function devOlderLatency(fetchMs: number, paintMs: number): void {
  if (!enabled) return
  const total = fetchMs + paintMs
  const prev = olderLat
  olderLat = {
    fetchMs,
    paintMs,
    worstMs: Math.max(total, prev ? prev.worstMs : 0),
    n: (prev ? prev.n : 0) + 1,
  }
  if (ensureHost()) paint()
}

/** Programmatic scroll movement, accumulated per older-page landing.
 *
 *  This is the reader's complaint measured directly. The bounce is NOT the change
 *  in scrollTop across a landing -- that conflates the finger with the machine --
 *  it is the sum of the writes the app itself performed. Every one is already
 *  logged as `WRITE <who> <from>-><to>`, but the event ring holds ~8 lines, so
 *  after three landings in one scroll the deltas that mattered have scrolled off.
 *
 *  Summed as ABSOLUTE values: two compensations of +400 and -400 are two visible
 *  jolts, not a quiet zero, and a net figure would report them as perfect. */
let moved: { px: number; n: number; worstPx: number; writers: string } | null = null

/** Re-pricing of the content ABOVE the reader, accumulated per landing.
 *
 *  The top spacer's height is the offset tree's answer for everything above the
 *  mounted window. It changes for two unrelated reasons, and only one of them is
 *  a defect: the window moving (the reader scrolled -- expected), and the tree
 *  REPRICING rows it had never measured (a row first mounts, its real height
 *  replaces the running mean, and everything below it shifts).
 *
 *  Isolated by accumulating only across renders where the window START did not
 *  move. What remains is displacement nobody asked for -- and comparing it with
 *  `moved` says whether it was compensated: repriced with no matching write is
 *  exactly the bounce the reader feels while scrolling through a region that
 *  has just loaded. */
let repriced: { px: number; n: number; worstPx: number } | null = null
let lastSpacer: { px: number; start: number } | null = null

function noteSpacer(spacerPx: number, start: number): void {
  const prev = lastSpacer
  lastSpacer = { px: spacerPx, start }
  // Recorded ahead of every other rule, because every other rule excludes it.
  if (landing && landing.samples < LANDING_SAMPLES) {
    if (landing.from === null) landing.from = prev ? prev.px : spacerPx
    landing.to = spacerPx
    landing.samples += 1
  }
  if (!prev || prev.start !== start) return
  const d = Math.abs(spacerPx - prev.px)
  if (!Number.isFinite(d) || d < 0.5) return
  repriced = {
    px: (repriced ? repriced.px : 0) + d,
    n: (repriced ? repriced.n : 0) + 1,
    worstPx: Math.max(d, repriced ? repriced.worstPx : 0),
  }
}

/** Report the top spacer so a reprice above the reader can be told from the
 *  reader simply having scrolled. Cheap by design: two numbers, no reads. */
export function devSpacer(spacerPx: number, windowStart: number): void {
  if (!enabled) return
  noteSpacer(spacerPx, windowStart)
}

/** Scroll movement that NO logged write accounts for.
 *
 *  The reason this exists: an anchor correction of 13,202px was measured on the
 *  device while the transcript had grown 131px and the only logged write was
 *  -64px. Solving the anchor identity `top = C - S` for that landing gives
 *  S1 - S0 = -13,071 -- something moved the reader 13k px between the anchor's
 *  capture and its consume, through a path that emits no `WRITE` line. The
 *  correction then faithfully compensated a movement nobody can see in the log.
 *
 *  Candidates it has to tell apart: a browser CLAMP (content briefly shorter
 *  than `scrollTop`, so the engine silently pulls it down to
 *  `scrollHeight - clientHeight`), a `scrollIntoView` somewhere, or an
 *  assignment that bypasses the write chokepoint. iOS WebKit has no scroll
 *  anchoring, so that one is already excluded.
 *
 *  A finger is excluded by MAGNITUDE, not by asking whether input happened:
 *  even iOS momentum does not deliver a kilopixel inside one scroll event on a
 *  ~600px viewport, and a threshold cannot be fooled by a gesture that never
 *  stamped an input flag. */
const UNOWNED_MIN_PX = 800
let lastSampleTop: number | null = null
let lastWrittenTo: number | null = null
let unowned: { maxPx: number; n: number; clamps: number } | null = null

/** Report the scroller's position on every scroll event, with the range limit so
 *  a CLAMP can be told from a JUMP without a second reading.
 *
 *  The engine pulls `scrollTop` down to `scrollHeight - clientHeight` whenever
 *  the content becomes shorter than the current position, and it does so
 *  silently -- no event names it, nothing logs it. A displacement that lands
 *  exactly ON that limit is therefore a clamp; one that lands anywhere else was
 *  somebody's deliberate write. The two need opposite fixes, so guessing between
 *  them would waste the reading. */
export function devScrollTop(top: number, maxTop?: number): void {
  if (!enabled) return
  const prev = lastSampleTop
  lastSampleTop = top
  if (prev === null || !Number.isFinite(top)) return
  const d = Math.abs(top - prev)
  if (d < UNOWNED_MIN_PX) return
  // Ours if the position landed where our own last write asked it to. Compared
  // against the write's TARGET rather than its delta: a clamp can truncate our
  // write, and that truncation is exactly the movement being hunted.
  if (lastWrittenTo !== null && Math.abs(top - lastWrittenTo) <= 1) return
  const clamped = typeof maxTop === 'number' && Number.isFinite(maxTop) && Math.abs(top - maxTop) <= 1
  unowned = {
    maxPx: Math.max(d, unowned ? unowned.maxPx : 0),
    n: (unowned ? unowned.n : 0) + 1,
    clamps: (unowned ? unowned.clamps : 0) + (clamped ? 1 : 0),
  }
}

function noteWrite(detail: string): void {
  // `<who> <from>-><to>` (a trailing ' smooth' is possible and ignored).
  const m = /^(\S+)\s+(-?\d+)->(-?\d+)/.exec(detail)
  if (!m) return
  // A CORRECTION opens its own measurement window when no landing has opened one.
  //
  // The anchor correction fires without the message count changing -- a restore, a
  // regroup and a turn-end rebuild all reach it -- while the window used to arm
  // only on the count RISING. So a correction outside a landing was measured
  // against the PREVIOUS landing's spacer samples, and the overlay reported
  // `spacer 0->0` for an event it had never sampled, making a possibly-legitimate
  // compensation read as pure error. Third time a window-alignment mistake has
  // turned a reading into an artifact tonight, and the same shape each time: the
  // counter and the thing it measures opened on different events.
  //
  // Arming here makes `from` the spacer as it stood before this write and `to` the
  // next sample after it.
  if (!landing && m[1] === 'resize') {
    landing = { from: lastSpacer ? lastSpacer.px : null, to: null, samples: 0 }
  }
  lastWrittenTo = Number(m[3])
  const d = Math.abs(Number(m[3]) - Number(m[2]))
  if (!Number.isFinite(d) || d === 0) return
  const prev = moved
  const writers = prev && prev.writers.includes(m[1]) ? prev.writers : `${prev ? prev.writers + ',' : ''}${m[1]}`
  moved = {
    px: (prev ? prev.px : 0) + d,
    n: (prev ? prev.n : 0) + 1,
    worstPx: Math.max(d, prev ? prev.worstPx : 0),
    writers,
  }
}

function noteOlderProducer(detail: string): void {
  // A landing STARTS a fresh accounting period, so the figure on screen is
  // "movement caused by the page that just landed" rather than a session total.
  // Deliberately NOT reset here: a fetch STARTING is not a landing, and resetting
  // on it opened the window earlier than the spacer's, which is what made RESIDUAL
  // compare two different landings. The reset lives in devWatchMessages.
  // The first token is the producer: `walk p3`, `jump p12`, `sentinel`,
  // `manual-bar`, `error-retry`. Tallying here rather than at the five dispatch
  // sites means a producer added later is counted without being remembered.
  const who = detail.split(' ')[0] || '?'
  const vps = watched && watched.clientHeight > 0 ? watched.scrollTop / watched.clientHeight : 0
  const t = olderTally.get(who) ?? { n: 0, lastVp: 0, maxVp: 0 }
  t.n += 1
  t.lastVp = vps
  if (vps > t.maxVp) t.maxVp = vps
  olderTally.set(who, t)
}

/** The compensation's OWN accounting, kept sticky.
 *
 *  There were two residuals on this overlay and they disagreed on the same landing:
 *  a spacer-derived one printed 1579px while the compensation printed `res=0`. The
 *  compensation was right. `spacer` is not "content above the reader" -- once
 *  `windowRange.start === 0`, which is the state every top-walk ends in, prepended
 *  rows MOUNT above the anchor instead of growing the spacer, so the spacer
 *  undercounts by exactly the mounted growth and every residual measured against it
 *  is inflated by that amount. 1579px was the mounted rows, not a defect.
 *
 *  So the sticky figure now reads the same numbers the corrector itself computed
 *  from resolved row positions (`CORR d= owed= res=`), and the spacer keeps only
 *  the job it can actually do: describing what the spacer did.
 *
 *  Worst is kept alongside last because a single bad landing in a run of good ones
 *  is the whole complaint, and a last-only reading hides it behind the next landing.
 */
let corr: { n: number; lastRes: number; worstRes: number; lastOwed: number } | null = null
function noteCorr(detail: string): void {
  const m = /d=(-?\d+) owed=(-?\d+) res=(-?\d+)/.exec(detail)
  if (!m) return
  const owed = Number(m[2])
  const res = Number(m[3])
  // A landing that asked for nothing is not a landing; counting it would dilute
  // the run and make a genuine residual look rarer than it is.
  if (owed === 0 && res === 0) return
  corr = {
    n: (corr ? corr.n : 0) + 1,
    lastRes: res,
    worstRes: Math.max(corr ? corr.worstRes : 0, Math.abs(res)),
    lastOwed: owed,
  }
}

/** Append one event line. Newest at the bottom; the buffer is a ring. */
export function devLog(tag: string, detail: string): void {
  if (!enabled) return
  if (tag === 'OLDER') noteOlderProducer(detail)
  if (tag === 'WRITE') noteWrite(detail)
  if (tag === 'CORR') noteCorr(detail)
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
/** The top spacer across a LANDING, which is the one window the other two
 *  counters are blind to -- and the blindness was designed in, so it is named
 *  here rather than quietly fixed.
 *
 *  `repriced` deliberately skips any render where the window START moved, to
 *  separate a reprice from the reader scrolling. A landing moves START (the
 *  window is re-based for the prepended rows), so the very event under
 *  investigation was excluded by that rule. `UNOWNED` samples on scroll events,
 *  and a same-frame clamp fires none.
 *
 *  Armed by the message count RISING -- the landing itself, not the fetch start
 *  -- and closed after a few renders, so ordinary scrolling never enters. What
 *  it should show if the rebase lands a frame late: `offsetBefore` collapsing by
 *  about one page of rows, which is what the engine then clamps `scrollTop` by. */
const LANDING_SAMPLES = 6
let landing: { from: number | null; to: number | null; samples: number } | null = null

export function devWatchMessages(loaded: number, serverTotal: number): void {
  if (!enabled) return
  if (loaded > watchedMsgs && watchedMsgs > 0) {
    // ONE reset point for every per-landing counter. They used to reset on two
    // different events -- `moved` on the fetch STARTING, the spacer window on the
    // payload ARRIVING -- and with several landings per scroll the two windows did
    // not cover the same landing. RESIDUAL then subtracted one landing's owed
    // movement from another's writes and read as a defect of ~8,000px that was
    // really two legitimate corrections measured against one page of growth.
    landing = { from: lastSpacer ? lastSpacer.px : null, to: null, samples: 0 }
    moved = null
    repriced = null
    unowned = null
  }
  watchedMsgs = loaded
  watchedTotal = serverTotal
}

/** Does the browser ACTUALLY hold scroll position when content is inserted above
 *  the viewport?
 *
 *  Measured, not asked. Reading `'overflowAnchor' in style` and treating the
 *  answer as the capability is a mistake this transcript already paid for: WebKit
 *  has landed the property, so it answers yes on a device whose anchoring does not
 *  hold a virtualized list, and a gate keyed to it switched itself off exactly
 *  where it was needed. WebKit's own tracker said the same thing about an earlier
 *  round -- the "supported" listing "is misleading, it is currently not
 *  implemented".
 *
 *  So the overlay reports BOTH, side by side, and their disagreement is the datum:
 *  `sa` is what the browser CLAIMS, `hold` is what it DOES.
 *
 *  The probe builds a real off-screen scroller, parks it away from 0 (anchoring is
 *  suppressed at the top edge), inserts a known height above the parked position,
 *  forces layout, and reads `scrollTop` back. A browser that anchors has moved it
 *  by the inserted height; one that does not has left it alone. Nothing is
 *  estimated -- the inserted height is what we wrote, and the answer is a readback.
 */
function probeAnchorHold(): string {
  if (typeof document === 'undefined' || !document.body) return '?'
  let box: HTMLDivElement | null = null
  try {
    box = document.createElement('div')
    box.setAttribute('aria-hidden', 'true')
    box.style.cssText =
      'position:fixed;left:-9999px;top:0;width:80px;height:100px;overflow-y:scroll'
    const head = document.createElement('div')
    head.style.height = '400px'
    const tail = document.createElement('div')
    tail.style.height = '400px'
    box.append(head, tail)
    document.body.appendChild(box)
    box.scrollTop = 200
    const before = box.scrollTop
    if (before < 100) return '?'
    const grow = document.createElement('div')
    grow.style.height = '300px'
    box.insertBefore(grow, head)
    void box.scrollHeight
    const moved = box.scrollTop - before
    // Reported as the measured delta rather than a bare yes/no, so a PARTIAL
    // implementation is visible instead of being rounded into one of two verdicts.
    return `${moved >= 250 ? 'yes' : 'no'}${moved !== 0 && moved < 250 ? `(${Math.round(moved)})` : ''}`
  } catch {
    return '?'
  } finally {
    box?.remove()
  }
}

/** Which bundle is on the device.
 *
 *  On the overlay because a hot-swapped `dist` looks identical from the outside:
 *  more than one reading tonight was taken against a build that was not the one
 *  being reasoned about, and there was no way to tell from the screen. */
function bundleTag(): string {
  if (typeof document === 'undefined') return '?'
  const src = Array.from(document.querySelectorAll('script[src]'))
    .map((e) => (e as HTMLScriptElement).src)
    .find((u) => /\/assets\/main-/.test(u))
  return src ? (/main-([A-Za-z0-9_-]+)\.js/.exec(src)?.[1] ?? '?') : '?'
}

/** The environment the readings below were taken in, computed once.
 *
 *  First line of the tool on purpose. Every wrong conclusion tonight came from
 *  reasoning about a platform instead of reading it, so the platform is now on the
 *  screen next to the numbers it explains. */
let envMemo: string | null = null
function envLine(): string {
  if (envMemo !== null) return envMemo
  const sa =
    typeof document !== 'undefined'
    && !!document.documentElement
    && 'overflowAnchor' in document.documentElement.style
  const vv = typeof window !== 'undefined' ? window.visualViewport : null
  const dpr = typeof window !== 'undefined' ? window.devicePixelRatio : 0
  envMemo =
    `env sa=${sa ? 'yes' : 'no'} hold=${probeAnchorHold()}`
    + ` ${vv ? `${Math.round(vv.width)}x${Math.round(vv.height)}` : '?'}@${dpr || '?'}`
    + ` b=${bundleTag()}`
  return envMemo
}

function tick(): void {
  const w = watched
  if (!w || !ensureHost() || !liveEl) return
  const dist = w.scrollHeight - w.clientHeight - w.scrollTop
  // Distance to the TOP, in the same unit the older-history trigger spends:
  // viewport heights. A raw pixel count cannot be compared against the threshold
  // by eye on a device whose viewport is whatever the browser chrome left over.
  const vps = w.clientHeight > 0 ? w.scrollTop / w.clientHeight : 0
  liveEl.textContent =
    envLine() +
    `\nto-end ${Math.round(dist)}px  rows=${watchedRows}  msgs=${watchedMsgs}/${watchedTotal < 0 ? '?' : watchedTotal}` +
    `\nto-top ${Math.round(w.scrollTop)}px = ${vps.toFixed(1)}vp` +
    `\ny=${Math.round(w.scrollTop)} h=${Math.round(w.scrollHeight)} v=${Math.round(w.clientHeight)}` +
    `  h/n=${watchedRows > 0 ? Math.round(w.scrollHeight / watchedRows) : '-'}` +
    // One line per producer that has actually fired, so an idle session stays
    // compact and a runaway names itself.
    Array.from(olderTally.entries())
      .map(([who, t]) => `\nolder ${who}=${t.n} last=${t.lastVp.toFixed(0)}vp max=${t.maxVp.toFixed(0)}vp`)
      .join('') +
    (olderLat
      ? `\nlat n=${olderLat.n} fetch=${(olderLat.fetchMs / 1000).toFixed(1)}s` +
        ` paint=${(olderLat.paintMs / 1000).toFixed(1)}s` +
        ` worst=${(olderLat.worstMs / 1000).toFixed(1)}s`
      : '') +
    (olderSpans ? `\n  net=${(olderSpans.netMs / 1000).toFixed(2)}s` : '') +
    (moved
      ? `\nmoved ${Math.round(moved.px)}px in ${moved.n} write(s)` +
        ` worst=${Math.round(moved.worstPx)}px [${moved.writers}]`
      : '') +
    (repriced
      ? `\nrepriced ${Math.round(repriced.px)}px in ${repriced.n}` +
        ` worst=${Math.round(repriced.worstPx)}px`
      : '') +
    (unowned
      ? `\nUNOWNED ${Math.round(unowned.maxPx)}px n=${unowned.n}` +
        ` ${unowned.clamps > 0 ? `clamp=${unowned.clamps}` : 'jump'}`
      : '') +
    (landing && landing.from !== null && landing.to !== null
      ? `\nspacer ${Math.round(landing.from)}->${Math.round(landing.to)}` +
        ` (${landing.to - landing.from >= 0 ? '+' : ''}${Math.round(landing.to - landing.from)}px)`
      : '') +
    // RESIDUAL -- the defect, separated from the work.
    //
    // `moved` is NOT the bounce. When a page lands above the reader the content
    // above them genuinely grows, and scrolling by exactly that much is what
    // keeps them still: most of `moved` is REQUIRED.
    //
    // Sourced from the corrector's own `CORR d= owed= res=`, which resolves row
    // positions, NOT from the spacer. The frame that settled it: `spacer 0->0`
    // while 8,360px of content arrived and the corrector reported `res=0 off=0
    // painted=0` -- so the spacer formula would have printed 8360px of defect on a
    // pixel-perfect landing. Prepended rows MOUNT above the anchor rather than
    // growing the spacer once the window reaches the start, so the spacer can miss
    // the growth entirely and the subtraction inherits all of it as fake residual.
    //
    // The run figures say `since load` OUT LOUD because `moved` on the line above
    // resets at each landing and these do not. Two adjacent numbers on different
    // windows is the same mistake that made an earlier residual unreadable; the
    // scope belongs in the text, not in the reader's memory.
    // The run figures go on their own indented continuation line, the same shape
    // `lat` uses. On one line the overlay clipped them at the device's width and the
    // part it cut was `since load` -- the scope label, which exists precisely to stop
    // the misreading, invisible on the only screen that matters.
    (corr
      ? `\nRESIDUAL ${corr.lastRes}px of ${corr.lastOwed}px owed` +
        `\n  worst ${corr.worstRes}px in ${corr.n} corr since load`
      : '') +
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
