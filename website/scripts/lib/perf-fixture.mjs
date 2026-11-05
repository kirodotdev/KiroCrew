/**
 * Synthetic world for the render benchmark (`scripts/bench-render.mjs`).
 *
 * A seeded, gateway-free stand-in for a busy dashboard: 222 sidebar sessions
 * across 8 folders, a long transcript the user has open, and generators for the
 * WebSocket frames a streaming turn produces. Frame shapes follow the gateway's
 * own (`dashboard/chat_runner.py`, read by `store/chat/wire.ts`), so the client
 * runs its real reducers, selectors and components on them.
 *
 * Everything is seeded, so two runs of one scenario see the same slots, the same
 * transcript and the same frame sequence. That is what lets the benchmark gate
 * on counts rather than on wall time.
 */

/** Seeded PRNG (mulberry32). Returns a float in [0, 1). */
export function rng(seed) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const WORDS = ('gateway slot reducer selector virtuoso sidebar websocket frame chunk render commit '
  + 'heap snapshot trace profile session transcript folder cron monitor worker review '
  + 'build deploy pipeline cache memo layout paint scroll index window bucket').split(' ')

/** Slot keys the scenarios stream into. */
export const ACTIVE_LONG = 'perf-long'
export const ACTIVE_STREAM = 'perf-stream'
export const fillerKey = (i) => `perf-s${String(i).padStart(3, '0')}`

/**
 * Build the sidebar and transcripts.
 * @param {{ sessions?: number, longRows?: number, seed?: number }} opts
 *   `sessions` is the total sidebar count, the two perf slots included.
 */
export function buildWorld({ sessions = 222, longRows = 100, seed = 1018 } = {}) {
  const r = rng(seed)
  const pick = (xs) => xs[Math.floor(r() * xs.length)]
  const int = (lo, hi) => lo + Math.floor(r() * (hi - lo + 1))
  const sentence = (n = 12) => {
    const s = Array.from({ length: n }, () => pick(WORDS)).join(' ')
    return s[0].toUpperCase() + s.slice(1) + '.'
  }
  const markdownBlock = () => {
    const parts = [`## ${sentence(4).slice(0, -1)}`, sentence(18), sentence(14)]
    if (r() < 0.6) parts.push(`- ${sentence(8)}\n- ${sentence(9)}\n- ${sentence(7)}`)
    if (r() < 0.5) {
      const lang = pick(['python', 'ts', 'bash'])
      const lines = Array.from({ length: int(4, 14) }, (_, j) => `    value_${j} = compute(${int(0, 999)})  # ${pick(WORDS)}`)
      parts.push('```' + lang + '\ndef handler(event):\n' + lines.join('\n') + '\n    return value_0\n```')
    }
    if (r() < 0.2) {
      parts.push('| col | value |\n|---|---|\n' + Array.from({ length: 5 }, () => `| ${pick(WORDS)} | ${int(0, 99)} |`).join('\n'))
    }
    return parts.join('\n\n')
  }

  // Ages are relative to the real clock: the sidebar folds sessions idle for
  // longer than its sleeping threshold, so a fixed date would hide them all.
  // The offsets are seeded, so every run gets the same sidebar.
  const now = Date.now()
  let mid = 0
  let ts = now - 2 * 86400_000
  const row = (role, content, meta = {}) => {
    ts += int(1, 30) * 1000
    return {
      role, content, ts: new Date(ts).toISOString(), cls: `msg msg-${role}`,
      meta: { mid: `m-${(++mid).toString(16).padStart(16, '0')}`, ...meta },
    }
  }
  const toolRow = () => {
    const kind = pick(['execute', 'execute', 'execute', 'edit', 'read', 'search'])
    const label = { execute: '🔧 Running', edit: '🔧 Editing', read: '🔧 Reading', search: '🔧 Searching for' }[kind]
    const output = Array.from({ length: int(5, 30) }, () => `${pick(WORDS)} ${int(0, 99999)} ${sentence(6)}`).join('\n')
    return row('tool', `${label} ${sentence(5).slice(0, -1)}`, {
      tool_call_id: `call_${mid.toString(16).padStart(8, '0')}`, purpose: sentence(6),
      input: JSON.stringify({ command: `ls -la ${pick(WORDS)}` }), kind, done: true, output,
    })
  }

  // The open transcript. The client loads one 100-row page of a long session on
  // open, so `longRows` beyond 100 only matters with `has_more` paging.
  const long = []
  while (long.length < longRows) {
    long.push(row('user', sentence(int(6, 30))))
    for (let k = int(1, 6); k > 0; k--) long.push(toolRow())
    long.push(row('assistant', markdownBlock()))
  }
  long.length = longRows
  const stream = [row('user', 'Start the background stream test.'), row('assistant', markdownBlock())]

  const folders = ['Infra', 'Reviews', 'Research', 'Oncall', 'Docs', 'Perf', 'Fleet', 'Misc']
    .map((name, i) => ({ id: `f${i.toString(16).padStart(11, '0')}`, name, order: i, collapsed: false, parent_id: '' }))
  const perfFolder = folders[5].id
  const slot = (key, title, messages, extra = {}) => ({
    key, title, running: false, messages, agent: 'kirocrew', model: 'auto',
    last_ts: new Date(now - int(10, 60 * 24 * 14) * 60_000).toISOString(), ...extra,
  })
  const slots = [
    slot(ACTIVE_LONG, 'Perf A: long transcript', longRows, { folder_id: perfFolder, pinned: true }),
    slot(ACTIVE_STREAM, 'Perf B: background stream', stream.length, { folder_id: perfFolder, pinned: true }),
  ]
  // Offscreen sidebar rows do not re-render on their slot's status, so where
  // the streaming slots sit matters. perf-s000 is pinned in the first folder,
  // on screen at 1440x900; perf-stream and perf-s001..003 are below the fold.
  // That is the layout the pod home had when the baseline was measured.
  for (let i = 0; slots.length < sessions; i++) {
    const drawn = r() < 0.6 ? pick(folders).id : ''
    const folder = i === 0 ? folders[0].id : drawn
    slots.push(slot(fillerKey(i), `${sentence(int(2, 6)).slice(0, -1)} #${i}`, int(4, 10), {
      ...(folder ? { folder_id: folder } : {}), pinned: i < 10,
    }))
  }

  const transcripts = { [ACTIVE_LONG]: long, [ACTIVE_STREAM]: stream }
  /** The `GET /api/chat/slots/{key}` body, one page of the newest rows. */
  const detail = (key) => {
    const rows = transcripts[key] || []
    const page = rows.slice(-100)
    return {
      key, running: false, stopping: false, messages: page, queue: [],
      total: rows.length, has_more: rows.length > page.length, next_before: rows.length - page.length,
    }
  }
  // The command bar is a default-on builtin app. While it owns the quick-search
  // slot the closed CommandPalette is not mounted, so leaving it out would bill
  // every scenario for a palette a default install never renders.
  const apps = [{
    name: 'command-bar', enabled: true, origin: 'builtin',
    manifest: { ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] } },
  }]
  return { slots, folders, detail, apps }
}

