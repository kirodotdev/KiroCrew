/**
 * A wheel and a pointer move of a live panel on the dashboard's one socket, so that a scroll does not wait behind a
 * request of its own. Only when the gateway says that it takes them there (its answer to `hello`, asked at the first
 * wheel or move on a socket): an older gateway drops a word it does not know, so with no answer every event keeps the
 * request (`api.browserPanelInput`). It listens to nothing by itself: a panel's source hands it the socket's words.
 *
 * The gateway answers each event on the socket with what the request would answer, named by the event's count `n`.
 * A press, a key and a text never go this way.
 */
import type { BrowserPanelEvent, BrowserPanelInputAnswer } from '../../../api/client/browserLive'
import { sendXrPanel, xrPanelLinkOpen } from '../../../hooks/xrPanelLink'

/** How long an event's answer is waited for before it counts as lost (ms). */
export const INPUT_ANSWER_MS = 3000
/** The shortest time between two wheels, or two moves, of one page on the socket (ms): at most 62 a second of each. */
export const INPUT_GAP_MS = 16

type Answer = BrowserPanelInputAnswer | null

let takes = false
let count = 0
/** Whether this socket's gateway was asked what it takes. */
let asked = false
const waiting = new Map<number, { done: (answer: Answer) => void; timer: number }>()

function settle(n: number, answer: Answer): void {
  const w = waiting.get(n)
  if (!w) return
  waiting.delete(n)
  window.clearTimeout(w.timer)
  w.done(answer)
}

/** The socket opened (again): what the last one said holds no more, and the next wheel asks anew. */
export function panelInputOpened(): void { takes = false; asked = false }

/** The socket closed: every wait ends with no answer. */
export function panelInputClosed(): void {
  takes = false
  asked = false
  for (const n of [...waiting.keys()]) settle(n, null)
}

/**
 * One `xr_panel` message, handed on by a panel's source (each source hears every message, so one may come here more
 * than once). `true` when it was this link's own word.
 */
export function panelInputHeard(data: Record<string, unknown>): boolean {
  if (data.op === 'hello') { takes = data.input === true; return true }
  if (data.op !== 'input') return false
  if (typeof data.n !== 'number') return true
  // An event that the gateway added to a newer one, dropped or refused has no answer of its own.
  const none = data.merged === true || data.dropped === true || typeof data.error === 'string'
  settle(data.n, none ? null : { ...(data as unknown as BrowserPanelInputAnswer), ok: true })
  return true
}

/** Whether a wheel or a move goes on the socket now. */
export function socketTakesInput(): boolean { return takes && xrPanelLinkOpen() }

/**
 * Sends a wheel or a move of the panel `handle` on the socket. `null` when it does not go this way (the gateway did
 * not say that it takes them, there is no socket, or the event is another kind): the caller sends its request.
 */
export function sendPanelInput(handle: string, event: BrowserPanelEvent): Promise<Answer> | null {
  const kind = event.type === 'wheel' || (event.type === 'mouse' && event.action === 'move')
  if (!kind || !handle || !xrPanelLinkOpen()) return null
  // The first wheel or move on a socket asks its gateway once, and goes by its request meanwhile.
  if (!asked) { asked = true; sendXrPanel({ op: 'hello' }) }
  if (!takes) return null
  count = count >= 2 ** 31 - 1 ? 1 : count + 1
  const n = count
  return new Promise<Answer>(done => {
    waiting.set(n, { done, timer: window.setTimeout(() => settle(n, null), INPUT_ANSWER_MS) })
    if (!sendXrPanel({ op: 'input', handle, n, event })) settle(n, null)
  })
}

/** For tests: as a tab that was just loaded. */
export function resetPanelInputLink(): void {
  for (const n of [...waiting.keys()]) settle(n, null)
  takes = false
  asked = false
  count = 0
}
