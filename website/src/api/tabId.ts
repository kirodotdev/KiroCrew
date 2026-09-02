/**
 * Identifies THIS browser tab for the lifetime of the page.
 *
 * Queue broadcasts are delivered to every owner socket, so a consumer that must act only on
 * its OWN action — a cancel handing the question back to the composer — has no other way to
 * tell its own echo from another tab's. Deliberately per-tab and in-memory: two tabs of the
 * same user are exactly the case being separated, so anything persisted or per-user would
 * defeat the purpose.
 *
 * It lives in its own module rather than in `api/client` because a test that mocks the API
 * module would otherwise replace this too, silently making every origin comparison compare
 * against `undefined`.
 */
export const TAB_ID: string = globalThis.crypto?.randomUUID?.()
  ?? `tab-${Math.random().toString(36).slice(2)}-${Date.now()}`
