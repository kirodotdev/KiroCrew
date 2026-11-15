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
 * A read a person is waiting on is marked by its caller and waits in its own
 * line, which is served before the other one. One read is marked today: the
 * newest page of a transcript, asked for when a chat is opened. A cold open
 * asks for dozens of reads at once, and unmarked that page started only after
 * every read still waiting when it was asked for.
 *
 * shortcut: a marked read is never held back for an unmarked one, so unmarked
 * reads could wait without end if marked reads kept coming; give unmarked reads
 * an age limit before many endpoints mark. Marking the session list, the
 * branding, the theme and the setup check as well showed the transcript sooner
 * still, but the sidebar then mounts early and is drawn again as each later
 * read lands (long tasks +207 to +320 ms, the last read 263 to 368 ms later,
 * measured); cut those redraws before marking them. Counts this document only:
 * a second browser tab on the same gateway has its own count.
 */
export const READ_GATE_WIDTH = 4

interface Gate {
  out: number
  /** Marked reads that are waiting; served before `waiting`, in their own order. */
  first: Array<() => void>
  waiting: Array<() => void>
}

const gates = new Map<string, Gate>()

/** Test seam: forget every read that is out or waiting. */
export function __resetReadGateForTests(): void {
  gates.clear()
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
    gate = { out: 0, first: [], waiting: [] }
    gates.set(origin, gate)
  }
  return gate
}

function release(gate: Gate): void {
  const next = gate.first.shift() ?? gate.waiting.shift()
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
 * it waits rejects with the signal's reason and is never started. A read asked
 * for with *first* waits ahead of every read asked for without it.
 */
export function gatedRead(input: string, signal: AbortSignal | null | undefined, start: () => Promise<Response>, first = false): Promise<Response> {
  const gate = gateFor(input)
  const line = first ? gate.first : gate.waiting
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
