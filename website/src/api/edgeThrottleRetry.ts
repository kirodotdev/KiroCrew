import { isEdgeThrottleBody } from './apiError'

/** Attempts a throttled WRITE gets in total: the first send plus two retries. */
export const EDGE_THROTTLE_WRITE_ATTEMPTS = 3

/**
 * Backoff before retry number `attempt` (0-based) after an edge throttle. Shared
 * by the read ladder (api/queryClient.ts) and the write retry below. The jitter
 * keeps requests that were throttled together from re-bursting in lockstep and
 * tripping the limit again.
 */
export const edgeThrottleDelayMs = (attempt: number): number =>
  Math.min(1_000 * 2 ** attempt, 15_000) + Math.random() * 500

/** Which write a retry belongs to, and the caller's cancel. */
export interface EdgeThrottleWrite {
  method: string
  url: string
  /** The request body, read only to see whether a fork names its message. */
  body?: unknown
  signal?: AbortSignal | null
}

/** The hub's route to a remote crew, `crewPeerUrl` in api/client/instances.ts. */
const PEER_PROXY_PREFIX = /^\/api\/instances\/[^/]+\/proxy(?=\/)/

/** The one DELETE that is not a removal: it cancels a scheduled continue, whichever is armed when it lands. */
const LIMIT_CONTINUE_PATH = /^\/api\/chat\/slots\/[^/]+\/limit-continue$/

/** POSTs that set a value, so a second landing leaves what the first did. */
const STATE_SET_POST_PATHS: readonly RegExp[] = [/^\/api\/chat\/mode$/]

const FORK_PATH = /^\/api\/chat\/slots\/[^/]+\/fork$/

/** True when a fork body names the message it forks at, so a later landing forks the same point. */
function forkNamesItsMessage(body: unknown): boolean {
  if (typeof body !== 'string') return false
  try {
    const parsed = JSON.parse(body) as { at_message_index?: unknown; at_message_id?: unknown }
    return Number.isInteger(parsed.at_message_index)
      || (typeof parsed.at_message_id === 'string' && parsed.at_message_id !== '')
  } catch {
    return false
  }
}

/**
 * True for a write the edge-throttle retry may re-send.
 *
 * The rule: a write is retried only when its second landing equals its first.
 * An edge 429 means the first never reached the gateway, so the retry is the
 * only landing; it must still do what the user asked when it lands later.
 *
 * - PUT, PATCH and DELETE set or remove a named thing and are retried, except
 *   `DELETE .../limit-continue`, which cancels whichever continue is armed then.
 * - A POST is retried only when it is on the allowlist: a state-set such as
 *   `/api/chat/mode`, or a fork that names its message (`at_message_index` or
 *   `at_message_id`). A fork without one copies whatever the transcript holds
 *   when it lands.
 * - Every other POST is not: a turn send, a stop or cancel, a task start, an
 *   upload. Its throttle keeps the visible 429.
 *
 * A remote crew's route through the hub's peer proxy is judged as its own path.
 */
export const isRetryableWrite = (write: EdgeThrottleWrite): boolean => {
  const path = write.url.split(/[?#]/, 1)[0].replace(PEER_PROXY_PREFIX, '')
  const method = write.method.toUpperCase()
  if (method === 'PUT' || method === 'PATCH') return true
  if (method === 'DELETE') return !LIMIT_CONTINUE_PATH.test(path)
  if (method !== 'POST') return false
  if (STATE_SET_POST_PATHS.some((allowed) => allowed.test(path))) return true
  return FORK_PATH.test(path) && forkNamesItsMessage(write.body)
}

/** Writes to the same method and URL are sent one after another. */
const targetOf = (write: EdgeThrottleWrite): string => `${write.method.toUpperCase()} ${write.url}`

/** Per target, the last write in line; it settles once its whole send-and-retry ends. */
const lastInLineByTarget = new Map<string, Promise<void>>()

/** Resolves when `pending` settles or `signal` aborts, whichever comes first. */
function untilSettledOrAborted(pending: Promise<unknown>, signal: AbortSignal | null | undefined): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve()
      return
    }
    const finish = () => {
      signal?.removeEventListener('abort', finish)
      resolve()
    }
    signal?.addEventListener('abort', finish, { once: true })
    pending.then(finish, finish)
  })
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

