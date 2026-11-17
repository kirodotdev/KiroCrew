/**
 * Keeps reads from using every connection the browser has to the gateway.
 *
 * A browser opens at most six HTTP/1.1 connections to one origin, and a request
 * past that waits in the browser's own queue. The dashboard's re-reads (worker
 * lists, the board, the roster, the agent catalog) are ordinary requests on that
 * origin, and so is a send. When the gateway answers slowly the re-reads hold all
 * six, and the send waits behind them until the composer's own timer gives up on
 * a message that never left the browser.
 *
 * So a read waits here, in order, once `READ_GATE_WIDTH` reads to its origin are
 * out. Writes never wait here. A read holds its place until its answer's body has
 * arrived, because the connection is busy until then.
 *
 * A read a person is waiting on goes to the front of the line. One caller uses
 * it: opening a chat reads that chat's page inside `asFrontRead`. Opening the
 * dashboard queues about thirty reads at once, and the chat named in the URL is
 * read only once the session list has answered, so in line order its page would
 * go out behind the fifteen or so reads still waiting by then. A front read
 * still waits for a place, so the reads that are out never exceed
 * `READ_GATE_WIDTH` and a send keeps its free connection.
 *
 * The session list itself stays in the ordinary line on purpose. The sidebar
 * asks for its folders a moment before the list; sent first, the list answers
 * before the folders and the sidebar paints a flat list, then regroups it.
 *
 * Counts this document only: a second browser tab on the same gateway has its
 * own count.
 */
export const READ_GATE_WIDTH = 4

interface Gate {
  out: number
  /** Reads a person is waiting on, in the order they arrived. Served first. */
  front: Array<() => void>
  waiting: Array<() => void>
}

const gates = new Map<string, Gate>()
/** How many `asFrontRead` calls are on the stack right now. */
let frontDepth = 0

/** Test seam: forget every read that is out or waiting. */
export function __resetReadGateForTests(): void {
  gates.clear()
  frontDepth = 0
}

/**
 * Run *read*, and put every gated read it starts before it returns at the front
 * of the line. For a read a person is waiting on. Only the reads started inside
 * the call count: one that *read* starts later, after an `await`, waits in the
 * ordinary line.
 */
export function asFrontRead<T>(read: () => T): T {
  frontDepth += 1
  try {
    return read()
  } finally {
    frontDepth -= 1
  }
}

function gateFor(input: string): Gate {
  let origin = ''
  try {
    origin = new URL(input, globalThis.location?.href).origin
  } catch {
    // An address that does not parse shares the page's own line.
  }
  let gate = gates.get(origin)
  if (!gate) {
    gate = { out: 0, front: [], waiting: [] }
    gates.set(origin, gate)
  }
  return gate
}

function release(gate: Gate): void {
  const next = gate.front.shift() ?? gate.waiting.shift()
  // The place passes straight to the next read in line, so `out` does not move.
  if (next) next()
  else gate.out -= 1
}

/** Resolves once *response*'s body has arrived (or failed, or there is none). */
async function bodyArrived(response: Response): Promise<void> {
  if (typeof response?.clone !== 'function' || !response.body || response.bodyUsed) return
  try {
    const reader = response.clone().body?.getReader()
    if (!reader) return
    while (!(await reader.read()).done) { /* discard: the caller reads its own copy */ }
  } catch {
    // A body that fails to arrive has stopped using its connection too.
  }
}

/**
 * Run *start* (the read) now when fewer than `READ_GATE_WIDTH` reads to its origin
 * are out, otherwise when one of them finishes. A read whose *signal* aborts while
 * it waits rejects with the signal's reason and is never started. A read started
 * inside `asFrontRead` waits ahead of the ordinary ones, behind the front reads
 * already waiting.
 */
export function gatedRead(input: string, signal: AbortSignal | null | undefined, start: () => Promise<Response>): Promise<Response> {
  const gate = gateFor(input)
  const line = frontDepth > 0 ? gate.front : gate.waiting
  const run = (): Promise<Response> => {
    let request: Promise<Response>
    try {
      request = start()
      // Inside the try: a `start` that returns no promise throws here, and must
      // still give its place back.
      request.then(bodyArrived, () => undefined).finally(() => release(gate))
    } catch (error) {
      release(gate)
      return Promise.reject(error)
    }
    return request
  }
  if (gate.out < READ_GATE_WIDTH) {
    gate.out += 1
    return run()
  }
  return new Promise<Response>((resolve, reject) => {
    const aborted = () => signal?.reason ?? new DOMException('aborted', 'AbortError')
    if (signal?.aborted) { reject(aborted()); return }
    const turn = () => {
      signal?.removeEventListener('abort', leave)
      run().then(resolve, reject)
    }
    const leave = () => {
      const at = line.indexOf(turn)
      if (at >= 0) line.splice(at, 1)
      reject(aborted())
    }
    signal?.addEventListener('abort', leave, { once: true })
    line.push(turn)
  })
}
