// Names the large binary allocation that precedes a renderer OOM.
//
// Why this exists: the black-screen crashes are renderer V8 aborts, and the
// always-on native log (native-logging.js) finally captured the fatal reason —
// `V8 javascript OOM (CALL_AND_RETRY_LAST)` with `Heap: used=21.9MB
// limit=4192.0MB`. The GC-managed JS heap was 0.5% full, so the limit hit was
// NOT the object heap: it was the V8 pointer-compression cage/sandbox, the
// bounded virtual region that also holds ArrayBuffer/TypedArray backing stores.
// So the memory is being eaten by a large *binary buffer*, not by JS objects —
// but V8 reported `stack trace capture may not succeed` and logged no JS stack,
// so the log names the failure without naming the allocation.
//
// This fills that gap: it wraps the buffer-allocating constructors and, for an
// allocation at or above a threshold, reports the requested size and a JS stack
// to the main process BEFORE the allocation runs. Reporting first is the whole
// point — a cage OOM aborts the process rather than throwing a catchable error,
// so a post-hoc hook would die with the renderer. `ipcRenderer.send` posts the
// message to the main-process pipe synchronously, so the culprit is on its way
// to the log before the fatal allocation is attempted. The main process buffers
// these in a bounded ring and flushes them next to the crash line (see
// big-alloc-log.js), so a normal install writes nothing in steady state and
// every future OOM carries the allocations that led up to it.
//
// Cost discipline, matching pierrePerf.ts: the wrappers add one numeric compare
// on each buffer construction; the expensive part (capturing a stack, an IPC)
// runs only at or above the threshold, which a normal session never reaches.
// Nothing accumulates in the renderer — the ring buffer lives in main — and a
// hard per-session report cap bounds the IPC even against a runaway allocator.
//
// Caveat, deliberately accepted: wrapping the global constructors with a Proxy
// preserves `instanceof` and the statics (via the default get trap), but code
// that compares `x.constructor === ArrayBuffer` sees the original constructor,
// not the Proxy, so such an identity check would be false. That pattern is rare
// (real code uses `instanceof` or `ArrayBuffer.isView`), and the value of naming
// the OOM culprit outweighs it. The C++ side (structuredClone, transfer lists,
// TypedArray internals) operates on the real object and is unaffected.
//
// REMOVAL CONDITION — deferred-deletion, not furniture. Once a crash dump has
// reported the allocation kind + size + stack that precedes the cage OOM, the
// culprit is named and the fix belongs where that allocation is made. Delete
// this module, its IPC channel, its preload entry and the main-side log then,
// rather than leaving a permanent constructor patch in the renderer.

/** Report shape sent to the main process. Field names are the log line. */
export interface BigAllocEvent {
  /** The constructor that was called, e.g. "ArrayBuffer" or "Uint8Array". */
  kind: string
  /** Requested size in bytes (length × element size), best-effort. */
  bytes: number
  /** "requested" before the allocation; "failed" if the constructor threw. */
  outcome: 'requested' | 'failed'
  /** Trimmed JS stack naming the caller. */
  stack: string
  /** Present only when outcome === "failed". */
  error?: string
}

/** Default threshold: 64 MiB. Large enough that a normal session never trips it
 *  (icons, avatars, ordinary payloads are kilobytes), small enough to catch the
 *  binary buffers that pressure a 4 GB cage. Overridable at runtime via
 *  `globalThis.__KIROCREW_BIG_ALLOC_BYTES__` for tuning without a rebuild. */
export const DEFAULT_MIN_BYTES = 64 * 1024 * 1024

/** Bounds IPC even if something allocates large buffers in a loop. The main-side
 *  ring only keeps the last N anyway, so past this cap the renderer stops
 *  sending — a runaway allocator must not turn a diagnostic into a flood. */
const MAX_REPORTS_PER_SESSION = 4096

/** The buffer-allocating globals, held as constructor REFERENCES rather than
 *  name-string literals — the i18n source-string gate flags any added string
 *  literal that could be user-visible copy, and a constructor's own `.name`
 *  carries the identity without a literal. Typed arrays are included because
 *  `new Uint8Array(n)` allocates a backing store WITHOUT calling the JS
 *  `ArrayBuffer` constructor, so wrapping ArrayBuffer alone would miss them.
 *  SharedArrayBuffer is appended only where the runtime defines it. */