const frame = (type, data) => JSON.stringify({ type, data })
const iso = (t) => new Date(t).toISOString()

/**
 * A background agentic turn, one generator per slot, stepped every 100 ms.
 * Per slot per second: 10 `chat_chunk`, 2 `chat_status` with a new label each,
 * one `tool_call` with its `tool` row, one 900-char `tool_result`, and every
 * 5 s a `chat_segment` plus an assistant row.
 */
export function* agenticTurn(slot, k, clock) {
  let seq = 10000 * (k + 1)
  let tick = 0
  let mid = 0
  const m = () => `m-perf-${k}-${++mid}`
  yield [frame('chat_message', { slot, role: 'user', content: 'Run the long build and summarize.', ts: iso(clock()), cls: 'msg msg-user', meta: { mid: m() } })]
  while (true) {
    tick++
    const out = []
    const text = Array.from({ length: 3 + (tick % 4) }, (_, i) => WORDS[(tick * 7 + i + k) % WORDS.length]).join(' ') + ' '
    out.push(frame('chat_chunk', { slot, content: text, seq: ++seq, gen: 'perfgen' }))
    if (tick % 5 === 0) out.push(frame('chat_status', { slot, status: `Working step ${tick}` }))
    if (tick % 10 === 3) {
      const id = `call_perf_${k}_${tick}`
      out.push(frame('tool_call', { slot, tool: `Running make test ${tick}`, kind: 'execute', purpose: 'Run tests', input_preview: 'make test', tool_call_id: id, is_shell: true }))
      out.push(frame('chat_message', { slot, role: 'tool', content: `🔧 Running make test ${tick}`, ts: iso(clock()), cls: 'msg msg-tool', meta: { mid: m(), tool_call_id: id, kind: 'execute', input: '{"command":"make test"}' } }))
    }
    if (tick % 10 === 8) {
      out.push(frame('tool_result', { slot, output: 'ok '.repeat(300), tool_call_id: `call_perf_${k}_${tick - 5}`, final: true }))
    }
    if (tick % 50 === 49) {
      out.push(frame('chat_segment', { slot }))
      out.push(frame('chat_message', { slot, role: 'assistant', content: 'Step done. ' + text.repeat(20), ts: iso(clock()), cls: 'msg msg-assistant', meta: { mid: m() } }))
    }
    yield out
  }
}

/**
 * The open slot streams a prose answer: 3 chunks per 100 ms, about 540
 * chars/s, with a paragraph break every ~400 chars so the markdown has blocks.
 */
export function* proseAnswer(slot, clock) {
  let seq = 90000
  let chars = 0
  let tick = 0
  yield [frame('chat_message', { slot, role: 'user', content: 'Write a long explanation.', ts: iso(clock()), cls: 'msg msg-user', meta: { mid: 'm-perf-prose-1' } })]
  while (true) {
    tick++
    const out = []
    for (let j = 0; j < 3; j++) {
      let text = Array.from({ length: 3 }, (_, i) => WORDS[(tick * 5 + j * 3 + i) % WORDS.length]).join(' ') + (j === 2 && tick % 3 === 0 ? '. ' : ' ')
      chars += text.length
      if (chars > 400) { text += '\n\n'; chars = 0 }
      out.push(frame('chat_chunk', { slot, content: text, seq: ++seq, gen: 'perfgen' }))
    }
    yield out
  }
}

/** The frame that ends a streaming turn. */
export const doneFrame = (slot) => frame('chat_done', { slot, ends_turn: true })
