/**
 * Rewind helper — wraps `api.rewind` with a rollback callback that fires on
 * failure. Separate from ChatPage so the success/failure branches can be
 * unit-tested without mounting the full chat page.
 */

import { api } from '../api/client'

/**
 * Call `/api/chat/slots/{slot}/rewind` and invoke `rollback` with the error if
 * the request rejects. Logs a warning on failure too.
 */
export async function rewindWithRollback(
  slot: string,
  ts: string,
  content: string,
  rollback: (e: unknown) => void,
): Promise<void> {
  try {
    await api.rewind(slot, ts, content)
  } catch (e) {
    // Intentional diagnostic beside the caller's notice: the console keeps
    // the stack the notice does not show.
    // eslint-disable-next-line no-console
    console.warn('rewind failed', e)
    rollback(e)
  }
}
