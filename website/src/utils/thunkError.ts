/**
 * Reading an error that has crossed a Redux Toolkit thunk boundary.
 *
 * This module exists because `e instanceof Error` is WRONG on the rejection
 * `dispatch(someThunk()).unwrap()` throws, and wrong in a way that fails
 * silently. `createAsyncThunk` does not rethrow what the payload creator threw:
 * it stores `miniSerializeError(err)` on the rejected action, and `unwrap()`
 * throws THAT — a plain object carrying only the string-valued `name`,
 * `message`, `stack` and `code`. The class is gone, so:
 *
 *   - `e instanceof Error` is false, and the usual
 *     `e instanceof Error ? e.message : String(e)` idiom yields
 *     `String(plainObject)` === `'[object Object]'`;
 *   - any NON-string field is dropped outright, `ApiError.status` included, so a
 *     numeric status cannot be read back on the far side at all.
 *
 * A classifier that matches on the message — "is this a 404?", "was this an
 * auth denial?" — therefore stops matching the moment its error travels through
 * a thunk, and misclassifies every rejection as the fallback case. That is not
 * hypothetical: it shipped twice, in two copies of the same "the slot is gone,
 * open a fresh session" fallback (Issue Radar and Auto Improvement), and made
 * both fallbacks unreachable. One definition lives here so the next thunk-side
 * classifier inherits the fix instead of re-deriving the bug.
 */

/** The human message of a rejection, in whichever shape it arrives.
 *
 * Handles a real `Error`, the serialized `{ message }` object a thunk boundary
 * produces, and a thrown primitive (a bare string IS its own message).
 *
 * Returns `''` -- never `'[object Object]'` -- for an object carrying no usable
 * message, and that is a contract callers depend on rather than a detail.
 * `String(someObject)` cannot produce a message: it produces the class tag, which
 * is truthy, so a caller writing the usual `errMessage(e) || <fallback>` would
 * render `'[object Object]'` to the user INSTEAD of its fallback. Emptiness is
 * the only honest answer for "this rejection said nothing", and it lets every
 * caller keep its own fallback. */
export function errMessage(e: unknown): string {
  if (e instanceof Error) return e.message
  if (e && typeof e === 'object') {
    const { message } = e as { message?: unknown }
    return typeof message === 'string' ? message : ''
  }
  // Primitives only: a thrown string or number is its own message. `null` and
  // `undefined` stringify to the words "null"/"undefined", which are noise.
  return e == null ? '' : String(e)
}

/** True when a rejection means the chat slot is genuinely GONE (the slot-detail
 * fetch 404s), as opposed to a transient failure reaching the gateway.
 *
 * Deliberately narrow, and the narrowness is the point: only a missing slot
 * justifies replacing a session. Reading a network blip or a 500 as "deleted"
 * would orphan a session that is still alive and overwrite the record pointing
 * at it, which is a worse failure than the one the fallback repairs. */
export function isMissingSlotError(e: unknown): boolean {
  const msg = errMessage(e)
  return /\b404\b/.test(msg) || /not found/i.test(msg)
}