async function sendRetryingEdgeThrottle(send: () => Promise<Response>, write: EdgeThrottleWrite): Promise<Response> {
  for (let attempt = 0; ; attempt++) {
    const response = await send()
    const isLastAttempt = attempt >= EDGE_THROTTLE_WRITE_ATTEMPTS - 1
    // Duck-typed like keepIfRefused in client.ts: some callers' fakes carry no `clone`.
    if (response.status !== 429 || isLastAttempt || typeof response.clone !== 'function') return response
    const body = await response.clone().text().catch(() => '')
    if (!isEdgeThrottleBody(body)) return response
    await untilSettledOrAborted(sleep(edgeThrottleDelayMs(attempt)), write.signal)
    if (write.signal?.aborted) throw write.signal.reason
  }
}

/**
 * Re-send a write the EDGE throttled, before any caller sees the failure.
 *
 * A dashboard write is a one-shot: a redux thunk (`chat/forkSlot`) or a
 * `useMutation` calls the api method once and renders whatever it throws. The
 * QueryClient's 429 ladder covers queries only, so without this an edge throttle
 * on a write lost the action while its message said the dashboard retries
 * automatically. Retrying in `jfetch` (api/client.ts), which every write passes
 * through, the helpers and the raw-`fetch` endpoints alike, keeps that promise
 * without each call site opting in.
 *
 * Re-sending a non-idempotent write is safe for this failure only: the edge
 * rejects before the gateway sees the request, so nothing was applied. A 429 the
 * gateway raised (`isEdgeThrottleBody` is false) is a decision, such as a fork
 * past the live-slot cap, and is returned untouched.
 *
 * Only a write `isRetryableWrite` accepts is retried, and only those wait in
 * line: writes to one method and URL go out one at a time, in the order they were
 * made, each after the previous one's sends and retries end. A retry therefore
 * never lands after a newer write: a setting flipped back during the backoff
 * keeps the last choice, and every write of a throttled burst is still re-sent.
 * A cancel while waiting in line or during the backoff rejects with the signal's
 * reason, as the send itself would.
 *
 * Every other write goes straight out and keeps its 429. Reads are not
 * retried here: they already have the QueryClient ladder, and stacking the two
 * would multiply the requests sent to an edge asking for less traffic.
 *
 * The body is peeked through `clone()` so the caller still reads it. A rejected
 * send (offline, an aborted signal) passes straight through.
 */
export async function retryEdgeThrottle(send: () => Promise<Response>, write: EdgeThrottleWrite): Promise<Response> {
  if (!isRetryableWrite(write)) return send()
  const target = targetOf(write)
  const previous = lastInLineByTarget.get(target)
  let leaveLine = () => {}
  const mine = new Promise<void>((resolve) => { leaveLine = resolve })
  // Chained on `previous` so a cancelled write still holds its place: the one
  // behind it waits for every write ahead, not just the next.
  const lineEnd = previous ? previous.then(() => mine) : mine
  lastInLineByTarget.set(target, lineEnd)
  try {
    if (previous) {
      await untilSettledOrAborted(previous, write.signal)
      if (write.signal?.aborted) throw write.signal.reason
    }
    return await sendRetryingEdgeThrottle(send, write)
  } finally {
    leaveLine()
    if (lastInLineByTarget.get(target) === lineEnd) {
      void lineEnd.then(() => {
        if (lastInLineByTarget.get(target) === lineEnd) lastInLineByTarget.delete(target)
      })
    }
  }
}
