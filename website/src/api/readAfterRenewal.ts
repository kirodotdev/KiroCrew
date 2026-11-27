// Leaf modules, not `./client`: most tests mock that module with a bare
// factory, and the refusal check below must not depend on exports they leave out.
import { ApiError } from './apiError'
import { pendingRefresh } from './refreshOnce'

/**
 * One read, tried once more when the gateway refused it because the dashboard
 * sign-in had run out. On that refusal the client starts renewing the sign-in
 * and hands the refusal back unchanged (`checkSessionExpired`), so the first
 * read after a lapse fails while the same read a moment later answers. For a
 * read that opts out of the default retry, nothing else reads it again soon.
 * The second read waits for a renewal that is still running. A renewal that
 * failed, or a second read that fails, leaves the read failed.
 */
export async function readAfterRenewal<T>(read: () => Promise<T>): Promise<T> {
  try {
    return await read()
  } catch (e) {
    // A proxy's own sign-in page sets `authRequired` too, and no renewal clears it.
    if (!(e instanceof ApiError && e.authRequired && !e.edgeChallenge)) throw e
    const renewal = pendingRefresh()
    if (renewal && !(await renewal).ok) throw e
    return read()
  }
}