const WATCHED_CTORS: Array<{ name: string }> = [
  ArrayBuffer,
  Int8Array,
  Uint8Array,
  Uint8ClampedArray,
  Int16Array,
  Uint16Array,
  Int32Array,
  Uint32Array,
  Float32Array,
  Float64Array,
  BigInt64Array,
  BigUint64Array,
]
if (typeof SharedArrayBuffer !== 'undefined') {
  WATCHED_CTORS.push(SharedArrayBuffer)
}

interface Patched {
  target: Record<string, unknown>
  name: string
  original: unknown
}

let installed: Patched[] | null = null
let reportCount = 0

function minBytes(): number {
  const override = (globalThis as unknown as { __KIROCREW_BIG_ALLOC_BYTES__?: number })
    .__KIROCREW_BIG_ALLOC_BYTES__
  return typeof override === 'number' && override > 0 ? override : DEFAULT_MIN_BYTES
}

/** Best-effort requested size from the constructor arguments. A typed array or
 *  buffer constructed OVER an existing buffer (first arg not a number) allocates
 *  no fresh backing store, so it returns -1 and is skipped. */
function requestedBytes(Ctor: unknown, args: unknown[]): number {
  const first = args[0]
  if (typeof first !== 'number' || !Number.isFinite(first) || first <= 0) return -1
  const per = Number((Ctor as { BYTES_PER_ELEMENT?: number }).BYTES_PER_ELEMENT) || 1
  return first * per
}

/** Captures the caller stack, dropping this module's own frames so the first
 *  reported frame is the real allocation site. Bounded to keep the log line
 *  readable; preload re-caps as the trust boundary. */
function captureStack(): string {
  const raw = new Error().stack || ''
  const lines = raw.split('\n')
  // Drop the "Error" header and any frame mentioning this module.
  const frames = lines
    .slice(1)
    .filter((l) => !/allocWatch/.test(l))
    .map((l) => l.trim())
  return frames.slice(0, 8).join(' <- ')
}

function send(ev: BigAllocEvent): void {
  if (reportCount >= MAX_REPORTS_PER_SESSION) return
  const api = (window as unknown as {
    electronAPI?: { reportBigAlloc?: (e: BigAllocEvent) => void }
  }).electronAPI
  const report = api && api.reportBigAlloc
  if (typeof report !== 'function') return
  reportCount += 1
  try {
    report(ev)
  } catch {
    // Never let a diagnostic break allocation.
  }
}

function wrap(name: string, Original: unknown): unknown {
  if (typeof Original !== 'function') return Original
  return new Proxy(Original as new (...a: unknown[]) => unknown, {
    construct(target, args, newTarget) {
      const bytes = requestedBytes(target, args)
      if (bytes >= minBytes()) {
        const stack = captureStack()
        // Report BEFORE constructing: a cage OOM aborts the process instead of
        // throwing, so this may be the last thing this renderer does.
        send({ kind: name, bytes, outcome: 'requested', stack })
        try {
          return Reflect.construct(target, args, newTarget)
        } catch (e) {
          send({ kind: name, bytes, outcome: 'failed', stack, error: String(e) })
          throw e
        }
      }
      return Reflect.construct(target, args, newTarget)
    },
  })
}

/**
 * Patches the buffer-allocating globals. Idempotent — a second call is a no-op.
 * Safe to call in any renderer: it only reports when `electronAPI.reportBigAlloc`
 * exists, so a plain-browser dashboard patches the constructors but sends nothing.
 */
export function installAllocWatch(scope: Record<string, unknown> = globalThis as unknown as Record<string, unknown>): void {
  if (installed) return
  const patched: Patched[] = []
  for (const Ctor of WATCHED_CTORS) {
    const name = Ctor.name
    const original = scope[name]
    if (typeof original !== 'function') continue
    scope[name] = wrap(name, original)
    patched.push({ target: scope, name, original })
  }
  installed = patched
}

/** Test seam: restores the original globals and resets the session counter. */
export function uninstallAllocWatch(): void {
  if (!installed) return
  for (const p of installed) p.target[p.name] = p.original
  installed = null
  reportCount = 0
}
